import { Boom } from "@hapi/boom";
import { DisconnectReason } from "@whiskeysockets/baileys";
import type { ConnectionState } from "@whiskeysockets/baileys";

export interface ConnectionHooks {
    onQr: (qr: string) => void;
    onConnecting: () => void;
    onOpen: (phone: string | undefined) => void;
    /** `statusCode` is passed through purely so it lands in the logs — diagnosing a
     *  reconnect problem without knowing which code WhatsApp sent is guesswork. */
    onClose: (shouldReconnect: boolean, statusCode: number | undefined) => void;
    onLoggedOut: () => void;
}

/**
 * Disconnect reasons that are safe to retry: a transient network/server-side
 * condition where reconnecting is what WhatsApp's own client would do.
 *
 * Everything NOT in this set is treated as terminal — including unrecognised
 * status codes. That default direction matters: retrying something WhatsApp
 * meant as "stop" is what gets a number banned, while failing to retry
 * something transient only costs one manual reconnect tap.
 */
const RETRYABLE_STATUS_CODES = new Set<number>([
    DisconnectReason.connectionClosed, // 428
    DisconnectReason.connectionLost, // 408
    DisconnectReason.timedOut, // 408
    DisconnectReason.restartRequired, // 515 — Baileys' normal post-pairing restart
    DisconnectReason.unavailableService // 503
]);

/**
 * Reasons that mean "this credential is finished" — the session files are no
 * longer usable and reconnecting with them can only produce more rejections.
 * `connectionReplaced` is deliberately here: another device took this session
 * over, and reconnecting starts a tug-of-war where each socket kicks the other
 * in a loop, which is the single fastest way to get flagged.
 */
const TERMINAL_STATUS_CODES = new Set<number>([
    DisconnectReason.loggedOut, // 401
    DisconnectReason.badSession, // 500
    DisconnectReason.connectionReplaced, // 440
    DisconnectReason.multideviceMismatch, // 411
    DisconnectReason.forbidden // 403
]);

export function isTerminalStatusCode(statusCode: number | undefined): boolean {
    if (statusCode === undefined) return true;
    if (TERMINAL_STATUS_CODES.has(statusCode)) return true;
    return !RETRYABLE_STATUS_CODES.has(statusCode);
}

export function handleConnectionUpdate(update: Partial<ConnectionState>, hooks: ConnectionHooks): void {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
        hooks.onQr(qr);
        return;
    }

    if (connection === "connecting") {
        hooks.onConnecting();
        return;
    }

    if (connection === "close") {
        const statusCode = (lastDisconnect?.error as Boom | undefined)?.output?.statusCode;

        if (statusCode === DisconnectReason.loggedOut) {
            hooks.onLoggedOut();
            return;
        }

        hooks.onClose(!isTerminalStatusCode(statusCode), statusCode);
    }
}
