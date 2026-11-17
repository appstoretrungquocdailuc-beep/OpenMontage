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
                if spec.mode == "redirect" and kw.get("direct_host") and \
                        str(self.headers.get("Host") or "").startswith(kw["direct_host"]):
                    # Giống GVS thật: cùng path trên host ĐÍCH phục vụ thẳng nội dung (không 302 nữa)
                    tgt = mock.files.get(kw["to"].split("//", 1)[1].split("/", 1)[1].rsplit("?", 1)[0].join(["/", ""]))
                    if tgt is not None:
                        spec, kw = tgt, tgt.kw
                if mock.ttfb:
                    time.sleep(mock.ttfb)
                if kw.get("hdr_delay"):
                    time.sleep(float(kw["hdr_delay"]))
                if kw.get("cold_n") and nreq <= int(kw["cold_n"]):      # cold_n request ĐẦU chậm thêm (kết nối lạnh / tranh chấp đợt đầu)
                    time.sleep(float(kw.get("cold_delay") or 0))
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
                # stall_start/stall_delay/stall_count: `stall_count` request đầu tiên xin đúng byte `stall_start` bị treo
                # header thêm `stall_delay` giây (mô phỏng 1 range kẹt: mất gói SYN/TLS, edge chậm...). Request cứu sau đó nhanh.
                if kw.get("stall_start") is not None and start == int(kw["stall_start"]):
                    with spec.lock:
                        spec.kw["_stalled"] = int(spec.kw.get("_stalled", 0)) + 1
                        _c = spec.kw["_stalled"]
                    if _c <= int(kw.get("stall_count", 1)):
                        time.sleep(float(kw.get("stall_delay", 3.0)))
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


# ======================================================================================================
# MockGVSHosts (thêm cho WS360 tối ưu tải v2) — TLS THẬT, NHIỀU "máy chủ" googlevideo trên 1 cổng loopback (phân biệt theo
# SNI/Host). Mô phỏng: bắt tay kết nối mới tốn conn_delay (≈ RTT TCP+TLS), server_wait theo phân phối (median + % lượt chậm),
# 302 từ host A sang host B (cùng path+query), tham số mn= trong URL, trả mã lỗi theo client (?c=...) hoặc theo host.
# Đếm: số kết nối TLS mới / số request theo host. Ánh xạ tên *.googlevideo.com → 127.0.0.1 bằng install_dns(ws360_net).
# ======================================================================================================
import collections
import ssl
import subprocess
from urllib.parse import parse_qs, urlsplit


def make_cert(d):
    """Chứng chỉ tự ký cho *.googlevideo.com + localhost (cần openssl). Trả (crt, key)."""
    crt, key = os.path.join(str(d), "gvs.crt"), os.path.join(str(d), "gvs.key")
    if not os.path.exists(crt):
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", crt, "-days", "2",
                        "-subj", "/CN=googlevideo.test", "-addext", "subjectAltName=DNS:*.googlevideo.com,DNS:localhost"],
                       check=True, capture_output=True)
    return crt, key


class MockGVSHosts:
    def __init__(self, crt, key, conn_delay=0.0, seed=7):
        self.ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        self.ctx.load_cert_chain(crt, key)
        self.conn_delay = float(conn_delay)
        self.files = {}                         # path → bytes
        self.host_rules = {}                    # host → {redirect_to, server_wait=(base, p_slow, slow), status}
        self.client_status = {}                 # (path, client) → mã HTTP
        self.host_status = {}                   # (host, path) → mã HTTP
        self.conns = collections.Counter()      # host → số kết nối (đã có request) | handshakes = tổng kết nối TLS mới
        self.handshakes = 0
        self.reqs = collections.Counter()       # (host, path) → số request
        self.lock = threading.Lock()
        self.rng = random.Random(seed)
        mock = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def setup(self):
                if mock.conn_delay:
                    time.sleep(mock.conn_delay)                     # 'RTT' bắt tay của kết nối MỚI
                self.request = mock.ctx.wrap_socket(self.request, server_side=True)
                with mock.lock:
                    mock.handshakes += 1
                self._seen = False
                super().setup()

            def do_GET(self):
                host = (self.headers.get("Host") or "").split(":", 1)[0].lower()
                u = urlsplit(self.path)
                path, q = u.path, parse_qs(u.query)
                with mock.lock:
                    mock.reqs[(host, path)] += 1
                    if not self._seen:                              # 1 handler = 1 kết nối (keep-alive): đếm theo host ở request đầu
                        self._seen = True
                        mock.conns[host] += 1
                if path == "/generate_204":
                    return self._send(204, b"")
                rule = mock.host_rules.get(host) or {}
                sw = rule.get("server_wait")
                if sw:
                    base, p_slow, slow = sw
                    with mock.lock:
                        w = slow if mock.rng.random() < p_slow else base
                    time.sleep(w)
                code = mock.host_status.get((host, path)) or mock.client_status.get((path, (q.get("c") or [""])[0]))
                if code:
                    return self._send(int(code), b"denied")
                if rule.get("redirect_to"):
                    port = self.server.server_address[1]
                    self.send_response(302)
                    self.send_header("Location", f"https://{rule['redirect_to']}:{port}{self.path}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                data = mock.files.get(path)
                if data is None:
                    return self._send(404, b"nf")
                rng = self.headers.get("Range")
                if rng:
                    a, b = rng.split("=", 1)[1].split("-", 1)
                    st, en = int(a), (int(b) if b else len(data) - 1)
                    body = data[st:en + 1]
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {st}-{st + len(body) - 1}/{len(data)}")
                else:
                    body = data
                    self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    self.close_connection = True

            def _send(self, code, body):
                self.send_response(code)
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
                pass

        self.httpd = Srv(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def add(self, path, data):
        self.files[path] = data

    def url(self, host, path, mn=(), client=""):
        q = []
        if mn:
            q.append("mn=" + ",".join(mn))
        if client:
            q.append("c=" + client)
        q.append("sig=TESTSIG")
        return f"https://{host}:{self.port}{path}?" + "&".join(q)

    def conns_to(self, host):
        with self.lock:
            return self.conns.get(host, 0)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


def install_dns(net_mod):
    """Mọi *.googlevideo.com → 127.0.0.1 trong DNS cache của ws360_net (chịu được cả DNS.m.clear())."""
    real = net_mod.DNSCache.resolve

    def resolve(self, host, port):
        if str(host).endswith(".googlevideo.com"):
            return socket.getaddrinfo("127.0.0.1", port, 0, socket.SOCK_STREAM), True
        return real(self, host, port)
    net_mod.DNSCache.resolve = resolve
    return lambda: setattr(net_mod.DNSCache, "resolve", real)
