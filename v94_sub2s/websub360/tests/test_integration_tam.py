# -*- coding: utf-8 -*-
"""Tích hợp WS360 + TAM. Dùng FILE TAM THẬT (tests/fixtures/tam_INSTANT_POST.py: JOURNAL, PostStatus, UploadResult,
PerfTrace, khoá profile, FSM đều thật). CHỈ lớp trình duyệt (Genlogin runProfile / Selenium / TikTok) được thay bằng bản
giả — máy test không có Genlogin, Chrome, tài khoản TikTok và KHÔNG được bấm Post thật.
Mọi số đo ở đây là GIẢ LẬP, không phải tốc độ đăng TikTok thật."""
import hashlib
import hmac
import os
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_ws360 as T  # noqa: E402
from test_ws360 import gvs, media  # noqa: E402,F401  (fixture dùng chung: máy chủ video giả + file mp4 thật)

ws = T.ws
FIX_TAM = HERE / "fixtures" / "tam_INSTANT_POST.py"
need_ff = T.need_ff
CH = T.CH
P1, P2, P3 = "26555915", "25704605", "11112222"


class FakeDriver:
    def __init__(self, pid):
        self.pid = pid
        self.dead = False

    @property
    def current_url(self):
        if self.dead:
            raise RuntimeError("invalid session id")
        return "https://www.tiktok.com/tiktokstudio/upload?from=creator_center"

    def quit(self):
        self.dead = True


class FakeTikTok:
    """Thay ĐÚNG 3 hàm chạm trình duyệt của TAM; giữ nguyên JOURNAL / FSM / khoá / PerfTrace thật."""

    def __init__(self, tam, monkeypatch, upload_s=0.05):
        self.tam = tam
        self.opens = []
        self.names = []                      # tên file uploader nhận
        self.uploads = []                    # (pid, path, t_start_ns)
        self.outcome = {}                    # video_id → "CONFIRMED" | "UNKNOWN" | "FAILED_SAFE" | "FAILED_VISIBLE"
        self.active = {}
        self.max_conc = {}
        self.upload_s = upload_s
        self.open_fail = set()
        self.lock = threading.Lock()
        monkeypatch.setattr(tam, "setup_and_navigate_profile", self.open)
        monkeypatch.setattr(tam, "upload_video_to_tiktok_ex", self.upload)
        monkeypatch.setattr(tam, "prepare_upload_page", lambda drv, log, kill=None, fsm=None, trace=None, tag="": True)

    def open(self, pid, token, chromedriver, url):
        time.sleep(0.05)
        with self.lock:
            self.opens.append(pid)
        if pid in self.open_fail:
            return pid, None
        fsm = self.tam.get_fsm(pid)
        if fsm.state == self.tam.PState.IDLE:
            fsm.to(self.tam.PState.READY, "giả: trang upload sẵn sàng")
        return pid, FakeDriver(pid)

    def upload(self, drv, paths, logger_func=None, check_force_kill=None, pid="", mode="", trace=None, hooks=None, dry_run=False):
        tam = self.tam
        PS = tam.PostStatus
        with self.lock:
            self.active[pid] = self.active.get(pid, 0) + 1
            self.max_conc[pid] = max(self.max_conc.get(pid, 0), self.active[pid])
            self.uploads.append((pid, paths[0], time.perf_counter_ns()))
        try:
            assert not Path(paths[0]).name.startswith("."), "uploader nhận file .part!"
            trace.mark("SEND_KEYS_START")
            trace.mark("SEND_KEYS_END")
            time.sleep(self.upload_s)
            trace.mark("PROCESSING_READY")
            nm = Path(paths[0]).name
            vid = nm.split("_360p")[0] if nm.endswith("_360p.mp4") else nm[-15:-4]   # '<tiêu đề>_<ID>.mp4' hoặc '<ID>_360p.mp4'
            self.names.append(nm)
            out = self.outcome.get(vid, "CONFIRMED")
            if dry_run:
                res = tam.UploadResult(PS.DRY_RUN, "dry-run", False, "", trace, paths)
            elif out == "FAILED_SAFE":
                res = tam.UploadResult(PS.FAILED_SAFE, "không thấy ô nạp file", False, "", trace, paths)
            else:
                hooks.before_click()
                trace.mark("POST_CLICKED")
                time.sleep(0.01)
                if out == "CONFIRMED":
                    trace.mark("POST_CONFIRMED")
                    res = tam.UploadResult(PS.CONFIRMED, "", True, "giả: 'Video published'", trace, paths)
                elif out == "UNKNOWN":
                    tam.get_fsm(pid).to(tam.PState.UNKNOWN, "giả: không xác minh được")
                    res = tam.UploadResult(PS.UNKNOWN, "giả: không xác minh được", True, "", trace, paths)
                else:
                    res = tam.UploadResult(PS.FAILED_VISIBLE, "giả: TikTok báo lỗi", True, "", trace, paths)
            if hooks and hooks.after_result:
                hooks.after_result(res)
            return res
        finally:
            with self.lock:
                self.active[pid] -= 1


