# -*- coding: utf-8 -*-
"""V9.9 (bản ỨNG VIÊN V9_9_CANDIDATE.py): tail-rescue, đo từng range, chọn format 480p, học theo codec.
Chạy:  set V94_TEST_MODULE=V9_9_CANDIDATE  &&  python -m pytest tests -q        (bản chính V9.8.1: bỏ qua file này)"""
import json
import time

import pytest

from mock_gvs import MockGVS, make_payload, sha256

MB = 1024 * 1024


@pytest.fixture()
def v99(v94, monkeypatch, tmp_path):
    if not hasattr(v94, "V99_TAIL_RESCUE"):
        pytest.skip("chỉ áp dụng cho bản ứng viên V9.9 (V94_TEST_MODULE=V9_9_CANDIDATE)")
    monkeypatch.setattr(v94, "BASE_DIR", str(tmp_path))                  # v99_format_stats.json ghi vào thư mục tạm
    monkeypatch.setattr(v94, "_V99_FSTATS", {"loaded": False, "codecs": {}})
    return v94


def _dl(mod, url, tmp_path, name, expected, kind="video", info=None):
    info = {} if info is None else info
    ok = mod._download_http_range_turbo(url, tmp_path, name, lambda m: None, 64 * 1024, expected, media_kind=kind, info=info)
    return ok, info


# ----------------------------------------------------------------------------- mặc định = hành vi V9.8.1
def test_v99_defaults_keep_v981_behaviour(v99):
    assert v99.V99_FORMAT_MODE == "fixed" and v99.V99_FORCE_VIDEO_ITAG == "" and v99.V99_FORCE_AUDIO_ITAG == ""
    assert "av01" not in v99.V99_ALLOW_VIDEO_CODECS                     # AV1 không bao giờ tự bật
    assert v99.V99_OUTPUT_H264 is False and int(v99.V99_AUDIO_WORKERS) == 0 and int(v99.V99_HEDGE_MAX) == -1
    assert v99.V98_RACE_MODE == "dual" and v99.V98_PREALLOC_ASYNC is True


# ----------------------------------------------------------------------------- tail-rescue
def _stall_case(v99, tmp_path, monkeypatch, rescue, name):
    """Giống trường hợp thật: AUDIO 9 MiB / 24 worker (đoạn 0,375 MiB, không chẻ được), đợt đầu TTFB ~1.0 s (lạnh),
    request sau 0.2 s; range thứ 5 treo 2.5 s."""
    monkeypatch.setattr(v99, "V99_TAIL_RESCUE", rescue)
    monkeypatch.setattr(v99, "V99_WARM_TTFB", rescue)                   # off = đúng luật V9.8.1 (cả 2 cơ chế mới tắt)
    monkeypatch.setattr(v99, "V98_WORKERS_LEARN", False)
    monkeypatch.setattr(v99, "V94_RANGE_WORKERS_AUDIO", 24)
    data = make_payload(9 * MB, seed=91)
    part = (len(data) + 23) // 24
    with MockGVS() as g:
        url = g.add("/st", data, hdr_delay=0.2, cold_n=24, cold_delay=0.8, stall_start=part * 5, stall_delay=2.5, stall_count=1)
        ok, info = _dl(v99, url, tmp_path, name, len(data), "audio")
        time.sleep(0.2)
        n = len(g.requests_for("/st"))
    assert ok, info
    assert sha256((tmp_path / name).read_bytes()) == sha256(data)
    return info, n


def test_tail_rescue_bounds_a_stalled_range(v99, tmp_path, monkeypatch):
    off, n_off = _stall_case(v99, tmp_path, monkeypatch, False, "off.bin")
    on, n_on = _stall_case(v99, tmp_path, monkeypatch, True, "on.bin")
    # V9.8.1: chỉ thoát nhờ read-timeout 1.5 s của audio rồi retry; ứng viên: cứu ngay khi 23 range kia xong
    assert off["rescues"] == 0 and off["req_errors"].get("error:ReadTimeoutError", 0) >= 1
    assert on["rescues"] + on["takeovers"] >= 1 and not on["req_errors"]   # cứu sớm (rescue hoặc giành lại theo TTFB ấm), không chờ timeout
    assert on["elapsed_ms"] < off["elapsed_ms"] - 150, (on["elapsed_ms"], off["elapsed_ms"])
    assert on["tail_ms"] < off["tail_ms"]
    assert n_on <= n_off + int(v99.V99_TAIL_RESCUE_MAX)                 # request thêm có trần, không spam


