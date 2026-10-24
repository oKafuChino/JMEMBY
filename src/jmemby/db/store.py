from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import secrets
import sqlite3
import time
import uuid

from jmemby.domain.models import Chapter, Manga, Page, normalize_tags
from jmemby.storage.cbz import chapter_filename, library_directory

TERMINAL = ("COMPLETED", "FAILED", "CANCELLED")


def decode_manga(raw):
    data = json.loads(raw) if isinstance(raw, str) else raw
    return Manga(data["source"], data["source_id"], data["title"], tuple(data["authors"]),
                 tuple(Chapter(c["source_id"], c["ordinal"], c["title"],
                               tuple(Page(**p) for p in c["pages"])) for c in data["chapters"]),
                 data.get("cover_url"), normalize_tags(data.get("tags")))


SCHEMA = """
CREATE TABLE manga (
 id INTEGER PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL,
 title TEXT NOT NULL, directory TEXT NOT NULL, metadata TEXT NOT NULL,
 UNIQUE(source, source_id));
CREATE TABLE chapters (
 id INTEGER PRIMARY KEY, manga_id INTEGER NOT NULL REFERENCES manga(id),
 source_id TEXT NOT NULL, ordinal INTEGER NOT NULL, title TEXT NOT NULL,
 path TEXT NOT NULL UNIQUE, pages TEXT, size INTEGER, checksum TEXT,
 status TEXT NOT NULL DEFAULT 'MISSING', komga_id TEXT,
 UNIQUE(manga_id, source_id));
CREATE TABLE tasks (
 id TEXT PRIMARY KEY, manga_id INTEGER NOT NULL REFERENCES manga(id),
 snapshot TEXT NOT NULL, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'QUEUED', phase TEXT NOT NULL DEFAULT 'download',
 attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
 cancel_requested INTEGER NOT NULL DEFAULT 0, error_code TEXT, error TEXT,
 progress TEXT NOT NULL DEFAULT '', message_id INTEGER, retry_of TEXT REFERENCES tasks(id),
 created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE UNIQUE INDEX one_active_manga ON tasks(manga_id)
 WHERE status NOT IN ('COMPLETED','FAILED','CANCELLED');
CREATE TABLE task_chapters (
 task_id TEXT NOT NULL REFERENCES tasks(id), chapter_id INTEGER NOT NULL REFERENCES chapters(id),
 status TEXT NOT NULL DEFAULT 'PENDING', PRIMARY KEY(task_id,chapter_id));
CREATE TABLE notifications (
 id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), event TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0,
 next_at REAL NOT NULL DEFAULT 0, UNIQUE(task_id,event));
CREATE TABLE confirmations (
 token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
 snapshot TEXT NOT NULL, expires REAL NOT NULL, task_id TEXT REFERENCES tasks(id),
 dismissed INTEGER NOT NULL DEFAULT 0);
CREATE INDEX ready_tasks ON tasks(status,next_at);
"""


