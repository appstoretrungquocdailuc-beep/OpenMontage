"""Tạo video nguồn & bản edit tổng hợp với ĐÁP ÁN đúng từng khung.

Nguồn giả lập phim hoạt hình: nền phẳng, nhân vật viền đen, vẽ "on 2s" (giữ 2 khung),
có cảnh nội bộ, máy quay lia; âm thanh riêng từng nguồn (để kiểm tra lệch tiếng).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

AUDIO_RATE = 48000


@dataclass
class SourceSpec:
    sid: str
    seed: int
    seconds: float
    fps_num: int = 24000
    fps_den: int = 1001
    width: int = 640
    height: int = 360
    textured: bool = False  # nền vẽ có họa tiết (nhiều điểm đặc trưng như nền anime)

    @property
    def fps(self) -> float:
        return self.fps_num / self.fps_den

    @property
    def n(self) -> int:
        return int(round(self.seconds * self.fps))


class CartoonSource:
    def __init__(self, spec: SourceSpec, copy_from: tuple | None = None):
        """copy_from=(CartoonSource khác, frame_đầu_nguồn_khác, frame_đầu_ở_đây, số_khung) để tạo cảnh trùng."""
        self.spec = spec
        rng = np.random.default_rng(spec.seed)
        self.shots = []
        f = 0
        while f < spec.n:
            length = int(rng.integers(int(2 * spec.fps), int(5 * spec.fps)))
            self.shots.append({
                "start": f, "end": min(spec.n, f + length),
                "bg": rng.integers(40, 230, 3).tolist(), "bg2": rng.integers(40, 230, 3).tolist(),
                "hold": int(rng.choice([1, 2, 2, 3])),
                "pan": float(rng.uniform(-1.5, 1.5)),
                "scenery": [(rng.integers(0, 1400), rng.integers(150, 330), rng.integers(30, 120),
                             rng.integers(30, 140), rng.integers(30, 230, 3).tolist()) for _ in range(9)],
                "chars": [{
                    "x": rng.uniform(50, 590), "y": rng.uniform(60, 300),
                    "vx": rng.uniform(-3, 3), "vy": rng.uniform(-1.5, 1.5),
                    "r": rng.uniform(18, 50), "kind": int(rng.integers(0, 3)),
                    "col": rng.integers(20, 255, 3).tolist(), "ph": rng.uniform(0, 6),
                } for _ in range(int(rng.integers(3, 6)))],
            })
            f += length
        self.copy_from = copy_from

    def shot_of(self, i):
        for k, s in enumerate(self.shots):
            if s["start"] <= i < s["end"]:
                return k, s
        return len(self.shots) - 1, self.shots[-1]

    def drawing_id(self, i: int) -> tuple:
        """Hai khung có cùng drawing_id trông giống hệt nhau (giữ khung kiểu anime)."""
        if self.copy_from:
            other, of, here, cnt = self.copy_from
            if here <= i < here + cnt:
                return other.drawing_id(of + i - here)
        k, s = self.shot_of(i)
        return (self.spec.sid, k, (i - s["start"]) // s["hold"])

    def frame(self, i: int) -> np.ndarray:
        if self.copy_from:
            other, of, here, cnt = self.copy_from
            if here <= i < here + cnt:
                return cv2.resize(other.frame(of + i - here), (self.spec.width, self.spec.height),
                                  interpolation=cv2.INTER_AREA)
        W, H = 640, 360
        k, s = self.shot_of(i)
        t = (i - s["start"]) // s["hold"] * s["hold"]
        img = np.zeros((H, W, 3), np.uint8)
        img[:] = s["bg"]
        img[int(H * 0.62):] = s["bg2"]
        off = int(s["pan"] * t)
        if self.spec.textured:
            if "tex" not in s:
                r = np.random.default_rng(self.spec.seed * 1000 + k)
                tex = np.zeros((H, 1400, 3), np.uint8)
                tex[:] = s["bg"]
                for _ in range(900):  # nét cọ / chi tiết nền ngẫu nhiên, cố định theo cảnh
                    c = tuple(int(v) for v in r.integers(20, 235, 3))
                    p0 = (int(r.integers(0, 1400)), int(r.integers(0, H)))
                    if r.random() < 0.5:
                        cv2.circle(tex, p0, int(r.integers(2, 12)), c, int(r.choice([-1, 1, 2])))
                    else:
                        p1 = (p0[0] + int(r.integers(-60, 60)), p0[1] + int(r.integers(-40, 40)))
                        cv2.line(tex, p0, p1, c, int(r.integers(1, 4)))
                s["tex"] = tex
            sh = int(off % 1400)
            img[:] = np.roll(s["tex"], -sh, axis=1)[:, :W]
        for (x, y, w, h, col) in s["scenery"]:
            xx = int((x - off) % 1400) - 300
            cv2.rectangle(img, (xx, int(y) - int(h)), (xx + int(w), int(y)), col, -1)
            cv2.rectangle(img, (xx, int(y) - int(h)), (xx + int(w), int(y)), (20, 20, 20), 2)
            cv2.line(img, (xx, int(y) - int(h)), (xx + int(w) // 2, int(y) - int(h) - 25), (30, 30, 30), 2)
        for c in s["chars"]:
            x = (c["x"] + c["vx"] * t) % (W + 80) - 40
            y = c["y"] + c["vy"] * t + 6 * np.sin(c["ph"] + t * 0.3)
            r = c["r"] * (1 + 0.08 * np.sin(c["ph"] + t * 0.21))
            ctr = (int(x), int(y))
            if c["kind"] == 0:
                cv2.ellipse(img, ctr, (int(r), int(r * 0.8)), 0, 0, 360, c["col"], -1)
                cv2.ellipse(img, ctr, (int(r), int(r * 0.8)), 0, 0, 360, (10, 10, 10), 3)
            elif c["kind"] == 1:
                cv2.rectangle(img, (int(x - r), int(y - r)), (int(x + r), int(y + r * 0.6)), c["col"], -1)
                cv2.rectangle(img, (int(x - r), int(y - r)), (int(x + r), int(y + r * 0.6)), (10, 10, 10), 3)
            else:
                pts = np.array([[x, y - r], [x + r, y + r * 0.7], [x - r * 0.9, y + r * 0.5]], np.int32)
                cv2.fillPoly(img, [pts], c["col"])
                cv2.polylines(img, [pts], True, (10, 10, 10), 3)
            # mắt + miệng (bất đối xứng -> phân biệt lật gương)
            cv2.circle(img, (int(x - r * 0.3), int(y - r * 0.2)), max(2, int(r * 0.12)), (0, 0, 0), -1)
            cv2.circle(img, (int(x + r * 0.15), int(y - r * 0.25)), max(2, int(r * 0.09)), (250, 250, 250), -1)
            mouth = int(r * 0.25 * (1 + np.sin(t * 0.9 + c["ph"])))
            cv2.ellipse(img, (int(x + r * 0.2), int(y + r * 0.3)), (max(2, int(r * 0.3)), max(1, mouth)),
                        15, 0, 180, (30, 0, 60), -1)
        if (self.spec.width, self.spec.height) != (W, H):
            img = cv2.resize(img, (self.spec.width, self.spec.height), interpolation=cv2.INTER_CUBIC)
        return img

    def audio(self) -> np.ndarray:
        """Âm thanh stereo đặc trưng theo thời gian (tiếng 'thoại' giả lập)."""
        rng = np.random.default_rng(self.spec.seed + 999)
        n = int(self.spec.n / self.spec.fps * AUDIO_RATE)
        t = np.arange(n) / AUDIO_RATE
        sig = np.zeros(n, np.float32)
        pos = 0
        while pos < n:
            ln = int(rng.integers(AUDIO_RATE // 10, AUDIO_RATE // 2))
            f0 = rng.uniform(150, 1200)
            seg = np.sin(2 * np.pi * (f0 + rng.uniform(-200, 200) * t[:ln]) * t[:ln])
            seg *= np.hanning(min(ln, n - pos))[:len(seg)] if ln <= n - pos else 1
            sig[pos:pos + ln] += 0.3 * seg[: n - pos].astype(np.float32)
            pos += ln + int(rng.integers(0, AUDIO_RATE // 8))
        noise = rng.normal(0, 0.05, n).astype(np.float32)
        sig += np.convolve(noise, np.ones(8) / 8, "same").astype(np.float32)
        return np.stack([sig, np.roll(sig, 13) * 0.9], axis=1)

    def render(self, path: Path, copy_audio_from=None):
        sp = self.spec
        a = self.audio()
        if self.copy_from and copy_audio_from is not None:
            pass
        apath = path.with_suffix(".f32")
        a.astype(np.float32).tofile(apath)
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
               "-s", f"{sp.width}x{sp.height}", "-r", f"{sp.fps_num}/{sp.fps_den}", "-i", "pipe:0",
               "-f", "f32le", "-ar", str(AUDIO_RATE), "-ac", "2", "-i", str(apath),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-g", "48", "-bf", "2",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
               "-metadata:s:a:0", "language=jpn", "-shortest", str(path)]
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        for i in range(sp.n):
            p.stdin.write(self.frame(i).tobytes())
        p.stdin.close()
        p.wait()
        apath.unlink()
        assert p.returncode == 0


# ------------------------------------------------------------------ bản edit

def _hue_shift(img, deg, sat=1.0, bright=1.0, contrast=1.0):
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV).astype(np.float32)
    hsv[..., 0] = (hsv[..., 0] + deg / 2) % 180
    hsv[..., 1] = np.clip(hsv[..., 1] * sat, 0, 255)
    out = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR).astype(np.float32)
    out = (out - 128) * contrast + 128 * bright
    return np.clip(out, 0, 255).astype(np.uint8)


def _subtitle(img, text):
    h, w = img.shape[:2]
    scale = w / 640 * 1.1
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_DUPLEX, scale, 2)
    org = ((w - tw) // 2, int(h * 0.88))
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_DUPLEX, scale, (0, 0, 0), int(6 * scale) + 2, cv2.LINE_AA)
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_DUPLEX, scale, (255, 255, 255), 2, cv2.LINE_AA)


def _watermark(img):
    h, w = img.shape[:2]
    over = img.copy()
    cv2.putText(over, "@douyin_shin", (int(w * 0.04), int(h * 0.08)), cv2.FONT_HERSHEY_SIMPLEX,
                w / 900, (255, 255, 255), 2, cv2.LINE_AA)
    cv2.addWeighted(over, 0.6, img, 0.4, 0, img)


@dataclass
class EditSeg:
    sid: str | None  # None = cảnh không thuộc nguồn nào
    src_start: int = 0
    seconds: float = 2.0
    crop: tuple | None = None  # (x, y, w, h) chuẩn hóa trên nguồn
    flip: bool = False
    hue: float = 0
    sat: float = 1.0
    bright: float = 1.0
    contrast: float = 1.0
    subtitle: str | None = None
    speed: float = 1.0
    foreign_seed: int = 0


def build_edit(path: Path, sources: dict[str, CartoonSource], segs: list[EditSeg], fps: float = 30.0,
               size=(960, 540), layout: str = "fill", watermark: bool = False) -> dict:
    """layout: fill (lấp đầy khung) | letterbox (khung dọc, nền mờ, nội dung 16:9 ở giữa)."""
    W, H = size
    gt = []
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
           "-s", f"{W}x{H}", "-r", str(fps), "-i", "pipe:0",
           "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest", str(path)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    foreign_cache = {}
    k_global = 0
    for si, sg in enumerate(segs):
        n = int(round(sg.seconds * fps))
        for k in range(n):
            t = k / fps
            if sg.sid is None:
                if sg.foreign_seed not in foreign_cache:
                    foreign_cache[sg.foreign_seed] = CartoonSource(SourceSpec("X", sg.foreign_seed, 30, 24, 1))
                src_img = foreign_cache[sg.foreign_seed].frame(int(t * 24) + 50)
                gt_entry = {"edit_frame": k_global, "source_id": None, "source_frame": None, "seg": si}
            else:
                cs = sources[sg.sid]
                f = sg.src_start + int(np.floor(t * sg.speed * cs.spec.fps + 1e-6))
                src_img = cs.frame(f)
                gt_entry = {"edit_frame": k_global, "source_id": sg.sid, "source_frame": f,
                            "drawing": list(map(str, cs.drawing_id(f))), "seg": si,
                            "flip": sg.flip, "crop": sg.crop, "speed": sg.speed}
            img = src_img
            if sg.crop:
                h, w = img.shape[:2]
                x, y, cw, ch = sg.crop
                img = img[int(y * h):int((y + ch) * h), int(x * w):int((x + cw) * w)]
            if sg.flip:
                img = img[:, ::-1]
            if sg.hue or sg.sat != 1 or sg.bright != 1 or sg.contrast != 1:
                img = _hue_shift(np.ascontiguousarray(img), sg.hue, sg.sat, sg.bright, sg.contrast)
            if layout == "fill":
                frame = cv2.resize(np.ascontiguousarray(img), (W, H), interpolation=cv2.INTER_CUBIC)
            else:
                bg = cv2.resize(np.ascontiguousarray(img), (W, H), interpolation=cv2.INTER_AREA)
                bg = cv2.GaussianBlur(bg, (0, 0), 25)
                bg = (bg * 0.6).astype(np.uint8)
                ch_ = int(W * img.shape[0] / img.shape[1])
                fg = cv2.resize(np.ascontiguousarray(img), (W, ch_), interpolation=cv2.INTER_CUBIC)
                y0 = (H - ch_) // 2
                bg[y0:y0 + ch_] = fg
                cv2.putText(bg, "SHIN-CHAN TAP 1", (int(W * 0.1), int(H * 0.15)), cv2.FONT_HERSHEY_DUPLEX,
                            W / 500, (255, 255, 255), 3, cv2.LINE_AA)
                frame = bg
            if sg.subtitle:
                _subtitle(frame, sg.subtitle)
            if watermark:
                _watermark(frame)
            p.stdin.write(np.ascontiguousarray(frame).tobytes())
            gt.append(gt_entry)
            k_global += 1
    p.stdin.close()
    p.wait()
    assert p.returncode == 0
    truth = {"fps": fps, "frames": gt, "segments": [sg.__dict__ for sg in segs]}
    path.with_suffix(".truth.json").write_text(json.dumps(truth, indent=1))
    return truth
