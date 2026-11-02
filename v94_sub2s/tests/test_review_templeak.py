# -*- coding: utf-8 -*-
"""Regression (review V9.4.1): validate-fail path (V9.4.1 strict) leaves the downloaded inputs in _temp_yt480p."""
import pytest
from test_v941 import _run_job, h264_media, need_ff  # noqa: F401


@need_ff
@pytest.mark.parametrize("which", ["v_trunc", "a_trunc"])
def test_validate_fail_leaves_temp_inputs(v94, monkeypatch, tmp_path, h264_media, which):
    v = h264_media["v_trunc"] if which == "v_trunc" else h264_media["v"]
    a = h264_media["a_trunc"] if which == "a_trunc" else h264_media["a"]
    w, master, st = _run_job(v94, monkeypatch, tmp_path, v, a, 30)
    assert w._v94["fail"].startswith("validate_fail"), w._v94["fail"]
    tdir = tmp_path / "_temp_yt480p"
    left = sorted(p.name for p in tdir.glob("*")) if tdir.exists() else []
    print("fail=", w._v94["fail"], "keep_temp=", w.keep_temp, "left in temp:", left,
          [ (tdir / n).stat().st_size for n in left])
    assert not left, left
