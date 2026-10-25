"""Tier 1 — story understanding by multimodal reasoning on a LOCAL model.

Roles follow the *idea* of VideoMind (planner / grounder / verifier / answerer);
no VideoMind code or weights are used (its released checkpoints are Qwen2-VL
LoRAs). Every role here is a prompt to the same local Qwen3-VL model plus
deterministic code:

- **planner (code)** — groups consecutive scenes into time windows and keeps a
  running *story memory* (story so far, character goals/states, open setups);
- **answerer (model)** — reads a window's frames in time order, each frame
  preceded by its source timestamp, together with the Whisper lines, and
  returns events as cause → action → reaction → consequence, character
  goals/obstacles/mistakes, misunderstandings, escalations/reversals, gags
  (setup → payoff + why funny); every item is labelled observation /
  inference / hypothesis and cites frame times and line ids;
- **evidence requests** — the answerer may ask for a closer look at a time
  range; code extracts denser frames there and asks once more;
- **grounder (code)** — snaps cited frame times to frames that were shown,
  keeps only line ids that exist, clamps times into the window, maps them to
  scene ids, and demotes anything without evidence to a hypothesis;
- **verifier (model)** — re-shows only the frames/lines of an inference and
  asks whether they support it; rejected inferences become hypotheses;
- **synthesizer (model)** — links the grounded claims of all windows into the
  episode's causal chain, conflicts, turning points and gags; unknown ids are
  dropped, never invented.

Outputs go to ``<run>/reasoner/``: ``story_map.json``, ``ban_do_cot_truyen.md``
(Vietnamese), ``kiem_tra_ban_do_cot_truyen.csv`` (human check against the
video), ``qwen_trial_report.md/json`` (model, speed, VRAM, errors) and a row in
``<ket_qua>/so_sanh_model.csv``.

CLI (inside .venv-qwen)::

    python -m lib.anime_story_reasoner run --run anime_trial/ket_qua/<run> [--backend transformers|ollama]
    python -m lib.anime_story_reasoner score --run anime_trial/ket_qua/<run>
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from lib.anime_vision_providers import prepare_image

ROLE_ANSWERER = "[ROLE:ANSWERER]"
ROLE_VERIFIER = "[ROLE:VERIFIER]"
ROLE_SYNTHESIZER = "[ROLE:SYNTHESIZER]"

KINDS = ("observation", "inference", "hypothesis")
KIND_LABEL_VI = {"observation": "QUAN SÁT", "inference": "SUY LUẬN", "hypothesis": "GIẢ THUYẾT"}
CATEGORY_VI = {
    "event": "Sự kiện", "misunderstanding": "Hiểu lầm", "gag_setup": "Gài (setup)", "gag_payoff": "Trả (payoff)",
    "escalation": "Leo thang", "reversal": "Đảo ngược",
}
FRAME_SNAP_SECONDS = 1.0
TIME_SLACK_SECONDS = 1.0
FRAME_WIDTH = 640
CHECK_SHEET = "kiem_tra_ban_do_cot_truyen.csv"
COMPARISON = "so_sanh_model.csv"
CHECK_ANSWERS = ("DUNG", "SAI", "THIEU_BANG_CHUNG", "KHONG_CHAC")


class ReasonerError(RuntimeError):
    pass


def mmss(seconds: float) -> str:
    t = round(max(0.0, float(seconds)), 1)
    minutes = int(t // 60)
    return f"{minutes:02d}:{t - 60 * minutes:04.1f}"


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


def dense_frames(index: dict, start: float, end: float, out_dir: Path, *, n: int = 6) -> list[dict]:
    video = Path(index.get("video", {}).get("path", ""))
    if not video.is_file() or end <= start:
        return []
    times = [round(start + (end - start) * (i + 0.5) / n, 3) for i in range(n)]
    return [{**f, "scene_id": _scene_at(index, f["t"]), "source": "extra"} for f in _extract(video, times, out_dir)]


def window_lines(index: dict, window: dict) -> list[dict]:
    return [
        {"line_id": line["line_id"], "start": line["start_seconds"], "end": line["end_seconds"],
         "speaker": line.get("speaker"), "text_ja": line["text_ja"], "conf": line.get("asr_confidence"),
         "flags": line.get("asr_flags", [])}
        for scene in index["scenes"] if scene["scene_id"] in window["scene_ids"] for line in scene.get("dialogue", [])
    ]


# ── prompts ──────────────────────────────────────────────────────────────

ANSWERER_SYSTEM = ROLE_ANSWERER + """ Bạn là nhà phân tích phim hoạt hình (Crayon Shin-chan). Bạn xem một đoạn phim
dưới dạng chuỗi khung hình theo thời gian — trước mỗi ảnh có nhãn [t=giây cảnh] là thời điểm thật trong phim —
cùng lời thoại tiếng Nhật do Whisper nhận dạng (có thể sai; câu có cờ low_confidence/known_hallucination
không được coi là chắc chắn). Nhiệm vụ: HIỂU diễn biến, không chỉ mô tả ảnh.
- Tách rõ: "observation" = thấy/nghe trực tiếp; "inference" = suy ra từ bằng chứng; "hypothesis" = phỏng đoán.
- Mỗi mục phải trích bằng chứng: frame_times (đúng các mốc t đã cho) và/hoặc line_ids.
- Không bịa động cơ, nhân vật hay sự kiện không có trong khung hình/lời thoại. Không biết thì ghi hypothesis.
- Dùng STORY_MEMORY để nối với các cảnh trước (mục tiêu nhân vật, chi tiết đã gài chờ payoff).
- Viết mọi trường *_vi bằng tiếng Việt, ngắn gọn. Trích lời thoại thì giữ nguyên tiếng Nhật.
Chỉ trả về MỘT đối tượng JSON, không thêm chữ nào khác."""

ANSWERER_TASK = """Trả về JSON đúng khuôn:
{"window_summary_vi": "...",
 "events": [{"event_id": "e1", "kind": "observation|inference|hypothesis", "start_seconds": 0.0, "end_seconds": 0.0,
   "cause_vi": "", "action_vi": "", "reaction_vi": "", "consequence_vi": "",
   "evidence": {"frame_times": [0.0], "line_ids": ["sc001_l01"]}, "confidence": "high|medium|low"}],
 "character_states": [{"character": "", "goal_vi": "", "desire_vi": "", "obstacle_vi": "", "mistake_vi": "", "reaction_vi": ""}],
 "misunderstandings": [{"id": "m1", "kind": "...", "start_seconds": 0.0, "end_seconds": 0.0, "who_vi": "", "believes_vi": "",
   "truth_vi": "", "evidence": {"frame_times": [], "line_ids": []}}],
 "gags": [{"gag_id": "g1", "kind": "...", "mechanism": "incongruity|escalation|dramatic_irony|character_flaw|callback|misunderstanding|reversal|other",
   "why_funny_vi": "vì sao buồn cười",
   "setup": {"start_seconds": 0.0, "end_seconds": 0.0, "desc_vi": "", "evidence": {"frame_times": [], "line_ids": []}},
   "payoff": {"start_seconds": 0.0, "end_seconds": 0.0, "desc_vi": "", "evidence": {"frame_times": [], "line_ids": []}}}],
 "escalations_reversals": [{"id": "r1", "type": "escalation|reversal", "kind": "...", "start_seconds": 0.0, "end_seconds": 0.0,
   "desc_vi": "", "evidence": {"frame_times": [], "line_ids": []}}],
 "evidence_requests": [{"start_seconds": 0.0, "end_seconds": 0.0, "reason_vi": "cần xem kỹ vì..."}],
 "memory_update": {"story_so_far_vi": "", "characters": [{"character": "", "goal_vi": "", "state_vi": ""}],
   "open_setups": [{"id": "", "desc_vi": "", "start_seconds": 0.0}]}}
