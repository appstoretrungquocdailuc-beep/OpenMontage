# -*- coding: utf-8 -*-
"""Đo mạng upload bằng CDP + preconnect máy chủ upload — Chromium THẬT có cổng gỡ lỗi + trang Upload GIẢ; máy chủ upload ở origin
khác, đặt sau bộ chuyển tiếp mô phỏng proxy ở xa (kết nối MỚI chờ 300 ms, rảnh 1,5 s thì đóng). Chỉ DRY-RUN, không TikTok."""
import json
import os
import random
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
LAB = HERE / "lab"
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(LAB))
pw_api = pytest.importorskip("playwright.sync_api")
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    work = tmp_path_factory.mktemp("netlab")
    os.environ.update({"TAM_HIGH_PRIORITY": "0", "TAM_PERF_LOG": str(work / "perf.jsonl"), "TAM_JOURNAL": str(work / "j.jsonl"),
                       "TAM_UPLOAD_URL_PATH": "/tiktokstudio/upload"})
    from ws360_tam_bridge import load_tam
    tam = load_tam(str(HERE / "fixtures" / "tam_INSTANT_POST.py"))
    tam.CFG.upload_hosts_file = str(work / "hosts.json")
    f = work / "v.mp4"
    f.write_bytes(random.Random(5).randbytes(1_500_000))
    port = _free_port()
    srv = subprocess.Popen([sys.executable, str(LAB / "mock_tiktok.py"), str(port), "80", "400"], stdout=subprocess.PIPE, text=True)
    assert srv.stdout.readline().startswith("READY")
    from latency_proxy import LatencyProxy
    lp = LatencyProxy(port, connect_ms=300, idle_s=1.5)
    import cdp_chrome
    p = pw_api.sync_playwright().start()
    try:
        h = cdp_chrome.start(p, work)
    except Exception as e:  # noqa: BLE001
        srv.terminate()
        p.stop()
        pytest.skip(f"không chạy được Chromium có cổng gỡ lỗi: {e}")
    url = f"http://127.0.0.1:{port}/tiktokstudio/upload?nodes=60&up=http://127.0.0.1:{lp.port}"
    yield {"tam": tam, "h": h, "drv": h["drv"], "url": url, "file": str(f), "work": work, "lp": lp}
    cdp_chrome.stop(h)
    p.stop()
    srv.terminate()
    lp.close()


def run(lab, pre, pid="NL", lead=1.0, dry=True):
    tam, drv = lab["tam"], lab["drv"]
    tam.CFG.upload_preconnect = pre
    tam.CFG.net_trace = True
    tam.TARGET_UPLOAD_URL = lab["url"]
    fsm = tam.get_fsm(pid)
    fsm.reset_unknown("t")
    fsm.dirty = True
    tam.prepare_upload_page(drv, lambda *a: None, None, fsm)
    time.sleep(2.0)                                           # > idle_s: kết nối cũ tới máy chủ upload đã bị đóng
    if pre:
        tam.remember_tab(drv)
        tam.prewarm_upload_connections(drv)                   # như lúc WS360 PHÁT HIỆN video
    time.sleep(lead)                                          # như lúc đang tải YouTube
    logs = []
    tr = tam.PerfTrace(label="n", profile=pid, mode="T")
    res = tam.upload_video_to_tiktok_ex(drv, [lab["file"]], logger_func=logs.append, pid=pid, mode="T", trace=tr, dry_run=dry,
                                        hooks=tam.JobHooks(before_click=lambda: False))
    return res, tr.flags, logs


def test_recorder_timeline_body_unknown_and_never_logs_secrets(lab):
    res, fl, logs = run(lab, pre=False)
    assert res.status.value == "DRY_RUN" and not res.clicked
    assert fl["net_upload_reqs"] >= 2 and fl["net_upload_failed"] == 0 and fl["net_body_sent"] == "UNKNOWN"
    for k in ("net_attach_to_upload_start_ms", "net_upload_span_ms", "net_attach_to_upload_done_ms", "net_processing_ms"):
        assert isinstance(fl[k], float) and fl[k] >= 0, k
    assert 350 <= fl["net_processing_ms"] <= 1500                     # 'TikTok xử lý' giả = 400 ms + trang tự hỏi mỗi 300 ms
    blob = json.dumps(fl, ensure_ascii=False) + "\n".join(logs)
    assert "SECRETSIG" not in blob and "COOKIESECRET" not in blob and "X-Sig" not in blob   # không query, không cookie
    assert any("[UPLOAD_NET]" in x and "gửi HẾT body: UNKNOWN" in x for x in logs)
    assert json.loads(Path(lab["tam"].CFG.upload_hosts_file).read_text())["hosts"]           # đã học máy chủ upload (chỉ host)


