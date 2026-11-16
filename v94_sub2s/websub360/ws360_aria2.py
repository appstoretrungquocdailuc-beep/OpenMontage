# -*- coding: utf-8 -*-
"""ARIA2 RPC (THỬ NGHIỆM) cho WS360 — dựa trên 3 hàm của 360pok.py, có chỉnh cho an toàn với YouTube.

360pok.py (đã đọc kỹ):
  _start_aria2_rpc_daemon : 1 tiến trình aria2c cố định (--enable-rpc, chỉ nghe 127.0.0.1, cổng trống ngẫu nhiên, secret),
                            --file-allocation=none --disk-cache=64M --max-concurrent-downloads=16; chờ getVersion ≤ 1,2 s;
                            atexit → aria2.shutdown. Lợi: không tốn thời gian tạo tiến trình aria2c cho mỗi video.
  _aria2_tuned_split      : ≤ 5 MiB → 8 kết nối, ≤ 15 MiB → 12, lớn hơn → 16.
  _download_aria2c_rpc    : aria2.addUri {split = max-connection-per-server = N, min-split-size 1M, max-tries 2, retry-wait 0,
                            connect-timeout 2, timeout 4, stream-piece-selector default, reuse-uri true}; hỏi tellStatus mỗi 15 ms;
                            thành công = 'complete' + file ≥ min_bytes; lỗi/timeout → forceRemove rồi removeDownloadResult.

Khác 360pok (để giữ quy tắc an toàn của WS360):
  * max-tries = 1 (360pok: 2, retry-wait 0) → 403/429 KHÔNG bị aria2 thử lại ngay trên mọi kết nối (đúng 1 request bị từ chối).
  * Lỗi HTTP đọc từ errorMessage ('status=403/429…', errorCode 3 = 404) → báo đúng mã cho WS360 (429 → chờ ≥ 10 s như cũ).
  * Thành công CHỈ khi đúng dung lượng clen (360pok: ≥ min_bytes); sau đó WS360 vẫn kiểm MP4 + ffprobe như mọi đường tải.
  * Đo thêm: TTFB (gửi addUri → byte đầu, hỏi mỗi 5 ms), số kết nối THỰC TẾ (max 'connections' của tellStatus).
  * Không ghi URL/secret vào log; secret ngẫu nhiên (secrets.token_hex)."""
import atexit
import http.client
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from pathlib import Path


def tuned_split(expected_bytes: int) -> int:
    """= _aria2_tuned_split của 360pok."""
    mb = (expected_bytes or 0) / (1024 * 1024)
    if mb and mb <= 5:
        return 8
    if mb and mb <= 15:
        return 12
    return 16


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


_STATUS_RE = re.compile(r"status=(\d{3})")


class Aria2Error(IOError):
    pass


