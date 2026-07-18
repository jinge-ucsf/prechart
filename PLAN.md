# PreChart — build plan

**One line:** the chart pre-writes the visit; the ambient conversation verifies it —
and where the two disagree, an agent adjudicates *two fallible witnesses* and stages
the delta for the clinician, never overwriting the chart on its own.

## Why it's agentic (say this to judges)
> "PreChart is a **verification agent**, not a note generator. Neither the chart nor
> the conversation is ground truth — charts go stale, patients misremember — so for
> each item the model resolves lay language to coded meds, weighs which witness the
> evidence favors (using the chart's own refill gaps, related labs, duplicates), and
> **refuses** to act on anything high-stakes. The note is just the receipt."

The adjudication is model-driven (entity resolution, negation/adherence/tense,
conflict-vs-context, source-reliability weighting) — a rule/diff cannot do it.

## The six states
`CONFIRMED · CONTRADICTED · ELABORATED · NEW · UNADDRESSED · UNRESOLVED`
UNRESOLVED = genuine conflict where neither witness is clearly right → surface, don't guess.

## Files
- `models.py` — `Proposal` + states + source enum
- `dataio.py` — load record; extract chart items (with provenance **tier**) + spoken assertions
- `tools.py` — agent tools; **`triangulate_chart_evidence`** is the star (order recency, related-lab corroboration, duplicate therapy, label-only status)
- `adjudicator.py` — the agentic core: Claude tool-use loop (real path) + `--dry-run` heuristic (tonight)
- `run.py` — orchestrate → CLI + `ledger.html`

## Run
```bash
python3 run.py --record-index 10 --dry-run     # tonight: stdlib only, no key
uv add anthropic && export ANTHROPIC_API_KEY=…  # Saturday
python3 run.py --record-index 10               # real agent adjudication
```

## Saturday build order (reuse this scaffold; star exists early)
1. **11:00** — wire the live agent path: `uv add anthropic`, drop in the provided key, run on record 10, confirm the tool-use loop calls `triangulate_chart_evidence` and returns proposals. (The loop + schema + system prompt are already written.)
2. **11:45** — harden output: switch JSON-in-final-message → strict structured output (`output_config.format`); it's the one fragile seam.
3. **12:30** — author **ONE clean CONTRADICTED case** (a stopped/dose-changed med) — the gold notes pre-reconcile the obvious ones, so the hero contradiction is synthetic. Say so on stage.
4. **1:30** — ledger UI: provenance chips that deep-link to the FHIR resource id / transcript turn; one-tap accept/reject; note-as-receipt render.
5. **2:30** — the **pre-visit oracle** flow: run chart-only, reveal the audio, watch states flip; land the CONTRADICTED + one UNADDRESSED + the "water pill" lay-resolution beat.
6. **3:30** — generalization proof: run the same engine live on a *provided* general-exam record (defeats "you staged the data"). Rehearse.

## Demo (3 min)
1. Load a real provided record; generate the pre-chart **chart-only** (before the audio).
2. Play the audio → lines flip: several CONFIRMED, "water pill" resolved to the coded diuretic, an UNADDRESSED cardiac problem surfaces.
3. The CONTRADICTED beat: "patient says they stopped the blood thinner 3 months ago; chart still lists it active — and the last order was 9 months ago. I won't change it. You decide." (refusal = credibility)
4. Show `triangulate_chart_evidence` evidence behind that call (refill gap + no recent INR).
5. Sign-off → the reconciled note assembles as the receipt.

## Honest TODOs / risks
- **Dry-run is a placeholder**, not the model — do not demo it; the agent path is the product.
- **CONTRADICTED needs an authored case** (real data pre-reconciles) — be transparent.
- **Spoken-assertion extraction** is keyword-gated (recall gap) — upgrade to a model extractor if time.
- **"Doesn't Abridge do this?"** — answer: they pull chart context to *write* the note; nobody uses the conversation to *adjudicate a stale chart* and refuse. Two-tier provenance + refusal is the wedge.
- **Safety:** never auto-write dose/active-med/allergy/anticoagulation changes — always `requires_signoff`.

## Suite role
PreChart's reconciled problem/med list is the clean input the downstream products
(FollowThrough, NapGuard) build on — it's the backbone of the UCSF GI portfolio.
