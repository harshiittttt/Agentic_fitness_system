"""The typed contract every agent speaks: a Proposal. Agents never write state, they submit these."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Kind = Literal["volume_change", "calorie_change", "swap_exercise", "restriction", "note", "new_block", "escalate", "user_change", "nudge"]
Tier = Literal["auto", "notify_first", "approve", "escalate"]

# What each agent may propose (the "capability token": Nutrition can't move a workout).
SCOPES: dict[str, set[str]] = {
    "training": {"volume_change", "swap_exercise", "restriction", "new_block", "note"},
    "nutrition": {"calorie_change", "note"},
    "recovery": {"volume_change", "restriction", "escalate", "note"},
    "progress": {"note"},
    "adherence": {"nudge", "note"},
    "injury": {"restriction", "swap_exercise", "escalate", "note"},
    "periodization": {"volume_change", "new_block", "note"},     # block / deload / taper structure; never calories or exercises
    "logistics": {"nudge", "note", "volume_change"},              # travel, schedule, equipment, time-boxing; a cut only in practice
    "sleep": {"nudge", "note", "volume_change"},
    "research": {"note"},                                          # evidence flags only; Intake writes through the profile-edit path, not proposals
    "intake": {"note"},
    "form": {"note", "nudge"},                                      # hedged technique cues only; never load, calories or exercises, never a diagnosis
    "orchestrator": {"volume_change", "calorie_change", "swap_exercise", "restriction", "new_block", "note", "escalate", "nudge"},
    "safety_critic": {"escalate", "note"},
    "watchdog": {"restriction", "escalate", "volume_change", "note"},
}


class Proposal(BaseModel):
    agent: str
    kind: Kind
    params: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(min_length=3)
    evidence: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    def summary(self) -> str:
        p = self.params
        if self.kind == "volume_change":
            return f"Volume {p.get('fraction', 0):+.0%}"
        if self.kind == "calorie_change":
            return f"Calories {p.get('fraction', 0):+.0%}"
        if self.kind == "swap_exercise":
            return f"Swap {p.get('exercise')} -> {p.get('replacement')}"
        if self.kind == "restriction":
            return f"Restriction: {p.get('text')}"
        if self.kind == "user_change":
            return str(p.get("summary") or p.get("op") or "Your change")[:160]
        if p.get("revert_of"):
            return str(p.get("text") or "Undid a change")[:160]
        if self.kind == "nudge":
            return f"Message: {str(p.get('text', ''))[:100]}"
        if self.kind == "escalate":
            return f"Escalate {p.get('domain', 'all')}: {p.get('reason', '')}"
        return f"{self.kind}: {p.get('text', '')}"[:120]


class Verdict(BaseModel):
    verdict: Literal["allow", "allow_with_limits", "block"]
    tier: Tier
    fired: list[str] = Field(default_factory=list)
    veto_window_minutes: int | None = None
    explain: str = ""
    params: dict[str, Any] = Field(default_factory=dict)  # params after limits were applied
