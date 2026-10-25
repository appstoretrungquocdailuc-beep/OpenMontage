"""Claude Pro workflow: evidence packet export and import of Claude's answers.

A 70 s synthetic episode (red 20 s → moving testsrc2 25 s → green 25 s) with a
mock transcript stands in for #253-1. No model is called anywhere.
"""

from __future__ import annotations

import csv
import itertools
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from lib import anime_claude_packet as acp
from lib.anime_source_indexer import build_source_index

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
pytest.importorskip("PIL")


def _w(text, s, e, p=0.9):
    return {"word": text, "start": s, "end": e, "probability": p}


def _transcript() -> dict:
    lines = [(1.0, 2.5, "ケーキ買ってきたよ。"), (8.0, 9.5, "あとで食べようね。"), (21.0, 22.5, "え？本当に？"),
             (30.0, 31.5, "食べちゃダメよ！"), (50.0, 51.5, "ケーキがない！"), (60.0, 61.5, "オラ知らないゾ。")]
    return {"language": "ja", "model_size": "fixture", "segments": [
        {"id": i, "start": s, "end": e, "text": t, "words": [_w(t, s, e)]} for i, (s, e, t) in enumerate(lines)]}


@pytest.fixture(scope="session")
def base_run(tmp_path_factory) -> Path:
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg not installed")
    base = tmp_path_factory.mktemp("packet")
    video = base / "episode.mp4"
    cmd = ["ffmpeg", "-v", "error", "-y",
           "-f", "lavfi", "-i", "color=c=red:s=320x240:r=24000/1001:d=20",
           "-f", "lavfi", "-i", "testsrc2=s=320x240:r=24000/1001:d=25",
           "-f", "lavfi", "-i", "color=c=green:s=320x240:r=24000/1001:d=25",
           "-f", "lavfi", "-i", "sine=f=440:d=70",
           "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]", "-map", "3:a",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(video)]
    subprocess.run(cmd, check=True, timeout=180)
    transcript = base / "t.json"
    transcript.write_text(json.dumps(_transcript(), ensure_ascii=False), encoding="utf-8")
    run = base / "ket_qua" / "ep_full_1"
    build_source_index(video, run, transcript_path=transcript, source_index_id="TEN-TAP-BI-MAT-253", provenance_kind="mock")
    return run


@pytest.fixture(scope="session")
def exported(base_run) -> Path:
    return acp.export_packet(base_run, mode="detail", start=0.0, end=70.0, progress=lambda m: None)


@pytest.fixture
def packet(exported, tmp_path) -> Path:
    """A private copy of the exported run, so tests may write answers into it."""
    run_copy = tmp_path / "ket_qua" / "ep_full_1"
    shutil.copytree(exported.parents[1], run_copy)
    pkt = run_copy / acp.PACKET_DIR / exported.name
    data = json.loads((pkt / acp.PACKET_FILE).read_text(encoding="utf-8"))
    data["run_dir"] = str(run_copy)
    (pkt / acp.PACKET_FILE).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return pkt


def _index(run: Path) -> dict:
    return json.loads((run / "anime_source_index.json").read_text(encoding="utf-8"))


# ── pure helpers ────────────────────────────────────────────────────────


def test_time_and_range_parsing():
    assert acp.parse_time("2:10") == 130.0 and acp.parse_time("0:01:23.5") == 83.5 and acp.parse_time("83,5") == 83.5
    assert acp.parse_ranges("2:41-2:44, 3:02.5 - 3:04") == [(161.0, 164.0), (182.5, 184.0)]
    with pytest.raises(acp.PacketError):
        acp.parse_ranges("3:04-3:02")
    assert acp.timecode(round(83.5 * 24000 / 1001), 24000 / 1001) == "00:01:23:10"  # non-drop count on 24 nominal


