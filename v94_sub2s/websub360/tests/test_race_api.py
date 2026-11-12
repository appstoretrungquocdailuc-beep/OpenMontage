# -*- coding: utf-8 -*-
"""RACE MODE (WebSub + YouTube Data API) — test với máy chủ Data API GIẢ (local, đúng định dạng activities.list + lỗi
quota/key của Google), máy chủ video giả và Excel/ffprobe thật. Mọi số đo ở đây là GIẢ LẬP: KHÔNG chứng minh nguồn nào
nhanh hơn ngoài đời — số thật lấy từ RACE_BENCHMARK.md trên máy bạn."""
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from openpyxl import load_workbook

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import test_ws360 as T  # noqa: E402
from test_ws360 import gvs, media  # noqa: E402,F401
import ws360_api_scanner as A  # noqa: E402

ws = T.ws
CH = T.CH
need_ff = T.need_ff
K = ["AIza" + c * 35 for c in "abcdefgh"]          # key giả đúng định dạng


def now():
    return datetime.now(timezone.utc)


class FakeDataAPI:
    """GET /youtube/v3/activities giống Google: key ở header X-goog-api-key, 403 quotaExceeded, 400 keyInvalid…"""

    def __init__(self):
        self.items = {}                 # channel → [item mới nhất trước]
        self.reqs = []                  # (key, channel, t)
        self.key_mode = {}              # key → "quota" | "invalid" | "429"
        self.lock = threading.Lock()
        api = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_GET(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                key = self.headers.get("X-goog-api-key", "")
                with api.lock:
                    api.reqs.append((key, q.get("channelId"), time.time(), self.path))
                    items = list(api.items.get(q.get("channelId"), []))
                mode = api.key_mode.get(key)
                if "key=" in self.path:
                    code, body = 400, {"error": {"code": 400, "message": "key trong URL", "errors": [{"reason": "badRequest"}]}}
                elif mode == "quota":
                    code, body = 403, {"error": {"code": 403, "message": "The request cannot be completed because you have "
                                                 "exceeded your quota.", "errors": [{"reason": "quotaExceeded"}]}}
                elif mode == "invalid":
                    code, body = 400, {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                                                 "errors": [{"reason": "badRequest"}],
                                                 "details": [{"reason": "API_KEY_INVALID"}]}}
                else:
                    code, body = 200, {"items": items[:int(q.get("maxResults", 5))]}
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/youtube/v3/activities"

    def upload(self, ch, vid, title="tiêu đề API", published=None, kind="upload"):
        it = {"snippet": {"type": kind, "publishedAt": (published or now()).isoformat().replace("+00:00", "Z"),
                          "title": title, "channelId": ch},
              "contentDetails": {"upload": {"videoId": vid}} if kind == "upload" else
              {"playlistItem": {"resourceId": {"videoId": vid}}}}
        with self.lock:
            self.items.setdefault(ch, []).insert(0, it)

    def count(self, key=None):
        with self.lock:
            return sum(1 for r in self.reqs if key is None or r[0] == key)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture()
def api():
    a = FakeDataAPI()
    yield a
    a.close()


def keys(tmp, lines):
    (tmp / "api_keys.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def eng(tmp, chans, api, mode="API_ONLY", **kw):
    cfg = dict(detection_mode=mode, api_url=api.url, api_min_interval_sec=0.2, api_quota_safety=1.0,
               api_quota_per_project=100000)
    cfg.update(kw)
    return T.make_engine(tmp, chans, **cfg)


def wait(cond, t=10):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.03)
    return False


def logs(e):
    return "\n".join(e._logs)


