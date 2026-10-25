"""Tier 1 story reasoner (VideoMind-style roles on a local Qwen3-VL).

A scripted fake backend stands in for the model: these tests pin the
orchestration (windows, timestamped frames, memory, grounding, evidence
requests, verifier, synthesis, outputs) — not the model's intelligence, which
is only measured by real runs on the user's GPU.
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
def run_dir(tmp_path_factory) -> Path:
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


def _index(run_dir: Path) -> dict:
    return json.loads((run_dir / "anime_source_index.json").read_text(encoding="utf-8"))


# ── planner: windows and frames ─────────────────────────────────────────


def test_windows_are_contiguous_and_bounded():
    index = {"scenes": [{"scene_id": f"sc{i:03d}", "start_seconds": i * 5.0, "end_seconds": i * 5.0 + 5.0}
                        for i in range(1, 10)]}
    windows = sr.build_windows(index, max_seconds=12.0, max_scenes=3)
    assert [w["scene_ids"] for w in windows][:2] == [["sc001", "sc002"], ["sc003", "sc004"]]
    assert sum(len(w["scene_ids"]) for w in windows) == 9
    for a, b in itertools.pairwise(windows):
        assert a["end_seconds"] == b["start_seconds"]
    assert all(w["end_seconds"] - w["start_seconds"] <= 12.0 or len(w["scene_ids"]) == 1 for w in windows)


@needs_ffmpeg
def test_window_frames_are_timestamped_sorted_and_inside_the_window(run_dir, tmp_path):
    index = _index(run_dir)
    window = sr.build_windows(index, max_seconds=10.0)[1]
    frames = sr.sample_window_frames(index, window, tmp_path / "frames", interval=1.0, max_frames=8)
    times = [f["t"] for f in frames]
    assert times == sorted(times) and 2 <= len(frames) <= 8
    assert all(window["start_seconds"] <= t < window["end_seconds"] for t in times)
    assert all(Path(f["path"]).is_file() for f in frames)
    assert {f["scene_id"] for f in frames} <= set(window["scene_ids"])


def test_parse_model_json_tolerates_thinking_fences_and_trailing_text():
    assert sr.parse_model_json('<think>x</think>```json\n{"a": 1}\n```\nthanks') == {"a": 1}
    assert sr.parse_model_json('reasoning</think> {"b": {"c": 2}} done') == {"b": {"c": 2}}
    with pytest.raises(sr.ReasonerError):
        sr.parse_model_json("no json here")


# ── grounder ────────────────────────────────────────────────────────────


def _window():
    return {"window_id": "w02", "scene_ids": ["sc002"], "start_seconds": 6.0, "end_seconds": 13.0}


def _frames():
    return [{"t": 6.5, "scene_id": "sc002", "path": "a.jpg"}, {"t": 9.0, "scene_id": "sc002", "path": "b.jpg"}]


def _claim(**kw):
    base = {"event_id": "e1", "kind": "observation", "start_seconds": 8.0, "end_seconds": 9.5,
            "cause_vi": "", "action_vi": "Shin cúi đầu", "reaction_vi": "", "consequence_vi": "",
            "evidence": {"frame_times": [9.1], "line_ids": ["sc002_l01"]}, "confidence": "high"}
    base.update(kw)
    return base


def test_grounding_snaps_frames_keeps_known_lines_and_maps_scenes():
    scenes = [{"scene_id": "sc002", "start_seconds": 6.0, "end_seconds": 13.0}]
    claims, _notes = sr.ground_claims({"events": [_claim()]}, _window(), _frames(), {"sc002_l01"}, scenes)
    c = claims[0]
    assert c["kind"] == "observation" and c["evidence"]["frame_times"] == [9.0]
    assert c["scene_ids"] == ["sc002"] and c["claim_id"] == "w02.e1"


def test_claim_without_valid_evidence_becomes_hypothesis():
    scenes = [{"scene_id": "sc002", "start_seconds": 6.0, "end_seconds": 13.0}]
    raw = _claim(evidence={"frame_times": [12.9], "line_ids": ["sc999_l01"]}, kind="inference")
    claims, _notes = sr.ground_claims({"events": [raw]}, _window(), _frames(), {"sc002_l01"}, scenes)
    assert claims[0]["kind"] == "hypothesis"
    assert {"no_evidence", "unknown_line"} <= set(claims[0]["notes"])


def test_claim_outside_the_window_is_demoted_and_clamped():
    scenes = [{"scene_id": "sc002", "start_seconds": 6.0, "end_seconds": 13.0}]
    claims, _ = sr.ground_claims({"events": [_claim(start_seconds=40.0, end_seconds=41.0)]},
                                 _window(), _frames(), {"sc002_l01"}, scenes)
    assert claims[0]["kind"] == "hypothesis" and "time_outside_window" in claims[0]["notes"]
    assert 6.0 <= claims[0]["start_seconds"] <= claims[0]["end_seconds"] <= 13.0


def test_gags_misunderstandings_and_reversals_become_claims():
    scenes = [{"scene_id": "sc002", "start_seconds": 6.0, "end_seconds": 13.0}]
    analysis = {
        "gags": [{"gag_id": "g1", "kind": "inference", "mechanism": "dramatic_irony", "why_funny_vi": "khán giả biết trước",
                  "setup": {"start_seconds": 6.5, "end_seconds": 7.0, "desc_vi": "giấu bánh",
                            "evidence": {"frame_times": [6.5], "line_ids": []}},
                  "payoff": {"start_seconds": 9.0, "end_seconds": 9.5, "desc_vi": "bị lộ",
                             "evidence": {"frame_times": [9.0], "line_ids": ["sc002_l01"]}}}],
        "misunderstandings": [{"id": "m1", "kind": "inference", "start_seconds": 8.0, "end_seconds": 9.0,
                               "who_vi": "Misae", "believes_vi": "Shiro ăn", "truth_vi": "Shin ăn",
                               "evidence": {"frame_times": [9.0], "line_ids": ["sc002_l01"]}}],
        "escalations_reversals": [{"id": "r1", "type": "reversal", "kind": "observation", "start_seconds": 9.0,
                                   "end_seconds": 9.5, "desc_vi": "nhận lỗi", "evidence": {"frame_times": [9.0], "line_ids": []}}],
    }
    claims, _ = sr.ground_claims(analysis, _window(), _frames(), {"sc002_l01"}, scenes)
    categories = sorted(c["category"] for c in claims)
    assert categories == ["gag_payoff", "gag_setup", "misunderstanding", "reversal"]
    payoff = next(c for c in claims if c["category"] == "gag_payoff")
    assert payoff["links"]["setup"] == "w02.g1.setup" and payoff["why_funny_vi"] == "khán giả biết trước"


# ── full run with a scripted fake model ────────────────────────────────


def _analysis(window, lines, memory_text, request_more=False):
    first = lines[0]["line_id"] if lines else None
    t0, t1 = window["start_seconds"], window["end_seconds"]
    return {
        "window_summary_vi": f"tóm tắt {window['window_id']}",
        "events": [
            {"event_id": "e1", "kind": "observation", "start_seconds": t0 + 0.5, "end_seconds": t0 + 1.5,
             "cause_vi": "", "action_vi": "nhân vật xuất hiện", "reaction_vi": "", "consequence_vi": "",
             "evidence": {"frame_times": [t0 + 0.6], "line_ids": [first] if first else []}, "confidence": "high"},
            {"event_id": "e2", "kind": "inference", "start_seconds": t0 + 1.0, "end_seconds": t1 - 0.5,
             "cause_vi": "đói", "action_vi": "ăn bánh", "reaction_vi": "mẹ giận", "consequence_vi": "bị phạt",
             "evidence": {"frame_times": [t0 + 1.2], "line_ids": []}, "confidence": "medium"},
        ],
        "character_states": [{"character": "Shinnosuke", "goal_vi": "ăn bánh", "desire_vi": "", "obstacle_vi": "mẹ cấm",
                              "mistake_vi": "", "reaction_vi": ""}],
        "misunderstandings": [], "gags": [], "escalations_reversals": [],
        "evidence_requests": [{"start_seconds": t0 + 1.0, "end_seconds": t0 + 2.0, "reason_vi": "xem kỹ"}] if request_more else [],
        "memory_update": {"story_so_far_vi": f"MEMORY-AFTER-{window['window_id']}", "characters": [], "open_setups": []},
    }


class FakeQwen:
    """Dispatches on the role marker in the system prompt."""

    def __init__(self, *, verdict="supported", break_window=None):
        self.calls, self.verdict, self.break_window = [], verdict, break_window
        self.requested = set()

    def info(self):
        return {"backend": "fake", "model_id": "fake-qwen", "quantization": "none", "gpu": None}

    def chat(self, system, parts, *, max_new_tokens=4096):
        text = "\n".join(p["text"] for p in parts if "text" in p)
        images = [p["image"] for p in parts if "image" in p]
        self.calls.append({"role": system.split("]")[0] + "]", "text": text, "images": images})
        stats = {"prompt_tokens": 100, "generated_tokens": 50, "seconds": 0.1, "tokens_per_second": 500.0,
                 "peak_vram_mb": None, "hit_max_new_tokens": False}
        if system.startswith(sr.ROLE_ANSWERER):
            meta = json.loads(text.split("WINDOW_JSON:", 1)[1].split("\n", 1)[0])
            if meta["window_id"] == self.break_window:
                return "I cannot answer in JSON, sorry.", stats
            ask = meta["window_id"] == "w01" and "w01" not in self.requested
            self.requested.add(meta["window_id"])
            memory = text.split("STORY_MEMORY:", 1)[1].split("\n", 1)[0]
            return "<think>ok</think>" + json.dumps(_analysis(meta, meta["lines"], memory, request_more=ask),
                                                    ensure_ascii=False), stats
        if system.startswith(sr.ROLE_VERIFIER):
            return json.dumps({"verdict": self.verdict, "seen_vi": "thấy rõ"}), stats
        if system.startswith(sr.ROLE_SYNTHESIZER):
            ids = [line.split()[0] for line in text.splitlines() if line.startswith("w0")]
            return json.dumps({
                "logline_vi": "Shin ăn vụng bánh của mẹ.",
                "main_characters": [{"character": "Shinnosuke", "want_vi": "ăn bánh", "obstacle_vi": "mẹ", "arc_vi": "nhận lỗi"}],
                "causal_chain": [{"step": 1, "claim_ids": ids[:2] + ["w99.e9"], "explanation_vi": "đói nên ăn"}],
                "gags": [], "conflicts": [{"claim_ids": ids[:1], "desc_vi": "mẹ và con"}],
                "turning_points": [], "why_watchable_vi": "trẻ con và luật nhà", "open_questions_vi": [],
            }, ensure_ascii=False), stats
        raise AssertionError(system[:40])


@needs_ffmpeg
def test_full_run_writes_vietnamese_story_map_with_evidence(run_dir, tmp_path):
    work = tmp_path / "copy"
    shutil.copytree(run_dir, work)
    fake = FakeQwen()
    report = sr.run_reasoner(work, fake, max_seconds=10.0, frames_per_window=6, verify_limit=10)

    story = json.loads((work / "reasoner" / "story_map.json").read_text(encoding="utf-8"))
    assert story["provenance"]["backend"] == "fake" and story["provenance"]["real_model"] is False
    assert len(story["windows"]) == 3
    # memory: the second window's prompt carries the first window's memory update
    answers = [c for c in fake.calls if c["role"] == sr.ROLE_ANSWERER]
    assert "MEMORY-AFTER-w01" in answers[2]["text"]  # w01, w01 (evidence re-ask), w02
    # evidence request: w01 asked again with extra dense frames
    assert len(answers[1]["images"]) > len(answers[0]["images"])
    assert report["stats"]["evidence_rounds"] == 1
    # timestamps are interleaved with the frames
    assert "[t=" in answers[0]["text"]
    # every claim is grounded to scene ids and has a time
    claims = [c for w in story["windows"] for c in w["claims"]]
    assert claims and all(c["scene_ids"] and c["start_seconds"] <= c["end_seconds"] for c in claims)
    # synthesis: unknown claim id dropped, not invented
    chain = story["synthesis"]["causal_chain"][0]
    assert "w99.e9" not in chain["claim_ids"] and "unknown_claim_id" in story["synthesis"]["notes"]

    md = (work / "reasoner" / "ban_do_cot_truyen.md").read_text(encoding="utf-8")
    for needle in ("Bản đồ cốt truyện", "QUAN SÁT", "SUY LUẬN", "KHÔNG phải model thật", "Shin ăn vụng bánh"):
        assert needle in md, needle
    assert "00:0" in md  # mm:ss timestamps
    assert "sc001_l01「ケーキ買ってきたよ。」" in md  # cited line shown with its Japanese text

    rows = list(csv.DictReader((work / "reasoner" / "kiem_tra_ban_do_cot_truyen.csv").open(encoding="utf-8-sig")))
    assert len(rows) == len(claims) and rows[0]["ket_qua"] == ""

    rep = (work / "reasoner" / "qwen_trial_report.md").read_text(encoding="utf-8")
    assert "fake-qwen" in rep and "PENDING" in rep
    assert report["overall"] == "PENDING"


@needs_ffmpeg
def test_verifier_rejection_turns_inference_into_hypothesis(run_dir, tmp_path):
    work = tmp_path / "copy"
    shutil.copytree(run_dir, work)
    sr.run_reasoner(work, FakeQwen(verdict="unsupported"), max_seconds=10.0, frames_per_window=6, verify_limit=10)
    story = json.loads((work / "reasoner" / "story_map.json").read_text(encoding="utf-8"))
    inferred = [c for w in story["windows"] for c in w["claims"] if c.get("verifier")]
    assert inferred and all(c["kind"] == "hypothesis" for c in inferred)
    assert all(c["verifier"]["verdict"] == "unsupported" for c in inferred)


@needs_ffmpeg
def test_unparseable_window_is_retried_once_then_reported_not_invented(run_dir, tmp_path):
    work = tmp_path / "copy"
    shutil.copytree(run_dir, work)
    fake = FakeQwen(break_window="w02")
    report = sr.run_reasoner(work, fake, max_seconds=10.0, frames_per_window=6, verify_limit=0)
    w02_calls = [c for c in fake.calls if c["role"] == sr.ROLE_ANSWERER and '"window_id": "w02"' in c["text"]]
    assert len(w02_calls) == 2
    story = json.loads((work / "reasoner" / "story_map.json").read_text(encoding="utf-8"))
    w02 = next(w for w in story["windows"] if w["window_id"] == "w02")
    assert w02["status"] == "failed" and w02["claims"] == []
    assert report["stats"]["parse_failures"] == 1


@needs_ffmpeg
def test_comparison_table_gets_a_row_and_score_updates_it(run_dir, tmp_path):
    work = tmp_path / "copy"
    shutil.copytree(run_dir, work)
    sr.run_reasoner(work, FakeQwen(), max_seconds=10.0, frames_per_window=6, verify_limit=10)
    sheet = work / "reasoner" / "kiem_tra_ban_do_cot_truyen.csv"
    rows = list(csv.DictReader(sheet.open(encoding="utf-8-sig")))
    for i, row in enumerate(rows):
        row["ket_qua"] = "DUNG" if i % 2 == 0 else "SAI"
    with sheet.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    score = sr.score_story_map(work)
    assert score["judged"] == len(rows) and 0 < score["accuracy"] < 1
    table = list(csv.DictReader((work.parent / "so_sanh_model.csv").open(encoding="utf-8-sig")))
    row = next(r for r in table if r["run"] == work.name)
    assert row["model_id"] == "fake-qwen" and row["do_chinh_xac_nguoi_cham"] == f"{score['accuracy']:.3f}"


def test_vietnamese_time_format():
    assert sr.mmss(65.4) == "01:05.4" and sr.mmss(0) == "00:00.0"
