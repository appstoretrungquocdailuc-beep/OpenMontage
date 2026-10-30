"""Điều phối Chế độ A (phân tích & cắt) và Chế độ B (cắt theo bảng có sẵn), có tiếp tục khi gián đoạn."""

from __future__ import annotations

import hashlib
import json
import pickle
import threading
import time
from pathlib import Path

import numpy as np

from .config import FINGERPRINT_VERSION, TOOL_VERSION, AppConfig
from .fingerprint import build_fingerprint
from .log import get_logger
from .matcher import GREEN, Matcher
from .media import probe
from .scenes import detect_content_rect, detect_scenes
from .table import build_manifest, segment_rows, write_outputs

log = get_logger()

STEPS = [
    "1. Kiểm tra metadata", "2. Lập chỉ mục video nguồn", "3. Phát hiện cảnh Douyin",
    "4-7. Tìm nguồn & đối chiếu từng frame", "8. Kiểm định bảng cắt", "9-10. Cắt ghép & xuất 2K",
    "11. Kiểm tra thành phẩm (QA)",
]


class Reporter:
    def __init__(self, cb=None):
        self.cb = cb

    def __call__(self, step: int, frac: float, msg: str = ""):
        if self.cb:
            self.cb(step, STEPS[step], max(0.0, min(1.0, frac)), msg)


def normalize_sources(sources: list) -> list[dict]:
    out = []
    for k, s in enumerate(sources, 1):
        if isinstance(s, (str, Path)):
            s = {"path": str(s)}
        s = dict(s)
        s.setdefault("id", f"S{k}")
        s.setdefault("audio_offset_ms", 0.0)
        s.setdefault("render_path", None)
        out.append(s)
    return out


def _code_hash() -> str:
    """Băm mã nguồn thuật toán: đổi/cập nhật thuật toán -> không dùng lại checkpoint cũ."""
    h = hashlib.sha1()
    for name in ("align.py", "matcher.py", "scenes.py", "fingerprint.py", "index.py", "config.py", "sift_tier.py"):
        h.update((Path(__file__).parent / name).read_bytes())
    return h.hexdigest()[:10]


def _job_dir(out_dir: Path, edit_hash: str, src_hashes: list[str], cfg: AppConfig) -> Path:
    key = hashlib.sha1(json.dumps([edit_hash, src_hashes, cfg.analysis.__dict__, FINGERPRINT_VERSION,
                                   TOOL_VERSION, _code_hash()], sort_keys=True).encode()).hexdigest()[:12]
    d = out_dir / ".afm_job" / key
    d.mkdir(parents=True, exist_ok=True)
    return d


