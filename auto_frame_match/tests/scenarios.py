"""Các kịch bản kiểm thử với đáp án đã biết + hàm chấm điểm bảng cắt."""

from __future__ import annotations

import json
from pathlib import Path

from synth import CartoonSource, EditSeg, SourceSpec, build_edit


def make_sources(root: Path, which=("S1", "S2", "S3"), upscale_s2=False, dup=False, textured=False):
    root.mkdir(parents=True, exist_ok=True)
    specs = {
        "S1": SourceSpec("S1", 11, 40.0, textured=textured),
        "S2": SourceSpec("S2", 22, 36.0, width=1280 if upscale_s2 else 640, height=720 if upscale_s2 else 360),
        "S3": SourceSpec("S3", 33, 30.0, fps_num=25, fps_den=1),
    }
    srcs = {}
    for sid in which:
        copy = None
        if dup and sid == "S3":
            # S3 khung 100..199 là bản sao S1 khung 300..399 (cảnh trùng giữa 2 nguồn)
            copy = (srcs["S1"], 300, 100, 100)
        srcs[sid] = CartoonSource(specs[sid], copy_from=copy)
    paths = {}
    for sid, cs in srcs.items():
        p = root / f"{sid}.mp4"
        if not p.exists():
            cs.render(p)
        paths[sid] = p
    return srcs, paths


SCENARIOS = {
    # 1) một nguồn, cùng fps, không biến đổi
    "mot_nguon": dict(
        sources=("S1",), fps=24000 / 1001, size=(960, 540), layout="fill",
        segs=[EditSeg("S1", 30, 2.5), EditSeg("S1", 400, 3.0), EditSeg("S1", 150, 2.0), EditSeg("S1", 700, 2.2)]),
    # 2) hai nguồn xen kẽ, fps khác (30), phụ đề, nguồn S2 đã upscale
    "hai_nguon_fps_phude": dict(
        sources=("S1", "S2"), fps=30.0, size=(960, 540), layout="fill", upscale_s2=True,
        segs=[EditSeg("S1", 60, 2.0, subtitle="Shin-chan: Oi oi!"), EditSeg("S2", 200, 2.4),
              EditSeg("S1", 500, 1.8, subtitle="Mama dang gian kia"), EditSeg("S2", 600, 2.6, subtitle="Hahaha"),
              EditSeg("S1", 820, 1.5)]),
    # 3) ba nguồn xen kẽ + crop/zoom + lật + màu + lặp cảnh + cảnh ngoài + watermark + tốc độ
    "ba_nguon_bien_doi": dict(
        sources=("S1", "S2", "S3"), fps=30.0, size=(960, 540), layout="fill", watermark=True, dup=True,
        segs=[EditSeg("S1", 100, 2.0, crop=(0.1, 0.1, 0.8, 0.8)),
              EditSeg("S2", 300, 2.0, flip=True),
              EditSeg("S3", 400, 2.2, hue=25, sat=1.3, bright=1.05),
              EditSeg(None, seconds=2.0, foreign_seed=777),
              EditSeg("S1", 100, 2.0, crop=(0.1, 0.1, 0.8, 0.8)),  # cảnh lặp
              EditSeg("S2", 500, 2.0, crop=(0.2, 0.05, 0.6, 0.6), subtitle="Zoom vao!"),
              EditSeg("S1", 300, 2.0),  # cảnh này cũng có trong S3 (bản sao)
              EditSeg("S3", 600, 2.0, speed=1.25),
              EditSeg("S1", 600, 2.0, contrast=1.3, bright=0.9)]),
    # 4) Douyin dọc 9:16: nội dung 16:9 giữa nền mờ, chữ tiêu đề, phụ đề
    "doc_letterbox": dict(
        sources=("S1", "S2"), fps=30.0, size=(720, 1280), layout="letterbox",
        segs=[EditSeg("S2", 50, 2.0, subtitle="Tap 1"), EditSeg("S1", 250, 2.5, flip=True),
              EditSeg("S2", 420, 2.0), EditSeg("S1", 650, 2.0, subtitle="Het")]),
    # 5) Douyin dọc 9:16 crop lấp đầy (lấy cột giữa của khung 16:9)
    "doc_crop": dict(
        sources=("S1",), fps=30.0, size=(540, 960), layout="fill",
        segs=[EditSeg("S1", 80, 2.0, crop=(0.33, 0.0, 0.316, 1.0)),
              EditSeg("S1", 520, 2.0, crop=(0.2, 0.0, 0.316, 1.0), subtitle="Wow")]),
    # 6) cảnh lặp nhiều lần + cảnh không có nguồn ở đầu và cuối + edit 25 fps
    "lap_va_ngoai_nguon": dict(
        sources=("S1", "S2"), fps=25.0, size=(960, 540), layout="fill",
        segs=[EditSeg(None, seconds=1.5, foreign_seed=4242), EditSeg("S2", 120, 2.0),
              EditSeg("S1", 333, 1.6), EditSeg("S2", 120, 2.0), EditSeg("S1", 333, 1.6, flip=True),
              EditSeg(None, seconds=1.2, foreign_seed=99)]),
    # 7) (v1.1) phóng to rất mạnh + phụ đề cứng + watermark; cảnh ngoài nguồn kẹp giữa 2 đoạn cùng nguồn
    "zoom_manh_noi_suy": dict(
        sources=("S1",), fps=30.0, size=(960, 540), layout="fill", watermark=True, textured=True,
        segs=[EditSeg("S1", 100, 2.0, subtitle="Shin-chan: Oi!"),
              EditSeg("S1", 300, 2.5, crop=(0.40, 0.28, 0.36, 0.36), subtitle="Zoom manh qua"),
              EditSeg("S1", 400, 2.0),
              EditSeg(None, seconds=1.5, foreign_seed=555),
              EditSeg("S1", 520, 2.0, subtitle="Het")]),
}


