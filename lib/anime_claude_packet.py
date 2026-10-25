"""Claude Pro workflow (no API): evidence packets out, Claude's answers back in.

The person uploads a packet to claude.ai (Pro) or opens it in Claude Code; no
API key, no API cost. This module never calls a model. It only:

1. **export** — from an existing Phase 2 run (``anime_source_index.json`` +
   the source video; Whisper is NOT re-run and Phase 1–2 files are never
   written) build ``<run>/claude_pro/<packet>/``:

   - ``01_khung_hinh.pdf`` — contact sheet: frames in time order, each with its
     exact source time, frame number (``f`` + ``round(t × fps)``), scene id,
     why it was picked, and the line audible at that moment; the dialogue of
     each page is printed under its frames;
   - ``02_loi_thoai_va_canh.md`` — Japanese transcript (line ids, times,
     ASR confidence, flags), scene list, frame index, machine hints;
   - ``03_PROMPT_*.md`` — the instructions to paste into Claude;
   - ``packet.json`` — the same data for the importers; ``frames/*.jpg``.

   ``overview`` covers the whole episode sparsely so Claude can pick one
   complete 60–90 s situation; ``detail`` covers one range densely: a regular
   grid plus every scene cut, every line, and extra frames (4–5 fps) where the
   machine sees fast motion, loud/exclaimed lines, misunderstanding cues
   (え？, 違う…) or a phrase repeated later (possible setup → payoff). These
   hints only decide where to look closer — they are never conclusions;

2. **more** — extra frames for time ranges Claude asked to see (evidence
   request) as ``bo_sung_NN.pdf``; their frame ids join the packet;

3. **understanding** — validate Claude's ``hieu_phim.json`` (characters,
   beats with frame ids / line ids, cause → action → reaction → consequence,
   misunderstandings, setup → payoff gags, observation / inference /
   hypothesis) against what the packet really showed, and render
   ``hieu_phim.md`` for the person to check against the video;

4. **approve** — the person's approval, bound to the file's SHA-256;

5. **script** — validate Claude's ``kich_ban.json`` (Japanese review script)
   against the approved understanding and with ``lib.anime_recap`` (frame math,
   gapless timeline, evidence, 60–90 s, story roles), then write
   ``kich_ban.md`` and ``cut_list.csv``. Nothing is rendered.

CLI (inside .venv-anime)::

    python -m lib.anime_claude_packet export --mode detail --start 2:10 --end 3:30 [--run <run>]
    python -m lib.anime_claude_packet more --ranges "2:41-2:44, 3:02.5-3:04" [--packet <dir>]
    python -m lib.anime_claude_packet understanding [--packet <dir>] [--paste | --file f.json]
    python -m lib.anime_claude_packet approve [--packet <dir>]
    python -m lib.anime_claude_packet script [--packet <dir>] [--paste | --file f.json]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from lib.anime_recap import (
    STORY_ROLES,
    estimate_ja_speech_seconds,
    seconds_to_frame,
    validate_recap_plan,
)

PACKET_DIR = "claude_pro"
PACKET_FILE = "packet.json"
UNDERSTANDING_FILE = "hieu_phim.json"
APPROVAL_FILE = "hieu_phim_duyet.json"
SCRIPT_FILE = "kich_ban.json"
CUT_LIST = "cut_list.csv"
KINDS = ("observation", "inference", "hypothesis")
KIND_VI = {"observation": "QUAN SÁT", "inference": "SUY LUẬN", "hypothesis": "GIẢ THUYẾT"}

REASONS = {
    "grid": "lưới đều", "cut": "cắt cảnh", "keyframe": "keyframe", "line": "thoại", "short_scene": "cảnh ngắn",
    "motion": "chuyển động nhanh", "peak": "to / cảm thán", "misunderstanding": "nghi hiểu lầm",
    "callback": "nghi setup/payoff", "request": "bổ sung",
}
HOT_REASONS = ("motion", "peak", "misunderstanding", "callback", "request")
# Spoken cues of surprise, denial or confusion — where a misunderstanding may be shown or revealed.
MISUNDERSTANDING_CUES = ("え？", "えっ", "ええっ", "は？", "はぁ", "なに", "何", "違う", "ちがう", "まさか", "ウソ", "うそ",
                         "本当", "ほんと", "なんで", "どうして", "勘違い", "つもり", "じゃなくて", "じゃない", "って？")
EXCLAIM = re.compile(r"[！!]")
QUESTION = re.compile(r"[？?]")
_KANJI_KATA = re.compile(r"[\u4e00-\u9fff\u30a0-\u30ffー]")

DETAIL = {"base_interval": 0.5, "hot_fps": 5.0, "max_frames": 320, "cols": 3, "rows": 3, "width": 640}
OVERVIEW = {"base_interval": 2.0, "max_frames": 320, "cols": 4, "rows": 4, "width": 480}
MORE = {"fps": 8.0, "max_frames": 120, "cols": 3, "rows": 3, "width": 640}
DETAIL_MIN_SECONDS, DETAIL_MAX_SECONDS = 20.0, 150.0


class PacketError(RuntimeError):
    pass


# ── time helpers ─────────────────────────────────────────────────────────


def mmss(seconds: float) -> str:
    t = round(max(0.0, float(seconds)), 2)
    minutes = int(t // 60)
    return f"{minutes:02d}:{t - 60 * minutes:05.2f}"


def timecode(frame: int, fps: float) -> str:
    """HH:MM:SS:FF on the nominal integer rate (24 for 23.976), as editors show it."""
    nominal = round(fps)
    ff = frame % nominal
    total = frame // nominal
    return f"{total // 3600:02d}:{total // 60 % 60:02d}:{total % 60:02d}:{ff:02d}"


def parse_time(text: str | float) -> float:
    """``83.5``, ``1:23.5``, ``01:23`` or ``0:01:23.5`` → seconds."""
    if isinstance(text, (int, float)):
        return float(text)
    parts = str(text).strip().replace(",", ".").split(":")
    try:
        values = [float(p) for p in parts]
    except ValueError as exc:
        raise PacketError(f"không đọc được thời gian '{text}' (ví dụ đúng: 2:10 hoặc 130.5)") from exc
    if not 1 <= len(values) <= 3 or any(v < 0 for v in values):
        raise PacketError(f"không đọc được thời gian '{text}'")
    seconds = 0.0
    for v in values:
        seconds = seconds * 60 + v
    return seconds


def parse_ranges(text: str) -> list[tuple[float, float]]:
    """``"2:41-2:44, 3:02.5-3:04"`` → [(161.0, 164.0), (182.5, 184.0)]."""
    ranges = []
    for chunk in re.split(r"[;,\s]+(?=\d)", re.sub(r"\s*-\s*", "-", text.strip())):
        chunk = chunk.strip().strip(",;")
        if not chunk:
            continue
        if "-" not in chunk:
            raise PacketError(f"khoảng '{chunk}' cần dạng bắt_đầu-kết_thúc, ví dụ 2:41-2:44")
        a, b = chunk.split("-", 1)
        s, e = parse_time(a), parse_time(b)
        if e <= s:
            raise PacketError(f"khoảng '{chunk}': kết thúc phải sau bắt đầu")
        ranges.append((s, e))
    if not ranges:
        raise PacketError("chưa nhập khoảng thời gian nào")
    return ranges


# ── run / index ──────────────────────────────────────────────────────────


def load_index(run_dir: Path) -> dict:
    path = Path(run_dir) / "anime_source_index.json"
    if not path.is_file():
        raise PacketError(f"không thấy {path} — cần thư mục kết quả Giai đoạn 2")
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_video(index: dict, run_dir: Path, override: Path | None = None) -> Path:
    candidates = [override] if override else []
    candidates += [Path(index.get("video", {}).get("path", "")), Path(run_dir) / "work" / "input.mp4",
                   Path(run_dir) / "work" / "clip.mp4"]
    for c in candidates:
        if c and str(c) not in ("", ".") and Path(c).is_file():
            return Path(c)
    raise PacketError("không tìm thấy video gốc của lần chạy này — chỉ định bằng --video <file.mp4>")


def all_lines(index: dict) -> list[dict]:
    return [{**line, "scene_id": scene["scene_id"]} for scene in index["scenes"] for line in scene.get("dialogue", [])]


def scene_at(index: dict, t: float) -> str:
    for scene in index["scenes"]:
        if scene["start_seconds"] <= t < scene["end_seconds"]:
            return scene["scene_id"]
    return index["scenes"][-1]["scene_id"] if t >= index["scenes"][-1]["start_seconds"] else index["scenes"][0]["scene_id"]


def _overlaps(a0: float, a1: float, b0: float, b1: float) -> bool:
    return a0 < b1 and b0 < a1


# ── machine hints (where to look closer; never conclusions) ──────────────


def motion_scores(video: Path, fps: float, start: float, end: float) -> dict[int, float]:
    """Per-frame ffmpeg scene score (0–1) on a small proxy; frame numbers are approximate (±1)."""
    ss = max(0.0, start)
    cmd = ["ffmpeg", "-hide_banner", "-v", "info", "-ss", f"{ss:.3f}", "-t", f"{end - ss:.3f}", "-i", str(video), "-an",
           "-vf", "scale=192:-2,select='gte(scene\\,0)',metadata=print:key=lavfi.scene_score", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    scores: dict[int, float] = {}
    first = round(ss * fps)
    current = None
    for line in proc.stderr.splitlines():
        m = re.search(r"frame:(\d+)\s", line)
        if m:
            current = first + int(m.group(1))
            continue
        m = re.search(r"lavfi\.scene_score=([\d.]+)", line)
        if m and current is not None:
            scores[current] = float(m.group(1))
    return scores


def _repeated_phrases(lines: list[dict], *, min_gap: float = 8.0, max_phrases: int = 5) -> list[tuple[str, list[dict]]]:
    """Phrases (≥2 chars with kanji/katakana) said again ≥ min_gap later — possible setup → payoff."""
    found: dict[str, list[dict]] = {}
    for line in lines:
        text = re.sub(r"[\s、。！？!?「」『』…〜ー]+$", "", line.get("text_ja", ""))
        grams = {text[i:i + n] for n in range(2, 7) for i in range(len(text) - n + 1)}
        for g in grams:
            if _KANJI_KATA.search(g) and not re.search(r"[、。！？!?\s]", g):
                found.setdefault(g, []).append(line)
    phrases = []
    for g, ls in found.items():
        ls = sorted({id(x): x for x in ls}.values(), key=lambda x: x["start_seconds"])
        if len(ls) >= 2 and ls[-1]["start_seconds"] - ls[0]["start_seconds"] >= min_gap:
            phrases.append((g, ls))
    phrases.sort(key=lambda p: (-len(p[0]), p[1][0]["start_seconds"]))
    kept: list[tuple[str, list[dict]]] = []
    for g, ls in phrases:
        if not any(g in k for k, _ in kept):
            kept.append((g, ls))
        if len(kept) >= max_phrases:
            break
    return kept


def find_hotspots(index: dict, start: float, end: float, motion: dict[int, float] | None = None, fps: float | None = None) -> list[dict]:
    lines = [ln for ln in all_lines(index) if _overlaps(ln["start_seconds"], ln["end_seconds"], start, end)]
    spots: list[dict] = []
    for ln in lines:
        text = ln.get("text_ja", "")
        s, e = max(start, ln["start_seconds"] - 0.5), min(end, ln["end_seconds"] + 1.0)
        if (ln.get("excite") or 0) >= 1.5 or EXCLAIM.search(text):
            spots.append({"reason": "peak", "start": s, "end": e, "line_id": ln["line_id"], "note": text})
        if any(cue in text for cue in MISUNDERSTANDING_CUES) or QUESTION.search(text):
            spots.append({"reason": "misunderstanding", "start": s, "end": e, "line_id": ln["line_id"], "note": text})
    for phrase, ls in _repeated_phrases(lines):
        for ln in ls:
            spots.append({"reason": "callback", "start": max(start, ln["start_seconds"] - 0.5),
                          "end": min(end, ln["end_seconds"] + 1.0), "line_id": ln["line_id"], "note": f"「{phrase}」 lặp lại"})
    if motion and fps:
        cuts = {round(sc["start_seconds"] * fps) for sc in index["scenes"]}
        bins: dict[int, float] = {}
        for frame, score in motion.items():
            if any(abs(frame - c) <= 1 for c in cuts):
                continue  # a cut is not motion
            b = int((frame / fps - start) // 0.5)
            bins[b] = max(bins.get(b, 0.0), score)
        if bins:
            values = sorted(bins.values())
            threshold = max(0.003, values[int(0.75 * (len(values) - 1))])  # relative: top quarter of this range
            hot = sorted(b for b, v in bins.items() if v >= threshold and v > 0)
            groups: list[list[int]] = []
            for b in hot:
                if groups and b - groups[-1][-1] <= 1:
                    groups[-1].append(b)
                else:
                    groups.append([b])
            groups.sort(key=lambda g: -max(bins[b] for b in g))
            for g in groups[:8]:
                spots.append({"reason": "motion", "start": max(start, start + g[0] * 0.5), "end": min(end, start + (g[-1] + 1) * 0.5),
                              "line_id": None, "note": f"điểm chuyển động {max(bins[b] for b in g):.2f}"})
    return sorted(spots, key=lambda s: (s["start"], s["reason"]))


# ── frame plan ───────────────────────────────────────────────────────────


def plan_frames(index: dict, start: float, end: float, *, base_interval: float, hotspots: list[dict] | None = None,
                hot_fps: float = 5.0, max_frames: int = 320, with_lines: bool = True) -> list[dict]:
    """Frames to show for [start, end): grid + cuts + lines + dense hotspots, deduplicated by frame number."""
    fps = float(index["video"]["fps"])
    last = max(0, round(index["video"]["duration_seconds"] * fps) - 1)
    first_f, end_f = max(0, round(start * fps)), min(last, round(end * fps) - 1)
    picks: dict[int, set[str]] = {}

    def add(t: float, reason: str, *, frame: int | None = None) -> None:
        f = min(max(round(t * fps) if frame is None else frame, first_f), end_f)
        picks.setdefault(f, set()).add(reason)

    for sc in index["scenes"]:
        s0, s1 = sc["start_seconds"], sc["end_seconds"]
        if not _overlaps(s0, s1, start, end):
            continue
        add(0, "cut", frame=round(max(s0, start) * fps))  # first frame of the scene (or of the range)
        add(0, "cut", frame=round(min(s1, end) * fps) - 1)  # its last frame
        if s1 - s0 < 1.0:
            add((s0 + s1) / 2, "short_scene")
        for kf in sc.get("keyframes", []):
            if start <= kf["timestamp_seconds"] < end:
                add(kf["timestamp_seconds"], "keyframe")
    if with_lines:
        for ln in all_lines(index):
            if _overlaps(ln["start_seconds"], ln["end_seconds"], start, end):
                add(max(start, ln["start_seconds"] + 0.1), "line")
                add(min(end, (ln["start_seconds"] + ln["end_seconds"]) / 2), "line")
    hot_frames: dict[int, set[str]] = {}
    for spot in hotspots or []:
        t = spot["start"]
        while t < spot["end"]:
            f = min(max(round(t * fps), first_f), end_f)
            hot_frames.setdefault(f, set()).add(spot["reason"])
            t += 1.0 / hot_fps
    for f, reasons in hot_frames.items():
        picks.setdefault(f, set()).update(reasons)

    must = len(picks)
    interval = base_interval
    while True:
        grid = {}
        t = start + interval / 2
        while t < end:
            f = min(max(round(t * fps), first_f), end_f)
            if f not in picks:
                grid[f] = {"grid"}
            t += interval
        if must + len(grid) <= max_frames or interval >= 4 * base_interval:
            break
        interval *= 1.5
    chosen = {**grid, **picks}
    if len(chosen) > max_frames:  # thin hotspot frames, keep cuts/lines
        hot_only = sorted(f for f, r in chosen.items() if r <= set(HOT_REASONS) | {"grid"})
        drop = len(chosen) - max_frames
        for f in hot_only[1::2][:drop]:
            chosen.pop(f)
    return [{"frame": f, "t": round(f / fps, 3), "scene_id": scene_at(index, f / fps), "reasons": sorted(r)}
            for f, r in sorted(chosen.items())]


def extract_frames(video: Path, frames: list[dict], out_dir: Path, fps: float, *, width: int) -> None:
    """Exact frames by number: seek just before each cluster, select frame offsets (no per-frame ffmpeg spawn)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    todo = [f for f in frames if not (out_dir / f"f{f['frame']:06d}.jpg").is_file()]
    clusters: list[list[dict]] = []
    for f in todo:
        if clusters and f["frame"] - clusters[-1][0]["frame"] < round(20 * fps) and len(clusters[-1]) < 120:
            clusters[-1].append(f)
        else:
            clusters.append([f])
    tmp = out_dir / "_tmp"
    for cluster in clusters:
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        n0 = cluster[0]["frame"]
        expr = "+".join(f"eq(n\\,{f['frame'] - n0})" for f in cluster)
        cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-ss", f"{max(0.0, (n0 - 0.25) / fps):.6f}", "-i", str(video),
               "-an", "-vf", f"select='{expr}',scale={width}:-2", "-fps_mode", "passthrough", "-q:v", "3",
               str(tmp / "o_%05d.jpg")]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        outputs = sorted(tmp.glob("o_*.jpg"))
        if proc.returncode != 0 or len(outputs) != len(cluster):
            raise PacketError(f"ffmpeg trích {len(outputs)}/{len(cluster)} khung quanh {mmss(n0 / fps)}: {proc.stderr[-300:]}")
        for f, path in zip(cluster, outputs):
            path.replace(out_dir / f"f{f['frame']:06d}.jpg")
    shutil.rmtree(tmp, ignore_errors=True)
    for f in frames:
        f["file"] = f"frames/f{f['frame']:06d}.jpg"


