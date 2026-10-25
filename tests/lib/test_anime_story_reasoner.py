"""Tier 1 story reasoner — after the first real Qwen3-VL run on #253-1 failed.

Real failure (RTX 3060, Qwen3-VL-4B-Thinking NF4): 3/4 calls hit max_new_tokens,
w01 "unterminated JSON", 0 grounded claims — yet the synthesizer wrote an
invented logline (a man, a watch, a girl, a wallet) and the report said PENDING.

The pipeline is now split into short JSON steps (extract ≤4 observed events →
reason only over grounded event ids → verify → synthesize only grounded claims
within the analysed scope). A scripted fake backend stands in for the model.
"""

from __future__ import annotations

import csv
import itertools
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from lib import anime_story_reasoner as sr
from lib.anime_source_indexer import build_source_index
from tests.lib.test_anime_source_indexer import _transcript

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")


@pytest.fixture(scope="session")
def base_run(tmp_path_factory) -> Path:
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg not installed")
    base = tmp_path_factory.mktemp("reasoner")
    video = base / "episode.mp4"
    cmd = ["ffmpeg", "-v", "error", "-y"]
    for colour, d in (("red", 6), ("blue", 7), ("green", 7)):
        cmd += ["-f", "lavfi", "-i", f"color=c={colour}:s=320x240:r=24000/1001:d={d}"]
    cmd += ["-f", "lavfi", "-i", "sine=f=440:d=20", "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
            "-map", "[v]", "-map", "3:a", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(video)]
    subprocess.run(cmd, check=True, timeout=120)
    transcript = base / "t.json"
    transcript.write_text(json.dumps(_transcript(), ensure_ascii=False), encoding="utf-8")
    out = base / "run"
    build_source_index(video, out, transcript_path=transcript, provenance_kind="mock")
    return out


@pytest.fixture
def run_dir(base_run, tmp_path) -> Path:
    work = tmp_path / "ket_qua" / "ep_full_1"
    shutil.copytree(base_run, work)
    return work


def _story(run_dir: Path) -> dict:
    return json.loads((run_dir / "reasoner" / "story_map.json").read_text(encoding="utf-8"))


def _md(run_dir: Path, name: str = "ban_do_cot_truyen.md") -> str:
    return (run_dir / "reasoner" / name).read_text(encoding="utf-8")


# ── parser (fix: escaped quotes broke valid JSON; truncation must be explicit) ──


def test_parser_handles_escaped_quotes_and_braces_inside_strings():
    data = {"a": 'x "', "b": "{", "why_funny_vi": 'mẹ hét "Shin-chan!" rồi { ném dép', "c": "C:\\", "d": "}"}
    text = json.dumps(data, ensure_ascii=False)
    assert sr.parse_model_json(text) == data


def test_parser_reports_truncated_json_explicitly():
    with pytest.raises(sr.ReasonerError, match="bị cắt"):
        sr.parse_model_json('{"events": [{"id": "e1", "what_vi": "Shin chạy')


def test_parser_ignores_json_inside_thinking_and_reads_the_answer():
    assert sr.parse_model_json('<think>maybe {"not": "this"}</think>\n{"real": 1}') == {"real": 1}
    assert sr.parse_model_json('no opening tag {x}\n</think>\n```json\n{"ok": true}\n```') == {"ok": True}


def test_parser_rejects_answers_without_json():
    with pytest.raises(sr.ReasonerError):
        sr.parse_model_json("<think>long thoughts</think>Tôi không chắc.")


# ── planner / grounding units ───────────────────────────────────────────


def test_windows_are_contiguous_and_bounded():
    index = {"scenes": [{"scene_id": f"sc{i:03d}", "start_seconds": i * 5.0, "end_seconds": i * 5.0 + 5.0}
                        for i in range(1, 10)]}
    windows = sr.build_windows(index, max_seconds=12.0, max_scenes=3)
    assert [w["scene_ids"] for w in windows][:2] == [["sc001", "sc002"], ["sc003", "sc004"]]
    assert all(a["end_seconds"] == b["start_seconds"] for a, b in itertools.pairwise(windows))