def test_tail_rescue_request_cap(v99, tmp_path, monkeypatch):
    monkeypatch.setattr(v99, "V99_TAIL_RESCUE_MAX", 0)
    info, _ = _stall_case(v99, tmp_path, monkeypatch, True, "cap.bin")
    assert info["rescues"] == 0


def test_tail_rescue_no_extra_requests_when_healthy(v99, tmp_path, monkeypatch):
    monkeypatch.setattr(v99, "V98_WORKERS_LEARN", False)
    monkeypatch.setattr(v99, "V94_RANGE_WORKERS_VIDEO", 12)
    data = make_payload(16 * MB, seed=92)
    res = {}
    for flag in (False, True):
        monkeypatch.setattr(v99, "V99_TAIL_RESCUE", flag)
        with MockGVS() as g:
            ok, info = _dl(v99, g.add("/h", data, hdr_delay=0.1, rate=4 * MB), tmp_path, f"h{int(flag)}.bin", len(data))
            time.sleep(0.2)
            res[flag] = (info, len(g.requests_for("/h")))
        assert ok and sha256((tmp_path / f"h{int(flag)}.bin").read_bytes()) == sha256(data)
    assert res[True][0]["rescues"] == 0
    assert res[True][1] <= res[False][1] + 2


def test_tail_rescue_403_still_hard_fail(v99, tmp_path):
    with MockGVS() as g:
        ok, info = _dl(v99, g.add("/deny", mode="status", code=403), tmp_path, "d.bin", 8 * MB)
        assert not ok and info["fail_reason"] == "HTTP_403" and info["rescues"] == 0
        assert len(g.requests_for("/deny")) <= info["workers_initial"]
    assert not (tmp_path / "d.bin").exists()


def test_per_range_telemetry_fields(v99, tmp_path):
    data = make_payload(8 * MB, seed=93)
    with MockGVS() as g:
        ok, info = _dl(v99, g.add("/t", data, hdr_delay=0.05), tmp_path, "t.bin", len(data))
    assert ok
    lr = info["last_range"]
    assert lr["ttfb_ms"] is not None and lr["body_ms"] is not None and lr["bytes"] > 0
    assert info["ttfb_first_p50_ms"] >= 40 and info["ttfb_first_p90_ms"] >= info["ttfb_first_p50_ms"]
    assert info["t90_ms"] is not None and info["tail_ms"] is not None and info["tail_ms"] >= 0
    assert isinstance(info["req_errors"], dict)


def test_audio_workers_and_hedge_cap_flags(v99, tmp_path, monkeypatch):
    data = make_payload(9 * MB, seed=94)
    monkeypatch.setattr(v99, "V99_AUDIO_WORKERS", 8)
    with MockGVS() as g:
        ok, info = _dl(v99, g.add("/a", data), tmp_path, "a.bin", len(data), "audio")
    assert ok and info["workers_initial"] == 8
    assert sha256((tmp_path / "a.bin").read_bytes()) == sha256(data)
    monkeypatch.setattr(v99, "V99_AUDIO_WORKERS", 0)
    monkeypatch.setattr(v99, "V99_HEDGE_MAX", 0)                         # không hedge: host chính chậm vẫn phải xong, 0 hedge thật
    monkeypatch.setattr(v99, "V98_RACE_MODE", "fixed")
    monkeypatch.setattr(v99, "V94_FIRST_WAVE_RACE_MAX", 0)
    monkeypatch.setattr(v99, "V94_RACE_ALL_IF_WORKERS_LE", 0)
    monkeypatch.setattr(v99, "V94_RANGE_WORKERS_VIDEO", 12)
    big = make_payload(16 * MB, seed=95)
    with MockGVS() as g:
        real = g.add("/m/real", big, hdr_delay=0.5)
        main = g.add("/m/main", mode="redirect", to=real)
        alt = g.add("/m/alt", big, hdr_delay=0.05)
        monkeypatch.setattr(v99, "_gvs_alt_url", lambda u: alt if str(u) == main else "")
        v99._MN_RACE_BAD_UNTIL[0] = 0.0
        ok, info = _dl(v99, main, tmp_path, "m.bin", len(big))
        time.sleep(0.2)
        assert ok and len(g.requests_for("/m/alt")) == 0
    assert sha256((tmp_path / "m.bin").read_bytes()) == sha256(big)