def test_preconnect_reuses_warm_connection_and_cuts_upload_time(lab):
    base = [run(lab, pre=False)[1] for _ in range(3)]
    pre = [run(lab, pre=True)[1] for _ in range(3)]
    assert all(x["net_first_conn_new"] is True for x in base)
    assert all(x["net_first_conn_new"] is False for x in pre)                # dùng lại kết nối đã mở sẵn
    mb = sorted(x["net_attach_to_upload_done_ms"] for x in base)[1]
    mp = sorted(x["net_attach_to_upload_done_ms"] for x in pre)[1]
    assert mb - mp >= 200, (mb, mp)                                           # ~300 ms bắt tay mô phỏng được bỏ khỏi đường upload


def test_preconnect_off_by_flag_adds_nothing(lab):
    tam = lab["tam"]
    tam.CFG.upload_preconnect = False
    assert tam.preconnect_upload_hosts(lab["drv"])[0] == 0
    tam.CFG.upload_preconnect = True


def test_bridge_prewarm_runs_in_background_for_channel_profiles(monkeypatch):
    import ws360_tam_bridge as BR
    calls = []

    class FakeTam:
        drivers_lock = __import__("threading").Lock()
        drivers = {"77": object()}

        @staticmethod
        def prewarm_upload_connections(drv):
            time.sleep(0.2)
            calls.append(drv)
            return 2, "h"
    b = BR.TikTokBridge.__new__(BR.TikTokBridge)
    b.tam, b.accepting, b.targets, b.stats, b.log = FakeTam, True, {"UCx": ["77", "88"]}, {}, lambda *_: None
    t0 = time.perf_counter()
    assert b.prewarm("UCx") == 1 and time.perf_counter() - t0 < 0.1         # không chặn luồng phát hiện
    time.sleep(0.4)
    assert calls == [FakeTam.drivers["77"]]


def test_first_download_report_reads_only_first_attempts(tmp_path):
    sys.path.insert(0, str(HERE))
    import test_ws360 as T
    ws = T.ws
    db = ws.DB(tmp_path / "database.sqlite")
    for i, (att, ttfb) in enumerate([(1, 120), (1, 180), (2, 900)]):
        net = {"resolver": {"client": "android_vr", "new_conn": False, "tls_ms": 0}, "ffprobe_ms": 40,
               "media": {"host": "rr1---sn-x.googlevideo.com", "new_conns": 1, "dns_ms": 5, "tcp_ms": 8, "tls_ms": 20, "ttfb_ms": ttfb,
                         "transfer_ms": 500}}
        db.x("INSERT INTO videos(video_id, state, size, resolver_ms, download_ms, total_ms, mbps, done_at, attempts, net) "
             "VALUES(?,?,?,?,?,?,?,?,?,?)", (f"FDL{i:08d}", "DONE", 8_000_000, 300, 700, 1400, 90, ws.iso(), att, json.dumps(net)))
    out = ws.first_dl_report(tmp_path)
    assert out["first"] == 2
    md = (tmp_path / "FIRST_DL_REPORT.md").read_text(encoding="utf-8")
    assert "| cdn_ttfb_ms | 150 |" in md and (tmp_path / "FIRST_DL_REPORT.csv").exists()


def test_bench_net_cli_dry_run_ab_writes_csv_and_summary(lab, monkeypatch, tmp_path):
    tam = lab["tam"]
    tam.TARGET_UPLOAD_URL = lab["url"]
    monkeypatch.setattr(tam, "_open_one", lambda pid: lab["drv"])
    monkeypatch.setattr(tam, "_close_one", lambda pid, d, keep: None)
    monkeypatch.setattr(tam, "BASE_DIR", str(tmp_path))
    assert tam.cli_bench_net("NB", lab["file"], 2, True, False, idle_s=2.0, lead_s=1.0) == 0
    rows = json.loads(next(tmp_path.glob("tam_bench_net_*.json")).read_text(encoding="utf-8"))["rows"]
    meas = [r for r in rows if r["variant"] in ("base", "pre")]
    assert len(meas) == 4 and all(r["result"] == "OK" and r["clicked"] is False for r in meas)
    assert all(r["net_first_conn_new"] is False for r in meas if r["variant"] == "pre")
    summ = next(tmp_path.glob("tam_bench_net_*_summary.csv")).read_text(encoding="utf-8-sig")
    assert summ.startswith("variant,runs,ok,error_rate_pct,post_ready_ms_median") and "\npre," in summ


