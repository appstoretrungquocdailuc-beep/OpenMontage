"""Kiểm thử thực tế với video tổng hợp có ĐÁP ÁN đúng từng khung.

Chạy:  python -m pytest tests -v        (hoặc nhấp đúp KIEM_THU.bat)
Đặt AFM_TEST_DIR=<thư mục> để giữ lại video/kết quả giữa các lần chạy.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from afm.config import AppConfig  # noqa: E402
from afm.log import setup_logging  # noqa: E402
from afm.matcher import GREEN, RED, YELLOW  # noqa: E402
from afm.pipeline import analyze, run_mode_a, run_mode_b  # noqa: E402
from scenarios import build_scenario, score_table  # noqa: E402

setup_logging(None)


@pytest.fixture(scope="session")
def root(tmp_path_factory):
    d = os.environ.get("AFM_TEST_DIR")
    p = Path(d) if d else tmp_path_factory.mktemp("afm")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _cfg(root: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.cache_dir = str(root / "cache")
    cfg.render.codec = "auto"
    return cfg


_ANALYSIS: dict = {}


def run_analysis(root: Path, name: str):
    if name not in _ANALYSIS:
        srcs, paths, edit, truth = build_scenario(name, root, per_scenario_sources=True)
        out = root / f"out_{name}"
        shutil.rmtree(out / ".afm_job", ignore_errors=True)
        res = analyze(str(edit), [str(paths[s]) for s in sorted(paths)], out, _cfg(root))
        _ANALYSIS[name] = (srcs, paths, edit, truth, res)
    return _ANALYSIS[name]


def _frames_by_gt(res, truth):
    return list(zip(res["doc"]["frames"], truth["frames"]))


# ---------------------------------------------------------------------------- nhận đúng frame & nguồn

@pytest.mark.parametrize("name", ["mot_nguon", "hai_nguon_fps_phude", "ba_nguon_bien_doi",
                                  "doc_letterbox", "doc_crop", "lap_va_ngoai_nguon"])
def test_bang_cat_dung_tung_khung(root, name):
    srcs, paths, edit, truth, res = run_analysis(root, name)
    st = score_table(res["doc"]["frames"], truth, srcs)
    print(name, json.dumps({k: v for k, v in st.items() if k != "wrong_list"}))
    matchable = st["total"] - st["foreign"]
    # 1) khung XANH thì tuyệt đối không được sai
    assert st["green_wrong"] == 0, st.get("wrong_list")
    assert st["missed"] == 0
    # 2) khung sai (nếu có) chỉ được nằm trong đoạn VÀNG đã được gắn cờ nghi vấn, và cực hiếm
    assert st["wrong"] <= max(1, int(0.005 * matchable)), st.get("wrong_list")
    # 3) mọi khung có nguồn đều tìm đúng (đúng chỉ số khung, hoặc khung giống hệt khi nguồn trùng nội dung)
    assert st["visual"] >= matchable - st["wrong"]
    # 4) KHÔNG báo khớp cho cảnh không thuộc nguồn nào
    assert st["false_match"] == 0
    for row, g in _frames_by_gt(res, truth):
        if g["source_id"] is None:
            assert row["status"] == RED and not row["source_id"]
        if row["status"] == GREEN:  # XANH thì phải đúng tuyệt đối
            assert row["source_id"] == g["source_id"] and row["source_frame"] == g["source_frame"]


def test_mot_nguon_xac_minh_100(root):
    srcs, paths, edit, truth, res = run_analysis(root, "mot_nguon")
    assert res["doc"]["summary"]["fully_verified"] is True
    assert all(r["status"] == GREEN for r in res["doc"]["segments"])


def test_crop_zoom_lat_toc_do(root):
    """Kiểm tra tham số crop / lật / tốc độ ghi trong bảng so với đáp án."""
    srcs, paths, edit, truth, res = run_analysis(root, "ba_nguon_bien_doi")
    segs = res["doc"]["segments"]
    gt_segs = truth["segments"]

    def seg_at(frame):
        return next(s for s in segs if s["edit_frame_start"] <= frame <= s["edit_frame_end"])

    # cảnh 1: crop (0.1, 0.1, 0.8, 0.8) của S1 640x360 -> 64,36 512x288
    s = seg_at(10)
    assert s["source_id"] == "S1" and abs(s["crop_x"] - 64) < 3 and abs(s["crop_w"] - 512) < 4
    assert s["flip"] == 0
    # cảnh 2: lật gương
    s = seg_at(80)
    assert s["source_id"] == "S2" and s["flip"] == 1
    # cảnh tốc độ 1.25 của S3
    s = seg_at(440)
    assert s["source_id"] == "S3" and abs(float(s["speed"]) - 1.25) < 0.02
    # cảnh không thuộc nguồn nào -> UNMATCHED
    s = seg_at(200)
    assert s["source_id"] == "UNMATCHED" and s["status"] == RED
    # zoom 0.6 lệch tâm + phụ đề: crop (0.2, 0.05, 0.6, 0.6) theo kích thước thật của S2
    s = seg_at(330)
    w2 = res["manifest"]["sources"]["S2"]["width"]
    assert s["source_id"] == "S2" and abs(s["crop_x"] - 0.2 * w2) < 0.01 * w2 and abs(s["crop_w"] - 0.6 * w2) < 0.012 * w2
    assert gt_segs[5]["crop"] == [0.2, 0.05, 0.6, 0.6]


def test_canh_trung_nhieu_nguon_khong_doan(root):
    """S3 chứa bản sao cảnh S1: tool phải đối chiếu tính liên tục / hiển thị ứng viên, không đoán bừa."""
    srcs, paths, edit, truth, res = run_analysis(root, "ba_nguon_bien_doi")
    seg = next(s for s in res["doc"]["segments"] if s["edit_frame_start"] <= 380 <= s["edit_frame_end"])
    note = seg["reasons"] + seg["candidates"]
    assert seg["status"] == YELLOW or "LIÊN TỤC" in note or "S3" in note


def test_bang_va_manifest_day_du(root):
    srcs, paths, edit, truth, res = run_analysis(root, "hai_nguon_fps_phude")
    out = Path(res["out_dir"])
    for f in ("BANG_CAT_AUTO.csv", "BANG_CAT_AUTO.json", "SOURCE_MANIFEST.json", "BANG_CAT_TUNG_FRAME.csv",
              "KIEM_TRA_THU_CONG.csv"):
        assert (out / f).exists(), f
    man = json.loads((out / "SOURCE_MANIFEST.json").read_text("utf-8"))
    for sid, m in man["sources"].items():
        for k in ("fps", "fps_fraction", "time_base", "start_time", "content_hash", "audio_offset_ms",
                  "phash_samples", "audio_stream"):
            assert k in m, (sid, k)
    doc = json.loads((out / "BANG_CAT_AUTO.json").read_text("utf-8"))
    fr = doc["frames"][0]
    for k in ("edit_frame", "edit_pts", "source_id", "source_frame", "source_pts", "similarity", "crop", "flip",
              "speed"):
        assert k in fr
    assert len(doc["frames"]) == len(truth["frames"])


# ---------------------------------------------------------------------------- dựng + QA

def test_dung_2k_xac_minh_va_qa(root):
    srcs, paths, edit, truth = build_scenario("hai_nguon_fps_phude", root, per_scenario_sources=True)
    out = root / "full_hai"
    res = run_mode_a(str(edit), [str(paths[s]) for s in sorted(paths)], out, _cfg(root))
    assert res["verified"] is True
    qa = res["qa"]
    assert qa["status"] == GREEN and not qa["global_issues"]
    assert qa["height"] == 1440 and qa["width"] == 2560
    assert qa["frames"] == qa["expected_frames"]
    assert all(abs(a["lag_ms"]) < 5 and a["corr"] > 0.9 for a in qa["audio"])
    assert (out / "THANH_PHAM_2K.mp4").exists() and (out / "QA_REPORT.html").exists()
    # tiếng Nhật lấy từ nguồn (nhãn ngôn ngữ jpn), không lấy tiếng Douyin (sine 330Hz)
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                        "stream_tags=language", "-of", "csv=p=0", str(out / "THANH_PHAM_2K.mp4")],
                       capture_output=True, text=True)
    assert "jpn" in p.stdout


def test_ban_nhap_khong_dung_doan_do(root):
    """Còn cảnh ĐỎ: không xuất bản 'đã xác minh'; bản nháp bỏ đoạn ĐỎ (không thay bằng màn trắng)."""
    srcs, paths, edit, truth = build_scenario("lap_va_ngoai_nguon", root, per_scenario_sources=True)
    out = root / "full_lap"
    cfg = _cfg(root)
    res = run_mode_a(str(edit), [str(paths[s]) for s in sorted(paths)], out, cfg)
    assert res["verified"] is False and res["render"] is None
    assert not (out / "THANH_PHAM_2K.mp4").exists()
    cfg.render.allow_unverified = True
    res = run_mode_a(str(edit), [str(paths[s]) for s in sorted(paths)], out, cfg)
    assert res["verified"] is False
    assert (out / "THANH_PHAM_2K_CHUA_XAC_MINH.mp4").exists()
    assert res["qa"]["status"] == GREEN  # các đoạn đã dựng khớp bản Douyin
    n_red = sum(1 for f in truth["frames"] if f["source_id"] is None)
    assert res["qa"]["frames"] < len(truth["frames"]) - n_red + 5


def test_qa_bat_loi_cat_sai(root):
    """Cố ý làm sai bảng (lệch khung, lật sai) -> QA phải báo ĐỎ."""
    from afm.qa import verify_output
    from afm.render import RenderPlan, prepare_sources, render
    from afm.pipeline import _plan_segments_from_analysis, _sources_for_render

    srcs, paths, edit, truth, res = run_analysis(root, "mot_nguon")
    segs, _ = _plan_segments_from_analysis(res["segments"], res["edit_fp"], False)
    segs[1]["a"] += 40  # nhầm cảnh (lệch 40 khung)
    segs[2]["flip"] = not segs[2]["flip"]  # sai lật gương
    sources = _sources_for_render(res["manifest"], res["src_fps"])
    prepare_sources(sources)
    plan = RenderPlan(segs, sources, dict(res["doc"]["edit"]), _cfg(root).render)
    out = root / "qa_sai.mp4"
    rinfo = render(plan, out, root / "qa_sai_work")
    qa = verify_output(str(out), plan, rinfo, _cfg(root).analysis, res["edit_fp"], res["src_fps"])
    st = {s["segment_id"]: s for s in qa["segments"]}
    assert st[1]["status"] == GREEN and st[4]["status"] == GREEN
    assert st[2]["status"] == RED
    assert st[3]["status"] == RED
    assert qa["status"] == RED


def test_che_do_b_va_xac_minh_nguon(root):
    srcs, paths, edit, truth = build_scenario("hai_nguon_fps_phude", root, per_scenario_sources=True)
    tbl = root / "full_hai" / "BANG_CAT_AUTO.csv"
    if not tbl.exists():
        run_mode_a(str(edit), [str(paths[s]) for s in sorted(paths)], root / "full_hai", _cfg(root))
    ren = root / "ren"
    ren.mkdir(exist_ok=True)
    shutil.copy(paths["S2"], ren / "tap_hai.mp4")
    shutil.copy(paths["S1"], ren / "tap_mot.mp4")
    # đổi tên + đảo thứ tự: tool tự nhận source_id theo vân tay
    res = run_mode_b(str(tbl), [str(ren / "tap_hai.mp4"), str(ren / "tap_mot.mp4")], root / "modeb", _cfg(root))
    assert res["verified"] is True and res["qa"]["status"] == GREEN
    # nguồn sai -> từ chối
    other = build_scenario("mot_nguon", root, per_scenario_sources=True)[1]
    bad = root / "bad"
    bad.mkdir(exist_ok=True)
    # một video khác nội dung nhưng cùng định dạng
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(paths["S1"]), "-vf", "hflip,negate",
                    "-c:a", "copy", str(bad / "khac.mp4")], check=True)
    with pytest.raises(RuntimeError):
        run_mode_b(str(tbl), [str(paths["S1"]), str(bad / "khac.mp4")], root / "modeb_bad", _cfg(root))
    del other


def test_video2x_ban_lam_net(root):
    """Dựng từ bản 'Video2X' (upscale 2x) thay vì bản gốc, cùng số khung."""
    srcs, paths, edit, truth, res = run_analysis(root, "mot_nguon")
    v2x = root / "S1_video2x.mp4"
    if not v2x.exists():
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(paths["S1"]), "-vf",
                        "scale=1280:720:flags=lanczos,unsharp=5:5:0.8", "-c:v", "libx264", "-crf", "14", "-an",
                        str(v2x)], check=True)
    out = root / "full_v2x"
    r = run_mode_a(str(edit), [{"path": str(paths["S1"]), "render_path": str(v2x)}], out, _cfg(root))
    assert r["verified"] is True and r["qa"]["status"] == GREEN
    assert all(abs(a["lag_ms"]) < 5 for a in r["qa"]["audio"])


def test_tiep_tuc_sau_gian_doan(root):
    """Lần chạy thứ hai dùng cache fingerprint + checkpoint (không quét lại)."""
    import time

    srcs, paths, edit, truth = build_scenario("mot_nguon", root, per_scenario_sources=True)
    out = root / "out_mot_nguon"
    t = time.time()
    res = analyze(str(edit), [str(paths[s]) for s in sorted(paths)], out, _cfg(root))
    assert time.time() - t < 15
    assert res["doc"]["summary"]["fully_verified"]


def test_do_tre_audio_rieng_tung_nguon(root):
    """Độ trễ audio của nguồn được áp dụng đúng (QA đối chiếu với nguồn tại source_pts + offset)."""
    srcs, paths, edit, truth = build_scenario("mot_nguon", root, per_scenario_sources=True)
    out = root / "full_offset"
    r = run_mode_a(str(edit), [{"path": str(paths["S1"]), "audio_offset_ms": 120.0}], out, _cfg(root))
    assert r["verified"]
    for seg in r["render"]["segments"]:
        assert seg["audio_start"] is not None
    man = json.loads((out / "SOURCE_MANIFEST.json").read_text("utf-8"))
    assert man["sources"]["S1"]["audio_offset_ms"] == 120.0
    # âm thanh xuất so với âm thanh gốc KHÔNG dời -> phải lệch đúng ~120 ms
    from afm.media import extract_audio_pcm
    from afm.qa import _xcorr_lag

    seg = r["render"]["segments"][1]
    o = extract_audio_pcm(str(out / "THANH_PHAM_2K.mp4"), 1, 0.0, 30.0)
    sr = 16000
    k0 = int(r["render"]["segments"][0]["frames"] / (24000 / 1001) * sr)
    ref = extract_audio_pcm(str(paths["S1"]), 1, seg["audio_start"] - 0.120 + 0.5, 1.0)
    lag, peak = _xcorr_lag(o[k0 + int(0.5 * sr): k0 + int(1.5 * sr)], ref, sr)
    assert abs(abs(lag) - 120) < 3, (lag, peak)
