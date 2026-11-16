# -*- coding: utf-8 -*-
"""ARIA2 RPC (thử nghiệm) — chạy aria2c THẬT với MockGVS cục bộ (không phải YouTube). Bỏ qua nếu máy không có aria2c."""
import json
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[1] / "tests"))
import test_ws360 as T  # noqa: E402
from test_ws360 import gvs, media  # noqa: E402,F401
import ws360_aria2 as A2  # noqa: E402
from mock_gvs import make_payload  # noqa: E402

ws = T.ws
need_a2 = pytest.mark.skipif(not shutil.which("aria2c"), reason="cần aria2c")


@pytest.fixture(autouse=True)
def _clean():
    ws.reset_dl_v2_state()
    old = ws.ARIA2C
    ws.ARIA2C = shutil.which("aria2c")
    yield
    ws.ARIA2C = old
    ws.reset_dl_v2_state()


@pytest.fixture(scope="module")
def daemon():
    if not shutil.which("aria2c"):
        pytest.skip("cần aria2c")
    d = A2.daemon(shutil.which("aria2c"))
    assert d.start()
    yield d


def test_tuned_split_matches_360pok():
    assert [A2.tuned_split(x) for x in (0, 3 << 20, 5 << 20, 9 << 20, 15 << 20, 101 << 20)] == [16, 8, 8, 12, 12, 16]


@need_a2
def test_download_is_byte_exact_and_reports_real_connections(daemon, gvs, tmp_path):
    data = make_payload(9_500_000, seed=3)
    url = gvs.add("/a2/ok", data, "ok", rate=8_000_000)                    # ~64 Mbit/s mỗi kết nối (giống giới hạn per-connection)
    out = {}
    for n in (1, 4):
        info = {}
        t0 = time.perf_counter()
        assert daemon.download(url, tmp_path / f"f{n}", n, info, expected=len(data), http_exc=ws.HttpStatus) == len(data)
        out[n] = time.perf_counter() - t0
        assert (tmp_path / f"f{n}").read_bytes() == data and info["connections"] >= (1 if n == 1 else 3)
        assert info["ttfb_ms"] is not None and info["aria2_conns_req"] == n and not Path(str(tmp_path / f"f{n}") + ".aria2").exists()
    assert out[4] < out[1] * 0.6                                                  # nhiều kết nối nhanh hơn khi mỗi kết nối bị giới hạn


@need_a2
@pytest.mark.parametrize("code", [403, 429])
def test_403_429_raise_http_status_after_exactly_one_request(daemon, gvs, tmp_path, code):
    url = gvs.add(f"/a2/s{code}", b"", "status", code=code)
    with pytest.raises(ws.HttpStatus) as ei:
        daemon.download(url, tmp_path / "x", 8, {}, expected=1000, http_exc=ws.HttpStatus)
    assert ei.value.code == code and len(gvs.requests_for(f"/a2/s{code}")) == 1   # max-tries=1: aria2 không thử lại dồn dập
    assert not (tmp_path / "x").exists()


@need_a2
def test_wrong_size_and_cancel_leave_no_partial_file(daemon, gvs, tmp_path):
    data = make_payload(3_000_000, seed=4)
    url = gvs.add("/a2/sz", data, "ok")
    with pytest.raises(A2.Aria2Error):
        daemon.download(url, tmp_path / "w", 4, {}, expected=len(data) + 1, http_exc=ws.HttpStatus)
    assert not (tmp_path / "w").exists()
    slow = gvs.add("/a2/slow", data, "ok", rate=500_000)
    ev = threading.Event()
    threading.Timer(0.3, ev.set).start()
    with pytest.raises(InterruptedError):
        daemon.download(slow, tmp_path / "c", 2, {}, expected=len(data), cancel=ev, http_exc=ws.HttpStatus)
    assert not (tmp_path / "c").exists() and not Path(str(tmp_path / "c") + ".aria2").exists()


# ------------------------------------------------------------------ tích hợp Engine (cờ dl_engine)
def _run(tmp_path, gvs, media, monkeypatch, vid, **cfg):
    monkeypatch.setattr(ws.Resolver, "resolve", lambda self, v, deadline_s=2.5: T.plan_prog(gvs, media, v))
    e = T.make_engine(tmp_path, T.CH[:1], excel_enabled=False, **cfg)
    e.start(websub=False)
    e.on_event(T.CH[0], vid, "WEBSUB", T.TS)
    r = T.wait_state(e, vid, states=("DONE", "FAILED", "RETRY_WAIT"), t=20)
    e.stop()
    return e, r


@need_a2
@T.need_ff
def test_engine_aria2_mode_downloads_and_verifies(tmp_path, gvs, media, monkeypatch):
    e, r = _run(tmp_path, gvs, media, monkeypatch, "A2ENGINE001", dl_engine="aria2_rpc", aria2_connections=4)
    assert r["state"] == "DONE", r["error"]
    net = json.loads(r["net"])["media"]
    assert net["engine"] == "aria2_rpc" and net["aria2_conns_req"] == 4
    assert any("aria2 RPC 4 kết nối yêu cầu" in l for l in e._logs) and any("downloader aria2_rpc" in l for l in e._logs)


@T.need_ff
def test_engine_default_http_never_calls_aria2(tmp_path, gvs, media, monkeypatch):
    monkeypatch.setattr(ws, "download_aria2_rpc", lambda *a, **k: (_ for _ in ()).throw(AssertionError("không được gọi")))
    e, r = _run(tmp_path, gvs, media, monkeypatch, "A2DEFAULT01")
    assert r["state"] == "DONE" and json.loads(r["net"])["media"].get("engine") is None


