import type { AuthenticationCreds, AuthenticationState } from "@whiskeysockets/baileys";

/**
 * Abstraction over where Baileys auth state (creds + signal keys) lives.
 * Everything above this interface — socket.factory, session.service — talks
 * only to this shape, never to the filesystem directly, so a future provider
 * (Firebase, etc.) is a drop-in behind STORAGE_PROVIDER without touching
 * connection logic.
 */
export interface AuthStateHandle {
    state: AuthenticationState;
    saveCreds: () => Promise<void>;
}

export interface StorageProvider {
    /** Loads (or initializes) the auth state for a session. Mirrors Baileys' useMultiFileAuthState contract. */
    loadAuthState(sessionId: string): Promise<AuthStateHandle>;

    /** Permanently removes a session's stored credentials. */
    deleteSession(sessionId: string): Promise<void>;

    /** Lists session ids currently persisted, for boot-time restore. */
    listSessionIds(): Promise<string[]>;
}

/** Re-exported for providers that need to type stored creds directly. */
export type { AuthenticationCreds };
