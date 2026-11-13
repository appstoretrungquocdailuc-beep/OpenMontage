# -*- coding: utf-8 -*-
"""
tam_INSTANT_POST — backend upload TikTok (PRODUCTION v5, đã audit)

THAY ĐỔI SO VỚI BẢN CŨ (tóm tắt; chi tiết trong BAO_CAO_AUDIT):
  * Chỉ coi là THÀNH CÔNG khi có bằng chứng giao diện (POST_CONFIRMED). Không MOVE / DONE / reload / đóng profile trước đó.
  * Chờ nút Post theo SỰ KIỆN (observer nhẹ + execute_async_script theo lát), không polling 20 ms.
  * Click CDP có kiểm chứng trong trang; sau khi click đã chạm nút thì KHÔNG BAO GIỜ click lại (chống đăng đôi).
  * Retry chỉ khi lỗi xảy ra TRƯỚC khi bấm Post. Sau khi bấm mà không xác minh được => UNKNOWN, khóa thư mục, dừng quét.
  * Nhật ký bền vững (journal) chống đăng trùng khi crash/MOVE lỗi; sửa lỗi rơi file khi dùng "giới hạn video/profile".
  * Không upload file đang ghi dở (ổn định kích thước/mtime, atomic rename, file .part, marker tuỳ chọn).
  * Không còn ngủ cố định: 5 s giữa 2 video (TT), 0,15 s/thư mục khi quét, reload tuần tự -> song song.
  * Stage RAM disk chỉ khi bật TAM_STAGE=ram (mặc định đọc thẳng từ nguồn).
  * Đo PERF 11 mốc bằng perf_counter_ns + đồng bộ đồng hồ Chrome; ghi JSONL để tính median/P95 (xem --help).

NÚM CHỈNH (biến môi trường, đều có mặc định an toàn; dùng để A/B bằng số đo thật trên máy của bạn):
  TAM_VARIANT=<nhãn>            nhãn ghi vào nhật ký PERF để gom nhóm khi so sánh
  TAM_WAIT_MODE=event|poll      event (mặc định): chờ nút Post theo sự kiện;  poll: hỏi trang mỗi TAM_POLL_S (kiểu cũ, để so sánh)
  TAM_STAGE=direct|ram          direct (mặc định): đọc thẳng file nguồn;  ram: hardlink/copy sang R:\\Temp như bản cũ
  TAM_FORCE_RELOAD=0|1          1: luôn nạp lại trang Upload trước mỗi việc (so với giữ trang mở sẵn)
  TAM_PAGE_LOAD_STRATEGY=normal|eager, TAM_HIGH_PRIORITY=1|0, TAM_BRING_TO_FRONT=0|1
  TAM_READY_TIMEOUT_S, TAM_CONFIRM_TIMEOUT_S, TAM_STABLE_MS, TAM_UPLOAD_URL, TAM_JOURNAL, TAM_PERF_LOG
  TAM_CONFIRM_URL_RE, TAM_CONFIRM_TEXT_RE, TAM_FAIL_TEXT_RE   (bằng chứng "đã đăng"; hiệu chuẩn bằng --calibrate-confirm)
  TAM_UPLOAD_DONE_SEL, TAM_UPLOAD_DONE_TEXT_RE                (tuỳ chọn: tín hiệu "upload xong" → đo NetworkUpload và chặn bấm sớm)
  (và các biến cũ TIKTOK_SCAN_INTERVAL / TIKTOK_DOUBLESCAN_WAIT / TIKTOK_CUM_BATCH vẫn được hiểu).

Chạy GUI một profile (như cũ):   python tam_INSTANT_POST.py
Công cụ dòng lệnh:                python tam_INSTANT_POST.py --help
"""
import argparse
import collections
import contextlib
import datetime
import difflib
import enum
import hashlib
import io
import json
import math
import os
import queue
import random
import re
import shutil
import statistics
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from functools import partial
from typing import Any, Callable, Dict, List, Optional, Tuple

BACKEND_NAME = "tam_INSTANT_POST"
BACKEND_VERSION = "5.2.0"
BACKEND_API_LEVEL = 5          # GUI bắt buộc khớp số này (chống GUI mới chạy backend cũ)


def _env_str(name: str, default: str) -> str:
    v = os.environ.get(name)
    return default if v is None or v.strip() == "" else v.strip()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, "").strip() or default))
    except Exception:
        return int(default)


def _env_flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "y")


# --- ƯU TIÊN HIGH cho tiến trình điều khiển (giữ hành vi cũ; tắt bằng TAM_HIGH_PRIORITY=0 để A/B) ---
try:
    import psutil

    def set_high_priority():
        try:
            p = psutil.Process(os.getpid())
            p.nice(psutil.HIGH_PRIORITY_CLASS)
            print("🚀 [System] Đã kích hoạt chế độ ưu tiên HIGH PRIORITY.")
        except Exception as e:
            print(f"⚠️ Không thể thiết lập ưu tiên: {e}")
    if os.name == "nt" and _env_flag("TAM_HIGH_PRIORITY", True):       # HIGH_PRIORITY_CLASS chỉ có trên Windows
        set_high_priority()
except ImportError:
    psutil = None
    print("⚠️ Chưa cài psutil, bỏ qua set priority.")

# --- BẮT SỰ KIỆN THƯ MỤC ZERO-DELAY ---
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except ImportError:
    Observer = None
    FileSystemEventHandler = object
    print("⚠️ Chưa cài 'watchdog' (pip install watchdog). Sẽ dùng chế độ trễ mặc định.")

# -------------------- Dependencies --------------------
try:
    import pandas as pd
    from openpyxl import load_workbook
except Exception:
    pd = None
    load_workbook = None

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, ElementClickInterceptedException, WebDriverException
except Exception:
    webdriver = None

    class WebDriverException(Exception):            # chỉ để mã bên dưới vẫn nạp được khi thiếu selenium
        pass

    class TimeoutException(WebDriverException):
        pass

    class ElementClickInterceptedException(WebDriverException):
        pass

try:
    from Genlogin import Genlogin
except Exception:
    Genlogin = None

# -------------------- Constants & Config --------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_EXCEL_PATH = os.path.join(BASE_DIR, "check.xlsx")
LINK_COL = "LINK VIDEO GỐC"
PID_COL = "PROFILE ID"
TITLE_COLS = ["TIÊU ĐỀ", "TITLE"]
UPLOAD_COL = "UPLOAD"
CHROMEDRIVER = os.path.join(BASE_DIR, "chromedriver.exe")
CHROME_DRIVER_PATH = CHROMEDRIVER
TARGET_UPLOAD_URL = _env_str("TAM_UPLOAD_URL", "https://www.tiktok.com/tiktokstudio/upload?from=creator_center")

SCAN_INTERVAL = float(os.getenv("TIKTOK_SCAN_INTERVAL", "0.5"))        # chu kỳ an toàn khi không có sự kiện
DOUBLESCAN_WAIT = float(os.getenv("TIKTOK_DOUBLESCAN_WAIT", "0.15"))   # giữ để tương thích; KHÔNG còn ngủ theo từng thư mục
CUM_BATCH = int(os.getenv("TIKTOK_CUM_BATCH", "0"))
CONFIG_THEME_JSON = os.path.join(BASE_DIR, "chiadeu_theme.json")
CONFIG_APP_JSON = os.path.join(BASE_DIR, "premium_profile_uploader_config.json")

UC_RE = re.compile(r"(UC[0-9A-Za-z_-]{22})")
YTID_RE = re.compile(r"\[([0-9A-Za-z_-]{11})\]")
GENLOGIN_API_TOKEN = ""

VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi")


def _rx(env_name: str, default: str) -> "re.Pattern":
    pat = _env_str(env_name, default)
    try:
        return re.compile(pat, re.I)
    except re.error:
        print(f"⚠️ {env_name} không phải regex hợp lệ, dùng mặc định.")
        return re.compile(default, re.I)


class _Cfg:
    """Mọi núm chỉnh đọc từ biến môi trường. CFG.reload() đọc lại (dùng cho kiểm thử/A-B)."""

    def __init__(self):
        self.reload()

    def reload(self):
        self.variant = _env_str("TAM_VARIANT", "default")
        self.wait_mode = _env_str("TAM_WAIT_MODE", "event").lower()          # event (A) | sparse (B) | lite (C) | poll (cũ)
        self.sparse_ms = max(20, _env_int("TAM_SPARSE_MS", 250))             # B: chu kỳ đọc 1 thuộc tính nút Post (không cài gì vào trang)
        self.lite_tick_ms = max(20, _env_int("TAM_LITE_TICK_MS", 100))       # C: chu kỳ kiểm tra nút còn trong DOM (không layout)
        self.poll_s = max(0.005, _env_float("TAM_POLL_S", 0.02))             # chỉ dùng khi wait_mode=poll
        self.stage = _env_str("TAM_STAGE", "direct").lower()                 # direct | ram
        self.page_load = _env_str("TAM_PAGE_LOAD_STRATEGY", "normal").lower()
        self.slice_ms = max(50, _env_int("TAM_SLICE_MS", 400))               # độ dài một lát chờ (độ nhạy Hard Stop)
        self.ready_timeout_s = _env_float("TAM_READY_TIMEOUT_S", 90.0)       # như bản cũ: 90 s
        self.confirm_timeout_s = _env_float("TAM_CONFIRM_TIMEOUT_S", 40.0)
        self.page_ready_timeout_s = _env_float("TAM_PAGE_READY_TIMEOUT_S", 30.0)
        self.file_input_wait_s = _env_float("TAM_FILE_INPUT_WAIT_S", 10.0)                # như bản gốc: chờ ô nhập file 10 s (rồi refresh, chờ thêm 10 s)
        self.stable_ms = max(0, _env_int("TAM_STABLE_MS", 600))              # file phải không đổi chừng này ms
        self.stable_warn_s = _env_float("TAM_STABLE_WARN_S", 300.0)
        self.lock_grace_s = _env_float("TAM_LOCK_GRACE_S", 60.0)             # Windows: file bị khoá ghi quá lâu mà không đổi => cảnh báo rồi cho qua
        self.move_wait_s = _env_float("TAM_MOVE_WAIT_S", 8.0)                # MOVE gặp khoá: thử lại tối đa chừng này giây
        self.require_marker = _env_str("TAM_REQUIRE_MARKER", "")             # vd "READY.flag": chỉ xử lý khi có file này
        self.ignore_intermediate = _env_flag("TAM_IGNORE_INTERMEDIATE", True)
        self.sel_post = _env_str("TAM_SEL_POST", "button[data-e2e='post_video_button']")
        self.sel_file = _env_str("TAM_SEL_FILE_INPUT", "input[type='file']")
        # Mặc định dưới đây là GIẢ ĐỊNH về giao diện TikTok Studio, CHƯA kiểm chứng; hiệu chuẩn bằng --calibrate-confirm.
        self.confirm_url_re = _rx("TAM_CONFIRM_URL_RE", r"(tiktokstudio/content|manage/videos|creator-center/content|creator#/content)")
        self.confirm_text_re = _rx("TAM_CONFIRM_TEXT_RE", r"(uploaded to tiktok|video (published|posted)|your video (has been|was) (published|posted|uploaded)|đã (được )?(đăng|tải lên) )")
        self.fail_text_re = _rx("TAM_FAIL_TEXT_RE", r"(couldn'?t (post|upload|publish)|failed to (post|upload|publish)|something went wrong|please try again|không thể đăng|đăng thất bại|tải lên thất bại)")
        self.upload_done_sel = _env_str("TAM_UPLOAD_DONE_SEL", "")           # CSS selector (tuỳ chọn): xuất hiện khi upload xong => mốc UPLOAD_COMPLETED
        self.upload_done_re = _env_str("TAM_UPLOAD_DONE_TEXT_RE", "")        # regex (tuỳ chọn) trên văn bản mới xuất hiện khi upload xong
        self.ready_stable_ms = max(0, _env_int("TAM_READY_STABLE_MS", 0))     # nút Post phải sáng liên tục chừng này ms mới bấm (0 = như bản cũ)
        self.click_attempts = max(1, _env_int("TAM_CLICK_ATTEMPTS", 3))
        self.click_move = _env_flag("TAM_CLICK_MOVE", True)                  # gửi mouseMoved trước khi nhấn (như bản cũ)
        self.upload_url_path = _env_str("TAM_UPLOAD_URL_PATH", "")           # rỗng => lấy path của TARGET_UPLOAD_URL
        self.bring_to_front = _env_flag("TAM_BRING_TO_FRONT", False)         # CDP Page.bringToFront trước khi upload (A/B tab nền); mặc định TẮT
        self.perf_enable = _env_flag("TAM_PERF", True)
        self.perf_log = _env_str("TAM_PERF_LOG", os.path.join(BASE_DIR, "tam_perf_log.jsonl"))
        self.perf_verbose = _env_flag("TAM_PERF_VERBOSE", False)
        self.journal_path = _env_str("TAM_JOURNAL", os.path.join(BASE_DIR, "tam_post_journal.jsonl"))
        self.profile_stagger_s = _env_float("TAM_PROFILE_STAGGER_S", 0.0)    # bản cũ: 0,1 s giữa các profile
        self.reload_parallel = _env_flag("TAM_RELOAD_PARALLEL", True)
        self.force_reload = _env_flag("TAM_FORCE_RELOAD", False)             # A/B: luôn nạp lại trang Upload trước mỗi việc (thay vì giữ trang mở sẵn)
        # --- AttachToReady (gắn file -> nút Post sáng): các núm A/B, MẶC ĐỊNH TẮT cho tới khi bạn có số đo trên máy thật ---
        self.focus_emu = _env_flag("TAM_FOCUS_EMU", False)                   # CDP Emulation.setFocusEmulationEnabled: tab bị che/nền vẫn chạy như tab hiển thị (xem báo cáo)
        self.warm_ms = max(0, _env_int("TAM_WARM_MS", 0))                    # chờ trang đủ "tuổi" (ms kể từ lúc nạp) và readyState=complete rồi mới gắn file; 0 = không chờ
        self.rec_enable = _env_flag("TAM_REC", False)                        # ghi thụ động: Resource Timing, longtask, sự kiện chọn file, CDP Performance (chi phí nhỏ; mặc định tắt)
        self.upload_url_re = _env_str("TAM_UPLOAD_URL_RE", "")               # regex trên host+path của yêu cầu upload video => tách NetworkUpload / TikTokProcessing


CFG = _Cfg()


def backend_info() -> Dict[str, Any]:
    """Dùng để GUI/nhật ký xác minh ĐÚNG file backend đang chạy."""
    path = os.path.abspath(__file__)
    try:
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()[:12]
    except Exception:
        digest = "?"
    return {"name": BACKEND_NAME, "version": BACKEND_VERSION, "api": BACKEND_API_LEVEL, "file": path, "sha256_12": digest}


# --- GLOBAL LOCK: CHỐNG CHỒNG CHÉO PROFILE KHI CHẠY MULTI-GROUP ---
GLOBAL_PROFILE_LOCKS: Dict[str, threading.Lock] = {}
global_locks_lock = threading.Lock()


def get_profile_lock(pid) -> threading.Lock:
    pid = str(pid)
    with global_locks_lock:
        if pid not in GLOBAL_PROFILE_LOCKS:
            GLOBAL_PROFILE_LOCKS[pid] = threading.Lock()
        return GLOBAL_PROFILE_LOCKS[pid]


drivers: Dict[str, Any] = {}
drivers_lock = threading.Lock()


# ======================================================================================
# PHẦN 1 — TIỆN ÍCH (giữ nguyên hành vi cũ) + PHÂN PHỐI FILE (đã sửa lỗi rơi file)
# ======================================================================================
def quit_genlogin_driver(profile_id, genlogin_token):
    try:
        if Genlogin is None:
            return
        gen = Genlogin(GENLOGIN_API_TOKEN)
        gen.stopProfile(str(profile_id))
    except Exception as e:
        print(f"Lỗi khi dừng Genlogin profile {profile_id}: {e}")


SAFE_NAME_RE = re.compile(r'[\/\:*?"<>|\r\n]+')


def _normalize(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"[\U00010000-\U0010ffff]", "", str(s))
    s = re.sub(r"[^\w぀-ヿ一-鿿]", "", s)
    return s.lower().strip()


def fuzzy_ok(a: str, b: str, threshold: float = 0.7) -> bool:
    A = _normalize(a)
    B = _normalize(b)
    if not A or not B:
        return False
    if A in B or B in A:
        return True
    if difflib.SequenceMatcher(None, A, B).ratio() >= threshold:
        return True
    for i in range(len(A)):
        for j in range(len(B)):
            k = 0
            while i + k < len(A) and j + k < len(B) and A[i + k] == B[j + k]:
                k += 1
            if k >= 15:
                return True
    return False


def _link_to_canonical(v: str) -> str:
    v = v.strip()
    m = UC_RE.fullmatch(v)
    if m:
        return f"https://www.youtube.com/channel/{m.group(1)}"
    if v.startswith("http") and "youtube.com" in v:
        return v
    return ""


def plan_distribution_ex(video_paths, pids, per_profile_limit=0):
    """Chia video cho các profile. TRẢ VỀ (plan, leftover).

    Cách chia GIỐNG HỆT bản cũ (cùng thứ tự, cùng kích thước chunk). Khác duy nhất: bản cũ nhánh
    'giới hạn video/profile' lặng lẽ BỎ RƠI các file vượt n_profile*limit (rồi folder vẫn bị MOVE
    sang DONE => mất file chưa đăng). Ở đây phần dư được trả riêng trong `leftover` để Scanner
    đăng ở lượt sau và chỉ MOVE khi đã xác nhận hết.
    """
    files = list(video_paths)
    pids = list(pids)
    total = len(files)
    n = len(pids)
    if total == 0 or n == 0:
        return [(pid, []) for pid in pids], list(files)

    if per_profile_limit and per_profile_limit > 0:
        sizes = [0 for _ in range(n)]
        file_idx = 0
        while file_idx < total:
            assigned = False
            for i in range(n):
                if sizes[i] < per_profile_limit:
                    sizes[i] += 1
                    file_idx += 1
                    assigned = True
                    if file_idx >= total:
                        break
            if not assigned and file_idx < total:
                break
    else:
        q, r = divmod(total, n)
        sizes = [(q + (1 if i < r else 0)) for i in range(n)]

    result = []
    start = 0
    for i, pid in enumerate(pids):
        sz = max(0, min(sizes[i], total - start))
        end = start + sz
        chunk = files[start:end] if sz > 0 else []
        start = end
        result.append((pid, chunk))
    return result, files[start:]


def plan_distribution(video_paths, pids, per_profile_limit=0):
    """Giữ tên/chữ ký cũ để tương thích. Chỉ trả plan (phần dư xem plan_distribution_ex)."""
    plan, _left = plan_distribution_ex(video_paths, pids, per_profile_limit)
    return plan


excel_lock = threading.Lock()


def write_upload_done(*args, **kwargs):
    """CỐ Ý LÀ STUB (giống bản gốc): tính năng ghi Excel đã bị vô hiệu hoá ở GUI (excel_path luôn rỗng,
    nút chọn Excel báo 'đã bị vô hiệu hóa', infer_channel_for_folder trả dữ liệu giả).
    Scanner chỉ gọi hàm này SAU KHI POST_CONFIRMED, nên nếu sau này bật lại ghi 'DONE' thì đã an toàn.
    """
    return True


def infer_channel_for_folder(folder_path: str, groups: dict, logger=None):
    MOCK_CHANNEL_URL = "https://www.youtube.com/mock-channel"
    MOCK_GROUP = {"original_url": MOCK_CHANNEL_URL, "uploader_ids": list(drivers.keys())}
    return MOCK_CHANNEL_URL, MOCK_GROUP, "mocked_group"


def _extract_channel_url(url_or_video: str) -> str:
    if "watch?v=" in url_or_video:
        return ""
    m = UC_RE.search(url_or_video)
    if m:
        return f"https://www.youtube.com/channel/{m.group(1)}"
    return ""


# --- tiện ích nhỏ dùng chung ---
def _ms(ns: Optional[int]) -> str:
    """ns -> chuỗi 'xxx.x ms'; None -> 'UNKNOWN' (không bao giờ bịa số)."""
    if ns is None:
        return "UNKNOWN"
    return f"{ns / 1e6:.1f}ms"


def sleep_interruptible(seconds: float, check_kill: Optional[Callable[[], bool]] = None, step: float = 0.05) -> bool:
    """Ngủ nhưng ngắt được bởi Hard Stop. Trả True nếu ngủ đủ, False nếu bị ngắt."""
    end = time.perf_counter() + max(0.0, seconds)
    while True:
        if check_kill and check_kill():
            return False
        left = end - time.perf_counter()
        if left <= 0:
            return True
        time.sleep(min(step, left))


def _tail(path: str, n: int = 2) -> str:
    parts = os.path.normpath(path).replace("\\", "/").split("/")
    return "/".join(parts[-n:])


# ======================================================================================
# PHẦN 2 — ĐO PERF: perf_counter_ns (Python) + performance.now() (Chrome) + đồng bộ đồng hồ
# Nguyên tắc: mốc nào không đo tin cậy được thì ghi UNKNOWN. Tuyệt đối không bịa số.
# ======================================================================================
MILESTONES = (
    "FILE_READY", "FILE_DETECTED", "STAGE_START", "STAGE_END", "SEND_KEYS_START", "SEND_KEYS_END",
    "UPLOAD_COMPLETED", "PROCESSING_READY", "POST_CLICKED", "POST_CONFIRMED", "NEXT_PROFILE_READY",
)
# Mốc phụ (không nằm trong 11 mốc theo yêu cầu): JOB_START = lúc bắt đầu một lần đăng (trước khi chuẩn bị trang Upload).

# (tên chỉ số, mốc đầu, mốc cuối, nhóm thời gian)
INTERVALS = (
    ("Detect", "FILE_READY", "FILE_DETECTED", "scan"),
    ("Stage", "STAGE_START", "STAGE_END", "browser"),
    ("PrepareToAttach", "JOB_START", "SEND_KEYS_START", "browser"),     # trang Upload sẵn sàng + cài watcher (có nạp lại trang thì tăng)
    ("FileAttach", "SEND_KEYS_START", "SEND_KEYS_END", "browser"),
    ("NetworkUpload", "SEND_KEYS_END", "UPLOAD_COMPLETED", "transfer"),
    ("TikTokProcessing", "UPLOAD_COMPLETED", "PROCESSING_READY", "tiktok"),
    ("AttachToReady", "SEND_KEYS_END", "PROCESSING_READY", "transfer+tiktok"),
    ("AttachToReadyPage", "FILE_CHANGE", "PROCESSING_READY", "transfer+tiktok"),   # cả hai mốc đo bằng ĐỒNG HỒ CỦA TRANG (sự kiện change của ô file -> nút Post sáng): dùng chung cho đăng tay và tool
    ("ReadyToClick", "PROCESSING_READY", "POST_CLICKED", "browser"),
    ("ClickToConfirmed", "POST_CLICKED", "POST_CONFIRMED", "tiktok"),
    ("TotalConfirmed", "FILE_READY", "POST_CONFIRMED", "tong"),
    ("NextProfileReady", "POST_CONFIRMED", "NEXT_PROFILE_READY", "browser"),
)

SYNC_JS = r"""
 try { if (!window.__tamDoc) window.__tamDoc = Math.random().toString(36).slice(2) + Date.now().toString(36); } catch (e) {}
 return [performance.now(), window.__tamDoc || null];
"""