# ------------------------------------------------------------------ file key / quota / lập lịch (thuần)
def test_parse_keys_projects_and_never_prints_key():
    rows, warns = A.parse_keys(f"# ghi chú\nPROJ_A|{K[0]}\nPROJ_A,{K[1]}\n{K[2]}\n{K[3]}|PROJ_B|50000\n{K[0]}\nkhong-phai-key\n")
    assert [(r["project"], r["key"]) for r in rows] == [("PROJ_A", K[0]), ("PROJ_A", K[1]),
                                                        (rows[2]["project"], K[2]), ("PROJ_B", K[3])]
    assert rows[2]["auto"] and rows[2]["project"].startswith("auto-") and rows[3]["limit"] == 50000
    assert len(warns) == 2 and not any(k in " ".join(warns) for k in K)
    assert A.scrub_keys(f"lỗi với {K[0]} rồi") == "lỗi với AIza*** rồi"


def test_quota_day_resets_at_pacific_midnight_with_dst():
    # 2026-07-01 06:59 UTC = 23:59 PDT 30/6 → còn 60 s; 07:00 UTC = ngày mới
    t = datetime(2026, 7, 1, 6, 59, tzinfo=timezone.utc).timestamp()
    assert A.quota_day(t) == ("2026-06-30", 60.0)
    assert A.quota_day(t + 60)[0] == "2026-07-01"
    # mùa đông (PST, UTC-8): 2026-01-15 08:00 UTC = 0h PST
    t2 = datetime(2026, 1, 15, 7, 59, 30, tzinfo=timezone.utc).timestamp()
    assert A.quota_day(t2) == ("2026-01-14", 30.0)


def test_adaptive_plan_never_defaults_to_continuous_fast_polling():
    # 1 project (9.500 dùng được), 15 kênh, cả ngày → mỗi kênh ~136 s, KHÔNG phải 0,2 s
    hot, cold = A.plan_intervals(9500, 86400, 15, [], 0.2)
    assert cold == pytest.approx(15 * 86400 / 9500) and cold > 100
    # 20 project → nhanh hơn 20 lần
    assert A.plan_intervals(9500 * 20, 86400, 15, [], 0.2)[1] == pytest.approx(15 * 86400 / 190000)
    # quota dư → chạm sàn min_interval
    assert A.plan_intervals(10 ** 9, 86400, 1, [], 1.0) == (1.0, 1.0)
    # giờ vàng 5 phút × 15 kênh ở 1 s = 4.500 đơn vị → giờ vàng 1 s, phần còn lại chia cho giờ thường
    hot, cold = A.plan_intervals(9500, 86400, 15, [(300, 15)], 1.0)
    assert hot == 1.0 and cold == pytest.approx((15 * 86400 - 4500) / 5000)
    # quota không đủ cho giờ vàng → giãn giờ vàng, NGỪNG giờ thường
    hot, cold = A.plan_intervals(1000, 86400, 15, [(300, 15)], 1.0)
    assert hot == pytest.approx(4.5) and cold == float("inf")
    assert A.plan_intervals(0, 86400, 15, [], 1.0) == (float("inf"), float("inf"))


def test_hot_window_parsing_and_overlap():
    w = A.parse_windows(["20:58-21:03", {"time": "23:59:00-00:01:00", "channels": [CH[0]]}, "rác"])
    assert w[0][:2] == (20 * 3600 + 58 * 60, 21 * 3600 + 3 * 60) and w[1][1] - w[1][0] == 120 and w[1][2] == {CH[0]}
    m = A._local_midnight(time.time())
    assert A.window_overlap(w[0], m, m + 86400) == pytest.approx(300)
    assert A.in_window(w[0], m + 21 * 3600) and not A.in_window(w[0], m + 22 * 3600)


