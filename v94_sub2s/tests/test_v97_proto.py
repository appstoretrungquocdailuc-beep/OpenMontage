# -*- coding: utf-8 -*-
"""V9.7 — V97_PROTO: h1 (mặc định = V9.6) | h2 (httpx) | h3 (curl --http3). Lỗi h2/h3 → h1 ngay trong job."""
import json
import shutil
import sys
import textwrap
import time
from pathlib import Path

import pytest

from mock_gvs import MockGVS, make_payload, sha256
from test_v95_360p import _run, prog18  # noqa: F401

MB = 1024 * 1024
need_ff = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="cần ffmpeg")


@pytest.fixture(autouse=True)
def _reset_curl(v94):
    v94._V97_CURL.update({"path": None, "h3": None, "ver": ""})
    yield
    v94._V97_CURL.update({"path": None, "h3": None, "ver": ""})


def _fake_curl(tmp_path, body_src, http3=True, exit_code=0):
    """curl giả: -V in Features HTTP3; -o ghi body; -w in JSON write-out."""
    p = tmp_path / "curl_fake"
    p.write_text("#!" + sys.executable + "\n" + textwrap.dedent(f"""
        import sys, json, shutil
        a = sys.argv[1:]
        if "-V" in a:
            print("curl 8.10.0 (x86_64-w64-mingw32) libcurl/8.10.0 ngtcp2/1.7.0")
            print("Features: alt-svc AsynchDNS HSTS {"HTTP3 " if http3 else ""}HTTPS-proxy IPv6 SSL")
            sys.exit(0)
        out = a[a.index("-o") + 1]
        shutil.copyfile({str(body_src)!r}, out)
        print(json.dumps({{"http_version": "3", "code": 200, "t_namelookup": 0.012, "t_connect": 0.040,
                           "t_appconnect": 0.090, "t_pretransfer": 0.091, "t_redirect": 0.070, "t_start": 0.420,
                           "t_total": 0.700, "size": 5, "redirects": 1}}))
        sys.exit({exit_code})
    """), encoding="utf-8")
    p.chmod(0o755)
    return str(p)


def test_find_curl_detects_http3(v94, monkeypatch, tmp_path):
    body = tmp_path / "b.bin"; body.write_bytes(b"x" * 300_000)
    monkeypatch.setattr(v94, "V97_CURL_PATH", _fake_curl(tmp_path, body, http3=True))
    c = v94._v97_find_curl()
    assert c["h3"] is True and c["path"].endswith("curl_fake") and "ngtcp2" in c["ver"]


def test_find_curl_without_http3(v94, monkeypatch, tmp_path):
    body = tmp_path / "b.bin"; body.write_bytes(b"x" * 10)
    monkeypatch.setattr(v94, "V97_CURL_PATH", _fake_curl(tmp_path, body, http3=False))
    monkeypatch.setattr(v94.shutil, "which", lambda *a, **k: None)
    monkeypatch.setattr(v94, "BASE_DIR", str(tmp_path / "nope"))
    assert v94._v97_find_curl()["h3"] is False
    info = {}
    assert not v94._v97_download_curl_h3("https://x/y", tmp_path, "o.mp4", lambda m: None, 1, 0, info)
    assert info["fail_reason"] == "no_curl_http3"


def test_curl_h3_download_parses_writeout(v94, monkeypatch, tmp_path):
    body = tmp_path / "b.bin"; body.write_bytes(make_payload(300_000, seed=3))
    monkeypatch.setattr(v94, "V97_CURL_PATH", _fake_curl(tmp_path, body))
    info = {}
    logs = []
    ok = v94._v97_download_curl_h3("https://x/y", tmp_path / "t", "o.mp4", logs.append, 1, 300_000, info)
    assert ok and sha256((tmp_path / "t" / "o.mp4").read_bytes()) == sha256(body.read_bytes())
    assert info["http_version"] == "3" and info["dns_ms"] == 12.0 and info["connect_ms"] == 40.0
    assert info["tls_ms"] == 50.0 and info["ttfb_ms"] == 420.0 and info["xfer_ms"] == 280.0 and info["redirect_hops"] == 1
    assert any("[CURL-H3] ✅" in l and "HTTP/3" in l for l in logs)


def test_curl_h3_failure_cleans_and_reports(v94, monkeypatch, tmp_path):
    body = tmp_path / "b.bin"; body.write_bytes(b"x" * 300_000)
    monkeypatch.setattr(v94, "V97_CURL_PATH", _fake_curl(tmp_path, body, exit_code=56))
    info = {}
    assert not v94._v97_download_curl_h3("https://x/y", tmp_path / "t", "o.mp4", lambda m: None, 1, 300_000, info)
    assert info["fail_reason"].startswith("curl_rc56") and not (tmp_path / "t" / "o.mp4").exists()


