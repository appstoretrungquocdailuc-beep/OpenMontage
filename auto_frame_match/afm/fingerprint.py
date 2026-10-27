"""Fingerprint video: thumbnail xám (memmap), PTS, pHash/DCT, vector đặc trưng đa cửa sổ.

Cache theo băm NỘI DUNG file -> gặp lại cùng tập (kể cả đổi tên) không cần quét lại.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .config import FINGERPRINT_VERSION, AnalysisConfig
from .log import get_logger
from .media import MediaInfo, decode_thumbs, thumb_size

log = get_logger()


# ---------------------------------------------------------------- đặc trưng

@functools.lru_cache(maxsize=4096)
def area_matrix(n_in: int, n_out: int) -> np.ndarray:
    """Ma trận (n_out x n_in) lấy trung bình theo diện tích (tương đương INTER_AREA khi thu nhỏ)."""
    m = np.zeros((n_out, n_in), np.float32)
    scale = n_in / n_out
    for i in range(n_out):
        a, b = i * scale, (i + 1) * scale
        for j in range(int(np.floor(a)), min(n_in, int(np.ceil(b)))):
            m[i, j] = min(b, j + 1) - max(a, j)
        m[i] /= m[i].sum()
    return m


def window_features(imgs: np.ndarray, side: int) -> np.ndarray:
    """(n, h, w) uint8 -> (n, side*side) vector chuẩn hóa (trừ trung bình, chia chuẩn).

    Bất biến độ sáng/tương phản; vector hóa bằng phép nhân ma trận (nhanh cho hàng chục nghìn khung).
    """
    n, h, w = imgs.shape
    ry, rx = area_matrix(h, side), area_matrix(w, side)
    # 2 phép nhân ma trận lớn (BLAS) thay vì n phép nhỏ
    x = imgs.astype(np.float32).transpose(1, 0, 2).reshape(h, n * w)  # (h, n*w)
    t = (ry @ x).reshape(side, n, w).transpose(1, 0, 2).reshape(n * side, w)  # (n*side, w)
    small = (t @ rx.T).reshape(n, side * side)
    small -= small.mean(axis=1, keepdims=True)
    nrm = np.linalg.norm(small, axis=1, keepdims=True)
    out = small / np.maximum(nrm, 1e-6)
    out[nrm[:, 0] < 1e-3 * side] = 0.0
    return out


def window_feature(img: np.ndarray, side: int) -> np.ndarray:
    return window_features(np.ascontiguousarray(img)[None], side)[0]


def crop_bounds(h: int, w: int, win) -> tuple[int, int, int, int]:
    _, x, y, ww, hh = win
    x0, y0 = int(round(x * w)), int(round(y * h))
    x1, y1 = max(x0 + 2, int(round((x + ww) * w))), max(y0 + 2, int(round((y + hh) * h)))
    return x0, y0, x1, y1


def phash(img: np.ndarray) -> np.uint64:
    small = cv2.resize(img, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    d = cv2.dct(small)[:8, :8].ravel()[1:]
    bits = (d > np.median(d)).astype(np.uint64)
    v = np.uint64(0)
    for b in bits:
        v = (v << np.uint64(1)) | b
    return v


def hamming(a: np.uint64, b: np.uint64) -> int:
    return bin(int(a) ^ int(b)).count("1")


def source_windows(aspect: float) -> list[tuple[str, float, float, float, float]]:
    """Các cửa sổ (x, y, w, h chuẩn hóa) để hỗ trợ crop/zoom (kể cả lệch tâm) và khung dọc."""
    wins = [("full", 0, 0, 1, 1), ("c85", 0.075, 0.075, 0.85, 0.85)]
    for s in (0.7, 0.55):
        for yi, y in enumerate(np.linspace(0, 1 - s, 3)):
            for xi, x in enumerate(np.linspace(0, 1 - s, 3)):
                wins.append((f"g{int(s * 100)}_{yi}{xi}", float(x), float(y), s, s))
    pw = min(1.0, (9 / 16) / aspect)  # cột dọc 9:16 cao bằng khung (Douyin crop dọc)
    if pw < 0.95:
        nx = int(np.ceil((1 - pw) / 0.04)) + 1  # bước 4% bề ngang -> chịu được crop lệch tâm
        for k, x in enumerate(np.linspace(0, 1 - pw, nx)):
            wins.append((f"p9x16_{k:02d}", float(x), 0, pw, 1))
        pz = pw * 0.8  # cột dọc có zoom thêm
        for k, x in enumerate(np.linspace(0, 1 - pz, max(2, nx - 2))):
            wins.append((f"p9x16z_{k:02d}", float(x), 0.1, pz, 0.8))
    return wins


def crop_norm(img: np.ndarray, win) -> np.ndarray:
    x0, y0, x1, y1 = crop_bounds(img.shape[0], img.shape[1], win)
    return img[y0:y1, x0:x1]


# ---------------------------------------------------------------- cache

@dataclass
class Fingerprint:
    root: Path
    meta: dict
    thumbs: np.ndarray  # memmap (N, H, W) uint8
    pts: np.ndarray  # (N,) giây
    phashes: np.ndarray  # (N,) uint64
    feats: np.ndarray | None  # (Nidx, W, d) float16 (chỉ nguồn)
    windows: list

    @property
    def n(self) -> int:
        return int(self.thumbs.shape[0])

    @property
    def fps(self) -> float:
        return float(self.meta["fps"])

    @property
    def size(self) -> tuple[int, int]:
        return int(self.meta["thumb_w"]), int(self.meta["thumb_h"])

    def frame_at_time(self, t: float) -> int:
        """Khung đang hiển thị tại thời điểm t (theo PTS)."""
        i = int(np.searchsorted(self.pts, t + 1e-6, side="right")) - 1
        return min(max(i, 0), self.n - 1)


def index_stride(info: MediaInfo, cfg: AnalysisConfig) -> int:
    """Chỉ mục tìm nhanh không cần mọi khung (phim hoạt hình vẽ 'on 2s'); so khớp chính xác vẫn dùng MỌI khung."""
    if cfg.index_stride > 0:
        return cfg.index_stride
    return 2 if info.nb_frames_hint > 6000 else 1


def _cache_key(info: MediaInfo, role: str, size: tuple[int, int], cfg: AnalysisConfig) -> str:
    extra = f"_s{index_stride(info, cfg)}_f{cfg.feat_side}" if role == "source" else ""
    return f"{role}_{info.content_hash}_v{FINGERPRINT_VERSION}_{size[0]}x{size[1]}{extra}"


def build_fingerprint(info: MediaInfo, role: str, cfg: AnalysisConfig, cache_dir: str | Path,
                      progress=None, cancel: threading.Event | None = None) -> Fingerprint:
    aspect = info.display_aspect
    area = cfg.source_thumb_area if role == "source" else cfg.edit_thumb_area
    size = thumb_size(aspect, area)
    root = Path(cache_dir) / _cache_key(info, role, size, cfg)
    meta_path = root / "meta.json"
    if meta_path.exists():
        try:
            fp = load_fingerprint(root)
            log.info("Dùng cache fingerprint (%s): %s", role, Path(info.path).name)
            if progress:
                progress(1.0, "cache")
            return fp
        except Exception as e:  # cache hỏng -> làm lại
            log.warning("Cache hỏng, quét lại: %s", e)
            shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    expected = max(1, info.nb_frames_hint)
    log.info("Giải mã toàn bộ khung %s (%s, ~%d khung, thumb %dx%d)", role,
             Path(info.path).name, expected, *size)
    part = root / "thumbs.u8.part"
    n, pts = decode_thumbs(
        info.path, size, part, expected, use_gpu=cfg.use_gpu_decode,
        progress=(lambda k: progress(min(0.7, 0.7 * k / expected), f"giải mã {k}/{expected}"))
        if progress else None,
        cancel=cancel,
    )
    if n == 0:
        raise RuntimeError(f"Không giải mã được khung nào: {info.path}")
    os.replace(part, root / "thumbs.u8")
    np.save(root / "pts.npy", pts)
    thumbs = np.memmap(root / "thumbs.u8", np.uint8, "r", shape=(n, size[1], size[0]))

    ph = np.zeros(n, np.uint64)
    for i in range(n):
        ph[i] = phash(thumbs[i])
        if progress and i % 500 == 0:
            progress(0.7 + 0.1 * i / n, "pHash/DCT")
    np.save(root / "phash.npy", ph)

    wins = source_windows(aspect) if role == "source" else []
    if role == "source":
        idx = np.arange(0, n, index_stride(info, cfg))
        d = cfg.feat_side ** 2
        feats = np.lib.format.open_memmap(root / "feats.npy", "w+", np.float16, (len(idx), len(wins), d))
        H, W = size[1], size[0]
        for a in range(0, len(idx), 512):
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Đã hủy")
            blk = thumbs[idx[a:a + 512]]
            for wi, win in enumerate(wins):
                x0, y0, x1, y1 = crop_bounds(H, W, win)
                feats[a:a + 512, wi] = window_features(blk[:, y0:y1, x0:x1], cfg.feat_side)
            if progress:
                progress(0.8 + 0.2 * a / len(idx), "đặc trưng cửa sổ")
        feats.flush()
        del feats
        np.save(root / "feat_idx.npy", idx)
    meta = {
        "role": role, "path": info.path, "content_hash": info.content_hash,
        "fps": info.fps, "n": n, "thumb_w": size[0], "thumb_h": size[1],
        "width": info.width, "height": info.height, "aspect": aspect,
        "windows": wins, "fp_version": FINGERPRINT_VERSION,
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    if progress:
        progress(1.0, "xong")
    return load_fingerprint(root)


def load_fingerprint(root: Path) -> Fingerprint:
    meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
    n, w, h = meta["n"], meta["thumb_w"], meta["thumb_h"]
    thumbs = np.memmap(root / "thumbs.u8", np.uint8, "r", shape=(n, h, w))
    pts = np.load(root / "pts.npy")
    ph = np.load(root / "phash.npy")
    feats = None
    if (root / "feats.npy").exists():
        feats = np.load(root / "feats.npy", mmap_mode="r")
        meta["feat_idx"] = np.load(root / "feat_idx.npy")
    if len(pts) != n or len(ph) != n:
        raise RuntimeError("cache không đầy đủ")
    return Fingerprint(root, meta, thumbs, pts, ph, feats, [tuple(x) for x in meta["windows"]])