def test_vietnamese_time_format():
    assert sr.mmss(65.4) == "01:05.4" and sr.mmss(0) == "00:00.0"


def test_extracted_events_are_observations_capped_and_grounded():
    window = {"window_id": "w02", "scene_ids": ["sc002"], "start_seconds": 6.0, "end_seconds": 13.0}
    frames = [{"t": 6.5, "scene_id": "sc002", "path": "a"}, {"t": 9.0, "scene_id": "sc002", "path": "b"}]
    scenes = [{"scene_id": "sc002", "start_seconds": 6.0, "end_seconds": 13.0}]
    events = [{"id": f"e{i}", "start_seconds": 8.0, "end_seconds": 9.0, "who": ["Shin"], "what_vi": f"việc {i}",
               "evidence": {"frame_times": [9.1], "line_ids": []}} for i in range(1, 7)]
    events.append({"id": "e9", "start_seconds": 8.0, "end_seconds": 9.0, "what_vi": "không bằng chứng",
                   "evidence": {"frame_times": [], "line_ids": ["sc999_l01"]}})
    claims = sr.ground_events({"events": events}, window, frames, {"sc002_l01"}, scenes)
    assert len(claims) == sr.MAX_EVENTS_PER_WINDOW
    assert all(c["kind"] == "observation" and c["evidence"]["frame_times"] == [9.0] for c in claims)
    assert claims[0]["claim_id"] == "w02.e1" and claims[0]["scene_ids"] == ["sc002"]


def test_reasoning_may_only_reference_grounded_events():
    events = {"w01.e1": {"claim_id": "w01.e1", "start_seconds": 1.0, "end_seconds": 2.0, "scene_ids": ["sc001"], "kind": "observation"},
              "w01.e2": {"claim_id": "w01.e2", "start_seconds": 3.0, "end_seconds": 4.0, "scene_ids": ["sc001"], "kind": "observation"}}
    reasoning = {
        "links": [{"id": "c1", "cause": "w01.e1", "effect": "w01.e2", "explanation_vi": "vì A nên B", "kind": "inference"},
                  {"id": "c2", "cause": "w01.e7", "effect": "w01.e2", "explanation_vi": "bịa", "kind": "inference"}],
        "gags": [{"id": "g1", "setup": "w01.e1", "payoff": "w01.e2", "mechanism": "callback", "why_funny_vi": "gọi lại", "kind": "inference"},
                 {"id": "g2", "setup": "", "payoff": "", "mechanism": "other", "why_funny_vi": "đồng hồ và ví", "kind": "inference"}],
        "misunderstandings": [{"id": "m1", "who_vi": "Misae", "believes_vi": "Shiro ăn", "truth_vi": "Shin ăn",
                               "events": ["w01.e2"], "kind": "observation"}],
    }
    claims, dropped = sr.ground_reasoning(reasoning, "w01", events)
    ids = {c["claim_id"] for c in claims}
    assert ids == {"w01.c1", "w01.g1", "w01.m1"}
    assert dropped == 2  # c2 cites a non-existent event; g2 cites nothing
    link = next(c for c in claims if c["claim_id"] == "w01.c1")
    assert (link["start_seconds"], link["end_seconds"]) == (1.0, 4.0) and link["refs"] == ["w01.e1", "w01.e2"]
    assert next(c for c in claims if c["claim_id"] == "w01.m1")["kind"] == "inference"  # reasoning is never "observation"


# ── scripted fake model ─────────────────────────────────────────────────


