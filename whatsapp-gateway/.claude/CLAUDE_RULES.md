# Claude Code Rules

## Before Writing Any Code

1. Read `README.md`
2. Read `SPRINT.md` — work only on what's listed there
3. Scan the existing `src/` folder before creating new files — reuse before rewriting

## Always

- TypeScript, explicit types (no `any`)
- `async/await`, not raw promises
- Small functions (aim 10–30 lines, 50 max)
- Every async function wrapped in try/catch
- Validate every request body (zod)
- Follow the flow: Routes → Controllers → Services → Baileys. Never skip a layer.

## Never

- Never implement anything not listed in the current sprint in `SPRINT.md`
- Never rewrite working code without saying why first
- Never add a new folder/dependency/architecture layer without flagging it to me first
- Never log API keys, credentials, or `sessions/` contents
- Never touch `.env` or commit `sessions/`
- Never write code that sends a WhatsApp message automatically, on a timer, on a loop, on reconnect, or in response to an incoming message, unless explicitly asked for that specific behavior. Every send must be triggered only by a direct API call from a human or an explicit test run. No auto-replies, no retry-loops that resend on failure without a cap, no test scripts that send in a loop without a hard, low iteration limit (e.g. max 1).

## After Finishing a Task

1. Tell me what you changed and why, in plain language
2. Update `SPRINT.md` (mark task done, note what's next)
3. Stop and wait for me before starting the next sprint item — don't chain features on your own