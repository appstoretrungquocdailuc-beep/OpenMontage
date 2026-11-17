# -*- coding: utf-8 -*-
"""bench_v99.py — V9.9 SMART AUTO FASTEST 480p: bằng chứng trước, sửa sau.

Engine:  A = V9_4_SUB2S.py (V9.8.1 ĐANG CHẠY, không sửa)   B = V9_9_CANDIDATE.py (bản ứng viên)   O = V9.7.1 (backup)

Chế độ (đều ghi perf_report/v99_*; KHÔNG bao giờ ghi URL có chữ ký / cookie / token):
  env                 Phiên bản thật đang dùng: python, module yt_dlp (bản tool import) và yt-dlp.exe (bản CMD gọi), ffmpeg, aria2c.
  autopsy [file...]   Đọc telemetry_*.jsonl ĐÃ CÓ trong perf_report (tool tự ghi mỗi lần tải) → dòng thời gian từng HTTP Range của
                      các job chậm: range nào xong cuối, TTFB / thân / host / race-hedge / takeover. Không tải gì thêm.
  mock480             Như bench_v98 + kịch bản 1 range bị treo (S6/S7). SỐ MOCK.
  formats             LIVE: liệt kê format 480p + audio JA mà TỪNG nguồn (native visionos / web_embedded / yt-dlp) thật sự trả về
                      (itag, codec, MiB, có link trực tiếp không). Không tải media.
  live480             LIVE: NGUYÊN pipeline cho từng tổ hợp video+audio (--combos 244+139,397+139,...) → resolve / video / audio /
                      HOT READY / cắt / TOTAL / GET / byte dư / retry-403 / codec file ra. Median + p95.
  live_dl / linkcap   Như bench_v98 (cùng link mỗi vòng, SHA-256).
  cutlab              CỤC BỘ: tạo nguồn VP9 / AV1 / H.264 + AAC / Opus, chạy NGUYÊN pipeline cắt của tool, kiểm từng part bằng giải mã.
  verify_cut <thư mục>  Kiểm TỪNG file part: có video + audio, codec, thời lượng, lệch A/V, giải mã toàn bộ không lỗi, phủ kín nguồn.
"""
import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import bench_v98 as b  # noqa: E402

b.ENGINE_FILES = {"A": ROOT / "V9_4_SUB2S.py", "B": ROOT / "V9_9_CANDIDATE.py", "O": ROOT / "backup" / "V9_7_1_SUB2S_truoc_v98.py"}
b.REPORT_PREFIX = "v99"
MB = 1024 * 1024

# 1 range của audio bị treo trên CẢ 2 host (theo mô tả log thật: audio 9,09 MiB / 24 worker, 8,71 MiB lúc 2,07 s, xong lúc 3,5 s)
b.SCENARIOS.update({
    "S6_astall_fast": dict(note="audio: 1/24 range treo 3 s trên cả 2 host; TTFB bình thường (~0.3 s)",
                           link=900, v_conn=18.6, a_conn=20.0, redirect_ms=60, final_ms=200, alt_ms=260,
                           a_stall={"seg": 17, "workers": 24, "delay": 3.0}),
    "S7_astall_slowttfb": dict(note="audio: 1/24 range treo 3 s trên cả 2 host; đợt ĐẦU của audio chậm (TTFB ~1.3 s: kết nối lạnh + tranh chấp), request sau ~0.25 s (như ttfb_late trong log thật)",
                               link=900, v_conn=18.6, a_conn=20.0, redirect_ms=60, final_ms=200, alt_ms=260,
                               a_final_ms=200, a_alt_ms=260, a_cold={"n": 24, "extra_ms": 1100},
                               a_stall={"seg": 17, "workers": 24, "delay": 3.0}),
    # theo log thật 18/11 01:28-01:29 (V9.9-CANDIDATE): TTFB đợt đầu p50 0.47-0.75 s, request sau 0.04-0.23 s, ~16 Mbps/kết nối,
    # tốc độ truyền tổng ~650 Mbps; vài kết nối chậm (audio: 0.49 MiB mất 701 ms ≈ 5.9 Mbps)
    "S9_tail_real": dict(note="theo log 18/11: đợt đầu lạnh (+0.45 s), sau đó ấm (0.2 s); 16.5 Mbps/kết nối; 1/9 request chỉ 6 Mbps; link 700 Mbps",
                         link=700, v_conn=16.5, a_conn=20.0, redirect_ms=60, final_ms=200, alt_ms=220,
                         v_cold={"n": 64, "extra_ms": 450}, a_cold={"n": 32, "extra_ms": 450},
                         v_slow={"every": 9, "mbps": 6.0}, a_slow={"every": 9, "mbps": 6.0}),
    "S8_vstall": dict(note="video: 1 range đợt đầu treo 3 s trên cả 2 host (kiểm tail-rescue cho video dual)",
                      link=900, v_conn=18.6, a_conn=20.0, redirect_ms=60, final_ms=200, alt_ms=260,
                      v_stall={"seg": 41, "workers": 64, "delay": 3.0}),
})


# Bộ quét theo yêu cầu (dùng với mock480 / live_dl):  --b-variants preset:v99_conns | v99_audio | v99_hedge | v99_range | v99_rescue
b.PRESETS.update({
    "v99_conns": {"c32": {"V98_DUAL_MAX_CONNS": 32}, "c48": {"V98_DUAL_MAX_CONNS": 48}, "c64": {},
                  "c80": {"V94_RANGE_WORKERS_VIDEO": 40, "V98_DUAL_MAX_CONNS": 80}},
    "v99_audio": {"a8": {"V99_AUDIO_WORKERS": 8}, "a16": {"V99_AUDIO_WORKERS": 16}, "a24": {"V99_AUDIO_WORKERS": 24},
                  "a32": {"V99_AUDIO_WORKERS": 32}},
    "v99_hedge": {"h0": {"V99_HEDGE_MAX": 0}, "h8": {"V99_HEDGE_MAX": 8}, "h16": {"V99_HEDGE_MAX": 16}, "h40": {"V99_HEDGE_MAX": 40}},
    "v99_range": {"auto": {}, "r1": {"V98_PART_MIB": 1.0}, "r2": {"V98_PART_MIB": 2.0}, "r4": {"V98_PART_MIB": 4.0}},
    "v99_rescue": {"rescue_on": {}, "rescue_off": {"V99_TAIL_RESCUE": False}},
    "v99_tail": {"tail_new": {}, "tail_old": {"V99_WARM_TTFB": False, "V99_TAIL_RESCUE": False},
                 "warm_only": {"V99_TAIL_RESCUE": False}, "rescue_only": {"V99_WARM_TTFB": False}},
})


def pct(vals, p):
    return b.pct(vals, p)


# ----------------------------------------------------------------------------- env
def _run(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or r.stderr or "").strip().splitlines()[0][:160] if (r.stdout or r.stderr) else ""
    except Exception as e:  # noqa: BLE001
        return f"(lỗi: {type(e).__name__})"


