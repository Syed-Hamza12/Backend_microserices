import type { NextFunction, Request, Response } from "express";
import jwt from "jsonwebtoken";

import { env } from "../config/env.js";
import { ApiError } from "../utils/ApiError.js";

const EXPECTED_ISSUER = "django-backend";
const EXPECTED_AUDIENCE = "whatsapp-gateway";

/** Scope a token carries when it is allowed to create a brand-new session (no session id exists yet). */
export const SESSION_CREATE_SCOPE = "session:create";

export interface GatewayTokenPayload {
    /** The one gateway session this token may act on, or undefined for a create-scoped token. */
    sub: string | undefined;
    scope: string | undefined;
}

declare global {
    // eslint-disable-next-line @typescript-eslint/no-namespace
    namespace Express {
        interface Request {
            gatewayToken?: GatewayTokenPayload;
        }
    }
}

export function requireDjangoJwt(req: Request, _res: Response, next: NextFunction): void {
    const header = req.header("authorization");
    const token = header?.startsWith("Bearer ") ? header.slice("Bearer ".length) : undefined;

    if (!token) {
        next(new ApiError(401, "UNAUTHORIZED", "Missing or invalid Authorization bearer token."));
        return;
    }

    try {
        const payload = jwt.verify(token, env.WHATSAPP_GATEWAY_JWT_SECRET, {
            algorithms: ["HS256"],
            issuer: EXPECTED_ISSUER,
            audience: EXPECTED_AUDIENCE,
            // Django mints these with a 60s lifetime; refuse anything longer even
            // if it verifies, so a leaked token has a hard, short blast radius.
            maxAge: "5m",
            clockTolerance: 10
        });
        if (typeof payload === "string") {
            throw new Error("unexpected token payload");
        }
        req.gatewayToken = { sub: payload.sub as string | undefined, scope: payload.scope as string | undefined };
    } catch {
        next(new ApiError(401, "UNAUTHORIZED", "Missing or invalid Authorization bearer token."));
        return;
    }

    next();
}

/**
 * Enforces that the caller's token was minted for THIS session.
 *
 * Without this the gateway trusted any valid service token to act on any
 * `gatewaySessionId` it named — tenant isolation lived only in Django, so a
 * single leaked token (or any bug that let one business's request reach the
 * gateway carrying another's session id) could send WhatsApp messages from
 * every connected business's number.
 */
export function assertSessionScope(req: Request, sessionId: string): void {
    const token = req.gatewayToken;
    if (!token || token.sub !== sessionId) {
        throw new ApiError(403, "FORBIDDEN", "This token is not authorised for that gateway session.");
    }
}

export function assertCreateScope(req: Request): void {
    const token = req.gatewayToken;
    if (!token || token.scope !== SESSION_CREATE_SCOPE) {
        throw new ApiError(403, "FORBIDDEN", "This token is not authorised to create gateway sessions.");
    }
}
