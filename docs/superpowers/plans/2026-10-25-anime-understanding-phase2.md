# Anime Video Understanding — Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Turn an ≈8-minute anime MP4 (no subtitles, original Japanese audio) into a Phase 1–compatible `anime_source_index.json`, by reusing OpenMontage's `scene_detect`, `frame_sampler` and `transcriber` (faster-whisper) tools.

**Architecture:** One library module `lib/anime_source_indexer.py` (like `lib/source_media_review.py`) orchestrates existing tools: ffprobe → low-res proxy → `SceneDetect` → frame-snapped scenes → `FrameSampler` keyframes → `Transcriber(language="ja")` or a supplied transcript JSON → word-level assignment of dialogue to scenes → index + review packet. Visual understanding (characters, actions, reactions, speaker, Vietnamese translation) is done by the agent looking at the keyframes and merged with `apply_visual_annotations`, which validates every id. No pipeline/tool modifications.

**Tech Stack:** Python stdlib + existing tools, ffmpeg/ffprobe, pytest. faster-whisper optional at runtime.

## Global Constraints

- No TTS, render or GUI. No paid APIs. No changes to `tools/` or `pipeline_defs/`.
- Output must pass Phase 1 (`lib/anime_recap.py`) and `schemas/artifacts/anime_source_index.schema.json`.
- Never fabricate: the builder leaves `visual_summary` empty with `visual_review: "pending"`; dialogue only from ASR; no transcriber and no transcript → hard error.
- Frame math uses the exact stream fps (e.g. 24000/1001), not a 2-decimal rounding.

## Review Focus

1. `scene_detect` returning a single scene for a long video (timeout fallback) → warning `scene_detect_degenerate`.
2. A Whisper segment spanning a cut → split at the boundary by word timestamps.
3. Whisper hallucinations on silence/music (「ご視聴ありがとうございました」 …) → flagged, low confidence.
4. Long static scenes → keyframes at most every 6 s, not just 3.
5. Annotation JSON naming unknown scene/line/character → rejected, index unchanged.

### Task 1: Pure helpers (TDD) — `snap_to_frame`, `keyframe_timestamps`, `assign_dialogue`, `apply_visual_annotations`
Test: `tests/lib/test_anime_source_indexer.py` (no ffmpeg needed).

### Task 2: Media steps (TDD with a synthetic sample video) — `probe_video`, `detect_scenes`, `extract_keyframes`, `build_source_index`, `write_review_packet`, CLI
Test: same file; session fixture renders a 4-scene 13 s clip at 24000/1001 fps with ffmpeg; transcript fixture mimics faster-whisper output. Phase 1 compatibility test builds a plan on the generated index and runs `validate_recap_plan`. A real faster-whisper test runs only when `faster_whisper` is importable and `OPENMONTAGE_JA_SAMPLE_VIDEO` points at a Japanese clip.

### Task 3: Schema + skill update
Additive fields in `anime_source_index.schema.json`; `skills/creative/anime-recap-ja.md` step 1 points at the builder; contract test asserts it.

### Task 4: Verify (new tests, full suite vs baseline), commit, push.