def analyze(edit_path: str, sources: list, out_dir: str | Path, cfg: AppConfig | None = None,
            report=None, cancel: threading.Event | None = None) -> dict:
    """Bước 1-8: tạo bảng cắt. Trả về dict kết quả (đường dẫn file, tóm tắt)."""
    cfg = cfg or AppConfig()
    rep = Reporter(report)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sources = normalize_sources(sources)
    t_start = time.time()

    # 1. metadata
    rep(0, 0.0, "đọc metadata")
    edit_info = probe(edit_path)
    infos = {}
    for k, s in enumerate(sources):
        infos[s["id"]] = probe(s["path"])
        if s.get("render_path"):
            ri = probe(s["render_path"], with_hash=False)
            s["render_info"] = ri.to_dict()
            if abs(ri.duration - infos[s["id"]].duration) > 1.0:
                log.warning("Bản Video2X của %s lệch thời lượng %.2fs so với bản gốc",
                            s["id"], ri.duration - infos[s["id"]].duration)
        rep(0, (k + 1) / len(sources), Path(s["path"]).name)
    log.info("Edit: %s %dx%d %.3ffps %.2fs", Path(edit_path).name, edit_info.width, edit_info.height,
             edit_info.fps, edit_info.duration)
    for sid, i in infos.items():
        log.info("Nguồn %s: %s %dx%d %s fps %.2fs, audio=%s", sid, Path(i.path).name, i.width, i.height,
                 i.fps_fraction, i.duration, [(a["index"], a["language"]) for a in i.audio_streams])
    job = _job_dir(out_dir, edit_info.content_hash, [i.content_hash for i in infos.values()], cfg)
    cache_dir = Path(cfg.cache_dir)

    # 2. fingerprint nguồn (cache)
    src_fps = {}
    for k, s in enumerate(sources):
        sid = s["id"]
        src_fps[sid] = build_fingerprint(
            infos[sid], "source", cfg.analysis, cache_dir,
            progress=lambda f, m="", k=k: rep(1, (k + f) / len(sources), f"{sid}: {m}"), cancel=cancel)

    # 3. cảnh
    rep(2, 0.0, "giải mã toàn bộ khung Douyin")
    edit_fp = build_fingerprint(edit_info, "edit", cfg.analysis, cache_dir,
                                progress=lambda f, m="": rep(2, 0.6 * f, m), cancel=cancel)
    rep(2, 0.7, "PySceneDetect + kiểm tra điểm cắt")
    scenes, scene_dbg = detect_scenes(edit_path, edit_fp, cfg.analysis.scenedetect_threshold,
                                      cfg.analysis.own_cut_ratio, cfg.analysis.min_scene_frames)
    content = detect_content_rect(edit_fp)
    rep(2, 1.0, f"{len(scenes)} cảnh")

    # 4-7. so khớp (checkpoint để tiếp tục khi bị gián đoạn)
    ckpt = job / "segments.pkl"
    segs = None
    if ckpt.exists():
        try:
            segs = pickle.loads(ckpt.read_bytes())
            log.info("Tiếp tục: dùng kết quả so khớp đã lưu (%s)", ckpt)
        except Exception:
            segs = None
    if segs is None:
        matcher = Matcher(edit_fp, src_fps, cfg.analysis, content, edit_path,
                          {s["id"]: s["path"] for s in sources},
                          progress=lambda f, m="": rep(3, f, m), cancel=cancel)
        segs = matcher.run(scenes)
        ckpt.write_bytes(pickle.dumps(segs))
    rep(3, 1.0, "xong")

    # 8. kiểm định + xuất bảng
    rep(4, 0.2, "ghi bảng cắt")
    manifest = build_manifest(sources, infos, src_fps)
    seg_rows, frame_rows = segment_rows(segs, edit_fp, edit_info, src_fps, manifest)
    edit_meta = edit_info.to_dict()
    edit_meta.update({"n_frames": edit_fp.n, "first_pts_time": float(edit_fp.pts[0]),
                      "content_rect": content, "fingerprint_cache": edit_fp.root.name})
    analysis_meta = {"config": cfg.analysis.__dict__, "scenes_detected": len(scenes),
                     "suspicious_cuts": scene_dbg["suspicious"], "seconds": round(time.time() - t_start, 1)}
    doc = write_outputs(out_dir, seg_rows, frame_rows, manifest, edit_meta, analysis_meta)
    summ = doc["summary"]
    log.info("Kết quả: %d/%d khung XANH, %d VÀNG, %d ĐỎ (%d cảnh)", summ["frames_green"], summ["frames_total"],
             summ["frames_yellow"], summ["frames_red"], summ["segments_total"])
    rep(4, 1.0, "xong")
    return {"doc": doc, "out_dir": str(out_dir), "job_dir": str(job), "segments": segs,
            "edit_fp": edit_fp, "src_fps": src_fps, "manifest": manifest, "edit_info": edit_info,
            "sources": sources}


# ====================================================================== dựng + QA

