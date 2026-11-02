# -*- coding: utf-8 -*-
"""Logic không mạng: chọn audio JA, đuôi file, Excel cache/calamine, telemetry, cờ."""
import json
import time

import pytest


def _player(audios, title="テスト動画です", default_lang="", author="チャンネル"):
    return {
        "videoDetails": {"title": title, "author": author},
        "microformat": {"playerMicroformatRenderer": {"defaultAudioLanguage": default_lang}},
        "streamingData": {"adaptiveFormats": audios},
    }


def _a(itag, mime="audio/mp4; codecs=\"mp4a.40.5\"", lang=None, tid=None, br=48000):
    f = {"itag": int(itag), "url": f"https://x/{itag}", "mimeType": mime, "bitrate": br}
    if lang or tid:
        f["audioTrack"] = {"id": tid or f"{lang}.4", "languageCode": lang or ""}
    return f


def test_explicit_ja_track_wins(v94):
    fmts = [_a(140, lang="en", tid="en.4"), _a(139, lang="ja", tid="ja.4")]
    pick = v94._pick_native_ja_audio(fmts, _player(fmts))
    assert pick["itag"] == 139 and not pick.get("_v85_inferred_single_ja")


def test_multi_track_without_ja_rejected(v94):
    fmts = [_a(140, lang="en", tid="en.4"), _a(139, lang="es", tid="es.4")]
    assert v94._pick_native_ja_audio(fmts, _player(fmts)) is None


def test_single_track_kana_inferred(v94):
    fmts = [_a(139), _a(140)]
    pick = v94._pick_native_ja_audio(fmts, _player(fmts))
    assert pick is not None and pick.get("_v85_inferred_single_ja")


def test_single_track_no_kana_rejected(v94):
    fmts = [_a(139)]
    assert v94._pick_native_ja_audio(fmts, _player(fmts, title="中文标题", author="频道")) is None


def test_guard_default_language_non_ja_blocks_inference(v94, v93):
    """Tiêu đề có kana nhưng player khai báo defaultAudioLanguage=en → V9.4 không đoán là JA."""
    fmts = [_a(139)]
    data = _player(fmts, title="【英語】リスニング練習", default_lang="en")
    assert v94._pick_native_ja_audio([dict(f) for f in fmts], data) is None
    # V9.3 (baseline) vẫn đoán là JA → rủi ro nhầm track. Ghi nhận khác biệt.
    assert v93._pick_native_ja_audio([dict(f) for f in fmts], data) is not None


def test_default_language_ja_accepts(v94):
    fmts = [_a(139)]
    pick = v94._pick_native_ja_audio(fmts, _player(fmts, title="no kana", author="x", default_lang="ja"))
    assert pick is not None


def test_audio_ext(v94):
    assert v94._v94_audio_ext({"audio_format_id": "139", "audio_mime": "audio/mp4"}) == ".m4a"
    assert v94._v94_audio_ext({"audio_format_id": "251", "audio_mime": "audio/webm; codecs=opus"}) == ".webm"
    assert v94._v94_audio_ext({"audio_format_id": "251-1"}) == ".webm"
    assert v94._v94_audio_ext({"audio_ext": "m4a"}) == ".m4a"


def test_ytdlp_bundle_244_then_135_and_strict_ja(v94):
    info = {"id": "abcdefghijk", "title": "t", "duration": 30, "formats": [
        {"format_id": "135", "url": "u135", "vcodec": "avc1", "filesize": 10},
        {"format_id": "244", "url": "u244", "vcodec": "vp9", "filesize": 20},
        {"format_id": "140-0", "url": "a_en", "vcodec": "none", "language": "en", "ext": "m4a"},
        {"format_id": "140-1", "url": "a_ja", "vcodec": "none", "language": "ja", "ext": "m4a"},
        {"format_id": "396", "url": "u360", "vcodec": "av01"},
    ]}
    b = v94._pick_ytdlp_480_bundle(info, "T")
    assert b["video_format_id"] == "244" and b["audio_url"] == "a_ja" and b["audio_mime"] == "audio/mp4"
    info["formats"] = [f for f in info["formats"] if f["format_id"] != "244"]
    assert v94._pick_ytdlp_480_bundle(info, "T")["video_format_id"] == "135"
    info["formats"] = [f for f in info["formats"] if f["format_id"] not in ("244", "135")]
    assert v94._pick_ytdlp_480_bundle(info, "T") is None          # không hạ 360p/AV1


def test_fastest_valid_480p_default_off(v94):
    assert v94.V94_FASTEST_VALID_480P is False
    assert v94.HARD_480_VIDEO_ITAGS == ("244", "135")


def _make_xlsx(path, rows, status_col="STATUS"):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["URL", "CHANNEL ID", "TÊN KÊNH", "TITLE", "LINK VIDEO GỐC", status_col])
    for r in rows:
        ws.append(r)
    wb.save(path)


def test_excel_reader_matches_openpyxl_and_status(v94, tmp_path):
    p = str(tmp_path / "check.xlsx")
    rows = [[f"https://www.youtube.com/shorts/AAAAAAAAAA{i % 10}", f"UC{i}", "kênh", f"タイトル{i}", "goc",
             "DONE" if i % 3 == 0 else ""] for i in range(60)]
    _make_xlsx(p, rows)
    links, ucol, scol = v94.read_links_excel_url_only(p, lambda m: None)
    assert ucol == "URL" and scol == "STATUS"
    want = [(r[0], i) for i, r in enumerate(rows) if r[5] != "DONE"]
    assert links == want
    assert v94.read_excel_cell_value(p, 4, "TITLE", lambda m: None) == "タイトル4"
    # ghi DONE đúng dòng (row = idx + 2)
    v94.write_status_to_excel(p, 4, "STATUS", "DONE", lambda m: None)
    from openpyxl import load_workbook
    ws = load_workbook(p).worksheets[0]
    assert ws.cell(row=6, column=6).value == "DONE"
    assert ws.cell(row=6, column=1).value == rows[4][0]


