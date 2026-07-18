"""Core data model for PreChart adjudication.

The reconciliation treats the chart and the ambient conversation as TWO
FALLIBLE WITNESSES. Neither is ground truth: charts carry stale med lists,
copy-forward errors, and coded artifacts; patients misremember, use lay terms,
and get mis-transcribed. The adjudicator's job is to weigh both per claim,
propose the most-likely-correct answer with its reasoning, and — when it
genuinely can't resolve — say so (UNRESOLVED) rather than guess. Nothing
high-stakes is written without a clinician tap.
"""
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class State(str, Enum):
    CONFIRMED = "CONFIRMED"        # both witnesses agree
    CONTRADICTED = "CONTRADICTED"  # they disagree AND the evidence favors one side
    ELABORATED = "ELABORATED"      # spoken adds detail (adherence, side effect)
    NEW = "NEW"                    # said in the room, absent from the chart
    UNADDRESSED = "UNADDRESSED"    # in the chart, never came up in the visit
    UNRESOLVED = "UNRESOLVED"      # genuine conflict; neither witness is clearly right


class Source(str, Enum):
    CHART = "chart"
    PATIENT = "patient"
    UNKNOWN = "unknown"


# Tri-state actions the ledger can stage. High-stakes ones always require_signoff.
ACTIONS = ("confirm", "update_chart", "add_to_chart", "inactivate", "flag_allergy", "clarify", "none")


@dataclass
class Proposal:
    topic: str                              # "Medication: metoprolol succinate"
    kind: str                               # medication | problem | lab | allergy | sdoh
    # --- witness 1: the chart ---
    chart_side: Optional[str]               # what the chart asserts (or None)
    chart_resource_id: Optional[str]        # FHIR id, if resource-linked
    provenance_tier: str                    # resource-linked | chart-label | none
    # --- witness 2: the room ---
    spoken_side: Optional[str]              # what was said (or None)
    spoken_span: Optional[str]              # verbatim transcript quote
    # --- the adjudication ---
    state: str                              # State
    likely_correct: str                     # Source: chart | patient | unknown
    confidence: str                         # low | medium | high
    clinical_significance: str              # low | medium | high
    reasoning: str                          # what the assembled evidence shows
    corroborating_evidence: list = field(default_factory=list)  # quick triangulation signals
    # --- the investigation ---
    evidence_dossier: list = field(default_factory=list)   # [{source, finding, leans}] adjacent data pulled
    recommended_next_data: Optional[str] = None            # the ONE datum that would resolve an UNRESOLVED
    proposed_action: str = "none"
    requires_signoff: bool = True

    def to_dict(self):
        return asdict(self)
