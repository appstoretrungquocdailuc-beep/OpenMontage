"""The anime-recap-ja Layer 2 skill must point at real, matching artifacts.

The skill tells the agent to emit anime_source_index / anime_recap_plan JSON and
to run the validator. If a schema path, the CLI command or a story role named
in the skill drifts from the code, the agent follows dead instructions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.anime_recap import STORY_ROLES

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL = REPO_ROOT / "skills" / "creative" / "anime-recap-ja.md"
INDEX_SCHEMA = REPO_ROOT / "schemas" / "artifacts" / "anime_source_index.schema.json"
PLAN_SCHEMA = REPO_ROOT / "schemas" / "artifacts" / "anime_recap_plan.schema.json"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "anime_recap"

SCREENWRITING_SKILLS = (
    "sw-sitcom-comedy",
    "sw-japanese-screenwriting",
    "sw-scene-craft",
    "sw-character-conflict",
    "sw-story-structure",
)


def _skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_skill_file_exists():
    assert SKILL.is_file()


def test_skill_names_validator_command_and_schemas():
    text = _skill_text()
    assert "python -m lib.anime_recap" in text
    assert "schemas/artifacts/anime_source_index.schema.json" in text
    assert "schemas/artifacts/anime_recap_plan.schema.json" in text


@pytest.mark.parametrize("role", STORY_ROLES)
def test_skill_documents_every_story_role(role):
    assert f"`{role}`" in _skill_text()


@pytest.mark.parametrize("skill", SCREENWRITING_SKILLS)
def test_skill_maps_screenwriting_skill(skill):
    assert skill in _skill_text()


def test_skill_states_no_fabrication_and_no_fake_analysis_rules():
    text = _skill_text()
    assert "claim_without_evidence" in text
    assert 'provenance.kind' in text


def test_skill_is_listed_in_index():
    index = (REPO_ROOT / "skills" / "INDEX.md").read_text(encoding="utf-8")
    assert "creative/anime-recap-ja.md" in index


@pytest.mark.parametrize("schema_path", [INDEX_SCHEMA, PLAN_SCHEMA])
def test_schema_is_valid_json_with_title(schema_path):
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["$schema"].startswith("http://json-schema.org/")
    assert schema["title"]


def test_plan_schema_story_roles_match_validator():
    schema = json.loads(PLAN_SCHEMA.read_text(encoding="utf-8"))
    roles = schema["properties"]["segments"]["items"]["properties"]["story_role"]["enum"]
    assert tuple(roles) == STORY_ROLES


@pytest.mark.parametrize(
    ("schema_path", "fixture"),
    [(INDEX_SCHEMA, "mock_source_index.json"), (PLAN_SCHEMA, "mock_recap_plan.json")],
)
def test_fixtures_conform_to_schemas(schema_path, fixture):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    data = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))
    jsonschema.validate(data, schema)


# ── Phase 2: video understanding ────────────────────────────────────────


def test_skill_points_at_source_indexer_build_and_annotate():
    text = _skill_text()
    assert "python -m lib.anime_source_indexer build" in text
    assert "python -m lib.anime_source_indexer annotate" in text
    assert "review_packet.md" in text


@pytest.mark.parametrize(
    "dotted",
    [
        "provenance.transcript_origin",
        "provenance.visual_review",
        "video.has_audio",
        "scenes.visual_review",
        "scenes.reactions",
        "scenes.dialogue.low_confidence",
        "scenes.dialogue.asr_flags",
        "scenes.dialogue.loudness_db",
        "scenes.dialogue.excite",
    ],
)
def test_index_schema_documents_phase2_fields(dotted):
    node = json.loads(INDEX_SCHEMA.read_text(encoding="utf-8"))
    for part in dotted.split("."):
        node = node["properties"][part]
        if node.get("type") == "array":
            node = node["items"]
    assert node.get("description") or node.get("enum") or node.get("type")


def test_index_schema_allows_every_flag_the_indexer_emits():
    schema = json.loads(INDEX_SCHEMA.read_text(encoding="utf-8"))
    dialogue = schema["properties"]["scenes"]["items"]["properties"]["dialogue"]["items"]["properties"]
    allowed = set(dialogue["asr_flags"]["items"]["enum"])
    assert {"split_at_scene_cut", "no_word_timestamps", "known_hallucination",
            "likely_no_speech", "agent_marked_unreliable"} <= allowed
