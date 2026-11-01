"""V2: dòng thời gian chính xác - MỖI khung edit có đúng MỘT khung xuất.

Mỗi khung có nguồn gốc (origin):
* SOURCE_RAW            - khung anime gốc (đã/khoảng chưa xác minh tùy status)
* SOURCE_INTERPOLATED   - ảnh trộn 2 khung gốc liền nhau (Douyin nội suy slow-motion)
* DOUYIN_CLEAN_FALLBACK - khung Douyin đã qua kiểm tra sạch (không chữ/logo/watermark)
* UNVERIFIED            - chưa xác minh: KHÔNG được xuất thành phẩm chính thức

Khung được nhóm thành "run" liên tiếp cùng cách dựng để renderer xử lý theo khối.
"""

from __future__ import annotations

import numpy as np

from .log import get_logger
from .matcher import GREEN, RED, YELLOW

log = get_logger()

SOURCE_RAW, SOURCE_INTERP = "SOURCE_RAW", "SOURCE_INTERPOLATED"
CLEAN_FB, UNVERIFIED = "DOUYIN_CLEAN_FALLBACK", "UNVERIFIED"


def frame_records(segs, clean_by_seg: dict, approve_clean: bool) -> list[dict]:
    """Bản ghi từng khung edit (đúng thứ tự, không thiếu, không trùng)."""
    recs = []
    for k, sg in enumerate(segs, 1):
        for j in range(sg.n):
            ei = sg.start + j
            fst = sg.frame_status[j] if sg.frame_status else sg.status
            r = {"edit_frame": ei, "segment_id": k, "status": fst, "method": sg.method if sg.sid else ""}
            guessed = sg.method in ("interp", "freeze")
            if sg.sid is not None and fst != RED:
                pair = -1 if sg.interp_pair is None else int(sg.interp_pair[j])
                r.update({"source_id": sg.sid, "source_frame": int(sg.frames[j]), "pair": pair,
                          "origin": UNVERIFIED if guessed else (SOURCE_INTERP if pair >= 0 else SOURCE_RAW),
                          "verified": fst == GREEN and not guessed})
            else:
                cc = clean_by_seg.get((k, "seg" if sg.sid is None else "red"))
                verdict = cc.verdict if cc is not None else None
                ok = approve_clean and verdict == "SACH"
                r.update({"source_id": None, "source_frame": None, "pair": -1,
                          "origin": CLEAN_FB if ok else UNVERIFIED, "verified": bool(ok),
                          "clean_check": verdict or "", "status": GREEN if ok else (RED if fst == RED else YELLOW)})
            if sg.local_speed is not None:
                r["speed"] = float(sg.local_speed[j])
            elif sg.sid is not None:
                r["speed"] = float(sg.speed)
            recs.append(r)
    for i, r in enumerate(recs):  # bất biến: chỉ số khung liên tục 0..n-1
        if r["edit_frame"] != i:
            raise AssertionError(f"Dòng thời gian bị hở/trùng tại khung {i} (gặp {r['edit_frame']})")
    return recs


def seg_params(segs) -> dict[int, dict]:
    """Tham số dựng theo đoạn (từ kết quả phân tích)."""
    out = {}
    for k, sg in enumerate(segs, 1):
        if sg.sid is None:
            continue
        out[k] = {"flip": bool(sg.geom.flip), "crop_norm": sg.geom.source_crop(), "geometry": sg.geom,
                  "a": float(sg.a), "speed": float(sg.speed), "t_ref": float(sg.t_ref),
                  "linear": sg.frame_map is None and sg.method != "freeze", "method": sg.method}
    return out


def build_runs(recs: list[dict], params: dict[int, dict]) -> list[dict]:
    """Gom khung liên tiếp cùng cách dựng thành run (để renderer xử lý theo khối)."""
    runs = []
    for r in recs:
        kind = "source" if r["source_id"] else "edit"
        key = (kind, r["segment_id"], r["source_id"], r["origin"] == UNVERIFIED and kind == "source")
        if runs and runs[-1]["_key"] == key and kind == "edit":
            runs[-1]["edit_f1"] += 1
            runs[-1]["verified"] &= r["verified"]
            continue
        if runs and runs[-1]["_key"] == key and kind == "source" and \
                r["source_frame"] >= runs[-1]["frames"][-1]:  # nguồn đi tới (không lùi) -> cùng run
            ru = runs[-1]
            ru["edit_f1"] += 1
            ru["frames"].append(r["source_frame"])
            ru["pairs"].append(r["pair"])
            ru["speeds"].append(r.get("speed", 1.0))
            ru["verified"] &= r["verified"]
            ru["origins"].add(r["origin"])
            continue
        prm = params.get(r["segment_id"], {})
        ru = {"_key": key, "kind": kind, "segment_id": r["segment_id"], "edit_f0": r["edit_frame"],
              "edit_f1": r["edit_frame"] + 1, "verified": r["verified"], "origins": {r["origin"]},
              "source_id": r["source_id"]}
        if kind == "source":
            ru.update({"frames": [r["source_frame"]], "pairs": [r["pair"]], "speeds": [r.get("speed", 1.0)],
                       **prm})
        runs.append(ru)
    for k, ru in enumerate(runs, 1):
        ru["run_id"] = k
        ru.pop("_key", None)
        ru["origins"] = sorted(ru["origins"])
        if ru["kind"] == "source":
            ru["frames"] = np.asarray(ru["frames"], np.int64)
            ru["pairs"] = np.asarray(ru["pairs"], np.int64)
            ru["speeds"] = np.asarray(ru["speeds"], np.float64)
    return runs


def check_invariant(runs: list[dict], n_edit: int):
    pos = 0
    for ru in runs:
        if ru["edit_f0"] != pos:
            raise AssertionError(f"Run {ru['run_id']} bắt đầu ở {ru['edit_f0']}, mong đợi {pos}")
        pos = ru["edit_f1"]
    if pos != n_edit:
        raise AssertionError(f"Dòng thời gian phủ {pos} khung, bản edit có {n_edit} khung")


def summarize_origins(recs: list[dict]) -> dict:
    out = {"frames_total": len(recs)}
    for o in (SOURCE_RAW, SOURCE_INTERP, CLEAN_FB, UNVERIFIED):
        out[o] = sum(1 for r in recs if r["origin"] == o)
    out["verified"] = sum(1 for r in recs if r["verified"])
    out["source_verified"] = sum(1 for r in recs if r["verified"] and r["origin"] in (SOURCE_RAW, SOURCE_INTERP))
    out["interp_verified"] = sum(1 for r in recs if r["verified"] and r["origin"] == SOURCE_INTERP)
    out["all_verified"] = out["verified"] == len(recs) and len(recs) > 0
    return out
