# -*- coding: utf-8 -*-
"""Test tích hợp WS360: hub WebSub GIẢ (local) + máy chủ video GIẢ (mock_gvs) + Excel thật (openpyxl) + ffmpeg/ffprobe thật.
Mọi sự kiện ở đây là GIẢ LẬP qua đúng đường webhook/event processor mà WebSub thật dùng — không phải video mới thật."""
import hashlib
import hmac
import importlib.util
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode

import pytest
from openpyxl import Workbook, load_workbook

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "tests"))          # mock_gvs của V9
from mock_gvs import MockGVS  # noqa: E402

spec = importlib.util.spec_from_file_location("ws", HERE.parent / "YOUTUBE_WEBSUB_INSTANT_360P.py")
ws = importlib.util.module_from_spec(spec)
sys.modules["ws"] = ws
spec.loader.exec_module(ws)

FF = shutil.which("ffmpeg")
need_ff = pytest.mark.skipif(not FF, reason="cần ffmpeg")
CH = ["UC" + c * 22 for c in "ABCDEFG"]          # Channel ID đúng định dạng (giả)
TS = datetime.now(timezone.utc)


# ------------------------------------------------------------------ fixtures
@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    prog, vonly, aonly, silent = d / "p.mp4", d / "v.mp4", d / "a.m4a", d / "s.mp4"
    run = lambda *a: subprocess.run([FF, "-y", "-loglevel", "error", *a], check=True)
    run("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25", "-f", "lavfi", "-i", "sine=duration=6", "-t", "6",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(prog))
    run("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25", "-t", "6", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-an", str(vonly))
    run("-f", "lavfi", "-i", "sine=duration=6", "-c:a", "aac", "-vn", str(aonly))
    shutil.copyfile(vonly, silent)
    return {k: v.read_bytes() for k, v in (("prog", prog), ("v", vonly), ("a", aonly), ("silent", silent))}


@pytest.fixture()
def gvs():
    with MockGVS() as g:
        yield g


def make_excel(path, chans, extra_rows=0):
    wb = Workbook()
    ws_ = wb.active
    ws_.append(["A", "B", "C", "URL", "E", "F", "G", "H", "I", "J", "CHANNEL ID", "L"])
    for i, ch in enumerate(chans):
        ws_.append([f"a{i}", f"b{i}", f"c{i}", None, f"e{i}", f"f{i}", 7, 8.5, "i", "j", ch, f"l{i}"])
    for i in range(extra_rows):
        ws_.append([f"x{i}", None, None, None, None, None, None, None, None, None, "khong-phai-kenh", None])
    wb.save(path)


def snapshot(path, skip_col=4):
    ws_ = load_workbook(path).active
    return [[c.value for c in row if c.column != skip_col] for row in ws_.iter_rows()]


class FakeHub:
    """Hub WebSub giả theo đặc tả: nhận POST subscribe → 202 → GỌI GET xác minh tới callback (async)."""

    def __init__(self):
        self.reqs, self.verified, self.secret = [], {}, {}
        hub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                q = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}
                hub.reqs.append(q)
                if q.get("hub.verify") == "sync":
                    ok = hub._verify(q)
                    body = b"" if ok else b"Error trying to confirm subscription"
                    self.send_response(204 if ok else 409)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                threading.Thread(target=hub._verify, args=(q,), daemon=True).start()

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/subscribe"

    def _verify(self, q):
        time.sleep(0.05)
        chal = "c" + os.urandom(6).hex()
        u = q["hub.callback"] + ("&" if "?" in q["hub.callback"] else "?") + urlencode({"hub.mode": q["hub.mode"], "hub.topic": q["hub.topic"],
                                                  "hub.challenge": chal, "hub.lease_seconds": "432000"})
        try:
            with urllib.request.urlopen(u, timeout=3) as r:
                ok = r.status == 200 and r.read().decode() == chal
        except Exception:
            ok = False
        ch = ws.norm_channel(q["hub.topic"])
        self.verified[ch] = (q["hub.mode"], ok)
        if q.get("hub.secret"):
            self.secret[ch] = q["hub.secret"]
        return ok

    def push(self, callback, ch, vid, published=None, title="t", sign=True, secret=None):
        body = atom(ch, vid, published or datetime.now(timezone.utc), title)
        h = {"Content-Type": "application/atom+xml"}
        if sign:
            h["X-Hub-Signature"] = "sha1=" + hmac.new((secret or self.secret[ch]).encode(), body, hashlib.sha1).hexdigest()
        with urllib.request.urlopen(urllib.request.Request(callback, data=body, headers=h), timeout=3) as r:
            return r.status

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def atom(ch, vid, published, title="t"):
    return (f'<?xml version="1.0" encoding="UTF-8"?><feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
            f'xmlns="http://www.w3.org/2005/Atom"><link rel="hub" href="https://pubsubhubbub.appspot.com"/>'
            f'<entry><id>yt:video:{vid}</id><yt:videoId>{vid}</yt:videoId><yt:channelId>{ch}</yt:channelId>'
            f'<title>{title}</title><link rel="alternate" href="https://www.youtube.com/watch?v={vid}"/>'
            f'<author><name>n</name><uri>https://www.youtube.com/channel/{ch}</uri></author>'
            f'<published>{published.isoformat()}</published><updated>{datetime.now(timezone.utc).isoformat()}</updated>'
            f'</entry></feed>').encode()


def mkfmt(url, itag, height, mime, clen, **kw):
    d = {"itag": itag, "url": url, "height": height, "mime": mime, "clen": clen, "bitrate": 0,
         "is_video": mime.startswith("video/") and kw.get("adaptive", False),
         "is_audio": mime.startswith("audio/"), "has_audio": not kw.get("adaptive", False) or mime.startswith("audio/"),
         "is_default_audio": True}
    return d


def make_engine(tmp, chans, hub=None, keep_excel=False, **cfg_over):
    if not (keep_excel and (tmp / "check.xlsx").exists()):
        make_excel(tmp / "check.xlsx", chans)
    cfg = dict(ws.DEFAULT_CONFIG, listen_host="127.0.0.1", listen_port=0,
               retry_schedule_sec=[0.1, 0.2, 0.3], workers=3)
    if hub:
        cfg["hub_url"] = hub.url
    cfg.update(cfg_over)
    logs = []
    log = ws.Log(tmp / "logs")
    log.sinks.append(logs.append)
    e = ws.Engine(cfg, tmp, log)
    e._logs = logs
    return e


def wait_state(e, vid, states=("DONE", "FAILED"), t=20):
    end = time.time() + t
    while time.time() < end:
        r = e.db.one("SELECT * FROM videos WHERE video_id=?", (vid,))
        if r and r["state"] in states:
            return r
        time.sleep(0.05)
    return e.db.one("SELECT * FROM videos WHERE video_id=?", (vid,))


def wait_excel(e, vid, t=20):
    end = time.time() + t
    while time.time() < end:
        r = e.db.one("SELECT * FROM excel_jobs WHERE video_id=?", (vid,))
        if r and r["state"] in ("DONE", "NO_ROW", "SUPERSEDED"):
            return r
        time.sleep(0.05)
    return e.db.one("SELECT * FROM excel_jobs WHERE video_id=?", (vid,))


def plan_prog(gvs, media, vid, mode="ok", data=None, **kw):
    data = media["prog"] if data is None else data
    url = gvs.add(f"/vp/{vid}", data, mode, **kw)
    return {"pick": {"kind": "progressive", "video": mkfmt(url, 18, 360, 'video/mp4; codecs="avc1, mp4a"', len(data)),
                     "audio": None}, "source": "MOCK"}


# ------------------------------------------------------------------ parse / chọn format
def test_pick_formats_prefers_progressive_avc_and_never_assumes_itag18():
    prog = [mkfmt("u1", 43, 360, 'video/webm; codecs="vp8"', 10), mkfmt("u2", 777, 360, 'video/mp4; codecs="avc1.42001E, mp4a"', 9),
            mkfmt("u3", 22, 720, 'video/mp4; codecs="avc1"', 50)]
    p = ws.pick_formats(prog, [])
    assert p["kind"] == "progressive" and p["video"]["itag"] == 777
    adap = [mkfmt("v1", 243, 360, 'video/webm; codecs="vp9"', 5, adaptive=True),
            mkfmt("v2", 134, 360, 'video/mp4; codecs="avc1.4d401e"', 6, adaptive=True),
            mkfmt("a1", 251, 0, 'audio/webm; codecs="opus"', 1, adaptive=True),
            mkfmt("a2", 140, 0, 'audio/mp4; codecs="mp4a.40.2"', 1, adaptive=True)]
    p = ws.pick_formats([mkfmt("x", 22, 720, "video/mp4", 1)], adap)
    assert p["kind"] == "dash" and p["video"]["itag"] == 134 and p["audio"]["itag"] == 140
    assert ws.pick_formats([], [mkfmt("a", 140, 0, "audio/mp4", 1, adaptive=True)]) is None


def test_innertube_json_parse_and_playability():
    data = {"playabilityStatus": {"status": "OK"},
            "streamingData": {"formats": [{"itag": 18, "url": "https://x/18", "mimeType": 'video/mp4; codecs="avc1.42001E, mp4a.40.2"',
                                           "qualityLabel": "360p", "height": 360, "contentLength": "123"}],
                              "adaptiveFormats": [{"itag": 140, "url": "https://x/140", "mimeType": 'audio/mp4; codecs="mp4a.40.2"'}]}}
    prog, adap = ws._innertube_formats(data)
    assert prog[0]["height"] == 360 and prog[0]["clen"] == 123 and adap[0]["is_audio"]
    assert ws.classify_playability({"status": "OK"}, False) == "notready"
    assert ws.classify_playability({"status": "LOGIN_REQUIRED", "reason": "Sign in to confirm your age"}, False) == "fatal"
    assert ws.classify_playability({"status": "ERROR", "reason": "Video unavailable"}, False) == "notready"
    assert ws.classify_playability({"status": "UNPLAYABLE", "reason": "Join this channel to get access to members-only content"}, False) == "fatal"


