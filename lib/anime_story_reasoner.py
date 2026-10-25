"""Tier 1 — story understanding by multimodal reasoning on a LOCAL model.

Roles follow the *idea* of VideoMind (planner / grounder / verifier / answerer);
no VideoMind code or weights are used (its released checkpoints are Qwen2-VL
LoRAs). Every role is a short prompt to a local Qwen3-VL model plus
deterministic code. The first real run on #253-1 (one big JSON answer per
window, 6144 new tokens) failed: 3/4 calls hit the token limit, no claim was
grounded, and the synthesizer still wrote an invented logline. So the work is
now split into short JSON steps, each with its own thinking/answer budget:

1. **planner (code)** — groups consecutive scenes into time windows and keeps a
   running *story memory*;
2. **extractor (model, sees frames)** — at most 4 *observed* events, each with
   start/end time and evidence (frame times shown, Whisper line ids);
3. **grounder (code)** — snaps frame times to frames that were shown, keeps
   only real line ids, maps scenes; an event without evidence is dropped;
4. **reasoner (model, text only)** — causality, character goals/obstacles,
   misunderstandings, setup → payoff, *only* by referencing grounded event ids;
   anything citing an unknown or no event is dropped;
5. **verifier (model)** — re-shows the frames/lines of a claim; "unsupported"
   demotes it to a hypothesis;
6. **synthesizer (model)** — runs only if at least one grounded,
   non-hypothesis claim exists, sees only the analysed windows, and every
   item it returns must cite valid claim ids or it is removed.

No grounded claim → overall **FAIL**, no logline. Every call is logged
(duration, thinking/answer tokens, token-limit hit, parse error) and its raw
answer is saved under ``reasoner/raw/`` for debugging — raw text (including the
model's thinking) never goes into the story map.

Outputs go to ``<run>/reasoner/``: ``story_map.json``, ``ban_do_cot_truyen.md``
(Vietnamese), ``kiem_tra_ban_do_cot_truyen.csv`` (human check against the
video), ``qwen_trial_report.md/json`` (model, speed, VRAM, per-call table) and a
row in ``<ket_qua>/so_sanh_model.csv``.

CLI (inside .venv-qwen)::

    python -m lib.anime_story_reasoner run --run <run> --max-windows 1 --window-seconds 20 --max-calls 4
    python -m lib.anime_story_reasoner score --run <run>
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import unicodedata
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from lib.anime_vision_providers import prepare_image

ROLE_EXTRACTOR = "[ROLE:EXTRACTOR]"
ROLE_REASONER = "[ROLE:REASONER]"
ROLE_VERIFIER = "[ROLE:VERIFIER]"
ROLE_SYNTHESIZER = "[ROLE:SYNTHESIZER]"

# (thinking budget, answer budget) in new tokens per step. Thinking models are
# forced to close </think> at the first number; Instruct models ignore it.
BUDGETS: dict[str, tuple[int, int]] = {
    "extract": (512, 768),
    "reason": (768, 1024),
    "verify": (256, 256),
    "synthesize": (768, 1536),
}
STEP_VI = {"extract": "trích xuất", "reason": "suy luận", "verify": "kiểm chứng", "synthesize": "tổng hợp"}
MAX_EVENTS_PER_WINDOW = 4

KINDS = ("observation", "inference", "hypothesis")
KIND_LABEL_VI = {"observation": "QUAN SÁT", "inference": "SUY LUẬN", "hypothesis": "GIẢ THUYẾT"}
CATEGORY_VI = {"event": "Sự kiện", "link": "Nhân quả", "gag": "Gag (setup → payoff)", "misunderstanding": "Hiểu lầm"}
FRAME_SNAP_SECONDS = 1.0
TIME_SLACK_SECONDS = 1.0
FRAME_WIDTH = 640
CHECK_SHEET = "kiem_tra_ban_do_cot_truyen.csv"
COMPARISON = "so_sanh_model.csv"
CHECK_ANSWERS = ("DUNG", "SAI", "THIEU_BANG_CHUNG", "KHONG_CHAC")


class ReasonerError(RuntimeError):
    pass


class _CallLimitReached(Exception):
    pass


class _BackendFailed(Exception):
    pass


def mmss(seconds: float) -> str:
    t = round(max(0.0, float(seconds)), 1)
    minutes = int(t // 60)
    return f"{minutes:02d}:{t - 60 * minutes:04.1f}"


def parse_model_json(text: str) -> dict:
    """The first JSON object of the *answer* (text after the last ``</think>``)."""
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    elif text.lstrip().startswith("<think>"):
        raise ReasonerError("chỉ có phần suy nghĩ, chưa có câu trả lời (JSON bị cắt trước khi bắt đầu)")
    text = re.sub(r"```(?:json)?", "", text)
    start = text.find("{")
    if start < 0:
        raise ReasonerError("câu trả lời không có JSON")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(text[start:i + 1])
                except json.JSONDecodeError as exc:
                    raise ReasonerError(f"JSON không hợp lệ: {exc}") from exc
                if not isinstance(data, dict):
                    raise ReasonerError("JSON không phải đối tượng")
                return data
    raise ReasonerError("JSON bị cắt giữa chừng (thiếu dấu đóng — thường do chạm giới hạn token trả lời)")


# ── planner ──────────────────────────────────────────────────────────────


def build_windows(index: dict, *, max_seconds: float = 40.0, max_scenes: int = 8) -> list[dict]:
    windows: list[dict] = []
    current: list[dict] = []
    for scene in index["scenes"]:
        if current and (scene["end_seconds"] - current[0]["start_seconds"] > max_seconds or len(current) >= max_scenes):
            windows.append(current)
            current = []
        current.append(scene)
    if current:
        windows.append(current)
    return [
        {"window_id": f"w{i + 1:02d}", "scene_ids": [s["scene_id"] for s in group],
         "start_seconds": group[0]["start_seconds"], "end_seconds": group[-1]["end_seconds"]}
        for i, group in enumerate(windows)
    ]


def _scene_at(index: dict, t: float) -> str:
    for scene in index["scenes"]:
        if scene["start_seconds"] <= t < scene["end_seconds"]:
            return scene["scene_id"]
    return index["scenes"][-1]["scene_id"]


def _extract(video: Path, times: list[float], out_dir: Path) -> list[dict]:
    """Frames at exact source times via OpenMontage's frame_sampler, resized for the model."""
    from tools.analysis.frame_sampler import FrameSampler

    if not times:
        return []
    result = FrameSampler().execute({"input_path": str(video), "strategy": "timestamps", "timestamps": times,
                                     "output_dir": str(out_dir), "format": "jpg", "quality": 3})
    if not result.success:
        raise ReasonerError(f"frame_sampler failed: {result.error}")
    return [{"t": float(f["timestamp_seconds"]), "path": str(prepare_image(Path(f["path"]), out_dir, max_width=FRAME_WIDTH))}
            for f in result.data.get("frames", [])]


def _evenly(items: list, n: int) -> list:
    if len(items) <= n:
        return items
    step = (len(items) - 1) / (n - 1)
    return [items[round(i * step)] for i in range(n)]


