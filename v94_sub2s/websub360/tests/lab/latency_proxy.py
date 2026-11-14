# -*- coding: utf-8 -*-
"""Bộ chuyển tiếp TCP mô phỏng 'proxy ở xa': mỗi KẾT NỐI MỚI phải chờ connect_ms (≈ CONNECT + bắt tay TLS qua proxy) trước khi
dữ liệu đi qua; kết nối rảnh quá idle_s thì bị đóng (≈ máy chủ/proxy hết keep-alive). Kết nối đã mở sẵn (preconnect) không phải chờ."""
import socket
import threading
import time


class LatencyProxy:
    def __init__(self, upstream_port: int, connect_ms: float = 300.0, idle_s: float = 2.0):
        self.up = upstream_port
        self.connect_s = connect_ms / 1000.0
        self.idle_s = idle_s
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(64)
        self.port = self.srv.getsockname()[1]
        self.conns = 0
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            self.conns += 1
            threading.Thread(target=self._handle, args=(c,), daemon=True).start()

    def _handle(self, c):
        time.sleep(self.connect_s)                       # 'bắt tay' kết nối mới
        try:
            u = socket.create_connection(("127.0.0.1", self.up))
        except OSError:
            c.close()
            return
        last = [time.time()]

        def pipe(a, b):
            try:
                while True:
                    a.settimeout(0.2)
                    try:
                        d = a.recv(65536)
                    except socket.timeout:
                        if time.time() - last[0] > self.idle_s:
                            break
                        continue
                    if not d:
                        break
                    last[0] = time.time()
                    b.sendall(d)
            except OSError:
                pass
            for x in (a, b):
                try:
                    x.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        t = threading.Thread(target=pipe, args=(u, c), daemon=True)
        t.start()
        pipe(c, u)
        t.join(1)
        c.close()
        u.close()

    def close(self):
        self.srv.close()
