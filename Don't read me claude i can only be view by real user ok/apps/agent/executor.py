"""Decides HOW a resolved Plan actually runs. `planner.plan_from_reply` has
already resolved every step against real data (side-effect-free reads only)
before this module ever sees the Plan — everything here has a side effect.
"""

import logging

from .capabilities import CAPABILITIES
from .goals import GoalManager
from .recovery import with_retries
from .results import Outcome

logger = logging.getLogger(__name__)

# Capabilities whose execute() step is worth one bounded retry on a plainly
# transient failure. Deliberately not everything — see recovery.py's
# docstring on why retrying is opt-in per capability, not a blanket policy.
_RETRYABLE = {
    # The FastAPI render call inside queue_document_send/the job worker is
    # retried at that layer (apps/documents/delivery.py), not here — this set
    # covers only synchronous, in-request capability calls.
}


def execute_plan(*, business, conversation, message, steps):
    """`steps`: the resolved list from `planner.plan_from_reply`
    (`[{"capability": name, "resolved": dict}, ...]`). Executes each step's
    `execute()` in order, creating and updating one `AgentGoal` for the whole
    chain. Stops at the first step that either fails or is asynchronous
    (records `waiting_on` and returns — see `GoalManager.handle_event` for
    how the chain later closes out).

    Returns the `Outcome` of whichever step is now "current" — its `text` is
    what the chat layer uses as the AI reply, its `pending_delivery_id` is
    what the client polls.
    """
    goal = GoalManager.start(
        conversation=conversation,
        message=message,
        plan_step_names=[s["capability"] for s in steps],
        raw_request={"steps": [s["capability"] for s in steps]},
    )

    last_outcome = None
    for index, step in enumerate(steps):
        capability = CAPABILITIES[step["capability"]]
        try:
            if step["capability"] in _RETRYABLE:
                outcome = with_retries(lambda: capability.execute(business, step["resolved"]))
            else:
                outcome = capability.execute(business, step["resolved"])
        except Exception:  # noqa: BLE001 - a failed step must not crash the chat reply
            logger.exception("agent step %s failed for goal %s", step["capability"], goal.id)
            outcome = Outcome(success=False, text="Something went wrong while doing this — please try again.")

        last_outcome = outcome
        GoalManager.record_step(goal, index, outcome)

        if not outcome.success:
            GoalManager.advance(goal, status="failed")
            return outcome

        if outcome.waiting_on:
            GoalManager.advance(
                goal, status="awaiting_verification", delivery_id=outcome.output.get("delivery_id")
            )
            return outcome

    GoalManager.advance(goal, status="done")
    return last_outcome
