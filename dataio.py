"""Load a record and extract the two witnesses: chart items + spoken assertions.

Deterministic plumbing only — no judgment here. The judgment lives in the
adjudicator (the model). Chart items carry a provenance TIER so the adjudicator
never claims a FHIR id it doesn't have:
  - resource-linked : a real FHIR resource with an id/date/value
  - chart-label     : a bare string from longitudinal_summary (no id, no date)

Temporal integrity: the office visit is one point on a longer timeline (office →
prep → procedure → post-procedure). PreChart runs AT the office visit, so it must
never see an artifact dated after it — the post-procedure report, path result, or
post-op labs are the future and belong to LeftBehind, not here. `redact_future`
strips those at load time so leakage is structurally impossible, not a convention
someone has to remember. The `as_of` clock is reusable: a later-phase agent
(LeftBehind) just loads the same record with a later as_of.
"""
import copy
import json
import os

DEFAULT_DATASET = os.path.join(
    os.path.dirname(__file__), "data", "synthetic-gi.jsonl"
)

# Non-clinical Synthea "findings" that get miscoded as diagnoses — never surface as problems.
JUNK_FINDING = ("employment", "education", "labor force", "medication review due",
                "risk activity", "transport", "social", "stress finding", "higher education")


# --------------------------------------------------------------------------- #
# Temporal filter — no artifact from the future leaks into an earlier-phase agent
# --------------------------------------------------------------------------- #
def _encounter_date(rec):
    """The office-visit date (YYYY-MM-DD) — the default clock PreChart reads as-of."""
    d = rec.get("metadata", {}).get("date")
    if isinstance(d, str) and d[:4].isdigit():
        return d[:10]
    per = rec.get("encounter_fhir", {}).get("encounter", {}).get("period", {}) or {}
    if isinstance(per.get("start"), str) and per["start"][:4].isdigit():
        return per["start"][:10]
    return None


def _resource_date(res):
    """When a FHIR resource happened/was recorded, as YYYY-MM-DD, or None if undated.
    Undated is meaningful: a *planned* procedure order (no performedDateTime) or a
    bare running-list label carries no future information, so it is never redacted."""
    for f in ("authoredOn", "effectiveDateTime", "issued", "performedDateTime",
              "occurrenceDateTime", "onsetDateTime", "recordedDate", "date"):
        v = res.get(f)
        if isinstance(v, str) and v[:4].isdigit():
            return v[:10]
    for f in ("performedPeriod", "effectivePeriod", "period"):
        v = res.get(f)
        if isinstance(v, dict) and isinstance(v.get("start"), str) and v["start"][:4].isdigit():
            return v["start"][:10]
    return None


def redact_future(rec, as_of=None):
    """Drop every artifact dated strictly AFTER `as_of` (default: the office-encounter
    date) so a downstream document — the post-procedure report, path result, post-op
    labs — can never leak into an agent that runs earlier on the timeline. Undated
    artifacts are kept (they carry no future information). Returns a NEW record and
    stamps rec['_temporal_filter'] = {as_of, removed:[...]} as an audit trail."""
    rec = copy.deepcopy(rec)
    as_of = as_of or _encounter_date(rec)
    removed = []
    if as_of:
        rr = rec.get("encounter_fhir", {}).get("related_resources", {}) or {}
        for typ, arr in list(rr.items()):
            kept = []
            for res in arr:
                d = _resource_date(res)
                if d and d > as_of:
                    removed.append({"type": typ, "date": d,
                                    "label": (res.get("code", {}) or {}).get("text")
                                             or res.get("id") or "?"})
                else:
                    kept.append(res)
            rr[typ] = kept
        pn = rec.get("patient_context", {}).get("prior_notes")
        if isinstance(pn, list):
            kept_pn = []
            for note in pn:
                d = (note.get("date") if isinstance(note, dict) else "") or ""
                if d[:10] > as_of:
                    removed.append({"type": "prior_note", "date": d[:10],
                                    "label": (note or {}).get("document_type", "note")})
                else:
                    kept_pn.append(note)
            rec["patient_context"]["prior_notes"] = kept_pn
    rec["_temporal_filter"] = {"as_of": as_of, "removed": removed}
    return rec


