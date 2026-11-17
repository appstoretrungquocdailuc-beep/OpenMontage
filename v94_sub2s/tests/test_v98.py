# -*- coding: utf-8 -*-
"""V9.8: cờ tối ưu RANGE-TURBO (prealloc nền, ghi ngoài khoá, race thích ứng, scale-up, đoạn đầu) + aria2 info.
Mọi biến thể phải: đúng từng byte (SHA-256), đúng offset, 403/416/Content-Range sai vẫn an toàn, không để file dở."""
import os
import shutil
import threading
import time

import pytest

from mock_gvs import make_payload, sha256

MB = 1024 * 1024
WRITE_MODES = ["lock", "split", "pwrite", "pwrite_handles"]


def _dl(mod, url, tmp_path, name, expected, kind="video", info=None):
    info = {} if info is None else info
    ok = mod._download_http_range_turbo(url, tmp_path, name, lambda m: None, 64 * 1024, expected, media_kind=kind, info=info)
    return ok, info


@pytest.fixture()
def wmode(v94, monkeypatch, request):
    m = request.param
    if m == "pwrite_handles":               # nhánh Windows: mỗi luồng 1 handle riêng (không có os.pwrite)
        monkeypatch.delattr(os, "pwrite", raising=False)
        m = "pwrite"
    monkeypatch.setattr(v94, "V98_WRITE_MODE", m)
    return m


def test_v98_defaults(v94):
    """Mặc định V9.8 = chỉ các thay đổi đã qua A/B (BAO_CAO_V98.md); phần còn lại giữ đúng V9.7.1."""
    assert v94.V94_VERSION.startswith("V9.8")
    assert v94.V98_PREALLOC_ASYNC is True and v94.V98_RACE_MODE == "dual" and v94.V98_WORKERS_LEARN is True
    assert v94.V98_FORCE_ARIA2 is False and int(v94.V98_DUAL_MAX_CONNS) == 64
    assert v94.V98_WRITE_MODE == "lock" and v94.V98_SCALE_UP is False and float(v94.V98_PART_MIB) == 0.0
    assert int(v94.V98_ARIA2_SPLIT) == 0 and int(v94.V98_ARIA2_MAX_TRIES) == 2


def test_v98_rollback_env(monkeypatch):
    """Biến môi trường V98_*=0 / fixed → đúng hành vi V9.7.1 (đọc lại module trong tiến trình con)."""
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    code = ("import sys; sys.path.insert(0, r'%s'); sys.path.insert(0, r'%s');"
            "import conftest; conftest._install_pyside6_stub();"
            "import V9_4_SUB2S as m;"
            "print(m.V98_PREALLOC_ASYNC, m.V98_RACE_MODE, m.V98_WORKERS_LEARN)") % (root, root / "tests")
    env = dict(os.environ, V98_PREALLOC_ASYNC="0", V98_RACE_MODE="fixed", V98_WORKERS_LEARN="0", QT_QPA_PLATFORM="offscreen")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120, cwd=str(root))
    assert out.stdout.strip().splitlines()[-1] == "False fixed False", out.stderr[-2000:]


@pytest.mark.parametrize("wmode", WRITE_MODES, indirect=True)
@pytest.mark.parametrize("size,kind", [(3 * MB + 17, "video"), (11 * MB + 3, "audio"), (24 * MB + 1, "video")])
def test_write_modes_exact_bytes(v94, gvs, tmp_path, wmode, size, kind):
    data = make_payload(size, seed=size)
    url = gvs.add("/w", data)
    ok, info = _dl(v94, url, tmp_path, "w.bin", size, kind)
    assert ok, info
    assert sha256((tmp_path / "w.bin").read_bytes()) == sha256(data)
    assert info["write_mode"] == wmode and info["bytes"] == size
    assert info["write_ms"] >= 0 and info["lock_hold_ms"] >= 0


@pytest.mark.parametrize("wmode", WRITE_MODES, indirect=True)
def test_write_modes_steal_drop_resize(v94, gvs, tmp_path, wmode):
    """Đoạn bị chẻ (steal/split), mất kết nối giữa chừng, size ước tính sai — vẫn đúng từng byte."""
    data = make_payload(9 * MB + 77, seed=5)
    u1 = gvs.add("/tail", data, slow_first_n=2, slow_rate=256 * 1024)
    ok, info = _dl(v94, u1, tmp_path, "t.bin", len(data))
    assert ok, info
    assert sha256((tmp_path / "t.bin").read_bytes()) == sha256(data) and info["steals"] >= 1
    u2 = gvs.add("/drop", data, mode="drop_once", drop_at=3 * MB + 12345)
    ok, info = _dl(v94, u2, tmp_path, "d.bin", len(data))
    assert ok, info
    assert sha256((tmp_path / "d.bin").read_bytes()) == sha256(data)
    for exp in (7 * MB, 12 * MB):
        u3 = gvs.add(f"/rs{exp}", data)
        ok, info = _dl(v94, u3, tmp_path, f"r{exp}.bin", exp)
        assert ok, info
        assert sha256((tmp_path / f"r{exp}.bin").read_bytes()) == sha256(data)


