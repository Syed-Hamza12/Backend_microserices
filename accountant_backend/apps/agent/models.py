from django.db import models

from apps.accounts.models import Business
from apps.chat.models import ChatMessage, Conversation
from apps.documents.models import DocumentDelivery

# See `GoalManager` in goals.py for the actual state-machine logic — this file
# only holds the schema. `plan` is the ordered step list a Goal is executing
# (see apps/agent/planner.py); it is workflow bookkeeping only (capability
# names, small scalar params, per-step status) and never a copy of business
# data — the ledger (Customer, ActivityEntry, DocumentDelivery, ...) stays the
# single source of truth for everything a step reads or writes.
STATUS_CHOICES = [
    ("planning", "Planning"),
    ("awaiting_clarification", "Awaiting clarification"),
    ("awaiting_authorization", "Awaiting authorization"),
    ("executing", "Executing"),
    ("awaiting_verification", "Awaiting verification"),
    ("done", "Done"),
    ("failed", "Failed"),
]


class AgentGoal(models.Model):
    """One business goal derived from an owner message that needs more than
    an immediate DB write to satisfy — anything with a side effect or an
    async tail (see `apps.agent.planner.plan_from_reply`). A plain answered
    question never gets a row here.
    """

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="agent_goals")
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="agent_goals")
    message = models.ForeignKey(ChatMessage, on_delete=models.CASCADE, related_name="agent_goals")

    # The capability request(s) the LLM's reply named, before resolution.
    raw_request = models.JSONField(default=dict, blank=True)
    # Ordered list of steps: [{"capability": str, "params": dict, "status": str,
    # "output": dict, "waiting_on": dict|None}, ...]. See planner.py/executor.py.
    plan = models.JSONField(default=list, blank=True)
    current_step = models.PositiveSmallIntegerField(default=0)

    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default="planning")
    retry_count = models.PositiveSmallIntegerField(default=0)

    # Set when the current step is a WhatsApp send awaiting the job worker's
    # verification event — the one field the chat layer needs to poll/report on.
    delivery = models.ForeignKey(
        DocumentDelivery, on_delete=models.SET_NULL, null=True, blank=True, related_name="agent_goals"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["conversation", "status"]),
            models.Index(fields=["business", "status"]),
        ]

    def __str__(self):
        return f"AgentGoal({self.id}, {self.status}, step {self.current_step}/{len(self.plan)})"