@pytest.fixture()
def tam(tmp_path, monkeypatch):
    monkeypatch.setenv("TAM_JOURNAL", str(tmp_path / "tam_post_journal.jsonl"))
    monkeypatch.setenv("TAM_PERF_LOG", str(tmp_path / "tam_perf.jsonl"))
    monkeypatch.setenv("TAM_HIGH_PRIORITY", "0")
    from ws360_tam_bridge import load_tam
    mod = load_tam(str(FIX_TAM))
    with mod.drivers_lock:
        mod.drivers.clear()
    for pid in (P1, P2, P3):
        mod.drop_fsm(pid)
    yield mod


def make_excel(path, rows):
    """rows: [(profile_C, channel_K)]"""
    from openpyxl import Workbook
    wb = Workbook()
    s = wb.active
    s.append(["A", "B", "PROFILE ID", "URL", "E", "F", "G", "H", "I", "J", "CHANNEL ID"])
    for pid, ch in rows:
        s.append(["a", "b", pid, None, None, None, None, None, None, None, ch])
    wb.save(path)


def engine(tmp_path, rows, tam, **cfg):
    make_excel(tmp_path / "check.xlsx", rows)
    e = T.make_engine(tmp_path, sorted({ch for _, ch in rows}), keep_excel=True, **cfg)
    br = ws.attach_tiktok(e, dict(e.cfg, tam_path=str(FIX_TAM), **cfg), e.log, tam=tam)
    return e, br


def wait_job(br, job_id, states=("CONFIRMED", "UNKNOWN", "FAILED", "FAILED_VISIBLE", "HELD", "DUPLICATE", "DRY_RUN_DONE",
                                 "WAIT_USER"), t=20):
    end = time.time() + t
    while time.time() < end:
        r = br.db.one("SELECT * FROM upload_jobs WHERE job_id=?", (job_id,))
        if r and r["state"] in states:
            return r
        time.sleep(0.03)
    return br.db.one("SELECT * FROM upload_jobs WHERE job_id=?", (job_id,))


# ------------------------------------------------------------------ 1–4: ánh xạ cột C ↔ K
def test_mapping_C_to_K_rules(tmp_path):
    from ws360_tam_bridge import read_profile_mapping
    p = tmp_path / "check.xlsx"
    make_excel(p, [(P1, CH[0]), (26555915.0, CH[0]), (P2, CH[1]), (P3, CH[1]), (None, CH[2]), ("abc", CH[3]),
                   (P1, CH[4])])
    t, issues = read_profile_mapping(p, "K", "C")
    assert t[CH[0]] == [P1]                                  # 2 dòng cùng profile (kể cả kiểu số 26555915.0) → 1 đích
    assert t[CH[1]] == [P2, P3] and any("TẤT CẢ" in i for i in issues)
    assert t[CH[2]] == [] and t[CH[3]] == []
    assert any("CHƯA có Profile ID" in i for i in issues) and any("KHÔNG hợp lệ" in i for i in issues)
    assert t[CH[4]] == [P1]                                  # nhiều kênh cùng 1 profile


