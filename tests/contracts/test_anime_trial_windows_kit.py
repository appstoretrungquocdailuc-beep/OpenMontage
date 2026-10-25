"""The Windows trial kit's .bat files and Vietnamese guide must match the code.

The .bat files cannot run here (no Windows), so this pins what they call:
the venv path, the `python -m lib.anime_trial` subcommands, UTF-8 console
setup — and that user media / venvs are git-ignored.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
KIT = REPO / "scripts" / "anime_trial"
GUIDE = REPO / "docs" / "anime-recap" / "HUONG_DAN_CHAY_THU_WINDOWS.md"

BATS = {
    "1_CAI_DAT.bat": "setup",
    "2_CHAY_THU_DOAN_NGAN.bat": "run --mode clip",
    "3_CHAY_TOAN_BO.bat": "run --mode full",
    "4_CHAM_DIEM.bat": "score",
}


@pytest.mark.parametrize(("name", "command"), BATS.items())
def test_bat_calls_trial_module_inside_venv(name, command):
    text = (KIT / name).read_text(encoding="utf-8")
    assert f"-m lib.anime_trial {command}" in text
    assert ".venv-anime" in text
    assert "chcp 65001" in text and "PYTHONUTF8=1" in text
    assert 'cd /d "%~dp0..\\.."' in text  # always run from the repo root


def test_bats_use_crlf_line_endings():
    for name in BATS:
        raw = (KIT / name).read_bytes()
        assert b"\r\n" in raw and raw.count(b"\n") == raw.count(b"\r\n"), name


def test_setup_bat_prefers_python_311_and_creates_the_venv():
    text = (KIT / "1_CAI_DAT.bat").read_text(encoding="utf-8")
    assert "py -3.11" in text
    assert "-m venv .venv-anime" in text


def test_subcommands_exist_in_the_cli():
    from lib import anime_trial

    source = Path(anime_trial.__file__).read_text(encoding="utf-8")
    for sub in ("setup", "check", "run", "score"):
        assert re.search(rf'add_parser\("{sub}"', source), sub


def test_user_media_and_venv_are_git_ignored():
    ignore = (REPO / ".gitignore").read_text(encoding="utf-8")
    assert re.search(r"^/anime_trial/$", ignore, re.MULTILINE)
    assert re.search(r"^/\.venv-anime/$", ignore, re.MULTILINE)


def test_kit_scripts_are_not_swallowed_by_the_media_ignore_rule():
    """An unanchored `anime_trial/` rule also ignored scripts/anime_trial/ — the kit itself."""
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git not installed")
    for path in ["scripts/anime_trial/1_CAI_DAT.bat", "anime_trial/video_vao/ep.mp4", ".venv-anime/x"]:
        ignored = subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=REPO).returncode == 0
        assert ignored == (not path.startswith("scripts/")), path


def test_guide_tells_where_to_put_video_what_to_run_and_where_results_are():
    text = GUIDE.read_text(encoding="utf-8")
    for needle in (*BATS, "anime_trial\\video_vao", "anime_trial\\ket_qua", "trial_report.md",
                   "review_packet.md", "anime_source_index.json", "kiem_tra_loi_thoai.csv", "kiem_tra_canh.csv",
                   "PENDING", "Python 3.11", "FFmpeg"):
        assert needle in text, needle