# ------------------------------------------------------------------ API ONLY: quét liên tục, không trình duyệt, không thêm dòng
@need_ff
def test_api_only_detects_every_video_and_keeps_scanning(tmp_path, api, gvs, media, monkeypatch):
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: pytest.fail("không được mở trình duyệt"))
    keys(tmp_path, [f"P1|{K[0]}"])
    api.upload(CH[0], "OLDAPIVID01", published=now() - timedelta(days=2))     # mốc: video cũ → không tải
    api.upload(CH[0], "PLAYLISTVD1", kind="playlistItem")                    # thêm video vào playlist ≠ video mới
    e = eng(tmp_path, CH[:2], api)
    rows_before = load_workbook(tmp_path / "check.xlsx").active.max_row
    monkeypatch.setattr(e.resolver, "resolve", lambda v, deadline_s=2.5: T.plan_prog(gvs, media, v))
    e.start()
    assert e.httpd is None and e.api is not None                              # API_ONLY: không mở webhook WebSub
    assert wait(lambda: e.api.chan.get(CH[0]) and e.api.chan[CH[0]].baseline)
    api.upload(CH[0], "APIVIDEO001", title="Video 1: thử?")
    r1 = T.wait_state(e, "APIVIDEO001")
    api.upload(CH[0], "APIVIDEO002", title="Video 2")                         # bản cũ dừng kênh sau video 1 — bản mới quét tiếp
    api.upload(CH[1], "APIVIDEO003", title="Video 3")
    r2, r3 = T.wait_state(e, "APIVIDEO002"), T.wait_state(e, "APIVIDEO003")
    assert r1["state"] == r2["state"] == r3["state"] == "DONE"
    assert Path(r1["file"]).name == "Video 1： thử？.mp4" and r1["engine"] == "API"
    assert {r["video_id"] for r in e.db.all("SELECT video_id FROM videos")} == {"APIVIDEO001", "APIVIDEO002", "APIVIDEO003"}
    assert T.wait_excel(e, "APIVIDEO003")["state"] == "DONE"
    w = load_workbook(tmp_path / "check.xlsx").active
    assert w.max_row == rows_before                                          # KHÔNG thêm dòng — chỉ ghi cột D đúng kênh
    assert w["D3"].value == ws.video_url("APIVIDEO003")
    L = logs(e)
    assert "API_DETECTED APIVIDEO001 | WINNER=API" in L and "API_QUOTA_USED" in L
    assert "DETECTION_TO_DOWNLOAD_DONE APIVIDEO001" in L
    assert not any(k in L for k in K)                                        # key không bao giờ ra log
    assert all("key=" not in r[3] for r in api.reqs)                         # key ở header, không ở URL
    e.stop()
    assert e.db.one("SELECT used FROM api_quota WHERE project='P1'")["used"] >= api.count()


def test_api_baseline_fresh_video_uses_same_age_gate_as_websub(tmp_path, api):
    keys(tmp_path, [K[0]])
    api.upload(CH[0], "FRESHBASE01", published=now() - timedelta(minutes=3))
    api.upload(CH[0], "OLDBASE0001", published=now() - timedelta(hours=5))
    e = eng(tmp_path, CH[:1], api, download_enabled=False, excel_enabled=False)
    e.start()
    assert wait(lambda: e.db.one("SELECT 1 FROM videos WHERE video_id='FRESHBASE01'"))
    time.sleep(0.5)
    assert not e.db.one("SELECT 1 FROM videos WHERE video_id='OLDBASE0001'")
    assert e.db.one("SELECT baseline FROM detections WHERE video_id='FRESHBASE01'")["baseline"] == 1
    assert "mốc khởi động API" in logs(e) and "1 project riêng" in logs(e)
    e.stop()


