"""anime_source_indexer: anime MP4 → Phase 1 anime_source_index.json.

Pure helpers are tested directly. Media steps run against a synthetic 4-scene
clip rendered by ffmpeg (24000/1001 fps, sine-tone audio) — it contains no
speech, so dialogue comes from a transcript fixture shaped like faster-whisper
output. Real Japanese ASR is exercised only by the opt-in test at the bottom.
"""

from __future__ import annotations

import copy
import itertools
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from lib import anime_source_indexer as asi
from lib.anime_recap import validate_recap_plan

FPS = 24000 / 1001
SCENE_SECONDS = (3.0, 2.5, 4.0, 3.5)  # red, blue, testsrc2, green
HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")

SCHEMA = Path(__file__).resolve().parents[2] / "schemas" / "artifacts" / "anime_source_index.schema.json"


def _word(text: str, start: float, end: float, p: float = 0.9) -> dict:
    return {"word": text, "start": start, "end": end, "probability": p}


def _transcript() -> dict:
    """Shaped like tools/analysis/transcriber.py output (faster-whisper, ja)."""
    return {
        "language": "ja",
        "model_size": "fixture",
        "segments": [
            {"id": 0, "start": 0.5, "end": 2.0, "text": "ケーキ買ってきたよ。",
             "words": [_word("ケーキ", 0.5, 1.0), _word("買ってきた", 1.0, 1.7), _word("よ。", 1.7, 2.0)]},
            # Spans the cut at 3.003 s: first half belongs to sc001, second to sc002.
            {"id": 1, "start": 2.4, "end": 3.9, "text": "食べちゃダメよ！本当に？",
             "words": [_word("食べちゃ", 2.4, 2.7), _word("ダメよ！", 2.7, 2.95),
                       _word("本当", 3.1, 3.5), _word("に？", 3.5, 3.9)]},
            {"id": 2, "start": 6.0, "end": 7.2, "text": "オラが食べたゾ。",
             "words": [_word("オラが", 6.0, 6.5, 0.8), _word("食べたゾ。", 6.5, 7.2, 0.7)]},
            # Classic Whisper hallucination over music/silence.
            {"id": 3, "start": 10.0, "end": 12.5, "text": "ご視聴ありがとうございました",
             "words": [_word("ご視聴", 10.0, 11.0, 0.3), _word("ありがとうございました", 11.0, 12.5, 0.2)]},
        ],
    }


def _scenes() -> list[dict]:
    return [
        {"scene_id": "sc001", "start_seconds": 0.0, "end_seconds": 3.003},
        {"scene_id": "sc002", "start_seconds": 3.003, "end_seconds": 5.505},
        {"scene_id": "sc003", "start_seconds": 5.505, "end_seconds": 9.509},
        {"scene_id": "sc004", "start_seconds": 9.509, "end_seconds": 12.971},
    ]


# ── pure helpers ────────────────────────────────────────────────────────


def test_snap_to_frame_lands_on_frame_grid():
    assert asi.snap_to_frame(3.0, FPS) == pytest.approx(3.003, abs=1e-3)
    assert asi.snap_to_frame(0.0, FPS) == 0.0


def test_parse_fps_keeps_ntsc_precision():
    assert asi.parse_fps("24000/1001") == pytest.approx(23.976024, abs=1e-6)
    assert asi.parse_fps("0/0") == 0.0


def test_keyframe_timestamps_stay_inside_scene():
    stamps = asi.keyframe_timestamps(3.003, 5.505, FPS)
    assert len(stamps) == 3
    assert all(3.003 <= t < 5.505 for t in stamps)
    assert stamps == sorted(stamps)


def test_long_scene_gets_keyframe_at_least_every_six_seconds():
    stamps = asi.keyframe_timestamps(0.0, 40.0, FPS)
    gaps = [b - a for a, b in itertools.pairwise(stamps)]
    assert max(gaps) <= 6.0 + 1e-6
    assert len(stamps) >= 7


def test_dialogue_assigned_by_word_midpoint():
    by_scene = asi.assign_dialogue(_transcript()["segments"], _scenes())
    assert by_scene["sc001"][0]["text_ja"] == "ケーキ買ってきたよ。"
    assert by_scene["sc003"][0]["text_ja"] == "オラが食べたゾ。"


def test_segment_spanning_cut_is_split_at_scene_boundary():
    by_scene = asi.assign_dialogue(_transcript()["segments"], _scenes())
    assert [l["text_ja"] for l in by_scene["sc001"]] == ["ケーキ買ってきたよ。", "食べちゃダメよ！"]
    assert [l["text_ja"] for l in by_scene["sc002"]] == ["本当に？"]
    assert by_scene["sc002"][0]["start_seconds"] >= 3.003


