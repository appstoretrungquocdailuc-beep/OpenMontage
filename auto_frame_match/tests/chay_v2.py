"""Chạy nhanh Chế độ A V2 trên 1 kịch bản: python tests/chay_v2.py <kịch_bản> <thư_mục> [preview]"""
import json, sys, logging
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent)); sys.path.insert(0, str(Path(__file__).resolve().parent))
from afm.config import AppConfig
from afm.log import setup_logging
from afm.pipeline import run_mode_a
from scenarios import build_scenario
setup_logging(None, logging.INFO)
name, root = sys.argv[1], Path(sys.argv[2])
srcs, paths, edit, truth = build_scenario(name, root, per_scenario_sources=True)
cfg = AppConfig(); cfg.cache_dir = str(root / "cache")
if "preview" in sys.argv: cfg.render.export_mode = "preview"
out = root / f"full_{name}_{'preview' if 'preview' in sys.argv else 'strict'}"
r = run_mode_a(str(edit), [str(paths[s]) for s in sorted(paths)], out, cfg)
print("TIMELINE", json.dumps(r["timeline"]))
print("verified", r["verified"], "render", bool(r["render"]))
if r["qa"]:
    q = r["qa"]; print("QA", q["status"], q["frames"], len(truth["frames"]), round(q["duration"], 3), q["global_issues"])
    for s in q["segments"]: print("  run", s.get("run_id"), s.get("origins"), s["status"], s.get("median"), s["issues"][:2])
