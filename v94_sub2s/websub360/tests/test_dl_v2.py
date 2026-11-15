# -*- coding: utf-8 -*-
"""TỐI ƯU TẢI V2 — mỗi hạng mục có test riêng. Máy chủ video GIẢ: MockGVSHosts (TLS thật, nhiều tên *.googlevideo.com trên
1 cổng loopback, 302 giữa các host, mn=, server_wait, lỗi theo client/host) + MockGVS cũ (HTTP). KHÔNG phải số YouTube thật."""
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest
import urllib3

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[1] / "tests"))
import test_ws360 as T  # noqa: E402
from test_ws360 import gvs, media  # noqa: E402,F401
import ws360_net as NET  # noqa: E402
from mock_gvs import MockGVSHosts, install_dns, make_cert  # noqa: E402

ws = T.ws
need_openssl = pytest.mark.skipif(not shutil.which("openssl"), reason="cần openssl")
A1, A2, A3, A4 = "rr1---sn-aaa.googlevideo.com", "rr2---sn-aaa.googlevideo.com", "rr3---sn-aaa.googlevideo.com", "rr4---sn-aaa.googlevideo.com"
B1, B4 = "rr1---sn-bbb.googlevideo.com", "rr4---sn-bbb.googlevideo.com"
C1, C3 = "rr1---sn-ccc.googlevideo.com", "rr3---sn-ccc.googlevideo.com"


@pytest.fixture(scope="module")
def cert(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("cần openssl")
    return make_cert(tmp_path_factory.mktemp("gvscert"))


@pytest.fixture()
def hosts(cert):
    undo = install_dns(NET)
    with MockGVSHosts(*cert) as m:
        yield m
    undo()


@pytest.fixture(autouse=True)
def clean_opts():
    saved = dict(ws.NET_OPTS)
    with ws._RDR_LOCK:
        ws._RDR_HOST.clear()
        ws._RDR_SN.clear()
    ws._HEDGE_BAN.clear()
    yield
    ws.set_net_opts(saved)
    _rebuild()


def _rebuild():
    with ws._POOL_LOCK:
        if ws._POOL is not None:
            ws._POOL.clear()
        ws._POOL = None


def use(cert, **flags):
    ws.set_net_mode("fast")
    ws.set_net_opts(dict(ws.NET_OPTS_OFF, **flags))
    _rebuild()
    ws.pool().connection_pool_kw.update(ca_certs=cert[0], cert_reqs="CERT_REQUIRED")


# ------------------------------------------------------------------ #1 SSLContext dùng chung + socket options
@need_openssl
def test_shared_ssl_context_created_once_for_10_new_connections(hosts, cert, monkeypatch):
    import urllib3.connection as uc
    import urllib3.util.ssl_ as us
    calls = []
    real = us.create_urllib3_context

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)
    monkeypatch.setattr(uc, "create_urllib3_context", counting)
    monkeypatch.setattr(us, "create_urllib3_context", counting)
    hosts.add("/p", b"x" * 1000)
    out = {}
    for shared in (True, False):
        NET.reset_shared_ssl_context()
        calls.clear()
        use(cert, net_shared_ssl=shared)
        for i in range(10):
            r = ws.pool().request("GET", hosts.url(f"rr{i + 1}---sn-sss.googlevideo.com", "/p"), retries=False)
            assert r.status == 200
        out[shared] = len(calls)
    assert out == {True: 1, False: 10}                                     # bản cũ: 1 context MỚI mỗi kết nối


@need_openssl
def test_shared_ssl_still_verifies_certificate_hostname(hosts, cert):
    use(cert, net_shared_ssl=True)
    hosts.add("/p", b"x")
    with pytest.raises(urllib3.exceptions.SSLError):
        try:
            ws.pool().request("GET", f"https://127.0.0.1:{hosts.port}/p", retries=False)   # chứng chỉ không có IP này
        except urllib3.exceptions.MaxRetryError as e:
            raise e.reason


def test_socket_options_keep_nodelay_and_add_rcvbuf():
    import socket
    o = NET.socket_opts(4)
    assert (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1) in o and (socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024) in o
    assert NET.socket_opts(0) == [(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)]
    ws.set_net_opts(dict(ws.NET_OPTS_OFF, net_shared_ssl=True, net_rcvbuf_mb=4))
    _rebuild()
    kw = ws.pool().connection_pool_kw
    assert kw.get("ssl_context") is NET.shared_ssl_context() and (socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024) in kw["socket_options"]
    ws.set_net_opts(ws.NET_OPTS_OFF)
    _rebuild()
    assert "ssl_context" not in ws.pool().connection_pool_kw and "socket_options" not in ws.pool().connection_pool_kw   # cờ tắt = như cũ


