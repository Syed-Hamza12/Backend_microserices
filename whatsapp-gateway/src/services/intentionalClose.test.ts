import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
    consumeIntentionalClose,
    hasPendingIntentionalClose,
    withIntentionalClose
} from "./intentionalClose.js";

/**
 * Regression tests for the suppression flag that stops a close we caused
 * ourselves from being auto-reconnected.
 *
 * The bug these exist for: the flag was added before a teardown and left for
 * the `onClose` hook to consume, but `destroySocket` detaches that listener
 * before ending the socket — so the close never arrived, the flag was never
 * consumed, and the *next genuine* disconnect was silently swallowed. The
 * session then sat reporting CONNECTED over a dead socket with no reconnect
 * scheduled.
 *
 * The invariant, in one line: after any teardown completes, no flag survives.
 */
describe("intentional close flag lifecycle", () => {
    it("clears the flag once the teardown completes", async () => {
        await withIntentionalClose("session-a", async () => {});

        assert.equal(
            hasPendingIntentionalClose("session-a"),
            false,
            "a flag left behind here is what swallowed the next real disconnect"
        );
    });

    it("holds the flag for the whole teardown, not just its start", async () => {
        let flaggedDuringTeardown = false;

        await withIntentionalClose("session-b", async () => {
            // Stand-in for destroySocket's async work — and, in unlinkSession,
            // for socket.logout(), which can emit a close while the listeners
            // are still attached. The flag has to cover all of it.
            await Promise.resolve();
            flaggedDuringTeardown = hasPendingIntentionalClose("session-b");
        });

        assert.equal(flaggedDuringTeardown, true, "the suppression window must be preserved");
        assert.equal(hasPendingIntentionalClose("session-b"), false);
    });

    it("clears the flag even when the teardown throws", async () => {
        await assert.rejects(
            withIntentionalClose("session-c", async () => {
                throw new Error("socket refused to close");
            })
        );

        assert.equal(
            hasPendingIntentionalClose("session-c"),
            false,
            "a failed teardown must not leave a permanent reconnect suppressor"
        );
    });

    it("survives socket replacement: repeated teardowns leave nothing behind", async () => {
        // openSocket tears down the previous socket every time it reconnects,
        // which is exactly how the old implementation accumulated flags.
        for (let i = 0; i < 5; i++) {
            await withIntentionalClose("session-d", async () => {});
        }

        assert.equal(hasPendingIntentionalClose("session-d"), false);
    });

    it("does not excuse a disconnect that happens after the teardown", async () => {
        await withIntentionalClose("session-e", async () => {});

        // This is the real-world assertion: the next close is genuine, so the
        // hook must NOT treat it as ours. Returning true here is the bug —
        // it means no reconnect is scheduled and the socket is never cleaned up.
        assert.equal(consumeIntentionalClose("session-e"), false);
    });

    it("still excuses a close that lands inside the teardown window", async () => {
        let excused: boolean | undefined;

        await withIntentionalClose("session-f", async () => {
            // A close arriving before the listener is detached — the narrow
            // case the flag genuinely exists for.
            excused = consumeIntentionalClose("session-f");
        });

        assert.equal(excused, true, "removing the flag entirely would reconnect from our own close");
    });

    it("excuses such a close only once", async () => {
        await withIntentionalClose("session-g", async () => {
            assert.equal(consumeIntentionalClose("session-g"), true);
            assert.equal(consumeIntentionalClose("session-g"), false);
        });

        assert.equal(hasPendingIntentionalClose("session-g"), false);
    });

    it("keeps sessions independent", async () => {
        await withIntentionalClose("session-h", async () => {
            assert.equal(
                hasPendingIntentionalClose("session-i"),
                false,
                "one session's teardown must not suppress another's reconnect"
            );
        });
    });
});
