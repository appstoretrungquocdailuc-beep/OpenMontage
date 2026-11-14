# -*- coding: utf-8 -*-
"""LAB: độ trễ NÚT POST SÁNG → CLICK trên Chromium THẬT + trang Upload GIẢ (mock_tiktok.py). Chạy MÃ TAM THẬT, BẤM THẬT vào
trang giả (không có TikTok). So sánh:
  OLD   = TAM 5.3.0 (backup) — đường chromedriver, watcher 5.3.0
  DRV   = TAM 5.4 TEST, TAM_CLICK_PATH=driver
  WS    = TAM 5.4 TEST, TAM_CLICK_PATH=ws (WebSocket CDP riêng, promise không đứt quãng)
Kịch bản: visible (tab hiển thị) | hidden (tab bị tab khác che → Chrome bóp hẹn giờ) | coverhid (nút sáng nhưng bị lớp phủ che 600 ms, tab ẩn).
Thước đo ĐỘC LẬP với TAM (trang giả tự ghi, đồng hồ trang): enabled→click (nút bật → hàm onclick của trang chạy),
uncover→click (hết che → onclick). Kèm: kết quả (CONFIRMED), số lần /post máy chủ nhận (phải = 1), click khi nút còn tắt/bị che (phải = 0).
LƯU Ý: Lab dùng Playwright thay chromedriver → đường 'driver' ở lab NHANH hơn máy thật nhiều (không có bước HTTP của chromedriver).
Dùng: python bench_click_lab.py [--rounds 10]"""
import argparse
import json
import os
import random
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
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")

from playwright.sync_api import sync_playwright  # noqa: E402
import cdp_chrome  # noqa: E402
from bench_watch_lab import free_port  # noqa: E402

SCEN = {"visible": ("nodes=300", False), "hidden": ("nodes=300", True), "coverhid": ("nodes=300&coverhold=600", True)}