# ------------------------------------------------------------------ #2 nhớ máy chủ sau 302
@need_openssl
def test_redirect_memory_skips_302_and_falls_back_on_wrong_guess(hosts, cert, tmp_path):
    hosts.host_rules[A1] = {"redirect_to": B1}
    hosts.host_rules[A2] = {"redirect_to": B1}
    data = {i: bytes([i]) * (50_000 + i) for i in range(1, 5)}
    for i, d in data.items():
        hosts.add(f"/v{i}", d)
    use(cert, net_redirect_memory=True)
    info = {}
    ws.download_single(hosts.url(A1, "/v1"), tmp_path / "1", info)
    assert info["hops"] == 2 and not info.get("redirect_skip")                     # lần đầu: phải theo 302 (và học)
    info = {}
    ws.download_single(hosts.url(A1, "/v2"), tmp_path / "2", info)
    assert info["hops"] == 1 and info["redirect_skip"] == "host" and hosts.reqs[(A1, "/v2")] == 0
    info = {}
    ws.download_single(hosts.url(A2, "/v3"), tmp_path / "3", info)                 # rr2 chưa gặp, cùng cụm sn-aaa
    assert info["redirect_skip"] == "cluster" and hosts.reqs[(A2, "/v3")] == 0
    hosts.host_rules[A1] = {"redirect_to": C1}                                       # GVS đổi đích; đích cũ từ chối
    hosts.host_status[(B1, "/v4")] = 403
    info = {}
    n = ws.download_single(hosts.url(A1, "/v4"), tmp_path / "4", info)
    assert n == len(data[4]) and (tmp_path / "4").read_bytes() == data[4]           # quay về URL gốc NGAY, cùng lần thử
    assert info["redirect_skip_fail"].startswith("HttpStatus") and info["hops"] == 2 and info["host"] == C1.split(":")[0]
    assert ws.redirect_predict(hosts.url(A1, "/v9"))[0].split("/")[2].startswith(C1)   # đã học lại đích mới


@need_openssl
def test_redirect_memory_off_is_old_behaviour(hosts, cert, tmp_path):
    hosts.host_rules[A1] = {"redirect_to": B1}
    hosts.add("/o1", b"1" * 1000)
    hosts.add("/o2", b"2" * 1000)
    use(cert)
    for p in ("/o1", "/o2"):
        info = {}
        ws.download_single(hosts.url(A1, p), tmp_path / p[1:], info)
        assert info["hops"] == 2 and "redirect_skip" not in info


# ------------------------------------------------------------------ #3 preconnect máy chủ mn=
@need_openssl
@pytest.mark.parametrize("on", [True, False])
def test_mn_preconnect_has_target_connection_ready_before_302(hosts, cert, tmp_path, on):
    hosts.conn_delay = 0.08                                                          # mỗi kết nối mới tốn 80 ms
    hosts.host_rules[A3] = {"redirect_to": C3, "server_wait": (0.3, 0, 0)}
    hosts.add("/m", b"m" * 20_000)
    use(cert, net_mn_preconnect=on)
    info = {}
    ws.download_single(hosts.url(A3, "/m", mn=("sn-aaa", "sn-ccc")), tmp_path / "m", info)
    assert info["hops"] == 2
    if on:
        assert info["mn_preconnect"] == 1 and info["new_conns"] == 1                # chặng 2 (sau 302) dùng kết nối đã mở sẵn
    else:
        assert "mn_preconnect" not in info and info["new_conns"] == 2


# ------------------------------------------------------------------ #4 hedged GET
@need_openssl
def test_hedge_takes_faster_replica_and_opens_at_most_two_sockets(hosts, cert, tmp_path):
    hosts.host_rules[A4] = {"server_wait": (0.8, 0, 0)}
    hosts.host_rules[B4] = {"server_wait": (0.1, 0, 0)}
    hosts.add("/h", b"h" * 200_000)
    use(cert, dl_hedge=True, dl_hedge_delay_ms=120)
    info = {}
    t0 = time.perf_counter()
    n = ws.download_single(hosts.url(A4, "/h", mn=("sn-aaa", "sn-bbb")), tmp_path / "h", info)
    dt = time.perf_counter() - t0
    assert n == 200_000 and info["hedge_win"] == "replica" and dt < 0.6, dt       # ≈ 120 + 100 ms, không phải 800 ms
    time.sleep(0.9)                                                                  # bên thua trả về → bị đóng (không đọc thân)
    assert hosts.handshakes <= 2