def _plan_segments_from_analysis(segs, edit_fp, allow_unverified: bool) -> tuple[list[dict], list]:
    out, skipped = [], []
    last = float(edit_fp.pts[-1]) + 1.0 / edit_fp.fps
    for k, sg in enumerate(segs, 1):
        t0 = float(edit_fp.pts[sg.start])
        t1 = float(edit_fp.pts[sg.end]) if sg.end < edit_fp.n else last
        d = {"segment_id": k, "status": sg.status, "edit_t0": t0, "edit_t1": t1, "n_edit": sg.n,
             "source_id": sg.sid, "render": False}
        if sg.sid is not None:
            d.update({"a": float(sg.a), "speed": float(sg.speed), "t_ref": float(sg.t_ref), "method": sg.method,
                      "flip": bool(sg.geom.flip), "crop_norm": sg.geom.source_crop(), "geometry": sg.geom})
        if sg.sid is None:
            skipped.append((k, "ĐỎ/UNMATCHED"))
        elif sg.status == GREEN or allow_unverified:
            d["render"] = True
        else:
            skipped.append((k, sg.status))
        out.append(d)
    return out, skipped


def _sources_for_render(manifest: dict, src_fps: dict) -> dict:
    out = {}
    for sid, m in manifest["sources"].items():
        d = dict(m)
        d["_pts"] = np.asarray(src_fps[sid].pts) if sid in src_fps else None
        out[sid] = d
    return out


def render_and_qa(segments: list[dict], sources: dict, edit_meta: dict, cfg: AppConfig, out_dir: Path,
                  job: Path, verified: bool, edit_fp=None, src_fps=None, report=None, cancel=None,
                  step_render: int = 5, step_qa: int = 6) -> tuple[dict, dict]:
    from .qa import verify_output
    from .render import RenderPlan, prepare_sources, render

    rep = Reporter(report)
    prepare_sources(sources)
    plan = RenderPlan(segments, sources, edit_meta, cfg.render)
    name = "THANH_PHAM_2K.mp4" if verified else "THANH_PHAM_2K_CHUA_XAC_MINH.mp4"
    out_path = out_dir / name
    log.info("Dựng %dx%d @ %s fps (%s), %d khung, crop=%s", plan.width, plan.height, plan.fps, plan.fps_mode,
             plan.total_frames, cfg.render.crop_mode)
    rinfo = render(plan, out_path, job / "render", progress=lambda f, m="": rep(step_render, f, m), cancel=cancel)
    rinfo["skipped"] = [s["segment_id"] for s in segments if not s.get("render")]
    rep(step_qa, 0.0, "so sánh thành phẩm")
    qa = verify_output(str(out_path), plan, rinfo, cfg.analysis, edit_fp, src_fps or {},
                       progress=lambda f, m="": rep(step_qa, f, m))
    if not verified and out_path.exists():
        log.warning("Thành phẩm CHƯA XÁC MINH: %s", out_path)
    return rinfo, qa


