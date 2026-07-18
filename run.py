#!/usr/bin/env python3
"""PreChart — reconcile the chart against the room and stage a clinician ledger.

    python3 run.py --dry-run                 # tonight: stdlib only, no key
    python3 run.py --record-index 10 --dry-run
    python3 run.py                           # Saturday: real Claude adjudication (needs key)

Writes ledger.html (open in a browser). Deterministic plumbing loads the two
witnesses and renders the ledger; the ADJUDICATION is the agentic core.
"""
import argparse
import html
import os

from dataio import load_record, extract_chart_items, extract_spoken_assertions, DEFAULT_DATASET
from adjudicator import adjudicate
from specialty import load_specialty, available as available_specialties

STATE_COLOR = {"CONTRADICTED": "#c0392b", "UNRESOLVED": "#e08e0b", "ELABORATED": "#2b6cb0",
               "NEW": "#2f855a", "UNADDRESSED": "#718096", "CONFIRMED": "#4a5568"}
ORDER = ["CONTRADICTED", "UNRESOLVED", "ELABORATED", "NEW", "UNADDRESSED", "CONFIRMED"]


def cli(props, rec, dry, spec=None):
    m = rec["metadata"]
    print(f"\nPreChart — {m['visit_title']}  ({m['date'][:10]})")
    if spec:
        print(f"Specialty: {spec['name']}")
    print(f"Mode: {'DRY-RUN (heuristic; not the model)' if dry else 'AGENT (model adjudication)'}")
    tf = rec.get("_temporal_filter", {})
    if tf.get("removed"):
        print(f"Temporal guard: withheld {len(tf['removed'])} future artifact(s) dated after {tf['as_of']} "
              f"(post-procedure data belongs to a later phase)")
    counts = {s: sum(p.state == s for p in props) for s in ORDER}
    print("  " + " · ".join(f"{counts[s]} {s.lower()}" for s in ORDER if counts[s]) + "\n")
    for s in ORDER:
        for p in sorted((p for p in props if p.state == s), key=lambda x: x.topic):
            flag = "  [SIGN-OFF]" if p.requires_signoff else ""
            print(f"[{s:12}] {p.topic[:44]:44} likely:{p.likely_correct:7}{flag}")
            if p.spoken_side:
                print(f"               said : {p.spoken_side[:80]}")
            if p.chart_side:
                print(f"               chart: {p.chart_side[:60]}  ({p.provenance_tier})")
            print(f"               why  : {p.reasoning[:96]}")
            for d in (p.evidence_dossier or [])[:4]:
                print(f"               evid : [{d.get('source')}] {str(d.get('finding'))[:76]}")
            if p.recommended_next_data:
                print(f"               NEXT : {p.recommended_next_data[:86]}")
    print()


