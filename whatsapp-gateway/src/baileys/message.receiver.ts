import type { WAMessage, WASocket } from "@whiskeysockets/baileys";

import logger from "../logger/logger.js";

/**
 * Log-only, per CLAUDE_RULES.md's send-safety rule: this must never call
 * message.service.ts or trigger a send/reply/forward of any kind.
 */
export function registerMessageReceiver(sessionId: string, socket: WASocket): void {
    socket.ev.on("messages.upsert", ({ messages }: { messages: WAMessage[] }) => {
        for (const msg of messages) {
            logger.info(
                {
                    sessionId,
                    from: msg.key.remoteJid,
                    fromMe: msg.key.fromMe,
                    text: msg.message?.conversation ?? msg.message?.extendedTextMessage?.text
                },
                "Message received."
            );
        }
    });
}