@need_openssl
def test_hedge_never_fires_after_fast_403_and_bans_replica_on_403(hosts, cert, tmp_path):
    hosts.add("/h2", b"x" * 1000)
    hosts.add("/h3", b"y" * 1000)
    hosts.host_status[(A4, "/h2")] = 403
    use(cert, dl_hedge=True, dl_hedge_delay_ms=120)
    with pytest.raises(ws.HttpStatus):
        ws.download_single(hosts.url(A4, "/h2", mn=("sn-aaa", "sn-bbb")), tmp_path / "x", {})
    assert hosts.reqs[(B4, "/h2")] == 0                                              # 403 TRƯỚC khi hedge → không bắn kép
    hosts.host_rules[A4] = {"server_wait": (0.3, 0, 0)}
    hosts.host_status[(B4, "/h3")] = 403
    info = {}
    ws.download_single(hosts.url(A4, "/h3", mn=("sn-aaa", "sn-bbb")), tmp_path / "y", info)
    assert info["hedge_win"] == "primary" and any(k.startswith(B4) for k in ws._HEDGE_BAN)   # replica 403 chắc chắn → cấm 10 phút
    info = {}
    hosts.add("/h4", b"z" * 1000)
    ws.download_single(hosts.url(A4, "/h4", mn=("sn-aaa", "sn-bbb")), tmp_path / "z", info)
    assert not info.get("hedge_sent")                                                # replica bị cấm → không bắn kép tới nó nữa


# ------------------------------------------------------------------ #5 link client khác khi 403
@T.need_ff
def test_alt_client_url_used_in_same_attempt_on_403(tmp_path, gvs, media, monkeypatch):
    vid = "ALT403VID01"
    good = gvs.add(f"/vp/{vid}", media["prog"], "ok")
    bad = gvs.add(f"/vp/{vid}_bad", b"", "status", code=403, hdr_delay=0.25)

    def player(self, v, client):
        if client != "android_vr":
            time.sleep(0.05)
        url = bad if client == "android_vr" else good
        return {"client": client, "verdict": "ok", "net": {}, "title": "t",
                "pick": {"kind": "progressive", "audio": None,
                         "video": T.mkfmt(url, 18, 360, 'video/mp4; codecs="avc1, mp4a"', len(media["prog"]))}}
    monkeypatch.setattr(ws.Resolver, "_player", player)
    e = T.make_engine(tmp_path, T.CH[:1], excel_enabled=False)
    e.start(websub=False)
    e.on_event(T.CH[0], vid, "WEBSUB", T.TS)
    r = T.wait_state(e, vid)
    e.stop()
    assert r["state"] == "DONE" and r["attempts"] == 1, r["error"]
    net = __import__("json").loads(r["net"])
    assert net["media"]["fallback_client"] in ("web_embedded", "android")


# ------------------------------------------------------------------ #7 kiểm toàn vẹn MP4 trước ffprobe
@T.need_ff
def test_mp4_quick_check_detects_truncated_empty_and_missing_moov(tmp_path, media):
    good = tmp_path / "g.mp4"
    good.write_bytes(media["prog"])
    ok, why = ws.mp4_quick_check(good)
    assert ok and why.startswith("ftyp") and "moov" in why and "mdat" in why
    cut = tmp_path / "c.mp4"
    cut.write_bytes(media["prog"][:-777])
    assert ws.mp4_quick_check(cut)[0] is False and "cụt" in ws.mp4_quick_check(cut)[1]
    z = tmp_path / "z.mp4"
    z.write_bytes(b"")
    assert ws.mp4_quick_check(z)[0] is False
    nm = tmp_path / "n.mp4"
    nm.write_bytes(media["prog"].replace(b"moov", b"free", 1))
    assert ws.mp4_quick_check(nm) == (False, "thiếu moov")
    t0 = time.perf_counter()
    for _ in range(100):
        ws.mp4_quick_check(good)
    assert (time.perf_counter() - t0) / 100 < 0.002                                  # < 2 ms