Payoff có thể nằm ở đoạn sau: khi đó để "payoff": null và ghi setup vào memory_update.open_setups."""

VERIFIER_SYSTEM = ROLE_VERIFIER + """ Bạn là người kiểm chứng. Chỉ dựa vào các khung hình (có nhãn thời gian) và lời
thoại được đưa, hãy xét xem KẾT LUẬN có được bằng chứng ủng hộ không. Mô tả ngắn điều thực sự thấy (tiếng Việt).
Chỉ trả JSON: {"verdict": "supported|partly|unsupported", "seen_vi": "..."}"""

SYNTHESIZER_SYSTEM = ROLE_SYNTHESIZER + """ Bạn tổng hợp cốt truyện cả tập từ các kết luận đã được gắn bằng chứng.
Chỉ dùng claim_id có trong danh sách; ưu tiên QUAN SÁT và SUY LUẬN; GIẢ THUYẾT chỉ được nêu là chưa xác minh.
Nối chuỗi nguyên nhân → hành động → phản ứng → hậu quả xuyên suốt tập; chỉ ra xung đột chính, bước ngoặt,
và mỗi tình huống hài: setup ở đâu, payoff ở đâu, vì sao buồn cười / hấp dẫn. Viết tiếng Việt.
Chỉ trả JSON:
{"logline_vi": "", "main_characters": [{"character": "", "want_vi": "", "obstacle_vi": "", "arc_vi": ""}],
 "causal_chain": [{"step": 1, "claim_ids": [], "explanation_vi": ""}],
 "gags": [{"setup_claim_ids": [], "payoff_claim_ids": [], "mechanism": "", "why_funny_vi": ""}],
 "conflicts": [{"claim_ids": [], "desc_vi": ""}], "turning_points": [{"claim_ids": [], "desc_vi": ""}],
 "why_watchable_vi": "", "open_questions_vi": []}"""


def parse_model_json(text: str) -> dict:
    text = text.split("</think>", 1)[1] if "</think>" in text else text
    text = re.sub(r"```(?:json)?", "", text)
    start = text.find("{")
    if start < 0:
        raise ReasonerError("no JSON object in the model answer")
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(text[start:], start):
        if in_str:
            esc = (ch == "\\") and not esc
            if ch == '"' and not esc:
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
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError as exc:
                    raise ReasonerError(f"invalid JSON from the model: {exc}") from exc
    raise ReasonerError("unterminated JSON object in the model answer")


def _answerer_parts(window: dict, frames: list[dict], lines: list[dict], memory: dict, *, retry: bool) -> list[dict]:
    meta = {"window_id": window["window_id"], "start_seconds": window["start_seconds"],
            "end_seconds": window["end_seconds"], "scene_ids": window["scene_ids"], "lines": lines}
    parts: list[dict] = [
        {"text": "WINDOW_JSON: " + json.dumps(meta, ensure_ascii=False)},
        {"text": "STORY_MEMORY: " + json.dumps(memory, ensure_ascii=False)},
        {"text": f"Khung hình theo thời gian ({mmss(window['start_seconds'])}–{mmss(window['end_seconds'])}):"},
    ]
    for f in frames:
        tag = " EXTRA" if f["source"] == "extra" else ""
        parts += [{"text": f"[t={f['t']:.3f}s {f['scene_id']}{tag}]"}, {"image": Path(f["path"])}]
    parts.append({"text": ANSWERER_TASK + ("\nLẦN TRƯỚC KHÔNG ĐỌC ĐƯỢC JSON — chỉ trả JSON." if retry else "")})
    return parts


# ── grounder ─────────────────────────────────────────────────────────────


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _ground(raw: dict, *, claim_id: str, category: str, text_vi: str, window: dict, frames: list[dict],
            line_ids: set[str], scenes: list[dict], extra: dict | None = None) -> dict:
    notes: list[str] = []
    kind = raw.get("kind") if raw.get("kind") in KINDS else "hypothesis"
    w0, w1 = window["start_seconds"], window["end_seconds"]
    s, e = _num(raw.get("start_seconds")), _num(raw.get("end_seconds"))
    if s is None or e is None:
        s, e = w0, w1
        notes.append("invalid_time")
        kind = "hypothesis"
    if s < w0 - TIME_SLACK_SECONDS or e > w1 + TIME_SLACK_SECONDS:
        notes.append("time_outside_window")
        kind = "hypothesis"
    s = min(max(s, w0), w1)
    e = min(max(e, s), w1)
    evidence = raw.get("evidence") or {}
    snapped = []
    for ft in evidence.get("frame_times") or []:
        ft = _num(ft)
        nearest = min(frames, key=lambda f: abs(f["t"] - ft), default=None) if ft is not None else None
        if nearest is not None and abs(nearest["t"] - ft) <= FRAME_SNAP_SECONDS:
            if nearest["t"] not in snapped:
                snapped.append(nearest["t"])
        else:
            notes.append("unmatched_frame")
    lines = []
    for lid in evidence.get("line_ids") or []:
        if lid in line_ids:
            lines.append(lid)
        else:
            notes.append("unknown_line")
    if kind != "hypothesis" and not snapped and not lines:
        notes.append("no_evidence")
        kind = "hypothesis"
    scene_ids = [sc["scene_id"] for sc in scenes
                 if sc["scene_id"] in window["scene_ids"] and sc["start_seconds"] <= e and s < sc["end_seconds"]] \
        or [sc["scene_id"] for sc in scenes if sc["scene_id"] in window["scene_ids"] and sc["start_seconds"] <= s < sc["end_seconds"]] \
        or [window["scene_ids"][-1]]
    claim = {"claim_id": claim_id, "category": category, "kind": kind, "start_seconds": round(s, 3),
             "end_seconds": round(e, 3), "scene_ids": scene_ids, "text_vi": text_vi.strip(),
             "evidence": {"frame_times": snapped, "line_ids": lines}, "confidence": raw.get("confidence"),
             "notes": sorted(set(notes))}
    if extra:
        claim.update(extra)
    return claim


def ground_claims(analysis: dict, window: dict, frames: list[dict], line_ids: set[str], scenes: list[dict]) -> tuple[list[dict], list[str]]:
    wid = window["window_id"]
    common = {"window": window, "frames": frames, "line_ids": line_ids, "scenes": scenes}
    claims: list[dict] = []
    for ev in analysis.get("events") or []:
        parts = [("Nguyên nhân", ev.get("cause_vi")), ("Hành động", ev.get("action_vi")),
                 ("Phản ứng", ev.get("reaction_vi")), ("Hậu quả", ev.get("consequence_vi"))]
        text = " → ".join(f"{k}: {v}" for k, v in parts if v)
        claims.append(_ground(ev, claim_id=f"{wid}.{ev.get('event_id', len(claims) + 1)}", category="event", text_vi=text,
                              extra={"chain": {k: v for k, v in zip(("cause_vi", "action_vi", "reaction_vi", "consequence_vi"),
                                                                    (p[1] for p in parts))}}, **common))
    for m in analysis.get("misunderstandings") or []:
        text = f"{m.get('who_vi', '?')} tưởng: {m.get('believes_vi', '')} — thực tế: {m.get('truth_vi', '')}"
        claims.append(_ground(m, claim_id=f"{wid}.{m.get('id', 'm')}", category="misunderstanding", text_vi=text, **common))
    for r in analysis.get("escalations_reversals") or []:
        category = "reversal" if r.get("type") == "reversal" else "escalation"
        claims.append(_ground(r, claim_id=f"{wid}.{r.get('id', 'r')}", category=category, text_vi=r.get("desc_vi", ""), **common))
    for g in analysis.get("gags") or []:
        gid = f"{wid}.{g.get('gag_id', 'g')}"
        shared = {"mechanism": g.get("mechanism"), "why_funny_vi": g.get("why_funny_vi", "")}
        setup = g.get("setup") or {}
        claims.append(_ground({**setup, "kind": g.get("kind")}, claim_id=f"{gid}.setup", category="gag_setup",
                              text_vi=setup.get("desc_vi", ""), extra={**shared, "links": {"payoff": f"{gid}.payoff" if g.get("payoff") else None}},
                              **common))
        if g.get("payoff"):
            payoff = g["payoff"]
            claims.append(_ground({**payoff, "kind": g.get("kind")}, claim_id=f"{gid}.payoff", category="gag_payoff",
                                  text_vi=payoff.get("desc_vi", ""), extra={**shared, "links": {"setup": f"{gid}.setup"}}, **common))
    notes = sorted({n for c in claims for n in c["notes"]})
    return claims, notes


# ── verifier / synthesizer ───────────────────────────────────────────────


def _verify(backend: Any, claim: dict, frames: list[dict], lines: list[dict], calls: list) -> None:
    s, e = claim["start_seconds"] - 0.5, claim["end_seconds"] + 0.5
    shown = [f for f in frames if s <= f["t"] <= e][:8] or sorted(frames, key=lambda f: abs(f["t"] - claim["start_seconds"]))[:2]
    near = [line for line in lines if line["start"] <= e and s <= line["end"]]
    parts: list[dict] = [{"text": f"KẾT LUẬN ({KIND_LABEL_VI[claim['kind']]}, {mmss(claim['start_seconds'])}–"
                                  f"{mmss(claim['end_seconds'])}): {claim['text_vi']}"
                                  + (f" | Vì sao buồn cười: {claim['why_funny_vi']}" if claim.get("why_funny_vi") else "")},
                         {"text": "Lời thoại: " + json.dumps(near, ensure_ascii=False)}]
    for f in shown:
        parts += [{"text": f"[t={f['t']:.3f}s {f['scene_id']}]"}, {"image": Path(f["path"])}]
    parts.append({"text": 'Chỉ trả JSON {"verdict": "...", "seen_vi": "..."}'})
    text, stats = backend.chat(VERIFIER_SYSTEM, parts, max_new_tokens=2048)
    calls.append({"role": "verifier", "claim_id": claim["claim_id"], **stats})
    try:
        result = parse_model_json(text)
    except ReasonerError:
        claim["verifier"] = {"verdict": "error", "seen_vi": ""}
        return
    verdict = result.get("verdict") if result.get("verdict") in ("supported", "partly", "unsupported") else "error"
    claim["verifier"] = {"verdict": verdict, "seen_vi": str(result.get("seen_vi", ""))[:400]}
    if verdict == "unsupported":
        claim["kind"] = "hypothesis"
        claim["notes"] = sorted(set(claim["notes"]) | {"verifier_rejected"})


def _synthesize(backend: Any, windows: list[dict], calls: list) -> dict:
    claims = {c["claim_id"]: c for w in windows for c in w["claims"]}
    listing = [f"{c['claim_id']} [{KIND_LABEL_VI[c['kind']]}] {mmss(c['start_seconds'])}-{mmss(c['end_seconds'])} "
               f"{CATEGORY_VI.get(c['category'], c['category'])}: {c['text_vi']}"
               + (f" (vì sao buồn cười: {c['why_funny_vi']})" if c.get("why_funny_vi") else "")
               for c in claims.values()]
    summaries = [f"{w['window_id']} {mmss(w['start_seconds'])}-{mmss(w['end_seconds'])}: {w.get('summary_vi', '')}" for w in windows]
    parts = [{"text": "TÓM TẮT TỪNG ĐOẠN:\n" + "\n".join(summaries)},
             {"text": "CÁC KẾT LUẬN (claim_id đứng đầu dòng):\n" + "\n".join(listing)},
             {"text": "Tổng hợp cốt truyện cả tập. Chỉ trả JSON."}]
    text, stats = backend.chat(SYNTHESIZER_SYSTEM, parts, max_new_tokens=6144)
    calls.append({"role": "synthesizer", **stats})
    try:
        synthesis = parse_model_json(text)
    except ReasonerError as exc:
        return {"status": "failed", "error": str(exc), "notes": []}
    notes: set[str] = set()

    def clean(ids: list) -> list[str]:
        kept = [i for i in ids or [] if i in claims]
        if len(kept) < len(ids or []):
            notes.add("unknown_claim_id")
        return kept

    for key in ("causal_chain", "conflicts", "turning_points"):
        for item in synthesis.get(key) or []:
            item["claim_ids"] = clean(item.get("claim_ids"))
            item["chua_xac_minh"] = bool(item["claim_ids"]) and all(claims[i]["kind"] == "hypothesis" for i in item["claim_ids"])
    for gag in synthesis.get("gags") or []:
        gag["setup_claim_ids"] = clean(gag.get("setup_claim_ids"))
        gag["payoff_claim_ids"] = clean(gag.get("payoff_claim_ids"))
    synthesis["status"] = "ok"
    synthesis["notes"] = sorted(notes)
    return synthesis


# ── run ──────────────────────────────────────────────────────────────────


def _analyze_window(backend, window, frames, lines, memory, calls) -> tuple[dict | None, str | None]:
    for retry in (False, True):
        text, stats = backend.chat(ANSWERER_SYSTEM, _answerer_parts(window, frames, lines, memory, retry=retry), max_new_tokens=6144)
        calls.append({"role": "answerer", "window_id": window["window_id"], "retry": retry, **stats})
        try:
            return parse_model_json(text), None
        except ReasonerError as exc:
            error = str(exc)
    return None, error


def run_reasoner(
    run_dir: Path,
    backend: Any,
    *,
    max_seconds: float = 40.0,
    max_scenes: int = 8,
    frames_per_window: int = 16,
    frame_interval: float = 1.0,
    verify_limit: int = 30,
    max_windows: int | None = None,
    max_evidence_rounds: int = 1,
) -> dict:
    run_dir = Path(run_dir)
    index = json.loads((run_dir / "anime_source_index.json").read_text(encoding="utf-8"))
    out = run_dir / "reasoner"
    frames_dir = out / "frames"
    out.mkdir(parents=True, exist_ok=True)
    info = backend.info()
    started = time.perf_counter()
    calls: list[dict] = []
    stats = {"evidence_rounds": 0, "parse_failures": 0, "verified": 0}
    memory: dict = {"story_so_far_vi": "", "characters": [], "open_setups": []}
    scenes = [{k: s[k] for k in ("scene_id", "start_seconds", "end_seconds")} for s in index["scenes"]]
    line_texts = {line["line_id"]: line["text_ja"] for s in index["scenes"] for line in s.get("dialogue", [])}
    all_line_ids = set(line_texts)
    windows_out = []

    windows = build_windows(index, max_seconds=max_seconds, max_scenes=max_scenes)
    for window in windows[:max_windows] if max_windows else windows:
        frames = sample_window_frames(index, window, frames_dir, interval=frame_interval, max_frames=frames_per_window)
        lines = window_lines(index, window)
        analysis, error = _analyze_window(backend, window, frames, lines, memory, calls)
        rounds = 0
        while analysis and analysis.get("evidence_requests") and rounds < max_evidence_rounds:
            extra = []
            for i, req in enumerate(analysis["evidence_requests"][:2]):
                s0, s1 = _num(req.get("start_seconds")), _num(req.get("end_seconds"))
                if s0 is None or s1 is None:
                    continue
                s0, s1 = max(s0, window["start_seconds"]), min(s1, window["end_seconds"])
                extra += dense_frames(index, s0, s1, frames_dir / f"{window['window_id']}_extra{rounds}{i}")
            if not extra:
                break
            frames = sorted(frames + extra, key=lambda f: f["t"])
            rounds += 1
            stats["evidence_rounds"] += 1
            again, error = _analyze_window(backend, window, frames, lines, memory, calls)
            analysis = again or analysis
        record = {**window, "frames": [{k: f[k] for k in ("t", "scene_id", "source", "path")} for f in frames]}
        if analysis is None:
            stats["parse_failures"] += 1
            windows_out.append({**record, "status": "failed", "error": error, "claims": []})
            continue
        claims, _ = ground_claims(analysis, window, frames, all_line_ids, scenes)
        for claim in claims:
            claim["evidence"]["line_texts"] = {lid: line_texts[lid] for lid in claim["evidence"]["line_ids"]}
        for claim in claims:
            if claim["kind"] == "inference" and stats["verified"] < verify_limit:
                _verify(backend, claim, frames, lines, calls)
                stats["verified"] += 1
        if isinstance(analysis.get("memory_update"), dict):
            mem = analysis["memory_update"]
            memory = {"story_so_far_vi": str(mem.get("story_so_far_vi", ""))[:1200],
                      "characters": (mem.get("characters") or [])[:10], "open_setups": (mem.get("open_setups") or [])[:10]}
        windows_out.append({**record, "status": "ok", "summary_vi": analysis.get("window_summary_vi", ""),
                            "character_states": analysis.get("character_states") or [], "claims": claims,
                            "memory_after": memory})

    ok_windows = [w for w in windows_out if w["status"] == "ok"]
    synthesis = _synthesize(backend, ok_windows, calls) if ok_windows else {"status": "failed", "notes": []}
    story = {"version": "1.0", "source_index_id": index["source_index_id"],
             "provenance": {**info, "real_model": info.get("backend") != "fake",
                            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                            "videomind": "roles adapted as prompts (planner/grounder/verifier/answerer); no VideoMind code or weights"},
             "windows": windows_out, "synthesis": synthesis}
    (out / "story_map.json").write_text(json.dumps(story, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "ban_do_cot_truyen.md").write_text(render_story_map_vi(story), encoding="utf-8")
    write_check_sheet(story, out / CHECK_SHEET)

    all_claims = [c for w in windows_out for c in w["claims"]]
    verdicts = [c["verifier"]["verdict"] for c in all_claims if c.get("verifier")]
    tps = [c["tokens_per_second"] for c in calls if c.get("tokens_per_second")]
    vram = [c["peak_vram_mb"] for c in calls if c.get("peak_vram_mb")]
    stats.update({
        "windows": len(windows_out), "windows_failed": len(windows_out) - len(ok_windows), "claims": len(all_claims),
        **{k: sum(1 for c in all_claims if c["kind"] == k) for k in KINDS},
        **{f"verifier_{v}": verdicts.count(v) for v in ("supported", "partly", "unsupported", "error")},
        "calls": len(calls), "hit_max_new_tokens": sum(1 for c in calls if c.get("hit_max_new_tokens")),
        "total_seconds": round(time.perf_counter() - started, 1),
        "avg_tokens_per_second": round(sum(tps) / len(tps), 2) if tps else None,
        "peak_vram_mb": max(vram) if vram else None,
    })
    report = {"run": run_dir.name, "model": info, "stats": stats, "calls": calls,
              "overall": "FAIL" if not ok_windows else "PENDING"}
    (out / "qwen_trial_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "qwen_trial_report.md").write_text(render_report_vi(report, story), encoding="utf-8")
    _upsert_comparison(run_dir, report)
    return report


# ── outputs ──────────────────────────────────────────────────────────────


def _evidence_text(claim: dict) -> str:
    texts = claim["evidence"].get("line_texts") or {}
    bits = [f"khung {', '.join(mmss(t) for t in claim['evidence']['frame_times'])}"] if claim["evidence"]["frame_times"] else []
    bits += [f"{lid}「{texts.get(lid, '')}」" for lid in claim["evidence"]["line_ids"]]
    return "; ".join(bits) or "không có"


def render_story_map_vi(story: dict) -> str:
    prov = story["provenance"]
    out = [f"# Bản đồ cốt truyện — {story['source_index_id']}", ""]
    if prov.get("real_model"):
        out.append(f"> Kết quả từ model thật **{prov.get('model_id')}** ({prov.get('backend')}, {prov.get('quantization')}). "
                   "Đây là suy luận của AI — **chưa được người đối chiếu với video** (xem kiem_tra_ban_do_cot_truyen.csv).")
    else:
        out.append(f"> ⚠ Chạy bằng backend `{prov.get('backend')}` — **KHÔNG phải model thật**, chỉ để kiểm thử quy trình.")
    out += ["", ("Nhãn: **QUAN SÁT** = thấy/nghe trực tiếp · **SUY LUẬN** = suy ra từ bằng chứng · "
            "**GIẢ THUYẾT** = chưa xác minh (không dùng làm sự thật)."), ""]
    syn = story.get("synthesis") or {}
    if syn.get("status") == "ok":
        out += ["## Tóm tắt cả tập", "", f"**Logline:** {syn.get('logline_vi', '')}", ""]
        for ch in syn.get("main_characters") or []:
            out.append(f"- **{ch.get('character')}** — muốn: {ch.get('want_vi', '')}; trở ngại: {ch.get('obstacle_vi', '')}; "
                       f"diễn biến: {ch.get('arc_vi', '')}")
        out += ["", "### Chuỗi nhân quả", ""]
        for step in syn.get("causal_chain") or []:
            flag = " _(chưa xác minh)_" if step.get("chua_xac_minh") else ""
            out.append(f"{step.get('step', '•')}. {step.get('explanation_vi', '')} — [{', '.join(step['claim_ids'])}]{flag}")
        if syn.get("gags"):
            out += ["", "### Tình huống hài (setup → payoff)", ""]
            for gag in syn["gags"]:
                out.append(f"- {gag.get('mechanism', '')}: {gag.get('why_funny_vi', '')} — setup [{', '.join(gag['setup_claim_ids'])}] "
                           f"→ payoff [{', '.join(gag['payoff_claim_ids'])}]")
        for key, title in (("conflicts", "Xung đột"), ("turning_points", "Bước ngoặt")):
            if syn.get(key):
                out += ["", f"### {title}", ""]
                out += [f"- {item.get('desc_vi', '')} [{', '.join(item['claim_ids'])}]" for item in syn[key]]
        if syn.get("why_watchable_vi"):
            out += ["", f"**Vì sao đáng xem:** {syn['why_watchable_vi']}"]
        if syn.get("notes"):
            out += ["", f"_Ghi chú kiểm tra: {', '.join(syn['notes'])}_"]
    else:
        out += ["## Tóm tắt cả tập", "", f"⚠ Bước tổng hợp thất bại: {syn.get('error', 'không có đoạn nào phân tích được')}"]
    out += ["", "## Diễn biến theo thời gian", ""]
    for w in story["windows"]:
        out.append(f"### {w['window_id']} · {mmss(w['start_seconds'])}–{mmss(w['end_seconds'])} · {', '.join(w['scene_ids'])}")
        out.append("")
        if w["status"] != "ok":
            out += [f"⚠ Không phân tích được đoạn này: {w.get('error')}", ""]
            continue
        out += [w.get("summary_vi", ""), ""]
        for c in w["claims"]:
            verdict = f" · kiểm chứng: {c['verifier']['verdict']}" if c.get("verifier") else ""
            out.append(f"- **[{KIND_LABEL_VI[c['kind']]}]** {mmss(c['start_seconds'])}–{mmss(c['end_seconds'])} "
                       f"{CATEGORY_VI.get(c['category'], c['category'])}: {c['text_vi']}"
                       + (f" — _vì sao buồn cười:_ {c['why_funny_vi']}" if c.get("why_funny_vi") else "")
                       + f"  \n  Bằng chứng: {_evidence_text(c)}{verdict}")
        for st in w.get("character_states") or []:
            out.append(f"- Nhân vật {st.get('character')}: mục tiêu {st.get('goal_vi', '')}; trở ngại {st.get('obstacle_vi', '')}; "
                       f"sai lầm {st.get('mistake_vi', '')}; phản ứng {st.get('reaction_vi', '')}")
        out.append("")
    return "\n".join(out) + "\n"


def write_check_sheet(story: dict, path: Path) -> None:
    with Path(path).open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["claim_id", "loai", "nhan", "bat_dau", "ket_thuc", "mo_ta_vi", "bang_chung", "verifier",
                         "ket_qua", "ghi_chu"])
        for w in story["windows"]:
            for c in w["claims"]:
                writer.writerow([c["claim_id"], CATEGORY_VI.get(c["category"], c["category"]), KIND_LABEL_VI[c["kind"]],
                                 mmss(c["start_seconds"]), mmss(c["end_seconds"]),
                                 c["text_vi"] + (f" | vì sao buồn cười: {c['why_funny_vi']}" if c.get("why_funny_vi") else ""),
                                 _evidence_text(c), (c.get("verifier") or {}).get("verdict", ""), "", ""])


def render_report_vi(report: dict, story: dict) -> str:
    s, m = report["stats"], report["model"]
    real = story["provenance"]["real_model"]
    out = [f"# Báo cáo thử nghiệm Tầng 1 — {report['run']}", "",
           f"**Trạng thái: {report['overall']}** — " + ("có lỗi nghiêm trọng" if report["overall"] == "FAIL" else
                                                        "chờ bạn đối chiếu bản đồ cốt truyện với video (kiem_tra_ban_do_cot_truyen.csv)"),
           "", "## Model thực tế đã chạy", "",
           f"- Backend: `{m.get('backend')}` · model: `{m.get('model_id')}` · lượng tử hóa: `{m.get('quantization')}` · GPU: `{m.get('gpu')}`",
           "- " + ("Model thật, chạy cục bộ, không gửi dữ liệu lên cloud." if real else "**KHÔNG phải model thật** (backend giả để kiểm thử)."),
           "", "## Tốc độ và tài nguyên", "",
           f"- Tổng thời gian: {s['total_seconds']} s · số lần gọi model: {s['calls']} · token/giây TB: {s['avg_tokens_per_second']}",
           f"- VRAM đỉnh (PyTorch): {s['peak_vram_mb']} MB · số lần chạm giới hạn max_new_tokens: {s['hit_max_new_tokens']}",
           "", "## Kết quả suy luận (tự động — chưa phải độ chính xác)", "",
           (f"- Đoạn: {s['windows']} (lỗi {s['windows_failed']}) · kết luận: {s['claims']} — quan sát {s['observation']}, "
           f"suy luận {s['inference']}, giả thuyết {s['hypothesis']}"),
           (f"- Verifier: ủng hộ {s['verifier_supported']}, một phần {s['verifier_partly']}, bác bỏ {s['verifier_unsupported']}, "
           f"lỗi {s['verifier_error']} · lần xin thêm bằng chứng: {s['evidence_rounds']} · lỗi đọc JSON: {s['parse_failures']}"),
           "", "## Lỗi phát hiện", ""]
    errors = [f"- {w['window_id']}: {w.get('error')}" for w in story["windows"] if w["status"] != "ok"]
    if story["synthesis"].get("status") != "ok":
        errors.append(f"- tổng hợp: {story['synthesis'].get('error')}")
    out += errors or ["- (không có lỗi kỹ thuật)"]
    out += ["", "## Việc còn PENDING", "",
            ("- **PENDING** — Bạn mở video tại các mốc thời gian trong `kiem_tra_ban_do_cot_truyen.csv`, ghi DUNG / SAI / "
            "THIEU_BANG_CHUNG / KHONG_CHAC cho từng kết luận, rồi chạy `8_CHAM_BAN_DO_COT_TRUYEN.bat`."),
            "- Chỉ khi độ chính xác người chấm đủ cao mới sang Tầng 2 (viết kịch bản)."]
    if report.get("score"):
        sc = report["score"]
        out += ["", "## Kết quả người chấm", "", f"- Đã chấm {sc['judged']} kết luận · chính xác {sc['accuracy']}",
                *[f"- {KIND_LABEL_VI.get(k, k)}: {v}" for k, v in sc.get("by_kind", {}).items()]]
    return "\n".join(out) + "\n"


COMPARISON_FIELDS = ["thoi_diem", "run", "backend", "model_id", "quantization", "gpu", "tong_giay", "tb_token_moi_giay",
                     "vram_dinh_mb", "so_doan", "so_ket_luan", "quan_sat", "suy_luan", "gia_thuyet", "verifier_ung_ho",
                     "verifier_mot_phan", "verifier_bac_bo", "loi_parse", "do_chinh_xac_nguoi_cham"]


def _read_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _write_rows(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _upsert_comparison(run_dir: Path, report: dict, *, accuracy: float | None = None) -> None:
    path = run_dir.parent / COMPARISON
    rows = [r for r in _read_rows(path) if r.get("run") != run_dir.name]
    s, m = report["stats"], report["model"]
    rows.append({
        "thoi_diem": datetime.now().astimezone().isoformat(timespec="seconds"), "run": run_dir.name,
        "backend": m.get("backend"), "model_id": m.get("model_id"), "quantization": m.get("quantization"), "gpu": m.get("gpu"),
        "tong_giay": s["total_seconds"], "tb_token_moi_giay": s["avg_tokens_per_second"], "vram_dinh_mb": s["peak_vram_mb"],
        "so_doan": s["windows"], "so_ket_luan": s["claims"], "quan_sat": s["observation"], "suy_luan": s["inference"],
        "gia_thuyet": s["hypothesis"], "verifier_ung_ho": s["verifier_supported"], "verifier_mot_phan": s["verifier_partly"],
        "verifier_bac_bo": s["verifier_unsupported"], "loi_parse": s["parse_failures"],
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
    r.add_argument("--quant", choices=["nf4", "bf16"], default="nf4")
    r.add_argument("--max-windows", type=int, help="analyse only the first N windows (pilot)")
    r.add_argument("--frames-per-window", type=int, default=16)
    r.add_argument("--verify-limit", type=int, default=30)
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
        BackendError,
        OllamaQwenBackend,
        TransformersQwenBackend,
    )

    backend = (TransformersQwenBackend(args.model or DEFAULT_MODEL_ID, quant=args.quant) if args.backend == "transformers"
               else OllamaQwenBackend(args.model or DEFAULT_OLLAMA_MODEL))
    print(f"Tang 1 tren {run_dir} bang {backend.info()['model_id']} ({args.backend}) — co the mat 30-90 phut cho ca tap")
    try:
        report = run_reasoner(run_dir, backend, max_seconds=args.window_seconds, frames_per_window=args.frames_per_window,
                              verify_limit=args.verify_limit, max_windows=args.max_windows)
    except BackendError as exc:
        print(f"LOI: {exc}")
        return 1
    print(f"Trang thai: {report['overall']} · ket luan: {report['stats']['claims']} · "
          f"thoi gian: {report['stats']['total_seconds']} s · VRAM dinh: {report['stats']['peak_vram_mb']} MB")
    print(f"Doc: {run_dir / 'reasoner' / 'ban_do_cot_truyen.md'}")
    return 1 if report["overall"] == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