@pytest.mark.parametrize("wmode", ["split", "pwrite", "pwrite_handles"], indirect=True)
def test_write_modes_bad_cr_and_403_safe(v94, gvs, tmp_path, wmode):
    data = make_payload(3 * MB)
    ok, info = _dl(v94, gvs.add("/bad", data, mode="badcr", shift=1), tmp_path, "b.bin", len(data))
    assert not ok and info["fail_reason"].startswith("bad_content_range")
    assert not (tmp_path / "b.bin").exists()
    ok, info = _dl(v94, gvs.add("/deny", mode="status", code=403), tmp_path, "d.bin", 4 * MB)
    assert not ok and info["fail_reason"] == "HTTP_403"
    assert len(gvs.requests_for("/deny")) <= info["workers_initial"]
    assert not (tmp_path / "d.bin").exists()


@pytest.mark.parametrize("wmode", ["pwrite_handles"], indirect=True)
def test_per_thread_handles_closed(v94, gvs, tmp_path, wmode):
    """Windows: handle riêng của mỗi luồng phải được đóng (không thì không xoá/đổi tên file được)."""
    data = make_payload(6 * MB, seed=2)
    ok, info = _dl(v94, gvs.add("/h", data), tmp_path, "h.bin", len(data))
    assert ok, info
    os.replace(tmp_path / "h.bin", tmp_path / "h2.bin")
    (tmp_path / "h2.bin").unlink()


@pytest.mark.parametrize("wmode", ["split", "pwrite"], indirect=True)
def test_write_modes_416_and_cancel(v94, gvs, tmp_path, wmode):
    data = make_payload(300 * 1024)
    ok, info = _dl(v94, gvs.add("/s416", data), tmp_path, "z.bin", 4 * MB, "audio")
    assert ok, info
    assert sha256((tmp_path / "z.bin").read_bytes()) == sha256(data)
    big = make_payload(8 * MB)
    ev = threading.Event()
    threading.Timer(0.4, ev.set).start()
    ok, info = _dl(v94, gvs.add("/slow", big, rate=512 * 1024), tmp_path, "cx.bin", len(big), info={"cancel": ev})
    assert not ok and info["fail_reason"] == "cancelled"
    assert not (tmp_path / "cx.bin").exists()


class _SlowTrunc:
    def __init__(self, f, delay, fail=False):
        self._f, self._d, self._fail = f, delay, fail

    def truncate(self, size=None):
        time.sleep(self._d)
        if self._fail:
            raise OSError("disk full (giả lập)")
        return self._f.truncate(size)

    def __getattr__(self, k):
        return getattr(self._f, k)


def _patch_slow_open(v94, monkeypatch, delay, fail=False):
    import builtins
    real = builtins.open

    def op(file, mode="r", *a, **k):
        f = real(file, mode, *a, **k)
        return _SlowTrunc(f, delay, fail) if mode == "w+b" else f
    monkeypatch.setattr(v94, "open", op, raising=False)


def test_prealloc_async_overlaps_first_get(v94, gvs, tmp_path, monkeypatch):
    """Preallocation chạy nền: GET đầu được gửi NGAY (không chờ 300 ms truncate), file vẫn đúng."""
    _patch_slow_open(v94, monkeypatch, 0.3)
    data = make_payload(8 * MB + 5, seed=8)
    url = gvs.add("/pa", data, hdr_delay=0.35)
    monkeypatch.setattr(v94, "V98_PREALLOC_ASYNC", False)
    ok, info0 = _dl(v94, url, tmp_path, "p0.bin", len(data))
    assert ok and info0["first_get_ms"] >= 280
    monkeypatch.setattr(v94, "V98_PREALLOC_ASYNC", True)
    for wm in ("lock", "pwrite"):
        monkeypatch.setattr(v94, "V98_WRITE_MODE", wm)
        ok, info = _dl(v94, url, tmp_path, f"p_{wm}.bin", len(data))
        assert ok, info
        assert sha256((tmp_path / f"p_{wm}.bin").read_bytes()) == sha256(data)
        assert info["prealloc_async"] and info["prealloc_bg_ms"] >= 280
        assert info["first_get_ms"] < 100, info["first_get_ms"]
        assert info["elapsed_ms"] < info0["elapsed_ms"] - 150