def _events_json(meta, *, grounded=True):
    t0 = meta["start_seconds"]
    first = meta["lines"][0]["line_id"] if meta["lines"] else None
    ev = lambda i, dt, line: {"id": f"e{i}", "start_seconds": t0 + dt, "end_seconds": t0 + dt + 1.0, "who": ["Shinnosuke"],
                              "what_vi": f"sự kiện {i} ở {meta['window_id']}",
                              "evidence": {"frame_times": [t0 + dt + 0.1] if grounded else [],
                                           "line_ids": [line] if (grounded and line) else []}}
    return {"events": [ev(1, 0.5, first), ev(2, 2.0, None)], "need_closer_look": []}


class FakeQwen:
    def __init__(self, *, grounded=True, truncate=None, verdict="supported", synthesis=None, gpu_after_load="NVIDIA GeForce RTX 3060"):
        self.calls, self.grounded, self.truncate, self.verdict = [], grounded, truncate, verdict
        self.synthesis, self.gpu_after_load, self.loaded = synthesis, gpu_after_load, False

    def info(self):
        return {"backend": "fake", "model_id": "fake-qwen", "quantization": "none",
                "gpu": self.gpu_after_load if self.loaded else None}

    def chat(self, system, parts, *, think_budget, answer_budget, on_progress=None, label=""):
        self.loaded = True
        text = "\n".join(p["text"] for p in parts if "text" in p)
        role = system.split("]")[0] + "]"
        self.calls.append({"role": role, "text": text, "images": [p["image"] for p in parts if "image" in p],
                           "think_budget": think_budget, "answer_budget": answer_budget})
        stats = {"prompt_tokens": 500, "generated_tokens": 300, "thinking_tokens": 200, "answer_tokens": 100,
                 "think_forced_close": False, "hit_max_new_tokens": False, "seconds": 0.2,
                 "tokens_per_second": 15.0, "peak_vram_mb": 4662}
        if on_progress:
            on_progress(f"    … {label}: 300 token")
        if role == sr.ROLE_EXTRACTOR:
            meta = json.loads(text.split("WINDOW_JSON:", 1)[1].split("\n", 1)[0])
            if self.truncate == meta["window_id"]:
                return '<think>SECRET-THOUGHT</think>{"events": [{"id": "e1", "what_vi": "Shin', {**stats, "hit_max_new_tokens": True}
            return "<think>SECRET-THOUGHT</think>" + json.dumps(_events_json(meta, grounded=self.grounded), ensure_ascii=False), stats
        if role == sr.ROLE_REASONER:
            wid = json.loads(text.split("WINDOW_JSON:", 1)[1].split("\n", 1)[0])["window_id"]
            return json.dumps({
                "links": [{"id": "c1", "cause": f"{wid}.e1", "effect": f"{wid}.e2", "explanation_vi": "vì đói nên ăn", "kind": "inference"}],
                "character_states": [{"character": "Shinnosuke", "goal_vi": "ăn bánh", "obstacle_vi": "mẹ cấm",
                                      "mistake_vi": "", "reaction_vi": "", "events": [f"{wid}.e1"]}],
                "misunderstandings": [], "gags": [],
                "memory_update": {"story_so_far_vi": f"MEMORY-AFTER-{wid}", "characters": [], "open_setups": []},
            }, ensure_ascii=False), stats
        if role == sr.ROLE_VERIFIER:
            return json.dumps({"verdict": self.verdict, "seen_vi": "thấy rõ"}), stats
        if role == sr.ROLE_SYNTHESIZER:
            ids = [line.split()[0] for line in text.splitlines() if line.startswith("w0")]
            data = self.synthesis(ids) if self.synthesis else {
                "logline_vi": "Shin ăn vụng bánh.", "logline_claim_ids": ids[:1],
                "causal_chain": [{"step": 1, "claim_ids": ids[:2], "explanation_vi": "đói nên ăn"}],
                "gags": [], "conflicts": [], "turning_points": [], "main_characters": [], "why_watchable_vi": ""}
            return json.dumps(data, ensure_ascii=False), stats
        raise AssertionError(role)


