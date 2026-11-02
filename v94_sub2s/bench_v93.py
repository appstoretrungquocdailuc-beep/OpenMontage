# -*- coding: utf-8 -*-
"""bench_v93.py — đo baseline V9.3 và A/B với V9.4 (cùng máy, cùng mạng, cùng video).

Chế độ:
  offline   Mock GoogleVideo cục bộ (giới hạn băng thông tổng + per-connection + TTFB):
            so engine Range V9.3 vs V9.4 trên kịch bản size đúng/sai/không biết, 403, Content-Range sai.
            → số đo ĐỘ ĐÚNG và CHI PHÍ ENGINE, KHÔNG phải tốc độ CDN thật.
  sweep     Như offline nhưng quét số worker 4/6/8/12/16/24/32/40 (phương pháp; số live mới là quyết định).
  trigger   Độ trễ scanner ghi Excel + append trigger_wakeup.txt → downloader thấy URL mới (V9.3 vs V9.4).
  live      URL YouTube thật (CLI hoặc file), chạy NGUYÊN pipeline Worker._process_single_url của
            từng bản, interleaved ABBA, có warm-up, có giãn cách chống 429, ffprobe file kết quả.

Ví dụ (Windows, cùng thư mục với V9_3_AUDIO.py / V9_4_SUB2S.py):
  python bench_v93.py offline --reps 5
  python bench_v93.py trigger --reps 30
  python bench_v93.py live --urls urls.txt --reps 3 --gap 5
  python bench_v93.py live --url https://www.youtube.com/shorts/XXXXXXXXXXX --versions v94 --workers-video 8,16,24

Kết quả: perf_report/<mode>_<timestamp>.jsonl + .csv + _summary.md (median/p90/p95, success, under_2s).
URL media có chữ ký KHÔNG bao giờ được ghi ra file kết quả.
"""
import argparse
import csv
import importlib.util
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPORT = ROOT / "perf_report"
sys.path.insert(0, str(ROOT / "tests"))
MB = 1024 * 1024


# ----------------------------------------------------------------------------- utils
def _ensure_qt():
    try:
        from PySide6 import QtWidgets, QtCore, QtGui  # noqa: F401
        return False
    except Exception:
        from conftest import _install_pyside6_stub   # stub chỉ cho máy không có PySide6
        _install_pyside6_stub()
        return True


def load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


MODS = {"v93": "V9_3_AUDIO", "v94": "V9_4_SUB2S"}


