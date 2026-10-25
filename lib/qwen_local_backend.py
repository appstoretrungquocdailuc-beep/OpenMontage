"""Local Qwen3-VL backends for the anime story reasoner (offline, $0).

Both backends take the same input: a system prompt plus an ordered list of
parts, ``{"text": ...}`` or ``{"image": Path}``. The reasoner interleaves each
frame with its source timestamp (``[t=12.345s sc004]``) so the model reads a
time-ordered frame sequence with exact times, next to the Whisper lines.

- ``TransformersQwenBackend`` — the official ``Qwen/Qwen3-VL-4B-Thinking``
  weights via Hugging Face ``transformers`` (``Qwen3VLForConditionalGeneration``),
  4-bit NF4 with bitsandbytes on an RTX 3060-class GPU. Lives in its own venv
  (``.venv-qwen``) so the Whisper venv is never touched.
- ``OllamaQwenBackend`` — ``qwen3-vl`` from the Ollama library, as a second local
  model for comparison.

Nothing here talks to a cloud API; the only network use is the one-time model
download from huggingface.co / ollama.com during setup.

CLI (inside .venv-qwen)::

    python -m lib.qwen_local_backend setup      # pip install + download + smoke test
    python -m lib.qwen_local_backend check
"""

from __future__ import annotations

import argparse
import base64
import json
import platform
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

DEFAULT_MODEL_ID = "Qwen/Qwen3-VL-4B-Thinking"
DEFAULT_OLLAMA_MODEL = "qwen3-vl:4b"
TORCH_INDEX_URL = "https://download.pytorch.org/whl/cu126"
# Qwen's recommended sampling for the Thinking variants.
SAMPLING = {"do_sample": True, "temperature": 0.6, "top_p": 0.95, "top_k": 20}


class BackendError(RuntimeError):
    pass


def split_thinking(text: str) -> tuple[str, str]:
    """(reasoning, answer). Handles a missing opening <think> tag."""
    if "</think>" not in text:
        return "", text.strip()
    head, answer = text.split("</think>", 1)
    return head.replace("<think>", "").strip(), answer.strip()


def build_hf_messages(system: str, parts: list[dict]) -> list[dict]:
    content = []
    for part in parts:
        if "image" in part:
            content.append({"type": "image", "image": str(part["image"])})
        else:
            content.append({"type": "text", "text": part["text"]})
    return [{"role": "system", "content": [{"type": "text", "text": system}]}, {"role": "user", "content": content}]


class TransformersQwenBackend:
    backend = "transformers"

    def __init__(self, model_id: str = DEFAULT_MODEL_ID, *, quant: str = "nf4", seed: int = 253):
        if quant not in ("nf4", "bf16"):
            raise BackendError("quant must be nf4 or bf16")
        self.model_id, self.quant, self.seed = model_id, quant, seed
        self._model = self._processor = None
        self._device_name = None

    def info(self) -> dict:
        return {
            "backend": self.backend,
            "model_id": self.model_id,
            "quantization": "bitsandbytes-nf4" if self.quant == "nf4" else "bf16",
            "gpu": self._device_name,
        }

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            import torch
            from transformers import (
                AutoProcessor,
                BitsAndBytesConfig,
                Qwen3VLForConditionalGeneration,
            )
        except (ImportError, TypeError) as exc:
            raise BackendError(
                "torch / transformers>=4.57 / bitsandbytes are not installed in this Python. "
                "Run scripts\\anime_trial\\6_CAI_DAT_QWEN3_VL.bat (creates .venv-qwen)."
            ) from exc
        if not torch.cuda.is_available():
            raise BackendError("PyTorch does not see a CUDA GPU — reinstall torch from the CUDA wheel index (setup does this)")
        kwargs: dict[str, Any] = {"dtype": torch.bfloat16, "device_map": "cuda", "attn_implementation": "sdpa"}
        if self.quant == "nf4":
            kwargs["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_use_double_quant=True,
            )
        self._model = Qwen3VLForConditionalGeneration.from_pretrained(self.model_id, **kwargs)
        self._processor = AutoProcessor.from_pretrained(self.model_id)
        self._device_name = torch.cuda.get_device_name(0)

    def chat(self, system: str, parts: list[dict], *, max_new_tokens: int = 4096) -> tuple[str, dict]:
        self._load()
        import torch

        torch.manual_seed(self.seed)
        inputs = self._processor.apply_chat_template(
            build_hf_messages(system, parts), tokenize=True, add_generation_prompt=True,
            return_dict=True, return_tensors="pt",
        ).to(self._model.device)
        torch.cuda.reset_peak_memory_stats()
        start = time.perf_counter()
        with torch.inference_mode():
            out = self._model.generate(**inputs, max_new_tokens=max_new_tokens, **SAMPLING)
        seconds = time.perf_counter() - start
        prompt_len = inputs["input_ids"].shape[1]
        generated = out[:, prompt_len:]
        text = self._processor.batch_decode(generated, skip_special_tokens=True)[0]
        n = int(generated.shape[1])
        return text, {
            "prompt_tokens": int(prompt_len), "generated_tokens": n, "seconds": round(seconds, 2),
            "tokens_per_second": round(n / seconds, 2) if seconds else None,
            "peak_vram_mb": round(torch.cuda.max_memory_allocated() / 2**20),
            "hit_max_new_tokens": n >= max_new_tokens,
        }


