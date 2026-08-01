import * as rateLimiter from "./rateLimiter.service.js";
import * as sessionService from "./session.service.js";
import logger from "../logger/logger.js";
import { ApiError } from "../utils/ApiError.js";

const MAX_MEDIA_BYTES = 15 * 1024 * 1024;

/** Leading bytes each accepted format must actually start with. */
const MAGIC_BYTES: Record<string, { prefix: Buffer; label: string }> = {
    pdf: { prefix: Buffer.from("%PDF-", "ascii"), label: "PDF" },
    png: { prefix: Buffer.from([0x89, 0x50, 0x4e, 0x47]), label: "PNG" }
};

export interface MediaPayload {
    kind: "image" | "document";
    fileName: string;
    caption?: string;
    content: Buffer;
}

function toJid(phone: string): string {
    return `${phone}@s.whatsapp.net`;
}

function sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Verifies the recipient actually exists on WhatsApp before sending.
 *
 * Repeatedly messaging numbers that aren't registered is a strong spam signal —
 * it's what bulk senders working from a scraped list look like. A typo in a
 * customer's phone number is enough to produce that pattern here.
 */
async function assertRecipientOnWhatsApp(sessionId: string, to: string): Promise<void> {
    const socket = sessionService.getSocket(sessionId);
    try {
        const results = await socket.onWhatsApp(to);
        if (!results || results.length === 0 || !results[0]?.exists) {
            throw new ApiError(422, "RECIPIENT_NOT_ON_WHATSAPP", "That number is not registered on WhatsApp.");
        }
    } catch (err) {
        if (err instanceof ApiError) throw err;
        // A lookup failure is not proof of anything — log it and let the send
        // proceed rather than blocking a legitimate message on a flaky check.
        logger.warn({ err, sessionId }, "Could not verify recipient on WhatsApp; continuing.");
    }
}

export async function sendText(sessionId: string, to: string, message: string): Promise<void> {
    sessionService.getSocket(sessionId);
    await assertRecipientOnWhatsApp(sessionId, to);

    const waitMs = rateLimiter.checkAndConsume(sessionId, to);
    try {
        if (waitMs > 0) await sleep(waitMs);
        // Re-fetched after the wait: the session may have dropped while pacing.
        const socket = sessionService.getSocket(sessionId);
        await socket.sendMessage(toJid(to), { text: message });
    } catch (err) {
        rateLimiter.release(sessionId, to);
        throw err;
    }
}

/**
 * Sends an already-rendered file. The bytes arrive in the request rather than
 * being fetched from a URL, so this service never reaches out to the network to
 * collect a document and there is no temporary file anywhere in the flow.
 */
/**
 * Checks the bytes actually are what the filename claims.
 *
 * Exported so it can be exercised on its own: inside sendMedia it sits behind a
 * session lookup, which makes it awkward to test through the HTTP layer without
 * a live WhatsApp connection.
 */
export function assertMediaMatchesType(media: MediaPayload): void {
    if (media.content.length > MAX_MEDIA_BYTES) {
        throw new ApiError(422, "MEDIA_TOO_LARGE", "File exceeds the 15MB size limit.");
    }

    const extension = media.fileName.toLowerCase().endsWith(".pdf") ? "pdf" : "png";
    const expected = MAGIC_BYTES[extension]!;
    if (!media.content.subarray(0, expected.prefix.length).equals(expected.prefix)) {
        // Content is checked against the claimed type, never trusted from it —
        // the same reason the old fetch path verified PDF magic bytes.
        throw new ApiError(
            422,
            "MEDIA_TYPE_MISMATCH",
            `File does not look like a valid ${expected.label} (magic bytes mismatch).`
        );
    }
    if (media.kind === "image" && extension !== "png") {
        throw new ApiError(422, "MEDIA_TYPE_MISMATCH", "Image sends must be PNG.");
    }
}

export async function sendMedia(sessionId: string, to: string, media: MediaPayload): Promise<void> {
    // Confirm the session is connected before doing any other work.
    sessionService.getSocket(sessionId);

    assertMediaMatchesType(media);

    await assertRecipientOnWhatsApp(sessionId, to);

    const waitMs = rateLimiter.checkAndConsume(sessionId, to);
    try {
        if (waitMs > 0) await sleep(waitMs);
        const socket = sessionService.getSocket(sessionId);

        if (media.kind === "image") {
            // Sent as an image, not an attachment, so a bill renders inline in
            // the chat — which is the whole reason the image format exists.
            await socket.sendMessage(toJid(to), {
                image: media.content,
                ...(media.caption ? { caption: media.caption } : {})
            });
        } else {
            await socket.sendMessage(toJid(to), {
                document: media.content,
                fileName: media.fileName,
                mimetype: "application/pdf",
                ...(media.caption ? { caption: media.caption } : {})
            });
        }
    } catch (err) {
        rateLimiter.release(sessionId, to);
        throw err;
    }
}
