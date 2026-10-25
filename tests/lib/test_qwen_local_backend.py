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


# ── thinking budget (fix: 3/4 real calls burned all 6144 tokens) ──────

THINK_END, EOS = 99, 0
FORCED = [7, 7, THINK_END, 8]


def _scripted(chunks):
    """generate_fn(prefix, max_new) returning scripted token lists, capped at max_new."""
    calls = []

    def generate(prefix, max_new):
        calls.append((list(prefix), max_new))
        return chunks.pop(0)[:max_new]
    return generate, calls


def test_model_that_closes_thinking_in_budget_is_not_forced():
    gen, calls = _scripted([[1, 2, THINK_END, 5, 6, EOS]])
    r = qb.budgeted_generate(gen, think_end_id=THINK_END, eos_ids={EOS}, forced_close_ids=FORCED,
                             think_budget=10, answer_budget=10)
    assert r["tokens"] == [1, 2, THINK_END, 5, 6, EOS]
    assert (r["thinking_tokens"], r["answer_tokens"], r["think_forced_close"], r["hit_max_new_tokens"]) == (3, 3, False, False)
    assert len(calls) == 1


def test_thinking_over_budget_is_closed_and_the_answer_still_generated():
    gen, calls = _scripted([[1] * 50, [5, 6, EOS]])
    r = qb.budgeted_generate(gen, think_end_id=THINK_END, eos_ids={EOS}, forced_close_ids=FORCED,
                             think_budget=4, answer_budget=10)
    assert r["tokens"] == [1, 1, 1, 1, *FORCED, 5, 6, EOS]
    assert r["think_forced_close"] is True and r["thinking_tokens"] == 4 + len(FORCED)
    assert calls[1] == ([1, 1, 1, 1, *FORCED], 10)  # answer continues after the forced </think>


def test_answer_cut_by_its_budget_is_reported():
    gen, _ = _scripted([[1, THINK_END, 5, 5, 5]])
    r = qb.budgeted_generate(gen, think_end_id=THINK_END, eos_ids={EOS}, forced_close_ids=FORCED,
                             think_budget=10, answer_budget=3)
    assert r["hit_max_new_tokens"] is True and r["answer_tokens"] == 3


def test_answer_continues_when_first_pass_ended_mid_answer():
    gen, calls = _scripted([[1, THINK_END, 5, 5], [6, EOS]])
    r = qb.budgeted_generate(gen, think_end_id=THINK_END, eos_ids={EOS}, forced_close_ids=FORCED,
                             think_budget=4, answer_budget=10)
    assert r["tokens"] == [1, THINK_END, 5, 5, 6, EOS] and r["answer_tokens"] == 4
    assert calls[1][1] == 8  # remaining answer budget


def test_instruct_model_has_no_thinking_phase():
    gen, calls = _scripted([[5, 6, EOS]])
    r = qb.budgeted_generate(gen, think_end_id=None, eos_ids={EOS}, forced_close_ids=[],
                             think_budget=512, answer_budget=10)
    assert r["thinking_tokens"] == 0 and r["answer_tokens"] == 3 and calls[0][1] == 10


def test_is_thinking_model():
    assert qb.is_thinking_model("Qwen/Qwen3-VL-4B-Thinking") is True
    assert qb.is_thinking_model("Qwen/Qwen3-VL-4B-Instruct") is False


def test_gpu_name_reported_even_before_the_model_is_loaded(monkeypatch):
    import sys
    from types import SimpleNamespace

    fake_torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True, get_device_name=lambda i: "NVIDIA GeForce RTX 3060"))
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    assert qb.TransformersQwenBackend().info()["gpu"] == "NVIDIA GeForce RTX 3060"
