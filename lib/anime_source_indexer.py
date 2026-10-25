"""Build an anime_source_index from an anime episode file (Phase 2).

Reuses OpenMontage analysis tools instead of re-implementing them:

- ``scene_detect`` (PySceneDetect, or the ffmpeg scene filter) on a low-res
  proxy → scene boundaries, snapped to the exact frame grid;
- ``frame_sampler`` → keyframes per scene (≥3, and at least every 6 s);
- ``transcriber`` (faster-whisper, ``language="ja"``) → Japanese dialogue with
  word timestamps — or a transcript JSON produced by it earlier;
- word-level assignment of dialogue to scenes (a segment spanning a cut is
  split at the cut), mean word probability as ``asr_confidence``, and flags for
  known Whisper hallucinations.

What this module does *not* do is "look" at the frames. Each scene is written
with ``visual_summary: ""`` and ``visual_review: "pending"``; the agent reads
the keyframes listed in ``review_packet.md``, writes an annotations JSON, and
``apply_visual_annotations`` merges it after checking every scene, line and
character id. Output is the Phase 1 artifact
``schemas/artifacts/anime_source_index.schema.json`` validated by
``lib/anime_recap.py``.

CLI::

    python -m lib.anime_source_indexer build --video ep.mp4 --out projects/<id>/artifacts/source
    python -m lib.anime_source_indexer annotate --index .../anime_source_index.json --annotations ann.json
"""

from __future__ import annotations

import argparse
import bisect
import copy
import importlib.util
import json
import math
import re
import subprocess
import sys
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from lib.anime_recap import Issue

KEYFRAME_MAX_INTERVAL_SECONDS = 6.0
KEYFRAMES_PER_SCENE_MIN = 3
KEYFRAME_EDGE_FRAMES = 2
DEFAULT_MIN_SCENE_SECONDS = 0.5
DEFAULT_LOW_CONFIDENCE = 0.5
DEGENERATE_SINGLE_SCENE_SECONDS = 30.0
PROXY_MAX_HEIGHT = 360
VFR_MAX_DRIFT_FRAMES = 1.0  # stream duration vs frame count on the nominal grid
VFR_RATE_TOLERANCE = 0.001  # avg vs nominal rate, when the frame count is unknown

# Phrases Whisper emits over silence/music in Japanese audio (training-data
# artifacts from video outros). Matched after removing whitespace.
KNOWN_JA_HALLUCINATIONS = (
    "ご視聴ありがとうございました",
    "ご視聴ありがとうございます",
    "チャンネル登録",
    "高評価",
    "字幕作成",
    "字幕by",
    "Thankyouforwatching",
    "以下は日本語のアニメの会話です",  # echo of JA_ANIME_PROMPT
)

# Decoding context for faster-whisper on anime dialogue (with beam search 5).
JA_ANIME_PROMPT = "以下は日本語のアニメの会話です。"
JA_BEAM_SIZE = 5
SMALL_ASR_MODELS = frozenset({"tiny", "base", "small"})
NO_SPEECH_FLAG_PROBABILITY = 0.6

# Content-detector threshold tuned lower than PySceneDetect's 27 for flat-shaded
# anime cuts; RGB scdet catches cuts between colours of equal luma, which the
# luma-only ffmpeg `select=scene` fallback cannot see.
ANIME_CONTENT_THRESHOLD = 22.0
RGB_SCDET_THRESHOLD = 10.0

# Heuristic "excitement" of a line: loudness vs the episode median, exclamation
# marks / stretched vowels, short punchy lines. A beat-mining signal, not a
# judgement about the story.
_EXCLAIM = re.compile(r"[！!？?]|ー{2,}|っ$|ッ$|[ぁぃぅぇぉ]{2,}")
LOUDNESS_SAMPLE_RATE = 16000


class IndexBuildError(RuntimeError):
    """The index cannot be built honestly (missing input, ASR, or a tool failed)."""


@dataclass
class BuildResult:
    index: dict
    index_path: Path
    review_packet_path: Path
    annotations_template_path: Path
    warnings: list[Issue] = field(default_factory=list)


# ── frame math ───────────────────────────────────────────────────────────