# ----------------------------------------------------------------------------- chọn format
def _nf(itag, mime, clen, url=True, height=480, **kw):
    f = {"itag": itag, "mimeType": mime, "contentLength": str(clen), "height": height, "qualityLabel": f"{height}p", "fps": 24}
    if url:
        f["url"] = f"https://example.invalid/{itag}"
    f.update(kw)
    return f


NATIVE = [
    _nf(135, 'video/mp4; codecs="avc1.4d401e"', int(148.61 * MB)),
    _nf(244, 'video/webm; codecs="vp9"', int(102.10 * MB)),
    _nf(397, 'video/mp4; codecs="av01.0.04M.08"', int(78.35 * MB)),
    _nf(136, 'video/mp4; codecs="avc1.4d401f"', int(300 * MB), height=720),
    _nf(139, 'audio/mp4; codecs="mp4a.40.5"', int(9.09 * MB), height=None),
]


def test_native_candidates_only_480p_video(v99):
    c = v99._v99_native_video_cands(NATIVE)
    assert sorted((x["itag"], x["codec"]) for x in c) == [("135", "avc1"), ("244", "vp9"), ("397", "av01")]
    assert all(x["direct"] for x in c)
    hdr = _nf(398, 'video/mp4; codecs="av01.0.04M.10"', 1, colorInfo={"transferCharacteristics": "COLOR_TRANSFER_CHARACTERISTICS_SMPTEST2084"})
    assert v99._v99_native_video_cands([hdr]) == []


def test_fixed_mode_keeps_v981_choice(v99):
    pick, why = v99._v99_pick_video(v99._v99_native_video_cands(NATIVE))
    assert pick is None and why == "keep"                                # resolver giữ 244 → 135 như V9.8.1


def test_smart_default_never_picks_av1(v99, monkeypatch):
    monkeypatch.setattr(v99, "V99_FORMAT_MODE", "smart")
    pick, why = v99._v99_pick_video(v99._v99_native_video_cands(NATIVE))
    assert pick["itag"] == "244" and why.startswith("smart:")           # vp9 nhỏ hơn avc1; av01 không được phép


def test_smart_with_av1_allowed_picks_smallest_then_learns(v99, monkeypatch):
    monkeypatch.setattr(v99, "V99_FORMAT_MODE", "smart")
    monkeypatch.setattr(v99, "V99_ALLOW_VIDEO_CODECS", "av01,vp9,avc1")
    c = v99._v99_native_video_cands(NATIVE)
    assert v99._v99_pick_video(c)[0]["itag"] == "397"                    # chưa có số đo → file nhỏ nhất
    # AV1 đo được chậm hẳn (vd edge khác) → dự đoán lâu hơn dù file nhỏ hơn → quay về 244
    for _ in range(3):
        v99._v99_stats_note("av01", "397", True, 150.0, "", 80 * MB)
        v99._v99_stats_note("vp9", "244", True, 600.0, "", 100 * MB)
    assert v99._v99_pick_video(c)[0]["itag"] == "244"
    saved = json.load(open(v99._v99_stats_path(), encoding="utf-8"))
    assert saved["codecs"]["av01"]["n_ok"] == 3 and saved["codecs"]["vp9"]["ema_mbps"] == 600.0


