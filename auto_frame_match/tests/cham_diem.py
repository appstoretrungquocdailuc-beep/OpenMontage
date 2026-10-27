"""Chấm điểm nhanh 1 kịch bản: python tests/cham_diem.py <tên_kịch_bản> <thư_mục> [--per] [-v]"""
import json, sys, time, logging
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from afm.config import AppConfig
from afm.log import setup_logging
from afm.pipeline import analyze
from scenarios import build_scenario, score_table

root = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/afm_dev")
name = sys.argv[1]
setup_logging(None, logging.DEBUG if "-v" in sys.argv else logging.INFO)
srcs, paths, edit, truth = build_scenario(name, root, per_scenario_sources="--per" in sys.argv)
cfg = AppConfig(); cfg.cache_dir = str(root / "cache")
t = time.time()
res = analyze(str(edit), [str(paths[s]) for s in sorted(paths)], root / f"out_{name}", cfg)
print("time", round(time.time() - t, 1))
print(json.dumps(score_table(res["doc"]["frames"], truth, srcs)))
for r in res["doc"]["segments"]:
    print(r["segment_id"], r["status"], r["edit_frame_start"], r["edit_frame_end"], r["source_id"], r.get("source_frame_start"), r.get("speed"), r.get("flip"), r.get("crop_x"), r.get("crop_y"), r.get("crop_w"), r.get("crop_h"), r["score"], r["reasons"][:150])
