# -*- coding: utf-8 -*-
"""
VPS SNIPER PRO — V42 FAST (tối ưu từ V41 "API METADATA FIXED")

Giữ nguyên: giao diện, config.json, file API keys, Excel (cột A giờ, B CHANNEL ID, D URL, H TITLE —
trùng định dạng scanner V39), âm báo, Auto Click, hẹn giờ On/Off, delay giữa 2 lần hỏi.
KHÔNG tăng số lần gọi API (mỗi lần hỏi vẫn đúng 1 request activities, quota 1 đơn vị).

Thay đổi để nhanh hơn / không hỏng:
  1. Ghi Excel NGAY khi thấy video (tiêu đề + channelId lấy luôn từ activities) — bỏ request
     videos.list thứ 2 trước khi ghi Excel (bớt ~200-500 ms trên đường nóng).
  2. Ghi Excel xong append trigger_wakeup.txt cạnh check.xlsx → tool tải V9 thức dậy ngay
     (như scanner V39), không phải chờ vòng dò Excel.
  3. Ghi Excel an toàn: lưu file tạm rồi os.replace (tool tải không bao giờ đọc trúng file ghi dở),
     không ghi trùng video đã có, thử lại ngắn nếu Excel đang bị khoá.
  4. Thứ tự đường nóng: Excel → trigger → âm báo → log; mở trình duyệt (Auto Click) chạy luồng riêng SAU cùng.
  5. Chống báo nhầm: chỉ nhận video MỚI ĐĂNG (publishedAt so với giờ máy chủ Google ≤ "Tuổi tối đa"),
     nhớ mọi video đã thấy (không báo lại khi activities đảo thứ tự) — bản cũ báo cả video cũ thêm vào playlist.
  6. Giao diện an toàn luồng: luồng quét không đụng trực tiếp Tkinter (bản cũ có thể treo/đơ GUI).
  7. Yêu cầu nhẹ hơn: `fields=` chỉ lấy đúng các trường cần (quota không đổi), header dựng sẵn,
     timeout tách connect/read, log rõ thời gian request + tuổi video lúc phát hiện.
  8. Tuỳ chọn "Quét tiếp sau khi bắt" (mặc định TẮT = như bản cũ: bắt 1 video rồi dừng kênh đó).
"""
import json
import os
import queue
import re
import threading
import time
import webbrowser
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

try:
    import winsound  # Windows
except ImportError:  # pragma: no cover - máy không phải Windows
    winsound = None

try:
    import openpyxl
except ImportError:  # pragma: no cover
    openpyxl = None

try:
    import tkinter as tk
    from tkinter import ttk, scrolledtext, messagebox, filedialog
except ImportError:  # pragma: no cover - chỉ để chạy test lõi trên máy không có Tk
    tk = None

VERSION = "V42 FAST"
TRIGGER_FILE_NAME = "trigger_wakeup.txt"
ACTIVITIES_URL = "https://www.googleapis.com/youtube/v3/activities"
# Chỉ lấy các trường dùng tới (giảm byte trả về; quota vẫn 1 đơn vị/lần như cũ)
ACTIVITIES_FIELDS = ("items(snippet(type,title,publishedAt,channelId),"
                     "contentDetails(upload/videoId,playlistItem/resourceId/videoId))")
DEFAULT_MAX_AGE_SEC = 600.0
VIDEO_ID_RE = re.compile(r"(?:v=|/shorts/|youtu\.be/|/live/)([A-Za-z0-9_-]{11})")
_ILLEGAL_XML_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


# ======================= LÕI (không phụ thuộc Tkinter — test được) ======================= #
def parse_rfc3339(s):
    """'2026-11-11T12:00:18Z' / '...+00:00' / có phần lẻ giây → datetime UTC; lỗi → None."""
    if not s:
        return None
    try:
        t = str(s).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def server_now_from_date_header(date_header, t_send, t_recv):
    """Giờ máy chủ Google (header Date, chính xác tới giây) quy về thời điểm nhận phản hồi; lỗi → None.
    Header Date được tạo trong khoảng [t_send, t_recv] → lấy điểm giữa + 0.5 s (bù làm tròn xuống)."""
    if not date_header:
        return None
    try:
        d = parsedate_to_datetime(date_header)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp() + 0.5 + max(0.0, (t_recv - t_send) / 2.0)
    except (TypeError, ValueError, IndexError):
        return None


