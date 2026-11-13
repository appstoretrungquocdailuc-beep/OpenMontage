# -*- coding: utf-8 -*-
"""InnerTube BROWSE (từ V39) tích hợp WS360 — máy chủ InnerTube GIẢ đúng hình dạng JSON (playlistVideoListRenderer /
richGridRenderer + lockupViewModel / player microformat). Số đo ở đây là GIẢ LẬP; tốc độ thật phải đo trên VPS."""
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from openpyxl import load_workbook

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import test_ws360 as T  # noqa: E402
from test_ws360 import gvs, media  # noqa: E402,F401
import ws360_browse as B  # noqa: E402

ws = T.ws
CH = T.CH
need_ff = T.need_ff


def now():
    return datetime.now(timezone.utc)


class FakeInnerTube:
    def __init__(self):
        self.uploads = {}            # channel → [(vid, age_text, title)] mới nhất trước
        self.publish = {}            # vid → chuỗi publishDate của player (None = không có)
        self.status = {}             # channel → mã HTTP ép (403/429)
        self.global_status = None
        self.raw = {}                # channel → bytes trả nguyên (giả JSON đổi cấu trúc)
        self.delay = 0.0
        self.reqs, self.player_reqs = [], []
        self.inflight, self.max_inflight = {}, {}
        self.lock = threading.Lock()
        api = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                return

            def _send(self, code, body):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if "/player" in self.path:
                    vid = body.get("videoId")
                    with api.lock:
                        api.player_reqs.append(vid)
                    pd = api.publish.get(vid)
                    mf = {"playerMicroformatRenderer": {"publishDate": pd}} if pd else {}
                    return self._send(200, json.dumps({"playabilityStatus": {"status": "OK"}, "microformat": mf}).encode())
                bid = body.get("browseId", "")
                surf = "videos" if body.get("params") else "uploads"
                ch = bid if surf == "videos" else "UC" + bid[4:]
                with api.lock:
                    api.reqs.append((ch, surf, time.time(), self.path))
                    api.inflight[ch] = api.inflight.get(ch, 0) + 1
                    api.max_inflight[ch] = max(api.max_inflight.get(ch, 0), api.inflight[ch])
                try:
                    if api.delay:
                        time.sleep(api.delay)
                    st = api.global_status or api.status.get(ch)
                    if st:
                        return self._send(st, b'{"error":{}}')
                    if ch in api.raw:
                        return self._send(200, api.raw[ch])
                    items = api.uploads.get(ch, [])
                    if surf == "uploads":
                        lst = [{"playlistVideoRenderer": {"videoId": v, "title": {"runs": [{"text": t}]},
                                                          "videoInfo": {"runs": [{"text": "0 views"}, {"text": " • "}, {"text": a}]} if a else {}}}
                               for v, a, t in items]
                        data = {"contents": {"twoColumnBrowseResultsRenderer": {"tabs": [{"tabRenderer": {"content": {"sectionListRenderer": {
                            "contents": [{"itemSectionRenderer": {"contents": [{"playlistVideoListRenderer": {"contents": lst}}]}}]}}}}]}}}
                    else:
                        lst = [{"richItemRenderer": {"content": {"lockupViewModel": {
                            "contentType": "LOCKUP_CONTENT_TYPE_VIDEO", "contentId": v,
                            "metadata": {"lockupMetadataViewModel": {"title": {"content": t}, "metadata": {"contentMetadataViewModel": {
                                "metadataRows": [{"metadataParts": [{"text": {"content": "0 views"}}, {"text": {"content": a}}]}]}}}}}}}}
                               for v, a, t in items]
                        data = {"contents": {"twoColumnBrowseResultsRenderer": {"tabs": [{"tabRenderer": {"content": {
                            "richGridRenderer": {"contents": lst}}}}]}}}
                    return self._send(200, json.dumps(data).encode())
                finally:
                    with api.lock:
                        api.inflight[ch] -= 1

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{self.srv.server_address[1]}/youtubei/v1"
        self.browse_url, self.player_url = base + "/browse", base + "/player"

    def add(self, ch, vid, age="5 seconds ago", title=None):
        with self.lock:
            self.uploads.setdefault(ch, []).insert(0, (vid, age, title or f"Tiêu đề {vid}"))

    def count(self, ch=None, surf=None):
        with self.lock:
            return sum(1 for c, s, _, _ in self.reqs if (ch is None or c == ch) and (surf is None or s == surf))

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture()
def it():
    f = FakeInnerTube()
    yield f
    f.close()