def _segment_thumbs(segs, edit_fp, src_fps) -> dict:
    from .qa import _jpeg_b64

    th = {}
    for k, sg in enumerate(segs, 1):
        mid = sg.start + sg.n // 2
        d = {"edit": _jpeg_b64(edit_fp.thumbs[mid], 150)}
        if sg.sid is not None:
            d["nguon"] = _jpeg_b64(src_fps[sg.sid].thumbs[int(sg.frames[sg.n // 2])], 150)
        th[k] = d
    return th


def run_mode_a(edit_path: str, sources: list, out_dir: str | Path, cfg: AppConfig | None = None,
               report=None, cancel: threading.Event | None = None, do_render: bool = True) -> dict:
    """CHẾ ĐỘ A - PHÂN TÍCH & CẮT TỰ ĐỘNG (một nút)."""
    from .qa import write_report

    cfg = cfg or AppConfig()
    out_dir = Path(out_dir)
    res = analyze(edit_path, sources, out_dir, cfg, report, cancel)
    doc, segs = res["doc"], res["segments"]
    verified = bool(doc["summary"]["fully_verified"])
    rinfo = qa = None
    if do_render:
        # bật nội suy = chấp nhận dựng đoạn VÀNG (file vẫn mang hậu tố _CHUA_XAC_MINH)
        allow = cfg.render.allow_unverified or cfg.analysis.interpolate_unmatched
        if verified or allow:
            plan_segs, skipped = _plan_segments_from_analysis(segs, res["edit_fp"], allow)
            if any(s["render"] for s in plan_segs):
                edit_meta = dict(doc["edit"])
                rinfo, qa = render_and_qa(plan_segs, _sources_for_render(res["manifest"], res["src_fps"]),
                                          edit_meta, cfg, out_dir, Path(res["job_dir"]), verified,
                                          res["edit_fp"], res["src_fps"], report, cancel)
                if skipped:
                    log.warning("Các đoạn không được dựng (không có nguồn xác định): %s", skipped)
        else:
            log.warning("KHÔNG xuất thành phẩm đã xác minh: còn %d khung VÀNG, %d khung ĐỎ. "
                        "Xem KIEM_TRA_THU_CONG.csv, sửa bảng rồi dùng Chế độ B, hoặc bật 'Cho phép xuất bản nháp'.",
                        doc["summary"]["frames_yellow"], doc["summary"]["frames_red"])
    thumbs = _segment_thumbs(segs, res["edit_fp"], res["src_fps"])
    write_report(out_dir / "QA_REPORT.html", doc, qa, rinfo, "A - Phân tích & cắt tự động", thumbs)
    if qa is not None:
        (out_dir / "QA_RESULT.json").write_text(json.dumps(
            {k: v for k, v in qa.items() if k != "segments"} |
            {"segments": [{kk: vv for kk, vv in s.items() if not kk.startswith("thumb")} for s in qa["segments"]]},
            ensure_ascii=False, indent=1, default=str), "utf-8")
    return {"summary": doc["summary"], "verified": verified and (qa is None or qa["status"] == GREEN),
            "render": rinfo, "qa": qa, "out_dir": str(out_dir), "report": str(out_dir / "QA_REPORT.html"),
            "segments": doc["segments"]}


# ====================================================================== CHẾ ĐỘ B

def verify_source_identity(sid: str, info, fp, manifest_src: dict | None) -> tuple[bool, str]:
    """Xác minh file nguồn đưa vào đúng là nguồn trong bảng (băm nội dung hoặc vân tay pHash)."""
    from .fingerprint import hamming

    if not manifest_src:
        return True, "không có manifest - chỉ kiểm tra metadata"
    if manifest_src.get("content_hash") == info.content_hash:
        return True, "trùng băm nội dung"
    samples = manifest_src.get("phash_samples") or []
    if not samples:
        return False, "khác băm nội dung và không có vân tay để đối chiếu"
    good = 0
    for smp in samples:
        i = fp.frame_at_time(float(smp["pts_time"]))
        if hamming(fp.phashes[i], np.uint64(int(smp["phash"], 16))) <= 12:
            good += 1
    ok = good >= 0.8 * len(samples)
    return ok, f"vân tay pHash khớp {good}/{len(samples)} mẫu"


def run_mode_b(table_path: str, sources: list, out_dir: str | Path, cfg: AppConfig | None = None,
               report=None, cancel: threading.Event | None = None) -> dict:
    """CHẾ ĐỘ B - CẮT THEO BẢNG CÓ SẴN (không cần video Douyin)."""
    from .qa import write_report
    from .table import MANUAL_OK, MANUAL_SKIP, read_table

    cfg = cfg or AppConfig()
    rep = Reporter(report)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tbl = read_table(table_path)
    meta = tbl["meta"] or {}
    man_path = Path(table_path).with_name("SOURCE_MANIFEST.json")
    manifest = json.loads(man_path.read_text("utf-8")) if man_path.exists() else {"sources": meta.get("sources") or {}}
    rows = tbl["segments"]
    need_ids = sorted({r["source_id"] for r in rows if r["source_id"]})
    sources = normalize_sources(sources)
    rep(0, 0.0, "đọc metadata nguồn")
    infos = {}
    for s in sources:
        infos[s["path"]] = probe(s["path"])
    # gán source_id: theo băm nội dung trong manifest, sau đó theo id người dùng nhập/thứ tự
    assign: dict[str, dict] = {}
    for s in sources:
        h = infos[s["path"]].content_hash
        for sid, m in manifest.get("sources", {}).items():
            if m.get("content_hash") == h and sid not in assign:
                assign[sid] = s
                break
    for s in sources:
        if s in assign.values():
            continue
        if s.get("id") in need_ids and s["id"] not in assign:
            assign[s["id"]] = s
    missing = [sid for sid in need_ids if sid not in assign]
    if missing:
        raise RuntimeError(f"Thiếu video nguồn cho source_id: {', '.join(missing)}. "
                           f"Hãy thêm đủ video gốc (bảng cần: {', '.join(need_ids)}).")
    src_fps, src_render = {}, {}
    checks = {}
    for k, sid in enumerate(need_ids):
        s = assign[sid]
        info = infos[s["path"]]
        fp = build_fingerprint(info, "source", cfg.analysis, Path(cfg.cache_dir),
                               progress=lambda f, m="", k=k: rep(1, (k + f) / len(need_ids), f"{sid}: {m}"),
                               cancel=cancel)
        ok, why = verify_source_identity(sid, info, fp, manifest.get("sources", {}).get(sid))
        checks[sid] = {"ok": ok, "detail": why, "path": s["path"]}
        log.info("Xác minh nguồn %s (%s): %s - %s", sid, Path(s["path"]).name, "OK" if ok else "SAI", why)
        if not ok:
            raise RuntimeError(f"Video {Path(s['path']).name} KHÔNG khớp nguồn {sid} trong bảng ({why}).")
        src_fps[sid] = fp
        m = dict(manifest.get("sources", {}).get(sid) or {})
        a = info.pick_audio(cfg.render.audio_lang, s.get("audio_stream"))
        m.update({"source_id": sid, "path": str(Path(s["path"]).resolve()), "content_hash": info.content_hash,
                  "fps": info.fps, "fps_fraction": info.fps_fraction, "time_base": info.time_base,
                  "start_time": info.start_time, "width": info.width, "height": info.height,
                  "display_aspect": info.display_aspect, "n_frames": fp.n, "audio_stream": a,
                  "render_path": s.get("render_path") or (m.get("render_path") if m.get("render_path")
                                                          and Path(m["render_path"]).exists() else None)})
        # độ trễ audio: giá trị người dùng nhập (khác 0) được ưu tiên, nếu không thì theo bảng
        if float(s.get("audio_offset_ms") or 0.0) != 0.0 or "audio_offset_ms" not in m:
            m["audio_offset_ms"] = float(s.get("audio_offset_ms") or 0.0)
        m["_pts"] = np.asarray(fp.pts)
        src_render[sid] = m
    # dựng kế hoạch từ bảng
    segments, skipped, verified = [], [], True
    for r in rows:
        act = r["manual_action"]
        t0, t1 = r["edit_time_start"], r["edit_time_end"]
        if t1 is None and t0 is not None and r["duration"] is not None:
            t1 = t0 + r["duration"]
        if t0 is None or t1 is None:
            raise RuntimeError(f"Dòng {r['segment_id']}: thiếu edit_time_start/edit_time_end")
        d = {"segment_id": r["segment_id"], "status": r["status"], "edit_t0": t0, "edit_t1": t1,
             "n_edit": (r["edit_frame_end"] - r["edit_frame_start"] + 1) if r["edit_frame_start"] is not None else 1,
             "source_id": r["source_id"], "render": False}
        if act == MANUAL_SKIP or r["source_id"] is None:
            skipped.append((r["segment_id"], "BO_QUA" if act == MANUAL_SKIP else "UNMATCHED"))
            if act != MANUAL_SKIP:
                verified = False
            segments.append(d)
            continue
        m = src_render[r["source_id"]]
        if r["source_frame_exact"] is not None:
            f_exact = float(r["source_frame_exact"])
        elif r["source_frame_start"] is not None:
            f_exact = float(r["source_frame_start"])
        elif r["source_time_start"] is not None:  # bảng chỉ ghi thời gian nguồn
            f_exact = float(src_fps[r["source_id"]].frame_at_time(float(r["source_time_start"])))
        else:
            raise RuntimeError(f"Dòng {r['segment_id']}: thiếu source_frame_start/source_time_start")
        crop = r["crop"]
        crop_norm = (crop[0] / m["width"], crop[1] / m["height"], crop[2] / m["width"], crop[3] / m["height"]) \
            if all(c is not None for c in crop) else None
        d.update({"a": float(f_exact), "speed": float(r["speed"]), "t_ref": float(t0), "flip": r["flip"],
                  "crop_norm": crop_norm, "geometry": r["geometry"], "render": True,
                  "method": "freeze" if float(r["speed"] or 0) <= 0 else r["method"]})
        if not (r["status"] == GREEN or act == MANUAL_OK):
            verified = False
        segments.append(d)
    if not any(s["render"] for s in segments):
        raise RuntimeError("Bảng không có đoạn nào để dựng")
    if not verified and not cfg.render.allow_unverified:
        bad = [s["segment_id"] for s in segments if s["status"] != GREEN]
        raise RuntimeError(f"Bảng còn đoạn chưa kiểm định ({bad[:20]}). Ghi 'OK' hoặc 'BO_QUA' vào cột "
                           "manual_action sau khi kiểm tra thủ công, hoặc bật 'Cho phép xuất bản nháp'.")
    edit_meta = meta.get("edit") or {}
    edit_meta.setdefault("fps_fraction", "30/1")
    edit_meta.setdefault("first_pts_time", min(s["edit_t0"] for s in segments))
    job = out_dir / ".afm_job" / ("B_" + hashlib.sha1(Path(table_path).read_bytes()).hexdigest()[:12])
    rinfo, qa = render_and_qa(segments, src_render, edit_meta, cfg, out_dir, job, verified, None, src_fps,
                              report, cancel)
    rinfo["source_checks"] = checks
    doc = json.loads(Path(table_path).with_suffix(".json").read_text("utf-8-sig")) \
        if Path(table_path).with_suffix(".json").exists() else {"segments": [], "summary": {}}
    if Path(table_path).suffix.lower() == ".csv" or not doc.get("segments"):
        doc["segments"] = [{"segment_id": r["segment_id"], "status": r["status"], "edit_frame_start": r["edit_frame_start"],
                            "edit_frame_end": r["edit_frame_end"], "edit_time_start": r["edit_time_start"] or 0,
                            "edit_time_end": r["edit_time_end"] or 0, "source_id": r["source_id"] or "UNMATCHED",
                            "source_frame_start": r["source_frame_start"], "speed": r["speed"], "flip": int(r["flip"]),
                            "score": r["score"], "reasons": (r["reasons"] or "") + (f" | THỦ CÔNG: {r['manual_action']}"
                                                                                  if r["manual_action"] else "")}
                           for r in rows]
    doc["summary"] = dict(doc.get("summary") or {})
    doc["summary"]["fully_verified"] = verified
    doc["sources"] = {sid: {k: v for k, v in m.items() if not k.startswith("_")} for sid, m in src_render.items()}
    write_report(out_dir / "QA_REPORT.html", doc, qa, rinfo, "B - Cắt theo bảng có sẵn")
    return {"verified": verified and qa["status"] == GREEN, "render": rinfo, "qa": qa, "out_dir": str(out_dir),
            "report": str(out_dir / "QA_REPORT.html"), "skipped": skipped}
