"""Real-footage trial kit for the anime indexer (Windows-first, RTX GPUs).

Wraps the Phase 2 builder (``lib/anime_source_indexer.py``) for a person
testing their own episode:

- ``setup``  — install only the missing pieces (faster-whisper, jsonschema,
  CUDA 12 cuBLAS/cuDNN wheels when an NVIDIA GPU is present), check
  FFmpeg/CUDA/faster-whisper, and smoke-test Whisper on the chosen device;
- ``run``    — pick an MP4, cut a 30–60 s trial clip (or use the full file),
  build ``anime_source_index.json`` + keyframes + ``review_packet.md``, run
  automatic checks, write Excel-friendly manual check sheets and a report with
  per-stage timings;
- ``score``  — read the filled sheets and add dialogue accuracy / CER and
  scene false-cut figures to the report.

Steps that cannot run automatically (the agent's look at keyframes, a human
listening to the Japanese) are reported as ``PENDING``; ``overall`` never
becomes ``PASS`` while they are open.

Usage (from the repo root, inside ``.venv-anime``)::

    python -m lib.anime_trial setup
    python -m lib.anime_trial run --mode clip      # file picker opens
    python -m lib.anime_trial run --mode full --video anime_trial/video_vao/ep.mp4
    python -m lib.anime_trial score --run anime_trial/ket_qua/<run>
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import importlib.util
import itertools
import json
import os
import re
import shutil
import site
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

from lib.anime_recap import spoken_char_count
from lib.anime_source_indexer import (
    IndexBuildError,
    build_source_index,
    probe_video,
    snap_to_frame,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
TRIAL_ROOT = REPO_ROOT / "anime_trial"
INPUT_DIR = TRIAL_ROOT / "video_vao"
OUTPUT_DIR = TRIAL_ROOT / "ket_qua"

MIN_FFMPEG = (5, 1)  # -fps_mode (proxy) needs FFmpeg 5.1+
DEFAULT_CLIP_SECONDS = 45.0
SKIP_OPENING_SECONDS = 60.0
SSIM_FALSE_CUT = 0.92
LOW_CONFIDENCE_WARN_RATIO = 0.30
FAST_SPEECH_CHARS_PER_SECOND = 12.0
DIALOGUE_PASS_ACCURACY = 0.85
DIALOGUE_PASS_CER = 0.15
SCENE_PASS_FALSE_CUT_RATE = 0.10
SCENE_PASS_MISSED_RATE = 0.10

DIALOGUE_SHEET = "kiem_tra_loi_thoai.csv"
SCENE_SHEET = "kiem_tra_canh.csv"

STAGE_LABELS = {
    "prepare_input": "Chuẩn bị file vào",
    "clip": "Cắt đoạn thử",
    "probe": "Đọc thông số video",
    "proxy": "Tạo proxy 360p",
    "scene_detect": "Phát hiện cảnh",
    "keyframes": "Trích keyframe",
    "transcript": "Phiên âm tiếng Nhật (faster-whisper)",
    "dialogue": "Gán thoại vào cảnh",
    "loudness": "Đo độ to từng câu",
    "write": "Ghi index + review packet",
    "checks": "Kiểm tra tự động",
    "total": "TỔNG",
}


class TrialError(RuntimeError):
    pass


@dataclass
class Check:
    name: str
    status: str  # PASS / WARN / FAIL / PENDING / NOT_RUN
    detail: str


# ── environment ──────────────────────────────────────────────────────────


def _capture(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=False, **kwargs)


def _stream(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=False, **kwargs)


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _cuda_device_count() -> int:
    try:
        import ctranslate2

        return int(ctranslate2.get_cuda_device_count())
    except Exception:  # noqa: BLE001 — any import/driver failure means "no usable GPU"
        return 0


def parse_ffmpeg_version(banner: str) -> tuple[int, int] | None:
    match = re.search(r"version n?(\d+)\.(\d+)", banner)
    return (int(match.group(1)), int(match.group(2))) if match else None


def parse_nvidia_smi(text: str) -> list[dict]:
    gpus = []
    for row in text.strip().splitlines():
        parts = [p.strip() for p in row.split(",")]
        if len(parts) >= 3 and parts[0]:
            memory = re.match(r"(\d+)", parts[1])
            gpus.append({"name": parts[0], "memory_mib": int(memory.group(1)) if memory else 0, "driver": parts[2]})
    return gpus


def _query_gpus(runner: Callable) -> list[dict]:
    try:
        proc = runner(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"])
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return []
    return parse_nvidia_smi(proc.stdout or "") if proc.returncode == 0 else []


def check_environment(
    *,
    runner: Callable = _capture,
    module_available: Callable[[str], bool] = _module_available,
    cuda_device_count: Callable[[], int] = _cuda_device_count,
) -> list[Check]:
    checks: list[Check] = []
    py = sys.version_info
    checks.append(Check(
        "python", "PASS" if (3, 9) <= py[:2] <= (3, 12) else "WARN",
        f"Python {py.major}.{py.minor}.{py.micro}" + ("" if (3, 9) <= py[:2] <= (3, 12) else " — khuyên dùng Python 3.11"),
    ))

    try:
        banner = runner(["ffmpeg", "-version"]).stdout.splitlines()[0]
        version = parse_ffmpeg_version(banner)
        if version is None:
            checks.append(Check("ffmpeg", "WARN", f"không đọc được phiên bản: {banner[:60]}"))
        elif version < MIN_FFMPEG:
            checks.append(Check("ffmpeg", "FAIL", f"FFmpeg {version[0]}.{version[1]} quá cũ — cần ≥ 5.1"))
        else:
            checks.append(Check("ffmpeg", "PASS", f"FFmpeg {version[0]}.{version[1]}"))
        filters = runner(["ffmpeg", "-hide_banner", "-filters"]).stdout
        missing = [f for f in ("scdet", "ssim") if not re.search(rf"\b{f}\b", filters)]
        checks.append(Check("ffmpeg_filters", "FAIL" if missing else "PASS",
                            f"thiếu bộ lọc {', '.join(missing)}" if missing else "scdet, ssim"))
    except (FileNotFoundError, OSError, subprocess.SubprocessError, IndexError):
        checks.append(Check("ffmpeg", "FAIL", "không tìm thấy ffmpeg trong PATH"))
        checks.append(Check("ffmpeg_filters", "NOT_RUN", "chưa có ffmpeg"))
    try:
        runner(["ffprobe", "-version"])
        checks.append(Check("ffprobe", "PASS", "có"))
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        checks.append(Check("ffprobe", "FAIL", "không tìm thấy ffprobe trong PATH"))

    gpus = _query_gpus(runner)
    if gpus:
        g = gpus[0]
        checks.append(Check("gpu", "PASS", f"{g['name']} · {g['memory_mib']} MiB · driver {g['driver']}"))
    else:
        checks.append(Check("gpu", "WARN", "không thấy GPU NVIDIA (nvidia-smi) — phiên âm sẽ chạy CPU, chậm hơn nhiều"))

    has_fw = module_available("faster_whisper")
    checks.append(Check("faster_whisper", "PASS" if has_fw else "FAIL",
                        "đã cài" if has_fw else "chưa cài — chạy 1_CAI_DAT.bat"))
    if not has_fw:
        checks.append(Check("cuda_runtime", "NOT_RUN", "cần faster-whisper trước"))
    elif not gpus:
        checks.append(Check("cuda_runtime", "NOT_RUN", "không có GPU NVIDIA"))
    else:
        n = cuda_device_count()
        checks.append(Check("cuda_runtime", "PASS" if n > 0 else "WARN",
                            f"ctranslate2 thấy {n} GPU" if n > 0 else "ctranslate2 không thấy GPU — kiểm tra driver NVIDIA"))
    has_js = module_available("jsonschema")
    checks.append(Check("jsonschema", "PASS" if has_js else "WARN",
                        "đã cài" if has_js else "chưa cài — bỏ qua bước kiểm tra schema"))
    return checks


def pip_packages(*, has_gpu: bool) -> list[str]:
    packages = ["faster-whisper>=1.1.0", "jsonschema>=4.0"]
    if has_gpu:
        # ctranslate2 4.5+ (pulled by faster-whisper) runs on CUDA 12 + cuDNN 9.
        packages += ["nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*"]
    return packages


def install_packages(*, runner: Callable | None = None, gpu_packages: bool = True) -> dict:
    has_gpu = bool(_query_gpus(runner or _capture)) and gpu_packages
    packages = pip_packages(has_gpu=has_gpu)
    proc = (runner or _stream)([sys.executable, "-m", "pip", "install", *packages])
    return {"has_gpu": has_gpu, "packages": packages, "returncode": proc.returncode}


def register_windows_cuda_dlls(
    *,
    site_dirs: list[Path] | None = None,
    os_name: str = os.name,
    add_dll_directory: Callable[[str], Any] | None = None,
) -> list[Path]:
    """Expose pip-installed CUDA DLLs (nvidia/*/bin) to ctranslate2 on Windows."""
    if os_name != "nt":
        return []
    if site_dirs is None:
        site_dirs = [Path(p) for p in site.getsitepackages()] + [Path(site.getusersitepackages())]
    add = add_dll_directory or getattr(os, "add_dll_directory", None)
    found: list[Path] = []
    for base in site_dirs:
        for bin_dir in sorted((Path(base) / "nvidia").glob("*/bin")):
            if bin_dir.is_dir():
                if add is not None:
                    add(str(bin_dir))
                os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
                found.append(bin_dir)
    return found


def smoke_test(model_size: str, device: str, *, work_dir: Path | None = None) -> dict:
    """Load Whisper on ``device`` and transcribe 1 s of silence (downloads the model once)."""
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(work_dir or tmp) / "silence_1s.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "1", str(wav)],
                       check=True, timeout=60)
        compute_type = "float16" if device == "cuda" else "int8"
        start = time.perf_counter()
        try:
            from faster_whisper import WhisperModel

            model = WhisperModel(model_size, device=device, compute_type=compute_type)
            segments, _ = model.transcribe(str(wav), language="ja")
            list(segments)
        except Exception as exc:  # noqa: BLE001 — report any load/CUDA failure, do not crash setup
            return {"ok": False, "device": device, "model": model_size, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "device": device, "model": model_size, "seconds": round(time.perf_counter() - start, 1)}