# ------------------------------------------------------------------ 5–7: mở profile bằng backend TAM gốc, không mở trùng
def test_prewarm_opens_each_profile_once_with_concurrency_limit(tmp_path, tam, monkeypatch):
    fk = FakeTikTok(tam, monkeypatch)
    e, br = engine(tmp_path, [(P1, CH[0]), (P2, CH[1]), (P1, CH[2])], tam, open_concurrency=1)
    br.start()
    for _ in range(3):                                       # gọi mở trùng song song
        threading.Thread(target=br._ensure_open, args=(P1,)).start()
    end = time.time() + 5
    while time.time() < end and br.snapshot()["ready"] < 2:
        time.sleep(0.05)
    time.sleep(0.3)
    assert sorted(fk.opens) == sorted([P1, P2])                      # mỗi profile mở ĐÚNG 1 lần
    assert br.snapshot()["ready"] == 2
    with tam.drivers_lock:
        assert set(tam.drivers) == {P1, P2}                  # dùng CHÍNH danh sách drivers của TAM
    br.stop(wait_s=2)


def test_smart_prewarm_opens_only_priority_then_on_demand(tmp_path, tam, monkeypatch):
    fk = FakeTikTok(tam, monkeypatch)
    e, br = engine(tmp_path, [(P1, CH[0]), (P2, CH[1])], tam, prewarm_mode="SMART_PREWARM", smart_profiles=[P2])
    br.start()
    time.sleep(0.4)
    assert fk.opens == [P2]
    f = tmp_path / "DOWNLOADS" / CH[0] / "SMARTVID001_360p.mp4"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x" * 100)
    br.submit({"vid": "SMARTVID001", "ch": CH[0], "path": str(f), "size": 100})
    assert wait_job(br, "SMARTVID001:" + P1)["state"] == "CONFIRMED"
    assert sorted(fk.opens) == sorted([P1, P2])                      # P1 mở khi có job
    br.stop(wait_s=2)


# ------------------------------------------------------------------ 8–10, 14: DONE → đúng profile, không .part, không đọc Excel/quét thư mục
@need_ff
def test_websub_to_download_to_tiktok_end_to_end(tmp_path, tam, monkeypatch, gvs, media):
    fk = FakeTikTok(tam, monkeypatch)
    e, br = engine(tmp_path, [(P1, CH[0]), (P2, CH[1])], tam)
    monkeypatch.setattr(e.resolver, "resolve", lambda v, deadline_s=2.5: T.plan_prog(gvs, media, v))
    calls = {"map": 0}
    import ws360_tam_bridge as B
    real_map = B.read_profile_mapping

    def counted(*a, **k):
        calls["map"] += 1
        return real_map(*a, **k)
    monkeypatch.setattr(B, "read_profile_mapping", counted)
    listdir_calls = []
    real_scandir = os.scandir
    monkeypatch.setattr(os, "scandir", lambda *a, **k: listdir_calls.append(a) or real_scandir(*a, **k))
    e.start()
    br.start()
    end = time.time() + 5
    while time.time() < end and br.snapshot()["ready"] < 2:
        time.sleep(0.05)
    port = e.httpd.server_address[1]
    with e.hub_lock:
        e.pending_hub[(CH[0], "subscribe")] = time.time()
    maps_before = calls["map"]
    for vid, ch in (("E2EVIDEO001", CH[0]), ("E2EVIDEO002", CH[0]), ("E2EVIDEO003", CH[1])):
        body = T.atom(ch, vid, datetime.now(timezone.utc))
        sig = "sha1=" + hmac.new(e.secret.encode(), body, hashlib.sha1).hexdigest()
        T._post(port, body, {"X-Hub-Signature": sig}, "?g=" + ws.secret_fp(e.secret))
    r1 = wait_job(br, "E2EVIDEO001:" + P1)
    r2 = wait_job(br, "E2EVIDEO002:" + P1)
    r3 = wait_job(br, "E2EVIDEO003:" + P2)
    assert r1["state"] == r2["state"] == r3["state"] == "CONFIRMED"
    up = {(pid, Path(p).name) for pid, p, _ in fk.uploads}
    assert up == {(P1, "t_E2EVIDEO001.mp4"), (P1, "t_E2EVIDEO002.mp4"), (P2, "t_E2EVIDEO003.mp4")}   # tên = tiêu đề WebSub ('t')
    assert fk.max_conc[P1] == 1                              # 2 video cùng profile → tuần tự, không 2 phiên
    assert calls["map"] == maps_before                       # KHÔNG đọc lại Excel cho mỗi job
    assert not [a for a in listdir_calls if "DOWNLOADS" in str(a)]   # KHÔNG quét thư mục để tìm video
    assert sorted(fk.opens) == sorted([P1, P2])                      # không mở lại profile đang READY
    for r in (r1, r2, r3):
        assert r["done_to_attach_ms"] is not None and r["websub_to_confirm_ms"] is not None
    # job đầu tiên của mỗi profile: DONE → gắn file gần như tức thì (giả lập; số thật cần đo trên máy bạn)
    assert r1["done_to_attach_ms"] < 200 and r3["done_to_attach_ms"] < 200
    assert T.wait_excel(e, "E2EVIDEO003")["state"] in ("DONE", "SUPERSEDED")
    j = (tmp_path / "tam_post_journal.jsonl").read_text(encoding="utf-8")
    assert j.count('"ev": "POSTING"') == 3 and j.count('"ev": "CONFIRMED"') == 3
    assert any("DONE→gắn file" in l for l in e._logs)
    e.stop()


