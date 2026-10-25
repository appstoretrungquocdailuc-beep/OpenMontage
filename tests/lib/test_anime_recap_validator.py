"""anime_recap_plan validator: timestamps, evidence and no-fabrication rules.

The fixtures under tests/fixtures/anime_recap/ are a fictional mock episode
(provenance.kind == "mock") — they exercise the contract, they are not the
result of analysing a real video.
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import pytest

from lib.anime_recap import (
    estimate_ja_speech_seconds,
    main,
    seconds_to_frame,
    validate_recap_plan,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "anime_recap"
INDEX_PATH = FIXTURES / "mock_source_index.json"
PLAN_PATH = FIXTURES / "mock_recap_plan.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def index() -> dict:
    return _load(INDEX_PATH)


@pytest.fixture
def plan() -> dict:
    return _load(PLAN_PATH)


def _seg(plan: dict, segment_id: str) -> dict:
    return next(s for s in plan["segments"] if s["segment_id"] == segment_id)


def _codes(plan: dict, index: dict) -> set[str]:
    return validate_recap_plan(plan, index).codes()


# ── baseline ────────────────────────────────────────────────────────────


def test_valid_mock_plan_passes(plan, index):
    report = validate_recap_plan(plan, index)
    assert report.errors == []
    assert report.ok


def test_report_flags_mock_source(plan, index):
    assert validate_recap_plan(plan, index).source_is_mock is True


def test_fixture_declares_mock_provenance(index):
    assert index["provenance"]["kind"] == "mock"


# ── structure ───────────────────────────────────────────────────────────


def test_missing_top_level_field(plan, index):
    del plan["segments"]
    assert "missing_field" in _codes(plan, index)


def test_missing_segment_field(plan, index):
    del plan["segments"][0]["narration"]
    assert "missing_field" in _codes(plan, index)


def test_source_index_id_mismatch(plan, index):
    plan["source_index_id"] = "another-episode"
    assert "index_mismatch" in _codes(plan, index)


def test_duplicate_segment_id(plan, index):
    plan["segments"][1]["segment_id"] = plan["segments"][0]["segment_id"]
    assert "duplicate_id" in _codes(plan, index)


# ── timestamps & frames ─────────────────────────────────────────────────


def test_fps_mismatch(plan, index):
    plan["fps"] = 30.0
    assert "fps_mismatch" in _codes(plan, index)


def test_reversed_source_range(plan, index):
    seg = plan["segments"][0]
    seg["source_start_seconds"], seg["source_end_seconds"] = (
        seg["source_end_seconds"],
        seg["source_start_seconds"],
    )
    assert "invalid_timestamp" in _codes(plan, index)


def test_negative_timestamp(plan, index):
    plan["segments"][0]["source_start_seconds"] = -1.0
    assert "invalid_timestamp" in _codes(plan, index)


def test_nan_timestamp_is_error_not_crash(plan, index):
    plan["segments"][0]["source_end_seconds"] = math.nan
    assert "invalid_timestamp" in _codes(plan, index)


def test_string_timestamp_is_error_not_crash(plan, index):
    plan["segments"][0]["source_end_seconds"] = "306"
    assert "invalid_timestamp" in _codes(plan, index)


def test_timestamp_beyond_video_duration(plan, index):
    seg = _seg(plan, "seg07")
    seg["source_end_seconds"] = 500.0
    assert "out_of_video" in _codes(plan, index)


def test_source_range_outside_its_scenes(plan, index):
    seg = _seg(plan, "seg01")  # scene sc008 = 280–330
    seg["source_start_seconds"] = 270.0
    seg["cut"]["in_frame"] = seconds_to_frame(270.0, plan["fps"])
    assert "outside_scene" in _codes(plan, index)


def test_range_spanning_two_listed_adjacent_scenes_is_allowed(plan, index):
    seg = _seg(plan, "seg07")
    assert seg["scene_ids"] == ["sc011", "sc012"]
    assert "outside_scene" not in _codes(plan, index)


def test_unknown_scene_in_segment(plan, index):
    plan["segments"][0]["scene_ids"] = ["sc999"]
    assert "unknown_scene" in _codes(plan, index)


def test_frame_mismatch(plan, index):
    plan["segments"][0]["cut"]["in_frame"] += 3
    assert "frame_mismatch" in _codes(plan, index)


def test_seconds_to_frame_rounds_fractional_fps():
    assert seconds_to_frame(300.0, 23.976) == 7193
    assert seconds_to_frame(0.0, 23.976) == 0


def test_timeline_gap(plan, index):
    seg = plan["segments"][1]
    seg["timeline_start_seconds"] += 1.0
    seg["timeline_end_seconds"] += 1.0
    assert "timeline_gap" in _codes(plan, index)


def test_timeline_length_must_match_cut_length(plan, index):
    plan["segments"][0]["timeline_end_seconds"] += 2.0
    assert "timeline_gap" in _codes(plan, index)


def test_total_duration_too_short(plan, index):
    plan["segments"] = plan["segments"][:3]
    assert "total_duration_out_of_range" in validate_recap_plan(plan, index).codes()


# ── evidence & no-fabrication ───────────────────────────────────────────


def test_evidence_unknown_scene(plan, index):
    plan["segments"][0]["claims"][0]["evidence"][0]["scene_id"] = "sc404"
    assert "unknown_scene" in _codes(plan, index)


def test_evidence_unknown_line(plan, index):
    plan["segments"][0]["claims"][0]["evidence"][0]["line_id"] = "sc008_l99"
    assert "unknown_line" in _codes(plan, index)


def test_evidence_line_from_another_scene(plan, index):
    plan["segments"][0]["claims"][0]["evidence"][0]["line_id"] = "sc002_l01"
    assert "unknown_line" in _codes(plan, index)


def test_evidence_time_outside_its_scene(plan, index):
    plan["segments"][0]["claims"][0]["evidence"][0]["start_seconds"] = 10.0
    assert "evidence_outside_scene" in _codes(plan, index)


def test_event_claim_without_evidence(plan, index):
    plan["segments"][0]["claims"][0]["evidence"] = []
    assert "claim_without_evidence" in _codes(plan, index)


def test_quote_claim_without_line_reference(plan, index):
    claim = _seg(plan, "seg02")["claims"][1]
    assert claim["type"] == "quote"
    claim["evidence"][0]["line_id"] = None
    assert "claim_without_evidence" in _codes(plan, index)


def test_paraphrased_quote_is_rejected(plan, index):
    seg = _seg(plan, "seg02")
    claim = seg["claims"][1]
    claim["text_ja"] = "「絶対に食べないでね！」"
    seg["narration"]["text_ja"] = "".join(c["text_ja"] for c in seg["claims"])
    assert "quote_not_in_source" in _codes(plan, index)


def test_commentary_without_evidence_is_allowed(plan, index):
    claim = _seg(plan, "seg05")["claims"][1]
    assert claim["type"] == "commentary" and claim["evidence"] == []
    assert "claim_without_evidence" not in _codes(plan, index)


def test_unknown_claim_type_is_rejected(plan, index):
    plan["segments"][0]["claims"][0]["type"] = "opinion"
    assert "invalid_field" in _codes(plan, index)


def test_unknown_story_role_is_rejected(plan, index):
    plan["segments"][1]["story_role"] = "filler"
    assert "invalid_field" in _codes(plan, index)


def test_dialogue_used_outside_cut_is_warning(plan, index):
    plan["segments"][0]["dialogue_used"][0]["line_id"] = "sc009_l01"
    plan["segments"][0]["dialogue_used"][0]["text_ja"] = "しんのすけ、正直に言いなさい。"
    report = validate_recap_plan(plan, index)
    assert "dialogue_outside_cut" in {w.code for w in report.warnings}


def test_narration_with_unsourced_extra_sentence(plan, index):
    seg = plan["segments"][0]
    seg["narration"]["text_ja"] += "実はひろしが犯人だった。"
    assert "narration_claims_mismatch" in _codes(plan, index)


def test_dialogue_used_text_must_match_source(plan, index):
    plan["segments"][0]["dialogue_used"][0]["text_ja"] = "私のケーキがない！"
    assert "dialogue_text_mismatch" in _codes(plan, index)


def test_dialogue_used_unknown_line(plan, index):
    plan["segments"][0]["dialogue_used"][0]["line_id"] = "sc008_l42"
    assert "unknown_line" in _codes(plan, index)


# ── narration timing ────────────────────────────────────────────────────


def test_narration_longer_than_cut(plan, index):
    plan["segments"][0]["narration"]["duration_seconds"] = 9.0  # cut is 6 s
    assert "narration_exceeds_cut" in _codes(plan, index)


def test_speech_rate_too_fast(plan, index):
    plan["segments"][0]["narration"]["duration_seconds"] = 1.0
    assert "speech_rate_too_fast" in _codes(plan, index)


def test_estimate_ja_speech_seconds_counts_characters_not_words():
    # 13 spoken characters at 6.5 c/s = 2.0 s, plus one 0.15 s pause for "。".
    assert estimate_ja_speech_seconds("みさえはケーキを買ってきた。") == pytest.approx(2.15, abs=0.01)
    assert estimate_ja_speech_seconds("") == 0.0


# ── story structure ─────────────────────────────────────────────────────


def test_missing_story_role(plan, index):
    for seg in plan["segments"]:
        if seg["story_role"] == "climax":
            seg["story_role"] = "conflict"
    assert "missing_story_role" in _codes(plan, index)


def test_first_segment_must_be_hook(plan, index):
    plan["segments"][0]["story_role"] = "development"
    _seg(plan, "seg02")["story_role"] = "hook"
    assert "first_segment_not_hook" in _codes(plan, index)


def test_climax_before_conflict_is_story_order_error(plan, index):
    _seg(plan, "seg03")["story_role"] = "climax"
    _seg(plan, "seg06")["story_role"] = "development"
    assert "story_order" in _codes(plan, index)


# ── CLI ─────────────────────────────────────────────────────────────────


def test_cli_returns_zero_for_valid_plan(capsys):
    assert main(["--plan", str(PLAN_PATH), "--index", str(INDEX_PATH)]) == 0
    assert "MOCK" in capsys.readouterr().out


def test_cli_returns_one_and_json_for_invalid_plan(tmp_path, plan, capsys):
    plan["fps"] = 30.0
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    assert main(["--plan", str(bad), "--index", str(INDEX_PATH), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "fps_mismatch" in {e["code"] for e in payload["errors"]}


def test_cli_reports_malformed_json(tmp_path, capsys):
    bad = tmp_path / "broken.json"
    bad.write_text("{not json", encoding="utf-8")
    assert main(["--plan", str(bad), "--index", str(INDEX_PATH), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert "invalid_json" in {e["code"] for e in payload["errors"]}


def test_validator_does_not_mutate_inputs(plan, index):
    before = (copy.deepcopy(plan), copy.deepcopy(index))
    validate_recap_plan(plan, index)
    assert (plan, index) == before
