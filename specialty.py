"""Specialty configuration. PreChart is built for GI but is specialty-agnostic.

A specialty config sets the agent's FRAMING (what to prioritize for this kind of
visit) and which meds/problems count as high clinical significance. It deliberately
does NOT change the core reconciliation logic — only what the agent is told to care
about — so the same investigator engine serves any specialty. Add one by dropping a
JSON file in specialties/.
"""
import glob
import json
import os

SPEC_DIR = os.path.join(os.path.dirname(__file__), "specialties")
DEFAULT = "gi"


def available():
    return sorted(os.path.splitext(os.path.basename(p))[0]
                  for p in glob.glob(os.path.join(SPEC_DIR, "*.json")))


def load_specialty(key=None):
    key = (key or DEFAULT).lower()
    path = os.path.join(SPEC_DIR, f"{key}.json")
    if not os.path.exists(path):
        raise SystemExit(f"unknown specialty '{key}'. available: {', '.join(available())}")
    with open(path) as fh:
        cfg = json.load(fh)
    cfg.setdefault("key", key)
    cfg.setdefault("name", key.title())
    cfg.setdefault("visit_framing", "")
    for f in ("priority_meds", "priority_problems", "high_significance_flags"):
        cfg.setdefault(f, [])
    return cfg
