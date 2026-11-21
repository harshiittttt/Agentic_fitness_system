"""Shared vocabulary of the athlete memory layer (see DESIGN.md). Rows are plain dicts, like the rest of fitness_os; these are the typed shapes
that cross module boundaries, plus the predicate registry that tells the store how each kind of fact behaves."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---- enumerations (strings, so they are JSON/SQL friendly) -------------------------------------------------------------------------------
FACT_KINDS = ("semantic", "episodic", "procedural", "preference", "constraint", "trait", "goal")
SOURCES = ("stated", "corrected", "observed", "inferred", "derived", "imported")   # stated = the athlete said it; observed = computed from logged data
STATUSES = ("active", "superseded", "disputed", "retracted", "archived")
USER_STATES = ("none", "confirmed", "corrected", "denied")
SENSITIVITY = ("normal", "health", "body", "mental")
DISCOVERY_STATUS = ("candidate", "confirmed", "weakening", "retired")
ITEM_TYPES = ("fact", "discovery", "dossier", "question")

# Beta(alpha, beta) priors by source: confidence = alpha / (alpha + beta). Evidence moves them (store.reinforce).
PRIOR = {"stated": (3.0, 1.0), "corrected": (6.0, 1.0), "observed": (4.0, 1.0), "inferred": (2.0, 1.0), "derived": (3.0, 1.0), "imported": (2.5, 1.0)}

# ---- predicate registry ------------------------------------------------------------------------------------------------------------------
# functional  : at most one ACTIVE value per subject at a time (a new, better-evidenced value supersedes the old one).
# half_life   : days for confidence to relax halfway back to 0.5 when nothing reinforces it (None = never; pinned facts are exempt anyway).
# sensitivity : default sensitivity class. cls: which part of the dossier the predicate feeds.
PREDICATES: dict[str, dict[str, Any]] = {
    # goals and identity
    "goal": {"functional": True, "half_life": 240, "sensitivity": "normal", "cls": "goals"},
    "target_weight_kg": {"functional": True, "half_life": 180, "sensitivity": "body", "cls": "goals"},
    "target_date": {"functional": True, "half_life": 120, "sensitivity": "normal", "cls": "goals"},
    "motivation": {"functional": False, "half_life": 240, "sensitivity": "normal", "cls": "behavior"},
    # preferences
    "likes_exercise": {"functional": False, "half_life": 300, "sensitivity": "normal", "cls": "training"},
    "dislikes_exercise": {"functional": False, "half_life": 300, "sensitivity": "normal", "cls": "training"},
    "prefers_rep_range": {"functional": True, "half_life": 200, "sensitivity": "normal", "cls": "training"},
    "prefers_session_minutes": {"functional": True, "half_life": 120, "sensitivity": "normal", "cls": "training"},
    "prefers_training_time": {"functional": True, "half_life": 150, "sensitivity": "normal", "cls": "schedule"},
    "trains_days_per_week": {"functional": True, "half_life": 90, "sensitivity": "normal", "cls": "schedule"},
    "available_equipment": {"functional": False, "half_life": 365, "sensitivity": "normal", "cls": "training"},
    "likes_food": {"functional": False, "half_life": 240, "sensitivity": "normal", "cls": "nutrition"},
    "dislikes_food": {"functional": False, "half_life": 300, "sensitivity": "normal", "cls": "nutrition"},
    "diet_type": {"functional": True, "half_life": 365, "sensitivity": "normal", "cls": "nutrition"},
    "allergy": {"functional": False, "half_life": None, "sensitivity": "health", "cls": "risk"},
    # health and risk (never forgotten, never shown to anyone but the athlete and the agents that need them)
    "injury_site": {"functional": False, "half_life": None, "sensitivity": "health", "cls": "risk"},
    "pain_aggravated_by": {"functional": False, "half_life": 365, "sensitivity": "health", "cls": "risk"},
    "pain_relieved_by": {"functional": False, "half_life": 365, "sensitivity": "health", "cls": "risk"},
    "medical_condition": {"functional": False, "half_life": None, "sensitivity": "health", "cls": "risk"},
    "avoids_movement": {"functional": False, "half_life": 365, "sensitivity": "health", "cls": "risk"},
    # schedule and life
    "work_pattern": {"functional": True, "half_life": 120, "sensitivity": "normal", "cls": "schedule"},
    "travel_pattern": {"functional": False, "half_life": 90, "sensitivity": "normal", "cls": "schedule"},
    "sleep_schedule": {"functional": True, "half_life": 90, "sensitivity": "normal", "cls": "schedule"},
    "life_event": {"functional": False, "half_life": 60, "sensitivity": "normal", "cls": "behavior"},
    # communication and behaviour (largely derived)
    "responds_to_tone": {"functional": True, "half_life": 120, "sensitivity": "normal", "cls": "behavior"},
    "ignores_nudges_at": {"functional": False, "half_life": 90, "sensitivity": "normal", "cls": "behavior"},
    "adherence_pattern": {"functional": False, "half_life": 120, "sensitivity": "normal", "cls": "behavior"},
    # derived traits the Insight Engine writes (all carry numbers in `value_num`)
    "response_profile": {"functional": False, "half_life": 120, "sensitivity": "normal", "cls": "response"},
    "recovery_profile": {"functional": False, "half_life": 120, "sensitivity": "normal", "cls": "response"},
    "calibration_bias": {"functional": False, "half_life": 90, "sensitivity": "normal", "cls": "response"},
    # episodes
    "happened": {"functional": False, "half_life": 30, "sensitivity": "normal", "cls": "episode"},
    "milestone": {"functional": False, "half_life": 400, "sensitivity": "normal", "cls": "episode"},
    "setback": {"functional": False, "half_life": 120, "sensitivity": "normal", "cls": "episode"},
}
DEFAULT_PREDICATE = {"functional": False, "half_life": 120, "sensitivity": "normal", "cls": "other"}
SUBJECT_KINDS = ("athlete", "exercise", "body_part", "food", "routine", "place", "person", "concept")


def register_predicate(name: str, **spec: Any) -> None:
    """Modules may add predicates at import time (functional, half_life, sensitivity, cls). Existing entries are never overwritten."""
    PREDICATES.setdefault(name, {**DEFAULT_PREDICATE, **spec})


def predicate(name: str) -> dict[str, Any]:
    return PREDICATES.get(name, DEFAULT_PREDICATE)


# ---- shapes that cross module boundaries --------------------------------------------------------------------------------------------------
@dataclass
class Hit:
    """One retrieved memory item. `score` is comparable only within one search; `why` explains the ranking (for debugging and for the UI)."""
    item_type: str                 # one of ITEM_TYPES
    item_id: int
    text: str                      # the sentence an agent can read
    score: float = 0.0
    confidence: float | None = None
    kind: str | None = None
    ts: str | None = None          # valid_from / observed_at, for recency reasoning
    sensitivity: str = "normal"
    why: dict[str, float] = field(default_factory=dict)    # e.g. {"bm25": .., "dense": .., "graph": .., "recency": .., "confidence": ..}
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"type": self.item_type, "id": self.item_id, "text": self.text, "score": round(self.score, 4), "confidence": self.confidence,
                "kind": self.kind, "ts": self.ts, "sensitivity": self.sensitivity, "why": {k: round(v, 4) for k, v in self.why.items()}, "meta": self.meta}


@dataclass
class Pack:
    """What an agent actually receives: a small, budgeted, relevant slice of everything known. Built by mem.pack()."""
    core: list[str] = field(default_factory=list)          # always-on: stable facts, active restrictions, top discoveries
    retrieved: list[Hit] = field(default_factory=list)     # relevant to this query
    situational: list[str] = field(default_factory=list)   # today versus this athlete's normal ("slept 5.2 h: your squat usually drops ~4% after that")
    text: str = ""                                         # the rendered block for a prompt (data, not instructions)
    tokens: int = 0
    dropped: int = 0

    def as_dict(self) -> dict:
        return {"core": self.core, "retrieved": [h.as_dict() for h in self.retrieved], "situational": self.situational, "text": self.text,
                "tokens": self.tokens, "dropped": self.dropped}


def rough_tokens(text: str) -> int:
    """Cheap, model-independent token estimate (~4 chars per token; Devanagari ~2). Used only for budgeting."""
    dev = sum(1 for ch in text if "ऀ" <= ch <= "ॿ")
    return max(1, (len(text) - dev) // 4 + dev // 2)
