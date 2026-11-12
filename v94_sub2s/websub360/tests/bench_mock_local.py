# -*- coding: utf-8 -*-
"""Benchmark CỤC BỘ (máy chủ video GIẢ trên 127.0.0.1, có giới hạn băng thông/độ trễ mô phỏng) — KHÔNG phải số đo YouTube.
Dùng để kiểm tra harness --bench và đo độ trễ nội bộ thật (webhook POST → hàng đợi → bắt đầu resolver)."""
import hashlib, hmac, json, sys, time, threading, urllib.request
from datetime import datetime, timezone
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parents[1] / "tests"))
import test_ws360 as T
from mock_gvs import MockGVS
ws = T.ws
out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent
tmp = out_dir / "_bench_tmp"; tmp.mkdir(exist_ok=True)
import subprocess, shutil
FF = shutil.which("ffmpeg")
media = {}
for name, dur in (("short", 20), ("long", 120)):
    p = tmp / f"{name}.mp4"
    if not p.exists():
        subprocess.run([FF, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25", "-f", "lavfi",
                        "-i", f"sine=duration={dur}", "-t", str(dur), "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "500k",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(p)], check=True)
    media[name] = p.read_bytes()
# 1) BENCH harness: link 100 Mbps / kết nối 25 Mbps, TTFB 150 ms (mô phỏng)
with MockGVS(link_bytes_per_sec=100e6 / 8, ttfb=0.15) as g:
    urls = {k: g.add(f"/b/{k}", v, "ok", rate=25e6 / 8) for k, v in media.items()}
    def fake_resolve(self, vid, deadline_s=2.5):
        time.sleep(0.15)                      # mô phỏng resolver ~150 ms
        k = "short" if vid.startswith("SHORT") else "long"
        return {"pick": {"kind": "progressive", "video": T.mkfmt(urls[k], 18, 360, 'video/mp4; codecs="avc1, mp4a"', len(media[k])),
                         "audio": None}, "source": "MOCK"}
    ws.Resolver.resolve = fake_resolve
    ws.ARIA2C = None
    (out_dir / "BENCHMARK_MOCK_LOCAL.csv").unlink(missing_ok=True)
    cfg = dict(ws.DEFAULT_CONFIG)
    b = out_dir / "_bench_base"; b.mkdir(exist_ok=True)
    ws.bench(cfg, b, ["SHORT000001", "LONG0000001"], reps=3)
    src = b / "BENCHMARK.csv"
    lines = src.read_text(encoding="utf-8").replace(",real\n", ",LOCAL_MOCK(127.0.0.1: link 100Mbps, 25Mbps/conn, TTFB 150ms)\n")
    (out_dir / "BENCHMARK_MOCK_LOCAL.csv").write_text(lines, encoding="utf-8")
# 2) Độ trễ nội bộ qua webhook thật (HTTP POST có chữ ký) — 300 sự kiện, không tải (workers tắt) để đo đúng đoạn nhận→hàng đợi
e = T.make_engine(tmp / "lat", [T.CH[0]]) if (tmp / "lat").mkdir(exist_ok=True) is None else None
e.start(api=False, feed=False, workers=False)
port = e.httpd.server_address[1]
for i in range(300):
    vid = f"LAT{i:08d}"
    body = T.atom(T.CH[0], vid, datetime.now(timezone.utc))
    sig = hmac.new(e.secret.encode(), body, hashlib.sha1).hexdigest()
    urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/websub", data=body, headers={"X-Hub-Signature": f"sha1={sig}"}), timeout=3).read()
time.sleep(0.5)
enq = [r["enqueue_ms"] for r in e.db.all("SELECT enqueue_ms FROM videos")]
# enqueue → resolver start: bật 3 worker, resolver giả trả lỗi ngay để chỉ đo thời gian lấy việc khỏi hàng đợi
e.stop()
e2 = T.make_engine(tmp / "lat2", [T.CH[0]]) if (tmp / "lat2").mkdir(exist_ok=True) is None else None
ws.Resolver.resolve = lambda self, v, deadline_s=2.5: (_ for _ in ()).throw(ws.Fatal("bench"))
e2.start(websub=False, api=False, feed=False)
for i in range(200):
    e2.on_event(T.CH[0], f"RS{i:09d}", "WEBSUB", datetime.now(timezone.utc))
    time.sleep(0.01)
time.sleep(1.5)
rs = [r["resolver_start_ms"] for r in e2.db.all("SELECT resolver_start_ms FROM videos WHERE resolver_start_ms IS NOT NULL")]
e2.stop()
res = {"webhook_recv_to_enqueue_ms": {"n": len(enq), "p50": ws.pct(enq, 50), "p90": ws.pct(enq, 90), "p99": ws.pct(enq, 99), "max": max(enq)},
       "event_to_resolver_start_ms": {"n": len(rs), "p50": ws.pct(rs, 50), "p90": ws.pct(rs, 90), "p99": ws.pct(rs, 99), "max": max(rs)}}
(out_dir / "LATENCY_MOCK_LOCAL.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
print(json.dumps(res, indent=2))
shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(out_dir / "_bench_base", ignore_errors=True)