def test_excel_calamine_verify_fallback(v94, tmp_path, monkeypatch):
    """Ô số (CHANNEL ID = 12345) → nếu calamine đọc khác openpyxl thì tự tắt calamine cho file đó."""
    p = str(tmp_path / "num.xlsx")
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active
    ws.append(["URL", "CHANNEL ID", "STATUS"])
    ws.append(["https://youtu.be/AAAAAAAAAAA", 12345, ""])
    ws.append(["https://youtu.be/BBBBBBBBBBB", 1.5, "DONE"])
    wb.save(p)
    v94._EXCEL_DF_CACHE.clear()
    df = v94._read_excel_df_cached(p)
    import pandas as pd
    ref = pd.read_excel(p, sheet_name=0, dtype=str).fillna("")
    assert df.astype(str).values.tolist() == ref.astype(str).values.tolist()


def test_wakeup_signature_detects_append(v94, tmp_path):
    import threading
    trig = tmp_path / "trigger_wakeup.txt"
    trig.write_text("x")
    xl = tmp_path / "check.xlsx"; xl.write_bytes(b"0")
    paths = [trig]
    sig = v94._rt_trigger_signature(paths)
    esig = v94._rt_file_signature(str(xl))
    stop = threading.Event()
    res = {}

    def waiter():
        t0 = time.perf_counter()
        res["r"] = v94._rt_wait_for_wakeup(stop, paths, str(xl), sig, esig)
        res["dt"] = time.perf_counter() - t0
    th = threading.Thread(target=waiter); th.start()
    time.sleep(0.2)
    t_w = time.perf_counter()
    with open(trig, "a") as f:
        f.write("\n1")
    th.join(2)
    assert res["r"][2] == "TRIGGER"
    detect_ms = (time.perf_counter() - t_w) * 1000
    assert detect_ms < 100
    assert v94.REALTIME_WAKEUP_CHECK_SEC == pytest.approx(0.010)


def test_telemetry_redacts_and_flushes(v94, tmp_path):
    t = v94.V94Telemetry(out_dir=str(tmp_path), enabled=True)
    t.span("vid1", "gvs_request_begin", video_url="https://rr1---sn-x.googlevideo.com/videoplayback?sig=SECRET&pot=TOK")
    t.span("vid1", "status_done")
    n = t.flush()
    assert n == 2
    files = list(tmp_path.glob("telemetry_*.jsonl"))
    rows = [json.loads(l) for l in files[0].read_text(encoding="utf-8").splitlines()]
    assert rows[0]["ev"] == "gvs_request_begin" and "SECRET" not in json.dumps(rows) and "TOK" not in json.dumps(rows)
    assert rows[1]["t_ns"] >= rows[0]["t_ns"]
    t.close()


def test_telemetry_span_cost_is_tiny(v94):
    t = v94.V94Telemetry(enabled=True)
    t._thread = object()   # không start thread ghi file
    n = 20000
    t0 = time.perf_counter_ns()
    for i in range(n):
        t.span("j", "x", a=i)
    per_us = (time.perf_counter_ns() - t0) / n / 1000
    assert per_us < 20, per_us


def test_keepalive_yield_counter(v94):
    assert v94._v94_jobs_active() == 0
    v94._v94_job_enter(); v94._v94_job_enter()
    assert v94._v94_jobs_active() == 2
    v94._v94_job_exit(); v94._v94_job_exit(); v94._v94_job_exit()
    assert v94._v94_jobs_active() == 0


def test_env_flag_parser(v94, monkeypatch):
    monkeypatch.setenv("V94_FOO", "0")
    assert v94._v94_env("FOO", True) is False
    monkeypatch.setenv("V94_FOO", "12")
    assert v94._v94_env("FOO", 4) == 12
    monkeypatch.setenv("V94_FOO", "bad")
    assert v94._v94_env("FOO", 4) == 4


@pytest.mark.parametrize("modname", ["v93", "v94"])
def test_concurrent_status_writes_no_lost_update(request, modname, tmp_path):
    """8 job ghi DONE cùng lúc vào 8 dòng khác nhau. V9.4 phải giữ đủ 8. V9.3 có thể mất (race)."""
    import threading
    mod = request.getfixturevalue(modname)
    lost_total = 0
    corrupt = [0]
    for trial in range(3):
        p = str(tmp_path / f"w{trial}.xlsx")
        _make_xlsx(p, [[f"https://youtu.be/AAAAAAAAA{i:02d}", "UC", "k", "t", "g", ""] for i in range(8)])
        bar = threading.Barrier(8)

        def job(i):
            bar.wait()
            mod.write_status_to_excel(p, i, "STATUS", "DONE", lambda m: None)
        ths = [threading.Thread(target=job, args=(i,)) for i in range(8)]
        [t.start() for t in ths]
        [t.join(30) for t in ths]
        from openpyxl import load_workbook
        try:
            ws = load_workbook(p).worksheets[0]
            got = sum(1 for r in range(2, 10) if ws.cell(row=r, column=6).value == "DONE")
        except Exception:      # V9.3: các thread cùng ghi 'check.xlsx.tmp' → file zip hỏng
            got = 0
            corrupt[0] += 1
        lost_total += 8 - got
    if modname == "v94":
        assert lost_total == 0 and corrupt[0] == 0
    else:
        print(f"\nV9.3: 3 lần x 8 job ghi song song → mất {lost_total}/24 DONE, file hỏng {corrupt[0]}/3 lần")
