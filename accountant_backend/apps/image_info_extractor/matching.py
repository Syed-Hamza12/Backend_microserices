from difflib import SequenceMatcher

from apps.customers.models import Customer

# A match must be at least this good to be treated as "this is that customer".
#
# Raised from 0.6: at that level near-misses like "Ali" vs "Adil" cleared the
# bar, so a photographed bill could be attached — silently, and phrased with
# full confidence — to a different customer's ledger. Money landing on the wrong
# person's account is the failure that ends trust in an AI bookkeeper, so the
# bar for acting without asking is deliberately high.
MATCH_THRESHOLD = 0.85

# The best match must also beat the runner-up by this margin. Two customers
# scoring 0.86 and 0.87 is not a match, it's a question — taking the higher one
# is a coin toss dressed up as a decision.
MATCH_MARGIN = 0.12

# Below this, a name isn't even worth offering as a suggestion.
SUGGESTION_FLOOR = 0.5


def _score(name, candidate_name):
    a = name.strip().lower()
    b = candidate_name.strip().lower()
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def find_matching_customer(business, name):
    """Returns `(customer, candidates)`.

    `customer` is set only when exactly one customer matches clearly enough to
    act on without asking. Otherwise it is None and `candidates` holds the near
    misses, so the caller can ask the owner which one they meant rather than
    guessing on their behalf.
    """
    if not name or not name.strip():
        return None, []

    customers = list(Customer.objects.filter(business=business))

    # An exact name match is decisive and skips the margin rule below. Without
    # this, a customer named "Ali Raza" failed to match the name "Ali Raza"
    # whenever an "Alina Raza" also existed — the two scored close enough that
    # the ambiguity guard rejected a perfect match. Two customers sharing an
    # identical name is genuinely ambiguous, so that still falls through to a
    # question.
    exact = [c for c in customers if c.name.strip().lower() == name.strip().lower()]
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        return None, exact[:3]

    scored = sorted(
        ((_score(name, c.name), c) for c in customers),
        key=lambda pair: pair[0],
        reverse=True,
    )
    if not scored:
        return None, []

    best_score, best_customer = scored[0]
    if best_score < MATCH_THRESHOLD:
        return None, [c for score, c in scored[:3] if score >= SUGGESTION_FLOOR]

    runner_up_score = scored[1][0] if len(scored) > 1 else 0.0
    if best_score - runner_up_score < MATCH_MARGIN:
        # Genuinely ambiguous — hand back the tied candidates to ask about.
        return None, [c for score, c in scored[:3] if score >= MATCH_THRESHOLD - MATCH_MARGIN]

    return best_customer, []