def test_part_file_is_never_uploaded(tmp_path, tam, monkeypatch):
    fk = FakeTikTok(tam, monkeypatch)
    e, br = engine(tmp_path, [(P1, CH[0])], tam)
    br.start()
    f = tmp_path / ".PARTVIDEO01.part.mp4"
    f.write_bytes(b"x" * 10)
    br.submit({"vid": "PARTVIDEO01", "ch": CH[0], "path": str(f), "size": 10})
    r = wait_job(br, "PARTVIDEO01:" + P1)
    assert r["state"] == "HELD" and ".part" in r["reason"] and not fk.uploads
    br.stop(wait_s=2)


def test_missing_profile_is_held_not_posted_elsewhere(tmp_path, tam, monkeypatch):
    fk = FakeTikTok(tam, monkeypatch)
    e, br = engine(tmp_path, [(P1, CH[0]), (None, CH[1])], tam)
    br.start()
    f = tmp_path / "NOPROFILE01_360p.mp4"
    f.write_bytes(b"x")
    assert br.submit({"vid": "NOPROFILE01", "ch": CH[1], "path": str(f), "size": 1}) == []
    r = br.db.one("SELECT * FROM upload_jobs WHERE video_id='NOPROFILE01'")
    assert r["state"] == "HELD" and not fk.uploads
    br.stop(wait_s=2)


def test_profile_id_not_found_keeps_job_waiting(tmp_path, tam, monkeypatch):
    fk = FakeTikTok(tam, monkeypatch)
    fk.open_fail.add(P3)
    e, br = engine(tmp_path, [(P3, CH[0])], tam)
    br.start()
    f = tmp_path / "BADPROFILE1_360p.mp4"
    f.write_bytes(b"x")
    br.submit({"vid": "BADPROFILE1", "ch": CH[0], "path": str(f), "size": 1})
    r = wait_job(br, "BADPROFILE1:" + P3, states=("RETRY",), t=5)
    assert r["state"] == "RETRY" and "chưa mở được" in r["reason"] and not fk.uploads
    br.stop(wait_s=2)


# ------------------------------------------------------------------ 11: tải vẫn chạy khi uploader lỗi
@need_ff
def test_download_and_excel_continue_when_uploader_broken(tmp_path, tam, monkeypatch, gvs, media):
    fk = FakeTikTok(tam, monkeypatch)
    monkeypatch.setattr(tam, "upload_video_to_tiktok_ex", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("TAM hỏng")))
    e, br = engine(tmp_path, [(P1, CH[0])], tam)
    monkeypatch.setattr(e.resolver, "resolve", lambda v, deadline_s=2.5: T.plan_prog(gvs, media, v))
    e.start()
    br.start()
    e.on_event(CH[0], "UPBROKEN001", "WEBSUB", T.TS)
    assert T.wait_state(e, "UPBROKEN001")["state"] == "DONE"
    assert T.wait_excel(e, "UPBROKEN001")["state"] == "DONE"
    r = wait_job(br, "UPBROKEN001:" + P1, states=("RETRY",), t=5)
    assert r["state"] == "RETRY"
    e.stop()


