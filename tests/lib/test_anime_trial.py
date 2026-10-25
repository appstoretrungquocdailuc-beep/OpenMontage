"""Windows real-footage trial kit (lib/anime_trial.py).

Everything here runs on Linux: subprocess calls are faked where they would
need Windows / a GPU / pip, and media steps use a synthetic ffmpeg clip with a
transcript fixture. Nothing claims real anime accuracy.
"""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib import anime_trial as trial
from tests.lib.test_anime_source_indexer import _transcript

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
FPS = 24000 / 1001


# ── environment ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("banner", "expected"),
    [
        ("ffmpeg version 7.1-full_build-www.gyan.dev Copyright (c) 2000-2024", (7, 1)),
        ("ffmpeg version 6.1.1-3ubuntu5 Copyright (c) 2000-2023", (6, 1)),
        ("ffmpeg version n5.1.4 Copyright", (5, 1)),
        ("ffmpeg version N-112345-gabcdef Copyright", None),
    ],
)
def test_parse_ffmpeg_version(banner, expected):
    assert trial.parse_ffmpeg_version(banner) == expected


def test_parse_nvidia_smi_csv():
    gpus = trial.parse_nvidia_smi("NVIDIA GeForce RTX 3060, 12288 MiB, 560.94\n")
    assert gpus == [{"name": "NVIDIA GeForce RTX 3060", "memory_mib": 12288, "driver": "560.94"}]
    assert trial.parse_nvidia_smi("") == []


def test_pip_packages_add_cuda_runtime_only_with_gpu():
    cpu = trial.pip_packages(has_gpu=False)
    gpu = trial.pip_packages(has_gpu=True)
    assert any(p.startswith("faster-whisper") for p in cpu)
    assert not any(p.startswith("nvidia-") for p in cpu)
    assert any(p.startswith("nvidia-cublas-cu12") for p in gpu)
    assert any(p.startswith("nvidia-cudnn-cu12") for p in gpu)


def test_register_windows_cuda_dlls_finds_nvidia_bin_dirs(tmp_path):
    for lib in ("cublas", "cudnn"):
        (tmp_path / "nvidia" / lib / "bin").mkdir(parents=True)
    added = []
    dirs = trial.register_windows_cuda_dlls(site_dirs=[tmp_path], os_name="nt", add_dll_directory=added.append)
    assert sorted(Path(d).parent.name for d in dirs) == ["cublas", "cudnn"]
    assert len(added) == 2


def test_register_windows_cuda_dlls_is_noop_elsewhere(tmp_path):
    (tmp_path / "nvidia" / "cublas" / "bin").mkdir(parents=True)
    assert trial.register_windows_cuda_dlls(site_dirs=[tmp_path], os_name="posix") == []


def _fake_runner(outputs: dict):
    def run(cmd, **kwargs):
        key = cmd[0] if cmd[0] != "ffmpeg" or "-filters" not in cmd else "ffmpeg-filters"
        if key not in outputs:
            raise FileNotFoundError(cmd[0])
        return SimpleNamespace(returncode=0, stdout=outputs[key], stderr="")
    return run


def test_check_environment_reports_each_component():
    runner = _fake_runner({
        "ffmpeg": "ffmpeg version 7.1-full_build-www.gyan.dev Copyright",
        "ffmpeg-filters": " ... scdet  V->V  Detect video scene change\n ... ssim  VV->V  SSIM",
        "ffprobe": "ffprobe version 7.1",
        "nvidia-smi": "NVIDIA GeForce RTX 3060, 12288 MiB, 560.94",
    })
    checks = {c.name: c for c in trial.check_environment(runner=runner, module_available=lambda m: m != "jsonschema",
                                                          cuda_device_count=lambda: 1)}
    assert checks["ffmpeg"].status == "PASS"
    assert checks["ffmpeg_filters"].status == "PASS"
    assert checks["gpu"].status == "PASS" and "RTX 3060" in checks["gpu"].detail
    assert checks["faster_whisper"].status == "PASS"
    assert checks["cuda_runtime"].status == "PASS"
    assert checks["jsonschema"].status == "WARN"


def test_check_environment_fails_without_ffmpeg_and_warns_without_gpu():
    checks = {c.name: c for c in trial.check_environment(runner=_fake_runner({}), module_available=lambda m: False,
                                                          cuda_device_count=lambda: 0)}
    assert checks["ffmpeg"].status == "FAIL"
    assert checks["gpu"].status == "WARN"
    assert checks["faster_whisper"].status == "FAIL"