def parse_activity(js):
    """JSON activities → dict {id, title, type, published, channel_id} hoặc (None, lý do)."""
    items = (js or {}).get("items") or []
    if not items:
        return None, "empty"
    item = items[0] or {}
    snip = item.get("snippet") or {}
    det = item.get("contentDetails") or {}
    typ = snip.get("type")
    vid = None
    if typ == "upload":
        vid = (det.get("upload") or {}).get("videoId")
    elif typ == "playlistItem":
        vid = ((det.get("playlistItem") or {}).get("resourceId") or {}).get("videoId")
    if not vid:
        return None, f"type:{typ}"
    return {"id": vid, "title": snip.get("title", "") or "", "type": typ,
            "published": parse_rfc3339(snip.get("publishedAt")),
            "channel_id": snip.get("channelId", "") or ""}, "ok"


def judge_new(data, seen_ids, server_now_ts, max_age_sec):
    """Quyết định có phải video MỚI cần bắt không. Trả về (True/False, lý do, tuổi_giây|None).
    - đã thấy rồi → bỏ (activities có thể đảo thứ tự giữa các lần hỏi);
    - có publishedAt + giờ máy chủ: tuổi > max_age → video cũ (vd thêm video cũ vào playlist) → bỏ;
    - không đo được tuổi → vẫn bắt (giống bản cũ) nhưng ghi lý do."""
    vid = data["id"]
    if vid in seen_ids:
        return False, "seen", None
    age = None
    pub = data.get("published")
    if pub is not None and server_now_ts is not None:
        age = server_now_ts - pub.timestamp()
        if max_age_sec and max_age_sec > 0 and age > max_age_sec:
            return False, "old", age
    return True, "new" if age is not None else "new(no-age)", age


def extract_video_id(v):
    m = VIDEO_ID_RE.search(str(v or ""))
    return m.group(1) if m else None


def save_excel_atomic(path, rows, retries=6, retry_sleep=0.05):
    """Ghi các dòng [(stamp, channel_id, url, title)] vào cuối sheet đầu tiên.
    Cột giống V41/scanner V39: A giờ, B CHANNEL ID, D URL, H TITLE.
    An toàn: bỏ video đã có (theo id ở cột D), lưu file tạm rồi os.replace (atomic trên cùng ổ).
    Trả về list (url, số_dòng | 'dup')."""
    if openpyxl is None:
        raise RuntimeError("thiếu openpyxl")
    last_err = None
    for attempt in range(max(1, retries)):
        tmp = f"{path}.v42-{os.getpid()}-{threading.get_ident()}.tmp.xlsx"
        try:
            wb = openpyxl.load_workbook(path)
            ws = wb.active
            existing = set()
            for (v,) in ws.iter_rows(min_row=2, min_col=4, max_col=4, values_only=True):
                vid = extract_video_id(v)
                if vid:
                    existing.add(vid)
            out = []
            changed = False
            for stamp, ch, url, title in rows:
                vid = extract_video_id(url)
                if vid and vid in existing:
                    out.append((url, "dup"))
                    continue
                n = ws.max_row + 1
                ws.cell(row=n, column=1).value = stamp
                ws.cell(row=n, column=2).value = ch
                ws.cell(row=n, column=4).value = url
                ws.cell(row=n, column=8).value = _ILLEGAL_XML_RE.sub("", title or "")
                if vid:
                    existing.add(vid)
                out.append((url, n))
                changed = True
            if not changed:
                return out
            wb.save(tmp)
            os.replace(tmp, path)
            return out
        except PermissionError as e:   # Excel đang mở/khoá file trên Windows → thử lại ngắn
            last_err = e
            time.sleep(retry_sleep * (attempt + 1))
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
    raise last_err if last_err else RuntimeError("không ghi được Excel")


def append_trigger(excel_path, channel_id):
    """Đánh thức tool tải V9: append 1 dòng vào trigger_wakeup.txt CẠNH file Excel (tool tải theo dõi file này)."""
    d = os.path.dirname(os.path.abspath(excel_path)) or "."
    p = os.path.join(d, TRIGGER_FILE_NAME)
    with open(p, "a", encoding="utf-8") as f:
        f.write(f"{channel_id or '-'}\n")
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    return p


