MIGRATION = """
BEGIN IMMEDIATE;
CREATE TABLE schedules (
 id INTEGER PRIMARY KEY, owner_user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
 tag TEXT NOT NULL, spec TEXT NOT NULL, item_limit INTEGER NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1,
 next_run_at REAL, created_at REAL NOT NULL);
CREATE TABLE schedule_runs (
 id TEXT PRIMARY KEY, schedule_id INTEGER NOT NULL REFERENCES schedules(id),
 scheduled_for REAL NOT NULL, trigger_type TEXT NOT NULL, status TEXT NOT NULL,
 snapshot TEXT NOT NULL, created_at REAL NOT NULL, next_attempt_at REAL NOT NULL DEFAULT 0,
 attempts INTEGER NOT NULL DEFAULT 0, error TEXT, stats TEXT NOT NULL DEFAULT '{}');
CREATE UNIQUE INDEX schedule_slot ON schedule_runs(schedule_id,scheduled_for)
 WHERE trigger_type='timer';
CREATE TABLE schedule_run_items (
 run_id TEXT NOT NULL REFERENCES schedule_runs(id), source TEXT NOT NULL, source_id TEXT NOT NULL,
 rank INTEGER NOT NULL, likes_raw TEXT, likes_exact INTEGER, checked_at REAL,
 policy_version INTEGER NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL,
 snapshot TEXT, task_id TEXT REFERENCES tasks(id), PRIMARY KEY(run_id,source,source_id));
CREATE TABLE auto_download_claims (
 source TEXT NOT NULL, source_id TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES schedule_runs(id),
 task_id TEXT NOT NULL REFERENCES tasks(id), PRIMARY KEY(source,source_id));
CREATE TABLE schedule_notifications (
 id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES schedule_runs(id), event TEXT NOT NULL,
 text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0,
 next_at REAL NOT NULL DEFAULT 0, UNIQUE(run_id,event));
CREATE TABLE schedule_actions (
 token TEXT PRIMARY KEY, owner_user_id INTEGER NOT NULL, schedule_id INTEGER NOT NULL REFERENCES schedules(id),
 action TEXT NOT NULL, expires REAL NOT NULL, revision INTEGER NOT NULL, result TEXT);
CREATE TABLE schedule_drafts (
 owner_user_id INTEGER PRIMARY KEY, step TEXT NOT NULL, data TEXT NOT NULL, expires REAL NOT NULL);
ALTER TABLE tasks ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE tasks ADD COLUMN schedule_run_id TEXT REFERENCES schedule_runs(id);
ALTER TABLE tasks ADD COLUMN policy_version INTEGER;
ALTER TABLE tasks ADD COLUMN queue_sequence INTEGER;
WITH ordered AS (SELECT id,ROW_NUMBER() OVER (ORDER BY created_at,id) AS position FROM tasks)
UPDATE tasks SET queue_sequence=(SELECT position FROM ordered WHERE ordered.id=tasks.id);
CREATE UNIQUE INDEX task_sequence ON tasks(queue_sequence);
PRAGMA user_version=5;
COMMIT;
"""