def test_segment_without_words_goes_to_scene_with_most_overlap():
    seg = [{"id": 0, "start": 4.8, "end": 6.5, "text": "あれ？"}]  # 0.705 s in sc002, 0.995 s in sc003
    by_scene = asi.assign_dialogue(seg, _scenes())
    assert [l["text_ja"] for l in by_scene["sc003"]] == ["あれ？"]
    assert by_scene["sc002"] == []


def test_line_ids_are_sequential_per_scene():
    by_scene = asi.assign_dialogue(_transcript()["segments"], _scenes())
    assert [l["line_id"] for l in by_scene["sc001"]] == ["sc001_l01", "sc001_l02"]


def test_asr_confidence_is_mean_word_probability():
    line = asi.assign_dialogue(_transcript()["segments"], _scenes())["sc003"][0]
    assert line["asr_confidence"] == pytest.approx(0.75)
    assert line["speaker"] is None


def test_known_hallucination_is_flagged_low_confidence():
    line = asi.assign_dialogue(_transcript()["segments"], _scenes())["sc004"][0]
    assert line["low_confidence"] is True
    assert "known_hallucination" in line["asr_flags"]


def test_lines_are_clamped_inside_their_scene():
    by_scene = asi.assign_dialogue(_transcript()["segments"], _scenes())
    for scene in _scenes():
        for line in by_scene[scene["scene_id"]]:
            assert scene["start_seconds"] <= line["start_seconds"] < line["end_seconds"] <= scene["end_seconds"]


# ── visual annotations (the agent's look at the keyframes) ─────────────


def _bare_index() -> dict:
    scenes = []
    by_scene = asi.assign_dialogue(_transcript()["segments"], _scenes())
    for s in _scenes():
        scenes.append({**s, "visual_summary": "", "visual_review": "pending", "characters": [],
                       "actions": [], "reactions": [], "keyframes": [], "dialogue": by_scene[s["scene_id"]]})
    return {
        "version": "1.0", "source_index_id": "clip", "provenance": {"kind": "mock", "tools": []},
        "video": {"path": "clip.mp4", "fps": FPS, "duration_seconds": 12.971},
        "characters": [], "scenes": scenes,
    }


def _annotations() -> dict:
    return {
        "source_index_id": "clip",
        "characters": [{"character_id": "misae", "name_ja": "みさえ"}, {"character_id": "shinnosuke", "name_ja": "しんのすけ"}],
        "scenes": [{"scene_id": "sc001", "visual_summary": "赤い画面", "characters": ["misae"],
                    "actions": ["箱を見せる"], "reactions": ["笑顔"]}],
        "lines": [{"line_id": "sc001_l01", "speaker": "misae", "text_vi": "Mẹ mua bánh về rồi."}],
    }


def test_apply_annotations_merges_scene_and_line_fields():
    merged, issues = asi.apply_visual_annotations(_bare_index(), _annotations())
    assert issues == []
    sc1 = merged["scenes"][0]
    assert sc1["visual_summary"] == "赤い画面" and sc1["visual_review"] == "done"
    assert sc1["reactions"] == ["笑顔"]
    assert sc1["dialogue"][0]["speaker"] == "misae"
    assert sc1["dialogue"][0]["text_vi"] == "Mẹ mua bánh về rồi."
    assert {c["character_id"] for c in merged["characters"]} == {"misae", "shinnosuke"}
    assert merged["provenance"]["visual_review"] == "partial"


def test_apply_annotations_does_not_mutate_input():
    index = _bare_index()
    before = copy.deepcopy(index)
    asi.apply_visual_annotations(index, _annotations())
    assert index == before


def test_apply_annotations_marks_review_complete_when_every_scene_done():
    ann = _annotations()
    ann["scenes"] = [{"scene_id": s["scene_id"], "visual_summary": "x", "characters": [], "actions": [], "reactions": []}
                     for s in _scenes()]
    merged, issues = asi.apply_visual_annotations(_bare_index(), ann)
    assert issues == []
    assert merged["provenance"]["visual_review"] == "complete"
    assert "agent_vision" in merged["provenance"]["tools"]


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda a: a.__setitem__("source_index_id", "other"), "index_mismatch"),
        (lambda a: a["scenes"][0].__setitem__("scene_id", "sc999"), "unknown_scene"),
        (lambda a: a["lines"][0].__setitem__("line_id", "sc001_l09"), "unknown_line"),
        (lambda a: a["lines"][0].__setitem__("speaker", "kazama"), "unknown_character"),
        (lambda a: a["scenes"][0].__setitem__("characters", ["kazama"]), "unknown_character"),
    ],
)
def test_apply_annotations_rejects_bad_references(mutate, code):
    ann = _annotations()
    mutate(ann)
    index = _bare_index()
    merged, issues = asi.apply_visual_annotations(index, ann)
    assert code in {i.code for i in issues}
    assert merged == index  # nothing applied when any reference is wrong


