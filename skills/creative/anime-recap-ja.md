# Anime Recap (Japanese) — Creative Skill

> Layer 2 skill. Turns one anime episode (≈8 min, no subtitles, original audio
> and picture only) into a **60–90 s Japanese review script with a
> frame-accurate cut list**. Built for Shin-chan-style family comedy; works for
> any episodic anime.
>
> Artifacts: `schemas/artifacts/anime_source_index.schema.json` (what the
> episode contains) → `schemas/artifacts/anime_recap_plan.schema.json` (the
> review). Validator: `python -m lib.anime_recap --plan <plan.json> --index <index.json>`.
> Fixture example: `tests/fixtures/anime_recap/` (mock data).

## When to Use

- The user supplies an anime episode file and wants a short review / recap /
  解説 video script in Japanese.
- A Douyin / TikTok / Shorts recap is supplied **as a style reference** — analyse
  it with `skills/meta/video-reference-analyst.md` (`video_analyzer`) for hook
  length, cuts per minute and caption style only. Never copy its wording or its
  claims about the episode.

Production still goes through a pipeline (AGENT_GUIDE Rule Zero). The closest
fit is `hybrid` (source footage is the anchor medium); this skill is the
creative rulebook its script / scene_plan / edit stages follow.

## Hard Rules

1. **Never claim an analysis that did not run.** An `anime_source_index` with
   `provenance.kind: "analyzed"` may only be written after `scene_detect`,
   `frame_sampler` and `transcriber` actually ran on the user's file and you
   looked at the keyframes. Anything else is `provenance.kind: "mock"`, and you
   say so to the user. The validator reports `source_is_mock`; the CLI prints
   `MOCK SOURCE`.
2. **No fabricated events.** Every sentence of narration is a *claim*.
   `event` and `quote` claims must cite evidence (scene_id, time range,
   optional line_id). A missing citation is `claim_without_evidence`; an extra
   sentence that is not a claim is `narration_claims_mismatch`.
3. **Quotes are verbatim.** A `quote` claim must cite a dialogue `line_id` and
   its text must appear in that line (`quote_not_in_source`). Paraphrase is an
   `event` claim, not a quote.
4. **Narrator voice ≠ fact.** `commentary` (「これは苦しい言い訳だ」) and
   `question` (「いったい何があったのか？」) need no evidence, therefore they
   must not introduce new facts, motives, off-screen events or future plot.
   Motive words (〜したかった, 〜のせいで) are facts — write them as `event`
   with evidence, or drop them.
5. **ASR uncertainty is not truth.** Lines with low `asr_confidence`, unknown
   `speaker`, or that you could not hear clearly are not quoted. Cite the visual
   instead, or leave the beat out.
6. **Story structure serves selection, not invention.** The screenwriting skills
   below decide *which real moments to pick, in what order, with what rhythm* —
   never what happened.

## Workflow

### 1. Build the source index (real analysis only)

`lib/anime_source_indexer.py` runs the tools below in order and writes a
Phase 1–compatible index plus a review packet:

```bash
python -m lib.anime_source_indexer build --video <episode.mp4> \
    --out projects/<id>/artifacts/source --model-size small   # or large-v3 for best Japanese
# → anime_source_index.json, keyframes/<scene>_kfNN.jpg, review_packet.md,
#   visual_annotations.template.json
```

- Videos taller than 360 px get an audio-less proxy with identical frame
  timing for scene detection; keyframes come from the original.
- Scene cuts are snapped to the exact stream fps (24000/1001 stays 23.976024).
  A single scene for a long video raises `scene_detect_degenerate` — inspect
  before trusting it.
- Without PySceneDetect the ffmpeg fallback is luma-only, so an RGB `scdet`
  pass is unioned in (cuts between equal-brightness colours). With
  PySceneDetect the anime threshold 22 is used.
- ASR runs with `initial_prompt` 「以下は日本語のアニメの会話です。」 and beam 5;
  `small`/`base`/`tiny` raise `asr_model_small` (use `large-v3` for real
  episodes). Segments with `no_speech_prob` > 0.6 get `likely_no_speech`.
- Every line gets `loudness_db` and a heuristic `excite` (0–3: louder than the
  episode median, ！？/ー stretches, short) — a hint for picking hook/climax
  candidates, never evidence that something happened.
- Dialogue is split across cuts by word timestamps. Lines with low
  `asr_confidence` or `asr_flags: ["known_hallucination"]` (「ご視聴ありがとうございました」…)
  are `low_confidence: true` — never quote them unverified.
- No faster-whisper and no `--transcript` → hard error. The builder never
  invents dialogue. If huggingface.co is unreachable, run the transcriber
  elsewhere and pass its JSON with `--transcript` (`transcript_origin: "external_file"`).
- Every scene starts `visual_review: "pending"` with an empty
  `visual_summary`. Then do the visual pass: open each keyframe listed in
  `review_packet.md`, fill the template (visual_summary, characters, actions,
  reactions; per line speaker and `text_vi`), and merge:

```bash
python -m lib.anime_source_indexer annotate --index .../anime_source_index.json \
    --annotations .../visual_annotations.json
```

  Unknown scene / line / character ids reject the whole file. Write the recap
  only from scenes whose `visual_review` is `done`.

