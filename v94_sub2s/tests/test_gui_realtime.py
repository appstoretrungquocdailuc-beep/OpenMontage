# -*- coding: utf-8 -*-
"""GUI realtime (Qt offscreen): START → scanner ghi 4 URL vào Excel + trigger → 4 job đồng thời
→ DONE đúng dòng → STOP → không còn thread Range/Media rác. Chạy cho cả V9.3 (bản cũ còn chạy) và V9.4."""
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mock_gvs import MockGVS  # noqa: E402

FFMPEG = shutil.which("ffmpeg")


def _real_qt():
    import PySide6
    return not getattr(PySide6, "__v94_stub__", False)


_KEEP_ALIVE = []   # widget Qt phải sống tới hết phiên: thread nền còn emit signal sau khi test xong


def _shutdown(w, pump):
    w.stop_realtime()
    th = w.realtime_thread
    t_end = time.time() + 10
    while th is not None and th.is_alive() and time.time() < t_end:
        pump(0.1)
    pump(0.5)
    _KEEP_ALIVE.append(w)


pytestmark = pytest.mark.skipif(not (FFMPEG and _real_qt()), reason="cần PySide6 thật + ffmpeg")


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    import subprocess
    d = tmp_path_factory.mktemp("gui_media")
    v, a = d / "v.webm", d / "a.m4a"
    subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-i", "testsrc2=size=480x854:rate=5", "-t", "75",
                    "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-b:v", "100k", "-an", str(v)],
                   check=True, capture_output=True)
    subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-i", "sine=duration=75", "-c:a", "aac", "-b:a", "48k", str(a)],
                   check=True, capture_output=True)
    return v, a