@T.need_ff
def test_engine_aria2_non_http_error_falls_back_to_ws360_downloader(tmp_path, gvs, media, monkeypatch):
    monkeypatch.setattr(ws, "download_aria2_rpc", lambda *a, **k: (_ for _ in ()).throw(A2.Aria2Error("daemon chết")))
    e, r = _run(tmp_path, gvs, media, monkeypatch, "A2FALLBACK1", dl_engine="aria2_rpc")
    assert r["state"] == "DONE"
    assert "daemon chết" in json.loads(r["net"])["media"]["aria2_fallback"]


@need_a2
def test_engine_aria2_429_is_not_retried_by_another_downloader(tmp_path, gvs, media, monkeypatch):
    vid = "A2THROTT429"
    bad = gvs.add(f"/vp/{vid}", b"", "status", code=429)
    monkeypatch.setattr(ws.Resolver, "resolve", lambda self, v, deadline_s=2.5: {"pick": {"kind": "progressive", "audio": None,
                        "video": T.mkfmt(bad, 18, 360, 'video/mp4; codecs="avc1, mp4a"', 5000)}, "source": "MOCK"})
    e = T.make_engine(tmp_path, T.CH[:1], excel_enabled=False, dl_engine="aria2_rpc", retry_schedule_sec=[1, 2])
    e.start(websub=False)
    t0 = time.time()
    e.on_event(T.CH[0], vid, "WEBSUB", T.TS)
    r = T.wait_state(e, vid, states=("RETRY_WAIT", "FAILED"), t=15)
    e.stop()
    assert r["state"] == "RETRY_WAIT" and r["next_try"] - t0 >= 9.5 and len(gvs.requests_for(f"/vp/{vid}")) == 1


def test_engine_aria2_skipped_while_cautious(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(ws, "download_aria2_rpc", lambda *a, **k: called.append(1))
    monkeypatch.setattr(ws, "download_single", lambda url, dest, info, cancel=None: 0)
    eng = T.make_engine(tmp_path, T.CH[:1], excel_enabled=False, dl_engine="aria2_rpc")
    for info in ({"aria2_ok": True, "no_spec": True}, {"aria2_ok": True, "saw_block": 403}, {}):
        eng._fetch_one({"url": "http://x/y", "clen": 1000}, tmp_path / "o", info, 1)
    ws.note_429()
    eng._fetch_one({"url": "http://x/y", "clen": 1000}, tmp_path / "o", {"aria2_ok": True}, 1)
    assert called == []                                                           # sau 403/429 hoặc đang tạm dừng: không dùng aria2


# ------------------------------------------------------------------ bench cùng URL
@need_a2
@T.need_ff
def test_bench_aria2_same_url_hash_checked_report(tmp_path, gvs, media, monkeypatch):
    data = media["prog"]
    cnt = iter(range(1000))

    def resolve(v, deadline_s=2.5):
        url = gvs.add(f"/bench/{v}/{next(cnt)}", data, "ok", rate=4_000_000)
        return {"pick": {"kind": "progressive", "audio": None, "video": T.mkfmt(url, 18, 360, 'video/mp4; codecs="avc1, mp4a"', len(data))},
                "source": "MOCK"}
    cfg = dict(ws.DEFAULT_CONFIG, net_warm=False)
    res = ws.bench_aria2(cfg, tmp_path, ["BENCHA20001"], reps=3, conns=(1, 4), resolve=resolve)
    rows = [r for r in res["rows"] if r["variant"] != "-"]
    assert {r["variant"] for r in rows} == {"ws360", "aria2_1", "aria2_4"} and all(r["result"] == "DONE" for r in rows)
    assert all(r["sha_match"] for r in rows) and {r["pos"] for r in rows if r["variant"] == "aria2_4"} == {0, 1, 2}
    for f in ("BENCH_ARIA2.md", "BENCH_ARIA2_SUMMARY.md", "BENCH_ARIA2.csv"):
        assert (tmp_path / f).exists()
    assert "SHA-256" in (tmp_path / "BENCH_ARIA2_SUMMARY.md").read_text(encoding="utf-8")


def _row(var, rnd, total, result="DONE", size=9_000_000):
    return {"variant": var, "video_id": "V", "round": rnd, "pos": rnd % 3, "result": result, "total_ms": total, "download_ms": total - 60,
            "ttfb_ms": 300, "connections": 1, "ffprobe_ms": 55, "size": size}


def test_bench_aria2_decision_rules(tmp_path):
    import random as _r
    rnd = _r.Random(1)
    rows = []
    for i in range(20):
        rows.append(_row("ws360", i, rnd.gauss(1700, 40)))
        rows.append(_row("aria2_8", i, rnd.gauss(1100, 40)))                       # nhanh hơn rõ, hợp lệ
        rows.append(_row("aria2_12", i, rnd.gauss(1050, 40), result="INVALID" if i == 7 else "DONE"))   # nhanh hơn nhưng 1 file sai
        rows.append(_row("aria2_1", i, rnd.gauss(1705, 40)))                       # như nhau
    out = ws.bench_aria2_report(tmp_path, rows, ["ws360", "aria2_1", "aria2_8", "aria2_12"])
    assert out["aria2_8"]["decision"] == "on" and out["aria2_12"]["decision"] == "off" and out["aria2_1"]["decision"] != "on"
    assert out["best"] == "aria2_8" and out["aria2_8"]["target_hits"] == 20
    s = (tmp_path / "BENCH_ARIA2_SUMMARY.md").read_text(encoding="utf-8")
    assert '"aria2_connections": 8' in s
    out2 = ws.bench_aria2_report(tmp_path, rows, ["ws360", "aria2_8"], stopped_429=True)
    assert out2["aria2_8"]["decision"] == "inconclusive" and out2["best"] is None