def test_smart_avoids_codec_that_just_failed(v99, monkeypatch):
    monkeypatch.setattr(v99, "V99_FORMAT_MODE", "smart")
    monkeypatch.setattr(v99, "V99_ALLOW_VIDEO_CODECS", "av01,vp9,avc1")
    c = v99._v99_native_video_cands(NATIVE)
    v99._v99_stats_note("av01", "397", False, None, "HTTP_403", 80 * MB)
    assert v99._v99_pick_video(c)[0]["itag"] == "244"                    # vừa 403 → +3 s dự đoán
    monkeypatch.setattr(v99, "V99_FORMAT_FAIL_HOLD_SEC", 0.0)            # hết thời gian "giữ": thử lại được
    assert v99._v99_pick_video(c)[0]["itag"] == "397"


def test_small_files_do_not_train_speed(v99):
    v99._v99_stats_note("vp9", "244", True, 30.0, "", 2 * MB)            # file 2 MiB: Mbps bị TTFB chi phối
    assert v99._V99_FSTATS["codecs"]["vp9"]["ema_mbps"] is None and v99._V99_FSTATS["codecs"]["vp9"]["n_ok"] == 1


def test_output_h264_restricts_to_avc(v99, monkeypatch):
    monkeypatch.setattr(v99, "V99_FORMAT_MODE", "smart")
    monkeypatch.setattr(v99, "V99_ALLOW_VIDEO_CODECS", "av01,vp9,avc1")
    monkeypatch.setattr(v99, "V99_OUTPUT_H264", True)
    assert v99._v99_pick_video(v99._v99_native_video_cands(NATIVE))[0]["itag"] == "135"


def test_force_itag_exact_or_nothing(v99, monkeypatch):
    c = v99._v99_native_video_cands(NATIVE)
    monkeypatch.setattr(v99, "V99_FORCE_VIDEO_ITAG", "397")
    assert v99._v99_pick_video(c)[0]["itag"] == "397"
    no_url = [x for x in NATIVE if x["itag"] != 397] + [_nf(397, 'video/mp4; codecs="av01.0.04M.08"', 1, url=False)]
    pick, why = v99._v99_pick_video(v99._v99_native_video_cands(no_url))
    assert pick is None and why == "force_missing:397"                   # không âm thầm đổi sang format khác


def _yt(fid, vcodec, acodec, size, height=None, lang=None, ext="mp4"):
    return {"format_id": fid, "vcodec": vcodec, "acodec": acodec, "filesize": size, "height": height, "language": lang,
            "url": f"https://example.invalid/{fid}", "ext": ext, "format_note": ("Japanese original" if lang == "ja" else "")}


YT = {"id": "x", "title": "t", "duration": 1608.0, "formats": [
    _yt("135", "avc1.4d401e", "none", int(148.61 * MB), 480), _yt("244", "vp9", "none", int(102.10 * MB), 480, ext="webm"),
    _yt("397", "av01.0.04M.08", "none", int(78.35 * MB), 480),
    _yt("139", "none", "mp4a.40.5", int(9.09 * MB), lang="ja", ext="m4a"), _yt("249", "none", "opus", int(9.82 * MB), lang="ja", ext="webm"),
    _yt("140", "none", "mp4a.40.2", int(24.11 * MB), lang="ja", ext="m4a"), _yt("251", "none", "opus", int(25.60 * MB), lang="ja", ext="webm"),
    _yt("140-1", "none", "mp4a.40.2", int(24 * MB), lang="en", ext="m4a"),
]}