def test_prealloc_async_resize_waits(v94, gvs, tmp_path, monkeypatch):
    """Size thật khác ước tính: co/giãn file phải CHỜ preallocation nền xong (không truncate chồng nhau)."""
    _patch_slow_open(v94, monkeypatch, 0.25)
    monkeypatch.setattr(v94, "V98_PREALLOC_ASYNC", True)
    data = make_payload(6 * MB + 5, seed=6)
    for exp in (5 * MB, 8 * MB):
        ok, info = _dl(v94, gvs.add(f"/r{exp}", data), tmp_path, f"r{exp}.bin", exp)
        assert ok, info
        assert sha256((tmp_path / f"r{exp}.bin").read_bytes()) == sha256(data)
        assert info["resized"] == [exp, len(data)]


def test_prealloc_async_failure_is_clean(v94, gvs, tmp_path, monkeypatch):
    _patch_slow_open(v94, monkeypatch, 0.05, fail=True)
    monkeypatch.setattr(v94, "V98_PREALLOC_ASYNC", True)
    data = make_payload(4 * MB)
    ok, info = _dl(v94, gvs.add("/pf", data), tmp_path, "pf.bin", len(data))
    assert not ok
    assert not (tmp_path / "pf.bin").exists()


def _two_host(v94, gvs, monkeypatch, data, main_delay, alt_delay, tag):
    real = gvs.add(f"/{tag}/real", data, hdr_delay=main_delay)
    main = gvs.add(f"/{tag}/main", mode="redirect", to=real)
    alt = gvs.add(f"/{tag}/alt", data, hdr_delay=alt_delay)
    monkeypatch.setattr(v94, "_gvs_alt_url", lambda u: alt if str(u) == main else "")
    v94._MN_RACE_BAD_UNTIL[0] = 0.0
    return main


def test_adaptive_race_fewer_requests_when_main_fast(v94, gvs, tmp_path, monkeypatch):
    data = make_payload(24 * MB, seed=24)
    monkeypatch.setattr(v94, "V94_RANGE_WORKERS_VIDEO", 16)
    res = {}
    for mode in ("fixed", "adaptive"):
        monkeypatch.setattr(v94, "V98_RACE_MODE", mode)
        main = _two_host(v94, gvs, monkeypatch, data, 0.05, 0.4, mode)
        ok, info = _dl(v94, main, tmp_path, f"{mode}.bin", len(data))
        assert ok, info
        assert sha256((tmp_path / f"{mode}.bin").read_bytes()) == sha256(data)
        time.sleep(0.6)                                  # request thua cuộc về hết rồi mới đếm
        res[mode] = (info, len(gvs.requests_for(f"/{mode}/alt")))
    assert res["fixed"][0]["races"] == 16 and res["adaptive"][0]["races"] == int(v94.V98_RACE_PROBES)
    assert res["adaptive"][1] <= int(v94.V98_RACE_PROBES) + 1 < res["fixed"][1]
    assert res["adaptive"][0]["race_mode"] == "adaptive"


def test_adaptive_race_uses_replica_when_main_slow(v94, gvs, tmp_path, monkeypatch):
    """CDN chính chậm (900 ms), replica nhanh: thăm dò thắng → các đoạn khác hedge ngay, không chờ 900 ms."""
    data = make_payload(16 * MB, seed=16)
    monkeypatch.setattr(v94, "V94_RANGE_WORKERS_VIDEO", 12)
    monkeypatch.setattr(v94, "V98_RACE_MODE", "adaptive")
    main = _two_host(v94, gvs, monkeypatch, data, 0.9, 0.05, "slowmain")
    ok, info = _dl(v94, main, tmp_path, "sm.bin", len(data))
    assert ok, info
    assert sha256((tmp_path / "sm.bin").read_bytes()) == sha256(data)
    assert info["alt_wins"] >= 2
    assert info["elapsed_ms"] < 850, info["elapsed_ms"]


def test_adaptive_race_alt_403_safe(v94, gvs, tmp_path, monkeypatch):
    data = make_payload(16 * MB, seed=3)              # > V94_RACE_ALL_IF_WORKERS_LE đoạn (file nhỏ vẫn race hết như cũ)
    monkeypatch.setattr(v94, "V94_RANGE_WORKERS_VIDEO", 12)
    monkeypatch.setattr(v94, "V98_RACE_MODE", "adaptive")
    real = gvs.add("/a403/real", data, hdr_delay=0.05)
    main = gvs.add("/a403/main", mode="redirect", to=real)
    alt = gvs.add("/a403/alt", mode="status", code=403)
    monkeypatch.setattr(v94, "_gvs_alt_url", lambda u: alt if str(u) == main else "")
    v94._MN_RACE_BAD_UNTIL[0] = 0.0
    ok, info = _dl(v94, main, tmp_path, "a.bin", len(data))
    assert ok, info
    assert sha256((tmp_path / "a.bin").read_bytes()) == sha256(data)
    assert len(gvs.requests_for("/a403/alt")) <= int(v94.V98_RACE_PROBES) + 1


