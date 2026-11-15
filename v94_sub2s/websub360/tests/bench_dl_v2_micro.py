# -*- coding: utf-8 -*-
"""ĐO NHỎ từng hạng mục tải v2 (SỐ MOCK trên 127.0.0.1 — KHÔNG phải số YouTube):
  #1  thời gian mở 1 kết nối TLS MỚI: SSLContext dùng chung vs tạo mới mỗi kết nối (+ số context tạo ra)
  #7  kiểm nhanh MP4 (mp4_quick_check) vs ffprobe trên cùng file
  #8  download_single 8,6 MB: khúc 256 KB + đệm file 1 MiB (cũ) vs khúc 1 MiB + ghi thẳng (dl_hotpath_v2)
Lưu ý: trên Linux, nạp kho chứng chỉ rẻ (OpenSSL đọc thư mục lười) → #1 KHÔNG thể hiện chi phí của Windows (duyệt kho chứng chỉ hệ thống).
Dùng: python tests/bench_dl_v2_micro.py"""
import json
import shutil
import statistics
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


def med(xs):
    return round(statistics.median(xs), 2)


def p90(xs):
    return round(ws.pct(xs, 90), 2)


def main():
    work = Path(tempfile.mkdtemp(prefix="dlv2micro_"))
    out = {"note": "SỐ MOCK (127.0.0.1) — KHÔNG phải YouTube"}
    crt, key = make_cert(work)
    undo = install_dns(NET)
    # ---- #1: 20 kết nối TLS mới mỗi bên, xen kẽ; đếm số SSLContext tạo ra (bọc create_urllib3_context như test)
    import urllib3.connection as uc
    import urllib3.util.ssl_ as us
    real_ctx = us.create_urllib3_context
    calls = []

    def counting(*a, **k):
        calls.append(1)
        return real_ctx(*a, **k)
    uc.create_urllib3_context = us.create_urllib3_context = counting
    res = {True: [], False: []}
    created = {}
    NET.reset_shared_ssl_context()                                          # 1 context dùng chung cho cả 20 lượt
    with MockGVSHosts(crt, key) as g:
        for rnd in range(20):
            for shared in (False, True) if rnd % 2 else (True, False):
                ws.set_net_mode("fast")
                ws.set_net_opts(dict(ws.NET_OPTS_OFF, net_shared_ssl=shared))
                with ws._POOL_LOCK:
                    if ws._POOL is not None:
                        ws._POOL.clear()
                    ws._POOL = None
                c0 = len(calls)                                             # gồm cả context tạo lúc dựng pool
                p = ws.pool()
                p.connection_pool_kw.update(ca_certs=crt, cert_reqs="CERT_REQUIRED")
                cp = p.connection_from_host(f"rr{rnd + 1}---sn-aaa.googlevideo.com", g.port, scheme="https")
                conn = cp._get_conn()
                t0 = time.perf_counter()
                conn.connect()
                res[shared].append((time.perf_counter() - t0) * 1000)
                conn.close()
                created[shared] = created.get(shared, 0) + len(calls) - c0
    out["item1_new_tls_conn_ms"] = {("shared" if k else "per_conn"): {"median": med(v), "p90": p90(v), "n": len(v)} for k, v in res.items()}
    uc.create_urllib3_context = us.create_urllib3_context = real_ctx
    out["item1_ssl_contexts_created_per_20_conns"] = {("shared" if k else "per_conn"): v for k, v in created.items()}
    out["item1_note"] = "per_conn: urllib3 tạo SSLContext + load_default_certs cho mỗi kết nối; shared: 1 context (SSL_STATS đếm context dùng chung)"
    undo()
    # ---- #7: kiểm nhanh vs ffprobe
    mp4 = work / "a.mp4"
    subprocess.run([shutil.which("ffmpeg"), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25", "-f", "lavfi",
                    "-i", "sine=duration=60", "-t", "60", "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "1000k", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(mp4)], check=True)
    qc, fp = [], []
    for _ in range(20):
        t0 = time.perf_counter()
        ok, _why = ws.mp4_quick_check(mp4)
        qc.append((time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter()
        ws.ffprobe_info(mp4)
        fp.append((time.perf_counter() - t0) * 1000)
    out["item7"] = {"file_mb": round(mp4.stat().st_size / 2 ** 20, 2), "quick_check_ms": {"median": med(qc), "p90": p90(qc)},
                    "ffprobe_ms": {"median": med(fp), "p90": p90(fp)},
                    "note": "kiểm nhanh chạy TRƯỚC ffprobe (không thay ffprobe): file hỏng bị loại sớm hơn ~ffprobe_ms"}
    # ---- #8: đường nóng
    data = mp4.read_bytes()
    data = (data * (1 + (9_000_000 // len(data))))[:9_090_000]                 # ≈ 8,67 MB như số đo của người dùng
    res8 = {True: [], False: []}
    with MockGVS() as gv:
        url = gv.add("/hot", data, "ok")
        for rnd in range(30):
            for on in (False, True) if rnd % 2 else (True, False):
                ws.set_net_mode("fast")
                ws.set_net_opts(dict(ws.NET_OPTS_OFF, dl_hotpath_v2=on))
                dest = work / f"h{on}.bin"
                info = {}
                t0 = time.perf_counter()
                ws.download_single(url, dest, info)
                res8[on].append((time.perf_counter() - t0) * 1000)
                dest.unlink()
    out["item8_download_8_67MB_ms"] = {("v2_1MiB_unbuffered" if k else "old_256KB_buffered"): {"median": med(v), "p90": p90(v), "n": len(v)}
                                       for k, v in res8.items()}
    ws.set_net_opts(ws.NET_OPTS_OFF)
    (ROOT / "BENCH_DL_V2_MICRO.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1))
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
