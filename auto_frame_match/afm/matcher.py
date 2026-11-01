"""Tầng 2-4: tìm nguồn ứng viên, so khớp chính xác từng khung, kiểm định & tính liên tục."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import numpy as np

from .align import (Alignment, Canvas, Geometry, _from_px, align_scene, estimate_orb, estimate_template, geometry_from_windows,
                    pair_block_sims, pair_score, refine_direct, refine_ecc)
from .config import AnalysisConfig
from .fingerprint import Fingerprint, crop_norm, window_feature
from .index import SourceIndex
from .log import get_logger
from .media import grab_frames

log = get_logger()

GREEN, YELLOW, RED = "XANH", "VANG", "DO"
AMBIG = ("NHIỀU NGUỒN", "CẢNH TĨNH", "MƠ HỒ THỜI GIAN", "MƠ HỒ TỐC ĐỘ")


@dataclass
class Candidate:
    sid: str
    a: float
    geom: Geometry
    votes: float
    ref_edit: int
    ref_src: int
    align: Alignment | None = None
    extra: dict = field(default_factory=dict)

    @property
    def score(self) -> float:
        return self.align.score if self.align is not None else -1.0


@dataclass
class Segment:
    start: int
    end: int
    status: str = RED
    reasons: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    sid: str | None = None
    geom: Geometry | None = None
    a: float = 0.0
    speed: float = 1.0
    t_ref: float = 0.0  # thời điểm edit mà tại đó khung nguồn = a
    frames: np.ndarray | None = None
    sims: np.ndarray | None = None
    score: float = -1.0
    candidates: list = field(default_factory=list)
    ambiguous_frames: int = 0
    merged_cuts: list = field(default_factory=list)
    frame_status: list = field(default_factory=list)
    options: list = field(default_factory=list)  # ứng viên còn mơ hồ: (sid, Geometry, score)
    method: str = "match"  # match | sift | interp | freeze
    frame_map: np.ndarray | None = None  # V2: khung nguồn từng khung khi tốc độ thay đổi (DTW)
    local_speed: np.ndarray | None = None  # V2: tốc độ cục bộ từng khung
    interp_pair: np.ndarray | None = None  # V2: khung edit là ảnh nội suy giữa frames[k] và interp_pair[k]
    alts: list = field(default_factory=list)  # ứng viên đã căn chỉnh (sid, Geometry, Alignment, rank) cho DP

    @property
    def n(self) -> int:
        return self.end - self.start


class _WindowSource:
    """Nguồn chỉ nạp một dải khung ở độ phân giải cao (thay cho thumbnail) - dùng cho xác minh 640px."""

    def __init__(self, fp: Fingerprint, frames: list, j0: int):
        self.fp, self.j0, self._frames = fp, j0, frames
        self.n, self.fps, self.pts = fp.n, fp.fps, fp.pts
        self.size = (frames[0].shape[1], frames[0].shape[0])
        outer = self

        class _T:
            def __getitem__(self, j):
                k = int(j) - outer.j0
                k = min(max(k, 0), len(outer._frames) - 1)  # ngoài dải: dùng khung biên (điểm thấp tự nhiên)
                return outer._frames[k]

        self.thumbs = _T()


class Matcher:
    def __init__(self, edit: Fingerprint, sources: dict[str, Fingerprint], cfg: AnalysisConfig,
                 content_rect, edit_path: str, source_paths: dict[str, str],
                 progress=None, cancel: threading.Event | None = None):
        self.edit, self.sources, self.cfg = edit, sources, cfg
        self.content_rect = content_rect
        self.edit_path, self.source_paths = edit_path, source_paths
        self.progress = progress or (lambda f, m="": None)
        self.cancel = cancel
        self.index = SourceIndex(sources)
        self.qwins = self._query_windows()

    def plausible(self, g: Geometry | None, sid: str) -> bool:
        """Hình học hợp lý: co giãn gần ĐỀU theo 2 trục (Douyin chỉ kéo giãn nhẹ). Chặn ánh xạ méo kiểu
        sx=1.65/sy=0.44 mà bộ tối ưu tự do có thể 'uốn' cho vừa một cảnh lạ."""
        if g is None:
            return False
        ew, eh = self.edit.size
        fp = self.sources[sid]
        sw, sh = fp.size
        r = (abs(g.sx) / max(1e-6, g.sy)) / ((ew / eh) / (sw / sh))
        return 1 / self.cfg.max_anisotropy <= r <= self.cfg.max_anisotropy

    # ------------------------------------------------------------ tầng 2
    def _query_windows(self):
        wins = [("full", (0.0, 0.0, 1.0, 1.0))]
        x, y, w, h = self.content_rect
        if w * h < 0.92:
            wins.append(("content", (x, y, w, h)))
        ew, eh = self.edit.size
        aspect = ew / eh
        if aspect < 1.0:  # khung dọc (Douyin): thử dải 16:9 / 4:3 rộng hết khung, ở giữa khung và giữa vùng nội dung
            for name, ar in (("band169", 9 / 16), ("band43", 3 / 4)):
                bh = min(1.0, ar * aspect)
                for cy in (0.5, y + h / 2):
                    r = (0.0, min(max(cy - bh / 2, 0.0), 1 - bh), 1.0, bh)
                    if all(abs(r[1] - q[1][1]) > 0.01 or abs(r[3] - q[1][3]) > 0.01 for q in wins):
                        wins.append((name, r))
        return wins

    def _query_vectors(self, i: int):
        img = self.edit.thumbs[i]
        out, meta = [], []
        for qi, (name, r) in enumerate(self.qwins):
            crop = crop_norm(img, (name, *r))
            for flip in (False, True):
                c = crop[:, ::-1] if flip else crop
                out.append(window_feature(np.ascontiguousarray(c), self.cfg.feat_side))
                meta.append((qi, flip))
        return out, meta

    def find_candidates(self, s: int, e: int, max_cand: int = 4) -> list[Candidate]:
        cfg = self.cfg
        n = e - s
        q_frames = np.unique(np.linspace(s, e - 1, min(n, cfg.query_per_scene)).astype(int))
        vecs, metas = [], []
        for i in q_frames:
            v, m = self._query_vectors(int(i))
            vecs += v
            metas += [(int(i),) + mm for mm in m]
        vecs = np.stack(vecs)
        good = np.linalg.norm(vecs, axis=1) > 0.5
        if not good.any():
            return []
        vecs = vecs[good]
        metas = [m for m, g in zip(metas, good) if g]
        sims, ids = self.index.search(vecs, cfg.topk)
        te0 = self.edit.pts[s]
        votes: dict = {}
        for qk, (i, qi, flip) in enumerate(metas):
            for sim, idx in zip(sims[qk], ids[qk]):
                if idx < 0 or sim < 0.55:
                    continue
                src_k, f, wi = self.index.meta[idx]
                sid = self.index.sids[src_k]
                fp = self.sources[sid]
                a = f - fp.fps * (self.edit.pts[i] - te0)
                key = (sid, int(round(a / 3.0)))
                rec = votes.setdefault(key, {"frames": {}, "best": None})
                if sim > rec["frames"].get(i, 0):
                    rec["frames"][i] = float(sim)
                if rec["best"] is None or sim > rec["best"][0]:
                    rec["best"] = (float(sim), i, int(f), qi, int(wi), flip, float(a))
        scored = []
        for key, rec in votes.items():
            scored.append((sum(rec["frames"].values()), key, rec))
        scored.sort(key=lambda x: -x[0])
        cands: list[Candidate] = []
        used = []
        for total, (sid, abin), rec in scored:
            if any(sid == u[0] and abs(abin - u[1]) <= 2 for u in used):
                # gộp phiếu vào cụm lân cận đã chọn
                continue
            used.append((sid, abin))
            sim, i, f, qi, wi, flip, a = rec["best"]
            g = geometry_from_windows(self.qwins[qi][1], self.sources[sid].windows[wi], flip)
            cands.append(Candidate(sid, a, g, total, i, f, extra={"n_vote_frames": len(rec["frames"])}))
            if len(cands) >= max_cand:
                break
        return cands

    # ------------------------------------------------------------ tầng 3
    def _refine_geometry(self, c: Candidate) -> Geometry:
        """Ứng viên hình học: gợi ý cửa sổ + template matching đa tỉ lệ (2 chiều lật) -> ECC -> chọn tốt nhất."""
        fp = self.sources[c.sid]
        e_img = self.edit.thumbs[c.ref_edit]
        cfg = self.cfg
        best_g, best_s = c.geom, -2.0

        def consider(g, s_img):
            nonlocal best_g, best_s
            if g is None or not self.plausible(g, c.sid):
                return
            try:
                sc = pair_score(e_img, s_img, g, self.edit.size, fp.size, cfg)
            except ValueError:
                return
            if sc > best_s:
                best_g, best_s = g, sc
            if sc < best_s - 0.05:  # giả thuyết kém xa -> không tốn công tinh chỉnh ECC
                return
            gg = refine_ecc(e_img, s_img, g, iters=50)[0]
            try:
                sc2 = pair_score(e_img, s_img, gg, self.edit.size, fp.size, cfg)
            except ValueError:
                return
            if sc2 > best_s:
                best_g, best_s = gg, sc2

        f0 = min(max(c.ref_src, 0), fp.n - 1)
        s_img = fp.thumbs[f0]
        consider(c.geom, s_img)
        # template matching: thử mọi vùng truy vấn (toàn khung / vùng nội dung / dải 16:9), 2 chiều lật
        tm = []
        for _, qr in self.qwins:
            for flip in (False, True):
                g, v = estimate_template(e_img, s_img, qr, flip)
                if g is not None:
                    tm.append((v, g, qr, flip))
        tm.sort(key=lambda x: -x[0])
        for _, g, _, _ in tm[:3]:
            consider(g, s_img)
        # khung truy vấn có thể lệch thời gian (cảnh lia máy: lệch thời gian ~ lệch crop)
        # -> thử các khung nguồn lân cận, chọn cặp (khung, hình học) cho điểm cao nhất
        if tm and best_s < 0.97:
            _, _, qr, flip = tm[0]
            best_f = f0
            for f in range(f0 - 8, f0 + 9):
                if f == f0 or not 0 <= f < fp.n:
                    continue
                g, v = estimate_template(e_img, fp.thumbs[f], qr, flip)
                if g is None:
                    continue
                before = best_s
                consider(g, fp.thumbs[f])
                if best_s > before:
                    best_f = f
            c.ref_src = best_f
        if best_s < 0.6:  # dự phòng ORB
            for flip in (False, True):
                consider(estimate_orb(e_img, s_img, flip), s_img)
        c.extra["geom_score"] = best_s
        return best_g

    def _polish_geometry(self, s: int, c: Candidate) -> Geometry | None:
        """Tinh chỉnh hình học trên nhiều cặp khung đã căn thời gian (tối ưu trực tiếp)."""
        al, fp = c.align, self.sources[c.sid]
        ok = np.where(np.nan_to_num(al.sims, nan=-1) >= self.cfg.frame_min)[0]
        if len(ok) < 2:
            return None
        pick = ok[np.linspace(0, len(ok) - 1, min(5, len(ok))).astype(int)]
        pairs = [(self.edit.thumbs[s + k], fp.thumbs[al.frames[k]]) for k in pick]
        g, _ = refine_direct(pairs, c.geom, self.edit.size, fp.size, self.cfg,
                             region=c.geom.valid_edit_rect(0.004))  # vùng cố định: không 'ăn gian'
        return g

    def evaluate(self, s: int, e: int, c: Candidate) -> Candidate:
        fp = self.sources[c.sid]
        c.geom = self._refine_geometry(c)
        if not self.plausible(c.geom, c.sid):
            c.align = None
            return c
        try:
            c.align, dbg = align_scene(self.edit, fp, s, e, c.geom, c.a, self.cfg)
        except ValueError:
            c.align = None
            return c
        if c.align is not None and c.align.score > 0.5:
            g2 = self._polish_geometry(s, c)
            if g2 is not None and self.plausible(g2, c.sid) and not g2.close_to(c.geom, 0.004):
                try:
                    al2, _ = align_scene(self.edit, fp, s, e, g2, c.align.a, self.cfg, c.geom.valid_edit_rect(0.004))
                except ValueError:
                    al2 = None
                if al2 is not None and np.nanmean(al2.sims_sel) >= np.nanmean(c.align.sims_sel):
                    c.geom, c.align = g2, al2
        return c

    # ------------------------------------------------------------ tầng 4 (phần cảnh)
    def _same_content(self, c1: Candidate, c2: Candidate) -> float:
        """Hai ứng viên có cùng nội dung hình (cảnh lặp/trùng giữa các nguồn)?"""
        f1, f2 = self.sources[c1.sid], self.sources[c2.sid]
        n = len(c1.align.frames)
        ks = np.unique(np.linspace(0, n - 1, min(5, n)).astype(int))
        scores = []
        for k in ks:
            a, b = f1.thumbs[c1.align.frames[k]], f2.thumbs[c2.align.frames[k]]
            try:
                scores.append(pair_score(a, b, Geometry(1, 1, 0, 0), f1.size, f2.size, self.cfg))
            except ValueError:
                pass
        return float(np.median(scores)) if scores else 0.0

    def match_scene(self, s: int, e: int, depth: int = 0) -> list[Segment]:
        cfg = self.cfg
        seg = Segment(s, e)
        cands = self.find_candidates(s, e)
        if not cands:
            seg.notes.append("UNMATCHED: không có ứng viên nào trong chỉ mục")
            return [self._finish(seg)]
        evald = [self.evaluate(s, e, c) for c in cands]
        evald = [c for c in evald if c.align is not None]
        evald.sort(key=lambda c: -self._rank(c))
        if not evald or evald[0].score < cfg.scene_match:
            seg.notes.append(
                f"UNMATCHED: điểm tốt nhất {evald[0].score:.3f} < {cfg.scene_match}" if evald
                else "UNMATCHED: không căn chỉnh được")
            seg.candidates = [self._cand_info(c) for c in evald[:3]]
            # có thể là cảnh dài chứa điểm cắt bị bỏ sót -> thử chia đôi
            if depth < 2 and e - s >= 12 and evald and evald[0].score > 0.3:
                return self._split_retry(seg, evald[0], depth, force_half=True) or [self._finish(seg)]
            return [self._finish(seg)]
        best = evald[0]
        # ứng viên chỉ giải thích được một phần cảnh -> tìm thêm ứng viên từ chính các khung lỗi
        bad = np.where(np.nan_to_num(best.align.sims, nan=1.0) < cfg.frame_ok)[0]
        if len(bad) >= 2 and depth == 0:
            extra = self.find_candidates(s + int(bad[0]), s + int(bad[-1]) + 1, max_cand=3)
            for c in extra:
                if any(c.sid == o.sid and abs(c.a - o.a) < 4 for o in evald):
                    continue
                # quy offset về đầu cảnh
                c.a -= self.sources[c.sid].fps * (self.edit.pts[s + int(bad[0])] - self.edit.pts[s])
                c = self.evaluate(s, e, c)
                if c.align is not None:
                    evald.append(c)
            evald.sort(key=lambda c: -self._rank(c))
            best = evald[0]
        seg.candidates = [self._cand_info(c) for c in evald[:3]]
        self._apply(seg, best)
        # điểm cắt bị bỏ sót: có đoạn khung xấu liền nhau -> chia nhỏ và so lại
        if depth < 3:
            res = self._split_retry(seg, best, depth)
            if res:
                return res
        # ứng viên thứ hai có điểm sát nút
        for alt in evald[1:]:
            # mơ hồ khi ứng viên khác sát nút trên CẢ hai thước đo (khối tốt nhất & mọi khối)
            if alt.score >= best.score - cfg.ambiguity_margin and \
                    self._rank(alt) >= self._rank(best) - cfg.ambiguity_margin and \
                    (alt.sid != best.sid or abs(alt.align.a - best.align.a) > 3):
                same = self._same_content(best, alt)
                seg.notes.append(
                    f"NHIỀU NGUỒN: {alt.sid}@{alt.align.a:.0f} điểm {alt.score:.3f} "
                    f"(nội dung giống {same:.2f})")
                seg.options.append((alt.sid, alt.geom, alt.score))
        if best.align.ambiguous_frames > 2:
            seg.notes.append(f"CẢNH TĨNH: offset mơ hồ ±{best.align.ambiguous_frames} khung")
        seg.options.insert(0, (best.sid, best.geom, best.score))
        seg.alts = [(c.sid, c.geom, c.align, self._rank(c)) for c in evald[:5]
                    if c.align is not None and len(c.align.frames) == seg.n]
        return [self._finish(seg)]

    def _rank(self, c: Candidate) -> float:
        """Xếp hạng ứng viên: trung bình điểm 'mọi block' trên toàn cảnh (khung ngoài nguồn = -0.3)."""
        if c.align is None or c.align.sims_sel is None:
            return -9.0
        v = np.where(np.isfinite(c.align.sims_sel), c.align.sims_sel, -0.3)
        return float(v.mean()) if np.isfinite(c.align.sims_sel).any() else -9.0

    def _cand_info(self, c: Candidate) -> dict:
        al = c.align
        return {"source_id": c.sid, "score": round(c.score, 4),
                "source_frame_start": int(al.frames[0]) if al is not None else None,
                "speed": round(al.speed, 4) if al is not None else None,
                "flip": c.geom.flip, "votes": round(c.votes, 2)}

    def _apply(self, seg: Segment, c: Candidate):
        al = c.align
        seg.sid, seg.geom, seg.a, seg.speed = c.sid, c.geom, al.a, al.speed
        seg.t_ref = float(self.edit.pts[seg.start])
        seg.frames, seg.sims, seg.score = al.frames.copy(), al.sims.copy(), al.score
        seg.frame_map = None if al.frame_map is None else al.frame_map.copy()
        seg.local_speed = None if al.local_speed is None else al.local_speed.copy()
        self._speed_note(seg, al)
        seg.ambiguous_frames = al.ambiguous_frames

    def _split_retry(self, seg: Segment, best: Candidate, depth: int, force_half: bool = False):
        cfg = self.cfg
        n = seg.n
        if force_half:
            mid = seg.start + n // 2
            out = []
            for a, b in ((seg.start, mid), (mid, seg.end)):
                out += self.match_scene(a, b, depth + 1)
            if any(x.status != RED for x in out):
                return out
            return None
        sims = np.nan_to_num(best.align.sims, nan=1.0)  # khung trơn xử lý riêng
        bad = sims < cfg.frame_min
        if not bad.any() or bad.all():
            return None
        runs, k = [], 0
        while k < n:
            if bad[k]:
                j = k
                while j < n and bad[j]:
                    j += 1
                runs.append((k, j))
                k = j
            else:
                k += 1
        long_runs = [r for r in runs if r[1] - r[0] >= 2]
        if not long_runs or (~bad).sum() < cfg.min_evidence_frames:
            return None
        cuts = sorted({seg.start + x for r in long_runs for x in r} - {seg.start, seg.end})
        bounds = [seg.start] + cuts + [seg.end]
        log.info("Cảnh %d-%d: phát hiện điểm cắt bị bỏ sót -> chia %s", seg.start, seg.end, cuts)
        out = []
        for a, b in zip(bounds[:-1], bounds[1:]):
            if b > a:
                out += self.match_scene(a, b, depth + 1)
        return out

    def _flat_ok(self, ei: int, sid: str, f: int) -> bool:
        e = self.edit.thumbs[ei]
        s = self.sources[sid].thumbs[f]
        return float(e.std()) < 4 and float(s.std()) < 4

    def _finish(self, seg: Segment) -> Segment:
        """Gán trạng thái khung/cảnh theo ngưỡng kiểm định."""
        cfg = self.cfg
        if seg.sid is not None and seg.method in ("sift", "interp", "freeze"):
            # không xác minh được bằng hình ở độ phân giải phân tích -> luôn VÀNG
            seg.status = YELLOW
            seg.reasons = list(seg.notes)
            seg.frame_status = [YELLOW] * seg.n
            return seg
        if seg.sid is not None:  # không đủ bằng chứng -> không được gán nguồn (kể cả trạng thái VÀNG)
            v = np.asarray(seg.sims, dtype=np.float64)
            fin = v[np.isfinite(v)]
            okr = float((fin >= cfg.frame_ok).mean()) if len(fin) else 0.0
            why = None
            if seg.score < cfg.scene_match:
                why = f"điểm {seg.score:.3f} < {cfg.scene_match}"
            elif okr < cfg.match_ok_ratio:
                why = f"chỉ {okr:.0%} khung đạt ngưỡng {cfg.frame_ok} (< {cfg.match_ok_ratio:.0%})"
            if why:
                seg.notes.append(f"UNMATCHED: {why} sau kiểm định (ứng viên {seg.sid}@{int(seg.frames[0])} bị loại)")
                seg.sid, seg.frames, seg.sims, seg.geom = None, None, None, None
        if seg.sid is None:
            seg.reasons = list(seg.notes)
            seg.status = RED
            seg.frame_status = [RED] * seg.n
            return seg
        fs = []
        for k in range(seg.n):
            v = seg.sims[k]
            if not np.isfinite(v):
                fs.append(GREEN if self._flat_ok(seg.start + k, seg.sid, int(seg.frames[k])) else YELLOW)
            elif v >= cfg.frame_ok:
                fs.append(GREEN)
            elif v >= cfg.frame_min:
                fs.append(YELLOW)
            else:
                fs.append(RED)
        seg.frame_status = fs
        n_ok = sum(1 for x in fs if x == GREEN)
        n_red = sum(1 for x in fs if x == RED)
        ok_ratio = n_ok / max(1, seg.n)
        reasons = list(seg.notes)
        status = GREEN
        if seg.score < cfg.scene_green:
            status = YELLOW
            reasons.append(f"điểm median {seg.score:.3f} < {cfg.scene_green}")
        if ok_ratio < cfg.min_ok_ratio:
            status = YELLOW
            reasons.append(f"chỉ {ok_ratio:.0%} khung đạt")
        if n_red:
            status = YELLOW
            reasons.append(f"{n_red} khung không khớp")
        textured = int(np.isfinite(seg.sims).sum())
        if textured < cfg.min_evidence_frames:
            status = YELLOW
            reasons.append(f"chỉ {textured} khung làm bằng chứng (< {cfg.min_evidence_frames})")
        if any(r.startswith(AMBIG) for r in seg.notes):
            status = YELLOW
        seg.status = status
        seg.reasons = reasons
        if status != GREEN:  # khung trong cảnh chưa kiểm định không được coi là XANH
            seg.frame_status = [YELLOW if x == GREEN else x for x in fs]
        return seg

    # ------------------------------------------------------------ toàn bộ
    def run(self, scenes: list[tuple[int, int]]) -> list[Segment]:
        segs: list[Segment] = []
        done = [0]
        lock = threading.Lock()

        def work(se):
            if self.cancel is not None and self.cancel.is_set():
                raise InterruptedError("Đã hủy")
            r = self.match_scene(*se)
            with lock:
                done[0] += 1
                self.progress(0.88 * done[0] / max(1, len(scenes)), f"so khớp cảnh {done[0]}/{len(scenes)}")
            return r

        # các cảnh độc lập -> chạy song song (OpenCV/NumPy/FAISS nhả GIL khi tính toán nặng)
        workers = max(1, min(self.cfg.workers or (os.cpu_count() or 2), 8, len(scenes)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for r in ex.map(work, scenes):
                segs += r
        segs.sort(key=lambda x: x.start)
        if self.cfg.dp_enable:
            self.progress(0.89, "DP theo thứ tự thời gian")
            self.viterbi(segs)
        self.progress(0.9, "kiểm tra tính liên tục")
        self.refine_boundaries(segs)
        segs = [x for x in segs if x.n > 0]
        self.progress(0.92, "đồng thuận hình học & phép thử mơ hồ thời gian")
        self.consensus_geometry(segs)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(self.ambiguity_test, segs))
        for sg in segs:
            self.static_view_test(sg)
        self.resolve_continuity(segs)
        segs = self.merge_continuous(segs)
        if self.cfg.sift_enable and any(x.sid is None for x in segs):
            self.progress(0.94, "tầng 2: SIFT cho đoạn ĐỎ")
            self.sift_tier(segs)
            segs = self.merge_continuous(segs)
        if self.cfg.hires_zoom_verify:
            self.progress(0.945, "xác minh đoạn phóng to ở 640px")
            for sg in segs:
                self.hires_align(sg)
            for sg in segs:
                self.static_view_test(sg)
            segs = self.merge_continuous(segs)
        if self.cfg.hires_refine:
            self.progress(0.95, "tinh chỉnh crop ở độ phân giải cao")
            for sg in segs:
                if sg.sid is not None:
                    self.hires_geometry(sg)
        if self.cfg.detect_interpolated_frames:
            self.detect_interpolated(segs)
        if self.cfg.interpolate_unmatched:
            self.interpolate(segs)
        self.progress(1.0, "xong")
        return segs

    # ------------------------------------------------------------ P1: DP theo thứ tự thời gian
    def viterbi(self, segs: list[Segment]):
        """Chọn ứng viên cho chuỗi đoạn tối đa hóa tổng điểm - phạt nhảy thời gian (Viterbi).

        Chỉ xét ứng viên có điểm sát ứng viên tốt nhất (dp_window): DP phân xử nhập nhằng
        (cảnh lặp, mặt nhân vật xuất hiện nhiều lần), không lật ngược kết quả khớp rõ ràng.
        """
        cfg = self.cfg
        idx = [k for k, sg in enumerate(segs) if sg.sid is not None and sg.method == "match" and sg.alts]
        if len(idx) < 2:
            return
        cands = []
        for k in idx:
            sg = segs[k]
            top = max(a[3] for a in sg.alts)
            cs = [a for a in sg.alts if a[2].score >= cfg.scene_match and a[3] >= top - cfg.dp_window]
            cur = next((a for a in sg.alts if a[0] == sg.sid and abs(a[2].a - sg.a) < 0.5), None)
            if cur is not None and cur not in cs:
                cs.insert(0, cur)
            cands.append(cs or sg.alts[:1])
        if all(len(c) == 1 for c in cands):
            return

        def t_of(sid, al):
            return al.a / self.sources[sid].fps

        INF = -1e9
        dp = [[INF] * len(c) for c in cands]
        bp = [[-1] * len(c) for c in cands]
        for m, c in enumerate(cands[0]):
            dp[0][m] = c[3]
        for i in range(1, len(cands)):
            te_prev = float(self.edit.pts[segs[idx[i - 1]].start])
            te_cur = float(self.edit.pts[segs[idx[i]].start])
            for m, (sid2, _, al2, s2) in enumerate(cands[i]):
                for k, (sid1, _, al1, _) in enumerate(cands[i - 1]):
                    if dp[i - 1][k] <= INF / 2:
                        continue
                    if sid1 != sid2:
                        pen = 0.3
                    else:
                        gap = t_of(sid2, al2) - (t_of(sid1, al1) + (te_cur - te_prev) * al1.speed)
                        tol = 2.0 / self.sources[sid2].fps
                        pen = 0.0 if abs(gap) <= tol else (0.3 if gap > 0 else 1.0 + min(5.0, -gap))
                    v = dp[i - 1][k] + s2 - cfg.dp_lambda * pen * 10
                    if v > dp[i][m]:
                        dp[i][m], bp[i][m] = v, k
        m = int(np.argmax(dp[-1]))
        path = [m]
        for i in range(len(cands) - 1, 0, -1):
            m = bp[i][m]
            path.append(m)
        path = path[::-1]

        def continuous(i, j):  # ứng viên đã chọn của 2 đoạn i, j (i < j) liền mạch thời gian?
            sid1, _, al1, _ = cands[i][path[i]]
            sid2, _, al2, _ = cands[j][path[j]]
            if sid1 != sid2:
                return False
            dt = float(self.edit.pts[segs[idx[j]].start] - self.edit.pts[segs[idx[i]].start])
            gap = t_of(sid2, al2) - (t_of(sid1, al1) + dt * al1.speed)
            return abs(gap) <= 2.0 / self.sources[sid2].fps

        for i, (k, m) in enumerate(zip(idx, path)):
            sg = segs[k]
            sid, geom, al, _ = cands[i][m]
            if sid == sg.sid and abs(al.a - sg.a) < 0.5:
                continue
            def firm(j):  # đoạn kề không tự mang cờ mơ hồ
                return not any(r.startswith(AMBIG) for r in segs[idx[j]].notes)

            linked = (i > 0 and continuous(i - 1, i) and firm(i - 1)) or \
                     (i + 1 < len(cands) and continuous(i, i + 1) and firm(i + 1))
            old = f"{sg.sid}@{int(sg.frames[0])}"
            sg.sid, sg.geom, sg.a, sg.speed = sid, geom, al.a, al.speed
            sg.t_ref = float(self.edit.pts[sg.start])
            sg.frames, sg.sims, sg.score = al.frames.copy(), al.sims.copy(), al.score
            sg.frame_map = None if al.frame_map is None else al.frame_map.copy()
            sg.local_speed = None if al.local_speed is None else al.local_speed.copy()
            sg.ambiguous_frames = al.ambiguous_frames
            if linked:  # liền mạch với đoạn kề -> tính liên tục xác nhận, gỡ cờ mơ hồ
                sg.notes = [r for r in sg.notes if not r.startswith(AMBIG)]
            sg.notes.append(f"DP: chọn {sid}@{int(al.frames[0])} theo chuỗi thời gian (thay {old})"
                            + ("" if linked else " - không liền mạch với đoạn kề, vẫn nghi vấn"))
            log.info("DP đoạn %d-%d: %s -> %s@%d", sg.start, sg.end, old, sid, int(al.frames[0]))
            self._finish(sg)

    # ------------------------------------------------------------ P1: tầng 2 SIFT
    def _neighbor(self, segs, k, step):
        j = k + step
        while 0 <= j < len(segs):
            if segs[j].sid is not None:
                return segs[j]
            j += step
        return None

    def sift_tier(self, segs: list[Segment]):
        from .sift_tier import match_segment, source_sift

        cfg = self.cfg
        sdata = {}
        for k, sg in enumerate(segs):
            if sg.sid is not None:
                continue
            if sg.n < cfg.min_evidence_frames:
                sg.red_class = self.classify_red(segs, k)
                sg.notes.append(f"TẦNG 7: phân loại = {sg.red_class}")
                self._finish(sg)
                continue
            if self.cancel is not None and self.cancel.is_set():
                raise InterruptedError("Đã hủy")
            # Tầng 2: tìm lại bằng chỉ mục với Top-K lớn hơn và nhiều khung truy vấn hơn
            if self.deep_retry(sg):
                continue
            prev, nxt = self._neighbor(segs, k, -1), self._neighbor(segs, k, 1)
            src_list = []
            for sid, fp in self.sources.items():
                if sid not in sdata:
                    sdata[sid] = source_sift(self.source_paths[sid], fp, cfg.sift_step, cfg.sift_width,
                                             cfg.sift_nfeat, self.cancel)
                sd = sdata[sid]
                lo, hi = 0, len(sd["frame"])
                if prev is not None and nxt is not None and prev.sid == sid and nxt.sid == sid:
                    # cửa sổ [cuối đoạn trước - 5s, đầu đoạn sau + 5s]
                    f_lo = prev.a + prev.speed * fp.fps * (self.edit.pts[prev.end - 1] - prev.t_ref) \
                        - cfg.sift_window_s * fp.fps
                    f_hi = nxt.a + nxt.speed * fp.fps * (self.edit.pts[nxt.start] - nxt.t_ref) \
                        + cfg.sift_window_s * fp.fps
                    if f_hi > f_lo:
                        lo = int(np.searchsorted(sd["frame"], f_lo))
                        hi = int(np.searchsorted(sd["frame"], f_hi, side="right"))
                src_list.append((sid, sd, fp.fps, (lo, max(lo, hi))))
            if prev is not None and nxt is not None and prev.sid == nxt.sid:
                src_list.sort(key=lambda x: x[0] != prev.sid)
            n = sg.n
            qs = (0.1, 0.3, 0.5, 0.7, 0.9) if n >= 15 else (0.15, 0.5, 0.85)
            times = sorted({float(self.edit.pts[sg.start + int(q * (n - 1))]) for q in qs})
            res = match_segment(self.edit_path, times, src_list, cfg, cfg.sift_mask_bottom)
            windowed = any(lo > 0 or hi < len(sdata[sid]["frame"]) for sid, _, _, (lo, hi) in src_list)
            if res is None and windowed:
                # Tầng 6: cửa sổ quanh đoạn kề thất bại -> tìm trên TOÀN BỘ các video nguồn
                full = [(sid, sd, fps_, (0, len(sd["frame"]))) for sid, sd, fps_, _ in src_list]
                res = match_segment(self.edit_path, times, full, cfg, cfg.sift_mask_bottom)
                if res is not None:
                    sg.notes.append("TẦNG 6: SIFT toàn bộ nguồn (cửa sổ ±5s quanh đoạn kề không có)")
            if res is None:
                sg.notes.append("SIFT: không tìm được nguồn đủ tin cậy (cửa sổ + toàn bộ nguồn)")
                sg.red_class = self.classify_red(segs, k)
                sg.notes.append(f"TẦNG 7: phân loại = {sg.red_class}")
                self._finish(sg)
                continue
            fp = self.sources[res["sid"]]
            g = _from_px(res["M"], res["edit_size"], res["src_size"])
            if g is None:
                continue
            if res["flip"]:
                g = Geometry(-g.sx, g.sy, g.tx + g.sx, g.ty)
            # thời điểm SIFT ứng với times[0] -> quy về đầu đoạn
            a0 = res["f_start"] - fp.fps * (times[0] - float(self.edit.pts[sg.start]))
            info = f"SIFT: {res['sid']}@{int(a0)} ({res['inliers']} inlier, thứ hai {res['second']}, " \
                   f"{res['n_frames_ok']}/{len(times)} khung)"
            log.info("Đoạn %d-%d: %s", sg.start, sg.end, info)
            c = Candidate(res["sid"], a0, g, float(res["inliers"]), sg.start + n // 2,
                          int(round(a0 + fp.fps * (self.edit.pts[sg.start + n // 2] - self.edit.pts[sg.start]))))
            c = self.evaluate(sg.start, sg.end, c)
            notes = [r for r in sg.notes if not r.startswith("UNMATCHED")]
            if c.align is not None:
                trial = Segment(sg.start, sg.end, notes=notes + [info])
                self._apply(trial, c)
                self._finish(trial)
                if trial.sid is not None:
                    sg.__dict__.update(trial.__dict__)
                    continue
            # ảnh thu nhỏ không xác minh được (phóng to mạnh) -> tin SIFT, đánh dấu VÀNG
            sg.sid, sg.geom, sg.a, sg.speed, sg.method = res["sid"], g, a0, 1.0, "sift"
            sg.t_ref = float(self.edit.pts[sg.start])
            sg.frames, sg.sims, _ = self._model_sims(sg.sid, g, a0, 1.0, sg.t_ref, range(sg.start, sg.end))
            fin = sg.sims[np.isfinite(sg.sims)]
            sg.score = float(np.median(fin)) if len(fin) else -1.0
            sg.notes = notes + [info + " - ảnh thu nhỏ không xác minh được, cần xem lại"]
            self._finish(sg)

    # ------------------------------------------------------------ V2: khung nội suy của Douyin
    def detect_interpolated(self, segs: list[Segment]):
        """Khung edit không khớp 1 khung nguồn nhưng khớp ảnh TRỘN 2 khung liền nhau -> khung nội suy
        (slow-motion của Douyin). Ghi lại cặp khung để dựng lại bằng phép trộn tương ứng."""
        cfg = self.cfg
        for sg in segs:
            if sg.sid is None or sg.method != "match" or sg.sims is None:
                continue
            fp = self.sources[sg.sid]
            weak = [k for k in range(sg.n) if np.isfinite(sg.sims[k]) and sg.sims[k] < cfg.frame_ok]
            if not weak:
                continue
            try:
                canvas = Canvas(sg.geom, self.edit.size, fp.size, cfg)
            except ValueError:
                continue
            found = 0
            for k in weak:
                f = int(sg.frames[k])
                E, Es = canvas.blocks([self.edit.thumbs[sg.start + k]], "edit")
                best = (sg.sims[k], -1)
                for f2 in (f - 1, f + 1):
                    if not 0 <= f2 < fp.n:
                        continue
                    mix = (fp.thumbs[f].astype(np.float32) + fp.thumbs[f2].astype(np.float32)) / 2
                    S, Ss = canvas.blocks([mix], "source")
                    v = float(pair_block_sims(E, Es, S, Ss, cfg)[1][0])
                    if np.isfinite(v) and v > best[0] + 0.03:
                        best = (v, f2)
                if best[1] >= 0 and best[0] >= cfg.frame_ok:
                    if sg.interp_pair is None:
                        sg.interp_pair = np.full(sg.n, -1, np.int64)
                    sg.interp_pair[k] = best[1]
                    sg.sims[k] = best[0]
                    found += 1
            if found:
                sg.notes.append(f"NỘI SUY KHUNG: {found} khung là ảnh trộn của 2 khung nguồn liền nhau "
                                "(dựng lại bằng phép trộn tương ứng)")
                fin = sg.sims[np.isfinite(sg.sims)]
                sg.score = float(np.median(fin)) if len(fin) else sg.score
                self._finish(sg)

    def _hires_geometry_search(self, sg: Segment, hi, j0: int, j1: int) -> Geometry | None:
        """Tìm (khung nguồn, crop) cùng lúc ở 640px: với 3 khung edit, thử template matching trên mọi khung
        nguồn trong cửa sổ quanh ánh xạ hiện tại; lấy crop của cặp khớp nhất từng khung, rồi trung vị."""
        r = sg.geom.valid_edit_rect(0.004)
        if r is None:
            return None
        s0 = abs(sg.geom.sx) * r[2]
        scales = np.arange(s0 * 0.95, s0 * 1.0501, 0.005)
        found = []
        for q in (0.2, 0.5, 0.8):
            k = int(q * (sg.n - 1))
            f_c = int(sg.frames[k])
            best = (-1.0, None)
            for f in range(max(j0, f_c - 15), min(j1, f_c + 16)):
                g, v = estimate_template(self.edit.thumbs[sg.start + k], hi.thumbs[f], r, sg.geom.flip, scales)
                if g is not None and v > best[0]:
                    best = (v, g)
            if best[1] is not None:
                found.append(best[1])
        if not found:
            return None
        p = np.median([[g.sx, g.sy, g.tx, g.ty] for g in found], axis=0)
        return Geometry(*map(float, p))

    def _speed_note(self, seg: Segment, al: Alignment):
        sa = getattr(al, "speed_alt", None)
        if sa is not None and sa[1] < self.cfg.speed_ambiguity_margin and \
                not any(r.startswith("MƠ HỒ TỐC ĐỘ") for r in seg.notes):
            seg.notes.append(f"MƠ HỒ TỐC ĐỘ: x{al.speed:g} chỉ hơn x{sa[0]:g} {sa[1]:.3f} - hình không đủ phân biệt "
                             "tốc độ (âm thanh có thể lệch)")

    def static_view_test(self, sg: Segment):
        """Vùng hiển thị (sau crop) gần như không đổi suốt đoạn -> thời điểm/tốc độ nguồn KHÔNG quan sát được
        bằng hình -> nghi vấn (trừ khi tính liên tục với cảnh kề xác nhận)."""
        if sg.sid is None or sg.n < 6 or sg.geom is None or any(r.startswith("CẢNH TĨNH") for r in sg.notes):
            return
        r = sg.geom.valid_edit_rect(0.01)
        if r is None:
            return
        ew, eh = self.edit.size
        x0, y0 = int(r[0] * ew), int(r[1] * eh)
        x1, y1 = int((r[0] + r[2]) * ew), int((r[1] + r[3]) * eh)
        first = self.edit.thumbs[sg.start][y0:y1, x0:x1].astype(np.int16)
        mx = max(float(np.abs(self.edit.thumbs[i][y0:y1, x0:x1].astype(np.int16) - first).mean())
                 for i in range(sg.start + 1, sg.end))
        if mx < self.cfg.static_view_max_diff:
            sg.notes.append(f"CẢNH TĨNH: vùng hiển thị gần như không đổi ({mx:.2f}) - không xác định được "
                            "thời điểm/tốc độ nguồn bằng hình")
            self._finish(sg)

    # ------------------------------------------------------------ V2: xác minh đoạn phóng to ở 640px
    def hires_align(self, sg: Segment):
        """Đoạn Douyin phóng to >= 2x: ảnh nguồn 160px không đủ chi tiết -> căn chỉnh LẠI bằng khung nguồn
        640px (giải mã đúng dải cần dùng). Kết quả 640px là quyết định: không khớp -> UNMATCHED."""
        from .media import decode_range

        cfg = self.cfg
        if sg.sid is None or sg.method not in ("match", "sift") or abs(sg.geom.sx) >= cfg.hires_zoom_threshold:
            return
        fp = self.sources[sg.sid]
        n = sg.n
        rel = self.edit.pts[sg.start:sg.end] - self.edit.pts[sg.start]
        dur = float(rel[-1]) + 1.0 / max(self.edit.fps, 1)
        a_hint = float(sg.frames[0])
        j0 = int(max(0, np.floor(a_hint - cfg.window_margin_frames - n * 0.1)))
        j1 = int(min(fp.n, np.ceil(a_hint + dur * fp.fps * cfg.speed_max + cfg.window_margin_frames)))
        frames = decode_range(self.source_paths[sg.sid], fp.pts, j0, j1, cfg.hires_width)
        if len(frames) < (j1 - j0) * 0.98:
            log.warning("Đoạn %d-%d: giải mã 640px thiếu khung (%d/%d) - giữ kết quả cũ, đánh VÀNG",
                        sg.start, sg.end, len(frames), j1 - j0)
            sg.notes.append("XÁC MINH 640px: không giải mã đủ khung")
            sg.status = YELLOW
            return
        hi = _WindowSource(fp, frames, j0)
        import dataclasses

        # lưới so sánh mịn hơn: lưới 96x54 làm nhòe các dịch chuyển nhỏ của chi tiết khi phóng to
        fine = dataclasses.replace(cfg, canvas_area=cfg.hires_canvas_area)
        # crop ước lượng từ ảnh 160px lệch vài pixel ở 640px; khi nền lia, (khung sai + crop lệch) có thể khớp
        # hơn (khung đúng + crop lệch) -> ước lượng LẠI crop cùng lúc với khung nguồn bằng template matching
        g_hi = self._hires_geometry_search(sg, hi, j0, j1)
        geom = g_hi if g_hi is not None and self.plausible(g_hi, sg.sid) else sg.geom
        try:
            al, _ = align_scene(self.edit, hi, sg.start, sg.end, geom, a_hint, fine)
        except ValueError:
            al = None
        if al is not None and geom is not sg.geom:
            sg.geom = geom
        old = f"{sg.sid}@{int(sg.frames[0])} x{sg.speed:g}"
        ok = al is not None and al.score >= cfg.scene_match and \
            np.mean(np.nan_to_num(al.sims, nan=0) >= cfg.frame_ok) >= cfg.match_ok_ratio
        log.debug("640px %d-%d: geom cũ %s | geom 640 %s | điểm %s | đạt %s", sg.start, sg.end, sg.geom, geom,
                  None if al is None else round(al.score, 4),
                  None if al is None else round(float(np.mean(np.nan_to_num(al.sims, nan=0) >= cfg.frame_ok)), 3))
        if not ok:
            log.info("Đoạn %d-%d: xác minh 640px THẤT BẠI (%s) -> UNMATCHED", sg.start, sg.end, old)
            sg.notes.append(f"UNMATCHED: xác minh 640px không khớp (ứng viên {old} ở ảnh thu nhỏ bị loại)")
            sg.sid, sg.frames, sg.sims, sg.geom, sg.frame_map, sg.local_speed = None, None, None, None, None, None
            sg.method = "match"
            self._finish(sg)
            return
        sg.a, sg.speed, sg.t_ref = al.a, al.speed, float(self.edit.pts[sg.start])
        sg.frames, sg.sims, sg.score = al.frames.copy(), al.sims.copy(), al.score
        sg.frame_map = None if al.frame_map is None else al.frame_map.copy()
        sg.local_speed = None if al.local_speed is None else al.local_speed.copy()
        sg.ambiguous_frames = al.ambiguous_frames
        sg.notes = [r for r in sg.notes if not r.startswith("MƠ HỒ TỐC ĐỘ")]
        self._speed_note(sg, al)
        if sg.method == "sift":
            sg.method = "match"
        sg.notes.append(f"XÁC MINH 640px (zoom {1 / abs(sg.geom.sx):.1f}x): {sg.sid}@{int(sg.frames[0])} "
                        f"x{sg.speed:g} (ảnh thu nhỏ: {old})")
        log.info("Đoạn %d-%d: %s", sg.start, sg.end, sg.notes[-1])
        self._finish(sg)

    # ------------------------------------------------------------ V2: tầng cứu hộ bổ sung
    def deep_retry(self, sg: Segment) -> bool:
        """Tầng 2: chỉ mục với Top-K x3 và gấp đôi số khung truy vấn; chỉ nhận nếu qua kiểm định thường."""
        import dataclasses

        base = self.cfg
        self.cfg = dataclasses.replace(base, topk=max(base.topk * 3, 30), query_per_scene=base.query_per_scene * 2)
        try:
            cands = self.find_candidates(sg.start, sg.end, max_cand=8)
            evald = [c for c in (self.evaluate(sg.start, sg.end, c) for c in cands) if c.align is not None]
        finally:
            self.cfg = base
        if not evald:
            return False
        evald.sort(key=lambda c: -self._rank(c))
        trial = Segment(sg.start, sg.end, notes=[n for n in sg.notes if not n.startswith("UNMATCHED")]
                        + ["TẦNG 2: tìm lại với Top-K lớn hơn"])
        self._apply(trial, evald[0])
        self._finish(trial)
        if trial.sid is None:
            sg.notes.append("TẦNG 2: Top-K lớn hơn vẫn không có ứng viên đạt kiểm định")
            return False
        log.info("Đoạn %d-%d: tầng 2 cứu được (%s@%d)", sg.start, sg.end, trial.sid, int(trial.frames[0]))
        sg.__dict__.update(trial.__dict__)
        return True

    def classify_red(self, segs, k: int) -> str:
        """Tầng 7: đoạn ĐỎ là gì? (để người duyệt biết vì sao không có nguồn)."""
        sg = segs[k]
        th = [self.edit.thumbs[i].astype(np.float32) for i in range(sg.start, sg.end)]
        if sg.n <= 2:
            return "quá ngắn (1-2 khung)"
        means = np.array([t.mean() for t in th])
        stds = np.array([t.std() for t in th])
        if (stds < 6).all():
            return "khung trơn / flash (đen, trắng hoặc một màu)"
        diffs = [float(np.abs(th[i] - th[i - 1]).mean()) for i in range(1, len(th))]
        prev, nxt = self._neighbor(segs, k, -1), self._neighbor(segs, k, 1)
        if prev is not None and nxt is not None:  # chuyển cảnh trộn (dissolve) giữa 2 đoạn kề?
            a = self.edit.thumbs[prev.end - 1].astype(np.float32)
            b = self.edit.thumbs[nxt.start].astype(np.float32)
            errs = []
            for t in th:
                best = min(float(np.abs(t - (w * a + (1 - w) * b)).mean()) for w in np.linspace(0, 1, 11))
                errs.append(best)
            if np.median(errs) < 4.0:
                return "chuyển cảnh trộn (dissolve) giữa 2 cảnh kề"
        if np.median(diffs) < 0.8:
            return "cảnh tĩnh, không tìm thấy trong nguồn"
        if abs(means[-1] - means[0]) > 40 and np.median(diffs) > 2:
            return "hiệu ứng sáng/tối dần (fade)"
        return "cảnh có chuyển động, không tìm thấy trong mọi nguồn (có thể không thuộc anime gốc)"

    # ------------------------------------------------------------ P0: nội suy đoạn ĐỎ
    def interpolate(self, segs: list[Segment]):
        """Đoạn ĐỎ nằm giữa các đoạn đã khớp -> nội suy vị trí nguồn (hoặc giữ khung), đánh dấu VÀNG."""
        cfg = self.cfg
        last_t = float(self.edit.pts[-1]) + 1.0 / max(self.edit.fps, 1)

        def t_at(k):
            return float(self.edit.pts[k]) if k < self.edit.n else last_t

        for k, sg in enumerate(segs):
            if sg.sid is not None:
                continue
            a, b = self._neighbor(segs, k, -1), self._neighbor(segs, k, 1)
            if a is None and b is None:
                continue
            ref = a if a is not None else b
            fp = self.sources[ref.sid]
            t_i, dur = t_at(sg.start), t_at(sg.end) - t_at(sg.start)
            method, speed = "interp", 1.0
            if a is not None:
                t_ae = t_at(a.end)
                f_ae = a.a + a.speed * fp.fps * (t_ae - a.t_ref)  # khung (thực) cuối đoạn a
            if a is not None and b is not None and b.sid == a.sid:
                f_bs = b.a + b.speed * fp.fps * (t_at(b.start) - b.t_ref)
                gap_src = (f_bs - f_ae) / fp.fps
                gap_edit = max(1e-6, t_at(b.start) - t_ae)
                if gap_src < dur:
                    method, start = "freeze", float(a.frames[-1])
                else:
                    speed = float(np.clip(gap_src / gap_edit, cfg.interp_speed_min, cfg.interp_speed_max))
                    start = f_ae + (t_i - t_ae) * speed * fp.fps
            elif a is not None:
                start = f_ae + (t_i - t_ae) * fp.fps
                if start + dur * fp.fps > fp.n - 1:
                    method, start = "freeze", float(a.frames[-1])
            else:
                start = b.a + b.speed * fp.fps * (t_at(b.start) - b.t_ref) - dur * fp.fps
                if start < 0:
                    method, start = "freeze", float(b.frames[0])
            if method == "freeze":
                speed = 0.0
            sg.sid, sg.geom, sg.a, sg.speed, sg.method = ref.sid, ref.geom, float(start), speed, method
            sg.t_ref = t_i
            sg.frames, sg.sims, _ = self._model_sims(sg.sid, sg.geom, sg.a, speed, t_i, range(sg.start, sg.end))
            fin = sg.sims[np.isfinite(sg.sims)]
            sg.score = float(np.median(fin)) if len(fin) else -1.0
            what = "GIỮ KHUNG (freeze)" if method == "freeze" else f"NỘI SUY tốc độ {speed:.3f}"
            sg.notes.append(f"{what} từ {ref.sid}@{int(start)} theo đoạn kề - KHÔNG phải kết quả so khớp")
            log.info("Đoạn %d-%d: %s", sg.start, sg.end, sg.notes[-1])
            self._finish(sg)

    def _expected_a(self, prev: Segment, cur: Segment) -> float:
        """Khung nguồn (số thực) mà mô hình của ``prev`` dự đoán tại đầu cảnh ``cur`` (hai chiều).

        Đoạn có tốc độ thay đổi (frame_map): ngoại suy từ khung đầu/cuối với tốc độ cục bộ tại đó."""
        fp = self.sources[prev.sid]
        if prev.frame_map is not None:
            if prev.start >= cur.end:  # đoạn kề nằm SAU cur
                return float(prev.frame_map[0] - prev.local_speed[0] * fp.fps
                             * (self.edit.pts[prev.start] - self.edit.pts[cur.start]))
            return float(prev.frame_map[-1] + prev.local_speed[-1] * fp.fps
                         * (self.edit.pts[cur.start] - self.edit.pts[prev.end - 1]))
        return float(prev.a + prev.speed * fp.fps * (self.edit.pts[cur.start] - prev.t_ref))

    def _edge_speed(self, nb: Segment, before: bool) -> float:
        if nb.frame_map is not None:
            return float(nb.local_speed[-1] if before else nb.local_speed[0])
        return nb.speed

    def _model_sims(self, sid: str, geom: Geometry, a: float, speed: float, t_ref: float, idx):
        """Khung nguồn + điểm tương đồng của các khung edit ``idx`` theo mô hình tuyến tính đã cho."""
        fp = self.sources[sid]
        idx = list(idx)
        if not idx:
            return np.zeros(0, int), np.zeros(0), np.zeros(0)
        rel = self.edit.pts[idx] - t_ref
        raw = np.floor(a + speed * fp.fps * rel + 0.5).astype(int)
        frames = np.clip(raw, 0, fp.n - 1)
        try:
            canvas = Canvas(geom, self.edit.size, fp.size, self.cfg)
        except ValueError:
            return frames, np.full(len(idx), np.nan), np.full(len(idx), np.nan)
        E, Es = canvas.blocks([self.edit.thumbs[i] for i in idx], "edit")
        S, Ss = canvas.blocks([fp.thumbs[f] for f in frames], "source")
        sel, sims = pair_block_sims(E, Es, S, Ss, self.cfg)
        sel, sims = sel.astype(np.float64), sims.astype(np.float64)
        out = (raw < 0) | (raw >= fp.n)
        sims[out] = -1.0  # ngoài phạm vi nguồn
        sel[out] = -1.0
        return frames, sims, sel

    def _reeval_at(self, seg: Segment, sid: str, geom: Geometry, a_fixed: float, speed: float) -> Alignment | None:
        frames, sims, sel = self._model_sims(sid, geom, a_fixed, speed, float(self.edit.pts[seg.start]),
                                             range(seg.start, seg.end))
        valid = sims[np.isfinite(sims)]
        score = float(np.median(valid)) if len(valid) else -1
        return Alignment(a_fixed, speed, frames, sims, sims, score, 0, int((~np.isfinite(sims)).sum()), sel)

    def _set_range(self, sg: Segment, start: int, end: int):
        """Đổi phạm vi khung của cảnh, tính lại khung nguồn/điểm theo mô hình của cảnh."""
        sg.start, sg.end = start, end
        if sg.sid is None:
            sg.frames = sg.sims = None
            return
        sg.frames, sg.sims, _ = self._model_sims(sg.sid, sg.geom, sg.a, sg.speed, sg.t_ref, range(start, end))
        valid = sg.sims[np.isfinite(sg.sims)]
        sg.score = float(np.median(valid)) if len(valid) else -1

    def refine_boundaries(self, segs: list[Segment], radius: int = 4):
        """Dò lại từng điểm chuyển cảnh: gán mỗi khung sát ranh giới cho mô hình khớp nó tốt nhất."""
        cfg = self.cfg
        for k in range(len(segs) - 1):
            A, B = segs[k], segs[k + 1]
            if (A.sid is None and B.sid is None) or A.n == 0 or B.n == 0 or \
                    A.frame_map is not None or B.frame_map is not None:
                continue
            lo = max(A.start + (1 if A.sid is not None else 0), B.start - radius)
            hi = min(B.end - (1 if B.sid is not None else 0), B.start + radius)
            if hi <= lo:
                continue
            rng_ = list(range(lo, hi))
            base = lo  # sa/sb đánh chỉ số theo dải ban đầu (lo có thể bị thu hẹp bên dưới)

            def side_sims(sg):
                if sg.sid is None:
                    return np.full(len(rng_), cfg.frame_min)  # cảnh ĐỎ: chỉ nhận khung không khớp
                _, _, v = self._model_sims(sg.sid, sg.geom, sg.a, sg.speed, sg.t_ref, rng_)
                if sg.n < cfg.min_evidence_frames:  # đoạn quá ngắn: chỉ giữ khung nó tự khớp tốt
                    v = np.maximum(np.where(np.isfinite(v), v, -1), cfg.frame_ok - 0.05)
                return np.where(np.isfinite(v), v, cfg.frame_min)

            sa, sb = side_sims(A), side_sims(B)
            log.debug("Ranh giới %d|%d: khung %s sa=%s sb=%s", A.start, B.start, rng_, np.round(sa, 2), np.round(sb, 2))
            best_p, best_v = B.start, -1e9
            # đoạn quá ngắn chỉ được nhường khung, không được lấn sang cảnh kề
            if A.n < cfg.min_evidence_frames and A.sid is not None:
                hi = min(hi, B.start)
            if B.n < cfg.min_evidence_frames and B.sid is not None:
                lo = max(lo, B.start)
            for p in range(lo, hi + 1):
                v = sa[: p - base].sum() + sb[p - base:].sum()
                if v > best_v + 1e-6 or (abs(v - best_v) <= 1e-6 and p == B.start):
                    best_p, best_v = p, v
            if best_p != B.start:
                log.info("Dời điểm cắt %d -> %d (dò lại ranh giới cảnh)", B.start, best_p)
                old_a, old_b = (A.start, A.end), (B.start, B.end)
                self._set_range(A, A.start, best_p)
                self._set_range(B, best_p, B.end)
                for sg, old in ((A, old_a), (B, old_b)):
                    if sg.n > 0:
                        sg.notes.append(f"RANH GIỚI: dời từ {old[0]}-{old[1] - 1} thành {sg.start}-{sg.end - 1}")
                        self._finish(sg)

    def _adopt(self, sg: Segment, sid: str, geom: Geometry, al: Alignment, note: str):
        sg.sid, sg.geom, sg.a, sg.speed = sid, geom, al.a, al.speed
        sg.t_ref = float(self.edit.pts[sg.start])
        sg.frames, sg.sims, sg.score = al.frames.copy(), al.sims.copy(), al.score
        sg.frame_map = None if al.frame_map is None else al.frame_map.copy()
        sg.local_speed = None if al.local_speed is None else al.local_speed.copy()
        sg.ambiguous_frames = al.ambiguous_frames
        sg.method = "match"
        sg.notes.append(note)
        self._finish(sg)

    def consensus_geometry(self, segs: list[Segment]):
        """Bản Douyin thường dùng CÙNG một kiểu crop cho nhiều cảnh: thử hình học chung cho cảnh lệch."""
        groups: list[list] = []  # [geom, weight, count]
        for sg in segs:
            if sg.sid is None or sg.status != GREEN:
                continue
            for gr in groups:
                if gr[0].close_to(sg.geom, 0.012):
                    gr[1] += sg.n
                    gr[2] += 1
                    break
            else:
                groups.append([sg.geom, sg.n, 1])
        groups = [g for g in groups if g[2] >= 2]
        if not groups:
            return
        groups.sort(key=lambda g: -g[1])
        for sg in segs:
            if sg.sid is None or sg.method != "match" or sg.frame_map is not None:
                continue
            for G, _, _ in groups[:2]:
                if sg.geom.close_to(G, 0.006):
                    break
                cur = self._sel_mean(sg, sg.sid, sg.geom, sg.a, sg.speed, sg.t_ref)
                try:
                    al, _ = align_scene(self.edit, self.sources[sg.sid], sg.start, sg.end, G, float(sg.frames[0]),
                                        self.cfg)
                except ValueError:
                    continue
                if al is None:
                    continue
                new = self._sel_mean(sg, sg.sid, G, al.a, al.speed, float(self.edit.pts[sg.start]))
                log.debug("Đồng thuận %d-%d: sel %.4f -> %.4f", sg.start, sg.end, cur, new)
                if new >= cur - 0.004 and al.score >= self.cfg.scene_match:
                    old = f"{sg.sid}@{int(sg.frames[0])}"
                    self._adopt(sg, sg.sid, G, al, f"HÌNH HỌC CHUNG: dùng crop chung của video (thay {old})")
                    break

    def ambiguity_test(self, sg: Segment, shifts=None, depth: int = 0):
        """Phép thử mơ hồ thời gian-không gian (cảnh lia máy): dời thời gian ±d khung rồi khớp lại crop.

        Nếu giả thuyết dời vẫn khớp NGANG -> VÀNG (không được coi là đã xác minh); nếu khớp HƠN -> chuyển sang.
        """
        cfg = self.cfg
        amb = any(r.startswith(AMBIG) for r in sg.notes)
        if sg.sid is None or sg.n < cfg.min_evidence_frames or (sg.status != GREEN and not amb) or depth > 3 \
                or sg.frame_map is not None:
            return
        if shifts is None:  # cảnh đã mơ hồ -> dò rộng hơn
            shifts = tuple(d for d in range(-16, 17, 2) if d) if amb else (-8, -4, -2, -1, 1, 2, 4, 8)
        fp = self.sources[sg.sid]
        cur = self._sel_mean(sg, sg.sid, sg.geom, sg.a, sg.speed, sg.t_ref)
        ks = np.unique(np.linspace(0, sg.n - 1, min(4, sg.n)).astype(int))
        region = sg.geom.valid_edit_rect(0.004)
        worst = None
        for d in shifts:
            frames = np.clip(sg.frames[ks] + d, 0, fp.n - 1)
            pairs = [(self.edit.thumbs[sg.start + k], fp.thumbs[f]) for k, f in zip(ks, frames)]
            # khởi tạo hình học: crop hiện tại + template matching (tìm toàn cục quanh tỉ lệ hiện tại)
            inits = [sg.geom]
            if region is not None:
                s0 = abs(sg.geom.sx) * region[2]
                km = len(ks) // 2
                gt, _ = estimate_template(pairs[km][0], pairs[km][1], region, sg.geom.flip,
                                          scales=np.arange(s0 * 0.94, s0 * 1.0601, 0.01))
                if gt is not None:
                    inits.append(gt)
            g2, best_v = None, -9.0
            for g0 in inits:
                gg, v = refine_direct(pairs, g0, self.edit.size, fp.size, cfg, max_evals=40, region=region)
                if v > best_v:
                    g2, best_v = gg, v
            if amb and best_v >= cur - 0.03:  # chỉ tinh chỉnh kỹ giả thuyết có triển vọng
                g2, best_v = refine_direct(pairs, g2, self.edit.size, fp.size, cfg, max_evals=120, region=region)
            if g2.valid_edit_rect(0.004) is None or not self.plausible(g2, sg.sid):
                continue
            alt = self._sel_mean(sg, sg.sid, g2, sg.a + d, sg.speed, sg.t_ref)
            if alt >= cur - 0.003 and (worst is None or alt > worst[0]):
                worst = (alt, d, g2)
        if worst is None:
            # đã dò rộng (±16 khung, chỉnh lại crop) mà không giả thuyết CÙNG NGUỒN nào khớp ngang
            # -> ứng viên cùng nguồn khác offset bị loại; ứng viên NGUỒN KHÁC vẫn giữ nghi vấn
            if amb and len(shifts) > 4:
                kept = [r for r in sg.notes if not (r.startswith(("NHIỀU NGUỒN", "MƠ HỒ THỜI GIAN"))
                                                    and (f"{sg.sid}@" in r or r.startswith("MƠ HỒ")))]
                if len(kept) != len(sg.notes):
                    sg.notes = kept + ["ĐÃ LOẠI: mọi giả thuyết cùng nguồn lệch ±2..16 khung đều kém hơn"]
                    self._finish(sg)
            return
        alt, d, g2 = worst
        if alt > cur + 0.003:
            try:
                al, _ = align_scene(self.edit, fp, sg.start, sg.end, g2, float(sg.frames[0] + d), cfg)
            except ValueError:
                al = None
            if al is not None and self._sel_mean(sg, sg.sid, g2, al.a, al.speed,
                                                 float(self.edit.pts[sg.start])) > cur + 0.003:
                old = f"{sg.sid}@{int(sg.frames[0])}"
                self._adopt(sg, sg.sid, g2, al, f"LIA MÁY: giả thuyết dời {d:+d} khung khớp hơn (thay {old})")
                return self.ambiguity_test(sg, shifts, depth + 1)
        sg.notes.append(f"MƠ HỒ THỜI GIAN: dời {d:+d} khung + chỉnh crop vẫn khớp ngang ({alt:.3f} vs {cur:.3f})")
        self._finish(sg)

    def _sel_mean(self, sg: Segment, sid, geom, a, speed, t_ref) -> float:
        _, _, sel = self._model_sims(sid, geom, a, speed, t_ref, range(sg.start, sg.end))
        v = np.where(np.isfinite(sel), sel, -0.3)
        return float(v.mean()) if len(v) else -9.0

    def resolve_continuity(self, segs: list[Segment]):
        """Kiểm tra tính liên tục với cảnh lân cận.

        Nếu phần nối tiếp của cảnh kề (cùng nguồn, cùng crop, cùng tốc độ) giải thích cảnh này tốt
        NGANG hoặc HƠN -> chọn nó (giải quyết cảnh tĩnh, cảnh trùng nhiều nguồn, lia máy mơ hồ, đoạn
        quá ngắn). Cảnh mơ hồ mà không được tính liên tục xác nhận vẫn giữ trạng thái VÀNG.
        """
        cfg = self.cfg
        for _pass in range(2):
            for k, sg in enumerate(segs):
                if sg.sid is None or sg.n == 0 or sg.frame_map is not None:
                    continue
                amb = [r for r in sg.notes if r.startswith(AMBIG)]
                cur_sel = self._sel_mean(sg, sg.sid, sg.geom, sg.a, sg.speed, sg.t_ref)
                best = None
                for nb, direction in ((segs[k - 1] if k > 0 else None, -1),
                                      (segs[k + 1] if k + 1 < len(segs) else None, 1)):
                    if nb is None or nb.status != GREEN or nb.sid is None or nb.n < cfg.min_evidence_frames:
                        continue
                    a_exp = self._expected_a(nb, sg)  # mô hình liên tục của cảnh kề (hai chiều)
                    nb_speed = self._edge_speed(nb, direction < 0)
                    if nb_speed <= 0:  # đoạn kề đứng hình: không suy ra được vị trí tiếp theo
                        continue
                    same_model = (nb.sid == sg.sid and nb.geom.close_to(sg.geom, 0.004)
                                  and abs(nb_speed - sg.speed) < 1e-3 and abs(a_exp - sg.a) < 0.3)
                    if same_model:
                        continue
                    # dò pha dưới-khung quanh dự đoán (làm tròn khung nguồn ở ranh giới cảnh)
                    t0 = float(self.edit.pts[sg.start])
                    phases = [(self._sel_mean(sg, nb.sid, nb.geom, a_exp + d, nb_speed, t0), a_exp + d)
                              for d in np.arange(-1.0, 1.001, 0.125)]
                    sel, a_exp = max(phases)
                    al = self._reeval_at(sg, nb.sid, nb.geom, a_exp, nb_speed)
                    # Hai giả thuyết chênh nhau trong biên mơ hồ = không phân biệt được bằng hình -> tính liên tục
                    # với cảnh kề đã XANH là bằng chứng quyết định (đúng yêu cầu 'kiểm tra tính liên tục').
                    need = cur_sel - cfg.ambiguity_margin
                    log.debug("Liên tục %d-%d <- %d-%d: điểm %.4f, sel %.4f (cần %.4f)", sg.start, sg.end,
                              nb.start, nb.end, al.score if al else -1, sel, need)
                    log.debug("   khung %s a=%.3f t_ref=%.4f nb.a=%.3f nb.sp=%.4f geom=%s", list(al.frames[:8]) if al else None,
                              a_exp, float(self.edit.pts[sg.start]), nb.a, nb.speed, nb.geom)
                    if al is None or al.score < cfg.scene_green:
                        continue
                    if sel >= need and (best is None or sel > best[0]):
                        best = (sel, nb, al, direction)
                if best is None:
                    continue
                sel, nb, al, direction = best
                old = f"{sg.sid}@{int(sg.frames[0])}"
                sg.sid, sg.geom, sg.a, sg.speed = nb.sid, nb.geom, al.a, al.speed
                sg.method = "match"
                sg.t_ref = float(self.edit.pts[sg.start])
                sg.frames, sg.sims, sg.score = al.frames, al.sims, al.score
                sg.notes = [r for r in sg.notes if not r.startswith(AMBIG + ("RANH GIỚI",))]
                sg.notes.append(f"LIÊN TỤC: theo cảnh {'trước' if direction < 0 else 'sau'} ({nb.sid}), "
                                f"thay cho {old}" + (f"; đã loại mơ hồ: {'; '.join(amb)}" if amb else ""))
                self._finish(sg)

    def merge_continuous(self, segs: list[Segment]) -> list[Segment]:
        """Gộp các cảnh liền nhau lấy liên tục từ cùng nguồn (điểm cắt giả / cắt nội bộ nguồn).

        Căn chỉnh lại CHUNG trên cả đoạn -> ước lượng tốc độ chính xác hơn; chỉ gộp nếu điểm không giảm.
        """
        out: list[Segment] = []
        for sg in segs:
            if out and out[-1].sid is None and sg.sid is None:  # gộp các đoạn ĐỎ liền nhau
                p = out[-1]
                p.end = sg.end
                p.notes = p.notes + [r for r in sg.notes if r not in p.notes]
                self._finish(p)
                continue
            if out and self._try_merge(out[-1], sg):
                continue
            out.append(sg)
        return out

    def _try_merge(self, p: Segment, sg: Segment) -> bool:
        if p.frame_map is not None or sg.frame_map is not None:
            return False
        if (p.sid is None or sg.sid != p.sid or not p.geom.close_to(sg.geom, 0.02)
                or abs(p.speed - sg.speed) > 0.08 or abs(self._expected_a(p, sg) - sg.frames[0]) > 2.5
                or p.status == RED or sg.status == RED):
            return False
        fp = self.sources[p.sid]
        avg = Geometry(*((np.array([p.geom.sx, p.geom.sy, p.geom.tx, p.geom.ty]) * p.n +
                          np.array([sg.geom.sx, sg.geom.sy, sg.geom.tx, sg.geom.ty]) * sg.n) / (p.n + sg.n)))
        al, geom = None, None
        for g in (p.geom, sg.geom, avg):  # mỗi cảnh tinh chỉnh crop riêng -> thử cả 3
            try:
                a2, _ = align_scene(self.edit, fp, p.start, sg.end, g, float(p.frames[0]), self.cfg)
            except ValueError:
                continue
            if a2 is not None and (al is None or np.nanmean(a2.sims_sel) > np.nanmean(al.sims_sel)):
                al, geom = a2, g
        if al is None:
            return False
        parts = []
        for x in (p, sg):
            parts.append(self._model_sims(x.sid, x.geom, x.a, x.speed, x.t_ref, range(x.start, x.end))[2])
        sel_parts = np.concatenate(parts)
        sel_joint = al.sims_sel
        if not (np.nanmean(sel_joint) >= np.nanmean(sel_parts) - 0.012
                and al.score >= min(p.score, sg.score) - 0.01):
            log.debug("Không gộp %d-%d|%d-%d: sel chung %.4f vs %.4f, điểm %.4f vs %.4f (a=%.2f sp=%.3f)",
                      p.start, p.end, sg.start, sg.end, np.nanmean(sel_joint), np.nanmean(sel_parts),
                      al.score, min(p.score, sg.score), al.a, al.speed)
            return False
        log.info("Gộp cảnh %d-%d với %d-%d (nguồn liên tục, tốc độ %.3f)", p.start, p.end, sg.start, sg.end,
                 al.speed)
        p.merged_cuts.append(sg.start)
        p.end = sg.end
        p.a, p.speed, p.t_ref, p.geom = al.a, al.speed, float(self.edit.pts[p.start]), geom
        p.frames, p.sims, p.score = al.frames.copy(), al.sims.copy(), al.score
        p.ambiguous_frames = min(p.ambiguous_frames, sg.ambiguous_frames)
        notes = []
        for r in p.notes + sg.notes:
            if r.startswith("RANH GIỚI"):
                continue
            # căn chỉnh chung liên tục chỉ giải quyết nhập nhằng CÙNG nguồn; nội dung trùng ở
            # nguồn KHÁC vẫn là nghi vấn (bản sao cũng liền mạch như vậy)
            other_src = r.startswith("NHIỀU NGUỒN: ") and not r.startswith(f"NHIỀU NGUỒN: {p.sid}@")
            if r.startswith(AMBIG) and not other_src:
                r = "LIÊN TỤC xác nhận (" + r + ")"
            if r not in notes:
                notes.append(r)
        notes.append(f"GỘP: điểm cắt {sg.start} là cắt giả/nguồn liên tục")
        p.notes = notes
        p.candidates = p.candidates or sg.candidates
        self._finish(p)
        return True

    def hires_geometry(self, sg: Segment):
        fp = self.sources[sg.sid]
        ok = [k for k in range(sg.n) if np.isfinite(sg.sims[k]) and sg.sims[k] >= self.cfg.frame_ok]
        if not ok:
            return
        k = ok[len(ok) // 2]
        ei, fi = sg.start + k, int(sg.frames[k])
        W = self.cfg.hires_width
        try:
            e_img = grab_frames(self.edit_path, [float(self.edit.pts[ei])], W)[0]
            s_img = grab_frames(self.source_paths[sg.sid], [float(fp.pts[fi])], W)[0]
        except Exception as ex:  # pragma: no cover
            log.warning("Không lấy được khung hi-res: %s", ex)
            return
        if e_img is None or s_img is None:
            return
        try:
            base = pair_score(e_img, s_img, sg.geom, (e_img.shape[1], e_img.shape[0]),
                              (s_img.shape[1], s_img.shape[0]), self.cfg)
            g2, cc = refine_ecc(e_img, s_img, sg.geom, iters=60)
            if cc <= 0 or not g2.close_to(sg.geom, 0.03):
                return
            new = pair_score(e_img, s_img, g2, (e_img.shape[1], e_img.shape[0]),
                             (s_img.shape[1], s_img.shape[0]), self.cfg)
        except ValueError:
            return
        if new >= base:
            sg.geom = g2
