"""Tab "C. DUYỆT CẢNH VÀNG/ĐỎ": xem ảnh Douyin cạnh ảnh nguồn, chấp nhận / sửa nguồn / duyệt fallback sạch.

Ghi quyết định vào cột manual_action của BANG_CAT_AUTO.json + .csv để cắt lại bằng Chế độ B.
"""

from __future__ import annotations

import base64
import json
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import cv2

from .media import grab_frames
from .table import MANUAL_FALLBACK, MANUAL_OK, SEG_FIELDS, write_csv

THUMB_W = 420


def _photo(img_bgr) -> tk.PhotoImage | None:
    if img_bgr is None:
        return None
    h, w = img_bgr.shape[:2]
    img = cv2.resize(img_bgr, (THUMB_W, max(2, int(h * THUMB_W / w))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".png", img)
    return tk.PhotoImage(data=base64.b64encode(buf.tobytes()).decode()) if ok else None


class ReviewTab(ttk.Frame):
    def __init__(self, master, on_cut=None):
        super().__init__(master, padding=6)
        self.on_cut = on_cut
        self.doc = None
        self.path: Path | None = None
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Button(bar, text="Mở bảng cắt (BANG_CAT_AUTO.json)…", command=self.open).pack(side="left")
        self.only_bad = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Chỉ hiện cảnh VÀNG/ĐỎ", variable=self.only_bad,
                        command=self.refresh).pack(side="left", padx=8)
        self.info = ttk.Label(bar, text="")
        self.info.pack(side="left", padx=8)
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, pady=4)
        cols = ("seg", "st", "edit", "src", "clean", "act")
        self.tree = ttk.Treeview(body, columns=cols, show="headings", height=8)
        for c, t, w in (("seg", "#", 40), ("st", "TT", 60), ("edit", "Khung edit", 110), ("src", "Nguồn@khung", 110),
                        ("clean", "Kiểm sạch", 90), ("act", "Duyệt", 110)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="center")
        self.tree.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show())
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=8)
        imgs = ttk.Frame(right)
        imgs.pack(fill="x")
        self.lbl_e = ttk.Label(imgs, text="Ảnh Douyin")
        self.lbl_e.grid(row=0, column=0, padx=4)
        self.lbl_s = ttk.Label(imgs, text="Ảnh nguồn")
        self.lbl_s.grid(row=0, column=1, padx=4)
        self.reason = tk.Text(right, height=4, wrap="word", font=("Segoe UI", 9))
        self.reason.pack(fill="x", pady=4)
        form = ttk.Frame(right)
        form.pack(fill="x")
        ttk.Label(form, text="Nguồn:").pack(side="left")
        self.sid = ttk.Combobox(form, width=6)
        self.sid.pack(side="left", padx=4)
        ttk.Label(form, text="Khung nguồn bắt đầu:").pack(side="left")
        self.sframe = ttk.Entry(form, width=8)
        self.sframe.pack(side="left", padx=4)
        ttk.Label(form, text="Tốc độ:").pack(side="left")
        self.speed = ttk.Entry(form, width=6)
        self.speed.pack(side="left", padx=4)
        btns = ttk.Frame(right)
        btns.pack(fill="x", pady=4)
        for txt, cmd in (("Chấp nhận nguồn (OK)", self.accept), ("Sửa nguồn/khung rồi chấp nhận", self.edit_src),
                         ("Duyệt dùng khung Douyin sạch", self.approve_fb), ("Bỏ duyệt", self.clear)):
            ttk.Button(btns, text=txt, command=cmd).pack(side="left", padx=2)
        ttk.Button(btns, text="Lưu bảng & CẮT THEO BẢNG ĐÃ DUYỆT", command=self.save_and_cut).pack(side="right")
        self._ph = []

    # ------------------------------------------------------------------
    def open(self, path: str | None = None):
        path = path or filedialog.askopenfilename(filetypes=[("Bảng cắt JSON", "*.json")])
        if not path:
            return
        self.path = Path(path)
        self.doc = json.loads(self.path.read_text("utf-8-sig"))
        self.sid["values"] = sorted((self.doc.get("sources") or {}).keys())
        self.refresh()

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        if not self.doc:
            return
        n_bad = 0
        for r in self.doc["segments"]:
            bad = r.get("status") != "XANH"
            n_bad += bad
            if self.only_bad.get() and not bad:
                continue
            src = f"{r.get('source_id')}@{r.get('source_frame_start', '')}" if r.get("source_id") != "UNMATCHED" else "—"
            self.tree.insert("", "end", iid=str(r["segment_id"]), values=(
                r["segment_id"], {"XANH": "XANH", "VANG": "VÀNG", "DO": "ĐỎ"}.get(r["status"], r["status"]),
                f"{r['edit_frame_start']}–{r['edit_frame_end']}", src, r.get("clean_check", ""),
                r.get("manual_action", "")))
        self.info.configure(text=f"{n_bad} cảnh cần duyệt / {len(self.doc['segments'])}")

    def _row(self):
        sel = self.tree.selection()
        if not sel or not self.doc:
            return None
        return next(r for r in self.doc["segments"] if str(r["segment_id"]) == sel[0])

    def show(self):
        r = self._row()
        if r is None:
            return
        mid_t = (float(r["edit_time_start"]) + float(r["edit_time_end"])) / 2
        edit_path = (self.doc.get("edit") or {}).get("path")
        e = grab_frames(edit_path, [mid_t], 640, gray=False)[0] if edit_path and Path(edit_path).exists() else None
        s_img = None
        if r.get("source_id") not in (None, "", "UNMATCHED"):
            src = (self.doc.get("sources") or {}).get(r["source_id"], {})
            frames = [f for f in self.doc.get("frames", []) if r["edit_frame_start"] <= f["edit_frame"]
                      <= r["edit_frame_end"] and f.get("source_time") not in (None, "")]
            if frames and src.get("path") and Path(src["path"]).exists():
                f = frames[len(frames) // 2]
                s_img = grab_frames(src["path"], [float(f["source_time"])], 640, gray=False)[0]
        self._ph = [_photo(e), _photo(s_img)]
        self.lbl_e.configure(image=self._ph[0] or "", text="" if self._ph[0] else "(không có ảnh Douyin)")
        self.lbl_s.configure(image=self._ph[1] or "", text="" if self._ph[1] else "(chưa có nguồn)")
        self.reason.delete("1.0", "end")
        self.reason.insert("1.0", (r.get("reasons") or "") + "\nỨng viên: " + str(r.get("candidates") or ""))
        self.sid.set(r.get("source_id") if r.get("source_id") != "UNMATCHED" else "")
        self.sframe.delete(0, "end")
        self.sframe.insert(0, str(r.get("source_frame_start") or ""))
        self.speed.delete(0, "end")
        self.speed.insert(0, str(r.get("speed") or "1.0"))

    def _set(self, action):
        r = self._row()
        if r is None:
            return messagebox.showinfo("Duyệt", "Chọn một cảnh trước.")
        r["manual_action"] = action
        self.refresh()

    def accept(self):
        r = self._row()
        if r is not None and r.get("source_id") in (None, "", "UNMATCHED"):
            return messagebox.showwarning("Duyệt", "Cảnh chưa có nguồn: hãy sửa nguồn/khung hoặc duyệt khung Douyin.")
        self._set(MANUAL_OK)

    def approve_fb(self):
        r = self._row()
        if r is None:
            return
        if not messagebox.askyesno("Duyệt khung Douyin", "Bạn đã xem kỹ và xác nhận các khung Douyin của cảnh này "
                                   "KHÔNG có chữ, phụ đề, logo, watermark?\nKhung sẽ được ghi là "
                                   "DOUYIN_CLEAN_FALLBACK (không phải khung anime gốc), âm thanh để trống."):
            return
        r["source_id"] = "UNMATCHED"
        for f in self.doc.get("frames", []):
            if r["edit_frame_start"] <= f["edit_frame"] <= r["edit_frame_end"]:
                f["source_id"], f["source_frame"] = "", ""
        self._set(MANUAL_FALLBACK)

    def edit_src(self):
        r = self._row()
        if r is None:
            return
        try:
            sid = self.sid.get().strip()
            f0 = int(float(self.sframe.get()))
            sp = float(self.speed.get())
        except ValueError:
            return messagebox.showerror("Sửa nguồn", "Khung nguồn / tốc độ không hợp lệ.")
        if sid not in (self.doc.get("sources") or {}):
            return messagebox.showerror("Sửa nguồn", f"Không có nguồn '{sid}' trong bảng.")
        r.update({"source_id": sid, "source_frame_start": f0, "source_frame_exact": f0, "speed": sp,
                  "source_time_start": ""})
        for f in self.doc.get("frames", []):  # bỏ ánh xạ từng khung cũ -> Chế độ B dùng mô hình mới
            if r["edit_frame_start"] <= f["edit_frame"] <= r["edit_frame_end"]:
                f["source_id"], f["source_frame"], f["speed"] = sid, "", sp
        self._set(MANUAL_OK)

    def clear(self):
        self._set("")

    def save(self) -> Path | None:
        if not self.doc or not self.path:
            return None
        self.path.write_text(json.dumps(self.doc, ensure_ascii=False, indent=1), "utf-8")
        write_csv(self.path.with_suffix(".csv"), self.doc["segments"], SEG_FIELDS)
        return self.path

    def save_and_cut(self):
        p = self.save()
        if p and self.on_cut:
            self.on_cut(str(p))
