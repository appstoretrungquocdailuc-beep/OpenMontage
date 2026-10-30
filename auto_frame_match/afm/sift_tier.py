"""Tầng 2 cho đoạn UNMATCHED: SIFT + RANSAC (similarity) trên khung 640px.

Dùng khi chỉ mục ảnh thu nhỏ không bắt được cảnh (Douyin phóng to mạnh, phụ đề/logo che).
Nguồn: cứ ``step`` giây lấy 1 khung, SIFT tối đa ``nfeat`` điểm, lưu cache trong thư mục fingerprint.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import cv2
import numpy as np

from .fingerprint import Fingerprint
from .log import get_logger
from .media import CREATE_NO_WINDOW, ffmpeg_bin, grab_frames

log = get_logger()
_LOCK = threading.Lock()
_CACHE: dict[str, dict] = {}


def _sift(nfeat: int):
    return cv2.SIFT_create(nfeat)


def source_sift(src_path: str, fp: Fingerprint, step: float, width: int, nfeat: int, cancel=None) -> dict:
    """Điểm đặc trưng SIFT của nguồn (mỗi ``step`` giây 1 khung), có cache .npz."""
    key = f"sift_{step:g}_{width}_{nfeat}"
    path = Path(fp.root) / f"{key}.npz"
    ck = str(path)
    with _LOCK:
        if ck in _CACHE:
            return _CACHE[ck]
    if path.exists():
        z = np.load(path)
        d = {k: z[k] for k in z.files}
    else:
        k = max(1, int(round(fp.fps * step)))
        h = int(round(width * fp.size[1] / fp.size[0] / 2)) * 2
        cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error", "-i", src_path,
               "-map", "0:v:0", "-an", "-sn", "-vf", f"select='not(mod(n\\,{k}))',scale={width}:{h}:flags=area,"
               "format=gray", "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
        log.info("SIFT nguồn %s (mỗi %gs 1 khung)…", Path(src_path).name, step)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                creationflags=CREATE_NO_WINDOW)
        sift = _sift(nfeat)
        fidx, offs, pts_all, desc_all = [], [0], [], []
        j = 0
        while True:
            if cancel is not None and cancel.is_set():
                proc.kill()
                raise InterruptedError("Đã hủy")
            buf = proc.stdout.read(width * h)
            if len(buf) < width * h:
                break
            img = np.frombuffer(buf, np.uint8).reshape(h, width)
            kp, ds = sift.detectAndCompute(img, None)
            fidx.append(min(j * k, fp.n - 1))
            if ds is not None:
                pts_all.append(np.float32([p.pt for p in kp]))
                desc_all.append(np.clip(ds, 0, 255).astype(np.uint8))
                offs.append(offs[-1] + len(kp))
            else:
                offs.append(offs[-1])
            j += 1
        proc.wait()
        d = {"frame": np.array(fidx, np.int64), "offs": np.array(offs, np.int64),
             "pts": np.concatenate(pts_all) if pts_all else np.zeros((0, 2), np.float32),
             "desc": np.concatenate(desc_all) if desc_all else np.zeros((0, 128), np.uint8),
             "size": np.array([width, h])}
        tmp = path.with_suffix(".part.npz")
        np.savez(tmp, **d)
        tmp.replace(path)
    with _LOCK:
        _CACHE[ck] = d
    return d


def _score(kp_pts, desc, sd: dict, j: int, ratio: float, bf) -> tuple[int, np.ndarray | None]:
    a, b = int(sd["offs"][j]), int(sd["offs"][j + 1])
    if b - a < 8 or desc is None or len(desc) < 8:
        return 0, None
    m = bf.knnMatch(desc, sd["desc"][a:b].astype(np.float32), k=2)
    good = [x[0] for x in m if len(x) == 2 and x[0].distance < ratio * x[1].distance]
    if len(good) < 8:
        return 0, None
    src = kp_pts[[g.queryIdx for g in good]]
    dst = sd["pts"][a:b][[g.trainIdx for g in good]]
    M, mask = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=4.0)
    if M is None or mask is None:
        return 0, None
    return int(mask.sum()), M


def match_segment(edit_path: str, edit_times: list[float], src_list: list[tuple[str, dict, float, tuple]],
                  cfg, edit_mask_bottom: float) -> dict | None:
    """Tìm vị trí nguồn cho 1 đoạn bằng SIFT.

    src_list: [(sid, sift_data, fps, (j_lo, j_hi) dải mẫu được phép)].
    Trả về {sid, f_start (khung nguồn tại edit_times[0]), M (edit px -> nguồn px), sizes, inliers, ratio, flip}.
    """
    W = cfg.sift_width
    imgs = grab_frames(edit_path, edit_times, W)
    sift = _sift(cfg.sift_nfeat)
    bf = cv2.BFMatcher()
    per_frame = []
    for t, img in zip(edit_times, imgs):
        if img is None:
            continue
        mask = np.full(img.shape, 255, np.uint8)
        mask[int(img.shape[0] * (1 - edit_mask_bottom)):] = 0  # phụ đề cứng
        best = []
        for flip in (False, True):
            im = img[:, ::-1].copy() if flip else img
            mk = mask[:, ::-1].copy() if flip else mask
            kp, ds = sift.detectAndCompute(im, mk)
            if ds is None or len(kp) < 8:
                continue
            kpp = np.float32([p.pt for p in kp])
            ds = ds.astype(np.float32)
            for sid, sd, fps, (lo, hi) in src_list:
                for j in range(lo, hi):
                    inl, M = _score(kpp, ds, sd, j, cfg.sift_ratio, bf)
                    if inl:
                        best.append((inl, sid, int(sd["frame"][j]), M, flip, fps, tuple(sd["size"])))
            if best and max(b[0] for b in best) >= cfg.sift_min_inliers:
                break  # không cần thử lật
        if not best:
            continue
        best.sort(key=lambda x: -x[0])
        top = best[0]
        # ứng viên thứ hai ở vị trí khác hẳn (> 2 giây hoặc nguồn khác)
        second = next((b[0] for b in best[1:] if b[1] != top[1] or abs(b[2] - top[2]) > 2 * top[5]), 0)
        per_frame.append((t, top, second, img.shape))
        log.debug("SIFT t=%.2f: %s@%d %d inlier (thứ hai %d)", t, top[1], top[2], top[0], second)
    t0 = edit_times[0]

    def est_of(p):  # khung nguồn tại đầu đoạn (giả định tốc độ 1)
        return p[1][2] - (p[0] - t0) * p[1][5]

    def consensus(group):
        """Các khung cùng nguồn chỉ về cùng thời điểm (±1 giây quanh trung vị)."""
        if not group:
            return []
        sid = max({p[1][1] for p in group}, key=lambda x: sum(1 for p in group if p[1][1] == x))
        g = [p for p in group if p[1][1] == sid]
        med = float(np.median([est_of(p) for p in g]))
        return [p for p in g if abs(est_of(p) - med) <= g[0][1][5]]

    # 1) khung đủ inlier VÀ nổi trội (>= 1.5 lần ứng viên thứ hai)
    ok = consensus([p for p in per_frame if p[1][0] >= cfg.sift_min_inliers
                    and p[1][0] >= cfg.sift_unique * max(1, p[2])])
    # 2) cảnh nền giữ lâu: ứng viên thứ hai (cùng cảnh, khác thời điểm) cũng nhiều inlier ->
    #    chấp nhận khi các khung đủ inlier đồng thuận cùng một thời điểm
    if len(ok) < 2:
        ok = consensus([p for p in per_frame if p[1][0] >= cfg.sift_min_inliers])
    if len(ok) < min(2, len(edit_times)):  # không chấp nhận 1 khung giống làm bằng chứng
        if per_frame:
            b = max(per_frame, key=lambda p: p[1][0])
            log.info("SIFT: không đủ bằng chứng (tốt nhất %d inlier, thứ hai %d, %d khung đồng thuận)",
                     b[1][0], b[2], len(ok))
        return None
    f0 = float(np.median([est_of(p) for p in ok]))
    bestp = max(ok, key=lambda p: p[1][0])
    inl, sid, f, M, flip, fps, ssize = bestp[1]
    return {"sid": sid, "f_start": f0, "M": M, "flip": flip, "edit_size": (bestp[3][1], bestp[3][0]),
            "src_size": ssize, "inliers": inl, "second": bestp[2], "n_frames_ok": len(ok)}
