"""Bài thử tải quy mô thật: 2 nguồn 1080p dài + bản Douyin dọc 9:16 (nền mờ, phụ đề), đo thời gian & RAM.

Chạy: python tests/stress.py <thư_mục> [phút_nguồn]
"""

from __future__ import annotations

import json
import logging
import resource
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from afm.config import AppConfig  # noqa: E402
from afm.log import setup_logging  # noqa: E402
from afm.pipeline import run_mode_a  # noqa: E402
from scenarios import score_table  # noqa: E402
from synth import CartoonSource, EditSeg, SourceSpec, build_edit  # noqa: E402


def main():
    root = Path(sys.argv[1])
    minutes = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0
    root.mkdir(parents=True, exist_ok=True)
    setup_logging(root / "logs", logging.INFO)
    specs = [SourceSpec("L1", 101, minutes * 60, width=1920, height=1080),
             SourceSpec("L2", 202, minutes * 60 * 0.85, width=1920, height=1080)]
    srcs, paths = {}, {}
    t = time.time()
    for sp in specs:
        cs = CartoonSource(sp)
        p = root / f"{sp.sid}_1080p.mp4"
        if not p.exists():
            cs.render(p)
        srcs[sp.sid], paths[sp.sid] = cs, p
    print(f"tạo nguồn: {time.time() - t:.0f}s")
    import numpy as np

    rng = np.random.default_rng(7)
    segs = []
    for k in range(16):
        sid = "L1" if k % 2 == 0 else "L2"
        n = srcs[sid].spec.n
        segs.append(EditSeg(sid, int(rng.integers(0, n - 200)), float(rng.uniform(1.5, 4.0)),
                            flip=bool(k % 5 == 3), subtitle="Shin-chan noi gi do" if k % 3 == 0 else None))
    segs.insert(7, EditSeg(None, seconds=2.0, foreign_seed=31337))
    edit = root / "douyin_doc.mp4"
    if not edit.exists():
        build_edit(edit, srcs, segs, fps=30.0, size=(720, 1280), layout="letterbox", watermark=True)
    truth = json.loads(edit.with_suffix(".truth.json").read_text())
    cfg = AppConfig()
    cfg.cache_dir = str(root / "cache")
    cfg.render.allow_unverified = True
    t = time.time()
    res = run_mode_a(str(edit), [str(paths["L1"]), str(paths["L2"])], root / "out", cfg)
    dt = time.time() - t
    st = score_table(json.loads((root / "out" / "BANG_CAT_AUTO.json").read_text("utf-8"))["frames"], truth, srcs)
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(json.dumps({k: v for k, v in st.items() if k != "wrong_list"}))
    print(f"tổng thời gian (phân tích+dựng+QA): {dt:.0f}s, RAM đỉnh {rss:.0f} MB, "
          f"QA={res['qa']['status'] if res['qa'] else None}")
    cache = sum(f.stat().st_size for f in (root / "cache").rglob("*") if f.is_file()) / 1e6
    print(f"cache: {cache:.0f} MB")


if __name__ == "__main__":
    main()
