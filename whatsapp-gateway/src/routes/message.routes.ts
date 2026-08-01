import { Router, raw } from "express";

import { sendMedia, sendText } from "../controllers/message.controller.js";

const router = Router();

router.post("/messages", sendText);

/**
 * Media is uploaded as a raw body rather than fetched from a URL.
 *
 * Django renders the document and streams the bytes straight here, so the file
 * never exists on disk and never needs a public URL. That removes the whole
 * fetch path — no SSRF surface, no HTTPS requirement in development, and no
 * delete-after-send step that could race the send.
 *
 * Metadata travels as query parameters because the body is the file itself.
 */
router.post(
    "/messages/media",
    raw({ type: "application/octet-stream", limit: "16mb" }),
    sendMedia
);

export default router;
