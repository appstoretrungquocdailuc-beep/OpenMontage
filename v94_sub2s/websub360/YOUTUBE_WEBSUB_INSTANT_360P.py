# -*- coding: utf-8 -*-
"""
YOUTUBE WEBSUB INSTANT 360P — phát hiện video mới (WebSub + Data API + Atom feed) → TẢI 360P NGAY
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

Không in API key / secret / URL media có chữ ký ra log.
"""
from __future__ import annotations

import argparse
import asyncio
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
    import httpx
except ImportError:  # pragma: no cover
    httpx = None
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
TOPIC_FMT = "https://www.youtube.com/xml/feeds/videos.xml?channel_id={}"
FEED_FMT = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
STATES_ACTIVE = ("DETECTED", "QUEUED", "RESOLVING", "DOWNLOADING", "VERIFYING", "RETRY_WAIT")

DEFAULT_CONFIG = {
    "excel_file": "check.xlsx",
    "excel_sheet": "",                 # trống = sheet đầu tiên
    "col_channel": "K",
    "col_url": "D",
    "excel_enabled": True,
    "download_enabled": True,
    "api_enabled": True,
    "feed_enabled": True,
    "websub_enabled": True,
    "callback_url": "",                # trống = tự đọc URL https từ ngrok (http://127.0.0.1:4040)
    "listen_host": "0.0.0.0",
    "listen_port": 8765,
    "websub_path": "/websub",
    "hub_url": "https://pubsubhubbub.appspot.com/subscribe",
    "lease_seconds": 432000,
    "api_keys_file": "api_keys.txt",
    "api_daily_units_per_key": 8000,   # chừa 20% quota/key cho việc khác
    "api_min_interval_sec": 15.0,
    "feed_interval_sec": 60.0,
    "max_age_minutes": 30,             # video có 'published' cũ hơn → coi là CẬP NHẬT video cũ, không tải
    "download_dir": "DOWNLOADS",
    "workers": 3,
    "range_connections": 1,            # 1 = 1 GET (mặc định). Đổi sau khi chạy --bench trên máy bạn.
    "retry_schedule_sec": [1, 2, 3, 5, 8, 13, 20, 30, 45, 60],
    "max_concurrent_downloads": 3,
    "v9_path": "",                     # đường dẫn V9_4_SUB2S.py (chỉ dùng để benchmark so sánh)
}


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
    """Atom (WebSub push hoặc feed) → [{video_id, channel_id, title, published, updated, deleted}]."""
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
    for cand in (BASE_DIR / f"{name}.exe", BASE_DIR / "bin" / f"{name}.exe", BASE_DIR / name):
        if cand.exists():
            return str(cand)
    return shutil.which(name)


FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")
ARIA2C = find_tool("aria2c")


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
    """

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.con = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=30)
        self.con.row_factory = sqlite3.Row
        with self.lock:
            self.con.executescript(self.SCHEMA)
            self.con.execute("PRAGMA synchronous=NORMAL")
            try:                                          # nâng cấp DB cũ
                self.con.execute("ALTER TABLE excel_jobs ADD COLUMN order_key TEXT")
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
        if not kw:
            return
        cols = ", ".join(f"{k}=?" for k in kw)
        self.x(f"UPDATE videos SET {cols} WHERE video_id=?", (*kw.values(), vid))


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


def pool():
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                _POOL = urllib3.PoolManager(num_pools=32, maxsize=24, block=False, retries=False,
                                            timeout=urllib3.Timeout(connect=2.0, read=8.0))
    return _POOL


def _innertube_client(name):
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


class Resolver:
    CLIENTS = ("android_vr", "web_embedded", "android")

    def __init__(self, log):
        self.log = log
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
        host = str(cfg.get("INNERTUBE_HOST") or "www.youtube.com")
        body = json.dumps({"context": ctx, "videoId": vid, "contentCheckOk": True, "racyCheckOk": True,
                           "playbackContext": {"contentPlaybackContext": {"html5Preference": "HTML5_PREF_WANTS"}}},
                          separators=(",", ":")).encode()
        hdr = {"Content-Type": "application/json", "Accept-Encoding": "gzip", "Origin": "https://www.youtube.com",
               "User-Agent": str(c.get("userAgent") or "com.google.android.youtube/20.10.38 (Linux; U; Android 11) gzip"),
               "X-YouTube-Client-Name": str(cfg.get("INNERTUBE_CONTEXT_CLIENT_NAME") or 3),
               "X-YouTube-Client-Version": str(c.get("clientVersion") or "")}
        r = pool().request("POST", f"https://{host}/youtubei/v1/player?prettyPrint=false", body=body, headers=hdr,
                           timeout=urllib3.Timeout(connect=1.0, read=2.0), retries=False, preload_content=True)
        if r.status != 200:
            return {"client": client, "verdict": "notready", "reason": f"player HTTP {r.status}"}
        data = json.loads(r.data.decode("utf-8", "replace"))
        prog, adap = _innertube_formats(data)
        pick = pick_formats(prog, adap)
        verdict = classify_playability(data.get("playabilityStatus") or {}, bool(prog or adap))
        dur = float((data.get("videoDetails") or {}).get("lengthSeconds") or 0)
        return {"client": client, "verdict": verdict if (pick or verdict != "ok") else "noformat", "pick": pick,
                "reason": str((data.get("playabilityStatus") or {}).get("reason") or (data.get("playabilityStatus") or {}).get("status") or ""),
                "duration": dur, "title": (data.get("videoDetails") or {}).get("title") or ""}

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
                    results.append(r)
                    if r.get("pick"):
                        r["source"] = f"INNERTUBE_{r['client'].upper()}"
                        r["resolve_ms"] = ms(t0, time.perf_counter_ns())
                        return r
        except FTE:
            pass
        r = self._ytdlp(vid)
        if r and r.get("pick"):
            r["resolve_ms"] = ms(t0, time.perf_counter_ns())
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
    for _ in range(6):
        r = pool().request(method, cur, headers=headers or {}, redirect=False, preload_content=preload,
                           retries=False, timeout=timeout or urllib3.Timeout(connect=2.0, read=8.0))
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


def download_single(url: str, dest: Path, info: dict, cancel: threading.Event | None = None) -> int:
    """1 GET, ghi thẳng vào file tạm. info nhận ttfb_ms/bytes/elapsed_ms."""
    t0 = time.perf_counter_ns()
    r, final = _follow(url)
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
    info.update(ttfb_ms=ms(t0, first or time.perf_counter_ns()), bytes=total, elapsed_ms=ms(t0, time.perf_counter_ns()),
                redirected=final != url)
    return total


def download_ranges(url: str, dest: Path, size: int, n: int, info: dict, cancel=None) -> int:
    """N kết nối Range song song (chỉ khi biết size). Kiểm Content-Range từng đoạn; lỗi → ném ra (caller về 1 GET)."""
    from concurrent.futures import ThreadPoolExecutor
    t0 = time.perf_counter_ns()
    if size <= 0 or n <= 1:
        return download_single(url, dest, info, cancel)
    # dò 302 một lần (không đọc thân; bắt buộc 206 — server bỏ qua Range thì về 1 GET)
    r, final = _follow(url, headers={"Range": "bytes=0-0"}, preload=False)
    st = r.status
    if st == 206:
        r.drain_conn()                                    # đọc hết 1 byte → kết nối dùng lại được an toàn
        r.release_conn()
    else:
        r.close()                                         # server bỏ qua Range (200 cả file) → đóng, không đọc
        raise HttpStatus(st)
    with open(dest, "wb") as f:
        f.truncate(size)
    step = -(-size // n)
    parts = [(i * step, min(size, (i + 1) * step) - 1) for i in range(n) if i * step < size]
    firsts = []

    def one(a, b):
        rr = pool().request("GET", final, headers={"Range": f"bytes={a}-{b}"}, preload_content=False, redirect=False,
                            retries=False, timeout=urllib3.Timeout(connect=2.0, read=8.0))
        try:
            if rr.status != 206:
                raise HttpStatus(rr.status)
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
    info.update(ttfb_ms=ms(t0, min(firsts) if firsts else None), bytes=total, elapsed_ms=ms(t0, time.perf_counter_ns()),
                connections=len(parts))
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
    p = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,height",
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
            "height": (v[0].get("height") if v else None), "vcodec": (v[0].get("codec_name") if v else None),
            "acodec": (a[0].get("codec_name") if a else None)}


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
        self.pending_hub = {}                 # channel → (mode, thời điểm) yêu cầu ta THỰC SỰ gửi tới hub
        self.hub_lock = threading.Lock()
        self.resolver = Resolver(log)
        self.secret = self.db.meta_get("websub_secret") or secrets.token_hex(24)
        self.db.meta_set("websub_secret", self.secret)
        self.threads = []
        self.httpd = None
        self.callback = ""
        self.stats = {"detected": 0, "downloading": 0, "done": 0, "failed": 0, "excel_ok": 0, "excel_pending": 0,
                      "excel_err": 0, "websub_verified": 0, "webhook": "OFF", "api": "OFF", "feed": "OFF",
                      "last": {}, "speed_mbps": 0.0, "pushes": 0}
        self.flags = {"download": bool(cfg.get("download_enabled", True)), "excel": bool(cfg.get("excel_enabled", True)),
                      "api": bool(cfg.get("api_enabled", True)), "feed": bool(cfg.get("feed_enabled", True))}
        self.api_keys = self._load_keys()
        self.key_dead = {}
        self.feed_state = {}
        self.dl_dir = base / cfg.get("download_dir", "DOWNLOADS")
        self.dl_dir.mkdir(parents=True, exist_ok=True)

    # ---------------- vòng đời ---------------- #
    def start(self, websub=True, api=True, feed=True, workers=True, excel_watch=True):
        with self.life_lock:                              # start/stop không bao giờ chạy chồng nhau
            self._acquire_instance_lock()
            self.stop_ev.clear()
            try:
                self._start_inner(websub, api, feed, workers, excel_watch)
            except Exception:
                self.stop_ev.set()
                self._stop_inner()
                raise

    def _start_inner(self, websub, api, feed, workers, excel_watch):
        self.reload_channels(initial=True)
        self.recover()
        self.resolver.warm()
        if workers:
            for i in range(max(1, int(self.cfg.get("workers", 3)))):
                self._spawn(self._dl_worker, f"DL-{i + 1}")
            self._spawn(self._excel_worker, "EXCEL")
        if excel_watch:
            self._spawn(self._excel_watch, "EXCEL-WATCH")
        if websub and self.cfg.get("websub_enabled", True):
            self.start_webhook()
            self._spawn(self._websub_subscriber, "WEBSUB-SUB")
        if api:
            self._spawn(self._api_loop, "API")
        if feed:
            self._spawn(self._feed_thread, "FEED")
        self._spawn(self._stats_loop, "STATS")
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
        self.stop_ev.set()
        with self.life_lock:
            self._stop_inner()

    def _stop_inner(self):
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

    def _load_keys(self):
        p = self.base / self.cfg.get("api_keys_file", "api_keys.txt")
        if not p.exists():
            return []
        return [l.strip() for l in p.read_text(encoding="utf-8-sig").splitlines() if l.strip() and not l.startswith("#")]

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
                if res[0]:
                    self.subscribe_all(list(res[0]))

    def monitored(self, ch):
        with self.ch_lock:
            return ch in self.channels

    # ---------------- XỬ LÝ SỰ KIỆN (đường nóng) ---------------- #
    def on_event(self, ch: str, vid: str, engine: str, published: datetime | None = None, title="",
                 t_recv_ns: int | None = None) -> str:
        """Gọi từ webhook/API/feed. KHÔNG chặn: chỉ chống trùng + đưa vào 2 hàng đợi độc lập."""
        t_recv_ns = t_recv_ns or time.perf_counter_ns()
        if not VID_RE.match(vid or "") or not norm_channel(ch):
            return "invalid"
        if not self.monitored(ch):
            return "foreign"
        if published is not None:
            age = (now_utc() - published).total_seconds()
            if age > float(self.cfg.get("max_age_minutes", 30)) * 60:
                self._mark_seen(vid, ch, f"old:{int(age)}s")
                return "old"
        with self.seen_lock:
            if vid in self.seen:
                return "dup"
            self.seen.add(vid)
        url = video_url(vid)
        meta = {"title": title, "engine": engine, "published": iso(published) if published else None, "detected_at": iso()}
        okey = meta["published"] or meta["detected_at"]  # thứ tự "mới nhất" cho ô D: theo giờ đăng
        # 1) đưa NGAY vào 2 hàng đợi độc lập (worker tự tạo dòng DB nếu tới trước), 2) rồi ghi bền vững SQLite (WAL).
        #    Cửa sổ mất việc khi tắt đột ngột chỉ là vài micro-giây giữa 2 bước.
        t_enq = time.perf_counter_ns()
        if self.flags["download"]:
            self.dlq.put((time.time(), next(self.seq), {"vid": vid, "ch": ch, "t_recv_ns": t_recv_ns, "t_enq_ns": t_enq, **meta}))
        if self.flags["excel"]:
            self.xlq.put({"vid": vid, "ch": ch, "url": url, "t_enq_ns": t_enq, "okey": okey})
        enq_ms = ms(t_recv_ns, t_enq)
        stmts = [("INSERT OR IGNORE INTO videos(video_id, channel_id, url, title, engine, detected_at, published_at, state, "
                  "enqueue_ms) VALUES(?,?,?,?,?,?,?,?,?)",
                  (vid, ch, url, title, engine, meta["detected_at"], meta["published"],
                   "QUEUED" if self.flags["download"] else "SKIPPED", enq_ms))]
        if self.flags["excel"]:
            stmts.append(("INSERT OR IGNORE INTO excel_jobs(video_id, channel_id, url, state, created_at, order_key) "
                          "VALUES(?,?,?,?,?,?)", (vid, ch, url, "PENDING", iso(), okey)))
        self.db.tx(stmts)                                 # 2 bản ghi cùng 1 giao dịch
        self.stats["detected"] += 1
        age_s = f" | tuổi {(now_utc() - published).total_seconds():.1f}s" if published else ""
        self.log(f"🔥 [{engine}] VIDEO MỚI {vid} | kênh {ch} | nhận→hàng đợi {enq_ms:.3f} ms{age_s} | {title[:60]}")
        return "queued"

    def _db_insert_video(self, vid, ch, meta, state, enq_ms=None):
        self.db.x("INSERT OR IGNORE INTO videos(video_id, channel_id, url, title, engine, detected_at, published_at, state, enqueue_ms)"
                  " VALUES(?,?,?,?,?,?,?,?,?)", (vid, ch, video_url(vid), meta.get("title", ""), meta.get("engine", ""),
                                                 meta.get("detected_at") or iso(), meta.get("published"), state, enq_ms))
        if enq_ms is not None:
            self.db.upd(vid, enqueue_ms=enq_ms)

    def _is_recent(self, published) -> bool:
        """Video đăng trong cửa sổ max_age_minutes → coi là MỚI (kể cả lúc lấy mốc ban đầu)."""
        if published is None:
            return False
        return (now_utc() - published).total_seconds() <= float(self.cfg.get("max_age_minutes", 30)) * 60

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
        for p in self.dl_dir.rglob("*.part*"):
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
            final = self.dl_dir / ch / f"{vid}_360p.mp4"
            if final.exists():
                v = ffprobe_info(final)
                if v["ok"]:
                    self.db.upd(vid, state="DONE", file=str(final), size=final.stat().st_size, error=None)
                    self.log(f"✅ {vid} đã có file hợp lệ — không tải lại")
                    return "DONE"
                final.unlink(missing_ok=True)
            final.parent.mkdir(parents=True, exist_ok=True)
            t_start = time.perf_counter_ns()
            k = {}
            if job.get("t_recv_ns"):
                k["resolver_start_ms"] = ms(job["t_recv_ns"], t_start)
            self.db.upd(vid, state="RESOLVING", attempts=attempts + 1, **k)
            tag = f"{vid}.a{attempts + 1}"                # tên tạm RIÊNG cho từng lần thử
            tmpdir = final.parent
            plan = self.resolver.resolve(vid)
            t_res = time.perf_counter_ns()
            pick = plan["pick"]
            self.db.upd(vid, state="DOWNLOADING", resolver_ms=ms(t_start, t_res), kind=pick["kind"],
                        itags=",".join(str(f["itag"]) for f in (pick["video"], pick["audio"]) if f),
                        download_started_at=iso())
            info_v, info_a = {}, {}
            mux_ms = None
            n = int(self.cfg.get("range_connections", 1))
            out_tmp = tmpdir / f".{tag}.part.mp4"
            parts.append(out_tmp)
            if pick["kind"] == "progressive":
                self._fetch(pick["video"], out_tmp, info_v, n, cancel)
            else:
                vtmp = tmpdir / f".{tag}.part.v"
                atmp = tmpdir / f".{tag}.part.a"
                parts += [vtmp, atmp]
                errs = []
                if pick["audio"]:
                    def _a():
                        try:
                            self._fetch(pick["audio"], atmp, info_a, 1, cancel)
                        except Exception as e:  # noqa: BLE001
                            errs.append(e)
                    th = threading.Thread(target=_a, daemon=True, name=f"audio-{vid}")
                    th.start()
                self._fetch(pick["video"], vtmp, info_v, n, cancel)
                if th:
                    th.join()
                    th = None
                    if errs:
                        raise errs[0]
                self.db.upd(vid, state="VERIFYING")
                if not FFMPEG:
                    raise Fatal("cần ffmpeg để ghép/đóng gói 360p DASH")
                mux_ms = ffmpeg_mux(vtmp, atmp if pick["audio"] else None, out_tmp)
            t_dl = time.perf_counter_ns()
            self.db.upd(vid, state="VERIFYING")
            t_v0 = time.perf_counter_ns()
            v = ffprobe_info(out_tmp)
            verify_ms = ms(t_v0, time.perf_counter_ns())
            if not v["ok"]:
                raise IOError(f"ffprobe: file hỏng ({v})")
            if not v.get("audio"):
                self.log(f"⚠️ {vid}: nguồn không có luồng tiếng ({pick['kind']}) — giữ file chỉ có hình")
            if pick["kind"] == "dash" and pick["audio"] and not v.get("audio"):
                raise IOError("ghép xong nhưng không có audio")
            os.replace(out_tmp, final)
            t_end = time.perf_counter_ns()
            size = final.stat().st_size
            dl_ms = ms(t_res, t_dl) - (mux_ms or 0)
            total = ms(job["t_recv_ns"], t_end) if job.get("t_recv_ns") else ms(t_start, t_end)
            mbps = round(size * 8 / 1e6 / max(1e-3, (info_v.get("elapsed_ms") or dl_ms) / 1000), 1)
            self.db.upd(vid, state="DONE", file=str(final), size=size, ttfb_ms=info_v.get("ttfb_ms"), download_ms=dl_ms,
                        mux_ms=mux_ms, verify_ms=verify_ms, total_ms=total, done_at=iso(), error=None, mbps=mbps)
            self.stats["done"] += 1
            self.stats["speed_mbps"] = mbps
            self.stats["last"] = {"vid": vid, "resolver_ms": ms(t_start, t_res), "download_ms": dl_ms, "mux_ms": mux_ms,
                                  "verify_ms": verify_ms, "total_ms": total}
            itags = ",".join(str(f["itag"]) for f in (pick["video"], pick["audio"]) if f)
            self.log(f"✅ DONE {vid} | {pick['kind']} itag {itags} | {size / 1e6:.2f} MB | resolver {ms(t_start, t_res):.0f} ms "
                     f"({plan.get('source')}) | TTFB {info_v.get('ttfb_ms')} ms | tải {dl_ms:.0f} ms ≈{mbps} Mbps | "
                     f"mux {mux_ms or 0:.0f} ms | verify {verify_ms:.0f} ms | TỔNG {total:.0f} ms | "
                     f"{v.get('height')}p {v.get('vcodec')}/{v.get('acodec')}")
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
            self.db.upd(vid, state="RETRY_WAIT", next_try=time.time() + wait, error=f"{type(e).__name__}: {str(e)[:200]}")
            self.log(f"⏳ {vid}: {type(e).__name__}: {redact(str(e))[:160]} → thử lại sau {wait}s (lần {attempts + 1}/{len(sched)})")
            job = dict(job, not_before=time.time() + wait)
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

    def _fetch(self, fmt, dest, info, n, cancel=None):
        size = int(fmt.get("clen") or 0)
        cancel = cancel or self.stop_ev
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
                        eng.db.x("UPDATE subs SET state='DENIED', error=? WHERE channel_id=?", (q.get("hub.reason", "")[:200], chd))
                        eng.log(f"⚠️ WebSub hub TỪ CHỐI kênh {chd}: {q.get('hub.reason', '')[:120]}")
                    return self._reply(200)
                if "hub.challenge" not in q:
                    return self._reply(200, b"WS360 OK")          # kiểm tra sống (health)
                ch = norm_channel(q.get("hub.topic", ""))
                mode = q.get("hub.mode", "")
                # chỉ xác nhận yêu cầu mà CHÍNH TOOL đã gửi (chống giả mạo trạng thái đăng ký từ internet)
                asked = eng.hub_asked(ch, mode) if ch else False
                if mode == "subscribe":
                    ok_mode = asked and eng.monitored(ch)
                elif mode == "unsubscribe":
                    ok_mode = asked and not eng.monitored(ch)    # không để lệnh huỷ cũ cắt đăng ký mới
                else:
                    ok_mode = False
                if ok_mode and len(q["hub.challenge"]) <= 512:
                    if mode == "subscribe":
                        try:
                            lease = int(q.get("hub.lease_seconds") or eng.cfg.get("lease_seconds", 432000))
                        except ValueError:
                            lease = int(eng.cfg.get("lease_seconds", 432000))
                        lease = max(60, min(lease, int(eng.cfg.get("lease_seconds", 432000)), 10 * 86400))
                        eng.db.x("INSERT INTO subs(channel_id, callback, verified_at, lease_seconds, state) VALUES(?,?,?,?,'VERIFIED') "
                                 "ON CONFLICT(channel_id) DO UPDATE SET verified_at=excluded.verified_at, callback=excluded.callback, "
                                 "lease_seconds=excluded.lease_seconds, state='VERIFIED', error=NULL",
                                 (ch, eng.callback, time.time(), lease))
                    else:
                        eng.db.x("UPDATE subs SET state='UNSUBSCRIBED' WHERE channel_id=?", (ch,))
                    return self._reply(200, q["hub.challenge"].encode())   # giữ pending: hub có thể gửi lại xác minh
                return self._reply(404)

            def do_POST(self):
                t_recv = time.perf_counter_ns()
                if urlparse(self.path).path != path:
                    return self._reply(404)
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = -1
                if n <= 0 or n > 256 * 1024:                  # push thật chỉ vài KB
                    return self._reply(413 if n > 0 else 400)
                raw = self.rfile.read(n)
                sig = (self.headers.get("X-Hub-Signature") or "").strip()
                ok = False
                if "=" in sig:
                    algo, _, hexs = sig.partition("=")
                    if algo.lower() in ("sha1", "sha256", "sha384", "sha512"):
                        mac = hmac.new(eng.secret.encode(), raw, getattr(hashlib, algo.lower())).hexdigest()
                        ok = hmac.compare_digest(mac, hexs.lower())
                self._reply(204)                              # ACK ngay — xử lý sau, không bắt hub chờ
                if not ok:
                    eng.log("🚫 WebSub POST sai/thiếu chữ ký HMAC — bỏ qua")
                    return
                eng.handle_push(raw, t_recv)

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
            r = self.on_event(ch, e["video_id"], "WEBSUB", e["published"], e["title"], t_recv_ns)
            if r == "old":
                self.log(f"↩️ WebSub: {e['video_id']} là CẬP NHẬT video cũ (published {e['published']}) — không tải")
            elif r == "foreign":
                self.log(f"↩️ WebSub: kênh {ch} không còn trong Excel — bỏ qua")

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
        return cb

    def hub_asked(self, ch, mode) -> bool:
        with self.hub_lock:
            t = self.pending_hub.get((ch, mode))
        return bool(t and time.time() - t < 6 * 3600)

    def hub_done(self, ch, mode):
        with self.hub_lock:
            self.pending_hub.pop((ch, mode), None)

    def _hub(self, mode, ch, cb):
        with self.hub_lock:
            self.pending_hub[(ch, mode)] = time.time()
            self.pending_hub.pop((ch, "unsubscribe" if mode == "subscribe" else "subscribe"), None)
        data = {"hub.mode": mode, "hub.topic": TOPIC_FMT.format(ch), "hub.callback": cb, "hub.verify": "async"}
        if mode == "subscribe":
            data["hub.secret"] = self.secret
            data["hub.lease_seconds"] = str(int(self.cfg.get("lease_seconds", 432000)))
        r = requests.post(self.cfg["hub_url"], data=data, timeout=(3, 8))
        return r.status_code, r.text[:200]

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
                self.db.x("INSERT INTO subs(channel_id, callback, requested_at, state, error) VALUES(?,?,?,?,?) "
                          "ON CONFLICT(channel_id) DO UPDATE SET callback=excluded.callback, requested_at=excluded.requested_at, "
                          "state=CASE WHEN subs.state='VERIFIED' AND subs.callback=excluded.callback THEN 'VERIFIED' ELSE excluded.state END, "
                          "error=excluded.error", (ch, cb, time.time(), st, None if st != "ERROR" else f"HTTP {code} {body}"))
                ok += st != "ERROR"
            except Exception as e:  # noqa: BLE001
                self.db.x("INSERT INTO subs(channel_id, callback, requested_at, state, error) VALUES(?,?,?,?,?) "
                          "ON CONFLICT(channel_id) DO UPDATE SET error=excluded.error, requested_at=excluded.requested_at, "
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
                self.stop_ev.wait(15)
                continue
            if cb != self.callback:
                self.callback = cb
                if cb != last_cb:
                    self.log(f"🔁 Callback WebSub {'đổi' if last_cb else 'mới'}: {cb} → đăng ký lại toàn bộ")
                    self.db.x("UPDATE subs SET state='STALE' WHERE callback IS NOT ?", (cb,))
                self.db.meta_set("websub_callback", cb)
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
                elif r["state"] in ("REQUESTED", "ERROR", "STALE", "EXPIRED", "UNSUBSCRIBED", "REMOVED", "DENIED") \
                        and since_req > 600:
                    renew.append(r["channel_id"])
            missing = [c for c in self.channels if not self.db.one("SELECT 1 FROM subs WHERE channel_id=?", (c,))]
            if renew or missing:
                self.subscribe_all(renew + missing)
            self.stats["websub_verified"] = self.db.one(
                "SELECT COUNT(*) c FROM subs WHERE state='VERIFIED' AND callback=? AND verified_at + lease_seconds > ?",
                (self.callback, time.time()))["c"]
            self.stats["webhook"] = f"{self.callback}"
            self.stop_ev.wait(30)

    # ---------------- DATA API (dự phòng) ---------------- #
    def _api_loop(self):
        if not self.api_keys or requests is None:
            self.stats["api"] = "TẮT (không có api_keys.txt)"
            return
        ses = requests.Session()
        baseline = set()
        kidx = 0
        while not self.stop_ev.is_set():
            if not self.flags["api"]:
                self.stats["api"] = "TẮT"
                self.stop_ev.wait(2)
                continue
            chans = list(self.channels)
            if not chans:
                self.stop_ev.wait(5)
                continue
            alive = [k for k in self.api_keys if self.key_dead.get(k, 0) < time.time()]
            if not alive:
                self.stats["api"] = "HẾT QUOTA (mọi key)"
                self.stop_ev.wait(60)
                continue
            budget = len(alive) * float(self.cfg.get("api_daily_units_per_key", 8000))
            per_ch = max(float(self.cfg.get("api_min_interval_sec", 15)), len(chans) * 86400.0 / budget)
            gap = per_ch / len(chans)
            self.stats["api"] = f"BẬT | {len(alive)} key | mỗi kênh ~{per_ch:.0f}s"
            for ch in chans:
                if self.stop_ev.is_set() or not self.flags["api"]:
                    break
                key = alive[kidx % len(alive)]
                kidx += 1
                t_send = time.perf_counter_ns()
                try:
                    r = ses.get("https://www.googleapis.com/youtube/v3/playlistItems",
                                params={"part": "contentDetails", "playlistId": "UU" + ch[2:], "maxResults": 5, "key": key,
                                        "fields": "items(contentDetails(videoId,videoPublishedAt))"}, timeout=(2, 5))
                    if r.status_code == 200:
                        for it in r.json().get("items", []):
                            cd = it.get("contentDetails") or {}
                            vid = cd.get("videoId")
                            pub = parse_time(cd.get("videoPublishedAt"))
                            if ch not in baseline and not self._is_recent(pub):
                                self._mark_seen(vid, ch, "baseline-api")
                                continue
                            self.on_event(ch, vid, "API", pub, "", t_send)
                        baseline.add(ch)
                    elif r.status_code == 403 and "quota" in r.text.lower():
                        self.key_dead[key] = time.time() + 3600 * 6
                        self.log(f"⚠️ API key …{key[-4:]} hết quota → nghỉ 6 giờ")
                    elif r.status_code in (403, 429):
                        self.stop_ev.wait(30)
                    elif r.status_code == 404:
                        self.log(f"⚠️ API: kênh {ch} không có danh sách uploads (sai Channel ID?)")
                except Exception as e:  # noqa: BLE001
                    self.stats["api"] = f"LỖI MẠNG {type(e).__name__}"
                    self.stop_ev.wait(5)
                self.stop_ev.wait(gap)

    # ---------------- ATOM FEED POLLING (dự phòng / backfill) ---------------- #
    def _feed_thread(self):
        if httpx is None:
            self.stats["feed"] = "TẮT (thiếu httpx)"
            return
        asyncio.run(self._feed_main())

    async def _feed_main(self):
        lim = httpx.Limits(max_connections=8, max_keepalive_connections=8, keepalive_expiry=60)
        async with httpx.AsyncClient(timeout=httpx.Timeout(6.0, connect=3.0), limits=lim, http2=False,
                                     headers={"User-Agent": f"{VERSION} feed-check"}) as cli:
            sem = asyncio.Semaphore(4)
            nxt = {}
            while not self.stop_ev.is_set():
                if not self.flags["feed"]:
                    self.stats["feed"] = "TẮT"
                    await asyncio.sleep(2)
                    continue
                chans = list(self.channels)
                base_iv = float(self.cfg.get("feed_interval_sec", 60))
                now = time.time()
                due = [c for c in chans if nxt.get(c, 0) <= now]
                for i, ch in enumerate(due):
                    nxt[ch] = now + base_iv + (i % 10)          # rải đều, không dồn cùng lúc

                async def one(ch):
                    async with sem:
                        await self._feed_one(cli, ch)
                if due:
                    await asyncio.gather(*(one(c) for c in due), return_exceptions=True)
                self.stats["feed"] = f"BẬT | {len(chans)} kênh / {base_iv:.0f}s"
                await asyncio.sleep(1.0)

    async def _feed_one(self, cli, ch):
        st = self.feed_state.setdefault(ch, {"etag": None, "lm": None, "base": False, "fail": 0})
        hdr = {}
        if st["etag"]:
            hdr["If-None-Match"] = st["etag"]
        if st["lm"]:
            hdr["If-Modified-Since"] = st["lm"]
        t_send = time.perf_counter_ns()
        try:
            r = await cli.get(FEED_FMT.format(ch), headers=hdr)
        except Exception:
            st["fail"] += 1
            return
        if r.status_code == 304:
            return
        if r.status_code != 200:
            st["fail"] += 1
            if r.status_code == 404 and st["fail"] in (1, 10):
                self.log(f"⚠️ Feed: kênh {ch} trả 404 (sai Channel ID?)")
            return
        st["fail"] = 0
        st["etag"] = r.headers.get("ETag")
        st["lm"] = r.headers.get("Last-Modified")
        try:
            entries = parse_atom(r.content)
        except Exception:
            return
        for e in entries:
            if not e["video_id"]:
                continue
            if not st["base"] and not self._is_recent(e["published"]):
                self._mark_seen(e["video_id"], ch, "baseline-feed")
                continue
            self.on_event(ch, e["video_id"], "FEED", e["published"], e["title"], t_send)
        st["base"] = True

    def _stats_loop(self):
        while not self.stop_ev.wait(1.0):
            if self.on_stats:
                try:
                    s = dict(self.stats)
                    s["channels"] = len(self.channels)
                    self.on_stats(s)
                except Exception:
                    pass


# =============================== GIAO DIỆN PYSIDE6 =============================== #
def run_gui(cfg, base):
    from PySide6.QtCore import QObject, Signal, Qt
    from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
                                   QPlainTextEdit, QCheckBox)

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
    for k, label in (("download", "Auto Download 360p"), ("excel", "Ghi Excel"), ("api", "API fallback"), ("feed", "Polling fallback")):
        c = QCheckBox(label)
        c.setChecked(bool(cfg.get(f"{k}_enabled", True)))
        chk[k] = c
        top.addWidget(c)
    top.addStretch(1)
    lay.addLayout(top)
    grid = QGridLayout()
    labels = {}
    names = [("channels", "Tổng kênh"), ("websub_verified", "WebSub đã xác minh"), ("webhook", "Webhook"),
             ("api", "API"), ("feed", "Polling"), ("detected", "Video mới"), ("downloading", "Đang tải"),
             ("done", "Tải xong"), ("failed", "Lỗi tải"), ("speed_mbps", "Tốc độ (Mbps)"), ("resolver_ms", "Resolver (ms)"),
             ("download_ms", "Tải (ms)"), ("mux_ms", "Mux (ms)"), ("total_ms", "Tổng (ms)"), ("excel", "Excel")]
    for i, (k, t) in enumerate(names):
        grid.addWidget(QLabel(t + ":"), i // 3, (i % 3) * 2)
        lb = QLabel("-")
        lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
        labels[k] = lb
        grid.addWidget(lb, i // 3, (i % 3) * 2 + 1)
    lay.addLayout(grid)
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
        for k in labels:
            v = s.get(k, last.get(k))
            if k == "excel":
                v = f"OK {s.get('excel_ok', 0)} | chờ {s.get('excel_pending', 0)} | lỗi {s.get('excel_err', 0)}"
            labels[k].setText("-" if v is None else str(v))
    br.stats.connect(show)

    def start():
        for k, c in chk.items():
            cfg[f"{k}_enabled"] = c.isChecked()
        e = Engine(cfg, base, log, on_stats=br.stats.emit)
        state["eng"] = e
        for k, c in chk.items():
            c.toggled.connect(lambda v, k=k: e.flags.__setitem__(k, bool(v)))
        b_start.setEnabled(False)
        b_stop.setEnabled(False)                         # STOP chỉ bật khi start() đã xong hẳn

        def _bg():
            try:
                e.start()
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
    br.stopped.connect(lambda: (b_start.setEnabled(True), b_stop.setEnabled(False)))
    br.started.connect(lambda: b_stop.setEnabled(True))
    b_start.clicked.connect(start)
    b_stop.clicked.connect(stop)
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
        eng.start(websub=True, api=False, feed=False)
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
                q = urlencode({"hub.mode": "subscribe", "hub.topic": TOPIC_FMT.format(ch0), "hub.challenge": "pub456"})
                req = urllib.request.Request(f"{cb}?{q}", headers={"ngrok-skip-browser-warning": "1"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    okp = r.read() == b"pub456"
                rec("TEST2b callback HTTPS công khai", "PASS" if okp and cb.startswith("https://") else "FAIL", cb)
            except Exception as e:
                rec("TEST2b callback HTTPS công khai", "FAIL", f"{cb}: {type(e).__name__}: {e}")
            eng.callback = cb
            n = eng.subscribe_all()
            t_end = time.time() + wait_verify
            while time.time() < t_end:
                v = eng.db.one("SELECT COUNT(*) c FROM subs WHERE state='VERIFIED'")["c"]
                if v >= len(chans):
                    break
                time.sleep(2)
            v = eng.db.one("SELECT COUNT(*) c FROM subs WHERE state='VERIFIED'")["c"]
            lease = eng.db.one("SELECT MIN(lease_seconds) a, MAX(lease_seconds) b FROM subs WHERE state='VERIFIED'")
            rec("TEST2c đăng ký hub thật + xác minh", "PASS" if v == len(chans) else ("FAIL" if v == 0 else "PARTIAL"),
                f"gửi {n}/{len(chans)} (HTTP 202) | hub GỌI XÁC MINH {v}/{len(chans)} | lease {lease['a']}–{lease['b']}s")
        # POST hợp lệ / không hợp lệ (qua chính webhook)
        body = (f'<?xml version="1.0"?><feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
                f'xmlns="http://www.w3.org/2005/Atom"><entry><yt:videoId>AAAAAAAAAAA</yt:videoId>'
                f'<yt:channelId>{ch0}</yt:channelId><title>t</title><published>2000-01-01T00:00:00+00:00</published>'
                f'</entry></feed>').encode()
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
        old_ok = eng.db.one("SELECT why FROM seen WHERE video_id='AAAAAAAAAAA'")
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


def main(argv=None):
    ap = argparse.ArgumentParser(description=f"YouTube WebSub Instant 360P {VERSION}")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--video", default="")
    ap.add_argument("--bench", nargs="*")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--config", default="config.json")
    a = ap.parse_args(argv)
    base = BASE_DIR
    os.chdir(base)
    cfg = load_config(base / a.config)
    if a.selftest:
        vid = extract_vid(a.video) if a.video else None
        if a.video and not vid:
            print(f"⚠️ '{a.video}' không phải ID/link video YouTube hợp lệ — bỏ qua TEST3")
        r = selftest(cfg, base, vid)
        return 0 if all(s != "FAIL" for _, s, _ in r) else 1
    if a.bench is not None:
        bench(cfg, base, [v for v in (extract_vid(x) for x in (a.bench or [])) if v], a.reps)
        return 0
    if a.headless:
        log = Log(base / "logs")
        e = Engine(cfg, base, log)
        e.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            e.stop()
        return 0
    return run_gui(cfg, base)


if __name__ == "__main__":
    sys.exit(main())