def test_scale_up_adds_workers_when_per_connection_limited(v94, gvs, tmp_path, monkeypatch):
    data = make_payload(24 * MB, seed=31)
    monkeypatch.setattr(v94, "V98_SCALE_UP", True)
    monkeypatch.setattr(v94, "V94_RANGE_WORKERS_VIDEO", 8)
    url = gvs.add("/pc", data, rate=2 * MB)                 # mỗi kết nối 2 MiB/s
    ok, info = _dl(v94, url, tmp_path, "pc.bin", len(data))
    assert ok, info
    assert sha256((tmp_path / "pc.bin").read_bytes()) == sha256(data)
    assert info["scale_added"] >= 2 and info["workers_peak"] > 8


def test_scale_up_stops_when_link_capped(v94, tmp_path, monkeypatch):
    from mock_gvs import MockGVS
    data = make_payload(24 * MB, seed=32)
    monkeypatch.setattr(v94, "V98_SCALE_UP", True)
    monkeypatch.setattr(v94, "V94_RANGE_WORKERS_VIDEO", 8)
    with MockGVS(link_bytes_per_sec=12 * MB) as g:          # đường truyền chung 12 MiB/s, mỗi kết nối không giới hạn
        ok, info = _dl(v94, g.add("/lc", data), tmp_path, "lc.bin", len(data))
    assert ok, info
    assert sha256((tmp_path / "lc.bin").read_bytes()) == sha256(data)
    assert info["scale_added"] <= 4                          # tối đa 1 bước, rồi dừng vì không nhanh hơn
    if info["scale_steps"]:
        assert info["scale_steps"][-1]["gain"] < 0.35


@pytest.mark.parametrize("pm", [0.5, 2.0])
def test_part_mib_cap_exact(v94, gvs, tmp_path, monkeypatch, pm):
    monkeypatch.setattr(v94, "V98_PART_MIB", pm)
    data = make_payload(13 * MB + 9, seed=13)
    ok, info = _dl(v94, gvs.add("/pm", data), tmp_path, "pm.bin", len(data))
    assert ok, info
    assert sha256((tmp_path / "pm.bin").read_bytes()) == sha256(data)
    assert info["part_bytes"] <= max(pm * MB, v94.RANGE_MIN_PART_BYTES)