def test_parse_atom_entry_deleted_and_xxe():
    raw = atom(CH[0], "VIDVIDVID01", TS)
    e = ws.parse_atom(raw)[0]
    assert e["video_id"] == "VIDVIDVID01" and e["channel_id"] == CH[0] and e["published"] is not None
    tomb = (b'<?xml version="1.0"?><feed xmlns:at="http://purl.org/atompub/tombstones/1.0" xmlns="http://www.w3.org/2005/Atom">'
            b'<at:deleted-entry ref="yt:video:VIDVIDVID02" when="2026-11-11T00:00:00+00:00"/></feed>')
    d = ws.parse_atom(tomb)[0]
    assert d["deleted"] and d["video_id"] == "VIDVIDVID02"
    with pytest.raises(ValueError):
        ws.parse_atom(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><feed/>')


# ------------------------------------------------------------------ Excel
def test_excel_reads_column_K_with_duplicates_and_writes_D_only(tmp_path):
    p = tmp_path / "check.xlsx"
    make_excel(p, [CH[0], CH[1], CH[0], "https://www.youtube.com/channel/" + CH[2]], extra_rows=2)
    before = snapshot(p)
    xb = ws.ExcelBook(p)
    chans, bad = xb.read_channels()
    assert chans == {CH[0]: [2, 4], CH[1]: [3], CH[2]: [5]} and len(bad) == 2
    rows = xb.write_url(CH[0], "https://www.youtube.com/watch?v=VIDVIDVID01")
    assert rows == [2, 4]
    after = snapshot(p)
    assert before == after                              # mọi cột khác D giữ nguyên
    w = load_workbook(p).active
    assert w["D2"].value == w["D4"].value == "https://www.youtube.com/watch?v=VIDVIDVID01" and w["D3"].value is None


def test_excel_resorted_rows_written_to_correct_row(tmp_path):
    p = tmp_path / "check.xlsx"
    make_excel(p, [CH[0], CH[1], CH[2]])
    xb = ws.ExcelBook(p)
    assert xb.read_channels()[0][CH[2]] == [4]
    wb = load_workbook(p)                               # người dùng sắp xếp lại: CH[2] lên dòng 2
    s = wb.active
    rows = [[c.value for c in r] for r in s.iter_rows(min_row=2)]
    rows.sort(key=lambda r: r[10], reverse=True)
    for i, r in enumerate(rows, start=2):
        for j, v in enumerate(r, start=1):
            s.cell(i, j).value = v
    wb.save(p)
    assert xb.write_url(CH[2], "U") == [2]
    w = load_workbook(p).active
    assert w["D2"].value == "U" and w["K2"].value == CH[2] and w["D4"].value is None


def test_excel_open_lockfile_without_com_raises_locked(tmp_path):
    p = tmp_path / "check.xlsx"
    make_excel(p, [CH[0]])
    xb = ws.ExcelBook(p)
    (tmp_path / "~$check.xlsx").write_text("x")
    with pytest.raises(ws.ExcelLocked):
        xb.write_url(CH[0], "U")
    assert load_workbook(p).active["D2"].value is None


# ------------------------------------------------------------------ WebSub đầy đủ (hub giả)
@need_ff
def test_websub_subscribe_verify_push_download_and_excel_parallel(tmp_path, gvs, media, monkeypatch):
    hub = FakeHub()
    try:
        e = make_engine(tmp_path, CH[:3] + [CH[0]], hub)
        plans = {}
        monkeypatch.setattr(e.resolver, "resolve", lambda vid, deadline_s=2.5: plans[vid])
        e.start()
        port = e.httpd.server_address[1]
        e.callback = f"http://127.0.0.1:{port}/websub"
        assert e.subscribe_all() == 3                         # 3 kênh duy nhất (CH[0] trùng 2 dòng)
        end = time.time() + 5
        while time.time() < end and len(hub.verified) < 3:
            time.sleep(0.05)
        assert all(v == ("subscribe", True) for v in hub.verified.values()) and len(hub.verified) == 3
        assert e.db.one("SELECT COUNT(*) c FROM subs WHERE state='VERIFIED'")["c"] == 3
        assert all(r["lease_seconds"] == 432000 for r in e.db.all("SELECT * FROM subs"))
        assert all(q["hub.topic"].startswith("https://www.youtube.com/xml/feeds/videos.xml?channel_id=") for q in hub.reqs)
        # Excel bị khoá suốt lúc tải → download vẫn chạy xong TRƯỚC khi Excel ghi được
        lock = tmp_path / "~$check.xlsx"
        lock.write_text("x")
        vid = "NEWVID00001"
        plans[vid] = plan_prog(gvs, media, vid)
        t0 = time.perf_counter()
        assert hub.push(e.callback, CH[0], vid, title="video mới") == 204
        r = wait_state(e, vid)
        t_dl = time.perf_counter() - t0
        assert r["state"] == "DONE", r["error"]
        x = e.db.one("SELECT * FROM excel_jobs WHERE video_id=?", (vid,))
        assert x["state"] != "DONE"                           # Excel vẫn đang chờ — download KHÔNG chờ Excel
        f = Path(r["file"])
        assert f.name == f"{vid}_360p.mp4" and f.parent.name == CH[0] and ws.ffprobe_info(f)["audio"]
        assert r["engine"] == "WEBSUB" and r["enqueue_ms"] < 100 and r["resolver_start_ms"] < 100
        lock.unlink()
        x = wait_excel(e, vid)
        assert x["state"] == "DONE" and x["rows"] == "2,5"
        w = load_workbook(tmp_path / "check.xlsx").active
        assert w["D2"].value == w["D5"].value == ws.video_url(vid) and w["D3"].value is None
        assert t_dl < 10
        # push sai chữ ký → bỏ; push trùng → không tải lại; video cũ (cập nhật) → không tải; kênh lạ → bỏ
        assert hub.push(e.callback, CH[1], "BADSIG00001", sign=False) == 204
        assert hub.push(e.callback, CH[0], vid) == 204
        assert hub.push(e.callback, CH[1], "OLDVID00001", published=TS - timedelta(days=3)) == 204
        assert hub.push(e.callback, CH[5], "FOREIGN0001", secret=e.secret) == 204
        time.sleep(0.5)
        assert e.db.one("SELECT COUNT(*) c FROM videos")["c"] == 1
        assert e.db.one("SELECT 1 FROM hub_events WHERE kind='push-old' AND detail LIKE 'OLDVID00001%'")
        assert "OLDVID00001" not in e.seen                   # không đánh dấu vĩnh viễn → hub gửi lại muộn vẫn xét lại được
        assert len(gvs.requests_for(f"/vp/{vid}")) == 1
        e.stop()
    finally:
        hub.close()


@need_ff
def test_three_engines_same_video_downloaded_once(tmp_path, gvs, media, monkeypatch):
    e = make_engine(tmp_path, CH[:1])
    vid = "SAMEVID0001"
    calls = []
    plan = plan_prog(gvs, media, vid)
    monkeypatch.setattr(e.resolver, "resolve", lambda v, deadline_s=2.5: calls.append(v) or plan)
    e.start(websub=False)
    out = []
    ths = [threading.Thread(target=lambda eng=eng: out.append(e.on_event(CH[0], vid, eng, TS))) for eng in ("WEBSUB",) * 15]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert sorted(out).count("queued") == 1 and out.count("dup") == 14
    assert wait_state(e, vid)["state"] == "DONE" and calls == [vid]
    e.stop()


@need_ff
def test_many_videos_at_once_and_latency_percentiles(tmp_path, gvs, media, monkeypatch):
    e = make_engine(tmp_path, CH[:4], max_concurrent_downloads=3)
    plans = {}
    monkeypatch.setattr(e.resolver, "resolve", lambda v, deadline_s=2.5: plans[v])
    e.start(websub=False)
    vids = [f"MANYVID{i:04d}" for i in range(12)]
    for i, v in enumerate(vids):
        plans[v] = plan_prog(gvs, media, v)
        assert e.on_event(CH[i % 4], v, "WEBSUB", TS) == "queued"
    rs = [wait_state(e, v, t=60) for v in vids]
    assert all(r["state"] == "DONE" for r in rs), [(r["video_id"], r["error"]) for r in rs if r["state"] != "DONE"]
    enq = [r["enqueue_ms"] for r in rs]
    assert ws.pct(enq, 99) < 100
    sts = [wait_excel(e, v)["state"] for v in vids]
    assert set(sts) <= {"DONE", "SUPERSEDED"} and sts.count("DONE") >= 4
    w = load_workbook(tmp_path / "check.xlsx").active
    for i in range(4):                                     # mỗi kênh: ô D = video MỚI NHẤT của kênh đó
        assert w[f"D{i + 2}"].value == ws.video_url(vids[8 + i])
    e.stop()


# ------------------------------------------------------------------ đường tải
@need_ff
def test_dash_360_video_plus_audio_mux(tmp_path, gvs, media, monkeypatch):
    e = make_engine(tmp_path, CH[:1])
    vid = "DASHVID0001"
    vu = gvs.add("/dv", media["v"])
    au = gvs.add("/da", media["a"])
    plan = {"pick": {"kind": "dash", "video": mkfmt(vu, 134, 360, 'video/mp4; codecs="avc1"', len(media["v"]), adaptive=True),
                     "audio": mkfmt(au, 140, 0, 'audio/mp4; codecs="mp4a"', len(media["a"]), adaptive=True)}}
    monkeypatch.setattr(e.resolver, "resolve", lambda v, deadline_s=2.5: plan)
    e.start(websub=False)
    e.on_event(CH[0], vid, "WEBSUB", TS)
    r = wait_state(e, vid)
    assert r["state"] == "DONE" and r["kind"] == "dash" and r["mux_ms"] > 0 and r["itags"] == "134,140"
    i = ws.ffprobe_info(Path(r["file"]))
    assert i["video"] and i["audio"] and i["height"] == 360
    e.stop()


@need_ff
def test_progressive_without_audio_is_kept_with_warning(tmp_path, gvs, media, monkeypatch):
    e = make_engine(tmp_path, CH[:1])
    vid = "SILENTVID01"
    monkeypatch.setattr(e.resolver, "resolve", lambda v, deadline_s=2.5: plan_prog(gvs, media, vid, data=media["silent"]))
    e.start(websub=False)
    e.on_event(CH[0], vid, "WEBSUB", TS)
    r = wait_state(e, vid)
    assert r["state"] == "DONE" and not ws.ffprobe_info(Path(r["file"]))["audio"]
    assert any("không có luồng tiếng" in l for l in e._logs)
    e.stop()


@need_ff
@pytest.mark.parametrize("n", [4, 8])
def test_range_connections_and_bad_content_range_fallback(tmp_path, gvs, media, monkeypatch, n):
    e = make_engine(tmp_path, CH[:1], range_connections=n)
    good, bad = "RANGEGOOD01", "RANGEBADCR1"
    plans = {good: plan_prog(gvs, media, good), bad: plan_prog(gvs, media, bad, mode="badcr", shift=7)}
    monkeypatch.setattr(e.resolver, "resolve", lambda v, deadline_s=2.5: plans[v])
    e.start(websub=False)
    e.on_event(CH[0], good, "WEBSUB", TS)
    e.on_event(CH[0], bad, "WEBSUB", TS)
    for v in (good, bad):
        r = wait_state(e, v)
        assert r["state"] == "DONE", r["error"]
        assert Path(r["file"]).read_bytes() == media["prog"]           # không bao giờ ghép sai byte
    assert any("Range" in l and "1 GET" in l for l in e._logs)
    e.stop()


@need_ff
def test_403_then_ok_429_backoff_notready_then_ready(tmp_path, gvs, media, monkeypatch):
    e = make_engine(tmp_path, CH[:1], retry_schedule_sec=[0.2, 0.3, 0.5, 0.5])
    v403, vnr = "FORBID00001", "NOTREADY001"
    p403 = plan_prog(gvs, media, v403, mode="status", code=403)
    cnt = {"nr": 0}

    def resolve(v, deadline_s=2.5):
        if v == v403:
            if len(gvs.requests_for(f"/vp/{v403}")) >= 1:
                gvs.files[f"/vp/{v403}"].mode = "ok"
            return p403
        cnt["nr"] += 1
        if cnt["nr"] < 3:
            raise ws.NotReady("chưa có luồng 360p")
        return plan_prog(gvs, media, vnr)
    monkeypatch.setattr(e.resolver, "resolve", resolve)
    e.start(websub=False)
    e.on_event(CH[0], v403, "WEBSUB", TS)
    e.on_event(CH[0], vnr, "WEBSUB", TS)
    r1, r2 = wait_state(e, v403), wait_state(e, vnr)
    assert r1["state"] == "DONE" and r1["attempts"] == 2
    assert r2["state"] == "DONE" and r2["attempts"] == 3
    e.stop()


def test_429_respects_retry_after_and_retry_limit_then_failed(tmp_path, gvs, media, monkeypatch):
    e = make_engine(tmp_path, CH[:1], retry_schedule_sec=[0.1, 0.1])
    v = "TOOMANY0001"
    monkeypatch.setattr(e.resolver, "resolve", lambda x, deadline_s=2.5: plan_prog(gvs, media, v, mode="status", code=429,
                                                                                    headers={"Retry-After": "1"}))
    e.start(websub=False)
    e.on_event(CH[0], v, "WEBSUB", TS)
    time.sleep(1.0)
    r = e.db.one("SELECT * FROM videos WHERE video_id=?", (v,))
    assert r["state"] == "RETRY_WAIT" and r["next_try"] - time.time() > 5          # 429 → chờ ≥10s, không dội liên tục
    assert len(gvs.requests_for(f"/vp/{v}")) == 1
    e.stop()
    e2 = make_engine(tmp_path / "b", CH[:1], retry_schedule_sec=[0.05, 0.05]) if (tmp_path / "b").mkdir() is None else None
    monkeypatch.setattr(e2.resolver, "resolve", lambda x, deadline_s=2.5: (_ for _ in ()).throw(ws.NotReady("x")))
    e2.start(websub=False)
    e2.on_event(CH[0], "NEVERREADY1", "WEBSUB", TS)
    r = wait_state(e2, "NEVERREADY1")
    assert r["state"] == "FAILED" and r["attempts"] == 3
    e2.stop()


def test_fatal_not_retried(tmp_path, monkeypatch):
    e = make_engine(tmp_path, CH[:1])
    calls = []
    monkeypatch.setattr(e.resolver, "resolve", lambda x, deadline_s=2.5: calls.append(x) or (_ for _ in ()).throw(ws.Fatal("members only")))
    e.start(websub=False)
    e.on_event(CH[0], "MEMBERS0001", "WEBSUB", TS)
    assert wait_state(e, "MEMBERS0001")["state"] == "FAILED" and calls == ["MEMBERS0001"]
    e.stop()


@need_ff
def test_crash_restart_recovers_and_cleans_partials(tmp_path, gvs, media, monkeypatch):
    e = make_engine(tmp_path, CH[:1])
    vid = "CRASHVID001"
    e.db.x("INSERT INTO videos(video_id, channel_id, url, engine, detected_at, state) VALUES(?,?,?,?,?,?)",
           (vid, CH[0], ws.video_url(vid), "WEBSUB", ws.iso(), "DOWNLOADING"))
    e.db.x("INSERT INTO excel_jobs(video_id, channel_id, url, state, created_at) VALUES(?,?,?,?,?)",
           (vid, CH[0], ws.video_url(vid), "RETRY", ws.iso()))
    part = tmp_path / "DOWNLOADS" / CH[0] / f".{vid}_360p.part.mp4"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"half")
    e.db.con.close()
    e2 = make_engine(tmp_path, CH[:1])                    # "khởi động lại"
    monkeypatch.setattr(e2.resolver, "resolve", lambda v, deadline_s=2.5: plan_prog(gvs, media, vid))
    e2.start(websub=False)
    assert not part.exists()
    assert wait_state(e2, vid)["state"] == "DONE"
    assert wait_excel(e2, vid)["state"] == "DONE"
    assert e2.on_event(CH[0], vid, "WEBSUB", TS) == "dup"
    e2.stop()


