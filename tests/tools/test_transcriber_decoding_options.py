"""Optional decoding options (initial_prompt, beam_size) and per-segment
confidence signals (avg_logprob, no_speech_prob) on the transcriber tool.

Defaults must not change: callers that pass neither option get exactly the
previous faster-whisper call.
"""

import sys
from types import SimpleNamespace

from tools.analysis.transcriber import Transcriber


class _Info:
    language = "ja"
    duration = 2.0


def _install_fake(monkeypatch, calls, segments=()):
    class FakeWhisperModel:
        def __init__(self, model_size, *, device, compute_type):
            pass

        def transcribe(self, *args, **kwargs):
            calls.append(kwargs)
            return iter(segments), _Info()

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeWhisperModel))
    monkeypatch.setitem(
        sys.modules,
        "ctranslate2",
        SimpleNamespace(get_cuda_device_count=lambda: 0, get_supported_compute_types=lambda device: set()),
    )


def _input(tmp_path, **extra):
    path = tmp_path / "audio.wav"
    path.write_bytes(b"fake")
    return {"input_path": str(path), "output_dir": str(tmp_path), "language": "ja", **extra}


def test_initial_prompt_and_beam_size_are_forwarded(monkeypatch, tmp_path):
    calls = []
    _install_fake(monkeypatch, calls)
    result = Transcriber().execute(_input(tmp_path, initial_prompt="以下は日本語のアニメの会話です。", beam_size=5))
    assert result.success, result.error
    assert calls[0]["initial_prompt"] == "以下は日本語のアニメの会話です。"
    assert calls[0]["beam_size"] == 5


def test_defaults_leave_the_faster_whisper_call_unchanged(monkeypatch, tmp_path):
    calls = []
    _install_fake(monkeypatch, calls)
    assert Transcriber().execute(_input(tmp_path)).success
    assert "initial_prompt" not in calls[0]
    assert "beam_size" not in calls[0]


def test_segments_carry_avg_logprob_and_no_speech_prob(monkeypatch, tmp_path):
    seg = SimpleNamespace(id=0, start=0.0, end=1.0, text=" はい", words=None, avg_logprob=-0.25, no_speech_prob=0.7)
    _install_fake(monkeypatch, [], segments=[seg])
    result = Transcriber().execute(_input(tmp_path))
    assert result.success, result.error
    out = result.data["segments"][0]
    assert out["avg_logprob"] == -0.25
    assert out["no_speech_prob"] == 0.7


def test_segments_without_those_attributes_still_work(monkeypatch, tmp_path):
    seg = SimpleNamespace(id=0, start=0.0, end=1.0, text="はい", words=None)
    _install_fake(monkeypatch, [], segments=[seg])
    result = Transcriber().execute(_input(tmp_path))
    assert result.success, result.error
    assert "no_speech_prob" not in result.data["segments"][0]