def pct(vals, p):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    k = (len(vals) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    return round(vals[lo] + (vals[hi] - vals[lo]) * (k - lo), 2)


def summarize(rows, group_keys, metrics):
    groups = {}
    for r in rows:
        groups.setdefault(tuple(r.get(k) for k in group_keys), []).append(r)
    out = []
    for key, rs in groups.items():
        row = dict(zip(group_keys, key))
        row["n"] = len(rs)
        row["success_rate"] = round(sum(1 for r in rs if r.get("success")) / len(rs), 3)
        for m in metrics:
            vals = [r.get(m) for r in rs if r.get("success") and isinstance(r.get(m), (int, float))]
            row[f"{m}_p50"] = pct(vals, 50)
            row[f"{m}_p90"] = pct(vals, 90)
            row[f"{m}_p95"] = pct(vals, 95)
            row[f"{m}_max"] = round(max(vals), 2) if vals else None
        for m in ("media_ready_ms", "hot_ready_ms"):
            vals = [r.get(m) for r in rs if isinstance(r.get(m), (int, float)) and r.get("success")]
            if vals:
                row[f"under_2s_{m}"] = round(sum(1 for v in vals if v < 2000) / len(rs), 3)
        fails = {}
        for r in rs:
            if not r.get("success"):
                fr = str(r.get("fail_reason") or "?")[:60]
                fails[fr] = fails.get(fr, 0) + 1
        row["fail_reasons"] = json.dumps(fails, ensure_ascii=False) if fails else ""
        out.append(row)
    return out


def write_report(mode, rows, summary, notes):
    REPORT.mkdir(exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    base = REPORT / f"{mode}_{ts}"
    with open(f"{base}.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    keys = sorted({k for r in rows for k in r.keys()})
    with open(f"{base}.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow({k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                        for k, v in r.items()})
    with open(f"{base}_summary.md", "w", encoding="utf-8") as f:
        f.write(f"# {mode} — {ts}\n\n")
        for n in notes:
            f.write(f"- {n}\n")
        envs = {k: v for k, v in os.environ.items() if k.startswith("V94_") and k != "V94_TELEMETRY_DIR"}
        f.write(f"- Cờ môi trường V94_*: {json.dumps(envs) if envs else 'mặc định'}\n")
        f.write(f"- Máy: {sys.platform} | Python {sys.version.split()[0]} | CPU={os.cpu_count()}\n")
        if summary:
            cols = list(summary[0].keys())
            for s in summary[1:]:
                cols += [c for c in s.keys() if c not in cols]
            f.write("\n| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n")
            for s in summary:
                f.write("| " + " | ".join("" if s.get(c) is None else str(s.get(c)) for c in cols) + " |\n")
    print(f"\n→ {base}.jsonl / .csv / _summary.md")
    return base


def print_table(summary, cols):
    print("\n" + " | ".join(cols))
    for s in summary:
        print(" | ".join(str(s.get(c, "")) for c in cols))


# ----------------------------------------------------------------------------- offline
def _engine_call(mod, ver, url, tmp, name, expected, kind, use_smart=False):
    info = {}
    t0 = time.perf_counter_ns()
    th0 = threading.active_count()
    if use_smart:
        fn = mod.download_smart_audio if kind == "audio" else mod.download_smart_progressive
        if ver == "v94":
            ok = fn(url, tmp, name, lambda m: None, 1, expected, info=info)
        else:
            ok = fn(url, tmp, name, lambda m: None, 1, expected)
    elif ver == "v94":
        ok = mod._download_http_range_turbo(url, tmp, name, lambda m: None, 1, expected, kind, info=info)
    else:
        ok = mod._download_http_range_turbo(url, tmp, name, lambda m: None, 1, expected, kind)
    ms = (time.perf_counter_ns() - t0) / 1e6
    return ok, ms, info, threading.active_count() - th0


def run_offline(args, sweep=False):
    _ensure_qt()
    from mock_gvs import MockGVS, make_payload, sha256
    mods = {v: load(MODS[v]) for v in args.versions}
    # Mạng giả lập: băng thông tổng + giới hạn/kết nối + TTFB (tham số CLI).
    link = int(args.link_mbps * 1e6 / 8)
    rows = []
    notes = [f"MOCK GVS cục bộ: link={args.link_mbps}Mbps, per-conn video={args.conn_mbps_video}Mbps "
             f"audio={args.conn_mbps_audio}Mbps, ttfb={args.ttfb_ms}ms. KHÔNG phải số đo CDN thật.",
             "aria2/IDM bị tắt trong offline (chỉ đo engine Range).",
             "Thứ tự chạy interleaved ABAB, mỗi kịch bản lặp --reps lần."]
    with MockGVS(link_bytes_per_sec=link, ttfb=args.ttfb_ms / 1000.0) as g:
        vrate = args.conn_mbps_video * 1e6 / 8
        arate = args.conn_mbps_audio * 1e6 / 8
        cases = []
        if sweep:
            for w in [int(x) for x in args.workers.split(",")]:
                cases.append((f"video24_w{w}", 24 * MB, 24 * MB, "video", vrate, False, w))
                cases.append((f"audio11_w{w}", 11 * MB, 11 * MB, "audio", arate, False, w))
        else:
            cases = [
                ("audio_0.4MB", 400 * 1024, 400 * 1024, "audio", arate, False, 0),
                ("audio_3MB", 3 * MB, 3 * MB, "audio", arate, False, 0),
                ("audio_11MB", 11 * MB, 11 * MB, "audio", arate, False, 0),
                ("video_3MB", 3 * MB, 3 * MB, "video", vrate, False, 0),
                ("video_24MB", 24 * MB, 24 * MB, "video", vrate, False, 0),
                ("video_90MB", 90 * MB, 90 * MB, "video", vrate, False, 0),
                ("video_24MB_size_est_minus3pct", 24 * MB, int(24 * MB * 0.97), "video", vrate, False, 0),
                ("audio_3MB_size_est_plus8pct", 3 * MB, int(3 * MB * 1.08), "audio", arate, False, 0),
                ("audio_0.4MB_size_unknown_smart", 400 * 1024, 0, "audio", arate, True, 0),
                ("audio_2MB_size_unknown_smart", 2 * MB, 0, "audio", arate, True, 0),
            ]
        payload_cache = {}
        for (cname, real, expected, kind, rate, smart, wov) in cases:
            if real not in payload_cache:
                payload_cache[real] = make_payload(real, seed=real & 0xFFFF)
            data = payload_cache[real]
            digest = sha256(data)
            path = "/videoplayback/" + re.sub(r"[^A-Za-z0-9_.-]", "_", cname)
            url = g.add(path, data, rate=rate)
            for rep in range(args.reps):
                order = list(args.versions) if rep % 2 == 0 else list(reversed(args.versions))
                for ver in order:
                    mod = mods[ver]
                    if wov:
                        _set_workers(mod, ver, kind, wov)
                    tmp = Path(tempfile.mkdtemp(prefix="bench_"))
                    n_req0 = len(g.requests_for(path))
                    conns0 = g.conns
                    ok, ms, info, dthreads = _engine_call(mod, ver, url, tmp, "o.bin", expected, kind, smart)
                    f = tmp / "o.bin"
                    good = ok and f.exists() and sha256(f.read_bytes()) == digest
                    rows.append({
                        "case": cname, "version": ver, "rep": rep, "success": bool(good), "engine_ok": bool(ok),
                        "ms": round(ms, 2), "mbps": round(real * 8 / 1e6 / max(1e-6, ms / 1000), 1),
                        "bytes": real, "expected": expected, "http_requests": len(g.requests_for(path)) - n_req0,
                        "tcp_conns": g.conns - conns0, "threads_delta": dthreads,
                        "workers": info.get("workers_initial"), "workers_peak": info.get("workers_peak"),
                        "steals": info.get("steals"), "retries": info.get("retries"),
                        "resized": info.get("resized"), "lock_wait_ms": info.get("lock_wait_ms"),
                        "ttfb_ms": info.get("ttfb_ms"), "fail_reason": info.get("fail_reason") if not good else "",
                    })
                    shutil.rmtree(tmp, ignore_errors=True)
                    if wov:
                        _set_workers(mod, ver, kind, 0)
                    time.sleep(0.05)
            print(f"  {cname}: " + ", ".join(
                f"{v}={pct([r['ms'] for r in rows if r['case']==cname and r['version']==v and r['success']],50)}ms"
                for v in args.versions))
        # 2 kịch bản đúng/sai thuần (không tính thời gian)
        for cname, mode_kw in (("http_403", {"mode": "status", "code": 403}),
                               ("bad_content_range_shift4096", {"mode": "badcr", "shift": 4096})):
            data = make_payload(3 * MB, seed=5)
            url = g.add(f"/videoplayback/{cname}", data, **mode_kw)
            for ver in args.versions:
                tmp = Path(tempfile.mkdtemp(prefix="bench_"))
                n0 = len(g.requests_for(f"/videoplayback/{cname}"))
                ok, ms, info, _ = _engine_call(mods[ver], ver, url, tmp, "o.bin", len(data), "video")
                f = tmp / "o.bin"
                intact = ok and f.exists() and sha256(f.read_bytes()) == sha256(data)
                rows.append({"case": cname, "version": ver, "rep": 0,
                             "success": (not ok) or intact,   # đúng = từ chối, hoặc file nguyên vẹn
                             "engine_ok": bool(ok), "file_intact": bool(intact), "ms": round(ms, 2),
                             "http_requests": len(g.requests_for(f"/videoplayback/{cname}")) - n0,
                             "fail_reason": info.get("fail_reason") or ("CORRUPT_FILE_ACCEPTED" if ok and not intact else "")})
                shutil.rmtree(tmp, ignore_errors=True)
    summ = summarize(rows, ["case", "version"], ["ms", "http_requests", "tcp_conns", "threads_delta"])
    print_table(summ, ["case", "version", "n", "success_rate", "ms_p50", "ms_p90", "ms_p95",
                       "http_requests_p50", "fail_reasons"])
    write_report("sweep" if sweep else "offline", rows, summ, notes)


def _set_workers(mod, ver, kind, n):
    if ver == "v94":
        if kind == "audio":
            mod.V94_RANGE_WORKERS_AUDIO = n
        else:
            mod.V94_RANGE_WORKERS_VIDEO = n
        return
    # V9.3: không có cờ → bọc hàm heuristic (chỉ trong tiến trình bench)
    if not hasattr(mod, "_bench_orig_rwc"):
        mod._bench_orig_rwc = mod._range_worker_count
        mod._bench_ov = {}
    mod._bench_ov[kind] = n
    orig = mod._bench_orig_rwc
    mod._range_worker_count = lambda tb, mk="video": (mod._bench_ov.get(mk) or orig(tb, mk))


# ----------------------------------------------------------------------------- trigger
def run_trigger(args):
    _ensure_qt()
    from openpyxl import Workbook, load_workbook
    rows = []
    for ver in args.versions:
        mod = load(MODS[ver])
        d = Path(tempfile.mkdtemp(prefix="trig_"))
        xl = d / "check.xlsx"
        trig = d / mod.REALTIME_WAKEUP_FILE
        wb = Workbook(); ws = wb.active
        ws.append(["URL", "CHANNEL ID", "TÊN KÊNH", "TITLE", "LINK VIDEO GỐC", "STATUS"])
        for i in range(args.rows):
            ws.append([f"https://www.youtube.com/shorts/OLD{i:08d}", "UC", "k", "t", "g", "DONE"])
        wb.save(xl)
        trig.write_text("start\n")
        mod._EXCEL_DF_CACHE.clear()
        seen = {u for u, _ in mod.read_links_excel_url_only(str(xl), lambda m: None)[0]}
        stop = threading.Event()
        for rep in range(args.reps):
            new_url = f"https://www.youtube.com/shorts/N{ver}{rep:08d}"[:42]
            tsig = mod._rt_trigger_signature([trig])
            # scanner: ghi Excel rồi append trigger (đúng thứ tự V34)
            wb = load_workbook(xl); wb.active.append([new_url, "UC", "k", "t", "g", ""]); wb.save(xl)
            # chữ ký Excel lấy SAU khi ghi → chỉ trigger đánh thức: đo đúng đường trigger (poll interval)
            esig = mod._rt_file_signature(str(xl))
            box = {}

            def listener():
                r = mod._rt_wait_for_wakeup(stop, [trig], str(xl), tsig, esig)
                t_det = time.perf_counter()
                links, _, _ = mod.read_links_excel_url_only(str(xl), lambda m: None)
                t_read = time.perf_counter()
                box.update(reason=r[2], t_det=t_det, t_read=t_read,
                           found=any(u == new_url for u, _ in links))
            # listener bắt đầu chờ TRƯỚC khi có trigger (như vòng realtime)
            th = threading.Thread(target=listener)
            th.start()
            time.sleep(0.002)
            t_trig = time.perf_counter()
            with open(trig, "a") as f:
                f.write(f"{rep}\n")
            th.join(5)
            rows.append({"version": ver, "rep": rep, "success": bool(box.get("found")),
                         "wake": box.get("reason"), "rows": args.rows,
                         "detect_ms": round((box.get("t_det", t_trig) - t_trig) * 1000, 2),
                         "excel_ms": round((box.get("t_read", 0) - box.get("t_det", 0)) * 1000, 2),
                         "trigger_to_url_ms": round((box.get("t_read", t_trig) - t_trig) * 1000, 2)})
            time.sleep(0.03)
        shutil.rmtree(d, ignore_errors=True)
    summ = summarize(rows, ["version"], ["detect_ms", "excel_ms", "trigger_to_url_ms"])
    print_table(summ, ["version", "n", "success_rate", "detect_ms_p50", "excel_ms_p50", "excel_ms_p95",
                       "trigger_to_url_ms_p50", "trigger_to_url_ms_p95"])
    write_report("trigger", rows, summ, [
        f"Excel {args.rows} dòng. Ghi Excel trước, append trigger sau (đúng thứ tự scanner V34).",
        "Chữ ký Excel chụp sau khi scanner ghi → chỉ trigger đánh thức (đo đúng chu kỳ poll 25ms vs 10ms).",
        "trigger_to_url_ms = từ lúc append trigger tới lúc danh sách URL mới đã đọc xong (sẵn sàng submit worker).",
    ])


# ----------------------------------------------------------------------------- live
class _T:
    def __init__(self, v): self.v = v
    def text(self): return self.v


class _C:
    def __init__(self, v): self.v = v
    def isChecked(self): return self.v


class _Combo:
    def __init__(self, v): self.v = v
    def currentData(self): return self.v


_RX = {
    "resolve_video_ms": re.compile(r"⚡ (?:244|135) READY \[[^\]]*\] \[[^\]]*\] ([\d.]+)s"),
    "resolve_audio_ms": re.compile(r"⚡ JA AUDIO READY \[[^\]]*\] \[[^\]]*\] ([\d.]+)s"),
    "video_ms": re.compile(r"HOÀN TẤT VIDEO \| [\d.]+MB \| ([\d.]+)s"),
    "audio_ms": re.compile(r"HOÀN TẤT AUDIO \| [\d.]+MB \| ([\d.]+)s"),
    "mux_ms": re.compile(r"MUX COPY 480P\+JA: ([\d.]+)s"),
    "hot_ready_ms": re.compile(r"HOT 480P READY: ([\d.]+)s"),
    "pipeline_total_ms": re.compile(r"PIPELINE TOTAL: ([\d.]+)s"),
}


def _parse_log(lines):
    out = {}
    txt = "\n".join(lines)
    for k, rx in _RX.items():
        m = rx.search(txt)
        if m:
            out[k] = round(float(m.group(1)) * 1000.0, 2)
    m = re.search(r"⚡ (244|135) READY \[[^\]]*\] \[([^\]]*)\]", txt)
    if m:
        out["video_itag"], out["source"] = m.group(1), m.group(2)
    m = re.search(r"fmt=(\d+)", txt)
    if m:
        out["audio_itag"] = m.group(1)
    out["inferred_single_ja"] = "[SINGLE-TRACK-JA]" in txt
    out["n_hedge_lines"] = txt.count("hedge=")
    out["safe_refresh"] = "SAFE-REFRESH" in txt
    out["http403"] = len(re.findall(r"HTTP_403", txt))
    out["http429"] = len(re.findall(r"HTTP_429", txt))
    if "media_ready_ms" not in out and "hot_ready_ms" in out:
        out["media_ready_ms"] = round(out["hot_ready_ms"] - out.get("mux_ms", 0.0), 2)
    return out


def _probe(ffprobe, f):
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-show_entries",
                            "stream=codec_type,codec_name,width,height:stream_tags=language:format=duration",
                            "-of", "json", str(f)], capture_output=True, text=True, timeout=20)
        return json.loads(r.stdout or "{}")
    except Exception:
        return {}


def run_live_once(mod, ver, url, work, engine, cutsec):
    out = work / "out"; final = work / "final"
    for p in (out, final):
        p.mkdir(parents=True, exist_ok=True)
    m = types.SimpleNamespace()
    m.worker_signals = mod.WorkerSignals()
    logs, done = [], []
    m.worker_signals.log_message.connect(lambda s: logs.append(s))
    m.worker_signals.url_finished.connect(lambda u, i, ok: done.append(ok))
    m.out_entry = _T(str(out)); m.keep_temp_checkbox = _C(False); m.final_move_entry = _T(str(final))
    m.combo_downloader = _Combo(engine); m.cutsec_entry = _T(str(cutsec))
    m.realtime_stop_event = threading.Event()
    w = mod.Worker(m, "", "URL", "STATUS")
    th0 = threading.active_count()
    cpu0 = time.process_time()
    t0 = time.perf_counter()
    w._process_single_url(url, None)
    wall_ms = (time.perf_counter() - t0) * 1000
    row = {"version": ver, "video_id": mod._extract_youtube_video_id(url), "success": bool(done and done[-1]),
           "wall_ms": round(wall_ms, 2), "cpu_s": round(time.process_time() - cpu0, 3),
           "threads_after": threading.active_count(), "threads_delta": threading.active_count() - th0}
    row.update(_parse_log(logs))
    if ver == "v94" and getattr(w, "_v94", None):
        k = w._v94.get("kpi", {})
        row.update({kk: vv for kk, vv in k.items() if isinstance(vv, (int, float))})
        for kind in ("video", "audio"):
            inf = w._v94.get(kind) or {}
            for kk in ("requests", "hedges", "races", "steals", "retries", "workers_initial", "workers_peak",
                       "mbps", "ttfb_ms", "lock_wait_ms", "new_conns", "total", "resized"):
                if kk in inf:
                    row[f"{kind}_{kk}"] = inf[kk]
        row["fail_reason"] = w._v94.get("fail") or ""
    if not row["success"] and not row.get("fail_reason"):
        tail = [l for l in logs if "❌" in l or "⚠️" in l]
        row["fail_reason"] = (tail[-1] if tail else "unknown")[:120]
    files = [p for p in final.rglob("*") if p.is_file()] + [p for p in out.rglob("*.mp4")]
    vals = []
    for f in files[:6]:
        pr = _probe(mod.FFPROBE_PATH or "ffprobe", f)
        st = pr.get("streams") or []
        v = next((s for s in st if s.get("codec_type") == "video"), {})
        a = next((s for s in st if s.get("codec_type") == "audio"), {})
        vals.append({"file": f.name[-40:], "vcodec": v.get("codec_name"), "w": v.get("width"), "h": v.get("height"),
                     "acodec": a.get("codec_name"), "dur": (pr.get("format") or {}).get("duration")})
    row["outputs"] = vals
    row["valid_av"] = bool(vals) and all(x["vcodec"] and x["acodec"] for x in vals)
    if row["success"] and not row["valid_av"]:
        row["success"] = False
        row["fail_reason"] = "output_invalid"
    return row


def run_live(args):
    qt_stub = _ensure_qt()
    if qt_stub:
        print("⚠️ Không có PySide6: dùng stub (chỉ hợp lệ cho test). Trên Windows hãy cài PySide6.")
    else:
        from PySide6.QtWidgets import QApplication
        _app = QApplication.instance() or QApplication([])  # noqa: F841
    urls = list(args.url or [])
    if args.urls:
        urls += [l.strip() for l in Path(args.urls).read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.strip().startswith("#")]
    if not urls:
        print("❌ Chưa có URL. Dùng --url ... hoặc --urls urls.txt (mỗi dòng 1 URL). "
              "Không có URL hợp lệ thì KHÔNG có số đo live — bench không tự bịa số.")
        return 2
    mods = {v: load(MODS[v]) for v in args.versions}
    for mod in mods.values():
        try:
            mod._prewarm_480_visitor_data(force=True)
            mod._start_fast_keepalive()
            mod._prewarm_ytdlp_480_resolver_thread()
            if args.engine == "aria2c":
                mod._start_aria2_rpc_daemon()
        except Exception as e:
            print("warm-up:", e)
    work_root = Path(tempfile.mkdtemp(prefix="live_"))
    rows = []
    wsets = [int(x) for x in (args.workers_video or "0").split(",")]
    # warm-up: 1 lần/bản, không tính
    if not args.no_warmup:
        for ver, mod in mods.items():
            print(f"[warm-up] {ver} {urls[0]}")
            run_live_once(mod, ver, urls[0], work_root / f"warm_{ver}", args.engine, args.cutsec)
            time.sleep(args.gap)
    i = 0
    for rep in range(args.reps):
        for url in urls:
            for wv in wsets:
                order = list(mods) if (i % 2 == 0) else list(reversed(list(mods)))   # ABBA
                i += 1
                for ver in order:
                    mod = mods[ver]
                    if wv:
                        _set_workers(mod, ver, "video", wv)
                    row = run_live_once(mod, ver, url, work_root / f"r{i}_{ver}", args.engine, args.cutsec)
                    row.update({"rep": rep, "workers_video_override": wv, "url_kind":
                                "short" if "/shorts/" in url else "watch"})
                    rows.append(row)
                    if wv:
                        _set_workers(mod, ver, "video", 0)
                    print(f"  {ver} {row['video_id']} ok={row['success']} hot={row.get('hot_ready_ms')}ms "
                          f"media={row.get('media_ready_ms')}ms total={row.get('pipeline_total_ms')}ms "
                          f"{'FAIL:' + str(row.get('fail_reason')) if not row['success'] else ''}")
                    shutil.rmtree(work_root / f"r{i}_{ver}", ignore_errors=True)
                    time.sleep(args.gap)
    metrics = ["resolve_video_ms", "resolve_audio_ms", "video_ms", "audio_ms", "media_ready_ms",
               "mux_ms", "hot_ready_ms", "pipeline_total_ms", "threads_after", "cpu_s"]
    summ = summarize(rows, ["version", "workers_video_override"], metrics)
    summ_vid = summarize(rows, ["video_id", "version"], ["media_ready_ms", "hot_ready_ms"])
    print_table(summ, ["version", "workers_video_override", "n", "success_rate", "resolve_video_ms_p50",
                       "video_ms_p50", "audio_ms_p50", "media_ready_ms_p50", "media_ready_ms_p90",
                       "hot_ready_ms_p50", "hot_ready_ms_p90", "hot_ready_ms_p95", "under_2s_hot_ready_ms",
                       "fail_reasons"])
    write_report("live", rows, summ + summ_vid, [
        f"URLs={len(urls)} reps={args.reps} gap={args.gap}s engine={args.engine} ABBA interleaved, warm-up "
        f"{'OFF' if args.no_warmup else 'ON (bỏ khỏi thống kê)'}.",
        "Mốc 0 = lúc Worker._process_single_url nhận URL (giống log 'HOT 480P READY ... từ lúc worker nhận URL').",
        "media_ready_ms = KPI1 (đủ video+audio). hot_ready_ms = KPI2 (file mux xong). V9.3: media_ready ≈ hot_ready − mux.",
        "Mỗi file kết quả được ffprobe: phải có video + audio, nếu không → success=False (output_invalid).",
    ])
    return 0


def run_pipeline_offline(args):
    """A/B toàn pipeline Worker._process_single_url với resolver giả + mock GVS + ffmpeg thật."""
    qt_stub = _ensure_qt()
    if not qt_stub:
        from PySide6.QtWidgets import QApplication
        _app = QApplication.instance() or QApplication([])  # noqa: F841
    from mock_gvs import MockGVS
    ff = shutil.which("ffmpeg")
    if not ff:
        print("❌ cần ffmpeg"); return 2
    media_dir = Path(tempfile.mkdtemp(prefix="pmedia_"))
    clips = {}
    for dur, size, rate in ((30, "480x854", 10), (120, "854x480", 5)):
        v, a = media_dir / f"v{dur}.webm", media_dir / f"a{dur}.m4a"
        subprocess.run([ff, "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}", "-t", str(dur),
                        "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-b:v", "400k", "-an",
                        str(v)], check=True, capture_output=True)
        subprocess.run([ff, "-y", "-f", "lavfi", "-i", f"sine=duration={dur}", "-c:a", "aac", "-b:a", "64k",
                        str(a)], check=True, capture_output=True)
        clips[dur] = (v, a)
    mods = {v: load(MODS[v]) for v in args.versions}
    rows = []
    link = int(args.link_mbps * 1e6 / 8)
    with MockGVS(link_bytes_per_sec=link, ttfb=args.ttfb_ms / 1000.0) as g:
        for dur, (v, a) in clips.items():
            vu = g.add(f"/videoplayback/v{dur}", v.read_bytes(), rate=args.conn_mbps_video * 1e6 / 8)
            au = g.add(f"/videoplayback/a{dur}", a.read_bytes(), rate=args.conn_mbps_audio * 1e6 / 8)
            for rep in range(args.reps):
                order = list(mods) if rep % 2 == 0 else list(reversed(list(mods)))
                for ver in order:
                    mod = mods[ver]
                    vid = f"Pipe{dur:03d}{rep:02d}{ver[-1]}"[:11].ljust(11, "x")
                    bundle = {"id": vid, "title": "テスト", "video_url": vu, "video_format_id": "244",
                              "video_content_length": v.stat().st_size, "audio_url": au, "audio_format_id": "140",
                              "audio_content_length": a.stat().st_size, "audio_mime": "audio/mp4",
                              "duration_sec": float(dur), "source": "NATIVE_VISIONOS", "resolver_ms": 0.0}
                    mod._fast_innertube_480 = (lambda b: (lambda u: dict(b)))(bundle)
                    mod._ytdlp_resolve_480_on_warm_thread = lambda u, robust=False: None
                    mod.ARIA2C_PATH = mod.ARIA2C_PATH or sys.executable
                    work = Path(tempfile.mkdtemp(prefix="pipe_"))
                    row = run_live_once(mod, ver, f"https://www.youtube.com/shorts/{vid}", work, "aria2c", "0")
                    row.update({"case": f"dur{dur}_v{v.stat().st_size // 1024}KiB_a{a.stat().st_size // 1024}KiB",
                                "rep": rep})
                    rows.append(row)
                    shutil.rmtree(work, ignore_errors=True)
                    time.sleep(0.05)
    metrics = ["resolve_video_ms", "video_ms", "audio_ms", "media_ready_ms", "mux_ms", "validate_ms",
               "hot_ready_ms", "file_validated_ms", "pipeline_total_ms", "wall_ms"]
    summ = summarize(rows, ["case", "version"], metrics)
    print_table(summ, ["case", "version", "n", "success_rate", "media_ready_ms_p50", "mux_ms_p50",
                       "hot_ready_ms_p50", "hot_ready_ms_p90", "validate_ms_p50", "pipeline_total_ms_p50",
                       "under_2s_hot_ready_ms"])
    write_report("pipeline_offline", rows, summ, [
        "Resolver GIẢ (trả URL mock ngay) → resolve≈0: KHÔNG đại diện độ trễ Innertube/yt-dlp thật.",
        f"Mock GVS: link={args.link_mbps}Mbps per-conn video={args.conn_mbps_video} audio={args.conn_mbps_audio}Mbps "
        f"ttfb={args.ttfb_ms}ms. ffmpeg/ffprobe thật cho mux/validate/move.",
        "V9.4 xác thực file trước DONE bằng log của chính lệnh mux (validate_ms≈0; ffprobe chỉ là fallback).",
    ])
    shutil.rmtree(media_dir, ignore_errors=True)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)
    for name in ("offline", "sweep"):
        p = sub.add_parser(name)
        p.add_argument("--reps", type=int, default=5)
        p.add_argument("--versions", default="v93,v94")
        p.add_argument("--link-mbps", type=float, default=400.0)
        p.add_argument("--conn-mbps-video", type=float, default=40.0)
        p.add_argument("--conn-mbps-audio", type=float, default=4.0)
        p.add_argument("--ttfb-ms", type=float, default=40.0)
        p.add_argument("--workers", default="4,6,8,12,16,24,32,40")
    p = sub.add_parser("pipeline")
    p.add_argument("--reps", type=int, default=5)
    p.add_argument("--versions", default="v93,v94")
    p.add_argument("--link-mbps", type=float, default=400.0)
    p.add_argument("--conn-mbps-video", type=float, default=40.0)
    p.add_argument("--conn-mbps-audio", type=float, default=4.0)
    p.add_argument("--ttfb-ms", type=float, default=40.0)
    p = sub.add_parser("trigger")
    p.add_argument("--reps", type=int, default=30)
    p.add_argument("--rows", type=int, default=500)
    p.add_argument("--versions", default="v93,v94")
    p = sub.add_parser("live")
    p.add_argument("--url", action="append")
    p.add_argument("--urls")
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--gap", type=float, default=5.0, help="giây nghỉ giữa 2 lần chạy (chống 429)")
    p.add_argument("--versions", default="v93,v94")
    p.add_argument("--engine", default="aria2c", choices=["aria2c", "idm"])
    p.add_argument("--cutsec", default="0", help="Cut Seconds; 0 = luôn MUX (đo KPI2 thuần)")
    p.add_argument("--workers-video", default="0", help="vd 8,16,24 để A/B số worker; 0=heuristic")
    p.add_argument("--no-warmup", action="store_true")
    p.add_argument("--config", help="benchmark_config.json (ghi đè tham số)")
    args = ap.parse_args()
    cfg = getattr(args, "config", None)
    if cfg:
        for k, v in json.loads(Path(cfg).read_text(encoding="utf-8")).items():
            if k.startswith("_"):
                continue
            if k == "env":
                os.environ.update({str(a): str(b) for a, b in v.items()})
                continue
            setattr(args, k.replace("-", "_"), v)
    if isinstance(getattr(args, "versions", None), str):
        args.versions = [v.strip() for v in args.versions.split(",") if v.strip()]
    if args.mode == "offline":
        run_offline(args)
    elif args.mode == "sweep":
        run_offline(args, sweep=True)
    elif args.mode == "pipeline":
        return run_pipeline_offline(args)
    elif args.mode == "trigger":
        run_trigger(args)
    else:
        return run_live(args)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
