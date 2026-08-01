import { readFileSync, writeFileSync } from "fs";
import { mkdir } from "fs/promises";
import path from "path";

import { env } from "../config/env.js";
import {
    DAILY_WINDOW_MS,
    MAX_MESSAGES_PER_DAY,
    MAX_MESSAGES_PER_MINUTE,
    MAX_MESSAGES_PER_RECIPIENT_PER_HOUR,
    MIN_SEND_INTERVAL_MS,
    RATE_LIMIT_STATE_FILE,
    RATE_LIMIT_WINDOW_MS,
    RECIPIENT_WINDOW_MS,
    SEND_JITTER_MS
} from "../config/rateLimit.js";
import logger from "../logger/logger.js";
import { ApiError } from "../utils/ApiError.js";

interface SendRecord {
    at: number;
    to: string;
}

/** sessionId -> send history, trimmed to the longest window we care about (24h). */
const history = new Map<string, SendRecord[]>();

function stateFilePath(): string {
    return path.join(env.SESSION_PATH, RATE_LIMIT_STATE_FILE);
}

/**
 * The limits are only meaningful if they survive a restart. Held in memory
 * alone, a crash-loop (or `tsx watch` reloading on every file save) reset the
 * counters to zero each time, so "5 per minute" silently became "5 per
 * restart" — no limit at all in exactly the situation where one matters most.
 */
export async function loadRateLimitState(): Promise<void> {
    try {
        await mkdir(env.SESSION_PATH, { recursive: true });
        const raw = readFileSync(stateFilePath(), "utf8");
        const parsed = JSON.parse(raw) as Record<string, SendRecord[]>;
        const cutoff = Date.now() - DAILY_WINDOW_MS;
        for (const [sessionId, records] of Object.entries(parsed)) {
            const kept = records.filter((r) => typeof r.at === "number" && r.at > cutoff);
            if (kept.length > 0) history.set(sessionId, kept);
        }
        logger.info({ sessions: history.size }, "Restored send-rate history from disk.");
    } catch {
        logger.info("No send-rate history on disk yet — starting fresh.");
    }
}

function persist(): void {
    try {
        writeFileSync(stateFilePath(), JSON.stringify(Object.fromEntries(history)), "utf8");
    } catch (err) {
        // A failed write must not block a legitimate send; the in-memory limits
        // still hold for this process.
        logger.warn({ err }, "Could not persist send-rate history.");
    }
}

function recent(sessionId: string, now: number): SendRecord[] {
    const cutoff = now - DAILY_WINDOW_MS;
    const records = (history.get(sessionId) ?? []).filter((r) => r.at > cutoff);
    history.set(sessionId, records);
    return records;
}

/**
 * Checks every limit and, if the send is allowed, records it and returns how
 * long the caller must wait before actually putting the message on the wire.
 * Check-and-consume is deliberately one step: any gap between them lets two
 * concurrent requests both pass the same check.
 */
export function checkAndConsume(sessionId: string, to: string): number {
    const now = Date.now();
    const records = recent(sessionId, now);

    const inMinute = records.filter((r) => r.at > now - RATE_LIMIT_WINDOW_MS);
    if (inMinute.length >= MAX_MESSAGES_PER_MINUTE) {
        throw new ApiError(
            429,
            "RATE_LIMIT_EXCEEDED",
            `Send limit of ${MAX_MESSAGES_PER_MINUTE} messages per minute exceeded for this session.`
        );
    }

    if (records.length >= MAX_MESSAGES_PER_DAY) {
        throw new ApiError(
            429,
            "DAILY_LIMIT_EXCEEDED",
            `Daily send limit of ${MAX_MESSAGES_PER_DAY} messages reached for this session.`
        );
    }

    const toRecipient = records.filter((r) => r.to === to && r.at > now - RECIPIENT_WINDOW_MS);
    if (toRecipient.length >= MAX_MESSAGES_PER_RECIPIENT_PER_HOUR) {
        throw new ApiError(
            429,
            "RECIPIENT_LIMIT_EXCEEDED",
            `This number has already received ${MAX_MESSAGES_PER_RECIPIENT_PER_HOUR} messages in the last hour.`
        );
    }

    const lastSend = records.length > 0 ? Math.max(...records.map((r) => r.at)) : 0;
    const sinceLast = now - lastSend;
    const jitter = Math.round(Math.random() * SEND_JITTER_MS);
    const waitMs = lastSend === 0 ? jitter : Math.max(0, MIN_SEND_INTERVAL_MS - sinceLast) + jitter;

    // Recorded at its intended send time, so the spacing calculation for the
    // next message accounts for messages still waiting out their delay.
    records.push({ at: now + waitMs, to });
    history.set(sessionId, records);
    persist();

    return waitMs;
}

/** Rolls back a consumed slot when the send itself failed, so a WhatsApp error doesn't burn quota. */
export function release(sessionId: string, to: string): void {
    const records = history.get(sessionId);
    if (!records) return;
    for (let i = records.length - 1; i >= 0; i -= 1) {
        if (records[i]?.to === to) {
            records.splice(i, 1);
            break;
        }
    }
    persist();
}
