# -*- coding: utf-8 -*-
"""Mock GoogleVideo (GVS) server cho test/bench offline.

Mỗi "file" được đăng ký với hành vi riêng:
  ok            206 đúng chuẩn (Range bắt buộc), keep-alive HTTP/1.1
  redirect      302 → file đích (1 hoặc nhiều hop)
  status        trả mã cố định (403/429/410...) + header tuỳ chọn
  norange       bỏ qua Range, trả 200 cả file
  badcr         206 nhưng Content-Range lệch start (dữ liệu sai vị trí)
  drop_once     request đầu tiên chạm offset X sẽ bị cắt kết nối giữa chừng
  hdr_delay     trễ header (TTFB) cố định
  rate          giới hạn byte/s mỗi kết nối (mô phỏng giới hạn per-connection của GVS)
  slow_first_n  N request đầu bị bóp băng thông (slow tail)
"""
import hashlib
import os
import random
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def make_payload(size: int, seed: int = 1234) -> bytes:
    rnd = random.Random(seed)
    return rnd.randbytes(size) if hasattr(rnd, "randbytes") else os.urandom(size)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Spec:
    def __init__(self, data=b"", mode="ok", **kw):
        self.data = data
        self.mode = mode
        self.kw = kw
        self.lock = threading.Lock()
        self.requests = 0
        self.dropped = False


class _LinkBucket:
    """Giới hạn băng thông tổng (mô phỏng đường truyền thật) dùng chung cho mọi kết nối."""

    def __init__(self, bps):
        self.bps = float(bps)
        self.lock = threading.Lock()
        self.next_free = time.perf_counter()

    def take(self, nbytes):
        with self.lock:
            now = time.perf_counter()
            start = max(now, self.next_free)
            self.next_free = start + nbytes / self.bps
            wait = self.next_free - now
        if wait > 0:
            time.sleep(wait)


class MockGVS:
    def __init__(self, link_bytes_per_sec=0, ttfb=0.0):
        self.link = _LinkBucket(link_bytes_per_sec) if link_bytes_per_sec else None
        self.ttfb = float(ttfb or 0.0)
        self.files = {}
        self.log = []
        self.log_lock = threading.Lock()
        self.conns = 0
        mock = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def setup(self):
                super().setup()
                with mock.log_lock:
                    mock.conns += 1

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                spec = mock.files.get(path)
                rng = self.headers.get("Range")
                with mock.log_lock:
                    mock.log.append((time.perf_counter(), path, rng))
                if spec is None:
                    return self._send(404, b"nf")
                with spec.lock:
                    spec.requests += 1
                    nreq = spec.requests
                kw = spec.kw
                if mock.ttfb:
                    time.sleep(mock.ttfb)
                if kw.get("hdr_delay"):
                    time.sleep(float(kw["hdr_delay"]))
                if spec.mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", kw["to"])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if spec.mode == "status":
                    hdrs = dict(kw.get("headers") or {})
                    return self._send(int(kw["code"]), b"denied", hdrs)
                data = spec.data
                n = len(data)
                if spec.mode == "norange" or not rng:
                    # 200 cả file: cùng giới hạn per-connection/link như 206 (so sánh công bằng)
                    self.send_response(200)
                    self.send_header("Content-Length", str(n))
                    self.end_headers()
                    return self._stream(data, float(kw.get("rate") or 0))
                try:
                    unit, spec_r = rng.split("=", 1)
                    a, b = spec_r.split("-", 1)
                    start = int(a)
                    end = int(b) if b else n - 1
                except Exception:
                    return self._send(400, b"bad range")
                if start >= n:
                    return self._send(416, b"", {"Content-Range": f"bytes */{n}"})
                end = min(end, n - 1)
                cr_start = start
                if spec.mode == "badcr":
                    # Server/edge trả SAI đoạn byte (lệch shift) nhưng Content-Range trung thực.
                    sh = int(kw.get("shift", 1))
                    if end + sh <= n - 1:          # chỉ lệch khi còn đủ byte (đoạn giữa file)
                        cr_start, end = start + sh, end + sh
                body = data[cr_start:end + 1]
                self.send_response(206)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Range", f"bytes {cr_start}-{cr_start + len(body) - 1}/{n}")
                self.end_headers()
                rate = float(kw.get("rate") or 0)
                if kw.get("slow_first_n") and nreq <= int(kw["slow_first_n"]):
                    rate = float(kw.get("slow_rate") or 64 * 1024)
                drop_at = kw.get("drop_at")
                pos = 0
                chunk = 16 * 1024
                t0 = time.perf_counter()
                try:
                    while pos < len(body):
                        piece = body[pos:pos + chunk]
                        if (spec.mode == "drop_once" and drop_at is not None and not spec.dropped
                                and start <= drop_at <= end and start + pos + len(piece) > drop_at):
                            with spec.lock:
                                if not spec.dropped:
                                    spec.dropped = True
                                    cut = drop_at - (start + pos)
                                    if cut > 0:
                                        self.wfile.write(piece[:cut])
                                        self.wfile.flush()
                                    self.connection.shutdown(socket.SHUT_RDWR)
                                    self.close_connection = True
                                    return
                        if mock.link is not None:
                            mock.link.take(len(piece))
                        self.wfile.write(piece)
                        pos += len(piece)
                        if rate > 0:
                            want = pos / rate
                            dt = want - (time.perf_counter() - t0)
                            if dt > 0:
                                time.sleep(dt)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    self.close_connection = True

            def _stream(self, body, rate):
                pos, chunk, t0 = 0, 16 * 1024, time.perf_counter()
                try:
                    while pos < len(body):
                        piece = body[pos:pos + chunk]
                        if mock.link is not None:
                            mock.link.take(len(piece))
                        self.wfile.write(piece)
                        pos += len(piece)
                        if rate > 0:
                            dt = pos / rate - (time.perf_counter() - t0)
                            if dt > 0:
                                time.sleep(dt)
                except OSError:
                    self.close_connection = True

            def _send(self, code, body, headers=None):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    try:
                        self.wfile.write(body)
                    except OSError:
                        pass

        class Srv(ThreadingHTTPServer):
            daemon_threads = True
            request_queue_size = 256

            def handle_error(self, request, client_address):
                pass   # client đóng socket (steal/hedge/cancel) là chuyện bình thường

        self.httpd = Srv(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def add(self, path, data=b"", mode="ok", **kw):
        self.files[path] = _Spec(data, mode, **kw)
        return f"http://127.0.0.1:{self.port}{path}"

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def requests_for(self, path):
        with self.log_lock:
            return [x for x in self.log if x[1] == path]

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()

    start = __enter__

    def stop(self):
        self.__exit__()
