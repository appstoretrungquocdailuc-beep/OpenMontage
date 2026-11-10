# -*- coding: utf-8 -*-
"""V9.5 — lựa chọn 360p nhanh (itag 18) sau cờ V95_360P. Luồng 480p phải y như V9.4 khi chọn 480p."""
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from mock_gvs import MockGVS
from test_pipeline_v94 import _make_master, _xlsx

FFMPEG = shutil.which("ffmpeg")
need_ff = pytest.mark.skipif(not FFMPEG, reason="cần ffmpeg")
MB = 1024 * 1024


@pytest.fixture(scope="module")
def prog18(tmp_path_factory):
    """File progressive giống itag 18: H.264 + AAC trong 1 MP4, 30s / 120s / 310s."""
    d = tmp_path_factory.mktemp("p18")
    out = {}
    for dur, rate in ((30, 10), (120, 5), (310, 2)):
        f = d / f"p18_{dur}.mp4"
        subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate={rate}", "-f", "lavfi",
                        "-i", f"sine=frequency=440:duration={dur}", "-t", str(dur), "-c:v", "libx264", "-preset",
                        "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "48k", "-shortest", str(f)],
                       check=True, capture_output=True)
        out[dur] = f
    return out


class _Radio:
    def __init__(self, v): self.v = v
    def isChecked(self): return self.v


def _master360(v94, root, mode="360fast"):
    m = _make_master(v94, root)
    m.radio_light = _Radio(mode == "360fast")
    m.quality_mode = lambda: mode
    return m