@need_ff
def test_360_flow_h3_then_fallback_same_job(v94, monkeypatch, tmp_path, prog18):
    monkeypatch.setattr(v94, "V97_PROTO", "h3")
    body = tmp_path / "b.mp4"; shutil.copyfile(prog18[120], body)
    monkeypatch.setattr(v94, "V97_CURL_PATH", _fake_curl(tmp_path, body))
    (tmp_path / "a").mkdir(); (tmp_path / "b").mkdir()
    w, master, st, _ = _run(v94, monkeypatch, tmp_path / "a", prog18[120], 120)
    k = w._v94["kpi"]
    assert st == "DONE" and k["engine"] == "h3curl" and k["proto"] == "h3" and k["http_version"] == "3"
    assert "cpu_pct" in k and "proto=h3/3" in [l for l in master.logs if "KPI-360" in l][0]
    # curl lỗi → về h1 trong cùng job, không retry
    v94._V97_CURL.update({"path": None, "h3": None})
    monkeypatch.setattr(v94, "V97_CURL_PATH", _fake_curl(tmp_path, body, exit_code=28))
    w2, m2, st2, _ = _run(v94, monkeypatch, tmp_path / "b", prog18[120], 120)
    assert st2 == "DONE" and w2._v94["kpi"]["engine"] == "h3curl→1get" and w2._v94["kpi"]["h3_fail"].startswith("curl_rc28")
    assert sum(1 for l in m2.logs if "H3/curl lỗi" in l) == 1


@need_ff
@pytest.mark.parametrize("n", [1, 2, 4])
def test_360_flow_h2_range_n(v94, monkeypatch, tmp_path, prog18, n):
    pytest.importorskip("httpx")
    monkeypatch.setattr(v94, "V97_PROTO", "h2"); monkeypatch.setattr(v94, "V97_H2_RANGE_N", n)
    w, master, st, _ = _run(v94, monkeypatch, tmp_path, prog18[120], 120)
    assert st == "DONE" and w._v94["kpi"]["engine"] == f"h2x{n}" and w._v94["kpi"]["proto"] == "h2"


@need_ff
def test_360_flow_proto_h1_is_v96(v94, monkeypatch, tmp_path, prog18):
    monkeypatch.setattr(v94, "V97_PROTO", "h1")
    w, master, st, _ = _run(v94, monkeypatch, tmp_path, prog18[120], 120)
    assert st == "DONE" and w._v94["kpi"]["engine"] == "1get" and w._v94["kpi"]["proto"] == "h1"


def test_live360_verdict_rule():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import bench_v93 as b
    rows = []
    for cfg, hot, err in (("1get", [1000, 1100, 1200], 0), ("h2x1", [960, 1060, 1160], 0), ("h3", [900, 1300, 1400], 0),
                          ("range4", [500, 600, 700], 1)):
        for i, h in enumerate(hot):
            rows.append({"config": cfg, "success": not (cfg == "range4" and i == 0), "hot_ready_ms": h,
                         "engine": cfg, "fail_reason": "x" if (cfg == "range4" and i == 0) else ""})
    summ = b._live360_rows_to_summary(rows)
    base = next(s for s in summ if s["config"] == "1get")
    ok = [s for s in summ if s["config"] != "1get" and base["hot_ready_ms_p50"] - s["hot_ready_ms_p50"] >= 30
          and base["hot_ready_ms_p90"] - s["hot_ready_ms_p90"] >= 30 and s["errors"] <= base["errors"]]
    assert [s["config"] for s in ok] == ["h2x1"]      # h3: p90 tệ hơn; range4: có lỗi → loại


@need_ff
def test_360_short_video_triggers_ffmpeg_prewarm(v94, monkeypatch, tmp_path, prog18):
    calls = []
    monkeypatch.setattr(v94, "_v94_ff_prewarm", lambda *a, **k: calls.append(a) or {"ffmpeg_ms": 1, "ffprobe_ms": 1, "idle_s": 9})
    with v94._V94_FF_LOCK:
        v94._V94_FF_STATE["last_exit"]["ffmpeg"] = time.perf_counter() - 60
    w, master, st, _ = _run(v94, monkeypatch, tmp_path, prog18[30], 30)
    assert st == "DONE"
    time.sleep(0.2)
    assert calls and w._v94["kpi"].get("prewarm") not in (None, "")
    (tmp_path / "b").mkdir()
    calls.clear()
    with v94._V94_FF_LOCK:
        v94._V94_FF_STATE["last_exit"]["ffmpeg"] = time.perf_counter() - 60
    w2, m2, st2, _ = _run(v94, monkeypatch, tmp_path / "b", prog18[120], 120)   # 60-300s: không cần ffmpeg
    time.sleep(0.2)
    assert st2 == "DONE" and not calls
