#!/usr/bin/env python3
"""Eval harness — score PreChart's reconciliation against the labeled GI benchmark.

For every planted discrepancy (chart_state vs spoken_truth) it predicts the
expected_state + source_of_error and scores against ground truth. Because the
benchmark labels BOTH chart-errors and patient-errors (and routine controls),
this measures the real claim: does the agent catch a stale chart AND a mistaken
patient, without crying wolf on the routine cases?

    python3 eval.py --dry-run                      # keyword baseline (no key)
    export ANTHROPIC_API_KEY=... ; python3 eval.py # real model classifier

Writes eval-report.md. The headline number is what goes on the slide.
"""
import argparse
import collections
import json
import os

from dataio import redact_future  # same temporal guard PreChart loads through

DATA = os.path.join(os.path.dirname(__file__), "data", "synthetic-gi.jsonl")
STATES = ["CONFIRMED", "NEW", "CONTRADICTED", "UNADDRESSED"]


def load(path):
    return [json.loads(l) for l in open(path)]


def evidence_summary(rec):
    """Compact adjacent-evidence bundle the classifier can reason over (order dates + labs)."""
    rr = rec["encounter_fhir"]["related_resources"]
    meds = [f"{m.get('medicationCodeableConcept', {}).get('text', '?')} "
            f"(ordered {str(m.get('authoredOn', '?'))[:10]})" for m in rr.get("MedicationRequest", [])]
    labs = []
    for o in rr.get("Observation", []):
        vq = o.get("valueQuantity", {})
        nm = o.get("code", {}).get("text") or " ".join(c.get("display", "") for c in o.get("code", {}).get("coding", []))
        if isinstance(vq.get("value"), (int, float)):
            labs.append(f"{nm}={vq['value']} {vq.get('unit', '')}")
    return {"ordered_meds": meds,
            "running_med_list": rec["patient_context"]["longitudinal_summary"].get("medication_labels", []),
            "labs": labs[:24]}


# ---------------- baseline heuristic (dry-run) ---------------- #
def heuristic_classify(d):
    sp = d.get("spoken_truth", "").lower()
    if any(w in sp for w in ("confirm", "matches", "agrees", "same as", "consistent")):
        return "CONFIRMED", "none"
    if any(w in sp for w in ("no longer", "stopped", "discontinued", "actually", "not taking",
                             "never took", "held", "switched")):
        return "CONTRADICTED", "chart"
    if any(w in sp for w in ("also takes", "additionally", "not on the", "missing", "new ",
                             "reports a", "on top of")):
        return "NEW", "chart"
    return "CONFIRMED", "none"


