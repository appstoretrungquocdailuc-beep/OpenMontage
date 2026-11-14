# -*- coding: utf-8 -*-
"""LAB A/B (Chromium thật, trang Upload giả, máy chủ upload sau 'proxy ở xa' mô phỏng: kết nối mới chờ 300 ms, rảnh 1,5 s thì đóng):
  1) preconnect: base vs pre — chạy ĐÚNG cli_bench_net của TAM (DRY-RUN), 10 vòng A-B-B-A.
  2) chi phí bộ đo: TAM_NET_TRACE bật vs tắt — POST_READY (gắn file → nút Post sáng), 10 vòng xen kẽ.
Số đo là LAB (độ trễ mô phỏng), KHÔNG phải số trên VPS/proxy Nhật."""
import json
import os
import random
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
from playwright.sync_api import sync_playwright  # noqa: E402
import cdp_chrome  # noqa: E402
from latency_proxy import LatencyProxy  # noqa: E402


def main(rounds=10):
    work = Path(tempfile.mkdtemp(prefix="netlab_"))
    os.environ.update({"TAM_HIGH_PRIORITY": "0", "TAM_PERF_LOG": str(work / "perf.jsonl"), "TAM_JOURNAL": str(work / "j.jsonl"),
                       "TAM_UPLOAD_URL_PATH": "/tiktokstudio/upload", "TAM_UPLOAD_HOSTS_FILE": str(work / "hosts.json")})
    from ws360_tam_bridge import load_tam
    tam = load_tam(str(ROOT / "TAM_PATCH" / "tam_INSTANT_POST.py"))
    f = work / "v.mp4"
    f.write_bytes(random.Random(9).randbytes(1_500_000))
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = subprocess.Popen([sys.executable, str(HERE / "mock_tiktok.py"), str(port), "80", "400"], stdout=subprocess.PIPE, text=True)
    assert srv.stdout.readline().startswith("READY")
    lp = LatencyProxy(port, connect_ms=300, idle_s=1.5)
    out = {}
    with sync_playwright() as pw:
        h = cdp_chrome.start(pw, work)
        try:
            tam.TARGET_UPLOAD_URL = f"http://127.0.0.1:{port}/tiktokstudio/upload?nodes=300&up=http://127.0.0.1:{lp.port}"
            tam._open_one = lambda pid: h["drv"]
            tam._close_one = lambda pid, d, keep: None
            tam.BASE_DIR = str(ROOT)
            tam.cli_bench_net("LAB", str(f), rounds, True, False, idle_s=2.0, lead_s=1.0)
            # 2) chi phí bộ đo
            rows = []
            for r in range(rounds):
                for on in ((True, False) if r % 2 == 0 else (False, True)):
                    tam.CFG.net_trace = on
                    tam.CFG.upload_preconnect = False
                    fsm = tam.get_fsm("OV")
                    fsm.reset_unknown("t")
                    fsm.dirty = True
                    tam.prepare_upload_page(h["drv"], lambda *a: None, None, fsm)
                    time.sleep(2.0)
                    tr = tam.PerfTrace(label="ov", profile="OV", mode="LAB")
                    res = tam.upload_video_to_tiktok_ex(h["drv"], [str(f)], logger_func=lambda *_: None, pid="OV", mode="LAB", trace=tr,
                                                        dry_run=True, hooks=tam.JobHooks(before_click=lambda: False))
                    a, b = tr.marks.get("SEND_KEYS_END"), tr.marks.get("PROCESSING_READY")
                    rows.append({"net_trace": on, "ok": res.status.value == "DRY_RUN",
                                 "post_ready_ms": round((b.py_ns - a.py_ns) / 1e6, 1) if a and b and a.py_ns and b.py_ns else None})
            for on in (False, True):
                v = [x["post_ready_ms"] for x in rows if x["net_trace"] is on and x["ok"] and x["post_ready_ms"] is not None]
                out[f"net_trace_{'on' if on else 'off'}"] = {"n": len(v), "median": round(statistics.median(v), 1),
                                                             "p90": round(sorted(v)[int(0.9 * (len(v) - 1))], 1)}
            xa = [x["post_ready_ms"] for x in rows if x["net_trace"] is False and x["ok"]]
            xb = [x["post_ready_ms"] for x in rows if x["net_trace"] is True and x["ok"]]
            dd, lo, hi = tam.bootstrap_median_diff(xa, xb)
            out["overhead_on_minus_off"] = {"median_ms": dd, "ci95": [lo, hi], "p": tam.mann_whitney_p(xa, xb)}
        finally:
            cdp_chrome.stop(h)
            srv.terminate()
            lp.close()
    (ROOT / "NET_TRACE_OVERHEAD_LAB.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 10)