def test_extract_json_from_a_chatty_answer():
    text = 'Phần A: tóm tắt {không phải json}\n```json\n{"beats": [{"what_vi": "mẹ hét \\"Shin!\\" {"}]}\n```\nHết.'
    assert acp.extract_json(text) == {"beats": [{"what_vi": 'mẹ hét "Shin!" {'}]}
    with pytest.raises(acp.PacketError):
        acp.extract_json("Tôi không chắc.")


def test_hotspots_flag_cues_but_are_only_hints():
    index = {"scenes": [{"scene_id": "sc001", "start_seconds": 0.0, "end_seconds": 70.0, "dialogue": [
        {"line_id": "sc001_l01", "start_seconds": 1.0, "end_seconds": 2.5, "text_ja": "ケーキ買ってきたよ。"},
        {"line_id": "sc001_l02", "start_seconds": 21.0, "end_seconds": 22.5, "text_ja": "え？本当に？"},
        {"line_id": "sc001_l03", "start_seconds": 30.0, "end_seconds": 31.5, "text_ja": "食べちゃダメよ！", "excite": 0.2},
        {"line_id": "sc001_l04", "start_seconds": 50.0, "end_seconds": 51.5, "text_ja": "ケーキがない！"}]}]}
    spots = acp.find_hotspots(index, 0.0, 70.0)
    by = {(s["reason"], s["line_id"]) for s in spots}
    assert ("misunderstanding", "sc001_l02") in by and ("peak", "sc001_l03") in by
    assert ("callback", "sc001_l01") in by and ("callback", "sc001_l04") in by  # ケーキ … ケーキ 49 s later


# ── export ──────────────────────────────────────────────────────────────


@needs_ffmpeg
def test_frames_are_exact_by_frame_number(base_run, tmp_path):
    from PIL import Image

    fps = 24000 / 1001
    video = acp.resolve_video(_index(base_run), base_run)
    last_red = round(20.0 * fps) - 1  # 479 (19.979 s) is red, 480 (20.020 s) is the first testsrc2 frame
    frames = [{"frame": last_red}, {"frame": last_red + 1}]
    acp.extract_frames(video, frames, tmp_path, fps, width=160)
    red = Image.open(tmp_path / f"f{last_red:06d}.jpg").convert("RGB").getpixel((5, 5))
    other = Image.open(tmp_path / f"f{last_red + 1:06d}.jpg").convert("RGB").getpixel((5, 5))
    assert red[0] > 200 and red[1] < 60 and red[2] < 60
    assert not (other[0] > 200 and other[1] < 60 and other[2] < 60)


@needs_ffmpeg
def test_detail_packet_is_dense_where_it_matters(exported):
    packet = json.loads((exported / acp.PACKET_FILE).read_text(encoding="utf-8"))
    frames = packet["frames"]
    fps = packet["fps"]
    assert 140 <= len(frames) <= acp.DETAIL["max_frames"]
    assert all((exported / f["file"]).is_file() for f in frames)
    assert [f["frame"] for f in frames] == sorted({f["frame"] for f in frames})
    reasons = {r for f in frames for r in f["reasons"]}
    assert {"grid", "cut", "line", "motion", "misunderstanding", "callback"} <= reasons
    # around the misunderstanding cue (21–22.5 s) frames are ≤ 0.25 s apart; plain red stretch keeps the 0.5 s grid
    near = [f["t"] for f in frames if 20.6 <= f["t"] <= 22.8]
    assert max(b - a for a, b in itertools.pairwise(near)) <= 0.25
    # both sides of each cut are shown
    for sc in packet["scenes"][1:]:
        cut = round(sc["start_seconds"] * fps)
        assert {cut - 1, cut} <= {f["frame"] for f in frames}, sc["scene_id"]


