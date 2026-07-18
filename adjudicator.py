"""The agentic core: a tool-using loop where the MODEL adjudicates the two
witnesses. This is deliberately NOT a deterministic diff — the reconciliation
(entity resolution, negation/adherence/tense, conflict-vs-context, and weighing
which fallible source is more likely right) is judgment the model owns.

Runtime:
  --dry-run  : stdlib-only heuristic so the skeleton runs tonight with no key.
  (default)  : real Claude tool-use loop; needs ANTHROPIC_API_KEY + `uv add anthropic`.
"""
import json

from tools import (TOOL_SCHEMAS, triangulate_chart_evidence,
                   check_physiologic_markers, find_chart_item)
from models import Proposal

MODEL = "claude-opus-4-8"

SYSTEM = """You are PreChart's evidence-gathering agent for a clinic visit.

You are given two accounts of the patient — the CHART (coded FHIR + a running
problem/med list) and what was SAID in the room (ambient transcript). ASSUME BOTH
CAN BE WRONG: charts go stale and carry copy-forward and coding errors; patients
misremember, use lay terms ("water pill"), and get mis-transcribed. Do NOT default
to picking a winner between them.

Your job is to INVESTIGATE. For each claim that matters — from either source —
treat it as a hypothesis and go find ADDITIONAL / ADJACENT data that corroborates,
refutes, or contextualizes it, then assemble a decision-ready package for the
clinician. Prefer the most OBJECTIVE evidence available:
  - `check_physiologic_markers` — the body is a third witness the chart and the
    patient don't control (INR for warfarin, LDL for a statin, A1c for metformin).
    Read it against BOTH claims; it may support one, both, or NEITHER.
  - `triangulate_chart_evidence` — order/refill recency, related labs, duplicate
    therapy, label-only status.
Call these before you decide anything about a medication or problem, and record
what you found in evidence_dossier.

Then:
  - If the adjacent evidence resolves it, state which account it supports AND WHY,
    citing the dossier.
  - If it does NOT resolve it — or the evidence contradicts BOTH accounts — set
    state UNRESOLVED, likely_correct "unknown", and put in recommended_next_data
    the single datum that would settle it (e.g., "order INR today", "confirm fill
    history with pharmacy", "repeat a fasting lipid panel"). Do NOT guess.

Distinguish a real contradiction from CONTEXT (home vs clinic BP is not a conflict).

States: CONFIRMED, CONTRADICTED, ELABORATED, NEW, UNADDRESSED, UNRESOLVED.
likely_correct: chart | patient | neither | unknown.

SAFETY: you propose, the clinician decides. Never auto-write a high-stakes change
(dose, active/inactive med, allergy, anticoagulation): requires_signoff=true and
proposed_action = update_chart / add_to_chart / inactivate / flag_allergy / clarify.

Return ONLY a JSON array of proposals, each:
{topic, kind, chart_side, chart_resource_id, provenance_tier, spoken_side,
 spoken_span, state, likely_correct, confidence, clinical_significance, reasoning,
 corroborating_evidence:[...], evidence_dossier:[{source,finding,leans}],
 recommended_next_data, proposed_action, requires_signoff}
Set chart_resource_id only when provenance_tier == "resource-linked" (never invent ids)."""


def adjudicate(rec, chart_items, spoken, dry_run=False, specialty=None, trace=None):
    """Reconcile the two witnesses into a list of Proposals.

    trace: optional list; if given, each tool invocation (the agent's investigation
    steps) is appended as {tool, target, result} so a UI can show the loop working."""
    if dry_run:
        return _dry_run(rec, chart_items, spoken, specialty, trace)
    return _run_agent(rec, chart_items, spoken, specialty, trace)


def _specialty_framing(specialty):
    """A short block appended to SYSTEM so the same engine prioritizes for THIS visit."""
    if not specialty:
        return ""
    flags = ", ".join(specialty.get("high_significance_flags", [])) or "none specified"
    return (f"\n\nSPECIALTY CONTEXT — you are pre-charting for {specialty.get('name', 'this')}: "
            f"{specialty.get('visit_framing', '')}\nTreat these as HIGH clinical significance "
            f"when present: {flags}. This changes what you prioritize and flag for sign-off, "
            f"not how you reconcile.")


