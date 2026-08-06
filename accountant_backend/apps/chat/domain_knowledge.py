"""Business Domain Knowledge Provider.

Loads a condensed, token-efficient summary of the markdown reference
document matching a business's `business_type`
(`docs/business_domains/<BUSINESS_TYPE>.md`) for injection into the chat
system prompt — see `apps.chat.prompt.build_system_prompt`'s context
ordering:

    System prompt (OUTPUT_CONTRACT_INSTRUCTIONS)
    -> Business Domain Knowledge (this module)
    -> Business Special Instructions (Business.special_instructions,
       owner-written, always wins on conflict)
    -> Current ledger context
    -> Current user message

The markdown files under `docs/business_domains/` are reference documents
for humans extending this system, not prompts — a full file runs several
hundred lines, most of it a conversation-example catalog meant to document
realistic Pakistani market behaviour for whoever writes the next domain
file, not to be pasted into an LLM call verbatim (see each file's own
"IMPORTANT" note). This module extracts only the sections that actually
steer model behaviour — vocabulary, spelling variants, clarification
rules, common mistakes, best practices — and drops the rest, keeping the
injected text to a few hundred tokens instead of several thousand.

Exactly one document is ever loaded per request, chosen by
`business.business_type` alone. A business with no type set (blank, the
default) or a type with no matching file yet gets an empty string — the
same "inject nothing rather than guess" pattern every other optional
context block in `prompt.py` already follows.
"""

import logging
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

DOMAIN_DOCS_DIR = Path(settings.BASE_DIR) / "docs" / "business_domains"

# Markdown H1 ("# Heading") sections pulled into the condensed summary, in
# this order. Deliberately excludes "# Conversation Examples" (the largest
# section by far — a catalog for human maintainers, not model steering) and
# "# Business Overview"/"# Typical Customers"/"# Typical Products"/etc.
# (descriptive background a 70B model already has from its own training on
# Pakistani commerce; not the part that changes how it should ask/act).
_SUMMARY_SECTIONS = [
    "Common Units",
    "Common Pakistani Vocabulary",
    "Roman Urdu Variations",
    "Product Nicknames",
    "Owner Behaviour",
    "Common Payment Behaviour",
    "Outstanding Balance Behaviour",
    "Clarification Rules",
    "Frequently Forgotten Information",
    "Common AI Mistakes",
    "Best Practices",
]

# business_type -> (file mtime at last read, condensed summary). Only ever
# holds one entry per type actually queried — a handful of entries at most
# for a real deployment's mix of business types, so no eviction policy is
# needed. Keyed on mtime (not just business_type) so an edited markdown
# file is picked up on the next request without a server restart, while a
# business's 50th message today doesn't re-parse and re-condense the same
# unchanged file 50 times.
_cache = {}


def _parse_sections(markdown_text):
    """{heading: body} for every level-1 ("# ") heading in the file."""
    sections = {}
    heading = None
    lines = []
    for line in markdown_text.splitlines():
        if line.startswith("# "):
            if heading is not None:
                sections[heading] = "\n".join(lines).strip()
            heading = line[2:].strip()
            lines = []
        else:
            lines.append(line)
    if heading is not None:
        sections[heading] = "\n".join(lines).strip()
    return sections


def _condense(markdown_text):
    sections = _parse_sections(markdown_text)
    parts = [
        f"{heading}:\n{sections[heading]}"
        for heading in _SUMMARY_SECTIONS
        if sections.get(heading)
    ]
    return "\n\n".join(parts)


def available_business_types():
    """business_type codes that currently have a matching markdown file —
    used only for diagnostics/admin, never by the chat request path (which
    always goes through `get_domain_context` and degrades gracefully on a
    miss instead of needing to check availability first)."""
    if not DOMAIN_DOCS_DIR.is_dir():
        return []
    # "_TEMPLATE.md" is the authoring template (see its own header comment),
    # never a real business_type value stored on any Business — excluded so
    # this list only ever reports codes that would actually resolve.
    return sorted(p.stem for p in DOMAIN_DOCS_DIR.glob("*.md") if not p.stem.startswith("_"))


def get_domain_context(business_type):
    """The condensed domain-knowledge text for this `business_type`, or ""
    if it's unset, unrecognised, or has no matching document yet. The
    caller treats an empty string as "inject nothing" — never raises, never
    blocks a chat reply on a missing or malformed file.
    """
    if not business_type:
        return ""

    path = DOMAIN_DOCS_DIR / f"{business_type}.md"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ""

    cached = _cache.get(business_type)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 - a bad doc file must never break chat
        logger.warning("failed to read domain doc %s: %s", path, exc)
        return ""

    summary = _condense(text)
    _cache[business_type] = (mtime, summary)
    return summary
