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
    ws.reset_dl_v2_state()
    yield
    ws.set_net_opts(saved)
    ws.set_net_mode("fast")
    ws.reset_dl_v2_state()
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
    assert not info.get("hedge_sent") and hosts.reqs[(A1, "/v4")] == 1                # quay về = đúng 1 GET thường
    # đoán sai → CHẶN đoán cho máy chủ đó VÀ cụm đó 10 phút (request về URL gốc dạy lại cặp cũ/mới, nhưng không đoán nữa)
    assert ws.redirect_predict(hosts.url(A1, "/v9")) == (None, "") and ws.redirect_predict(hosts.url(A2, "/v9")) == (None, "")
    hosts.add("/v5", b"5" * 1000)
    info = {}
    ws.download_single(hosts.url(A1, "/v5"), tmp_path / "5", info)
    assert "redirect_skip" not in info and hosts.reqs[(B1, "/v5")] == 0 and info["hops"] == 2


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


@need_openssl
def test_hedge_also_covers_the_predicted_redirect_target(hosts, cert, tmp_path):
    """Hồi quy (mock bench): URL đã đoán (#2) phải được hedge (#4); trước đây nhánh đoán đúng bỏ qua hedge → đuôi chậm còn nguyên."""
    hosts.host_rules[A1] = {"redirect_to": B1}
    hosts.add("/p1", b"1" * 5000)
    hosts.add("/p2", b"2" * 5000)
    use(cert, net_redirect_memory=True, dl_hedge=True, dl_hedge_delay_ms=120)
    ws.download_single(hosts.url(A1, "/p1", mn=("sn-aaa", "sn-bbb", "sn-ccc")), tmp_path / "1", {})
    hosts.host_rules[B1] = {"server_wait": (0.8, 0, 0)}                              # đích đã học trở nên chậm
    info = {}
    t0 = time.perf_counter()
    ws.download_single(hosts.url(A1, "/p2", mn=("sn-aaa", "sn-bbb", "sn-ccc")), tmp_path / "2", info)
    assert info["redirect_skip"] == "host" and info["hedge_win"] == "replica" and time.perf_counter() - t0 < 0.6
    assert hosts.reqs[(C1, "/p2")] == 1 and hosts.reqs[(A1, "/p2")] == 0               # dự phòng = sn-ccc (không phải máy chủ 302)


@need_openssl
def test_range_parts_never_use_redirect_prediction(hosts, cert, tmp_path):
    """Review vòng 2: mọi đoạn Range gửi cùng lúc → đoán sai/403/429 nhân n lần trước khi kịp biết. Range KHÔNG đoán 302."""
    hosts.host_rules[A1] = {"redirect_to": B1}
    data = bytes(range(256)) * 4000
    hosts.add("/r1", b"x" * 1000)
    hosts.add("/r2", data)
    use(cert, net_redirect_memory=True)
    ws.download_single(hosts.url(A1, "/r1"), tmp_path / "1", {})
    assert ws.redirect_predict(hosts.url(A1, "/r2"))[0]                               # đã học
    info = {}
    assert ws.download_ranges(hosts.url(A1, "/r2"), tmp_path / "2", len(data), 3, info) == len(data)
    assert (tmp_path / "2").read_bytes() == data and hosts.reqs[(A1, "/r2")] == 3 and "redirect_skip" not in info


def test_summary_recommends_group_only_if_it_beats_reference_and_its_predecessor(tmp_path):
    S = lambda m, p9, fail=0: {"total_ms": (m, p9, p9), "n": 20, "fail": fail}
    summ = {"warm_all": S(640, 1990), "v2_ssl": S(650, 1985), "v2_redirect": S(360, 450), "v2_hedge": S(355, 440),
            "v2_full": S(357, 445), "v2_full_auto": S(540, 1900)}
    out = ws.bench_dl_summary(tmp_path, list(summ), summ)                          # không có số từng lượt → so điểm (như cũ)
    assert out["v2_ssl"]["recommend_on"] is False                                    # không thắng warm_all
    assert out["v2_redirect"]["recommend_on"] is True and out["v2_hedge"]["recommend_on"] is True
    assert out["v2_full"]["recommend_on"] is True                                    # chậm hơn v2_hedge 2–5 ms = nhiễu (≤ 25 ms)
    assert out["v2_full_auto"]["recommend_on"] is False                              # range auto: ngưỡng 100 ms + chậm hơn v2_full
    md = (tmp_path / "BENCH_DL_SUMMARY.md").read_text(encoding="utf-8")
    assert "GIỮ TẮT nhóm cờ này" in md and 'range_connections = "auto"' in md


def _samples(rnd, med, n=20, tail=0, slow=1500.0):
    xs = [rnd.gauss(med, 15) for _ in range(n - tail)] + [rnd.gauss(slow, 60) for _ in range(tail)]
    rnd.shuffle(xs)
    return xs


def _summ(samples, fails=None):
    return {v: {"total_ms": tuple(ws.pct(x, p) for p in (50, 90, 95)), "n": len(x) + (fails or {}).get(v, 0),
                "fail": (fails or {}).get(v, 0)} for v, x in samples.items()}


def test_summary_marks_p90_flip_from_tail_count_as_inconclusive_not_off(tmp_path):
    """Review: 20 lượt, đuôi chậm 10% → P90 nhảy ~1,3 s chỉ vì 1 lượt rơi vào đuôi. Không được kết luận GIỮ TẮT/BẬT từ nhiễu đó."""
    import random as _r
    rnd = _r.Random(3)
    smp = {"warm_all": _samples(rnd, 640, tail=2), "v2_ssl": _samples(rnd, 645, tail=2), "v2_redirect": _samples(rnd, 360, tail=1),
           "v2_hedge": _samples(rnd, 362, tail=3)}
    out = ws.bench_dl_summary(tmp_path, list(smp), _summ(smp), samples=smp)
    assert out["v2_ssl"]["decision"] == "off"                                        # median chắc chắn không nhanh hơn 50 ms
    assert out["v2_hedge"]["decision"] != "off"                                      # P90 tệ hơn v2_redirect chỉ vì 3 vs 1 lượt đuôi
    assert out["v2_redirect"]["ci_gain_median"][0] > 0
    md = (tmp_path / "BENCH_DL_SUMMARY.md").read_text(encoding="utf-8")
    assert "lượt > 1 s" in md and "KTC 90%" in md