def test_old_ffmpeg_fails_version_check():
    runner = _fake_runner({"ffmpeg": "ffmpeg version 4.4.2", "ffmpeg-filters": "scdet ssim", "ffprobe": "x"})
    checks = {c.name: c for c in trial.check_environment(runner=runner, module_available=lambda m: True,
                                                          cuda_device_count=lambda: 0)}
    assert checks["ffmpeg"].status == "FAIL"


def test_setup_installs_planned_packages_with_current_python(monkeypatch):
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "nvidia-smi":
            return SimpleNamespace(returncode=0, stdout="NVIDIA GeForce RTX 3060, 12288 MiB, 560.94", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    plan = trial.install_packages(runner=runner)
    pip_call = next(c for c in calls if "pip" in c)
    assert pip_call[:4] == [trial.sys.executable, "-m", "pip", "install"]
    assert any(p.startswith("nvidia-cudnn-cu12") for p in pip_call)
    assert plan["has_gpu"] is True


# ── input preparation ───────────────────────────────────────────────────


@pytest.fixture(scope="session")
def episode(tmp_path_factory) -> Path:
    """20 s synthetic 'episode': red 6 s, blue 7 s, green 7 s, sine audio."""
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg not installed")
    out = tmp_path_factory.mktemp("trial_ep") / "episode.mp4"
    cmd = ["ffmpeg", "-v", "error", "-y"]
    for colour, d in (("red", 6), ("blue", 7), ("green", 7)):
        cmd += ["-f", "lavfi", "-i", f"color=c={colour}:s=320x240:r=24000/1001:d={d}"]
    cmd += ["-f", "lavfi", "-i", "sine=f=440:d=20", "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
            "-map", "[v]", "-map", "3:a", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out)]
    subprocess.run(cmd, check=True, timeout=120)
    return out


@needs_ffmpeg
def test_make_clip_starts_at_zero_with_requested_length(episode, tmp_path):
    info = trial.make_clip(episode, tmp_path / "clip.mp4", start=4.0, seconds=6.0)
    assert info["start_offset_seconds"] == pytest.approx(4.0, abs=1 / FPS)
    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=duration,r_frame_rate",
         "-of", "json", str(tmp_path / "clip.mp4")], capture_output=True, text=True, check=True).stdout)["streams"][0]
    assert float(probe["duration"]) == pytest.approx(6.0, abs=0.1)
    assert probe["r_frame_rate"] == "24000/1001"


@needs_ffmpeg
def test_make_clip_rejects_start_past_end(episode, tmp_path):
    with pytest.raises(trial.TrialError, match="past the end"):
        trial.make_clip(episode, tmp_path / "clip.mp4", start=25.0, seconds=10.0)


def test_auto_clip_start_skips_the_opening_only_for_long_videos():
    assert trial.auto_clip_start(480.0) == 60.0
    assert trial.auto_clip_start(90.0) == 0.0


def test_prepare_input_copies_unsafe_paths(tmp_path):
    src = tmp_path / "クレヨン 'test'.mp4"
    src.write_bytes(b"data")
    safe = trial.prepare_input(src, tmp_path / "work")
    assert safe != src and safe.read_bytes() == b"data"
    assert safe.name.isascii() and "'" not in str(safe)
    plain = tmp_path / "episode01.mp4"
    plain.write_bytes(b"x")
    assert trial.prepare_input(plain, tmp_path / "work") == plain


# ── automatic checks on a built index ───────────────────────────────────


