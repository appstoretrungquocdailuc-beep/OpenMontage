"""Vision/LLM providers for Phase 3 — no network: fake HTTP opener and fake SDK client."""

from __future__ import annotations

import base64
import io
import json
import shutil
import subprocess
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib import anime_vision_providers as vp

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
HAS_FFMPEG = shutil.which("ffmpeg") is not None


@pytest.fixture
def jpg(tmp_path) -> Path:
    path = tmp_path / "kf.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    return path


# ── Ollama (local, free) ────────────────────────────────────────────────


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _opener(responses: dict, seen: list):
    def open_(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else request
        body = json.loads(request.data.decode("utf-8")) if getattr(request, "data", None) else None
        seen.append((url, body))
        for suffix, payload in responses.items():
            if url.endswith(suffix):
                if isinstance(payload, Exception):
                    raise payload
                return _Resp(json.dumps(payload).encode("utf-8"))
        raise AssertionError(url)
    return open_


def test_ollama_sends_schema_images_and_system(jpg):
    seen = []
    opener = _opener({"/api/chat": {"message": {"content": '{"ok": true}'}, "prompt_eval_count": 120, "eval_count": 8}}, seen)
    provider = vp.OllamaProvider("qwen2.5vl:7b", opener=opener)
    data, usage = provider.generate_json("SYS", "PROMPT", SCHEMA, images=[jpg])
    assert data == {"ok": True}
    url, body = seen[0]
    assert url == "http://127.0.0.1:11434/api/chat"
    assert body["model"] == "qwen2.5vl:7b" and body["stream"] is False
    assert body["format"] == SCHEMA
    assert body["messages"][0] == {"role": "system", "content": "SYS"}
    assert body["messages"][1]["content"] == "PROMPT"
    assert body["messages"][1]["images"] == [base64.b64encode(jpg.read_bytes()).decode()]
    assert (usage.input_tokens, usage.output_tokens, usage.cost_usd) == (120, 8, 0.0)


def test_ollama_strips_think_block_and_disables_thinking_for_qwen3():
    seen = []
    opener = _opener({"/api/chat": {"message": {"content": '<think>hmm</think>\n{"ok": true}'}}}, seen)
    data, _ = vp.OllamaProvider("qwen3:14b", opener=opener).generate_json("S", "P", SCHEMA)
    assert data == {"ok": True}
    assert seen[0][1]["think"] is False


def test_ollama_unreachable_gives_actionable_error():
    opener = _opener({"/api/chat": urllib.error.URLError("refused")}, [])
    with pytest.raises(vp.ProviderError, match="ollama serve"):
        vp.OllamaProvider("qwen2.5vl:7b", opener=opener).generate_json("S", "P", SCHEMA)


def test_ollama_non_json_answer_is_an_error():
    opener = _opener({"/api/chat": {"message": {"content": "xin chào"}}}, [])
    with pytest.raises(vp.ProviderError, match="JSON"):
        vp.OllamaProvider("qwen2.5vl:7b", opener=opener).generate_json("S", "P", SCHEMA)


def test_ollama_check_reports_missing_models():
    opener = _opener({"/api/tags": {"models": [{"name": "qwen2.5vl:7b"}]}}, [])
    status = vp.OllamaProvider("qwen3:14b", opener=opener).check()
    assert status["reachable"] is True and status["model_available"] is False
    assert "ollama pull qwen3:14b" in status["hint"]


# ── Anthropic (paid, opt-in) ────────────────────────────────────────────


class _FakeMessages:
    def __init__(self, response):
        self.response, self.calls = response, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _claude_response(text='{"ok": true}', stop="end_turn", **usage):
    u = {"input_tokens": 1000, "output_tokens": 200, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    u.update(usage)
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop,
                           stop_details=None, usage=SimpleNamespace(**u), model="claude-opus-5-5")


def _claude(response):
    messages = _FakeMessages(response)
    client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    return vp.AnthropicProvider(client=client), messages


def test_anthropic_request_uses_structured_output_fallbacks_and_images(jpg):
    provider, messages = _claude(_claude_response())
    data, usage = provider.generate_json("SYS", "PROMPT", SCHEMA, images=[jpg])
    assert data == {"ok": True}
    call = messages.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    assert call["output_config"]["effort"] in {"low", "medium", "high", "xhigh", "max"}
    assert call["betas"] == ["server-side-fallback-2026-07-01"] and call["fallbacks"] == "default"
    assert "temperature" not in call and "thinking" not in call
    image = call["messages"][0]["content"][0]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/jpeg"
    assert image["source"]["data"] == base64.standard_b64encode(jpg.read_bytes()).decode()
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert usage.cost_usd == pytest.approx(1000 * 4 / 1e6 + 200 * 20 / 1e6)


def test_anthropic_refusal_and_truncation_are_errors():
    provider, _ = _claude(_claude_response(stop="refusal"))
    with pytest.raises(vp.ProviderError, match="refus"):
        provider.generate_json("S", "P", SCHEMA)
    provider, _ = _claude(_claude_response(text='{"ok": tr', stop="max_tokens"))
    with pytest.raises(vp.ProviderError, match="max_tokens"):
        provider.generate_json("S", "P", SCHEMA)


def test_anthropic_without_sdk_explains_how_to_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)
    with pytest.raises(vp.ProviderError, match="pip install anthropic"):
        vp.AnthropicProvider().generate_json("S", "P", SCHEMA)


def test_cost_estimate_is_zero_for_local_and_positive_for_claude():
    assert vp.estimate_cost("ollama", "qwen2.5vl:7b", input_tokens=10**6, output_tokens=10**5) == 0.0
    assert vp.estimate_cost("anthropic", "claude-opus-5-5", input_tokens=10**6, output_tokens=10**5) == pytest.approx(6.0)


# ── fake provider and image preparation ─────────────────────────────────


def test_fake_provider_replays_scripted_answers_and_records_calls(jpg):
    fake = vp.FakeProvider([{"ok": True}, lambda system, prompt, schema, images: {"ok": len(images) == 1}])
    assert fake.generate_json("S", "P1", SCHEMA)[0] == {"ok": True}
    assert fake.generate_json("S", "P2", SCHEMA, images=[jpg])[0] == {"ok": True}
    assert [c["prompt"] for c in fake.calls] == ["P1", "P2"]
    assert fake.name == "fake"


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_prepare_image_downscales_once_and_caches(tmp_path):
    src = tmp_path / "big.jpg"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=1920x1080:d=0.1",
                    "-frames:v", "1", str(src)], check=True, timeout=60)
    out = vp.prepare_image(src, tmp_path / "cache", max_width=640)
    width = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=width",
                            "-of", "csv=p=0", str(out)], capture_output=True, text=True, check=True).stdout.strip()
    assert width == "640"
    mtime = out.stat().st_mtime_ns
    assert vp.prepare_image(src, tmp_path / "cache", max_width=640) == out and out.stat().st_mtime_ns == mtime