# ── input preparation ────────────────────────────────────────────────────


def auto_clip_start(duration: float) -> float:
    """Skip the first minute (title/opening) on full-length videos."""
    return SKIP_OPENING_SECONDS if duration >= 3 * SKIP_OPENING_SECONDS else 0.0


def prepare_input(video: Path, work_dir: Path) -> Path:
    """Copy to an ASCII name when the path would break ffmpeg's lavfi parser."""
    text = str(video)
    if text.isascii() and not any(c in text for c in "'\"%!^&;,[]"):
        return video
    work_dir.mkdir(parents=True, exist_ok=True)
    target = work_dir / f"input{video.suffix.lower() or '.mp4'}"
    shutil.copy2(video, target)
    return target


def make_clip(video: Path, out_path: Path, *, start: float, seconds: float) -> dict:
    """Frame-accurate re-encoded trial clip that starts at t=0."""
    info = probe_video(video)
    fps, duration = info["fps"], info["duration_seconds"]
    if start >= duration - 1.0:
        raise TrialError(f"clip start {start:.1f}s is past the end of the video ({duration:.1f}s)")
    start = snap_to_frame(start, fps)
    seconds = min(seconds, duration - start)
    rate = Fraction(fps).limit_denominator(1001)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    proc = _capture([
        "ffmpeg", "-v", "error", "-y", "-ss", f"{start:.6f}", "-i", str(video), "-t", f"{seconds:.6f}",
        "-map", "0:v:0", "-map", "0:a:0?", "-r", f"{rate.numerator}/{rate.denominator}", "-fps_mode", "cfr",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", str(out_path),
    ])
    if proc.returncode != 0:
        raise TrialError(f"ffmpeg could not cut the clip: {proc.stderr.strip()[:300]}")
    return {"source": str(video), "path": str(out_path), "start_offset_seconds": start, "seconds": round(seconds, 3)}


