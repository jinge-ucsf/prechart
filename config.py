"""Runtime model / speed config, shared by the note generator and the adjudicator.

Tuned for a fast demo by default: Opus, no extended thinking, single-shot
adjudication (the deterministic tools are pre-run and handed to one model call).
Dial up quality when you want it:
  PRECHART_MODEL=<model-id>   -> override the model for note + adjudication
  PRECHART_FAST=1             -> use Sonnet (faster still)
  PRECHART_THINKING=adaptive  -> re-enable extended thinking (slower, deeper)
  PRECHART_AGENTIC_LOOP=1     -> use the multi-turn tool-use loop (slower, fully agentic)
"""
import os

FAST = os.environ.get("PRECHART_FAST") == "1"
MODEL = os.environ.get("PRECHART_MODEL") or ("claude-sonnet-5" if FAST else "claude-opus-4-8")

# Multi-turn tool-use loop (the model chooses tools round by round) vs. one-shot with
# pre-gathered evidence. One-shot is much faster and is the default for the demo.
AGENTIC_LOOP = os.environ.get("PRECHART_AGENTIC_LOOP") == "1"


def thinking_kwargs():
    """{} to disable thinking (default — faster), or the thinking param to enable it."""
    mode = (os.environ.get("PRECHART_THINKING") or "off").lower()
    return {} if mode in ("off", "none", "0", "false") else {"thinking": {"type": "adaptive"}}
