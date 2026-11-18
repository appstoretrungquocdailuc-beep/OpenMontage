# -*- coding: utf-8 -*-
"""shorwweb v18: Innertube trực tiếp (race, kết nối nóng), lỗi aria2c đọc từ STDOUT, 403 → client khác → yt-dlp, Python Range dự phòng,
ghép video+audio khi không có itag 18. Máy chủ GIẢ (127.0.0.1): Innertube /youtubei/v1/player + googlevideo (MockGVS)."""
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(ROOT.parent / "tests"))   # mock_gvs (cạnh test hoặc của v94_sub2s)
from mock_gvs import MockGVS, make_payload  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
MB = 1024 * 1024


@pytest.fixture(scope="session")
def v18():
    spec = importlib.util.spec_from_file_location("shorwweb_v18", ROOT / "shorwweb_v18_fast360.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["shorwweb_v18"] = m
    spec.loader.exec_module(m)
    return m


def sha(b):
    return hashlib.sha256(b).hexdigest()


class FakeInnertube:
    """POST /youtubei/v1/player: trả JSON theo client (đọc context.client.clientName). behaviour[client] = (delay_s, data|None, status)."""

    def __init__(self):
        self.behaviour = {}
        self.calls = []
        self.lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                with outer.lock:
                    outer.calls.append(("GET", self.path, None))
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                cname = str(((body.get("context") or {}).get("client") or {}).get("clientName") or "")
                with outer.lock:
                    outer.calls.append(("POST", self.path, cname))
                delay, data, status = outer.behaviour.get(cname, (0.0, None, 200))
                if delay:
                    time.sleep(delay)
                raw = json.dumps(data or {"playabilityStatus": {"status": "ERROR", "reason": "no data"}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def player(vid, title, formats=(), adaptive=(), status="OK"):
    return {"playabilityStatus": {"status": status}, "videoDetails": {"videoId": vid, "title": title, "lengthSeconds": "30"},
            "streamingData": {"formats": list(formats), "adaptiveFormats": list(adaptive)}}


def f18(url, clen):
    return {"itag": 18, "url": url, "mimeType": 'video/mp4; codecs="avc1.42001E, mp4a.40.2"', "contentLength": str(clen), "height": 360}


@pytest.fixture()
def env(v18, monkeypatch):
    it = FakeInnertube()
    monkeypatch.setattr(v18, "INNERTUBE_BASE", f"http://127.0.0.1:{it.port}")
    names = {c: str(v18.innertube_client(c).get("INNERTUBE_CONTEXT", {}).get("client", {}).get("clientName")) for c in v18.V18_CLIENTS}
    yield it, names
    it.close()


# ----------------------------------------------------------------------------- resolver
def test_clients_and_po_policy(v18):
    assert v18.V18_CLIENTS[0] == "visionos"
    assert v18.client_needs_po_token("android_vr") is True          # đúng chính sách yt-dlp → link android_vr dễ bị 403
    assert v18.client_needs_po_token("visionos") is False
    assert v18.client_needs_po_token("web_embedded") is False


def test_extract_video_id(v18):
    for u in ("https://www.youtube.com/watch?v=9tbnXFHVqNs", "https://youtu.be/9tbnXFHVqNs?t=3", "https://www.youtube.com/shorts/9tbnXFHVqNs",
              "9tbnXFHVqNs", "https://m.youtube.com/watch?feature=x&v=9tbnXFHVqNs"):
        assert v18.extract_video_id(u) == "9tbnXFHVqNs"


def test_race_first_clean_client_wins_without_waiting(v18, env):
    it, n = env
    it.behaviour[n["visionos"]] = (0.05, player("AbCdEfGhIjK", "テスト", [f18("https://x.invalid/v18", 10)]), 200)
    it.behaviour[n["web_embedded"]] = (1.2, player("AbCdEfGhIjK", "テスト"), 200)
    it.behaviour[n["android_vr"]] = (1.2, player("AbCdEfGhIjK", "テスト"), 200)
    v18.FAST360.warm()
    t = time.perf_counter()
    r = v18.FAST360.resolve("https://www.youtube.com/watch?v=AbCdEfGhIjK")
    el = time.perf_counter() - t
    assert r["ok"] and r["client"] == "visionos" and r["mode"] == "prog" and r["itag"] == "18" and not r["po_risky"]
    assert el < 0.45, el                                             # không chờ 2 client chậm (1.2 s)
    assert r["ua"] and r["title"] == "テスト"


def test_po_client_waits_short_grace_for_clean_client(v18, env):
    it, n = env
    it.behaviour[n["android_vr"]] = (0.0, player("AbCdEfGhIjK", "t", [f18("https://x.invalid/vr", 10)]), 200)
    it.behaviour[n["visionos"]] = (0.08, player("AbCdEfGhIjK", "t", [f18("https://x.invalid/vos", 10)]), 200)
    it.behaviour[n["web_embedded"]] = (1.2, player("AbCdEfGhIjK", "t"), 200)
    r = v18.FAST360.resolve("AbCdEfGhIjK")
    assert r["ok"] and r["client"] == "visionos" and r["url"].endswith("/vos")
    # client sạch không có 360p → dùng android_vr nhưng đánh dấu rủi ro PO token
    it.behaviour[n["visionos"]] = (0.0, player("AbCdEfGhIjK", "t"), 200)
    it.behaviour[n["web_embedded"]] = (0.0, player("AbCdEfGhIjK", "t"), 200)
    r = v18.FAST360.resolve("AbCdEfGhIjK")
    assert r["ok"] and r["client"] == "android_vr" and r["po_risky"] is True


def test_cipher_only_formats_not_used_and_pair_fallback(v18, env):
    it, n = env
    ciph = {"itag": 18, "signatureCipher": "s=x&url=y", "mimeType": "video/mp4", "contentLength": "10", "height": 360}
    adap = [{"itag": 134, "url": "https://x.invalid/134", "mimeType": 'video/mp4; codecs="avc1.4d401e"', "height": 360, "contentLength": "100"},
            {"itag": 243, "url": "https://x.invalid/243", "mimeType": 'video/webm; codecs="vp9"', "height": 360, "contentLength": "90"},
            {"itag": 140, "url": "https://x.invalid/140-en", "mimeType": 'audio/mp4; codecs="mp4a.40.2"', "contentLength": "50",
             "audioTrack": {"id": "en.4", "audioIsDefault": False}},
            {"itag": 140, "url": "https://x.invalid/140-ja", "mimeType": 'audio/mp4; codecs="mp4a.40.2"', "contentLength": "50",
             "audioTrack": {"id": "ja.4", "audioIsDefault": True}}]
    it.behaviour[n["visionos"]] = (0.0, player("AbCdEfGhIjK", "t", [ciph], adap), 200)
    it.behaviour[n["web_embedded"]] = (0.5, player("AbCdEfGhIjK", "t", [ciph]), 200)
    it.behaviour[n["android_vr"]] = (0.5, player("AbCdEfGhIjK", "t"), 200)
    r = v18.FAST360.resolve("AbCdEfGhIjK")
    assert r["ok"] and r["mode"] == "pair" and r["video"]["itag"] == "134" and r["audio"]["url"].endswith("140-ja")


def test_all_clients_fail_reports_reasons(v18, env):
    it, n = env
    it.behaviour[n["visionos"]] = (0.0, player("AbCdEfGhIjK", "t", status="LOGIN_REQUIRED"), 200)
    it.behaviour[n["web_embedded"]] = (0.0, None, 500)
    it.behaviour[n["android_vr"]] = (0.0, player("AbCdEfGhIjK", "t"), 200)
    r = v18.FAST360.resolve("AbCdEfGhIjK", deadline=1.0)
    assert not r["ok"] and "web_embedded: player HTTP 500" in r["reason"] and "LOGIN_REQUIRED" in r["reason"]


# ----------------------------------------------------------------------------- aria2c / python download
need_aria2 = pytest.mark.skipif(not shutil.which("aria2c"), reason="không có aria2c")


@need_aria2
def test_aria2_error_is_read_from_stdout(v18, tmp_path, monkeypatch):
    """v17 đọc stderr (trống) → 'Lỗi Tải: '. v18 phải báo đúng HTTP 403."""
    monkeypatch.setattr(v18, "ARIA2C_PATH", shutil.which("aria2c"))
    logs, info = [], {}
    with MockGVS() as g:
        ok = v18.download_aria2c_ultimate(g.add("/deny", mode="status", code=403), tmp_path, "x.mp4", logs.append, 1, info=info)
    assert not ok and info["http"] == 403 and info["fail"] == "HTTP_403"
    line = [l for l in logs if "Lỗi Tải" in l][0]
    assert "HTTP 403" in line and "errorCode=22" in line and "127.0.0.1" not in line     # không in URL
    assert not (tmp_path / "x.mp4").exists()


@need_aria2
def test_aria2_ok_exact_size(v18, tmp_path, monkeypatch):
    monkeypatch.setattr(v18, "ARIA2C_PATH", shutil.which("aria2c"))
    data = make_payload(6 * MB, seed=18)
    info = {}
    with MockGVS() as g:
        ok = v18.download_aria2c_ultimate(g.add("/ok", data), tmp_path, "o.mp4", lambda s: None, 1, expected=len(data), info=info)
    assert ok and sha((tmp_path / "o.mp4").read_bytes()) == sha(data)


def test_python_range_fallback_exact(v18, tmp_path, monkeypatch):
    monkeypatch.setattr(v18, "ARIA2C_PATH", None)
    data = make_payload(9 * MB + 123, seed=19)
    info = {}
    with MockGVS() as g:
        real = g.add("/real", data)
        url = g.add("/r", mode="redirect", to=real)
        ok = v18.fetch_media(url, tmp_path, "p.mp4", lambda s: None, 1, {}, expected=0, info=info)
    assert ok and info["engine"] == "python" and info["conns"] > 1
    assert sha((tmp_path / "p.mp4").read_bytes()) == sha(data)


def test_python_range_rejects_bad_content_range(v18, tmp_path, monkeypatch):
    monkeypatch.setattr(v18, "ARIA2C_PATH", None)
    data = make_payload(4 * MB, seed=20)
    info = {}
    with MockGVS() as g:
        ok = v18.fetch_media(g.add("/bad", data, mode="badcr", shift=7), tmp_path, "b.mp4", lambda s: None, 1, {}, expected=len(data), info=info)
    assert not ok and not (tmp_path / "b.mp4").exists()


def test_403_not_retried_with_python(v18, tmp_path, monkeypatch):
    monkeypatch.setattr(v18, "ARIA2C_PATH", None)
    info = {}
    with MockGVS() as g:
        ok = v18.fetch_media(g.add("/d", mode="status", code=403), tmp_path, "d.mp4", lambda s: None, 1, {}, info=info)
        n = len(g.requests_for("/d"))
    assert not ok and info["http"] == 403 and n == 1


# ----------------------------------------------------------------------------- NGUYÊN luồng Worker (GUI giữ nguyên)
@pytest.fixture(scope="session")
def mp4_30s(tmp_path_factory):
    ff = shutil.which("ffmpeg")
    if not ff:
        pytest.skip("không có ffmpeg")
    d = tmp_path_factory.mktemp("media")
    out = {}
    v = d / "v.mp4"
    subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25", "-f", "lavfi", "-i", "sine=duration=30",
                    "-t", "30", "-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(v)],
                   check=True)
    out["prog"] = v
    vo, ao = d / "v134.mp4", d / "a140.m4a"
    subprocess.run([ff, "-y", "-v", "error", "-i", str(v), "-map", "0:v:0", "-c", "copy", str(vo)], check=True)
    subprocess.run([ff, "-y", "-v", "error", "-i", str(v), "-map", "0:a:0", "-c", "copy", str(ao)], check=True)
    out["video"], out["audio"] = vo, ao
    return out


def _master(v18, root, cutsec="10"):
    class T:
        def __init__(self, v): self.v = v
        def text(self): return self.v

    class C:
        def isChecked(self): return False
    m = types.SimpleNamespace()
    m.worker_signals = v18.WorkerSignals()
    m.logs, m.done = [], []
    m.worker_signals.log_message.connect(lambda s: m.logs.append(s))
    m.worker_signals.url_finished.connect(lambda u, i, ok: m.done.append(ok))
    m.out_entry, m.keep_temp_checkbox, m.final_move_entry = T(str(root / "out")), C(), T(str(root / "final"))
    m.prefix_entry, m.cutsec_entry, m.cutout_entry, m.cutout_touched = T("ok"), T(cutsec), T(""), False
    m.realtime_stop_event = threading.Event()
    return m


def _qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _run(v18, tmp_path, url="https://www.youtube.com/watch?v=AbCdEfGhIjK"):
    app = _qapp()
    m = _master(v18, tmp_path)
    w = v18.Worker(m, "", None, None)
    w._process_single_url(url, 0)
    for _ in range(20):                    # log phát từ luồng tải song song đi qua hàng đợi Qt (như GUI thật)
        app.processEvents()
        time.sleep(0.01)
    return m


@need_aria2
def test_worker_end_to_end_native_aria2_and_cut(v18, env, tmp_path, monkeypatch, mp4_30s):
    it, n = env
    monkeypatch.setattr(v18, "ARIA2C_PATH", shutil.which("aria2c"))
    data = mp4_30s["prog"].read_bytes()
    with MockGVS() as g:
        url = g.add("/videoplayback/18", data)
        it.behaviour[n["visionos"]] = (0.0, player("AbCdEfGhIjK", "テスト動画", [f18(url, len(data))]), 200)
        m = _run(v18, tmp_path)
    log = "\n".join(m.logs)
    assert m.done == [True], log[-2000:]
    assert "LẤY LINK [INNERTUBE_VISIONOS · itag 18]" in log and "[ARIA2C] ✅" in log
    parts = sorted((tmp_path / "final").rglob("*.mp4"))
    assert len(parts) == 3, log[-1500:]                              # 30 s / 10 s


@need_aria2
def test_worker_403_switches_to_other_client_link(v18, env, tmp_path, monkeypatch, mp4_30s):
    it, n = env
    monkeypatch.setattr(v18, "ARIA2C_PATH", shutil.which("aria2c"))
    data = mp4_30s["prog"].read_bytes()
    with MockGVS() as g:
        bad = g.add("/videoplayback/bad", mode="status", code=403)
        good = g.add("/videoplayback/good", data)
        it.behaviour[n["visionos"]] = (0.0, player("AbCdEfGhIjK", "t", [f18(bad, len(data))]), 200)
        it.behaviour[n["web_embedded"]] = (0.1, player("AbCdEfGhIjK", "t", [f18(good, len(data))]), 200)
        m = _run(v18, tmp_path)
        n_bad = len(g.requests_for("/videoplayback/bad"))
    log = "\n".join(m.logs)
    assert m.done == [True], log[-2000:]
    assert "HTTP 403" in log and "→ thử INNERTUBE_WEB_EMBEDDED" in log
    assert n_bad == 1                                               # link bị từ chối: đúng 1 request, không thử lại


def test_worker_pair_mux_without_aria2(v18, env, tmp_path, monkeypatch, mp4_30s):
    """Không có itag 18 + không có aria2c → tải video 360p + audio bằng Python, ghép stream-copy, cắt."""
    it, n = env
    monkeypatch.setattr(v18, "ARIA2C_PATH", None)
    vb, ab = mp4_30s["video"].read_bytes(), mp4_30s["audio"].read_bytes()
    with MockGVS() as g:
        vu, au = g.add("/videoplayback/134", vb), g.add("/videoplayback/140", ab)
        adap = [{"itag": 134, "url": vu, "mimeType": 'video/mp4; codecs="avc1.42001e"', "height": 360, "contentLength": str(len(vb))},
                {"itag": 140, "url": au, "mimeType": 'audio/mp4; codecs="mp4a.40.2"', "contentLength": str(len(ab))}]
        it.behaviour[n["visionos"]] = (0.0, player("AbCdEfGhIjK", "t", [], adap), 200)
        m = _run(v18, tmp_path)
    log = "\n".join(m.logs)
    assert m.done == [True], log[-2000:]
    assert "itag 134+140" in log and "[MUX] ✅" in log and "[PY-RANGE] ✅" in log
    parts = sorted((tmp_path / "final").rglob("*.mp4"))
    assert len(parts) == 3
    pr = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(parts[0])],
                        capture_output=True, text=True)
    assert {"video", "audio"} <= set(pr.stdout.split())


