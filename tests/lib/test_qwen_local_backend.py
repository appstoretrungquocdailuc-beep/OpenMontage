"""Local Qwen3-VL backends — message construction and output parsing only.

The real model (transformers + CUDA, or Ollama) is not available in CI; these
tests pin the request shapes so a run on the user's GPU sends what we expect.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import pytest

from lib import qwen_local_backend as qb


@pytest.fixture
def frames(tmp_path) -> list[Path]:
    out = []
    for i in range(2):
        p = tmp_path / f"f{i}.jpg"
        p.write_bytes(b"\xff\xd8jpeg" + bytes([i]))
        out.append(p)
    return out


def test_split_thinking_separates_reasoning_from_answer():
    thinking, answer = qb.split_thinking("<think>\nreasoning here\n</think>\n\n{\"a\": 1}")
    assert thinking == "reasoning here" and answer == '{"a": 1}'
    # transformers often drops the opening tag of Qwen3 thinking models
    thinking, answer = qb.split_thinking("reasoning only\n</think>\n{\"a\": 2}")
    assert thinking == "reasoning only" and answer == '{"a": 2}'
    assert qb.split_thinking('{"a": 3}') == ("", '{"a": 3}')


def test_hf_messages_interleave_timestamp_text_and_images(frames):
    parts = [{"text": "HEADER"}, {"text": "[t=1.000s sc001]"}, {"image": frames[0]},
             {"text": "[t=2.500s sc002]"}, {"image": frames[1]}, {"text": "TASK"}]
    messages = qb.build_hf_messages("SYS", parts)
    assert messages[0] == {"role": "system", "content": [{"type": "text", "text": "SYS"}]}
    content = messages[1]["content"]
    assert [c["type"] for c in content] == ["text", "text", "image", "text", "image", "text"]
    assert content[2]["image"] == str(frames[0])
    assert content[3]["text"] == "[t=2.500s sc002]"


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_ollama_backend_sends_one_message_per_frame_with_its_timestamp(frames):
    seen = []

    def opener(request, timeout=None):
        seen.append(json.loads(request.data.decode("utf-8")))
        return _Resp(json.dumps({"message": {"content": '{"ok": true}', "thinking": "hmm"},
                                 "prompt_eval_count": 900, "eval_count": 120, "eval_duration": 2_000_000_000}).encode())

    backend = qb.OllamaQwenBackend("qwen3-vl:4b", opener=opener)
    text, stats = backend.chat("SYS", [{"text": "HEADER"}, {"text": "[t=1.000s sc001]"}, {"image": frames[0]},
                                       {"text": "[t=2.500s sc002]"}, {"image": frames[1]}, {"text": "TASK"}])
    body = seen[0]
    msgs = body["messages"]
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert msgs[1]["content"] == "HEADER" and "images" not in msgs[1]
    assert msgs[2]["content"] == "[t=1.000s sc001]"
    assert msgs[2]["images"] == [base64.b64encode(frames[0].read_bytes()).decode()]
    assert msgs[3]["content"] == "[t=2.500s sc002]"
    assert msgs[-1]["content"] == "TASK"
    assert body["stream"] is False and body["model"] == "qwen3-vl:4b"
    assert text == '{"ok": true}'
    assert stats["generated_tokens"] == 120 and stats["tokens_per_second"] == pytest.approx(60.0)


def test_transformers_backend_explains_missing_dependencies(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "torch", None)
    backend = qb.TransformersQwenBackend()
    with pytest.raises(qb.BackendError, match="6_CAI_DAT_QWEN3_VL"):
        backend.chat("SYS", [{"text": "x"}])


def test_backend_info_names_the_real_model_and_quantization():
    info = qb.TransformersQwenBackend(model_id="Qwen/Qwen3-VL-4B-Thinking", quant="nf4").info()
    assert info["model_id"] == "Qwen/Qwen3-VL-4B-Thinking"
    assert info["quantization"] == "bitsandbytes-nf4"
    assert info["backend"] == "transformers"


def test_install_plan_uses_separate_venv_packages_and_cuda_wheels():
    plan = qb.install_commands(python="py.exe")
    flat = [" ".join(c) for c in plan]
    assert any("torch" in c and "download.pytorch.org/whl/cu" in c for c in flat)
    assert any("transformers>=4.57" in c for c in flat)
    assert any("bitsandbytes" in c for c in flat)
    assert not any("faster-whisper" in c for c in flat)  # Whisper venv untouched
