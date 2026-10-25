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
    "5_KIEM_TRA_LAI.bat": "recheck",
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
    for sub in ("setup", "check", "run", "score", "recheck"):
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
        ignored = subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=REPO, check=False).returncode == 0
        assert ignored == (not path.startswith("scripts/")), path


def test_guide_tells_where_to_put_video_what_to_run_and_where_results_are():
    text = GUIDE.read_text(encoding="utf-8")
    for needle in (*BATS, "anime_trial\\video_vao", "anime_trial\\ket_qua", "trial_report.md",
                   "review_packet.md", "anime_source_index.json", "kiem_tra_loi_thoai.csv", "kiem_tra_canh.csv",
                   "PENDING", "Python 3.11", "FFmpeg"):
        assert needle in text, needle


# ── Tier 1 local Qwen3-VL experiment (separate venv, Whisper untouched) ─

QWEN_BATS = {
    "6_CAI_DAT_QWEN3_VL.bat": "-m lib.qwen_local_backend setup",
    "7_QWEN3_VL_HIEU_PHIM.bat": "-m lib.anime_story_reasoner run",
    "8_CHAM_BAN_DO_COT_TRUYEN.bat": "-m lib.anime_story_reasoner score",
}


@pytest.mark.parametrize(("name", "command"), QWEN_BATS.items())
def test_qwen_bats_use_their_own_venv(name, command):
    raw = (KIT / name).read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n"), "CRLF"
    text = raw.decode("utf-8")
    assert command in text
    assert ".venv-qwen" in text and ".venv-anime" not in text
    assert "chcp 65001" in text and 'cd /d "%~dp0..\\.."' in text


def test_qwen_setup_bat_creates_the_separate_venv():
    text = (KIT / "6_CAI_DAT_QWEN3_VL.bat").read_text(encoding="utf-8")
    assert "-m venv .venv-qwen" in text and "py -3.11" in text


def test_qwen_venv_is_git_ignored():
    assert re.search(r"^/\.venv-qwen/$", (REPO / ".gitignore").read_text(encoding="utf-8"), re.MULTILINE)


def test_tier1_guide_states_model_offline_and_videomind_honestly():
    text = (REPO / "docs" / "anime-recap" / "HUONG_DAN_TANG1_QWEN3_VL.md").read_text(encoding="utf-8")
    for needle in (*QWEN_BATS, "Qwen/Qwen3-VL-4B-Thinking", "VideoMind", "offline", "$0", "ban_do_cot_truyen.md",
                   "kiem_tra_ban_do_cot_truyen.csv", "so_sanh_model.csv", "qwen_trial_report.md"):
        assert needle in text, needle


# ── Claude Pro workflow (no API): packet export / answer import, inside .venv-anime ─

CLAUDE_BATS = {
    "9_XUAT_GOI_CHO_CLAUDE.bat": ("-m lib.anime_claude_packet export --mode overview",
                                  "-m lib.anime_claude_packet export --mode detail", "-m lib.anime_claude_packet more"),
    "10_NHAP_KET_QUA_CLAUDE.bat": ("-m lib.anime_claude_packet understanding --paste", "-m lib.anime_claude_packet approve",
                                   "-m lib.anime_claude_packet script --paste"),
}


@pytest.mark.parametrize(("name", "commands"), CLAUDE_BATS.items())
def test_claude_bats_call_the_packet_module(name, commands):
    raw = (KIT / name).read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n"), "CRLF"
    text = raw.decode("utf-8")
    for command in commands:
        assert command in text, command
    assert ".venv-anime" in text and ".venv-qwen" not in text
    assert "chcp 65001" in text and 'cd /d "%~dp0..\\.."' in text
    assert "anthropic" not in text.lower() and "api_key" not in text.lower()


def test_claude_pro_guide_covers_the_workflow():
    text = (REPO / "docs" / "anime-recap" / "HUONG_DAN_CLAUDE_PRO.md").read_text(encoding="utf-8")
    for needle in (*CLAUDE_BATS, "01_khung_hinh.pdf", "02_loi_thoai_va_canh.md", "03_PROMPT_HIEU_PHIM.md",
                   "04_PROMPT_VIET_KICH_BAN.md", "hieu_phim.md", "kich_ban.md", "cut_list.csv", "DUYET", "#253-1",
                   "sw-sitcom-comedy", "sw-japanese-screenwriting", "sw-scene-craft", "sw-character-conflict",
                   "sw-story-structure", "không chạy lại Whisper", "Không render", "$0"):
        assert needle.lower() in text.lower(), needle