def eng(tmp, chans, it, mode="BROWSE_ONLY", keep_excel=False, **kw):
    cfg = dict(detection_mode=mode, browse_url=it.browse_url, browse_player_url=it.player_url, browse_uploads_period_sec=0.2,
               browse_videos_period_sec=0.4, browse_max_rps=50, browse_videos_max_rps=20, browse_connect_timeout=1.0,
               browse_read_timeout=2.0, browse_stats_log_sec=1)
    cfg.update(kw)
    return T.make_engine(tmp, chans, keep_excel=keep_excel, **cfg)


def wait(cond, t=10):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.03)
    return False


def L(e):
    return "\n".join(e._logs)


def seed_old(it, ch, n=4):
    for i in range(n):
        it.add(ch, f"OLD{ch[2:5]}{i:05d}"[:11].ljust(11, "x"), f"{i + 2} days ago")


# ------------------------------------------------------------------ parser
def test_parser_fast_path_scan_fallback_and_age_text():
    data = {"contents": {"twoColumnBrowseResultsRenderer": {"tabs": [{"tabRenderer": {"content": {"sectionListRenderer": {
        "contents": [{"itemSectionRenderer": {"contents": [{"playlistVideoListRenderer": {"contents": [
            {"playlistVideoRenderer": {"videoId": "AAAAAAAAAAA", "title": {"runs": [{"text": "a"}]},
                                       "videoInfo": {"runs": [{"text": "3 views"}, {"text": " • "}, {"text": "7 seconds ago"}]}}},
            {"playlistVideoRenderer": {"videoId": "BBBBBBBBBBB", "title": {"simpleText": "b"}, "upcomingEventData": {"startTime": "1"}}}]}}]}}]}}}}]}}}
    rows, how = B.parse_browse_uploads_json(data)
    assert how == "fast" and [r["video_id"] for r in rows] == ["AAAAAAAAAAA", "BBBBBBBBBBB"] and rows[0]["age_text"].endswith("7 seconds ago")
    assert rows[1]["upcoming"] is True
    odd = {"x": [{"y": {"contents": [{"gridVideoRenderer": {"videoId": "CCCCCCCCCCC", "publishedTimeText": {"simpleText": "1 hour ago"}}}]}}]}
    rows2, how2 = B.parse_browse_uploads_json(odd)
    assert how2 == "scan" and rows2[0]["video_id"] == "CCCCCCCCCCC" and rows2[0]["age_text"] == "1 hour ago"
    assert B.parse_browse_uploads_json({"nothing": 1}) == ([], "none")
    assert B.parse_relative_age("0 views • 5 seconds ago") == (5, 6)
    assert B.parse_relative_age("2 minutes ago") == (120, 180) and B.parse_relative_age("1 hour ago") == (3600, 7200)
    assert B.parse_relative_age("Premieres in 3 hours") == "SCHEDULED" and B.parse_relative_age("") is None
    assert B.parse_relative_age("Streamed 3 days ago") == (259200, 345600)