def test_worker_innertube_fails_uses_ytdlp_fallback(v18, env, tmp_path, monkeypatch, mp4_30s):
    it, n = env
    monkeypatch.setattr(v18, "ARIA2C_PATH", None)
    data = mp4_30s["prog"].read_bytes()
    with MockGVS() as g:
        url = g.add("/videoplayback/yt", data)
        for c in n.values():
            it.behaviour[c] = (0.0, player("AbCdEfGhIjK", "t", status="UNPLAYABLE"), 200)
        monkeypatch.setattr(v18.YTDLP_FALLBACK, "resolve", lambda u, logger=None: {
            "ok": True, "ms": 900.0, "client": "yt-dlp", "source": "YTDLP", "po_risky": False, "title": "t", "id": "AbCdEfGhIjK",
            "race": [], "alt_futs": [], "mode": "prog", "itag": "18", "url": url, "clen": len(data), "ua": v18.V18_UA_CHROME})
        m = _run(v18, tmp_path)
    log = "\n".join(m.logs)
    assert m.done == [True], log[-2000:]
    assert "Innertube không cho link 360p" in log and "UNPLAYABLE" in log and "LẤY LINK [YTDLP" in log


def test_bench_link_cli_offline(v18, env, capsys, monkeypatch):
    it, n = env
    with MockGVS() as g:
        ok_url, bad_url = g.add("/videoplayback/ok", make_payload(4096, seed=1)), g.add("/videoplayback/bad", mode="status", code=403)
        it.behaviour[n["visionos"]] = (0.0, player("AbCdEfGhIjK", "t", [f18(ok_url, 4096)]), 200)
        v18.bench_link(["https://www.youtube.com/watch?v=AbCdEfGhIjK"], reps=2, gap=0.0, with_v17=False)
        it.behaviour[n["visionos"]] = (0.0, player("AbCdEfGhIjK", "t", [f18(bad_url, 4096)]), 200)
        v18.bench_link(["https://www.youtube.com/watch?v=AbCdEfGhIjK"], reps=1, gap=0.0, with_v17=False)
    out = capsys.readouterr().out
    assert "thắng: visionos itag 18 | thử 1KB: HTTP 206 OK" in out and "HTTP 403 BỊ TỪ CHỐI" in out
    assert "127.0.0.1" not in out                                    # không in link


def test_gui_builds_and_warms(v18, env, monkeypatch, tmp_path):
    """Giao diện v17 giữ nguyên; mở tool → mở sẵn kết nối (GET /generate_204) + log 🔥."""
    it, n = env
    monkeypatch.setattr(v18, "CONFIG_JSON", str(tmp_path / "cfg.json"))
    monkeypatch.setattr(v18, "CONFIG_THEME_JSON", str(tmp_path / "theme.json"))
    app = _qapp()
    w = v18.PremiumDownloaderApp()
    for _ in range(100):
        app.processEvents()
        time.sleep(0.02)
        if "🔥" in w.log_widget.toPlainText():
            break
    assert "v18" in w.windowTitle() and w.input_cutsec.text() == "71" and w.btn_start.text() == "START MONITOR"
    assert "kết nối YouTube đã mở sẵn" in w.log_widget.toPlainText() and "android_vr (cần PO token)" in w.log_widget.toPlainText()
    assert sum(1 for c in it.calls if c[0] == "GET" and c[1] == "/generate_204") >= v18.V18_WARM_CONNS
    v18.FAST360.stop_keepalive()
    w.logger_qt.stop()
