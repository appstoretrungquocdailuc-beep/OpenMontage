# -*- coding: utf-8 -*-
# ==============================================================================
# 🚀 shorwweb_v11_ultralow.py - FINAL PRODUCTION FAST-PATH (ANDROID_VR LOCKED)
# ==============================================================================
# - Khóa cứng tuyệt đối player_client=['android_vr'] qua Global Persistent YDL Instance
# - Đạt tốc độ Warm ~0.68s - 0.71s ổn định tuyệt đối, loại bỏ hoàn toàn các client chết
# - Đồng bộ HTTP Headers sang aria2c 16 luồng
# - Đọc Excel 1 lần/chu kỳ (kiểm tra mtime) + Atomic Excel Write (Lock + os.replace)
# ==============================================================================

import os
import sys
import time
import json
import threading
import shutil
import subprocess
import re
from functools import partial
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from PySide6.QtWidgets import (
        QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
        QFileDialog, QTextEdit, QLineEdit, QFrame, QMessageBox,
        QSpinBox, QCheckBox, QRadioButton, QComboBox, QGridLayout, QSizePolicy
    )
    from PySide6.QtGui import QFont, QTextCursor
    from PySide6.QtCore import Qt, QTimer, Signal, QObject
except Exception:
    raise RuntimeError("PySide6 is required. Install via: pip install PySide6")

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

try:
    import pandas as pd
    from openpyxl import load_workbook
except ImportError:
    pd = None
    load_workbook = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_JSON = os.path.join(BASE_DIR, "yt_720p_gui_config.json")
CONFIG_THEME_JSON = os.path.join(BASE_DIR, "chiadeu_theme.json")
COOKIES_TXT = os.path.join(BASE_DIR, "cookies.txt")

def find_aria2c():
    cands = [os.path.join(BASE_DIR, "aria2c.exe"), shutil.which("aria2c.exe"), shutil.which("aria2c")]
    for c in cands:
        if c and os.path.exists(c): return c
    return None

def find_ffmpeg():
    local = os.path.join(BASE_DIR, "ffmpeg", "ffmpeg.exe")
    return local if os.path.exists(local) else shutil.which("ffmpeg")

def find_ffprobe():
    local = os.path.join(BASE_DIR, "ffmpeg", "ffprobe.exe")
    return local if os.path.exists(local) else shutil.which("ffprobe")

ARIA2C_PATH = find_aria2c()
FFMPEG_PATH = find_ffmpeg()
FFPROBE_PATH = find_ffprobe()