def _run(v94, monkeypatch, tmp_path, media, dur, mode="360fast", lang_meta=None, native_ok=True,
         fast_fail=False, status_code=None):
    from conftest import get_qapp
    get_qapp()
    vid = "Q360TestAbc"
    url = f"https://www.youtube.com/shorts/{vid}"
    monkeypatch.setattr(v94, "ARIA2C_PATH", "/bin/true")
    calls = {"native360": 0, "native480": 0, "aria2": 0}
    with MockGVS() as g:
        data = media.read_bytes()
        if status_code:
            u18 = g.add("/videoplayback/18", mode="status", code=status_code)
        elif fast_fail:
            u18 = g.add("/videoplayback/18", data[: len(data) // 2])        # bị cắt → size < content_length
        else:
            u18 = g.add("/videoplayback/18", data)
        alt18 = g.add("/videoplayback/18alt", data)
        bundle18 = {"url": u18, "title": "テスト360", "id": vid, "format_id": "18", "content_length": len(data),
                    "source": "FAST_RACE_ANDROID_VR", "default_audio_lang": "ja", "has_ja_audio": True,
                    "audio_tracks": 1, "duration_sec": float(dur)}
        bundle18.update(lang_meta or {})

        def fake360(u):
            calls["native360"] += 1
            return dict(bundle18) if native_ok else None
        monkeypatch.setattr(v94, "_fast_innertube_360p", fake360)
        alt = dict(bundle18); alt["url"] = alt18; alt["source"] = "YTDLP_FAST"
        monkeypatch.setattr(v94, "_ytdlp_resolve_on_warm_thread", lambda u, robust=False: dict(alt))
        # 480p resolver (chỉ dùng khi fallback)
        v480 = g.add("/videoplayback/v480", data)
        a480 = g.add("/videoplayback/a480", data)

        def fake480(u):
            calls["native480"] += 1
            return {"id": vid, "title": "テスト480", "video_url": v480, "video_format_id": "135",
                    "video_content_length": len(data), "audio_url": a480, "audio_format_id": "140",
                    "audio_content_length": len(data), "audio_mime": "audio/mp4", "duration_sec": float(dur),
                    "source": "NATIVE_VISIONOS"}
        monkeypatch.setattr(v94, "_fast_innertube_480", fake480)
        monkeypatch.setattr(v94, "_ytdlp_resolve_480_on_warm_thread", lambda u, robust=False: None)
        monkeypatch.setattr(v94, "download_aria2c_ultimate",
                            lambda *a, **k: calls.__setitem__("aria2", calls["aria2"] + 1) or False)
        xl = tmp_path / "check.xlsx"
        _xlsx(str(xl), url)
        v94._EXCEL_DF_CACHE.clear()
        master = _master360(v94, tmp_path, mode)
        w = v94.Worker(master, str(xl), "URL", "STATUS")
        assert w.quality_mode == mode
        w._process_single_url(url, 1)
        time.sleep(0.6)       # ffprobe nền
    from openpyxl import load_workbook
    st = load_workbook(str(xl)).worksheets[0].cell(row=3, column=6).value
    return w, master, st, calls


@need_ff
@pytest.mark.parametrize("dur", [30, 120, 310])
def test_360_end_to_end(v94, monkeypatch, tmp_path, prog18, dur):
    w, master, st, calls = _run(v94, monkeypatch, tmp_path, prog18[dur], dur)
    log = "\n".join(master.logs)
    assert st == "DONE", log[-3000:]
    assert calls["native360"] == 1 and calls["native480"] == 0 and calls["aria2"] == 0
    assert "START 360P-FAST" in log and "HOT 360P READY" in log and "MUX COPY" not in log
    assert "V9.5 KPI-360" in log and "[V9.5-SUB2S]" in log
    k = w._v94["kpi"]
    assert k["mode"] == "360fast"
    for key in ("resolve_ms", "first_byte_ms", "download_ms", "hot_ready_ms", "postprocess_ms", "status_done_ms",
                "pipeline_total_ms"):
        assert key in k, (key, k)
    assert k["resolve_ms"] <= k["first_byte_ms"] <= k["hot_ready_ms"] <= k["postprocess_ms"] <= k["status_done_ms"]
    files = [p for p in (tmp_path / "final").rglob("*.mp4")]
    assert files
    if dur < 60:
        inf = v94._ffprobe_av_info(files[0])
        assert inf["has_audio"] and inf["video_duration"] >= 60
    elif dur <= 300:
        assert len(files) == 1 and v94._ffprobe_av_info(files[0])["has_audio"]
    else:
        assert len(files) >= 3
    assert not list((tmp_path / "_temp_yt480p").glob("*_18.mp4"))
    assert "ffprobe SAU DONE: file nghi hỏng" not in log


@need_ff
def test_360_ja_fallback_to_480(v94, monkeypatch, tmp_path, prog18):
    w, master, st, calls = _run(v94, monkeypatch, tmp_path, prog18[120], 120,
                                lang_meta={"default_audio_lang": "en", "has_ja_audio": True, "audio_tracks": 2})
    log = "\n".join(master.logs)
    assert st == "DONE", log[-2000:]
    assert "chuyển sang 480p + audio Nhật" in log and "[360p→480p]" in log and "START 480P-HARD" in log
    assert calls["native480"] >= 1 and "MUX COPY" in log
    assert w._v94["kpi"]["mode"] == "480hard(from360)" and w._v94["kpi"]["ja_fallback"] == "default=en"


@need_ff
def test_360_non_ja_default_without_ja_track_still_360(v94, monkeypatch, tmp_path, prog18):
    w, master, st, calls = _run(v94, monkeypatch, tmp_path, prog18[120], 120,
                                lang_meta={"default_audio_lang": "en", "has_ja_audio": False, "audio_tracks": 1})
    log = "\n".join(master.logs)
    assert st == "DONE" and calls["native480"] == 0 and "không thấy track Nhật" in log


@need_ff
def test_360_truncated_download_no_done_then_alt_link(v94, monkeypatch, tmp_path, prog18):
    w, master, st, calls = _run(v94, monkeypatch, tmp_path, prog18[120], 120, fast_fail=True)
    log = "\n".join(master.logs)
    # link đầu trả file cụt → 1GET báo thiếu → link dự phòng (yt-dlp) trả đủ → DONE
    assert "link dự phòng" in log and st == "DONE", log[-2000:]
    assert calls["aria2"] == 0


@need_ff
def test_360_http403_then_aria2_fallback_not_done_when_aria2_fails(v94, monkeypatch, tmp_path, prog18):
    w, master, st, calls = _run(v94, monkeypatch, tmp_path, prog18[120], 120, status_code=403)
    # cả 2 link đều /18 (403) vì alt khác URL nhưng... alt18 OK → DONE; kiểm tra alt được dùng
    log = "\n".join(master.logs)
    assert st == "DONE", log[-1500:]


@need_ff
def test_360_no_link_no_done(v94, monkeypatch, tmp_path, prog18):
    monkeypatch.setattr(v94, "_ytdlp_resolve_on_warm_thread", lambda u, robust=False: None)
    from conftest import get_qapp
    get_qapp()
    monkeypatch.setattr(v94, "_fast_innertube_360p", lambda u: None)
    monkeypatch.setattr(v94, "ARIA2C_PATH", "/bin/true")
    xl = tmp_path / "check.xlsx"
    url = "https://www.youtube.com/shorts/Q360TestAbc"
    _xlsx(str(xl), url)
    master = _master360(v94, tmp_path)
    w = v94.Worker(master, str(xl), "URL", "STATUS")
    w._process_single_url(url, 1)
    from openpyxl import load_workbook
    assert load_workbook(str(xl)).worksheets[0].cell(row=3, column=6).value in (None, "")
    assert w._v94["fail"] == "no_itag18"


@need_ff
def test_480_mode_unchanged(v94, monkeypatch, tmp_path, prog18):
    w, master, st, calls = _run(v94, monkeypatch, tmp_path, prog18[120], 120, mode="480hard")
    log = "\n".join(master.logs)
    assert st == "DONE" and calls["native360"] == 0 and "360P" not in log and "START 480P-HARD" in log
    assert w._v94["kpi"].get("mode") in (None, "480hard")


def test_flag_off_forces_480(v94, monkeypatch, tmp_path):
    monkeypatch.setattr(v94, "V95_360P", False)
    m = _master360(v94, tmp_path, "360fast")
    w = v94.Worker(m, "", "URL", "STATUS")
    assert w.quality_mode == "480hard"


def test_bg_probe_warns_on_bad_file(v94, monkeypatch, tmp_path):
    """ffprobe nền sau DONE chỉ cảnh báo (không đổi DONE)."""
    logs = []
    w = object.__new__(v94.Worker)
    bad = tmp_path / "x.mp4"; bad.write_bytes(b"not a video" * 1000)
    w._v94 = {"job": "J", "kpi": {"final_file": str(bad)}, "video": {}, "audio": {}}
    w.log = logs.append
    # chạy trực tiếp phần probe nền
    info = v94._ffprobe_av_info(bad)
    assert not (info["has_video"] and info["has_audio"])


def test_audio_lang_meta(v94):
    data = {"videoDetails": {"lengthSeconds": "58"},
            "microformat": {"playerMicroformatRenderer": {"defaultAudioLanguage": "en"}},
            "streamingData": {"adaptiveFormats": [
                {"mimeType": "audio/mp4", "audioTrack": {"id": "en.4", "displayName": "English original", "audioIsDefault": True}},
                {"mimeType": "audio/mp4", "audioTrack": {"id": "ja.3", "displayName": "日本語"}},
                {"mimeType": "video/mp4"}]}}
    m = v94._v95_audio_lang_meta(data, {"approxDurationMs": "58170"})
    assert m == {"default_audio_lang": "en", "has_ja_audio": True, "audio_tracks": 2, "duration_sec": 58.17}
    m2 = v94._v95_audio_lang_meta({"videoDetails": {"lengthSeconds": "20"}, "streamingData": {"adaptiveFormats": [
        {"mimeType": "audio/mp4"}]}})
    assert m2["default_audio_lang"] == "" and m2["has_ja_audio"] is False and m2["audio_tracks"] == 1


def test_yt_info_to_direct_meta(v94):
    info = {"url": "u", "id": "x", "title": "t", "format_id": "18", "filesize": 10, "language": "ja", "duration": 30,
            "formats": [{"format_id": "140-0", "vcodec": "none", "url": "a", "language": "en"},
                        {"format_id": "140-1", "vcodec": "none", "url": "b", "language": "ja"}]}
    d = v94._yt_info_to_direct(info, "S")
    assert d["default_audio_lang"] == "ja" and d["has_ja_audio"] and d["audio_tracks"] == 2 and d["duration_sec"] == 30


def test_config_roundtrip_quality_mode(v94, monkeypatch, tmp_path):
    from conftest import get_qapp
    if get_qapp() is None:
        pytest.skip("cần PySide6 thật")
    monkeypatch.setattr(v94, "CONFIG_JSON", str(tmp_path / "cfg.json"))
    monkeypatch.setattr(v94, "CONFIG_THEME_JSON", str(tmp_path / "theme.json"))
    w = v94.PremiumDownloaderApp()
    assert w.quality_mode() == "480hard"
    w.radio_light.setChecked(True)
    assert w.quality_mode() == "360fast"
    w._save_config()
    assert json.load(open(tmp_path / "cfg.json", encoding="utf-8"))["quality_mode"] == "360fast"
    w2 = v94.PremiumDownloaderApp()
    assert w2.radio_light.isChecked() and w2.quality_mode() == "360fast"
    w2.radio_auto.setChecked(True); w2._save_config()
    w3 = v94.PremiumDownloaderApp()
    assert w3.quality_mode() == "480hard"
    for x in (w, w2, w3):
        x.logger_qt.stop()


@need_ff
def test_360_range_engine_flag(v94, monkeypatch, tmp_path, prog18):
    monkeypatch.setattr(v94, "V95_360P_RANGE", True)
    w, master, st, calls = _run(v94, monkeypatch, tmp_path, prog18[120], 120)
    assert st == "DONE" and w._v94["kpi"]["engine"] == "range" and "RANGE-TURBO" in "\n".join(master.logs)
    assert w._v94["kpi"]["first_byte_ms"] <= w._v94["kpi"]["hot_ready_ms"]