def quota_estimate(n_channels, delay_s, avg_req_s, n_keys):
    """Ước lượng đơn vị quota/ngày nếu quét liên tục (mỗi request activities = 1 đơn vị)."""
    per_ch = 86400.0 / max(0.05, float(delay_s) + float(avg_req_s))
    total = per_ch * max(0, n_channels)
    cap = 10000 * max(0, n_keys)
    return int(total), int(cap)


# ======================= GIAO DIỆN ======================= #
class YoutubeSniperMulti:
    def __init__(self, root):
        self.root = root
        self.root.title(f"VPS SNIPER PRO ({VERSION} - API METADATA FIXED)")
        self.root.geometry("790x800")
        self.root.configure(bg="#000000")

        # --- BIẾN HỆ THỐNG ---
        self.config_file = "config.json"
        self.api_key_path = tk.StringVar()
        self.excel_path = tk.StringVar()
        self.audio_path = tk.StringVar()
        self.delay_time = tk.DoubleVar(value=0.2)
        self.auto_click = tk.BooleanVar(value=True)
        self.keep_scanning = tk.BooleanVar(value=False)       # V42: quét tiếp sau khi bắt
        self.max_age = tk.DoubleVar(value=DEFAULT_MAX_AGE_SEC)  # V42: tuổi tối đa (giây)

        self.api_keys_list = []
        self.key_index = 0
        self.key_lock = threading.Lock()
        self.excel_lock = threading.Lock()
        self.channel_rows = []
        self.ui_q = queue.Queue()          # V42: luồng quét → GUI (Tkinter không an toàn đa luồng)
        self.req_ms_ema = 300.0

        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=150, pool_maxsize=150, max_retries=1)
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Connection': 'keep-alive',
        })
        # V42: header dựng sẵn một lần (bản cũ copy dict mỗi request)
        self.req_headers = {'Cache-Control': 'no-cache', 'Pragma': 'no-cache'}

        self.setup_ui()
        self.load_config_from_file()
        self.root.after(30, self._drain_ui_queue)
        self.start_scheduler()

    # ---------- an toàn luồng ----------
    def ui(self, fn, *a, **kw):
        """Gọi hàm Tkinter từ bất kỳ luồng nào: xếp hàng, luồng GUI thực thi."""
        self.ui_q.put((fn, a, kw))

    def _drain_ui_queue(self):
        try:
            for _ in range(500):
                fn, a, kw = self.ui_q.get_nowait()
                try:
                    fn(*a, **kw)
                except Exception:
                    pass
        except queue.Empty:
            pass
        self.root.after(30, self._drain_ui_queue)

    def setup_ui(self):
        frame_config = tk.LabelFrame(self.root, text=" [ CẤU HÌNH CHUNG ] ", bg="#000000", fg="#00ff00", font=("Consolas", 10, "bold"))
        frame_config.pack(padx=10, pady=5, fill="x")

        tk.Label(frame_config, text="File API Keys:", bg="#000000", fg="white").grid(row=0, column=0, padx=5, pady=5, sticky="e")
        tk.Entry(frame_config, textvariable=self.api_key_path, width=40, bg="#222", fg="#00ff00").grid(row=0, column=1, padx=5)
        tk.Button(frame_config, text="📂 Load Keys", command=self.load_api_file, bg="#333", fg="white").grid(row=0, column=2, padx=5)
        self.lbl_key_status = tk.Label(frame_config, text="(0 keys)", bg="#000000", fg="#888")
        self.lbl_key_status.grid(row=0, column=3)

        tk.Label(frame_config, text="File Excel Log:", bg="#000000", fg="yellow").grid(row=1, column=0, padx=5, pady=5, sticky="e")
        tk.Entry(frame_config, textvariable=self.excel_path, width=40, bg="#222", fg="yellow").grid(row=1, column=1, padx=5)
        tk.Button(frame_config, text="📊 Chọn Excel", command=self.load_excel_file, bg="#333", fg="white").grid(row=1, column=2, padx=5)

        tk.Label(frame_config, text="Âm báo (.wav):", bg="#000000", fg="#FF00FF").grid(row=2, column=0, padx=5, pady=5, sticky="e")
        tk.Entry(frame_config, textvariable=self.audio_path, width=40, bg="#222", fg="#FF00FF").grid(row=2, column=1, padx=5)
        tk.Button(frame_config, text="🎵 Chọn Nhạc", command=self.load_audio_file, bg="#333", fg="white").grid(row=2, column=2, padx=5)

        tk.Label(frame_config, text="Delay (s):", bg="#000000", fg="white").grid(row=3, column=0, padx=5, pady=5, sticky="e")
        tk.Entry(frame_config, textvariable=self.delay_time, width=10, bg="#222", fg="white").grid(row=3, column=1, sticky="w", padx=5)
        chk_click = tk.Checkbutton(frame_config, text="Tự động mở Link (Auto Click)", variable=self.auto_click,
                                   bg="#000000", fg="#00FFFF", selectcolor="#222", activebackground="#000000", activeforeground="#00FFFF")
        chk_click.grid(row=3, column=2, padx=5, sticky="w")
        btn_save = tk.Button(frame_config, text="💾 LƯU CẤU HÌNH (SAVE)", command=self.save_config_to_file, bg="#00008B", fg="white", font=("Arial", 9, "bold"))
        btn_save.grid(row=3, column=3, padx=5, sticky="ew")

        # V42: 2 tuỳ chọn mới
        tk.Label(frame_config, text="Tuổi tối đa (s):", bg="#000000", fg="white").grid(row=4, column=0, padx=5, pady=5, sticky="e")
        tk.Entry(frame_config, textvariable=self.max_age, width=10, bg="#222", fg="white").grid(row=4, column=1, sticky="w", padx=5)
        tk.Checkbutton(frame_config, text="Quét tiếp sau khi bắt", variable=self.keep_scanning,
                       bg="#000000", fg="#00FFFF", selectcolor="#222", activebackground="#000000",
                       activeforeground="#00FFFF").grid(row=4, column=2, padx=5, sticky="w")

        frame_list_container = tk.LabelFrame(self.root, text=" [ DANH SÁCH QUẢN LÝ KÊNH ] ", bg="#000000", fg="cyan", font=("Consolas", 10, "bold"))
        frame_list_container.pack(padx=10, pady=5, fill="both", expand=True)
        self.canvas = tk.Canvas(frame_list_container, bg="black", highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(frame_list_container, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = tk.Frame(self.canvas, bg="black")
        self.scrollable_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        btn_add = tk.Button(self.root, text="[+] THÊM DÒNG MỚI", command=lambda: self.add_channel_row(), bg="#004400", fg="lime", font=("Arial", 11, "bold"))
        btn_add.pack(pady=5, fill="x", padx=10)

        tk.Label(self.root, text=">>> SYSTEM LOG:", bg="#000000", fg="#00ff00").pack(anchor="w", padx=10)
        self.txt_log = scrolledtext.ScrolledText(self.root, height=10, bg="#111", fg="#00ff00", font=("Consolas", 9))
        self.txt_log.pack(padx=10, pady=5, fill="x")
        self.txt_log.tag_config("NEW", foreground="yellow", background="#AA0000", font=("Consolas", 10, "bold"))
        self.txt_log.tag_config("DONE", foreground="#00FF00")
        self.txt_log.tag_config("INFO", foreground="#00FFFF")
        self.txt_log.tag_config("AUTO", foreground="#FF00FF")
        self.txt_log.tag_config("META", foreground="#FFA500")
        self.txt_log.tag_config("ERROR", foreground="#FF5555")

    def start_scheduler(self):
        self.scheduler_tick()

    def scheduler_tick(self):
        try:
            now_str = datetime.now().strftime("%H:%M:%S")
            for i, row in enumerate(self.channel_rows):
                start_time = self.get_time_string(row, "start")
                stop_time = self.get_time_string(row, "stop")
                if start_time and start_time != "00:00:00" and start_time == now_str:
                    if not row["running"] and row["last_auto_on"] != now_str:
                        row["last_auto_on"] = now_str
                        self.auto_start_trigger(i, start_time)
                if stop_time and stop_time != "00:00:00" and stop_time == now_str:
                    if row["running"] and row["last_auto_off"] != now_str:
                        row["last_auto_off"] = now_str
                        self.auto_stop_trigger(i, stop_time)
        except Exception:
            pass
        # V42: canh đúng ranh giới giây (bản cũ after(1000) trôi dần → có thể nhảy qua 1 giây hẹn)
        ms = 1000 - int((time.time() % 1.0) * 1000) + 5
        self.root.after(max(20, ms), self.scheduler_tick)

    def get_time_string(self, row, type_):
        try:
            h = row[f"{type_}_h"].get()
            m = row[f"{type_}_m"].get()
            s = row[f"{type_}_s"].get()
            return f"{int(h):02}:{int(m):02}:{int(s):02}"
        except Exception:
            return ""

    def auto_start_trigger(self, index, time_str):
        row = self.channel_rows[index]
        if not row["running"]:
            name = row["entry_name"].get()
            self.log(f"⏰ [AUTO] Đến giờ hẹn ({time_str}). Tự động BẬT: {name}", "AUTO")
            self.toggle_scan(index, True)

    def auto_stop_trigger(self, index, time_str):
        row = self.channel_rows[index]
        if row["running"]:
            name = row["entry_name"].get()
            self.log(f"⏰ [AUTO] Hết giờ ({time_str}). Tự động TẮT: {name}", "AUTO")
            self.toggle_scan(index, False)

    def add_channel_row(self, name_val="", id_val="", start_val="00:00:00", stop_val="00:00:00"):
        row_index = len(self.channel_rows)
        frame_row = tk.Frame(self.scrollable_frame, bg="black", pady=2)
        frame_row.pack(fill="x", anchor="w")

        default_name = name_val if name_val else f"Kênh {row_index + 1}"
        entry_name = tk.Entry(frame_row, width=15, bg="#333", fg="cyan", font=("Arial", 9))
        entry_name.insert(0, default_name)
        entry_name.pack(side="left", padx=2)

        entry_id = tk.Entry(frame_row, width=30, bg="#222", fg="#00ff00", font=("Consolas", 10))
        entry_id.insert(0, id_val)
        entry_id.pack(side="left", padx=5)

        def create_time_spinner(parent, val_str):
            try:
                parts = val_str.split(":"); h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
            except Exception:
                h, m, s = 0, 0, 0
            f = tk.Frame(parent, bg="black"); f.pack(side="left", padx=2)

            def on_scroll(event, widget):
                if event.delta > 0: widget.invoke('buttonup')
                else: widget.invoke('buttondown')
            sb_h = tk.Spinbox(f, from_=0, to=23, width=2, bg="#222", fg="white", format="%02.0f", font=("Consolas", 9), wrap=True)
            sb_h.delete(0, "end"); sb_h.insert(0, f"{h:02}"); sb_h.pack(side="left")
            sb_h.bind("<MouseWheel>", lambda e, w=sb_h: on_scroll(e, w))
            tk.Label(f, text=":", bg="black", fg="#888").pack(side="left")
            sb_m = tk.Spinbox(f, from_=0, to=59, width=2, bg="#222", fg="white", format="%02.0f", font=("Consolas", 9), wrap=True)
            sb_m.delete(0, "end"); sb_m.insert(0, f"{m:02}"); sb_m.pack(side="left")
            sb_m.bind("<MouseWheel>", lambda e, w=sb_m: on_scroll(e, w))
            tk.Label(f, text=":", bg="black", fg="#888").pack(side="left")
            sb_s = tk.Spinbox(f, from_=0, to=59, width=2, bg="#222", fg="white", format="%02.0f", font=("Consolas", 9), wrap=True)
            sb_s.delete(0, "end"); sb_s.insert(0, f"{s:02}"); sb_s.pack(side="left")
            sb_s.bind("<MouseWheel>", lambda e, w=sb_s: on_scroll(e, w))
            return sb_h, sb_m, sb_s

        tk.Label(frame_row, text="On:", fg="#888", bg="black", font=("Arial", 8)).pack(side="left")
        start_h, start_m, start_s = create_time_spinner(frame_row, start_val)
        tk.Label(frame_row, text="Off:", fg="#888", bg="black", font=("Arial", 8)).pack(side="left")
        stop_h, stop_m, stop_s = create_time_spinner(frame_row, stop_val)

        lbl_status = tk.Label(frame_row, text="IDLE", fg="#555", bg="black", width=10, font=("Arial", 8))
        lbl_status.pack(side="left", padx=5)

        btn_start = tk.Button(frame_row, text="▶ START", bg="green", fg="white", width=7)
        btn_stop = tk.Button(frame_row, text="⏹ STOP", bg="#550000", fg="white", width=7, state="disabled")
        btn_start.config(command=lambda: self.toggle_scan(row_index, True))
        btn_stop.config(command=lambda: self.toggle_scan(row_index, False))
        btn_start.pack(side="left", padx=2); btn_stop.pack(side="left", padx=2)

        row_data = {
            "entry_name": entry_name, "entry_id": entry_id,
            "start_h": start_h, "start_m": start_m, "start_s": start_s,
            "stop_h": stop_h, "stop_m": stop_m, "stop_s": stop_s,
            "btn_start": btn_start, "btn_stop": btn_stop, "lbl_status": lbl_status,
            "running": False, "last_video": None, "last_auto_on": "", "last_auto_off": "",
            "seen": set(), "gen": 0,
        }
        self.channel_rows.append(row_data)

    def save_config_to_file(self):
        data = {
            "api_key_path": self.api_key_path.get(),
            "excel_path": self.excel_path.get(),
            "audio_path": self.audio_path.get(),
            "delay_time": self.delay_time.get(),
            "auto_click": self.auto_click.get(),
            "keep_scanning": self.keep_scanning.get(),
            "max_age_sec": self.max_age.get(),
            "channels": [],
        }
        for row in self.channel_rows:
            cid = row["entry_id"].get().strip()
            if cid:
                data["channels"].append({"name": row["entry_name"].get(), "id": cid,
                                         "start": self.get_time_string(row, "start"), "stop": self.get_time_string(row, "stop")})
        try:
            tmp = self.config_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            os.replace(tmp, self.config_file)
            messagebox.showinfo("Thành công", "Đã lưu cấu hình!")
        except Exception as e:
            messagebox.showerror("Lỗi", str(e))

    def load_config_from_file(self):
        if not os.path.exists(self.config_file):
            self.add_channel_row(); return
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.api_key_path.set(data.get("api_key_path", ""))
            self.excel_path.set(data.get("excel_path", ""))
            self.audio_path.set(data.get("audio_path", ""))
            self.delay_time.set(data.get("delay_time", 0.2))
            self.auto_click.set(data.get("auto_click", True))
            self.keep_scanning.set(data.get("keep_scanning", False))
            self.max_age.set(data.get("max_age_sec", DEFAULT_MAX_AGE_SEC))
            if self.api_key_path.get():
                self.load_api_file_silent()
            channels = data.get("channels", [])
            if channels:
                for ch in channels:
                    self.add_channel_row(ch.get("name", ""), ch.get("id", ""), ch.get("start", "00:00:00"), ch.get("stop", "00:00:00"))
            else:
                self.add_channel_row()
        except Exception:
            self.add_channel_row()

    def load_api_file(self):
        p = filedialog.askopenfilename(filetypes=[("Text Files", "*.txt")])
        if p:
            self.api_key_path.set(p); self.load_api_file_silent()

    def load_api_file_silent(self):
        try:
            if os.path.exists(self.api_key_path.get()):
                with open(self.api_key_path.get(), "r", encoding="utf-8") as f:
                    self.api_keys_list = [l.strip() for l in f if l.strip()]
                self.lbl_key_status.config(text=f"({len(self.api_keys_list)} keys)", fg="#00ff00")
        except Exception:
            pass

    def load_excel_file(self):
        p = filedialog.askopenfilename(filetypes=[("Excel Files", "*.xlsx")])
        if p:
            self.excel_path.set(p)

    def load_audio_file(self):
        p = filedialog.askopenfilename(filetypes=[("Audio Files", "*.wav")])
        if p:
            self.audio_path.set(p)

    def log(self, message, tag=None):
        """An toàn từ mọi luồng (V42). Giờ lấy ngay lúc gọi, có mili-giây để so với log tool tải."""
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{ts}] {message}\n"

        def _ins():
            self.txt_log.insert(tk.END, line, tag); self.txt_log.see(tk.END)
            # giữ log gọn: tối đa ~3000 dòng
            try:
                n = int(self.txt_log.index("end-1c").split(".")[0])
                if n > 3000:
                    self.txt_log.delete("1.0", f"{n - 2500}.0")
            except Exception:
                pass
        if threading.current_thread() is threading.main_thread():
            try:
                _ins()
            except Exception:
                pass
        else:
            self.ui(_ins)

    def set_status(self, row, text, fg):
        self.ui(row["lbl_status"].config, text=text, fg=fg)

    def get_next_key(self):
        with self.key_lock:
            if not self.api_keys_list:
                return None
            k = self.api_keys_list[self.key_index % len(self.api_keys_list)]
            self.key_index = (self.key_index + 1) % len(self.api_keys_list)
            return k

    def toggle_scan(self, index, start=True):
        row = self.channel_rows[index]; cid = row["entry_id"].get().strip(); cname = row["entry_name"].get().strip()
        if start:
            if not self.api_keys_list or not cid:
                return
            if row["running"]:
                return
            row["running"] = True
            row["gen"] += 1                      # V42: bấm STOP→START nhanh không chạy 2 luồng cùng kênh
            gen = row["gen"]
            row["btn_start"].config(state="disabled", bg="#222"); row["btn_stop"].config(state="normal", bg="red")
            row["entry_id"].config(state="disabled"); row["entry_name"].config(state="disabled")
            row["lbl_status"].config(text="Starting...", fg="yellow")
            n_run = sum(1 for r in self.channel_rows if r["running"])
            used, cap = quota_estimate(n_run, self.delay_time.get(), self.req_ms_ema / 1000.0, len(self.api_keys_list))
            if used > cap:
                self.log(f"⚠️ QUOTA: {n_run} kênh × delay {self.delay_time.get()}s ≈ {used:,} đơn vị/ngày nếu chạy liên tục "
                         f"> {cap:,} ({len(self.api_keys_list)} key). Hãy dùng giờ hẹn On/Off hoặc tăng delay.", "ERROR")
            t = threading.Thread(target=self.worker_scan, args=(index, cid, cname, gen), name=f"scan-{index}")
            t.daemon = True; t.start()
        else:
            row["running"] = False; row["lbl_status"].config(text="STOPPING...", fg="orange")

    def _alive(self, row, gen):
        return row["running"] and row["gen"] == gen

    def worker_scan(self, index, channel_id, channel_name, gen):
        row = self.channel_rows[index]; dname = channel_name if channel_name else channel_id
        self.log(f"[{dname}] Start scan...", "INFO")
        init_ok = False
        for _ in range(5):
            if not self._alive(row, gen):
                break
            data, _st = self.get_latest_activity(self.get_next_key(), channel_id)
            if data:
                row["last_video"] = data['id']
                row["seen"].add(data['id'])      # mốc: video mới nhất hiện có KHÔNG bao giờ bị báo là mới
                self.set_status(row, f"Mốc: {data['id']}", "cyan")
                init_ok = True
                break
            if _st and _st.get("reason") in ("empty",) :
                # kênh chưa có activity nào: vẫn quét được (mọi video xuất hiện sau đều là mới)
                init_ok = True
                self.set_status(row, "Mốc: (trống)", "cyan")
                break
            time.sleep(1)
        if not init_ok and self._alive(row, gen):
            self.log(f"[{dname}] Lỗi khởi động.", "ERROR"); self.stop_thread_ui(index, gen); return
        self.set_status(row, "Scanning...", "#00FF00")
        last_status = "scan"
        while self._alive(row, gen):
            try:
                data, st = self.get_latest_activity(self.get_next_key(), channel_id)
                if data:
                    is_new, why, age = judge_new(data, row["seen"], st.get("server_now"),
                                                 float(self.max_age.get() or 0))
                    if is_new:
                        row["seen"].add(data['id'])
                        row["last_video"] = data['id']
                        self.on_new_video(dname, channel_id, data, st, age)
                        if not self.keep_scanning.get():
                            row["running"] = False
                            break
                    elif why == "old" and data['id'] not in row["seen"]:
                        row["seen"].add(data['id'])
                        self.log(f"↩️ [{dname}] Bỏ qua video CŨ {data['id']} (tuổi {age:.0f}s > {self.max_age.get():.0f}s, "
                                 f"loại {data['type']})", "INFO")
                    if last_status != "scan":
                        self.set_status(row, "Scanning...", "#00FF00"); last_status = "scan"
                elif st.get("reason") not in ("empty",) and not str(st.get("reason", "")).startswith("type:"):
                    if last_status != "err":
                        self.set_status(row, "API Error", "red"); last_status = "err"
                elif last_status != "scan":
                    self.set_status(row, "Scanning...", "#00FF00"); last_status = "scan"
            except Exception as e:
                self.log(f"❌ [{dname}] Lỗi vòng quét: {type(e).__name__}: {e}", "ERROR")
            try:
                d = float(self.delay_time.get())
            except Exception:
                d = 0.2
            time.sleep(max(0.0, d))
        self.stop_thread_ui(index, gen)

    # --- ĐƯỜNG NÓNG: thấy video mới → Excel → trigger → âm báo → log → (luồng riêng) mở trình duyệt ---
    def on_new_video(self, dname, channel_id, data, st, age):
        t0 = time.perf_counter()
        vid = data['id']
        url = f"https://www.youtube.com/watch?v={vid}"
        ch = data.get("channel_id") or channel_id
        path = self.excel_path.get()
        excel_msg = "không có Excel"
        if path:
            try:
                with self.excel_lock:
                    res = save_excel_atomic(path, [(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ch, url, data.get("title", ""))])
                t_x = (time.perf_counter() - t0) * 1000
                r = res[0][1] if res else None
                if r == "dup":
                    excel_msg = f"đã có trong Excel ({t_x:.0f}ms)"
                else:
                    try:
                        append_trigger(path, ch)
                        excel_msg = f"💾 Excel dòng {r} + trigger ({(time.perf_counter() - t0) * 1000:.0f}ms)"
                    except OSError as e:
                        excel_msg = f"💾 Excel dòng {r} ({t_x:.0f}ms) | ⚠️ trigger lỗi: {e}"
            except Exception as e:
                excel_msg = f"❌ Lỗi Excel: {type(e).__name__}: {e}"
        audio = self.audio_path.get()
        if winsound is not None and audio and os.path.exists(audio):
            try:
                winsound.PlaySound(audio, winsound.SND_FILENAME | winsound.SND_ASYNC)
            except Exception:
                pass
        age_s = f"tuổi {age:.1f}s" if age is not None else "tuổi ?"
        self.log(f"🚀 [{dname}] NEW: {data.get('title', '')} | {vid} | {age_s} | req={st.get('req_ms', 0):.0f}ms | {excel_msg}", "NEW")
        if self.auto_click.get():
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()

    def stop_thread_ui(self, index, gen=None):
        row = self.channel_rows[index]
        if gen is not None and row["gen"] != gen:
            return                                # đã có luồng mới cho kênh này — không đụng UI của nó
        row["running"] = False

        def _ui():
            row["btn_start"].config(state="normal", bg="green")
            row["btn_stop"].config(state="disabled", bg="#550000")
            row["entry_id"].config(state="normal")
            row["entry_name"].config(state="normal")
            for k in ["start", "stop"]:
                for t in ["h", "m", "s"]:
                    try:
                        row[f"{k}_{t}"].config(state="normal")
                    except Exception:
                        pass
            if not row["running"]:
                row["lbl_status"].config(text="IDLE", fg="#555")
        self.ui(_ui)

    def get_latest_activity(self, api_key, channel_id):
        """1 request activities (quota 1 đơn vị). Trả về (data|None, status dict)."""
        st = {"reason": "", "req_ms": 0.0, "server_now": None}
        if not api_key:
            st["reason"] = "no-key"
            return None, st
        params = {'part': 'snippet,contentDetails', 'channelId': channel_id, 'maxResults': 1,
                  'key': api_key, 'hl': 'ja', 'fields': ACTIVITIES_FIELDS}
        t_send = time.perf_counter()
        try:
            r = self.session.get(ACTIVITIES_URL, params=params, headers=self.req_headers, timeout=(2.0, 3.0))
            t_recv = time.perf_counter()
            st["req_ms"] = (t_recv - t_send) * 1000
            self.req_ms_ema = 0.9 * self.req_ms_ema + 0.1 * st["req_ms"]
            if r.status_code == 200:
                srv = server_now_from_date_header(r.headers.get("Date"), t_send, t_recv)
                if srv is not None:
                    # quy về "bây giờ" theo đồng hồ máy chủ tại thời điểm xử lý
                    srv += time.perf_counter() - t_recv
                st["server_now"] = srv
                data, why = parse_activity(r.json())
                st["reason"] = why
                if data is None and why.startswith("type:"):
                    self.log(f"⚠ Kênh {channel_id} có hoạt động lạ: {why[5:]}", "INFO")
                return data, st
            st["reason"] = f"http{r.status_code}"
            self.log(f"❌ Lỗi HTTP {r.status_code} ở kênh {channel_id} (Key: {api_key[:8]}...)", "ERROR")
        except requests.exceptions.Timeout:
            st["reason"] = "timeout"
            self.log(f"⚠ Timeout - Nghẽn mạng ở kênh {channel_id}", "ERROR")
        except Exception as e:
            st["reason"] = type(e).__name__
            self.log(f"❌ Kẹt mạng/Lỗi khác: {type(e).__name__}: {e}", "ERROR")
        return None, st


if __name__ == "__main__":
    root = tk.Tk(); app = YoutubeSniperMulti(root); root.mainloop()
