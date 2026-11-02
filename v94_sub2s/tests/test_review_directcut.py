# -*- coding: utf-8 -*-
"""Regression (review V9.4.1): commit claim 'a truncated input no longer passes and is never marked DONE' on the
>300s DIRECT-CUT path (no mux, so no strict output validation)."""
from pathlib import Path
import pytest
from test_pipeline_v94 import media, _make_master, _xlsx  # noqa: F401
from mock_gvs import MockGVS


@pytest.mark.parametrize("which", ["v_trunc", "a_trunc"])
def test_direct_cut_truncated_input(v94, media, tmp_path, monkeypatch, which):
    from conftest import get_qapp
    get_qapp()
    v, a = media[310]
    vb, ab = v.read_bytes(), a.read_bytes()
    if which == "v_trunc":
        vb = vb[: int(len(vb) * 0.6)]
    else:
        ab = ab[: int(len(ab) * 0.4)]
    vid = "AbCdEfGhIjZ"
    url = f"https://www.youtube.com/shorts/{vid}"
    monkeypatch.setattr(v94, "ARIA2C_PATH", "/bin/true")
    with MockGVS() as g:
        vurl = g.add("/videoplayback/v", vb)
        aurl = g.add("/videoplayback/a", ab)
        bundle = {"id": vid, "title": "テスト動画タイトル", "video_url": vurl, "video_format_id": "244",
                  "video_content_length": len(vb), "audio_url": aurl, "audio_format_id": "140",
                  "audio_content_length": len(ab), "audio_mime": "audio/mp4", "duration_sec": 310.0,
                  "source": "NATIVE_VISIONOS", "resolver_ms": 1.0}
        monkeypatch.setattr(v94, "_fast_innertube_480", lambda u: dict(bundle))
        monkeypatch.setattr(v94, "_ytdlp_resolve_480_on_warm_thread", lambda u, robust=False: None)
        xl = tmp_path / "check.xlsx"
        _xlsx(str(xl), url)
        v94._EXCEL_DF_CACHE.clear()
        master = _make_master(v94, tmp_path)
        w = v94.Worker(master, str(xl), "URL", "STATUS")
        w._process_single_url(url, 1)
    from openpyxl import load_workbook
    st = load_workbook(str(xl)).worksheets[0].cell(row=3, column=6).value
    files = sorted(p for p in (tmp_path / "final").rglob("*") if p.is_file())
    infos = [(f.name[-20:], v94._ffprobe_av_info(f)) for f in files]
    print("\n", which, "STATUS=", st, "fail=", w._v94.get("fail"))
    for n, i in infos:
        print("   ", n, "v=%.1f a=%.1f has_v=%s has_a=%s" % (i["video_duration"], i["audio_duration"], i["has_video"], i["has_audio"]))
    print("\n".join(l for l in master.logs if "DIRECT-CUT" in l or "Cắt" in l or "VALIDATE" in l)[:2000])
    assert st != "DONE"
