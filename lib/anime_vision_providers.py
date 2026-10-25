"""Vision / LLM providers for the anime director (Phase 3).

One call shape for every backend::

    data, usage = provider.generate_json(system, prompt, schema, images=[Path, ...])

- ``OllamaProvider`` — local models through Ollama's HTTP API on localhost
  (free, no API key; e.g. ``qwen2.5vl:7b`` for frames, ``qwen3:14b`` for writing).
  JSON is constrained with Ollama's ``format`` = JSON Schema.
- ``AnthropicProvider`` — Claude through the official ``anthropic`` SDK
  (paid, needs ``ANTHROPIC_API_KEY``; default ``claude-opus-5-5``). Uses
  structured outputs (``output_config.format``) and the server-side refusal
  fallback (``fallbacks="default"``).
- ``FakeProvider`` — scripted answers for tests and dry runs; its runs are
  labelled ``fake`` everywhere so they are never mistaken for real analysis.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

OLLAMA_HOST = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_VISION_MODEL = "qwen2.5vl:7b"
DEFAULT_OLLAMA_WRITER_MODEL = "qwen3:14b"
DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
CLAUDE_FALLBACK_BETA = "server-side-fallback-2026-07-01"

# USD per million tokens: (input, output, cache read). Cache writes bill at 1.25x input.
CLAUDE_PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}


class ProviderError(RuntimeError):
    """The model could not produce a usable JSON answer (or is not reachable)."""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cost_usd = round(self.cost_usd + other.cost_usd, 6)


def estimate_cost(provider: str, model: str, *, input_tokens: int, output_tokens: int) -> float:
    if provider != "anthropic":
        return 0.0
    price_in, price_out, _ = CLAUDE_PRICES.get(model, CLAUDE_PRICES[DEFAULT_CLAUDE_MODEL])
    return round(input_tokens * price_in / 1e6 + output_tokens * price_out / 1e6, 6)


def _parse_json(text: str, who: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"{who} did not return JSON: {text[:200]!r}") from exc
    if not isinstance(data, dict):
        raise ProviderError(f"{who} returned JSON that is not an object")
    return data


def prepare_image(src: Path, cache_dir: Path, *, max_width: int = 768) -> Path:
    """Downscaled JPEG copy (fewer vision tokens), cached by name and width."""
    src = Path(src)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / f"{src.stem}_w{max_width}.jpg"
    if out.is_file() and out.stat().st_mtime >= src.stat().st_mtime:
        return out
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vf", f"scale='min(iw,{max_width})':-2", "-q:v", "3", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=False,
    )
    if proc.returncode != 0:
        raise ProviderError(f"ffmpeg could not resize {src}: {proc.stderr.strip()[:200]}")
    return out


def _b64(path: Path) -> str:
    return base64.standard_b64encode(Path(path).read_bytes()).decode("ascii")


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        model: str = DEFAULT_OLLAMA_VISION_MODEL,
        *,
        host: str = OLLAMA_HOST,
        num_ctx: int = 16384,
        timeout: float = 900.0,
        opener: Callable = urllib.request.urlopen,
    ):
        self.model, self.host, self.num_ctx, self.timeout, self._open = model, host.rstrip("/"), num_ctx, timeout, opener

    def _request(self, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(f"{self.host}{path}", data=data, headers={"Content-Type": "application/json"})
        try:
            with self._open(request, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300] if hasattr(exc, "read") else ""
            raise ProviderError(f"Ollama HTTP {exc.code} for {self.model}: {detail}") from exc
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            raise ProviderError(
                f"cannot reach Ollama at {self.host} ({exc}). Start it (open the Ollama app or run `ollama serve`) "
                f"and make sure the model is pulled: `ollama pull {self.model}`"
            ) from exc

    def check(self) -> dict:
        try:
            tags = self._request("/api/tags")
        except ProviderError as exc:
            return {"reachable": False, "model_available": False, "hint": str(exc)}
        names = {m.get("name") for m in tags.get("models", [])}
        available = self.model in names or f"{self.model}:latest" in names
        return {"reachable": True, "model_available": available,
                "hint": "" if available else f"run: ollama pull {self.model}"}

    def generate_json(self, system: str, prompt: str, schema: dict, images: list[Path] | None = None) -> tuple[dict, Usage]:
        user: dict[str, Any] = {"role": "user", "content": prompt}
        if images:
            user["images"] = [_b64(p) for p in images]
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, user],
            "stream": False,
            "format": schema,
            "options": {"temperature": 0.2, "num_ctx": self.num_ctx},
        }
        if self.model.startswith("qwen3"):
            body["think"] = False  # keep the reasoning out of the JSON answer
        reply = self._request("/api/chat", body)
        data = _parse_json(reply.get("message", {}).get("content", ""), f"Ollama {self.model}")
        return data, Usage(int(reply.get("prompt_eval_count", 0)), int(reply.get("eval_count", 0)), 0.0)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str = DEFAULT_CLAUDE_MODEL, *, effort: str = "medium", max_tokens: int = 16000, client: Any = None):
        self.model, self.effort, self.max_tokens, self._client = model, effort, max_tokens, client

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise ProviderError(
                    "the Claude provider needs the official SDK: `.venv-anime\\Scripts\\python.exe -m pip install anthropic` "
                    "and an ANTHROPIC_API_KEY (paid API)"
                ) from exc
            self._client = anthropic.Anthropic()
        return self._client

    def generate_json(self, system: str, prompt: str, schema: dict, images: list[Path] | None = None) -> tuple[dict, Usage]:
        client = self._get_client()
        content: list[dict] = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _b64(p)}}
            for p in images or []
        ]
        content.append({"type": "text", "text": prompt})
        try:
            response = client.beta.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": content}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
                betas=[CLAUDE_FALLBACK_BETA],
                fallbacks="default",
            )
        except Exception as exc:
            raise ProviderError(f"Claude API error ({type(exc).__name__}): {exc}") from exc
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise ProviderError(f"Claude refused the request ({getattr(details, 'category', None)})")
        if response.stop_reason == "max_tokens":
            raise ProviderError("Claude hit max_tokens — answer truncated; try fewer scenes per request")
        text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), "")
        data = _parse_json(text, f"Claude {self.model}")
        u = response.usage
        price_in, price_out, price_cache = CLAUDE_PRICES.get(self.model, CLAUDE_PRICES[DEFAULT_CLAUDE_MODEL])
        cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
        cost = (u.input_tokens * price_in + cache_write * price_in * 1.25 + cache_read * price_cache
                + u.output_tokens * price_out) / 1e6
        return data, Usage(u.input_tokens + cache_write + cache_read, u.output_tokens, round(cost, 6))


class FakeProvider:
    """Scripted answers: each item is a dict or a callable(system, prompt, schema, images) -> dict."""

    name = "fake"

    def __init__(self, answers: list, model: str = "fake"):
        self.answers, self.model, self.calls = list(answers), model, []

    def generate_json(self, system: str, prompt: str, schema: dict, images: list[Path] | None = None) -> tuple[dict, Usage]:
        self.calls.append({"system": system, "prompt": prompt, "schema": schema, "images": list(images or [])})
        if not self.answers:
            raise ProviderError("FakeProvider has no scripted answer left")
        answer = self.answers.pop(0)
        if callable(answer):
            answer = answer(system, prompt, schema, list(images or []))
        if isinstance(answer, Exception):
            raise answer
        return answer, Usage()


def make_provider(name: str, model: str | None = None, **kwargs) -> Any:
    if name == "ollama":
        return OllamaProvider(model or DEFAULT_OLLAMA_VISION_MODEL, **kwargs)
    if name == "anthropic":
        return AnthropicProvider(model or DEFAULT_CLAUDE_MODEL, **kwargs)
    raise ProviderError(f"unknown provider {name!r} (ollama | anthropic)")
