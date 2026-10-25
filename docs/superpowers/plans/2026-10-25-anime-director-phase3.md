# Phase 3 — AI understands the episode, then writes the review — DESIGN (awaiting model/budget decision)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Do not start Tasks 2–5
> before the user has picked a provider/model and a budget cap.

**Goal:** From a Phase 2 run folder (`anime_source_index.json`, keyframes, dialogue, loudness), build a
verified, timestamped *story map* by multimodal reasoning (Tier 1), then write a 60–90 s Japanese review with
commentary (+ Vietnamese), cut list and evidence (Tier 2), validated by `lib/anime_recap.py`.

**Done (Task 1, committed):** `lib/anime_vision_providers.py` — `OllamaProvider` (local, free),
`AnthropicProvider` (official SDK, `claude-opus-5-5` default, structured outputs, refusal fallback, cost from
usage), `FakeProvider` (tests; runs labelled `fake`), `prepare_image`, `estimate_cost`. 11 tests, no network.

## Tier 1 — understanding and plot reasoning (`lib/anime_story_reasoner.py`)
1. **Scene pass, in time order, with memory.** Per scene: 3–5 keyframes + the scene's dialogue (ids, conf,
   flags) + audio cues (loudness_db / excite / silences) + a running *story memory* (summary so far, each
   character's current goal/desire/obstacle, open setups awaiting payoff). Output keeps three kinds apart:
   `observations` (directly visible/heard; each cites keyframe or line ids), `inferences` (reasoned; each cites
   evidence ids + confidence), `hypotheses` (unverified; never used as facts). Also per character: goal, desire,
   obstacle, mistake, reaction; and speaker attribution / suspected-mishearing / fragment flags for lines (ASR
   text is never rewritten).
2. **Ask-for-more-evidence loop.** The model may return `evidence_requests` (scene, time range, reason). Code
   extracts extra frames there (a dense strip: e.g. 6 frames over 2 s, tiled into one contact-sheet image) and
   re-asks once for that scene. Capped per scene and by the run's budget.
3. **Episode pass.** Over the whole scene ledger: events as `cause → action → reaction → consequence`, conflicts,
   misunderstandings, escalation steps, reversals, gags (setup ref → payoff ref, *mechanism*: incongruity,
   escalation, dramatic irony, character flaw, callback…), emotional beats, and *why each is funny / moving /
   gripping*, with evidence. Output `story_map.json` + `story_map.md`.
4. **Verification pass (code).** Every observation/inference must cite existing scene/keyframe/line ids within
   time bounds; uncited items are demoted to hypotheses; hypotheses are excluded from Tier 2 input.

## Tier 2 — the screenwriter (`lib/anime_director.py`)
1. **Select** beats by dramatic value (clear want + obstacle, stakes, comic mechanism, available payoff, visual
   clarity, causal necessity) — loudness is only a hint. Choose hook, setup, conflict/escalation, climax, payoff,
   ending; explain each choice.
2. **Write** Japanese narration that comments and explains (why it is funny/moving), not a list of actions;
   claims typed event/quote/commentary/question with evidence; Vietnamese translation.
3. **Assemble (code):** frames, timeline, durations (`estimate_ja_speech_seconds`), dialogue_used text from the
   index; Phase 1 validator; ≤ 2 repair rounds with the validator's errors.
4. **Editor pass:** a second call grades hook strength, causal coherence, commentary-vs-listing, grounding
   (rubric 1–5) and requests one rewrite below threshold.
5. **Outputs:** `anime_recap_plan.json`, `kich_ban_review.md` (JA + VI + evidence), `cut_list.csv`,
   `director_report.md` (provider, model, real token usage and cost, checks, PENDING human review).

Craft rules come from sw-sitcom-comedy, sw-japanese-screenwriting, sw-scene-craft, sw-character-conflict,
sw-story-structure (distilled into `lib/anime_director_prompts.py`; the runtime model does not load Claude Code
skills itself).

## Quality verification
- Deterministic: evidence ids/time bounds, hypotheses excluded, Phase 1 validator, quotes only from
  non-low-confidence lines.
- Human gold check: `kiem_tra_story_map.csv` (per event / per inference: DUNG / SAI / THIEU_BANG_CHUNG) and
  `kiem_tra_kich_ban.csv` (per claim + rubric) → scored like Phase 2.
- Pilot on the 45 s clip first; A/B providers on the same episode before choosing one.

## Global Constraints
- Phase 1–2 behaviour unchanged. No anime footage or legacy code in git.
- Never claim understanding without a real vision run; provenance records provider/model; `fake` runs labelled.
- Paid API only with explicit provider choice + `--max-cost-usd` cap + confirmation.
