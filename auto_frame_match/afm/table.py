"""Bảng cắt: BANG_CAT_AUTO.csv/.json, bảng từng khung, SOURCE_MANIFEST.json, bảng kiểm tra thủ công."""

from __future__ import annotations

import csv
import json
import time
from fractions import Fraction
from pathlib import Path

import numpy as np

from .align import Geometry
from .config import TOOL_VERSION
from .fingerprint import Fingerprint
from .matcher import GREEN, RED, YELLOW, Segment

STATUS_LABEL = {GREEN: "XANH - đã kiểm định", YELLOW: "VÀNG - nghi vấn", RED: "ĐỎ - không khớp/UNMATCHED"}
# Trạng thái người dùng có thể ghi vào cột manual_action khi kiểm tra thủ công
MANUAL_OK, MANUAL_SKIP = "OK", "BO_QUA"
MANUAL_FALLBACK = "DUYET_FALLBACK"  # V2: duyệt dùng khung Douyin (đã xem là sạch) cho đoạn không có nguồn

SEG_FIELDS = [
    "segment_id", "status", "method", "clean_check", "manual_action", "edit_frame_start", "edit_frame_end", "edit_pts_start",
    "edit_time_start", "edit_time_end", "duration", "source_id", "source_frame_start", "source_frame_end",
    "source_pts_start", "source_time_start", "source_time_end", "source_frame_exact", "speed", "flip", "crop_x", "crop_y",
    "crop_w", "crop_h", "score", "ok_ratio", "audio_offset_ms", "candidates", "reasons",
    "geom_sx", "geom_sy", "geom_tx", "geom_ty",
]
FRAME_FIELDS = [
    "edit_frame", "edit_pts", "edit_time", "segment_id", "status", "method", "origin", "verified", "clean_check",
    "source_id", "source_frame",
    "source_pts", "source_time", "similarity", "flip", "crop", "speed",
]


def _tb(tb: str) -> Fraction:
    try:
        return Fraction(tb)
    except Exception:
        return Fraction(1, 1000)


def to_pts(t: float, tb: str) -> int:
    return int(round(Fraction(t).limit_denominator(10 ** 9) / _tb(tb)))


def phash_samples(fp: Fingerprint, k: int = 24) -> list[dict]:
    idx = np.unique(np.linspace(0, fp.n - 1, min(k, fp.n)).astype(int))
    return [{"frame": int(i), "pts_time": float(fp.pts[i]), "phash": f"{int(fp.phashes[i]):016x}"} for i in idx]


def build_manifest(sources: list[dict], infos: dict, fps_: dict[str, Fingerprint]) -> dict:
    out = {"tool_version": TOOL_VERSION, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "sources": {}}
    for s in sources:
        sid = s["id"]
        info, fp = infos[sid], fps_[sid]
        a = info.pick_audio(s.get("audio_lang", "jpn"), s.get("audio_stream"))
        out["sources"][sid] = {
            "source_id": sid,
            "path": str(Path(info.path).resolve()),
            "render_path": str(Path(s["render_path"]).resolve()) if s.get("render_path") else None,
            "content_hash": info.content_hash,
            "fps": info.fps, "fps_fraction": info.fps_fraction, "time_base": info.time_base,
            "start_time": info.start_time, "video_start_time": info.video_start_time,
            "duration": info.duration, "n_frames": fp.n, "width": info.width, "height": info.height,
            "display_aspect": info.display_aspect, "codec": info.codec,
            "first_pts_time": float(fp.pts[0]), "last_pts_time": float(fp.pts[-1]),
            "audio_stream": a, "audio_offset_ms": float(s.get("audio_offset_ms", 0.0)),
            "fingerprint_cache": fp.root.name,
            "phash_samples": phash_samples(fp),
        }
    return out