def parse_fps(rate: str) -> float:
    """ffprobe rate ('24000/1001', '30/1') → float with NTSC precision kept."""
    try:
        value = Fraction(rate)
    except (ValueError, ZeroDivisionError):
        return 0.0
    return round(float(value), 6) if value > 0 else 0.0


def snap_to_frame(seconds: float, fps: float) -> float:
    """Timestamp of the nearest frame boundary."""
    return round(round(seconds * fps) / fps, 6)


def keyframe_timestamps(
    start: float,
    end: float,
    fps: float,
    *,
    max_interval: float = KEYFRAME_MAX_INTERVAL_SECONDS,
    min_count: int = KEYFRAMES_PER_SCENE_MIN,
) -> list[float]:
    """Evenly spaced frame-aligned timestamps inside [start, end).

    Skips a couple of frames at each edge (cut transitions can blend), takes at
    least ``min_count`` frames and never leaves a gap wider than
    ``max_interval`` so long static-camera scenes still show their action.
    """
    frame = 1.0 / fps
    last = end - frame
    lo, hi = start + KEYFRAME_EDGE_FRAMES * frame, end - KEYFRAME_EDGE_FRAMES * frame
    if hi <= lo:
        return [snap_to_frame(min(max((start + end) / 2, start), last), fps)]
    count = max(min_count, math.ceil((hi - lo) / max_interval) + 1)
    step = (hi - lo) / (count - 1)
    stamps = []
    for i in range(count):
        t = snap_to_frame(lo + i * step, fps)
        t = min(max(t, start), last)
        if not stamps or t > stamps[-1]:
            stamps.append(t)
    return stamps


# ── dialogue → scenes ────────────────────────────────────────────────────


def _is_hallucination(text: str) -> bool:
    compact = re.sub(r"\s", "", text)
    return any(phrase in compact for phrase in KNOWN_JA_HALLUCINATIONS)


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def assign_dialogue(
    segments: list[dict],
    scenes: list[dict],
    *,
    low_confidence_threshold: float = DEFAULT_LOW_CONFIDENCE,
) -> dict[str, list[dict]]:
    """Split transcriber segments into per-scene dialogue lines.

    Words are assigned to the scene containing their midpoint, so a sentence
    spoken across a cut becomes two lines. Segments without word timestamps go
    whole to the scene they overlap most.
    """
    starts = [s["start_seconds"] for s in scenes]
    by_scene: dict[str, list[dict]] = {s["scene_id"]: [] for s in scenes}

    def scene_at(t: float) -> int:
        return max(0, min(len(scenes) - 1, bisect.bisect_right(starts, t) - 1))

    pieces: list[tuple[int, float, float, str, list[float], list[str]]] = []
    for seg in segments:
        no_speech = seg.get("no_speech_prob")
        seg_flags = ["likely_no_speech"] if isinstance(no_speech, (int, float)) and no_speech > NO_SPEECH_FLAG_PROBABILITY else []
        words = [w for w in seg.get("words") or [] if isinstance(w.get("start"), (int, float)) and isinstance(w.get("end"), (int, float))]
        if words:
            groups: list[tuple[int, list[dict]]] = []
            for w in words:
                idx = scene_at((w["start"] + w["end"]) / 2)
                if groups and groups[-1][0] == idx:
                    groups[-1][1].append(w)
                else:
                    groups.append((idx, [w]))
            split = seg_flags + (["split_at_scene_cut"] if len(groups) > 1 else [])
            for idx, group in groups:
                text = "".join(str(w.get("word", "")).strip() for w in group)
                probs = [float(w["probability"]) for w in group if isinstance(w.get("probability"), (int, float))]
                pieces.append((idx, group[0]["start"], group[-1]["end"], text, probs, list(split)))
        else:
            s0, s1 = float(seg.get("start", 0.0)), float(seg.get("end", 0.0))
            idx = max(range(len(scenes)), key=lambda i: (_overlap(s0, s1, scenes[i]["start_seconds"], scenes[i]["end_seconds"]), -i))
            pieces.append((idx, s0, s1, str(seg.get("text", "")).strip(), [], seg_flags + ["no_word_timestamps"]))

    pieces.sort(key=lambda p: p[1])
    for idx, start, end, text, probs, flags in pieces:
        if not text:
            continue
        scene = scenes[idx]
        start = min(max(start, scene["start_seconds"]), scene["end_seconds"])
        end = min(max(end, start), scene["end_seconds"])
        if end <= start:
            end = min(scene["end_seconds"], start + 0.01)
            start = min(start, end - 0.001)
        lines = by_scene[scene["scene_id"]]
        line: dict[str, Any] = {
            "line_id": f"{scene['scene_id']}_l{len(lines) + 1:02d}",
            "start_seconds": round(start, 3),
            "end_seconds": round(end, 3),
            "speaker": None,
            "text_ja": text,
        }
        confidence = round(sum(probs) / len(probs), 3) if probs else None
        if confidence is not None:
            line["asr_confidence"] = confidence
        if _is_hallucination(text):
            flags.append("known_hallucination")
        line["asr_flags"] = flags
        line["low_confidence"] = bool(
            "known_hallucination" in flags
            or "likely_no_speech" in flags
            or (confidence is not None and confidence < low_confidence_threshold)
        )
        lines.append(line)
    return by_scene


