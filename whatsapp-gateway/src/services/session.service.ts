import { randomUUID } from "crypto";

import type { WASocket } from "@whiskeysockets/baileys";

import { createSocket, destroySocket } from "../baileys/socket.factory.js";
import { env } from "../config/env.js";
import {
    COOLDOWN_DURATION_MS,
    COOLDOWN_FAILURE_THRESHOLD,
    COOLDOWN_WINDOW_MS,
    QR_MAX_CYCLES,
    RECONNECT_BASE_DELAY_MS,
    RECONNECT_JITTER_RATIO,
    RECONNECT_MAX_ATTEMPTS,
    RECONNECT_MAX_DELAY_MS,
    RESTORE_STAGGER_MS
} from "../config/reconnect.js";
import logger from "../logger/logger.js";
import { storageProvider } from "../storage/index.js";
import { SessionStatus, type GatewaySession } from "../types/session.types.js";
import { ApiError } from "../utils/ApiError.js";
import { consumeIntentionalClose, withIntentionalClose } from "./intentionalClose.js";

const sessions = new Map<string, GatewaySession>();
const sockets = new Map<string, WASocket>();

/** Session ids with a disconnect/unlink currently in progress — guards against double-invocation races. */
const pendingOps = new Set<string>();

/**
 * Session ids with a socket currently being opened. Without this, two
 * overlapping connect calls (the app's Connect button tapped twice, or a tap
 * landing while a scheduled reconnect is mid-flight) each open a Baileys socket
 * on the SAME credentials. WhatsApp resolves that by kicking both, each close
 * schedules another reconnect, and the sockets multiply — the fastest known way
 * to get a number flagged.
 */
const connectingSessions = new Set<string>();

/** Pending reconnect timers, kept so disconnect/unlink can cancel a retry that hasn't fired yet. */
const reconnectTimers = new Map<string, NodeJS.Timeout>();

/**
 * Socket closes caused by our own openSocket/disconnectSession/unlinkSession
 * call, so the connection hooks never mistake one for an unexpected drop and
 * auto-reconnect from it. Scoped to the teardown it covers — see
 * services/intentionalClose.ts for why an unscoped flag leaked and what that
 * broke.
 */

/** Timestamps of recent gateway-wide connection failures, for the circuit breaker. */
let recentFailures: number[] = [];
let cooldownUntil = 0;

const ACTIVE_STATUSES = new Set<SessionStatus>([
    SessionStatus.CONNECTING,
    SessionStatus.QR_READY,
    SessionStatus.CONNECTED
]);

function touch(session: GatewaySession): void {
    session.updatedAt = new Date();
}

/**
 * Records one failed connection attempt and reports whether the gateway is now
 * in cooldown. Deliberately counted across all sessions: the per-session
 * attempt cap can't see a fleet of sessions each failing "acceptably" while the
 * gateway as a whole looks abusive from WhatsApp's side.
 */
function registerFailureAndCheckCooldown(): boolean {
    const now = Date.now();
    recentFailures = recentFailures.filter((ts) => ts > now - COOLDOWN_WINDOW_MS);
    recentFailures.push(now);

    if (recentFailures.length >= COOLDOWN_FAILURE_THRESHOLD && now >= cooldownUntil) {
        cooldownUntil = now + COOLDOWN_DURATION_MS;
        recentFailures = [];
        logger.error(
            { cooldownUntil: new Date(cooldownUntil).toISOString(), threshold: COOLDOWN_FAILURE_THRESHOLD },
            "Gateway-wide reconnect cooldown engaged — too many connection failures. All automatic reconnects suspended."
        );
    }

    return isInCooldown();
}

export function isInCooldown(): boolean {
    return Date.now() < cooldownUntil;
}

export function cooldownRemainingMs(): number {
    return Math.max(0, cooldownUntil - Date.now());
}

function backoffDelayMs(attempt: number): number {
    const exponential = Math.min(RECONNECT_BASE_DELAY_MS * 2 ** (attempt - 1), RECONNECT_MAX_DELAY_MS);
    const jitter = exponential * RECONNECT_JITTER_RATIO * (Math.random() * 2 - 1);
    return Math.max(RECONNECT_BASE_DELAY_MS, Math.round(exponential + jitter));
}

