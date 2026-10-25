# Anime real-footage trial on Windows — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans.

**Goal:** One-click Windows kit (RTX 3060) that installs only what Phase 2 needs, lets the user pick an MP4, runs a 30–60 s clip trial then the full episode through the Phase 2 indexer, and writes an honest report (timings, automatic checks, manual/AI-vision steps marked pending).

**Architecture:** Thin `.bat` wrappers → `python -m lib.anime_trial {setup,run,score}`. All logic in Python so it is testable on Linux. Reuses `lib.anime_source_indexer.build_source_index` (adds only `asr_device` and `timings` hooks) and `tools/analysis/transcriber.py` (adds an optional `device` input).

## Global Constraints
- No paid APIs; no anime video, transcripts or legacy source in git (`anime_trial/` and `.venv-anime/` ignored).
- No TTS, GUI app, render. AI visual review and human listening checks are reported as PENDING, never as done.
- GPU path: pip `nvidia-cublas-cu12` + `nvidia-cudnn-cu12==9.*`, DLL dirs registered at runtime; CPU fallback stays.

## Review Focus
1. Path with quotes / non-ASCII (Windows user folders) → copied to a safe name before ffmpeg lavfi.
2. CUDA present but cuBLAS/cuDNN DLLs missing → transcriber's CPU fallback, reported in the report.
3. Clip start beyond video end → clear error.
4. Half-filled manual CSV → status "partial", never PASS.
5. Excel opening Japanese CSV → UTF-8 BOM.

### Task 1: transcriber `device` input; indexer `asr_device` + `timings` (TDD)
### Task 2: `lib/anime_trial.py` env checks, pip plan, DLL registration, clip, safe input (TDD)
### Task 3: automatic checks (structure, timing, SSIM false-cut suspicion), CSV sheets, score, report (TDD)
### Task 4: `.bat` wrappers, `.gitignore`, Vietnamese guide, contract test; verify; commit; push.
