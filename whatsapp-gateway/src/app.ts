import cors from "cors";
import express, { type Application } from "express";

import { env } from "./config/env.js";
import { errorHandler, notFoundHandler } from "./middleware/errorHandler.middleware.js";
import { requireDjangoJwt } from "./middleware/jwt.middleware.js";
import healthRoutes from "./routes/health.routes.js";
import messageRoutes from "./routes/message.routes.js";
import sessionRoutes from "./routes/session.routes.js";

const app: Application = express();

app.disable("x-powered-by");

/**
 * The only legitimate caller is Django, server-to-server, where CORS does not
 * apply. `cors()` with no options previously reflected every origin, which on a
 * service that sends WhatsApp messages meant any web page a user visited could
 * script requests at it. Empty allowlist = no browser origin permitted.
 */
const corsOrigins = env.CORS_ALLOWED_ORIGINS.split(",")
    .map((o) => o.trim())
    .filter(Boolean);
app.use(cors({ origin: corsOrigins.length > 0 ? corsOrigins : false }));

app.use((_req, res, next) => {
    res.setHeader("X-Content-Type-Options", "nosniff");
    res.setHeader("X-Frame-Options", "DENY");
    res.setHeader("Referrer-Policy", "no-referrer");
    next();
});

// Every request body here is a handful of small fields; the default 100kb is
// already generous, and an explicit cap keeps it that way.
app.use(express.json({ limit: "64kb" }));

app.use("/api/v1", healthRoutes);
app.use("/api/v1", requireDjangoJwt);
app.use("/api/v1", sessionRoutes);
app.use("/api/v1", messageRoutes);

app.use(notFoundHandler);
app.use(errorHandler);

export default app;
