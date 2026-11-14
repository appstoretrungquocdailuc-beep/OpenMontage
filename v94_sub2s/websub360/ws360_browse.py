# -*- coding: utf-8 -*-
"""WS360 — NGUỒN PHÁT HIỆN InnerTube BROWSE (chuyển từ queton V39.0: BrowseEngine / _browse_post_surface /
parse_browse_uploads_json / BrowseGuard / lập lịch theo kênh), nối THẲNG vào Engine.on_event của WS360
(cùng chống trùng SQLite, cùng hàng đợi tải 360p, Excel cột D, TikTok) — không có pipeline thứ hai.

Hai bề mặt:  BROWSE_UPLOADS (browseId = VLUU…, mục tiêu ~0,75 s/kênh)  |  BROWSE_VIDEOS_TAB (tab Videos, ~3 s/kênh).
Nhịp là MỤC TIÊU, bị chặn bởi RPS tổng; 403/429 → tự giảm tốc + nghỉ có kiểm soát (không cố vượt giới hạn dịch vụ).

Phát hiện theo TẬP HỢP: ID có trong danh sách hiện tại mà chưa có trong baseline (lưu SQLite, giữ qua khởi động lại).
XÁC MINH ĐỘ MỚI (không bao giờ tự suy đoán video là mới):
  1) chữ tuổi ngay trong phản hồi Browse ("5 seconds ago") — 0 request thêm;
  2) InnerTube WEB player → microformat publishDate (KHÔNG phải YouTube Data API);
  3) (chỉ chế độ API + BROWSE) YouTube Data API videos.list (tính quota như API scanner);
  không kết luận được → UNVERIFIED: ghi lý do, KHÔNG tải, KHÔNG đăng.
Không dùng yt-dlp/Deno để quét. Không vượt CAPTCHA / giới hạn IP / rate limit. Không ghi key/cookie ra log.
"""
from __future__ import annotations

import collections
import email.utils
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

try:
    import urllib3
except ImportError:  # pragma: no cover
    urllib3 = None

VIDEO_ID_RE = re.compile(r"[0-9A-Za-z_-]{11}")
KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{20,}")
VIDEOS_TAB_PARAMS = "EgZ2aWRlb3PyBgQKAjoA"                 # V39 DUAL_BROWSE_VIDEOS_TAB_PARAMS
DEFAULT_CLIENT_VERSION = "2.20260708.00.00"                # V39 BROWSE_DEFAULT_CLIENT_VERSION
# khoá WEB CÔNG KHAI của InnerTube (nhúng sẵn trong mọi trang youtube.com, không phải khoá của bạn) — như V39
DEFAULT_WEB_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
MAX_RESULTS = 12                                           # V39 WEB_HOT_MAX_RESULTS
SHAPE_CHANGE_UNSEEN = 5                                    # V39 BROWSE_SHAPE_CHANGE_UNSEEN
GLOBAL_THROTTLE_DISTINCT = 2                               # V39 BROWSE_GLOBAL_THROTTLE_DISTINCT
GLOBAL_THROTTLE_WINDOW = 5.0
RECOVER_SUCCESS = 30                                       # V39 BROWSE_RECOVER_SUCCESS
CH_FAIL_THRESHOLD = 3
CH_COOLDOWN_BASE = 5.0
CH_COOLDOWN_MAX = 60.0
JOB_STUCK_SEC = 6.0


RX_RAW_IDS = re.compile(rb'"(?:videoId|contentId)"\s*:\s*"([0-9A-Za-z_-]{11})"')


def raw_ids(raw: bytes) -> set:
    """MỌI Video ID có trong JSON thô (≈1–2 ms / 1,3 MB; đã kiểm: = đúng danh sách video, không lẫn video đề xuất)."""
    return {m.decode() for m in RX_RAW_IDS.findall(raw)}


class ServerClock:
    """Đồng hồ máy chủ Google từ header Date (độ phân giải 1 s): giữ CẬN chặt nhất của độ lệch (server − máy)."""

    def __init__(self, window=120):
        self.lock = threading.Lock()
        self.samples = collections.deque(maxlen=window)

    def note(self, date_hdr, wall_send, wall_recv):
        try:
            d = email.utils.parsedate_to_datetime(date_hdr).timestamp()
        except Exception:
            return
        with self.lock:
            self.samples.append((d - wall_recv, d + 1.0 - wall_send))   # server∈[Date, Date+1) tại một thời điểm trong [send, recv]

    def server_time(self, wall):
        """(ước lượng, ± nửa khoảng) hoặc (None, None)."""
        with self.lock:
            if not self.samples:
                return None, None
            lo = max(a for a, _ in self.samples)
            hi = min(b for _, b in self.samples)
        if hi < lo:                                         # mẫu mâu thuẫn (đồng hồ máy nhảy) → dùng mẫu mới nhất
            lo, hi = self.samples[-1]
        return wall + (lo + hi) / 2, (hi - lo) / 2


class TraceLog:
    """logs/browse_trace.jsonl — 1 dòng / lượt Browse (giờ monotonic + giờ thật + ID trả về). Tự cắt khi > max_mb (giữ 3 file)."""

    def __init__(self, path, max_mb=50):
        self.path = path
        self.max = max_mb * 1024 * 1024
        self.lock = threading.Lock()
        self.fh = None

    def write(self, rec):
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
        with self.lock:
            try:
                if self.fh is None:
                    os.makedirs(os.path.dirname(self.path), exist_ok=True)
                    self.fh = open(self.path, "a", encoding="utf-8", buffering=1)
                self.fh.write(line + "\n")
                if self.fh.tell() > self.max:
                    self.fh.close()
                    self.fh = None
                    for i in (2, 1):
                        if os.path.exists(f"{self.path}.{i}"):
                            os.replace(f"{self.path}.{i}", f"{self.path}.{i + 1}")
                    os.replace(self.path, f"{self.path}.1")
            except OSError:
                self.fh = None

    def close(self):
        with self.lock:
            try:
                if self.fh:
                    self.fh.close()
            except OSError:
                pass
            self.fh = None


def scrub(text) -> str:
    return KEY_RE.sub("AIza***", str(text or ""))


# ------------------------------------------------------------------ PARSER (V38.6/V39 + đọc chữ tuổi)
def _text_from_renderer(value):
    if not isinstance(value, dict):
        return ""
    title = value.get("title")
    if isinstance(title, dict):
        if title.get("simpleText"):
            return str(title.get("simpleText") or "")
        runs = title.get("runs") or []
        if isinstance(runs, list):
            return "".join(str(x.get("text") or "") for x in runs if isinstance(x, dict))
    return ""


def _runs_text(v):
    if isinstance(v, dict):
        if v.get("simpleText"):
            return str(v["simpleText"])
        if isinstance(v.get("runs"), list):
            return "".join(str(x.get("text") or "") for x in v["runs"] if isinstance(x, dict))
        if v.get("content"):
            return str(v["content"])
    return ""


def _lockup_title(node):
    try:
        md = (node.get("metadata") or {}).get("lockupMetadataViewModel") or {}
        t = md.get("title") or {}
        return str(t.get("content") or "") if isinstance(t, dict) else str(t or "")
    except Exception:
        return ""


def _lockup_age_text(node):
    try:
        md = (node.get("metadata") or {}).get("lockupMetadataViewModel") or {}
        rows = (((md.get("metadata") or {}).get("contentMetadataViewModel") or {}).get("metadataRows") or [])
        parts = []
        for r in rows:
            for p in (r.get("metadataParts") or []):
                parts.append(str(((p.get("text") or {}).get("content")) or ""))
        return " • ".join(x for x in parts if x)
    except Exception:
        return ""