# ── media steps on a synthetic sample video ─────────────────────────────


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory) -> Path:
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg not installed")
    out = tmp_path_factory.mktemp("anime_sample") / "sample.mp4"
    sources = [
        f"color=c=red:s=320x240:r=24000/1001:d={SCENE_SECONDS[0]}",
        f"color=c=blue:s=320x240:r=24000/1001:d={SCENE_SECONDS[1]}",
        f"testsrc2=s=320x240:r=24000/1001:d={SCENE_SECONDS[2]}",
        f"color=c=green:s=320x240:r=24000/1001:d={SCENE_SECONDS[3]}",
    ]
    cmd = ["ffmpeg", "-v", "error", "-y"]
    for src in sources:
        cmd += ["-f", "lavfi", "-i", src]
    cmd += ["-f", "lavfi", "-i", f"sine=f=440:d={sum(SCENE_SECONDS)}",
            "-filter_complex", "[0:v][1:v][2:v][3:v]concat=n=4:v=1:a=0[v]",
            "-map", "[v]", "-map", "4:a", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(out)]
    subprocess.run(cmd, check=True, timeout=120)
    return out


@pytest.fixture
def transcript_file(tmp_path) -> Path:
    path = tmp_path / "sample_transcript.json"
    path.write_text(json.dumps(_transcript(), ensure_ascii=False), encoding="utf-8")
    return path


@needs_ffmpeg
def test_probe_video_uses_video_stream_duration_and_exact_fps(sample_video):
    info = asi.probe_video(sample_video)
    assert info["fps"] == pytest.approx(FPS, abs=1e-6)
    assert info["duration_seconds"] == pytest.approx(311 / FPS, abs=1e-3)  # not the 13.0 s audio length
    assert info["has_audio"] is True
    assert (info["width"], info["height"]) == (320, 240)


@pytest.fixture(scope="session")
def looped_video(sample_video) -> Path:
    """Stream-copied loop: timestamps jump at each join, so avg_frame_rate drifts
    away from the nominal 24000/1001 like a variable-frame-rate web rip."""
    out = sample_video.parent / "looped.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-stream_loop", "2", "-i", str(sample_video), "-c", "copy", str(out)],
                   check=True, timeout=120)
    return out


@needs_ffmpeg
def test_probe_prefers_nominal_fps_and_flags_variable_frame_rate(looped_video):
    info = asi.probe_video(looped_video)
    assert info["fps"] == pytest.approx(FPS, abs=1e-6)
    assert info["variable_frame_rate"] is True
    # Duration comes from the stream, not nb_frames / fps, when timing is irregular.
    raw = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=duration", "-of", "json", str(looped_video)],
        capture_output=True, text=True, check=True).stdout)
    assert info["duration_seconds"] == pytest.approx(float(raw["streams"][0]["duration"]), abs=1e-3)


@needs_ffmpeg
def test_constant_frame_rate_video_is_not_flagged(sample_video):
    assert asi.probe_video(sample_video)["variable_frame_rate"] is False


@needs_ffmpeg
def test_build_warns_on_variable_frame_rate(looped_video, transcript_file, tmp_path):
    result = asi.build_source_index(looped_video, tmp_path / "vfr", transcript_path=transcript_file, provenance_kind="mock")
    assert "variable_frame_rate" in {w.code for w in result.warnings}


@needs_ffmpeg
def test_detect_scenes_finds_every_cut_within_one_frame(sample_video, tmp_path):
    scenes, warnings = asi.detect_scenes(sample_video, tmp_path, fps=FPS, duration=311 / FPS)
    assert warnings == []
    assert [s["scene_id"] for s in scenes] == ["sc001", "sc002", "sc003", "sc004"]
    expected_cuts = [3.0, 5.5, 9.5]
    for scene, cut in zip(scenes[1:], expected_cuts):
        assert abs(scene["start_seconds"] - cut) <= 1 / FPS + 1e-6
    assert scenes[-1]["end_seconds"] == pytest.approx(311 / FPS, abs=1e-3)
    for a, b in itertools.pairwise(scenes):
        assert a["end_seconds"] == b["start_seconds"]


