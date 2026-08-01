import type { Request, Response } from "express";

import { sendSuccess } from "../utils/response.util.js";

export function getHealth(_req: Request, res: Response): void {
    sendSuccess(res, {}, "Gateway is healthy.");
}