# ── automatic checks ─────────────────────────────────────────────────────


def frame_similarity(a: Path, b: Path) -> float:
    """SSIM (0–1) of two keyframes via ffmpeg, on planar RGB.

    On YUV the luma plane dominates and equal-brightness colour changes (a real
    cut) score ~0.93 — the same blind spot as the luma-only scene filter.
    """
    proc = _capture([
        "ffmpeg", "-v", "info", "-hide_banner", "-i", str(a), "-i", str(b),
        "-lavfi", "[0:v]scale=320:180,format=gbrp[x];[1:v]scale=320:180,format=gbrp[y];[x][y]ssim",
        "-f", "null", "-",
    ])
    match = re.search(r"All:([0-9.]+)", proc.stderr or "")
    return float(match.group(1)) if match else 0.0


def dialogue_stats(index: dict) -> dict:
    lines = [line for scene in index["scenes"] for line in scene.get("dialogue", [])]
    confidences = [line["asr_confidence"] for line in lines if isinstance(line.get("asr_confidence"), (int, float))]
    fast = 0
    for line in lines:
        span = line["end_seconds"] - line["start_seconds"]
        if span > 0 and spoken_char_count(line.get("text_ja", "")) / span > FAST_SPEECH_CHARS_PER_SECOND:
            fast += 1
    return {
        "line_count": len(lines),
        "mean_confidence": round(sum(confidences) / len(confidences), 3) if confidences else None,
        "low_confidence_ratio": round(sum(1 for line in lines if line.get("low_confidence")) / len(lines), 3) if lines else 0.0,
        "hallucinations": sum(1 for line in lines if "known_hallucination" in line.get("asr_flags", [])),
        "split_at_cut": sum(1 for line in lines if "split_at_scene_cut" in line.get("asr_flags", [])),
        "too_fast_lines": fast,
    }


