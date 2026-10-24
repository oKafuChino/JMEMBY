-- Historical empty schema for removal migration regression tests.
BEGIN TRANSACTION;
CREATE TABLE auto_download_claims (
 source TEXT NOT NULL, source_id TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES schedule_runs(id),
 task_id TEXT NOT NULL REFERENCES tasks(id), PRIMARY KEY(source,source_id));
CREATE TABLE chapters (
 id INTEGER PRIMARY KEY, manga_id INTEGER NOT NULL REFERENCES manga(id),
 source_id TEXT NOT NULL, ordinal INTEGER NOT NULL, title TEXT NOT NULL,
 path TEXT NOT NULL UNIQUE, pages TEXT, size INTEGER, checksum TEXT,
 status TEXT NOT NULL DEFAULT 'MISSING', komga_id TEXT,
 UNIQUE(manga_id, source_id));
CREATE TABLE confirmations (
 token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
 snapshot TEXT NOT NULL, expires REAL NOT NULL, task_id TEXT REFERENCES tasks(id),
 dismissed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE invites (
                    id INTEGER PRIMARY KEY, code_hash TEXT NOT NULL UNIQUE,
                    max_uses INTEGER NOT NULL CHECK(max_uses>0),
                    used INTEGER NOT NULL DEFAULT 0,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    created_by INTEGER NOT NULL, created_at REAL NOT NULL);
CREATE TABLE komga_accounts (
                    user_id INTEGER PRIMARY KEY REFERENCES users(user_id),
                    email TEXT NOT NULL UNIQUE, password_encrypted TEXT,
                    remote_id TEXT, status TEXT NOT NULL DEFAULT 'PENDING',
                    error_code TEXT, created_at REAL NOT NULL);
CREATE TABLE manga (
 id INTEGER PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL,
 title TEXT NOT NULL, directory TEXT NOT NULL, metadata TEXT NOT NULL,
 UNIQUE(source, source_id));
CREATE TABLE notifications (
 id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), event TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0,
 next_at REAL NOT NULL DEFAULT 0, UNIQUE(task_id,event));
CREATE TABLE schedule_actions (
 token TEXT PRIMARY KEY, owner_user_id INTEGER NOT NULL, schedule_id INTEGER NOT NULL REFERENCES schedules(id),
 action TEXT NOT NULL, expires REAL NOT NULL, revision INTEGER NOT NULL, result TEXT);
CREATE TABLE schedule_drafts (
 owner_user_id INTEGER PRIMARY KEY, step TEXT NOT NULL, data TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE schedule_notifications (
 id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES schedule_runs(id), event TEXT NOT NULL,
 text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0,
 next_at REAL NOT NULL DEFAULT 0, UNIQUE(run_id,event));
CREATE TABLE schedule_run_items (
 run_id TEXT NOT NULL REFERENCES schedule_runs(id), source TEXT NOT NULL, source_id TEXT NOT NULL,
 rank INTEGER NOT NULL, likes_raw TEXT, likes_exact INTEGER, checked_at REAL,
 policy_version INTEGER NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL,
 snapshot TEXT, task_id TEXT REFERENCES tasks(id), PRIMARY KEY(run_id,source,source_id));
CREATE TABLE schedule_runs (
 id TEXT PRIMARY KEY, schedule_id INTEGER NOT NULL REFERENCES schedules(id),
 scheduled_for REAL NOT NULL, trigger_type TEXT NOT NULL, status TEXT NOT NULL,
 snapshot TEXT NOT NULL, created_at REAL NOT NULL, next_attempt_at REAL NOT NULL DEFAULT 0,
 attempts INTEGER NOT NULL DEFAULT 0, error TEXT, stats TEXT NOT NULL DEFAULT '{}');
CREATE TABLE schedules (
 id INTEGER PRIMARY KEY, owner_user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
 tag TEXT NOT NULL, spec TEXT NOT NULL, item_limit INTEGER NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1,
 next_run_at REAL, created_at REAL NOT NULL);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO "settings" VALUES('registration_open','1');
INSERT INTO "settings" VALUES('invite_required','0');
CREATE TABLE task_chapters (
 task_id TEXT NOT NULL REFERENCES tasks(id), chapter_id INTEGER NOT NULL REFERENCES chapters(id),
 status TEXT NOT NULL DEFAULT 'PENDING', PRIMARY KEY(task_id,chapter_id));
CREATE TABLE tasks (
 id TEXT PRIMARY KEY, manga_id INTEGER NOT NULL REFERENCES manga(id),
 snapshot TEXT NOT NULL, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'QUEUED', phase TEXT NOT NULL DEFAULT 'download',
 attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
 cancel_requested INTEGER NOT NULL DEFAULT 0, error_code TEXT, error TEXT,
 progress TEXT NOT NULL DEFAULT '', message_id INTEGER, retry_of TEXT REFERENCES tasks(id),
 created_at REAL NOT NULL, updated_at REAL NOT NULL, origin TEXT NOT NULL DEFAULT 'manual', schedule_run_id TEXT REFERENCES schedule_runs(id), policy_version INTEGER, queue_sequence INTEGER);
CREATE TABLE users (
                    user_id INTEGER PRIMARY KEY, username TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    blocked INTEGER NOT NULL DEFAULT 0 CHECK(blocked IN (0,1)),
                    registered_at REAL NOT NULL);
CREATE UNIQUE INDEX one_active_manga ON tasks(manga_id)
 WHERE status NOT IN ('COMPLETED','FAILED','CANCELLED');
CREATE INDEX ready_tasks ON tasks(status,next_at);
CREATE UNIQUE INDEX schedule_slot ON schedule_runs(schedule_id,scheduled_for)
 WHERE trigger_type='timer';
CREATE UNIQUE INDEX task_sequence ON tasks(queue_sequence);
COMMIT;
PRAGMA user_version=5;