def _run(run_dir, fake, **kw):
    kw.setdefault("max_seconds", 10.0)
    kw.setdefault("frames_per_window", 6)
    return sr.run_reasoner(run_dir, fake, **kw)


# ── regression: the real failure modes ─────────────────────────────────


@needs_ffmpeg
def test_zero_grounded_claims_is_fail_and_synthesizer_is_never_called(run_dir):
    fake = FakeQwen(grounded=False)
    report = _run(run_dir, fake, max_windows=2)
    assert report["overall"] == "FAIL"
    assert not any(c["role"] == sr.ROLE_SYNTHESIZER for c in fake.calls)
    assert not any(c["role"] == sr.ROLE_REASONER for c in fake.calls)  # nothing grounded to reason about
    story = _story(run_dir)
    assert story["synthesis"]["status"] == "skipped_no_grounded_claims"
    md = _md(run_dir)
    assert "FAIL" in md and "Logline" not in md


@needs_ffmpeg
def test_synthesizer_content_without_valid_claim_ids_is_removed(run_dir):
    invented = lambda ids: {"logline_vi": "Một người đàn ông, chiếc đồng hồ, cô gái và chiếc ví.", "logline_claim_ids": [],
                            "causal_chain": [{"step": 1, "claim_ids": [], "explanation_vi": "tranh cãi cộng đồng"}],
                            "gags": [{"setup_claim_ids": ["w09.e9"], "payoff_claim_ids": [], "mechanism": "x", "why_funny_vi": "ví"}],
                            "conflicts": [{"claim_ids": [], "desc_vi": "đồng hồ"}], "turning_points": [],
                            "main_characters": [], "why_watchable_vi": "cô gái"}
    _run(run_dir, FakeQwen(synthesis=invented), max_windows=1)
    syn = _story(run_dir)["synthesis"]
    assert syn["status"] == "failed_no_grounded_content"
    md = _md(run_dir)
    for word in ("đồng hồ", "chiếc ví", "cô gái", "tranh cãi cộng đồng"):
        assert word not in md, word


@needs_ffmpeg
def test_truncated_json_fails_that_window_without_retrying_the_same_budget(run_dir):
    fake = FakeQwen(truncate="w01")
    report = _run(run_dir, fake, max_windows=1)
    w01 = [c for c in fake.calls if c["role"] == sr.ROLE_EXTRACTOR]
    assert len(w01) == 1
    call = report["calls"][0]
    assert call["hit_max_new_tokens"] is True and "bị cắt" in call["parse_error"]
    raw = run_dir / "reasoner" / call["raw_file"]
    assert raw.is_file() and "SECRET-THOUGHT" in raw.read_text(encoding="utf-8")
    assert report["overall"] == "FAIL"


@needs_ffmpeg
def test_pilot_synthesizes_only_the_analysed_windows_and_says_so(run_dir):
    fake = FakeQwen()
    _run(run_dir, fake, max_windows=1)
    story = _story(run_dir)
    assert story["scope"]["kind"] == "pilot" and story["scope"]["analysed_windows"] == ["w01"]
    synth = next(c for c in fake.calls if c["role"] == sr.ROLE_SYNTHESIZER)
    assert "w02." not in synth["text"] and "THỬ NGHIỆM" in synth["text"]
    md = _md(run_dir)
    assert "THỬ NGHIỆM" in md and "không phải toàn bộ tập" in md and "1/3" in md


@needs_ffmpeg
def test_thinking_text_never_reaches_the_story_map(run_dir):
    _run(run_dir, FakeQwen(), max_windows=1)
    assert "SECRET-THOUGHT" not in _md(run_dir)
    assert "SECRET-THOUGHT" not in (run_dir / "reasoner" / "story_map.json").read_text(encoding="utf-8")
    assert list((run_dir / "reasoner" / "raw").glob("*.txt"))


