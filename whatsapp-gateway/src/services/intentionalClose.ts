/**
 * Tracks socket closes that this gateway caused itself, so the connection hooks
 * never mistake one for an unexpected drop and auto-reconnect from it.
 *
 * ## Why this is scoped rather than a bare Set
 *
 * The flag used to be added before a teardown and left for `onClose` to
 * consume. It never was: `destroySocket` detaches the `connection.update`
 * listener *before* ending the socket (deliberately — a dying socket must not
 * fire into the hooks of the session already replacing it), so the close event
 * had nowhere to land and the id stayed in the set forever. The next genuine
 * disconnect then consumed that stale flag and was silently swallowed: no
 * reconnect scheduled, socket left in the registry, session reported CONNECTED
 * with a dead socket underneath it.
 *
 * The window the flag genuinely protects is small but real — between marking
 * the close as ours and the listener actually being detached, and across
 * `socket.logout()` in unlinkSession, which can emit while listeners are still
 * attached. So the flag is kept, and given a lifetime: set for exactly the
 * duration of the teardown, cleared unconditionally afterwards.
 *
 * This module holds no reconnect logic of its own and must never gain any.
 */

const pending = new Set<string>();

/**
 * Marks [sessionId]'s next close as intentional for the duration of
 * [teardown], and clears the mark afterwards — including when [teardown]
 * throws, since a failed teardown must not leave a suppression flag behind.
 */
export async function withIntentionalClose(
    sessionId: string,
    teardown: () => Promise<void>
): Promise<void> {
    pending.add(sessionId);
    try {
        await teardown();
    } finally {
        pending.delete(sessionId);
    }
}

/**
 * True if this close was one of ours, clearing the mark as it reports it.
 *
 * Kept consuming (rather than a plain read) so that a close which *does* land
 * inside the teardown window is only ever excused once.
 */
export function consumeIntentionalClose(sessionId: string): boolean {
    return pending.delete(sessionId);
}

/** Test seam: whether a suppression flag is currently outstanding. */
export function hasPendingIntentionalClose(sessionId: string): boolean {
    return pending.has(sessionId);
}