function giveUp(session: GatewaySession, reason: string): void {
    session.status = SessionStatus.ERROR;
    session.needsManualReconnect = true;
    session.lastError = reason;
    delete session.qr;
    touch(session);
    logger.error({ sessionId: session.id, reason }, "Session parked — manual reconnect required.");
}

/**
 * Schedules the next automatic reconnect, or gives up. Every automatic
 * reconnect in this service goes through here — nothing calls connectSession
 * recursively, which is what previously produced an unbounded immediate retry
 * loop.
 */
function scheduleReconnect(sessionId: string, statusCode: number | undefined): void {
    const session = sessions.get(sessionId);
    if (!session) return;

    if (registerFailureAndCheckCooldown()) {
        giveUp(session, `Gateway in cooldown after repeated connection failures (last status ${statusCode ?? "unknown"}).`);
        return;
    }

    session.reconnectAttempts += 1;
    if (session.reconnectAttempts > RECONNECT_MAX_ATTEMPTS) {
        giveUp(
            session,
            `Gave up after ${RECONNECT_MAX_ATTEMPTS} reconnect attempts (last status ${statusCode ?? "unknown"}).`
        );
        return;
    }

    const delay = backoffDelayMs(session.reconnectAttempts);
    session.status = SessionStatus.RECONNECTING;
    session.lastError = `Disconnected (status ${statusCode ?? "unknown"}); retrying.`;
    touch(session);

    logger.warn(
        { sessionId, attempt: session.reconnectAttempts, maxAttempts: RECONNECT_MAX_ATTEMPTS, delayMs: delay, statusCode },
        "Scheduling reconnect."
    );

    const timer = setTimeout(() => {
        reconnectTimers.delete(sessionId);
        void openSocket(sessionId).catch((err: unknown) => {
            logger.error({ err, sessionId }, "Reconnect attempt failed to open a socket.");
            scheduleReconnect(sessionId, undefined);
        });
    }, delay);

    reconnectTimers.set(sessionId, timer);
}

function cancelScheduledReconnect(sessionId: string): void {
    const timer = reconnectTimers.get(sessionId);
    if (timer) {
        clearTimeout(timer);
        reconnectTimers.delete(sessionId);
    }
}

export function createSession(displayName: string): GatewaySession {
    if (sessions.size >= env.MAX_SESSIONS) {
        throw new ApiError(
            409,
            "MAX_SESSIONS_REACHED",
            `Gateway already has the maximum of ${env.MAX_SESSIONS} sessions registered.`
        );
    }

    const id = randomUUID();
    const now = new Date();

    const session: GatewaySession = {
        id,
        displayName,
        status: SessionStatus.CREATED,
        createdAt: now,
        updatedAt: now,
        reconnectAttempts: 0,
        qrCycles: 0
    };

    sessions.set(id, session);
    return session;
}

export function getSession(sessionId: string): GatewaySession {
    const session = sessions.get(sessionId);
    if (!session) {
        throw new ApiError(404, "SESSION_NOT_FOUND", "Gateway session not found.");
    }
    return session;
}

export function getStatus(sessionId: string): SessionStatus {
    return getSession(sessionId).status;
}

export function getSocket(sessionId: string): WASocket {
    const session = getSession(sessionId);
    const socket = sockets.get(sessionId);

    if (!socket || session.status !== SessionStatus.CONNECTED) {
        throw new ApiError(409, "SESSION_NOT_CONNECTED", "Gateway session is not connected.");
    }

    return socket;
}

export function getQr(sessionId: string): string {
    const session = getSession(sessionId);
    if (!session.qr) {
        throw new ApiError(404, "QR_NOT_AVAILABLE", "No QR code available for this session right now.");
    }
    return session.qr;
}

