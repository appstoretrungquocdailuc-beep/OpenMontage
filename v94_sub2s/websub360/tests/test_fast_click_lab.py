# -*- coding: utf-8 -*-
"""ĐƯỜNG BẤM NHANH (TAM_CLICK_PATH=ws) trên Chromium THẬT có cổng gỡ lỗi (như profile Genlogin) + trang Upload GIẢ.
Chạy mã TAM thật; chỉ bấm vào trang giả 127.0.0.1. Kiểm: bấm đúng 1 lần, DRY-RUN không gửi lệnh chuột nào, không bấm khi bị che,
sai tab → không bấm qua ws (quay về chromedriver), Hard Stop / nhật ký chặn → không bấm, --bench-click chạy trọn và không đụng tab cũ."""
import json
import os
import random
import socket
import subprocess
import sys
import urllib.request
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
    work = tmp_path_factory.mktemp("fclab")
    os.environ.update({"TAM_HIGH_PRIORITY": "0", "TAM_PERF_LOG": str(work / "perf.jsonl"), "TAM_JOURNAL": str(work / "j.jsonl"),
                       "TAM_UPLOAD_URL_PATH": "/tiktokstudio/upload"})
    from ws360_tam_bridge import load_tam
    tam = load_tam(str(HERE / "fixtures" / "tam_INSTANT_POST.py"))
    f = work / "v.mp4"
    f.write_bytes(random.Random(2).randbytes(800_000))
    port = _free_port()
    srv = subprocess.Popen([sys.executable, str(LAB / "mock_tiktok.py"), str(port), "40", "400"], stdout=subprocess.PIPE, text=True)
    assert srv.stdout.readline().startswith("READY")
    import cdp_chrome
    p = pw_api.sync_playwright().start()
    try:
        h = cdp_chrome.start(p, work)
    except Exception as e:  # noqa: BLE001
        srv.terminate()
        p.stop()
        pytest.skip(f"không chạy được Chromium có cổng gỡ lỗi: {e}")
    yield {"tam": tam, "h": h, "drv": h["drv"], "base": f"http://127.0.0.1:{port}", "file": str(f), "work": work}
    tam.CFG.click_path = "ws"
    cdp_chrome.stop(h)
    p.stop()
    srv.terminate()


class Spy:
    def __init__(self, tam, monkeypatch):
        self.sent = []
        real = tam.CdpWs.send

        def send(this, method, params=None):
            self.sent.append(method)
            return real(this, method, params)
        monkeypatch.setattr(tam.CdpWs, "send", send)

    def mouse(self):
        return [m for m in self.sent if m.startswith("Input.")]


def run(lab, query="nodes=60", dry=False, pid="F", hooks=None, kill=None):
    tam, drv = lab["tam"], lab["drv"]
    tam.CFG.wait_mode = "event"
    tam.TARGET_UPLOAD_URL = f"{lab['base']}/tiktokstudio/upload?{query}"
    fsm = tam.get_fsm(pid)
    fsm.reset_unknown("test")
    fsm.dirty = True
    logs = []
    tr = tam.PerfTrace(label="t", profile=pid, mode="TEST")
    res = tam.upload_video_to_tiktok_ex(drv, [lab["file"]], logger_func=logs.append, pid=pid, mode="TEST", dry_run=dry, trace=tr,
                                        hooks=hooks, check_force_kill=kill)
    marks = drv.page.evaluate("() => window.__mock ? Object.assign({id: window.__mock.id}, window.__mock.marks) : {}")
    posts = 0
    if marks.get("id"):
        posts = json.loads(urllib.request.urlopen(f"{lab['base']}/log?id={marks['id']}", timeout=3).read()).get("post_count", 0)
    return res, marks, posts, tr.flags, logs


def test_ws_path_clicks_once_fast_and_confirms(lab, monkeypatch):
    lab["tam"].CFG.click_path = "ws"
    spy = Spy(lab["tam"], monkeypatch)
    res, marks, posts, fl, logs = run(lab)
    assert res.status.value == "CONFIRMED", res.reason
    assert fl["cp_path"] == "ws" and posts == 1
    assert spy.mouse().count("Input.dispatchMouseEvent") == 3            # move + press + release, ĐÚNG 1 lần
    assert fl["cp_rtc_page_ms"] is not None and fl["cp_rtc_page_ms"] < 100
    assert marks["postClick"] - marks["enabled"] < 150
    assert any("[CLICK_PATH] ws" in x for x in logs)


