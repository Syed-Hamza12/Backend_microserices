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
        try {
            return await readdir(env.SESSION_PATH);
        } catch {
            return [];
        }
    }
}
