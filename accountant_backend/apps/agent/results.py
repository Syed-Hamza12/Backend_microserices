"""Small shared result types — kept dependency-free (no imports from
capabilities/planner/executor) so every one of those modules can import from
here without risk of a cycle.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Clarification:
    """A capability's `resolve()` returns this when it genuinely cannot
    proceed — ambiguous customer, no matching entry, WhatsApp not connected,
    an impossible date range. Never a guess. `message` is shown to the owner
    verbatim as the reply's `text` (already phrased in business language by
    the resolver, not a code)."""

    message: str
    code: str = "CLARIFICATION_NEEDED"


@dataclass
class Outcome:
    """What running one capability produced. `output` feeds forward into
    later steps' `have` dict (see planner.py's backward-chaining). `text` is
    optional — most steps are silent; only the terminal step of a plan
    usually sets it, and the executor uses that as the AI reply's text."""

    success: bool
    output: dict[str, Any] = field(default_factory=dict)
    text: str | None = None
    pending_delivery_id: int | None = None
    # Set when this step's own completion must wait for an external event
    # (see apps.agent.goals.GoalManager.handle_event) rather than being known
    # right now — e.g. a queued WhatsApp send.
    waiting_on: dict | None = None
