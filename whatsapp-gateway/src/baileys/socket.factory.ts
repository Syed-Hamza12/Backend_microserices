import path from "path";

import makeWASocket, { Browsers, useMultiFileAuthState, type WASocket } from "@whiskeysockets/baileys";

import { env } from "../config/env.js";
import logger from "../logger/logger.js";
import { handleConnectionUpdate, type ConnectionHooks } from "./connection.handler.js";
import { registerMessageReceiver } from "./message.receiver.js";

/**
 * Pinned so every reconnect presents the same client identity. The previous
 * default let the fingerprint vary between connections, which — stacked on top
 * of frequent reconnects — is extra "this isn't a real client" signal.
 */
const BROWSER_FINGERPRINT = Browsers.appropriate("Desktop");

export async function createSocket(sessionId: string, hooks: ConnectionHooks): Promise<WASocket> {
    const sessionDir = path.join(env.SESSION_PATH, sessionId);
    const { state, saveCreds } = await useMultiFileAuthState(sessionDir);

    const socket = makeWASocket({
        auth: state,
        browser: BROWSER_FINGERPRINT,
        logger: logger.child({ module: "baileys", sessionId }),
        // The owner's phone is the real client; this gateway is a background
        // sender. Announcing itself as online would silence notifications on
        // their handset, and syncing full history is a large, pointless
        // transfer on every connect given message.receiver is log-only.
        markOnlineOnConnect: false,
        syncFullHistory: false
    });

    socket.ev.on("creds.update", saveCreds);
    registerMessageReceiver(sessionId, socket);
    socket.ev.on("connection.update", (update) => {
        if (update.connection === "open") {
            hooks.onOpen(socket.user?.phoneNumber);
            return;
        }
        handleConnectionUpdate(update, hooks);
    });

    return socket;
}

/**
 * Fully tears a socket down: listeners first, then the connection. Detaching
 * listeners before ending matters — otherwise the dying socket's own close
 * event fires into the hooks of the session that is already replacing it, and
 * a stale closure schedules a reconnect the caller never asked for.
 */
export async function destroySocket(socket: WASocket): Promise<void> {
    try {
        socket.ev.removeAllListeners("connection.update");
        socket.ev.removeAllListeners("creds.update");
        socket.ev.removeAllListeners("messages.upsert");
    } catch {
        // Listener teardown is best-effort; the end() below is what actually matters.
    }
    try {
        socket.end(undefined);
    } catch {
        // Already dead — nothing to close.
    }
}