def sample_window_frames(index: dict, window: dict, frames_dir: Path, *, interval: float = 1.0, max_frames: int = 16) -> list[dict]:
    """Time-ordered frames for a window: Phase 2 keyframes plus a regular grid."""
    t0, t1 = window["start_seconds"], window["end_seconds"]
    keyframes = [
        {"t": kf["timestamp_seconds"], "src": kf["path"], "source": "keyframe"}
        for scene in index["scenes"] if scene["scene_id"] in window["scene_ids"]
        for kf in scene.get("keyframes", []) if Path(kf["path"]).is_file()
    ]
    grid, t = [], t0 + interval / 2
    while t < t1:
        if all(abs(t - k["t"]) > 0.4 for k in keyframes):
            grid.append({"t": round(t, 3), "source": "grid"})
        t += interval
    chosen = _evenly(sorted(keyframes + grid, key=lambda f: f["t"]), max_frames)
    video = Path(index.get("video", {}).get("path", ""))
    wanted = [f["t"] for f in chosen if f["source"] == "grid"] if video.is_file() else []
    extracted = {round(f["t"], 3): f["path"] for f in _extract(video, wanted, Path(frames_dir) / f"{window['window_id']}_grid")}
    frames = []
    for f in chosen:
        if f["source"] == "keyframe":
            path = str(prepare_image(Path(f["src"]), Path(frames_dir) / "keyframes", max_width=FRAME_WIDTH))
        else:
            path = extracted.get(round(f["t"], 3))
            if not path:
                continue
        frames.append({"t": round(f["t"], 3), "scene_id": _scene_at(index, f["t"]), "path": path, "source": f["source"]})
    return frames


def window_lines(index: dict, window: dict) -> list[dict]:
    return [
        {"line_id": line["line_id"], "start": line["start_seconds"], "end": line["end_seconds"],
         "speaker": line.get("speaker"), "text_ja": line["text_ja"], "conf": line.get("asr_confidence"),
         "flags": line.get("asr_flags", [])}
        for scene in index["scenes"] if scene["scene_id"] in window["scene_ids"] for line in scene.get("dialogue", [])
    ]


# ── prompts ──────────────────────────────────────────────────────────────

EXTRACTOR_SYSTEM = ROLE_EXTRACTOR + """ Bạn xem một đoạn phim hoạt hình Crayon Shin-chan dưới dạng chuỗi khung hình
theo thời gian: trước mỗi ảnh có nhãn [t=giây cảnh] là thời điểm thật trong phim. Kèm theo là lời thoại tiếng Nhật
do Whisper nhận dạng (có thể sai).
Việc DUY NHẤT: liệt kê 2–4 SỰ KIỆN NHÌN THẤY hoặc NGHE THẤY TRỰC TIẾP, theo thứ tự thời gian.
- Chỉ mô tả điều có trong ảnh/lời thoại. KHÔNG đoán động cơ, KHÔNG thêm người/đồ vật không thấy.
- Gọi nhân vật theo ngoại hình nếu không chắc tên (ví dụ "cậu bé áo đỏ").
- evidence.frame_times phải là các mốc t đã cho; evidence.line_ids là id lời thoại có thật.
- what_vi: một câu tiếng Việt ngắn (≤ 25 từ).
- Nghĩ ngắn gọn. Không chắc gì thì trả {"events": []}.
Chỉ trả về MỘT đối tượng JSON, không thêm chữ nào khác."""

EXTRACTOR_TASK = """Khuôn JSON:
{"events": [{"id": "e1", "start_seconds": 0.0, "end_seconds": 0.0, "who": ["..."], "what_vi": "...",
  "evidence": {"frame_times": [0.0], "line_ids": ["sc001_l01"]}}]}"""

REASONER_SYSTEM = ROLE_REASONER + """ Bạn phân tích diễn biến một đoạn phim Crayon Shin-chan CHỈ từ danh sách sự kiện
đã được kiểm tra bằng chứng (mỗi sự kiện có id) và lời thoại. Không thấy ảnh ở bước này.
Hãy tìm: nhân quả giữa các sự kiện; nhân vật muốn gì, vấp phải gì, sai ở đâu, phản ứng ra sao; hiểu lầm;
setup → payoff của tình huống hài và vì sao buồn cười.
- MỌI mục phải tham chiếu id sự kiện có trong danh sách. Không có sự kiện làm căn cứ thì BỎ mục đó.
- kind: "inference" (suy ra từ bằng chứng) hoặc "hypothesis" (phỏng đoán).
- Không bịa nhân vật, đồ vật hay sự kiện mới. Viết *_vi bằng tiếng Việt, ngắn.
- Nghĩ ngắn gọn. Chỉ trả về MỘT đối tượng JSON."""

REASONER_TASK = """Khuôn JSON (mảng có thể rỗng):
{"links": [{"cause": "w01.e1", "effect": "w01.e2", "explanation_vi": "", "kind": "inference"}],
 "character_states": [{"character": "", "goal_vi": "", "obstacle_vi": "", "mistake_vi": "", "reaction_vi": "", "events": ["w01.e1"]}],
 "misunderstandings": [{"who_vi": "", "believes_vi": "", "truth_vi": "", "events": ["w01.e2"], "kind": "inference"}],
 "gags": [{"setup": "w01.e1", "payoff": "w01.e2", "mechanism": "incongruity|escalation|dramatic_irony|character_flaw|callback|misunderstanding|reversal|other",
   "why_funny_vi": "", "kind": "inference"}],
 "memory_update": {"story_so_far_vi": "", "characters": [{"character": "", "goal_vi": "", "state_vi": ""}],
   "open_setups": [{"event": "w01.e1", "desc_vi": ""}]}}
payoff có thể để "" nếu chưa xảy ra (ghi vào memory_update.open_setups)."""

VERIFIER_SYSTEM = ROLE_VERIFIER + """ Bạn là người kiểm chứng. Chỉ dựa vào các khung hình (có nhãn thời gian) và lời
thoại được đưa, xét xem KẾT LUẬN có được bằng chứng ủng hộ không. Mô tả rất ngắn điều thực sự thấy (tiếng Việt).
Nghĩ ngắn gọn. Chỉ trả JSON: {"verdict": "supported|partly|unsupported", "seen_vi": "..."}"""

SYNTHESIZER_SYSTEM = ROLE_SYNTHESIZER + """ Bạn tổng hợp cốt truyện CHỈ trong phạm vi được nêu, từ các kết luận đã gắn
bằng chứng (claim_id đứng đầu mỗi dòng). Không có claim_id thì không được viết.
- Mọi mục phải có claim_ids hợp lệ trong danh sách; mục không có sẽ bị xoá.
- Không thêm nhân vật, đồ vật, sự kiện nào không nằm trong các kết luận.
- GIẢ THUYẾT chỉ được nêu là chưa xác minh. Viết tiếng Việt, ngắn.
- Nghĩ ngắn gọn. Chỉ trả JSON:
{"logline_vi": "", "logline_claim_ids": [],
 "main_characters": [{"character": "", "want_vi": "", "obstacle_vi": "", "claim_ids": []}],
 "causal_chain": [{"step": 1, "claim_ids": [], "explanation_vi": ""}],
 "gags": [{"setup_claim_ids": [], "payoff_claim_ids": [], "mechanism": "", "why_funny_vi": ""}],
 "conflicts": [{"claim_ids": [], "desc_vi": ""}], "turning_points": [{"claim_ids": [], "desc_vi": ""}],
 "why_watchable_vi": "", "why_watchable_claim_ids": []}"""


def _frame_parts(frames: list[dict]) -> list[dict]:
    parts: list[dict] = []
    for f in frames:
        parts += [{"text": f"[t={f['t']:.3f}s {f['scene_id']}]"}, {"image": Path(f["path"])}]
    return parts


def _window_meta(window: dict, lines: list[dict]) -> str:
    meta = {"window_id": window["window_id"], "start_seconds": window["start_seconds"],
            "end_seconds": window["end_seconds"], "scene_ids": window["scene_ids"], "lines": lines}
    return "WINDOW_JSON: " + json.dumps(meta, ensure_ascii=False)