@pytest.mark.parametrize("modname", ["v93", "v94"])
def test_gui_realtime_4_jobs(request, modname, clip, tmp_path, monkeypatch):
    mod = request.getfixturevalue(modname)
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(mod, "CONFIG_JSON", str(tmp_path / "cfg.json"))
    monkeypatch.setattr(mod, "CONFIG_THEME_JSON", str(tmp_path / "theme.json"))
    monkeypatch.setattr(mod, "ARIA2C_PATH", "/bin/true")
    monkeypatch.setattr(mod, "_prewarm_480_visitor_data", lambda force=False: False)
    monkeypatch.setattr(mod, "_start_fast_keepalive", lambda: None)
    monkeypatch.setattr(mod, "_prewarm_fast_youtube_connection", lambda: None)
    monkeypatch.setattr(mod, "_start_aria2_rpc_daemon", lambda: False)
    monkeypatch.setattr(mod, "_ytdlp_resolve_480_on_warm_thread", lambda u, robust=False: None)
    monkeypatch.setattr(mod, "_prewarm_ytdlp_480_resolver_thread", lambda: True)
    v, a = clip
    ids = [f"GuiTest{i:04d}" for i in range(4)]
    with MockGVS() as g:
        vurl = g.add("/videoplayback/v", v.read_bytes())
        aurl = g.add("/videoplayback/a", a.read_bytes())

        def fake_native(url):
            vid = mod._extract_youtube_video_id(url)
            return {"id": vid, "title": f"テスト{vid}", "video_url": vurl + f"?id={vid}", "video_format_id": "244",
                    "video_content_length": v.stat().st_size, "audio_url": aurl + f"?id={vid}",
                    "audio_format_id": "140", "audio_content_length": a.stat().st_size, "audio_mime": "audio/mp4",
                    "duration_sec": 75.0, "source": "NATIVE_VISIONOS", "resolver_ms": 1.0}
        monkeypatch.setattr(mod, "_fast_innertube_480", fake_native)

        from openpyxl import Workbook, load_workbook
        xl = tmp_path / "check.xlsx"
        wb = Workbook(); ws = wb.active
        ws.append(["URL", "CHANNEL ID", "TÊN KÊNH", "TITLE", "LINK VIDEO GỐC", "STATUS"])
        ws.append(["https://www.youtube.com/watch?v=OldDoneVid1", "UC", "k", "old", "g", "DONE"])
        wb.save(xl)
        trig = tmp_path / "trigger_wakeup.txt"
        trig.write_text("")
        mod._EXCEL_DF_CACHE.clear()

        w = mod.PremiumDownloaderApp()
        w.input_excel.setText(str(xl))
        w.input_folder.setText(str(tmp_path / "out"))
        w.input_finalmove.setText(str(tmp_path / "final"))
        w.input_cutsec.setText("0")
        w.spin_threads.setValue(4)
        idx = w.combo_downloader.findData("aria2c")
        if idx >= 0:
            w.combo_downloader.setCurrentIndex(idx)
        th_before = {t.ident for t in threading.enumerate()}
        w.start_realtime()
        assert w.realtime_thread is not None and w.realtime_thread.is_alive()

        def pump(sec):
            end = time.time() + sec
            while time.time() < end:
                app.processEvents()
                time.sleep(0.01)
        pump(0.5)
        # scanner V34: ghi Excel trước, append trigger sau
        wb = load_workbook(xl); ws = wb.active
        for vid in ids:
            ws.append([f"https://www.youtube.com/shorts/{vid}", "UC1", "kênh", f"タイトル{vid}", f"goc_{vid}", ""])
        wb.save(xl)
        with open(trig, "a") as f:
            f.write("1\n")
        t0 = time.time()
        done = False
        while time.time() - t0 < 60:
            pump(0.2)
            try:
                st = [load_workbook(xl).worksheets[0].cell(row=r, column=6).value for r in range(3, 7)]
            except Exception:
                continue
            if all(x == "DONE" for x in st):
                done = True
                break
        log = w.log_widget.toPlainText()
        final_status = None
        for _ in range(20):
            try:
                final_status = [load_workbook(xl).worksheets[0].cell(row=r, column=6).value for r in range(3, 7)]
                break
            except Exception:
                time.sleep(0.1)
        _shutdown(w, pump)
        if modname == "v93" and not done:
            state = "check.xlsx HỎNG (không mở được)" if final_status is None else f"STATUS={final_status}"
            pytest.xfail(f"BUG V9.3 tái hiện với 4 job đồng thời: {state} — write_status_to_excel ghi song song "
                         "cùng file .tmp. Đã sửa ở V9.4 (_V94_EXCEL_WRITE_LOCK).")
        assert done, (final_status, log[-3000:])
        ws = load_workbook(xl).worksheets[0]
        assert ws.cell(row=2, column=6).value == "DONE"
        for i, vid in enumerate(ids):
            assert ws.cell(row=3 + i, column=1).value.endswith(vid)
            folder = tmp_path / "final" / f"goc_{vid}"
            files = [p for p in folder.rglob("*.mp4")]
            assert len(files) == 1, (vid, list(folder.rglob("*")))
        assert w.realtime_thread is None or not w.realtime_thread.is_alive()
        # không còn thread Range/Media/hedge của job (executor shutdown + response đóng)
        time.sleep(1.0)
        leftovers = [t.name for t in threading.enumerate()
                     if t.ident not in th_before and ("Range" in t.name or "MediaNow" in t.name)]
        assert not leftovers, leftovers
        if modname == "v94":
            # V9.4.1: dòng KPI có queue (chờ slot) và detect (phát hiện → worker chạy), detect >= queue
            import re as _re
            pump(0.5)
            kl = [l for l in w.log_widget.toPlainText().splitlines() if "V9.4 KPI" in l]
            assert len(kl) == 4, kl
            for l in kl:
                q = float(_re.search(r"queue=([\d.]+)", l).group(1))
                d = float(_re.search(r"detect=([\d.]+)", l).group(1))
                assert q >= 0 and d + 0.5 >= q, l
                assert "KPI3=" in l and "[V9.7-SUB2S]" in l
        # temp sạch (keep_temp=False)
        tdir = tmp_path / "_temp_yt480p"
        left = [p.name for p in tdir.glob("*")] if tdir.exists() else []
        assert not left, left