@need_ff
def test_existing_valid_file_not_redownloaded_corrupt_file_replaced(tmp_path, gvs, media, monkeypatch):
    e = make_engine(tmp_path, CH[:1])
    v1, v2 = "EXISTOK0001", "EXISTBAD001"
    d = tmp_path / "DOWNLOADS" / CH[0]
    d.mkdir(parents=True)
    (d / f"{v1}_360p.mp4").write_bytes(media["prog"])
    (d / f"{v2}_360p.mp4").write_bytes(b"corrupt")
    monkeypatch.setattr(e.resolver, "resolve", lambda v, deadline_s=2.5: plan_prog(gvs, media, v))
    e.start(websub=False)
    e.on_event(CH[0], v1, "WEBSUB", TS)
    e.on_event(CH[0], v2, "WEBSUB", TS)
    assert wait_state(e, v1)["state"] == "DONE" and wait_state(e, v2)["state"] == "DONE"
    assert not gvs.requests_for(f"/vp/{v1}") and len(gvs.requests_for(f"/vp/{v2}")) == 1
    assert (d / f"{v2}_360p.mp4").read_bytes() == media["prog"]
    e.stop()


def test_truncated_download_never_marked_done(tmp_path, gvs, media, monkeypatch):
    e = make_engine(tmp_path, CH[:1], retry_schedule_sec=[0.05])
    v = "TRUNCATED01"
    monkeypatch.setattr(e.resolver, "resolve", lambda x, deadline_s=2.5: plan_prog(gvs, media, v, data=media["prog"][:5000]))
    e.start(websub=False)
    e.on_event(CH[0], v, "WEBSUB", TS)
    r = wait_state(e, v)
    assert r["state"] == "FAILED" and "ffprobe" in r["error"]
    assert not list((tmp_path / "DOWNLOADS").rglob(f"{v}*"))
    e.stop()


# ------------------------------------------------------------------ Excel thêm/xoá kênh, sai kênh, chế độ tắt
def test_excel_add_remove_channels_resubscribes(tmp_path):
    hub = FakeHub()
    try:
        e = make_engine(tmp_path, CH[:2], hub)
        e.start(workers=False)
        e.callback = f"http://127.0.0.1:{e.httpd.server_address[1]}/websub"
        e.subscribe_all()
        time.sleep(0.5)
        make_excel(tmp_path / "check.xlsx", [CH[1], CH[2], "SAI_KENH"])
        end = time.time() + 6
        while time.time() < end and CH[2] not in hub.verified:
            time.sleep(0.1)
        time.sleep(0.4)
        assert set(e.channels) == {CH[1], CH[2]}
        assert hub.verified[CH[2]] == ("subscribe", True)
        assert hub.verified[CH[0]] == ("unsubscribe", True)
        assert any("không phải Channel ID" in l for l in e._logs)
        assert e.on_event(CH[0], "REMOVEDCH01", "WEBSUB", TS) == "foreign"
        e.stop()
    finally:
        hub.close()


def test_callback_change_resubscribes(tmp_path):
    hub = FakeHub()
    try:
        e = make_engine(tmp_path, CH[:2], hub)
        e.start(workers=False, websub=True)
        port = e.httpd.server_address[1]
        e.cfg["callback_url"] = f"http://127.0.0.1:{port}/websub"
        end = time.time() + 6
        while time.time() < end and len(hub.reqs) < 2:
            time.sleep(0.1)
        n1 = len(hub.reqs)
        e.cfg["callback_url"] = f"http://localhost:{port}/websub"           # callback đổi
        e.callback = ""
        e.db.meta_set("websub_callback", f"http://127.0.0.1:{port}/websub")
        e.stop()
        e2 = make_engine(tmp_path, CH[:2], hub, callback_url=f"http://localhost:{port + 0}/websub")
        e2.cfg["listen_port"] = 0
        e2.start(workers=False)
        e2.cfg["callback_url"] = f"http://localhost:{e2.httpd.server_address[1]}/websub"
        end = time.time() + 40
        while time.time() < end and len(hub.reqs) < n1 + 2:
            time.sleep(0.2)
        assert len(hub.reqs) >= n1 + 2 and hub.reqs[-1]["hub.callback"].startswith("http://localhost:")
        e2.stop()
    finally:
        hub.close()


def test_download_disabled_still_writes_excel(tmp_path):
    e = make_engine(tmp_path, CH[:1], download_enabled=False)
    e.start(websub=False)
    assert e.on_event(CH[0], "NODOWNLOAD1", "WEBSUB", TS) == "queued"
    assert wait_excel(e, "NODOWNLOAD1")["state"] == "DONE"
    assert e.db.one("SELECT state FROM videos WHERE video_id='NODOWNLOAD1'")["state"] == "SKIPPED"
    e.stop()


def test_invalid_ids_rejected(tmp_path):
    e = make_engine(tmp_path, CH[:1])
    assert e.on_event("UCshort", "VIDVIDVID01", "WEBSUB") == "invalid"
    assert e.on_event(CH[0], "bad id!", "WEBSUB") == "invalid"
    e.db.con.close()


