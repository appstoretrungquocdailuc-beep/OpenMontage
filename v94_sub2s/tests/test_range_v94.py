# -*- coding: utf-8 -*-
"""Range engine V9.4 trên mock GVS: toàn vẹn byte (SHA256), lỗi, huỷ, dọn dẹp."""
import threading
import time

import pytest

from mock_gvs import make_payload, sha256

MB = 1024 * 1024


def _dl(mod, url, tmp_path, name, expected, kind="video", info=None, logger=None):
    info = {} if info is None else info
    ok = mod._download_http_range_turbo(url, tmp_path, name, logger or (lambda m: None),
                                        64 * 1024, expected, media_kind=kind, info=info)
    return ok, info


@pytest.mark.parametrize("size,kind", [(300 * 1024, "audio"), (3 * MB + 17, "video"),
                                       (11 * MB + 3, "audio"), (24 * MB + 1, "video")])
def test_exact_206_sha256(v94, gvs, tmp_path, size, kind):
    data = make_payload(size, seed=size)
    url = gvs.add("/f", data)
    ok, info = _dl(v94, url, tmp_path, "out.bin", size, kind)
    assert ok, info
    assert sha256((tmp_path / "out.bin").read_bytes()) == sha256(data)
    assert info["bytes"] == size and info["total"] == size
    assert info["fail_reason"] == ""
    assert info["status"].get("206", 0) >= 1


def test_redirect_302(v94, gvs, tmp_path):
    data = make_payload(5 * MB)
    real = gvs.add("/real", data)
    url = gvs.add("/r1", mode="redirect", to=real)
    ok, info = _dl(v94, url, tmp_path, "o.bin", len(data))
    assert ok, info
    assert sha256((tmp_path / "o.bin").read_bytes()) == sha256(data)
    assert info["redirects"] >= 1
    # URL cuối được chia sẻ: không phải mọi request đều đi qua redirect
    assert len(gvs.requests_for("/r1")) <= info["workers_initial"] + 1


@pytest.mark.parametrize("code", [403, 410, 429])
def test_auth_fail_is_hard_and_fast(v94, gvs, tmp_path, code):
    hdr = {"Retry-After": "2"} if code == 429 else {}
    url = gvs.add("/deny", mode="status", code=code, headers=hdr)
    t0 = time.perf_counter()
    ok, info = _dl(v94, url, tmp_path, "d.bin", 4 * MB)
    dt = time.perf_counter() - t0
    assert not ok
    assert info["fail_reason"] == f"HTTP_{code}"
    assert v94._v94_is_auth_fail(info)
    assert dt < 1.5, dt
    assert not (tmp_path / "d.bin").exists()
    if code == 429:
        assert info["retry_after"] == 2.0
    # Không spam: tối đa 1 request/worker đợt đầu, không retry
    assert len(gvs.requests_for("/deny")) <= info["workers_initial"]


