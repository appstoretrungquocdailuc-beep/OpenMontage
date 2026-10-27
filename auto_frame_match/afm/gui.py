"""Giao diện tiếng Việt - AUTO FRAME MATCH 2K.

Chạy: nhấp đúp CHAY_AUTO_FRAME_MATCH.bat (Windows) hoặc ``python -m afm.gui``.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from .config import TOOL_ROOT, TOOL_VERSION, load_config
from .log import setup_logging
from .matcher import GREEN, RED, YELLOW

VIDEO_TYPES = [("Video", "*.mp4 *.mkv *.mov *.avi *.flv *.ts *.webm *.m4v"), ("Tất cả", "*.*")]
SETTINGS = TOOL_ROOT / "gui_settings.json"
COLORS = {GREEN: "#c8f0d2", YELLOW: "#fff1b8", RED: "#ffcdcd"}
LABEL = {GREEN: "XANH", YELLOW: "VÀNG", RED: "ĐỎ"}


class QueueHandler(logging.Handler):
    def __init__(self, q):
        super().__init__()
        self.q = q

    def emit(self, record):
        self.q.put(("log", self.format(record)))


def open_path(p: str):
    try:
        if sys.platform == "win32":
            os.startfile(p)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
    except Exception as e:
        messagebox.showerror("Lỗi", f"Không mở được: {p}\n{e}")


class SourceList(ttk.Frame):
    """Danh sách video gốc: ID, file, bản Video2X (tùy chọn), độ trễ audio."""

    def __init__(self, master, title: str):
        super().__init__(master)
        ttk.Label(self, text=title, font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w")
        cols = ("id", "file", "v2x", "offset")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=4)
        for c, t, w in (("id", "ID", 50), ("file", "Video gốc (âm thanh tiếng Nhật)", 380),
                        ("v2x", "Bản Video2X dùng để dựng (tùy chọn)", 300), ("offset", "Trễ audio (ms)", 100)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w" if c in ("file", "v2x") else "center")
        self.tree.grid(row=1, column=0, sticky="nsew")
        bar = ttk.Frame(self)
        bar.grid(row=1, column=1, sticky="n", padx=(6, 0))
        for txt, cmd in (("Thêm video gốc…", self.add), ("Chọn bản Video2X…", self.set_v2x),
                         ("Độ trễ audio…", self.set_offset), ("Lên", lambda: self.move(-1)),
                         ("Xuống", lambda: self.move(1)), ("Xóa", self.remove)):
            ttk.Button(bar, text=txt, command=cmd, width=18).pack(pady=1)
        self.columnconfigure(0, weight=1)
        self.items: list[dict] = []

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for k, it in enumerate(self.items, 1):
            it["id"] = f"S{k}"
            self.tree.insert("", "end", iid=str(k - 1), values=(
                it["id"], it["path"], it.get("render_path") or "—", f"{it.get('audio_offset_ms', 0):g}"))

    def add(self):
        for p in filedialog.askopenfilenames(title="Chọn 1 hoặc nhiều video gốc", filetypes=VIDEO_TYPES):
            self.items.append({"path": p, "render_path": None, "audio_offset_ms": 0.0})
        self.refresh()

    def _sel(self):
        s = self.tree.selection()
        if not s:
            messagebox.showinfo("Chọn nguồn", "Hãy chọn một dòng video gốc trước.")
            return None
        return int(s[0])

    def set_v2x(self):
        i = self._sel()
        if i is None:
            return
        p = filedialog.askopenfilename(title=f"Bản Video2X của {self.items[i]['id']} (Hủy = bỏ chọn)",
                                       filetypes=VIDEO_TYPES)
        self.items[i]["render_path"] = p or None
        self.refresh()

    def set_offset(self):
        i = self._sel()
        if i is None:
            return
        v = simpledialog.askfloat("Độ trễ audio", "Độ trễ audio riêng của nguồn này (ms).\n"
                                  "Dương = lấy tiếng muộn hơn hình, âm = sớm hơn.",
                                  initialvalue=self.items[i].get("audio_offset_ms", 0.0))
        if v is not None:
            self.items[i]["audio_offset_ms"] = float(v)
            self.refresh()

    def move(self, d):
        i = self._sel()
        if i is None or not 0 <= i + d < len(self.items):
            return
        self.items[i], self.items[i + d] = self.items[i + d], self.items[i]
        self.refresh()
        self.tree.selection_set(str(i + d))

    def remove(self):
        i = self._sel()
        if i is not None:
            del self.items[i]
            self.refresh()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"AUTO FRAME MATCH 2K — Phân tích & cắt ghép 100% trên máy (v{TOOL_VERSION})")
        self.geometry("1180x860")
        self.minsize(980, 700)
        try:
            ttk.Style().theme_use("vista" if sys.platform == "win32" else "clam")
        except tk.TclError:
            pass
        self.q: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.last_out: str | None = None
        self.last_report: str | None = None
        self._build()
        self._load_settings()
        logf = setup_logging(TOOL_ROOT / "logs", logging.INFO, QueueHandler(self.q))
        self._log(f"Log được ghi tại: {logf}")
        self.after(100, self._poll)

    # ------------------------------------------------------------------ giao diện
    def _build(self):
        pad = {"padx": 8, "pady": 4}
        nb = ttk.Notebook(self)
        nb.pack(fill="x", **pad)
        self.nb = nb
        # --- Chế độ A
        fa = ttk.Frame(nb, padding=8)
        nb.add(fa, text="  A. PHÂN TÍCH & CẮT TỰ ĐỘNG  ")
        self.edit_var = tk.StringVar()
        self.out_a = tk.StringVar()
        self._file_row(fa, 0, "Video Douyin đã edit:", self.edit_var, VIDEO_TYPES)
        self.src_a = SourceList(fa, "Video gốc (1, 2, 3 hoặc nhiều tập):")
        self.src_a.grid(row=1, column=0, columnspan=3, sticky="nsew", pady=6)
        self._dir_row(fa, 2, "Thư mục xuất:", self.out_a)
        self.btn_a = tk.Button(fa, text="TỰ ĐỘNG PHÂN TÍCH & CẮT GHÉP", bg="#1f6feb", fg="white",
                               font=("Segoe UI", 13, "bold"), height=2, command=self.start_a)
        self.btn_a.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        fa.columnconfigure(1, weight=1)
        # --- Chế độ B
        fb = ttk.Frame(nb, padding=8)
        nb.add(fb, text="  B. CẮT THEO BẢNG CÓ SẴN  ")
        self.table_var = tk.StringVar()
        self.out_b = tk.StringVar()
        self._file_row(fb, 0, "Bảng cắt (CSV/JSON):", self.table_var,
                       [("Bảng cắt", "*.csv *.json"), ("Tất cả", "*.*")])
        self.src_b = SourceList(fb, "Video gốc (tool tự nhận source_id theo vân tay):")
        self.src_b.grid(row=1, column=0, columnspan=3, sticky="nsew", pady=6)
        self._dir_row(fb, 2, "Thư mục xuất:", self.out_b)
        self.btn_b = tk.Button(fb, text="CẮT GHÉP THEO BẢNG", bg="#8250df", fg="white",
                               font=("Segoe UI", 13, "bold"), height=2, command=self.start_b)
        self.btn_b.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        fb.columnconfigure(1, weight=1)
        # --- tùy chọn chung
        opt = ttk.LabelFrame(self, text="Tùy chọn xuất", padding=6)
        opt.pack(fill="x", **pad)
        self.draft = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="Cho phép xuất BẢN NHÁP khi chưa xác minh 100% (file *_CHUA_XAC_MINH)",
                        variable=self.draft).grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(opt, text="Khung hình:").grid(row=1, column=0, sticky="e")
        self.crop = ttk.Combobox(opt, state="readonly", width=34,
                                 values=["Giống bản Douyin (crop/zoom/lật)", "Toàn khung nguồn (chỉ lật)"])
        self.crop.current(0)
        self.crop.grid(row=1, column=1, sticky="w", padx=4)
        ttk.Label(opt, text="Bộ mã hóa:").grid(row=1, column=2, sticky="e")
        self.codec = ttk.Combobox(opt, state="readonly", width=30,
                                  values=["Tự động (H.264 NVENC nếu có GPU)", "H.264 NVENC (RTX)", "x264 (CPU)"])
        self.codec.current(0)
        self.codec.grid(row=1, column=3, sticky="w", padx=4)
        ttk.Label(opt, text="Chất lượng (CQ/CRF, nhỏ = đẹp):").grid(row=2, column=0, sticky="e")
        self.cq = tk.IntVar(value=18)
        ttk.Spinbox(opt, from_=12, to=30, textvariable=self.cq, width=6).grid(row=2, column=1, sticky="w", padx=4)
        ttk.Label(opt, text="Xuất 2K/QHD 1440p · âm thanh tiếng Nhật lấy từ video gốc · không dùng tiếng Douyin",
                  foreground="#555").grid(row=2, column=2, columnspan=2, sticky="w")
        # --- tiến độ
        prog = ttk.Frame(self)
        prog.pack(fill="x", **pad)
        self.banner = tk.Label(prog, text="Sẵn sàng", font=("Segoe UI", 12, "bold"), bg="#e9ecef", anchor="w",
                               padx=10, pady=6)
        self.banner.pack(fill="x")
        self.step_lbl = ttk.Label(prog, text="")
        self.step_lbl.pack(fill="x", pady=(4, 0))
        self.pb_step = ttk.Progressbar(prog, maximum=1000)
        self.pb_step.pack(fill="x")
        self.pb_all = ttk.Progressbar(prog, maximum=1000)
        self.pb_all.pack(fill="x", pady=(2, 0))
        bar = ttk.Frame(prog)
        bar.pack(fill="x", pady=4)
        self.btn_cancel = ttk.Button(bar, text="Hủy / Tạm dừng", command=self.do_cancel, state="disabled")
        self.btn_cancel.pack(side="left")
        ttk.Button(bar, text="Mở thư mục xuất", command=lambda: self.last_out and open_path(self.last_out)
                   ).pack(side="left", padx=4)
        ttk.Button(bar, text="Mở QA_REPORT.html", command=lambda: self.last_report and open_path(self.last_report)
                   ).pack(side="left")
        ttk.Button(bar, text="Xóa cache fingerprint…", command=self.clear_cache).pack(side="right")
        # --- kết quả + log
        pw = ttk.PanedWindow(self, orient="vertical")
        pw.pack(fill="both", expand=True, **pad)
        rf = ttk.Frame(pw)
        cols = ("seg", "st", "edit", "time", "src", "sframe", "speed", "flip", "score", "note")
        self.res = ttk.Treeview(rf, columns=cols, show="headings", height=9)
        for c, t, w in (("seg", "#", 40), ("st", "Trạng thái", 80), ("edit", "Khung edit", 100),
                        ("time", "Thời gian edit", 120), ("src", "Nguồn", 60), ("sframe", "Khung nguồn", 100),
                        ("speed", "Tốc độ", 60), ("flip", "Lật", 40), ("score", "Điểm", 60),
                        ("note", "Ghi chú / lý do", 460)):
            self.res.heading(c, text=t)
            self.res.column(c, width=w, anchor="w" if c == "note" else "center")
        for st, col in COLORS.items():
            self.res.tag_configure(st, background=col)
        sb = ttk.Scrollbar(rf, orient="vertical", command=self.res.yview)
        self.res.configure(yscrollcommand=sb.set)
        self.res.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        pw.add(rf, weight=3)
        lf = ttk.Frame(pw)
        self.logbox = tk.Text(lf, height=8, wrap="none", font=("Consolas", 9))
        lsb = ttk.Scrollbar(lf, orient="vertical", command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=lsb.set)
        self.logbox.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")
        pw.add(lf, weight=2)

    def _file_row(self, parent, row, label, var, types):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="e")
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", padx=4)
        ttk.Button(parent, text="Chọn…", command=lambda: var.set(
            filedialog.askopenfilename(filetypes=types) or var.get())).grid(row=row, column=2)

    def _dir_row(self, parent, row, label, var):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="e")
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", padx=4)
        ttk.Button(parent, text="Chọn…", command=lambda: var.set(
            filedialog.askdirectory() or var.get())).grid(row=row, column=2)

    # ------------------------------------------------------------------ cấu hình
    def _cfg(self):
        cfg = load_config()
        cfg.render.allow_unverified = bool(self.draft.get())
        cfg.render.crop_mode = "match_edit" if self.crop.current() == 0 else "full_source"
        cfg.render.codec = ["auto", "h264_nvenc", "libx264"][self.codec.current()]
        cfg.render.nvenc_cq = cfg.render.x264_crf = int(self.cq.get())
        return cfg

    def _load_settings(self):
        try:
            d = json.loads(SETTINGS.read_text("utf-8"))
        except Exception:
            return
        self.out_a.set(d.get("out_a", ""))
        self.out_b.set(d.get("out_b", ""))
        self.draft.set(bool(d.get("draft", False)))

    def _save_settings(self):
        try:
            SETTINGS.write_text(json.dumps({"out_a": self.out_a.get(), "out_b": self.out_b.get(),
                                            "draft": bool(self.draft.get())}, ensure_ascii=False), "utf-8")
        except Exception:
            pass

    # ------------------------------------------------------------------ chạy
    def _busy(self, on: bool):
        st = "disabled" if on else "normal"
        self.btn_a.configure(state=st)
        self.btn_b.configure(state=st)
        self.btn_cancel.configure(state="normal" if on else "disabled")

    def _set_banner(self, text, st=None):
        self.banner.configure(text=text, bg={GREEN: "#1f9d55", YELLOW: "#e3b100", RED: "#d64545"}.get(st, "#e9ecef"),
                              fg="white" if st in (GREEN, RED) else "black")

    def start_a(self):
        edit = self.edit_var.get().strip()
        if not edit or not Path(edit).exists():
            return messagebox.showwarning("Thiếu video", "Hãy chọn video Douyin đã edit.")
        if not self.src_a.items:
            return messagebox.showwarning("Thiếu video gốc", "Hãy thêm ít nhất 1 video gốc.")
        out = self.out_a.get().strip() or str(Path(edit).with_suffix("")) + "_AUTO_MATCH"
        self.out_a.set(out)
        from .pipeline import run_mode_a

        self._run("A", lambda cfg, rep: run_mode_a(edit, [dict(s) for s in self.src_a.items], out, cfg, rep,
                                                   self.cancel), out)

    def start_b(self):
        tbl = self.table_var.get().strip()
        if not tbl or not Path(tbl).exists():
            return messagebox.showwarning("Thiếu bảng", "Hãy chọn file BANG_CAT_AUTO.csv hoặc .json.")
        if not self.src_b.items:
            return messagebox.showwarning("Thiếu video gốc", "Hãy thêm các video gốc mà bảng cắt sử dụng.")
        out = self.out_b.get().strip() or str(Path(tbl).parent / "CAT_THEO_BANG")
        self.out_b.set(out)
        from .pipeline import run_mode_b

        self._run("B", lambda cfg, rep: run_mode_b(tbl, [dict(s) for s in self.src_b.items], out, cfg, rep,
                                                   self.cancel), out)

    def _run(self, mode, fn, out):
        self._save_settings()
        self.cancel.clear()
        self.res.delete(*self.res.get_children())
        self._busy(True)
        self._set_banner(f"Đang chạy chế độ {mode}…")
        self.last_out = out
        cfg = self._cfg()
        t0 = time.time()

        def rep(step, name, frac, msg):
            self.q.put(("prog", step, name, frac, msg))

        def target():
            try:
                res = fn(cfg, rep)
                self.q.put(("done", res, time.time() - t0))
            except InterruptedError:
                self.q.put(("error", "Đã hủy. Chạy lại sẽ TIẾP TỤC từ phần đã xong (cache + checkpoint)."))
            except Exception as e:
                logging.getLogger("afm").exception("Lỗi")
                self.q.put(("error", str(e)))

        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()

    def do_cancel(self):
        self.cancel.set()
        self._log("Đang hủy… (sẽ dừng ở điểm an toàn gần nhất)")

    def clear_cache(self):
        cdir = Path(load_config().cache_dir)
        if messagebox.askyesno("Xóa cache", f"Xóa toàn bộ cache fingerprint tại:\n{cdir}\n\n"
                               "Lần sau sẽ phải quét lại video gốc."):
            import shutil

            shutil.rmtree(cdir, ignore_errors=True)
            self._log("Đã xóa cache.")

    # ------------------------------------------------------------------ hàng đợi
    def _log(self, s):
        self.logbox.insert("end", s + "\n")
        self.logbox.see("end")

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._log(item[1])
                elif kind == "prog":
                    _, step, name, frac, msg = item
                    self.step_lbl.configure(text=f"{name} — {msg}")
                    self.pb_step["value"] = frac * 1000
                    self.pb_all["value"] = (step + frac) / 7 * 1000
                elif kind == "done":
                    self._done(item[1], item[2])
                elif kind == "error":
                    self._busy(False)
                    self._set_banner("Lỗi / dừng: " + item[1][:160], RED)
                    messagebox.showerror("Lỗi", item[1])
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _done(self, res, secs):
        self._busy(False)
        self.pb_all["value"] = 1000
        self.last_report = res.get("report")
        segs = res.get("segments") or []
        if not segs and res.get("report"):
            try:
                doc = json.loads((Path(res["out_dir"]) / "BANG_CAT_AUTO.json").read_text("utf-8"))
                segs = doc["segments"]
            except Exception:
                segs = []
        for r in segs:
            st = r.get("status", RED)
            sf = f"{r.get('source_frame_start', '')}–{r.get('source_frame_end', '')}" if r.get("source_frame_start") \
                not in (None, "") else "—"
            self.res.insert("", "end", tags=(st,), values=(
                r["segment_id"], LABEL.get(st, st), f"{r['edit_frame_start']}–{r['edit_frame_end']}",
                f"{float(r['edit_time_start']):.2f}–{float(r['edit_time_end']):.2f}s", r.get("source_id"), sf,
                r.get("speed", ""), "Có" if str(r.get("flip")) == "1" else "", r.get("score", ""),
                r.get("reasons", "")))
        summ = res.get("summary") or {}
        qa = res.get("qa")
        if res.get("verified"):
            self._set_banner(f"XANH — ĐÃ XÁC MINH. Thành phẩm: {res['render']['output']}  ({secs:.0f}s)", GREEN)
        else:
            pct = 100 * summ.get("verified_ratio", 0) if summ else 0
            reason = []
            if summ:
                reason.append(f"{summ.get('frames_green', 0)}/{summ.get('frames_total', 0)} khung XANH "
                              f"({pct:.1f}%), {summ.get('frames_yellow', 0)} VÀNG, {summ.get('frames_red', 0)} ĐỎ")
            if qa:
                reason.append(f"QA thành phẩm: {LABEL.get(qa['status'])}")
            if not res.get("render"):
                reason.append("CHƯA xuất video (cần kiểm tra thủ công hoặc bật bản nháp)")
            st = RED if (summ and summ.get("frames_red")) or (qa and qa["status"] == RED) else YELLOW
            self._set_banner("CHƯA XÁC MINH 100% — " + "; ".join(reason), st)
        self._log(f"Hoàn tất sau {secs:.0f}s. Báo cáo: {res.get('report')}")


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