/** Opens the actual Baileys socket. Single-flight per session; never call directly from a route. */
async function openSocket(sessionId: string): Promise<void> {
    const session = getSession(sessionId);

    if (connectingSessions.has(sessionId)) {
        logger.warn({ sessionId }, "Socket open already in progress — skipping duplicate attempt.");
        return;
    }
    connectingSessions.add(sessionId);

    try {
        // Any previous socket must be fully gone before a new one touches the
        // same credentials — two live sockets on one identity is the failure
        // mode this whole service is built to avoid.
        const existing = sockets.get(sessionId);
        if (existing) {
            await withIntentionalClose(sessionId, () => destroySocket(existing));
            sockets.delete(sessionId);
        }

        session.status = SessionStatus.CONNECTING;
        touch(session);

        const socket = await createSocket(sessionId, {
            onQr: (qr) => {
                session.qrCycles += 1;
                if (session.qrCycles > QR_MAX_CYCLES) {
                    // Nobody is scanning. Left alone, each expiring QR closes the
                    // connection and the reconnect produces another one, forever.
                    giveUp(session, `No QR scanned after ${QR_MAX_CYCLES} attempts.`);
                    const active = sockets.get(sessionId);
                    if (active) {
                        // Still not awaited — this is a synchronous Baileys hook,
                        // and blocking it was never the intent.
                        void withIntentionalClose(sessionId, () => destroySocket(active));
                        sockets.delete(sessionId);
                    }
                    return;
                }
                session.qr = qr;
                session.status = SessionStatus.QR_READY;
                touch(session);
            },
            onConnecting: () => {
                session.status = SessionStatus.CONNECTING;
                touch(session);
            },
            onOpen: (phone) => {
                session.status = SessionStatus.CONNECTED;
                if (phone) {
                    session.phone = phone;
                }
                delete session.qr;
                session.connectedAt = new Date();
                // A successful open is the only thing that clears the budget —
                // otherwise a session that flaps every few minutes would reset
                // its own counter and retry forever.
                session.reconnectAttempts = 0;
                session.qrCycles = 0;
                session.needsManualReconnect = false;
                delete session.lastError;
                touch(session);
                logger.info({ sessionId, phone }, "Session connected.");
            },
            onClose: (shouldReconnect, statusCode) => {
                if (consumeIntentionalClose(sessionId)) {
                    return;
                }

                if (!shouldReconnect) {
                    // Terminal reason (or one we don't recognise) — retrying can
                    // only produce more rejections against this number.
                    sockets.delete(sessionId);
                    giveUp(session, `WhatsApp closed the connection permanently (status ${statusCode ?? "unknown"}).`);
                    return;
                }

                sockets.delete(sessionId);
                scheduleReconnect(sessionId, statusCode);
            },
            onLoggedOut: () => {
                if (consumeIntentionalClose(sessionId)) {
                    return;
                }

                cancelScheduledReconnect(sessionId);
                session.status = SessionStatus.UNLINKED;
                session.needsManualReconnect = true;
                session.lastError = "Logged out from the phone — the QR must be scanned again.";
                delete session.qr;
                touch(session);
                sockets.delete(sessionId);
                logger.warn({ sessionId }, "Session logged out from the handset.");

                // Same cleanup unlinkSession() does for an owner-initiated
                // unlink, now also done here for a phone-initiated one (e.g.
                // removed from WhatsApp's own Linked Devices menu). Without
                // this the on-disk creds stay marked "registered" even
                // though WhatsApp has revoked them, so the next connect()
                // resumes with those same dead credentials instead of
                // starting a fresh pairing — Baileys only ever emits a QR
                // when there is no existing registration to try first. That
                // produced exactly the observed loop: repeated "Connection
                // Failure" / "logged out from handset" on every Connect tap,
                // with no QR ever shown. Fire-and-forget: this must not
                // block the event handler, and a failed cleanup here still
                // leaves the session correctly marked UNLINKED for the
                // owner to retry.
                storageProvider.deleteSession(sessionId).catch((err) => {
                    logger.warn({ err, sessionId }, "Error while deleting session files after phone-side logout.");
                });
            }
        });

        sockets.set(sessionId, socket);
    } finally {
        connectingSessions.delete(sessionId);
    }
}

/**
 * Human-triggered connect (POST /sessions/:id/connect). Idempotent: if the
 * session is already connecting, showing a QR, or connected, this is a no-op
 * rather than a second socket. Django calls it on every Connect tap, and the
 * app has a retry button — neither may be able to stack sockets.
 */
export async function connectSession(sessionId: string): Promise<void> {
    const session = getSession(sessionId);

    if (isInCooldown()) {
        throw new ApiError(
            503,
            "GATEWAY_COOLDOWN",
            `Gateway is cooling down after repeated WhatsApp connection failures. Try again in ${Math.ceil(
                cooldownRemainingMs() / 60_000
            )} minute(s).`
        );
    }

    if (connectingSessions.has(sessionId) || ACTIVE_STATUSES.has(session.status)) {
        logger.info({ sessionId, status: session.status }, "Connect requested for an already-active session — ignoring.");
        return;
    }

    // An explicit human action is the one thing that resets the budget: they
    // have seen the failure and asked for another try.
    cancelScheduledReconnect(sessionId);
    session.reconnectAttempts = 0;
    session.qrCycles = 0;
    session.needsManualReconnect = false;
    delete session.lastError;

    await openSocket(sessionId);
}

