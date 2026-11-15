# -*- coding: utf-8 -*-
"""BENCH TẢI V2 trên máy chủ video GIẢ (MockGVSHosts: TLS thật, nhiều host *.googlevideo.com trên loopback) — SỐ MOCK, KHÔNG phải
số YouTube. Chạy ĐÚNG bench_dl của WS360 (Engine.process thật: resolve giả 150 ms → tải → kiểm → ffprobe), thứ tự xoay vòng,
mỗi lượt link MỚI (path mới), 2 video × 10 vòng = 20 lượt/biến thể.

Mô phỏng (chọn theo đặc điểm đã đo trên máy người dùng: TTFB lớn do bắt tay + 302 + chờ máy chủ):
  * mỗi kết nối TLS MỚI tốn 60 ms (≈ RTT TCP+TLS) | link mới ở rrN---sn-aaa (N ngẫu nhiên) → 302 sang rrN---sn-bbb
  * chờ máy chủ mỗi request: 150 ms, 10% lượt 1500 ms (đuôi chậm) | mn=sn-aaa,sn-bbb,sn-ccc (sn-ccc phục vụ thẳng)
Không mô phỏng được: chi phí nạp kho chứng chỉ Windows (#1 chỉ đo được trên Windows thật).
Thêm: #5 (link client khác khi 403) đo riêng: thời gian tới DONE khi link client thắng bị 403, cờ bật/tắt.
Dùng: python tests/bench_dl_v2_mock.py [reps]"""
import itertools
import json
import os
import random
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
import ws360_net as NET  # noqa: E402
from mock_gvs import MockGVS, MockGVSHosts, install_dns, make_cert  # noqa: E402

ws = T.ws
VARIANTS = ["warm_all", "v2_ssl", "v2_redirect", "v2_hedge", "v2_full", "v2_full_auto", "warm_all_auto"]