def check_index(
    index: dict,
    out_dir: Path,
    *,
    ssim_threshold: float = SSIM_FALSE_CUT,
    similarity: Callable[[Path, Path], float] = frame_similarity,
) -> tuple[list[Check], dict]:
    fps, duration = index["video"]["fps"], index["video"]["duration_seconds"]
    scenes = index["scenes"]
    frame = 1.0 / fps
    checks: list[Check] = []

    schema_path = REPO_ROOT / "schemas" / "artifacts" / "anime_source_index.schema.json"
    if _module_available("jsonschema"):
        import jsonschema

        try:
            jsonschema.validate(index, json.loads(schema_path.read_text(encoding="utf-8")))
            checks.append(Check("schema", "PASS", "anime_source_index.schema.json"))
        except jsonschema.ValidationError as exc:
            checks.append(Check("schema", "FAIL", exc.message[:200]))
    else:
        checks.append(Check("schema", "NOT_RUN", "jsonschema chưa cài"))

    gaps = []
    if scenes and abs(scenes[0]["start_seconds"]) > 1e-6:
        gaps.append(f"{scenes[0]['scene_id']} không bắt đầu từ 0")
    for a, b in itertools.pairwise(scenes):
        if abs(a["end_seconds"] - b["start_seconds"]) > 1e-6:
            gaps.append(f"{a['scene_id']}→{b['scene_id']}")
    if any(s["end_seconds"] <= s["start_seconds"] for s in scenes):
        gaps.append("cảnh có độ dài ≤ 0")
    if scenes and abs(scenes[-1]["end_seconds"] - duration) > frame:
        gaps.append(f"cảnh cuối kết thúc {scenes[-1]['end_seconds']:.3f}s ≠ video {duration:.3f}s")
    checks.append(Check("scenes_contiguous", "FAIL" if gaps else "PASS", "; ".join(gaps[:5]) or f"{len(scenes)} cảnh liền mạch"))

    off_grid = [s["scene_id"] for s in scenes[1:] if abs(s["start_seconds"] * fps - round(s["start_seconds"] * fps)) > 0.01]
    checks.append(Check("frame_grid", "FAIL" if off_grid else "PASS",
                        f"lệch lưới frame: {', '.join(off_grid[:5])}" if off_grid else f"mọi điểm cắt khớp frame @ {fps} fps"))

    bad_kf = []
    for s in scenes:
        for kf in s.get("keyframes", []):
            if not Path(kf["path"]).is_file() or not (s["start_seconds"] <= kf["timestamp_seconds"] < s["end_seconds"]):
                bad_kf.append(f"{s['scene_id']}@{kf['timestamp_seconds']}")
    no_kf = [s["scene_id"] for s in scenes if not s.get("keyframes")]
    checks.append(Check("keyframes_exist", "FAIL" if bad_kf or no_kf else "PASS",
                        "; ".join(bad_kf[:5] + [f"{x} không có keyframe" for x in no_kf[:5]])
                        or f"{sum(len(s.get('keyframes', [])) for s in scenes)} keyframe"))

    stray = []
    for s in scenes:
        prev = -1.0
        for line in s.get("dialogue", []):
            if not (s["start_seconds"] <= line["start_seconds"] < line["end_seconds"] <= s["end_seconds"]) or line["start_seconds"] < prev:
                stray.append(line["line_id"])
            prev = line["start_seconds"]
    checks.append(Check("lines_inside_scenes", "FAIL" if stray else "PASS",
                        f"thoại nằm ngoài cảnh / sai thứ tự: {', '.join(stray[:5])}" if stray else "mọi câu thoại nằm trong cảnh"))

    ids = [s["scene_id"] for s in scenes] + [line["line_id"] for s in scenes for line in s.get("dialogue", [])]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    checks.append(Check("unique_ids", "FAIL" if dupes else "PASS", f"trùng: {', '.join(dupes[:5])}" if dupes else "không trùng"))

    d = dialogue_stats(index)
    if d["line_count"] == 0:
        checks.append(Check("dialogue_auto", "WARN", "không có câu thoại nào — kiểm tra âm thanh / model"))
    else:
        status = "WARN" if d["low_confidence_ratio"] > LOW_CONFIDENCE_WARN_RATIO or d["hallucinations"] else "PASS"
        checks.append(Check("dialogue_auto", status,
                            f"{d['line_count']} câu · độ tin cậy TB {d['mean_confidence']} · "
                            f"{d['low_confidence_ratio']:.0%} câu tin cậy thấp · {d['hallucinations']} câu nghi ảo giác · "
                            f"{d['too_fast_lines']} câu nói nhanh bất thường (tự động, chưa phải độ chính xác thật)"))

    boundary_ssim: dict[str, float] = {}
    suspected: list[str] = []
    for a, b in itertools.pairwise(scenes):
        if a.get("keyframes") and b.get("keyframes"):
            value = similarity(Path(a["keyframes"][-1]["path"]), Path(b["keyframes"][0]["path"]))
            boundary_ssim[b["scene_id"]] = round(value, 3)
            if value >= ssim_threshold:
                suspected.append(b["scene_id"])
    lengths = [s["end_seconds"] - s["start_seconds"] for s in scenes]
    stats = {
        "scene_count": len(scenes),
        "avg_scene_seconds": round(sum(lengths) / len(lengths), 2) if lengths else 0.0,
        "cuts_per_minute": round((len(scenes) - 1) / (duration / 60), 2) if duration else 0.0,
        "short_scenes_under_1s": sum(1 for x in lengths if x < 1.0),
        "suspected_false_cuts": suspected,
        "boundary_ssim": boundary_ssim,
        **d,
    }
    checks.append(Check("scene_auto", "WARN" if suspected else "PASS",
                        f"{len(scenes)} cảnh · TB {stats['avg_scene_seconds']}s/cảnh · {stats['cuts_per_minute']} lần cắt/phút · "
                        f"{len(suspected)} điểm cắt nghi nhầm (SSIM ≥ {ssim_threshold})"))
    return checks, stats


