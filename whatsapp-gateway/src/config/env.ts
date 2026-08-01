import "dotenv/config";

import { z } from "zod";

const envSchema = z.object({
    PORT: z.coerce.number().default(3000),
    NODE_ENV: z.enum(["development", "production", "test"]).default("development"),
    LOG_LEVEL: z.enum(["error", "warn", "info", "debug"]).default("info"),
    SESSION_PATH: z.string().default("./sessions"),
    WHATSAPP_GATEWAY_JWT_SECRET: z.string().min(1),
    /**
     * Comma-separated origins allowed to call this gateway from a browser.
     * Empty means "no browser origin at all", which is the correct answer in
     * production: the only legitimate caller is Django, server-to-server, and
     * CORS does not apply to it.
     */
    CORS_ALLOWED_ORIGINS: z.string().default(""),
    /** See config/reconnect.ts: off by default so a crash-loop can't storm WhatsApp. */
    RESTORE_SESSIONS_ON_BOOT: z
        .enum(["true", "false"])
        .default("false")
        .transform((value) => value === "true")
});

const parsed = envSchema.safeParse(process.env);

if (!parsed.success) {
    console.error("Invalid environment configuration:", parsed.error.flatten().fieldErrors);
    process.exit(1);
}

export const env = parsed.data;