def extractor_parts(window: dict, frames: list[dict], lines: list[dict], memory: dict) -> list[dict]:
    return [
        {"text": _window_meta(window, lines)},
        {"text": "STORY_MEMORY: " + json.dumps(memory, ensure_ascii=False)},
        {"text": f"Khung hình theo thời gian ({mmss(window['start_seconds'])}–{mmss(window['end_seconds'])}):"},
        *_frame_parts(frames),
        {"text": EXTRACTOR_TASK},
    ]


def reasoner_parts(window: dict, lines: list[dict], memory: dict, events: list[dict], earlier: list[dict]) -> list[dict]:
    def row(c: dict) -> str:
        return f"{c['claim_id']} {mmss(c['start_seconds'])}-{mmss(c['end_seconds'])} {', '.join(c.get('who') or [])}: {c['text_vi']}"

    return [
        {"text": _window_meta(window, lines)},
        {"text": "STORY_MEMORY: " + json.dumps(memory, ensure_ascii=False)},
        {"text": "SỰ KIỆN ĐÃ CÓ BẰNG CHỨNG (đoạn này):\n" + "\n".join(row(c) for c in events)},
        {"text": "SỰ KIỆN CÁC ĐOẠN TRƯỚC (có thể dùng làm setup):\n" + ("\n".join(row(c) for c in earlier[-8:]) or "(không có)")},
        {"text": REASONER_TASK},
    ]


# ── grounder ─────────────────────────────────────────────────────────────


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _scenes_for(scenes: list[dict], allowed: list[str], s: float, e: float) -> list[str]:
    inside = [sc for sc in scenes if sc["scene_id"] in allowed]
    return ([sc["scene_id"] for sc in inside if sc["start_seconds"] <= e and s < sc["end_seconds"]]
            or [sc["scene_id"] for sc in inside if sc["start_seconds"] <= s < sc["end_seconds"]]
            or [allowed[-1]])


def ground_events(analysis: dict, window: dict, frames: list[dict], line_ids: set[str], scenes: list[dict]) -> list[dict]:
    """Observed events with real evidence, at most MAX_EVENTS_PER_WINDOW; the rest is dropped."""
    w0, w1 = window["start_seconds"], window["end_seconds"]
    claims: list[dict] = []
    for ev in analysis.get("events") or []:
        if len(claims) >= MAX_EVENTS_PER_WINDOW:
            break
        if not isinstance(ev, dict) or not str(ev.get("what_vi") or "").strip():
            continue
        evidence = ev.get("evidence") if isinstance(ev.get("evidence"), dict) else {}
        snapped: list[float] = []
        for ft in evidence.get("frame_times") or []:
            ft = _num(ft)
            nearest = min(frames, key=lambda f: abs(f["t"] - ft), default=None) if ft is not None else None
            if nearest is not None and abs(nearest["t"] - ft) <= FRAME_SNAP_SECONDS and nearest["t"] not in snapped:
                snapped.append(nearest["t"])
        lines = [lid for lid in evidence.get("line_ids") or [] if isinstance(lid, str) and lid in line_ids]
        if not snapped and not lines:
            continue
        s, e = _num(ev.get("start_seconds")), _num(ev.get("end_seconds"))
        if s is None or e is None:
            s, e = (min(snapped), max(snapped)) if snapped else (w0, w1)
        if s < w0 - TIME_SLACK_SECONDS or e > w1 + TIME_SLACK_SECONDS:
            continue
        s = min(max(s, w0), w1)
        e = min(max(e, s), w1)
        who = [str(x)[:40] for x in ev.get("who") or [] if isinstance(x, str)][:4]
        claims.append({
            "claim_id": f"{window['window_id']}.e{len(claims) + 1}", "category": "event", "kind": "observation",
            "start_seconds": round(s, 3), "end_seconds": round(e, 3),
            "scene_ids": _scenes_for(scenes, window["scene_ids"], s, e), "who": who,
            "text_vi": str(ev["what_vi"]).strip()[:300], "evidence": {"frame_times": sorted(snapped), "line_ids": lines},
            "refs": [], "notes": [],
        })
    return claims


def _ref_claim(claim_id: str, category: str, text_vi: str, refs: list[str], events: dict, kind: Any, extra: dict | None = None) -> dict:
    cited = [events[r] for r in refs]
    frames = sorted({t for c in cited for t in (c.get("evidence") or {}).get("frame_times", [])})
    lines = list(dict.fromkeys(lid for c in cited for lid in (c.get("evidence") or {}).get("line_ids", [])))
    claim = {
        "claim_id": claim_id, "category": category, "kind": "hypothesis" if kind == "hypothesis" else "inference",
        "start_seconds": min(c["start_seconds"] for c in cited), "end_seconds": max(c["end_seconds"] for c in cited),
        "scene_ids": list(dict.fromkeys(s for c in cited for s in c["scene_ids"])),
        "text_vi": text_vi.strip()[:400], "evidence": {"frame_times": frames, "line_ids": lines}, "refs": refs, "notes": [],
    }
    if extra:
        claim.update(extra)
    return claim


def _refs(values: list, events: dict) -> list[str] | None:
    """Referenced event ids, or None when any is unknown (invented) or none is given."""
    ids = [v for v in values if isinstance(v, str) and v.strip()]
    if not ids or any(v not in events for v in ids):
        return None
    return list(dict.fromkeys(ids))


def ground_reasoning(reasoning: dict, window_id: str, events: dict) -> tuple[list[dict], int]:
    """Reasoning claims that only reference grounded event ids; everything else is dropped and counted."""
    claims: list[dict] = []
    dropped = 0
    counters = {"c": 0, "g": 0, "m": 0}

    def new_id(prefix: str) -> str:
        counters[prefix] += 1
        return f"{window_id}.{prefix}{counters[prefix]}"

    for link in reasoning.get("links") or []:
        refs = _refs([link.get("cause"), link.get("effect")], events) if isinstance(link, dict) else None
        if refs is None or len(refs) < 2 or not str(link.get("explanation_vi") or "").strip():
            dropped += 1
            continue
        claims.append(_ref_claim(new_id("c"), "link", str(link["explanation_vi"]), refs, events, link.get("kind")))
    for gag in reasoning.get("gags") or []:
        refs = _refs([gag.get("setup"), gag.get("payoff")], events) if isinstance(gag, dict) else None
        if refs is None:
            dropped += 1
            continue
        setup, payoff = gag.get("setup") or None, gag.get("payoff") or None
        claims.append(_ref_claim(new_id("g"), "gag", str(gag.get("why_funny_vi") or ""), refs, events, gag.get("kind"),
                                 {"mechanism": str(gag.get("mechanism") or "other")[:40], "setup_ref": setup, "payoff_ref": payoff,
                                  "why_funny_vi": str(gag.get("why_funny_vi") or "")[:300]}))
    for m in reasoning.get("misunderstandings") or []:
        refs = _refs(m.get("events") or [], events) if isinstance(m, dict) else None
        if refs is None:
            dropped += 1
            continue
        text = f"{m.get('who_vi', '?')} tưởng: {m.get('believes_vi', '')} — thực tế: {m.get('truth_vi', '')}"
        claims.append(_ref_claim(new_id("m"), "misunderstanding", text, refs, events, m.get("kind")))
    return claims, dropped


def ground_character_states(reasoning: dict, events: dict) -> list[dict]:
    states = []
    for st in reasoning.get("character_states") or []:
        refs = _refs(st.get("events") or [], events) if isinstance(st, dict) else None
        if refs is None or not st.get("character"):
            continue
        states.append({k: str(st.get(k) or "")[:200] for k in ("character", "goal_vi", "obstacle_vi", "mistake_vi", "reaction_vi")}
                      | {"events": refs})
    return states


# ── run ──────────────────────────────────────────────────────────────────