# ── manual sheets and scoring ────────────────────────────────────────────


def write_check_sheets(index: dict, out_dir: Path, *, suspected: list[str] | tuple = ()) -> tuple[Path, Path]:
    """Excel-friendly (UTF-8 BOM) sheets for the human listening / watching pass."""
    out_dir = Path(out_dir)
    dialogue_csv, scene_csv = out_dir / DIALOGUE_SHEET, out_dir / SCENE_SHEET
    with dialogue_csv.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["line_id", "scene_id", "bat_dau", "ket_thuc", "do_tin_cay", "canh_bao", "text_ja",
                         "ket_qua", "text_ja_dung", "ghi_chu"])
        for scene in index["scenes"]:
            for line in scene.get("dialogue", []):
                writer.writerow([line["line_id"], scene["scene_id"], f"{line['start_seconds']:.3f}", f"{line['end_seconds']:.3f}",
                                 line.get("asr_confidence", ""), " ".join(line.get("asr_flags", [])), line["text_ja"], "", "", ""])
    with scene_csv.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["scene_id", "bat_dau", "ket_thuc", "do_dai", "keyframes", "nghi_cat_nham", "ket_qua", "sot_canh", "ghi_chu"])
        for scene in index["scenes"]:
            names = " ".join(Path(k["path"]).name for k in scene.get("keyframes", []))
            writer.writerow([scene["scene_id"], f"{scene['start_seconds']:.3f}", f"{scene['end_seconds']:.3f}",
                             f"{scene['end_seconds'] - scene['start_seconds']:.2f}", names,
                             "CO" if scene["scene_id"] in suspected else "", "", "", ""])
    return dialogue_csv, scene_csv


def _norm_answer(value: str) -> str:
    value = (value or "").strip().replace("Đ", "D").replace("đ", "d")
    value = "".join(c for c in unicodedata.normalize("NFD", value) if unicodedata.category(c) != "Mn")
    return re.sub(r"[\s-]+", "_", value.upper())


def character_error_rate(hypothesis: str, reference: str) -> float:
    prev = list(range(len(reference) + 1))
    for i, h in enumerate(hypothesis, 1):
        cur = [i]
        for j, r in enumerate(reference, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (h != r)))
        prev = cur
    return prev[-1] / max(1, len(reference))


