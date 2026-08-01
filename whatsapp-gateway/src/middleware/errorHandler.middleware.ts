import type { NextFunction, Request, Response } from "express";
import { ZodError } from "zod";

import logger from "../logger/logger.js";
import { ApiError } from "../utils/ApiError.js";
import { sendError } from "../utils/response.util.js";

export function notFoundHandler(req: Request, res: Response): void {
    sendError(res, "NOT_FOUND", `Route ${req.originalUrl} not found.`, 404);
}

export function errorHandler(
    err: unknown,
    _req: Request,
    res: Response,
    _next: NextFunction
): void {
    if (err instanceof ApiError) {
        logger.warn(`${err.code}: ${err.message}`);
        sendError(res, err.code, err.message, err.statusCode);
        return;
    }

    // body-parser rejects an oversized/malformed body with its own typed error.
    // Left unclassified it fell through to a bare 500, which tells the caller
    // nothing and looks like a server fault rather than a rejected request.
    if (err instanceof Error && "type" in err && typeof (err as { type?: unknown }).type === "string") {
        const bodyParserType = (err as unknown as { type: string; status?: number }).type;
        if (bodyParserType === "entity.too.large") {
            sendError(res, "PAYLOAD_TOO_LARGE", "Request body is too large.", 413);
            return;
        }
        if (bodyParserType === "entity.parse.failed") {
            sendError(res, "INVALID_JSON", "Request body is not valid JSON.", 400);
            return;
        }
    }

    if (err instanceof ZodError) {
        const message = err.issues.map((issue) => `${issue.path.join(".")}: ${issue.message}`).join("; ");
        logger.warn(`VALIDATION_ERROR: ${message}`);
        sendError(res, "VALIDATION_ERROR", message, 400);
        return;
    }

    const message = err instanceof Error ? err.message : "Unknown error";
    logger.error({ stack: err instanceof Error ? err.stack : undefined }, message);
    sendError(res, "INTERNAL_SERVER_ERROR", "Something went wrong.", 500);
}