class _Session:
    """Counts and logs every model call; saves each raw answer for debugging."""

    def __init__(self, out: Path, max_calls: int | None, progress: Callable[[str], None]):
        self.out, self.max_calls, self.progress = out, max_calls, progress
        self.calls: list[dict] = []
        (out / "raw").mkdir(parents=True, exist_ok=True)

    def call(self, backend: Any, step: str, system: str, parts: list[dict], *, window_id: str | None = None,
             claim_id: str | None = None) -> tuple[dict | None, dict]:
        if self.max_calls is not None and len(self.calls) >= self.max_calls:
            raise _CallLimitReached()
        n = len(self.calls) + 1
        think, answer = BUDGETS[step]
        images = sum(1 for p in parts if "image" in p)
        target = claim_id or window_id or "tong_hop"
        self.progress(f"  [{target}] bước {STEP_VI[step]} · lượt gọi {n}" + (f"/{self.max_calls}" if self.max_calls else "")
                      + f" · {images} frame · ngân sách token suy nghĩ {think} / trả lời {answer}")
        record: dict = {"n": n, "step": step, "window_id": window_id, "claim_id": claim_id, "images": images,
                        "think_budget": think, "answer_budget": answer, "parse_error": None, "raw_file": None,
                        "thinking_tokens": None, "answer_tokens": None, "hit_max_new_tokens": None, "seconds": None}
        self.calls.append(record)
        started = time.perf_counter()
        try:
            text, stats = backend.chat(system, parts, think_budget=think, answer_budget=answer,
                                       on_progress=self.progress, label=f"{target} {STEP_VI[step]}")
        except Exception as exc:  # model load / CUDA OOM: stop the run, keep the report
            record.update({"seconds": round(time.perf_counter() - started, 2), "error": f"{type(exc).__name__}: {exc}"})
            self.progress(f"  LỖI backend: {exc}")
            raise _BackendFailed(str(exc)) from exc
        record.update(stats)
        record["seconds"] = stats.get("seconds", round(time.perf_counter() - started, 2))
        raw_name = f"raw/{n:03d}_{step}_{target}.txt"
        (self.out / raw_name).write_text(text, encoding="utf-8")
        record["raw_file"] = raw_name
        try:
            data = parse_model_json(text)
        except ReasonerError as exc:
            data = None
            record["parse_error"] = str(exc) + (" — đã chạm giới hạn token" if stats.get("hit_max_new_tokens") else "")
        self.progress(f"    xong {record['seconds']}s · token suy nghĩ {stats.get('thinking_tokens')} · token trả lời "
                      f"{stats.get('answer_tokens')} · chạm giới hạn: {'CÓ' if stats.get('hit_max_new_tokens') else 'không'}"
                      + (f" · LỖI JSON: {record['parse_error']}" if record["parse_error"] else ""))
        return data, record


def _verify(session: _Session, backend: Any, claim: dict, frames: list[dict], lines: list[dict]) -> None:
    s, e = claim["start_seconds"] - 0.5, claim["end_seconds"] + 0.5
    wanted = set(claim["evidence"]["frame_times"])
    shown = [f for f in frames if f["t"] in wanted or s <= f["t"] <= e][:6] \
        or sorted(frames, key=lambda f: abs(f["t"] - claim["start_seconds"]))[:2]
    near = [line for line in lines if line["line_id"] in claim["evidence"]["line_ids"] or (line["start"] <= e and s <= line["end"])]
    parts = [{"text": f"KẾT LUẬN ({KIND_LABEL_VI[claim['kind']]}, {mmss(claim['start_seconds'])}–{mmss(claim['end_seconds'])}): "
                      f"{claim['text_vi']}"},
             {"text": "Lời thoại: " + json.dumps(near, ensure_ascii=False)},
             *_frame_parts(shown),
             {"text": 'Chỉ trả JSON {"verdict": "supported|partly|unsupported", "seen_vi": "..."}'}]
    result, _ = session.call(backend, "verify", VERIFIER_SYSTEM, parts, window_id=claim["claim_id"].split(".")[0],
                             claim_id=claim["claim_id"])
    verdict = (result or {}).get("verdict")
    verdict = verdict if verdict in ("supported", "partly", "unsupported") else "error"
    claim["verifier"] = {"verdict": verdict, "seen_vi": str((result or {}).get("seen_vi", ""))[:300]}
    if verdict == "unsupported":
        claim["kind"] = "hypothesis"
        claim["notes"] = sorted(set(claim["notes"]) | {"verifier_rejected"})


def _claim_row(c: dict) -> str:
    return (f"{c['claim_id']} [{KIND_LABEL_VI[c['kind']]}] {mmss(c['start_seconds'])}-{mmss(c['end_seconds'])} "
            f"{CATEGORY_VI.get(c['category'], c['category'])}: {c['text_vi']}"
            + (f" (refs: {', '.join(c['refs'])})" if c.get("refs") else ""))


def clean_synthesis(synthesis: dict, claims: dict) -> dict:
    """Keep only items that cite valid claim ids; count — but never keep — the rest."""
    removed = 0

    def valid(ids: Any) -> list[str]:
        return [i for i in ids if isinstance(i, str) and i in claims] if isinstance(ids, list) else []

    out: dict = {}
    logline_ids = valid(synthesis.get("logline_claim_ids"))
    if logline_ids and str(synthesis.get("logline_vi") or "").strip():
        out["logline_vi"], out["logline_claim_ids"] = str(synthesis["logline_vi"]).strip(), logline_ids
    elif synthesis.get("logline_vi"):
        removed += 1
    for key in ("main_characters", "causal_chain", "conflicts", "turning_points"):
        kept = []
        for item in synthesis.get(key) or []:
            ids = valid(item.get("claim_ids")) if isinstance(item, dict) else []
            if ids:
                kept.append({**item, "claim_ids": ids, "chua_xac_minh": all(claims[i]["kind"] == "hypothesis" for i in ids)})
            else:
                removed += 1
        out[key] = kept
    gags = []
    for gag in synthesis.get("gags") or []:
        if not isinstance(gag, dict):
            removed += 1
            continue
        setup, payoff = gag.get("setup_claim_ids") or [], gag.get("payoff_claim_ids") or []
        if (setup or payoff) and len(valid(setup)) == len(setup) and len(valid(payoff)) == len(payoff):
            gags.append({**gag, "setup_claim_ids": setup, "payoff_claim_ids": payoff})
        else:
            removed += 1
    out["gags"] = gags
    watch_ids = valid(synthesis.get("why_watchable_claim_ids"))
    if watch_ids and str(synthesis.get("why_watchable_vi") or "").strip():
        out["why_watchable_vi"], out["why_watchable_claim_ids"] = str(synthesis["why_watchable_vi"]).strip(), watch_ids
    elif synthesis.get("why_watchable_vi"):
        removed += 1
    has_content = bool(out.get("logline_vi") or any(out[k] for k in ("main_characters", "causal_chain", "conflicts",
                                                                       "turning_points", "gags")))
    out["removed_items"] = removed
    out["status"] = ("ok" if out.get("logline_vi") else "partial") if has_content else "failed_no_grounded_content"
    return out


