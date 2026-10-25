"""Validator for anime_recap_plan artifacts (Japanese anime review scripts).

An ``anime_recap_plan`` is a 60–90 s review script plus cut list built from an
``anime_source_index`` (scenes, dialogue and timestamps of one episode). This
module checks the plan against the index so the cut list can be trusted by a
video-cutting tool and so the narration never states an event the source does
not show:

- timestamps are finite, ordered, inside the video and inside the listed scenes;
- cut frames equal ``round(seconds * fps)`` and the output timeline is gapless;
- every ``event`` / ``quote`` claim cites evidence that exists in the index,
  and quotes match the source line verbatim;
- the narration is exactly the concatenation of its claims (no unsourced extra
  sentences) and fits inside its cut at a speakable Japanese rate;
- the five story roles (hook → development → conflict → climax → resolution)
  are present and in order.

Stdlib only. Schemas: ``schemas/artifacts/anime_source_index.schema.json`` and
``schemas/artifacts/anime_recap_plan.schema.json``. Usage::

    python -m lib.anime_recap --plan plan.json --index source_index.json [--json]
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

STORY_ROLES = ("hook", "development", "conflict", "climax", "resolution")
CLAIM_TYPES = ("event", "quote", "commentary", "question")
EVIDENCE_REQUIRED = frozenset({"event", "quote"})
DURATION_SOURCES = ("estimate", "measured")

DEFAULT_MIN_TOTAL_SECONDS = 60.0
DEFAULT_MAX_TOTAL_SECONDS = 90.0
MAX_JA_CHARS_PER_SECOND = 9.0
NARRATION_OVERRUN_TOLERANCE_SECONDS = 0.25
DEFAULT_JA_CHARS_PER_SECOND = 6.5
PAUSE_SECONDS = 0.15

_PLAN_FIELDS = ("version", "source_index_id", "language", "fps", "segments")
_SEGMENT_FIELDS = (
    "segment_id",
    "story_role",
    "scene_ids",
    "source_start_seconds",
    "source_end_seconds",
    "cut",
    "timeline_start_seconds",
    "timeline_end_seconds",
    "narration",
    "claims",
)
_CLAIM_FIELDS = ("claim_id", "text_ja", "type", "evidence")

# Characters that are not spoken: whitespace, punctuation, quote brackets.
# The long-vowel mark ー is spoken (one mora) and is deliberately kept.
_NON_SPOKEN = re.compile(r"[\s、。，．・！？!?「」『』（）()…〜\-\"“”]")
_PAUSES = re.compile(r"[、。！？!?]")
_QUOTE_STRIP = re.compile(r"[\s「」『』\"“”。、！？!?]")


@dataclass
class Issue:
    code: str
    path: str
    message: str


@dataclass
class ValidationReport:
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    source_is_mock: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors

    def codes(self) -> set[str]:
        return {issue.code for issue in self.errors}

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "source_is_mock": self.source_is_mock,
            "errors": [asdict(i) for i in self.errors],
            "warnings": [asdict(i) for i in self.warnings],
        }


def seconds_to_frame(seconds: float, fps: float) -> int:
    """Frame index of a timestamp; fractional rates (23.976) round to nearest."""
    return round(seconds * fps)


def spoken_char_count(text: str) -> int:
    return len(_NON_SPOKEN.sub("", text))


def estimate_ja_speech_seconds(
    text: str, chars_per_second: float = DEFAULT_JA_CHARS_PER_SECOND
) -> float:
    """Rough narration length for Japanese text.

    Counts spoken characters (Japanese has no word spaces, so word counts are
    meaningless) plus a short pause per 、。！？. Use only for planning; replace
    with the measured TTS duration (``duration_source: "measured"``) later.
    """
    chars = spoken_char_count(text)
    if chars == 0:
        return 0.0
    return round(chars / chars_per_second + PAUSE_SECONDS * len(_PAUSES.findall(text)), 2)


def _num(value: Any) -> float | None:
    """Finite real number or None (rejects bool, str, NaN, inf)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _norm_quote(text: str) -> str:
    return _QUOTE_STRIP.sub("", text)


