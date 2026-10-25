# anime-recap-ja — technical decisions (read this before re-analysing)

Branch `claude/charming-meitner-s9tzcb`. Plans: `docs/superpowers/plans/2026-10-25-*`.

## Phase 1 (commit 6f621d3) — recap contract
- `skills/creative/anime-recap-ja.md`, `schemas/artifacts/anime_{source_index,recap_plan}.schema.json`,
  `lib/anime_recap.py` validator (`python -m lib.anime_recap`). Every event/quote claim cites scene/line evidence.
- Schemas are not in `schemas/artifacts/__init__.py:ARTIFACT_NAMES` on purpose (no checkpoint changes);
  `load_schema("anime_source_index")` still works by filename.

## Phase 2 — video understanding (`lib/anime_source_indexer.py`)
- Orchestrates existing tools: `scene_detect` → `frame_sampler` → `transcriber`; no new tool class.
- fps from `r_frame_rate` (exact 24000/1001). VFR = stream duration drifts > 1 frame from
  `nb_frames` on the nominal grid → warning (stream-copied joins showed avg 23.84 vs 23.976).
- Scene detection runs on a 360p audio-less proxy (identical frame timing, verified).
  ffmpeg `select=scene` is luma-only: green→red cuts scored < 0.02 and were merged (181 of 240
  scenes on the 8-min test). Fix: union with `format=gbrp,scdet=threshold=10` (scored 49.7).
  With PySceneDetect installed, ContentDetector threshold 22 is used instead.
- Dialogue → scenes by word midpoint; a segment spanning a cut is split. Flags:
  `known_hallucination`, `likely_no_speech` (no_speech_prob > 0.6), `split_at_scene_cut`,
  `no_word_timestamps`; `low_confidence` lines must not be quoted unverified.
- Visual understanding is the agent reading keyframes (`review_packet.md`) and merging via
  `annotate`; ids are validated, nothing is applied on any bad reference. The builder never fills
  visual fields itself.
- `tools/analysis/transcriber.py`: additive `initial_prompt` / `beam_size` inputs and per-segment
  `avg_logprob` / `no_speech_prob`; default call unchanged.

## Windows real-footage trial kit (`lib/anime_trial.py`, `scripts/anime_trial/*.bat`)
- Guide: `docs/anime-recap/HUONG_DAN_CHAY_THU_WINDOWS.md`. `.bat` files only find Python 3.11/3.12,
  create `.venv-anime`, offer `winget install Gyan.FFmpeg`, then call `python -m lib.anime_trial`
  `setup|run|score` — all logic is Python and unit-tested on Linux. The `.bat` files have never run on Windows.
- GPU: pip `nvidia-cublas-cu12` + `nvidia-cudnn-cu12==9.*`; `register_windows_cuda_dlls()` adds
  `site-packages/nvidia/*/bin` via `os.add_dll_directory`. Transcriber gained `device: auto|cuda|cpu`
  (cuda without a GPU → CPU + `gpu_fallback_reason`); setup smoke-tests Whisper on 1 s of silence.
- Clip trial: re-encoded at `-ss` (frame-accurate, starts at 0, nominal `-r`); default start 60 s
  when the video is ≥ 3 min; report states the offset to add.
- Paths with non-ASCII/quotes are copied to `work/input.mp4` (ffmpeg lavfi `movie=` breaks on quotes).
- Automatic checks: schema, contiguity, frame grid, keyframes, lines in scenes, ids, dialogue heuristics,
  suspected false cuts = RGB SSIM ≥ 0.92 across a cut. YUV SSIM was luma-dominated (green→red = 0.93,
  59 false alarms on the 8-min test); `format=gbrp` gives 0.008.
- Manual sheets are UTF-8-BOM CSV (Excel). `score` → accuracy, CER (Levenshtein), false-cut rate;
  half-filled = PARTIAL. AI visual review / manual checks stay PENDING; `overall` is FAIL or PENDING
  until they are really done — never PASS by default.