# ── media steps (ffprobe / existing tools) ───────────────────────────────


def _run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise IndexBuildError(f"{cmd[0]} failed: {detail.strip()[:500]}") from exc


def probe_video(path: Path) -> dict[str, Any]:
    """fps, video-stream duration, size and stream presence via ffprobe."""
    proc = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], timeout=60)
    data = json.loads(proc.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise IndexBuildError(f"{path} has no video stream")
    # r_frame_rate is the nominal rate an editor's frame grid uses; avg_frame_rate
    # drifts from it on variable-frame-rate files and stream-copied joins.
    nominal = parse_fps(video.get("r_frame_rate", "0/0"))
    average = parse_fps(video.get("avg_frame_rate", "0/0"))
    fps = nominal or average
    if fps <= 0:
        raise IndexBuildError(f"cannot read the frame rate of {path}")
    frames = video.get("nb_frames")
    stream_duration = float(video.get("duration") or 0)
    if frames and str(frames).isdigit() and int(frames) > 0 and stream_duration > 0:
        # Irregular when presentation time drifts more than one frame off the nominal grid.
        variable = abs(stream_duration * fps - int(frames)) > VFR_MAX_DRIFT_FRAMES
    else:
        variable = bool(nominal and average and abs(average - nominal) / nominal > VFR_RATE_TOLERANCE)
    if not variable and frames and str(frames).isdigit() and int(frames) > 0:
        duration = int(frames) / fps
    else:
        duration = stream_duration or float(data.get("format", {}).get("duration") or 0)
    if duration <= 0:
        raise IndexBuildError(f"cannot read the duration of {path}")
    return {
        "fps": fps,
        "duration_seconds": round(duration, 6),
        "width": int(video.get("width", 0)),
        "height": int(video.get("height", 0)),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
        "has_subtitle_stream": any(s.get("codec_type") == "subtitle" for s in streams),
        "variable_frame_rate": variable,
    }


def make_analysis_proxy(video: Path, out_path: Path, height: int = PROXY_MAX_HEIGHT) -> Path:
    """Small, audio-less copy with identical frame timing — scene detection on an
    8-minute 1080p file is otherwise slow enough to hit scene_detect's timeout."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _run([
        "ffmpeg", "-v", "error", "-y", "-i", str(video), "-an",
        "-vf", f"scale=-2:{height}", "-fps_mode", "passthrough",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", str(out_path),
    ])
    return out_path


def detect_scenes(
    video: Path,
    work_dir: Path,
    *,
    fps: float,
    duration: float,
    threshold: float | None = None,
    min_scene_seconds: float = DEFAULT_MIN_SCENE_SECONDS,
    detector: Any = None,
) -> tuple[list[dict], list[Issue]]:
    """Run scene_detect and return contiguous, frame-snapped scenes.

    With the default tool on its ffmpeg fallback, an RGB ``scdet`` pass is
    unioned in so cuts between equal-luma colours are not lost.
    """
    supplement = detector is None
    if detector is None:
        from tools.analysis.scene_detect import SceneDetect

        detector = SceneDetect()
        if threshold is None and detector._has_pyscenedetect():
            threshold = ANIME_CONTENT_THRESHOLD
    inputs: dict[str, Any] = {
        "input_path": str(video),
        "method": "content",
        "min_scene_length_seconds": min_scene_seconds,
        "output_path": str(work_dir / "scenes_raw.json"),
    }
    if threshold is not None:  # scale differs between PySceneDetect (≈27) and ffmpeg (≈0.3)
        inputs["threshold"] = threshold
    result = detector.execute(inputs)
    if not result.success:
        raise IndexBuildError(f"scene_detect failed: {result.error}")

    candidates = [float(scene.get("start_seconds", 0.0)) for scene in result.data.get("scenes", [])]
    if supplement and result.data.get("method") == "ffmpeg":
        candidates += rgb_scene_cuts(video)

    frame = 1.0 / fps
    end_of_video = snap_to_frame(duration, fps)
    min_gap = max(frame, min_scene_seconds) - 1e-9
    cuts = [0.0]
    for t in sorted(snap_to_frame(c, fps) for c in candidates):
        if t - cuts[-1] >= min_gap and t < end_of_video - frame / 2:
            cuts.append(t)
    width = max(3, len(str(len(cuts))))
    bounds = cuts + [end_of_video]
    scenes = [
        {"scene_id": f"sc{i + 1:0{width}d}", "start_seconds": bounds[i], "end_seconds": bounds[i + 1]}
        for i in range(len(cuts))
    ]
    warnings = []
    if len(scenes) == 1 and duration > DEGENERATE_SINGLE_SCENE_SECONDS:
        warnings.append(Issue(
            "scene_detect_degenerate", "scenes",
            f"one scene for {duration:.0f}s of video — detection probably timed out or the threshold is too high; "
            "inspect frames before trusting the index",
        ))
    return scenes, warnings


def rgb_scene_cuts(video: Path, threshold: float = RGB_SCDET_THRESHOLD) -> list[float]:
    """Cut times from ffmpeg ``scdet`` on planar RGB (sees hue changes, not just luma)."""
    proc = _run([
        "ffmpeg", "-v", "error", "-i", str(video), "-an",
        "-vf", f"format=gbrp,scdet=threshold={threshold}:sc_pass=1,metadata=print:file=-",
        "-f", "null", "-",
    ])
    return [float(m) for m in re.findall(r"lavfi\.scd\.time=([0-9.]+)", proc.stdout)]


def measure_line_loudness(video: Path, lines_by_scene: dict[str, list[dict]]) -> None:
    """Add ``loudness_db`` and heuristic ``excite`` (0–3) to every line, in place.

    excite = (dB above the episode's median line loudness) / 6
             + 0.8 if the text exclaims / stretches vowels + 0.4 if the line is ≤ 2.5 s.
    """
    import array

    lines = [line for scene_lines in lines_by_scene.values() for line in scene_lines]
    if not lines:
        return
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", str(LOUDNESS_SAMPLE_RATE),
         "-f", "s16le", "-"],
        capture_output=True, timeout=600, check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise IndexBuildError(f"ffmpeg could not decode audio for loudness: {proc.stderr.decode('utf-8', 'replace')[:300]}")
    samples = array.array("h")
    samples.frombytes(proc.stdout[: len(proc.stdout) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()

    for line in lines:
        a = int(line["start_seconds"] * LOUDNESS_SAMPLE_RATE)
        b = max(a + 1, int(line["end_seconds"] * LOUDNESS_SAMPLE_RATE))
        chunk = samples[a:b]
        if chunk:
            rms = math.sqrt(sum(s * s for s in chunk) / len(chunk)) / 32768.0
            line["loudness_db"] = round(20 * math.log10(rms + 1e-9), 1)
        else:
            line["loudness_db"] = -90.0
    levels = sorted(line["loudness_db"] for line in lines)
    median = levels[len(levels) // 2] if len(levels) % 2 else (levels[len(levels) // 2 - 1] + levels[len(levels) // 2]) / 2
    for line in lines:
        loud = max(0.0, (line["loudness_db"] - median) / 6.0)
        exclaim = 0.8 if _EXCLAIM.search(line.get("text_ja", "")) else 0.0
        short = 0.4 if line["end_seconds"] - line["start_seconds"] <= 2.5 else 0.0
        line["excite"] = round(min(3.0, loud + exclaim + short), 2)


def extract_keyframes(
    video: Path, scenes: list[dict], out_dir: Path, fps: float, *, sampler: Any = None
) -> dict[str, list[dict]]:
    """Keyframes per scene via frame_sampler, renamed ``<scene_id>_kfNN.jpg``."""
    if sampler is None:
        from tools.analysis.frame_sampler import FrameSampler

        sampler = FrameSampler()
    wanted: list[tuple[str, float]] = [
        (scene["scene_id"], t) for scene in scenes for t in keyframe_timestamps(scene["start_seconds"], scene["end_seconds"], fps)
    ]
    raw_dir = out_dir / "_raw"
    result = sampler.execute({
        "input_path": str(video), "strategy": "timestamps", "timestamps": [t for _, t in wanted],
        "output_dir": str(raw_dir), "format": "jpg", "quality": 3,
    })
    if not result.success:
        raise IndexBuildError(f"frame_sampler failed: {result.error}")
    by_scene: dict[str, list[dict]] = {scene["scene_id"]: [] for scene in scenes}
    for frame in result.data.get("frames", []):
        scene_id, t = wanted[frame["index"]]
        target = out_dir / f"{scene_id}_kf{len(by_scene[scene_id]) + 1:02d}.jpg"
        Path(frame["path"]).replace(target)
        by_scene[scene_id].append({"timestamp_seconds": t, "path": str(target.resolve())})
    if raw_dir.exists() and not any(raw_dir.iterdir()):
        raw_dir.rmdir()
    return by_scene


def _faster_whisper_available() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def transcribe_japanese(video: Path, work_dir: Path, model_size: str) -> dict:
    """Run the transcriber tool (faster-whisper) with language forced to ja."""
    from tools.analysis.transcriber import Transcriber

    result = Transcriber().execute({
        "input_path": str(video), "language": "ja", "model_size": model_size, "output_dir": str(work_dir),
        "initial_prompt": JA_ANIME_PROMPT, "beam_size": JA_BEAM_SIZE,
    })
    if not result.success:
        raise IndexBuildError(
            f"transcriber failed: {result.error}. The first run downloads the Whisper model from huggingface.co; "
            "if that host is blocked, run the transcriber elsewhere and pass --transcript"
        )
    return result.data


def _load_transcript(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IndexBuildError(f"cannot read transcript {path}: {exc}") from exc
    if isinstance(data, list):
        data = {"segments": data}
    if not isinstance(data.get("segments"), list):
        raise IndexBuildError(f"transcript {path} has no 'segments' list (expected transcriber output)")
    return data


# ── review packet for the agent's visual pass ────────────────────────────


def annotations_template(index: dict) -> dict:
    return {
        "source_index_id": index["source_index_id"],
        "characters": [],
        "scenes": [
            {"scene_id": s["scene_id"], "visual_summary": "", "characters": [], "actions": [], "reactions": []}
            for s in index["scenes"]
        ],
        "lines": [
            {"line_id": line["line_id"], "speaker": None, "text_vi": ""}
            for s in index["scenes"] for line in s["dialogue"]
        ],
    }


def write_review_packet(index: dict, path: Path) -> Path:
    """Markdown the agent works through: keyframes + dialogue, scene by scene."""
    base = path.parent
    out = [
        f"# Visual review packet — {index['source_index_id']}",
        "",
        (
            f"Source: `{index['video']['path']}` · {index['video']['fps']} fps · "
            f"{index['video']['duration_seconds']:.2f} s · {len(index['scenes'])} scenes · "
            f"provenance `{index['provenance']['kind']}`"
        ),
        "",
        (
            "For every scene: open each keyframe image, then fill `visual_annotations.template.json` "
            "(visual_summary = what is visible, characters, actions, reactions; per line: speaker, text_vi). "
            "Write only what the frames and lines show. Leave a field empty when unsure. "
            "Lines flagged `low_confidence` or `known_hallucination` must not be quoted until you have verified them."
        ),
        "",
        (
            "Merge with: `python -m lib.anime_source_indexer annotate --index anime_source_index.json "
            "--annotations visual_annotations.json`"
        ),
        "",
    ]
    for scene in index["scenes"]:
        out.append(f"## {scene['scene_id']} — {scene['start_seconds']:.3f}–{scene['end_seconds']:.3f} s")
        out.append("")
        for kf in scene["keyframes"]:
            kf_path = Path(kf["path"])
            try:
                shown = kf_path.relative_to(base.resolve())
            except ValueError:
                shown = kf_path
            out.append(f"- keyframe {kf['timestamp_seconds']:.3f} s: `{shown}`")
        if scene["dialogue"]:
            out.append("")
            for line in scene["dialogue"]:
                conf = line.get("asr_confidence")
                meta = [f"conf {conf:.2f}" if conf is not None else "conf ?"] + line.get("asr_flags", [])
                if line.get("low_confidence"):
                    meta.append("LOW CONFIDENCE")
                out.append(
                    f"- `{line['line_id']}` {line['start_seconds']:.3f}–{line['end_seconds']:.3f} s "
                    f"「{line['text_ja']}」 ({', '.join(meta)})"
                )
        else:
            out.append("- (no dialogue detected)")
        out.append("")
    path.write_text("\n".join(out), encoding="utf-8")
    return path


# ── build ────────────────────────────────────────────────────────────────


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower() or "episode"


def build_source_index(
    video_path: Path,
    output_dir: Path,
    *,
    transcript_path: Path | None = None,
    source_index_id: str | None = None,
    model_size: str = "small",
    provenance_kind: str = "analyzed",
    scene_threshold: float | None = None,
    min_scene_seconds: float = DEFAULT_MIN_SCENE_SECONDS,
    low_confidence_threshold: float = DEFAULT_LOW_CONFIDENCE,
) -> BuildResult:
    """Analyse ``video_path`` and write ``anime_source_index.json`` + review packet."""
    video_path = Path(video_path)
    if not video_path.is_file():
        raise IndexBuildError(f"video not found: {video_path}")
    if provenance_kind not in ("analyzed", "mock"):
        raise IndexBuildError("provenance_kind must be 'analyzed' or 'mock'")
    if transcript_path is None and not _faster_whisper_available():
        raise IndexBuildError(
            "faster-whisper is not installed and no --transcript was given. Install it with "
            "`pip install faster-whisper` (the model download needs huggingface.co), or pass a transcript "
            "JSON produced by tools/analysis/transcriber.py. Dialogue is never invented."
        )

    output_dir = Path(output_dir)
    work_dir = output_dir / "work"
    keyframe_dir = output_dir / "keyframes"
    work_dir.mkdir(parents=True, exist_ok=True)
    keyframe_dir.mkdir(parents=True, exist_ok=True)

    info = probe_video(video_path)
    fps, duration = info["fps"], info["duration_seconds"]
    warnings: list[Issue] = []
    tools_used: list[str] = ["ffprobe"]
    if info["variable_frame_rate"]:
        warnings.append(Issue(
            "variable_frame_rate", "video.fps",
            f"frame timing is irregular; cut frames use the nominal {fps} fps — consider re-encoding to constant "
            "frame rate before cutting",
        ))

    detect_on = video_path
    if info["height"] > PROXY_MAX_HEIGHT:
        detect_on = make_analysis_proxy(video_path, work_dir / "analysis_proxy.mp4")
        tools_used.append("ffmpeg(proxy)")
    scenes, scene_warnings = detect_scenes(
        detect_on, work_dir, fps=fps, duration=duration, threshold=scene_threshold, min_scene_seconds=min_scene_seconds
    )
    warnings += scene_warnings
    tools_used.append("scene_detect")
    if importlib.util.find_spec("scenedetect") is None:
        tools_used.append("ffmpeg scdet(rgb)")  # detect_scenes' supplement on the ffmpeg fallback

    keyframes = extract_keyframes(video_path, scenes, keyframe_dir, fps)
    tools_used.append("frame_sampler")

    if transcript_path is not None:
        transcript = _load_transcript(Path(transcript_path))
        transcript_origin = "external_file"
        tools_used.append(f"transcript:{Path(transcript_path).name}")
    else:
        transcript = transcribe_japanese(video_path, work_dir, model_size)
        transcript_origin = "faster_whisper"
        tools_used.append(f"transcriber(faster-whisper {model_size}, ja)")
        if model_size in SMALL_ASR_MODELS:
            warnings.append(Issue(
                "asr_model_small", "transcript",
                f"Whisper '{model_size}' often mishears Japanese anime over background music — prefer --model-size large-v3",
            ))
    language = transcript.get("language")
    if language and language != "ja":
        warnings.append(Issue("transcript_not_japanese", "transcript.language", f"transcript language is '{language}', expected 'ja'"))

    dialogue = assign_dialogue(transcript["segments"], scenes, low_confidence_threshold=low_confidence_threshold)
    if not any(dialogue.values()):
        warnings.append(Issue("no_dialogue", "scenes", "no dialogue lines were assigned — check the audio track and transcript"))
    elif info["has_audio"]:
        measure_line_loudness(video_path, dialogue)
        tools_used.append("ffmpeg(line loudness)")

    index = {
        "version": "1.0",
        "source_index_id": source_index_id or _slug(video_path.stem),
        "provenance": {
            "kind": provenance_kind,
            "tools": tools_used,
            "transcript_origin": transcript_origin,
            "visual_review": "pending",
            "note": "Scenes, keyframes and dialogue come from tools; visual fields stay empty until the agent reviews the keyframes.",
        },
        "video": {
            "path": str(video_path.resolve()),
            "fps": fps,
            "duration_seconds": duration,
            "width": info["width"],
            "height": info["height"],
            "has_audio": info["has_audio"],
            "has_subtitles": info["has_subtitle_stream"],
        },
        "characters": [],
        "scenes": [
            {
                **scene,
                "visual_summary": "",
                "visual_review": "pending",
                "characters": [],
                "actions": [],
                "reactions": [],
                "keyframes": keyframes[scene["scene_id"]],
                "dialogue": dialogue[scene["scene_id"]],
            }
            for scene in scenes
        ],
    }

    index_path = output_dir / "anime_source_index.json"
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    packet_path = write_review_packet(index, output_dir / "review_packet.md")
    template_path = output_dir / "visual_annotations.template.json"
    template_path.write_text(json.dumps(annotations_template(index), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return BuildResult(index, index_path, packet_path, template_path, warnings)


# ── merge the agent's visual annotations ─────────────────────────────────


def apply_visual_annotations(index: dict, annotations: dict) -> tuple[dict, list[Issue]]:
    """Merge agent-written scene/line annotations into a copy of ``index``.

    All references are checked first; if any is wrong nothing is applied and
    the index is returned unchanged. Empty strings / null mean "not annotated".
    """
    issues: list[Issue] = []
    if annotations.get("source_index_id") != index.get("source_index_id"):
        issues.append(Issue("index_mismatch", "annotations.source_index_id",
                            f"annotations are for '{annotations.get('source_index_id')}', index is '{index.get('source_index_id')}'"))

    roster = {c["character_id"]: c for c in index.get("characters", [])}
    for i, char in enumerate(annotations.get("characters", [])):
        if not char.get("character_id") or not char.get("name_ja"):
            issues.append(Issue("missing_field", f"annotations.characters[{i}]", "character needs character_id and name_ja"))
        else:
            roster[char["character_id"]] = {**roster.get(char["character_id"], {}), **char}

    scenes = {s["scene_id"]: s for s in index["scenes"]}
    lines = {line["line_id"]: line for s in index["scenes"] for line in s["dialogue"]}
    for i, ann in enumerate(annotations.get("scenes", [])):
        if ann.get("scene_id") not in scenes:
            issues.append(Issue("unknown_scene", f"annotations.scenes[{i}].scene_id", f"scene '{ann.get('scene_id')}' is not in the index"))
        for cid in ann.get("characters") or []:
            if cid not in roster:
                issues.append(Issue("unknown_character", f"annotations.scenes[{i}].characters", f"'{cid}' is not in the character roster"))
    for i, ann in enumerate(annotations.get("lines", [])):
        if ann.get("line_id") not in lines:
            issues.append(Issue("unknown_line", f"annotations.lines[{i}].line_id", f"line '{ann.get('line_id')}' is not in the index"))
        speaker = ann.get("speaker")
        if speaker is not None and speaker not in roster:
            issues.append(Issue("unknown_character", f"annotations.lines[{i}].speaker", f"'{speaker}' is not in the character roster"))
    if issues:
        return index, issues

    merged = copy.deepcopy(index)
    m_scenes = {s["scene_id"]: s for s in merged["scenes"]}
    m_lines = {line["line_id"]: line for s in merged["scenes"] for line in s["dialogue"]}
    applied = False
    for ann in annotations.get("scenes", []):
        scene = m_scenes[ann["scene_id"]]
        if isinstance(ann.get("visual_summary"), str) and ann["visual_summary"].strip():
            scene["visual_summary"] = ann["visual_summary"].strip()
            scene["visual_review"] = "done"
            applied = True
        for key in ("characters", "actions", "reactions"):
            if ann.get(key):
                scene[key] = list(ann[key])
                applied = True
    for ann in annotations.get("lines", []):
        line = m_lines[ann["line_id"]]
        if ann.get("speaker") is not None:
            line["speaker"] = ann["speaker"]
            applied = True
        if isinstance(ann.get("text_vi"), str) and ann["text_vi"].strip():
            line["text_vi"] = ann["text_vi"].strip()
            applied = True
        if ann.get("unreliable"):
            line["low_confidence"] = True
            line.setdefault("asr_flags", []).append("agent_marked_unreliable")
            applied = True

    merged["characters"] = list(roster.values())
    done = sum(1 for s in merged["scenes"] if s.get("visual_review") == "done")
    merged["provenance"]["visual_review"] = (
        "complete" if done == len(merged["scenes"]) else "partial" if done else "pending"
    )
    if applied and "agent_vision" not in merged["provenance"].setdefault("tools", []):
        merged["provenance"]["tools"].append("agent_vision")
    return merged, []


# ── CLI ──────────────────────────────────────────────────────────────────


def _print_issues(issues: list[Issue], label: str) -> None:
    for issue in issues:
        print(f"{label:<8} {issue.code:<26} {issue.path}: {issue.message}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lib.anime_source_indexer", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="analyse a video into anime_source_index.json")
    b.add_argument("--video", required=True)
    b.add_argument("--out", required=True, help="output directory")
    b.add_argument("--transcript", help="transcriber JSON to use instead of running faster-whisper")
    b.add_argument("--model-size", default="small", help="faster-whisper model (tiny…large-v3)")
    b.add_argument("--source-index-id")
    b.add_argument("--scene-threshold", type=float)
    b.add_argument("--mock", action="store_true", help="mark the index as mock (test or synthetic input)")

    a = sub.add_parser("annotate", help="merge the agent's visual annotations")
    a.add_argument("--index", required=True)
    a.add_argument("--annotations", required=True)
    a.add_argument("--out", help="write here instead of overwriting --index")

    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            result = build_source_index(
                Path(args.video), Path(args.out),
                transcript_path=Path(args.transcript) if args.transcript else None,
                source_index_id=args.source_index_id, model_size=args.model_size,
                provenance_kind="mock" if args.mock else "analyzed", scene_threshold=args.scene_threshold,
            )
            n_lines = sum(len(s["dialogue"]) for s in result.index["scenes"])
            low = sum(1 for s in result.index["scenes"] for line in s["dialogue"] if line["low_confidence"])
            print(f"index:   {result.index_path}")
            print(f"packet:  {result.review_packet_path}")
            print(f"scenes:  {len(result.index['scenes'])} · lines: {n_lines} ({low} low confidence)")
            print("visual review: pending — read the keyframes in the packet and run `annotate`")
            _print_issues(result.warnings, "WARNING")
            return 0
        index = json.loads(Path(args.index).read_text(encoding="utf-8"))
        annotations = json.loads(Path(args.annotations).read_text(encoding="utf-8"))
        merged, issues = apply_visual_annotations(index, annotations)
        if issues:
            _print_issues(issues, "ERROR")
            return 1
        target = Path(args.out) if args.out else Path(args.index)
        target.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"merged → {target} (visual review: {merged['provenance']['visual_review']})")
        return 0
    except (IndexBuildError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR    {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
