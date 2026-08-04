import type { AuthStateHandle, StorageProvider } from "./storage.provider.js";

/**
 * Placeholder only — not implemented yet. Selecting STORAGE_PROVIDER=firebase
 * today will fail loudly at startup rather than silently falling back to
 * local storage, which is the safer failure mode.
 *
 * To implement: store creds.json + the signal key store (app-state-sync-key,
 * pre-key, session, sender-key entries) per session, e.g. as documents under
 * a `sessions/{sessionId}` collection, mirroring the AuthenticationState
 * shape Baileys expects. See docs/whatsapp_gateway_guide.md for the plan.
 */
export class FirebaseStorageProvider implements StorageProvider {
    constructor() {
        throw new Error(
            "FirebaseStorageProvider is not implemented yet. Set STORAGE_PROVIDER=local until Firebase support is built."
        );
    }

    loadAuthState(_sessionId: string): Promise<AuthStateHandle> {
        throw new Error("Not implemented.");
    }

    deleteSession(_sessionId: string): Promise<void> {
        throw new Error("Not implemented.");
    }

    listSessionIds(): Promise<string[]> {
        throw new Error("Not implemented.");
    }
}