# ------------------------------------------------------------------ 12–13: restart không mất job, không đăng trùng
def test_restart_recovers_job_and_never_double_posts(tmp_path, tam, monkeypatch):
    fk = FakeTikTok(tam, monkeypatch)
    e, br = engine(tmp_path, [(P1, CH[0])], tam)
    f = tmp_path / "RESTART0001_360p.mp4"
    f.write_bytes(b"y" * 50)
    br.reload_mapping()
    br.accepting = True                                      # nhận job nhưng KHÔNG có worker chạy (tool tắt ngay sau)
    br.stop_ev.set()
    br.submit({"vid": "RESTART0001", "ch": CH[0], "path": str(f), "size": 50})
    assert br.db.one("SELECT state FROM upload_jobs WHERE job_id=?", ("RESTART0001:" + P1,))["state"] == "QUEUED"
    e.db.con.close()
    e2, br2 = engine(tmp_path, [(P1, CH[0])], tam)
    br2.start()                                              # khởi động lại → khôi phục từ SQLite
    assert wait_job(br2, "RESTART0001:" + P1)["state"] == "CONFIRMED"
    br2.stop(wait_s=2)
    e3, br3 = engine(tmp_path, [(P1, CH[0])], tam)
    br3.start()
    br3.submit({"vid": "RESTART0001", "ch": CH[0], "path": str(f), "size": 50})      # đưa lại cùng video
    time.sleep(0.5)
    assert len([u for u in fk.uploads if "RESTART0001" in u[1]]) == 1               # chỉ đăng 1 lần
    br3.stop(wait_s=2)


def test_journal_blocks_repost_after_unknown_even_with_new_job(tmp_path, tam, monkeypatch):
    fk = FakeTikTok(tam, monkeypatch)
    fk.outcome["UNKNOWN0001"] = "UNKNOWN"
    e, br = engine(tmp_path, [(P1, CH[0])], tam)
    br.start()
    f = tmp_path / "UNKNOWN0001_360p.mp4"
    f.write_bytes(b"z" * 20)
    br.submit({"vid": "UNKNOWN0001", "ch": CH[0], "path": str(f), "size": 20})
    assert wait_job(br, "UNKNOWN0001:" + P1)["state"] == "UNKNOWN"
    # video KHÁC tới cùng profile → profile đang UNKNOWN → GIỮ chờ người kiểm tra, không đăng
    g = tmp_path / "AFTERUNK001_360p.mp4"
    g.write_bytes(b"w" * 20)
    br.submit({"vid": "AFTERUNK001", "ch": CH[0], "path": str(g), "size": 20})
    assert wait_job(br, "AFTERUNK001:" + P1, states=("WAIT_USER",), t=5)["state"] == "WAIT_USER"
    assert len(fk.uploads) == 1
    br.reset_unknown(P1)                                     # người dùng bấm "Đã kiểm tra"
    assert wait_job(br, "AFTERUNK001:" + P1)["state"] == "CONFIRMED"
    # job mới cho file đã UNKNOWN trong nhật ký → KHÔNG đăng lại
    br.db.x("DELETE FROM upload_jobs WHERE video_id='UNKNOWN0001'")
    br.submit({"vid": "UNKNOWN0001", "ch": CH[0], "path": str(f), "size": 20})
    r = wait_job(br, "UNKNOWN0001:" + P1)
    assert r["state"] == "UNKNOWN" and "nhật ký" in r["reason"] and len(fk.uploads) == 2
    br.stop(wait_s=2)


# ------------------------------------------------------------------ 15: mất kết nối Selenium → mở lại
def test_dead_selenium_session_is_reopened(tmp_path, tam, monkeypatch):
    fk = FakeTikTok(tam, monkeypatch)
    e, br = engine(tmp_path, [(P1, CH[0])], tam)
    br.start()
    end = time.time() + 3
    while time.time() < end and br.snapshot()["ready"] < 1:
        time.sleep(0.05)
    with tam.drivers_lock:
        tam.drivers[P1].dead = True
    f = tmp_path / "DEADSESS001_360p.mp4"
    f.write_bytes(b"d")
    br.submit({"vid": "DEADSESS001", "ch": CH[0], "path": str(f), "size": 1})
    assert wait_job(br, "DEADSESS001:" + P1)["state"] == "CONFIRMED"
    assert fk.opens.count(P1) == 2
    br.stop(wait_s=2)


