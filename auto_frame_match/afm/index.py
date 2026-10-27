"""Chỉ mục tìm nguồn ứng viên (FAISS nếu có, ngược lại numpy theo khối - không tạo ma trận toàn cục).

Vector được thêm THEO KHỐI từ memmap (không nạp cả cache vào RAM). Khi rất nhiều vector (nhiều tập dài),
dùng IVF + lượng tử 8-bit: ~144 byte/vector thay vì 576 byte.
"""

from __future__ import annotations

import numpy as np

from .fingerprint import Fingerprint
from .log import get_logger

log = get_logger()

try:  # pragma: no cover - phụ thuộc môi trường
    import faiss  # type: ignore

    HAVE_FAISS = True
except Exception:  # pragma: no cover
    faiss = None
    HAVE_FAISS = False

BIG = 300_000
CHUNK = 4096  # số khung mỗi lần thêm


class SourceIndex:
    def __init__(self, sources: dict[str, Fingerprint]):
        self.sids = list(sources)
        total = sum(int(np.prod(fp.feats.shape[:2])) for fp in sources.values())
        d = next(iter(sources.values())).feats.shape[2]
        self.d = d
        metas, store = [], []
        self.index = None
        if HAVE_FAISS:
            if total > BIG:
                nlist = int(min(4096, max(256, np.sqrt(total) * 2)))
                quant = faiss.IndexFlatIP(d)
                self.index = faiss.IndexIVFScalarQuantizer(quant, d, nlist, faiss.ScalarQuantizer.QT_8bit,
                                                           faiss.METRIC_INNER_PRODUCT)
                self.index.train(self._sample(sources, nlist * 50))
                self.index.nprobe = 48
                self._quant = quant  # giữ tham chiếu
            else:
                self.index = faiss.IndexFlatIP(d)
        for si, (sid, fp) in enumerate(sources.items()):
            idx = fp.meta["feat_idx"]
            nidx, nw, _ = fp.feats.shape
            for a in range(0, nidx, CHUNK):
                blk = np.asarray(fp.feats[a:a + CHUNK], dtype=np.float32).reshape(-1, d)
                m = np.empty((len(blk), 3), np.int32)
                m[:, 0] = si
                m[:, 1] = np.repeat(idx[a:a + CHUNK], nw)
                m[:, 2] = np.tile(np.arange(nw), min(CHUNK, nidx - a))
                keep = np.linalg.norm(blk, axis=1) > 0.5  # bỏ vector rỗng (khung/cửa sổ trơn)
                blk, m = np.ascontiguousarray(blk[keep]), m[keep]
                if self.index is not None:
                    self.index.add(blk)
                else:
                    store.append(blk.astype(np.float16))
                metas.append(m)
        self.meta = np.concatenate(metas)
        self.vecs = np.concatenate(store) if store else None
        log.info("Chỉ mục: %d vector (%s)", len(self.meta),
                 ("FAISS IVF-SQ8" if total > BIG else "FAISS") if HAVE_FAISS else "numpy")

    @staticmethod
    def _sample(sources, n):
        rng = np.random.default_rng(0)
        per = max(1, n // len(sources))
        out = []
        for fp in sources.values():
            nidx, nw, d = fp.feats.shape
            rows = np.sort(rng.choice(nidx, min(nidx, max(1, per // nw + 1)), replace=False))
            v = np.asarray(fp.feats[rows], dtype=np.float32).reshape(-1, d)
            out.append(v[np.linalg.norm(v, axis=1) > 0.5])
        return np.ascontiguousarray(np.concatenate(out))

    def search(self, q: np.ndarray, k: int):
        q = np.ascontiguousarray(q, dtype=np.float32)
        if self.index is not None:
            return self.index.search(q, k)
        sims = np.empty((len(q), k), np.float32)
        ids = np.empty((len(q), k), np.int64)
        for s in range(0, len(q), 256):
            block = np.zeros((min(256, len(q) - s), len(self.vecs)), np.float32)
            for c in range(0, len(self.vecs), 200_000):
                block[:, c:c + 200_000] = q[s:s + 256] @ self.vecs[c:c + 200_000].astype(np.float32).T
            kk = min(k, block.shape[1] - 1)
            top = np.argpartition(-block, kk, axis=1)[:, :k]
            ts = np.take_along_axis(block, top, 1)
            order = np.argsort(-ts, axis=1)
            ids[s:s + 256] = np.take_along_axis(top, order, 1)
            sims[s:s + 256] = np.take_along_axis(ts, order, 1)
        return sims, ids
