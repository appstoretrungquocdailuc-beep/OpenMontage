# -*- coding: utf-8 -*-
"""MODE A/B/C của TAM trên Chromium THẬT + trang Upload GIẢ (tests/lab/mock_tiktok.py). Chạy mã TAM thật
(upload_video_to_tiktok_ex). Bỏ qua nếu máy không có Playwright/Chromium. Không chạm TikTok thật."""
import os
import random
import socket
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
LAB = HERE / "lab"
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(LAB))
pw_api = pytest.importorskip("playwright.sync_api")
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
MODES = {"A": "event", "B": "sparse", "C": "lite"}


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    work = tmp_path_factory.mktemp("lab")
    os.environ.update({"TAM_HIGH_PRIORITY": "0", "TAM_PERF_LOG": str(work / "perf.jsonl"), "TAM_JOURNAL": str(work / "j.jsonl"),
                       "TAM_UPLOAD_URL_PATH": "/tiktokstudio/upload"})
    from ws360_tam_bridge import load_tam
    tam = load_tam(str(HERE / "fixtures" / "tam_INSTANT_POST.py"))
    f = work / "v.mp4"
    f.write_bytes(random.Random(1).randbytes(1_500_000))
    port = _free_port()
    srv = subprocess.Popen([sys.executable, str(LAB / "mock_tiktok.py"), str(port), "40", "600"], stdout=subprocess.PIPE, text=True)
    assert srv.stdout.readline().startswith("READY")
    from pw_adapter import PWDriver
    p = pw_api.sync_playwright().start()
    try:
        ctx = p.chromium.launch_persistent_context(str(work / "prof"), headless=True, channel="chromium")
    except Exception as e:  # noqa: BLE001
        srv.terminate()
        p.stop()
        pytest.skip(f"không chạy được Chromium: {e}")
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    drv = PWDriver(page, ctx.new_cdp_session(page))
    yield {"tam": tam, "drv": drv, "page": page, "base": f"http://127.0.0.1:{port}", "file": str(f)}
    ctx.close()
    p.stop()
    srv.terminate()


def run(lab, mode, query, dry=True, pid="T"):
    tam, page = lab["tam"], lab["page"]
    tam.CFG.wait_mode = MODES[mode]
    tam.TARGET_UPLOAD_URL = f"{lab['base']}/tiktokstudio/upload?{query}"
    fsm = tam.get_fsm(pid)
    fsm.reset_unknown("test")
    fsm.dirty = True
    seen = {}

    def log(msg):
        if "[Timing] T1" in msg:                     # đang upload: watcher nào đang nằm trong trang?
            seen.update(page.evaluate("() => ({full: !!window.__tam5, lite: !!window.__tamLite})"))
    res = tam.upload_video_to_tiktok_ex(lab["drv"], [lab["file"]], logger_func=log, pid=pid, mode="TEST", dry_run=dry)
    return res, seen, page.evaluate("() => window.__mock.marks")


@pytest.mark.parametrize("mode", ["A", "B", "C"])
def test_mode_installs_only_what_it_should_during_upload(lab, mode):
    res, seen, marks = run(lab, mode, "nodes=60")
    assert res.status.value == "DRY_RUN" and not res.clicked
    assert seen == {"A": {"full": True, "lite": False}, "B": {"full": False, "lite": False},
                    "C": {"full": False, "lite": True}}[mode]
    assert marks.get("enabled") and not marks.get("postClick")              # dry-run: KHÔNG bấm


@pytest.mark.parametrize("mode", ["B", "C"])
def test_light_modes_detect_when_react_replaces_button_node(lab, mode):
    res, _, marks = run(lab, mode, "nodes=300&rbtn=1")
    assert res.status.value == "DRY_RUN" and marks.get("enabled")


@pytest.mark.parametrize("mode", ["A", "B", "C"])
def test_never_clicks_while_post_button_is_covered_and_still_confirms(lab, mode):
    """Nút đã BẬT nhưng còn bị lớp phủ che 1,2 s: không được bấm lúc đó; bấm sau khi hết che; POST_CONFIRMED vẫn có."""
    res, _, marks = run(lab, mode, "nodes=60&coverhold=1200", dry=False, pid="TC" + mode)
    assert res.status.value == "CONFIRMED", res.reason
    assert marks["postClick"] >= marks["coverOff"] > marks["enabled"]


def test_bench_watch_cli_modes_x_page_states_reports_p90_p95(lab, monkeypatch, capsys):
    tam = lab["tam"]
    tam.TARGET_UPLOAD_URL = f"{lab['base']}/tiktokstudio/upload?nodes=60"
    monkeypatch.setattr(tam, "_open_one", lambda pid: lab["drv"])
    monkeypatch.setattr(tam, "_close_one", lambda pid, drv, keep: None)
    before = set(Path(tam.BASE_DIR).glob("tam_bench_watch_*.json"))
    try:
        assert tam.cli_bench_watch("BW", [lab["file"]], 2, True, True, "AC", "fresh,idle", 0.3) == 0
        new = set(Path(tam.BASE_DIR).glob("tam_bench_watch_*.json")) - before
        import json
        rep = json.loads(next(iter(new)).read_text(encoding="utf-8"))
        assert set(rep["summary"]) == {"A/fresh", "C/fresh", "A/idle", "C/idle"}
        assert len(rep["rows"]) == 8 and all(r["result"] == "DRY_RUN" for r in rep["rows"])
        s = rep["summary"]["A/fresh"]["attach_to_ready_py_ms"]
        assert {"median", "p90", "p95"} <= set(s)
        idle_rows = [r for r in rep["rows"] if r["page_state"] == "idle"]
        assert all(r["page_reloaded"] == 0 for r in idle_rows)            # trang 'idle' = trang mở sẵn, không nạp lại khi gắn
        out = capsys.readouterr().out
        assert "median | P90 | P95" in out
    finally:
        for f in set(Path(tam.BASE_DIR).glob("tam_bench_watch_*.json")) - before:
            f.unlink()