@needs_ffmpeg
def test_single_scene_for_long_video_is_flagged(sample_video, tmp_path, monkeypatch):
    class OneScene:
        def execute(self, inputs):
            from tools.base_tool import ToolResult
            return ToolResult(success=True, data={"scenes": [{"start_seconds": 0.0, "end_seconds": 480.0}], "method": "ffmpeg"})

    scenes, warnings = asi.detect_scenes(sample_video, tmp_path, fps=FPS, duration=480.0, detector=OneScene())
    assert len(scenes) == 1
    assert "scene_detect_degenerate" in {w.code for w in warnings}


@needs_ffmpeg
def test_build_index_end_to_end(sample_video, transcript_file, tmp_path):
    result = asi.build_source_index(
        sample_video, tmp_path / "out", transcript_path=transcript_file,
        source_index_id="sample-clip", provenance_kind="mock",
    )
    index = json.loads(result.index_path.read_text(encoding="utf-8"))
    assert index == result.index
    assert index["source_index_id"] == "sample-clip"
    assert index["provenance"]["kind"] == "mock"
    assert index["provenance"]["transcript_origin"] == "external_file"
    assert index["provenance"]["visual_review"] == "pending"
    assert {"scene_detect", "frame_sampler"} <= set(index["provenance"]["tools"])
    assert len(index["scenes"]) == 4
    for scene in index["scenes"]:
        assert scene["visual_summary"] == "" and scene["visual_review"] == "pending"
        assert len(scene["keyframes"]) >= 3
        for kf in scene["keyframes"]:
            assert Path(kf["path"]).is_file()
            assert scene["start_seconds"] <= kf["timestamp_seconds"] < scene["end_seconds"]
    assert [l["text_ja"] for l in index["scenes"][1]["dialogue"]] == ["本当に？"]


@needs_ffmpeg
def test_built_index_matches_schema(sample_video, transcript_file, tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    result = asi.build_source_index(sample_video, tmp_path / "out", transcript_path=transcript_file, provenance_kind="mock")
    jsonschema.validate(result.index, json.loads(SCHEMA.read_text(encoding="utf-8")))


@needs_ffmpeg
def test_built_index_is_usable_by_phase1_validator(sample_video, transcript_file, tmp_path):
    index = asi.build_source_index(
        sample_video, tmp_path / "out", transcript_path=transcript_file, provenance_kind="mock"
    ).index
    scenes = index["scenes"]
    roles = ["hook", "development", "conflict", "climax", "resolution"]
    picks = [scenes[2], scenes[0], scenes[1], scenes[2], scenes[3]]
    segments, t = [], 0.0
    for n, (role, scene) in enumerate(zip(roles, picks), start=1):
        start, end = scene["start_seconds"], scene["start_seconds"] + 2.0
        segments.append({
            "segment_id": f"seg{n:02d}", "story_role": role, "scene_ids": [scene["scene_id"]],
            "source_start_seconds": start, "source_end_seconds": end,
            "cut": {"in_frame": round(start * index["video"]["fps"]), "out_frame": round(end * index["video"]["fps"])},
            "timeline_start_seconds": round(t, 6), "timeline_end_seconds": round(t + 2.0, 6),
            "narration": {"text_ja": "画面が変わる。", "duration_seconds": 1.2, "duration_source": "estimate"},
            "claims": [{"claim_id": f"c{n}", "text_ja": "画面が変わる。", "type": "event",
                        "evidence": [{"scene_id": scene["scene_id"], "line_id": None,
                                      "start_seconds": start, "end_seconds": end}]}],
        })
        t += 2.0
    plan = {"version": "1.0", "source_index_id": index["source_index_id"], "language": "ja",
            "fps": index["video"]["fps"], "target_duration_seconds": {"min": 5, "max": 15}, "segments": segments}
    report = validate_recap_plan(plan, index)
    assert report.errors == []
    assert report.source_is_mock is True


@needs_ffmpeg
def test_review_packet_lists_every_scene_keyframe_and_line(sample_video, transcript_file, tmp_path):
    result = asi.build_source_index(sample_video, tmp_path / "out", transcript_path=transcript_file, provenance_kind="mock")
    packet = result.review_packet_path.read_text(encoding="utf-8")
    for scene in result.index["scenes"]:
        assert scene["scene_id"] in packet
        for kf in scene["keyframes"]:
            assert Path(kf["path"]).name in packet
        for line in scene["dialogue"]:
            assert line["line_id"] in packet
    assert "known_hallucination" in packet


@needs_ffmpeg
def test_build_without_asr_or_transcript_fails_clearly(sample_video, tmp_path, monkeypatch):
    monkeypatch.setattr(asi, "_faster_whisper_available", lambda: False)
    with pytest.raises(asi.IndexBuildError, match="faster-whisper"):
        asi.build_source_index(sample_video, tmp_path / "out")


def test_build_rejects_missing_video(tmp_path):
    with pytest.raises(asi.IndexBuildError, match="not found"):
        asi.build_source_index(tmp_path / "nope.mp4", tmp_path / "out")


@needs_ffmpeg
def test_cli_build_then_annotate(sample_video, transcript_file, tmp_path, capsys):
    out = tmp_path / "cli"
    assert asi.main(["build", "--video", str(sample_video), "--out", str(out),
                     "--transcript", str(transcript_file), "--mock"]) == 0
    index_path = out / "anime_source_index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    ann = {"source_index_id": index["source_index_id"], "characters": [],
           "scenes": [{"scene_id": "sc001", "visual_summary": "赤い画面", "characters": [], "actions": [], "reactions": []}],
           "lines": []}
    ann_path = tmp_path / "ann.json"
    ann_path.write_text(json.dumps(ann, ensure_ascii=False), encoding="utf-8")
    assert asi.main(["annotate", "--index", str(index_path), "--annotations", str(ann_path)]) == 0
    updated = json.loads(index_path.read_text(encoding="utf-8"))
    assert updated["scenes"][0]["visual_summary"] == "赤い画面"
    ann["scenes"][0]["scene_id"] = "sc999"
    ann_path.write_text(json.dumps(ann, ensure_ascii=False), encoding="utf-8")
    assert asi.main(["annotate", "--index", str(index_path), "--annotations", str(ann_path)]) == 1