export async function disconnectSession(sessionId: string): Promise<GatewaySession> {
    const session = getSession(sessionId);

    if (pendingOps.has(sessionId)) {
        throw new ApiError(409, "OPERATION_IN_PROGRESS", "A disconnect or unlink is already in progress for this session.");
    }
    pendingOps.add(sessionId);

    try {
        cancelScheduledReconnect(sessionId);

        const socket = sockets.get(sessionId);
        if (socket) {
            await withIntentionalClose(sessionId, async () => {
                try {
                    await destroySocket(socket);
                } catch (err) {
                    logger.warn({ err, sessionId }, "Error while closing socket during disconnect (continuing).");
                }
            });
        }

        sockets.delete(sessionId);
        session.status = SessionStatus.DISCONNECTED;
        session.reconnectAttempts = 0;
        session.qrCycles = 0;
        delete session.qr;
        touch(session);

        return session;
    } finally {
        pendingOps.delete(sessionId);
    }
}

export async function unlinkSession(sessionId: string): Promise<GatewaySession> {
    const session = getSession(sessionId);

    if (pendingOps.has(sessionId)) {
        throw new ApiError(409, "OPERATION_IN_PROGRESS", "A disconnect or unlink is already in progress for this session.");
    }
    pendingOps.add(sessionId);

    try {
        cancelScheduledReconnect(sessionId);

        const socket = sockets.get(sessionId);
        if (socket) {
            // The flag must span the logout too, not just destroySocket:
            // logout() can emit a close while the listeners are still attached.
            await withIntentionalClose(sessionId, async () => {
                try {
                    await socket.logout();
                } catch (err) {
                    logger.warn({ err, sessionId }, "Error while logging out during unlink (continuing with cleanup).");
                }
                await destroySocket(socket);
            });
        }

        sockets.delete(sessionId);

        try {
            await storageProvider.deleteSession(sessionId);
        } catch (err) {
            logger.warn({ err, sessionId }, "Error while deleting session files during unlink.");
        }

        session.status = SessionStatus.UNLINKED;
        touch(session);
        sessions.delete(sessionId);

        return session;
    } finally {
        pendingOps.delete(sessionId);
    }
}

/**
 * Rebuilds the in-memory session registry from disk on boot.
 *
 * Sessions are registered but NOT auto-connected unless
 * RESTORE_SESSIONS_ON_BOOT=true. Reconnecting on every boot is dangerous in
 * exactly the environment this runs in during development: `tsx watch`
 * restarts the process on every file save, so each save became a fresh round of
 * WhatsApp logins. When enabled for a real deployment, connects are staggered.
 */
export async function restoreSessionsFromDisk(): Promise<void> {
    const entries = await storageProvider.listSessionIds();

    if (entries.length === 0) {
        logger.info("No sessions found in storage, nothing to restore.");
        return;
    }

    for (const sessionId of entries) {
        if (!sessions.has(sessionId)) {
            const now = new Date();
            sessions.set(sessionId, {
                id: sessionId,
                displayName: sessionId,
                status: SessionStatus.DISCONNECTED,
                createdAt: now,
                updatedAt: now,
                reconnectAttempts: 0,
                qrCycles: 0
            });
        }
    }

    if (!env.RESTORE_SESSIONS_ON_BOOT) {
        logger.info(
            { sessionCount: entries.length },
            "Sessions registered from disk but left disconnected (RESTORE_SESSIONS_ON_BOOT is not true)."
        );
        return;
    }

    let index = 0;
    for (const sessionId of entries) {
        const delay = index * RESTORE_STAGGER_MS;
        index += 1;
        setTimeout(() => {
            void connectSession(sessionId).catch((err: unknown) => {
                logger.error({ err, sessionId }, "Failed to restore session from disk.");
                const session = sessions.get(sessionId);
                if (session) {
                    giveUp(session, "Failed to restore this session on gateway startup.");
                }
            });
        }, delay);
    }
    logger.info({ sessionCount: entries.length, staggerMs: RESTORE_STAGGER_MS }, "Staggered session restore scheduled.");
}
