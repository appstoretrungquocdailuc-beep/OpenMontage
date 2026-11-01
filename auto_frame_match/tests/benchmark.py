"""Benchmark: python tests/benchmark.py <afm_root> <data_dir> <nhan> <scenarios...>  -> JSON lines (độ đúng, thời gian, RAM)."""
import json, sys, time, logging, shutil
try:
    import resource  # chỉ có trên Linux/macOS
except ImportError:
    resource = None
from pathlib import Path
afm_root, data, label = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
sys.path.insert(0, str(afm_root))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # bộ sinh dữ liệu + chấm điểm (dùng chung)
from afm.config import AppConfig
from afm.log import setup_logging
from afm.pipeline import analyze
from scenarios import build_scenario, score_table
setup_logging(None, logging.WARNING)
for name in sys.argv[4:]:
    srcs, paths, edit, truth = build_scenario(name, data, per_scenario_sources=True)
    cfg = AppConfig(); cfg.cache_dir = str(data / f"cache_{label}")
    out = data / f"bench_{label}_{name}"
    shutil.rmtree(out, ignore_errors=True)
    t = time.time()
    res = analyze(str(edit), [str(paths[s]) for s in sorted(paths)], out, cfg)
    dt = time.time() - t
    st = score_table(res["doc"]["frames"], truth, srcs)
    st.pop("wrong_list", None)
    summ = res["doc"]["summary"]
    print(json.dumps({"label": label, "scenario": name, "seconds": round(dt, 1),
                      "rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024) if resource else None,
                      **st, "frames_green": summ["frames_green"], "frames_yellow": summ["frames_yellow"],
                      "frames_red": summ["frames_red"], "timeline": summ.get("timeline", {})}), flush=True)