def test_parse_cost_on_realistic_response_size():
    lst = [{"playlistVideoRenderer": {"videoId": f"V{i:010d}", "title": {"runs": [{"text": "x" * 80}]},
                                      "thumbnail": {"thumbnails": [{"url": "u" * 200}] * 4},
                                      "videoInfo": {"runs": [{"text": "1 views"}, {"text": f"{i + 1} days ago"}]}}} for i in range(100)]
    data = {"contents": {"twoColumnBrowseResultsRenderer": {"tabs": [{"tabRenderer": {"content": {"sectionListRenderer": {
        "contents": [{"itemSectionRenderer": {"contents": [{"playlistVideoListRenderer": {"contents": lst}}]}}]}}}}]}},
            "frameworkUpdates": {"blob": ["z" * 200] * 400}}
    raw = json.dumps(data).encode()
    t0 = time.perf_counter()
    for _ in range(20):
        B.parse_browse_uploads_json(json.loads(raw))
    per = (time.perf_counter() - t0) / 20 * 1000
    assert len(raw) > 150_000 and per < 25, per                 # đường nhanh: vài ms/lượt (giữ GIL ngắn, không làm chậm tải)


# ------------------------------------------------------------------ BROWSE ONLY
@need_ff
def test_browse_only_detects_new_video_without_data_api_or_websub(tmp_path, it, gvs, media, monkeypatch):
    seed_old(it, CH[0])
    seed_old(it, CH[1])
    e = eng(tmp_path, CH[:2], it)
    monkeypatch.setattr(e.resolver, "resolve", lambda v, deadline_s=2.5: T.plan_prog(gvs, media, v))
    e.start()
    assert e.api is None and e.httpd is None and e.browse is not None          # không Data API, không WebSub/ngrok
    assert wait(lambda: e.browse._s("uploads", CH[0]).first_ok and e.browse._s("uploads", CH[1]).first_ok)
    assert e.db.one("SELECT COUNT(*) c FROM videos")["c"] == 0                 # video cũ = baseline, không tải
    it.add(CH[1], "BRWSNEW0001", "3 seconds ago", "Video mới: thử?")
    r = T.wait_state(e, "BRWSNEW0001")
    assert r["state"] == "DONE" and r["engine"] == "BROWSE" and Path(r["file"]).name == "Video mới： thử？.mp4"
    assert T.wait_excel(e, "BRWSNEW0001")["state"] == "DONE"
    assert load_workbook(tmp_path / "check.xlsx").active["D3"].value == ws.video_url("BRWSNEW0001")
    log = L(e)
    assert "BROWSE_DETECTED BRWSNEW0001 | source=BROWSE_UPLOADS | age=3.0s" in log and "BROWSE_RESPONSE_MS=" in log
    assert "DETECTION_TO_DOWNLOAD_DONE BRWSNEW0001" in log and "AIzaSy" not in log
    assert all("googleapis.com" not in p for _, _, _, p in it.reqs) and not it.player_reqs   # chữ tuổi đủ → 0 request xác minh
    assert e.db.one("SELECT verdict, method FROM browse_verify WHERE video_id='BRWSNEW0001'")["method"] == "ROW_TEXT"
    time.sleep(0.6)
    assert len(gvs.requests_for("/vp/BRWSNEW0001")) == 1                       # 2 bề mặt cùng thấy → tải 1 lần
    e.stop()


def test_unverifiable_candidates_are_never_treated_as_new(tmp_path, it):
    seed_old(it, CH[0])
    e = eng(tmp_path, CH[:1], it, download_enabled=False, excel_enabled=False)
    e.start()
    assert wait(lambda: e.browse._s("uploads", CH[0]).first_ok)
    ts = (now() - timedelta(seconds=20)).isoformat()
    it.publish.update({"NOAGEPRECI1": ts, "NOAGEDATE01": now().date().isoformat(), "NOAGEOLD001": "2020-01-01T00:00:00+00:00"})
    for v in ("NOAGEPRECI1", "NOAGEDATE01", "NOAGEOLD001", "NOAGENONE01"):
        it.add(CH[0], v, "")                                                  # không có chữ tuổi
    it.add(CH[0], "SCHEDULED01", "Premieres in 2 hours")
    assert wait(lambda: e.db.one("SELECT COUNT(*) c FROM browse_verify")["c"] >= 5)
    v = {r["video_id"]: (r["verdict"], r["method"]) for r in e.db.all("SELECT * FROM browse_verify")}
    assert v["NOAGEPRECI1"] == ("FRESH", "PLAYER")                             # player (InnerTube, KHÔNG Data API) có giờ chính xác
    assert v["NOAGEDATE01"][0] == "UNVERIFIED"                                 # chỉ có ngày hôm nay → không đoán là mới
    assert v["NOAGEOLD001"] == ("OLD", "PLAYER") and v["NOAGENONE01"][0] == "UNVERIFIED"
    assert v["SCHEDULED01"][0] == "UNVERIFIED"
    assert {r["video_id"] for r in e.db.all("SELECT video_id FROM videos")} == {"NOAGEPRECI1"}
    assert "BROWSE_UNVERIFIED NOAGENONE01" in L(e)
    e.stop()