def segment_rows(segs: list[Segment], edit_fp: Fingerprint, edit_info, src_fps: dict[str, Fingerprint],
                 manifest: dict) -> tuple[list[dict], list[dict]]:
    seg_rows, frame_rows = [], []
    e_tb = edit_info.time_base
    for k, sg in enumerate(segs, 1):
        sid = sg.sid
        t0 = float(edit_fp.pts[sg.start])
        t1 = float(edit_fp.pts[sg.end]) if sg.end < edit_fp.n else \
            float(edit_fp.pts[-1]) + 1.0 / edit_fp.fps
        row = {
            "segment_id": k, "status": sg.status, "method": sg.method if sg.sid else "", "manual_action": "",
            "edit_frame_start": sg.start, "edit_frame_end": sg.end - 1,
            "edit_pts_start": to_pts(t0, e_tb), "edit_time_start": round(t0, 6), "edit_time_end": round(t1, 6),
            "duration": round(t1 - t0, 6),
            "source_id": sid or "UNMATCHED",
            "candidates": json.dumps(sg.candidates, ensure_ascii=False),
            "reasons": " | ".join(sg.reasons),
            "score": round(float(sg.score), 4), "ok_ratio": round(sg.frame_status.count(GREEN) / max(1, sg.n), 4),
        }
        if sid is not None:
            fp = src_fps[sid]
            m = manifest["sources"][sid]
            f0, f1 = int(sg.frames[0]), int(sg.frames[-1])
            crop = sg.geom.source_crop() or (0, 0, 1, 1)
            row.update({
                "source_frame_start": f0, "source_frame_end": f1,
                "source_pts_start": to_pts(float(fp.pts[f0]), m["time_base"]),
                "source_time_start": round(float(fp.pts[f0]), 6),
                "source_time_end": round(float(fp.pts[f1]) + 1.0 / fp.fps, 6),
                "source_frame_exact": round(float(sg.a + sg.speed * fp.fps * (t0 - sg.t_ref)), 4),
                "speed": round(sg.speed, 6), "flip": int(sg.geom.flip),
                "crop_x": round(crop[0] * m["width"], 1), "crop_y": round(crop[1] * m["height"], 1),
                "crop_w": round(crop[2] * m["width"], 1), "crop_h": round(crop[3] * m["height"], 1),
                "audio_offset_ms": m["audio_offset_ms"],
                "geom_sx": sg.geom.sx, "geom_sy": sg.geom.sy, "geom_tx": sg.geom.tx, "geom_ty": sg.geom.ty,
            })
        seg_rows.append(row)
        for j in range(sg.n):
            ei = sg.start + j
            te = float(edit_fp.pts[ei])
            fr = {"edit_frame": ei, "edit_pts": to_pts(te, e_tb), "edit_time": round(te, 6), "segment_id": k,
                  "status": sg.frame_status[j] if sg.frame_status else sg.status,
                  "method": sg.method if sg.sid else ""}
            if sid is not None:
                f = int(sg.frames[j])
                v = sg.sims[j]
                fr.update({"source_id": sid, "source_frame": f,
                           "source_pts": to_pts(float(src_fps[sid].pts[f]), manifest["sources"][sid]["time_base"]),
                           "source_time": round(float(src_fps[sid].pts[f]), 6),
                           "similarity": round(float(v), 4) if np.isfinite(v) else "",
                           "flip": int(sg.geom.flip), "crop": row["crop_x"], "speed": row["speed"]})
                fr["crop"] = f'{row["crop_x"]},{row["crop_y"]},{row["crop_w"]},{row["crop_h"]}'
            else:
                fr.update({"source_id": "", "source_frame": "", "source_pts": "", "source_time": "",
                           "similarity": "", "flip": "", "crop": "", "speed": ""})
            frame_rows.append(fr)
    return seg_rows, frame_rows


