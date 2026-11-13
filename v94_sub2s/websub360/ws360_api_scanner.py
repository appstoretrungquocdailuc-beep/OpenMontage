# -*- coding: utf-8 -*-
"""WS360 — NGUỒN PHÁT HIỆN THỨ 2: YouTube Data API v3 (activities.list), chạy song song WebSub (RACE MODE).

Chuyển từ 'quetphantichid - Sao chép(4).py' (VPS SNIPER V41) với các sửa đổi:
  • quét TIẾP sau mỗi video (bản cũ dừng kênh ngay khi thấy 1 video);
  • KHÔNG mở trình duyệt YouTube, KHÔNG tự thêm dòng Excel — video mới đi qua ĐÚNG đường của WebSub
    (Engine.on_event: chống trùng nguyên tử → tải 360p → ghi cột D → hàng đợi TikTok);
  • quota tính theo TỪNG Google Cloud project (nhiều key cùng project dùng CHUNG 10.000 đơn vị/ngày),
    reset lúc 0h giờ Thái Bình Dương; nhịp quét THÍCH ỨNG theo quota còn lại (không mặc định 0,2 s);
  • key gửi qua header X-goog-api-key (không nằm trong URL) và KHÔNG BAO GIỜ in ra log (chỉ dấu vân tay).

Mỗi lần gọi activities.list = 1 đơn vị quota (không phụ thuộc maxResults).
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import urllib3
except ImportError:  # pragma: no cover
    urllib3 = None

API_URL = "https://www.googleapis.com/youtube/v3/activities"
FIELDS = "items(snippet(type,publishedAt,title,channelId),contentDetails(upload(videoId)))"
KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{30,}")
DEFAULT_LIMIT = 10000

# lý do lỗi Google → cách xử lý
R_QUOTA = {"quotaExceeded", "dailyLimitExceeded", "dailyLimitExceededUnreg"}
R_RATE = {"rateLimitExceeded", "userRateLimitExceeded", "RATE_LIMIT_EXCEEDED"}
R_BADKEY = {"keyInvalid", "keyExpired", "API_KEY_INVALID", "API_KEY_EXPIRED", "accessNotConfigured",
            "SERVICE_DISABLED", "API_KEY_SERVICE_BLOCKED", "API_KEY_HTTP_REFERRER_BLOCKED", "API_KEY_IP_ADDRESS_BLOCKED",
            "ipRefererBlocked", "API_KEY_ANDROID_APP_BLOCKED", "API_KEY_IOS_APP_BLOCKED"}
R_CHANNEL = {"forbidden", "channelForbidden", "channelClosed", "channelSuspended", "channelNotFound"}   # lỗi của KÊNH, key vẫn tốt


def key_fp(key: str) -> str:
    return "key#" + hashlib.sha256(key.encode()).hexdigest()[:6]


def scrub_keys(text: str) -> str:
    return KEY_RE.sub("AIza***", str(text or ""))


def parse_iso(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ------------------------------------------------------------------ ngày quota (giờ Thái Bình Dương)
def _pt_offset_hours(u: datetime) -> int:
    """Giờ Thái Bình Dương (Mỹ): DST từ CN thứ 2 tháng 3 (2h) tới CN đầu tháng 11 (2h). Không cần tzdata."""
    y = u.year

    def nth_sunday(month, n):
        d = datetime(y, month, 1, tzinfo=timezone.utc)
        return d + timedelta(days=(6 - d.weekday()) % 7, weeks=n - 1)
    start = nth_sunday(3, 2) + timedelta(hours=10)        # 02:00 PST = 10:00 UTC
    end = nth_sunday(11, 1) + timedelta(hours=9)          # 02:00 PDT = 09:00 UTC
    return -7 if start <= u < end else -8


def quota_day(t: float) -> tuple[str, float]:
    """(ngày quota 'YYYY-MM-DD' theo giờ PT, số giây còn lại tới lúc Google reset quota)."""
    u = datetime.fromtimestamp(t, timezone.utc)
    off = _pt_offset_hours(u)
    loc = u + timedelta(hours=off)
    nxt = datetime(loc.year, loc.month, loc.day, tzinfo=timezone.utc) + timedelta(days=1)
    reset = nxt - timedelta(hours=_pt_offset_hours(nxt - timedelta(hours=off)))
    return loc.date().isoformat(), max(1.0, (reset - u).total_seconds())


# ------------------------------------------------------------------ file key
def parse_keys(text: str) -> tuple[list[dict], list[str]]:
    """Mỗi dòng 1 key. Dạng:  KEY  |  PROJECT|KEY  |  PROJECT|KEY|QUOTA_NGAY   (dấu | , ; tab đều được).
    Dòng trống / bắt đầu bằng # bị bỏ qua. Key không ghi project → coi là 1 project riêng (CẢNH BÁO)."""
    out, warns, seen = [], [], set()
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in re.split(r"[|,;\t]", line) if p.strip()]
        if len(parts) == 1 and " " in parts[0]:
            parts = parts[0].split()
        if len(parts) == 1:
            key, proj, lim = parts[0], None, None
        else:
            proj, key = parts[0], parts[1]
            lim = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
            if KEY_RE.fullmatch(proj) and not KEY_RE.fullmatch(key):   # người dùng ghi ngược KEY|PROJECT
                proj, key = key, proj
        if not KEY_RE.fullmatch(key):
            warns.append(f"dòng {i}: không giống API key Google (AIza…) — bỏ qua")
            continue
        if key in seen:
            warns.append(f"dòng {i}: key trùng ({key_fp(key)}) — bỏ qua")
            continue
        seen.add(key)
        out.append({"project": proj or f"auto-{key_fp(key)[4:]}", "key": key, "limit": lim, "auto": proj is None})
    return out, warns


