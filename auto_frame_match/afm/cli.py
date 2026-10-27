"""Dòng lệnh (dùng cho tự động hóa / kiểm thử). GUI: ``python -m afm.gui``."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import TOOL_ROOT, load_config
from .log import setup_logging


def _kv(items: list[str] | None) -> dict[str, str]:
    out = {}
    for it in items or []:
        if "=" not in it:
            raise SystemExit(f"Sai định dạng '{it}', cần ID=giá_trị (vd S1=D:/video2x/tap1.mp4)")
        k, v = it.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _sources(args) -> list[dict]:
    v2x = _kv(args.video2x)
    off = _kv(args.audio_offset)
    out = []
    for k, p in enumerate(args.source, 1):
        sid = f"S{k}"
        out.append({"id": sid, "path": p, "render_path": v2x.get(sid),
                    "audio_offset_ms": float(off.get(sid, 0))})
    return out


def _progress(step, name, frac, msg):
    sys.stderr.write(f"\r[{name}] {frac * 100:5.1f}% {msg[:60]:<60}")
    sys.stderr.flush()
    if frac >= 1.0:
        sys.stderr.write("\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="afm", description="AUTO FRAME MATCH 2K - phân tích & cắt ghép cục bộ")
    ap.add_argument("--config", help="file afm_config.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("phan-tich", help="Chế độ A: phân tích & cắt tự động")
    a.add_argument("--edit", required=True, help="video Douyin đã edit")
    a.add_argument("--source", action="append", required=True, help="video gốc (lặp lại cho nhiều nguồn)")
    a.add_argument("--video2x", action="append", help="S1=đường_dẫn bản Video2X dùng để dựng")
    a.add_argument("--audio-offset", action="append", help="S1=ms độ trễ audio riêng của nguồn")
    a.add_argument("--out", required=True)
    a.add_argument("--draft", action="store_true", help="cho phép xuất bản nháp khi chưa xác minh 100%%")
    a.add_argument("--no-render", action="store_true", help="chỉ phân tích, không dựng")
    a.add_argument("--crop", choices=["match_edit", "full_source"])
    a.add_argument("--codec", choices=["auto", "h264_nvenc", "libx264"])
    b = sub.add_parser("cat-theo-bang", help="Chế độ B: cắt theo bảng có sẵn")
    b.add_argument("--table", required=True, help="BANG_CAT_AUTO.csv hoặc .json")
    b.add_argument("--source", action="append", required=True)
    b.add_argument("--video2x", action="append")
    b.add_argument("--audio-offset", action="append")
    b.add_argument("--out", required=True)
    b.add_argument("--draft", action="store_true")
    b.add_argument("--crop", choices=["match_edit", "full_source"])
    b.add_argument("--codec", choices=["auto", "h264_nvenc", "libx264"])
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    if getattr(args, "draft", False):
        cfg.render.allow_unverified = True
    if getattr(args, "crop", None):
        cfg.render.crop_mode = args.crop
    if getattr(args, "codec", None):
        cfg.render.codec = args.codec
    logf = setup_logging(TOOL_ROOT / "logs", logging.INFO)
    print(f"Log: {logf}")
    from .pipeline import run_mode_a, run_mode_b

    try:
        if args.cmd == "phan-tich":
            res = run_mode_a(args.edit, _sources(args), args.out, cfg, _progress, do_render=not args.no_render)
            print(json.dumps(res["summary"], ensure_ascii=False, indent=1))
        else:
            res = run_mode_b(args.table, _sources(args), args.out, cfg, _progress)
        print("ĐÃ XÁC MINH" if res["verified"] else "CHƯA XÁC MINH 100% - xem QA_REPORT.html")
        print("Báo cáo:", res["report"])
        return 0 if res["verified"] else 2
    except Exception as e:  # pragma: no cover
        logging.getLogger("afm").exception("Lỗi: %s", e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
