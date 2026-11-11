# -*- coding: utf-8 -*-
"""V9.6 — giảm TTFB (nhớ host sau 302, kết nối nóng) và truyền (Range N, HTTP/2) cho 360p. Cờ V96_*."""
import time
from pathlib import Path

import pytest

from mock_gvs import MockGVS, make_payload, sha256

MB = 1024 * 1024


@pytest.fixture(autouse=True)
def _reset(v94):
    with v94._V96_LOCK:
        v94._V96_REDIRECT.clear(); v94._V96_WARM_HOSTS.clear()
    v94._MN_RACE_BAD_UNTIL[0] = 0.0
    yield


def _get(v94, url, tmp, name, expected, info=None):
    info = {} if info is None else info
    ok = v94._download_direct_single_get(url, tmp, name, lambda m: None, 1, expected, info=info)
    return ok, info


def test_redirect_remembered_and_skipped(v94, monkeypatch, tmp_path):
    data = make_payload(2 * MB, seed=1)
    with MockGVS() as g:
        real = g.add("/videoplayback/real", data)
        main = g.add("/videoplayback/main", mode="redirect", to=real, hdr_delay=0.15)
        # host khác nhau? mock chỉ có 127.0.0.1 → giả host qua hostname khác: dùng 'localhost' cho đích
        real_lh = real.replace("127.0.0.1", "localhost")
        g.files["/videoplayback/main"].kw["to"] = real_lh
        monkeypatch.setattr(v94, "_V96_HOST_SUFFIX", "")
        ok, i1 = _get(v94, main, tmp_path, "a.bin", len(data))
        assert ok and i1.get("redirect_hops") == 1 and not i1.get("redirect_skipped")
        assert i1.get("first_resp_ms") >= 140 and i1.get("tls_ms") is not None
        # lần 2: GET thẳng host đích (mock: cùng path /videoplayback/main trên localhost không tồn tại → 404 → về URL gốc)
        ok, i2 = _get(v94, main, tmp_path, "b.bin", len(data))
        assert ok and sha256((tmp_path / "b.bin").read_bytes()) == sha256(data)
        assert i2.get("redirect_skipped") is False          # thử host đích, bị 404 → quay về gốc, vẫn thành công
        assert v94._V96_STATS["redirect_skip_fail"] >= 1


def test_predicted_url_swaps_host_only(v94):
    u = "https://rr1---sn-aaa.googlevideo.com/videoplayback?a=1&sig=S"
    v94._v96_remember_redirect(u, "https://rr3---sn-bbb.googlevideo.com/videoplayback?a=1&sig=S")
    p = v94._v96_predicted_url(u)
    assert p == "https://rr3---sn-bbb.googlevideo.com/videoplayback?a=1&sig=S"
    assert v94._v96_predicted_url("https://rr9---sn-zzz.googlevideo.com/x") is None
    with v94._V96_LOCK:
        v94._V96_REDIRECT["rr1---sn-aaa.googlevideo.com"] = ("rr3---sn-bbb.googlevideo.com", time.time() - 10_000)
    assert v94._v96_predicted_url(u) is None                  # hết TTL


def test_host_pool_flag_off(v94, monkeypatch):
    monkeypatch.setattr(v94, "V96_HOST_POOL", False)
    u = "https://rr1---sn-aaa.googlevideo.com/v"
    v94._v96_remember_redirect(u, "https://rr3---sn-bbb.googlevideo.com/v")
    assert v94._v96_predicted_url(u) is None


def test_preconnect_measures_and_pools(v94):
    with MockGVS() as g:
        pool = v94._get_direct_media_pool()
        hp = pool.connection_from_host("127.0.0.1", g.port, "http")
        n0 = hp.num_connections
        # preconnect dùng https:443 trong code → với mock http chỉ kiểm DNS; đo riêng bằng cách gọi socket
        d = v94._v96_preconnect(f"http://127.0.0.1:{g.port}/x", pool, timeout=0.5)
        assert d["ok"] and d["dns_ms"] is not None and d["tls_ms"] is not None
        assert hp.num_connections == n0 + 1
        d2 = v94._v96_preconnect(f"http://127.0.0.1:{g.port}/x", pool, timeout=0.5)
        assert d2["tls_ms"] == 0.0 and hp.num_connections == n0 + 1      # kết nối nóng được dùng lại


@pytest.mark.parametrize("n", [2, 4, 8])
def test_range_n_override_and_fallback(v94, tmp_path, n):
    data = make_payload(6 * MB, seed=n)
    with MockGVS() as g:
        url = g.add("/videoplayback/r", data, rate=1_500_000)
        info = {"workers_override": n}
        ok = v94._download_http_range_turbo(url, tmp_path, "r.bin", lambda m: None, 1, len(data), "video", info=info)
        assert ok and info["workers_initial"] == n and sha256((tmp_path / "r.bin").read_bytes()) == sha256(data)
        bad = g.add("/videoplayback/bad", mode="status", code=403)
        info = {"workers_override": n}
        assert not v94._download_http_range_turbo(bad, tmp_path, "x.bin", lambda m: None, 1, 6 * MB, "video", info=info)
        assert info["fail_reason"] == "HTTP_403" and len(g.requests_for("/videoplayback/bad")) <= n


