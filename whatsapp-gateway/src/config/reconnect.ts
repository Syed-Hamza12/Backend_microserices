/**
 * Reconnect governor tuning.
 *
 * Context for why every one of these exists: an earlier version of
 * session.service.ts reconnected immediately and recursively on close, with no
 * delay, no attempt cap and no give-up state. In a single 17.8-minute run that
 * produced 335 login attempts against WhatsApp on one real number (~19/min) —
 * the exact pattern WhatsApp bans numbers for. Nothing here may be loosened
 * without understanding that.
 */

/** First retry waits this long. Doubles each attempt, up to RECONNECT_MAX_DELAY_MS. */
export const RECONNECT_BASE_DELAY_MS = 5_000;

/** Ceiling for the exponential backoff. */
export const RECONNECT_MAX_DELAY_MS = 300_000;

/**
 * Consecutive automatic attempts before the session gives up and parks in
 * ERROR, requiring an explicit POST /sessions/:id/connect from a human. With
 * the delays above that spans roughly 5s + 10s + 20s + 40s + 80s + 160s — about
 * five minutes of trying, then silence.
 */
export const RECONNECT_MAX_ATTEMPTS = 6;

/**
 * Randomises each delay by ±30% so several sessions dropping at once (a gateway
 * restart, a WhatsApp-side blip) don't all retry on the same tick.
 */
export const RECONNECT_JITTER_RATIO = 0.3;

/**
 * How many times a session may hand out a fresh QR before giving up. Without
 * this, a QR nobody ever scans expires, closes the connection, and gets
 * reconnected into a new QR forever — an unattended screen would hammer
 * WhatsApp indefinitely.
 */
export const QR_MAX_CYCLES = 3;

/**
 * Circuit breaker across ALL sessions. If WhatsApp rejects this many connection
 * attempts gateway-wide inside COOLDOWN_WINDOW_MS, every session stops
 * reconnecting for COOLDOWN_DURATION_MS. This is the backstop for the case the
 * per-session cap can't see: many sessions each failing "acceptably" while the
 * gateway as a whole looks like an attack.
 */
export const COOLDOWN_FAILURE_THRESHOLD = 10;
export const COOLDOWN_WINDOW_MS = 600_000;
export const COOLDOWN_DURATION_MS = 1_800_000;

/**
 * Gap between sessions when restoring from disk on boot, so a gateway with
 * several linked numbers doesn't open every socket in the same instant.
 */
export const RESTORE_STAGGER_MS = 15_000;

/**
 * Restoring sessions on boot means a crash-loop (or `tsx watch` reloading on
 * every file save, as in local dev) turns into a reconnect storm. Off by
 * default; set RESTORE_SESSIONS_ON_BOOT=true on a real deployment where the
 * process is stable and supervised.
 */
export const RESTORE_ON_BOOT_ENV = "RESTORE_SESSIONS_ON_BOOT";
