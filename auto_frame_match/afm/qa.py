"""Bước 11: kiểm tra thành phẩm (so với bản Douyin hoặc với nguồn theo bảng) + QA_REPORT.html."""

from __future__ import annotations

import base64
import html
import json
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np

from .align import Canvas, Geometry, pair_block_sims
from .config import TOOL_VERSION, AnalysisConfig
from .fingerprint import Fingerprint
from .log import get_logger
from .matcher import GREEN, RED, YELLOW
from .media import decode_thumbs, extract_audio_pcm, probe, thumb_size

log = get_logger()


def _inv(g: Geometry) -> Geometry:
    return Geometry(1 / g.sx, 1 / g.sy, -g.tx / g.sx, -g.ty / g.sy)


def out_to_source(crop_fit, flip: bool) -> Geometry:
    x, y, w, h = crop_fit
    if flip:
        return Geometry(-w, h, x + w, y)
    return Geometry(w, h, x, y)


def out_to_edit(edit_geom: Geometry, crop_fit, flip: bool) -> Geometry:
    """Ghép: khung xuất -> nguồn -> edit."""
    a = out_to_source(crop_fit, flip)  # out -> source
    b = _inv(edit_geom)  # source -> edit
    return Geometry(b.sx * a.sx, b.sy * a.sy, b.sx * a.tx + b.tx, b.sy * a.ty + b.ty)


def _sims(canvas: Canvas, outs, refs, cfg):
    E, Es = canvas.blocks(outs, "edit")
    S, Ss = canvas.blocks(refs, "source")
    sel, ver = pair_block_sims(E, Es, S, Ss, cfg)
    return sel.astype(np.float64), ver.astype(np.float64)