class _Checker:
    def __init__(self, plan: dict, index: dict, min_total: float, max_total: float):
        self.plan = plan
        self.index = index
        self.min_total = min_total
        self.max_total = max_total
        self.report = ValidationReport()
        self.scenes: dict[str, dict] = {}
        self.lines: dict[str, tuple[str, dict]] = {}  # line_id -> (scene_id, line)
        self.fps = 0.0
        self.duration = 0.0

    def error(self, code: str, path: str, message: str) -> None:
        self.report.errors.append(Issue(code, path, message))

    def warn(self, code: str, path: str, message: str) -> None:
        self.report.warnings.append(Issue(code, path, message))

    # ── index ────────────────────────────────────────────────────────

    def load_index(self) -> bool:
        idx = self.index
        if not isinstance(idx, dict):
            self.error("missing_field", "index", "source index must be a JSON object")
            return False
        self.report.source_is_mock = (idx.get("provenance") or {}).get("kind") == "mock"
        video = idx.get("video") or {}
        fps, duration = _num(video.get("fps")), _num(video.get("duration_seconds"))
        if not fps or fps <= 0 or not duration or duration <= 0:
            self.error("missing_field", "index.video", "video.fps and video.duration_seconds must be positive numbers")
            return False
        self.fps, self.duration = fps, duration
        scenes = idx.get("scenes")
        if not isinstance(scenes, list) or not scenes:
            self.error("missing_field", "index.scenes", "source index has no scenes")
            return False
        for i, scene in enumerate(scenes):
            sid = scene.get("scene_id")
            start, end = _num(scene.get("start_seconds")), _num(scene.get("end_seconds"))
            if not sid or start is None or end is None or end <= start:
                self.error("invalid_timestamp", f"index.scenes[{i}]", "scene needs scene_id and start < end")
                continue
            self.scenes[sid] = scene
            for line in scene.get("dialogue") or []:
                if line.get("line_id"):
                    self.lines[line["line_id"]] = (sid, line)
        return bool(self.scenes)

    # ── plan ─────────────────────────────────────────────────────────

    def run(self) -> ValidationReport:
        if not self.load_index():
            return self.report
        plan = self.plan
        if not isinstance(plan, dict):
            self.error("missing_field", "plan", "plan must be a JSON object")
            return self.report
        missing = [f for f in _PLAN_FIELDS if f not in plan]
        for f in missing:
            self.error("missing_field", f"plan.{f}", f"required field '{f}' is missing")
        if "segments" in missing or not isinstance(plan.get("segments"), list):
            return self.report

        if plan.get("source_index_id") != self.index.get("source_index_id"):
            self.error(
                "index_mismatch",
                "plan.source_index_id",
                f"plan targets '{plan.get('source_index_id')}' but index is '{self.index.get('source_index_id')}'",
            )
        plan_fps = _num(plan.get("fps"))
        if plan_fps is None or abs(plan_fps - self.fps) > 1e-6:
            self.error("fps_mismatch", "plan.fps", f"plan fps {plan.get('fps')} != source fps {self.fps}")

        target = plan.get("target_duration_seconds") or {}
        min_total = _num(target.get("min")) or self.min_total
        max_total = _num(target.get("max")) or self.max_total

        seen_segments: set[str] = set()
        seen_claims: set[str] = set()
        roles: list[str] = []
        timeline_cursor: float | None = 0.0
        half_frame = 0.5 / self.fps

        for i, seg in enumerate(plan["segments"]):
            path = f"segments[{i}]"
            if not isinstance(seg, dict):
                self.error("missing_field", path, "segment must be an object")
                timeline_cursor = None
                continue
            absent = [f for f in _SEGMENT_FIELDS if f not in seg]
            for f in absent:
                self.error("missing_field", f"{path}.{f}", f"required field '{f}' is missing")
            if absent:
                timeline_cursor = None
                continue

            sid = seg["segment_id"]
            if sid in seen_segments:
                self.error("duplicate_id", f"{path}.segment_id", f"segment_id '{sid}' is used twice")
            seen_segments.add(sid)

            role = seg["story_role"]
            if role not in STORY_ROLES:
                self.error("invalid_field", f"{path}.story_role", f"story_role must be one of {STORY_ROLES}")
            roles.append(role)

            cut_len = self._check_source_range(seg, path)
            timeline_cursor = self._check_timeline(seg, path, cut_len, timeline_cursor, half_frame)
            self._check_dialogue_used(seg, path)
            self._check_narration(seg, path, cut_len)
            self._check_claims(seg, path, seen_claims)

        if timeline_cursor is not None and not (min_total <= timeline_cursor <= max_total):
            self.error(
                "total_duration_out_of_range",
                "plan.segments",
                f"total {timeline_cursor:.2f}s is outside {min_total:g}–{max_total:g}s",
            )
        self._check_story(roles)
        return self.report

    def _check_source_range(self, seg: dict, path: str) -> float | None:
        start, end = _num(seg["source_start_seconds"]), _num(seg["source_end_seconds"])
        if start is None or end is None or start < 0 or end <= start:
            self.error(
                "invalid_timestamp",
                f"{path}.source_start_seconds",
                f"source range must be finite numbers with 0 <= start < end (got {seg['source_start_seconds']!r}–{seg['source_end_seconds']!r})",
            )
            return None
        if end > self.duration + 1e-6:
            self.error("out_of_video", f"{path}.source_end_seconds", f"{end}s is past the video end ({self.duration}s)")

        scene_ids = seg["scene_ids"]
        if not isinstance(scene_ids, list) or not scene_ids:
            self.error("missing_field", f"{path}.scene_ids", "scene_ids must be a non-empty list")
        else:
            known = [self.scenes[s] for s in scene_ids if s in self.scenes]
            for s in scene_ids:
                if s not in self.scenes:
                    self.error("unknown_scene", f"{path}.scene_ids", f"scene '{s}' is not in the source index")
            if known and len(known) == len(scene_ids):
                lo = min(_num(s["start_seconds"]) for s in known)
                hi = max(_num(s["end_seconds"]) for s in known)
                if start < lo - 1e-6 or end > hi + 1e-6:
                    self.error(
                        "outside_scene",
                        f"{path}.source_start_seconds",
                        f"{start}–{end}s is outside scenes {scene_ids} ({lo}–{hi}s)",
                    )

        cut = seg["cut"] if isinstance(seg["cut"], dict) else {}
        for key, seconds in (("in_frame", start), ("out_frame", end)):
            expected = seconds_to_frame(seconds, self.fps)
            got = cut.get(key)
            if isinstance(got, bool) or not isinstance(got, int) or got != expected:
                self.error("frame_mismatch", f"{path}.cut.{key}", f"expected {expected} (round({seconds}*{self.fps})), got {got!r}")
        return end - start

    def _check_timeline(
        self, seg: dict, path: str, cut_len: float | None, cursor: float | None, tol: float
    ) -> float | None:
        t0, t1 = _num(seg["timeline_start_seconds"]), _num(seg["timeline_end_seconds"])
        if t0 is None or t1 is None or t1 <= t0:
            self.error("invalid_timestamp", f"{path}.timeline_start_seconds", "timeline range must be finite with start < end")
            return None
        if cursor is not None and abs(t0 - cursor) > tol:
            self.error("timeline_gap", f"{path}.timeline_start_seconds", f"starts at {t0}s but previous segment ends at {cursor}s")
        if cut_len is not None and abs((t1 - t0) - cut_len) > tol:
            self.error("timeline_gap", f"{path}.timeline_end_seconds", f"timeline length {t1 - t0:.3f}s != cut length {cut_len:.3f}s")
        return t1

    def _check_dialogue_used(self, seg: dict, path: str) -> None:
        start, end = _num(seg["source_start_seconds"]), _num(seg["source_end_seconds"])
        for j, used in enumerate(seg.get("dialogue_used") or []):
            upath = f"{path}.dialogue_used[{j}]"
            entry = self.lines.get(used.get("line_id"))
            if entry is None:
                self.error("unknown_line", upath, f"line '{used.get('line_id')}' is not in the source index")
                continue
            _, line = entry
            if used.get("text_ja") != line.get("text_ja"):
                self.error("dialogue_text_mismatch", f"{upath}.text_ja", f"text differs from source line: {line.get('text_ja')!r}")
            ls, le = _num(line.get("start_seconds")), _num(line.get("end_seconds"))
            if None not in (start, end, ls, le) and (le <= start or ls >= end):
                self.warn("dialogue_outside_cut", upath, f"line {ls}–{le}s is not audible in cut {start}–{end}s")

    def _check_narration(self, seg: dict, path: str, cut_len: float | None) -> None:
        narration = seg["narration"] if isinstance(seg["narration"], dict) else {}
        npath = f"{path}.narration"
        text = narration.get("text_ja")
        if not isinstance(text, str) or not text.strip():
            self.error("missing_field", f"{npath}.text_ja", "narration.text_ja is required")
            return
        if narration.get("duration_source") not in DURATION_SOURCES:
            self.error("invalid_field", f"{npath}.duration_source", f"must be one of {DURATION_SOURCES}")
        duration = _num(narration.get("duration_seconds"))
        if duration is None or duration <= 0:
            self.error("invalid_timestamp", f"{npath}.duration_seconds", "duration_seconds must be a positive number")
            return
        if cut_len is not None and duration > cut_len + NARRATION_OVERRUN_TOLERANCE_SECONDS:
            self.error("narration_exceeds_cut", f"{npath}.duration_seconds", f"narration {duration}s is longer than its cut {cut_len:.2f}s")
        rate = spoken_char_count(text) / duration
        if rate > MAX_JA_CHARS_PER_SECOND:
            self.error(
                "speech_rate_too_fast",
                f"{npath}.duration_seconds",
                f"{rate:.1f} chars/s exceeds {MAX_JA_CHARS_PER_SECOND} — duration is wrong or the text is too long",
            )

        claims = seg["claims"] if isinstance(seg["claims"], list) else []
        joined = "".join(c.get("text_ja", "") for c in claims if isinstance(c, dict))
        if re.sub(r"\s", "", joined) != re.sub(r"\s", "", text):
            self.error(
                "narration_claims_mismatch",
                f"{npath}.text_ja",
                "narration must be exactly the concatenation of its claims — an unsourced sentence was added or a claim is missing",
            )

    def _check_claims(self, seg: dict, path: str, seen: set[str]) -> None:
        if not isinstance(seg["claims"], list) or not seg["claims"]:
            self.error("missing_field", f"{path}.claims", "claims must be a non-empty list")
            return
        for k, claim in enumerate(seg["claims"]):
            cpath = f"{path}.claims[{k}]"
            absent = [f for f in _CLAIM_FIELDS if f not in claim]
            for f in absent:
                self.error("missing_field", f"{cpath}.{f}", f"required field '{f}' is missing")
            if absent:
                continue
            cid = claim["claim_id"]
            if cid in seen:
                self.error("duplicate_id", f"{cpath}.claim_id", f"claim_id '{cid}' is used twice")
            seen.add(cid)
            ctype = claim["type"]
            if ctype not in CLAIM_TYPES:
                self.error("invalid_field", f"{cpath}.type", f"type must be one of {CLAIM_TYPES}")

            evidence = claim["evidence"] if isinstance(claim["evidence"], list) else []
            valid_lines = [line for line in (self._check_evidence(ev, f"{cpath}.evidence[{e}]") for e, ev in enumerate(evidence)) if line]

            if ctype in EVIDENCE_REQUIRED and not evidence:
                self.error("claim_without_evidence", cpath, f"'{ctype}' claim has no evidence: {claim['text_ja']!r}")
            if ctype == "quote":
                cited = [ev for ev in evidence if ev.get("line_id")]
                if not cited:
                    self.error("claim_without_evidence", cpath, "quote must cite the dialogue line_id it quotes")
                elif valid_lines and not any(_norm_quote(claim["text_ja"]) in _norm_quote(line.get("text_ja", "")) for line in valid_lines):
                    self.error("quote_not_in_source", f"{cpath}.text_ja", f"quote {claim['text_ja']!r} does not appear verbatim in the cited line(s)")

    def _check_evidence(self, ev: dict, path: str) -> dict | None:
        """Validate one evidence item; return the cited dialogue line if any."""
        scene = self.scenes.get(ev.get("scene_id"))
        if scene is None:
            self.error("unknown_scene", f"{path}.scene_id", f"scene '{ev.get('scene_id')}' is not in the source index")
            return None
        start, end = _num(ev.get("start_seconds")), _num(ev.get("end_seconds"))
        ss, se = _num(scene["start_seconds"]), _num(scene["end_seconds"])
        if start is None or end is None or end <= start:
            self.error("invalid_timestamp", f"{path}.start_seconds", "evidence needs finite start < end")
        elif start < ss - 1e-6 or end > se + 1e-6:
            self.error("evidence_outside_scene", f"{path}.start_seconds", f"{start}–{end}s is outside scene {ev['scene_id']} ({ss}–{se}s)")
        line_id = ev.get("line_id")
        if line_id is None:
            return None
        entry = self.lines.get(line_id)
        if entry is None or entry[0] != ev["scene_id"]:
            self.error("unknown_line", f"{path}.line_id", f"line '{line_id}' is not a dialogue line of scene {ev['scene_id']}")
            return None
        return entry[1]

    def _check_story(self, roles: list[str]) -> None:
        if not roles:
            return
        for role in STORY_ROLES:
            if role not in roles:
                self.error("missing_story_role", "plan.segments", f"no segment has story_role '{role}'")
        if roles[0] != "hook":
            self.error("first_segment_not_hook", "segments[0].story_role", f"first segment is '{roles[0]}', expected 'hook'")
        order = [("conflict", "climax"), ("climax", "resolution")]
        for before, after in order:
            if before in roles and after in roles and roles.index(after) < roles.index(before):
                self.error("story_order", "plan.segments", f"first '{after}' comes before first '{before}'")


