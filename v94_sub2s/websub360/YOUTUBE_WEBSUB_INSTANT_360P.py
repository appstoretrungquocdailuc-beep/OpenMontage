# -*- coding: utf-8 -*-
"""
YOUTUBE WEBSUB INSTANT 360P — phát hiện video mới bằng WebSub/PubSubHubbub và/hoặc YouTube Data API (RACE MODE:
                                 WEBSUB_ONLY | API_ONLY | PARALLEL) → TẢI 360P NGAY
                                 và GHI URL VÀO EXCEL (cột D theo Channel ID cột K) SONG SONG.

Đường nóng:  sự kiện hợp lệ → chống trùng (SQLite + RAM) → HÀNG ĐỢI TẢI ƯU TIÊN  ┐  (độc lập)
                                                         → HÀNG ĐỢI EXCEL        ┘
Download worker: resolver (Innertube race 3 client, giống V9_4_SUB2S) → progressive 360p 1 GET
                 | (không có) 360p video + audio tải song song → ffmpeg -c copy
                 | (không có link trực tiếp) yt-dlp resolver dự phòng
                 → file tạm → ffprobe kiểm tra → os.replace → DONE.

Chạy:   python YOUTUBE_WEBSUB_INSTANT_360P.py              (giao diện PySide6)
        python YOUTUBE_WEBSUB_INSTANT_360P.py --headless   (không giao diện)
        python YOUTUBE_WEBSUB_INSTANT_360P.py --selftest [--video ID]   (TEST 1–3, 5 → TEST_REPORT.md)
        python YOUTUBE_WEBSUB_INSTANT_360P.py --bench ID [ID ...] [--reps 3]  (TEST 4 → BENCHMARK.csv)
        python YOUTUBE_WEBSUB_INSTANT_360P.py --diag [--resubscribe]   (chẩn đoán từng kênh WebSub → DIAG_REPORT.md)
        python YOUTUBE_WEBSUB_INSTANT_360P.py --race-report  (so sánh WebSub ↔ API trên dữ liệu thật → RACE_BENCHMARK.md)

Không in secret WebSub / URL media có chữ ký ra log.
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import hmac
import itertools
import json
import os
import queue
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, urljoin

try:
    import urllib3
except ImportError:  # pragma: no cover
    urllib3 = None
try:
    import requests
except ImportError:  # pragma: no cover
    requests = None
try:
    import yt_dlp
except ImportError:  # pragma: no cover
    yt_dlp = None
try:
    import openpyxl
    from openpyxl.utils import column_index_from_string
except ImportError:  # pragma: no cover
    openpyxl = None

VERSION = "WS360-1.0"
BASE_DIR = Path(os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, "frozen", False) else __file__)))
CH_RE = re.compile(r"\b(UC[0-9A-Za-z_-]{22})\b")
VID_RE = re.compile(r"^[0-9A-Za-z_-]{11}$")
ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"
AT = "{http://purl.org/atompub/tombstones/1.0}"
# Topic theo tài liệu YouTube "Subscribe to push notifications" (developers.google.com/youtube/v3/guides/push_notifications)
# WebSub TOPIC (đăng ký với hub) — dạng /xml/feeds/... theo tài liệu YouTube "Subscribe to push notifications".
TOPIC_FMT = "https://www.youtube.com/xml/feeds/videos.xml?channel_id={}"
# Kiến trúc DUY NHẤT: WebSub → Video ID → tải 360p; song song ghi Excel. KHÔNG có feed polling / Data API / quét định kỳ.
# Trang kênh chỉ dùng trong DIAG (chạy tay) để kiểm tra Channel ID tồn tại — không dùng để phát hiện video.
CHANNEL_PAGE_FMT = "https://www.youtube.com/channel/{}"
STATES_ACTIVE = ("DETECTED", "QUEUED", "RESOLVING", "DOWNLOADING", "VERIFYING", "RETRY_WAIT")

DEFAULT_CONFIG = {
    "excel_file": "check.xlsx",
    "excel_sheet": "",                 # trống = sheet đầu tiên
    "col_channel": "K",
    "col_url": "D",
    "excel_enabled": True,
    "download_enabled": True,
    "callback_url": "",                # trống = tự đọc URL https từ ngrok (http://127.0.0.1:4040)
    "listen_host": "0.0.0.0",
    "listen_port": 8765,
    "websub_path": "/websub",
    "hub_url": "https://pubsubhubbub.appspot.com/subscribe",
    "lease_seconds": 432000,
    "max_age_minutes": 30,             # video có 'published' cũ hơn → coi là CẬP NHẬT video cũ, không tải
    "download_dir": "DOWNLOADS",
    "workers": 3,
    "range_connections": 1,            # itag 18 (1 file): 1 GET — đã đạt 1.0–1.5 s, GIỮ NGUYÊN
    "dash_range_connections": 4,       # DASH (hình+tiếng riêng, khi chưa có itag 18): N kết nối/luồng — 1 kết nối bị bóp ~0.8 Mbps
    "retry_schedule_sec": [1, 2, 3, 5, 8, 13, 20, 30, 45, 60],
    "max_concurrent_downloads": 3,
    "v9_path": "",                     # đường dẫn V9_4_SUB2S.py (chỉ dùng để benchmark so sánh)
    "verify_retry_sec": [120, 300, 600, 1800, 3600],   # hub chưa gọi xác minh sau X s → gửi lại (giãn dần)
    "atomic_rename": False,            # False (theo yêu cầu): tải THẲNG vào tên file cuối, không đổi tên;
                                       # True: tải vào .part rồi os.replace (chậm hơn trên Windows có Defender)
    "callback_generation": True,
    # ---- TÍCH HỢP ĐĂNG TIKTOK (TAM) — chạy chung tiến trình ----
    "tiktok_enabled": False,           # bật bằng START_INTEGRATED.bat hoặc ô "Tự đăng TikTok" trên giao diện
    "tam_path": "tam_INSTANT_POST.py", # đường dẫn backend TAM (thư mục có Genlogin.py + chromedriver.exe)
    "col_profile": "C",                # cột PROFILE ID TikTok (cùng hàng với Channel ID cột K)
    "prewarm_mode": "PREWARM_ALL",     # PREWARM_ALL: mở sẵn mọi profile | SMART_PREWARM: chỉ smart_profiles, còn lại mở khi có job
    "smart_profiles": [],
    "open_concurrency": 2,             # số profile Genlogin mở CÙNG LÚC khi khởi động
    "max_parallel_uploads": 4,         # số phiên upload TikTok đồng thời tối đa (backpressure)
    "upload_retry_failed_safe": 1,     # lỗi TRƯỚC khi bấm Post (an toàn) → thử lại tối đa N lần
    "tiktok_dry_run": False,           # True: chạy hết tới khi nút Post sáng rồi DỪNG, KHÔNG bấm Post
    "tam_wait_mode": "",               # cách TAM theo dõi nút Post: "" = mặc định của TAM (event/A) | "lite" (C) | "sparse" (B)
    # ---- NGUỒN PHÁT HIỆN (RACE MODE) ----
    "detection_mode": "PARALLEL",      # WEBSUB_ONLY | API_ONLY | PARALLEL (WebSub + API chạy đồng thời, nguồn nào tới trước thắng)
    "api_keys_file": "api_keys.txt",   # mỗi dòng: TEN_PROJECT|KEY  (hoặc chỉ KEY = 1 project riêng)
    "api_quota_per_project": 10000,    # quota/ngày của 1 Google Cloud project (mặc định Google cấp 10.000)
    "api_project_quota": {},           # quota riêng từng project nếu được Google nâng: {"TEN_PROJECT": 50000}
    "api_quota_safety": 0.95,          # chỉ tiêu tối đa 95% quota (chừa phần cho sai số / dùng tay)
    "api_min_interval_sec": 1.0,       # nhịp NHANH NHẤT cho 1 kênh (chỉ đạt được khi quota đủ — tool tự giãn)
    "api_hot_windows": [],             # giờ vàng (giờ máy): ["20:58-21:03", {"time": "08:59-09:01", "channels": ["UC…"]}]
    "api_concurrency": 8,
    "api_timeout_sec": 4.0,
    "api_quota_log_sec": 300,
    "api_url": "",                     # trống = https://www.googleapis.com/youtube/v3/activities
    "alert_wav": "",                   # file .wav phát khi có video mới (Windows); trống = tắt
    # ---- LỚP MẠNG TẢI 360P ----
    "net_mode": "fast",                # fast: đo DNS/TCP/TLS/chờ máy chủ + DNS cache | legacy: đúng bản trước (rollback)
    "net_warm": True,                  # giữ kết nối ấm tới máy chủ Innertube + máy chủ video vừa dùng (GET /generate_204)
    "net_warm_interval_sec": 40,
    "net_warm_innertube_conns": 3,     # = số client resolver chạy song song
    "net_warm_media_hosts": 6,
    "resolver_host": "",               # "" = www.youtube.com (như yt-dlp) | "youtubei.googleapis.com" (máy chủ API của app Android)
    # ---- TÊN FILE ----
    "filename_mode": "TITLE",          # TITLE: '<tiêu đề gốc>.mp4' | TITLE_ID: '<tiêu đề>_<ID>.mp4' | ID_360P: '<ID>_360p.mp4'
    "filename_title_max": 0,           # 0 = không tự cắt (chỉ cắt khi vượt giới hạn đường dẫn Windows)
    "filename_max_path": 240,          # tổng độ dài đường dẫn tối đa (Windows giới hạn 260)
}
MODES = ("WEBSUB_ONLY", "API_ONLY", "PARALLEL")


# =============================== TIỆN ÍCH =============================== #
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or now_utc()).isoformat(timespec="milliseconds")


def parse_time(s) -> datetime | None:
    if not s:
        return None
    try:
        t = str(s).strip().replace("Z", "+00:00")
        d = datetime.fromisoformat(t)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def ms(ns_a, ns_b):
    if ns_a is None or ns_b is None:
        return None
    return round((ns_b - ns_a) / 1e6, 3)


def redact(s: str) -> str:
    """Bỏ query (chữ ký, key) khỏi URL trước khi log."""
    s = str(s or "")
    s = re.sub(r"(key=)[^&\s]+", r"\1***", s)
    return re.sub(r"(https?://[^\s?]+)\?[^\s]*", r"\1?…", s)


def video_url(vid: str) -> str:
    return f"https://www.youtube.com/watch?v={vid}"


def extract_vid(v) -> str | None:
    """Nhận ID hoặc mọi dạng link: watch?v=, /shorts/, youtu.be/, /live/, /embed/."""
    v = str(v or "").strip()
    if VID_RE.match(v):
        return v
    m = re.search(r"(?:v=|/shorts/|youtu\.be/|/live/|/embed/)([0-9A-Za-z_-]{11})", v)
    return m.group(1) if m else None


# ---------------- tên file theo tiêu đề ---------------- #
_WIN_FORBIDDEN = {'<': '＜', '>': '＞', ':': '：', '"': '＂', '/': '／', '\\': '＼', '|': '｜', '?': '？', '*': '＊'}


# file tạm của tool: .<ID>.part.mp4 | .<ID>.aN.part.v/.a | (bản cũ) .<ID>_360p.part.mp4 — KHÔNG BAO GIỜ trùng tên tiêu đề
TEMP_RE = re.compile(r"^\.[0-9A-Za-z_-]{11}(?:_360p)?(?:\.a\d+)?\.part(?:\.mp4|\.v|\.a)?$")
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_title(title, max_len=0) -> str:
    """GIỮ NGUYÊN tiêu đề gốc. Chỉ sửa đúng những gì Windows KHÔNG cho phép lưu (không sửa thì không tạo được file):
    • 9 ký tự cấm < > : " / \\ | ? * → bản full-width trông gần như giống hệt;
    • ký tự điều khiển (mã 0–31: xuống dòng, tab…) → khoảng trắng;
    • dấu chấm / khoảng trắng ở CUỐI (Windows tự xoá → tên file sẽ lệch với đường dẫn tool giữ);
    • tên thiết bị dành riêng (CON, NUL, COM1…) → thêm '_';
    • chỉ cắt bớt khi vượt giới hạn đường dẫn của Windows (max_len > 0).
    Emoji, chữ Nhật/Việt, nhiều khoảng trắng, dấu chấm đầu… giữ nguyên."""
    out = []
    for c in str(title or ""):
        if c in _WIN_FORBIDDEN:
            out.append(_WIN_FORBIDDEN[c])
        elif ord(c) < 32:
            out.append(" ")
        else:
            out.append(c)
    t = "".join(out)
    if max_len > 0 and len(t) > max_len:
        t = t[:max_len]
        if 0xD800 <= ord(t[-1]) <= 0xDBFF:            # không cắt đôi cặp surrogate
            t = t[:-1]
    t = t.rstrip(" .")
    if t.split(".")[0].strip().upper() in _WIN_RESERVED:
        t += "_"
    return t


def _units(s: str) -> int:
    """Độ dài theo đơn vị UTF-16 (cách Windows đếm giới hạn đường dẫn: emoji = 2)."""
    return len(s.encode("utf-16-le")) // 2


def build_filename(title, vid, folder: Path, mode="TITLE", title_max=0, max_path=240, n=1) -> str:
    """TITLE (mặc định): '<tiêu đề gốc>.mp4' | TITLE_ID: '<tiêu đề>_<ID>.mp4' | ID_360P: '<ID>_360p.mp4'.
    Không có tiêu đề → '<ID>_360p.mp4'. Video ID luôn là định danh nội bộ (SQLite giữ ID ↔ đường dẫn file)."""
    mode = str(mode or "TITLE").upper()
    suffix = f" ({n})" if n > 1 else ""
    if mode not in ("TITLE", "TITLE_ID"):
        return f"{vid}_360p{suffix.replace(' ', '')}.mp4"
    tail = f"{suffix}_{vid}.mp4" if mode == "TITLE_ID" else f"{suffix}.mp4"
    budget = int(max_path) - _units(str(folder)) - 1 - len(tail)
    budget = min(budget, 250 - len(tail))                  # 1 tên file trên NTFS tối đa 255 đơn vị
    if title_max and int(title_max) > 0:
        budget = min(budget, int(title_max))
    t = safe_title(title, 0)
    if _units(t) > budget:                                 # chỉ cắt khi Windows bắt buộc
        while t and _units(t) > budget:
            t = t[:-1]
        t = safe_title(t, 0)
    if not t:
        return f"{vid}_360p{suffix.replace(' ', '')}.mp4"
    return f"{t}{tail}"


def port_free(host, port) -> bool:
    import socket
    s_ = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s_.bind((host if host not in ("", "0.0.0.0") else "0.0.0.0", int(port)))
        return True
    except OSError:
        return False
    finally:
        s_.close()


def norm_channel(v) -> str | None:
    m = CH_RE.search(str(v or ""))
    return m.group(1) if m else None


def safe_parse_xml(raw: bytes):
    if b"<!DOCTYPE" in raw[:4096].upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("xml có DOCTYPE/ENTITY — từ chối")
    return ET.fromstring(raw)


def parse_atom(raw: bytes) -> list[dict]:
    """Atom của WebSub push → [{video_id, channel_id, title, published, updated, deleted}]."""
    root = safe_parse_xml(raw)
    out = []
    for e in root.iter(f"{ATOM}entry"):
        vid = (e.findtext(f"{YT}videoId") or "").strip()
        ch = (e.findtext(f"{YT}channelId") or "").strip()
        if not ch:
            a = e.find(f"{ATOM}author/{ATOM}uri")
            ch = norm_channel(a.text if a is not None else "") or ""
        out.append({"video_id": vid, "channel_id": ch, "title": (e.findtext(f"{ATOM}title") or "").strip(),
                    "published": parse_time(e.findtext(f"{ATOM}published")),
                    "updated": parse_time(e.findtext(f"{ATOM}updated")), "deleted": False})
    for d in root.iter(f"{AT}deleted-entry"):
        ref = d.get("ref") or ""
        m = re.search(r"video:([0-9A-Za-z_-]{11})", ref)
        out.append({"video_id": m.group(1) if m else "", "channel_id": "", "title": "", "published": None,
                    "updated": parse_time(d.get("when")), "deleted": True})
    return out


def pct(vals, p):
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    k = (len(v) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(v) - 1)
    return round(v[f] + (v[c] - v[f]) * (k - f), 3)


def find_tool(name):
    """Chỉ nhận FILE chạy được (không nhận nhầm THƯ MỤC tên 'ffmpeg'). Thứ tự: cạnh tool, bin/, ffmpeg/bin/, ffmpeg/, PATH."""
    exe = f"{name}.exe" if os.name == "nt" else name
    for cand in (BASE_DIR / exe, BASE_DIR / "bin" / exe, BASE_DIR / "ffmpeg" / "bin" / exe, BASE_DIR / "ffmpeg" / exe,
                 BASE_DIR / name):
        if cand.is_file():
            return str(cand)
    w = shutil.which(name)
    return w if w and os.path.isfile(w) else None


FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")
ARIA2C = find_tool("aria2c")


def norm_mode(v) -> str:
    v = str(v or "").strip().upper().replace(" ", "_").replace("+", "_")
    if v in MODES:
        return v
    if "API" in v and "WEBSUB" in v or v in ("RACE", "BOTH", "PARALLEL_RACE"):
        return "PARALLEL"
    if v.startswith("API"):
        return "API_ONLY"
    if v.startswith("WEBSUB"):
        return "WEBSUB_ONLY"
    return "PARALLEL"


def load_config(path: Path) -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if path.exists():
        try:
            cfg.update(json.loads(path.read_text(encoding="utf-8-sig")))
        except Exception as e:
            print(f"⚠️ config.json lỗi ({e}) — dùng mặc định")
    return cfg


# =============================== LOG =============================== #
class Log:
    def __init__(self, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / f"ws360_{datetime.now():%Y%m%d}.log"
        self.lock = threading.Lock()
        self.sinks = []
        try:
            self.fh = open(self.path, "a", encoding="utf-8", buffering=1)
        except OSError:
            self.fh = None

    def __call__(self, msg: str):
        line = f"[{datetime.now():%H:%M:%S.%f}"[:-3] + f"] {msg}"
        with self.lock:
            try:
                if self.fh:
                    self.fh.write(line + "\n")
            except (OSError, ValueError):
                pass
        for s in list(self.sinks):
            try:
                s(line)
            except Exception:
                pass
        if not self.sinks:
            try:
                print(line, flush=True)
            except Exception:
                pass


# =============================== SQLITE =============================== #
class _TimedLock:
    """RLock có đo THỜI GIAN CHỜ và ghi lại LUỒNG đang giữ — để biết chắc SQLite có làm chậm đường nóng hay không."""

    def __init__(self):
        self._l = threading.RLock()
        self.holder = None
        self.since = 0
        self.depth = 0
        self.last_wait_ms = 0.0
        self.last_holder_seen = None

    def __enter__(self):
        t0 = time.perf_counter_ns()
        h = self.holder
        self._l.acquire()
        w = (time.perf_counter_ns() - t0) / 1e6
        self.depth += 1
        if self.depth == 1:
            self.holder = threading.current_thread().name
            self.since = time.perf_counter_ns()
        self.last_wait_ms = w
        self.last_holder_seen = h if w > 1 else None
        return self

    def __exit__(self, *a):
        self.depth -= 1
        if self.depth == 0:
            self.holder = None
        self._l.release()
        return False


class DB:
    SCHEMA = """
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS videos(
      video_id TEXT PRIMARY KEY, channel_id TEXT, url TEXT, title TEXT,
      engine TEXT, detected_at TEXT, published_at TEXT, state TEXT, attempts INTEGER DEFAULT 0,
      next_try REAL DEFAULT 0, error TEXT, file TEXT, size INTEGER,
      t_event_ms REAL, enqueue_ms REAL, resolver_start_ms REAL, resolver_ms REAL, ttfb_ms REAL,
      download_ms REAL, mux_ms REAL, verify_ms REAL, total_ms REAL, kind TEXT, itags TEXT,
      download_started_at TEXT, done_at TEXT, mbps REAL);
    CREATE TABLE IF NOT EXISTS excel_jobs(
      id INTEGER PRIMARY KEY AUTOINCREMENT, video_id TEXT, channel_id TEXT, url TEXT, state TEXT,
      attempts INTEGER DEFAULT 0, next_try REAL DEFAULT 0, error TEXT, created_at TEXT, done_at TEXT,
      write_ms REAL, rows TEXT, order_key TEXT, UNIQUE(video_id, channel_id));
    CREATE TABLE IF NOT EXISTS seen(video_id TEXT PRIMARY KEY, channel_id TEXT, why TEXT, at TEXT);
    CREATE TABLE IF NOT EXISTS subs(channel_id TEXT PRIMARY KEY, callback TEXT, requested_at REAL,
      verified_at REAL, lease_seconds INTEGER, state TEXT, last_push REAL, error TEXT);
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS hub_events(id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, channel_id TEXT, kind TEXT,
      mode TEXT, http INTEGER, accepted INTEGER, detail TEXT);
    CREATE TABLE IF NOT EXISTS detections(video_id TEXT, source TEXT, channel_id TEXT, t_wall REAL, detected_at TEXT,
      winner INTEGER, lag_ms REAL, published_at TEXT, title TEXT, api_interval_s REAL, baseline INTEGER DEFAULT 0,
      mode TEXT, PRIMARY KEY(video_id, source));
    CREATE TABLE IF NOT EXISTS api_quota(project TEXT, day TEXT, used INTEGER, PRIMARY KEY(project, day));
    """

    def __init__(self, path: Path):
        self.path = path
        self.lock = _TimedLock()
        self.con = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=30)
        self.con.row_factory = sqlite3.Row
        with self.lock:
            self.con.executescript(self.SCHEMA)
            self.con.execute("PRAGMA synchronous=NORMAL")
            for col in ("db_verifying_ms", "db_lock_wait_ms", "pre_verify_gap_ms", "ffprobe_ms", "rename_ms",
                        "post_verify_gap_ms", "sum_check_ms", "expected_size", "db_resolving_ms", "db_downloading_ms",
                        "tiktok", "t_done_wall"):
                try:
                    self.con.execute(f"ALTER TABLE videos ADD COLUMN {col} REAL")
                except sqlite3.OperationalError:
                    pass
            for alter in ("ALTER TABLE excel_jobs ADD COLUMN order_key TEXT",     # nâng cấp DB cũ
                          "ALTER TABLE videos ADD COLUMN target_file TEXT",       # tên file cuối đã chọn (giữ qua khởi động lại)
                          "ALTER TABLE videos ADD COLUMN net TEXT",               # JSON: DNS/TCP/TLS/SERVER_WAIT resolver + tải
                          "ALTER TABLE subs ADD COLUMN attempts INTEGER DEFAULT 0",
                          "ALTER TABLE subs ADD COLUMN last_http INTEGER",
                          "ALTER TABLE subs ADD COLUMN secret_fp TEXT",          # secret gửi trong lần subscribe gần nhất
                          "ALTER TABLE subs ADD COLUMN verified_secret_fp TEXT"):  # secret của thuê bao đã được hub xác minh
                try:
                    self.con.execute(alter)
                except sqlite3.OperationalError:
                    pass

    def tx(self, stmts):
        """Nhiều câu lệnh trong MỘT giao dịch (không bao giờ ghi nửa chừng)."""
        with self.lock:
            self.con.execute("BEGIN IMMEDIATE")
            try:
                for sql, args in stmts:
                    self.con.execute(sql, args)
                self.con.execute("COMMIT")
            except Exception:
                self.con.execute("ROLLBACK")
                raise

    def x(self, sql, args=()):
        with self.lock:
            return self.con.execute(sql, args)

    def one(self, sql, args=()):
        with self.lock:
            return self.con.execute(sql, args).fetchone()

    def all(self, sql, args=()):
        with self.lock:
            return self.con.execute(sql, args).fetchall()

    def meta_get(self, k, default=None):
        r = self.one("SELECT v FROM meta WHERE k=?", (k,))
        return r["v"] if r else default

    def meta_set(self, k, v):
        self.x("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))

    def upd(self, vid, **kw):
        """Trả (tổng ms, ms chờ khoá, luồng đang giữ khoá khi phải chờ) để đo đường nóng."""
        if not kw:
            return (0.0, 0.0, None)
        cols = ", ".join(f"{k}=?" for k in kw)
        t0 = time.perf_counter_ns()
        with self.lock as lk:
            wait, holder = lk.last_wait_ms, lk.last_holder_seen
            self.con.execute(f"UPDATE videos SET {cols} WHERE video_id=?", (*kw.values(), vid))
        return ((time.perf_counter_ns() - t0) / 1e6, wait, holder)


# =============================== EXCEL =============================== #
class ExcelLocked(Exception):
    pass


class ExcelBook:
    """Đọc Channel ID cột K, ghi URL cột D vào ĐÚNG các dòng có Channel ID đó (tìm lại dòng ngay lúc ghi).
    Excel đang mở trên Windows → ghi qua COM (pywin32) vào chính workbook đang mở; không có COM → ExcelLocked (retry).
    Excel không mở → openpyxl ghi file tạm rồi os.replace (atomic). Không bao giờ dùng 2 cơ chế cho cùng 1 lần ghi."""

    def __init__(self, path: Path, col_channel="K", col_url="D", sheet=""):
        self.path = Path(path)
        self.ck = column_index_from_string(col_channel.upper()) if openpyxl else 11
        self.cu = column_index_from_string(col_url.upper()) if openpyxl else 4
        self.sheet = sheet
        self.own_sig = None

    def sig(self):
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def lock_file(self) -> Path:
        return self.path.with_name("~$" + self.path.name)

    def is_open_in_excel(self) -> bool:
        """~$file tồn tại VÀ file đang bị giữ (Excel mở file với chế độ cấm ghi chung).
        ~$file sót lại sau khi Excel crash (file không bị giữ) → coi như KHÔNG mở."""
        if not self.lock_file().exists():
            return False
        if os.name != "nt":
            return True                                    # ngoài Windows: tin file khoá (dùng cho test)
        try:
            fd = os.open(str(self.path), os.O_RDWR | getattr(os, "O_BINARY", 0))
            os.close(fd)
            return False
        except PermissionError:
            return True
        except OSError:
            return True

    def _ws(self, wb):
        return wb[self.sheet] if self.sheet and self.sheet in wb.sheetnames else wb.worksheets[0]

    def read_channels(self) -> tuple[dict, list]:
        """→ ({channel_id: [row,...]}, [(row, giá_trị_sai)])"""
        wb = openpyxl.load_workbook(self.path, read_only=True, data_only=True)
        try:
            ws = self._ws(wb)
            out, bad = {}, []
            for r_idx, row in enumerate(ws.iter_rows(min_col=self.ck, max_col=self.ck, values_only=True), start=1):
                v = row[0] if row else None
                if v is None or str(v).strip() == "":
                    continue
                ch = norm_channel(v)
                if ch:
                    out.setdefault(ch, []).append(r_idx)
                elif r_idx > 1 or not re.search(r"channel|kênh|id", str(v), re.I):   # dòng tiêu đề thì bỏ qua
                    bad.append((r_idx, str(v)[:60]))
            return out, bad
        finally:
            wb.close()

    def write_url(self, channel_id: str, url: str) -> list[int]:
        return self.write_urls({channel_id: url}).get(channel_id, [])

    def write_urls(self, mapping: dict) -> dict:
        """{channel_id: url} → {channel_id: [dòng đã ghi]} trong MỘT lần mở/lưu (gom nhiều video cùng lúc)."""
        if self.is_open_in_excel():
            return self._write_com(mapping)
        return self._write_openpyxl(mapping)

    def _write_openpyxl(self, mapping):
        if self.path.suffix.lower() == ".xlsb":
            raise ExcelLocked("file .xlsb không ghi được bằng openpyxl — mở bằng Excel để tool ghi qua COM")
        tmp = self.path.with_name(f".{self.path.stem}.ws360-{os.getpid()}.tmp{self.path.suffix}")
        try:
            sig0 = self.sig()
            wb = openpyxl.load_workbook(self.path, keep_vba=self.path.suffix.lower() == ".xlsm")
            ws = self._ws(wb)
            out = {ch: [] for ch in mapping}
            for r in range(1, ws.max_row + 1):
                ch = norm_channel(ws.cell(r, self.ck).value)
                if ch in mapping:
                    out[ch].append(r)
            if not any(out.values()):
                return out
            for ch, rows in out.items():
                for r in rows:
                    ws.cell(r, self.cu).value = mapping[ch]
            wb.save(tmp)
            if self.is_open_in_excel():          # người dùng vừa mở Excel trong lúc ta ghi → không ghi đè
                raise ExcelLocked("Excel vừa được mở")
            if self.sig() != sig0:               # người dùng vừa lưu file trong lúc ta ghi → làm lại từ bản mới
                raise ExcelLocked("file vừa được lưu bởi người khác — ghi lại từ bản mới")
            os.replace(tmp, self.path)
            self.own_sig = self.sig()
            return out
        except PermissionError as e:
            raise ExcelLocked(str(e))
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    def _write_com(self, mapping):
        try:
            import pythoncom  # type: ignore
            import win32com.client  # type: ignore
        except ImportError:
            raise ExcelLocked("Excel đang mở và không có pywin32 để ghi qua COM — sẽ ghi khi Excel đóng")
        pythoncom.CoInitialize()
        try:
            wb = self._com_find_workbook(win32com.client)
            if wb is None:
                raise ExcelLocked("file không mở trong Excel của phiên này (có thể máy/người khác đang mở) — chờ")
            ws = wb.Worksheets(self.sheet) if self.sheet else wb.Worksheets(1)
            last = ws.UsedRange.Row + ws.UsedRange.Rows.Count - 1
            vals = ws.Range(ws.Cells(1, self.ck), ws.Cells(last, self.ck)).Value
            out = {ch: [] for ch in mapping}
            if not isinstance(vals, tuple):
                vals = ((vals,),)
            for i, v in enumerate(vals or (), start=1):
                cell = v[0] if isinstance(v, tuple) else v
                ch = norm_channel(cell)
                if ch in mapping:
                    out[ch].append(i)
            for ch, rows in out.items():
                for r in rows:
                    ws.Cells(r, self.cu).Value = mapping[ch]
            if any(out.values()):
                wb.Save()
            self.own_sig = self.sig()
            return out
        except ExcelLocked:
            raise
        except Exception as e:
            raise ExcelLocked(f"COM lỗi: {type(e).__name__}: {e}")
        finally:
            pythoncom.CoUninitialize()

    def _com_find_workbook(self, w32):
        """Tìm workbook ĐANG MỞ (không bao giờ tự mở file ẩn):
        1) qua ROT theo đường dẫn — chỉ chấp nhận nếu Excel đã có sẵn workbook đó (Workbooks.Count không tăng);
        2) quét Excel đang chạy, so FullName (chuẩn hoá: ổ mạng/UNC, OneDrive https) hoặc Name trùng duy nhất."""
        try:
            xl = w32.GetActiveObject("Excel.Application")
        except Exception:
            xl = None
        names = []
        if xl is not None:
            try:
                names = [(b, str(b.FullName), str(b.Name)) for b in xl.Workbooks]
            except Exception:
                names = []
        want_full = {str(self.path).lower(), str(self.path.resolve()).lower()}
        for b, full, _n in names:
            if full.lower() in want_full:
                return b
        if xl is not None:                                     # ROT: chỉ khi số workbook không đổi (không mở ẩn)
            try:
                before = xl.Workbooks.Count
                b = w32.GetObject(str(self.path.resolve()))
                if xl.Workbooks.Count == before:
                    return b
                try:
                    b.Close(SaveChanges=False)
                except Exception:
                    pass
            except Exception:
                pass
        same = [b for b, full, n in names if n.lower() == self.path.name.lower()]
        if len(same) == 1:                                     # OneDrive/UNC: FullName khác dạng nhưng tên trùng duy nhất
            return same[0]
        return None

    def read_cell(self, row, col_letter="D"):
        wb = openpyxl.load_workbook(self.path, read_only=True, data_only=True)
        try:
            return self._ws(wb).cell(row, column_index_from_string(col_letter)).value
        finally:
            wb.close()


# =============================== RESOLVER =============================== #
class NotReady(Exception):
    pass


class Fatal(Exception):
    pass


class HttpStatus(Exception):
    def __init__(self, code, retry_after=None):
        super().__init__(f"HTTP {code}")
        self.code = code
        self.retry_after = retry_after


_POOL = None
_POOL_LOCK = threading.Lock()
_NET_MODE = "fast"                  # fast: đo DNS/TCP/TLS/SERVER_WAIT + DNS cache (ws360_net) | legacy: PoolManager cũ y nguyên
NET_LEARN = None                    # Engine gắn: học máy chủ video vừa dùng để giữ kết nối ấm
try:
    sys.path.insert(0, str(Path(os.path.dirname(os.path.abspath(__file__)))))
    import ws360_net as NET
except Exception:  # pragma: no cover
    NET = None


class _NoTrace:
    d = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def net_trace():
    return NET.trace() if (NET is not None and _NET_MODE == "fast") else _NoTrace()


def set_net_mode(mode: str):
    """Đổi lớp mạng (rollback: 'legacy' = đúng PoolManager của bản trước). Đóng pool cũ."""
    global _POOL, _NET_MODE
    mode = "legacy" if str(mode).lower() == "legacy" or NET is None else "fast"
    with _POOL_LOCK:
        if _POOL is not None and mode != _NET_MODE:
            try:
                _POOL.clear()
            except Exception:
                pass
            _POOL = None
        _NET_MODE = mode
    return mode


def pool():
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                if _NET_MODE == "fast" and NET is not None:
                    _POOL = NET.make_pool("fast")
                else:
                    _POOL = urllib3.PoolManager(num_pools=32, maxsize=24, block=False, retries=False,
                                                timeout=urllib3.Timeout(connect=2.0, read=8.0))
    return _POOL


_CLIENT_CACHE = {}


def _innertube_client(name):
    if name in _CLIENT_CACHE:                     # cấu hình client yt-dlp: nạp 1 lần (trước đây import + deepcopy mỗi request)
        return _CLIENT_CACHE[name]
    _CLIENT_CACHE[name] = _innertube_client_load(name)
    return _CLIENT_CACHE[name]


def _innertube_client_load(name):
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


def _fmt_height(f):
    """'Độ phân giải p' = CẠNH NGẮN của khung hình. Shorts dọc 360p là 360x640 → trả 360 (không phải 640).
    Ưu tiên nhãn '360p' của YouTube; không có nhãn thì lấy min(width, height)."""
    m = re.match(r"(\d+)p", str(f.get("qualityLabel") or f.get("format_note") or ""))
    if m:
        return int(m.group(1))
    w, h = int(f.get("width") or 0), int(f.get("height") or 0)
    if w and h:
        return min(w, h)
    return h or w


def pick_formats(progressive: list, adaptive: list) -> dict | None:
    """Chọn 360p: progressive (hình+tiếng 1 file, ưu tiên MP4/H.264) → nếu không có: video 360p (ưu tiên avc1)
    + audio (ưu tiên AAC/m4a itag 140). Không giả định itag cố định. Mỗi format: dict có url, mime, itag, clen, height."""
    def is_mp4(f):
        return "mp4" in str(f.get("mime", ""))

    def is_avc(f):
        return "avc1" in str(f.get("mime", ""))

    prog = [f for f in progressive if f.get("url") and f.get("height") == 360 and f.get("has_audio", True)]
    if prog:
        prog.sort(key=lambda f: (not is_avc(f), not is_mp4(f), -(f.get("clen") or 0)))
        return {"kind": "progressive", "video": prog[0], "audio": None}
    vids = [f for f in adaptive if f.get("url") and f.get("height") == 360 and f.get("is_video")]
    auds = [f for f in adaptive if f.get("url") and f.get("is_audio")]
    if vids:
        vids.sort(key=lambda f: (not is_avc(f), not is_mp4(f), f.get("clen") or 1 << 40))
        aud = None
        if auds:
            auds.sort(key=lambda f: (str(f.get("itag")) != "140", "mp4a" not in str(f.get("mime", "")),
                                     not f.get("is_default_audio", True), -(f.get("bitrate") or 0)))
            aud = auds[0]
        return {"kind": "dash", "video": vids[0], "audio": aud}
    return None


def _innertube_formats(data: dict) -> tuple[list, list]:
    sd = data.get("streamingData") or {}

    def conv(f, adaptive):
        mime = str(f.get("mimeType") or "")
        tr = f.get("audioTrack") or {}
        return {"itag": f.get("itag"), "url": f.get("url"), "mime": mime, "height": _fmt_height(f),
                "clen": int(f.get("contentLength") or 0), "bitrate": int(f.get("bitrate") or 0),
                "is_video": mime.startswith("video/"), "is_audio": mime.startswith("audio/"),
                "has_audio": (not adaptive) or mime.startswith("audio/"),
                "is_default_audio": bool(tr.get("audioIsDefault", True)),
                "duration": float(int(f.get("approxDurationMs") or 0) / 1000.0)}
    return [conv(f, False) for f in sd.get("formats") or []], [conv(f, True) for f in sd.get("adaptiveFormats") or []]


def classify_playability(ps: dict, has_streams: bool) -> str:
    """'ok' | 'notready' | 'fatal' — video mới có thể tạm 'unavailable'/'processing' vài giây."""
    st = str(ps.get("status") or "").upper()
    es = ps.get("errorScreen") or {}
    sub = ""
    try:
        r0 = (es.get("playerErrorMessageRenderer") or {})
        sub = " ".join(str(x.get("text") or "") for k in ("reason", "subreason")
                       for x in ((r0.get(k) or {}).get("runs") or [])) + " " + str((r0.get("subreason") or {}).get("simpleText") or "")
    except Exception:
        sub = ""
    reason = (str(ps.get("reason") or "") + " " + sub).lower()
    if st == "OK":
        return "ok" if has_streams else "notready"
    if st in ("LIVE_STREAM_OFFLINE",) or "premiere" in reason or "processing" in reason or "upcoming" in reason:
        return "notready"
    if any(k in reason for k in ("members-only", "members only", "join this channel", "thành viên", "payment",
                                  "purchase", "copyright", "terminated", "has been removed", "đã bị xóa",
                                  "confirm your age", "age-restricted", "age restricted", "drm")):
        return "fatal"
    if st in ("ERROR", "UNPLAYABLE", "LOGIN_REQUIRED", "CONTENT_CHECK_REQUIRED"):
        return "notready"   # video vừa đăng hay tạm báo unavailable/private → thử lại có giới hạn
    return "notready"


def _net_phases(d: dict, t0=None, t1=None) -> dict:
    """Gom số đo 1 request: kết nối mới (DNS/TCP/TLS) hay dùng lại, SERVER_WAIT (gửi xong → header), đọc thân."""
    out = {"new_conn": bool(d.get("new_conn")), "dns_ms": d.get("dns_ms"), "tcp_ms": d.get("tcp_ms"), "tls_ms": d.get("tls_ms"),
           "server_wait_ms": d.get("server_wait_ms")}
    if t0 is not None and t1 is not None:
        out["total_ms"] = ms(t0, t1)
        if d.get("t_hdr"):
            out["body_ms"] = ms(d["t_hdr"], t1)
    return out


class Resolver:
    CLIENTS = ("android_vr", "web_embedded", "android")

    def __init__(self, log):
        self.log = log
        self.host_override = ""                   # "" = máy chủ Innertube của yt-dlp | vd "youtubei.googleapis.com"
        from concurrent.futures import ThreadPoolExecutor
        self.ex = ThreadPoolExecutor(max_workers=8, thread_name_prefix="Resolve")
        self._ydl = None
        self._ydl_lock = threading.Lock()

    def warm(self):
        try:
            for c in self.CLIENTS:
                _innertube_client(c)
            if urllib3:
                pool().connection_from_url("https://www.youtube.com")
        except Exception:
            pass

    def _player(self, vid, client):
        cfg = _innertube_client(client)
        if not cfg:
            return None
        ctx = copy.deepcopy(cfg.get("INNERTUBE_CONTEXT") or {})
        c = ctx.setdefault("client", {})
        c.setdefault("hl", "en")
        c.setdefault("timeZone", "UTC")
        c.setdefault("utcOffsetMinutes", 0)
        host = self.host_override or str(cfg.get("INNERTUBE_HOST") or "www.youtube.com")
        body = json.dumps({"context": ctx, "videoId": vid, "contentCheckOk": True, "racyCheckOk": True,
                           "playbackContext": {"contentPlaybackContext": {"html5Preference": "HTML5_PREF_WANTS"}}},
                          separators=(",", ":")).encode()
        hdr = {"Content-Type": "application/json", "Accept-Encoding": "gzip", "Origin": "https://www.youtube.com",
               "User-Agent": str(c.get("userAgent") or "com.google.android.youtube/20.10.38 (Linux; U; Android 11) gzip"),
               "X-YouTube-Client-Name": str(cfg.get("INNERTUBE_CONTEXT_CLIENT_NAME") or 3),
               "X-YouTube-Client-Version": str(c.get("clientVersion") or "")}
        t0 = time.perf_counter_ns()
        with net_trace() as tr:
            r = pool().request("POST", f"https://{host}/youtubei/v1/player?prettyPrint=false", body=body, headers=hdr,
                               timeout=urllib3.Timeout(connect=1.0, read=2.0), retries=False, preload_content=True)
        t1 = time.perf_counter_ns()
        net = _net_phases(tr.d, t0, t1)
        net["host"] = host
        if r.status != 200:
            return {"client": client, "verdict": "notready", "reason": f"player HTTP {r.status}", "net": net}
        data = json.loads(r.data)
        net["parse_ms"] = ms(t1, time.perf_counter_ns())
        net["kb"] = round(len(r.data) / 1024, 1)
        prog, adap = _innertube_formats(data)
        pick = pick_formats(prog, adap)
        verdict = classify_playability(data.get("playabilityStatus") or {}, bool(prog or adap))
        dur = float((data.get("videoDetails") or {}).get("lengthSeconds") or 0)
        sd = data.get("streamingData") or {}
        allf = list(sd.get("formats") or []) + list(sd.get("adaptiveFormats") or [])
        counts = {"prog": len(prog), "adap": len(adap), "cipher": sum(1 for f in allf if not f.get("url")),
                  "has18": any(str(f.get("itag")) == "18" for f in allf)}
        return {"client": client, "verdict": verdict if (pick or verdict != "ok") else "noformat", "pick": pick, "counts": counts,
                "reason": str((data.get("playabilityStatus") or {}).get("reason") or (data.get("playabilityStatus") or {}).get("status") or ""),
                "duration": dur, "title": (data.get("videoDetails") or {}).get("title") or "", "net": net}

    def resolve(self, vid: str, deadline_s=2.5) -> dict:
        """Race 3 client Innertube; client nào trả 360p dùng được trước thì thắng. Không có → yt-dlp."""
        t0 = time.perf_counter_ns()
        futs = [self.ex.submit(self._player, vid, c) for c in self.CLIENTS] if urllib3 else []
        results = []
        from concurrent.futures import as_completed, TimeoutError as FTE
        try:
            for f in as_completed(futs, timeout=deadline_s):
                try:
                    r = f.result()
                except Exception as e:
                    r = {"verdict": "error", "reason": f"{type(e).__name__}"}
                if r:
                    r["done_ms"] = ms(t0, time.perf_counter_ns())
                    results.append(r)
                    if r.get("pick"):
                        r["source"] = f"INNERTUBE_{r['client'].upper()}"
                        r["resolve_ms"] = r["done_ms"]
                        r["race_ms"] = {x.get("client"): x.get("done_ms") for x in results}
                        return r
        except FTE:
            pass
        race = "; ".join(f"{x.get('client', '?')}={x.get('verdict')}"
                         + (f"(prog {x['counts']['prog']}, adap {x['counts']['adap']}, cipher {x['counts']['cipher']}, "
                            f"itag18 {'có' if x['counts']['has18'] else 'không'})" if x.get("counts") else "")
                         + (f" '{str(x.get('reason'))[:60]}'" if x.get("reason") and x.get("verdict") != "noformat" else "")
                         for x in results) or "không client nào trả lời kịp"
        r = self._ytdlp(vid)
        if r and r.get("pick"):
            r["resolve_ms"] = ms(t0, time.perf_counter_ns())
            r["race"] = race                      # vì sao Innertube nhanh không dùng được → phải dùng yt-dlp
            return r
        verdicts = [x.get("verdict") for x in results + ([r] if r else [])]
        reasons = "; ".join(sorted({str(x.get("reason") or "")[:80] for x in results + ([r] if r else []) if x.get("reason")}))
        if verdicts and all(v == "fatal" for v in verdicts if v not in ("error",)) and "fatal" in verdicts:
            raise Fatal(reasons or "không tải được")
        raise NotReady(reasons or "chưa có luồng 360p")

    def _ytdlp(self, vid):
        if not yt_dlp:
            return None
        with self._ydl_lock:
            if self._ydl is None:
                self._ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                                              "noplaylist": True, "socket_timeout": 8, "cachedir": False})
            ydl = self._ydl
        try:
            info = ydl.extract_info(video_url(vid), download=False, process=True)
        except Exception as e:
            msg = str(e).lower()
            fatal = any(k in msg for k in ("members", "private video", "removed", "terminated", "copyright", "sign in to confirm your age"))
            return {"verdict": "fatal" if fatal and "private" not in msg else "notready", "reason": str(e)[:160]}
        prog, adap = [], []
        for f in info.get("formats") or []:
            if not f.get("url") or str(f.get("protocol", "https")).startswith(("m3u8", "http_dash")):
                continue
            vc, ac = str(f.get("vcodec") or "none"), str(f.get("acodec") or "none")
            ext = str(f.get("ext") or "")
            d = {"itag": f.get("format_id"), "url": f["url"], "height": _fmt_height(f),
                 "mime": f"{'video' if vc != 'none' else 'audio'}/{ext}; codecs=\"{vc if vc != 'none' else ac}\"",
                 "clen": int(f.get("filesize") or 0), "bitrate": int((f.get("tbr") or 0) * 1000),
                 "is_video": vc != "none" and ac == "none", "is_audio": vc == "none" and ac != "none",
                 "has_audio": ac != "none", "is_default_audio": (f.get("language_preference") or 0) >= 0}
            if vc != "none" and ac != "none":
                prog.append(d)
            else:
                adap.append(d)
        pick = pick_formats(prog, adap)
        return {"verdict": "ok" if pick else "noformat", "pick": pick, "source": "YTDLP", "reason": "",
                "duration": float(info.get("duration") or 0), "title": info.get("title") or ""}


# =============================== HTTP DOWNLOAD =============================== #
def _follow(url, headers=None, method="GET", preload=False, timeout=None):
    """GET theo 302 thủ công (tối đa 5), trả (resp, final_url). 4xx/5xx → HttpStatus."""
    cur = url
    hops = []
    for _ in range(6):
        t0 = time.perf_counter_ns()
        with net_trace() as tr:
            r = pool().request(method, cur, headers=headers or {}, redirect=False, preload_content=preload,
                               retries=False, timeout=timeout or urllib3.Timeout(connect=2.0, read=8.0))
        hop = _net_phases(tr.d, t0, time.perf_counter_ns())
        hop["host"] = urlparse(cur).hostname
        hop["status"] = r.status
        hops.append(hop)
        r._ws_hops = hops
        if r.status in (301, 302, 303, 307, 308):
            loc = r.headers.get("Location")
            r.drain_conn()
            r.release_conn()
            if not loc:
                raise HttpStatus(r.status)
            cur = urljoin(cur, loc)
            continue
        if r.status >= 400:
            ra = r.headers.get("Retry-After")
            r.close()
            raise HttpStatus(r.status, float(ra) if ra and ra.isdigit() else None)
        return r, cur
    raise HttpStatus(310)


def probe_size(url: str) -> int:
    """Dung lượng thật qua Range bytes=0-0 → Content-Range: bytes 0-0/TỔNG (theo 302)."""
    r, _ = _follow(url, headers={"Range": "bytes=0-0"}, preload=False)
    cr = r.headers.get("Content-Range", "")
    st = r.status
    if st == 206:
        r.drain_conn()
        r.release_conn()
    else:
        r.close()
    m = re.search(r"/(\d+)$", cr)
    if st != 206 or not m:
        raise HttpStatus(st)
    return int(m.group(1))


def download_single(url: str, dest: Path, info: dict, cancel: threading.Event | None = None) -> int:
    """1 GET, ghi thẳng vào file tạm. info nhận ttfb_ms/bytes/elapsed_ms."""
    t0 = time.perf_counter_ns()
    r, final = _follow(url)
    t_hdr = time.perf_counter_ns()
    total = 0
    first = None
    expected = int(r.headers.get("Content-Length") or 0)
    try:
        with open(dest, "wb", buffering=1024 * 1024) as f:
            for chunk in r.stream(256 * 1024):
                if first is None:
                    first = time.perf_counter_ns()
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("dừng")
                f.write(chunk)
                total += len(chunk)
    finally:
        r.release_conn()
    if expected and total != expected:
        raise IOError(f"thiếu dữ liệu {total}/{expected}")
    t_end = time.perf_counter_ns()
    info.update(ttfb_ms=ms(t0, first or t_end), bytes=total, elapsed_ms=ms(t0, t_end), redirected=final != url)
    hops = getattr(r, "_ws_hops", []) or []
    sm = lambda k: round(sum(h.get(k) or 0 for h in hops), 2) if any(h.get(k) is not None for h in hops) else None
    # TTFB = setup (DNS+TCP+TLS mọi chặng) + SERVER_WAIT (mọi chặng) + phần còn lại; TRANSFER = byte đầu → byte cuối (không tính TTFB 2 lần)
    info.update(hops=len(hops), new_conns=sum(1 for h in hops if h.get("new_conn")), dns_ms=sm("dns_ms"), tcp_ms=sm("tcp_ms"),
                tls_ms=sm("tls_ms"), server_wait_ms=sm("server_wait_ms"), headers_ms=ms(t0, t_hdr),
                first_byte_after_hdr_ms=ms(t_hdr, first) if first else None, transfer_ms=ms(first, t_end) if first else None,
                host=urlparse(final).hostname)
    if NET_LEARN is not None:
        try:
            NET_LEARN(final)
        except Exception:
            pass
    return total


def download_ranges(url: str, dest: Path, size: int, n: int, info: dict, cancel=None) -> int:
    """N kết nối Range song song (chỉ khi biết size). Kiểm Content-Range từng đoạn; lỗi → ném ra (caller về 1 GET)."""
    from concurrent.futures import ThreadPoolExecutor
    t0 = time.perf_counter_ns()
    if size <= 0 or n <= 1:
        return download_single(url, dest, info, cancel)
    # KHÔNG còn request dò 'bytes=0-0' (tốn thêm 1 vòng mạng + chờ máy chủ): mỗi đoạn tự theo 302 và BẮT BUỘC 206 +
    # Content-Range khớp; máy chủ bỏ qua Range (200) → đóng ngay không đọc, ném lỗi → caller về 1 GET.
    with open(dest, "wb") as f:
        f.truncate(size)
    step = -(-size // n)
    parts = [(i * step, min(size, (i + 1) * step) - 1) for i in range(n) if i * step < size]
    firsts = []
    hops_all = []
    finals = []

    def one(a, b):
        rr, fin = _follow(url, headers={"Range": f"bytes={a}-{b}"}, preload=False)
        hops_all.extend(getattr(rr, "_ws_hops", []) or [])
        finals.append(fin)
        if rr.status != 206:
            rr.close()
            raise HttpStatus(rr.status)
        try:
            cr = rr.headers.get("Content-Range", "")
            if not cr.startswith(f"bytes {a}-{b}/"):
                raise IOError(f"Content-Range sai: {cr}")
            got = 0
            with open(dest, "r+b") as f:
                f.seek(a)
                for chunk in rr.stream(256 * 1024):
                    if not got:
                        firsts.append(time.perf_counter_ns())
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError("dừng")
                    f.write(chunk)
                    got += len(chunk)
            if got != b - a + 1:
                raise IOError(f"đoạn {a}-{b} thiếu {got}")
            return got
        finally:
            rr.release_conn()
    with ThreadPoolExecutor(max_workers=len(parts), thread_name_prefix="Range") as ex:
        total = sum(f.result() for f in [ex.submit(one, a, b) for a, b in parts])
    t_end = time.perf_counter_ns()
    sm = lambda k: round(max((h.get(k) or 0) for h in hops_all), 2) if any(h.get(k) is not None for h in hops_all) else None
    info.update(ttfb_ms=ms(t0, min(firsts) if firsts else None), bytes=total, elapsed_ms=ms(t0, t_end), connections=len(parts),
                hops=len(hops_all), new_conns=sum(1 for h in hops_all if h.get("new_conn")), dns_ms=sm("dns_ms"), tcp_ms=sm("tcp_ms"),
                tls_ms=sm("tls_ms"), server_wait_ms=sm("server_wait_ms"), transfer_ms=ms(min(firsts), t_end) if firsts else None,
                host=urlparse(finals[0]).hostname if finals else None)
    if NET_LEARN is not None and finals:
        try:
            NET_LEARN(finals[0])
        except Exception:
            pass
    return total


def download_aria2(url: str, dest: Path, n: int, info: dict) -> int:
    if not ARIA2C:
        raise RuntimeError("không có aria2c")
    t0 = time.perf_counter_ns()
    cmd = [ARIA2C, "-q", "-x", str(n), "-s", str(n), "-k", "1M", "--allow-overwrite=true", "--auto-file-renaming=false",
           "--file-allocation=none", "-d", str(dest.parent), "-o", dest.name, url]
    p = subprocess.run(cmd, capture_output=True, timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if p.returncode != 0 or not dest.exists():
        raise IOError(f"aria2c rc={p.returncode}")
    total = dest.stat().st_size
    info.update(bytes=total, elapsed_ms=ms(t0, time.perf_counter_ns()), connections=n)
    return total


def ffprobe_info(path: Path) -> dict:
    if not FFPROBE:
        return {"ok": path.exists() and path.stat().st_size > 0, "reason": "no_ffprobe"}
    p = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,width,height",
                        "-of", "json", str(path)], capture_output=True, text=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        j = json.loads(p.stdout or "{}")
    except ValueError:
        j = {}
    st = j.get("streams") or []
    dur = float((j.get("format") or {}).get("duration") or 0)
    v = [s for s in st if s.get("codec_type") == "video"]
    a = [s for s in st if s.get("codec_type") == "audio"]
    return {"ok": p.returncode == 0 and dur > 0 and bool(v), "duration": dur, "video": bool(v), "audio": bool(a),
            "height": (v[0].get("height") if v else None), "width": (v[0].get("width") if v else None),
            "vcodec": (v[0].get("codec_name") if v else None),
            "acodec": (a[0].get("codec_name") if a else None)}


def _replace_retry(src: Path, dst: Path, budget_s=3.0):
    """os.replace có thử lại khi Windows báo đang bị giữ (Defender/indexer đang quét file vừa ghi)."""
    end = time.time() + budget_s
    while True:
        try:
            return os.replace(src, dst)
        except PermissionError:
            if time.time() > end:
                raise
            time.sleep(0.02)


class _AnyEvent:
    """is_set() đúng khi BẤT KỲ event nào được đặt (huỷ theo job hoặc dừng toàn tool)."""

    def __init__(self, *evs):
        self.evs = [e for e in evs if e is not None]

    def is_set(self):
        return any(e.is_set() for e in self.evs)


def ffmpeg_mux(vfile: Path, afile: Path | None, out: Path) -> float:
    """-c copy (không encode). afile=None → chỉ đóng gói lại video vào MP4 (không đổi tên webm thành .mp4)."""
    t0 = time.perf_counter_ns()
    args = ["-i", str(vfile)] + (["-i", str(afile), "-map", "0:v:0", "-map", "1:a:0"] if afile else ["-map", "0:v:0"])
    p = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args, "-c", "copy", "-f", "mp4", str(out)],
                       capture_output=True, text=True, timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if p.returncode != 0:
        raise IOError(f"ffmpeg mux lỗi: {(p.stderr or '')[-300:]}")
    return ms(t0, time.perf_counter_ns())


def secret_fp(sec: str) -> str:
    """Dấu vân tay để so sánh trong log — KHÔNG lộ secret."""
    return hashlib.sha256(str(sec or "").encode()).hexdigest()[:8]


def retire_secret(base: Path, old: str, current: str):
    """Ghi secret cũ (hub có thể vẫn giữ cho thuê bao cũ) vào websub_secret_history.txt để chẩn đoán + chuyển tiếp an toàn."""
    if not old or old == current or not re.fullmatch(r"[0-9a-f]{32,128}", old):
        return
    f = base / "websub_secret_history.txt"
    try:
        have = f.read_text(encoding="utf-8").split() if f.exists() else []
    except OSError:
        have = []
    if old not in have:
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(f"{old} {int(time.time())}\n")


def load_secret_history(base: Path) -> list[tuple[str, float]]:
    out = []
    try:
        for line in (base / "websub_secret_history.txt").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if parts and re.fullmatch(r"[0-9a-f]{32,128}", parts[0]):
                out.append((parts[0], float(parts[1]) if len(parts) > 1 else 0.0))
    except OSError:
        pass
    return out


def load_shared_secret(base: Path, db: "DB | None" = None) -> str:
    """MỘT secret HMAC cho cả thư mục (tool chính, SELFTEST, DIAG). Hub lưu thuê bao theo (callback, topic):
    nếu mỗi chế độ dùng secret khác nhau, lần đăng ký sau sẽ ghi đè secret và tool chính bỏ nhầm push thật."""
    f = base / "websub_secret.txt"
    try:
        v = f.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[0-9a-f]{32,128}", v):
            return v
    except OSError:
        pass
    v = None
    main_db = base / "database.sqlite"                     # ưu tiên secret CŨ của tool chính (hub đang giữ secret này)
    if main_db.exists():
        try:
            con = sqlite3.connect(f"file:{main_db}?mode=ro", uri=True, timeout=5)
            row = con.execute("SELECT v FROM meta WHERE k='websub_secret'").fetchone()
            con.close()
            v = row[0] if row else None
        except sqlite3.Error:
            v = None
    v = v or (db.meta_get("websub_secret") if db is not None else None) or secrets.token_hex(24)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(v, encoding="utf-8")
    os.replace(tmp, f)
    return v


# =============================== ENGINE =============================== #
class Engine:
    def __init__(self, cfg: dict, base: Path, log: Log, on_stats=None, db_name="database.sqlite", lock_name="ws360.lock"):
        self.lock_name = lock_name
        self.cfg = cfg
        self.base = base
        self.log = log
        self.on_stats = on_stats
        self.db = DB(base / db_name)
        self.excel = ExcelBook(base / cfg["excel_file"], cfg["col_channel"], cfg["col_url"], cfg.get("excel_sheet", ""))
        self.stop_ev = threading.Event()
        self.channels: dict[str, list] = {}
        self.ch_lock = threading.RLock()
        self.seen_lock = threading.Lock()
        self.seen = set(r["video_id"] for r in self.db.all("SELECT video_id FROM seen")) | \
            set(r["video_id"] for r in self.db.all("SELECT video_id FROM videos"))
        self.dlq: queue.PriorityQueue = queue.PriorityQueue()
        self.xlq: queue.Queue = queue.Queue()
        self.seq = itertools.count()
        self.sem = threading.Semaphore(max(1, int(cfg.get("max_concurrent_downloads", 3))))
        self.active = set()
        self.active_lock = threading.Lock()
        self.life_lock = threading.Lock()
        self.pending_hub = {}                 # (channel, mode) → thời điểm yêu cầu ta THỰC SỰ gửi tới hub
        self.pending_ok = {}                  # (channel, mode) → thời điểm đã chấp nhận xác minh
        self.hub_lock = threading.Lock()
        self.net_mode = set_net_mode(cfg.get("net_mode", "fast"))
        self.resolver = Resolver(log)
        self.resolver.host_override = str(cfg.get("resolver_host") or "")
        self.warmer = None
        old_secret = self.db.meta_get("websub_secret")
        self.secret = load_shared_secret(base, self.db)
        retire_secret(base, old_secret, self.secret)     # DB từng dùng secret khác → giữ lại để nhận diện thuê bao cũ
        if old_secret and old_secret != self.secret and self.db.meta_get("subs_secret_sig") is None:
            # DB này từng đăng ký với secret cũ → buộc vòng WebSub coi là 'secret đổi' và đăng ký lại toàn bộ
            self.db.meta_set("subs_secret_sig", hashlib.sha256(old_secret.encode()).hexdigest()[:16])
        self.db.meta_set("websub_secret", self.secret)
        try:
            last_alive = float(self.db.meta_get("last_alive") or 0)
        except ValueError:
            last_alive = 0.0
        self.last_alive_at_start = last_alive or None   # thời điểm cuối cùng lần chạy trước còn sống
        self.last_request = {}
        self.probes = {}              # --hubtest: probe id → [(thời điểm, mode)] các GET xác minh nhận được
        self.started_at = time.time()
        self.old_logged = set()
        self.inbox_db = None          # SELFTEST: push THẬT nhận nhầm → chuyển vào database.sqlite của tool chính
        self.sim_vids = set()         # SELFTEST: video id của sự kiện giả lập
        self.sim_pending = set()      # SELFTEST: (kênh, mode) của các lần xác minh GIẢ LẬP (TEST2a/2b)
        self.on_done = None           # TÍCH HỢP TikTok: gọi NGAY khi file 360p hoàn chỉnh (DOWNLOAD_DONE)
        self.on_excel_reload = None   # TÍCH HỢP TikTok: Excel đổi → cập nhật ánh xạ Profile↔Kênh
        self.tiktok_on = False        # chỉ video phát hiện khi bật tích hợp mới được đăng (không đăng video cũ)
        self._badsig_resub = {}       # kênh → lần cuối đăng ký lại vì chữ ký sai
        self.mode = norm_mode(cfg.get("detection_mode", "PARALLEL"))
        self.race = {}                # video → {winner, ns{nguồn}, wall{nguồn}} (đo WebSub ↔ API trong cùng tiến trình)
        self.retry_gen = {}           # video → thế hệ lần thử lại hiện hành (tín hiệu nguồn thứ 2 → thử lại sớm)
        self.retry_jobs = {}
        self.nudged = set()           # (video, nguồn) đã kích thử lại sớm — mỗi nguồn tối đa 1 lần / video
        self.name_lock = threading.Lock()
        self.api = None               # ApiScanner khi chế độ có API
        self.websub_on = False
        self.threads = []
        self.httpd = None
        self.callback = ""
        self.stats = {"detected": 0, "downloading": 0, "done": 0, "failed": 0, "excel_ok": 0, "excel_pending": 0,
                      "excel_err": 0, "websub_verified": 0, "webhook": "OFF",
                      "last": {}, "speed_mbps": 0.0, "pushes": 0, "wins_websub": 0, "wins_api": 0, "mode": self.mode}
        self.flags = {"download": bool(cfg.get("download_enabled", True)), "excel": bool(cfg.get("excel_enabled", True)),
}
        self.dl_dir = base / cfg.get("download_dir", "DOWNLOADS")
        self.dl_dir.mkdir(parents=True, exist_ok=True)

    # ---------------- vòng đời ---------------- #
    def start(self, websub=True, workers=True, excel_watch=True, api=None):
        """websub/api theo detection_mode. api=None: bật API chỉ khi là lần chạy ĐẦY ĐỦ (websub=True) — SELFTEST/DIAG/HUBTEST
        (websub=False) không bao giờ tiêu quota API."""
        api = websub if api is None else api
        with self.life_lock:                              # start/stop không bao giờ chạy chồng nhau
            self._acquire_instance_lock()
            self.stop_ev.clear()
            try:
                self._start_inner(websub and self.mode != "API_ONLY", workers, excel_watch,
                                  api and self.mode in ("API_ONLY", "PARALLEL"))
            except Exception:
                self.stop_ev.set()
                self._stop_inner()
                raise

    def _start_inner(self, websub, workers, excel_watch, api=False):
        self.websub_on = bool(websub)
        self.reload_channels(initial=True)
        self.recover()
        self.resolver.warm()
        if workers:
            for i in range(max(1, int(self.cfg.get("workers", 3)))):
                self._spawn(self._dl_worker, f"DL-{i + 1}")
            self._spawn(self._excel_worker, "EXCEL")
        if excel_watch:
            self._spawn(self._excel_watch, "EXCEL-WATCH")
        if websub:
            self.start_webhook()
            self._spawn(self._websub_subscriber, "WEBSUB-SUB")
        if api:
            sys.path.insert(0, str(BASE_DIR))
            from ws360_api_scanner import ApiScanner
            self.api = ApiScanner(self, self.cfg, self.log, self.base)
            if not self.api.start():
                self.api = None
        self.log(f"🏁 Chế độ phát hiện: {self.mode} | WebSub {'BẬT' if websub else 'TẮT'} | API {'BẬT' if self.api else 'TẮT'}"
                 + (" (thiếu key → thực tế chỉ còn WebSub)" if self.mode == "PARALLEL" and api and not self.api else "")
                 + (" (thiếu key → KHÔNG có nguồn phát hiện nào!)" if self.mode == "API_ONLY" and api and not self.api else ""))
        self._spawn(self._stats_loop, "STATS")
        if workers and FFPROBE:
            self._spawn(self._ff_keepwarm, "FF-WARM")
        if workers and self.net_mode == "fast" and self.cfg.get("net_warm", True) and NET is not None:
            global NET_LEARN
            hosts = [self.resolver.host_override or "www.youtube.com"]
            self.warmer = NET.NetWarmer(pool, self.log, hosts, int(self.cfg.get("net_warm_innertube_conns", 3)),
                                        int(self.cfg.get("net_warm_media_hosts", 6)), float(self.cfg.get("net_warm_interval_sec", 40)))
            NET_LEARN = self.warmer.learn
            self.warmer.start()
        self.log(f"🔑 secret HMAC hiện tại: dấu vân tay {secret_fp(self.secret)} (websub_secret.txt) | "
                 f"secret cũ đã biết: {len(load_secret_history(self.base))}")
        self.log(f"✅ {VERSION} chạy | {len(self.channels)} kênh | workers={self.cfg.get('workers')} | "
                 f"ffmpeg={'có' if FFMPEG else 'KHÔNG'} | ffprobe={'có' if FFPROBE else 'KHÔNG'} | yt-dlp={'có' if yt_dlp else 'KHÔNG'}")

    def _acquire_instance_lock(self):
        self._lockfh = open(self.base / self.lock_name, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self._lockfh.seek(0)
                msvcrt.locking(self._lockfh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lockfh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lockfh.close()
            self._lockfh = None
            raise RuntimeError("Tool đang chạy ở cửa sổ khác trong cùng thư mục (START.bat?) — chỉ được chạy 1 bản. "
                               "Tắt cửa sổ đó rồi chạy lại.")

    def _release_instance_lock(self):
        fh = getattr(self, "_lockfh", None)
        if fh:
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            fh.close()
            self._lockfh = None

    def stop(self):
        br = getattr(self, "bridge", None)
        if br is not None:
            br.stop()                                       # ngừng nhận job; việc đang đăng làm cho xong
        self.stop_ev.set()
        with self.life_lock:
            self._stop_inner()

    def _stop_inner(self):
        if getattr(self, "warmer", None) is not None:
            self.warmer.stop()
            self.warmer = None
        if self.api is not None:                          # ngừng nguồn API trước (không phát hiện thêm khi đang tắt)
            try:
                self.api.stop()
            except Exception:
                pass
            self.api = None
        self.stop_ev.set()
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None
        for _ in range(int(self.cfg.get("workers", 3)) + 1):
            self.dlq.put((float("inf"), next(self.seq), None))
        self.xlq.put(None)
        for t in self.threads:
            t.join(timeout=5)
        t0 = time.time()                                  # chờ các lần tải/ghép dở dang trả về (mỗi bước đều có timeout riêng)
        while self.active:
            if int(time.time() - t0) % 10 == 0 and time.time() - t0 > 1:
                self.log(f"⏳ đang chờ {len(self.active)} video dở dang kết thúc trước khi nhả khoá...")
            time.sleep(0.5)
        self.threads = []
        self._release_instance_lock()
        self.log("⏹ đã dừng")

    def _spawn(self, fn, name):
        def run():
            while not self.stop_ev.is_set():
                try:
                    fn()
                    return
                except Exception as e:
                    self.log(f"❌ luồng {name} lỗi: {type(e).__name__}: {e} — khởi động lại sau 2s")
                    self.log(traceback.format_exc(limit=3))
                    self.stop_ev.wait(2)
        t = threading.Thread(target=run, name=name, daemon=True)
        t.start()
        self.threads.append(t)

    # ---------------- Excel → danh sách kênh ---------------- #
    def reload_channels(self, initial=False):
        try:
            chans, bad = self.excel.read_channels()
        except FileNotFoundError:
            self.log(f"❌ Không thấy {self.excel.path.name} trong thư mục tool")
            return None
        except Exception as e:
            self.log(f"❌ Đọc Excel lỗi: {type(e).__name__}: {e}")
            return None
        with self.ch_lock:
            old = set(self.channels)
            self.channels = chans
            self.channels_loaded = True
        new = set(chans)
        added, removed = new - old, old - new
        if initial or added or removed:
            dup = sum(1 for v in chans.values() if len(v) > 1)
            self.log(f"📗 Excel: {len(chans)} kênh (cột {self.cfg['col_channel']}) | trùng nhiều dòng: {dup} | "
                     f"giá trị sai: {len(bad)}" + (f" | +{len(added)} -{len(removed)}" if not initial else ""))
            for r, v in bad[:5]:
                self.log(f"   ⚠️ dòng {r}: '{v}' không phải Channel ID (UC + 22 ký tự)")
        if not initial:
            for ch in removed:
                self._unsubscribe(ch)
        return added, removed

    def _excel_watch(self):
        last = self.excel.sig()
        while not self.stop_ev.wait(2.0):
            s = self.excel.sig()
            if s and s != last:
                res = self.reload_channels()      # đọc lại cả khi do chính ta ghi (người dùng có thể sửa cùng lúc)
                if res is None:
                    continue                      # đọc lỗi (file đang ghi dở…) → giữ 'last' cũ, lần sau đọc lại
                last = s
                if res[0] and self.websub_on:              # API_ONLY: không đăng ký hub
                    self.subscribe_all(list(res[0]))
                if self.on_excel_reload is not None:
                    try:
                        self.on_excel_reload()
                    except Exception as e:  # noqa: BLE001
                        self.log(f"⚠️ cập nhật ánh xạ TikTok lỗi: {e}")

    def monitored(self, ch):
        with self.ch_lock:
            return ch in self.channels

    # ---------------- XỬ LÝ SỰ KIỆN (đường nóng) ---------------- #
    def on_event(self, ch: str, vid: str, engine: str, published: datetime | None = None, title="",
                 t_recv_ns: int | None = None, race=True, extra=None) -> str:
        """Gọi từ webhook WebSub VÀ từ API scanner (cùng 1 hàm = cùng bộ chống trùng nguyên tử). KHÔNG chặn: chỉ chống trùng
        + đưa vào 2 hàng đợi độc lập. Nguồn tới SAU chỉ được ghi nhận (đo race) và có thể kích thử lại sớm."""
        t_recv_ns = t_recv_ns or time.perf_counter_ns()
        if not VID_RE.match(vid or "") or not norm_channel(ch):
            return "invalid"
        if not self.monitored(ch):
            return "foreign"
        if published is not None:
            age = (now_utc() - published).total_seconds()
            if not self.is_new_video(published):
                # KHÔNG đánh dấu 'đã thấy' vĩnh viễn: chỉ ghi nhật ký để có thể xem lại
                self.hub_event(ch, "push-old", "", None, 0, f"{vid} published {iso(published)} tuổi {int(age)}s ({engine})")
                with self.seen_lock:
                    self.old_logged.add(vid)
                return "old"
        wall = time.time()
        extra = extra or {}
        with self.seen_lock:                              # kiểm + đánh dấu trong 1 khoá: 2 nguồn cùng lúc → đúng 1 bên thắng
            dup = vid in self.seen
            rec = self.race.get(vid)
            if not dup:
                self.seen.add(vid)
                rec = self.race[vid] = {"winner": engine, "ns": {engine: t_recv_ns}, "wall": {engine: wall}}
                first_src = True
            else:
                if rec is None:
                    rec = self.race[vid] = {"winner": None, "ns": {}, "wall": {}}
                first_src = engine not in rec["ns"]
                if first_src:
                    rec["ns"][engine] = t_recv_ns
                    rec["wall"][engine] = wall
        if dup:
            if first_src and race:
                try:
                    self._second_signal(vid, ch, engine, rec, title, published, wall, t_recv_ns, extra)
                except Exception as e:  # noqa: BLE001
                    self.log(f"⚠️ ghi nhận nguồn thứ 2 cho {vid} lỗi: {type(e).__name__}: {e}")
            return "dup"
        url = video_url(vid)
        meta = {"title": title, "engine": engine, "published": iso(published) if published else None, "detected_at": iso()}
        okey = meta["published"] or meta["detected_at"]  # thứ tự "mới nhất" cho ô D: theo giờ đăng
        # 1) đưa NGAY vào 2 hàng đợi độc lập (worker tự tạo dòng DB nếu tới trước), 2) rồi ghi bền vững SQLite (WAL).
        #    Cửa sổ mất việc khi tắt đột ngột chỉ là vài micro-giây giữa 2 bước.
        t_enq = time.perf_counter_ns()
        if self.flags["download"]:
            self.dlq.put((time.time(), next(self.seq), {"vid": vid, "ch": ch, "t_recv_ns": t_recv_ns, "t_enq_ns": t_enq,
                                                         "tiktok": bool(self.tiktok_on), **meta}))
        if self.flags["excel"]:
            self.xlq.put({"vid": vid, "ch": ch, "url": url, "t_enq_ns": t_enq, "okey": okey})
        enq_ms = ms(t_recv_ns, t_enq)
        stmts = [("INSERT OR IGNORE INTO videos(video_id, channel_id, url, title, engine, detected_at, published_at, state, "
                  "enqueue_ms) VALUES(?,?,?,?,?,?,?,?,?)",
                  (vid, ch, url, title, engine, meta["detected_at"], meta["published"],
                   "QUEUED" if self.flags["download"] else "SKIPPED", enq_ms)),
                 ("INSERT OR IGNORE INTO detections(video_id, source, channel_id, t_wall, detected_at, winner, lag_ms, "
                  "published_at, title, api_interval_s, baseline, mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (vid, engine, ch, wall, meta["detected_at"], 1, 0.0, meta["published"], title,
                   extra.get("api_interval_s"), 1 if extra.get("baseline") else 0, self.mode))]
        if self.tiktok_on:
            stmts.append(("UPDATE videos SET tiktok=1 WHERE video_id=?", (vid,)))
        if self.flags["excel"]:
            stmts.append(("INSERT OR IGNORE INTO excel_jobs(video_id, channel_id, url, state, created_at, order_key) "
                          "VALUES(?,?,?,?,?,?)", (vid, ch, url, "PENDING", iso(), okey)))
        self.db.tx(stmts)                                 # các bản ghi cùng 1 giao dịch
        self.stats["detected"] += 1
        self.stats["wins_api" if engine == "API" else "wins_websub"] += 1
        self._alert()
        age_s = f" | tuổi {(now_utc() - published).total_seconds():.1f}s" if published else ""
        self.log(f"🔥 {engine}_DETECTED {vid} | WINNER={engine} | [{engine}] VIDEO MỚI | kênh {ch} | nhận→hàng đợi "
                 f"{enq_ms:.3f} ms{age_s}" + (" | mốc khởi động API" if extra.get("baseline") else "") + f" | {title[:60]}")
        return "queued"

    def _second_signal(self, vid, ch, engine, rec, title, published, wall, t_ns, extra):
        """Nguồn còn lại cũng thấy video: ghi đo race, bổ sung tiêu đề, và nếu link tải CHƯA sẵn sàng → thử lại sớm."""
        win = rec.get("winner")
        lag = None
        if win and win in rec["ns"] and win != engine:
            lag = (t_ns - rec["ns"][win]) / 1e6
        if lag is None:                                   # nguồn thắng ở lần chạy trước → so bằng đồng hồ máy
            r = self.db.one("SELECT source, t_wall FROM detections WHERE video_id=? AND winner=1", (vid,))
            if r is not None and r["source"] != engine:
                win, lag = r["source"], (wall - float(r["t_wall"] or wall)) * 1000
            elif r is None:
                v = self.db.one("SELECT engine FROM videos WHERE video_id=?", (vid,))
                win = v["engine"] if v is not None else None
        if not win or win == engine:
            return                                        # cùng 1 nguồn gửi lại (vd WebSub cập nhật) → không phải race
        rec["winner"] = win
        self.db.x("INSERT OR IGNORE INTO detections(video_id, source, channel_id, t_wall, detected_at, winner, lag_ms, "
                  "published_at, title, api_interval_s, baseline, mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (vid, engine, ch, wall, iso(), 0, None if lag is None else round(lag, 3),
                   iso(published) if published else None, title, extra.get("api_interval_s"), 0, self.mode))
        row = self.db.one("SELECT state, title FROM videos WHERE video_id=?", (vid,))
        if row is not None and title and not (row["title"] or "").strip():
            self.db.x("UPDATE videos SET title=? WHERE video_id=? AND (title IS NULL OR title='')", (title, vid))
        self.log(f"📡 {engine}_DETECTED {vid} | WINNER={win} | {engine} chậm hơn "
                 + (f"{lag:.0f} ms" if lag is not None else "? ms") + f" | trạng thái tải: {row['state'] if row else '?'}")
        if row is not None and row["state"] == "RETRY_WAIT":
            self._nudge(vid, engine)

    def _nudge(self, vid, src):
        """Có kiểm soát: mỗi nguồn tối đa 1 lần/video, chỉ khi đang CHỜ thử lại (không chen lúc đang tải), không tăng số lần
        thử tối đa. Lần chờ cũ bị huỷ nhờ 'thế hệ' (không chạy 2 lần)."""
        with self.active_lock:
            if (vid, src) in self.nudged or vid in self.active or vid not in self.retry_jobs:
                return
            self.nudged.add((vid, src))
            g = self.retry_gen.get(vid, 0) + 1
            self.retry_gen[vid] = g
            job = dict(self.retry_jobs[vid], not_before=0, gen=g, nudged_by=src)
        self.dlq.put((time.time(), next(self.seq), job))
        self.log(f"🔔 {vid}: {src} cũng báo có video → thử lấy link tải lại NGAY (không chờ hết lịch thử lại)")

    def _alert(self):
        wav = str(self.cfg.get("alert_wav") or "")
        if not wav or os.name != "nt":
            return
        try:
            import winsound
            p = Path(wav) if Path(wav).is_absolute() else self.base / wav
            if p.is_file():
                winsound.PlaySound(str(p), winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception:
            pass

    def _db_insert_video(self, vid, ch, meta, state, enq_ms=None):
        self.db.x("INSERT OR IGNORE INTO videos(video_id, channel_id, url, title, engine, detected_at, published_at, state, enqueue_ms)"
                  " VALUES(?,?,?,?,?,?,?,?,?)", (vid, ch, video_url(vid), meta.get("title", ""), meta.get("engine", ""),
                                                 meta.get("detected_at") or iso(), meta.get("published"), state, enq_ms))
        if enq_ms is not None:
            self.db.upd(vid, enqueue_ms=enq_ms)

    def is_new_video(self, published) -> bool:
        """MỚI nếu: tuổi <= max_age_minutes, HOẶC được đăng ĐÚNG trong khoảng tool tắt (từ lần còn sống cuối −10 phút tới lúc
        mở lại, tối đa 24h) — để push hub GỬI LẠI muộn sau khi tool/ngrok tắt vẫn được tải, mà sửa video cũ thì không."""
        if published is None:
            return True
        age = (now_utc() - published).total_seconds()
        if age <= float(self.cfg.get("max_age_minutes", 30)) * 60:
            return True
        la = self.last_alive_at_start
        if la and age <= 86400:
            t = published.timestamp()
            return la - 600 <= t <= self.started_at
        return False

    def _mark_seen(self, vid, ch, why):
        with self.seen_lock:
            if vid in self.seen:
                return
            self.seen.add(vid)
        self.db.x("INSERT OR IGNORE INTO seen(video_id, channel_id, why, at) VALUES(?,?,?,?)", (vid, ch, why, iso()))

    # ---------------- khôi phục sau khi tắt đột ngột ---------------- #
    def recover(self):
        n = 0
        for r in self.db.all(f"SELECT * FROM videos WHERE state IN ({','.join('?' * len(STATES_ACTIVE))})", STATES_ACTIVE):
            self.dlq.put((time.time(), next(self.seq), {"vid": r["video_id"], "ch": r["channel_id"],
                                                         "t_recv_ns": None, "t_enq_ns": time.perf_counter_ns(), "recovered": True}))
            n += 1
        m = 0
        for r in self.db.all("SELECT * FROM excel_jobs WHERE state IN ('PENDING','RETRY')"):
            self.xlq.put({"vid": r["video_id"], "ch": r["channel_id"], "url": r["url"], "t_enq_ns": time.perf_counter_ns(),
                          "okey": r["order_key"] or r["created_at"] or ""})
            m += 1
        for p in self.dl_dir.rglob(".*"):              # CHỈ file tạm đúng mẫu của tool: tiêu đề gốc (kể cả bắt đầu
            if not p.is_file() or not TEMP_RE.match(p.name):    # bằng '.' hay có chữ '.part') KHÔNG bao giờ bị xoá nhầm
                continue
            try:
                p.unlink()
            except OSError:
                pass
        if n or m:
            self.log(f"♻️ Khôi phục: {n} video đang dở, {m} lần ghi Excel chưa xong")

    # ---------------- DOWNLOAD ---------------- #
    def _dl_worker(self):
        while not self.stop_ev.is_set():
            _, _, job = self.dlq.get()
            if job is None:
                return
            if "gen" in job and self.retry_gen.get(job["vid"]) != job["gen"]:
                continue                                  # lần chờ cũ đã được thay bằng lần thử sớm (tín hiệu nguồn thứ 2)
            nt = job.get("not_before", 0)
            if nt and time.time() < nt:
                # chưa tới giờ thử lại: đặt lại hàng đợi theo thời điểm, ngủ ngắn (không chặn sự kiện mới vì priority theo thời gian)
                self.dlq.put((nt, next(self.seq), job))
                self.stop_ev.wait(min(0.2, max(0.0, nt - time.time())))
                continue
            with self.sem:
                self.stats["downloading"] += 1
                try:
                    r = self.process(job)
                    if r == "BUSY":                       # đang có worker khác xử lý đúng video này
                        self.dlq.put((time.time() + 1, next(self.seq), dict(job, not_before=time.time() + 1)))
                except Exception as e:  # noqa: BLE001 — không bao giờ để mất việc vì lỗi bất ngờ
                    self.log(f"❌ worker lỗi bất ngờ với {job.get('vid')}: {type(e).__name__}: {e} → thử lại sau 5s")
                    self.dlq.put((time.time() + 5, next(self.seq), dict(job, not_before=time.time() + 5)))
                finally:
                    self.stats["downloading"] -= 1

    def process(self, job) -> str:
        """Một lần thử cho 1 video. MỌI lỗi (kể cả trước khi tải) đi qua cùng logic thử lại có giới hạn / FAILED."""
        vid, ch = job["vid"], job["ch"]
        with self.active_lock:
            if vid in self.active:                        # không bao giờ 2 worker cùng 1 video
                return "BUSY"
            self.active.add(vid)
            self.retry_jobs.pop(vid, None)                # đang chạy → không còn 'đang chờ thử lại'
            if "gen" in job:
                self.retry_gen[vid] = job["gen"] + 1      # mọi bản sao cũ của job trong hàng đợi đều hết hiệu lực
        attempts = 0
        parts = []
        cancel = threading.Event()
        th = None
        try:
            row = self.db.one("SELECT * FROM videos WHERE video_id=?", (vid,))
            if row is None:                               # worker tới trước lần ghi DB của on_event
                self._db_insert_video(vid, ch, job, "QUEUED")
                row = self.db.one("SELECT * FROM videos WHERE video_id=?", (vid,))
            if row and row["state"] in ("DONE", "FAILED", "SKIPPED"):
                return row["state"]
            attempts = int(row["attempts"] if row else 0)
            title = (row["title"] if row else "") or job.get("title") or ""
            legacy = self.dl_dir / ch / f"{vid}_360p.mp4"
            final = Path(row["target_file"]) if row is not None and row["target_file"] else \
                (self._choose_target(vid, ch, title) if title.strip() else None)       # tiêu đề từ WebSub/API: KHÔNG gọi API thêm
            for cand in dict.fromkeys(c for c in (final, legacy) if c is not None):
                if not cand.exists():
                    continue
                v = ffprobe_info(cand)
                exp = row["expected_size"] if row and "expected_size" in row.keys() else None
                if exp and cand.stat().st_size != int(exp):    # file dở do tắt giữa chừng (tải thẳng, không .part)
                    v = dict(v, ok=False)
                if v["ok"]:
                    self.db.upd(vid, state="DONE", file=str(cand), size=cand.stat().st_size, error=None)
                    self.log(f"✅ {vid} đã có file hợp lệ ({cand.name}) — không tải lại")
                    return "DONE"
                cand.unlink(missing_ok=True)
            (self.dl_dir / ch).mkdir(parents=True, exist_ok=True)
            t_start = time.perf_counter_ns()
            k = {}
            if job.get("t_recv_ns"):
                k["resolver_start_ms"] = ms(job["t_recv_ns"], t_start)
            db_res = self.db.upd(vid, state="RESOLVING", attempts=attempts + 1, **k)
            tag = f"{vid}.a{attempts + 1}"                # tên tạm RIÊNG cho từng lần thử
            plan = self.resolver.resolve(vid)
            t_res = time.perf_counter_ns()
            if final is None:                             # WebSub/API không có tiêu đề → dùng tiêu đề resolver vừa trả (miễn phí)
                if plan.get("title") and not title.strip():
                    self.db.x("UPDATE videos SET title=? WHERE video_id=? AND (title IS NULL OR title='')", (plan["title"], vid))
                final = self._choose_target(vid, ch, plan.get("title") or "")
            tmpdir = final.parent
            pick = plan["pick"]
            exp_size = int(pick["video"].get("clen") or 0) if pick["kind"] == "progressive" else 0
            db_dl = self.db.upd(vid, state="DOWNLOADING", resolver_ms=ms(t_start, t_res), kind=pick["kind"],
                                itags=",".join(str(f["itag"]) for f in (pick["video"], pick["audio"]) if f),
                                download_started_at=iso(), expected_size=exp_size or None)
            info_v, info_a = {}, {}
            mux_ms = None
            n = int(self.cfg.get("range_connections", 1))
            atomic = bool(self.cfg.get("atomic_rename", False)) or self.on_done is not None   # tích hợp: luôn .part → đổi tên
            # atomic_rename=False (mặc định theo yêu cầu): ghi THẲNG vào tên cuối; DONE chỉ sau ffprobe + đúng dung lượng;
            # lỗi → xoá file. atomic_rename=True: ghi .part rồi os.replace.
            out_tmp = (tmpdir / f".{vid}.part.mp4") if atomic else final        # uploader KHÔNG BAO GIỜ nhận .part
            parts.append(out_tmp)
            if pick["kind"] == "progressive":
                self._fetch(pick["video"], out_tmp, info_v, n, cancel)
                if exp_size and out_tmp.stat().st_size != exp_size:
                    raise IOError(f"dung lượng {out_tmp.stat().st_size} ≠ {exp_size} byte YouTube báo")
            else:
                vtmp = tmpdir / f".{tag}.part.v"
                atmp = tmpdir / f".{tag}.part.a"
                parts += [vtmp, atmp]
                errs = []
                if pick["audio"]:
                    nd = max(1, int(self.cfg.get("dash_range_connections", 4)))

                    def _a():
                        try:
                            self._fetch(pick["audio"], atmp, info_a, nd, cancel)
                        except Exception as e:  # noqa: BLE001
                            errs.append(e)
                    th = threading.Thread(target=_a, daemon=True, name=f"audio-{vid}")
                    th.start()
                self._fetch(pick["video"], vtmp, info_v, max(1, int(self.cfg.get("dash_range_connections", 4))), cancel)
                if th:
                    th.join()
                    th = None
                    if errs:
                        raise errs[0]
                self.db.upd(vid, state="VERIFYING")
                if not FFMPEG:
                    raise Fatal("cần ffmpeg để ghép/đóng gói 360p DASH")
                mux_ms = ffmpeg_mux(vtmp, atmp if pick["audio"] else None, out_tmp)
            t_dl = time.perf_counter_ns()                                  # tải (và ghép) xong
            t_db0 = time.perf_counter_ns()
            db_v = self.db.upd(vid, state="VERIFYING")                     # DB_VERIFYING (gồm chờ khoá)
            t_db1 = time.perf_counter_ns()
            t_v0 = time.perf_counter_ns()                                  # PRE_VERIFY_GAP = t_db1→t_v0
            v = ffprobe_info(out_tmp)
            t_v1 = time.perf_counter_ns()                                  # FFPROBE = t_v0→t_v1
            verify_ms = ms(t_v0, t_v1)
            if not v["ok"]:
                raise IOError(f"ffprobe: file hỏng ({v})")
            if not v.get("audio"):
                self.log(f"⚠️ {vid}: nguồn không có luồng tiếng ({pick['kind']}) — giữ file chỉ có hình")
            if pick["kind"] == "dash" and pick["audio"] and not v.get("audio"):
                raise IOError("ghép xong nhưng không có audio")
            t_r0 = time.perf_counter_ns()
            if out_tmp != final:
                _replace_retry(out_tmp, final)                              # RENAME = t_r0→t_r1 (chỉ khi atomic_rename)
            parts = [p for p in parts if p != final]                       # thành công: KHÔNG xoá file cuối
            t_r1 = time.perf_counter_ns()
            t_end = time.perf_counter_ns()
            size = final.stat().st_size
            dl_ms = ms(t_res, t_dl) - (mux_ms or 0)
            total = ms(job["t_recv_ns"], t_end) if job.get("t_recv_ns") else ms(t_start, t_end)
            steps = {"db_verifying_ms": ms(t_db0, t_db1), "db_lock_wait_ms": db_v[1], "pre_verify_gap_ms": ms(t_db1, t_v0),
                     "ffprobe_ms": verify_ms, "rename_ms": ms(t_r0, t_r1),
                     "post_verify_gap_ms": round(ms(t_v1, t_r0) + ms(t_r1, t_end), 3),
                     "db_resolving_ms": db_res[0], "db_downloading_ms": db_dl[0]}
            parts_sum = (ms(job["t_recv_ns"], t_start) if job.get("t_recv_ns") else 0) + ms(t_start, t_res) + dl_ms + \
                (mux_ms or 0) + steps["db_verifying_ms"] + steps["pre_verify_gap_ms"] + steps["ffprobe_ms"] + \
                steps["post_verify_gap_ms"] + steps["rename_ms"]
            steps["sum_check_ms"] = round(total - parts_sum, 3)            # phải ≈ 0: tổng các bước khớp total_ms
            mbps = round(size * 8 / 1e6 / max(1e-3, (info_v.get("elapsed_ms") or dl_ms) / 1000), 1)
            self.db.upd(vid, state="DONE", file=str(final), size=size, ttfb_ms=info_v.get("ttfb_ms"), download_ms=dl_ms,
                        mux_ms=mux_ms, verify_ms=verify_ms, total_ms=total, done_at=iso(), error=None, mbps=mbps, **steps)
            slow = [f"{k}={v_:.0f}" for k, v_ in steps.items() if isinstance(v_, (int, float)) and abs(v_) > 50]
            self.log(f"⏱️ BƯỚC {vid} | DB_VERIFYING_MS {steps['db_verifying_ms']:.1f} (chờ khoá {db_v[1]:.1f}"
                     f"{', luồng giữ: ' + str(db_v[2]) if db_v[2] else ''}) | PRE_VERIFY_GAP_MS {steps['pre_verify_gap_ms']:.2f} | "
                     f"FFPROBE_MS {steps['ffprobe_ms']:.1f} | RENAME_MS {steps['rename_ms']:.1f} | "
                     f"POST_VERIFY_GAP_MS {steps['post_verify_gap_ms']:.2f} | DB resolving {db_res[0]:.1f} / downloading "
                     f"{db_dl[0]:.1f} | lệch tổng {steps['sum_check_ms']:.3f}" + (f" | ⚠️ CHẬM: {', '.join(slow)}" if slow else ""))
            self.stats["done"] += 1
            want_tt = bool(job.get("tiktok")) or bool((row["tiktok"] if row is not None and "tiktok" in row.keys() else 0))
            if self.on_done is not None and want_tt:
                try:                                       # DOWNLOAD_DONE → job TikTok ngay (không chặn tải/Excel)
                    self.on_done({"vid": vid, "ch": ch, "path": str(final), "size": size, "t_recv_ns": job.get("t_recv_ns"),
                                  "t_detect_ns": job.get("t_enq_ns"), "t_dl_start_ns": t_res, "t_done_ns": t_end})
                except Exception as e:  # noqa: BLE001
                    self.log(f"❌ không chuyển được {vid} sang TikTok: {type(e).__name__}: {e} (video vẫn an toàn trong DOWNLOADS)")
            self.stats["speed_mbps"] = mbps
            self.stats["last"] = {"vid": vid, "resolver_ms": ms(t_start, t_res), "download_ms": dl_ms, "mux_ms": mux_ms,
                                  "verify_ms": verify_ms, "total_ms": total}
            itags = ",".join(str(f["itag"]) for f in (pick["video"], pick["audio"]) if f)
            try:
                rn = plan.get("net") or {}
                net = {"resolver": dict(rn, client=plan.get("client"), race_ms=plan.get("race_ms")),
                       "media": {k: info_v.get(k) for k in ("host", "hops", "new_conns", "dns_ms", "tcp_ms", "tls_ms", "server_wait_ms",
                                                            "headers_ms", "first_byte_after_hdr_ms", "ttfb_ms", "transfer_ms")},
                       "ffprobe_ms": verify_ms, "net_mode": _NET_MODE}
                self.db.upd(vid, net=json.dumps(net, ensure_ascii=False))
                f_ = lambda v: "-" if v is None else (f"{v:.0f}" if isinstance(v, (int, float)) else str(v))
                m_ = net["media"]
                self.log(f"🌐 MẠNG {vid} | RESOLVER {plan.get('client')} {f_(ms(t_start, t_res))} ms: "
                         f"{'kết nối MỚI' if rn.get('new_conn') else 'kết nối dùng lại'} DNS {f_(rn.get('dns_ms'))} TCP {f_(rn.get('tcp_ms'))} "
                         f"TLS {f_(rn.get('tls_ms'))} | chờ máy chủ {f_(rn.get('server_wait_ms'))} | đọc {f_(rn.get('body_ms'))} "
                         f"({f_(rn.get('kb'))} KB) | parse {f_(rn.get('parse_ms'))} | race {plan.get('race_ms')} || "
                         f"TẢI {m_.get('host')}: {f_(m_.get('new_conns'))} kết nối mới/{f_(m_.get('hops'))} chặng | DNS {f_(m_.get('dns_ms'))} "
                         f"TCP {f_(m_.get('tcp_ms'))} TLS {f_(m_.get('tls_ms'))} | chờ máy chủ {f_(m_.get('server_wait_ms'))} | "
                         f"TTFB {f_(m_.get('ttfb_ms'))} | truyền sau byte đầu {f_(m_.get('transfer_ms'))} | ffprobe {f_(verify_ms)}")
            except Exception as e:  # noqa: BLE001 — số đo không bao giờ làm hỏng việc tải
                self.log(f"⚠️ ghi số đo mạng lỗi: {type(e).__name__}: {e}")
            det = self.race.get(vid) or {}
            self.log(f"🏁 DETECTION_TO_DOWNLOAD_DONE {vid} = "
                     + (f"{total:.0f} ms (WINNER={det.get('winner') or job.get('engine') or '?'})" if job.get("t_recv_ns")
                        else "không đo (job khôi phục sau khởi động lại)") + f" | file: {final.name}")
            self.log(f"✅ DONE {vid} | {pick['kind']} itag {itags} | {size / 1e6:.2f} MB | resolver {ms(t_start, t_res):.0f} ms "
                     f"({plan.get('source')}) | TTFB {info_v.get('ttfb_ms')} ms | tải {dl_ms:.0f} ms ≈{mbps} Mbps | "
                     f"mux {mux_ms or 0:.0f} ms | verify {verify_ms:.0f} ms | TỔNG {total:.0f} ms | "
                     f"{v.get('width')}x{v.get('height')} {v.get('vcodec')}/{v.get('acodec')}"
                     + (f" | kết nối DASH: hình {info_v.get('connections', 1)}, tiếng {info_a.get('connections', 1)}"
                        if pick["kind"] == "dash" else "")
                     + (f" | vì sao không dùng Innertube nhanh: {plan.get('race')}" if plan.get("race") else ""))
            return "DONE"
        except Exception as e:  # noqa: BLE001
            if isinstance(e, InterruptedError) or self.stop_ev.is_set():
                self.db.upd(vid, state="QUEUED", error="dừng giữa chừng")
                return "STOPPED"
            sched = list(self.cfg.get("retry_schedule_sec") or [1, 2, 5])
            fatal = isinstance(e, Fatal) or (isinstance(e, HttpStatus) and e.code in (404, 410))
            if fatal or attempts + 1 > len(sched):
                self.db.upd(vid, state="FAILED", error=f"{type(e).__name__}: {str(e)[:200]}")
                self.stats["failed"] += 1
                self.log(f"❌ FAILED {vid} sau {attempts + 1} lần: {type(e).__name__}: {redact(str(e))[:200]}")
                return "FAILED"
            wait = sched[min(attempts, len(sched) - 1)]
            if isinstance(e, HttpStatus) and e.code == 429:
                wait = max(wait, float(e.retry_after or 0), 10.0)
            with self.active_lock:
                if isinstance(e, HttpStatus) and e.code in (403, 429):
                    self.nudged |= {(vid, "API"), (vid, "WEBSUB")}   # 403/429: tôn trọng giãn cách, không thử sớm
                g = self.retry_gen.get(vid, 0) + 1
                self.retry_gen[vid] = g
                job = dict(job, not_before=time.time() + wait, gen=g)
                self.retry_jobs[vid] = job
            self.db.upd(vid, state="RETRY_WAIT", next_try=time.time() + wait, error=f"{type(e).__name__}: {str(e)[:200]}")
            self.log(f"⏳ {vid}: {type(e).__name__}: {redact(str(e))[:160]} → thử lại sau {wait}s (lần {attempts + 1}/{len(sched)})"
                     + (" — nếu nguồn phát hiện còn lại báo video này, thử lại ngay" if self.mode == "PARALLEL" else ""))
            self.dlq.put((job["not_before"], next(self.seq), job))
            return "RETRY_WAIT"
        finally:
            cancel.set()                                  # luồng audio (nếu còn) dừng ngay
            if th is not None:
                th.join(timeout=30)
            for p in parts:                               # luôn dọn file tạm của lần thử này
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
            with self.active_lock:
                self.active.discard(vid)

    def _choose_target(self, vid, ch, title) -> Path:
        """Chọn TÊN FILE CUỐI 1 lần rồi lưu SQLite (khởi động lại vẫn đúng tên). Chống trùng: tên đã thuộc video KHÁC
        (so không phân biệt hoa/thường như Windows) → thêm ' (2)', ' (3)'…"""
        folder = self.dl_dir / ch
        with self.name_lock:
            r = self.db.one("SELECT target_file FROM videos WHERE video_id=?", (vid,))
            if r is not None and r["target_file"]:
                return Path(r["target_file"])
            p = None
            for n in range(1, 100):
                name = build_filename(title, vid, folder, self.cfg.get("filename_mode", "TITLE"),
                                      int(self.cfg.get("filename_title_max", 0) or 0), int(self.cfg.get("filename_max_path", 240)), n)
                p = folder / name
                clash = self.db.one("SELECT video_id FROM videos WHERE video_id<>? AND (lower(target_file)=lower(?) "
                                    "OR lower(file)=lower(?))", (vid, str(p), str(p)))
                # file đã có trên đĩa mà KHÔNG phải của video này (tên không chứa ID) → KHÔNG BAO GIỜ dùng lại / ghi đè
                if clash is None and not p.exists() and not TEMP_RE.match(name):
                    break
            self.db.upd(vid, target_file=str(p))
            return p

    def _fetch(self, fmt, dest, info, n, cancel=None):
        size = int(fmt.get("clen") or 0)
        cancel = cancel or self.stop_ev
        if n > 1 and size <= 0:
            try:
                size = probe_size(fmt["url"])                 # yt-dlp hay không báo dung lượng → dò bằng 1 byte đầu
                info["probed_size"] = size
            except Exception as e:  # noqa: BLE001
                info["probe_error"] = f"{type(e).__name__}"
        if n > 1 and size > 0:
            try:
                return download_ranges(fmt["url"], dest, size, n, info, _AnyEvent(cancel, self.stop_ev))
            except InterruptedError:
                raise
            except Exception as e:  # noqa: BLE001
                if isinstance(e, HttpStatus) and e.code in (403, 404, 410, 429):
                    raise
                self.log(f"   ↩️ Range x{n} lỗi ({type(e).__name__}: {redact(str(e))[:80]}) → 1 GET")
        return download_single(fmt["url"], dest, info, _AnyEvent(cancel, self.stop_ev))

    # ---------------- EXCEL WRITER (độc lập với download) ---------------- #
    def _excel_newer_done(self, ch, okey) -> bool:
        r = self.db.one("SELECT MAX(order_key) k FROM excel_jobs WHERE channel_id=? AND state='DONE'", (ch,))
        return bool(r and r["k"] and okey and r["k"] > okey)

    def _excel_worker(self):
        """Gom việc đến hạn → MỘT lần mở/lưu Excel. Mỗi kênh chỉ ghi URL có giờ đăng MỚI NHẤT (không bao giờ ghi đè
        link mới bằng link cũ, kể cả sau khi khởi động lại). Excel khoá → giữ (SQLite + RAM), thử lại giãn cách.
        Lỗi khác (không phải khoá) → tách từng kênh để 1 dòng lỗi không chặn cả lô; quá 10 lần → FAILED."""
        delays = [0.5, 1, 2, 5, 10, 20, 30]
        pending = []
        fails = 0
        next_ok = 0.0
        single = False
        while not self.stop_ev.is_set():
            try:
                item = self.xlq.get(timeout=0.25)
            except queue.Empty:
                item = "tick"
            if item is None:
                return
            if item != "tick":
                pending.append(item)
            while True:
                try:
                    it2 = self.xlq.get_nowait()
                except queue.Empty:
                    break
                if it2 is None:
                    return
                pending.append(it2)
            self.stats["excel_pending"] = len(pending)
            now = time.time()
            if not pending or now < next_ok:
                continue
            # chọn bản mới nhất theo giờ đăng cho từng kênh; bản cũ hơn → SUPERSEDED (không ghi)
            latest = {}
            for it in pending:
                cur = latest.get(it["ch"])
                if cur is None or (it.get("okey") or "") >= (cur.get("okey") or ""):
                    latest[it["ch"]] = it
            for it in pending:
                if latest[it["ch"]] is not it or self._excel_newer_done(it["ch"], it.get("okey")):
                    self.db.x("UPDATE excel_jobs SET state='SUPERSEDED', done_at=?, error=? WHERE video_id=? AND channel_id=?",
                              (iso(), "kênh đã có link video mới hơn ở cột D", it["vid"], it["ch"]))
            todo = [it for it in latest.values() if not self._excel_newer_done(it["ch"], it.get("okey"))]
            pending = []
            batches = [[it] for it in todo] if single else ([todo] if todo else [])
            retry = []
            for batch in batches:
                mapping = {it["ch"]: it["url"] for it in batch}
                t0 = time.perf_counter_ns()
                try:
                    res = self.excel.write_urls(mapping)
                except ExcelLocked as e:
                    if fails in (0, 3, 6) or fails % 20 == 0:
                        self.log(f"🔒 Excel bận ({e}) → {len(batch)} URL giữ trong SQLite, thử lại")
                    for it in batch:
                        self.db.x("UPDATE excel_jobs SET attempts=attempts+1, error=?, state='RETRY' WHERE video_id=? AND channel_id=?",
                                  (str(e)[:200], it["vid"], it["ch"]))
                    retry += batch
                    continue
                except Exception as e:  # noqa: BLE001
                    self.stats["excel_err"] += 1
                    self.log(f"❌ Excel lỗi {type(e).__name__}: {e}" + (" → tách từng kênh" if len(batch) > 1 else ""))
                    for it in batch:
                        it["errs"] = it.get("errs", 0) + (1 if len(batch) == 1 else 0)
                        if it["errs"] >= 10:
                            self.db.x("UPDATE excel_jobs SET state='FAILED', error=?, done_at=? WHERE video_id=? AND channel_id=?",
                                      (f"{type(e).__name__}: {e}"[:200], iso(), it["vid"], it["ch"]))
                            self.log(f"❌ Excel: bỏ ghi {it['vid']} sau 10 lần lỗi ({type(e).__name__})")
                            continue
                        self.db.x("UPDATE excel_jobs SET attempts=attempts+1, error=?, state='RETRY' WHERE video_id=? AND channel_id=?",
                                  (f"{type(e).__name__}: {e}"[:200], it["vid"], it["ch"]))
                        retry.append(it)
                    single = True
                    continue
                wms = ms(t0, time.perf_counter_ns())
                for it in batch:
                    rows = res.get(it["ch"]) or []
                    if not rows:
                        self.db.x("UPDATE excel_jobs SET state='NO_ROW', error=?, done_at=? WHERE video_id=? AND channel_id=?",
                                  ("không còn dòng nào có Channel ID này", iso(), it["vid"], it["ch"]))
                        self.log(f"⚠️ Excel: không còn dòng có kênh {it['ch']} — bỏ ghi {it['vid']}")
                        continue
                    self.db.x("UPDATE excel_jobs SET state='DONE', done_at=?, write_ms=?, rows=?, error=NULL "
                              "WHERE video_id=? AND channel_id=?", (iso(), wms, ",".join(map(str, rows)), it["vid"], it["ch"]))
                    self.stats["excel_ok"] += 1
                    self.log(f"📝 Excel: {it['url']} → cột {self.cfg['col_url']} dòng {rows} | ghi {wms:.0f} ms | "
                             f"từ lúc vào hàng đợi {ms(it['t_enq_ns'], time.perf_counter_ns()):.0f} ms")
            if retry:
                next_ok = now + delays[min(fails, len(delays) - 1)]
                fails += 1
                pending = retry
            else:
                fails = 0
                single = False
            self.stats["excel_pending"] = len(pending)

    # ---------------- WEBSUB ---------------- #
    def start_webhook(self):
        eng = self
        path = self.cfg.get("websub_path", "/websub")

        class H(BaseHTTPRequestHandler):
            server_version = "WS360"
            protocol_version = "HTTP/1.1"
            timeout = 10                                      # chống kết nối treo / gửi chậm (slowloris)

            def log_message(self, *a):
                return

            def _reply(self, code, body=b"", ctype="text/plain"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                if body:
                    self.wfile.write(body)
                self.wfile.flush()

            def do_GET(self):
                u = urlparse(self.path)
                if u.path != path:
                    return self._reply(404)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                if q.get("hub.mode") == "denied":
                    chd = norm_channel(q.get("hub.topic", ""))
                    if chd and eng.monitored(chd):
                        eng.hub_event(chd, "denied", "denied", 200, 0, q.get("hub.reason", "")[:200])
                        eng.db.x("UPDATE subs SET state='DENIED', error=? WHERE channel_id=?", (q.get("hub.reason", "")[:200], chd))
                        eng.log(f"⚠️ WebSub hub TỪ CHỐI kênh {chd}: {q.get('hub.reason', '')[:120]}")
                    return self._reply(200)
                if "hub.challenge" not in q:                  # GET KIỂM TRA SỨC KHOẺ (không phải xác minh của hub)
                    eng.hub_event("", "health-get", q.get("hub.mode", ""), 200, 1,
                                  f"{eng.describe_source(self.client_address[0], self.headers)}")
                    return self._reply(200, b"WS360 OK")
                ch = norm_channel(q.get("hub.topic", ""))
                mode = q.get("hub.mode", "")
                src = eng.describe_source(self.client_address[0], self.headers)
                probe = q.get("ws360probe", "")
                if probe:                                      # GET xác minh cho đăng ký THỬ (--hubtest), không đụng subs
                    if probe in eng.probes and len(q["hub.challenge"]) <= 512:
                        eng.probes[probe].append((time.time(), mode))
                        eng.hub_event(ch or "", "verify-probe", mode, 200, 1, f"probe {probe} | {src}")
                        eng.log(f"🧪 HUB GỌI XÁC MINH (đăng ký thử {probe}, {mode}) kênh {ch} | {src}")
                        return self._reply(200, q["hub.challenge"].encode())
                    eng.hub_event(ch or "", "verify-probe", mode, 404, 0, f"probe lạ {probe} | {src}")
                    return self._reply(404)
                gen = q.get("g")
                if gen is not None and eng.cfg.get("callback_generation", True) and gen != secret_fp(eng.secret):
                    eng.hub_event(ch or "", "verify", mode, 404, 0, f"callback THẾ HỆ CŨ g={gen} | {src}")
                    eng.log(f"🚫 XÁC MINH BỊ TỪ CHỐI | kênh {ch} | mode={mode} | lý do: callback thế hệ cũ g={gen}")
                    return self._reply(404)
                # chỉ xác nhận yêu cầu mà CHÍNH TOOL đã gửi (chống giả mạo trạng thái đăng ký từ internet)
                asked = eng.hub_asked(ch, mode) if ch else False
                if mode == "subscribe":
                    ok_mode = asked and eng.monitored(ch)
                elif mode == "unsubscribe":
                    ok_mode = asked and not eng.monitored(ch)    # không để lệnh huỷ cũ cắt đăng ký mới
                else:
                    ok_mode = False
                if not ok_mode:
                    why = ("không đọc được Channel ID trong hub.topic" if not ch else
                           "tool chưa từng gửi yêu cầu '" + mode + "' cho kênh này (hoặc đã quá 6 giờ)" if not asked else
                           "kênh không còn trong Excel" if mode == "subscribe" else
                           "kênh vẫn đang theo dõi → từ chối lệnh huỷ cũ" if mode == "unsubscribe" else f"mode lạ '{mode}'")
                    eng.hub_event(ch or q.get("hub.topic", "")[:80], "verify", mode, 404, 0, f"{why} | {src}")
                    eng.log(f"🚫 XÁC MINH BỊ TỪ CHỐI | kênh {ch} | mode={mode} | lý do: {why}")
                    return self._reply(404)
                if len(q["hub.challenge"]) <= 512:
                    if mode == "subscribe":
                        try:
                            lease = int(q.get("hub.lease_seconds") or eng.cfg.get("lease_seconds", 432000))
                        except ValueError:
                            lease = int(eng.cfg.get("lease_seconds", 432000))
                        lease = max(60, min(lease, int(eng.cfg.get("lease_seconds", 432000)), 10 * 86400))
                        eng.db.x("INSERT INTO subs(channel_id, callback, verified_at, lease_seconds, state) VALUES(?,?,?,?,'VERIFIED') "
                                 "ON CONFLICT(channel_id) DO UPDATE SET verified_at=excluded.verified_at, callback=excluded.callback, "
                                 "lease_seconds=excluded.lease_seconds, state='VERIFIED', error=NULL, attempts=0, "
                                 "verified_secret_fp=subs.secret_fp",
                                 (ch, eng.callback, time.time(), lease))
                        sim = (ch, mode) in eng.sim_pending
                        eng.hub_event(ch, "verify-sim" if sim else "verify", mode, 200, 1,
                                      f"GET XÁC MINH THẬT, đã trả đúng hub.challenge | lease {lease}s | {src}")
                        eng.hub_accepted(ch, mode)
                        if sim:
                            eng.log(f"🧪 (GIẢ LẬP SELFTEST) webhook trả đúng challenge cho kênh {ch} — KHÔNG phải hub Google xác minh")
                        else:
                            eng.log(f"✅ Hub XÁC MINH kênh {ch} | lease {lease}s")
                    else:
                        eng.db.x("UPDATE subs SET state='UNSUBSCRIBED' WHERE channel_id=?", (ch,))
                        eng.hub_event(ch, "verify", mode, 200, 1, "huỷ")
                        eng.hub_accepted(ch, mode)
                    return self._reply(200, q["hub.challenge"].encode())   # giữ pending: hub có thể gửi lại xác minh
                eng.hub_event(ch or "", "verify", mode, 404, 0, "hub.challenge quá dài")
                return self._reply(404)

            def do_POST(self):
                t_recv = time.perf_counter_ns()
                if urlparse(self.path).path != path:
                    return self._reply(404)
                raw = self._read_body()
                if raw is None:
                    return
                src = eng.describe_source(self.client_address[0], self.headers)
                gen = (parse_qs(urlparse(self.path).query).get("g") or [""])[0]
                verdict = eng.check_signature(raw, self.headers.get("X-Hub-Signature"))
                verdict["gen"] = gen
                self._reply(204)                              # ACK ngay — xử lý sau, không bắt hub chờ
                eng.on_signed_post(raw, verdict, src, self.headers, t_recv)

            def _read_body(self):
                """Đọc ĐÚNG byte thô hub gửi (HMAC tính trên đây). Hỗ trợ Content-Length và chunked; tối đa 256 KB."""
                te = (self.headers.get("Transfer-Encoding") or "").lower()
                if "chunked" in te:
                    buf = bytearray()
                    while True:
                        line = self.rfile.readline(64)
                        try:
                            size = int(line.split(b";")[0].strip(), 16)
                        except ValueError:
                            self._reply(400)
                            return None
                        if size == 0:
                            self.rfile.readline(64)
                            break
                        if len(buf) + size > 256 * 1024:
                            self._reply(413)
                            return None
                        buf += self.rfile.read(size)
                        self.rfile.readline(8)
                    return bytes(buf)
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = -1
                if n <= 0 or n > 256 * 1024:                  # push thật chỉ vài KB
                    self._reply(413 if n > 0 else 400)
                    return None
                return self.rfile.read(n)

        slots = threading.BoundedSemaphore(32)

        def _hard_close(sock):
            try:
                import socket as _s
                sock.shutdown(_s.SHUT_RDWR)
            except OSError:
                pass

        class Srv(ThreadingHTTPServer):
            daemon_threads = True

            def process_request(self, request, client_address):      # tối đa 32 kết nối đồng thời
                if not slots.acquire(blocking=False):
                    self.shutdown_request(request)
                    return
                super().process_request(request, client_address)

            def process_request_thread(self, request, client_address):
                killer = threading.Timer(15.0, lambda: _hard_close(request))   # hạn TỔNG 15 s / request
                killer.daemon = True
                killer.start()
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    killer.cancel()
                    slots.release()

        self.httpd = Srv((self.cfg.get("listen_host", "0.0.0.0"), int(self.cfg.get("listen_port", 8765))), H)
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True, name="WEBHOOK").start()
        self.stats["webhook"] = f"LOCAL :{self.httpd.server_address[1]}{path}"
        self.log(f"📡 Webhook lắng nghe {self.cfg.get('listen_host')}:{self.httpd.server_address[1]}{path}")

    def describe_source(self, ip, headers) -> str:
        xff = (headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        ua = (headers.get("User-Agent") or "-")[:60]
        if xff:
            return f"qua tunnel (ngrok) từ {xff} | UA {ua}"
        if ip in ("127.0.0.1", "::1"):
            return f"localhost (SELFTEST/thử tay) | UA {ua}"
        return f"trực tiếp từ {ip} | UA {ua}"

    def check_signature(self, raw: bytes, header) -> dict:
        """HMAC trên BYTE THÔ của body, so sánh bằng hmac.compare_digest. Trả về kết quả + thông tin chẩn đoán an toàn."""
        sig = str(header or "").strip()
        out = {"ok": False, "which": None, "algo": None, "sig_hint": "", "len": len(raw), "reason": ""}
        if not sig:
            out["reason"] = "THIẾU header X-Hub-Signature"
            return out
        algo, _, hexs = sig.partition("=")
        algo = algo.strip().lower()
        hexs = hexs.strip().lower()
        out["algo"] = algo
        out["sig_hint"] = (hexs[:6] + "…") if hexs else ""
        if algo not in ("sha1", "sha256", "sha384", "sha512") or not re.fullmatch(r"[0-9a-f]+", hexs or ""):
            out["reason"] = f"header sai định dạng (thuật toán '{algo[:10]}')"
            return out

        def match(sec):
            return hmac.compare_digest(hmac.new(sec.encode(), raw, getattr(hashlib, algo)).hexdigest(), hexs)
        if match(self.secret):
            out.update(ok=True, which="current")
            return out
        for old, retired_at in load_secret_history(self.base):
            # secret CŨ do chính tool tạo: chỉ chấp nhận trong thời hạn lease tối đa sau khi bị thay (hub chưa chuyển xong)
            if time.time() - retired_at <= 10 * 86400 and match(old):
                out.update(ok=True, which=f"old:{secret_fp(old)}")
                return out
        out["reason"] = (f"chữ ký {algo} KHÔNG khớp secret hiện tại ({secret_fp(self.secret)}) "
                         f"hay {len(load_secret_history(self.base))} secret cũ đã biết")
        return out

    def classify_post(self, raw, v) -> tuple[str, list, str]:
        """→ (mã, [(kênh, video)], giải thích). Mã: VALID_SIGNATURE | VALID_OLD_SECRET | MISSING_SIGNATURE |
        INVALID_SIGNATURE | UNKNOWN_CHANNEL | UNKNOWN_SUBSCRIPTION. KHÔNG bao giờ trả 'hợp lệ' khi HMAC không khớp."""
        try:
            ids = [(x["channel_id"], x["video_id"]) for x in parse_atom(raw) if x.get("video_id")][:5]
        except Exception:  # noqa: BLE001
            ids = []
        chans = sorted({c for c, _ in ids if c})
        if v["ok"]:
            if chans and not any(self.monitored(c) for c in chans):
                return "UNKNOWN_CHANNEL", ids, "chữ ký đúng nhưng kênh KHÔNG có trong cột K (thuê bao còn sót) → huỷ đăng ký"
            if v["which"] != "current":
                # PHẠM VI secret cũ: chỉ trong giai đoạn chuyển — kênh CHƯA được hub xác minh với secret hiện tại,
                # hoặc mới xác minh < 1 giờ (push đang trên đường). Ngoài phạm vi → coi là chữ ký không hợp lệ.
                cur = secret_fp(self.secret)
                for c in chans:
                    r = self.db.one("SELECT * FROM subs WHERE channel_id=?", (c,))
                    if r and r["state"] == "VERIFIED" and r["verified_secret_fp"] == cur and \
                            time.time() - (r["verified_at"] or 0) > 3600:
                        return "INVALID_SIGNATURE", ids, (f"ký bằng secret cũ {v['which']} nhưng kênh {c} đã chuyển sang "
                                                          f"secret hiện tại hơn 1 giờ → hết giai đoạn chuyển, từ chối")
                return "VALID_OLD_SECRET", ids, ""
            return "VALID_SIGNATURE", ids, ""
        if v["reason"].startswith("THIẾU"):
            return "MISSING_SIGNATURE", ids, v["reason"]
        cur_gen = secret_fp(self.secret)
        if self.cfg.get("callback_generation", True) and v.get("gen", None) is not None and v.get("gen") != cur_gen \
                and any(self.monitored(c) for c in chans):
            g = v.get("gen") or "(không có — đăng ký từ bản cũ, trước khi có thế hệ callback)"
            return "UNKNOWN_SUBSCRIPTION", ids, (f"{v['reason']}; POST tới callback THẾ HỆ CŨ g={g} (hiện tại g={cur_gen}) → "
                                                  "thuê bao cũ với secret cũ, sẽ tự hết hạn; thuê bao mới đang dùng callback hiện tại")
        if chans and not any(self.monitored(c) for c in chans):
            return "UNKNOWN_CHANNEL", ids, (f"{v['reason']}; kênh {chans} KHÔNG có trong {len(self.channels)} kênh cột K → "
                                             "thuê bao do bản cũ / tool khác / Excel cũ tạo, hub ký bằng secret khác → huỷ đăng ký")
        cur = secret_fp(self.secret)
        for c in chans:
            r = self.db.one("SELECT * FROM subs WHERE channel_id=?", (c,))
            if (r is None or r["state"] != "VERIFIED" or r["callback"] != self.callback
                    or (r["verified_secret_fp"] and r["verified_secret_fp"] != cur)):
                st = "chưa có bản ghi" if r is None else (f"trạng thái {r['state']}, secret xác minh "
                                                         f"{r['verified_secret_fp'] or '?'} / hiện tại {cur}")
                return "UNKNOWN_SUBSCRIPTION", ids, (f"{v['reason']}; tool CHƯA có thuê bao đã xác minh với secret hiện tại cho "
                                                      f"kênh {c} ({st}) → hub đang dùng thuê bao khác (bản cũ / DB bị ghi đè / "
                                                      "tool khác cùng callback) → đăng ký lại")
        return "INVALID_SIGNATURE", ids, (f"{v['reason']}; kênh đã có thuê bao xác minh với secret hiện tại → body bị sửa "
                                          "hoặc POST không phải từ hub của thuê bao này")

    def on_signed_post(self, raw, v, src, headers, t_recv_ns):
        code, ids, why = self.classify_post(raw, v)
        chans = sorted({c for c, _ in ids if c})
        diag = (f"nguồn: {src} | {v['algo'] or '-'} {v['sig_hint']} | body {v['len']} B | "
                f"Content-Encoding {headers.get('Content-Encoding') or '-'} | video {ids}")
        for c in (chans or [""]):
            self.hub_event(c, f"push-{code}", "", None, int(code.startswith("VALID")), f"{diag} | {why}")
        if code in ("VALID_SIGNATURE", "VALID_OLD_SECRET"):
            if code == "VALID_OLD_SECRET":
                self.log(f"🔑 VALID_OLD_SECRET ({v['which']}) — secret cũ do chính tool cấp, còn hạn → nhận; đăng ký lại {chans} "
                         f"để hub chuyển sang secret hiện tại | {diag}")
                self._resubscribe_after_badsig(chans, src, force=True)
            self.handle_push(raw, t_recv_ns)
            return
        self.log(f"🚫 {code}: {why} | {diag}")
        self._save_rejected(raw, headers, code, ids, src)
        if code == "UNKNOWN_CHANNEL":
            self._unsubscribe_foreign([c for c in chans if not self.monitored(c)])
        elif code in ("UNKNOWN_SUBSCRIPTION", "INVALID_SIGNATURE"):
            self._resubscribe_after_badsig(chans, src)

    def _save_rejected(self, raw, headers, code, ids, src):
        """Lưu NGUYÊN request bị từ chối (body thô + header chữ ký) trên máy để kiểm lại bằng --verify-post.
        Chỉ lưu cục bộ; log KHÔNG in chữ ký đầy đủ."""
        try:
            d = self.base / "logs" / "rejected_posts"
            d.mkdir(parents=True, exist_ok=True)
            vid = ids[0][1] if ids else "unknown"
            stem = f"{datetime.now():%Y%m%d_%H%M%S_%f}_{code}_{vid}"
            (d / f"{stem}.body").write_bytes(raw)
            (d / f"{stem}.json").write_text(json.dumps({
                "received_at": iso(), "code": code, "source": src, "ids": ids,
                "x_hub_signature": headers.get("X-Hub-Signature"), "content_type": headers.get("Content-Type"),
                "content_length": headers.get("Content-Length"), "user_agent": headers.get("User-Agent"),
                "x_forwarded_for": headers.get("X-Forwarded-For"), "secret_fp_at_receive": secret_fp(self.secret)},
                ensure_ascii=False, indent=1), encoding="utf-8")
            files = sorted(d.glob("*.json"))
            for old in files[:-200]:                       # giữ 200 request gần nhất
                old.unlink(missing_ok=True)
                old.with_suffix(".body").unlink(missing_ok=True)
        except OSError:
            pass

    def _unsubscribe_foreign(self, chans):
        now = time.time()
        for c in chans:
            if now - self._badsig_resub.get("unsub:" + c, 0) < 3600 or not self.callback:
                continue
            self._badsig_resub["unsub:" + c] = now
            self.log(f"🧹 Huỷ thuê bao còn sót cho kênh {c} (không có trong cột K) để hub ngừng gửi")
            threading.Thread(target=self._unsubscribe, args=(c,), daemon=True, name="UNSUB-FOREIGN").start()

    def _resubscribe_after_badsig(self, chans, src, force=False):
        """Push THẬT (qua tunnel) mang chữ ký không khớp → hub đang giữ secret khác cho kênh đó → đăng ký lại (≤1 lần/10 phút/kênh)."""
        if not chans or not self.callback or ("tunnel" not in src and not force):
            return
        now = time.time()
        todo = [c for c in chans if self.monitored(c) and now - self._badsig_resub.get(c, 0) > 600]
        for c in todo:
            self._badsig_resub[c] = now
        if todo:
            threading.Thread(target=self.subscribe_all, args=(todo,), daemon=True, name="RESUB-BADSIG").start()

    def handle_push(self, raw: bytes, t_recv_ns: int):
        try:
            entries = parse_atom(raw)
        except Exception as e:  # noqa: BLE001
            self.log(f"🚫 WebSub POST XML lỗi: {e}")
            return
        self.stats["pushes"] += 1
        for e in entries:
            if e["deleted"]:
                self.log(f"🗑️ WebSub: video {e['video_id']} bị xoá/ẩn — bỏ qua")
                continue
            ch = e["channel_id"]
            if ch:
                self.db.x("UPDATE subs SET last_push=? WHERE channel_id=?", (time.time(), ch))
            if self.inbox_db is not None and e["video_id"] not in self.sim_vids:
                self._divert_to_main(e)
                continue
            r = self.on_event(ch, e["video_id"], "WEBSUB", e["published"], e["title"], t_recv_ns)
            if r == "old":
                self.log(f"↩️ WebSub: {e['video_id']} là CẬP NHẬT video cũ (published {e['published']}) — không tải")
            elif r == "foreign":
                self.log(f"↩️ WebSub: kênh {ch} không còn trong Excel — bỏ qua")

    def _divert_to_main(self, e):
        """SELFTEST đang giữ cổng chính mà nhận PUSH THẬT → không tải ở môi trường test; ghi vào database.sqlite của tool
        chính (QUEUED + Excel PENDING) để tool chính tải + ghi check.xlsx ngay khi mở lại (recover)."""
        ch, vid = e["channel_id"], e["video_id"]
        if not VID_RE.match(vid or "") or not self.monitored(ch):
            return
        if not self.is_new_video(e["published"]):         # cùng bộ lọc như tool chính: sửa video cũ → bỏ
            self.hub_event(ch, "push-old", "", None, 0, f"{vid} (SELFTEST, không chuyển)")
            return
        if self.inbox_db.one("SELECT 1 FROM videos WHERE video_id=? UNION SELECT 1 FROM seen WHERE video_id=?", (vid, vid)):
            return                                         # tool chính đã biết video này
        pub = iso(e["published"]) if e["published"] else None
        okey = pub or iso()
        try:
            self.inbox_db.tx([
                ("INSERT OR IGNORE INTO videos(video_id, channel_id, url, title, engine, detected_at, published_at, state) "
                 "VALUES(?,?,?,?,?,?,?,?)", (vid, ch, video_url(vid), e["title"], "WEBSUB", iso(), pub, "QUEUED")),
                ("INSERT OR IGNORE INTO excel_jobs(video_id, channel_id, url, state, created_at, order_key) VALUES(?,?,?,?,?,?)",
                 (vid, ch, video_url(vid), "PENDING", iso(), okey))])
            self.log(f"📥 PUSH THẬT trong lúc SELFTEST: {vid} (kênh {ch}) → chuyển cho tool chính (sẽ tải khi mở START.bat)")
        except Exception as ex:  # noqa: BLE001
            self.log(f"❌ không chuyển được push thật {vid} cho tool chính: {type(ex).__name__}: {ex}")

    def resolve_callback(self) -> str:
        cb = str(self.cfg.get("callback_url") or "").strip()
        if not cb and requests is not None:
            try:
                r = requests.get("http://127.0.0.1:4040/api/tunnels", timeout=1.5)
                for t in r.json().get("tunnels", []):
                    pu = str(t.get("public_url") or "")
                    addr = str((t.get("config") or {}).get("addr") or "")
                    if pu.startswith("https://") and addr.endswith(str(self.cfg.get("listen_port", 8765))):
                        cb = pu
                        break
            except Exception:
                cb = ""
        if cb and urlparse(cb).path in ("", "/"):
            cb = cb.rstrip("/") + self.cfg.get("websub_path", "/websub")
        if cb and self.cfg.get("callback_generation", True) and "g=" not in urlparse(cb).query:
            # Thuê bao trên hub được định danh bởi (callback, topic). Gắn 'thế hệ' = dấu vân tay secret vào callback:
            # secret đổi ⇒ callback đổi ⇒ hub tạo thuê bao MỚI và PHẢI xác minh lại; thuê bao cũ (secret cũ) tự hết hạn
            # và POST của nó được nhận diện rõ là 'thế hệ cũ' thay vì chữ ký sai bí ẩn.
            cb = add_query(cb, {"g": secret_fp(self.secret)})
        return cb

    def verified_count(self, since=None) -> int:
        """Chỉ đếm kênh ĐANG theo dõi, đã được hub xác minh THẬT cho callback hiện tại và lease còn hạn."""
        chans = list(self.channels)
        if not chans or not self.callback:
            return 0
        q = (f"SELECT COUNT(*) c FROM subs WHERE state='VERIFIED' AND callback=? AND verified_at + lease_seconds > ? "
             f"AND channel_id IN ({','.join('?' * len(chans))})" + (" AND verified_at > ?" if since else ""))
        args = [self.callback, time.time(), *chans] + ([since] if since else [])
        return self.db.one(q, args)["c"]

    def hub_event(self, ch, kind, mode, http, accepted, detail=""):
        try:
            self.db.x("INSERT INTO hub_events(at, channel_id, kind, mode, http, accepted, detail) VALUES(?,?,?,?,?,?,?)",
                      (time.time(), ch, kind, mode, http, accepted, str(detail)[:900]))
        except Exception:
            pass

    def hub_asked(self, ch, mode) -> bool:
        """Chỉ chấp nhận xác minh trong 30 phút sau khi tool THỰC SỰ gửi yêu cầu; sau khi đã chấp nhận 1 lần,
        chỉ cho hub gửi lại thêm trong 2 phút (phản hồi đầu bị mất) → challenge giả mạo/phát lại về sau bị từ chối."""
        with self.hub_lock:
            t = self.pending_hub.get((ch, mode))
            ok_at = self.pending_ok.get((ch, mode))
        if not t:
            return False
        if ok_at:
            return time.time() - ok_at < 120
        return time.time() - t < 1800

    def hub_accepted(self, ch, mode):
        with self.hub_lock:
            self.pending_ok.setdefault((ch, mode), time.time())

    def hub_done(self, ch, mode):
        with self.hub_lock:
            self.pending_hub.pop((ch, mode), None)

    def _hub(self, mode, ch, cb, verify="async", timeout=(3, 8)):
        with self.hub_lock:
            self.pending_hub[(ch, mode)] = time.time()
            self.pending_ok.pop((ch, mode), None)
            self.pending_hub.pop((ch, "unsubscribe" if mode == "subscribe" else "subscribe"), None)
        data = {"hub.mode": mode, "hub.topic": TOPIC_FMT.format(ch), "hub.callback": cb, "hub.verify": verify}
        if mode == "subscribe":
            data["hub.secret"] = self.secret
            data["hub.lease_seconds"] = str(int(self.cfg.get("lease_seconds", 432000)))
        # bản ghi CHÍNH XÁC yêu cầu gửi hub (secret chỉ ghi dấu vân tay + độ dài)
        self.last_request = {"hub_url": self.cfg["hub_url"], "hub.mode": mode, "hub.topic": data["hub.topic"],
                             "hub.callback": cb, "hub.verify": verify, "hub.lease_seconds": data.get("hub.lease_seconds"),
                             "hub.secret": (f"fp {secret_fp(self.secret)}, {len(self.secret)} ký tự" if mode == "subscribe" else None)}
        r = requests.post(self.cfg["hub_url"], data=data, timeout=timeout)
        return r.status_code, scrub(r.text, self.secret)[:300]

    def subscribe_all(self, chans=None):
        cb = self.callback
        if not cb:
            return 0
        ok = 0
        for ch in (chans if chans is not None else list(self.channels)):
            if self.stop_ev.is_set():
                break
            try:
                code, body = self._hub("subscribe", ch, cb)
                st = "REQUESTED" if code in (202, 204) else "ERROR"
                self.db.x("INSERT INTO subs(channel_id, callback, requested_at, state, error, attempts, last_http, secret_fp) "
                          "VALUES(?,?,?,?,?,1,?,?) "
                          "ON CONFLICT(channel_id) DO UPDATE SET callback=excluded.callback, requested_at=excluded.requested_at, "
                          "secret_fp=excluded.secret_fp, "
                          "state=CASE WHEN subs.state='VERIFIED' AND subs.callback=excluded.callback THEN 'VERIFIED' ELSE excluded.state END, "
                          "error=excluded.error, attempts=COALESCE(subs.attempts,0)+1, last_http=excluded.last_http",
                          (ch, cb, time.time(), st, None if st != "ERROR" else f"HTTP {code} {body}", code, secret_fp(self.secret)))
                self.hub_event(ch, "subscribe", "subscribe", code, int(st != "ERROR"),
                               (f"HTTP {code} = hub NHẬN yêu cầu (chưa xác minh)" if st != "ERROR" else f"HTTP {code} {body[:200]}") +
                               f" | gửi: {json.dumps(self.last_request, ensure_ascii=False)}")
                if st == "ERROR":
                    self.log(f"❌ Hub trả HTTP {code} khi đăng ký kênh {ch}: {body[:120]}")
                ok += st != "ERROR"
            except Exception as e:  # noqa: BLE001
                self.log(f"❌ Gửi đăng ký kênh {ch} tới hub lỗi mạng: {type(e).__name__}: {str(e)[:120]} → sẽ gửi lại theo lịch")
                self.hub_event(ch, "subscribe", "subscribe", None, 0, f"{type(e).__name__}: {str(e)[:150]}")
                self.db.x("INSERT INTO subs(channel_id, callback, requested_at, state, error, attempts, last_http) VALUES(?,?,?,?,?,1,NULL) "
                          "ON CONFLICT(channel_id) DO UPDATE SET error=excluded.error, requested_at=excluded.requested_at, "
                          "attempts=COALESCE(subs.attempts,0)+1, last_http=NULL, "
                          "state=CASE WHEN subs.state='VERIFIED' AND subs.callback=excluded.callback "
                          "AND subs.verified_at + subs.lease_seconds > excluded.requested_at THEN 'VERIFIED' ELSE 'ERROR' END",
                          (ch, cb, time.time(), "ERROR", f"{type(e).__name__}"))
            self.stop_ev.wait(0.05)
        return ok

    def _unsubscribe(self, ch):
        if self.callback and requests is not None:
            try:
                self._hub("unsubscribe", ch, self.callback)
            except Exception:
                pass
        self.db.x("UPDATE subs SET state='REMOVED' WHERE channel_id=?", (ch,))

    def _websub_subscriber(self):
        last_cb = self.db.meta_get("websub_callback", "")
        while not self.stop_ev.is_set():
            cb = self.resolve_callback()
            if not cb:
                self.stats["webhook"] = "CHƯA CÓ URL CÔNG KHAI (chạy ngrok hoặc điền callback_url)"
                self.stop_ev.wait(min(15.0, float(self.cfg.get("websub_loop_sec", 30))))
                continue
            sec_sig = hashlib.sha256(self.secret.encode()).hexdigest()[:16]
            if cb != self.callback:
                self.callback = cb
                sec_changed = self.db.meta_get("subs_secret_sig") not in (None, sec_sig)
                if cb != last_cb or sec_changed:
                    why = "secret HMAC đổi (hub có thể đang giữ secret cũ)" if cb == last_cb else \
                        f"callback {'đổi' if last_cb else 'mới'}: {cb}"
                    self.log(f"🔁 WebSub: {why} → đăng ký lại TOÀN BỘ kênh")
                    self.db.x("UPDATE subs SET state='STALE'")
                self.db.meta_set("websub_callback", cb)
                self.db.meta_set("subs_secret_sig", sec_sig)
                last_cb = cb
                n = self.subscribe_all()
                self.log(f"📡 WebSub: gửi đăng ký {n}/{len(self.channels)} kênh (HTTP 202 = hub NHẬN YÊU CẦU, chưa phải đã xác minh)")
            # gia hạn: đã xác minh và quá 80% lease, hoặc yêu cầu >10 phút mà hub chưa gọi xác minh
            now = time.time()
            renew = []
            for r in self.db.all("SELECT * FROM subs"):
                if r["channel_id"] not in self.channels:
                    continue
                lease = r["lease_seconds"] or 432000
                since_req = now - (r["requested_at"] or 0)
                if r["state"] == "VERIFIED" and r["verified_at"] and now > r["verified_at"] + lease:
                    self.db.x("UPDATE subs SET state='EXPIRED' WHERE channel_id=?", (r["channel_id"],))
                    renew.append(r["channel_id"])
                elif r["state"] == "VERIFIED" and r["verified_at"] and now > r["verified_at"] + 0.8 * lease and since_req > 600:
                    renew.append(r["channel_id"])          # gia hạn ở 80% lease, không gửi lại quá 1 lần / 10 phút
                elif r["state"] in ("REQUESTED", "ERROR", "STALE", "EXPIRED", "UNSUBSCRIBED", "REMOVED", "DENIED"):
                    sched = list(self.cfg.get("verify_retry_sec") or [120, 300, 600, 1800, 3600])
                    k = max(0, int(r["attempts"] or 1) - 1)
                    wait = sched[min(k, len(sched) - 1)]
                    if since_req > wait:
                        self.log(f"🔁 Kênh {r['channel_id']} chưa được hub xác minh sau {since_req:.0f}s (trạng thái {r['state']}, "
                                 f"HTTP lần gửi {r['last_http']}, lần {r['attempts']}) → gửi lại")
                        renew.append(r["channel_id"])
            missing = [c for c in self.channels if not self.db.one("SELECT 1 FROM subs WHERE channel_id=?", (c,))]
            if renew or missing:
                self.subscribe_all(renew + missing)
            if getattr(self, "channels_loaded", False) and self.channels:   # chỉ khi đã đọc Excel THÀNH CÔNG
                for r in self.db.all("SELECT channel_id, state FROM subs WHERE state NOT IN ('REMOVED','UNSUBSCRIBED')"):
                    if r["channel_id"] not in self.channels:          # kênh đã rời cột K (kể cả lúc tool tắt)
                        self._unsubscribe(r["channel_id"])
            self.stats["websub_verified"] = self.verified_count()
            self.stats["webhook"] = f"{self.callback}"
            self.stop_ev.wait(float(self.cfg.get("websub_loop_sec", 30)))

    def _ff_keepwarm(self):
        """Windows nạp ffprobe/ffmpeg (DLL + quét antivirus) lần đầu ~0.3–0.6 s. Chạy '-version' lúc khởi động
        và mỗi 20 s khi rảnh để lần kiểm tra file thật luôn 'nóng' (cùng kỹ thuật prewarm của V9.4)."""
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        first = True
        while not self.stop_ev.is_set():
            if first or not self.active:
                for exe in (FFPROBE, FFMPEG):
                    if exe:
                        t0 = time.perf_counter_ns()
                        err = ""
                        try:
                            pr = subprocess.run([exe, "-hide_banner", "-version"], capture_output=True, timeout=10, creationflags=flags)
                            if pr.returncode != 0:
                                err = f"mã lỗi {pr.returncode}"
                        except Exception as e:  # noqa: BLE001
                            err = f"{type(e).__name__}: {e}"
                        if first:
                            self.log(f"🔥 làm nóng {exe}: {ms(t0, time.perf_counter_ns()):.0f} ms" +
                                     (f" | ❌ KHÔNG CHẠY ĐƯỢC ({err}) — video cần ghép hình+tiếng sẽ lỗi" if err else ""))
                first = False
            self.stop_ev.wait(20)

    def _stats_loop(self):
        n = 0
        while not self.stop_ev.wait(1.0):
            n += 1
            if n % 15 == 0:
                try:
                    self.db.meta_set("last_alive", time.time())     # đo thời gian tắt cho cửa sổ 'video mới'
                except Exception:
                    pass
            if n % 600 == 0:                                         # bỏ dữ liệu race cũ hơn 3 giờ khỏi RAM
                lim = time.time() - 10800
                with self.seen_lock:
                    for v in [v for v, r in self.race.items() if max(r["wall"].values() or [0]) < lim]:
                        self.race.pop(v, None)
            if self.on_stats:
                try:
                    s = dict(self.stats)
                    s["channels"] = len(self.channels)
                    br = getattr(self, "bridge", None)
                    if br is not None:
                        s["tiktok"] = br.snapshot()
                    if self.api is not None:
                        s["api"] = self.api.snapshot()
                    self.on_stats(s)
                except Exception:
                    pass


# =============================== TÍCH HỢP TIKTOK (TAM) =============================== #
def attach_tiktok(engine, cfg, log, tam=None):
    """Gắn cầu nối TAM vào engine: DOWNLOAD_DONE → job TikTok đúng profile. Gọi TRƯỚC engine.start()."""
    sys.path.insert(0, str(BASE_DIR))
    from ws360_tam_bridge import TikTokBridge
    tp = Path(cfg.get("tam_path") or "tam_INSTANT_POST.py")
    if not tp.is_absolute():
        tp = BASE_DIR / tp
    cfg = dict(cfg, tam_path=str(tp))
    br = TikTokBridge(engine, cfg, log, tam=tam)
    engine.bridge = br
    engine.tiktok_on = True
    engine.on_done = br.submit
    engine.on_excel_reload = br.reload_mapping
    return br


# =============================== GIAO DIỆN PYSIDE6 =============================== #
def run_gui(cfg, base):
    from PySide6.QtCore import QObject, Signal, Qt
    from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
                                   QPlainTextEdit, QCheckBox, QComboBox, QSpinBox, QDoubleSpinBox, QTableWidget,
                                   QTableWidgetItem)

    class Bridge(QObject):
        line = Signal(str)
        stats = Signal(dict)
        stopped = Signal()
        started = Signal()

    app = QApplication.instance() or QApplication(sys.argv)
    w = QWidget()
    w.setWindowTitle(f"YOUTUBE WEBSUB INSTANT 360P — {VERSION}")
    w.resize(1000, 720)
    lay = QVBoxLayout(w)
    top = QHBoxLayout()
    b_start, b_stop = QPushButton("▶ START"), QPushButton("⏹ STOP")
    b_stop.setEnabled(False)
    top.addWidget(b_start)
    top.addWidget(b_stop)
    chk = {}
    for k, label in (("download", "Auto Download 360p"), ("excel", "Ghi Excel")):
        c = QCheckBox(label)
        c.setChecked(bool(cfg.get(f"{k}_enabled", True)))
        chk[k] = c
        top.addWidget(c)
    top.addStretch(1)
    lay.addLayout(top)
    # ---- hàng NGUỒN PHÁT HIỆN (RACE MODE) ----
    det = QHBoxLayout()
    cb_det = QComboBox()
    det_labels = {"WEBSUB_ONLY": "WEBSUB ONLY", "API_ONLY": "API ONLY", "PARALLEL": "WEBSUB + API PARALLEL"}
    for k, t in det_labels.items():
        cb_det.addItem(t, k)
    cb_det.setCurrentIndex(list(det_labels).index(norm_mode(cfg.get("detection_mode", "PARALLEL"))))
    sp_iv = QDoubleSpinBox()
    sp_iv.setRange(0.2, 600.0)
    sp_iv.setDecimals(1)
    sp_iv.setSuffix(" s")
    sp_iv.setValue(float(cfg.get("api_min_interval_sec", 1.0)))
    sp_iv.setToolTip("Nhịp NHANH NHẤT mỗi kênh. Tool tự giãn theo quota thật (không bao giờ vượt quota).")
    kf = Path(cfg.get("api_keys_file") or "api_keys.txt")
    lb_keys = QLabel(f"File key: {kf.name}" + ("" if (kf if kf.is_absolute() else base / kf).exists() else " (CHƯA CÓ)"))
    for wdg in (QLabel("Nguồn phát hiện:"), cb_det, QLabel("API nhanh nhất:"), sp_iv, lb_keys):
        det.addWidget(wdg)
    det.addStretch(1)
    lay.addLayout(det)
    # ---- hàng TikTok (TAM) ----
    tt = QHBoxLayout()
    c_tt = QCheckBox("Tự đăng TikTok")
    c_tt.setChecked(bool(cfg.get("tiktok_enabled")))
    c_dry = QCheckBox("Chạy thử (KHÔNG bấm Post)")
    c_dry.setChecked(bool(cfg.get("tiktok_dry_run")))
    cb_mode = QComboBox()
    cb_mode.addItems(["PREWARM_ALL", "SMART_PREWARM"])
    cb_mode.setCurrentText(str(cfg.get("prewarm_mode", "PREWARM_ALL")).upper())
    sp_open = QSpinBox()
    sp_open.setRange(1, 8)
    sp_open.setValue(int(cfg.get("open_concurrency", 2)))
    b_reset = QPushButton("Đã kiểm tra profile UNKNOWN")
    for wdg in (c_tt, c_dry, QLabel("Mở profile:"), cb_mode, QLabel("Mở cùng lúc:"), sp_open, b_reset):
        tt.addWidget(wdg)
    tt.addStretch(1)
    lay.addLayout(tt)
    grid = QGridLayout()
    labels = {}
    names = [("channels", "Tổng kênh"), ("websub_verified", "WebSub đã xác minh"), ("webhook", "Webhook"),
             ("pushes", "Push WebSub nhận"), ("detected", "Video mới"), ("downloading", "Đang tải"),
             ("done", "Tải xong"), ("failed", "Lỗi tải"), ("speed_mbps", "Tốc độ (Mbps)"), ("resolver_ms", "Resolver (ms)"),
             ("download_ms", "Tải (ms)"), ("mux_ms", "Mux (ms)"), ("total_ms", "Tổng (ms)"), ("excel", "Excel"),
             ("mode", "Chế độ phát hiện"), ("wins", "Thắng: WebSub / API"), ("api_status", "API"),
             ("api_quota", "API_QUOTA_USED"), ("api_interval", "Nhịp quét API"), ("api_polls", "API gọi / lỗi / ms"),
             ("tt_genlogin", "Genlogin/Selenium"), ("tt_opened", "Profile đã mở"), ("tt_ready", "Profile READY"),
             ("tt_waiting", "Job chờ đăng"), ("tt_uploading", "Đang đăng"), ("tt_confirmed", "POST_CONFIRMED"),
             ("tt_bad", "UNKNOWN/FAILED"), ("tt_held", "Giữ lại (thiếu profile/file)"), ("tt_last", "Lần đăng gần nhất")]
    for i, (k, t) in enumerate(names):
        grid.addWidget(QLabel(t + ":"), i // 3, (i % 3) * 2)
        lb = QLabel("-")
        lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
        labels[k] = lb
        grid.addWidget(lb, i // 3, (i % 3) * 2 + 1)
    lay.addLayout(grid)
    table = QTableWidget(0, 4)
    table.setHorizontalHeaderLabels(["Profile", "Trạng thái", "Job chờ", "Ghi chú"])
    table.setMaximumHeight(170)
    lay.addWidget(table)
    logw = QPlainTextEdit()
    logw.setReadOnly(True)
    logw.setMaximumBlockCount(5000)
    lay.addWidget(logw, 1)

    log = Log(base / "logs")
    br = Bridge()
    br.line.connect(logw.appendPlainText)
    log.sinks.append(br.line.emit)
    state = {"eng": None}

    def show(s):
        last = s.get("last") or {}
        t = s.get("tiktok") or {}
        tl = t.get("last") or {}
        extra = {"tt_genlogin": t.get("genlogin", "TẮT"), "tt_opened": t.get("opened"), "tt_ready": t.get("ready"),
                 "tt_waiting": t.get("waiting"), "tt_uploading": t.get("uploading"), "tt_confirmed": t.get("confirmed"),
                 "tt_bad": t.get("unknown_failed"), "tt_held": t.get("held"),
                 "tt_last": (f"{tl.get('vid')} {tl.get('result')} | DONE→gắn {tl.get('done_to_attach_ms')} ms | "
                             f"WebSub→xác nhận {tl.get('websub_to_confirm_ms')} ms") if tl else None}
        a = s.get("api") or {}
        extra.update({"mode": det_labels.get(s.get("mode"), s.get("mode")),
                      "wins": f"{s.get('wins_websub', 0)} / {s.get('wins_api', 0)}",
                      "api_status": (f"{a.get('status')} | {a.get('keys')} key / {a.get('projects')} project | "
                                     f"phát hiện {a.get('detected')}") if a else "TẮT",
                      "api_quota": a.get("quota"), "api_interval": (f"{a.get('interval')}" + (
                          f" | giờ vàng {a.get('hot_interval')}" if a.get("hot_interval") not in (None, "-") else "")) if a else None,
                      "api_polls": f"{a.get('polls')} / {a.get('errors')} / {a.get('last_ms')}" if a else None})
        for k in labels:
            v = extra[k] if k in extra else s.get(k, last.get(k))
            if k == "excel":
                v = f"OK {s.get('excel_ok', 0)} | chờ {s.get('excel_pending', 0)} | lỗi {s.get('excel_err', 0)}"
            labels[k].setText("-" if v is None else str(v))
        prof = t.get("profiles") or []
        table.setRowCount(len(prof))
        for i, r in enumerate(prof):
            for j, key in enumerate(("pid", "state", "queue", "reason")):
                table.setItem(i, j, QTableWidgetItem(str(r.get(key, ""))))
    br.stats.connect(show)

    def start():
        for k, c in chk.items():
            cfg[f"{k}_enabled"] = c.isChecked()
        cfg["tiktok_enabled"] = c_tt.isChecked()
        cfg["tiktok_dry_run"] = c_dry.isChecked()
        cfg["prewarm_mode"] = cb_mode.currentText()
        cfg["open_concurrency"] = sp_open.value()
        cfg["detection_mode"] = cb_det.currentData()
        cfg["api_min_interval_sec"] = sp_iv.value()
        cb_det.setEnabled(False)
        sp_iv.setEnabled(False)
        e = Engine(cfg, base, log, on_stats=br.stats.emit)
        state["eng"] = e
        if cfg["tiktok_enabled"]:
            try:
                attach_tiktok(e, cfg, log)
            except Exception as ex:  # noqa: BLE001
                log(f"❌ Không bật được đăng TikTok: {type(ex).__name__}: {ex} — WS360 vẫn chạy bình thường")
        for k, c in chk.items():
            c.toggled.connect(lambda v, k=k: e.flags.__setitem__(k, bool(v)))
        b_start.setEnabled(False)
        b_stop.setEnabled(False)                         # STOP chỉ bật khi start() đã xong hẳn

        def _bg():
            try:
                e.start()
                if getattr(e, "bridge", None) is not None:
                    threading.Thread(target=e.bridge.start, daemon=True, name="TT-START").start()   # không chặn WebSub
                br.started.emit()
            except Exception as ex:  # noqa: BLE001 — báo lỗi lên cửa sổ log, không im lặng
                log(f"❌ KHÔNG khởi động được: {type(ex).__name__}: {ex}")
                br.stopped.emit()
        threading.Thread(target=_bg, daemon=True).start()

    def stop():
        # START chỉ bật lại khi engine cũ đã dừng HẲN (mọi worker xong) → không bao giờ 2 engine cùng 1 database
        b_stop.setEnabled(False)
        b_start.setEnabled(False)
        eng = state["eng"]

        def _bg():
            try:
                if eng:
                    eng.stop()
            finally:
                br.stopped.emit()
        threading.Thread(target=_bg, daemon=True).start()
    br.stopped.connect(lambda: (b_start.setEnabled(True), b_stop.setEnabled(False), cb_det.setEnabled(True), sp_iv.setEnabled(True)))
    br.started.connect(lambda: b_stop.setEnabled(True))
    b_start.clicked.connect(start)
    b_stop.clicked.connect(stop)

    def reset_unknown():
        eng = state["eng"]
        bb = getattr(eng, "bridge", None) if eng else None
        if not bb:
            return
        for r in (bb.snapshot().get("profiles") or []):
            if r["state"] == "UNKNOWN":
                bb.reset_unknown(r["pid"])
                log(f"✅ Profile {r['pid']}: bạn xác nhận đã kiểm tra → đăng tiếp")
    b_reset.clicked.connect(reset_unknown)
    w.show()
    return app.exec()


# =============================== SELFTEST / BENCH =============================== #
def selftest(cfg, base, video_id=None, wait_verify=90):
    """TEST 1–3, 5 trên máy thật. Không sửa check.xlsx gốc: TEST 3 ghi vào BẢN SAO check_TEST.xlsx."""
    log = Log(base / "logs")
    res = []

    def rec(name, status, detail):
        res.append((name, status, detail))
        log(f"{'✅' if status == 'PASS' else '⚠️' if status in ('PENDING', 'BLOCKED') else '❌'} {name}: {status} — {detail}")

    xl = base / cfg["excel_file"]
    # TEST 1
    try:
        sig0 = hashlib.sha256(xl.read_bytes()).hexdigest()
        chans, bad = ExcelBook(xl, cfg["col_channel"], cfg["col_url"], cfg.get("excel_sheet", "")).read_channels()
        same = hashlib.sha256(xl.read_bytes()).hexdigest() == sig0
        rows = sum(len(v) for v in chans.values())
        rec("TEST1 đọc Channel ID cột K", "PASS" if chans and same else "FAIL",
            f"{len(chans)} kênh duy nhất / {rows} dòng; sai định dạng {len(bad)}; file gốc không đổi={same}; mapping K→D theo từng dòng")
    except Exception as e:
        chans = {}
        rec("TEST1 đọc Channel ID cột K", "FAIL", f"{type(e).__name__}: {e}")
    # TEST 2 webhook cục bộ + công khai + hub thật
    test_xl = base / "check_TEST.xlsx"
    if xl.exists():
        shutil.copyfile(xl, test_xl)
    tcfg = dict(cfg, excel_file=test_xl.name, download_dir="DOWNLOADS_TEST")   # không đụng thư mục/file tạm của tool chính
    main_busy = not port_free(cfg.get("listen_host", "0.0.0.0"), cfg.get("listen_port", 8765))
    if main_busy:
        log(f"ℹ️ Cổng {cfg.get('listen_port', 8765)} đang bận (tool chính đang chạy?) → SELFTEST dùng cổng tạm, "
            "bỏ qua bài test hub thật")
        tcfg["listen_port"] = 0
    db_test = base / "database_TEST.sqlite"
    for p in (db_test, Path(str(db_test) + "-wal"), Path(str(db_test) + "-shm")):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
    eng = Engine(tcfg, base, log, db_name=db_test.name, lock_name="ws360_selftest.lock")
    try:
        eng.start(websub=False)          # KHÔNG chạy vòng đăng ký nền: chỉ đăng ký tường minh ở TEST2c (khi tool chính tắt)
        eng.start_webhook()
        if not main_busy:
            eng.inbox_db = DB(base / "database.sqlite")   # push THẬT tới trong lúc test → chuyển cho tool chính
    except Exception as e:  # noqa: BLE001
        rec("SELFTEST khởi động", "FAIL", f"{type(e).__name__}: {e}")
        _write_selftest_report(base, res, log)
        return res
    try:
        port = eng.httpd.server_address[1]
        ch0 = next(iter(chans), None)
        import urllib.request
        ok_local = False
        if ch0:
            with eng.hub_lock:
                eng.pending_hub[(ch0, "subscribe")] = time.time()     # mô phỏng: tool vừa gửi yêu cầu đăng ký
            eng.sim_pending.add((ch0, "subscribe"))
            q = urlencode({"hub.mode": "subscribe", "hub.topic": TOPIC_FMT.format(ch0), "hub.challenge": "xyz123", "hub.lease_seconds": "60"})
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{cfg['websub_path']}?{q}", timeout=3) as r:
                ok_local = r.read() == b"xyz123"
        rec("TEST2a webhook cục bộ trả hub.challenge", "PASS" if ok_local else "FAIL", f"cổng {port}")
        cb = "" if main_busy else eng.resolve_callback()
        if main_busy:
            rec("TEST2b callback HTTPS công khai", "BLOCKED", "tool chính đang giữ cổng — tắt START.bat rồi chạy lại SELFTEST.bat")
            rec("TEST2c đăng ký hub thật + xác minh", "BLOCKED", "như trên")
        elif not cb:
            rec("TEST2b callback HTTPS công khai", "BLOCKED", "chưa có ngrok/cloudflared hoặc callback_url trong config.json")
            rec("TEST2c đăng ký hub thật + xác minh", "BLOCKED", "cần callback công khai")
        else:
            try:
                with eng.hub_lock:
                    eng.pending_hub[(ch0, "subscribe")] = time.time()
                    eng.pending_ok.pop((ch0, "subscribe"), None)
                eng.sim_pending.add((ch0, "subscribe"))
                q = urlencode({"hub.mode": "subscribe", "hub.topic": TOPIC_FMT.format(ch0), "hub.challenge": "pub456"})
                req = urllib.request.Request(cb + ("&" if "?" in cb else "?") + q, headers={"ngrok-skip-browser-warning": "1"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    okp = r.read() == b"pub456"
                rec("TEST2b callback HTTPS công khai", "PASS" if okp and cb.startswith("https://") else "FAIL", cb)
            except Exception as e:
                rec("TEST2b callback HTTPS công khai", "FAIL", f"{cb}: {type(e).__name__}: {e}")
            eng.sim_pending.clear()                       # từ đây chỉ còn xác minh THẬT từ hub
            eng.db.x("DELETE FROM subs")                 # xoá dấu vết xác minh GIẢ LẬP của TEST2a/2b trước khi đo thật
            eng.db.x("DELETE FROM hub_events")
            eng.callback = cb
            t_send = time.time()
            n = eng.subscribe_all()
            t_end = time.time() + wait_verify
            while time.time() < t_end:
                if eng.verified_count(since=t_send) >= len(chans):
                    break
                time.sleep(2)
            v = eng.verified_count(since=t_send)        # chỉ xác minh THẬT từ hub, sau lần gửi này
            lease = eng.db.one("SELECT MIN(lease_seconds) a, MAX(lease_seconds) b FROM subs WHERE state='VERIFIED' AND verified_at > ?",
                               (t_send,))
            rec("TEST2c đăng ký hub thật + xác minh", "PASS" if v == len(chans) else ("FAIL" if v == 0 else "PARTIAL"),
                f"gửi {n}/{len(chans)} (HTTP 202) | hub GỌI XÁC MINH {v}/{len(chans)} | lease {lease['a']}–{lease['b']}s")
            for chx in chans:                                         # từng kênh CHƯA xác minh: in đủ bằng chứng
                rr = eng.db.one("SELECT * FROM subs WHERE channel_id=?", (chx,))
                if rr and rr["state"] == "VERIFIED" and (rr["verified_at"] or 0) > t_send:
                    continue
                ev = eng.db.all("SELECT * FROM hub_events WHERE channel_id=? ORDER BY id DESC LIMIT 3", (chx,))
                hd = hub_details(cfg, cb, chx, eng.secret, save_dir=base / "logs" / "hub_details")
                rec(f"TEST2c-kênh {chx}", "FAIL",
                    f"trạng thái {rr['state'] if rr else 'không có'} | HTTP đăng ký {rr['last_http'] if rr else '-'} | "
                    f"xác minh: {[(e['kind'], e['mode'], e['http'], 'OK' if e['accepted'] else 'TỪ CHỐI', e['detail']) for e in ev] or 'hub CHƯA gọi tới callback'} | "
                    f"hub Google báo: {hub_details_short(hd)}")
        # POST hợp lệ / không hợp lệ (qua chính webhook)
        body = (f'<?xml version="1.0"?><feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
                f'xmlns="http://www.w3.org/2005/Atom"><entry><yt:videoId>AAAAAAAAAAA</yt:videoId>'
                f'<yt:channelId>{ch0}</yt:channelId><title>t</title><published>2000-01-01T00:00:00+00:00</published>'
                f'</entry></feed>').encode()
        eng.sim_vids.add("AAAAAAAAAAA")
        bad_req = urllib.request.Request(f"http://127.0.0.1:{port}{cfg['websub_path']}", data=body,
                                         headers={"X-Hub-Signature": "sha1=deadbeef", "Content-Type": "application/atom+xml"})
        with urllib.request.urlopen(bad_req, timeout=3):
            pass
        time.sleep(0.3)
        bad_ok = "AAAAAAAAAAA" not in eng.seen
        sig = hmac.new(eng.secret.encode(), body, hashlib.sha1).hexdigest()
        good = urllib.request.Request(f"http://127.0.0.1:{port}{cfg['websub_path']}", data=body,
                                      headers={"X-Hub-Signature": f"sha1={sig}", "Content-Type": "application/atom+xml"})
        with urllib.request.urlopen(good, timeout=3):
            pass
        time.sleep(0.3)
        old_ok = eng.db.one("SELECT detail FROM hub_events WHERE kind='push-old' AND detail LIKE 'AAAAAAAAAAA%'")
        bad_ok = bad_ok and bool(eng.db.one("SELECT 1 FROM hub_events WHERE kind IN ('push-UNKNOWN_SUBSCRIPTION','push-INVALID_SIGNATURE')")) and \
            not eng.db.one("SELECT 1 FROM videos WHERE video_id='AAAAAAAAAAA'")
        rec("TEST2d POST sai chữ ký bị chặn, POST đúng chữ ký (video cũ) nhận nhưng không tải", "PASS" if bad_ok and old_ok else "FAIL",
            f"chặn={bad_ok}, cập-nhật-cũ={dict(old_ok) if old_ok else None}")
        # TEST 3
        if not video_id:
            rec("TEST3 sự kiện → tải 360p + Excel song song", "PENDING", "chạy lại với --video <ID video công khai>")
        elif not ch0:
            rec("TEST3 sự kiện → tải 360p + Excel song song", "BLOCKED", "Excel không có Channel ID")
        else:
            body = (f'<?xml version="1.0"?><feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
                    f'xmlns="http://www.w3.org/2005/Atom"><entry><yt:videoId>{video_id}</yt:videoId>'
                    f'<yt:channelId>{ch0}</yt:channelId><title>SELFTEST (giả lập, không phải video mới thật)</title>'
                    f'<published>{iso()}</published></entry></feed>').encode()
            eng.sim_vids.add(video_id)
            sig = hmac.new(eng.secret.encode(), body, hashlib.sha1).hexdigest()
            rq = urllib.request.Request(f"http://127.0.0.1:{port}{cfg['websub_path']}", data=body,
                                        headers={"X-Hub-Signature": f"sha1={sig}"})
            with urllib.request.urlopen(rq, timeout=3):
                pass
            t_end = time.time() + 120
            while time.time() < t_end:
                r = eng.db.one("SELECT * FROM videos WHERE video_id=?", (video_id,))
                if r and r["state"] in ("DONE", "FAILED"):
                    break
                time.sleep(0.2)
            r = eng.db.one("SELECT * FROM videos WHERE video_id=?", (video_id,))
            x = eng.db.one("SELECT * FROM excel_jobs WHERE video_id=?", (video_id,))
            cell_ok = False
            if x and x["state"] == "DONE":
                rows = [int(a) for a in (x["rows"] or "").split(",") if a]
                cell_ok = all(ExcelBook(test_xl, cfg["col_channel"], cfg["col_url"]).read_cell(rw, cfg["col_url"]) == video_url(video_id) for rw in rows)
            fv = ffprobe_info(Path(r["file"])) if r and r["file"] else {}
            rec("TEST3 sự kiện → tải 360p + Excel song song",
                "PASS" if r and r["state"] == "DONE" and fv.get("ok") and cell_ok else "FAIL",
                f"state={r['state'] if r else None} lỗi={r['error'] if r else None} | {fv} | Excel ô D đúng={cell_ok} | "
                f"resolver_start {r['resolver_start_ms'] if r else None} ms sau khi nhận | resolver {r['resolver_ms'] if r else None} ms | "
                f"tải {r['download_ms'] if r else None} ms | mux {r['mux_ms'] if r else None} | tổng {r['total_ms'] if r else None} ms | "
                f"Excel ghi {x['write_ms'] if x else None} ms (độc lập)")
        rec("TEST6 live video mới thật", "PENDING", "chỉ xác minh được khi một kênh trong Excel đăng video trong lúc tool chạy "
            "(xem log dòng '[WEBSUB] VIDEO MỚI' + bảng videos engine='WEBSUB')")
    finally:
        eng.stop()
    _write_selftest_report(base, res, log)
    return res


def _write_selftest_report(base, res, log):
    rep = base / "TEST_REPORT_MAY_BAN.md"
    with open(rep, "w", encoding="utf-8") as f:
        f.write(f"# TEST REPORT (chạy trên máy bạn) — {VERSION} — {datetime.now():%Y-%m-%d %H:%M:%S}\n\n| Test | Kết quả | Chi tiết |\n|---|---|---|\n")
        for n, s, d in res:
            f.write(f"| {n} | **{s}** | {str(d).replace('|', '/')} |\n")
    log(f"📄 Đã ghi {rep}")


def _strip_html(t: str) -> str:
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", t)
    t = re.sub(r"(?s)<br\s*/?>|</(tr|p|div|li|h\d)>", "\n", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    import html as _h
    return "\n".join(" ".join(l.split()) for l in _h.unescape(t).splitlines() if l.strip())


def scrub(text: str, *secrets_) -> str:
    """Xoá mọi bí mật (secret, chữ ký) khỏi văn bản trước khi ghi báo cáo."""
    t = str(text or "")
    for sec in secrets_:
        if sec:
            t = t.replace(sec, f"<secret {secret_fp(sec)}>")
    t = re.sub(r"(hub\.secret=)[^&\s\"']+", r"\1<ẩn>", t)
    return re.sub(r"(sha(?:1|256|384|512)=)[0-9a-fA-F]{12,}", r"\1<ẩn>", t)


def hub_details(cfg, cb, ch, secret, timeout=10, save_dir: Path | None = None) -> dict:
    """Hỏi CHÍNH hub Google trang subscription-details. Trả: mã HTTP + TOÀN BỘ thông điệp (đã xoá bí mật) — trước đây
    chỉ giữ dòng chứa vài từ khoá nên chỉ còn chữ 'Error' và mất câu giải thích. Thử cả có và không có hub.secret."""
    url = cfg["hub_url"].rsplit("/", 1)[0] + "/subscription-details"
    out = {"http": None, "lines": [], "variants": []}
    for label, params in (("có secret", {"hub.callback": cb, "hub.topic": TOPIC_FMT.format(ch), "hub.secret": secret}),
                          ("không secret", {"hub.callback": cb, "hub.topic": TOPIC_FMT.format(ch)})):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            txt = scrub(_strip_html(r.text), secret)
            lines = [l for l in txt.splitlines() if l.strip()][:25]
            out["variants"].append({"label": label, "http": r.status_code, "lines": lines})
            if save_dir is not None:
                save_dir.mkdir(parents=True, exist_ok=True)
                (save_dir / f"{ch}_{'sec' if 'hub.secret' in params else 'nosec'}.html").write_text(
                    scrub(r.text, secret), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            out["variants"].append({"label": label, "http": None, "lines": [f"lỗi mạng: {type(e).__name__}: {str(e)[:150]}"]})
    first = out["variants"][0] if out["variants"] else {"http": None, "lines": []}
    out["http"], out["lines"] = first["http"], first["lines"]
    return out


def hub_details_short(hd) -> str:
    parts = []
    for v in hd.get("variants") or []:
        msg = " / ".join(l for l in v["lines"] if l.lower() not in ("error",))[:220] or "(trang trống)"
        parts.append(f"[{v['label']}] HTTP {v['http']}: {msg}")
    return " ‖ ".join(parts) or f"HTTP {hd.get('http')}"


def http_status(url, timeout=8, **kw):
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                                                        "Accept-Language": "en-US,en;q=0.8"}, **kw)
        return r.status_code, r.text
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def diag(cfg, base, resubscribe=False, wait_s=120):
    """Chẩn đoán TỪNG kênh WebSub: DB thuê bao + nhật ký xác minh + chi tiết từ hub Google + trang kênh (kiểm tra ID)."""
    log = Log(base / "logs")
    out = [f"# DIAG WebSub — {VERSION} — {datetime.now():%Y-%m-%d %H:%M:%S}", ""]
    eng = Engine(cfg, base, log)
    chans, bad = eng.excel.read_channels()
    eng.channels = chans
    out.append(f"Excel: {len(chans)} kênh, giá trị sai: {bad}")
    cb = eng.resolve_callback()
    out.append(f"Callback: {cb or 'KHÔNG CÓ (ngrok chưa chạy / callback_url trống)'}")
    out.append(f"Topic WebSub dùng để đăng ký: {TOPIC_FMT.format('<CHANNEL_ID>')}")
    out.append(f"Secret HMAC hiện tại: dấu vân tay {secret_fp(eng.secret)} | secret cũ đã biết: "
               f"{[secret_fp(x) for x, _ in load_secret_history(base)]}")
    bad = eng.db.all("SELECT * FROM hub_events WHERE kind LIKE 'push-%' AND kind NOT IN ('push-VALID_SIGNATURE','push-old') "
                     "ORDER BY id DESC LIMIT 10")
    out.append("POST chữ ký sai / secret cũ gần nhất:" + ("" if bad else " không có"))
    for e in bad:
        out.append(f"  - {datetime.fromtimestamp(e['at']):%d/%m %H:%M:%S} {e['kind']} kênh {e['channel_id'] or '?'}: {e['detail']}")
    out.append("Phát hiện video: CHỈ WebSub (không feed polling, không Data API). Trang kênh chỉ dùng ở đây để kiểm tra ID.")
    if cb:
        try:
            r = requests.get(cb, timeout=8, headers={"ngrok-skip-browser-warning": "1"})
            out.append(f"Callback trả lời GET: HTTP {r.status_code} '{r.text[:30]}' (mong đợi 200 'WS360 OK' khi tool chính/selftest đang chạy)")
        except Exception as e:  # noqa: BLE001
            out.append(f"Callback KHÔNG trả lời: {type(e).__name__}: {e}")
    if resubscribe:
        if not cb:
            out.append("--resubscribe: BỎ QUA vì chưa có callback công khai")
        elif not port_free(cfg.get("listen_host", "0.0.0.0"), cfg.get("listen_port", 8765)):
            out.append("--resubscribe: BỎ QUA — cổng đang bận (tool chính đang chạy; tool chính tự gửi lại theo lịch). "
                       "Tắt tool chính nếu muốn chẩn đoán đăng ký lại ngay.")
        else:
            eng.start(websub=False, workers=False, excel_watch=False)
            eng.start_webhook()
            eng.callback = cb
            n = eng.subscribe_all()
            out.append(f"Đã gửi đăng ký lại {n}/{len(chans)} kênh; chờ hub xác minh tối đa {wait_s}s…")
            end = time.time() + wait_s
            while time.time() < end:
                v = eng.db.one("SELECT COUNT(*) c FROM subs WHERE state='VERIFIED' AND callback=? AND verified_at > ?",
                               (cb, end - wait_s - 1))["c"]
                if v >= len(chans):
                    break
                time.sleep(2)
    out += ["", "| # | Channel ID | Dòng Excel | Trạng thái DB | HTTP đăng ký | Lần gửi | Xác minh gần nhất | Hub Google báo | "
            "Trang kênh | Kết luận |", "|---|" + "---|" * 9]
    for i, (ch, rows) in enumerate(chans.items(), start=1):
        r = eng.db.one("SELECT * FROM subs WHERE channel_id=?", (ch,))
        ev = eng.db.all("SELECT * FROM hub_events WHERE channel_id=? ORDER BY id DESC LIMIT 3", (ch,))
        evs = "; ".join(f"{datetime.fromtimestamp(e['at']):%H:%M:%S} {e['kind']}/{e['mode']} {e['http']} "
                        f"{'OK' if e['accepted'] else 'TỪ CHỐI'} {e['detail'] or ''}".strip() for e in ev) or "chưa có"
        hd = hub_details(cfg, cb, ch, eng.secret, save_dir=base / "logs" / "hub_details") if cb and requests \
            else {"http": None, "lines": []}
        hds = hub_details_short(hd).replace("|", "/")
        pc, ptxt = http_status(CHANNEL_PAGE_FMT.format(ch))
        page_ok = pc == 200 and ch in (ptxt or "") and "This channel doesn" not in (ptxt or "")
        state = r["state"] if r else "CHƯA ĐĂNG KÝ"
        if state == "VERIFIED" and r["callback"] == cb and (r["verified_at"] or 0) + (r["lease_seconds"] or 0) > time.time():
            concl = f"OK — hub đã xác minh, lease tới {datetime.fromtimestamp(r['verified_at'] + r['lease_seconds']):%d/%m %H:%M}"
        elif state == "VERIFIED":
            concl = ("VERIFIED nhưng cho CALLBACK CŨ " + str(r["callback"]) if r["callback"] != cb else "VERIFIED nhưng LEASE ĐÃ HẾT") + \
                " → mở tool chính để đăng ký lại"
        elif pc is not None and not page_ok:
            concl = f"Trang kênh trả HTTP {pc} / không thấy kênh → Channel ID có thể SAI hoặc kênh bị xoá/ẩn; kiểm tra ô cột K"
        elif r is None:
            concl = "CHƯA từng gửi đăng ký (chưa có callback lúc tool chạy, hoặc kênh mới thêm) — chạy DIAG chọn 2"
        elif ev and not ev[0]["accepted"] and ev[0]["kind"] == "verify":
            concl = f"Hub ĐÃ gọi xác minh nhưng tool TỪ CHỐI: {ev[0]['detail']}"
        elif not any(e["kind"] == "verify" for e in ev):
            concl = "Hub CHƯA gọi xác minh tới callback (xem cột Hub Google báo; tool sẽ gửi lại theo lịch 2/5/10/30/60 phút)"
        else:
            concl = f"trạng thái {state}"
        out.append(f"| {i} | {ch} | {rows} | {state} | {r['last_http'] if r else '-'} | {r['attempts'] if r else 0} | {evs} | "
                   f"{hds} | {pc}{' (tồn tại)' if page_ok else ''} | {concl} |")
        time.sleep(0.3)
    if resubscribe and eng.httpd:
        eng.stop()
    rep = base / "DIAG_REPORT.md"
    rep.write_text("\n".join(o.replace("\n", " ") if o.startswith("|") else o for o in out) + "\n", encoding="utf-8")
    for o in out:
        print(o)
    log(f"📄 Đã ghi {rep}")
    return out


def add_query(url: str, params: dict) -> str:
    return url + ("&" if "?" in url else "?") + urlencode(params)


def hubtest(cfg, base, n_channels=2, wait_s=60):
    """KIỂM TRA SÂU hub Google (tool chính PHẢI tắt, ngrok PHẢI chạy). Cho từng kênh thử:
      A) đăng ký ĐỒNG BỘ (hub.verify=sync) callback hiện tại → hub phải gọi GET xác minh NGAY và trả 204 nếu thành công,
         hoặc 4xx kèm lý do nếu không xác minh được → thấy trực tiếp nguyên nhân;
      B) đăng ký THỬ với callback MỚI hoàn toàn (…?ws360probe=…) → phân biệt 'hub không gọi được tới máy bạn'
         với 'hub không xác minh lại đăng ký ĐÃ CÓ'. Đăng ký thử được huỷ ngay sau đó.
    Ghi HUBTEST_REPORT.md. Không ghi secret."""
    log = Log(base / "logs")
    out = [f"# HUBTEST — {VERSION} — {datetime.now():%Y-%m-%d %H:%M:%S}", ""]
    if not port_free(cfg.get("listen_host", "0.0.0.0"), cfg.get("listen_port", 8765)):
        msg = "Cổng đang bận → TẮT tool chính (START.bat) rồi chạy lại. (ngrok vẫn để chạy.)"
        print(msg)
        (base / "HUBTEST_REPORT.md").write_text("\n".join(out + [msg]) + "\n", encoding="utf-8")
        return out + [msg]
    eng = Engine(cfg, base, log)
    eng.start(websub=False, workers=False, excel_watch=False)
    try:
        eng.start_webhook()
        cb = eng.resolve_callback()
        eng.callback = cb
        out.append(f"Callback: {cb or 'KHÔNG CÓ (ngrok chưa chạy?)'}")
        out.append(f"Secret hiện tại: dấu vân tay {secret_fp(eng.secret)}")
        if not cb:
            return out
        try:
            r = requests.get(cb, timeout=10)
            out.append(f"GET sức khoẻ qua internet → HTTP {r.status_code} '{r.text[:20]}' "
                       f"(phải là 200 'WS360 OK'; nếu là trang HTML của ngrok thì ngrok đang chặn)")
        except Exception as e:  # noqa: BLE001
            out.append(f"GET sức khoẻ qua internet → LỖI {type(e).__name__}: {e}")
        out += ["", "| Kênh | Thử | Yêu cầu gửi hub | Hub trả lời | GET xác minh nhận được | Kết luận |", "|---|---|---|---|---|---|"]
        for ch in list(eng.channels)[:max(1, n_channels)]:
            # A) sync, callback hiện tại
            t0 = time.time()
            try:
                code, body = eng._hub("subscribe", ch, cb, verify="sync", timeout=(5, 60))
            except Exception as e:  # noqa: BLE001
                code, body = None, f"lỗi mạng: {type(e).__name__}: {e}"
            req = json.dumps(eng.last_request, ensure_ascii=False)
            got = eng.db.all("SELECT * FROM hub_events WHERE channel_id=? AND kind='verify' AND at>=?", (ch, t0))
            got_s = "; ".join(f"{e['http']} {'OK' if e['accepted'] else 'TỪ CHỐI'} {e['detail'][:80]}" for e in got) or "KHÔNG"
            concl = ("hub xác minh ĐỒNG BỘ thành công → đăng ký với secret hiện tại đã có hiệu lực" if code == 204 else
                     "hub chỉ nhận yêu cầu (202), KHÔNG xác minh đồng bộ" if code == 202 else
                     f"hub TỪ CHỐI / không xác minh được: {body[:160]}")
            out.append(f"| {ch} | A: sync, callback hiện tại | {req.replace('|', '/')} | HTTP {code}: {body[:160].replace('|', '/')} | "
                       f"{got_s.replace('|', '/')} | {concl} |")
            # B) callback MỚI (probe) — sync rồi async chờ
            pid = secrets.token_hex(4)
            eng.probes[pid] = []
            pcb = add_query(cb, {"ws360probe": pid})
            t1 = time.time()
            try:
                code2, body2 = eng._hub("subscribe", ch, pcb, verify="sync", timeout=(5, 60))
            except Exception as e:  # noqa: BLE001
                code2, body2 = None, f"lỗi mạng: {type(e).__name__}: {e}"
            end = time.time() + (wait_s if code2 == 202 else 0)
            while time.time() < end and not eng.probes[pid]:
                time.sleep(1)
            got2 = eng.probes[pid]
            concl2 = ("hub GỌI ĐƯỢC tới máy bạn với callback mới" if got2 else
                      "hub KHÔNG gọi tới máy bạn kể cả với callback mới → hub không tới được callback "
                      "(ngrok/mạng) hoặc hub đang trì hoãn xác minh")
            out.append(f"| {ch} | B: callback MỚI (thử) | callback …?ws360probe={pid}, verify=sync | HTTP {code2}: "
                       f"{body2[:160].replace('|', '/')} | {len(got2)} lần sau {(got2[0][0] - t1) if got2 else 0:.1f}s | {concl2} |")
            try:                                          # dọn đăng ký thử
                eng._hub("unsubscribe", ch, pcb, verify="sync", timeout=(5, 30))
            except Exception:  # noqa: BLE001
                pass
            hd = hub_details(cfg, cb, ch, eng.secret, save_dir=base / "logs" / "hub_details")
            out.append(f"| {ch} | trang subscription-details | callback hiện tại | {hub_details_short(hd).replace('|', '/')} | | |")
        out += ["", "Cách đọc:",
                "- A=204: hub đã xác minh ngay → vấn đề đã hết, mở lại START.bat.",
                "- A=202 và B có GET: hub tới được máy bạn nhưng KHÔNG xác minh lại đăng ký đã có (của secret cũ) → "
                "bật 'callback_generation' (mặc định bật) để tool dùng callback mới theo secret.",
                "- A và B đều không có GET: hub không gọi được tới callback → kiểm tra ngrok (cửa sổ ngrok phải 'online', "
                "đúng cổng 8765), tường lửa, hoặc chờ hub (Google có thể trì hoãn).",
                "- A=4xx: đọc lý do hub ghi trong cột 'Hub trả lời'."]
    finally:
        eng.stop()
    rep = base / "HUBTEST_REPORT.md"
    rep.write_text("\n".join(out) + "\n", encoding="utf-8")
    for o in out:
        print(o)
    log(f"📄 Đã ghi {rep}")
    return out


def verify_post(cfg, base, path) -> dict:
    """Kiểm lại một request đã lưu (logs/rejected_posts/*.json hoặc .body) với secret HIỆN TẠI và các secret CŨ.
    In: khớp secret nào (dấu vân tay) hoặc không khớp — KHÔNG in secret."""
    p = Path(path)
    meta = json.loads(p.with_suffix(".json").read_text(encoding="utf-8")) if p.with_suffix(".json").exists() else {}
    raw = p.with_suffix(".body").read_bytes()
    sig = meta.get("x_hub_signature") or ""
    cur = load_shared_secret(base)
    cands = [("HIỆN TẠI", cur)] + [(f"CŨ (thay lúc {datetime.fromtimestamp(t):%d/%m %H:%M})", x)
                                   for x, t in load_secret_history(base)]
    algo, _, hexs = sig.partition("=")
    algo = algo.strip().lower()
    res = {"file": p.name, "ids": meta.get("ids"), "source": meta.get("source"), "algo": algo,
           "body_bytes": len(raw), "content_length_header": meta.get("content_length"), "matches": []}
    if algo in ("sha1", "sha256", "sha384", "sha512"):
        for name, sec in cands:
            ok = hmac.compare_digest(hmac.new(sec.encode(), raw, getattr(hashlib, algo)).hexdigest(), hexs.strip().lower())
            res["matches"].append((name, secret_fp(sec), ok))
    hit = [m for m in res["matches"] if m[2]]
    res["ket_luan"] = (f"KHỚP secret {hit[0][0]} ({hit[0][1]})" if hit else
                       "KHÔNG khớp secret nào của tool → hub đang giữ một thuê bao dùng secret mà tool KHÔNG có "
                       "(DB/secret bị ghi đè, hoặc tool khác đăng ký cùng callback). Cách sửa: để tool đăng ký lại và chờ "
                       "dòng '✅ Hub XÁC MINH' cho kênh đó.")
    if str(res["content_length_header"] or "") not in ("", str(len(raw))):
        res["ket_luan"] += " | CẢNH BÁO: số byte body khác Content-Length"
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return res


STEP_COLS = ["resolver_start_ms", "db_resolving_ms", "resolver_ms", "db_downloading_ms", "ttfb_ms", "download_ms", "mux_ms",
             "db_verifying_ms", "db_lock_wait_ms", "pre_verify_gap_ms", "ffprobe_ms", "rename_ms", "post_verify_gap_ms",
             "total_ms", "sum_check_ms"]


def bench360(cfg, base, ids, reps=10):
    """Đo ĐÚNG đường tải thật của tool (Engine.process: resolve → tải → ffprobe → DONE, có SQLite) N lượt / video.
    Mỗi lượt: xoá file + bản ghi, lấy link MỚI. Ghi BENCH360.csv và in median/p90 từng bước. Không đụng Excel/DB thật."""
    log = Log(base / "logs")
    bcfg = dict(cfg, download_dir="DOWNLOADS_BENCH", excel_enabled=False)
    eng = Engine(bcfg, base, log, db_name="database_BENCH.sqlite", lock_name="ws360_bench.lock")
    eng.resolver.warm()
    if FFPROBE:
        subprocess.run([FFPROBE, "-version"], capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    rows = []
    for vid in ids:
        for rep in range(reps):
            eng.db.x("DELETE FROM videos WHERE video_id=?", (vid,))
            ch = "UCBENCH" + "0" * 17
            shutil.rmtree(eng.dl_dir / ch, ignore_errors=True)       # tên file theo tiêu đề: xoá cả thư mục bench mỗi lượt
            t = time.perf_counter_ns()
            eng.db.x("INSERT INTO videos(video_id, channel_id, url, engine, detected_at, state) VALUES(?,?,?,?,?,?)",
                     (vid, ch, video_url(vid), "BENCH", iso(), "QUEUED"))
            r = eng.process({"vid": vid, "ch": ch, "t_recv_ns": t, "t_enq_ns": t})
            row = dict(eng.db.one("SELECT * FROM videos WHERE video_id=?", (vid,)))
            row.update(rep=rep, result=r)
            rows.append(row)
            log(f"BENCH360 {vid} #{rep + 1}/{reps}: {r} total {row.get('total_ms')} ms | " +
                " ".join(f"{k}={row.get(k)}" for k in STEP_COLS[:-2]))
    out = base / "BENCH360.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["video_id", "rep", "result", "size", "kind"] + STEP_COLS)
        for r in rows:
            w.writerow([r["video_id"], r["rep"], r["result"], r.get("size"), r.get("kind")] + [r.get(c) for c in STEP_COLS])
    ok = [r for r in rows if r["result"] == "DONE"]
    print(f"\nBENCH360: {len(ok)}/{len(rows)} lượt DONE → {out}")
    print(f"{'bước':<22}{'median':>10}{'p90':>10}{'max':>10}   (ms; lượt #1 mỗi video là lượt 'nguội')")
    for c in STEP_COLS:
        vals = [float(r[c]) for r in ok if r.get(c) is not None]
        if vals:
            print(f"{c:<22}{pct(vals, 50):>10.1f}{pct(vals, 90):>10.1f}{max(vals):>10.1f}")
    shutil.rmtree(eng.dl_dir, ignore_errors=True)
    return rows


BENCH_DL_VARIANTS = {
    # tên: (cấu hình, chuẩn bị trước mỗi lượt)
    "legacy":        ({"net_mode": "legacy", "range_connections": 1}, "cold"),     # đúng bản trước, kết nối nguội (như sau lúc rảnh)
    "fast_cold":     ({"net_mode": "fast", "range_connections": 1}, "cold"),       # lớp mạng mới, KHÔNG giữ ấm (để tách tác dụng)
    "warm_resolver": ({"net_mode": "fast", "range_connections": 1}, "warm_inner"), # giữ ấm Innertube; máy chủ video NGUỘI (thực tế xấu)
    "warm_all":      ({"net_mode": "fast", "range_connections": 1}, "warm_all"),   # giữ ấm cả máy chủ video vừa dùng (thực tế tốt)
    "warm_all_r2":   ({"net_mode": "fast", "range_connections": 2}, "warm_all"),
    "warm_all_r4":   ({"net_mode": "fast", "range_connections": 4}, "warm_all"),
    "warm_apihost":  ({"net_mode": "fast", "range_connections": 1, "resolver_host": "youtubei.googleapis.com"}, "warm_all"),
    "warm_h2":       ({"net_mode": "fast", "range_connections": 1}, "warm_all"),  # tải bằng HTTP/2 (cần: pip install "httpx[http2]")
}


def _h2_client():
    try:
        import httpx  # noqa: F401
        import h2  # noqa: F401
    except ImportError:
        return None
    return httpx.Client(http2=True, follow_redirects=True, timeout=httpx.Timeout(8.0, connect=2.0))


def _bench_h2_once(eng, vid, ch, client):
    """Cùng các bước như Engine.process (resolve → tải 1 GET → ffprobe), nhưng phần tải qua HTTP/2 (httpx, kết nối giữ ấm)."""
    t0 = time.perf_counter_ns()
    plan = eng.resolver.resolve(vid)
    t_res = time.perf_counter_ns()
    pick = plan["pick"]
    if pick["kind"] != "progressive":
        return {"result": "SKIP", "error": "không phải progressive"}
    d = eng.dl_dir / ch
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f".{vid}.part.mp4"
    first = None
    n = 0
    with client.stream("GET", pick["video"]["url"]) as r:
        t_hdr = time.perf_counter_ns()
        if r.status_code >= 400:
            return {"result": "FAILED", "error": f"HTTP {r.status_code}"}
        with open(dest, "wb", buffering=1024 * 1024) as f:
            for chunk in r.iter_bytes(256 * 1024):
                if first is None:
                    first = time.perf_counter_ns()
                f.write(chunk)
                n += len(chunk)
        ver = r.http_version
    t_dl = time.perf_counter_ns()
    exp = int(pick["video"].get("clen") or 0)
    v = ffprobe_info(dest)
    t_v = time.perf_counter_ns()
    dest.unlink(missing_ok=True)
    ok = v.get("ok") and (not exp or exp == n)
    return {"result": "DONE" if ok else "FAILED", "error": "" if ok else f"ffprobe/size {n}/{exp}", "total_ms": ms(t0, t_v),
            "resolver_ms": ms(t0, t_res), "ttfb_ms": ms(t_res, first), "transfer_ms": ms(first, t_dl), "download_ms": ms(t_res, t_dl),
            "ffprobe_ms": ms(t_dl, t_v), "size": n, "media_server_wait_ms": None, "http_version": ver,
            "mbps": round(n * 8 / 1e6 / max(1e-3, (t_dl - t_res) / 1e9), 1)}
BENCH_DL_COLS = ["total_ms", "resolver_ms", "res_new_conn", "res_setup_ms", "res_server_wait_ms", "res_body_parse_ms",
                 "media_new_conns", "media_setup_ms", "media_server_wait_ms", "ttfb_ms", "transfer_ms", "download_ms", "ffprobe_ms",
                 "rename_ms", "mbps", "size"]


def bench_dl(cfg, base, ids, reps=None, variants=None):
    """Đo đường tải THẬT (Engine.process: resolve → tải → ffprobe → DONE) với nhiều phương án, thứ tự xoay vòng.
    Mỗi lượt: lấy link MỚI, xoá file + bản ghi trước (không dùng file/kết quả có sẵn). Kết quả: BENCH_DL.csv / BENCH_DL.md."""
    names = [v for v in (variants or list(BENCH_DL_VARIANTS)) if v in BENCH_DL_VARIANTS]
    reps = reps or max(1, -(-20 // max(1, len(ids))))               # ≥ 20 lượt mỗi phương án
    log = Log(base / "logs")
    bcfg = dict(cfg, download_dir="DOWNLOADS_BENCH", excel_enabled=False, net_warm=False)
    eng = Engine(bcfg, base, log, db_name="database_BENCH.sqlite", lock_name="ws360_bench.lock")
    if FFPROBE:
        subprocess.run([FFPROBE, "-version"], capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    global NET_LEARN
    learned = []
    NET_LEARN = lambda u: learned.append(u)
    rows = []
    ch = "UCBENCH" + "0" * 17
    h2c = _h2_client() if "warm_h2" in names else None
    if "warm_h2" in names and h2c is None:
        names = [n for n in names if n != "warm_h2"]
        log("BENCH-DL: bỏ qua warm_h2 (chưa cài 'httpx[http2]': python -m pip install \"httpx[http2]\")")
    log(f"BENCH-DL: {len(ids)} video × {reps} vòng × {len(names)} phương án = {len(ids) * reps * len(names)} lượt tải thật")
    for rep_i in range(reps):
        for vid in ids:
            order = names[rep_i % len(names):] + names[:rep_i % len(names)]
            if rep_i % 2:
                order = order[::-1]
            for v in order:
                over, prep = BENCH_DL_VARIANTS[v]
                eng.cfg.update(dict({"range_connections": 1, "resolver_host": ""}, **over))
                set_net_mode(eng.cfg["net_mode"])
                eng.resolver.host_override = eng.cfg.get("resolver_host") or ""
                p = pool()
                if prep == "cold" or prep == "warm_inner":
                    p.clear()                                       # đóng mọi kết nối: như khi tool rảnh lâu
                    if NET is not None:
                        with NET.DNS.lock:
                            NET.DNS.m.clear()
                if prep in ("warm_inner", "warm_all") and NET is not None:
                    host = eng.resolver.host_override or "www.youtube.com"
                    w = NET.NetWarmer(pool, log, (host,), 3, 6 if prep == "warm_all" else 0, 40)
                    if prep == "warm_all":
                        for u in learned[-6:]:
                            w.learn(u)
                    w.warm_now()                                    # = trạng thái bộ giữ ấm tạo ra khi chạy thật
                eng.db.x("DELETE FROM videos WHERE video_id=?", (vid,))
                shutil.rmtree(eng.dl_dir / ch, ignore_errors=True)
                if v == "warm_h2":
                    try:
                        out = dict({"variant": v, "video_id": vid, "rep": rep_i}, **_bench_h2_once(eng, vid, ch, h2c))
                    except Exception as e:  # noqa: BLE001
                        out = {"variant": v, "video_id": vid, "rep": rep_i, "result": "FAILED", "error": f"{type(e).__name__}: {e}"[:160]}
                    rows.append(out)
                    log(f"BENCH-DL {v:14} {vid} #{rep_i + 1}: {out['result']} TỔNG {out.get('total_ms')} ms | resolver "
                        f"{out.get('resolver_ms')} | TTFB {out.get('ttfb_ms')} truyền {out.get('transfer_ms')} | {out.get('http_version')}"
                        + (f" | LỖI {out['error']}" if out.get("error") else ""))
                    time.sleep(0.3)
                    continue
                t = time.perf_counter_ns()
                eng.db.x("INSERT INTO videos(video_id, channel_id, url, engine, detected_at, state) VALUES(?,?,?,?,?,?)",
                         (vid, ch, video_url(vid), "BENCH", iso(), "QUEUED"))
                r = eng.process({"vid": vid, "ch": ch, "t_recv_ns": t, "t_enq_ns": t})
                row = dict(eng.db.one("SELECT * FROM videos WHERE video_id=?", (vid,)))
                net = json.loads(row.get("net") or "{}")
                rn, mn = net.get("resolver") or {}, net.get("media") or {}
                add = lambda *xs: round(sum(x for x in xs if x is not None), 1) if any(x is not None for x in xs) else None
                out = {"variant": v, "video_id": vid, "rep": rep_i, "result": r, "total_ms": row.get("total_ms"),
                       "resolver_ms": row.get("resolver_ms"), "res_new_conn": 1 if rn.get("new_conn") else 0,
                       "res_setup_ms": add(rn.get("dns_ms"), rn.get("tcp_ms"), rn.get("tls_ms")) if rn.get("new_conn") else 0.0,
                       "res_server_wait_ms": rn.get("server_wait_ms"), "res_body_parse_ms": add(rn.get("body_ms"), rn.get("parse_ms")),
                       "media_new_conns": mn.get("new_conns"),
                       "media_setup_ms": add(mn.get("dns_ms"), mn.get("tcp_ms"), mn.get("tls_ms")) if mn.get("new_conns") else 0.0,
                       "media_server_wait_ms": mn.get("server_wait_ms"), "ttfb_ms": mn.get("ttfb_ms"), "transfer_ms": mn.get("transfer_ms"),
                       "download_ms": row.get("download_ms"), "ffprobe_ms": row.get("ffprobe_ms"), "rename_ms": row.get("rename_ms"),
                       "mbps": row.get("mbps"), "size": row.get("size"), "client": rn.get("client"), "media_host": mn.get("host"),
                       "error": row.get("error") if r != "DONE" else ""}
                rows.append(out)
                log(f"BENCH-DL {v:14} {vid} #{rep_i + 1}: {r} TỔNG {out['total_ms']} ms | resolver {out['resolver_ms']} "
                    f"(setup {out['res_setup_ms']}, chờ {out['res_server_wait_ms']}) | tải TTFB {out['ttfb_ms']} (setup "
                    f"{out['media_setup_ms']}, chờ {out['media_server_wait_ms']}) truyền {out['transfer_ms']} | ffprobe {out['ffprobe_ms']}"
                    + (f" | LỖI {out['error']}" if out["error"] else ""))
                time.sleep(0.3)
    NET_LEARN = None
    set_net_mode(cfg.get("net_mode", "fast"))
    shutil.rmtree(eng.dl_dir, ignore_errors=True)
    with open(base / "BENCH_DL.csv", "w", newline="", encoding="utf-8") as f:
        keys = list(dict.fromkeys(k for r in rows for k in r)) or ["variant"]
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    md = [f"# BENCH-DL — tải 360p thật, {len(ids)} video, {reps} vòng/phương án, thứ tự xoay vòng ({iso()})", "",
          "Mỗi lượt: link MỚI + xoá file trước (không dùng file có sẵn). Giá trị: median / P90 (ms). TỔNG = từ lúc bắt đầu resolve "
          "tới khi file đã tải xong + ffprobe xác minh + bàn giao (không tính TTFB 2 lần: TTFB nằm trong 'tải').", "",
          "| Phương án | lượt | lỗi | TỔNG | ≤1000 ms | resolver | setup resolver | chờ máy chủ resolver | TTFB | setup tải | "
          "chờ máy chủ tải | truyền | ffprobe |", "|" + "---|" * 13]
    summary = {}
    for v in names:
        rs = [r for r in rows if r["variant"] == v]
        ok = [r for r in rs if r["result"] == "DONE"]
        q = lambda k: (pct([r[k] for r in ok if r.get(k) is not None], 50), pct([r[k] for r in ok if r.get(k) is not None], 90))
        summary[v] = {k: q(k) for k in BENCH_DL_COLS}
        summary[v].update(n=len(rs), fail=len(rs) - len(ok), under_1s=sum(1 for r in ok if (r["total_ms"] or 1e9) <= 1000))
        f_ = lambda t: "-" if t[0] is None else f"{t[0]:.0f} / {t[1]:.0f}"
        sv = summary[v]
        md.append(f"| {v} | {sv['n']} | {sv['fail']} ({100 * sv['fail'] / max(1, sv['n']):.0f}%) | {f_(sv['total_ms'])} | "
                  f"{sv['under_1s']}/{len(ok)} | {f_(sv['resolver_ms'])} | {f_(sv['res_setup_ms'])} | {f_(sv['res_server_wait_ms'])} | "
                  f"{f_(sv['ttfb_ms'])} | {f_(sv['media_setup_ms'])} | {f_(sv['media_server_wait_ms'])} | {f_(sv['transfer_ms'])} | "
                  f"{f_(sv['ffprobe_ms'])} |")
    md += ["", "Phương án: legacy = bản trước (kết nối nguội); fast_cold = lớp mạng mới không giữ ấm; warm_resolver = giữ ấm "
           "Innertube, máy chủ video nguội; warm_all = giữ ấm cả máy chủ video vừa dùng; _r2/_r4 = chia 2/4 đoạn Range; "
           "warm_apihost = resolver qua youtubei.googleapis.com.",
           "Lưu ý: cùng 1 video tải lặp lại thì máy chủ cache của Google (CDN) đã có sẵn → 'chờ máy chủ tải' có thể thấp hơn video "
           "vừa đăng thật; thứ tự xoay vòng để mọi phương án chịu ảnh hưởng như nhau."]
    (base / "BENCH_DL.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    return {"rows": rows, "summary": summary}


def bench(cfg, base, ids, reps=3):
    """TEST 4 trên máy thật: mỗi lần chạy RESOLVE LẠI (không dùng link cũ), thử nhiều cấu hình tải, ghi BENCHMARK.csv."""
    log = Log(base / "logs")
    rs = Resolver(log)
    rs.warm()
    out = base / "BENCHMARK.csv"
    new = not out.exists()
    d = base / "DOWNLOADS" / "_bench"
    d.mkdir(parents=True, exist_ok=True)
    confs = [("1get", 1, "py")] + [(f"range_x{n}", n, "py") for n in (4, 8, 12, 16)]
    if ARIA2C:
        confs += [("aria2_x4", 4, "aria2"), ("aria2_x16", 16, "aria2")]
    v9 = None
    if cfg.get("v9_path") and Path(cfg["v9_path"]).exists():
        import importlib.util
        spec = importlib.util.spec_from_file_location("V9_4_SUB2S", cfg["v9_path"])
        v9 = importlib.util.module_from_spec(spec)
        sys.modules["V9_4_SUB2S"] = v9
        spec.loader.exec_module(v9)
        confs.append(("v9_1get_cu", 1, "v9"))
    with open(out, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "video_id", "config", "rep", "kind", "itags", "size_bytes", "resolve_ms", "ttfb_ms",
                        "download_ms", "mux_ms", "verify_ms", "total_ms", "mbps", "ok", "error", "env"])
        for vid in ids:
            for rep in range(reps):
                for name, n, eng in confs:
                    row = [iso(), vid, name, rep]
                    t0 = time.perf_counter_ns()
                    try:
                        plan = rs.resolve(vid)
                        t1 = time.perf_counter_ns()
                        pick = plan["pick"]
                        info = {}
                        tmp = d / f"{vid}_{name}_{rep}.mp4"
                        mux = None
                        if pick["kind"] == "progressive":
                            if eng == "aria2":
                                download_aria2(pick["video"]["url"], tmp, n, info)
                            elif eng == "v9":
                                ok = v9._download_direct_single_get(pick["video"]["url"], d, tmp.name, lambda m: None,
                                                                    100_000, int(pick["video"].get("clen") or 0), info)
                                if not ok:
                                    raise IOError(info.get("fail_reason"))
                            else:
                                download_ranges(pick["video"]["url"], tmp, int(pick["video"].get("clen") or 0), n, info)
                        else:
                            vt, at = d / f"{vid}.v", d / f"{vid}.a"
                            download_ranges(pick["video"]["url"], vt, int(pick["video"].get("clen") or 0), n, info)
                            if pick["audio"]:
                                download_single(pick["audio"]["url"], at, {})
                                mux = ffmpeg_mux(vt, at, tmp)
                            else:
                                os.replace(vt, tmp)
                        t2 = time.perf_counter_ns()
                        v = ffprobe_info(tmp)
                        t3 = time.perf_counter_ns()
                        size = tmp.stat().st_size
                        dl = ms(t1, t2) - (mux or 0)
                        row += [pick["kind"], ",".join(str(x["itag"]) for x in (pick["video"], pick["audio"]) if x), size,
                                ms(t0, t1), info.get("ttfb_ms"), dl, mux, ms(t2, t3), ms(t0, t3),
                                round(size * 8 / 1e6 / max(1e-3, dl / 1000), 2), v["ok"], "", "real"]
                        tmp.unlink(missing_ok=True)
                    except Exception as e:  # noqa: BLE001
                        row += ["", "", "", ms(t0, time.perf_counter_ns()), "", "", "", "", "", "", False,
                                f"{type(e).__name__}: {redact(str(e))[:120]}", "real"]
                    w.writerow(row)
                    f.flush()
                    log(f"BENCH {vid} {name} #{rep}: {row[4:16]}")
    log(f"📄 Đã ghi {out}")


def race_report(db: "DB", out_dir: Path | None = None) -> dict:
    """So sánh WebSub ↔ API trên dữ liệu THẬT trong database.sqlite (bảng detections). Không suy diễn: video chỉ 1 nguồn
    thấy được ghi riêng; không đủ dữ liệu → PENDING."""
    rows = db.all("SELECT * FROM detections WHERE baseline=0 ORDER BY t_wall")
    by = {}
    for r in rows:
        by.setdefault(r["video_id"], {})[r["source"]] = r
    both, only = [], {"WEBSUB": [], "API": []}
    for vid, d in by.items():
        if "WEBSUB" in d and "API" in d:
            w, a = d["WEBSUB"], d["API"]
            win = "WEBSUB" if w["winner"] else ("API" if a["winner"] else ("WEBSUB" if w["t_wall"] <= a["t_wall"] else "API"))
            lag = (a if win == "WEBSUB" else w)["lag_ms"]
            if lag is None:
                lag = abs(a["t_wall"] - w["t_wall"]) * 1000
            pub = parse_time(w["published_at"] or a["published_at"])
            both.append({"video_id": vid, "channel_id": w["channel_id"], "published_at": iso(pub) if pub else "",
                         "websub_at": w["detected_at"], "api_at": a["detected_at"], "winner": win, "lead_ms": round(lag, 1),
                         "websub_after_publish_s": round(w["t_wall"] - pub.timestamp(), 1) if pub else None,
                         "api_after_publish_s": round(a["t_wall"] - pub.timestamp(), 1) if pub else None,
                         "api_interval_s": a["api_interval_s"], "mode": w["mode"] or a["mode"]})
        else:
            for src in d:
                if d[src]["mode"] == "PARALLEL":
                    only[src].append(vid)
    ws_w = [x for x in both if x["winner"] == "WEBSUB"]
    ap_w = [x for x in both if x["winner"] == "API"]
    res = {"pairs": len(both), "websub_wins": len(ws_w), "api_wins": len(ap_w),
           "websub_lead_ms_p50": pct([x["lead_ms"] for x in ws_w], 50), "websub_lead_ms_p90": pct([x["lead_ms"] for x in ws_w], 90),
           "api_lead_ms_p50": pct([x["lead_ms"] for x in ap_w], 50), "api_lead_ms_p90": pct([x["lead_ms"] for x in ap_w], 90),
           "websub_after_publish_p50_s": pct([x["websub_after_publish_s"] for x in both], 50),
           "api_after_publish_p50_s": pct([x["api_after_publish_s"] for x in both], 50),
           "only_websub": len(only["WEBSUB"]), "only_api": len(only["API"]), "rows": both,
           "verdict": "PENDING (chưa có video nào được CẢ HAI nguồn phát hiện ở chế độ PARALLEL)" if not both else
           (f"Trên {len(both)} video thật: WebSub tới trước {len(ws_w)} lần, API tới trước {len(ap_w)} lần"
            + (" — ÍT DỮ LIỆU, chưa đủ để kết luận" if len(both) < 10 else ""))}
    if out_dir is not None:
        with open(out_dir / "RACE_BENCHMARK.csv", "w", newline="", encoding="utf-8-sig") as f:
            cols = ["video_id", "channel_id", "published_at", "websub_at", "api_at", "winner", "lead_ms",
                    "websub_after_publish_s", "api_after_publish_s", "api_interval_s", "mode"]
            w_ = csv.DictWriter(f, fieldnames=cols)
            w_.writeheader()
            for x in both:
                w_.writerow(x)
        md = ["# RACE BENCHMARK — WebSub ↔ YouTube Data API (dữ liệu THẬT từ database.sqlite)", "",
              f"Tạo lúc {iso()} | {VERSION}", "", f"**Kết luận: {res['verdict']}**", "",
              "| Chỉ số | Giá trị |", "|---|---|",
              f"| Video cả 2 nguồn cùng thấy | {res['pairs']} |",
              f"| WebSub tới trước | {res['websub_wins']} (dẫn p50 {res['websub_lead_ms_p50']} ms, p90 {res['websub_lead_ms_p90']} ms) |",
              f"| API tới trước | {res['api_wins']} (dẫn p50 {res['api_lead_ms_p50']} ms, p90 {res['api_lead_ms_p90']} ms) |",
              f"| WebSub: trễ sau giờ đăng (p50) | {res['websub_after_publish_p50_s']} s |",
              f"| API: trễ sau giờ đăng (p50) | {res['api_after_publish_p50_s']} s |",
              f"| Chỉ WebSub thấy (PARALLEL) | {res['only_websub']} |", f"| Chỉ API thấy (PARALLEL) | {res['only_api']} |", "",
              "Ghi chú: 'trễ sau giờ đăng' dùng giờ đăng YouTube (độ chính xác ~1 s) so với đồng hồ máy — lệch giờ máy làm "
              "sai số này; còn 'dẫn trước' (ms) đo bằng CÙNG đồng hồ trong 1 tiến trình nên chính xác. Tốc độ API phụ thuộc "
              "nhịp quét lúc đó (cột api_interval_s): quota ít → nhịp thưa → API chậm hơn.", "",
              "| Video | Kênh | Thắng | Dẫn (ms) | WebSub sau đăng (s) | API sau đăng (s) | Nhịp API (s) |", "|---|---|---|---|---|---|---|"]
        md += [f"| {x['video_id']} | {x['channel_id']} | {x['winner']} | {x['lead_ms']} | {x['websub_after_publish_s']} | "
               f"{x['api_after_publish_s']} | {x['api_interval_s']} |" for x in both[-200:]]
        (out_dir / "RACE_BENCHMARK.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=f"YouTube WebSub Instant 360P {VERSION}")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--video", default="")
    ap.add_argument("--bench", nargs="*")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--diag", action="store_true", help="chẩn đoán từng kênh WebSub → DIAG_REPORT.md")
    ap.add_argument("--resubscribe", action="store_true", help="dùng với --diag: đăng ký lại cả danh sách rồi chờ hub xác minh")
    ap.add_argument("--verify-post", default="", help="kiểm lại 1 request bị từ chối đã lưu trong logs/rejected_posts")
    ap.add_argument("--hubtest", action="store_true", help="kiểm tra sâu hub (đăng ký đồng bộ + callback thử) → HUBTEST_REPORT.md")
    ap.add_argument("--bench360", nargs="*", help="đo đường tải thật N lượt/video → BENCH360.csv (median/p90 từng bước)")
    ap.add_argument("--bench-dl", nargs="*", help="so sánh các phương án mạng/tải trên video thật → BENCH_DL.md/.csv (≥20 lượt mỗi phương án)")
    ap.add_argument("--variants", default="", help="--bench-dl: danh sách phương án, cách nhau dấu phẩy")
    ap.add_argument("--integrated", action="store_true", help="bật sẵn tự đăng TikTok (TAM) khi mở giao diện / headless")
    ap.add_argument("--dry-run-post", action="store_true", help="tích hợp: chạy tới khi nút Post sáng rồi DỪNG, không bấm")
    ap.add_argument("--mode", default="", help="WEBSUB_ONLY | API_ONLY | PARALLEL (ghi đè detection_mode trong config.json)")
    ap.add_argument("--race-report", action="store_true", help="so sánh WebSub ↔ API trên dữ liệu thật → RACE_BENCHMARK.md/.csv")
    a = ap.parse_args(argv)
    base = BASE_DIR
    os.chdir(base)
    cfg = load_config(base / a.config)
    if a.integrated:
        cfg["tiktok_enabled"] = True
    if a.dry_run_post:
        cfg["tiktok_dry_run"] = True
    if a.mode:
        cfg["detection_mode"] = norm_mode(a.mode)
    if a.race_report:
        r = race_report(DB(base / "database.sqlite"), base)
        print(f"{r['verdict']}\n→ RACE_BENCHMARK.md / RACE_BENCHMARK.csv")
        return 0
    if a.selftest:
        vid = extract_vid(a.video) if a.video else None
        if a.video and not vid:
            print(f"⚠️ '{a.video}' không phải ID/link video YouTube hợp lệ — bỏ qua TEST3")
        r = selftest(cfg, base, vid)
        return 0 if all(s != "FAIL" for _, s, _ in r) else 1
    if a.bench360 is not None:
        bench360(cfg, base, [v for v in (extract_vid(x) for x in a.bench360) if v], max(1, a.reps if a.reps != 3 else 10))
        return 0
    if a.bench_dl is not None:
        ids = [v for v in (extract_vid(x) for x in a.bench_dl) if v]
        if not ids:
            print("Cần ít nhất 1 ID/link video. Ví dụ: --bench-dl ID1 ID2 ID3")
            return 2
        bench_dl(cfg, base, ids, a.reps if a.reps != 3 else None, [x.strip() for x in a.variants.split(",") if x.strip()] or None)
        return 0
    if a.hubtest:
        hubtest(cfg, base)
        return 0
    if a.verify_post:
        verify_post(cfg, base, a.verify_post)
        return 0
    if a.diag:
        diag(cfg, base, resubscribe=a.resubscribe)
        return 0
    if a.bench is not None:
        bench(cfg, base, [v for v in (extract_vid(x) for x in (a.bench or [])) if v], a.reps)
        return 0
    if a.headless:
        log = Log(base / "logs")
        e = Engine(cfg, base, log)
        if cfg.get("tiktok_enabled"):
            attach_tiktok(e, cfg, log)
        e.start()
        if getattr(e, "bridge", None) is not None:
            threading.Thread(target=e.bridge.start, daemon=True, name="TT-START").start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            e.stop()
        return 0
    return run_gui(cfg, base)


if __name__ == "__main__":
    sys.exit(main())
