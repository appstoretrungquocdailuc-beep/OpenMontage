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


def _proxy_bench(lab, monkeypatch, tmp_path, ips, asks, **kw):
    tam = lab["tam"]
    tam.TARGET_UPLOAD_URL = f"{lab['base']}/tiktokstudio/upload?nodes=60"
    opened, closed = [], []
    monkeypatch.setattr(tam, "_open_one", lambda pid: opened.append(pid) or lab["drv"])
    monkeypatch.setattr(tam, "_close_one", lambda pid, drv, keep: closed.append(pid))
    seq = iter(ips)
    pb = tam.ProxyBench("PB1", lab["file"], "Viettel", "FPT", 3, log=lambda m: None,
                        ask_switch=lambda label, pid, attempt: asks.append((label, attempt)) or True,
                        out_dir=str(tmp_path / "PROXY_BENCH"), ip_fn=lambda drv: next(seq), **kw)
    return pb, opened, closed


def test_proxy_bench_abba_test_only_never_clicks_and_reports(lab, monkeypatch, tmp_path):
    asks = []
    pb, opened, closed = _proxy_bench(lab, monkeypatch, tmp_path, ["1.1.1.1", "2.2.2.2", "1.1.1.1"], asks)
    tam = lab["tam"]
    seen = []
    real = tam.upload_video_to_tiktok_ex

    def spy(*a, **k):
        seen.append((k.get("dry_run"), k["hooks"].before_click()))    # TEST ONLY: luôn dry-run + hook chặn bấm
        return real(*a, **k)
    monkeypatch.setattr(tam, "upload_video_to_tiktok_ex", spy)
    jp = tam.CFG.journal_path
    j_before = open(jp, encoding="utf-8").read() if os.path.exists(jp) else ""
    out = pb.run()
    assert pb.plan() == ["Viettel"] * 3 + ["FPT"] * 6 + ["Viettel"] * 3          # A-B-B-A theo khối
    assert [a for a, _ in asks] == ["Viettel", "FPT", "Viettel"]                   # chỉ đổi proxy 2 lần (+ lần đặt đầu)
    assert len(opened) == 3 and len(closed) == 3                                   # đóng phiên cũ trước mỗi lần đổi
    rows = out["rows"]
    assert len(rows) == 12 and all(r["result"] == "OK" and not r["clicked"] for r in rows)
    assert all(d is True and bc is False for d, bc in seen)
    assert not lab["page"].evaluate("() => window.__mock.marks.postClick")       # trang không nhận cú bấm nào
    assert sum(1 for r in rows if r["profile_start_ms"] is not None) == 3          # chỉ lượt đầu sau mỗi lần mở profile
    assert all(r["post_ready_ms"] and r["file_attach_ms"] is not None for r in rows)
    s = out["summary"]
    assert s["Viettel"]["runs"] == 6 and s["FPT"]["runs"] == 6 and s["FPT"]["error_rate"] == 0.0
    md = Path(out["files"][0]).read_text(encoding="utf-8")
    assert "| Proxy | Lượt thử | File MB | Post Ready (ms) | Thành công/Lỗi |" in md and "1.1.1.1" not in md   # IP che bớt
    assert ("THẮNG" in out["verdict"]) or ("CHƯA" in out["verdict"])
    j_after = open(jp, encoding="utf-8").read() if os.path.exists(jp) else ""
    assert j_after == j_before                                                     # journal đăng production KHÔNG đổi


def test_proxy_bench_refuses_to_test_when_ip_did_not_change(lab, monkeypatch, tmp_path):
    asks = []
    # lần 1 (A) IP 1.1.1.1; đổi sang B nhưng IP vẫn 1.1.1.1 ×3 → không chạy lượt B nào, dừng
    pb, opened, closed = _proxy_bench(lab, monkeypatch, tmp_path, ["1.1.1.1"] + ["1.1.1.1"] * 3, asks)
    out = pb.run()
    assert [a for a, _ in asks] == ["Viettel", "FPT", "FPT", "FPT"]
    assert {r["proxy"] for r in out["rows"]} == {"Viettel"} and len(out["rows"]) == 3
    assert "CHƯA KẾT LUẬN" in out["verdict"]


def test_proxy_bench_verdict_needs_real_significance():
    """Quy tắc chọn proxy thắng: 2 proxy GIỐNG HỆT → hiếm khi (≤ 6%) báo thắng; chênh thật 1,5 s → gần như luôn phát hiện."""
    import random as R
    tam = __import__("ws360_tam_bridge").load_tam(str(HERE / "fixtures" / "tam_INSTANT_POST.py"))
    assert round(tam.mann_whitney_p([1, 2, 3, 4, 5, 6], [7, 8, 9, 10, 11, 12]), 4) == 0.0022     # giá trị chuẩn U=0, n=6/6
    rnd = R.Random(11)
    pb = tam.ProxyBench("X", __file__, "A", "B", 3)
    fp = 0
    for _ in range(150):
        pb.rows = [{"proxy": p, "run": i, "result": "OK", "post_ready_ms": 3000 + rnd.gauss(0, 400)} for p in "AB" for i in range(6)]
        fp += "THẮNG" in pb.verdict(pb.summary())
    assert fp / 150 <= 0.06, fp
    hit = 0
    for _ in range(60):
        pb.rows = [{"proxy": "A", "run": i, "result": "OK", "post_ready_ms": 2000 + rnd.gauss(0, 300)} for i in range(6)] + \
                  [{"proxy": "B", "run": i, "result": "OK", "post_ready_ms": 3500 + rnd.gauss(0, 300)} for i in range(6)]
        hit += "THẮNG: A" in pb.verdict(pb.summary())
    assert hit / 60 >= 0.95, hit
    pb.rows = [{"proxy": "A", "run": i, "result": "OK", "post_ready_ms": 2000} for i in range(6)] + \
              [{"proxy": "B", "run": i, "result": "OK" if i < 3 else "TIMEOUT", "post_ready_ms": 1500} for i in range(6)]
    assert "CHƯA KẾT LUẬN" in pb.verdict(pb.summary())                          # B chỉ 3 lượt thành công
    pb.rows = [{"proxy": "A", "run": i, "result": "OK" if i < 5 else "TIMEOUT", "post_ready_ms": 1500} for i in range(7)] + \
              [{"proxy": "B", "run": i, "result": "OK", "post_ready_ms": 3000 + i} for i in range(7)]
    assert "CHƯA CHỌN" in pb.verdict(pb.summary())                              # nhanh hơn nhưng lỗi nhiều hơn → không chọn


def test_proxy_bench_window_builds(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    tam = __import__("ws360_tam_bridge").load_tam(str(HERE / "fixtures" / "tam_INSTANT_POST.py"))
    if not tam.HAS_QT:
        pytest.skip("PySide6 không nạp được")
    shown = {}
    monkeypatch.setattr(tam.QApplication, "exec", lambda self: shown.setdefault("ok", 0))
    assert tam.run_proxy_bench_gui() == 0
    w = [x for x in tam.QApplication.topLevelWidgets() if "PROXY UPLOAD BENCHMARK" in x.windowTitle()]
    assert w and "TEST ONLY" in w[0].windowTitle()
    texts = " ".join(b.text() for b in w[0].findChildren(tam.QPushButton))
    assert "Chọn file MP4" in texts and "BẮT ĐẦU TEST" in texts
