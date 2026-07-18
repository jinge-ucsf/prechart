"""Pre-charting: draft the office-visit note from the chart BEFORE the room.

This is the "pre-chart" — a specialty-styled draft the clinician reads before the
patient walks in. It is built from the CHART ONLY (problems, meds, labs, prior
notes); the ambient transcript is deliberately excluded, because verifying this
draft against the room is exactly what the reconciliation step does next.

Live path drafts with the model; --dry-run assembles a deterministic template so
the skeleton renders with no key. Both return Markdown.
"""
import json

MODEL = "claude-opus-4-8"

SYSTEM = """You are pre-charting an office visit: drafting the note from the chart BEFORE
the patient is seen, so the clinician can review it and walk in prepared.

Draft a concise, clinically useful pre-visit note using ONLY the chart data provided
(active problems, medications, recent labs, prior notes). Rules:
- Do NOT invent diagnoses, medications, values, or history. If a section has no data,
  write "None documented."
- This is a DRAFT to be verified in the room — never state adherence, current dosing,
  or "confirmed" as fact; where the chart is the only source, hedge ("per chart").
- End with a short "To confirm in the visit" list: the specific chart items whose
  accuracy most affects THIS visit (prioritize the specialty's high-significance items).
Return GitHub-flavored Markdown with ## section headings. No preamble."""


def _labs(rec, limit=12):
    out = []
    for o in rec.get("encounter_fhir", {}).get("related_resources", {}).get("Observation", []):
        vq = o.get("valueQuantity", {})
        name = o.get("code", {}).get("text") or " ".join(
            c.get("display", "") for c in o.get("code", {}).get("coding", []))
        if isinstance(vq.get("value"), (int, float)) and name:
            when = (o.get("effectiveDateTime") or o.get("issued") or "")[:10]
            out.append(f"{name} = {vq['value']} {vq.get('unit', '')}".strip() + (f" ({when})" if when else ""))
    return out[:limit]


def _diagnostics(rr):
    out = []
    for d in rr.get("DiagnosticReport", []):
        name = d.get("code", {}).get("text") or "diagnostic report"
        when = (d.get("effectiveDateTime") or d.get("issued") or "")[:10]
        result = (d.get("conclusion") or "").strip()
        out.append(f"{name}" + (f" ({when})" if when else "") + (f": {result[:300]}" if result else ""))
    return out


def _procedures(rr):
    out = []
    for pr in rr.get("Procedure", []):
        name = pr.get("code", {}).get("text") or "procedure"
        when = (pr.get("performedDateTime") or (pr.get("performedPeriod") or {}).get("start") or "")[:10]
        out.append(f"{name}" + (f" ({when})" if when else ""))
    return out


def _chart_payload(rec, chart_items, specialty):
    rr = rec.get("encounter_fhir", {}).get("related_resources", {}) or {}
    return {
        "reason_for_visit": rec.get("metadata", {}).get("visit_title", ""),
        "specialty": (specialty or {}).get("name", "General"),
        "specialty_focus": (specialty or {}).get("visit_framing", ""),
        "high_significance": (specialty or {}).get("high_significance_flags", []),
        "active_problems": [it["label"] for it in chart_items if it["kind"] == "problem"],
        "medications": [it["label"] for it in chart_items if it["kind"] == "medication"],
        "allergies": [it["label"] for it in chart_items if it["kind"] == "allergy"],
        "recent_labs": _labs(rec),
        "imaging_and_diagnostics": _diagnostics(rr),
        "procedures": _procedures(rr),
        "prior_notes": [
            {"type": n.get("document_type"), "date": n.get("date"),
             "specialty": n.get("provider_specialty"), "text": (n.get("text") or "")[:1500]}
            for n in rec.get("patient_context", {}).get("prior_notes", []) or []
        ],
    }


def draft_note(rec, chart_items, specialty=None, dry_run=False):
    payload = _chart_payload(rec, chart_items, specialty)
    if dry_run:
        return _template_note(payload)
    system = (specialty or {}).get("note_prompt") or SYSTEM   # the specialty's Epic-style spec
    return _model_note(payload, system)


def _model_note(payload, system):
    import anthropic  # lazy: live path only
    client = anthropic.Anthropic()
    user = ("Draft the note from this pre-visit chart material (problems, medications, labs, "
            "imaging/diagnostics, and prior/outside notes are included where available):\n\n"
            + json.dumps(payload, indent=1))
    # Stream with a generous budget: adaptive thinking AND the full note (HPI + Outside Data +
    # the complete Assessment & Plan) must both fit under max_tokens, or the A/P is truncated.
    # Streaming also avoids the SDK's non-streaming timeout on large outputs.
    with client.messages.stream(
        model=MODEL, max_tokens=20000, thinking={"type": "adaptive"},
        system=system, messages=[{"role": "user", "content": user}],
    ) as stream:
        msg = stream.get_final_message()
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
    if msg.stop_reason == "max_tokens":
        text += "\n\n[note truncated at max_tokens — raise the budget in note.py:_model_note]"
    return text


def _template_note(p):
    """Deterministic fallback (NOT the model) — assembles the chart into the three-part
    plain-text shape (HPI / Outside Data / A/P) so the format is visible offline. The
    live model writes the real interpretive note from the specialty spec."""
    hi = tuple(p["high_significance"])
    probs = p["active_problems"]
    meds = p["medications"]

    hpi = (f"Patient presenting for {p['reason_for_visit']}. "
           f"Active problems per chart: {', '.join(probs) if probs else 'none documented'}. "
           f"Medications per chart: {', '.join(meds) if meds else 'none documented'}. "
           f"Allergies: {', '.join(p['allergies']) if p['allergies'] else 'none documented'}. "
           f"[dry-run template — the live model writes the full interpretive HPI]")

    outside = ""
    if p["recent_labs"] or p["imaging_and_diagnostics"]:
        outside = "\n\nOutside Data (Care Everywhere):\n"
        if p["recent_labs"]:
            outside += "Labs:\n" + "\n".join(f"- {x}" for x in p["recent_labs"]) + "\n"
        if p["imaging_and_diagnostics"]:
            outside += "Imaging/diagnostics:\n" + "\n".join(f"- {x}" for x in p["imaging_and_diagnostics"]) + "\n"

    blocks = []
    for prob in (probs or [p["reason_for_visit"]]):
        blocks.append(f"**{prob}: *** dry-run template — the live model writes the assessment "
                      f"(diagnosis, dates, values, status).\n- Confirm status and management with the patient in the room.")
    for m in [m for m in meds if any(h in m.lower() for h in hi)]:
        blocks.append(f"**Medication — {m}: high-significance for this visit.\n"
                      f"- *** Verify current use and dose and reconcile against the room.")

    return (f"History of Present Illness\n{hpi}"
            f"{outside}\n\n"
            f"Assessment & Plan\n"
            f"Pre-visit draft for {p['specialty']} (dry-run template — not the model).\n\n"
            + "\n\n".join(blocks) + "\n")
