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


def _chart_payload(rec, chart_items, specialty):
    return {
        "reason_for_visit": rec.get("metadata", {}).get("visit_title", ""),
        "specialty": (specialty or {}).get("name", "General"),
        "specialty_focus": (specialty or {}).get("visit_framing", ""),
        "high_significance": (specialty or {}).get("high_significance_flags", []),
        "active_problems": [it["label"] for it in chart_items if it["kind"] == "problem"],
        "medications": [it["label"] for it in chart_items if it["kind"] == "medication"],
        "allergies": [it["label"] for it in chart_items if it["kind"] == "allergy"],
        "recent_labs": _labs(rec),
        "prior_notes": [
            {"type": n.get("document_type"), "date": n.get("date"),
             "specialty": n.get("provider_specialty"), "text": (n.get("text") or "")[:1200]}
            for n in rec.get("patient_context", {}).get("prior_notes", []) or []
        ],
    }


def draft_note(rec, chart_items, specialty=None, dry_run=False):
    payload = _chart_payload(rec, chart_items, specialty)
    if dry_run:
        return _template_note(payload)
    return _model_note(payload)


def _model_note(payload):
    import anthropic  # lazy: live path only
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=MODEL, max_tokens=4000, thinking={"type": "adaptive"}, system=SYSTEM,
        messages=[{"role": "user", "content":
                   "Draft the pre-visit note from this chart data:\n\n" + json.dumps(payload, indent=1)}])
    return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()


def _template_note(p):
    """Deterministic fallback (NOT the model) — assembles the chart into a draft."""
    def bullets(items):
        return "\n".join(f"- {x}" for x in items) if items else "None documented."

    hi = tuple(p["high_significance"])
    meds = [f"{m}  _(verify — high-significance for this visit)_" if any(h in m.lower() for h in hi) else m
            for m in p["medications"]]
    priors = "\n".join(
        f"- **{n['type']}** ({n['date']}, {n['specialty']}): {n['text'][:200].strip()}…"
        for n in p["prior_notes"]) or "None documented."
    to_confirm = [m for m in p["medications"] if any(h in m.lower() for h in hi)] or \
                 p["medications"][:3] or ["chart items with the patient"]
    return f"""## Pre-Visit Note — {p['reason_for_visit']}
*{p['specialty']} · drafted from the chart, to be verified in the room (dry-run template — not the model)*

## Reason for Visit
{p['reason_for_visit']}

## Active Problems (per chart)
{bullets(p['active_problems'])}

## Medications (per chart)
{bullets(meds)}

## Allergies
{bullets(p['allergies'])}

## Pertinent Labs
{bullets(p['recent_labs'])}

## {p['specialty']} Considerations
{p['specialty_focus'] or 'None specified.'}

## Prior Notes
{priors}

## To Confirm in the Visit
{bullets(to_confirm)}
"""
