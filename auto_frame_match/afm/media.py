"""FFmpeg/FFprobe: metadata, giải mã thumbnail toàn bộ khung kèm PTS, lấy khung lẻ."""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Callable

import numpy as np

from .config import TOOL_ROOT
from .log import get_logger

log = get_logger()

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _find_binary(name: str) -> str:
    exe = name + (".exe" if sys.platform == "win32" else "")
    for cand in (TOOL_ROOT / "bin" / exe, TOOL_ROOT / "ffmpeg" / "bin" / exe):
        if cand.exists():
            return str(cand)
    found = shutil.which(name)
    if not found:
        raise RuntimeError(
            f"Không tìm thấy {name}. Hãy cài FFmpeg và thêm vào PATH, "
            f"hoặc chép {exe} vào thư mục auto_frame_match/bin/"
        )
    return found


@functools.lru_cache(None)
def ffmpeg_bin() -> str:
    return _find_binary("ffmpeg")


@functools.lru_cache(None)
def ffprobe_bin() -> str:
    return _find_binary("ffprobe")


def run(cmd: list[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    log.debug("RUN %s", " ".join(cmd))
    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        creationflags=CREATE_NO_WINDOW,
    )
    if check and p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace")[-3000:] if p.stderr else ""
        raise RuntimeError(f"Lệnh thất bại ({p.returncode}): {' '.join(cmd[:6])}...\n{err}")
    return p


@functools.lru_cache(None)
def cuda_decode_available() -> bool:
    try:
        p = run([ffmpeg_bin(), "-hide_banner", "-hwaccels"], check=False)
        if b"cuda" not in p.stdout:
            return False
        # thử giải mã 1 khung thật bằng cuda
        p = run(
            [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-hwaccel", "cuda",
             "-f", "lavfi", "-i", "testsrc2=s=320x240:d=0.1", "-frames:v", "1", "-f", "null", "-"],
            check=False,
        )
        return p.returncode == 0
    except Exception:
        return False


@functools.lru_cache(None)
def nvenc_available() -> bool:
    try:
        p = run(
            [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "testsrc2=s=1280x720:d=0.2", "-c:v", "h264_nvenc", "-f", "null", "-"],
            check=False,
        )
        return p.returncode == 0
    except Exception:
        return False


def file_content_hash(path: str | os.PathLike) -> str:
    """Băm nội dung nhanh (kích thước + 3 khối 4MB). Không phụ thuộc tên/đường dẫn file."""
    p = Path(path)
    size = p.stat().st_size
    h = hashlib.sha1(str(size).encode())
    chunk = 4 * 1024 * 1024
    with p.open("rb") as f:
        for pos in (0, max(0, size // 2 - chunk // 2), max(0, size - chunk)):
            f.seek(pos)
            h.update(f.read(chunk))
    return h.hexdigest()[:20]


@dataclass
class MediaInfo:
    path: str
    width: int
    height: int
    fps: float
    fps_fraction: str
    time_base: str
    duration: float
    nb_frames_hint: int
    codec: str
    pix_fmt: str
    start_time: float
    video_start_time: float
    sar: str
    dar: str
    rotation: int
    audio_streams: list[dict] = field(default_factory=list)
    content_hash: str = ""

    @property
    def display_aspect(self) -> float:
        w, h = self.width, self.height
        if self.rotation in (90, 270):
            w, h = h, w
        try:
            num, den = (int(x) for x in self.sar.split(":"))
            if num > 0 and den > 0:
                return w * num / (h * den)
        except Exception:
            pass
        return w / h

    def pick_audio(self, lang: str = "jpn", index: int | None = None) -> dict | None:
        if not self.audio_streams:
            return None
        if index is not None:
            for a in self.audio_streams:
                if a["index"] == index:
                    return a
        for a in self.audio_streams:
            if a.get("language", "").lower() in (lang, lang[:2], "ja", "jpn", "japanese"):
                return a
        return self.audio_streams[0]

    def to_dict(self) -> dict:
        from dataclasses import asdict

        return asdict(self)


def probe(path: str | os.PathLike, with_hash: bool = True) -> MediaInfo:
    path = str(path)
    if not Path(path).exists():
        raise FileNotFoundError(f"Không tìm thấy file: {path}")
    p = run([ffprobe_bin(), "-v", "error", "-show_format", "-show_streams", "-of", "json", path])
    data = json.loads(p.stdout.decode("utf-8", "replace"))
    vs = [s for s in data.get("streams", []) if s.get("codec_type") == "video"
          and not s.get("disposition", {}).get("attached_pic")]
    if not vs:
        raise RuntimeError(f"File không có luồng video: {path}")
    v = vs[0]
    fr = v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1"
    try:
        fps_f = Fraction(fr)
    except (ZeroDivisionError, ValueError):
        fps_f = Fraction(0)
    if fps_f == 0:
        fps_f = Fraction(v.get("r_frame_rate", "25/1"))
    fmt = data.get("format", {})
    rotation = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = int(abs(int(sd["rotation"]))) % 360
    if "rotate" in v.get("tags", {}):
        rotation = int(v["tags"]["rotate"]) % 360
    audio = []
    for s in data.get("streams", []):
        if s.get("codec_type") == "audio":
            audio.append({
                "index": int(s["index"]),
                "codec": s.get("codec_name", ""),
                "channels": int(s.get("channels", 2)),
                "sample_rate": int(s.get("sample_rate", 48000)),
                "language": s.get("tags", {}).get("language", ""),
                "title": s.get("tags", {}).get("title", ""),
                "start_time": float(s.get("start_time", 0) or 0),
            })
    duration = float(v.get("duration") or fmt.get("duration") or 0)
    info = MediaInfo(
        path=path,
        width=int(v["width"]),
        height=int(v["height"]),
        fps=float(fps_f),
        fps_fraction=f"{fps_f.numerator}/{fps_f.denominator}",
        time_base=v.get("time_base", "1/1000"),
        duration=duration,
        nb_frames_hint=int(v.get("nb_frames") or round(duration * float(fps_f))),
        codec=v.get("codec_name", ""),
        pix_fmt=v.get("pix_fmt", ""),
        start_time=float(fmt.get("start_time", 0) or 0),
        video_start_time=float(v.get("start_time", 0) or 0),
        sar=v.get("sample_aspect_ratio", "1:1") or "1:1",
        dar=v.get("display_aspect_ratio", "") or "",
        rotation=rotation,
        audio_streams=audio,
    )
    if with_hash:
        info.content_hash = file_content_hash(path)
    return info


def thumb_size(aspect: float, area: int) -> tuple[int, int]:
    h = int(round((area / aspect) ** 0.5))
    w = int(round(h * aspect))
    return max(16, w - w % 2), max(16, h - h % 2)


_SHOWINFO_RE = re.compile(rb"n:\s*(\d+)\s+pts:\s*(-?\d+)\s+pts_time:\s*(-?[\d.eE+-]+)")


def decode_thumbs(
    path: str,
    size: tuple[int, int],
    out_file: str | os.PathLike,
    expected_frames: int,
    use_gpu: bool = True,
    progress: Callable[[int], None] | None = None,
    cancel: threading.Event | None = None,
) -> tuple[int, np.ndarray]:
    """Giải mã MỌI khung (không bỏ khung, không nhân khung) thành grayscale nhỏ.

    Ghi trực tiếp ra file .u8 (memmap). Trả về (số khung, mảng pts_time từng khung).
    """
    w, h = size
    frame_bytes = w * h
    hw = ["-hwaccel", "cuda"] if (use_gpu and cuda_decode_available()) else []
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "info", *hw,
        "-i", path, "-map", "0:v:0", "-an", "-sn", "-dn",
        "-vf", f"showinfo,scale={w}:{h}:flags=area,format=gray",
        "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
    ]
    log.debug("DECODE %s", " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=CREATE_NO_WINDOW, bufsize=frame_bytes * 16)
    pts: dict[int, float] = {}
    err_tail: list[bytes] = []

    def read_err():
        for line in proc.stderr:
            m = _SHOWINFO_RE.search(line)
            if m:
                pts[int(m.group(1))] = float(m.group(3))
            elif b"showinfo" not in line:
                err_tail.append(line)
                del err_tail[:-30]

    t = threading.Thread(target=read_err, daemon=True)
    t.start()
    n = 0
    with open(out_file, "wb") as fo:
        while True:
            if cancel is not None and cancel.is_set():
                proc.kill()
                raise InterruptedError("Đã hủy")
            buf = proc.stdout.read(frame_bytes * 64)
            if not buf:
                break
            while len(buf) % frame_bytes:
                more = proc.stdout.read(frame_bytes - len(buf) % frame_bytes)
                if not more:
                    break
                buf += more
            fo.write(buf[: len(buf) - len(buf) % frame_bytes])
            n += len(buf) // frame_bytes
            if progress:
                progress(n)
    proc.wait()
    t.join(timeout=10)
    if proc.returncode != 0:
        if hw:  # thử lại không dùng GPU
            log.warning("Giải mã GPU lỗi, chuyển sang CPU: %s", path)
            return decode_thumbs(path, size, out_file, expected_frames, False, progress, cancel)
        raise RuntimeError("Giải mã thất bại: " + b"".join(err_tail).decode("utf-8", "replace"))
    if len(pts) >= n and n > 0:
        arr = np.array([pts[i] for i in range(n)], dtype=np.float64)
    else:
        log.warning("Thiếu PTS từ showinfo (%d/%d) - dùng CFR suy ra", len(pts), n)
        info = probe(path, with_hash=False)
        arr = info.video_start_time + np.arange(n) / info.fps
    return n, arr


def grab_frames(path: str, times: list[float], width: int, gray: bool = True,
                start_time: float = 0.0) -> list[np.ndarray | None]:
    """Lấy các khung lẻ ở độ phân giải trung bình (seek chính xác theo pts_time)."""
    out: list[np.ndarray | None] = []
    for t in times:
        rel = max(0.0, t - start_time)
        cmd = [
            ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error",
            "-ss", f"{max(0.0, rel - 2.0):.6f}", "-copyts", "-i", path,
            "-vf", f"select='gte(t\\,{t - 0.0005:.6f})',scale={width}:-2:flags=area,"
                   f"format={'gray' if gray else 'bgr24'}",
            "-frames:v", "1", "-f", "image2pipe", "-vcodec", "rawvideo", "pipe:1",
        ]
        p = run(cmd, check=False)
        data = p.stdout
        if not data:
            out.append(None)
            continue
        ch = 1 if gray else 3
        # tính chiều cao từ kích thước dữ liệu
        h = len(data) // (width * ch)
        arr = np.frombuffer(data[: width * h * ch], np.uint8).reshape(h, width, ch) if ch == 3 else \
            np.frombuffer(data[: width * h], np.uint8).reshape(h, width)
        out.append(arr.copy())
    return out


def extract_audio_pcm(path: str, stream_index: int, start: float, duration: float,
                      rate: int = 16000, extra_filters: list[str] | None = None) -> np.ndarray:
    """Âm thanh mono float32 từ ``start`` (giây theo timestamp gốc của file)."""
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error",
        "-ss", f"{max(0.0, start - 1.0):.6f}", "-copyts", "-i", path,
        "-map", f"0:{stream_index}", "-af",
        ",".join([f"atrim=start={max(0.0, start):.6f}:duration={duration:.6f}", "asetpts=PTS-STARTPTS",
                  *(extra_filters or [])]),
        "-ac", "1", "-ar", str(rate), "-f", "f32le", "pipe:1",
    ]
    p = run(cmd, check=False)
    return np.frombuffer(p.stdout, np.float32).copy()
