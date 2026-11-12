# -*- coding: utf-8 -*-
"""Lớp mạng nhanh (ws360_net): đo DNS/TCP/TLS/SERVER_WAIT trên HTTPS THẬT (máy chủ local, chứng chỉ tự ký),
dùng lại kết nối, giữ ấm, không làm hỏng kiểm tra hostname TLS, và rollback 'legacy'."""
import shutil
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import urllib3

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import ws360_net as NET  # noqa: E402
import test_ws360 as T  # noqa: E402

ws = T.ws
need_openssl = pytest.mark.skipif(not shutil.which("openssl"), reason="cần openssl")


@pytest.fixture(scope="module")
def tls_server(tmp_path_factory):
    d = tmp_path_factory.mktemp("tls")
    key, crt = d / "k.pem", d / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(crt), "-days", "1",
                    "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"], check=True, capture_output=True)
    hits = {"n": 0, "conns": set()}

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            return

        def do_GET(self):
            hits["n"] += 1
            hits["conns"].add(self.client_address)
            if self.path.startswith("/slow"):
                time.sleep(0.25)                                  # SERVER_WAIT có chủ đích
            body = b"" if self.path == "/generate_204" else b"x" * 200_000
            self.send_response(204 if self.path == "/generate_204" else 200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(str(crt), str(key))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"port": srv.server_address[1], "ca": str(crt), "hits": hits}
    srv.shutdown()


def fast_pool(ca):
    pm = NET.make_pool("fast")
    pm.connection_pool_kw.update(ca_certs=ca, cert_reqs="CERT_REQUIRED")
    return pm


@need_openssl
def test_phases_split_and_connection_reuse(tls_server):
    pm = fast_pool(tls_server["ca"])
    url = f"https://localhost:{tls_server['port']}/slow"
    with NET.trace() as t1:
        r = pm.request("GET", url, preload_content=True, retries=False)
    a = t1.d
    assert r.status == 200 and a["new_conn"] and a["dns_ms"] is not None and a["tcp_ms"] is not None and a["tls_ms"] > 0
    assert 230 <= a["server_wait_ms"] <= 1500                            # máy chủ ngủ 250 ms → nằm ở SERVER_WAIT, không ở TLS
    with NET.trace() as t2:
        pm.request("GET", url, preload_content=True, retries=False)
    assert t2.d["new_conn"] is False and t2.d.get("tls_ms") is None     # lần 2: DÙNG LẠI kết nối, không bắt tay lại
    with NET.trace() as t3:
        pm.request("GET", f"https://localhost:{tls_server['port']}/x", preload_content=True, retries=False)
    assert t3.d["new_conn"] is False


@need_openssl
def test_tls_hostname_verification_still_enforced(tls_server):
    pm = fast_pool(tls_server["ca"])
    with pytest.raises(urllib3.exceptions.SSLError):
        try:
            pm.request("GET", f"https://127.0.0.1:{tls_server['port']}/x", retries=False)   # chứng chỉ cho 'localhost', không cho IP
        except urllib3.exceptions.MaxRetryError as e:
            raise e.reason


@need_openssl
def test_warmer_keeps_connection_open_so_next_request_skips_handshake(tls_server):
    pm = fast_pool(tls_server["ca"])
    host = f"localhost:{tls_server['port']}"
    w = NET.NetWarmer(lambda: pm, print, innertube_hosts=(host,), k_innertube=2, media_max=0, interval=60)
    w.warm_now()
    assert w.stats["pings"] == 2 and w.stats["errors"] == 0
    with NET.trace() as t:
        pm.request("GET", f"https://{host}/slow", preload_content=True, retries=False)
    assert t.d["new_conn"] is False                                     # video mới tới: kết nối đã ấm sẵn


def test_media_host_learning_and_sibling_dns_prefetch(monkeypatch):
    seen = []
    monkeypatch.setattr(NET.DNS, "prefetch", lambda h, port=443: seen.append(h))
    w = NET.NetWarmer(lambda: None, print, innertube_hosts=("www.youtube.com",), media_max=2, siblings=3)
    for h in ("rr4---sn-abc123.googlevideo.com", "rr5---sn-abc123.googlevideo.com", "rr6---sn-abc123.googlevideo.com"):
        w.learn(f"https://{h}/videoplayback?sig=SECRET")
    w.learn("https://evil.example.com/x")                                # không phải máy chủ video → bỏ qua
    time.sleep(0.2)
    assert [h for h, _ in w.targets()] == ["www.youtube.com", "rr5---sn-abc123.googlevideo.com", "rr6---sn-abc123.googlevideo.com"]
    assert "rr1---sn-abc123.googlevideo.com" in seen and "rr3---sn-abc123.googlevideo.com" in seen


def test_dns_cache_ttl():
    c = NET.DNSCache(ttl=0.2)
    assert c.resolve("localhost", 80)[1] is False and c.resolve("localhost", 80)[1] is True
    time.sleep(0.25)
    assert c.resolve("localhost", 80)[1] is False


def test_legacy_rollback_restores_plain_pool():
    try:
        assert ws.set_net_mode("legacy") == "legacy"
        p = ws.pool()
        assert type(p).__name__ == "PoolManager" and p.pool_classes_by_scheme["https"].__name__ == "HTTPSConnectionPool"
        assert isinstance(ws.net_trace(), ws._NoTrace)
        assert ws.set_net_mode("fast") == "fast"
        assert ws.pool().pool_classes_by_scheme["https"] is NET.TimedHTTPSPool
    finally:
        ws.set_net_mode("fast")


def test_innertube_client_config_loaded_once(monkeypatch):
    ws._CLIENT_CACHE.clear()
    calls = []
    real = ws._innertube_client_load
    monkeypatch.setattr(ws, "_innertube_client_load", lambda n: calls.append(n) or real(n))
    for _ in range(5):
        ws._innertube_client("android")
    assert calls == ["android"]


from test_ws360 import gvs, media  # noqa: E402,F401


@T.need_ff
def test_bench_dl_runs_every_variant_with_fresh_download_each_time(tmp_path, gvs, media, monkeypatch):
    T.make_excel(tmp_path / "check.xlsx", T.CH[:1])
    calls = []
    monkeypatch.setattr(ws.Resolver, "resolve", lambda self, v, deadline_s=2.5: calls.append(v) or dict(T.plan_prog(gvs, media, v), title="t"))
    monkeypatch.setattr(NET.NetWarmer, "warm_now", lambda self: None)
    cfg = dict(ws.DEFAULT_CONFIG, retry_schedule_sec=[0.1])
    res = ws.bench_dl(cfg, tmp_path, ["BENCHDL0001", "BENCHDL0002"], reps=2, variants=["legacy", "fast_cold", "warm_all", "warm_all_r2"])
    rows = res["rows"]
    assert len(rows) == 2 * 2 * 4 and all(r["result"] == "DONE" for r in rows), [r["error"] for r in rows]
    assert len(calls) == 16                                                    # link MỚI mỗi lượt
    assert len(gvs.requests_for("/vp/BENCHDL0001")) >= 8                       # tải lại thật mỗi lượt (không dùng file có sẵn)
    s = res["summary"]
    assert set(s) == {"legacy", "fast_cold", "warm_all", "warm_all_r2"} and s["legacy"]["n"] == 4
    md = (tmp_path / "BENCH_DL.md").read_text(encoding="utf-8")
    assert "| legacy |" in md and "≤1000 ms" in md and (tmp_path / "BENCH_DL.csv").exists()
    assert not (tmp_path / "DOWNLOADS_BENCH").exists() and ws._NET_MODE == "fast"
