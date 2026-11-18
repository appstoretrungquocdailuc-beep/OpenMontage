# -*- coding: utf-8 -*-
# ==============================================================================
# 🛠️ shorwweb_v10fast.py — từ Downloader_Realtime_Layout_V5 (v10fix) — GIAO DIỆN GIỮ NGUYÊN
# 🚀 LẤY LINK: Innertube TRỰC TIẾP (visionos / web_embedded / android_vr chạy đua) trên kết nối TLS GIỮ NÓNG
#    → mục tiêu ~0.1–0.25 s (v10fix2: yt-dlp tạo mới mỗi video ~3 s). yt-dlp (visionos/web_embedded) chỉ còn là DỰ PHÒNG.
# 🛑 ARIA2C 16 LUỒNG: lỗi đọc từ stdout; 403 → link client khác → yt-dlp; không aria2c → Python Range; không 18 → ghép video+audio
# ==============================================================================

import os
import sys
import time
import json
import threading
import shutil
import subprocess
import re
import copy
import urllib.parse
from functools import partial
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

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

try:
    import urllib3
except ImportError:
    urllib3 = None

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
COOKIES_TXT = os.path.join(BASE_DIR, "cookies.txt")

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
FFMPEG_PATH  = find_ffmpeg()
FFPROBE_PATH = find_ffprobe()
YTDLP_PATH   = find_ytdlp()


# ===================== V18: INNERTUBE TRỰC TIẾP + KẾT NỐI GIỮ NÓNG =====================
V18_VERSION = "v18-fast360"
INNERTUBE_BASE = "https://www.youtube.com"          # (test có thể đổi sang máy chủ giả)
V18_CLIENTS = ("visionos", "web_embedded", "android_vr")
V18_PO_GRACE_SEC = 0.15        # client cần PO token thắng trước: chờ thêm tối đa N giây cho client không cần PO token
V18_RESOLVE_DEADLINE_SEC = 1.5
V18_KEEPALIVE_SEC = 25.0
V18_WARM_CONNS = 3
V18_PY_CONNS = 8               # số kết nối khi tải bằng Python (dự phòng aria2c)
V18_UA_CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
_YTID_RE = re.compile(r"(?<![A-Za-z0-9_-])([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")
_FALLBACK_CLIENTS = {
    "android_vr": {"INNERTUBE_CONTEXT": {"client": {"clientName": "ANDROID_VR", "clientVersion": "1.65.10",
                   "deviceMake": "Oculus", "deviceModel": "Quest 3", "androidSdkVersion": 32, "osName": "Android", "osVersion": "12L",
                   "userAgent": "com.google.android.apps.youtube.vr.oculus/1.65.10 (Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip"}},
                   "INNERTUBE_CONTEXT_CLIENT_NAME": 28},
}


def extract_video_id(url: str) -> str:
    try:
        t = (url or "").strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", t):
            return t
        u = urllib.parse.urlparse(t)
        host = (u.netloc or "").lower().split(":", 1)[0]
        path = (u.path or "").strip("/")
        if host.endswith("youtu.be"):
            c = path.split("/", 1)[0]
            return c if re.fullmatch(r"[A-Za-z0-9_-]{11}", c or "") else ""
        q = urllib.parse.parse_qs(u.query or "")
        c = (q.get("v") or [""])[0]
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", c or ""):
            return c
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v") and re.fullmatch(r"[A-Za-z0-9_-]{11}", parts[1] or ""):
            return parts[1]
        m = _YTID_RE.search(t)
        return m.group(1) if m else ""
    except Exception:
        return ""


_CFG_CACHE = {}
_CFG_LOCK = threading.Lock()


def innertube_client(name: str) -> dict:
    """Cấu hình client lấy từ CHÍNH yt-dlp đang cài (luôn đúng version mới nhất); thiếu thì dùng bản dự phòng."""
    with _CFG_LOCK:
        if name in _CFG_CACHE:
            return _CFG_CACHE[name]
    cfg = None
    if yt_dlp is not None:
        for mod_path in ("yt_dlp.extractor.youtube._base", "yt_dlp.extractor.youtube"):
            try:
                mod = __import__(mod_path, fromlist=["INNERTUBE_CLIENTS"])
                cfg = copy.deepcopy(getattr(mod, "INNERTUBE_CLIENTS", {}).get(name))
                if cfg:
                    break
            except Exception:
                cfg = None
    cfg = cfg or copy.deepcopy(_FALLBACK_CLIENTS.get(name) or {})
    with _CFG_LOCK:
        _CFG_CACHE[name] = cfg
    return cfg


def client_needs_po_token(name: str) -> bool:
    """yt-dlp ghi chính sách GVS PO token của từng client: link HTTPS (progressive/adaptive tải thẳng) có bắt buộc không."""
    cfg = innertube_client(name)
    pol = cfg.get("GVS_PO_TOKEN_POLICY")
    if isinstance(pol, dict) and pol:
        for k, v in pol.items():
            if "https" in str(k).lower():
                return bool(getattr(v, "required", False))
    return name in ("android_vr", "android", "ios", "android_music", "ios_music")


_POOL = None
_POOL_LOCK = threading.Lock()


def http_pool():
    """1 PoolManager dùng chung + 1 SSLContext nạp sẵn (urllib3 mặc định nạp kho chứng chỉ cho TỪNG kết nối: 30–100 ms trên Windows)."""
    global _POOL
    if urllib3 is None:
        return None
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                kw = {}
                try:
                    from urllib3.util.ssl_ import create_urllib3_context
                    ctx = create_urllib3_context()
                    ctx.load_default_certs()
                    kw["ssl_context"] = ctx
                except Exception:
                    pass
                _POOL = urllib3.PoolManager(num_pools=16, maxsize=24, block=False, retries=False,
                                            timeout=urllib3.Timeout(connect=2.0, read=8.0), **kw)
    return _POOL


def _fmt_info(f: dict) -> dict:
    mime = str(f.get("mimeType") or "")
    try:
        clen = int(f.get("contentLength") or 0)
    except Exception:
        clen = 0
    return {"itag": str(f.get("itag") or ""), "url": f.get("url") or "", "mime": mime, "clen": clen,
            "height": int(f.get("height") or 0), "bitrate": int(f.get("bitrate") or 0),
            "ext": "webm" if "webm" in mime.lower() else "mp4"}