@T.need_ff
def test_truncated_download_rejected_before_ffprobe(tmp_path, gvs, media, monkeypatch):
    vid = "TRUNCVID001"
    data = media["prog"][:-3000]
    monkeypatch.setattr(ws.Resolver, "resolve", lambda self, v, deadline_s=2.5: T.plan_prog(gvs, media, v, data=data))
    probed = []
    real = ws.ffprobe_info
    monkeypatch.setattr(ws, "ffprobe_info", lambda p: probed.append(str(p)) or real(p))
    e = T.make_engine(tmp_path, T.CH[:1], excel_enabled=False, retry_schedule_sec=[30])
    e.start(websub=False)
    e.on_event(T.CH[0], vid, "WEBSUB", T.TS)
    assert T.wait_state(e, vid, states=("RETRY_WAIT", "FAILED"), t=10) is not None
    end = time.time() + 5
    while time.time() < end and "kiểm nhanh" not in "\n".join(e._logs):
        time.sleep(0.05)
    e.stop()
    assert "kiểm nhanh" in "\n".join(e._logs) and not [p for p in probed if vid in p or "Tiêu đề" in p or p.endswith(".mp4")]


# ------------------------------------------------------------------ #6 range auto
def test_range_auto_picks_connections_by_size(tmp_path, gvs, monkeypatch):
    seen = {}
    sizes = {"AUTO000001S": 1_000_000, "AUTO000003M": 3_000_000, "AUTO000008L": 8_000_000}

    def resolve(self, v, deadline_s=2.5):
        d = b"\0" * sizes[v]
        url = gvs.add(f"/a/{v}", d, "ok")
        return {"pick": {"kind": "progressive", "audio": None, "video": T.mkfmt(url, 18, 360, 'video/mp4; codecs="avc1, mp4a"', len(d))},
                "source": "MOCK"}
    real = ws.Engine._fetch_one
    monkeypatch.setattr(ws.Resolver, "resolve", resolve)
    monkeypatch.setattr(ws.Engine, "_fetch_one", lambda self, fmt, dest, info, n, cancel=None: seen.setdefault(fmt["clen"], n) and
                        real(self, fmt, dest, info, n, cancel) or real(self, fmt, dest, info, n, cancel))
    monkeypatch.setattr(ws, "ffprobe_info", lambda p: {"ok": True, "audio": True})
    e = T.make_engine(tmp_path, T.CH[:1], excel_enabled=False, range_connections="auto", dl_quick_integrity=False)
    e.start(websub=False)
    for v in sizes:
        e.on_event(T.CH[0], v, "WEBSUB", T.TS)
    for v in sizes:
        assert T.wait_state(e, v)["state"] == "DONE"
    e.stop()
    assert seen == {1_000_000: 1, 3_000_000: 2, 8_000_000: 4}


# ------------------------------------------------------------------ #8 đường nóng
def test_hotpath_chunk_size_follows_flag(tmp_path, gvs, monkeypatch):
    amts = []
    real = urllib3.response.HTTPResponse.stream
    monkeypatch.setattr(urllib3.response.HTTPResponse, "stream", lambda self, amt=2 ** 16, decode_content=None: amts.append(amt) or
                        real(self, amt, decode_content))
    url = gvs.add("/hp", b"q" * 3_000_000, "ok")
    for on, want in ((True, 1024 * 1024), (False, 256 * 1024)):
        ws.set_net_opts(dict(ws.NET_OPTS_OFF, dl_hotpath_v2=on))
        amts.clear()
        assert ws.download_single(url, tmp_path / f"hp{on}", {}) == 3_000_000
        assert amts == [want]


# ------------------------------------------------------------------ cờ tắt hết = đúng bản trước
def test_all_flags_off_takes_exactly_the_old_path(tmp_path, gvs, monkeypatch):
    for fn in ("redirect_predict", "mn_preconnect", "_hedged_follow", "mp4_quick_check"):
        monkeypatch.setattr(ws, fn, lambda *a, **k: (_ for _ in ()).throw(AssertionError(f"{fn} không được gọi khi cờ tắt")))
    ws.set_net_opts(ws.NET_OPTS_OFF)
    _rebuild()
    url = gvs.add("/off", b"o" * 10_000, "ok")
    assert ws.download_single(url, tmp_path / "off", {}) == 10_000
    assert "ssl_context" not in ws.pool().connection_pool_kw