def _synthesize(session: _Session, backend: Any, windows: list[dict], scope: dict) -> dict:
    claims = {c["claim_id"]: c for w in windows for c in w["claims"]}
    if not any(c["kind"] != "hypothesis" for c in claims.values()):
        return {"status": "skipped_no_grounded_claims", "removed_items": 0}
    if scope["kind"] == "pilot":
        scope_text = (f"PHẠM VI: THỬ NGHIỆM — chỉ {len(scope['analysed_windows'])}/{scope['total_windows']} đoạn, "
                      f"{mmss(scope['start_seconds'])}–{mmss(scope['end_seconds'])}. KHÔNG tóm tắt cả tập, chỉ phần này.")
    else:
        scope_text = f"PHẠM VI: cả tập, {scope['total_windows']} đoạn."
    parts = [{"text": scope_text},
             {"text": "CÁC KẾT LUẬN (claim_id đứng đầu dòng):\n" + "\n".join(_claim_row(c) for c in claims.values())},
             {"text": "Tổng hợp trong phạm vi trên. Chỉ trả JSON."}]
    data, record = session.call(backend, "synthesize", SYNTHESIZER_SYSTEM, parts)
    if data is None:
        return {"status": "failed_parse", "error": record["parse_error"], "removed_items": 0}
    return clean_synthesis(data, claims)


