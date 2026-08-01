import type { Response } from "express";

export function sendSuccess(
    res: Response,
    data: unknown = {},
    message = "Success",
    statusCode = 200
): void {
    res.status(statusCode).json({
        success: true,
        message,
        data
    });
}

export function sendError(
    res: Response,
    code: string,
    message: string,
    statusCode = 500
): void {
    res.status(statusCode).json({
        success: false,
        error: {
            code,
            message
        }
    });
}