def test_restart_keeps_baseline_catches_gap_video_and_never_takes_old_reappearing(tmp_path, it):
    seed_old(it, CH[0])
    e = eng(tmp_path, CH[:1], it, download_enabled=False, excel_enabled=False)
    e.start()
    assert wait(lambda: e.browse._s("uploads", CH[0]).first_ok)
    e.stop()
    it.add(CH[0], "GAPNEW00001", "40 seconds ago")                             # đăng lúc tool tắt
    it.add(CH[0], "REAPPEAR001", "3 years ago")                                # video cũ vừa hiện lại (vd đổi riêng tư → công khai)
    e2 = eng(tmp_path, CH[:1], it, keep_excel=True, download_enabled=False, excel_enabled=False)
    e2.start()
    assert wait(lambda: e2.db.one("SELECT 1 FROM videos WHERE video_id='GAPNEW00001'"))
    time.sleep(0.5)
    assert not e2.db.one("SELECT 1 FROM videos WHERE video_id='REAPPEAR001'")
    assert e2.db.one("SELECT verdict FROM browse_verify WHERE video_id='REAPPEAR001'")["verdict"] == "OLD"
    assert not e2.db.one("SELECT 1 FROM videos WHERE video_id LIKE 'OLD%'")    # baseline giữ qua restart
    assert "RESTART_GAP" in L(e2)
    for i in range(6):                                                         # đổi shelf: 6 ID cũ lạ cùng lúc
        it.add(CH[0], f"SHELF{i:06d}", "2 years ago")
    assert wait(lambda: "SHAPE_CHANGE" in L(e2))
    time.sleep(0.5)
    assert not e2.db.one("SELECT 1 FROM videos WHERE video_id LIKE 'SHELF%'")
    e2.stop()


def test_first_ever_start_takes_video_published_seconds_before_start_only_if_text_proves_it(tmp_path, it):
    seed_old(it, CH[0])
    it.add(CH[0], "JUSTBEFORE1", "8 seconds ago")
    e = eng(tmp_path, CH[:1], it, download_enabled=False, excel_enabled=False)
    e.start()
    assert wait(lambda: e.db.one("SELECT 1 FROM videos WHERE video_id='JUSTBEFORE1'"))
    time.sleep(0.4)
    assert e.db.one("SELECT COUNT(*) c FROM videos")["c"] == 1
    e.stop()