- 8-min 1080p synthetic, full mode, no ASR, cloud CPU: total 333 s; keyframes 223 s (720 ffmpeg
  spawns in `frame_sampler`), proxy 51 s, checks 37 s, scene detect 19 s. Keyframe batching is the
  obvious speed-up if it matters on the user's PC (tool left unchanged on purpose).

## Fix: lines_inside_scenes FAIL on episode #253-1 (430.9 s, 70 scenes, 117 lines)
- Reported ids sc003_l02, sc011_l01, sc036_l01, sc040_l01, sc041_l01 (checker listed at most 5).
- Root cause (reproduced with 6-decimal frame-grid cuts; the synthetic fixtures used round 3.003 cuts):
  `assign_dialogue` clamped ASR times to the scene and *then* `round(…, 3)`; a start clamped to
  12.554208 became 12.554 (before the scene), an end clamped to 30.030542 became 30.031 (after it).
  Fix: round ASR times first, clamp with the exact scene bounds; keep originals in
  `asr_start/end_seconds` + flag `clamped_to_scene`; zero-length/inverted → 1 ms line + `invalid_asr_timing`;
  order kept per scene; lines starting before the previous one ended → `overlaps_previous`.
- Checker now separates: outside scene (FAIL, with ms deviation), out of order (FAIL),
  overlap/crosstalk (`line_overlaps` WARN, not a data error); counts all ids, not 5. No tolerance added.
- Existing runs: `recheck` / `5_KIEM_TRA_LAI.bat` rebuilds dialogue from `work/*_transcript.json`
  (no Whisper, no scene detection), backs up the old index and a filled dialogue sheet.
- Not yet confirmed on the user's real files (need `anime_source_index.json` + `work/*_transcript.json`).

## Legacy AI_VIDEO_DIRECTOR_CN_JP (private; never commit its source to this public repo)
| Legacy module | Decision | Why |
|---|---|---|
| asr/transcribe.py — JA anime prompt, beam 5, no_speech penalty | ADAPT | re-implemented as transcriber options + indexer flags |
| asr/transcribe.py — SRT/VTT/TXT import | SKIP | episodes have no subtitles; `--transcript` takes transcriber JSON |
| episode/analyze.py — line dB vs median, exclamation, short line → excite | ADAPT | `measure_line_loudness` (ffmpeg + stdlib) |
| episode/analyze.py — warn on small Whisper model | ADAPT | `asr_model_small` warning |
| vision/scenes.py — ContentDetector threshold 22, min 0.4 s | ADAPT | threshold 22 when PySceneDetect present; min 0.5 s kept |
| vision/scenes.py — text-band crop, motion score (OpenCV) | SKIP | clean episodes; keyframes ≤ 6 s apart; no OpenCV dependency |
| media/probe.py — VFR check, frame PTS, wav16k | KEEP OpenMontage / SKIP / ADAPT | drift-based VFR is stricter; PTS not needed yet; wav decode reused in loudness |
| episode/beats.py, script.py, translate/facts.py | Phase 3 | story extraction & Japanese script |
| align/autofit.py, episode/assemble.py, tts/* (VOICEVOX/AivisSpeech) | Phase 4 | measured voice timing, local JA TTS, cut export |
| vision/clean_match.py | only for Douyin-vs-clean matching | not episode understanding |

## Environment facts (cloud container, 2026-10-25)
- huggingface.co is blocked by the network policy (proxy 403) → Whisper models cannot download here;
  real ASR must run where HF is reachable, or pass `--transcript`. PyPI is reachable.
- Not installed: faster-whisper, scenedetect, opencv, numpy. ffmpeg 6.1 present.
- 8-min 1080p synthetic benchmark: 240/240 cuts found, 4 min 42 s end-to-end (no ASR).
- No real anime episode has been analysed yet.