def _row_from_browse_item(obj):
    """Một dòng video từ 1 phần tử playlist/grid (V38.6/V39) + chữ tuổi nếu có."""
    if not isinstance(obj, dict):
        return None
    lock = obj.get("lockupViewModel")
    if isinstance(lock, dict) and lock.get("contentType") == "LOCKUP_CONTENT_TYPE_VIDEO":
        vid = str(lock.get("contentId") or "")
        if VIDEO_ID_RE.fullmatch(vid):
            return {"video_id": vid, "title": _lockup_title(lock), "age_text": _lockup_age_text(lock)}
    if obj.get("contentType") == "LOCKUP_CONTENT_TYPE_VIDEO":
        vid = str(obj.get("contentId") or "")
        if VIDEO_ID_RE.fullmatch(vid):
            return {"video_id": vid, "title": _lockup_title(obj), "age_text": _lockup_age_text(obj)}
    for key in ("playlistVideoRenderer", "playlistPanelVideoRenderer", "videoRenderer", "gridVideoRenderer"):
        node = obj.get(key)
        if isinstance(node, dict):
            vid = str(node.get("videoId") or "")
            if VIDEO_ID_RE.fullmatch(vid):
                age = _runs_text(node.get("publishedTimeText")) or _runs_text(node.get("videoInfo"))
                upcoming = bool(node.get("upcomingEventData"))
                return {"video_id": vid, "title": _text_from_renderer(node), "age_text": age, "upcoming": upcoming}
    rich = obj.get("richItemRenderer")
    if isinstance(rich, dict):
        return _row_from_browse_item(rich.get("content") or {})
    shorts = obj.get("shortsLockupViewModel")
    if isinstance(shorts, dict):
        try:
            vid = str((((shorts.get("onTap") or {}).get("innertubeCommand") or {}).get("reelWatchEndpoint") or {}).get("videoId") or "")
            if VIDEO_ID_RE.fullmatch(vid):
                title = str((((shorts.get("overlayMetadata") or {}).get("primaryText") or {}).get("content")) or "")
                return {"video_id": vid, "title": title, "age_text": ""}
        except Exception:
            pass
    return None


def _fast_lists(data):
    """Đường NHANH cho 2 hình dạng đã biết (đỡ tốn CPU/GIL so với quét cả cây); trả None nếu không khớp."""
    try:
        tabs = data["contents"]["twoColumnBrowseResultsRenderer"]["tabs"]
    except (KeyError, TypeError):
        return None
    out = []
    for t in tabs:
        c = ((t.get("tabRenderer") or {}).get("content") or {})
        sl = (c.get("sectionListRenderer") or {}).get("contents") or []
        for sec in sl:
            items = (sec.get("itemSectionRenderer") or {}).get("contents") or []
            for it in items:
                pl = (it.get("playlistVideoListRenderer") or {}).get("contents")
                if isinstance(pl, list):
                    out.append(pl)
            # hình dạng 2026 (đã kiểm trên JSON thật): itemSectionRenderer.contents = danh sách lockupViewModel trực tiếp
            if items and isinstance(items[0], dict) and ("lockupViewModel" in items[0] or "videoRenderer" in items[0]):
                out.append(items)
        rg = (c.get("richGridRenderer") or {}).get("contents")
        if isinstance(rg, list):
            out.append(rg)
    return out or None


def parse_browse_uploads_json(data, anchor_ids=None):
    """V38.1/V39: chọn danh sách video hợp lý nhất (ưu tiên danh sách chứa ID mốc). Trả (rows, how)."""
    anchor_ids = set(anchor_ids or ())

    def rows_of(lst, limit=None):
        rows, seen = [], set()
        for item in lst:
            row = _row_from_browse_item(item)
            if row and row["video_id"] not in seen:
                seen.add(row["video_id"])
                rows.append(row)
                if limit and len(rows) >= limit:                # chỉ cần đầu danh sách (mới nhất)
                    break
        return rows
    fast = _fast_lists(data)
    if fast:
        best = max((rows_of(lst, MAX_RESULTS) for lst in fast), key=len)
        if best:
            return best[:MAX_RESULTS], "fast"
    candidates = []

    def scan_lists(obj):
        if isinstance(obj, list):
            rows = rows_of(obj)
            if rows:
                ai = next((i for i, r in enumerate(rows) if r["video_id"] in anchor_ids), None)
                candidates.append(((1 if ai is not None else 0, -(ai if ai is not None else 10 ** 6), len(rows)), rows))
            for item in obj:
                scan_lists(item)
        elif isinstance(obj, dict):
            for v in obj.values():
                scan_lists(v)
    scan_lists(data)
    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1][:MAX_RESULTS], "scan"
    return [], "none"


_LOCK_MARK = b'"lockupViewModel":{'
_RX_CID = re.compile(rb'"contentId":"([0-9A-Za-z_-]{11})"')
_RX_TITLE = re.compile(rb'"lockupMetadataViewModel":\{"title":\{"content":"((?:[^"\\]|\\.)*)"')
_RX_PARTS = re.compile(rb'"metadataParts":\[(.*?)\]\}', re.S)
_RX_TXT = re.compile(rb'"text":\{"content":"((?:[^"\\]|\\.)*)"')


def fast_rows_from_raw(raw: bytes, limit=MAX_RESULTS):
    """Đọc NHANH 'limit' video đầu từ JSON THÔ (hình dạng lockupViewModel 2026, đã kiểm trên phản hồi thật) mà KHÔNG giải mã
    cả 1,3 MB: chỉ cắt các khối lockupViewModel đầu tiên. Trả None nếu không chắc (→ dùng json.loads đầy đủ).
    Được đối chiếu định kỳ với cách giải mã đầy đủ (BrowseScanner) — lệch thì tự tắt."""
    p = raw.find(b'"contents":')
    if p < 0:
        return None
    pos, starts = p, []
    while len(starts) < limit + 1:
        i = raw.find(_LOCK_MARK, pos)
        if i < 0:
            break
        starts.append(i)
        pos = i + len(_LOCK_MARK)
    if len(starts) < 2:
        return None
    side = raw.find(b'"sidebar":', p)
    rows = []
    for k in range(min(limit, len(starts) - 1 if len(starts) > limit else len(starts))):
        a = starts[k]
        b = starts[k + 1] if k + 1 < len(starts) else min(len(raw), a + 200_000)
        if side > 0 and a > side:
            break
        seg = raw[a:b]
        if b'"contentType":"LOCKUP_CONTENT_TYPE_VIDEO"' not in seg:
            continue
        m = _RX_CID.search(seg)
        if not m:
            return None
        title = ""
        mt = _RX_TITLE.search(seg)
        if mt:
            try:
                title = json.loads(b'"' + mt.group(1) + b'"')
            except ValueError:
                title = ""
        age = ""
        mr = seg.find(b'"metadataRows":[')
        if mr >= 0:
            parts = []
            for grp in _RX_PARTS.findall(seg, mr):           # MỌI hàng metadata (hàng 1: kênh, hàng 2: lượt xem • tuổi)
                for x in _RX_TXT.findall(grp):
                    try:
                        parts.append(json.loads(b'"' + x + b'"'))
                    except ValueError:
                        pass
            age = " • ".join(p_ for p_ in parts if p_)
        rows.append({"video_id": m.group(1).decode(), "title": title, "age_text": age})
    return rows or None


_AGE_RE = re.compile(r"(\d+)\s*(second|sec|minute|min|hour|hr|day|week|month|year)s?\s+ago", re.I)
_UNIT = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400, "week": 604800,
         "month": 2592000, "year": 31536000}