def write_html(props, rec, dry, path, spec=None):
    def esc(x):
        return html.escape(str(x if x is not None else ""))
    counts = {s: sum(p.state == s for p in props) for s in ORDER}
    rows = []
    for s in ORDER:
        for p in sorted((p for p in props if p.state == s), key=lambda x: x.topic):
            doss = "".join(f'<li><b>{esc(d.get("source"))}:</b> {esc(d.get("finding"))}</li>'
                           for d in (p.evidence_dossier or []))
            nxt = (f'<div class="next">needs → {esc(p.recommended_next_data)}</div>'
                   if p.recommended_next_data else '')
            rows.append(f"""<div class="row">
<span class="chip" style="background:{STATE_COLOR[s]}">{esc(s)}</span>
<div class="body">
  <div class="topic">{esc(p.topic)} {'<span class="so">sign-off</span>' if p.requires_signoff else ''}</div>
  <div class="cols">
    <div><b>Room:</b> {esc(p.spoken_side) or '<i>not mentioned</i>'}</div>
    <div><b>Chart:</b> {esc(p.chart_side) or '<i>absent</i>'} <span class="tier">{esc(p.provenance_tier)}</span></div>
  </div>
  <div class="verdict">likely correct: <b>{esc(p.likely_correct)}</b> · {esc(p.confidence)} confidence · {esc(p.clinical_significance)} significance → <b>{esc(p.proposed_action)}</b></div>
  <div class="why">{esc(p.reasoning)}</div>
  {f'<ul class="ev">{doss}</ul>' if doss else ''}
  {nxt}
</div></div>""")
    mode = "DRY-RUN heuristic (placeholder — real adjudication runs the model)" if dry else "Agent adjudication"
    doc = f"""<!doctype html><html><head><meta charset="utf-8"><title>PreChart Ledger</title><style>
:root{{color-scheme:light dark}}
body{{font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;color:#1a1a1a;background:#fafafa}}
h1{{margin:0 0 .1rem}} .sub{{color:#666;margin:0 0 1rem}}
.stats{{display:flex;gap:.5rem;flex-wrap:wrap;margin:0 0 1.2rem}}
.stat{{background:#fff;border:1px solid #e5e5e5;border-radius:8px;padding:.4rem .7rem;font-size:.85rem}}
.row{{display:flex;gap:.8rem;background:#fff;border:1px solid #e8e8e8;border-radius:10px;padding:.7rem .9rem;margin:0 0 .7rem}}
.chip{{color:#fff;font-size:.68rem;font-weight:700;padding:.22rem .5rem;border-radius:6px;height:fit-content;white-space:nowrap}}
.body{{flex:1}} .topic{{font-weight:600;margin-bottom:.3rem}}
.so{{background:#fde68a;color:#7a5b00;font-size:.66rem;font-weight:700;padding:.1rem .4rem;border-radius:5px;margin-left:.4rem}}
.cols{{display:grid;grid-template-columns:1fr 1fr;gap:.3rem 1rem;font-size:.88rem;margin-bottom:.3rem}}
.tier{{color:#999;font-size:.75rem}}
.verdict{{font-size:.82rem;color:#333;margin-bottom:.25rem}}
.why{{font-size:.85rem;color:#555;font-style:italic}}
.ev{{margin:.3rem 0 0;padding-left:1.1rem;font-size:.78rem;color:#777}}
.next{{background:#fff7ed;border-left:3px solid #e08e0b;padding:.25rem .55rem;font-size:.8rem;color:#7a5b00;margin-top:.35rem;border-radius:4px}}
.guard{{color:#555;background:#eef4fb;border-left:3px solid #2b6cb0;padding:.3rem .6rem;font-size:.8rem;border-radius:4px;margin:0 0 1rem}}
footer{{color:#999;font-size:.78rem;margin:2rem 0}}
</style></head><body>
<h1>PreChart Ledger</h1>
<p class="sub">{esc(rec['metadata']['visit_title'])}{' · ' + esc(spec['name']) if spec else ''} · reconciling two fallible witnesses — the chart and the room. {esc(mode)}.</p>
{f'<p class="guard">Temporal guard: {len(rec["_temporal_filter"]["removed"])} future-dated artifact(s) withheld (post-procedure data belongs to a later phase).</p>' if rec.get("_temporal_filter", {}).get("removed") else ''}
<div class="stats">{''.join(f'<span class="stat" style="border-left:3px solid {STATE_COLOR[s]}">{counts[s]} {s.lower()}</span>' for s in ORDER if counts[s])}</div>
{''.join(rows)}
<footer>Synthetic data. The agent proposes; the clinician signs off. Nothing high-stakes is written automatically.</footer>
</body></html>"""
    with open(path, "w") as fh:
        fh.write(doc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=DEFAULT_DATASET)
    ap.add_argument("--record-index", type=int, default=47)
    ap.add_argument("--record-id", default=None)
    ap.add_argument("--specialty", default="gi",
                    help=f"visit framing / significance profile ({', '.join(available_specialties())})")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    dry = a.dry_run or not os.environ.get("ANTHROPIC_API_KEY")
    if dry and not a.dry_run:
        print("(no ANTHROPIC_API_KEY found → falling back to --dry-run heuristic)")

    spec = load_specialty(a.specialty)
    rec = load_record(a.dataset, index=a.record_index, record_id=a.record_id)
    chart = extract_chart_items(rec)
    spoken = extract_spoken_assertions(rec)
    props = adjudicate(rec, chart, spoken, dry_run=dry, specialty=spec)

    cli(props, rec, dry, spec)
    out = os.path.join(os.path.dirname(__file__), "ledger.html")
    write_html(props, rec, dry, out, spec)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