def make_media(work):
    out = {}
    for name, dur in (("short", 20), ("long", 60)):
        p = work / f"{name}.mp4"
        subprocess.run([shutil.which("ffmpeg"), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25", "-f",
                        "lavfi", "-i", f"sine=duration={dur}", "-t", str(dur), "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "500k",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(p)], check=True)
        out[name] = p.read_bytes()
    return out


def main(reps=10):
    work = Path(tempfile.mkdtemp(prefix="dlv2mock_"))
    crt, key = make_cert(work)
    media = make_media(work)
    undo = install_dns(NET)
    # giữ ấm: NetWarmer gốc bỏ cổng khỏi tên máy chủ (thật luôn là 443) → mock cần giữ cổng; bỏ ping Innertube thật (không có mạng)
    NET.NetWarmer.learn = lambda self, u: self.media.__setitem__(u.split("/")[2], time.time()) if "googlevideo.com" in str(u) else None
    NET.NetWarmer.targets = lambda self: [(h, 1) for h in list(self.media)[-6:]]
    real_pool = ws.pool
    real_resolve, real_player = ws.Resolver.resolve, ws.Resolver._player

    def pool_with_ca():
        p = real_pool()
        p.connection_pool_kw.setdefault("ca_certs", crt)
        return p
    ws.pool = pool_with_ca
    out = {"note": "SỐ MOCK (127.0.0.1, TLS thật, bắt tay 60 ms/kết nối mới, chờ máy chủ 150 ms/10% 1500 ms) — KHÔNG phải YouTube"}
    with MockGVSHosts(crt, key, conn_delay=0.06, seed=11) as g:
        for sn in ("sn-aaa", "sn-bbb", "sn-ccc"):
            for n in range(1, 21):
                h = f"rr{n}---{sn}.googlevideo.com"
                g.host_rules[h] = {"server_wait": (0.15, 0.10, 1.5)}
                if sn == "sn-aaa":
                    g.host_rules[h]["redirect_to"] = f"rr{n}---sn-bbb.googlevideo.com"
        cnt = itertools.count()
        rnd = random.Random(5)

        def fake_resolve(self, vid, deadline_s=2.5):
            time.sleep(0.15)                                              # resolver ~150 ms (như bench_mock_local)
            data = media["long" if vid.startswith("LONG") else "short"]
            path = f"/vp/{vid}/{next(cnt)}"                               # link MỚI mỗi lượt
            g.add(path, data)
            url = g.url(f"rr{rnd.randint(1, 20)}---sn-aaa.googlevideo.com", path, mn=("sn-aaa", "sn-bbb", "sn-ccc"))
            return {"pick": {"kind": "progressive", "audio": None, "video": T.mkfmt(url, 18, 360, 'video/mp4; codecs="avc1, mp4a"', len(data))},
                    "source": "MOCK", "client": "mock", "net": {}}
        ws.Resolver.resolve = fake_resolve
        base = work / "bench"
        base.mkdir()
        cfg = dict(ws.DEFAULT_CONFIG)
        res = ws.bench_dl(cfg, base, ["SHORT000001", "LONG0000001"], reps=reps, variants=VARIANTS)
        out["bench_dl"] = {v: {"n": s["n"], "fail": s["fail"], "total_ms_p50_p90_p95": s["total_ms"], "ttfb_ms_p50_p90_p95": s["ttfb_ms"]}
                           for v, s in res["summary"].items()}
        out["conclusion"] = res.get("conclusion")
        for f in ("BENCH_DL.md", "BENCH_DL_SUMMARY.md", "BENCH_DL.csv"):
            shutil.copyfile(base / f, ROOT / f.replace("BENCH_DL", "BENCH_DL_V2_MOCK"))
        out["ssl_contexts_created"] = NET.SSL_STATS["created"]
        out["dl_stats"] = dict(ws.DL_STATS)
        out["ticket_kept"] = dict(NET.TICKET_STATS)
    ws.pool = real_pool
    ws.Resolver.resolve = real_resolve                       # #5 dùng Resolver.resolve THẬT (race 3 client) với _player giả
    undo()
    # ---- #5: link client thắng bị 403 → thời gian tới DONE (cờ bật vs tắt), 5 lượt mỗi bên, MockGVS HTTP
    r5 = {}
    with MockGVS() as gv:
        for on in (False, True, False, True, False, True, False, True, False, True):
            vid = f"ALT{len(r5.get(on, [])) + 10 * on:08d}"
            good = gv.add(f"/vp/{vid}", media["short"], "ok")
            bad = gv.add(f"/vp/{vid}_bad", b"", "status", code=403, hdr_delay=0.2)

            def player(self, v, client, good=good, bad=bad):
                if client != "android_vr":
                    time.sleep(0.05)
                return {"client": client, "verdict": "ok", "net": {}, "title": "t", "pick": {
                    "kind": "progressive", "audio": None,
                    "video": T.mkfmt(bad if client == "android_vr" else good, 18, 360, 'video/mp4; codecs="avc1, mp4a"', len(media["short"]))}}
            ws.Resolver._player = player
            e = T.make_engine(work / f"e{vid}" if (work / f"e{vid}").mkdir() is None else None, T.CH[:1], excel_enabled=False,
                              dl_alt_url_on_403=on, retry_schedule_sec=[1.0, 2.0])
            e.start(websub=False)
            t0 = time.perf_counter()
            e.on_event(T.CH[0], vid, "WEBSUB", T.TS)
            row = T.wait_state(e, vid, t=30)
            dt = (time.perf_counter() - t0) * 1000
            e.stop()
            r5.setdefault(on, []).append({"ms": round(dt), "state": row["state"], "attempts": row["attempts"]})
    ws.Resolver._player = real_player
    out["item5_alt_url_on_403"] = {("on" if k else "off"): {"runs": v, "median_ms": sorted(x["ms"] for x in v)[len(v) // 2]} for k, v in r5.items()}
    (ROOT / "BENCH_DL_V2_MOCK.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "item5_alt_url_on_403"}, ensure_ascii=False, indent=1))
    print(json.dumps(out["item5_alt_url_on_403"], ensure_ascii=False))
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 10)