@pytest.mark.skipif(not pytest.importorskip("httpx", reason="httpx"), reason="httpx")
@pytest.mark.parametrize("n", [1, 4])
def test_h2_client_path_with_redirect(v94, tmp_path, n):
    """httpx (mock chỉ nói HTTP/1.1 → http_version=HTTP/1.1; đường code h2 vẫn chạy): 302 + N Range + kiểm size."""
    data = make_payload(3 * MB, seed=7)
    with MockGVS() as g:
        real = g.add("/videoplayback/real", data)
        main = g.add("/videoplayback/main", mode="redirect", to=real)
        info = {}
        ok = v94._v96_download_h2(main, tmp_path, "h.bin", lambda m: None, 1, len(data), n, info)
        assert ok, info
        assert sha256((tmp_path / "h.bin").read_bytes()) == sha256(data)
        assert info["engine"] == f"h2x{n}" and info["http_version"] in ("HTTP/1.1", "HTTP/2")
        bad = g.add("/videoplayback/bad", mode="status", code=429)
        info = {}
        assert not v94._v96_download_h2(bad, tmp_path, "h2.bin", lambda m: None, 1, len(data), n, info)
        assert info["fail_reason"] == "HTTP_429"


def test_h2_without_httpx_returns_false(v94, monkeypatch, tmp_path):
    monkeypatch.setattr(v94, "_v96_h2_client", lambda: None)
    info = {}
    assert not v94._v96_download_h2("http://127.0.0.1:1/x", tmp_path, "z.bin", lambda m: None, 1, 100, 2, info)
    assert info["fail_reason"] == "no_httpx"


# ---- pipeline-level: 360 flow với Range N / H2 / fallback về 1-GET ----
import shutil, subprocess  # noqa: E402
from test_v95_360p import _run, prog18  # noqa: E402,F401

need_ff = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="cần ffmpeg")


@need_ff
@pytest.mark.parametrize("rn", [2, 4])
def test_360_flow_range_n(v94, monkeypatch, tmp_path, prog18, rn):
    monkeypatch.setattr(v94, "V96_RANGE_N", rn)
    w, master, st, calls = _run(v94, monkeypatch, tmp_path, prog18[120], 120)
    assert st == "DONE" and w._v94["kpi"]["engine"] == f"range{rn}"
    assert any(f"[RANGE-TURBO] 🚀 START VIDEO" in l and f"workers={rn}" in l for l in master.logs)


@need_ff
def test_360_flow_range_403_falls_back_to_1get_same_url(v94, monkeypatch, tmp_path, prog18):
    monkeypatch.setattr(v94, "V96_RANGE_N", 4)
    calls = {"range": 0, "get": 0}
    orig_get = v94._download_direct_single_get

    def fake_range(url, *a, **k):
        calls["range"] += 1
        k.get("info", {}).update({"fail_reason": "HTTP_403"})
        return False

    def spy_get(url, *a, **k):
        calls["get"] += 1
        return orig_get(url, *a, **k)
    monkeypatch.setattr(v94, "_download_http_range_turbo", fake_range)
    monkeypatch.setattr(v94, "_download_direct_single_get", spy_get)
    w, master, st, _ = _run(v94, monkeypatch, tmp_path, prog18[120], 120)
    assert st == "DONE" and calls == {"range": 1, "get": 1}
    assert w._v94["kpi"]["engine"] == "range4→1get" and w._v94["kpi"]["range_fail"] == "HTTP_403"


@need_ff
def test_360_flow_h2_then_fallback(v94, monkeypatch, tmp_path, prog18):
    monkeypatch.setattr(v94, "V96_HTTP2", True)
    monkeypatch.setattr(v94, "V96_RANGE_N", 2)
    w, master, st, _ = _run(v94, monkeypatch, tmp_path, prog18[120], 120)
    assert st == "DONE" and w._v94["kpi"]["engine"] == "h2x2"
    monkeypatch.setattr(v94, "_v96_h2_client", lambda: None)
    d2 = tmp_path / "b"; d2.mkdir()
    w2, m2, st2, _ = _run(v94, monkeypatch, d2, prog18[120], 120)
    assert st2 == "DONE" and w2._v94["kpi"]["engine"] == "h2x2→1get" and w2._v94["kpi"]["h2_fail"] == "no_httpx"


@need_ff
def test_360_flow_flags_off_is_v95(v94, monkeypatch, tmp_path, prog18):
    monkeypatch.setattr(v94, "V96_RANGE_N", 0); monkeypatch.setattr(v94, "V96_HTTP2", False)
    monkeypatch.setattr(v94, "V96_HOST_POOL", False)
    w, master, st, _ = _run(v94, monkeypatch, tmp_path, prog18[120], 120)
    assert st == "DONE" and w._v94["kpi"]["engine"] == "1get" and "redirect_skipped" not in w._v94["kpi"]


def test_mn_hosts_and_preconnect(v94, monkeypatch):
    u = "https://rr3---sn-aaa.googlevideo.com/videoplayback?mn=sn-aaa%2Csn-bbb%2Csn-ccc&sig=S"
    assert v94._v96_mn_hosts(u) == ["rr3---sn-bbb.googlevideo.com", "rr3---sn-ccc.googlevideo.com"]
    assert v94._v96_mn_hosts("https://x.y/z") == []
    started = []
    monkeypatch.setattr(v94, "_v96_preconnect", lambda url, pool=None: started.append(url))
    hs = v94._v96_mn_preconnect(u)
    time.sleep(0.1)
    assert hs and sorted(started) == ["https://rr3---sn-bbb.googlevideo.com/", "https://rr3---sn-ccc.googlevideo.com/"]
    monkeypatch.setattr(v94, "V96_MN_PRECONNECT", False)
    assert v94._v96_mn_preconnect(u) == []
