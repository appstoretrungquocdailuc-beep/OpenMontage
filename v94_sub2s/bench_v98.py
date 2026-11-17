# -*- coding: utf-8 -*-
"""bench_v98.py — đo 480p (video 244 + audio JA song song) cho V9.7.1 (A) và V9.8 (B), aria2 RPC (C), HTTP/2 (D).

Chế độ:
  mock480   Máy chủ GoogleVideo GIẢ chạy ở TIẾN TRÌNH RIÊNG (tests/mock_gvs_proc.py): Range/206, 302, replica mn,
            giới hạn băng thông tổng + theo từng kết nối, TTFB. Chạy ĐÚNG engine RANGE-TURBO của từng bản trên cùng
            dữ liệu (SHA-256 so với bản tham chiếu). Thứ tự cấu hình NGẪU NHIÊN mỗi vòng.
            → số MOCK: đo chi phí/độ đúng của engine và tác dụng tương đối, KHÔNG phải tốc độ CDN thật.
  live480   URL YouTube THẬT, chạy NGUYÊN pipeline Worker._process_single_url (480p, DIRECT-CUT/MUX, Excel tắt)
            với từng cấu hình (thứ tự ngẫu nhiên, nghỉ giữa các lượt, 429 → dừng), ffprobe file ra.
  linkcap   Giới hạn vật lý: tải CÙNG 1 video 244 nhiều lần với 32 kết nối (lượt sau là cache nóng) → Mbps tối đa
            của đường truyền + CDN. Dùng để biết "≤1.5s" có khả thi không — KHÔNG dùng làm số tốc độ live.

Bảng chính: ENGINE | WORKERS_VIDEO | WORKERS_AUDIO | HEDGE | FILE_MB | RESOLVE_MS | TTFB_MS | VIDEO_MS | AUDIO_MS |
            HOT_READY_MS | PIPELINE_TOTAL_MS | P50 | P90 | REQUESTS | ERRORS | SHA256_OK
Kết quả: perf_report/v98_<mode>_<thời gian>.jsonl/.csv/.json + _summary.md. Không bao giờ ghi URL có chữ ký."""
import argparse
import builtins
import csv
import hashlib
import importlib.util
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPORT = ROOT / "perf_report"
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))
MB = 1024 * 1024
REPORT_PREFIX = "v98"
TABLE_COLS = ["ENGINE", "WORKERS_VIDEO", "WORKERS_AUDIO", "HEDGE", "FILE_MB", "RESOLVE_MS", "TTFB_MS", "VIDEO_MS", "AUDIO_MS",
              "HOT_READY_MS", "PIPELINE_TOTAL_MS", "P50", "P90", "REQUESTS", "ERRORS", "SHA256_OK"]


def _ensure_qt():
    try:
        from PySide6 import QtWidgets, QtCore, QtGui  # noqa: F401
        return False
    except Exception:
        from conftest import _install_pyside6_stub
        _install_pyside6_stub()
        return True