# ------------------------------------------------------------------ FAILED_SAFE retry, FAILED_VISIBLE không retry, dry-run
def test_failed_safe_retried_failed_visible_not(tmp_path, tam, monkeypatch):
    fk = FakeTikTok(tam, monkeypatch)
    fk.outcome.update({"FSAFE000001": "FAILED_SAFE", "FVISIBLE001": "FAILED_VISIBLE"})
    e, br = engine(tmp_path, [(P1, CH[0])], tam, upload_retry_failed_safe=1)
    br.start()
    for vid in ("FSAFE000001", "FVISIBLE001"):
        f = tmp_path / f"{vid}_360p.mp4"
        f.write_bytes(b"q")
        br.submit({"vid": vid, "ch": CH[0], "path": str(f), "size": 1})
    assert wait_job(br, "FSAFE000001:" + P1, states=("FAILED",), t=10)["state"] == "FAILED"
    assert wait_job(br, "FVISIBLE001:" + P1)["state"] == "FAILED_VISIBLE"
    assert len([u for u in fk.uploads if "FSAFE" in u[1]]) == 2       # thử lại 1 lần (chắc chắn chưa bấm)
    assert len([u for u in fk.uploads if "FVISIBLE" in u[1]]) == 1    # đã bấm + lỗi → KHÔNG thử lại
    br.stop(wait_s=2)


def test_dry_run_never_clicks_post(tmp_path, tam, monkeypatch):
    fk = FakeTikTok(tam, monkeypatch)
    e, br = engine(tmp_path, [(P1, CH[0])], tam, tiktok_dry_run=True)
    br.start()
    f = tmp_path / "DRYRUN00001_360p.mp4"
    f.write_bytes(b"r")
    br.submit({"vid": "DRYRUN00001", "ch": CH[0], "path": str(f), "size": 1})
    assert wait_job(br, "DRYRUN00001:" + P1)["state"] == "DRY_RUN_DONE"
    j = Path(os.environ["TAM_JOURNAL"])
    assert not j.exists() or "POSTING" not in j.read_text(encoding="utf-8")
    br.stop(wait_s=2)


# ------------------------------------------------------------------ 17: STOP trong lúc đang đăng
def test_stop_during_upload_lets_it_finish_and_accepts_no_new_jobs(tmp_path, tam, monkeypatch):
    fk = FakeTikTok(tam, monkeypatch, upload_s=0.8)
    e, br = engine(tmp_path, [(P1, CH[0])], tam)
    br.start()
    f = tmp_path / "STOPVIDEO01_360p.mp4"
    f.write_bytes(b"s")
    br.submit({"vid": "STOPVIDEO01", "ch": CH[0], "path": str(f), "size": 1})
    end = time.time() + 3
    while time.time() < end and not fk.uploads:
        time.sleep(0.02)
    br.stop(wait_s=5)                                        # STOP giữa lúc đang đăng
    assert br.db.one("SELECT state FROM upload_jobs WHERE job_id=?", ("STOPVIDEO01:" + P1,))["state"] == "CONFIRMED"
    assert br.submit({"vid": "AFTERSTOP01", "ch": CH[0], "path": str(f), "size": 1}) == []
    with tam.drivers_lock:
        assert not tam.drivers[P1].dead                      # KHÔNG đóng trình duyệt


# ------------------------------------------------------------------ 18: Excel bị khoá không chặn đăng
@need_ff
def test_excel_locked_does_not_block_tiktok(tmp_path, tam, monkeypatch, gvs, media):
    fk = FakeTikTok(tam, monkeypatch)
    e, br = engine(tmp_path, [(P1, CH[0])], tam)
    monkeypatch.setattr(e.resolver, "resolve", lambda v, deadline_s=2.5: T.plan_prog(gvs, media, v))
    (tmp_path / "~$check.xlsx").write_text("x")
    e.start()
    br.start()
    e.on_event(CH[0], "XLLOCKED001", "WEBSUB", T.TS)
    assert wait_job(br, "XLLOCKED001:" + P1)["state"] == "CONFIRMED"
    assert br.db.one("SELECT state FROM excel_jobs WHERE video_id='XLLOCKED001'")["state"] != "DONE"
    e.stop()


