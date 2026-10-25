# anime-recap-ja — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give OpenMontage a Layer 2 skill, a data contract and a stdlib validator for 60–90 s Japanese anime review scripts whose every factual sentence is traceable to a scene/line of a source index.

**Architecture:** No pipeline or tool changes. A Layer 2 skill (`skills/creative/anime-recap-ja.md`) tells the agent how to build two artifacts — `anime_source_index` (what the episode contains, with timestamps) and `anime_recap_plan` (the review script + cut list). `lib/anime_recap.py` validates a plan against an index and is runnable as `python -m lib.anime_recap`. JSON Schemas document both artifacts.

**Tech Stack:** Python 3 stdlib only (validator), pytest (tests), JSON Schema draft-07 (docs; checked only when `jsonschema` is importable).

**Spec:** user request "GIAI ĐOẠN 1" in session (items 1–6 + constraints).

## Global Constraints

- Do not modify core pipelines (`pipeline_defs/`, `tools/`, `lib/checkpoint.py`).
- No new dependencies, no paid APIs, no network in tests.
- Mock data must declare `provenance.kind = "mock"`; nothing may claim a real episode was analysed.
- No GUI, TTS or render in this phase.
- Story roles required in every plan: `hook`, `development`, `conflict`, `climax`, `resolution`.
- Target length 60–90 s; Japanese narration speech-rate ceiling 9.0 chars/s.

## Review Focus

1. NaN / non-numeric timestamps → error, not crash.
2. Fractional FPS (23.976) → frame numbers use `round(seconds * fps)`.
3. A segment spanning two adjacent scenes → allowed when range is inside their union.
4. A quote whose wording differs from the source line → error (no paraphrased "quotes").
5. Malformed JSON / missing top-level keys → structured error report, CLI exit 1.

---

### Task 1: Fixtures (mock source index + valid plan)

**Files:** Create `tests/fixtures/anime_recap/mock_source_index.json`, `tests/fixtures/anime_recap/mock_recap_plan.json`

- [ ] 8-minute (480 s) fictional episode at 23.976 fps, 12 scenes, dialogue with `text_ja`/`text_vi`, `provenance.kind = "mock"`.
- [ ] 7-segment plan, ~79 s, covering all five roles, frames computed with `round(s*fps)`.

### Task 2: Validator (TDD)

**Files:** Create `lib/anime_recap.py`; Test `tests/lib/test_anime_recap_validator.py`

**Interfaces (Produces):**
- `validate_recap_plan(plan: dict, source_index: dict, *, min_total=60.0, max_total=90.0) -> ValidationReport`
- `ValidationReport(errors: list[Issue], warnings: list[Issue], source_is_mock: bool)`, `.ok -> bool`, `.codes() -> set[str]`
- `Issue(code: str, path: str, message: str)`
- `estimate_ja_speech_seconds(text: str, chars_per_second: float = 6.5) -> float`
- `seconds_to_frame(seconds: float, fps: float) -> int`
- `main(argv: list[str] | None = None) -> int` (CLI: `--plan`, `--index`, `--json`)

**Error codes pinned by tests:** `missing_field`, `index_mismatch`, `fps_mismatch`, `invalid_timestamp`, `out_of_video`, `outside_scene`, `frame_mismatch`, `unknown_scene`, `unknown_line`, `evidence_outside_scene`, `claim_without_evidence`, `quote_not_in_source`, `narration_claims_mismatch`, `narration_exceeds_cut`, `speech_rate_too_fast`, `timeline_gap`, `total_duration_out_of_range`, `missing_story_role`, `first_segment_not_hook`, `story_order`, `dialogue_text_mismatch`, `duplicate_id`, `invalid_json`.

- [ ] Write failing tests (one per code + valid fixture passes + CLI + helpers); run → FAIL (module missing).
- [ ] Implement; run → PASS.

### Task 3: Schemas + skill + index entry

**Files:** Create `schemas/artifacts/anime_source_index.schema.json`, `schemas/artifacts/anime_recap_plan.schema.json`, `skills/creative/anime-recap-ja.md`; Modify `skills/INDEX.md` (one row); Test `tests/contracts/test_anime_recap_skill_contract.py`

- [ ] Failing contract test: skill exists, names the validator command, both schema paths, five roles, five screenwriting skills, the no-fabrication rule; INDEX lists it; fixtures satisfy schemas when `jsonschema` is importable.
- [ ] Write schemas, skill, INDEX row; run → PASS.

### Task 4: Verify + commit

- [ ] Run the new tests, then the broader suite; record unrelated failures by name.
- [ ] Commit and push to `claude/charming-meitner-s9tzcb`.
