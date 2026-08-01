import type { NextFunction, Request, Response } from "express";
import { z } from "zod";

import { assertSessionScope } from "../middleware/jwt.middleware.js";
import * as messageService from "../services/message.service.js";
import { sendSuccess } from "../utils/response.util.js";

/** WhatsApp's own text limit is ~65k; cap well under it so one call can't build a giant payload. */
const MAX_MESSAGE_LENGTH = 4096;

const phoneNumber = z
    .string()
    .regex(/^\d{7,15}$/, "to must be digits only, in international format (e.g. 923001234567).");

const sendTextSchema = z.object({
    gatewaySessionId: z.string().min(1).max(128),
    to: phoneNumber,
    message: z.string().min(1).max(MAX_MESSAGE_LENGTH)
});

const sendMediaSchema = z.object({
    gatewaySessionId: z.string().min(1).max(128),
    to: phoneNumber,
    /**
     * `image` is sent inline so the customer reads the bill in the chat without
     * downloading anything; `document` arrives as a tappable PDF attachment.
     */
    kind: z.enum(["image", "document"]),
    fileName: z
        .string()
        .max(255)
        // No path separators: this becomes the attachment's display name and
        // must never look like a path.
        .regex(/^[^/\\]+\.(pdf|png)$/i, "fileName must be a plain filename ending in .pdf or .png."),
    caption: z.string().max(1024).optional()
});

export async function sendText(req: Request, res: Response, next: NextFunction): Promise<void> {
    try {
        const { gatewaySessionId, to, message } = sendTextSchema.parse(req.body);
        assertSessionScope(req, gatewaySessionId);
        await messageService.sendText(gatewaySessionId, to, message);
        sendSuccess(res, {}, "Message sent.");
    } catch (err) {
        next(err);
    }
}

export async function sendMedia(req: Request, res: Response, next: NextFunction): Promise<void> {
    try {
        const { gatewaySessionId, to, kind, fileName, caption } = sendMediaSchema.parse(req.query);
        assertSessionScope(req, gatewaySessionId);

        const body = req.body as Buffer | undefined;
        if (!Buffer.isBuffer(body) || body.length === 0) {
            throw new Error("Request body must be the raw file bytes (Content-Type: application/octet-stream).");
        }

        await messageService.sendMedia(gatewaySessionId, to, {
            kind,
            fileName,
            ...(caption !== undefined ? { caption } : {}),
            content: body
        });
        sendSuccess(res, {}, "Media sent.");
    } catch (err) {
        next(err);
    }
}