# ── opt-in: real faster-whisper on a real Japanese clip ─────────────────


@pytest.mark.skipif(
    not (os.environ.get("OPENMONTAGE_JA_SAMPLE_VIDEO") and asi._faster_whisper_available()),
    reason="set OPENMONTAGE_JA_SAMPLE_VIDEO to a short Japanese clip and install faster-whisper (model download needs huggingface.co)",
)
def test_real_faster_whisper_on_japanese_clip(tmp_path):
    video = Path(os.environ["OPENMONTAGE_JA_SAMPLE_VIDEO"])
    result = asi.build_source_index(video, tmp_path / "real", model_size=os.environ.get("OPENMONTAGE_WHISPER_MODEL", "small"))
    assert result.index["provenance"]["kind"] == "analyzed"
    assert result.index["provenance"]["transcript_origin"] == "faster_whisper"
    assert any(scene["dialogue"] for scene in result.index["scenes"])


# ── Phase 2b: behaviours adapted from the legacy tool (re-implemented) ──


def test_high_no_speech_probability_is_flagged():
    seg = [{"id": 0, "start": 0.5, "end": 2.0, "text": "うーん", "no_speech_prob": 0.8,
            "words": [_word("うーん", 0.5, 2.0, 0.9)]}]
    line = asi.assign_dialogue(seg, _scenes())["sc001"][0]
    assert "likely_no_speech" in line["asr_flags"]
    assert line["low_confidence"] is True


def test_prompt_echo_is_treated_as_hallucination():
    seg = [{"id": 0, "start": 0.5, "end": 2.0, "text": asi.JA_ANIME_PROMPT,
            "words": [_word(asi.JA_ANIME_PROMPT, 0.5, 2.0, 0.9)]}]
    line = asi.assign_dialogue(seg, _scenes())["sc001"][0]
    assert "known_hallucination" in line["asr_flags"]


