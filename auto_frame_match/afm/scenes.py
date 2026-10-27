"""Tầng 1: phát hiện cảnh (PySceneDetect + kiểm tra lại bằng diff từng khung) và vùng nội dung."""

from __future__ import annotations

import cv2
import numpy as np

from .fingerprint import Fingerprint
from .log import get_logger

log = get_logger()


def frame_diffs(fp: Fingerprint) -> np.ndarray:
    """|khung_i - khung_{i-1}| trung bình, tính trên ảnh 64px. d[0] = 0."""
    n = fp.n
    w, h = fp.size
    sw = 64
    sh = max(8, int(round(h * sw / w)))
    d = np.zeros(n, np.float32)
    prev = None
    for i in range(n):
        cur = cv2.resize(fp.thumbs[i], (sw, sh), interpolation=cv2.INTER_AREA).astype(np.float32)
        if prev is not None:
            d[i] = float(np.mean(np.abs(cur - prev)))
        prev = cur
    return d


def _pyscenedetect_cuts(path: str, threshold: float, fp: Fingerprint) -> list[int]:
    try:
        from scenedetect import ContentDetector, detect  # type: ignore
    except Exception:
        log.warning("Chưa cài PySceneDetect - chỉ dùng bộ dò diff nội bộ")
        return []
    try:
        scenes = detect(path, ContentDetector(threshold=threshold, min_scene_len=3), show_progress=False)
    except Exception as e:  # pragma: no cover
        log.warning("PySceneDetect lỗi (%s) - chỉ dùng bộ dò nội bộ", e)
        return []
    t0 = float(fp.pts[0])
    cuts = []
    for start, _ in scenes[1:]:
        sec = start.seconds if isinstance(getattr(type(start), "seconds", None), property) else start.get_seconds()
        cuts.append(fp.frame_at_time(t0 + float(sec)))
    return cuts


def detect_scenes(edit_path: str, fp: Fingerprint, threshold: float = 27.0,
                  ratio: float = 4.0, min_len: int = 3) -> tuple[list[tuple[int, int]], dict]:
    """Trả về danh sách cảnh [start, end) theo chỉ số khung + thông tin kiểm tra."""
    d = frame_diffs(fp)
    n = fp.n
    sd_cuts = _pyscenedetect_cuts(edit_path, threshold, fp)
    # Kiểm tra lại các điểm cắt PySceneDetect: dời về cực đại diff trong ±2 khung
    refined = set()
    suspicious = []
    for c in sd_cuts:
        lo, hi = max(1, c - 2), min(n, c + 3)
        if hi <= lo:
            continue
        best = lo + int(np.argmax(d[lo:hi]))
        if best != c:
            suspicious.append({"frame": c, "moved_to": best, "reason": "điểm cắt lệch khung"})
        refined.add(best)
    # Điểm cắt bổ sung mà PySceneDetect có thể bỏ sót
    pad = 12
    own = set()
    for i in range(1, n):
        lo, hi = max(1, i - pad), min(n, i + pad + 1)
        local = np.median(d[lo:hi])
        if d[i] > max(6.0, ratio * max(local, 0.5)) and d[i] >= d[max(1, i - 1):min(n, i + 2)].max():
            own.add(i)
    for c in sorted(own - refined):
        suspicious.append({"frame": c, "reason": "PySceneDetect bỏ sót - bộ dò nội bộ phát hiện"})
    for c in sorted(refined - own):
        suspicious.append({"frame": c, "reason": "chỉ PySceneDetect phát hiện - sẽ xác minh bằng so khớp"})
    cuts = sorted(refined | own)
    # gộp điểm cắt quá gần
    merged: list[int] = []
    for c in cuts:
        if merged and c - merged[-1] < min_len:
            if d[c] > d[merged[-1]]:
                merged[-1] = c
            continue
        merged.append(c)
    bounds = [0] + merged + [n]
    scenes = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1) if bounds[i + 1] > bounds[i]]
    log.info("Phát hiện %d cảnh (PySceneDetect %d điểm, nội bộ %d điểm, %d điểm nghi vấn)",
             len(scenes), len(sd_cuts), len(own), len(suspicious))
    return scenes, {"diffs": d, "suspicious": suspicious, "pyscenedetect_cuts": sd_cuts,
                    "own_cuts": sorted(own)}


def _main_run(profile: np.ndarray, rel: float = 0.25, gap_frac: float = 0.03) -> tuple[int, int]:
    """Dải liên tục có tổng năng lượng lớn nhất (bỏ qua khe hở nhỏ) -> tách nội dung khỏi phụ đề/chữ ở viền."""
    n = len(profile)
    thr = rel * np.percentile(profile, 90)
    on = profile > thr
    if not on.any():
        return 0, n
    gap = max(1, int(gap_frac * n))
    runs, start, last = [], None, None
    for i in range(n):
        if on[i]:
            if start is None:
                start = i
            elif i - last > gap + 1:
                runs.append((start, last + 1))
                start = i
            last = i
    runs.append((start, last + 1))
    best = max(runs, key=lambda r: profile[r[0]:r[1]].sum())
    return best


def detect_content_rect(fp: Fingerprint, samples: int = 120) -> tuple[float, float, float, float]:
    """Vùng nội dung lấy từ nguồn: cấu trúc SẮC NÉT và CHUYỂN ĐỘNG (loại viền đen, nền mờ, chữ tĩnh).

    Chuẩn hóa (x, y, w, h).
    """
    n = fp.n
    idx = np.linspace(0, n - 1, min(samples, n)).astype(int)
    laps = np.stack([np.abs(cv2.Laplacian(fp.thumbs[i].astype(np.float32), cv2.CV_32F, ksize=3)) for i in idx])
    static = np.median(laps, axis=0)
    dyn = np.abs(laps - static[None]).mean(axis=0)  # nền mờ ~0, chữ tĩnh ~0, nội dung động > 0
    h, w = dyn.shape
    # theo chiều dọc: dải chính (tách chữ tiêu đề/phụ đề trên nền mờ của Douyin)
    y0, y1 = _main_run(dyn.mean(axis=1), gap_frac=0.05)
    # theo chiều ngang: từ cột đầu đến cột cuối có nội dung (nội dung hoạt hình có thể có khe trơn)
    col = dyn[y0:y1].mean(axis=0)
    on = np.where(col > 0.25 * np.percentile(col, 90))[0]
    x0, x1 = (int(on[0]), int(on[-1]) + 1) if len(on) else (0, w)
    rect = (x0 / w, y0 / h, (x1 - x0) / w, (y1 - y0) / h)
    if rect[2] * rect[3] < 0.08:  # quá nhỏ -> không tin, dùng toàn khung
        rect = (0.0, 0.0, 1.0, 1.0)
    # gần sát viền -> coi như toàn khung (tránh cắt mất mép do khung trơn ở rìa)
    x, y, ww, hh = rect
    if x < 0.03 and x + ww > 0.97:
        x, ww = 0.0, 1.0
    if y < 0.03 and y + hh > 0.97:
        y, hh = 0.0, 1.0
    rect = (x, y, ww, hh)
    log.info("Vùng nội dung bản edit: x=%.3f y=%.3f w=%.3f h=%.3f", *rect)
    return rect