# ------------------------------------------------------------------ API + BROWSE
@need_ff
@pytest.mark.parametrize("first", ["BROWSE", "API"])
def test_api_plus_browse_race_single_download_and_duplicate_ignored(tmp_path, it, gvs, media, monkeypatch, first):
    from test_race_api import FakeDataAPI, keys, K
    api = FakeDataAPI()
    try:
        keys(tmp_path, [f"P1|{K[0]}"])
        seed_old(it, CH[0])
        e = eng(tmp_path, CH[:1], it, mode="API_BROWSE", api_url=api.url, api_min_interval_sec=0.2, api_quota_safety=1.0,
                api_quota_per_project=100000)
        monkeypatch.setattr(e.resolver, "resolve", lambda v, deadline_s=2.5: T.plan_prog(gvs, media, v))
        e.start()
        assert e.api is not None and e.browse is not None and e.httpd is None
        assert wait(lambda: e.browse._s("uploads", CH[0]).first_ok and e.api.chan.get(CH[0]) and e.api.chan[CH[0]].baseline)
        vid = "RACEBRW0001"
        if first == "BROWSE":
            it.add(CH[0], vid, "2 seconds ago")
            assert wait(lambda: e.db.one("SELECT 1 FROM videos WHERE video_id=?", (vid,)))
            api.upload(CH[0], vid)
        else:
            api.upload(CH[0], vid)
            assert wait(lambda: e.db.one("SELECT 1 FROM videos WHERE video_id=?", (vid,)))
            it.add(CH[0], vid, "2 seconds ago")
        assert T.wait_state(e, vid)["state"] == "DONE"
        assert wait(lambda: e.db.one("SELECT COUNT(*) c FROM detections WHERE video_id=?", (vid,))["c"] == 2)
        d = {r["source"]: r["winner"] for r in e.db.all("SELECT * FROM detections WHERE video_id=?", (vid,))}
        assert d[first] == 1 and sum(d.values()) == 1
        assert len(gvs.requests_for(f"/vp/{vid}")) == 1 and e.db.one("SELECT COUNT(*) c FROM excel_jobs WHERE video_id=?", (vid,))["c"] == 1
        loser = "API" if first == "BROWSE" else "BROWSE"
        assert f"WINNER={first} | duplicate_ignored={loser}" in L(e)
        e.stop()
        rep = ws.browse_report(tmp_path)
        assert rep["sources"][first]["wins"] == 1 and (tmp_path / "BROWSE_REPORT.md").exists()
    finally:
        api.close()


# ------------------------------------------------------------------ 403/429, chồng request, JSON đổi
def test_global_429_trips_breaker_and_lowers_rps_channel_403_isolated(tmp_path, it):
    for c in CH[:3]:
        seed_old(it, c)
    e = eng(tmp_path, CH[:3], it, download_enabled=False, excel_enabled=False, browse_videos_tab=False)
    e.start()
    assert wait(lambda: all(e.browse._s("uploads", c).first_ok for c in CH[:3]))
    it.status[CH[2]] = 403                                                     # chỉ 1 kênh bị chặn → cô lập
    time.sleep(1.5)
    st = e.browse._s("uploads", CH[2])
    assert st.cooldown_until > time.monotonic() - 2 and e.browse.guard.channel_trips >= 1 and e.browse.guard.global_trips == 0
    n_other = it.count(CH[0])
    time.sleep(0.8)
    assert it.count(CH[0]) > n_other                                           # kênh khác vẫn chạy
    it.status.clear()
    it.global_status = 429                                                     # nhiều kênh bị 429 → nghỉ chung + giảm RPS
    assert wait(lambda: e.browse.guard.global_trips >= 1)
    rps, cool = e.browse.guard.current(e.browse._target("uploads"))
    assert cool > time.monotonic() and rps < e.browse._target("uploads")
    n0 = it.count()
    time.sleep(1.5)
    assert it.count() - n0 <= 2                                                # trong lúc nghỉ chung gần như không gửi
    assert "BROWSE GLOBAL HTTP 429" in L(e)
    it.global_status = None
    e.stop()


def test_never_overlaps_requests_for_same_channel(tmp_path, it):
    seed_old(it, CH[0])
    it.delay = 0.5
    e = eng(tmp_path, CH[:1], it, download_enabled=False, excel_enabled=False, browse_uploads_period_sec=0.2,
            browse_videos_tab=False)
    e.start()
    time.sleep(2.0)
    e.stop()
    assert it.max_inflight.get(CH[0], 0) == 1 and it.count(CH[0]) >= 2