@needs_ffmpeg
def test_call_limit_stops_the_run_and_is_reported(run_dir):
    fake = FakeQwen()
    report = _run(run_dir, fake, max_calls=2)
    assert len(fake.calls) == 2
    assert report["stats"]["stopped_by_call_limit"] is True
    assert "giới hạn lượt gọi" in _md(run_dir, "qwen_trial_report.md")


@needs_ffmpeg
def test_per_call_diagnostics_are_recorded_and_shown(run_dir):
    report = _run(run_dir, FakeQwen(), max_windows=1)
    for call in report["calls"]:
        for key in ("step", "window_id", "seconds", "thinking_tokens", "answer_tokens", "hit_max_new_tokens",
                    "parse_error", "raw_file", "think_budget", "answer_budget"):
            assert key in call, key
    rep = _md(run_dir, "qwen_trial_report.md")
    assert "token suy nghĩ" in rep and "trích xuất" in rep


@needs_ffmpeg
def test_gpu_name_comes_from_the_backend_after_loading(run_dir):
    report = _run(run_dir, FakeQwen(), max_windows=1)
    assert report["model"]["gpu"] == "NVIDIA GeForce RTX 3060"


@needs_ffmpeg
def test_live_progress_messages_cover_window_frames_step_calls_tokens_and_claims(run_dir):
    messages = []
    _run(run_dir, FakeQwen(), max_windows=1, progress=messages.append)
    joined = "\n".join(messages)
    for needle in ("w01", "frame", "trích xuất", "lượt gọi 1", "token", "kết luận hợp lệ"):
        assert needle in joined, needle


@needs_ffmpeg
def test_happy_path_short_window_produces_grounded_claims_with_japanese_quotes(run_dir):
    fake = FakeQwen()
    report = _run(run_dir, fake, max_windows=1, verify_limit=1)
    assert report["overall"] == "PENDING"
    story = _story(run_dir)
    claims = story["windows"][0]["claims"]
    assert {c["category"] for c in claims} >= {"event", "link"}
    assert "sc001_l01「ケーキ買ってきたよ。」" in _md(run_dir)
    budgets = {c["role"]: (c["think_budget"], c["answer_budget"]) for c in fake.calls}
    assert budgets[sr.ROLE_EXTRACTOR] == sr.BUDGETS["extract"]
    assert sum(1 for c in fake.calls if c["role"] == sr.ROLE_VERIFIER) == 1


@needs_ffmpeg
def test_memory_from_window_one_reaches_window_two(run_dir):
    fake = FakeQwen()
    _run(run_dir, fake, max_windows=2, verify_limit=0)
    extract_w02 = [c for c in fake.calls if c["role"] == sr.ROLE_EXTRACTOR][1]
    assert "MEMORY-AFTER-w01" in extract_w02["text"]


@needs_ffmpeg
def test_verifier_rejection_demotes_inference(run_dir):
    _run(run_dir, FakeQwen(verdict="unsupported"), max_windows=1, verify_limit=5)
    links = [c for c in _story(run_dir)["windows"][0]["claims"] if c["category"] == "link"]
    assert links and all(c["kind"] == "hypothesis" and c["verifier"]["verdict"] == "unsupported" for c in links)


@needs_ffmpeg
def test_comparison_row_and_human_score(run_dir):
    _run(run_dir, FakeQwen(), max_windows=1)
    sheet = run_dir / "reasoner" / sr.CHECK_SHEET
    rows = list(csv.DictReader(sheet.open(encoding="utf-8-sig")))
    for i, row in enumerate(rows):
        row["ket_qua"] = "DUNG" if i % 2 == 0 else "SAI"
    with sheet.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    score = sr.score_story_map(run_dir)
    assert score["judged"] == len(rows) and 0 < score["accuracy"] < 1
    table = list(csv.DictReader((run_dir.parent / sr.COMPARISON).open(encoding="utf-8-sig")))
    row = next(r for r in table if r["run"] == run_dir.name)
    assert row["pham_vi"] == "pilot 1/3" and row["do_chinh_xac_nguoi_cham"] == f"{score['accuracy']:.3f}"