def test_all_flags_together_concurrent_jobs(v94, gvs, tmp_path, monkeypatch):
    for k, v in dict(V98_PREALLOC_ASYNC=True, V98_WRITE_MODE="pwrite", V98_RACE_MODE="adaptive", V98_SCALE_UP=True).items():
        monkeypatch.setattr(v94, k, v)
    datas = [make_payload(5 * MB + i * 1000, seed=i) for i in range(3)]
    urls = [gvs.add(f"/c{i}", d, rate=4 * MB) for i, d in enumerate(datas)]
    res = [None] * 6

    def run(i):
        res[i] = _dl(v94, urls[i // 2], tmp_path, f"c{i}.bin", len(datas[i // 2]), "video" if i % 2 == 0 else "audio")
    ths = [threading.Thread(target=run, args=(i,)) for i in range(6)]
    [t.start() for t in ths]
    [t.join(30) for t in ths]
    for i in range(6):
        ok, info = res[i]
        assert ok, info
        assert sha256((tmp_path / f"c{i}.bin").read_bytes()) == sha256(datas[i // 2])


# ----------------------------------------------------------------------------- aria2 RPC (engine C, chỉ bench)
need_aria2 = pytest.mark.skipif(not shutil.which("aria2c"), reason="không có aria2c")


@need_aria2
def test_aria2_rpc_info_and_split(v94, gvs, tmp_path, monkeypatch):
    monkeypatch.setattr(v94, "ARIA2C_PATH", shutil.which("aria2c"))
    monkeypatch.setattr(v94, "V98_ARIA2_SPLIT", 8)
    data = make_payload(12 * MB, seed=12)
    url = gvs.add("/ar", data, rate=3 * MB)
    info = {}
    ok = v94._download_aria2c_rpc(url, tmp_path, "ar.bin", lambda m: None, 1, len(data), info=info)
    assert ok, info
    assert sha256((tmp_path / "ar.bin").read_bytes()) == sha256(data)
    assert info["split"] == 8 and 2 <= info["connections"] <= 8 and info["ttfb_ms"] > 0


@need_aria2
def test_aria2_rpc_403_single_request(v94, gvs, tmp_path, monkeypatch):
    monkeypatch.setattr(v94, "ARIA2C_PATH", shutil.which("aria2c"))
    monkeypatch.setattr(v94, "V98_ARIA2_MAX_TRIES", 1)
    url = gvs.add("/a403", mode="status", code=403)
    info = {}
    ok = v94._download_aria2c_rpc(url, tmp_path, "x.bin", lambda m: None, 1, 4 * MB, info=info)
    assert not ok and info["fail_reason"] == "HTTP_403"
    assert len(gvs.requests_for("/a403")) == 1


def test_learn_race_mode_switches_by_history(v94, gvs, tmp_path, monkeypatch):
    """learn: chưa có lịch sử → race hết; replica không thắng → job sau chỉ thăm dò; CDN chính chậm lại → thăm dò thắng,
    hedge ngay trong job đó, và job sau quay lại race hết."""
    monkeypatch.setattr(v94, "V98_RACE_MODE", "learn")
    monkeypatch.setattr(v94, "_V98_RACE_HIST", [])
    monkeypatch.setattr(v94, "V94_RANGE_WORKERS_VIDEO", 12)
    data = make_payload(16 * MB, seed=40)
    seq = [("l1", 0.05, 0.4, "learn:all"), ("l2", 0.05, 0.4, "learn:probe"),
           ("l3", 0.9, 0.05, "learn:probe"), ("l4", 0.9, 0.05, "learn:all")]
    infos = {}
    for tag, md, ad, want in seq:
        main = _two_host(v94, gvs, monkeypatch, data, md, ad, tag)
        ok, info = _dl(v94, main, tmp_path, f"{tag}.bin", len(data))
        assert ok, info
        assert sha256((tmp_path / f"{tag}.bin").read_bytes()) == sha256(data)
        assert info["race_mode"] == want, (tag, info["race_mode"])
        infos[tag] = info
        time.sleep(0.3)
    assert infos["l1"]["races"] == 12 and infos["l1"]["alt_wins"] == 0
    assert infos["l2"]["races"] == int(v94.V98_RACE_PROBES)
    assert infos["l3"]["alt_wins"] >= 1 and infos["l3"]["elapsed_ms"] < 900
    assert infos["l4"]["races"] == 12


@pytest.mark.parametrize("link_capped", [False, True])
def test_workers_learn_picks_by_measured_speed(v94, tmp_path, monkeypatch, link_capped):
    """Giới hạn THEO KẾT NỐI → sau 4 job thử, chọn mức nhiều worker; giới hạn ĐƯỜNG TRUYỀN → giữ mức ít worker."""
    from mock_gvs import MockGVS
    monkeypatch.setattr(v94, "V98_WORKERS_LEARN", True)
    monkeypatch.setattr(v94, "_V98_WL", {})
    monkeypatch.setattr(v94, "V94_RANGE_WORKERS_VIDEO", 0)
    data = make_payload(16 * MB, seed=50)
    arms = []
    with MockGVS(link_bytes_per_sec=(8 * MB if link_capped else 0)) as g:
        url = g.add("/wl", data, rate=(0 if link_capped else 1 * MB))
        for i in range(5):
            ok, info = _dl(v94, url, tmp_path, f"wl{i}.bin", len(data))
            assert ok, info
            assert sha256((tmp_path / f"wl{i}.bin").read_bytes()) == sha256(data)
            arms.append((info["workers_learn"], info["workers_initial"]))
    assert [a for a, _ in arms[:4]] == ["lo", "hi", "lo", "hi"]
    assert arms[1][1] > arms[0][1]
    assert arms[4][0] == ("lo" if link_capped else "hi"), arms


# ----------------------------------------------------------------------------- V9.8.1: dual-host + ép aria2
def _two_host_rate(v94, gvs, monkeypatch, data, main_delay, alt_delay, tag, rate):
    real = gvs.add(f"/{tag}/real", data, hdr_delay=main_delay, rate=rate)
    main = gvs.add(f"/{tag}/main", mode="redirect", to=real)
    alt = gvs.add(f"/{tag}/alt", data, hdr_delay=alt_delay, rate=rate)
    monkeypatch.setattr(v94, "_gvs_alt_url", lambda u: alt if str(u) == main else "")
    v94._MN_RACE_BAD_UNTIL[0] = 0.0
    return main


def test_dual_all_connections_carry_data(v94, gvs, tmp_path, monkeypatch):
    """dual: 2N kết nối đều tải (không bắn kép), nhanh hơn race-all khi giới hạn THEO KẾT NỐI, không nhiều request hơn."""
    monkeypatch.setattr(v94, "V98_WORKERS_LEARN", False)
    monkeypatch.setattr(v94, "V94_RANGE_WORKERS_VIDEO", 12)
    data = make_payload(24 * MB, seed=61)
    res = {}
    for mode in ("fixed", "dual"):
        monkeypatch.setattr(v94, "V98_RACE_MODE", mode)
        main = _two_host_rate(v94, gvs, monkeypatch, data, 0.05, 0.08, mode, 1 * MB)
        ok, info = _dl(v94, main, tmp_path, f"{mode}.bin", len(data))
        assert ok, info
        assert sha256((tmp_path / f"{mode}.bin").read_bytes()) == sha256(data)
        time.sleep(0.5)
        res[mode] = (info, len(gvs.requests_for(f"/{mode}/")))
    fi, di = res["fixed"][0], res["dual"][0]
    assert di["dual"] is True and di["race_mode"] == "dual" and di["races"] == 0
    assert di["workers_initial"] == 24 and fi["workers_initial"] == 12
    assert len(gvs.requests_for("/dual/alt")) >= 10 and len(gvs.requests_for("/dual/real")) >= 10
    assert di["elapsed_ms"] < 0.75 * fi["elapsed_ms"], (di["elapsed_ms"], fi["elapsed_ms"])
    assert res["dual"][1] <= res["fixed"][1] + 6, (res["dual"][1], res["fixed"][1])


def test_dual_slow_main_hedges_to_replica(v94, gvs, tmp_path, monkeypatch):
    monkeypatch.setattr(v94, "V98_RACE_MODE", "dual")
    monkeypatch.setattr(v94, "V98_WORKERS_LEARN", False)
    monkeypatch.setattr(v94, "V94_RANGE_WORKERS_VIDEO", 12)
    data = make_payload(16 * MB, seed=62)
    main = _two_host_rate(v94, gvs, monkeypatch, data, 1.2, 0.05, "dslow", 0)
    ok, info = _dl(v94, main, tmp_path, "ds.bin", len(data))
    assert ok, info
    assert sha256((tmp_path / "ds.bin").read_bytes()) == sha256(data)
    assert info["elapsed_ms"] < 1100, info["elapsed_ms"]         # không chờ host chính 1.2 s


def test_dual_replica_403_falls_back_to_main(v94, gvs, tmp_path, monkeypatch):
    monkeypatch.setattr(v94, "V98_RACE_MODE", "dual")
    monkeypatch.setattr(v94, "V98_WORKERS_LEARN", False)
    monkeypatch.setattr(v94, "V94_RANGE_WORKERS_VIDEO", 12)
    data = make_payload(16 * MB, seed=63)
    real = gvs.add("/d403/real", data, hdr_delay=0.05)
    main = gvs.add("/d403/main", mode="redirect", to=real)
    alt = gvs.add("/d403/alt", mode="status", code=403)
    monkeypatch.setattr(v94, "_gvs_alt_url", lambda u: alt if str(u) == main else "")
    v94._MN_RACE_BAD_UNTIL[0] = 0.0
    ok, info = _dl(v94, main, tmp_path, "d4.bin", len(data))
    v94._MN_RACE_BAD_UNTIL[0] = 0.0
    assert ok, info
    assert sha256((tmp_path / "d4.bin").read_bytes()) == sha256(data)
    assert len(gvs.requests_for("/d403/alt")) <= 13              # tối đa 1 request/đoạn gán cho replica, không thử lại


def test_dual_main_403_is_hard_fail(v94, gvs, tmp_path, monkeypatch):
    monkeypatch.setattr(v94, "V98_RACE_MODE", "dual")
    monkeypatch.setattr(v94, "V94_RANGE_WORKERS_VIDEO", 12)
    main = gvs.add("/m403/main", mode="status", code=403)
    alt = gvs.add("/m403/alt", mode="status", code=403)
    monkeypatch.setattr(v94, "_gvs_alt_url", lambda u: alt if str(u) == main else "")
    v94._MN_RACE_BAD_UNTIL[0] = 0.0
    ok, info = _dl(v94, main, tmp_path, "m4.bin", 16 * MB)
    v94._MN_RACE_BAD_UNTIL[0] = 0.0
    assert not ok and info["fail_reason"] == "HTTP_403"
    assert not (tmp_path / "m4.bin").exists()
    assert len(gvs.requests_for("/m403/")) <= 2 * 12 + 2        # mỗi đoạn đúng 1 request: KHÔNG failover khi cả 2 host từ chối
    # tự bảo vệ: dual tắt 30 phút → job sau dùng learn (race như V9.8), vẫn đúng từng byte
    assert v94._V98_DUAL_GUARD["strikes"] == 1 and v94._V98_DUAL_GUARD["until"] > time.time() + 1000
    monkeypatch.setattr(v94, "V98_WORKERS_LEARN", False)
    data = make_payload(16 * MB, seed=67)
    main2 = _two_host_rate(v94, gvs, monkeypatch, data, 0.05, 0.05, "aftg", 0)
    ok, info = _dl(v94, main2, tmp_path, "ag.bin", len(data))
    assert ok and info["dual"] is False and info["race_mode"].startswith("learn") and info["workers_initial"] == 12
    assert sha256((tmp_path / "ag.bin").read_bytes()) == sha256(data)


@pytest.mark.parametrize("code", [401, 403])
def test_dual_failover_when_main_rejects_but_replica_serves(v94, gvs, tmp_path, monkeypatch, code):
    """Host chính từ chối, replica phục vụ được (race V9.7.1 vốn che được trường hợp này) → dual chuyển đoạn sang replica,
    mỗi đoạn tối đa 1 request tới host lỗi, không tính là 'dual bị từ chối'."""
    monkeypatch.setattr(v94, "V98_RACE_MODE", "dual")
    monkeypatch.setattr(v94, "V98_WORKERS_LEARN", False)
    monkeypatch.setattr(v94, "V94_RANGE_WORKERS_VIDEO", 12)
    data = make_payload(16 * MB, seed=68)
    main = gvs.add(f"/fo{code}/main", mode="status", code=code)
    alt = gvs.add(f"/fo{code}/alt", data, hdr_delay=0.15)
    monkeypatch.setattr(v94, "_gvs_alt_url", lambda u: alt if str(u) == main else "")
    v94._MN_RACE_BAD_UNTIL[0] = 0.0
    ok, info = _dl(v94, main, tmp_path, "fo.bin", len(data))
    assert ok, info
    assert sha256((tmp_path / "fo.bin").read_bytes()) == sha256(data)
    assert info["failovers"] >= 10 and info["host_ok"][0] == 0
    assert len(gvs.requests_for(f"/fo{code}/main")) <= 12 + 1
    assert v94._V98_DUAL_GUARD["strikes"] == 0


def test_dual_429_never_fails_over(v94, gvs, tmp_path, monkeypatch):
    monkeypatch.setattr(v94, "V98_RACE_MODE", "dual")
    monkeypatch.setattr(v94, "V98_WORKERS_LEARN", False)
    monkeypatch.setattr(v94, "V94_RANGE_WORKERS_VIDEO", 12)
    data = make_payload(16 * MB, seed=69)
    main = gvs.add("/f429/main", mode="status", code=429, headers={"Retry-After": "2"})
    alt = gvs.add("/f429/alt", data, hdr_delay=0.3)
    monkeypatch.setattr(v94, "_gvs_alt_url", lambda u: alt if str(u) == main else "")
    v94._MN_RACE_BAD_UNTIL[0] = 0.0
    ok, info = _dl(v94, main, tmp_path, "f9.bin", len(data))
    assert not ok and info["fail_reason"] == "HTTP_429" and info["failovers"] == 0
    assert len(gvs.requests_for("/f429/main")) <= 12
    assert not (tmp_path / "f9.bin").exists()


def test_dual_only_for_video_with_replica(v94, gvs, tmp_path, monkeypatch):
    monkeypatch.setattr(v94, "V98_RACE_MODE", "dual")
    monkeypatch.setattr(v94, "V98_WORKERS_LEARN", False)
    data = make_payload(12 * MB, seed=64)
    main = _two_host_rate(v94, gvs, monkeypatch, data, 0.05, 0.05, "daud", 0)
    ok, info = _dl(v94, main, tmp_path, "au.bin", len(data), "audio")
    assert ok and info["dual"] is False and info["race_mode"].startswith("learn")
    assert sha256((tmp_path / "au.bin").read_bytes()) == sha256(data)
    monkeypatch.setattr(v94, "_gvs_alt_url", lambda u: "")       # không có replica mn → như cũ
    monkeypatch.setattr(v94, "V94_RANGE_WORKERS_VIDEO", 12)
    ok, info = _dl(v94, gvs.add("/noalt", data), tmp_path, "na.bin", len(data))
    assert ok and info["dual"] is False and info["workers_initial"] == 12


@need_aria2
def test_force_aria2_switch(v94, gvs, tmp_path, monkeypatch):
    monkeypatch.setattr(v94, "ARIA2C_PATH", shutil.which("aria2c"))
    data = make_payload(12 * MB, seed=65)
    url = gvs.add("/fa", data, rate=3 * MB)
    ok, info = _dl(v94, url, tmp_path, "off.bin", len(data))     # mặc định TẮT → RANGE-TURBO
    assert ok and info.get("engine") != "aria2_rpc"
    monkeypatch.setattr(v94, "V98_FORCE_ARIA2", True)
    ok, info = _dl(v94, url, tmp_path, "on.bin", len(data))
    assert ok and info["engine"] == "aria2_rpc" and 1 <= info["connections"] <= 16
    assert sha256((tmp_path / "on.bin").read_bytes()) == sha256(data)
    deny = gvs.add("/fa403", mode="status", code=403)
    ok, info = _dl(v94, deny, tmp_path, "deny.bin", 4 * MB)
    assert not ok and info["fail_reason"] == "HTTP_403" and v94._v94_is_auth_fail(info)
    assert len(gvs.requests_for("/fa403")) == 1                  # không thử lại, không rơi về RANGE-TURBO cùng link
    assert not (tmp_path / "deny.bin").exists()
    # dung lượng sai → không nhận file, quay về RANGE-TURBO (tự co/giãn đúng size)
    ok, info = _dl(v94, url, tmp_path, "sz.bin", len(data) + 4096)
    assert ok and info.get("engine") != "aria2_rpc"
    assert sha256((tmp_path / "sz.bin").read_bytes()) == sha256(data)


@need_aria2
def test_aria2_split_clamped_to_16(v94, gvs, tmp_path, monkeypatch):
    """aria2c từ chối > 16 kết nối/máy chủ: file > 48 MiB trước đây xin 20/24 → RPC lỗi."""
    monkeypatch.setattr(v94, "ARIA2C_PATH", shutil.which("aria2c"))
    assert v94._aria2_tuned_split(100 * MB) > 16
    data = make_payload(6 * MB, seed=66)
    info = {}
    ok = v94._download_aria2c_rpc(gvs.add("/cl", data), tmp_path, "cl.bin", lambda m: None, 1, 100 * MB, info=info)
    assert ok and info["split"] == 16
    assert sha256((tmp_path / "cl.bin").read_bytes()) == sha256(data)


# ----------------------------------------------------------------------------- GUI: lựa chọn "ARIA2C (ép)"
def test_gui_has_force_aria2_option_and_saves_it(v94, monkeypatch, tmp_path):
    import json
    from conftest import get_qapp
    if get_qapp() is None:
        pytest.skip("cần PySide6 thật")
    monkeypatch.setattr(v94, "CONFIG_JSON", str(tmp_path / "cfg.json"))
    monkeypatch.setattr(v94, "CONFIG_THEME_JSON", str(tmp_path / "theme.json"))
    w = v94.PremiumDownloaderApp()
    assert w.combo_downloader.currentData() == "aria2c"                 # mặc định KHÔNG đổi: SMART AUTO (RANGE-TURBO)
    idx = w.combo_downloader.findData("aria2c_force")
    assert idx >= 0
    w.combo_downloader.setCurrentIndex(idx)
    w._save_config()
    assert json.load(open(tmp_path / "cfg.json", encoding="utf-8"))["download_engine"] == "aria2c_force"
    w2 = v94.PremiumDownloaderApp()
    assert w2.combo_downloader.currentData() == "aria2c_force"
    for x in (w, w2):
        x.logger_qt.stop()


def _mk_worker(v94, tmp_path, engine):
    import types

    class _T:
        def __init__(self, v): self.v = v
        def text(self): return self.v

    class _C:
        def isChecked(self): return False

    class _Combo:
        def currentData(self): return engine
    m = types.SimpleNamespace()
    m.worker_signals = v94.WorkerSignals()
    m.out_entry, m.keep_temp_checkbox, m.final_move_entry = _T(str(tmp_path / "out")), _C(), _T(str(tmp_path / "final"))
    m.combo_downloader, m.cutsec_entry = _Combo(), _T("0")
    m.quality_mode = lambda: "480hard"
    m.realtime_stop_event = threading.Event()
    return v94.Worker(m, "", "URL", "STATUS")


def test_worker_force_aria2_from_gui_choice(v94, tmp_path, monkeypatch):
    monkeypatch.setattr(v94, "ARIA2C_PATH", shutil.which("aria2c") or "/bin/true")
    w = _mk_worker(v94, tmp_path, "aria2c_force")
    assert w.force_aria2 is True and w.download_engine == "aria2c"      # mọi nhánh khác coi như SMART AUTO
    w = _mk_worker(v94, tmp_path, "aria2c")
    assert w.force_aria2 is False and w.download_engine == "aria2c"
    w = _mk_worker(v94, tmp_path, "idm")
    assert w.force_aria2 is False and w.download_engine == "idm"


@need_aria2
def test_force_aria2_per_job_info(v94, gvs, tmp_path, monkeypatch):
    """Lựa chọn GUI truyền qua info['force_aria2'] của từng job (không cần biến môi trường)."""
    monkeypatch.setattr(v94, "ARIA2C_PATH", shutil.which("aria2c"))
    assert v94.V98_FORCE_ARIA2 is False
    data = make_payload(10 * MB, seed=70)
    url = gvs.add("/gj", data, rate=3 * MB)
    ok, info = _dl(v94, url, tmp_path, "gj.bin", len(data), info={"force_aria2": True})
    assert ok and info["engine"] == "aria2_rpc" and 1 <= info["connections"] <= 16
    assert sha256((tmp_path / "gj.bin").read_bytes()) == sha256(data)
    ok, info = _dl(v94, url, tmp_path, "gj2.bin", len(data), "audio", info={"force_aria2": True})
    assert ok and info["engine"] == "aria2_rpc"
    assert sha256((tmp_path / "gj2.bin").read_bytes()) == sha256(data)