def run_reasoner(
    run_dir: Path,
    backend: Any,
    *,
    extract_backend: Any = None,
    max_seconds: float = 40.0,
    max_scenes: int = 8,
    frames_per_window: int = 12,
    frame_interval: float = 1.0,
    verify_limit: int = 2,
    max_windows: int | None = None,
    max_calls: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict:
    run_dir = Path(run_dir)
    index = json.loads((run_dir / "anime_source_index.json").read_text(encoding="utf-8"))
    out = run_dir / "reasoner"
    frames_dir = out / "frames"
    out.mkdir(parents=True, exist_ok=True)
    progress = progress or (lambda msg: print(msg, flush=True))
    extract_backend = extract_backend or backend
    session = _Session(out, max_calls, progress)
    started = time.perf_counter()
    stats = {"stopped_by_call_limit": False, "stopped_by_error": None, "dropped_reasoning_items": 0, "verified": 0}
    memory: dict = {"story_so_far_vi": "", "characters": [], "open_setups": []}
    scenes = [{k: s[k] for k in ("scene_id", "start_seconds", "end_seconds")} for s in index["scenes"]]
    line_texts = {line["line_id"]: line["text_ja"] for s in index["scenes"] for line in s.get("dialogue", [])}
    all_events: dict[str, dict] = {}
    windows_out: list[dict] = []

    windows = build_windows(index, max_seconds=max_seconds, max_scenes=max_scenes)
    planned = windows[:max_windows] if max_windows else windows
    for window in planned:
        wid = window["window_id"]
        frames = sample_window_frames(index, window, frames_dir, interval=frame_interval, max_frames=frames_per_window)
        lines = window_lines(index, window)
        progress(f"[{wid}] {mmss(window['start_seconds'])}–{mmss(window['end_seconds'])} · {len(frames)} frame · "
                 f"{len(lines)} câu thoại · cảnh {', '.join(window['scene_ids'])}")
        record = {**window, "frames": [{k: f[k] for k in ("t", "scene_id", "source", "path")} for f in frames],
                  "status": "failed", "error": None, "claims": [], "character_states": []}
        attempted = False
        try:
            if max_calls is not None and len(session.calls) >= max_calls:
                raise _CallLimitReached()
            attempted = True
            analysis, call = session.call(extract_backend, "extract", EXTRACTOR_SYSTEM,
                                          extractor_parts(window, frames, lines, memory), window_id=wid)
            if analysis is None:
                record["error"] = f"trích xuất: {call['parse_error']}"
                continue
            events = ground_events(analysis, window, frames, set(line_texts), scenes)
            for c in events:
                c["evidence"]["line_texts"] = {lid: line_texts[lid] for lid in c["evidence"]["line_ids"]}
            record["claims"] = events
            if not events:
                record.update(status="no_grounded_events", error="không có sự kiện nào có bằng chứng")
                continue
            record["status"] = "ok"
            earlier = list(all_events.values())
            all_events.update({c["claim_id"]: c for c in events})
            reasoning, call = session.call(backend, "reason", REASONER_SYSTEM,
                                           reasoner_parts(window, lines, memory, events, earlier), window_id=wid)
            if reasoning is None:
                record.update(status="events_only", error=f"suy luận: {call['parse_error']}")
            else:
                linked, dropped = ground_reasoning(reasoning, wid, all_events)
                for c in linked:
                    c["evidence"]["line_texts"] = {lid: line_texts[lid] for lid in c["evidence"]["line_ids"]}
                stats["dropped_reasoning_items"] += dropped
                record["claims"] = events + linked
                record["character_states"] = ground_character_states(reasoning, all_events)
                mem = reasoning.get("memory_update")
                if isinstance(mem, dict):
                    memory = {"story_so_far_vi": str(mem.get("story_so_far_vi", ""))[:800],
                              "characters": (mem.get("characters") or [])[:8], "open_setups": (mem.get("open_setups") or [])[:8]}
                    record["memory_after"] = memory
            order = [c for c in record["claims"] if c["kind"] == "inference"] + [c for c in record["claims"] if c["kind"] == "observation"]
            for claim in order:
                if stats["verified"] >= verify_limit:
                    break
                _verify(session, backend, claim, frames, lines)
                stats["verified"] += 1
        except _CallLimitReached:
            stats["stopped_by_call_limit"] = True
            progress(f"  Dừng: đã đạt giới hạn lượt gọi ({max_calls})")
        except _BackendFailed as exc:
            stats["stopped_by_error"] = str(exc)
            record["error"] = f"backend: {exc}"
        finally:
            if attempted:
                if record["status"] == "failed" and record["claims"]:
                    record["status"] = "ok"
                windows_out.append(record)
                valid = sum(1 for c in record["claims"] if c["kind"] != "hypothesis")
                progress(f"[{wid}] kết luận hợp lệ: {valid} (tổng {len(record['claims'])}) · trạng thái {record['status']}")
        if stats["stopped_by_call_limit"] or stats["stopped_by_error"]:
            break

    analysed = [w["window_id"] for w in windows_out]
    scope = {"kind": "full" if len(analysed) == len(windows) else "pilot", "analysed_windows": analysed,
             "total_windows": len(windows),
             "start_seconds": windows_out[0]["start_seconds"] if windows_out else 0.0,
             "end_seconds": windows_out[-1]["end_seconds"] if windows_out else 0.0}
    synthesis: dict
    try:
        if stats["stopped_by_error"]:
            synthesis = {"status": "skipped_backend_error", "removed_items": 0}
        else:
            synthesis = _synthesize(session, backend, windows_out, scope)
    except _CallLimitReached:
        stats["stopped_by_call_limit"] = True
        synthesis = {"status": "skipped_call_limit", "removed_items": 0}
    except _BackendFailed as exc:
        stats["stopped_by_error"] = str(exc)
        synthesis = {"status": "skipped_backend_error", "removed_items": 0}

    info = backend.info()  # after the calls: the GPU name is known once the model is loaded
    model = {**info, "extract_model_id": extract_backend.info().get("model_id") if extract_backend is not backend else None}
    calls = session.calls
    all_claims = [c for w in windows_out for c in w["claims"]]
    grounded = sum(1 for c in all_claims if c["kind"] != "hypothesis")
    overall = "FAIL" if grounded == 0 else "PENDING"
    story = {"version": "2.0", "source_index_id": index["source_index_id"], "scope": scope, "overall": overall,
             "provenance": {**model, "real_model": info.get("backend") != "fake",
                            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                            "videomind": "roles adapted as prompts (planner/grounder/verifier/answerer); no VideoMind code or weights"},
             "windows": windows_out, "synthesis": synthesis}
    (out / "story_map.json").write_text(json.dumps(story, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "ban_do_cot_truyen.md").write_text(render_story_map_vi(story), encoding="utf-8")
    write_check_sheet(story, out / CHECK_SHEET)

    verdicts = [c["verifier"]["verdict"] for c in all_claims if c.get("verifier")]
    tps = [c["tokens_per_second"] for c in calls if c.get("tokens_per_second")]
    vram = [c["peak_vram_mb"] for c in calls if c.get("peak_vram_mb")]
    stats.update({
        "windows": len(windows_out), "windows_total": len(windows),
        "windows_failed": sum(1 for w in windows_out if w["status"] in ("failed", "no_grounded_events")),
        "claims": len(all_claims), "grounded_claims": grounded,
        **{k: sum(1 for c in all_claims if c["kind"] == k) for k in KINDS},
        **{f"verifier_{v}": verdicts.count(v) for v in ("supported", "partly", "unsupported", "error")},
        "calls": len(calls), "hit_max_new_tokens": sum(1 for c in calls if c.get("hit_max_new_tokens")),
        "think_forced_close": sum(1 for c in calls if c.get("think_forced_close")),
        "parse_failures": sum(1 for c in calls if c.get("parse_error")),
        "total_seconds": round(time.perf_counter() - started, 1),
        "avg_tokens_per_second": round(sum(tps) / len(tps), 2) if tps else None,
        "peak_vram_mb": max(vram) if vram else None,
    })
    report = {"run": run_dir.name, "model": model, "scope": scope, "stats": stats, "calls": calls,
              "synthesis_status": synthesis["status"], "overall": overall}
    (out / "qwen_trial_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "qwen_trial_report.md").write_text(render_report_vi(report, story), encoding="utf-8")
    _upsert_comparison(run_dir, report)
    return report


# ── outputs ──────────────────────────────────────────────────────────────


def _evidence_text(claim: dict) -> str:
    texts = claim["evidence"].get("line_texts") or {}
    bits = [f"khung {', '.join(mmss(t) for t in claim['evidence']['frame_times'])}"] if claim["evidence"]["frame_times"] else []
    bits += [f"{lid}「{texts.get(lid, '')}」" for lid in claim["evidence"]["line_ids"]]
    if claim.get("refs"):
        bits.append(f"dựa trên {', '.join(claim['refs'])}")
    return "; ".join(bits) or "không có"


def _scope_line(scope: dict) -> str:
    n, total = len(scope["analysed_windows"]), scope["total_windows"]
    if scope["kind"] == "pilot":
        return (f"> ⚠ **THỬ NGHIỆM** — chỉ phân tích {n}/{total} đoạn ({mmss(scope['start_seconds'])}–"
                f"{mmss(scope['end_seconds'])}), **không phải toàn bộ tập**. Mọi tóm tắt dưới đây chỉ nói về phần này.")
    return f"> Phạm vi: toàn bộ tập, {n}/{total} đoạn."


def _ids(ids: list[str]) -> str:
    return f"[{', '.join(ids)}]"


def render_story_map_vi(story: dict) -> str:
    prov, scope = story["provenance"], story["scope"]
    out = [f"# Bản đồ cốt truyện — {story['source_index_id']}", ""]
    if prov.get("real_model"):
        extra = f" · trích xuất bằng {prov['extract_model_id']}" if prov.get("extract_model_id") else ""
        out.append(f"> Kết quả từ model thật **{prov.get('model_id')}** ({prov.get('backend')}, {prov.get('quantization')}){extra}. "
                   "Đây là suy luận của AI — **chưa được người đối chiếu với video** (xem kiem_tra_ban_do_cot_truyen.csv).")
    else:
        out.append(f"> ⚠ Chạy bằng backend `{prov.get('backend')}` — **KHÔNG phải model thật**, chỉ để kiểm thử quy trình.")
    out += [">", _scope_line(scope), ""]
    if story["overall"] == "FAIL":
        out += ["## KẾT QUẢ: FAIL", "",
                ("Model **không đưa ra được kết luận nào có bằng chứng** (khung hình đã xem / lời thoại có thật). "
                 "Vì vậy **không tổng hợp**: không có tóm tắt, xung đột hay tình huống hài nào được viết ra. "
                 "Xem `qwen_trial_report.md` (bảng từng lượt gọi) để biết vì sao."), ""]
    else:
        out += [("Nhãn: **QUAN SÁT** = thấy/nghe trực tiếp · **SUY LUẬN** = suy ra từ bằng chứng · "
                 "**GIẢ THUYẾT** = chưa xác minh (không dùng làm sự thật)."), ""]
        out += _render_synthesis(story["synthesis"], scope)
    out += ["", "## Diễn biến theo thời gian", ""]
    for w in story["windows"]:
        out += [f"### {w['window_id']} · {mmss(w['start_seconds'])}–{mmss(w['end_seconds'])} · {', '.join(w['scene_ids'])}", ""]
        if w.get("error"):
            out += [f"⚠ {w['error']}", ""]
        for c in w["claims"]:
            verdict = f" · kiểm chứng: {c['verifier']['verdict']}" if c.get("verifier") else ""
            who = f" ({', '.join(c['who'])})" if c.get("who") else ""
            mech = f" [{c['mechanism']}]" if c.get("mechanism") else ""
            out.append(f"- `{c['claim_id']}` **[{KIND_LABEL_VI[c['kind']]}]** {mmss(c['start_seconds'])}–{mmss(c['end_seconds'])} "
                       f"{CATEGORY_VI.get(c['category'], c['category'])}{mech}{who}: {c['text_vi']}"
                       f"  \n  Bằng chứng: {_evidence_text(c)}{verdict}")
        for st in w.get("character_states") or []:
            bits = [f"{label} {st[key]}" for key, label in (("goal_vi", "mục tiêu"), ("obstacle_vi", "trở ngại"),
                                                              ("mistake_vi", "sai lầm"), ("reaction_vi", "phản ứng")) if st[key]]
            out.append(f"- Nhân vật {st['character']}: {'; '.join(bits)} {_ids(st['events'])}")
        out.append("")
    return "\n".join(out) + "\n"


def _render_synthesis(syn: dict, scope: dict) -> list[str]:
    title = "## Tóm tắt phần đã phân tích (THỬ NGHIỆM)" if scope["kind"] == "pilot" else "## Tóm tắt cả tập"
    status = syn.get("status")
    if status not in ("ok", "partial"):
        reason = {"skipped_call_limit": "đã hết giới hạn lượt gọi trước bước tổng hợp",
                  "skipped_backend_error": "backend lỗi",
                  "failed_parse": f"không đọc được JSON ({syn.get('error')})",
                  "failed_no_grounded_content": "mọi mục model viết đều không dẫn claim_id hợp lệ nên đã bị xoá",
                  "skipped_no_grounded_claims": "không có kết luận có bằng chứng"}.get(status, status)
        return [title, "", f"⚠ Không có tổng hợp: {reason}. Chỉ dùng danh sách kết luận bên dưới."]
    out = [title, ""]
    if syn.get("logline_vi"):
        out += [f"**Logline:** {syn['logline_vi']} {_ids(syn['logline_claim_ids'])}", ""]
    for ch in syn.get("main_characters") or []:
        out.append(f"- **{ch.get('character')}** — muốn: {ch.get('want_vi', '')}; trở ngại: {ch.get('obstacle_vi', '')} {_ids(ch['claim_ids'])}")
    if syn.get("causal_chain"):
        out += ["", "### Chuỗi nhân quả", ""]
        for step in syn["causal_chain"]:
            flag = " _(chưa xác minh)_" if step.get("chua_xac_minh") else ""
            out.append(f"{step.get('step', '•')}. {step.get('explanation_vi', '')} — {_ids(step['claim_ids'])}{flag}")
    if syn.get("gags"):
        out += ["", "### Tình huống hài (setup → payoff)", ""]
        for gag in syn["gags"]:
            out.append(f"- {gag.get('mechanism', '')}: {gag.get('why_funny_vi', '')} — setup {_ids(gag['setup_claim_ids'])} "
                       f"→ payoff {_ids(gag['payoff_claim_ids'])}")
    for key, heading in (("conflicts", "Xung đột"), ("turning_points", "Bước ngoặt")):
        if syn.get(key):
            out += ["", f"### {heading}", ""]
            out += [f"- {item.get('desc_vi', '')} {_ids(item['claim_ids'])}" for item in syn[key]]
    if syn.get("why_watchable_vi"):
        out += ["", f"**Vì sao đáng xem:** {syn['why_watchable_vi']} {_ids(syn['why_watchable_claim_ids'])}"]
    if syn.get("removed_items"):
        out += ["", f"_Đã xoá {syn['removed_items']} mục tổng hợp không dẫn claim_id hợp lệ (không hiển thị)._"]
    return out


def write_check_sheet(story: dict, path: Path) -> None:
    with Path(path).open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["claim_id", "loai", "nhan", "bat_dau", "ket_thuc", "mo_ta_vi", "bang_chung", "verifier",
                         "ket_qua", "ghi_chu"])
        for w in story["windows"]:
            for c in w["claims"]:
                writer.writerow([c["claim_id"], CATEGORY_VI.get(c["category"], c["category"]), KIND_LABEL_VI[c["kind"]],
                                 mmss(c["start_seconds"]), mmss(c["end_seconds"]), c["text_vi"],
                                 _evidence_text(c), (c.get("verifier") or {}).get("verdict", ""), "", ""])


def _diagnosis(report: dict) -> list[str]:
    s, calls = report["stats"], report["calls"]
    out = []
    if s.get("stopped_by_call_limit"):
        out.append(f"- Đã dừng vì **giới hạn lượt gọi** ({s['calls']} lượt) — đúng như cấu hình, không phải lỗi model.")
    if s.get("stopped_by_error"):
        out.append(f"- Backend lỗi, đã dừng: {s['stopped_by_error']}")
    forced = [c for c in calls if c.get("think_forced_close")]
    if forced:
        out.append(f"- {len(forced)} lượt model **nghĩ hết ngân sách** suy nghĩ và bị buộc dừng nghĩ "
                   "(cột 'buộc dừng nghĩ'). Nếu kết quả kém, đây là nguyên nhân chính.")
    long_answers = [c for c in calls if c.get("hit_max_new_tokens") and not c.get("think_forced_close")]
    if long_answers:
        out.append(f"- {len(long_answers)} lượt **phần trả lời quá dài** so với ngân sách trả lời.")
    if s.get("grounded_claims", 0) == 0:
        out.append("- **Không có kết luận nào có bằng chứng** → FAIL, không tổng hợp, không viết logline.")
    if s.get("dropped_reasoning_items"):
        out.append(f"- Đã loại {s['dropped_reasoning_items']} mục suy luận dẫn tới sự kiện không tồn tại hoặc không dẫn gì.")
    return out or ["- Không thấy dấu hiệu chạm giới hạn token hay lỗi JSON."]


def render_report_vi(report: dict, story: dict) -> str:
    s, m, scope = report["stats"], report["model"], report["scope"]
    real = story["provenance"]["real_model"]
    status_text = ("không có kết luận nào có bằng chứng — model chưa hiểu được đoạn này" if report["overall"] == "FAIL" else
                   "chờ bạn đối chiếu bản đồ cốt truyện với video (kiem_tra_ban_do_cot_truyen.csv)")
    out = [f"# Báo cáo thử nghiệm Tầng 1 — {report['run']}", "",
           f"**Trạng thái: {report['overall']}** — {status_text}", "", _scope_line(scope), "",
           "## Model thực tế đã chạy", "",
           f"- Backend: `{m.get('backend')}` · model: `{m.get('model_id')}` · lượng tử hóa: `{m.get('quantization')}` · GPU: `{m.get('gpu')}`",
           *([f"- Model trích xuất riêng: `{m['extract_model_id']}`"] if m.get("extract_model_id") else []),
           "- " + ("Model thật, chạy cục bộ, không gửi dữ liệu lên cloud." if real else "**KHÔNG phải model thật** (backend giả để kiểm thử)."),
           "", "## Tốc độ và tài nguyên", "",
           f"- Tổng thời gian: {s['total_seconds']} s · số lượt gọi model: {s['calls']} · token/giây TB: {s['avg_tokens_per_second']}",
           (f"- VRAM đỉnh (PyTorch): {s['peak_vram_mb']} MB · chạm giới hạn token: {s['hit_max_new_tokens']} · "
            f"buộc dừng nghĩ: {s['think_forced_close']} · lỗi JSON: {s['parse_failures']}"),
           *(["- **Đã dừng vì giới hạn lượt gọi.**"] if s.get("stopped_by_call_limit") else []),
           "", "## Từng lượt gọi", "",
           "| # | bước | đoạn / kết luận | ảnh | giây | ngân sách (nghĩ/trả lời) | token suy nghĩ | token trả lời | buộc dừng nghĩ | chạm giới hạn | lỗi JSON | file thô |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for c in report["calls"]:
        out.append(f"| {c['n']} | {STEP_VI[c['step']]} | {c.get('claim_id') or c.get('window_id') or 'tổng hợp'} | {c.get('images')} | "
                   f"{c.get('seconds')} | {c['think_budget']}/{c['answer_budget']} | {c.get('thinking_tokens')} | {c.get('answer_tokens')} | "
                   f"{'có' if c.get('think_forced_close') else ''} | {'CÓ' if c.get('hit_max_new_tokens') else ''} | "
                   f"{c.get('parse_error') or c.get('error') or ''} | {c.get('raw_file') or ''} |")
    out += ["", "File thô (`raw/`) chứa nguyên văn câu trả lời, kể cả phần suy nghĩ, chỉ để gỡ lỗi — không dùng làm kịch bản.",
            "", "## Chẩn đoán", "", *_diagnosis(report),
            "", "## Kết quả suy luận (tự động — chưa phải độ chính xác)", "",
            (f"- Đoạn: {s['windows']}/{s['windows_total']} (lỗi {s['windows_failed']}) · kết luận: {s['claims']} "
             f"(có bằng chứng, không phải giả thuyết: {s['grounded_claims']}) — quan sát {s['observation']}, "
             f"suy luận {s['inference']}, giả thuyết {s['hypothesis']}"),
            (f"- Verifier: ủng hộ {s['verifier_supported']}, một phần {s['verifier_partly']}, bác bỏ {s['verifier_unsupported']}, "
             f"lỗi {s['verifier_error']} · tổng hợp: {report['synthesis_status']}"),
            "", "## Lỗi theo đoạn", ""]
    errors = [f"- {w['window_id']}: {w['error']}" for w in story["windows"] if w.get("error")]
    out += errors or ["- (không có)"]
    if report["overall"] != "FAIL":
        out += ["", "## Việc còn PENDING", "",
                ("- **PENDING** — Bạn mở video tại các mốc thời gian trong `kiem_tra_ban_do_cot_truyen.csv`, ghi DUNG / SAI / "
                 "THIEU_BANG_CHUNG / KHONG_CHAC cho từng kết luận, rồi chạy `8_CHAM_BAN_DO_COT_TRUYEN.bat`."),
                "- Chỉ chạy cả tập khi đoạn ngắn đạt; chỉ sang Tầng 2 khi độ chính xác người chấm đủ cao."]
    if report.get("score"):
        sc = report["score"]
        out += ["", "## Kết quả người chấm", "", f"- Đã chấm {sc['judged']} kết luận · chính xác {sc['accuracy']}",
                *[f"- {KIND_LABEL_VI.get(k, k)}: {v}" for k, v in sc.get("by_kind", {}).items()]]
    return "\n".join(out) + "\n"


COMPARISON_FIELDS = ["thoi_diem", "run", "backend", "model_id", "model_trich_xuat", "quantization", "gpu", "pham_vi",
                     "tong_giay", "so_luot_goi", "tb_token_moi_giay", "vram_dinh_mb", "cham_gioi_han_token", "loi_parse",
                     "so_doan", "so_ket_luan", "ket_luan_co_bang_chung", "quan_sat", "suy_luan", "gia_thuyet",
                     "verifier_ung_ho", "verifier_mot_phan", "verifier_bac_bo", "trang_thai", "do_chinh_xac_nguoi_cham"]


def _read_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _write_rows(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _upsert_comparison(run_dir: Path, report: dict, *, accuracy: float | None = None) -> None:
    path = run_dir.parent / COMPARISON
    rows = [r for r in _read_rows(path) if r.get("run") != run_dir.name]
    s, m, scope = report["stats"], report["model"], report["scope"]
    rows.append({
        "thoi_diem": datetime.now().astimezone().isoformat(timespec="seconds"), "run": run_dir.name,
        "backend": m.get("backend"), "model_id": m.get("model_id"), "model_trich_xuat": m.get("extract_model_id") or "",
        "quantization": m.get("quantization"), "gpu": m.get("gpu"),
        "pham_vi": f"{scope['kind']} {len(scope['analysed_windows'])}/{scope['total_windows']}",
        "tong_giay": s["total_seconds"], "so_luot_goi": s["calls"], "tb_token_moi_giay": s["avg_tokens_per_second"],
        "vram_dinh_mb": s["peak_vram_mb"], "cham_gioi_han_token": s["hit_max_new_tokens"], "loi_parse": s["parse_failures"],
        "so_doan": s["windows"], "so_ket_luan": s["claims"], "ket_luan_co_bang_chung": s["grounded_claims"],
        "quan_sat": s["observation"], "suy_luan": s["inference"], "gia_thuyet": s["hypothesis"],
        "verifier_ung_ho": s["verifier_supported"], "verifier_mot_phan": s["verifier_partly"],
        "verifier_bac_bo": s["verifier_unsupported"], "trang_thai": report["overall"],
        "do_chinh_xac_nguoi_cham": f"{accuracy:.3f}" if accuracy is not None else "",
    })
    _write_rows(path, COMPARISON_FIELDS, rows)


def _norm(value: str) -> str:
    value = (value or "").strip().replace("Đ", "D").replace("đ", "d")
    value = "".join(c for c in unicodedata.normalize("NFD", value) if unicodedata.category(c) != "Mn")
    return re.sub(r"[\s-]+", "_", value.upper())


def score_story_map(run_dir: Path) -> dict:
    run_dir = Path(run_dir)
    out = run_dir / "reasoner"
    rows = _read_rows(out / CHECK_SHEET)
    answers = [(r, _norm(r.get("ket_qua", ""))) for r in rows]
    judged = [(r, a) for r, a in answers if a in ("DUNG", "SAI", "THIEU_BANG_CHUNG")]
    correct = sum(1 for _, a in judged if a == "DUNG")
    by_kind: dict[str, str] = {}
    for label in KIND_LABEL_VI.values():
        subset = [a for r, a in judged if r.get("nhan") == label]
        if subset:
            by_kind[label] = f"{subset.count('DUNG')}/{len(subset)}"
    score = {"rows": len(rows), "judged": len(judged), "unsure": sum(1 for _, a in answers if a == "KHONG_CHAC"),
             "accuracy": round(correct / len(judged), 3) if judged else None, "by_kind": by_kind}
    report_path = out / "qwen_trial_report.json"
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if "scope" not in report:  # written by the old single-answer version
            return score
        report["score"] = score
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        story = json.loads((out / "story_map.json").read_text(encoding="utf-8"))
        (out / "qwen_trial_report.md").write_text(render_report_vi(report, story), encoding="utf-8")
        if score["accuracy"] is not None:
            _upsert_comparison(run_dir, report, accuracy=score["accuracy"])
    return score


def _latest_run(root: Path) -> Path | None:
    runs = [p for p in root.glob("*_*_*") if (p / "anime_source_index.json").is_file()]
    return max(runs, key=lambda p: p.stat().st_mtime) if runs else None


def main(argv: list[str] | None = None) -> int:
    from lib.anime_trial import OUTPUT_DIR

    parser = argparse.ArgumentParser(prog="python -m lib.anime_story_reasoner")
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="Tier 1: understand the episode with a local Qwen3-VL")
    r.add_argument("--run", help="Phase 2 run folder (default: newest in anime_trial/ket_qua)")
    r.add_argument("--backend", choices=["transformers", "ollama"], default="transformers")
    r.add_argument("--model", help="default: Qwen/Qwen3-VL-4B-Thinking (transformers) or qwen3-vl:4b (ollama)")
    r.add_argument("--extract-model", help="separate model for the extract step, e.g. Qwen/Qwen3-VL-4B-Instruct (extra ~9 GB download)")
    r.add_argument("--quant", choices=["nf4", "bf16"], default="nf4")
    r.add_argument("--max-windows", type=int, help="analyse only the first N windows (pilot)")
    r.add_argument("--max-calls", type=int, help="hard cap on model calls (the run stops and says so)")
    r.add_argument("--frames-per-window", type=int, default=12)
    r.add_argument("--verify-limit", type=int, default=2)
    r.add_argument("--window-seconds", type=float, default=40.0)
    sc = sub.add_parser("score", help="score the filled kiem_tra_ban_do_cot_truyen.csv")
    sc.add_argument("--run")
    args = parser.parse_args(argv)

    run_dir = Path(args.run) if args.run else _latest_run(OUTPUT_DIR)
    if run_dir is None or not (run_dir / "anime_source_index.json").is_file():
        print("Khong tim thay thu muc ket qua co anime_source_index.json")
        return 2
    if args.command == "score":
        print(json.dumps(score_story_map(run_dir), ensure_ascii=False, indent=2))
        return 0

    from lib.qwen_local_backend import (
        DEFAULT_MODEL_ID,
        DEFAULT_OLLAMA_MODEL,
        OllamaQwenBackend,
        TransformersQwenBackend,
    )

    def make(model_id: str | None):
        if args.backend == "transformers":
            return TransformersQwenBackend(model_id or DEFAULT_MODEL_ID, quant=args.quant)
        return OllamaQwenBackend(model_id or DEFAULT_OLLAMA_MODEL)

    backend = make(args.model)
    extract_backend = make(args.extract_model) if args.extract_model else None
    print(f"Tang 1 tren {run_dir} bang {backend.info()['model_id']} ({args.backend})"
          + (f", trich xuat bang {args.extract_model}" if args.extract_model else "")
          + f" · toi da {args.max_windows or 'tat ca'} doan · toi da {args.max_calls or 'khong gioi han'} luot goi", flush=True)
    report = run_reasoner(run_dir, backend, extract_backend=extract_backend, max_seconds=args.window_seconds,
                          frames_per_window=args.frames_per_window, verify_limit=args.verify_limit,
                          max_windows=args.max_windows, max_calls=args.max_calls)
    s = report["stats"]
    print(f"Trang thai: {report['overall']} · ket luan co bang chung: {s['grounded_claims']} · luot goi: {s['calls']} · "
          f"thoi gian: {s['total_seconds']} s · VRAM dinh: {s['peak_vram_mb']} MB · GPU: {report['model'].get('gpu')}")
    if s.get("stopped_by_error"):
        print(f"LOI backend: {s['stopped_by_error']}")
    print(f"Doc: {run_dir / 'reasoner' / 'ban_do_cot_truyen.md'} va qwen_trial_report.md")
    return 1 if report["overall"] == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