def build_scenario(name: str, root: Path, per_scenario_sources: bool = False):
    sc = SCENARIOS[name]
    srcs, paths = make_sources(root / f"sources_{name}" if per_scenario_sources else root / "sources",
                               sc["sources"], sc.get("upscale_s2", False), sc.get("dup", False),
                               sc.get("textured", False))
    edit = root / f"edit_{name}.mp4"
    truth_path = edit.with_suffix(".truth.json")
    if not edit.exists() or not truth_path.exists():
        build_edit(edit, srcs, sc["segs"], fps=sc["fps"], size=sc["size"], layout=sc["layout"],
                   watermark=sc.get("watermark", False))
    truth = json.loads(truth_path.read_text())
    return srcs, paths, edit, truth


def score_table(per_frame: list[dict], truth: dict, srcs: dict) -> dict:
    """So bảng cắt từng khung với đáp án.

    exact: đúng nguồn + đúng chỉ số khung; visual: đúng nguồn + khung trông giống hệt (giữ khung).
    false_match: khung KHÔNG có nguồn nhưng tool vẫn báo khớp (XANH/VÀNG có source_id).
    """
    gt = truth["frames"]
    assert len(per_frame) == len(gt), (len(per_frame), len(gt))
    stats = dict(total=0, exact=0, visual=0, wrong=0, missed=0, foreign=0, false_match=0, green_wrong=0,
                 off_by_one=0)
    for row, g in zip(per_frame, gt):
        stats["total"] += 1
        if g["source_id"] is None:
            stats["foreign"] += 1
            if row.get("source_id") and row.get("status") != "DO":
                stats["false_match"] += 1
            continue
        if not row.get("source_id"):
            stats["missed"] += 1
            continue
        sid, f = row["source_id"], int(row["source_frame"])
        same_draw = sid in srcs and f < srcs[sid].spec.n and \
            list(map(str, srcs[sid].drawing_id(f))) == g["drawing"]
        same_draw = same_draw or (sid != g["source_id"] and sid in srcs and f < srcs[sid].spec.n
                                  and list(map(str, srcs[sid].drawing_id(f))) == g["drawing"])
        if sid == g["source_id"] and f == g["source_frame"]:
            stats["exact"] += 1
            stats["visual"] += 1
        elif same_draw:
            stats["visual"] += 1
            if sid == g["source_id"] and abs(f - g["source_frame"]) == 1:
                stats["off_by_one"] += 1
        else:
            stats["wrong"] += 1
            stats.setdefault("wrong_list", []).append((row["edit_frame"], sid, f, g["source_id"], g["source_frame"], row.get("status"), row.get("similarity")))
            if row.get("status") == "XANH":
                stats["green_wrong"] += 1
    return stats