# ── contact sheet PDF (Pillow pages → minimal PDF with JPEG pages) ───────

FONT_CANDIDATES = {
    "ja": ["C:/Windows/Fonts/YuGothM.ttc", "C:/Windows/Fonts/meiryo.ttc", "C:/Windows/Fonts/BIZ-UDGothicR.ttc",
           "C:/Windows/Fonts/msgothic.ttc", "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
           "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/usr/share/fonts/opentype/ipaexfont-gothic/ipaexg.ttf",
           "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"],
    "vi": ["C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf",
           "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
}


def find_font(kind: str) -> str | None:
    return next((p for p in FONT_CANDIDATES[kind] if Path(p).is_file()), None)


class _Fonts:
    def __init__(self):
        from PIL import ImageFont

        self.ja_path, self.vi_path = find_font("ja"), find_font("vi")
        self._cache: dict = {}
        self._mod = ImageFont

    def get(self, kind: str, size: int):
        key = (kind, size)
        if key not in self._cache:
            path = self.ja_path if kind == "ja" else self.vi_path
            path = path or self.ja_path or self.vi_path
            self._cache[key] = self._mod.truetype(path, size) if path else self._mod.load_default()
        return self._cache[key]

    @staticmethod
    def runs(text: str) -> list[tuple[str, str]]:
        """Split into script runs: Japanese glyphs from the JA font, everything else (Vietnamese) from the Latin font."""
        out: list[tuple[str, str]] = []
        for ch in text:
            kind = "ja" if _CJK.match(ch) else "vi"
            if out and out[-1][0] == kind:
                out[-1] = (kind, out[-1][1] + ch)
            else:
                out.append((kind, ch))
        return out

    def length(self, draw, text: str, size: int) -> float:
        return sum(draw.textlength(run, font=self.get(kind, size)) for kind, run in self.runs(text))

    def draw(self, draw, xy: tuple[float, float], text: str, size: int, fill) -> None:
        x, y = xy
        for kind, run in self.runs(text):
            draw.text((x, y), run, font=self.get(kind, size), fill=fill)
            x += draw.textlength(run, font=self.get(kind, size))


_CJK = re.compile(r"[\u3000-\u30ff\u3400-\u9fff\uff00-\uffef「」]")


def _fit(draw, fonts: _Fonts, text: str, size: int, width: int) -> str:
    if fonts.length(draw, text, size) <= width:
        return text
    while text and fonts.length(draw, text + "…", size) > width:
        text = text[:-1]
    return text + "…"


def _wrap(draw, fonts: _Fonts, text: str, size: int, width: int) -> list[str]:
    lines, current = [], ""
    for ch in text:
        if fonts.length(draw, current + ch, size) > width and current:
            lines.append(current)
            current = ch.lstrip()
        else:
            current += ch
    return lines + ([current] if current else [])


def _line_label(ln: dict) -> str:
    flags = [f for f in ln.get("asr_flags") or []]
    if ln.get("low_confidence"):
        flags.append("low_confidence")
    conf = ln.get("asr_confidence")
    meta = f"{ln['line_id']} {mmss(ln['start_seconds'])}–{mmss(ln['end_seconds'])}" + (f" tin cậy {conf:.2f}" if conf is not None else "")
    return meta + (f" [{', '.join(sorted(set(flags)))}]" if flags else "")


def render_pages(packet_dir: Path, frames: list[dict], lines: list[dict], *, title: str, cols: int, rows: int,
                 intro: list[str]) -> list[bytes]:
    from PIL import Image, ImageDraw

    fonts = _Fonts()
    page_w, margin, gap = 1800, 36, 18
    tile_w = (page_w - 2 * margin - (cols - 1) * gap) // cols
    per_page = cols * rows
    chunks = [frames[i:i + per_page] for i in range(0, len(frames), per_page)]
    total = len(chunks) + 1
    pages: list[bytes] = []

    def encode(img) -> bytes:
        import io

        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=78, optimize=True)
        return buf.getvalue()

    # cover / legend page
    cover = Image.new("RGB", (page_w, 1300), "white")
    d = ImageDraw.Draw(cover)
    y = margin
    fonts.draw(d, (margin, y), f"{title} · trang 1/{total}", 40, "black")
    y += 70
    for text in intro:
        for part in _wrap(d, fonts, text, 30, page_w - 2 * margin) or [""]:
            fonts.draw(d, (margin, y), part, 30, "black")
            y += 42
        if y > 1240:
            break
    pages.append(encode(cover))

    for p, chunk in enumerate(chunks, start=2):
        t0, t1 = chunk[0]["t"], chunk[-1]["t"]
        page_lines = [ln for ln in lines if _overlaps(ln["start_seconds"], ln["end_seconds"], t0 - 0.5, t1 + 0.5)]
        tiles = []
        for f in chunk:
            img = Image.open(packet_dir / f["file"]).convert("RGB")
            img = img.resize((tile_w, round(img.height * tile_w / img.width)))
            tiles.append(img)
        tile_h = max(t.height for t in tiles)
        cap_h = 104
        scratch = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        dlg_rows = []
        for ln in page_lines:
            dlg_rows.append(("vi", _line_label(ln)))
            dlg_rows += [("ja", "  " + part) for part in _wrap(scratch, fonts, "「" + ln.get("text_ja", "") + "」", 28, page_w - 2 * margin - 30)]
        rows_used = math.ceil(len(chunk) / cols)
        height = margin + 60 + rows_used * (tile_h + cap_h + gap) + 50 + 38 * max(1, len(dlg_rows)) + margin
        page = Image.new("RGB", (page_w, height), "white")
        d = ImageDraw.Draw(page)
        fonts.draw(d, (margin, margin), f"{title} · trang {p}/{total} · {mmss(t0)}–{mmss(t1)} · thời điểm = thời gian thật trong video gốc",
                   28, "black")
        prev_scene = frames[frames.index(chunk[0]) - 1]["scene_id"] if frames.index(chunk[0]) > 0 else None
        for i, (f, img) in enumerate(zip(chunk, tiles)):
            x = margin + (i % cols) * (tile_w + gap)
            y = margin + 60 + (i // cols) * (tile_h + cap_h + gap)
            page.paste(img, (x, y))
            hot = [r for r in f["reasons"] if r in HOT_REASONS]
            if hot:
                d.rectangle([x - 4, y - 4, x + tile_w + 3, y + img.height + 3], outline=(230, 110, 0), width=5)
            if f["scene_id"] != prev_scene:
                d.rectangle([x, y, x + 150, y + 34], fill=(20, 90, 200))
                fonts.draw(d, (x + 8, y + 3), f"▶ {f['scene_id']}", 24, "white")
            prev_scene = f["scene_id"]
            cy = y + img.height + 4
            fonts.draw(d, (x, cy), f"f{f['frame']:06d}  {mmss(f['t'])}  {f['scene_id']}", 26, "black")
            tags = ", ".join(REASONS[r] for r in f["reasons"] if r != "grid")
            fonts.draw(d, (x, cy + 32), _fit(d, fonts, tags, 24, tile_w), 24, (200, 90, 0) if hot else (90, 90, 90))
            speaking = [ln for ln in lines if ln["start_seconds"] <= f["t"] <= ln["end_seconds"]]
            if speaking:
                ln = speaking[0]
                fonts.draw(d, (x, cy + 64), _fit(d, fonts, f"{ln['line_id']}「{ln.get('text_ja', '')}」", 24, tile_w), 24, (0, 70, 0))
        y = margin + 60 + rows_used * (tile_h + cap_h + gap) + 6
        d.line([margin, y, page_w - margin, y], fill=(150, 150, 150), width=2)
        fonts.draw(d, (margin, y + 8), "Lời thoại trong khoảng thời gian của trang này (Whisper, có thể sai):", 26, "black")
        y += 50
        for kind, text in dlg_rows or [("vi", "(không có lời thoại)")]:
            fonts.draw(d, (margin, y), text, 28, (0, 70, 0) if kind == "ja" else (60, 60, 60))
            y += 38
        pages.append(encode(page))
    return pages


MAX_PDF_BYTES = 24 * 2**20  # claude.ai accepts 30 MB per file


def write_pdf_parts(path: Path, jpeg_pages: list[bytes]) -> list[Path]:
    """One PDF, or numbered parts when it would exceed MAX_PDF_BYTES."""
    parts: list[list[bytes]] = [[]]
    size = 0
    for page in jpeg_pages:
        if parts[-1] and size + len(page) > MAX_PDF_BYTES:
            parts.append([])
            size = 0
        parts[-1].append(page)
        size += len(page)
    path = Path(path)
    for old in path.parent.glob(f"{path.stem}*.pdf"):
        old.unlink()
    if len(parts) == 1:
        write_pdf(path, parts[0])
        return [path]
    out = [path.with_name(f"{path.stem}_phan{i}.pdf") for i in range(1, len(parts) + 1)]
    for p, pages in zip(out, parts):
        write_pdf(p, pages)
    return out


def write_pdf(path: Path, jpeg_pages: list[bytes]) -> None:
    """Minimal PDF: one full-page JPEG (DCTDecode) per page, 150 dpi."""
    import io
    import struct

    def jpeg_size(data: bytes) -> tuple[int, int]:
        i = 2
        while i < len(data):
            marker, length = data[i + 1], struct.unpack(">H", data[i + 2:i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            i += 2 + length
        raise PacketError("JPEG không hợp lệ")

    out = io.BytesIO()
    offsets: list[int] = []

    def obj(num: int, body: bytes) -> None:
        offsets.append(out.tell())
        out.write(f"{num} 0 obj\n".encode() + body + b"\nendobj\n")

    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    n = len(jpeg_pages)
    kids = " ".join(f"{3 + 3 * i} 0 R" for i in range(n))
    obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    obj(2, f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    for i, data in enumerate(jpeg_pages):
        w, h = jpeg_size(data)
        pw, ph = w * 72 / 150, h * 72 / 150
        page, content, image = 3 + 3 * i, 4 + 3 * i, 5 + 3 * i
        obj(page, f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {pw:.2f} {ph:.2f}] /Resources << /XObject << /Im{i} {image} 0 R >> >> "
                  f"/Contents {content} 0 R >>".encode())
        stream = f"q {pw:.2f} 0 0 {ph:.2f} 0 0 cm /Im{i} Do Q".encode()
        obj(content, f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
        obj(image, f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace /DeviceRGB /BitsPerComponent 8 "
                   f"/Filter /DCTDecode /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream")
    xref = out.tell()
    out.write(f"xref\n0 {len(offsets) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(offsets) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    Path(path).write_bytes(out.getvalue())


# ── export ───────────────────────────────────────────────────────────────


def _packet_name(mode: str, start: float, end: float) -> str:
    def tag(t: float) -> str:
        return f"{int(t // 60):02d}-{int(t % 60):02d}"

    return f"{'tong_quan' if mode == 'overview' else 'chi_tiet'}_{tag(start)}_{tag(end)}"


def export_packet(run_dir: Path, *, mode: str, start: float | None = None, end: float | None = None,
                  video: Path | None = None, progress=print) -> Path:
    try:
        import PIL  # noqa: F401
    except ImportError as exc:
        raise PacketError("thiếu thư viện Pillow — chạy: python -m pip install pillow") from exc
    run_dir = Path(run_dir)
    index = load_index(run_dir)
    fps, duration = float(index["video"]["fps"]), float(index["video"]["duration_seconds"])
    if mode == "overview":
        start, end = 0.0, duration
    elif mode == "detail":
        if start is None or end is None:
            raise PacketError("chế độ chi tiết cần --start và --end")
        end = min(end, duration)
        if not DETAIL_MIN_SECONDS <= end - start <= DETAIL_MAX_SECONDS:
            raise PacketError(f"đoạn chi tiết nên dài {DETAIL_MIN_SECONDS:g}–{DETAIL_MAX_SECONDS:g} giây (đang là {end - start:.1f} s)")
    else:
        raise PacketError("mode phải là overview hoặc detail")
    if not 0 <= start < end:
        raise PacketError("thời gian không hợp lệ")
    source = resolve_video(index, run_dir, video)
    packet_dir = run_dir / PACKET_DIR / _packet_name(mode, start, end)
    packet_dir.mkdir(parents=True, exist_ok=True)
    progress(f"Gói: {packet_dir}")

    if mode == "detail":
        progress("Đo chuyển động trong đoạn (ffmpeg)…")
        motion = motion_scores(source, fps, start, end)
        hotspots = find_hotspots(index, start, end, motion, fps)
        frames = plan_frames(index, start, end, base_interval=DETAIL["base_interval"], hotspots=hotspots,
                             hot_fps=DETAIL["hot_fps"], max_frames=DETAIL["max_frames"])
        cfg = DETAIL
    else:
        hotspots = find_hotspots(index, start, end)
        frames = plan_frames(index, start, end, base_interval=OVERVIEW["base_interval"], max_frames=OVERVIEW["max_frames"],
                             with_lines=False)
        cfg = OVERVIEW
    progress(f"Trích {len(frames)} khung hình chính xác theo số frame…")
    extract_frames(source, frames, packet_dir / "frames", fps, width=cfg["width"])
    lines = [ln for ln in all_lines(index) if _overlaps(ln["start_seconds"], ln["end_seconds"], start, end)]
    scenes = [{k: sc[k] for k in ("scene_id", "start_seconds", "end_seconds")} for sc in index["scenes"]
              if _overlaps(sc["start_seconds"], sc["end_seconds"], start, end)]
    packet = {
        "version": "1.0", "packet_id": packet_dir.name, "mode": mode, "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_index_id": index["source_index_id"], "run_dir": str(run_dir.resolve()), "video": str(source.resolve()),
        "fps": fps, "duration_seconds": duration, "scope": {"start_seconds": round(start, 3), "end_seconds": round(end, 3)},
        "scenes": scenes, "lines": [{k: ln.get(k) for k in ("line_id", "scene_id", "start_seconds", "end_seconds", "text_ja",
                                                             "asr_confidence", "low_confidence", "asr_flags", "excite")} for ln in lines],
        "hotspots": hotspots, "frames": frames, "supplements": [],
    }
    progress("Dựng contact sheet PDF…")
    title = f"Gói {packet['packet_id']}"
    pdfs = write_pdf_parts(packet_dir / "01_khung_hinh.pdf", render_pages(packet_dir, frames, lines, title=title, cols=cfg["cols"], rows=cfg["rows"],
                                                             intro=_intro(packet)))
    (packet_dir / PACKET_FILE).write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (packet_dir / "02_loi_thoai_va_canh.md").write_text(render_transcript_md(packet), encoding="utf-8")
    write_prompts(packet_dir, packet)
    progress(f"Xong: {len(frames)} khung hình, {len(lines)} câu thoại, {len(scenes)} cảnh, {len(hotspots)} điểm máy gợi ý · "
             + ", ".join(f"{p.name} {p.stat().st_size / 2**20:.1f} MB" for p in pdfs))
    return packet_dir


def _intro(packet: dict) -> list[str]:
    sc = packet["scope"]
    out = [
        (f"Phạm vi: {mmss(sc['start_seconds'])}–{mmss(sc['end_seconds'])} · fps {packet['fps']:.6f} · "
        f"{len(packet['frames'])} khung hình · {len(packet['lines'])} câu thoại · {len(packet['scenes'])} cảnh."),
        ("Mỗi ảnh ghi: mã frame (f + số frame = round(thời điểm × fps)), thời điểm thật trong video gốc (mm:ss.ss), mã cảnh, "
        "lý do chọn ảnh, và câu thoại đang nói (nếu có). Thanh xanh ▶ = bắt đầu cảnh mới. Viền cam = vùng máy gợi ý xem kỹ."),
        ("Lý do chọn ảnh: lưới đều · cắt cảnh · thoại · keyframe · chuyển động nhanh · to / cảm thán · nghi hiểu lầm · "
        "nghi setup/payoff · bổ sung. Các gợi ý chỉ là tín hiệu máy (âm lượng, dấu !, ?, từ như え？/違う, cụm từ lặp lại) — "
        "KHÔNG phải kết luận về nội dung phim."),
        "Lời thoại do Whisper nhận dạng, có thể sai; câu có cờ low_confidence / known_hallucination không được coi là chắc chắn.",
        "Khi trích dẫn bằng chứng, dùng mã frame (vd f004321) và line_id (vd sc045_l02) đúng như in trên trang.",
    ]
    if packet["mode"] == "detail":
        out.append("Các cảnh trong phạm vi: " + " · ".join(
            f"{s['scene_id']} {mmss(s['start_seconds'])}–{mmss(s['end_seconds'])}" for s in packet["scenes"]))
    return out


def render_transcript_md(packet: dict) -> str:
    sc = packet["scope"]
    out = [f"# Gói {packet['packet_id']} — lời thoại, cảnh và mục lục khung hình", "",
           f"- Phạm vi: **{mmss(sc['start_seconds'])}–{mmss(sc['end_seconds'])}** ({sc['start_seconds']}–{sc['end_seconds']} s)",
           f"- fps: `{packet['fps']}` — frame = round(giây × fps); mã frame `f004321` = frame 4321",
           "- Lời thoại do Whisper nhận dạng (tiếng Nhật gốc), **có thể sai**. Không có phụ đề gốc.",
           "", "## Cảnh", "", "| scene_id | bắt đầu | kết thúc | dài (s) |", "|---|---|---|---|"]
    out += [f"| {s['scene_id']} | {mmss(s['start_seconds'])} ({s['start_seconds']}) | {mmss(s['end_seconds'])} ({s['end_seconds']}) | "
            f"{s['end_seconds'] - s['start_seconds']:.2f} |" for s in packet["scenes"]]
    out += ["", "## Lời thoại", "", "| line_id | cảnh | bắt đầu–kết thúc (s) | lời thoại (ja) | tin cậy | cờ |", "|---|---|---|---|---|---|"]
    for ln in packet["lines"]:
        flags = list(ln.get("asr_flags") or []) + (["low_confidence"] if ln.get("low_confidence") else [])
        conf = f"{ln['asr_confidence']:.2f}" if ln.get("asr_confidence") is not None else ""
        out.append(f"| {ln['line_id']} | {ln['scene_id']} | {ln['start_seconds']:.3f}–{ln['end_seconds']:.3f} | {ln['text_ja']} | {conf} | "
                   f"{', '.join(sorted(set(flags)))} |")
    out += ["", "## Điểm máy gợi ý xem kỹ (tín hiệu kỹ thuật — KHÔNG phải kết luận)", ""]
    out += [f"- {mmss(h['start'])}–{mmss(h['end'])} · {REASONS[h['reason']]}" + (f" · {h['line_id']}" if h.get("line_id") else "")
            + (f" · {h['note']}" if h.get("note") else "") for h in packet["hotspots"]] or ["- (không có)"]
    out += ["", "## Mục lục khung hình theo cảnh", ""]
    by_scene: dict[str, list[dict]] = {}
    for f in packet["frames"]:
        by_scene.setdefault(f["scene_id"], []).append(f)
    for sid, fs in by_scene.items():
        out.append(f"- **{sid}**: " + ", ".join(f"f{f['frame']:06d} {mmss(f['t'])}" for f in fs))
    for sup in packet.get("supplements", []):
        out += ["", f"## Bổ sung {sup['name']} ({', '.join(f'{mmss(a)}–{mmss(b)}' for a, b in sup['ranges'])})", ""]
        out.append(", ".join(f"f{f:06d}" for f in sup["frames"]))
    return "\n".join(out) + "\n"


def _load_packet(packet_dir: Path) -> dict:
    path = Path(packet_dir) / PACKET_FILE
    if not path.is_file():
        raise PacketError(f"không thấy {path} — chạy bước xuất gói trước")
    return json.loads(path.read_text(encoding="utf-8"))


def _save_packet(packet_dir: Path, packet: dict) -> None:
    (Path(packet_dir) / PACKET_FILE).write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def add_evidence(packet_dir: Path, ranges: list[tuple[float, float]], *, fps_sample: float = MORE["fps"], progress=print) -> Path:
    """Extra frames for ranges Claude asked to see; they join the packet so they can be cited."""
    packet_dir = Path(packet_dir)
    packet = _load_packet(packet_dir)
    index = load_index(Path(packet["run_dir"]))
    fps = packet["fps"]
    known = {f["frame"] for f in packet["frames"]}
    new: dict[int, dict] = {}
    for s, e in ranges:
        s, e = max(0.0, s), min(e, packet["duration_seconds"])
        t = s
        while t < e:
            f = round(t * fps)
            new.setdefault(f, {"frame": f, "t": round(f / fps, 3), "scene_id": scene_at(index, f / fps), "reasons": ["request"]})
            t += 1.0 / fps_sample
    frames = sorted(new.values(), key=lambda f: f["frame"])
    if len(frames) > MORE["max_frames"]:
        raise PacketError(f"{len(frames)} khung hình là quá nhiều cho một lần bổ sung (tối đa {MORE['max_frames']}) — thu hẹp khoảng")
    video = Path(packet["video"]) if Path(packet["video"]).is_file() else resolve_video(index, Path(packet["run_dir"]))
    extract_frames(video, frames, packet_dir / "frames", fps, width=MORE["width"])
    n = len(packet["supplements"]) + 1
    name = f"bo_sung_{n:02d}"
    lines = [ln for ln in all_lines(index) if any(_overlaps(ln["start_seconds"], ln["end_seconds"], s, e) for s, e in ranges)]
    intro = [(f"Bổ sung bằng chứng {n}: {', '.join(f'{mmss(a)}–{mmss(b)}' for a, b in ranges)} · {fps_sample:g} khung/giây · "
             f"{len(frames)} khung hình. Mã frame mới có thể trích dẫn như các frame khác.")]
    pdfs = write_pdf_parts(packet_dir / f"{name}.pdf", render_pages(packet_dir, frames, lines, title=f"Gói {packet['packet_id']} · {name}",
                                                       cols=MORE["cols"], rows=MORE["rows"], intro=intro))
    packet["frames"] = sorted([f for f in packet["frames"]] + [f for f in frames if f["frame"] not in known], key=lambda f: f["frame"])
    packet["supplements"].append({"name": name, "ranges": [[round(s, 3), round(e, 3)] for s, e in ranges],
                                  "frames": [f["frame"] for f in frames]})
    _save_packet(packet_dir, packet)
    (packet_dir / "02_loi_thoai_va_canh.md").write_text(render_transcript_md(packet), encoding="utf-8")
    progress(f"Xong: {', '.join(p.name for p in pdfs)} ({len(frames)} khung hình)")
    return pdfs[0]


# ── prompts ──────────────────────────────────────────────────────────────

PROMPT_RULES = """QUY TẮC BẮT BUỘC
1. Chỉ dựa vào hình ảnh trong PDF và lời thoại trong file .md của gói này. KHÔNG dùng tên tập, kiến thức về cốt truyện
   tập này, bản tóm tắt trên mạng hay bất kỳ phân tích AI nào khác (kể cả bản đồ cốt truyện của Qwen).
2. Được nhận diện nhân vật quen thuộc của Crayon Shin-chan theo ngoại hình/lời gọi tên, nhưng ghi rõ căn cứ.
3. Mỗi điều khẳng định phải có bằng chứng: mã frame (vd f004321) và/hoặc line_id (vd sc045_l02), kèm thời điểm.
   Chỉ dùng mã frame có in trên trang PDF và line_id có trong file .md.
4. Phân loại: "observation" = thấy/nghe trực tiếp trên frame/lời thoại; "inference" = suy ra có căn cứ từ các quan sát;
   "hypothesis" = phỏng đoán chưa đủ bằng chứng. Không chắc thì ghi hypothesis hoặc để trống — KHÔNG bịa.
5. Lời thoại Whisper có thể sai. Nếu lời thoại mâu thuẫn với hình ảnh, nói rõ trong dialogue_doubts.
6. Nếu một chỗ chưa rõ (động tác quá nhanh, không thấy ai làm gì), ghi vào evidence_requests khoảng thời gian cần xem
   thêm (người dùng sẽ xuất thêm khung hình dày hơn và gửi lại).
7. Các ô viền cam chỉ là gợi ý kỹ thuật của máy (âm lượng, dấu !/?, từ lặp lại), không phải sự thật về nội dung."""

UNDERSTANDING_SCHEMA = """{
  "situation": {"complete": true, "start_seconds": 0.0, "end_seconds": 0.0, "summary_vi": "…", "beat_ids": ["b01"]},
  "characters": [{"character_id": "shinnosuke", "name_ja": "しんのすけ", "name_vi": "Shin", "identified_by_vi": "áo đỏ, quần vàng; được gọi 'しんちゃん' ở sc012_l03",
                  "goal_vi": "…", "obstacle_vi": "…", "beat_ids": ["b01", "b03"], "kind": "inference"}],
  "beats": [{"beat_id": "b01", "start_seconds": 0.0, "end_seconds": 0.0, "kind": "observation", "who": ["shinnosuke"],
             "what_vi": "điều thấy/nghe được, một câu", "frames": ["f004321"], "line_ids": ["sc045_l02"]}],
  "causal_chain": [{"step": 1, "cause": ["b01"], "action": ["b02"], "reaction": ["b03"], "consequence": ["b04"],
                    "explanation_vi": "vì … nên …", "kind": "inference"}],
  "misunderstandings": [{"who": "misae", "believes_vi": "…", "truth_vi": "…", "beat_ids": ["b03", "b06"], "kind": "inference"}],
  "gags": [{"setup": ["b02"], "payoff": ["b07"], "mechanism": "callback|escalation|misunderstanding|reversal|incongruity|character_flaw|other",
            "why_funny_vi": "…", "kind": "inference"}],
  "dialogue_doubts": [{"line_id": "sc045_l02", "issue_vi": "…"}],
  "evidence_requests": [{"start_seconds": 0.0, "end_seconds": 0.0, "reason_vi": "…"}]
}"""

SCRIPT_SCHEMA = """{
  "title_ja": "…", "title_vi": "…",
  "skills_applied": [{"skill": "sw-sitcom-comedy", "how_vi": "…"}],
  "segments": [
    {"segment_id": "s01", "story_role": "hook", "source_start_seconds": 0.0, "source_end_seconds": 0.0,
     "dialogue_used": [{"line_id": "sc045_l02"}],
     "claims": [{"claim_id": "s01c1", "type": "question|commentary|event|quote", "text_ja": "…", "text_vi": "…",
                 "beat_ids": ["b07"],
                 "evidence": [{"scene_id": "sc045", "line_id": null, "start_seconds": 0.0, "end_seconds": 0.0}]}],
     "craft_notes": ["…"]}
  ]
}"""

SKILL_CHECKLIST = """CÁCH ÁP DỤNG 5 SKILL BIÊN KỊCH (nếu tài khoản của bạn đã cài các skill này thì dùng trực tiếp; nếu không, theo tóm tắt sau)
- sw-story-structure: 起承転結 trong 60–90 s — hook (起, vào thẳng xung đột hoặc câu hỏi, "第一刀切在接近高潮处"),
  development (承, chiếm phần lớn, mỗi nhịp đẩy cao hơn), conflict, climax (転, cao trào phải là điều đã thấy trong phim),
  resolution (結, ngắn, có dư vị; kết kiểu "响雷"/呼应 gọi lại hook). Không giải thích dài dòng.
- sw-scene-craft: mỗi segment có một giá trị đổi chiều (vui→tệ, yên→loạn); vào muộn ra sớm; hành động hơn lời nói —
  chọn cut có động tác/biểu cảm rõ; để lại một câu hỏi treo giữa các segment.
- sw-character-conflict: nói rõ nhân vật MUỐN gì và cái gì CHẶN; xung đột leo thang từng bậc, không nhảy cóc;
  phản ứng đúng tính cách đã thấy trong phim.
- sw-sitcom-comedy: setup bình thường đáng tin → punchline bất ngờ, từ khoá cười để CUỐI câu; không giải thích trò đùa,
  không "đi quá" punchline; callback/running gag nếu phim có thật; mỗi câu kể phải vừa buồn cười, vừa phục vụ mạch truyện.
- sw-japanese-screenwriting: giọng kể Nhật tự nhiên kiểu review/ツッコミ — người kể là "tsukkomi" của khán giả;
  câu ngắn, nhịp nhảy (câu sau lật câu trước); không tường thuật từng động tác ("〜して、〜して"); dùng ナレーション
  văn nói, có thể hỏi khán giả (〜と思いきや？ / まさかの〜).
GIỚI HẠN SỰ THẬT: mọi chi tiết cốt truyện trong lời kể (claim type event/quote) phải dựa trên beat đã duyệt (beat_ids)
và có evidence đúng thời điểm nguồn. commentary/question là giọng người kể — được bình luận, đặt câu hỏi, đùa,
nhưng KHÔNG được thêm sự kiện mới. quote phải trích nguyên văn lời thoại có line_id."""


CLAUDE_CODE_NOTE = """NẾU BẠN LÀ CLAUDE CODE TRÊN MÁY NGƯỜI DÙNG (đăng nhập bằng tài khoản Pro, không dùng API key)
Đọc PDF trong thư mục gói (hoặc ảnh trong frames/), ghi câu trả lời vào {file} trong thư mục gói, rồi tự kiểm tra:
    python -m lib.anime_claude_packet {step} --packet "<thư mục gói>" --file "<thư mục gói>/{file}"
Sửa tới khi kết quả OK. Cần thêm khung hình: python -m lib.anime_claude_packet more --packet "<thư mục gói>" --ranges "2:41-2:44".
KHÔNG tự chạy bước approve — chỉ người dùng được duyệt. KHÔNG render video."""


def write_prompts(packet_dir: Path, packet: dict) -> None:
    sc = packet["scope"]
    head = (f"Gói: {packet['packet_id']} · phạm vi {mmss(sc['start_seconds'])}–{mmss(sc['end_seconds'])} "
            f"({sc['start_seconds']}–{sc['end_seconds']} s) · fps {packet['fps']}\n"
            "Tệp đính kèm: 01_khung_hinh.pdf hoặc các phần 01_khung_hinh_phanN.pdf (khung hình theo thời gian), 02_loi_thoai_va_canh.md (lời thoại tiếng Nhật, cảnh, "
            "mục lục khung hình)" + (", các bo_sung_NN.pdf nếu có" if packet["mode"] == "detail" else "") + ".\n")
    if packet["mode"] == "overview":
        text = (f"# Bước 0 — Chọn MỘT tình huống hoàn chỉnh 60–90 giây\n\n{head}\n{PROMPT_RULES}\n\n"
                "NHIỆM VỤ\nXem toàn bộ khung hình và lời thoại. Đề xuất 1–3 tình huống HOÀN CHỈNH (có mở đầu, xung đột, "
                "cao trào, kết) dài 60–90 giây, phù hợp làm video review hài. Với mỗi đề xuất ghi:\n"
                "- start_seconds và end_seconds (đúng mốc cắt cảnh nếu được, xem bảng Cảnh);\n"
                "- tóm tắt 2–3 câu tiếng Việt CHỈ những gì thấy/nghe được, kèm mã frame và line_id làm bằng chứng;\n"
                "- vì sao tình huống này hài/hấp dẫn, và điều gì còn chưa chắc.\n"
                "Đây chỉ là bước chọn đoạn. Gói tổng quan thưa (khoảng 2 giây một ảnh) nên đừng kết luận chi tiết hành động.\n"
                "Trả lời bằng tiếng Việt, cuối cùng ghi dòng: CHON: <start>-<end> cho đề xuất tốt nhất.\n")
        (packet_dir / "03_PROMPT_CHON_TINH_HUONG.md").write_text(text, encoding="utf-8")
        return
    understanding = (f"# Bước 1 — HIỂU PHIM (chưa viết kịch bản)\n\n{head}\n{PROMPT_RULES}\n\n"
                     "NHIỆM VỤ\nXem lần lượt từng trang PDF và lời thoại, rồi trả lời:\n"
                     "1. Ai là nhân vật, mỗi người muốn gì (mục tiêu) và vấp phải gì (trở ngại)?\n"
                     "2. Chuỗi nguyên nhân → hành động → phản ứng → hậu quả.\n"
                     "3. Hiểu lầm, tình tiết hài, setup → payoff — CHỈ nếu có thật trong phim; vì sao buồn cười.\n"
                     "4. Bằng chứng (mã frame, line_id, thời điểm) cho từng ý; tách observation / inference / hypothesis.\n"
                     "5. Đoạn này có phải một tình huống hoàn chỉnh không; nếu không, đề xuất start/end hoàn chỉnh hơn.\n\n"
                     "TRẢ LỜI GỒM 2 PHẦN\n"
                     "Phần A — tóm tắt tiếng Việt ngắn cho người đọc (≤ 15 dòng).\n"
                     "Phần B — MỘT khối ```json đúng khuôn dưới đây (beat là đơn vị quan sát nhỏ nhất, 8–25 beat; mọi mục khác "
                     "chỉ được tham chiếu beat_id; causal_chain/misunderstandings/gags không bao giờ là observation):\n\n"
                     f"```json\n{UNDERSTANDING_SCHEMA}\n```\n\n"
                     "Thời gian tính bằng giây trong video gốc (như in trên PDF). Không có thì để mảng rỗng [].\n\n"
                     f"{CLAUDE_CODE_NOTE.format(step='understanding', file='dan_hieu_phim_vao_day.txt')}")
    (packet_dir / "03_PROMPT_HIEU_PHIM.md").write_text(understanding, encoding="utf-8")
    script = (f"# Bước 2 — VIẾT KỊCH BẢN REVIEW TIẾNG NHẬT 60–90 GIÂY\n\n{head}\n"
              "CHỈ DÙNG SAU KHI NGƯỜI DÙNG ĐÃ DUYỆT PHẦN HIỂU PHIM. Đính kèm thêm hieu_phim.json đã duyệt và hieu_phim.md.\n\n"
              f"{SKILL_CHECKLIST}\n\n"
              "YÊU CẦU KỊCH BẢN\n"
              "- Tổng thời lượng các cut 60–90 giây; 5 vai theo thứ tự xuất hiện lần đầu: hook → development → conflict → climax → "
              "resolution (có thể nhiều segment cùng vai). Hook mạnh trong 3 giây đầu; được mở bằng một cut từ cao trào.\n"
              "- Mỗi segment là MỘT đoạn liền trong video gốc (source_start/end_seconds, nằm trong phạm vi gói và trong các cảnh "
              "liệt kê); lời kể của segment phải đọc xong trong độ dài cut (≈ 6.5 ký tự/giây, tối đa 9).\n"
              "- Lời kể = các claim nối tiếp nhau theo đúng thứ tự (máy sẽ ghép lại). Mỗi claim có text_ja và text_vi.\n"
              "- event/quote phải có beat_ids (beat đã duyệt, không phải hypothesis) và evidence: scene_id + start/end nằm trong "
              "cảnh đó (+ line_id nếu dựa vào lời thoại). quote phải trích nguyên văn một câu thoại.\n"
              "- dialogue_used: các câu thoại gốc giữ lại cho khán giả nghe (chỉ cần line_id).\n"
              "- Không cần tính frame, timeline hay tổng thời lượng — máy tự tính và kiểm tra.\n\n"
              "TRẢ LỜI GỒM: (A) kịch bản đọc thử dạng bảng ngắn (vai · câu Nhật · nghĩa Việt), (B) một khối ```json:\n\n"
              f"```json\n{SCRIPT_SCHEMA}\n```\n\n"
              f"{CLAUDE_CODE_NOTE.format(step='script', file='dan_kich_ban_vao_day.txt')}")
    (packet_dir / "04_PROMPT_VIET_KICH_BAN.md").write_text(script, encoding="utf-8")


# ── answers from Claude ──────────────────────────────────────────────────


def extract_json(text: str) -> dict:
    """The JSON object in Claude's answer: prefer a ```json block, else the first decodable {…}."""
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    decoder = json.JSONDecoder()
    for candidate in blocks + [text]:
        for m in re.finditer(r"\{", candidate):
            try:
                data, _ = decoder.raw_decode(candidate[m.start():])
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
    raise PacketError("không tìm thấy khối JSON hợp lệ trong câu trả lời — hãy dán nguyên khối ```json … ``` của Claude")


def _ids(values: Any) -> list[str]:
    return [v for v in values if isinstance(v, str)] if isinstance(values, list) else []


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def validate_understanding(data: dict, packet: dict, index: dict) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    s0, s1 = packet["scope"]["start_seconds"], packet["scope"]["end_seconds"]
    frames = {f"f{f['frame']:06d}": f for f in packet["frames"]}
    lines = {ln["line_id"]: ln for ln in all_lines(index)}
    beats: dict[str, dict] = {}
    for i, b in enumerate(data.get("beats") or []):
        where = f"beats[{i}]"
        if not isinstance(b, dict):
            errors.append(f"{where}: không phải đối tượng")
            continue
        bid = b.get("beat_id")
        if not isinstance(bid, str) or not bid or bid in beats:
            errors.append(f"{where}: beat_id thiếu hoặc trùng ({bid!r})")
            continue
        beats[bid] = b
        kind, s, e = b.get("kind"), _num(b.get("start_seconds")), _num(b.get("end_seconds"))
        if kind not in KINDS:
            errors.append(f"{bid}: kind phải là observation/inference/hypothesis")
        if s is None or e is None or e < s:
            errors.append(f"{bid}: start_seconds/end_seconds không hợp lệ")
            continue
        if s < s0 - 1 or e > s1 + 1:
            errors.append(f"{bid}: {mmss(s)}–{mmss(e)} nằm ngoài phạm vi gói {mmss(s0)}–{mmss(s1)} (máy chưa cho xem đoạn đó)")
        if not str(b.get("what_vi") or "").strip():
            errors.append(f"{bid}: thiếu what_vi")
        cited_frames, cited_lines = _ids(b.get("frames")), _ids(b.get("line_ids"))
        for fid in cited_frames:
            f = frames.get(fid)
            if f is None:
                errors.append(f"{bid}: frame {fid} không có trong gói (chỉ dùng mã in trên PDF)")
            elif not (s - 2.0 <= f["t"] <= e + 2.0):
                (errors if not (s - 5.0 <= f["t"] <= e + 5.0) else warnings).append(
                    f"{bid}: frame {fid} ({mmss(f['t'])}) cách xa thời gian của beat {mmss(s)}–{mmss(e)}")
        for lid in cited_lines:
            ln = lines.get(lid)
            if ln is None:
                errors.append(f"{bid}: line_id {lid} không tồn tại")
            elif not _overlaps(ln["start_seconds"] - 2.0, ln["end_seconds"] + 2.0, s, e + 1e-6):
                warnings.append(f"{bid}: câu {lid} ({mmss(ln['start_seconds'])}) không trùng thời gian của beat")
            elif ln.get("low_confidence"):
                warnings.append(f"{bid}: câu {lid} có độ tin cậy thấp — cần nghe lại video")
        if kind == "observation" and not cited_frames and not cited_lines:
            errors.append(f"{bid}: observation phải có ít nhất một frame hoặc line_id")
        if kind == "inference" and not cited_frames and not cited_lines:
            errors.append(f"{bid}: inference trong beat cũng phải có bằng chứng; không có thì ghi hypothesis")

    def refs(where: str, values: Any, *, required: bool = True) -> list[dict]:
        ids = _ids(values)
        if required and not ids:
            errors.append(f"{where}: phải tham chiếu ít nhất một beat_id")
        missing = [i for i in ids if i not in beats]
        if missing:
            errors.append(f"{where}: beat không tồn tại {missing}")
        return [beats[i] for i in ids if i in beats]

    def reasoning_kind(where: str, item: dict, cited: list[dict]) -> None:
        kind = item.get("kind")
        if kind == "observation":
            errors.append(f"{where}: suy luận về nhân quả/hiểu lầm/gag không thể là observation — dùng inference hoặc hypothesis")
        elif kind not in KINDS:
            errors.append(f"{where}: kind phải là inference hoặc hypothesis")
        elif kind == "inference" and cited and all(b.get("kind") == "hypothesis" for b in cited):
            errors.append(f"{where}: inference chỉ dựa trên beat giả thuyết — phải ghi hypothesis")

    for i, ch in enumerate(data.get("characters") or []):
        where = f"characters[{i}] {ch.get('character_id', '?')}"
        if not ch.get("character_id"):
            errors.append(f"{where}: thiếu character_id")
        cited = refs(where, ch.get("beat_ids"), required=ch.get("kind") != "hypothesis")
        if ch.get("kind") not in KINDS:
            errors.append(f"{where}: kind phải là observation/inference/hypothesis")
        elif ch.get("kind") != "hypothesis" and cited and all(b.get("kind") == "hypothesis" for b in cited):
            errors.append(f"{where}: mục tiêu/trở ngại chỉ dựa trên beat giả thuyết — phải ghi hypothesis")
    for i, step in enumerate(data.get("causal_chain") or []):
        where = f"causal_chain[{i}]"
        cited = []
        for part in ("cause", "action", "reaction", "consequence"):
            cited += refs(f"{where}.{part}", step.get(part), required=part in ("cause", "action"))
        reasoning_kind(where, step, cited)
    for i, m in enumerate(data.get("misunderstandings") or []):
        where = f"misunderstandings[{i}]"
        reasoning_kind(where, m, refs(where, m.get("beat_ids")))
    for i, g in enumerate(data.get("gags") or []):
        where = f"gags[{i}]"
        setup, payoff = refs(f"{where}.setup", g.get("setup")), refs(f"{where}.payoff", g.get("payoff"))
        reasoning_kind(where, g, setup + payoff)
        if setup and payoff and min(_num(b.get("start_seconds")) or 0 for b in payoff) < min(_num(b.get("start_seconds")) or 0 for b in setup):
            errors.append(f"{where}: payoff xảy ra trước setup")
        if not str(g.get("why_funny_vi") or "").strip():
            warnings.append(f"{where}: thiếu why_funny_vi")
    situation = data.get("situation")
    if not isinstance(situation, dict):
        errors.append("situation: thiếu (tình huống có hoàn chỉnh không, start/end)")
    else:
        refs("situation", situation.get("beat_ids"))
        ss, se = _num(situation.get("start_seconds")), _num(situation.get("end_seconds"))
        if ss is None or se is None or se <= ss:
            errors.append("situation: start_seconds/end_seconds không hợp lệ")
    for i, req in enumerate(data.get("evidence_requests") or []):
        rs, re_ = _num(req.get("start_seconds")), _num(req.get("end_seconds"))
        if rs is None or re_ is None or re_ <= rs:
            errors.append(f"evidence_requests[{i}]: start/end không hợp lệ")
    for i, doubt in enumerate(data.get("dialogue_doubts") or []):
        if doubt.get("line_id") not in lines:
            errors.append(f"dialogue_doubts[{i}]: line_id {doubt.get('line_id')!r} không tồn tại")
    grounded = [b for b in beats.values() if b.get("kind") in ("observation", "inference")]
    if not grounded:
        errors.append("không có beat nào là observation/inference có bằng chứng — chưa hiểu được đoạn phim")
    return {"status": "OK" if not errors else "FAIL", "errors": errors, "warnings": warnings,
            "counts": {"beats": len(beats), **{k: sum(1 for b in beats.values() if b.get("kind") == k) for k in KINDS},
                       "causal_chain": len(data.get("causal_chain") or []), "misunderstandings": len(data.get("misunderstandings") or []),
                       "gags": len(data.get("gags") or []), "characters": len(data.get("characters") or []),
                       "evidence_requests": len(data.get("evidence_requests") or [])}}


def _beat_evidence(b: dict, frames: dict, lines: dict) -> str:
    bits = [f"{fid} ({mmss(frames[fid]['t'])})" for fid in _ids(b.get("frames")) if fid in frames]
    bits += [f"{lid}「{lines[lid].get('text_ja', '')}」" for lid in _ids(b.get("line_ids")) if lid in lines]
    return "; ".join(bits) or "không có"


def render_understanding_md(data: dict, packet: dict, index: dict, result: dict) -> str:
    frames = {f"f{f['frame']:06d}": f for f in packet["frames"]}
    lines = {ln["line_id"]: ln for ln in all_lines(index)}
    beats = {b.get("beat_id"): b for b in data.get("beats") or [] if isinstance(b, dict)}
    sc = packet["scope"]

    def label(item: dict) -> str:
        return f"**[{KIND_VI.get(item.get('kind'), '?')}]**"

    def bref(ids: Any) -> str:
        return ", ".join(f"{i} ({mmss(beats[i]['start_seconds'])})" if i in beats and _num(beats[i].get("start_seconds")) is not None
                         else str(i) for i in _ids(ids)) or "—"

    out = [f"# Hiểu phim — gói {packet['packet_id']} ({mmss(sc['start_seconds'])}–{mmss(sc['end_seconds'])})", "",
           f"**Kiểm tra tự động: {result['status']}** · " + ", ".join(f"{k} {v}" for k, v in result["counts"].items()), "",
           ("> Do Claude phân tích từ khung hình + lời thoại Whisper. **Chưa được người đối chiếu với video.** "
           "Lời thoại trong ngoặ「」 do máy chèn từ transcript gốc, không phải Claude tự viết. "
           "Hãy mở video tại các mốc thời gian để kiểm tra trước khi duyệt."), ""]
    if result["errors"]:
        out += ["## ❌ Lỗi phải sửa (gửi nguyên mục này lại cho Claude)", "", *[f"- {e}" for e in result["errors"]], ""]
    if result["warnings"]:
        out += ["## ⚠ Cảnh báo", "", *[f"- {w}" for w in result["warnings"]], ""]
    sit = data.get("situation") or {}
    if sit:
        out += ["## Tình huống", "", (f"- Hoàn chỉnh: **{'có' if sit.get('complete') else 'không'}** · "
                f"{mmss(_num(sit.get('start_seconds')) or 0)}–{mmss(_num(sit.get('end_seconds')) or 0)}"),
                f"- {sit.get('summary_vi', '')} (beat: {bref(sit.get('beat_ids'))})", ""]
    out += ["## Nhân vật — mục tiêu và trở ngại", ""]
    for ch in data.get("characters") or []:
        out.append(f"- {label(ch)} **{ch.get('name_vi') or ch.get('character_id')}** ({ch.get('name_ja', '')}) — nhận diện: "
                   f"{ch.get('identified_by_vi', '')}  \n  muốn: {ch.get('goal_vi', '')} · trở ngại: {ch.get('obstacle_vi', '')} · "
                   f"beat: {bref(ch.get('beat_ids'))}")
    out += ["", "## Diễn biến (beat)", ""]
    for b in sorted(beats.values(), key=lambda b: _num(b.get("start_seconds")) or 0):
        s, e = _num(b.get("start_seconds")) or 0, _num(b.get("end_seconds")) or 0
        out.append(f"- `{b.get('beat_id')}` {label(b)} {mmss(s)}–{mmss(e)} {', '.join(_ids(b.get('who')))}: {b.get('what_vi', '')}  \n"
                   f"  Bằng chứng: {_beat_evidence(b, frames, lines)}")
    out += ["", "## Nguyên nhân → hành động → phản ứng → hậu quả", ""]
    for step in data.get("causal_chain") or []:
        out.append(f"{step.get('step', '•')}. {label(step)} {step.get('explanation_vi', '')}  \n"
                   f"   nguyên nhân {bref(step.get('cause'))} → hành động {bref(step.get('action'))} → phản ứng "
                   f"{bref(step.get('reaction'))} → hậu quả {bref(step.get('consequence'))}")
    out += ["", "## Hiểu lầm", ""]
    out += [f"- {label(m)} {m.get('who', '')} tưởng: {m.get('believes_vi', '')} — thực tế: {m.get('truth_vi', '')} (beat: {bref(m.get('beat_ids'))})"
            for m in data.get("misunderstandings") or []] or ["- (không có)"]
    out += ["", "## Tình tiết hài (setup → payoff)", ""]
    out += [f"- {label(g)} [{g.get('mechanism', '')}] {g.get('why_funny_vi', '')} — setup {bref(g.get('setup'))} → payoff {bref(g.get('payoff'))}"
            for g in data.get("gags") or []] or ["- (không có)"]
    if data.get("dialogue_doubts"):
        out += ["", "## Lời thoại nghi nhận dạng sai", ""]
        out += [f"- {d.get('line_id')}「{lines.get(d.get('line_id'), {}).get('text_ja', '')}」: {d.get('issue_vi', '')}"
                for d in data["dialogue_doubts"]]
    if data.get("evidence_requests"):
        reqs = data["evidence_requests"]
        out += ["", "## Claude cần xem thêm", ""]
        out += [f"- {mmss(_num(r.get('start_seconds')) or 0)}–{mmss(_num(r.get('end_seconds')) or 0)}: {r.get('reason_vi', '')}" for r in reqs]
        ranges = ", ".join(f"{_num(r.get('start_seconds')) or 0:.1f}-{_num(r.get('end_seconds')) or 0:.1f}" for r in reqs)
        out += ["", f"Xuất thêm khung hình: chạy `9_XUAT_GOI_CHO_CLAUDE.bat`, chọn 3, dán: `{ranges}`"]
    out += ["", "## Duyệt", "",
            ("Nếu đúng với video: chạy `10_NHAP_KET_QUA_CLAUDE.bat` → chọn 2 (DUYỆT). Nếu sai: nói cho Claude chỗ sai, "
            "nhận JSON mới và nhập lại (chọn 1).")]
    return "\n".join(out) + "\n"


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def import_understanding(packet_dir: Path, text: str) -> dict:
    packet_dir = Path(packet_dir)
    packet = _load_packet(packet_dir)
    index = load_index(Path(packet["run_dir"]))
    data = extract_json(text)
    path = packet_dir / UNDERSTANDING_FILE
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result = validate_understanding(data, packet, index)
    (packet_dir / "hieu_phim.md").write_text(render_understanding_md(data, packet, index, result), encoding="utf-8")
    approval = packet_dir / APPROVAL_FILE
    if approval.is_file():
        approval.unlink()  # a new understanding needs a new approval
    return result


def approve_understanding(packet_dir: Path, *, note: str = "") -> dict:
    packet_dir = Path(packet_dir)
    packet = _load_packet(packet_dir)
    path = packet_dir / UNDERSTANDING_FILE
    if not path.is_file():
        raise PacketError("chưa nhập hiểu phim (hieu_phim.json)")
    result = validate_understanding(json.loads(path.read_text(encoding="utf-8")), packet, load_index(Path(packet["run_dir"])))
    if result["status"] != "OK":
        raise PacketError("hiểu phim còn lỗi kiểm tra tự động — sửa trước khi duyệt:\n" + "\n".join(result["errors"][:10]))
    record = {"approved_at": datetime.now().astimezone().isoformat(timespec="seconds"), "sha256": _sha256(path), "note": note}
    (packet_dir / APPROVAL_FILE).write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return record


def _approved_understanding(packet_dir: Path) -> dict:
    approval, path = packet_dir / APPROVAL_FILE, packet_dir / UNDERSTANDING_FILE
    if not approval.is_file() or not path.is_file():
        raise PacketError("phần hiểu phim chưa được bạn duyệt — chạy bước DUYỆT trước khi nhập kịch bản")
    if json.loads(approval.read_text(encoding="utf-8")).get("sha256") != _sha256(path):
        raise PacketError("hieu_phim.json đã thay đổi sau khi duyệt — duyệt lại")
    return json.loads(path.read_text(encoding="utf-8"))


# ── script → anime_recap_plan + cut list ─────────────────────────────────


def build_plan(script: dict, packet: dict, index: dict) -> dict:
    """Fill the mechanical fields (ids, frames, timeline, narration) so Claude only writes the story."""
    fps = float(index["video"]["fps"])
    segments = []
    cursor = 0.0
    for i, seg in enumerate(script.get("segments") or []):
        seg = dict(seg)
        s, e = _num(seg.get("source_start_seconds")), _num(seg.get("source_end_seconds"))
        if s is None or e is None or e <= s:
            raise PacketError(f"segments[{i}]: source_start_seconds/source_end_seconds không hợp lệ")
        s, e = seconds_to_frame(s, fps) / fps, seconds_to_frame(e, fps) / fps  # on the frame grid
        seg["source_start_seconds"], seg["source_end_seconds"] = round(s, 6), round(e, 6)
        seg.setdefault("segment_id", f"s{i + 1:02d}")
        if not seg.get("scene_ids"):
            seg["scene_ids"] = [sc["scene_id"] for sc in index["scenes"] if _overlaps(sc["start_seconds"], sc["end_seconds"], s, e)]
        seg["cut"] = {"in_frame": seconds_to_frame(s, fps), "out_frame": seconds_to_frame(e, fps)}
        seg["timeline_start_seconds"] = round(cursor, 6)
        cursor += e - s
        seg["timeline_end_seconds"] = round(cursor, 6)
        claims = [dict(c) for c in seg.get("claims") or [] if isinstance(c, dict)]
        for k, c in enumerate(claims):
            c.setdefault("claim_id", f"{seg['segment_id']}c{k + 1}")
            c.setdefault("evidence", [])
            for ev in c["evidence"]:
                if isinstance(ev, dict) and not ev.get("scene_id") and _num(ev.get("start_seconds")) is not None:
                    ev["scene_id"] = scene_at(index, ev["start_seconds"])
        seg["claims"] = claims
        text_ja = "".join(str(c.get("text_ja", "")) for c in claims)
        seg["narration"] = {"text_ja": text_ja, "text_vi": " ".join(str(c.get("text_vi", "")) for c in claims).strip(),
                            "duration_seconds": max(0.01, estimate_ja_speech_seconds(text_ja)), "duration_source": "estimate"}
        lines = {ln["line_id"]: ln for ln in all_lines(index)}
        seg["dialogue_used"] = [{"line_id": d.get("line_id"), "text_ja": lines.get(d.get("line_id"), {}).get("text_ja", "")}
                                for d in seg.get("dialogue_used") or [] if isinstance(d, dict)]
        segments.append(seg)
    return {"version": "1.0", "source_index_id": index["source_index_id"], "language": "ja", "fps": fps,
            "title_ja": script.get("title_ja", ""), "target_duration_seconds": {"min": 60, "max": 90},
            "segments": segments, "skills_applied": script.get("skills_applied") or []}


def check_against_understanding(plan: dict, understanding: dict, packet: dict) -> list[str]:
    errors = []
    beats = {b.get("beat_id"): b for b in understanding.get("beats") or [] if isinstance(b, dict)}
    s0, s1 = packet["scope"]["start_seconds"], packet["scope"]["end_seconds"]
    for seg in plan["segments"]:
        sid = seg["segment_id"]
        if seg["source_start_seconds"] < s0 - 1e-3 or seg["source_end_seconds"] > s1 + 1e-3:
            errors.append(f"{sid}: cut {mmss(seg['source_start_seconds'])}–{mmss(seg['source_end_seconds'])} nằm ngoài phạm vi đã phân tích "
                          f"{mmss(s0)}–{mmss(s1)}")
        if seg.get("story_role") not in STORY_ROLES:
            errors.append(f"{sid}: story_role phải là một trong {', '.join(STORY_ROLES)}")
        for c in seg["claims"]:
            if c.get("type") not in ("event", "quote"):
                continue
            ids = _ids(c.get("beat_ids"))
            if not ids:
                errors.append(f"{c['claim_id']}: câu kể sự kiện/trích dẫn phải có beat_ids từ phần hiểu phim đã duyệt")
            for bid in ids:
                b = beats.get(bid)
                if b is None:
                    errors.append(f"{c['claim_id']}: beat {bid} không có trong phần hiểu phim đã duyệt")
                elif b.get("kind") == "hypothesis":
                    errors.append(f"{c['claim_id']}: dựa trên beat {bid} là GIẢ THUYẾT — không được kể như sự thật")
    return errors


def write_cut_list(plan: dict, path: Path) -> None:
    fps = plan["fps"]
    with Path(path).open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["thu_tu", "segment_id", "story_role", "scene_ids", "source_in_seconds", "source_out_seconds", "in_frame", "out_frame",
                    "source_in_tc", "source_out_tc", "timeline_in_seconds", "timeline_out_seconds", "do_dai_cut_s",
                    "loi_ke_ja", "loi_ke_vi", "uoc_tinh_doc_s", "thoai_goc_giu_lai", "trang_thai"])
        for i, seg in enumerate(plan["segments"], start=1):
            w.writerow([i, seg["segment_id"], seg["story_role"], " ".join(seg["scene_ids"]),
                        f"{seg['source_start_seconds']:.6f}", f"{seg['source_end_seconds']:.6f}", seg["cut"]["in_frame"], seg["cut"]["out_frame"],
                        timecode(seg["cut"]["in_frame"], fps), timecode(seg["cut"]["out_frame"], fps),
                        f"{seg['timeline_start_seconds']:.6f}", f"{seg['timeline_end_seconds']:.6f}",
                        f"{seg['source_end_seconds'] - seg['source_start_seconds']:.3f}", seg["narration"]["text_ja"],
                        seg["narration"].get("text_vi", ""), seg["narration"]["duration_seconds"],
                        " ".join(d["line_id"] for d in seg.get("dialogue_used") or []), "CHO_DUYET"])


def check_cut_list(path: Path, index: dict, *, min_total: float = 60.0, max_total: float = 90.0) -> list[str]:
    """Re-read a cut_list.csv (as an editor tool would) and check it against the source index."""
    fps, duration = float(index["video"]["fps"]), float(index["video"]["duration_seconds"])
    scenes = {sc["scene_id"]: sc for sc in index["scenes"]}
    errors: list[str] = []
    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return ["cut_list rỗng"]
    cursor = 0.0
    for row in rows:
        rid = row.get("segment_id", "?")
        try:
            s, e = float(row["source_in_seconds"]), float(row["source_out_seconds"])
            fin, fout = int(row["in_frame"]), int(row["out_frame"])
            t0, t1 = float(row["timeline_in_seconds"]), float(row["timeline_out_seconds"])
        except (KeyError, ValueError):
            errors.append(f"{rid}: cột số không đọc được")
            continue
        if fin != seconds_to_frame(s, fps) or fout != seconds_to_frame(e, fps):
            errors.append(f"{rid}: frame không khớp round(giây × fps)")
        if not 0 <= s < e <= duration + 1e-6:
            errors.append(f"{rid}: thời gian nguồn ngoài video")
        if abs(t0 - cursor) > 0.5 / fps or abs((t1 - t0) - (e - s)) > 0.5 / fps:
            errors.append(f"{rid}: timeline không liền mạch")
        ids = row.get("scene_ids", "").split()
        if not ids or any(i not in scenes for i in ids):
            errors.append(f"{rid}: scene_ids không hợp lệ")
        elif s < min(scenes[i]["start_seconds"] for i in ids) - 1e-6 or e > max(scenes[i]["end_seconds"] for i in ids) + 1e-6:
            errors.append(f"{rid}: cut nằm ngoài các cảnh đã ghi")
        cursor = t1
    if not min_total <= cursor <= max_total:
        errors.append(f"tổng thời lượng {cursor:.2f} s ngoài {min_total:g}–{max_total:g} s")
    return errors


def render_script_md(plan: dict, script: dict, packet: dict, index: dict, status: str, errors: list[str], warnings: list[str]) -> str:
    fps = plan["fps"]
    lines = {ln["line_id"]: ln for ln in all_lines(index)}
    total = plan["segments"][-1]["timeline_end_seconds"] if plan["segments"] else 0
    out = [f"# Kịch bản review — {script.get('title_ja', '')}", "", f"_{script.get('title_vi', '')}_", "",
           f"**Kiểm tra tự động: {status}** · tổng {total:.1f} s · {len(plan['segments'])} segment · trạng thái: **CHỜ DUYỆT — chưa render**", ""]
    if errors:
        out += ["## ❌ Lỗi phải sửa (gửi nguyên mục này lại cho Claude)", "", *[f"- {e}" for e in errors], ""]
    if warnings:
        out += ["## ⚠ Cảnh báo", "", *[f"- {w}" for w in warnings], ""]
    if plan.get("skills_applied"):
        out += ["## Skill biên kịch đã áp dụng (Claude tự khai)", ""]
        out += [f"- **{s.get('skill')}**: {s.get('how_vi', '')}" for s in plan["skills_applied"] if isinstance(s, dict)]
        out.append("")
    for seg in plan["segments"]:
        cut_len = seg["source_end_seconds"] - seg["source_start_seconds"]
        out += [f"## {seg['segment_id']} · {seg.get('story_role')} · timeline {mmss(seg['timeline_start_seconds'])}–{mmss(seg['timeline_end_seconds'])}", "",
                (f"Nguồn: {mmss(seg['source_start_seconds'])}–{mmss(seg['source_end_seconds'])} · frame {seg['cut']['in_frame']}–{seg['cut']['out_frame']} "
                f"({timecode(seg['cut']['in_frame'], fps)}–{timecode(seg['cut']['out_frame'], fps)}) · cảnh {', '.join(seg['scene_ids'])} · "
                f"cut {cut_len:.2f} s · lời kể ước tính {seg['narration']['duration_seconds']:.2f} s"), "",
                f"> 🇯🇵 {seg['narration']['text_ja']}", ">", f"> 🇻🇳 {seg['narration'].get('text_vi', '')}", ""]
        for c in seg["claims"]:
            ev = "; ".join(f"{e.get('scene_id')} {mmss(_num(e.get('start_seconds')) or 0)}–{mmss(_num(e.get('end_seconds')) or 0)}"
                           + (f" {e['line_id']}「{lines.get(e['line_id'], {}).get('text_ja', '')}」" if e.get("line_id") else "")
                           for e in c.get("evidence") or [] if isinstance(e, dict)) or "—"
            out.append(f"- `{c.get('claim_id')}` [{c.get('type')}] {c.get('text_ja', '')} — _{c.get('text_vi', '')}_  \n"
                       f"  ước tính {estimate_ja_speech_seconds(str(c.get('text_ja', ''))):.2f} s · beat {', '.join(_ids(c.get('beat_ids'))) or '—'} · nguồn: {ev}")
        if seg.get("dialogue_used"):
            out.append("- Thoại gốc giữ lại: " + "; ".join(f"{d['line_id']}「{d['text_ja']}」" for d in seg["dialogue_used"]))
        out.append("")
    out += [("Ước tính đọc = số ký tự nói ÷ 6.5 ký tự/giây + nghỉ ở dấu câu (chưa phải giọng đọc thật). "
            "Cut list: `cut_list.csv`. Không có bước render nào được chạy.")]
    return "\n".join(out) + "\n"


def import_script(packet_dir: Path, text: str) -> dict:
    packet_dir = Path(packet_dir)
    packet = _load_packet(packet_dir)
    index = load_index(Path(packet["run_dir"]))
    understanding = _approved_understanding(packet_dir)
    script = extract_json(text)
    (packet_dir / SCRIPT_FILE).write_text(json.dumps(script, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    errors: list[str] = []
    warnings: list[str] = []
    try:
        plan = build_plan(script, packet, index)
    except PacketError as exc:
        plan, errors = None, [str(exc)]
    if plan is not None:
        if not plan["segments"]:
            errors.append("kịch bản không có segment nào")
        errors += check_against_understanding(plan, understanding, packet)
        report = validate_recap_plan(plan, index)
        errors += [f"{i.path}: {i.message}" for i in report.errors]
        warnings += [f"{i.path}: {i.message}" for i in report.warnings]
        (packet_dir / "anime_recap_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    status = "OK" if not errors else "FAIL"
    cut_path = packet_dir / CUT_LIST
    if status == "OK":
        write_cut_list(plan, cut_path)
        errors += check_cut_list(cut_path, index)
        status = "OK" if not errors else "FAIL"
    if status != "OK" and cut_path.is_file():
        cut_path.unlink()  # never leave a cut list that failed its checks
    if plan is not None:
        (packet_dir / "kich_ban.md").write_text(render_script_md(plan, script, packet, index, status, errors, warnings), encoding="utf-8")
    else:
        (packet_dir / "kich_ban.md").write_text("# Kịch bản — FAIL\n\n" + "\n".join(f"- {e}" for e in errors) + "\n", encoding="utf-8")
    return {"status": status, "errors": errors, "warnings": warnings,
            "total_seconds": plan["segments"][-1]["timeline_end_seconds"] if plan and plan["segments"] else 0.0}


# ── CLI ──────────────────────────────────────────────────────────────────


def latest_run(root: Path) -> Path | None:
    runs = [p for p in Path(root).glob("*") if (p / "anime_source_index.json").is_file()]
    return max(runs, key=lambda p: p.stat().st_mtime) if runs else None


def latest_packet(root: Path) -> Path | None:
    packets = [p.parent for p in Path(root).glob(f"*/{PACKET_DIR}/*/{PACKET_FILE}")]
    return max(packets, key=lambda p: (p / PACKET_FILE).stat().st_mtime) if packets else None


def _read_answer(packet_dir: Path, args, name: str) -> str:
    if args.file:
        return Path(args.file).read_text(encoding="utf-8-sig")
    paste = packet_dir / name
    if not paste.is_file():
        paste.write_text("", encoding="utf-8")
    print(f"\nMở {paste}\n→ Dán TOÀN BỘ câu trả lời của Claude (hoặc khối ```json), lưu (Ctrl+S), đóng Notepad.")
    if sys.platform == "win32":
        os.startfile(str(paste))
    input("Xong thì bấm Enter… ")
    return paste.read_text(encoding="utf-8-sig")


def _print_result(result: dict, md: Path) -> None:
    if sys.platform == "win32" and md.is_file():
        os.startfile(str(md))
    print(f"\nKết quả kiểm tra: {result['status']}")
    for e in result["errors"][:30]:
        print(f"  LỖI  {e}")
    for w in result["warnings"][:15]:
        print(f"  CẢNH BÁO  {w}")
    print(f"Đọc: {md}")
    if result["errors"]:
        print("→ Gửi mục '❌ Lỗi phải sửa' trong file trên cho Claude, nhận JSON mới rồi nhập lại.")


def main(argv: list[str] | None = None) -> int:
    from lib.anime_trial import OUTPUT_DIR

    parser = argparse.ArgumentParser(prog="python -m lib.anime_claude_packet")
    sub = parser.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("export", help="xuất gói dữ liệu cho Claude Pro")
    ex.add_argument("--run")
    ex.add_argument("--mode", choices=["overview", "detail"], required=True)
    ex.add_argument("--start")
    ex.add_argument("--end")
    ex.add_argument("--video")
    mo = sub.add_parser("more", help="bổ sung khung hình dày cho các khoảng Claude cần xem")
    mo.add_argument("--packet")
    mo.add_argument("--ranges", required=True)
    mo.add_argument("--fps", type=float, default=MORE["fps"])
    for name in ("understanding", "script"):
        p = sub.add_parser(name)
        p.add_argument("--packet")
        p.add_argument("--file")
        p.add_argument("--paste", action="store_true")
    ap = sub.add_parser("approve")
    ap.add_argument("--packet")
    ap.add_argument("--note", default="")
    args = parser.parse_args(argv)

    try:
        if args.command == "export":
            run_dir = Path(args.run) if args.run else latest_run(OUTPUT_DIR)
            if run_dir is None:
                raise PacketError("không tìm thấy thư mục kết quả Giai đoạn 2 trong anime_trial/ket_qua")
            start = parse_time(args.start) if args.start else None
            end = parse_time(args.end) if args.end else None
            packet_dir = export_packet(run_dir, mode=args.mode, start=start, end=end, video=Path(args.video) if args.video else None)
            prompt = "03_PROMPT_CHON_TINH_HUONG.md" if args.mode == "overview" else "03_PROMPT_HIEU_PHIM.md"
            print(f"\nTải lên Claude: 01_khung_hinh.pdf + 02_loi_thoai_va_canh.md, rồi dán nội dung {prompt}\nThư mục: {packet_dir}")
            return 0
        packet_dir = Path(args.packet) if args.packet else latest_packet(OUTPUT_DIR)
        if packet_dir is None:
            raise PacketError("chưa có gói nào — chạy bước xuất gói trước")
        print(f"Gói: {packet_dir}")
        if args.command == "more":
            pdf = add_evidence(packet_dir, parse_ranges(args.ranges), fps_sample=args.fps)
            print(f"Tải {pdf.name} lên cùng cuộc trò chuyện với Claude và nói: 'Đây là khung hình bổ sung bạn yêu cầu'.")
        elif args.command == "understanding":
            result = import_understanding(packet_dir, _read_answer(packet_dir, args, "dan_hieu_phim_vao_day.txt"))
            _print_result(result, packet_dir / "hieu_phim.md")
            return 0 if result["status"] == "OK" else 1
        elif args.command == "approve":
            print(f"Hãy chắc chắn bạn đã đọc {packet_dir / 'hieu_phim.md'} và đối chiếu với video.")
            if input("Phần hiểu phim ĐÚNG với video? Gõ DUYET để duyệt: ").strip().upper() != "DUYET":
                print("Chưa duyệt.")
                return 1
            note = args.note or input("Ghi chú (có thể bỏ trống): ").strip()
            approve_understanding(packet_dir, note=note)
            print(f"Đã duyệt. Bước tiếp: dán {packet_dir / '04_PROMPT_VIET_KICH_BAN.md'} cho Claude (cùng cuộc trò chuyện).")
        elif args.command == "script":
            result = import_script(packet_dir, _read_answer(packet_dir, args, "dan_kich_ban_vao_day.txt"))
            _print_result(result, packet_dir / "kich_ban.md")
            if result["status"] == "OK":
                print(f"Tổng {result['total_seconds']:.1f} s · cut list: {packet_dir / CUT_LIST} · CHƯA render.")
            return 0 if result["status"] == "OK" else 1
    except PacketError as exc:
        print(f"LỖI: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