# ===================== GLOBAL PERSISTENT ANDROID_VR YDL =====================
class GlobalFastAndroidVRResolver:
    """
    Sử dụng chung 1 instance yt_dlp.YoutubeDL đã cấu hình sẵn player_client=['android_vr']
    giúp giữ trọn vẹn kết nối Keep-Alive và đạt tốc độ phân giải nhanh nhất (~0.68s).
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._ydl = None

    def _get_ydl(self):
        if self._ydl is None and yt_dlp is not None:
            opts = {
                "format": "18/best[height<=360][ext=mp4]/best[height<=360]",
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "extract_flat": False,
                "lazy_playlist": True,
                "socket_timeout": 8,
                "extractor_args": {
                    "youtube": {
                        "player_client": ["android_vr"],
                        "skip": ["dash", "hls", "translated_subs"],
                    }
                },
            }
            if os.path.exists(COOKIES_TXT):
                opts["cookiefile"] = COOKIES_TXT
            self._ydl = yt_dlp.YoutubeDL(opts)
        return self._ydl

    def resolve_360p(self, url: str, logger=None):
        if yt_dlp is None:
            return None
        with self._lock:
            try:
                ydl = self._get_ydl()
                if not ydl:
                    return None
                info = ydl.extract_info(url, download=False)
                if info and info.get("url"):
                    return {
                        "url": info["url"],
                        "title": info.get("title") or "video",
                        "id": info.get("id") or str(int(time.time())),
                        "http_headers": info.get("http_headers") or {},
                        "client_profile": "android_vr_fast",
                    }
            except Exception as e:
                # Tự động reset instance nếu gặp lỗi ngắt socket
                with self._lock:
                    try:
                        if self._ydl: self._ydl.close()
                    except Exception: pass
                    self._ydl = None
                if logger:
                    logger(f"⚠️ AndroidVR Resolver lỗi, khởi tạo lại: {str(e).splitlines()[0][:80]}")
        return None

GLOBAL_FAST_RESOLVER = GlobalFastAndroidVRResolver()

# ===================== TIỆN ÍCH FILE & EXCEL THREAD-SAFE =====================
SAFE_NAME_RE = re.compile(r'[\/:*?"<>|\r\n]+')
EXCEL_WRITE_LOCK = threading.Lock()

def sanitize_name(s: str) -> str:
    if not s: return "video"
    s = SAFE_NAME_RE.sub(" ", s).strip()
    return re.sub(r"\s+", " ", s)[:150]

def ensure_dir(p):
    os.makedirs(p, exist_ok=True); return p

def write_status_to_excel(excel_path: str, url_index: int, status_col_name: str, new_status: str, logger):
    if not load_workbook:
        logger("⚠️ Thiếu 'openpyxl', không ghi STATUS được."); return
    with EXCEL_WRITE_LOCK:
        try:
            wb = load_workbook(excel_path)
            ws = wb.worksheets[0]
            header = [c.value for c in ws[1]]
            try: col_idx = header.index(status_col_name) + 1
            except ValueError: logger(f"⚠️ Không thấy cột '{status_col_name}'."); return
            row = url_index + 2
            if row > ws.max_row: logger(f"⚠️ Dòng {row} ngoài phạm vi."); return
            ws.cell(row=row, column=col_idx).value = new_status
            temp_path = f"{excel_path}.{threading.get_ident()}.tmp"
            wb.save(temp_path); wb.close()
            os.replace(temp_path, excel_path)
            logger(f"    -> STATUS='{new_status}' @ dòng {row}")
        except Exception as e:
            logger(f"❌ Lỗi ghi Excel: {e}")

def read_excel_batch_rows(excel_path, logger):
    rows_data = []; url_col_name = None; status_col_name = "STATUS"
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
            ch_id_col = cols_upper.get("CHANNEL ID") or cols_upper.get("ID KÊNH")
            ch_name_col = cols_upper.get("TÊN KÊNH")
            orig_link_col = cols_upper.get("LINK VIDEO GỐC")
            for idx, row in df.iterrows():
                v = str(row[url_col_name]).strip()
                st = str(row.get(status_col_name) or "").upper().strip()
                if v.startswith("http") and st != "DONE":
                    rows_data.append({
                        "url": v, "index": int(idx),
                        "channel_id": str(row[ch_id_col]).strip() if ch_id_col else "",
                        "channel_name": str(row[ch_name_col]).strip() if ch_name_col else "",
                        "link_video_goc": str(row[orig_link_col]).strip() if orig_link_col else "",
                    })
    except Exception as e:
        logger(f"⚠️ Lỗi đọc Excel: {e}")
    return rows_data, url_col_name, status_col_name

# ===================== FFPROBE / FFMPEG / ARIA2C =====================
def _ffprobe_duration_seconds(ffprobe_path: str, file_path: Path) -> float:
    try:
        r = subprocess.run([ffprobe_path,"-v","error","-show_entries","format=duration",
                            "-of","default=nw=1:nk=1",str(file_path)],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
        val = (r.stdout or "").strip()
        return float(val) if val else 0.0
    except Exception:
        return 0.0

def split_video_copy_parallel(ffmpeg_path: str, input_path: Path, out_dir: Path,
                              part_seconds: int, prefix: str, logger):
    try:
        ensure_dir(out_dir)
        dur = _ffprobe_duration_seconds(FFPROBE_PATH, input_path)
        if dur <= 0: logger(f"⚠️ Không đọc được thời lượng: {input_path.name}"); return False
        if part_seconds <= 0: logger("⚠️ Thời lượng cắt không hợp lệ."); return False
        num_parts = int(dur // part_seconds) 
        if num_parts <= 0: logger(f"⚠️ Video quá ngắn ({dur:.1f}s)"); return False
        logger(f"✂️ Cắt: {num_parts} phần, {part_seconds}s/phần (copy).")
        cmds=[]; safe_prefix=re.sub(r'[\\/:*?"<>|]+','-',prefix or "part")
        for i in range(num_parts):
            start=i*part_seconds
            out_file=out_dir / f"{safe_prefix}.part{(i+1):02d}.mp4"
            cmd=[ffmpeg_path,"-hide_banner","-loglevel","error","-y","-ss",str(start),"-i",str(input_path),"-t",str(part_seconds),"-c","copy","-avoid_negative_ts","make_zero",str(out_file)]
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

def download_aria2c_ultimate(url: str, temp_dir: Path, filename: str, logger, min_bytes: int, http_headers: dict = None) -> bool:
    temp_file = temp_dir / filename
    if temp_file.exists():
        try: temp_file.unlink()
        except: pass
    logger(f"    [ARIA2C] 🚀 Đang kéo 16 luồng: {filename}")
    if not ARIA2C_PATH: logger("    ❌ LỖI: Không tìm thấy file aria2c.exe"); return False
    try:
        cmd = [
            ARIA2C_PATH, "-x", "16", "-s", "16", "-k", "1M",
            "--min-split-size=1M", "--console-log-level=error", "--summary-interval=0",
            "--file-allocation=none", "--connect-timeout=8", "--timeout=15", "--max-tries=2", "--retry-wait=1",
            "-d", str(temp_dir), "-o", filename,
        ]
        if http_headers:
            for hk, hv in http_headers.items():
                if hk.lower() in ("user-agent", "accept", "accept-language", "referer", "origin") and hv:
                    cmd.extend(["--header", f"{hk}: {hv}"])
        cmd.append(url)
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
            logger(f"    [ARIA2C] ❌ Lỗi Tải: {res.stderr.strip()[-150:]}")
            return False
    except Exception as e:
        logger(f"    [ARIA2C] ❌ Lỗi Exception: {e}"); return False


# ===================== GUI WORKER =====================
class WorkerSignals(QObject):
    log_message = Signal(str)
    finished = Signal()
    url_finished = Signal(str, int, bool)

class Worker(threading.Thread):
    MIN_BYTES_VIDEO = 1 * 1024 * 1024

    def __init__(self, master_app, excel_path, url_col_name, status_col_name):
        super().__init__(daemon=True)
        self.signals = master_app.worker_signals 
        self.master = master_app 
        self.excel_path = excel_path
        self.url_col_name = url_col_name
        self.status_col_name = status_col_name
        self.stop_flag = False
        self.log = partial(self.signals.log_message.emit)

        self.out_dir = Path(self.master.out_entry.text().strip())
        self.temp_dir = self.out_dir.parent / "_temp_yt360p"
        ensure_dir(self.out_dir); ensure_dir(self.temp_dir)
        self.keep_temp = self.master.keep_temp_checkbox.isChecked()
        self.final_move_dir = self.master.final_move_entry.text().strip()

        if not (yt_dlp and ARIA2C_PATH):
            self.log("❌ Thiếu Aria2c hoặc thư viện yt-dlp."); self._critical_error = True
        else: self._critical_error = False
        self.links_with_index = []

    def _quick_fetch_native_360p(self, url):
        return GLOBAL_FAST_RESOLVER.resolve_360p(url, logger=self.log)

    def _cut_after_merge(self, final_mp4: Path, base_name: str, url_index: int, cached_link_video_goc: str = ""):
        t_cut_ns = time.perf_counter_ns()
        t_move_ns = 0
        try:
            prefix = self.master.prefix_entry.text().strip() or "part"
            cutsecs_str = self.master.cutsec_entry.text().strip()
            out_cut_dir = self.master.cutout_entry.text().strip() if self.master.cutout_touched else ""
            cutsecs = 0
            if cutsecs_str:
                try: cutsecs = int(cutsecs_str)
                except ValueError: pass
            final_move_base = Path(self.final_move_dir) if self.final_move_dir else None
            if final_move_base: ensure_dir(final_move_base)
            link_video_goc = cached_link_video_goc

            def execute_move(source_path, is_folder=False):
                nonlocal t_move_ns
                m_start = time.perf_counter_ns()
                if not final_move_base or not os.path.exists(source_path): return
                target_sub_dir = final_move_base / link_video_goc if link_video_goc else final_move_base
                ensure_dir(target_sub_dir)
                final_move_path = target_sub_dir / source_path.name
                if source_path == final_move_path: return
                try:
                    if os.path.exists(final_move_path):
                        name_stem = source_path.stem if not is_folder else source_path.name
                        ext = source_path.suffix if not is_folder else ""
                        final_move_path = target_sub_dir / f"{name_stem}_{int(time.time())}{ext}"
                    shutil.move(str(source_path), str(final_move_path))
                    t_move_ns += (time.perf_counter_ns() - m_start)
                except Exception as move_e:
                    self.log(f"❌ Lỗi MOVE: {move_e}")

            is_cut_success = False; target_dir = None
            if cutsecs > 0:
                prefix = re.sub(r'[\\/:*?"<>|]+','-',prefix)
                target_dir_name = f"{base_name}_CUT"
                target_dir = Path(out_cut_dir).joinpath(target_dir_name) if out_cut_dir else Path(final_mp4.parent).joinpath(target_dir_name)
                ensure_dir(target_dir)
                is_cut_success = split_video_copy_parallel(FFMPEG_PATH, final_mp4, target_dir, cutsecs, prefix, self.log)

            if is_cut_success:
                execute_move(target_dir, is_folder=True)
            else:
                wrap_dir = Path(final_mp4.parent).joinpath(base_name)
                ensure_dir(wrap_dir)
                shutil.move(str(final_mp4), str(wrap_dir / final_mp4.name))
                execute_move(wrap_dir, is_folder=True)
        except Exception as e:
            self.log(f"❌ Lỗi cắt/move: {e}")

    def _process_single_url(self, url: str, url_index: int, row_meta: dict = None):
        if self._critical_error: self.signals.url_finished.emit(url, url_index, False); return
        if hasattr(self.master, 'realtime_stop_event') and self.master.realtime_stop_event.is_set():
            self.signals.url_finished.emit(url, url_index, False); return

        t_total_start = time.perf_counter_ns()
        excel_info = f"[Dòng {url_index+2}] " if url_index is not None else ""
        self.log(f"=== [START] {excel_info}{url} ===")
        success = False

        # 1. BẮT LINK 360P BẰNG ANDROID_VR FAST-PATH (~0.68s)
        t_link_start = time.perf_counter_ns()
        quick_data = self._quick_fetch_native_360p(url)
        t_link_ms = (time.perf_counter_ns() - t_link_start) / 1e6

        if not quick_data:
            self.log(f"❌ Không lấy được link 360p ({t_link_ms:.1f}ms).")
            self.signals.url_finished.emit(url, url_index, False); return

        self.log(f"⏱️ LẤY LINK [{quick_data.get('client_profile')}]: {t_link_ms/1000:.3f}s ({t_link_ms:.0f}ms)")

        final_title = sanitize_name(quick_data["title"])
        final_vid_id = quick_data["id"]
        base_name = f"{final_title} [{final_vid_id}]"
        out_file_final = self.out_dir / f"{base_name}.mp4"
        combined_file = self.temp_dir / f"{final_vid_id}_360p.mp4"

        # 2. TẢI BẰNG ARIA2C 16 LUỒNG + ĐỒNG BỘ HEADERS
        vurl = quick_data["url"]
        http_headers = quick_data.get("http_headers") or {}
        t_dl_start = time.perf_counter_ns()

        if download_aria2c_ultimate(vurl, self.temp_dir, combined_file.name, self.log, self.MIN_BYTES_VIDEO, http_headers):
            t_dl_ms = (time.perf_counter_ns() - t_dl_start) / 1e6
            self.log(f"⏱️ TẢI ARIA2C XONG TRONG: {t_dl_ms/1000:.3f}s ({t_dl_ms:.0f}ms)")

            if out_file_final.exists(): out_file_final.unlink()
            shutil.move(str(combined_file), str(out_file_final))
            self.log(f"✅ Success: {base_name}.mp4")

            cached_orig_link = (row_meta or {}).get("link_video_goc", "")
            self._cut_after_merge(out_file_final, base_name, url_index, cached_link_video_goc=cached_orig_link)
            success = True
        else:
            self.log("❌ Tải Aria2c thất bại hoặc File lỗi.")

        if not self.keep_temp and combined_file.exists():
            try: combined_file.unlink()
            except: pass

        if success and url_index is not None and self.excel_path and self.status_col_name:
            write_status_to_excel(self.excel_path, url_index, self.status_col_name, "DONE", self.log)

        t_total_ms = (time.perf_counter_ns() - t_total_start) / 1e6
        self.log(f"=== [END] {url} | Tổng: {t_total_ms/1000:.3f}s ({t_total_ms:.0f}ms) | Thành công: {success} ===")
        self.signals.url_finished.emit(url, url_index, success)

    def run(self):
        if self.links_with_index:
            url, url_index = self.links_with_index[0]
            self._process_single_url(url, url_index)
        self.signals.finished.emit()


# ==============================================================================
# 🖥️ PHẦN GIAO DIỆN QT - GIỮ NGUYÊN 100%
# ==============================================================================
class LoggerQt:
    def __init__(self, text_widget: QTextEdit, interval_ms: int = 80):
        self.t = text_widget; self.q = [] 
        self._lock = threading.Lock()
        self._timer = QTimer(); self._timer.setInterval(interval_ms); self._timer.timeout.connect(self._drain)
    def start(self): self._timer.start()
    def stop(self):
        try: self._timer.stop()
        except Exception: pass
    def log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        with self._lock: self.q.append(f"[{ts}] {msg}")
    def _drain(self):
        with self._lock:
            if not self.q: return
            lines = self.q[:]; self.q.clear()
        for line in lines:
            self.t.append(line); self.t.ensureCursorVisible()

class PremiumDownloaderApp(QWidget):
    log_signal = Signal(str)
    log_html_signal = Signal(str) 
    realtime_stopped_signal = Signal() 

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Downloader Tool — Realtime Monitor v11 Final Fast-Path (android_vr + Aria2c)")
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
        
        # Pre-warm Global Fast Resolver ngay khi mở app
        threading.Thread(target=GLOBAL_FAST_RESOLVER.resolve_360p, args=("https://www.youtube.com/watch?v=FWKRi942lWM",), daemon=True).start()

    def _handle_log_signal(self, msg): self.logger_qt.log(msg)

    def _handle_html_log(self, html_msg):
        cursor = self.log_widget.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.log_widget.setTextCursor(cursor)
        self.log_widget.insertHtml(html_msg + "<br>")
        self.log_widget.ensureCursorVisible()

    def _build_ui(self):
        root = QHBoxLayout(self); root.setContentsMargins(12, 12, 12, 12)
        left = QVBoxLayout(); left_frame = QFrame(); left_frame.setLayout(left); left_frame.setMaximumWidth(600); left_frame.setMinimumWidth(600) 
        
        header = QLabel("Downloader Tool — Fast 360p Monitor (v11 Final)"); header.setObjectName("HeaderLabel"); header.setMinimumHeight(60)
        header.setStyleSheet("font-size:18px; font-weight:700;"); left.addWidget(header)
        
        left.addWidget(QLabel("1. Chọn file Excel (Data Input):"))
        self.input_excel = QLineEdit(os.path.join(BASE_DIR, "check.xlsx")); self.input_excel.setPlaceholderText("Select .xlsx file...")
        h1 = QHBoxLayout()
        btn_excel = QPushButton("Browse"); btn_excel.clicked.connect(self._pick_excel); btn_excel.setFixedWidth(100) 
        btn_open_excel = QPushButton("Mở Excel"); btn_open_excel.clicked.connect(self._open_excel_file); btn_open_excel.setFixedWidth(100)
        h1.addWidget(self.input_excel); h1.addWidget(btn_excel); h1.addWidget(btn_open_excel); left.addLayout(h1)
        
        left.addWidget(QLabel("2. Chọn thư mục lưu Download:"))
        self.input_folder = QLineEdit(BASE_DIR); self.input_folder.setPlaceholderText("Select output folder...")
        h2 = QHBoxLayout()
        btn_folder = QPushButton("Browse"); btn_folder.clicked.connect(self._pick_folder); btn_folder.setFixedWidth(80) 
        h2.addWidget(self.input_folder); h2.addWidget(btn_folder); left.addLayout(h2)
        
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
        g_cut.setColumnStretch(0, 1); g_cut.setColumnStretch(1, 1)
        sett_layout.addLayout(g_cut)
        
        sett_layout.addWidget(QLabel("Chất lượng:"))
        self.radio_auto = QRadioButton("Cứng 360p (Fastest)"); self.radio_auto.setChecked(True)
        self.radio_light = QRadioButton("Ignored"); self.radio_hard = QRadioButton("Ignored")
        h_rad = QHBoxLayout(); h_rad.addWidget(self.radio_auto); h_rad.addStretch(); sett_layout.addLayout(h_rad)

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
        h_opt.addWidget(self.spin_threads); h_opt.addStretch()
        sett_layout.addLayout(h_opt)
        left.addLayout(sett_layout)
        
        left.addWidget(QLabel("--- Realtime Monitor & Controls ---"))
        h_rt = QHBoxLayout()
        self.btn_start = QPushButton("START MONITOR"); self.btn_start.clicked.connect(self.start_realtime)
        self.btn_stop = QPushButton("STOP MONITOR"); self.btn_stop.setEnabled(False); self.btn_stop.clicked.connect(self.stop_realtime)
        h_rt.addWidget(self.btn_start); h_rt.addWidget(self.btn_stop); left.addLayout(h_rt)
        
        left.addWidget(QLabel("--- Appearance ---"))
        left.addWidget(QLabel("Giao diện (Accent Color):")); self.combo_accent = QComboBox()
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
        self.log_widget.setAcceptRichText(True); right.addWidget(self.log_widget)
        
        bottom = QHBoxLayout()
        self.btn_save_config = QPushButton("Lưu cấu hình"); self.btn_save_config.clicked.connect(self._save_config); self.btn_save_config.setFixedWidth(120) 
        btn_clear = QPushButton("Clear Log"); btn_clear.clicked.connect(self.log_widget.clear); btn_clear.setFixedWidth(120)
        bottom.addWidget(self.btn_save_config); bottom.addStretch(); bottom.addWidget(btn_clear)
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
        if p: self.input_cutout.setText(p); self.cutout_touched = True

    def _pick_finalmove(self):
        p = QFileDialog.getExistingDirectory(self, "Select Final Move Folder", BASE_DIR)
        if p: self.input_finalmove.setText(p)

    def start_realtime(self):
        excel_path = self.input_excel.text().strip()
        out = self.input_folder.text().strip()
        if not os.path.exists(excel_path): QMessageBox.warning(self, "Thiếu Excel", "Hãy chọn file Excel."); return
        if not out: QMessageBox.warning(self, "Thiếu thư mục","Hãy chọn thư mục lưu video."); return
        ensure_dir(out)
        if self.realtime_thread and self.realtime_thread.is_alive(): return

        self.log_widget.clear(); self._save_config(); self.realtime_stop_event.clear()
        self.btn_start.setEnabled(False); self.btn_stop.setEnabled(True)
        self.status_badge.setText("MONITORING")
        self.status_badge.setStyleSheet(f"background:{self._adjust_brightness(self.accent,-0.12)}; border-radius:8px; padding:6px; font-weight:700;")
        
        self.seen_urls.clear()
        max_workers = self.spin_threads.value()
        self.batch_executor = ThreadPoolExecutor(max_workers=max_workers)
        
        self.realtime_thread = threading.Thread(target=self._realtime_worker, daemon=True)
        self.realtime_thread.start()
        self.logger_qt.log(f"🔄 Bắt đầu chế độ **android_vr Fast-Path**... Max Threads: {max_workers}")

    def stop_realtime(self):
        if not self.realtime_thread: return
        self.logger_qt.log("🛑 Đang dừng Realtime...")
        self.realtime_stop_event.set(); self.btn_stop.setEnabled(False)
        if self.batch_executor: self.batch_executor.shutdown(wait=False); self.batch_executor = None

    def _realtime_worker(self):
        excel_path = self.input_excel.text().strip(); last_mtime = 0.0
        while not self.realtime_stop_event.is_set():
            try:
                if not os.path.exists(excel_path): time.sleep(0.5); continue
                try: cur_mtime = os.path.getmtime(excel_path)
                except OSError: cur_mtime = 0.0
                if cur_mtime == last_mtime and self.seen_urls: time.sleep(0.35); continue
                
                rows_data, url_col, status_col = read_excel_batch_rows(excel_path, lambda m: None)
                last_mtime = cur_mtime
                new_items = [r for r in rows_data if r["url"] not in self.seen_urls]
                if not new_items: time.sleep(0.35); continue
                
                for r in new_items:
                    if self.realtime_stop_event.is_set(): break
                    url = r["url"]; idx = r["index"]; self.seen_urls.add(url)
                    log_html = f"""
                    <table border="0" cellspacing="0" cellpadding="0" style="margin-top:5px; margin-bottom:5px;">
                        <tr><td valign="middle" style="padding-left: 10px;">
                            <span style="font-size:11pt;">🔔 <b>Phát hiện video mới:</b></span><br>
                            ID: <span style='color:#ff6aa3; font-weight:bold;'>{r['channel_id'] or "N/A"}</span><br>
                            Kênh: <span style='color:#22c55e; font-weight:bold;'>{r['channel_name'] or "N/A"}</span>
                        </td></tr></table><hr style="border: 0; height: 1px; background-color: #333; margin: 5px 0;">
                    """
                    self.log_html_signal.emit(log_html)
                    if self.batch_executor:
                        worker = Worker(self, excel_path, url_col, status_col)
                        self.batch_executor.submit(worker._process_single_url, url, idx, r)
                time.sleep(0.35)
            except Exception as e:
                self.log_signal.emit(f"❌ Lỗi Realtime Thread: {e}"); time.sleep(1.5)
        self.log_signal.emit("✅ Đã dừng chế độ Realtime.")
        self.realtime_stopped_signal.emit()

    def _stop_realtime_ui_reset(self):
        self.btn_start.setEnabled(True); self.btn_stop.setEnabled(False)
        self.status_badge.setText("IDLE"); self.status_badge.setStyleSheet("background:#2b2b2b; border-radius:8px; padding:6px; font-weight:700;")
        self.realtime_thread = None

    def _on_single_url_finished(self, url, url_index, success): pass

    def _save_config(self):
        try:
            data = {
                "excel_path": self.input_excel.text(), "save_folder": self.input_folder.text(),
                "cut_prefix": self.input_prefix.text(), "cut_seconds": self.input_cutsec.text(),
                "cut_out": self.input_cutout.text(), "final_move": self.input_finalmove.text(),
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
        """)
        if self.status_badge.text() != "IDLE": self.status_badge.setStyleSheet(f"background:{darker}; border-radius:8px; padding:6px; font-weight:700;")

    def _apply_accent(self, idx):
        col = self.combo_accent.itemData(idx)
        if col: self.hex_entry.setText(col); self._apply_stylesheet(col); self._save_theme()

    def _apply_hex(self):
        val = (self.hex_entry.text() or "").strip()
        if not val.startswith('#') or len(val) not in (4,7): QMessageBox.warning(self, "Hex lỗi", "Nhập mã màu HEX hợp lệ"); return
        self._apply_stylesheet(val); self._save_theme()

    def _adjust_brightness(self, hexcol: str, delta: float) -> str:
        try:
            h = hexcol.lstrip('#'); 
            if len(h) == 3: h = ''.join([c*2 for c in h])
            r = int(h[0:2],16); g=int(h[2:4],16); b=int(h[4:6],16)
            def clamp(x): return max(0, min(255, int(x)))
            return f"#{clamp(r*(1+delta)):02x}{clamp(g*(1+delta)):02x}{clamp(b*(1+delta)):02x}"
        except Exception: return hexcol

    def _save_theme(self):
        try:
            with open(CONFIG_THEME_JSON, 'w', encoding='utf-8') as f: json.dump({"accent": self.accent}, f, ensure_ascii=False, indent=2)
        except Exception: pass

    def _load_theme(self):
        try:
            if os.path.exists(CONFIG_THEME_JSON):
                with open(CONFIG_THEME_JSON, 'r', encoding='utf-8') as f: cfg = json.load(f)
                accent = cfg.get('accent')
                if accent:
                    for i in range(self.combo_accent.count()):
                        if self.combo_accent.itemData(i).lower() == accent.lower(): self.combo_accent.setCurrentIndex(i); break
                    self._apply_stylesheet(accent)
        except Exception: pass

if __name__ == '__main__':
    app = QApplication(sys.argv)
    w = PremiumDownloaderApp()
    w.show()
    sys.exit(app.exec())