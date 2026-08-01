import { Router } from "express";

import {
    connectSession,
    createSession,
    deleteSession,
    disconnectSession,
    getQr,
    getStatus
} from "../controllers/session.controller.js";

const router = Router();

router.post("/sessions", createSession);
router.post("/sessions/:id/connect", connectSession);
router.get("/sessions/:id/qr", getQr);
router.get("/sessions/:id/status", getStatus);
router.post("/sessions/:id/disconnect", disconnectSession);
router.delete("/sessions/:id", deleteSession);

export default router;