# ------------------------------------------------------------------ PARALLEL: race, chống trùng nguyên tử
@need_ff
@pytest.mark.parametrize("first", ["WEBSUB", "API"])
def test_parallel_race_one_download_one_excel_one_winner(tmp_path, api, gvs, media, monkeypatch, first):
    keys(tmp_path, [f"P1|{K[0]}"])
    e = eng(tmp_path, CH[:1], api, mode="PARALLEL")
    calls = []
    monkeypatch.setattr(e.resolver, "resolve", lambda v, deadline_s=2.5: calls.append(v) or T.plan_prog(gvs, media, v))
    e.start()
    assert e.httpd is not None and e.api is not None
    assert wait(lambda: e.api.chan.get(CH[0]) and e.api.chan[CH[0]].baseline)
    vid = "RACEVID0001"
    if first == "WEBSUB":
        e.handle_push(T.atom(CH[0], vid, now(), "tiêu đề WebSub"), __import__("time").perf_counter_ns())
        time.sleep(0.3)
        api.upload(CH[0], vid, title="tiêu đề API")
    else:
        api.upload(CH[0], vid, title="tiêu đề API")
        assert wait(lambda: e.db.one("SELECT 1 FROM videos WHERE video_id=?", (vid,)))
        e.handle_push(T.atom(CH[0], vid, now(), "tiêu đề WebSub"), __import__("time").perf_counter_ns())
    r = T.wait_state(e, vid)
    assert r["state"] == "DONE" and r["engine"] == first
    assert wait(lambda: e.db.one("SELECT COUNT(*) c FROM detections WHERE video_id=?", (vid,))["c"] == 2)
    d = {x["source"]: x for x in e.db.all("SELECT * FROM detections WHERE video_id=?", (vid,))}
    loser = "API" if first == "WEBSUB" else "WEBSUB"
    assert d[first]["winner"] == 1 and d[loser]["winner"] == 0 and d[loser]["lag_ms"] > 0
    assert calls == [vid] and len(gvs.requests_for(f"/vp/{vid}")) == 1       # tải đúng 1 lần
    assert e.db.one("SELECT COUNT(*) c FROM excel_jobs WHERE video_id=?", (vid,))["c"] == 1
    assert T.wait_excel(e, vid)["state"] == "DONE"
    want_title = "tiêu đề WebSub" if first == "WEBSUB" else "tiêu đề API"   # tên file theo nguồn THẮNG (không gọi thêm)
    assert Path(r["file"]).name == f"{want_title}.mp4"
    L = logs(e)
    assert f"{first}_DETECTED {vid} | WINNER={first}" in L and f"{loser}_DETECTED {vid} | WINNER={first}" in L
    rep = ws.race_report(e.db, tmp_path)
    assert rep["pairs"] == 1 and rep[f"{first.lower()}_wins"] == 1 and (tmp_path / "RACE_BENCHMARK.md").exists()
    e.stop()


def test_simultaneous_signals_exactly_one_winner_per_video(tmp_path):
    e = T.make_engine(tmp_path, CH[:1], download_enabled=False)
    e.reload_channels(initial=True)
    res = {}
    for i in range(40):
        vid = f"SIMUL{i:06d}"
        bar = threading.Barrier(2)

        def go(src):
            bar.wait()
            res[(vid, src)] = e.on_event(CH[0], vid, src, now(), "t")
        ts = [threading.Thread(target=go, args=(s,)) for s in ("WEBSUB", "API")]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert sorted((res[(vid, "WEBSUB")], res[(vid, "API")])) == ["dup", "queued"]
    assert e.db.one("SELECT COUNT(*) c FROM videos")["c"] == 40
    assert e.db.one("SELECT COUNT(*) c FROM excel_jobs")["c"] == 40
    assert e.db.one("SELECT COUNT(*) c FROM detections WHERE winner=1")["c"] == 40
    assert e.db.one("SELECT COUNT(*) c FROM detections WHERE winner=0")["c"] == 40
    assert e.xlq.qsize() == 40


@need_ff
def test_second_source_triggers_controlled_early_retry_when_link_not_ready(tmp_path, api, gvs, media, monkeypatch):
    keys(tmp_path, [f"P1|{K[0]}"])
    e = eng(tmp_path, CH[:1], api, mode="PARALLEL", retry_schedule_sec=[30, 30, 30])
    calls = []

    def resolve(v, deadline_s=2.5):
        calls.append(time.time())
        if len(calls) == 1:
            raise ws.NotReady("LOGIN_REQUIRED (video vừa đăng)")
        return T.plan_prog(gvs, media, v)
    monkeypatch.setattr(e.resolver, "resolve", resolve)
    e.start()
    assert wait(lambda: e.api.chan.get(CH[0]) and e.api.chan[CH[0]].baseline)
    vid = "NUDGEVID001"
    e.handle_push(T.atom(CH[0], vid, now(), "x"), time.perf_counter_ns())
    assert T.wait_state(e, vid, ("RETRY_WAIT",))["state"] == "RETRY_WAIT"
    t0 = time.time()
    api.upload(CH[0], vid)                                                   # nguồn còn lại báo → thử lại ngay
    r = T.wait_state(e, vid, t=10)
    assert r["state"] == "DONE" and time.time() - t0 < 5 and r["attempts"] == 2
    time.sleep(0.5)
    assert len(calls) == 2                                                   # lần chờ 30 s cũ bị huỷ, không chạy thêm
    assert "🔔 NUDGEVID001: API cũng báo" in logs(e)
    e.stop()