@needs_ffmpeg
def test_packet_files_pdf_and_transcript(exported):
    pdf = (exported / "01_khung_hinh.pdf").read_bytes()
    packet = json.loads((exported / acp.PACKET_FILE).read_text(encoding="utf-8"))
    assert pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF")
    pages = pdf.count(b"/Type /Page ")
    assert pages == 1 + -(-len(packet["frames"]) // 9)
    md = (exported / "02_loi_thoai_va_canh.md").read_text(encoding="utf-8")
    assert "sc001_l01" in md and "ケーキ買ってきたよ。" in md and "KHÔNG phải kết luận" in md
    prompt = (exported / "03_PROMPT_HIEU_PHIM.md").read_text(encoding="utf-8")
    for needle in ("observation", "hypothesis", "evidence_requests", "KHÔNG dùng tên tập", "Qwen"):
        assert needle in prompt, needle
    script_prompt = (exported / "04_PROMPT_VIET_KICH_BAN.md").read_text(encoding="utf-8")
    for skill in ("sw-sitcom-comedy", "sw-japanese-screenwriting", "sw-scene-craft", "sw-character-conflict", "sw-story-structure"):
        assert skill in script_prompt, skill


@needs_ffmpeg
def test_episode_title_is_not_shown_to_claude(exported):
    for name in ("02_loi_thoai_va_canh.md", "03_PROMPT_HIEU_PHIM.md", "04_PROMPT_VIET_KICH_BAN.md"):
        assert "TEN-TAP-BI-MAT" not in (exported / name).read_text(encoding="utf-8"), name
    assert "TEN-TAP" not in exported.name


@needs_ffmpeg
def test_phase2_files_are_not_modified(base_run, exported):
    index = _index(base_run)
    assert index["source_index_id"] == "TEN-TAP-BI-MAT-253"
    before = (base_run / "anime_source_index.json").stat().st_mtime
    acp.export_packet(base_run, mode="overview", progress=lambda m: None)
    assert (base_run / "anime_source_index.json").stat().st_mtime == before


@needs_ffmpeg
def test_overview_packet_covers_the_whole_episode_sparsely(base_run):
    pkt = acp.export_packet(base_run, mode="overview", progress=lambda m: None)
    packet = json.loads((pkt / acp.PACKET_FILE).read_text(encoding="utf-8"))
    times = [f["t"] for f in packet["frames"]]
    assert times[0] < 2.5 and times[-1] > 67 and len(times) < 70
    assert (pkt / "03_PROMPT_CHON_TINH_HUONG.md").is_file()


@needs_ffmpeg
def test_detail_range_is_bounded(base_run):
    with pytest.raises(acp.PacketError):
        acp.export_packet(base_run, mode="detail", start=0.0, end=5.0, progress=lambda m: None)


@needs_ffmpeg
def test_evidence_request_adds_citable_frames(packet):
    pdf = acp.add_evidence(packet, [(21.0, 22.0)], fps_sample=12, progress=lambda m: None)
    data = json.loads((packet / acp.PACKET_FILE).read_text(encoding="utf-8"))
    assert pdf.is_file() and data["supplements"][0]["name"] == "bo_sung_01"
    added = data["supplements"][0]["frames"]
    assert len(added) >= 11 and all((packet / "frames" / f"f{f:06d}.jpg").is_file() for f in added)
    assert set(added) <= {f["frame"] for f in data["frames"]}


# ── understanding ───────────────────────────────────────────────────────


def _fid(packet_dir: Path, t: float) -> str:
    frames = json.loads((packet_dir / acp.PACKET_FILE).read_text(encoding="utf-8"))["frames"]
    return f"f{min(frames, key=lambda f: abs(f['t'] - t))['frame']:06d}"


def _understanding(p: Path) -> dict:
    return {
        "situation": {"complete": True, "start_seconds": 0.0, "end_seconds": 70.0, "summary_vi": "Bánh biến mất.", "beat_ids": ["b01", "b04"]},
        "characters": [{"character_id": "misae", "name_ja": "みさえ", "name_vi": "Misae", "identified_by_vi": "giọng nói",
                        "goal_vi": "giữ bánh", "obstacle_vi": "Shin", "beat_ids": ["b01", "b03"], "kind": "inference"}],
        "beats": [
            {"beat_id": "b01", "start_seconds": 1.0, "end_seconds": 2.5, "kind": "observation", "who": ["misae"],
             "what_vi": "Có người nói đã mua bánh về.", "frames": [_fid(p, 1.5)], "line_ids": ["sc001_l01"]},
            {"beat_id": "b02", "start_seconds": 21.0, "end_seconds": 22.5, "kind": "observation", "who": [],
             "what_vi": "Có người hỏi lại 'thật sao?'.", "frames": [_fid(p, 21.5)], "line_ids": ["sc002_l01"]},
            {"beat_id": "b03", "start_seconds": 30.0, "end_seconds": 31.5, "kind": "observation", "who": ["misae"],
             "what_vi": "Cấm ăn bánh.", "frames": [], "line_ids": ["sc002_l02"]},
            {"beat_id": "b04", "start_seconds": 50.0, "end_seconds": 51.5, "kind": "observation", "who": [],
             "what_vi": "Phát hiện bánh không còn.", "frames": [_fid(p, 50.5)], "line_ids": ["sc003_l01"]},
            {"beat_id": "b05", "start_seconds": 40.0, "end_seconds": 45.0, "kind": "hypothesis", "who": [],
             "what_vi": "Có thể Shin đã ăn bánh.", "frames": [], "line_ids": []},
        ],
        "causal_chain": [{"step": 1, "cause": ["b01"], "action": ["b03"], "reaction": ["b04"], "consequence": [],
                          "explanation_vi": "bánh mua về bị cấm ăn rồi biến mất", "kind": "inference"}],
        "misunderstandings": [],
        "gags": [{"setup": ["b01"], "payoff": ["b04"], "mechanism": "callback", "why_funny_vi": "ケーキ quay lại", "kind": "inference"}],
        "dialogue_doubts": [], "evidence_requests": [{"start_seconds": 40.0, "end_seconds": 45.0, "reason_vi": "ai ăn bánh?"}],
    }


def _lines_by_text(run: Path) -> dict:
    return {line["text_ja"]: line["line_id"] for sc in _index(run)["scenes"] for line in sc.get("dialogue", [])}


def _fix_line_ids(data: dict, run: Path) -> dict:
    ids = _lines_by_text(run)
    mapping = {"sc001_l01": ids["ケーキ買ってきたよ。"], "sc002_l01": ids["え？本当に？"], "sc002_l02": ids["食べちゃダメよ！"],
               "sc003_l01": ids["ケーキがない！"]}
    for b in data["beats"]:
        b["line_ids"] = [mapping.get(x, x) for x in b["line_ids"]]
    return data


@needs_ffmpeg
def test_valid_understanding_is_ok_and_rendered_with_real_dialogue(packet):
    data = _fix_line_ids(_understanding(packet), packet.parents[1])
    result = acp.import_understanding(packet, "Phần A…\n```json\n" + json.dumps(data, ensure_ascii=False) + "\n```")
    assert result["status"] == "OK", result["errors"]
    md = (packet / "hieu_phim.md").read_text(encoding="utf-8")
    assert "ケーキ買ってきたよ。" in md and "QUAN SÁT" in md and "GIẢ THUYẾT" in md
    assert "40.0-45.0" in md  # ready-to-paste evidence request


@needs_ffmpeg
@pytest.mark.parametrize(("mutate", "needle"), [
    (lambda d: d["beats"][0].update(frames=["f999999"]), "không có trong gói"),
    (lambda d: d["beats"][3].update(frames=[], line_ids=[]), "observation phải có"),
    (lambda d: d["causal_chain"][0].update(kind="observation"), "không thể là observation"),
    (lambda d: d["gags"][0].update(setup=["b04"], payoff=["b01"]), "payoff xảy ra trước setup"),
    (lambda d: d["causal_chain"][0].update(cause=["b09"]), "không tồn tại"),
    (lambda d: d["beats"][0].update(start_seconds=200.0, end_seconds=201.0), "ngoài phạm vi"),
    (lambda d: d["gags"][0].update(setup=["b05"], payoff=["b05"]), "chỉ dựa trên beat giả thuyết"),
])
def test_understanding_errors_are_caught(packet, mutate, needle):
    data = _fix_line_ids(_understanding(packet), packet.parents[1])
    mutate(data)
    result = acp.import_understanding(packet, json.dumps(data, ensure_ascii=False))
    assert result["status"] == "FAIL" and any(needle in e for e in result["errors"]), result["errors"]
    assert "Lỗi phải sửa" in (packet / "hieu_phim.md").read_text(encoding="utf-8")


@needs_ffmpeg
def test_understanding_with_only_hypotheses_fails(packet):
    data = _fix_line_ids(_understanding(packet), packet.parents[1])
    for b in data["beats"]:
        b["kind"] = "hypothesis"
    data["characters"][0]["kind"] = "hypothesis"
    data["causal_chain"][0]["kind"] = data["gags"][0]["kind"] = "hypothesis"
    result = acp.import_understanding(packet, json.dumps(data, ensure_ascii=False))
    assert result["status"] == "FAIL" and any("chưa hiểu được" in e for e in result["errors"])


@needs_ffmpeg
def test_approval_is_bound_to_the_file(packet):
    data = _fix_line_ids(_understanding(packet), packet.parents[1])
    acp.import_understanding(packet, json.dumps(data, ensure_ascii=False))
    acp.approve_understanding(packet)
    acp._approved_understanding(packet)
    (packet / acp.UNDERSTANDING_FILE).write_text(json.dumps({**data, "x": 1}), encoding="utf-8")
    with pytest.raises(acp.PacketError, match="thay đổi"):
        acp._approved_understanding(packet)


# ── script → cut list ───────────────────────────────────────────────────


def _script(run: Path) -> dict:
    ids = _lines_by_text(run)
    scenes = {sc["scene_id"]: sc for sc in _index(run)["scenes"]}
    sc1, sc2, sc3 = sorted(scenes)

    def ev(sid, s, e, line=None):
        return [{"scene_id": sid, "line_id": line, "start_seconds": s, "end_seconds": e}]

    return {"title_ja": "消えたケーキの謎", "title_vi": "Bí ẩn chiếc bánh biến mất",
            "skills_applied": [{"skill": "sw-sitcom-comedy", "how_vi": "setup bánh → payoff bánh mất"}],
            "segments": [
                {"segment_id": "s01", "story_role": "hook", "source_start_seconds": 49.5, "source_end_seconds": 56.0,
                 "dialogue_used": [{"line_id": ids["ケーキがない！"]}],
                 "claims": [{"claim_id": "s01c1", "type": "question", "text_ja": "ケーキ、どこ行った？", "text_vi": "Bánh đâu rồi?"}]},
                {"segment_id": "s02", "story_role": "development", "source_start_seconds": 0.5, "source_end_seconds": 17.0,
                 "claims": [{"claim_id": "s02c1", "type": "event", "text_ja": "始まりは、ママが買ってきたケーキ。", "text_vi": "Mọi chuyện bắt đầu từ chiếc bánh.",
                             "beat_ids": ["b01"], "evidence": ev(sc1, 1.0, 2.5, ids["ケーキ買ってきたよ。"])}]},
                {"segment_id": "s03", "story_role": "conflict", "source_start_seconds": 21.0, "source_end_seconds": 40.0,
                 "claims": [{"claim_id": "s03c1", "type": "quote", "text_ja": "食べちゃダメよ！", "text_vi": "Không được ăn!",
                             "beat_ids": ["b03"], "evidence": ev(sc2, 30.0, 31.5, ids["食べちゃダメよ！"])}]},
                {"segment_id": "s04", "story_role": "climax", "source_start_seconds": 46.0, "source_end_seconds": 60.0,
                 "claims": [{"claim_id": "s04c1", "type": "event", "text_ja": "そして、ケーキは消えた。", "text_vi": "Và chiếc bánh biến mất.",
                             "beat_ids": ["b04"], "evidence": ev(sc3, 50.0, 51.5, ids["ケーキがない！"])}]},
                {"segment_id": "s05", "story_role": "resolution", "source_start_seconds": 60.0, "source_end_seconds": 69.0,
                 "claims": [{"claim_id": "s05c1", "type": "commentary", "text_ja": "犯人は、言うまでもない。", "text_vi": "Thủ phạm thì khỏi nói."}]},
            ]}


def _approve(packet: Path) -> None:
    data = _fix_line_ids(_understanding(packet), packet.parents[1])
    assert acp.import_understanding(packet, json.dumps(data, ensure_ascii=False))["status"] == "OK"
    acp.approve_understanding(packet)


@needs_ffmpeg
def test_script_requires_approved_understanding(packet):
    with pytest.raises(acp.PacketError, match="chưa được bạn duyệt"):
        acp.import_script(packet, json.dumps(_script(packet.parents[1]), ensure_ascii=False))


@needs_ffmpeg
def test_valid_script_writes_checked_cut_list_and_bilingual_md(packet):
    _approve(packet)
    result = acp.import_script(packet, "```json\n" + json.dumps(_script(packet.parents[1]), ensure_ascii=False) + "\n```")
    assert result["status"] == "OK", result["errors"]
    assert 60 <= result["total_seconds"] <= 90
    rows = list(csv.DictReader((packet / acp.CUT_LIST).open(encoding="utf-8-sig")))
    fps = 24000 / 1001
    assert [r["story_role"] for r in rows] == ["hook", "development", "conflict", "climax", "resolution"]
    assert all(int(r["in_frame"]) == round(float(r["source_in_seconds"]) * fps) for r in rows)
    assert all(r["trang_thai"] == "CHO_DUYET" for r in rows)
    assert acp.check_cut_list(packet / acp.CUT_LIST, _index(packet.parents[1])) == []
    md = (packet / "kich_ban.md").read_text(encoding="utf-8")
    assert "ケーキ、どこ行った？" in md and "Bánh đâu rồi?" in md and "chưa render" in md
    assert "食べちゃダメよ！" in md  # source line text inserted by code next to the evidence


@needs_ffmpeg
def test_script_cannot_tell_a_hypothesis_as_fact(packet):
    _approve(packet)
    script = _script(packet.parents[1])
    script["segments"][3]["claims"][0]["beat_ids"] = ["b05"]
    result = acp.import_script(packet, json.dumps(script, ensure_ascii=False))
    assert result["status"] == "FAIL" and any("GIẢ THUYẾT" in e for e in result["errors"])
    assert not (packet / acp.CUT_LIST).exists()


@needs_ffmpeg
@pytest.mark.parametrize(("mutate", "needle"), [
    (lambda s: s["segments"][1]["claims"][0].update(beat_ids=[]), "phải có beat_ids"),
    (lambda s: s["segments"][2]["claims"][0].update(text_ja="食べていいよ！"), "verbatim"),
    (lambda s: s["segments"][4].update(source_end_seconds=62.0), "outside"),
    (lambda s: s["segments"][0]["claims"][0].update(text_ja="ケーキ" * 30), "longer than its cut"),
])
def test_script_errors_are_caught(packet, mutate, needle):
    _approve(packet)
    script = _script(packet.parents[1])
    mutate(script)
    result = acp.import_script(packet, json.dumps(script, ensure_ascii=False))
    assert result["status"] == "FAIL" and any(needle in e for e in result["errors"]), result["errors"]


@needs_ffmpeg
def test_tampered_cut_list_is_detected(packet):
    _approve(packet)
    assert acp.import_script(packet, json.dumps(_script(packet.parents[1]), ensure_ascii=False))["status"] == "OK"
    path = packet / acp.CUT_LIST
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    rows[1]["in_frame"] = str(int(rows[1]["in_frame"]) + 3)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    assert any("frame" in e for e in acp.check_cut_list(path, _index(packet.parents[1])))