def load_module(alias, path):
    if alias in sys.modules:
        return sys.modules[alias]
    spec = importlib.util.spec_from_file_location(alias, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


ENGINE_FILES = {"A": ROOT / "backup" / "V9_7_1_SUB2S_truoc_v98.py", "B": ROOT / "V9_4_SUB2S.py"}


def pct(vals, p):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    k = (len(vals) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    return round(vals[lo] + (vals[hi] - vals[lo]) * (k - lo), 2)


def sha_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ----------------------------------------------------------------------------- mock server ở tiến trình riêng
class MockProc:
    def __init__(self, spec):
        self.p = subprocess.Popen([sys.executable, str(ROOT / "tests" / "mock_gvs_proc.py"), json.dumps(spec)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        line = self.p.stdout.readline()
        self.port = int(line.split()[1])
        self.lock = threading.Lock()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _ask(self, cmd):
        with self.lock:
            self.p.stdin.write(cmd + "\n")
            self.p.stdin.flush()
            return int(self.p.stdout.readline().split()[1])

    def count(self, prefix):
        return self._ask(f"count {prefix}")

    def conns(self):
        return self._ask("conns")

    def reset(self):
        return self._ask("reset")

    def close(self):
        try:
            self.p.stdin.write("quit\n")
            self.p.stdin.flush()
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


# Kịch bản dựng theo telemetry thật của người dùng (fJ8--LQXd-Q): video 49.20 MiB / audio 5.21 MiB, TTFB video ~410 ms,
# 20 worker → ~268 Mbps (≈13–14 Mbps/kết nối), audio 14 worker → ~103 Mbps (≈7.4 Mbps/kết nối), replica mn chậm hơn (alt-win=0).
SCENARIOS = {
    "S1_perconn": dict(note="giới hạn THEO KẾT NỐI (14/7.4 Mbps), đường truyền 600 Mbps, host chính 302, replica chậm hơn",
                       link=600, v_conn=14.0, a_conn=7.4, redirect_ms=120, final_ms=200, alt_ms=420),
    "S2_linkcap": dict(note="giới hạn ĐƯỜNG TRUYỀN 380 Mbps (mỗi kết nối 40/20 Mbps), host chính 302, replica chậm hơn",
                       link=380, v_conn=40.0, a_conn=20.0, redirect_ms=120, final_ms=200, alt_ms=420),
    "S4_log1117": dict(note="theo log thật 17/11 (video 83 MB): 16.4/9.7 Mbps mỗi kết nối, đường truyền 700 Mbps, 2 host TTFB ngang nhau",
                       link=700, v_conn=16.4, a_conn=9.7, redirect_ms=100, final_ms=200, alt_ms=250),
    "S5_altslow": dict(note="theo log thật 17/11 21:23 (video 230 MB): host chính nhanh (TTFB ~0.2 s), REPLICA chậm (1.4 s), 18.6 Mbps mỗi kết nối, đường truyền 900 Mbps",
                       link=900, v_conn=18.6, a_conn=20.0, redirect_ms=60, final_ms=140, alt_ms=1400),
    "S3_mainslow": dict(note="host chính CHẬM (302 + 650 ms), replica NHANH (150 ms) — kiểm tra P90 khi CDN chính kém",
                        link=600, v_conn=14.0, a_conn=7.4, redirect_ms=150, final_ms=500, alt_ms=150),
}


def mock_spec(sc, vsize, asize):
    s = SCENARIOS[sc]
    vr, ar = s["v_conn"] * 1e6 / 8, s["a_conn"] * 1e6 / 8
    files = []
    for k, size, rate, seed in (("v", vsize, vr, 244), ("a", asize, ar, 139)):
        ex = {}
        st = s.get(f"{k}_stall")            # {"seg": i, "workers": N, "delay": giây}: range thứ i của đợt đầu bị treo trên CẢ 2 host
        if st:
            part = (size + int(st["workers"]) - 1) // int(st["workers"])
            ex = {"stall_start": part * int(st["seg"]), "stall_delay": float(st["delay"]), "stall_count": 1}
        cold = s.get(f"{k}_cold")           # {"n": N, "extra_ms": X}: N request đầu của mỗi host chậm thêm X ms
        if cold:
            ex.update({"cold_n": int(cold["n"]), "cold_delay": float(cold["extra_ms"]) / 1000})
        fin = (s.get(f"{k}_final_ms") or s["final_ms"]) / 1000
        alt = (s.get(f"{k}_alt_ms") or s["alt_ms"]) / 1000
        files.append({"path": f"/{k}/real", "size": size, "seed": seed, "mode": "ok", "kw": dict({"rate": rate, "hdr_delay": fin}, **ex)})
        files.append({"path": f"/{k}/main", "size": 0, "mode": "redirect", "kw": {"to_path": f"/{k}/real", "hdr_delay": s["redirect_ms"] / 1000}})
        files.append({"path": f"/{k}/alt", "size": size, "seed": seed, "mode": "ok", "kw": dict({"rate": rate, "hdr_delay": alt}, **ex)})
    return {"link_mbps": s["link"], "ttfb_ms": 0, "files": files}


class _SlowTruncFile:
    """Mô phỏng chi phí preallocation của Windows (_chsize ghi số 0): truncate tốn N ms/MiB. Chỉ dùng trong bench."""

    def __init__(self, f, ms_per_mib):
        self._f, self._r = f, ms_per_mib

    def truncate(self, size=None):
        if size and self._r > 0:
            time.sleep(size / MB * self._r / 1000.0)
        return self._f.truncate(size)

    def __getattr__(self, k):
        return getattr(self._f, k)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return self._f.__exit__(*a)


def _patch_open(mod, ms_per_mib):
    if ms_per_mib <= 0:
        mod.__dict__.pop("open", None)
        return
    real = builtins.open

    def op(file, mode="r", *a, **k):
        f = real(file, mode, *a, **k)
        return _SlowTruncFile(f, ms_per_mib) if ("w" in mode and "b" in mode) else f
    mod.open = op


_LEARN_STORE = {}


def run_pair(mod, mp, cfg, vsize, asize, vsha, asha, win_prealloc):
    """Video + audio SONG SONG như production. Trả 1 dòng kết quả."""
    saved = {}
    for k, v in cfg.get("flags", {}).items():
        saved[k] = getattr(mod, k, None)
        setattr(mod, k, v)
    # bộ nhớ "học" (race learn, worker tự học) RIÊNG cho từng cấu hình — không thì các biến thể B dùng chung lịch sử
    if hasattr(mod, "_V98_WL"):
        st = _LEARN_STORE.setdefault((mp.port, mp.p.pid, cfg.get("label")), {"wl": {}, "rh": []})   # id(mp) có thể bị tái dùng
        mod._V98_WL, mod._V98_RACE_HIST = st["wl"], st["rh"]
    alt = {mp.url("/v/main"): mp.url("/v/alt"), mp.url("/a/main"): mp.url("/a/alt")}
    old_alt = mod._gvs_alt_url
    mod._gvs_alt_url = lambda u: alt.get(str(u), "")
    mod._MN_RACE_BAD_UNTIL[0] = 0.0
    _patch_open(mod, win_prealloc)
    tmp = Path(tempfile.mkdtemp(prefix="v98_"))
    res = {}
    mp.reset()
    r0 = {"v": mp.count("/v/"), "a": mp.count("/a/")}
    c0 = mp.conns()
    cpu0 = time.process_time()

    def go(kind, size):
        info = {}
        t = time.perf_counter_ns()
        try:
            ok = cfg["fn"](mod, mp.url(f"/{kind}/main"), tmp, f"{kind}.bin", size, "video" if kind == "v" else "audio", info)
        except Exception as e:  # noqa: BLE001
            ok, info["fail_reason"] = False, f"{type(e).__name__}: {e}"
        res[kind] = (ok, (time.perf_counter_ns() - t) / 1e6, info)
    t0 = time.perf_counter_ns()
    ths = [threading.Thread(target=go, args=("v", vsize)), threading.Thread(target=go, args=("a", asize))]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    ready = (time.perf_counter_ns() - t0) / 1e6
    cpu = time.process_time() - cpu0
    time.sleep(0.15)                                               # để request thua cuộc (race/hedge) kịp về rồi mới đếm
    reqs = {"v": mp.count("/v/") - r0["v"], "a": mp.count("/a/") - r0["a"]}
    tcp = mp.conns() - c0
    (vok, vms, vinf), (aok, ams, ainf) = res["v"], res["a"]
    vgood = vok and (tmp / "v.bin").exists() and sha_file(tmp / "v.bin") == vsha
    agood = aok and (tmp / "a.bin").exists() and sha_file(tmp / "a.bin") == asha
    shutil.rmtree(tmp, ignore_errors=True)
    mod._gvs_alt_url = old_alt
    _patch_open(mod, 0)
    for k, v in saved.items():
        setattr(mod, k, v)
    return {"video_ok": bool(vgood), "audio_ok": bool(agood), "sha_ok": bool(vgood and agood), "VIDEO_MS": round(vms, 1),
            "AUDIO_MS": round(ams, 1), "READY_MS": round(ready, 1), "TTFB_MS": vinf.get("ttfb_ms"), "TTFB_AUDIO_MS": ainf.get("ttfb_ms"),
            "REQUESTS": reqs["v"] + reqs["a"], "req_video": reqs["v"], "req_audio": reqs["a"],
            "races": (vinf.get("races") or 0) + (ainf.get("races") or 0), "alt_wins": (vinf.get("alt_wins") or 0) + (ainf.get("alt_wins") or 0),
            "hedges": (vinf.get("hedges") or 0) + (ainf.get("hedges") or 0), "conc_peak_video": vinf.get("conc_peak"),
            "lock_wait_ms_video": vinf.get("lock_wait_ms"), "lock_hold_ms_video": vinf.get("lock_hold_ms"), "writes_video": vinf.get("writes"),
            "prealloc_ms_video": vinf.get("prealloc_ms"), "first_get_ms_video": vinf.get("first_get_ms"),
            "steals_video": vinf.get("steals"), "splits_video": vinf.get("splits"), "workers_video": vinf.get("workers_initial"),
            "workers_audio": ainf.get("workers_initial"), "xfer_mbps_video": vinf.get("xfer_mbps"), "cpu_s": round(cpu, 3),
            "takeovers_audio": ainf.get("takeovers"), "gets_audio": ainf.get("requests"), "rescues_audio": ainf.get("rescues"),
            "tail_ms_audio": ainf.get("tail_ms"), "tail_ms_video": vinf.get("tail_ms"), "rescues_video": vinf.get("rescues"),
            "TCP_CONNS": tcp, "write_ms_video": vinf.get("write_ms"), "write_mode": vinf.get("write_mode"),
            "prealloc_bg_ms_video": vinf.get("prealloc_bg_ms"), "scale_added_video": vinf.get("scale_added"),
            "workers_peak_video": vinf.get("workers_peak"), "aria2_conns_video": vinf.get("connections"),
            "ERRORS": int(not vgood) + int(not agood), "fail": (vinf.get("fail_reason") or "") + "|" + (ainf.get("fail_reason") or "")}


def _range_fn(mod, url, tmp, name, size, kind, info):
    return mod._download_http_range_turbo(url, tmp, name, lambda m: None, 1, size, kind, info=info)


def _aria2_fn(mod, url, tmp, name, size, kind, info):
    """C: aria2 RPC daemon. Số kết nối do cờ V98_ARIA2_SPLIT của cấu hình (đặt TRƯỚC khi 2 luồng chạy)."""
    if not mod.ARIA2C_PATH:
        mod.ARIA2C_PATH = shutil.which("aria2c") or shutil.which("aria2c.exe") or ""
    return mod._download_aria2c_rpc(url, tmp, name, lambda m: None, 1, size, info=info)


def _h2_fn(n):
    def fn(mod, url, tmp, name, size, kind, info):
        """D: httpx HTTP/2 (chỉ thật sự là h2 khi máy chủ HTTPS hỗ trợ ALPN h2 — mock HTTP thường sẽ là HTTP/1.1)."""
        return mod._v96_download_h2(url, tmp, name, lambda m: None, 1, size, n, info)
    return fn


# Bộ biến thể đặt sẵn (BAT trên Windows không phải gõ JSON): --b-variants preset:<tên>
_OFF = {"V98_PREALLOC_ASYNC": False, "V98_RACE_MODE": "fixed", "V98_WORKERS_LEARN": False, "V98_WRITE_MODE": "lock",
        "V98_SCALE_UP": False, "V98_ETA_FIRST_BYTE": False}  # = hành vi V9.7.1
PRESETS = {
    # từng cờ riêng so với V9.7.1 ("off"), và bản V9.8 mặc định ("v98") ± ghi pwrite (ổ đĩa Windows thật)
    "flags": {"off": dict(_OFF), "pa": dict(_OFF, V98_PREALLOC_ASYNC=True), "split": dict(_OFF, V98_WRITE_MODE="split"),
              "pwrite": dict(_OFF, V98_WRITE_MODE="pwrite"), "learn": {"V98_RACE_MODE": "learn"}, "v981": {},
              "v981_pwrite": {"V98_WRITE_MODE": "pwrite"}},
    "v98": {"v98": {}},
    "dual": {"learn": {"V98_RACE_MODE": "learn"}, "dual": {"V98_RACE_MODE": "dual"},
             "dual_fixw": {"V98_RACE_MODE": "dual", "V98_WORKERS_LEARN": False}},
    "v98_vs_off": {"v98": {}, "v98off": dict(_OFF)},
    # live480 (nguyên pipeline): V9.8 / không prewarm ffmpeg / opt-in FASTEST_VALID_480P (135 khi nhỏ hơn 244)
    "pipeline": {"v98": {}, "noprewarm": {"V94_FF_PREWARM": False}, "fv480": {"V94_FASTEST_VALID_480P": True}},
    # live_dl: V9.8 mặc định / tắt hết cờ V9.8 / + scale-up / cố định 32 worker video
    "live": {"v981": {}, "v98off": dict(_OFF), "v98learn": {"V98_RACE_MODE": "learn"}, "v981_eta0": {"V98_ETA_FIRST_BYTE": False}},
}


def _variants_arg(x):
    x = str(x or "").strip()
    if x.startswith("preset:"):
        return PRESETS[x.split(":", 1)[1]]
    return json.loads(x) if x else None


def configs_from(args, eng_mods):
    """Danh sách cấu hình (engine, nhãn, cờ). Workers video/audio, race, kích thước đoạn đầu."""
    out = []
    vws = [int(x) for x in args.workers_video.split(",") if x]
    aws = [int(x) for x in args.workers_audio.split(",") if x]
    races = [x for x in args.race.split(",") if x]
    parts = [float(x) for x in args.part_mib.split(",") if x]
    for eng in [e for e in args.engines.split(",") if e]:
        if eng in ("A", "B") and eng in eng_mods:
            for vw in vws:
                for aw in aws:
                    for rc in races:
                        for pm in (parts if eng == "B" else [0.0]):
                            flags = {"V94_RANGE_WORKERS_VIDEO": vw, "V94_RANGE_WORKERS_AUDIO": aw}
                            if rc == "adaptive":
                                if eng != "B":
                                    continue
                                flags.update(V98_RACE_MODE="adaptive", V94_FIRST_WAVE_RACE_MAX=-1)
                            else:
                                flags["V94_FIRST_WAVE_RACE_MAX"] = int(rc)
                                if eng == "B" and REPORT_PREFIX == "v98":   # bench V9.8 cũ: B không tên = race cố định
                                    flags["V98_RACE_MODE"] = "fixed"
                            variants = ({"": {}} if eng != "B" else (args.b_variants or {"": {}}))
                            for vn, vflags in variants.items():
                                fl = dict(flags)
                                if vn and rc != "adaptive":
                                    fl.pop("V98_RACE_MODE", None)      # biến thể có tên: giữ MẶC ĐỊNH của bản B (không ép fixed)
                                if eng == "B" or REPORT_PREFIX != "v98":
                                    fl["V98_PART_MIB"] = pm
                                    fl.update(args.b_flags or {})
                                    fl.update(vflags or {})
                                label = (f"{eng}{(':' + vn) if vn else ''}|v{vw or 'auto'}|a{aw or 'auto'}|race={rc}"
                                         + (f"|part={pm:g}MiB" if pm else ""))
                                out.append({"engine": eng, "label": label, "flags": fl, "fn": _range_fn, "vw": vw, "aw": aw,
                                            "hedge": rc, "mod": eng})
        elif eng.startswith("C") and "B" in eng_mods:
            for sp in [int(x) for x in args.aria2_split.split(",") if x]:
                out.append({"engine": "C", "label": f"C|aria2rpc|split={sp}",
                            "flags": {"V98_ARIA2_SPLIT": sp, "V98_ARIA2_MAX_TRIES": 1}, "fn": _aria2_fn, "vw": sp, "aw": sp,
                            "hedge": "-", "mod": "B"})
        elif eng == "D" and "B" in eng_mods:
            for n in [int(x) for x in args.h2_n.split(",") if x]:
                out.append({"engine": "D", "label": f"D|httpx-h2|n={n}", "flags": {}, "fn": _h2_fn(n), "vw": n, "aw": n, "hedge": "-",
                            "mod": "B"})
    return out


def summarize(rows):
    groups = {}
    for r in rows:
        groups.setdefault((r["scenario"], r["label"]), []).append(r)
    out = []
    for (sc, lab), rs in groups.items():
        ok = [r for r in rs if r["sha_ok"]]
        q = lambda k, p: pct([r.get(k) for r in ok if isinstance(r.get(k), (int, float))], p)
        out.append({"SCENARIO": sc, "ENGINE": lab, "WORKERS_VIDEO": rs[0]["vw"] or "auto", "WORKERS_AUDIO": rs[0]["aw"] or "auto",
                    "HEDGE": rs[0]["hedge"], "FILE_MB": round((rs[0]["vsize"] + rs[0]["asize"]) / MB, 2), "RESOLVE_MS": 0,
                    "TTFB_MS": q("TTFB_MS", 50), "VIDEO_MS": q("VIDEO_MS", 50), "AUDIO_MS": q("AUDIO_MS", 50),
                    "HOT_READY_MS": q("READY_MS", 50), "PIPELINE_TOTAL_MS": None, "P50": q("READY_MS", 50), "P90": q("READY_MS", 90),
                    "REQUESTS": q("REQUESTS", 50), "ERRORS": sum(r["ERRORS"] for r in rs), "SHA256_OK": f"{len(ok)}/{len(rs)}",
                    "n": len(rs), "lock_wait_ms_video": q("lock_wait_ms_video", 50), "lock_hold_ms_video": q("lock_hold_ms_video", 50),
                    "first_get_ms_video": q("first_get_ms_video", 50), "cpu_s": q("cpu_s", 50), "alt_wins": q("alt_wins", 50),
                    "TCP_CONNS": q("TCP_CONNS", 50), "write_ms_video": q("write_ms_video", 50),
                    "scale_added_video": q("scale_added_video", 50), "P90_VIDEO": q("VIDEO_MS", 90), "MAX": q("READY_MS", 100)})
    return out


def write_report(mode, rows, summary, notes):
    REPORT.mkdir(exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    base = REPORT / f"{REPORT_PREFIX}_{mode}_{ts}"
    with open(f"{base}.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    keys = list(dict.fromkeys(k for r in rows for k in r))
    with open(f"{base}.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow({k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v) for k, v in r.items()})
    Path(f"{base}.json").write_text(json.dumps({"notes": notes, "summary": summary}, ensure_ascii=False, indent=1, default=str),
                                    encoding="utf-8")
    cols = ["SCENARIO"] + TABLE_COLS + [c for c in (summary[0].keys() if summary else []) if c not in TABLE_COLS and c != "SCENARIO"]
    md = [f"# v98 {mode} — {ts}", ""] + [f"- {n}" for n in notes] + [
        f"- Máy: {sys.platform} | Python {sys.version.split()[0]} | CPU={os.cpu_count()}", "",
        "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for s in summary:
        md.append("| " + " | ".join("-" if s.get(c) is None else str(s.get(c)).replace("|", " / ") for c in cols) + " |")
    Path(f"{base}_summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    print(f"\n→ {base}.jsonl / .csv / .json / _summary.md")
    return base


def run_mock480(args):
    _ensure_qt()
    from mock_gvs import make_payload
    eng_mods = {}
    for e in set(x[0] for x in args.engines.split(",") if x):
        key = "B" if e in ("C", "D") else e
        if key in ENGINE_FILES and key not in eng_mods:
            eng_mods[key] = load_module(f"v98bench_{key}", ENGINE_FILES[key])
    vsize, asize = int(args.video_mib * MB), int(args.audio_mib * MB)
    vsha = hashlib.sha256(make_payload(vsize, seed=244)).hexdigest()
    asha = hashlib.sha256(make_payload(asize, seed=139)).hexdigest()
    cfgs = configs_from(args, eng_mods)
    rows = []
    rnd = random.Random(args.seed)
    for sc in [s for s in args.scenarios.split(",") if s]:
        mp = MockProc(mock_spec(sc, vsize, asize))
        try:
            for rep in range(args.reps):
                order = list(cfgs)
                rnd.shuffle(order)                                  # thứ tự NGẪU NHIÊN mỗi vòng
                for c in order:
                    r = run_pair(eng_mods[c["mod"]], mp, c, vsize, asize, vsha, asha, args.win_prealloc_ms_per_mib)
                    r.update(scenario=sc, label=c["label"], engine=c["engine"], vw=c["vw"], aw=c["aw"], hedge=c["hedge"], rep=rep,
                             vsize=vsize, asize=asize)
                    rows.append(r)
                    print(f"  {sc} rep{rep} {c['label']:<44} ready={r['READY_MS']:>7}ms v={r['VIDEO_MS']:>7} a={r['AUDIO_MS']:>7} "
                          f"req={r['REQUESTS']:>3} sha={'OK' if r['sha_ok'] else 'FAIL'} lockw={r['lock_wait_ms_video']}", flush=True)
                    time.sleep(args.gap)
        finally:
            mp.close()
    notes = [f"SỐ MOCK (127.0.0.1, server tiến trình riêng) — KHÔNG phải tốc độ YouTube. Video {args.video_mib} MiB + audio "
             f"{args.audio_mib} MiB tải SONG SONG như production; HOT_READY_MS ở đây = lúc cả 2 file xong (resolver = 0).",
             f"Preallocation Windows mô phỏng: {args.win_prealloc_ms_per_mib} ms/MiB (log thật: 96 ms cho 49.2 MiB ≈ 1.95 ms/MiB)"
             if args.win_prealloc_ms_per_mib > 0 else "Preallocation: truncate Linux (thưa, ~0 ms) — KHÔNG thể hiện chi phí Windows.",
             "Kịch bản: " + "; ".join(f"{k}: {SCENARIOS[k]['note']}" for k in args.scenarios.split(",") if k),
             f"{args.reps} vòng × {len(cfgs)} cấu hình, thứ tự ngẫu nhiên mỗi vòng (seed {args.seed})."]
    return write_report("mock480", rows, summarize(rows), notes)


# ----------------------------------------------------------------------------- LIVE (YouTube thật — chạy trên máy người dùng)
class NetTrace:
    """Đo DNS / TCP / TLS của từng KẾT NỐI MỚI (vá urllib3 TRONG BENCH, production không đổi). Không ghi host/URL."""

    def __init__(self):
        self.rows = []
        self.lock = threading.Lock()
        self.tl = threading.local()
        self._undo = []

    def __enter__(self):
        try:
            import socket
            import urllib3.connection as uc
            import urllib3.util.connection as ucc
        except Exception:  # noqa: BLE001
            return self
        tl = self.tl
        gai0, cc0 = socket.getaddrinfo, ucc.create_connection

        def gai(*a, **k):
            t = time.perf_counter()
            try:
                return gai0(*a, **k)
            finally:
                tl.dns = (time.perf_counter() - t) * 1000.0

        def cc(*a, **k):
            tl.dns = None
            t = time.perf_counter()
            sk = cc0(*a, **k)
            tl.tcp_total = (time.perf_counter() - t) * 1000.0
            return sk
        socket.getaddrinfo, ucc.create_connection = gai, cc
        self._undo += [(socket, "getaddrinfo", gai0), (ucc, "create_connection", cc0)]
        for cls, tls in ((uc.HTTPSConnection, True), (uc.HTTPConnection, False)):
            c0 = cls.__dict__.get("connect")
            if c0 is None:
                continue

            def mk(c0=c0, tls=tls):
                def connect(conn_self, *a, **k):
                    tl.tcp_total, tl.dns = None, None
                    t = time.perf_counter()
                    r = c0(conn_self, *a, **k)
                    tot = (time.perf_counter() - t) * 1000.0
                    tcp = tl.tcp_total
                    with self.lock:
                        self.rows.append({"dns_ms": None if tl.dns is None else round(tl.dns, 2),
                                          "tcp_ms": None if tcp is None else round(tcp - (tl.dns or 0.0), 2),
                                          "tls_ms": round(tot - (tcp or 0.0), 2) if tls else 0.0, "connect_ms": round(tot, 2)})
                    return r
                return connect
            setattr(cls, "connect", mk())
            self._undo.append((cls, "connect", c0))
        return self

    def take(self):
        with self.lock:
            r, self.rows = self.rows, []
        return r

    def __exit__(self, *a):
        for obj, name, orig in reversed(self._undo):
            setattr(obj, name, orig)


def _net_summary(rows):
    q = lambda k, p: pct([r[k] for r in rows if isinstance(r.get(k), (int, float))], p)
    return {"new_conns": len(rows), "dns_p50": q("dns_ms", 50), "dns_p90": q("dns_ms", 90), "tcp_p50": q("tcp_ms", 50),
            "tcp_p90": q("tcp_ms", 90), "tls_p50": q("tls_ms", 50), "tls_p90": q("tls_ms", 90), "connect_p90": q("connect_ms", 90)}


def _read_urls(args):
    urls = list(args.url or [])
    if args.urls:
        urls += [x.strip() for x in Path(args.urls).read_text(encoding="utf-8").splitlines() if x.strip() and not x.strip().startswith("#")]
    return urls


def _cold_pools(mods):
    """Mỗi lượt bắt đầu như nhau: đóng kết nối GVS đang giữ của MỌI engine (không thì engine chạy sau hưởng kết nối nóng)."""
    for m in mods.values():
        try:
            m._get_direct_media_pool().clear()
        except Exception:  # noqa: BLE001
            pass


def _ffprobe_ok(mod, f, want):
    exe = getattr(mod, "FFPROBE_PATH", None) or shutil.which("ffprobe")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "-v", "error", "-show_entries", "stream=codec_type,codec_name:format=duration", "-of", "json", str(f)],
                           capture_output=True, text=True, timeout=20)
        st = (json.loads(r.stdout or "{}").get("streams") or [])
        return any(x.get("codec_type") == want for x in st)
    except Exception:  # noqa: BLE001
        return False


def _summ_live(rows, ready_key="READY_MS"):
    groups = {}
    for r in rows:
        groups.setdefault(r["label"], []).append(r)
    out = []
    for lab, rs in groups.items():
        ok = [r for r in rs if r.get("sha_ok")]
        q = lambda k, p, src=ok: pct([r.get(k) for r in src if isinstance(r.get(k), (int, float))], p)
        first = [r for r in ok if r.get("pos_in_round") == 0]
        out.append({"ENGINE": lab, "WORKERS_VIDEO": rs[0]["vw"] or "auto", "WORKERS_AUDIO": rs[0]["aw"] or "auto", "HEDGE": rs[0]["hedge"],
                    "FILE_MB": q("FILE_MB", 50), "RESOLVE_MS": q("RESOLVE_MS", 50), "TTFB_MS": q("TTFB_MS", 50),
                    "VIDEO_MS": q("VIDEO_MS", 50), "AUDIO_MS": q("AUDIO_MS", 50), "HOT_READY_MS": q("HOT_READY_MS", 50),
                    "PIPELINE_TOTAL_MS": q("PIPELINE_TOTAL_MS", 50), "P50": q(ready_key, 50), "P90": q(ready_key, 90),
                    "REQUESTS": q("REQUESTS", 50), "ERRORS": sum(int(r.get("ERRORS") or 0) for r in rs), "SHA256_OK": f"{len(ok)}/{len(rs)}",
                    "n": len(rs), "TTFB_AUDIO_MS": q("TTFB_AUDIO_MS", 50), "MBPS_VIDEO": q("MBPS_VIDEO", 50), "MBPS_MAX": q("MBPS_VIDEO", 100),
                    "P50_first_in_round": q(ready_key, 50, first), "n_first": len(first), "new_conns": q("new_conns", 50),
                    "tls_p50": q("tls_p50", 50), "tcp_p50": q("tcp_p50", 50), "dns_p50": q("dns_p50", 50),
                    "fails": ";".join(sorted(set(str(r.get("fail")) for r in rs if r.get("ERRORS"))))[:160]})
    return sorted(out, key=lambda x: (x["P50"] is None, x["P50"] or 0))


TOOL_PATHS = ("ARIA2C_PATH", "IDM_PATH", "FFMPEG_PATH", "FFPROBE_PATH", "YTDLP_PATH")


def _share_tool_paths(mods):
    """Bản A nằm trong backup/ → nó tìm ffmpeg/, aria2c.exe… trong backup/. Dùng chung đường dẫn công cụ với bản B
    (cùng máy, cùng công cụ) để so sánh công bằng."""
    b = mods.get("B")
    ref = b if b is not None else load_module("v98bench_B", ENGINE_FILES["B"])
    for m in mods.values():
        for k in TOOL_PATHS:
            if not getattr(m, k, None) and getattr(ref, k, None):
                setattr(m, k, getattr(ref, k))


def _live_mods(args):
    _ensure_qt()
    mods = {}
    for e in [x[0] for x in args.engines.split(",") if x]:
        key = "B" if e in ("C", "D") else e
        if key in ENGINE_FILES and key not in mods:
            mods[key] = load_module(f"v98bench_{key}", ENGINE_FILES[key])
    _share_tool_paths(mods)
    for m in mods.values():
        for fn in ("_prewarm_480_visitor_data", "_start_fast_keepalive"):
            try:
                getattr(m, fn)(**({"force": True} if fn == "_prewarm_480_visitor_data" else {}))
            except Exception as e:  # noqa: BLE001
                print("warm-up:", fn, type(e).__name__)
    return mods


def run_live_dl(args):
    """live_dl: mỗi vòng resolve 1 lần (link 244 + audio JA MỚI) rồi tải CHÍNH link đó bằng mọi cấu hình, thứ tự ngẫu nhiên.
    linkcap: chỉ video, 20/32/40 kết nối, đo Mbps tối đa đường truyền + CDN (lượt sau trong vòng = cache nóng)."""
    urls = _read_urls(args)
    if not urls:
        print("❌ Chưa có URL (--url hoặc --urls file.txt). Không có URL thật thì KHÔNG có số live — bench không tự bịa số.")
        return 2
    linkcap = args.mode == "linkcap"
    if linkcap:
        args.engines, args.race, args.part_mib, args.b_variants = "B", "-1", "0", {"": {}}
        args.workers_video = args.linkcap_workers
    mods = _live_mods(args)
    cfgs = configs_from(args, mods)
    rnd = random.Random(args.seed)
    rows, stop, n403 = [], None, 0
    work = Path(tempfile.mkdtemp(prefix="v98live_"))
    res_mod = mods.get("B") or next(iter(mods.values()))
    with NetTrace() as nt:
        for rep in range(args.reps):
            for url in urls:
                if stop:
                    break
                t = time.perf_counter_ns()
                try:
                    data = res_mod._fast_innertube_480(url)
                except Exception as e:  # noqa: BLE001
                    data, err = None, type(e).__name__
                resolve_ms = round((time.perf_counter_ns() - t) / 1e6, 2)
                vid = res_mod._extract_youtube_video_id(url)
                if not data or not data.get("video_url"):
                    print(f"  [{vid}] resolve không có link 480p native → bỏ vòng")
                    rows.append({"label": "RESOLVE", "video_id": vid, "rep": rep, "ERRORS": 1, "fail": "resolve_none", "vw": "-", "aw": "-",
                                 "hedge": "-", "RESOLVE_MS": resolve_ms})
                    continue
                v_url, a_url = data["video_url"], (None if linkcap else data.get("audio_url"))
                vsize, asize = int(data.get("video_content_length") or 0), int(data.get("audio_content_length") or 0)
                if not vsize or (a_url and not asize):
                    print(f"  [{vid}] thiếu content-length → bỏ vòng")
                    continue
                order = list(cfgs)
                rnd.shuffle(order)
                rnd_rows = []
                nt.take()
                for pos, c in enumerate(order):
                    mod = mods[c["mod"]]
                    if not args.warm:
                        _cold_pools(mods)
                    saved = {k: getattr(mod, k, None) for k in c["flags"]}
                    for k, v in c["flags"].items():
                        setattr(mod, k, v)
                    tmp = work / f"r{rep}_{pos}"
                    tmp.mkdir(parents=True, exist_ok=True)
                    res = {}

                    def go(kind, u, size):
                        info = {}
                        t1 = time.perf_counter_ns()
                        try:
                            ok = c["fn"](mod, u, tmp, f"{kind}.bin", size, "video" if kind == "v" else "audio", info)
                        except Exception as e:  # noqa: BLE001
                            ok, info["fail_reason"] = False, f"EXC:{type(e).__name__}"
                        res[kind] = (ok, (time.perf_counter_ns() - t1) / 1e6, info)
                    jobs = [("v", v_url, vsize)] + ([("a", a_url, asize)] if a_url else [])
                    t0 = time.perf_counter_ns()
                    ths = [threading.Thread(target=go, args=j) for j in jobs]
                    for th in ths:
                        th.start()
                    for th in ths:
                        th.join()
                    ready = round((time.perf_counter_ns() - t0) / 1e6, 1)
                    for k, v in saved.items():
                        setattr(mod, k, v)
                    net = _net_summary(nt.take())
                    (vok, vms, vinf) = res["v"]
                    aok, ams, ainf = res.get("a", (True, None, {}))
                    vf, af = tmp / "v.bin", tmp / "a.bin"
                    vgood = bool(vok and vf.exists() and vf.stat().st_size == vsize)
                    agood = bool(aok and (not a_url or (af.exists() and af.stat().st_size == asize)))
                    row = {"label": c["label"], "engine": c["engine"], "vw": c["vw"], "aw": c["aw"], "hedge": c["hedge"], "rep": rep,
                           "video_id": vid, "pos_in_round": pos, "itag_video": str(data.get("video_format_id") or ""),
                           "itag_audio": str(data.get("audio_format_id") or ""), "source": str(data.get("source") or ""),
                           "FILE_MB": round((vsize + (asize if a_url else 0)) / MB, 2), "RESOLVE_MS": resolve_ms,
                           "TTFB_MS": vinf.get("ttfb_ms"), "TTFB_AUDIO_MS": ainf.get("ttfb_ms"), "VIDEO_MS": round(vms, 1),
                           "AUDIO_MS": None if ams is None else round(ams, 1), "READY_MS": ready,
                           "DL_READY_PLUS_RESOLVE_MS": round(ready + resolve_ms, 1),
                           "REQUESTS": int(vinf.get("requests") or 0) + int(ainf.get("requests") or 0) if c["engine"] in ("A", "B") else None,
                           "MBPS_VIDEO": round(vsize * 8 / 1e6 / max(1e-3, (vms - float(vinf.get("ttfb_ms") or 0)) / 1000.0), 1) if vgood else None,
                           "video_ok": vgood, "audio_ok": agood, "ERRORS": int(not vgood) + int(not agood),
                           "fail": (vinf.get("fail_reason") or "") + "|" + (ainf.get("fail_reason") or ""),
                           "sha_v": sha_file(vf) if vgood else None, "sha_a": sha_file(af) if (agood and a_url) else None}
                    row.update(net)
                    for kk in ("races", "alt_wins", "hedges", "steals", "splits", "retries", "workers_initial", "workers_peak", "conc_peak",
                               "lock_wait_ms", "lock_hold_ms", "write_ms", "prealloc_ms", "prealloc_bg_ms", "first_get_ms", "scale_added",
                               "connections", "http_version", "resized"):
                        if kk in vinf:
                            row[f"v_{kk}"] = vinf[kk]
                        if kk in ainf:
                            row[f"a_{kk}"] = ainf[kk]
                    if vgood and not rnd_rows:
                        row["ffprobe_video"] = _ffprobe_ok(mod, vf, "video")
                        row["ffprobe_audio"] = _ffprobe_ok(mod, af, "audio") if a_url and agood else None
                    shutil.rmtree(tmp, ignore_errors=True)
                    rnd_rows.append(row)
                    print(f"  rep{rep} [{vid}] {c['label']:<40} ready={ready:>7}ms v={row['VIDEO_MS']:>7} a={row['AUDIO_MS']} "
                          f"ttfb={row['TTFB_MS']} {row['MBPS_VIDEO']}Mbps conns={net['new_conns']} "
                          f"{'OK' if not row['ERRORS'] else 'FAIL ' + row['fail']}", flush=True)
                    if "429" in row["fail"]:
                        stop = "HTTP 429 — dừng bench ngay (không dồn request)"
                        break
                    if "403" in row["fail"] or "410" in row["fail"]:
                        n403 += 1
                        print("  ⚠️ 403/410 → bỏ phần còn lại của vòng (không thử lại cùng link)")
                        if n403 >= 2:
                            stop = "2 lần 403/410 — dừng bench"
                        break
                    time.sleep(args.gap)
                # SHA-256: mọi cấu hình trong vòng phải ra CÙNG nội dung (đa số làm chuẩn)
                for key in ("sha_v", "sha_a"):
                    vals = [r[key] for r in rnd_rows if r.get(key)]
                    ref = max(set(vals), key=vals.count) if vals else None
                    for r in rnd_rows:
                        r[key + "_ok"] = (r.get(key) == ref) if r.get(key) else (key == "sha_a" and not a_url)
                for r in rnd_rows:
                    r["sha_ok"] = bool(r["video_ok"] and r["audio_ok"] and r["sha_v_ok"] and r["sha_a_ok"])
                rows += rnd_rows
                time.sleep(args.gap * 2)
    shutil.rmtree(work, ignore_errors=True)
    summ = _summ_live([r for r in rows if r["label"] != "RESOLVE"])
    notes = [f"SỐ LIVE (YouTube thật) — {len(urls)} URL × {args.reps} vòng; mỗi vòng resolve 1 lần rồi tải CÙNG link bằng mọi cấu hình, "
             f"thứ tự ngẫu nhiên (seed {args.seed}), nghỉ {args.gap}s; kết nối GVS {'giữ nóng' if args.warm else 'đóng trước mỗi lượt (lạnh)'}.",
             "P50/P90 = thời điểm cả video+audio xong (tải song song). HOT_READY thật của pipeline: dùng chế độ live480. "
             "DL_READY_PLUS_RESOLVE_MS = resolve + tải (≈ HOT_READY khi DIRECT-CUT).",
             "SHA256_OK = đúng dung lượng + SHA-256 trùng với đa số cấu hình trong cùng vòng; vòng đầu mỗi cấu hình có ffprobe.",
             "P50_first_in_round: chỉ lượt ĐẦU mỗi vòng (cache CDN lạnh) — so với P50 để thấy ảnh hưởng cache."]
    if stop:
        notes.append(f"⛔ {stop}")
    if linkcap:
        ok = [r for r in rows if r.get("sha_ok") and r.get("MBPS_VIDEO")]
        if ok:
            mx = max(r["MBPS_VIDEO"] for r in ok)
            res_p50 = pct([r["RESOLVE_MS"] for r in ok], 50) or 0
            ttfb_p50 = pct([r["TTFB_MS"] for r in ok if r.get("TTFB_MS")], 50) or 0
            for mib in (40, 49.2, 60):
                floor = res_p50 + ttfb_p50 + mib * MB * 8 / 1e6 / mx * 1000
                notes.append(f"Giới hạn vật lý ước tính (Mbps tối đa đo được {mx:.0f}, resolve P50 {res_p50:.0f} ms, TTFB P50 {ttfb_p50:.0f} ms): "
                             f"video {mib} MiB → HOT_READY ≥ {floor:.0f} ms (chưa tính audio chia băng thông)")
    base = write_report(args.mode, rows, summ, notes)
    return 0 if not stop else 3


def run_live480(args):
    """live480: NGUYÊN pipeline Worker._process_single_url (resolve → tải → DIRECT-CUT/MUX → ffprobe) cho từng cấu hình.
    Số HOT_READY / PIPELINE_TOTAL lấy từ KPI của chính tool. Thứ tự ngẫu nhiên, nghỉ giữa lượt, 429/403 → dừng."""
    urls = _read_urls(args)
    if not urls:
        print("❌ Chưa có URL (--url hoặc --urls file.txt). Không có URL thật thì KHÔNG có số live.")
        return 2
    import bench_v93
    qt_stub = _ensure_qt()
    if not qt_stub:
        from PySide6.QtWidgets import QApplication
        _app = QApplication.instance() or QApplication([])  # noqa: F841
    mods = _live_mods(args)
    cfgs = [c for c in configs_from(args, mods) if c["engine"] in ("A", "B")]
    rnd = random.Random(args.seed)
    rows, stop = [], None
    work = Path(tempfile.mkdtemp(prefix="v98p_"))
    if not args.no_warmup:
        for k, m in mods.items():
            print(f"[warm-up {k}] {urls[0]} (không tính)")
            bench_v93.run_live_once(m, "v94", urls[0], work / f"warm_{k}", args.dl_engine, args.cutsec, "480hard")
            time.sleep(args.gap)
    with NetTrace() as nt:
        for rep in range(args.reps):
            for url in urls:
                if stop:
                    break
                order = list(cfgs)
                rnd.shuffle(order)
                for pos, c in enumerate(order):
                    mod = mods[c["mod"]]
                    if not args.warm:
                        _cold_pools(mods)
                    saved = {k: getattr(mod, k, None) for k in c["flags"]}
                    for k, v in c["flags"].items():
                        setattr(mod, k, v)
                    nt.take()
                    try:
                        r = bench_v93.run_live_once(mod, "v94", url, work / f"r{rep}_{pos}", args.dl_engine, args.cutsec, "480hard")
                    finally:
                        for k, v in saved.items():
                            setattr(mod, k, v)
                    net = _net_summary(nt.take())
                    row = {"label": c["label"], "engine": c["engine"], "vw": c["vw"], "aw": c["aw"], "hedge": c["hedge"], "rep": rep,
                           "pos_in_round": pos, "video_id": r.get("video_id"),
                           "RESOLVE_MS": r.get("resolve_video_ms"), "TTFB_MS": r.get("video_ttfb_ms"), "TTFB_AUDIO_MS": r.get("audio_ttfb_ms"),
                           "VIDEO_MS": r.get("video_ms"), "AUDIO_MS": r.get("audio_ms"), "HOT_READY_MS": r.get("hot_ready_ms"),
                           "PIPELINE_TOTAL_MS": r.get("pipeline_total_ms"), "READY_MS": r.get("hot_ready_ms"),
                           "FILE_MB": round((float(r.get("video_total") or 0) + float(r.get("audio_total") or 0)) / MB, 2) or None,
                           "REQUESTS": (r.get("video_requests") or 0) + (r.get("audio_requests") or 0),
                           "MBPS_VIDEO": r.get("video_mbps"), "sha_ok": bool(r.get("success") and r.get("valid_av")),
                           "ERRORS": int(not r.get("success")), "fail": str(r.get("fail_reason") or ""),
                           "prewarm": r.get("prewarm"), "outputs": r.get("outputs")}
                    row.update(net)
                    row.update({k: v for k, v in r.items() if k not in row and isinstance(v, (int, float, str, bool))})
                    rows.append(row)
                    print(f"  rep{rep} {c['label']:<40} hot={row['HOT_READY_MS']} total={row['PIPELINE_TOTAL_MS']} v={row['VIDEO_MS']} "
                          f"a={row['AUDIO_MS']} {'OK' if row['sha_ok'] else 'FAIL ' + row['fail']}", flush=True)
                    shutil.rmtree(work / f"r{rep}_{pos}", ignore_errors=True)
                    if int(r.get("http429") or 0) or "429" in row["fail"]:
                        stop = "HTTP 429 — dừng bench"
                        break
                    if int(r.get("http403") or 0) >= 2:
                        stop = "403 lặp lại — dừng bench"
                        break
                    time.sleep(args.gap)
    shutil.rmtree(work, ignore_errors=True)
    notes = [f"SỐ LIVE — NGUYÊN pipeline 480p (Excel tắt) — {len(urls)} URL × {args.reps} vòng, thứ tự ngẫu nhiên, nghỉ {args.gap}s.",
             "HOT_READY_MS / PIPELINE_TOTAL_MS = KPI của chính tool (mốc 0 = lúc worker nhận URL). SHA256_OK ở chế độ này = "
             "pipeline thành công + file ra ffprobe có cả video lẫn audio (file cắt/mux nên không so SHA được — so SHA ở live_dl)."]
    if stop:
        notes.append(f"⛔ {stop}")
    write_report("live480", rows, _summ_live(rows, "HOT_READY_MS"), notes)
    return 0 if not stop else 3


def add_live_args(sub):
    for name in ("live_dl", "linkcap", "live480"):
        p = sub.add_parser(name)
        p.add_argument("--url", action="append")
        p.add_argument("--urls")
        p.add_argument("--engines", default="A,B" if name != "live_dl" else "A,B,C")
        p.add_argument("--workers-video", default="0")
        p.add_argument("--workers-audio", default="0")
        p.add_argument("--race", default="-1")
        p.add_argument("--part-mib", default="0")
        p.add_argument("--aria2-split", default="8,12,16")
        p.add_argument("--h2-n", default="8,16")
        p.add_argument("--b-flags", type=json.loads, default=None)
        p.add_argument("--b-variants", type=_variants_arg, default=None)
        p.add_argument("--reps", type=int, default=3)
        p.add_argument("--gap", type=float, default=3.0)
        p.add_argument("--seed", type=int, default=98)
        p.add_argument("--warm", action="store_true", help="KHÔNG đóng kết nối GVS giữa các lượt")
        p.add_argument("--linkcap-workers", default="20,32,40")
        p.add_argument("--dl-engine", default="aria2c", choices=["aria2c", "idm"])
        p.add_argument("--cutsec", default="0")
        p.add_argument("--no-warmup", action="store_true")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("mock480")
    p.add_argument("--engines", default="A,B")
    p.add_argument("--scenarios", default="S1_perconn,S2_linkcap,S3_mainslow")
    p.add_argument("--workers-video", default="20")
    p.add_argument("--workers-audio", default="14")
    p.add_argument("--race", default="-1")
    p.add_argument("--part-mib", default="0")
    p.add_argument("--aria2-split", default="8,12,16")
    p.add_argument("--video-mib", type=float, default=49.20)
    p.add_argument("--audio-mib", type=float, default=5.21)
    p.add_argument("--reps", type=int, default=5)
    p.add_argument("--gap", type=float, default=0.2)
    p.add_argument("--seed", type=int, default=98)
    p.add_argument("--win-prealloc-ms-per-mib", type=float, default=0.0)
    p.add_argument("--b-flags", type=json.loads, default=None, help='cờ thêm cho engine B, vd {"V98_PREALLOC_ASYNC": true}')
    p.add_argument("--b-variants", type=_variants_arg, default=None,
                   help='nhiều biến thể B trong CÙNG lượt đo, vd {"base": {}, "pa": {"V98_PREALLOC_ASYNC": true}}')
    p.add_argument("--h2-n", default="8,16")
    add_live_args(sub)
    a = ap.parse_args()
    if a.mode == "mock480":
        run_mock480(a)
    elif a.mode in ("live_dl", "linkcap"):
        sys.exit(run_live_dl(a) or 0)
    elif a.mode == "live480":
        sys.exit(run_live480(a) or 0)


if __name__ == "__main__":
    main()
