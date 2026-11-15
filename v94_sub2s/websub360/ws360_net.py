# -*- coding: utf-8 -*-
"""WS360 — lớp mạng nhanh cho resolver + downloader (chế độ "fast"; "legacy" = PoolManager cũ, không đổi gì).

1) ĐO TỪNG GIAI ĐOẠN mỗi request: DNS | TCP CONNECT | TLS | SERVER_WAIT (gửi xong → có header) | kết nối mới hay dùng lại.
2) DNS cache (TTL 5 phút) + tải trước DNS các máy chủ video cùng cụm (rr1..rr20---sn-xxx.googlevideo.com).
3) GIỮ KẾT NỐI ẤM: máy chủ Innertube (resolver) và các máy chủ video vừa dùng — mỗi ~40 s gửi GET /generate_204
   (endpoint công khai trả 204, thân rỗng) để Google không đóng kết nối rảnh → video mới KHÔNG phải bắt tay TCP+TLS lại.
Không đổi nội dung request/response, không đụng tới chữ ký URL, không bỏ qua giới hạn nào của YouTube.
"""
from __future__ import annotations

import re
import socket
import threading
import time
from collections import OrderedDict

import urllib3
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.util.connection import create_connection
from urllib3.util.wait import wait_for_read
import ssl as _ssl

_TL = threading.local()
GV_RE = re.compile(r"^rr(\d+)---(sn-[a-z0-9-]+)\.googlevideo\.com$", re.I)


def _ms(a, b):
    return None if a is None or b is None else round((b - a) / 1e6, 2)


# ------------------------------------------------------------------ đo theo request (thread-local)
class trace:
    """with trace() as t: pool().request(...)  → t.d = {dns_ms, tcp_ms, tls_ms, new_conn, server_wait_ms, ...}"""

    def __enter__(self):
        self.prev = getattr(_TL, "t", None)
        self.d = {"new_conn": False, "conns": 0}
        _TL.t = self.d
        return self

    def __exit__(self, *a):
        _TL.t = self.prev
        return False


def _cur():
    return getattr(_TL, "t", None)


# ------------------------------------------------------------------ DNS cache
class DNSCache:
    def __init__(self, ttl=300.0):
        self.ttl = ttl
        self.lock = threading.Lock()
        self.m = {}
        self.hits = 0
        self.miss = 0

    def resolve(self, host, port):
        k = (host, port)
        now = time.monotonic()
        with self.lock:
            e = self.m.get(k)
            if e and now - e[0] < self.ttl:
                self.hits += 1
                return e[1], True
        res = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
        with self.lock:
            self.m[k] = (now, res)
            self.miss += 1
        return res, False

    def prefetch(self, host, port=443):
        try:
            self.resolve(host, port)
        except OSError:
            pass


DNS = DNSCache()


# v2 #3: kết nối MỞ SẴN (chưa gửi request) qua TLS 1.3 nhận NewSessionTicket SAU bắt tay → socket "có dữ liệu chờ đọc" → urllib3
# tưởng kết nối đã đứt (is_connection_dropped) → đóng và mở lại → mở sẵn vô ích. Bật cờ này: đọc thử không chặn; chỉ có bản ghi TLS
# (ticket) → vẫn sống. Có dữ liệu ứng dụng lạ / đã đóng → coi là đứt như cũ. (Engine bật cùng net_mn_preconnect.)
TICKET_AWARE = False
TICKET_STATS = {"kept": 0}


