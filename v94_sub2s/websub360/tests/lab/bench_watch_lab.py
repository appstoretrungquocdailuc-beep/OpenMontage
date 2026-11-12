# -*- coding: utf-8 -*-
"""LAB A/B/C: watcher nút Post có làm UPLOAD chậm không? Chạy MÃ TAM THẬT (upload_video_to_tiktok_ex, dry_run=True)
trên Chromium thật + trang Upload giả (mock_tiktok.py, process riêng).

  A = event  (watcher hiện tại: observer toàn trang + tổ tiên nút + vòng 8 ms khi bị che)
  B = sparse (không cài gì trong lúc upload; đọc thuộc tính nút mỗi TAM_SPARSE_MS)
  C = lite   (observer CHỈ trên nút Post, không ép layout)

Đo mỗi lượt (cùng file, cùng trang vừa nạp lại, thứ tự A/B/C xoay vòng):
  transfer_ms   máy chủ: khúc đầu → khúc cuối nhận xong  | gap_ms: tổng thời gian máy chủ RẢNH chờ khúc tiếp (trễ phía trình duyệt)
  processing_ms trang: upload xong → nút Post BẬT (gồm 'TikTok xử lý' cố định + trang tự hỏi trạng thái)
  enabled_ms    trang: gắn file → nút Post BẬT  (= thời gian THẬT tới Post READY, không phụ thuộc watcher phát hiện)
  detected_ms   Python nhận READY − gắn file  | lag_ms = detected − enabled
  CPU Chrome (cả cây process) / Python, số lệnh WebDriver, long task (>50 ms), CDP TaskDuration/Layout/RecalcStyle.
Dùng: python bench_watch_lab.py [--rounds 4] [--out DIR]
"""
import argparse
import json
import os
import random
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import psutil  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402
from pw_adapter import PWDriver  # noqa: E402

MODES = {"A": "event", "B": "sparse", "C": "lite"}
SCEN = {"light": "nodes=60", "heavy": "nodes=800&rbtn=1", "cover": "nodes=300&cover=1"}
CDPK = ("TaskDuration", "ScriptDuration", "LayoutDuration", "RecalcStyleDuration", "LayoutCount", "RecalcStyleCount")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def chrome_cpu(udd):
    tot = 0.0
    for p in psutil.process_iter(["cmdline"]):
        try:
            if udd in " ".join(p.info["cmdline"] or []):
                t = p.cpu_times()
                tot += t.user + t.system
        except Exception:
            pass
    return tot


def cdp_metrics(cdp):
    r = cdp.send("Performance.getMetrics")
    return {m["name"]: m["value"] for m in r["metrics"] if m["name"] in CDPK}


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--bw", type=float, default=25.0, help="Mbps máy chủ giả")
    ap.add_argument("--proc", type=float, default=1200.0, help="ms 'TikTok xử lý' sau khúc cuối")
    ap.add_argument("--sizes", default="2.67,9.11")
    ap.add_argument("--scen", default="light,heavy,cover")
    ap.add_argument("--out", default=str(ROOT))
    a = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="tamlab_"))
    os.environ.update({"TAM_HIGH_PRIORITY": "0", "TAM_PERF_LOG": str(work / "perf.jsonl"), "TAM_JOURNAL": str(work / "j.jsonl"),
                       "TAM_UPLOAD_URL_PATH": "/tiktokstudio/upload"})
    from ws360_tam_bridge import load_tam
    tam = load_tam(str(ROOT / "tests" / "fixtures" / "tam_INSTANT_POST.py"))

    files = {}
    rnd = random.Random(7)
    for mb in [float(x) for x in a.sizes.split(",")]:
        f = work / f"video_{mb}MB.mp4"
        f.write_bytes(rnd.randbytes(int(mb * 1e6)))
        files[mb] = str(f)

    port = free_port()
    srv = subprocess.Popen([sys.executable, str(HERE / "mock_tiktok.py"), str(port), str(a.bw), str(a.proc)], stdout=subprocess.PIPE, text=True)
    assert srv.stdout.readline().startswith("READY")
    base = f"http://127.0.0.1:{port}"
    rows = []
    udd = str(work / "profile")
    try:
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(udd, headless=True, channel="chromium", viewport={"width": 1280, "height": 800})
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            cdp = ctx.new_cdp_session(page)
            cdp.send("Performance.enable")
            drv = PWDriver(page, cdp)
            page.goto(f"{base}/tiktokstudio/upload")
            for scen in a.scen.split(","):
                for mb, fp in files.items():
                    for r in range(a.rounds):
                        order = ["A", "B", "C"]
                        order = order[r % 3:] + order[:r % 3]          # xoay vòng thứ tự để triệt tiêu trôi theo thời gian
                        if r % 2:
                            order.reverse()
                        for m in order:
                            rows.append(run_one(tam, drv, cdp, page, base, scen, mb, fp, m, r, udd))
                            x = rows[-1]
                            print(f"{scen:6} {mb:5}MB r{r} {m}: enabled {x['enabled_ms']} | detected {x['detected_ms']} | "
                                  f"lag {x['lag_ms']} | transfer {x['transfer_ms']} gap {x['gap_ms']} | proc {x['processing_ms']} | "
                                  f"chromeCPU {x['chrome_cpu_s']} | cmds {x['cmds']} | long {x['longtask_ms']} | {x['result']}", flush=True)
            confirm = {m: confirm_check(tam, drv, base, files[min(files)], m) for m in ("A", "B", "C")}
            ctx.close()
    finally:
        srv.terminate()
    summ = summarize(rows)
    out = Path(a.out)
    (out / "WATCH_BENCH_LAB.json").write_text(json.dumps({"params": vars(a), "rows": rows, "summary": summ, "confirm_check": confirm},
                                                         ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summ, ensure_ascii=False, indent=1))
    print("CONFIRM CHECK (không dry-run, trang giả):", confirm)


