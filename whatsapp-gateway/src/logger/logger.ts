import pino from "pino";

import { env } from "../config/env.js";

const logger = pino({
    level: env.LOG_LEVEL
});

export default logger;