def test_summary_clear_win_and_range_auto_needs_100ms_from_warm_all_auto(tmp_path):
    import random as _r
    rnd = _r.Random(4)
    smp = {"warm_all": _samples(rnd, 900), "v2_ssl": _samples(rnd, 800), "warm_all_auto": _samples(rnd, 840),
           "v2_full_auto": _samples(rnd, 800)}
    out = ws.bench_dl_summary(tmp_path, list(smp), _summ(smp), samples=smp)
    assert out["v2_ssl"]["decision"] == "on"                                         # thắng rõ 100 ms, không đuôi
    assert out["warm_all_auto"]["need_gain_ms"] == 100 and out["warm_all_auto"]["decision"] == "off"   # chỉ 60 ms < 100
    assert out["range_auto"]["decision"] == "off"
    smp2 = dict(smp, warm_all_auto=_samples(rnd, 760))
    out2 = ws.bench_dl_summary(tmp_path, list(smp2), _summ(smp2), samples=smp2)
    assert out2["warm_all_auto"]["decision"] == "on"
    fails = {"warm_all_auto": 3}
    out3 = ws.bench_dl_summary(tmp_path, list(smp2), _summ(smp2, fails), samples=smp2)
    assert out3["warm_all_auto"]["decision"] == "off"                                # lỗi tăng → giữ tắt


def test_bench_variants_cover_spec_and_warm_all_auto_has_no_v2_flags():
    for v in ("legacy", "warm_all", "v2_ssl", "v2_redirect", "v2_hedge", "v2_full", "v2_full_auto", "warm_all_auto"):
        assert v in ws.BENCH_DL_VARIANTS and v in ws.BENCH_DL_V2_SET
    o = ws.BENCH_DL_VARIANTS["warm_all_auto"][0]
    assert o["range_connections"] == "auto" and all(o[k] == ws.NET_OPTS_OFF[k] for k in ws.NET_OPTS_KEYS)
    bat = (HERE.parent / "BENCH_TAI_360.bat").read_text(encoding="utf-8", errors="replace")
    assert "warm_all_auto" in bat and "v2_full_auto" in bat


# ------------------------------------------------------------------ hồi quy theo review đối kháng: 403/429 không sinh thêm request
@need_openssl
def test_predicted_host_429_raises_without_any_extra_request(hosts, cert, tmp_path):
    hosts.host_rules[A1] = {"redirect_to": B1}
    hosts.add("/q1", b"1" * 2000)
    hosts.add("/q2", b"2" * 2000)
    use(cert, net_redirect_memory=True, net_mn_preconnect=True, dl_hedge=True, dl_hedge_delay_ms=120)
    ws.download_single(hosts.url(A1, "/q1", mn=("sn-aaa", "sn-bbb", "sn-ccc")), tmp_path / "1", {})
    hosts.host_status[(B1, "/q2")] = 429
    info = {}
    with pytest.raises(ws.HttpStatus) as ei:
        ws.download_single(hosts.url(A1, "/q2", mn=("sn-aaa", "sn-bbb", "sn-ccc")), tmp_path / "2", info)
    assert ei.value.code == 429 and info["redirect_skip_fail"] == "HttpStatus 429"
    time.sleep(0.3)
    assert hosts.reqs[(B1, "/q2")] == 1 and hosts.reqs[(A1, "/q2")] == 0 and hosts.reqs[(C1, "/q2")] == 0   # không gửi thêm gì
    assert ws.spec_paused()                                                          # 10 phút không request suy đoán
    hosts.add("/q3", b"3" * 2000)
    hosts.host_rules[A1] = {"redirect_to": B1, "server_wait": (0.3, 0, 0)}
    info = {}
    ws.download_single(hosts.url(A1, "/q3", mn=("sn-aaa", "sn-bbb", "sn-ccc")), tmp_path / "3", info)
    assert not info.get("hedge_sent") and "mn_preconnect" not in info and "redirect_skip" not in info


@need_openssl
def test_late_429_from_losing_replica_bans_it_and_pauses_speculation(hosts, cert, tmp_path):
    hosts.host_rules[A4] = {"server_wait": (0.2, 0, 0)}
    hosts.host_rules[B4] = {"server_wait": (0.4, 0, 0)}
    hosts.host_status[(B4, "/l1")] = 429
    hosts.add("/l1", b"l" * 3000)
    use(cert, dl_hedge=True, dl_hedge_delay_ms=50)
    info = {}
    ws.download_single(hosts.url(A4, "/l1", mn=("sn-aaa", "sn-bbb")), tmp_path / "l", info)
    assert info["hedge_win"] == "primary"
    end = time.time() + 2
    while time.time() < end and not ws._HEDGE_BAN:
        time.sleep(0.02)
    assert any(k.startswith(B4) for k in ws._HEDGE_BAN) and ws.spec_paused()      # 429 MUỘN của bên thua vẫn được ghi nhận


@need_openssl
def test_late_failure_of_predicted_host_is_reported_and_blocked_even_if_hedge_rescued(hosts, cert, tmp_path):
    hosts.host_rules[A1] = {"redirect_to": B1}
    hosts.add("/s1", b"1" * 2000)
    hosts.add("/s2", b"2" * 2000)
    use(cert, net_redirect_memory=True, dl_hedge=True, dl_hedge_delay_ms=50)
    ws.download_single(hosts.url(A1, "/s1", mn=("sn-aaa", "sn-bbb", "sn-ccc")), tmp_path / "1", {})
    hosts.host_rules[B1] = {"server_wait": (0.3, 0, 0)}
    hosts.host_status[(B1, "/s2")] = 403                                             # đích đoán từ chối CHẬM (sau khi đã hedge)
    info = {}
    ws.download_single(hosts.url(A1, "/s2", mn=("sn-aaa", "sn-bbb", "sn-ccc")), tmp_path / "2", info)
    assert info["hedge_win"] == "replica"
    end = time.time() + 2
    while time.time() < end and not info.get("redirect_skip_fail"):
        time.sleep(0.02)
    assert info["redirect_skip_fail"] == "HttpStatus 403" and ws.redirect_predict(hosts.url(A1, "/s9")) == (None, "")