def _jpeg_b64(img: np.ndarray, width: int = 200) -> str:
    h, w = img.shape[:2]
    img = cv2.resize(img, (width, max(2, int(h * width / w))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return base64.b64encode(buf.tobytes()).decode() if ok else ""


def _xcorr_lag(a: np.ndarray, b: np.ndarray, rate: int, max_ms: float = 250) -> tuple[float, float]:
    n = min(len(a), len(b))
    if n < rate // 10:
        return 0.0, 0.0
    a, b = a[:n] - a[:n].mean(), b[:n] - b[:n].mean()
    if np.std(a) < 1e-4 or np.std(b) < 1e-4:
        return 0.0, 1.0  # cả hai im lặng
    size = 1 << int(np.ceil(np.log2(2 * n)))
    c = np.fft.irfft(np.fft.rfft(a, size) * np.conj(np.fft.rfft(b, size)), size)
    m = int(max_ms / 1000 * rate)
    cand = np.concatenate([c[:m + 1], c[-m:]])
    lags = np.concatenate([np.arange(m + 1), np.arange(-m, 0)])
    k = int(np.argmax(cand))
    peak = float(cand[k] / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))
    return float(lags[k] / rate * 1000), peak


def verify_output(out_path: str, plan, render_info: dict, cfg: AnalysisConfig, edit_fp: Fingerprint | None,
                  src_fps: dict[str, Fingerprint], progress=None) -> dict:
    """So sánh thành phẩm từng khung. Tham chiếu: bản Douyin (Chế độ A) hoặc nguồn theo bảng (Chế độ B)."""
    info = probe(out_path, with_hash=False)
    size = thumb_size(info.width / info.height, cfg.edit_thumb_area)
    with tempfile.TemporaryDirectory() as td:
        tf = Path(td) / "out.u8"
        n, pts = decode_thumbs(out_path, size, tf, plan.total_frames, use_gpu=cfg.use_gpu_decode)
        thumbs = np.fromfile(tf, np.uint8).reshape(n, size[1], size[0])
    issues_global = []
    if n != plan.total_frames:
        issues_global.append(f"Số khung thành phẩm {n} khác dự kiến {plan.total_frames}")
    exp_dur = plan.duration
    if abs(info.duration - exp_dur) > 1.5 / float(plan.fps):
        issues_global.append(f"Lệch thời lượng: {info.duration:.3f}s so với dự kiến {exp_dur:.3f}s")
    if edit_fp is not None and all(s.get("render") for s in plan.segments):
        edit_dur = float(edit_fp.pts[-1] - edit_fp.pts[0]) + 1 / edit_fp.fps
        if abs(exp_dur - edit_dur) > 1.5 / float(plan.fps):
            issues_global.append(f"Lệch thời lượng so với bản Douyin: {exp_dur:.3f}s vs {edit_dur:.3f}s")
    if not info.audio_streams:
        issues_global.append("Thành phẩm không có âm thanh")

    ref_mode = "douyin" if edit_fp is not None else "nguon"
    seg_results = []
    used = [s for s in plan.segments if s.get("render")]
    for si, s in enumerate(used):
        if progress:
            progress(si / max(1, len(used)), f"QA đoạn {s['segment_id']}")
        o0, o1 = s["out_start"], min(s["out_end"], n)
        res = {"segment_id": s["segment_id"], "source_id": s["source_id"], "out_start": o0, "out_end": o1,
               "issues": [], "status": GREEN}
        if o1 <= o0:
            res["issues"].append("Thiếu toàn bộ khung của đoạn")
            res["status"] = RED
            seg_results.append(res)
            continue
        if s.get("method") in ("interp", "freeze"):
            res["issues"].append("NỘI SUY/GIỮ KHUNG - không phải kết quả so khớp, không đối chiếu được")
            res["status"] = YELLOW
            seg_results.append(res)
            continue
        src = plan.sources[s["source_id"]]
        rw, rh = src["_render_wh"]
        crop_fit = plan.crop_px(s, rw, rh)[4]
        ks = np.arange(o0, o1)
        outs = [thumbs[k] for k in ks]
        if ref_mode == "douyin":
            g = s.get("geometry")
            if g is None:
                res["issues"].append("Thiếu hình học để so với bản Douyin")
                res["status"] = YELLOW
                seg_results.append(res)
                continue
            # chuẩn đối chiếu = hình học DỰ KIẾN từ phân tích (không dùng cờ lật của bản dựng)
            geom = out_to_edit(g, crop_fit, g.flip)
            ref_fp = edit_fp
            e_lo = edit_fp.frame_at_time(s["edit_t0"] + 1e-4)
            e_hi = edit_fp.frame_at_time(s["edit_t1"] - 1e-4)
            # khung edit đang hiển thị tại thời điểm của khung xuất (kẹp trong phạm vi đoạn)
            ref_idx = np.array([min(max(edit_fp.frame_at_time(float(plan.edit_time(s, k + 0.25))), e_lo), e_hi)
                                for k in ks])
        else:
            geom = out_to_source(crop_fit, s["flip"])
            ref_fp = src_fps[s["source_id"]]
            ref_idx = plan.source_frames(s)[: len(ks)]
        try:
            canvas = Canvas(geom, size, ref_fp.size, cfg)
        except ValueError:
            res["issues"].append("Hình học không hợp lệ")
            res["status"] = RED
            seg_results.append(res)
            continue
        sel0, ver0 = _sims(canvas, outs, [ref_fp.thumbs[i] for i in ref_idx], cfg)
        # lệch khung: so với khung tham chiếu lân cận
        # Khi fps xuất khác fps bản Douyin, lượng tử hóa thời gian cho phép lệch tối đa 1 khung tham chiếu.
        tol = 1 if (ref_mode == "douyin" and abs(float(plan.fps) - edit_fp.fps) > 1e-3) else 0
        sel_d, ver_d = {}, {}
        for d in (-2, -1, 1, 2):
            idx = np.clip(ref_idx + d, 0, ref_fp.n - 1)
            if ref_mode == "douyin":
                idx = np.clip(idx, e_lo, e_hi)
            sel_d[d], ver_d[d] = _sims(canvas, outs, [ref_fp.thumbs[i] for i in idx], cfg)
        if tol:
            for d in (-1, 1):
                ver0 = np.fmax(ver0, ver_d[d])
                sel0 = np.fmax(sel0, sel_d[d])
        cand = [d for d in sel_d if abs(d) > tol]
        best_other = np.nanmax(np.stack([sel_d[d] for d in cand]), axis=0)
        better = (best_other > np.nan_to_num(sel0, nan=-1) + 0.03) & (np.nan_to_num(ver0, nan=1) < cfg.frame_ok)
        shift_bad = int(better.sum())
        shifted_list = [int(ks[i]) for i in np.where(better)[0][:20]]
        # lật gương
        try:
            cflip = Canvas(geom.flipped(), size, ref_fp.size, cfg)
            pick = np.linspace(0, len(outs) - 1, min(8, len(outs))).astype(int)
            self_f, _ = _sims(cflip, [outs[i] for i in pick], [ref_fp.thumbs[ref_idx[i]] for i in pick], cfg)
            flip_wrong = np.nanmedian(self_f) > np.nanmedian(sel0[pick]) + 0.05
        except ValueError:
            flip_wrong = False
        # lặp khung không mong muốn: 2 khung xuất liền nhau giống hệt trong khi 2 khung nguồn dự kiến khác nhau
        dup = 0
        sfp = src_fps.get(s["source_id"])
        if sfp is not None:
            exp = plan.source_frames(s)[: len(outs)]
            for k in range(1, len(outs)):
                if exp[k] == exp[k - 1]:
                    continue
                if np.mean(np.abs(outs[k].astype(np.int16) - outs[k - 1])) < 0.4:
                    r1, r0 = sfp.thumbs[exp[k]], sfp.thumbs[exp[k - 1]]
                    if np.mean(np.abs(r1.astype(np.int16) - r0)) > 2.0:
                        dup += 1
        valid = ver0[np.isfinite(ver0)]
        med = float(np.median(valid)) if len(valid) else -1
        ok_ratio = float(np.mean(valid >= cfg.frame_ok)) if len(valid) else 0
        res.update({"median": round(med, 4), "ok_ratio": round(ok_ratio, 4), "min": round(float(valid.min()), 4)
                    if len(valid) else None, "shifted_frames": shift_bad, "shifted_list": shifted_list, "dup_frames": dup,
                    "ver_at_shift": [round(float(ver0[i]), 3) for i in np.where(better)[0][:5]],
                    "flip_wrong": bool(flip_wrong)})
        if med < cfg.scene_match:
            res["issues"].append(f"Cắt nhầm cảnh/nhầm nguồn (điểm {med:.3f})")
            res["status"] = RED
        elif med < cfg.scene_green or ok_ratio < cfg.min_ok_ratio:
            res["issues"].append(f"Độ khớp thấp (median {med:.3f}, {ok_ratio:.0%} khung đạt)")
            res["status"] = YELLOW
        if shift_bad:
            res["issues"].append(f"{shift_bad} khung lệch/thiếu/sai thứ tự so với tham chiếu")
            res["status"] = RED if shift_bad > max(2, 0.1 * len(outs)) else max(res["status"], YELLOW, key=_rank)
        if dup:
            res["issues"].append(f"{dup} khung bị lặp không mong muốn")
            res["status"] = max(res["status"], YELLOW, key=_rank)
        if flip_wrong:
            res["issues"].append("Sai lật gương")
            res["status"] = RED
        # thumbnail để kiểm tra bằng mắt
        mid = len(outs) // 2
        res["thumb_out"] = _jpeg_b64(outs[mid])
        res["thumb_ref"] = _jpeg_b64(ref_fp.thumbs[ref_idx[mid]])
        seg_results.append(res)

    # âm thanh: so với âm thanh nguồn tại source_pts (bắt lệch tiếng ở điểm nối/mã hóa)
    audio = []
    rate = 16000
    out_a = None
    if info.audio_streams:
        out_a = extract_audio_pcm(out_path, info.audio_streams[0]["index"], info.start_time, info.duration + 1, rate)
    for s, ri in zip(used, render_info.get("segments", [])):
        if out_a is None or ri.get("audio_start") is None or s.get("method") in ("interp", "freeze"):
            continue
        src = plan.sources[s["source_id"]]
        dur = (s["out_end"] - s["out_start"]) / float(plan.fps)
        take = min(dur, 3.0)
        off = max(0.0, dur / 2 - take / 2)
        o = out_a[int((s["out_start"] / float(plan.fps) + off) * rate):][: int(take * rate)]
        from .render import _atempo_chain

        ref = extract_audio_pcm(src["path"], ri["audio_stream"], ri["audio_start"] + off * s["speed"],
                                take * s["speed"], rate, extra_filters=_atempo_chain(s["speed"]))
        lag, peak = _xcorr_lag(o, ref, rate)
        a = {"segment_id": s["segment_id"], "lag_ms": round(lag, 2), "corr": round(peak, 3), "ok": True}
        if abs(lag) > 1000 / float(plan.fps) / 2 or peak < 0.5:
            a["ok"] = False
        audio.append(a)
        for r in seg_results:
            if r["segment_id"] == s["segment_id"] and not a["ok"]:
                r["issues"].append(f"Lệch âm thanh {lag:.1f} ms (tương quan {peak:.2f})")
                r["status"] = max(r["status"], YELLOW, key=_rank)
    if progress:
        progress(1.0, "xong QA")
    worst = max([r["status"] for r in seg_results] + [GREEN], key=_rank)
    if issues_global:
        worst = max(worst, YELLOW, key=_rank)
    return {"reference": ref_mode, "output": out_path, "frames": n, "duration": info.duration,
            "expected_frames": plan.total_frames, "expected_duration": exp_dur, "width": info.width,
            "height": info.height, "fps": info.fps_fraction, "global_issues": issues_global,
            "segments": seg_results, "audio": audio, "status": worst}


def _rank(s: str) -> int:
    return {GREEN: 0, YELLOW: 1, RED: 2}.get(s, 2)


# ------------------------------------------------------------------ báo cáo HTML

_CSS = """
:root{--bg:#f6f7f9;--fg:#1d232b;--card:#fff;--line:#dfe3e8;--g:#1f9d55;--y:#d9a400;--r:#d64545;--mut:#66707c}
@media (prefers-color-scheme:dark){:root{--bg:#14171b;--fg:#e7eaee;--card:#1d2127;--line:#323844;--mut:#9aa4b1}}
body{background:var(--bg);color:var(--fg);font:14px/1.45 "Segoe UI",system-ui,sans-serif;margin:0;padding:16px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:22px 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin:10px 0}
.banner{font-size:18px;font-weight:600;padding:14px;border-radius:10px;color:#fff}
.XANH{background:var(--g)}.VANG{background:var(--y);color:#111}.DO{background:var(--r)}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid var(--line);padding:6px;text-align:left;vertical-align:top}
.pill{display:inline-block;padding:1px 8px;border-radius:9px;font-size:12px;font-weight:600}
.mut{color:var(--mut)}.grid{display:flex;gap:14px;flex-wrap:wrap}.kv b{display:inline-block;min-width:150px}
img{border-radius:4px;border:1px solid var(--line)}.wrap{overflow-x:auto}
"""

_LABEL = {GREEN: "XANH", YELLOW: "VÀNG", RED: "ĐỎ"}


def _pill(st: str) -> str:
    return f'<span class="pill {st}">{_LABEL.get(st, st)}</span>'


def write_report(path: Path, doc: dict, qa: dict | None, render_info: dict | None, mode: str,
                 thumbs: dict | None = None) -> None:
    summ = doc.get("summary", {})
    verified = bool(summ.get("fully_verified")) and (qa is None or qa.get("status") == GREEN)
    if qa is not None and qa.get("status") == RED:
        overall = RED
    elif verified:
        overall = GREEN
    else:
        overall = YELLOW if summ.get("frames_red", 0) == 0 else RED
    if verified:
        msg = "ĐÃ XÁC MINH: mọi khung khớp nguồn và thành phẩm qua kiểm định."
    else:
        pct = 100 * summ.get("verified_ratio", 0)
        msg = (f"CHƯA XÁC MINH 100% — {pct:.1f}% khung đạt XANH. "
               "Các đoạn VÀNG/ĐỎ cần kiểm tra thủ công (xem KIEM_TRA_THU_CONG.csv).")
    h = [f"<!doctype html><html lang='vi'><head><meta charset='utf-8'><meta name='viewport' "
         f"content='width=device-width,initial-scale=1'><title>QA Report</title><style>{_CSS}</style></head><body>",
         f"<h1>QA REPORT — AUTO FRAME MATCH 2K</h1><div class='mut'>Phiên bản {TOOL_VERSION} · "
         f"{time.strftime('%Y-%m-%d %H:%M:%S')} · Chế độ {html.escape(mode)}</div>",
         f"<div class='banner {overall}' style='margin-top:12px'>{html.escape(msg)}</div>"]
    h.append("<div class='card grid'>")
    for k, label in (("frames_total", "Tổng khung edit"), ("frames_green", "Khung XANH"),
                     ("frames_yellow", "Khung VÀNG"), ("frames_red", "Khung ĐỎ"),
                     ("segments_total", "Số đoạn"), ("segments_green", "Đoạn XANH"),
                     ("segments_yellow", "Đoạn VÀNG"), ("segments_red", "Đoạn ĐỎ")):
        h.append(f"<div class='kv'><b>{label}</b>{summ.get(k, '-')}</div>")
    h.append("</div>")
    if render_info:
        h.append("<h2>Thành phẩm</h2><div class='card'>")
        h.append(f"<div class='kv'><b>File</b>{html.escape(str(render_info.get('output')))}</div>"
                 f"<div class='kv'><b>Kích thước</b>{render_info.get('size')}</div>"
                 f"<div class='kv'><b>FPS</b>{render_info.get('fps')}</div>"
                 f"<div class='kv'><b>Encoder</b>{render_info.get('encoder')}</div>")
        if render_info.get("skipped"):
            h.append(f"<div class='kv'><b>Đoạn KHÔNG dựng</b>{html.escape(str(render_info['skipped']))}</div>")
        h.append("</div>")
    if qa:
        h.append(f"<h2>Kiểm tra thành phẩm (so với {'bản Douyin' if qa['reference'] == 'douyin' else 'nguồn theo bảng'}) "
                 f"{_pill(qa['status'])}</h2><div class='card'>")
        h.append(f"<div class='kv'><b>Khung</b>{qa['frames']} / dự kiến {qa['expected_frames']}</div>"
                 f"<div class='kv'><b>Thời lượng</b>{qa['duration']:.3f}s / dự kiến {qa['expected_duration']:.3f}s</div>"
                 f"<div class='kv'><b>Độ phân giải</b>{qa['width']}x{qa['height']} @ {qa['fps']}</div>")
        for iss in qa["global_issues"]:
            h.append(f"<div style='color:var(--r)'>⚠ {html.escape(iss)}</div>")
        h.append("<div class='wrap'><table><tr><th>Đoạn</th><th>Nguồn</th><th>Trạng thái</th><th>Median</th>"
                 "<th>Khung đạt</th><th>Lệch khung</th><th>Lặp</th><th>Âm thanh</th><th>Vấn đề</th>"
                 "<th>Tham chiếu</th><th>Thành phẩm</th></tr>")
        amap = {a["segment_id"]: a for a in qa.get("audio", [])}
        for r in qa["segments"]:
            a = amap.get(r["segment_id"])
            atxt = f"{a['lag_ms']} ms / r={a['corr']}" if a else "-"
            h.append(f"<tr><td>{r['segment_id']}</td><td>{r['source_id']}</td><td>{_pill(r['status'])}</td>"
                     f"<td>{r.get('median', '-')}</td><td>{r.get('ok_ratio', '-')}</td>"
                     f"<td>{r.get('shifted_frames', '-')}</td><td>{r.get('dup_frames', '-')}</td><td>{atxt}</td>"
                     f"<td>{html.escape('; '.join(r['issues'])) or '—'}</td>"
                     f"<td>{'<img src=data:image/jpeg;base64,' + r['thumb_ref'] + '>' if r.get('thumb_ref') else ''}</td>"
                     f"<td>{'<img src=data:image/jpeg;base64,' + r['thumb_out'] + '>' if r.get('thumb_out') else ''}</td></tr>")
        h.append("</table></div></div>")
    h.append("<h2>Bảng cắt (theo đoạn)</h2><div class='card wrap'><table><tr><th>#</th><th>TT</th>"
             "<th>Edit khung</th><th>Edit thời gian</th><th>Nguồn</th><th>Khung nguồn</th><th>Tốc độ</th>"
             "<th>Lật</th><th>Crop (px)</th><th>Điểm</th><th>Ghi chú / ứng viên</th><th>Ảnh</th></tr>")
    thumbs = thumbs or {}
    for r in doc.get("segments", []):
        crop = f"{r.get('crop_x', '')},{r.get('crop_y', '')} {r.get('crop_w', '')}x{r.get('crop_h', '')}" \
            if r.get("crop_w") not in (None, "") else "-"
        cands = r.get("candidates") or "[]"
        try:
            cl = json.loads(cands) if isinstance(cands, str) else cands
        except Exception:
            cl = []
        ctxt = "; ".join(f"{c['source_id']}@{c.get('source_frame_start')} ({c['score']})" for c in cl[:3])
        th = thumbs.get(r["segment_id"], {})
        imgs = "".join(f"<img title='{k}' src='data:image/jpeg;base64,{v}'> " for k, v in th.items() if v)
        h.append(f"<tr><td>{r['segment_id']}</td><td>{_pill(r['status'])}</td>"
                 f"<td>{r['edit_frame_start']}–{r['edit_frame_end']}</td>"
                 f"<td>{r['edit_time_start']:.3f}–{r['edit_time_end']:.3f}s</td><td>{html.escape(str(r['source_id']))}</td>"
                 f"<td>{r.get('source_frame_start', '-')}–{r.get('source_frame_end', '-')}</td>"
                 f"<td>{r.get('speed', '-')}</td><td>{'Có' if str(r.get('flip')) == '1' else '-'}</td>"
                 f"<td>{crop}</td><td>{r.get('score', '-')}</td>"
                 f"<td>{html.escape(r.get('reasons') or '')}<div class='mut'>Ứng viên: {html.escape(ctxt) or '—'}</div></td>"
                 f"<td>{imgs}</td></tr>")
    h.append("</table></div>")
    sus = doc.get("analysis", {}).get("suspicious_cuts") or []
    if sus:
        h.append("<h2>Điểm cắt nghi vấn đã dò lại</h2><div class='card wrap'><table><tr><th>Khung</th><th>Ghi chú</th></tr>")
        for s in sus[:300]:
            h.append(f"<tr><td>{s.get('frame')}</td><td>{html.escape(s.get('reason', ''))}"
                     f"{(' → ' + str(s['moved_to'])) if 'moved_to' in s else ''}</td></tr>")
        h.append("</table></div>")
    h.append("<h2>Nguồn</h2><div class='card wrap'><table><tr><th>ID</th><th>File</th><th>FPS</th><th>Timebase</th>"
             "<th>Start</th><th>Độ trễ audio</th><th>Vân tay</th></tr>")
    for sid, s in (doc.get("sources") or {}).items():
        h.append(f"<tr><td>{sid}</td><td>{html.escape(str(s.get('path')))}"
                 f"{('<div class=mut>Video2X: ' + html.escape(str(s.get('render_path'))) + '</div>') if s.get('render_path') else ''}</td>"
                 f"<td>{s.get('fps_fraction')}</td><td>{s.get('time_base')}</td><td>{s.get('start_time')}</td>"
                 f"<td>{s.get('audio_offset_ms')} ms</td><td>{s.get('content_hash')}</td></tr>")
    h.append("</table></div><p class='mut'>Không được coi là chính xác 100% nếu còn bất kỳ đoạn VÀNG/ĐỎ nào.</p>")
    h.append("</body></html>")
    path.write_text("\n".join(h), encoding="utf-8")