@pytest.fixture(scope="session")
def isoluminant_video(tmp_path_factory) -> Path:
    """green (0,128,0) → red (255,0,0) → blue: the first cut has almost equal luma,
    which ffmpeg's luma-only `select=scene` score misses."""
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg not installed")
    out = tmp_path_factory.mktemp("isolum") / "isolum.mp4"
    cmd = ["ffmpeg", "-v", "error", "-y"]
    for colour in ("green", "red", "blue"):
        cmd += ["-f", "lavfi", "-i", f"color=c={colour}:s=320x240:r=24000/1001:d=2"]
    cmd += ["-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)]
    subprocess.run(cmd, check=True, timeout=120)
    return out


@needs_ffmpeg
def test_cut_between_equal_luma_colours_is_detected(isoluminant_video, tmp_path):
    info = asi.probe_video(isoluminant_video)
    scenes, _ = asi.detect_scenes(isoluminant_video, tmp_path, fps=info["fps"], duration=info["duration_seconds"])
    starts = [s["start_seconds"] for s in scenes]
    assert len(scenes) == 3, starts
    assert abs(starts[1] - 2.0) <= 1 / FPS + 1e-6
    assert abs(starts[2] - 4.0) <= 1 / FPS + 1e-6


def test_transcribe_japanese_uses_anime_prompt_and_beam(monkeypatch, tmp_path):
    seen = {}

    class FakeTranscriber:
        def execute(self, inputs):
            from tools.base_tool import ToolResult
            seen.update(inputs)
            return ToolResult(success=True, data={"language": "ja", "segments": []})

    monkeypatch.setattr("tools.analysis.transcriber.Transcriber", FakeTranscriber)
    asi.transcribe_japanese(tmp_path / "v.mp4", tmp_path, "large-v3")
    assert seen["language"] == "ja"
    assert seen["initial_prompt"] == asi.JA_ANIME_PROMPT
    assert seen["beam_size"] == 5
    assert seen["model_size"] == "large-v3"


@needs_ffmpeg
def test_small_whisper_model_raises_accuracy_warning(sample_video, tmp_path, monkeypatch):
    class FakeTranscriber:
        def execute(self, inputs):
            from tools.base_tool import ToolResult
            return ToolResult(success=True, data=_transcript())

    monkeypatch.setattr("tools.analysis.transcriber.Transcriber", FakeTranscriber)
    monkeypatch.setattr(asi, "_faster_whisper_available", lambda: True)
    result = asi.build_source_index(sample_video, tmp_path / "small", model_size="small", provenance_kind="mock")
    assert "asr_model_small" in {w.code for w in result.warnings}
    assert result.index["provenance"]["transcript_origin"] == "faster_whisper"
    big = asi.build_source_index(sample_video, tmp_path / "big", model_size="large-v3", provenance_kind="mock")
    assert "asr_model_small" not in {w.code for w in big.warnings}


@pytest.fixture(scope="session")
def loudness_video(tmp_path_factory) -> Path:
    """4 s quiet tone, then 4 s loud tone."""
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg not installed")
    out = tmp_path_factory.mktemp("loud") / "loud.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=160x120:r=24000/1001:d=8",
        "-f", "lavfi", "-i", "sine=f=440:d=8",
        "-af", "volume='if(lt(t,4),0.02,0.8)':eval=frame", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(out)], check=True, timeout=120)
    return out


@needs_ffmpeg
def test_line_loudness_and_excite_from_real_audio(loudness_video):
    # Most lines are normal level (as in an episode), so the median sits on the quiet side.
    lines = {"sc001": [
        {"line_id": "sc001_l01", "start_seconds": 1.0, "end_seconds": 3.0, "text_ja": "そうですね"},
        {"line_id": "sc001_l02", "start_seconds": 5.0, "end_seconds": 7.0, "text_ja": "そうですね"},
        {"line_id": "sc001_l03", "start_seconds": 5.0, "end_seconds": 7.0, "text_ja": "なにー！？"},
        {"line_id": "sc001_l04", "start_seconds": 0.5, "end_seconds": 2.0, "text_ja": "はい"},
        {"line_id": "sc001_l05", "start_seconds": 2.0, "end_seconds": 3.5, "text_ja": "うん"},
        {"line_id": "sc001_l06", "start_seconds": 1.0, "end_seconds": 3.0, "text_ja": "そうですね！"},
    ]}
    asi.measure_line_loudness(loudness_video, lines)
    quiet, loud, shout = lines["sc001"][:3]
    quiet_exclaim = lines["sc001"][5]
    assert loud["loudness_db"] > quiet["loudness_db"] + 20
    assert loud["excite"] > quiet["excite"]
    assert quiet_exclaim["excite"] > quiet["excite"]  # exclamation adds to the score
    assert all(0.0 <= line["excite"] <= 3.0 for line in lines["sc001"])
    assert shout["excite"] == 3.0  # capped


@needs_ffmpeg
def test_build_index_records_loudness_for_every_line(sample_video, transcript_file, tmp_path):
    result = asi.build_source_index(sample_video, tmp_path / "ld", transcript_path=transcript_file, provenance_kind="mock")
    lines = [line for s in result.index["scenes"] for line in s["dialogue"]]
    assert lines and all("loudness_db" in line and "excite" in line for line in lines)


@needs_ffmpeg
def test_provenance_records_rgb_scdet_when_ffmpeg_fallback_is_used(sample_video, transcript_file, tmp_path):
    import importlib.util
    if importlib.util.find_spec("scenedetect") is not None:
        pytest.skip("PySceneDetect installed — the ffmpeg fallback is not used")
    result = asi.build_source_index(sample_video, tmp_path / "prov", transcript_path=transcript_file, provenance_kind="mock")
    assert "ffmpeg scdet(rgb)" in result.index["provenance"]["tools"]


# ── Windows trial hooks ─────────────────────────────────────────────────


@needs_ffmpeg
def test_build_records_stage_timings(sample_video, transcript_file, tmp_path):
    timings: dict = {}
    asi.build_source_index(sample_video, tmp_path / "t", transcript_path=transcript_file,
                           provenance_kind="mock", timings=timings)
    for stage in ("probe", "scene_detect", "keyframes", "transcript", "dialogue", "loudness", "write"):
        assert stage in timings and timings[stage] >= 0.0, stage