def pick_360_formats(data: dict) -> dict:
    """itag 18 (360p video+audio 1 file) nếu có link TRỰC TIẾP; không thì video 360p (ưu tiên H.264) + audio track MẶC ĐỊNH."""
    sd = data.get("streamingData") or {}
    prog = [f for f in (sd.get("formats") or []) if isinstance(f, dict)]
    adap = [f for f in (sd.get("adaptiveFormats") or []) if isinstance(f, dict)]
    out = {"prog": None, "video": None, "audio": None, "cipher": 0}
    out["cipher"] = sum(1 for f in prog + adap if not f.get("url"))
    f18 = next((f for f in prog if str(f.get("itag")) == "18" and f.get("url")), None)
    if f18:
        out["prog"] = _fmt_info(f18)
        return out
    vids = [f for f in adap if f.get("url") and str(f.get("mimeType") or "").startswith("video/") and int(f.get("height") or 0) == 360]
    vids.sort(key=lambda f: (0 if "avc1" in str(f.get("mimeType")) else 1, int(f.get("bitrate") or 0)))
    auds = []
    for f in adap:
        if not (f.get("url") and str(f.get("mimeType") or "").startswith("audio/")):
            continue
        tr = f.get("audioTrack") or {}
        if tr and not tr.get("audioIsDefault", False):
            continue                                   # chỉ track gốc (mặc định) — giống itag 18
        if f.get("isDrc"):
            continue
        auds.append(f)
    pref = {"140": 0, "139": 1, "251": 2, "250": 3, "249": 4}
    auds.sort(key=lambda f: pref.get(str(f.get("itag")), 9))
    if vids and auds:
        out["video"], out["audio"] = _fmt_info(vids[0]), _fmt_info(auds[0])
    return out


class FastInnertube360:
    """Race các client Innertube trên kết nối giữ nóng. Kết quả: dict {url | video_url + audio_url, ua, client, ms, alts...}."""

    def __init__(self):
        self.ex = ThreadPoolExecutor(max_workers=8, thread_name_prefix="V18Resolve")
        self._ka_stop = threading.Event()
        self._ka_thread = None
        self.last_warm_ms = None

    # ---- giữ nóng
    def _ping(self):
        p = http_pool()
        r = p.request("GET", f"{INNERTUBE_BASE}/generate_204", headers={"User-Agent": V18_UA_CHROME}, redirect=False,
                      retries=False, preload_content=True, timeout=urllib3.Timeout(connect=2.0, read=3.0))
        return int(r.status)

    def warm(self, k: int = V18_WARM_CONNS):
        """Mở sẵn k kết nối TLS tới YouTube (k = số client chạy đua) + nạp cấu hình client. Gọi lúc mở tool."""
        if urllib3 is None:
            return None
        t = time.perf_counter()
        for c in V18_CLIENTS:
            innertube_client(c)
        futs = [self.ex.submit(self._ping) for _ in range(max(1, k))]
        for f in futs:
            try:
                f.result(timeout=5)
            except Exception:
                pass
        self.last_warm_ms = round((time.perf_counter() - t) * 1000, 1)
        return self.last_warm_ms

    def start_keepalive(self, interval: float = V18_KEEPALIVE_SEC):
        if self._ka_thread and self._ka_thread.is_alive():
            return
        self._ka_stop.clear()

        def loop():
            while not self._ka_stop.wait(interval):
                try:
                    self.warm()
                except Exception:
                    pass
        self._ka_thread = threading.Thread(target=loop, name="V18KeepAlive", daemon=True)
        self._ka_thread.start()

    def stop_keepalive(self):
        self._ka_stop.set()

    # ---- 1 client
    def _player(self, vid: str, client: str) -> dict:
        t0 = time.perf_counter()
        cfg = innertube_client(client)
        if not cfg:
            return {"client": client, "ok": False, "reason": "client không có trong yt-dlp", "ms": 0.0}
        ctx = copy.deepcopy(cfg.get("INNERTUBE_CONTEXT") or {})
        c = ctx.setdefault("client", {})
        c.setdefault("hl", "en")
        c.setdefault("timeZone", "UTC")
        c.setdefault("utcOffsetMinutes", 0)
        ua = str(c.get("userAgent") or V18_UA_CHROME)
        body = json.dumps({"context": ctx, "videoId": vid, "contentCheckOk": True, "racyCheckOk": True,
                           "playbackContext": {"contentPlaybackContext": {"html5Preference": "HTML5_PREF_WANTS"}}},
                          separators=(",", ":")).encode("utf-8")
        hdr = {"Content-Type": "application/json", "Accept-Encoding": "gzip", "Origin": "https://www.youtube.com",
               "User-Agent": ua, "X-YouTube-Client-Name": str(cfg.get("INNERTUBE_CONTEXT_CLIENT_NAME") or 3),
               "X-YouTube-Client-Version": str(c.get("clientVersion") or "")}
        try:
            r = http_pool().request("POST", f"{INNERTUBE_BASE}/youtubei/v1/player?prettyPrint=false", body=body, headers=hdr,
                                    retries=False, preload_content=True, timeout=urllib3.Timeout(connect=1.0, read=1.5))
        except Exception as e:
            return {"client": client, "ok": False, "reason": f"{type(e).__name__}", "ms": round((time.perf_counter() - t0) * 1000, 1)}
        ms = round((time.perf_counter() - t0) * 1000, 1)
        if int(r.status) != 200:
            return {"client": client, "ok": False, "reason": f"player HTTP {r.status}", "ms": ms}
        try:
            data = json.loads(r.data.decode("utf-8", errors="replace"))
        except Exception:
            return {"client": client, "ok": False, "reason": "JSON lỗi", "ms": ms}
        ps = data.get("playabilityStatus") or {}
        st = str(ps.get("status") or "").upper()
        det = data.get("videoDetails") or {}
        pick = pick_360_formats(data)
        ok = bool(pick["prog"] or (pick["video"] and pick["audio"])) and (not st or st == "OK")
        return {"client": client, "ok": ok, "pick": pick, "ua": ua, "ms": ms, "po": client_needs_po_token(client),
                "status": st or "OK", "reason": "" if ok else (str(ps.get("reason") or st or "") or
                                                             f"không có 360p tải thẳng (cipher {pick['cipher']})"),
                "title": det.get("title") or "video", "id": det.get("videoId") or vid}

    # ---- race
    def resolve(self, url: str, deadline: float = V18_RESOLVE_DEADLINE_SEC) -> dict:
        vid = extract_video_id(url)
        if not vid or urllib3 is None:
            return {"ok": False, "reason": "không đọc được video id" if not vid else "thiếu urllib3", "race": []}
        t0 = time.perf_counter()
        futs = {self.ex.submit(self._player, vid, c): c for c in V18_CLIENTS}
        done_list, best, best_po = [], None, None
        t_end = t0 + deadline
        pending = set(futs)
        from concurrent.futures import wait, FIRST_COMPLETED
        while pending:
            now = time.perf_counter()
            if best_po is not None:                     # đã có link cần PO token: chỉ chờ thêm GRACE cho client sạch
                limit = min(t_end, best_po["_t"] + V18_PO_GRACE_SEC)
            else:
                limit = t_end
            if now >= limit:
                break
            done, pending = wait(pending, timeout=limit - now, return_when=FIRST_COMPLETED)
            for f in done:
                try:
                    r = f.result()
                except Exception as e:
                    r = {"client": futs[f], "ok": False, "reason": type(e).__name__, "ms": 0.0}
                r["_t"] = time.perf_counter()
                done_list.append(r)
                if r.get("ok") and not r.get("po") and best is None:
                    best = r
                elif r.get("ok") and r.get("po") and best_po is None:
                    best_po = r
            if best is not None:
                break
        winner = best or best_po
        ms = round((time.perf_counter() - t0) * 1000, 1)
        race = [{"client": r["client"], "ok": r.get("ok"), "ms": r.get("ms"), "po": r.get("po"), "reason": r.get("reason", "")}
                for r in done_list]
        for f, c in futs.items():
            if not any(x["client"] == c for x in race):
                race.append({"client": c, "ok": None, "ms": None, "reason": "chưa trả lời"})
        if winner is None:
            return {"ok": False, "ms": ms, "race": race, "reason": "; ".join(f"{x['client']}: {x['reason']}" for x in race)}
        p = winner["pick"]
        out = {"ok": True, "ms": ms, "client": winner["client"], "source": f"INNERTUBE_{winner['client'].upper()}",
               "po_risky": bool(winner.get("po")), "ua": winner["ua"], "title": winner["title"], "id": winner["id"], "race": race,
               "alt_futs": [(c, f) for f, c in futs.items() if c != winner["client"]]}
        if p["prog"]:
            out.update(mode="prog", itag=p["prog"]["itag"], url=p["prog"]["url"], clen=p["prog"]["clen"])
        else:
            out.update(mode="pair", itag=f"{p['video']['itag']}+{p['audio']['itag']}", video=p["video"], audio=p["audio"],
                       clen=p["video"]["clen"] + p["audio"]["clen"])
        return out


