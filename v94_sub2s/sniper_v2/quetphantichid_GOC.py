import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog
import requests
import threading
import time
import webbrowser
from datetime import datetime
import random
import os
import json
import winsound  # Thêm thư viện phát âm thanh hệ thống

# --- GIỮ NGUYÊN IMPORT EXCEL ---
try:
    import openpyxl
except ImportError:
    pass

class YoutubeSniperMulti:
    def __init__(self, root):
        self.root = root
        self.root.title("VPS SNIPER PRO (V41 - API METADATA FIXED)") 
        self.root.geometry("790x780") # Tăng nhẹ chiều cao để chứa thêm dòng âm thanh
        self.root.configure(bg="#000000") 

        # --- BIẾN HỆ THỐNG ---
        self.config_file = "config.json"
        self.api_key_path = tk.StringVar()
        self.excel_path = tk.StringVar()
        self.audio_path = tk.StringVar() # Biến lưu đường dẫn âm thanh
        self.delay_time = tk.DoubleVar(value=0.2)
        
        self.auto_click = tk.BooleanVar(value=True) 
        
        self.api_keys_list = []
        self.key_index = 0
        self.key_lock = threading.Lock()
        self.excel_lock = threading.Lock() 

        self.channel_rows = [] 

        # SESSION GIẢ DẠNG TRÌNH DUYỆT
        self.session = requests.Session()
        
        # --- BẢN VÁ 1: MỞ RỘNG BĂNG THÔNG CHO ĐA LUỒNG ---
        adapter = requests.adapters.HTTPAdapter(pool_connections=150, pool_maxsize=150, max_retries=1)
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)
        # -------------------------------------------------

        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Connection': 'keep-alive'
        })

        self.setup_ui()
        self.load_config_from_file()
        self.start_scheduler()

    def setup_ui(self):
        # 1. KHUNG CẤU HÌNH CHUNG
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

        # --- THÊM DÒNG CHỌN FILE ÂM THANH ---
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

        # 2. KHUNG CHỨA DANH SÁCH KÊNH
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

        # 3. LOG TỔNG
        tk.Label(self.root, text=">>> SYSTEM LOG:", bg="#000000", fg="#00ff00").pack(anchor="w", padx=10)
        self.txt_log = scrolledtext.ScrolledText(self.root, height=10, bg="#111", fg="#00ff00", font=("Consolas", 9))
        self.txt_log.pack(padx=10, pady=5, fill="x")
        self.txt_log.tag_config("NEW", foreground="yellow", background="#AA0000", font=("Consolas", 10, "bold"))
        self.txt_log.tag_config("DONE", foreground="#00FF00")
        self.txt_log.tag_config("INFO", foreground="#00FFFF")
        self.txt_log.tag_config("AUTO", foreground="#FF00FF")
        self.txt_log.tag_config("META", foreground="#FFA500") 

    def start_scheduler(self):
        self.scheduler_tick()

    def scheduler_tick(self):
        try:
            now_str = datetime.now().strftime("%H:%M:%S")
            for i, row in enumerate(self.channel_rows):
                start_time = self.get_time_string(row, "start")
                stop_time = self.get_time_string(row, "stop")
                if start_time and start_time == now_str:
                    if not row["running"] and row["last_auto_on"] != now_str:
                        row["last_auto_on"] = now_str 
                        self.auto_start_trigger(i, start_time)
                if stop_time and stop_time == now_str:
                    if row["running"] and row["last_auto_off"] != now_str:
                        row["last_auto_off"] = now_str
                        self.auto_stop_trigger(i, stop_time)
        except Exception: pass
        self.root.after(1000, self.scheduler_tick)

    def get_time_string(self, row, type_):
        try:
            h = row[f"{type_}_h"].get()
            m = row[f"{type_}_m"].get()
            s = row[f"{type_}_s"].get()
            return f"{int(h):02}:{int(m):02}:{int(s):02}"
        except: return ""

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
            except: h, m, s = 0, 0, 0
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
            "running": False, "last_video": None, "last_auto_on": "", "last_auto_off": ""
        }
        self.channel_rows.append(row_data)

    def save_config_to_file(self):
        data = {
            "api_key_path": self.api_key_path.get(), 
            "excel_path": self.excel_path.get(), 
            "audio_path": self.audio_path.get(), # Lưu đường dẫn audio
            "delay_time": self.delay_time.get(), 
            "auto_click": self.auto_click.get(), 
            "channels": []
        }
        for row in self.channel_rows:
            cid = row["entry_id"].get().strip()
            if cid: data["channels"].append({"name": row["entry_name"].get(), "id": cid, "start": self.get_time_string(row, "start"), "stop": self.get_time_string(row, "stop")})
        try:
            with open(self.config_file, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=4)
            messagebox.showinfo("Thành công", "Đã lưu cấu hình!")
        except Exception as e: messagebox.showerror("Lỗi", str(e))

    def load_config_from_file(self):
        if not os.path.exists(self.config_file): self.add_channel_row(); return
        try:
            with open(self.config_file, "r", encoding="utf-8") as f: data = json.load(f)
            self.api_key_path.set(data.get("api_key_path", ""))
            self.excel_path.set(data.get("excel_path", ""))
            self.audio_path.set(data.get("audio_path", "")) # Load đường dẫn audio
            self.delay_time.set(data.get("delay_time", 0.2))
            self.auto_click.set(data.get("auto_click", True))
            if self.api_key_path.get(): self.load_api_file_silent()
            channels = data.get("channels", [])
            if channels:
                for ch in channels: self.add_channel_row(ch.get("name", ""), ch.get("id", ""), ch.get("start", "00:00:00"), ch.get("stop", "00:00:00"))
            else: self.add_channel_row()
        except: self.add_channel_row()

    def load_api_file(self):
        p = filedialog.askopenfilename(filetypes=[("Text Files", "*.txt")])
        if p: self.api_key_path.set(p); self.load_api_file_silent()

    def load_api_file_silent(self):
        try:
            if os.path.exists(self.api_key_path.get()):
                with open(self.api_key_path.get(), "r", encoding="utf-8") as f: self.api_keys_list = [l.strip() for l in f if l.strip()]
                self.lbl_key_status.config(text=f"({len(self.api_keys_list)} keys)", fg="#00ff00")
        except: pass

    def load_excel_file(self):
        p = filedialog.askopenfilename(filetypes=[("Excel Files", "*.xlsx")])
        if p: self.excel_path.set(p)

    # --- HÀM MỚI: CHỌN FILE ÂM THANH ---
    def load_audio_file(self):
        p = filedialog.askopenfilename(filetypes=[("Audio Files", "*.wav")])
        if p: self.audio_path.set(p)

    def log(self, message, tag=None):
        try:
            ts = datetime.now().strftime("%H:%M:%S")
            self.txt_log.insert(tk.END, f"[{ts}] {message}\n", tag); self.txt_log.see(tk.END)
        except: pass

    def get_next_key(self):
        with self.key_lock:
            if not self.api_keys_list: return None
            k = self.api_keys_list[self.key_index]
            self.key_index = (self.key_index + 1) % len(self.api_keys_list); return k

    def toggle_scan(self, index, start=True):
        row = self.channel_rows[index]; cid = row["entry_id"].get().strip(); cname = row["entry_name"].get().strip()
        if start:
            if not self.api_keys_list or not cid: return
            row["running"] = True; row["btn_start"].config(state="disabled", bg="#222"); row["btn_stop"].config(state="normal", bg="red")
            row["entry_id"].config(state="disabled"); row["entry_name"].config(state="disabled")
            row["lbl_status"].config(text="Starting...", fg="yellow")
            t = threading.Thread(target=self.worker_scan, args=(index, cid, cname))
            t.daemon = True; t.start()
        else:
            row["running"] = False; row["lbl_status"].config(text="STOPPING...", fg="orange")

    def worker_scan(self, index, channel_id, channel_name):
        row = self.channel_rows[index]; dname = channel_name if channel_name else channel_id
        self.log(f"[{dname}] Start scan...", "INFO")
        init_ok = False
        for _ in range(5):
            if not row["running"]: break
            k = self.get_next_key(); data = self.get_latest_activity(k, channel_id)
            if data: row["last_video"] = data['id']; row["lbl_status"].config(text=f"Mốc: {data['id']}", fg="cyan"); init_ok = True; break
            time.sleep(1)
        if not init_ok and row["running"]: self.log(f"[{dname}] Lỗi khởi động.", "ERROR"); self.stop_thread_ui(index); return
        row["lbl_status"].config(text="Scanning...", fg="#00FF00")
        while row["running"]:
            try:
                k = self.get_next_key(); data = self.get_latest_activity(k, channel_id)
                if data and data['id'] != row["last_video"]:
                    # --- PHÁT ÂM THANH NGAY KHI PHÁT HIỆN ---
                    audio = self.audio_path.get()
                    if audio and os.path.exists(audio):
                        winsound.PlaySound(audio, winsound.SND_FILENAME | winsound.SND_ASYNC)
                    
                    self.log(f"🚀 [{dname}] NEW: {data['title']}", "NEW")
                    url = f"https://www.youtube.com/watch?v={data['id']}"
                    if self.auto_click.get(): webbrowser.open(url)
                    # GỌI HÀM PHÂN TÍCH BẰNG API (ĐÃ THAY THẾ YT-DLP)
                    t_analyze = threading.Thread(target=self.analyze_metadata_with_api, args=(data['id'], dname, url))
                    t_analyze.daemon = True; t_analyze.start()
                    row["running"] = False; break
                elif not data: 
                    row["lbl_status"].config(text="API Error", fg="red")
                else: 
                    # --- BẢN VÁ 2: RESET LẠI TRẠNG THÁI XANH SAU KHI LỖI ---
                    row["lbl_status"].config(text="Scanning...", fg="#00FF00")
            except: pass
            time.sleep(self.delay_time.get())
        self.stop_thread_ui(index)

    # --- HÀM VÁ: DÙNG API THAY CHO YT-DLP ĐỂ LẤY METADATA ---
    def analyze_metadata_with_api(self, video_id, display_name, url):
        self.log(f"[{display_name}] Đang lấy Metadata qua API...", "META")
        title = ""; channel_id = ""
        try:
            key = self.get_next_key()
            api_url = "https://www.googleapis.com/youtube/v3/videos"
            params = {'part': 'snippet', 'id': video_id, 'key': key, 'hl': 'ja'}
            r = self.session.get(api_url, params=params, timeout=5)
            if r.status_code == 200:
                items = r.json().get("items", [])
                if items:
                    snippet = items[0]["snippet"]
                    title = snippet.get("title", "")
                    channel_id = snippet.get("channelId", "")
                    self.log(f"✅ [{display_name}] Metadata API OK", "META")
                else: self.log(f"❌ [{display_name}] Không tìm thấy Video qua API.", "ERROR")
            else: self.log(f"❌ [{display_name}] API Metadata lỗi: {r.status_code}", "ERROR")
        except Exception as e: self.log(f"❌ [{display_name}] Lỗi: {str(e)}", "ERROR")
        
        # GIỮ NGUYÊN LOGIC GHI EXCEL
        self.save_excel_enhanced(url, channel_id, title)

    def save_excel_enhanced(self, url, channel_id, title):
        path = self.excel_path.get()
        if not path: return
        with self.excel_lock:
            try:
                wb = openpyxl.load_workbook(path); ws = wb.active; nr = ws.max_row + 1
                ws.cell(row=nr, column=1).value = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                ws.cell(row=nr, column=2).value = channel_id
                ws.cell(row=nr, column=4).value = url
                ws.cell(row=nr, column=8).value = title
                wb.save(path); self.log(f"💾 Đã lưu Excel dòng {nr}", "DONE")
            except Exception as e: self.log(f"❌ Lỗi Excel: {e}", "ERROR")

    def stop_thread_ui(self, index):
        row = self.channel_rows[index]; row["running"] = False
        self.root.after(0, lambda: row["btn_start"].config(state="normal", bg="green"))
        self.root.after(0, lambda: row["btn_stop"].config(state="disabled", bg="#550000"))
        self.root.after(0, lambda: row["entry_id"].config(state="normal"))
        self.root.after(0, lambda: row["entry_name"].config(state="normal"))
        for k in ["start", "stop"]:
            for t in ["h", "m", "s"]:
                try: row[f"{k}_{t}"].config(state="normal")
                except: pass

    # --- BẢN VÁ 3: NÂNG CẤP HÀM LẤY HOẠT ĐỘNG VÀ BẮT LỖI CHI TIẾT ---
    def get_latest_activity(self, api_key, channel_id):
        url = "https://www.googleapis.com/youtube/v3/activities"
        params = {'part': 'snippet,contentDetails', 'channelId': channel_id, 'maxResults': 1, 'key': api_key, 'hl': 'ja'}
        
        headers = self.session.headers.copy()
        headers.update({'Cache-Control': 'no-cache', 'Pragma': 'no-cache'})
        
        try:
            r = self.session.get(url, params=params, headers=headers, timeout=4)
            if r.status_code == 200:
                items = r.json().get("items", [])
                if items:
                    item = items[0]; snip = item["snippet"]; det = item["contentDetails"]
                    vid = None
                    if snip["type"] == "upload": vid = det["upload"]["videoId"]
                    elif snip["type"] == "playlistItem": vid = det["playlistItem"]["resourceId"]["videoId"]
                    
                    if vid: return {'id': vid, 'title': snip["title"]}
                    else:
                        self.log(f"⚠ Kênh {channel_id} có hoạt động lạ: {snip['type']}", "INFO")
                else:
                    self.log(f"⚠ Kênh {channel_id} trống dữ liệu activities.", "INFO")
            else:
                self.log(f"❌ Lỗi HTTP {r.status_code} ở kênh {channel_id} (Key: {api_key[:8]}...)", "ERROR")
        except requests.exceptions.Timeout:
            self.log(f"⚠ Timeout - Nghẽn mạng ở kênh {channel_id}", "ERROR")
        except Exception as e: 
            self.log(f"❌ Kẹt mạng/Lỗi khác: {str(e)}", "ERROR")
            pass
            
        return None

if __name__ == "__main__":
    root = tk.Tk(); app = YoutubeSniperMulti(root); root.mainloop()