@needs_ffmpeg
def test_build_passes_asr_device_to_transcriber(sample_video, tmp_path, monkeypatch):
    seen = {}

    class FakeTranscriber:
        def execute(self, inputs):
            from tools.base_tool import ToolResult
            seen.update(inputs)
            return ToolResult(success=True, data=_transcript())

    monkeypatch.setattr("tools.analysis.transcriber.Transcriber", FakeTranscriber)
    monkeypatch.setattr(asi, "_faster_whisper_available", lambda: True)
    asi.build_source_index(sample_video, tmp_path / "dev", model_size="large-v3", provenance_kind="mock", asr_device="cpu")
    assert seen["device"] == "cpu"


# ── Regression: episode #253-1 reported lines_inside_scenes FAIL ───────
# Real scene cuts sit on the frame grid with 6-decimal timestamps
# (e.g. 12.554208); the synthetic fixtures above used round values (3.003),
# which hid a rounding bug at scene boundaries.

REAL_SCENES = [
    {"scene_id": "sc001", "start_seconds": 0.0, "end_seconds": 12.554208},
    {"scene_id": "sc002", "start_seconds": 12.554208, "end_seconds": 30.030542},
    {"scene_id": "sc003", "start_seconds": 30.030542, "end_seconds": 40.0},
]


def _inside(line: dict, scene: dict) -> bool:
    return scene["start_seconds"] <= line["start_seconds"] < line["end_seconds"] <= scene["end_seconds"]


def test_start_clamped_to_cut_is_not_rounded_below_scene_start():
    # word starts 54 ms before the cut, its midpoint is after it → belongs to sc002
    seg = [{"start": 12.50, "end": 13.4, "words": [_word("あの", 12.50, 12.70), _word("さ", 12.70, 13.4)]}]
    line = asi.assign_dialogue(seg, REAL_SCENES)["sc002"][0]
    assert _inside(line, REAL_SCENES[1]), line


def test_end_clamped_to_cut_is_not_rounded_above_scene_end():
    # word midpoint 30.0 is before the 30.030542 cut, but it ends after it
    seg = [{"start": 29.9, "end": 30.1, "words": [_word("えっ", 29.9, 30.1)]}]
    line = asi.assign_dialogue(seg, REAL_SCENES)["sc002"][0]
    assert _inside(line, REAL_SCENES[1]), line


def test_boundary_adjustment_is_flagged_and_original_asr_times_kept():
    seg = [{"start": 12.50, "end": 13.4, "words": [_word("あの", 12.50, 12.70), _word("さ", 12.70, 13.4)]}]
    line = asi.assign_dialogue(seg, REAL_SCENES)["sc002"][0]
    assert "clamped_to_scene" in line["asr_flags"]
    assert line["asr_start_seconds"] == 12.5 and line["asr_end_seconds"] == 13.4
    untouched = asi.assign_dialogue([{"start": 14.0, "end": 15.0, "words": [_word("はい", 14.0, 15.0)]}], REAL_SCENES)["sc002"][0]
    assert "clamped_to_scene" not in untouched["asr_flags"] and "asr_start_seconds" not in untouched


def test_overlapping_asr_segments_keep_every_line_in_order_and_flag_overlap():
    """Crosstalk / overlapping Whisper segments, given out of order."""
    segs = [
        {"start": 15.5, "end": 17.0, "words": [_word("やだ", 15.5, 16.0), _word("もん", 16.0, 17.0)]},
        {"start": 14.0, "end": 16.2, "words": [_word("しんちゃん", 14.0, 15.0), _word("待って", 15.0, 16.2)]},
    ]
    lines = asi.assign_dialogue(segs, REAL_SCENES)["sc002"]
    assert [l["text_ja"] for l in lines] == ["しんちゃん待って", "やだもん"]
    assert [l["line_id"] for l in lines] == ["sc002_l01", "sc002_l02"]
    assert lines[0]["start_seconds"] <= lines[1]["start_seconds"]
    assert "overlaps_previous" in lines[1]["asr_flags"]
    assert "overlaps_previous" not in lines[0]["asr_flags"]
    assert all(_inside(l, REAL_SCENES[1]) for l in lines)


