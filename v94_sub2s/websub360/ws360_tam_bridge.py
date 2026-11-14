# -*- coding: utf-8 -*-
"""
CẦU NỐI WS360 → TAM (đăng TikTok) — CHẠY CHUNG MỘT TIẾN TRÌNH.

Vì sao cùng tiến trình: job đi từ DOWNLOAD_DONE sang uploader qua hàng đợi trong bộ nhớ (≈ micro-giây), profile
Genlogin/Selenium đã mở sẵn nằm ngay trong `tam.drivers`, và mọi mốc đo (WS360 + TAM) dùng CHUNG perf_counter_ns —
không cần đồng bộ đồng hồ giữa 2 tiến trình. Job được ghi BỀN VỮNG vào SQLite (bảng upload_jobs) TRƯỚC khi đưa
vào hàng đợi → crash/khởi động lại không mất job.

TÁI SỬ DỤNG NGUYÊN VẸN backend TAM (không sửa file tam_INSTANT_POST.py):
  setup_and_navigate_profile  — mở profile Genlogin + gắn Selenium (debuggerAddress) + nạp trang Upload
  drivers / drivers_lock       — danh sách driver đang mở
  get_profile_lock             — khoá thực thi riêng từng profile
  upload_video_to_tiktok_ex    — IDLE→READY→ATTACHING→…→POST_READY→POSTING→CONFIRMED; CHỈ CONFIRMED khi có bằng chứng
  JOURNAL (PostJournal)        — POSTING trước khi bấm, CONFIRMED/UNKNOWN/FAILED_VISIBLE sau (chống đăng trùng, crash)
  get_fsm / PState             — trạng thái profile (UNKNOWN chặn đăng tiếp cho tới khi người kiểm tra)
  prepare_upload_page          — nạp lại trang Upload sẵn sàng cho video kế tiếp
  PerfTrace                    — các mốc SEND_KEYS_START/END, PROCESSING_READY (POST_READY), POST_CLICKED, POST_CONFIRMED

QUY TẮC GHÉP PROFILE (cột C) ↔ KÊNH (cột K):
  * Mỗi cặp (kênh, profile) KHÁC NHAU là một đích đăng. Một kênh có nhiều dòng với CÙNG profile → đăng 1 lần.
    Một kênh có nhiều dòng với profile KHÁC nhau → đăng cho TẤT CẢ các profile đó (log rõ khi đọc Excel).
  * Nhiều kênh cùng một profile → hàng đợi riêng của profile đó, đăng lần lượt (không bao giờ 2 phiên song song).
  * Kênh không có Profile ID hợp lệ → job HELD (không đăng nhầm, không chia ngẫu nhiên), chờ bạn sửa Excel.
"""
from __future__ import annotations

import collections
import importlib.util
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

PID_RE = re.compile(r"^\d{3,15}$")
TERMINAL = ("CONFIRMED", "UNKNOWN", "FAILED_VISIBLE", "DUPLICATE", "DRY_RUN_DONE")
OPEN_STATES = ("CLOSED", "OPENING", "READY", "BUSY", "ERROR", "UNKNOWN", "NOT_FOUND")


TEMP_RE = re.compile(r"^\.[0-9A-Za-z_-]{11}(?:_360p)?(?:\.a\d+)?\.part(?:\.mp4|\.v|\.a)?$")


def _ms(a, b):
    return None if a is None or b is None else round((b - a) / 1e6, 3)


def norm_pid(v) -> str | None:
    s = str(v or "").strip()
    if s.endswith(".0"):                       # Excel lưu số → "26555915.0"
        s = s[:-2]
    return s if PID_RE.match(s) else None


def load_tam(tam_path: str):
    """Nạp tam_INSTANT_POST.py như THƯ VIỆN (không chạy GUI của nó). Tắt HIGH priority toàn tiến trình."""
    p = Path(tam_path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"không thấy {p}")
    os.environ.setdefault("TAM_HIGH_PRIORITY", "0")        # không để TAM đặt HIGH cho cả WS360
    d = str(p.parent)
    if d not in sys.path:
        sys.path.insert(0, d)                              # để `from Genlogin import Genlogin` thấy Genlogin.py cạnh TAM
    spec = importlib.util.spec_from_file_location("tam_INSTANT_POST", str(p))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tam_INSTANT_POST"] = mod
    spec.loader.exec_module(mod)
    need = ("setup_and_navigate_profile", "drivers", "drivers_lock", "get_profile_lock", "upload_video_to_tiktok_ex",
            "JOURNAL", "PostStatus", "get_fsm", "PState", "prepare_upload_page", "PerfTrace", "TARGET_UPLOAD_URL",
            "CHROME_DRIVER_PATH")
    miss = [n for n in need if not hasattr(mod, n)]
    if miss:
        raise RuntimeError(f"tam_INSTANT_POST.py thiếu {miss} (phiên bản không tương thích)")
    return mod


