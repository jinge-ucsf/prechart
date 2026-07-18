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
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # run from any cwd

from dataio import load_record, extract_chart_items, extract_spoken_assertions, DEFAULT_DATASET
from adjudicator import adjudicate, MODEL
from specialty import load_specialty, available as available_specialties

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(HERE, "web", "index.html")
HAS_KEY = bool(os.environ.get("ANTHROPIC_API_KEY"))
_CACHE = {}


def _records():
    with open(DEFAULT_DATASET) as fh:
        return [json.loads(l) for l in fh]


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
        props = adjudicate(rec, chart, spoken, dry_run=dry, specialty=spec, trace=trace)
    except Exception as e:  # live path failed (API/network) -> fall back so the demo survives
        warning = f"live agent failed ({type(e).__name__}: {e}); showing the dry-run heuristic instead"
        dry, trace = True, []
        props = adjudicate(rec, chart, spoken, dry_run=True, specialty=spec, trace=trace)

    tf = rec.get("_temporal_filter", {})
    counts = {}
    for p in props:
        counts[p.state] = counts.get(p.state, 0) + 1

    result = {
        "meta": {
            "id": rec["id"],
            "index": index,
            "visit_title": rec["metadata"].get("visit_title", ""),
            "date": (rec["metadata"].get("date") or "")[:10],
            "specialty": spec["name"],
            "mode": "DRY-RUN heuristic (placeholder — not the model)" if dry else f"AGENT · {MODEL}",
            "is_dry": dry,
            "temporal_removed": len(tf.get("removed", [])),
            "warning": warning,
        },
        "counts": counts,
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
    a = ap.parse_args()
    mode = f"LIVE agent available ({MODEL})" if HAS_KEY else "DRY-RUN only (no ANTHROPIC_API_KEY)"
    print(f"PreChart web app → http://localhost:{a.port}   [{mode}]")
    ThreadingHTTPServer(("0.0.0.0", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