def test_dry_run_sends_no_mouse_event_at_all(lab, monkeypatch):
    lab["tam"].CFG.click_path = "ws"
    spy = Spy(lab["tam"], monkeypatch)
    res, marks, posts, fl, _ = run(lab, dry=True, pid="FD")
    assert res.status.value == "DRY_RUN" and not res.clicked
    assert spy.mouse() == [] and not marks.get("postClick") and posts == 0 and marks.get("enabled")


def test_never_clicks_while_covered_then_clicks_right_after(lab, monkeypatch):
    lab["tam"].CFG.click_path = "ws"
    res, marks, posts, fl, _ = run(lab, query="nodes=60&coverhold=900", pid="FC")
    assert res.status.value == "CONFIRMED", res.reason
    assert marks["postClick"] >= marks["coverOff"] > marks["enabled"] and posts == 1
    assert marks["postClick"] - marks["coverOff"] < 150                 # hết che là bấm ngay (theo thay đổi DOM)
    assert fl["cp_lit_to_ready_ms"] >= 800


def test_hook_refusal_and_hard_stop_never_click(lab, monkeypatch):
    tam = lab["tam"]
    tam.CFG.click_path = "ws"
    spy = Spy(tam, monkeypatch)
    res, marks, posts, _, _ = run(lab, pid="FH", hooks=tam.JobHooks(before_click=lambda: False))
    assert res.status.value == "FAILED_SAFE" and not marks.get("postClick") and posts == 0
    state = {"n": 0}

    def kill():                                     # Hard Stop trong lúc chờ nút Post
        state["n"] += 1
        return state["n"] > 25
    res, marks, posts, _, _ = run(lab, pid="FK", kill=kill)
    assert res.status.value == "KILLED" and not marks.get("postClick") and posts == 0
    assert spy.mouse() == []


def test_ws_connected_to_wrong_tab_never_fires_there_and_falls_back(lab, monkeypatch):
    tam = lab["tam"]
    tam.CFG.click_path = "ws"
    other = lab["drv"].page.context.new_page()
    other.goto("about:blank")
    tid = other.context.new_cdp_session(other).send("Target.getTargetInfo")["targetInfo"]["targetId"]
    real = tam.fast_click_conn

    def wrong(driver, log=None):
        c = tam.CdpWs(f"ws://{lab['h']['addr']}/devtools/page/{tid}", tid)
        return c, "test: tab khác"
    monkeypatch.setattr(tam, "fast_click_conn", wrong)
    spy = Spy(tam, monkeypatch)
    res, marks, posts, fl, logs = run(lab, pid="FW")
    other.close()
    monkeypatch.setattr(tam, "fast_click_conn", real)
    assert res.status.value == "CONFIRMED" and posts == 1 and fl["cp_path"] == "driver"     # bấm bằng chromedriver vào ĐÚNG tab
    assert spy.mouse() == [] and any("không khớp tài liệu" in x for x in logs)


def test_driver_path_still_available_for_rollback(lab):
    lab["tam"].CFG.click_path = "driver"
    try:
        res, marks, posts, fl, _ = run(lab, pid="FR")
    finally:
        lab["tam"].CFG.click_path = "ws"
    assert res.status.value == "CONFIRMED" and fl["cp_path"] == "driver" and posts == 1 and isinstance(fl["cp_cdp_cmd_ms"], list)