def write_csv(path: Path, rows: list[dict], fields: list[str]):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig: Excel đọc đúng tiếng Việt
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_outputs(out_dir: Path, seg_rows, frame_rows, manifest: dict, edit_meta: dict, analysis_meta: dict,
                  timeline: dict | None = None):
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / "BANG_CAT_AUTO.csv", seg_rows, SEG_FIELDS)
    write_csv(out_dir / "BANG_CAT_TUNG_FRAME.csv", frame_rows, FRAME_FIELDS)
    manual = [r for r in seg_rows if r["status"] != GREEN]
    write_csv(out_dir / "KIEM_TRA_THU_CONG.csv", manual, SEG_FIELDS)
    (out_dir / "SOURCE_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), "utf-8")
    doc = {
        "tool_version": TOOL_VERSION, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "edit": edit_meta, "analysis": analysis_meta,
        "summary": summarize(seg_rows, frame_rows, timeline),
        "sources": {k: {kk: v[kk] for kk in ("source_id", "path", "render_path", "content_hash", "fps",
                                               "fps_fraction", "time_base", "start_time", "audio_offset_ms")}
                    for k, v in manifest["sources"].items()},
        "segments": seg_rows, "frames": frame_rows,
    }
    (out_dir / "BANG_CAT_AUTO.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), "utf-8")
    return doc


def summarize(seg_rows, frame_rows, timeline: dict | None = None) -> dict:
    cnt = {GREEN: 0, YELLOW: 0, RED: 0}
    for r in frame_rows:
        cnt[r["status"]] = cnt.get(r["status"], 0) + 1
    segc = {GREEN: 0, YELLOW: 0, RED: 0}
    for r in seg_rows:
        segc[r["status"]] = segc.get(r["status"], 0) + 1
    n = max(1, len(frame_rows))
    return {"frames_total": len(frame_rows), "frames_green": cnt[GREEN], "frames_yellow": cnt[YELLOW],
            "frames_red": cnt[RED], "segments_total": len(seg_rows), "segments_green": segc[GREEN],
            "segments_yellow": segc[YELLOW], "segments_red": segc[RED],
            "verified_ratio": round(cnt[GREEN] / n, 4),
            # V2: "đã xác minh" = mọi khung có nguồn gốc đã kiểm định (anime gốc XANH hoặc Douyin sạch đã duyệt)
            "fully_verified": bool(timeline["all_verified"]) if timeline else
            (cnt[YELLOW] == 0 and cnt[RED] == 0 and len(frame_rows) > 0),
            "timeline": timeline or {}}


# ---------------------------------------------------------------- đọc bảng (Chế độ B)

def _num(v, typ=float, default=None):
    if v is None or v == "":
        return default
    try:
        return typ(float(v)) if typ is int else typ(v)
    except (TypeError, ValueError):
        return default


# Tên cột thay thế (bảng do người dùng/tool khác lập) -> tên chuẩn
COLUMN_ALIASES = {
    "source_id": ("nguon", "nguồn", "source", "video_goc", "src", "id_nguon"),
    "edit_time_start": ("edit_start", "bat_dau_edit", "edit_in", "start_edit", "tg_edit_bat_dau"),
    "edit_time_end": ("edit_end", "ket_thuc_edit", "edit_out", "end_edit", "tg_edit_ket_thuc"),
    "source_frame_start": ("src_frame", "khung_nguon", "source_frame", "frame_goc"),
    "source_time_start": ("source_start", "src_start", "bat_dau_nguon", "tg_nguon"),
    "edit_frame_start": ("edit_frame", "khung_edit_bat_dau"),
    "manual_action": ("thu_cong", "hanh_dong"),
}


def _alias(r: dict) -> dict:
    low = {str(k).strip().lower(): v for k, v in r.items()}
    out = dict(low)
    for std, alts in COLUMN_ALIASES.items():
        if not out.get(std):
            for a in alts:
                if low.get(a) not in (None, ""):
                    out[std] = low[a]
                    break
    # chỉ có thời gian nguồn (không có số khung) -> source_frame_exact sẽ được suy ra khi dựng
    return out


def read_table(path: str | Path) -> dict:
    """Đọc BANG_CAT_AUTO.csv hoặc .json (kể cả bảng người dùng đã sửa tay)."""
    p = Path(path)
    if p.suffix.lower() == ".json":
        doc = json.loads(p.read_text(encoding="utf-8-sig"))
        rows = doc.get("segments", doc if isinstance(doc, list) else [])
        meta = {k: doc.get(k) for k in ("edit", "sources", "analysis")} if isinstance(doc, dict) else {}
    else:
        with open(p, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        meta = {}
        jp = p.with_suffix(".json")
        if jp.exists():
            try:
                d = json.loads(jp.read_text(encoding="utf-8-sig"))
                meta = {k: d.get(k) for k in ("edit", "sources", "analysis")}
            except Exception:
                pass
    segs = []
    rows = [_alias(r) for r in rows]
    for r in rows:
        sid = (r.get("source_id") or "").strip()
        seg = {
            "segment_id": _num(r.get("segment_id"), int, len(segs) + 1),
            "status": (r.get("status") or "").strip() or RED,
            "manual_action": (r.get("manual_action") or "").strip().upper(),
            "edit_frame_start": _num(r.get("edit_frame_start"), int),
            "edit_frame_end": _num(r.get("edit_frame_end"), int),
            "edit_time_start": _num(r.get("edit_time_start")),
            "edit_time_end": _num(r.get("edit_time_end")),
            "duration": _num(r.get("duration")),
            "source_id": None if sid in ("", "UNMATCHED") else sid,
            "source_frame_start": _num(r.get("source_frame_start"), int),
            "source_frame_exact": _num(r.get("source_frame_exact")),
            "source_time_start": _num(r.get("source_time_start")),
            "speed": _num(r.get("speed"), float, 1.0),  # 0 = giữ khung (freeze)
            "method": (r.get("method") or "").strip().lower() or "match",
            "flip": bool(_num(r.get("flip"), int, 0)),
            "crop": tuple(_num(r.get(k)) for k in ("crop_x", "crop_y", "crop_w", "crop_h")),
            "audio_offset_ms": _num(r.get("audio_offset_ms"), float, 0.0),
            "score": _num(r.get("score"), float, -1),
            "reasons": r.get("reasons", ""),
        }
        g = [_num(r.get(k)) for k in ("geom_sx", "geom_sy", "geom_tx", "geom_ty")]
        seg["geometry"] = Geometry(*g) if all(v is not None for v in g) else None
        if seg["duration"] is None and seg["edit_time_start"] is not None and seg["edit_time_end"] is not None:
            seg["duration"] = seg["edit_time_end"] - seg["edit_time_start"]
        segs.append(seg)
    return {"segments": segs, "meta": meta}


# ---------------------------------------------------------------- V2: dòng thời gian & báo cáo

TIMELINE_FIELDS = [
    "edit_frame", "edit_pts", "edit_duration", "source_id", "source_frame", "source_pts", "speed", "crop", "flip",
    "confidence", "status", "method", "origin", "clean_check", "notes",
]


def write_v2_reports(out_dir: Path, recs: list[dict], segs, edit_fp, edit_info, src_fps: dict, manifest: dict,
                     clean_by_seg: dict) -> None:
    """FRAME_TIMELINE_EXACT.csv, SPEED_RAMP_REPORT.csv, CLEAN_FALLBACK_REPORT.csv, UNMATCHED_RESCUE_REPORT.csv."""
    e_tb = edit_info.time_base
    last = float(edit_fp.pts[-1]) + 1.0 / edit_fp.fps
    rows = []
    for r in recs:
        i = r["edit_frame"]
        sg = segs[r["segment_id"] - 1]
        te = float(edit_fp.pts[i])
        te1 = float(edit_fp.pts[i + 1]) if i + 1 < edit_fp.n else last
        row = {"edit_frame": i, "edit_pts": to_pts(te, e_tb), "edit_duration": round(te1 - te, 6),
               "status": r["status"], "method": r.get("method", ""), "origin": r["origin"],
               "clean_check": r.get("clean_check", ""), "speed": round(r["speed"], 4) if "speed" in r else ""}
        if r["source_id"]:
            sid, f = r["source_id"], r["source_frame"]
            crop = sg.geom.source_crop() or (0, 0, 1, 1)
            m = manifest["sources"][sid]
            k = i - sg.start
            v = sg.sims[k] if sg.sims is not None else float("nan")
            row.update({"source_id": sid, "source_frame": f,
                        "source_pts": to_pts(float(src_fps[sid].pts[f]), m["time_base"]),
                        "crop": f"{crop[0] * m['width']:.1f},{crop[1] * m['height']:.1f},"
                                f"{crop[2] * m['width']:.1f},{crop[3] * m['height']:.1f}",
                        "flip": int(sg.geom.flip), "confidence": round(float(v), 4) if np.isfinite(v) else "",
                        "notes": (f"trộn với khung {r['pair']}" if r["pair"] >= 0 else "")})
        else:
            row.update({"source_id": "", "source_frame": "", "source_pts": "", "crop": "", "flip": "",
                        "confidence": "", "notes": "lấy khung Douyin" if r["origin"] == "DOUYIN_CLEAN_FALLBACK"
                        else "chưa xác minh - cần duyệt"})
        rows.append(row)
    write_csv(out_dir / "FRAME_TIMELINE_EXACT.csv", rows, TIMELINE_FIELDS)

    # tốc độ: mỗi đoạn có tốc độ thay đổi -> các khúc tốc độ không đổi
    sp_rows = []
    for k, sg in enumerate(segs, 1):
        if sg.sid is None:
            continue
        sp = sg.local_speed if sg.local_speed is not None else np.full(sg.n, sg.speed)
        a = 0
        for j in range(1, sg.n + 1):
            if j == sg.n or abs(sp[j] - sp[a]) > 1e-6:
                sp_rows.append({"segment_id": k, "edit_frame_start": sg.start + a, "edit_frame_end": sg.start + j - 1,
                                "source_id": sg.sid, "source_frame_start": int(sg.frames[a]),
                                "source_frame_end": int(sg.frames[j - 1]), "speed": round(float(sp[a]), 4),
                                "kind": "dong_hinh" if sp[a] == 0 else ("ramp" if sg.local_speed is not None
                                                                        else "tuyen_tinh"),
                                "status": sg.status})
                a = j
    write_csv(out_dir / "SPEED_RAMP_REPORT.csv", sp_rows,
              ["segment_id", "edit_frame_start", "edit_frame_end", "source_id", "source_frame_start",
               "source_frame_end", "speed", "kind", "status"])

    cf_rows = []
    for (k, part), cc in sorted(clean_by_seg.items()):
        sg = segs[k - 1]
        cf_rows.append({"segment_id": k, "phan": "ca_doan" if part == "seg" else "khung_do_trong_doan",
                        "edit_frame_start": sg.start, "edit_frame_end": sg.end - 1, "verdict": cc.verdict,
                        "used_as_fallback": int(any(r["origin"] == "DOUYIN_CLEAN_FALLBACK"
                                                    for r in recs if r["segment_id"] == k)),
                        "reasons": " | ".join(cc.reasons), "scores": json.dumps(cc.scores, ensure_ascii=False)})
    write_csv(out_dir / "CLEAN_FALLBACK_REPORT.csv", cf_rows,
              ["segment_id", "phan", "edit_frame_start", "edit_frame_end", "verdict", "used_as_fallback",
               "reasons", "scores"])

    un_rows = []
    for k, sg in enumerate(segs, 1):
        tiers = [n for n in sg.notes if n.startswith(("UNMATCHED", "SIFT", "TẦNG", "LIÊN TỤC", "DP"))]
        if sg.sid is None or any(n.startswith(("UNMATCHED", "SIFT", "TẦNG")) for n in sg.notes):
            un_rows.append({"segment_id": k, "edit_frame_start": sg.start, "edit_frame_end": sg.end - 1,
                            "final_status": sg.status, "final_source": sg.sid or "UNMATCHED",
                            "method": sg.method if sg.sid else "", "tiers": " | ".join(tiers),
                            "classification": getattr(sg, "red_class", "")})
    write_csv(out_dir / "UNMATCHED_RESCUE_REPORT.csv", un_rows,
              ["segment_id", "edit_frame_start", "edit_frame_end", "final_status", "final_source", "method",
               "tiers", "classification"])