def run_env(args):
    b._ensure_qt()
    out = {"python": sys.version.split()[0], "python_exe": sys.executable, "cwd": str(ROOT)}
    try:
        import yt_dlp
        from yt_dlp.version import __version__ as ver
        out["yt_dlp_module_version"] = ver
        out["yt_dlp_module_path"] = str(Path(yt_dlp.__file__).parent)
    except Exception as e:  # noqa: BLE001
        out["yt_dlp_module_version"] = f"KHÔNG import được ({type(e).__name__})"
    exes = []
    for cand in [ROOT / "yt-dlp.exe", shutil.which("yt-dlp.exe"), shutil.which("yt-dlp")]:
        if cand and Path(cand).exists() and str(cand) not in [x[0] for x in exes]:
            exes.append((str(cand), _run([str(cand), "--version"])))
    out["yt_dlp_exe"] = [{"path": p, "version": v} for p, v in exes]
    try:
        import urllib3
        out["urllib3"] = urllib3.__version__
    except Exception:  # noqa: BLE001
        pass
    for k in ("A", "B"):
        try:
            m = b.load_module(f"v98bench_{k}", b.ENGINE_FILES[k])
            out[f"engine_{k}"] = {"file": b.ENGINE_FILES[k].name, "version": m.V94_VERSION,
                                  "ffmpeg": _run([m.FFMPEG_PATH, "-version"]) if m.FFMPEG_PATH else None,
                                  "aria2c": _run([m.ARIA2C_PATH, "--version"]) if m.ARIA2C_PATH else None,
                                  "ytdlp_exe_tool_would_use": getattr(m, "YTDLP_PATH", None)}
        except Exception as e:  # noqa: BLE001
            out[f"engine_{k}"] = f"lỗi nạp: {type(e).__name__}: {e}"
    print(json.dumps(out, ensure_ascii=False, indent=1))
    print("\nGiải thích: tool KHÔNG gọi yt-dlp.exe để lấy link — nó `import yt_dlp` (module của Python đang chạy tool). Dòng log"
          "\n'⚡ V6 WARM: yt-dlp=...' in phiên bản MODULE. Lệnh `yt-dlp --version` trong CMD chạy file .exe ở PATH → có thể khác bản."
          "\nCập nhật bản mà TOOL dùng:  python -m pip install -U yt-dlp   (đúng python ở 'python_exe' phía trên).")
    b.REPORT.mkdir(exist_ok=True)
    (b.REPORT / f"v99_env_{time.strftime('%Y%m%d_%H%M%S')}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


# ----------------------------------------------------------------------------- autopsy (telemetry đã có)
def _load_spans(files):
    jobs = {}
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    j = r.get("job")
                    if j is None:
                        continue
                    jobs.setdefault((Path(f).name, str(j)), []).append(r)
        except OSError:
            continue
    return jobs


def _downloads(rows, kind):
    """Tách TỪNG lượt tải của 1 job (1 video có thể tải 2 lần: SAFE-REFRESH; bench dùng job '-') theo thứ tự ghi:
    các gvs_req cho tới span <kind>_download_complete."""
    cur = []
    for r in rows:
        ev = r.get("ev")
        if ev == "gvs_req" and r.get("kind") == kind:
            cur.append(r)
        elif ev == f"{kind}_download_complete":
            if cur:
                yield cur, r
            cur = []
    if cur:
        yield cur, None


def _autopsy_kind(reqs, done, kind):
    if not reqs:
        return None
    ok = [r for r in reqs if r.get("end") in ("done", "victim", "taken") and (r.get("bytes") or 0) > 0]
    first = [r for r in reqs if r.get("req") == "first"]
    ttfb = lambda r: (r["t_hdr_ms"] - r["t_issue_ms"]) if r.get("t_hdr_ms") is not None else None  # noqa: E731
    body = lambda r: (r["t_end_ms"] - r["t_hdr_ms"]) if r.get("t_hdr_ms") is not None else None  # noqa: E731
    tt_first = sorted(x for x in (ttfb(r) for r in first) if x is not None)
    t_end_all = max(r["t_end_ms"] for r in reqs)
    ends = sorted(r["t_end_ms"] for r in ok) or [t_end_all]
    t_p90_done = pct(ends, 90)
    last = max(ok or reqs, key=lambda r: r["t_end_ms"])
    total_bytes = sum(int(r.get("bytes") or 0) for r in reqs)
    size = int((done or {}).get("total") or (done or {}).get("bytes") or 0)
    by_end = {}
    for r in reqs:
        by_end[r.get("end")] = by_end.get(r.get("end"), 0) + 1
    # range chưa có header khi 90% range khác đã xong = "kẹt"
    stuck = [r for r in reqs if r.get("t_hdr_ms") is None or r["t_hdr_ms"] > t_p90_done]
    rescue = [r for r in reqs if r.get("req") in ("takeover", "split", "retry")]
    out = {
        "kind": kind, "requests": len(reqs), "first_wave": len(first), "outcomes": by_end,
        "elapsed_ms": (done or {}).get("elapsed_ms") or round(t_end_all, 1), "size_mib": round(size / MB, 2) if size else None,
        "ttfb_first_p50_ms": pct(tt_first, 50), "ttfb_first_p90_ms": pct(tt_first, 90), "ttfb_first_max_ms": (tt_first[-1] if tt_first else None),
        "t_90pct_ranges_done_ms": t_p90_done, "t_all_done_ms": round(t_end_all, 1),
        "tail_ms": round(t_end_all - (t_p90_done or t_end_all), 1),
        "last_range": {"req": last.get("req"), "mode": last.get("mode") or "-", "host": last.get("host"), "bytes": last.get("bytes"),
                       "t_issue_ms": last.get("t_issue_ms"), "t_30x_ms": last.get("t_30x_ms"), "t_hdr_ms": last.get("t_hdr_ms"),
                       "t_end_ms": last.get("t_end_ms"), "ttfb_ms": (round(ttfb(last), 1) if ttfb(last) is not None else None),
                       "body_ms": (round(body(last), 1) if body(last) is not None else None), "conn_reused": last.get("conn_reused")},
        "late_header_ranges": len(stuck), "rescue_requests": len(rescue),
        "first_rescue_issue_ms": (min(r["t_issue_ms"] for r in rescue) if rescue else None),
        "dup_bytes": (total_bytes - size) if size else None,
        "info": {k: (done or {}).get(k) for k in ("workers_initial", "ttfb_ms", "steals", "splits", "takeovers", "hedges", "races",
                                                    "alt_wins", "retries", "lock_wait_ms", "lock_hold_ms", "write_ms", "race_mode",
                                                    "conc_peak", "mbps", "xfer_mbps") if (done or {}).get(k) is not None},
    }
    lr = out["last_range"]
    # kết luận CHỈ từ số liệu
    why = []
    if out["tail_ms"] is not None and out["tail_ms"] >= 300:
        if lr["ttfb_ms"] is not None and lr["body_ms"] is not None:
            if lr["ttfb_ms"] >= 2 * max(1.0, out["ttfb_first_p50_ms"] or 1.0) and lr["ttfb_ms"] > lr["body_ms"]:
                why.append(f"range xong cuối CHỜ HEADER {lr['ttfb_ms']:.0f} ms (p50 đợt đầu {out['ttfb_first_p50_ms']:.0f} ms) — kẹt ở "
                           f"kết nối/TTFB của host={lr['host']}, không phải ở tốc độ tải")
            elif lr["body_ms"] > lr["ttfb_ms"]:
                why.append(f"range xong cuối tải THÂN {lr['body_ms']:.0f} ms cho {int(lr['bytes'] or 0) / MB:.2f} MiB — kết nối đó chậm/đứng")
        if lr["req"] in ("takeover", "retry", "split"):
            why.append(f"range cuối là request CỨU ({lr['req']}) phát lúc {lr['t_issue_ms']:.0f} ms — engine chỉ cứu sau "
                       f"{lr['t_issue_ms']:.0f} ms kể từ đầu job")
        elif not rescue:
            why.append("KHÔNG có request cứu nào (takeover/split/retry) cho tới khi xong")
    out["verdict"] = why or ["không có đuôi chậm (tail < 300 ms)"]
    return out


def run_autopsy(args):
    files = [Path(x) for x in (args.files or [])] or sorted((ROOT / "perf_report").glob("telemetry_*.jsonl"))[-args.last:]
    if not files:
        print("❌ Không thấy perf_report/telemetry_*.jsonl. Tool ghi file này mỗi phiên chạy (V94_TELEMETRY mặc định bật).")
        return 2
    jobs = _load_spans(files)
    res = []
    for (fname, job), rows in jobs.items():
        for kind in ("video", "audio"):
            for i, (reqs, done) in enumerate(_downloads(rows, kind)):
                a = _autopsy_kind(reqs, done, kind)
                if a:
                    a.update(file=fname, job=(job if job != "-" else f"-#{i + 1}"), ok=bool((done or {}).get("ok", done is not None)))
                    res.append(a)
    if args.job:
        res = [r for r in res if args.job in r["job"]]
    res.sort(key=lambda r: -(r.get("tail_ms") or 0))
    show = res if args.all else res[:args.top]
    md = [f"# v99 autopsy — {time.strftime('%Y-%m-%d %H:%M:%S')}", "",
          f"- {len(files)} file telemetry, {len(res)} lượt tải (video/audio). Sắp theo ĐUÔI (thời gian từ lúc 90% range xong → xong hẳn).",
          "- Mọi con số lấy từ span `gvs_req` do chính tool ghi lúc tải thật. Không có URL / chữ ký trong telemetry.", "",
          "| job | loại | MiB | xong (ms) | 90% range xong (ms) | ĐUÔI (ms) | GET | TTFB đợt đầu p50/p90/max | range cuối: loại/host | chờ header | tải thân | request cứu (lúc ms) | byte dư |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    f0 = lambda v: "-" if v is None else (f"{v:.0f}" if isinstance(v, (int, float)) else str(v))  # noqa: E731
    for a in show:
        lr = a["last_range"]
        md.append(f"| {a['job']} | {a['kind']} | {f0(a['size_mib']) if a['size_mib'] is None else a['size_mib']} | {f0(a['t_all_done_ms'])} | "
                  f"{f0(a['t_90pct_ranges_done_ms'])} | **{f0(a['tail_ms'])}** | {a['requests']} | {f0(a['ttfb_first_p50_ms'])}/"
                  f"{f0(a['ttfb_first_p90_ms'])}/{f0(a['ttfb_first_max_ms'])} | {lr['req']}{'/' + lr['mode'] if lr['mode'] != '-' else ''}/{lr['host']} | "
                  f"{f0(lr['ttfb_ms'])} | {f0(lr['body_ms'])} | {a['rescue_requests']} ({f0(a['first_rescue_issue_ms'])}) | "
                  f"{'-' if a['dup_bytes'] is None else round(a['dup_bytes'] / MB, 2)} |")
    md.append("")
    for a in show:
        md.append(f"- **{a['job']} {a['kind']}**: " + "; ".join(a["verdict"]) + f" | kết thúc request: {a['outcomes']} | {a['info']}")
    b.REPORT.mkdir(exist_ok=True)
    base = b.REPORT / f"v99_autopsy_{time.strftime('%Y%m%d_%H%M%S')}"
    Path(f"{base}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    Path(f"{base}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print("\n".join(md))
    print(f"\n→ {base}.md / .json")
    return 0


# ----------------------------------------------------------------------------- formats (LIVE, không tải media)
V480 = {"135": "avc1 (H.264)", "244": "vp9", "397": "av01 (AV1)"}


def _codec_of(mime):
    m = re.search(r'codecs="([^"]+)"', str(mime or ""))
    c = (m.group(1) if m else "").split(",")[0].strip()
    return c or str(mime or "").split(";")[0]


def run_formats(args):
    urls = b._read_urls(args)
    if not urls:
        print("❌ Chưa có URL (--url hoặc --urls urls.txt).")
        return 2
    b._ensure_qt()
    mod = b.load_module("v98bench_B", b.ENGINE_FILES["B"])
    rows = []
    for url in urls:
        vid = mod._extract_youtube_video_id(url)
        # 1) native: đúng 2 client mà tool dùng
        for ck in ("visionos", "web_embedded"):
            t = time.perf_counter()
            try:
                inv = mod._v99_native_inventory(vid, ck)
            except Exception as e:  # noqa: BLE001
                inv = {"error": type(e).__name__}
            ms = round((time.perf_counter() - t) * 1000, 1)
            for f in (inv.get("video") or []) + (inv.get("audio") or []):
                rows.append(dict(f, video_id=vid, source=f"NATIVE_{ck.upper()}", resolve_ms=ms))
            if inv.get("error") or not (inv.get("video") or inv.get("audio")):
                rows.append({"video_id": vid, "source": f"NATIVE_{ck.upper()}", "resolve_ms": ms, "note": inv.get("error") or inv.get("status") or "không có format"})
            time.sleep(args.gap)
        # 2) yt-dlp (module mà tool import)
        t = time.perf_counter()
        try:
            import yt_dlp
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
                info = ydl.extract_info(url, download=False)
            ms = round((time.perf_counter() - t) * 1000, 1)
            for f in info.get("formats") or []:
                h, vc, ac = f.get("height"), str(f.get("vcodec") or "none"), str(f.get("acodec") or "none")
                if vc != "none" and h == 480 and ac == "none":
                    rows.append({"video_id": vid, "source": "YTDLP", "resolve_ms": ms, "type": "video", "itag": str(f.get("format_id")),
                                 "codec": vc, "mib": round((f.get("filesize") or f.get("filesize_approx") or 0) / MB, 2),
                                 "fps": f.get("fps"), "direct": bool(f.get("url")), "w": f.get("width"), "h": h})
                elif vc == "none" and ac != "none":
                    rows.append({"video_id": vid, "source": "YTDLP", "resolve_ms": ms, "type": "audio", "itag": str(f.get("format_id")),
                                 "codec": ac, "mib": round((f.get("filesize") or f.get("filesize_approx") or 0) / MB, 2),
                                 "lang": str(f.get("language") or ""), "direct": bool(f.get("url")),
                                 "note": str(f.get("format_note") or "")})
        except Exception as e:  # noqa: BLE001
            rows.append({"video_id": vid, "source": "YTDLP", "note": f"lỗi {type(e).__name__}"})
        time.sleep(args.gap)
    cols = ["video_id", "source", "resolve_ms", "type", "itag", "codec", "mib", "fps", "lang", "direct", "note"]
    md = [f"# v99 formats — {time.strftime('%Y-%m-%d %H:%M:%S')}", "",
          "- LIVE: format 480p (video) + audio mà từng nguồn THẬT SỰ trả về cho máy này, lúc này. `direct` = có link tải thẳng.",
          "- `mib` = MiB (1 MiB = 1.048.576 byte). Không tải media, không ghi URL.", "",
          "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        md.append("| " + " | ".join("-" if r.get(c) in (None, "") else str(r.get(c)) for c in cols) + " |")
    b.REPORT.mkdir(exist_ok=True)
    base = b.REPORT / f"v99_formats_{time.strftime('%Y%m%d_%H%M%S')}"
    Path(f"{base}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    Path(f"{base}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n".join(md))
    print(f"\n→ {base}.md / .json")
    return 0


# ----------------------------------------------------------------------------- verify_cut
def _ffjson(exe, args, timeout=120):
    r = subprocess.run([exe, "-v", "error"] + args, capture_output=True, text=True, timeout=timeout)
    try:
        return json.loads(r.stdout or "{}"), (r.stderr or "").strip()
    except Exception:  # noqa: BLE001
        return {}, (r.stderr or "").strip()


def verify_parts(folder, ffprobe=None, ffmpeg=None, source_dur=None, decode=True):
    """Kiểm từng file trong thư mục part. Trả (rows, summary). Không sửa file nào."""
    ffprobe = ffprobe or shutil.which("ffprobe")
    ffmpeg = ffmpeg or shutil.which("ffmpeg")
    files = sorted([p for p in Path(folder).rglob("*") if p.is_file() and p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")],
                   key=lambda p: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", p.name)])
    rows = []
    for f in files:
        j, err = _ffjson(ffprobe, ["-show_entries",
                                   "stream=index,codec_type,codec_name,profile,width,height,r_frame_rate,start_time,duration,nb_frames,"
                                   "sample_rate,channels:stream_tags=language:format=duration,size,format_name", "-of", "json", str(f)])
        st = j.get("streams") or []
        v = next((s for s in st if s.get("codec_type") == "video"), None)
        a = next((s for s in st if s.get("codec_type") == "audio"), None)
        fl = lambda x: float(x) if x not in (None, "N/A", "") else None  # noqa: E731
        row = {"file": f.name[-60:], "size_mib": round(f.stat().st_size / MB, 2), "container": (j.get("format") or {}).get("format_name"),
               "dur": fl((j.get("format") or {}).get("duration")), "has_video": bool(v), "has_audio": bool(a),
               "vcodec": (v or {}).get("codec_name"), "w": (v or {}).get("width"), "h": (v or {}).get("height"),
               "fps": (v or {}).get("r_frame_rate"), "v_start": fl((v or {}).get("start_time")), "v_dur": fl((v or {}).get("duration")),
               "acodec": (a or {}).get("codec_name"), "a_profile": (a or {}).get("profile"), "a_lang": ((a or {}).get("tags") or {}).get("language"),
               "a_start": fl((a or {}).get("start_time")), "a_dur": fl((a or {}).get("duration")), "probe_err": err[:120]}
        if row["v_start"] is not None and row["a_start"] is not None:
            row["av_start_diff_ms"] = round((row["a_start"] - row["v_start"]) * 1000, 1)
        if row["v_dur"] is not None and row["a_dur"] is not None:
            row["av_dur_diff_ms"] = round((row["a_dur"] - row["v_dur"]) * 1000, 1)
        if ffprobe and v:
            # gói ĐẦU TIÊN trong file (bỏ qua edit list) phải là keyframe — không thì phần đầu không giải mã được
            k, _ = _ffjson(ffprobe, ["-ignore_editlist", "1", "-select_streams", "v:0", "-read_intervals", "%+#1",
                                     "-show_entries", "packet=flags", "-of", "json", str(f)])
            fl0 = str(((k.get("packets") or [{}])[0]).get("flags") or "")
            row["first_packet_key"] = ("K" in fl0) if fl0 else None
        if decode and ffmpeg and v:
            t = time.perf_counter()
            r = subprocess.run([ffmpeg, "-v", "error", "-i", str(f), "-map", "0:v:0", "-map", "0:a:0?", "-f", "null", "-"],
                               capture_output=True, text=True, timeout=1800)
            lines = [x for x in (r.stderr or "").splitlines() if x.strip()]
            ts_warn = [x for x in lines if "non monotonically increasing dts" in x]
            other = [x for x in lines if x not in ts_warn]
            row["decode_ok"] = (r.returncode == 0 and not other)
            row["ts_warn"] = len(ts_warn)           # timestamp lùi ở đầu part (khung "pre-roll" trước điểm cắt lộ ra)
            row["decode_err"] = (other[0][:160] if other else "")
            row["decode_ms"] = round((time.perf_counter() - t) * 1000)
            c, _ = _ffjson(ffprobe, ["-select_streams", "v:0", "-count_frames", "-show_entries", "stream=nb_read_frames", "-of", "json", str(f)],
                           timeout=1800)
            try:
                row["v_frames_decoded"] = int(((c.get("streams") or [{}])[0]).get("nb_read_frames") or 0)
            except Exception:  # noqa: BLE001
                row["v_frames_decoded"] = None
            c, _ = _ffjson(ffprobe, ["-ignore_editlist", "1", "-select_streams", "v:0", "-count_packets", "-show_entries",
                                     "stream=nb_read_packets", "-of", "json", str(f)])
            try:
                row["v_packets"] = int(((c.get("streams") or [{}])[0]).get("nb_read_packets") or 0)
            except Exception:  # noqa: BLE001
                row["v_packets"] = None
            try:
                num, den = str(row["fps"] or "0/1").split("/")
                fps = float(num) / float(den or 1)
            except Exception:  # noqa: BLE001
                fps = 0.0
            if fps > 0 and row["v_dur"] and row["v_frames_decoded"]:
                row["extra_frames"] = int(row["v_frames_decoded"] - round(row["v_dur"] * fps))   # > 0: lộ khung pre-roll
        row["ok"] = bool(row["has_video"] and row["has_audio"] and (row.get("decode_ok", True)) and (row["dur"] or 0) > 0.5
                         and abs(row.get("av_start_diff_ms") or 0) <= 150 and (row.get("first_packet_key") is not False))
        rows.append(row)
    durs = [r["dur"] for r in rows if r["dur"]]
    summ = {"parts": len(rows), "ok_parts": sum(1 for r in rows if r["ok"]), "total_dur_s": round(sum(durs), 2) if durs else 0,
            "vcodecs": sorted(set(str(r["vcodec"]) for r in rows)), "acodecs": sorted(set(str(r["acodec"]) for r in rows)),
            "containers": sorted(set(str(r["container"]) for r in rows)),
            "no_audio": [r["file"] for r in rows if not r["has_audio"]], "no_video": [r["file"] for r in rows if not r["has_video"]],
            "decode_fail": [r["file"] for r in rows if r.get("decode_ok") is False],
            "not_keyframe_start": [r["file"] for r in rows if r.get("first_packet_key") is False],
            "parts_with_ts_warn": sum(1 for r in rows if (r.get("ts_warn") or 0) > 0),
            "max_extra_frames": max([r.get("extra_frames") or 0 for r in rows] or [0]),
            "frames_decoded": sum(r.get("v_frames_decoded") or 0 for r in rows),
            "max_av_start_diff_ms": max([abs(r.get("av_start_diff_ms") or 0) for r in rows] or [0]),
            "max_av_dur_diff_ms": max([abs(r.get("av_dur_diff_ms") or 0) for r in rows] or [0]),
            "total_size_mib": round(sum(r["size_mib"] for r in rows), 2), "source_dur_s": source_dur}
    if source_dur:
        summ["dur_vs_source_s"] = round(summ["total_dur_s"] - float(source_dur), 2)
    return rows, summ


def run_verify_cut(args):
    rows, summ = verify_parts(args.folder, source_dur=args.source_dur, decode=not args.no_decode)
    if not rows:
        print("❌ Không thấy file video nào trong thư mục.")
        return 2
    cols = ["file", "ok", "dur", "vcodec", "acodec", "a_lang", "container", "size_mib", "av_start_diff_ms", "av_dur_diff_ms", "decode_ok",
            "first_packet_key", "ts_warn", "extra_frames", "v_frames_decoded"]
    md = [f"# v99 verify_cut — {time.strftime('%Y-%m-%d %H:%M:%S')}", "", f"- Thư mục: {Path(args.folder).name}",
          f"- Tổng kết: {json.dumps(summ, ensure_ascii=False)}", "",
          "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        md.append("| " + " | ".join("-" if r.get(c) is None else str(r.get(c)) for c in cols) + " |")
    b.REPORT.mkdir(exist_ok=True)
    base = b.REPORT / f"v99_verify_cut_{time.strftime('%Y%m%d_%H%M%S')}"
    Path(f"{base}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    Path(f"{base}.json").write_text(json.dumps({"summary": summ, "parts": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n".join(md))
    print(f"\n→ {base}.md / .json")
    return 0 if summ["ok_parts"] == summ["parts"] else 1


# ----------------------------------------------------------------------------- live480 theo tổ hợp format
def run_live_combos(args):
    """NGUYÊN pipeline cho từng tổ hợp video+audio. Tổ hợp ép bằng cờ V99_FORCE_* của bản B (ứng viên). Mỗi lượt: ffprobe +
    kiểm từng part (verify_parts, không giải mã toàn bộ trừ khi --decode) TRƯỚC khi thư mục kết quả bị xoá."""
    import random
    import tempfile
    import bench_v93
    urls = b._read_urls(args)
    if not urls:
        print("❌ Chưa có URL (--url hoặc --urls urls.txt). Không có URL thật thì KHÔNG có số live — bench không tự bịa số.")
        return 2
    qt_stub = b._ensure_qt()
    if not qt_stub:
        from PySide6.QtWidgets import QApplication
        _app = QApplication.instance() or QApplication([])  # noqa: F841
    args.engines = "A,B"
    mods = b._live_mods(args)
    cfgs = [{"label": "A | V9.8.1 hiện tại (244→135 + JA 139)", "mod": "A", "flags": {}, "combo": "prod"}]
    for combo in [c.strip() for c in args.combos.split(",") if c.strip()]:
        v, a = (combo.split("+") + [""])[:2]
        cfgs.append({"label": f"B | {combo}", "mod": "B", "combo": combo,
                     "flags": {"V99_FORCE_VIDEO_ITAG": v.strip(), "V99_FORCE_AUDIO_ITAG": a.strip(), "V99_FORMAT_MODE": "fixed"}})
    if args.smart:
        cfgs.append({"label": "B | SMART (tự chọn)", "mod": "B", "combo": "smart", "flags": {"V99_FORMAT_MODE": "smart",
                                                                                              "V99_ALLOW_VIDEO_CODECS": args.allow_codecs}})
    rnd = random.Random(args.seed)
    rows, stop = [], None
    work = Path(tempfile.mkdtemp(prefix="v99p_"))
    if not args.no_warmup:
        for k, m in mods.items():
            print(f"[warm-up {k}] (không tính)")
            bench_v93.run_live_once(m, "v94", urls[0], work / f"warm_{k}", args.dl_engine, args.cutsec, "480hard")
            shutil.rmtree(work / f"warm_{k}", ignore_errors=True)
            time.sleep(args.gap)
    for rep in range(args.reps):
        for url in urls:
            if stop:
                break
            order = list(cfgs)
            rnd.shuffle(order)
            for pos, c in enumerate(order):
                mod = mods[c["mod"]]
                if not args.warm:
                    b._cold_pools(mods)
                saved = {k: getattr(mod, k, None) for k in c["flags"]}
                for k, v in c["flags"].items():
                    setattr(mod, k, v)
                wdir = work / f"r{rep}_{pos}"
                try:
                    r = bench_v93.run_live_once(mod, "v94", url, wdir, args.dl_engine, args.cutsec, "480hard")
                finally:
                    for k, v in saved.items():
                        setattr(mod, k, v)
                t_v = time.perf_counter()
                prows, psum = verify_parts(wdir / "final", ffprobe=getattr(mod, "FFPROBE_PATH", None), ffmpeg=getattr(mod, "FFMPEG_PATH", None),
                                           source_dur=None, decode=args.decode)
                if not prows:
                    prows, psum = verify_parts(wdir / "out", ffprobe=getattr(mod, "FFPROBE_PATH", None), ffmpeg=getattr(mod, "FFMPEG_PATH", None),
                                               decode=args.decode)
                hot, tot = r.get("hot_ready_ms"), r.get("pipeline_total_ms")
                row = {"label": c["label"], "combo": c["combo"], "rep": rep, "pos_in_round": pos, "video_id": r.get("video_id"),
                       "ok": bool(r.get("success") and psum.get("parts") and psum["ok_parts"] == psum["parts"]),
                       "itag_video": r.get("video_itag"), "itag_audio": r.get("audio_itag"),
                       "RESOLVE_MS": r.get("resolve_video_ms"), "VIDEO_MS": r.get("video_ms"), "AUDIO_MS": r.get("audio_ms"),
                       "HOT_READY_MS": hot, "CUT_MUX_MS": (round(tot - hot, 1) if (hot and tot) else None), "PIPELINE_TOTAL_MS": tot,
                       "VIDEO_MIB": round(float(r.get("video_total") or 0) / MB, 2) or None,
                       "AUDIO_MIB": round(float(r.get("audio_total") or 0) / MB, 2) or None,
                       "GET_VIDEO": r.get("video_requests"), "GET_AUDIO": r.get("audio_requests"),
                       "RETRIES": (r.get("video_retries") or 0) + (r.get("audio_retries") or 0),
                       "HTTP403": r.get("http403"), "HTTP429": r.get("http429"), "safe_refresh": r.get("safe_refresh"),
                       "TTFB_VIDEO_MS": r.get("video_ttfb_ms"), "TTFB_AUDIO_MS": r.get("audio_ttfb_ms"),
                       "MBPS_VIDEO": r.get("video_mbps"), "parts": psum.get("parts"), "parts_ok": psum.get("ok_parts"),
                       "out_vcodec": ",".join(psum.get("vcodecs") or []), "out_acodec": ",".join(psum.get("acodecs") or []),
                       "out_container": ",".join(psum.get("containers") or []), "out_total_dur_s": psum.get("total_dur_s"),
                       "max_av_start_diff_ms": psum.get("max_av_start_diff_ms"), "no_audio_parts": len(psum.get("no_audio") or []),
                       "verify_ms": round((time.perf_counter() - t_v) * 1000), "fail": str(r.get("fail_reason") or "")[:100]}
                rows.append(row)
                print(f"  rep{rep} {c['label']:<44} v={row['itag_video']} a={row['itag_audio']} hot={hot} cut={row['CUT_MUX_MS']} "
                      f"total={tot} parts={row['parts_ok']}/{row['parts']} out={row['out_vcodec']}+{row['out_acodec']} "
                      f"{'OK' if row['ok'] else 'FAIL ' + row['fail']}", flush=True)
                shutil.rmtree(wdir, ignore_errors=True)
                if int(r.get("http429") or 0) or "429" in row["fail"]:
                    stop = "HTTP 429 — dừng bench"
                    break
                time.sleep(args.gap)
    shutil.rmtree(work, ignore_errors=True)
    groups = {}
    for r in rows:
        groups.setdefault(r["label"], []).append(r)
    cols = ["TỔ HỢP", "n", "OK", "video/audio itag", "VIDEO_MIB", "AUDIO_MIB", "RESOLVE p50", "VIDEO p50/p95", "AUDIO p50/p95",
            "HOT READY p50/p95", "CUT/MUX p50", "TOTAL p50/p95", "GET v+a", "retry", "403", "file ra (codec)", "part OK"]
    md = [f"# v99 live480 theo tổ hợp format — {time.strftime('%Y-%m-%d %H:%M:%S')}", "",
          f"- SỐ LIVE: {len(urls)} URL × {args.reps} vòng, thứ tự ngẫu nhiên, nghỉ {args.gap}s. Thời gian = KPI của chính tool (ms).",
          "- OK = pipeline thành công VÀ mọi part có cả video + audio, gói đầu là keyframe, lệch A/V ≤ 150 ms"
          + (" VÀ giải mã toàn bộ không lỗi." if args.decode else " (chưa giải mã toàn bộ: thêm --decode)."),
          "- p95 với ít mẫu (n < 20) chỉ là giá trị gần lớn nhất; đọc cùng n.", "- MiB = 1.048.576 byte; Mbps = megabit/giây.", "",
          "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    summ = []
    for lab, rs in sorted(groups.items(), key=lambda kv: (pct([r["PIPELINE_TOTAL_MS"] for r in kv[1] if r["ok"]], 50) or 9e9)):
        ok = [r for r in rs if r["ok"]]
        q = lambda k, p: pct([r.get(k) for r in ok if isinstance(r.get(k), (int, float))], p)  # noqa: E731
        f0 = lambda v: "-" if v is None else f"{v:.0f}"  # noqa: E731
        s = {"label": lab, "n": len(rs), "ok": len(ok), "itags": sorted(set(f"{r['itag_video']}+{r['itag_audio']}" for r in rs)),
             "resolve_p50": q("RESOLVE_MS", 50), "video_p50": q("VIDEO_MS", 50), "video_p95": q("VIDEO_MS", 95),
             "audio_p50": q("AUDIO_MS", 50), "audio_p95": q("AUDIO_MS", 95), "hot_p50": q("HOT_READY_MS", 50), "hot_p95": q("HOT_READY_MS", 95),
             "cut_p50": q("CUT_MUX_MS", 50), "total_p50": q("PIPELINE_TOTAL_MS", 50), "total_p95": q("PIPELINE_TOTAL_MS", 95)}
        summ.append(s)
        md.append("| " + " | ".join([lab, str(len(rs)), f"{len(ok)}/{len(rs)}", ",".join(s["itags"]), str(q("VIDEO_MIB", 50)), str(q("AUDIO_MIB", 50)),
                                      f0(s["resolve_p50"]), f"{f0(s['video_p50'])}/{f0(s['video_p95'])}", f"{f0(s['audio_p50'])}/{f0(s['audio_p95'])}",
                                      f"{f0(s['hot_p50'])}/{f0(s['hot_p95'])}", f0(s["cut_p50"]), f"{f0(s['total_p50'])}/{f0(s['total_p95'])}",
                                      f"{f0(q('GET_VIDEO', 50))}+{f0(q('GET_AUDIO', 50))}", str(sum(r["RETRIES"] or 0 for r in rs)),
                                      str(sum(int(r["HTTP403"] or 0) for r in rs)),
                                      ";".join(sorted(set(f"{r['out_vcodec']}+{r['out_acodec']}/{r['out_container']}" for r in rs if r["parts"])))[:60],
                                      f"{sum(r['parts_ok'] or 0 for r in rs)}/{sum(r['parts'] or 0 for r in rs)}"]) + " |")
    if stop:
        md.append(f"\n⛔ {stop}")
    b.REPORT.mkdir(exist_ok=True)
    base = b.REPORT / f"v99_live480_combos_{time.strftime('%Y%m%d_%H%M%S')}"
    Path(f"{base}_summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    Path(f"{base}.json").write_text(json.dumps({"summary": summ, "rows": rows}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    import csv
    keys = list(dict.fromkeys(k for r in rows for k in r))
    with open(f"{base}.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    print("\n".join(md))
    print(f"\n→ {base}_summary.md / .csv / .json")
    return 0 if not stop else 3


# ----------------------------------------------------------------------------- cutlab (cục bộ, không cần mạng)
def _mk_source(ffmpeg, d, vcodec, acodec, dur, fps=24):
    """Nguồn 854x480 tổng hợp (testsrc2 + số khung hình vẽ lên hình + tiếng beep mỗi giây) để kiểm cắt chính xác."""
    venc = {"vp9": (["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-b:v", "400k"], "webm"),
            "av1": (["-c:v", "libsvtav1", "-preset", "12", "-crf", "45"], "mp4"),
            "h264": (["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-b:v", "500k"], "mp4")}[vcodec]
    aenc = {"aac": (["-c:a", "aac", "-b:a", "48k"], "m4a"), "opus": (["-c:a", "libopus", "-b:a", "48k"], "webm")}[acodec]
    v = Path(d) / f"src_{vcodec}_{dur}.{venc[1]}"
    a = Path(d) / f"src_{acodec}_{dur}.{aenc[1]}"
    if not v.exists():
        r = subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size=854x480:rate={fps}", "-t", str(dur)]
                           + venc[0] + ["-g", str(fps * 5), "-an", str(v)], capture_output=True, text=True)
        if r.returncode != 0 or not v.exists():
            return None, None, (r.stderr or "")[-200:]
    if not a.exists():
        r = subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i",
                            f"sine=frequency=880:beep_factor=4:duration={dur}", "-ac", "2"] + aenc[0] + [str(a)],
                           capture_output=True, text=True)
        if r.returncode != 0 or not a.exists():
            return None, None, (r.stderr or "")[-200:]
    return v, a, ""


def run_cutlab(args):
    """Chạy NGUYÊN pipeline của tool (engine B) trên nguồn tổng hợp qua máy chủ giả cục bộ, rồi kiểm từng part:
    có video + audio, bắt đầu bằng keyframe, lệch A/V, GIẢI MÃ TOÀN BỘ không lỗi, tổng số gói video so với nguồn."""
    import tempfile
    import threading
    b._ensure_qt()
    sys.path.insert(0, str(ROOT / "tests"))
    from mock_gvs import MockGVS
    from openpyxl import Workbook, load_workbook
    try:
        from PySide6.QtWidgets import QApplication
        _app = QApplication.instance() or QApplication([])  # noqa: F841
    except Exception:  # noqa: BLE001
        pass
    mod = b.load_module("v98bench_" + args.engine, b.ENGINE_FILES[args.engine])
    ffmpeg, ffprobe = mod.FFMPEG_PATH or shutil.which("ffmpeg"), mod.FFPROBE_PATH or shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        print("❌ Không thấy ffmpeg/ffprobe.")
        return 2
    work = Path(tempfile.mkdtemp(prefix="v99cut_"))
    rows = []
    itag_of = {"vp9": "244", "av1": "397", "h264": "135"}
    a_itag = {"aac": "139", "opus": "249"}

    class _T:
        def __init__(self, v): self.v = v
        def text(self): return self.v

    class _C:
        def isChecked(self): return False

    class _Combo:
        def currentData(self): return "aria2c"
    for combo in [c.strip() for c in args.combos.split(",") if c.strip()]:
        vc, ac = combo.split("+")
        for dur in [int(x) for x in args.durations.split(",") if x]:
            src_v, src_a, err = _mk_source(ffmpeg, work, vc, ac, dur)
            row = {"combo": combo, "dur_src": dur, "engine": mod.V94_VERSION}
            if src_v is None:
                row.update(ok=False, note=f"không tạo được nguồn ({err.strip()[:80]})")
                rows.append(row)
                print(f"  {combo} {dur}s: bỏ qua — {row['note']}")
                continue
            sj, _ = _ffjson(ffprobe, ["-select_streams", "v:0", "-count_packets", "-show_entries", "stream=nb_read_packets,codec_name",
                                      "-of", "json", str(src_v)])
            src_pk = int(((sj.get("streams") or [{}])[0]).get("nb_read_packets") or 0)
            root = work / f"job_{vc}_{ac}_{dur}"
            root.mkdir()
            vid = "CutLab" + f"{abs(hash(combo + str(dur))) % 100000:05d}"
            url = f"https://www.youtube.com/watch?v={vid}"
            xl = root / "check.xlsx"
            wb = Workbook()
            ws = wb.active
            ws.append(["URL", "CHANNEL ID", "TÊN KÊNH", "TITLE", "LINK VIDEO GỐC", "STATUS"])
            ws.append(["https://www.youtube.com/watch?v=ZZZZZZZZZZZ", "UC0", "k", "khác", "g0", "DONE"])
            ws.append([url, "UC1", "kênh", "テスト動画タイトル", "goc_folder", ""])
            wb.save(str(xl))
            m = type("M", (), {})()
            m.worker_signals = mod.WorkerSignals()
            m.logs = []
            m.worker_signals.log_message.connect(lambda s_, m=m: m.logs.append(s_))
            m.worker_signals.url_finished.connect(lambda u, i, ok: None)
            m.out_entry, m.keep_temp_checkbox, m.final_move_entry = _T(str(root / "out")), _C(), _T(str(root / "final"))
            m.combo_downloader, m.cutsec_entry = _Combo(), _T(str(args.cutsec))
            m.realtime_stop_event = threading.Event()
            saved = {k: getattr(mod, k) for k in ("_fast_innertube_480", "_ytdlp_resolve_480_on_warm_thread", "ARIA2C_PATH")}
            t0 = time.perf_counter()
            try:
                with MockGVS() as g:
                    bundle = {"id": vid, "title": "テスト動画タイトル", "video_url": g.add("/videoplayback/v", src_v.read_bytes()),
                              "video_format_id": itag_of[vc], "video_content_length": src_v.stat().st_size,
                              "video_mime": "video/webm" if src_v.suffix == ".webm" else "video/mp4",
                              "video_codec": {"vp9": "vp9", "av1": "av01", "h264": "avc1"}[vc],
                              "audio_url": g.add("/videoplayback/a", src_a.read_bytes()), "audio_format_id": a_itag[ac],
                              "audio_content_length": src_a.stat().st_size,
                              "audio_mime": "audio/webm" if src_a.suffix == ".webm" else "audio/mp4",
                              "duration_sec": float(dur), "source": "NATIVE_VISIONOS", "resolver_ms": 1.0}
                    mod._fast_innertube_480 = lambda u, bundle=bundle: dict(bundle)
                    mod._ytdlp_resolve_480_on_warm_thread = lambda u, robust=False: None
                    mod.ARIA2C_PATH = mod.ARIA2C_PATH or "/bin/true"
                    mod._EXCEL_DF_CACHE.clear()
                    w = mod.Worker(m, str(xl), "URL", "STATUS")
                    w._process_single_url(url, 1)
            finally:
                for k, v in saved.items():
                    setattr(mod, k, v)
            wall = round((time.perf_counter() - t0) * 1000)
            st = load_workbook(str(xl)).worksheets[0].cell(row=3, column=6).value
            kpi = dict(getattr(w, "_v94", {}).get("kpi") or {})
            log = "\n".join(m.logs)
            prows, psum = verify_parts(root / "final", ffprobe=ffprobe, ffmpeg=ffmpeg, source_dur=dur, decode=True)
            pk = sum(int(r.get("v_packets") or 0) for r in prows)
            hot, tot = kpi.get("hot_ready_ms"), kpi.get("pipeline_total_ms")
            row.update(status=st, ok=bool(st == "DONE" and prows and psum["ok_parts"] == psum["parts"]), parts=psum.get("parts"),
                       parts_ok=psum.get("ok_parts"), out_vcodec=",".join(psum.get("vcodecs") or []),
                       out_acodec=",".join(psum.get("acodecs") or []), out_container=",".join(psum.get("containers") or []),
                       branch=("DIRECT-CUT" if "DIRECT-CUT" in log and "BỎ MUX" in log else "MUX+CẮT" if "MUX COPY" in log else "?"),
                       reencode=bool(re.search(r"fallback ultrafast|FAST COPY chưa đạt", log, re.I)
                                     or (psum.get("vcodecs") and psum["vcodecs"] != [{"vp9": "vp9", "av1": "av1", "h264": "h264"}[vc]])),
                       branch_short=("PREPEND<60s" if "FAST COPY" in log or "ultrafast" in log else ""),
                       hot_ready_ms=hot, cut_mux_ms=(round(tot - hot, 1) if (hot and tot) else None), total_ms=tot, wall_ms=wall,
                       total_out_dur_s=psum.get("total_dur_s"), src_video_packets=src_pk, out_video_packets=pk,
                       max_av_start_diff_ms=psum.get("max_av_start_diff_ms"), max_av_dur_diff_ms=psum.get("max_av_dur_diff_ms"),
                       decode_fail=len(psum.get("decode_fail") or []), no_audio=len(psum.get("no_audio") or []),
                       not_keyframe_start=len(psum.get("not_keyframe_start") or []), src_mib=round(src_v.stat().st_size / MB, 2),
                       parts_with_ts_warn=psum.get("parts_with_ts_warn"), max_extra_frames=psum.get("max_extra_frames"),
                       frames_decoded=psum.get("frames_decoded"),
                       frames_expected=int(round(sum((r.get("v_dur") or 0) for r in prows) * 24)),
                       part_durs=[round(r["dur"], 2) for r in prows if r["dur"]][:40], fail=str(getattr(w, "_v94", {}).get("fail") or ""))
            rows.append(row)
            print(f"  {combo:<10} {dur:>4}s  {row['branch']:<10} STATUS={st} part {row['parts_ok']}/{row['parts']} ra={row['out_vcodec']}+"
                  f"{row['out_acodec']}/{row['out_container']} cut={row['cut_mux_ms']}ms gói video {pk}/{src_pk} lệchA/V≤{row['max_av_start_diff_ms']}ms "
                  f"giải mã lỗi={row['decode_fail']} part có ts lùi={row['parts_with_ts_warn']} khung thừa tối đa={row['max_extra_frames']} "
                  f"{'OK' if row['ok'] else 'FAIL ' + row['fail']}", flush=True)
            if not args.keep:
                shutil.rmtree(root, ignore_errors=True)
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    cols = ["combo", "dur_src", "branch", "status", "ok", "parts_ok", "parts", "out_vcodec", "out_acodec", "out_container", "reencode",
            "cut_mux_ms", "total_ms", "src_video_packets", "out_video_packets", "total_out_dur_s", "max_av_start_diff_ms",
            "max_av_dur_diff_ms", "decode_fail", "no_audio", "not_keyframe_start", "parts_with_ts_warn", "max_extra_frames",
            "frames_decoded", "frames_expected", "note"]
    md = [f"# v99 cutlab — {time.strftime('%Y-%m-%d %H:%M:%S')}", "",
          f"- CỤC BỘ (không mạng): nguồn 854x480 24fps tổng hợp, chạy NGUYÊN pipeline `{mod.V94_VERSION}` qua máy chủ giả; ffmpeg: {_run([ffmpeg, '-version'])}",
          f"- cutsec={args.cutsec}. Mỗi part được ffprobe + GIẢI MÃ TOÀN BỘ. `reencode`=True nếu codec ra KHÁC codec nguồn hoặc log báo fallback libx264.",
          "- `frames_decoded` so với `frames_expected` (= thời lượng part × 24): LỚN hơn = lộ khung 'pre-roll' trước điểm cắt (max_extra_frames / part).",
          "- `parts_with_ts_warn`: số part mà ffmpeg báo timestamp lùi khi giải mã (không phải lỗi giải mã).",
          "- Tool cắt theo thiết kế cũ: bỏ 6 s đầu, part sau lùi +2 s, phần dư cuối (< cutsec) KHÔNG xuất → tổng thời lượng ra < nguồn.",
          "- Thời gian cắt ở đây là trên máy thử với file nhỏ — KHÔNG phải số của máy bạn với file 100 MiB.", "",
          "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        md.append("| " + " | ".join("-" if r.get(c) is None else str(r.get(c)) for c in cols) + " |")
    b.REPORT.mkdir(exist_ok=True)
    base = b.REPORT / f"v99_cutlab_{time.strftime('%Y%m%d_%H%M%S')}"
    Path(f"{base}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    Path(f"{base}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print("\n".join(md))
    print(f"\n→ {base}.md / .json")
    return 0 if all(r.get("ok") or r.get("note") for r in rows) else 1


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    sub.add_parser("env")
    p = sub.add_parser("autopsy")
    p.add_argument("files", nargs="*")
    p.add_argument("--last", type=int, default=6, help="số file telemetry mới nhất (khi không chỉ định file)")
    p.add_argument("--top", type=int, default=12)
    p.add_argument("--all", action="store_true")
    p.add_argument("--job", default="")
    p = sub.add_parser("formats")
    p.add_argument("--url", action="append")
    p.add_argument("--urls")
    p.add_argument("--gap", type=float, default=2.0)
    p = sub.add_parser("verify_cut")
    p.add_argument("folder")
    p.add_argument("--source-dur", type=float, default=None)
    p.add_argument("--no-decode", action="store_true")
    p = sub.add_parser("live480")
    p.add_argument("--url", action="append")
    p.add_argument("--urls")
    p.add_argument("--combos", default="244+139,397+139,397+249,244+249,135+139")
    p.add_argument("--smart", action="store_true")
    p.add_argument("--allow-codecs", default="av01,vp9,avc1")
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--gap", type=float, default=4.0)
    p.add_argument("--seed", type=int, default=99)
    p.add_argument("--warm", action="store_true")
    p.add_argument("--decode", action="store_true")
    p.add_argument("--dl-engine", default="aria2c", choices=["aria2c", "idm"])
    p.add_argument("--cutsec", default="0")
    p.add_argument("--no-warmup", action="store_true")
    p = sub.add_parser("cutlab")
    p.add_argument("--engine", default="B", choices=["A", "B"])
    p.add_argument("--combos", default="vp9+aac,av1+aac,h264+aac,vp9+opus,av1+opus")
    p.add_argument("--durations", default="330,120")
    p.add_argument("--cutsec", default="71")
    p.add_argument("--keep", action="store_true")
    known = {"cutlab": run_cutlab, "env": run_env, "autopsy": run_autopsy, "formats": run_formats, "verify_cut": run_verify_cut, "live480": run_live_combos}
    if len(sys.argv) > 1 and sys.argv[1] in known:
        a = ap.parse_args()
        sys.exit(known[a.mode](a) or 0)
    b.main()            # mock480 / live_dl / linkcap: dùng nguyên bench_v98 với engine A = V9.8.1, B = ứng viên


if __name__ == "__main__":
    main()