def run_one(tam, drv, cdp, page, base, scen, mb, fp, m, r, udd):
    tam.CFG.wait_mode = MODES[m]
    tam.TARGET_UPLOAD_URL = f"{base}/tiktokstudio/upload?{SCEN[scen]}"
    tam.get_fsm("LAB").dirty = True
    snap = {}

    def log(msg):
        if "[Timing] T1" in msg:                           # ngay sau khi gắn file
            snap["t_att"] = time.time()
            snap["c0"], snap["p0"], snap["n0"], snap["m0"] = chrome_cpu(udd), time.process_time(), drv.command_count, cdp_metrics(cdp)
        elif "💡 [READY]" in msg:                          # Python vừa nhận READY
            snap["t_det"] = time.time()
            snap["c1"], snap["p1"], snap["n1"], snap["m1"] = chrome_cpu(udd), time.process_time(), drv.command_count, cdp_metrics(cdp)
    tr = tam.PerfTrace(label=f"lab-{m}", profile="LAB", mode="LAB")
    res = tam.upload_video_to_tiktok_ex(drv, [fp], logger_func=log, check_force_kill=None, pid="LAB", mode="LAB", trace=tr, dry_run=True)
    mk = page.evaluate("() => window.__mock")
    srv = json.loads(urllib.request.urlopen(f"{base}/log?id={mk['id']}", timeout=5).read() or b"{}") if mk.get("id") else {}
    M = mk.get("marks") or {}
    att, en = M.get("attach"), M.get("enabled")
    lt = [d for (t, d) in (mk.get("long") or []) if att and en and att - 50 <= t <= en]
    row = {"scen": scen, "mb": mb, "mode": m, "round": r, "result": res.status.value,
           "enabled_ms": round(en - att) if att and en else None,
           "upload_ms": round(M["uploaded"] - att) if att and M.get("uploaded") else None,
           "processing_ms": round(en - M["uploaded"]) if en and M.get("uploaded") else None,
           "transfer_ms": round(srv["last_done"] - srv["first_start"]) if srv.get("last_done") else None,
           "attach_to_first_chunk_ms": round(srv["first_start"] - att) if srv.get("first_start") and att else None,
           "gap_ms": round(srv.get("gap_ms", 0)), "max_gap_ms": round(srv.get("max_gap_ms", 0)),
           "detected_ms": round(snap["t_det"] * 1000 - att) if att and snap.get("t_det") else None,
           "renders": mk.get("renders"), "longtask_n": len(lt), "longtask_ms": round(sum(lt))}
    row["lag_ms"] = (row["detected_ms"] - row["enabled_ms"]) if row["detected_ms"] is not None and row["enabled_ms"] is not None else None
    if "c1" in snap:
        row["chrome_cpu_s"] = round(snap["c1"] - snap["c0"], 3)
        row["py_cpu_s"] = round(snap["p1"] - snap["p0"], 3)
        row["cmds"] = snap["n1"] - snap["n0"]
        win = max(1e-3, snap["t_det"] - snap["t_att"])
        row["chrome_cpu_pct"] = round(100 * row["chrome_cpu_s"] / win, 1)
        for k in CDPK:
            d = snap["m1"].get(k, 0) - snap["m0"].get(k, 0)
            row["cdp_" + k] = round(d * 1000, 1) if k.endswith("Duration") else int(d)
    return row


def confirm_check(tam, drv, base, fp, m):
    """Không dry-run trên TRANG GIẢ: B/C cài watcher đầy đủ sau khi nút bật → bảo vệ lúc nhấn + POST_CONFIRMED vẫn chạy."""
    tam.CFG.wait_mode = MODES[m]
    tam.TARGET_UPLOAD_URL = f"{base}/tiktokstudio/upload?nodes=60"
    tam.get_fsm("LABC").dirty = True
    tam.get_fsm("LABC").reset_unknown("lab")
    res = tam.upload_video_to_tiktok_ex(drv, [fp], logger_func=lambda s: None, pid="LABC", mode="LAB", dry_run=False)
    return {"status": res.status.value, "evidence": res.evidence[:80], "clicked": res.clicked}


def summarize(rows):
    out = {}
    keys = ("enabled_ms", "upload_ms", "transfer_ms", "gap_ms", "max_gap_ms", "processing_ms", "detected_ms", "lag_ms", "chrome_cpu_s",
            "chrome_cpu_pct", "py_cpu_s", "cmds", "longtask_ms", "cdp_TaskDuration", "cdp_ScriptDuration", "cdp_LayoutDuration",
            "cdp_RecalcStyleDuration", "cdp_LayoutCount", "cdp_RecalcStyleCount")
    for x in rows:
        g = out.setdefault(f"{x['scen']}|{x['mb']}MB|{x['mode']}", {"n": 0, "_v": {k: [] for k in keys}})
        g["n"] += 1
        for k in keys:
            g["_v"][k].append(x.get(k))
    for g in out.values():
        for k, v in g.pop("_v").items():
            g[k] = med(v)
    return out


if __name__ == "__main__":
    main()