def validate_recap_plan(
    plan: dict,
    source_index: dict,
    *,
    min_total: float = DEFAULT_MIN_TOTAL_SECONDS,
    max_total: float = DEFAULT_MAX_TOTAL_SECONDS,
) -> ValidationReport:
    """Validate ``plan`` against ``source_index``. Inputs are not modified.

    ``plan.target_duration_seconds`` (min/max), when present, overrides the
    ``min_total``/``max_total`` defaults.
    """
    return _Checker(plan, source_index, min_total, max_total).run()


def _load_json(path: str, label: str, report: ValidationReport) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        report.errors.append(Issue("invalid_json", label, f"cannot read {path}: {exc}"))
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lib.anime_recap", description="Validate an anime_recap_plan against its anime_source_index.")
    parser.add_argument("--plan", required=True, help="anime_recap_plan JSON")
    parser.add_argument("--index", required=True, help="anime_source_index JSON")
    parser.add_argument("--json", action="store_true", help="print a machine-readable report")
    args = parser.parse_args(argv)

    report = ValidationReport()
    plan = _load_json(args.plan, "plan", report)
    index = _load_json(args.index, "index", report)
    if plan is not None and index is not None:
        report = validate_recap_plan(plan, index)

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        if report.source_is_mock:
            print("MOCK SOURCE: this index is mock data, not an analysed episode.")
        for issue in report.errors:
            print(f"ERROR   {issue.code:<28} {issue.path}: {issue.message}")
        for issue in report.warnings:
            print(f"WARNING {issue.code:<28} {issue.path}: {issue.message}")
        print("OK" if report.ok else f"FAILED ({len(report.errors)} error(s))")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
