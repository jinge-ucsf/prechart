"""Agent tools. The model DECIDES when to call these; they are deterministic.

The centerpiece is `triangulate_chart_evidence`: instead of adjudicating from
just the two conflicting statements, the agent pulls the chart's OWN internal
signals (refill/order gaps, related labs, duplicates, label-only status) to
weigh WHICH witness is more likely right. That reasoning is what a good
clinician does at the desk before the visit — and what a string-diff can't.
"""
import re

# Lay term -> plausible drug class/members (for find_chart_item entity resolution)
LAY_TO_DRUGS = {
    "water pill": ["hydrochlorothiazide", "furosemide", "chlorthalidone"],
    "blood thinner": ["warfarin", "clopidogrel", "apixaban", "rivaroxaban", "aspirin"],
    "sugar pill": ["metformin"], "diabetes pill": ["metformin"],
    "cholesterol pill": ["simvastatin", "atorvastatin", "rosuvastatin", "pravastatin"],
    "statin": ["simvastatin", "atorvastatin", "rosuvastatin", "pravastatin"],
    "pressure pill": ["lisinopril", "losartan", "amlodipine", "metoprolol", "hydrochlorothiazide"],
    "blood pressure pill": ["lisinopril", "losartan", "amlodipine", "metoprolol"],
    "heart pill": ["metoprolol", "carvedilol", "atenolol"],
}

# Drug (class) -> a lab whose presence/value corroborates real use
DRUG_RELATED_LAB = {
    "statin": ("18262-6", "LDL"), "simvastatin": ("18262-6", "LDL"),
    "atorvastatin": ("18262-6", "LDL"), "rosuvastatin": ("18262-6", "LDL"),
    "warfarin": ("6301-6", "INR"),
    "metformin": ("4548-4", "HbA1c"),
}


def _loinc(obs):
    for c in obs.get("code", {}).get("coding", []):
        if c.get("system", "").endswith("loinc.org"):
            return c.get("code")
    return None


def find_chart_item(mention, chart_items):
    """Resolve a spoken mention (incl. lay terms) to chart item(s). Returns matches."""
    m = mention.lower()
    targets = {m}
    for lay, drugs in LAY_TO_DRUGS.items():
        if lay in m:
            targets.update(drugs)
    hits = []
    for it in chart_items:
        label = it["label"].lower()
        first = label.split()[0] if label else ""
        if any(t in label or (t and t in first) or first in t for t in targets if t):
            hits.append(it)
    return hits


def span_lookup(quote, rec):
    """Locate a quote in the transcript; return the turn index + surrounding context."""
    q = quote.strip().lower()[:60]
    for i, line in enumerate(rec["transcript"].split("\n")):
        if q and q in line.lower():
            return {"turn": i, "line": line.strip()}
    return {"turn": None, "line": None}


def _encounter_date(rec):
    return rec["metadata"]["date"][:10]


def _days_between(a, b):
    from datetime import date
    ya, ma, da = (int(x) for x in a.split("-"))
    yb, mb, db = (int(x) for x in b.split("-"))
    return abs((date(ya, ma, da) - date(yb, mb, db)).days)


def triangulate_chart_evidence(item, rec):
    """THE STAR TOOL. Given a chart item (usually a medication), gather the chart's
    own internal signals that bear on whether the chart or the patient is more
    likely correct. Returns a structured evidence bundle + a coarse `staleness`
    read the adjudicator can weigh (it does NOT decide — the model does)."""
    ev = {"signals": [], "staleness": "unknown", "related_lab": None, "label_only": None,
          "duplicate_therapy": False}
    label = item["label"].lower()
    enc = _encounter_date(rec)
    rr = rec["encounter_fhir"]["related_resources"]

    # 1) Order recency: a med "active" on the running list with no recent order is a
    #    classic stale-chart signal (favors the patient if they report stopping it).
    ev["label_only"] = (item.get("tier") == "chart-label" and item.get("resource_id") is None)
    last_order = None
    for mr in rr.get("MedicationRequest", []):
        name = (mr.get("medicationCodeableConcept", {}).get("text") or "").lower()
        disp = " ".join(c.get("display", "") for c in mr.get("medicationCodeableConcept", {}).get("coding", [])).lower()
        if label.split()[0] and label.split()[0] in (name + " " + disp):
            when = mr.get("authoredOn", "")[:10]
            if when:  # keep the NEWEST order, not the last one in array order
                last_order = when if last_order is None else max(last_order, when)
    if item["kind"] == "medication":
        if last_order:
            gap = _days_between(enc, last_order)
            ev["signals"].append(f"most recent order {gap} days before this visit ({last_order})")
            ev["staleness"] = "recent-order" if gap <= 180 else "no-recent-order"
        elif ev["label_only"]:
            ev["signals"].append("appears only on the running list — no order/date in this record")
            ev["staleness"] = "no-order-on-file"

    # 2) Related lab corroboration (statin->LDL, warfarin->INR, metformin->A1c)
    key = next((k for k in DRUG_RELATED_LAB if k in label), None)
    if key:
        loinc, lname = DRUG_RELATED_LAB[key]
        for o in rr.get("Observation", []):
            if _loinc(o) == loinc and isinstance(o.get("valueQuantity", {}).get("value"), (int, float)):
                v = round(o["valueQuantity"]["value"], 1)
                ev["related_lab"] = f"{lname} = {v} present in this encounter"
                ev["signals"].append(ev["related_lab"])
                break

    # 3) Duplicate-therapy detector (e.g., two statins on the list)
    firsts = [it for it in _all_med_firsts(rec)]
    if item["kind"] == "medication":
        base = _drug_class(label)
        if base and sum(1 for f in firsts if _drug_class(f) == base) > 1:
            ev["duplicate_therapy"] = True
            ev["signals"].append(f"possible duplicate therapy within class '{base}'")

    return ev