# --------------------------------------------------------------------------- #
# Real agentic path: Claude tool-use loop
# --------------------------------------------------------------------------- #
def _run_agent(rec, chart_items, spoken, specialty=None, trace=None):
    import anthropic  # lazy import: only needed for the live path
    client = anthropic.Anthropic()

    system = SYSTEM + _specialty_framing(specialty)
    payload = {
        "chart_items": [{k: v for k, v in it.items() if k != "_raw"} for it in chart_items],
        "spoken_assertions": spoken,
    }
    messages = [{"role": "user", "content":
                 "Reconcile these two witnesses and return the JSON array of proposals.\n\n"
                 + json.dumps(payload, indent=2)}]

    for _ in range(12):  # bounded tool-use loop
        resp = client.messages.create(
            model=MODEL, max_tokens=16000,
            thinking={"type": "adaptive"},
            system=system, tools=TOOL_SCHEMAS, messages=messages,
        )
        if resp.stop_reason == "max_tokens":
            raise RuntimeError("adjudication truncated (max_tokens) — raise max_tokens or split the chart")
        if resp.stop_reason != "tool_use":
            return _parse(_final_text(resp))
        messages.append({"role": "assistant", "content": resp.content})
        dispatch = {"triangulate_chart_evidence": triangulate_chart_evidence,
                    "check_physiologic_markers": check_physiologic_markers}
        results = []
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name in dispatch:
                label = block.input.get("chart_item_label", "")
                item = next((it for it in chart_items if it["label"] == label),
                            {"label": label, "kind": "medication", "tier": "chart-label",
                             "resource_id": None})
                out = dispatch[block.name](item, rec)
                if trace is not None:
                    trace.append({"tool": block.name, "target": label or "(unspecified)",
                                  "result": _trace_summary(block.name, out)})
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps(out)})
        messages.append({"role": "user", "content": results})

    # loop exhausted while still investigating: force ONE final answer with tools disabled,
    # and fail LOUDLY rather than silently returning an empty ledger.
    final = client.messages.create(model=MODEL, max_tokens=16000, thinking={"type": "adaptive"},
                                   system=system, messages=messages)
    props = _parse(_final_text(final))
    if not props:
        raise RuntimeError("agent produced no proposal array after 12 tool rounds")
    return props


def _final_text(resp):
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


def _parse(text):
    """Extract the first well-formed JSON array, tolerant of prose/brackets around it.
    A greedy [.*] regex over-captures when the model emits a stray bracket in prose;
    scanning each '[' with raw_decode isolates the real array instead."""
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "[":
            continue
        try:
            raw, _ = dec.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(raw, list):
            return [_coerce(r) for r in raw]
    return []


def _coerce(r):
    fields = Proposal.__dataclass_fields__
    kwargs = {f: r.get(f) for f in fields}
    kwargs.setdefault("corroborating_evidence", r.get("corroborating_evidence") or [])
    if kwargs.get("corroborating_evidence") is None:
        kwargs["corroborating_evidence"] = []
    if kwargs.get("evidence_dossier") is None:
        kwargs["evidence_dossier"] = []
    for req in ("topic", "kind", "provenance_tier", "state", "likely_correct",
                "confidence", "clinical_significance", "reasoning", "proposed_action"):
        kwargs[req] = kwargs.get(req) or ("unknown" if req in ("likely_correct",) else "")
    kwargs["requires_signoff"] = bool(r.get("requires_signoff", True))
    return Proposal(**kwargs)


# --------------------------------------------------------------------------- #
# Dry-run heuristic: NOT the product — just enough to render the skeleton tonight
# --------------------------------------------------------------------------- #
HIGH_STAKES = ("dose", "mg", "warfarin", "clopidogrel", "apixaban", "aspirin",
               "insulin", "allergy")


def _trace_summary(tool, out):
    """One-line summary of a tool result for the investigation trace."""
    if not isinstance(out, dict):
        return str(out)[:120]
    if tool == "check_physiologic_markers":
        if out.get("value") is not None:
            return f"{out.get('marker')} = {out.get('value')} {out.get('unit', '')} → {out.get('read', '')}".strip()
        return out.get("read") or out.get("note") or "no marker available"
    sig = out.get("signals") or []
    return "; ".join(sig) if sig else (out.get("staleness") or "no internal signals")