@need_openssl
def test_range_reports_429_even_if_another_part_fails_differently(hosts, cert, tmp_path):
    """Review vòng 2: lỗi của đoạn đầu (vd 404/timeout) không được che 429 của đoạn khác (process phải chờ ≥ 10 s)."""
    data = bytes(range(256)) * 2000
    hosts.add("/g2", data)
    real = ws._follow

    def fake(url, headers=None, **k):
        a = int(headers["Range"].split("=")[1].split("-")[0])
        if a == 0:
            raise ws.HttpStatus(404)
        if a > len(data) // 2:
            raise ws.HttpStatus(429)
        return real(url, headers=headers, **k)
    use(cert, net_mn_preconnect=True)
    ws_follow = ws._follow
    ws._follow = fake
    try:
        info = {}
        with pytest.raises(ws.HttpStatus) as ei:
            ws.download_ranges(hosts.url(A3, "/g2"), tmp_path / "2", len(data), 4, info)
        assert ei.value.code == 429 and info["saw_block"] == 429
        ws.set_net_opts(ws.NET_OPTS_OFF)                                             # cờ tắt: như bản cũ (lỗi đoạn đầu)
        with pytest.raises(ws.HttpStatus) as ei:
            ws.download_ranges(hosts.url(A3, "/g2"), tmp_path / "3", len(data), 4, {})
        assert ei.value.code == 404
    finally:
        ws._follow = ws_follow


@need_openssl
def test_range_mn_preconnect_runs_once_per_file_not_per_part(hosts, cert, tmp_path):
    hosts.host_rules[A3] = {"redirect_to": C3}
    data = bytes(range(256)) * 2000
    hosts.add("/k", data)
    use(cert, net_mn_preconnect=True)
    before = ws.DL_STATS["mn_preconnect"]
    info = {}
    ws.download_ranges(hosts.url(A3, "/k", mn=("sn-aaa", "sn-bbb", "sn-ccc", "sn-ddd")), tmp_path / "k", len(data), 4, info)
    time.sleep(0.3)
    assert info["mn_preconnect"] <= 3 and ws.DL_STATS["mn_preconnect"] - before <= 3


@need_openssl
def test_mn_preconnect_is_skipped_in_legacy_net_mode(hosts, cert):
    """Review: ở net_mode legacy (urllib3 gốc), kết nối mở sẵn sau TLS 1.3 bị coi là đứt và bỏ đi → chỉ tốn bắt tay. Không mở sẵn."""
    u = hosts.url(A3, "/lg", mn=("sn-aaa", "sn-ccc"))
    ws.set_net_mode("legacy")
    ws.set_net_opts(dict(ws.NET_OPTS_OFF, net_mn_preconnect=True))
    assert ws.mn_preconnect(u) == []
    use(cert, net_mn_preconnect=True)
    assert ws.mn_preconnect(u) == [C3.split(":")[0] + f":{hosts.port}"]


def test_alt_urls_are_plain_gets_and_skipped_on_the_next_attempt(tmp_path, gvs, monkeypatch):
    from concurrent.futures import Future
    bad1 = gvs.add("/alt/w", b"", "status", code=403)
    bad2 = gvs.add("/alt/a", b"", "status", code=403)
    seen = []
    real = ws._open_media
    monkeypatch.setattr(ws, "_open_media", lambda u, info, **k: seen.append((u, dict(info))) or real(u, info, **k))
    ws.set_net_opts(dict(ws.NET_OPTS_OFF, dl_alt_url_on_403=True, dl_hedge=True, net_redirect_memory=True, net_mn_preconnect=True))
    e = T.make_engine(tmp_path, T.CH[:1], excel_enabled=False)
    fut = Future()
    fut.set_result({"pick": {"kind": "progressive", "video": T.mkfmt(bad2, 18, 360, "video/mp4", 100)}})
    fmt = T.mkfmt(bad1, 18, 360, "video/mp4", 100)
    with pytest.raises(ws.HttpStatus):
        e._fetch(fmt, tmp_path / "o.mp4", {}, 1, alts=[("web_embedded", fut)])
    assert [u for u, _ in seen] == [bad1, bad2] and seen[1][1].get("no_spec") and seen[1][1].get("no_hedge")
    seen.clear()
    with pytest.raises(ws.HttpStatus):                                               # lần thử sau (lỗi trước là HTTP 403): no_alt
        e._fetch(fmt, tmp_path / "o.mp4", {"no_alt": True, "no_spec": True, "no_hedge": True}, 1, alts=[("web_embedded", fut)])
    assert [u for u, _ in seen] == [bad1]


def test_v2_ttfb_first_read_is_256k_like_old_path(tmp_path, gvs, monkeypatch):
    """Review: khúc 1 MiB làm 'byte đầu' = 1 MiB đầu → TTFB của v2 trông tệ hơn chỉ vì cách đo. Khúc đầu phải 256 KB ở cả 2 đường."""
    firsts = []
    real = urllib3.response.HTTPResponse.read

    def rd(self, amt=None, *a, **k):
        if not getattr(self, "_t_first", False):
            self._t_first = True
            firsts.append(amt)
        return real(self, amt, *a, **k)
    monkeypatch.setattr(urllib3.response.HTTPResponse, "read", rd)
    url = gvs.add("/tt", b"t" * 3_000_000, "ok")
    for on in (True, False):
        ws.set_net_opts(dict(ws.NET_OPTS_OFF, dl_hotpath_v2=on))
        assert ws.download_single(url, tmp_path / f"t{on}", {}) == 3_000_000
    assert firsts == [256 * 1024, 256 * 1024]


