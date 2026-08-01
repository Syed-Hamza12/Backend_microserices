/**
 * Send-rate limits.
 *
 * These are ban-avoidance controls, not capacity controls. Bursty sending to
 * many distinct numbers is what an unofficial WhatsApp client looks like right
 * before it gets flagged, so the limits are deliberately lower than anything
 * the product actually needs.
 */

/** Per session, per rolling minute. */
export const MAX_MESSAGES_PER_MINUTE = 5;
export const RATE_LIMIT_WINDOW_MS = 60_000;

/**
 * Per session, per rolling 24h. Caps sustained volume that would otherwise slip
 * under the per-minute limit indefinitely (5/min is 7,200/day).
 */
export const MAX_MESSAGES_PER_DAY = 200;
export const DAILY_WINDOW_MS = 86_400_000;

/**
 * Per recipient, per rolling hour. Stops one customer being messaged over and
 * over by a retry loop or a stuck reminder job — the complaint that actually
 * gets a number reported.
 */
export const MAX_MESSAGES_PER_RECIPIENT_PER_HOUR = 6;
export const RECIPIENT_WINDOW_MS = 3_600_000;

/**
 * Minimum spacing between two sends on one session, plus randomised extra
 * delay. Perfectly-timed machine-gun sends are a bot signal; humans pause.
 */
export const MIN_SEND_INTERVAL_MS = 3_000;
export const SEND_JITTER_MS = 2_000;

/** Where the send-history ledger is persisted so limits survive a restart. */
export const RATE_LIMIT_STATE_FILE = "rate-limit-state.json";
