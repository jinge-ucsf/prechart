"""Pre-charting: draft the office-visit note from the encounter material.

PreChart drafts a specialty-styled consultation note from the chart (problems,
meds, labs, imaging, prior/outside notes) AND the ambient transcript of the visit,
so the Assessment & Plan is the best approximation of the ACTUAL management — not a
chart-only guess. The separate reconciliation step surfaces where the chart and the
room disagreed.

Live path drafts with the model (streamed); --dry-run assembles a deterministic
template so the skeleton renders with no key.
"""
import json
import re

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


# Strip coded-terminology qualifiers (anywhere) so the note doesn't read like a code dump.
_SNOMED_QUAL = re.compile(
    r"\s*\((disorder|finding|situation|procedure|morphologic abnormality|observable entity|"
    r"regime/therapy|substance|product|qualifier value)\)", re.I)


def _clean(label):
    s = _SNOMED_QUAL.sub("", (label or ""))
    s = re.sub(r"\s+,", ",", s)      # "esophagus , C2M4" -> "esophagus, C2M4"
    s = re.sub(r"\s{2,}", " ", s)    # collapse doubled spaces
    return s.strip()


def _dedupe(items):
    seen, out = set(), []
    for x in items:
        k = x.lower().strip()
        if k and k not in seen:
            seen.add(k)
            out.append(x)
    return out


def _age_sex(rec):
    pat = (rec.get("patient_context", {}) or {}).get("patient") or {}
    dob = (pat.get("birthDate") or "")[:10]
    enc = (rec.get("metadata", {}).get("date") or "")[:10]
    age = None
    if dob[:4].isdigit() and enc[:4].isdigit():
        try:
            by, bm, bd = (int(x) for x in dob.split("-"))
            ey, em, ed = (int(x) for x in enc.split("-"))
            age = ey - by - ((em, ed) < (bm, bd))
        except ValueError:
            age = None
    return age, pat.get("gender", "")


def _note_payload(rec, chart_items, specialty):
    rr = rec.get("encounter_fhir", {}).get("related_resources", {}) or {}
    age, sex = _age_sex(rec)
    return {
        "reason_for_visit": rec.get("metadata", {}).get("visit_title", ""),
        "patient": {"age": age, "sex": sex},
        "specialty": (specialty or {}).get("name", "General"),
        "specialty_focus": (specialty or {}).get("visit_framing", ""),
        "high_significance": (specialty or {}).get("high_significance_flags", []),
        "active_problems": _dedupe([_clean(it["label"]) for it in chart_items if it["kind"] == "problem"]),
        "medications": _dedupe([it["label"] for it in chart_items if it["kind"] == "medication"]),
        "allergies": _dedupe([_clean(it["label"]) for it in chart_items if it["kind"] == "allergy"]),
        "recent_labs": _labs(rec),
        "imaging_and_diagnostics": _diagnostics(rr),
        "procedures": _procedures(rr),
        "prior_notes": [
            {"type": n.get("document_type"), "date": n.get("date"),
             "specialty": n.get("provider_specialty"), "text": (n.get("text") or "")[:2000]}
            for n in rec.get("patient_context", {}).get("prior_notes", []) or []
        ],
        # the ambient recording of THIS visit — drives the interval history and the A/P
        "ambient_transcript": rec.get("transcript", ""),
    }


def draft_note(rec, chart_items, specialty=None, dry_run=False):
    payload = _note_payload(rec, chart_items, specialty)
    if dry_run:
        return _template_note(payload)
    system = (specialty or {}).get("note_prompt") or SYSTEM   # the specialty's Epic-style spec
    return _model_note(payload, system)


def _model_note(payload, system):
    import anthropic  # lazy: live path only
    client = anthropic.Anthropic()
    user = ("Draft the note from this encounter material. The chart (problems, medications, labs, "
            "imaging/diagnostics, prior/outside notes) AND the ambient transcript of today's visit "
            "are provided — use BOTH: the transcript drives the interval history and the Assessment "
            "& Plan (the actual management discussed), the chart supplies background and objective "
            "data. Where they conflict, reason to the most likely truth and flag it.\n\n"
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
