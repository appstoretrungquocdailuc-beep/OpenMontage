# -*- coding: utf-8 -*-
"""Regression (review V9.4.1): Duration gắn đúng input; N/A → không đặt sàn; fallback không dùng sàn resolver."""

HEAD = ("Input #0, matroska,webm, from 'v.webm':\n  Duration: N/A, start: 0.000\n"
        "Input #1, mov,mp4,m4a, from 'a.m4a':\n  Duration: 00:00:58.10, start: 0\n"
        "Output #0, mp4, to 'o.mp4':\n  Stream #0:0: Video: vp9, yuv420p, 480x854\n  Stream #0:1: Audio: aac\n"
        "[out#0/mp4 @ 0x1] video:2701kB audio:342kB muxing overhead: 1%\n"
        "size=    3106kB time=00:00:58.10 bitrate= 437kbits/s\n")


def test_duration_na_is_not_shifted(v94):
    info = v94._v94_parse_mux_log(HEAD)
    assert info["video_duration"] == 0.0 and abs(info["audio_duration"] - 58.10) < 1e-6
    assert info["in_dur_unknown"] is True
    assert v94._v94_strict_mux_check(info) == (None, "input_duration_unknown")


def test_both_durations_known(v94):
    txt = HEAD.replace("Duration: N/A", "Duration: 00:00:58.17")
    info = v94._v94_parse_mux_log(txt)
    assert abs(info["video_duration"] - 58.17) < 1e-6 and abs(info["audio_duration"] - 58.10) < 1e-6
    assert v94._v94_strict_mux_check(info) == (True, "OK")