# ------------------------------------------------------------------ quota theo project
def test_quota_hard_cap_per_project_and_google_errors(tmp_path, api):
    lt = time.localtime()
    a = time.strftime("%H:%M:%S", time.localtime(time.time() - 2))
    b = time.strftime("%H:%M:%S", time.localtime(time.time() + 4))
    keys(tmp_path, [f"PA|{K[0]}|6", f"PB|{K[1]}|6", f"PB|{K[2]}|6", f"PC|{K[3]}|50", f"PD|{K[4]}|50"])
    api.key_mode[K[3]] = "quota"                              # Google báo hết quota sớm hơn tool đếm
    api.key_mode[K[4]] = "invalid"
    e = eng(tmp_path, CH[:1], api, api_hot_windows=[f"{a}-{b}"], download_enabled=False, excel_enabled=False)
    e.start()
    time.sleep(4.5)
    snap = e.api.snapshot()
    e.stop()
    assert api.count(K[0]) <= 6 and api.count(K[1]) + api.count(K[2]) <= 6    # KHÔNG BAO GIỜ vượt quota project
    assert api.count(K[3]) == 1 and api.count(K[4]) == 1                   # bị Google từ chối → ngừng dùng ngay
    L = logs(e)
    assert "PC HẾT quota" in L and A.key_fp(K[4]) in L and not any(k in L for k in K)
    used = {r["project"]: r["used"] for r in e.db.all("SELECT * FROM api_quota")}
    assert used["PA"] == api.count(K[0]) and used["PB"] == api.count(K[1]) + api.count(K[2])
    assert snap["keys"] == 4                                  # PD bị loại (key sai); PC còn key nhưng hết quota
    # khởi động lại cùng ngày: bộ đếm quota giữ nguyên (không tiêu lại từ 0)
    s2 = A.ApiScanner(e, e.cfg, e.log, tmp_path, http=lambda *a: (200, {}, b"{}"))
    s2.load_keys()
    assert s2.projects["PA"].used == used["PA"]
    del lt


def test_adaptive_interval_from_real_quota_state(tmp_path, api):
    keys(tmp_path, [f"P1|{K[0]}"])
    e = eng(tmp_path, CH[:5], api, api_quota_per_project=10000, api_quota_safety=0.95, api_min_interval_sec=1.0,
            download_enabled=False, excel_enabled=False)
    e.reload_channels(initial=True)
    s = A.ApiScanner(e, e.cfg, e.log, tmp_path, http=lambda *a: (200, {}, b"{}"))
    s.load_keys()
    s.projects["P1"].used = 2000
    s._recompute()
    _, ttl = A.quota_day(time.time())
    assert s.ivs[1] == pytest.approx(max(1.0, 5 * ttl / (9500 - 2000)), rel=0.01)
    assert "API_QUOTA_USED 2000/9500" in s.quota_text()


def test_mode_parsing_and_parallel_without_keys_falls_back_to_websub(tmp_path):
    assert ws.norm_mode("websub + api parallel") == "PARALLEL" and ws.norm_mode("API ONLY") == "API_ONLY"
    assert ws.norm_mode("WEBSUB_ONLY") == "WEBSUB_ONLY" and ws.DEFAULT_CONFIG["detection_mode"] == "PARALLEL"
    e = T.make_engine(tmp_path, CH[:1], detection_mode="PARALLEL")
    e.start()
    assert e.api is None and e.httpd is not None
    assert "thiếu key → thực tế chỉ còn WebSub" in logs(e)
    e.stop()