# ------------------------------------------------------------------ chỉ video MỚI khi bật tích hợp (không đăng hàng loạt video cũ)
@need_ff
def test_old_downloads_are_not_posted_when_integration_turns_on(tmp_path, tam, monkeypatch, gvs, media):
    fk = FakeTikTok(tam, monkeypatch)
    make_excel(tmp_path / "check.xlsx", [(P1, CH[0])])
    e0 = T.make_engine(tmp_path, [CH[0]], keep_excel=True)
    monkeypatch.setattr(e0.resolver, "resolve", lambda v, deadline_s=2.5: T.plan_prog(gvs, media, v))
    e0.start(websub=False)
    e0.on_event(CH[0], "OLDVIDEO001", "WEBSUB", T.TS)               # tải khi CHƯA bật tích hợp
    assert T.wait_state(e0, "OLDVIDEO001")["state"] == "DONE"
    e0.stop()
    e, br = engine(tmp_path, [(P1, CH[0])], tam)
    e.start(websub=False)
    br.start()
    time.sleep(0.6)
    assert not fk.uploads and not br.db.one("SELECT 1 FROM upload_jobs WHERE video_id='OLDVIDEO001'")
    e.stop()


@need_ff
def test_downloads_use_part_name_then_atomic_rename_when_integrated(tmp_path, tam, monkeypatch, gvs, media):
    fk = FakeTikTok(tam, monkeypatch)
    e, br = engine(tmp_path, [(P1, CH[0])], tam)
    seen = []
    real = ws.download_single

    def spy(url, dest, info, cancel=None):
        seen.append(Path(dest).name)
        return real(url, dest, info, cancel)
    monkeypatch.setattr(ws, "download_single", spy)
    monkeypatch.setattr(e.resolver, "resolve", lambda v, deadline_s=2.5: T.plan_prog(gvs, media, v))
    e.start(websub=False)
    br.start()
    e.on_event(CH[0], "PARTNAME001", "WEBSUB", T.TS)
    r = T.wait_state(e, "PARTNAME001")
    assert seen == [".PARTNAME001.part.mp4"] and r["state"] == "DONE" and Path(r["file"]).name == "PARTNAME001_360p.mp4"
    assert wait_job(br, "PARTNAME001:" + P1)["state"] == "CONFIRMED"
    e.stop()


@need_ff
def test_title_named_file_reaches_uploader_exactly_and_attach_is_logged(tmp_path, tam, monkeypatch, gvs, media):
    """Tên file = tiêu đề công khai (lọc ký tự cấm), Video ID giữ ở cuối; uploader nhận ĐÚNG đường dẫn cuối đã xác minh;
    tiêu đề có chữ '.Part' không bị coi là file tạm."""
    fk = FakeTikTok(tam, monkeypatch)
    e, br = engine(tmp_path, [(P1, CH[0])], tam)
    monkeypatch.setattr(e.resolver, "resolve", lambda v, deadline_s=2.5: dict(T.plan_prog(gvs, media, v), title="KHÔNG dùng"))
    e.start(websub=False)
    br.start()
    e.on_event(CH[0], "TITLEVID001", "WEBSUB", T.TS, title='Vol.Part 2: "母が決めた" ルール?')
    r = T.wait_state(e, "TITLEVID001")
    j = wait_job(br, "TITLEVID001:" + P1)
    assert r["state"] == "DONE" and j["state"] == "CONFIRMED", (r["error"], j["reason"])
    name = 'Vol.Part 2： ＂母が決めた＂ ルール？_TITLEVID001.mp4'
    assert Path(r["file"]).name == name and Path(r["file"]).is_file()
    assert fk.names == [name] and fk.uploads[0][1] == r["file"] == j["path"]
    assert any("DOWNLOAD_DONE_TO_TIKTOK_ATTACH=" in l and name in l for l in e._logs)
    assert any("DETECTION_TO_DOWNLOAD_DONE TITLEVID001" in l for l in e._logs)
    e.stop()