class ClockSync:
    """Ánh xạ performance.now() của MỘT tài liệu Chrome sang perf_counter_ns của Python.

    * Bắt tay nhiều lần, lấy mẫu có RTT nhỏ nhất. Script chắc chắn chạy trong khoảng [t0,t1] của Python,
      nên sai số của offset bị chặn cứng bởi ±RTT/2 (không phải giả định đối xứng).
    * performance.timeOrigin đổi theo từng lần điều hướng => mỗi tài liệu có doc_id riêng (window.__tamDoc);
      mốc của tài liệu cũ KHÔNG được quy đổi bằng offset của tài liệu mới (trả về None -> UNKNOWN).
    """

    def __init__(self):
        self.doc_id = None          # type: Optional[str]
        self.offset_ns = None       # type: Optional[int]
        self.err_ns = None          # type: Optional[int]
        self.rtt_ns = None          # type: Optional[int]

    def sync(self, driver, samples: int = 4) -> bool:
        best = None
        for _ in range(max(1, samples)):
            t0 = time.perf_counter_ns()
            try:
                r = driver.execute_script(SYNC_JS)
            except Exception:
                return False
            t1 = time.perf_counter_ns()
            if not r or r[0] is None:
                return False
            rtt = t1 - t0
            if best is None or rtt < best[0]:
                best = (rtt, (t0 + t1) // 2, float(r[0]), r[1])
        if best is None:
            return False
        rtt, mid, cms, doc = best
        return self.add_sample(mid - rtt // 2, mid + (rtt - rtt // 2), cms, doc)

    def add_sample(self, t0_ns: int, t1_ns: int, chrome_ms, doc_id) -> bool:
        """Thêm một mẫu bắt tay (vd. giá trị `now` trả về từ lệnh cài watcher). Giữ mẫu có RTT nhỏ nhất của CÙNG tài liệu."""
        if chrome_ms is None or doc_id is None:
            return False
        rtt = t1_ns - t0_ns
        if rtt < 0:
            return False
        if self.doc_id == doc_id and self.rtt_ns is not None and rtt >= self.rtt_ns:
            return True
        self.offset_ns = (t0_ns + t1_ns) // 2 - int(float(chrome_ms) * 1e6)
        self.err_ns = rtt // 2
        self.rtt_ns = rtt
        self.doc_id = doc_id
        return True

    def valid_for(self, doc_id) -> bool:
        return self.offset_ns is not None and doc_id is not None and doc_id == self.doc_id

    def to_py_ns(self, chrome_ms, doc_id):
        """(py_ns, err_ns) hoặc (None, None) nếu không quy đổi tin cậy được."""
        if chrome_ms is None or not self.valid_for(doc_id):
            return None, None
        return self.offset_ns + int(float(chrome_ms) * 1e6), self.err_ns


class _Mark:
    __slots__ = ("py_ns", "err_ns", "src", "chrome_ms", "doc_id")

    def __init__(self, py_ns, err_ns, src, chrome_ms=None, doc_id=None):
        self.py_ns = py_ns
        self.err_ns = err_ns
        self.src = src
        self.chrome_ms = chrome_ms
        self.doc_id = doc_id


class PerfTrace:
    """Dấu vết một việc đăng (một profile × một lượt gửi file)."""

    def __init__(self, label: str = "", profile: str = "", mode: str = "", n_files: int = 0, n_bytes: int = 0):
        self.label = label
        self.profile = str(profile)
        self.mode = mode
        self.n_files = n_files
        self.n_bytes = n_bytes
        self.marks = {}                 # type: Dict[str, _Mark]
        self.flags = {}                 # type: Dict[str, Any]
        self.outcome = "UNKNOWN"
        self.sync = None                # type: Optional[ClockSync]

    # ---- ghi mốc ----
    def mark(self, name: str, t_ns: Optional[int] = None, src: str = "py", err_ns: int = 0, overwrite: bool = False):
        if name in self.marks and not overwrite:
            return
        self.marks[name] = _Mark(time.perf_counter_ns() if t_ns is None else t_ns, err_ns, src)

    def mark_chrome(self, name: str, chrome_ms, doc_id, sync: Optional[ClockSync] = None, overwrite: bool = False):
        """Mốc đo bằng đồng hồ Chrome. Giữ cả giá trị gốc (để trừ cùng-đồng-hồ => không có sai số đồng bộ)."""
        if name in self.marks and not overwrite:
            return
        sync = sync or self.sync
        py, err = (None, None)
        if sync is not None:
            py, err = sync.to_py_ns(chrome_ms, doc_id)
        self.marks[name] = _Mark(py, err, "chrome", chrome_ms, doc_id)

    def mark_unknown(self, name: str, why: str = ""):
        if name not in self.marks:
            self.marks[name] = _Mark(None, None, "unknown:" + why if why else "unknown")

    def mark_file_ready(self, mtime_ns: Optional[int], moved_event_ns: Optional[int] = None, marker_mtime_ns: Optional[int] = None):
        """FILE_READY = lúc file thực sự xong. Ước lượng từ mtime (hoặc sự kiện rename/marker nếu có và muộn hơn).
        Đây là ƯỚC LƯỢNG (mtime phụ thuộc đồng hồ ổ đĩa) nên src ghi rõ và Total có dấu '≈'."""
        now_wall = time.time_ns()
        now_py = time.perf_counter_ns()
        cands = []
        src = []
        if mtime_ns:
            cands.append(now_py - (now_wall - mtime_ns))
            src.append("mtime")
        if marker_mtime_ns:
            cands.append(now_py - (now_wall - marker_mtime_ns))
            src.append("marker")
        if moved_event_ns:
            cands.append(moved_event_ns)
            src.append("rename-event")
        if not cands:
            self.mark_unknown("FILE_READY", "no-mtime")
            return
        t = max(cands)
        if t > now_py + 50_000_000:     # mtime ở tương lai > 50 ms => lệch đồng hồ, không tin
            self.mark_unknown("FILE_READY", "mtime-in-future")
            return
        self.marks["FILE_READY"] = _Mark(min(t, now_py), 0, "+".join(src))

    def set(self, key: str, value):
        self.flags[key] = value

    # ---- đọc/tính ----
    def get(self, name: str):
        m = self.marks.get(name)
        return None if m is None else m.py_ns

    def _delta(self, a: str, b: str):
        """(ns, err_ns, approx) hoặc (None, None, False)."""
        ma, mb = self.marks.get(a), self.marks.get(b)
        if ma is None or mb is None:
            return None, None, False
        # cùng đồng hồ Chrome + cùng tài liệu => chính xác tuyệt đối (không cần đồng bộ)
        if ma.chrome_ms is not None and mb.chrome_ms is not None and ma.doc_id is not None and ma.doc_id == mb.doc_id:
            return int((float(mb.chrome_ms) - float(ma.chrome_ms)) * 1e6), 0, False
        if ma.py_ns is None or mb.py_ns is None:
            return None, None, False
        err = (ma.err_ns or 0) + (mb.err_ns or 0)
        approx = ("mtime" in ma.src) or ("marker" in ma.src) or ("rename" in ma.src)
        return mb.py_ns - ma.py_ns, err, approx

    def durations(self) -> Dict[str, Dict[str, Any]]:
        out = {}
        for name, a, b, grp in INTERVALS:
            ns, err, approx = self._delta(a, b)
            if ns is not None and ns < -((err or 0) + 1_000_000):   # âm vượt sai số đo => mốc không nhất quán => UNKNOWN
                ns, err = None, None
            if ns is not None and ns < 0:
                ns = 0
            out[name] = {"ns": ns, "err_ns": err, "approx": approx, "group": grp}
        return out

    def render(self) -> str:
        parts = ["[PERF]"]
        if self.profile:
            parts.append(f"pid={self.profile}")
        parts.append(f"files={self.n_files}")
        segs = []
        for name, d in self.durations().items():
            if d["ns"] is None:
                segs.append(f"{name}=UNKNOWN")
                continue
            ap = "≈" if d["approx"] else ""
            er = f"±{d['err_ns'] / 1e6:.1f}" if d["err_ns"] and d["err_ns"] > 100_000 else ""
            segs.append(f"{name}={ap}{d['ns'] / 1e6:.1f}ms{er}")
        return " ".join(parts) + " | " + " | ".join(segs) + f" | outcome={self.outcome}"

    def record(self) -> Dict[str, Any]:
        d = self.durations()
        return {
            "ts": datetime.datetime.now().isoformat(timespec="milliseconds"),
            "variant": CFG.variant, "label": self.label, "profile": self.profile, "mode": self.mode,
            "files": self.n_files, "bytes": self.n_bytes, "outcome": self.outcome,
            "ms": {k: (None if v["ns"] is None else round(v["ns"] / 1e6, 3)) for k, v in d.items()},
            "err_ms": {k: (None if v["err_ns"] is None else round(v["err_ns"] / 1e6, 3)) for k, v in d.items()},
            "approx": [k for k, v in d.items() if v["approx"]],
            "group": {k: v["group"] for k, v in d.items()},
            "marks_src": {k: m.src for k, m in self.marks.items()},
            "flags": self.flags,
        }


class PerfLog:
    """Ghi JSONL (mỗi dòng một việc) để tính median/P95 sau. Lỗi ghi file không bao giờ làm hỏng việc đăng."""

    def __init__(self):
        self._lock = threading.Lock()
        self._warned = False

    def write(self, rec: Dict[str, Any]):
        if not CFG.perf_enable or not CFG.perf_log:
            return
        try:
            line = json.dumps(rec, ensure_ascii=False, default=str)       # default=str: một giá trị lạ không được làm mất cả bản ghi
            with self._lock:
                with open(CFG.perf_log, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception as e:
            if not self._warned:
                self._warned = True
                print(f"⚠️ [PERF] Không ghi được {CFG.perf_log}: {e}")


PERF_LOG = PerfLog()


def percentile(xs: List[float], q: float) -> Optional[float]:
    """Nội suy tuyến tính (giống numpy mặc định). n nhỏ thì P95 ≈ max — luôn in kèm n."""
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    f = int(math.floor(k))
    c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def summarize_perf(records: List[Dict[str, Any]], by: str = "variant") -> Dict[str, Any]:
    """Gộp các bản ghi PERF theo `by`: median / P95 / n cho từng chỉ số + tỷ lệ thành công."""
    groups = collections.OrderedDict()
    for r in records:
        key = r.get(by)
        if key is None:                                     # cho phép gom theo cờ trong `flags` (stage_mode, wait_mode, concurrency...)
            key = (r.get("flags") or {}).get(by)
        groups.setdefault(str(key if key is not None else "?"), []).append(r)
    out = collections.OrderedDict()
    for g, rows in groups.items():
        n = len(rows)
        oc = collections.Counter(str(r.get("outcome")) for r in rows)
        ok = oc.get("CONFIRMED", 0) + oc.get("DRY_RUN", 0)      # DRY_RUN = chạy thử tới lúc nút Post sáng (chỉ từ --bench-upload)
        stat = collections.OrderedDict()
        for name, _a, _b, grp in INTERVALS:
            vals = [r["ms"].get(name) for r in rows if r.get("ms") and r["ms"].get(name) is not None]
            stat[name] = {
                "group": grp, "n": len(vals),
                "median_ms": None if not vals else round(statistics.median(vals), 1),
                "p95_ms": None if not vals else round(percentile(vals, 0.95), 1),
            }
        extras = collections.OrderedDict()
        for r in rows:
            for k, v in (r.get("flags") or {}).items():
                if k.startswith("x_") and isinstance(v, (int, float)) and not isinstance(v, bool):
                    extras.setdefault(k, []).append(float(v))
        ex = collections.OrderedDict((k, {"n": len(v), "median": round(statistics.median(v), 2), "max": round(max(v), 2)}) for k, v in extras.items())
        out[g] = {"jobs": n, "confirmed": ok, "success_rate": round(ok / n, 4) if n else None, "outcomes": dict(oc), "metrics": stat, "extras": ex}
    return out


def load_perf_records(path: str) -> List[Dict[str, Any]]:
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def format_perf_summary(summary: Dict[str, Any]) -> str:
    lines = []
    for g, s in summary.items():
        sr = "n/a" if s["success_rate"] is None else f"{s['success_rate'] * 100:.1f}%"
        oc = ", ".join(f"{k}={v}" for k, v in sorted((s.get("outcomes") or {}).items()))
        lines.append(f"\n=== {g} — {s['jobs']} việc, thành công {s['confirmed']} ({sr}) | kết quả: {oc} ===")
        lines.append(f"{'chỉ số':18} {'nhóm':16} {'n':>4} {'median(ms)':>12} {'P95(ms)':>12}")
        for name, m in s["metrics"].items():
            med = "UNKNOWN" if m["median_ms"] is None else f"{m['median_ms']:.1f}"
            p95 = "UNKNOWN" if m["p95_ms"] is None else f"{m['p95_ms']:.1f}"
            lines.append(f"{name:18} {m['group']:16} {m['n']:>4} {med:>12} {p95:>12}")
        for k, e in (s.get("extras") or {}).items():
            lines.append(f"  {k:24} n={e['n']:<3} median={e['median']}  max={e['max']}")
    return "\n".join(lines)


# ======================================================================================
# PHẦN 3 — MÁY TRẠNG THÁI TỪNG PROFILE + KẾT QUẢ ĐĂNG
#   IDLE → READY → ATTACHING → UPLOADING → PROCESSING → POST_READY → POSTING → CONFIRMED → CLEANUP → READY
#   Lỗi/không xác minh được => ERROR / UNKNOWN (không tự đăng lại khi có nguy cơ đăng trùng).
# ======================================================================================
class PState(enum.Enum):
    IDLE = "IDLE"
    READY = "READY"
    ATTACHING = "ATTACHING"
    UPLOADING = "UPLOADING"
    PROCESSING = "PROCESSING"
    POST_READY = "POST_READY"
    POSTING = "POSTING"
    CONFIRMED = "CONFIRMED"
    CLEANUP = "CLEANUP"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


_S = PState
_ALLOWED = {
    _S.IDLE: {_S.READY, _S.ERROR},
    _S.READY: {_S.ATTACHING, _S.IDLE, _S.ERROR, _S.UNKNOWN},
    _S.ATTACHING: {_S.UPLOADING, _S.PROCESSING, _S.POST_READY, _S.ERROR, _S.UNKNOWN},
    _S.UPLOADING: {_S.PROCESSING, _S.POST_READY, _S.ERROR, _S.UNKNOWN},
    _S.PROCESSING: {_S.POST_READY, _S.ERROR, _S.UNKNOWN},
    _S.POST_READY: {_S.POSTING, _S.ERROR, _S.UNKNOWN},
    _S.POSTING: {_S.CONFIRMED, _S.UNKNOWN, _S.ERROR},
    _S.CONFIRMED: {_S.CLEANUP, _S.READY, _S.IDLE, _S.ERROR},
    _S.CLEANUP: {_S.READY, _S.IDLE, _S.ERROR},
    _S.ERROR: {_S.READY, _S.IDLE},                 # phục hồi chỉ khi trang đã nạp lại sạch (hoặc đóng profile)
    _S.UNKNOWN: {_S.READY, _S.IDLE},               # chỉ sau khi NGƯỜI xác nhận (reset_unknown) hoặc đóng profile
}
STATE_STRICT = _env_flag("TAM_FSM_STRICT", False)       # kiểm thử: chuyển trạng thái sai => ném lỗi


class IllegalTransition(RuntimeError):
    pass


class ProfileFSM:
    def __init__(self, pid: str):
        self.pid = str(pid)
        self.state = _S.IDLE
        self.since_ns = time.perf_counter_ns()
        self.history = collections.deque(maxlen=80)     # (perf_ns, from, to, why)
        self.violations = 0
        self.dirty = False
        self._lock = threading.RLock()

    def to(self, new: "PState", why: str = "") -> bool:
        with self._lock:
            old = self.state
            if new == old:
                return True
            ok = new in _ALLOWED.get(old, set())
            if not ok:
                self.violations += 1
                msg = f"[FSM] {self.pid}: chuyển trạng thái lạ {old.value}→{new.value} ({why})"
                if STATE_STRICT:
                    raise IllegalTransition(msg)
                print("⚠️ " + msg)
            if new == _S.ATTACHING:
                self.dirty = True            # đã gắn file => trang không còn "nguyên sơ" cho tới khi nạp lại và xác minh sạch
            self.state = new
            self.since_ns = time.perf_counter_ns()
            self.history.append((self.since_ns, old.value, new.value, why))
        _notify_state(self.pid, old, new, why)
        return ok

    def reset_unknown(self, why: str = "người dùng đã kiểm tra tay"):
        """Chỉ gọi khi NGƯỜI đã xác nhận trạng thái thật trên TikTok."""
        with self._lock:
            if self.state in (_S.UNKNOWN, _S.ERROR):
                self.to(_S.READY, why)
                self.dirty = True            # trang chưa được xác nhận sạch => vẫn nạp lại ở việc kế tiếp

    def mark_page_clean(self):
        with self._lock:
            self.dirty = False

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"pid": self.pid, "state": self.state.value, "dirty": self.dirty, "violations": self.violations,
                    "age_s": round((time.perf_counter_ns() - self.since_ns) / 1e9, 3)}


_FSMS = {}                      # type: Dict[str, ProfileFSM]
_fsm_lock = threading.Lock()
_STATE_LISTENERS = []           # type: List[Callable[[str, PState, PState, str], None]]


def get_fsm(pid) -> ProfileFSM:
    pid = str(pid)
    with _fsm_lock:
        f = _FSMS.get(pid)
        if f is None:
            f = _FSMS[pid] = ProfileFSM(pid)
        return f


def drop_fsm(pid):
    with _fsm_lock:
        _FSMS.pop(str(pid), None)


def profile_states() -> Dict[str, str]:
    with _fsm_lock:
        return {p: f.state.value for p, f in _FSMS.items()}


def add_state_listener(cb: Callable[[str, "PState", "PState", str], None]):
    """GUI đăng ký để hiện trạng thái từng profile. cb có thể được gọi từ thread worker (dùng Qt signal để chuyển luồng)."""
    if cb not in _STATE_LISTENERS:
        _STATE_LISTENERS.append(cb)


def remove_state_listener(cb):
    try:
        _STATE_LISTENERS.remove(cb)
    except ValueError:
        pass


def _notify_state(pid, old, new, why):
    for cb in list(_STATE_LISTENERS):
        try:
            cb(pid, old, new, why)
        except Exception:
            pass


class PostStatus(enum.Enum):
    CONFIRMED = "CONFIRMED"            # có bằng chứng GIAO DIỆN là video đã đăng
    FAILED_SAFE = "FAILED_SAFE"        # lỗi TRƯỚC khi bấm Post => chắc chắn chưa đăng, retry được
    FAILED_VISIBLE = "FAILED_VISIBLE"  # đã bấm, TikTok hiện lỗi rõ ràng. KHÔNG tự retry (nguy cơ trùng)
    UNKNOWN = "UNKNOWN"                # đã bấm mà không xác minh được => khóa folder, người kiểm tra
    KILLED = "KILLED"                  # Hard Stop trước khi bấm
    DRY_RUN = "DRY_RUN"                # chỉ CLI --bench-upload: đo đến lúc nút Post sáng rồi DỪNG, KHÔNG bấm (không đăng gì)


class UploadResult:
    __slots__ = ("status", "reason", "clicked", "evidence", "trace", "paths")

    def __init__(self, status: "PostStatus", reason: str = "", clicked: bool = False, evidence: str = "",
                 trace: Optional[PerfTrace] = None, paths: Optional[List[str]] = None):
        self.status = status
        self.reason = reason
        self.clicked = clicked
        self.evidence = evidence
        self.trace = trace
        self.paths = list(paths or [])

    @property
    def confirmed(self) -> bool:
        return self.status == PostStatus.CONFIRMED

    @property
    def safe_to_retry(self) -> bool:
        """Chỉ khi chắc chắn CHƯA bấm Post (kể cả bị ngắt). Mọi trường hợp khác có thể đã đăng => không tự đăng lại."""
        return self.status in (PostStatus.FAILED_SAFE,) and not self.clicked

    def __repr__(self):
        return f"UploadResult({self.status.value}, clicked={self.clicked}, reason={self.reason!r}, evidence={self.evidence!r})"


# ======================================================================================
# PHẦN 4 — FILE SẴN SÀNG, NHẬT KÝ CHỐNG ĐĂNG TRÙNG, STAGE, MOVE
# ======================================================================================
_INTERMEDIATE_SUFFIXES = (".part", ".tmp", ".temp", ".crdownload", ".download", ".ytdl", ".partial",
                          ".opdownload", ".aria2", ".!ut", ".filepart")
# yt-dlp: luồng tách "Tên.f137.mp4" (chỉ hình/tiếng) và đầu ra ghép "Tên.temp.mp4" trước khi đổi tên thành file cuối
_YTDLP_STREAM_RE = re.compile(r"\.f(?:\d{1,5}(?:-\d{1,3})?|(?:hls|dash|http)-[0-9A-Za-z_\-=+]{1,24})\.(mp4|m4a|webm|mkv|mov|avi|mp3|opus|aac|ogg)$", re.I)
_TEMP_MID_RE = re.compile(r"\.(temp|tmp|part)\.[A-Za-z0-9]{2,5}$", re.I)


def is_video_name(name: str) -> bool:
    return name.lower().endswith(VIDEO_EXTS)


def is_intermediate_name(name: str) -> bool:
    """True nếu tên cho thấy file đang tải/ghép dở. Chỉ áp dụng khi TAM_IGNORE_INTERMEDIATE=1 (mặc định)."""
    n = name.lower()
    if n.startswith("._") or n.startswith("~$"):
        return True
    if n.endswith(_INTERMEDIATE_SUFFIXES):
        return True
    if _YTDLP_STREAM_RE.search(name) or _TEMP_MID_RE.search(name):
        return True
    return False


def stat_file(path: str):
    """(size, mtime_ns) hoặc None. Dùng os.stat (mở handle trên Windows) chứ KHÔNG dùng DirEntry.stat
    vì thông tin thư mục trên Windows có thể cũ khi file đang được ghi."""
    try:
        st = os.stat(path)
        return st.st_size, st.st_mtime_ns
    except OSError:
        return None


def _locked_for_write(path: str) -> bool:
    """CHỈ Windows, best-effort: thử mở để ghi; bị từ chối (sharing violation) => tiến trình khác đang giữ file.
    Không phát hiện được writer mở với FILE_SHARE_WRITE (ffmpeg/Python mặc định) nên KHÔNG thay cho kiểm tra ổn định."""
    if os.name != "nt":
        return False
    try:
        if not os.access(path, os.W_OK):       # thuộc tính read-only => thử mở ghi vô nghĩa
            return False
        fd = os.open(path, os.O_RDWR)
        os.close(fd)
        return False
    except PermissionError:
        return True
    except Exception:
        return False


class FileGate:
    """Cổng "file đã ghi xong": không bao giờ cho đăng file đang được ghi.

    Một file được coi là SẴN SÀNG khi: không phải file trung gian (.part/.tmp/yt-dlp .fNNN), size>0, và
    (size, mtime_ns) không đổi ít nhất `stable_ms` — tính từ lần mtime cuối HOẶC từ lần ta thấy nó không đổi.
    File xong từ lâu => sẵn sàng NGAY ở lần quét đầu (không còn sleep cố định 0,15 s/thư mục như bản cũ).
    Cho biết còn bao lâu nữa nên kiểm tra lại (next_in_s) để Scanner thức dậy đúng lúc thay vì đoán.
    """

    def __init__(self, stable_ms: Optional[int] = None):
        self.stable_ms = CFG.stable_ms if stable_ms is None else stable_ms
        self._seen = {}                 # type: Dict[str, Dict[str, Any]]
        self._warned = {}               # type: Dict[str, float]

    def forget_missing(self, keep_paths):
        keep = set(keep_paths)
        for p in [p for p in self._seen if p not in keep]:
            self._seen.pop(p, None)
            self._warned.pop(p, None)

    def check(self, path: str) -> Tuple[bool, float, str]:
        """(ready, next_in_s, reason). next_in_s: sau chừng này giây nên kiểm tra lại (0 nếu đã sẵn sàng)."""
        now_ns = time.perf_counter_ns()
        st = stat_file(path)
        if st is None:
            self._seen.pop(path, None)
            return False, 0.5, "không đọc được (đang bị xoá/đổi tên?)"
        size, mt = st
        rec = self._seen.get(path)
        if rec is None or rec["size"] != size or rec["mtime"] != mt:
            rec = {"size": size, "mtime": mt, "same_since": now_ns, "first_seen": now_ns}
            if path in self._seen:
                rec["first_seen"] = self._seen[path]["first_seen"]
            self._seen[path] = rec
        if size <= 0:
            return False, max(0.1, self.stable_ms / 1000.0), "size=0"
        stable = self.stable_ms
        age_ms = (time.time_ns() - mt) / 1e6 if mt else 0.0
        same_ms = (now_ns - rec["same_since"]) / 1e6
        # file đã "im" tối thiểu stable_ms: hoặc mtime đã cũ hơn ngưỡng, hoặc ta quan sát thấy nó không đổi đủ lâu
        quiet_ms = max(age_ms, same_ms) if age_ms >= 0 else same_ms
        if age_ms < 0:                                  # mtime ở tương lai (lệch đồng hồ/ổ mạng) => chỉ tin quan sát của ta
            quiet_ms = same_ms
        if quiet_ms < stable:
            return False, max(0.01, (stable - quiet_ms) / 1000.0), f"đang thay đổi (im {quiet_ms:.0f}/{stable} ms)"
        if _locked_for_write(path):
            first = self._warned.setdefault(path, time.time())
            held = time.time() - first
            if held < CFG.lock_grace_s:
                return False, 0.5, "đang bị tiến trình khác giữ khoá ghi"
        return True, 0.0, "ok"

    def first_seen_ns(self, path: str) -> Optional[int]:
        r = self._seen.get(path)
        return None if r is None else r["first_seen"]


class FolderView:
    """Kết quả đánh giá một thư mục con."""
    __slots__ = ("path", "videos", "ready", "pending", "skipped", "next_in_s", "marker_ok", "marker_mtime_ns", "reasons")

    def __init__(self, path: str):
        self.path = path
        self.videos = []                # type: List[str]  (đã sắp xếp, không gồm file trung gian)
        self.ready = []                 # type: List[str]
        self.pending = []               # type: List[str]
        self.skipped = []               # type: List[str]  (file trung gian bị bỏ qua)
        self.next_in_s = None           # type: Optional[float]
        self.marker_ok = True
        self.marker_mtime_ns = None     # type: Optional[int]
        self.reasons = {}               # type: Dict[str, str]

    @property
    def all_ready(self) -> bool:
        return bool(self.videos) and not self.pending and self.marker_ok


def evaluate_folder(sub: str, gate: FileGate) -> FolderView:
    """Liệt kê MỘT lần, không sleep. Trả về video sẵn sàng/đang chờ. Thay cho cặp listdir cách nhau DOUBLESCAN_WAIT."""
    fv = FolderView(sub)
    try:
        names = os.listdir(sub)
    except OSError:
        return fv
    vids = []
    for n in names:
        if not is_video_name(n):
            continue
        if CFG.ignore_intermediate and is_intermediate_name(n):
            fv.skipped.append(os.path.join(sub, n))
            continue
        vids.append(os.path.join(sub, n))
    vids = list(dict.fromkeys(vids))
    vids.sort()
    fv.videos = vids
    gate.forget_missing(vids)
    if CFG.require_marker:
        mp = os.path.join(sub, CFG.require_marker)
        mst = stat_file(mp)
        fv.marker_ok = mst is not None
        fv.marker_mtime_ns = None if mst is None else mst[1]
        if not fv.marker_ok:
            fv.next_in_s = SCAN_INTERVAL
            return fv
    nxt = None
    for p in vids:
        ok, wait_s, why = gate.check(p)
        if ok:
            fv.ready.append(p)
        else:
            fv.pending.append(p)
            fv.reasons[p] = why
            nxt = wait_s if nxt is None else min(nxt, wait_s)
    fv.next_in_s = nxt
    return fv


# --------------------------------------------------------------------------------------
# Nhật ký bền vững chống đăng trùng (JSONL, append-only)
# --------------------------------------------------------------------------------------
class PostJournal:
    """Mỗi sự kiện một dòng JSON.  POSTING ghi TRƯỚC khi bấm Post (flush, không fsync để không tốn thời gian ở đường găng),
    CONFIRMED/UNKNOWN/FAILED_VISIBLE/MOVED ghi SAU (có fsync). Sau khi crash/MOVE lỗi, file đã CONFIRMED không bị đăng lại,
    còn POSTING/UNKNOWN (đã bấm nhưng chưa rõ) bị KHÓA cho tới khi người xác nhận."""

    BLOCKING = ("POSTING", "UNKNOWN", "FAILED_VISIBLE")

    def __init__(self, path: Optional[str] = None):
        self.path = path or CFG.journal_path
        self._lock = threading.Lock()
        self._st = {}                   # type: Dict[str, Dict[str, Any]]
        self._fh = None
        self._loaded = False
        self._warned = False

    @staticmethod
    def file_key(path: str) -> Optional[str]:
        st = stat_file(path)
        if st is None:
            return None
        raw = f"{os.path.normcase(os.path.abspath(path))}|{st[0]}|{st[1]}"
        return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:20]

    def load(self):
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                        except Exception:
                            continue
                        ev, keys = r.get("ev"), r.get("keys") or []
                        for k in keys:
                            if ev in ("POSTING", "CONFIRMED", "UNKNOWN", "FAILED_VISIBLE", "NOT_POSTED"):
                                self._st[k] = {"status": ev, "path": r.get("paths", {}).get(k, ""), "pid": r.get("pid", ""), "t": r.get("t", "")}
            except FileNotFoundError:
                pass
            except Exception as e:
                print(f"⚠️ [JOURNAL] Không đọc được {self.path}: {e}")

    def status(self, key: Optional[str]) -> Optional[str]:
        if not key:
            return None
        self.load()
        with self._lock:
            r = self._st.get(key)
            return None if r is None else r["status"]

    def blocked_entries(self) -> List[Dict[str, Any]]:
        self.load()
        with self._lock:
            return [dict(key=k, **v) for k, v in self._st.items() if v["status"] in self.BLOCKING]

    def record(self, ev: str, items: List[Tuple[str, str]], pid: str = "", durable: bool = False, extra: Optional[Dict[str, Any]] = None):
        """items: [(key, path)]. Không bao giờ ném lỗi (ghi hỏng không được làm hỏng việc đăng), nhưng trả cảnh báo 1 lần."""
        self.load()
        keys = [k for k, _ in items if k]
        rec = {"ev": ev, "t": datetime.datetime.now().isoformat(timespec="milliseconds"), "pid": str(pid),
               "keys": keys, "paths": {k: p for k, p in items if k}}
        if extra:
            rec.update(extra)
        try:
            line = json.dumps(rec, ensure_ascii=False)
            with self._lock:
                if ev in ("POSTING", "CONFIRMED", "UNKNOWN", "FAILED_VISIBLE", "NOT_POSTED"):
                    for k in keys:
                        self._st[k] = {"status": ev, "path": rec["paths"].get(k, ""), "pid": rec["pid"], "t": rec["t"]}
                if self._fh is None:
                    self._fh = open(self.path, "a", encoding="utf-8")
                self._fh.write(line + "\n")
                self._fh.flush()
                if durable:
                    try:
                        os.fsync(self._fh.fileno())
                    except Exception:
                        pass
            return True
        except Exception as e:
            if not self._warned:
                self._warned = True
                print(f"⚠️ [JOURNAL] Không ghi được {self.path}: {e}. Chống đăng trùng chỉ còn trong bộ nhớ phiên này.")
            return False

    def close(self):
        with self._lock:
            try:
                if self._fh:
                    self._fh.close()
            finally:
                self._fh = None


JOURNAL = PostJournal()


# --------------------------------------------------------------------------------------
# Stage file: mặc định ĐỌC THẲNG từ nguồn; RAM disk chỉ khi TAM_STAGE=ram (để A/B bằng số đo thật)
# --------------------------------------------------------------------------------------
def stage_files_for_upload(file_paths, prefix="tk_up", info: Optional[Dict[str, Any]] = None, check_kill: Optional[Callable[[], bool]] = None):
    """Hành vi gốc (R:\\Temp: hardlink -> copy2 -> dùng thẳng nguồn). Thêm: ghi `info` (phương pháp/số byte) để đo."""
    if info is None:
        info = {}
    info.update({"method": "none", "bytes_copied": 0, "linked": 0, "copied": 0, "fallback": 0})
    if not file_paths:
        return [], None
    ram_root = _env_str("TAM_RAM_DIR", "R:\\")
    ram_disk_path = os.path.join(ram_root, "Temp")
    if not os.path.exists(ram_root):
        info["method"] = "direct(no-ram-drive)"
        return list(file_paths), None
    try:
        os.makedirs(ram_disk_path, exist_ok=True)
        temp_dir = os.path.join(ram_disk_path, f"{prefix}_{int(time.time() * 1000)}")
        os.makedirs(temp_dir, exist_ok=True)
        staged = []
        for src in file_paths:
            if check_kill and check_kill():
                break
            base = os.path.basename(src)
            dst = os.path.join(temp_dir, base)
            try:
                os.link(src, dst)
                info["linked"] += 1
            except Exception:
                try:
                    shutil.copy2(src, dst)
                    info["copied"] += 1
                    try:
                        info["bytes_copied"] += os.path.getsize(dst)
                    except Exception:
                        pass
                except Exception:
                    dst = src
                    info["fallback"] += 1
            staged.append(dst)
        info["method"] = "ram"
        return staged, temp_dir
    except Exception:
        info["method"] = "direct(stage-error)"
        return list(file_paths), None


def stage_files(file_paths, prefix: str, trace: Optional[PerfTrace] = None, check_kill: Optional[Callable[[], bool]] = None):
    """Cổng stage theo CFG.stage. Luôn đánh dấu STAGE_START/END (direct => ~0 ms, đo thật chứ không bịa)."""
    info = {}       # type: Dict[str, Any]
    if trace:
        trace.mark("STAGE_START")
    if CFG.stage == "ram":
        staged, tmp = stage_files_for_upload(file_paths, prefix=prefix, info=info, check_kill=check_kill)
    else:
        staged, tmp = list(file_paths), None
        info["method"] = "direct"
    if trace:
        trace.mark("STAGE_END")
        trace.set("stage", info)
    return staged, tmp, info


def cleanup_stage_dir(temp_dir: Optional[str]):
    if temp_dir and os.path.exists(temp_dir):
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


# --------------------------------------------------------------------------------------
# MOVE folder sang DONE (có thử lại có giới hạn khi file còn bị giữ handle — thường là Chrome còn giữ file vừa upload)
# --------------------------------------------------------------------------------------
def _looks_like_lock(e: BaseException) -> bool:
    if isinstance(e, shutil.Error):
        return False                     # đích đã tồn tại... => KHÔNG phải khoá, không thử lại
    if isinstance(e, PermissionError):
        return True
    w = getattr(e, "winerror", None)
    if w in (5, 32, 33):
        return True
    return getattr(e, "errno", None) in (13, 16, 26)


def move_folder_safely(src: str, dst: str, check_kill: Optional[Callable[[], bool]] = None, on_locked: Optional[Callable[[], None]] = None,
                       max_wait_s: float = 8.0, log: Optional[Callable[[str], None]] = None) -> bool:
    """shutil.move giống bản gốc. Nếu lỗi do khoá/handle: gọi on_locked() một lần (vd. điều hướng Chrome rời trang để nhả file),
    thử lại tối đa max_wait_s giây (lùi dần 0,25→1 s). Lỗi khác (đích đã tồn tại...) => False ngay."""
    t0 = time.perf_counter()
    attempt = 0
    while True:
        try:
            shutil.move(src, dst)
            if attempt and log:
                log(f"   (MOVE thành công sau {attempt} lần thử lại, {time.perf_counter() - t0:.2f}s)")
            return True
        except Exception as e:
            if not _looks_like_lock(e) or (time.perf_counter() - t0) >= max_wait_s or (check_kill and check_kill()):
                if log:
                    log(f"   (MOVE lỗi: {type(e).__name__}: {e})")
                return False
            if attempt == 0 and on_locked is not None:
                try:
                    on_locked()
                except Exception:
                    pass
            time.sleep(min(0.25 * (2 ** attempt), 1.0))
            attempt += 1


# ======================================================================================
# PHẦN 5 — MÃ JAVASCRIPT PHÍA TRÌNH DUYỆT (theo SỰ KIỆN, không polling)
#
#  Thiết kế (khác bản gốc):
#   * KHÔNG quan sát attributes trên toàn DOM. Chỉ: (1) childList toàn trang để biết nút xuất hiện/bị thay;
#     (2) attributes CHỈ trên nút Post và chuỗi tổ tiên của nó (disabled/aria-disabled/class/style/hidden).
#   * "Cổng rẻ" (disabled/aria-disabled/class) chạy trước; getComputedStyle/getBoundingClientRect/scrollIntoView
#     chỉ chạy khi cổng rẻ đã qua — bản gốc gọi chúng ở MỌI đợt mutation của MỌI phần tử.
#   * Promise `waitReady` được giải ngay trong callback của observer (microtask): độ trễ phát hiện ~0,
#     phần còn lại chỉ là 1 vòng WebDriver để trả kết quả về Python (không còn 20 ms polling + RTT).
#   * Mốc thời gian dùng performance.now() của chính tài liệu (đồng hồ Chrome) => ReadyToClick tính CÙNG ĐỒNG HỒ.
#   * "Bảo vệ lúc nhấn": từ lúc nút sẵn sàng đến lúc nhấn xong, sự kiện chuột *tin cậy* (isTrusted) rơi ngoài nút
#     bị chặn (không ai bấm nhầm hộp thoại/overlay vừa hiện) và nút tắt lại đúng lúc nhấn thì không nhận.
#   * Bằng chứng đăng: URL đổi (khác URL trước khi bấm, khớp regex, không còn là trang upload) HOẶC văn bản MỚI xuất hiện
#     sau khi bấm khớp regex thành công. Văn bản lỗi => FAILED_VISIBLE. Hộp thoại chặn => ghi nhận, KHÔNG tự bấm.
# ======================================================================================
JS_INSTALL = r"""
const cfg = arguments[0] || {};
const NS = '__tam5';
try { const old = window[NS]; if (old && old.dispose) old.dispose(); } catch (e) {}
try { if (!window.__tamDoc) window.__tamDoc = Math.random().toString(36).slice(2) + Date.now().toString(36); } catch (e) {}

const S = {
  v: 5, doc: window.__tamDoc || null,
  btn: null, candAt: null, readyAt: null, readyInfo: null,
  attachedAt: null, attachFlag: false,
  waiters: [], cwaiters: [],
  armed: false, frozen: false, watchText: false, clickAt: null,
  ev: [], blocked: [],
  stats: {root: 0, btn: 0, text: 0, checks: 0, full: 0, cover: 0},
  upDoneAt: null, preUrl: String(location.href), urlOther: null,
  verdict: null, dialog: null, newText: [], reErr: null, disposed: false
};
const mk = function (p, f) {
  if (!p) return null;
  try { return new RegExp(p, f || 'i'); } catch (e) { S.reErr = (S.reErr || '') + ' [' + p + ' => ' + e + ']'; return null; }
};
const esc = function (s) { return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); };
const reOk = mk(cfg.confirmText), reFail = mk(cfg.failText), reUrl = mk(cfg.confirmUrl), reUpDone = mk(cfg.upDoneText);
const reUpPath = cfg.uploadPath ? mk(esc(cfg.uploadPath)) : null;
const DLG = '[role="dialog"],[role="alertdialog"],[aria-modal="true"]';

function gate(b) {
  if (!b) return false;
  if (b.disabled) return false;
  if (String(b.getAttribute('aria-disabled') || '').toLowerCase() === 'true') return false;
  const c = b.className;
  if (typeof c === 'string' && c.toLowerCase().indexOf('disabled') >= 0) return false;
  return true;
}
function full(b) {
  const cs = getComputedStyle(b);
  if (cs.display === 'none' || cs.visibility === 'hidden' || cs.pointerEvents === 'none') return false;
  const r = b.getBoundingClientRect();
  return r.width > 1 && r.height > 1;
}
function compute() {
  S.stats.checks++;
  let b = S.btn;
  if (!b || !b.isConnected) {
    b = document.querySelector(cfg.sel);
    if (b !== S.btn) { S.btn = b; watchBtn(b); }
  }
  if (!b || !gate(b)) return null;
  S.stats.full++;
  if (!full(b)) return null;
  let r = b.getBoundingClientRect();
  const vw = window.innerWidth || document.documentElement.clientWidth;
  const vh = window.innerHeight || document.documentElement.clientHeight;
  let x = r.left + r.width / 2, y = r.top + r.height / 2;
  if (x < 0 || y < 0 || x >= vw || y >= vh) {
    try { b.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'}); } catch (e) {}
    r = b.getBoundingClientRect(); x = r.left + r.width / 2; y = r.top + r.height / 2;
  }
  const top = document.elementFromPoint(x, y);
  if (top && !(top === b || b.contains(top))) return {covered: true};
  return {x: x, y: y};
}
function markUpDone() { if (S.upDoneAt === null) S.upDoneAt = performance.now(); }
function readyPayload() {
  if (S.upDoneAt === null && cfg.upDoneSel) { try { if (document.querySelector(cfg.upDoneSel)) markUpDone(); } catch (e) {} }
  return {ready: true, x: S.readyInfo.x, y: S.readyInfo.y, readyAt: S.readyAt, now: performance.now(), doc: S.doc,
          vis: document.visibilityState, upDoneAt: S.upDoneAt, attachedAt: S.attachedAt, stats: S.stats, reErr: S.reErr};
}
function flush() {
  if (!S.waiters.length) return;
  const w = S.waiters.splice(0);
  for (let i = 0; i < w.length; i++) { clearTimeout(w[i].timer); w[i].resolve(readyPayload()); }
}
let coverT = null;
function coverStart() {
  if (coverT === null && !S.disposed) coverT = setTimeout(function () { coverT = null; S.stats.cover++; evaluate(); }, 8);
}
function coverStop() { if (coverT !== null) { clearTimeout(coverT); coverT = null; } }
function upOk() {
  if (!(cfg.upDoneSel || reUpDone)) return true;             // không có tín hiệu "upload xong" => dựa vào việc TikTok tự bật nút
  if (S.upDoneAt === null && cfg.upDoneSel) { try { if (document.querySelector(cfg.upDoneSel)) markUpDone(); } catch (e) {} }
  return S.upDoneAt !== null;
}
function evaluate() {
  if (S.disposed || S.frozen) return;
  const attached = S.attachedAt !== null || S.attachFlag;
  const c = compute();
  if (c && !c.covered) {
    if (S.candAt === null) S.candAt = performance.now();
    if (!attached) return;                                   // nút sáng sẵn từ trước khi gắn file: bỏ qua
    if (!upOk()) return;                                     // đã cấu hình tín hiệu upload xong mà chưa thấy: KHÔNG coi là sẵn sàng
    if (S.readyInfo === null) {
      S.readyAt = (S.attachedAt !== null && S.attachedAt > S.candAt) ? S.attachedAt : S.candAt;
      S.armed = true;
      startEvidence();
    }
    S.readyInfo = {x: c.x, y: c.y};
    flush();
    coverStop();
  } else {
    S.candAt = null; S.readyAt = null;
    // `armed` GIỮ NGUYÊN khi nút chớp tắt: bảo vệ lúc nhấn phải còn hiệu lực cho tới khi lần nhấn kết thúc (hoặc rearm/dispose).
    // (Bản đầu tắt `armed` ở đây => nhấn xuống lọt qua không ghi nhận, rồi click sau đó bị coi là "chưa tới" => nhấn lần 2 => ĐĂNG ĐÔI.)
    if (S.readyInfo !== null) { S.readyInfo = null; }
    if (c && c.covered) coverStart(); else coverStop();
  }
}

let btnObs = null;
function watchBtn(b) {
  if (btnObs) { try { btnObs.disconnect(); } catch (e) {} btnObs = null; }
  if (!b) return;
  btnObs = new MutationObserver(function (m) { S.stats.btn += m.length; evaluate(); });
  const opt = {attributes: true, attributeFilter: ['disabled', 'aria-disabled', 'class', 'style', 'hidden']};
  let n = b, depth = 0;
  while (n && n.nodeType === 1 && depth < 40) { btnObs.observe(n, opt); n = n.parentElement; depth++; }
}

function setVerdict(v) {
  if (S.verdict) return;
  v.at = performance.now(); v.doc = S.doc; S.verdict = v;
  if (!S.cwaiters.length) return;
  const w = S.cwaiters.splice(0);
  for (let i = 0; i < w.length; i++) { clearTimeout(w[i].timer); w[i].resolve(confirmPayload(false)); }
}
function confirmPayload(pending) {
  return {pending: pending, verdict: S.verdict, ev: S.ev.slice(), blocked: S.blocked.slice(), dialog: S.dialog,
          newText: S.newText.slice(-6), url: String(location.href), urlOther: S.urlOther, now: performance.now(), doc: S.doc,
          clickAt: S.clickAt, vis: document.visibilityState, stats: S.stats};
}
function checkUrl() {
  if (S.verdict || !S.watchText) return;
  const u = String(location.href);
  if (u === S.preUrl) return;
  if (reUpPath && reUpPath.test(location.pathname)) return;      // vẫn là trang upload => KHÔNG tính (lỗi false-positive của bản gốc)
  if (reUrl && reUrl.test(u)) setVerdict({kind: 'url', url: u}); else S.urlOther = u;
}
function scanText(t) {
  if (!t || S.verdict) return;
  t = String(t).slice(0, 4000);
  S.newText.push(t.slice(0, 160)); if (S.newText.length > 12) S.newText.shift();
  if (reFail && reFail.test(t)) setVerdict({kind: 'fail-text', text: t.slice(0, 160)});
  else if (reOk && reOk.test(t)) setVerdict({kind: 'ok-text', text: t.slice(0, 160)});
}
function scanNode(n) {
  let t = '';
  try { t = n.textContent || ''; } catch (e) {}
  if (t) scanText(t);
  try {
    const d = (n.matches && n.matches(DLG)) ? n : (n.querySelector ? n.querySelector(DLG) : null);
    if (d && !S.dialog) S.dialog = {text: String(d.textContent || '').trim().slice(0, 200), at: performance.now()};
  } catch (e) {}
}
function dlgVisible(d) {
  try {
    if (d.hasAttribute && d.hasAttribute('hidden')) return false;
    const cs = getComputedStyle(d);
    return cs.display !== 'none' && cs.visibility !== 'hidden';
  } catch (e) { return false; }
}
function textScan(m) {
  S.stats.text += m.length;
  for (let i = 0; i < m.length && !S.verdict; i++) {
    const rec = m[i];
    if (rec.type === 'attributes') {
      // chỉ quan tâm hộp thoại/modal vừa được HIỆN ra (không tốn layout cho các phần tử khác)
      const t = rec.target;
      try {
        if (!S.dialog && t.matches && t.matches(DLG) && dlgVisible(t)) {
          S.dialog = {text: String(t.textContent || '').trim().slice(0, 200), at: performance.now()};
        }
      } catch (e) {}
      continue;
    }
    if (rec.type === 'childList') {
      for (let j = 0; j < rec.addedNodes.length; j++) {
        const n = rec.addedNodes[j];
        if (n.nodeType === 1) scanNode(n); else if (n.nodeType === 3) scanText(n.data);
      }
    } else if (rec.type === 'characterData') { scanText(rec.target.data); }
  }
  checkUrl();
}

const rootObs = new MutationObserver(function (m) {
  S.stats.root += m.length;
  if (S.disposed) return;
  if (!S.frozen && (!S.btn || !S.btn.isConnected)) evaluate();
  if (S.upDoneAt === null && (cfg.upDoneSel || reUpDone)) {
    if (cfg.upDoneSel) { try { if (document.querySelector(cfg.upDoneSel)) markUpDone(); } catch (e) {} }
    if (reUpDone && S.upDoneAt === null) {
      for (let i = 0; i < m.length && S.upDoneAt === null; i++) {
        if (m[i].type === 'characterData') { if (reUpDone.test(String(m[i].target.data || '').slice(0, 2000))) markUpDone(); continue; }
        if (m[i].type !== 'childList') continue;
        for (let j = 0; j < m[i].addedNodes.length; j++) {
          const n = m[i].addedNodes[j];
          const tx = (n.nodeType === 1) ? n.textContent : (n.nodeType === 3 ? n.data : '');
          if (reUpDone.test(String(tx || '').slice(0, 2000))) { markUpDone(); break; }
        }
      }
    }
    if (S.upDoneAt !== null) evaluate();                     // tín hiệu upload xong có thể là điều kiện cuối cùng để nút được coi là sẵn sàng
  }
  if (S.watchText) textScan(m);
});
rootObs.observe(document.documentElement, {childList: true, subtree: true, characterData: !!reUpDone});
function startEvidence() {
  try { rootObs.observe(document.documentElement, {childList: true, subtree: true, characterData: true}); } catch (e) {}
}

const EVTS = ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click', 'auxclick', 'dblclick', 'contextmenu'];
function blockEvt(rec, e) {
  if (S.blocked.length < 24) S.blocked.push(rec);
  e.stopImmediatePropagation(); e.preventDefault();
}
function onEvt(e) {
  if (!e.isTrusted || S.disposed) return;
  if (!S.armed && !S.frozen) return;
  const t = e.target, b = S.btn;
  const on = !!(b && t && (t === b || (b.contains && b.contains(t))));
  const down = (e.type === 'mousedown' || e.type === 'pointerdown');
  const rec = {type: e.type, on: on, at: performance.now(), x: e.clientX, y: e.clientY, tag: (t && t.tagName) || null};
  // Một lần nhấn đã được CHẤP NHẬN (nhấn xuống hợp lệ trúng nút): để phần còn lại của chuỗi (up/click) đi hết, chỉ ghi nhận.
  if (S.frozen) { if (S.ev.length < 24) S.ev.push(rec); return; }
  if (!on) { blockEvt(rec, e); return; }                       // trúng chỗ khác (hộp thoại/overlay) => chặn
  if (!down) { rec.orphan = true; blockEvt(rec, e); return; }  // up/click KHÔNG có nhấn xuống hợp lệ đi trước => không bao giờ cho tới trang
  if (!gate(b)) { rec.notReady = true; blockEvt(rec, e); return; }   // nút đã tắt lại đúng lúc nhấn
  S.ev.push(rec);
  S.frozen = true; S.watchText = true; S.clickAt = rec.at;
  try {
    rootObs.observe(document.documentElement, {childList: true, subtree: true, characterData: true, attributes: true,
                                               attributeFilter: ['style', 'class', 'hidden', 'open', 'aria-hidden']});
  } catch (e2) {}
}
for (let i = 0; i < EVTS.length; i++) window.addEventListener(EVTS[i], onEvt, true);

function onChange(e) {
  const t = e.target;
  if (t && t.tagName === 'INPUT' && t.type === 'file' && t.files && t.files.length) {
    if (S.attachedAt === null) S.attachedAt = performance.now();
    evaluate();
  }
}
document.addEventListener('change', onChange, true);

const hp = history.pushState, hr = history.replaceState;
const pushWrap = function () { const r = hp.apply(this, arguments); try { checkUrl(); } catch (e) {} return r; };
const replWrap = function () { const r = hr.apply(this, arguments); try { checkUrl(); } catch (e) {} return r; };
history.pushState = pushWrap; history.replaceState = replWrap;
const onUrl = function () { checkUrl(); };
window.addEventListener('popstate', onUrl); window.addEventListener('hashchange', onUrl);

S.waitReady = function (ms, attachFlag) {
  if (attachFlag) { S.attachFlag = true; evaluate(); }
  return new Promise(function (resolve) {
    if (S.readyInfo) { resolve(readyPayload()); return; }
    const w = {resolve: resolve, timer: null};
    w.timer = setTimeout(function () {
      const i = S.waiters.indexOf(w); if (i >= 0) S.waiters.splice(i, 1);
      resolve({ready: false, now: performance.now(), doc: S.doc, vis: document.visibilityState, stats: S.stats, btn: !!S.btn,
               attached: (S.attachedAt !== null || S.attachFlag), reErr: S.reErr});
    }, ms);
    S.waiters.push(w);
  });
};
// Chế độ A/B "poll" (kiểu cũ): Python hỏi trang liên tục; MỖI lần hỏi tính lại (có layout) và trả về ngay
S.pollReady = function (attachFlag) {
  if (attachFlag) S.attachFlag = true;
  evaluate();
  if (S.readyInfo) return readyPayload();
  return {ready: false, now: performance.now(), doc: S.doc, vis: document.visibilityState, stats: S.stats, btn: !!S.btn,
          attached: (S.attachedAt !== null || S.attachFlag), reErr: S.reErr};
};
S.waitConfirm = function (ms) {
  return new Promise(function (resolve) {
    const landed = S.forceLanded || S.ev.some(function (e) { return !!e.on; });
    if (S.verdict || !landed) { resolve(confirmPayload(!S.verdict)); return; }
    const w = {resolve: resolve, timer: null};
    w.timer = setTimeout(function () {
      const i = S.cwaiters.indexOf(w); if (i >= 0) S.cwaiters.splice(i, 1);
      resolve(confirmPayload(true));
    }, ms);
    S.cwaiters.push(w);
  });
};
// Dùng sau khi trang điều hướng mà chưa đủ bằng chứng: gắn watcher vào tài liệu MỚI, chỉ theo dõi (không nhấn gì)
S.watchOnly = function (preUrl) {
  S.frozen = true; S.watchText = true; S.forceLanded = true;
  if (preUrl) S.preUrl = String(preUrl);
  startEvidence(); checkUrl();
  return true;
};
S.snapshot = function () { return confirmPayload(!S.verdict); };
S.rearm = function () {
  S.ev.length = 0; S.blocked.length = 0; S.frozen = false; S.watchText = false; S.verdict = null;
  S.armed = false; S.readyInfo = null; S.readyAt = null; S.candAt = null; S.clickAt = null;
  evaluate();
  return true;
};
S.dispose = function () {
  S.disposed = true;
  try { rootObs.disconnect(); } catch (e) {}
  try { if (btnObs) btnObs.disconnect(); } catch (e) {}
  coverStop();
  for (let i = 0; i < EVTS.length; i++) window.removeEventListener(EVTS[i], onEvt, true);
  document.removeEventListener('change', onChange, true);
  window.removeEventListener('popstate', onUrl); window.removeEventListener('hashchange', onUrl);
  if (history.pushState === pushWrap) history.pushState = hp;
  if (history.replaceState === replWrap) history.replaceState = hr;
  let w = S.waiters.splice(0);
  for (let i = 0; i < w.length; i++) { clearTimeout(w[i].timer); w[i].resolve({ready: false, disposed: true}); }
  w = S.cwaiters.splice(0);
  for (let i = 0; i < w.length; i++) { clearTimeout(w[i].timer); w[i].resolve({pending: true, disposed: true}); }
  try { delete window[NS]; } catch (e) { window[NS] = null; }
};
window[NS] = S;
evaluate();
return {ok: true, doc: S.doc, now: performance.now(), vis: document.visibilityState, reErr: S.reErr, btn: !!S.btn, url: S.preUrl};
"""

# chờ nút sẵn sàng theo lát (execute_async_script; tham số: ms, attachFlag)
JS_WAIT_READY = r"""
const ms = arguments[0], attachFlag = arguments[1], done = arguments[arguments.length - 1];
const S = window.__tam5;
if (!S) { done({ready: false, err: 'not-installed'}); return; }
S.waitReady(ms, attachFlag).then(done, function (e) { done({ready: false, err: String(e)}); });
"""

# hỏi một lần, trả ngay (chỉ dùng khi TAM_WAIT_MODE=poll để so sánh với cơ chế sự kiện)
JS_POLL_READY = r"""const S = window.__tam5; if (!S) return {ready: false, err: 'not-installed'}; return S.pollReady(arguments[0]);"""

# ---------------- MODE C (lite): theo dõi NHẸ NHẤT trong lúc upload ----------------
#  * 1 MutationObserver CHỈ trên chính nút Post (disabled/aria-disabled/class) — không subtree, không tổ tiên.
#  * Không getComputedStyle / getBoundingClientRect / elementFromPoint trong lúc upload (không ép layout).
#  * Nút bị React thay node: kiểm tra isConnected mỗi TAM_LITE_TICK_MS (querySelector khi cần, không layout).
#  * Khi nút đã bật: Python mới cài watcher đầy đủ (bảo vệ lúc nhấn + bằng chứng đăng) như cũ.
JS_LITE_INSTALL = r"""
const sel = arguments[0], tickMs = arguments[1];
const NS = '__tamLite';
try { const o = window[NS]; if (o && o.dispose) o.dispose(); } catch (e) {}
try { if (!window.__tamDoc) window.__tamDoc = Math.random().toString(36).slice(2) + Date.now().toString(36); } catch (e) {}
const L = {doc: window.__tamDoc || null, btn: null, obs: null, aobs: null, attachedAt: null, readyAt: null, waiters: [],
           stats: {checks: 0, muts: 0, anc: 0, ticks: 0, requery: 0}, timer: null, disposed: false};
function enabled(b) {
  if (!b || !b.isConnected || b.disabled) return false;
  if (String(b.getAttribute('aria-disabled') || '').toLowerCase() === 'true') return false;
  const c = b.className;
  if (typeof c === 'string' && c.toLowerCase().indexOf('disabled') >= 0) return false;
  return true;
}
function hook(b) {
  if (L.obs) { try { L.obs.disconnect(); } catch (e) {} L.obs = null; }
  if (L.aobs) { try { L.aobs.disconnect(); } catch (e) {} L.aobs = null; }
  L.btn = b;
  if (!b) return;
  L.obs = new MutationObserver(function (m) { L.stats.muts += m.length; check(); });
  L.obs.observe(b, {attributes: true, attributeFilter: ['disabled', 'aria-disabled', 'class']});
  // React thay NODE nút (không đổi thuộc tính): chỉ nghe con TRỰC TIẾP của 3 tổ tiên gần nhất (không subtree, không layout)
  L.aobs = new MutationObserver(function (m) { L.stats.anc += m.length; if (!L.btn || !L.btn.isConnected) check(); });
  let n = b.parentElement, d = 0;
  while (n && d < 3) { L.aobs.observe(n, {childList: true}); n = n.parentElement; d++; }
}
function payload() {
  return {ready: L.readyAt !== null, readyAt: L.readyAt, attachedAt: L.attachedAt, now: performance.now(), doc: L.doc,
          vis: document.visibilityState, stats: L.stats};
}
function flush() {
  const w = L.waiters.splice(0);
  for (let i = 0; i < w.length; i++) { clearTimeout(w[i].t); w[i].r(payload()); }
}
function check() {
  if (L.disposed) return;
  L.stats.checks++;
  let b = L.btn;
  if (!b || !b.isConnected) { L.stats.requery++; b = document.querySelector(sel); if (b !== L.btn) hook(b); }
  if (L.attachedAt !== null && enabled(b)) { if (L.readyAt === null) L.readyAt = performance.now(); flush(); }
  else if (!enabled(b)) L.readyAt = null;
}
function tick() {
  if (L.disposed) return;
  L.stats.ticks++;
  if (!L.btn || !L.btn.isConnected) check();
  L.timer = setTimeout(tick, tickMs);
}
function onChange(e) {
  const t = e.target;
  if (t && t.tagName === 'INPUT' && t.type === 'file' && t.files && t.files.length) {
    if (L.attachedAt === null) L.attachedAt = performance.now();
    check();
  }
}
document.addEventListener('change', onChange, true);
L.wait = function (ms, attachFlag) {
  if (attachFlag && L.attachedAt === null) L.attachedAt = performance.now();
  check();
  return new Promise(function (res) {
    if (L.readyAt !== null) { res(payload()); return; }
    const x = {r: res, t: null};
    x.t = setTimeout(function () { const i = L.waiters.indexOf(x); if (i >= 0) L.waiters.splice(i, 1); res(payload()); }, ms);
    L.waiters.push(x);
  });
};
L.dispose = function () {
  L.disposed = true;
  try { if (L.obs) L.obs.disconnect(); } catch (e) {}
  try { if (L.aobs) L.aobs.disconnect(); } catch (e) {}
  clearTimeout(L.timer);
  document.removeEventListener('change', onChange, true);
  flush();
  try { delete window[NS]; } catch (e) { window[NS] = null; }
};
window[NS] = L;
hook(document.querySelector(sel));
tick();
return {ok: true, doc: L.doc, now: performance.now(), vis: document.visibilityState, url: String(location.href)};
"""

JS_LITE_WAIT = r"""
const ms = arguments[0], attachFlag = arguments[1], done = arguments[arguments.length - 1];
const L = window.__tamLite;
if (!L) { done({ready: false, err: 'not-installed'}); return; }
L.wait(ms, attachFlag).then(done, function (e) { done({ready: false, err: String(e)}); });
"""

JS_LITE_DISPOSE = r"""try { const L = window.__tamLite; if (L) L.dispose(); } catch (e) {} return true;"""

# ---------------- MODE B (sparse): KHÔNG cài gì vào trang trong lúc upload ----------------
#  Mỗi TAM_SPARSE_MS, Python đọc đúng 1 lần thuộc tính disabled của nút Post (querySelector, không layout, không observer).
JS_SPARSE_PREP = r"""
try { if (!window.__tamDoc) window.__tamDoc = Math.random().toString(36).slice(2) + Date.now().toString(36); } catch (e) {}
return {ok: true, doc: window.__tamDoc || null, now: performance.now(), vis: document.visibilityState, url: String(location.href)};
"""

JS_SPARSE_STATE = r"""
const b = document.querySelector(arguments[0]);
let en = false;
if (b) {
  const c = b.className;
  en = !(b.disabled || String(b.getAttribute('aria-disabled') || '').toLowerCase() === 'true' ||
         (typeof c === 'string' && c.toLowerCase().indexOf('disabled') >= 0));
}
return [performance.now(), en, window.__tamDoc || null, document.visibilityState];
"""

# chờ bằng chứng đăng theo lát (tham số: ms)
JS_WAIT_CONFIRM = r"""
const ms = arguments[0], done = arguments[arguments.length - 1];
const S = window.__tam5;
if (!S) { done({pending: true, err: 'not-installed'}); return; }
S.waitConfirm(ms).then(done, function (e) { done({pending: true, err: String(e)}); });
"""

JS_SNAPSHOT = r"""const S = window.__tam5; if (!S) return null; return S.snapshot();"""
JS_WATCH_ONLY = r"""const S = window.__tam5; if (!S) return false; return S.watchOnly(arguments[0]);"""
JS_REARM = r"""const S = window.__tam5; if (!S) return false; return S.rearm();"""
JS_DISPOSE = r"""try { const S = window.__tam5; if (S) S.dispose(); } catch (e) {} return true;"""

# chờ ô nhập file xuất hiện (theo sự kiện); trả về chính phần tử (WebElement) hoặc null khi hết lát
JS_WAIT_FILE_INPUT = r"""
const sel = arguments[0], ms = arguments[1], done = arguments[arguments.length - 1];
function find() { try { return document.querySelector(sel); } catch (e) { return null; } }
let el = find();
if (el) { done(el); return; }
let finished = false, obs = null, tm = null;
function fin(v) {
  if (finished) return; finished = true;
  try { if (obs) obs.disconnect(); } catch (e) {}
  try { if (tm) clearTimeout(tm); } catch (e) {}
  done(v);
}
try {
  obs = new MutationObserver(function () { const e = find(); if (e) fin(e); });
  obs.observe(document.documentElement || document, {childList: true, subtree: true});
} catch (e) {}
tm = setTimeout(function () { fin(null); }, ms);
"""

JS_VIS = r"""return [document.visibilityState, (document.hasFocus ? document.hasFocus() : null), String(location.href), document.readyState];"""

# Ghi lại (KHÔNG bấm gì) những gì xuất hiện khi NGƯỜI tự bấm Post — dùng cho --calibrate-confirm
JS_CALIB = r"""
if (window.__tamCalib && window.__tamCalib.stop) { try { window.__tamCalib.stop(); } catch (e) {} }
const C = {rows: [], t0: performance.now(), seen: {}, stop: null};
function add(kind, text) {
  text = String(text || '').replace(/\s+/g, ' ').trim().slice(0, 200);
  if (!text) return;
  const k = kind + '|' + text;
  if (C.seen[k]) return; C.seen[k] = 1;
  if (C.rows.length < 400) C.rows.push({t: Math.round(performance.now() - C.t0), kind: kind, text: text, url: String(location.href)});
}
const DLG = '[role="dialog"],[role="alertdialog"],[aria-modal="true"]';
const obs = new MutationObserver(function (m) {
  for (let i = 0; i < m.length; i++) {
    const r = m[i];
    if (r.type === 'childList') {
      for (let j = 0; j < r.addedNodes.length; j++) {
        const n = r.addedNodes[j];
        if (n.nodeType === 1) {
          add('text', (n.textContent || '').slice(0, 300));
          try { const d = (n.matches && n.matches(DLG)) ? n : (n.querySelector ? n.querySelector(DLG) : null); if (d) add('dialog', d.textContent); } catch (e) {}
        } else if (n.nodeType === 3) add('text', n.data);
      }
    } else if (r.type === 'characterData') add('text', r.target.data);
  }
  add('url', location.href);
});
obs.observe(document.documentElement, {childList: true, subtree: true, characterData: true});
const hp = history.pushState, hr = history.replaceState;
history.pushState = function () { const r = hp.apply(this, arguments); add('url', location.href); return r; };
history.replaceState = function () { const r = hr.apply(this, arguments); add('url', location.href); return r; };
window.addEventListener('popstate', function () { add('url', location.href); });
C.stop = function () { try { obs.disconnect(); } catch (e) {} history.pushState = hp; history.replaceState = hr; };
window.__tamCalib = C;
add('url', location.href);
return true;
"""


# ======================================================================================
# PHẦN 6 — THAO TÁC TRÌNH DUYỆT / TRANG UPLOAD (chờ theo sự kiện, không sleep cố định)
# ======================================================================================
def _upload_path() -> str:
    """Path nhận diện trang upload (để KHÔNG coi trang upload là "đã đăng xong")."""
    if CFG.upload_url_path:
        return CFG.upload_url_path
    try:
        return urllib.parse.urlparse(TARGET_UPLOAD_URL).path or ""
    except Exception:
        return ""


def _is_upload_url(url: str) -> bool:
    p = _upload_path()
    return bool(p) and p in (url or "")


def _exc_text(e: BaseException) -> str:
    try:
        return (str(e) or "").lower()
    except Exception:
        return ""


def _is_alert_exc(e: BaseException) -> bool:
    return type(e).__name__ == "UnexpectedAlertPresentException" or "unexpected alert" in _exc_text(e)


def _is_nav_exc(e: BaseException) -> bool:
    t = _exc_text(e)
    return ("document unloaded" in t or "unloaded while waiting" in t or "execution context was destroyed" in t
            or "target frame detached" in t or "inspected target navigated" in t or "cannot find context" in t)


def _is_script_timeout(e: BaseException) -> bool:
    t = _exc_text(e)
    return isinstance(e, TimeoutException) or "script timeout" in t or ("timed out" in t and "script" in t)


def _is_dead_browser_exc(e: BaseException) -> bool:
    n = type(e).__name__
    t = _exc_text(e)
    return (n in ("InvalidSessionIdException", "NoSuchWindowException") or "invalid session id" in t or "no such window" in t
            or "chrome not reachable" in t or ("disconnected" in t and "devtools" in t) or "target window already closed" in t
            or "mất kết nối" in t)


def _dismiss_alert(driver):
    """Hộp thoại beforeunload ("Rời trang?") của CHÍNH trang upload khi ta chủ động nạp lại: chấp nhận.
    (Không phải hộp thoại bảo vệ/kiểm tra nội dung của TikTok — loại đó tuyệt đối không tự bấm.)"""
    try:
        driver.switch_to.alert.accept()
        return True
    except Exception:
        pass
    try:
        driver.execute_cdp_cmd("Page.handleJavaScriptDialog", {"accept": True})
        return True
    except Exception:
        return False


def safe_get(driver, url: str):
    try:
        driver.get(url)
    except Exception as e:
        if _is_alert_exc(e) and _dismiss_alert(driver):
            driver.get(url)
        else:
            raise


def _ensure_script_timeout(driver, seconds: float):
    """Đặt script timeout gọn (lát chờ + biên) để một lát bị page throttle (tab nền) không treo Hard Stop hàng chục giây."""
    try:
        if getattr(driver, "_tam_script_to", None) != seconds:
            driver.set_script_timeout(seconds)
            driver._tam_script_to = seconds
    except Exception:
        pass


def wait_file_input(driver, timeout_s: float, kill: Optional[Callable[[], bool]] = None, sel: Optional[str] = None):
    """Chờ <input type=file> xuất hiện bằng MutationObserver trong trang (trả về WebElement hoặc None khi hết giờ/bị dừng)."""
    sel = sel or CFG.sel_file
    slice_s = CFG.slice_ms / 1000.0
    _ensure_script_timeout(driver, slice_s + 1.5)
    end = time.perf_counter() + timeout_s
    while True:
        if kill and kill():
            return None
        left = end - time.perf_counter()
        if left <= 0:
            return None
        ms = int(max(20, min(slice_s, left) * 1000))
        try:
            el = driver.execute_async_script(JS_WAIT_FILE_INPUT, sel, ms)
        except Exception as e:
            if _is_script_timeout(e):
                continue
            if _is_alert_exc(e):
                _dismiss_alert(driver)
                continue
            if _is_nav_exc(e):
                time.sleep(0.02)
                continue
            raise
        if el is not None:
            return el


def ensure_upload_page(driver, fsm: "ProfileFSM", log: Callable[[str], None], kill: Optional[Callable[[], bool]] = None):
    """Đảm bảo trang đang ở form upload SẠCH và có ô nhập file. Trả (WebElement|None, lý do).

    * Trang đã ở form upload & sạch  -> KHÔNG nạp lại (giữ trang sẵn sàng; không sleep).
    * Trang bẩn (đã gắn file ở lần trước, lỗi giữa chừng) hoặc đang ở trang khác -> điều hướng về TARGET_UPLOAD_URL.
    * Không thấy ô nhập file sau 10 s -> refresh một lần (như bản gốc), rồi chờ thêm 10 s.
    """
    cur = ""
    need_nav = fsm.dirty or CFG.force_reload
    if not need_nav:
        try:
            cur = driver.current_url
        except Exception as e:
            return None, f"không đọc được URL trình duyệt: {e}"
        need_nav = not _is_upload_url(cur)
    if need_nav:
        why = "trang bẩn sau lần trước" if fsm.dirty else ("TAM_FORCE_RELOAD=1" if CFG.force_reload else f"đang ở {cur[:70]}")
        log(f" - [TikTok] Nạp lại trang Upload ({why})...")
        try:
            safe_get(driver, TARGET_UPLOAD_URL)
        except Exception as e:
            return None, f"nạp trang Upload lỗi: {e}"
        el = wait_file_input(driver, CFG.page_ready_timeout_s, kill)
        if el is None:
            return None, "không thấy chỗ nạp file sau khi nạp trang Upload (chưa đăng nhập?)"
        fsm.mark_page_clean()
        return el, "reloaded"
    el = wait_file_input(driver, CFG.file_input_wait_s, kill)
    if el is None and not (kill and kill()):
        log(" - [TikTok] Không thấy chỗ nạp file, thử refresh...")
        try:
            driver.refresh()
        except Exception as e:
            if _is_alert_exc(e) and _dismiss_alert(driver):
                driver.refresh()
            else:
                return None, f"refresh lỗi: {e}"
        el = wait_file_input(driver, CFG.file_input_wait_s, kill)
        if el is None:
            return None, "không thấy chỗ nạp file (kể cả sau refresh)"
        return el, "refreshed"
    if el is None:
        return None, "bị dừng"
    return el, "ok"


def prepare_upload_page(driver, log: Callable[[str], None], kill: Optional[Callable[[], bool]] = None,
                        fsm: Optional["ProfileFSM"] = None, trace: Optional[PerfTrace] = None, tag: str = "") -> bool:
    """Điều hướng về trang Upload và CHỈ trả về khi ô nhập file đã có (thay cho `drv.get(...) + sleep 5 s`).
    Dùng giữa các video (TT/CUM_BATCH) và cho KEEP-ALIVE sau MOVE."""
    try:
        safe_get(driver, TARGET_UPLOAD_URL)
        el = wait_file_input(driver, CFG.page_ready_timeout_s, kill)
    except Exception as e:
        log(f"❌ {tag}Nạp lại trang Upload lỗi: {e}")
        return False
    if el is None:
        return False
    if fsm is not None:
        fsm.mark_page_clean()
        if fsm.state == PState.ERROR:
            # trang vừa nạp SẠCH (đã thấy ô nạp file): lỗi trước-khi-bấm của việc trước đã được dọn → việc kế tiếp KHÔNG nạp lại
            # lần nữa sau DOWNLOAD_DONE. UNKNOWN thì KHÔNG đụng (cần người kiểm tra).
            fsm.to(PState.READY, "trang upload vừa nạp sạch")
    if trace is not None:
        trace.mark("NEXT_PROFILE_READY", overwrite=True)
    return True


def dispose_watcher(driver):
    try:
        driver.execute_script(JS_DISPOSE)
    except Exception:
        pass


def profile_closed(pid):
    """GUI gọi sau khi đóng một profile: trình duyệt cũ đã mất nên trạng thái (kể cả UNKNOWN/ERROR) của nó không còn ý nghĩa.
    Các file nghi đã đăng vẫn bị KHOÁ bởi nhật ký (journal), không phụ thuộc hàm này."""
    drop_fsm(pid)


def setup_and_navigate_profile(profile_id, genlogin_token, chromedriver_path, url):
    """Mở profile Genlogin, gắn Selenium vào Chrome đang chạy (debuggerAddress) và nạp trang Upload.

    Giữ nguyên cách gắn cũ. Lưu ý (đã kiểm chứng bằng thực nghiệm): khi gắn vào Chrome đang chạy, `prefs` và
    `--disk-cache-dir` KHÔNG có hiệu lực (Chrome đã khởi động trước) nên chỉ còn là mã chết — giữ lại cho tương thích.
    Thêm: chờ ô nhập file (không bắt buộc, tối đa TAM_OPEN_READY_WAIT_S) để trang ở trạng thái SẴN SÀNG trước khi quét.
    """
    if Genlogin is None:
        print("❌ Genlogin not available")
        return profile_id, None

    drop_fsm(profile_id)            # mở profile = trình duyệt MỚI => máy trạng thái bắt đầu lại từ IDLE
    gen = Genlogin(GENLOGIN_API_TOKEN)
    try:
        profile_info = gen.runProfile(profile_id)
        if not profile_info or "wsEndpoint" not in profile_info:
            raise Exception("Không thể lấy WebSocket endpoint.")
        wsEndpoint = profile_info["wsEndpoint"].replace("ws://", "").split('/')[0]
        chrome_options = Options()

        prefs = {
            "profile.managed_default_content_settings.images": 2,
            "profile.default_content_setting_values.notifications": 2
        }
        chrome_options.add_experimental_option("prefs", prefs)

        if os.path.exists("R:\\"):
            chrome_options.add_argument(f"--disk-cache-dir=R:\\TikTokCache\\{profile_id}")

        if CFG.page_load in ("eager", "none"):
            try:
                chrome_options.page_load_strategy = CFG.page_load
            except Exception:
                pass
        chrome_options.add_experimental_option("debuggerAddress", wsEndpoint)
        service = Service(executable_path=chromedriver_path)
        driver = webdriver.Chrome(service=service, options=chrome_options)
        try:
            driver._tam_dbg_addr = wsEndpoint            # để công cụ đo (--bench-overhead) tìm tiến trình Chrome theo cổng debug
        except Exception:
            pass
        driver.set_page_load_timeout(60)
        apply_focus_emu(driver)                          # TAM_FOCUS_EMU=1: cả lúc nạp trang ở nền cũng không bị bóp (mặc định tắt)
        driver.get(url)
        wait_s = _env_float("TAM_OPEN_READY_WAIT_S", 10.0)
        if wait_s > 0:
            try:
                el = wait_file_input(driver, wait_s)
                fsm = get_fsm(profile_id)
                if el is not None:
                    fsm.mark_page_clean()
                    if fsm.state == PState.IDLE:
                        fsm.to(PState.READY, "mở profile: trang upload sẵn sàng")
                else:
                    print(f" - [Profile {profile_id}] Chưa thấy chỗ nạp file sau {wait_s:.0f}s (chưa đăng nhập?). Vẫn giữ profile mở.")
            except Exception as e:
                print(f" - [Profile {profile_id}] Bỏ qua bước chờ trang sẵn sàng: {e}")
        return profile_id, driver
    except Exception as e:
        print(f" - LỖI khi thiết lập profile {profile_id}: {e}")
        return profile_id, None


# ======================================================================================
# PHẦN 7 — THÀNH PHẦN CỦA LẦN ĐĂNG: chờ nút (theo lát sự kiện), click CDP, chờ BẰNG CHỨNG đăng
# ======================================================================================
class JobError(Exception):
    """Lỗi xảy ra TRƯỚC khi bấm Post (an toàn để thử lại)."""


class JobHooks:
    """Điểm móc để Scanner ghi nhật ký bền vững đúng lúc.
    before_click(): gọi NGAY TRƯỚC mỗi lần phát lệnh click (ghi POSTING). Trả False => huỷ, không bấm.
    after_result(res): gọi một lần với kết quả cuối."""

    def __init__(self, before_click: Optional[Callable[[], Any]] = None, after_result: Optional[Callable[["UploadResult"], Any]] = None):
        self.before_click = before_click
        self.after_result = after_result


def js_cfg() -> Dict[str, Any]:
    return {
        "sel": CFG.sel_post, "fileSel": CFG.sel_file,
        "confirmUrl": CFG.confirm_url_re.pattern, "confirmText": CFG.confirm_text_re.pattern, "failText": CFG.fail_text_re.pattern,
        "uploadPath": _upload_path(), "upDoneSel": CFG.upload_done_sel, "upDoneText": CFG.upload_done_re,
    }


def install_watcher(driver, trace: Optional[PerfTrace] = None, sync: Optional[ClockSync] = None) -> Dict[str, Any]:
    """Cài watcher (1 lệnh WebDriver). Giá trị `now` trả về còn dùng làm mẫu đồng bộ đồng hồ đầu tiên."""
    t0 = time.perf_counter_ns()
    r = driver.execute_script(JS_INSTALL, js_cfg())
    t1 = time.perf_counter_ns()
    if sync is not None and isinstance(r, dict):
        sync.add_sample(t0, t1, r.get("now"), r.get("doc"))
    return r if isinstance(r, dict) else {}


def _alert_text(driver) -> str:
    try:
        return str(driver.switch_to.alert.text)
    except Exception:
        return ""


def _wait_post_ready_poll(driver, kill: Callable[[], bool], timeout_s: float, stats: Optional[Dict[str, Any]] = None):
    """CHỈ ĐỂ A/B (TAM_WAIT_MODE=poll): Python hỏi trang mỗi TAM_POLL_S giây — mỗi lần hỏi là 1 vòng WebDriver cộng một lần tính lại
    trong trang. Độ trễ phát hiện trung bình ≈ poll_s/2 + RTT. Cùng bảo vệ lúc nhấn và cùng điều kiện sẵn sàng như chế độ sự kiện."""
    end = time.perf_counter() + timeout_s
    last = None
    n = 0
    while True:
        if kill():
            return "killed", last
        if time.perf_counter() >= end:
            return "timeout", last
        try:
            r = driver.execute_script(JS_POLL_READY, True)
        except Exception as e:
            if _is_alert_exc(e):
                raise JobError(f"hộp thoại của trang chặn lệnh (không tự bấm): {_alert_text(driver)[:120]}")
            if _is_nav_exc(e):
                raise JobError("trang điều hướng/nạp lại khi đang chờ nút Post (mất watcher)")
            if _is_dead_browser_exc(e):
                raise JobError(f"mất kết nối trình duyệt: {e}")
            raise
        n += 1
        if stats is not None:
            stats["polls"] = n
        if isinstance(r, dict):
            if r.get("ready"):
                return "ready", r
            if r.get("err") == "not-installed":
                raise JobError("watcher không còn trong trang (trang đã nạp lại?)")
            last = r
        time.sleep(CFG.poll_s)


def wait_post_ready(driver, kill: Callable[[], bool], timeout_s: float, stats: Optional[Dict[str, Any]] = None):
    """Chờ nút Post sẵn sàng (watcher ĐẦY ĐỦ đã cài). Trả ('ready', payload) | ('timeout', last) | ('killed', last).
    Lỗi trước-khi-bấm => JobError. Mode sparse/lite: watcher đầy đủ chỉ được cài SAU khi nút đã bật → hàm này chạy chế độ sự kiện."""
    if CFG.wait_mode == "poll":
        return _wait_post_ready_poll(driver, kill, timeout_s, stats)
    return _wait_post_ready_event(driver, kill, timeout_s, stats)


def _raise_pre_click(driver, e: BaseException, what: str):
    if _is_alert_exc(e):
        raise JobError(f"hộp thoại của trang chặn lệnh (không tự bấm): {_alert_text(driver)[:120]}")
    if _is_nav_exc(e):
        raise JobError(f"trang điều hướng/nạp lại khi đang chờ nút Post ({what})")
    if _is_dead_browser_exc(e):
        raise JobError(f"mất kết nối trình duyệt: {e}")
    raise e


def wait_post_enabled_light(driver, kill: Callable[[], bool], timeout_s: float, stats: Dict[str, Any], mode: str):
    """B/C: chờ nút Post BẬT mà không cài watcher nặng. Trả ('ready', {readyAt, attachedAt, doc, now, vis}) | timeout | killed.
    B (sparse): mỗi TAM_SPARSE_MS đọc 1 thuộc tính (readyAt = lần đọc đầu tiên thấy bật, sai số ≤ 1 chu kỳ).
    C (lite):   observer trên CHÍNH nút; Python chờ theo lát execute_async_script (readyAt chính xác theo đồng hồ trang)."""
    end = time.perf_counter() + timeout_s
    n = 0
    if mode == "lite":
        slice_s = CFG.slice_ms / 1000.0
        _ensure_script_timeout(driver, slice_s + 1.5)
    while True:
        if kill():
            return "killed", None
        left = end - time.perf_counter()
        if left <= 0:
            return "timeout", None
        try:
            if mode == "lite":
                r = driver.execute_async_script(JS_LITE_WAIT, int(max(20, min(CFG.slice_ms / 1000.0, left) * 1000)), True)
            else:
                now, en, doc, vis = driver.execute_script(JS_SPARSE_STATE, CFG.sel_post)
                r = {"ready": bool(en), "readyAt": now if en else None, "attachedAt": None, "now": now, "doc": doc, "vis": vis}
        except Exception as e:
            if mode == "lite" and _is_script_timeout(e):
                n += 1
                continue
            _raise_pre_click(driver, e, f"mode {mode}")
        n += 1
        stats["light_calls"] = n
        if isinstance(r, dict):
            if r.get("err") == "not-installed":
                raise JobError("watcher nhẹ không còn trong trang (trang đã nạp lại?)")
            if r.get("ready"):
                stats["light_stats"] = r.get("stats")
                return "ready", r
        if mode == "sparse":
            time.sleep(min(CFG.sparse_ms / 1000.0, max(0.0, end - time.perf_counter())))


def _wait_post_ready_event(driver, kill: Callable[[], bool], timeout_s: float, stats: Optional[Dict[str, Any]] = None):
    slice_s = CFG.slice_ms / 1000.0
    _ensure_script_timeout(driver, slice_s + 1.5)
    end = time.perf_counter() + timeout_s
    last = None
    n = 0
    while True:
        if kill():
            return "killed", last
        left = end - time.perf_counter()
        if left <= 0:
            return "timeout", last
        ms = int(max(20, min(slice_s, left) * 1000))
        try:
            r = driver.execute_async_script(JS_WAIT_READY, ms, True)
        except Exception as e:
            if _is_script_timeout(e):
                n += 1
                continue
            if _is_alert_exc(e):
                raise JobError(f"hộp thoại của trang chặn lệnh (không tự bấm): {_alert_text(driver)[:120]}")
            if _is_nav_exc(e):
                raise JobError("trang điều hướng/nạp lại khi đang chờ nút Post (mất watcher)")
            if _is_dead_browser_exc(e):
                raise JobError(f"mất kết nối trình duyệt: {e}")
            raise
        n += 1
        if stats is not None:
            stats["slices"] = n
        if isinstance(r, dict):
            if r.get("ready"):
                return "ready", r
            if r.get("err") == "not-installed":
                raise JobError("watcher không còn trong trang (trang đã nạp lại?)")
            last = r


def cdp_click(driver, x: float, y: float):
    """Click chuột TIN CẬY qua CDP (giống bản gốc nhưng bỏ sleep 8 ms: các lệnh CDP được Chrome xử lý theo thứ tự)."""
    if CFG.click_move:
        driver.execute_cdp_cmd('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x, 'y': y, 'button': 'none'})
    driver.execute_cdp_cmd('Input.dispatchMouseEvent', {'type': 'mousePressed', 'x': x, 'y': y, 'button': 'left', 'clickCount': 1})
    driver.execute_cdp_cmd('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': x, 'y': y, 'button': 'left', 'clickCount': 1})


def payload_landed(p: Optional[Dict[str, Any]]) -> bool:
    """Có BẤT KỲ sự kiện chuột nào đã tới nút (xuống/lên/click) mà bảo vệ không chặn => coi là ĐÃ bấm, không bao giờ bấm lại.
    (Sự kiện bị chặn nằm ở `blocked`, không nằm ở `ev`; up/click mồ côi sau một lần nhấn bị chặn cũng bị chặn.)"""
    for e in (p or {}).get("ev") or []:
        if e.get("on") and e.get("type") in ("mousedown", "pointerdown", "mouseup", "pointerup", "click"):
            return True
    return False


def describe_blocked(p: Optional[Dict[str, Any]]) -> str:
    b = (p or {}).get("blocked") or []
    if not b:
        return "không thấy sự kiện chuột nào chạm trang"
    tags = sorted({str(x.get("tag")) for x in b})
    nr = sum(1 for x in b if x.get("notReady"))
    orph = sum(1 for x in b if x.get("orphan"))
    s = f"{len(b)} sự kiện bị chặn (đích: {','.join(tags)})"
    if nr:
        s += f", {nr} lần nút vừa tắt lại đúng lúc nhấn"
    if orph:
        s += f", {orph} sự kiện up/click bị chặn vì không có nhấn xuống hợp lệ"
    return s


def _verdict_to_status(v: Dict[str, Any]):
    k = v.get("kind")
    if k == "url":
        return PostStatus.CONFIRMED, f"URL → {v.get('url')}"
    if k == "ok-text":
        return PostStatus.CONFIRMED, f"văn bản mới: “{v.get('text')}”"
    if k == "fail-text":
        return PostStatus.FAILED_VISIBLE, f"TikTok báo lỗi: “{v.get('text')}”"
    return PostStatus.UNKNOWN, f"bằng chứng lạ: {v}"


def _after_nav(driver, pre_url: str, extra: Dict[str, Any]):
    """Trang đã điều hướng sau khi bấm. Trả dict kết quả cuối, hoặc None nếu cần tiếp tục chờ (đã gắn watcher vào tài liệu mới)."""
    try:
        url = driver.current_url
    except Exception as e2:
        return {"status": PostStatus.UNKNOWN, "reason": f"mất kết nối sau khi trang điều hướng: {e2}", "nav": True, "extra": extra}
    if url != pre_url and CFG.confirm_url_re.search(url or "") and not _is_upload_url(url):
        return {"status": PostStatus.CONFIRMED, "evidence": f"URL → {url}", "at": None, "doc": None, "nav": True, "extra": extra}
    extra["urlAfterNav"] = url
    try:
        install_watcher(driver)
        driver.execute_script(JS_WATCH_ONLY, pre_url)       # chỉ quan sát tài liệu mới, không nhấn gì
    except Exception:
        time.sleep(0.05)
    return None


def await_confirm(driver, kill: Callable[[], bool], pre_url: str, first: Optional[Dict[str, Any]], t_click_py_ns: int,
                  log: Callable[[str], None]) -> Dict[str, Any]:
    """Sau khi nút đã bị bấm: chờ BẰNG CHỨNG GIAO DIỆN. Không có bằng chứng thì KHÔNG bao giờ trả CONFIRMED.

    Trả về dict: status(PostStatus) / evidence / reason / at(ms đồng hồ Chrome) / doc / nav(bool) / extra.
    """
    slice_s = CFG.slice_ms / 1000.0
    _ensure_script_timeout(driver, slice_s + 1.5)
    end = t_click_py_ns / 1e9 + CFG.confirm_timeout_s
    payload = first
    nav = False
    extra = {}      # type: Dict[str, Any]
    lost_polls = 0
    while True:
        if payload:
            if payload.get("dialog"):
                extra["dialog"] = payload["dialog"]
            if payload.get("newText"):
                extra["newText"] = payload["newText"]
            if payload.get("urlOther"):
                extra["urlOther"] = payload["urlOther"]
            v = payload.get("verdict")
            if v:
                st, ev = _verdict_to_status(v)
                return {"status": st, "evidence": ev, "at": v.get("at"), "doc": payload.get("doc"), "nav": nav, "extra": extra}
            if payload.get("err") == "not-installed":      # tài liệu mới không còn watcher => đã điều hướng không kèm lỗi
                nav = True
                r = _after_nav(driver, pre_url, extra)
                if r is not None:
                    return r
                lost_polls += 1
                if lost_polls > 40:
                    return {"status": PostStatus.UNKNOWN, "reason": "không gắn lại được watcher vào trang mới", "nav": True, "extra": extra}
        if kill():
            return {"status": PostStatus.UNKNOWN, "reason": "Hard Stop khi đã bấm Post, chưa kịp xác minh", "nav": nav, "extra": extra}
        left = end - time.perf_counter_ns() / 1e9
        if left <= 0:
            reason = f"không thấy bằng chứng đăng trong {CFG.confirm_timeout_s:.0f}s sau khi bấm"
            if extra.get("dialog"):
                reason += f"; có hộp thoại: “{str(extra['dialog'].get('text', ''))[:100]}” (KHÔNG tự bấm)"
            return {"status": PostStatus.UNKNOWN, "reason": reason, "nav": nav, "extra": extra}
        ms = int(max(20, min(slice_s, left) * 1000))
        try:
            payload = driver.execute_async_script(JS_WAIT_CONFIRM, ms)
        except Exception as e:
            payload = None
            if _is_script_timeout(e):
                continue
            if _is_nav_exc(e):
                nav = True
                r = _after_nav(driver, pre_url, extra)
                if r is not None:
                    return r
                continue
            if _is_alert_exc(e):
                return {"status": PostStatus.UNKNOWN, "reason": f"hộp thoại trình duyệt sau khi bấm: {_alert_text(driver)[:120]} (KHÔNG tự bấm)", "nav": nav, "extra": extra}
            if _is_dead_browser_exc(e):
                return {"status": PostStatus.UNKNOWN, "reason": f"mất kết nối trình duyệt sau khi bấm: {e}", "nav": nav, "extra": extra}
            return {"status": PostStatus.UNKNOWN, "reason": f"lỗi khi chờ bằng chứng: {type(e).__name__}: {e}", "nav": nav, "extra": extra}


# ======================================================================================
# PHẦN 8 — MỘT LẦN ĐĂNG HOÀN CHỈNH:  gắn file → chờ nút → bấm → XÁC NHẬN bằng giao diện
# ======================================================================================
class _Ctx:
    """Trạng thái dùng chung giữa phần lõi và phần xử lý lỗi (để biết lỗi xảy ra TRƯỚC hay SAU khi bấm)."""
    __slots__ = ("maybe_clicked", "clicked", "nav", "t_attached", "t_start", "dry_run")

    def __init__(self):
        self.maybe_clicked = False     # đã phát lệnh click nhưng chưa biết có trúng nút không
        self.clicked = False           # chuột đã nhấn xuống đúng nút
        self.nav = False
        self.t_attached = None
        self.t_start = time.perf_counter()
        self.dry_run = False           # chỉ CLI --bench-upload: dừng ngay trước khi bấm Post


def _prepare_fsm(fsm: "ProfileFSM"):
    st = fsm.state
    if st == PState.ERROR:
        fsm.dirty = True
        fsm.to(PState.READY, "phục hồi: sẽ nạp lại trang")
    elif st in (PState.CONFIRMED, PState.CLEANUP, PState.IDLE):
        fsm.to(PState.READY, "bắt đầu việc mới")
    elif st in (PState.ATTACHING, PState.UPLOADING, PState.PROCESSING, PState.POST_READY, PState.POSTING):
        fsm.to(PState.ERROR, "trạng thái dở dang từ việc trước")
        fsm.dirty = True
        fsm.to(PState.READY, "phục hồi: sẽ nạp lại trang")


def _send_keys_with_refind(driver, el, text: str):
    try:
        el.send_keys(text)
        return
    except Exception as e:
        if type(e).__name__ != "StaleElementReferenceException" and "stale element" not in _exc_text(e):
            raise
    el2 = driver.find_element(By.CSS_SELECTOR, CFG.sel_file)          # React vừa dựng lại input: lấy lại đúng một lần
    el2.send_keys(text)


def _debounce_ready(driver, kill, ready: Dict[str, Any], t_ref_ns: int, wstats: Dict[str, Any]):
    """Chỉ khi TAM_READY_STABLE_MS>0. Đảm bảo nút Post sáng LIÊN TỤC ≥ ready_stable_ms (cùng một mốc readyAt) rồi mới cho bấm.
    Nút chớp tắt giữa chừng => chờ sáng lại và tính lại. Trả ('ready', payload) | ('killed'|'timeout', None)."""
    deadline = time.perf_counter() + CFG.ready_timeout_s
    while True:
        if kill():
            return "killed", None
        try:
            age_ms = (float(ready["now"]) - float(ready["readyAt"])) + (time.perf_counter_ns() - t_ref_ns) / 1e6
        except Exception:
            age_ms = 0.0
        if age_ms < CFG.ready_stable_ms:
            time.sleep((CFG.ready_stable_ms - age_ms) / 1000.0)
        chk = driver.execute_script(JS_POLL_READY, True)          # 1 vòng WebDriver, chỉ chạy khi bật debounce
        t_chk = time.perf_counter_ns()
        if isinstance(chk, dict) and chk.get("ready"):
            if chk.get("readyAt") == ready.get("readyAt"):
                return "ready", chk                               # cùng một khoảng sáng liên tục, đã đủ lâu
            wstats["flaps"] = wstats.get("flaps", 0) + 1          # đã tắt rồi sáng lại trong lúc chờ: khoảng sáng mới
            ready, t_ref_ns = chk, t_chk
            continue
        wstats["flaps"] = wstats.get("flaps", 0) + 1
        left = deadline - time.perf_counter()
        if left <= 0:
            return "timeout", None
        st, r2 = wait_post_ready(driver, kill, left, wstats)
        if st != "ready" or not r2:
            return st, None
        ready, t_ref_ns = r2, time.perf_counter_ns()


def _warm_gate(driver, kill, trace: PerfTrace):
    """TAM_WARM_MS>0 (A/B): chỉ gắn file khi trang đã đủ tuổi và readyState=complete. Mặc định tắt; chờ theo điều kiện, không ngủ cố định."""
    t0 = time.perf_counter()
    waited = 0.0
    while not kill():
        try:
            age, rs = driver.execute_script(JS_PAGE_AGE)
        except Exception:
            break
        if rs == "complete" and age >= CFG.warm_ms:
            break
        if time.perf_counter() - t0 > 30.0:
            break
        time.sleep(0.02 if (CFG.warm_ms - (age or 0)) < 100 else 0.05)
    waited = time.perf_counter() - t0
    trace.set("x_warm_wait_ms", round(waited * 1000.0))


def _finish_attach(driver, trace: PerfTrace, sync: ClockSync, ready: Dict[str, Any], rec_ok: bool, m0):
    """Mốc FILE_CHANGE (sự kiện change của ô file, đồng hồ trang) + dữ liệu ghi thụ động. Dùng chung cho đăng bằng tool và --bench-attach (đăng tay)."""
    att = ready.get("attachedAt")
    if isinstance(att, (int, float)):
        trace.mark_chrome("FILE_CHANGE", att, ready.get("doc"), sync)
        trace.set("x_page_age_at_attach_ms", round(att))
    else:
        trace.mark_unknown("FILE_CHANGE", "trang không báo sự kiện change của ô file")
    if rec_ok:
        _collect_rec(driver, trace, sync, ready, m0)


def _collect_rec(driver, trace: PerfTrace, sync: ClockSync, ready: Dict[str, Any], m0):
    try:
        m1 = cdp_metrics(driver)
        rec = driver.execute_script(JS_REC_READ)
        ana = rec_analyze(rec, ready.get("attachedAt"), ready.get("readyAt"))
        for k, v in cdp_delta(m0, m1).items():
            trace.set(k, v)
        for k, v in ana.items():
            trace.set(k, v)
        endp = ana.get("rec_upload_end_page_ms")
        if endp is not None and ready.get("upDoneAt") is None and ana.get("x_after_net_to_ready_ms", -1) >= 0:
            trace.mark_chrome("UPLOAD_COMPLETED", endp, ready.get("doc"), sync, overwrite=True)
            trace.set("upload_signal", "resource-timing(TAM_UPLOAD_URL_RE)")
    except Exception as e:
        trace.set("rec_error", _exc_text(e)[:160])


def _upload_core(driver, paths: List[str], log, kill, pid: str, trace: PerfTrace, fsm: "ProfileFSM", sync: ClockSync,
                 hooks: Optional[JobHooks], ctx: _Ctx) -> "UploadResult":
    S = PostStatus
    trace.mark("JOB_START")
    if not driver or not paths:
        return UploadResult(S.FAILED_SAFE, "thiếu driver hoặc danh sách video", trace=trace, paths=paths)
    if kill():
        return UploadResult(S.KILLED, "Hard Stop trước khi bắt đầu", trace=trace, paths=paths)
    if fsm.state == PState.UNKNOWN:
        return UploadResult(S.FAILED_SAFE, "profile đang UNKNOWN (lần đăng trước chưa xác minh được) — cần người kiểm tra tay rồi reset",
                            trace=trace, paths=paths)
    _prepare_fsm(fsm)
    log(f"⚡ [Perf] Bắt đầu Upload {len(paths)} files (EVENT watcher + CDP, xác nhận bằng giao diện)...")

    # --- 1) trang upload sẵn sàng + ô nhập file (không sleep; không nạp lại nếu đang sạch) ---
    el, why = ensure_upload_page(driver, fsm, log, kill)
    trace.mark("PAGE_READY")                              # PAGE_RELOAD_MS = JOB_START → PAGE_READY (gồm nạp lại nếu trang bẩn)
    trace.set("page_reloaded", 1 if why in ("reloaded", "refreshed") else 0)
    if el is None:
        if kill():
            return UploadResult(S.KILLED, "Hard Stop khi chờ trang upload", trace=trace, paths=paths)
        fsm.to(PState.ERROR, why)
        return UploadResult(S.FAILED_SAFE, why, trace=trace, paths=paths)
    if kill():
        return UploadResult(S.KILLED, "Hard Stop", trace=trace, paths=paths)
    if CFG.bring_to_front:
        try:
            driver.execute_cdp_cmd("Page.bringToFront", {})
        except Exception:
            pass

    focus = apply_focus_emu(driver)
    trace.set("focus_emu", 1 if focus else 0)
    if CFG.warm_ms > 0:
        _warm_gate(driver, kill, trace)
        if kill():
            return UploadResult(S.KILLED, "Hard Stop khi chờ trang ấm", trace=trace, paths=paths)

    # --- 2) cài watcher TRƯỚC khi gắn file (để không bỏ lỡ video xử lý rất nhanh) ---
    #     B (sparse) / C (lite): KHÔNG cài watcher đầy đủ trong lúc upload — chỉ cài sau khi nút Post đã bật.
    light = CFG.wait_mode in ("sparse", "lite")
    if light:
        t0i = time.perf_counter_ns()
        try:
            if CFG.wait_mode == "lite":
                info = driver.execute_script(JS_LITE_INSTALL, CFG.sel_post, CFG.lite_tick_ms) or {}
            else:
                info = driver.execute_script(JS_SPARSE_PREP) or {}
        except Exception as e:
            info = {"ok": False, "err": str(e)}
        if isinstance(info, dict):
            sync.add_sample(t0i, time.perf_counter_ns(), info.get("now"), info.get("doc"))
    else:
        info = install_watcher(driver, trace, sync)
    if not info.get("ok"):
        fsm.to(PState.ERROR, "không cài được watcher")
        return UploadResult(S.FAILED_SAFE, "không cài được watcher vào trang", trace=trace, paths=paths)
    pre_url = str(info.get("url") or "")
    trace.set("vis_install", info.get("vis"))
    if info.get("reErr"):
        log(f"⚠️ [Confirm] Regex xác nhận không hợp lệ cho JavaScript: {info['reErr']}")

    rec_ok = False
    m0 = None
    if CFG.rec_enable:
        rec_ok = bool(rec_install(driver).get("ok"))
        m0 = cdp_metrics(driver)

    # --- 3) gắn file ---
    fsm.to(PState.ATTACHING, "send_keys")
    trace.mark("SEND_KEYS_START")
    _send_keys_with_refind(driver, el, "\n".join(paths))
    trace.mark("SEND_KEYS_END")
    ctx.t_attached = time.perf_counter()
    fsm.to(PState.UPLOADING, "đã gắn file")
    log(f"⏱️ [Timing] T1 (Đã nạp file): {ctx.t_attached - ctx.t_start:.3f}s")
    if CFG.wait_mode == "poll":
        log(f"⚡ [INSTANT] (A/B) Chế độ POLLING {CFG.poll_s * 1000:.0f} ms — chỉ để so sánh với chế độ sự kiện.")
    elif CFG.wait_mode == "lite":
        log("⚡ [INSTANT] MODE C (lite): chỉ theo dõi chính nút Post trong lúc upload (không observer toàn trang, không ép layout).")
    elif CFG.wait_mode == "sparse":
        log(f"⚡ [INSTANT] MODE B (sparse): KHÔNG cài watcher trong lúc upload; đọc trạng thái nút mỗi {CFG.sparse_ms} ms.")
    else:
        log("⚡ [INSTANT] Rình nút Post theo SỰ KIỆN ngay từ lúc attach file (không polling, không chờ cứng).")
    if CFG.perf_enable and not light:
        sync.sync(driver, samples=3)      # lúc này trình duyệt đang upload => tốn vài ms ở đây không ảnh hưởng ReadyToClick

    # --- 4) chờ nút Post sẵn sàng (theo lát; Hard Stop kiểm tra giữa các lát) ---
    wstats = {}     # type: Dict[str, Any]
    if light:
        state, lready = wait_post_enabled_light(driver, kill, CFG.ready_timeout_s, wstats, CFG.wait_mode)
        t_enabled_py = time.perf_counter_ns()
        ready = None
        if state == "ready" and lready:
            if CFG.wait_mode == "lite":
                try:
                    driver.execute_script(JS_LITE_DISPOSE)
                except Exception:
                    pass
            # nút đã BẬT: giờ mới cài watcher đầy đủ (kiểm tra hiển thị/che, bảo vệ lúc nhấn, bằng chứng đăng) — như mode A
            info2 = install_watcher(driver, trace, sync)
            if not info2.get("ok"):
                fsm.to(PState.ERROR, "không cài được watcher")
                return UploadResult(S.FAILED_SAFE, "không cài được watcher vào trang", trace=trace, paths=paths)
            left = max(1.0, CFG.ready_timeout_s - (time.perf_counter() - ctx.t_attached))
            state, ready = wait_post_ready(driver, kill, left, wstats)
            if state == "ready" and ready:
                ready = dict(ready, readyAt=lready.get("readyAt") if lready.get("doc") == ready.get("doc") else ready.get("readyAt"),
                             attachedAt=lready.get("attachedAt"), handoffAt=ready.get("readyAt"))
                wstats["handoff_ms"] = round((time.perf_counter_ns() - t_enabled_py) / 1e6, 1)
    else:
        state, ready = wait_post_ready(driver, kill, CFG.ready_timeout_s, wstats)
    t_resp = time.perf_counter_ns()
    if state == "killed":
        fsm.to(PState.ERROR, "Hard Stop khi chờ nút Post")
        return UploadResult(S.KILLED, "Hard Stop khi chờ nút Post (chưa bấm)", trace=trace, paths=paths)
    if state == "timeout" or not ready:
        det = ""
        if ready is None:
            det = ""
        fsm.to(PState.ERROR, "hết giờ chờ nút Post")
        return UploadResult(S.FAILED_SAFE, f"hết {CFG.ready_timeout_s:.0f}s mà nút Post chưa sẵn sàng (chưa bấm){det}", trace=trace, paths=paths)
    trace.mark_chrome("PROCESSING_READY", ready.get("readyAt"), ready.get("doc"), sync)
    if trace.marks.get("PROCESSING_READY") is None or trace.marks["PROCESSING_READY"].py_ns is None and trace.marks["PROCESSING_READY"].chrome_ms is None:
        trace.mark("PROCESSING_READY", t_resp, "py(resp)", overwrite=True)
    if ready.get("upDoneAt") is not None:
        trace.mark_chrome("UPLOAD_COMPLETED", ready["upDoneAt"], ready.get("doc"), sync)
        fsm.to(PState.PROCESSING, "upload xong")
    else:
        trace.mark_unknown("UPLOAD_COMPLETED", "không có tín hiệu: đặt TAM_UPLOAD_DONE_SEL / TAM_UPLOAD_DONE_TEXT_RE để đo")
    fsm.to(PState.POST_READY, "nút Post sẵn sàng")
    trace.set("vis_ready", ready.get("vis"))
    trace.set("watch_stats", ready.get("stats"))
    trace.set("slices", wstats.get("slices"))
    trace.set("polls", wstats.get("polls"))
    trace.set("light_calls", wstats.get("light_calls"))
    trace.set("light_stats", wstats.get("light_stats"))
    trace.set("handoff_ms", wstats.get("handoff_ms"))
    trace.set("wait_mode", CFG.wait_mode)
    trace.set("attach_mode", "tool")
    _finish_attach(driver, trace, sync, ready, rec_ok, m0)
    try:
        lag = max(0.0, float(ready["now"]) - float(ready["readyAt"]))
        lag_s = f" | watcher→Python≈{lag:.0f}ms"
    except Exception:
        lag_s = ""
    log(f"💡 [READY] Nút Post vừa sáng sau {time.perf_counter() - ctx.t_attached:.3f}s{lag_s} | tab={ready.get('vis')}")
    if ready.get("vis") == "hidden":
        log("⚠️ [PERF] Tab đang ở nền (visibilityState=hidden): Chrome có thể giãn timer/animation của trang nên chậm hơn thao tác tay. "
            "Giữ cửa sổ profile hiển thị hoặc thử TAM_BRING_TO_FRONT=1 rồi so sánh PERF.")

    # --- 5) (tuỳ chọn, mặc định TẮT như bản cũ) nút phải sáng LIÊN TỤC ready_stable_ms trước khi bấm: chống giao diện chớp bật/tắt ---
    if CFG.ready_stable_ms > 0:
        st5, ready5 = _debounce_ready(driver, kill, ready, t_resp, wstats)
        trace.set("flaps", wstats.get("flaps"))
        if st5 == "killed":
            fsm.to(PState.ERROR, "Hard Stop trước khi bấm")
            return UploadResult(S.KILLED, "Hard Stop khi chờ nút Post ổn định (chưa bấm)", trace=trace, paths=paths)
        if st5 != "ready" or not ready5:
            fsm.to(PState.ERROR, "nút Post không ổn định")
            return UploadResult(S.FAILED_SAFE, f"nút Post chớp bật/tắt, không ổn định {CFG.ready_stable_ms}ms trong {CFG.ready_timeout_s:.0f}s (chưa bấm)",
                                trace=trace, paths=paths)
        ready = ready5

    # --- 5b) CHẠY THỬ (chỉ CLI --bench-upload): đã đo đến lúc nút Post sáng; DỪNG, không bấm, không đăng gì ---
    if ctx.dry_run:
        trace.mark_unknown("POST_CLICKED", "dry-run: không bấm")
        trace.mark_unknown("POST_CONFIRMED", "dry-run: không bấm")
        fsm.to(PState.ERROR, "dry-run: đã gắn file nhưng KHÔNG bấm Post — trang bẩn, việc sau sẽ nạp lại")
        log("🧪 [DRY-RUN] Nút Post đã sáng. KHÔNG bấm (chế độ đo) — không có video nào được đăng.")
        return UploadResult(S.DRY_RUN, "dry-run: không bấm Post", False, "", trace, paths)

    # --- 6) bấm Post (CDP, tin cậy). Chỉ thử lại khi CHẮC CHẮN chuột chưa trúng nút. ---
    conf = None
    t_c0 = time.perf_counter_ns()
    for attempt in range(CFG.click_attempts):
        if kill():
            fsm.to(PState.ERROR, "Hard Stop trước khi bấm")
            return UploadResult(S.KILLED, "Hard Stop trước khi bấm (chưa bấm)", trace=trace, paths=paths)
        if attempt > 0:
            try:
                driver.execute_script(JS_REARM)
            except Exception as e:
                raise JobError(f"không rearm được watcher: {e}")
            st2, ready = wait_post_ready(driver, kill, min(5.0, CFG.ready_timeout_s), wstats)
            if st2 != "ready" or not ready:
                fsm.to(PState.ERROR, "nút Post không sẵn sàng lại")
                return UploadResult(S.KILLED if st2 == "killed" else S.FAILED_SAFE,
                                    f"nút Post không sẵn sàng lại sau lần click trượt ({describe_blocked(conf)})", trace=trace, paths=paths)
        if hooks is not None and hooks.before_click is not None:
            if hooks.before_click() is False:
                fsm.to(PState.ERROR, "huỷ trước khi bấm")
                return UploadResult(S.FAILED_SAFE, "huỷ trước khi bấm (nhật ký chặn)", trace=trace, paths=paths)
        if attempt == 0:
            fsm.to(PState.POSTING, "phát lệnh click")
        x, y = float(ready["x"]), float(ready["y"])
        ctx.maybe_clicked = True
        t_c0 = time.perf_counter_ns()
        cdp_err = None
        try:
            cdp_click(driver, x, y)
        except Exception as e:
            cdp_err = e
        try:
            conf = driver.execute_script(JS_SNAPSHOT)          # đồng bộ, không dùng timer của trang
        except Exception as e:
            # không hỏi được trang sau khi đã phát lệnh: trang điều hướng (do click) hoặc trình duyệt mất => bảo thủ: ĐÃ bấm
            ctx.clicked = True
            ctx.nav = _is_nav_exc(e)
            conf = {"err": "not-installed" if ctx.nav else "query-failed"}
            break
        if conf is None:                                       # watcher biến mất ngay sau click => trang vừa điều hướng
            ctx.clicked = True
            ctx.nav = True
            conf = {"err": "not-installed"}
            break
        if payload_landed(conf) or (conf or {}).get("verdict"):
            ctx.clicked = True
            break
        # chuột KHÔNG chạm nút (bị chặn bởi bảo vệ lúc nhấn, nút tắt lại, hoặc sự kiện không tới trang) => chưa đăng => thử lại an toàn
        ctx.maybe_clicked = False
        log(f"⚠️ [Instant Click] Lần {attempt + 1}: chuột không trúng nút Post ({describe_blocked(conf)})"
            f"{'; CDP lỗi: ' + str(cdp_err) if cdp_err else ''}. Thử lại an toàn.")
    if not ctx.clicked:
        fsm.to(PState.ERROR, "click không trúng nút")
        return UploadResult(S.FAILED_SAFE, f"click không trúng nút Post sau {CFG.click_attempts} lần ({describe_blocked(conf)})", trace=trace, paths=paths)

    # --- 7) mốc POST_CLICKED (đồng hồ Chrome nếu có) ---
    if conf and conf.get("clickAt") is not None and conf.get("doc"):
        trace.mark_chrome("POST_CLICKED", conf["clickAt"], conf["doc"], sync)
    if trace.marks.get("POST_CLICKED") is None or (trace.marks["POST_CLICKED"].py_ns is None and trace.marks["POST_CLICKED"].chrome_ms is None):
        trace.mark("POST_CLICKED", t_c0, "py(dispatch)", overwrite=True)
    d = trace.durations().get("ReadyToClick", {})
    rtc = "UNKNOWN" if d.get("ns") is None else f"{d['ns'] / 1e6:.0f}ms"
    log(f"🚀 [INSTANT CLICK] READY→CLICK = {rtc} | Attach→Click = {time.perf_counter() - ctx.t_attached:.3f}s  (đã bấm; CHƯA phải đã đăng)")

    # --- 8) XÁC NHẬN bằng trạng thái giao diện. Không có bằng chứng => UNKNOWN, không bao giờ coi là thành công. ---
    out = await_confirm(driver, kill, pre_url, conf, t_c0, log)
    ctx.nav = ctx.nav or bool(out.get("nav"))
    st = out["status"]
    extra = out.get("extra") or {}
    if st == S.CONFIRMED:
        if out.get("at") is not None and out.get("doc"):
            trace.mark_chrome("POST_CONFIRMED", out["at"], out["doc"], sync)
        if trace.marks.get("POST_CONFIRMED") is None or (trace.marks["POST_CONFIRMED"].py_ns is None and trace.marks["POST_CONFIRMED"].chrome_ms is None):
            trace.mark("POST_CONFIRMED", src="py(detect)", overwrite=True)
        fsm.to(PState.CONFIRMED, out.get("evidence", ""))
        log(f"✅ [CONFIRMED] Đã đăng — bằng chứng giao diện: {out.get('evidence')}")
        return UploadResult(S.CONFIRMED, "", True, out.get("evidence", ""), trace, paths)
    if st == S.FAILED_VISIBLE:
        fsm.to(PState.ERROR, out.get("evidence", ""))
        log(f"❌ [FAILED] {out.get('evidence')} — KHÔNG tự đăng lại (nguy cơ trùng). Cần kiểm tra tay.")
        return UploadResult(S.FAILED_VISIBLE, out.get("evidence", ""), True, out.get("evidence", ""), trace, paths)
    reason = out.get("reason", "không xác minh được")
    fsm.to(PState.UNKNOWN, reason)
    snap = []
    if extra.get("urlAfterNav"):
        snap.append(f"URL sau điều hướng: {extra['urlAfterNav']}")
    if extra.get("urlOther"):
        snap.append(f"URL lạ: {extra['urlOther']}")
    if extra.get("newText"):
        snap.append("văn bản mới: " + " | ".join(str(t)[:60] for t in extra["newText"][-3:]))
    log(f"⚠️ [UNKNOWN] {reason}. " + ("; ".join(snap) + ". " if snap else "") +
        "Đã bấm Post nhưng KHÔNG xác minh được: không MOVE, không tự đăng lại. Kiểm tra tay trên TikTok Studio, "
        "rồi chỉnh TAM_CONFIRM_*_RE bằng --calibrate-confirm nếu đây là đăng thành công.")
    return UploadResult(S.UNKNOWN, reason, True, "; ".join(snap), trace, paths)


def upload_video_to_tiktok_ex(driver, video_paths, logger_func=None, check_force_kill=None, pid: str = "", mode: str = "",
                              trace: Optional[PerfTrace] = None, hooks: Optional[JobHooks] = None,
                              dry_run: bool = False) -> "UploadResult":
    """Đăng `video_paths` trong MỘT lần gắn file. KHÔNG BAO GIỜ ném lỗi; luôn trả UploadResult.

    CONFIRMED chỉ khi có bằng chứng giao diện. Lỗi trước khi bấm => FAILED_SAFE (retry được).
    Đã bấm mà không rõ => UNKNOWN. TikTok báo lỗi rõ => FAILED_VISIBLE. Hard Stop trước khi bấm => KILLED.
    dry_run=True (chỉ để đo, không dùng trong Scanner): dừng ngay trước khi bấm, trả DRY_RUN.
    """
    log = logger_func if logger_func else print
    kill = check_force_kill if check_force_kill else (lambda: False)
    paths = list(dict.fromkeys(video_paths or []))
    own_trace = trace is None
    if trace is None:
        trace = PerfTrace(label="direct", profile=str(pid), mode=mode)
    trace.n_files = len(paths)
    fsm = get_fsm(pid if pid else ("drv%x" % id(driver)))
    sync = ClockSync()
    trace.sync = sync
    ctx = _Ctx()
    ctx.dry_run = bool(dry_run)
    S = PostStatus
    result = None
    try:
        result = _upload_core(driver, paths, log, kill, str(pid), trace, fsm, sync, hooks, ctx)
    except JobError as e:
        if ctx.maybe_clicked or ctx.clicked:
            result = UploadResult(S.UNKNOWN, f"lỗi sau khi phát lệnh bấm: {e}", True, "", trace, paths)
            fsm.to(PState.UNKNOWN, str(e))
        else:
            result = UploadResult(S.FAILED_SAFE, str(e), False, "", trace, paths)
            fsm.to(PState.ERROR, str(e))
        log(f" - [TikTok] ❌ Lỗi upload ({result.status.value}): {e}")
    except Exception as e:
        if ctx.maybe_clicked or ctx.clicked:
            result = UploadResult(S.UNKNOWN, f"lỗi sau khi phát lệnh bấm: {type(e).__name__}: {e}", True, "", trace, paths)
            fsm.to(PState.UNKNOWN, str(e))
            log(f" - [TikTok] ⚠️ Lỗi SAU khi phát lệnh bấm => UNKNOWN (không tự đăng lại): {type(e).__name__}: {e}")
        else:
            result = UploadResult(S.FAILED_SAFE, f"{type(e).__name__}: {e}", False, "", trace, paths)
            fsm.to(PState.ERROR, str(e))
            log(f" - [TikTok] ❌ Lỗi upload: {type(e).__name__}: {e}")
    finally:
        if result is None:                 # BaseException (Ctrl+C...) — vẫn dọn
            result = UploadResult(S.UNKNOWN if (ctx.maybe_clicked or ctx.clicked) else S.FAILED_SAFE, "bị ngắt", ctx.clicked, "", trace, paths)
        trace.outcome = result.status.value
        trace.set("reason", result.reason)
        if hooks is not None and hooks.after_result is not None:
            try:
                hooks.after_result(result)
            except Exception:
                pass
        if not ctx.nav:
            dispose_watcher(driver)        # nhả "bảo vệ lúc nhấn"; tài liệu mới (sau điều hướng) vốn không có watcher
            if CFG.wait_mode == "lite":
                try:
                    driver.execute_script(JS_LITE_DISPOSE)
                except Exception:
                    pass
        if result.status in (S.KILLED, S.FAILED_SAFE) and fsm.state in (PState.ATTACHING, PState.UPLOADING, PState.PROCESSING, PState.POST_READY):
            fsm.to(PState.ERROR, result.reason)
        if result.confirmed:
            total = time.perf_counter() - ctx.t_start
            log(f"🚀 [Perf] ĐÃ XÁC NHẬN đăng. Thời gian lần đăng này: {total:.3f}s")
        if own_trace:
            if CFG.perf_enable:
                log(trace.render())
                PERF_LOG.write(trace.record())
    return result


def upload_video_to_tiktok(driver, video_paths, logger_func=None, check_force_kill=None):
    """Tương thích chữ ký cũ. Trả True CHỈ khi đã XÁC NHẬN đăng bằng giao diện (khác bản gốc: trả True ngay sau khi bấm)."""
    return upload_video_to_tiktok_ex(driver, video_paths, logger_func, check_force_kill).confirmed


# --- cầu nối cho bench/overhead.py (đo chi phí cơ chế chờ nút) ---
def bench_prepare(driver):
    install_watcher(driver)


def bench_wait_ready(driver):
    stats = {}      # type: Dict[str, Any]
    st, r = wait_post_ready(driver, lambda: False, 60.0, stats)
    return {"so_lat": stats.get("slices"), "so_poll": stats.get("polls"), "state": st}

# ======================================================================================
# PHẦN 6b — GHI THỤ ĐỘNG CHO AttachToReady (gắn file -> nút Post sáng)
# Chỉ QUAN SÁT: không bấm, không sửa DOM, không chặn/đổi yêu cầu mạng. Dùng cho --bench-attach (đăng tay vs tool)
# và (tuỳ chọn, TAM_REC=1) cho các việc đăng thật. Mọi con số lấy từ đồng hồ của TRANG nên đăng tay và tool so sánh được.
# ======================================================================================

JS_REC_INSTALL = r"""
return (function () {
  if (window.__tamRec && window.__tamRec.v === 1) return {ok: true, again: true, now: performance.now(), doc: window.__tamDoc || null};
  const R = {v: 1, ev: [], res: [], long: [], vis: [], dropped: 0, resErr: null, longErr: null};
  const NOW = function () { return Math.round(performance.now() * 10) / 10; };
  const strip = function (u) { try { const x = new URL(u, location.href); return x.host + x.pathname; } catch (e) { return String(u).slice(0, 100); } };
  const isFileIn = function (t) { return !!(t && t.tagName === 'INPUT' && t.type === 'file'); };
  const rel = function (t) { try { return !!(t && t.closest && t.closest("input[type=file],label,button")); } catch (e) { return false; } };
  ['pointerdown', 'click', 'input', 'change', 'drop', 'dragenter'].forEach(function (type) {
    document.addEventListener(type, function (e) {
      if (R.ev.length >= 80) return;
      const t = e.target;
      const f = isFileIn(t);
      if ((type === 'input' || type === 'change') && !f) return;
      if ((type === 'click' || type === 'pointerdown') && !(f || rel(t))) return;
      let n = null, sz = null;
      if (f && t.files) { n = t.files.length; sz = 0; for (let i = 0; i < t.files.length; i++) sz += t.files[i].size; }
      R.ev.push({t: NOW(), type: type, trusted: !!e.isTrusted, tgt: (t && t.tagName ? t.tagName.toLowerCase() + (t.type ? ':' + t.type : '') : '?'),
                 n: n, size: sz, ua: (navigator.userActivation ? !!navigator.userActivation.hasBeenActive : null)});
    }, true);
  });
  try {
    new PerformanceObserver(function (l) {
      l.getEntries().forEach(function (e) {
        if (R.res.length >= 300) { R.dropped++; return; }
        R.res.push({u: strip(e.name), i: e.initiatorType, s: Math.round(e.startTime), e: Math.round(e.responseEnd), d: Math.round(e.duration),
                    tx: e.transferSize | 0, eb: e.encodedBodySize | 0, p: e.nextHopProtocol || ''});
      });
    }).observe({type: 'resource'});
  } catch (err) { R.resErr = String(err); }
  try {
    new PerformanceObserver(function (l) {
      l.getEntries().forEach(function (e) { if (R.long.length < 300) R.long.push([Math.round(e.startTime), Math.round(e.duration)]); });
    }).observe({type: 'longtask'});
  } catch (err) { R.longErr = String(err); }
  const vis = function () { if (R.vis.length < 60) R.vis.push([NOW(), document.visibilityState, (document.hasFocus ? document.hasFocus() : null)]); };
  document.addEventListener('visibilitychange', vis, true); window.addEventListener('focus', vis, true); window.addEventListener('blur', vis, true);
  vis();
  window.__tamRec = R;
  return {ok: true, now: performance.now(), doc: window.__tamDoc || null};
})();
"""

JS_REC_READ = r"""
const R = window.__tamRec; if (!R) return null;
return {ev: R.ev, res: R.res, long: R.long, vis: R.vis, dropped: R.dropped, resErr: R.resErr, longErr: R.longErr, now: performance.now()};
"""

JS_ENV = r"""
const c = navigator.connection;
return {ua: navigator.userAgent, hc: navigator.hardwareConcurrency, mem: (navigator.deviceMemory || null), wd: navigator.webdriver,
        dpr: window.devicePixelRatio, vis: document.visibilityState, focus: (document.hasFocus ? document.hasFocus() : null),
        w: window.innerWidth, h: window.innerHeight, rs: document.readyState, age: performance.now(),
        conn: c ? {et: c.effectiveType, dl: c.downlink, rtt: c.rtt} : null};
"""

# Chế độ "sparse": KHÔNG cài observer nào; chỉ một listener change (bắt thời điểm chọn file) và hỏi thưa theo chu kỳ.
JS_SPARSE_INSTALL = r"""
if (!window.__tamSp) {
  const S = {attachedAt: null};
  document.addEventListener('change', function (e) { const t = e.target; if (S.attachedAt === null && t && t.tagName === 'INPUT' && t.type === 'file' && t.files && t.files.length) S.attachedAt = performance.now(); }, true);
  window.__tamSp = S;
}
if (!window.__tamDoc) window.__tamDoc = Math.random().toString(36).slice(2) + Date.now().toString(36);
return {ok: true, now: performance.now(), doc: window.__tamDoc};
"""

JS_SPARSE_POLL = r"""
const S = window.__tamSp; const b = document.querySelector(arguments[0]);
let en = false;
if (b) { const dis = b.disabled || b.hasAttribute('disabled') || b.getAttribute('aria-disabled') === 'true'; const r = b.getBoundingClientRect(); en = !dis && r.width > 0 && r.height > 0; }
return [performance.now(), en, S ? S.attachedAt : null, document.visibilityState];
"""

JS_PAGE_AGE = r"""return [performance.now(), document.readyState];"""

_CDP_KEYS = ("TaskDuration", "ScriptDuration", "LayoutDuration", "RecalcStyleDuration", "LayoutCount", "RecalcStyleCount", "Nodes", "JSHeapUsedSize", "V8CompileDuration")


def apply_focus_emu(driver) -> bool:
    """TAM_FOCUS_EMU=1: tab bị che/chạy nền vẫn được Chrome chạy như tab hiển thị (timer không bị bóp, trang thấy visibilityState=visible).
    Đây là lệnh CDP tiêu chuẩn của công cụ tự động hoá; CÓ đổi những gì trang nhìn thấy về focus/visibility => mặc định TẮT, bạn tự quyết sau khi A/B."""
    if not CFG.focus_emu or getattr(driver, "_tam_focus_emu", False):
        return bool(getattr(driver, "_tam_focus_emu", False))
    try:
        driver.execute_cdp_cmd("Emulation.setFocusEmulationEnabled", {"enabled": True})
        driver._tam_focus_emu = True
        return True
    except Exception:
        return False


def cdp_metrics(driver) -> Optional[Dict[str, float]]:
    """Số đo CỘNG DỒN của renderer (Performance.getMetrics). Dùng hiệu hai lần đo; None nếu không đọc được."""
    try:
        if not getattr(driver, "_tam_perf_on", False):
            driver.execute_cdp_cmd("Performance.enable", {})
            driver._tam_perf_on = True
        r = driver.execute_cdp_cmd("Performance.getMetrics", {})
        m = {x["name"]: float(x["value"]) for x in (r.get("metrics") or [])}
        return {k: m[k] for k in _CDP_KEYS if k in m}
    except Exception:
        return None


def cdp_delta(m0: Optional[Dict[str, float]], m1: Optional[Dict[str, float]]) -> Dict[str, Any]:
    out = {}   # type: Dict[str, Any]
    if not m0 or not m1:
        return out
    for k in m0:
        if k in m1:
            d = m1[k] - m0[k]
            if k.endswith("Duration"):
                if d < 0:
                    continue                           # có điều hướng giữa hai lần đo => bộ đếm đã đặt lại, bỏ
                out["x_" + k[:-8].lower() + "_ms"] = round(d * 1000.0, 1)
            elif k.endswith("Count"):
                if d < 0:
                    continue
                out["x_" + k[:-5].lower() + "_count"] = int(d)
            elif k == "Nodes":
                out["x_nodes_delta"] = int(d)
            elif k == "JSHeapUsedSize":
                out["x_heap_delta_kb"] = round(d / 1024.0, 1)
    return out


def rec_install(driver) -> Dict[str, Any]:
    try:
        r = driver.execute_script(JS_REC_INSTALL)
        return r if isinstance(r, dict) else {}
    except Exception:
        return {}


def rec_analyze(rec: Optional[Dict[str, Any]], attached_at: Optional[float], ready_at: Optional[float]) -> Dict[str, Any]:
    """Rút gọn dữ liệu ghi thụ động thành các cờ x_* (số) và rec_* (cấu trúc nhỏ). Không đoán: thiếu tín hiệu => không có khoá."""
    out = {}   # type: Dict[str, Any]
    if not rec:
        out["rec_error"] = "không đọc được bộ ghi"
        return out
    ev = rec.get("ev") or []
    out["rec_events"] = ev[:12]
    ch = [e for e in ev if e.get("type") == "change" and (e.get("n") or 0) > 0]
    if ch:
        out["x_change_trusted"] = 1 if ch[0].get("trusted") else 0
        out["x_change_useractivation"] = (1 if ch[0].get("ua") else 0) if ch[0].get("ua") is not None else None
        if out["x_change_useractivation"] is None:
            out.pop("x_change_useractivation")
        out["x_attach_bytes"] = ch[0].get("size")
    if attached_at is None and ch:
        attached_at = ch[0].get("t")
    lo = (attached_at if attached_at is not None else 0) - 50.0
    hi = (ready_at if ready_at is not None else float("inf"))
    # tác vụ dài (>50 ms) của luồng chính trong cửa sổ attach -> ready
    lt = [x for x in (rec.get("long") or []) if x[0] >= lo and x[0] <= hi]
    if rec.get("longErr") is None:
        out["x_longtask_count"] = len(lt)
        out["x_longtask_ms"] = int(sum(x[1] for x in lt))
        out["x_longtask_max_ms"] = int(max([x[1] for x in lt] or [0]))
    # ẩn/hiện và focus trong cửa sổ
    vis = [v for v in (rec.get("vis") or []) if v[0] >= lo - 1e9]
    out["rec_vis"] = vis[-8:]
    out["x_hidden_any"] = 1 if any(v[1] != "visible" for v in vis) else 0
    # yêu cầu mạng (Resource Timing): chỉ những yêu cầu bắt đầu sau lúc gắn file
    res = [r for r in (rec.get("res") or []) if attached_at is None or r.get("s", 0) >= lo]
    cand = sorted([r for r in res if r.get("d", 0) >= 150], key=lambda r: -r.get("d", 0))[:6]
    out["rec_res_top"] = [{"url": r["u"], "init": r["i"], "start_off_ms": (None if attached_at is None else round(r["s"] - attached_at)), "dur_ms": r["d"],
                           "bytes_out_hint": r.get("eb"), "proto": r.get("p")} for r in cand]
    out["x_res_count"] = len(res)
    if rec.get("resErr"):
        out["rec_res_error"] = rec["resErr"]
    pat = CFG.upload_url_re
    if pat:
        try:
            rx = re.compile(pat, re.I)
            mt = [r for r in res if rx.search(r["u"])]
        except re.error:
            mt = []
            out["rec_upload_re_error"] = "TAM_UPLOAD_URL_RE không phải regex hợp lệ"
        if mt:
            s0 = min(r["s"] for r in mt)
            e1 = max(r["e"] for r in mt)
            out["x_net_requests"] = len(mt)
            out["x_net_busy_ms"] = int(sum(r["d"] for r in mt))
            out["rec_upload_end_page_ms"] = e1
            out["x_net_span_ms"] = int(e1 - s0)
            if attached_at is not None:
                out["x_net_first_after_attach_ms"] = int(s0 - attached_at)
            if ready_at is not None:
                out["x_after_net_to_ready_ms"] = int(ready_at - e1)       # âm => upload còn chạy khi nút đã sáng: KHÔNG dùng làm TikTokProcessing
    return out


# ======================================================================================
# PHẦN 9 — QT (PySide6) + LoggerQt
# Bản gốc raise RuntimeError ngay khi import nếu thiếu PySide6. Ở đây thiếu PySide6 vẫn nạp được phần backend
# (để chạy --selftest/--bench ở máy không có giao diện); GUI vẫn cần PySide6 và sẽ báo lỗi rõ ràng khi khởi tạo.
# ======================================================================================
try:
    from PySide6.QtWidgets import (
        QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
        QFileDialog, QTextEdit, QListWidget, QListWidgetItem, QLineEdit, QSpinBox,
        QComboBox, QFrame, QMessageBox, QProgressBar, QMenu
    )
    from PySide6.QtGui import QFont, QColor
    from PySide6.QtCore import Qt, QTimer, Signal, QObject
    HAS_QT = True
except Exception as _qt_err:
    HAS_QT = False
    QApplication = None
    QTextEdit = object

    class QWidget(object):                  # chỉ để các class bên dưới vẫn định nghĩa được
        def __init__(self, *a, **k):
            raise RuntimeError("PySide6 is required. Install via: pip install PySide6")

    class QObject(object):
        pass

    def Signal(*a, **k):                    # noqa: N802  (giữ tên của Qt)
        return None

    QVBoxLayout = QHBoxLayout = QPushButton = QLabel = QFileDialog = QListWidget = QListWidgetItem = None
    QLineEdit = QSpinBox = QComboBox = QFrame = QMessageBox = QProgressBar = QFont = QColor = Qt = QTimer = QMenu = None


class LoggerQt:
    def __init__(self, text_widget: QTextEdit, interval_ms: int = 100):
        self.t = text_widget
        self.q = queue.Queue()
        self._timer = QTimer()
        self._timer.setInterval(80)
        self._timer.timeout.connect(self._drain)

    def start(self):
        self._timer.start()

    def stop(self):
        try:
            self._timer.stop()
        except Exception:
            pass

    def log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        try:
            self.q.put(f"[{ts}] {msg}")
        except Exception:
            try:
                self.t.append(f"[{ts}] {msg}")
            except Exception:
                pass

    def _drain(self):
        try:
            # tối đa 200 dòng mỗi nhịp để giao diện không bị khựng khi log dồn dập
            for _ in range(200):
                line = self.q.get_nowait()
                self.t.append(line)
        except queue.Empty:
            pass


# ======================================================================================
# PHẦN 10 — CÔNG VIỆC THEO PROFILE (dùng bởi Scanner)
#   * "xong" chỉ được log SAU KHI mọi video của profile đã POST_CONFIRMED (GUI dùng dòng này để AUTO-CLOSE profile).
#   * Retry 1 lần như bản gốc, nhưng CHỈ phần chưa xác nhận và CHỈ khi lỗi xảy ra TRƯỚC khi bấm Post.
#   * Giữa hai video/lô: nạp trang Upload và chờ ô nhập file có mặt (điều kiện thật) thay cho `sleep 5 s`.
# ======================================================================================
class FolderCtx:
    """Thông tin của MỘT lần xử lý thư mục, dùng chung cho các worker."""

    def __init__(self, sub: str, t_detected_ns: int, info: Dict[str, Dict[str, Any]], keys: Dict[str, Optional[str]]):
        self.sub = sub
        self.t_detected_ns = t_detected_ns
        self.info = info             # path -> {"mtime_ns":..., "moved_ns":..., "marker_ns":...}
        self.keys = keys             # path -> journal key
        self.prewarm = False         # còn file dư (giới hạn video/profile) => nạp sẵn trang cho lượt sau
        self.n_profiles = 0          # số profile cùng đăng trong lượt này (ghi vào PERF để so 1 profile vs nhiều profile)


class _ProfileWork:
    # ---- PERF ----
    def _new_trace(self, ppid: str, mode: str, files: List[str], ctx: FolderCtx, index: int, stage_t: Tuple[int, int]) -> PerfTrace:
        n_bytes = 0
        for f in files:
            st = stat_file(f)
            if st:
                n_bytes += st[0]
        tr = PerfTrace(label=os.path.basename(ctx.sub), profile=ppid, mode=mode, n_files=len(files), n_bytes=n_bytes)
        mts = [ctx.info.get(f, {}).get("mtime_ns") for f in files]
        mts = [m for m in mts if m]
        moved = [ctx.info.get(f, {}).get("moved_ns") for f in files]
        moved = [m for m in moved if m]
        marker = [ctx.info.get(f, {}).get("marker_ns") for f in files]
        marker = [m for m in marker if m]
        tr.mark_file_ready(max(mts) if mts else None, max(moved) if moved else None, max(marker) if marker else None)
        tr.mark("FILE_DETECTED", ctx.t_detected_ns, "py(scan)")
        tr.mark("STAGE_START", stage_t[0], "py")
        tr.mark("STAGE_END", stage_t[1], "py")
        tr.set("index_in_chunk", index)
        tr.set("stage_mode", CFG.stage)
        tr.set("variant", CFG.variant)
        tr.set("concurrency", ctx.n_profiles)
        return tr

    def _emit_perf(self, tr: PerfTrace):
        if not CFG.perf_enable:
            return
        try:
            self.logger(tr.render())
            PERF_LOG.write(tr.record())
        except Exception:
            pass

    # ---- nhật ký chống đăng trùng ----
    def _make_hooks(self, ppid: str, orig_files: List[str], ctx: FolderCtx) -> JobHooks:
        items = [(ctx.keys.get(p), p) for p in orig_files]
        state = {"posting": False}

        def before_click():
            JOURNAL.record("POSTING", items, pid=ppid)            # flush, không fsync => không tốn thời gian ở đường găng
            state["posting"] = True
            return True

        def after_result(res: UploadResult):
            if res.status == PostStatus.CONFIRMED:
                JOURNAL.record("CONFIRMED", items, pid=ppid, durable=True, extra={"evidence": res.evidence})
            elif res.status == PostStatus.UNKNOWN:
                JOURNAL.record("UNKNOWN", items, pid=ppid, durable=True, extra={"reason": res.reason})
            elif res.status == PostStatus.FAILED_VISIBLE:
                JOURNAL.record("FAILED_VISIBLE", items, pid=ppid, durable=True, extra={"reason": res.reason})
            elif state["posting"]:
                # đã ghi POSTING nhưng chắc chắn CHƯA bấm trúng nút => gỡ khoá
                JOURNAL.record("NOT_POSTED", items, pid=ppid, durable=True, extra={"reason": res.reason})

        h = JobHooks(before_click, after_result)
        h.state = state                                     # type: ignore[attr-defined]  (để biết đã ghi POSTING hay chưa)
        return h

    def _upload_once(self, ppid: str, drv, orig_files: List[str], staged_files: List[str], mode: str, ctx: FolderCtx,
                     stage_t: Tuple[int, int], index: int) -> UploadResult:
        tr = self._new_trace(ppid, mode, orig_files, ctx, index, stage_t)
        hooks = self._make_hooks(ppid, orig_files, ctx)
        try:
            res = upload_video_to_tiktok_ex(drv, staged_files, logger_func=self.logger, check_force_kill=self._is_killed,
                                            pid=ppid, mode=mode, trace=tr, hooks=hooks)
        except Exception as e:                        # upload_video_to_tiktok_ex không ném lỗi; phòng hờ
            self.logger(f"❌ Lỗi upload {ppid}: {e}")
            wrote_posting = bool(getattr(hooks, "state", {}).get("posting"))
            res = UploadResult(PostStatus.UNKNOWN if wrote_posting else PostStatus.FAILED_SAFE,
                               f"{type(e).__name__}: {e}", wrote_posting, "", tr, orig_files)
            if wrote_posting:
                JOURNAL.record("UNKNOWN", [(ctx.keys.get(p), p) for p in orig_files], pid=ppid, durable=True, extra={"reason": res.reason})
        res.paths = list(orig_files)
        return res

    # ---- một lượt cho một profile ----
    def _attempt_profile(self, ppid: str, drv, mode: str, files: List[str], ctx: FolderCtx, tag: str) -> Dict[str, Any]:
        res = {"status": "FAILED_SAFE", "reason": "", "confirmed": []}     # type: Dict[str, Any]
        if not files:
            res["status"] = "OK"
            return res
        fsm = get_fsm(ppid)
        lock = get_profile_lock(ppid)
        self.logger(f"⚡ {ppid}: Stage ({'RAM/HARDLINK' if CFG.stage == 'ram' else 'NO-COPY'}) {len(files)} files...")
        t_s0 = time.perf_counter_ns()
        staged, tmp, _sinfo = stage_files(files, prefix=f"{tag.strip().lower() or 'pid'}_{ppid}", trace=None, check_kill=self._is_killed)
        t_s1 = time.perf_counter_ns()
        if len(staged) != len(files):
            cleanup_stage_dir(tmp)
            res["status"] = "KILLED"
            res["reason"] = "Hard Stop khi stage"
            return res
        try:
            with lock:
                if self.force_kill:
                    res["status"] = "KILLED"
                    return res
                if mode == "CUM":
                    if CUM_BATCH and CUM_BATCH > 0:
                        self.logger(f"➡️ {tag}{ppid} (CỤM-BATCH={CUM_BATCH}): Đăng {len(files)} video.")
                        batches = [list(range(i, min(i + CUM_BATCH, len(files)))) for i in range(0, len(files), CUM_BATCH)]
                    else:
                        self.logger(f"➡️ {tag}{ppid} (CỤM): Đăng {len(files)} video.")
                        batches = [list(range(len(files)))]
                    total_b = len(batches)
                    for bi, idxs in enumerate(batches):
                        if self.force_kill:
                            res["status"] = "KILLED"
                            return res
                        if total_b > 1:
                            self.logger(f"   • {ppid}: Batch {bi + 1}/{total_b} ({len(idxs)} video)")
                        r = self._upload_once(ppid, drv, [files[i] for i in idxs], [staged[i] for i in idxs], mode, ctx,
                                              (t_s0, t_s1) if bi == 0 else (t_s1, t_s1), bi)
                        if not r.confirmed:
                            self._emit_perf(r.trace)
                            res["status"], res["reason"] = r.status.value, r.reason
                            return res
                        res["confirmed"].extend(r.paths)
                        last = (bi == total_b - 1)
                        if (not last) or ctx.prewarm:
                            ok_next = prepare_upload_page(drv, self.logger, self._is_killed, fsm, r.trace, tag=f"{ppid}: ")
                            if not ok_next and not last:
                                self._emit_perf(r.trace)
                                res["status"], res["reason"] = "FAILED_SAFE", "không nạp lại được trang Upload cho lô kế tiếp"
                                return res
                        self._emit_perf(r.trace)
                else:                                                        # TT: tuần tự từng video
                    self.logger(f"➡️ {tag}{ppid} (TUẦN TỰ): Bắt đầu {len(files)} video...")
                    for i, (orig, st) in enumerate(zip(files, staged)):
                        if self.force_kill:
                            res["status"] = "KILLED"
                            return res
                        self.logger(f" - {ppid}: Đăng {i + 1}/{len(files)}: {os.path.basename(orig)}")
                        r = self._upload_once(ppid, drv, [orig], [st], mode, ctx, (t_s0, t_s1) if i == 0 else (t_s1, t_s1), i)
                        if not r.confirmed:
                            self._emit_perf(r.trace)
                            res["status"], res["reason"] = r.status.value, r.reason
                            self.logger(f"❌ {ppid} lỗi upload {os.path.basename(orig)}. Dừng. ({r.status.value}: {r.reason})")
                            return res
                        res["confirmed"].append(orig)
                        last = (i == len(files) - 1)
                        if (not last) or ctx.prewarm:
                            ok_next = prepare_upload_page(drv, self.logger, self._is_killed, fsm, r.trace, tag=f"{ppid}: ")
                            if not ok_next and not last:
                                self._emit_perf(r.trace)
                                res["status"], res["reason"] = "FAILED_SAFE", "không nạp lại được trang Upload cho video kế tiếp"
                                self.logger(f"❌ {ppid} lỗi nạp lại trang Upload. Dừng tuần tự.")
                                return res
                        self._emit_perf(r.trace)
                res["status"] = "OK"
                return res
        finally:
            cleanup_stage_dir(tmp)
            try:
                if fsm.state == PState.CONFIRMED:
                    fsm.to(PState.CLEANUP, "dọn stage")
                    fsm.to(PState.READY, "xong lượt")
            except Exception:
                pass

    def _run_profile(self, ppid, chunk: List[str], ctx: FolderCtx, results: Dict[str, Dict[str, Any]]):
        """Chạy trong thread riêng cho từng profile. Ghi kết quả vào results[ppid]."""
        ppid = str(ppid)
        out = {"ok": False, "status": "FAILED_SAFE", "reason": "", "confirmed": []}     # type: Dict[str, Any]
        results[ppid] = out
        try:
            if self.force_kill:
                out["status"] = "KILLED"
                self.logger(f"🛑 {ppid} ĐÃ BỊ DỪNG KHẨN CẤP.")
                return
            with drivers_lock:
                drv = drivers.get(ppid)
            if drv is None:
                self.logger(f"⚠️ Profile {ppid} chưa mở. Bỏ qua.")
                out["reason"] = "profile chưa mở"
                return
            mode = self.upload_modes.get(ppid, "CUM")
            remaining = list(chunk)
            for attempt in (0, 1):
                tag = "" if attempt == 0 else "RETRY "
                r = self._attempt_profile(ppid, drv, mode, remaining, ctx, tag)
                out["confirmed"].extend(r["confirmed"])
                cset = set(r["confirmed"])
                remaining = [p for p in remaining if p not in cset]
                out["status"], out["reason"] = r["status"], r["reason"]
                if r["status"] == "OK":
                    out["ok"] = True
                    break
                if r["status"] != "FAILED_SAFE" or self.force_kill or attempt == 1 or not remaining:
                    break
                self.logger(f"🔁 Retry 1 lần cho: {ppid} (còn {len(remaining)} file chưa đăng; lỗi trước khi bấm Post nên an toàn): {r['reason']}")
            if out["ok"] and not self.force_kill:
                write_upload_done(self.excel_path, ctx.sub, ppid, "DONE")          # stub có chủ đích; CHỈ gọi sau khi đã CONFIRMED
                self.logger(f"✅ {'Retry ' if out['status'] == 'OK' and attempt == 1 else ''}{ppid} xong.")
            else:
                out["ok"] = False
                if self.force_kill or out["status"] == "KILLED":
                    self.logger(f"🛑 {ppid} ĐÃ BỊ DỪNG KHẨN CẤP.")
                elif out["status"] in ("UNKNOWN", "FAILED_VISIBLE"):
                    self.logger(f"❌ {ppid} lỗi upload: {out['status']} — {out['reason']}. KHÔNG tự đăng lại; "
                                f"đã khoá file trong nhật ký {os.path.basename(CFG.journal_path)}.")
                else:
                    self.logger(f"❌ {ppid} lỗi upload. ({out['reason']})")
        except Exception as e:
            out["ok"] = False
            out["reason"] = f"{type(e).__name__}: {e}"
            self.logger(f"❌ Lỗi worker {ppid}: {e}")

    # ---- KEEP-ALIVE: nạp lại trang Upload sau MOVE, các profile SONG SONG ----
    def _reload_profiles_after_move(self, profile_ids) -> bool:
        """KEEP-ALIVE only: reload sau MOVE và chỉ trả về khi trang Upload READY (điều kiện thật, không sleep cố định)."""
        if not self.keep_alive:
            return True
        pids = [str(p) for p in profile_ids]
        results = {}        # type: Dict[str, bool]

        def one(pid: str):
            if self.force_kill or not self.running:
                results[pid] = False
                return
            with get_profile_lock(pid):          # cùng lock profile để reload không chen ngang một upload khác
                if self.force_kill or not self.running:
                    results[pid] = False
                    return
                with drivers_lock:
                    drv = drivers.get(pid)
                if drv is None:
                    self.logger(f"❌ [KEEP-ALIVE] Không tìm thấy driver Profile {pid} sau khi MOVE. Dừng quét để tránh đăng trùng.")
                    results[pid] = False
                    return
                try:
                    self.logger(f"♻️ [KEEP-ALIVE] MOVE đã hoàn tất. Đang load lại trang Upload cho Profile {pid}...")
                    ok = prepare_upload_page(drv, self.logger, self._is_killed, get_fsm(pid), None, tag=f"[KEEP-ALIVE] {pid}: ")
                    if not ok:
                        raise RuntimeError("không thấy chỗ nạp file")
                    self.logger(f"🔄 [KEEP-ALIVE] Profile {pid} đã load lại trang Upload và READY. Tiếp tục quét.")
                    results[pid] = True
                except Exception as e:
                    self.logger(f"❌ [KEEP-ALIVE] Reload Profile {pid} không READY: {e}. Dừng quét để tránh đăng trùng.")
                    results[pid] = False

        if CFG.reload_parallel and len(pids) > 1:
            ths = [threading.Thread(target=one, args=(p,), daemon=True) for p in pids]
            for t in ths:
                t.start()
            for t in ths:
                t.join()
        else:
            for p in pids:
                one(p)
                if not results.get(p):
                    break
        return bool(pids) and all(results.get(p, False) for p in pids)

    def _release_file_handles(self, profile_ids):
        """MOVE gặp khoá: điều hướng các profile rời form upload (Chrome nhả handle file vừa upload) rồi MOVE thử lại."""
        def one(pid):
            with drivers_lock:
                drv = drivers.get(str(pid))
            if drv is not None:
                try:
                    safe_get(drv, TARGET_UPLOAD_URL)
                except Exception:
                    pass
        ths = [threading.Thread(target=one, args=(p,), daemon=True) for p in profile_ids]
        for t in ths:
            t.start()
        for t in ths:
            t.join(timeout=15)


# ======================================================================================
# PHẦN 11 — SCANNER (quét theo sự kiện, không sleep cố định, MOVE chỉ sau khi đã XÁC NHẬN)
#
# So với bản gốc:
#   * Bỏ `listdir -> sleep(DOUBLESCAN_WAIT) -> listdir` cho MỌI thư mục (kể cả thư mục rỗng, mỗi vòng quét): thay bằng
#     cổng "file ghi xong" (FileGate): file xong từ lâu => sẵn sàng ngay; đang ghi => chờ đúng phần còn thiếu.
#   * Thức dậy theo sự kiện watchdog (created/moved/modified) và đúng lúc file đủ ổn định; SCAN_INTERVAL chỉ là lưới an toàn.
#   * Không bỏ rơi file khi dùng "giới hạn video/profile"; chỉ MOVE khi MỌI video trong thư mục đã CONFIRMED.
#   * Kết quả lỗi/UNKNOWN: không MOVE, không retry mù; folder bị bỏ qua trong phiên (giống bản gốc) và nằm trong nhật ký.
# ======================================================================================
class Scanner(_ProfileWork):
    MIN_PASS_S = 0.05            # khoảng cách tối thiểu giữa hai lượt quét khi bị đánh thức dồn dập (file đang ghi)

    def __init__(self, excel_path, base_folder, logger_func, progress_bar, limit_callable, open_folder_after_var, pids_list,
                 done_folder_path, upload_modes, keep_alive=False):
        self.excel_path = excel_path
        self.base_folder = base_folder
        self.logger = logger_func
        self.progress = progress_bar
        self.limit_callable = limit_callable
        self.open_folder_after_var = open_folder_after_var
        self.running = False

        self.force_kill = False

        self.processed_dirs = set()
        self.processing_now = set()
        self._processing_lock = threading.Lock()
        self.pids_list = pids_list
        self.done_folder_path = done_folder_path
        self.upload_modes = upload_modes
        # Chỉ bật chuỗi MOVE -> RELOAD READY -> RESUME khi GUI tích KEEP-ALIVE. Nhánh không tích giữ nguyên hành vi cũ.
        self.keep_alive = bool(keep_alive)

        self.gate = FileGate()
        self.arrivals = {}                       # type: Dict[str, Tuple[str, int]]
        self._arr_lock = threading.Lock()
        self._wait_log = {}                      # type: Dict[str, float]
        self.wake_event = threading.Event()
        self.observer = None
        if Observer:
            scanner = self

            class Watcher(FileSystemEventHandler):
                def on_any_event(event_self, event):
                    try:
                        if event.event_type in ("created", "moved") and not getattr(event, "is_directory", False):
                            dest = getattr(event, "dest_path", "") or event.src_path
                            with scanner._arr_lock:
                                scanner.arrivals[os.path.normpath(str(dest))] = (event.event_type, time.perf_counter_ns())
                    except Exception:
                        pass
                    scanner.wake_event.set()
            self.observer = Observer()
            self.observer.schedule(Watcher(), self.base_folder, recursive=True)

    def start(self):
        self.running = True
        self.force_kill = False
        JOURNAL.load()
        if self.observer:
            try:
                self.observer.start()
            except Exception as e:
                self.logger(f"⚠️ Watchdog khởi động lỗi: {e}")
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self.running = False
        self.force_kill = True
        self.wake_event.set()
        if self.observer:
            try:
                self.observer.stop()
                self.observer.join(timeout=1)
            except Exception:
                pass

    def _is_killed(self):
        return self.force_kill

    # ---- tiện ích tiến độ (GUI gốc truyền QProgressBar; lỗi giao diện không được làm hỏng vòng quét) ----
    def _progress_max(self, n):
        if self.progress:
            try:
                self.progress.setMaximum(max(1, n))
            except Exception:
                pass

    def _progress_val(self, v):
        if self.progress:
            try:
                self.progress.setValue(v)
            except Exception:
                pass

    def _log_waiting(self, sub: str, fv: "FolderView"):
        now = time.time()
        if now - self._wait_log.get(sub, 0.0) < 10.0:
            return
        self._wait_log[sub] = now
        if not fv.marker_ok:
            self.logger(f"⏳ {os.path.basename(sub)}: chờ file đánh dấu hoàn tất '{CFG.require_marker}'.")
            return
        first = fv.pending[0] if fv.pending else ""
        self.logger(f"⏳ {os.path.basename(sub)}: chờ {len(fv.pending)}/{len(fv.videos)} video ghi xong "
                    f"({os.path.basename(first)}: {fv.reasons.get(first, '')}).")

    def _stop_scanning(self, sub: str, msg: str):
        with self._processing_lock:
            self.processing_now.discard(sub)
        self.running = False                    # đặt TRƯỚC khi ghi nhật ký: GUI nhận tín hiệu "DỪNG QUÉT" sẽ thấy running=False (không đua)
        self.wake_event.set()
        self.logger(msg)

    # ---- xử lý MỘT thư mục. Trả (hành động, gợi ý chờ giây) ----
    def _process_folder(self, sub: str, pids: List[str]) -> Tuple[str, Optional[float]]:
        name = os.path.basename(sub)
        fv = evaluate_folder(sub, self.gate)
        if not fv.videos:
            # KHÔNG đánh dấu processed: chờ file tải xong, watchdog sẽ quét lại (giữ đúng bản vá của bản gốc)
            return "empty", None
        t_detected = time.perf_counter_ns()
        if not fv.all_ready:
            self._log_waiting(sub, fv)
            return "wait", fv.next_in_s
        self._wait_log.pop(sub, None)

        # --- nhật ký: file nào đã đăng (CONFIRMED), file nào nghi đã đăng (khoá), file nào cần đăng ---
        keys = {p: JOURNAL.file_key(p) for p in fv.ready}
        todo, done_before, blocked = [], [], []
        for p in fv.ready:
            st = JOURNAL.status(keys[p])
            if st == "CONFIRMED":
                done_before.append(p)
            elif st in PostJournal.BLOCKING:
                blocked.append((p, st))
            else:
                todo.append(p)
        if blocked:
            self.logger(f"🛑 [JOURNAL] {name}: {len(blocked)} file đang bị KHOÁ vì nghi đã đăng ({blocked[0][1]}: {os.path.basename(blocked[0][0])}...). "
                        f"Không tự đăng lại. Kiểm tra tay trên TikTok rồi gỡ khoá bằng: python tam_INSTANT_POST.py --journal-resolve")
            return "blocked", None
        if done_before and todo:
            self.logger(f"ℹ️ {name}: bỏ qua {len(done_before)} video đã CONFIRMED trong nhật ký (chống đăng trùng).")

        leftover = []                       # type: List[str]
        participating = []                  # type: List[str]
        results = {}                        # type: Dict[str, Dict[str, Any]]
        if todo:
            active = [p for p in pids if get_fsm(p).state != PState.UNKNOWN]
            for p in pids:
                if p not in active:
                    self.logger(f"⚠️ Profile {p} đang UNKNOWN (chưa xác minh được lần đăng trước) — bỏ khỏi phân phối.")
            if not active:
                return "stop_all_unknown", None
            self.logger(f"📁 {sub} -> Phân phối cho {len(active)} profile.")
            try:
                per_profile_limit = int(self.limit_callable())
            except Exception:
                per_profile_limit = 0
            plan, leftover = plan_distribution_ex(todo, active, per_profile_limit=per_profile_limit)
            self.logger(f"🔢 Phân phối {len(todo)} file -> {[len(chunk) for _, chunk in plan]}")
            if leftover:
                self.logger(f"📦 Giới hạn {per_profile_limit} video/profile: còn {len(leftover)} file sẽ đăng ở lượt sau "
                            f"(bản cũ bỏ rơi các file này rồi vẫn MOVE folder).")
            info = {}
            for p in todo:
                st = stat_file(p)
                with self._arr_lock:
                    arr = self.arrivals.get(os.path.normpath(p))
                info[p] = {"mtime_ns": st[1] if st else None,
                           "moved_ns": arr[1] if (arr and arr[0] == "moved") else None,
                           "marker_ns": fv.marker_mtime_ns}
            ctx = FolderCtx(sub, t_detected, info, keys)
            ctx.prewarm = bool(leftover)
            ctx.n_profiles = sum(1 for _, chunk in plan if chunk)
            threads = []
            for index, (pid, files) in enumerate(plan):
                if self.force_kill:
                    break
                if not files:
                    self.logger(f"➡️ {pid}: 0 clip (bỏ qua).")
                    continue
                participating.append(str(pid))
                if participating and len(participating) > 1 and CFG.profile_stagger_s > 0:
                    time.sleep(CFG.profile_stagger_s)
                t = threading.Thread(target=self._run_profile, args=(pid, files, ctx, results), daemon=True)
                t.start()
                threads.append(t)
            for t in threads:
                t.join()
            if self.force_kill:
                return "killed", None
            failed = [p for p in participating if not results.get(p, {}).get("ok")]
            if failed:
                detail = "; ".join(f"{p}: {results.get(p, {}).get('status', '?')}" for p in failed)
                self.logger(f"❌ {name}: không MOVE — profile chưa đăng xong/xác nhận được ({detail}). "
                            f"Folder được bỏ qua trong phiên này (giống bản gốc); xem nhật ký {os.path.basename(CFG.journal_path)}.")
                return "failed", None
            if leftover:
                return "again", 0.0

        # --- trước khi MOVE: tất cả video hiện có trong folder phải đã CONFIRMED (không MOVE file mới tới/đang ghi dở) ---
        fv2 = evaluate_folder(sub, self.gate)
        not_confirmed = [p for p in fv2.videos if JOURNAL.status(JOURNAL.file_key(p)) != "CONFIRMED"]
        if not_confirmed:
            self.logger(f"⏳ {name}: có {len(not_confirmed)} video mới/chưa xong chưa được đăng — chưa MOVE, sẽ xử lý ở lượt sau.")
            return "again", 0.05
        if self.force_kill:
            return "killed", None

        # --- MOVE: bước commit bắt buộc trước mọi lần quét tiếp ---
        dest_path = os.path.join(self.done_folder_path, name)
        self.logger(f"💾 Di chuyển {name} tới: {dest_path}")
        move_pids = participating or [str(p) for p in pids]
        move_ok = move_folder_safely(sub, dest_path, self._is_killed, on_locked=lambda: self._release_file_handles(move_pids),
                                     max_wait_s=CFG.move_wait_s, log=self.logger)
        if not move_ok:
            self.logger(f"❌ Lỗi di chuyển {name}.")
            return "stop_move", None
        self.logger(f"✅ Di chuyển {name} hoàn tất.")
        JOURNAL.record("MOVED", [], extra={"folder": sub, "dest": dest_path})

        # --- KEEP-ALIVE: reload (song song) và chỉ tiếp tục khi mọi trang Upload đã READY ---
        if self.keep_alive:
            if not self._reload_profiles_after_move(move_pids):
                return "stop_reload", None
        return "done", None

    # ---- vòng quét ----
    def _loop(self):
        self.logger("▶️ Bắt đầu quét và theo dõi thư mục (Zero-Delay & Hard Stop Enabled)...")
        pids = list(self.pids_list)
        if not pids:
            self.stop()                               # đặt running=False TRƯỚC khi ghi nhật ký (GUI đọc running khi nhận tín hiệu)
            self.logger("❌ Lỗi: Không có Profile ID nào được mở sẵn. Dừng quét.")
            return
        try:
            blocked = JOURNAL.blocked_entries()
            if blocked:
                self.logger(f"⚠️ [JOURNAL] Đang có {len(blocked)} file bị KHOÁ vì nghi đã đăng ở phiên trước (POSTING/UNKNOWN/FAILED_VISIBLE). "
                            f"Chúng sẽ không bị đăng lại cho tới khi bạn xác nhận (--journal-resolve).")
        except Exception:
            pass

        while self.running:
            if self.force_kill:
                break
            self.wake_event.clear()                  # xoá TRƯỚC khi quét: sự kiện đến trong lúc quét sẽ không bị mất
            t_pass = time.perf_counter()
            next_wait = SCAN_INTERVAL
            try:
                try:
                    subdirs = [os.path.join(self.base_folder, d)
                               for d in os.listdir(self.base_folder)
                               if os.path.isdir(os.path.join(self.base_folder, d))]
                    subdirs.sort()
                except FileNotFoundError:
                    self.logger(f"❌ Thư mục không tồn tại: {self.base_folder}")
                    sleep_interruptible(SCAN_INTERVAL, self._is_killed)
                    continue

                self._progress_max(len(subdirs))
                done = 0
                for sub in subdirs:
                    if not self.running or self.force_kill:
                        break
                    if sub in self.processed_dirs:
                        done += 1
                        self._progress_val(done)
                        continue
                    with self._processing_lock:
                        if sub in self.processing_now:
                            done += 1
                            self._progress_val(done)
                            continue
                        self.processing_now.add(sub)
                    try:
                        action, hint = self._process_folder(sub, pids)
                    except Exception as e:
                        self.logger(f"❌ Lỗi xử lý {os.path.basename(sub)}: {type(e).__name__}: {e}")
                        action, hint = "failed", None
                    if action in ("stop_move", "stop_reload", "stop_all_unknown"):
                        if action == "stop_move":
                            self._stop_scanning(sub, "🛑 [ANTI-DUP] Upload đã thành công nhưng MOVE thất bại. DỪNG QUÉT; không reload và không nhận thư mục mới để tránh đăng lại video.")
                        elif action == "stop_reload":
                            self._stop_scanning(sub, "🛑 [ANTI-DUP] Folder đã MOVE nhưng trang Upload chưa READY. DỪNG QUÉT để không phát sinh job chồng chéo.")
                        else:
                            self._stop_scanning(sub, "🛑 Mọi profile đều ở trạng thái UNKNOWN — DỪNG QUÉT. Hãy kiểm tra tay trên TikTok Studio rồi reset.")
                        return
                    with self._processing_lock:
                        self.processing_now.discard(sub)
                        if action in ("done", "failed", "blocked", "killed"):
                            self.processed_dirs.add(sub)
                    if action == "wait" and hint is not None:
                        next_wait = min(next_wait, max(0.01, hint))
                    elif action == "again":
                        next_wait = min(next_wait, hint if hint is not None else 0.0)
                    done += 1
                    self._progress_val(done)
            except Exception as e:
                self.logger(f"❌ Lỗi vòng quét: {e}")

            if self.force_kill or not self.running:
                break
            if next_wait > 0:
                self.wake_event.wait(timeout=next_wait)
            # chống quét liên tục khi bị đánh thức dồn dập (file đang được ghi): tối thiểu MIN_PASS_S giữa hai lượt
            el = time.perf_counter() - t_pass
            if el < self.MIN_PASS_S:
                sleep_interruptible(self.MIN_PASS_S - el, self._is_killed)


# ======================================================================================
# PHẦN 12 — WIDGET DÙNG CHUNG + GIAO DIỆN MỘT NHÓM (PremiumApp)
#   * ProfileModeWidget giữ nguyên API cũ (pid / label_pid / combo_mode / get_mode / set_mode) + set_state() hiện trạng thái FSM.
#   * StateBridge: nhận trạng thái FSM từ thread worker và chuyển sang luồng giao diện bằng Qt signal (không đụng widget từ thread khác).
#   * Chỉ chạm giao diện từ luồng giao diện (bản gốc cập nhật QListWidget/QPushButton từ thread đóng profile).
# ======================================================================================
STATE_BADGE = {
    #  trạng thái:   (chữ ngắn,       màu,        giải thích)
    "IDLE":       ("●",            "#8a8a93", "Chưa mở/chưa sẵn sàng"),
    "READY":      ("●",            "#4df58c", "Sẵn sàng: trang Upload đã mở, ô nhập file có mặt"),
    "ATTACHING":  ("▲ gắn",        "#ffd60a", "Đang gắn file vào trang Upload"),
    "UPLOADING":  ("▲ tải",        "#ffd60a", "TikTok đang nhận file"),
    "PROCESSING": ("▲ xử lý",      "#ffd60a", "TikTok đang xử lý video"),
    "POST_READY": ("▲ sẵn Post",   "#ff9c43", "Nút Post đã sáng, sắp bấm"),
    "POSTING":    ("▲ ĐĂNG",       "#ff9c43", "Đã/đang bấm Post, chờ bằng chứng giao diện"),
    "CONFIRMED":  ("✔ xong",       "#4df58c", "Đã xác nhận đăng (có bằng chứng giao diện)"),
    "CLEANUP":    ("✔ dọn",        "#4df58c", "Dọn dẹp sau khi đăng"),
    "UNKNOWN":    ("⚠ KIỂM TAY",   "#ff3b30", "Đã bấm Post nhưng KHÔNG xác minh được. Kiểm tra tay trên TikTok Studio"),
    "ERROR":      ("✖ LỖI",        "#ff6b6b", "Lỗi trước khi bấm Post (chưa đăng). Việc kế tiếp sẽ nạp lại trang"),
}


class ProfileModeWidget(QWidget):
    def __init__(self, pid, mode="CUM", color="#a88bfa", parent=None):
        super().__init__(parent)
        self.pid = pid
        self.state = ""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(10)
        self.label_pid = QLabel(f"Profile ID: <b style='color:{color};'>{pid}</b>")
        self.lbl_state = QLabel("")                                   # [v5] trạng thái FSM của profile
        self.combo_mode = QComboBox()
        self.combo_mode.setStyleSheet("QComboBox {padding: 4px; border: 1px solid #555; border-radius: 4px;}")
        self.combo_mode.addItem("⚡ CỤM (CUM)", "CUM")
        self.combo_mode.addItem("🐢 TUẦN TỰ (TT)", "TT")
        index = self.combo_mode.findData(mode)
        if index != -1:
            self.combo_mode.setCurrentIndex(index)
        layout.addWidget(self.label_pid)
        layout.addWidget(self.lbl_state)
        layout.addStretch()
        layout.addWidget(QLabel("Mode:"))
        layout.addWidget(self.combo_mode)

    def get_mode(self):
        return self.combo_mode.currentData()

    def set_mode(self, mode):
        index = self.combo_mode.findData(mode)
        if index != -1:
            self.combo_mode.setCurrentIndex(index)

    def set_state(self, state: str, why: str = ""):
        """Chỉ hiển thị; không đổi logic. Phải gọi từ luồng giao diện."""
        mark, col, desc = STATE_BADGE.get(state, ("●", "#8a8a93", state))
        self.state = state
        self.lbl_state.setText(f"<span style='color:{col}; font-weight:700;'>{mark}</span>")
        tip = f"{state}: {desc}"
        if why:
            tip += f"\n{why}"
        if state in ("UNKNOWN", "ERROR"):
            tip += "\n(Chuột phải để đánh dấu 'đã kiểm tra tay' khi cần mở khoá profile)"
        self.lbl_state.setToolTip(tip)


class StateBridge(QObject):
    """FSM báo trạng thái từ thread worker -> phát signal -> slot chạy ở luồng giao diện."""
    changed = Signal(str, str, str)             # pid, state, why

    def push(self, pid, old, new, why):
        try:
            self.changed.emit(str(pid), new.value, str(why or ""))
        except Exception:
            pass


def attach_profile_context_menu(list_widget, log: Callable[[str], None], get_pid_of_item: Callable[[Any], Optional[str]]):
    """Chuột phải trên một profile ở trạng thái UNKNOWN/ERROR: 'Đã kiểm tra tay' -> cho phép profile nhận việc mới.
    Chỉ mở khoá PROFILE. File nghi đã đăng vẫn bị khoá bởi nhật ký cho tới khi bạn xác nhận từng file (--journal-resolve / hộp thoại nhật ký)."""
    list_widget.setContextMenuPolicy(Qt.CustomContextMenu)

    def show(pos):
        item = list_widget.itemAt(pos)
        if item is None:
            return
        pid = get_pid_of_item(item)
        if not pid:
            return
        st = get_fsm(pid).state
        menu = QMenu(list_widget)
        act = menu.addAction(f"Đã kiểm tra tay trên TikTok → mở khoá profile {pid}")
        act.setEnabled(st in (PState.UNKNOWN, PState.ERROR))
        chosen = menu.exec(list_widget.viewport().mapToGlobal(pos))
        if chosen is not None and act.isEnabled():            # menu chỉ có MỘT hành động => không cần so sánh danh tính đối tượng Qt
            get_fsm(pid).reset_unknown("người dùng đã kiểm tra tay")
            log(f"🔓 Profile {pid}: đã mở khoá thủ công (UNKNOWN/ERROR → READY). Trang sẽ được nạp lại ở việc kế tiếp.")

    list_widget.customContextMenuRequested.connect(show)


def parse_pid_lines(text: str) -> list:
    """Mỗi dòng một Profile ID, bỏ dòng trống/trùng, GIỮ NGUYÊN THỨ TỰ nhập (ưu tiên 1,2,3...)."""
    pids, seen = [], set()
    for line in (text or "").strip().split("\n"):
        pid = line.strip()
        if pid and pid not in seen:
            pids.append(pid)
            seen.add(pid)
    return pids


class PremiumApp(QWidget):
    closed_all = Signal()                                    # [v5] báo từ thread đóng profile về luồng giao diện

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Profile Uploader — Premium GUI (Instant Upload)")
        self.setMinimumSize(1100, 680)
        self.setFont(QFont("Segoe UI", 10))
        self.accents = [("Purple", "#a88bfa"), ("Blue", "#2d89ef"), ("Green", "#0a7a4f"), ("Pink", "#ff6aa3"), ("Orange", "#ff8c42"), ("Gray", "#6b7280")]
        self.profile_colors = ["#ff6aa3", "#a88bfa", "#2d89ef", "#0a7a4f", "#ff8c42", "#6b7280"]
        self.profile_color_map = {}
        self.accent = self.accents[0][1]
        self.scanner = None
        self.mode_widgets = {}
        self.saved_modes = {}
        self.displayed_pids = set()
        self.local_pids = set()
        self.open_threads = []

        self._build_ui()
        self.logger_qt = LoggerQt(self.log_widget, interval_ms=80)
        self.log = self._log_to_ui
        self._load_theme()
        self._load_config()
        self.logger_qt.start()

        self.closed_all.connect(self._on_closed_all)
        self._bridge = StateBridge()
        self._bridge.changed.connect(self._on_state)
        add_state_listener(self._bridge.push)
        attach_profile_context_menu(self.list_profiles, self.log, lambda it: getattr(self.list_profiles.itemWidget(it), "pid", None))
        info = backend_info()
        self.log(f"ℹ️ Backend {info['name']} v{info['version']} (api {info['api']}, sha {info['sha256_12']}) — {info['file']}")

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        left = QVBoxLayout()
        left_frame = QFrame()
        left_frame.setLayout(left)
        left_frame.setFixedWidth(420)

        header = QLabel("Profile Uploader (Instant)")
        header.setFont(QFont("Segoe UI", 14, QFont.Bold))
        header.setAlignment(Qt.AlignCenter)
        left.addWidget(header)

        left.addWidget(QLabel("1. Nhập Profile ID (Mỗi ID 1 dòng):"))
        self.input_pids = QTextEdit()
        self.input_pids.setFixedHeight(100)
        self.input_pids.setPlaceholderText("ID1\nID2\n...")
        left.addWidget(self.input_pids)

        h1 = QHBoxLayout()
        self.btn_open_profiles = QPushButton("Open Profiles")
        self.btn_open_profiles.clicked.connect(self._open_profiles)
        self.btn_close_profiles = QPushButton("Close Profiles")
        self.btn_close_profiles.clicked.connect(self.stop_scan)
        self.btn_close_profiles.setEnabled(False)
        h1.addWidget(self.btn_open_profiles)
        h1.addWidget(self.btn_close_profiles)
        left.addLayout(h1)

        left.addWidget(QLabel("2. Thư mục chứa video nguồn:"))
        self.input_folder = QLineEdit()
        self.input_folder.setPlaceholderText("Folder containing subfolders")
        btn_folder = QPushButton("Browse")
        btn_folder.clicked.connect(self._pick_folder)
        h2 = QHBoxLayout()
        h2.addWidget(self.input_folder)
        h2.addWidget(btn_folder)
        left.addLayout(h2)

        left.addWidget(QLabel("3. Thư mục DONE (Sau khi xử lý):"))
        self.input_done_folder = QLineEdit()
        self.input_done_folder.setPlaceholderText("Folder to move processed files")
        btn_done_folder = QPushButton("Browse")
        btn_done_folder.clicked.connect(self._pick_done_folder)
        h3 = QHBoxLayout()
        h3.addWidget(self.input_done_folder)
        h3.addWidget(btn_done_folder)
        left.addLayout(h3)

        left.addWidget(QLabel("4. Giới hạn video/profile (0 = chia đều):"))
        self.spin_limit = QSpinBox()
        self.spin_limit.setRange(0, 999)
        self.spin_limit.setValue(0)
        left.addWidget(self.spin_limit)

        left.addWidget(QLabel("Accent color"))
        self.combo_accent = QComboBox()
        for name, col in self.accents:
            self.combo_accent.addItem(name, col)
        self.combo_accent.currentIndexChanged.connect(self._apply_accent)
        left.addWidget(self.combo_accent)

        hhex = QHBoxLayout()
        self.hex_entry = QLineEdit()
        self.hex_entry.setPlaceholderText("#rrggbb")
        self.btn_apply_hex = QPushButton("Apply")
        self.btn_apply_hex.clicked.connect(self._apply_hex)
        hhex.addWidget(self.hex_entry)
        hhex.addWidget(self.btn_apply_hex)
        left.addLayout(hhex)

        btn_layout = QHBoxLayout()
        self.btn_start = QPushButton("Start Scanning")
        self.btn_start.clicked.connect(self.start_scan)
        self.btn_stop_scan = QPushButton("Stop Scanning (Hard Kill)")
        self.btn_stop_scan.clicked.connect(self._stop_scan_only)
        self.btn_stop_scan.setEnabled(False)
        btn_layout.addWidget(self.btn_start)
        btn_layout.addWidget(self.btn_stop_scan)
        left.addLayout(btn_layout)

        left.addWidget(QLabel("Tiến độ:"))
        self.progress = QProgressBar()
        self.progress.setMaximum(100)
        left.addWidget(self.progress)

        left.addWidget(QLabel("5. Profiles & Mode:"))
        self.list_profiles = QListWidget()
        self.list_profiles.setFixedHeight(120)
        left.addWidget(self.list_profiles)

        self.btn_save_config = QPushButton("Lưu cấu hình")
        self.btn_save_config.clicked.connect(self._save_config)
        left.addWidget(self.btn_save_config)

        root.addWidget(left_frame)

        right = QVBoxLayout()
        right_frame = QFrame()
        right_frame.setLayout(right)

        self.status_badge = QLabel("IDLE")
        self.status_badge.setFixedWidth(90)
        self.status_badge.setAlignment(Qt.AlignCenter)
        self.status_badge.setStyleSheet("background:#999; border-radius:8px; padding:6px; font-weight:700;")

        right_header = QHBoxLayout()
        right_header.addWidget(QLabel("Log hoạt động (Có Timing):"))
        right_header.addStretch()
        right_header.addWidget(self.status_badge)
        right.addLayout(right_header)

        self.log_widget = QTextEdit()
        self.log_widget.setReadOnly(True)
        self.log_widget.setFont(QFont("Consolas", 9))
        self.log_widget.document().setMaximumBlockCount(1500)
        right.addWidget(self.log_widget)

        root.addWidget(right_frame)

    def _log_to_ui(self, msg: str):
        ACCENT_COLOR = self.accent
        final_msg = str(msg)

        KEYWORD_RE = re.compile(r'(Profile ID|Mode|video|file|clip|CỤM|TUẦN TỰ)')
        final_msg = KEYWORD_RE.sub(lambda m: f"<b style='color:{ACCENT_COLOR};'>{m.group(0)}</b>", final_msg)

        PATH_RE = re.compile(r'((?:[A-Za-z]:[\\/][^\s\<\>]+[^\s\<\>]*|[^\s\<\>]+[\\/][^\s\<\>]+|[\w\-. ]+\.(?:mp4|mov|mkv|avi|xlsx|json)))')
        final_msg = PATH_RE.sub(lambda m: f"<span style='color:#00ffff;'>{m.group(0)}</span>", final_msg)

        NUMBER_RE = re.compile(r'(?<!\w)(\d+(?:/\d+)?)(?!\w)')
        final_msg = NUMBER_RE.sub(lambda m: f"<span style='color:#ffdd66;'>{m.group(0)}</span>", final_msg)

        STATUS_COLOR_MAP = {
            "✅": "#00ff7f", "❌": "#ff4d4d", "⚠️": "#ffb800",
            "▶️": "#0099ff", "🛑": "#ff3300", "📁": "#ffb800",
            "🚀": "#a88bfa", "💾": "#99ccff", "🔢": "#ff66b2",
            "➡️": "#999999", "🔁": "#00ff7f", "🚨": "#ff4d4d",
            "⚡": "#ffff00", "⏱️": "#00ffff"
        }
        for symbol, color in STATUS_COLOR_MAP.items():
            final_msg = final_msg.replace(symbol, f"<span style='color:{color};'>{symbol}</span>")
        self.logger_qt.log(final_msg)

    def _on_state(self, pid: str, state: str, why: str):
        w = self.mode_widgets.get(pid)
        if w is not None:
            try:
                w.set_state(state, why)
            except Exception:
                pass

    def _pick_excel(self):
        QMessageBox.warning(self, "Info", "Tính năng này đã bị vô hiệu hóa.")

    def _pick_folder(self):
        p = QFileDialog.getExistingDirectory(self, "Select base folder", BASE_DIR)
        if p:
            self.input_folder.setText(p)

    def _pick_done_folder(self):
        p = QFileDialog.getExistingDirectory(self, "Select done folder", BASE_DIR)
        if p:
            self.input_done_folder.setText(p)

    def _apply_stylesheet(self, accent_hex):
        self.accent = accent_hex
        darker = self._adjust_brightness(accent_hex, -0.4)
        self.setStyleSheet(f"""
            QWidget {{ background-color: #2e2e2e; color: #f0f0f0; }}
            QFrame {{ border: none; }}
            QTextEdit, QLineEdit, QSpinBox, QListWidget {{ background-color: #3e3e3e; border: 1px solid #555; border-radius: 4px; padding: 5px; }}
            QPushButton {{ background-color: {accent_hex}; color: #2e2e2e; border: none; border-radius: 4px; padding: 8px; font-weight: bold; }}
            QPushButton:hover {{ background-color: {self._adjust_brightness(accent_hex, 0.1)}; }}
            QPushButton:pressed {{ background-color: {self._adjust_brightness(accent_hex, -0.1)}; }}
            QPushButton:disabled {{ background-color: #555; color: #999; }}
            QProgressBar {{ border: 1px solid #555; border-radius: 4px; text-align: center; color: #f0f0f0; background-color: #3e3e3e; }}
            QProgressBar::chunk {{ background-color: {accent_hex}; border-radius: 4px; }}
            QComboBox {{ background-color: #3e3e3e; border: 1px solid #555; border-radius: 4px; padding: 5px; }}
            QComboBox::drop-down {{ border: 0px; width: 20px; }}
            QComboBox::down-arrow {{ image: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" height="24" viewBox="0 0 24 24" width="24"><path fill="{accent_hex.replace('#', '%23')}" d="M7 10l5 5 5-5z"/></svg>'); }}
            QTextEdit:focus, QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ outline: 2px solid {accent_hex}; }}
        """)
        self.status_badge.setStyleSheet(f"background:{darker}; border-radius:8px; padding:6px; font-weight:700;")

    def _apply_accent(self, idx):
        col = self.combo_accent.itemData(idx)
        if col:
            self.hex_entry.setText(col)
            self._apply_stylesheet(col)
            self._save_theme()

    def _apply_hex(self):
        val = (self.hex_entry.text() or "").strip()
        if not val.startswith('#') or len(val) not in (4, 7):
            return
        self._apply_stylesheet(val)
        self._save_theme()

    def _adjust_brightness(self, hexcol: str, delta: float) -> str:
        try:
            h = hexcol.lstrip('#')
            if len(h) == 3:
                h = ''.join([c * 2 for c in h])
            r = int(h[0:2], 16)
            g = int(h[2:4], 16)
            b = int(h[4:6], 16)

            def clamp(x):
                return max(0, min(255, int(x)))
            r = clamp(r * (1 + delta))
            g = clamp(g * (1 + delta))
            b = clamp(b * (1 + delta))
            return f"#{r:02x}{g:02x}{b:02x}"
        except Exception:
            return hexcol

    def _save_theme(self):
        try:
            cfg = {"accent": self.accent}
            with open(CONFIG_THEME_JSON, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_theme(self):
        try:
            if os.path.exists(CONFIG_THEME_JSON):
                with open(CONFIG_THEME_JSON, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
                accent = cfg.get('accent')
                if accent:
                    for i in range(self.combo_accent.count()):
                        if self.combo_accent.itemData(i).lower() == accent.lower():
                            self.combo_accent.setCurrentIndex(i)
                            break
                    self._apply_stylesheet(accent)
        except Exception:
            pass

    def _save_config(self):
        try:
            current_modes = self._get_modes_from_gui()
            if current_modes:
                self.saved_modes = current_modes
            data = {
                "pids": self.input_pids.toPlainText(),
                "src_folder": self.input_folder.text(),
                "done_folder": self.input_done_folder.text(),
                "limit": self.spin_limit.value(),
                "saved_modes": self.saved_modes
            }
            with open(CONFIG_APP_JSON, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self.log("✅ Đã lưu cấu hình.")
        except Exception as e:
            self.log(f"❌ Lỗi khi lưu cấu hình: {e}")

    def _load_config(self):
        try:
            if os.path.exists(CONFIG_APP_JSON):
                with open(CONFIG_APP_JSON, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.input_pids.setText(data.get("pids", ""))
                self.input_folder.setText(data.get("src_folder", ""))
                self.input_done_folder.setText(data.get("done_folder", ""))
                self.spin_limit.setValue(data.get("limit", 0))
                self.saved_modes = data.get("saved_modes", {})
                self.log("✅ Đã tải cấu hình.")
        except Exception as e:
            self.log(f"⚠️ Lỗi khi tải cấu hình: {e}")

    def _get_pids_only_from_input(self) -> list:
        return parse_pid_lines(self.input_pids.toPlainText())

    def _get_modes_from_gui(self) -> dict:
        modes = {}
        for pid, widget in self.mode_widgets.items():
            modes[pid] = widget.get_mode()
        return modes

    def _add_profile_row(self, pid: str):
        color = self.profile_colors[len(self.displayed_pids) % len(self.profile_colors)]
        self.profile_color_map[pid] = color
        item = QListWidgetItem(self.list_profiles)
        mode = self.saved_modes.get(pid, "CUM")
        w = ProfileModeWidget(pid, mode, color=color)
        item.setSizeHint(w.sizeHint())
        self.list_profiles.addItem(item)
        self.list_profiles.setItemWidget(item, w)
        self.mode_widgets[pid] = w
        self.displayed_pids.add(pid)
        w.set_state(get_fsm(pid).state.value)

    def _open_profiles(self):
        all_pids = self._get_pids_only_from_input()
        if not all_pids:
            QMessageBox.critical(self, "Lỗi", "Hãy nhập Profile ID.")
            return

        self.list_profiles.clear()
        self.mode_widgets.clear()
        self.profile_color_map.clear()
        self.displayed_pids.clear()
        self.local_pids.clear()
        self.log(f"🔓 Đang mở {len(all_pids)} profile. Vui lòng đợi...")
        self.btn_open_profiles.setEnabled(False)
        self.btn_close_profiles.setEnabled(False)
        self.btn_start.setEnabled(False)

        def preload_worker(pid_s):
            try:
                pid_ret, drv = setup_and_navigate_profile(str(pid_s), None, CHROME_DRIVER_PATH, TARGET_UPLOAD_URL)
            except Exception as e:
                pid_ret, drv = pid_s, None
                self.log(f"❌ Profile {pid_s} mở lỗi: {e}")
            if drv:
                with drivers_lock:
                    drivers[str(pid_s)] = drv
                self.log(f"✅ Profile {pid_s} mở OK.")
                self.local_pids.add(str(pid_s))
            else:
                self.log(f"❌ Profile {pid_s} mở lỗi.")

        self.open_threads = []
        for pid_s in all_pids:
            t = threading.Thread(target=preload_worker, args=(pid_s,), daemon=True)
            t.start()
            self.open_threads.append(t)

        def check_threads_done():
            for pid in list(self.local_pids):
                if pid not in self.displayed_pids:
                    self._add_profile_row(pid)
                    self.btn_start.setEnabled(True)

            # [v5] kết thúc khi MỌI thread mở đã xong (bản gốc chờ đủ số profile nên một profile lỗi làm nút bị khoá mãi)
            if not any(t.is_alive() for t in self.open_threads):
                for pid in list(self.local_pids):
                    if pid not in self.displayed_pids:
                        self._add_profile_row(pid)
                self.check_open_timer.stop()
                self.btn_open_profiles.setEnabled(True)
                self.btn_close_profiles.setEnabled(True)
                self.btn_start.setEnabled(bool(self.local_pids))
                self.log(f"✅ Đã mở xong {len(self.local_pids)}/{len(all_pids)} profiles.")

        self.check_open_timer = QTimer(self)
        self.check_open_timer.timeout.connect(check_threads_done)
        self.check_open_timer.start(300)

    def _stop_scan_only(self):
        if self.scanner:
            self.scanner.force_kill = True
            self.scanner.stop()
            self.scanner = None
        self.btn_start.setEnabled(True)
        self.btn_stop_scan.setEnabled(False)
        self.status_badge.setText("IDLE")
        self.status_badge.setStyleSheet(f"background:{self._adjust_brightness(self.accent, -0.4)}; border-radius:8px; padding:6px; font-weight:700;")
        self.progress.setValue(0)
        self.log("🛑 Đã dừng quét (Hard Kill).")

    def start_scan(self):
        base = self.input_folder.text()
        done_folder = self.input_done_folder.text()

        input_pids = self._get_pids_only_from_input()
        opened_pids = [pid for pid in input_pids if pid in drivers]

        if not opened_pids:
            QMessageBox.critical(self, "Lỗi", "Hãy mở profiles trước.")
            return
        if not base or not os.path.exists(base) or not os.path.isdir(base):
            QMessageBox.critical(self, "Thiếu thư mục", "Hãy chọn thư mục video gốc.")
            return
        if not done_folder or not os.path.exists(done_folder):
            QMessageBox.critical(self, "Thiếu thư mục DONE", "Hãy chọn thư mục đích.")
            return

        final_modes = self._get_modes_from_gui()
        if not final_modes:
            QMessageBox.critical(self, "Lỗi", "Chưa có mode.")
            return

        self.log_widget.clear()
        self.log(f"🚀 Bắt đầu quét với {len(opened_pids)} profile (Instant Speed & Hard Stop).")
        self.scanner = Scanner("", base, self._log_to_ui, self.progress, lambda: self.spin_limit.value(), True, opened_pids, done_folder, final_modes)
        self.scanner.start()
        self.btn_start.setEnabled(False)
        self.btn_stop_scan.setEnabled(True)
        self.status_badge.setText("SCANNING")
        self.status_badge.setStyleSheet(f"background:{self.accent}; border-radius:8px; padding:6px; font-weight:700;")
        self.progress.setValue(0)

    def stop_scan(self):
        self._stop_scan_only()
        self.btn_open_profiles.setEnabled(False)
        self.btn_close_profiles.setEnabled(False)
        self.log("🛑 Đang đóng tất cả Profiles...")

        def close_worker():
            with drivers_lock:
                pids_to_close = list(drivers.keys())
            for pid in pids_to_close:
                drv = drivers.get(pid)
                if drv:
                    try:
                        drv.quit()
                        quit_genlogin_driver(pid, GENLOGIN_API_TOKEN)
                        self.log(f"🛑 Đã đóng profile {pid}.")
                    except Exception as e:
                        self.log(f"❌ Lỗi đóng {pid}: {e}")
                    finally:
                        with drivers_lock:
                            if pid in drivers:
                                del drivers[pid]
                        profile_closed(pid)
            self.log("✅ Đã đóng tất cả profile.")
            self.closed_all.emit()                          # [v5] việc cập nhật giao diện làm ở luồng giao diện
        threading.Thread(target=close_worker, daemon=True).start()

    def _on_closed_all(self):
        self.list_profiles.clear()
        self.mode_widgets.clear()
        self.displayed_pids.clear()
        self.local_pids.clear()
        self.btn_open_profiles.setEnabled(True)
        self.btn_close_profiles.setEnabled(True)
        self.btn_start.setEnabled(False)


# ======================================================================================
# PHẦN 13 — CÔNG CỤ DÒNG LỆNH (không đăng gì, trừ khi bạn tự chạy Scanner/GUI)
#   --selftest          kiểm tra nội bộ không cần trình duyệt/TikTok
#   --perf-summary      median/P95/tỷ lệ thành công từ nhật ký PERF (+ --compare A B có khoảng tin cậy bootstrap)
#   --journal-show      liệt kê file bị khoá vì nghi đã đăng;  --journal-resolve posted|not-posted  để gỡ khoá sau khi bạn kiểm tra tay
#   --calibrate-confirm quan sát (KHÔNG bấm gì) khi BẠN tự đăng 1 video: ghi lại URL/văn bản/hộp thoại để chỉnh TAM_CONFIRM_*_RE
#   --bench-overhead    đo chi phí cơ chế chờ nút (RTT WebDriver, CPU, số mutation) — event vs poll — trên profile thật, KHÔNG đăng
#   --bench-upload      chạy thử gắn file → chờ nút Post sáng → DỪNG (không bấm Post) để A/B Stage/WaitMode/Reload trên TikTok thật
# ======================================================================================
def journal_resolve(keys: List[str], posted: bool, note: str = "manual") -> int:
    """Người đã kiểm tra tay. posted=True: ghi CONFIRMED (coi như đã đăng, được phép MOVE).
    posted=False: ghi NOT_POSTED (gỡ khoá, tool được phép đăng lại file này). Trả số file đã xử lý."""
    ents = {e["key"]: e for e in JOURNAL.blocked_entries()}
    items = [(k, ents[k]["path"]) for k in keys if k in ents]
    if not items:
        return 0
    ok = JOURNAL.record("CONFIRMED" if posted else "NOT_POSTED", items, pid="manual", durable=True,
                        extra={"manual": True, "note": note})
    return len(items) if ok else 0


def bootstrap_median_diff(a: List[float], b: List[float], iters: int = 2000, seed: int = 20260101):
    """(median(b)-median(a), lo95, hi95). Dương = B chậm hơn A. Bootstrap xác định (seed cố định)."""
    rnd = random.Random(seed)
    na, nb = len(a), len(b)
    diffs = []
    for _ in range(iters):
        sa = [a[rnd.randrange(na)] for _ in range(na)]
        sb = [b[rnd.randrange(nb)] for _ in range(nb)]
        diffs.append(statistics.median(sb) - statistics.median(sa))
    diffs.sort()
    lo = diffs[int(0.025 * iters)]
    hi = diffs[max(0, int(0.975 * iters) - 1)]
    return statistics.median(b) - statistics.median(a), lo, hi


def compare_perf_groups(records: List[Dict[str, Any]], by: str, ga: str, gb: str) -> str:
    def vals(group, metric):
        out = []
        for r in records:
            key = r.get(by)
            if key is None:
                key = (r.get("flags") or {}).get(by)
            if str(key) == group and r.get("ms") and r["ms"].get(metric) is not None:
                out.append(float(r["ms"][metric]))
        return out

    lines = [f"\n=== SO SÁNH  A={ga}  vs  B={gb}  (theo '{by}')  — Δ = median(B) − median(A); âm = B nhanh hơn ===",
             f"{'chỉ số':18} {'nA':>4} {'nB':>4} {'medA(ms)':>10} {'medB(ms)':>10} {'Δ(ms)':>9} {'KTC95% của Δ':>22}  kết luận"]
    for name, _a, _b, _g in INTERVALS:
        va, vb = vals(ga, name), vals(gb, name)
        if not va or not vb:
            lines.append(f"{name:18} {len(va):>4} {len(vb):>4} {'UNKNOWN':>10} {'UNKNOWN':>10} {'-':>9} {'-':>22}  không đủ dữ liệu")
            continue
        ma, mb = statistics.median(va), statistics.median(vb)
        if min(len(va), len(vb)) < 5:
            lines.append(f"{name:18} {len(va):>4} {len(vb):>4} {ma:>10.1f} {mb:>10.1f} {mb - ma:>9.1f} {'-':>22}  n<5: chưa kết luận được")
            continue
        d, lo, hi = bootstrap_median_diff(va, vb)
        if lo > 0:
            v = "B CHẬM hơn A (khác biệt rõ)"
        elif hi < 0:
            v = "B NHANH hơn A (khác biệt rõ)"
        else:
            v = "chưa đủ bằng chứng có khác biệt"
        lines.append(f"{name:18} {len(va):>4} {len(vb):>4} {ma:>10.1f} {mb:>10.1f} {d:>9.1f} {('[%.1f ; %.1f]' % (lo, hi)):>22}  {v}")
    ex = collections.OrderedDict()
    for r in records:
        key = r.get(by)
        if key is None:
            key = (r.get("flags") or {}).get(by)
        for k, v in (r.get("flags") or {}).items():
            if k.startswith("x_") and isinstance(v, (int, float)) and not isinstance(v, bool):
                ex.setdefault(k, {}).setdefault(str(key), []).append(float(v))
    if ex:
        lines.append(f"{'chỉ số phụ (x_*)':28} {'nA':>4} {'nB':>4} {'medA':>10} {'medB':>10}   (tải CPU/DOM, tab ẩn, tuổi trang...)")
        for k, d in ex.items():
            va, vb = d.get(ga, []), d.get(gb, [])
            if va and vb:
                lines.append(f"{k:28} {len(va):>4} {len(vb):>4} {statistics.median(va):>10.1f} {statistics.median(vb):>10.1f}")
    lines.append("Lưu ý: chỉ so sánh khi hai nhóm chạy xen kẽ cùng điều kiện (cùng profile, cùng giờ, cùng cỡ file). "
                 "Mỗi nhóm nên ≥ 20 việc; n nhỏ thì P95 ≈ max.")
    return "\n".join(lines)


def cli_perf_summary(path: Optional[str], by: str, compare: Optional[List[str]]) -> int:
    path = path or CFG.perf_log
    if not os.path.exists(path):
        print(f"Không thấy nhật ký PERF: {path}\n(Chạy một vài lượt đăng/--bench-upload trước; mỗi việc ghi 1 dòng JSONL.)")
        return 2
    recs = load_perf_records(path)
    if not recs:
        print(f"Nhật ký PERF rỗng: {path}")
        return 2
    print(f"Nhật ký: {path} — {len(recs)} bản ghi — gom theo '{by}'")
    print(format_perf_summary(summarize_perf(recs, by)))
    print("\nChú thích: UNKNOWN = mốc không đo tin cậy được (không bịa số). '≈' = ước lượng từ mtime/sự kiện rename. "
          "Nhóm: scan | browser | transfer | tiktok | tong. 'success' = CONFIRMED / số việc.")
    if compare:
        print(compare_perf_groups(recs, by, compare[0], compare[1]))
    return 0


# ---------------------------------------------------------------- journal ----
def cli_journal_show() -> int:
    ents = JOURNAL.blocked_entries()
    print(f"Nhật ký: {JOURNAL.path}")
    if not ents:
        print("Không có file nào đang bị khoá.")
        return 0
    print(f"{len(ents)} file đang bị KHOÁ (nghi đã đăng; tool sẽ KHÔNG tự đăng lại):")
    for e in sorted(ents, key=lambda x: x.get("t", "")):
        print(f"  [{e['status']:<14}] key={e['key']}  pid={e.get('pid', '')}  lúc {e.get('t', '')}\n      {e.get('path', '')}")
    print("\nHãy mở TikTok Studio → Manage posts kiểm tra từng video, rồi:\n"
          "  đã đăng      :  python tam_INSTANT_POST.py --journal-resolve posted --keys <key> [<key> ...]   (hoặc --all)\n"
          "  CHƯA đăng    :  python tam_INSTANT_POST.py --journal-resolve not-posted --keys <key> ...\n"
          "Sau đó dừng/chạy lại quét để thư mục được xét lại (thư mục bị khoá được bỏ qua đến hết phiên quét).")
    return 0


def cli_journal_resolve(verdict: str, keys: Optional[List[str]], all_: bool, yes: bool) -> int:
    ents = JOURNAL.blocked_entries()
    if not ents:
        print("Không có file nào đang bị khoá.")
        return 0
    if all_:
        keys = [e["key"] for e in ents]
    if not keys:
        print("Cần --keys <key>... hoặc --all (xem danh sách bằng --journal-show).")
        return 2
    known = {e["key"] for e in ents}
    unknown = [k for k in keys if k not in known]
    if unknown:
        print("Không có key đang khoá: " + ", ".join(unknown))
        return 2
    posted = verdict == "posted"
    what = "ĐÃ ĐĂNG (ghi CONFIRMED; thư mục sẽ được MOVE khi đủ)" if posted else "CHƯA ĐĂNG (gỡ khoá; tool sẽ ĐĂNG LẠI các file này)"
    if not yes:
        ans = input(f"Xác nhận {len(keys)} file là {what}? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("Đã huỷ.")
            return 1
    n = journal_resolve(keys, posted, note="cli")
    print(f"Đã ghi {n}/{len(keys)} file vào nhật ký." + ("" if n == len(keys) else "  ⚠️ Có file không ghi được — xem cảnh báo ở trên."))
    return 0 if n == len(keys) else 1


# ---------------------------------------------------------------- selftest ----
def run_selftest() -> int:
    rows = []       # type: List[Tuple[str, str, str]]    (OK|FAIL|WARN|SKIP, tên, chi tiết)

    def check(name, fn):
        try:
            d = fn()
            if isinstance(d, tuple):
                rows.append((d[0], name, d[1]))
            else:
                rows.append(("OK", name, d or ""))
        except Exception as e:
            rows.append(("FAIL", name, f"{type(e).__name__}: {e}"))

    def t_python():
        assert sys.version_info >= (3, 9), f"cần Python ≥ 3.9, đang {sys.version.split()[0]}"
        return sys.version.split()[0]

    def t_backend_file():
        info = backend_info()
        here = os.path.basename(info["file"])
        dups = []
        try:
            for n in os.listdir(os.path.dirname(info["file"])):
                low = n.lower()
                if low.endswith(".py") and "tam_instant_post" in low and n != "tam_INSTANT_POST.py" and not low.startswith("test"):
                    dups.append(n)
        except Exception:
            pass
        msg = f"{here} v{info['version']} api{info['api']} sha {info['sha256_12']}"
        if here != "tam_INSTANT_POST.py":
            return ("FAIL", msg + " — TÊN FILE SAI: GUI nhóm chạy `import tam_INSTANT_POST` nên sẽ KHÔNG nạp file này. Đổi tên (bỏ hậu tố như ' (4)').")
        if dups:
            return ("WARN", msg + " — còn bản trùng tên gần giống: " + ", ".join(dups) + " (xoá/di chuyển đi để khỏi nhầm).")
        return msg

    def t_deps():
        miss = []
        for label, ok, need in (("selenium", webdriver is not None, "bắt buộc"), ("Genlogin", Genlogin is not None, "bắt buộc"),
                                ("PySide6", HAS_QT, "bắt buộc cho GUI"), ("watchdog", Observer is not None, "khuyến nghị (quét theo sự kiện)"),
                                ("psutil", psutil is not None, "khuyến nghị (ưu tiên HIGH, đo CPU)")):
            if not ok:
                miss.append(f"{label} ({need})")
        if miss:
            return ("WARN", "thiếu: " + "; ".join(miss))
        return "đủ selenium, Genlogin, PySide6, watchdog, psutil"

    def t_driver():
        if os.path.exists(CHROME_DRIVER_PATH):
            return CHROME_DRIVER_PATH
        return ("WARN", f"không thấy {CHROME_DRIVER_PATH} (cần để gắn Selenium vào profile)")

    def t_fsm():
        f = ProfileFSM("selftest")
        for st in (PState.READY, PState.ATTACHING, PState.UPLOADING, PState.PROCESSING, PState.POST_READY, PState.POSTING,
                   PState.CONFIRMED, PState.CLEANUP, PState.READY):
            assert f.to(st, "selftest"), f"chuyển {st.value} bị coi là sai"
        assert f.violations == 0
        g = ProfileFSM("selftest2")
        g.to(PState.READY)
        g.to(PState.ATTACHING)
        g.to(PState.UNKNOWN, "x")
        v0 = g.violations
        try:
            with contextlib.redirect_stdout(io.StringIO()):          # FSM in cảnh báo khi gặp chuyển trái phép: ở đây là chủ ý
                ok = g.to(PState.POSTING, "cấm: UNKNOWN không được tự đăng tiếp")
        except IllegalTransition:
            ok = False
        assert not ok and (g.violations == v0 + 1 or STATE_STRICT), "chuyển UNKNOWN→POSTING phải bị phát hiện"
        return "chuỗi hợp lệ IDLE→READY→…→CONFIRMED→CLEANUP→READY; chuyển trái phép UNKNOWN→POSTING bị phát hiện"

    def t_plan():
        pids = ["A", "B", "C"]
        files = [f"/x/{i}.mp4" for i in range(7)]
        plan, left = plan_distribution_ex(files, pids, 0)
        assert [p for p, _ in plan] == pids, "thứ tự ưu tiên profile phải giữ nguyên"
        assert sum(len(c) for _, c in plan) == 7 and not left
        assert sorted(sum((c for _, c in plan), [])) == sorted(files), "mất/trùng file"
        plan2, left2 = plan_distribution_ex(files, pids, 2)
        assert sum(len(c) for _, c in plan2) == 6 and len(left2) == 1, "giới hạn 2/profile: 6 đăng, 1 để lượt sau"
        return "ưu tiên A,B,C; chia 7 file = " + str([len(c) for _, c in plan]) + "; limit=2 -> còn 1 file ở lượt sau (bản cũ làm rơi)"

    def t_filegate():
        d = tempfile.mkdtemp(prefix="tam_st_")
        try:
            a = os.path.join(d, "a.mp4")
            with open(a, "wb") as f:
                f.write(b"x" * 2048)
            old = time.time() - 30
            os.utime(a, (old, old))
            g = FileGate(stable_ms=300)
            ok, _w, why = g.check(a)
            assert ok, f"file cũ phải SẴN SÀNG ngay ({why})"
            b = os.path.join(d, "b.mp4")
            with open(b, "wb") as f:
                f.write(b"y" * 100)
            ok2, w2, _ = g.check(b)
            assert not ok2 and w2 > 0, "file vừa ghi phải CHỜ"
            time.sleep(0.45)
            ok3, _, _ = g.check(b)
            assert ok3, "file không đổi quá 300 ms phải sẵn sàng"
            with open(b, "ab") as f:
                f.write(b"z" * 10)
            ok4, _, _ = g.check(b)
            assert not ok4, "file vừa đổi kích thước phải CHỜ lại"
            assert is_intermediate_name("clip.mp4.part") and is_intermediate_name("v.f137.mp4") and not is_intermediate_name("my.favorite.mp4")
            os.makedirs(os.path.join(d, "s"))
            for nm in ("ok.mp4", "clip.f137.mp4", "x.temp.mp4"):
                with open(os.path.join(d, "s", nm), "wb") as f:
                    f.write(b"1" * 10)
            fv = evaluate_folder(os.path.join(d, "s"), FileGate(stable_ms=0))
            assert [os.path.basename(v) for v in fv.videos] == ["ok.mp4"], [os.path.basename(v) for v in fv.videos]
            return "file xong từ lâu → sẵn sàng ngay; file đang ghi/đổi kích thước → chờ; .part/.fNNN bị bỏ qua"
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def t_journal():
        d = tempfile.mkdtemp(prefix="tam_jr_")
        try:
            j = PostJournal(os.path.join(d, "j.jsonl"))
            f = os.path.join(d, "v.mp4")
            with open(f, "wb") as fh:
                fh.write(b"abc")
            k = PostJournal.file_key(f)
            assert k and j.status(k) is None
            j.record("POSTING", [(k, f)], pid="P")
            assert j.status(k) == "POSTING" and j.blocked_entries()
            j.record("UNKNOWN", [(k, f)], pid="P", durable=True)
            j2 = PostJournal(os.path.join(d, "j.jsonl"))
            assert j2.status(k) == "UNKNOWN", "khoá phải còn sau khi khởi động lại"
            j2.record("NOT_POSTED", [(k, f)], pid="manual", durable=True)
            assert not PostJournal(os.path.join(d, "j.jsonl")).blocked_entries(), "NOT_POSTED phải gỡ khoá"
            j.close()
            j2.close()
            return "ghi POSTING→UNKNOWN, khoá còn sau khi khởi động lại, gỡ khoá bằng NOT_POSTED"
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def t_confirm_logic():
        up = TARGET_UPLOAD_URL
        assert _is_upload_url(up), "URL trang upload phải được nhận ra là trang upload"
        assert not CFG.confirm_url_re.search(up), "URL trang upload KHÔNG được khớp mẫu xác nhận (lỗi false-positive của bản gốc)"
        assert CFG.fail_text_re.search("Something went wrong. Please try again")
        return ("WARN", "logic OK (trang upload KHÔNG bị coi là đã đăng). Mẫu xác nhận mặc định là GIẢ ĐỊNH chưa kiểm chứng với TikTok thật → chạy --calibrate-confirm")

    def t_perf_math():
        xs = [100, 110, 120, 130, 140, 150, 160, 170, 180, 400]
        recs = [{"variant": "t", "outcome": "CONFIRMED", "ms": {"FileAttach": float(v)}} for v in xs]
        s = summarize_perf(recs, "variant")["t"]["metrics"]["FileAttach"]
        assert abs(s["median_ms"] - 145.0) < 0.11, s
        assert abs(percentile(xs, 0.95) - 301.0) < 1e-6, percentile(xs, 0.95)      # k=8.55: 180 + 0.55*(400-180)
        tr = PerfTrace(label="t", profile="p")
        tr.mark("SEND_KEYS_START", 1_000_000_000)
        tr.mark("SEND_KEYS_END", 1_250_000_000)
        assert abs(tr.durations()["FileAttach"]["ns"] - 250_000_000) < 10
        tr.mark_unknown("UPLOAD_COMPLETED", "x")
        assert tr.durations()["NetworkUpload"]["ns"] is None, "mốc không đo được phải là UNKNOWN, không được bịa số"
        d, lo, hi = bootstrap_median_diff([100.0] * 8, [150.0] * 8)
        assert d == 50.0 and lo == 50.0 and hi == 50.0
        return "median=145, P95=301 (nội suy) khớp tính tay; mốc thiếu → UNKNOWN; bootstrap xác định"

    def t_stage():
        d = tempfile.mkdtemp(prefix="tam_sg_")
        try:
            f = os.path.join(d, "a.mp4")
            with open(f, "wb") as fh:
                fh.write(b"1" * 100)
            staged, tmp, info = stage_files([f], "selftest")
            if CFG.stage == "ram":
                cleanup_stage_dir(tmp)
                return f"TAM_STAGE=ram: phương pháp={info.get('method')}"
            assert staged == [f] and tmp is None and info.get("method") == "direct"
            return "TAM_STAGE=direct: dùng thẳng file nguồn (không copy/hardlink)"
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def t_js():
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            return ("SKIP", "không có `node` để kiểm cú pháp JavaScript (sẽ được kiểm ở lần chạy thật: install_watcher báo lỗi nếu hỏng)")
        import subprocess
        bad = []
        names = ("JS_INSTALL", "JS_WAIT_READY", "JS_POLL_READY", "JS_WAIT_CONFIRM", "JS_SNAPSHOT", "JS_WATCH_ONLY", "JS_REARM",
                 "JS_DISPOSE", "JS_WAIT_FILE_INPUT", "JS_CALIB", "SYNC_JS",
                 "JS_REC_INSTALL", "JS_REC_READ", "JS_ENV", "JS_SPARSE_INSTALL", "JS_SPARSE_POLL", "JS_PAGE_AGE",
                 "JS_LITE_INSTALL", "JS_LITE_WAIT", "JS_LITE_DISPOSE", "JS_SPARSE_PREP", "JS_SPARSE_STATE")
        for name in names:
            code = globals()[name]
            try:
                r = subprocess.run([node, "-e", "new Function(require('fs').readFileSync(0,'utf8'))"], input=code.encode("utf-8"),
                                   capture_output=True, timeout=15)
                if r.returncode != 0:
                    bad.append(f"{name}: {r.stderr.decode('utf-8', 'replace').strip()[:120]}")
            except Exception as e:
                bad.append(f"{name}: {e}")
        assert not bad, "; ".join(bad)
        return f"{len(names)} đoạn JavaScript hợp lệ cú pháp (node)"

    for nm, fn in (("Python", t_python), ("File backend", t_backend_file), ("Thư viện", t_deps), ("chromedriver", t_driver),
                   ("Máy trạng thái FSM", t_fsm), ("Phân phối profile/limit", t_plan), ("Cổng file ghi xong", t_filegate),
                   ("Nhật ký chống đăng trùng", t_journal), ("Logic xác nhận đăng", t_confirm_logic), ("Phép đo PERF", t_perf_math),
                   ("Stage", t_stage), ("JavaScript", t_js)):
        check(nm, fn)

    icon = {"OK": "✅", "FAIL": "❌", "WARN": "⚠️ ", "SKIP": "⏭️ "}
    print(f"\n=== SELFTEST {BACKEND_NAME} v{BACKEND_VERSION} ===")
    for st, nm, d in rows:
        print(f"{icon[st]} {nm:<28} {d}")
    fails = sum(1 for r in rows if r[0] == "FAIL")
    print(f"\n{'KẾT QUẢ: ' + ('CÓ LỖI' if fails else 'ĐẠT')}  ({sum(1 for r in rows if r[0] == 'OK')} OK, {fails} FAIL, "
          f"{sum(1 for r in rows if r[0] == 'WARN')} WARN, {sum(1 for r in rows if r[0] == 'SKIP')} SKIP).  "
          "Selftest KHÔNG thay thế việc thử trên TikTok thật (chạy --bench-upload / --calibrate-confirm).")
    return 1 if fails else 0


# ----------------------------------------------------- các lệnh cần mở profile ----
def _open_one(pid: str):
    if webdriver is None or Genlogin is None:
        print("❌ Thiếu selenium hoặc Genlogin — không mở được profile.")
        return None
    print(f"🔓 Đang mở profile {pid} ...")
    _, drv = setup_and_navigate_profile(str(pid), None, CHROME_DRIVER_PATH, TARGET_UPLOAD_URL)
    if not drv:
        print(f"❌ Không mở được profile {pid}.")
        return None
    with drivers_lock:
        drivers[str(pid)] = drv
    return drv


def _close_one(pid: str, drv, keep_open: bool):
    if keep_open:
        print(f"ℹ️ Giữ profile {pid} mở (--keep-open).")
        return
    try:
        drv.quit()
        quit_genlogin_driver(pid, GENLOGIN_API_TOKEN)
    except Exception as e:
        print(f"⚠️ Đóng profile {pid} lỗi: {e}")
    finally:
        with drivers_lock:
            drivers.pop(str(pid), None)
        profile_closed(pid)


def _debug_port(drv) -> Optional[int]:
    addr = getattr(drv, "_tam_dbg_addr", None)
    if not addr:
        try:
            addr = (drv.capabilities.get("goog:chromeOptions") or {}).get("debuggerAddress")
        except Exception:
            addr = None
    try:
        return int(str(addr).rsplit(":", 1)[1])
    except Exception:
        return None


def _cpu_snapshot(drv) -> Dict[str, Optional[float]]:
    """CPU-giây (user+system) tích luỹ: python / chromedriver / chrome (tìm theo cổng debug). Không đo được => None."""
    out = {"python": time.process_time(), "chromedriver": None, "chrome": None}       # type: Dict[str, Optional[float]]
    if psutil is None:
        return out
    try:
        me = psutil.Process(os.getpid())
        tot, found = 0.0, False
        for c in me.children(recursive=True):
            if "chromedriver" in (c.name() or "").lower():
                t = c.cpu_times()
                tot += t.user + t.system
                found = True
        out["chromedriver"] = tot if found else None
    except Exception:
        pass
    port = _debug_port(drv)
    if port:
        try:
            pid = None
            for conn in psutil.net_connections(kind="tcp"):
                if conn.status == "LISTEN" and conn.laddr and conn.laddr.port == port and conn.pid:
                    pid = conn.pid
                    break
            if pid:
                p = psutil.Process(pid)
                tot = 0.0
                for q in [p] + p.children(recursive=True):
                    try:
                        t = q.cpu_times()
                        tot += t.user + t.system
                    except Exception:
                        pass
                out["chrome"] = tot
        except Exception:
            pass
    return out


class _CountingDriver(object):
    """Đếm số lệnh WebDriver đã gửi (execute_script / execute_async_script / mọi lệnh khác) mà không đổi hành vi.
    Selenium thật: mọi lệnh đi qua WebDriver.execute => bọc hàm đó. Driver giả (kiểm thử) có `command_count` => dùng hiệu số.
    Không đếm được cách nào => count = None (UNKNOWN), KHÔNG báo 0."""

    def __init__(self, drv):
        self._d = drv
        self._n = 0
        self._orig = None
        self._base = None
        self._final = None
        self._released = False
        orig = getattr(drv, "execute", None)
        if callable(orig):
            def counting(*a, **k):
                self._n += 1
                return orig(*a, **k)
            try:
                drv.execute = counting
                self._orig = orig
            except Exception:
                self._orig = None
        if self._orig is None and isinstance(getattr(drv, "command_count", None), int):
            self._base = int(drv.command_count)

    def _current(self) -> Optional[int]:
        if self._orig is not None:
            return self._n
        if self._base is not None:
            return int(self._d.command_count) - self._base
        return None

    @property
    def count(self) -> Optional[int]:
        return self._final if self._released else self._current()

    def release(self):
        self._final = self._current()
        self._released = True
        if self._orig is not None:
            try:
                self._d.execute = self._orig
            except Exception:
                pass


def _pct_list(xs: List[float]) -> Dict[str, Any]:
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"n": 0}
    return {"n": len(xs), "median": round(statistics.median(xs), 3), "p90": round(percentile(xs, 0.90), 3),
            "p95": round(percentile(xs, 0.95), 3), "max": round(max(xs), 3)}


def cli_bench_overhead(pid: str, secs: float, keep_open: bool) -> int:
    drv = _open_one(pid)
    if drv is None:
        return 3
    try:
        fsm = get_fsm(pid)
        fsm.dirty = True
        el, why = ensure_upload_page(drv, fsm, print, None)
        if el is None:
            print(f"❌ Trang Upload chưa sẵn sàng: {why}")
            return 3
        # 1) RTT WebDriver
        rtts = []
        for _ in range(120):
            t0 = time.perf_counter_ns()
            drv.execute_script("return 1")
            rtts.append((time.perf_counter_ns() - t0) / 1e6)
        rtt = _pct_list(rtts)
        print(f"\nRTT một lệnh WebDriver (execute_script 'return 1', {rtt['n']} mẫu): median {rtt['median']} ms | P95 {rtt['p95']} ms | max {rtt['max']} ms")

        report = {"profile": pid, "secs": secs, "rtt_ms": rtt, "modes": {}, "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                  "note": "Đo trên trang Upload hiện tại, KHÔNG gắn file, KHÔNG đăng. Nút Post chưa sáng nên cả hai chế độ chờ đủ thời gian."}
        results = []
        old_mode = CFG.wait_mode
        try:
            for mode in ("idle", "event", "poll", "event", "poll"):         # xen kẽ để triệt tiêu trôi tải
                counter = _CountingDriver(drv)
                c0 = _cpu_snapshot(drv)
                t0 = time.perf_counter()
                extra = {}      # type: Dict[str, Any]
                if mode == "idle":
                    time.sleep(secs)
                else:
                    CFG.wait_mode = mode
                    install_watcher(drv)
                    stats = {}      # type: Dict[str, Any]
                    st, last = wait_post_ready(drv, lambda: False, secs, stats)
                    extra = {"state": st, "slices": stats.get("slices"), "polls": stats.get("polls")}
                    try:
                        extra["page_stats"] = drv.execute_script("var S=window.__tam5; return S? S.stats : null;")
                    except Exception:
                        pass
                    dispose_watcher(drv)
                wall = time.perf_counter() - t0
                c1 = _cpu_snapshot(drv)
                counter.release()
                row = {"mode": mode, "wall_s": round(wall, 3), "webdriver_commands": counter.count, **extra}
                for k in ("python", "chromedriver", "chrome"):
                    row["cpu_" + k + "_s"] = None if (c0[k] is None or c1[k] is None) else round(c1[k] - c0[k], 3)
                results.append(row)
                print("  ", row)
        finally:
            CFG.wait_mode = old_mode
        for mode in ("idle", "event", "poll"):
            rows = [r for r in results if r["mode"] == mode]
            agg = {"webdriver_commands": _pct_list([r["webdriver_commands"] for r in rows])}
            for k in ("python", "chromedriver", "chrome"):
                agg["cpu_" + k + "_s"] = _pct_list([r["cpu_" + k + "_s"] for r in rows if r.get("cpu_" + k + "_s") is not None])
            if mode != "idle":
                agg["states"] = sorted({str(r.get("state")) for r in rows})
            report["modes"][mode] = agg
        report["runs"] = results
        out = os.path.join(BASE_DIR, f"tam_bench_overhead_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        try:
            with open(out, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            print(f"\nĐã lưu: {out}")
        except Exception as e:
            print(f"⚠️ Không lưu được báo cáo: {e}")
        print("\nTÓM TẮT (median mỗi chế độ, mỗi lần chờ %.0f s):" % secs)
        for mode, agg in report["modes"].items():
            print(f"  {mode:<6} lệnh WebDriver={agg['webdriver_commands'].get('median', 'UNKNOWN')}  "
                  f"CPU python={agg['cpu_python_s'].get('median', 'UNKNOWN')}s  chromedriver={agg['cpu_chromedriver_s'].get('median', 'UNKNOWN')}s  "
                  f"chrome={agg['cpu_chrome_s'].get('median', 'UNKNOWN')}s")
        if any(r.get("state") == "ready" for r in results):
            print("⚠️ Có lần nút Post đã sáng sẵn (không cần gắn file) nên lần đó KHÔNG đo được chi phí chờ — kết quả event/poll không đáng tin.")
        print("Ý nghĩa: so 'poll' với 'event' ⇒ chi phí điều khiển của polling kiểu cũ trên máy BẠN. Không suy ra thời gian đăng nhanh hơn bao nhiêu — "
              "việc đó chỉ đo được bằng --bench-upload / PERF thật.")
        return 0
    finally:
        _close_one(pid, drv, keep_open)


def cli_bench_upload(pid: str, files: List[str], repeat: int, yes: bool, keep_open: bool) -> int:
    files = [os.path.abspath(f) for f in files]
    miss = [f for f in files if not os.path.isfile(f)]
    if miss:
        print("Không thấy file: " + ", ".join(miss))
        return 2
    print(f"Chạy thử {repeat} lượt: gắn {len(files)} file vào trang Upload của profile {pid}, chờ nút Post sáng rồi DỪNG.\n"
          f"KHÔNG bấm Post — không có video nào được đăng. Cấu hình: variant={CFG.variant} wait={CFG.wait_mode} stage={CFG.stage} "
          f"force_reload={CFG.force_reload}")
    if not yes:
        if input("Tiếp tục? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Đã huỷ.")
            return 1
    drv = _open_one(pid)
    if drv is None:
        return 3
    recs = []
    try:
        n_bytes = sum((stat_file(f) or (0, 0))[0] for f in files)
        for i in range(max(1, repeat)):
            tr = PerfTrace(label=f"bench#{i + 1}", profile=pid, mode="BENCH", n_files=len(files), n_bytes=n_bytes)
            tr.mark_unknown("FILE_READY", "bench: file có sẵn")
            tr.mark("FILE_DETECTED")
            staged, tmp, _info = stage_files(files, "benchup", tr)
            tr.set("stage_mode", CFG.stage)
            tr.set("variant", CFG.variant)
            tr.set("concurrency", 1)
            try:
                res = upload_video_to_tiktok_ex(drv, staged, logger_func=print, check_force_kill=None, pid=pid, mode="BENCH", trace=tr, dry_run=True)
            finally:
                cleanup_stage_dir(tmp)
            tr.outcome = res.status.value
            print(tr.render())
            if CFG.perf_enable:
                PERF_LOG.write(tr.record())
            recs.append(tr.record())
            if res.status != PostStatus.DRY_RUN:
                print(f"⚠️ Lượt {i + 1} kết thúc {res.status.value}: {res.reason}")
        print("\n=== TÓM TẮT (dry-run: ClickToConfirmed/TotalConfirmed luôn UNKNOWN vì không bấm Post) ===")
        summ = summarize_perf(recs, "variant")
        for g, s in summ.items():
            print(f"\nvariant={g}: {s['jobs']} lượt")
            print(f"{'chỉ số':18} {'n':>3} {'median(ms)':>12} {'P95(ms)':>12}")
            for name, m in s["metrics"].items():
                if m["n"]:
                    print(f"{name:18} {m['n']:>3} {m['median_ms']:>12.1f} {m['p95_ms']:>12.1f}")
        print("\nSo sánh biến thể: đổi TAM_VARIANT/--wait-mode/--stage/--force-reload rồi chạy lại xen kẽ; "
              "xem --perf-summary --compare A B. Mốc UPLOAD_COMPLETED chỉ có khi đặt TAM_UPLOAD_DONE_SEL / TAM_UPLOAD_DONE_TEXT_RE.")
        return 0
    finally:
        _close_one(pid, drv, keep_open)


def cli_bench_watch(pid: str, files: List[str], repeat: int, yes: bool, keep_open: bool, modes: str = "ABC",
                    states: str = "fresh", idle_s: float = 120.0) -> int:
    """A/B/C trên TikTok THẬT, cùng profile, cùng file, mỗi lượt nạp lại trang Upload sạch, thứ tự xoay vòng.
    A = event (watcher hiện tại) | B = sparse (không cài gì khi upload) | C = lite (chỉ theo dõi chính nút Post).
    LUÔN dry-run: chạy tới khi nút Post sáng rồi DỪNG — KHÔNG BAO GIỜ bấm Post, không video nào được đăng."""
    names = {"A": "event", "B": "sparse", "C": "lite"}
    modes = [m for m in modes.upper() if m in names] or ["A", "B", "C"]
    # TRẠNG THÁI TRANG lúc gắn file: fresh = vừa nạp lại | idle = nạp lại rồi để rảnh idle_s giây (giống trang mở sẵn chờ video)
    # | front = Page.bringToFront ngay trước khi gắn (tab hiển thị). Đo để biết trang mở sẵn lâu có làm POST_READY chậm không.
    sts = [x.strip().lower() for x in str(states or "fresh").split(",") if x.strip().lower() in ("fresh", "idle", "front")] or ["fresh"]
    combos = [(m, st) for st in sts for m in modes]
    files = [os.path.abspath(f) for f in files]
    miss = [f for f in files if not os.path.isfile(f)]
    if miss:
        print("Không thấy file: " + ", ".join(miss))
        return 2
    print(f"BENCH WATCH: profile {pid} | {len(files)} file | {repeat} vòng × {''.join(modes)} (thứ tự xoay vòng) | DRY-RUN: KHÔNG bấm Post.\n"
          f"Mỗi lượt gắn file vào trang Upload rồi DỪNG khi nút Post sáng — TikTok có thể giữ bản nháp tải lên; tool nạp lại trang "
          f"trước lượt sau. Bộ ghi thụ động (long task, Resource Timing) bật GIỐNG NHAU cho mọi chế độ.")
    if not yes and input("Tiếp tục? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Đã huỷ.")
        return 1
    drv = _open_one(pid)
    if drv is None:
        return 3
    saved = (CFG.wait_mode, CFG.rec_enable)
    rows = []   # type: List[Dict[str, Any]]
    try:
        CFG.rec_enable = True
        n_bytes = sum((stat_file(f) or (0, 0))[0] for f in files)
        for r in range(max(1, repeat)):
            order = combos[r % len(combos):] + combos[:r % len(combos)]
            if r % 2:
                order = order[::-1]
            for m, pst in order:
                CFG.wait_mode = names[m]
                get_fsm(pid).dirty = True                       # mỗi lượt: trang Upload SẠCH vừa nạp
                if pst == "idle":                               # nạp sạch TRƯỚC rồi để trang rảnh như khi chờ video thật
                    prepare_upload_page(drv, lambda *_: None, None, get_fsm(pid))
                    time.sleep(max(0.0, idle_s))                # chỉ trong công cụ đo, không có trong đường đăng thật
                saved_front = CFG.bring_to_front
                CFG.bring_to_front = (pst == "front")
                snap = {}       # type: Dict[str, Any]
                counter = _CountingDriver(drv)

                def hook(msg, snap=snap, counter=counter):
                    if "[Timing] T1" in msg:
                        snap.update(t0=time.perf_counter(), c0=_cpu_snapshot(drv), n0=counter.count, m0=cdp_metrics(drv))
                    elif "💡 [READY]" in msg:
                        snap.update(t1=time.perf_counter(), c1=_cpu_snapshot(drv), n1=counter.count, m1=cdp_metrics(drv))
                tr = PerfTrace(label=f"watch-{m}#{r + 1}", profile=pid, mode="BENCH-WATCH", n_files=len(files), n_bytes=n_bytes)
                tr.mark_unknown("FILE_READY", "bench")
                staged, tmp, _info = stage_files(files, "benchwatch", tr)
                try:
                    res = upload_video_to_tiktok_ex(drv, staged, logger_func=hook, check_force_kill=None, pid=pid, mode="BENCH-WATCH",
                                                    trace=tr, dry_run=True)
                finally:
                    cleanup_stage_dir(tmp)
                    counter.release()
                    CFG.bring_to_front = saved_front
                rec = tr.record()
                fl = rec.get("flags") or {}
                row = {"mode": m, "page_state": pst, "cell": f"{m}/{pst}", "wait_mode": names[m], "round": r + 1,
                       "result": res.status.value, "page_reloaded": fl.get("page_reloaded"), "vis_ready": fl.get("vis_ready"),
                       "attach_to_ready_py_ms": round((snap["t1"] - snap["t0"]) * 1000) if "t1" in snap and "t0" in snap else None,
                       "attach_to_ready_page_ms": (rec.get("ms") or {}).get("AttachToReadyPage"),
                       "webdriver_cmds": (snap["n1"] - snap["n0"]) if snap.get("n1") is not None and snap.get("n0") is not None else None}
                for k in ("python", "chromedriver", "chrome"):
                    a0, a1 = (snap.get("c0") or {}).get(k), (snap.get("c1") or {}).get(k)
                    row["cpu_" + k + "_s"] = None if a0 is None or a1 is None else round(a1 - a0, 3)
                for k, v in cdp_delta(snap.get("m0"), snap.get("m1")).items():
                    row[k] = v
                for k in ("x_longtask_count", "x_longtask_ms", "x_longtask_max_ms", "x_net_span_ms", "x_net_busy_ms",
                          "x_after_net_to_ready_ms", "handoff_ms", "light_calls", "slices", "watch_stats", "rec_res_top"):
                    if k in fl:
                        row[k] = fl[k]
                rows.append(row)
                if CFG.perf_enable:
                    PERF_LOG.write(rec)
                print(f"  vòng {r + 1} {m}({names[m]})/{pst}: gắn→READY {row['attach_to_ready_py_ms']} ms | trang {row['attach_to_ready_page_ms']} | "
                      f"CPU chrome {row['cpu_chrome_s']} s | lệnh {row['webdriver_cmds']} | long task {row.get('x_longtask_ms')} ms | "
                      f"{row['result']}" + ("" if res.status == PostStatus.DRY_RUN else f" ({res.reason})"))
        rep = {"profile": pid, "files": [os.path.basename(f) for f in files], "bytes": n_bytes, "repeat": repeat,
               "ts": datetime.datetime.now().isoformat(timespec="seconds"), "rows": rows, "summary": {}}
        cells = [f"{m}/{st}" for m, st in combos]
        for m in cells:
            rs = [x for x in rows if x["cell"] == m and x["result"] == PostStatus.DRY_RUN.value]
            rep["summary"][m] = {k: _pct_list([x.get(k) for x in rs if isinstance(x.get(k), (int, float))])
                                 for k in ("attach_to_ready_py_ms", "attach_to_ready_page_ms", "cpu_chrome_s", "cpu_python_s",
                                           "webdriver_cmds", "x_longtask_ms", "x_task_ms", "x_layout_count", "x_recalcstyle_count",
                                           "x_net_span_ms")}
        out = os.path.join(BASE_DIR, f"tam_bench_watch_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=1)
        print("\n=== TÓM TẮT (median | P90 | P95) — DRY-RUN, không bấm Post ===")
        print(f"{'chế độ/trang':16} {'gắn→READY ms':>22} {'CPU chrome s':>20} {'lệnh WD':>14} {'long task ms':>16}")
        for m in cells:
            sm = rep["summary"][m]
            f_ = lambda k: (f"{sm[k].get('median')}|{sm[k].get('p90')}|{sm[k].get('p95')}" if sm[k].get("n") else "-")
            print(f"{m:16} {f_('attach_to_ready_py_ms'):>22} {f_('cpu_chrome_s'):>20} {f_('webdriver_cmds'):>14} {f_('x_longtask_ms'):>16}")
        pairs = [(f"A/{sts[0]}", f"C/{sts[0]}"), (f"A/{sts[0]}", f"B/{sts[0]}")] + [(f"{modes[0]}/fresh", f"{modes[0]}/{st}") for st in sts if st != "fresh" and "fresh" in sts]
        for a_, b_ in pairs:
            xa = [x["attach_to_ready_py_ms"] for x in rows if x["cell"] == a_ and x.get("attach_to_ready_py_ms") is not None]
            xb = [x["attach_to_ready_py_ms"] for x in rows if x["cell"] == b_ and x.get("attach_to_ready_py_ms") is not None]
            if len(xa) >= 3 and len(xb) >= 3:
                d, lo, hi = bootstrap_median_diff(xa, xb)
                print(f"{b_} − {a_} (gắn→READY, median): {d:+.0f} ms, KTC95% [{lo:+.0f}; {hi:+.0f}]"
                      + ("  ⇒ khác biệt CÓ ý nghĩa" if (lo > 0 or hi < 0) else "  ⇒ CHƯA khác biệt rõ (chạy thêm vòng)"))
        print(f"\nĐã lưu: {out}  — gửi file này để phân tích.")
        return 0
    finally:
        CFG.wait_mode, CFG.rec_enable = saved
        _close_one(pid, drv, keep_open)


def _attach_prepare(drv, pid: str, watch: str, kill=lambda: False):
    """Mỗi lượt đo: trang Upload SẠCH vừa nạp (cùng trạng thái cho tool và tay). Trả (sync, info, rec_ok, m0) hoặc None."""
    fsm = get_fsm(pid)
    fsm.dirty = True
    log = lambda m: None
    el, why = ensure_upload_page(drv, fsm, log, kill)
    if el is None:
        print(f"❌ [{pid}] {why}")
        return None
    if CFG.bring_to_front:
        try:
            drv.execute_cdp_cmd("Page.bringToFront", {})
        except Exception:
            pass
    focus = apply_focus_emu(drv)
    if CFG.warm_ms > 0:
        _warm_gate(drv, kill, PerfTrace())
    sync = ClockSync()
    rec_ok, m0 = False, None
    if watch == "sparse":
        r = drv.execute_script(JS_SPARSE_INSTALL)
        info = r if isinstance(r, dict) else {}
    else:
        info = install_watcher(drv, None, sync)
        if CFG.rec_enable:
            rec_ok = bool(rec_install(drv).get("ok"))
            m0 = cdp_metrics(drv)
    return sync, info, rec_ok, m0, focus


def _attach_manual(drv, pid: str, i: int, n: int, watch: str, poll_ms: int, settle: float, files: List[str], timeout_s: float):
    prep = _attach_prepare(drv, pid, watch)
    if prep is None:
        return None
    sync, info, rec_ok, m0, focus = prep
    tr = PerfTrace(label=f"attach-manual#{i}", profile=pid, mode="BENCH-ATTACH", n_files=len(files),
                   n_bytes=sum((stat_file(f) or (0, 0))[0] for f in files))
    tr.sync = sync
    tr.mark("JOB_START")
    tr.mark_unknown("FILE_READY", "bench")
    names = ", ".join(os.path.basename(f) for f in files)
    print(f"\n👉 LƯỢT {i}/{n} (ĐĂNG TAY): trang Upload đã sạch. Hãy TỰ CHỌN file [{names}] bằng hộp thoại chọn file như bình thường. "
          f"KHÔNG bấm Post. (chờ tối đa {timeout_s:.0f}s)")
    if watch == "sparse":
        end = time.perf_counter() + timeout_s
        ready = None
        while time.perf_counter() < end:
            try:
                now, en, att, vis = drv.execute_script(JS_SPARSE_POLL, CFG.sel_post)
            except Exception as e:
                print(f"❌ Mất kết nối trang: {e}")
                return None
            if en and att is not None:
                ready = {"attachedAt": att, "readyAt": now, "doc": info.get("doc"), "vis": vis, "now": now}
                break
            time.sleep(poll_ms / 1000.0)
        if ready is None:
            print("⏱️ Hết giờ chờ — lượt này bỏ.")
            return None
        tr.set("x_poll_ms", poll_ms)
    else:
        state, ready = wait_post_ready(drv, lambda: False, timeout_s, {})
        if state != "ready" or not ready:
            print("⏱️ Hết giờ chờ — lượt này bỏ.")
            return None
    tr.mark_chrome("PROCESSING_READY", ready.get("readyAt"), ready.get("doc"), sync)
    tr.set("vis_ready", ready.get("vis"))
    tr.set("attach_mode", "manual")
    tr.set("watch", watch)
    tr.set("focus_emu", 1 if focus else 0)
    if watch == "sparse":
        tr.mark_chrome("FILE_CHANGE", ready["attachedAt"], ready.get("doc"), sync)
        tr.set("x_page_age_at_attach_ms", round(ready["attachedAt"]))
    else:
        _finish_attach(drv, tr, sync, ready, rec_ok, m0)
    tr.outcome = "DRY_RUN"
    return tr


def _attach_tool(drv, pid: str, i: int, n: int, watch: str, poll_ms: int, files: List[str], barrier=None):
    prep = None
    if watch == "sparse":
        # chế độ thưa: cùng chuẩn bị như tay, rồi send_keys, rồi hỏi thưa -> so được với tay-thưa
        prep = _attach_prepare(drv, pid, "sparse")
        if prep is None:
            return None
        sync, info, _rec_ok, _m0, focus = prep
        tr = PerfTrace(label=f"attach-tool-sparse#{i}", profile=pid, mode="BENCH-ATTACH", n_files=len(files),
                       n_bytes=sum((stat_file(f) or (0, 0))[0] for f in files))
        tr.sync = sync
        tr.mark("JOB_START")
        tr.mark_unknown("FILE_READY", "bench")
        if barrier is not None:
            barrier.wait()
        el = wait_file_input(drv, CFG.page_ready_timeout_s)
        tr.mark("SEND_KEYS_START")
        _send_keys_with_refind(drv, el, "\n".join(files))
        tr.mark("SEND_KEYS_END")
        end = time.perf_counter() + CFG.ready_timeout_s
        ready = None
        while time.perf_counter() < end:
            now, en, att, vis = drv.execute_script(JS_SPARSE_POLL, CFG.sel_post)
            if en and att is not None:
                ready = {"attachedAt": att, "readyAt": now, "doc": info.get("doc"), "vis": vis}
                break
            time.sleep(poll_ms / 1000.0)
        if ready is None:
            print(f"⏱️ [{pid}] hết giờ chờ nút Post.")
            return None
        tr.mark_chrome("PROCESSING_READY", ready["readyAt"], ready["doc"], sync)
        tr.mark_chrome("FILE_CHANGE", ready["attachedAt"], ready["doc"], sync)
        tr.set("x_poll_ms", poll_ms)
        tr.set("x_page_age_at_attach_ms", round(ready["attachedAt"]))
        tr.set("vis_ready", ready.get("vis"))
        tr.set("focus_emu", 1 if focus else 0)
        tr.outcome = "DRY_RUN"
    else:
        fsm = get_fsm(pid)
        fsm.dirty = True
        tr = PerfTrace(label=f"attach-tool#{i}", profile=pid, mode="BENCH-ATTACH", n_files=len(files),
                       n_bytes=sum((stat_file(f) or (0, 0))[0] for f in files))
        tr.mark_unknown("FILE_READY", "bench")
        if barrier is not None:
            barrier.wait()
        res = upload_video_to_tiktok_ex(drv, files, logger_func=lambda m: None, check_force_kill=None, pid=pid, mode="BENCH-ATTACH",
                                        trace=tr, dry_run=True)
        tr.outcome = res.status.value
        if res.status != PostStatus.DRY_RUN:
            print(f"⚠️ [{pid}] lượt {i} kết thúc {res.status.value}: {res.reason}")
            return None
    tr.set("attach_mode", "tool")
    tr.set("watch", watch)
    return tr


def cli_bench_attach(pids: List[str], files: List[str], repeat: int, mode: str, watch: str, poll_ms: int, settle: float,
                     concurrent: bool, yes: bool, keep_open: bool, manual_timeout: float = 180.0) -> int:
    """ĐO AttachToReady: từ lúc file được CHỌN (sự kiện change của ô file) đến lúc nút Post SÁNG — cả hai mốc cùng đồng hồ của TRANG.
    Đăng tay và tool dùng CÙNG profile, file, proxy, mạng; mỗi lượt nạp lại trang Upload sạch. Không bao giờ bấm Post."""
    files = [os.path.abspath(f) for f in files]
    miss = [f for f in files if not os.path.isfile(f)]
    if miss:
        print("Không thấy file: " + ", ".join(miss))
        return 2
    if mode in ("manual", "both") and len(pids) > 1:
        print("Đăng tay chỉ đo 1 profile mỗi lần (bạn chỉ có 1 tay). Dùng 1 profile cho --attach-mode manual/both; "
              "nhiều profile chỉ dùng với --attach-mode tool --concurrent.")
        return 2
    print(f"BENCH ATTACH → READY: profile={','.join(pids)} file={len(files)} lượt={repeat} mode={mode} watch={watch} "
          f"{'poll=%dms ' % poll_ms if watch == 'sparse' else ''}concurrent={concurrent}\n"
          f"Cấu hình: variant={CFG.variant} stage={CFG.stage} force_reload={CFG.force_reload} focus_emu={CFG.focus_emu} "
          f"bring_to_front={CFG.bring_to_front} warm_ms={CFG.warm_ms} upload_url_re={'có' if CFG.upload_url_re else 'CHƯA ĐẶT (NetworkUpload/TikTokProcessing sẽ UNKNOWN)'}\n"
          f"KHÔNG bấm Post, không video nào được đăng.")
    if not yes and input("Tiếp tục? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Đã huỷ.")
        return 1
    drvs = {}
    saved_rec = CFG.rec_enable
    CFG.rec_enable = (watch != "sparse")          # bench luôn bật bộ ghi thụ động (trừ chế độ thưa: cố ý không cài gì)
    for pid in pids:
        d = _open_one(pid)
        if d is None:
            for p2, d2 in drvs.items():
                _close_one(p2, d2, keep_open)
            return 3
        drvs[pid] = d
    recs = []   # type: List[Dict[str, Any]]
    stage_tmp = []
    try:
        def stage_for(pid):
            st, tmp, _ = stage_files(files, "benchatt" + str(pid), None)
            stage_tmp.append(tmp)
            return st
        order = []
        for i in range(1, repeat + 1):
            if mode == "both":
                order.append(("manual" if i % 2 == 1 else "tool", i))      # xen kẽ để trôi theo thời gian không dồn vào một nhóm
                order.append(("tool" if i % 2 == 1 else "manual", i))
            else:
                order.append((mode, i))
        for k, (m, i) in enumerate(order, 1):
            if m == "manual":
                pid = pids[0]
                tr = _attach_manual(drvs[pid], pid, i, repeat, watch, poll_ms, settle, files, manual_timeout)
                trs = [tr]
            else:
                bar = threading.Barrier(len(pids)) if concurrent and len(pids) > 1 else None
                out = {}
                def work(pid):
                    out[pid] = _attach_tool(drvs[pid], pid, i, repeat, watch, poll_ms, stage_for(pid), bar)
                if bar is not None:
                    ths = [threading.Thread(target=work, args=(p,)) for p in pids]
                    [t.start() for t in ths]
                    [t.join() for t in ths]
                else:
                    for p in pids:
                        work(p)
                trs = [out.get(p) for p in pids]
            for tr in trs:
                if tr is None:
                    continue
                tr.set("cell", f"{tr.flags.get('attach_mode')}/{tr.flags.get('watch')}")
                tr.set("concurrency", len(pids) if concurrent else 1)
                tr.set("variant", CFG.variant)
                tr.set("stage_mode", CFG.stage)
                r = tr.record()
                recs.append(r)
                if CFG.perf_enable:
                    PERF_LOG.write(r)
                v = (r.get("ms") or {}).get("AttachToReadyPage")
                print(f"  [{tr.profile}] {tr.flags.get('attach_mode'):6} lượt {i}: AttachToReadyPage = "
                      f"{('%.0f ms' % v) if v is not None else 'UNKNOWN'} | tab={tr.flags.get('vis_ready')} "
                      f"| tuổi trang lúc gắn={tr.flags.get('x_page_age_at_attach_ms', '?')} ms")
        print("\n=== TÓM TẮT (đồng hồ TRANG; dry-run, không bấm Post) ===")
        if not recs:
            print("Không có lượt hợp lệ.")
            return 4
        summ = summarize_perf(recs, "cell")
        for g, s in summ.items():
            print(f"\n[{g}] {s['jobs']} lượt")
            print(format_perf_summary({g: s}).rstrip())
        cells = sorted(summ.keys())
        if len(cells) >= 2:
            a = [c for c in cells if c.startswith("manual")]
            b = [c for c in cells if c.startswith("tool")]
            if a and b:
                print("\n" + compare_perf_groups(recs, "cell", a[0], b[0]))
        print("\nĐọc kết quả: chênh lệch tool−tay ở AttachToReadyPage là con số cần thu hẹp. Nếu x_hidden_any=1 hoặc vis_ready≠visible: "
              "thử TAM_FOCUS_EMU=1 / TAM_BRING_TO_FRONT=1. Tuổi trang lúc gắn nhỏ: thử TAM_WARM_MS=1500. "
              "NetworkUpload/TikTokProcessing chỉ có khi TAM_UPLOAD_URL_RE khớp yêu cầu upload (xem rec_res_top ở nhật ký PERF); nếu không => UNKNOWN. Lưu ý: trình duyệt chỉ ghi Resource Timing của yêu cầu fetch/XHR sau khi trang đọc xong phần thân phản hồi; nếu trang không đọc, mục này có thể không bao giờ xuất hiện => UNKNOWN (không phải lỗi công cụ).")
        return 0
    finally:
        CFG.rec_enable = saved_rec
        for t in stage_tmp:
            cleanup_stage_dir(t)
        for p, d in drvs.items():
            _close_one(p, d, keep_open)


def cli_calibrate_confirm(pid: str, secs: float, keep_open: bool) -> int:
    drv = _open_one(pid)
    if drv is None:
        return 3
    rows = []       # type: List[Dict[str, Any]]
    try:
        print(f"\n👀 Hiệu chuẩn bằng chứng 'đã đăng' trong {secs:.0f}s. Công cụ CHỈ QUAN SÁT, KHÔNG bấm gì.\n"
              "   Bây giờ hãy TỰ TAY: chọn 1 video → chờ nút Post sáng → bấm Post → chờ TikTok báo xong. Ctrl+C để kết thúc sớm.")
        t0 = time.perf_counter()
        last_url = None
        seen_py = set()
        try:
            while time.perf_counter() - t0 < secs:
                try:
                    if not drv.execute_script("return !!window.__tamCalib"):
                        drv.execute_script(JS_CALIB)
                    got = drv.execute_script("var C=window.__tamCalib; if(!C) return null; return C.rows.splice(0);") or []
                except Exception:
                    got = []
                try:
                    url = drv.current_url
                except Exception:
                    url = last_url
                if url and url != last_url:
                    got = list(got) + [{"t": int((time.perf_counter() - t0) * 1000), "kind": "url(py)", "text": url, "url": url}]
                    last_url = url
                for r in got:
                    key = (r.get("kind"), r.get("text"))
                    if key in seen_py:
                        continue
                    seen_py.add(key)
                    r["t_py_ms"] = int((time.perf_counter() - t0) * 1000)
                    rows.append(r)
                    print(f"  +{r['t_py_ms']:>6} ms  [{r.get('kind'):<7}] {str(r.get('text'))[:110]}")
                time.sleep(0.12)
        except KeyboardInterrupt:
            print("\n(đã dừng sớm)")
        urls = []
        for r in rows:
            u = r.get("url")
            if u and u not in urls:
                urls.append(u)
        post_urls = [u for u in urls if not _is_upload_url(u)]
        kw = re.compile(r"(post|upload|publish|đăng|tải lên|đã|success|thành công|manage|view)", re.I)
        texts = [r["text"] for r in rows if r.get("kind") in ("text", "dialog") and kw.search(str(r.get("text")))]
        out = os.path.join(BASE_DIR, f"tam_calibration_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        try:
            with open(out, "w", encoding="utf-8") as f:
                json.dump({"profile": pid, "urls": urls, "rows": rows, "ts": datetime.datetime.now().isoformat(timespec="seconds")},
                          f, ensure_ascii=False, indent=2)
            print(f"\nĐã lưu: {out}")
        except Exception as e:
            print(f"⚠️ Không lưu được: {e}")
        print("\n=== ỨNG VIÊN (bạn tự xem và quyết định; công cụ KHÔNG tự áp dụng) ===")
        print("URL khác trang Upload sau khi đăng:", post_urls or "(không thấy URL nào khác)")
        for u in post_urls[:5]:
            path = urllib.parse.urlparse(u).path.strip("/")
            if path:
                print(f"   gợi ý  TAM_CONFIRM_URL_RE={re.escape(path)}")
        print("Văn bản mới xuất hiện có từ khoá:")
        for t in texts[:12]:
            print("   -", str(t)[:140])
        print("   gợi ý  TAM_CONFIRM_TEXT_RE=<cụm từ ổn định, đặc trưng cho thông báo thành công, KHÔNG chung chung như 'post'>")
        print("Đặt biến môi trường (hoặc file .bat) rồi chạy lại. Hiện đang dùng:\n"
              f"   URL : {CFG.confirm_url_re.pattern}\n   TEXT: {CFG.confirm_text_re.pattern}\n   FAIL: {CFG.fail_text_re.pattern}")
        return 0
    finally:
        _close_one(pid, drv, keep_open)


# ----------------------------------------------------------------- GUI một nhóm ----
def run_gui() -> int:
    missing = [n for n, ok in (("selenium", webdriver is not None), ("Genlogin", Genlogin is not None), ("PySide6", HAS_QT)) if not ok]
    if missing:
        print("Lỗi: Thiếu dependencies: " + ", ".join(missing))
        try:
            input("Enter to exit...")
        except Exception:
            pass
        return 1
    try:
        app = QApplication(sys.argv)
        app.setApplicationName("ProfileUploader")
        app.setStyleSheet("QWidget { color: #f0f0f0; } QToolTip { background-color: #3e3e3e; color: #f0f0f0; border: 1px solid #555; }")
        window = PremiumApp()
        window.show()
        return app.exec()
    except Exception as e:
        print(f"Lỗi khởi động: {e}")
        try:
            input("Enter to exit...")
        except Exception:
            pass
        return 1


# ======================================================================================
# PHẦN 14 — ĐIỂM VÀO
#   python tam_INSTANT_POST.py                 -> GUI một nhóm (như bản cũ)
#   python tam_INSTANT_POST.py --help          -> các công cụ dòng lệnh
# GUI nhiều nhóm (nhomtamquaylai_INSTANT_POST.py) KHÔNG chạy file này mà `import tam_INSTANT_POST`.
# ======================================================================================
def build_parser() -> "argparse.ArgumentParser":
    ap = argparse.ArgumentParser(
        prog="tam_INSTANT_POST.py", formatter_class=argparse.RawDescriptionHelpFormatter,
        description=f"{BACKEND_NAME} v{BACKEND_VERSION} (api {BACKEND_API_LEVEL}) — backend upload TikTok. Không có tham số => mở GUI một nhóm.",
        epilog=(
            "Ví dụ:\n"
            "  python tam_INSTANT_POST.py --selftest\n"
            "  python tam_INSTANT_POST.py --calibrate-confirm --profile 12345            # quan sát khi bạn TỰ đăng 1 video, KHÔNG bấm gì\n"
            "  python tam_INSTANT_POST.py --bench-overhead --profile 12345 --secs 10     # chi phí chờ nút: event vs poll (không đăng)\n"
            "  python tam_INSTANT_POST.py --bench-watch --profile 12345 --files a.mp4 --repeat 6   # A/B/C watcher nút Post (không đăng)\n"
            "  python tam_INSTANT_POST.py --bench-upload --profile 12345 --files a.mp4 --repeat 5 --variant direct\n"
            "  python tam_INSTANT_POST.py --bench-upload --profile 12345 --files a.mp4 --repeat 5 --variant ram --stage ram\n"
            "  python tam_INSTANT_POST.py --bench-attach --profile 12345 --files a.mp4 --attach-mode both --repeat 10   # tay vs tool, cùng trang/profile/file\n"
            "  TAM_FOCUS_EMU=1 python tam_INSTANT_POST.py --bench-attach --profile 12345 --files a.mp4 --attach-mode tool --variant focusemu\n"
            "  python tam_INSTANT_POST.py --perf-summary --by variant --compare direct ram\n"
            "  python tam_INSTANT_POST.py --journal-show\n"
            "  python tam_INSTANT_POST.py --journal-resolve posted --keys 1a2b3c4d5e6f7a8b9c0d\n"))
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--version", action="store_true", help="in thông tin backend (tên, phiên bản, file, sha256) rồi thoát")
    g.add_argument("--selftest", action="store_true", help="kiểm tra nội bộ không cần trình duyệt/TikTok")
    g.add_argument("--perf-summary", action="store_true", help="median/P95/tỷ lệ thành công từ nhật ký PERF")
    g.add_argument("--journal-show", action="store_true", help="liệt kê file đang bị khoá vì nghi đã đăng")
    g.add_argument("--journal-resolve", choices=["posted", "not-posted"], help="gỡ khoá sau khi bạn kiểm tra tay (cần --keys hoặc --all)")
    g.add_argument("--calibrate-confirm", action="store_true", help="quan sát (không bấm gì) khi bạn tự đăng 1 video; cần --profile")
    g.add_argument("--bench-overhead", action="store_true", help="đo chi phí chờ nút event vs poll trên profile thật; cần --profile")
    g.add_argument("--bench-upload", action="store_true", help="chạy thử tới lúc nút Post sáng rồi DỪNG (không đăng); cần --profile --files")
    g.add_argument("--bench-watch", action="store_true", help="A/B/C cơ chế theo dõi nút Post trên TikTok thật (DRY-RUN, không bấm Post); cần --profile --files")
    g.add_argument("--bench-attach", action="store_true", help="đo AttachToReady (file được chọn -> nút Post sáng): đăng tay vs tool, không đăng; cần --profile --files")
    ap.add_argument("--attach-mode", choices=["tool", "manual", "both"], default="both", help="--bench-attach: tool | manual (bạn tự chọn file) | both (xen kẽ ABBA)")
    ap.add_argument("--watch", choices=["event", "sparse"], default="event", help="--bench-attach: event = watcher+bộ ghi; sparse = không observer, hỏi thưa (đo tải do công cụ gây ra)")
    ap.add_argument("--poll-ms", type=int, default=100, help="--bench-attach --watch sparse: chu kỳ hỏi (độ phân giải đo)")
    ap.add_argument("--concurrent", action="store_true", help="--bench-attach --attach-mode tool: chạy NHIỀU profile (--profile a,b,c) cùng lúc, có rào đồng bộ")
    ap.add_argument("--manual-timeout", type=float, default=180.0, help="--bench-attach: giây chờ bạn chọn file mỗi lượt")
    ap.add_argument("--perf-log", help="đường dẫn nhật ký PERF (mặc định tam_perf_log.jsonl cạnh file này)")
    ap.add_argument("--by", default="variant", help="gom nhóm khi tóm tắt: variant | mode | profile | label | stage_mode | wait_mode | concurrency ...")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"), help="so sánh hai nhóm của --by (median, khoảng tin cậy 95%% bootstrap)")
    ap.add_argument("--keys", nargs="+", help="các key (xem --journal-show)")
    ap.add_argument("--all", action="store_true", help="áp dụng cho mọi file đang khoá")
    ap.add_argument("--yes", action="store_true", help="không hỏi xác nhận")
    ap.add_argument("--profile", help="Profile ID Genlogin")
    ap.add_argument("--files", nargs="+", help="file video dùng cho --bench-upload")
    ap.add_argument("--repeat", type=int, default=3, help="số lượt cho --bench-upload (mặc định 3)")
    ap.add_argument("--secs", type=float, default=None, help="thời gian đo / quan sát (giây)")
    ap.add_argument("--variant", help="nhãn ghi vào PERF (TAM_VARIANT)")
    ap.add_argument("--wait-mode", choices=["event", "sparse", "lite", "poll"], help="đặt TAM_WAIT_MODE cho lần chạy này (event=A, sparse=B, lite=C)")
    ap.add_argument("--modes", default="ABC", help="--bench-watch: các chế độ so sánh (mặc định ABC)")
    ap.add_argument("--page-states", default="fresh", help="--bench-watch: fresh,idle,front (trạng thái trang lúc gắn file)")
    ap.add_argument("--idle-s", type=float, default=120.0, help="--bench-watch --page-states idle: số giây trang rảnh trước khi gắn")
    ap.add_argument("--stage", choices=["direct", "ram"], help="đặt TAM_STAGE cho lần chạy này")
    ap.add_argument("--force-reload", action="store_true", help="đặt TAM_FORCE_RELOAD=1 cho lần chạy này")
    ap.add_argument("--keep-open", action="store_true", help="không đóng profile sau khi đo")
    return ap


def _apply_cli_overrides(a) -> None:
    changed = False
    for val, env in ((a.variant, "TAM_VARIANT"), (a.wait_mode, "TAM_WAIT_MODE"), (a.stage, "TAM_STAGE")):
        if val:
            os.environ[env] = val
            changed = True
    if a.force_reload:
        os.environ["TAM_FORCE_RELOAD"] = "1"
        changed = True
    if a.perf_log:
        os.environ["TAM_PERF_LOG"] = a.perf_log
        changed = True
    if changed:
        CFG.reload()


def main(argv: Optional[List[str]] = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    _apply_cli_overrides(a)
    if a.version:
        print(json.dumps(backend_info(), ensure_ascii=False, indent=2))
        return 0
    if a.selftest:
        return run_selftest()
    if a.perf_summary:
        return cli_perf_summary(a.perf_log, a.by, a.compare)
    if a.journal_show:
        return cli_journal_show()
    if a.journal_resolve:
        return cli_journal_resolve(a.journal_resolve, a.keys, a.all, a.yes)
    if a.calibrate_confirm:
        if not a.profile:
            ap.error("--calibrate-confirm cần --profile")
        return cli_calibrate_confirm(a.profile, a.secs or 180.0, a.keep_open)
    if a.bench_overhead:
        if not a.profile:
            ap.error("--bench-overhead cần --profile")
        return cli_bench_overhead(a.profile, a.secs or 10.0, a.keep_open)
    if a.bench_watch:
        if not a.profile or not a.files:
            ap.error("--bench-watch cần --profile và --files")
        return cli_bench_watch(a.profile, a.files, a.repeat if a.repeat != 3 else 20, a.yes, a.keep_open, a.modes,
                               a.page_states, a.idle_s)
    if a.bench_attach:
        if not a.profile or not a.files:
            ap.error("--bench-attach cần --profile (có thể a,b,c) và --files")
        pids = [x.strip() for x in str(a.profile).split(",") if x.strip()]
        return cli_bench_attach(pids, a.files, a.repeat, a.attach_mode, a.watch, max(10, a.poll_ms), 0.0, a.concurrent, a.yes, a.keep_open, a.manual_timeout)
    if a.bench_upload:
        if not a.profile or not a.files:
            ap.error("--bench-upload cần --profile và --files")
        return cli_bench_upload(a.profile, a.files, a.repeat, a.yes, a.keep_open)
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
