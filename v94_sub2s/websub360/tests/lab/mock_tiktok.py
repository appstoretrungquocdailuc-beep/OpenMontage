# -*- coding: utf-8 -*-
"""Trang Upload GIẢ mô phỏng TikTok Studio để đo tác động của watcher lên tốc độ upload (chạy ở PROCESS RIÊNG).

Mô phỏng các đặc điểm có thể bị watcher làm chậm:
  • upload chia khúc 512 KB bằng fetch trên MAIN THREAD (mỗi khúc chờ khúc trước, giữa các khúc JS của trang cập nhật UI);
  • SPA cập nhật giao diện liên tục trong lúc upload (re-render panel N node mỗi 16 ms + text % + thanh tiến trình);
  • (tuỳ chọn) React thay node nút Post mỗi lần cập nhật; (tuỳ chọn) lớp phủ che nút Post trong lúc upload/xử lý;
  • máy chủ giới hạn băng thông (sleep theo dung lượng) + thời gian "TikTok xử lý" cố định sau khúc cuối;
  • trang tự hỏi /status mỗi 300 ms (setTimeout) rồi BẬT nút Post.
Mốc thời gian: trang ghi epoch ms (performance.timeOrigin + now); máy chủ ghi epoch ms (time.time) → so được với nhau.
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

STATE = {}
LOCK = threading.Lock()

PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Upload</title>
<style>
body{font-family:sans-serif;margin:0} #panel div{padding:2px;border-bottom:1px solid #eee}
.bar{height:6px;background:#ddd}.bar i{display:block;height:6px;background:#fe2c55;width:0}
.footer{position:fixed;bottom:0;left:0;right:0;height:80px;background:#fff;border-top:1px solid #ccc}
.btn{position:absolute;right:40px;top:20px;width:120px;height:40px}
#cover{position:fixed;bottom:0;right:0;width:300px;height:90px;background:rgba(0,0,0,.6);color:#fff;display:none;z-index:9}
.spin{width:20px;height:20px;border:3px solid #fe2c55;border-radius:50%}
</style></head><body>
<div id="root"><h3>Upload video</h3>
<label>Chọn video <input type="file" id="f" accept="video/*"></label>
<div class="bar"><i id="barfill"></i></div><div id="pct">0%</div><div class="spin" id="spin"></div>
<div id="panel"></div>
<div class="footer" id="footer"><div class="wrap" id="wrap"><button data-e2e="post_video_button" class="btn btn-disabled" disabled>Post</button></div></div>
<div id="cover">Đang tải lên…</div></div>
<script>
const Q = new URLSearchParams(location.search);
const CFG = {nodes: +(Q.get('nodes') || 60), rerenderBtn: Q.get('rbtn') === '1', cover: Q.get('cover') === '1', chunk: 512 * 1024,
             coverHold: +(Q.get('coverhold') || 0)};
const M = window.__mock = {marks: {}, long: [], chunks: 0, renders: 0, id: null};
const E = () => performance.timeOrigin + performance.now();
try { new PerformanceObserver(l => l.getEntries().forEach(e => M.long.push([performance.timeOrigin + e.startTime, e.duration]))).observe({type: 'longtask'}); } catch (e) {}
let progress = 0, phase = 'idle', renderTimer = null, ang = 0;
function renderBtn(enabled) {
  const w = document.getElementById('wrap');
  if (CFG.rerenderBtn || enabled) {
    w.innerHTML = '<button data-e2e="post_video_button" class="btn ' + (enabled ? 'btn-primary' : 'btn-disabled') + '"' + (enabled ? '' : ' disabled') + '>Post</button>';
    const b = w.querySelector('button'); b.addEventListener('click', onPost);
  }
}
function render() {
  M.renders++;
  document.getElementById('pct').textContent = (phase === 'processing' ? 'Đang xử lý ' : 'Đang tải lên ') + Math.round(progress * 100) + '%';
  document.getElementById('barfill').style.width = (progress * 100) + '%';
  let h = '';
  for (let i = 0; i < CFG.nodes; i++) h += '<div class="row r' + (i % 7) + '"><span>' + phase + ' ' + i + '</span><b>' + ((M.renders * 31 + i) % 997) + '</b></div>';
  document.getElementById('panel').innerHTML = h;
  ang = (ang + 12) % 360; document.getElementById('spin').style.transform = 'rotate(' + ang + 'deg)';
  if (CFG.rerenderBtn && phase !== 'ready') renderBtn(false);
}
function onPost() {
  M.marks.postClick = E();
  fetch('/post?id=' + M.id, {method: 'POST'}).then(() => { history.pushState({}, '', '/tiktokstudio/content'); const d = document.createElement('div'); d.textContent = 'Video published'; document.body.appendChild(d); });
}
document.querySelector('button[data-e2e=post_video_button]').addEventListener('click', onPost);
async function upload(file) {
  M.id = Math.random().toString(36).slice(2);
  M.marks.attach = E(); M.size = file.size;
  phase = 'uploading';
  if (CFG.cover) document.getElementById('cover').style.display = 'block';
  renderTimer = setInterval(render, 16);
  const n = Math.ceil(file.size / CFG.chunk);
  for (let i = 0; i < n; i++) {
    const b = file.slice(i * CFG.chunk, Math.min(file.size, (i + 1) * CFG.chunk));
    const r = await fetch('/chunk?id=' + M.id + '&i=' + i + '&n=' + n, {method: 'POST', body: b});
    await r.text();
    M.chunks++; progress = (i + 1) / n;
  }
  M.marks.uploaded = E(); phase = 'processing';
  while (true) {
    await new Promise(r => setTimeout(r, 300));
    const s = await (await fetch('/status?id=' + M.id)).json();
    if (s.ready) break;
  }
  clearInterval(renderTimer); phase = 'ready'; render();
  if (CFG.coverHold > 0) {                        // nút BẬT nhưng vẫn bị che thêm coverHold ms → không được bấm lúc này
    document.getElementById('cover').style.display = 'block';
    renderBtn(true); M.marks.enabled = E();
    setTimeout(() => { document.getElementById('cover').style.display = 'none'; M.marks.coverOff = E(); }, CFG.coverHold);
    return;
  }
  if (CFG.cover) document.getElementById('cover').style.display = 'none';
  renderBtn(true);
  M.marks.enabled = E();
}
document.getElementById('f').addEventListener('change', e => { if (e.target.files.length) upload(e.target.files[0]); });
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        return

    def _send(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path.endswith("/upload"):
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        if u.path == "/status":
            with LOCK:
                st = STATE.get(q.get("id"), {})
            ready = bool(st.get("last_done")) and time.time() * 1000 >= st["last_done"] + self.server.proc_ms
            if ready and not st.get("ready_at"):
                st["ready_at"] = time.time() * 1000
            return self._send(200, json.dumps({"ready": ready}).encode())
        if u.path == "/log":
            with LOCK:
                return self._send(200, json.dumps(STATE.get(q.get("id"), {})).encode())
        if u.path.startswith("/tiktokstudio/content"):
            return self._send(200, b"<html><body>Video published</body></html>", "text/html")
        self._send(404)

    def do_POST(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        n = int(self.headers.get("Content-Length") or 0)
        t0 = time.time() * 1000
        data = self.rfile.read(n) if n else b""
        if u.path == "/chunk":
            time.sleep(len(data) * 8 / self.server.bw_bps)          # băng thông giới hạn (mô phỏng mạng tới TikTok)
            t1 = time.time() * 1000
            with LOCK:
                st = STATE.setdefault(q.get("id"), {"first_start": t0, "chunks": 0, "bytes": 0, "gap_ms": 0.0, "max_gap_ms": 0.0})
                if st.get("prev_end") is not None:              # máy chủ RẢNH giữa 2 khúc = độ trễ phía trình duyệt
                    g = t0 - st["prev_end"]
                    st["gap_ms"] += g
                    st["max_gap_ms"] = max(st["max_gap_ms"], g)
                st["prev_end"] = t1
                st["chunks"] += 1
                st["bytes"] += len(data)
                if int(q.get("i", 0)) + 1 >= int(q.get("n", 1)):
                    st["last_done"] = t1
            return self._send(200, b"{}")
        if u.path == "/post":
            with LOCK:
                STATE.setdefault(q.get("id"), {})["posted"] = time.time() * 1000
            return self._send(200, b"{}")
        self._send(404)


def main():
    port, bw_mbps, proc_ms = int(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.bw_bps = bw_mbps * 1e6
    srv.proc_ms = proc_ms
    print("READY", srv.server_address[1], flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