def read_profile_mapping(xlsx_path, col_channel="K", col_profile="C", sheet=""):
    """→ (targets {channel: [pid,...]}, issues [str]) — đọc 1 lần, giữ trong RAM."""
    import openpyxl
    from openpyxl.utils import column_index_from_string
    ck, cp = column_index_from_string(col_channel), column_index_from_string(col_profile)
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    try:
        ws = wb[sheet] if sheet and sheet in wb.sheetnames else wb.worksheets[0]
        rows = collections.defaultdict(list)
        lo, hi = min(ck, cp), max(ck, cp)
        for r_idx, row in enumerate(ws.iter_rows(min_col=lo, max_col=hi, values_only=True), start=1):
            ch_raw = row[ck - lo]
            m = re.search(r"\b(UC[0-9A-Za-z_-]{22})\b", str(ch_raw or ""))
            if not m:
                continue
            rows[m.group(1)].append((r_idx, row[cp - lo]))
    finally:
        wb.close()
    targets, issues = {}, []
    for ch, lst in rows.items():
        pids = []
        for r_idx, raw in lst:
            pid = norm_pid(raw)
            if pid is None:
                if str(raw or "").strip():
                    issues.append(f"dòng {r_idx}: Profile ID '{raw}' của kênh {ch} KHÔNG hợp lệ (chỉ gồm chữ số)")
                else:
                    issues.append(f"dòng {r_idx}: kênh {ch} CHƯA có Profile ID ở cột {col_profile}")
                continue
            if pid not in pids:
                pids.append(pid)
        if len(pids) > 1:
            issues.append(f"kênh {ch} có {len(pids)} profile khác nhau {pids} → video mới sẽ đăng cho TẤT CẢ các profile này")
        targets[ch] = pids
    return targets, issues


class ProfileSlot:
    def __init__(self, pid):
        self.pid = pid
        self.state = "CLOSED"
        self.reason = ""
        self.queue = collections.deque()
        self.cond = threading.Condition()
        self.open_lock = threading.Lock()
        self.thread = None
        self.last = ""
        self.opened_at = None


