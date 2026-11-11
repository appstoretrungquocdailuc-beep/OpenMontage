# -*- coding: utf-8 -*-
"""V9.4.1 — các bản vá sau log live đầu tiên (HOT_READY 2.394s, MUX 0.602s).

Mỗi test tương ứng 1 hạng mục của kế hoạch: ff-prewarm, mux-diag-split, validate-mux-output-strict,
race-all-small-files, alt-ban-strict, shorts-fanout-ab, excel-atomic-replace, status-row-verify,
ja-infer-strict, ffprobe-duration-timeout, prewarm-innertube-import, range-request-telemetry,
kpi-timeline-diagnostics.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from mock_gvs import MockGVS, make_payload, sha256

MB = 1024 * 1024
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
need_ff = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="cần ffmpeg/ffprobe")


# ----------------------------------------------------------------------------- helpers
@pytest.fixture(autouse=True)
def _reset_globals(v94):
    v94._MN_RACE_BAD_UNTIL[0] = 0.0
    yield
    v94._MN_RACE_BAD_UNTIL[0] = 0.0


@pytest.fixture()
def telem(v94, monkeypatch, tmp_path):
    t = v94.V94Telemetry(out_dir=str(tmp_path), enabled=True)
    t._thread = object()          # không có thread flush → test tự drain
    monkeypatch.setattr(v94, "TELEM", t)
    return t


def _write_fake_exe(path: Path, body: str):
    path.write_text("#!" + sys.executable + "\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(0o755)
    return str(path)


@pytest.fixture(scope="module")
def h264_media(tmp_path_factory):
    """H.264 DASH-fMP4 (như itag 135) + AAC m4a, 30s, và bản cắt cụt 60% (header còn nguyên)."""
    if not FFMPEG:
        pytest.skip("cần ffmpeg")
    d = tmp_path_factory.mktemp("h264")
    v, a = d / "v135.mp4", d / "a139.m4a"
    subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-i", "testsrc2=size=480x854:rate=15", "-t", "30",
                    "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-pix_fmt", "yuv420p", "-an",
                    "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", str(v)],
                   check=True, capture_output=True)
    subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-i", "sine=frequency=500:duration=30", "-c:a", "aac",
                    "-b:a", "48k", "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", str(a)],
                   check=True, capture_output=True)
    vt = d / "v135_trunc60.mp4"
    raw = v.read_bytes()
    vt.write_bytes(raw[: int(len(raw) * 0.6)])
    at = d / "a139_trunc.m4a"
    ra = a.read_bytes()
    at.write_bytes(ra[: max(70 * 1024, int(len(ra) * 0.40))])   # > MIN_BYTES_AUDIO để lỗi lộ ra ở bước mux
    return {"v": v, "a": a, "v_trunc": vt, "a_trunc": at}


# ============================================================================ ff-prewarm
@need_ff
def test_prewarm_runs_real_remux_outside_job_dirs(v94, monkeypatch, tmp_path):
    calls = []
    real_popen = subprocess.Popen

    def rec(cmd, *a, **k):
        calls.append(list(cmd))
        return real_popen(cmd, *a, **k)
    monkeypatch.setattr(v94.subprocess, "Popen", rec)
    v94._V94_FF_STATE["prewarm_running"] = False
    logs = []
    t0 = time.perf_counter()
    res = v94._v94_ff_prewarm("t", job="x", logger=logs.append)
    dt = time.perf_counter() - t0
    assert res and res["ffmpeg_rc"] == 0 and res["ffprobe_rc"] == 0, res
    assert dt < 3.0
    ff = [c for c in calls if os.path.basename(c[0]).startswith("ffmpeg")]
    fp = [c for c in calls if os.path.basename(c[0]).startswith("ffprobe")]
    assert len(ff) == 1 and len(fp) == 1
    warm_dir = v94._V94_FF_STATE["assets_dir"]
    for c in ff + fp:
        for arg in c[1:]:
            if os.sep in arg:
                assert arg.startswith(warm_dir), arg          # không đụng temp_dir / out_dir của job
    w1 = [x for x in ff[0] if x.endswith(".mp4") and "w1_" in x][0]
    w2 = [x for x in ff[0] if x.endswith(".mp4") and "w2_" in x][0]
    for w, codecs in ((w1, {"h264", "aac"}), (w2, {"vp9", "opus"})):
        out = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0", w],
                             capture_output=True, text=True).stdout.split()
        assert set(out) == codecs, (w, out)
    assert any("FF PREWARM" in l for l in logs)


def test_prewarm_without_ffmpeg_is_noop(v94, monkeypatch):
    monkeypatch.setattr(v94, "FFMPEG_PATH", None)
    assert v94._v94_ff_prewarm("t", logger=lambda m: (_ for _ in ()).throw(AssertionError("no log"))) is None


def test_prewarm_skips_when_another_running(v94):
    v94._V94_FF_STATE["prewarm_running"] = True
    try:
        assert v94._v94_ff_prewarm("t") is None
    finally:
        v94._V94_FF_STATE["prewarm_running"] = False


def _bare_worker(v94, logs):
    w = object.__new__(v94.Worker)
    w._v94 = {"job": "J", "kpi": {}, "video": {}, "audio": {}}
    w.log = logs.append
    return w


@need_ff
def test_job_prewarm_idle_gating(v94, monkeypatch):
    started = []
    monkeypatch.setattr(v94, "_v94_ff_prewarm", lambda *a, **k: started.append(a) or {"ffmpeg_ms": 1, "ffprobe_ms": 1,
                                                                                         "idle_s": 9})
    # vừa có ffmpeg thoát → bỏ qua
    v94._v94_ff_mark_exit(v94.FFMPEG_PATH)
    w = _bare_worker(v94, [])
    w._v94_maybe_prewarm("J")
    assert not started and w._v94["kpi"]["prewarm"].startswith("skip")
    # rảnh lâu → chạy (thread nền), job không chờ
    with v94._V94_FF_LOCK:
        v94._V94_FF_STATE["last_exit"]["ffmpeg"] = time.perf_counter() - 60
    w2 = _bare_worker(v94, [])
    t0 = time.perf_counter()
    w2._v94_maybe_prewarm("J")
    assert time.perf_counter() - t0 < 0.05
    for _ in range(100):
        if started:
            break
        time.sleep(0.01)
    assert started and w2._v94.get("ff_prewarm_started")
    w2._v94_maybe_prewarm("J")      # lần 2 cùng job: không chạy lại
    time.sleep(0.05)
    assert len(started) == 1


def test_job_prewarm_flag_off(v94, monkeypatch):
    started = []
    monkeypatch.setattr(v94, "V94_FF_PREWARM", False)
    monkeypatch.setattr(v94, "_v94_ff_prewarm", lambda *a, **k: started.append(1))
    w = _bare_worker(v94, [])
    w._v94_maybe_prewarm("J")
    time.sleep(0.05)
    assert not started and "prewarm" not in w._v94["kpi"]


@need_ff
def test_start_baseline_line_and_innertube_import(v94):
    logs = []
    v94._V94_FF_STATE["prewarm_running"] = False
    v94._v94_start_baseline(logs.append, "/tmp", "/tmp")
    line = [l for l in logs if "FF BASELINE" in l]
    assert line and "cold ffmpeg=" in line[0] and "warm ffmpeg=" in line[0]
    if v94.yt_dlp:
        assert "yt_dlp.extractor.youtube._base" in sys.modules
        t0 = time.perf_counter()
        v94._get_innertube_client("visionos")
        assert (time.perf_counter() - t0) < 0.05


# ============================================================================ mux-diag-split
@need_ff
def test_mux_diag_equivalent_to_subprocess_run(v94, monkeypatch, h264_media, tmp_path):
    outs = {}
    for lvl in (0, 1):
        monkeypatch.setattr(v94, "V94_MUX_DIAG", lvl)
        o = tmp_path / f"m{lvl}.mp4"
        logs = []
        assert v94.mux_480_stream_copy(h264_media["v"], h264_media["a"], o, logs.append)
        outs[lvl] = (hashlib.sha256(o.read_bytes()).hexdigest(), v94._V94_MUX_INFO.pop(str(o)), logs)
    assert outs[0][0] == outs[1][0]                                  # cùng byte output
    keys = ("video_duration", "audio_duration", "out_time", "out_video_kb", "out_audio_kb", "width", "height")
    assert {k: outs[0][1].get(k) for k in keys} == {k: outs[1][1].get(k) for k in keys}
    line = [l for l in outs[1][2] if "MUX COPY 480P+JA" in l][0]
    assert re.search(r"MUX COPY 480P\+JA: ([\d.]+)s", line)            # regex của bench_v93 vẫn khớp
    for f in ("seq=", "idle=", "pre=", "popen=", "→in0=", "copy="):
        assert f in line, line


def test_ff_run_marks_input_open_delay(v94, tmp_path):
    fake = _write_fake_exe(tmp_path / "ffmpeg", """
        import sys, time
        time.sleep(0.30)
        sys.stderr.write("Input #0, mov,mp4, from 'x':\\n"); sys.stderr.flush()
        sys.stderr.write("Input #1, mov,mp4, from 'y':\\n"); sys.stderr.flush()
        sys.stderr.write("Output #0, mp4, to 'z':\\n"); sys.stderr.flush()
        time.sleep(0.05)
        sys.stderr.write("[out#0/mp4 @ 0x1] video:10kB audio:2kB muxing overhead: 1%\\n")
        sys.stderr.write("size=   12kB time=00:00:01.00 bitrate=1\\r\\n")
    """)
    r, d = v94._v94_ff_run([fake], timeout=10)
    assert r.returncode == 0
    assert 220 <= d["start_in0_ms"] <= 600, d
    assert "\r" not in r.stderr and "time=00:00:01.00" in r.stderr     # chuẩn hoá xuống dòng như text=True
    assert d["copy_ms"] is not None and d["copy_ms"] >= 30


def test_ff_run_timeout_kills_child(v94, tmp_path):
    marker = tmp_path / "alive.txt"
    fake = _write_fake_exe(tmp_path / "ffmpeg_slow", f"""
        import time, os
        open({str(marker)!r}, "w").write(str(os.getpid()))
        time.sleep(30)
    """)
    t0 = time.perf_counter()
    with pytest.raises(subprocess.TimeoutExpired):
        v94._v94_ff_run([fake], timeout=0.8)
    assert time.perf_counter() - t0 < 4.0
    pid = int(marker.read_text())
    time.sleep(0.1)
    try:
        os.kill(pid, 0)
        alive = True
    except OSError:
        alive = False
    assert not alive


def test_mux_timeout_returns_false(v94, monkeypatch, tmp_path):
    fake = _write_fake_exe(tmp_path / "ffmpeg_hang", "import time\ntime.sleep(30)\n")
    monkeypatch.setattr(v94, "FFMPEG_PATH", fake)
    real = v94._v94_ff_run
    monkeypatch.setattr(v94, "_v94_ff_run", lambda cmd, timeout=120, **k: real(cmd, timeout=0.5, **k))
    v = tmp_path / "v.mp4"; v.write_bytes(b"0" * 1000)
    a = tmp_path / "a.m4a"; a.write_bytes(b"0" * 1000)
    logs = []
    assert v94.mux_480_stream_copy(v, a, tmp_path / "o.mp4", logs.append) is False
    assert any("MUX exception" in l and "TimeoutExpired" in l for l in logs)


# ============================================================================ validate-mux-output-strict
@need_ff
@pytest.mark.parametrize("which,expect", [("good", True), ("v_trunc", False), ("a_trunc", False)])
def test_strict_check_on_real_mux_logs(v94, h264_media, tmp_path, which, expect):
    v = h264_media["v_trunc"] if which == "v_trunc" else h264_media["v"]
    a = h264_media["a_trunc"] if which == "a_trunc" else h264_media["a"]
    o = tmp_path / "o.mp4"
    r = subprocess.run([FFMPEG, "-y", "-loglevel", "info", "-nostats", "-hide_banner", "-i", str(v), "-i", str(a),
                        "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-shortest", str(o)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    info = v94._v94_parse_mux_log(r.stderr)
    ok, why = v94._v94_strict_mux_check(info)
    if expect:
        assert ok is True, (why, info)
    else:
        assert ok is not True, (why, info)    # False (bằng chứng hỏng) hoặc None → ffprobe fallback quyết định
        if ok is None:
            p = v94._ffprobe_av_info(o)
            ins = [x for x in (info.get("video_duration"), info.get("audio_duration")) if x]
            assert ins and (p.get("video_duration") or 0) < min(ins) - 0.3


def test_strict_parser_ffmpeg7_kib_and_missing_stats(v94):
    txt = ("Input #0, mov,mp4, from 'v':\n  Duration: 00:00:58.17, start: 0\n"
           "Input #1, mov,mp4, from 'a':\n  Duration: 00:00:58.30, start: 0\n"
           "Output #0, mp4, to 'o':\n  Stream #0:0: Video: h264, yuv420p, 480x854\n  Stream #0:1: Audio: aac\n"
           "[out#0/mp4 @ 0x1] video:2701KiB audio:342KiB subtitle:0KiB other streams:0KiB muxing overhead: 1%\n"
           "size=    3106KiB time=00:00:58.28 bitrate= 437kbits/s speed=900x\n")
    info = v94._v94_parse_mux_log(txt)
    assert info["out_video_kb"] == 2701 and info["out_audio_kb"] == 342 and abs(info["out_time"] - 58.28) < 1e-6
    assert v94._v94_strict_mux_check(info) == (True, "OK")
    no_stats = "\n".join(l for l in txt.splitlines() if "time=" not in l and "video:" not in l)
    ok, why = v94._v94_strict_mux_check(v94._v94_parse_mux_log(no_stats))
    assert ok is None and why in ("no_out_time", "no_stream_sizes")


# ============================================================================ race-all + alt-ban-strict
def _two_hosts(g, data, main_delay=0.2, alt_delay=0.05, alt_mode="ok", alt_code=403):
    real = g.add("/videoplayback/real", data)
    main = g.add("/videoplayback/main", mode="redirect", to=real, hdr_delay=main_delay)
    if alt_mode == "status":
        alt = g.add("/videoplayback/alt", mode="status", code=alt_code)
    else:
        alt = g.add("/videoplayback/alt", data, hdr_delay=alt_delay)
    return main, alt


def _dl(v94, url, tmp_path, name, expected, kind, info=None, logs=None):
    info = {} if info is None else info
    ok = v94._download_http_range_turbo(url, tmp_path, name, (logs.append if logs is not None else (lambda m: None)),
                                        64 * 1024, expected, media_kind=kind, info=info)
    return ok, info


@pytest.mark.parametrize("size,cap,flag,expect_races", [
    (int(2.88 * MB), -1, 8, "all"), (24 * MB, -1, 8, "all"),      # mặc định V9.4.1 = V9.3: race hết
    (int(2.88 * MB), 4, 8, "all"), (24 * MB, 4, 8, 4),             # trần tuỳ chọn: file nhỏ vẫn race hết
    (int(2.88 * MB), 4, 0, 4)])                                     # = V9.4.0
def test_race_all_small_files(v94, monkeypatch, tmp_path, size, cap, flag, expect_races):
    monkeypatch.setattr(v94, "V94_FIRST_WAVE_RACE_MAX", cap)
    monkeypatch.setattr(v94, "V94_RACE_ALL_IF_WORKERS_LE", flag)
    data = make_payload(size, seed=size & 0xFFFF)
    with MockGVS() as g:
        main, alt = _two_hosts(g, data)
        monkeypatch.setattr(v94, "_gvs_alt_url", lambda u: alt)
        ok, info = _dl(v94, main, tmp_path, "r.bin", size, "video")
    assert ok, info
    assert sha256((tmp_path / "r.bin").read_bytes()) == sha256(data)
    want = info["workers_initial"] if expect_races == "all" else expect_races
    assert info["races"] == want, info


def test_race_all_faster_when_alt_faster(v94, monkeypatch, tmp_path):
    size = int(2.88 * MB)
    data = make_payload(size, seed=11)
    res = {}
    monkeypatch.setattr(v94, "V94_FIRST_WAVE_RACE_MAX", 4)
    for flag in (0, 8):
        monkeypatch.setattr(v94, "V94_RACE_ALL_IF_WORKERS_LE", flag)
        ts = []
        for rep in range(3):
            with MockGVS() as g:
                real = g.add("/videoplayback/real", data, rate=1_500_000)
                main = g.add("/videoplayback/main", mode="redirect", to=real, hdr_delay=0.25)
                alt = g.add("/videoplayback/alt", data, hdr_delay=0.03, rate=1_500_000)
                monkeypatch.setattr(v94, "_gvs_alt_url", lambda u, alt=alt: alt)
                t0 = time.perf_counter()
                ok, info = _dl(v94, main, tmp_path, f"f{flag}_{rep}.bin", size, "video")
                ts.append(time.perf_counter() - t0)
                assert ok and sha256((tmp_path / f"f{flag}_{rep}.bin").read_bytes()) == sha256(data)
            v94._MN_RACE_BAD_UNTIL[0] = 0.0
        res[flag] = sorted(ts)[1]
    assert res[8] <= res[0] + 0.05, res


@pytest.mark.parametrize("strict", [True, False])
def test_late_loser_timeout_does_not_ban_under_strict(v94, monkeypatch, tmp_path, strict):
    monkeypatch.setattr(v94, "V94_ALT_BAN_STRICT", strict)
    size = 340 * 1024
    data = make_payload(size, seed=5)
    logs = []
    with MockGVS() as g:
        real = g.add("/videoplayback/a", data)
        alt = g.add("/videoplayback/alt", data, hdr_delay=2.0)       # > read timeout audio 1.5s
        monkeypatch.setattr(v94, "_gvs_alt_url", lambda u: alt)
        ok, info = _dl(v94, real, tmp_path, "a.bin", size, "audio", logs=logs)
        assert ok and sha256((tmp_path / "a.bin").read_bytes()) == sha256(data)
        line = [l for l in logs if "HOÀN TẤT AUDIO" in l][0]
        assert "alt=n/a/" in line and "alt_lost=" in line, line
        time.sleep(2.3)                                               # chờ request thua timeout
        banned = v94._MN_RACE_BAD_UNTIL[0] > time.time()
        if strict:
            assert not banned
            vdata = make_payload(int(2.88 * MB), seed=6)
            vreal = g.add("/videoplayback/v", vdata)
            valt = g.add("/videoplayback/valt", vdata)
            monkeypatch.setattr(v94, "_gvs_alt_url", lambda u: valt)
            ok2, info2 = _dl(v94, vreal, tmp_path, "v.bin", len(vdata), "video")
            assert ok2 and info2["races"] > 0
        else:
            assert banned
            assert any("mn-race OFF 600s" in l and "after_end=True" in l for l in logs), logs


@pytest.mark.parametrize("strict", [True, False])
def test_alt_403_bans_under_both_rules(v94, monkeypatch, tmp_path, strict):
    monkeypatch.setattr(v94, "V94_ALT_BAN_STRICT", strict)
    size = int(2.88 * MB)
    data = make_payload(size, seed=7)
    logs = []
    with MockGVS() as g:
        real = g.add("/videoplayback/real", data, hdr_delay=0.15)
        alt = g.add("/videoplayback/alt", mode="status", code=403)
        monkeypatch.setattr(v94, "_gvs_alt_url", lambda u: alt)
        ok, info = _dl(v94, real, tmp_path, "x.bin", size, "video", logs=logs)
    assert ok and sha256((tmp_path / "x.bin").read_bytes()) == sha256(data)
    time.sleep(0.2)
    assert v94._MN_RACE_BAD_UNTIL[0] > time.time()
    assert any("mn-race OFF 600s" in l and "HTTP_403" in l for l in logs), logs


def test_alt_err_definitive_classification(v94):
    import urllib3
    assert v94._v94_alt_err_definitive(v94.V94RangeHTTPError(403))
    assert v94._v94_alt_err_definitive(v94.V94RangeHTTPError(429))
    assert not v94._v94_alt_err_definitive(v94.V94RangeHTTPError(503))
    assert not v94._v94_alt_err_definitive(urllib3.exceptions.ReadTimeoutError(None, "u", "read timed out"))
    assert not v94._v94_alt_err_definitive(urllib3.exceptions.ProtocolError("x"))
    assert v94._v94_alt_err_definitive(ConnectionRefusedError())


# ============================================================================ shorts-fanout-ab
def test_small_video_workers_scoped(v94):
    assert v94._range_worker_count(int(2.88 * MB), "video") == 6
    assert v94._range_worker_count(int(2.88 * MB), "video", 11) == 11
    assert v94._range_worker_count(5 * MB, "video", 11) == v94._range_worker_count(5 * MB, "video")
    assert v94._range_worker_count(int(0.34 * MB), "audio", 11) == v94._range_worker_count(int(0.34 * MB), "audio")


def test_small_video_workers_faster_when_per_conn_limited(v94, tmp_path):
    size = int(2.88 * MB)
    data = make_payload(size, seed=21)
    res = {}
    for svw in (0, 11):
        ts = []
        for rep in range(2):
            with MockGVS() as g:
                url = g.add("/videoplayback/s", data, rate=600_000, hdr_delay=0.3)
                info = {"small_video_workers": svw}
                t0 = time.perf_counter()
                ok, info = _dl(v94, url, tmp_path, f"s{svw}_{rep}.bin", size, "video", info=info)
                ts.append(time.perf_counter() - t0)
                assert ok and sha256((tmp_path / f"s{svw}_{rep}.bin").read_bytes()) == sha256(data)
                assert info["workers_initial"] == (11 if svw else 6)
        res[svw] = min(ts)
    assert res[11] < res[0], res


def test_small_video_workers_with_resize(v94, tmp_path):
    real = int(2.9 * MB)
    data = make_payload(real, seed=9)
    with MockGVS() as g:
        url = g.add("/videoplayback/rz", data)
        ok, info = _dl(v94, url, tmp_path, "rz.bin", int(2.7 * MB), "video", info={"small_video_workers": 11})
    assert ok and info["resized"] == [int(2.7 * MB), real]
    assert sha256((tmp_path / "rz.bin").read_bytes()) == sha256(data)


def test_ab_arms_alternate(v94, monkeypatch):
    monkeypatch.setattr(v94, "V94_VIDEO_SMALL_WORKERS_AB", "6,11")
    v94._V94_AB_COUNTER[0] = 0
    arms = v94._v94_ab_arms()
    picks = [arms[v94._v94_ab_next() % len(arms)] for _ in range(4)]
    assert picks == [6, 11, 6, 11]
    monkeypatch.setattr(v94, "V94_VIDEO_SMALL_WORKERS_AB", "")
    assert v94._v94_ab_arms() == []


# ============================================================================ range-request-telemetry
def test_gvs_req_spans_and_honest_line(v94, tmp_path, telem):
    size = 3 * MB
    data = make_payload(size, seed=33)
    logs = []
    with MockGVS() as g:
        real = g.add("/videoplayback/real", data, rate=2_000_000)
        url = g.add("/videoplayback/r", mode="redirect", to=real)
        ok, info = _dl(v94, url + "?sig=SECRET&pot=TOKEN", tmp_path, "t.bin", size, "video", logs=logs)
        n_real = len(g.requests_for("/videoplayback/real"))
    assert ok
    rows = telem.drain()
    reqs = [r for r in rows if r["ev"] == "gvs_req"]
    assert len(reqs) == n_real, (len(reqs), n_real)
    assert all(r["host"] in ("main", "alt") for r in reqs)
    blob = json.dumps(rows)
    assert "SECRET" not in blob and "TOKEN" not in blob and "videoplayback?" not in blob
    first = [r for r in reqs if r["req"] == "first"]
    assert first and all(r["t_30x_ms"] is not None for r in first)   # redirect được tách khỏi TTFB edge
    line = [l for l in logs if "HOÀN TẤT VIDEO" in l][0]
    assert re.search(r"HOÀN TẤT VIDEO \| [\d.]+MB \| ([\d.]+)s", line)
    for f in ("xfer≈", "hedge_real=", "gets=", "conc_peak=", "splits=", "takeovers=", "prealloc="):
        assert f in line, line
    assert info["prealloc_ms"] >= 0 and "first_get_ns" in info


# ============================================================================ excel-atomic-replace + row verify
def _xlsx(path, urls, extra_rows_before=0):
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active
    ws.append(["URL", "CHANNEL ID", "STATUS"])
    for _ in range(extra_rows_before):
        ws.append(["https://www.youtube.com/shorts/ZZZZZZZZZZZ", "UC", ""])
    for u in urls:
        ws.append([u, "UC", ""])
    wb.save(path)


def _status(path):
    from openpyxl import load_workbook
    ws = load_workbook(path).worksheets[0]
    return [(ws.cell(row=r, column=1).value, ws.cell(row=r, column=3).value) for r in range(2, ws.max_row + 1)]


def test_atomic_replace_no_partial_reads(v94, monkeypatch, tmp_path):
    import pandas as pd
    p = str(tmp_path / "check.xlsx")
    urls = [f"https://www.youtube.com/shorts/AAAAAAAA{i:03d}" for i in range(60)]
    _xlsx(p, urls)
    real_rename = os.rename

    def win_rename(src, dst, *a, **k):                    # ngữ nghĩa Windows: đích tồn tại → lỗi
        if os.path.exists(dst):
            raise FileExistsError(dst)
        return real_rename(src, dst, *a, **k)
    monkeypatch.setattr(os, "rename", win_rename)
    bad = [0]
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                pd.read_excel(p, sheet_name=0, dtype=str)
            except Exception:
                bad[0] += 1
    th = threading.Thread(target=reader); th.start()
    stats_all = []
    try:
        for i in range(25):
            st = {}
            assert v94.write_status_to_excel(p, i, "STATUS", "DONE", lambda m: None,
                                             expected_url=urls[i], url_col_name="URL", stats=st)
            stats_all.append(st)
    finally:
        stop.set(); th.join(5)
    assert bad[0] == 0
    assert all(s.get("done_replace_ms") is not None for s in stats_all)
    assert sum(1 for _, s in _status(p) if s == "DONE") == 25


def test_replace_retry_then_fallback(v94, monkeypatch, tmp_path):
    p = str(tmp_path / "c.xlsx")
    urls = [f"https://youtu.be/BBBBBBBB{i:03d}" for i in range(5)]
    _xlsx(p, urls)
    real_replace = os.replace
    fails = [3]

    def flaky(src, dst):
        if fails[0] > 0:
            fails[0] -= 1
            raise PermissionError("locked")
        return real_replace(src, dst)
    monkeypatch.setattr(v94.os, "replace", flaky)
    st = {}
    assert v94.write_status_to_excel(p, 0, "STATUS", "DONE", lambda m: None, stats=st)
    assert st["replace_retries"] == 3 and not st.get("replace_fallback")
    monkeypatch.setattr(v94.os, "replace", lambda s, d: (_ for _ in ()).throw(PermissionError("x")))
    st2 = {}
    assert v94.write_status_to_excel(p, 1, "STATUS", "DONE", lambda m: None, stats=st2)
    assert st2.get("replace_fallback") is True
    assert [s for _, s in _status(p)][:2] == ["DONE", "DONE"]


def test_status_write_failure_returns_false(v94, monkeypatch, tmp_path):
    p = str(tmp_path / "f.xlsx")
    _xlsx(p, ["https://youtu.be/CCCCCCCCCCC"])
    from openpyxl.workbook.workbook import Workbook
    monkeypatch.setattr(Workbook, "save", lambda self, path: (_ for _ in ()).throw(OSError("disk")))
    logs = []
    assert v94.write_status_to_excel(p, 0, "STATUS", "DONE", logs.append) is False
    assert any("Lỗi ghi Excel" in l for l in logs)


def test_row_verify_follows_inserted_row(v94, tmp_path):
    p = str(tmp_path / "v.xlsx")
    X = "https://www.youtube.com/shorts/XXXXXXXXXXX"
    _xlsx(p, [X], extra_rows_before=0)
    # scanner chèn 1 dòng lên trên trong lúc job chạy → X giờ ở dòng 3 (idx cũ = 0)
    from openpyxl import load_workbook
    wb = load_workbook(p); ws = wb.active
    ws.insert_rows(2)
    ws.cell(row=2, column=1).value = "https://www.youtube.com/shorts/YYYYYYYYYYY"
    wb.save(p)
    logs = []
    assert v94.write_status_to_excel(p, 0, "STATUS", "DONE", logs.append, expected_url=X, url_col_name="URL")
    st = _status(p)
    assert st[0] == ("https://www.youtube.com/shorts/YYYYYYYYYYY", None)
    assert st[1] == (X, "DONE")
    assert any("row moved 2→3" in l for l in logs)


def test_row_verify_deleted_row_no_done(v94, tmp_path):
    p = str(tmp_path / "d.xlsx")
    X = "https://www.youtube.com/shorts/XXXXXXXXXXX"
    _xlsx(p, ["https://www.youtube.com/shorts/QQQQQQQQQQQ", X])
    from openpyxl import load_workbook
    wb = load_workbook(p); ws = wb.active
    ws.delete_rows(3)                 # dòng của X biến mất
    ws.cell(row=3, column=1).value = "https://www.youtube.com/shorts/WWWWWWWWWWW"
    wb.save(p)
    logs = []
    assert v94.write_status_to_excel(p, 1, "STATUS", "DONE", logs.append, expected_url=X, url_col_name="URL") is False
    assert all(s != "DONE" for _, s in _status(p))
    assert any("row mismatch" in l for l in logs)


def test_row_verify_hyperlink_and_blank(v94, tmp_path):
    p = str(tmp_path / "h.xlsx")
    X = "https://www.youtube.com/shorts/XXXXXXXXXXX"
    _xlsx(p, ['=HYPERLINK("' + X + '","x")', "=A2"])
    assert v94.write_status_to_excel(p, 0, "STATUS", "DONE", lambda m: None, expected_url=X, url_col_name="URL")
    logs = []
    assert v94.write_status_to_excel(p, 1, "STATUS", "DONE", logs.append,
                                     expected_url="https://www.youtube.com/shorts/KKKKKKKKKKK", url_col_name="URL")
    assert [s for _, s in _status(p)] == ["DONE", "DONE"]


def test_row_verify_flag_off_positional(v94, monkeypatch, tmp_path):
    monkeypatch.setattr(v94, "V94_STATUS_VERIFY_URL", False)
    p = str(tmp_path / "o.xlsx")
    _xlsx(p, ["https://www.youtube.com/shorts/QQQQQQQQQQQ"])
    assert v94.write_status_to_excel(p, 0, "STATUS", "DONE", lambda m: None,
                                     expected_url="https://www.youtube.com/shorts/XXXXXXXXXXX", url_col_name="URL")
    assert _status(p)[0][1] == "DONE"


# ============================================================================ ja-infer-strict
def _fmt(itag, track=None, drc=False):
    f = {"itag": int(itag), "url": f"https://x/{itag}", "mimeType": 'audio/mp4; codecs="mp4a.40.5"',
         "bitrate": 50000 if itag == 139 else 130000}
    if track:
        f["audioTrack"] = track
    if drc:
        f["isDrc"] = True
    return f


JA_CASES = {
    "single_track_id_en": ([_fmt(139, {"id": "en.4", "displayName": "English original"}),
                            _fmt(140, {"id": "en.4", "displayName": "English original"})],
                           {"videoDetails": {"title": "しんちゃん clip", "author": "x"}}),
    "two_tracks_no_meta": ([_fmt(139), _fmt(140), _fmt(139), _fmt(140)],
                           {"videoDetails": {"title": "しんちゃん clip", "author": "x"}}),
    "translated_micro_title": ([_fmt(139), _fmt(140)],
                               {"videoDetails": {"title": "Funny cat compilation", "author": "CatsDaily"},
                                "microformat": {"playerMicroformatRenderer": {"title": {"simpleText": "面白い猫のまとめ"}}}}),
    "display_name_ja_english": ([_fmt(139, {"id": "en.4", "displayName": "英語（オリジナル）"})],
                                {"videoDetails": {"title": "しんちゃん", "author": "x"}}),
}


@pytest.mark.parametrize("case", sorted(JA_CASES))
def test_ja_infer_strict_refuses(v94, v93, case):
    fm, data = JA_CASES[case]
    assert v94._pick_native_ja_audio([dict(f) for f in fm], json.loads(json.dumps(data))) is None


def test_ja_infer_strict_keeps_genuine_cases(v94):
    fm = [_fmt(139), _fmt(140), _fmt(140, drc=True)]          # 1 track + biến thể DRC (không phải track khác)
    pick = v94._pick_native_ja_audio(fm, {"videoDetails": {"title": "【神回】しんちゃん", "author": "x"}})
    assert pick and pick["itag"] == 139 and pick.get("_v85_inferred_single_ja")
    assert pick["_v94_infer"]["kana_source"] == "title"
    fm2 = [_fmt(140, {"id": "en.4", "displayName": "English"}), _fmt(139, {"id": "ja.4", "displayName": "日本語"})]
    pick2 = v94._pick_native_ja_audio(fm2, {"videoDetails": {"title": "x"}})
    assert pick2 and pick2["itag"] == 139 and not pick2.get("_v85_inferred_single_ja")


def test_ja_infer_records_asr_without_acting(v94):
    data = {"videoDetails": {"title": "しんちゃん clip", "author": "x"},
            "captions": {"playerCaptionsTracklistRenderer": {"captionTracks": [{"languageCode": "en", "kind": "asr"}]}}}
    pick = v94._pick_native_ja_audio([_fmt(139)], data)
    assert pick and pick["_v94_infer"]["asr_lang"] == "en"


def test_ja_infer_flag_off_reproduces_v94(v94, monkeypatch):
    monkeypatch.setattr(v94, "V94_JA_INFER_STRICT", False)
    fm, data = JA_CASES["single_track_id_en"]
    pick = v94._pick_native_ja_audio([dict(f) for f in fm], json.loads(json.dumps(data)))
    assert pick is not None and pick["itag"] == 139       # hành vi cũ (sai) được giữ khi tắt cờ


def test_ja_refuse_reason_reaches_bundle(v94, monkeypatch):
    data = {"playabilityStatus": {"status": "OK"}, "videoDetails": {"videoId": "AbCdEfGhIjK", "title": "しんちゃん"},
            "streamingData": {"adaptiveFormats": [
                {"itag": 135, "url": "https://v/135", "mimeType": "video/mp4", "contentLength": "1000"},
                _fmt(139, {"id": "en.4", "displayName": "English original"})]}}
    monkeypatch.setattr(v94, "_get_innertube_client", lambda k: {"INNERTUBE_CONTEXT": {"client": {"clientVersion": "1"}}})

    payload = json.dumps(data).encode()

    class _R:
        status = 200
        data = payload
    monkeypatch.setattr(v94, "_get_fast_pool", lambda: type("P", (), {"request": lambda self, *a, **k: _R()})())
    b = v94._fast_player_request_480("AbCdEfGhIjK", "visionos")
    assert b and b["video_url"] and not b["audio_url"]
    assert b["audio_refuse_reason"].startswith("track_id:en")


# ============================================================================ ffprobe-duration-timeout
def test_ffprobe_duration_timeout(v94, monkeypatch, tmp_path):
    fake = _write_fake_exe(tmp_path / "ffprobe_hang", "import time\ntime.sleep(30)\n")
    monkeypatch.setattr(v94, "V94_FFPROBE_TIMEOUT", 1.0)
    t0 = time.perf_counter()
    assert v94._ffprobe_duration_seconds(fake, tmp_path / "x.mp4") == 0.0
    assert time.perf_counter() - t0 < 3.0


@need_ff
def test_ffprobe_duration_uses_given_path(v94, h264_media):
    assert abs(v94._ffprobe_duration_seconds(FFPROBE, h264_media["v"]) - 30.0) < 0.6


# ============================================================================ pipeline-level (Worker thật)
from test_pipeline_v94 import _make_master as _pl_master, _xlsx as _pl_xlsx  # noqa: E402


def _run_job(v94, monkeypatch, tmp_path, vfile, afile, dur, vid="AbCdEfGhIjK", itag="135", extra=None):
    from conftest import get_qapp
    get_qapp()
    url = f"https://www.youtube.com/shorts/{vid}"
    monkeypatch.setattr(v94, "ARIA2C_PATH", "/bin/true")
    with MockGVS() as g:
        vurl = g.add("/videoplayback/v", Path(vfile).read_bytes())
        aurl = g.add("/videoplayback/a", Path(afile).read_bytes())
        bundle = {"id": vid, "title": "テスト動画", "video_url": vurl, "video_format_id": itag,
                  "video_content_length": Path(vfile).stat().st_size, "audio_url": aurl, "audio_format_id": "139",
                  "audio_content_length": Path(afile).stat().st_size, "audio_mime": "audio/mp4",
                  "duration_sec": float(dur), "source": "NATIVE_VISIONOS", "resolver_ms": 1.0}
        if extra:
            bundle.update(extra)
        monkeypatch.setattr(v94, "_fast_innertube_480", lambda u: dict(bundle))
        monkeypatch.setattr(v94, "_ytdlp_resolve_480_on_warm_thread", lambda u, robust=False: None)
        xl = tmp_path / "check.xlsx"
        _pl_xlsx(str(xl), url)
        v94._EXCEL_DF_CACHE.clear()
        master = _pl_master(v94, tmp_path)
        w = v94.Worker(master, str(xl), "URL", "STATUS")
        w._process_single_url(url, 1)
    from openpyxl import load_workbook
    st = load_workbook(str(xl)).worksheets[0].cell(row=3, column=6).value
    return w, master, st


@need_ff
def test_pipeline_job_never_waits_for_prewarm(v94, monkeypatch, tmp_path, h264_media):
    hot = {}
    for mode in ("off", "slow"):
        if mode == "off":
            monkeypatch.setattr(v94, "V94_FF_PREWARM", False)
        else:
            monkeypatch.setattr(v94, "V94_FF_PREWARM", True)
            monkeypatch.setattr(v94, "_v94_ff_prewarm", lambda *a, **k: time.sleep(2.0) or {"ffmpeg_ms": 2000})
            with v94._V94_FF_LOCK:
                v94._V94_FF_STATE["last_exit"]["ffmpeg"] = time.perf_counter() - 60
        d = tmp_path / mode
        d.mkdir()
        w, master, st = _run_job(v94, monkeypatch, d, h264_media["v"], h264_media["a"], 30)
        assert st == "DONE", "\n".join(master.logs[-30:])
        hot[mode] = w._v94["kpi"]["hot_ready_ms"]
    assert hot["slow"] <= hot["off"] + 150, hot       # job không chờ prewarm 2s


@need_ff
@pytest.mark.parametrize("which", ["v_trunc", "a_trunc"])
def test_pipeline_truncated_input_never_done(v94, monkeypatch, tmp_path, h264_media, which):
    v = h264_media["v_trunc"] if which == "v_trunc" else h264_media["v"]
    a = h264_media["a_trunc"] if which == "a_trunc" else h264_media["a"]
    w, master, st = _run_job(v94, monkeypatch, tmp_path, v, a, 30)
    log = "\n".join(master.logs)
    assert st in (None, ""), log[-2000:]
    assert master.done and master.done[-1][2] is False
    assert w._v94["fail"].startswith(("validate_fail", "mux_fail")), w._v94["fail"]
    final = tmp_path / "final"
    assert not final.exists() or not any(p.is_file() for p in final.rglob("*")), list(final.rglob("*"))


@need_ff
def test_pipeline_kpi_line_and_post_line(v94, monkeypatch, tmp_path, h264_media, telem):
    w, master, st = _run_job(v94, monkeypatch, tmp_path, h264_media["v"], h264_media["a"], 30)
    assert st == "DONE"
    log = "\n".join(master.logs)
    kpi = [l for l in master.logs if "📊 V9.4 KPI" in l][-1]
    for f in ("KPI1=", "KPI3=", "first_get=", "valid=", "post=", "done=", "[V9.7-SUB2S]"):
        assert f in kpi, kpi
    assert "📮 POST:" in log and "done_load=" in log and "prepend_ff=" in log
    k = w._v94["kpi"]
    order = [k["hot_ready_ms"], k["file_validated_ms"], k["postprocess_ms"], k["status_done_ms"], k["pipeline_total_ms"]]
    assert order == sorted(order), order
    assert abs(k["media_ready_ms"] - max(k["video_done_at_ms"], k["audio_done_at_ms"])) < 50
    assert k["first_get_ms"] >= 0
    assert "strict=OK" in log and "VALIDATE mux-log" in log
    hot_line = [l for l in master.logs if "HOT 480P READY" in l][0]
    assert "135+139" in hot_line
    rows = telem.drain()
    evs = {r["ev"] for r in rows}
    assert {"mux_diag", "file_validated", "job_summary", "gvs_req"} <= evs


@need_ff
def test_pipeline_ab_label(v94, monkeypatch, tmp_path, h264_media):
    monkeypatch.setattr(v94, "V94_VIDEO_SMALL_WORKERS_AB", "6,11")
    v94._V94_AB_COUNTER[0] = 0
    seen = []
    for i in range(2):
        d = tmp_path / f"j{i}"
        d.mkdir()
        w, master, st = _run_job(v94, monkeypatch, d, h264_media["v"], h264_media["a"], 30)
        assert st == "DONE"
        line = [l for l in master.logs if "HOÀN TẤT VIDEO" in l][0]
        k = w._v94["kpi"]
        if int(w._v94["video"].get("total") or 0) <= 4 * MB:
            seen.append(k["ab_small_workers"])
            assert f"ab={seen[-1]}" in line
        else:                                   # video > 4MiB: nhánh A/B không áp dụng → không gắn nhãn
            seen.append(k["ab_small_workers_ignored"])
            assert " ab=" not in line
    assert seen == [6, 11]
