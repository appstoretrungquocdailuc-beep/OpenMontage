"""Bước 9-10: cắt ghép từ nguồn chất lượng cao, xuất 2K, âm thanh tiếng Nhật theo source_id/source_pts.

Thiết kế chính xác từng khung:
* Mỗi đoạn: giải mã đúng dải khung nguồn (trim theo PTS gốc, -copyts), crop/lật/scale lanczos lên 2K,
  Python chọn ĐÚNG khung nguồn cho từng khung xuất (theo mô hình tuyến tính của bảng cắt), mã hóa.
* Âm thanh: trích PCM từ nguồn theo source_pts (+ độ trễ riêng từng nguồn), atempo nếu đổi tốc độ,
  ghép theo SỐ MẪU tích lũy -> không lệch hình/tiếng tại điểm nối.
* Mỗi đoạn là một file trung gian -> chạy lại sẽ bỏ qua đoạn đã xong (tiếp tục khi gián đoạn).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import wave
from fractions import Fraction
from pathlib import Path

import numpy as np

from .config import RenderConfig
from .log import get_logger
from .media import CREATE_NO_WINDOW, ffmpeg_bin, ffprobe_bin, nvenc_available, run

log = get_logger()

RENDER_VERSION = 4  # tăng khi đổi cách chọn khung/âm thanh -> đoạn trung gian cũ bị dựng lại


def packet_pts(path: str) -> np.ndarray:
    """PTS (giây) mọi khung theo thứ tự hiển thị - đọc packet, không cần giải mã."""
    p = run([ffprobe_bin(), "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time",
             "-of", "csv=p=0", path])
    vals = []
    for line in p.stdout.decode().split():
        line = line.strip().strip(",")
        if line and line != "N/A":
            try:
                vals.append(float(line))
            except ValueError:
                pass
    return np.sort(np.array(vals))


def _even(x: float) -> int:
    return max(2, int(round(x / 2.0)) * 2)


def output_size(aspect: float, height: int) -> tuple[int, int]:
    """1440p: khung ngang -> cao 1440; khung dọc -> rộng 1440 (giữ đúng tỉ lệ)."""
    if aspect >= 1.0:
        return _even(height * aspect), height
    return height, _even(height / aspect)


def fit_crop(crop, aspect_px: float, src_w: int, src_h: int):
    """Chỉnh vùng crop (chuẩn hóa) về đúng tỉ lệ khung xuất, giữ tâm, không vượt khung nguồn."""
    x, y, w, h = crop
    cx, cy = x + w / 2, y + h / 2
    wp, hp = w * src_w, h * src_h
    if wp / hp > aspect_px:
        wp = hp * aspect_px
    else:
        hp = wp / aspect_px
    w2, h2 = min(1.0, wp / src_w), min(1.0, hp / src_h)
    x2 = min(max(cx - w2 / 2, 0.0), 1 - w2)
    y2 = min(max(cy - h2 / 2, 0.0), 1 - h2)
    return x2, y2, w2, h2


class RenderPlan:
    """Kế hoạch dựng V2: danh sách RUN phủ MỌI khung edit; khung xuất k = khung edit k (cùng FPS)."""

    def __init__(self, segments: list[dict], sources: dict, edit_meta: dict, rcfg: RenderConfig):
        self.rcfg = rcfg
        self.sources = sources
        self.edit_meta = edit_meta
        self.segments = segments  # = runs
        for s in segments:
            s.setdefault("render", True)
            s.setdefault("kind", "source")
        if not segments:
            raise RuntimeError("Không có khung nào để dựng")
        self.fps = Fraction(edit_meta.get("fps_fraction") or "30/1")
        self.fps_mode = "edit"
        self.edit_pts = np.asarray(edit_meta.get("_pts") or [], np.float64)
        # tỉ lệ khung xuất: trung vị (theo số khung) tỉ lệ vùng nguồn hiển thị trong bản edit
        aspects, weights = [], []
        for s in segments:
            if s["kind"] != "source":
                continue
            src = sources[s["source_id"]]
            sw, sh = src["width"], src["height"]
            par = src.get("display_aspect", sw / sh) / (sw / sh)
            if rcfg.crop_mode == "full_source" or s.get("crop_norm") is None:
                aspects.append(src.get("display_aspect", sw / sh))
            else:
                cx, cy, cw, ch = s["crop_norm"]
                aspects.append((cw * sw) / max(1e-6, ch * sh) * par)
            weights.append(s["edit_f1"] - s["edit_f0"])
        if aspects:
            order = np.argsort(aspects)
            cum = np.cumsum(np.array(weights)[order])
            self.aspect = float(np.array(aspects)[order][np.searchsorted(cum, cum[-1] / 2)])
        else:  # toàn bộ là khung Douyin -> theo vùng nội dung của bản edit
            x, y, w, h = edit_meta.get("content_rect") or (0, 0, 1, 1)
            self.aspect = float(edit_meta.get("width", 16) * w) / float(edit_meta.get("height", 9) * h)
        self.width, self.height = output_size(self.aspect, rcfg.height)
        self.t0 = float(edit_meta.get("first_pts_time", 0.0))
        for s in segments:
            s["out_start"], s["out_end"], s["v_start"] = s["edit_f0"], s["edit_f1"], s["edit_f0"]
        self.total_frames = segments[-1]["edit_f1"]
        self.duration = float(Fraction(self.total_frames) / self.fps)

    def edit_time(self, s: dict, k):
        """Thời điểm (theo đồng hồ bản edit) của khung xuất k: đúng PTS khung edit k (giữ cả VFR)."""
        k = np.asarray(k, dtype=np.float64)
        if len(self.edit_pts):
            ki = np.clip(np.floor(k).astype(int), 0, len(self.edit_pts) - 1)
            return self.edit_pts[ki] + (k - ki) / float(self.fps)  # sau khung cuối: + bội số 1/fps
        return self.t0 + k / float(self.fps)

    def source_frames(self, s: dict) -> np.ndarray:
        """Khung nguồn cho TỪNG khung xuất của run - đúng bảng ánh xạ từng khung."""
        return np.asarray(s["frames"], np.int64)

    def crop_px(self, s: dict, w: int, h: int):
        """Vùng crop (pixel của file dựng w x h) đã chỉnh về đúng tỉ lệ khung xuất."""
        if self.rcfg.crop_mode == "full_source" or s.get("crop_norm") is None:
            crop = (0.0, 0.0, 1.0, 1.0)
        else:
            crop = s["crop_norm"]
        par = self.sources[s["source_id"]].get("display_aspect", w / h) / (w / h)  # tỉ lệ điểm ảnh
        x, y, cw, ch = fit_crop(crop, self.aspect / par, w, h)
        X, Y = int(round(x * w)), int(round(y * h))
        W, H = min(int(round(cw * w)), w - X), min(int(round(ch * h)), h - Y)
        return X, Y, max(2, W - W % 2), max(2, H - H % 2), (x, y, cw, ch)


def _encoder_args(rcfg: RenderConfig, fps: Fraction) -> list[str]:
    codec = rcfg.codec
    if codec == "auto":
        codec = "h264_nvenc" if nvenc_available() else "libx264"
    g = str(int(round(float(fps) * 2)))
    if codec == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", rcfg.preset_nvenc, "-tune", "hq", "-rc", "vbr",
                "-cq", str(rcfg.nvenc_cq), "-b:v", "0", "-maxrate", "60M", "-bufsize", "120M",
                "-profile:v", "high", "-g", g, "-bf", "3", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", rcfg.preset_x264, "-crf", str(rcfg.x264_crf),
            "-profile:v", "high", "-g", g, "-pix_fmt", "yuv420p"]


def encoder_name(rcfg: RenderConfig) -> str:
    return ("h264_nvenc" if nvenc_available() else "libx264") if rcfg.codec == "auto" else rcfg.codec


def render_segment_video(plan: RenderPlan, s: dict, out_file: Path, cancel: threading.Event | None = None,
                         progress=None) -> None:
    """Dựng 1 run; giải mã bằng GPU (CUDA) nếu có, lỗi thì tự làm lại bằng CPU."""
    from .media import cuda_decode_available

    if s.get("kind") == "edit":
        return _render_edit_video(plan, s, out_file, cancel, progress)

    if cuda_decode_available():
        try:
            return _render_segment_video(plan, s, out_file, cancel, progress, hw=True)
        except InterruptedError:
            raise
        except Exception as e:  # pragma: no cover - phụ thuộc GPU
            log.warning("Giải mã GPU lỗi ở đoạn %s (%s) -> dùng CPU", s["segment_id"], str(e)[:120])
    return _render_segment_video(plan, s, out_file, cancel, progress, hw=False)


def _render_segment_video(plan: RenderPlan, s: dict, out_file: Path, cancel, progress, hw: bool) -> None:
    src = plan.sources[s["source_id"]]
    rpath = src.get("render_path") or src["path"]
    rpts = src["_render_pts"]
    frames = plan.source_frames(s)
    pairs = np.asarray(s.get("pairs") if s.get("pairs") is not None else np.full(len(frames), -1), np.int64)
    if src.get("render_path"):  # bản Video2X: ánh xạ theo chỉ số khung (đã xác minh cùng số khung)
        ratio = len(rpts) / src["n_frames"]
        frames_r = np.clip(np.round(frames * ratio).astype(int), 0, len(rpts) - 1)
        pairs_r = np.where(pairs >= 0, np.clip(np.round(pairs * ratio).astype(int), 0, len(rpts) - 1), -1)
    else:
        frames_r, pairs_r = frames, pairs
    allf = np.concatenate([frames_r, pairs_r[pairs_r >= 0]])
    f_lo, f_hi = int(allf.min()), int(allf.max())
    rw, rh = src["_render_wh"]
    X, Y, W, H, _ = plan.crop_px(s, rw, rh)
    t_lo = rpts[f_lo]
    t_hi = rpts[f_hi]
    half = 0.5 / float(src.get("render_fps") or src["fps"])
    vf = [f"trim=start={t_lo - half * 1.4:.6f}:end={t_hi + half * 1.4:.6f}",
          f"crop={W}:{H}:{X}:{Y}"]
    if s["flip"]:
        vf.append("hflip")
    vf += [f"scale={plan.width}:{plan.height}:flags=lanczos+accurate_rnd+full_chroma_int", "setsar=1",
           "format=yuv420p"]
    start_seek = max(0.0, t_lo - float(src.get("render_start_time", 0.0)) - 3.0)
    dec = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error",
           *(["-hwaccel", "cuda"] if hw else []),
           "-ss", f"{start_seek:.6f}", "-copyts", "-i", rpath, "-map", "0:v:0", "-an", "-sn",
           "-vf", ",".join(vf), "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "yuv420p", "pipe:1"]
    enc = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{plan.width}x{plan.height}",
           "-framerate", f"{plan.fps.numerator}/{plan.fps.denominator}", "-i", "pipe:0",
           *_encoder_args(plan.rcfg, plan.fps), "-an", "-movflags", "+faststart", str(out_file)]
    fsize = plan.width * plan.height * 3 // 2
    pd = subprocess.Popen(dec, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
    pe = subprocess.Popen(enc, stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
    need = frames_r - f_lo
    need_pair = np.where(pairs_r >= 0, pairs_r - f_lo, -1)
    cache: dict[int, bytes] = {}  # vài khung gần nhất (khung trộn cần khung kề)
    cur_idx = -1
    written = 0

    def get(idx):
        nonlocal cur_idx
        while cur_idx < idx:
            buf = pd.stdout.read(fsize)
            if len(buf) < fsize:
                # decoder hết khung sớm 1-3 khung ở rìa run (trim/seek lệch): lặp khung cuối
                if cur_idx >= 0 and idx - cur_idx <= 3:
                    log.warning("Run %s: thiếu %d khung cuối nguồn, lặp khung cuối",
                                s.get("run_id", s.get("segment_id")), idx - cur_idx)
                    return cache[cur_idx]
                raise RuntimeError(f"Thiếu khung nguồn khi dựng run {s.get('run_id', s.get('segment_id'))} "
                                   f"(cần khung {f_lo + idx}, chỉ đọc được {f_lo + cur_idx})")
            cur_idx += 1
            cache[cur_idx] = buf
            for old in [c for c in cache if c < cur_idx - 3]:
                del cache[old]
        if idx not in cache:
            raise RuntimeError("Ánh xạ khung đi lùi quá xa trong một run")
        return cache[idx]

    try:
        for k, want in enumerate(need):
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Đã hủy")
            if need_pair[k] >= 0:  # khung nội suy: trộn 50/50 hai khung nguồn liền nhau
                get(max(want, need_pair[k]))  # giải mã tới khung xa hơn; khung kia còn trong cache
                a_ = np.frombuffer(get(want), np.uint8)
                b_ = np.frombuffer(get(need_pair[k]), np.uint8)
                cur = ((a_.astype(np.uint16) + b_) // 2).astype(np.uint8).tobytes()
            else:
                cur = get(want)
            pe.stdin.write(cur)
            written += 1
            if progress and k % 10 == 0:
                progress(k / len(need))
    finally:
        try:
            pe.stdin.close()
        except Exception:
            pass
        pd.stdout.close()
        pd.kill()
        pd.wait()
    pe.wait()
    if pe.returncode != 0:
        raise RuntimeError("Mã hóa thất bại: " + pe.stderr.read().decode("utf-8", "replace")[-2000:])
    if written != len(need):
        raise RuntimeError("Số khung đoạn không khớp")


def _render_edit_video(plan: RenderPlan, s: dict, out_file: Path, cancel, progress) -> None:
    """Run lấy khung từ bản Douyin (fallback sạch, hoặc xem thử khung chưa xác minh):
    cắt vùng nội dung, scale giữ tỉ lệ, viền đen nếu khác tỉ lệ khung xuất. Đúng số khung."""
    em = plan.edit_meta
    path = em.get("path")
    if not path or not Path(path).exists():
        raise RuntimeError("Cần file video Douyin để dựng các khung lấy từ bản edit (fallback/xem thử).")
    pts = plan.edit_pts
    f0, f1 = s["edit_f0"], s["edit_f1"]
    W0, H0 = int(em.get("width")), int(em.get("height"))
    x, y, w, h = em.get("content_rect") or (0, 0, 1, 1)
    X, Y, CW, CH = int(x * W0), int(y * H0), max(2, int(w * W0) // 2 * 2), max(2, int(h * H0) // 2 * 2)
    half = 0.5 / float(plan.fps)
    t_lo, t_hi = float(pts[f0]), float(pts[f1 - 1])
    vf = [f"trim=start={t_lo - half:.6f}:end={t_hi + half:.6f}", f"crop={CW}:{CH}:{X}:{Y}",
          f"scale={plan.width}:{plan.height}:force_original_aspect_ratio=decrease:flags=lanczos",
          f"pad={plan.width}:{plan.height}:(ow-iw)/2:(oh-ih)/2:color=black", "setsar=1", "format=yuv420p"]
    start_seek = max(0.0, t_lo - float(em.get("start_time", 0.0)) - 3.0)
    dec = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", f"{start_seek:.6f}", "-copyts",
           "-i", path, "-map", "0:v:0", "-an", "-vf", ",".join(vf), "-fps_mode", "passthrough",
           "-f", "rawvideo", "-pix_fmt", "yuv420p", "pipe:1"]
    enc = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{plan.width}x{plan.height}",
           "-framerate", f"{plan.fps.numerator}/{plan.fps.denominator}", "-i", "pipe:0",
           *_encoder_args(plan.rcfg, plan.fps), "-an", "-movflags", "+faststart", str(out_file)]
    fsize = plan.width * plan.height * 3 // 2
    n = f1 - f0
    pd = subprocess.Popen(dec, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
    pe = subprocess.Popen(enc, stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
    written = 0
    try:
        for k in range(n):
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Đã hủy")
            buf = pd.stdout.read(fsize)
            if len(buf) < fsize:
                raise RuntimeError(f"Thiếu khung Douyin khi dựng run {s.get('run_id')} ({k}/{n})")
            pe.stdin.write(buf)
            written += 1
    finally:
        try:
            pe.stdin.close()
        except Exception:
            pass
        pd.stdout.close()
        pd.kill()
        pd.wait()
    pe.wait()
    if pe.returncode != 0 or written != n:
        raise RuntimeError("Mã hóa run Douyin thất bại / thiếu khung")


def _fade(pcm: np.ndarray, rate: int, ms: float = 3.0) -> np.ndarray:
    """Làm mềm 2 đầu khúc âm thanh (chống tiếng tách ở điểm nối)."""
    k = min(len(pcm) // 2, int(rate * ms / 1000))
    if k > 1:
        ramp = np.linspace(0, 1, k, dtype=np.float32)[:, None]
        pcm[:k] *= ramp
        pcm[-k:] *= ramp[::-1]
    return pcm


def _atempo_chain(speed: float) -> list[str]:
    out, s = [], speed
    while s > 2.0:
        out.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        out.append("atempo=0.5")
        s /= 0.5
    if abs(s - 1.0) > 1e-4:
        out.append(f"atempo={s:.6f}")
    return out


def segment_audio(plan: RenderPlan, s: dict, n_samples: int, rate: int) -> tuple[np.ndarray, dict]:
    """Âm thanh của run. Khung Douyin / đứng hình: KHÔNG bịa tiếng -> im lặng + AUDIO_UNRESOLVED."""
    if s.get("kind") == "edit":
        return np.zeros((n_samples, 2), np.float32), {"audio_start": None, "audio_stream": None,
                                                       "audio_unresolved": [[0, n_samples]]}
    if not s.get("linear", True):
        return _ramp_audio(plan, s, n_samples, rate)
    pcm, info = _linear_audio(plan, s, n_samples, rate)
    return _fade(pcm, rate), info


def _ramp_audio(plan: RenderPlan, s: dict, n_samples: int, rate: int) -> tuple[np.ndarray, dict]:
    """Speed ramp: chia run thành khúc tốc độ không đổi, time-stretch (giữ cao độ) từng khúc."""
    src = plan.sources[s["source_id"]]
    sp = np.asarray(s["speeds"], np.float64)
    fr = np.asarray(s["frames"], np.int64)
    n = len(fr)
    out = np.zeros((n_samples, 2), np.float32)
    unresolved = []
    starts = [0] + [i for i in range(1, n) if abs(sp[i] - sp[i - 1]) > 1e-6] + [n]
    o0 = s["out_start"]
    info = {"audio_start": None, "audio_stream": (src.get("audio_stream") or {}).get("index"), "pieces": []}
    for a, b in zip(starts[:-1], starts[1:]):
        s0 = int(round((plan.edit_time(s, o0 + a) - plan.edit_time(s, o0)) * rate))
        s1 = n_samples if b == n else int(round((plan.edit_time(s, o0 + b) - plan.edit_time(s, o0)) * rate))
        if s1 <= s0:
            continue
        if sp[a] <= 0 or not src.get("audio_stream"):
            unresolved.append([s0, s1])
            continue
        # a = khung - 0.5: mô hình tuyến tính hiển thị đúng khung fr[a] từ thời điểm bắt đầu của nó
        sub = dict(s, a=float(fr[a]) - 0.5, speed=float(sp[a]), t_ref=float(plan.edit_time(s, o0 + a)),
                   out_start=o0 + a, linear=True)
        pcm, inf = _linear_audio(plan, sub, s1 - s0, rate)
        out[s0:s1] = _fade(pcm, rate)
        info["pieces"].append({"from_frame": a, "speed": float(sp[a]), "audio_start": inf["audio_start"]})
        if info["audio_start"] is None:
            info["audio_start"] = inf["audio_start"]
    if unresolved:
        info["audio_unresolved"] = unresolved
    return out, info


def _linear_audio(plan: RenderPlan, s: dict, n_samples: int, rate: int) -> tuple[np.ndarray, dict]:
    """Âm thanh của đoạn từ đúng nguồn, đúng source_pts, đã hiệu chỉnh độ trễ riêng của nguồn."""
    src = plan.sources[s["source_id"]]
    a = src.get("audio_stream")
    info = {"audio_start": None, "audio_stream": a["index"] if a else None}
    if s["speed"] <= 0:  # giữ khung (freeze): không có tiếng tương ứng -> im lặng, KHÔNG bịa
        info["audio_unresolved"] = [[0, n_samples]]
        return np.zeros((n_samples, 2), np.float32), info
    if not a:
        log.warning("Nguồn %s không có âm thanh -> đoạn %s im lặng", s["source_id"], s["segment_id"])
        return np.zeros((n_samples, 2), np.float32), info
    fs = float(src["fps"])
    pts = np.asarray(src["_pts"])
    # thời điểm nguồn liên tục ứng với đầu đoạn (cùng mô hình với hình)
    x = s["a"] + s["speed"] * fs * (float(plan.edit_time(s, s["out_start"])) - s["t_ref"]) + 0.5
    idx = np.arange(len(pts))
    if 0 <= x <= len(pts) - 1:
        t_start = float(np.interp(x, idx, pts))
    else:
        t_start = float(pts[0] + x / fs)
    t_start += float(src.get("audio_offset_ms", 0.0)) / 1000.0
    dur_out = n_samples / rate
    dur_src = dur_out * s["speed"]
    info["audio_start"] = t_start
    filt = [f"atrim=start={max(0.0, t_start):.6f}:duration={dur_src + 0.05:.6f}", "asetpts=PTS-STARTPTS",
            *_atempo_chain(s["speed"]), f"aresample={rate}"]
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error",
           "-ss", f"{max(0.0, t_start - float(src.get('start_time', 0.0)) - 2.0):.6f}", "-copyts",
           "-i", src["path"], "-map", f"0:{a['index']}", "-af", ",".join(filt),
           "-ac", "2", "-f", "f32le", "pipe:1"]
    p = run(cmd, check=False)
    pcm = np.frombuffer(p.stdout, np.float32).reshape(-1, 2) if p.stdout else np.zeros((0, 2), np.float32)
    lead = 0
    if t_start < 0:  # đoạn bắt đầu trước khi nguồn có âm thanh
        lead = int(round(-t_start * rate))
    out = np.zeros((n_samples, 2), np.float32)
    m = max(0, min(n_samples - lead, len(pcm)))
    out[lead:lead + m] = pcm[:m]
    if m < n_samples - lead - int(0.01 * rate):
        log.warning("Đoạn %s: âm thanh nguồn ngắn hơn dự kiến (%d/%d mẫu)", s["segment_id"], m, n_samples)
    return out, info


def _write_wav(path: Path, pcm: np.ndarray, rate: int):
    data = (np.clip(pcm, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())


def prepare_sources(sources: dict) -> None:
    """Bổ sung PTS & kích thước file dựng (bản Video2X nếu có) + kiểm tra khớp số khung."""
    from .media import probe

    for sid, src in sources.items():
        rpath = src.get("render_path") or src["path"]
        ri = probe(rpath, with_hash=False)
        src["_render_wh"] = (ri.width, ri.height)
        src["render_fps"] = ri.fps
        src["render_start_time"] = ri.start_time
        if src.get("_pts") is None:
            src["_pts"] = packet_pts(src["path"])
        if src.get("render_path"):
            rp = packet_pts(rpath)
            n0 = src["n_frames"]
            if abs(len(rp) - n0) > max(2, 0.001 * n0):
                raise RuntimeError(
                    f"Bản Video2X của {sid} có {len(rp)} khung, bản gốc {n0} khung -> không cùng dòng thời gian. "
                    "Hãy dùng bản Video2X giữ nguyên số khung (không nội suy khung).")
            src["_render_pts"] = rp
        else:
            src["_render_pts"] = np.asarray(src["_pts"])


def render(plan: RenderPlan, out_path: Path, work_dir: Path, progress=None,
           cancel: threading.Event | None = None) -> dict:
    work_dir.mkdir(parents=True, exist_ok=True)
    rcfg = plan.rcfg
    rate = rcfg.audio_rate
    segs = [s for s in plan.segments if s.get("render")]
    total = sum(s["out_end"] - s["out_start"] for s in segs)
    done = 0
    parts = []
    audio_parts = []
    seg_info = []
    for s in segs:
        src_meta = plan.sources.get(s.get("source_id")) or {}
        key = hashlib.sha1(json.dumps({
            "v": RENDER_VERSION, "code": hashlib.sha1(Path(__file__).read_bytes()).hexdigest()[:8],
            "kind": s.get("kind"), "o": s["out_start"], "n": s["out_end"] - s["out_start"],
            "src": src_meta.get("content_hash"), "r": src_meta.get("render_path"),
            "edit": plan.edit_meta.get("content_hash"),
            "frames": hashlib.sha1(np.asarray(s.get("frames", []), np.int64).tobytes()).hexdigest()[:10],
            "pairs": hashlib.sha1(np.asarray(s.get("pairs", []), np.int64).tobytes()).hexdigest()[:10],
            "crop": s.get("crop_norm"), "flip": s.get("flip"),
            "size": [plan.width, plan.height], "fps": str(plan.fps), "enc": encoder_name(rcfg),
            "q": [rcfg.nvenc_cq, rcfg.x264_crf, rcfg.crop_mode]}, sort_keys=True, default=str).encode()).hexdigest()[:12]
        vf = work_dir / f"run_{s.get('run_id', s.get('segment_id')):05d}_{key}.mp4"
        n_out = s["out_end"] - s["out_start"]
        if not (vf.exists() and _count_frames(vf) == n_out):
            log.info("Dựng run %s (%s): %s, %d khung", s.get("run_id"), "/".join(s.get("origins", [])),
                     s.get("source_id") or "khung Douyin", n_out)
            tmp = vf.with_suffix(".part.mp4")
            render_segment_video(plan, s, tmp, cancel,
                                 (lambda f, d=done: progress(0.9 * (d + f * n_out) / total, f"đoạn {s['segment_id']}"))
                                 if progress else None)
            tmp.replace(vf)
        else:
            log.info("Bỏ qua đoạn %s (đã dựng trước đó)", s["segment_id"])
        parts.append(vf)
        # âm thanh: số mẫu theo thời gian xuất tích lũy -> nối không lệch
        # vị trí mẫu theo PTS khung edit (giữ đúng cả VFR), làm tròn tích lũy -> không trôi ở điểm nối
        a0 = int(round((float(plan.edit_time(s, s["out_start"])) - plan.t0) * rate))
        a1 = int(round((float(plan.edit_time(s, s["out_end"])) - plan.t0) * rate))
        pcm, ainfo = segment_audio(plan, s, a1 - a0, rate)
        audio_parts.append(pcm)
        seg_info.append({"segment_id": s.get("segment_id"), "run_id": s.get("run_id"), "kind": s.get("kind"),
                         "origins": s.get("origins"), "file": vf.name, "frames": n_out,
                         "out_start": s["out_start"], "out_end": s["out_end"], **ainfo})
        done += n_out
        if progress:
            progress(0.9 * done / total, f"xong run {s.get('run_id')}")
    lst = work_dir / "concat.txt"
    lst.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in parts), encoding="utf-8")
    wav = work_dir / "audio.wav"
    _write_wav(wav, np.concatenate(audio_parts) if audio_parts else np.zeros((0, 2), np.float32), rate)
    tmp_out = out_path.with_suffix(".part.mp4")
    run([ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
         "-f", "concat", "-safe", "0", "-i", str(lst), "-i", str(wav),
         "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", rcfg.audio_bitrate,
         "-ar", str(rate), "-metadata:s:a:0", f"language={rcfg.audio_lang}",
         "-movflags", "+faststart", str(tmp_out)])
    tmp_out.replace(out_path)
    if progress:
        progress(1.0, "xong")
    return {"output": str(out_path), "frames": plan.total_frames, "fps": str(plan.fps),
            "size": [plan.width, plan.height], "encoder": encoder_name(rcfg), "segments": seg_info}


def _count_frames(path: Path) -> int:
    try:
        p = run([ffprobe_bin(), "-v", "error", "-select_streams", "v:0", "-count_packets",
                 "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)], check=False)
        return int(p.stdout.decode().strip().split(",")[0])
    except Exception:
        return -1