def parse_relative_age(text):
    """'5 seconds ago' → (5, 6); '2 minutes ago' → (120, 180) [cận dưới, cận trên) giây. 'Premieres…/Scheduled…' → 'SCHEDULED'.
    Không có chữ tuổi → None. (Payload Browse đặt hl=en nên chữ là tiếng Anh.)"""
    t = str(text or "")
    if not t:
        return None
    if re.search(r"(?i)\b(premieres?|scheduled|upcoming|waiting)\b", t):
        return "SCHEDULED"
    if re.search(r"(?i)\bjust now\b", t):
        return (0, 60)
    m = _AGE_RE.search(t)
    if not m:
        return None
    n, unit = int(m.group(1)), _UNIT[m.group(2).lower()]
    return (n * unit, (n + 1) * unit)


# ------------------------------------------------------------------ BrowseGuard (V39)
class BrowseGuard:
    def __init__(self, log):
        self.log = log
        self.lock = threading.Lock()
        self.cooldown_until = 0.0
        self.dynamic_rps = 0.0
        self.good_streak = 0
        self.last_log = 0.0
        self.events = collections.deque()
        self.global_trips = 0
        self.channel_trips = 0

    def throttle(self, cid, status, target_rps, n_channels):
        """≥2 kênh KHÁC NHAU bị 429/403 trong 5 s (hoặc chỉ có 1 kênh) → giới hạn theo IP: nghỉ chung + giảm RPS 40%.
        Chỉ 1 kênh bị → cô lập kênh đó, các kênh khác chạy bình thường."""
        now = time.monotonic()
        with self.lock:
            self.events.append((now, cid))
            while self.events and now - self.events[0][0] > GLOBAL_THROTTLE_WINDOW:
                self.events.popleft()
            if len({c for _t, c in self.events}) < GLOBAL_THROTTLE_DISTINCT and n_channels > 1:
                self.channel_trips += 1
                return "CHANNEL"
            self.global_trips += 1
            self.good_streak = 0
            if self.dynamic_rps <= 0:
                self.dynamic_rps = target_rps
            old = self.dynamic_rps
            self.dynamic_rps = max(1.0, self.dynamic_rps * 0.60)
            cooldown = 6.0 if old > 3.0 else 10.0
            self.cooldown_until = max(self.cooldown_until, now + cooldown)
            if now - self.last_log > 3.0:
                self.log(f"⚠️ BROWSE GLOBAL HTTP {status}: nghỉ chung {cooldown:.0f}s | RPS {old:.1f}→{self.dynamic_rps:.1f} (target {target_rps:.1f})")
                self.last_log = now
            return "GLOBAL"

    def success(self, target_rps):
        with self.lock:
            if self.dynamic_rps <= 0:
                self.dynamic_rps = target_rps
            self.good_streak += 1
            if self.dynamic_rps < target_rps and self.good_streak >= RECOVER_SUCCESS:
                self.dynamic_rps = min(target_rps, self.dynamic_rps + max(0.4, target_rps * 0.10))
                self.good_streak = 0

    def current(self, target_rps):
        with self.lock:
            dyn = self.dynamic_rps if self.dynamic_rps > 0 else target_rps
            return max(0.5, min(target_rps, dyn)), self.cooldown_until


class _Surf:
    __slots__ = ("known", "order", "first_ok", "polls", "empty", "last_attempt", "cooldown_until", "cool_n", "fails", "had_rows",
                 "raw_known", "raw_order")

    def __init__(self):
        self.known, self.order = set(), collections.deque()
        self.raw_known, self.raw_order = None, collections.deque()
        self.first_ok = False
        self.polls = self.empty = self.cool_n = self.fails = 0
        self.last_attempt = self.cooldown_until = 0.0
        self.had_rows = False

    def add_raw(self, ids):
        if self.raw_known is None:
            self.raw_known = set()
        for i in ids:
            if i not in self.raw_known:
                self.raw_known.add(i)
                self.raw_order.append(i)
        while len(self.raw_order) > 1500:
            self.raw_known.discard(self.raw_order.popleft())

    def add(self, ids):
        for i in ids:
            if i not in self.known:
                self.known.add(i)
                self.order.append(i)
        while len(self.order) > 400:                       # giới hạn bộ nhớ
            self.known.discard(self.order.popleft())


SURFACES = {"uploads": "BROWSE_UPLOADS", "videos": "BROWSE_VIDEOS_TAB"}


