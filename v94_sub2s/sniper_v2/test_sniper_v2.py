# -*- coding: utf-8 -*-
import os, sys, threading, time
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(__file__))
import quetphantichid_V2 as s
from openpyxl import Workbook, load_workbook

def _act(typ="upload", vid="AAAAAAAAAAA", pub="2026-11-11T12:00:18Z"):
    det = {"upload": {"videoId": vid}} if typ == "upload" else {"playlistItem": {"resourceId": {"videoId": vid}}}
    return {"items": [{"snippet": {"type": typ, "title": "タイトル", "publishedAt": pub, "channelId": "UCx"}, "contentDetails": det}]}

def test_parse_and_judge():
    d, why = s.parse_activity(_act())
    assert why == "ok" and d["id"] == "AAAAAAAAAAA" and d["channel_id"] == "UCx"
    pub = d["published"].timestamp()
    ok, w, age = s.judge_new(d, set(), pub + 4.8, 600)
    assert ok and abs(age - 4.8) < 1e-6
    assert s.judge_new(d, {"AAAAAAAAAAA"}, pub + 4.8, 600)[0] is False
    ok, w, age = s.judge_new(d, set(), pub + 3600, 600)
    assert not ok and w == "old"
    assert s.judge_new(d, set(), None, 600)[0] is True       # không đo được tuổi → vẫn bắt như bản cũ
    assert s.parse_activity({"items": []}) == (None, "empty")
    assert s.parse_activity({"items": [{"snippet": {"type": "like"}, "contentDetails": {}}]})[1] == "type:like"
    assert s.parse_activity(_act("playlistItem", "BBBBBBBBBBB"))[0]["id"] == "BBBBBBBBBBB"

def test_server_date():
    t = s.server_now_from_date_header("Wed, 11 Nov 2026 12:00:22 GMT", 0.0, 0.2)
    assert abs(t - (datetime(2026, 11, 11, 12, 0, 22, tzinfo=timezone.utc).timestamp() + 0.6)) < 1e-6
    assert s.server_now_from_date_header("rác", 0, 0) is None and s.server_now_from_date_header(None, 0, 0) is None

def test_excel_atomic_dedupe_and_trigger(tmp_path):
    p = tmp_path / "check.xlsx"
    wb = Workbook(); ws = wb.active; ws.append(["LINK VIDEO GỐC", "CHANNEL ID", "", "URL", "STATUS", "", "", "TITLE"]); wb.save(p)
    u = "https://www.youtube.com/watch?v=H6p3292j9iw"
    r = s.save_excel_atomic(str(p), [("2026-11-11 21:00:22", "UCx", u, "t\x01itle")])
    assert r == [(u, 2)]
    assert s.save_excel_atomic(str(p), [("x", "UCx", "https://www.youtube.com/shorts/H6p3292j9iw", "t")]) == [("https://www.youtube.com/shorts/H6p3292j9iw", "dup")]
    ws = load_workbook(p).active
    assert ws.max_row == 2 and ws.cell(2, 4).value == u and ws.cell(2, 2).value == "UCx" and ws.cell(2, 8).value == "title"
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".tmp.xlsx")]
    tp = s.append_trigger(str(p), "UCx"); s.append_trigger(str(p), "UCy")
    assert tp == str(tmp_path / "trigger_wakeup.txt") and open(tp).read() == "UCx\nUCy\n"

def test_excel_concurrent(tmp_path):
    p = tmp_path / "check.xlsx"
    wb = Workbook(); wb.active.append(["A", "B", "", "URL"]); wb.save(p)
    lock = threading.Lock()
    def w(i):
        with lock:
            s.save_excel_atomic(str(p), [("t", "UC", f"https://www.youtube.com/watch?v=Vid{i:08d}", "x")])
    th = [threading.Thread(target=w, args=(i,)) for i in range(8)]
    [t.start() for t in th]; [t.join() for t in th]
    assert load_workbook(p).active.max_row == 9

def test_excel_permission_retry(tmp_path, monkeypatch):
    p = tmp_path / "check.xlsx"
    wb = Workbook(); wb.active.append(["A", "B", "", "URL"]); wb.save(p)
    real = os.replace; n = {"c": 0}
    def flaky(a, b):
        n["c"] += 1
        if n["c"] < 3: raise PermissionError("locked")
        return real(a, b)
    monkeypatch.setattr(s.os, "replace", flaky)
    assert s.save_excel_atomic(str(p), [("t", "UC", "https://www.youtube.com/watch?v=CCCCCCCCCCC", "x")], retry_sleep=0.001)[0][1] == 2
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".tmp.xlsx")]

def test_quota_estimate():
    used, cap = s.quota_estimate(1, 0.2, 0.3, 5)
    assert used == 172800 and cap == 50000