def test_json_structure_change_is_reported_not_silently_empty(tmp_path, it):
    seed_old(it, CH[0])
    seed_old(it, CH[1])
    e = eng(tmp_path, CH[:2], it, download_enabled=False, excel_enabled=False, browse_videos_tab=False)
    e.start()
    assert wait(lambda: e.browse._s("uploads", CH[0]).first_ok and e.browse._s("uploads", CH[1]).first_ok)
    it.raw[CH[0]] = json.dumps({"contents": {"somethingNew": {"items": []}}}).encode()     # cấu trúc mới → 0 dòng
    it.raw[CH[1]] = b"{not json"
    assert wait(lambda: "BROWSE_SHAPE_EMPTY" in L(e) and "BROWSE_PARSE_ERROR" in L(e))
    s = e.browse.snapshot()
    assert s["parse"] >= 1
    e.stop()


def test_mode_names_and_old_modes_still_supported():
    assert ws.norm_mode("API + BROWSE") == "API_BROWSE" and ws.norm_mode("BROWSE ONLY") == "BROWSE_ONLY"
    for m in ("WEBSUB_ONLY", "API_ONLY", "PARALLEL"):
        assert ws.norm_mode(m) == m


def _lockup_json(items):
    lst = [{"lockupViewModel": {"contentId": v, "contentType": "LOCKUP_CONTENT_TYPE_VIDEO", "rendererContext": {"x": 1},
                                "metadata": {"lockupMetadataViewModel": {"title": {"content": t}, "metadata": {"contentMetadataViewModel": {
                                    "metadataRows": [{"metadataParts": [{"text": {"content": 'Kênh "A" \\ b'}}]},
                                                     {"metadataParts": [{"text": {"content": "1 view"}}, {"text": {"content": a}}]}]}}}}}}
           for v, a, t in items]
    return {"responseContext": {}, "contents": {"twoColumnBrowseResultsRenderer": {"tabs": [{"tabRenderer": {"content": {
        "sectionListRenderer": {"contents": [{"itemSectionRenderer": {"contents": lst}}]}}}}]}}, "sidebar": {"lockupViewModel": {}}}


def test_fast_raw_reader_equals_full_json_decode_including_escapes():
    items = [(f"V{i:010d}", "5 seconds ago" if i == 0 else f"{i} days ago", f'母が "決めた" \\\\ ルール {i} 😀') for i in range(15)]
    for ascii_ in (True, False):                                  # JSON gọn như YouTube (prettyPrint=false), cả 2 kiểu mã hoá
        raw = json.dumps(_lockup_json(items), separators=(",", ":"), ensure_ascii=ascii_).encode()
        full, _ = B.parse_browse_uploads_json(json.loads(raw))
        fast = B.fast_rows_from_raw(raw)
        assert fast is not None and len(fast) == 12
        assert [(x["video_id"], x["title"], x["age_text"]) for x in fast] == [(x["video_id"], x["title"], x["age_text"]) for x in full]
        assert B.parse_relative_age(fast[0]["age_text"]) == (5, 6)
    assert B.fast_rows_from_raw(json.dumps(_lockup_json(items)).encode()) is None   # định dạng khác (có khoảng trắng) → về json đầy đủ
    assert B.fast_rows_from_raw(b'{"contents":{"x":1}}') is None                 # không chắc → None (dùng json đầy đủ)


def test_fast_reader_mismatch_disables_itself(tmp_path, it, monkeypatch):
    seed_old(it, CH[0])
    e = eng(tmp_path, CH[:1], it, download_enabled=False, excel_enabled=False, browse_fast_verify_every=1)
    monkeypatch.setattr(B, "fast_rows_from_raw", lambda raw, limit=12: [{"video_id": "WRONGWRONG1", "title": "", "age_text": ""}])
    e.start()
    assert wait(lambda: "bộ đọc nhanh lệch" in L(e))
    assert e.browse.fast_raw is False and not e.db.one("SELECT 1 FROM browse_verify WHERE video_id='WRONGWRONG1'")
    e.stop()