@pytest.fixture
def built(episode, tmp_path):
    from lib.anime_source_indexer import build_source_index

    transcript = tmp_path / "t.json"
    transcript.write_text(json.dumps(_transcript(), ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "run"
    result = build_source_index(episode, out, transcript_path=transcript, provenance_kind="mock")
    return result.index, out


@needs_ffmpeg
def test_check_index_passes_structural_checks(built):
    index, out = built
    checks, stats = trial.check_index(index, out)
    by = {c.name: c for c in checks}
    for name in ("scenes_contiguous", "frame_grid", "keyframes_exist", "lines_inside_scenes", "unique_ids"):
        assert by[name].status == "PASS", (name, by[name].detail)
    assert stats["scene_count"] == 3
    assert stats["line_count"] == 4  # cuts at 6 s / 13 s: the 2.4–3.9 s segment is not split
    assert stats["suspected_false_cuts"] == []


@needs_ffmpeg
def test_check_index_fails_on_missing_keyframe_and_stray_line(built):
    index, out = built
    Path(index["scenes"][0]["keyframes"][0]["path"]).unlink()
    index["scenes"][1]["dialogue"].append({"line_id": "x", "start_seconds": 0.1, "end_seconds": 0.5, "text_ja": "x"})
    by = {c.name: c for c in trial.check_index(index, out)[0]}
    assert by["keyframes_exist"].status == "FAIL"
    assert by["lines_inside_scenes"].status == "FAIL"


@needs_ffmpeg
def test_cut_inside_one_colour_is_flagged_as_suspected_false_cut(built):
    index, out = built
    red = index["scenes"][0]
    fake_end = 3.003
    first, second = dict(red), dict(red)
    first.update(end_seconds=fake_end, scene_id="sc001", keyframes=[k for k in red["keyframes"] if k["timestamp_seconds"] < fake_end])
    second.update(start_seconds=fake_end, scene_id="sc001b", dialogue=[],
                  keyframes=[k for k in red["keyframes"] if k["timestamp_seconds"] >= fake_end])
    index["scenes"] = [first, second] + index["scenes"][1:]
    stats = trial.check_index(index, out)[1]
    assert "sc001b" in stats["suspected_false_cuts"]
    assert "sc002" not in stats["suspected_false_cuts"]  # red→blue is a real cut


def test_dialogue_heuristics_flag_low_confidence_share():
    index = {"video": {"fps": FPS, "duration_seconds": 10.0}, "scenes": [{
        "scene_id": "sc001", "start_seconds": 0.0, "end_seconds": 10.0, "keyframes": [],
        "dialogue": [
            {"line_id": f"l{i}", "start_seconds": i, "end_seconds": i + 0.5, "text_ja": "あ", "asr_confidence": 0.3,
             "low_confidence": True, "asr_flags": []} for i in range(4)
        ]}]}
    stats = trial.dialogue_stats(index)
    assert stats["low_confidence_ratio"] == 1.0
    assert stats["mean_confidence"] == pytest.approx(0.3)


# ── manual sheets and scoring ───────────────────────────────────────────


@needs_ffmpeg
def test_check_sheets_are_excel_friendly_utf8_bom(built):
    index, out = built
    dialogue_csv, scene_csv = trial.write_check_sheets(index, out)
    assert dialogue_csv.read_bytes().startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(dialogue_csv.open(encoding="utf-8-sig")))
    assert len(rows) == 4 and rows[0]["text_ja"] == "ケーキ買ってきたよ。"
    assert "ket_qua" in rows[0] and rows[0]["ket_qua"] == ""
    assert len(list(csv.DictReader(scene_csv.open(encoding="utf-8-sig")))) == 3


def _fill(path: Path, values: list[dict]):
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    for row, update in zip(rows, values):
        row.update(update)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


@needs_ffmpeg
def test_score_computes_accuracy_cer_and_false_cut_rate(built):
    index, out = built
    dialogue_csv, scene_csv = trial.write_check_sheets(index, out)
    _fill(dialogue_csv, [{"ket_qua": "DUNG"}, {"ket_qua": "SAI", "text_ja_dung": "食べちゃダメよ！ほんとうに？"},
                         {"ket_qua": "ĐÚNG"}, {"ket_qua": "bỏ qua"}])
    _fill(scene_csv, [{"ket_qua": "DUNG"}, {"ket_qua": "CAT_NHAM"}, {"ket_qua": "DUNG", "sot_canh": "1"}])
    score = trial.score_run(out)
    assert score["dialogue"]["reviewed"] == 3
    assert score["dialogue"]["skipped"] == 1
    assert score["dialogue"]["accuracy"] == pytest.approx(2 / 3, abs=1e-3)
    assert 0 < score["dialogue"]["cer"] <= 1
    assert score["dialogue"]["status"] == "WARN"
    assert score["scenes"]["false_cut_rate"] == pytest.approx(1 / 3, abs=1e-3)
    assert score["scenes"]["missed_cuts"] == 1
    assert score["dialogue"]["complete"] is True


@needs_ffmpeg
def test_score_with_half_filled_sheet_is_partial_not_pass(built):
    index, out = built
    dialogue_csv, _ = trial.write_check_sheets(index, out)
    _fill(dialogue_csv, [{"ket_qua": "DUNG"}])
    score = trial.score_run(out)
    assert score["dialogue"]["complete"] is False
    assert score["dialogue"]["status"] == "PARTIAL"


def test_cer_is_levenshtein_over_reference_length():
    assert trial.character_error_rate("ほんとに", "ほんとうに") == pytest.approx(1 / 5)
    assert trial.character_error_rate("同じ", "同じ") == 0.0


# ── end-to-end trial and report ─────────────────────────────────────────


@needs_ffmpeg
def test_run_trial_clip_mode_writes_everything_and_marks_pending(episode, tmp_path):
    transcript = tmp_path / "t.json"
    transcript.write_text(json.dumps(_transcript(), ensure_ascii=False), encoding="utf-8")
    report = trial.run_trial(episode, tmp_path / "ket_qua", mode="clip", clip_start=2.0, clip_seconds=12.0,
                             transcript=transcript, provenance_kind="mock")
    run_dir = Path(report["run_dir"])
    for name in ("anime_source_index.json", "review_packet.md", "trial_report.md", "trial_report.json",
                 "kiem_tra_loi_thoai.csv", "kiem_tra_canh.csv"):
        assert (run_dir / name).is_file(), name
    assert any((run_dir / "keyframes").iterdir())
    assert report["clip"]["start_offset_seconds"] == pytest.approx(2.0, abs=1 / FPS)
    for stage in ("clip", "probe", "scene_detect", "keyframes", "transcript", "checks", "total"):
        assert stage in report["timings"], stage
    status = {s["name"]: s["status"] for s in report["steps"]}
    assert status["ai_visual_review"] == "PENDING"
    assert status["manual_dialogue_check"] == "PENDING"
    assert status["manual_scene_check"] == "PENDING"
    assert status["full_episode"] == "NOT_RUN"
    assert report["overall"] != "PASS"
    md = (run_dir / "trial_report.md").read_text(encoding="utf-8")
    assert "CHƯA" in md and "PENDING" in md
    assert "transcript có sẵn" in md and "`None`" not in md  # no fake ASR model/device line


@needs_ffmpeg
def test_run_trial_reports_build_failure_without_crashing(episode, tmp_path, monkeypatch):
    monkeypatch.setattr("lib.anime_source_indexer._faster_whisper_available", lambda: False)
    report = trial.run_trial(episode, tmp_path / "ket_qua", mode="clip", clip_start=0.0, clip_seconds=5.0)
    assert report["overall"] == "FAIL"
    assert "faster-whisper" in report["error"]
    assert (Path(report["run_dir"]) / "trial_report.md").is_file()


@needs_ffmpeg
def test_score_updates_report(built, tmp_path):
    index, out = built
    trial.write_check_sheets(index, out)
    (out / "trial_report.json").write_text(json.dumps({"steps": [
        {"name": "manual_dialogue_check", "status": "PENDING"}, {"name": "manual_scene_check", "status": "PENDING"}],
        "timings": {}, "overall": "PENDING"}), encoding="utf-8")
    trial.score_run(out)
    report = json.loads((out / "trial_report.json").read_text(encoding="utf-8"))
    assert "score" in report
    assert {s["name"]: s["status"] for s in report["steps"]}["manual_dialogue_check"] == "PENDING"  # nothing filled


def test_cli_without_video_uses_picker_and_aborts_when_cancelled(monkeypatch, capsys):
    monkeypatch.setattr(trial, "pick_video", lambda initial_dir: None)
    assert trial.main(["run", "--mode", "clip"]) == 2
    assert "Chưa chọn video" in capsys.readouterr().out


@needs_ffmpeg
def test_smoke_test_runs_whisper_on_one_second_of_silence(monkeypatch, tmp_path):
    import sys
    used = []

    class FakeWhisperModel:
        def __init__(self, model_size, *, device, compute_type):
            used.append((model_size, device, compute_type))

        def transcribe(self, audio, **kwargs):
            assert Path(audio).is_file()
            return iter(()), SimpleNamespace(language="ja", duration=1.0)

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeWhisperModel))
    result = trial.smoke_test("tiny", "cuda", work_dir=tmp_path)
    assert result["ok"] is True and used == [("tiny", "cuda", "float16")]


@needs_ffmpeg
def test_smoke_test_reports_cuda_failure_instead_of_raising(monkeypatch, tmp_path):
    import sys

    class BrokenModel:
        def __init__(self, *a, **k):
            raise RuntimeError("Could not load library cudnn_ops64_9.dll")

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=BrokenModel))
    result = trial.smoke_test("tiny", "cuda", work_dir=tmp_path)
    assert result["ok"] is False and "cudnn" in result["error"]


@needs_ffmpeg
def test_frame_similarity_sees_hue_not_just_luma(tmp_path):
    """green (0,128,0) and red (255,0,0) have equal luma; a real cut must not look 'similar'."""
    paths = {}
    for colour in ("green", "red"):
        paths[colour] = tmp_path / f"{colour}.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={colour}:s=320x240:d=0.1",
                        "-frames:v", "1", str(paths[colour])], check=True, timeout=60)
    assert trial.frame_similarity(paths["green"], paths["red"]) < 0.5
    assert trial.frame_similarity(paths["red"], paths["red"]) > 0.99
