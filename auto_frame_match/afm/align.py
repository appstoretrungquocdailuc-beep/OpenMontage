"""Tầng 3: hình học (crop/zoom/lật) + căn chỉnh thời gian chính xác từng khung.

Quy ước: ``A`` (2x3) ánh xạ tọa độ CHUẨN HÓA của bản edit [0,1]^2 -> tọa độ chuẩn hóa của nguồn.
Lật gương thể hiện bằng hệ số sx < 0.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass

import cv2
import numpy as np

from .config import AnalysisConfig
from .fingerprint import Fingerprint
from .log import get_logger

log = get_logger()


# ---------------------------------------------------------------- hình học

@dataclass
class Geometry:
    sx: float
    sy: float
    tx: float
    ty: float

    @property
    def flip(self) -> bool:
        return self.sx < 0

    def matrix(self) -> np.ndarray:
        return np.array([[self.sx, 0, self.tx], [0, self.sy, self.ty]], np.float64)

    def map(self, x, y):
        return self.sx * np.asarray(x) + self.tx, self.sy * np.asarray(y) + self.ty

    def inv_map(self, xs, ys):
        return (np.asarray(xs) - self.tx) / self.sx, (np.asarray(ys) - self.ty) / self.sy

    def valid_edit_rect(self, margin: float = 0.002) -> tuple[float, float, float, float] | None:
        """Phần khung edit thực sự lấy từ nguồn (giao của khung edit và khung nguồn)."""
        xa, ya = self.inv_map(0 + margin, 0 + margin)
        xb, yb = self.inv_map(1 - margin, 1 - margin)
        x0, x1 = sorted((float(xa), float(xb)))
        y0, y1 = sorted((float(ya), float(yb)))
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(1, x1), min(1, y1)
        if x1 - x0 < 0.05 or y1 - y0 < 0.05:
            return None
        return x0, y0, x1 - x0, y1 - y0

    def source_crop(self) -> tuple[float, float, float, float] | None:
        """Vùng nguồn (chuẩn hóa) hiển thị trong bản edit -> dùng để crop khi xuất."""
        r = self.valid_edit_rect(0)
        if r is None:
            return None
        x0, y0, w, h = r
        xs = sorted(float(v) for v in self.map(np.array([x0, x0 + w]), np.array([y0, y0 + h]))[0])
        ys = sorted(float(v) for v in self.map(np.array([x0, x0 + w]), np.array([y0, y0 + h]))[1])
        return max(0.0, xs[0]), max(0.0, ys[0]), min(1.0, xs[1]) - max(0.0, xs[0]), \
            min(1.0, ys[1]) - max(0.0, ys[0])

    def to_dict(self) -> dict:
        crop = self.source_crop()
        return {"sx": self.sx, "sy": self.sy, "tx": self.tx, "ty": self.ty, "flip": self.flip,
                "source_crop": crop, "edit_valid": self.valid_edit_rect(0)}

    @staticmethod
    def from_dict(d: dict) -> "Geometry":
        return Geometry(float(d["sx"]), float(d["sy"]), float(d["tx"]), float(d["ty"]))

    def flipped(self) -> "Geometry":
        """Cùng vùng nguồn nhưng lật gương ngược lại."""
        return Geometry(-self.sx, self.sy, self.tx + self.sx, self.ty)

    def close_to(self, o: "Geometry", tol: float = 0.02) -> bool:
        return (np.sign(self.sx) == np.sign(o.sx) and abs(self.sx - o.sx) < tol
                and abs(self.sy - o.sy) < tol and abs(self.tx - o.tx) < tol and abs(self.ty - o.ty) < tol)


def geometry_from_windows(qe, ws, flip: bool) -> Geometry:
    """qe: cửa sổ trên edit (x,y,w,h), ws: cửa sổ trên nguồn (name,x,y,w,h)."""
    qx, qy, qw, qh = qe
    _, wx, wy, ww, wh = ws
    sy = wh / qh
    ty = wy - qy * sy
    if not flip:
        sx = ww / qw
        tx = wx - qx * sx
    else:
        sx = -ww / qw
        tx = wx + ww + qx * ww / qw
    return Geometry(sx, sy, tx, ty)


def _to_px(g: Geometry, esize, ssize) -> np.ndarray:
    ew, eh = esize
    sw, sh = ssize
    S_s = np.diag([sw, sh, 1.0])
    S_e_inv = np.diag([1 / ew, 1 / eh, 1.0])
    A = np.vstack([g.matrix(), [0, 0, 1]])
    return (S_s @ A @ S_e_inv)[:2].astype(np.float32)


def _from_px(M: np.ndarray, esize, ssize) -> Geometry | None:
    ew, eh = esize
    sw, sh = ssize
    A = np.diag([1 / sw, 1 / sh, 1.0]) @ np.vstack([M, [0, 0, 1]]) @ np.diag([ew, eh, 1.0])
    sx, sy = A[0, 0], A[1, 1]
    if abs(sx) < 1e-3 or abs(sy) < 1e-3:
        return None
    # chỉ chấp nhận crop/scale theo trục (không xoay/nghiêng đáng kể)
    if abs(A[0, 1]) > 0.08 * abs(sy) or abs(A[1, 0]) > 0.08 * abs(sx) or sy < 0:
        return None
    return Geometry(float(sx), float(sy), float(A[0, 2] + A[0, 1] * 0.5), float(A[1, 2] + A[1, 0] * 0.5))


def refine_ecc(edit_img: np.ndarray, src_img: np.ndarray, g: Geometry, iters: int = 80) -> tuple[Geometry, float]:
    """Tinh chỉnh hình học bằng ECC (template = edit, input = nguồn)."""
    esize = (edit_img.shape[1], edit_img.shape[0])
    ssize = (src_img.shape[1], src_img.shape[0])
    M = _to_px(g, esize, ssize).astype(np.float64)
    valid = g.valid_edit_rect(0.01)
    if valid is None:
        return g, -1.0
    # cắt template theo vùng hợp lệ thay cho mask (mask ECC khác nhau giữa OpenCV 4 và 5)
    x, y, w, h = valid
    x0, y0 = int(x * esize[0]), int(y * esize[1])
    x1, y1 = int((x + w) * esize[0]), int((y + h) * esize[1])
    if x1 - x0 < 16 or y1 - y0 < 16:
        return g, -1.0
    tmpl = np.ascontiguousarray(edit_img[y0:y1, x0:x1]).astype(np.float32)
    Mo = M.copy()
    Mo[:, 2] = M[:, :2] @ np.array([x0, y0]) + M[:, 2]
    try:
        cc, M2o = cv2.findTransformECC(
            tmpl, src_img.astype(np.float32), Mo.astype(np.float32), cv2.MOTION_AFFINE,
            (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, iters, 1e-5), None, 5)
    except cv2.error:
        return g, -1.0
    M2 = M2o.astype(np.float64)
    M2[:, 2] = M2o[:, 2] - M2o[:, :2] @ np.array([x0, y0])
    g2 = _from_px(M2, esize, ssize)
    if g2 is None or g2.flip != g.flip:
        return g, -1.0
    return g2, float(cc)


def estimate_template(edit_img: np.ndarray, src_img: np.ndarray, qr, flip: bool,
                      scales=None) -> tuple[Geometry | None, float]:
    """Ước lượng crop/zoom bằng template matching đa tỉ lệ: phần ``qr`` của edit nằm ở đâu trong nguồn."""
    eh, ew = edit_img.shape[:2]
    sh, sw = src_img.shape[:2]
    qx, qy, qw, qh = qr
    T = edit_img[int(round(qy * eh)):int(round((qy + qh) * eh)), int(round(qx * ew)):int(round((qx + qw) * ew))]
    if T.shape[0] < 8 or T.shape[1] < 8:
        return None, -1.0
    if flip:
        T = T[:, ::-1]
    T = np.ascontiguousarray(T)
    t_aspect = T.shape[0] / T.shape[1]
    src = src_img.astype(np.float32)
    best = (-1.0, None)
    if scales is None:  # thô -> tinh: bước 0.04 rồi ±0.03 quanh 2 tỉ lệ tốt nhất
        coarse = []
        for sc in np.arange(0.24, 1.0001, 0.04):
            g, v = estimate_template(edit_img, src_img, qr, flip, scales=[sc])
            coarse.append((v, sc))
        coarse.sort(reverse=True)
        fine = sorted({round(c + d, 3) for _, c in coarse[:2] for d in np.arange(-0.03, 0.0301, 0.01)})
        return estimate_template(edit_img, src_img, qr, flip, scales=[f for f in fine if 0.2 <= f <= 1.0])
    for s in scales:
        tw = int(round(s * sw))
        th = int(round(tw * t_aspect))
        if tw < 12 or th < 8 or tw > sw or th > sh:
            continue
        t = cv2.resize(T, (tw, th), interpolation=cv2.INTER_AREA).astype(np.float32)
        if t.std() < 2:
            continue
        r = cv2.matchTemplate(src, t, cv2.TM_CCOEFF_NORMED)
        _, v, _, loc = cv2.minMaxLoc(r)
        if v > best[0]:
            best = (float(v), (loc, tw, th))
    if best[1] is None:
        return None, -1.0
    (x, y), tw, th = best[1]
    sy = th / (qh * sh)
    ty = y / sh - qy * sy
    if not flip:
        sx = tw / (qw * sw)
        tx = x / sw - qx * sx
    else:
        sx = -tw / (qw * sw)
        tx = (x + tw) / sw + qx * tw / (qw * sw)
    return Geometry(sx, sy, tx, ty), best[0]


def refine_direct(pairs, g: Geometry, esize, ssize, cfg: AnalysisConfig, max_evals: int = 160,
                  region=None) -> tuple[Geometry, float]:
    """Tối ưu trực tiếp (sx, sy, tx, ty) theo điểm 'mọi block' trên NHIỀU cặp khung.

    Bền với phụ đề/watermark hơn ECC (chúng phạt đều mọi giả thuyết nên không kéo lệch nghiệm).
    """
    E = [p[0] for p in pairs]
    S = [p[1] for p in pairs]

    def obj(gg: Geometry) -> float:
        try:
            cv = Canvas(gg, esize, ssize, cfg, region)
        except ValueError:
            return -9.0
        Eb, Es = cv.blocks(E, "edit")
        Sb, Ss = cv.blocks(S, "source")
        vals = pair_block_sims(Eb, Es, Sb, Ss, cfg)[0]
        vals = vals[np.isfinite(vals)]
        return float(vals.mean()) if len(vals) else -9.0

    best = obj(g)
    p = np.array([g.sx, g.sy, g.tx, g.ty], np.float64)
    st = np.array([0.01 * abs(g.sx), 0.01 * abs(g.sy), 1.0 / ssize[0], 1.0 / ssize[1]])
    evals = 1
    moves = [np.eye(4)[i] for i in range(4)] + [np.array([np.sign(g.sx), 1, 0, 0])]
    while evals < max_evals and st[2] * ssize[0] > 0.12:
        improved = False
        for mv in moves:
            for sgn in (1, -1):
                q = p + sgn * mv * st
                v = obj(Geometry(*q))
                evals += 1
                if v > best + 1e-5:
                    best, p, improved = v, q, True
                    break
        if not improved:
            st = st / 2
    return Geometry(*map(float, p)), best


def estimate_orb(edit_img: np.ndarray, src_img: np.ndarray, flip: bool) -> Geometry | None:
    """Ước lượng hình học bằng ORB + RANSAC (dự phòng khi không có gợi ý tốt)."""
    e = cv2.flip(edit_img, 1) if flip else edit_img
    up_e = 640 / e.shape[1]
    up_s = 640 / src_img.shape[1]
    e2 = cv2.resize(e, None, fx=up_e, fy=up_e, interpolation=cv2.INTER_CUBIC)
    s2 = cv2.resize(src_img, None, fx=up_s, fy=up_s, interpolation=cv2.INTER_CUBIC)
    orb = cv2.ORB_create(1500)
    k1, d1 = orb.detectAndCompute(e2, None)
    k2, d2 = orb.detectAndCompute(s2, None)
    if d1 is None or d2 is None or len(k1) < 10 or len(k2) < 10:
        return None
    matches = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(d1, d2, k=2)
    good = [m for m, *rest in matches if rest and m.distance < 0.78 * rest[0].distance]
    if len(good) < 12:
        return None
    p1 = np.float32([k1[m.queryIdx].pt for m in good])
    p2 = np.float32([k2[m.trainIdx].pt for m in good])
    M, inl = cv2.estimateAffinePartial2D(p1, p2, method=cv2.RANSAC, ransacReprojThreshold=4.0)
    if M is None or inl is None or inl.sum() < 12:
        return None
    # về tọa độ chuẩn hóa
    ew, eh = e2.shape[1], e2.shape[0]
    sw, sh = s2.shape[1], s2.shape[0]
    g = _from_px(M, (ew, eh), (sw, sh))
    if g is None:
        return None
    if flip:  # bản edit đã được lật trước khi so -> ghép phép lật vào hình học
        g = Geometry(-g.sx, g.sy, g.tx + g.sx, g.ty)
    return g


# ---------------------------------------------------------------- so khớp khối

_BLUR_CACHE: "OrderedDict[tuple, np.ndarray]" = OrderedDict()
_BLUR_LOCK = threading.Lock()
_BLUR_MAX = 1500


def _blurred(img: np.ndarray, sigma: float) -> np.ndarray:
    """Ảnh float32 đã làm mờ chống răng cưa; có cache LRU cho thumbnail memmap (dùng lại rất nhiều lần)."""
    sig = round(float(sigma) * 8) / 8
    if sig <= 0.3:
        return img.astype(np.float32)
    key = None
    fname = getattr(img, "filename", None) if isinstance(img, np.memmap) else None
    if fname:
        # địa chỉ ảo có thể bị tái sử dụng sau khi memmap cũ đóng -> khóa phải gồm cả file
        key = (str(fname), img.__array_interface__["data"][0], img.shape, sig)
    mm = key is not None
    if mm:
        with _BLUR_LOCK:
            hit = _BLUR_CACHE.get(key)
            if hit is not None:
                _BLUR_CACHE.move_to_end(key)
        if hit is not None:
            return hit.astype(np.float32)
    out = cv2.GaussianBlur(img.astype(np.float32), (0, 0), sig)
    if mm:
        with _BLUR_LOCK:
            _BLUR_CACHE[key] = out.astype(np.float16)
            if len(_BLUR_CACHE) > _BLUR_MAX:
                _BLUR_CACHE.popitem(last=False)
    return out


class Canvas:
    """Lưới so sánh chung: lấy mẫu edit và nguồn trên cùng một lưới tọa độ."""

    def __init__(self, g: Geometry, esize, ssize, cfg: AnalysisConfig, region=None):
        """region: vùng CỐ ĐỊNH trên edit để chấm điểm (vùng nội dung). Phần vùng này rơi ra ngoài
        nguồn bị tính là không khớp -> hình học không thể "ăn gian" bằng cách đẩy phụ đề ra ngoài."""
        valid = g.valid_edit_rect(0.004)
        if valid is None:
            raise ValueError("hình học không hợp lệ")
        x, y, w, h = region if region is not None else valid
        ew, eh = esize
        aspect = (w * ew) / max(1e-6, h * eh)
        ch = int(round((cfg.canvas_area / aspect) ** 0.5))
        cw = int(round(ch * aspect))
        G = cfg.block_grid
        cw = max(G * 4, cw - cw % G)
        ch = max(G * 4, ch - ch % G)
        self.cw, self.ch, self.G = cw, ch, G
        u = x + (np.arange(cw) + 0.5) / cw * w
        v = y + (np.arange(ch) + 0.5) / ch * h
        U, V = np.meshgrid(u, v)
        self.emap = (U * ew - 0.5).astype(np.float32), (V * eh - 0.5).astype(np.float32)
        Xs, Ys = g.map(U, V)
        sw, sh = ssize
        self.smap = (Xs * sw - 0.5).astype(np.float32), (Ys * sh - 0.5).astype(np.float32)
        bh, bw = ch // G, cw // G
        outside = (Xs < 0) | (Xs > 1) | (Ys < 0) | (Ys > 1)
        self.block_out = outside.reshape(G, bh, G, bw).mean(axis=(1, 3)).ravel() > 0.3
        # hệ số thu nhỏ: nếu lưới thưa hơn ảnh gốc -> làm mờ trước để tránh răng cưa
        self.e_sigma = max(0.0, 0.5 * (w * ew / cw - 1))
        self.s_sigma = max(0.0, 0.5 * (abs(g.sx) * w * sw / cw - 1))

    def _sample(self, img, maps, sigma):
        return cv2.remap(_blurred(img, sigma), maps[0], maps[1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

    def blocks(self, imgs, role: str, tex_min: float = 0.0):
        """-> (n, B, P) vector khối chuẩn hóa, (n, B) độ lệch chuẩn khối."""
        maps, sigma = (self.emap, self.e_sigma) if role == "edit" else (self.smap, self.s_sigma)
        G = self.G
        bh, bw = self.ch // G, self.cw // G
        if len(imgs) == 0:
            return np.zeros((0, G * G, bh * bw), np.float32), np.zeros((0, G * G), np.float32)
        c = np.stack([self._sample(img, maps, sigma) for img in imgs])  # (n, ch, cw)
        b = c.reshape(len(imgs), G, bh, G, bw).transpose(0, 1, 3, 2, 4).reshape(len(imgs), G * G, bh * bw)
        b = b - b.mean(axis=2, keepdims=True)
        stds = np.sqrt((b ** 2).mean(axis=2)).astype(np.float32)
        out = (b / (np.maximum(stds, 1e-3)[:, :, None] * np.sqrt(bh * bw))).astype(np.float32)
        if role == "source" and self.block_out.any():  # ngoài khung nguồn = không có gì để khớp
            out[:, self.block_out] = 0
            stds[:, self.block_out] = -1.0  # dấu hiệu "ngoài nguồn"
        return out, stds


def block_sims(E, Estd, S, Sstd, cfg: AnalysisConfig, mode: str = "ver"):
    """Ma trận tương đồng CỤC BỘ (n x m) - chỉ trong cửa sổ ứng viên, không toàn cục.

    mode="ver": trung bình các block tốt nhất (bỏ phụ đề/watermark) -> dùng để KIỂM ĐỊNH.
    mode="sel": trung bình MỌI block có kết cấu -> dùng để CHỌN offset/hình học (nhân vật chuyển
    động không bị loại bỏ, tránh nhầm cảnh lia máy). mode="both" trả về (sel, ver).
    """
    if E.shape[0] > 96:  # chia khối hàng để giới hạn RAM
        parts = [block_sims(E[k:k + 96], Estd[k:k + 96], S, Sstd, cfg, mode) for k in range(0, E.shape[0], 96)]
        if mode == "both":
            return np.concatenate([p[0] for p in parts]), np.concatenate([p[1] for p in parts])
        return np.concatenate(parts)
    n, B, _ = E.shape
    m = S.shape[0]
    sims = np.empty((n, m, B), np.float32)
    for b in range(B):
        sims[:, :, b] = E[:, b, :] @ S[:, b, :].T
    tex = Estd >= cfg.static_texture_min  # (n, B) - block có kết cấu trên edit
    flat_s = Sstd < cfg.static_texture_min  # (m, B)
    # edit có kết cấu nhưng nguồn trơn -> 0
    sims = np.where(flat_s[None, :, :] & tex[:, None, :], 0.0, sims)
    texf = tex[:, None, :].astype(np.float32)
    ntex = tex.sum(axis=1)
    sel = (np.clip(sims, -0.5, 1.0) * texf).sum(axis=2) / np.maximum(ntex, 1)[:, None]
    sel[ntex < 3] = np.nan
    if mode == "sel":
        return sel
    # "ver" chỉ xét phần khung edit thực sự lấy từ nguồn: block ngoài nguồn (std nguồn = 0
    # do Canvas đánh dấu) không tính - phạt "ngoài nguồn" chỉ áp dụng cho "sel"
    out_s = (Sstd < 0.0)  # (m, B)
    use = tex[:, None, :] & ~out_s[None, :, :]
    nuse = use.sum(axis=2)  # (n, m)
    masked = np.where(use, sims, -9.0)
    keepm = np.maximum(1, np.round(nuse * cfg.block_keep)).astype(int)
    srt = -np.sort(-masked, axis=2)
    csum = np.cumsum(srt, axis=2)
    ver = np.take_along_axis(csum, (keepm - 1)[:, :, None], axis=2)[:, :, 0] / keepm
    ver = np.where(nuse >= 3, ver, np.nan).astype(np.float32)
    return (sel, ver) if mode == "both" else ver


def pair_block_sims(E, Estd, S, Sstd, cfg: AnalysisConfig):
    """Như block_sims nhưng cho TỪNG CẶP (E[k], S[k]) -> (sel[k], ver[k]); vector hóa, không tạo ma trận n x m."""
    sims = np.einsum("kbp,kbp->kb", E, S)
    tex = Estd >= cfg.static_texture_min
    flat_s = Sstd < cfg.static_texture_min
    out_s = Sstd < 0.0
    sims = np.where(flat_s & tex, 0.0, sims)
    ntex = tex.sum(axis=1)
    sel = (np.clip(sims, -0.5, 1.0) * tex).sum(axis=1) / np.maximum(ntex, 1)
    sel = np.where(ntex >= 3, sel, np.nan)
    use = tex & ~out_s
    nuse = use.sum(axis=1)
    srt = -np.sort(-np.where(use, sims, -9.0), axis=1)
    keep = np.maximum(1, np.round(nuse * cfg.block_keep)).astype(int)
    csum = np.cumsum(srt, axis=1)
    ver = np.take_along_axis(csum, (keep - 1)[:, None], axis=1)[:, 0] / keep
    ver = np.where(nuse >= 3, ver, np.nan)
    return sel.astype(np.float32), ver.astype(np.float32)


# ---------------------------------------------------------------- căn chỉnh thời gian

@dataclass
class Alignment:
    a: float  # khung nguồn (thực) tại edit frame đầu
    speed: float
    frames: np.ndarray  # khung nguồn từng khung edit
    sims: np.ndarray  # điểm từng khung
    best_alt: np.ndarray  # điểm tốt nhất bất kỳ trong cửa sổ (kiểm tra lệch cục bộ)
    score: float
    ambiguous_frames: int  # độ rộng dải offset cho điểm gần như bằng nhau
    low_info: int  # số khung không đủ kết cấu
    sims_sel: np.ndarray | None = None  # điểm "mọi block" từng khung


def line_search(M: np.ndarray, j0: int, rel_t: np.ndarray, fps_s: float, cfg: AnalysisConfig,
                n_src: int, speeds: np.ndarray | None = None, Mver: np.ndarray | None = None) -> Alignment | None:
    """Tìm đường thẳng f_i = a + speed*fps_s*t_i tối đa hóa tổng tương đồng (Hough/DP 1 chiều)."""
    n, m = M.shape
    info = ~np.isnan(M[:, 0])
    if info.sum() == 0:
        return None
    Mf = np.where(np.isnan(M), 0.0, M)
    rows = np.where(info)[0]

    def eval_speed(sp, offs):
        base = sp * fps_s * rel_t[rows]
        idx = np.floor(offs[:, None] + base[None, :] + 0.5).astype(np.int64) - j0
        inside = (idx >= 0) & (idx < m)
        vals = np.where(inside, Mf[rows[None, :], np.clip(idx, 0, m - 1)], -0.3)
        return vals.mean(axis=1)

    if speeds is None:
        speeds = np.array([1.0])
    best = (-9.0, 0.0, 1.0)
    offs_coarse = np.arange(j0 - 2, j0 + m + 2, 1.0)
    for sp in speeds:
        sc = eval_speed(sp, offs_coarse) - cfg.speed_prior * abs(sp - 1.0)
        k = int(np.argmax(sc))
        if sc[k] > best[0]:
            best = (float(sc[k]), float(offs_coarse[k]), float(sp))
    # tinh chỉnh tốc độ quanh nghiệm thô
    if len(speeds) > 1:
        fine = np.arange(max(cfg.speed_min, best[2] - 0.03), min(cfg.speed_max, best[2] + 0.03) + 1e-9, 0.005)
        for sp in fine:
            offs = np.arange(best[1] - 3, best[1] + 3.01, 0.25)
            sc = eval_speed(sp, offs) - cfg.speed_prior * abs(sp - 1.0)
            k = int(np.argmax(sc))
            if sc[k] > best[0]:
                best = (float(sc[k]), float(offs[k]), float(sp))
    # ưu tiên tốc độ "tròn" mà người dựng hay dùng nếu điểm gần như bằng nghiệm tự do
    if len(speeds) > 1 and abs(best[2] - 1.0) > 1e-6:
        free_raw = best[0] + cfg.speed_prior * abs(best[2] - 1.0)
        for csp in sorted(COMMON_SPEEDS, key=lambda v: abs(v - best[2])):
            if abs(csp - best[2]) > 0.08:
                continue
            offs = np.arange(best[1] - 4, best[1] + 4.01, 0.25)
            sc = eval_speed(csp, offs)
            k = int(np.argmax(sc))
            if sc[k] >= free_raw - 0.004:
                best = (float(sc[k]), float(offs[k]), float(csp))
                break
    sp = best[2]
    offs = np.arange(best[1] - 3, best[1] + 3.01, 0.25)
    sc = eval_speed(sp, offs)
    k = int(np.argmax(sc))
    a = float(offs[k])
    # độ mơ hồ: dải offset (theo khung) có điểm gần bằng đỉnh
    offs_wide = np.arange(j0 - 2, j0 + m + 2, 1.0)
    scw = eval_speed(sp, offs_wide)
    near = offs_wide[scw >= sc[k] - 0.004]
    ambiguous = int(near.max() - near.min()) if len(near) else 0
    frames = np.clip(np.floor(a + sp * fps_s * rel_t + 0.5).astype(np.int64), 0, n_src - 1)
    cols = np.clip(frames - j0, 0, m - 1)
    inside = (frames - j0 >= 0) & (frames - j0 < m)
    V = M if Mver is None else Mver
    sims = np.where(inside, V[np.arange(n), cols], np.nan)
    sims_sel = np.where(inside, M[np.arange(n), cols], np.nan)
    best_alt = np.nanmax(np.where(np.isnan(V), -1, V), axis=1)
    valid = sims[~np.isnan(sims)]
    score = float(np.median(valid)) if len(valid) else -1.0
    return Alignment(a, sp, frames, sims, best_alt, score, ambiguous, int((~info).sum()), sims_sel)


COMMON_SPEEDS = (0.5, 0.75, 0.8, 0.9, 1.0, 1.1, 1.2, 1.25, 1.3, 1.5, 1.75, 2.0)


def speed_grid(cfg: AnalysisConfig) -> np.ndarray:
    return np.round(np.arange(cfg.speed_min, cfg.speed_max + 1e-9, 0.02), 3)


def align_scene(edit_fp: Fingerprint, src_fp: Fingerprint, s: int, e: int, g: Geometry,
                a_hint: float, cfg: AnalysisConfig, region=None) -> tuple[Alignment | None, dict]:
    """Căn chỉnh cảnh [s, e) của edit với nguồn theo hình học g, quanh khung gợi ý a_hint."""
    canvas = Canvas(g, edit_fp.size, src_fp.size, cfg, region)
    n = e - s
    rel_t = edit_fp.pts[s:e] - edit_fp.pts[s]
    dur = float(rel_t[-1]) + 1.0 / max(edit_fp.fps, 1)
    margin = cfg.window_margin_frames
    j0 = int(max(0, np.floor(a_hint - margin - n * 0.1)))
    j1 = int(min(src_fp.n, np.ceil(a_hint + dur * src_fp.fps * cfg.speed_max + margin)))
    if j1 - j0 < 1:
        return None, {}
    E, Estd = canvas.blocks([edit_fp.thumbs[i] for i in range(s, e)], "edit")
    S, Sstd = canvas.blocks([src_fp.thumbs[j] for j in range(j0, j1)], "source")
    M, Mver = block_sims(E, Estd, S, Sstd, cfg, "both")
    # luôn dò tốc độ (phim hoạt hình giữ khung -> tốc độ 1.0 vẫn có thể cho điểm khá cao)
    al = line_search(M, j0, rel_t, src_fp.fps, cfg, src_fp.n,
                     speeds=speed_grid(cfg) if n >= cfg.speed_min_frames else None, Mver=Mver)
    return al, {"j0": j0, "j1": j1, "matrix_shape": M.shape}


def pair_score(edit_img, src_img, g: Geometry, esize, ssize, cfg: AnalysisConfig, region=None) -> float:
    canvas = Canvas(g, esize, ssize, cfg, region)
    E, Es = canvas.blocks([edit_img], "edit")
    S, Ss = canvas.blocks([src_img], "source")
    v = pair_block_sims(E, Es, S, Ss, cfg)[0][0]
    return float(v) if np.isfinite(v) else -1.0