class Aria2Rpc:
    """1 tiến trình aria2c --enable-rpc dùng chung (như _start_aria2_rpc_daemon). An toàn đa luồng: mỗi luồng 1 kết nối RPC."""

    def __init__(self, path: str):
        self.path = path
        self.proc = None
        self.port = None
        self.secret = None
        self.lock = threading.Lock()
        self.tls = threading.local()
        self.started_ms = None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and bool(self.port)

    def call(self, method, params=None, timeout=0.5):
        if not self.port or not self.secret:
            return None
        body = json.dumps({"jsonrpc": "2.0", "id": "ws360", "method": method,
                           "params": [f"token:{self.secret}"] + list(params or [])}, separators=(",", ":")).encode("utf-8")
        conn = getattr(self.tls, "conn", None)
        for attempt in range(2):
            try:
                if conn is None:
                    conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
                    self.tls.conn = conn
                conn.timeout = timeout
                if conn.sock is not None:
                    conn.sock.settimeout(timeout)
                conn.request("POST", "/jsonrpc", body=body, headers={"Content-Type": "application/json", "Connection": "keep-alive"})
                resp = conn.getresponse()
                raw = resp.read()
                obj = json.loads(raw.decode("utf-8", errors="replace"))
                if "error" in obj and obj.get("error"):
                    return {"_rpc_error": obj["error"]}
                return obj.get("result")
            except Exception:  # noqa: BLE001
                try:
                    if conn is not None:
                        conn.close()
                except Exception:  # noqa: BLE001
                    pass
                conn = None
                self.tls.conn = None
                if attempt:
                    return None
        return None

    def start(self, wait_s: float = 2.0) -> bool:
        with self.lock:
            if self.alive():
                return True
            if not self.path:
                return False
            t0 = time.perf_counter()
            try:
                port, secret = _free_port(), secrets.token_hex(16)
                cmd = [self.path, "--enable-rpc=true", "--rpc-listen-all=false", f"--rpc-listen-port={port}", f"--rpc-secret={secret}",
                       "--console-log-level=error", "--summary-interval=0", "--download-result=hide", "--file-allocation=none",
                       "--disk-cache=64M", "--max-concurrent-downloads=16", "--auto-file-renaming=false", "--allow-overwrite=true"]
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
                self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                             creationflags=flags)
                self.port, self.secret = port, secret
                deadline = time.perf_counter() + wait_s
                while time.perf_counter() < deadline:
                    if self.proc.poll() is not None:
                        break
                    if self.call("aria2.getVersion", timeout=0.2):
                        self.started_ms = round((time.perf_counter() - t0) * 1000, 1)
                        return True
                    time.sleep(0.02)
            except Exception:  # noqa: BLE001
                pass
            self._kill()
            return False

    def _kill(self):
        try:
            if self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()
                self.proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            pass
        self.proc, self.port, self.secret = None, None, None

    def stop(self):
        try:
            if self.alive():
                self.call("aria2.shutdown", timeout=0.3)
                self.proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            pass
        with self.lock:
            self._kill()

    def _remove(self, gid):
        """Dừng hẳn 1 lượt tải (trước khi đường tải khác ghi cùng file): forceRemove → chờ trạng thái kết thúc → xoá kết quả."""
        try:
            self.call("aria2.forceRemove", [gid], timeout=0.3)
            end = time.perf_counter() + 2.0
            while time.perf_counter() < end:
                st = self.call("aria2.tellStatus", [gid, ["status"]], timeout=0.3)
                if not isinstance(st, dict) or st.get("status") in ("removed", "error", "complete") or "_rpc_error" in st:
                    break
                time.sleep(0.01)
        finally:
            self.call("aria2.removeDownloadResult", [gid], timeout=0.3)

    def download(self, url: str, dest: Path, n: int, info: dict, expected: int = 0, cancel=None, http_exc=None,
                 poll_s: float = 0.005, deadline_s: float = 180.0) -> int:
        """Tải url → dest bằng N kết nối. info nhận: ttfb_ms, elapsed_ms, transfer_ms, bytes, connections (thực tế, lớn nhất),
        aria2_conns_req. Lỗi HTTP → http_exc(code) (vd HttpStatus); lỗi khác → Aria2Error. Không bao giờ để file dở dang."""
        if not self.start():
            raise Aria2Error("không khởi động được aria2 RPC")
        dest = Path(dest)
        ctrl = Path(str(dest) + ".aria2")
        for p in (dest, ctrl):
            p.unlink(missing_ok=True)
        opts = {"dir": str(dest.parent), "out": dest.name, "split": str(int(n)), "max-connection-per-server": str(int(n)),
                "min-split-size": "1M", "file-allocation": "none", "allow-overwrite": "true", "auto-file-renaming": "false",
                "max-tries": "1", "retry-wait": "0", "connect-timeout": "2", "timeout": "4", "lowest-speed-limit": "0",
                "stream-piece-selector": "default", "reuse-uri": "true"}
        t0 = time.perf_counter_ns()
        gid = self.call("aria2.addUri", [[url], opts], timeout=0.5)
        if not isinstance(gid, str):
            raise Aria2Error("aria2.addUri không trả gid")
        first = None
        maxc = 0
        st = None
        ok = False                      # aria2 báo 'complete'
        good = False                    # complete + đúng dung lượng → mới được giữ file
        try:
            end = time.perf_counter() + deadline_s
            while time.perf_counter() < end:
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("dừng")
                st = self.call("aria2.tellStatus", [gid, ["status", "completedLength", "totalLength", "connections", "errorCode",
                                                         "errorMessage"]], timeout=0.5)
                if isinstance(st, dict) and "_rpc_error" not in st:
                    maxc = max(maxc, int(st.get("connections") or 0))
                    if first is None and int(st.get("completedLength") or 0) > 0:
                        first = time.perf_counter_ns()
                    s = st.get("status")
                    if s == "complete":
                        ok = True
                        break
                    if s in ("error", "removed"):
                        msg = str(st.get("errorMessage") or "")
                        m = _STATUS_RE.search(msg)
                        code = int(m.group(1)) if m else (404 if str(st.get("errorCode")) == "3" else None)
                        info["aria2_error"] = f"errorCode {st.get('errorCode')}" + (f" HTTP {code}" if code else "")
                        if code and http_exc is not None:
                            raise http_exc(code)
                        raise Aria2Error(f"aria2 lỗi errorCode {st.get('errorCode')}")
                time.sleep(poll_s)
            if not ok:
                raise Aria2Error("aria2 quá thời gian")
            t_end = time.perf_counter_ns()
            size = dest.stat().st_size if dest.exists() else 0
            if expected and size != int(expected):
                raise Aria2Error(f"sai dung lượng {size}/{expected}")
            ms_ = lambda a, b: round((b - a) / 1e6, 2) if a is not None and b is not None else None
            info.update(ttfb_ms=ms_(t0, first or t_end), elapsed_ms=ms_(t0, t_end), transfer_ms=ms_(first, t_end) if first else None,
                        bytes=size, connections=maxc, aria2_conns_req=int(n), engine="aria2_rpc")
            good = True
            return size
        finally:
            if not good:
                if not ok:
                    self._remove(gid)
                else:
                    self.call("aria2.removeDownloadResult", [gid], timeout=0.3)
                for p in (dest, ctrl):
                    try:
                        p.unlink(missing_ok=True)
                    except OSError:
                        pass
            else:
                self.call("aria2.removeDownloadResult", [gid], timeout=0.3)
                ctrl.unlink(missing_ok=True)


_DAEMONS = {}
_DLOCK = threading.Lock()


def daemon(path: str) -> Aria2Rpc:
    with _DLOCK:
        d = _DAEMONS.get(path)
        if d is None:
            d = _DAEMONS[path] = Aria2Rpc(path)
        return d


def _stop_all():
    for d in list(_DAEMONS.values()):
        d.stop()


atexit.register(_stop_all)