def pct(xs, p):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    return round(xs[f] + (xs[c] - xs[f]) * (k - f), 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--scen", default="visible,hidden,coverhid")
    ap.add_argument("--variants", default="OLD,DRV,WS")
    ap.add_argument("--out", default=str(ROOT / "FAST_CLICK_LAB.json"))
    a = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="clicklab_"))
    os.environ.update({"TAM_HIGH_PRIORITY": "0", "TAM_PERF_LOG": str(work / "perf.jsonl"), "TAM_JOURNAL": str(work / "j.jsonl"),
                       "TAM_UPLOAD_URL_PATH": "/tiktokstudio/upload"})
    from ws360_tam_bridge import load_tam
    mods = {"OLD": load_tam(str(ROOT / "backup" / "tam_INSTANT_POST_5.3.0_truoc_fast_click.py")),
            "NEW": load_tam(str(ROOT / "TAM_PATCH" / "tam_INSTANT_POST.py"))}
    f = work / "v.mp4"
    f.write_bytes(random.Random(3).randbytes(1_200_000))
    port = free_port()
    srv = subprocess.Popen([sys.executable, str(HERE / "mock_tiktok.py"), str(port), "40", "500"], stdout=subprocess.PIPE, text=True)
    assert srv.stdout.readline().startswith("READY")
    base = f"http://127.0.0.1:{port}"
    variants = [v for v in a.variants.split(",") if v]
    rows = []
    with sync_playwright() as pw:
        h = cdp_chrome.start(pw, work)
        page = h["page"]
        other = page.context.new_page()                      # tab che: đưa lên trước → tab upload thành 'hidden'
        other.goto("about:blank")
        try:
            for scen in a.scen.split(","):
                q, hidden = SCEN[scen]
                for r in range(a.rounds):
                    order = variants[r % len(variants):] + variants[:r % len(variants)]
                    if r % 2:
                        order = order[::-1]
                    for v in order:
                        tam = mods["OLD" if v == "OLD" else "NEW"]
                        if v != "OLD":
                            tam.CFG.click_path = "ws" if v == "WS" else "driver"
                        tam.TARGET_UPLOAD_URL = f"{base}/tiktokstudio/upload?{q}"
                        pid = f"L{v}"
                        fsm = tam.get_fsm(pid)
                        fsm.reset_unknown("lab")
                        fsm.dirty = True
                        logs = []

                        def log(msg, logs=logs):
                            logs.append(msg)
                            if hidden and "[Timing] T1" in msg:      # sau khi gắn file: đưa tab khác lên trước → tab upload bị ẩn
                                h["cdp"].send("Target.activateTarget", {"targetId": tid_other})
                        tid_other = other.context.new_cdp_session(other).send("Target.getTargetInfo")["targetInfo"]["targetId"]
                        if not hidden:
                            page.bring_to_front()
                        res = tam.upload_video_to_tiktok_ex(h["drv"], [str(f)], logger_func=log, pid=pid, mode="LAB")
                        time.sleep(0.15)
                        try:
                            mk = page.evaluate("() => window.__mock ? Object.assign({vis: document.visibilityState, id: window.__mock.id}, window.__mock.marks) : null")
                        except Exception:
                            mk = None
                        mk = mk or {}
                        srvlog = {}
                        if mk.get("id"):
                            try:
                                srvlog = json.loads(urllib.request.urlopen(f"{base}/log?id={mk['id']}", timeout=3).read())
                            except Exception:
                                pass
                        cp = (res.trace.flags if res.trace else {}) or {}
                        d_ = lambda x, y: None if x is None or y is None else round(x - y, 1)
                        row = {"scen": scen, "variant": v, "round": r + 1, "result": res.status.value, "reason": res.reason[:80],
                               "vis_at_end": mk.get("vis"), "enabled_to_click_ms": d_(mk.get("postClick"), mk.get("enabled")),
                               "uncover_to_click_ms": d_(mk.get("postClick"), mk.get("coverOff")),
                               "clicked_while_covered": bool(mk.get("coverOff") and mk.get("postClick") and mk["postClick"] < mk["coverOff"]),
                               "server_posts": srvlog.get("post_count", 0),
                               "cp_path": cp.get("cp_path"), "rtc_page_ms": cp.get("cp_rtc_page_ms"),
                               "lit_to_click_ms": cp.get("cp_lit_to_click_ms"), "lit_to_ready_ms": cp.get("cp_lit_to_ready_ms"),
                               "flush_ms": cp.get("cp_watcher_flush_ms"), "via": cp.get("cp_ready_via"),
                               "py_preclick_ms": cp.get("cp_py_preclick_ms"), "cdp_cmd_ms": cp.get("cp_cdp_cmd_ms"),
                               "press_ack_ms": cp.get("cp_press_ack_ms"), "snap_tries": cp.get("cp_snap_tries")}
                        rows.append(row)
                        print(json.dumps(row, ensure_ascii=False), flush=True)
                        page.bring_to_front()
        finally:
            cdp_chrome.stop(h)
            srv.terminate()
    out = {"rows": rows, "summary": {}}
    for scen in a.scen.split(","):
        for v in variants:
            rs = [x for x in rows if x["scen"] == scen and x["variant"] == v]
            ok = [x for x in rs if x["result"] == "CONFIRMED"]
            out["summary"][f"{scen}/{v}"] = {"n": len(rs), "confirmed": len(ok), "double_posts": sum(1 for x in rs if x["server_posts"] > 1),
                                             "clicked_while_covered": sum(1 for x in rs if x["clicked_while_covered"]),
                                             **{f"{k}_p50": pct([x[k] for x in ok], 50) for k in ("enabled_to_click_ms", "uncover_to_click_ms", "rtc_page_ms")},
                                             **{f"{k}_p90": pct([x[k] for x in ok], 90) for k in ("enabled_to_click_ms", "uncover_to_click_ms", "rtc_page_ms")}}
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n=== enabled→click (trang tự đo, ms) P50 / P90 | hết che→click P50 / P90 | CONFIRMED | đăng trùng | bấm lúc bị che ===")
    for k, sm in out["summary"].items():
        print(f"{k:16} {sm['enabled_to_click_ms_p50']} / {sm['enabled_to_click_ms_p90']} | {sm['uncover_to_click_ms_p50']} / "
              f"{sm['uncover_to_click_ms_p90']} | {sm['confirmed']}/{sm['n']} | {sm['double_posts']} | {sm['clicked_while_covered']}")


if __name__ == "__main__":
    main()
