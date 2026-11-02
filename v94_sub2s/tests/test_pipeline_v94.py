# -*- coding: utf-8 -*-
"""End-to-end offline: _process_single_url với resolver giả + mock GVS + ffmpeg thật.

Kiểm tra: 244(VP9)+JA audio tải song song → mux copy → ffprobe hợp lệ → nhánh cắt
<60 (bù 60.2s) / 60–300 (không cắt) / >300 (direct-cut) → MOVE đúng folder → DONE đúng dòng.
Media thật (ffmpeg) chỉ thay cho GoogleVideo; không chạm YouTube.
"""
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from mock_gvs import MockGVS

FFMPEG = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(not FFMPEG, reason="cần ffmpeg")


def _run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    out = {}
    for dur, size, rate in ((30, "854x480", 10), (120, "480x854", 5), (310, "320x180", 2)):
        v = d / f"v{dur}.webm"
        _run([FFMPEG, "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}", "-t", str(dur),
              "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-b:v", "150k",
              "-g", str(rate * 2), "-an", str(v)])
        a = d / f"a{dur}.m4a"
        _run([FFMPEG, "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}",
              "-c:a", "aac", "-b:a", "48k", str(a)])
        out[dur] = (v, a)
    o = d / "a30.webm"
    _run([FFMPEG, "-y", "-f", "lavfi", "-i", "sine=frequency=300:duration=30", "-c:a", "libopus",
          "-b:a", "48k", str(o)])
    out["opus30"] = o
    return out


def test_mux_vp9_aac_and_validate(v94, media, tmp_path):
    v, a = media[30]
    out = tmp_path / "m.mp4"
    t0 = time.perf_counter()
    assert v94.mux_480_stream_copy(v, a, out, lambda m: None)
    mux_ms = (time.perf_counter() - t0) * 1000
    info = v94._ffprobe_av_info(out)
    assert info["has_video"] and info["has_audio"] and info["height"] == 480
    assert abs(info["video_duration"] - 30) < 1.0 and abs(info["audio_duration"] - 30) < 1.0
    print(f"mux_ms={mux_ms:.1f}")


def test_mux_log_parser_matches_ffprobe(v94, media, tmp_path):
    v, a = media[30]
    out = tmp_path / "p.mp4"
    assert v94.mux_480_stream_copy(v, a, out, lambda m: None)
    got = v94._V94_MUX_INFO.pop(str(out))
    ref = v94._ffprobe_av_info(out)
    assert got["has_video"] and got["has_audio"]
    assert (got["width"], got["height"]) == (ref["width"], ref["height"]) == (854, 480)
    assert abs(got["format_duration"] - ref["format_duration"]) < 0.2


def test_mux_missing_audio_stream_fails(v94, media, tmp_path):
    """File 'audio' thực ra không có track audio → mux phải FAIL (→ không DONE)."""
    v, _ = media[30]
    out = tmp_path / "x.mp4"
    assert not v94.mux_480_stream_copy(v, v, out, lambda m: None)


def test_mux_vp9_opus_webm_audio(v94, media, tmp_path):
    v, _ = media[30]
    out = tmp_path / "o.mp4"
    assert v94.mux_480_stream_copy(v, media["opus30"], out, lambda m: None)
    info = v94._ffprobe_av_info(out)
    assert info["has_video"] and info["has_audio"]


class _Txt:
    def __init__(self, v): self.v = v
    def text(self): return self.v


class _Chk:
    def __init__(self, v): self.v = v
    def isChecked(self): return self.v


class _Combo:
    def currentData(self): return "aria2c"


def _qt_app_if_real():
    from conftest import get_qapp
    return get_qapp()


def _make_master(v94, root: Path, cutsec="100"):
    m = type("M", (), {})()
    m.worker_signals = v94.WorkerSignals()
    m.logs = []
    m.done = []
    m.worker_signals.log_message.connect(lambda s: m.logs.append(s))
    m.worker_signals.url_finished.connect(lambda u, i, ok: m.done.append((u, i, ok)))
    m.out_entry = _Txt(str(root / "out"))
    m.keep_temp_checkbox = _Chk(False)
    m.final_move_entry = _Txt(str(root / "final"))
    m.combo_downloader = _Combo()
    m.cutsec_entry = _Txt(cutsec)
    m.realtime_stop_event = threading.Event()
    return m


def _xlsx(path, url):
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active
    ws.append(["URL", "CHANNEL ID", "TÊN KÊNH", "TITLE", "LINK VIDEO GỐC", "STATUS"])
    ws.append(["https://www.youtube.com/watch?v=ZZZZZZZZZZZ", "UC0", "k", "khác", "g0", "DONE"])
    ws.append([url, "UC1", "kênh", "テスト動画タイトル", "goc_folder", ""])
    wb.save(path)