FAST360 = FastInnertube360()


class YtdlpFallbackResolver:
    """DỰ PHÒNG khi Innertube trực tiếp không cho link: yt-dlp với client MẶC ĐỊNH của nó (tự xử lý PO token / chữ ký).
    Dùng visionos/web_embedded (không cần PO token) — đã chạy được trên máy bạn (v10fix2)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._ydl = None

    def _get_ydl(self):
        if self._ydl is None and yt_dlp is not None:
            opts = {"format": "18/b[height<=360][vcodec!=none][acodec!=none]/bv*[height<=360]+ba",
                    "quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 8,
                    "extractor_args": {"youtube": {"player_client": ["visionos", "web_embedded"], "skip": ["hls", "translated_subs"]}}}
            if os.path.exists(COOKIES_TXT):
                opts["cookiefile"] = COOKIES_TXT
            self._ydl = yt_dlp.YoutubeDL(opts)
        return self._ydl

    def resolve(self, url: str, logger=None):
        if yt_dlp is None:
            return None
        t0 = time.perf_counter()
        with self._lock:
            try:
                info = self._get_ydl().extract_info(url, download=False)
            except Exception as e:
                try:
                    if self._ydl:
                        self._ydl.close()
                except Exception:
                    pass
                self._ydl = None
                if logger:
                    logger(f"⚠️ yt-dlp dự phòng lỗi: {str(e).splitlines()[0][:100]}")
                return None
        ms = round((time.perf_counter() - t0) * 1000, 1)
        if not info:
            return None
        base = {"ok": True, "ms": ms, "client": "yt-dlp", "source": "YTDLP", "po_risky": False, "title": info.get("title") or "video",
                "id": info.get("id") or str(int(time.time())), "race": [], "alt_futs": []}
        rf = info.get("requested_formats")
        if rf and len(rf) == 2:
            v = next((f for f in rf if str(f.get("vcodec") or "none") != "none"), rf[0])
            a = next((f for f in rf if f is not v), rf[1])
            mk = lambda f: {"itag": str(f.get("format_id")), "url": f.get("url"), "clen": int(f.get("filesize") or 0),  # noqa: E731
                            "ext": str(f.get("ext") or "mp4"), "headers": f.get("http_headers") or {}}
            base.update(mode="pair", itag=f"{v.get('format_id')}+{a.get('format_id')}", video=mk(v), audio=mk(a),
                        ua=str((v.get("http_headers") or {}).get("User-Agent") or V18_UA_CHROME),
                        clen=int(v.get("filesize") or 0) + int(a.get("filesize") or 0))
        elif info.get("url"):
            base.update(mode="prog", itag=str(info.get("format_id") or ""), url=info["url"], clen=int(info.get("filesize") or 0),
                        ua=str((info.get("http_headers") or {}).get("User-Agent") or V18_UA_CHROME))
        else:
            return None
        return base


YTDLP_FALLBACK = YtdlpFallbackResolver()



# ===================== TIỆN ÍCH FILE & EXCEL =====================
SAFE_NAME_RE = re.compile(r'[\/:*?"<>|\r\n]+')
def sanitize_name(s: str) -> str:
    if not s: return "video"
    s = SAFE_NAME_RE.sub(" ", s).strip()
    return re.sub(r"\s+", " ", s)[:150]

def ensure_dir(p):
    os.makedirs(p, exist_ok=True); return p

def write_status_to_excel(excel_path: str, url_index: int, status_col_name: str, new_status: str, logger):
    try:
        if not load_workbook:
            logger("⚠️ Thiếu 'openpyxl', không ghi STATUS được."); return
        wb = load_workbook(excel_path)
        ws = wb.worksheets[0]
        header = [c.value for c in ws[1]]
        try:
            col_idx = header.index(status_col_name) + 1
        except ValueError:
            logger(f"⚠️ Không thấy cột '{status_col_name}'."); return
        row = url_index + 2
        if row > ws.max_row:
            logger(f"⚠️ Dòng {row} ngoài phạm vi."); return
        ws.cell(row=row, column=col_idx).value = new_status
        temp_path = excel_path + ".tmp"
        
        loop = QEventLoop()
        QTimer.singleShot(0, loop.quit)
        loop.exec()
        
        wb.save(temp_path)
        shutil.move(temp_path, excel_path)
        logger(f"    -> STATUS='{new_status}' @ dòng {row}")
    except Exception as e:
        logger(f"❌ Lỗi ghi Excel: {e}")

def read_links_excel_url_only(excel_path, logger):
    links_with_index=[]; url_col_name=None; status_col_name="STATUS"
    if not pd:
        logger("⚠️ Thiếu 'pandas'. Cài: pip install pandas openpyxl"); return [], None, None
    try:
        if os.path.exists(excel_path):
            df = pd.read_excel(excel_path, sheet_name=0, dtype=str).fillna("")
            cols_upper = {str(c).upper(): c for c in df.columns}
            if "URL" not in cols_upper:
                logger('⚠️ Excel thiếu cột "URL".'); return [], None, None
            url_col_name = cols_upper["URL"]
            if "STATUS" in cols_upper: status_col_name = cols_upper["STATUS"]
            for idx,row in df.iterrows():
                v = str(row[url_col_name]).strip()
                st= str(row.get(status_col_name) or "").upper().strip()
                if v.startswith("http") and st!="DONE":
                    links_with_index.append((v, idx))
    except Exception as e:
        logger(f"⚠️ Lỗi đọc Excel: {e}")
    return links_with_index, url_col_name, status_col_name

def read_excel_cell_value(excel_path: str, url_index: int, col_name: str, logger) -> str:
    try:
        if not pd: return ""
        if not os.path.exists(excel_path): return ""
            
        df = pd.read_excel(excel_path, sheet_name=0, dtype=str).fillna("")
        col_names = {str(c).upper(): c for c in df.columns}
        if col_name.upper() not in col_names: return ""
            
        real_col_name = col_names[col_name.upper()]
        if url_index < 0 or url_index >= len(df): return ""
        return str(df.iloc[url_index][real_col_name]).strip()
    except Exception:
        return ""


def _ffprobe_duration_seconds(ffprobe_path: str, file_path: Path) -> float:
    try:
        r = subprocess.run([FFPROBE_PATH,"-v","error","-show_entries","format=duration",
                            "-of","default=nw=1:nk=1",str(file_path)],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        val = (r.stdout or "").strip()
        return float(val) if val else 0.0
    except Exception:
        return 0.0

def split_video_copy_parallel(ffmpeg_path: str, input_path: Path, out_dir: Path,
                              part_seconds: int, prefix: str, logger):
    try:
        ensure_dir(out_dir)
        dur = _ffprobe_duration_seconds(FFPROBE_PATH, input_path)
        if dur <= 0: 
            logger(f"⚠️ Không đọc được thời lượng: {input_path.name}")
            return False
        if part_seconds <= 0: 
            logger("⚠️ Thời lượng cắt không hợp lệ.")
            return False
        num_parts = int(dur // part_seconds) 
        if num_parts <= 0: 
            logger(f"⚠️ Video quá ngắn ({dur:.1f}s)")
            return False
        logger(f"✂️ Cắt: {num_parts} phần, {part_seconds}s/phần (copy).")
        cmds=[]; safe_prefix=re.sub(r'[\\/:*?"<>|]+','-',prefix or "part")
        
        for i in range(num_parts):
            start=i*part_seconds
            out_file=out_dir / f"{safe_prefix}.part{(i+1):02d}.mp4"
            cmd=[ffmpeg_path,"-y","-ss",str(start),"-i",str(input_path),"-t",str(part_seconds),"-c","copy",str(out_file)]
            cmds.append(cmd)
            
        max_workers=min(max(1,(os.cpu_count() or 4)),8)
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs=[ex.submit(subprocess.run,cmd,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL) for cmd in cmds]
            done=0
            for f in as_completed(futs):
                _=f.result(); done+=1
                if done%2==0 or done==len(cmds): logger(f"    -> {done}/{len(cmds)}")
        logger("✅ Cắt xong.")
        return True
    except Exception as e:
        logger(f"❌ Lỗi cắt: {e}"); return False

_ARIA2_STATUS_RE = re.compile(r"status=(\d{3})")
_ARIA2_CODE_RE = re.compile(r"errorCode=(\d+)")


def parse_aria2_error(text: str) -> dict:
    """aria2c in lỗi ra STDOUT (v17 đọc stderr → log trống). Trả mã HTTP (nếu có), errorCode và dòng lỗi có nghĩa."""
    t = text or ""
    st = _ARIA2_STATUS_RE.findall(t)
    codes = _ARIA2_CODE_RE.findall(t)
    lines = [ln.strip() for ln in t.splitlines() if ("errorCode=" in ln or "Exception" in ln or "->" in ln or "ERROR" in ln)]
    msg = lines[-1] if lines else t.strip().splitlines()[-1] if t.strip() else ""
    msg = re.sub(r"https?://\S+", "<URL>", msg)       # không in link có chữ ký
    return {"http": int(st[-1]) if st else None, "code": int(codes[-1]) if codes else None, "msg": msg[:200]}


def download_aria2c_ultimate(url: str, temp_dir: Path, filename: str, logger, min_bytes: int, http_headers: dict = None,
                             expected: int = 0, info: dict = None) -> bool:
    info = info if isinstance(info, dict) else {}
    temp_file = temp_dir / filename
    for p in (temp_file, Path(str(temp_file) + ".aria2")):
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass
    if not ARIA2C_PATH:
        info.update(fail="no_aria2c")
        logger("    [ARIA2C] ⚠️ Không tìm thấy aria2c.exe")
        return False
    logger(f"    [ARIA2C] 🚀 Đang kéo 16 luồng: {filename}")
    t0 = time.perf_counter()
    try:
        cmd = [ARIA2C_PATH, "-x", "16", "-s", "16", "-k", "1M",
               "--min-split-size=1M", "--console-log-level=error", "--summary-interval=0", "--download-result=hide",
               "--file-allocation=none", "--connect-timeout=8", "--timeout=15", "--max-tries=2", "--retry-wait=1",
               "--auto-file-renaming=false", "--allow-overwrite=true", "-d", str(temp_dir), "-o", filename]
        for hk, hv in (http_headers or {}).items():
            if hk.lower() in ("user-agent", "accept", "accept-language", "referer", "origin") and hv:
                cmd.extend(["--header", f"{hk}: {hv}"])
        cmd.append(url)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        res = subprocess.run(cmd, capture_output=True, text=True, errors="replace", creationflags=flags)
        el = time.perf_counter() - t0
        size = temp_file.stat().st_size if temp_file.exists() else 0
        if res.returncode == 0 and size >= min_bytes and (not expected or size == expected):
            info.update(ok=True, engine="aria2c", bytes=size, ms=round(el * 1000, 1))
            logger(f"    [ARIA2C] ✅ HOÀN TẤT - {size/1024/1024:.1f}MB | {el:.3f}s | ≈{size*8/1e6/max(el,1e-3):.0f}Mbps")
            return True
        err = parse_aria2_error((res.stdout or "") + "\n" + (res.stderr or ""))
        if res.returncode == 0:
            err["msg"] = f"sai dung lượng {size}/{expected or '?'} byte"
        info.update(ok=False, engine="aria2c", http=err["http"], code=err["code"], rc=res.returncode, ms=round(el * 1000, 1),
                    fail=(f"HTTP_{err['http']}" if err["http"] else f"aria2c_rc{res.returncode}"))
        logger(f"    [ARIA2C] ❌ Lỗi Tải sau {el:.2f}s | exit={res.returncode}"
               + (f" | HTTP {err['http']}" if err["http"] else "") + (f" | errorCode={err['code']}" if err["code"] is not None else "")
               + (f" | {err['msg']}" if err["msg"] else ""))
        for p in (temp_file, Path(str(temp_file) + ".aria2")):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
        return False
    except Exception as e:
        info.update(ok=False, engine="aria2c", fail=f"EXC:{type(e).__name__}")
        logger(f"    [ARIA2C] ❌ Lỗi Exception: {e}")
        return False


class HttpStatusError(OSError):
    def __init__(self, status):
        super().__init__(f"HTTP_{status}")
        self.status = int(status)


def _get_follow(url, headers, rng=None, preload=False, timeout=None):
    """GET (+Range) theo 302 thủ công; 4xx/5xx → HttpStatusError."""
    cur = url
    h = dict(headers or {})
    if rng:
        h["Range"] = rng
    for _ in range(6):
        r = http_pool().request("GET", cur, headers=h, redirect=False, retries=False, preload_content=preload,
                                timeout=timeout or urllib3.Timeout(connect=3.0, read=10.0))
        if r.status in (301, 302, 303, 307, 308):
            loc = r.headers.get("Location")
            r.release_conn()
            if not loc:
                raise HttpStatusError(r.status)
            cur = urllib.parse.urljoin(cur, loc)
            continue
        if r.status >= 400:
            r.release_conn()
            raise HttpStatusError(r.status)
        return r, cur
    raise HttpStatusError(310)


def download_python_ranges(url: str, temp_dir: Path, filename: str, logger, min_bytes: int, http_headers: dict = None,
                           expected: int = 0, conns: int = V18_PY_CONNS, info: dict = None) -> bool:
    """Dự phòng khi không dùng được aria2c: N Range song song, kiểm Content-Range + dung lượng; không để file dở."""
    info = info if isinstance(info, dict) else {}
    dest = temp_dir / filename
    t0 = time.perf_counter()
    headers = {"User-Agent": (http_headers or {}).get("User-Agent") or V18_UA_CHROME, "Accept": "*/*", "Accept-Encoding": "identity"}
    try:
        total = int(expected or 0)
        r0, final = _get_follow(url, headers, rng="bytes=0-0", preload=True)
        cr = str(r0.headers.get("Content-Range") or "")
        if r0.status == 206 and "/" in cr and cr.rsplit("/", 1)[1].strip().isdigit():
            total = int(cr.rsplit("/", 1)[1])
        if total <= 0:
            raise OSError("không biết dung lượng")
        n = max(1, min(int(conns), total // (512 * 1024) or 1))
        part = (total + n - 1) // n
        segs = [(i * part, min(total - 1, (i + 1) * part - 1)) for i in range(n)]
        with open(dest, "wb") as f:
            f.truncate(total)
        errs = []
        got = [0]
        lk = threading.Lock()

        def one(seg):
            a, b = seg
            try:
                r, _ = _get_follow(final, headers, rng=f"bytes={a}-{b}")
                crs = str(r.headers.get("Content-Range") or "")
                if r.status != 206 or not crs.startswith(f"bytes {a}-"):
                    r.release_conn()
                    raise OSError(f"Content-Range sai: {r.status} {crs[:40]}")
                pos = a
                with open(dest, "r+b") as fh:
                    fh.seek(a)
                    for chunk in r.stream(256 * 1024):
                        take = min(len(chunk), b + 1 - pos)
                        if take <= 0:
                            break
                        fh.write(chunk[:take])
                        pos += take
                r.release_conn()
                if pos != b + 1:
                    raise OSError(f"thiếu dữ liệu {pos}/{b + 1}")
                with lk:
                    got[0] += b + 1 - a
            except Exception as e:
                with lk:
                    errs.append(e)
        with ThreadPoolExecutor(max_workers=n) as ex:
            list(ex.map(one, segs))
        if errs:
            raise errs[0]
        size = dest.stat().st_size
        if size != total or got[0] != total or size < min_bytes:
            raise OSError(f"sai dung lượng {size}/{total}")
        el = time.perf_counter() - t0
        info.update(ok=True, engine="python", bytes=size, ms=round(el * 1000, 1), conns=n)
        logger(f"    [PY-RANGE] ✅ HOÀN TẤT - {size/1024/1024:.1f}MB | {n} kết nối | {el:.3f}s | ≈{size*8/1e6/max(el,1e-3):.0f}Mbps")
        return True
    except Exception as e:
        st = getattr(e, "status", None)
        info.update(ok=False, engine="python", http=st, fail=(f"HTTP_{st}" if st else f"{type(e).__name__}: {str(e)[:80]}"))
        logger(f"    [PY-RANGE] ❌ {info['fail']}")
        try:
            if dest.exists():
                dest.unlink()
        except OSError:
            pass
        return False


def fetch_media(url, temp_dir, filename, logger, min_bytes, headers, expected=0, info=None) -> bool:
    """aria2c trước; aria2c lỗi KHÔNG phải do máy chủ từ chối (không có aria2c, mạng đứt, sai dung lượng) → Python Range.
    403/404/410/429 → trả False ngay (link bị từ chối: tải lại cùng link chỉ thêm request vô ích)."""
    info = info if isinstance(info, dict) else {}
    if download_aria2c_ultimate(url, temp_dir, filename, logger, min_bytes, headers, expected=expected, info=info):
        return True
    if info.get("http") in (401, 403, 404, 410, 429):
        return False
    return download_python_ranges(url, temp_dir, filename, logger, min_bytes, headers, expected=expected, info=info)


def mux_pair_copy(video_path: Path, audio_path: Path, out_path: Path, logger) -> bool:
    """Ghép video + audio bằng stream-copy (không mã hoá lại), kiểm có đủ 2 luồng."""
    if not FFMPEG_PATH:
        logger("    ❌ Thiếu ffmpeg để ghép video + audio")
        return False
    t0 = time.perf_counter()
    flags = 0x08000000 if os.name == "nt" else 0
    r = subprocess.run([FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video_path), "-i", str(audio_path),
                        "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-movflags", "+faststart", str(out_path)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace", creationflags=flags)
    ok = r.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0
    if ok and FFPROBE_PATH:
        pr = subprocess.run([FFPROBE_PATH, "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(out_path)],
                            capture_output=True, text=True, creationflags=flags)
        kinds = set((pr.stdout or "").split())
        ok = {"video", "audio"} <= kinds
    logger(f"    [MUX] {'✅' if ok else '❌'} ghép stream-copy {time.perf_counter() - t0:.3f}s"
           + ("" if ok else f" | {(r.stderr or '').strip()[-120:]}"))
    return ok


# ===================== GUI WORKER =====================
class WorkerSignals(QObject):
    log_message = Signal(str)
    finished = Signal()
    url_finished = Signal(str, int, bool)

class Worker(threading.Thread):
    MIN_BYTES_VIDEO = 1 * 1024 * 1024 # Nhận diện file 360p từ 1MB

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
        self.temp_dir       = self.out_dir.parent / "_temp_yt360p"
        ensure_dir(self.out_dir); ensure_dir(self.temp_dir)
        self.keep_temp      = self.master.keep_temp_checkbox.isChecked()
        self.final_move_dir = self.master.final_move_entry.text().strip()

        if not (yt_dlp or urllib3):
            self.log("❌ Thiếu cả yt-dlp lẫn urllib3.")
            self._critical_error = True
        else:
            self._critical_error = False
        if not ARIA2C_PATH:
            self.log("⚠️ Không thấy aria2c.exe → tải bằng Python (Range song song).")
            
        self.links_with_index = []

    # --- LẤY LINK 360P: Innertube trực tiếp (race, kết nối nóng) → không được thì yt-dlp dự phòng ---
    def _quick_fetch_native_360p(self, url):
        r = FAST360.resolve(url)
        if r.get("ok"):
            return r
        race = " | ".join(f"{x['client']}: {x.get('reason') or ('OK' if x.get('ok') else '?')}"
                          + (f" ({x['ms']:.0f}ms)" if x.get("ms") else "") for x in r.get("race") or [])
        self.log(f"    ↪ Innertube không cho link 360p ({r.get('ms', 0):.0f}ms): {race or r.get('reason')} → yt-dlp dự phòng")
        return YTDLP_FALLBACK.resolve(url, logger=self.log)

    def _cut_after_merge(self, final_mp4: Path, base_name: str, url_index: int):
        try:
            prefix     = self.master.prefix_entry.text().strip() or "part"
            cutsecs_str= self.master.cutsec_entry.text().strip()
            out_cut_dir= self.master.cutout_entry.text().strip() if self.master.cutout_touched else ""

            # Xử lý số giây cắt
            cutsecs = 0
            if cutsecs_str:
                try: cutsecs = int(cutsecs_str)
                except ValueError: pass
            
            # Chuẩn bị đường dẫn gốc cho thư mục Move
            final_move_base = Path(self.final_move_dir) if self.final_move_dir else None
            if final_move_base:
                ensure_dir(final_move_base)
                
            # Đọc tên thư mục con từ Excel
            link_video_goc = ""
            if url_index is not None and self.excel_path:
                link_video_goc = read_excel_cell_value(self.excel_path, url_index, "LINK VIDEO GỐC", self.log)

            # Đóng gói logic Move thành một hàm nội bộ gọn gàng
            def execute_move(source_path, is_folder=False):
                if not final_move_base or not os.path.exists(source_path):
                    return
                
                # Logic Smart Move: Gom vào thư mục con nếu có
                target_sub_dir = final_move_base / link_video_goc if link_video_goc else final_move_base
                ensure_dir(target_sub_dir)
                
                if link_video_goc:
                    self.log(f"🧠 MOVE THÔNG MINH: {source_path.name} → {target_sub_dir.name}...")
                else:
                    self.log(f"📦 MOVE: {source_path.name} → {target_sub_dir.name}...")

                final_move_path = target_sub_dir / source_path.name
                
                # Chống lặp vòng nếu đích trùng nguồn
                if source_path == final_move_path:
                    self.log(f"⚠️ Đích MOVE trùng nguồn. Bỏ qua.")
                    return

                try:
                    # Chống ghi đè nếu file/thư mục đã tồn tại
                    if os.path.exists(final_move_path):
                        self.log(f"⚠️ Đích đã tồn tại. Thêm timestamp.")
                        name_stem = source_path.stem if not is_folder else source_path.name
                        ext = source_path.suffix if not is_folder else ""
                        final_move_path = target_sub_dir / f"{name_stem}_{int(time.time())}{ext}"
                        
                    shutil.move(str(source_path), str(final_move_path))
                    self.log("✅ MOVE hoàn tất.")
                except Exception as move_e:
                    self.log(f"❌ Lỗi MOVE: {move_e}")

            # Thực thi logic CẮT
            is_cut_success = False
            if cutsecs > 0:
                prefix = re.sub(r'[\\/:*?"<>|]+','-',prefix)
                target_dir_name = f"{base_name}_CUT"
                target_dir = Path(out_cut_dir).joinpath(target_dir_name) if out_cut_dir else Path(final_mp4.parent).joinpath(target_dir_name)
                ensure_dir(target_dir)
                self.log(f"✂️ Cắt vào: {target_dir}")
                
                is_cut_success = split_video_copy_parallel(FFMPEG_PATH, final_mp4, target_dir, cutsecs, prefix, self.log)

            # Quyết định MOVE cái gì dựa vào kết quả CẮT
            if is_cut_success:
                # Cắt thành công -> Bế toàn bộ thư mục chứa các mảnh cắt đi
                execute_move(target_dir, is_folder=True)
            else:
                # Video ngắn (Short) -> Bọc vào thư mục riêng rồi mới Move
                if cutsecs > 0:
                    self.log("⚠️ Video ngắn bỏ qua cắt. Đang bọc vào thư mục riêng...")
                else:
                    self.log("📦 Đang bọc video vào thư mục riêng để các tool khác dễ quét...")
                
                # 1. Khởi tạo một thư mục bọc ngoài mang tên của chính video đó
                wrap_dir = Path(final_mp4.parent).joinpath(base_name)
                ensure_dir(wrap_dir)
                
                # 2. Bỏ file mp4 gốc vào bên trong thư mục vừa tạo
                mp4_inside_wrap = wrap_dir / final_mp4.name
                shutil.move(str(final_mp4), str(mp4_inside_wrap))
                
                # 3. Tiến hành bế toàn bộ thư mục bọc này đi qua Smart Move
                execute_move(wrap_dir, is_folder=True)
            
        except Exception as e:
            self.log(f"❌ Lỗi cắt/move: {e}")

    # --- LOGIC CHỈ TẢI CỨNG 360P BẰNG ARIA2C ---
    def _process_single_url(self, url: str, url_index: int):
        if self._critical_error: 
            self.signals.url_finished.emit(url, url_index, False); return
        if hasattr(self.master, 'realtime_stop_event') and self.master.realtime_stop_event.is_set():
            self.signals.url_finished.emit(url, url_index, False); return
            
        excel_info = f"[Dòng {url_index+2}] " if url_index is not None else ""
        self.log(f"=== [START] {excel_info}{url} ===")

        success = False
        
        # 1. LẤY LINK 360P: Innertube trực tiếp, race client, kết nối giữ nóng
        t_link_start = time.perf_counter()
        quick_data = self._quick_fetch_native_360p(url)
        t_link = time.perf_counter() - t_link_start

        if not quick_data:
            self.log(f"❌ Không lấy được link 360p ({t_link:.2f}s).")
            self.signals.url_finished.emit(url, url_index, False); return

        race = " ".join("%s=%s%s" % (x["client"], "-" if x.get("ms") is None else "%.0fms" % x["ms"], "" if x.get("ok") else "✗")
                        for x in quick_data.get("race") or [])
        self.log(f"⏱️ LẤY LINK [{quick_data.get('source')} · itag {quick_data.get('itag')}]: {t_link:.3f}s ({t_link * 1000:.0f}ms)"
                 + (f" | race: {race}" if race else "")
                 + (" | ⚠️ client cần PO token (dễ bị 403)" if quick_data.get("po_risky") else ""))

        final_title = sanitize_name(quick_data["title"])
        final_vid_id = quick_data["id"]
        base_name = f"{final_title} [{final_vid_id}]"

        out_file_final = self.out_dir / f"{base_name}.mp4"
        combined_file = self.temp_dir / f"{final_vid_id}_360p.mp4"
        tmp_parts = []

        # 2. TẢI: aria2c 16 luồng (Python Range dự phòng). 403/410 → link client khác (mỗi link 1 lần) → yt-dlp.
        t_dl_start = time.perf_counter()
        tried = []
        ok_dl = False
        cand = quick_data
        while cand and not ok_dl:
            tried.append(cand.get("source"))
            hdr = {"User-Agent": cand.get("ua") or V18_UA_CHROME}
            dinfo = {}
            if cand.get("mode") == "pair":
                v, a = cand["video"], cand["audio"]
                vf = self.temp_dir / f"{final_vid_id}_v{v['itag']}.{v.get('ext') or 'mp4'}"
                af = self.temp_dir / f"{final_vid_id}_a{a['itag']}.{'m4a' if (a.get('ext') or 'mp4') == 'mp4' else a.get('ext')}"
                tmp_parts += [vf, af]
                infos = [{}, {}]
                with ThreadPoolExecutor(max_workers=2) as ex2:
                    fv = ex2.submit(fetch_media, v["url"], self.temp_dir, vf.name, self.log, 64 * 1024, hdr, v.get("clen") or 0, infos[0])
                    fa = ex2.submit(fetch_media, a["url"], self.temp_dir, af.name, self.log, 16 * 1024, hdr, a.get("clen") or 0, infos[1])
                    okv, oka = fv.result(), fa.result()
                dinfo = infos[0] if not okv else infos[1]
                ok_dl = bool(okv and oka and mux_pair_copy(vf, af, combined_file, self.log))
            else:
                ok_dl = fetch_media(cand["url"], self.temp_dir, combined_file.name, self.log, self.MIN_BYTES_VIDEO, hdr,
                                    int(cand.get("clen") or 0), dinfo)
            if ok_dl:
                break
            if dinfo.get("http") not in (401, 403, 404, 410):
                break                                    # 429 / lỗi khác: KHÔNG thử link khác (tránh dồn request)
            nxt = None
            for c, fut in cand.get("alt_futs") or []:
                try:
                    r = fut.result(timeout=1.0)
                except Exception:
                    r = None
                if r and r.get("ok") and f"INNERTUBE_{c.upper()}" not in tried:
                    pk = r["pick"]
                    nxt = {"ok": True, "source": f"INNERTUBE_{c.upper()}", "ua": r["ua"], "title": quick_data["title"],
                           "id": quick_data["id"], "alt_futs": [], "po_risky": r.get("po")}
                    if pk["prog"]:
                        nxt.update(mode="prog", itag=pk["prog"]["itag"], url=pk["prog"]["url"], clen=pk["prog"]["clen"])
                    else:
                        nxt.update(mode="pair", itag=f"{pk['video']['itag']}+{pk['audio']['itag']}", video=pk["video"], audio=pk["audio"])
                    break
            if nxt is None and "YTDLP" not in tried:
                nxt = YTDLP_FALLBACK.resolve(url, logger=self.log)
            if nxt is not None:
                self.log(f"    ↪ {cand.get('source')} bị từ chối (HTTP {dinfo.get('http')}) → thử {nxt.get('source')} · itag {nxt.get('itag')}")
            cand = nxt

        if ok_dl:
            self.log(f"⏱️ TẢI XONG TRONG: {time.perf_counter() - t_dl_start:.2f}s")

            # 3. MOVE & HOÀN TẤT
            if os.path.exists(out_file_final): os.remove(out_file_final)
            shutil.move(str(combined_file), str(out_file_final))
            self.log(f"✅ Success: {base_name}.mp4")

            # Gọi lệnh xử lý cắt/move cuối
            self._cut_after_merge(out_file_final, base_name, url_index)
            success = True
        else:
            self.log(f"❌ Tải thất bại (đã thử: {', '.join(str(x) for x in tried)}).")

        for tp in tmp_parts:
            if not self.keep_temp:
                try:
                    if tp.exists():
                        tp.unlink()
                except OSError:
                    pass

        # 4. DỌN DẸP
        if not self.keep_temp and combined_file.exists():
            try: os.remove(combined_file)
            except: pass

        if success and url_index is not None and self.excel_path and self.status_col_name:
            write_status_to_excel(self.excel_path, url_index, self.status_col_name, "DONE", self.log)

        self.log(f"=== [END] {url} (Thành công: {success}) ===")
        self.signals.url_finished.emit(url, url_index, success)
        
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
        self.setWindowTitle("Downloader Tool — Realtime Monitor v10fast (360p Innertube trực tiếp + Aria2c)")
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

        # Mở sẵn kết nối TLS tới YouTube + giữ nóng (ping 25 s/lần) → lấy link chỉ còn 1 vòng mạng
        def _warm():
            ms = FAST360.warm()
            if ms is not None:
                self.log_signal.emit(f"🔥 v10fast: kết nối YouTube đã mở sẵn ({V18_WARM_CONNS} kết nối, {ms:.0f}ms) | client: "
                                     + ", ".join(c + (" (cần PO token)" if client_needs_po_token(c) else "") for c in V18_CLIENTS))
            FAST360.start_keepalive()
        threading.Thread(target=_warm, daemon=True).start()

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
        
        header = QLabel("Downloader Tool — Native 360p Monitor"); header.setObjectName("HeaderLabel"); header.setMinimumHeight(60)
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
        self.radio_auto = QRadioButton("Cứng 360p (Fastest)"); self.radio_auto.setChecked(True)
        self.radio_light = QRadioButton("Ignored")
        self.radio_hard = QRadioButton("Ignored")
        
        h_rad = QHBoxLayout()
        h_rad.addWidget(self.radio_auto)
        h_rad.addStretch()
        sett_layout.addLayout(h_rad)

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
        
        self.realtime_thread = threading.Thread(target=self._realtime_worker, daemon=True)
        self.realtime_thread.start()
        self.logger_qt.log(f"🔄 Bắt đầu chế độ **v10fast: Innertube trực tiếp + kết nối nóng + Aria2c**... Max Threads: {max_workers}")
        FAST360.start_keepalive()

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
        
        while not self.realtime_stop_event.is_set():
            try:
                def _rt_log(m): pass
                links, url_col, status_col = read_links_excel_url_only(excel_path, _rt_log)
                
                new_items = []
                for url, idx in links:
                    if url not in self.seen_urls:
                        new_items.append((url, idx))
                
                if not new_items:
                    time.sleep(1.0)
                    continue
                
                for url, idx in new_items:
                    if self.realtime_stop_event.is_set(): break
                    self.seen_urls.add(url)
                    
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
                    
                    if self.batch_executor:
                        worker = Worker(self, excel_path, url_col, status_col)
                        self.batch_executor.submit(worker._process_single_url, url, idx)
                
                time.sleep(1.0)

            except Exception as e:
                self.log_signal.emit(f"❌ Lỗi Realtime Thread: {e}")
                time.sleep(2.0)
        
        if not self.chk_keeptemp.isChecked():
            tdir = Path(self.input_folder.text().strip()).parent / "_temp_yt360p"
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

    def _save_config(self):
        try:
            if self.light_radio.isChecked(): qm = '720light'
            elif self.hard_radio.isChecked(): qm = '480hard'
            else: qm = 'auto'
            data = {
                "excel_path": self.input_excel.text(), "save_folder": self.input_folder.text(),
                "cut_prefix": self.input_prefix.text(), "cut_seconds": self.input_cutsec.text(),
                "cut_out": self.input_cutout.text(), "final_move": self.input_finalmove.text(),
                "quality_mode": qm, "threads": self.spin_threads.value(), "keep_temp": self.chk_keeptemp.isChecked()
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
                
                qm = data.get("quality_mode", 'auto')
                if qm == '720light': self.radio_light.setChecked(True)
                elif qm == '480hard': self.radio_hard.setChecked(True)
                else: self.radio_auto.setChecked(True)
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

def bench_link(urls, reps=5, gap=2.0, with_v17=True):
    """Đo LẤY LINK trên máy thật (không mở GUI): kết nối nóng như lúc tool chạy, mỗi lượt resolve 1 lần rồi thử ĐÚNG 1 KB của link thắng
    (biết ngay link tải được hay bị 403 — 1 request nhỏ). So với cách v10fix2 (yt-dlp visionos/web_embedded) 1 lần/URL. Không in URL có chữ ký."""
    print(f"{V18_VERSION} | yt-dlp {getattr(getattr(yt_dlp, 'version', None), '__version__', '?')} | python {sys.version.split()[0]}")
    print("client: " + ", ".join(f"{c}{' (CẦN PO token)' if client_needs_po_token(c) else ''}" for c in V18_CLIENTS))
    print(f"mở sẵn kết nối: {FAST360.warm()} ms")
    rows = []
    for url in urls:
        for i in range(reps):
            r = FAST360.resolve(url)
            probe = "-"
            if r.get("ok"):
                u = r["url"] if r["mode"] == "prog" else r["video"]["url"]
                try:
                    resp, _ = _get_follow(u, {"User-Agent": r.get("ua") or V18_UA_CHROME}, rng="bytes=0-1023", preload=True)
                    probe = f"HTTP {resp.status} OK"
                except Exception as e:
                    probe = f"HTTP {getattr(e, 'status', '?')} BỊ TỪ CHỐI" if getattr(e, "status", None) else type(e).__name__
            race = " ".join("%s=%s%s" % (x["client"], "-" if x.get("ms") is None else "%.0fms" % x["ms"], "" if x.get("ok") else "✗")
                            for x in r.get("race") or [])
            print(f"[{extract_video_id(url)}] #{i + 1} LẤY LINK {r.get('ms', 0):.0f} ms | thắng: {r.get('client', '-')} itag {r.get('itag', '-')}"
                  f" | thử 1KB: {probe} | {race}" + ("" if r.get("ok") else f" | {r.get('reason')}"))
            rows.append(r.get("ms") or 0)
            time.sleep(gap)
        if with_v17 and yt_dlp is not None:
            t = time.perf_counter()
            try:
                with yt_dlp.YoutubeDL({"format": "18/best[height<=360][vcodec!=none][acodec!=none]", "quiet": True, "no_warnings": True,
                                       "extractor_args": {"youtube": {"player_client": ["visionos", "web_embedded"]}}}) as y:
                    info = y.extract_info(url, download=False)
                el = (time.perf_counter() - t) * 1000
                probe = "-"
                if info and info.get("url"):
                    try:
                        resp, _ = _get_follow(info["url"], dict(info.get("http_headers") or {}), rng="bytes=0-1023", preload=True)
                        probe = f"HTTP {resp.status} OK"
                    except Exception as e:
                        probe = f"HTTP {getattr(e, 'status', '?')} BỊ TỪ CHỐI"
                print(f"[{extract_video_id(url)}] v10fix2 (yt-dlp visionos/web_embedded, tạo mới như cũ): {el:.0f} ms | thử 1KB: {probe}")
            except Exception as e:
                print(f"[{extract_video_id(url)}] v10fix2 (yt-dlp): lỗi {str(e).splitlines()[0][:100]}")
            time.sleep(gap)
    if rows:
        a = sorted(rows)
        print(f"\nLẤY LINK v10fast: trung vị {a[len(a) // 2]:.0f} ms | nhanh nhất {a[0]:.0f} ms | chậm nhất {a[-1]:.0f} ms | {len(a)} lượt")


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == "--bench-link":
        _urls = [a for a in sys.argv[2:] if not a.startswith("--")]
        if not _urls:
            print("Dùng: python shorwweb_v10fast.py --bench-link URL [URL ...]")
            sys.exit(2)
        bench_link(_urls)
        sys.exit(0)
    # Kiểm tra dependencies cơ bản
    if not (ARIA2C_PATH and (YTDLP_PATH or shutil.which("yt-dlp"))):
        pass 

    app = QApplication(sys.argv)
    w = PremiumDownloaderApp()
    w.show()
    sys.exit(app.exec())