def test_smart_progressive_skips_aria2_on_403(v94, gvs, tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(v94, "download_aria2c_ultimate", lambda *a, **k: called.append(1) or False)
    url = gvs.add("/deny2", mode="status", code=403)
    info = {}
    ok = v94.download_smart_progressive(url, tmp_path, "x.bin", lambda m: None, 1, 4 * MB, info=info)
    assert not ok and not called and info["fail_reason"] == "HTTP_403"


def test_server_ignores_range_200(v94, gvs, tmp_path):
    data = make_payload(2 * MB)
    url = gvs.add("/norange", data, mode="norange")
    ok, info = _dl(v94, url, tmp_path, "n.bin", len(data))
    assert not ok and info["fail_reason"] == "HTTP_200"


@pytest.mark.parametrize("real,expected", [(6 * MB + 5, 5 * MB), (5 * MB, 6 * MB + 5),
                                           (900 * 1024, 2 * MB), (3 * MB, 1 * MB)])
def test_wrong_size_estimate_resized_in_place(v94, gvs, tmp_path, real, expected):
    data = make_payload(real, seed=real)
    url = gvs.add("/sz", data)
    ok, info = _dl(v94, url, tmp_path, "s.bin", expected)
    assert ok, info
    assert sha256((tmp_path / "s.bin").read_bytes()) == sha256(data)
    assert info["resized"] == [expected, real]
    # không tải lại: tổng byte nhận = đúng size thật
    assert info["bytes"] == real


@pytest.mark.parametrize("size,kind", [(350 * 1024, "audio"), (9 * MB + 7, "audio"), (7 * MB, "video")])
def test_unknown_size_range_first(v94, gvs, tmp_path, size, kind):
    data = make_payload(size, seed=7)
    url = gvs.add("/unk", data)
    ok, info = _dl(v94, url, tmp_path, "u.bin", 0, kind)
    assert ok, info
    assert sha256((tmp_path / "u.bin").read_bytes()) == sha256(data)
    assert info["workers_peak"] >= 1
    if size > 4 * MB:
        assert info["workers_peak"] > 1     # fan-out sau header đầu


def test_fake_content_range_rejected(v94, gvs, tmp_path):
    data = make_payload(3 * MB)
    url = gvs.add("/bad", data, mode="badcr", shift=1)
    ok, info = _dl(v94, url, tmp_path, "b.bin", len(data))
    assert not ok
    assert info["fail_reason"].startswith("bad_content_range")
    assert not (tmp_path / "b.bin").exists()


def test_v93_writes_wrong_range_bytes(v93, gvs, tmp_path):
    """Bằng chứng lỗ hổng V9.3: edge trả sai đoạn byte (Content-Range lệch) → V9.3 vẫn ghi,
    báo thành công nhưng file SAI nội dung. V9.4 từ chối (test ở trên)."""
    data = make_payload(3 * MB)
    url = gvs.add("/bad93", data, mode="badcr", shift=4096)
    ok = v93._download_http_range_turbo(url, tmp_path, "c.bin", lambda m: None, 1, len(data), "video")
    assert ok                                                          # V9.3 báo thành công...
    assert sha256((tmp_path / "c.bin").read_bytes()) != sha256(data)   # ...nhưng file hỏng


def test_connection_drop_midstream_recovers(v94, gvs, tmp_path):
    data = make_payload(8 * MB, seed=3)
    url = gvs.add("/drop", data, mode="drop_once", drop_at=3 * MB + 12345)
    ok, info = _dl(v94, url, tmp_path, "dr.bin", len(data))
    assert ok, info
    assert sha256((tmp_path / "dr.bin").read_bytes()) == sha256(data)
    assert info["retries"] >= 1


def test_delayed_header(v94, gvs, tmp_path):
    data = make_payload(2 * MB)
    url = gvs.add("/slowhdr", data, hdr_delay=0.4)
    ok, info = _dl(v94, url, tmp_path, "h.bin", len(data))
    assert ok and info["ttfb_ms"] >= 380
    assert sha256((tmp_path / "h.bin").read_bytes()) == sha256(data)


def test_slow_tail_work_stealing(v94, gvs, tmp_path):
    data = make_payload(6 * MB, seed=9)
    # 2 kết nối đầu bị bóp 128KiB/s → nếu không steal sẽ mất > 5s
    url = gvs.add("/tail", data, slow_first_n=2, slow_rate=128 * 1024)
    t0 = time.perf_counter()
    ok, info = _dl(v94, url, tmp_path, "t.bin", len(data))
    dt = time.perf_counter() - t0
    assert ok, info
    assert sha256((tmp_path / "t.bin").read_bytes()) == sha256(data)
    assert info["steals"] >= 1
    assert dt < 4.0, dt


def test_concurrent_jobs(v94, gvs, tmp_path):
    datas = [make_payload(4 * MB + i * 1000, seed=i) for i in range(4)]
    urls = [gvs.add(f"/c{i}", d) for i, d in enumerate(datas)]
    res = [None] * 8

    def run(i):
        kind = "video" if i % 2 == 0 else "audio"
        res[i] = _dl(v94, urls[i // 2], tmp_path, f"c{i}.bin", len(datas[i // 2]), kind)

    ths = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    [t.start() for t in ths]
    [t.join(30) for t in ths]
    for i in range(8):
        ok, info = res[i]
        assert ok, info
        assert sha256((tmp_path / f"c{i}.bin").read_bytes()) == sha256(datas[i // 2])


def test_cancel_cleans_up(v94, gvs, tmp_path):
    data = make_payload(8 * MB)
    url = gvs.add("/slow", data, rate=512 * 1024)
    ev = threading.Event()
    info = {"cancel": ev}
    before = threading.active_count()
    threading.Timer(0.4, ev.set).start()
    t0 = time.perf_counter()
    ok, info = _dl(v94, url, tmp_path, "cx.bin", len(data), info=info)
    assert not ok and info["fail_reason"] == "cancelled"
    assert time.perf_counter() - t0 < 2.0
    assert not (tmp_path / "cx.bin").exists()
    # các thread Range thoát nhanh vì response bị đóng từ thread chính
    deadline = time.time() + 3.0
    while time.time() < deadline and threading.active_count() > before + 2:
        time.sleep(0.05)
    names = [t.name for t in threading.enumerate() if t.name.startswith("YTVideoRange")]
    assert len(names) == 0, names


def test_416_beyond_real_size(v94, gvs, tmp_path):
    """Ước tính quá lớn → các segment vượt size thật nhận 416 */N và được bỏ qua an toàn."""
    data = make_payload(300 * 1024)
    url = gvs.add("/s416", data)
    ok, info = _dl(v94, url, tmp_path, "z.bin", 4 * MB, "audio")
    assert ok, info
    assert sha256((tmp_path / "z.bin").read_bytes()) == sha256(data)


def test_v93_engine_rollback_flag(v94, gvs, tmp_path, monkeypatch):
    data = make_payload(3 * MB)
    url = gvs.add("/rb", data)
    monkeypatch.setattr(v94, "V94_RANGE_ENGINE", "v93")
    ok, info = _dl(v94, url, tmp_path, "rb.bin", len(data))
    assert ok and info["engine"] == "v93"
    assert sha256((tmp_path / "rb.bin").read_bytes()) == sha256(data)


def test_worker_override(v94, monkeypatch):
    monkeypatch.setattr(v94, "V94_RANGE_WORKERS_VIDEO", 12)
    assert v94._range_worker_count(100 * MB, "video") == 12
    monkeypatch.setattr(v94, "V94_RANGE_WORKERS_VIDEO", 0)
    assert v94._range_worker_count(100 * MB, "video") == 32   # heuristic V9.3 giữ nguyên


def test_first_wave_race_default_is_v93(v94):
    # V9.4.1: mặc định race mọi segment đợt đầu như V9.3 (trần 4 của V9.4.0 chậm hơn khi replica nhanh)
    assert v94.V94_FIRST_WAVE_RACE_MAX == -1


def test_gvs_alt_url_and_redact(v94):
    u = ("https://rr3---sn-abc123.googlevideo.com/videoplayback?expire=1&ei=x&ip=1.2.3.4"
         "&mn=sn-abc123%2Csn-def456&sig=SECRET&lsig=SECRET2&pot=TOKEN")
    alt = v94._gvs_alt_url(u)
    assert "rr3---sn-def456.googlevideo.com" in alt
    red = v94.v94_redact(u)
    for secret in ("SECRET", "TOKEN", "1.2.3.4", "abc123"):
        assert secret not in red
    w = v94.v94_redact("https://www.youtube.com/watch?v=dQw4w9WgXcQ&si=TRACK")
    assert "TRACK" not in w and "dQw4w9WgXcQ" in w