def test_chrome_semantics_maxPostDataSize_zero_leaks_body_one_does_not(lab):
    """Kiểm chứng TRÊN CHROMIUM THẬT (lỗi review tìm ra): maxPostDataSize=0 = KHÔNG giới hạn (body bị gửi qua CDP);
    =1 → body không rời Chrome. Bộ đo dùng 1."""
    tam, page = lab["tam"], lab["drv"].page
    page.goto(lab["url"])
    url = tam._tab_ws_url(lab["drv"])
    seen = {}
    for cap in (0, 1):
        c = tam.CdpWs(url, url.rsplit("/", 1)[-1])
        c.call("Network.enable", {"maxPostDataSize": cap})
        page.evaluate("() => fetch('/x', {method: 'POST', body: JSON.stringify({s: 'BODYSECRET-CAP'})}).catch(() => 0)")
        end = time.time() + 3
        while time.time() < end and cap not in seen:
            c.ws.settimeout(0.3)
            try:
                m = json.loads(c.ws.recv())
            except Exception:
                continue
            if m.get("method") == "Network.requestWillBeSent" and (m["params"]["request"].get("method") == "POST"):
                rq = m["params"]["request"]
                seen[cap] = bool(rq.get("postData") or rq.get("postDataEntries"))
        c.close()
    assert seen == {0: True, 1: False}


def test_recorder_never_receives_bodies_and_counts_preflight_as_the_new_connection(lab, monkeypatch):
    tam = lab["tam"]
    got = []
    real = tam.NetRecorder._handle

    def spy(self, meth, p, sid):
        if meth == "Network.requestWillBeSent":
            rq = p.get("request") or {}
            got.append(bool(rq.get("postData") or rq.get("postDataEntries")))
        return real(self, meth, p, sid)
    monkeypatch.setattr(tam.NetRecorder, "_handle", spy)
    url0 = lab["url"]
    lab["url"] = url0 + "&pf=1"                                   # upload có header riêng → trình duyệt gửi preflight OPTIONS trước
    try:
        res, fl, logs = run(lab, pre=False, pid="NP")
    finally:
        lab["url"] = url0
    assert res.status.value == "DRY_RUN" and got and not any(got)   # không request nào mang body sang recorder
    # Chrome báo preflight OPTIONS ~1 ms SAU POST nhưng chính preflight mở kết nối → phải nhận ra 'MỚI' (do OPTIONS)
    assert fl["net_first_conn_new"] is True and fl["net_first_conn_by"] == "OPTIONS"
    assert "BODYSECRET" not in json.dumps(fl, ensure_ascii=False) + "\n".join(logs)


def test_summary_unknowns_instead_of_guesses(lab):
    tam = lab["tam"]
    rec = tam.NetRecorder("ws://x/devtools/page/T")
    rec.marks["attach"] = 1000.0
    base = {"wall": 1000.1, "ts": 50.1, "post": True, "w": False, "host": "up.example.com", "u": "up.example.com/upload"}
    rec.reqs[(None, "1")] = dict(base, m="POST", hint=True)                                # chưa có phản hồi, chưa có timing
    rec.reqs[(None, "2")] = dict(base, m="POST", hint=False, u="mon.example.com/collect", host="mon.example.com",
                                 wall=1002.0, ts=52.0, resp_ts=52.1)                       # beacon sau click: KHÔNG phải API đăng
    sm = rec.summary(1001.0, 1001.9, 1002.5)
    assert sm["net_first_conn_new"] is None                    # không có timing → UNKNOWN (trước đây báo 'dùng lại')
    assert sm["net_upload_span_ms"] is None                    # request upload chưa xong → UNKNOWN
    assert sm["net_click_to_publish_resp_ms"] is None          # beacon không khớp TAM_PUBLISH_URL_RE → UNKNOWN
    rec.reqs[(None, "1")].update(end_ts=50.6, timing={"connectStart": 0, "connectEnd": 120, "sslStart": 40, "sslEnd": 120})
    sm = rec.summary(1001.0, 1001.9, 1002.5)
    assert sm["net_first_conn_new"] is True and sm["net_new_conns"] == 1 and sm["net_first_connect_ms"] == 120
    assert sm["net_upload_span_ms"] == 500.0 and sm["net_body_sent"] == "UNKNOWN"
