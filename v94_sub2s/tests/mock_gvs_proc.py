# -*- coding: utf-8 -*-
"""MockGVS chạy ở TIẾN TRÌNH RIÊNG (để GIL/CPU của server không làm méo số đo của engine tải).

Dùng: python tests/mock_gvs_proc.py '<json spec>'   → in 1 dòng "PORT <n>", rồi đọc lệnh từ stdin:
  count <tiền tố path>   → in "COUNT <n>" (số request có path bắt đầu bằng tiền tố)
  conns                  → in "CONNS <n>" (số kết nối TCP server đã nhận)
  quit
spec: {"link_mbps": 400, "ttfb_ms": 0, "files": [{"path": "/v/real", "size": N, "seed": 1, "mode": "ok", "kw": {...}}, ...]}
  kw có thể chứa "to_path" (redirect tới path khác của chính server) — đổi thành URL đầy đủ khi đã biết cổng."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mock_gvs import MockGVS, make_payload  # noqa: E402


def main():
    spec = json.loads(sys.argv[1])
    link = int(float(spec.get("link_mbps") or 0) * 1e6 / 8) or 0
    g = MockGVS(link_bytes_per_sec=link, ttfb=float(spec.get("ttfb_ms") or 0) / 1000.0)
    g.start()
    cache = {}
    for f in spec["files"]:
        kw = dict(f.get("kw") or {})
        if "to_path" in kw:
            kw["to"] = g.url(kw.pop("to_path"))
        data = b""
        if f.get("size"):
            key = (int(f["size"]), int(f.get("seed", 1)))
            if key not in cache:
                cache[key] = make_payload(key[0], seed=key[1])
            data = cache[key]
        g.add(f["path"], data, f.get("mode", "ok"), **kw)
    print(f"PORT {g.port}", flush=True)
    for line in sys.stdin:
        parts = line.strip().split(" ", 1)
        if not parts or not parts[0]:
            continue
        if parts[0] == "count":
            pre = parts[1] if len(parts) > 1 else ""
            with g.log_lock:
                n = sum(1 for x in g.log if x[1].startswith(pre))
            print(f"COUNT {n}", flush=True)
        elif parts[0] == "reset":           # trước mỗi lượt đo: đếm lại request (cold_n) và range treo (stall_*)
            for sp in g.files.values():
                with sp.lock:
                    sp.requests = 0
                    sp.kw.pop("_stalled", None)
                    sp.kw.pop("_t_first", None)
            print("RESET 1", flush=True)
        elif parts[0] == "conns":
            print(f"CONNS {g.conns}", flush=True)
        elif parts[0] == "quit":
            break
    g.httpd.shutdown()


if __name__ == "__main__":
    main()