class TimedHTTPSConnection(HTTPSConnection):
    """Tách DNS / TCP / TLS; đánh dấu thời điểm gửi request và nhận header (SERVER_WAIT)."""

    @property
    def is_connected(self) -> bool:
        if self.sock is None:
            return False
        if not wait_for_read(self.sock, timeout=0.0):
            return True
        if not TICKET_AWARE or not isinstance(self.sock, _ssl.SSLSocket):
            return False
        old = self.sock.gettimeout()
        try:
            self.sock.settimeout(0.0)
            self.sock.recv(1)
            return False                                   # có dữ liệu ứng dụng lạ hoặc b"" (đã đóng) → coi là đứt
        except (_ssl.SSLWantReadError, BlockingIOError):
            TICKET_STATS["kept"] += 1
            return True                                    # chỉ là ticket TLS 1.3 → kết nối còn dùng được
        except Exception:
            return False
        finally:
            try:
                self.sock.settimeout(old)
            except Exception:
                pass

    def _new_conn(self):
        t = _cur()
        t0 = time.perf_counter_ns()
        infos, cached = DNS.resolve(self._dns_host, self.port)
        t1 = time.perf_counter_ns()
        err = None
        sock = None
        for fam, _, _, _, sa in infos[:4]:
            try:
                sock = create_connection((sa[0], self.port), self.timeout, source_address=self.source_address,
                                         socket_options=self.socket_options)
                break
            except OSError as e:
                err = e
        if sock is None:
            raise urllib3.exceptions.NewConnectionError(self, f"không kết nối được {self._dns_host}: {err}")
        t2 = time.perf_counter_ns()
        if t is not None:
            t.update(dns_ms=_ms(t0, t1), dns_cached=cached, tcp_ms=_ms(t1, t2), ip=sa[0])
        self._t_tcp_done = t2
        return sock

    def connect(self):
        t = _cur()
        t0 = time.perf_counter_ns()
        super().connect()
        t1 = time.perf_counter_ns()
        CONN_STATS["new"] += 1
        if t is not None:
            t["new_conn"] = True
            t["conns"] = t.get("conns", 0) + 1
            t["tls_ms"] = _ms(getattr(self, "_t_tcp_done", t0), t1)
            t["connect_total_ms"] = _ms(t0, t1)
            reused = getattr(self.sock, "session_reused", None)       # TLS session resumption (Python chỉ resume khi được đưa session)
            if reused is not None:
                t["tls_resumed"] = bool(reused)

    def request(self, *a, **k):
        t = _cur()
        if t is not None:
            t["t_send"] = time.perf_counter_ns()
        r = super().request(*a, **k)
        if t is not None:
            t["t_sent"] = time.perf_counter_ns()
        return r

    def getresponse(self, *a, **k):
        r = super().getresponse(*a, **k)
        t = _cur()
        if t is not None:
            t["t_hdr"] = time.perf_counter_ns()
            t["server_wait_ms"] = _ms(t.get("t_sent"), t["t_hdr"])
        return r


class TimedHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = TimedHTTPSConnection


# ------------------------------------------------------------------ SSLContext DÙNG CHUNG (v2 #1)
# urllib3 (không truyền ssl_context) tạo SSLContext MỚI + load_default_certs() cho TỪNG kết nối mới. Windows: duyệt kho chứng chỉ
# hệ thống mỗi lần (~30–100 ms, giữ GIL). Dùng chung 1 context (tạo 1 lần) → bỏ chi phí đó; vẫn xác minh chứng chỉ + hostname
# (urllib3 đặt verify_mode=CERT_REQUIRED, check_hostname=True như bản thường).
_SSL_CTX = None
_SSL_LOCK = threading.Lock()
SSL_STATS = {"created": 0}
CONN_STATS = {"new": 0}


def shared_ssl_context():
    global _SSL_CTX
    if _SSL_CTX is None:
        with _SSL_LOCK:
            if _SSL_CTX is None:
                from urllib3.util import ssl_ as _u3ssl
                ctx = _u3ssl.create_urllib3_context()
                ctx.load_default_certs()
                SSL_STATS["created"] += 1
                _SSL_CTX = ctx
    return _SSL_CTX


def reset_shared_ssl_context():
    """Chỉ cho test/bench: bỏ context dùng chung (lần sau tạo lại)."""
    global _SSL_CTX
    with _SSL_LOCK:
        _SSL_CTX = None


def socket_opts(rcvbuf_mb=0.0):
    """TCP_NODELAY (= mặc định urllib3) + SO_RCVBUF (nếu rcvbuf_mb > 0). Truyền socket_options sẽ THAY danh sách mặc định nên
    phải giữ TCP_NODELAY."""
    opts = [(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)]
    if rcvbuf_mb and float(rcvbuf_mb) > 0:
        opts.append((socket.SOL_SOCKET, socket.SO_RCVBUF, int(float(rcvbuf_mb) * 1024 * 1024)))
    return opts


