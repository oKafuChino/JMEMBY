"""One-way upgrades that retain download history and published file references."""

FIFO_FROM_V4 = """
BEGIN IMMEDIATE;
ALTER TABLE tasks ADD COLUMN queue_sequence INTEGER;
WITH ordered AS (SELECT id,ROW_NUMBER() OVER (ORDER BY created_at,id) AS position FROM tasks)
UPDATE tasks SET queue_sequence=(SELECT position FROM ordered WHERE ordered.id=tasks.id);
CREATE UNIQUE INDEX task_sequence ON tasks(queue_sequence);
PRAGMA user_version=6;
COMMIT;
"""

REMOVE_AUTOMATION_FROM_V5 = """
BEGIN IMMEDIATE;
UPDATE notifications SET status='SKIPPED'
 WHERE status='PENDING' AND task_id IN (SELECT id FROM tasks WHERE origin='schedule');
UPDATE tasks SET status='CANCELLED',cancel_requested=1,error_code='AUTO_DOWNLOAD_REMOVED',
 error='自动下载功能已移除；任务已取消，已保存文件保留。',updated_at=CAST(strftime('%s','now') AS REAL)
 WHERE origin='schedule' AND status NOT IN ('COMPLETED','FAILED','CANCELLED');
DELETE FROM settings WHERE key IN ('schedule_active_download','schedule_download_after');
DROP TABLE auto_download_claims;
DROP TABLE schedule_notifications;
DROP TABLE schedule_actions;
DROP TABLE schedule_drafts;
DROP TABLE schedule_run_items;
ALTER TABLE tasks DROP COLUMN schedule_run_id;
ALTER TABLE tasks DROP COLUMN policy_version;
ALTER TABLE tasks DROP COLUMN origin;
DROP TABLE schedule_runs;
DROP TABLE schedules;
PRAGMA user_version=6;
COMMIT;
"""