class Project:
    def __init__(self, name, limit):
        self.name = name
        self.limit = int(limit)
        self.keys: list[str] = []
        self.dead: dict[str, str] = {}        # key → lý do bị loại (in bằng dấu vân tay)
        self.ki = 0
        self.used = 0
        self.day = ""
        self.exhausted_day = ""               # Google báo hết quota trong ngày này
        self.backoff_until = 0.0
        self.backoff_n = 0
        self.auto = False

    def live_keys(self):
        return [k for k in self.keys if k not in self.dead]


# ------------------------------------------------------------------ giờ vàng
def parse_windows(spec) -> list[tuple[int, int, set | None]]:
    """['20:55-21:05', {'time': '08:59:30-09:01', 'channels': ['UC…']}] → [(giây bắt đầu, giây kết thúc, kênh|None)]."""
    out = []
    for w in spec or []:
        chans = None
        if isinstance(w, dict):
            chans = set(w.get("channels") or []) or None
            w = w.get("time") or w.get("window") or ""
        m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*-\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*", str(w))
        if not m:
            continue
        a = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3] or 0)
        b = int(m[4]) * 3600 + int(m[5]) * 60 + int(m[6] or 0)
        if b <= a:
            b += 86400                        # qua nửa đêm
        out.append((a, b, chans))
    return out


def _local_midnight(t: float) -> float:
    lt = time.localtime(t)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def window_overlap(win, now: float, until: float) -> float:
    a, b, _ = win
    tot = 0.0
    m0 = _local_midnight(now)
    for d in (-1, 0, 1, 2):
        ws_, we = m0 + d * 86400 + a, m0 + d * 86400 + b
        tot += max(0.0, min(we, until) - max(ws_, now))
    return tot


def in_window(win, now: float) -> bool:
    return window_overlap(win, now, now + 0.001) > 0