def pool_kwargs(shared_ssl=False, rcvbuf_mb=0.0):
    kw = {}
    if shared_ssl:
        kw["ssl_context"] = shared_ssl_context()
    if rcvbuf_mb and float(rcvbuf_mb) > 0:
        kw["socket_options"] = socket_opts(rcvbuf_mb)
    return kw


def make_pool(mode="fast", shared_ssl=False, rcvbuf_mb=0.0):
    kw = dict(num_pools=48, maxsize=24, block=False, retries=False, timeout=urllib3.Timeout(connect=2.0, read=8.0))
    kw.update(pool_kwargs(shared_ssl, rcvbuf_mb))
    pm = urllib3.PoolManager(**kw)
    if mode == "fast":
        pm.pool_classes_by_scheme = {"http": HTTPConnectionPool, "https": TimedHTTPSPool}
        pm.key_fn_by_scheme = dict(pm.key_fn_by_scheme)
    return pm


# ------------------------------------------------------------------ giữ kết nối ấm
class NetWarmer:
    """Mỗi `interval` giây: gửi k request /generate_204 SONG SONG tới mỗi máy chủ đích (tạo/giữ k kết nối rảnh trong pool).
    Máy chủ video: học từ các lần tải gần nhất (LRU), kèm tải trước DNS các máy chủ cùng cụm."""

    def __init__(self, pool_fn, log, innertube_hosts=("www.youtube.com",), k_innertube=3, media_max=6, interval=40.0,
                 siblings=20):
        self.pool_fn = pool_fn
        self.log = log
        self.inner = list(dict.fromkeys(innertube_hosts))
        self.k_inner = max(1, int(k_innertube))
        self.media = OrderedDict()
        self.media_max = max(0, int(media_max))
        self.interval = max(10.0, float(interval))
        self.siblings = max(0, int(siblings))
        self.lock = threading.Lock()
        self.stop_ev = threading.Event()
        self.th = None
        self.stats = {"pings": 0, "errors": 0, "last_ms": None}

    def learn(self, url_or_host):
        h = url_or_host
        m = re.match(r"https?://([^/:?#]+)", str(url_or_host))
        if m:
            h = m.group(1)
        if not h or not h.endswith("googlevideo.com") or self.media_max <= 0:
            return
        with self.lock:
            self.media.pop(h, None)
            self.media[h] = time.time()
            while len(self.media) > self.media_max:
                self.media.popitem(last=False)
        g = GV_RE.match(h)
        if g and self.siblings:
            threading.Thread(target=lambda: [DNS.prefetch(f"rr{i}---{g.group(2)}.googlevideo.com") for i in range(1, self.siblings + 1)],
                             daemon=True, name="DNS-PREFETCH").start()

    def targets(self):
        with self.lock:
            med = list(self.media)
        return [(h, self.k_inner) for h in self.inner] + [(h, 1) for h in med]

    def ping(self, host, k=1, timeout=3.0):
        def one():
            try:
                r = self.pool_fn().request("GET", f"https://{host}/generate_204", preload_content=True, retries=False,
                                           redirect=False, timeout=urllib3.Timeout(connect=2.0, read=timeout))
                r.release_conn()
                self.stats["pings"] += 1
            except Exception:
                self.stats["errors"] += 1
        ths = [threading.Thread(target=one, daemon=True) for _ in range(k)]
        [t.start() for t in ths]
        [t.join(timeout + 3) for t in ths]

    def warm_now(self):
        t0 = time.perf_counter_ns()
        ths = [threading.Thread(target=self.ping, args=(h, k), daemon=True) for h, k in self.targets()]
        [t.start() for t in ths]
        [t.join(10) for t in ths]
        self.stats["last_ms"] = _ms(t0, time.perf_counter_ns())

    def start(self):
        if self.th:
            return
        self.stop_ev.clear()

        def loop():
            self.warm_now()
            while not self.stop_ev.wait(self.interval):
                self.warm_now()
        self.th = threading.Thread(target=loop, daemon=True, name="NET-WARM")
        self.th.start()

    def stop(self):
        self.stop_ev.set()
        self.th = None