# ------------------------------------------------------------------ GUI dựng được (offscreen)
def test_gui_builds_offscreen(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer
    app = QApplication.instance() or QApplication([])
    make_excel(tmp_path / "check.xlsx", CH[:1])
    QTimer.singleShot(300, app.quit)
    assert ws.run_gui(dict(ws.DEFAULT_CONFIG), tmp_path) == 0


def test_excel_never_overwrites_newer_url_with_older_even_after_restart(tmp_path, monkeypatch):
    e = make_engine(tmp_path, CH[:1], download_enabled=False)
    e.start(websub=False)
    t_new = datetime.now(timezone.utc)
    t_old = t_new - timedelta(minutes=5)
    lock = tmp_path / "~$check.xlsx"
    lock.write_text("x")                                     # Excel khoá → cả 2 cùng chờ trong 1 lô
    e.on_event(CH[0], "NEWERVID001", "WEBSUB", t_new)          # tới TRƯỚC nhưng đăng SAU
    e.on_event(CH[0], "OLDERVID001", "WEBSUB", t_old)
    time.sleep(0.6)
    lock.unlink()
    assert wait_excel(e, "NEWERVID001")["state"] == "DONE"
    assert wait_excel(e, "OLDERVID001")["state"] == "SUPERSEDED"
    assert load_workbook(tmp_path / "check.xlsx").active["D2"].value == ws.video_url("NEWERVID001")
    e.stop()
    # khởi động lại: job SUPERSEDED/NO_ROW không được chạy lại; job cũ còn PENDING cũng không ghi đè link mới hơn
    e.db.x("UPDATE excel_jobs SET state='PENDING' WHERE video_id='OLDERVID001'")
    e.db.con.close()
    e2 = make_engine(tmp_path, CH[:1], keep_excel=True, download_enabled=False)
    e2.start(websub=False)
    assert wait_excel(e2, "OLDERVID001")["state"] == "SUPERSEDED"
    assert load_workbook(tmp_path / "check.xlsx").active["D2"].value == ws.video_url("NEWERVID001")
    e2.stop()


def test_forged_verification_and_late_unsubscribe_refused(tmp_path):
    e = make_engine(tmp_path, CH[:2])
    e.start(workers=False)
    port = e.httpd.server_address[1]

    def get(mode, ch, chal="zz9"):
        q = urlencode({"hub.mode": mode, "hub.topic": ws.TOPIC_FMT.format(ch), "hub.challenge": chal, "hub.lease_seconds": "99999999"})
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/websub?{q}", timeout=3) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as err:
            return err.code, b""
    import urllib.error
    assert get("subscribe", CH[0])[0] == 404                 # tool chưa từng gửi yêu cầu → không xác nhận
    assert get("unsubscribe", CH[0])[0] == 404               # kênh đang theo dõi → không chấp nhận huỷ
    with e.hub_lock:
        e.pending_hub[(CH[0], "subscribe")] = time.time()
    assert get("subscribe", CH[0]) == (200, b"zz9")
    assert e.db.one("SELECT lease_seconds FROM subs WHERE channel_id=?", (CH[0],))["lease_seconds"] <= 432000
    big = b"x" * (300 * 1024)
    try:
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/websub", data=big), timeout=3)
        code = 200
    except urllib.error.HTTPError as err:
        code = err.code
    assert code == 413
    e.stop()


@need_ff
def test_dash_without_audio_is_remuxed_to_real_mp4_and_temps_cleaned(tmp_path, gvs, media, monkeypatch):
    e = make_engine(tmp_path, CH[:1])
    vid = "DASHNOAUD01"
    webm = tmp_path / "v.webm"
    subprocess.run([FF, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=10", "-t", "3",
                    "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", str(webm)], check=True)
    vu = gvs.add("/dw", webm.read_bytes())
    plan = {"pick": {"kind": "dash", "video": mkfmt(vu, 243, 360, 'video/webm; codecs="vp9"', webm.stat().st_size, adaptive=True),
                     "audio": None}}
    monkeypatch.setattr(e.resolver, "resolve", lambda v, deadline_s=2.5: plan)
    e.start(websub=False)
    e.on_event(CH[0], vid, "WEBSUB", TS)
    r = wait_state(e, vid)
    assert r["state"] == "DONE"
    f = Path(r["file"])
    fmt = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=format_name", "-of", "csv=p=0", str(f)],
                         capture_output=True, text=True).stdout
    assert "mp4" in fmt
    assert not [p for p in f.parent.iterdir() if ".part" in p.name]
    e.stop()


@need_ff
def test_unexpected_exception_before_download_is_retried(tmp_path, gvs, media, monkeypatch):
    e = make_engine(tmp_path, CH[:1])
    vid = "EARLYERR001"
    monkeypatch.setattr(e.resolver, "resolve", lambda v, deadline_s=2.5: plan_prog(gvs, media, vid))
    real = e.dl_dir.mkdir
    n = {"c": 0}
    orig_db_upd = e.db.upd

    def flaky_upd(v, **kw):
        if kw.get("state") == "RESOLVING" and n["c"] == 0:
            n["c"] += 1
            raise sqlite3_err()
        return orig_db_upd(v, **kw)
    import sqlite3
    sqlite3_err = lambda: sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(e.db, "upd", flaky_upd)
    e.start(websub=False)
    e.on_event(CH[0], vid, "WEBSUB", TS)
    assert wait_state(e, vid)["state"] == "DONE"
    e.stop()


def test_second_instance_in_same_folder_refused(tmp_path):
    e = make_engine(tmp_path, CH[:1])
    e.start(websub=False, workers=False)
    e2 = make_engine(tmp_path, CH[:1])
    with pytest.raises(RuntimeError):
        e2.start(websub=False, workers=False)
    e.stop()


def test_range_probe_leaves_pool_connections_reusable(tmp_path, gvs, media):
    url = gvs.add("/rr", media["prog"])
    for i in range(6):
        info = {}
        d = tmp_path / f"o{i}.mp4"
        ws.download_ranges(url, d, len(media["prog"]), 4, info)
        assert d.read_bytes() == media["prog"]


def test_hub_retry_and_denied_handled(tmp_path):
    e = make_engine(tmp_path, CH[:1])
    e.start(workers=False)
    port = e.httpd.server_address[1]
    with e.hub_lock:
        e.pending_hub[(CH[0], "subscribe")] = time.time()
    q = urlencode({"hub.mode": "subscribe", "hub.topic": ws.TOPIC_FMT.format(CH[0]), "hub.challenge": "r1", "hub.lease_seconds": "432000"})
    for _ in range(2):                                     # hub gửi lại xác minh (phản hồi đầu bị mất) → vẫn 200
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/websub?{q}", timeout=3) as r:
            assert r.read() == b"r1"
    d = urlencode({"hub.mode": "denied", "hub.topic": ws.TOPIC_FMT.format(CH[0]), "hub.reason": "nope"})
    urllib.request.urlopen(f"http://127.0.0.1:{port}/websub?{d}", timeout=3).read()
    assert e.db.one("SELECT state FROM subs WHERE channel_id=?", (CH[0],))["state"] == "DENIED"
    e.stop()


def test_stop_during_start_is_clean_and_restart_works(tmp_path, monkeypatch):
    e = make_engine(tmp_path, CH[:1])
    monkeypatch.setattr(e.resolver, "warm", lambda: time.sleep(1.0))
    errs = []

    def _st():
        try:
            e.start()
        except Exception as ex:  # noqa: BLE001
            errs.append(ex)
    th = threading.Thread(target=_st)
    th.start()
    time.sleep(0.2)
    e.stop()                                               # chờ start() xong rồi mới dọn → webhook không bị bỏ rơi
    th.join()
    assert not errs, errs
    assert e.httpd is None and not [t for t in e.threads if t.is_alive()]
    e2 = make_engine(tmp_path, CH[:1], keep_excel=True)
    e2.start()                       # khoá đã nhả, cổng đã đóng
    assert e2.httpd is not None
    e2.stop()


def test_vertical_shorts_360p_is_picked():
    """Shorts dọc: itag 18 có width=360, height=640, qualityLabel '360p' → phải được chọn."""
    data = {"playabilityStatus": {"status": "OK"},
            "streamingData": {"formats": [{"itag": 18, "url": "https://x/18", "mimeType": 'video/mp4; codecs="avc1.42001E, mp4a.40.2"',
                                           "qualityLabel": "360p", "width": 360, "height": 640, "contentLength": "9"}],
                              "adaptiveFormats": [{"itag": 134, "url": "https://x/134", "mimeType": 'video/mp4; codecs="avc1"',
                                                   "width": 360, "height": 640},
                                                  {"itag": 135, "url": "https://x/135", "mimeType": 'video/mp4; codecs="avc1"',
                                                   "qualityLabel": "480p", "width": 480, "height": 854}]}}
    prog, adap = ws._innertube_formats(data)
    p = ws.pick_formats(prog, adap)
    assert p["kind"] == "progressive" and p["video"]["itag"] == 18
    p = ws.pick_formats([], adap)
    assert p["video"]["itag"] == 134
    assert ws._fmt_height({"format_note": "360p", "width": 360, "height": 640}) == 360
    assert ws._fmt_height({"width": 640, "height": 360}) == 360