def test_zero_length_and_inverted_words_still_produce_valid_lines():
    segs = [
        {"start": 12.554208, "end": 12.554208, "words": [_word("ん", 12.554208, 12.554208)]},
        {"start": 20.0, "end": 19.8, "words": [_word("あ", 20.0, 19.8)]},
        {"start": 40.0, "end": 40.0, "words": [_word("お", 40.0, 40.0)]},  # exactly at the video end
    ]
    by_scene = asi.assign_dialogue(segs, REAL_SCENES)
    lines = [(sid, l) for sid, ls in by_scene.items() for l in ls]
    assert sorted(l["text_ja"] for _, l in lines) == ["あ", "お", "ん"]
    scene = {s["scene_id"]: s for s in REAL_SCENES}
    assert all(_inside(l, scene[sid]) for sid, l in lines), lines


def test_fuzz_every_line_inside_its_scene_in_order_and_no_text_lost():
    import random

    rng = random.Random(2531)
    fps = 24000 / 1001
    for _ in range(200):
        cuts = sorted({round(round(rng.uniform(0.5, 59.5) * fps) / fps, 6) for _ in range(rng.randint(1, 8))})
        bounds = [0.0, *cuts, round(round(60.0 * fps) / fps, 6)]
        scenes = [{"scene_id": f"sc{i + 1:03d}", "start_seconds": a, "end_seconds": b}
                  for i, (a, b) in enumerate(itertools.pairwise(bounds))]
        segments, chars = [], []
        for _ in range(rng.randint(1, 12)):
            t = rng.uniform(0, 59.0)
            words = []
            for k in range(rng.randint(1, 5)):
                w0 = t + rng.uniform(-0.3, 0.6)  # overlapping / non-monotonic, like real ASR
                w1 = w0 + rng.choice([0.0, rng.uniform(0.01, 1.2)])
                text = rng.choice("あいうえおかきくけこ") * (k + 1)
                words.append(_word(text, round(w0, 3), round(w1, 3)))
                chars.append(text)
                t = w1
            segments.append({"start": words[0]["start"], "end": words[-1]["end"], "words": words})
        rng.shuffle(segments)
        by_scene = asi.assign_dialogue(segments, scenes)
        got = []
        for scene in scenes:
            prev = -1.0
            for line in by_scene[scene["scene_id"]]:
                assert _inside(line, scene), (scene, line)
                assert line["start_seconds"] >= prev
                prev = line["start_seconds"]
                got.append(line["text_ja"])
        assert sorted("".join(got)) == sorted("".join(chars))  # every character kept, none invented


def test_reassign_dialogue_rebuilds_lines_from_saved_transcript():
    index = {
        "source_index_id": "ep", "provenance": {"kind": "analyzed", "tools": ["scene_detect"]},
        "video": {"path": "/nonexistent.mp4", "fps": 24000 / 1001, "duration_seconds": 40.0, "has_audio": True},
        "scenes": [{**s, "keyframes": [{"timestamp_seconds": s["start_seconds"] + 0.1, "path": "k.jpg"}],
                    "visual_summary": "", "dialogue": [{"line_id": "old", "start_seconds": 12.554,
                                                        "end_seconds": 13.4, "text_ja": "あのさ"}]}
                   for s in REAL_SCENES],
    }
    transcript = {"language": "ja", "segments": [
        {"start": 12.50, "end": 13.4, "words": [_word("あの", 12.50, 12.70), _word("さ", 12.70, 13.4)]}]}
    new_index, warnings = asi.reassign_dialogue(index, transcript)
    assert [s["keyframes"] for s in new_index["scenes"]] == [s["keyframes"] for s in index["scenes"]]
    sc2 = new_index["scenes"][1]
    assert [l["text_ja"] for l in sc2["dialogue"]] == ["あのさ"] and _inside(sc2["dialogue"][0], sc2)
    assert new_index["scenes"][0]["dialogue"] == []
    assert "reassign_dialogue" in new_index["provenance"]["tools"]
    assert "video_missing_for_loudness" in {w.code for w in warnings}
    assert index["scenes"][1]["dialogue"][0]["line_id"] == "old"  # input untouched


def test_reassign_refuses_to_drop_existing_line_annotations():
    index = {
        "source_index_id": "ep", "provenance": {"kind": "analyzed", "tools": []},
        "video": {"path": "/nonexistent.mp4", "fps": 24000 / 1001, "duration_seconds": 40.0, "has_audio": False},
        "scenes": [{**REAL_SCENES[0], "keyframes": [], "dialogue": [
            {"line_id": "sc001_l01", "start_seconds": 1.0, "end_seconds": 2.0, "text_ja": "はい", "speaker": "misae"}]}],
    }
    with pytest.raises(asi.IndexBuildError, match="annotat"):
        asi.reassign_dialogue(index, {"segments": []})
    new_index, _ = asi.reassign_dialogue(index, {"segments": []}, force=True)
    assert new_index["scenes"][0]["dialogue"] == []
