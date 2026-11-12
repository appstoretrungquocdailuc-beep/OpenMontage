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


class TimedHTTPSConnection(HTTPSConnection):
    """Tách DNS / TCP / TLS; đánh dấu thời điểm gửi request và nhận header (SERVER_WAIT)."""

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
        if t is not None:
            t["new_conn"] = True
            t["conns"] = t.get("conns", 0) + 1
            t["tls_ms"] = _ms(getattr(self, "_t_tcp_done", t0), t1)
            t["connect_total_ms"] = _ms(t0, t1)

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


def make_pool(mode="fast"):
    kw = dict(num_pools=48, maxsize=24, block=False, retries=False, timeout=urllib3.Timeout(connect=2.0, read=8.0))
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