def load_record(path=None, index=0, record_id=None, redact=True, as_of=None):
    """Load one record. By default it is redacted to the office-encounter date so
    PreChart can never ingest the future. Pass redact=False (or a later as_of) for a
    downstream agent that legitimately sees post-procedure data."""
    path = path or DEFAULT_DATASET
    recs = [json.loads(l) for l in open(path)]
    if record_id:
        rec = next((r for r in recs if r.get("id", "").startswith(record_id)), None)
        if rec is None:
            raise SystemExit(f"no record id starts with {record_id!r} ({len(recs)} records loaded)")
    else:
        if not -len(recs) <= index < len(recs):
            raise SystemExit(f"--record-index {index} out of range (0..{len(recs) - 1})")
        rec = recs[index]
    return redact_future(rec, as_of) if redact else rec


def _loinc(obs):
    for c in obs.get("code", {}).get("coding", []):
        if c.get("system", "").endswith("loinc.org"):
            return c.get("code")
    return None


def extract_chart_items(rec):
    """Return chart-side witnesses with provenance tiers, plus the raw FHIR the
    triangulation tool needs (kept on each item under `_raw`)."""
    items = []
    rr = (rec.get("encounter_fhir", {}) or {}).get("related_resources", {}) or {}
    ls = (rec.get("patient_context", {}) or {}).get("longitudinal_summary", {}) or {}

    # Medications — resource-linked (this-visit orders) + chart-label (the running list)
    linked_meds = set()
    for mr in rr.get("MedicationRequest", []):
        name = (mr.get("medicationCodeableConcept", {}).get("text")
                or " ".join(c.get("display", "") for c in mr.get("medicationCodeableConcept", {}).get("coding", []))
                or "(unnamed medication)")
        linked_meds.add(name.lower().split()[0])
        items.append(dict(kind="medication", label=name, resource_id=mr.get("id"),
                          tier="resource-linked", value=None, _raw=mr))
    for m in ls.get("medication_labels", []):
        if m.lower().split()[0] in linked_meds:
            continue  # already have it resource-linked
        items.append(dict(kind="medication", label=m, resource_id=None,
                          tier="chart-label", value=None, _raw=None))

    # Problems — strip the junk-coded findings
    for cond in rr.get("Condition", []):
        name = cond.get("code", {}).get("text", "") or ""
        if any(j in name.lower() for j in JUNK_FINDING):
            continue
        items.append(dict(kind="problem", label=name, resource_id=cond.get("id"),
                          tier="resource-linked", value=cond.get("clinicalStatus", {}), _raw=cond))
    for c in ls.get("condition_labels", []):
        if any(j in c.lower() for j in JUNK_FINDING):
            continue
        items.append(dict(kind="problem", label=c, resource_id=None, tier="chart-label",
                          value=None, _raw=None))

    # Allergies (safety-critical; usually sparse in this dataset)
    for a in rr.get("AllergyIntolerance", []):
        name = a.get("code", {}).get("text", "") or "(allergy)"
        items.append(dict(kind="allergy", label=name, resource_id=a.get("id"),
                          tier="resource-linked", value=None, _raw=a))

    return items


MED_KEYWORDS = ("pill", "tablet", "medication", "med", "dose", "milligram", "mg",
                "taking", "take", "stopped", "started", "quit", "statin", "metformin",
                "aspirin", "blood thinner", "water pill", "pressure", "cholesterol",
                "insulin", "inhaler", "prescription", "refill", "skip")


def extract_spoken_assertions(rec, max_items=40):
    """Candidate clinical statements from the ambient transcript (patient/family/clinician).
    Keyword-gated for the scaffold; TODO: upgrade to a model extractor for recall."""
    out = []
    for i, line in enumerate(rec["transcript"].split("\n")):
        line = line.strip()
        if not line or ":" not in line:
            continue
        speaker, _, text = line.partition(":")
        low = text.lower()
        if any(k in low for k in MED_KEYWORDS):
            out.append(dict(turn=i, speaker=speaker.strip(), quote=text.strip()))
        if len(out) >= max_items:
            break
    return out