def test_bench_click_cli_uses_new_local_tab_and_leaves_original_tab(lab, monkeypatch, tmp_path, capsys):
    tam = lab["tam"]
    drv = lab["drv"]
    drv.page.goto(f"{lab['base']}/tiktokstudio/upload?nodes=10")
    before_url, before_handle = drv.current_url, drv.current_window_handle
    monkeypatch.setattr(tam, "_open_one", lambda pid: drv)
    monkeypatch.setattr(tam, "_close_one", lambda pid, d, keep: None)
    monkeypatch.setattr(tam, "BASE_DIR", str(tmp_path))
    target_before = tam.TARGET_UPLOAD_URL
    assert tam.cli_bench_click("P1", 3, True, False, "driver,ws", 0, "asis") == 0
    rep = json.loads(next(tmp_path.glob("tam_bench_click_*.json")).read_text(encoding="utf-8"))
    rows = rep["rows"]
    assert len(rows) == 6 and all(r["result"] == "CONFIRMED" for r in rows), [r["reason"] for r in rows]
    assert {r["cp_path"] for r in rows} == {"driver", "ws"} and all(r["server_posts"] == 1 for r in rows)
    assert rep["summary"]["ws"]["double_posts"] == 0 and rep["summary"]["ws"]["clicked_while_covered"] == 0
    assert drv.current_window_handle == before_handle and drv.current_url == before_url      # tab cũ nguyên vẹn
    assert tam.TARGET_UPLOAD_URL == target_before and "127.0.0.1" not in str(tam.CFG.upload_url_path or "")
    assert (tmp_path / next(tmp_path.glob("tam_bench_click_*.json")).name.replace(".json", ".md")).exists()


def test_snapshot_is_only_requested_after_chrome_acked_the_release(lab, monkeypatch):
    """Hồi quy ĐĂNG TRÙNG (lab, bản đầu): snapshot gửi chung lô với nhấn/thả có thể chạy TRƯỚC khi sự kiện chuột tới DOM → tưởng
    'chưa trúng' → bấm lại. Snapshot phải được gửi SAU khi đã nhận xác nhận của lệnh thả chuột."""
    tam = lab["tam"]
    tam.CFG.click_path = "ws"
    seq = []
    real_send, real_recv = tam.CdpWs.send, tam.CdpWs.recv_until

    def send(this, method, params=None):
        i = real_send(this, method, params)
        seq.append(("send", method, (params or {}).get("type") or (params or {}).get("expression", "")[:40], i))
        return i

    def recv(this, ids, deadline, kill=None, tick=0.05, arrive=None):
        got = real_recv(this, ids, deadline, kill, tick, arrive)
        seq.append(("recv", tuple(sorted((got or {}).keys()))))
        return got
    monkeypatch.setattr(tam.CdpWs, "send", send)
    monkeypatch.setattr(tam.CdpWs, "recv_until", recv)
    res, marks, posts, fl, _ = run(lab, pid="FS")
    assert res.status.value == "CONFIRMED" and posts == 1
    rel = next(x for x in seq if x[0] == "send" and x[2] == "mouseReleased")
    snap = next(k for k, x in enumerate(seq) if x[0] == "send" and "snapshot" in str(x[2]))
    acked = next(k for k, x in enumerate(seq) if x[0] == "recv" and rel[3] in x[1])
    assert acked < snap                                              # đã nhận xác nhận THẢ chuột rồi mới hỏi trang


def test_no_trace_after_release_is_treated_as_clicked_never_reclicked(lab, monkeypatch):
    """Trang không ghi nhận sự kiện nào sau khi thả (giả lập): KHÔNG được bấm lại (tránh đăng trùng) → UNKNOWN/CONFIRMED, 1 lần nhấn."""
    tam = lab["tam"]
    tam.CFG.click_path = "ws"
    spy = Spy(tam, monkeypatch)
    real = tam._ws_value

    def blank(m):
        v = real(m)
        if isinstance(v, dict) and "ev" in v and "pending" in v:            # snapshot sau nhấn → xoá dấu vết
            v = dict(v, ev=[], blocked=[], verdict=None)
        return v
    monkeypatch.setattr(tam, "_ws_value", blank)
    calls = []
    real_click = tam.cdp_click
    monkeypatch.setattr(tam, "cdp_click", lambda *a, **k: calls.append(1) or real_click(*a, **k))
    res, marks, posts, fl, logs = run(lab, pid="FN")
    assert calls == [] and spy.mouse().count("Input.dispatchMouseEvent") == 3 and posts == 1
    assert res.clicked and any("coi như ĐÃ bấm" in x for x in logs)
