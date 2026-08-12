import { readdir, rm } from "fs/promises";
import path from "path";

import { useMultiFileAuthState } from "@whiskeysockets/baileys";

import { env } from "../config/env.js";
import type { AuthStateHandle, StorageProvider } from "./storage.provider.js";

/**
 * Today's storage: one directory per session under SESSION_PATH, holding the
 * same JSON files useMultiFileAuthState has always written. Behavior is
 * unchanged from before this refactor — this is a thin wrapper, not a new
 * implementation.
 */
export class LocalStorageProvider implements StorageProvider {
    private sessionDir(sessionId: string): string {
        return path.join(env.SESSION_PATH, sessionId);
    }

    async loadAuthState(sessionId: string): Promise<AuthStateHandle> {
        return useMultiFileAuthState(this.sessionDir(sessionId));
    }

    async deleteSession(sessionId: string): Promise<void> {
        await rm(this.sessionDir(sessionId), { recursive: true, force: true });
    }

    async listSessionIds(): Promise<string[]> {
        // Non-directory entries under SESSION_PATH are real (e.g.
        // rate-limit-state.json — see src/config/rateLimit.ts — lives in
        // this same folder) and must never be treated as a session id: a
        // plain `readdir` returned every entry regardless of type, so this
        // file was handed to `useMultiFileAuthState` as if it were a
        // session directory, which throws ("found something that is not a
        // directory") and parks every real session as unrestorable on
        // every boot.
        try {
            const entries = await readdir(env.SESSION_PATH, { withFileTypes: true });
            return entries.filter((entry) => entry.isDirectory()).map((entry) => entry.name);
        } catch {
            return [];
        }
    }
}