def _read_sheet(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def score_run(run_dir: Path) -> dict:
    run_dir = Path(run_dir)
    rows = _read_sheet(run_dir / DIALOGUE_SHEET)
    answers = [_norm_answer(r.get("ket_qua", "")) for r in rows]
    correct = answers.count("DUNG")
    wrong = answers.count("SAI")
    filled = sum(1 for a in answers if a in ("DUNG", "SAI", "BO_QUA"))
    edits = ref_chars = 0
    for row, answer in zip(rows, answers):
        if answer == "DUNG":
            ref_chars += len(row["text_ja"])
        elif answer == "SAI" and row.get("text_ja_dung", "").strip():
            reference = row["text_ja_dung"].strip()
            edits += round(character_error_rate(row["text_ja"], reference) * len(reference))
            ref_chars += len(reference)
    reviewed = correct + wrong
    dialogue = {
        "lines": len(rows), "reviewed": reviewed, "skipped": answers.count("BO_QUA"),
        "accuracy": round(correct / reviewed, 3) if reviewed else None,
        "cer": round(edits / ref_chars, 3) if ref_chars else None,
        "complete": bool(rows) and filled == len(rows),
    }
    dialogue["status"] = _score_status(filled, dialogue["complete"],
                                       dialogue["accuracy"] is not None and dialogue["accuracy"] >= DIALOGUE_PASS_ACCURACY
                                       and (dialogue["cer"] is None or dialogue["cer"] <= DIALOGUE_PASS_CER))

    scene_rows = _read_sheet(run_dir / SCENE_SHEET)
    scene_answers = [_norm_answer(r.get("ket_qua", "")) for r in scene_rows]
    false_cuts = scene_answers.count("CAT_NHAM")
    judged = false_cuts + scene_answers.count("DUNG")
    missed = sum(int(r["sot_canh"]) for r in scene_rows if (r.get("sot_canh") or "").strip().isdigit())
    scenes = {
        "scenes": len(scene_rows), "reviewed": judged, "false_cuts": false_cuts, "missed_cuts": missed,
        "false_cut_rate": round(false_cuts / judged, 3) if judged else None,
        "complete": bool(scene_rows) and judged == len(scene_rows),
    }
    scenes["status"] = _score_status(judged, scenes["complete"],
                                     scenes["false_cut_rate"] is not None and scenes["false_cut_rate"] <= SCENE_PASS_FALSE_CUT_RATE
                                     and missed <= SCENE_PASS_MISSED_RATE * max(1, len(scene_rows)))
    score = {"dialogue": dialogue, "scenes": scenes}

    report_path = run_dir / "trial_report.json"
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["score"] = score
        for step in report.get("steps", []):
            if step["name"] == "manual_dialogue_check":
                step["status"] = dialogue["status"]
            elif step["name"] == "manual_scene_check":
                step["status"] = scenes["status"]
        report["overall"] = _overall(report)
        _write_report(run_dir, report)
    return score


def _score_status(filled: int, complete: bool, passed: bool) -> str:
    if not filled:
        return "PENDING"
    if not complete:
        return "PARTIAL"
    return "PASS" if passed else "WARN"


# ── report ───────────────────────────────────────────────────────────────


def _overall(report: dict) -> str:
    if report.get("error") or any(c["status"] == "FAIL" for c in report.get("checks", [])):
        return "FAIL"
    if any(s["status"] in ("PENDING", "PARTIAL", "NOT_RUN") for s in report.get("steps", [])):
        return "PENDING"
    return "PASS"


def render_markdown(report: dict) -> str:
    out = [f"# Báo cáo chạy thử — {Path(report.get('video', '?')).name} ({report.get('mode')})", ""]
    overall = report.get("overall")
    banner = {
        "FAIL": "❌ THẤT BẠI — xem lỗi / kiểm tra FAIL bên dưới",
        "PENDING": "⏳ CHƯA HOÀN THÀNH — còn bước PENDING phải làm thủ công / bằng AI (xem mục 4)",
        "PASS": "✅ Đã qua mọi bước",
    }.get(overall, overall)
    out += [f"**Trạng thái chung: {overall}** — {banner}", ""]
    if report.get("error"):
        out += [f"**Lỗi:** `{report['error']}`", ""]
    out += ["## 1. Thông tin", ""]
    info = report.get("video_info") or {}
    out.append(f"- Video gốc: `{report.get('video')}`")
    if report.get("clip"):
        c = report["clip"]
        out.append(f"- Đoạn thử: {c['seconds']:.1f}s bắt đầu từ {c['start_offset_seconds']:.3f}s của video gốc "
                   f"— **mọi timestamp trong kết quả tính từ đầu đoạn thử; cộng {c['start_offset_seconds']:.3f}s để ra thời điểm trong phim**")
    if info:
        out.append(f"- {info.get('fps')} fps · {info.get('duration_seconds')} s · {info.get('width')}x{info.get('height')}")
    asr = report.get("asr") or {}
    if asr.get("source"):
        out.append("- Phiên âm: dùng transcript có sẵn (--transcript) — faster-whisper KHÔNG chạy trong lần này")
    elif asr:
        out.append(f"- Phiên âm: model `{asr.get('model_size')}` · thiết bị `{asr.get('device')}` ({asr.get('compute_type')})"
                   + (f" · ⚠ GPU lỗi, đã chuyển CPU: {asr['gpu_fallback_reason']}" if asr.get("gpu_fallback_reason") else ""))
    out.append(f"- Thư mục kết quả: `{report.get('run_dir')}`")
    out += ["", "## 2. Thời gian xử lý", "", "| Công đoạn | Giây | % |", "|---|---:|---:|"]
    timings = report.get("timings", {})
    total = timings.get("total") or sum(timings.values()) or 1.0
    for key, value in timings.items():
        if key != "total":
            out.append(f"| {STAGE_LABELS.get(key, key)} | {value:.2f} | {value / total:.0%} |")
    out.append(f"| **{STAGE_LABELS['total']}** | **{total:.2f}** | 100% |")
    out += ["", "## 3. Kiểm tra tự động", "", "| Kiểm tra | Kết quả | Chi tiết |", "|---|---|---|"]
    for c in report.get("checks", []):
        out.append(f"| {c['name']} | {c['status']} | {c['detail']} |")
    for w in report.get("warnings", []):
        out.append(f"| cảnh báo: {w['code']} | WARN | {w['message']} |")
    out += ["", "## 4. Các bước CHƯA tự động hoàn thành", ""]
    for s in report.get("steps", []):
        out.append(f"- **{s['status']}** — {s.get('label', s['name'])}: {s.get('detail', '')}")
    if report.get("score"):
        d, sc = report["score"]["dialogue"], report["score"]["scenes"]
        out += ["", "## 5. Kết quả chấm thủ công", "",
                f"- Lời thoại: {d['reviewed']}/{d['lines']} câu đã chấm · đúng {d['accuracy']} · CER {d['cer']} · {d['status']}",
                (f"- Cảnh: {sc['reviewed']}/{sc['scenes']} cảnh đã chấm · cắt nhầm {sc['false_cuts']} "
                 f"(tỉ lệ {sc['false_cut_rate']}) · bỏ sót {sc['missed_cuts']} · {sc['status']}")]
    return "\n".join(out) + "\n"


def _write_report(run_dir: Path, report: dict) -> None:
    (run_dir / "trial_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (run_dir / "trial_report.md").write_text(render_markdown(report), encoding="utf-8")


def _pending_steps(mode: str) -> list[dict]:
    steps = [
        {"name": "ai_visual_review", "status": "PENDING", "label": "Phân tích hình ảnh bằng AI",
         "detail": "CHƯA CHẠY — cần Claude mở các keyframe trong review_packet.md, điền visual_annotations.template.json "
                   "rồi chạy `python -m lib.anime_source_indexer annotate`. Công cụ không tự hiểu nhân vật/hành động."},
        {"name": "manual_dialogue_check", "status": "PENDING", "label": "Nghe kiểm tra lời thoại tiếng Nhật",
         "detail": f"CHƯA LÀM — mở {DIALOGUE_SHEET}, nghe từng câu, ghi DUNG/SAI/BO_QUA (sửa câu đúng vào text_ja_dung), "
                   "rồi chạy 4_CHAM_DIEM.bat."},
        {"name": "manual_scene_check", "status": "PENDING", "label": "Xem kiểm tra điểm cắt cảnh",
         "detail": f"CHƯA LÀM — mở {SCENE_SHEET}, xem keyframe, ghi DUNG/CAT_NHAM, số cảnh bị sót vào sot_canh, rồi chạy 4_CHAM_DIEM.bat."},
    ]
    if mode == "clip":
        steps.append({"name": "full_episode", "status": "NOT_RUN", "label": "Chạy toàn bộ tập phim",
                      "detail": "CHƯA CHẠY — khi đoạn thử ổn, chạy 3_CHAY_TOAN_BO.bat."})
    return steps


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()).strip("-").lower() or "video"


@contextlib.contextmanager
def _stage(timings: dict, name: str):
    start = time.perf_counter()
    try:
        yield
    finally:
        timings[name] = round(timings.get(name, 0.0) + time.perf_counter() - start, 3)


def _asr_info(work_dir: Path) -> dict:
    for path in sorted(work_dir.glob("*_transcript.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        return {k: data.get(k) for k in ("model_size", "device", "compute_type", "gpu_fallback_reason", "language")}
    return {}


def run_trial(
    video: Path,
    out_root: Path = OUTPUT_DIR,
    *,
    mode: str = "clip",
    clip_start: float | None = None,
    clip_seconds: float = DEFAULT_CLIP_SECONDS,
    model_size: str = "large-v3",
    device: str = "auto",
    transcript: Path | None = None,
    provenance_kind: str = "analyzed",
) -> dict:
    if mode not in ("clip", "full"):
        raise TrialError("mode must be 'clip' or 'full'")
    video = Path(video)
    if not video.is_file():
        raise TrialError(f"không tìm thấy video: {video}")
    started = time.perf_counter()
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    run_dir = Path(out_root) / f"{_slug(video.stem)}_{mode}_{stamp}"
    work_dir = run_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    timings: dict[str, float] = {}
    report: dict[str, Any] = {"run_dir": str(run_dir), "mode": mode, "video": str(video), "clip": None,
                              "timings": timings, "checks": [], "warnings": [], "steps": _pending_steps(mode), "error": None}
    try:
        with _stage(timings, "prepare_input"):
            source = prepare_input(video, work_dir)
        analysed = source
        if mode == "clip":
            with _stage(timings, "clip"):
                duration = probe_video(source)["duration_seconds"]
                start = auto_clip_start(duration) if clip_start is None else clip_start
                report["clip"] = make_clip(source, work_dir / "clip.mp4", start=start, seconds=clip_seconds)
            analysed = Path(report["clip"]["path"])
        result = build_source_index(
            analysed, run_dir, transcript_path=transcript, source_index_id=f"{_slug(video.stem)}-{mode}",
            model_size=model_size, provenance_kind=provenance_kind, asr_device=device, timings=timings,
        )
        index = result.index
        report["video_info"] = {k: index["video"].get(k) for k in ("fps", "duration_seconds", "width", "height")}
        report["warnings"] = [asdict(w) for w in result.warnings]
        report["asr"] = _asr_info(work_dir) or {"source": "transcript file" if transcript else "unknown"}
        with _stage(timings, "checks"):
            checks, stats = check_index(index, run_dir)
            write_check_sheets(index, run_dir, suspected=stats["suspected_false_cuts"])
        report["checks"] = [asdict(c) for c in checks]
        report["stats"] = stats
    except (IndexBuildError, TrialError, OSError) as exc:
        report["error"] = str(exc)
    timings["total"] = round(time.perf_counter() - started, 3)
    report["overall"] = _overall(report)
    _write_report(run_dir, report)
    return report


# ── CLI ──────────────────────────────────────────────────────────────────


def pick_video(initial_dir: Path) -> Path | None:
    """Windows file dialog (tkinter); falls back to typing a path."""
    try:
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askopenfilename(
            title="Chọn video anime (MP4)", initialdir=str(initial_dir),
            filetypes=[("Video", "*.mp4 *.mkv *.mov *.m4v"), ("Tất cả", "*.*")],
        )
        root.destroy()
        return Path(chosen) if chosen else None
    except Exception:  # noqa: BLE001 — no tkinter / no display: fall back to typing a path
        typed = input("Nhập đường dẫn video (Enter để huỷ): ").strip().strip('"')
        return Path(typed) if typed else None


def _print_checks(checks: list[Check]) -> None:
    for c in checks:
        print(f"  [{c.status:<7}] {c.name:<15} {c.detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lib.anime_trial", description="Anime real-footage trial kit")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("setup", help="cài thư viện còn thiếu + kiểm tra môi trường")
    s.add_argument("--model", default="large-v3")
    s.add_argument("--no-gpu-packages", action="store_true")
    s.add_argument("--skip-model-download", action="store_true")
    sub.add_parser("check", help="chỉ kiểm tra môi trường")
    r = sub.add_parser("run", help="chạy thử trên một video")
    r.add_argument("--video")
    r.add_argument("--mode", choices=["clip", "full"], default="clip")
    r.add_argument("--clip-start", type=float, help="giây bắt đầu đoạn thử (mặc định: 60 nếu video dài ≥ 3 phút, ngược lại 0)")
    r.add_argument("--clip-seconds", type=float, default=DEFAULT_CLIP_SECONDS)
    r.add_argument("--model-size", default="large-v3")
    r.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    r.add_argument("--transcript", help="transcript JSON có sẵn (bỏ qua faster-whisper)")
    r.add_argument("--out", default=str(OUTPUT_DIR))
    sc = sub.add_parser("score", help="chấm điểm từ 2 file CSV đã điền")
    sc.add_argument("--run", help="thư mục kết quả (mặc định: lần chạy mới nhất)")
    args = parser.parse_args(argv)

    if args.command in ("setup", "check", "run"):
        register_windows_cuda_dlls()

    if args.command == "setup":
        plan = install_packages(gpu_packages=not args.no_gpu_packages)
        print(f"pip install {' '.join(plan['packages'])} → mã thoát {plan['returncode']}")
        importlib.invalidate_caches()
        register_windows_cuda_dlls()
        checks = check_environment()
        _print_checks(checks)
        if not args.skip_model_download and _module_available("faster_whisper"):
            device = "cuda" if plan["has_gpu"] else "cpu"
            print(f"Tải model {args.model} (lần đầu có thể vài GB) và thử chạy trên {device}…")
            result = smoke_test(args.model, device)
            if not result["ok"] and device == "cuda":
                print(f"  GPU lỗi: {result['error']}\n  Thử lại trên CPU…")
                result = smoke_test(args.model, "cpu")
            print(f"  smoke test: {'OK' if result['ok'] else 'LỖI'} · {result}")
            if not result["ok"]:
                return 1
        return 1 if plan["returncode"] or any(c.status == "FAIL" for c in checks) else 0

    if args.command == "check":
        checks = check_environment()
        _print_checks(checks)
        return 1 if any(c.status == "FAIL" for c in checks) else 0

    if args.command == "run":
        INPUT_DIR.mkdir(parents=True, exist_ok=True)
        video = Path(args.video) if args.video else pick_video(INPUT_DIR)
        if video is None:
            print("Chưa chọn video — dừng.")
            return 2
        print(f"Đang phân tích ({args.mode}) {video} … có thể mất vài phút.")
        try:
            report = run_trial(video, Path(args.out), mode=args.mode, clip_start=args.clip_start,
                               clip_seconds=args.clip_seconds, model_size=args.model_size, device=args.device,
                               transcript=Path(args.transcript) if args.transcript else None)
        except TrialError as exc:
            print(f"LỖI: {exc}")
            return 1
        print(f"Trạng thái: {report['overall']}" + (f" — {report['error']}" if report.get("error") else ""))
        print(f"Báo cáo: {Path(report['run_dir']) / 'trial_report.md'}")
        return 1 if report["overall"] == "FAIL" else 0

    run_dir = Path(args.run) if args.run else max(OUTPUT_DIR.glob("*_*_*"), key=lambda p: p.stat().st_mtime, default=None)
    if run_dir is None or not run_dir.is_dir():
        print("Không tìm thấy thư mục kết quả để chấm.")
        return 2
    score = score_run(run_dir)
    print(json.dumps(score, ensure_ascii=False, indent=2))
    print(f"Đã cập nhật {run_dir / 'trial_report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
