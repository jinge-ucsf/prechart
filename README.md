# PreChart

**The chart drafts the visit note; the ambient conversation cross-examines it; the agent adjudicates the conflicts; the clinician steers — and the note follows.**

PreChart is a pre-charting **verification agent** for the office visit. It drafts a specialty-styled clinical note from the chart *and* the ambient conversation, and — where the two disagree — it doesn't just pick a side. Its founding assumption is what makes it different: **the chart and the conversation are two _fallible_ witnesses** (charts go stale and carry copy-forward/coding errors; patients misremember, use lay terms, and get mis-transcribed), so PreChart trusts neither by default. For each claim it gathers *adjacent, objective* evidence, proposes which account the evidence supports, and surfaces the conflict for the clinician. The clinician can accept the agent's call or **select the truth** themselves — and the note **updates live** to match. Nothing high-stakes is auto-written; the clinician signs off.

![PreChart concept](docs/agentic-loop.svg)

## How it works

**Two fallible witnesses in, gated to the visit.** The chart (FHIR problems, meds, labs, imaging, prior notes) and the ambient transcript are the inputs; a **temporal guard** admits only what was knowable *as of* the visit (a post-procedure report can't leak into a pre-visit note).

**The agent gathers evidence and adjudicates.** For each claim — from *either* side — it runs deterministic tools and reasons over them:

- **`triangulate_chart_evidence`** — the chart's *own* internal signals: medication order/refill recency, related-lab corroboration, duplicate therapy, label-only status.
- **`check_physiologic_markers`** — the **third witness**: the body, which neither the chart nor the patient controls (warfarin→INR, statin→LDL, metformin→A1c). It may back one account, both, or **neither**.

It assigns each claim a state and proposes the likely-correct source. If the evidence *doesn't* resolve it, it returns **`UNRESOLVED`** with `recommended_next_data` — the single datum that would settle it (e.g. *"order INR today"*) — rather than guessing.

**States:** `CONFIRMED · CONTRADICTED · ELABORATED · NEW · UNADDRESSED · UNRESOLVED`

**The clinician steers; the note follows.** Every conflict is a **Room / Chart / Unresolved** control with the agent's hypothesis pre-selected. Override it and the working note regenerates to treat *your* decision as the truth.

> Not a pipeline · not a summarizer (it distrusts its inputs and refuses) · not a black box (the clinician adjudicates, the note stays coherent).

## The interactive workspace (the demo)

```bash
python3 app.py                            # → http://localhost:8000
```

Pick one of the 60 synthetic patients + a specialty, hit **Run PreChart**, and you get a three-column clinician workspace:

- **Left — Transcript**: the ambient conversation (the room).
- **Center — Working note**: the Epic-style note (HPI → Outside Data → A/P), copy-pasteable. **It updates as you adjudicate.**
- **Right — Conflicts & adjudication**: each CONTRADICTED / UNRESOLVED / NEW / ELABORATED finding as a **select-the-truth** control (Room / Chart / Unresolved), the agent's pick badged, with Supporting Evidence. Routine (confirmed / not-discussed) items collapse out of the way.

Selecting a different truth **regenerates the note instantly** — the likely overrides are pre-fetched and cached in the background right after load, so the toggle you click swaps with no wait.

With `ANTHROPIC_API_KEY` set the app runs the live agent; without one it runs a stdlib dry-run so the whole flow is visible offline.

## Quickstart

Runs from a single clone — the synthetic dataset is bundled; the web app + dry-run path are stdlib-only.

```bash
# Fastest live demo: Sonnet, one-shot, warm the hero (warfarin) case for an instant first render
export ANTHROPIC_API_KEY=sk-ant-...       # set this in YOUR shell; never paste keys into code
PRECHART_FAST=1 uv run --with anthropic python app.py --warm 47

# Or plain (Opus quality). Dry-run (no key) works too:
python3 app.py
```

CLI (writes `ledger.html`): `python3 run.py --record-index 47 --specialty gi` (add `--dry-run` for no key).

## Speed & modes

Tuned for a fast demo by default; dial up quality when you want it:

| Env / flag | Effect |
|---|---|
| *(default)* | Opus, extended thinking **off**, **one-shot** adjudication (tools pre-run → one call), note + adjudication in parallel |
| `PRECHART_FAST=1` | Sonnet instead of Opus (faster still) |
| `PRECHART_THINKING=adaptive` | re-enable extended thinking (slower, deeper) |
| `PRECHART_AGENTIC_LOOP=1` | the multi-turn tool-use loop (the model calls tools round-by-round) instead of one-shot |
| `PRECHART_MODEL=<id>` | override the model |
| `app.py --warm 47,20` | pre-run those records into the cache before serving (instant on stage) |

Conflict toggles are made instant by background **prefetch + client-side cache** of the likely overrides.

## Specialty configuration

Built for GI, specialty-agnostic. A specialty config sets the agent's *framing* (what to prioritize) and its high-significance meds/problems; it does **not** change the core reconciliation.

```bash
python3 run.py --specialty gi | cardiology | hepatology
```

Each specialty also carries an **Epic-style note-format spec** in `specialties/notes/<key>.txt` that drives the note (HPI → Outside Data → A/P, in the physician's exact plain-text A/P format). Hepatology uses a transplant-hepatology spec; GI and cardiology are domain adaptations; `_default.txt` is the fallback. Add one by dropping `specialties/<key>.json` (+ optional `specialties/notes/<key>.txt`).

## Evaluation

The reconciliation is *scored*, not just demoed. `data/synthetic-gi.jsonl` ships with **367 labeled discrepancies** across 60 patients (`metadata.planted_discrepancies`): stale-chart errors, patient-misremembering errors, and routine controls.

```bash
python3 eval.py --dry-run                                # keyword baseline (no key)
export ANTHROPIC_API_KEY=sk-ant-... ; python3 eval.py    # the real model classifier
```

Reports overall state accuracy, source-of-error accuracy, **stale-chart catch**, **patient-error catch**, **false-fire on routine controls** (the "don't cry wolf" metric), prediction coverage, and a confusion matrix → `eval-report.md`. The `--dry-run` baseline is a deliberately weak keyword heuristic — the floor the agent beats. Scope caveat: the eval scores reconciliation *judgment* given each labeled `chart_state`/`spoken_truth` pair (slightly easier than extracting them from raw FHIR + transcript, which the live demo does end-to-end).

## Temporal integrity

The office visit is one point on a longer timeline (office → prep → procedure → post-procedure). PreChart runs *at* the office visit, so `dataio.redact_future` drops any artifact dated after it — a post-procedure report, path result, or post-op lab can never leak in. The `as_of` clock is reusable: a downstream agent (e.g. LeftBehind) loads the same record with a later date.

## Safety

Nothing high-stakes (dose, active/inactive medication, allergy, anticoagulation) is ever written automatically — every such change is staged with `requires_signoff`. **The agent proposes, the clinician decides, and the clinician signs.**

## Layout

| File | Role |
|---|---|
| `models.py` | `Proposal` dataclass + the six states |
| `dataio.py` | load record · extract chart items (provenance **tier**) + spoken assertions · **temporal filter** |
| `tools.py` | agent tools — `triangulate_chart_evidence`, `check_physiologic_markers`, `find_chart_item` |
| `adjudicator.py` | the agentic core — one-shot (default) + multi-turn tool-use loop + `--dry-run` heuristic |
| `note.py` | drafts the note from chart **+** transcript, in the specialty's Epic format; honors clinician decisions |
| `config.py` | model / thinking / one-shot vs loop knobs (env-driven) |
| `run.py` | CLI → `ledger.html` |
| `app.py` + `web/index.html` | the interactive workspace — `/api/prechart`, `/api/note` (live note regeneration) |
| `eval.py` | score predictions vs the labeled benchmark |
| `specialty.py` + `specialties/*.json` + `specialties/notes/*.txt` | specialty framing, significance, and Epic note specs |
| `data/` | 60 synthetic GI records + labels (**fully synthetic — no PHI**) |
| `docs/agentic-loop.svg` | the concept diagram above |

## Honest caveats

- **`--dry-run` is a placeholder, not the model.** The agent path is the product.
- **Spoken-assertion extraction is keyword-gated** (a recall gap) — a model extractor is the next upgrade.
- **All data is synthetic.** Planted discrepancies are authored so reconciliation can be measured; real charts are messier.
- **"Doesn't ambient AI already do this?"** Ambient tools pull chart context to *write* the note. PreChart uses the conversation to *adjudicate a possibly-stale chart*, weighs a third objective witness, refuses on high-stakes conflicts, and lets the clinician steer the truth. That wedge — adjudication + refusal + human-in-the-loop — is the difference.

## Suite

Part of the UCSF GI procedure-journey suite: **PreChart** (pre-office) → **FollowThrough** (patient prep) → **NapGuard** (pre-procedure clearance) → **LeftBehind** (post-procedure follow-up). PreChart's reconciled problem/med list is the clean input the downstream agents build on.

*Built for the Abridge × Anthropic × Lightspeed "Future of Agentic AI in Healthcare" hackathon.*
