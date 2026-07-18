#!/usr/bin/env python3
"""PreChart web app — pick one of the synthetic patients and watch the agent work.

Dependency-free: uses only the Python standard library for the server. The live
agent path still needs `anthropic` + ANTHROPIC_API_KEY; without a key the app runs
the --dry-run heuristic so it works fully offline.

    python3 app.py                 # http://localhost:8000
    python3 app.py --port 9000

The browser calls two endpoints:
    GET  /api/patients             -> the 60-patient index
    POST /api/prechart             -> {index, specialty, live} -> the reconciliation
"""
import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # run from any cwd

from dataio import load_record, extract_chart_items, extract_spoken_assertions, DEFAULT_DATASET
from adjudicator import adjudicate, MODEL
from note import draft_note
from specialty import load_specialty, available as available_specialties

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(HERE, "web", "index.html")
HAS_KEY = bool(os.environ.get("ANTHROPIC_API_KEY"))
_CACHE = {}


def _records():
    with open(DEFAULT_DATASET) as fh:
        return [json.loads(l) for l in fh]


def _demographics(rec):
    """Name / DOB / age / sex / MRN for the header. Records have no real MRN, so we
    derive a stable synthetic one from the record id (clearly synthetic data)."""
    pat = (rec.get("patient_context", {}) or {}).get("patient") \
        or (rec.get("patient_context", {}) or {}).get("Patient") or {}
    nm = (pat.get("name") or [{}])[0]
    given = " ".join(nm.get("given", []))
    name = (", ".join(p for p in (nm.get("family", ""), given) if p) or nm.get("text") or "Unknown")
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
    digits = re.findall(r"\d+", rec.get("id", "").split("::")[0])
    mrn = f"{7000000 + int(digits[-1])}" if digits else "—"
    return {"name": name, "dob": dob, "age": age, "sex": pat.get("gender", ""), "mrn": mrn}


def patients():
    out = []
    for i, r in enumerate(_records()):
        m = r.get("metadata", {})
        out.append({
            "index": i,
            "id": r.get("id", f"record-{i}"),
            "visit_title": m.get("visit_title", "(untitled visit)"),
            "date": (m.get("date") or "")[:10],
            "planted": len(m.get("planted_discrepancies", [])),
        })
    return out


def run_prechart(index, specialty_key, live):
    dry = not (live and HAS_KEY)
    cache_key = f"{index}|{specialty_key}|{'dry' if dry else 'live'}"
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    spec = load_specialty(specialty_key)
    rec = load_record(DEFAULT_DATASET, index=index)
    chart = extract_chart_items(rec)
    spoken = extract_spoken_assertions(rec)

    trace, warning = [], None
    try:
        # note (①) and reconciliation (③) are independent — run them concurrently to
        # roughly halve wall-clock on the live path.
        with ThreadPoolExecutor(max_workers=2) as ex:
            note_f = ex.submit(draft_note, rec, chart, specialty=spec, dry_run=dry)
            props_f = ex.submit(adjudicate, rec, chart, spoken, dry_run=dry, specialty=spec, trace=trace)
            note, props = note_f.result(), props_f.result()
    except Exception as e:  # live path failed (API/network) -> fall back so the demo survives
        warning = f"live agent failed ({type(e).__name__}: {e}); showing the dry-run heuristic instead"
        dry, trace = True, []
        note = draft_note(rec, chart, specialty=spec, dry_run=True)
        props = adjudicate(rec, chart, spoken, dry_run=True, specialty=spec, trace=trace)

    tf = rec.get("_temporal_filter", {})
    counts = {}
    for p in props:
        counts[p.state] = counts.get(p.state, 0) + 1

    result = {
        "meta": {
            "id": rec["id"],
            "index": index,
            "patient": _demographics(rec),
            "visit_title": rec["metadata"].get("visit_title", ""),
            "date": (rec["metadata"].get("date") or "")[:10],
            "specialty": spec["name"],
            "mode": "DRY-RUN heuristic (placeholder — not the model)" if dry else f"AGENT · {MODEL}",
            "is_dry": dry,
            "temporal_removed": len(tf.get("removed", [])),
            "warning": warning,
        },
        "counts": counts,
        "note": note,
        "transcript": rec.get("transcript", ""),
        "chart_items": [{k: v for k, v in it.items() if k != "_raw"} for it in chart],
        "spoken": spoken,
        "trace": trace,
        "proposals": [p.to_dict() for p in props],
        "planted": rec["metadata"].get("planted_discrepancies", []),
    }
    _CACHE[cache_key] = result
    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet console
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                with open(INDEX_HTML, "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(500, {"error": f"missing {INDEX_HTML}"})
        elif self.path == "/api/patients":
            self._send(200, {"patients": patients(),
                             "specialties": available_specialties(),
                             "has_key": HAS_KEY})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/api/prechart":
            self._send(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            result = run_prechart(int(body.get("index", 0)),
                                  body.get("specialty", "gi"),
                                  bool(body.get("live", False)))
            self._send(200, result)
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--warm", default="",
                    help="comma-separated record indices to pre-run into the cache before serving (e.g. --warm 47,15)")
    ap.add_argument("--warm-specialty", default="gi", help="specialty to warm with (default gi)")
    a = ap.parse_args()
    live_ready = HAS_KEY
    if HAS_KEY:
        try:
            import anthropic  # noqa: F401
        except ImportError:
            live_ready = False
            print("WARNING: ANTHROPIC_API_KEY is set but the 'anthropic' package is not installed in\n"
                  "         this Python — the live agent will fall back to the dry-run template.\n"
                  "         Start it with:  uv run --with anthropic python app.py   (or pip install anthropic)")
    if live_ready:
        mode = f"LIVE agent available ({MODEL})"
    elif HAS_KEY:
        mode = "DRY-RUN only (anthropic not installed — see warning above)"
    else:
        mode = "DRY-RUN only (no ANTHROPIC_API_KEY)"

    if a.warm:
        idxs = [int(x) for x in a.warm.split(",") if x.strip().lstrip("-").isdigit()]
        print(f"Pre-warming cache: {len(idxs)} record(s) × '{a.warm_specialty}' ({MODEL})… this is the slow part, once.")
        for i in idxs:
            try:
                run_prechart(i, a.warm_specialty, live=live_ready)
                print(f"  ✓ warmed idx {i}")
            except Exception as e:
                print(f"  ✗ idx {i}: {type(e).__name__}: {e}")

    print(f"PreChart web app → http://localhost:{a.port}   [{mode}]")
    ThreadingHTTPServer(("0.0.0.0", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