# ---------------- real model classifier (per patient) ---------------- #
def model_classify(rec, discs):
    import anthropic
    client = anthropic.Anthropic()
    sys = ("You reconcile a clinic chart against what the patient said in the room. ASSUME EITHER "
           "CAN BE WRONG — charts go stale, patients misremember. For each item classify:\n"
           " state: CONFIRMED (agree) | CONTRADICTED (disagree, one is wrong) | NEW (said but not "
           "charted, or chart missing it) | UNADDRESSED (charted, not discussed)\n"
           " source: chart | patient | none\n"
           "Use the adjacent evidence (medication order dates, lab values) to decide which account "
           "the objective data supports. Return ONLY a JSON array [{id, state, source}].")
    payload = {"adjacent_evidence": evidence_summary(rec),
               "items": [{"id": d["id"], "chart_state": d["chart_state"], "spoken_truth": d["spoken_truth"]}
                         for d in discs]}
    msg = client.messages.create(model="claude-opus-4-8", max_tokens=16000,
                                 thinking={"type": "adaptive"}, system=sys,
                                 messages=[{"role": "user", "content": json.dumps(payload, indent=1)}])
    if msg.stop_reason == "max_tokens":
        print(f"  ! {rec['id'][:12]}: output truncated (max_tokens) — its items will count as MISSING")
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    dec = json.JSONDecoder()  # scan for the first well-formed array (robust to prose/brackets)
    for i, ch in enumerate(text):
        if ch != "[":
            continue
        try:
            arr, _ = dec.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(arr, list):
            try:
                return {p["id"]: (p.get("state"), p.get("source")) for p in arr}
            except (KeyError, TypeError):
                return {}
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=DATA)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()
    dry = a.dry_run or not os.environ.get("ANTHROPIC_API_KEY")
    if dry and not a.dry_run:
        print("(no ANTHROPIC_API_KEY → baseline heuristic; export a key for the real number)")

    recs = load(a.dataset)[: a.limit] if a.limit else load(a.dataset)

    conf = collections.Counter()
    n = correct = src_ok = covered = 0
    chart_caught = chart_tot = pt_caught = pt_tot = ctrl_tot = ctrl_falsefire = 0
    leaks = collections.Counter()  # future-dated artifacts the temporal guard withheld

    for rec in recs:
        rec = redact_future(rec)  # no post-procedure artifact reaches the classifier's evidence
        for r in rec["_temporal_filter"]["removed"]:
            leaks[r["type"]] += 1
        discs = rec["metadata"].get("planted_discrepancies", [])
        preds = ({d["id"]: heuristic_classify(d) for d in discs} if dry
                 else model_classify(rec, discs))
        for d in discs:
            exp, esrc = d["expected_state"], d["source_of_error"]
            # A missing prediction counts as WRONG (sentinel), never the majority class —
            # otherwise dropped/partial model output silently inflates accuracy & floors false-fire.
            pstate, psrc = preds.get(d["id"], ("MISSING", "MISSING"))
            n += 1
            covered += (d["id"] in preds)
            conf[(exp, pstate)] += 1
            correct += (pstate == exp)
            src_ok += (psrc == esrc)
            # "catching" an error means BOTH the right state AND naming the right witness.
            if esrc == "chart":
                chart_tot += 1; chart_caught += (pstate == exp and psrc == esrc)
            elif esrc == "patient":
                pt_tot += 1; pt_caught += (pstate == exp and psrc == esrc)
            if exp == "CONFIRMED" and esrc == "none":
                ctrl_tot += 1; ctrl_falsefire += (pstate in ("CONTRADICTED", "NEW"))

    def pct(a_, b_):
        return f"{a_}/{b_} = {a_ / max(b_, 1):.0%}"

    mode = "DRY-RUN heuristic (baseline — not the model)" if dry else "MODEL (claude-opus-4-8)"
    lines = [
        f"# PreChart reconciliation eval — {mode}",
        f"\n**{len(recs)} patients · {n} labeled discrepancies**\n",
        f"| metric | result |",
        f"|---|---|",
        f"| overall state accuracy | **{pct(correct, n)}** |",
        f"| source-of-error accuracy | {pct(src_ok, n)} |",
        f"| chart-error catch (right state + blames the chart) | {pct(chart_caught, chart_tot)} |",
        f"| patient-error catch (right state + blames the patient) | {pct(pt_caught, pt_tot)} |",
        f"| false-fire on routine controls | {pct(ctrl_falsefire, ctrl_tot)} |",
        f"| prediction coverage | {pct(covered, n)} |",
        f"\n### Confusion (expected → predicted)\n",
        "| expected \\ predicted | " + " | ".join(STATES) + " |",
        "|" + "---|" * (len(STATES) + 1),
    ]
    for e in STATES:
        lines.append(f"| {e} | " + " | ".join(str(conf[(e, p)]) for p in STATES) + " |")

    total_leaks = sum(leaks.values())
    lines.append(f"\n### Temporal guard\n")
    if total_leaks:
        lines.append(f"Withheld **{total_leaks}** future-dated artifact(s) before scoring — "
                     + ", ".join(f"{v} {k}" for k, v in leaks.most_common()) + ". "
                     "None reached the classifier's evidence (no post-procedure leakage into the office visit).")
    else:
        lines.append("0 future-dated artifacts — dataset is already temporally clean as-of each "
                     "office visit; the guard is active and will withhold any that appear.")

    report = "\n".join(lines)
    print("\n" + report + "\n")
    out = os.path.join(os.path.dirname(__file__), "eval-report.md")
    with open(out, "w") as fh:
        fh.write(report + "\n")
    print(f"Wrote {out}")
    print(f"\nHEADLINE [{mode}]: caught {pct(chart_caught, chart_tot)} of stale-chart errors and "
          f"{pct(pt_caught, pt_tot)} of patient errors, false-firing on {pct(ctrl_falsefire, ctrl_tot)} controls.")
    if covered < n:
        print(f"WARNING: only {pct(covered, n)} of discrepancies were classified — the rest counted "
              f"as MISSING (wrong). Fix parsing / raise max_tokens before trusting this number.")


if __name__ == "__main__":
    main()
