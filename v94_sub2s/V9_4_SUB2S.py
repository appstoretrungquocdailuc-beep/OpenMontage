# -*- coding: utf-8 -*-
# ==============================================================================
# 🛠️ Downloader_Realtime_Layout_V5.py - FINAL FULL STABLE
# 🛑 V8.6: FRESH-SHORT NATIVE-FIRST — 244→135 + NATIVE SINGLE-TRACK JA + SHARED-REDIRECT RANGE + LAZY FALLBACK
# 🚀 V9.4 SUB-2S: dựa trên V9.3 AUDIO (giữ nguyên file V9_3_AUDIO.py để rollback).
#    Mọi thay đổi V9.4 nằm sau cờ V94_* (xem khối "V9.4 FEATURE FLAGS"); tắt cờ = hành vi V9.3.
#    + Telemetry span JSONL (perf_counter_ns, không print trên hot path, URL được redact)
#    + Range: xác thực Content-Range start/end/total, tự co/giãn theo size thật (KHÔNG tải lại)
#    + Range: size không biết → Range-first có hạn mức thay vì DIRECT-1GET blocking
#    + 403/410/429: không fallback aria2 cùng signed URL; 1 SAFE-REFRESH dùng chung
#    + FIRST-WAVE-RACE giới hạn số request kép mỗi job (chống bão socket)
#    + Đuôi file audio đúng codec (.m4a cho mp4a, .webm cho opus)
#    + ffprobe xác thực file sau mux trước khi ghi DONE
#    + Realtime: poll 10ms, Excel engine calamine nếu có, keepalive nhường job đang chạy
# ==============================================================================

import os
import sys
import time
import json
import threading
import shutil
import subprocess
import re
import random
import copy
import urllib.parse
import http.client
import socket
import atexit
from functools import partial
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED, Future

# --- BẮT BUỘC: Cài đặt thư viện Qt cho Python ---
try:
    from PySide6.QtWidgets import (
        QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
        QFileDialog, QTextEdit, QLineEdit, QFrame, QMessageBox, 
        QSpinBox, QCheckBox, QRadioButton, QComboBox, QGridLayout, QSizePolicy
    )
    from PySide6.QtGui import QFont, QTextCursor 
    from PySide6.QtCore import Qt, QTimer, Signal, QObject, QEventLoop
except Exception:
    raise RuntimeError("PySide6 is required. Install via: pip install PySide6")

# --- BẮT BUỘC: Thư viện yt_dlp ---
try:
    import yt_dlp
except ImportError:
    yt_dlp = None

# --- FAST LINK: urllib3 là optional. Nếu có sẽ dùng connection pool dùng chung giữa các thread. ---
try:
    import urllib3
except Exception:
    urllib3 = None

_FAST_YT_POOL = None
_FAST_YT_POOL_LOCK = threading.Lock()
_FAST_HTTP_TLS = threading.local()
_FAST_YTDLP_TLS = threading.local()
_FAST_PLAYER_EXECUTOR = ThreadPoolExecutor(max_workers=6, thread_name_prefix="YTPlayerRace")
# V7.4: independent fast yt-dlp client race. Keep this separate from the robust resolver
# so a slow EJS/challenge path can never queue behind or block the low-latency clients.
_YTDLP_FAST_CLIENT_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="YT244FastClient")
_FAST_480_VISITOR_LOCK = threading.Lock()
_FAST_480_VISITOR_DATA = ""
_FAST_480_VISITOR_REFRESH_LOCK = threading.Lock()
_FAST_480_VISITOR_LAST_REFRESH = 0.0
FAST_480_VISITOR_REFRESH_SEC = 300.0
# V7 480p: orchestration pool lets native player race, fast yt-dlp and robust yt-dlp
# resolve in parallel. Media downloads start as soon as each direct URL is available.
_BUNDLE_RESOLVE_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="YT480Resolve")
_MEDIA_DOWNLOAD_EXECUTOR = ThreadPoolExecutor(max_workers=12, thread_name_prefix="YT480Media")

def _submit_media_dedicated(fn, *args, **kwargs):
    """Start one media job immediately on its own daemon thread.

    Realtime rule: a fresh URL must never wait behind an older/stalled transfer in a
    shared executor queue. The returned Future is compatible with .done()/.result().
    """
    fut = Future()
    def _runner():
        if not fut.set_running_or_notify_cancel():
            return
        try:
            fut.set_result(fn(*args, **kwargs))
        except BaseException as exc:
            fut.set_exception(exc)
    threading.Thread(target=_runner, name="YT480MediaNow", daemon=True).start()
    return fut
HARD_480_PRIMARY_ITAG = "244"
HARD_480_FALLBACK_ITAG = "135"
HARD_480_VIDEO_ITAGS = (HARD_480_PRIMARY_ITAG, HARD_480_FALLBACK_ITAG)
HARD_480_AVAILABILITY_WAIT_SEC = 8.0
HARD_480_NATIVE_RETRY_SEC = 0.12   # V9.2: video vừa đăng chưa có 244 → hỏi lại nhanh hơn
HARD_480_ROBUST_RETRY_SEC = 1.00
DIRECT_MAX_REDIRECTS = 4
JA_LANG_PREFIXES = ("ja", "jpn")
MIN_BYTES_AUDIO = 64 * 1024
# Dedicated resolver workers keep YoutubeDL instances warm on stable threads instead of
# re-creating them on arbitrary GUI worker threads. This is only used when native player
# race cannot immediately return progressive itag 18.
_YTDLP_RESOLVER_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="YTDLPWarmResolver")
_FAST_KEEPALIVE_LOCK = threading.Lock()
_FAST_KEEPALIVE_STARTED = False
_FAST_KEEPALIVE_STOP = threading.Event()

# Persistent aria2 RPC daemon: bỏ chi phí spawn aria2c.exe cho từng video.
_ARIA2_RPC_LOCK = threading.Lock()
_ARIA2_RPC_PROC = None
_ARIA2_RPC_PORT = None
_ARIA2_RPC_SECRET = None
_ARIA2_RPC_TLS = threading.local()

# Direct GoogleVideo pool for small progressive MP4s. For 1-15 MB files a single hot
# HTTP GET is usually faster than RPC setup + many Range connections. No HEAD and no
# bytes=0-0 preflight are performed on this path.
_DIRECT_MEDIA_POOL = None
_DIRECT_MEDIA_POOL_LOCK = threading.Lock()
DIRECT_SINGLE_GET_MAX_BYTES = 3 * 1024 * 1024
DIRECT_READ_CHUNK = 512 * 1024
# V7.9: audio 139 can be only ~0.3-0.5 MiB on Shorts. Un-ranged GET was observed
# stalling ~29.5 s before first byte, so every valid-size JA audio uses Range-first.
RANGE_AUDIO_MIN_BYTES = 64 * 1024
RANGE_VIDEO_MIN_BYTES = 256 * 1024
# V8.6: fresh Shorts are only a few MiB. 1MiB min-parts collapsed a 3.11MiB
# video from the requested 4 workers to only 3. Keep parts small enough to fan out.
RANGE_MIN_PART_BYTES = 256 * 1024
RANGE_AUDIO_MIN_PART_BYTES = 64 * 1024
TINY_VIDEO_NO_PROBE_MAX_BYTES = 0  # V8.6: shared redirect probe is faster/more stable than N workers each following redirects
TINY_AUDIO_NO_PROBE_MAX_BYTES = 0  # same for tiny JA audio
LAZY_ROBUST_START_SEC = 0.45
SHORT_NATIVE_RETRY_SEC = 0.06
SHORT_FAST_YTDLP_START_SEC = 0.55
SHORT_ROBUST_START_SEC = 1.15
RANGE_READ_CHUNK = 512 * 1024
_RANGE_PROGRESS = {}
_RANGE_PROGRESS_LOCK = threading.Lock()


# ==============================================================================
# V9.4 FEATURE FLAGS — tất cả có thể ghi đè bằng biến môi trường V94_<TÊN>=0/1/số.
# Đặt cờ = giá trị V9.3 tương ứng để quay về đúng hành vi cũ mà không cần đổi file.
# ==============================================================================
def _v94_env(name, default):
    raw = os.environ.get(f"V94_{name}")
    if raw is None or str(raw).strip() == "":
        return default
    raw = str(raw).strip()
    if isinstance(default, bool):
        return raw.lower() in ("1", "true", "yes", "on")
    try:
        return type(default)(raw)
    except Exception:
        return default


V94_VERSION = "V9.6-SUB2S"     # V9.5 + giảm TTFB/truyền cho 360p (V96_*)
# Range: kiểm tra Content-Range start/end khớp đúng request (V9.3 chỉ đọc tổng).
V94_STRICT_CONTENT_RANGE = _v94_env("STRICT_CONTENT_RANGE", True)
# Range: size thật ≠ ước tính → co/giãn tại chỗ, không vứt dữ liệu và tải lại (V9.3 = restart).
V94_RANGE_ADAPTIVE_RESIZE = _v94_env("RANGE_ADAPTIVE_RESIZE", True)
# Size không biết (contentLength=0): Range-first 1 request đầu rồi fan-out theo Content-Range.
V94_RANGE_UNKNOWN_SIZE = _v94_env("RANGE_UNKNOWN_SIZE", True)
V94_UNKNOWN_FIRST_BYTES_AUDIO = _v94_env("UNKNOWN_FIRST_BYTES_AUDIO", 512 * 1024)
V94_UNKNOWN_FIRST_BYTES_VIDEO = _v94_env("UNKNOWN_FIRST_BYTES_VIDEO", 2 * 1024 * 1024)
V94_UNKNOWN_FIRST_WORKERS = _v94_env("UNKNOWN_FIRST_WORKERS", 4)
# DIRECT-1GET (chỉ còn là fallback cuối) có deadline tổng, không thể treo hàng chục giây.
V94_DIRECT_1GET_DEADLINE_SEC = _v94_env("DIRECT_1GET_DEADLINE_SEC", 12.0)
# 403/410/429 trên signed URL: không thử lại cùng URL bằng aria2 (vô ích + tăng tải).
V94_NO_ARIA2_ON_AUTH_FAIL = _v94_env("NO_ARIA2_ON_AUTH_FAIL", True)
# FIRST-WAVE-RACE: số request đầu được bắn kép (host chính + replica mn) mỗi file.
# -1 = không giới hạn = V9.3 (mặc định từ V9.4.1); 0 = tắt race; N>0 = tối đa N request kép/file.
# V9.4.0 đặt trần 4 (chống "bão socket") nhưng bench 2 host (perf_report/race_*): khi host chính 302 + chậm
# và replica nhanh (đúng như log live 17:09: alt-win=3) trần 4 làm Shorts 2.88MB chậm hơn 140ms và video
# 24MB chậm hơn 20%; khi replica chậm, trần chỉ bớt socket, KHÔNG nhanh hơn. → mặc định trở về V9.3.
V94_FIRST_WAVE_RACE_MAX = _v94_env("FIRST_WAVE_RACE_MAX", -1)
# Hedge thread pool (V9.3 = 192). Thread tạo lười nên chỉ là trần.
V94_HEDGE_POOL_MAX = _v94_env("HEDGE_POOL_MAX", 192)
# Ghi đè số worker Range (0 = heuristic V9.3). Dùng cho thí nghiệm 4/6/8/12/16/24/32/40.
V94_RANGE_WORKERS_VIDEO = _v94_env("RANGE_WORKERS_VIDEO", 0)
V94_RANGE_WORKERS_AUDIO = _v94_env("RANGE_WORKERS_AUDIO", 0)
V94_RANGE_MAX_WORKERS = _v94_env("RANGE_MAX_WORKERS", 40)
# Sau mux: ffprobe phải thấy video + audio + duration > 0 rồi mới đi tiếp/ghi DONE.
V94_VALIDATE_MUX = _v94_env("VALIDATE_MUX", True)
# Cách xác thực: "mux_log" = đọc stream output từ log của chính lệnh ffmpeg mux (0 process thêm),
# tự rơi về ffprobe nếu không đọc được; "ffprobe" = luôn spawn ffprobe (+50-70ms).
V94_VALIDATE_MODE = str(os.environ.get("V94_VALIDATE_MODE", "mux_log") or "mux_log").strip().lower()
# Realtime: chu kỳ stat trigger (V9.3 = 0.025).
V94_WAKEUP_CHECK_SEC = _v94_env("WAKEUP_CHECK_SEC", 0.010)
# Excel: dùng engine calamine (Rust) nếu đã cài python-calamine; nếu không → openpyxl như cũ.
V94_EXCEL_CALAMINE = _v94_env("EXCEL_CALAMINE", True)
# Keepalive không bắn HEAD/visitor GET khi đang có job tải (tránh tranh băng thông/CPU).
V94_KEEPALIVE_YIELD = _v94_env("KEEPALIVE_YIELD", True)
# Suy luận "single-track = JA" bị chặn nếu defaultAudioLanguage ghi rõ ngôn ngữ khác.
V94_JA_INFER_GUARD = _v94_env("JA_INFER_GUARD", True)
# IPC truyền URL trực tiếp: KHÔNG thêm — chưa xác nhận scanner V34 ghi URL vào trigger_wakeup.txt.
# Trigger + Excel giữ nguyên giao thức V9.3 (README mục "IPC").
# Công tắc đề xuất: chọn 135 nếu 135 có trước 244 trong cùng snapshot? KHÔNG — mặc định giữ 244→135.
# Chỉ bật khi đã có số đo + người dùng đồng ý. Khi bật: snapshot có cả 2 → chọn file nhỏ hơn.
V94_FASTEST_VALID_480P = _v94_env("FASTEST_VALID_480P", False)
# Telemetry JSONL (perf_report/telemetry_*.jsonl). Ghi bởi thread nền, không chặn hot path.
V94_TELEMETRY = _v94_env("TELEMETRY", True)

# ---------------------------------------------------------------------------------------------
# V9.4.1 — từ log live đầu tiên trên Windows (Shorts M1sbvQq1DAk: HOT_READY 2.394s, MUX 0.602s
# trong khi ffmpeg concat + ffprobe của bước <60s chỉ 0.11s ⇒ lần spawn ffmpeg.exe ĐẦU TIÊN sau
# thời gian rảnh là "nguội"). Mọi cờ dưới đây: đặt về giá trị trong ngoặc = hành vi V9.4 trước đó.
# ---------------------------------------------------------------------------------------------
# Làm nóng ffmpeg.exe/ffprobe.exe bằng 1 lần remux THẬT (mẫu 12KB nhúng sẵn, không chạm file job)
# chạy nền song song lúc video đang tải; mux thật không bao giờ chờ nó. (0 = tắt)
V94_FF_PREWARM = _v94_env("FF_PREWARM", True)
V94_FF_PREWARM_IDLE_SEC = _v94_env("FF_PREWARM_IDLE_SEC", 3.0)    # chỉ làm nóng nếu ffmpeg rảnh >= N giây
V94_FF_WARMUP_START = _v94_env("FF_WARMUP_START", True)           # làm nóng 2 lần lúc bấm START + dòng FF BASELINE
V94_FF_KEEPWARM_SEC = _v94_env("FF_KEEPWARM_SEC", 0.0)            # >0: giữ nóng định kỳ khi rảnh (mặc định tắt)
V94_PREWARM_INNERTUBE = _v94_env("PREWARM_INNERTUBE", True)       # import bảng client Innertube lúc START
# Đo tách pha lệnh mux (pre-read input, spawn, mở input, copy, thoát, thời gian kernel trên Windows).
# 0 = subprocess.run như cũ; 1 = đo; 2 = đo + -benchmark. Lệnh/đầu ra mux KHÔNG đổi.
V94_MUX_DIAG = _v94_env("MUX_DIAG", 1)
V94_MUX_PREREAD = _v94_env("MUX_PREREAD", True)                   # đọc trước 64KiB mỗi input (quy thời gian quét AV)
V94_GIL_PROBE = _v94_env("GIL_PROBE", False)                      # chỉ để chẩn đoán: đo GIL/GC stall trong lúc mux
# Xác thực chặt file OUTPUT của mux (time= cuối, video:/audio: kB, không có dòng lỗi) trước DONE.
V94_VALIDATE_MUX_STRICT = _v94_env("VALIDATE_MUX_STRICT", True)
V94_FFPROBE_TIMEOUT = _v94_env("FFPROBE_TIMEOUT", 20.0)           # ffprobe thời lượng có timeout (0 = như V9.3)
# Khi người dùng tự đặt trần V94_FIRST_WAVE_RACE_MAX>0: file nhỏ (<= N segment đợt đầu) vẫn race TẤT CẢ như V9.3.
V94_RACE_ALL_IF_WORKERS_LE = _v94_env("RACE_ALL_IF_WORKERS_LE", 8)
# Chỉ cấm replica mn 10 phút khi lỗi CHẮC CHẮN (403/404/410/429/401, từ chối kết nối, DNS) và job còn chạy;
# timeout muộn của request thua cuộc không được tắt race cho các job sau. (0 = luật cũ)
V94_ALT_BAN_STRICT = _v94_env("ALT_BAN_STRICT", True)
# Số worker cho video <= 4MiB (0 = heuristic cũ = 6). A/B trong app: "6,11" xen kẽ theo job, ghi vào log/telemetry.
V94_VIDEO_SMALL_WORKERS = _v94_env("VIDEO_SMALL_WORKERS", 0)
V94_VIDEO_SMALL_WORKERS_AB = _v94_env("VIDEO_SMALL_WORKERS_AB", "")
# check.xlsx: ghi bằng os.replace (nguyên tử, có retry) thay vì shutil.move = copy đè trên Windows. (0 = shutil.move)
V94_EXCEL_ATOMIC_REPLACE = _v94_env("EXCEL_ATOMIC_REPLACE", True)
# Chỉ ghi DONE vào dòng vẫn chứa đúng video id của job (dòng bị chèn/xoá/sắp xếp → tìm lại / không ghi). (0 = theo vị trí)
V94_STATUS_VERIFY_URL = _v94_env("STATUS_VERIFY_URL", True)
# Suy luận [SINGLE-TRACK-JA] chặt hơn: audioTrack.id/displayName, track trùng, bỏ microformat.title. (0 = V9.4)
V94_JA_INFER_STRICT = _v94_env("JA_INFER_STRICT", True)

# ---------------------------------------------------------------------------------------------
# V9.5 — lựa chọn "360p nhanh" (itag 18: hình + tiếng trong 1 file, KHÔNG ghép ffmpeg).
# V95_360P=0 → radio 360p bị ẩn/vô hiệu, mọi job đi luồng 480p y như V9.4. Luồng 480p KHÔNG thay đổi.
# ---------------------------------------------------------------------------------------------
V95_360P = (str(os.environ.get("V95_360P", "1")).strip().lower() in ("1", "true", "yes", "on"))
# Khi tiếng mặc định của itag 18 KHÔNG phải tiếng Nhật nhưng video có track tiếng Nhật → tự chuyển job đó sang 480p+JA.
V95_360P_JA_FALLBACK = (str(os.environ.get("V95_360P_JA_FALLBACK", "1")).strip().lower() in ("1", "true", "yes", "on"))
# Tuỳ chọn A/B (mặc định TẮT theo yêu cầu "1 GET"): tải itag 18 bằng engine Range nhiều kết nối của 480p.
# Lý do có công tắc: log live 17:09 cho thấy GVS ~5Mbps/kết nối → 1 GET cho 2-3MB có thể mất 4-5s, trong khi
# Range 6 kết nối mất ~1.5s. Chỉ bật khi đo live thấy 1 GET chậm hơn.
V95_360P_RANGE = (str(os.environ.get("V95_360P_RANGE", "0")).strip().lower() in ("1", "true", "yes", "on"))
# ---------------------------------------------------------------------------------------------
# V9.6 — giảm thời gian tải 360p. Log live: resolve 0.14s, TTFB 0.69-0.75s (có 302 đổi host), truyền 5-9MB 0.5-0.6s.
# Mọi cờ V96_*: tắt = hành vi V9.5. Chỉ giữ cấu hình nào ĐO ĐƯỢC nhanh hơn (python bench_v93.py live360).
# ---------------------------------------------------------------------------------------------
def _v96_env(name, default):
    raw = os.environ.get(f"V96_{name}")
    if raw is None or str(raw).strip() == "":
        return default
    raw = str(raw).strip()
    if isinstance(default, bool):
        return raw.lower() in ("1", "true", "yes", "on")
    try:
        return type(default)(raw)
    except Exception:
        return default


# 1) Nhớ host đích sau 302 (hostA → hostB, TTL 10 phút) và GET thẳng hostB lần sau (bỏ 1 RTT 302);
#    lỗi/không 200-206 → quay về URL gốc. Giữ kết nối nóng tới hostB sau khi tải xong (HEAD /generate_204 mỗi 25s, tối đa 10 phút).
V96_HOST_POOL = _v96_env("HOST_POOL", True)
V96_HOST_POOL_TTL_SEC = _v96_env("HOST_POOL_TTL_SEC", 600.0)
V96_HOST_KEEPALIVE_SEC = _v96_env("HOST_KEEPALIVE_SEC", 25.0)
# 2) Chia Range N đoạn song song khi biết content_length (0 = 1-GET như V9.5; thử 2/4/8). Lỗi 403/410/429 ở
#    bất kỳ đoạn nào → dừng chia, quay về 1-GET cùng URL (không retry liên tục, không đổi IP/proxy).
V96_RANGE_N = _v96_env("RANGE_N", 0)
# 3) HTTP/2 qua httpx[http2] (nếu cài): 1 kết nối cho nhiều Range. 0 = urllib3 HTTP/1.1.
V96_HTTP2 = _v96_env("HTTP2", False)
# ffprobe kiểm file 360p chạy nền SAU khi đã DONE (chỉ cảnh báo). 0 = không kiểm.
V95_360P_BG_PROBE = (str(os.environ.get("V95_360P_BG_PROBE", "1")).strip().lower() in ("1", "true", "yes", "on"))

# V9.4.1: A/B số worker video nhỏ theo job ("6,11" → job lẻ 6, job chẵn 11) + mốc phát hiện gần nhất.
_V94_AB_LOCK = threading.Lock()
_V94_AB_COUNTER = [0]
_V94_LAST_WAKE = {"lag_ms": None}


def _v94_ab_arms():
    try:
        arms = [int(x) for x in str(V94_VIDEO_SMALL_WORKERS_AB or "").replace(";", ",").split(",") if x.strip()]
        arms = [a for a in arms if a > 0]
        return arms if len(arms) >= 2 else []
    except Exception:
        return []


def _v94_ab_next() -> int:
    with _V94_AB_LOCK:
        n = _V94_AB_COUNTER[0]
        _V94_AB_COUNTER[0] += 1
        return n


# Trạng thái job đang tải (keepalive nhường đường).
_V94_ACTIVE_JOBS = [0]
_V94_ACTIVE_JOBS_LOCK = threading.Lock()


def _v94_job_enter():
    with _V94_ACTIVE_JOBS_LOCK:
        _V94_ACTIVE_JOBS[0] += 1


def _v94_job_exit():
    with _V94_ACTIVE_JOBS_LOCK:
        _V94_ACTIVE_JOBS[0] = max(0, _V94_ACTIVE_JOBS[0] - 1)


def _v94_jobs_active() -> int:
    with _V94_ACTIVE_JOBS_LOCK:
        return _V94_ACTIVE_JOBS[0]


def v94_redact(value) -> str:
    """Bỏ mọi query/token khỏi URL media trước khi log. Chỉ giữ host + path ngắn."""
    try:
        s = str(value or "")
        if "googlevideo.com" in s or "videoplayback" in s:
            u = urllib.parse.urlsplit(s)
            host = (u.hostname or "?")
            host = re.sub(r"^(rr\d+)---sn-[a-z0-9-]+", r"\1---sn-***", host)
            return f"{u.scheme}://{host}{(u.path or '')[:24]}?<redacted>"
        u = urllib.parse.urlsplit(s)
        if not u.query:
            return s
        host = (u.hostname or "").lower()
        if host.endswith("youtube.com") or host.endswith("youtu.be"):
            # URL watch/shorts công khai: chỉ giữ v=, bỏ mọi tham số khác (si=, pp=, token...).
            q = urllib.parse.parse_qs(u.query)
            keep = {"v": q["v"][:1]} if "v" in q else {}
            return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, urllib.parse.urlencode(keep, doseq=True), ""))
        return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, "<redacted>", ""))
    except Exception:
        return "<redacted>"


class V94Telemetry:
    """Span logger rẻ cho hot path.

    span() chỉ append tuple vào deque (lock-free với GIL) — không I/O, không format chuỗi.
    Thread nền flush ra JSONL mỗi 0.5s. Mọi URL/token phải đi qua v94_redact() trước khi
    đưa vào fields; key chứa 'url' sẽ tự động redact.
    """

    def __init__(self, out_dir=None, enabled=True):
        import collections
        self.enabled = bool(enabled)
        self._q = collections.deque()
        self._out_dir = out_dir
        self._path = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.t0_ns = time.perf_counter_ns()
        self.wall0 = time.time()

    def set_out_dir(self, out_dir):
        self._out_dir = out_dir

    def span(self, job_id, event, **fields):
        if not self.enabled:
            return
        self._q.append((time.perf_counter_ns(), threading.get_ident(), str(job_id or "-"), event, fields))
        if self._thread is None:
            self._start()

    def _start(self):
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._loop, name="V94Telemetry", daemon=True)
            self._thread.start()

    def _resolve_path(self):
        if self._path:
            return self._path
        base = self._out_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "perf_report")
        try:
            os.makedirs(base, exist_ok=True)
        except OSError:
            return None
        self._path = os.path.join(base, time.strftime("telemetry_%Y%m%d_%H%M%S.jsonl"))
        return self._path

    def drain(self):
        """Lấy toàn bộ event (dùng cho test/bench). Trả list dict."""
        rows = []
        while True:
            try:
                ns, tid, job, ev, f = self._q.popleft()
            except IndexError:
                break
            rows.append(self._row(ns, tid, job, ev, f))
        return rows

    def _row(self, ns, tid, job, ev, f):
        row = {"t_ns": ns, "t_ms": round((ns - self.t0_ns) / 1e6, 3), "tid": tid, "job": job, "ev": ev}
        for k, v in (f or {}).items():
            if "url" in k.lower() and v:
                v = v94_redact(v)
            row[k] = v
        return row

    def flush(self):
        rows = self.drain()
        if not rows:
            return 0
        path = self._resolve_path()
        if not path:
            return 0
        try:
            with open(path, "a", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        except OSError:
            return 0
        return len(rows)

    def _loop(self):
        while not self._stop.wait(0.5):
            self.flush()
        self.flush()

    def close(self):
        self._stop.set()
        try:
            self.flush()
        except Exception:
            pass


TELEM = V94Telemetry(out_dir=(os.environ.get("V94_TELEMETRY_DIR") or None), enabled=V94_TELEMETRY)
atexit.register(TELEM.close)


def _extract_youtube_video_id(url: str) -> str:
    """Lấy video ID nhanh, không gọi mạng."""
    try:
        text = (url or "").strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", text):
            return text
        u = urllib.parse.urlparse(text)
        host = (u.netloc or "").lower().split(":", 1)[0]
        path = (u.path or "").strip("/")
        if host in ("youtu.be", "www.youtu.be"):
            cand = path.split("/", 1)[0]
            return cand if re.fullmatch(r"[A-Za-z0-9_-]{11}", cand or "") else ""
        if "youtube.com" in host:
            q = urllib.parse.parse_qs(u.query or "")
            cand = (q.get("v") or [""])[0]
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", cand or ""):
                return cand
            parts = path.split("/")
            if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v"):
                cand = parts[1]
                if re.fullmatch(r"[A-Za-z0-9_-]{11}", cand or ""):
                    return cand
        m = re.search(r"(?<![A-Za-z0-9_-])([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])", text)
        return m.group(1) if m else ""
    except Exception:
        return ""


def _get_innertube_client(name: str) -> dict:
    """Đọc client context trực tiếp từ yt-dlp đang cài để luôn bám version hiện tại."""
    cfg = None
    if yt_dlp:
        try:
            from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS
            cfg = copy.deepcopy(INNERTUBE_CLIENTS.get(name))
        except Exception:
            try:
                from yt_dlp.extractor.youtube import YoutubeBaseInfoExtractor
                cfg = copy.deepcopy(getattr(YoutubeBaseInfoExtractor, "_INNERTUBE_CLIENTS", {}).get(name))
            except Exception:
                cfg = None
    return cfg or {}


def _get_android_innertube_client() -> dict:
    cfg = _get_innertube_client("android")
    if cfg:
        return cfg
    return {
        "INNERTUBE_CONTEXT": {
            "client": {
                "clientName": "ANDROID",
                "clientVersion": "21.26.364",
                "androidSdkVersion": 30,
                "userAgent": "com.google.android.youtube/21.26.364 (Linux; U; Android 11) gzip",
                "osName": "Android",
                "osVersion": "11",
            }
        },
        "INNERTUBE_CONTEXT_CLIENT_NAME": 3,
        "INNERTUBE_HOST": "www.youtube.com",
    }


_SHARED_SSL_CTX = None
_SHARED_SSL_CTX_LOCK = threading.Lock()


def _shared_ssl_context():
    """V9.1 FIX LỚN NHẤT: urllib3 mặc định tạo SSLContext MỚI + load_default_certs() cho TỪNG
    kết nối. Trên Windows việc đó duyệt cả kho chứng chỉ bằng vòng lặp Python (giữ GIL)
    ≈30-100ms/kết nối → 42 kết nối = 1-4s CPU, cả tiến trình đứng (log 19:57:16→18 im lặng,
    'ttfb' 0.5-1.1s ở mọi log thực chất là CPU nạp chứng chỉ). Dùng 1 context nạp sẵn 1 lần."""
    global _SHARED_SSL_CTX
    if urllib3 is None:
        return None
    if _SHARED_SSL_CTX is None:
        with _SHARED_SSL_CTX_LOCK:
            if _SHARED_SSL_CTX is None:
                try:
                    from urllib3.util.ssl_ import create_urllib3_context
                    ctx = create_urllib3_context()
                    ctx.load_default_certs()
                    _SHARED_SSL_CTX = ctx
                except Exception:
                    _SHARED_SSL_CTX = False
    return _SHARED_SSL_CTX or None


def _get_fast_pool():
    global _FAST_YT_POOL
    if urllib3 is None:
        return None
    if _FAST_YT_POOL is None:
        with _FAST_YT_POOL_LOCK:
            if _FAST_YT_POOL is None:
                _kw = {"ssl_context": _shared_ssl_context()} if _shared_ssl_context() else {}
                _FAST_YT_POOL = urllib3.PoolManager(
                    num_pools=2, maxsize=12, block=False,
                    timeout=urllib3.Timeout(connect=0.45, read=0.75),
                    retries=False, **_kw
                )
    return _FAST_YT_POOL


def _stdlib_youtube_post(host: str, path: str, body: bytes, headers: dict, timeout: float = 0.95):
    """Fallback stdlib, giữ HTTPS connection theo thread."""
    key = f"conn_{host.replace('.', '_')}"
    for attempt in range(2):
        conn = getattr(_FAST_HTTP_TLS, key, None)
        try:
            if conn is None:
                conn = http.client.HTTPSConnection(host, timeout=timeout)
                setattr(_FAST_HTTP_TLS, key, conn)
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status != 200:
                return None
            return json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
            setattr(_FAST_HTTP_TLS, key, None)
            if attempt:
                return None
    return None


def _fast_player_request(video_id: str, client_key: str):
    t_req = time.perf_counter()
    cfg = _get_innertube_client(client_key)
    if not cfg and client_key == "android":
        cfg = _get_android_innertube_client()
    if not cfg:
        return None

    ctx = copy.deepcopy((cfg or {}).get("INNERTUBE_CONTEXT") or {})
    client = ctx.setdefault("client", {})
    client.setdefault("hl", "en")
    client.setdefault("timeZone", "UTC")
    client.setdefault("utcOffsetMinutes", 0)
    client_name_num = str((cfg or {}).get("INNERTUBE_CONTEXT_CLIENT_NAME") or 3)
    client_version = str(client.get("clientVersion") or "21.26.364")
    ua = str(client.get("userAgent") or "com.google.android.youtube/21.26.364 (Linux; U; Android 11) gzip")
    host = str((cfg or {}).get("INNERTUBE_HOST") or "www.youtube.com")

    payload = {
        "context": ctx,
        "videoId": video_id,
        "contentCheckOk": True,
        "racyCheckOk": True,
        "playbackContext": {"contentPlaybackContext": {"html5Preference": "HTML5_PREF_WANTS"}},
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": ua,
        "X-YouTube-Client-Name": client_name_num,
        "X-YouTube-Client-Version": client_version,
        "Origin": "https://www.youtube.com",
        "Accept-Encoding": "gzip",
        "Connection": "keep-alive",
    }

    try:
        pool = _get_fast_pool()
        if pool is not None:
            resp = pool.request(
                "POST", f"https://{host}/youtubei/v1/player?prettyPrint=false",
                body=body, headers=headers, preload_content=True, decode_content=True,
                timeout=urllib3.Timeout(connect=0.40, read=0.70), retries=False,
            )
            if int(getattr(resp, "status", 0) or 0) != 200:
                return None
            data = json.loads(resp.data.decode("utf-8", errors="replace"))
        else:
            data = _stdlib_youtube_post(host, "/youtubei/v1/player?prettyPrint=false", body, headers, timeout=0.90)
            if not data:
                return None

        status = ((data.get("playabilityStatus") or {}).get("status") or "").upper()
        if status and status != "OK":
            return None
        streaming = data.get("streamingData") or {}
        formats = list(streaming.get("formats") or []) + list(streaming.get("adaptiveFormats") or [])
        f18 = next((f for f in formats if str(f.get("itag")) == "18" and f.get("url")), None)
        if not f18:
            return None
        details = data.get("videoDetails") or {}
        try:
            content_length = int(f18.get("contentLength") or 0)
        except Exception:
            content_length = 0
        out = {
            "url": f18["url"],
            "title": details.get("title") or "video",
            "id": details.get("videoId") or video_id,
            "format_id": "18",
            "content_length": content_length,
            "length_exact": content_length > 0,
            "source": f"FAST_RACE_{client_key.upper()}",
            "resolver_ms": (time.perf_counter() - t_req) * 1000.0,
        }
        out.update(_v95_audio_lang_meta(data, f18))
        return out
    except Exception:
        return None


def _v95_audio_lang_meta(data: dict, f18: dict = None) -> dict:
    """V9.5: itag 18 chỉ chứa track tiếng MẶC ĐỊNH. Đọc defaultAudioLanguage + danh sách track audio của player
    response để quyết định có cần chuyển sang 480p + audio Nhật không."""
    meta = {"default_audio_lang": "", "has_ja_audio": False, "audio_tracks": 0, "duration_sec": 0.0}
    try:
        micro = ((data.get("microformat") or {}).get("playerMicroformatRenderer") or {})
        meta["default_audio_lang"] = str(micro.get("defaultAudioLanguage") or "").lower()
        details = data.get("videoDetails") or {}
        try:
            meta["duration_sec"] = float(details.get("lengthSeconds") or 0.0)
        except Exception:
            pass
        if (f18 or {}).get("approxDurationMs"):
            try:
                meta["duration_sec"] = int(f18["approxDurationMs"]) / 1000.0 or meta["duration_sec"]
            except Exception:
                pass
        streaming = data.get("streamingData") or {}
        ids = set()
        for f in list(streaming.get("adaptiveFormats") or []):
            if not str(f.get("mimeType") or "").lower().startswith("audio/"):
                continue
            tr = f.get("audioTrack") or {}
            tid = str(tr.get("id") or "").strip()
            ids.add(tid or "_default")
            lang = " ".join(str(x or "") for x in (tid.split(".", 1)[0] if tid else "", tr.get("displayName"),
                                                   f.get("language"))).lower()
            if _looks_japanese_lang(lang.strip()):
                meta["has_ja_audio"] = True
            if tid and tr.get("audioIsDefault") and not meta["default_audio_lang"]:
                meta["default_audio_lang"] = tid.split(".", 1)[0].lower()
        meta["audio_tracks"] = len(ids)
        if not meta["has_ja_audio"] and _looks_japanese_lang(meta["default_audio_lang"]):
            meta["has_ja_audio"] = True
    except Exception:
        pass
    return meta


def _fast_innertube_360p(url: str):
    """
    Turbo race: Android VR + Web Embedded + Android chạy song song trên connection pool nóng.
    Client nào trả progressive itag 18 hợp lệ trước thì thắng. Android VR/Web Embedded
    hiện hữu ích vì có các trường hợp không cần GVS PO token; Android giữ làm fallback.
    """
    video_id = _extract_youtube_video_id(url)
    if not video_id:
        return None

    candidates = []
    for key in ("android_vr", "web_embedded", "android"):
        if _get_innertube_client(key) or key == "android":
            candidates.append(key)
    if not candidates:
        candidates = ["android"]

    futures = []
    try:
        for key in candidates[:3]:
            futures.append(_FAST_PLAYER_EXECUTOR.submit(_fast_player_request, video_id, key))
        for fut in as_completed(futures, timeout=1.0):
            try:
                data = fut.result()
            except Exception:
                data = None
            if data and data.get("url"):
                for other in futures:
                    if other is not fut:
                        other.cancel()
                return data
    except Exception:
        pass

    # Cuối cùng thử Android đồng bộ để không làm giảm độ ổn định.
    return _fast_player_request(video_id, "android")




def _native_audio_lang_text(fmt: dict) -> str:
    """Collect language hints from a raw Innertube audio format."""
    try:
        tr = fmt.get("audioTrack") or {}
        vals = [
            tr.get("id"), tr.get("languageCode"), tr.get("displayName"),
            fmt.get("language"), fmt.get("qualityLabel"),
        ]
        return " ".join(str(x or "") for x in vals).strip().lower()
    except Exception:
        return ""


def _looks_japanese_lang(value: str) -> bool:
    s = str(value or "").strip().lower()
    return (
        s.startswith("ja") or s.startswith("jpn") or
        " japanese" in f" {s}" or "日本語" in s or "日本" in s
    )


def _has_japanese_kana(value: str) -> bool:
    # Hiragana/Katakana (plus prolonged sound mark) is a strong Japanese signal and
    # avoids treating generic Han-only/Chinese titles as Japanese.
    return bool(re.search(r"[ぁ-ゖァ-ヺー]", str(value or "")))


def _pick_native_ja_audio(formats: list, data: dict):
    """Strict JA first; V8.6 can infer a single original track on clearly Japanese videos.

    Fresh Shorts sometimes expose 139/140 immediately but omit audioTrack.languageCode for
    the first ~1 s. If there is exactly one logical audio track, no explicit non-JA language
    label, and the title/channel contains Japanese kana, treating that sole track as the
    original JA audio avoids paying a full yt-dlp metadata round-trip.
    """
    audio_rows = []
    audios = []
    default_lang = ""
    try:
        default_lang = str((((data.get("microformat") or {}).get("playerMicroformatRenderer") or {})
                            .get("defaultAudioLanguage") or "")).lower()
    except Exception:
        default_lang = ""

    def _rank_audio(f):
        try: abr = float(f.get("bitrate") or 0) / 1000.0
        except Exception: abr = 0.0
        itag = str(f.get("itag") or "")
        mime = str(f.get("mimeType") or "").lower()
        if itag == "139": pref = 0
        elif itag == "140": pref = 1
        elif "audio/mp4" in mime: pref = 2
        else: pref = 5
        return (pref, abr if abr > 0 else 999999.0)

    for f in formats or []:
        if not isinstance(f, dict) or not f.get("url"):
            continue
        mime = str(f.get("mimeType") or "").lower()
        if not mime.startswith("audio/"):
            continue
        audio_rows.append(f)
        langtxt = _native_audio_lang_text(f)
        if _looks_japanese_lang(langtxt):
            audios.append(f)

    # Strong explicit/default JA path.
    if not audios and _looks_japanese_lang(default_lang):
        audios = list(audio_rows)

    # V8.6 fresh-upload fast path: accept the sole original audio track only when there is
    # no explicit conflicting language and the metadata is unmistakably Japanese (kana).
    if not audios and audio_rows:
        explicit_codes = []
        track_ids = set()
        explicit_non_ja = False
        for f in audio_rows:
            tr = f.get("audioTrack") or {}
            tid = str(tr.get("id") or "").strip()
            if tid:
                track_ids.add(tid)
            for raw in (tr.get("languageCode"), f.get("language")):
                code = str(raw or "").strip().lower()
                if code:
                    explicit_codes.append(code)
                    if not _looks_japanese_lang(code):
                        explicit_non_ja = True
        details = data.get("videoDetails") or {}
        micro = ((data.get("microformat") or {}).get("playerMicroformatRenderer") or {})
        jp_text = " ".join([
            str(details.get("title") or ""),
            str(details.get("author") or ""),
            str(micro.get("title") or ""),
            str(micro.get("ownerChannelName") or ""),
        ])
        single_logical_track = len(track_ids) <= 1
        refuse = ""
        # V9.4 guard: player khai báo rõ defaultAudioLanguage KHÁC tiếng Nhật → không suy luận.
        if V94_JA_INFER_GUARD and default_lang and not _looks_japanese_lang(default_lang):
            explicit_non_ja = True
            refuse = f"default_lang:{default_lang}"
        kana_source = ""
        asr_langs = []
        if V94_JA_INFER_STRICT:
            # V9.4.1 (đã kiểm chứng bằng probe): Innertube thường chỉ ghi ngôn ngữ trong audioTrack.id ("en.4")
            # và displayName ("English original" / "英語（オリジナル）"), không có languageCode.
            for f in audio_rows:
                tr = f.get("audioTrack") or {}
                tid = str(tr.get("id") or "").strip()
                pref = tid.split(".", 1)[0].strip().lower() if tid else ""
                if pref and not _looks_japanese_lang(pref):
                    explicit_non_ja = True
                    refuse = refuse or f"track_id:{pref}"
                dn = str(tr.get("displayName") or "").strip()
                if dn and not _looks_japanese_lang(dn.lower()):
                    explicit_non_ja = True
                    refuse = refuse or f"track_name:{dn[:24]}"
            # Cùng itag (cùng isDrc) xuất hiện 2 lần = có >= 2 track logic dù thiếu metadata → không đoán.
            keys = [(str(f.get("itag") or ""), bool(f.get("isDrc"))) for f in audio_rows]
            if len(keys) != len(set(keys)):
                single_logical_track = False
                refuse = refuse or "duplicate_tracks"
            # microformat.title có thể là dict {"simpleText": tiêu đề DỊCH theo hl=ja} → str() làm lọt kana.
            for src_name, txt in (("title", details.get("title")), ("author", details.get("author")),
                                  ("owner", micro.get("ownerChannelName"))):
                if _has_japanese_kana(str(txt or "")):
                    kana_source = src_name
                    break
            jp_text = " ".join(str(x or "") for x in (details.get("title"), details.get("author"),
                                                       micro.get("ownerChannelName")))
            try:
                for tr_ in (((data.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {})
                            .get("captionTracks") or []):
                    if str(tr_.get("kind") or "") == "asr":
                        asr_langs.append(str(tr_.get("languageCode") or "?"))
            except Exception:
                pass
        if not single_logical_track:
            refuse = refuse or "multi_track"
        if single_logical_track and not explicit_non_ja and _has_japanese_kana(jp_text):
            audios = list(audio_rows)
            for f in audios:
                f["_v85_inferred_single_ja"] = True
                f["_v94_infer"] = {"kana_source": kana_source or "legacy", "asr_lang": ",".join(asr_langs) or "-",
                                   "track_keys": len(audio_rows)}
        elif not refuse:
            refuse = "no_kana"
        if not audios and refuse:
            try:
                data["_v94_ja_refuse"] = refuse
            except Exception:
                pass

    if not audios:
        return None
    audios.sort(key=_rank_audio)
    return audios[0]


def _get_480_visitor_data():
    with _FAST_480_VISITOR_LOCK:
        return _FAST_480_VISITOR_DATA


def _set_480_visitor_data(value):
    global _FAST_480_VISITOR_DATA
    value = str(value or "").strip()
    if len(value) < 8:
        return ""
    with _FAST_480_VISITOR_LOCK:
        _FAST_480_VISITOR_DATA = value
    return value


def _capture_480_visitor_data(player_json):
    """Capture public Innertube visitorData so the next player POST skips session discovery."""
    try:
        rc = (player_json or {}).get("responseContext") or {}
        value = rc.get("visitorData") or ((rc.get("mainAppWebResponseContext") or {}).get("visitorData"))
        return _set_480_visitor_data(value)
    except Exception:
        return ""


def _prewarm_480_visitor_data(force=False):
    """Preload anonymous public YouTube visitorData before the first fresh-video race.

    V7.4 often won as NATIVE_*_VD, which means the first Player POST spent one extra
    round-trip learning a visitor identity. Pulling the public homepage while the monitor
    is idle lets the first real Player POST carry X-Goog-Visitor-Id immediately.
    """
    global _FAST_480_VISITOR_LAST_REFRESH
    if urllib3 is None:
        return False
    now = time.monotonic()
    with _FAST_480_VISITOR_REFRESH_LOCK:
        if (not force and _get_480_visitor_data() and
                now - float(_FAST_480_VISITOR_LAST_REFRESH or 0.0) < FAST_480_VISITOR_REFRESH_SEC):
            return True
        pool = _get_fast_pool()
        if pool is None:
            return False
        try:
            resp = pool.request(
                "GET", "https://www.youtube.com/?hl=ja",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154.0.0.0 Safari/537.36",
                    "Accept": "text/html,*/*;q=0.8",
                    "Accept-Encoding": "identity",
                    "Connection": "keep-alive",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
                preload_content=True, decode_content=True, redirect=True,
                timeout=urllib3.Timeout(connect=0.55, read=0.90), retries=False,
            )
            if int(getattr(resp, "status", 0) or 0) != 200:
                return False
            text = resp.data.decode("utf-8", errors="ignore")
            raw = ""
            for pat in (
                r'"VISITOR_DATA":"([^"\\]+(?:\\.[^"\\]*)*)"',
                r'"visitorData":"([^"\\]+(?:\\.[^"\\]*)*)"',
                r'VISITOR_DATA\\"\s*:\s*\\"([^\\"]+)',
            ):
                m = re.search(pat, text)
                if m:
                    raw = m.group(1)
                    break
            if not raw:
                return False
            value = (raw.replace(r"\u003d", "=")
                        .replace(r"\u0026", "&")
                        .replace(r"\/", "/"))
            value = urllib.parse.unquote(value)
            if not _set_480_visitor_data(value):
                return False
            _FAST_480_VISITOR_LAST_REFRESH = time.monotonic()
            return True
        except Exception:
            return False


def _fast_player_request_480(video_id: str, client_key: str):
    """V7.8 low-latency Player API resolver for HARD 480p.

    Priority is exact itag 244 (VP9/WebM) for better 480p visual quality, then exact itag 135 (H.264/MP4) with ZERO extra wait.
    If 244 is present in the same snapshot it wins immediately; if only 135 is directly usable,
    135 is returned immediately so Shorts never wait for an unavailable 244.
    Japanese audio remains independently useful and can start in parallel.
    """
    t_req = time.perf_counter()
    cfg = _get_innertube_client(client_key)
    if not cfg and client_key == "android":
        cfg = _get_android_innertube_client()
    if not cfg:
        return None

    def _request_once(visitor_data=""):
        ctx = copy.deepcopy((cfg or {}).get("INNERTUBE_CONTEXT") or {})
        client = ctx.setdefault("client", {})
        client["hl"] = "ja"
        client.setdefault("timeZone", "UTC")
        client.setdefault("utcOffsetMinutes", 0)
        if visitor_data:
            client["visitorData"] = visitor_data
        client_name_num = str((cfg or {}).get("INNERTUBE_CONTEXT_CLIENT_NAME") or 3)
        client_version = str(client.get("clientVersion") or "")
        ua = str(client.get("userAgent") or "Mozilla/5.0")
        host = str((cfg or {}).get("INNERTUBE_HOST") or "www.youtube.com")
        payload = {
            "context": ctx,
            "videoId": video_id,
            "contentCheckOk": True,
            "racyCheckOk": True,
            "playbackContext": {"contentPlaybackContext": {"html5Preference": "HTML5_PREF_WANTS"}},
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": ua,
            "X-YouTube-Client-Name": client_name_num,
            "X-YouTube-Client-Version": client_version,
            "Origin": "https://www.youtube.com",
            "Accept-Encoding": "gzip",
            "Connection": "keep-alive",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        if visitor_data:
            headers["X-Goog-Visitor-Id"] = visitor_data
        TELEM.span(video_id, "player_post_begin", client=client_key, vd=bool(visitor_data))
        _st = 0
        try:
            pool = _get_fast_pool()
            if pool is not None:
                resp = pool.request(
                    "POST", f"https://{host}/youtubei/v1/player?prettyPrint=false",
                    body=body, headers=headers, preload_content=True, decode_content=True,
                    timeout=urllib3.Timeout(connect=0.35, read=0.68), retries=False,
                )
                _st = int(getattr(resp, "status", 0) or 0)
                if _st != 200:
                    return None
                return json.loads(resp.data.decode("utf-8", errors="replace"))
            _st = -2
            return _stdlib_youtube_post(host, "/youtubei/v1/player?prettyPrint=false", body, headers, timeout=0.85)
        except Exception as _e:
            _st = f"EXC:{type(_e).__name__}"
            return None
        finally:
            TELEM.span(video_id, "player_post_end", client=client_key, status=_st)

    def _parse(data, suffix=""):
        if not data:
            return None
        status = str(((data.get("playabilityStatus") or {}).get("status") or "")).upper()
        if status and status != "OK":
            return None
        streaming = data.get("streamingData") or {}
        formats = list(streaming.get("formats") or []) + list(streaming.get("adaptiveFormats") or [])
        # HARD 480p only: 244 is primary for quality; 135 is immediate fallback.
        # No 360p/AV1 substitution, and critically: NO added wait for 244.
        by_itag = {}
        for f in formats:
            fid = str((f or {}).get("itag") or "")
            if fid in HARD_480_VIDEO_ITAGS and fid not in by_itag:
                by_itag[fid] = f
        v_any = next((by_itag.get(fid) for fid in HARD_480_VIDEO_ITAGS if by_itag.get(fid)), None)
        v = None
        selected_video_itag = ""
        for fid in HARD_480_VIDEO_ITAGS:
            cand = by_itag.get(fid)
            if isinstance(cand, dict) and cand.get("url"):
                v = cand
                selected_video_itag = fid
                break
        if V94_FASTEST_VALID_480P and v is not None:
            # Opt-in (mặc định TẮT): cả 244 và 135 đều direct trong cùng snapshot → chọn file nhỏ hơn.
            _c244, _c135 = by_itag.get("244"), by_itag.get("135")
            if (isinstance(_c244, dict) and _c244.get("url") and isinstance(_c135, dict) and _c135.get("url")):
                try:
                    if 0 < int(_c135.get("contentLength") or 0) < int(_c244.get("contentLength") or 0):
                        v, selected_video_itag = _c135, "135"
                except Exception:
                    pass
        ja = _pick_native_ja_audio(formats, data)
        details = data.get("videoDetails") or {}
        def _clen(f):
            try: return int((f or {}).get("contentLength") or 0)
            except Exception: return 0
        if not v and not ja:
            return None
        return {
            "id": details.get("videoId") or video_id,
            "title": details.get("title") or "video",
            "video_url": (v or {}).get("url") if v else None,
            "video_format_id": selected_video_itag,
            "video_content_length": _clen(v),
            "audio_url": (ja or {}).get("url") if ja else None,
            "audio_format_id": str((ja or {}).get("itag") or "") if ja else "",
            "audio_content_length": _clen(ja),
            "audio_inferred_single_ja": bool((ja or {}).get("_v85_inferred_single_ja")) if ja else False,
            "audio_mime": str((ja or {}).get("mimeType") or "") if ja else "",
            "video_mime": str((v or {}).get("mimeType") or "") if v else "",
            "audio_infer": (ja or {}).get("_v94_infer") if ja else None,
            "audio_refuse_reason": "" if ja else str(data.get("_v94_ja_refuse") or ""),
            "duration_sec": _native_duration_sec(v, details),
            "source": f"NATIVE_{client_key.upper()}{suffix}",
            "resolver_ms": (time.perf_counter() - t_req) * 1000.0,
            "native_480_present": bool(v_any),
            "native_480_direct": bool(v),
            "native_480_cipher": bool(isinstance(v_any, dict) and (v_any.get("signatureCipher") or v_any.get("cipher"))),
        }

    def _native_duration_sec(fmt, det):
        try:
            ms = int((fmt or {}).get("approxDurationMs") or 0)
            if ms > 0:
                return ms / 1000.0
            return float((det or {}).get("lengthSeconds") or 0)
        except Exception:
            return 0.0

    visitor_before = _get_480_visitor_data()
    data = _request_once(visitor_before)
    visitor_after = _capture_480_visitor_data(data)
    parsed = _parse(data)
    # V7.7 safety gate: current YouTube enforcement can return adaptive direct URLs
    # from android/android_vr that resolve instantly but fail at GVS with HTTP 403.
    # Never let those raw URLs win the HARD-480 race. visionos/web_embedded remain the
    # low-latency native winners; yt-dlp robust remains the correctness backstop.
    if parsed and parsed.get("video_url"):
        if client_key in ("android_vr", "android"):
            parsed = None
        else:
            return parsed

    # A fresh Visitor ID can change the format inventory/direct URL eligibility. Retry once,
    # but only when the first response actually taught us a new identity.
    if visitor_after and visitor_after != visitor_before:
        data2 = _request_once(visitor_after)
        _capture_480_visitor_data(data2)
        parsed2 = _parse(data2, "_VD")
        if client_key in ("android_vr", "android"):
            parsed2 = None
        if parsed2 and (parsed2.get("video_url") or parsed2.get("audio_url")):
            return parsed2

    # Audio-only partial result is still valuable; the orchestrator can combine it with a
    # HARD-480 URL from another client or yt-dlp resolver.
    return parsed


def _fast_innertube_480(url: str):
    """Race low-latency Player clients and return as soon as 244, else 135, has a direct URL."""
    video_id = _extract_youtube_video_id(url)
    if not video_id:
        return None
    candidates = []
    # V7.7: only clients whose adaptive GVS URLs are suitable for the direct hot path
    # are allowed to win without a token-provider round trip. android/android_vr may
    # expose adaptive media very early but can yield HTTP 403 at GVS, so they are intentionally
    # excluded from the winner set rather than creating a fake 0.1s resolver win.
    for key in ("visionos", "web_embedded"):
        if _get_innertube_client(key):
            candidates.append(key)
    if not candidates:
        return None

    futs = [_FAST_PLAYER_EXECUTOR.submit(_fast_player_request_480, video_id, k) for k in candidates[:2]]
    best_audio = None
    deadline = time.perf_counter() + 1.00
    pending = set(futs)
    try:
        while pending and time.perf_counter() < deadline:
            done, pending = wait(pending, timeout=max(0.0, min(0.12, deadline-time.perf_counter())), return_when=FIRST_COMPLETED)
            for fut in done:
                try: data = fut.result()
                except Exception: data = None
                if not data:
                    continue
                if data.get("video_url"):
                    for other in pending:
                        other.cancel()
                    return data
                if data.get("audio_url") and best_audio is None:
                    best_audio = data
    except Exception:
        pass
    for fut in pending:
        fut.cancel()
    return best_audio


def _fmt_is_audio_only(fmt: dict) -> bool:
    vcodec = str(fmt.get("vcodec") or "").lower()
    return vcodec in ("none", "") and bool(fmt.get("url"))


def _fmt_lang_text(fmt: dict) -> str:
    vals = [fmt.get("language"), fmt.get("format_note"), fmt.get("format_id"), fmt.get("name")]
    return " ".join(str(x or "") for x in vals).lower()


def _pick_ytdlp_480_bundle(info: dict, source: str):
    if not info:
        return None
    formats = list(info.get("formats") or [])
    v = None
    for wanted_itag in HARD_480_VIDEO_ITAGS:
        videos = [f for f in formats if str(f.get("format_id") or "") == wanted_itag and f.get("url")]
        if videos:
            v = videos[-1]
            break
    if not v:
        return None
    selected_video_itag = str(v.get("format_id") or "")
    ja_audio = [f for f in formats if _fmt_is_audio_only(f) and _looks_japanese_lang(_fmt_lang_text(f))]
    if not ja_audio:
        # Strict JA: no silent switch to another language.
        return {
            "id": info.get("id") or "",
            "title": info.get("title") or "video",
            "video_url": v.get("url"),
            "video_format_id": selected_video_itag,
            "video_content_length": int(v.get("filesize") or v.get("filesize_approx") or 0),
            "audio_url": None,
            "audio_format_id": "",
            "audio_content_length": 0,
            "source": source,
            "resolver_ms": 0.0,
            "ja_audio_missing": True,
        }
    def rank_audio(f):
        fid = str(f.get("format_id") or "")
        ext = str(f.get("ext") or "").lower()
        ac = str(f.get("acodec") or "").lower()
        if fid == "139": p = 0
        elif fid == "140": p = 1
        elif ext == "m4a" or "mp4a" in ac: p = 2
        else: p = 5
        try: abr = float(f.get("abr") or f.get("tbr") or 999999.0)
        except Exception: abr = 999999.0
        return (p, abr)
    ja_audio.sort(key=rank_audio)
    a = ja_audio[0]
    def _sz(f):
        try: return int(f.get("filesize") or f.get("filesize_approx") or 0)
        except Exception: return 0
    return {
        "id": info.get("id") or "",
        "title": info.get("title") or "video",
        "video_url": v.get("url"),
        "video_format_id": selected_video_itag,
        "video_content_length": _sz(v),
        "audio_url": a.get("url"),
        "audio_format_id": str(a.get("format_id") or ""),
        "audio_content_length": _sz(a),
        "audio_language": str(a.get("language") or "ja"),
        "audio_ext": str(a.get("ext") or ""),
        "audio_mime": ("audio/webm" if str(a.get("ext") or "").lower() in ("webm", "weba")
                       else "audio/mp4" if str(a.get("ext") or "").lower() in ("m4a", "mp4") else ""),
        "duration_sec": float(info.get("duration") or 0.0),
        "source": source,
        "resolver_ms": 0.0,
    }


def _preferred_js_runtime_opts():
    """Use the runtime yt-dlp currently recommends when present; Node remains supported."""
    deno = shutil.which("deno") or shutil.which("deno.exe")
    if deno:
        return {"deno": {"path": deno}}
    node = shutil.which("node") or shutil.which("node.exe")
    if node:
        return {"node": {"path": node}}
    return {}


def _get_thread_fast_ytdlp_480_variant(client_key: str):
    """Persistent one-client yt-dlp resolver. Separate clients are raced in parallel."""
    if not yt_dlp:
        return None
    key = str(client_key or "android_vr")
    cache = getattr(_FAST_YTDLP_TLS, "ydl_480_fast_variants", None)
    if cache is None:
        cache = {}
        _FAST_YTDLP_TLS.ydl_480_fast_variants = cache
    if key in cache:
        return cache[key]
    yt_args = {
        "player_client": [key],
        "skip": ["hls", "dash", "translated_subs"],
    }
    # For the two raw/direct-oriented clients, remove webpage/config/JS work entirely.
    # visionos is allowed to use EJS because its inventory may need challenge solving.
    if key in ("android_vr", "web_embedded"):
        yt_args["player_skip"] = ["webpage", "configs", "initial_data", "js"]
    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "extract_flat": False, "socket_timeout": 2.2,
        "retries": 0, "extractor_retries": 0,
        "extractor_args": {"youtube": yt_args},
    }
    js = _preferred_js_runtime_opts()
    if js:
        opts["js_runtimes"] = js
    ydl = yt_dlp.YoutubeDL(opts)
    cache[key] = ydl
    return ydl


def _ytdlp_fast_480_variant_once(url: str, client_key: str):
    t0 = time.perf_counter()
    try:
        ydl = _get_thread_fast_ytdlp_480_variant(client_key)
        if not ydl:
            return None
        info = ydl.extract_info(url, download=False)
        data = _pick_ytdlp_480_bundle(info, f"YTDLP_480_FAST_{client_key.upper()}")
        if data:
            data["resolver_ms"] = (time.perf_counter() - t0) * 1000.0
        return data
    except Exception:
        return None


def _get_thread_fast_ytdlp_480():
    # Backward-compatible object for older callers; keep a safe direct-capable client.
    return _get_thread_fast_ytdlp_480_variant("visionos")


def _get_thread_robust_ytdlp_480():
    if not yt_dlp:
        return None
    ydl = getattr(_FAST_YTDLP_TLS, "ydl_480_robust", None)
    if ydl is None:
        opts = {
            # Do not force a format in yt-dlp here. Extract inventory once; then the picker
            # chooses 244 first and 135 second without adding any availability delay.
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "extract_flat": False, "socket_timeout": 6,
            "retries": 1, "extractor_retries": 1,
            "extractor_args": {"youtube": {"skip": ["hls", "dash", "translated_subs"]}},
        }
        js = _preferred_js_runtime_opts()
        if js:
            opts["js_runtimes"] = js
        ydl = yt_dlp.YoutubeDL(opts)
        _FAST_YTDLP_TLS.ydl_480_robust = ydl
    return ydl


def _ytdlp_resolve_480_on_warm_thread(url: str, robust: bool = False):
    t0 = time.perf_counter()
    if robust:
        try:
            ydl = _get_thread_robust_ytdlp_480()
            if not ydl:
                return None
            info = ydl.extract_info(url, download=False)
            data = _pick_ytdlp_480_bundle(info, "YTDLP_480_ROBUST")
            if data:
                data["resolver_ms"] = (time.perf_counter() - t0) * 1000.0
            return data
        except Exception:
            return None

    # Three independent yt-dlp clients race. A failed/unsupported client never delays another.
    futs = {
        _YTDLP_FAST_CLIENT_EXECUTOR.submit(_ytdlp_fast_480_variant_once, url, "web_embedded"): "web_embedded",
        _YTDLP_FAST_CLIENT_EXECUTOR.submit(_ytdlp_fast_480_variant_once, url, "visionos"): "visionos",
    }
    best_partial = None
    deadline = time.perf_counter() + 1.15
    pending = set(futs)
    try:
        while pending and time.perf_counter() < deadline:
            done, pending = wait(pending, timeout=max(0.0, min(0.10, deadline-time.perf_counter())), return_when=FIRST_COMPLETED)
            for fut in done:
                try: data = fut.result()
                except Exception: data = None
                if not data:
                    continue
                if data.get("video_url"):
                    for other in pending:
                        other.cancel()
                    data["resolver_ms"] = (time.perf_counter() - t0) * 1000.0
                    return data
                if data.get("audio_url") and best_partial is None:
                    best_partial = data
    except Exception:
        pass
    for fut in pending:
        fut.cancel()
    if best_partial:
        best_partial["resolver_ms"] = (time.perf_counter() - t0) * 1000.0
    return best_partial


def _prewarm_ytdlp_480_resolver_thread():
    try:
        _get_thread_fast_ytdlp_480_variant("web_embedded")
        _get_thread_fast_ytdlp_480_variant("visionos")
        _get_thread_robust_ytdlp_480()
        return True
    except Exception:
        return False

def _prewarm_fast_youtube_connection():
    """Warm 2 TLS connection song song để player race không phải handshake lúc có video."""
    try:
        pool = _get_fast_pool()
        if pool is None:
            conn = http.client.HTTPSConnection("www.youtube.com", timeout=1.0)
            conn.request("HEAD", "/generate_204", headers={"Connection": "keep-alive"})
            r = conn.getresponse(); r.read()
            _FAST_HTTP_TLS.conn_www_youtube_com = conn
            return

        def _one():
            try:
                pool.request(
                    "HEAD", "https://www.youtube.com/generate_204",
                    headers={"Connection": "keep-alive", "Accept-Encoding": "identity"},
                    timeout=urllib3.Timeout(connect=0.60, read=0.60), retries=False,
                    preload_content=True,
                )
            except Exception:
                pass
        f1 = _FAST_PLAYER_EXECUTOR.submit(_one)
        f2 = _FAST_PLAYER_EXECUTOR.submit(_one)
        f3 = _FAST_PLAYER_EXECUTOR.submit(_one)
        try: f1.result(timeout=1.0)
        except Exception: pass
        try: f2.result(timeout=1.0)
        except Exception: pass
        try: f3.result(timeout=1.0)
        except Exception: pass
    except Exception:
        pass


def _fast_keepalive_worker():
    visitor_tick = 0
    while not _FAST_KEEPALIVE_STOP.is_set():
        if (float(V94_FF_KEEPWARM_SEC or 0) > 0 and _v94_jobs_active() == 0 and FFMPEG_PATH
                and _v94_ff_idle_s(FFMPEG_PATH) >= float(V94_FF_KEEPWARM_SEC)):
            # V9.4.1 (mặc định tắt): giữ ffmpeg.exe "nóng" khi rảnh lâu.
            _v94_ff_prewarm("KEEPWARM")
        if V94_KEEPALIVE_YIELD and _v94_jobs_active() > 0:
            # V9.4: đang có job tải → connection đã nóng sẵn nhờ chính job; không tranh băng thông.
            _FAST_KEEPALIVE_STOP.wait(2.0)
            continue
        _prewarm_fast_youtube_connection()
        # Refresh visitor identity while idle; never put this GET on the fresh-video hot path.
        visitor_tick += 1
        if visitor_tick == 1 or visitor_tick >= 25:  # ~5 minutes at 12s cadence
            _prewarm_480_visitor_data(force=False)
            visitor_tick = 1
        _FAST_KEEPALIVE_STOP.wait(12.0)


def _start_fast_keepalive():
    global _FAST_KEEPALIVE_STARTED
    with _FAST_KEEPALIVE_LOCK:
        if _FAST_KEEPALIVE_STARTED:
            return
        _FAST_KEEPALIVE_STARTED = True
        # V9.1: nạp chứng chỉ TLS + dựng pool media ngay lúc khởi động, không phải lúc có video.
        threading.Thread(target=_get_direct_media_pool, name="TLSWarm", daemon=True).start()
        threading.Thread(target=_fast_keepalive_worker, name="YTKeepAlive", daemon=True).start()


def _yt_info_to_direct(info, source: str):
    if not info or not info.get("url"):
        return None
    size = info.get("filesize") or info.get("filesize_approx") or 0
    try:
        size = int(size or 0)
    except Exception:
        size = 0
    out = {
        "url": info["url"],
        "title": info.get("title", "video"),
        "id": info.get("id", str(int(time.time()))),
        "format_id": str(info.get("format_id") or "18"),
        "content_length": size,
        # V9.5: filesize_approx = tbr×duration chỉ là ƯỚC LƯỢNG → không được dùng làm sàn kiểm size chính xác.
        "length_exact": bool(info.get("filesize")),
        "source": source,
        "default_audio_lang": str(info.get("language") or "").lower(),
        "has_ja_audio": False, "audio_tracks": 0,
        "duration_sec": float(info.get("duration") or 0.0),
    }
    try:
        ids = set()
        for f in info.get("formats") or []:
            if not _fmt_is_audio_only(f):
                continue
            ids.add(str(f.get("format_id") or "").split("-", 1)[-1] if "-" in str(f.get("format_id") or "") else "_default")
            if _looks_japanese_lang(_fmt_lang_text(f)):
                out["has_ja_audio"] = True
        out["audio_tracks"] = len(ids)
        if not out["default_audio_lang"]:
            # itag 18 không có audioTrack → info["language"] = None. Lấy từ track được yt-dlp đánh dấu
            # default/original (language_preference 10/5 hoặc format_note "(default)"/"(original)").
            for f in info.get("formats") or []:
                if not _fmt_is_audio_only(f) or not f.get("language"):
                    continue
                note = str(f.get("format_note") or "").lower()
                if f.get("language_preference") in (10, 5) or "(default)" in note or "(original)" in note:
                    out["default_audio_lang"] = str(f["language"]).lower()
                    break
        if not out["has_ja_audio"] and _looks_japanese_lang(out["default_audio_lang"]):
            out["has_ja_audio"] = True
    except Exception:
        pass
    return out


def _get_thread_fast_ytdlp():
    """Fast fallback: persistent YoutubeDL, minimal extraction, no JS challenge path."""
    if not yt_dlp:
        return None
    ydl = getattr(_FAST_YTDLP_TLS, "ydl_fast", None)
    if ydl is None:
        opts = {
            "format": "18",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "extract_flat": False,
            "socket_timeout": 3,
            "retries": 0,
            "extractor_retries": 0,
            "extractor_args": {
                "youtube": {
                    "player_client": ["android_vr", "web_embedded", "android"],
                    "player_skip": ["webpage", "configs", "js", "initial_data"],
                    "skip": ["hls", "dash", "translated_subs"],
                }
            },
        }
        ydl = yt_dlp.YoutubeDL(opts)
        _FAST_YTDLP_TLS.ydl_fast = ydl
    return ydl


def _get_thread_robust_ytdlp():
    """Robust persistent resolver.

    Do NOT hard-code legacy Android/iOS here. yt-dlp's current default YouTube client
    selection can use EJS + an installed PO-token provider plugin when required, while
    still falling back to clients that do not need a token. Keeping this object alive
    avoids paying YoutubeDL initialization cost for every difficult video.
    """
    if not yt_dlp:
        return None
    ydl = getattr(_FAST_YTDLP_TLS, "ydl_robust", None)
    if ydl is None:
        opts = {
            "format": "18",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "extract_flat": False,
            "socket_timeout": 6,
            "retries": 1,
            "extractor_retries": 1,
            # Keep JS/config extraction enabled here. Current yt-dlp can use EJS
            # (Deno/Node runtime) and PO-token provider plugins transparently.
            "extractor_args": {
                "youtube": {
                    "skip": ["hls", "dash", "translated_subs"],
                }
            },
        }
        ydl = yt_dlp.YoutubeDL(opts)
        _FAST_YTDLP_TLS.ydl_robust = ydl
    return ydl


def _ytdlp_resolve_on_warm_thread(url: str, robust: bool = False):
    t0 = time.perf_counter()
    ydl = _get_thread_robust_ytdlp() if robust else _get_thread_fast_ytdlp()
    if not ydl:
        return None
    info = ydl.extract_info(url, download=False)
    data = _yt_info_to_direct(info, "YTDLP_ROBUST" if robust else "YTDLP_FAST")
    if data:
        data["resolver_ms"] = (time.perf_counter() - t0) * 1000.0
    return data


def _prewarm_ytdlp_resolver_thread():
    # Object creation only; no YouTube request is sent.
    try:
        _get_thread_fast_ytdlp()
        _get_thread_robust_ytdlp()
        time.sleep(0.05)
        return True
    except Exception:
        return False


def _youtube_runtime_summary():
    version = "?"
    try:
        version = str(getattr(getattr(yt_dlp, "version", None), "__version__", "?"))
    except Exception:
        pass
    js = []
    if shutil.which("deno"):
        js.append("deno")
    if shutil.which("node") or shutil.which("node.exe"):
        js.append("node")
    return version, (",".join(js) if js else "none")


# ===================== THƯ VIỆN EXCEL =====================
try:
    import pandas as pd
    from openpyxl import load_workbook
except ImportError:
    pd = None
    load_workbook = None

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
CONFIG_JSON= os.path.join(BASE_DIR, "yt_720p_gui_config.json")
CONFIG_THEME_JSON = os.path.join(BASE_DIR, "chiadeu_theme.json")

# ===================== KHỞI TẠO PATH CÔNG CỤ =====================

def find_aria2c():
    cands = [
        os.path.join(BASE_DIR, "aria2c.exe"),
        shutil.which("aria2c.exe"),
        shutil.which("aria2c"),
    ]
    for c in cands:
        if c and os.path.exists(c): return c
    return None

def find_idm():
    """Tự tìm IDMan.exe: ưu tiên cùng folder tool, Registry, rồi Program Files."""
    cands = [os.path.join(BASE_DIR, "IDMan.exe")]

    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\DownloadManager") as key:
                exe_path, _ = winreg.QueryValueEx(key, "ExePath")
                if exe_path:
                    cands.append(str(exe_path).strip().strip('"'))
        except Exception:
            pass

    for env_name in ("ProgramFiles(x86)", "ProgramFiles"):
        root = os.environ.get(env_name)
        if root:
            cands.append(os.path.join(root, "Internet Download Manager", "IDMan.exe"))

    cands.extend([shutil.which("IDMan.exe"), shutil.which("idman")])

    seen = set()
    for c in cands:
        if not c:
            continue
        c = os.path.abspath(os.path.expandvars(c))
        key = os.path.normcase(c)
        if key in seen:
            continue
        seen.add(key)
        if os.path.isfile(c):
            return c
    return None

def find_ffmpeg():
    local = os.path.join(BASE_DIR, "ffmpeg", "ffmpeg.exe")
    return local if os.path.exists(local) else shutil.which("ffmpeg")

def find_ffprobe():
    local = os.path.join(BASE_DIR, "ffmpeg", "ffprobe.exe")
    return local if os.path.exists(local) else shutil.which("ffprobe")

def find_ytdlp():
    cands = [
        os.path.join(BASE_DIR, "yt-dlp.exe"),
        shutil.which("yt-dlp.exe"),
        shutil.which("yt-dlp"),
    ]
    for c in cands:
        if c and os.path.exists(c): return c
    return None

ARIA2C_PATH  = find_aria2c()
IDM_PATH     = find_idm()
FFMPEG_PATH  = find_ffmpeg()
FFPROBE_PATH = find_ffprobe()
YTDLP_PATH   = find_ytdlp()


# ===================== TIỆN ÍCH FILE & EXCEL =====================
SAFE_NAME_RE = re.compile(r'[\/:*?"<>|\r\n]+')
def sanitize_name(s: str) -> str:
    if not s: return "video"
    s = SAFE_NAME_RE.sub(" ", s).strip()
    return re.sub(r"\s+", " ", s)[:150]


# ===================== SMART TITLE — HẬU XỬ LÝ, KHÔNG ĐỤNG ENGINE TẢI =====================
SMART_JP_TAGS = ["【神回】", "【必見】", "【話題】", "衝撃", "切り抜き", "おすすめ",
                 "ハイライト", "絶対に笑う", "", "", ""]
SMART_JP_EMOTIONS = ["", "w", "ww", "www", "!!", "!?", "...", "(笑)", "(泣)"]

def _smart_title_snippet(original_title: str) -> str:
    raw_title = (original_title or "").strip()
    if re.fullmatch(r"(?i)video[_ -]?\d+(?:[_ -].*)?", raw_title):
        return ""
    safe_title = sanitize_name(raw_title or "video")
    length = random.randint(6, 14)
    if len(safe_title) > length:
        start = random.randint(0, len(safe_title) - length)
        snippet = safe_title[start:start + length].strip()
    else:
        snippet = safe_title
    return snippet or safe_title or "video"

def _clean_smart_name(value: str) -> str:
    cleaned = sanitize_name(value).strip(" .")
    return cleaned or "video"

def generate_dynamic_jp_part_name(original_title: str, part_idx: int) -> str:
    sort_prefix = f"[{part_idx + 1:02d}]"
    part_str = random.choice([
        f"その{part_idx + 1}",
        f"パート{part_idx + 1}",
        f"Part{part_idx + 1}",
        f"第{part_idx + 1}回",
    ])
    name_parts = [
        sort_prefix,
        random.choice(SMART_JP_TAGS),
        _smart_title_snippet(original_title),
        random.choice(SMART_JP_EMOTIONS),
        part_str,
    ]
    return _clean_smart_name(" ".join(p for p in name_parts if p))

def generate_dynamic_jp_single_name(original_title: str) -> str:
    clean_original = _clean_smart_name(original_title)
    snippet = _smart_title_snippet(original_title) or clean_original
    name_parts = [
        random.choice(SMART_JP_TAGS),
        snippet,
        random.choice(SMART_JP_EMOTIONS),
    ]
    final_name = _clean_smart_name(" ".join(p for p in name_parts if p))
    return final_name or clean_original

def read_original_title_from_excel(excel_path: str, url_index: int, logger) -> str:
    # Ưu tiên đúng title đã có trong Excel. Nếu trống, caller dùng title YouTube
    # đã lấy sẵn ở bước Native API — KHÔNG gọi lại YouTube.
    for col_name in ("TITLE", "TIÊU ĐỀ"):
        value = read_excel_cell_value(excel_path, url_index, col_name, logger)
        if value:
            logger(f"    📝 TIÊU ĐỀ GỐC [{col_name}]: {value}")
            return value
    return ""

def ensure_dir(p):
    os.makedirs(p, exist_ok=True); return p

_V94_EXCEL_WRITE_LOCK = threading.Lock()


def write_status_to_excel(excel_path: str, url_index: int, status_col_name: str, new_status: str, logger,
                          expected_url: str = None, url_col_name: str = None, stats: dict = None) -> bool:
    """V9.4: tuần tự hoá load→sửa→save trong tiến trình. V9.3 cho 3–4 job ghi song song nên
    2 job cùng load bản cũ → job save sau đè mất DONE của job trước (tái hiện ở test GUI 4 job).
    V9.4.1: trả True/False; (tuỳ chọn) kiểm tra dòng đích vẫn chứa video id của job; os.replace nguyên tử."""
    _t = time.perf_counter()
    with _V94_EXCEL_WRITE_LOCK:
        if isinstance(stats, dict):
            stats["done_lock_wait_ms"] = round((time.perf_counter() - _t) * 1000.0, 1)
        return _write_status_to_excel_locked(excel_path, url_index, status_col_name, new_status, logger,
                                             expected_url=expected_url, url_col_name=url_col_name, stats=stats)


_V94_YT_ID_IN_CELL = re.compile(
    r"(?:youtube\.com/(?:shorts/|embed/|live/|watch\?(?:[^\s\"']*?&)?v=)|youtu\.be/)([A-Za-z0-9_-]{11})")
_V94_VERIFY_SKIP_LOGGED = [False]


def _v94_find_url_col(header, url_col_name):
    try:
        if url_col_name and url_col_name in header:
            return header.index(url_col_name) + 1
        for i, h in enumerate(header):
            if str(h or "").strip().upper() == "URL":
                return i + 1
    except Exception:
        pass
    return None


def _write_status_to_excel_locked(excel_path: str, url_index: int, status_col_name: str, new_status: str, logger,
                                  expected_url: str = None, url_col_name: str = None, stats: dict = None) -> bool:
    st = stats if isinstance(stats, dict) else {}
    try:
        if not load_workbook:
            logger("⚠️ Thiếu 'openpyxl', không ghi STATUS được."); return False
        _t = time.perf_counter()
        wb = load_workbook(excel_path)
        st["done_load_ms"] = round((time.perf_counter() - _t) * 1000.0, 1)
        ws = wb.worksheets[0]
        header = [c.value for c in ws[1]]
        try:
            col_idx = header.index(status_col_name) + 1
        except ValueError:
            logger(f"⚠️ Không thấy cột '{status_col_name}'."); return False
        row = url_index + 2
        # V9.4.1: DONE chỉ vào dòng VẪN chứa đúng video id của job (scanner/người dùng có thể chèn, xoá,
        # sắp xếp dòng trong ~3s job chạy → ghi theo vị trí sẽ đánh DONE nhầm một URL khác chưa có file).
        if V94_STATUS_VERIFY_URL and expected_url:
            vid = _extract_youtube_video_id(expected_url)
            ucol = _v94_find_url_col(header, url_col_name)
            if vid and ucol:
                cell = str(ws.cell(row=row, column=ucol).value or "") if row <= ws.max_row else ""
                if vid not in cell:
                    _c = cell.strip()
                    _no_id_formula = (_c.startswith("=") and not _V94_YT_ID_IN_CELL.search(_c)
                                      and not _extract_youtube_video_id(_c.split('"')[1] if '"' in _c else ""))
                    # Review V9.4.1: mọi ô KHÔNG rỗng và không phải công thức-không-id đều coi là "video khác"
                    # (kể cả host viết hoa, /v/, nocookie, URL không phải YouTube) → đi tìm dòng đúng.
                    if (_c and not _no_id_formula) or row > ws.max_row:
                        matches = [r for r in range(2, ws.max_row + 1)
                                   if vid in str(ws.cell(row=r, column=ucol).value or "")]
                        if len(matches) == 1:
                            logger(f"    -> STATUS row moved {row}→{matches[0]} (dòng Excel đã đổi vị trí)")
                            st["status_row_moved"] = f"{row}->{matches[0]}"
                            row = matches[0]
                        else:
                            logger(f"    ⚠️ STATUS row mismatch: dòng {row} không còn chứa {vid} "
                                   f"({len(matches)} dòng khớp) → KHÔNG ghi DONE")
                            st["status_row_mismatch"] = len(matches)
                            return False
                    elif not _V94_VERIFY_SKIP_LOGGED[0]:
                        _V94_VERIFY_SKIP_LOGGED[0] = True
                        logger("    ℹ️ STATUS verify skipped: ô URL không chứa video id (công thức/ô trống) → ghi theo vị trí")
        if row > ws.max_row:
            logger(f"⚠️ Dòng {row} ngoài phạm vi."); return False
        ws.cell(row=row, column=col_idx).value = new_status
        temp_path = excel_path + ".tmp"
        
        loop = QEventLoop()
        QTimer.singleShot(0, loop.quit)
        loop.exec()
        
        _t = time.perf_counter()
        wb.save(temp_path)
        st["done_save_ms"] = round((time.perf_counter() - _t) * 1000.0, 1)
        _t = time.perf_counter()
        if V94_EXCEL_ATOMIC_REPLACE:
            # os.replace = đổi tên nguyên tử (MoveFileEx REPLACE_EXISTING). shutil.move trên Windows khi đích đã
            # tồn tại sẽ COPY đè từng byte → người đọc khác (realtime/scanner/job khác) có thể thấy zip dở dang.
            _done = False
            _tries = 0
            for _ in range(10):
                try:
                    with _EXCEL_DF_CACHE_LOCK:      # không reader nội bộ nào đang mở file lúc đổi tên
                        os.replace(temp_path, excel_path)
                    _done = True
                    break
                except OSError:
                    _tries += 1
                    time.sleep(0.03)
            st["replace_retries"] = _tries
            if not _done:
                st["replace_fallback"] = True
                shutil.move(temp_path, excel_path)
        else:
            shutil.move(temp_path, excel_path)
        st["done_replace_ms"] = round((time.perf_counter() - _t) * 1000.0, 1)
        try:
            st["xlsx_bytes"] = os.path.getsize(excel_path)
            st["n_sheets"] = len(wb.worksheets)
        except OSError:
            pass
        logger(f"    -> STATUS='{new_status}' @ dòng {row}")
        return True
    except Exception as e:
        logger(f"❌ Lỗi ghi Excel: {e}")
        return False

# ================== REALTIME WAKE-UP FAST PATH ================== #
# Scanner V34 ghi Excel xong mới append trigger_wakeup.txt. Downloader vẫn đọc
# Excel như cũ để lấy CHANNEL ID / TITLE / LINK VIDEO GỐC / STATUS và logic MOVE,
# nhưng không còn phải chờ cứng 1 giây mới kiểm tra lại.
REALTIME_WAKEUP_FILE = "trigger_wakeup.txt"
REALTIME_WAKEUP_CHECK_SEC = V94_WAKEUP_CHECK_SEC   # V9.4: 10ms (V9.3 = 25ms); chỉ stat file
REALTIME_FALLBACK_SCAN_SEC = 1.0    # nếu mất trigger vẫn quét Excel như bản cũ


def _rt_file_signature(path):
    """Chữ ký cực nhẹ để phát hiện file vừa thay đổi; không mở/đọc nội dung file."""
    try:
        st = os.stat(str(path))
        return (int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return None


def _rt_trigger_signature(paths):
    """Theo dõi trigger ở cả CWD và cùng thư mục với check.xlsx."""
    sig = []
    for p in paths:
        try:
            sig.append((str(p), _rt_file_signature(p)))
        except Exception:
            sig.append((str(p), None))
    return tuple(sig)


def _v94_wake_span(reason, sig):
    """trigger_detected: độ trễ (wall clock) từ mtime file trigger/Excel tới lúc phát hiện."""
    try:
        lag = None
        now_ns = time.time_ns()
        for _p, _sg in (sig or ()):
            if _sg:
                d = (now_ns - int(_sg[0])) / 1e6
                lag = d if lag is None else min(lag, d)
        _V94_LAST_WAKE["lag_ms"] = round(lag, 2) if lag is not None else None
        TELEM.span("-", "trigger_detected", reason=reason,
                   lag_ms=round(lag, 2) if lag is not None else None)
        return _V94_LAST_WAKE["lag_ms"]
    except Exception:
        return None


def _rt_wait_for_wakeup(stop_event, trigger_paths, excel_path, trigger_sig, excel_sig):
    """
    Chờ tối đa 1 giây nhưng thức gần như ngay khi:
      1) scanner append trigger_wakeup.txt, hoặc
      2) check.xlsx đổi mtime/size (fallback nhanh nếu trigger bị lỗi).
    Không đọc Excel trong lúc chờ, nên rất nhẹ CPU/disk.
    """
    deadline = time.perf_counter() + REALTIME_FALLBACK_SCAN_SEC
    latest_trigger = trigger_sig
    latest_excel = excel_sig

    while not stop_event.is_set():
        latest_trigger = _rt_trigger_signature(trigger_paths)
        latest_excel = _rt_file_signature(excel_path)

        if latest_trigger != trigger_sig:
            _v94_wake_span("TRIGGER", latest_trigger)
            return latest_trigger, latest_excel, "TRIGGER"
        if latest_excel != excel_sig:
            _v94_wake_span("EXCEL", (("excel", latest_excel),))
            return latest_trigger, latest_excel, "EXCEL"

        remain = deadline - time.perf_counter()
        if remain <= 0:
            return latest_trigger, latest_excel, "FALLBACK"

        # Event.wait cho phép Stop bẻ giấc ngủ ngay, không phải đợi sleep xong.
        stop_event.wait(min(REALTIME_WAKEUP_CHECK_SEC, remain))

    return latest_trigger, latest_excel, "STOP"


_EXCEL_DF_CACHE = {}
_EXCEL_DF_CACHE_LOCK = threading.Lock()
_V94_CALAMINE = [None]
_V94_CALAMINE_VERIFIED = {}


def _v94_calamine_ok() -> bool:
    if _V94_CALAMINE[0] is None:
        try:
            import python_calamine  # noqa: F401
            _V94_CALAMINE[0] = True
        except Exception:
            _V94_CALAMINE[0] = False
    return bool(_V94_CALAMINE[0])


def _read_excel_df_cached(excel_path: str):
    """V8.9: TITLE/TIÊU ĐỀ/LINK VIDEO GỐC trước đây đọc Excel 3 lần bằng pandas trên hot path.
    Cache theo (mtime, size): file đổi là đọc lại, nên dữ liệu luôn đúng."""
    try:
        st = os.stat(excel_path)
        sig = (int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return None
    with _EXCEL_DF_CACHE_LOCK:
        hit = _EXCEL_DF_CACHE.get(excel_path)
        if hit and hit[0] == sig:
            return hit[1]
        df = None
        _engine = "openpyxl"
        TELEM.span("-", "excel_read_begin")
        # Review V9.4.1: đọc cả file vào RAM rồi parse từ BytesIO → handle chỉ mở vài ms, os.replace của
        # write_status_to_excel (Windows từ chối khi file đang mở) gần như không bao giờ bị chặn.
        import io as _io
        with open(excel_path, "rb") as _fh:
            _xbytes = _fh.read()
        if V94_EXCEL_CALAMINE and _v94_calamine_ok() and _V94_CALAMINE_VERIFIED.get(excel_path, True):
            try:
                df = pd.read_excel(_io.BytesIO(_xbytes), sheet_name=0, dtype=str, engine="calamine").fillna("")
                _engine = "calamine"
            except Exception:
                df = None
            if df is not None and excel_path not in _V94_CALAMINE_VERIFIED:
                # Lần đầu với file này: đối chiếu 1 lần với openpyxl (đúng engine V9.3). Chỉ số dòng
                # quyết định dòng ghi DONE → bất kỳ sai khác shape/cột/URL nào là tắt calamine cho file.
                try:
                    ref = pd.read_excel(_io.BytesIO(_xbytes), sheet_name=0, dtype=str).fillna("")
                    same = (ref.shape == df.shape and list(map(str, ref.columns)) == list(map(str, df.columns))
                            and ref.astype(str).values.tolist() == df.astype(str).values.tolist())
                    _V94_CALAMINE_VERIFIED[excel_path] = bool(same)
                    if not same:
                        df, _engine = ref, "openpyxl"
                except Exception:
                    _V94_CALAMINE_VERIFIED[excel_path] = False
                    df = None
        if df is None:
            df = pd.read_excel(_io.BytesIO(_xbytes), sheet_name=0, dtype=str).fillna("")
            _engine = "openpyxl"
        TELEM.span("-", "excel_read_end", engine=_engine, rows=int(len(df)))
        _EXCEL_DF_CACHE.clear()
        _EXCEL_DF_CACHE[excel_path] = (sig, df)
        return df


def read_links_excel_url_only(excel_path, logger):
    links_with_index=[]; url_col_name=None; status_col_name="STATUS"
    if not pd:
        logger("⚠️ Thiếu 'pandas'. Cài: pip install pandas openpyxl"); return [], None, None
    try:
        if os.path.exists(excel_path):
            # V9.0: dùng chung cache → CHANNEL ID / TÊN KÊNH / TITLE / LINK VIDEO GỐC đọc sau đó
            # không parse lại xlsx (pandas giữ GIL, tranh CPU với 40 luồng tải đúng lúc khởi động).
            df = _read_excel_df_cached(excel_path)
            if df is None:
                return [], None, None
            cols_upper = {str(c).upper(): c for c in df.columns}
            if "URL" not in cols_upper:
                logger('⚠️ Excel thiếu cột "URL".'); return [], None, None
            url_col_name = cols_upper["URL"]
            if "STATUS" in cols_upper: status_col_name = cols_upper["STATUS"]
            # V9.4: duyệt cột thay vì df.iterrows() (iterrows tạo 1 Series/dòng: 500 dòng ≈ 20-30ms,
            # 3000 dòng ≈ 150ms trên hot path phát hiện URL). Ngữ nghĩa giữ nguyên.
            _urls = df[url_col_name].tolist()
            _sts = (df[status_col_name].tolist() if status_col_name in df.columns else [""] * len(_urls))
            for idx, _u, _s in zip(df.index.tolist(), _urls, _sts):
                v = str(_u).strip()
                st = str(_s or "").upper().strip()
                if v.startswith("http") and st != "DONE":
                    links_with_index.append((v, idx))
    except Exception as e:
        logger(f"⚠️ Lỗi đọc Excel: {e}")
    return links_with_index, url_col_name, status_col_name

def read_excel_cell_value(excel_path: str, url_index: int, col_name: str, logger) -> str:
    try:
        if not pd: return ""
        if not os.path.exists(excel_path): return ""
            
        df = _read_excel_df_cached(excel_path)
        if df is None: return ""
        col_names = {str(c).upper(): c for c in df.columns}
        if col_name.upper() not in col_names: return ""
            
        real_col_name = col_names[col_name.upper()]
        if url_index < 0 or url_index >= len(df): return ""
        return str(df.iloc[url_index][real_col_name]).strip()
    except Exception as _e:
        # V9.4.1: không còn im lặng — lỗi đọc (file đang bị ghi dở) làm đổi tên/đích MOVE; ghi span để đo.
        TELEM.span("-", "excel_cell_read_error", column=str(col_name), error=type(_e).__name__)
        return ""


def _ffprobe_duration_seconds(ffprobe_path: str, file_path: Path) -> float:
    # V9.4.1: dùng đúng ffprobe_path được truyền (V9.3 bỏ qua tham số này), có timeout (V9.3 có thể treo
    # vĩnh viễn và giữ 1 slot Max Threads). Cùng argv, cùng cờ console, cùng cách parse.
    exe = ffprobe_path or FFPROBE_PATH
    _to = float(V94_FFPROBE_TIMEOUT or 0)
    try:
        r = subprocess.run([exe,"-v","error","-show_entries","format=duration",
                            "-of","default=nw=1:nk=1",str(file_path)],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                           timeout=(_to if _to > 0 else None))
        val = (r.stdout or "").strip()
        return float(val) if val else 0.0
    except Exception:
        return 0.0
    finally:
        _v94_ff_mark_exit(exe)


def _ffprobe_av_info(file_path: Path) -> dict:
    info = {
        "format_duration": 0.0,
        "video_duration": 0.0,
        "audio_duration": 0.0,
        "width": 0,
        "height": 0,
        "has_video": False,
        "has_audio": False,
    }
    try:
        r = subprocess.run(
            [
                FFPROBE_PATH, "-v", "error",
                "-show_entries", "stream=codec_type,width,height,duration:format=duration",
                "-of", "json", str(file_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        _v94_ff_mark_exit(FFPROBE_PATH)
        data = json.loads(r.stdout or "{}")
        try:
            info["format_duration"] = float((data.get("format") or {}).get("duration") or 0.0)
        except Exception:
            pass

        for stream in data.get("streams") or []:
            kind = stream.get("codec_type")
            try:
                sd = float(stream.get("duration") or 0.0)
            except Exception:
                sd = 0.0

            if kind == "video" and not info["has_video"]:
                info["has_video"] = True
                info["video_duration"] = sd
                info["width"] = int(stream.get("width") or 0)
                info["height"] = int(stream.get("height") or 0)
            elif kind == "audio" and not info["has_audio"]:
                info["has_audio"] = True
                info["audio_duration"] = sd

        if info["video_duration"] <= 0 and info["has_video"]:
            info["video_duration"] = info["format_duration"]
        if info["audio_duration"] <= 0 and info["has_audio"]:
            info["audio_duration"] = info["format_duration"]
    except Exception:
        pass
    return info

def _even_dim(value: int, fallback: int) -> int:
    value = int(value or fallback)
    if value < 2:
        value = fallback
    return value if value % 2 == 0 else value - 1

SHORT_MIN_TARGET_SEC = 60.20

def prepend_tail_to_front_fast(main_video: Path, output_path: Path, duration: float, logger,
                               target_sec: float = SHORT_MIN_TARGET_SEC, stats: dict = None) -> float:
    """<60s: lấy đoạn CUỐI của chính video đưa lên ĐẦU, ưu tiên stream-copy."""
    t0 = time.perf_counter()
    try:
        duration = float(duration or 0.0)
        if duration <= 0:
            logger("❌ FAST PREPEND: duration không hợp lệ.")
            return 0.0
        if duration >= 60.0:
            return duration

        ensure_dir(output_path.parent)
        need = max(0.0, float(target_sec) - duration)
        logger(
            f"    ⚡ FAST <60s: hiện có {duration:.2f}s | thiếu {need:.2f}s "
            "→ lấy ĐOẠN CUỐI đưa lên ĐẦU."
        )

        full_repeats = int(need // duration)
        remainder = need - (full_repeats * duration)
        if remainder < 0.02:
            remainder = 0.0

        concat_list = output_path.parent / (
            f".tail_prepend_{int(time.time()*1000)}_{threading.get_ident()}.txt"
        )

        def esc(path: Path) -> str:
            return str(path.resolve()).replace("'", "'\\''")

        lines = []
        if remainder > 0.0:
            tail_start = max(0.0, duration - remainder)
            lines.append(f"file '{esc(main_video)}'")
            lines.append(f"inpoint {tail_start:.6f}")
            logger(
                f"    ↪ Tail cần lấy: {remainder:.2f}s cuối "
                f"(bắt đầu khoảng {tail_start:.2f}s)."
            )

        for _ in range(full_repeats):
            lines.append(f"file '{esc(main_video)}'")

        lines.append(f"file '{esc(main_video)}'")
        concat_list.write_text("\n".join(lines) + "\n", encoding="utf-8")

        create_no_window = 0x08000000 if os.name == "nt" else 0
        cmd = [
            FFMPEG_PATH, "-y", "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
        ]
        if output_path.suffix.lower() in (".mp4", ".m4v", ".mov"):
            cmd += ["-movflags", "+faststart"]
        cmd.append(str(output_path))

        _t_pp = time.perf_counter()
        r = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=create_no_window,
        )
        _v94_ff_mark_exit(FFMPEG_PATH)
        if isinstance(stats, dict):
            stats["pp_prepend_ffmpeg_ms"] = round((time.perf_counter() - _t_pp) * 1000.0, 1)
        try:
            concat_list.unlink()
        except OSError:
            pass

        if r.returncode == 0 and output_path.is_file() and output_path.stat().st_size > 0:
            _t_pp = time.perf_counter()
            out_info = _ffprobe_av_info(output_path)
            if isinstance(stats, dict):
                stats["pp_prepend_probe_ms"] = round((time.perf_counter() - _t_pp) * 1000.0, 1)
            out_vdur = out_info.get("video_duration") or out_info.get("format_duration") or 0.0
            if out_info.get("has_video") and out_info.get("has_audio") and out_vdur >= 60.0:
                logger(
                    f"    ✅ FAST COPY XONG: {duration:.2f}s → {out_vdur:.2f}s | "
                    f"{time.perf_counter()-t0:.2f}s"
                )
                return out_vdur

        logger("    ⚠️ FAST COPY chưa đạt. Chuyển fallback ultrafast.")
        try:
            if output_path.exists():
                output_path.unlink()
        except OSError:
            pass

        # Fallback chỉ dùng khi stream-copy không đạt >=60s.
        if need <= duration + 0.001:
            tail_start = max(0.0, duration - need)
            src_info = _ffprobe_av_info(main_video)
            _ = (
                _even_dim(src_info.get("width"), 480),
                _even_dim(src_info.get("height"), 854),
            )

            if src_info.get("has_audio"):
                filt = (
                    f"[0:v]split=2[tv][mv];"
                    f"[tv]trim=start={tail_start:.6f},setpts=PTS-STARTPTS[tv2];"
                    f"[mv]setpts=PTS-STARTPTS[mv2];"
                    f"[0:a]asplit=2[ta][ma];"
                    f"[ta]atrim=start={tail_start:.6f},asetpts=PTS-STARTPTS[ta2];"
                    f"[ma]asetpts=PTS-STARTPTS[ma2];"
                    f"[tv2][ta2][mv2][ma2]concat=n=2:v=1:a=1[v][a]"
                )
                cmd2 = [
                    FFMPEG_PATH, "-y", "-i", str(main_video),
                    "-filter_complex", filt,
                    "-map", "[v]", "-map", "[a]",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
                    "-c:a", "aac", "-b:a", "128k",
                ]
            else:
                filt = (
                    f"[0:v]split=2[tv][mv];"
                    f"[tv]trim=start={tail_start:.6f},setpts=PTS-STARTPTS[tv2];"
                    f"[mv]setpts=PTS-STARTPTS[mv2];"
                    f"[tv2][mv2]concat=n=2:v=1:a=0[v]"
                )
                cmd2 = [
                    FFMPEG_PATH, "-y", "-i", str(main_video),
                    "-filter_complex", filt,
                    "-map", "[v]",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
                ]

            if output_path.suffix.lower() in (".mp4", ".m4v", ".mov"):
                cmd2 += ["-movflags", "+faststart"]
            cmd2.append(str(output_path))

            r2 = subprocess.run(
                cmd2,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=create_no_window,
            )
            _v94_ff_mark_exit(FFMPEG_PATH)
            if r2.returncode == 0 and output_path.is_file():
                out_info = _ffprobe_av_info(output_path)
                out_vdur = out_info.get("video_duration") or out_info.get("format_duration") or 0.0
                if out_info.get("has_video") and out_vdur >= 60.0:
                    logger(
                        f"    ✅ FALLBACK ULTRAFAST XONG: {duration:.2f}s → "
                        f"{out_vdur:.2f}s | {time.perf_counter()-t0:.2f}s"
                    )
                    return out_vdur

        logger("❌ Không thể tạo video >=60s bằng cơ chế tail-prepend.")
        return 0.0
    except Exception as e:
        logger(f"❌ Lỗi FAST tail-prepend: {e}")
        return 0.0


def split_video_copy_parallel(input_path: Path, out_dir: Path, part_seconds: int,
                              original_title: str, logger, audio_path: Path = None,
                              duration: float = 0.0):
    """Chỉ dùng cho video >300s. Stream-copy + anti-overlap giống logic hôm qua.

    V8.8 DIRECT-CUT: nếu có audio_path thì cắt THẲNG từ video webm + audio m4a (bỏ hẳn bước
    MUX toàn file). Đã kiểm chứng: gói video giống hệt từng byte và audio nghe được giống hệt
    so với MUX→cắt, vì ffmpeg dùng cùng ts_offset=-ss cho cả 2 input.
    """
    try:
        ensure_dir(out_dir)
        dur = float(duration or 0.0) or _ffprobe_duration_seconds(FFPROBE_PATH, input_path)
        if dur <= 0:
            logger(f"⚠️ Không đọc được thời lượng: {input_path.name}")
            return False
        if part_seconds <= 0:
            logger("⚠️ Cut Seconds không hợp lệ.")
            return False

        num_parts = int(dur // part_seconds)
        if num_parts <= 0:
            logger(f"⚠️ Video quá ngắn để cắt ({dur:.1f}s).")
            return False

        logger(
            f"✂️ Video > 5 phút: cắt {num_parts} phần, "
            f"{part_seconds}s/phần, anti-overlap 2s."
        )

        cmds = []
        outputs = []
        ext = ".mp4" if audio_path else (input_path.suffix or ".mp4")
        anti_overlap_sec = 2.0

        for i in range(num_parts):
            if i == 0:
                # Bỏ 6 giây đầu như logic hôm qua.
                start_sec = 6.0
                actual_duration = max(1.0, part_seconds - 6.0)
            else:
                # Dịch điểm bắt đầu +2s để tránh lặp đoạn/frame giữa 2 part.
                start_sec = i * part_seconds + anti_overlap_sec
                actual_duration = part_seconds

            if start_sec >= dur - 0.05:
                break

            dynamic_name = generate_dynamic_jp_part_name(original_title, i)
            out_file = out_dir / f"{dynamic_name}{ext}"
            if audio_path:
                cmd = [
                    FFMPEG_PATH, "-y", "-loglevel", "error",
                    "-ss", str(start_sec), "-i", str(input_path),
                    "-ss", str(start_sec), "-i", str(audio_path),
                    "-t", str(actual_duration),
                    "-map", "0:v:0", "-map", "1:a:0",
                    "-c", "copy",
                    str(out_file),
                ]
            else:
                cmd = [
                    FFMPEG_PATH, "-y",
                    "-ss", str(start_sec),
                    "-i", str(input_path),
                    "-t", str(actual_duration),
                    "-c", "copy",
                    str(out_file),
                ]
            cmds.append(cmd)
            outputs.append(out_file)

        if not cmds:
            return False

        # V9.0: 15 ffmpeg cùng lúc trên Windows chậm hơn (Defender + tranh đĩa) → giới hạn theo số core.
        max_workers = min(len(cmds), max(4, min(12, os.cpu_count() or 4)))
        failures = []

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            future_map = {
                ex.submit(
                    subprocess.run, cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=0x08000000 if os.name == "nt" else 0,
                ): idx
                for idx, cmd in enumerate(cmds)
            }
            done = 0
            for fut in as_completed(future_map):
                idx = future_map[fut]
                result = fut.result()
                done += 1
                if (
                    result.returncode != 0
                    or not outputs[idx].is_file()
                    or outputs[idx].stat().st_size <= 0
                ):
                    failures.append(idx + 1)
                if done % 2 == 0 or done == len(cmds):
                    logger(f"    -> {done}/{len(cmds)}")
        _v94_ff_mark_exit(FFMPEG_PATH)

        if not failures and V94_VALIDATE_MUX_STRICT:
            # Review V9.4.1: nhánh >300s (DIRECT-CUT) không đi qua bước mux nên phải kiểm từng part: có cả
            # video + audio và hai luồng dài tương đương (input cắt cụt → part có v=79s a=100s → lỗi).
            def _chk(i_p):
                inf = _ffprobe_av_info(outputs[i_p])
                vd = float(inf.get("video_duration") or 0)
                ad = float(inf.get("audio_duration") or 0)
                ok_ = bool(inf.get("has_video") and inf.get("has_audio") and vd > 0 and ad > 0
                           and abs(vd - ad) <= max(0.6, 0.02 * max(vd, ad)))
                return i_p, ok_, vd, ad
            with ThreadPoolExecutor(max_workers=max_workers) as ex2:
                for i_p, ok_, vd, ad in ex2.map(_chk, range(len(outputs))):
                    if not ok_:
                        failures.append(i_p + 1)
                        logger(f"    ❌ Part {i_p + 1} không hợp lệ: video={vd:.1f}s audio={ad:.1f}s")

        if failures:
            logger(f"❌ Cắt lỗi ở part: {failures}")
            return False

        logger("✅ Cắt xong; chống trùng + random title Part đã áp dụng.")
        return True
    except Exception as e:
        logger(f"❌ Lỗi cắt video: {e}")
        return False

# ================== V6 ZERO-PREFLIGHT DIRECT HTTP ================== #
def _get_direct_media_pool():
    global _DIRECT_MEDIA_POOL
    if urllib3 is None:
        return None
    if _DIRECT_MEDIA_POOL is None:
        with _DIRECT_MEDIA_POOL_LOCK:
            if _DIRECT_MEDIA_POOL is None:
                _kw = {"ssl_context": _shared_ssl_context()} if _shared_ssl_context() else {}
                _DIRECT_MEDIA_POOL = urllib3.PoolManager(
                    num_pools=40, maxsize=160, block=True,
                    timeout=urllib3.Timeout(connect=0.35, read=6.0),
                    retries=False, **_kw
                )
    return _DIRECT_MEDIA_POOL


# ================== V9.6 HOST POOL / PRECONNECT / HTTP2 ================== #
_V96_LOCK = threading.Lock()
_V96_REDIRECT = {}          # host gốc → (host đích, t_seen)
_V96_WARM_HOSTS = {}        # host đích → t_last_use (keepalive tới khi quá TTL)
_V96_KEEPALIVE_STARTED = [False]
_V96_STATS = {"redirect_skip": 0, "redirect_skip_fail": 0, "preconnect": 0}
_V96_HOST_SUFFIX = "googlevideo.com"      # chỉ nhớ/giữ nóng host media (test có thể đổi)


def _v96_host(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(str(url)).hostname or "").lower()
    except Exception:
        return ""


def _v96_swap_host(url: str, host: str) -> str:
    u = urllib.parse.urlsplit(str(url))
    netloc = host + (f":{u.port}" if u.port else "")
    return urllib.parse.urlunsplit((u.scheme, netloc, u.path, u.query, u.fragment))


def _v96_remember_redirect(src_url: str, dst_url: str):
    a, b = _v96_host(src_url), _v96_host(dst_url)
    if not a or not b or a == b or not b.endswith(_V96_HOST_SUFFIX):
        return
    with _V96_LOCK:
        _V96_REDIRECT[a] = (b, time.time())
        _V96_WARM_HOSTS[b] = time.time()
    _v96_start_keepalive()


def _v96_predicted_url(url: str):
    """URL với host đích đã biết (bỏ 302) hoặc None."""
    if not V96_HOST_POOL:
        return None
    a = _v96_host(url)
    with _V96_LOCK:
        hit = _V96_REDIRECT.get(a)
    if not hit or time.time() - hit[1] > float(V96_HOST_POOL_TTL_SEC):
        return None
    return _v96_swap_host(url, hit[0])


def _v96_mark_host_used(url: str):
    h = _v96_host(url)
    if h.endswith(_V96_HOST_SUFFIX):
        with _V96_LOCK:
            _V96_WARM_HOSTS[h] = time.time()
        _v96_start_keepalive()


def _v96_preconnect(url_or_host: str, pool=None, timeout: float = 1.5) -> dict:
    """Mở 1 kết nối (TCP+TLS) tới host của URL và trả vào pool (GET sau dùng lại). Đo riêng DNS / TCP+TLS."""
    u = urllib.parse.urlsplit(url_or_host if "://" in str(url_or_host) else f"https://{url_or_host}")
    host, scheme = (u.hostname or "").lower(), (u.scheme or "https")
    port = u.port or (443 if scheme == "https" else 80)
    d = {"host": host, "dns_ms": None, "tls_ms": None, "ok": False}
    pool = pool or _get_direct_media_pool()
    if pool is None or not host:
        return d
    try:
        t = time.perf_counter()
        socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
        d["dns_ms"] = round((time.perf_counter() - t) * 1000.0, 1)
        hp = pool.connection_from_host(host, port, scheme)
        conn = hp._get_conn()
        try:
            if getattr(conn, "sock", None) is None:
                conn.timeout = timeout
                t = time.perf_counter()
                conn.connect()
                d["tls_ms"] = round((time.perf_counter() - t) * 1000.0, 1)
            else:
                d["tls_ms"] = 0.0          # đã có kết nối nóng trong pool
            d["ok"] = True
        finally:
            hp._put_conn(conn)
        with _V96_LOCK:
            _V96_STATS["preconnect"] += 1
    except Exception as e:
        d["error"] = type(e).__name__
    return d


def _v96_keepalive_worker():
    while True:
        time.sleep(max(5.0, float(V96_HOST_KEEPALIVE_SEC)))
        if _v94_jobs_active() > 0:
            continue                      # đang có job tải → kết nối tự nóng, không chen request
        with _V96_LOCK:
            hosts = [h for h, t in _V96_WARM_HOSTS.items() if time.time() - t <= float(V96_HOST_POOL_TTL_SEC)]
            for h in list(_V96_WARM_HOSTS):
                if h not in hosts:
                    _V96_WARM_HOSTS.pop(h, None)
        pool = _get_direct_media_pool()
        for h in hosts[:4]:
            try:
                r = pool.request("HEAD", f"https://{h}/generate_204",
                                 headers={"Connection": "keep-alive"}, redirect=False, retries=False,
                                 timeout=urllib3.Timeout(connect=0.8, read=0.8), preload_content=True)
                TELEM.span("-", "v96_keepalive", host=h[:12] + "***", status=int(r.status))
            except Exception as e:
                TELEM.span("-", "v96_keepalive", host=h[:12] + "***", error=type(e).__name__)


def _v96_start_keepalive():
    if not V96_HOST_POOL or float(V96_HOST_KEEPALIVE_SEC) <= 0:
        return
    with _V96_LOCK:
        if _V96_KEEPALIVE_STARTED[0]:
            return
        _V96_KEEPALIVE_STARTED[0] = True
    threading.Thread(target=_v96_keepalive_worker, name="V96HostKeepalive", daemon=True).start()


_V96_H2_CLIENT = [None]


def _v96_h2_client():
    """httpx.Client(http2=True) dùng chung (thread-safe). None nếu chưa cài httpx[http2]."""
    if _V96_H2_CLIENT[0] is not None:
        return _V96_H2_CLIENT[0] or None
    try:
        import httpx  # noqa: F401
        import h2  # noqa: F401
        _V96_H2_CLIENT[0] = httpx.Client(http2=True, follow_redirects=False,
                                         timeout=httpx.Timeout(connect=1.0, read=6.0, write=6.0, pool=5.0),
                                         limits=httpx.Limits(max_connections=64, max_keepalive_connections=32))
    except Exception:
        _V96_H2_CLIENT[0] = False
    return _V96_H2_CLIENT[0] or None


def _v96_download_h2(url: str, temp_dir: Path, filename: str, logger, min_bytes: int, expected: int,
                     n: int, info: dict) -> bool:
    """Tải bằng httpx HTTP/2: follow 302 tự tay, rồi N Range song song (n=1 → 1 GET) trên cùng client.
    Lỗi 403/410/429 ở bất kỳ đoạn nào → False (caller quay về 1-GET urllib3)."""
    cl = _v96_h2_client()
    if cl is None:
        info["fail_reason"] = "no_httpx"
        return False
    ensure_dir(temp_dir)
    temp_file = temp_dir / filename
    t0 = time.perf_counter()
    hdr = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154.0.0.0 Safari/537.36",
           "Accept": "*/*", "Accept-Encoding": "identity"}
    cur = str(url)
    try:
        # follow redirect bằng 1 request Range nhỏ (đồng thời cho TTFB + xác nhận 206 + size thật)
        total = int(expected or 0)
        for hop in range(DIRECT_MAX_REDIRECTS + 1):
            with cl.stream("GET", cur, headers={**hdr, "Range": "bytes=0-0"}) as r:
                info["status"] = r.status_code
                info["http_version"] = r.http_version
                if r.status_code in (301, 302, 303, 307, 308):
                    loc = r.headers.get("Location")
                    if not loc:
                        info["fail_reason"] = f"HTTP_{r.status_code}_redirect"; return False
                    cur = urllib.parse.urljoin(cur, loc)
                    continue
                if r.status_code == 206:
                    cr = str(r.headers.get("Content-Range") or "")
                    if "/" in cr and cr.rsplit("/", 1)[1].strip().isdigit():
                        total = int(cr.rsplit("/", 1)[1])
                    r.read()
                    break
                info["fail_reason"] = f"HTTP_{r.status_code}"; return False
        if cur != str(url):
            _v96_remember_redirect(url, cur)
        info["ttfb_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
        info["first_byte_ns"] = time.perf_counter_ns()
        if total <= 0:
            info["fail_reason"] = "no_total"; return False
        n = max(1, min(int(n), max(1, total // RANGE_MIN_PART_BYTES)))
        part = (total + n - 1) // n
        segs = [(i * part, min(total - 1, (i + 1) * part - 1)) for i in range(n)]
        err = {}
        fh = open(temp_file, "w+b"); fh.truncate(total)
        lock = threading.Lock()

        def _one(seg):
            a, b = seg
            try:
                with cl.stream("GET", cur, headers={**hdr, "Range": f"bytes={a}-{b}"}) as r:
                    if r.status_code != 206:
                        err[seg] = f"HTTP_{r.status_code}"; return
                    pos = a
                    for chunk in r.iter_bytes(256 * 1024):
                        with lock:
                            fh.seek(pos); fh.write(chunk)
                        pos += len(chunk)
                    if pos != b + 1:
                        err[seg] = f"short_{pos}/{b + 1}"
            except Exception as e:
                err[seg] = f"EXC:{type(e).__name__}"
        with ThreadPoolExecutor(max_workers=n) as ex:
            list(ex.map(_one, segs))
        fh.close()
        if err:
            info["fail_reason"] = next(iter(err.values()))
            try: temp_file.unlink()
            except OSError: pass
            return False
        size = temp_file.stat().st_size
        el = max(1e-3, time.perf_counter() - t0)
        info.update({"bytes": size, "elapsed_ms": round(el * 1000.0, 2), "engine": f"h2x{n}"})
        if size != total or size < min_bytes:
            info["fail_reason"] = f"size_{size}/{total}"; return False
        logger(f"    [H2-RANGE{n}] ✅ {size/1048576:.2f}MB | {info.get('http_version', '?')} | TTFB={info['ttfb_ms']:.0f}ms | "
               f"{el:.3f}s | ≈{size*8/1e6/el:.0f}Mbps")
        return True
    except Exception as e:
        info["fail_reason"] = f"EXC:{type(e).__name__}"
        try:
            if temp_file.exists(): temp_file.unlink()
        except OSError: pass
        return False

def _download_direct_single_get(url: str, temp_dir: Path, filename: str, logger,
                                min_bytes: int, expected_bytes: int = 0, info: dict = None) -> bool:
    """One GET, zero HEAD/Range preflight. Optimized for small progressive itag 18.
    V9.5: info (tuỳ chọn) nhận ttfb_ms / first_byte_ns / bytes / elapsed_ms / status / fail_reason."""
    info = info if isinstance(info, dict) else {}
    pool = _get_direct_media_pool()
    if pool is None:
        info["fail_reason"] = "no_pool"
        return False
    ensure_dir(temp_dir)
    temp_file = temp_dir / filename
    try:
        if temp_file.exists():
            temp_file.unlink()
    except OSError:
        return False

    t0 = time.perf_counter()
    first_byte = None
    total = 0
    resp = None
    current_url = str(url)
    last_progress_log = t0
    predicted = _v96_predicted_url(url)           # V9.6: host đích đã biết → bỏ 1 RTT 302
    if predicted:
        current_url = predicted
        info["redirect_skipped"] = True
    logger(
        f"    [DIRECT-1GET] 🚀 START | {filename} | "
        f"expected≈{int(expected_bytes or 0)/1048576:.2f}MB" + (" | host đích đã nhớ (bỏ 302)" if predicted else "")
    )
    try:
        # urllib3 with retries=False does not reliably follow 30x redirects.
        # Fresh GoogleVideo URLs can legitimately return 302 first, so follow it ourselves
        # without doing a HEAD/Range preflight. This keeps the hot path to a pure GET chain.
        for hop in range(DIRECT_MAX_REDIRECTS + 1):
            resp = pool.request(
                "GET", current_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154.0.0.0 Safari/537.36",
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                    "Connection": "keep-alive",
                },
                preload_content=False,
                decode_content=False,
                redirect=False,
                timeout=urllib3.Timeout(connect=0.55, read=2.0),
                retries=False,
            )
            status = int(getattr(resp, "status", 0) or 0)
            if hop == 0:
                info["first_resp_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)   # RTT request đầu (302 hoặc 200)
            if predicted and hop == 0 and status not in (200, 206):
                # host đích đã nhớ không còn đúng → về URL gốc (1 lần, không retry thêm)
                try: resp.release_conn()
                except Exception: pass
                resp = None
                with _V96_LOCK:
                    _V96_STATS["redirect_skip_fail"] += 1
                    _V96_REDIRECT.pop(_v96_host(url), None)
                info["redirect_skipped"] = False
                predicted = None
                current_url = str(url)
                logger(f"    [DIRECT-1GET] ↩ host đích đã nhớ trả HTTP {status} → dùng URL gốc")
                continue
            if status in (301, 302, 303, 307, 308):
                location = str((getattr(resp, "headers", {}) or {}).get("Location") or "").strip()
                if not location or hop >= DIRECT_MAX_REDIRECTS:
                    logger(f"    [DIRECT-1GET] ⚠️ HTTP {status} redirect không hợp lệ → fallback engine")
                    return False
                try:
                    resp.release_conn()
                except Exception:
                    pass
                resp = None
                current_url = urllib.parse.urljoin(current_url, location)
                info["redirect_hops"] = hop + 1
                if hop == 0:
                    logger(f"    [DIRECT-1GET] ↪ HTTP {status} → follow redirect trực tiếp")
                    if V96_HOST_POOL:
                        _pc = _v96_preconnect(current_url, pool)   # đo DNS/TLS tới host đích; kết nối vào pool
                        info["dns_ms"], info["tls_ms"] = _pc.get("dns_ms"), _pc.get("tls_ms")
                continue
            break
        if V96_HOST_POOL and resp is not None:
            if current_url != str(url) and not predicted:
                _v96_remember_redirect(url, current_url)
            _v96_mark_host_used(current_url)
            with _V96_LOCK:
                if predicted:
                    _V96_STATS["redirect_skip"] += 1

        status = int(getattr(resp, "status", 0) or 0) if resp is not None else 0
        info["status"] = status
        if status not in (200, 206):
            info["fail_reason"] = f"HTTP_{status}"
            logger(f"    [DIRECT-1GET] ⚠️ HTTP {status} → fallback engine")
            return False

        _deadline = t0 + float(V94_DIRECT_1GET_DEADLINE_SEC or 0) if float(V94_DIRECT_1GET_DEADLINE_SEC or 0) > 0 else 0
        with open(temp_file, "wb", buffering=1024 * 1024) as f:
            while True:
                if _deadline and time.perf_counter() > _deadline:
                    raise TimeoutError("direct_1get_deadline")
                chunk = resp.read(DIRECT_READ_CHUNK)
                if not chunk:
                    break
                if first_byte is None:
                    first_byte = time.perf_counter()
                    info["first_byte_ns"] = time.perf_counter_ns()
                f.write(chunk)
                total += len(chunk)
                now_p = time.perf_counter()
                if now_p - last_progress_log >= 1.0:
                    elapsed_p = max(0.001, now_p - t0)
                    mbps_p = (total * 8.0 / 1_000_000.0) / elapsed_p
                    logger(
                        f"    [DIRECT-1GET] ⏬ {filename} | "
                        f"{total/1048576:.2f}/{int(expected_bytes or 0)/1048576:.2f}MB | "
                        f"{elapsed_p:.1f}s | ≈{mbps_p:.0f}Mbps"
                    )
                    last_progress_log = now_p

        elapsed = max(0.0001, time.perf_counter() - t0)
        info.update({"bytes": total, "elapsed_ms": round(elapsed * 1000.0, 2),
                     "ttfb_ms": round(((first_byte or time.perf_counter()) - t0) * 1000.0, 2),
                     "xfer_ms": round((time.perf_counter() - (first_byte or t0)) * 1000.0, 2),
                     "final_host": _v96_host(current_url)[:14] + "***"})
        if total < min_bytes:
            info["fail_reason"] = f"too_small_{total}"
            logger(f"    [DIRECT-1GET] ⚠️ File quá nhỏ: {total} bytes")
            try: temp_file.unlink()
            except OSError: pass
            return False

        if expected_bytes and total + 4096 < int(expected_bytes * 0.96):
            info["fail_reason"] = f"short_{total}/{expected_bytes}"
            logger(
                f"    [DIRECT-1GET] ⚠️ Thiếu dữ liệu: {total/1048576:.2f}MB / "
                f"expected≈{expected_bytes/1048576:.2f}MB → fallback"
            )
            try: temp_file.unlink()
            except OSError: pass
            return False

        ttfb_ms = ((first_byte or time.perf_counter()) - t0) * 1000.0
        mbps = (total * 8.0 / 1_000_000.0) / elapsed
        logger(
            f"    [DIRECT-1GET] ✅ ZERO-PREFLIGHT | {total/1048576:.2f}MB | "
            f"TTFB={ttfb_ms:.0f}ms | {elapsed:.3f}s | ≈{mbps:.0f}Mbps"
            + (f" | req1={info.get('first_resp_ms')}ms" if info.get("first_resp_ms") is not None else "")
            + (f" dns={info.get('dns_ms')}ms tls={info.get('tls_ms')}ms" if info.get("tls_ms") is not None else "")
            + (" | 302-skip" if info.get("redirect_skipped") else "")
            + f" | xfer={info.get('xfer_ms')}ms"
        )
        return True
    except Exception as e:
        info["fail_reason"] = f"EXC:{type(e).__name__}"
        logger(f"    [DIRECT-1GET] ⚠️ {type(e).__name__} → fallback engine")
        try:
            if temp_file.exists(): temp_file.unlink()
        except OSError:
            pass
        return False
    finally:
        if resp is not None:
            try:
                resp.release_conn()
            except Exception:
                pass



def _range_progress_key(path: Path) -> str:
    try:
        return str(path.resolve())
    except Exception:
        return str(path)


def _range_progress_begin(path: Path, total: int):
    with _RANGE_PROGRESS_LOCK:
        _RANGE_PROGRESS[_range_progress_key(path)] = [0, int(total or 0)]


def _range_progress_add(path: Path, amount: int):
    with _RANGE_PROGRESS_LOCK:
        row = _RANGE_PROGRESS.get(_range_progress_key(path))
        if row is not None:
            row[0] += int(amount or 0)


def _range_progress_mb(path: Path):
    if not path:
        return None
    with _RANGE_PROGRESS_LOCK:
        row = _RANGE_PROGRESS.get(_range_progress_key(path))
        if row is None:
            return None
        return float(row[0]) / 1048576.0


def _range_progress_end(path: Path):
    with _RANGE_PROGRESS_LOCK:
        _RANGE_PROGRESS.pop(_range_progress_key(path), None)


def _range_worker_count(total_bytes: int, media_kind: str = "video", small_video_workers: int = 0) -> int:
    """V8.6 fan-out: avoid over-fanout on tiny Shorts; use measured 6/8-worker sweet spots."""
    # V9.4: ghi đè cố định cho thí nghiệm sweep (bench_v93.py --workers ...). 0 = heuristic V9.3.
    _ov = V94_RANGE_WORKERS_AUDIO if media_kind == "audio" else V94_RANGE_WORKERS_VIDEO
    if int(_ov or 0) > 0:
        return max(1, min(int(V94_RANGE_MAX_WORKERS), int(_ov)))
    mb = max(0.0, float(total_bytes or 0) / 1048576.0)
    # V9.4.1: số worker riêng cho video <= 4MiB (A/B "6,11"); 0 = giữ heuristic cũ (6). Video lớn và audio
    # không đổi. Lý do: luật "<=4MiB → 6" đo ở V8.6, TRƯỚC khi V9.1 dùng chung SSLContext (bỏ 30-100ms CPU
    # mỗi kết nối TLS), nên kết luận "nhiều worker chậm hơn" có thể không còn đúng — phải A/B lại live.
    if media_kind != "audio" and mb <= 4 and int(small_video_workers or 0) > 0:
        return max(1, min(int(V94_RANGE_MAX_WORKERS), int(small_video_workers)))
    if media_kind == "audio":
        # V9.3: log 21:04 — audio 11.4MB/10 worker mất 3.12s (≈3Mbps/kết nối, GVS giới hạn
        # tốc độ mỗi kết nối audio) trong khi video 129MB xong ở 2.33s → audio thành nút cổ chai.
        # Chia audio mịn hơn (~0.45MB/part) để nó luôn xong TRƯỚC video.
        if mb <= 1: return 4
        if mb <= 3: return 8
        if mb <= 6: return 14
        if mb <= 12: return 24
        if mb <= 20: return 32
        return 40
    # V8.6 measured tuning: 12 workers on ~7MiB was slower than the earlier 8-worker profile.
    # Keep enough parallelism to beat the old 3-worker path without paying 12 TLS/HTTP starts.
    if mb <= 4: return 6
    if mb <= 12: return 8
    if mb <= 20: return 10
    if mb <= 40: return 16
    if mb <= 64: return 20
    if mb <= 80: return 24
    if mb <= 150: return 32
    if mb <= 240: return 36
    return 40


def _range_follow_redirect_probe(pool, url: str, max_redirects: int = 4, read_timeout: float = 3.0):
    """Resolve the media CDN once with Range 0-0 and verify byte-range support."""
    current = str(url)
    for hop in range(max_redirects + 1):
        resp = None
        try:
            resp = pool.request(
                "GET", current,
                headers={
                    "Range": "bytes=0-0",
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                    "Connection": "keep-alive",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154.0.0.0 Safari/537.36",
                },
                preload_content=False, decode_content=False, redirect=False,
                timeout=urllib3.Timeout(connect=0.70, read=max(0.5, float(read_timeout))), retries=False,
            )
            status = int(getattr(resp, "status", 0) or 0)
            if status in (301, 302, 303, 307, 308):
                loc = str((getattr(resp, "headers", {}) or {}).get("Location") or "").strip()
                try: resp.read(64)
                except Exception: pass
                if not loc or hop >= max_redirects:
                    return None, status, hop
                current = urllib.parse.urljoin(current, loc)
                continue
            if status == 206:
                try: resp.read(8)
                except Exception: pass
                return current, status, hop
            return None, status, hop
        except Exception:
            return None, -1, hop
        finally:
            if resp is not None:
                try: resp.release_conn()
                except Exception: pass
    return None, -1, max_redirects


def _download_one_range(pool, final_url: str, temp_file: Path, start: int, end: int,
                        stop_event: threading.Event, read_timeout: float = 6.0,
                        follow_redirects: bool = False):
    if stop_event.is_set():
        raise RuntimeError("cancelled")
    resp = None
    need = end - start + 1
    got = 0
    current_url = str(final_url)
    try:
        for hop in range(DIRECT_MAX_REDIRECTS + 1):
            if stop_event.is_set():
                raise RuntimeError("cancelled")
            resp = pool.request(
                "GET", current_url,
                headers={
                    "Range": f"bytes={start}-{end}",
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                    "Connection": "keep-alive",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154.0.0.0 Safari/537.36",
                },
                preload_content=False, decode_content=False, redirect=False,
                timeout=urllib3.Timeout(connect=0.65, read=max(0.5, float(read_timeout))), retries=False,
            )
            status = int(getattr(resp, "status", 0) or 0)
            if follow_redirects and status in (301, 302, 303, 307, 308):
                loc = str((getattr(resp, "headers", {}) or {}).get("Location") or "").strip()
                try: resp.release_conn()
                except Exception: pass
                resp = None
                if not loc or hop >= DIRECT_MAX_REDIRECTS:
                    raise OSError(f"HTTP_{status}_redirect")
                current_url = urllib.parse.urljoin(current_url, loc)
                continue
            if status != 206:
                raise OSError(f"HTTP_{status}")
            break
        if resp is None:
            raise OSError("no_response")
        with open(temp_file, "r+b", buffering=0) as f:
            f.seek(start)
            while got < need and not stop_event.is_set():
                chunk = resp.read(min(RANGE_READ_CHUNK, need - got))
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                _range_progress_add(temp_file, len(chunk))
        if got != need:
            raise OSError(f"short_range_{got}/{need}")
        return got
    finally:
        if resp is not None:
            try: resp.release_conn()
            except Exception: pass


# V8.7 STEAL-TURBO tuning.
# Log V8.6 (72.8MB): 0→1.75s probe redirect x2 nối tiếp + TLS lạnh; 1.75→3.29s tải 67MB
# (~350Mbps, băng thông OK); 3.29→5.35s chỉ còn 4MB nhưng mất 2s vì chia tĩnh 24 phần
# bằng nhau → vài connection chậm giữ cả file. V8.7: probe = luôn tải phần đầu (không phí
# round-trip), worker rảnh "cướp" nửa sau của segment còn nhiều nhất, segment đứng im thì
# bị giành lại toàn bộ; lỗi 1 segment thì tải lại phần còn thiếu thay vì bỏ cả file.
RANGE_STEAL_READ_CHUNK = 128 * 1024
RANGE_STEAL_MIN_BYTES = 256 * 1024     # không chẻ nhỏ hơn mức này (tránh tốn TTFB)
RANGE_STALL_SEC = 0.70                 # segment không nhận byte nào trong 0.7s → giành lại
RANGE_SEG_MAX_RETRY = 4
RANGE_HEDGE_ETA_SEC = 0.45             # đuôi còn >0.45s mới xong (connection rùa) → tải song song bản mới
RANGE_HEDGE_MIN_AGE = 0.30
# V8.9 MN-RACE: URL GoogleVideo chứa tham số mn=sn-A,sn-B = danh sách replica cache.
# Chữ ký URL không gắn với host → cùng URL dùng được trên rr{N}---sn-B (cơ chế fallback
# host của chính YouTube player). Đợt request đầu chia 2 replica, replica nào trả byte đầu
# nhanh hơn sẽ nhận các request sau. Replica phụ lỗi 1 lần → tắt 10 phút, không làm fail file.
RANGE_MN_RACE = True
_MN_RACE_BAD_UNTIL = [0.0]
# V9.0 HEDGE: không chia cứng nửa đợt đầu cho replica phụ nữa (log 19:51 replica phụ chậm
# hơn: 1.36s vs 0.13s). Mọi request đi host chính; chỉ khi quá RANGE_HEDGE_DELAY mà chưa có
# header 206 mới bắn thêm 1 request y hệt sang replica phụ, bên nào về trước thắng, bên thua
# bị đóng. → không bao giờ chậm hơn chỉ dùng host chính.
RANGE_HEDGE_DELAY_MIN = 0.15
RANGE_HEDGE_MAX_FRAC = 0.25   # tối đa 25% số worker được hedge cùng lúc (log 19:57: 32/32 hedge → bão kết nối)
_HEDGE_EXEC = ThreadPoolExecutor(max_workers=max(8, int(V94_HEDGE_POOL_MAX)), thread_name_prefix="GVSHedge")


def _gvs_alt_url(url: str) -> str:
    try:
        u = urllib.parse.urlsplit(str(url))
        m = re.match(r"^(rr\d+)---(sn-[a-z0-9-]+)\.googlevideo\.com$", u.hostname or "")
        if not m:
            return ""
        q = urllib.parse.parse_qs(u.query)
        mns = [x.strip() for x in ",".join(q.get("mn", [])).split(",") if x.strip()]
        alt_sn = next((x for x in mns if x != m.group(2) and re.match(r"^sn-[a-z0-9-]+$", x)), "")
        if not alt_sn:
            return ""
        netloc = f"{m.group(1)}---{alt_sn}.googlevideo.com" + (f":{u.port}" if u.port else "")
        return urllib.parse.urlunsplit((u.scheme, netloc, u.path, u.query, u.fragment))
    except Exception:
        return ""


class _RangeSeg:
    __slots__ = ("pos", "end", "last_t", "retries", "taken", "t_start", "got", "h", "first", "gen",
                 "kind", "hmode")

    def __init__(self, pos: int, end: int, retries: int = 0):
        self.pos = int(pos)
        self.end = int(end)
        self.last_t = time.perf_counter()
        self.retries = retries
        self.taken = False  # đã bị worker khác giành lại toàn bộ (stall/chậm)
        self.t_start = self.last_t
        self.got = 0
        self.h = 0          # 0 = host chính, 1 = replica mn phụ
        self.first = False  # thuộc đợt request đầu tiên (chưa biết host nào nhanh)
        self.gen = 0        # số lần đoạn này đã bị giành lại (backoff chống livelock)
        self.kind = "first" # V9.4.1 telemetry: first/split/takeover/retry/ext (engine V9.3 bỏ qua)
        self.hmode = ""     # V9.4.1 telemetry: ""/race/hedge

    def eta(self, now: float) -> float:
        rem = self.end - self.pos + 1
        age = now - self.t_start
        if rem <= 0:
            return 0.0
        if self.got <= 0 or age <= 0:
            return 1e9 if age >= RANGE_STALL_SEC else 0.0
        return rem / (self.got / age)


def _range_request(pool, url: str, start: int, end: int, read_timeout: float, follow: bool):
    """GET Range; tự follow 30x. Trả (resp đang mở ở trạng thái 206, final_url, hops)."""
    current = str(url)
    for hop in range(DIRECT_MAX_REDIRECTS + 1):
        resp = pool.request(
            "GET", current,
            headers={
                "Range": f"bytes={start}-{end}",
                "Accept": "*/*",
                "Accept-Encoding": "identity",
                "Connection": "keep-alive",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154.0.0.0 Safari/537.36",
            },
            preload_content=False, decode_content=False, redirect=False,
            timeout=urllib3.Timeout(connect=0.35, read=max(0.5, float(read_timeout))), retries=False,
        )
        status = int(getattr(resp, "status", 0) or 0)
        if follow and status in (301, 302, 303, 307, 308):
            loc = str((getattr(resp, "headers", {}) or {}).get("Location") or "").strip()
            _range_close_resp(resp, drain=True)
            if not loc or hop >= DIRECT_MAX_REDIRECTS:
                raise OSError(f"HTTP_{status}_redirect")
            current = urllib.parse.urljoin(current, loc)
            continue
        if status != 206:
            _range_close_resp(resp, drain=False)
            raise OSError(f"HTTP_{status}")
        return resp, current, hop
    raise OSError("too_many_redirects")


def _range_close_resp(resp, drain: bool):
    """Body còn dư (segment bị chẻ/giành) → đóng hẳn socket, không trả connection bẩn về pool."""
    if resp is None:
        return
    try:
        if drain:
            resp.drain_conn()
            resp.release_conn()
        else:
            resp.close()
            resp.release_conn()
    except Exception:
        try: resp.close()
        except Exception: pass


def _range_content_total(resp) -> int:
    try:
        cr = str((resp.headers or {}).get("Content-Range") or "")
        if "/" in cr:
            tail = cr.rsplit("/", 1)[1].strip()
            if tail.isdigit():
                return int(tail)
    except Exception:
        pass
    return 0


def _download_http_range_turbo_v93(url: str, temp_dir: Path, filename: str, logger,
                                   min_bytes: int, expected_bytes: int, media_kind: str = "video") -> bool:
    """V9.3 nguyên bản (rollback: V94_RANGE_ENGINE=v93). Size sai → vứt dữ liệu và tải lại."""
    box = [0]
    if _download_http_range_turbo_once(url, temp_dir, filename, logger, min_bytes,
                                       expected_bytes, media_kind, box, False):
        return True
    if box[0] > 0:
        return _download_http_range_turbo_once(url, temp_dir, filename, logger, min_bytes,
                                               box[0], media_kind, [0], True)
    return False


def _download_http_range_turbo_once(url: str, temp_dir: Path, filename: str, logger,
                                    min_bytes: int, expected_bytes: int, media_kind: str,
                                    pending_retry_total, _retry_real: bool) -> bool:
    total = int(expected_bytes or 0)
    pool = _get_direct_media_pool()
    if urllib3 is None or pool is None or total <= 0:
        return False
    ensure_dir(temp_dir)
    temp_file = temp_dir / filename
    try:
        if temp_file.exists():
            temp_file.unlink()
    except OSError:
        return False

    t0 = time.perf_counter()
    is_audio = (media_kind == "audio")
    read_timeout = 1.5 if is_audio else 3.0
    workers = _range_worker_count(total, media_kind)
    min_part_bytes = RANGE_AUDIO_MIN_PART_BYTES if is_audio else RANGE_MIN_PART_BYTES
    workers = max(1, min(workers, max(1, total // min_part_bytes)))
    part = (total + workers - 1) // workers

    # --- V8.8 EAGER: KHÔNG probe nối tiếp. Mọi worker bắn request ngay t=0 lên URL gốc,
    # tự follow redirect song song; worker đầu tiên biết URL cuối sẽ chia sẻ cho các request sau.
    # Content-Range của response 206 đầu tiên dùng để xác minh dung lượng thật.
    segs = []
    s_ = 0
    while s_ < total:
        e_ = min(total - 1, s_ + part - 1)
        segs.append(_RangeSeg(s_, e_))
        segs[-1].first = True
        s_ = e_ + 1
    workers = len(segs)

    try:
        fh = open(temp_file, "w+b")
        fh.truncate(total)
    except OSError as e:
        logger(f"    [RANGE-TURBO] ⚠️ create file: {e}")
        return False

    lock = threading.Lock()
    pending = list(segs)              # segment chưa ai nhận
    active = set()                    # segment đang có worker tải
    state = {"done": 0, "error": None, "closed": False, "steals": 0, "retries": 0,
             "url": str(url), "hops": 0, "ttfb": None, "real_total": 0, "checked": False,
             "ttfb_ema": None, "alt": "", "alt_ok": None, "host_ttfb": [None, None],
             "host_req": [0, 0], "wave": 0, "hedges": 0, "alt_wins": 0, "hedging": 0}
    if (RANGE_MN_RACE and workers >= 4 and time.time() >= _MN_RACE_BAD_UNTIL[0]):
        state["alt"] = _gvs_alt_url(url)

    def _pick_host_locked(initial: bool) -> int:
        if not state["alt"] or state["alt_ok"] is False:
            return 0
        a, b = state["host_ttfb"]
        if state["alt_ok"] and b is not None and (a is None or b < a):
            return 1
        return 0
    finished = threading.Event()

    def _all_done_locked():
        return state["done"] >= total and not pending and not any(s.pos <= s.end for s in active)

    def _take():
        """Lấy segment: pending → chẻ đôi segment còn nhiều nhất → giành segment đứng im."""
        with lock:
            if state["error"] is not None or state["closed"]:
                return None
            if pending:
                seg = pending.pop(0)
                seg.last_t = seg.t_start = time.perf_counter()
                seg.h = _pick_host_locked(initial=(seg.retries == 0 and state["done"] == 0))
                active.add(seg)
                return seg
            now = time.perf_counter()
            ttfb = state["ttfb_ema"] if state["ttfb_ema"] is not None else 0.30
            rates = [x.got / (now - x.t_start) for x in active
                     if x.got > 0 and now - x.t_start > 0.05 and x.pos <= x.end]
            r_avg = (sum(rates) / len(rates)) if rates else 0.0
            # 1) chẻ segment có ETA lâu nhất — chỉ khi đáng hơn chi phí 1 request mới (TTFB)
            best, best_eta = None, 0.0
            for x in active:
                if x.taken or x.end - x.pos + 1 < 2 * RANGE_STEAL_MIN_BYTES:
                    continue
                e_ = x.eta(now)
                if e_ > best_eta:
                    best, best_eta = x, e_
            if best is not None and best_eta > 2.0 * ttfb + 0.10:
                rem = best.end - best.pos + 1
                r_v = best.got / max(1e-3, now - best.t_start) if best.got > 0 else 0.0
                if r_v > 0 and r_avg > 0:
                    # chia sao cho 2 bên về đích cùng lúc: x/r_v = ttfb + (rem-x)/r_avg
                    keep = int(r_v * (ttfb + rem / r_avg) / (1.0 + r_v / r_avg))
                else:
                    keep = rem // 2
                keep = max(RANGE_STEAL_MIN_BYTES, min(rem - RANGE_STEAL_MIN_BYTES, keep))
                mid = best.pos + keep
                new = _RangeSeg(mid, best.end)
                new.h = _pick_host_locked(initial=False)
                best.end = mid - 1
                active.add(new)
                state["steals"] += 1
                return new
            # 2) giành lại đoạn đuôi đứng im / chậm hơn hẳn 1 request mới
            for x in sorted(active, key=lambda y: -y.eta(now)):
                if x.pos > x.end or x.taken:
                    continue
                # V9.2 FIX LIVELOCK: request chưa nhận byte nào chỉ bị coi là kẹt khi đã chờ quá
                # 1.5×TTFB thực đo (+backoff ×2 mỗi lần bị giành). Trước đây host trả byte đầu
                # sau 1.24s bị giành lại ở 0.7s → vòng lặp vô hạn, treo tới timeout 60s.
                if x.got == 0 and (state["ttfb_ema"] is None or
                                   now - x.t_start < max(RANGE_STALL_SEC, 1.5 * ttfb + 0.30) * (2 ** x.gen)):
                    continue
                rem = x.end - x.pos + 1
                fresh_eta = ttfb + (rem / r_avg if r_avg > 0 else 0.0)
                stalled = now - x.last_t >= max(RANGE_STALL_SEC, ttfb + 0.35)
                slow = (now - x.t_start >= RANGE_HEDGE_MIN_AGE and
                        x.eta(now) >= max(RANGE_HEDGE_ETA_SEC, fresh_eta + 0.25))
                if stalled or slow:
                    new = _RangeSeg(x.pos, x.end)
                    new.h = _pick_host_locked(initial=False)
                    new.gen = x.gen + 1
                    x.taken = True
                    x.end = x.pos - 1     # owner cũ sẽ dừng ở lần read kế tiếp
                    active.add(new)
                    state["steals"] += 1
                    return new
            return None

    def _open_hedged(seg: _RangeSeg, start: int, end: int):
        """Hedged request: host chính trước; trễ quá ngưỡng → thêm replica phụ, lấy bên về trước.
        Ngân sách: tối đa 25% số worker được hedge cùng lúc."""
        with lock:
            if seg.h == 1 and (not state["alt"] or state["alt_ok"] is False):
                seg.h = 0
            first_h = seg.h
            urls = [state["url"], state["alt"] if state["alt_ok"] is not False else ""]
            a_ttfb = state["host_ttfb"][first_h]
            delay = max(RANGE_HEDGE_DELAY_MIN, 2.5 * a_ttfb) if a_ttfb is not None else RANGE_HEDGE_DELAY_MIN
            # V9.2 FIRST-WAVE RACE: đợt đầu chưa biết host nào nóng → bắn CẢ 2 host cùng lúc,
            # không chờ (log 20:35: main 1.24s vs alt 0.21s). Bên thua bị đóng ngay sau header.
            race_now = bool(seg.first and seg.retries == 0 and state["alt"] and state["alt_ok"] is not False
                            and state["alt_wins"] == 0 and state["host_ttfb"][0] is None)
            if race_now:
                delay = 0.0
            state["host_req"][first_h] += 1
        other_h = 1 - first_h
        if not urls[other_h]:
            resp, got_url, hops = _range_request(pool, urls[first_h], start, end, read_timeout, follow=True)
            seg.h = first_h
            return resp, got_url, hops, urls[first_h]
        f_first = _HEDGE_EXEC.submit(_range_request, pool, urls[first_h], start, end, read_timeout, True)
        futs = {f_first: first_h}
        done, _ = wait([f_first], timeout=delay) if delay > 0 else (set(), None)
        if done and f_first.exception() is None:
            seg.h = first_h
            return (*f_first.result(), urls[first_h])
        def _try_budget():
            if race_now:
                with lock:
                    state["hedges"] += 1
                    state["host_req"][other_h] += 1
                    state["hedging"] += 1
                return True
            with lock:
                _a, _b = state["host_ttfb"]
                alt_proven = state["alt_wins"] > 0 and _b is not None and (_a is None or _b < _a)
                cap = workers if alt_proven else max(1, int(workers * RANGE_HEDGE_MAX_FRAC))
                ok_ = (not finished.is_set() and bool(urls[other_h]) and state["alt_ok"] is not False and
                       state["hedging"] < cap)
                if ok_:
                    state["hedging"] += 1
                    state["hedges"] += 1
                    state["host_req"][other_h] += 1
                return ok_
        # hết ngân sách → vẫn chờ host chính, nhưng cứ 50ms xin lại (ngân sách mở khi replica đã thắng)
        while not _try_budget():
            if f_first.done() or finished.is_set() or state["alt_ok"] is False:
                seg.h = first_h
                return (*f_first.result(), urls[first_h])      # raise nếu host chính lỗi
            wait([f_first], timeout=0.05)
        try:
            futs[_HEDGE_EXEC.submit(_range_request, pool, urls[other_h], start, end, read_timeout, True)] = other_h
            pending_f = set(futs)
            winner = None
            errors = {}
            while pending_f and winner is None:
                done, pending_f = wait(pending_f, return_when=FIRST_COMPLETED)
                for f in done:
                    e = f.exception()
                    if e is None and winner is None:
                        winner = f
                    elif e is not None:
                        errors[futs[f]] = e
        finally:
            with lock:
                state["hedging"] -= 1

        def _loser_done(ff, lh):
            if ff.exception() is None:
                _range_close_resp(ff.result()[0], drain=False)   # bên thua: đóng ngay
            elif lh == 1:
                with lock:
                    if state["alt_ok"] is not True:
                        state["alt_ok"] = False                  # replica phụ hỏng → thôi hedge
                        _MN_RACE_BAD_UNTIL[0] = time.time() + 600.0
        for f in pending_f:
            f.add_done_callback(lambda ff, lh=futs[f]: _loser_done(ff, lh))
        if 1 in errors:
            with lock:
                if state["alt_ok"] is not True:
                    state["alt_ok"] = False
                    _MN_RACE_BAD_UNTIL[0] = time.time() + 600.0
        if winner is None:
            raise errors.get(0) or errors.get(1) or OSError("hedge_no_winner")
        h = futs[winner]
        seg.h = h
        if h == 1:
            with lock:
                state["alt_wins"] += 1
        resp, got_url, hops = winner.result()
        return resp, got_url, hops, urls[h]

    def _run_seg(seg: _RangeSeg, resp=None, req_end=None):
        try:
            if resp is None:
                with lock:
                    start, end = seg.pos, seg.end
                if start > end:
                    return
                req_end = end
                resp, got_url, hops, cur_url = _open_hedged(seg, start, end)
                rt = _range_content_total(resp)
                with lock:
                    _hn = time.perf_counter() - seg.t_start
                    _ht = state["host_ttfb"][seg.h]
                    state["host_ttfb"][seg.h] = _hn if _ht is None else 0.7 * _ht + 0.3 * _hn
                    if seg.h == 1:
                        state["alt_ok"] = True
                    if seg.h == 0 and got_url != cur_url and state["url"] == cur_url:
                        state["url"] = got_url          # chia sẻ URL cuối sau redirect
                        state["hops"] = hops
                    _now = time.perf_counter()
                    if state["ttfb"] is None:
                        state["ttfb"] = _now - t0
                    _req_ttfb = _now - seg.t_start
                    state["ttfb_ema"] = (_req_ttfb if state["ttfb_ema"] is None
                                         else 0.7 * state["ttfb_ema"] + 0.3 * _req_ttfb)
                    if rt > 0 and rt != total and not state["checked"]:
                        state["checked"] = True
                        state["real_total"] = rt
                        state["error"] = OSError(f"size_mismatch_{rt}")
                        finished.set()
                    elif rt > 0:
                        state["checked"] = True
            while True:
                with lock:
                    if state["closed"] or seg.pos > seg.end:
                        break
                    want = seg.end - seg.pos + 1
                chunk = resp.read(min(RANGE_STEAL_READ_CHUNK, want))
                if not chunk:
                    raise OSError("short_range")
                with lock:
                    if state["closed"]:
                        break
                    allowed = min(len(chunk), seg.end - seg.pos + 1)
                    if allowed <= 0:
                        break
                    fh.seek(seg.pos)
                    fh.write(chunk[:allowed] if allowed < len(chunk) else chunk)
                    seg.pos += allowed
                    seg.got += allowed
                    seg.last_t = time.perf_counter()
                    state["done"] += allowed
                    _range_progress_add(temp_file, allowed)
            with lock:
                # chỉ trả connection về pool khi body đã đọc hết; bị chẻ/giành → đóng socket
                clean = req_end is not None and seg.pos == req_end + 1
            _range_close_resp(resp, drain=clean)
            resp = None
        except Exception as e:
            _range_close_resp(resp, drain=False)
            resp = None
            with lock:
                if (seg.h == 1 and seg.got == 0 and seg.pos <= seg.end and not seg.taken
                        and not state["closed"]):
                    # replica phụ hỏng → tắt replica, trả segment về host chính, KHÔNG tính lỗi
                    if state["alt_ok"] is not True:
                        state["alt_ok"] = False
                        _MN_RACE_BAD_UNTIL[0] = time.time() + 600.0
                    retry = _RangeSeg(seg.pos, seg.end, seg.retries)
                    retry.h = 0
                    seg.end = seg.pos - 1
                    pending.insert(0, retry)
                elif seg.pos <= seg.end and not seg.taken and not state["closed"]:
                    if str(e).startswith("HTTP_4"):
                        state["error"] = e        # 403/404/416: URL hỏng → fallback ngay, không spam retry
                    elif seg.retries + 1 > RANGE_SEG_MAX_RETRY:
                        state["error"] = e
                    else:
                        # tải lại phần còn thiếu, ưu tiên đầu hàng đợi
                        retry = _RangeSeg(seg.pos, seg.end, seg.retries + 1)
                        seg.end = seg.pos - 1
                        pending.insert(0, retry)
                        state["retries"] += 1
        finally:
            with lock:
                active.discard(seg)
                if state["error"] is not None or _all_done_locked():
                    finished.set()

    def _worker():
        while not finished.is_set():
            seg = _take()
            if seg is None:
                finished.wait(0.03)   # chờ có segment để chẻ / segment bị stall
                continue
            _run_seg(seg)

    _range_progress_begin(temp_file, total)
    logger(
        f"    [RANGE-TURBO] 🚀 START {media_kind.upper()} | {total/1048576:.2f}MB | "
        f"workers={workers} | part≈{part/1048576:.2f}MB | EAGER+STEAL"
    )
    ex = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"YT{media_kind.capitalize()}Range")
    last_log = time.perf_counter()
    ok = False
    try:
        for _ in range(workers):
            ex.submit(_worker)
        while not finished.wait(0.10):
            now = time.perf_counter()
            if now - last_log >= 1.0 and now - t0 >= 1.0:   # V9.2: không log trong 1s đầu
                with lock:
                    live_done = state["done"]
                elapsed = max(0.001, now - t0)
                logger(
                    f"    [RANGE-TURBO] ⏬ {filename} | {live_done/1048576:.2f}/{total/1048576:.2f}MB | "
                    f"{elapsed:.2f}s | ≈{live_done*8.0/1e6/elapsed:.0f}Mbps"
                )
                last_log = now
            if now - t0 > 60.0:
                with lock:
                    state["error"] = TimeoutError("range_total_timeout")
                break
        with lock:
            err = state["error"]
            state["closed"] = True   # mọi thread còn treo sẽ không ghi thêm
            done = state["done"]
            steals, retries = state["steals"], state["retries"]
            alt_info = ""
            if state["alt"]:
                _a, _b = state["host_ttfb"]
                alt_info = (f" | hedge={state['hedges']} alt-win={state['alt_wins']}"
                            f" ({'OK' if state['alt_ok'] else ('FAIL' if state['alt_ok'] is False else '-')})"
                            f" main={(_a or 0):.2f}s/{state['host_req'][0]}req"
                            f" alt={(_b or 0):.2f}s/{state['host_req'][1]}req")
            real_total, hops, ttfb = state["real_total"], state["hops"], state["ttfb"]
        try:
            fh.close()
        except OSError:
            pass
        if err is not None and real_total > 0 and real_total != total and not _retry_real:
            logger(f"    [RANGE-TURBO] ℹ️ size thật {real_total/1048576:.2f}MB (ước tính {total/1048576:.2f}MB) → tải lại đúng size")
            pending_retry_total[0] = real_total
            return False
        if err is not None:
            logger(f"    [RANGE-TURBO] ⚠️ {type(err).__name__}: {err} → fallback")
            return False
        size = temp_file.stat().st_size if temp_file.exists() else 0
        if done != total or size != total or size < min_bytes:
            logger(f"    [RANGE-TURBO] ⚠️ size mismatch {done}/{size}/{total} → fallback")
            return False
        elapsed = max(0.001, time.perf_counter() - t0)
        logger(
            f"    [RANGE-TURBO] ✅ HOÀN TẤT {media_kind.upper()} | {total/1048576:.2f}MB | "
            f"{elapsed:.3f}s | ≈{total*8.0/1e6/elapsed:.0f}Mbps | workers={workers} | "
            f"steal={steals} | retry={retries} | ttfb={(ttfb or 0):.2f}s" +
            (f" | redirect=x{hops}" if hops else "") + alt_info
        )
        ok = True
        return True
    finally:
        with lock:
            state["closed"] = True
        finished.set()
        try:
            fh.close()
        except OSError:
            pass
        ex.shutdown(wait=False, cancel_futures=True)
        _range_progress_end(temp_file)
        if not ok:
            try:
                if temp_file.exists(): temp_file.unlink()
            except OSError:
                pass


# ================== V9.4 STREAMING-AWARE RANGE ENGINE ================== #
# Khác V9.3 (giữ nguyên ở trên dưới tên *_v93 để rollback bằng V94_RANGE_ENGINE=v93):
#  1) Mỗi response 206 được kiểm tra Content-Range: start == start đã xin, end == min(end xin, total-1),
#     total nhất quán giữa mọi response. Sai → lỗi cứng (không bao giờ ghi byte sai vị trí).
#  2) Header 206 ĐẦU TIÊN cho size thật → nếu khác ước tính: co (cắt segment/truncate) hoặc giãn
#     (thêm segment cho phần thiếu + thêm worker) NGAY TẠI CHỖ. Không vứt byte đã tải, không preflight.
#  3) Size không biết: 1 request Range đầu (512KiB audio / 2MiB video) → biết total → fan-out.
#  4) 403/410/429/200-bỏ-Range → lỗi cứng có lý do (info["fail_reason"]) để caller KHÔNG gọi aria2
#     với cùng signed URL mà đi SAFE-REFRESH. 416 có "bytes */N" → dùng làm bằng chứng size.
#  5) FIRST-WAVE-RACE giới hạn V94_FIRST_WAVE_RACE_MAX request kép/file (V9.3: mọi segment đợt đầu).
#  6) Kết thúc/lỗi/huỷ: đóng mọi response còn mở từ thread chính → thread đang read() thoát ngay.
#  7) info{} trả về số đo: workers, requests, hedges, steals, retries, ttfb, lock_wait, status...
V94_RANGE_ENGINE = str(os.environ.get("V94_RANGE_ENGINE", "v94") or "v94").strip().lower()
V94_AUTH_FAIL_REASONS = ("HTTP_401", "HTTP_403", "HTTP_404", "HTTP_410", "HTTP_429")


class V94RangeHTTPError(OSError):
    """HTTP lỗi trong Range engine; str() giữ dạng 'HTTP_<code>' như V9.3."""

    def __init__(self, status, cr_total=0, retry_after=0.0):
        super().__init__(f"HTTP_{int(status)}")
        self.status = int(status)
        self.cr_total = int(cr_total or 0)
        self.retry_after = float(retry_after or 0.0)


def _v94_parse_content_range(resp):
    """Trả (start, end, total) hoặc None. 'bytes */N' → (-1, -1, N)."""
    try:
        cr = str((resp.headers or {}).get("Content-Range") or "").strip()
    except Exception:
        return None
    if not cr:
        return None
    m = re.match(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$", cr, re.I)
    if m:
        tot = int(m.group(3)) if m.group(3) != "*" else 0
        return int(m.group(1)), int(m.group(2)), tot
    m = re.match(r"^bytes\s+\*/(\d+)$", cr, re.I)
    if m:
        return -1, -1, int(m.group(1))
    return None


def _v94_range_request(pool, url: str, start: int, end: int, read_timeout: float, follow: bool,
                       trace: dict = None):
    """Như _range_request V9.3 nhưng lỗi mang status + total (416) + Retry-After (429).
    V9.4.1: trace (tuỳ chọn) nhận t_30x (thời điểm nhận redirect đầu tiên) để tách RTT redirect / edge."""
    current = str(url)
    for hop in range(DIRECT_MAX_REDIRECTS + 1):
        resp = pool.request(
            "GET", current,
            headers={
                "Range": f"bytes={start}-{end}",
                "Accept": "*/*",
                "Accept-Encoding": "identity",
                "Connection": "keep-alive",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154.0.0.0 Safari/537.36",
            },
            preload_content=False, decode_content=False, redirect=False,
            timeout=urllib3.Timeout(connect=0.35, read=max(0.5, float(read_timeout))), retries=False,
        )
        status = int(getattr(resp, "status", 0) or 0)
        if follow and status in (301, 302, 303, 307, 308):
            if isinstance(trace, dict) and "t_30x" not in trace:
                trace["t_30x"] = time.perf_counter()
            loc = str((getattr(resp, "headers", {}) or {}).get("Location") or "").strip()
            _range_close_resp(resp, drain=True)
            if not loc or hop >= DIRECT_MAX_REDIRECTS:
                raise OSError(f"HTTP_{status}_redirect")
            current = urllib.parse.urljoin(current, loc)
            continue
        if status != 206:
            cr_total = 0
            retry_after = 0.0
            try:
                if status == 416:
                    cr = _v94_parse_content_range(resp)
                    cr_total = cr[2] if cr else 0
                if status == 429:
                    retry_after = float((resp.headers or {}).get("Retry-After") or 0.0)
            except Exception:
                pass
            _range_close_resp(resp, drain=False)
            raise V94RangeHTTPError(status, cr_total, retry_after)
        return resp, current, hop
    raise OSError("too_many_redirects")


def _v94_alt_err_definitive(e) -> bool:
    """Lỗi CHẮC CHẮN replica mn không dùng được: 401/403/404/410/429, bị từ chối kết nối, DNS hỏng.
    Timeout đọc/kết nối hay lỗi giao thức chỉ là 'chậm/đứt', không đủ để cấm replica 10 phút."""
    try:
        if isinstance(e, V94RangeHTTPError):
            return int(e.status) in (401, 403, 404, 410, 429)
        if urllib3 is not None:
            exc = urllib3.exceptions
            nce = getattr(exc, "NewConnectionError", None)
            nre = getattr(exc, "NameResolutionError", None)
            # NewConnectionError là lớp con của ConnectTimeoutError → phải xét trước; nhưng timeout kết nối
            # thuần (không phải từ chối/DNS) vẫn coi là không chắc chắn.
            if nre is not None and isinstance(e, nre):
                return True
            if nce is not None and isinstance(e, nce):
                txt = str(e).lower()
                return not ("timed out" in txt or "timeout" in txt)
            mre = getattr(exc, "MaxRetryError", None)
            if mre is not None and isinstance(e, mre):
                return _v94_alt_err_definitive(getattr(e, "reason", None))
        if isinstance(e, ConnectionRefusedError):
            return True
    except Exception:
        pass
    return False


def _v94_pool_new_conns(pool) -> int:
    """Tổng số kết nối TCP/TLS mới urllib3 đã mở (toàn tiến trình, ~ số TLS handshake)."""
    try:
        total = 0
        for key in list(pool.pools.keys()):
            p = pool.pools.get(key)
            total += int(getattr(p, "num_connections", 0) or 0)
        return total
    except Exception:
        return -1


def _download_http_range_turbo_v94(url: str, temp_dir: Path, filename: str, logger,
                                   min_bytes: int, expected_bytes: int, media_kind: str = "video",
                                   info: dict = None) -> bool:
    info = info if isinstance(info, dict) else {}
    job = str(info.get("job") or "-")
    cancel_ev = info.get("cancel")
    pool = info.get("pool") or _get_direct_media_pool()
    is_audio = (media_kind == "audio")
    size_known = int(expected_bytes or 0) > 0
    if urllib3 is None or pool is None:
        info["fail_reason"] = "no_pool"
        return False
    if not size_known and not V94_RANGE_UNKNOWN_SIZE:
        info["fail_reason"] = "unknown_size"
        return False
    total0 = int(expected_bytes) if size_known else int(
        V94_UNKNOWN_FIRST_BYTES_AUDIO if is_audio else V94_UNKNOWN_FIRST_BYTES_VIDEO)
    ensure_dir(temp_dir)
    temp_file = temp_dir / filename
    try:
        if temp_file.exists():
            temp_file.unlink()
    except OSError:
        info["fail_reason"] = "temp_locked"
        return False

    t0 = time.perf_counter()
    t0_ns = time.perf_counter_ns()
    conns0 = _v94_pool_new_conns(pool)
    read_timeout = 1.5 if is_audio else 3.0
    min_part_bytes = RANGE_AUDIO_MIN_PART_BYTES if is_audio else RANGE_MIN_PART_BYTES
    # V9.4.1: số worker riêng cho video <= 4MiB (A/B theo job qua info["small_video_workers"]).
    svw = 0 if is_audio else int(info.get("small_video_workers") or V94_VIDEO_SMALL_WORKERS or 0)
    if size_known:
        workers = _range_worker_count(total0, media_kind, svw)
        if int(info.get("workers_override") or 0) > 0:      # V9.6: ép N đoạn (A/B 360p)
            workers = int(info["workers_override"])
        workers = max(1, min(workers, max(1, total0 // min_part_bytes)))
    else:
        # Size chưa biết: chia khoảng đoán [0, guess) cho vài request song song ngay t=0 (đoạn vượt
        # size thật nhận 416 "*/N" hoặc bị cắt theo Content-Range → vô hại), header đầu → fan-out phần còn lại.
        workers = max(1, min(int(V94_UNKNOWN_FIRST_WORKERS), max(1, total0 // min_part_bytes)))
    part = (total0 + workers - 1) // workers
    max_workers_cap = max(workers, int(V94_RANGE_MAX_WORKERS))

    segs = []
    s_ = 0
    while s_ < total0:
        e_ = min(total0 - 1, s_ + part - 1)
        segs.append(_RangeSeg(s_, e_))
        segs[-1].first = True
        s_ = e_ + 1
    workers = len(segs)

    _t_pa = time.perf_counter()
    try:
        fh = open(temp_file, "w+b")
        fh.truncate(total0)
    except OSError as e:
        logger(f"    [RANGE-TURBO] ⚠️ create file: {e}")
        info["fail_reason"] = "create_file"
        return False
    prealloc_ms = round((time.perf_counter() - _t_pa) * 1000.0, 2)   # Windows: _chsize ghi 0 trước GET đầu

    lock = threading.Lock()
    pending = list(segs)
    active = set()
    open_resps = set()
    state = {"done": 0, "error": None, "closed": False, "steals": 0, "retries": 0,
             "url": str(url), "hops": 0, "ttfb": None, "total": total0, "size_known": size_known,
             "ttfb_ema": None, "alt": "", "alt_ok": None, "host_ttfb": [None, None],
             "host_req": [0, 0], "hedges": 0, "alt_wins": 0, "hedging": 0, "races": 0,
             "resized": None, "status": {}, "lock_wait_ns": 0, "writes": 0, "workers_live": 0,
             "workers_peak": 0, "first_byte": None, "bad_cr": 0,
             # V9.4.1 telemetry
             "splits": 0, "takeovers": 0, "losers": [], "loser_pending": 0, "inflight": 0, "conc_peak": 0,
             "conn_ids": set(), "req_seq": 0, "late_ttfb": [], "body_rates": [], "bans": 0}
    # V9.4.1: file nhỏ (<= V94_RACE_ALL_IF_WORKERS_LE segment đợt đầu) race TẤT CẢ như V9.3; file lớn giữ trần.
    # Log live: video 6 worker chỉ race 4 → 2 segment còn lại phải chờ host chính + 302 (+~150ms trong mock).
    _rm = int(V94_FIRST_WAVE_RACE_MAX)
    _le = int(V94_RACE_ALL_IF_WORKERS_LE or 0)
    race_max = -1 if (_rm != 0 and _le > 0 and workers <= _le) else _rm
    if (RANGE_MN_RACE and (workers >= 4 or not size_known) and time.time() >= _MN_RACE_BAD_UNTIL[0]):
        state["alt"] = _gvs_alt_url(url)
    finished = threading.Event()
    ex = ThreadPoolExecutor(max_workers=max_workers_cap, thread_name_prefix=f"YT{media_kind.capitalize()}Range")

    def _cnt_status_locked(code):
        k = str(code)
        state["status"][k] = state["status"].get(k, 0) + 1

    def _pick_host_locked(initial: bool) -> int:
        if not state["alt"] or state["alt_ok"] is False:
            return 0
        a, b = state["host_ttfb"]
        if state["alt_ok"] and b is not None and (a is None or b < a):
            return 1
        return 0

    def _all_done_locked():
        return (state["size_known"] and state["done"] >= state["total"] and not pending
                and not any(s.pos <= s.end for s in active))

    def _spawn_workers_locked(n):
        n = max(0, min(int(n), max_workers_cap - state["workers_live"]))
        for _ in range(n):
            state["workers_live"] += 1
            state["workers_peak"] = max(state["workers_peak"], state["workers_live"])
            ex.submit(_worker)

    def _apply_real_total_locked(rt: int):
        """Size thật đã biết (Content-Range). Co/giãn tại chỗ. Gọi khi đang giữ lock."""
        old = state["total"]
        if rt <= 0:
            return
        if state["size_known"] and rt == old:
            return
        if state["size_known"] and state["resized"] is not None and rt != old:
            raise OSError(f"inconsistent_total_{old}_{rt}")
        if not V94_RANGE_ADAPTIVE_RESIZE and size_known and rt != old:
            # Hành vi V9.3: báo size thật cho wrapper và tải lại từ đầu.
            info["pending_real_total"] = rt
            raise OSError(f"size_mismatch_{rt}")
        state["resized"] = (old, rt)
        state["size_known"] = True
        state["total"] = rt
        if rt > old:
            fh.truncate(rt)
            ext = rt - old
            target = _range_worker_count(rt, media_kind, svw)
            target = max(1, min(target, max(1, rt // min_part_bytes), max_workers_cap))
            n_new = max(1, min(target, ext // min_part_bytes or 1))
            step = (ext + n_new - 1) // n_new
            p = old
            ext_segs = []
            while p < rt:
                q = min(rt - 1, p + step - 1)
                ns = _RangeSeg(p, q)
                ns.first = state["host_ttfb"][0] is None
                ns.kind = "ext"
                ext_segs.append(ns)
                p = q + 1
            # Phần nới thêm lên ĐẦU hàng đợi + worker MỚI cho riêng nó ngay lúc này, không chờ worker
            # cũ xong đoạn của mình (đo offline: chờ = chậm hơn V9.3 restart ~10%).
            pending[:0] = ext_segs
            _spawn_workers_locked(max(target - state["workers_live"], len(ext_segs)))
        else:
            for sg in list(pending):
                sg.end = min(sg.end, rt - 1)
                if sg.pos > sg.end:
                    pending.remove(sg)
            for sg in active:
                sg.end = min(sg.end, rt - 1)
            fh.truncate(rt)

    def _take():
        with lock:
            if state["error"] is not None or state["closed"]:
                return None
            if pending:
                seg = pending.pop(0)
                seg.last_t = seg.t_start = time.perf_counter()
                seg.h = _pick_host_locked(initial=(seg.retries == 0 and state["done"] == 0))
                active.add(seg)
                return seg
            if not state["size_known"]:
                return None    # chưa biết size thì chưa chẻ (request đầu sẽ fan-out)
            now = time.perf_counter()
            ttfb = state["ttfb_ema"] if state["ttfb_ema"] is not None else 0.30
            rates = [x.got / (now - x.t_start) for x in active
                     if x.got > 0 and now - x.t_start > 0.05 and x.pos <= x.end]
            r_avg = (sum(rates) / len(rates)) if rates else 0.0
            best, best_eta = None, 0.0
            for x in active:
                if x.taken or x.end - x.pos + 1 < 2 * RANGE_STEAL_MIN_BYTES:
                    continue
                e_ = x.eta(now)
                if e_ > best_eta:
                    best, best_eta = x, e_
            if best is not None and best_eta > 2.0 * ttfb + 0.10:
                rem = best.end - best.pos + 1
                r_v = best.got / max(1e-3, now - best.t_start) if best.got > 0 else 0.0
                if r_v > 0 and r_avg > 0:
                    keep = int(r_v * (ttfb + rem / r_avg) / (1.0 + r_v / r_avg))
                else:
                    keep = rem // 2
                keep = max(RANGE_STEAL_MIN_BYTES, min(rem - RANGE_STEAL_MIN_BYTES, keep))
                mid = best.pos + keep
                new = _RangeSeg(mid, best.end)
                new.h = _pick_host_locked(initial=False)
                new.kind = "split"
                best.end = mid - 1
                active.add(new)
                state["steals"] += 1
                state["splits"] += 1
                return new
            for x in sorted(active, key=lambda y: -y.eta(now)):
                if x.pos > x.end or x.taken:
                    continue
                if x.got == 0 and (state["ttfb_ema"] is None or
                                   now - x.t_start < max(RANGE_STALL_SEC, 1.5 * ttfb + 0.30) * (2 ** x.gen)):
                    continue
                rem = x.end - x.pos + 1
                fresh_eta = ttfb + (rem / r_avg if r_avg > 0 else 0.0)
                stalled = now - x.last_t >= max(RANGE_STALL_SEC, ttfb + 0.35)
                slow = (now - x.t_start >= RANGE_HEDGE_MIN_AGE and
                        x.eta(now) >= max(RANGE_HEDGE_ETA_SEC, fresh_eta + 0.25))
                if stalled or slow:
                    new = _RangeSeg(x.pos, x.end)
                    new.h = _pick_host_locked(initial=False)
                    new.gen = x.gen + 1
                    new.kind = "takeover"
                    x.taken = True
                    x.end = x.pos - 1
                    active.add(new)
                    state["steals"] += 1
                    state["takeovers"] += 1
                    return new
            return None

    def _req(u, start, end, trace=None):
        with lock:
            state["inflight"] += 1
            state["conc_peak"] = max(state["conc_peak"], state["inflight"] + len(open_resps))
        try:
            return _v94_range_request(pool, u, start, end, read_timeout, True, trace=trace)
        finally:
            with lock:
                state["inflight"] -= 1

    def _arm_ban_locked(reason, after_end):
        """Cấm replica mn toàn tiến trình 10 phút — luôn có log + span (V9.3 im lặng)."""
        if state["alt_ok"] is not True:
            state["alt_ok"] = False
        if _MN_RACE_BAD_UNTIL[0] > time.time() + 590.0:
            return
        _MN_RACE_BAD_UNTIL[0] = time.time() + 600.0
        state["bans"] += 1
        try:
            logger(f"    [RANGE-TURBO] ⛔ mn-race OFF 600s | reason={reason} kind={media_kind} after_end={after_end}")
        except Exception:
            pass
        TELEM.span(job, "mn_race_ban", reason=str(reason), kind=media_kind, after_end=bool(after_end))

    def _alt_failed_locked(e, during_race_wait):
        """Replica mn lỗi. Luật cũ (V94_ALT_BAN_STRICT=0): mọi lỗi → cấm 10 phút. Luật mới: chỉ lỗi chắc chắn
        mới cấm; lỗi chậm/timeout chỉ tắt alt trong job đang chạy, và không làm gì nếu job đã xong."""
        after_end = finished.is_set()
        reason = (f"HTTP_{e.status}" if isinstance(e, V94RangeHTTPError) else type(e).__name__)
        if not V94_ALT_BAN_STRICT:
            if state["alt_ok"] is not True:
                _arm_ban_locked(reason, after_end)
            return
        if _v94_alt_err_definitive(e):
            if state["alt_ok"] is not True:
                _arm_ban_locked(reason, after_end)
        elif not after_end and state["alt_ok"] is not True:
            state["alt_ok"] = False

    def _open_hedged(seg: _RangeSeg, start: int, end: int):
        with lock:
            if seg.h == 1 and (not state["alt"] or state["alt_ok"] is False):
                seg.h = 0
            first_h = seg.h
            urls = [state["url"], state["alt"] if state["alt_ok"] is not False else ""]
            a_ttfb = state["host_ttfb"][first_h]
            delay = max(RANGE_HEDGE_DELAY_MIN, 2.5 * a_ttfb) if a_ttfb is not None else RANGE_HEDGE_DELAY_MIN
            race_now = bool(seg.first and seg.retries == 0 and state["alt"] and state["alt_ok"] is not False
                            and state["alt_wins"] == 0 and state["host_ttfb"][0] is None
                            and (race_max < 0 or state["races"] < race_max))
            if race_now:
                state["races"] += 1
                delay = 0.0
                seg.hmode = "race"
            state["host_req"][first_h] += 1
            if state["host_req"][0] + state["host_req"][1] == 1:
                info["first_get_ns"] = time.perf_counter_ns()
                TELEM.span(job, "gvs_request_begin", kind=media_kind, workers=workers,
                           size_known=size_known, expected=int(expected_bytes or 0), race_max=race_max)
        other_h = 1 - first_h
        traces = {first_h: {}}
        if not urls[other_h]:
            resp, got_url, hops = _req(urls[first_h], start, end, traces[first_h])
            seg.h = first_h
            return resp, got_url, hops, urls[first_h], traces[first_h]
        t_sub = time.perf_counter()
        f_first = _HEDGE_EXEC.submit(_req, urls[first_h], start, end, traces[first_h])
        futs = {f_first: first_h}
        done, _ = wait([f_first], timeout=delay) if delay > 0 else (set(), None)
        if done and f_first.exception() is None:
            seg.h = first_h
            return (*f_first.result(), urls[first_h], traces[first_h])

        def _try_budget():
            if race_now:
                with lock:
                    state["hedges"] += 1
                    state["host_req"][other_h] += 1
                    state["hedging"] += 1
                return True
            with lock:
                _a, _b = state["host_ttfb"]
                alt_proven = state["alt_wins"] > 0 and _b is not None and (_a is None or _b < _a)
                cap = workers if alt_proven else max(1, int(workers * RANGE_HEDGE_MAX_FRAC))
                ok_ = (not finished.is_set() and bool(urls[other_h]) and state["alt_ok"] is not False and
                       state["hedging"] < cap)
                if ok_:
                    state["hedging"] += 1
                    state["hedges"] += 1
                    state["host_req"][other_h] += 1
                return ok_
        while not _try_budget():
            if f_first.done() or finished.is_set() or state["alt_ok"] is False:
                seg.h = first_h
                return (*f_first.result(), urls[first_h], traces[first_h])
            wait([f_first], timeout=0.05)
        if not race_now:
            seg.hmode = "hedge"
        try:
            traces[other_h] = {}
            futs[_HEDGE_EXEC.submit(_req, urls[other_h], start, end, traces[other_h])] = other_h
            pending_f = set(futs)
            winner = None
            errors = {}
            while pending_f and winner is None:
                done, pending_f = wait(pending_f, return_when=FIRST_COMPLETED)
                for f in done:
                    e = f.exception()
                    if e is None and winner is None:
                        winner = f
                    elif e is None:
                        _range_close_resp(f.result()[0], drain=False)
                    else:
                        errors[futs[f]] = e
        finally:
            with lock:
                state["hedging"] -= 1

        def _loser_done(ff, lh):
            e = ff.exception()
            if e is None:
                _range_close_resp(ff.result()[0], drain=False)
            with lock:
                state["loser_pending"] -= 1
                state["losers"].append(("alt" if lh == 1 else "main",
                                        round((time.perf_counter() - t_sub) * 1000.0, 1),
                                        "ok" if e is None else f"err:{type(e).__name__}"))
                if e is not None and lh == 1:
                    _alt_failed_locked(e, during_race_wait=False)
        with lock:
            state["loser_pending"] += len(pending_f)
        for f in pending_f:
            f.add_done_callback(lambda ff, lh=futs[f]: _loser_done(ff, lh))
        if 1 in errors:
            with lock:
                _alt_failed_locked(errors[1], during_race_wait=True)
        if winner is None:
            raise errors.get(0) or errors.get(1) or OSError("hedge_no_winner")
        h = futs[winner]
        seg.h = h
        if h == 1:
            with lock:
                state["alt_wins"] += 1
        resp, got_url, hops = winner.result()
        return resp, got_url, hops, urls[h], traces.get(h) or {}

    def _validate_header_locked(resp, start, req_end):
        """Content-Range phải đúng byte đã xin. Trả None nếu ok, chuỗi lỗi nếu sai."""
        cr = _v94_parse_content_range(resp)
        if cr is None or cr[0] < 0:
            if not state["size_known"]:
                return "no_content_range"
            if V94_STRICT_CONTENT_RANGE:
                try:
                    cl = int((resp.headers or {}).get("Content-Length") or -1)
                except Exception:
                    cl = -1
                exp_len = min(req_end, state["total"] - 1) - start + 1
                return None if cl == exp_len else "no_content_range"
            return None
        c_start, c_end, c_total = cr
        if c_total > 0:
            _apply_real_total_locked(c_total)
        elif not state["size_known"]:
            return "content_range_total_unknown"
        if not V94_STRICT_CONTENT_RANGE:
            return None
        exp_end = min(req_end, state["total"] - 1)
        if c_start != start or c_end != exp_end:
            state["bad_cr"] += 1
            return f"bad_content_range_{c_start}-{c_end}_want_{start}-{exp_end}"
        return None

    def _run_seg(seg: _RangeSeg):
        resp = None
        _rq = {"t_issue": seg.t_start, "t_hdr": None, "trace": {}, "outcome": "done", "reused": None,
               "start": seg.pos, "req_end": seg.end}
        try:
            with lock:
                start, end = seg.pos, seg.end
            if start > end:
                _rq["outcome"] = "empty"
                return
            req_end = end
            _rq["start"], _rq["req_end"] = start, req_end
            resp, got_url, hops, cur_url, _rq["trace"] = _open_hedged(seg, start, end)
            _rq["t_hdr"] = time.perf_counter()
            with lock:
                open_resps.add(resp)
                try:
                    _cid = id(getattr(resp, "connection", None) or resp)
                    _rq["reused"] = _cid in state["conn_ids"]
                    state["conn_ids"].add(_cid)
                except Exception:
                    pass
                if state["closed"]:
                    raise OSError("closed_late")      # except-path đóng resp, không tính lỗi
                _cnt_status_locked(206)
                bad = _validate_header_locked(resp, start, req_end)
                if bad:
                    raise OSError(bad)
                _hn = time.perf_counter() - seg.t_start
                _ht = state["host_ttfb"][seg.h]
                state["host_ttfb"][seg.h] = _hn if _ht is None else 0.7 * _ht + 0.3 * _hn
                if seg.h == 1:
                    state["alt_ok"] = True
                if seg.h == 0 and got_url != cur_url and state["url"] == cur_url:
                    state["url"] = got_url
                    state["hops"] = hops
                    if V96_HOST_POOL:
                        _v96_remember_redirect(cur_url, got_url)
                _now = time.perf_counter()
                if state["ttfb"] is None:
                    state["ttfb"] = _now - t0
                    TELEM.span(job, "gvs_206_headers", kind=media_kind, total=state["total"],
                               ttfb_ms=round((_now - t0) * 1000.0, 2), hops=hops, host=seg.h)
                _req_ttfb = _now - seg.t_start
                state["ttfb_ema"] = (_req_ttfb if state["ttfb_ema"] is None
                                     else 0.7 * state["ttfb_ema"] + 0.3 * _req_ttfb)
            while True:
                with lock:
                    if state["closed"] or seg.pos > seg.end:
                        break
                    want = seg.end - seg.pos + 1
                chunk = resp.read(min(RANGE_STEAL_READ_CHUNK, want))
                if not chunk:
                    raise OSError("short_range")
                _tw = time.perf_counter_ns()
                with lock:
                    state["lock_wait_ns"] += time.perf_counter_ns() - _tw
                    if state["closed"]:
                        break
                    allowed = min(len(chunk), seg.end - seg.pos + 1)
                    if allowed <= 0:
                        break
                    fh.seek(seg.pos)
                    fh.write(chunk[:allowed] if allowed < len(chunk) else chunk)
                    if state["first_byte"] is None:
                        state["first_byte"] = time.perf_counter() - t0
                        TELEM.span(job, f"{media_kind}_first_byte",
                                   ms=round(state["first_byte"] * 1000.0, 2))
                    seg.pos += allowed
                    seg.got += allowed
                    seg.last_t = time.perf_counter()
                    state["done"] += allowed
                    state["writes"] += 1
                    _range_progress_add(temp_file, allowed)
            with lock:
                clean = seg.pos == req_end + 1
                open_resps.discard(resp)
                if state["closed"] and seg.pos <= seg.end:
                    _rq["outcome"] = "closed"
                elif seg.taken:
                    _rq["outcome"] = "taken"
                elif seg.end < req_end:
                    _rq["outcome"] = "victim"
            _range_close_resp(resp, drain=clean)
            resp = None
        except Exception as e:
            _st_e = getattr(e, "status", None)
            _rq["outcome"] = (f"error:HTTP_{_st_e}" if _st_e is not None else f"error:{type(e).__name__}")
            if str(e) == "closed_late":
                _rq["outcome"] = "closed"
            if resp is not None:
                with lock:
                    open_resps.discard(resp)
                _range_close_resp(resp, drain=False)
                resp = None
            with lock:
                st = getattr(e, "status", None)
                if st is not None:
                    _cnt_status_locked(st)
                    if st == 429:
                        state["retry_after"] = float(getattr(e, "retry_after", 0.0) or 0.0)
                if st == 416 and getattr(e, "cr_total", 0) > 0:
                    try:
                        _apply_real_total_locked(e.cr_total)
                    except Exception as e2:
                        if state["error"] is None:
                            state["error"] = e2
                    if seg.pos > seg.end:
                        e = None          # đoạn nằm ngoài size thật → không phải lỗi
                if e is None or state["closed"]:
                    pass
                elif (seg.h == 1 and seg.got == 0 and seg.pos <= seg.end and not seg.taken
                        and not str(e).startswith(("bad_content_range", "inconsistent_total", "size_mismatch"))):
                    if state["alt_ok"] is not True:
                        # hành vi V9.3/V9.4 giữ nguyên (alt được chọn làm host chính cho đoạn này mà lỗi), có log
                        _arm_ban_locked(f"in_job:{_rq['outcome']}", finished.is_set())
                    retry = _RangeSeg(seg.pos, seg.end, seg.retries)
                    retry.h = 0
                    retry.kind = "retry"
                    seg.end = seg.pos - 1
                    pending.insert(0, retry)
                elif seg.pos <= seg.end and not seg.taken:
                    es = str(e)
                    if (es.startswith("HTTP_4") or es.startswith("HTTP_200") or es.startswith("bad_content_range")
                            or es.startswith("no_content_range") or es.startswith("inconsistent_total")
                            or es.startswith("size_mismatch") or es.startswith("content_range")):
                        state["error"] = e
                    elif seg.retries + 1 > RANGE_SEG_MAX_RETRY:
                        state["error"] = e
                    else:
                        retry = _RangeSeg(seg.pos, seg.end, seg.retries + 1)
                        retry.kind = "retry"
                        seg.end = seg.pos - 1
                        pending.insert(0, retry)
                        state["retries"] += 1
                elif str(e).startswith(("bad_content_range", "inconsistent_total", "size_mismatch")):
                    state["error"] = e
        finally:
            _t_end = time.perf_counter()
            with lock:
                active.discard(seg)
                if state["error"] is not None or _all_done_locked():
                    finished.set()
                if _rq["outcome"] != "empty":
                    state["req_seq"] += 1
                    _seq = state["req_seq"]
                    _th = _rq["t_hdr"]
                    if _th is not None:
                        if seg.kind != "first":
                            state["late_ttfb"].append(_th - _rq["t_issue"])
                        if seg.got >= 128 * 1024 and _t_end > _th:
                            state["body_rates"].append(seg.got * 8.0 / 1e6 / (_t_end - _th))
            if _rq["outcome"] != "empty":
                _t30 = (_rq["trace"] or {}).get("t_30x")
                TELEM.span(job, "gvs_req", seq=_seq, kind=media_kind, req=seg.kind, mode=seg.hmode,
                           host=("alt" if seg.h == 1 else "main"), start=int(_rq["start"]),
                           len=int(_rq["req_end"]) - int(_rq["start"]) + 1,
                           t_issue_ms=round((_rq["t_issue"] - t0) * 1000.0, 1),
                           t_30x_ms=(round((_t30 - t0) * 1000.0, 1) if _t30 else None),
                           t_hdr_ms=(round((_th - t0) * 1000.0, 1) if _th else None),
                           t_end_ms=round((_t_end - t0) * 1000.0, 1), bytes=int(seg.got),
                           end=_rq["outcome"], conn_reused=_rq["reused"])

    def _worker():
        try:
            while not finished.is_set():
                seg = _take()
                if seg is None:
                    finished.wait(0.03)
                    continue
                _run_seg(seg)
        finally:
            with lock:
                state["workers_live"] -= 1

    _range_progress_begin(temp_file, total0)
    logger(
        f"    [RANGE-TURBO] 🚀 START {media_kind.upper()} | "
        + (f"{total0/1048576:.2f}MB" if size_known else f"size=? (Range-first {total0/1024:.0f}KiB)")
        + f" | workers={workers} | part≈{part/1048576:.2f}MB | V9.4 EAGER+STEAL+CR-CHECK"
    )
    last_log = time.perf_counter()
    ok = False
    err = None
    try:
        with lock:
            _spawn_workers_locked(workers)
        while not finished.wait(0.05):
            now = time.perf_counter()
            if cancel_ev is not None and cancel_ev.is_set():
                with lock:
                    state["error"] = RuntimeError("cancelled")
                break
            if now - last_log >= 1.0 and now - t0 >= 1.0:
                with lock:
                    live_done, tot_now = state["done"], state["total"]
                elapsed = max(0.001, now - t0)
                logger(
                    f"    [RANGE-TURBO] ⏬ {filename} | {live_done/1048576:.2f}/{tot_now/1048576:.2f}MB | "
                    f"{elapsed:.2f}s | ≈{live_done*8.0/1e6/elapsed:.0f}Mbps"
                )
                last_log = now
            if now - t0 > 60.0:
                with lock:
                    state["error"] = TimeoutError("range_total_timeout")
                break
        with lock:
            err = state["error"]
            state["closed"] = True
            done = state["done"]
            total = state["total"]
            to_close = list(open_resps)
            open_resps.clear()
        for r in to_close:      # thread đang read() sẽ nhận lỗi và thoát ngay, không treo tới timeout
            _range_close_resp(r, drain=False)
        try:
            fh.close()
        except OSError:
            pass
        if err is not None:
            logger(f"    [RANGE-TURBO] ⚠️ {type(err).__name__}: {err} → fallback")
            return False
        size = temp_file.stat().st_size if temp_file.exists() else 0
        if done != total or size != total or size < min_bytes:
            logger(f"    [RANGE-TURBO] ⚠️ size mismatch {done}/{size}/{total} → fallback")
            err = OSError(f"final_size_{done}_{size}_{total}")
            return False
        elapsed = max(0.001, time.perf_counter() - t0)
        alt_info = ""
        if state["alt"]:
            _a, _b = state["host_ttfb"]
            with lock:
                _lo = list(state["losers"])
                _lp = int(state["loser_pending"])
            _lok = sum(1 for x in _lo if x[2] == "ok")
            alt_info = (f" | hedge={state['hedges']} race={state['races']} alt-win={state['alt_wins']}"
                        f" ({'OK' if state['alt_ok'] else ('FAIL' if state['alt_ok'] is False else '-')})"
                        f" main={(_a or 0):.2f}s/{state['host_req'][0]}req"
                        + (f" alt={_b:.2f}s/{state['host_req'][1]}req" if _b is not None
                           else f" alt=n/a/{state['host_req'][1]}req")
                        + f" alt_lost={_lok}/{len(_lo) - _lok}/{_lp}")
        rz = state["resized"]
        # V9.4.1: số đo trung thực hơn (giữ nguyên mọi trường cũ ở trên, chỉ thêm phía sau)
        _fb = state["first_byte"] or 0.0
        _xfer = (total * 8.0 / 1e6 / max(1e-3, elapsed - _fb)) if _fb else 0.0
        with lock:
            _br = sorted(state["body_rates"])
            _lt = sorted(state["late_ttfb"])
        _p = lambda arr, q: arr[min(len(arr) - 1, int(q * (len(arr) - 1) + 0.5))] if arr else None
        _ltp50, _ltp90 = _p(_lt, 0.5), _p(_lt, 0.9)
        extra = (f" | xfer≈{_xfer:.0f}Mbps"
                 + (f" conn_p50≈{_p(_br, 0.5):.1f}Mbps" if _br else "")
                 + (f" ttfb_late={_ltp50:.2f}/{_ltp90:.2f}s" if _lt else "")
                 + f" hedge_real={state['hedges'] - state['races']} gets={state['host_req'][0] + state['host_req'][1]}"
                 + f" conc_peak={state['conc_peak']} splits={state['splits']} takeovers={state['takeovers']}"
                 + f" prealloc={prealloc_ms:.0f}ms" + (f" ab={svw}" if svw and total <= 4 * 1048576 else ""))
        logger(
            f"    [RANGE-TURBO] ✅ HOÀN TẤT {media_kind.upper()} | {total/1048576:.2f}MB | "
            f"{elapsed:.3f}s | ≈{total*8.0/1e6/elapsed:.0f}Mbps | workers={workers}→{state['workers_peak']} | "
            f"steal={state['steals']} | retry={state['retries']} | ttfb={(state['ttfb'] or 0):.2f}s"
            + (f" | redirect=x{state['hops']}" if state['hops'] else "")
            + (f" | resize {rz[0]}→{rz[1]} (giữ dữ liệu)" if rz else "") + alt_info + extra
        )
        ok = True
        return True
    finally:
        with lock:
            state["closed"] = True
            to_close = list(open_resps)
            open_resps.clear()
        for r in to_close:
            _range_close_resp(r, drain=False)
        finished.set()
        try:
            fh.close()
        except OSError:
            pass
        ex.shutdown(wait=False, cancel_futures=True)
        _range_progress_end(temp_file)
        if not ok:
            try:
                if temp_file.exists(): temp_file.unlink()
            except OSError:
                pass
        elapsed_ms = (time.perf_counter_ns() - t0_ns) / 1e6
        fr = ""
        if not ok:
            fr = str(err) if err is not None else (str(state.get("error")) if state.get("error") else "unknown")
        info.update({
            "ok": ok, "engine": "v94", "kind": media_kind, "bytes": int(state["done"]),
            "total": int(state["total"]), "expected": int(expected_bytes or 0),
            "workers_initial": workers, "workers_peak": state["workers_peak"],
            "requests": state["host_req"][0] + state["host_req"][1], "req_main": state["host_req"][0],
            "req_alt": state["host_req"][1], "hedges": state["hedges"], "races": state["races"],
            "alt_wins": state["alt_wins"], "steals": state["steals"], "retries": state["retries"],
            "redirects": state["hops"], "status": dict(state["status"]),
            "ttfb_ms": round((state["ttfb"] or 0) * 1000.0, 2),
            "first_byte_ms": round((state["first_byte"] or 0) * 1000.0, 2),
            "elapsed_ms": round(elapsed_ms, 2),
            "mbps": round((state["done"] * 8.0 / 1e6) / max(1e-6, elapsed_ms / 1000.0), 1),
            "lock_wait_ms": round(state["lock_wait_ns"] / 1e6, 3), "writes": state["writes"],
            "resized": list(state["resized"]) if state["resized"] else None,
            "new_conns": (_v94_pool_new_conns(pool) - conns0) if conns0 >= 0 else -1,
            "new_conns_scope": "process",
            "fail_reason": fr, "retry_after": float(state.get("retry_after") or 0.0),
            # V9.4.1
            "prealloc_ms": prealloc_ms, "splits": state["splits"], "takeovers": state["takeovers"],
            "hedge_real": state["hedges"] - state["races"], "conc_peak": state["conc_peak"],
            "race_max": race_max, "small_video_workers": svw,
            "losers_ok": sum(1 for x in state["losers"] if x[2] == "ok"),
            "losers_err": sum(1 for x in state["losers"] if x[2] != "ok"),
            "losers_pending": int(state["loser_pending"]), "mn_bans": state["bans"],
            "xfer_mbps": (round(state["done"] * 8.0 / 1e6 / max(1e-3, elapsed_ms / 1000.0 - (state["first_byte"] or 0)), 1)
                          if state["first_byte"] else None),
            "conn_p50_mbps": (lambda a: round(a[len(a) // 2], 2) if a else None)(sorted(list(state["body_rates"]))),
            "ttfb_late_p50_ms": (lambda a: round(a[len(a) // 2] * 1000.0, 1) if a else None)(sorted(list(state["late_ttfb"]))),
        })
        TELEM.span(job, f"{media_kind}_download_complete", **{k: v for k, v in info.items()
                                                                if k not in ("job", "cancel", "pool")})


def _download_http_range_turbo(url: str, temp_dir: Path, filename: str, logger,
                               min_bytes: int, expected_bytes: int, media_kind: str = "video",
                               info: dict = None) -> bool:
    """V9.4 dispatcher. V94_RANGE_ENGINE=v93 → engine V9.3 nguyên bản."""
    if V94_RANGE_ENGINE == "v93":
        t0 = time.perf_counter()
        ok = _download_http_range_turbo_v93(url, temp_dir, filename, logger, min_bytes, expected_bytes, media_kind)
        if isinstance(info, dict):
            info.update({"ok": ok, "engine": "v93", "kind": media_kind,
                         "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 2),
                         "fail_reason": "" if ok else "v93_fail"})
        return ok
    info = info if isinstance(info, dict) else {}
    if _download_http_range_turbo_v94(url, temp_dir, filename, logger, min_bytes, expected_bytes,
                                      media_kind, info):
        return True
    rt = int(info.pop("pending_real_total", 0) or 0)
    if rt > 0:        # chỉ khi V94_RANGE_ADAPTIVE_RESIZE=0 (hành vi V9.3)
        logger(f"    [RANGE-TURBO] ℹ️ size thật {rt/1048576:.2f}MB → tải lại đúng size (resize tắt)")
        return _download_http_range_turbo_v94(url, temp_dir, filename, logger, min_bytes, rt, media_kind, info)
    return False


def _v94_audio_ext(data: dict) -> str:
    """Đuôi file audio theo codec thật: opus/webm (249/250/251) → .webm, còn lại (139/140 mp4a) → .m4a."""
    mime = str((data or {}).get("audio_mime") or "").lower()
    ext = str((data or {}).get("audio_ext") or "").lower()
    itag = str((data or {}).get("audio_format_id") or "").split("-", 1)[0]
    if "webm" in mime or ext in ("webm", "weba", "opus") or itag in ("249", "250", "251"):
        return ".webm"
    return ".m4a"


def _v94_is_auth_fail(info: dict) -> bool:
    fr = str((info or {}).get("fail_reason") or "")
    return any(fr.startswith(x) for x in V94_AUTH_FAIL_REASONS)


def download_smart_progressive(url: str, temp_dir: Path, filename: str, logger,
                               min_bytes: int, expected_bytes: int = 0, info: dict = None) -> bool:
    """V8.6: adaptive video never uses the throttling-prone un-ranged 1GET path.

    V9.4: size không biết cũng đi Range-first (biết size từ header 206 đầu); 403/410/429 trên
    signed URL thì KHÔNG gọi aria2 với cùng URL (caller sẽ SAFE-REFRESH)."""
    info = info if isinstance(info, dict) else {}
    expected = int(expected_bytes or 0)
    if expected >= RANGE_VIDEO_MIN_BYTES or (expected <= 0 and V94_RANGE_UNKNOWN_SIZE):
        if _download_http_range_turbo(url, temp_dir, filename, logger, min_bytes, expected,
                                      media_kind="video", info=info):
            return True
        if V94_NO_ARIA2_ON_AUTH_FAIL and _v94_is_auth_fail(info):
            logger(f"    ↪ RANGE-TURBO video {info.get('fail_reason')} → BỎ aria2 (cùng signed URL), chờ SAFE-REFRESH")
            return False
        logger("    ↪ RANGE-TURBO video thất bại → ARIA2 RPC fallback")
    info["fallback"] = "aria2"
    return download_aria2c_ultimate(url, temp_dir, filename, logger, min_bytes, expected_bytes=expected)




def download_smart_audio(url: str, temp_dir: Path, filename: str, logger,
                         min_bytes: int = MIN_BYTES_AUDIO, expected_bytes: int = 0, info: dict = None) -> bool:
    """V7.9: never let an un-ranged 139 GET hold the hot path for tens of seconds.

    V9.4: size không biết → Range-first (1 request 512KiB đầu cho biết size thật rồi fan-out);
    DIRECT-1GET chỉ còn là đường cuối và có deadline tổng V94_DIRECT_1GET_DEADLINE_SEC."""
    info = info if isinstance(info, dict) else {}
    expected = int(expected_bytes or 0)
    if expected <= 0 and V94_RANGE_UNKNOWN_SIZE:
        if _download_http_range_turbo(url, temp_dir, filename, logger, min_bytes, 0,
                                      media_kind="audio", info=info):
            return True
        if V94_NO_ARIA2_ON_AUTH_FAIL and _v94_is_auth_fail(info):
            logger(f"    ↪ RANGE-FIRST audio {info.get('fail_reason')} → dừng, chờ SAFE-REFRESH")
            return False
        logger("    ↪ RANGE-FIRST audio (size=?) thất bại → DIRECT-1GET có deadline")
    if expected >= RANGE_AUDIO_MIN_BYTES:
        if _download_http_range_turbo(url, temp_dir, filename, logger, min_bytes, expected,
                                      media_kind="audio", info=info):
            return True
        if V94_NO_ARIA2_ON_AUTH_FAIL and _v94_is_auth_fail(info):
            logger(f"    ↪ RANGE-TURBO audio {info.get('fail_reason')} → BỎ aria2 (cùng signed URL), chờ SAFE-REFRESH")
            return False
        # The old DIRECT-1GET path once spent ~29.5 s on a 0.35 MiB Short audio.
        # Do not re-enter that path after a Range failure. The persistent aria2 RPC
        # daemon is already warm and is a safer bounded fallback.
        logger("    ↪ RANGE-TURBO audio thất bại → ARIA2 RPC (không quay lại DIRECT-1GET)")
        try:
            if _download_aria2c_rpc(url, temp_dir, filename, logger, min_bytes, expected_bytes=expected):
                return True
        except Exception as e:
            logger(f"    [ARIA2-RPC] ⚠️ audio fallback: {e}")
        return False

    # Extremely tiny/unknown-length audio is rare; keep the old direct path only here.
    if _download_direct_single_get(url, temp_dir, filename, logger, min_bytes, expected):
        return True
    return False


# ================== V9.4.1 FFMPEG WARM-UP + MUX DIAGNOSTICS ================== #
# Log live 17:09:51 (M1sbvQq1DAk): MUX COPY 0.602s trong khi ffmpeg concat + ffprobe ngay sau đó chỉ
# 0.11s, và offline cùng lệnh ~110ms ⇒ phần dư nằm ở lần spawn ffmpeg.exe ĐẦU TIÊN sau một quãng rảnh
# (nạp image/DLL, kiểm tra AV khi tạo process) hoặc AV quét file vừa ghi lúc mở. Hai việc:
#  1) làm nóng: 1 lần remux THẬT trên mẫu tí hon nhúng sẵn (cùng demuxer/muxer với 135/139/244/251),
#     chạy nền ngay khi media bắt đầu tải → mux thật (chạy sau ~1.5s) gặp ffmpeg đã nóng;
#  2) đo tách pha để log live sau phân biệt được: spawn / mở input / copy / thoát / quét AV / GIL.
_V94_FF_WARM_SAMPLES = {
    # H.264 Main CABAC, DASH fMP4 (ftyp/moov/sidx/moof/mdat) 64x64 0.3s — như itag 135 (4040 B)
    "v.mp4": (
        "AAAAIGZ0eXBpc281AAACAGlzbzVpc282bXA0MWRhc2gAAALnbW9vdgAAAGxtdmhkAAAAAAAAAAAAAAAAAAAD6AAAAAAAAQAAAQAA"
        "AAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAA"
        "Ael0cmFrAAAAXHRraGQAAAADAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAA"
        "AAAAAAAAAAAAAABAAAAAAEAAAABAAAAAAAGFbWRpYQAAACBtZGhkAAAAAAAAAAAAAAAAAAA8AAAAAABVxAAAAAAALWhkbHIAAAAA"
        "AAAAAHZpZGUAAAAAAAAAAAAAAABWaWRlb0hhbmRsZXIAAAABMG1pbmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAAHGRy"
        "ZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAAPBzdGJsAAAApHN0c2QAAAAAAAAAAQAAAJRhdmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAA"
        "AAAAAEAAQABIAAAASAAAAAAAAAABFUxhdmM2MC4zMS4xMDIgbGlieDI2NAAAAAAAAAAAAAAAGP//AAAALmF2Y0MBTUAK/+EAF2dN"
        "QArsohNgIgAAAwACAAADAHgeJEssAQAEaO+PyAAAABBwYXNwAAAAAQAAAAEAAAAQc3R0cwAAAAAAAAAAAAAAEHN0c2MAAAAAAAAA"
        "AAAAABRzdHN6AAAAAAAAAAAAAAAAAAAAEHN0Y28AAAAAAAAAAAAAAChtdmV4AAAAIHRyZXgAAAAAAAAAAQAAAAEAAAAAAAAAAAAA"
        "AAAAAABidWR0YQAAAFptZXRhAAAAAAAAACFoZGxyAAAAAAAAAABtZGlyYXBwbAAAAAAAAAAAAAAAAC1pbHN0AAAAJal0b28AAAAd"
        "ZGF0YQAAAAEAAAAATGF2ZjYwLjE2LjEwMAAAADRzaWR4AQAAAAAAAAEAADwAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAyNAAASAIAA"
        "AAAAAACwbW9vZgAAABBtZmhkAAAAAAAAAAEAAACYdHJhZgAAABx0ZmhkAAIAOAAAAAEAAAIAAAAGwgEBAAAAAAAUdGZkdAEAAAAA"
        "AAAAAAAAAAAAAGB0cnVuAAAKBQAAAAkAAAC4AgAAAAAABsIAAAQAAAAA6wAACAAAAAB3AAACAAAAAGcAAAIAAAABOAAACAAAAACV"
        "AAACAAAAAHQAAAIAAAAAmAAABgAAAABxAAACAAAAC91tZGF0AAACqwYF//+n3EXpvebZSLeWLNgg2SPu73gyNjQgLSBjb3JlIDE2"
        "NCByMzEwOCAzMWUxOWY5IC0gSC4yNjQvTVBFRy00IEFWQyBjb2RlYyAtIENvcHlsZWZ0IDIwMDMtMjAyMyAtIGh0dHA6Ly93d3cu"
        "dmlkZW9sYW4ub3JnL3gyNjQuaHRtbCAtIG9wdGlvbnM6IGNhYmFjPTEgcmVmPTEgZGVibG9jaz0xOjA6MCBhbmFseXNlPTB4MTow"
        "eDExMSBtZT1oZXggc3VibWU9MiBwc3k9MSBwc3lfcmQ9MS4wMDowLjAwIG1peGVkX3JlZj0wIG1lX3JhbmdlPTE2IGNocm9tYV9t"
        "ZT0xIHRyZWxsaXM9MCA4eDhkY3Q9MCBjcW09MCBkZWFkem9uZT0yMSwxMSBmYXN0X3Bza2lwPTEgY2hyb21hX3FwX29mZnNldD0w"
        "IHRocmVhZHM9MiBsb29rYWhlYWRfdGhyZWFkcz0xIHNsaWNlZF90aHJlYWRzPTAgbnI9MCBkZWNpbWF0ZT0xIGludGVybGFjZWQ9"
        "MCBibHVyYXlfY29tcGF0PTAgY29uc3RyYWluZWRfaW50cmE9MCBiZnJhbWVzPTIgYl9weXJhbWlkPTIgYl9hZGFwdD0xIGJfYmlh"
        "cz0wIGRpcmVjdD0xIHdlaWdodGI9MSBvcGVuX2dvcD0wIHdlaWdodHA9MSBrZXlpbnQ9MzAga2V5aW50X21pbj0zIHNjZW5lY3V0"
        "PTQwIGludHJhX3JlZnJlc2g9MCByY19sb29rYWhlYWQ9MTAgcmM9Y3JmIG1idHJlZT0xIGNyZj0yMy4wIHFjb21wPTAuNjAgcXBt"
        "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"
    ),
    # AAC-LC, DASH m4a (ftyp/moov/sidx/moof/mdat) 0.32s — như itag 139/140 (2149 B)
    "a.m4a": (
        "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"
    ),
    # VP9 profile 0 webm 64x64 0.3s — như itag 244 (4638 B)
    "v.webm": (
        "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"
    ),
    # Opus webm 0.31s — như itag 249-251 (1900 B)
    "a.webm": (
        "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"
        "zf5gHFO7a5G7j7OBALeK94EB8YIBxPCBAw=="
    ),
}

_V94_FF_LOCK = threading.Lock()
_V94_FF_STATE = {"last_exit": {}, "seq": 0, "prewarm_running": False, "prewarm_count": 0,
                 "assets_dir": None, "assets_err": "", "err_logged": False, "baseline": {}, "mux_running": 0}
_V94_FF_MARKS = (("in0", b"Input #0"), ("in1", b"Input #1"), ("out0", b"Output #0"),
                 ("final", b"muxing overhead"))
_V94_EPOCH_DIFF_100NS = 116444736000000000   # 1601-01-01 → 1970-01-01 (đơn vị 100ns)
_V94_MUX_DIAG = {}                            # str(output_file) → dict đo pha của lần mux đó


def _v94_ff_key(exe) -> str:
    try:
        b = os.path.basename(str(exe or "")).lower()
        return b[:-4] if b.endswith(".exe") else b
    except Exception:
        return "?"


def _v94_ff_mark_exit(exe):
    """Ghi thời điểm 1 process ffmpeg/ffprobe vừa thoát (để biết đã 'rảnh' bao lâu)."""
    try:
        with _V94_FF_LOCK:
            _V94_FF_STATE["last_exit"][_v94_ff_key(exe)] = time.perf_counter()
    except Exception:
        pass


def _v94_ff_idle_s(exe) -> float:
    """Số giây từ lần thoát gần nhất của exe này; -1 = chưa chạy lần nào trong phiên."""
    with _V94_FF_LOCK:
        t = _V94_FF_STATE["last_exit"].get(_v94_ff_key(exe))
    return -1.0 if t is None else max(0.0, time.perf_counter() - t)


def _v94_ff_next_seq() -> int:
    with _V94_FF_LOCK:
        _V94_FF_STATE["seq"] += 1
        return _V94_FF_STATE["seq"]


def _v94_preread(path, nbytes=65536) -> float:
    """Mở + đọc 64KiB như ffmpeg sẽ làm. Nếu AV quét-khi-mở thì thời gian chờ nằm ở đây (và được đo)."""
    t = time.perf_counter()
    try:
        with open(path, "rb") as fh:
            fh.read(nbytes)
    except OSError:
        return -1.0
    return round((time.perf_counter() - t) * 1000.0, 2)


def _v94_win_proc_times(handle):
    """Windows: thời điểm tạo/thoát process, CPU kernel+user, byte đọc/ghi (GetProcessTimes/IoCounters)."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ft = [wintypes.FILETIME() for _ in range(4)]
    if not k32.GetProcessTimes(wintypes.HANDLE(int(handle)), *[ctypes.byref(x) for x in ft]):
        return None
    v = [(x.dwHighDateTime << 32) | x.dwLowDateTime for x in ft]

    class _IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("ro", "wo", "oo", "rb", "wb", "ob")]
    io = _IO()
    k32.GetProcessIoCounters(wintypes.HANDLE(int(handle)), ctypes.byref(io))
    return {"create_ns": (v[0] - _V94_EPOCH_DIFF_100NS) * 100, "exit_ns": (v[1] - _V94_EPOCH_DIFF_100NS) * 100,
            "cpu_ms": (v[2] + v[3]) / 1e4, "read_mb": io.rb / 1048576.0, "write_mb": io.wb / 1048576.0}


def _v94_sys_busy_sample():
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            i, k, u = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
            ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(i), ctypes.byref(k), ctypes.byref(u))
            f = lambda x: (x.dwHighDateTime << 32) | x.dwLowDateTime
            return f(i), f(k) + f(u)          # thời gian kernel trên Windows đã gồm idle
        with open("/proc/stat") as fh:
            p = [int(x) for x in fh.readline().split()[1:]]
        return p[3] + p[4], sum(p)
    except Exception:
        return None


class _V94GilProbe:
    """Chỉ dùng khi V94_GIL_PROBE=1: đo độ trễ tối đa của 1 thread ngủ 5ms (GIL bị giữ) + thời gian GC gen2."""

    def __init__(self):
        self.max_late_ms = 0.0
        self.gc_ms = 0.0
        self._stop = threading.Event()
        self._gc_t0 = None

    def _gc_cb(self, phase, info):
        try:
            if info.get("generation") != 2:
                return
            if phase == "start":
                self._gc_t0 = time.perf_counter()
            elif self._gc_t0 is not None:
                self.gc_ms += (time.perf_counter() - self._gc_t0) * 1000.0
                self._gc_t0 = None
        except Exception:
            pass

    def _loop(self):
        while not self._stop.is_set():
            t = time.perf_counter()
            time.sleep(0.005)
            late = (time.perf_counter() - t - 0.005) * 1000.0
            if late > self.max_late_ms:
                self.max_late_ms = late

    def start(self):
        import gc
        gc.callbacks.append(self._gc_cb)
        threading.Thread(target=self._loop, name="V94GilProbe", daemon=True).start()

    def stop(self):
        import gc
        self._stop.set()
        try:
            gc.callbacks.remove(self._gc_cb)
        except ValueError:
            pass


def _v94_ff_run(cmd, timeout=120.0, inputs=(), preread=False, marks=_V94_FF_MARKS, low_priority=False):
    """Chạy ffmpeg/ffprobe y như subprocess.run(cmd, stdout=DEVNULL, stderr=PIPE, text=True,
    encoding='utf-8', errors='replace', creationflags=CREATE_NO_WINDOW, timeout=...) — cùng lệnh, cùng
    cờ, cùng text stderr — nhưng thêm dấu thời gian từng pha. Trả (CompletedProcess, diag dict)."""
    exe = cmd[0]
    d = {"seq": _v94_ff_next_seq(), "exe": _v94_ff_key(exe), "idle_s": round(_v94_ff_idle_s(exe), 1)}
    if preread:
        for k, pth in zip(("pre_v_ms", "pre_a_ms", "pre_x_ms"), inputs):
            d[k] = _v94_preread(pth)
    gp = None
    if V94_GIL_PROBE:
        try:
            gp = _V94GilProbe()
            gp.start()
        except Exception:
            gp = None
    s0 = _v94_sys_busy_sample()
    wall0_ns = time.time_ns()
    t0 = time.perf_counter()
    _cf = 0x08000000 if os.name == "nt" else 0
    if low_priority and os.name == "nt":
        _cf |= 0x00004000          # BELOW_NORMAL_PRIORITY_CLASS: làm nóng không tranh CPU với mux thật
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=_cf)
    t_popen = time.perf_counter()
    if low_priority and os.name != "nt":
        try:
            os.setpriority(os.PRIO_PROCESS, p.pid, 10)
        except Exception:
            pass
    got = {}
    chunks = []

    def _reader():
        try:
            for raw in iter(p.stderr.readline, b""):
                now = time.perf_counter()
                chunks.append(raw)
                for k, m in marks:
                    if k not in got and m in raw:
                        got[k] = now
        except Exception:
            pass
        got["eof"] = time.perf_counter()

    th = threading.Thread(target=_reader, name="V94FFStderr", daemon=True)
    th.start()
    # Chờ EOF của stderr (ffmpeg đóng stderr khi thoát) thay vì Popen.wait(timeout): trên POSIX wait(timeout)
    # thăm dò bằng sleep tăng dần tới 50ms → cộng thêm ~35-45ms vào mux (đo bằng bench pipeline).
    _deadline = time.perf_counter() + float(timeout)
    th.join(max(0.0, float(timeout)))
    try:
        if th.is_alive():
            raise subprocess.TimeoutExpired(cmd, timeout)
        rc = p.wait(max(0.05, _deadline - time.perf_counter()))
    except subprocess.TimeoutExpired:
        try:
            p.kill()
        except Exception:
            pass
        p.wait()
        th.join(2.0)
        _v94_ff_mark_exit(exe)
        if gp:
            gp.stop()
        raise
    try:
        p.stderr.close()
    except Exception:
        pass
    t_end = time.perf_counter()
    _v94_ff_mark_exit(exe)
    if gp:
        gp.stop()
        d["gil_max_ms"] = round(gp.max_late_ms, 1)
        d["gc2_ms"] = round(gp.gc_ms, 1)
    s1 = _v94_sys_busy_sample()

    def _ms(a, b):
        return round((a - b) * 1000.0, 1) if (a is not None and b is not None) else None
    d.update({"wall_ms": _ms(t_end, t0), "popen_ms": _ms(t_popen, t0),
              "start_in0_ms": _ms(got.get("in0"), t0), "in0_in1_ms": _ms(got.get("in1"), got.get("in0")),
              "in1_out0_ms": _ms(got.get("out0"), got.get("in1") or got.get("in0")),
              "copy_ms": _ms(got.get("final"), got.get("out0")), "exit_ms": _ms(got.get("eof"), got.get("final"))})
    if s0 and s1 and s1[1] > s0[1]:
        d["sys_busy_pct"] = round(100.0 * (1.0 - (s1[0] - s0[0]) / float(s1[1] - s0[1])), 1)
    if os.name == "nt":
        try:
            w = _v94_win_proc_times(p._handle)
            if w:
                d["create_lag_ms"] = round((w["create_ns"] - wall0_ns) / 1e6, 1)
                d["life_ms"] = round((w["exit_ns"] - w["create_ns"]) / 1e6, 1)
                d["cpu_ms"] = round(w["cpu_ms"], 1)
                d["exit_to_ret_ms"] = round((wall0_ns + (t_end - t0) * 1e9 - w["exit_ns"]) / 1e6, 1)
                d["read_mb"] = round(w["read_mb"], 2)
                d["write_mb"] = round(w["write_mb"], 2)
        except Exception:
            pass
    text = b"".join(chunks).decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    m = re.search(r"bench:\s*utime=([\d.]+)s\s+stime=([\d.]+)s\s+rtime=([\d.]+)s", text)
    if m:
        d["ff_rtime_ms"] = round(float(m.group(3)) * 1000.0, 1)
    return subprocess.CompletedProcess(cmd, rc, None, text), d


def _v94_fmt_ff_diag(d) -> str:
    """Phần đuôi gắn vào dòng MUX COPY (giữ nguyên tiền tố 'MUX COPY 480P+JA: X.XXXs')."""
    if not d:
        return ""
    keys = (("seq", "seq"), ("idle", "idle_s"), ("popen", "popen_ms"), ("→in0", "start_in0_ms"),
            ("→in1", "in0_in1_ms"), ("→out", "in1_out0_ms"), ("copy", "copy_ms"), ("exit", "exit_ms"),
            ("life", "life_ms"), ("cpu", "cpu_ms"), ("lag", "create_lag_ms"), ("ret", "exit_to_ret_ms"),
            ("rtime", "ff_rtime_ms"), ("sys%", "sys_busy_pct"), ("gil", "gil_max_ms"), ("gc", "gc2_ms"))
    parts = []
    if d.get("pre_v_ms") is not None or d.get("pre_a_ms") is not None:
        parts.append(f"pre={d.get('pre_v_ms')}/{d.get('pre_a_ms')}")
    for lab, k in keys:
        if d.get(k) is not None:
            parts.append(f"{lab}={d.get(k)}")
    return " | " + " ".join(parts)


def _v94_ff_warm_assets():
    """Ghi 4 mẫu tí hon (1 lần/tiến trình) vào %TEMP%\\v94_ffwarm — KHÔNG nằm trong _temp_yt480p."""
    with _V94_FF_LOCK:
        if _V94_FF_STATE["assets_dir"]:
            return _V94_FF_STATE["assets_dir"]
    try:
        import base64
        import tempfile
        d = os.path.join(tempfile.gettempdir(), "v94_ffwarm")
        os.makedirs(d, exist_ok=True)
        for name, b64 in _V94_FF_WARM_SAMPLES.items():
            raw = base64.b64decode(b64)
            pth = os.path.join(d, name)
            if not (os.path.isfile(pth) and os.path.getsize(pth) == len(raw)):
                tmp = pth + f".{os.getpid()}.tmp"
                with open(tmp, "wb") as fh:
                    fh.write(raw)
                os.replace(tmp, pth)
        with _V94_FF_LOCK:
            _V94_FF_STATE["assets_dir"] = d
        return d
    except Exception as e:
        with _V94_FF_LOCK:
            _V94_FF_STATE["assets_err"] = f"{type(e).__name__}: {e}"
        return None


def _v94_ff_prewarm(reason, job="-", logger=None):
    """1 lần remux thật (2 output: H.264+AAC và VP9+Opus) + 1 ffprobe trên mẫu tí hon. Không bao giờ raise,
    không chạm file của job; nếu đang có 1 prewarm khác chạy thì bỏ qua. Trả dict số đo hoặc None."""
    if not FFMPEG_PATH:
        return None
    with _V94_FF_LOCK:
        if _V94_FF_STATE["prewarm_running"]:
            return None
        _V94_FF_STATE["prewarm_running"] = True
    res = {"reason": str(reason), "job": str(job)}
    try:
        res["idle_s"] = round(_v94_ff_idle_s(FFMPEG_PATH), 1)
        d = _v94_ff_warm_assets()
        w1 = None
        if d:
            v, a, vw, aw = [os.path.join(d, n) for n in ("v.mp4", "a.m4a", "v.webm", "a.webm")]
            w1 = os.path.join(d, f"w1_{os.getpid()}.mp4")
            w2 = os.path.join(d, f"w2_{os.getpid()}.mp4")
            cmd = [FFMPEG_PATH, "-y", "-loglevel", "info", "-nostats", "-hide_banner",
                   "-i", v, "-i", a, "-i", vw, "-i", aw,
                   "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-shortest", w1,
                   "-map", "2:v:0", "-map", "3:a:0", "-c", "copy", "-shortest", w2]
        else:
            cmd = [FFMPEG_PATH, "-hide_banner", "-version"]
        t = time.perf_counter()
        _low = str(reason).upper().startswith(("JOB", "KEEP"))   # START chạy ưu tiên thường để đo baseline
        r, dg = _v94_ff_run(cmd, timeout=10.0, low_priority=_low)
        res["ffmpeg_ms"] = round((time.perf_counter() - t) * 1000.0, 1)
        res["ffmpeg_rc"] = r.returncode
        res["ffmpeg_popen_ms"] = dg.get("popen_ms")
        res["ffmpeg_life_ms"] = dg.get("life_ms")
        with _V94_FF_LOCK:
            _mux_busy = _V94_FF_STATE["mux_running"] > 0
        if FFPROBE_PATH and _low and _mux_busy:
            res["ffprobe_ms"] = "skip(mux)"        # mux thật đã bắt đầu → không tranh tài nguyên với nó
        elif FFPROBE_PATH:
            if w1 and os.path.isfile(w1):
                pcmd = [FFPROBE_PATH, "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", w1]
            else:
                pcmd = [FFPROBE_PATH, "-hide_banner", "-version"]
            t = time.perf_counter()
            r2, dg2 = _v94_ff_run(pcmd, timeout=10.0, marks=(), low_priority=_low)
            res["ffprobe_ms"] = round((time.perf_counter() - t) * 1000.0, 1)
            res["ffprobe_rc"] = r2.returncode
        with _V94_FF_LOCK:
            _V94_FF_STATE["prewarm_count"] += 1
        if logger:
            logger(f"    🔥 FF PREWARM [{job}] ffmpeg={res.get('ffmpeg_ms')}ms ffprobe={res.get('ffprobe_ms', '-')}ms "
                   f"idle={res['idle_s']}s ({reason})" + ("" if r.returncode == 0 else f" rc={r.returncode}"))
        TELEM.span(job, "ff_prewarm", **res)
        return res
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
        log_it = False
        with _V94_FF_LOCK:
            if not _V94_FF_STATE["err_logged"]:
                _V94_FF_STATE["err_logged"] = True
                log_it = True
        if logger and log_it:
            logger(f"    ⚠️ FF PREWARM lỗi (bỏ qua, không ảnh hưởng job): {res['error'][:160]}")
        TELEM.span(job, "ff_prewarm", **res)
        return res
    finally:
        with _V94_FF_LOCK:
            _V94_FF_STATE["prewarm_running"] = False


def _v94_drive_info(path) -> str:
    """Loại ổ (FIXED/REMOVABLE/REMOTE/...) + cờ OneDrive cho 1 thư mục; chỉ để log chẩn đoán."""
    try:
        p = os.path.abspath(str(path))
        kind = "?"
        if os.name == "nt":
            import ctypes
            root = os.path.splitdrive(p)[0] + "\\"
            kind = {0: "UNKNOWN", 1: "NOROOT", 2: "REMOVABLE", 3: "FIXED", 4: "REMOTE", 5: "CDROM",
                    6: "RAMDISK"}.get(int(ctypes.windll.kernel32.GetDriveTypeW(root)), "?")
        od = [os.environ.get(k) for k in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")]
        under_od = any(x and os.path.normcase(p).startswith(os.path.normcase(os.path.abspath(x))) for x in od)
        return kind + ("+OneDrive" if under_od else "")
    except Exception:
        return "?"


def _v94_exe_info(exe) -> str:
    try:
        st = os.stat(exe)
        s = f"{os.path.basename(exe)} {st.st_size/1048576:.0f}MB"
        attrs = getattr(st, "st_file_attributes", 0) or 0
        if attrs & 0x400000:
            s += " RECALL_ON_DATA_ACCESS"     # file OneDrive "online-only": mỗi lần chạy có thể phải tải về
        if attrs & 0x1000:
            s += " OFFLINE"
        return s
    except Exception:
        return f"{os.path.basename(str(exe or '?'))} ?"


def _v94_start_baseline(logger, out_dir=None, temp_dir=None):
    """Chạy nền lúc bấm START: import bảng client Innertube, làm nóng ffmpeg 2 lần (lần 1 = chi phí nguội,
    lần 2 = nóng), rồi in 1 dòng FF BASELINE để log live sau có mốc so sánh."""
    parts = []
    try:
        if V94_PREWARM_INNERTUBE:
            t = time.perf_counter()
            _get_innertube_client("visionos")
            _get_innertube_client("web_embedded")
            parts.append(f"innertube_import={(time.perf_counter()-t)*1000:.0f}ms")
    except Exception:
        pass
    if not (V94_FF_WARMUP_START and FFMPEG_PATH):
        if parts and logger:
            logger("🔥 FF BASELINE | " + " | ".join(parts))
        return
    try:
        r1 = _v94_ff_prewarm("START#1") or {}
        r2 = _v94_ff_prewarm("START#2") or {}
        ver = "?"
        try:
            rv = subprocess.run([FFMPEG_PATH, "-hide_banner", "-version"], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                timeout=10, creationflags=0x08000000 if os.name == "nt" else 0)
            _v94_ff_mark_exit(FFMPEG_PATH)
            ver = ((rv.stdout or "").strip().splitlines() or ["?"])[0].split(" Copyright")[0][:60]
        except Exception:
            pass
        with _V94_FF_LOCK:
            _V94_FF_STATE["baseline"] = {"cold": r1, "warm": r2, "version": ver}
        parts += [
            f"{ver}",
            f"cold ffmpeg={r1.get('ffmpeg_ms', '-')}ms ffprobe={r1.get('ffprobe_ms', '-')}ms",
            f"warm ffmpeg={r2.get('ffmpeg_ms', '-')}ms ffprobe={r2.get('ffprobe_ms', '-')}ms",
            f"exe: {_v94_exe_info(FFMPEG_PATH)}, {_v94_exe_info(FFPROBE_PATH) if FFPROBE_PATH else 'ffprobe ?'}",
            f"ổ: ffmpeg={_v94_drive_info(os.path.dirname(FFMPEG_PATH))}"
            + (f" temp={_v94_drive_info(temp_dir)}" if temp_dir else "")
            + (f" out={_v94_drive_info(out_dir)}" if out_dir else ""),
        ]
        if r1.get("error") or r2.get("error"):
            parts.append(f"lỗi={str(r1.get('error') or r2.get('error'))[:80]}")
        if logger:
            logger("🔥 FF BASELINE | " + " | ".join(parts))
        TELEM.span("-", "ff_baseline", cold=r1, warm=r2, version=ver,
                   ffmpeg_drive=_v94_drive_info(os.path.dirname(FFMPEG_PATH)))
    except Exception as e:
        if logger:
            logger(f"⚠️ FF BASELINE lỗi (bỏ qua): {type(e).__name__}: {e}")


_V94_MUX_INFO = {}


def _v94_parse_mux_log(text: str) -> dict:
    """Đọc stream của file OUTPUT từ log info của chính lệnh mux (khỏi spawn ffprobe ~50-70ms).
    Trả cùng khoá với _ffprobe_av_info()."""
    info = {"format_duration": 0.0, "video_duration": 0.0, "audio_duration": 0.0, "width": 0, "height": 0,
            "has_video": False, "has_audio": False, "source": "mux_log"}
    try:
        # Review V9.4.1: gắn Duration với ĐÚNG input (#0 video, #1 audio); "Duration: N/A" → không biết.
        _head = text.split("Output #0", 1)[0]
        _by = {}
        for _m in re.finditer(r"Input #(\d+),(.*?)(?=Input #\d+,|\Z)", _head, re.S):
            _d = re.search(r"Duration: (\d+):(\d+):([\d.]+)", _m.group(2))
            _by[int(_m.group(1))] = (int(_d.group(1)) * 3600 + int(_d.group(2)) * 60 + float(_d.group(3))) if _d else None
        if _by:
            durs = [(_by.get(0) or 0.0), (_by.get(1) or 0.0)]
            info["in_dur_unknown"] = (_by.get(0) is None or _by.get(1) is None)
        else:
            durs = [h * 3600 + m * 60 + sec for h, m, sec in
                    ((int(a), int(b), float(c)) for a, b, c in re.findall(r"Duration: (\d+):(\d+):([\d.]+)", text))]
            info["in_dur_unknown"] = len(durs) < 2
        out = text.split("Output #0", 1)
        if len(out) < 2:
            return info
        for line in out[1].splitlines():
            if "Stream #0:" not in line:
                continue
            if ": Video:" in line and not info["has_video"]:
                info["has_video"] = True
                m = re.search(r"(\d{2,5})x(\d{2,5})", line.split(": Video:", 1)[1])
                if m:
                    info["width"], info["height"] = int(m.group(1)), int(m.group(2))
            elif ": Audio:" in line and not info["has_audio"]:
                info["has_audio"] = True
        if durs:
            info["video_duration"] = durs[0]
            info["audio_duration"] = durs[1] if len(durs) > 1 else 0.0
            info["format_duration"] = min(x for x in durs[:2] if x > 0) if any(x > 0 for x in durs[:2]) else 0.0
        # V9.4.1: các dòng "Duration:" là của INPUT (header) — file input bị cắt cụt vẫn khai đủ thời lượng.
        # Sự thật về OUTPUT nằm ở báo cáo cuối của ffmpeg (có cả khi -nostats): "time=" cuối cùng và
        # "video:NkB audio:NkB" (ffmpeg >= 7 in KiB). Kèm đếm dòng báo lỗi đọc input.
        t = re.findall(r"time=\s*(-?\d+):(\d+):([\d.]+)", text)
        if t:
            h, mnt, sec = t[-1]
            info["out_time"] = int(h) * 3600 + int(mnt) * 60 + float(sec)
        k = re.findall(r"video:\s*(\d+)\s*(?:kB|KiB)\s+audio:\s*(\d+)\s*(?:kB|KiB)", text)
        if k:
            info["out_video_kb"], info["out_audio_kb"] = int(k[-1][0]), int(k[-1][1])
        info["bad_lines"] = sum(1 for ln in text.splitlines()
                                if ("Invalid NAL" in ln or "partial file" in ln
                                    or "error while decoding" in ln.lower() or "corrupt" in ln.lower()))
    except Exception:
        pass
    return info


def _v94_strict_mux_check(info: dict):
    """(ok, reason) cho file OUTPUT của mux dựa trên báo cáo cuối của ffmpeg. Thiếu dữ liệu → (None, lý do)
    để caller rơi về ffprobe. Dung sai cho -shortest: max(0.3s, 1% của input ngắn nhất)."""
    try:
        if info.get("in_dur_unknown"):
            return None, "input_duration_unknown"
        ins = [x for x in (info.get("video_duration"), info.get("audio_duration")) if x and x > 0]
        if not ins:
            return None, "no_input_duration"
        min_in = min(ins)
        tol = max(0.3, 0.01 * min_in)
        if info.get("out_time") is None:
            return None, "no_out_time"
        if info.get("out_video_kb") is None or info.get("out_audio_kb") is None:
            return None, "no_stream_sizes"
        if int(info.get("bad_lines") or 0) > 0:
            return False, f"bad_lines={info.get('bad_lines')}"
        if not (int(info["out_video_kb"]) > 0 and int(info["out_audio_kb"]) > 0):
            return False, f"empty_stream v={info['out_video_kb']}kB a={info['out_audio_kb']}kB"
        if float(info["out_time"]) < min_in - tol:
            return False, f"short_output {float(info['out_time']):.2f}s<{min_in:.2f}s"
        return True, "OK"
    except Exception as e:
        return None, f"check_error:{type(e).__name__}"


def mux_480_stream_copy(video_file: Path, audio_file: Path, output_file: Path, logger) -> bool:
    """Zero-reencode mux: HARD 480p video (244/135) + Japanese audio into MP4. No +faststart rewrite."""
    if not FFMPEG_PATH:
        logger("❌ Không tìm thấy ffmpeg để ghép 480p + audio.")
        return False
    try:
        if output_file.exists():
            output_file.unlink()
    except OSError:
        pass
    t0 = time.perf_counter()
    _mux_log = (V94_VALIDATE_MODE == "mux_log")
    _diag_level = int(V94_MUX_DIAG or 0)
    cmd = [
        FFMPEG_PATH, "-y", "-loglevel", ("info" if _mux_log else "error"), "-nostats", "-hide_banner",
        "-i", str(video_file), "-i", str(audio_file),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c", "copy", "-shortest",
        str(output_file),
    ]
    if _diag_level >= 2:
        cmd.insert(cmd.index("-hide_banner") + 1, "-benchmark")
    _dg = None
    with _V94_FF_LOCK:
        _V94_FF_STATE["mux_running"] += 1
    try:
        if _diag_level > 0:
            # V9.4.1: cùng lệnh/cờ/timeout/text, chỉ thêm dấu thời gian từng pha (xem _v94_ff_run).
            r, _dg = _v94_ff_run(cmd, timeout=120, inputs=(video_file, audio_file),
                                 preread=bool(V94_MUX_PREREAD))
        else:
            r = subprocess.run(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                creationflags=0x08000000 if os.name == "nt" else 0,
                timeout=120,
            )
            _v94_ff_mark_exit(FFMPEG_PATH)
        if _dg is not None:
            _V94_MUX_DIAG[str(output_file)] = _dg
        if r.returncode == 0 and output_file.is_file() and output_file.stat().st_size > 256 * 1024:
            logger(f"    ⚡ MUX COPY 480P+JA: {time.perf_counter()-t0:.3f}s | không re-encode" + _v94_fmt_ff_diag(_dg))
            if _mux_log:
                _V94_MUX_INFO[str(output_file)] = _v94_parse_mux_log(r.stderr or "")
            return True
        _err = (r.stderr or "").strip()
        if _mux_log:
            _err = "\n".join(l for l in _err.splitlines() if "rror" in l or "nvalid" in l or "atches no" in l) or _err
        logger(f"    ❌ MUX lỗi: {_err[-300:]}")
    except Exception as e:
        logger(f"    ❌ MUX exception: {type(e).__name__}: {e}")
    finally:
        with _V94_FF_LOCK:
            _V94_FF_STATE["mux_running"] = max(0, _V94_FF_STATE["mux_running"] - 1)
    return False

# ================== ARIA2 PERSISTENT RPC TURBO ================== #
def _pick_free_local_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _aria2_rpc_call(method: str, params=None, timeout=0.35):
    if not _ARIA2_RPC_PORT or not _ARIA2_RPC_SECRET:
        return None
    token_params = [f"token:{_ARIA2_RPC_SECRET}"] + list(params or [])
    body = json.dumps({
        "jsonrpc": "2.0", "id": "turbo", "method": method, "params": token_params
    }, separators=(",", ":")).encode("utf-8")
    conn = getattr(_ARIA2_RPC_TLS, "conn", None)
    for attempt in range(2):
        try:
            if conn is None:
                conn = http.client.HTTPConnection("127.0.0.1", _ARIA2_RPC_PORT, timeout=timeout)
                _ARIA2_RPC_TLS.conn = conn
            conn.request("POST", "/jsonrpc", body=body, headers={
                "Content-Type": "application/json", "Connection": "keep-alive"
            })
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status != 200:
                raise OSError(f"RPC HTTP {resp.status}")
            obj = json.loads(raw.decode("utf-8", errors="replace"))
            return obj.get("result")
        except Exception:
            try:
                if conn: conn.close()
            except Exception:
                pass
            conn = None
            _ARIA2_RPC_TLS.conn = None
            if attempt:
                return None
    return None


def _start_aria2_rpc_daemon():
    global _ARIA2_RPC_PROC, _ARIA2_RPC_PORT, _ARIA2_RPC_SECRET
    if not ARIA2C_PATH:
        return False
    with _ARIA2_RPC_LOCK:
        if _ARIA2_RPC_PROC is not None and _ARIA2_RPC_PROC.poll() is None and _ARIA2_RPC_PORT:
            return True
        try:
            port = _pick_free_local_port()
            secret = f"yt{os.getpid()}{int(time.time()*1000)}"
            cmd = [
                ARIA2C_PATH,
                "--enable-rpc=true",
                "--rpc-listen-all=false",
                f"--rpc-listen-port={port}",
                f"--rpc-secret={secret}",
                "--console-log-level=error",
                "--summary-interval=0",
                "--download-result=hide",
                "--file-allocation=none",
                "--disk-cache=64M",
                "--max-concurrent-downloads=16",
                "--auto-file-renaming=false",
                "--allow-overwrite=true",
            ]
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, creationflags=flags
            )
            _ARIA2_RPC_PROC = proc
            _ARIA2_RPC_PORT = port
            _ARIA2_RPC_SECRET = secret
            deadline = time.perf_counter() + 1.2
            while time.perf_counter() < deadline:
                if proc.poll() is not None:
                    break
                if _aria2_rpc_call("aria2.getVersion", timeout=0.15):
                    return True
                time.sleep(0.025)
        except Exception:
            pass
        try:
            if _ARIA2_RPC_PROC is not None and _ARIA2_RPC_PROC.poll() is None:
                _ARIA2_RPC_PROC.terminate()
        except Exception:
            pass
        _ARIA2_RPC_PROC = None
        _ARIA2_RPC_PORT = None
        _ARIA2_RPC_SECRET = None
        return False


def _stop_aria2_rpc_daemon():
    global _ARIA2_RPC_PROC
    try:
        if _ARIA2_RPC_PORT and _ARIA2_RPC_SECRET:
            _aria2_rpc_call("aria2.shutdown", timeout=0.2)
    except Exception:
        pass
    try:
        if _ARIA2_RPC_PROC is not None and _ARIA2_RPC_PROC.poll() is None:
            _ARIA2_RPC_PROC.terminate()
    except Exception:
        pass
    _ARIA2_RPC_PROC = None


atexit.register(_stop_aria2_rpc_daemon)


def _aria2_tuned_split(expected_bytes: int):
    mb = (expected_bytes or 0) / (1024 * 1024)
    if mb and mb <= 4:
        return 2
    if mb and mb <= 12:
        return 6
    if mb and mb <= 48:
        return 12
    if mb and mb <= 128:
        return 20
    return 24


def _download_aria2c_rpc(url: str, temp_dir: Path, filename: str, logger, min_bytes: int, expected_bytes: int = 0) -> bool:
    if not _start_aria2_rpc_daemon():
        return False
    temp_file = temp_dir / filename
    for p in (temp_file, Path(str(temp_file) + ".aria2")):
        try:
            if p.exists(): p.unlink()
        except Exception:
            pass

    split = _aria2_tuned_split(expected_bytes)
    logger(f"    [ARIA2-RPC] ⚡ Turbo daemon | split<={split} | {filename}")
    opts = {
        "dir": str(temp_dir),
        "out": filename,
        "split": str(split),
        "max-connection-per-server": str(split),
        "min-split-size": "1M",
        "file-allocation": "none",
        "allow-overwrite": "true",
        "auto-file-renaming": "false",
        "max-tries": "2",
        "retry-wait": "0",
        "connect-timeout": "2",
        "timeout": "4",
        "lowest-speed-limit": "0",
        "stream-piece-selector": "default",
        "reuse-uri": "true",
    }
    gid = _aria2_rpc_call("aria2.addUri", [[url], opts], timeout=0.25)
    if not gid:
        return False

    deadline = time.perf_counter() + 180.0
    status = None
    try:
        while time.perf_counter() < deadline:
            status = _aria2_rpc_call(
                "aria2.tellStatus", [gid, ["status", "completedLength", "totalLength", "errorCode", "errorMessage"]],
                timeout=0.20
            )
            if isinstance(status, dict):
                st = status.get("status")
                if st == "complete":
                    break
                if st in ("error", "removed"):
                    return False
            time.sleep(0.015)

        if not isinstance(status, dict) or status.get("status") != "complete":
            return False
        if temp_file.exists() and temp_file.stat().st_size >= min_bytes:
            size = temp_file.stat().st_size
            logger(f"    [ARIA2-RPC] ✅ HOÀN TẤT - {size/1024/1024:.1f}MB")
            return True
        return False
    finally:
        try:
            # Nếu RPC bị timeout/mất kết nối khi download còn active, dừng item trước khi fallback
            # để không có 2 downloader cùng ghi vào một file.
            if not (isinstance(status, dict) and status.get("status") == "complete"):
                _aria2_rpc_call("aria2.forceRemove", [gid], timeout=0.15)
                time.sleep(0.02)
            _aria2_rpc_call("aria2.removeDownloadResult", [gid], timeout=0.15)
        except Exception:
            pass


# --- HÀM TẢI BẰNG ARIA2C 16 LUỒNG CHUẨN ---
def _download_aria2c_legacy(url: str, temp_dir: Path, filename: str, logger, min_bytes: int) -> bool:
    temp_file = temp_dir / filename
    if os.path.exists(temp_file):
        try: os.remove(temp_file)
        except: pass
    
    logger(f"    [ARIA2C] 🚀 Đang kéo 16 luồng: {filename}")
    
    if not ARIA2C_PATH:
        logger(f"    ❌ LỖI: Không tìm thấy file aria2c.exe")
        return False
        
    try:
        cmd = [
            ARIA2C_PATH,
            "-x", "16",
            "-s", "16",
            "-k", "1M",
            "--console-log-level=error", 
            "--summary-interval=0", 
            "--file-allocation=none", 
            "-d", str(temp_dir),
            "-o", filename,
            url
        ]
        
        res = subprocess.run(cmd, capture_output=True, text=True)
        
        if res.returncode == 0 and temp_file.exists():
            current_size = temp_file.stat().st_size
            if current_size >= min_bytes:
                logger(f"    [ARIA2C] ✅ HOÀN TẤT - {current_size/1024/1024:.1f}MB")
                return True
            else:
                logger(f"    [ARIA2C] ⚠️ Cảnh báo: File tải về quá nhỏ ({current_size} bytes)")
                return False
        else:
            logger(f"    [ARIA2C] ❌ Lỗi Tải: {res.stderr.strip()[-100:]}")
            return False
            
    except Exception as e:
        logger(f"    [ARIA2C] ❌ Lỗi Exception: {e}")
        return False


def download_aria2c_ultimate(url: str, temp_dir: Path, filename: str, logger, min_bytes: int, expected_bytes: int = 0) -> bool:
    """Ưu tiên persistent RPC daemon; nếu bất kỳ lỗi nào thì rơi về subprocess bản cũ."""
    try:
        if _download_aria2c_rpc(url, temp_dir, filename, logger, min_bytes, expected_bytes=expected_bytes):
            return True
    except Exception as e:
        logger(f"    [ARIA2-RPC] ⚠️ Fallback legacy: {e}")
    try:
        ctrl = Path(str(temp_dir / filename) + ".aria2")
        if ctrl.exists(): ctrl.unlink()
    except Exception:
        pass
    return _download_aria2c_legacy(url, temp_dir, filename, logger, min_bytes)


def _windows_file_exclusive_ready(file_path: Path) -> bool:
    """True khi Windows cho mở file với share-mode=0 (IDM đã nhả handle)."""
    if os.name != "nt":
        try:
            with open(file_path, "rb"):
                return True
        except OSError:
            return False

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        CreateFileW = kernel32.CreateFileW
        CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE
        ]
        CreateFileW.restype = wintypes.HANDLE

        CloseHandle = kernel32.CloseHandle
        CloseHandle.argtypes = [wintypes.HANDLE]
        CloseHandle.restype = wintypes.BOOL

        GENERIC_READ = 0x80000000
        OPEN_EXISTING = 3
        FILE_ATTRIBUTE_NORMAL = 0x00000080
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        h = CreateFileW(
            str(file_path),
            GENERIC_READ,
            0,  # share-mode = 0: phải không còn process nào giữ file
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if h == INVALID_HANDLE_VALUE:
            return False
        CloseHandle(h)
        return True
    except Exception:
        return False


def _move_file_fast_retry(src: Path, dst: Path, logger, timeout_sec: float = 8.0, stats: dict = None) -> bool:
    """Move nhanh nhưng retry khi Windows/IDM vừa mới nhả handle; luôn có log nếu lỗi."""
    deadline = time.monotonic() + max(0.5, float(timeout_sec))
    last_err = None
    while time.monotonic() < deadline:
        try:
            if dst.exists():
                dst.unlink()
            shutil.move(str(src), str(dst))
            return True
        except (PermissionError, OSError) as e:
            last_err = e
            if isinstance(stats, dict):
                stats["retries"] = int(stats.get("retries", 0)) + 1
            time.sleep(0.05)
    logger(f"❌ MOVE sau IDM thất bại: {last_err}")
    return False


def download_idm_ultimate(url: str, temp_dir: Path, filename: str, logger, min_bytes: int,
                          timeout_sec: int = 1800, media_label: str = "media") -> bool:
    """Gửi DIRECT URL cho IDM và chờ file đích hoàn tất trước khi hậu xử lý."""
    temp_file = temp_dir / filename
    ensure_dir(temp_dir)

    if temp_file.exists():
        try:
            temp_file.unlink()
        except OSError:
            pass

    if not IDM_PATH:
        logger("    ❌ IDM: Không tìm thấy IDMan.exe (Registry/Program Files/cạnh tool).")
        return False

    logger(f"    [IDM] 🚀 Gửi direct link {media_label} sang IDM: {filename}")
    logger(f"    [IDM] 📍 IDMan.exe: {IDM_PATH}")

    # Theo CLI chính thức của IDM:
    # /d URL = tải URL, /p = folder đích, /f = tên file, /n = silent mode.
    # KHÔNG dùng /a vì /a chỉ thêm vào queue mà không bắt đầu tải.
    cmd = [
        IDM_PATH,
        "/d", url,
        "/p", str(temp_dir),
        "/f", filename,
        "/n",
    ]

    try:
        create_no_window = 0x08000000 if os.name == "nt" else 0
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=create_no_window,
        )
    except Exception as e:
        logger(f"    [IDM] ❌ Không gọi được IDMan.exe: {e}")
        return False

    # KHÔNG dùng riêng size/mtime để kết luận xong: IDM có thể đã tạo đủ kích thước
    # nhưng vẫn đang giữ HANDLE để finalize file. Trên Windows ta chỉ cho đi tiếp khi:
    # 1) size + mtime đứng yên rất ngắn (0.20s), VÀ
    # 2) CreateFileW mở được file với share-mode=0 => IDM đã thật sự nhả handle.
    deadline = time.monotonic() + max(30, int(timeout_sec))
    saw_target = False
    last_progress_log = 0.0
    stable_since = None
    stable_size = -1
    stable_mtime_ns = -1
    confirm_window = 0.20
    poll_interval = 0.05
    waiting_handle_logged = False

    while time.monotonic() < deadline:
        try:
            if temp_file.is_file():
                saw_target = True
                st = temp_file.stat()
                size = st.st_size
                mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))
                now = time.monotonic()

                if now - last_progress_log >= 5.0:
                    logger(f"    [IDM] ⏳ File hiện tại: {size/1024/1024:.1f}MB")
                    last_progress_log = now

                if size >= min_bytes:
                    if size == stable_size and mtime_ns == stable_mtime_ns:
                        if stable_since is None:
                            stable_since = now
                        elif now - stable_since >= confirm_window:
                            if _windows_file_exclusive_ready(temp_file):
                                # Chốt thêm 1 lần sau 50ms để tránh khe hở giữa 2 handle của IDM.
                                time.sleep(0.05)
                                st2 = temp_file.stat()
                                mtime2_ns = getattr(st2, "st_mtime_ns", int(st2.st_mtime * 1_000_000_000))
                                if (
                                    st2.st_size == size
                                    and mtime2_ns == mtime_ns
                                    and _windows_file_exclusive_ready(temp_file)
                                ):
                                    logger(
                                        f"    [IDM] ✅ HOÀN TẤT READY - "
                                        f"{size/1024/1024:.1f}MB | IDM đã nhả file"
                                    )
                                    return True
                            elif not waiting_handle_logged:
                                logger("    [IDM] ⚡ Đủ dung lượng, đang chờ IDM nhả file...")
                                waiting_handle_logged = True
                    else:
                        stable_size = size
                        stable_mtime_ns = mtime_ns
                        stable_since = now
                        waiting_handle_logged = False
                else:
                    stable_since = None
                    stable_size = size
                    stable_mtime_ns = mtime_ns
                    waiting_handle_logged = False
        except OSError:
            stable_since = None
            stable_size = -1
            stable_mtime_ns = -1
            waiting_handle_logged = False

        time.sleep(poll_interval)

    if saw_target and temp_file.exists():
        try:
            size = temp_file.stat().st_size
        except OSError:
            size = 0
        logger(f"    [IDM] ❌ Timeout, file chưa xác nhận hoàn tất ({size/1024/1024:.1f}MB).")
    else:
        logger("    [IDM] ❌ Timeout: IDM chưa tạo được file đích.")
    return False


# ===================== GUI WORKER =====================
class WorkerSignals(QObject):
    log_message = Signal(str)
    finished = Signal()
    url_finished = Signal(str, int, bool)

class Worker(threading.Thread):
    MIN_BYTES_VIDEO = 256 * 1024 # 480p video-only; short clips can be small

    def __init__(self, master_app, excel_path, url_col_name, status_col_name):
        super().__init__(daemon=True)
        self.signals = master_app.worker_signals 
        self.master = master_app 
        self.excel_path = excel_path
        self.url_col_name = url_col_name
        self.status_col_name = status_col_name
        self.stop_flag = False
        self.log = partial(self.signals.log_message.emit)

        self.out_dir        = Path(self.master.out_entry.text().strip())
        self.temp_dir       = self.out_dir.parent / "_temp_yt480p"
        ensure_dir(self.out_dir); ensure_dir(self.temp_dir)
        self.keep_temp      = self.master.keep_temp_checkbox.isChecked()
        self.final_move_dir = self.master.final_move_entry.text().strip()
        self.download_engine = (self.master.combo_downloader.currentData() or "aria2c").lower()
        # V9.5: chế độ chất lượng chụp 1 lần lúc tạo Worker (đổi radio giữa chừng không ảnh hưởng job đang chạy).
        try:
            _qm = self.master.quality_mode() if callable(getattr(self.master, "quality_mode", None)) else "480hard"
        except Exception:
            _qm = "480hard"
        self.quality_mode = "360fast" if (V95_360P and _qm == "360fast") else "480hard"

        if not yt_dlp:
            self.log("❌ Thiếu thư viện yt-dlp.")
            self._critical_error = True
        elif self.download_engine == "idm" and not IDM_PATH:
            self.log("❌ Đã chọn IDM nhưng không tìm thấy IDMan.exe.")
            self._critical_error = True
        elif self.download_engine == "aria2c" and not ARIA2C_PATH:
            self.log("❌ Đã chọn Aria2c nhưng không tìm thấy aria2c.exe.")
            self._critical_error = True
        elif not FFMPEG_PATH:
            self.log("❌ Không tìm thấy ffmpeg; HARD 480p 244/135 cần ghép audio bằng stream-copy.")
            self._critical_error = True
        else:
            self._critical_error = False
            
        self.links_with_index = []

    # --- BẮT LINK 360P: 3-CLIENT NATIVE RACE -> WARM yt-dlp FAST -> WARM ROBUST/EJS/PO ---
    def _quick_fetch_ytdlp_360p(self, url, robust=False):
        try:
            fut = _YTDLP_RESOLVER_EXECUTOR.submit(_ytdlp_resolve_on_warm_thread, url, robust)
            return fut.result(timeout=10.0 if robust else 4.0)
        except Exception as e:
            if robust:
                self.log(f"⚠️ yt-dlp robust/EJS/PO fallback lỗi: {type(e).__name__}: {e}")
            return None

    def _quick_fetch_native_360p(self, url):
        # Tầng 1: 3 Innertube clients song song, connection giữ nóng.
        data = _fast_innertube_360p(url)
        if data:
            return data

        # Tầng 2: dedicated warm yt-dlp resolver thread, minimal request path.
        data = self._quick_fetch_ytdlp_360p(url, robust=False)
        if data:
            return data

        # Tầng 3: yt-dlp robust để EJS/PO-token provider plugin xử lý video khó.
        return self._quick_fetch_ytdlp_360p(url, robust=True)

    def _cut_after_merge(self, final_mp4: Path, base_name: str, url_index: int,
                         audio_file: Path = None, duration_hint: float = 0.0):
        """HẬU XỬ LÝ THÔNG MINH — hậu xử lý sau khi 480p+JA đã sẵn sàng.

        V8.8: audio_file != None → final_mp4 là video-only (webm/mp4), chỉ dùng cho nhánh >300s
        (DIRECT-CUT, không MUX)."""
        try:
            _kp = self._v94_kpi()
            if float(duration_hint or 0.0) > 0:
                duration = float(duration_hint)
                _kp["duration_source"] = "hint"
            else:
                _t_pp = time.perf_counter()
                duration = _ffprobe_duration_seconds(FFPROBE_PATH, final_mp4)
                _kp["pp_probe_ms"] = round((time.perf_counter() - _t_pp) * 1000.0, 1)
                _kp["duration_source"] = "ffprobe"
            if duration <= 0:
                self.log(f"❌ Không đọc được thời lượng: {final_mp4.name}")
                return False

            # TITLE thật: ưu tiên Excel TITLE/TIÊU ĐỀ.
            # Nếu Excel trống, dùng title YouTube ĐÃ CÓ SẴN trong base_name,
            # tuyệt đối không gọi lại yt-dlp / không chạm engine tải.
            original_title = ""
            _t_xl = time.perf_counter()
            if url_index is not None and self.excel_path:
                original_title = read_original_title_from_excel(
                    self.excel_path, url_index, self.log
                )

            if not original_title:
                # base_name hiện có dạng: "<YouTube title> [VIDEO_ID]"
                original_title = re.sub(
                    r"\s*\[[A-Za-z0-9_-]{11}\]\s*$", "", base_name
                ).strip() or base_name

            self.log(f"🧠 SMART LOGIC: thời lượng = {duration:.2f}s")
            self.log(f"    🧠 TITLE dùng để đặt tên: {original_title}")

            final_move_base = Path(self.final_move_dir) if self.final_move_dir else None
            if final_move_base:
                ensure_dir(final_move_base)

            link_video_goc = ""
            if url_index is not None and self.excel_path:
                link_video_goc = read_excel_cell_value(
                    self.excel_path, url_index, "LINK VIDEO GỐC", self.log
                )
            _kp["pp_excel_ms"] = round((time.perf_counter() - _t_xl) * 1000.0, 1)

            target_move_dir = (
                final_move_base / link_video_goc
                if (final_move_base and link_video_goc)
                else final_move_base
            )
            if target_move_dir:
                ensure_dir(target_move_dir)

            def move_whole_folder(source_folder: Path):
                if not source_folder.is_dir():
                    self.log(f"❌ Folder không tồn tại: {source_folder}")
                    return None

                if not target_move_dir:
                    self.log(f"📁 Chưa cấu hình MOVE cuối; giữ tại: {source_folder}")
                    return source_folder

                dest = target_move_dir / source_folder.name
                if dest.exists():
                    dest = target_move_dir / f"{source_folder.name}_{int(time.time())}"

                _t_mv = time.perf_counter()
                try:
                    _kp["same_volume"] = bool(os.stat(str(source_folder)).st_dev == os.stat(str(target_move_dir)).st_dev)
                except OSError:
                    pass
                shutil.move(str(source_folder), str(dest))
                _kp["pp_move_ms"] = round((time.perf_counter() - _t_mv) * 1000.0, 1)
                self.log(f"📦 MOVE CẢ FOLDER: {source_folder.name} → {dest}")
                return dest

            def package_single(video_file: Path):
                smart_name = generate_dynamic_jp_single_name(original_title)
                package_dir = self.out_dir / smart_name
                if package_dir.exists():
                    package_dir = self.out_dir / f"{smart_name}_{int(time.time())}"
                ensure_dir(package_dir)

                renamed = package_dir / f"{smart_name}{video_file.suffix}"
                shutil.move(str(video_file), str(renamed))
                self.log(f"🧠 TÊN RANDOM: {renamed.name}")

                moved = move_whole_folder(package_dir)
                if moved is not None:
                    _kp["final_file"] = str(Path(moved) / renamed.name)
                return moved is not None

            # ==============================================================
            # 1) <60s: lấy ĐUÔI CHÍNH VIDEO đưa lên ĐẦU → mục tiêu 60.20s
            # ==============================================================
            if duration < 60.0:
                self.log(
                    "⚡ NHÁNH <60s: tự lấy đoạn CUỐI đưa lên ĐẦU "
                    "→ mục tiêu >=60.20s."
                )

                smart_name = generate_dynamic_jp_single_name(original_title)
                package_dir = self.out_dir / smart_name
                if package_dir.exists():
                    package_dir = self.out_dir / f"{smart_name}_{int(time.time())}"
                ensure_dir(package_dir)

                concat_output = package_dir / f"{smart_name}{final_mp4.suffix or '.mp4'}"
                new_duration = prepend_tail_to_front_fast(
                    final_mp4, concat_output, duration, self.log, stats=_kp
                )

                if new_duration < 60.0:
                    shutil.rmtree(package_dir, ignore_errors=True)
                    return False

                self.log(
                    f"✅ VIDEO <60s ĐÃ BÙ: {duration:.2f}s → {new_duration:.2f}s"
                )

                moved = move_whole_folder(package_dir)
                if moved is None:
                    return False
                _kp["final_file"] = str(Path(moved) / concat_output.name)

                # File gốc tải về chỉ là file trung gian sau khi đã tạo bản >=60s.
                if final_mp4.exists() and not self.keep_temp:
                    try:
                        final_mp4.unlink()
                    except OSError:
                        pass
                return True

            # ==============================================================
            # 2) 60–300s: KHÔNG CẮT — chỉ random title + folder + MOVE
            # ==============================================================
            if duration <= 300.0:
                self.log(
                    "📦 NHÁNH 60–300s: KHÔNG CẮT | random title + MOVE cả folder."
                )
                return package_single(final_mp4)

            # ==============================================================
            # 3) >300s: cắt chống trùng timeline/frame
            # ==============================================================
            self.log(
                "✂️ NHÁNH >300s: cắt theo Cut Seconds | "
                "Part đầu bỏ 6s | Part sau +2s anti-overlap."
            )

            cutsecs_str = self.master.cutsec_entry.text().strip()
            try:
                cutsecs = int(cutsecs_str)
            except Exception:
                cutsecs = 0

            if cutsecs <= 0:
                self.log("❌ Video >5 phút nhưng Cut Seconds <= 0.")
                return False

            # Theo logic hôm qua: folder CUT luôn nằm ngay trong Folder Download.
            target_dir = self.out_dir / f"{sanitize_name(base_name)}_CUT"
            if target_dir.exists():
                target_dir = self.out_dir / f"{target_dir.name}_{int(time.time())}"
            ensure_dir(target_dir)

            if not split_video_copy_parallel(
                final_mp4, target_dir, cutsecs, original_title, self.log,
                audio_path=audio_file, duration=duration,
            ):
                self.log("❌ Cắt thất bại; không MOVE và không ghi DONE.")
                return False

            moved = move_whole_folder(target_dir)
            if moved is None:
                return False
            _kp["final_file"] = str(moved)          # thư mục part (ffprobe nền 360p sẽ kiểm mọi *.mp4 bên trong)

            if final_mp4.exists() and not self.keep_temp:
                try:
                    final_mp4.unlink()
                except OSError:
                    pass

            return True

        except Exception as e:
            self.log(f"❌ Lỗi hậu xử lý Smart Title/Cut/Move: {e}")
            return False

    # --- V8.6 FRESH-SHORT NATIVE-FIRST: 244→135 + instant single-track JA + shared-redirect Range ---
    def _process_single_url(self, url: str, url_index: int):
        """V9.4 wrapper: đếm job đang chạy (keepalive nhường), ghi span + bản tóm tắt KPI JSONL."""
        vid = _extract_youtube_video_id(url) or "UNKNOWN"
        self._v94 = {"job": vid, "t0_ns": time.perf_counter_ns(), "cpu0": time.process_time(),
                     "video": {}, "audio": {}, "kpi": {}, "fail": ""}
        _t0n = self._v94["t0_ns"]
        _sub = getattr(self, "_v94_submit_ns", None)
        _wk = getattr(self, "_v94_wake_ns", None)
        if _sub:
            self._v94["kpi"]["queue_ms"] = round((_t0n - _sub) / 1e6, 2)      # chờ slot Max Threads
        if _wk:
            self._v94["kpi"]["detect_ms"] = round((_t0n - _wk) / 1e6, 2)      # phát hiện → worker chạy
        if getattr(self, "_v94_wake_lag_ms", None) is not None:
            self._v94["kpi"]["wake_lag_ms"] = self._v94_wake_lag_ms         # mtime trigger → phát hiện
        _arms = _v94_ab_arms()
        if _arms:
            _arm = _arms[_v94_ab_next() % len(_arms)]
            self._v94["video"]["small_video_workers"] = _arm
            self._v94["kpi"]["ab_small_workers"] = _arm
        TELEM.span(vid, "worker_start", url=url, row=(url_index + 2) if url_index is not None else None,
                   queue_ms=self._v94["kpi"].get("queue_ms"), detect_ms=self._v94["kpi"].get("detect_ms"))
        _v94_job_enter()
        ok_holder = {"ok": False}
        _orig_emit = self.signals.url_finished.emit

        def _emit(u, i, okv):
            ok_holder["ok"] = bool(okv)
            return _orig_emit(u, i, okv)
        try:
            self._v94_emit = _emit
            self._process_single_url_impl(url, url_index)
        finally:
            _v94_job_exit()
            try:
                k = self._v94["kpi"]
                t_end = time.perf_counter_ns()
                k.setdefault("pipeline_total_ms", round((t_end - self._v94["t0_ns"]) / 1e6, 2))
                summary = {
                    "version": V94_VERSION, "video_id": vid, "success": ok_holder["ok"],
                    "fail_reason": self._v94.get("fail") or ("" if ok_holder["ok"] else "unknown"),
                    "threads": threading.active_count(),
                    "cpu_s": round(time.process_time() - self._v94["cpu0"], 3),
                    "video": {kk: vv for kk, vv in self._v94["video"].items() if kk not in ("cancel", "pool")},
                    "audio": {kk: vv for kk, vv in self._v94["audio"].items() if kk not in ("cancel", "pool")},
                }
                summary.update(k)
                summary.setdefault("mode", "480hard")
                try:
                    import psutil  # optional
                    summary["rss_mb"] = round(psutil.Process().memory_info().rss / 1048576.0, 1)
                except Exception:
                    pass
                # V9.4.1: KPI3 = phát hiện (trigger/Excel) → GET media đầu tiên; first_get = worker nhận URL → GET đầu.
                _fg = [x for x in (self._v94["video"].get("first_get_ns"), self._v94["audio"].get("first_get_ns")) if x]
                if _fg:
                    k["first_get_ms"] = round((min(_fg) - self._v94["t0_ns"]) / 1e6, 2)
                    if _wk:
                        k["kpi3_ms"] = round((min(_fg) - _wk) / 1e6, 2)
                # Review V9.4.1: nhãn A/B chỉ có nghĩa với video <= 4MiB (nhánh khác không dùng số worker này)
                if k.get("ab_small_workers") and int(self._v94["video"].get("total") or 0) > 4 * 1048576:
                    k["ab_small_workers_ignored"] = k.pop("ab_small_workers")
                summary["first_get_ms"] = k.get("first_get_ms")
                summary["kpi3_ms"] = k.get("kpi3_ms")
                TELEM.span(vid, "job_summary", **summary)
                _post_keys = (("rename", "pp_rename_ms"), ("probe", "pp_probe_ms"), ("excel", "pp_excel_ms"),
                              ("prepend_ff", "pp_prepend_ffmpeg_ms"), ("prepend_probe", "pp_prepend_probe_ms"),
                              ("move", "pp_move_ms"), ("done_lock", "done_lock_wait_ms"), ("done_load", "done_load_ms"),
                              ("done_save", "done_save_ms"), ("done_replace", "done_replace_ms"))
                _post = [f"{lab}={k[key]}" for lab, key in _post_keys if k.get(key) is not None]
                if _post:
                    self.log(
                        "📮 POST: " + " ".join(_post)
                        + (f" | dur_src={k['duration_source']}" if k.get("duration_source") else "")
                        + (f" | same_vol={k['same_volume']}" if "same_volume" in k else "")
                        + (f" | rename_retry={k['rename_retries']}" if k.get("rename_retries") else "")
                        + (f" | replace_retry={k['replace_retries']}" if k.get("replace_retries") else "")
                        + (" | replace_FALLBACK" if k.get("replace_fallback") else "")
                        + (f" | xlsx={int(k['xlsx_bytes'])//1024}KB" if k.get("xlsx_bytes") else "")
                        + (" | STATUS-WRITE-FAIL" if k.get("status_write_ok") is False else "")
                    )
                _f = lambda key: k.get(key, "-")
                if k.get("mode") == "360fast":
                    self.log(
                        f"📊 V9.5 KPI-360 [{vid}] resolve={k.get('resolve_ms','-')}ms | first_byte={k.get('first_byte_ms','-')}ms "
                        f"| download={k.get('download_ms','-')}ms ({(k.get('bytes') or 0)/1048576:.2f}MB ≈{k.get('mbps','-')}Mbps, {k.get('engine','-')}"
                        f"{'+alt' if k.get('alt_link') else ''}) "
                        + (f"| req1={k['first_resp_ms']}ms " if k.get("first_resp_ms") is not None else "")
                        + (f"dns={k.get('dns_ms')} tls={k.get('tls_ms')} " if k.get("tls_ms") is not None else "")
                        + (f"xfer={k['xfer_ms']}ms " if k.get("xfer_ms") is not None else "")
                        + ("302skip " if k.get("redirect_skipped") else "")
                        + f"| HOT_READY={k.get('hot_ready_ms','-')}ms | post={k.get('postprocess_ms','-')} done={k.get('status_done_ms','-')} "
                        + f"| TOTAL={k.get('pipeline_total_ms','-')}ms | ok={ok_holder['ok']}"
                        + (f" | fail={summary['fail_reason']}" if not ok_holder["ok"] else "")
                        + f" | KPI3={_f('kpi3_ms')} queue={_f('queue_ms')} detect={_f('detect_ms')}"
                        + (f" ja_fallback={k['ja_fallback']}" if k.get("ja_fallback") else "")
                        + f" [{V94_VERSION}]"
                    )
                    return
                self.log(
                    f"📊 V9.4 KPI [{vid}] resolve_v={k.get('resolve_video_ms','-')}ms "
                    f"resolve_a={k.get('resolve_audio_ms','-')}ms | video={k.get('video_ms','-')}ms "
                    f"audio={k.get('audio_ms','-')}ms | max(v,a)={k.get('media_max_ms','-')}ms | "
                    f"mux={k.get('mux_ms','-')}ms | HOT_READY={k.get('hot_ready_ms','-')}ms | "
                    f"TOTAL={k.get('pipeline_total_ms','-')}ms | ok={ok_holder['ok']}"
                    + (f" | fail={summary['fail_reason']}" if not ok_holder["ok"] else "")
                    + f" | KPI1={_f('media_ready_ms')} v_end={_f('video_done_at_ms')} a_end={_f('audio_done_at_ms')}"
                    + f" KPI3={_f('kpi3_ms')} first_get={_f('first_get_ms')} queue={_f('queue_ms')} detect={_f('detect_ms')}"
                    + f" valid={_f('file_validated_ms')} post={_f('postprocess_ms')} done={_f('status_done_ms')}"
                    + (f" prewarm={k['prewarm']}" if k.get("prewarm") else "")
                    + (f" ab={k['ab_small_workers']}" if k.get("ab_small_workers") else "")
                    + f" [{V94_VERSION}]"
                )
            except Exception:
                pass

    def _v94_mark(self, key, ev=None, **fields):
        """Ghi mốc KPI (ms tính từ lúc worker nhận URL) + span tương ứng."""
        try:
            ms = round((time.perf_counter_ns() - self._v94["t0_ns"]) / 1e6, 2)
            if key and key not in self._v94["kpi"]:
                self._v94["kpi"][key] = ms
            if ev:
                TELEM.span(self._v94["job"], ev, at_ms=ms, **fields)
        except Exception:
            pass

    def _v94_kpi(self) -> dict:
        st = getattr(self, "_v94", None)
        return st["kpi"] if isinstance(st, dict) and isinstance(st.get("kpi"), dict) else {}

    def _v94_maybe_prewarm(self, vid):
        """V9.4.1: ngay khi media đầu tiên bắt đầu tải, nếu ffmpeg đã rảnh >= V94_FF_PREWARM_IDLE_SEC giây
        (hoặc chưa chạy lần nào), làm nóng ffmpeg/ffprobe ở thread nền. Job KHÔNG bao giờ chờ nó."""
        try:
            if not (V94_FF_PREWARM and FFMPEG_PATH):
                return
            st = getattr(self, "_v94", None)
            if not isinstance(st, dict) or st.get("ff_prewarm_started"):
                return
            idle = _v94_ff_idle_s(FFMPEG_PATH)
            if 0 <= idle < float(V94_FF_PREWARM_IDLE_SEC or 0):
                st["kpi"]["prewarm"] = f"skip(idle={idle:.1f}s)"
                return
            st["ff_prewarm_started"] = True

            def _run():
                res = _v94_ff_prewarm("JOB", job=st.get("job", vid), logger=self.log)
                if res is None:
                    st["kpi"]["prewarm"] = "busy"
                else:
                    st["kpi"]["prewarm"] = f"{res.get('ffmpeg_ms', '-')}/{res.get('ffprobe_ms', '-')}ms"
                    st["kpi"]["prewarm_idle_s"] = res.get("idle_s")
            threading.Thread(target=_run, name="V94FFPrewarm", daemon=True).start()
        except Exception:
            pass

    # ------------------------------------------------------------------ V9.5 360p nhanh (itag 18)
    def _process_single_url_360(self, url: str, url_index: int, excel_info: str = ""):
        """360p nhanh: link bằng _quick_fetch_native_360p (race 3 client → yt-dlp nhanh → robust), tải 1 GET
        (_download_direct_single_get), KHÔNG ghép ffmpeg, os.replace ra file cuối, hậu xử lý y như 480p.
        Trả "FALLBACK_480" khi phải chuyển sang 480p+JA (tiếng mặc định không phải Nhật nhưng có track Nhật)."""
        self.log(f"=== [START 360P-FAST] {excel_info}{url} ===")
        t0 = time.perf_counter_ns()
        k = self._v94["kpi"]
        k["mode"] = "360fast"
        vid_hint = _extract_youtube_video_id(url) or "UNKNOWN"
        _ms = lambda ns: round((ns - self._v94["t0_ns"]) / 1e6, 2)

        # --- 1) link ---
        TELEM.span(self._v94["job"], "q360_resolve_begin", at_ms=_ms(time.perf_counter_ns()))
        data = self._quick_fetch_native_360p(url)
        t_res = time.perf_counter_ns()
        if not data or not data.get("url"):
            self._v94["fail"] = "no_itag18"
            self.log(f"❌ 360P [{vid_hint}]: không lấy được itag 18 sau {(t_res - t0)/1e9:.2f}s")
            self._v94_emit(url, url_index, False)
            return "FAIL"
        k["resolve_ms"] = round((t_res - t0) / 1e6, 2)
        self._v94["video"].update({"itag": "18", "source": str(data.get("source") or "")})
        vid = str(data.get("id") or vid_hint)
        expected = int(data.get("content_length") or 0)
        dlang = str(data.get("default_audio_lang") or "")
        has_ja = bool(data.get("has_ja_audio"))
        TELEM.span(self._v94["job"], "q360_resolve_end", at_ms=_ms(t_res), source=data.get("source"),
                   clen=expected, default_lang=dlang, has_ja=has_ja, tracks=data.get("audio_tracks"))
        self.log(f"⚡ 18 READY [{vid}] [{data.get('source')}] {(t_res - t0)/1e9:.3f}s | size≈{expected/1048576:.1f}MB"
                 f" | lang={dlang or '?'} tracks={data.get('audio_tracks') or '?'}{' ja✓' if has_ja else ''}")

        # --- 2) tiếng mặc định ---
        def _ja_unsafe(d):
            """True khi itag 18 có thể mang tiếng KHÁC Nhật trong khi video có track Nhật: tiếng mặc định ghi rõ
            không phải Nhật, hoặc KHÔNG BIẾT tiếng mặc định mà video có >= 2 track (review V9.5)."""
            dl = str(d.get("default_audio_lang") or "")
            tracks = int(d.get("audio_tracks") or 0)
            return bool(d.get("has_ja_audio")) and not _looks_japanese_lang(dl) and (bool(dl) or tracks >= 2)
        if _ja_unsafe(data):
            k["ja_fallback"] = f"default={dlang or '?'}"
            TELEM.span(self._v94["job"], "q360_ja_fallback", default_lang=dlang or "?", tracks=data.get("audio_tracks"))
            self.log(f"🈁 360P [{vid}]: tiếng mặc định của itag 18 = '{dlang or '?'}' (tracks={data.get('audio_tracks')}) "
                     f"nhưng video CÓ track tiếng Nhật"
                     + (" → chuyển sang 480p + audio Nhật" if V95_360P_JA_FALLBACK else " (V95_360P_JA_FALLBACK=0: vẫn tải 360p)"))
            if V95_360P_JA_FALLBACK:
                return "FALLBACK_480"
        elif dlang and not _looks_japanese_lang(dlang):
            self.log(f"    ℹ️ 360P [{vid}]: tiếng mặc định '{dlang}', không thấy track Nhật → tải itag 18 như yêu cầu")

        # --- 3) tải 1 GET ---
        temp_name = f"{vid}_18.mp4"
        temp_file = self.temp_dir / temp_name
        dinfo = {}
        TELEM.span(self._v94["job"], "q360_get_begin", at_ms=_ms(time.perf_counter_ns()))
        self._v94["video"]["first_get_ns"] = time.perf_counter_ns()
        rn = int(getattr(self, "_v96_range_n", None) if getattr(self, "_v96_range_n", None) is not None else V96_RANGE_N)
        h2 = bool(getattr(self, "_v96_http2", None) if getattr(self, "_v96_http2", None) is not None else V96_HTTP2)
        ok = False
        src_url0 = _v96_predicted_url(data["url"]) if (h2 or rn > 1) else None
        if src_url0:
            k["redirect_skipped"] = True
        if h2 and expected >= RANGE_VIDEO_MIN_BYTES:
            ok = _v96_download_h2(src_url0 or data["url"], self.temp_dir, temp_name, self.log,
                                  self.MIN_BYTES_VIDEO, expected, max(1, rn), dinfo)
            k["engine"] = dinfo.get("engine") or f"h2x{max(1, rn)}"
            if not ok:
                self.log(f"    ↩ H2 lỗi ({dinfo.get('fail_reason')}) → 1-GET HTTP/1.1 cùng URL")
                k["h2_fail"] = str(dinfo.get("fail_reason"))
                dinfo = {}
        elif (rn > 1 or V95_360P_RANGE) and expected >= RANGE_VIDEO_MIN_BYTES:
            dinfo["job"] = self._v94["job"]
            if rn > 1:
                dinfo["workers_override"] = rn
            ok = _download_http_range_turbo(src_url0 or data["url"], self.temp_dir, temp_name, self.log,
                                            self.MIN_BYTES_VIDEO, expected, media_kind="video", info=dinfo)
            if dinfo.get("first_byte_ms") is not None:
                dinfo["first_byte_ns"] = self._v94["video"]["first_get_ns"] + int(dinfo["first_byte_ms"] * 1e6)
            k["engine"] = f"range{rn}" if rn > 1 else "range"
            if not ok:
                # V9.6: lỗi Range (403/410/429/...) → dừng chia, 1-GET cùng URL, không retry thêm
                self.log(f"    ↩ Range{rn} lỗi ({dinfo.get('fail_reason')}) → 1-GET cùng URL")
                k["range_fail"] = str(dinfo.get("fail_reason"))
                dinfo = {}
        if not ok:
            ok = _download_direct_single_get(data["url"], self.temp_dir, temp_name, self.log,
                                             self.MIN_BYTES_VIDEO, expected, info=dinfo)
            k["engine"] = ("1get" if k.get("engine") in (None, "1get") else f"{k['engine']}→1get")
        for kk in ("first_resp_ms", "dns_ms", "tls_ms", "xfer_ms", "redirect_skipped", "redirect_hops", "http_version"):
            if dinfo.get(kk) is not None:
                k[kk] = dinfo[kk]
        if dinfo.get("first_byte_ns"):
            k["first_byte_ms"] = _ms(dinfo["first_byte_ns"])
            TELEM.span(self._v94["job"], "q360_first_byte", at_ms=k["first_byte_ms"], ttfb_ms=dinfo.get("ttfb_ms"))
        used = data
        if not ok:
            # dự phòng 1: link khác (yt-dlp nhanh) bằng cùng đường 1 GET; dự phòng 2: engine (aria2 / IDM)
            self.log(f"    ↪ 360P [{vid}] 1GET lỗi ({dinfo.get('fail_reason')}) → link dự phòng")
            alt = self._quick_fetch_ytdlp_360p(url, robust=False) or self._quick_fetch_ytdlp_360p(url, robust=True)
            if alt and alt.get("url") and _ja_unsafe(alt) and V95_360P_JA_FALLBACK:
                k["ja_fallback"] = f"default={alt.get('default_audio_lang') or '?'}(alt)"
                self.log(f"🈁 360P [{vid}]: link dự phòng cho thấy tiếng mặc định không phải Nhật → chuyển sang 480p + audio Nhật")
                return "FALLBACK_480"
            if alt and alt.get("url") and alt.get("url") != data.get("url"):
                used = alt
                expected = int(alt.get("content_length") or 0)       # KHÔNG mang size của resolver A sang URL của B
                dinfo = {}
                k["alt_link"] = True
                ok = _download_direct_single_get(alt["url"], self.temp_dir, temp_name, self.log,
                                                 self.MIN_BYTES_VIDEO, expected, info=dinfo)
            if not ok:
                src_url = (used or data).get("url")
                if self.download_engine == "idm":
                    k["engine"] = "idm"
                    ok = download_idm_ultimate(src_url, self.temp_dir, temp_name, self.log,
                                               self.MIN_BYTES_VIDEO, media_label="360p itag 18")
                else:
                    k["engine"] = "aria2"
                    self.log(f"    ↪ 360P [{vid}] → ARIA2 fallback")
                    ok = download_aria2c_ultimate(src_url, self.temp_dir, temp_name, self.log,
                                                  self.MIN_BYTES_VIDEO, expected_bytes=expected)
                if ok and temp_file.exists():
                    dinfo = {"bytes": temp_file.stat().st_size, "engine": k["engine"]}
        t_dl = time.perf_counter_ns()
        k["download_ms"] = round((t_dl - t_res) / 1e6, 2)
        k["video_ms"] = k["download_ms"]
        k["bytes"] = dinfo.get("bytes")
        if dinfo.get("elapsed_ms") and dinfo.get("bytes"):
            k["mbps"] = round(dinfo["bytes"] * 8.0 / 1e6 / max(1e-3, dinfo["elapsed_ms"] / 1000.0), 1)
        self._v94_mark("video_done_at_ms", "q360_download_complete", ok=bool(ok), bytes=dinfo.get("bytes"),
                       ttfb_ms=dinfo.get("ttfb_ms"), elapsed_ms=dinfo.get("elapsed_ms"), fail=dinfo.get("fail_reason"),
                       engine=k.get("engine"), alt_link=bool(k.get("alt_link")))
        if not ok or not temp_file.exists():
            self._v94["fail"] = f"q360_download:{dinfo.get('fail_reason') or 'engine_fail'}"
            self.log(f"❌ 360P [{vid}]: tải thất bại → KHÔNG DONE")
            self._v94_emit(url, url_index, False)
            return "FAIL"

        # --- 4) xác thực nhẹ: kích thước (chỉ chặt khi content_length là số CHÍNH XÁC; filesize_approx → dung sai 4%) ---
        size = temp_file.stat().st_size
        _exact = bool((used or data).get("length_exact", True))
        if expected > 0 and ((size < expected) if _exact else (size + 4096 < int(expected * 0.96))):
            self._v94["fail"] = f"q360_short_{size}/{expected}"
            self.log(f"❌ 360P [{vid}]: file {size} B < content_length {expected} B → KHÔNG DONE")
            try:
                if not self.keep_temp:
                    temp_file.unlink()
            except OSError:
                pass
            self._v94_emit(url, url_index, False)
            return "FAIL"
        k["media_ready_ms"] = _ms(time.perf_counter_ns())
        k["media_max_ms"] = k["download_ms"]

        # --- 5) đổi tên ra file cuối (không mux) ---
        final_title = sanitize_name(str((used or data).get("title") or data.get("title") or "video"))
        base_name = f"{final_title} [{vid}]"
        out_file_final = self.out_dir / f"{base_name}.mp4"
        try:
            if out_file_final.exists():
                out_file_final.unlink()
            os.replace(str(temp_file), str(out_file_final))
        except OSError:
            if not _move_file_fast_retry(temp_file, out_file_final, self.log, timeout_sec=8.0):
                self._v94["fail"] = "move_fail"
                self._v94_emit(url, url_index, False)
                return "FAIL"
        hot_ready = (time.perf_counter_ns() - self._v94["t0_ns"]) / 1e9
        self._v94_mark("hot_ready_ms", "hot_ready", path="q360")
        self._v94_mark("file_validated_ms", "q360_file_ready", size=size)
        self.log(f"🚀 HOT 360P READY: {hot_ready:.3f}s từ lúc worker nhận URL | itag 18 1-GET | {size/1048576:.2f}MB | không ghép")
        self.log(f"✅ Success 360p: {base_name}.mp4")

        # --- 6) hậu xử lý như 480p (cắt <60 / 60-300 / >300, đặt tên, MOVE) ---
        duration = float((used or data).get("duration_sec") or data.get("duration_sec") or 0.0)
        success = bool(self._cut_after_merge(out_file_final, base_name, url_index, duration_hint=duration))
        self._v94_mark("postprocess_ms", "postprocess_complete", ok=bool(success))
        if not success:
            self._v94["fail"] = "postprocess_fail"
            self.log("❌ Hậu xử lý/cut/move thất bại → KHÔNG ghi DONE.")
        if success and url_index is not None and self.excel_path and self.status_col_name:
            if write_status_to_excel(self.excel_path, url_index, self.status_col_name, "DONE", self.log,
                                     expected_url=url, url_col_name=self.url_col_name, stats=k):
                self._v94_mark("status_done_ms", "status_done")
            else:
                k["status_write_ok"] = False
        k["pipeline_total_ms"] = _ms(time.perf_counter_ns())
        self.log(f"⏱️ PIPELINE TOTAL: {k['pipeline_total_ms']/1000.0:.3f}s | hot360={hot_ready:.3f}s | "
                 f"resolve={k.get('resolve_ms')}ms | download={k.get('download_ms')}ms")
        self.log(f"=== [END] {url} (Thành công: {success}) ===")
        self._v94_emit(url, url_index, success)

        # --- 7) ffprobe nền SAU DONE, chỉ cảnh báo ---
        if success and V95_360P_BG_PROBE and FFPROBE_PATH:
            final_path = k.get("final_file")
            if final_path:
                job = self._v94["job"]
                logf = self.log

                def _bg_probe(path=final_path, job=job, logf=logf):
                    try:
                        pth = Path(path)
                        targets = sorted(pth.glob("*.mp4")) if pth.is_dir() else [pth]
                        for t in targets:
                            info = _ffprobe_av_info(t)
                            okp = bool(info.get("has_video") and info.get("has_audio") and float(info.get("format_duration") or 0) > 0)
                            TELEM.span(job, "q360_bg_probe", ok=okp, file=t.name, w=info.get("width"), h=info.get("height"),
                                       vdur=info.get("video_duration"), adur=info.get("audio_duration"))
                            if not okp:
                                logf(f"    ⚠️ 360P [{job}] ffprobe SAU DONE: file nghi hỏng (video={info.get('has_video')} "
                                     f"audio={info.get('has_audio')}) → kiểm tra tay: {t.name}")
                    except Exception as _e:
                        TELEM.span(job, "q360_bg_probe", ok=None, error=f"{type(_e).__name__}: {_e}")
                threading.Thread(target=_bg_probe, name="V95BgProbe", daemon=True).start()
        return "DONE" if success else "FAIL"

    def _process_single_url_impl(self, url: str, url_index: int):
        if self._critical_error:
            self._v94_emit(url, url_index, False); return
        if hasattr(self.master, 'realtime_stop_event') and self.master.realtime_stop_event.is_set():
            self._v94_emit(url, url_index, False); return

        excel_info = f"[Dòng {url_index+2}] " if url_index is not None else ""
        if V95_360P and getattr(self, "quality_mode", "480hard") == "360fast":
            # V9.5: luồng 360p nhanh. Trả "FALLBACK_480" → tiếp tục luồng 480p bên dưới y như V9.4.
            r360 = self._process_single_url_360(url, url_index, excel_info)
            if r360 != "FALLBACK_480":
                return
            self._v94["kpi"]["mode"] = "480hard(from360)"
            self._v94["video"] = {}
            self.log(f"    ↪ [360p→480p] {excel_info}chuyển sang luồng 480p + audio Nhật cho video này")
        self.log(f"=== [START 480P-HARD] {excel_info}{url} ===")
        t_total_start = time.perf_counter()
        success = False

        # V8.6 fresh-Short policy: Native first, so resolver traffic does not compete with
        # the first 1-2 seconds of a tiny media transfer. yt-dlp FAST is armed only if Native
        # has not completed the bundle quickly; ROBUST is later still. Normal watch URLs keep
        # the previous parallel resolver behavior.
        resolve_started = time.perf_counter()
        video_id_hint = _extract_youtube_video_id(url) or "UNKNOWN"
        is_short_url = "/shorts/" in str(url).lower()
        futures = {
            _BUNDLE_RESOLVE_EXECUTOR.submit(_fast_innertube_480, url): "NATIVE-VD-RACE",
        }
        if not is_short_url:
            futures[_YTDLP_RESOLVER_EXECUTOR.submit(
                _ytdlp_resolve_480_on_warm_thread, url, False
            )] = "YTDLP-FAST-3CLIENT"
        pending = set(futures)
        best_meta = None
        video_candidates = []
        audio_candidates = []
        video_future = None
        audio_future = None
        video_file = None
        audio_file = None
        video_data_used = None
        audio_data_used = None

        # One shared robust refresh for both media threads. If a native URL is rejected
        # at GVS, do not waste time retrying the same forbidden URL with aria2/IDM.
        # Re-extract once through yt-dlp's robust path and let video+audio share it.
        _safe_refresh_lock = threading.Lock()
        _safe_refresh_holder = {"future": None}

        def _get_safe_refresh_bundle():
            with _safe_refresh_lock:
                fut = _safe_refresh_holder.get("future")
                if fut is None:
                    self.log(f"    ♻️ SAFE-REFRESH [{video_id_hint}] native media URL rejected → yt-dlp ROBUST once")
                    fut = _YTDLP_RESOLVER_EXECUTOR.submit(
                        _ytdlp_resolve_480_on_warm_thread, url, True
                    )
                    _safe_refresh_holder["future"] = fut
            try:
                return fut.result(timeout=8.0)
            except Exception:
                return None

        def add_candidate(data):
            nonlocal best_meta, video_future, audio_future, video_file, audio_file, video_data_used, audio_data_used
            if not data:
                return
            if best_meta is None or str(data.get("title") or "") != "video":
                best_meta = data
            vurl = data.get("video_url")
            if vurl and all(x.get("video_url") != vurl for x in video_candidates):
                video_candidates.append(data)
            aurl = data.get("audio_url")
            if aurl and all(x.get("audio_url") != aurl for x in audio_candidates):
                audio_candidates.append(data)

            vid = str(data.get("id") or video_id_hint or int(time.time()))
            if (not aurl and audio_future is None and data.get("audio_refuse_reason")
                    and not self._v94.get("ja_refuse_logged")):
                self._v94["ja_refuse_logged"] = True
                self._v94["kpi"]["ja_refuse"] = str(data.get("audio_refuse_reason"))
                self.log(f"    ℹ️ JA-INFER từ chối [{vid}] ({data.get('audio_refuse_reason')}) → chờ nguồn xác nhận tiếng Nhật")
            if video_future is None and vurl:
                self._v94_mark("resolve_video_ms", "resolve_first_video_url",
                               itag=str(data.get("video_format_id") or ""), source=str(data.get("source") or ""),
                               clen=int(data.get("video_content_length") or 0))
                selected_fmt = str(data.get("video_format_id") or "?")
                video_ext = ".mp4" if selected_fmt == "135" else ".webm" if selected_fmt == "244" else ".video"
                video_file = self.temp_dir / f"{vid}_{selected_fmt}{video_ext}"
                video_data_used = data
                self.log(
                    f"⚡ {selected_fmt} READY [{vid}] [{data.get('source')}] {time.perf_counter()-resolve_started:.3f}s | "
                    f"size≈{int(data.get('video_content_length') or 0)/1048576:.1f}MB → START VIDEO NGAY"
                )
                def _video_job(d=data, path=video_file):
                    self.log(f"    🚀 MEDIA THREAD START VIDEO [{vid}] | {path.name}")
                    t_v0 = time.perf_counter_ns()
                    vinfo = self._v94["video"]
                    vinfo.update({"job": self._v94["job"], "itag": str(d.get("video_format_id") or ""),
                                  "source": str(d.get("source") or "")})
                    source = str(d.get("source") or "")
                    expected = int(d.get("video_content_length") or 0)
                    fmt_id = str(d.get("video_format_id") or "?")
                    try:
                        if self.download_engine == "idm":
                            ok = download_idm_ultimate(
                                d["video_url"], self.temp_dir, path.name, self.log,
                                self.MIN_BYTES_VIDEO, media_label=f"480p itag {fmt_id}"
                            )
                        elif source.startswith("NATIVE_") and expected >= RANGE_VIDEO_MIN_BYTES:
                            # Native hot path: range once. If GVS rejects it, NEVER retry the
                            # same signed URL via aria2; refresh the bundle instead.
                            ok = _download_http_range_turbo(
                                d["video_url"], self.temp_dir, path.name, self.log,
                                self.MIN_BYTES_VIDEO, expected, media_kind="video", info=vinfo
                            )
                        else:
                            ok = download_smart_progressive(
                                d["video_url"], self.temp_dir, path.name, self.log,
                                self.MIN_BYTES_VIDEO, expected_bytes=expected, info=vinfo,
                            )
                    finally:
                        self._v94["kpi"]["video_ms"] = round((time.perf_counter_ns() - t_v0) / 1e6, 2)
                    if ok:
                        self._v94_mark("video_done_at_ms", "video_download_complete_job")
                        return True
                    # V9.4: URL bị GVS từ chối (403/410/429) dù nguồn nào → 1 SAFE-REFRESH dùng chung.
                    if source.startswith("NATIVE_") or _v94_is_auth_fail(vinfo):
                        if str(vinfo.get("fail_reason") or "").startswith("HTTP_429"):
                            _ra = max(0.5, min(3.0, float(vinfo.get("retry_after") or 1.0)))
                            self.log(f"    ⏸️ 429 [{vid}] video → chờ {_ra:.1f}s (giới hạn) rồi refresh 1 lần")
                            time.sleep(_ra)
                        fb = _get_safe_refresh_bundle()
                        if fb and fb.get("video_url") and fb.get("video_url") != d.get("video_url"):
                            self.log(f"    ↪ SAFE-REFRESH VIDEO [{vid}] source={fb.get('source')} → retry fresh URL")
                            return download_smart_progressive(
                                fb["video_url"], self.temp_dir, path.name, self.log,
                                self.MIN_BYTES_VIDEO,
                                expected_bytes=int(fb.get("video_content_length") or expected),
                            )
                    return False
                _t_vsub = time.perf_counter_ns()
                video_future = _submit_media_dedicated(_video_job)
                self._v94_maybe_prewarm(vid)
                video_future.add_done_callback(lambda _f, _t=_t_vsub: self._v94["kpi"].__setitem__(
                    "video_job_ms", round((time.perf_counter_ns() - _t) / 1e6, 2)))

            if audio_future is None and aurl:
                self._v94_mark("resolve_audio_ms", "resolve_first_ja_audio_url",
                               itag=str(data.get("audio_format_id") or ""), source=str(data.get("source") or ""),
                               clen=int(data.get("audio_content_length") or 0),
                               inferred=bool(data.get("audio_inferred_single_ja")),
                               infer=data.get("audio_infer"))
                # V9.4: đuôi file đúng codec thật (V9.3 luôn .m4a kể cả khi là opus/webm).
                audio_file = self.temp_dir / f"{vid}_ja_audio{_v94_audio_ext(data)}"
                audio_data_used = data
                self.log(
                    f"⚡ JA AUDIO READY [{vid}] [{data.get('source')}] {time.perf_counter()-resolve_started:.3f}s | "
                    f"fmt={data.get('audio_format_id') or '?'}" + (" [SINGLE-TRACK-JA]" if data.get('audio_inferred_single_ja') else "")
                    + (f" (kana={(data.get('audio_infer') or {}).get('kana_source', '?')} asr={(data.get('audio_infer') or {}).get('asr_lang', '-')})"
                       if data.get('audio_inferred_single_ja') and data.get('audio_infer') else "")
                    + " → START AUDIO SONG SONG"
                )
                def _audio_job(d=data, path=audio_file):
                    self.log(f"    🚀 MEDIA THREAD START AUDIO [{vid}] | {path.name}")
                    t_a0 = time.perf_counter_ns()
                    ainfo = self._v94["audio"]
                    ainfo.update({"job": self._v94["job"], "itag": str(d.get("audio_format_id") or ""),
                                  "source": str(d.get("source") or ""),
                                  "inferred_single_ja": bool(d.get("audio_inferred_single_ja"))})
                    source = str(d.get("source") or "")
                    expected = int(d.get("audio_content_length") or 0)
                    try:
                        if source.startswith("NATIVE_") and expected >= RANGE_AUDIO_MIN_BYTES:
                            ok = _download_http_range_turbo(
                                d["audio_url"], self.temp_dir, path.name, self.log,
                                MIN_BYTES_AUDIO, expected, media_kind="audio", info=ainfo
                            )
                        else:
                            ok = download_smart_audio(
                                d["audio_url"], self.temp_dir, path.name, self.log,
                                MIN_BYTES_AUDIO, expected, info=ainfo,
                            )
                    finally:
                        self._v94["kpi"]["audio_ms"] = round((time.perf_counter_ns() - t_a0) / 1e6, 2)
                    if ok:
                        self._v94_mark("audio_done_at_ms", "audio_download_complete_job")
                        return True
                    if source.startswith("NATIVE_") or _v94_is_auth_fail(ainfo):
                        if str(ainfo.get("fail_reason") or "").startswith("HTTP_429"):
                            _ra = max(0.5, min(3.0, float(ainfo.get("retry_after") or 1.0)))
                            self.log(f"    ⏸️ 429 [{vid}] audio → chờ {_ra:.1f}s (giới hạn) rồi refresh 1 lần")
                            time.sleep(_ra)
                        fb = _get_safe_refresh_bundle()
                        if fb and fb.get("audio_url") and fb.get("audio_url") != d.get("audio_url"):
                            self.log(f"    ↪ SAFE-REFRESH AUDIO [{vid}] source={fb.get('source')} → retry fresh URL")
                            return download_smart_audio(
                                fb["audio_url"], self.temp_dir, path.name, self.log,
                                MIN_BYTES_AUDIO, int(fb.get("audio_content_length") or expected),
                            )
                    return False
                _t_asub = time.perf_counter_ns()
                audio_future = _submit_media_dedicated(_audio_job)
                self._v94_maybe_prewarm(vid)
                audio_future.add_done_callback(lambda _f, _t=_t_asub: self._v94["kpi"].__setitem__(
                    "audio_job_ms", round((time.perf_counter_ns() - _t) / 1e6, 2)))

        deadline = resolve_started + HARD_480_AVAILABILITY_WAIT_SEC
        native_retry_gap = SHORT_NATIVE_RETRY_SEC if is_short_url else HARD_480_NATIVE_RETRY_SEC
        next_native_retry = resolve_started + native_retry_gap
        next_fast_ytdlp = resolve_started + (SHORT_FAST_YTDLP_START_SEC if is_short_url else 0.0)
        next_robust_retry = resolve_started + (SHORT_ROBUST_START_SEC if is_short_url else LAZY_ROBUST_START_SEC)
        fast_ytdlp_round = 0
        retry_notice = False
        native_round = 0
        robust_round = 0

        while time.perf_counter() < deadline and (video_future is None or audio_future is None):
            now = time.perf_counter()
            if pending:
                done, pending = wait(
                    pending, timeout=min(0.08, max(0.0, deadline-now)),
                    return_when=FIRST_COMPLETED
                )
                for fut in done:
                    try:
                        data = fut.result()
                    except Exception:
                        data = None
                    add_candidate(data)
            else:
                time.sleep(0.01 if is_short_url else 0.02)

            now = time.perf_counter()
            if is_short_url and ((video_future is None) != (audio_future is None)):
                # One half is already ready: ask FAST yt-dlp immediately for only the missing half.
                next_fast_ytdlp = min(next_fast_ytdlp, now)
            if video_future is not None and audio_future is not None:
                # Do not queue any more resolver work once both media transfers are live.
                for _pf in list(pending):
                    _pf.cancel()
                break

            if not retry_notice and now - resolve_started >= 0.65:
                self.log(
                    f"⏳ HARD 480P [{video_id_hint}]: 244/135/JA chưa đủ ở snapshot đầu → "
                    f"availability race tối đa {HARD_480_AVAILABILITY_WAIT_SEC:.1f}s, KHÔNG hạ 360p"
                )
                retry_notice = True

            native_busy = any(str(futures.get(f, '')).startswith('NATIVE') for f in pending)
            if now >= next_native_retry and not native_busy:
                native_round += 1
                f = _BUNDLE_RESOLVE_EXECUTOR.submit(_fast_innertube_480, url)
                futures[f] = f"NATIVE-RETRY-{native_round}"
                pending.add(f)
                next_native_retry = now + native_retry_gap

            fast_busy = any(str(futures.get(f, '')).startswith('YTDLP-FAST') for f in pending)
            if is_short_url and now >= next_fast_ytdlp and not fast_busy:
                fast_ytdlp_round += 1
                f = _YTDLP_RESOLVER_EXECUTOR.submit(_ytdlp_resolve_480_on_warm_thread, url, False)
                futures[f] = f"YTDLP-FAST-LAZY-{fast_ytdlp_round}"
                pending.add(f)
                # One FAST fallback is enough while Native continues polling; do not hammer EJS/client work.
                next_fast_ytdlp = deadline + 1.0

            robust_busy = any(str(futures.get(f, '')).startswith('YTDLP-ROBUST') for f in pending)
            if now >= next_robust_retry and not robust_busy:
                robust_round += 1
                f = _YTDLP_RESOLVER_EXECUTOR.submit(_ytdlp_resolve_480_on_warm_thread, url, True)
                futures[f] = f"YTDLP-ROBUST-RETRY-{robust_round}"
                pending.add(f)
                next_robust_retry = now + HARD_480_ROBUST_RETRY_SEC

        # Drain any resolver that has already completed without waiting, useful as retry candidate.
        for fut in list(pending):
            if fut.done():
                pending.discard(fut)
                try: add_candidate(fut.result())
                except Exception: pass

        if video_future is None:
            self._v94["fail"] = "no_480_itag"
            self.log(
                f"❌ HARD 480P [{video_id_hint}]: không thấy itag 244 hoặc 135 sau "
                f"{time.perf_counter()-resolve_started:.2f}s. Không tự hạ 360p/AV1."
            )
            self._v94_emit(url, url_index, False)
            return
        if audio_future is None:
            self._v94["fail"] = "no_ja_audio"
            self.log(
                f"❌ HARD 480P [{video_id_hint}]: không tìm thấy audio tiếng Nhật sau "
                f"{time.perf_counter()-resolve_started:.2f}s. Không tự đổi ngôn ngữ."
            )
            self._v94_emit(url, url_index, False)
            return

        # Wait for both transfers, but NEVER stay visually silent. V7.1 could look frozen
        # here for up to 1800s. V7.2 reports bytes every ~2s and uses dedicated media threads.
        t_media_wait = time.perf_counter()
        last_media_diag = time.perf_counter()   # V9.2: MEDIA LIVE đầu tiên sau 2s, không in ngay
        media_deadline = t_media_wait + 600.0
        while time.perf_counter() < media_deadline and not (video_future.done() and audio_future.done()):
            wait([video_future, audio_future], timeout=0.25)
            now_m = time.perf_counter()
            if now_m - last_media_diag >= 2.0:
                def _mb(p):
                    try:
                        rp = _range_progress_mb(p)
                        if rp is not None: return rp
                        return p.stat().st_size / 1048576.0 if p and p.exists() else 0.0
                    except Exception: return 0.0
                self.log(
                    f"    ⏬ MEDIA LIVE [{video_id_hint}] | "
                    f"video={_mb(video_file):.2f}MB ({'done' if video_future.done() else 'run'}) | "
                    f"audio={_mb(audio_file):.2f}MB ({'done' if audio_future.done() else 'run'}) | "
                    f"elapsed={now_m-t_media_wait:.1f}s"
                )
                last_media_diag = now_m

        try: video_ok = bool(video_future.result(timeout=0.05)) if video_future.done() else False
        except Exception as e:
            self.log(f"    ⚠️ VIDEO FUTURE lỗi: {type(e).__name__}: {e}")
            video_ok = False
        try: audio_ok = bool(audio_future.result(timeout=0.05)) if audio_future.done() else False
        except Exception as e:
            self.log(f"    ⚠️ AUDIO FUTURE lỗi: {type(e).__name__}: {e}")
            audio_ok = False
        if not video_future.done() or not audio_future.done():
            self.log(f"    ❌ MEDIA TIMEOUT [{video_id_hint}] sau {time.perf_counter()-t_media_wait:.1f}s")

        # One alternate URL retry if the first direct CDN URL failed. No quality/language switch.
        if not video_ok:
            alt = next((d for d in video_candidates if d.get("video_url") and d.get("video_url") != (video_data_used or {}).get("video_url")), None)
            if alt and video_file:
                alt_fmt = str(alt.get("video_format_id") or "?")
                self.log(f"    ↪ 480p URL đầu lỗi → retry {alt_fmt} từ {alt.get('source')}")
                if self.download_engine == "idm":
                    video_ok = download_idm_ultimate(
                        alt["video_url"], self.temp_dir, video_file.name, self.log,
                        self.MIN_BYTES_VIDEO, media_label=f"480p itag {alt_fmt}"
                    )
                else:
                    video_ok = download_smart_progressive(
                        alt["video_url"], self.temp_dir, video_file.name, self.log,
                        self.MIN_BYTES_VIDEO, int(alt.get("video_content_length") or 0)
                    )
        if not audio_ok:
            alt = next((d for d in audio_candidates if d.get("audio_url") and d.get("audio_url") != (audio_data_used or {}).get("audio_url")), None)
            if alt and audio_file:
                self.log(f"    ↪ JA audio URL đầu lỗi → retry cùng tiếng Nhật từ {alt.get('source')}")
                audio_ok = download_smart_audio(
                    alt["audio_url"], self.temp_dir, audio_file.name, self.log,
                    MIN_BYTES_AUDIO, int(alt.get("audio_content_length") or 0)
                )

        t_media_done = time.perf_counter()
        try:
            self._v94["kpi"]["media_max_ms"] = round(max(float(self._v94["kpi"].get("video_ms") or 0),
                                                         float(self._v94["kpi"].get("audio_ms") or 0)), 2)
            self._v94_mark("media_ready_ms", "media_both_done", video_ok=bool(video_ok), audio_ok=bool(audio_ok))
        except Exception:
            pass
        if not (video_ok and audio_ok and video_file and audio_file):
            self._v94["fail"] = (f"media_fail v={video_ok}:{self._v94['video'].get('fail_reason','')} "
                                 f"a={audio_ok}:{self._v94['audio'].get('fail_reason','')}")
            self.log(f"❌ MEDIA FAIL | video480={video_ok} | ja_audio={audio_ok}")
            self._v94_emit(url, url_index, False)
            return

        meta = best_meta or video_data_used or audio_data_used or {}
        final_vid_id = str(meta.get("id") or _extract_youtube_video_id(url) or int(time.time()))
        final_title = sanitize_name(str(meta.get("title") or "video"))
        base_name = f"{final_title} [{final_vid_id}]"
        combined_file = self.temp_dir / f"{final_vid_id}_480p244_ja.mp4"
        out_file_final = self.out_dir / f"{base_name}.mp4"

        # V8.8 DIRECT-CUT: video >300s sẽ bị cắt thành part → MUX nguyên file chỉ là ghi đĩa
        # thừa (~0.4s/90MB). Cắt thẳng từ video+audio, bỏ MUX, bỏ move file trung gian.
        direct_cut = False
        direct_dur = 0.0
        try:
            cutsecs_direct = int(self.master.cutsec_entry.text().strip())
        except Exception:
            cutsecs_direct = 0
        if cutsecs_direct > 0 and self.download_engine != "idm":
            try:
                direct_dur = float((video_data_used or {}).get("duration_sec") or 0.0)
            except Exception:
                direct_dur = 0.0
            if direct_dur <= 0:
                direct_dur = _ffprobe_duration_seconds(FFPROBE_PATH, video_file)
            direct_cut = direct_dur > 300.0
        if direct_cut:
            hot_ready = time.perf_counter() - t_total_start
            self._v94_mark("hot_ready_ms", "hot_ready", path="direct_cut")
            self.log(
                f"🚀 HOT 480P READY: {hot_ready:.3f}s từ lúc worker nhận URL | "
                f"DIRECT-CUT {direct_dur:.0f}s >300s → BỎ MUX, cắt thẳng video+JA"
            )
            success = bool(self._cut_after_merge(
                video_file, base_name, url_index, audio_file=audio_file, duration_hint=direct_dur
            ))
            if not success:
                self.log("    ↪ DIRECT-CUT lỗi → quay về đường cũ MUX + cắt")
        if direct_cut and success:
            if not self.keep_temp:
                for p in (video_file, audio_file):
                    try:
                        if p and p.exists(): p.unlink()
                    except Exception:
                        pass
            self._v94_mark("postprocess_ms", "postprocess_complete", ok=bool(success))
            if success and url_index is not None and self.excel_path and self.status_col_name:
                if write_status_to_excel(self.excel_path, url_index, self.status_col_name, "DONE", self.log,
                                         expected_url=url, url_col_name=self.url_col_name, stats=self._v94["kpi"]):
                    self._v94_mark("status_done_ms", "status_done")
                else:
                    self._v94["kpi"]["status_write_ok"] = False
            self._v94["kpi"]["pipeline_total_ms"] = round((time.perf_counter_ns() - self._v94["t0_ns"]) / 1e6, 2)
            self.log(
                f"⏱️ PIPELINE TOTAL: {time.perf_counter()-t_total_start:.3f}s | "
                f"hot480={hot_ready:.3f}s | media-wait={t_media_done-t_media_wait:.3f}s"
            )
            self.log(f"=== [END] {url} (Thành công: {success}) ===")
            self._v94_emit(url, url_index, success)
            return

        self._v94_mark("mux_begin_ms", "mux_begin")
        _t_mux = time.perf_counter_ns()
        _mux_ok = mux_480_stream_copy(video_file, audio_file, combined_file, self.log)
        self._v94["kpi"]["mux_ms"] = round((time.perf_counter_ns() - _t_mux) / 1e6, 2)
        _mdg = _V94_MUX_DIAG.pop(str(combined_file), None)
        if _mdg:
            self._v94["kpi"]["mux_diag"] = _mdg
            TELEM.span(self._v94["job"], "mux_diag", **_mdg)
        self._v94_mark("mux_end_ms", "mux_end", ok=bool(_mux_ok))
        if not _mux_ok:
            self._v94["fail"] = "mux_fail"
            self._v94_emit(url, url_index, False)
            return

        hot_ready = time.perf_counter() - t_total_start
        self._v94_mark("hot_ready_ms", "hot_ready", path="mux")
        if V94_VALIDATE_MUX:
            # V9.4: KHÔNG đi tiếp/ghi DONE nếu file không có đủ video + audio đọc được.
            # V9.4.1: xác thực file OUTPUT (báo cáo cuối của ffmpeg), không chỉ header của input.
            _t_val = time.perf_counter_ns()
            _vinfo = _V94_MUX_INFO.pop(str(combined_file), None) if V94_VALIDATE_MODE == "mux_log" else None
            _mi = _vinfo if isinstance(_vinfo, dict) else {}
            _ins = [x for x in (_mi.get("video_duration"), _mi.get("audio_duration")) if x and x > 0]
            if len(_ins) == 2 and not _mi.get("in_dur_unknown"):
                _expected = min(_ins)
                _tol = max(0.3, 0.01 * _expected)
            else:
                # Review V9.4.1: không đủ thời lượng của CẢ HAI input → không đặt sàn thời lượng (thời lượng resolver
                # chỉ là của video; audio có thể ngắn hơn hợp lệ với -shortest) → luật V9.4.0: có video+audio+duration>0.
                _expected = 0.0
                _tol = 0.0
            _strict = "off"
            _fail_why = ""
            _use_log = bool(_vinfo and _vinfo.get("has_video") and _vinfo.get("has_audio")
                            and float(_vinfo.get("format_duration") or 0) > 0)
            if V94_VALIDATE_MUX_STRICT and _use_log:
                _ok_s, _why_s = _v94_strict_mux_check(_vinfo)
                if _ok_s is True:
                    _strict = "OK"
                else:
                    _use_log = False
                    _strict = ("FALLBACK" if _ok_s is None else "FAIL") + f"({_why_s})"
            if _use_log:
                _valid = True
            else:
                _pinfo = _ffprobe_av_info(combined_file)      # fallback chắc chắn
                _pinfo["source"] = "ffprobe"
                for _k in ("out_time", "out_video_kb", "out_audio_kb"):
                    if _k in _mi:
                        _pinfo[_k] = _mi[_k]
                _valid = bool(_pinfo.get("has_video") and _pinfo.get("has_audio")
                              and float(_pinfo.get("format_duration") or 0) > 0)
                if not _valid:
                    _fail_why = f"ffprobe video={_pinfo.get('has_video')} audio={_pinfo.get('has_audio')}"
                elif V94_VALIDATE_MUX_STRICT and _expected > 0:
                    _vd = float(_pinfo.get("video_duration") or _pinfo.get("format_duration") or 0)
                    _ad = float(_pinfo.get("audio_duration") or 0)
                    if _vd < _expected - _tol or (0 < _ad < _expected - _tol):
                        _valid = False
                        _fail_why = f"ffprobe_short v={_vd:.2f}s a={_ad:.2f}s < {_expected:.2f}s"
                _vinfo = _pinfo
            self._v94["kpi"]["validate_ms"] = round((time.perf_counter_ns() - _t_val) / 1e6, 2)
            self._v94_mark("file_validated_ms", "file_validated", ok=_valid,
                           w=_vinfo.get("width"), h=_vinfo.get("height"),
                           vdur=_vinfo.get("video_duration"), adur=_vinfo.get("audio_duration"),
                           out_time=_vinfo.get("out_time"), out_video_kb=_vinfo.get("out_video_kb"),
                           out_audio_kb=_vinfo.get("out_audio_kb"), strict=_strict, source=_vinfo.get("source"))
            if _valid:
                _short = min(int(_vinfo.get("width") or 0), int(_vinfo.get("height") or 0))
                if _short and not (300 <= _short <= 490):
                    self.log(f"    ⚠️ VALIDATE: cạnh ngắn {_short}px khác dải 480p thường gặp (kiểm tra itag)")
                _out_t = _vinfo.get("out_time")
                self.log(f"    ✅ VALIDATE {'mux-log' if _vinfo.get('source') == 'mux_log' else 'ffprobe'}: "
                         f"{_vinfo.get('width')}x{_vinfo.get('height')} | "
                         f"v={float(_vinfo.get('video_duration') or 0):.2f}s a={float(_vinfo.get('audio_duration') or 0):.2f}s"
                         + (f" | out={float(_out_t):.2f}s" if _out_t is not None else "")
                         + f" | strict={_strict}")
            else:
                _fail_why = _fail_why or _strict
                self._v94["fail"] = f"validate_fail:{_fail_why}"
                self.log(f"    ❌ VALIDATE thất bại ({_fail_why}) → KHÔNG DONE, không MOVE")
                if not self.keep_temp:
                    for _p in (combined_file, video_file, audio_file):
                        try:
                            if _p and _p.exists():
                                _p.unlink()
                        except OSError:
                            pass
                self._v94_emit(url, url_index, False)
                return
        _vf = str((video_data_used or {}).get("video_format_id") or "?")
        _af = str((audio_data_used or {}).get("audio_format_id") or "?")
        _inf = "[inferred]" if (audio_data_used or {}).get("audio_inferred_single_ja") else ""
        self.log(
            f"🚀 HOT 480P READY: {hot_ready:.3f}s từ lúc worker nhận URL | "
            f"{_vf}+{_af}{_inf} JA tải SONG SONG + mux copy"
        )

        _t_rn = time.perf_counter()
        _rn_stats = {}
        if not _move_file_fast_retry(combined_file, out_file_final, self.log, timeout_sec=8.0, stats=_rn_stats):
            self._v94["fail"] = "move_fail"
            self._v94_emit(url, url_index, False)
            return
        self._v94["kpi"]["pp_rename_ms"] = round((time.perf_counter() - _t_rn) * 1000.0, 1)
        self._v94["kpi"]["rename_retries"] = int(_rn_stats.get("retries", 0))
        self.log(f"✅ Success 480p HARD: {base_name}.mp4")

        success = bool(self._cut_after_merge(out_file_final, base_name, url_index))
        self._v94_mark("postprocess_ms", "postprocess_complete", ok=bool(success))
        if not success:
            self._v94["fail"] = "postprocess_fail"
            self.log("❌ Hậu xử lý/cut/move thất bại → KHÔNG ghi DONE.")

        if not self.keep_temp:
            for p in (video_file, audio_file, combined_file):
                try:
                    if p and p.exists(): p.unlink()
                except Exception:
                    pass

        if success and url_index is not None and self.excel_path and self.status_col_name:
            if write_status_to_excel(self.excel_path, url_index, self.status_col_name, "DONE", self.log,
                                     expected_url=url, url_col_name=self.url_col_name, stats=self._v94["kpi"]):
                self._v94_mark("status_done_ms", "status_done")
            else:
                self._v94["kpi"]["status_write_ok"] = False
        self._v94["kpi"]["pipeline_total_ms"] = round((time.perf_counter_ns() - self._v94["t0_ns"]) / 1e6, 2)

        self.log(
            f"⏱️ PIPELINE TOTAL: {time.perf_counter()-t_total_start:.3f}s | "
            f"hot480={hot_ready:.3f}s | media-wait={t_media_done-t_media_wait:.3f}s"
        )
        self.log(f"=== [END] {url} (Thành công: {success}) ===")
        self._v94_emit(url, url_index, success)

    def run(self):
        if self.links_with_index:
            url, url_index = self.links_with_index[0]
            self._process_single_url(url, url_index)
        self.signals.finished.emit()


# ==============================================================================
# 🖥️ PHẦN GIAO DIỆN QT (FINAL LAYOUT V5) - GIỮ NGUYÊN 100%
# ==============================================================================

class LoggerQt:
    def __init__(self, text_widget: QTextEdit, interval_ms: int = 100):
        self.t = text_widget; self.q = [] 
        self._lock = threading.Lock()
        self._timer = QTimer(); self._timer.setInterval(interval_ms); self._timer.timeout.connect(self._drain)
    def start(self): self._timer.start()
    def stop(self):
        try: self._timer.stop()
        except Exception: pass
    def log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        with self._lock:
            self.q.append(f"[{ts}] {msg}")
    def _drain(self):
        with self._lock:
            if not self.q: return
            lines = self.q[:]
            self.q.clear()
        for line in lines:
            self.t.append(line)
            self.t.ensureCursorVisible()

class PremiumDownloaderApp(QWidget):
    log_signal = Signal(str)
    log_html_signal = Signal(str) 
    realtime_stopped_signal = Signal() 

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Downloader Tool — V7.2 HARD480 NO-STALL")
        self.setMinimumSize(1100, 680)
        self.setFont(QFont("Segoe UI", 10))
        
        self.batch_executor = None 
        self.cutout_touched = False
        self.keep_temp = False
        self.realtime_thread = None
        self.realtime_stop_event = threading.Event()
        self.seen_urls = set()
        
        self.accents = [("Purple", "#a88bfa"), ("Blue", "#2d89ef"), ("Green", "#0a7a4f"),
            ("Pink", "#ff6aa3"), ("Orange", "#ff8c42"), ("Gray", "#6b7280")]
        self.accent = self.accents[0][1]
        
        self.worker_signals = WorkerSignals()

        self._build_ui() 
        
        self.worker_signals.log_message.connect(self._handle_log_signal)
        self.worker_signals.url_finished.connect(self._on_single_url_finished)
        self.log_signal.connect(self._handle_log_signal)
        self.log_html_signal.connect(self._handle_html_log)
        self.realtime_stopped_signal.connect(self._stop_realtime_ui_reset)

        self._bridge_logic_variables() 
        self._load_theme()
        self._load_config()
        
        self.logger_qt = LoggerQt(self.log_widget, interval_ms=80); self.logger_qt.start()

    def _handle_log_signal(self, msg):
        self.logger_qt.log(msg)

    def _handle_html_log(self, html_msg):
        cursor = self.log_widget.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.log_widget.setTextCursor(cursor)
        self.log_widget.insertHtml(html_msg + "<br>")
        self.log_widget.ensureCursorVisible()

    def _build_ui(self):
        root = QHBoxLayout(self); root.setContentsMargins(12, 12, 12, 12)
        
        left = QVBoxLayout(); left_frame = QFrame(); left_frame.setLayout(left); 
        left_frame.setMaximumWidth(600)
        left_frame.setMinimumWidth(600) 
        
        header = QLabel("Downloader V9.5 — 480p 244→135 + audio Nhật | 360p nhanh (itag 18) + JA"); header.setObjectName("HeaderLabel"); header.setMinimumHeight(60)
        header.setStyleSheet("font-size:18px; font-weight:700;"); left.addWidget(header)
        
        left.addWidget(QLabel("1. Chọn file Excel (Data Input):"))
        self.input_excel = QLineEdit(os.path.join(BASE_DIR, "check.xlsx")); self.input_excel.setPlaceholderText("Select .xlsx file...")
        
        h1 = QHBoxLayout()
        btn_excel = QPushButton("Browse"); btn_excel.clicked.connect(self._pick_excel); btn_excel.setFixedWidth(100) 
        btn_open_excel = QPushButton("Mở Excel"); btn_open_excel.clicked.connect(self._open_excel_file); btn_open_excel.setFixedWidth(100)
        
        h1.addWidget(self.input_excel)
        h1.addWidget(btn_excel)
        h1.addWidget(btn_open_excel) 
        left.addLayout(h1)
        
        left.addWidget(QLabel("2. Chọn thư mục lưu Download:"))
        self.input_folder = QLineEdit(BASE_DIR); self.input_folder.setPlaceholderText("Select output folder...")
        
        h2 = QHBoxLayout()
        btn_folder = QPushButton("Browse"); btn_folder.clicked.connect(self._pick_folder); btn_folder.setFixedWidth(80) 
        
        h2.addWidget(self.input_folder); 
        h2.addWidget(btn_folder); 
        left.addLayout(h2)
        
        left.addWidget(QLabel("--- Settings & Cutting ---"))
        
        sett_layout = QVBoxLayout()
        
        g_cut = QGridLayout()
        g_cut.addWidget(QLabel("Prefix:"), 0, 0)
        self.input_prefix = QLineEdit("ok")
        self.input_prefix.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed) 
        g_cut.addWidget(self.input_prefix, 1, 0)
        
        g_cut.addWidget(QLabel("Cut Seconds:"), 0, 1)
        self.input_cutsec = QLineEdit("71")
        self.input_cutsec.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        g_cut.addWidget(self.input_cutsec, 1, 1)
        
        g_cut.setColumnStretch(0, 1) 
        g_cut.setColumnStretch(1, 1)
        
        sett_layout.addLayout(g_cut)
        
        sett_layout.addWidget(QLabel("Chất lượng:"))
        self.radio_auto = QRadioButton("Cứng 480p chất lượng — ưu tiên 244, fallback 135 tức thì + audio Nhật"); self.radio_auto.setChecked(True)
        # V9.5: radio "Ignored" thứ nhất → 360p nhanh (itag 18). Chỉ hiện khi V95_360P=1.
        self.radio_light = QRadioButton("360p nhanh — itag 18 (hình + tiếng 1 file, không ghép)")
        self.radio_light.setToolTip(
            "1 GET duy nhất, không HEAD/Range preflight, không ffmpeg. itag 18 chỉ có track tiếng MẶC ĐỊNH:\n"
            "nếu tiếng mặc định không phải tiếng Nhật mà video có track Nhật → tự chuyển video đó sang 480p + audio Nhật."
        )
        self.radio_hard = QRadioButton("Ignored")
        
        # V9.5: xếp DỌC để nhìn thấy đủ chữ của cả 2 lựa chọn
        h_rad = QVBoxLayout()
        h_rad.addWidget(self.radio_auto)
        if V95_360P:
            h_rad.addWidget(self.radio_light)
        sett_layout.addLayout(h_rad)

        sett_layout.addWidget(QLabel("Công cụ tải (Download Engine):"))
        self.combo_downloader = QComboBox()
        self.combo_downloader.addItem("SMART AUTO — Direct small + Aria2 RPC (khuyến nghị)", "aria2c")
        self.combo_downloader.addItem("IDM — video 244→135; audio Nhật tải song song", "idm")
        self.combo_downloader.setToolTip(
            f"IDM: {IDM_PATH}" if IDM_PATH else "IDM: chưa tìm thấy IDMan.exe trên máy"
        )
        sett_layout.addWidget(self.combo_downloader)

        sett_layout.addWidget(QLabel("Folder lưu file CẮT (Optional):"))
        self.input_cutout = QLineEdit(); self.input_cutout.setPlaceholderText("Default: same as video")
        btn_cutout = QPushButton("..."); btn_cutout.setFixedWidth(40); btn_cutout.clicked.connect(self._pick_cutout)
        h_co = QHBoxLayout(); h_co.addWidget(self.input_cutout); h_co.addWidget(btn_cutout); sett_layout.addLayout(h_co)

        sett_layout.addWidget(QLabel("Folder MOVE cuối cùng (Optional):"))
        self.input_finalmove = QLineEdit(); self.input_finalmove.setPlaceholderText("Move cut folder here (Smart Move logic)")
        btn_move = QPushButton("..."); btn_move.setFixedWidth(40); btn_move.clicked.connect(self._pick_finalmove)
        h_mv = QHBoxLayout(); h_mv.addWidget(self.input_finalmove); h_mv.addWidget(btn_move); sett_layout.addLayout(h_mv)

        h_opt = QHBoxLayout()
        self.chk_keeptemp = QCheckBox("Keep Temp")
        h_opt.addWidget(self.chk_keeptemp)
        h_opt.addWidget(QLabel("Threads:"))
        self.spin_threads = QSpinBox(); self.spin_threads.setRange(1, 12); self.spin_threads.setValue(4)
        h_opt.addWidget(self.spin_threads)
        h_opt.addStretch()
        sett_layout.addLayout(h_opt)

        left.addLayout(sett_layout)
        
        left.addWidget(QLabel("--- Realtime Monitor & Controls ---"))
        h_rt = QHBoxLayout()
        
        self.btn_start = QPushButton("START MONITOR"); self.btn_start.clicked.connect(self.start_realtime)
        self.btn_stop = QPushButton("STOP MONITOR"); self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_realtime)
        
        h_rt.addWidget(self.btn_start)
        h_rt.addWidget(self.btn_stop)
        left.addLayout(h_rt)
        
        left.addWidget(QLabel("--- Appearance ---"))
        
        left.addWidget(QLabel("Giao diện (Accent Color):")); self.combo_accent = QComboBox();
        for name, col in self.accents: self.combo_accent.addItem(name, col)
        self.combo_accent.currentIndexChanged.connect(self._apply_accent); left.addWidget(self.combo_accent)
        
        hhex = QHBoxLayout(); self.hex_entry = QLineEdit(); self.hex_entry.setPlaceholderText("#rrggbb")
        self.btn_apply_hex = QPushButton("Apply Accent"); self.btn_apply_hex.clicked.connect(self._apply_hex)
        hhex.addWidget(self.hex_entry); hhex.addWidget(self.btn_apply_hex); left.addLayout(hhex)
        
        left.addStretch() 

        root.addWidget(left_frame)
        
        right = QVBoxLayout(); right_frame = QFrame(); right_frame.setLayout(right)
        
        self.status_badge = QLabel("IDLE"); self.status_badge.setFixedWidth(140); self.status_badge.setAlignment(Qt.AlignCenter)
        self.status_badge.setStyleSheet("background:#2b2b2b; border-radius:8px; padding:6px; font-weight:700;")
        topbar = QHBoxLayout(); topbar.addWidget(self.status_badge); topbar.addStretch(); right.addLayout(topbar)
        
        self.log_widget = QTextEdit(); self.log_widget.setReadOnly(True)
        self.log_widget.setStyleSheet("font-family: Consolas, monospace; font-size:12px;")
        self.log_widget.setAcceptRichText(True) 
        right.addWidget(self.log_widget)
        
        bottom = QHBoxLayout()
        
        self.btn_save_config = QPushButton("Lưu cấu hình"); self.btn_save_config.clicked.connect(self._save_config)
        self.btn_save_config.setFixedWidth(120) 
        
        btn_clear = QPushButton("Clear Log"); btn_clear.clicked.connect(self.log_widget.clear)
        btn_clear.setFixedWidth(120)

        bottom.addWidget(self.btn_save_config)
        bottom.addStretch() 
        bottom.addWidget(btn_clear)

        right.addLayout(bottom)
        
        root.addWidget(right_frame)
        
        self._apply_stylesheet(self.accent)

    def _bridge_logic_variables(self):
        self.out_entry = self.input_folder
        self.prefix_entry = self.input_prefix
        self.cutsec_entry = self.input_cutsec
        self.cutout_entry = self.input_cutout
        self.final_move_entry = self.input_finalmove
        self.keep_temp_checkbox = self.chk_keeptemp
        self.auto_radio = self.radio_auto
        self.light_radio = self.radio_light
        self.hard_radio = self.radio_hard 
        self.txt_entry = QLineEdit() 

    def _pick_excel(self):
        p, _ = QFileDialog.getOpenFileName(self, "Select Excel", BASE_DIR, "Excel Files (*.xlsx);;All Files (*)")
        if p: self.input_excel.setText(p)

    def _open_excel_file(self):
        path = self.input_excel.text().strip()
        if os.path.exists(path):
            try: os.startfile(path)
            except Exception as e: QMessageBox.warning(self, "Lỗi", f"Không thể mở file: {e}")
        else: QMessageBox.warning(self, "Lỗi", "File Excel không tồn tại hoặc chưa chọn.")

    def _pick_folder(self):
        p = QFileDialog.getExistingDirectory(self, "Select Save Folder", BASE_DIR)
        if p: self.input_folder.setText(p)

    def _pick_cutout(self):
        p = QFileDialog.getExistingDirectory(self, "Select Cut Output Folder", BASE_DIR)
        if p: 
            self.input_cutout.setText(p)
            self.cutout_touched = True

    def _pick_finalmove(self):
        p = QFileDialog.getExistingDirectory(self, "Select Final Move Folder", BASE_DIR)
        if p: self.input_finalmove.setText(p)

    def start_realtime(self):
        excel_path = self.input_excel.text().strip()
        out = self.input_folder.text().strip()
        
        if not os.path.exists(excel_path):
            QMessageBox.warning(self, "Thiếu Excel", "Hãy chọn file Excel để Realtime Monitor hoạt động."); return
            
        if not out:
            QMessageBox.warning(self, "Thiếu thư mục","Hãy chọn thư mục lưu video."); return
        ensure_dir(out)

        engine = (self.combo_downloader.currentData() or "aria2c").lower()
        if not yt_dlp:
            QMessageBox.warning(self, "Thiếu yt-dlp", "Tool cần yt-dlp để lấy HARD 480p (244→135) + audio Nhật."); return
        if engine == "idm" and not IDM_PATH:
            QMessageBox.warning(
                self, "Không tìm thấy IDM",
                "Đã chọn IDM nhưng tool không tìm thấy IDMan.exe.\n"
                "Hãy cài IDM bình thường hoặc đặt IDMan.exe cạnh file tool rồi mở lại tool."
            ); return
        if engine == "aria2c" and not ARIA2C_PATH:
            QMessageBox.warning(self, "Không tìm thấy Aria2c", "Đã chọn Aria2c nhưng không tìm thấy aria2c.exe."); return
        
        if self.realtime_thread and self.realtime_thread.is_alive(): return

        self.log_widget.clear()
        self._save_config()
        self.realtime_stop_event.clear()
        
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.status_badge.setText("MONITORING"); 
        self.status_badge.setStyleSheet(f"background:{self._adjust_brightness(self.accent,-0.12)}; border-radius:8px; padding:6px; font-weight:700;")
        
        self.seen_urls.clear()
        max_workers = self.spin_threads.value()
        self.batch_executor = ThreadPoolExecutor(max_workers=max_workers)
        # V7.6: keep visitorData prewarm; tune only transfer hot path for sub-3s target. This costs only
        # monitor startup time, not the first fresh-video hot path.
        visitor_warm = False
        try:
            visitor_warm = bool(_prewarm_480_visitor_data(force=True))
        except Exception:
            visitor_warm = False

        # Giữ TLS/player connections nóng + dựng sẵn persistent YoutubeDL resolver workers.
        try:
            _start_fast_keepalive()
            self.batch_executor.submit(_prewarm_fast_youtube_connection)
            _YTDLP_RESOLVER_EXECUTOR.submit(_prewarm_ytdlp_480_resolver_thread)
            _YTDLP_RESOLVER_EXECUTOR.submit(_prewarm_ytdlp_480_resolver_thread)
            _YTDLP_RESOLVER_EXECUTOR.submit(_prewarm_ytdlp_480_resolver_thread)
            _YTDLP_RESOLVER_EXECUTOR.submit(_prewarm_ytdlp_480_resolver_thread)
            _ver, _js = _youtube_runtime_summary()
            self.logger_qt.log(
                f"⚡ V6 WARM: yt-dlp={_ver} | JS runtime={_js} | "
                f"480p/244→135 visitor-prewarm={'OK' if visitor_warm else 'MISS'} + native race + RANGE32 + robust EJS"
            )
        except Exception:
            pass
        # Khởi động sẵn aria2 RPC daemon để lúc file lớn/unknown không tốn thời gian spawn process.
        if engine == "aria2c":
            try:
                self.batch_executor.submit(_start_aria2_rpc_daemon)
            except Exception:
                pass
        # V9.4.1: làm nóng ffmpeg/ffprobe (2 lần: nguội/nóng) + import bảng Innertube, in 1 dòng FF BASELINE.
        # Chạy trên thread riêng (không chiếm slot Max Threads, không chặn job đầu tiên).
        if V94_FF_WARMUP_START or V94_PREWARM_INNERTUBE:
            try:
                _out_dir = Path(out)
                threading.Thread(target=_v94_start_baseline,
                                 args=(self.log_signal.emit, str(_out_dir), str(_out_dir.parent / "_temp_yt480p")),
                                 name="V94Baseline", daemon=True).start()
            except Exception:
                pass
        
        if self.quality_mode() == "360fast":
            self.logger_qt.log("🎞️ CHẾ ĐỘ: 360p nhanh (itag 18, 1 GET, không ghép). Video có track Nhật nhưng tiếng mặc "
                               "định khác → tự chuyển sang 480p+JA cho video đó.")
        self.realtime_thread = threading.Thread(target=self._realtime_worker, daemon=True)
        self.realtime_thread.start()
        engine = (self.combo_downloader.currentData() or "aria2c").upper()
        self.logger_qt.log(
            f"🔄 Bắt đầu **{V94_VERSION} (CR-CHECK + IN-PLACE-RESIZE + RANGE-FIRST + 403-NO-ARIA2 + FF-PREWARM + STRICT-VALIDATE + RACE={'ALL' if int(V94_FIRST_WAVE_RACE_MAX) < 0 else V94_FIRST_WAVE_RACE_MAX}) + V9.3 SHARED-TLS + FIRST-WAVE-RACE + EAGER-STEAL + DIRECT-CUT 244→135 SHARED-REDIRECT LAZY-FALLBACK SAFE-NATIVE-403 + {engine}** | Native + yt-dlp race + "
            f"video/audio song song + zero-copy mux | Max Threads: {max_workers}"
        )
        self.logger_qt.log(
            "🛡️ 403-GUARD: android/android_vr cannot win raw adaptive URL | SAFE-REFRESH=1x robust"
        )
        self.logger_qt.log(
            "🎯 V8 profile: video 64-80MB=24 ranges | 80-150MB=32 ranges | JA audio >=64KiB RANGE-FIRST | "
            "tiny JA<=1MB=4 ranges | audio min-part=64KiB"
        )

    def stop_realtime(self):
        if not self.realtime_thread: return
        self.logger_qt.log("🛑 Đang dừng Realtime... Đợi tác vụ đang chạy kết thúc.")
        self.realtime_stop_event.set()
        self.btn_stop.setEnabled(False)
        if self.batch_executor:
            self.batch_executor.shutdown(wait=False)
            self.batch_executor = None

    def _realtime_worker(self):
        excel_path = self.input_excel.text().strip()

        # Scanner thường chạy cùng folder với check.xlsx. Theo dõi cả 2 vị trí
        # để không phụ thuộc CWD của shortcut / PowerShell.
        trigger_candidates = []
        try:
            trigger_candidates.append(Path.cwd() / REALTIME_WAKEUP_FILE)
        except Exception:
            pass
        try:
            trigger_candidates.append(Path(excel_path).resolve().parent / REALTIME_WAKEUP_FILE)
        except Exception:
            pass

        # Bỏ path trùng nhưng giữ thứ tự.
        _uniq = []
        _seen_paths = set()
        for _p in trigger_candidates:
            _s = str(_p)
            if _s not in _seen_paths:
                _seen_paths.add(_s)
                _uniq.append(_p)
        trigger_candidates = _uniq or [Path(REALTIME_WAKEUP_FILE)]

        trigger_sig = _rt_trigger_signature(trigger_candidates)
        excel_sig = _rt_file_signature(excel_path)
        wake_reason = "STARTUP"
        wake_ns = time.perf_counter_ns()      # V9.4.1: mốc phát hiện (KPI3 = phát hiện → GET media đầu tiên)
        wake_lag_ms = None

        self.log_signal.emit(
            f"⚡ WAKE-UP FAST: trigger_wakeup.txt + Excel change | "
            f"check={REALTIME_WAKEUP_CHECK_SEC*1000:.0f}ms | "
            f"fallback={REALTIME_FALLBACK_SCAN_SEC:.1f}s"
        )

        while not self.realtime_stop_event.is_set():
            try:
                def _rt_log(m): pass
                links, url_col, status_col = read_links_excel_url_only(excel_path, _rt_log)

                new_items = []
                for url, idx in links:
                    if url not in self.seen_urls:
                        new_items.append((url, idx))

                if new_items and wake_reason in ("TRIGGER", "EXCEL"):
                    self.log_signal.emit(
                        f"⚡ WAKE-UP {wake_reason} → đọc Excel NGAY | "
                        f"{len(new_items)} URL mới"
                    )

                for url, idx in new_items:
                    if self.realtime_stop_event.is_set(): break
                    self.seen_urls.add(url)

                    # V7 HOT START: submit downloader BEFORE any extra Excel metadata reads.
                    # Old code re-read the whole xlsx 2-3 times before starting the worker.
                    if self.batch_executor:
                        worker = Worker(self, excel_path, url_col, status_col)
                        worker._v94_wake_ns = wake_ns
                        worker._v94_wake_lag_ms = wake_lag_ms
                        worker._v94_wake_reason = wake_reason
                        TELEM.span(_extract_youtube_video_id(url) or "UNKNOWN", "worker_submit",
                                   row=idx + 2 if idx is not None else None, wake=wake_reason)
                        worker._v94_submit_ns = time.perf_counter_ns()
                        self.batch_executor.submit(worker._process_single_url, url, idx)

                    # UI metadata is non-critical and can be read after the download has already started.
                    channel_id = read_excel_cell_value(excel_path, idx, "CHANNEL ID", _rt_log)
                    if not channel_id:
                         channel_id = read_excel_cell_value(excel_path, idx, "ID KÊNH", _rt_log)
                    channel_name = read_excel_cell_value(excel_path, idx, "TÊN KÊNH", _rt_log)

                    log_html = f"""
                    <table border="0" cellspacing="0" cellpadding="0" style="margin-top:5px; margin-bottom:5px;">
                        <tr>
                            <td valign="middle" style="padding-left: 10px;">
                                <span style="font-size:11pt;">🔔 <b>Phát hiện video mới:</b></span><br>
                                ID: <span style='color:#ff6aa3; font-weight:bold;'>{channel_id or "N/A"}</span><br>
                                Kênh: <span style='color:#22c55e; font-weight:bold;'>{channel_name or "N/A"}</span>
                            </td>
                        </tr>
                    </table>
                    <hr style="border: 0; height: 1px; background-color: #333; margin: 5px 0;">
                    """

                    self.log_html_signal.emit(log_html)

                # Không còn sleep(1). Chỉ stat trigger/Excel mỗi ~25ms.
                # Nếu scanner không gửi trigger thì sau 1s vẫn quét Excel như bản cũ.
                trigger_sig, excel_sig, wake_reason = _rt_wait_for_wakeup(
                    self.realtime_stop_event,
                    trigger_candidates,
                    excel_path,
                    trigger_sig,
                    excel_sig,
                )
                wake_ns = time.perf_counter_ns()
                wake_lag_ms = _V94_LAST_WAKE.get("lag_ms") if wake_reason in ("TRIGGER", "EXCEL") else None

            except Exception as e:
                self.log_signal.emit(f"❌ Lỗi Realtime Thread: {e}")
                # Lỗi tạm thời vẫn cho dừng ngay nếu người dùng bấm Stop.
                self.realtime_stop_event.wait(0.25)
                trigger_sig = _rt_trigger_signature(trigger_candidates)
                excel_sig = _rt_file_signature(excel_path)
                wake_reason = "RECOVER"
                wake_ns = time.perf_counter_ns()
                wake_lag_ms = None

        if not self.chk_keeptemp.isChecked():
            tdir = Path(self.input_folder.text().strip()).parent / "_temp_yt480p"
            try:
                if os.path.exists(tdir) and not os.listdir(tdir): os.rmdir(tdir)
            except OSError: pass

        self.log_signal.emit("✅ Đã dừng chế độ Realtime.")
        self.realtime_stopped_signal.emit()

    def _stop_realtime_ui_reset(self):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.status_badge.setText("IDLE"); 
        self.status_badge.setStyleSheet("background:#2b2b2b; border-radius:8px; padding:6px; font-weight:700;")
        self.realtime_thread = None

    def _on_single_url_finished(self, url, url_index, success):
        pass

    def quality_mode(self) -> str:
        """'360fast' hoặc '480hard'. Tắt cờ V95_360P → luôn '480hard' (hành vi V9.4)."""
        try:
            if V95_360P and self.radio_light.isChecked():
                return "360fast"
        except Exception:
            pass
        return "480hard"

    def _save_config(self):
        try:
            # V9.5: "360fast" khi chọn radio 360p (và cờ V95_360P bật); mặc định "480hard" (244→135 + audio Nhật).
            qm = self.quality_mode()
            data = {
                "excel_path": self.input_excel.text(), "save_folder": self.input_folder.text(),
                "cut_prefix": self.input_prefix.text(), "cut_seconds": self.input_cutsec.text(),
                "cut_out": self.input_cutout.text(), "final_move": self.input_finalmove.text(),
                "quality_mode": qm, "download_engine": (self.combo_downloader.currentData() or "aria2c"),
                "threads": self.spin_threads.value(), "keep_temp": self.chk_keeptemp.isChecked()
            }
            with open(CONFIG_JSON, "w", encoding="utf-8") as f: json.dump(data, f, indent=4, ensure_ascii=False)
        except Exception: pass

    def _load_config(self):
        if os.path.exists(CONFIG_JSON):
            try:
                with open(CONFIG_JSON, "r", encoding="utf-8") as f: data = json.load(f)
                self.input_excel.setText(data.get("excel_path", ""))
                self.input_folder.setText(data.get("save_folder", ""))
                self.input_prefix.setText(data.get("cut_prefix", "ok"))
                self.input_cutsec.setText(str(data.get("cut_seconds", "71")))
                self.input_cutout.setText(data.get("cut_out", ""))
                self.input_finalmove.setText(data.get("final_move", ""))
                self.spin_threads.setValue(data.get("threads", 4))
                self.chk_keeptemp.setChecked(data.get("keep_temp", False))

                engine = str(data.get("download_engine", "aria2c")).lower()
                engine_idx = self.combo_downloader.findData(engine)
                if engine_idx >= 0:
                    self.combo_downloader.setCurrentIndex(engine_idx)
                
                # V9.5: đọc lại đúng lựa chọn (V9.4 luôn ép 480p). Giá trị lạ/legacy → 480p.
                if V95_360P and str(data.get("quality_mode", "")).lower() == "360fast":
                    self.radio_light.setChecked(True)
                else:
                    self.radio_auto.setChecked(True)
            except: pass

    def _apply_stylesheet(self, accent_hex: str):
        self.accent = accent_hex; darker = self._adjust_brightness(accent_hex, -0.12); lighter = self._adjust_brightness(accent_hex, 0.10)
        self.setStyleSheet(f"""
            QWidget {{ background: #121216; color: #e6eef3; font-family: 'Segoe UI'; }}
            QLineEdit, QSpinBox, QComboBox, QTextEdit {{ background: #1b1b1f; border: 1px solid #2b2b2f; padding:6px; border-radius:6px; color: #e6eef3; }}
            QPushButton {{ background: {accent_hex}; color: white; border-radius:8px; padding:8px 12px; font-weight:600; }}
            QPushButton:hover {{ background: {lighter}; }}
            QPushButton:disabled {{ background: #333; color: #777; }}
            QTextEdit {{ background: #09090b; border: 1px solid #222226; border-radius:6px; }}
            QLabel#HeaderLabel {{ color: {accent_hex}; font-weight:700; font-size:18px; }}
            QComboBox QAbstractItemView::item:selected {{ background: {accent_hex}; color: #ffffff; }}
            QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QTextEdit:focus {{ outline: 2px solid {accent_hex}; }}
            QCheckBox, QRadioButton {{ color: #e6eef3; spacing: 8px; }}
            QCheckBox::indicator, QRadioButton::indicator {{ width: 16px; height: 16px; background: #1b1b1f; border: 1px solid #2b2b2f; border-radius: 4px; }}
            QCheckBox::indicator:checked, QRadioButton::indicator:checked {{ background: {accent_hex}; border: 1px solid {accent_hex}; }}
        """); 
        if self.status_badge.text() != "IDLE":
             self.status_badge.setStyleSheet(f"background:{darker}; border-radius:8px; padding:6px; font-weight:700;")

    def _apply_accent(self, idx):
        col = self.combo_accent.itemData(idx)
        if col: self.hex_entry.setText(col); self._apply_stylesheet(col); self._save_theme()

    def _apply_hex(self):
        val = (self.hex_entry.text() or "").strip()
        if not val.startswith('#') or len(val) not in (4,7):
            QMessageBox.warning(self, "Hex lỗi", "Hãy nhập mã màu HEX hợp lệ"); return
        self._apply_stylesheet(val); self._save_theme()

    def _adjust_brightness(self, hexcol: str, delta: float) -> str:
        try:
            h = hexcol.lstrip('#');
            if len(h) == 3: h = ''.join([c*2 for c in h])
            r = int(h[0:2],16); g=int(h[2:4],16); b=int(h[4:6],16)
            def clamp(x): return max(0, min(255, int(x)))
            r = clamp(r*(1+delta)); g = clamp(g*(1+delta)); b = clamp(b*(1+delta))
            return f"#{r:02x}{g:02x}{b:02x}"
        except Exception: return hexcol

    def _save_theme(self):
        try:
            cfg = {"accent": self.accent}
            with open(CONFIG_THEME_JSON, 'w', encoding='utf-8') as f: json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception: pass

    def _load_theme(self):
        try:
            if os.path.exists(CONFIG_THEME_JSON):
                with open(CONFIG_THEME_JSON, 'r', encoding='utf-8') as f: cfg = json.load(f)
                accent = cfg.get('accent')
                if accent:
                    for i in range(self.combo_accent.count()):
                        if self.combo_accent.itemData(i).lower() == accent.lower():
                            self.combo_accent.setCurrentIndex(i); break
                    self._apply_stylesheet(accent)
        except Exception: pass

if __name__ == '__main__':
    # Dependency downloader được kiểm tra theo lựa chọn GUI trong Worker.
    # Ở đây chỉ giữ startup không chặn để người dùng vẫn mở GUI chọn Aria2c hoặc IDM.
    if not (YTDLP_PATH or shutil.which("yt-dlp") or yt_dlp):
        pass 

    app = QApplication(sys.argv)
    w = PremiumDownloaderApp()
    w.show()
    sys.exit(app.exec())