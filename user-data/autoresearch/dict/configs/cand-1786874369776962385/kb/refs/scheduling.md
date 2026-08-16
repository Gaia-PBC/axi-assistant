# Scheduling Reference

Use the schedule MCP tools to manage scheduled events. Do NOT edit schedules.json directly.

Available tools:
- **schedule_list** — List all scheduled tasks (you see all schedules as the master agent).
- **schedule_create** — Create a new scheduled task.
- **schedule_modify** — Modify fields of an existing schedule (prompt, cron, cwd, session, reset_context).
- **schedule_delete** — Delete a scheduled task by name.
- **schedule_disable** — Disable a schedule without deleting it.
- **schedule_enable** — Re-enable a disabled schedule.

## Creating schedules

Required fields:
- **name** (string): short identifier, lowercase alphanumeric and hyphens (e.g. "daily-standup", "reminder-123")
- **prompt** (string): the message/instructions sent when the schedule fires
- **schedule_type**: "recurring" (with cron) or "one_off" (with at)
- **cwd** (string): absolute path to the working directory for the agent session. REQUIRED — every schedule must have a cwd.

Type-specific fields:
- **cron** (string): cron expression for recurring schedules (e.g. "0 9 * * *")
- **at** (string): ISO 8601 datetime with timezone for one-off events (e.g. "2026-03-01T14:00:00-08:00")

Optional fields:
- **session** (string): agent session name to route to. If set, the schedule fires in a dedicated session instead of routing to the owner. Prevents schedule events from clobbering the owner's session.
- **reset_context** (boolean): resets conversation before firing

IMPORTANT: Cron times are evaluated in the SCHEDULE_TIMEZONE configured in .env (US/Pacific), NOT in UTC.
For example, "0 10 * * *" means 10:00 AM Pacific. Do NOT write cron times in UTC. DST is handled automatically.

Every scheduled event spawns its own agent session. Events with the same session name share one persistent agent —
if the session already exists when an event fires, the prompt is sent to the existing agent instead of spawning a new one.

## Schedule Skips (One-Off Cancellations)

You can skip a single occurrence of a recurring event by editing schedule_skips.json in your working directory.
Each entry has a "name" (matching the recurring event name) and a "skip_date" (YYYY-MM-DD in the SCHEDULE_TIMEZONE).
Example: {"name": "morning-checkin", "skip_date": "2026-02-22"} skips the morning-checkin on Feb 22 only — it fires normally every other day.
Expired skips (past dates) are auto-pruned by the scheduler.
To **move** a recurring event to a different time on a specific day, compose two actions:
1) Add a skip entry for that day in schedule_skips.json, and
2) Create a one-off schedule with the same prompt but at the desired time.
