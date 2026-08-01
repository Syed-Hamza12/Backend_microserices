# Start Prompt — paste this into Claude Code CLI for a new session

You are working as the backend engineer (think: a new accountant employee who's been handed the
company handbook) on an AI-powered Automated Accountant SaaS for SMEs in Pakistan.

Before writing any code, read these files in `docs/` in this order:
1. `project_vision.md` — what we're building and why
2. `ARCHITECTURE.md` — the three-service shape (Django, FastAPI, WhatsApp Gateway) and why
3. `claude_rule.md` — hard rules for how you write code in this repo, follow these unconditionally
4. `backend_workflow.md` — step-by-step request lifecycles for every major flow
5. `sqlite_database_attributes.md` — the schema
6. `business_logic.md` — plan/feature-gating rules and core domain rules (balance math, draft bill lifecycle)
7. `ai_automation_layer.md` — how the Groq/Gemini pieces are composed and prompted
8. `feature_list.md` — what's gated vs always-available
9. `milestones.md` — the build order

Also read (existing, not written by you) in docs for guidence folder:
- `BACKEND_INTEGRATION_GUIDE.md` — the exact contract the Flutter app expects from Django, per 
`whatsapp Gateway Api testing guide Api_testing.md`
domain
- `USER_WORKFLOW.md` — how a business owner actually uses the app, screen by screen
- The WhatsApp Gateway's own Postman testing guide — the API you're proxying in `apps/whatsapp`

## Current state
- Flutter app: built, working against mock repositories only, ready to be pointed at real Django endpoints one repository at a time.
- WhatsApp Gateway microservice: already built and tested per its own Postman guide.
- Django backend, FastAPI microservice: not started — this is what you're building.

## What to do first
Start at Milestone 1 in `milestones.md`. Do not skip ahead to AI features before Customers/Sales
(Milestones 2–3) are solid — everything later depends on that data being correct.

## Ground rules (expanded in claude_rule.md, summarized here)
- Every business's data must be isolated by a `business` FK filter on every queryset — no exceptions.
- No raw SQL, `DecimalField` for money, timezone-aware timestamps.
- PDF generation and image extraction always go through the `jobs` app's `JobTask` — never call FastAPI directly from a request/response view.
- AI never writes to the database directly — only the owner's explicit "Confirm and Send" does.
- Feature-gate checks happen before any paid external API call, never after.
- When something is ambiguous or would lock in a hard-to-reverse decision, stop and ask instead of guessing.

## Deliverable style
Work milestone by milestone. At the end of each milestone, tell me exactly what's runnable/testable
and how to test it (e.g. "run `python manage.py runworker` in one terminal, `python manage.py
runserver` in another, then hit these endpoints in this order"). Update any `docs/` file that your
changes make stale in the same commit. When a milestone's deliverable is genuinely working
end-to-end (not just code written), tick its `- [ ] Status: Done` checkbox in `milestones.md` —
see that file's top note for the exact rule (never tick speculatively/partially).