class TikTokBridge:
    def __init__(self, engine, cfg: dict, log, tam=None):
        self.eng = engine
        self.cfg = cfg
        self.log = log
        self.tam = tam
        self.db = engine.db
        self.targets: dict[str, list[str]] = {}
        self.slots: dict[str, ProfileSlot] = {}
        self.slots_lock = threading.Lock()
        self.stop_ev = threading.Event()
        self.accepting = False
        self.open_sem = threading.BoundedSemaphore(max(1, int(cfg.get("open_concurrency", 2))))
        self.upload_sem = threading.BoundedSemaphore(max(1, int(cfg.get("max_parallel_uploads", 4))))
        self.stats = {"opened": 0, "ready": 0, "waiting": 0, "uploading": 0, "confirmed": 0, "unknown_failed": 0,
                      "held": 0, "genlogin": "chưa khởi động", "last": {}}
        self.db.x("""CREATE TABLE IF NOT EXISTS upload_jobs(
            job_id TEXT PRIMARY KEY, video_id TEXT, channel_id TEXT, profile_id TEXT, path TEXT, size INTEGER,
            state TEXT, attempts INTEGER DEFAULT 0, reason TEXT, created_at TEXT, updated_at TEXT, evidence TEXT,
            marks TEXT, done_to_enqueue_ms REAL, enqueue_to_received_ms REAL, received_to_attach_ms REAL,
            done_to_attach_ms REAL, attach_to_post_ready_ms REAL, click_to_confirm_ms REAL, websub_to_confirm_ms REAL,
            dry_run INTEGER DEFAULT 0)""")
        for col in ("profile_wait_ms REAL", "page_reload_ms REAL", "page_reloaded INTEGER", "file_attach_ms REAL",
                    "post_ready_ms REAL", "post_confirmed_ms REAL", "wait_mode TEXT"):
            try:
                self.db.x(f"ALTER TABLE upload_jobs ADD COLUMN {col}")
            except Exception:  # noqa: BLE001 — cột đã có
                pass

    # ------------------------------------------------------------------ vòng đời
    def start(self):
        if self.tam is None:
            self.tam = load_tam(self.cfg.get("tam_path") or "tam_INSTANT_POST.py")
        self.stats["genlogin"] = ("OK" if getattr(self.tam, "Genlogin", None) is not None else "THIẾU Genlogin.py") + \
            (" | selenium OK" if getattr(self.tam, "webdriver", None) is not None else " | THIẾU selenium")
        wm = str(self.cfg.get("tam_wait_mode") or "").strip().lower()
        if wm and hasattr(self.tam, "CFG"):
            if wm in ("event", "sparse", "lite") and (wm == "event" or hasattr(self.tam, "JS_LITE_INSTALL")):
                self.tam.CFG.wait_mode = wm
            else:
                self.log(f"⚠️ TikTok: tam_wait_mode '{wm}' không dùng được với file TAM này — giữ '{self.tam.CFG.wait_mode}'")
        self.log(f"👁️ TikTok: cách theo dõi nút Post = {getattr(getattr(self.tam, 'CFG', None), 'wait_mode', '?')} "
                 f"(event=A watcher đầy đủ | lite=C chỉ theo dõi nút | sparse=B không cài gì khi upload)")
        cp = str(self.cfg.get("tam_click_path") or "").strip().lower()
        if cp and hasattr(self.tam, "CFG") and hasattr(self.tam.CFG, "click_path"):
            if cp in ("ws", "driver"):
                self.tam.CFG.click_path = cp
            else:
                self.log(f"⚠️ TikTok: tam_click_path '{cp}' không hợp lệ (ws | driver) — giữ '{self.tam.CFG.click_path}'")
        if hasattr(getattr(self.tam, "CFG", None), "click_path"):
            self.log(f"🖱️ TikTok: đường bấm Post = {self.tam.CFG.click_path} (ws = WebSocket CDP riêng, bấm ngay khi nút sáng | "
                     f"driver = qua chromedriver như 5.3.0)")
        self.reload_mapping()
        self.accepting = True
        self.recover()
        mode = str(self.cfg.get("prewarm_mode", "PREWARM_ALL")).upper()
        pids = sorted({p for lst in self.targets.values() for p in lst})
        if mode == "SMART_PREWARM":
            pre = [str(p) for p in (self.cfg.get("smart_profiles") or []) if str(p) in pids] or \
                pids[: max(1, int(self.cfg.get("open_concurrency", 2)))]
        else:
            pre = pids
        self.log(f"🎬 TikTok: {len(pids)} profile từ cột {self.cfg.get('col_profile', 'C')} | chế độ {mode} → mở sẵn {len(pre)} | "
                 f"mở đồng thời tối đa {self.cfg.get('open_concurrency', 2)} | {self.stats['genlogin']}"
                 + (" | ⚠️ CHẠY THỬ: KHÔNG bấm Post" if self.cfg.get("tiktok_dry_run") else ""))
        for pid in pids:
            self._slot(pid)
        self.stats["speed"] = f"ĐANG CHUẨN BỊ 0/{len(pre)} profile"
        ths = [threading.Thread(target=self._ensure_open, args=(pid,), daemon=True, name=f"OPEN-{pid}") for pid in pre]
        t0 = time.perf_counter_ns()
        [t.start() for t in ths]

        def announce():
            for t in ths:
                while t.is_alive() and not self.stop_ev.is_set():
                    t.join(0.5)
                    ok = sum(1 for p in pre if self.slots[p].state in ("READY", "BUSY"))
                    self.stats["speed"] = f"ĐANG CHUẨN BỊ {ok}/{len(pre)} profile"
            ok = [p for p in pre if self.slots[p].state in ("READY", "BUSY")]   # BUSY = đã mở và đang đăng video chờ
            el = _ms(t0, time.perf_counter_ns())
            if len(ok) == len(pre):
                self.stats["speed"] = f"SẴN SÀNG TỐC ĐỘ TỐI ĐA ({len(ok)}/{len(pre)})"
                self.log(f"🚀 TikTok: SẴN SÀNG TỐC ĐỘ TỐI ĐA — {len(ok)}/{len(pre)} profile READY sau {el:.0f} ms "
                         f"(video phát hiện trong lúc chuẩn bị đã được lưu bền vững và đăng ngay khi profile READY)")
            else:
                bad = [p for p in pre if p not in ok]
                self.stats["speed"] = f"CHỈ {len(ok)}/{len(pre)} READY"
                self.log(f"⚠️ TikTok: chỉ {len(ok)}/{len(pre)} profile READY sau {el:.0f} ms — chưa sẵn sàng: {', '.join(bad[:10])} "
                         f"(job của các profile này vẫn được giữ và thử mở lại)")
        threading.Thread(target=announce, daemon=True, name="TT-SPEED").start()

    def stop(self, wait_s=180):
        """Ngừng NHẬN job mới. Việc đang đăng được làm cho xong (không đóng trình duyệt giữa lúc bấm/chờ xác nhận)."""
        self.accepting = False
        self.stop_ev.set()
        for s in list(self.slots.values()):
            with s.cond:
                s.cond.notify_all()
        end = time.time() + wait_s
        for s in list(self.slots.values()):
            if s.thread is not None:
                s.thread.join(timeout=max(0.1, end - time.time()))
        self.log("⏹ TikTok: đã ngừng nhận job mới (profile vẫn mở; job chưa xong được giữ trong SQLite)")

    def reload_mapping(self):
        x = self.eng.excel
        try:
            targets, issues = read_profile_mapping(x.path, self.cfg.get("col_channel", "K"), self.cfg.get("col_profile", "C"),
                                                   self.cfg.get("excel_sheet", ""))
        except Exception as e:  # noqa: BLE001
            self.log(f"❌ TikTok: đọc ánh xạ Profile↔Kênh lỗi: {type(e).__name__}: {e} — giữ ánh xạ cũ")
            return
        old = self.targets
        self.targets = targets
        n = sum(len(v) for v in targets.values())
        if targets != old:
            self.log(f"📗 TikTok: ánh xạ {len(targets)} kênh → {len({p for v in targets.values() for p in v})} profile ({n} đích)")
            for i in issues[:20]:
                self.log(f"   ⚠️ {i}")
        for pid in {p for v in targets.values() for p in v}:
            self._slot(pid)

    # ------------------------------------------------------------------ profile
    def _slot(self, pid) -> ProfileSlot:
        with self.slots_lock:
            s = self.slots.get(pid)
            if s is None:
                s = self.slots[pid] = ProfileSlot(pid)
            if s.thread is None or not s.thread.is_alive():
                s.thread = threading.Thread(target=self._worker, args=(s,), daemon=True, name=f"TT-{pid}")
                s.thread.start()
            return s

    def _driver(self, pid):
        with self.tam.drivers_lock:
            return self.tam.drivers.get(str(pid))

    def _alive(self, drv) -> bool:
        try:
            _ = drv.current_url                      # 1 lệnh CDP nhẹ
            return True
        except Exception:
            return False

    def _ensure_open(self, pid) -> bool:
        """Mở profile bằng backend TAM gốc (không mở trùng; giới hạn số profile mở đồng thời)."""
        s = self._slot(pid)
        drv = self._driver(pid)
        if drv is not None and self._alive(drv):
            if s.state in ("CLOSED", "OPENING", "ERROR"):
                s.state = "READY"
            return True
        with s.open_lock:                            # không bao giờ 2 lần mở cùng profile
            drv = self._driver(pid)
            if drv is not None and self._alive(drv):
                s.state = "READY"
                return True
            if drv is not None:                      # driver chết → bỏ, mở lại có kiểm soát
                with self.tam.drivers_lock:
                    self.tam.drivers.pop(str(pid), None)
                self.log(f"♻️ TikTok {pid}: phiên Selenium mất kết nối → mở lại profile")
            s.state, s.reason = "OPENING", ""
            t0 = time.perf_counter_ns()
            with self.open_sem:
                if self.stop_ev.is_set():
                    s.state = "CLOSED"
                    return False
                try:
                    _, drv = self.tam.setup_and_navigate_profile(str(pid), None, self.tam.CHROME_DRIVER_PATH,
                                                                  self.tam.TARGET_UPLOAD_URL)
                except Exception as e:  # noqa: BLE001
                    drv = None
                    s.reason = f"{type(e).__name__}: {e}"
            if drv is None:
                s.state = "ERROR"
                s.reason = s.reason or "Genlogin không mở được (Profile ID không tồn tại / Genlogin chưa chạy?)"
                self.log(f"❌ TikTok {pid}: mở profile lỗi — {s.reason}")
                return False
            with self.tam.drivers_lock:
                self.tam.drivers[str(pid)] = drv
            s.opened_at = time.time()
            fsm = self.tam.get_fsm(str(pid))
            s.state = "UNKNOWN" if fsm.state == self.tam.PState.UNKNOWN else "READY"
            self.log(f"✅ TikTok {pid}: profile mở + trang Upload sẵn sàng ({_ms(t0, time.perf_counter_ns()):.0f} ms)")
            return True

    # ------------------------------------------------------------------ nhận job từ WS360 (đường nóng)
    def submit(self, info: dict) -> list[str]:
        """Gọi NGAY sau DOWNLOAD_DONE (file cuối đã có, ffprobe + dung lượng đạt). Ghi bền vững rồi mới báo worker."""
        t_enq = time.perf_counter_ns()
        vid, ch, path = info["vid"], info["ch"], info["path"]
        pids = list(self.targets.get(ch) or [])
        if not self.accepting:
            return []
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        marks = {k: info.get(k) for k in ("t_recv_ns", "t_detect_ns", "t_dl_start_ns", "t_done_ns")}
        marks["t_enq_ns"] = t_enq
        old = self._too_old(info.get("published"))
        if old and pids and not info.get("bench"):
            stmts = [("INSERT OR IGNORE INTO upload_jobs(job_id, video_id, channel_id, profile_id, path, size, state, reason, "
                      "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (f"{vid}:{pid}", vid, ch, pid, path, info.get("size"), "HELD", old, now, now)) for pid in pids]
            self.db.tx(stmts)
            self.stats["held"] += 1
            self.log(f"⏸ TikTok: {vid} — {old} → GIỮ job, KHÔNG tự đăng")
            return []
        if not pids:
            jid = f"{vid}:NO_PROFILE"
            self.db.x("INSERT OR IGNORE INTO upload_jobs(job_id, video_id, channel_id, profile_id, path, size, state, reason, "
                      "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (jid, vid, ch, "", path, info.get("size"), "HELD", "kênh chưa có Profile ID hợp lệ ở cột "
                       f"{self.cfg.get('col_profile', 'C')}", now, now))
            self.stats["held"] += 1
            self.log(f"⏸ TikTok: {vid} (kênh {ch}) KHÔNG có Profile ID hợp lệ → GIỮ job, không đăng nhầm profile khác")
            return []
        stmts = [("INSERT OR IGNORE INTO upload_jobs(job_id, video_id, channel_id, profile_id, path, size, state, created_at, "
                  "updated_at, marks, dry_run) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (f"{vid}:{pid}", vid, ch, pid, path, info.get("size"), "QUEUED", now, now,
                   json.dumps(marks), int(bool(self.cfg.get("tiktok_dry_run"))))) for pid in pids]
        self.db.tx(stmts)                                   # BỀN VỮNG trước khi đưa vào hàng đợi
        out = []
        for pid in pids:
            s = self._slot(pid)
            with s.cond:
                if not any(j["job_id"] == f"{vid}:{pid}" for j in s.queue):
                    s.queue.append({"job_id": f"{vid}:{pid}", "vid": vid, "ch": ch, "pid": pid, "path": path,
                                    "size": info.get("size"), "marks": dict(marks), "bench": bool(info.get("bench"))})
                s.cond.notify()
            out.append(f"{vid}:{pid}")
        self.stats["waiting"] = sum(len(s.queue) for s in self.slots.values())
        return out

    def _too_old(self, published) -> str:
        """Lý do chặn nếu video quá cũ để tự đăng ('' = được đăng)."""
        if not published:
            return ""
        try:
            from datetime import datetime, timezone
            p = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
            if p.tzinfo is None:
                p = p.replace(tzinfo=timezone.utc)
            age_min = (datetime.now(timezone.utc) - p).total_seconds() / 60
        except ValueError:
            return ""
        lim = float(self.cfg.get("tiktok_max_age_minutes", self.cfg.get("max_age_minutes", 30)) or 0)
        if lim > 0 and age_min > lim:
            return f"video cũ (đăng cách đây {age_min:.0f} phút > {lim:.0f} phút)"
        return ""

    def recover(self):
        n = 0
        for r in self.db.all("SELECT * FROM upload_jobs WHERE state IN ('QUEUED','RETRY','UPLOADING') ORDER BY created_at"):
            if r["state"] == "UPLOADING":
                # đang đăng thì tool tắt: JOURNAL của TAM quyết định — đã ghi POSTING/UNKNOWN thì KHÔNG tự đăng lại
                self.db.x("UPDATE upload_jobs SET state='QUEUED', reason='khôi phục sau khi tool tắt' WHERE job_id=?",
                          (r["job_id"],))
            s = self._slot(r["profile_id"])
            with s.cond:
                s.queue.append({"job_id": r["job_id"], "vid": r["video_id"], "ch": r["channel_id"], "pid": r["profile_id"],
                                "path": r["path"], "size": r["size"], "marks": {}, "recovered": True})
                s.cond.notify()
            n += 1
        # video ĐÃ tải xong trong chế độ tích hợp nhưng tool tắt trước khi tạo job (không đụng video cũ không có cờ tiktok)
        try:
            lost = self.db.all("SELECT v.* FROM videos v WHERE v.state='DONE' AND v.tiktok=1 AND NOT EXISTS "
                               "(SELECT 1 FROM upload_jobs u WHERE u.video_id=v.video_id)")
        except Exception:  # noqa: BLE001
            lost = []
        for r in lost:
            if r["file"] and Path(r["file"]).is_file():
                self.submit({"vid": r["video_id"], "ch": r["channel_id"], "path": r["file"], "size": r["size"],
                             "published": r["published_at"]})
                n += 1
        if n:
            self.log(f"♻️ TikTok: khôi phục {n} job đăng chưa xong")

    # ------------------------------------------------------------------ worker theo profile
    def _worker(self, s: ProfileSlot):
        while True:
            with s.cond:
                while not s.queue and not self.stop_ev.is_set():
                    s.cond.wait(timeout=1.0)
                if self.stop_ev.is_set():
                    return
                job = s.queue.popleft()
            t_recv = time.perf_counter_ns()
            try:
                self._run_job(s, job, t_recv)
            except Exception as e:  # noqa: BLE001 — không bao giờ chết worker
                self.log(f"❌ TikTok {s.pid}: lỗi bất ngờ với {job['vid']}: {type(e).__name__}: {e}")
                self._set(job, "RETRY", reason=f"{type(e).__name__}: {e}")
            self.stats["waiting"] = sum(len(x.queue) for x in self.slots.values())

    def _set(self, job, state, **kw):
        cols = ", ".join(f"{k}=?" for k in ("state", "updated_at", *kw))
        self.db.x(f"UPDATE upload_jobs SET {cols} WHERE job_id=?",
                  (state, time.strftime("%Y-%m-%d %H:%M:%S"), *kw.values(), job["job_id"]))

    def _check_file(self, job) -> str:
        p = Path(job["path"])
        if TEMP_RE.match(p.name):
            # file tạm của WS360 có mẫu cố định '.<ID>.part.mp4' — tiêu đề gốc như '.Part 2 ….mp4' vẫn hợp lệ
            return "file tạm (.part) — không bao giờ upload"
        if not p.is_file():
            return "file không tồn tại"
        if job.get("size") and p.stat().st_size != int(job["size"]):
            return f"dung lượng {p.stat().st_size} ≠ {job['size']} của job"
        try:
            with open(p, "rb") as f:
                f.read(16)
        except OSError as e:
            return f"không đọc được file: {e}"
        return ""

    def _run_job(self, s: ProfileSlot, job, t_recv):
        tam = self.tam
        row = self.db.one("SELECT state FROM upload_jobs WHERE job_id=?", (job["job_id"],))
        if row and row["state"] in TERMINAL:
            return
        if not job.get("bench"):
            pr = self.db.one("SELECT published_at FROM videos WHERE video_id=?", (job["vid"],))
            old = self._too_old(pr["published_at"] if pr is not None else None)
            if old:                                           # vd job khôi phục sau khi tool tắt lâu: không đăng muộn
                self._set(job, "HELD", reason=old)
                self.log(f"⏸ TikTok {s.pid}: {job['vid']} — {old} → GIỮ job, KHÔNG tự đăng")
                return
        bad = self._check_file(job)
        if bad:
            self._set(job, "HELD", reason=bad)
            self.log(f"⏸ TikTok {s.pid}: {job['vid']} — {bad} → GIỮ job")
            return
        key = tam.JOURNAL.file_key(job["path"])
        jst = tam.JOURNAL.status(key)
        if jst == "CONFIRMED" or jst in getattr(tam.JOURNAL, "BLOCKING", ("POSTING", "UNKNOWN", "FAILED_VISIBLE")):
            st = "DUPLICATE" if jst == "CONFIRMED" else "UNKNOWN"
            self._set(job, st, reason=f"nhật ký TAM đã có trạng thái {jst} cho file này → KHÔNG đăng lại")
            self.log(f"🛡️ TikTok {s.pid}: {job['vid']} đã {jst} trong nhật ký → bỏ qua (chống đăng trùng)")
            return
        # profile sẵn sàng? (không mở lại nếu đang READY)
        drv = self._driver(s.pid)
        if drv is None or not self._alive(drv):
            if not self._ensure_open(s.pid):
                self._set(job, "RETRY", reason=f"profile chưa mở được: {s.reason}")
                self._requeue_later(s, job, 30)
                return
            drv = self._driver(s.pid)
        t_ready = time.perf_counter_ns()
        fsm = tam.get_fsm(str(s.pid))
        if fsm.state == tam.PState.UNKNOWN:
            s.state = "UNKNOWN"
            self._set(job, "WAIT_USER", reason="profile đang UNKNOWN (lần đăng trước chưa xác minh) — kiểm tra tay rồi bấm 'Đã kiểm tra'")
            self.log(f"⛔ TikTok {s.pid}: profile UNKNOWN → giữ {job['vid']} chờ bạn kiểm tra (không đăng tiếp để tránh trùng)")
            with s.cond:
                s.queue.appendleft(job)
                s.cond.wait(timeout=30)
            return
        items = [(key, job["path"])]
        hook_state = {"posting": False}

        def before_click():
            tam.JOURNAL.record("POSTING", items, pid=s.pid)
            hook_state["posting"] = True
            return True

        def after_result(res):
            PS = tam.PostStatus
            if res.status == PS.CONFIRMED:
                tam.JOURNAL.record("CONFIRMED", items, pid=s.pid, durable=True, extra={"evidence": res.evidence})
            elif res.status == PS.UNKNOWN:
                tam.JOURNAL.record("UNKNOWN", items, pid=s.pid, durable=True, extra={"reason": res.reason})
            elif res.status == PS.FAILED_VISIBLE:
                tam.JOURNAL.record("FAILED_VISIBLE", items, pid=s.pid, durable=True, extra={"reason": res.reason})
            elif hook_state["posting"]:
                tam.JOURNAL.record("NOT_POSTED", items, pid=s.pid, durable=True, extra={"reason": res.reason})

        hooks = tam.JobHooks(before_click, after_result)
        trace = tam.PerfTrace(label="WS360", profile=str(s.pid), mode="WS360", n_files=1,
                              n_bytes=int(job.get("size") or 0))
        dry = bool(self.cfg.get("tiktok_dry_run"))
        with self.upload_sem:                                 # backpressure: giới hạn số phiên upload đồng thời
            with tam.get_profile_lock(s.pid):                 # khoá thực thi RIÊNG profile (dùng chung với TAM)
                s.state = "BUSY"
                self.stats["uploading"] += 1
                self._set(job, "UPLOADING", attempts=int((self.db.one(
                    "SELECT attempts FROM upload_jobs WHERE job_id=?", (job["job_id"],)) or {"attempts": 0})["attempts"] or 0) + 1)
                try:
                    res = tam.upload_video_to_tiktok_ex(drv, [job["path"]], logger_func=self.log,
                                                        check_force_kill=lambda: False, pid=str(s.pid), mode="WS360",
                                                        trace=trace, hooks=hooks, dry_run=dry)
                finally:
                    self.stats["uploading"] -= 1
                st = res.status.value
                timing = self._timing(job, t_recv, t_ready, trace)
                if st == "CONFIRMED":
                    self._set(job, "CONFIRMED", evidence=str(res.evidence)[:300], reason="", **timing)
                    self.stats["confirmed"] += 1
                elif st == "DRY_RUN":
                    self._set(job, "DRY_RUN_DONE", reason="chạy thử: nút Post đã sáng, KHÔNG bấm", **timing)
                elif st in ("UNKNOWN", "FAILED_VISIBLE"):
                    self._set(job, st, reason=str(res.reason)[:300], **timing)
                    self.stats["unknown_failed"] += 1
                elif st == "KILLED":
                    self._set(job, "QUEUED", reason="dừng trước khi bấm Post")
                else:                                         # FAILED_SAFE: chắc chắn CHƯA bấm → được thử lại
                    n = int(self.db.one("SELECT attempts FROM upload_jobs WHERE job_id=?", (job["job_id"],))["attempts"])
                    if n <= int(self.cfg.get("upload_retry_failed_safe", 1)):
                        self._set(job, "RETRY", reason=str(res.reason)[:300], **timing)
                        self._requeue_later(s, job, 2)
                    else:
                        self._set(job, "FAILED", reason=str(res.reason)[:300], **timing)
                        self.stats["unknown_failed"] += 1
                self.stats["last"] = dict(timing, vid=job["vid"], pid=s.pid, result=st)
                f_ = lambda k: "-" if timing.get(k) is None else f"{timing[k]:.0f}"
                self.log(f"⏱️ TIKTOK_STAGES {job['vid']} {s.pid} {st} | PROFILE_WAIT_MS {f_('profile_wait_ms')} | "
                         f"PAGE_RELOAD_MS {f_('page_reload_ms')} (nạp lại: {'CÓ' if timing.get('page_reloaded') else 'không'}) | "
                         f"FILE_ATTACH_MS {f_('file_attach_ms')} | POST_READY_MS {f_('post_ready_ms')} | "
                         f"POST_CONFIRMED_MS {f_('post_confirmed_ms')} | chế độ {timing.get('wait_mode')}")
                self.log(f"🎬 TikTok {s.pid}: {job['vid']} → {st} | DOWNLOAD_DONE_TO_TIKTOK_ATTACH={timing.get('done_to_attach_ms')} ms "
                         f"| file {Path(job['path']).name} | DONE→gắn file {timing.get('done_to_attach_ms')} ms "
                         f"(DONE→hàng đợi {timing.get('done_to_enqueue_ms')}, →worker nhận {timing.get('enqueue_to_received_ms')}, "
                         f"→gắn {timing.get('received_to_attach_ms')}) | gắn→POST_READY {timing.get('attach_to_post_ready_ms')} | "
                         f"click→xác nhận {timing.get('click_to_confirm_ms')} | WebSub→xác nhận {timing.get('websub_to_confirm_ms')} ms"
                         + (f" | {res.reason}" if st not in ("CONFIRMED",) and res.reason else ""))
                # giữ profile READY cho video kế tiếp (chỉ khi đã kết thúc an toàn)
                if st in ("CONFIRMED", "DRY_RUN", "FAILED_SAFE") and not self.stop_ev.is_set():
                    ok = tam.prepare_upload_page(drv, self.log, None, tam.get_fsm(str(s.pid)), None, tag=f"{s.pid}: ")
                    s.state = "READY" if ok else "ERROR"
                else:
                    s.state = "UNKNOWN" if st == "UNKNOWN" else ("READY" if st != "FAILED_VISIBLE" else "ERROR")

    def _requeue_later(self, s, job, delay):
        def later():
            if self.stop_ev.wait(delay):
                return
            with s.cond:
                s.queue.append(job)
                s.cond.notify()
        threading.Thread(target=later, daemon=True).start()

    def _timing(self, job, t_recv, t_ready, trace) -> dict:
        m = job.get("marks") or {}

        def mark(name):
            x = trace.marks.get(name) if trace is not None else None
            return getattr(x, "py_ns", None) if x is not None else None
        t_attach = mark("SEND_KEYS_START")
        t_post_ready = mark("PROCESSING_READY")
        t_click, t_conf = mark("POST_CLICKED"), mark("POST_CONFIRMED")
        flags = getattr(trace, "flags", {}) or {}
        return {"profile_wait_ms": _ms(t_recv, t_ready),                       # worker nhận job → profile READY
                "page_reload_ms": _ms(mark("JOB_START"), mark("PAGE_READY")),  # có trang Upload sạch (nạp lại nếu bẩn)
                "page_reloaded": flags.get("page_reloaded"),
                "file_attach_ms": _ms(t_attach, mark("SEND_KEYS_END")),
                "post_ready_ms": _ms(mark("SEND_KEYS_END"), t_post_ready),     # file đã gắn → nút Post sẵn sàng
                "post_confirmed_ms": _ms(t_click, t_conf),
                "wait_mode": getattr(getattr(self.tam, "CFG", None), "wait_mode", None),
                "done_to_enqueue_ms": _ms(m.get("t_done_ns"), m.get("t_enq_ns")),
                "enqueue_to_received_ms": _ms(m.get("t_enq_ns"), t_recv),
                "received_to_attach_ms": _ms(t_recv, t_attach),
                "done_to_attach_ms": _ms(m.get("t_done_ns"), t_attach),
                "attach_to_post_ready_ms": _ms(t_attach, t_post_ready),
                "click_to_confirm_ms": _ms(t_click, t_conf),
                "websub_to_confirm_ms": _ms(m.get("t_recv_ns"), t_conf),
                "marks": json.dumps({"websub_received": 0,
                                     "download_started_ms": _ms(m.get("t_recv_ns"), m.get("t_dl_start_ns")),
                                     "download_done_ms": _ms(m.get("t_recv_ns"), m.get("t_done_ns")),
                                     "job_enqueued_ms": _ms(m.get("t_recv_ns"), m.get("t_enq_ns")),
                                     "uploader_received_ms": _ms(m.get("t_recv_ns"), t_recv),
                                     "profile_ready_ms": _ms(m.get("t_recv_ns"), t_ready),
                                     "file_attach_started_ms": _ms(m.get("t_recv_ns"), t_attach),
                                     "file_attach_done_ms": _ms(m.get("t_recv_ns"), mark("SEND_KEYS_END")),
                                     "tiktok_upload_completed_ms": _ms(m.get("t_recv_ns"), mark("UPLOAD_COMPLETED")),
                                     "post_ready_ms": _ms(m.get("t_recv_ns"), t_post_ready),
                                     "post_clicked_ms": _ms(m.get("t_recv_ns"), t_click),
                                     "post_confirmed_ms": _ms(m.get("t_recv_ns"), t_conf)})}

    # ------------------------------------------------------------------ giao diện
    def reset_unknown(self, pid):
        fsm = self.tam.get_fsm(str(pid))
        fsm.reset_unknown("người dùng đã kiểm tra tay (WS360)")
        s = self._slot(str(pid))
        s.state = "READY"
        for r in self.db.all("SELECT * FROM upload_jobs WHERE profile_id=? AND state='WAIT_USER'", (str(pid),)):
            self.db.x("UPDATE upload_jobs SET state='QUEUED' WHERE job_id=?", (r["job_id"],))
        with s.cond:
            s.cond.notify_all()

    def snapshot(self) -> dict:
        rows = []
        for pid, s in sorted(self.slots.items()):
            rows.append({"pid": pid, "state": s.state, "queue": len(s.queue), "reason": s.reason[:80], "last": s.last})
        st = dict(self.stats)
        st["opened"] = sum(1 for s in self.slots.values() if s.state in ("READY", "BUSY", "UNKNOWN"))
        st["ready"] = sum(1 for s in self.slots.values() if s.state == "READY")
        st["profiles"] = rows
        return st
