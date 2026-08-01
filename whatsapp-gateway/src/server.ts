import app from "./app.js";
import { env } from "./config/env.js";
import logger from "./logger/logger.js";
import { loadRateLimitState } from "./services/rateLimiter.service.js";
import { restoreSessionsFromDisk } from "./services/session.service.js";

async function start(): Promise<void> {
    try {
        // Send history first: no send may be processed before the limits that
        // survived the last restart are back in memory.
        await loadRateLimitState();
        await restoreSessionsFromDisk();
        app.listen(env.PORT, () => {
            logger.info(`Server started on port ${env.PORT}`);
        });
    } catch (err) {
        logger.error({ err }, "Failed to start server.");
        process.exit(1);
    }
}

void start();