class Store:
    """Short synchronous transactions, called only on the application event loop.

    No network or filesystem scans run while a write transaction is held.
    user_version is the migration version; unknown future schemas fail closed.
    """
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            self.connection.executescript("BEGIN IMMEDIATE;" + SCHEMA + "PRAGMA user_version=1; COMMIT;")
            version = 1
        if version == 1:
            self.connection.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE users (
                    user_id INTEGER PRIMARY KEY, username TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    blocked INTEGER NOT NULL DEFAULT 0 CHECK(blocked IN (0,1)),
                    registered_at REAL NOT NULL);
                CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO settings VALUES ('registration_open','1');
                PRAGMA user_version=2;
                COMMIT;
            """)
            version = 2
        if version == 2:
            self.connection.executescript("""
                BEGIN IMMEDIATE;
                INSERT INTO settings VALUES ('invite_required','0');
                CREATE TABLE invites (
                    id INTEGER PRIMARY KEY, code_hash TEXT NOT NULL UNIQUE,
                    max_uses INTEGER NOT NULL CHECK(max_uses>0),
                    used INTEGER NOT NULL DEFAULT 0,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    created_by INTEGER NOT NULL, created_at REAL NOT NULL);
                PRAGMA user_version=3;
                COMMIT;
            """)
            version = 3
        if version == 3:
            self.connection.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE komga_accounts (
                    user_id INTEGER PRIMARY KEY REFERENCES users(user_id),
                    email TEXT NOT NULL UNIQUE, password_encrypted TEXT,
                    remote_id TEXT, status TEXT NOT NULL DEFAULT 'PENDING',
                    error_code TEXT, created_at REAL NOT NULL);
                PRAGMA user_version=4;
                COMMIT;
            """)
            version = 4
        if version in {4, 5}:
            from jmemby.db.migrations import FIFO_FROM_V4, REMOVE_AUTOMATION_FROM_V5
            try:
                self.connection.executescript(FIFO_FROM_V4 if version == 4 else REMOVE_AUTOMATION_FROM_V5)
            except BaseException:
                # Closing rolls back an unfinished migration and releases its
                # write lock; never continue with a partially upgraded database.
                self.connection.close()
                raise
            version = 6
        if version != 6:
            self.connection.close()
            raise RuntimeError("Unsupported database version; restore or migrate before startup")

    def close(self):
        self.connection.close()

    @contextmanager
    def transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def get(self, task_id):
        row = self.connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def tasks(self, user_id=None, page=0, active=False):
        clauses, args = [], []
        if user_id is not None:
            clauses.append("user_id=?")
            args.append(user_id)
        if active:
            clauses.append("status NOT IN ('COMPLETED','FAILED','CANCELLED')")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return [dict(r) for r in self.connection.execute(
            "SELECT * FROM tasks" + where + " ORDER BY created_at DESC LIMIT 10 OFFSET ?", (*args, page * 10))]

    def chapters(self, task_id):
        return [dict(r) for r in self.connection.execute(
            "SELECT c.* FROM chapters c JOIN task_chapters tc ON tc.chapter_id=c.id "
            "WHERE tc.task_id=? ORDER BY c.ordinal,c.id", (task_id,))]

    def update_chapter(self, chapter_id, **fields):
        allowed = {"pages", "size", "checksum", "status", "komga_id"}
        if not fields or not fields.keys() <= allowed:
            raise ValueError("invalid chapter fields")
        self.connection.execute("UPDATE chapters SET " + ",".join(f"{k}=?" for k in fields) +
                                " WHERE id=?", (*fields.values(), chapter_id))

    def update(self, task_id, **fields):
        allowed = {"status", "phase", "attempts", "next_at", "cancel_requested", "error_code",
                   "error", "progress", "message_id"}
        if not fields or not fields.keys() <= allowed:
            raise ValueError("invalid task fields")
        fields["updated_at"] = time.time()
        self.connection.execute("UPDATE tasks SET " + ",".join(f"{k}=?" for k in fields) +
                                " WHERE id=?", (*fields.values(), task_id))

    def finish(self, task_id, status, code=None, error=None):
        if status not in TERMINAL:
            raise ValueError("not terminal")
        with self.transaction() as db:
            old = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not old or old[0] in TERMINAL:
                return
            self.update(task_id, status=status, error_code=code, error=error)
            db.execute("INSERT OR IGNORE INTO notifications(task_id,event) VALUES (?,?)", (task_id, status))

    def confirmation(self, manga, user_id, chat_id):
        token = secrets.token_urlsafe(12)
        self.connection.execute("INSERT INTO confirmations(token,user_id,chat_id,snapshot,expires) VALUES (?,?,?,?,?)",
                                (token, user_id, chat_id, json.dumps(asdict(manga)), time.time() + 600))
        return token

    def dismiss(self, token, user_id, chat_id=None):
        with self.transaction() as db:
            confirmation = db.execute("SELECT * FROM confirmations WHERE token=?", (token,)).fetchone()
            self._check_confirmation(confirmation, user_id, chat_id)
            if confirmation["task_id"]:
                raise ValueError("任务已确认，不能取消预览；请私聊使用 /cancel 任务ID")
            db.execute("UPDATE confirmations SET dismissed=1 WHERE token=?", (token,))

    @staticmethod
    def _check_confirmation(confirmation, user_id, chat_id):
        if (not confirmation or confirmation["user_id"] != user_id
                or (chat_id is not None and confirmation["chat_id"] != chat_id)):
            raise ValueError("按钮仅限发起人在原聊天中操作")

    def confirm(self, token, user_id, chat_id=None):
        with self.transaction() as db:
            confirmation = db.execute("SELECT * FROM confirmations WHERE token=?", (token,)).fetchone()
            self._check_confirmation(confirmation, user_id, chat_id)
            if confirmation["dismissed"]:
                raise ValueError("已取消确认")
            if confirmation["task_id"]:
                return self.get(confirmation["task_id"])
            if confirmation["expires"] < time.time():
                raise ValueError("确认已过期，请重新提交")
            task = self._submit(db, decode_manga(confirmation["snapshot"]), user_id, confirmation["chat_id"])
            db.execute("UPDATE confirmations SET task_id=? WHERE token=?", (task["id"], token))
            return task

    def submit(self, manga, user_id, chat_id, retry_of=None):
        with self.transaction() as db:
            return self._submit(db, manga, user_id, chat_id, retry_of)

    def _submit(self, db, manga, user_id, chat_id, retry_of=None):
        if not manga.chapters or len({c.source_id for c in manga.chapters}) != len(manga.chapters):
            raise ValueError("章节清单为空或重复")
        raw = json.dumps(asdict(manga), ensure_ascii=False)
        directory = library_directory(Path(), manga).as_posix()
        db.execute("INSERT OR IGNORE INTO manga(source,source_id,title,directory,metadata) VALUES (?,?,?,?,?)",
                   (manga.source, manga.source_id, manga.title, directory, raw))
        record = db.execute("SELECT * FROM manga WHERE source=? AND source_id=?", (manga.source, manga.source_id)).fetchone()
        active = db.execute("SELECT * FROM tasks WHERE manga_id=? AND status NOT IN ('COMPLETED','FAILED','CANCELLED')",
                            (record["id"],)).fetchone()
        if active:
            if active["user_id"] != user_id:
                raise ValueError("该漫画已有其他用户的下载任务，请稍后再试")
            return dict(active)
        task_id, now = uuid.uuid4().hex, time.time()
        db.execute("INSERT INTO tasks(id,manga_id,snapshot,user_id,chat_id,retry_of,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                   (task_id, record["id"], raw, user_id, chat_id, retry_of, now, now))
        sequence = db.execute("SELECT COALESCE(MAX(queue_sequence),0)+1 FROM tasks").fetchone()[0]
        db.execute("UPDATE tasks SET queue_sequence=? WHERE id=?", (sequence, task_id))
        for chapter in manga.chapters:
            path = (Path(record["directory"]) / chapter_filename(chapter, manga)).as_posix()
            db.execute("INSERT OR IGNORE INTO chapters(manga_id,source_id,ordinal,title,path) VALUES (?,?,?,?,?)",
                       (record["id"], chapter.source_id, chapter.ordinal, chapter.title, path))
            chapter_id = db.execute("SELECT id FROM chapters WHERE manga_id=? AND source_id=?",
                                    (record["id"], chapter.source_id)).fetchone()[0]
            db.execute("INSERT INTO task_chapters(task_id,chapter_id) VALUES (?,?)", (task_id, chapter_id))
        return self.get(task_id)

    def request_cancel(self, task_id, user_id):
        task = self.get(task_id)
        if not task or task["user_id"] != user_id:
            raise ValueError("任务不存在")
        if task["status"] not in TERMINAL:
            self.update(task_id, cancel_requested=1, status="CANCEL_REQUESTED")
            if task["status"] in ("QUEUED", "RETRY_WAIT", "PAUSED_DISK"):
                self.finish(task_id, "CANCELLED", "CANCELLED", "任务已取消；已发布文件保留")

    def recover(self):
        self.connection.execute("UPDATE tasks SET status='QUEUED' WHERE status IN ('DOWNLOADING','PACKAGING','IMPORT_PENDING')")

    def next_task(self):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM tasks WHERE status IN ('QUEUED','RETRY_WAIT','CANCEL_REQUESTED') "
                             "AND (next_at<=? OR cancel_requested=1) ORDER BY queue_sequence LIMIT 1", (time.time(),)).fetchone()
            if row:
                self.update(row["id"], status="DOWNLOADING")
                return self.get(row["id"])

    def notifications(self):
        return [dict(r) for r in self.connection.execute("SELECT * FROM notifications WHERE status='PENDING' AND next_at<=? LIMIT 10", (time.time(),))]

    def notified(self, notification_id):
        self.connection.execute("UPDATE notifications SET status='SENT' WHERE id=?", (notification_id,))

    def defer_notification(self, notification_id, delay):
        self.connection.execute("UPDATE notifications SET attempts=attempts+1,next_at=? WHERE id=?", (time.time() + delay, notification_id))

    def resume_disk(self):
        self.connection.execute("UPDATE tasks SET status='QUEUED' WHERE status='PAUSED_DISK'")