class OllamaQwenBackend:
    backend = "ollama"

    def __init__(self, model: str = DEFAULT_OLLAMA_MODEL, *, host: str = "http://127.0.0.1:11434",
                 num_ctx: int = 32768, timeout: float = 1800.0, opener: Callable = urllib.request.urlopen):
        self.model, self.host, self.num_ctx, self.timeout, self._open = model, host.rstrip("/"), num_ctx, timeout, opener

    def info(self) -> dict:
        return {"backend": self.backend, "model_id": self.model, "quantization": "ollama-default (GGUF)", "gpu": None}

    def chat(self, system: str, parts: list[dict], *, max_new_tokens: int = 4096) -> tuple[str, dict]:
        messages: list[dict] = [{"role": "system", "content": system}]
        for part in parts:
            if "image" in part:
                if messages[-1].get("role") != "user" or messages[-1].get("images"):
                    messages.append({"role": "user", "content": ""})
                messages[-1]["images"] = [base64.b64encode(Path(part["image"]).read_bytes()).decode("ascii")]
            else:
                messages.append({"role": "user", "content": part["text"]})
        body = {"model": self.model, "messages": messages, "stream": False,
                "options": {"num_ctx": self.num_ctx, "num_predict": max_new_tokens, "temperature": 0.6, "top_p": 0.95, "top_k": 20}}
        request = urllib.request.Request(f"{self.host}/api/chat", data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
        start = time.perf_counter()
        try:
            with self._open(request, timeout=self.timeout) as resp:
                reply = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            raise BackendError(f"cannot reach Ollama ({exc}); start Ollama and run `ollama pull {self.model}`") from exc
        message = reply.get("message", {})
        text = message.get("content", "")  # Ollama returns the reasoning separately in message.thinking
        n = int(reply.get("eval_count", 0))
        eval_s = reply.get("eval_duration", 0) / 1e9
        return text, {
            "prompt_tokens": int(reply.get("prompt_eval_count", 0)), "generated_tokens": n,
            "seconds": round(time.perf_counter() - start, 2),
            "tokens_per_second": round(n / eval_s, 2) if eval_s else None, "peak_vram_mb": None,
            "hit_max_new_tokens": n >= max_new_tokens, "thinking_chars": len(message.get("thinking") or ""),
        }


# ── setup / check (run inside .venv-qwen on the user's PC) ───────────────


def install_commands(python: str = sys.executable) -> list[list[str]]:
    return [
        [python, "-m", "pip", "install", "--upgrade", "pip"],
        [python, "-m", "pip", "install", "torch", "torchvision", "--index-url", TORCH_INDEX_URL],
        [python, "-m", "pip", "install", "transformers>=4.57.0", "accelerate", "bitsandbytes>=0.45.0",
         "pillow", "huggingface_hub"],
    ]


def environment_report() -> dict:
    report: dict[str, Any] = {"python": platform.python_version()}
    try:
        import torch

        report["torch"] = torch.__version__
        report["cuda_available"] = torch.cuda.is_available()
        report["cuda_version"] = torch.version.cuda
        if torch.cuda.is_available():
            report["gpu"] = torch.cuda.get_device_name(0)
            report["gpu_total_mb"] = round(torch.cuda.get_device_properties(0).total_memory / 2**20)
    except ImportError:
        report["torch"] = None
    for pkg in ("transformers", "bitsandbytes", "accelerate"):
        try:
            report[pkg] = __import__(pkg).__version__
        except ImportError:
            report[pkg] = None
    return report


def smoke_test(backend: TransformersQwenBackend) -> dict:
    """Real-model sanity check: one solid-colour frame, ask its colour."""
    with tempfile.TemporaryDirectory() as tmp:
        img = Path(tmp) / "red.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=640x360:d=0.1",
                        "-frames:v", "1", str(img)], check=True, timeout=60)
        text, stats = backend.chat(
            "You are a careful vision assistant.",
            [{"text": "[t=0.000s]"}, {"image": img},
             {"text": 'What is the dominant colour of this frame? Answer only JSON: {"colour": "<english word>"}'}],
            max_new_tokens=1024,
        )
    thinking, answer = split_thinking(text)
    match = re.search(r"\{.*\}", answer, re.DOTALL)
    colour = json.loads(match.group(0)).get("colour", "") if match else ""
    return {"ok": "red" in colour.lower(), "answer": answer[:200], "thinking_chars": len(thinking), **stats}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lib.qwen_local_backend")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("setup", help="install torch/transformers into THIS venv, download the model, smoke test")
    s.add_argument("--model", default=DEFAULT_MODEL_ID)
    s.add_argument("--quant", choices=["nf4", "bf16"], default="nf4")
    s.add_argument("--skip-install", action="store_true")
    sub.add_parser("check", help="print torch / CUDA / transformers versions")
    args = parser.parse_args(argv)

    if args.command == "check":
        print(json.dumps(environment_report(), indent=2))
        return 0
    if not args.skip_install:
        for cmd in install_commands():
            print("$", " ".join(cmd))
            if subprocess.run(cmd, check=False).returncode != 0:
                print("[LOI] pip install that bai — xem thong bao o tren")
                return 1
    print(json.dumps(environment_report(), indent=2))
    from huggingface_hub import snapshot_download

    print(f"Tai model {args.model} (~9 GB lan dau) ...")
    path = snapshot_download(args.model)
    print(f"Model o: {path}")
    result = smoke_test(TransformersQwenBackend(args.model, quant=args.quant))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