| Need | Tool (existing) | Notes |
|---|---|---|
| Scene boundaries | `scene_detect` (`method: content`) | Anime holds and pans can under-split; sample long scenes |
| Keyframes per scene | `frame_sampler` (`strategy: scene_guided`) | Look at them; write `visual_summary`, `characters`, `actions` |
| Japanese dialogue + timestamps | `transcriber` (`language: "ja"`, `model_size: "large-v3"`) | Word timestamps; split into lines; add `text_vi` translation |
| Loud moments (gags, shouts) | `audio_energy` | Candidate climax/hook signal, not proof |

Speaker names are an observation you make from the frames (mouth movement,
who is on screen). If unsure, `speaker: null`. Character recognition is not
automated in OpenMontage today.

### 2. Mine beats

Score each scene for `hook` / `development` / `conflict` / `climax` /
`resolution` potential using: a visible value turn (sw-scene-craft), a gag
payoff (sw-sitcom-comedy), a decision or confession (sw-story-structure),
an `audio_energy` peak. Keep a shortlist of ~10 candidate moments with exact
time ranges before writing a word.

### 3. Structure the 60–90 s review

| Role | Share (75 s) | Job | Source craft |
|---|---|---|---|
| `hook` | 4–8 s | Cold open: show the most striking *real* moment (often the climax image or a scream) + one `question` claim. Must be segment 1. | sw-japanese-screenwriting「情況説明を早く終わらせる」; sw-story-structure (start near the climax) |
| `development` | 20–30 s | 起・承: who wants what, the rule/prohibition that will be broken. Plant every seed the payoff needs. | sw-sitcom-comedy (setup has two parts: topic + complication; 種を蒔いたら刈り取る); sw-character-conflict (desire vs obstacle, 渇望 vs 誤った信念) |
| `conflict` | 15–25 s | 承→転: escalation — lie becomes a bigger lie, the obstacle grows. Each segment must raise the stakes, not repeat them. | sw-sitcom-comedy (Big Lie → Bigger Lies, escalation); sw-scene-craft (each beat stacks toward a turn) |
| `climax` | 8–15 s | 転: the decisive action / confession / reversal, shown with original audio where possible. First climax must come after first conflict. | sw-story-structure (crisis = a choice, climax = irreversible turn); sw-character-conflict (the character wins or loses by their own act) |
| `resolution` | 8–15 s | 合: back to daily life; a callback to the hook image or the opening prohibition. After the climax. | sw-japanese-screenwriting (起承転結, 日常の反復と偏差); sw-sitcom-comedy (callback, button) |

The validator enforces presence and order (`missing_story_role`,
`first_segment_not_hook`, `story_order`) and total length
(`total_duration_out_of_range`, from `target_duration_seconds`).

### 4. Write the Japanese narration

- Spoken, short sentences; 常体 (〜する／〜した) is the default recap voice;
  keep one register for the whole video.
- One claim = one sentence or quote. `narration.text_ja` is exactly the
  concatenation of the segment's claims.
- Length: plan with `estimate_ja_speech_seconds(text)` (≈6.5 spoken
  chars/s + pauses; Japanese has no word spaces, so never count words).
  Ceiling is 9 chars/s (`speech_rate_too_fast`); narration may not outrun its
  cut by more than 0.25 s (`narration_exceeds_cut`).
- Mark `duration_source: "estimate"` now; after TTS, replace with the measured
  file length and `"measured"`, then re-validate and re-time cuts.
- Let the original line play when it *is* the payoff (quote it with a `quote`
  claim or leave it in `dialogue_used`, whose `text_ja` must equal the source).
- Comedy lines: the punch word goes last (sw-sitcom-comedy, last things
  last); one `commentary` beat per gag at most, never explaining the joke.
- Add `text_vi` for the reviewer; it is not spoken.

### 5. Build the cut list

- `source_start/end_seconds` must sit inside the union of the segment's
  `scene_ids` (`outside_scene`) and inside the video (`out_of_video`).
- `cut.in_frame = round(source_start_seconds × fps)`, same for `out_frame`
  (`frame_mismatch`). Use the index's fps exactly (e.g. 23.976).
- Output timeline is gapless from 0: each segment's
  `timeline_end - timeline_start` equals its cut length (`timeline_gap`).
- Prefer cuts that start on a visual action and end after a reaction
  (sw-scene-craft: enter late, leave early).

### 6. Validate before showing the user

```bash
python -m lib.anime_recap --plan projects/<id>/artifacts/anime_recap_plan.json \
    --index projects/<id>/artifacts/anime_source_index.json
```

Exit code 0 = valid. Fix every error; read every warning
(`dialogue_outside_cut` means a `dialogue_used` line is not audible in its
cut). Then run the reviewer pass (`skills/meta/reviewer.md`) with the checklist
below. Present the script with `text_vi` and timestamps to the user for
approval.

## Review Checklist (things the validator cannot see)

- Does any `commentary` / `question` smuggle in a fact, motive or spoiler not
  on screen?
- Is each `event` claim's wording supported by what the cited frames/lines
  actually show (not just by a scene that exists)?
- Do translations (`text_vi`) match the Japanese meaning?
- Hook: does a viewer with no context understand the question in ≤ 8 s?
- Each `conflict` segment escalates rather than repeats?
- Is the climax the character's own act (no outside rescue)?
- Does the resolution call back to the hook or the opening rule?
- Total quoted footage per clip short and transformative (commentary, not a
  re-upload) — copyright risk for the user.

## Not Covered Yet (later phases)

- Automatic anime character recognition / speaker attribution.
- Japanese TTS (no local Japanese voice in the tool registry); measured
  durations and automatic re-timing of cuts to voice length.
- Export adapters for specific cutting tools beyond this plan JSON / `edit_decisions`.
