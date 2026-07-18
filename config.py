"""Runtime model / speed config, shared by the note generator and the adjudicator.

Defaults to Opus + adaptive thinking (best quality). For a faster demo:
  PRECHART_FAST=1           -> Sonnet + thinking off (much faster, still strong)
  PRECHART_MODEL=<model-id> -> override the model for both note + adjudication
  PRECHART_THINKING=off     -> disable extended thinking (big latency cut)
"""
import os

FAST = os.environ.get("PRECHART_FAST") == "1"
MODEL = os.environ.get("PRECHART_MODEL") or ("claude-sonnet-5" if FAST else "claude-opus-4-8")


def thinking_kwargs():
    """{} to disable thinking, or the thinking param to enable it."""
    mode = (os.environ.get("PRECHART_THINKING") or ("off" if FAST else "adaptive")).lower()
    return {} if mode == "off" else {"thinking": {"type": "adaptive"}}