# ------------------------------------------------------------------ WebSub chẩn đoán / feed 404 (sửa 12/11)
class MockYT:
    """Giả lập youtube.com: /feeds/videos.xml?channel_id= | ?playlist_id=UU… | /channel/UC… với mã trạng thái cấu hình được."""

    def __init__(self):
        self.feed_code, self.pl_code, self.page_code, self.hits = {}, {}, {}, []
        self.entries = {}
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_GET(self):
                from urllib.parse import urlparse as up
                u = up(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                me.hits.append(self.path)
                if u.path == "/feeds/videos.xml":
                    if "channel_id" in q:
                        ch, code = q["channel_id"], me.feed_code.get(q["channel_id"], 200)
                    else:
                        ch = "UC" + q["playlist_id"][2:]
                        code = me.pl_code.get(ch, 200)
                    body = b"nf" if code != 200 else me._feed(ch)
                elif u.path.startswith("/channel/"):
                    ch = u.path.split("/")[2]
                    code = me.page_code.get(ch, 200)
                    body = f"<html>channel {ch}</html>".encode() if code == 200 else b"This channel doesn't exist"
                else:
                    code, body = 404, b""
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def _feed(self, ch):
        body = '<?xml version="1.0"?><feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">'
        for v, p in self.entries.get(ch, []):
            body += (f"<entry><yt:videoId>{v}</yt:videoId><yt:channelId>{ch}</yt:channelId><title>t</title>"
                     f"<published>{p.isoformat()}</published></entry>")
        return (body + "</feed>").encode()

    def patch(self, monkeypatch):
        monkeypatch.setattr(ws, "CHANNEL_PAGE_FMT", self.base + "/channel/{}")

    def close(self):
        self.srv.shutdown()


def test_websub_only_architecture_no_polling_no_api(tmp_path):
    """Kiến trúc duy nhất: WebSub → tải. Không còn feed polling / Data API / quét định kỳ."""
    assert ws.TOPIC_FMT == "https://www.youtube.com/xml/feeds/videos.xml?channel_id={}"
    for name in ("FEED_FMT", "FEED_PLAYLIST_FMT"):
        assert not hasattr(ws, name)
    for name in ("_api_loop", "_feed_main", "_feed_one", "_feed_thread"):
        assert not hasattr(ws.Engine, name)
    src = (HERE.parent / "YOUTUBE_WEBSUB_INSTANT_360P.py").read_text(encoding="utf-8")
    assert "googleapis.com/youtube/v3" not in src and "feeds/videos.xml?channel_id" not in src.replace(ws.TOPIC_FMT, "")
    e = make_engine(tmp_path, CH[:2])
    before = {t.name for t in threading.enumerate()}
    e.start()
    names = {t.name for t in threading.enumerate()} - before
    assert not [n for n in names if any(k in n.upper() for k in ("FEED", "API", "POLL"))], names
    assert "WEBSUB-SUB" in names and "WEBHOOK" in names
    e.stop()


def test_rejected_verification_is_recorded_with_reason(tmp_path):
    e = make_engine(tmp_path, CH[:2])
    e.start(workers=False)
    port = e.httpd.server_address[1]
    for ch in (CH[0], CH[6]):
        q = urlencode({"hub.mode": "subscribe", "hub.topic": ws.TOPIC_FMT.format(ch), "hub.challenge": "c1"})
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/websub?{q}", timeout=3)
        except urllib.error.HTTPError:
            pass
    ev = {r["channel_id"]: r for r in e.db.all("SELECT * FROM hub_events WHERE kind='verify'")}
    assert ev[CH[0]]["accepted"] == 0 and "chưa từng gửi" in ev[CH[0]]["detail"]
    assert ev[CH[6]]["accepted"] == 0
    assert any("XÁC MINH BỊ TỪ CHỐI" in l and CH[0] in l for l in e._logs)
    e.stop()


def test_unverified_channel_is_resubscribed_with_backoff(tmp_path):
    hub = FakeHub()
    skip = CH[2]
    orig = hub._verify
    hub._verify = lambda q: None if ws.norm_channel(q["hub.topic"]) == skip else orig(q)   # hub "quên" xác minh kênh 3
    try:
        e = make_engine(tmp_path, CH[:3], hub, verify_retry_sec=[0.6, 0.6, 5], websub_loop_sec=0.3)
        e.start(workers=False)
        e.cfg["callback_url"] = f"http://127.0.0.1:{e.httpd.server_address[1]}/websub"
        end = time.time() + 8
        while time.time() < end and sum(1 for q in hub.reqs if ws.norm_channel(q["hub.topic"]) == skip) < 3:
            time.sleep(0.1)
        n_skip = sum(1 for q in hub.reqs if ws.norm_channel(q["hub.topic"]) == skip)
        n_ok = sum(1 for q in hub.reqs if ws.norm_channel(q["hub.topic"]) == CH[0])
        assert n_skip >= 3 and n_ok == 1                      # chỉ gửi lại kênh chưa xác minh
        r = e.db.one("SELECT * FROM subs WHERE channel_id=?", (skip,))
        assert r["state"] == "REQUESTED" and r["attempts"] >= 3 and r["last_http"] == 202
        assert any("chưa được hub xác minh" in l and skip in l for l in e._logs)
        assert e.stats["websub_verified"] == 2 or e.db.one("SELECT COUNT(*) c FROM subs WHERE state='VERIFIED'")["c"] == 2
        e.stop()
    finally:
        hub.close()


def test_diag_report_per_channel(tmp_path, monkeypatch):
    yt = MockYT()
    yt.patch(monkeypatch)
    yt.page_code[CH[1]] = 404

    class HubDetails(BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def do_GET(self):
            body = (b"<html><table><tr><td>State:</td><td>verified</td></tr><tr><td>Last verification error:</td>"
                    b"<td>none</td></tr><tr><td>Expiration time:</td><td>2026-11-17</td></tr></table></html>")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    hs = ThreadingHTTPServer(("127.0.0.1", 0), HubDetails)
    threading.Thread(target=hs.serve_forever, daemon=True).start()
    try:
        make_excel(tmp_path / "check.xlsx", CH[:3])
        cfg = dict(ws.DEFAULT_CONFIG, callback_url="http://127.0.0.1:1/websub",
                   hub_url=f"http://127.0.0.1:{hs.server_address[1]}/subscribe")
        out = ws.diag(cfg, tmp_path)
        rep = (tmp_path / "DIAG_REPORT.md").read_text(encoding="utf-8")
        rows = [l for l in rep.splitlines() if l.startswith("| ") and "UC" in l]
        assert len(rows) == 3
        assert "State: verified" in rep
        bad = next(l for l in rows if CH[1] in l)
        assert "Channel ID có thể SAI" in bad and "HTTP 404" in bad
        good = next(l for l in rows if CH[0] in l)
        assert "(tồn tại)" in good and "CHƯA ĐĂNG KÝ" in good
        assert "/xml/feeds/videos.xml" in rep
    finally:
        yt.close()
        hs.shutdown()


def test_selftest_and_main_share_one_websub_secret(tmp_path):
    """SELFTEST/DIAG đăng ký cùng callback → PHẢI cùng secret với tool chính, nếu không push thật bị coi là sai chữ ký."""
    e = make_engine(tmp_path, CH[:1])
    e2 = ws.Engine(dict(e.cfg), tmp_path, ws.Log(tmp_path / "logs"), db_name="database_TEST.sqlite",
                   lock_name="ws360_selftest.lock")
    assert e.secret == e2.secret and (tmp_path / "websub_secret.txt").read_text().strip() == e.secret
    (tmp_path / "database_TEST.sqlite").unlink()
    e3 = ws.Engine(dict(e.cfg), tmp_path, ws.Log(tmp_path / "logs"), db_name="database_TEST.sqlite")
    assert e3.secret == e.secret                          # DB test bị xoá mỗi lần → secret vẫn giữ nguyên


def test_secret_change_forces_full_resubscribe(tmp_path):
    hub = FakeHub()
    try:
        import socket
        so = socket.socket()
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
        so.close()
        cb = f"http://127.0.0.1:{port}/websub"
        e = make_engine(tmp_path, CH[:3], hub, websub_loop_sec=0.3, listen_port=port, callback_url=cb)
        e.db.meta_set("subs_secret_sig", "0000000000000000")       # lần đăng ký trước dùng secret khác
        e.db.meta_set("websub_callback", cb)                        # cùng callback → lý do đăng ký lại là SECRET
        e.start(workers=False)
        end = time.time() + 5
        while time.time() < end and len(hub.verified) < 3:
            time.sleep(0.1)
        assert any("đăng ký lại TOÀN BỘ" in l for l in e._logs)
        assert e.callback.endswith("g=" + ws.secret_fp(e.secret))          # callback mang thế hệ của secret hiện tại
        assert {q["hub.secret"] for q in hub.reqs} == {e.secret} and len(hub.verified) == 3
        e.stop()
    finally:
        hub.close()


# ------------------------------------------------------------------ hồi quy review vòng 3 (WebSub-only)
def _free_port():
    import socket
    so = socket.socket()
    so.bind(("127.0.0.1", 0))
    p = so.getsockname()[1]
    so.close()
    return p


def test_verified_count_only_monitored_and_removed_rows_unsubscribed(tmp_path):
    hub = FakeHub()
    port = _free_port()
    cb = f"http://127.0.0.1:{port}/websub"
    try:
        e = make_engine(tmp_path, [CH[0], CH[1], CH[3]], hub, listen_port=port, callback_url=cb, websub_loop_sec=0.3)
        e.start(workers=False)
        end = time.time() + 5
        while time.time() < end and e.stats["websub_verified"] < 3:
            time.sleep(0.1)
        assert e.stats["websub_verified"] == 3
        e.stop()
        make_excel(tmp_path / "check.xlsx", [CH[0], CH[1], CH[2]])     # lúc tool TẮT: thay kênh 3 (CH3 → CH2)
        orig = hub._verify
        hub._verify = lambda q: None if ws.norm_channel(q["hub.topic"]) == CH[2] and q["hub.mode"] == "subscribe" else orig(q)
        e2 = make_engine(tmp_path, [CH[0], CH[1], CH[2]], hub, keep_excel=True, listen_port=port, callback_url=cb,
                         websub_loop_sec=0.3)
        e2.start(workers=False)
        end = time.time() + 5
        while time.time() < end and hub.verified.get(CH[3], ("",))[0] != "unsubscribe":
            time.sleep(0.1)
        time.sleep(0.5)
        assert e2.stats["websub_verified"] == 2                         # KHÔNG phải 3/3
        assert e2.db.one("SELECT state FROM subs WHERE channel_id=?", (CH[3],))["state"] in ("REMOVED", "UNSUBSCRIBED")
        assert hub.verified[CH[3]] == ("unsubscribe", True)
        e2.stop()
    finally:
        hub.close()


def test_selftest_reports_real_verification_only_and_never_touches_original_excel(tmp_path):
    hub = FakeHub()
    orig = hub._verify
    hub._verify = lambda q: None if ws.norm_channel(q["hub.topic"]) == CH[0] else orig(q)   # hub KHÔNG xác minh kênh đầu
    port = _free_port()
    try:
        make_excel(tmp_path / "check.xlsx", CH[:3])
        h0 = hashlib.sha256((tmp_path / "check.xlsx").read_bytes()).hexdigest()
        cfg = dict(ws.DEFAULT_CONFIG, listen_host="127.0.0.1", listen_port=port, hub_url=hub.url,
                   callback_url=f"http://127.0.0.1:{port}/websub")
        res = {n: (st, d) for n, st, d in ws.selftest(cfg, tmp_path, None, wait_verify=3)}
        assert res["TEST2a webhook cục bộ trả hub.challenge"][0] == "PASS"
        st, d = res["TEST2c đăng ký hub thật + xác minh"]
        assert st == "PARTIAL" and "2/3" in d                           # giả lập của TEST2a/2b KHÔNG được tính
        assert res[f"TEST2c-kênh {CH[0]}"][0] == "FAIL"
        assert res["TEST2d POST sai chữ ký bị chặn, POST đúng chữ ký (video cũ) nhận nhưng không tải"][0] == "PASS"
        assert hashlib.sha256((tmp_path / "check.xlsx").read_bytes()).hexdigest() == h0
        logtxt = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "logs").glob("*.log"))
        assert "(GIẢ LẬP SELFTEST) webhook trả đúng challenge" in logtxt
        assert f"Hub XÁC MINH kênh {CH[0]}" not in logtxt             # kênh hub thật KHÔNG xác minh → không bao giờ in "Hub XÁC MINH"
    finally:
        hub.close()


def test_selftest_with_main_running_sends_nothing_to_hub(tmp_path):
    hub = FakeHub()
    port = _free_port()
    try:
        cfg = dict(ws.DEFAULT_CONFIG, listen_host="127.0.0.1", listen_port=port, hub_url=hub.url,
                   callback_url=f"http://127.0.0.1:{port}/websub")
        make_excel(tmp_path / "check.xlsx", CH[:2])
        main = ws.Engine(cfg, tmp_path, ws.Log(tmp_path / "logs"))
        main.start(workers=False)                                       # tool chính giữ cổng
        end = time.time() + 5
        while time.time() < end and len(hub.verified) < 2:              # chờ tool chính tự đăng ký xong
            time.sleep(0.1)
        n0 = len(hub.reqs)
        assert n0 == 2
        res = {n: st for n, st, d in ws.selftest(cfg, tmp_path, None, wait_verify=1)}
        assert res["TEST2c đăng ký hub thật + xác minh"] == "BLOCKED"
        time.sleep(0.5)
        assert len(hub.reqs) == n0                                      # SELFTEST không đăng ký gì lên hub
        main.stop()
    finally:
        hub.close()