class BrowseScanner:
    def __init__(self, engine, cfg: dict, log, pool_factory=None):
        self.e = engine
        self.cfg = cfg
        self.log = log
        self.stop_ev = threading.Event()
        self.lock = threading.Lock()
        self.url = str(cfg.get("browse_url") or "https://www.youtube.com/youtubei/v1/browse")
        self.player_url = str(cfg.get("browse_player_url") or "https://www.youtube.com/youtubei/v1/player")
        self.key = str(cfg.get("browse_api_key") or "") or DEFAULT_WEB_KEY
        self.cfg_lock = threading.Lock()
        self.client_version = str(cfg.get("browse_client_version") or "") or _yt_web_client_version()
        self.periods = {"uploads": max(0.2, float(cfg.get("browse_uploads_period_sec", 0.75))),
                        "videos": max(0.5, float(cfg.get("browse_videos_period_sec", 3.0)))}
        self.max_rps = {"uploads": max(0.5, float(cfg.get("browse_max_rps", 16.0))),
                        "videos": max(0.25, float(cfg.get("browse_videos_max_rps", 4.0)))}
        self.surfaces = ["uploads"] + (["videos"] if cfg.get("browse_videos_tab", True) else [])
        self.timeouts = (float(cfg.get("browse_connect_timeout", 0.45)), float(cfg.get("browse_read_timeout", 0.95)))
        self.verify_with_api = bool(cfg.get("browse_verify_with_api", True))
        self.guard = BrowseGuard(log)
        self.surf = {s: {} for s in self.surfaces}           # surface → cid → _Surf
        self.inflight = {s: {} for s in self.surfaces}       # surface → cid → (token, t0)
        self.baseline = {}                                   # cid → set(video_id) (SQLite)
        self.pool_factory = pool_factory
        self._pool = None
        self.ex = {}
        self.threads = []
        self.beat = {}
        self.token = 0
        self.clock = ServerClock()
        self.trace = TraceLog(os.path.join(str(getattr(engine, "base", ".")), "logs", "browse_trace.jsonl"),
                              int(cfg.get("browse_trace_max_mb", 50))) if cfg.get("browse_trace", True) else None
        self.pending = {}                                    # vid → chưa xác minh được, sẽ xét lại
        self.audit_sem = threading.Semaphore(2)
        self.first_json = {}                                 # vid → lượt ĐẦU TIÊN có ID trong JSON HTTP 200
        self.first_parsed = {}                               # vid → lượt đầu tiên bộ đọc trả ra ID
        self.seq = 0
        self.track_lock = threading.Lock()                   # seq/first_json dùng chung giữa các luồng quét
        self.default_endpoint = not cfg.get("browse_url")
        self.cfg_fixed = bool(cfg.get("browse_api_key")) or bool(cfg.get("browse_client_version"))
        self.resp_ms = collections.deque(maxlen=4000)
        self.parse_ms = collections.deque(maxlen=4000)
        self.fast_raw = bool(cfg.get("browse_fast_parse", True))
        self.verify_every = max(1, int(cfg.get("browse_fast_verify_every", 50)))
        self.raw_n = 0
        self.stats = collections.Counter()
        self.last_stats_log = time.monotonic()
        self.status = "TẮT"
        self._init_db()

    # ---------------------------------------------------------------- SQLite (cùng database của WS360)
    def _init_db(self):
        db = self.e.db
        db.x("CREATE TABLE IF NOT EXISTS browse_seen(channel_id TEXT, video_id TEXT, at REAL, PRIMARY KEY(channel_id, video_id))")
        db.x("CREATE TABLE IF NOT EXISTS browse_verify(video_id TEXT PRIMARY KEY, channel_id TEXT, source TEXT, verdict TEXT, "
             "method TEXT, age_lo REAL, age_hi REAL, reason TEXT, at TEXT, response_ms REAL)")
        db.x("CREATE TABLE IF NOT EXISTS browse_timeline(video_id TEXT PRIMARY KEY, channel_id TEXT, data TEXT)")
        db.x("CREATE TABLE IF NOT EXISTS browse_stats(ts REAL, polls INTEGER, p50 REAL, p90 REAL, p95 REAL, n403 INTEGER, "
             "n429 INTEGER, nerr INTEGER, nparse INTEGER, fresh INTEGER, old INTEGER, unverified INTEGER)")
        for r in db.all("SELECT channel_id, video_id FROM browse_seen"):
            self.baseline.setdefault(r["channel_id"], set()).add(r["video_id"])

    def _remember(self, cid, ids):
        new = [i for i in ids if i not in self.baseline.setdefault(cid, set())]
        if not new:
            return
        self.baseline[cid].update(new)
        now = time.time()
        try:
            self.e.db.tx([("INSERT OR IGNORE INTO browse_seen(channel_id, video_id, at) VALUES(?,?,?)", (cid, i, now)) for i in new])
            if len(self.baseline[cid]) > 300:                # giới hạn: giữ 200 ID mới nhất mỗi kênh
                self.e.db.x("DELETE FROM browse_seen WHERE channel_id=? AND video_id NOT IN (SELECT video_id FROM browse_seen "
                            "WHERE channel_id=? ORDER BY at DESC LIMIT 200)", (cid, cid))
                self.baseline[cid] = {r["video_id"] for r in self.e.db.all("SELECT video_id FROM browse_seen WHERE channel_id=?", (cid,))}
        except Exception as e:  # noqa: BLE001
            self.log(f"⚠️ BROWSE: lưu baseline lỗi {type(e).__name__}")

    # ---------------------------------------------------------------- HTTP
    def pool(self):
        if self._pool is None:
            if self.pool_factory is not None:
                self._pool = self.pool_factory()
            else:
                try:
                    import ws360_net as NET
                    self._pool = NET.make_pool("fast")       # pool RIÊNG: không chiếm kết nối ấm của resolver tải video
                except Exception:
                    self._pool = urllib3.PoolManager(num_pools=4, maxsize=16, retries=False)
        return self._pool

    def _headers(self):
        return {"User-Agent": UA, "Accept": "application/json", "Accept-Language": "en-US,en;q=0.8", "Accept-Encoding": "gzip, deflate",
                "Content-Type": "application/json", "Origin": "https://www.youtube.com", "Cache-Control": "no-cache",
                "Pragma": "no-cache", "X-YouTube-Client-Name": "1", "X-YouTube-Client-Version": self.client_version}

    def refresh_config(self):
        """Như V39 _refresh_browse_config: đọc INNERTUBE_API_KEY + INNERTUBE_CLIENT_VERSION mới nhất từ trang chủ YouTube
        (luồng nền, 10 phút/lần; lỗi thì giữ giá trị cũ). Không chạy khi bạn tự đặt key/version hoặc dùng máy chủ khác."""
        if not self.default_endpoint or self.cfg_fixed:
            return
        try:
            r = self.pool().request("GET", "https://www.youtube.com/", headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"},
                                    retries=False, preload_content=True, timeout=urllib3.Timeout(connect=1.0, read=3.0))
            if r.status != 200:
                return
            t = r.data.decode("utf-8", "replace")
            mk = re.search(r'"INNERTUBE_API_KEY"\s*:\s*"([^"]+)"', t)
            mv = re.search(r'"INNERTUBE_CLIENT_VERSION"\s*:\s*"([^"]+)"', t)
            with self.cfg_lock:
                if mk:
                    self.key = mk.group(1)
                if mv:
                    self.client_version = mv.group(1)
            self.stats["cfg_refresh"] += 1
        except Exception:
            pass

    def _refresh_soon(self):
        now = time.monotonic()
        with self.cfg_lock:
            if now - getattr(self, "_last_forced_refresh", -1e9) < 30:
                return
            self._last_forced_refresh = now
        threading.Thread(target=self.refresh_config, daemon=True, name="BROWSE-CFG-NOW").start()

    def _cfg_loop(self):
        self.refresh_config()
        while not self.stop_ev.wait(600):
            self.refresh_config()

    def browse(self, cid, surface):
        """1 POST InnerTube browse (payload V39). Trả dict(status, rows, ms, t_recv_ns, err, how, parse_ms)."""
        payload = {"context": {"client": {"clientName": "WEB", "clientVersion": self.client_version, "hl": "en", "gl": "US"},
                               "user": {"lockedSafetyMode": False}, "request": {"useSsl": True}}}
        if surface == "videos":
            payload["browseId"] = cid
            payload["params"] = VIDEOS_TAB_PARAMS
        else:
            payload["browseId"] = "VLUU" + cid[2:]
        w0 = time.time()
        t0 = time.perf_counter_ns()
        with self.cfg_lock:
            key = self.key
        try:
            r = self.pool().request("POST", f"{self.url}?key={key}&prettyPrint=false", body=json.dumps(payload).encode(),
                                    headers=self._headers(), retries=False, preload_content=True,
                                    timeout=urllib3.Timeout(connect=self.timeouts[0], read=self.timeouts[1]))
        except Exception as e:  # noqa: BLE001 — chỉ TÊN lỗi (URL có key)
            t1 = time.perf_counter_ns()
            kind = "timeout" if "Timeout" in type(e).__name__ else "net"
            return {"status": -1, "rows": [], "ms": (t1 - t0) / 1e6, "t_send_ns": t0, "t_recv_ns": t1, "wall_send": w0,
                    "wall_recv": time.time(), "err": f"{kind}:{type(e).__name__}"}
        t1 = time.perf_counter_ns()
        w1 = time.time()
        date = r.headers.get("Date")
        if date:
            self.clock.note(date, w0, w1)
        out = {"status": r.status, "rows": [], "ms": (t1 - t0) / 1e6, "t_send_ns": t0, "t_recv_ns": t1, "wall_send": w0,
               "wall_recv": w1, "date": date, "bytes": len(r.data or b""), "err": ""}
        if r.status == 200:
            out["raw"] = r.data
            out["raw_ids"] = raw_ids(r.data)
            tp = time.perf_counter_ns()
            try:
                rows, how = None, ""
                if self.fast_raw:
                    rows = fast_rows_from_raw(r.data)
                    how = "raw"
                    self.raw_n += 1
                    if rows is not None and self.raw_n % self.verify_every == 0:   # đối chiếu định kỳ với giải mã đầy đủ
                        full, _h = parse_browse_uploads_json(json.loads(r.data), anchor_ids=self.baseline.get(cid))
                        if [x["video_id"] for x in full] != [x["video_id"] for x in rows] or \
                                [x.get("age_text") for x in full] != [x.get("age_text") for x in rows]:
                            self.fast_raw = False
                            self.stats["fast_raw_mismatch"] += 1
                            self.log("⚠️ BROWSE: bộ đọc nhanh lệch với giải mã đầy đủ → TẮT bộ đọc nhanh, dùng json đầy đủ")
                            rows, how = full, "json(verify)"
                if rows is None:
                    rows, how = parse_browse_uploads_json(json.loads(r.data), anchor_ids=self.baseline.get(cid))
                out.update(rows=rows, how=how)
            except Exception as e:  # noqa: BLE001
                out.update(err=f"PARSE:{type(e).__name__}")
            out["parse_ms"] = (time.perf_counter_ns() - tp) / 1e6
            self.parse_ms.append(out["parse_ms"])
        return out

    def player_publish(self, vid):
        """InnerTube WEB player → microformat publishDate (KHÔNG phải Data API). Trả (datetime|None, precise:bool, lý do)."""
        body = {"context": {"client": {"clientName": "WEB", "clientVersion": self.client_version, "hl": "en", "gl": "US"}},
                "videoId": vid, "contentCheckOk": True, "racyCheckOk": True}
        try:
            r = self.pool().request("POST", f"{self.player_url}?key={self.key}&prettyPrint=false", body=json.dumps(body).encode(),
                                    headers=self._headers(), retries=False, preload_content=True,
                                    timeout=urllib3.Timeout(connect=1.0, read=2.5))
        except Exception as e:  # noqa: BLE001
            return None, False, f"player lỗi mạng {type(e).__name__}"
        if r.status != 200:
            return None, False, f"player HTTP {r.status}"
        try:
            d = json.loads(r.data)
        except ValueError:
            return None, False, "player JSON lỗi"
        mf = (d.get("microformat") or {}).get("playerMicroformatRenderer") or {}
        raw = str(mf.get("publishDate") or mf.get("uploadDate") or "")
        if not raw:
            st = str((d.get("playabilityStatus") or {}).get("status") or "")
            return None, False, f"player không có publishDate (playability {st})"
        try:
            p = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None, False, f"publishDate lạ '{raw[:30]}'"
        precise = "T" in raw
        if p.tzinfo is None:
            p = p.replace(tzinfo=timezone.utc)
        return p, precise, f"publishDate {raw[:25]}"

    # ---------------------------------------------------------------- xác minh độ mới
    def verify(self, cid, vid, row, source, ms_, allow_remote=True):
        """→ (verdict FRESH|OLD|UNVERIFIED, published|None, age_lo, age_hi, method, reason). Không bao giờ đoán 'mới'."""
        lim = float(self.cfg.get("max_age_minutes", 30)) * 60
        now = datetime.now(timezone.utc)
        ra = parse_relative_age((row or {}).get("age_text"))
        if (row or {}).get("upcoming") or ra == "SCHEDULED":
            return "UNVERIFIED", None, None, None, "ROW_TEXT", "video sắp công chiếu/hẹn giờ — chưa phát được"
        if isinstance(ra, tuple):
            lo, hi = ra
            if hi <= lim:
                return "FRESH", now - timedelta(seconds=lo), lo, hi, "ROW_TEXT", f"'{row.get('age_text')[:40]}'"
            if lo > lim:
                return "OLD", now - timedelta(seconds=lo), lo, hi, "ROW_TEXT", f"'{row.get('age_text')[:40]}'"
        if not allow_remote:
            return "UNVERIFIED", None, None, None, "ROW_TEXT", "chưa có chữ tuổi (chờ lịch thử player)"
        p, precise, why = self.player_publish(vid)
        if p is not None:
            age = (now - p).total_seconds()
            if precise:
                if age <= lim:
                    return "FRESH", p, max(0.0, age), max(0.0, age), "PLAYER", why
                return "OLD", p, age, age, "PLAYER", why
            if age > 2 * 86400:                               # chỉ có NGÀY: chỉ kết luận được 'cũ' khi đã qua ≥ 2 ngày
                return "OLD", p, age, age, "PLAYER", why + " (chỉ có ngày)"
            why += " (chỉ có ngày — không đủ để biết ≤ 30 phút)"
        if self.verify_with_api and getattr(self.e, "api", None) is not None:
            p2, why2 = self.e.api.video_published(vid) if hasattr(self.e.api, "video_published") else (None, "API không hỗ trợ")
            if p2 is not None:
                age = (now - p2).total_seconds()
                return ("FRESH" if age <= lim else "OLD"), p2, age, age, "DATA_API", why2
            why += f"; {why2}"
        return "UNVERIFIED", None, None, None, "NONE", why

    # ---------------------------------------------------------------- xử lý 1 phản hồi
    def _channels(self):
        with self.e.ch_lock:
            return list(self.e.channels)

    def _s(self, surface, cid) -> _Surf:
        d = self.surf[surface]
        if cid not in d:
            d[cid] = _Surf()
        return d[cid]

    def _track(self, cid, surface, res, st):
        """Ghi lượt quét + tìm lượt ĐẦU TIÊN mỗi ID mới có trong JSON + kiểm bộ đọc nhanh có bỏ sót ID mới không."""
        with self.track_lock:
            self.seq += 1
            seq = self.seq
        rid = res.get("raw_ids") or set()
        new_raw = set()
        if st.raw_known is None:
            st.add_raw(rid)                                  # lượt đầu của bề mặt này: chỉ làm mốc
        else:
            new_raw = rid - st.raw_known
            st.add_raw(rid)
        for vid in new_raw:
            with self.track_lock:
                if vid not in self.first_json:
                    self.first_json[vid] = {"t_ns": res["t_recv_ns"], "wall": res["wall_recv"], "surface": SURFACES[surface],
                                            "skey": surface, "cid": cid, "poll": st.polls, "seq": seq, "http_ms": round(res["ms"], 1)}
        parsed = {r["video_id"] for r in res["rows"]}
        miss = new_raw - parsed
        if miss and res.get("how") == "raw" and res.get("raw"):
            # ID mới có trong JSON mà bộ đọc nhanh không trả → giải mã ĐẦY ĐỦ ngay lượt này để chắc chắn không bỏ sót
            full, _h = parse_browse_uploads_json(json.loads(res["raw"]), anchor_ids=self.baseline.get(cid))
            lost = miss & {x["video_id"] for x in full}
            if lost:
                self.stats["fast_miss"] += 1
                self.fast_raw = False
                self.log(f"❗ BROWSE_FAST_MISS {cid} [{SURFACES[surface]}]: bộ đọc nhanh BỎ SÓT {sorted(lost)} (có trong giải mã đầy đủ) "
                         f"→ dùng kết quả đầy đủ cho lượt này và TẮT bộ đọc nhanh")
                res["rows"], res["how"] = full, "json(fast-miss)"
                parsed = {r["video_id"] for r in full}
        for vid in parsed:
            if vid in self.first_json and vid not in self.first_parsed:
                self.first_parsed[vid] = {"t_ns": res["t_recv_ns"], "wall": res["wall_recv"], "surface": SURFACES[surface],
                                          "poll": st.polls, "seq": seq, "how": res.get("how")}
        for d in (self.first_json, self.first_parsed):
            if len(d) > 5000:
                for k in list(d)[:1000]:
                    d.pop(k, None)
        if self.trace is not None:
            self.trace.write({"seq": seq, "wall_send": round(res["wall_send"], 3), "wall_recv": round(res["wall_recv"], 3),
                              "mono_send_ns": res["t_send_ns"], "mono_recv_ns": res["t_recv_ns"], "ch": cid, "surf": SURFACES[surface],
                              "st": res["status"], "ms": round(res["ms"], 1), "bytes": res.get("bytes"), "date": res.get("date"),
                              "how": res.get("how"), "parse_ms": round(res.get("parse_ms") or 0, 2),
                              "top": [r["video_id"] for r in res["rows"]], "new_in_json": sorted(new_raw), "n_json_ids": len(rid)})

    def _process(self, cid, surface, res):
        st = self._s(surface, cid)
        source = SURFACES[surface]
        st.polls += 1
        self._track(cid, surface, res, st)
        rows = res["rows"]
        ids = [r["video_id"] for r in rows]
        if not ids:
            st.empty += 1
            if st.had_rows and st.empty in (3, 30, 300):
                self.stats["shape_empty"] += 1
                self.log(f"❗ BROWSE_SHAPE_EMPTY {cid} [{source}]: {st.empty} lần liên tiếp 0 video dù trước đó có — cấu trúc JSON "
                         f"có thể đã đổi (KHÔNG coi là 'không có video mới')")
            return
        st.empty = 0
        st.had_rows = True
        by = {r["video_id"]: r for r in rows}
        for vid in [i for i in ids if i in self.pending]:      # chưa xác minh được lúc trước → xét lại (chữ tuổi có thể vừa hiện)
            self._candidate(cid, vid, by[vid], source, res, recheck=True)
        base = self.baseline.get(cid, set())
        unseen = [i for i in ids if i not in st.known and i not in base]
        cand = [i for i in unseen if i not in self.e.seen]
        for vid in unseen:                                     # nguồn khác (API/WebSub) đã thắng vừa xong → chỉ ghi nhận race
            if vid in self.e.seen and vid in self.e.race:
                self.e.on_event(cid, vid, "BROWSE", None, "", res["t_recv_ns"],
                                extra={"browse_source": SURFACES[surface], "browse_ms": round(res["ms"], 1)})
        if not st.first_ok:
            st.first_ok = True
            if base and cand and surface == "uploads":
                self.log(f"🧭 BROWSE RESTART_GAP {cid}: {len(cand)} ID chưa ghi nhận (đăng lúc tool tắt?) → xác minh từng ID")
            elif not base:
                # khởi động LẦN ĐẦU (chưa có baseline lưu): mọi ID làm mốc, TRỪ dòng có chữ tuổi chứng minh ≤ max_age (vd video vừa
                # đăng ngay trước START) — chỉ đọc chữ có sẵn, 0 request thêm; không có chữ tuổi → làm mốc (không đoán là mới)
                lim = float(self.cfg.get("max_age_minutes", 30)) * 60
                fresh = [r["video_id"] for r in rows if isinstance(parse_relative_age(r.get("age_text")), tuple)
                         and parse_relative_age(r.get("age_text"))[1] <= lim and r["video_id"] not in self.e.seen]
                st.add(ids)
                self._remember(cid, ids)
                for vid in reversed(fresh) if surface == "uploads" else []:
                    self._candidate(cid, vid, by[vid], SURFACES[surface], res)
                return
            else:
                cand = cand if surface == "uploads" else []    # tab Videos lần đầu: chỉ làm mốc
        if len(cand) >= SHAPE_CHANGE_UNSEEN:
            self.stats["shape_change"] += 1
            self.log(f"🧹 BROWSE SHAPE_CHANGE {cid} [{source}]: {len(cand)} ID lạ cùng lúc → xác minh từng ID (không ghi bừa)")
        st.add(ids)
        self._remember(cid, ids)
        for vid in reversed(cand):                             # cũ → mới
            self._candidate(cid, vid, by.get(vid) or {}, source, res)

    PLAYER_RETRY = (2, 5, 10, 30, 60)

    def _candidate(self, cid, vid, row, source, res, recheck=False):
        t_cand = time.perf_counter_ns()
        if vid in self.e.seen:                                 # vừa được nguồn khác nhận trong lúc ta xác minh
            self.pending.pop(vid, None)
            return
        pend = self.pending.get(vid)
        now_m = time.monotonic()
        allow_remote = pend is None or now_m >= pend["next"]   # xét lại: chữ tuổi mỗi lượt (0 request); player theo lịch giãn dần
        verdict, pub, lo, hi, method, why = self.verify(cid, vid, row, source, res["ms"], allow_remote=allow_remote)
        t_ver = time.perf_counter_ns()
        if verdict == "UNVERIFIED":
            if pend is None:
                pend = self.pending[vid] = {"t0": now_m, "n": 0, "next": now_m + self.PLAYER_RETRY[0], "cid": cid}
            elif allow_remote:
                pend["n"] += 1
                pend["next"] = now_m + self.PLAYER_RETRY[min(pend["n"], len(self.PLAYER_RETRY) - 1)]
            final = now_m - pend["t0"] > float(self.cfg.get("browse_unverified_window_sec", 900))
            if recheck and not final:
                return                                         # vẫn chưa chắc: im lặng chờ lượt sau
            if final:
                self.pending.pop(vid, None)
                verdict = "UNVERIFIED_FINAL"
        else:
            self.pending.pop(vid, None)
        self.stats[f"verdict_{'UNVERIFIED' if verdict.startswith('UNVERIFIED') else verdict}"] += 1
        self.e.db.x("INSERT OR REPLACE INTO browse_verify(video_id, channel_id, source, verdict, method, age_lo, age_hi, reason, at, "
                    "response_ms) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (vid, cid, source, verdict, method, lo, hi, scrub(why)[:200], datetime.now(timezone.utc).isoformat(), round(res["ms"], 1)))
        if verdict == "FRESH":
            txt = (row or {}).get("age_text") or ""
            self.log(f"🌐 BROWSE_DETECTED {vid} | source={source} | row_text='{txt[-40:]}' (chữ của YouTube, KHÔNG phải tuổi đo được) | "
                     f"xác minh {method} | BROWSE_RESPONSE_MS={res['ms']:.0f} | kênh {cid}" + (" | sau khi xét lại" if recheck else ""))
            r = self.e.on_event(cid, vid, "BROWSE", pub, row.get("title") or "", res["t_recv_ns"],
                                extra={"browse_source": source, "age_s": lo, "browse_ms": round(res["ms"], 1)})
            t_q = time.perf_counter_ns()
            self.stats[f"event_{r}"] += 1
            self._timeline(cid, vid, source, res, t_cand, t_ver, t_q, method, r)
        elif verdict == "OLD":
            self.log(f"↩️ BROWSE {vid} [{source}] kênh {cid}: video CŨ ({method}: {scrub(why)}) — không tải, không đăng")
        elif verdict == "UNVERIFIED_FINAL":
            self.log(f"❔ BROWSE_UNVERIFIED_FINAL {vid} [{source}] kênh {cid}: sau {self.cfg.get('browse_unverified_window_sec', 900)} s "
                     f"vẫn KHÔNG xác minh được độ mới ({method}: {scrub(why)}) — không tải, không đăng")
        else:
            self.log(f"❔ BROWSE_UNVERIFIED {vid} [{source}] kênh {cid}: chưa xác minh được độ mới ({method}: {scrub(why)}) — "
                     f"chưa tải; sẽ XÉT LẠI ở các lượt sau (chữ tuổi mỗi lượt, player sau {self.PLAYER_RETRY[0]} s…)")

    def _timeline(self, cid, vid, source, res, t_cand, t_ver, t_q, method, result):
        fj = self.first_json.get(vid)
        fp = self.first_parsed.get(vid)
        base_ns = fj["t_ns"] if fj else res["t_recv_ns"]
        ms_ = lambda a, b: None if a is None or b is None else round((b - a) / 1e6, 1)
        tl = {"video_id": vid, "channel_id": cid, "source": source, "verify": method, "result": result,
              "HTTP_RECEIVED_MS": round(res["ms"], 1),
              "FIRST_JSON_VISIBLE": ({"wall": round(fj["wall"], 3), "surface": fj["surface"], "poll": fj["poll"], "seq": fj["seq"],
                                      "http_ms": fj["http_ms"]} if fj else None),
              "FIRST_JSON_TO_THIS_POLL_MS": ms_(base_ns, res["t_recv_ns"]),
              "FIRST_PARSED_MS": ms_(base_ns, fp["t_ns"]) if fp else None, "FIRST_PARSED_HOW": fp.get("how") if fp else None,
              "FIRST_CANDIDATE_MS": ms_(base_ns, t_cand), "AGE_VERIFY_MS": ms_(t_cand, t_ver), "DETECTED_TO_QUEUE_MS": ms_(t_ver, t_q),
              # số lượt quét CÙNG kênh + CÙNG bề mặt đã trôi qua kể từ lượt đầu có ID trong JSON (0 = bắt ngay lượt đầu)
              "polls_between": (self._s(fj["skey"], fj["cid"]).polls - fj["poll"]) if fj else None, "wall_detect": round(time.time(), 3)}
        f_ = lambda v: "-" if v is None else v
        fjw = datetime.fromtimestamp(fj["wall"]).strftime("%H:%M:%S.%f")[:-3] if fj else "?"
        self.log(f"⏱️ BROWSE_TIMELINE {vid} | HTTP_RECEIVED_MS {tl['HTTP_RECEIVED_MS']} | FIRST_JSON_VISIBLE {fjw} "
                 f"[{fj['surface'] if fj else '?'} lượt #{fj['poll'] if fj else '?'}] | FIRST_PARSED +{f_(tl['FIRST_PARSED_MS'])} ms "
                 f"({f_(tl['FIRST_PARSED_HOW'])}) | FIRST_CANDIDATE +{f_(tl['FIRST_CANDIDATE_MS'])} ms | AGE_VERIFY_MS {f_(tl['AGE_VERIFY_MS'])} | "
                 f"DETECTED_TO_QUEUE_MS {f_(tl['DETECTED_TO_QUEUE_MS'])} | số lượt từ lúc có trong JSON: {f_(tl['polls_between'])}")
        try:
            self.e.db.x("INSERT OR REPLACE INTO browse_timeline(video_id, channel_id, data) VALUES(?,?,?)", (vid, cid, json.dumps(tl)))
        except Exception:
            pass
        if self.cfg.get("browse_age_audit", True):
            threading.Thread(target=self._age_audit, args=(vid, tl), daemon=True, name="BROWSE-AUDIT").start()

    def _age_audit(self, vid, tl):
        """Chạy NỀN sau khi đã vào hàng đợi tải (không làm chậm gì): giờ đăng CHÍNH XÁC + đồng hồ Google → tuổi thật lúc ID xuất hiện
        trong JSON và lúc phát hiện. API + BROWSE: Data API (1 đơn vị). BROWSE ONLY: InnerTube player (không Data API)."""
        with self.audit_sem:
            pub, how = None, ""
            if self.verify_with_api and getattr(self.e, "api", None) is not None and hasattr(self.e.api, "video_published"):
                pub, how = self.e.api.video_published(vid)
                how = "Data API: " + how
            if pub is None:
                p, precise, why = self.player_publish(vid)
                pub, how = (p, "player: " + why) if precise else (None, how + ("; " if how else "") + "player: " + why)
        if pub is None:
            self.log(f"🕒 BROWSE_AGE_AUDIT {vid}: không có giờ đăng chính xác ({scrub(how)[:120]}) — chỉ có dòng thời gian quét")
            return
        fj = tl.get("FIRST_JSON_VISIBLE") or {}
        res = {"published": pub.isoformat(), "published_src": how[:120]}
        for key, wall in (("age_at_first_json_s", fj.get("wall")), ("age_at_detect_s", tl.get("wall_detect"))):
            if wall:
                srv, half = self.clock.server_time(wall)
                if srv is not None:
                    res[key] = round(srv - pub.timestamp(), 2)
                    res["clock_half_s"] = round(half, 2)
        tl["audit"] = res
        try:
            self.e.db.x("UPDATE browse_timeline SET data=? WHERE video_id=?", (json.dumps(tl), vid))
        except Exception:
            pass
        self.log(f"🕒 BROWSE_AGE_AUDIT {vid} | published {pub.isoformat()} ({scrub(how)[:60]}) | tuổi lúc ID có trong JSON lần đầu ≈ "
                 f"{res.get('age_at_first_json_s', '?')} s | lúc vào hàng đợi tải ≈ {res.get('age_at_detect_s', '?')} s "
                 f"(đồng hồ Google từ header Date ±{res.get('clock_half_s', '?')} s; giờ đăng làm tròn tới giây)")

    # ---------------------------------------------------------------- 1 job
    def _job(self, surface, cid, token):
        st = self._s(surface, cid)
        try:
            res = self.browse(cid, surface)
            self.resp_ms.append(res["ms"])
            self.stats["polls"] += 1
            stt = res["status"]
            if (stt != 200 or res["err"]) and self.trace is not None:
                with self.track_lock:
                    self.seq += 1
                    seq = self.seq
                self.trace.write({"seq": seq, "wall_send": round(res.get("wall_send", 0), 3), "wall_recv": round(res.get("wall_recv", 0), 3),
                                  "mono_send_ns": res.get("t_send_ns"), "mono_recv_ns": res["t_recv_ns"], "ch": cid,
                                  "surf": SURFACES[surface], "st": stt, "ms": round(res["ms"], 1), "err": res["err"]})
            if stt == 200 and not res["err"]:
                st.fails = 0
                self.guard.success(self._target(surface))
                self._process(cid, surface, res)
            elif stt == 200:                                   # JSON hỏng/đổi cấu trúc: báo lỗi, KHÔNG coi là 'không có video'
                self.stats["parse_err"] += 1
                if self.stats["parse_err"] in (1, 10, 100) or self.stats["parse_err"] % 1000 == 0:
                    self.log(f"❗ BROWSE_PARSE_ERROR {cid} [{SURFACES[surface]}]: {res['err']} (lần {self.stats['parse_err']})")
            elif stt in (429, 403):
                self.stats[f"http_{stt}"] += 1
                if self.guard.throttle(cid, stt, self._target(surface), max(1, len(self._channels()))) == "CHANNEL":
                    st.cool_n += 1
                    sec = min(CH_COOLDOWN_MAX, CH_COOLDOWN_BASE * (2 ** min(st.cool_n - 1, 4)))
                    st.cooldown_until = time.monotonic() + sec
                    if st.cool_n in (1, 3, 6):
                        self.log(f"🧊 BROWSE {cid} [{SURFACES[surface]}] HTTP {stt} chỉ ở kênh này → nghỉ riêng {sec:.0f}s")
            else:
                self.stats["net_err" if stt < 0 else f"http_{stt}"] += 1
                if stt in (400, 401):                          # như V39: key/clientVersion có thể đã cũ → đọc lại ngay (nền, ≤ 1 lần/30 s)
                    self._refresh_soon()
                st.fails += 1
                if st.fails >= CH_FAIL_THRESHOLD:
                    st.cool_n += 1
                    sec = min(CH_COOLDOWN_MAX, CH_COOLDOWN_BASE * (2 ** min(st.cool_n - 1, 4)))
                    st.cooldown_until = time.monotonic() + sec
                    if st.cool_n in (1, 3, 6):
                        self.log(f"⚠️ BROWSE {cid} [{SURFACES[surface]}]: {st.fails} lỗi liên tiếp ({res['err'] or stt}) → nghỉ {sec:.0f}s")
        except Exception as e:  # noqa: BLE001 — không bao giờ chết worker
            self.stats["job_err"] += 1
            self.log(f"❌ BROWSE job lỗi {cid}: {type(e).__name__}: {scrub(e)[:120]}")
        finally:
            with self.lock:
                if self.inflight[surface].get(cid, (None,))[0] == token:
                    self.inflight[surface].pop(cid, None)

    # ---------------------------------------------------------------- lập lịch (V39)
    def _target(self, surface):
        n = max(1, len(self._channels()))
        return min(self.max_rps[surface], max(0.25, n / self.periods[surface]))

    def _eligible(self, surface, cid, now):
        st = self._s(surface, cid)
        if now < st.cooldown_until:
            return False
        with self.lock:
            t = self.inflight[surface].get(cid)
        return t is None or (now - t[1]) > JOB_STUCK_SEC        # KHÔNG gửi chồng khi request trước còn chạy

    def _scheduler(self, surface):
        next_slot = time.monotonic() + (0.0 if surface == "uploads" else 0.35)
        while not self.stop_ev.is_set():
            self.beat[surface] = time.monotonic()
            chs = self._channels()
            if not chs:
                self.stop_ev.wait(0.5)
                continue
            target = self._target(surface)
            if surface == "uploads":
                rps, cool = self.guard.current(target)
            else:
                prim, cool = self.guard.current(self._target("uploads"))
                rps = max(0.25, target * min(1.0, prim / max(0.5, self._target("uploads"))))
            now = time.monotonic()
            if now < cool:
                self.stop_ev.wait(min(0.2, cool - now))
                next_slot = max(next_slot, cool)
                continue
            if now < next_slot:
                self.stop_ev.wait(min(0.05, next_slot - now))
                continue
            cid, best = None, None
            for c in chs:                                      # kênh LÂU NHẤT chưa quét đi trước (công bằng)
                if not self._eligible(surface, c, now):
                    continue
                t = self._s(surface, c).last_attempt
                if cid is None or t < best:
                    cid, best = c, t
            if cid is None:
                self.stop_ev.wait(0.02)
                continue
            self._s(surface, cid).last_attempt = now
            with self.lock:
                self.token += 1
                tok = (self.token, now)
                self.inflight[surface][cid] = tok
            try:
                self.ex[surface].submit(self._job, surface, cid, tok[0])
            except RuntimeError:
                return
            next_slot = max(next_slot + 1.0 / rps, time.monotonic() + 0.002)

    def _watchdog(self):
        """Giám sát: bộ lập lịch đứng > 15 s → dựng lại; ghi thống kê định kỳ."""
        while not self.stop_ev.wait(1.0):
            now = time.monotonic()
            for s in self.surfaces:
                if now - self.beat.get(s, now) > 15.0:
                    self.stats["restarts"] += 1
                    self.log(f"♻️ BROWSE: bộ lập lịch {SURFACES[s]} đứng > 15 s → khởi động lại")
                    self._spawn(lambda s=s: self._scheduler(s), f"BROWSE-{s.upper()}")
                    self.beat[s] = now
            if now - self.last_stats_log >= float(self.cfg.get("browse_stats_log_sec", 60)):
                self.last_stats_log = now
                self._stats_tick()

    def pct(self, p):
        v = sorted(self.resp_ms)
        if not v:
            return None
        return round(v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))], 1)

    def _stats_tick(self):
        s = self.stats
        p50, p90, p95 = self.pct(50), self.pct(90), self.pct(95)
        try:
            self.e.db.x("INSERT INTO browse_stats VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (time.time(), s["polls"], p50, p90, p95, s["http_403"], s["http_429"], s["net_err"], s["parse_err"],
                         s["verdict_FRESH"], s["verdict_OLD"], s["verdict_UNVERIFIED"]))
        except Exception:
            pass
        rps, _ = self.guard.current(self._target("uploads"))
        pv = sorted(self.parse_ms)
        pm = round(pv[len(pv) // 2], 2) if pv else None
        self.log(f"📊 BROWSE_RESPONSE_MS P50 {p50} / P90 {p90} / P95 {p95} | đọc JSON P50 {pm} ms | lượt {s['polls']} | RPS {rps:.1f}/{self._target('uploads'):.1f} | "
                 f"403 {s['http_403']} | 429 {s['http_429']} | mạng {s['net_err']} | JSON lỗi {s['parse_err']} | "
                 f"mới {s['verdict_FRESH']} / cũ {s['verdict_OLD']} / chưa xác minh {s['verdict_UNVERIFIED']}")

    def _spawn(self, fn, name):
        def run():
            while not self.stop_ev.is_set():
                try:
                    fn()
                    return
                except Exception as e:  # noqa: BLE001
                    self.log(f"❌ {name} lỗi {type(e).__name__}: {scrub(e)[:120]} — chạy lại sau 1 s")
                    self.stop_ev.wait(1.0)
        t = threading.Thread(target=run, daemon=True, name=name)
        t.start()
        self.threads.append(t)

    def start(self):
        self.stop_ev.clear()
        self.ex = {"uploads": ThreadPoolExecutor(int(self.cfg.get("browse_workers", 8)), thread_name_prefix="BROWSE-UP"),
                   "videos": ThreadPoolExecutor(int(self.cfg.get("browse_videos_workers", 4)), thread_name_prefix="BROWSE-VD")}
        for s in self.surfaces:
            self.guard.dynamic_rps = self._target("uploads")
            self.beat[s] = time.monotonic()
            self._spawn(lambda s=s: self._scheduler(s), f"BROWSE-{s.upper()}")
        self._spawn(self._watchdog, "BROWSE-WATCHDOG")
        self._spawn(self._cfg_loop, "BROWSE-CFG")                # như V39: khoá + phiên bản client mới nhất, 10 phút/lần
        self.status = "CHẠY"
        n = len(self._channels())
        self.log(f"🌐 BROWSE: {n} kênh | Uploads mục tiêu {self.periods['uploads']:g} s/kênh (RPS ≤ {self.max_rps['uploads']:g} → thực tế "
                 f"≥ {n / self._target('uploads') if n else 0:.2f} s/kênh)" + (f" | Videos tab {self.periods['videos']:g} s/kênh" if
                                                                             "videos" in self.surfaces else "") +
                 f" | baseline đã lưu: {sum(len(v) for v in self.baseline.values())} ID | client WEB {self.client_version}")
        return True

    def stop(self):
        self.stop_ev.set()
        for t in self.threads:
            t.join(timeout=3)
        self.threads = []
        for ex in self.ex.values():
            ex.shutdown(wait=True, cancel_futures=True)
        if self.stats["polls"]:
            self._stats_tick()
        if self.trace is not None:
            self.trace.close()
        self.status = "TẮT"

    def snapshot(self):
        s = self.stats
        rps, cool = self.guard.current(self._target("uploads"))
        return {"status": self.status + (" (nghỉ chung)" if cool > time.monotonic() else ""), "polls": s["polls"],
                "p50": self.pct(50), "p90": self.pct(90), "p95": self.pct(95), "rps": round(rps, 1),
                "e403": s["http_403"], "e429": s["http_429"], "net": s["net_err"], "parse": s["parse_err"],
                "fresh": s["verdict_FRESH"], "old": s["verdict_OLD"], "unverified": s["verdict_UNVERIFIED"],
                "fast_miss": s["fast_miss"], "pending": len(self.pending)}


def _yt_web_client_version():
    try:
        from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS     # chỉ đọc HẰNG SỐ phiên bản client (không quét bằng yt-dlp)
        return str(INNERTUBE_CLIENTS["web"]["INNERTUBE_CONTEXT"]["client"]["clientVersion"])
    except Exception:
        return DEFAULT_CLIENT_VERSION