def test_selftest_style_start_never_spends_api_quota(tmp_path, api):
    keys(tmp_path, [f"P1|{K[0]}"])
    e = eng(tmp_path, CH[:1], api, mode="PARALLEL")
    e.start(websub=False)                                     # SELFTEST / DIAG / HUBTEST
    time.sleep(0.5)
    assert e.api is None and api.count() == 0
    e.stop()


# ------------------------------------------------------------------ tên file
def test_original_title_kept_only_windows_impossible_chars_changed():
    d = Path("C:/d")
    ex = "母が決めたある特殊ルールのせいで小5で100kgを超えたんだが…"
    assert ws.build_filename(ex, "TI8R0XUakKw", d) == ex + ".mp4"                  # giữ NGUYÊN, không thêm ID
    assert ws.build_filename("Hôm nay  😀 vui  .NET", "TI8R0XUakKw", d) == "Hôm nay  😀 vui  .NET.mp4"   # emoji, 2 dấu cách: giữ
    assert ws.build_filename(".Part 2", "TI8R0XUakKw", d) == ".Part 2.mp4"        # chấm đầu: giữ (Windows cho phép)
    assert ws.build_filename('a<b>c:d"e/f\\g|h?i*j', "X" * 11, d) == "a＜b＞c：d＂e／f＼g｜h？i＊j.mp4"   # 9 ký tự Windows cấm
    assert ws.build_filename("dòng 1\ndòng 2", "X" * 11, d) == "dòng 1 dòng 2.mp4"   # ký tự điều khiển
    assert ws.build_filename("Kết thúc...", "X" * 11, d) == "Kết thúc.mp4"         # Windows tự xoá dấu chấm cuối
    assert ws.build_filename("NUL", "X" * 11, d) == "NUL_.mp4"
    assert ws.build_filename("", "TI8R0XUakKw", d) == "TI8R0XUakKw_360p.mp4"
    assert ws.build_filename("x", "TI8R0XUakKw", d, mode="TITLE_ID") == "x_TI8R0XUakKw.mp4"
    assert ws.build_filename("x", "TI8R0XUakKw", d, mode="ID_360P") == "TI8R0XUakKw_360p.mp4"
    deep = Path("C:/" + "x" * 150)
    n = ws.build_filename("母" * 300, "TI8R0XUakKw", deep)                          # chỉ cắt khi vượt giới hạn Windows
    assert n.endswith(".mp4") and ws._units(str(deep / n)) <= 240
    n2 = ws.build_filename("😀" * 200, "TI8R0XUakKw", Path("C:/d"))
    assert ws._units(n2) <= 255 and "\ud83d" not in n2.encode("utf-16", "surrogatepass").decode("utf-16")
    assert ws.TEMP_RE.match(".TI8R0XUakKw.part.mp4") and not ws.TEMP_RE.match(".Part 2.mp4")


def test_same_title_two_videos_and_foreign_file_on_disk_get_unique_names(tmp_path):
    e = T.make_engine(tmp_path, CH[:1], download_enabled=False)
    e.reload_channels(initial=True)
    for v in ("SAMETITLE01", "SAMETITLE02", "SAMETITLE03"):
        assert e.on_event(CH[0], v, "WEBSUB", now(), "Cùng tiêu đề") == "queued"
    d = tmp_path / "DOWNLOADS" / CH[0]
    p1 = e._choose_target("SAMETITLE01", CH[0], "Cùng tiêu đề")
    assert p1 == d / "Cùng tiêu đề.mp4"
    assert e._choose_target("SAMETITLE02", CH[0], "Cùng tiêu đề").name == "Cùng tiêu đề (2).mp4"
    d.mkdir(parents=True, exist_ok=True)
    (d / "Cùng tiêu đề (3).mp4").write_bytes(b"file cua nguoi dung")       # file lạ có sẵn → KHÔNG dùng lại, không ghi đè
    assert e._choose_target("SAMETITLE03", CH[0], "Cùng tiêu đề").name == "Cùng tiêu đề (4).mp4"
    assert e._choose_target("SAMETITLE02", CH[0], "khác") .name == "Cùng tiêu đề (2).mp4"   # đã chọn → giữ qua khởi động lại


