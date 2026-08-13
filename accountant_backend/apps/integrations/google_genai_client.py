"""Canonical Google Generative Language API client — the key-rotation and
model-fallback mechanics shared by every caller of a Google-hosted model in
this codebase...
"""

import time

from google import genai

_clients = {}

# key -> unix timestamp when this key's cooldown ends. A key only enters
# this dict when it fails with a quota/rate-limit error (429 /
# RESOURCE_EXHAUSTED) — a busy-model 503 or any other error does NOT cool
# the key down, since that's the model's problem, not this key's quota.
_cooldowns = {}

# Free-tier per-minute quota resets after 60s — this only needs to outlast
# that window, not guess at daily limits (a daily 429 would just keep
# failing every retry after cooldown too, which is correct: the loop below
# tries it again, gets 429 again, re-cools it, and moves on).
COOLDOWN_SECONDS = 60


def client_for(key):
    if key not in _clients:
        _clients[key] = genai.Client(api_key=key)
    return _clients[key]


def model_ladder(primary, fallbacks=None):
    seen, ordered = set(), []
    for name in [primary, *(fallbacks or [])]:
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _is_on_cooldown(key):
    until = _cooldowns.get(key)
    if until is None:
        return False
    if time.monotonic() >= until:
        # Cooldown expired — release it. Deleting here (rather than a
        # separate cleanup pass) means a key silently becomes available
        # again on the very next call that checks it, no background task
        # needed.
        del _cooldowns[key]
        return False
    return True


def _is_quota_error(exc):
    """True only for a quota/rate-limit failure — 429 / RESOURCE_EXHAUSTED.
    Deliberately narrow: a 503 (model busy), a network error, or a bad
    request must never cool a perfectly good key down for 60s over a
    problem that has nothing to do with that key's quota."""
    text = str(exc).upper()
    return "429" in text or "RESOURCE_EXHAUSTED" in text or "QUOTA" in text


def _start_cooldown(key, logger=None):
    _cooldowns[key] = time.monotonic() + COOLDOWN_SECONDS
    if logger:
        logger.info("google genai key cooling down for %ss (quota exhausted)", COOLDOWN_SECONDS)


def generate(keys, models, contents, *, config=None, logger=None):
    """Runs `contents` through the model/key ladder, returning the raw
    `google.genai` response. Ordered model-first (see original docstring
    reasoning on 503s).

    Keys currently on a quota cooldown are skipped without being attempted
    — cheaper than calling out to Google just to watch it 429 again inside
    the same minute, and it stops one already-exhausted key from eating a
    model's position in the ladder before a fresh key even gets a turn.
    If every key for a given model is on cooldown, that model is skipped
    entirely and the next model in the ladder is tried with the same key
    list (a cooled-down key may still work against a different model, since
    Gemini free-tier quotas are typically per-model).

    Raises RuntimeError if `keys` is empty. Otherwise re-raises the last
    error seen once every (model, key) combination has either failed or
    was skipped on cooldown with nothing else to try.
    """
    if not keys:
        raise RuntimeError("No Google API key configured on this server.")

    last_error = None
    for model in models:
        for idx, key in enumerate(keys):
            if _is_on_cooldown(key):
                if logger:
                    logger.info("google genai key_index=%s skipped (on cooldown) model=%s", idx, model)
                continue
            try:
                response = client_for(key).models.generate_content(
                    model=model, contents=contents, config=config
                )
                if logger:
                    logger.info("google genai call ok key_index=%s model=%s", idx, model)
                return response
            except Exception as exc:  # noqa: BLE001 - any failure tries the next key/model
                last_error = exc
                if _is_quota_error(exc):
                    _start_cooldown(key, logger=logger)
                if logger:
                    logger.warning(
                        "google genai call failed key_index=%s model=%s error=%s", idx, model, exc
                    )

    if last_error is None:
        # Every key was already on cooldown before we even tried one —
        # there's no underlying exception to re-raise, so say so plainly.
        last_error = RuntimeError(
            "All configured Google API keys are on cooldown (quota exhausted); "
            "try again shortly."
        )
    raise last_error