def plan_intervals(remaining: float, secs_to_reset: float, n_channels: int, hot_demand: list[tuple[float, int]],
                   min_iv: float) -> tuple[float, float]:
    """Chia quota còn lại cho tới lúc reset.
    hot_demand = [(số giây giờ vàng còn lại trước reset, số kênh thuộc khung)].
    Trả (nhịp giờ vàng, nhịp thường) tính bằng giây/kênh. inf = tạm ngừng (hết quota)."""
    inf = float("inf")
    if n_channels <= 0:
        return min_iv, min_iv
    if remaining <= 0:
        return inf, inf
    hot_chsec = sum(s * n for s, n in hot_demand)
    h_req = hot_chsec / min_iv
    cold_chsec = max(0.0, n_channels * secs_to_reset - hot_chsec)
    if h_req >= remaining:
        return max(min_iv, min_iv * h_req / remaining), inf
    c = remaining - h_req
    cold = max(min_iv, cold_chsec / c) if cold_chsec > 0 else min_iv
    return min_iv, cold


class _ChanState:
    __slots__ = ("last", "inflight", "known", "baseline", "bad_until", "polls", "last_ms", "fails")

    def __init__(self, due):
        self.last = due                       # lúc gửi lần gọi gần nhất; tới hạn = last + nhịp HIỆN TẠI (tính lại liên tục)
        self.inflight = False
        self.known: list[str] = []
        self.baseline = False
        self.bad_until = 0.0
        self.polls = 0
        self.last_ms = None
        self.fails = 0