def test_summary_can_be_regenerated_from_csv_with_reasons(tmp_path):
    import csv as _csv
    import random as _r
    rnd = _r.Random(9)
    rows = [{"variant": v, "video_id": "X", "rep": i, "result": "DONE", "total_ms": round(t, 1)}
            for v, med_ in (("warm_all", 900), ("v2_ssl", 780)) for i, t in enumerate(_samples(rnd, med_))]
    rows.append({"variant": "v2_ssl", "video_id": "X", "rep": 99, "result": "FAILED", "total_ms": ""})
    with open(tmp_path / "BENCH_DL.csv", "w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    out = ws.bench_dl_summary_from_csv(tmp_path / "BENCH_DL.csv")
    assert out["v2_ssl"]["decision"] == "off" and out["v2_ssl"]["error_rate"] > 0 and out["v2_ssl"]["reason"] == "lỗi tăng"
    md = (tmp_path / "BENCH_DL_SUMMARY.md").read_text(encoding="utf-8")
    assert "Lý do" in md and "Cộng dồn" in md


# ------------------------------------------------------------------ hồi quy review vòng 2
@need_openssl
def test_replica_429_is_not_hidden_by_slow_403_of_predicted_host(hosts, cert, tmp_path):
    hosts.host_rules[A1] = {"redirect_to": B1}
    hosts.add("/w1", b"1" * 2000)
    hosts.add("/w2", b"2" * 2000)
    use(cert, net_redirect_memory=True, dl_hedge=True, dl_hedge_delay_ms=50)
    ws.download_single(hosts.url(A1, "/w1", mn=("sn-aaa", "sn-bbb", "sn-ccc")), tmp_path / "1", {})
    hosts.host_rules[B1] = {"server_wait": (0.3, 0, 0)}
    hosts.host_status[(B1, "/w2")] = 403                                             # đoán: 403 chậm (sau khi đã hedge)
    hosts.host_status[(C1, "/w2")] = 429                                             # replica: 429 ngay
    info = {}
    with pytest.raises(ws.HttpStatus) as ei:
        ws.download_single(hosts.url(A1, "/w2", mn=("sn-aaa", "sn-bbb", "sn-ccc")), tmp_path / "2", info)
    time.sleep(0.2)
    assert ei.value.code == 429 and hosts.reqs[(A1, "/w2")] == 0 and info["saw_block"] == 429 and ws.spec_paused()


@need_openssl
def test_replica_win_while_predicted_pending_is_not_counted_as_correct_guess(hosts, cert, tmp_path):
    hosts.host_rules[A1] = {"redirect_to": B1}
    hosts.add("/u1", b"1" * 2000)
    hosts.add("/u2", b"2" * 2000)
    use(cert, net_redirect_memory=True, dl_hedge=True, dl_hedge_delay_ms=50)
    ws.download_single(hosts.url(A1, "/u1", mn=("sn-aaa", "sn-bbb", "sn-ccc")), tmp_path / "1", {})
    hosts.host_rules[B1] = {"server_wait": (0.6, 0, 0)}                              # đích đoán chậm, chưa trả lời khi replica thắng
    before = ws.DL_STATS["redirect_skip"]
    info = {}
    ws.download_single(hosts.url(A1, "/u2", mn=("sn-aaa", "sn-bbb", "sn-ccc")), tmp_path / "2", info)
    assert info["hedge_win"] == "replica" and info["redirect_unconfirmed"] and ws.DL_STATS["redirect_skip"] == before


def test_v2_hotpath_handles_chunked_responses(tmp_path):
    """Review vòng 2: read() + stream() trên body chunked làm hỏng bộ giải mã chunk của urllib3."""
    import http.server
    body = bytes(range(256)) * 11719                                                 # ≈ 3 MB
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(0, len(body), 100_000):
                c = body[i:i + 100_000]
                self.wfile.write(f"{len(c):x}\r\n".encode() + c + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        for on in (True, False):
            ws.set_net_opts(dict(ws.NET_OPTS_OFF, dl_hotpath_v2=on))
            assert ws.download_single(f"http://127.0.0.1:{srv.server_address[1]}/c", tmp_path / f"c{on}", {}) == len(body)
            assert (tmp_path / f"c{on}").read_bytes() == body
    finally:
        srv.shutdown()
        srv.server_close()


def test_malformed_hedge_delay_never_breaks_downloads(tmp_path, gvs):
    url = gvs.add("/md", b"m" * 5000, "ok")
    ws.set_net_opts(dict(ws.NET_OPTS_OFF, dl_hedge_delay_ms="120ms"))                # cờ tắt + giá trị hỏng: như bản cũ
    assert ws.download_single(url, tmp_path / "a", {}) == 5000
    ws.set_net_opts(dict(ws.NET_OPTS_OFF, dl_hedge=True, dl_hedge_delay_ms="abc"))
    assert ws._hedge_delay_s() == 0.12 and ws.download_single(url, tmp_path / "b", {}) == 5000


def test_alt_loop_skipped_during_429_pause_and_alt_network_error_keeps_original_403(tmp_path, gvs, monkeypatch):
    from concurrent.futures import Future
    bad1 = gvs.add("/alt2/w", b"", "status", code=403)
    bad2 = gvs.add("/alt2/a", b"", "status", code=403)
    ws.set_net_opts(dict(ws.NET_OPTS_OFF, dl_alt_url_on_403=True))
    e = T.make_engine(tmp_path, T.CH[:1], excel_enabled=False)
    fut = Future()
    fut.set_result({"pick": {"kind": "progressive", "video": T.mkfmt(bad2, 18, 360, "video/mp4", 100)}})
    fmt = T.mkfmt(bad1, 18, 360, "video/mp4", 100)
    ws.note_429()
    with pytest.raises(ws.HttpStatus):
        e._fetch(fmt, tmp_path / "o.mp4", {}, 1, alts=[("web_embedded", fut)])
    assert len(gvs.requests_for("/alt2/a")) == 0                                     # đang tạm dừng vì 429 → không thử link phụ
    ws.reset_dl_v2_state()
    real = ws.Engine._fetch_one

    def one(self, f, dest, info, n, cancel=None):
        if f["url"] == bad2:
            raise urllib3.exceptions.ReadTimeoutError(None, bad2, "t")
        return real(self, f, dest, info, n, cancel)
    monkeypatch.setattr(ws.Engine, "_fetch_one", one)
    info = {}
    with pytest.raises(ws.HttpStatus) as ei:
        e._fetch(fmt, tmp_path / "o.mp4", info, 1, alts=[("web_embedded", fut)])
    assert ei.value.code == 403 and info["alt_tried"]                                # lỗi mạng của link phụ không che 403 gốc


@need_openssl
def test_probe_403_makes_following_get_plain(hosts, cert, tmp_path):
    import types
    hosts.add("/pz", b"z" * 1000)
    hosts.host_status[(A3, "/pz")] = 403
    use(cert, net_mn_preconnect=True, dl_hedge=True, dl_hedge_delay_ms=1)
    eng = types.SimpleNamespace(stop_ev=threading.Event(), log=lambda *a: None)
    info = {}
    with pytest.raises(ws.HttpStatus):
        ws.Engine._fetch_one(eng, {"url": hosts.url(A3, "/pz", mn=("sn-aaa", "sn-bbb", "sn-ccc")), "clen": 0}, tmp_path / "p", info, 2)
    time.sleep(0.2)
    assert info["no_spec"] and info["saw_block"] == 403 and "mn_preconnect" not in info and not info.get("hedge_sent")
    assert hosts.reqs[(A3, "/pz")] == 2                                              # dò + 1 GET thường, không gì thêm


@T.need_ff
def test_video_remembers_block_across_attempts_even_if_last_error_is_not_http(tmp_path, gvs, media, monkeypatch):
    """Review vòng 2: 403 bị lỗi mạng của link phụ che → lần thử sau vẫn phải 'thận trọng' (không suy đoán, không link phụ)."""
    vid = "CAUTION0001"
    bad = gvs.add(f"/vp/{vid}_w", b"", "status", code=403, hdr_delay=0.25)           # 403 chậm → link client khác đã về
    alt = gvs.add(f"/vp/{vid}_a", b"", "status", code=403)
    seen = []

    def player(self, v, client):
        if client != "android_vr":
            time.sleep(0.05)
        return {"client": client, "verdict": "ok", "net": {}, "title": "t", "pick": {
            "kind": "progressive", "audio": None,
            "video": T.mkfmt(bad if client == "android_vr" else alt, 18, 360, 'video/mp4; codecs="avc1, mp4a"', 1000)}}
    monkeypatch.setattr(ws.Resolver, "_player", player)
    real_fetch = ws.Engine._fetch
    monkeypatch.setattr(ws.Engine, "_fetch", lambda self, fmt, dest, info, n, cancel=None, alts=None: seen.append(dict(info)) or
                        real_fetch(self, fmt, dest, info, n, cancel, alts))
    e = T.make_engine(tmp_path, T.CH[:1], excel_enabled=False, retry_schedule_sec=[0.2, 0.2])
    e.start(websub=False)
    e.on_event(T.CH[0], vid, "WEBSUB", T.TS)
    r = T.wait_state(e, vid, states=("FAILED",), t=20)
    e.stop()
    assert r["state"] == "FAILED" and len(seen) == 3
    assert not seen[0].get("no_alt") and all(x.get("no_alt") and x.get("no_spec") for x in seen[1:])
    assert len(gvs.requests_for(f"/vp/{vid}_a")) == 1                                # link phụ: đúng 1 lần cho cả video


# số TỔNG (ms) của lần bench mock đã được review vòng 2 dùng làm ví dụ (commit 93ff0f3) — cố định trong test, không đọc file CSV
_R2_MOCK = {"warm_all": [640, 2003, 638, 1986, 1873, 573, 590, 655, 1984, 576, 574, 637, 639, 654, 651, 637, 632, 655, 629, 2006],
            "v2_ssl": [633, 645, 648, 2003, 635, 632, 630, 586, 646, 636, 640, 572, 631, 580, 640, 584, 631, 636, 1984, 634],
            "v2_redirect": [577, 358, 372, 357, 374, 359, 357, 385, 355, 1708, 356, 1708, 351, 356, 1715, 361, 354, 363, 354, 368],
            "v2_hedge": [371, 366, 356, 375, 376, 371, 554, 373, 370, 358, 356, 356, 363, 356, 360, 375, 359, 386, 355, 358],
            "v2_full": [355, 492, 357, 542, 479, 356, 354, 355, 356, 358, 556, 542, 353, 373, 363, 358, 354, 545, 354, 364]}


def test_summary_incremental_check_has_unsure_state_and_does_not_flip_on_one_run(tmp_path):
    smp = {k: [float(x) for x in v] for k, v in _R2_MOCK.items()}
    out = ws.bench_dl_summary(tmp_path, list(smp), _summ(smp), samples=smp)
    h2 = list(smp["v2_hedge"])
    h2[h2.index(554.0)] = 370.0                                                      # đổi đúng 1 lượt của v2_hedge
    smp2 = dict(smp, v2_hedge=h2)
    out2 = ws.bench_dl_summary(tmp_path, list(smp2), _summ(smp2), samples=smp2)
    assert out["v2_full"]["decision"] != "on" and {out["v2_full"]["decision"], out2["v2_full"]["decision"]} != {"on", "off"}


def test_summary_range_auto_needs_warm_all_auto_and_failed_variants_count(tmp_path):
    import random as _r
    rnd = _r.Random(6)
    smp = {"warm_all": _samples(rnd, 900), "v2_full": _samples(rnd, 760), "v2_full_auto": _samples(rnd, 775)}
    out = ws.bench_dl_summary(tmp_path, list(smp), _summ(smp), samples=smp)
    assert out["range_auto"]["decision"] == "inconclusive"                           # chưa đo warm_all_auto → không được BẬT
    names = list(smp) + ["warm_all_auto"]
    summ = dict(_summ(smp), warm_all_auto={"total_ms": (None, None, None), "n": 20, "fail": 20})
    out2 = ws.bench_dl_summary(tmp_path, names, summ, samples=smp)
    assert out2["warm_all_auto"]["decision"] == "off" and out2["range_auto"]["decision"] == "off"
    md = (tmp_path / "BENCH_DL_SUMMARY.md").read_text(encoding="utf-8")
    assert "xem dòng range auto" in md


def test_summary_marks_variants_measured_during_429_pause_inconclusive(tmp_path):
    import random as _r
    rnd = _r.Random(8)
    smp = {"warm_all": _samples(rnd, 900), "v2_ssl": _samples(rnd, 899), "v2_redirect": _samples(rnd, 600)[:3]}   # còn 3 lượt sạch
    out = ws.bench_dl_summary(tmp_path, list(smp), _summ(smp), samples=smp, paused={"v2_redirect": 17})
    assert out["v2_redirect"]["decision"] == "inconclusive" and "lượt sạch" in out["v2_redirect"]["reason"]
    smp2 = dict(smp, v2_redirect=_samples(rnd, 600))                                  # đủ lượt sạch → chấm trên lượt sạch
    out2 = ws.bench_dl_summary(tmp_path, list(smp2), _summ(smp2), samples=smp2, paused={"v2_redirect": 4})
    assert out2["v2_redirect"]["decision"] == "on" and "đã bỏ 4 lượt" in out2["v2_redirect"]["reason"]


def test_summary_lose_with_paused_runs_is_not_reported_as_keep_off(tmp_path):
    """Review vòng 3: 429 làm mọi lượt v2 chạy như đã tắt → không được ghi 'GIỮ TẮT' cho tính năng chưa hề chạy."""
    import random as _r
    rnd = _r.Random(12)
    smp = {"warm_all": _samples(rnd, 900), "v2_ssl": _samples(rnd, 890), "v2_redirect": []}
    summ = dict(_summ({k: v for k, v in smp.items() if v}), v2_redirect={"total_ms": (None, None, None), "n": 20, "fail": 0})
    out = ws.bench_dl_summary(tmp_path, list(smp), summ, samples=smp, paused={"v2_redirect": 20})
    assert out["v2_redirect"]["decision"] == "inconclusive"


def test_summary_compares_with_last_firm_variant_and_skips_failed_predecessor(tmp_path):
    import random as _r
    rnd = _r.Random(13)
    # v2_hedge lỗi hết → v2_full phải so với v2_redirect (chậm hơn 150 ms) chứ không mặc định 'ổn'
    smp = {"warm_all": _samples(rnd, 650), "v2_ssl": _samples(rnd, 640), "v2_redirect": _samples(rnd, 370), "v2_hedge": [],
           "v2_full": _samples(rnd, 520)}
    summ = dict(_summ({k: v for k, v in smp.items() if v}), v2_hedge={"total_ms": (None, None, None), "n": 20, "fail": 20})
    out = ws.bench_dl_summary(tmp_path, list(smp), summ, samples=smp)
    assert out["v2_hedge"]["decision"] == "off" and out["v2_full"]["decision"] == "off" and out["v2_full"]["inc_vs"] == "v2_redirect"
    # v2_hedge chưa kết luận → v2_full còn phải so với v2_redirect (biến thể chắc gần nhất); P90 kém 100+ ms trong nhiễu → không BẬT
    smp2 = {k: [float(x) for x in v] for k, v in _R2_MOCK.items()}
    smp2["v2_redirect"] = [370.0] * 19 + [380.0]
    smp2["v2_hedge"] = [365.0] * 16 + [1700.0] * 4
    smp2["v2_full"] = [365.0] * 16 + [520.0] * 4
    out2 = ws.bench_dl_summary(tmp_path, list(smp2), _summ(smp2), samples=smp2)
    assert out2["v2_hedge"]["decision"] != "on" and out2["v2_full"]["decision"] != "on"


def test_summary_with_fewer_than_5_runs_is_inconclusive(tmp_path):
    smp = {"warm_all": [900.0, 905, 910, 895], "v2_ssl": [700.0, 705, 710, 695]}
    out = ws.bench_dl_summary(tmp_path, list(smp), _summ(smp), samples=smp)
    assert out == {} or all(v["decision"] == "inconclusive" for v in out.values())


def test_summary_from_csv_drops_runs_where_speculation_was_off(tmp_path):
    import csv as _csv
    import random as _r
    rnd = _r.Random(14)
    rows = []
    for v, med_ in (("warm_all", 900), ("v2_ssl", 895), ("v2_redirect", 600)):
        for i, t in enumerate(_samples(rnd, med_)):
            rows.append({"variant": v, "video_id": "X", "rep": i, "result": "DONE", "total_ms": round(t, 1), "spec_off": 0})
    for i in range(10):                                                              # lượt bị tắt suy đoán: chậm như warm_all
        rows.append({"variant": "v2_redirect", "video_id": "X", "rep": 50 + i, "result": "DONE", "total_ms": 900.0, "spec_off": 1})
    with open(tmp_path / "BENCH_DL.csv", "w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    out = ws.bench_dl_summary_from_csv(tmp_path / "BENCH_DL.csv")
    assert out["v2_redirect"]["decision"] == "on" and out["v2_redirect"]["paused_runs"] == 10


# ------------------------------------------------------------------ hồi quy review vòng 3
@need_openssl
def test_any_429_pauses_speculation_immediately(hosts, cert, tmp_path):
    """Review vòng 3/4: 429 được ghi NGAY lúc nhận, ở mọi biến thể (kể cả cờ tắt — trạng thái này chỉ đường v2 đọc), để biến thể v2
    chạy ngay sau đó (bench, worker khác) không suy đoán."""
    hosts.add("/z", b"z" * 100)
    hosts.host_status[(A3, "/z")] = 429
    use(cert)
    with pytest.raises(ws.HttpStatus):
        ws._follow(hosts.url(A3, "/z"))
    assert ws.spec_paused()


@need_openssl
def test_range_429_in_one_part_pauses_now_and_stops_other_parts(hosts, cert, tmp_path):
    data = bytes(range(256)) * 4000
    hosts.add("/q", data)
    hosts.host_rules[A3] = {"server_wait": (0.6, 0, 0)}                              # các đoạn khác: header sau 0,6 s
    real = ws._follow

    def fake(url, headers=None, **k):
        if int(headers["Range"].split("=")[1].split("-")[0]) > len(data) // 2:
            if ws._v2_spec_flags():
                ws.note_429()                                                        # như _follow thật khi nhận 429
            raise ws.HttpStatus(429)
        return real(url, headers=headers, **k)
    use(cert, net_mn_preconnect=True)
    seen = {}

    info = {}

    def watch():
        time.sleep(0.25)
        seen["paused_mid"] = ws.spec_paused()                                        # khi các đoạn khác CHƯA xong
        seen["blk_mid"] = info.get("saw_block")                                      # ghi NGAY, không đợi mọi đoạn
    monkeypatch_follow = ws._follow
    ws._follow = fake
    try:
        threading.Thread(target=watch, daemon=True).start()
        with pytest.raises(ws.HttpStatus) as ei:
            ws.download_ranges(hosts.url(A3, "/q"), tmp_path / "q", len(data), 4, info)
    finally:
        ws._follow = monkeypatch_follow
    assert ei.value.code == 429 and info["saw_block"] == 429 and seen["paused_mid"] is True and seen["blk_mid"] == 429


def test_dash_streams_share_block_state():
    ws.set_net_opts(dict(ws.NET_OPTS_OFF, dl_hedge=True))
    blk = {}
    info_v, info_a = {"blk": blk}, {"blk": blk}
    assert ws._speculative_ok(info_v)
    ws.mark_block(info_a, ws.HttpStatus(403))
    assert not ws._speculative_ok(info_v) and blk["saw_block"] == 403
    e = ws.HttpStatus(429, retry_after=12)
    ws.mark_block(info_a, e)
    ws.mark_block(info_a, ws.HttpStatus(403))
    assert blk["saw_block"] == 429 and info_a["saw_429_exc"] is e                    # 429 giữ ưu tiên + giữ Retry-After


@T.need_ff
def test_hidden_block_is_written_into_error_and_hidden_429_keeps_retry_after(tmp_path, gvs, media, monkeypatch):
    calls = []

    def fetch(self, fmt, dest, info, n, cancel=None, alts=None):
        calls.append(dict(info))
        ws.mark_block(info, ws.HttpStatus(429, retry_after=12) if len(calls) == 1 else ws.HttpStatus(403))
        raise IOError("mất kết nối giữa chừng")
    monkeypatch.setattr(ws.Resolver, "resolve", lambda self, v, deadline_s=2.5: T.plan_prog(gvs, media, v))
    monkeypatch.setattr(ws.Engine, "_fetch", fetch)
    e = T.make_engine(tmp_path, T.CH[:1], excel_enabled=False, retry_schedule_sec=[0.2, 0.2, 0.2])
    e.start(websub=False)
    vid = "HIDDEN42901"
    t0 = time.time()
    e.on_event(T.CH[0], vid, "WEBSUB", T.TS)
    end = time.time() + 10
    row = None
    while time.time() < end:
        row = e.db.one("SELECT * FROM videos WHERE video_id=?", (vid,))
        if row is not None and row["state"] == "RETRY_WAIT":
            break
        time.sleep(0.05)
    e.stop()
    assert row["state"] == "RETRY_WAIT" and "HTTP 429" in row["error"]              # 429 bị IOError che → vẫn là 429
    assert row["next_try"] - t0 >= 11.5 and vid in e._dl_caution                     # giữ Retry-After 12 s


def test_range_auto_rows_never_contradict_combined_decision(tmp_path):
    import random as _r
    rnd = _r.Random(11)
    smp = {"warm_all": _samples(rnd, 900), "v2_full": _samples(rnd, 760), "v2_full_auto": _samples(rnd, 760),
           "warm_all_auto": _samples(rnd, 899)}
    names = ["warm_all", "v2_full", "v2_full_auto", "warm_all_auto"]
    out = ws.bench_dl_summary(tmp_path, names, _summ(smp), samples=smp)
    assert out["range_auto"]["decision"] == "off" and out["v2_full_auto"]["decision"] != "on"


# ------------------------------------------------------------------ hồi quy review vòng 4
def _rows(spec):
    """spec: {biến thể: [(video, vòng, TỔNG|None, spec_off, own_429), ...]} → các dòng như BENCH_DL.csv."""
    return [{"variant": v, "video_id": vid, "rep": rep, "result": "DONE" if t is not None else "FAILED", "total_ms": t,
             "spec_off": so, "own_429": o4} for v, xs in spec.items() for vid, rep, t, so, o4 in xs]


def _summ_rows(rows, names):
    out = {}
    for v in names:
        rs = [r for r in rows if r["variant"] == v]
        ok = [r["total_ms"] for r in rs if r["total_ms"] is not None]
        out[v] = {"total_ms": tuple(ws.pct(ok, p) for p in (50, 90, 95)), "n": len(rs), "fail": len(rs) - len(ok)}
    return out


def test_summary_matched_pairs_do_not_reward_dropping_a_slow_video(tmp_path):
    """Review vòng 3 (cao): lọc lượt 'bẩn' chỉ ở phía v2 → bỏ đúng video chậm làm nhóm KHÔNG có tác dụng trông nhanh hơn → BẬT sai."""
    import random as _r
    rnd = _r.Random(21)
    spec = {}
    for v in ("warm_all", "v2_ssl", "v2_redirect"):
        xs = []
        for rep in range(7):
            for vid, base_ in (("S", 700), ("M", 1000), ("L", 1300)):
                off_ = 1 if (v == "v2_redirect" and vid == "L") else 0                  # video L luôn bị 'thận trọng' ở v2
                xs.append((vid, rep, rnd.gauss(base_, 10), off_, 0))
        spec[v] = xs
    rows = _rows(spec)
    names = list(spec)
    keyed, off, own = ws._bench_keyed(rows, names, lambda r: bool(r["spec_off"]))
    out = ws.bench_dl_summary(tmp_path, names, _summ_rows(rows, names), keyed=keyed, off=off, own429=own)
    assert out["v2_redirect"]["decision"] != "on" and abs(out["v2_redirect"]["gain_median_ms"]) < 50


def test_summary_own_429_keeps_group_off_and_failed_clean_runs_are_off(tmp_path):
    import random as _r
    rnd = _r.Random(22)
    spec = {"warm_all": [("A", i, rnd.gauss(900, 10), 0, 0) for i in range(20)],
            "v2_ssl": [("A", i, rnd.gauss(890, 10), 0, 0) for i in range(20)],
            "v2_redirect": [("A", i, rnd.gauss(600, 10), 0, 1 if i == 3 else 0) for i in range(20)],
            "v2_hedge": [("A", i, None, 1 if i == 0 else 0, 0) for i in range(20)]}
    rows = _rows(spec)
    names = list(spec)
    keyed, off, own = ws._bench_keyed(rows, names, lambda r: bool(r["spec_off"]))
    out = ws.bench_dl_summary(tmp_path, names, _summ_rows(rows, names), keyed=keyed, off=off, own429=own)
    assert out["v2_redirect"]["decision"] == "off" and "429" in out["v2_redirect"]["reason"]
    assert out["v2_hedge"]["decision"] == "off"                                      # 19 lượt sạch, 0 thành công → giữ tắt


@need_openssl
def test_predicted_host_404_during_new_429_pause_is_retryable_not_fatal(hosts, cert, tmp_path):
    hosts.host_rules[A1] = {"redirect_to": B1}
    hosts.add("/f1", b"1" * 1000)
    hosts.add("/f2", b"2" * 1000)
    use(cert, net_redirect_memory=True)
    ws.download_single(hosts.url(A1, "/f1"), tmp_path / "1", {})
    hosts.host_rules[B1] = {"server_wait": (0.2, 0, 0)}
    hosts.host_status[(B1, "/f2")] = 404

    def other_worker_429():
        time.sleep(0.05)
        ws.note_429()
    threading.Thread(target=other_worker_429, daemon=True).start()
    with pytest.raises(ws.HttpStatus) as ei:
        ws.download_single(hosts.url(A1, "/f2"), tmp_path / "2", {})
    assert ei.value.code == 429 and hosts.reqs[(A1, "/f2")] == 0                      # không thành 404 'vĩnh viễn' của video


@T.need_ff
def test_fatal_error_is_not_rewritten_by_hidden_429(tmp_path, gvs, media, monkeypatch):
    def fetch(self, fmt, dest, info, n, cancel=None, alts=None):
        ws.mark_block(info, ws.HttpStatus(429, retry_after=11))
        raise ws.Fatal("cần ffmpeg")
    monkeypatch.setattr(ws.Resolver, "resolve", lambda self, v, deadline_s=2.5: T.plan_prog(gvs, media, v))
    monkeypatch.setattr(ws.Engine, "_fetch", fetch)
    e = T.make_engine(tmp_path, T.CH[:1], excel_enabled=False, retry_schedule_sec=[0.2, 0.2])
    e.start(websub=False)
    e.on_event(T.CH[0], "FATAL429VID", "WEBSUB", T.TS)
    r = T.wait_state(e, "FATAL429VID", states=("FAILED", "RETRY_WAIT"), t=10)
    e.stop()
    assert r["state"] == "FAILED" and r["error"].startswith("Fatal") and "[đã gặp HTTP 429]" in r["error"]


def test_range_auto_uses_one_connection_while_cautious_or_paused(tmp_path, gvs, monkeypatch):
    seen = []
    real = ws.Engine._fetch_one
    monkeypatch.setattr(ws.Engine, "_fetch_one", lambda self, fmt, dest, info, n, cancel=None: seen.append(n) or
                        real(self, fmt, dest, info, n, cancel))
    monkeypatch.setattr(ws.Resolver, "resolve", lambda self, v, deadline_s=2.5: {"pick": {"kind": "progressive", "audio": None,
                        "video": T.mkfmt(gvs.add(f"/ra/{v}", b"\0" * 8_000_000, "ok"), 18, 360, 'video/mp4; codecs="avc1, mp4a"', 8_000_000)},
                        "source": "MOCK"})
    monkeypatch.setattr(ws, "ffprobe_info", lambda p: {"ok": True, "audio": True})
    e = T.make_engine(tmp_path, T.CH[:1], excel_enabled=False, range_connections="auto", dl_quick_integrity=False)
    e.start(websub=False)
    ws.note_429()
    e.on_event(T.CH[0], "RAUTOPAUSE1", "WEBSUB", T.TS)
    assert T.wait_state(e, "RAUTOPAUSE1")["state"] == "DONE"
    e.stop()
    assert seen == [1]                                                               # đang tạm dừng vì 429 → không chia 4 đoạn


def test_429_pause_survives_restart_and_recover_keeps_next_try(tmp_path):
    ws.reset_dl_v2_state()
    ws.set_net_opts(dict(ws.NET_OPTS_OFF, dl_hedge=True))
    e = T.make_engine(tmp_path, T.CH[:1], excel_enabled=False)
    ws.note_429()
    assert (tmp_path / "ws360_spec_pause.txt").exists()
    ws.reset_dl_v2_state()
    assert not ws.spec_paused()
    left = ws.load_spec_pause(tmp_path / "ws360_spec_pause.txt")                       # = Engine mới sau khởi động lại
    assert 500 < left <= 600 and ws.spec_paused()
    e.db.x("INSERT INTO videos(video_id, channel_id, url, engine, detected_at, state, next_try, error) VALUES(?,?,?,?,?,?,?,?)",
           ("REC429VID01", T.CH[0], "u", "WEBSUB", ws.iso(), "RETRY_WAIT", time.time() + 30, "HttpStatus: HTTP 429"))
    e.recover()
    nb, _, job = e.dlq.get_nowait()
    assert nb >= time.time() + 25 and job["not_before"] == nb