def test_ytdlp_bundle_default_force_and_smart(v99, monkeypatch):
    bd = v99._pick_ytdlp_480_bundle(YT, "YTDLP")
    assert bd["video_format_id"] == "244" and bd["audio_format_id"] == "139" and bd["video_codec"] == "vp9"
    assert [c["itag"] for c in bd["video_candidates"]] == ["135", "244", "397"]
    monkeypatch.setattr(v99, "V99_FORCE_VIDEO_ITAG", "397")
    monkeypatch.setattr(v99, "V99_FORCE_AUDIO_ITAG", "249")
    bd = v99._pick_ytdlp_480_bundle(YT, "YTDLP")
    assert bd["video_format_id"] == "397" and bd["audio_format_id"] == "249" and bd["audio_mime"] == "audio/webm"
    assert bd["video_mime"] == "video/mp4"
    monkeypatch.setattr(v99, "V99_FORCE_VIDEO_ITAG", "999")
    assert v99._pick_ytdlp_480_bundle(YT, "YTDLP") is None
    monkeypatch.setattr(v99, "V99_FORCE_VIDEO_ITAG", "")
    monkeypatch.setattr(v99, "V99_FORCE_AUDIO_ITAG", "")
    monkeypatch.setattr(v99, "V99_FORMAT_MODE", "smart")
    monkeypatch.setattr(v99, "V99_ALLOW_VIDEO_CODECS", "av01,vp9,avc1")
    assert v99._pick_ytdlp_480_bundle(YT, "YTDLP")["video_format_id"] == "397"


def test_force_audio_never_picks_non_japanese(v99, monkeypatch):
    """Ép audio itag 140: chỉ được lấy track 140 TIẾNG NHẬT, không lấy 140-1 (en)."""
    monkeypatch.setattr(v99, "V99_FORCE_AUDIO_ITAG", "140")
    bd = v99._pick_ytdlp_480_bundle(YT, "YTDLP")
    assert bd["audio_format_id"] == "140" and bd["audio_language"] == "ja"
    only_en = dict(YT, formats=[f for f in YT["formats"] if f.get("language") != "ja"])
    bd = v99._pick_ytdlp_480_bundle(only_en, "YTDLP")
    assert bd["audio_url"] is None and bd.get("ja_audio_missing") is True


# ----------------------------------------------------------------------------- đuôi chậm: TTFB ấm (log thật 18/11)
def _tail_case(v99, tmp_path, monkeypatch, warm, name):
    """12 đoạn × 2 MiB; đợt đầu LẠNH (TTFB ~0.85 s), request sau ẤM (0.05 s); mỗi kết nối 2 MiB/s, cứ 4 request có 1 cái chỉ
    0.8 MiB/s. Lúc các đoạn nhanh xong (~1.85 s) đoạn chậm còn ~1.2 MiB = ETA ~1.5 s: V9.8.1 (ngưỡng 2 × 0.85 + 0.1 = 1.8 s)
    KHÔNG chẻ → 1 kết nối tải một mình; TTFB ấm (ngưỡng ~0.95 s) chẻ ngay — đúng hiện tượng log thật 18/11."""
    monkeypatch.setattr(v99, "V99_WARM_TTFB", warm)
    monkeypatch.setattr(v99, "V99_TAIL_RESCUE", False)
    monkeypatch.setattr(v99, "V98_WORKERS_LEARN", False)
    monkeypatch.setattr(v99, "V98_RACE_MODE", "fixed")
    monkeypatch.setattr(v99, "V94_RANGE_WORKERS_VIDEO", 12)
    data = make_payload(24 * MB, seed=96)
    with MockGVS() as g:
        url = g.add("/tl", data, hdr_delay=0.05, rate=2 * MB, cold_n=12, cold_delay=0.8, slow_every=4, slow_rate=int(0.8 * MB))
        ok, info = _dl(v99, url, tmp_path, name, len(data))
        time.sleep(0.2)
        n = len(g.requests_for("/tl"))
    assert ok, info
    assert sha256((tmp_path / name).read_bytes()) == sha256(data)
    return info, n


def test_warm_ttfb_shortens_tail(v99, tmp_path, monkeypatch):
    old, n_old = _tail_case(v99, tmp_path, monkeypatch, False, "old.bin")
    new, n_new = _tail_case(v99, tmp_path, monkeypatch, True, "new.bin")
    assert new["splits"] > old["splits"]
    assert new["elapsed_ms"] < old["elapsed_ms"] - 250, (new["elapsed_ms"], old["elapsed_ms"])
    assert new["tail_ms"] < old["tail_ms"]
    assert n_new <= n_old + 12                                          # chẻ thêm có giới hạn, không bùng request
    assert new["warm_ttfb_p50_ms"] is not None and new["warm_ttfb_p50_ms"] < new["ttfb_first_p50_ms"]
