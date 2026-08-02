"""GoalManager — the only write path to `AgentGoal`, so every status
transition is auditable. Owns tracking (current step, completed/pending
steps, retries) for anything the Planner/Executor composed; never business
data itself (see apps/agent/models.py's module docstring).
"""

from .models import AgentGoal


def _json_safe(output):
    """Keeps only plain-scalar, public (non "_"-prefixed) keys — model
    instances (Customer, ActivityEntry) that resolvers pass around internally
    are never written to a JSONField. Workflow bookkeeping only, per the
    architecture: the ledger stays the single source of truth for anything
    dropped here.
    """
    safe = {}
    for key, value in (output or {}).items():
        if key.startswith("_"):
            continue
        if value is None or isinstance(value, (str, int, float, bool)):
            safe[key] = value
        elif hasattr(value, "isoformat"):
            safe[key] = value.isoformat()
    return safe


class GoalManager:
    @staticmethod
    def start(*, conversation, message, plan_step_names, raw_request=None):
        goal = AgentGoal.objects.create(
            business=conversation.business,
            conversation=conversation,
            message=message,
            raw_request=raw_request or {},
            plan=[{"capability": name, "status": "pending", "output": {}} for name in plan_step_names],
            status="executing",
        )
        return goal

    @staticmethod
    def record_step(goal, index, outcome):
        plan = goal.plan
        if outcome.waiting_on:
            plan[index]["status"] = "awaiting_verification"
        else:
            plan[index]["status"] = "done" if outcome.success else "failed"
        plan[index]["output"] = _json_safe(outcome.output)
        goal.plan = plan
        goal.current_step = index if outcome.waiting_on or not outcome.success else index + 1
        goal.save(update_fields=["plan", "current_step", "updated_at"])

    @staticmethod
    def advance(goal, *, status, delivery_id=None):
        goal.status = status
        update_fields = ["status", "updated_at"]
        if delivery_id is not None:
            from apps.documents.models import DocumentDelivery

            goal.delivery = DocumentDelivery.objects.filter(pk=delivery_id).first()
            update_fields.append("delivery")
        goal.save(update_fields=update_fields)

    @staticmethod
    def handle_event(event_type, payload):
        """Called from `apps.documents.delivery.handle_document_send_job` at
        its existing success/failure points (see the added call there) — the
        single entry point every async completion advances a Goal through.
        This is where "success" is first allowed to be reported for an async
        capability; nothing before this point may claim the send is done.
        """
        if event_type not in ("document_delivery_accepted", "document_delivery_failed"):
            return None
        delivery_id = payload.get("delivery_id")
        goal = AgentGoal.objects.filter(delivery_id=delivery_id, status="awaiting_verification").first()
        if goal is None:
            return None

        success = event_type == "document_delivery_accepted"
        plan = goal.plan
        if plan:
            plan[-1]["status"] = "done" if success else "failed"
            goal.plan = plan
        goal.status = "done" if success else "failed"
        goal.current_step = len(plan)
        goal.save(update_fields=["plan", "status", "current_step", "updated_at"])
        return goal
