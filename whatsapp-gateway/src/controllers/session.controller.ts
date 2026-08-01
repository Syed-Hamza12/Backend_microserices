import type { NextFunction, Request, Response } from "express";
import QRCode from "qrcode";
import { z } from "zod";

import { assertCreateScope, assertSessionScope } from "../middleware/jwt.middleware.js";
import * as sessionService from "../services/session.service.js";
import { sendSuccess } from "../utils/response.util.js";

const createSessionSchema = z.object({
    displayName: z.string().min(1).max(255)
});

const sessionIdSchema = z.object({ id: z.string().min(1).max(128) });

export function createSession(req: Request, res: Response, next: NextFunction): void {
    try {
        assertCreateScope(req);
        const { displayName } = createSessionSchema.parse(req.body);
        const session = sessionService.createSession(displayName);
        sendSuccess(res, session, "Session created.", 201);
    } catch (err) {
        next(err);
    }
}

export async function connectSession(req: Request, res: Response, next: NextFunction): Promise<void> {
    try {
        const { id } = sessionIdSchema.parse(req.params);
        assertSessionScope(req, id);
        await sessionService.connectSession(id);
        sendSuccess(res, { id }, "Connection started. Poll /qr or /status for progress.");
    } catch (err) {
        next(err);
    }
}

export async function getQr(req: Request, res: Response, next: NextFunction): Promise<void> {
    try {
        const { id } = sessionIdSchema.parse(req.params);
        assertSessionScope(req, id);
        const qr = sessionService.getQr(id);
        const png = await QRCode.toBuffer(qr, { type: "png" });
        res.type("image/png").send(png);
    } catch (err) {
        next(err);
    }
}

export function getStatus(req: Request, res: Response, next: NextFunction): void {
    try {
        const { id } = sessionIdSchema.parse(req.params);
        assertSessionScope(req, id);
        const session = sessionService.getSession(id);
        sendSuccess(res, session);
    } catch (err) {
        next(err);
    }
}

export async function disconnectSession(req: Request, res: Response, next: NextFunction): Promise<void> {
    try {
        const { id } = sessionIdSchema.parse(req.params);
        assertSessionScope(req, id);
        const session = await sessionService.disconnectSession(id);
        sendSuccess(res, session, "Session disconnected. Session files kept on disk.");
    } catch (err) {
        next(err);
    }
}

export async function deleteSession(req: Request, res: Response, next: NextFunction): Promise<void> {
    try {
        const { id } = sessionIdSchema.parse(req.params);
        assertSessionScope(req, id);
        const session = await sessionService.unlinkSession(id);
        sendSuccess(res, session, "Session unlinked and deleted.");
    } catch (err) {
        next(err);
    }
}