def _all_med_firsts(rec):
    """Distinct meds across the running list + this visit's orders, deduped by drug
    (first token) so a med that appears on BOTH the running list and an encounter
    order counts once — otherwise the duplicate-therapy check false-fires on a
    single drug listed in two places."""
    labels = list(rec["patient_context"]["longitudinal_summary"].get("medication_labels", []))
    for mr in rec["encounter_fhir"]["related_resources"].get("MedicationRequest", []):
        labels.append(mr.get("medicationCodeableConcept", {}).get("text", "") or "")
    seen, out = set(), []
    for l in labels:
        if not l:
            continue
        key = l.lower().split()[0]
        if key not in seen:
            seen.add(key)
            out.append(l.lower())
    return out


def _drug_class(label):
    for cls in ("statin",):
        if cls in label:
            return cls
    for member, base in (("simvastatin", "statin"), ("atorvastatin", "statin"),
                         ("rosuvastatin", "statin"), ("pravastatin", "statin")):
        if member in label:
            return base
    return None


def check_physiologic_markers(item, rec):
    """THE THIRD WITNESS. The chart and the patient can both be wrong; the body is
    harder to fool. For a drug, pull the objective marker that reflects whether it's
    actually on board and read it AGAINST both claims. Returns the value + a coarse
    read; the model does the real interpretation and never treats it as proof alone."""
    label = item["label"].lower()
    rr = rec["encounter_fhir"]["related_resources"]

    def _value(loinc):
        """Most-recent matching observation (value, unit, id, date) — a stale marker
        can mislead, so we surface the date and always take the newest."""
        cands = []
        for o in rr.get("Observation", []):
            vq = o.get("valueQuantity", {})
            if _loinc(o) == loinc and isinstance(vq.get("value"), (int, float)):
                when = (o.get("effectiveDateTime") or o.get("issued") or "")[:10]
                cands.append((when, o))
        if not cands:
            return None, None, None, None
        when, o = max(cands, key=lambda t: t[0])
        return round(o["valueQuantity"]["value"], 2), o["valueQuantity"].get("unit", ""), o.get("id"), when

    # warfarin -> INR (cleanest on/off signal)
    if "warfarin" in label:
        v, u, oid, when = _value("6301-6")
        if v is None:
            return {"marker": "INR", "read": "no-marker-available",
                    "note": "no INR on file — order one to confirm anticoagulation status", "obs_id": None}
        read = ("consistent with active use (therapeutic INR)" if v >= 2.0
                else "suggests NOT currently taking (subtherapeutic INR)" if v <= 1.3
                else "inconclusive")
        return {"marker": "INR", "value": v, "unit": u, "read": read, "obs_id": oid, "date": when}

    # statin -> LDL (effect marker)
    if _drug_class(label) == "statin":
        v, u, oid, when = _value("18262-6")
        if v is None:
            return {"marker": "LDL", "read": "no-marker-available", "obs_id": None}
        read = ("consistent with active statin effect (LDL at goal)" if v < 100
                else "suggests inadequate or absent statin effect" if v >= 130 else "inconclusive")
        return {"marker": "LDL", "value": v, "unit": u, "read": read, "obs_id": oid, "date": when}

    # metformin -> A1c (control, not adherence)
    if "metformin" in label:
        v, u, oid, when = _value("4548-4")
        return {"marker": "HbA1c", "value": v, "unit": u,
                "read": "inconclusive (A1c reflects glycemic control, not adherence per se)",
                "obs_id": oid, "date": when}

    return {"marker": None, "read": "no-physiologic-marker-for-this-item"}


# Tool schemas advertised to the model (Anthropic tool-use format).
TOOL_SCHEMAS = [
    {
        "name": "check_physiologic_markers",
        "description": ("Pull the OBJECTIVE lab marker that reflects whether a medication is truly "
                        "on board (warfarin->INR, statin->LDL, metformin->A1c), read independently of "
                        "what the chart or patient claims. Use it as a third witness when the chart and "
                        "the patient disagree — the value may support one, both, or neither."),
        "input_schema": {"type": "object",
                         "properties": {"chart_item_label": {"type": "string"}},
                         "required": ["chart_item_label"]},
    },
    {
        "name": "triangulate_chart_evidence",
        "description": ("Gather the chart's OWN internal signals about a chart item to help "
                        "decide whether the chart or the patient is more likely correct: order/refill "
                        "recency, related-lab corroboration, duplicate therapy, and whether the item is "
                        "label-only (no resource/date). Call this before adjudicating any medication or "
                        "problem discrepancy."),
        "input_schema": {
            "type": "object",
            "properties": {"chart_item_label": {"type": "string",
                          "description": "The exact label of the chart item to investigate"}},
            "required": ["chart_item_label"],
        },
    },
]