class ApiScanner:
    def __init__(self, engine, cfg: dict, log, base: Path, http=None, clock=time.time):
        self.e = engine
        self.cfg = cfg
        self.log = log
        self.base = Path(base)
        self.clock = clock
        self.url = str(cfg.get("api_url") or API_URL)
        self.min_iv = max(0.2, float(cfg.get("api_min_interval_sec", 1.0)))
        self.safety = min(1.0, max(0.1, float(cfg.get("api_quota_safety", 0.95))))
        self.windows = parse_windows(cfg.get("api_hot_windows") or [])
        self.projects: dict[str, Project] = {}
        self.chan: dict[str, _ChanState] = {}
        self.lock = threading.RLock()
        self.stop_ev = threading.Event()
        self.threads: list[threading.Thread] = []
        self.ex = None
        self.http = http
        self.ivs = (self.min_iv, self.min_iv)
        self.paused_logged = False
        self.dirty = False
        self.stats = {"polls": 0, "errors": 0, "detected": 0, "last_ms": None, "status": "TẮT"}
        self._pool = None
        self._last_quota_log = 0.0

    # ---------------------------------------------------------------- khởi động
    def load_keys(self) -> int:
        p = Path(self.cfg.get("api_keys_file") or "api_keys.txt")
        if not p.is_absolute():
            p = self.base / p
        if not p.exists():
            self.log(f"⚠️ API: không có file key {p.name} → nguồn API KHÔNG chạy (WebSub vẫn chạy nếu chế độ có WebSub)")
            return 0
        rows, warns = parse_keys(p.read_text(encoding="utf-8-sig", errors="replace"))
        for w in warns:
            self.log(f"   ⚠️ {p.name} {w}")
        default_lim = int(self.cfg.get("api_quota_per_project", DEFAULT_LIMIT))
        per = self.cfg.get("api_project_quota") or {}
        for r in rows:
            pr = self.projects.get(r["project"])
            if pr is None:
                pr = self.projects[r["project"]] = Project(r["project"], r["limit"] or per.get(r["project"]) or default_lim)
                pr.auto = r["auto"]
            elif r["limit"]:
                pr.limit = r["limit"]
            pr.keys.append(r["key"])
        n_auto = sum(1 for p_ in self.projects.values() if p_.auto)
        if n_auto:
            self.log(f"   ℹ️ API: {n_auto} key không ghi tên project → mỗi key được tính là 1 project riêng. Nếu nhiều key thuộc "
                     f"CÙNG 1 project, ghi dạng 'TEN_PROJECT|KEY' để tool không tiêu vượt quota thật.")
        self._load_quota()
        return sum(len(p_.keys) for p_ in self.projects.values())

    def _load_quota(self):
        day, _ = quota_day(self.clock())
        for pr in self.projects.values():
            pr.day = day
            try:
                r = self.e.db.one("SELECT used FROM api_quota WHERE project=? AND day=?", (pr.name, day))
                pr.used = int(r["used"]) if r else 0
            except Exception:
                pr.used = 0

    def start(self) -> bool:
        n = self.load_keys()
        if not n:
            self.stats["status"] = "KHÔNG CÓ KEY"
            return False
        if self.http is None:
            if urllib3 is None:
                self.log("❌ API: thiếu thư viện urllib3")
                return False
            self._pool = urllib3.PoolManager(num_pools=4, maxsize=int(self.cfg.get("api_concurrency", 8)),
                                             headers={"User-Agent": "WS360-API/1.0", "Accept-Encoding": "gzip"})
            self.http = self._http
        self.ex = ThreadPoolExecutor(max_workers=int(self.cfg.get("api_concurrency", 8)), thread_name_prefix="API-POLL")
        self.stop_ev.clear()
        for fn, name in ((self._sched, "API-SCHED"), (self._flush_loop, "API-QUOTA")):
            t = threading.Thread(target=fn, name=name, daemon=True)
            t.start()
            self.threads.append(t)
        self.stats["status"] = "CHẠY"
        self._recompute()
        self.log(f"🔎 API: {n} key / {len(self.projects)} project | quota mỗi project {self._limits_text()} | "
                 f"nhịp tối thiểu {self.min_iv:g} s/kênh | giờ vàng: {len(self.windows)} khung")
        self.log_quota(force=True)
        return True

    def stop(self):
        self.stop_ev.set()
        for t in self.threads:
            t.join(timeout=5)
        self.threads = []
        if self.ex:
            self.ex.shutdown(wait=True, cancel_futures=True)
            self.ex = None
        self._flush()
        if self.stats["status"] == "CHẠY":
            self.log_quota(force=True)
        self.stats["status"] = "TẮT"

    def _limits_text(self):
        lims = sorted({p.limit for p in self.projects.values()})
        return "/".join(str(x) for x in lims) + f" × an toàn {self.safety:.0%}"

    # ---------------------------------------------------------------- quota
    def _roll_day(self):
        day, _ = quota_day(self.clock())
        for pr in self.projects.values():
            if pr.day != day:
                if pr.day:
                    self.log(f"🔄 API: sang ngày quota mới (giờ Thái Bình Dương) → project {pr.name} về 0")
                pr.day, pr.used = day, 0
                self.dirty = True

    def _usable(self, pr: Project, now: float) -> bool:
        return (bool(pr.live_keys()) and pr.exhausted_day != pr.day and now >= pr.backoff_until
                and pr.used < pr.limit * self.safety)

    def remaining(self) -> float:
        return sum(max(0.0, pr.limit * self.safety - pr.used) for pr in self.projects.values()
                   if pr.live_keys() and pr.exhausted_day != pr.day)

    def _take(self, now) -> tuple[Project, str] | None:
        """Chọn project còn nhiều quota nhất (giữ đều các project), rồi xoay vòng key trong project."""
        with self.lock:
            cands = [pr for pr in self.projects.values() if self._usable(pr, now)]
            if not cands:
                return None
            pr = max(cands, key=lambda p_: p_.limit * self.safety - p_.used)
            keys = pr.live_keys()
            k = keys[pr.ki % len(keys)]
            pr.ki += 1
            pr.used += 1                      # giữ chỗ trước: không bao giờ vượt quota vì nhiều luồng cùng lúc
            self.dirty = True
            return pr, k

    def _refund(self, pr: Project):
        with self.lock:
            pr.used = max(0, pr.used - 1)     # lỗi mạng: request không tới Google → không mất quota

    def _flush(self):
        if not self.dirty:
            return
        self.dirty = False
        try:
            self.e.db.tx([("INSERT INTO api_quota(project, day, used) VALUES(?,?,?) ON CONFLICT(project, day) "
                           "DO UPDATE SET used=excluded.used", (pr.name, pr.day, pr.used)) for pr in self.projects.values()])
        except Exception as e:  # noqa: BLE001
            self.dirty = True
            self.log(f"⚠️ API: lưu bộ đếm quota lỗi: {type(e).__name__}")

    def _flush_loop(self):
        while not self.stop_ev.wait(5.0):
            self._flush()
            self.log_quota()

    def quota_text(self) -> str:
        used = sum(p.used for p in self.projects.values())
        lim = sum(int(p.limit * self.safety) for p in self.projects.values())
        per = " | ".join(f"{p.name} {p.used}/{int(p.limit * self.safety)}"
                         + (" HẾT" if p.exhausted_day == p.day else "") + (" (key lỗi)" if not p.live_keys() else "")
                         for p in list(self.projects.values())[:6])
        more = f" | +{len(self.projects) - 6} project" if len(self.projects) > 6 else ""
        hot, cold = self.ivs
        return (f"API_QUOTA_USED {used}/{lim} | {per}{more} | nhịp quét: thường {self._fmt_iv(cold)}"
                + (f", giờ vàng {self._fmt_iv(hot)}" if self.windows else ""))

    @staticmethod
    def _fmt_iv(v):
        return "TẠM NGỪNG (hết quota)" if v == float("inf") else f"{v:.1f} s/kênh"

    def log_quota(self, force=False):
        now = self.clock()
        if force or now - self._last_quota_log >= float(self.cfg.get("api_quota_log_sec", 300)):
            self._last_quota_log = now
            self.log(f"📊 {self.quota_text()}")

    # ---------------------------------------------------------------- lập lịch
    def _channels(self) -> list[str]:
        with self.e.ch_lock:
            return list(self.e.channels)

    def _recompute(self):
        now = self.clock()
        self._roll_day()
        _, ttl = quota_day(now)
        chans = [c for c in self._channels() if self.chan.get(c) is None or self.chan[c].bad_until <= now]
        demand = []
        for w in self.windows:
            n = len(chans) if w[2] is None else len([c for c in chans if c in w[2]])
            if n:
                demand.append((window_overlap(w, now, now + ttl), n))
        self.ivs = plan_intervals(self.remaining(), ttl, len(chans), demand, self.min_iv)
        if self.ivs[1] == float("inf") and not self.paused_logged:
            self.paused_logged = True
            self.log("⚠️ API: quota còn lại KHÔNG đủ để quét ngoài giờ vàng → chỉ quét trong giờ vàng"
                     if self.windows and self.ivs[0] != float("inf") else
                     "⚠️ API: HẾT quota mọi project → tạm ngừng quét API tới khi Google reset (0h giờ Thái Bình Dương)")
        elif self.ivs[1] != float("inf"):
            self.paused_logged = False

    def interval_for(self, ch, now) -> float:
        hot, cold = self.ivs
        for w in self.windows:
            if (w[2] is None or ch in w[2]) and in_window(w, now):
                return hot
        return cold

    def _sched(self):
        last_re = 0.0
        while not self.stop_ev.is_set():
            now = self.clock()
            if now - last_re >= 1.0:
                self._recompute()
                last_re = now
            chans = self._channels()
            for i, ch in enumerate(chans):
                st = self.chan.get(ch)
                if st is None:                                     # kênh mới: gọi mốc ngay (dàn 20 ms/kênh)
                    st = self.chan[ch] = _ChanState(now - 1e9 + 0.02 * i)
                if st.inflight or now < st.bad_until:
                    continue
                # nhịp tính lại MỖI vòng: vào giờ vàng / sang ngày quota mới là quét nhanh ngay, không chờ hạn cũ
                iv = self.interval_for(ch, now) if st.baseline else self.min_iv
                if iv == float("inf") or now < st.last + iv:
                    continue
                got = self._take(now)
                if got is None:
                    break
                st.inflight = True
                st.last = now
                try:
                    self.ex.submit(self._poll, ch, st, got[0], got[1], iv)
                except RuntimeError:                               # executor đã đóng (đang dừng)
                    self._refund(got[0])
                    st.inflight = False
                    return
            self.stop_ev.wait(0.05)

    # ---------------------------------------------------------------- 1 lần gọi API
    def _http(self, url, headers, timeout):
        r = self._pool.request("GET", url, headers=headers, timeout=urllib3.Timeout(connect=2.0, read=timeout),
                               retries=False, preload_content=True)
        return r.status, dict(r.headers), r.data

    def _poll(self, ch, st: _ChanState, pr: Project, key: str, iv: float):
        url = f"{self.url}?part=snippet,contentDetails&channelId={ch}&maxResults=5&fields={FIELDS}"
        hdr = {"X-goog-api-key": key, "Cache-Control": "no-cache", "Pragma": "no-cache"}
        t0 = time.perf_counter_ns()
        try:
            try:
                status, rh, body = self.http(url, hdr, float(self.cfg.get("api_timeout_sec", 4.0)))
            except Exception as e:  # noqa: BLE001 — lỗi mạng: không tới Google → hoàn quota, thử lại ở nhịp sau
                self._refund(pr)
                st.fails += 1
                self.stats["errors"] += 1
                if st.fails in (1, 5) or st.fails % 50 == 0:
                    self.log(f"⚠️ API kênh {ch}: lỗi mạng {type(e).__name__}: {scrub_keys(str(e))[:120]}")
                return
            t_recv = time.perf_counter_ns()
            st.last_ms = self.stats["last_ms"] = round((t_recv - t0) / 1e6, 1)
            st.polls += 1
            self.stats["polls"] += 1
            if status == 200:
                st.fails = 0
                pr.backoff_n = 0
                self._handle_items(ch, st, json.loads(body.decode("utf-8", "replace") or "{}"), t_recv, iv)
                return
            self.stats["errors"] += 1
            self._handle_error(ch, st, pr, key, status, rh, body)
        except Exception as e:  # noqa: BLE001
            self.stats["errors"] += 1
            self.log(f"❌ API kênh {ch}: lỗi xử lý {type(e).__name__}: {scrub_keys(str(e))[:160]}")
        finally:
            st.inflight = False

    def _handle_items(self, ch, st: _ChanState, data: dict, t_recv: int, iv: float):
        ups = []
        for it in data.get("items") or []:
            sn = it.get("snippet") or {}
            if sn.get("type") != "upload":                  # 'playlistItem' = thêm video (có thể cũ) vào playlist → không phải video mới
                continue
            vid = ((it.get("contentDetails") or {}).get("upload") or {}).get("videoId")
            if vid:
                ups.append((vid, sn.get("publishedAt"), sn.get("title") or ""))
        ups.reverse()                                       # cũ → mới
        if not st.baseline:
            st.baseline = True
            st.known = [v for v, _, _ in ups]
            for vid, pub, title in ups:                     # mốc: chỉ video còn 'mới' theo CÙNG bộ lọc tuổi như WebSub
                p = parse_iso(pub)
                # mốc an toàn: CHỈ video ≤ max_age_minutes (không dùng cửa sổ 'lúc tool tắt' → video cũ 19 giờ không thành mới)
                if p is not None and self.e.is_fresh_video(p) and vid not in self.e.seen:
                    self._emit(ch, vid, p, title, t_recv, iv, baseline=True)
            return
        for vid, pub, title in ups:
            if vid in st.known:
                continue
            st.known.append(vid)
            self._emit(ch, vid, parse_iso(pub), title, t_recv, iv, baseline=False)
        del st.known[:-50]

    def _emit(self, ch, vid, published, title, t_recv, iv, baseline):
        r = self.e.on_event(ch, vid, "API", published, title, t_recv, race=not baseline,
                            extra={"api_interval_s": None if iv == float("inf") else round(iv, 2), "baseline": baseline})
        if r == "queued":
            self.stats["detected"] += 1
            self.log_quota(force=True)
        elif r == "old":
            self.log(f"↩️ API: {vid} (kênh {ch}) có giờ đăng cũ {published} — không tải")

    def _handle_error(self, ch, st, pr: Project, key, status, rh, body):
        reasons = set()
        msg = ""
        try:
            err = json.loads(body.decode("utf-8", "replace")).get("error") or {}
            msg = str(err.get("message") or "")
            reasons |= {str(x.get("reason")) for x in err.get("errors") or [] if x.get("reason")}
            reasons |= {str(x.get("reason")) for x in err.get("details") or [] if isinstance(x, dict) and x.get("reason")}
        except Exception:
            pass
        why = f"HTTP {status} {','.join(sorted(reasons)) or ''} {scrub_keys(msg)[:100]}".strip()
        now = self.clock()
        with self.lock:
            if reasons & R_QUOTA:
                if pr.exhausted_day != pr.day:
                    pr.exhausted_day = pr.day
                    self.log(f"⛽ API: project {pr.name} HẾT quota ngày {pr.day} (Google báo, tool đếm {pr.used}) → "
                             f"ngừng dùng tới khi reset 0h giờ Thái Bình Dương")
                return
            if reasons & R_BADKEY or (status == 400 and "key" in msg.lower()):
                if key not in pr.dead:
                    pr.dead[key] = why
                    self.log(f"🔑 API: loại {key_fp(key)} (project {pr.name}) — {why}")
                return
            if status == 404 or reasons & R_CHANNEL:
                st.bad_until = now + 600
                self.log(f"⚠️ API: kênh {ch} không tồn tại / không đọc được ({why}) → thử lại sau 10 phút")
                return
            if status == 429 or reasons & R_RATE or status >= 500 or status == 403:
                pr.backoff_n += 1
                ra = 0.0
                try:
                    ra = float((rh or {}).get("Retry-After") or 0)
                except ValueError:
                    pass
                wait = max(ra, min(120.0, 2.0 * (2 ** min(pr.backoff_n - 1, 6))))
                pr.backoff_until = now + wait
                if pr.backoff_n in (1, 3) or pr.backoff_n % 10 == 0:
                    self.log(f"⏳ API: project {pr.name} {why} → giãn {wait:.0f} s (có giới hạn)")
                return
        self.log(f"⚠️ API kênh {ch}: {why}")

    def video_published(self, vid):
        """Data API videos.list (1 đơn vị, tính vào quota project như mọi lời gọi khác) → (published datetime|None, lý do).
        Chỉ dùng để xác minh ứng viên BROWSE ở chế độ API + BROWSE."""
        got = self._take(self.clock())
        if got is None:
            return None, "Data API: hết quota/không có key dùng được"
        pr, key = got
        base = self.url.rsplit("/", 1)[0] + "/videos"
        try:
            status, rh, body = self.http(f"{base}?part=snippet&id={vid}&fields=items(snippet(publishedAt,channelId))",
                                         {"X-goog-api-key": key}, float(self.cfg.get("api_timeout_sec", 4.0)))
        except Exception as e:  # noqa: BLE001
            self._refund(pr)
            return None, f"Data API lỗi mạng {type(e).__name__}"
        if status != 200:
            return None, f"Data API HTTP {status}"
        try:
            items = json.loads(body.decode("utf-8", "replace")).get("items") or []
        except ValueError:
            return None, "Data API JSON lỗi"
        if not items:
            return None, "Data API chưa có video (chưa index)"
        p_ = parse_iso((items[0].get("snippet") or {}).get("publishedAt"))
        return (p_, f"Data API publishedAt {(items[0].get('snippet') or {}).get('publishedAt')}") if p_ else (None, "Data API thiếu publishedAt")

    # ---------------------------------------------------------------- giao diện
    def snapshot(self) -> dict:
        hot, cold = self.ivs
        used = sum(p.used for p in self.projects.values())
        lim = sum(int(p.limit * self.safety) for p in self.projects.values())
        return {"status": self.stats["status"], "projects": len(self.projects),
                "keys": sum(len(p.live_keys()) for p in self.projects.values()),
                "quota": f"{used}/{lim}", "interval": self._fmt_iv(cold),
                "hot_interval": self._fmt_iv(hot) if self.windows else "-", "polls": self.stats["polls"],
                "errors": self.stats["errors"], "detected": self.stats["detected"], "last_ms": self.stats["last_ms"]}