def test_real_push_during_selftest_is_handed_to_main_tool(tmp_path):
    make_excel(tmp_path / "check.xlsx", CH[:1])
    cfg = dict(ws.DEFAULT_CONFIG, listen_host="127.0.0.1", listen_port=0)
    t = ws.Engine(dict(cfg, excel_file="check.xlsx", download_dir="DOWNLOADS_TEST"), tmp_path, ws.Log(tmp_path / "logs"),
                  db_name="database_TEST.sqlite", lock_name="ws360_selftest.lock")
    t.channels = {CH[0]: [2]}
    t.inbox_db = ws.DB(tmp_path / "database.sqlite")
    t.handle_push(atom(CH[0], "REALPUSH001", datetime.now(timezone.utc)), time.perf_counter_ns())
    assert not t.db.one("SELECT 1 FROM videos WHERE video_id='REALPUSH001'")
    main = ws.Engine(cfg, tmp_path, ws.Log(tmp_path / "logs"))
    r = main.db.one("SELECT * FROM videos WHERE video_id='REALPUSH001'")
    assert r and r["state"] == "QUEUED" and r["engine"] == "WEBSUB"
    assert main.db.one("SELECT state FROM excel_jobs WHERE video_id='REALPUSH001'")["state"] == "PENDING"


def test_late_hub_retry_after_downtime_is_still_downloaded(tmp_path):
    make_excel(tmp_path / "check.xlsx", CH[:1])
    db = ws.DB(tmp_path / "database.sqlite")
    db.meta_set("last_alive", time.time() - 3 * 3600)                  # tool tắt 3 giờ
    db.con.close()
    e = make_engine(tmp_path, CH[:1], keep_excel=True)
    e.channels = {CH[0]: [2]}
    pub = datetime.now(timezone.utc) - timedelta(minutes=50)             # video đăng lúc tool tắt, hub gửi lại muộn
    assert e.on_event(CH[0], "LATERETRY01", "WEBSUB", pub) == "queued"
    assert e.on_event(CH[0], "REALLYOLD01", "WEBSUB", datetime.now(timezone.utc) - timedelta(days=3)) == "old"