@need_ff
def test_resolver_title_used_only_when_detection_had_none(tmp_path, gvs, media, monkeypatch):
    e = T.make_engine(tmp_path, CH[:1])
    monkeypatch.setattr(e.resolver, "resolve", lambda v, deadline_s=2.5: dict(T.plan_prog(gvs, media, v), title="Từ resolver"))
    e.start(websub=False)
    e.on_event(CH[0], "NOTITLE0001", "WEBSUB", now(), "")
    e.on_event(CH[0], "HASTITLE001", "WEBSUB", now(), "Từ WebSub")
    a, b = T.wait_state(e, "NOTITLE0001"), T.wait_state(e, "HASTITLE001")
    assert Path(a["file"]).name == "Từ resolver.mp4" and a["title"] == "Từ resolver"
    assert Path(b["file"]).name == "Từ WebSub.mp4"
    e.stop()


def test_restart_cleanup_keeps_titles_with_part_and_removes_only_temp_files(tmp_path):
    d = tmp_path / "DOWNLOADS" / CH[0]
    d.mkdir(parents=True)
    keep = d / ".Part 2.mp4"
    tmp1, tmp2 = d / ".PARTTITLE02.part.mp4", d / ".PARTTITLE03.a1.part.v"
    for p in (keep, tmp1, tmp2):
        p.write_bytes(b"x")
    e = T.make_engine(tmp_path, CH[:1])
    e.recover()
    assert keep.exists() and not tmp1.exists() and not tmp2.exists()


def test_race_report_pending_without_real_pairs(tmp_path):
    e = T.make_engine(tmp_path, CH[:1], download_enabled=False)
    e.reload_channels(initial=True)
    assert e.on_event(CH[0], "ONLYWEBSUB1", "WEBSUB", now(), "t") == "queued"
    r = ws.race_report(e.db, tmp_path)
    assert r["pairs"] == 0 and r["verdict"].startswith("PENDING") and r["only_websub"] == 1
    assert "PENDING" in (tmp_path / "RACE_BENCHMARK.md").read_text(encoding="utf-8")


def test_hot_window_starts_fast_polling_immediately_even_after_long_cold_interval(tmp_path, api):
    keys(tmp_path, [f"P1|{K[0]}|40"])
    a = time.strftime("%H:%M:%S", time.localtime(time.time() + 3))     # (giây bị cắt phần lẻ → bắt đầu sau 2–3 s)
    b = time.strftime("%H:%M:%S", time.localtime(time.time() + 6))
    # quota 40 cho cả ngày: ngoài giờ vàng gần như không quét; giờ vàng 3 s ở 0.2 s = 15 lần → đủ
    e = eng(tmp_path, CH[:1], api, api_hot_windows=[f"{a}-{b}"], download_enabled=False, excel_enabled=False)
    e.start()
    time.sleep(1.5)
    before = api.count()
    assert before == 1                                        # chỉ lần gọi mốc; giờ thường bị giãn theo quota
    time.sleep(4.5)
    during = api.count() - before
    e.stop()
    assert during >= 8, during                                # vào giờ vàng là quét nhanh ngay, không chờ hạn cũ


def test_channel_level_403_does_not_disable_key(tmp_path, api):
    keys(tmp_path, [f"P1|{K[0]}"])
    e = eng(tmp_path, CH[:1], api, download_enabled=False, excel_enabled=False)
    e.reload_channels(initial=True)
    s = A.ApiScanner(e, e.cfg, e.log, tmp_path, http=lambda *a: (200, {}, b"{}"))
    s.load_keys()
    st = A._ChanState(0)
    pr = s.projects["P1"]
    body = json.dumps({"error": {"code": 403, "errors": [{"reason": "forbidden"}], "message": "channel"}}).encode()
    s._handle_error(CH[0], st, pr, K[0], 403, {}, body)
    assert pr.live_keys() == [K[0]] and st.bad_until > time.time()
