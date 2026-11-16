# -*- coding: utf-8 -*-
"""BENCH ARIA2 RPC vs downloader WS360 trên máy chủ video GIẢ (MockGVS HTTP, 127.0.0.1) — SỐ MOCK, KHÔNG phải YouTube.
Mô phỏng theo log thật của người dùng (video 9,86 MB): mỗi request chờ máy chủ 160 ms, link chuyển hướng 2 lần (3 chặng),
mỗi kết nối bị giới hạn ~68 Mbit/s (9,86 MB truyền trong 1151 ms sau byte đầu). File MP4 thật ~9,5 MB (ffprobe được).
Chạy ĐÚNG bench_aria2 của WS360 (cùng URL mỗi vòng, xoay vòng, kiểm SHA/MP4/ffprobe) + đếm số request mỗi kiểu tải.
Dùng: python tests/bench_aria2_mock.py [số_vòng]"""
import itertools
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE.parents[1] / "tests"))
import test_ws360 as T  # noqa: E402
from mock_gvs import MockGVS  # noqa: E402

ws = T.ws
RATE = 8_500_000          # byte/s mỗi kết nối (~68 Mbit/s)
WAIT = 0.16               # chờ máy chủ mỗi request


def main(reps=10):
    work = Path(tempfile.mkdtemp(prefix="a2mock_"))
    mp4 = work / "v.mp4"
    subprocess.run([shutil.which("ffmpeg"), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25", "-f", "lavfi",
                    "-i", "sine=duration=60", "-t", "60", "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "1150k", "-maxrate", "1150k",
                    "-bufsize", "2M", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(mp4)], check=True)
    data = mp4.read_bytes()
    out = {"note": f"SỐ MOCK (127.0.0.1) — KHÔNG phải YouTube. File {len(data) / 1e6:.2f} MB, chờ máy chủ {WAIT * 1000:.0f} ms/request, "
                   f"2 lần chuyển hướng, {RATE * 8 / 1e6:.0f} Mbit/s mỗi kết nối"}
    ws.ARIA2C = shutil.which("aria2c")
    cnt = itertools.count()
    with MockGVS(ttfb=WAIT) as g:
        def resolve(v, deadline_s=2.5):
            k = next(cnt)
            final = f"/c/{v}/{k}"
            g.add(final, data, "ok", rate=RATE)
            g.add(f"/b/{v}/{k}", b"", "redirect", to=g.url(final))
            url = g.add(f"/a/{v}/{k}", b"", "redirect", to=g.url(f"/b/{v}/{k}"))
            return {"pick": {"kind": "progressive", "audio": None, "video": T.mkfmt(url, 18, 360, 'video/mp4; codecs="avc1, mp4a"', len(data))},
                    "source": "MOCK"}
        cfg = dict(ws.DEFAULT_CONFIG, net_warm=False)
        base = work / "bench"
        base.mkdir()
        res = ws.bench_aria2(cfg, base, ["MOCKVIDEO01"], reps=reps, conns=(1, 4, 8, 12), resolve=resolve)
        for f in ("BENCH_ARIA2.md", "BENCH_ARIA2_SUMMARY.md", "BENCH_ARIA2.csv"):
            shutil.copyfile(base / f, ROOT / f.replace("BENCH_ARIA2", "BENCH_ARIA2_MOCK"))
        out["summary"] = {k: {kk: vv for kk, vv in v.items() if kk in ("n", "ok", "invalid", "errors", "total_med", "total_p90", "decision",
                                                                         "gain_median_ms", "ci_median", "ci_p90", "target_hits", "target_n")}
                          for k, v in res["conclusion"].items() if isinstance(v, dict)}
        out["best"] = res["conclusion"].get("best")
        # số request mỗi lần tải (chuyển hướng: mỗi kết nối aria2 tự đi lại các chặng 302)
        reqs = {}
        for var in ("ws360", 1, 4, 8, 12):
            plan = resolve("REQCOUNT001")
            fmt = plan["pick"]["video"]
            before = len(g.log)
            dest = work / f"r_{var}.mp4"
            if var == "ws360":
                ws.Engine._fetch_one(type("E", (), {"stop_ev": __import__("threading").Event(), "log": lambda *a: None,
                                                    "cfg": cfg})(), fmt, dest, {}, 1)
            else:
                info = {}
                ws.download_aria2_rpc(fmt["url"], dest, len(data), var, info)
            time.sleep(0.2)
            reqs[str(var)] = len(g.log) - before
            dest.unlink(missing_ok=True)
        out["requests_per_download"] = reqs
    (ROOT / "BENCH_ARIA2_MOCK.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 10)