def _build_dossier(ev, phys):
    d = [{"source": "chart-internal", "finding": sig,
          "leans": "chart-may-be-stale" if ev.get("staleness") in ("no-recent-order", "no-order-on-file")
                    else "supports-chart"}
         for sig in ev.get("signals", [])]
    if phys and phys.get("read") not in (None, "no-physiologic-marker-for-this-item"):
        d.append({"source": "physiologic",
                  "finding": f"{phys.get('marker')}={phys.get('value', '?')} → {phys['read']}",
                  "leans": "objective"})
    return d


def _dry_run(rec, chart_items, spoken, specialty=None, trace=None):
    flags = tuple(HIGH_STAKES) + tuple(specialty.get("high_significance_flags", [])) if specialty else HIGH_STAKES

    def _sig(label):
        return "high" if any(h in label.lower() for h in flags) else "medium"

    proposals, mentioned = [], set()
    for s in spoken:
        low = s["quote"].lower()
        for it in find_chart_item(s["quote"], chart_items):
            mentioned.add(it["label"])
            ev = triangulate_chart_evidence(it, rec)
            phys = check_physiologic_markers(it, rec) if it["kind"] == "medication" else {}
            if trace is not None:
                trace.append({"tool": "triangulate_chart_evidence", "target": it["label"],
                              "result": _trace_summary("triangulate_chart_evidence", ev)})
                if phys:
                    trace.append({"tool": "check_physiologic_markers", "target": it["label"],
                                  "result": _trace_summary("check_physiologic_markers", phys)})
            dossier = _build_dossier(ev, phys)
            nxt = None
            if it["kind"] == "medication" and any(w in low for w in ("stopped", "quit", "don't take", "no longer")):
                pr = phys.get("read", "")
                if "consistent with active use" in pr:
                    state, who, action = "UNRESOLVED", "unknown", "clarify"
                    reason = ("patient reports stopping it, but the objective marker says it's on board — "
                              "the two accounts and the body disagree")
                    nxt = f"repeat {phys.get('marker')} and confirm fill history with pharmacy"
                else:
                    state, who, action = "CONTRADICTED", "patient", "inactivate"
                    reason = "patient reports discontinuation; " + (
                        "no recent order on file" if ev["staleness"] in ("no-recent-order", "no-order-on-file")
                        else "chart still lists it active")
                    nxt = phys.get("note")
            elif any(w in low for w in ("side effect", "cough", "rash", "made me", "sick")):
                state, who, reason, action = "ELABORATED", "patient", "patient reports a reaction/adherence detail", "clarify"
            else:
                state, who, reason, action = "CONFIRMED", "chart", "spoken mention corroborates the chart", "confirm"
            proposals.append(Proposal(
                topic=f"{it['kind'].title()}: {it['label']}", kind=it["kind"],
                chart_side=it["label"], chart_resource_id=it.get("resource_id"),
                provenance_tier=it["tier"], spoken_side=s["quote"][:120], spoken_span=s["quote"][:120],
                state=state, likely_correct=who, confidence="low",
                clinical_significance=_sig(it["label"]),
                reasoning="[dry-run heuristic — real adjudication runs the model] " + reason,
                corroborating_evidence=ev.get("signals", []), evidence_dossier=dossier,
                recommended_next_data=nxt, proposed_action=action, requires_signoff=True))
    # chart items never mentioned -> UNADDRESSED
    for it in chart_items:
        if it["label"] in mentioned:
            continue
        ev = triangulate_chart_evidence(it, rec) if it["kind"] == "medication" else {"signals": []}
        proposals.append(Proposal(
            topic=f"{it['kind'].title()}: {it['label']}", kind=it["kind"],
            chart_side=it["label"], chart_resource_id=it.get("resource_id"), provenance_tier=it["tier"],
            spoken_side=None, spoken_span=None, state="UNADDRESSED", likely_correct="unknown",
            confidence="low", clinical_significance=_sig(it["label"]),
            reasoning="[dry-run heuristic] on the chart but never came up in the visit",
            corroborating_evidence=ev.get("signals", []), evidence_dossier=[],
            recommended_next_data="confirm still active with the patient", proposed_action="clarify",
            requires_signoff=True))
    return proposals