@pytest.mark.parametrize("dur", [30, 120, 310])
def test_process_single_url_end_to_end(v94, media, tmp_path, monkeypatch, dur):
    _qt_app = _qt_app_if_real()
    v, a = media[dur]
    vid = "AbCdEfGhIjK"
    url = f"https://www.youtube.com/shorts/{vid}"
    monkeypatch.setattr(v94, "ARIA2C_PATH", "/bin/true")
    with MockGVS() as g:
        vurl = g.add("/videoplayback/v", v.read_bytes())
        aurl = g.add("/videoplayback/a", a.read_bytes())
        bundle = {
            "id": vid, "title": "テスト動画タイトル", "video_url": vurl, "video_format_id": "244",
            "video_content_length": v.stat().st_size, "audio_url": aurl, "audio_format_id": "140",
            "audio_content_length": a.stat().st_size, "audio_mime": "audio/mp4", "duration_sec": float(dur),
            "source": "NATIVE_VISIONOS", "resolver_ms": 1.0,
        }
        monkeypatch.setattr(v94, "_fast_innertube_480", lambda u: dict(bundle))
        monkeypatch.setattr(v94, "_ytdlp_resolve_480_on_warm_thread", lambda u, robust=False: None)
        xl = tmp_path / "check.xlsx"
        _xlsx(str(xl), url)
        v94._EXCEL_DF_CACHE.clear()
        master = _make_master(v94, tmp_path)
        links, ucol, scol = v94.read_links_excel_url_only(str(xl), lambda m: None)
        assert links == [(url, 1)]
        w = v94.Worker(master, str(xl), ucol, scol)
        assert not w._critical_error
        t0 = time.perf_counter()
        w._process_single_url(url, 1)
        total_ms = (time.perf_counter() - t0) * 1000
    log = "\n".join(master.logs)
    assert master.done and master.done[-1][2] is True, log[-3000:]
    from openpyxl import load_workbook
    ws = load_workbook(str(xl)).worksheets[0]
    assert ws.cell(row=3, column=6).value == "DONE"
    assert ws.cell(row=2, column=6).value == "DONE"     # dòng khác không bị đụng
    final = tmp_path / "final" / "goc_folder"
    assert final.is_dir()
    files = [p for p in final.rglob("*") if p.is_file()]
    assert files, "không có file nào được MOVE"
    if dur < 60:
        info = v94._ffprobe_av_info(files[0])
        assert info["has_audio"] and info["video_duration"] >= 60.0
    elif dur <= 300:
        assert len(files) == 1
        info = v94._ffprobe_av_info(files[0])
        assert info["has_video"] and info["has_audio"] and abs(info["format_duration"] - dur) < 1.5
        assert "VALIDATE mux-log" in log
    else:
        assert len(files) >= 3       # 310s / 100s → nhiều part
        assert "DIRECT-CUT" in log
        for f in files:
            inf = v94._ffprobe_av_info(f)
            assert inf["has_video"] and inf["has_audio"]
    assert "📊 V9.4 KPI" in log
    kpi = [l for l in log.splitlines() if "V9.4 KPI" in l][0]
    print(f"\n[dur={dur}] total_ms={total_ms:.0f} | {kpi}")


def test_process_fails_without_done_when_audio_403(v94, media, tmp_path, monkeypatch):
    _qt_app = _qt_app_if_real()
    v, a = media[120]
    vid = "AbCdEfGhIjQ"
    url = f"https://www.youtube.com/shorts/{vid}"
    monkeypatch.setattr(v94, "ARIA2C_PATH", "/bin/true")
    aria = []
    monkeypatch.setattr(v94, "download_aria2c_ultimate", lambda *a_, **k: aria.append(1) or False)
    monkeypatch.setattr(v94, "_download_aria2c_rpc", lambda *a_, **k: aria.append(1) or False)
    with MockGVS() as g:
        vurl = g.add("/videoplayback/v", v.read_bytes())
        aurl = g.add("/videoplayback/a403", mode="status", code=403)
        bundle = {"id": vid, "title": "t", "video_url": vurl, "video_format_id": "244",
                  "video_content_length": v.stat().st_size, "audio_url": aurl, "audio_format_id": "140",
                  "audio_content_length": a.stat().st_size, "duration_sec": 120.0, "source": "NATIVE_VISIONOS"}
        monkeypatch.setattr(v94, "_fast_innertube_480", lambda u: dict(bundle))
        refresh = []
        monkeypatch.setattr(v94, "_ytdlp_resolve_480_on_warm_thread",
                            lambda u, robust=False: refresh.append(robust) or None)
        xl = tmp_path / "check.xlsx"
        _xlsx(str(xl), url)
        v94._EXCEL_DF_CACHE.clear()
        master = _make_master(v94, tmp_path)
        w = v94.Worker(master, str(xl), "URL", "STATUS")
        w._process_single_url(url, 1)
    assert master.done[-1][2] is False
    from openpyxl import load_workbook
    assert load_workbook(str(xl)).worksheets[0].cell(row=3, column=6).value in (None, "")
    assert not aria, "không được gọi aria2 với signed URL đã 403"
    assert True in refresh, "phải có 1 SAFE-REFRESH robust"