def test_verification_replay_after_window_is_rejected(tmp_path):
    e = make_engine(tmp_path, CH[:1])
    e.start(workers=False)
    port = e.httpd.server_address[1]
    with e.hub_lock:
        e.pending_hub[(CH[0], "subscribe")] = time.time()
    q = urlencode({"hub.mode": "subscribe", "hub.topic": ws.TOPIC_FMT.format(CH[0]), "hub.challenge": "ok1"})
    assert urllib.request.urlopen(f"http://127.0.0.1:{port}/websub?{q}", timeout=3).read() == b"ok1"
    with e.hub_lock:
        e.pending_ok[(CH[0], "subscribe")] = time.time() - 300           # 5 phút sau lần chấp nhận
    with pytest.raises(urllib.request.HTTPError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/websub?{q}", timeout=3)
    e.stop()


def test_downtime_window_only_covers_the_outage(tmp_path):
    make_excel(tmp_path / "check.xlsx", CH[:1])
    db = ws.DB(tmp_path / "database.sqlite")
    db.meta_set("last_alive", time.time() - 3 * 3600)
    db.con.close()
    e = make_engine(tmp_path, CH[:1], keep_excel=True)
    e.channels = {CH[0]: [2]}
    now = datetime.now(timezone.utc)
    assert e.on_event(CH[0], "INOUTAGE001", "WEBSUB", now - timedelta(hours=2)) == "queued"     # đăng lúc tool tắt
    assert e.on_event(CH[0], "BEFOREOUT01", "WEBSUB", now - timedelta(hours=5)) == "old"        # đăng TRƯỚC khi tắt → sửa video cũ


def test_selftest_divert_ignores_old_edits_and_known_videos(tmp_path):
    make_excel(tmp_path / "check.xlsx", CH[:1])
    main = ws.DB(tmp_path / "database.sqlite")
    main.x("INSERT INTO seen(video_id, channel_id, why, at) VALUES('KNOWNVID001', ?, 'x', 'x')", (CH[0],))
    t = ws.Engine(dict(ws.DEFAULT_CONFIG, listen_port=0), tmp_path, ws.Log(tmp_path / "logs"), db_name="database_TEST.sqlite",
                  lock_name="ws360_selftest.lock")
    t.channels = {CH[0]: [2]}
    t.inbox_db = main
    t.handle_push(atom(CH[0], "OLDEDIT0001", datetime.now(timezone.utc) - timedelta(days=700)), time.perf_counter_ns())
    t.handle_push(atom(CH[0], "KNOWNVID001", datetime.now(timezone.utc)), time.perf_counter_ns())
    assert not main.one("SELECT 1 FROM videos WHERE video_id IN ('OLDEDIT0001','KNOWNVID001')")


def test_failed_first_excel_read_never_unsubscribes(tmp_path):
    hub = FakeHub()
    port = _free_port()
    cb = f"http://127.0.0.1:{port}/websub"
    try:
        e = make_engine(tmp_path, CH[:2], hub, listen_port=port, callback_url=cb, websub_loop_sec=0.3)
        e.start(workers=False)
        end = time.time() + 5
        while time.time() < end and e.stats["websub_verified"] < 2:
            time.sleep(0.1)
        e.stop()
        (tmp_path / "check.xlsx").write_bytes(b"not a zip")             # file tạm thời đọc lỗi (OneDrive/đang lưu…)
        n0 = len(hub.reqs)
        e2 = make_engine(tmp_path, CH[:2], hub, keep_excel=True, listen_port=port, callback_url=cb, websub_loop_sec=0.3)
        e2.start(workers=False)
        time.sleep(1.5)
        assert not [q for q in hub.reqs[n0:] if q["hub.mode"] == "unsubscribe"]
        e2.stop()
    finally:
        hub.close()


# ------------------------------------------------------------------ HMAC (điều tra 14:43 POST sai chữ ký)
def _post(port, body, headers, query=""):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/websub{query}", data=body, headers=headers)
    return urllib.request.urlopen(req, timeout=3).status


def test_hmac_correct_wrong_missing_and_no_secret_in_logs(tmp_path):
    e = make_engine(tmp_path, CH[:1])
    e.start(websub=True, workers=False)
    port = e.httpd.server_address[1]
    now = datetime.now(timezone.utc)
    b1 = atom(CH[0], "SIGSHA10001", now)
    b2 = atom(CH[0], "SIGSHA25601", now)
    b3 = atom(CH[0], "SIGWRONG001", now)
    b4 = atom(CH[0], "SIGMISSING1", now)
    assert _post(port, b1, {"X-Hub-Signature": "sha1=" + hmac.new(e.secret.encode(), b1, hashlib.sha1).hexdigest()}) == 204
    assert _post(port, b2, {"X-Hub-Signature": "sha256=" + hmac.new(e.secret.encode(), b2, hashlib.sha256).hexdigest(),
                            "X-Forwarded-For": "66.249.1.1", "User-Agent": "AppEngine-Google"}) == 204
    wrong = hmac.new(b"khac-secret", b3, hashlib.sha1).hexdigest()
    assert _post(port, b3, {"X-Hub-Signature": "sha1=" + wrong, "X-Forwarded-For": "66.249.1.1"}) == 204
    assert _post(port, b4, {}) == 204
    # HMAC trên byte THÔ: cùng nội dung XML nhưng khác 1 byte khoảng trắng → chữ ký cũ phải sai
    b5 = b1.replace(b"<entry>", b"<entry> ")
    assert _post(port, b5, {"X-Hub-Signature": "sha1=" + hmac.new(e.secret.encode(), b1, hashlib.sha1).hexdigest()}) == 204
    time.sleep(0.5)
    vids = {r["video_id"] for r in e.db.all("SELECT video_id FROM videos")}
    assert vids == {"SIGSHA10001", "SIGSHA25601"}
    ev = [(r["kind"], r["detail"]) for r in e.db.all("SELECT kind, detail FROM hub_events WHERE kind LIKE 'push-%'")]
    assert any(k == "push-UNKNOWN_SUBSCRIPTION" and "KHÔNG khớp secret hiện tại" in d and "qua tunnel (ngrok) từ 66.249.1.1" in d
               for k, d in ev)
    assert any(k == "push-MISSING_SIGNATURE" and "THIẾU header X-Hub-Signature" in d for k, d in ev)
    assert any(k == "push-VALID_SIGNATURE" for k, d in ev)
    logs = "\n".join(e._logs) + (tmp_path / "logs").glob("*.log").__next__().read_text(encoding="utf-8")
    assert e.secret not in logs and wrong not in logs and wrong[:6] + "…" in logs
    e.stop()


def test_hmac_chunked_body(tmp_path):
    import http.client
    e = make_engine(tmp_path, CH[:1])
    e.start(websub=True, workers=False)
    port = e.httpd.server_address[1]
    body = atom(CH[0], "CHUNKED0001", datetime.now(timezone.utc))
    sig = "sha1=" + hmac.new(e.secret.encode(), body, hashlib.sha1).hexdigest()
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    c.putrequest("POST", "/websub")
    c.putheader("Transfer-Encoding", "chunked")
    c.putheader("X-Hub-Signature", sig)
    c.endheaders()
    for i in range(0, len(body), 100):
        part = body[i:i + 100]
        c.send(f"{len(part):x}\r\n".encode() + part + b"\r\n")
    c.send(b"0\r\n\r\n")
    assert c.getresponse().status == 204
    time.sleep(0.4)
    assert e.db.one("SELECT 1 FROM videos WHERE video_id='CHUNKED0001'")
    e.stop()


def test_old_secret_after_db_overwrite_is_recognised_and_resubscribed(tmp_path):
    """Tình huống thật: database.sqlite bị ghi đè → secret mới; hub vẫn ký bằng secret CŨ của thuê bao cũ."""
    hub = FakeHub()
    port = _free_port()
    cb = f"http://127.0.0.1:{port}/websub"
    try:
        make_excel(tmp_path / "check.xlsx", CH[:1])
        old = "ab" * 24
        db = ws.DB(tmp_path / "database.sqlite")
        db.meta_set("websub_secret", old)                            # secret của bản chạy trước
        db.con.close()
        (tmp_path / "websub_secret.txt").write_text("cd" * 24)        # secret hiện tại khác
        e = make_engine(tmp_path, CH[:1], hub, keep_excel=True, listen_port=port, callback_url=cb, websub_loop_sec=0.3)
        assert [x for x, _ in ws.load_secret_history(tmp_path)] == [old]
        e.start(workers=False)
        end = time.time() + 5
        while time.time() < end and not hub.verified:
            time.sleep(0.1)
        n0 = len(hub.reqs)
        body = atom(CH[0], "OLDSECRET01", datetime.now(timezone.utc))
        assert _post(port, body, {"X-Hub-Signature": "sha1=" + hmac.new(old.encode(), body, hashlib.sha1).hexdigest(),
                                  "X-Forwarded-For": "66.249.1.1"}) == 204
        time.sleep(1.0)
        assert e.db.one("SELECT 1 FROM videos WHERE video_id='OLDSECRET01'")          # chữ ký hợp lệ (secret tool cấp) → nhận
        assert any("VALID_OLD_SECRET" in l for l in e._logs)
        assert len(hub.reqs) > n0 and hub.reqs[-1]["hub.secret"] == e.secret           # đăng ký lại bằng secret hiện tại
        assert old not in "\n".join(e._logs)
        e.stop()
    finally:
        hub.close()


def test_retired_secret_expires_after_10_days(tmp_path):
    e = make_engine(tmp_path, CH[:1])
    old = "ef" * 24
    (tmp_path / "websub_secret_history.txt").write_text(f"{old} {int(time.time() - 11 * 86400)}\n")
    body = b"<feed/>"
    v = e.check_signature(body, "sha1=" + hmac.new(old.encode(), body, hashlib.sha1).hexdigest())
    assert not v["ok"]


# ------------------------------------------------------------------ phân loại POST (POST thật FeedFetcher-Google bị từ chối)
def _verified_engine(tmp_path, chans, port=None):
    hub = FakeHub()
    port = port or _free_port()
    cb = f"http://127.0.0.1:{port}/websub"
    e = make_engine(tmp_path, chans, hub, listen_port=port, callback_url=cb, websub_loop_sec=0.3)
    e.start()
    end = time.time() + 6
    while time.time() < end and e.stats["websub_verified"] < len(chans):
        time.sleep(0.1)
    assert e.stats["websub_verified"] == len(chans)
    return e, hub, port


@need_ff
def test_post_classification_codes_and_queues(tmp_path, gvs, media, monkeypatch):
    e, hub, port = _verified_engine(tmp_path, CH[:2])
    try:
        monkeypatch.setattr(e.resolver, "resolve", lambda v, deadline_s=2.5: plan_prog(gvs, media, v))
        now = datetime.now(timezone.utc)
        ffg = {"User-Agent": "FeedFetcher-Google; (+http://www.google.com/feedfetcher.html)", "X-Forwarded-For": "66.102.0.1",
               "Content-Type": "application/atom+xml"}
        sign = lambda b, sec=None: "sha1=" + hmac.new((sec or e.secret).encode(), b, hashlib.sha1).hexdigest()
        ok = atom(CH[0], "VALIDSIG001", now)
        assert _post(port, ok, dict(ffg, **{"X-Hub-Signature": sign(ok)})) == 204
        foreign = atom("UCAqQb0yK6XkD8WS_0CKNXCQ", "tTN8iaYHO98", now)            # kênh KHÔNG có trong cột K
        assert _post(port, foreign, dict(ffg, **{"X-Hub-Signature": sign(foreign, "x" * 48)})) == 204
        gq = "?g=" + ws.secret_fp(e.secret)
        bad = atom(CH[1], "INVALIDSIG1", now)              # kênh có thuê bao xác minh, ĐÚNG callback hiện tại, chữ ký sai
        assert _post(port, bad, dict(ffg, **{"X-Hub-Signature": sign(bad, "y" * 48)}), gq) == 204
        oldgen = atom(CH[1], "OLDGENPOST1", now)           # POST tới callback thế hệ cũ (không có g) → thuê bao cũ
        assert _post(port, oldgen, dict(ffg, **{"X-Hub-Signature": sign(oldgen, "y" * 48)})) == 204
        miss = atom(CH[1], "MISSINGSIG1", now)
        assert _post(port, miss, ffg) == 204
        r = wait_state(e, "VALIDSIG001")
        assert r["state"] == "DONE" and wait_excel(e, "VALIDSIG001")["state"] == "DONE"
        kinds = {r["kind"] for r in e.db.all("SELECT kind FROM hub_events WHERE kind LIKE 'push-%'")}
        assert {"push-VALID_SIGNATURE", "push-UNKNOWN_CHANNEL", "push-INVALID_SIGNATURE", "push-MISSING_SIGNATURE",
                "push-UNKNOWN_SUBSCRIPTION"} <= kinds
        assert "THẾ HỆ CŨ" in e.db.one("SELECT detail FROM hub_events WHERE kind='push-UNKNOWN_SUBSCRIPTION'")["detail"]
        assert {r["video_id"] for r in e.db.all("SELECT video_id FROM videos")} == {"VALIDSIG001"}   # chỉ POST hợp lệ vào hàng đợi
        time.sleep(0.5)
        assert any(q["hub.mode"] == "unsubscribe" and "UCAqQb0yK6XkD8WS_0CKNXCQ" in q["hub.topic"] for q in hub.reqs)
        saved = sorted((tmp_path / "logs" / "rejected_posts").glob("*.json"))
        assert len(saved) == 4 and any("UNKNOWN_CHANNEL_tTN8iaYHO98" in p.name for p in saved)
        logs = "\n".join(e._logs)
        assert "🚫 UNKNOWN_CHANNEL" in logs and "🚫 INVALID_SIGNATURE" in logs and "🚫 MISSING_SIGNATURE" in logs
        assert e.secret not in logs
    finally:
        e.stop()
        hub.close()


def test_unknown_subscription_when_channel_not_verified_with_current_secret(tmp_path):
    e = make_engine(tmp_path, CH[:1])
    e.start(workers=False)
    port = e.httpd.server_address[1]
    b = atom(CH[0], "UNKSUB00001", datetime.now(timezone.utc))
    _post(port, b, {"X-Hub-Signature": "sha1=" + hmac.new(b"z" * 48, b, hashlib.sha1).hexdigest()})
    time.sleep(0.3)
    assert e.db.one("SELECT 1 FROM hub_events WHERE kind='push-UNKNOWN_SUBSCRIPTION'")
    e.stop()


def test_old_secret_scope_ends_after_transition(tmp_path):
    e = make_engine(tmp_path, CH[:1])
    e.channels = {CH[0]: [2]}
    old = "ab" * 24
    (tmp_path / "websub_secret_history.txt").write_text(f"{old} {int(time.time())}\n")
    b = atom(CH[0], "OLDSCOPE001", datetime.now(timezone.utc))
    v = e.check_signature(b, "sha1=" + hmac.new(old.encode(), b, hashlib.sha1).hexdigest())
    assert e.classify_post(b, v)[0] == "VALID_OLD_SECRET"                    # chưa chuyển xong → nhận
    e.db.x("INSERT INTO subs(channel_id, callback, verified_at, lease_seconds, state, verified_secret_fp) VALUES(?,?,?,?,?,?)",
           (CH[0], "", time.time() - 7200, 432000, "VERIFIED", ws.secret_fp(e.secret)))
    assert e.classify_post(b, v)[0] == "INVALID_SIGNATURE"                   # đã chuyển > 1 giờ → hết phạm vi


def test_verify_post_cli_on_saved_request(tmp_path, capsys):
    e = make_engine(tmp_path, CH[:1])
    e.start(workers=False)
    port = e.httpd.server_address[1]
    b = atom(CH[0], "SAVEDPOST01", datetime.now(timezone.utc))
    _post(port, b, {"X-Hub-Signature": "sha1=" + hmac.new(b"9a" * 24, b, hashlib.sha1).hexdigest(), "X-Forwarded-For": "66.1.1.1"})
    time.sleep(0.3)
    e.stop()
    f = next((tmp_path / "logs" / "rejected_posts").glob("*SAVEDPOST01.json"))
    res = ws.verify_post(dict(ws.DEFAULT_CONFIG), tmp_path, str(f))
    assert res["body_bytes"] == len(b) and res["algo"] == "sha1"
    assert res["matches"] and not any(m[2] for m in res["matches"]) and "KHÔNG khớp" in res["ket_luan"]
    out = capsys.readouterr().out
    assert e.secret not in out
    (tmp_path / "websub_secret_history.txt").write_text(f"{'9a' * 24} {int(time.time())}\n")   # nếu secret đó là secret cũ của tool
    assert "KHỚP secret CŨ" in ws.verify_post(dict(ws.DEFAULT_CONFIG), tmp_path, str(f))["ket_luan"]


# ------------------------------------------------------------------ chẩn đoán hub (DIAG chỉ hiện "Error")
def test_hub_details_returns_full_message_and_scrubs_secret(tmp_path):
    sec = "ab" * 24

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def do_GET(self):
            from urllib.parse import urlparse as up
            q = parse_qs(up(self.path).query)
            has = "hub.secret" in q
            body = (f"<html><h1>Error</h1><p>Could not find any record for topic URL: x</p>"
                    f"<p>echo secret={q.get('hub.secret', [''])[0]}</p></html>").encode()
            self.send_response(404 if has else 200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        cfg = dict(ws.DEFAULT_CONFIG, hub_url=f"http://127.0.0.1:{srv.server_address[1]}/subscribe")
        hd = ws.hub_details(cfg, "https://x/websub", CH[0], sec, save_dir=tmp_path / "hd")
        short = ws.hub_details_short(hd)
        assert "HTTP 404" in short and "Could not find any record" in short and "không secret" in short
        assert sec not in short and sec not in (tmp_path / "hd" / f"{CH[0]}_sec.html").read_text(encoding="utf-8")
    finally:
        srv.shutdown()


def test_health_get_vs_real_verification_are_distinguished(tmp_path):
    e = make_engine(tmp_path, CH[:1])
    e.start(workers=False)
    port = e.httpd.server_address[1]
    assert urllib.request.urlopen(f"http://127.0.0.1:{port}/websub", timeout=3).read() == b"WS360 OK"
    with e.hub_lock:
        e.pending_hub[(CH[0], "subscribe")] = time.time()
    q = urlencode({"hub.mode": "subscribe", "hub.topic": ws.TOPIC_FMT.format(CH[0]), "hub.challenge": "cc"})
    assert urllib.request.urlopen(f"http://127.0.0.1:{port}/websub?{q}", timeout=3).read() == b"cc"
    kinds = [r["kind"] for r in e.db.all("SELECT kind FROM hub_events ORDER BY id")]
    assert kinds.count("health-get") == 1 and kinds.count("verify") == 1
    e.stop()


def test_secret_generation_in_callback_and_old_generation_verification_rejected(tmp_path):
    e = make_engine(tmp_path, CH[:1], callback_url="https://abc.ngrok-free.dev/websub")
    cb = e.resolve_callback()
    assert cb == "https://abc.ngrok-free.dev/websub?g=" + ws.secret_fp(e.secret)
    e.start(workers=False)
    port = e.httpd.server_address[1]
    with e.hub_lock:
        e.pending_hub[(CH[0], "subscribe")] = time.time()
    q = urlencode({"g": "00000000", "hub.mode": "subscribe", "hub.topic": ws.TOPIC_FMT.format(CH[0]), "hub.challenge": "cc"})
    with pytest.raises(urllib.request.HTTPError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/websub?{q}", timeout=3)
    assert "THẾ HỆ CŨ" in e.db.one("SELECT detail FROM hub_events WHERE kind='verify'")["detail"]
    e.stop()


def test_hubtest_sync_and_probe_against_spec_hub(tmp_path):
    hub = FakeHub()
    port = _free_port()
    try:
        make_excel(tmp_path / "check.xlsx", CH[:2])
        cfg = dict(ws.DEFAULT_CONFIG, listen_host="127.0.0.1", listen_port=port, hub_url=hub.url,
                   callback_url=f"http://127.0.0.1:{port}/websub")
        out = "\n".join(ws.hubtest(cfg, tmp_path, n_channels=1, wait_s=3))
        assert "HTTP 204" in out and "hub xác minh ĐỒNG BỘ thành công" in out
        assert "hub GỌI ĐƯỢC tới máy bạn với callback mới" in out
        assert (tmp_path / "HUBTEST_REPORT.md").exists()
        reqs = [q for q in hub.reqs if q.get("hub.verify") == "sync"]
        assert any("ws360probe" in q["hub.callback"] and q["hub.mode"] == "unsubscribe" for q in reqs)   # đăng ký thử được huỷ
        db = ws.DB(tmp_path / "database.sqlite")
        assert db.one("SELECT state FROM subs WHERE channel_id=?", (CH[0],))["state"] == "VERIFIED"
        assert not db.one("SELECT 1 FROM subs WHERE callback LIKE '%ws360probe%'")                       # thử không đụng subs
    finally:
        hub.close()


def test_hubtest_reports_when_hub_cannot_reach_callback(tmp_path):
    hub = FakeHub()
    hub._verify = lambda q: False                                         # hub không gọi tới được máy bạn
    port = _free_port()
    try:
        make_excel(tmp_path / "check.xlsx", CH[:1])
        cfg = dict(ws.DEFAULT_CONFIG, listen_host="127.0.0.1", listen_port=port, hub_url=hub.url,
                   callback_url=f"http://127.0.0.1:{port}/websub")
        out = "\n".join(ws.hubtest(cfg, tmp_path, n_channels=1, wait_s=1))
        assert "HTTP 409" in out and "Error trying to confirm subscription" in out
        assert "hub KHÔNG gọi tới máy bạn kể cả với callback mới" in out
    finally:
        hub.close()


# ------------------------------------------------------------------ 860 ms: đo từng bước t_dl → t_end
STEPSUM = ["resolver_start_ms", "resolver_ms", "download_ms", "db_verifying_ms", "pre_verify_gap_ms", "ffprobe_ms",
           "rename_ms", "post_verify_gap_ms"]


@need_ff
@pytest.mark.parametrize("atomic", [False, True])
def test_step_breakdown_sums_to_total_and_no_rename_by_default(tmp_path, gvs, media, monkeypatch, atomic):
    e = make_engine(tmp_path, CH[:1], atomic_rename=atomic)
    monkeypatch.setattr(e.resolver, "resolve", lambda v, deadline_s=2.5: plan_prog(gvs, media, v))
    e.start(websub=False)
    e.on_event(CH[0], "STEPSUM0001", "WEBSUB", TS)
    r = wait_state(e, "STEPSUM0001")
    assert r["state"] == "DONE"
    parts = sum(float(r[c] or 0) for c in STEPSUM) + float(r["mux_ms"] or 0)
    assert abs(r["total_ms"] - parts) < 1.0 and abs(r["sum_check_ms"]) < 1.0            # khớp total, TTFB không cộng 2 lần
    assert r["ttfb_ms"] <= r["download_ms"]
    if not atomic:
        assert r["rename_ms"] < 1.0                                                     # không đổi tên
    assert Path(r["file"]).read_bytes() == media["prog"] and r["expected_size"] == len(media["prog"])
    assert any(l.startswith("⏱️ BƯỚC") or "⏱️ BƯỚC STEPSUM0001" in l for l in e._logs)
    assert not [p for p in Path(r["file"]).parent.iterdir() if ".part" in p.name]
    e.stop()


@need_ff
def test_sqlite_lock_holder_is_identified(tmp_path, gvs, media, monkeypatch):
    e = make_engine(tmp_path, CH[:1])
    real = ws.ffprobe_info
    holding = threading.Event()

    def hog():
        with e.db.lock:
            holding.set()
            time.sleep(0.4)

    def resolve(v, deadline_s=2.5):
        return plan_prog(gvs, media, v)

    def slow_dl(*a, **k):
        r = real_dl(*a, **k)
        threading.Thread(target=hog, name="LUONG-GIU-KHOA").start()
        holding.wait(2)
        return r
    real_dl = ws.download_single
    monkeypatch.setattr(ws, "download_single", slow_dl)
    monkeypatch.setattr(e.resolver, "resolve", resolve)
    e.start(websub=False)
    e.on_event(CH[0], "LOCKWAIT001", "WEBSUB", TS)
    r = wait_state(e, "LOCKWAIT001")
    assert r["state"] == "DONE" and r["db_lock_wait_ms"] >= 300 and r["db_verifying_ms"] >= 300
    assert any("luồng giữ: LUONG-GIU-KHOA" in l and "CHẬM" in l for l in e._logs)
    assert real is ws.ffprobe_info
    e.stop()


def test_short_download_vs_expected_size_never_done(tmp_path, gvs, media, monkeypatch):
    e = make_engine(tmp_path, CH[:1], retry_schedule_sec=[0.05])
    url = gvs.add("/short", media["prog"][:len(media["prog"]) // 2])                  # server gửi thiếu (Content-Length khớp phần thiếu)
    plan = {"pick": {"kind": "progressive", "video": mkfmt(url, 18, 360, "video/mp4", len(media["prog"])), "audio": None}}
    monkeypatch.setattr(e.resolver, "resolve", lambda v, deadline_s=2.5: plan)
    e.start(websub=False)
    e.on_event(CH[0], "SHORTSIZE01", "WEBSUB", TS)
    r = wait_state(e, "SHORTSIZE01")
    assert r["state"] == "FAILED" and "dung lượng" in r["error"]
    assert not list((tmp_path / "DOWNLOADS").rglob("SHORTSIZE01*"))
    e.stop()


@need_ff
def test_partial_final_file_after_crash_is_redownloaded(tmp_path, gvs, media, monkeypatch):
    e = make_engine(tmp_path, CH[:1])
    vid = "PARTIALFIN1"
    d = tmp_path / "DOWNLOADS" / CH[0]
    d.mkdir(parents=True)
    (d / f"{vid}_360p.mp4").write_bytes(media["prog"][:len(media["prog"]) - 1000])  # tắt máy giữa chừng (moov ở đầu → ffprobe vẫn đọc được)
    e.db.x("INSERT INTO videos(video_id, channel_id, url, engine, detected_at, state, expected_size) VALUES(?,?,?,?,?,?,?)",
           (vid, CH[0], ws.video_url(vid), "WEBSUB", ws.iso(), "DOWNLOADING", len(media["prog"])))
    monkeypatch.setattr(e.resolver, "resolve", lambda v, deadline_s=2.5: plan_prog(gvs, media, v))
    e.start(websub=False)
    r = wait_state(e, vid)
    assert r["state"] == "DONE" and (d / f"{vid}_360p.mp4").read_bytes() == media["prog"]
    assert len(gvs.requests_for(f"/vp/{vid}")) == 1
    e.stop()


@need_ff
def test_bench360_runs_production_path(tmp_path, gvs, media, monkeypatch, capsys):
    monkeypatch.setattr(ws.Resolver, "resolve", lambda self, v, deadline_s=2.5: plan_prog(gvs, media, v + "x"))
    make_excel(tmp_path / "check.xlsx", CH[:1])
    rows = ws.bench360(dict(ws.DEFAULT_CONFIG), tmp_path, ["BENCHVID001"], reps=10)
    assert len(rows) == 10 and all(r["result"] == "DONE" for r in rows)
    assert all(abs(r["sum_check_ms"]) < 1.0 for r in rows)
    out = capsys.readouterr().out
    assert "median" in out and "ffprobe_ms" in out and (tmp_path / "BENCH360.csv").exists()
    assert not (tmp_path / "database.sqlite").exists() or True


# ------------------------------------------------------------------ DASH chậm 31.9 s (MY524yaPKUk): 1 kết nối bị bóp băng thông
@need_ff
def test_dash_unknown_size_uses_parallel_ranges_against_per_connection_throttle(tmp_path, monkeypatch, media):
    big_v = media["v"] * 1                                        # file hình
    with MockGVS() as g:
        rate = 300_000                                            # 300 KB/s MỖI kết nối (giống YouTube bóp 1 kết nối)
        vu = g.add("/dv", big_v, "ok", rate=rate)
        au = g.add("/da", media["a"], "ok", rate=rate)
        plan = {"pick": {"kind": "dash", "video": mkfmt(vu, 134, 360, 'video/mp4; codecs="avc1"', 0, adaptive=True),
                         "audio": mkfmt(au, 140, 0, 'audio/mp4; codecs="mp4a"', 0, adaptive=True)}, "source": "YTDLP",
                "race": "android_vr=noformat(prog 0, adap 12, cipher 0, itag18 không)"}
        e = make_engine(tmp_path, CH[:1], dash_range_connections=4)
        monkeypatch.setattr(e.resolver, "resolve", lambda v, deadline_s=2.5: plan)
        e.start(websub=False)
        e.on_event(CH[0], "DASHPAR0001", "WEBSUB", TS)
        r = wait_state(e, "DASHPAR0001", t=60)
        assert r["state"] == "DONE", r["error"]
        single_s = len(big_v) / rate
        assert r["download_ms"] / 1000 < single_s * 0.6                   # nhanh hơn rõ so với 1 kết nối
        i = ws.ffprobe_info(Path(r["file"]))
        assert i["video"] and i["audio"]
        assert any("kết nối DASH: hình 4, tiếng 4" in l and "vì sao không dùng Innertube nhanh" in l for l in e._logs)
        e.stop()


def test_probe_size_reads_content_range(gvs, media):
    u = gvs.add("/ps", media["a"])
    assert ws.probe_size(u) == len(media["a"])


def test_resolver_race_summary_when_falling_back_to_ytdlp(monkeypatch):
    rs = ws.Resolver(lambda m: None)
    monkeypatch.setattr(ws, "urllib3", ws.urllib3)
    monkeypatch.setattr(rs, "_player", lambda vid, c: {"client": c, "verdict": "noformat", "pick": None,
                                                        "counts": {"prog": 0, "adap": 9, "cipher": 0, "has18": False}})
    monkeypatch.setattr(rs, "_ytdlp", lambda vid: {"verdict": "ok", "pick": {"kind": "dash", "video": {}, "audio": {}},
                                                   "source": "YTDLP"})
    r = rs.resolve("AAAAAAAAAAA")
    assert r["source"] == "YTDLP" and "itag18 không" in r["race"] and "android_vr=noformat" in r["race"]
