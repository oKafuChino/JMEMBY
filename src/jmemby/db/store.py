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
        if version == 6:
            try:
                self.connection.executescript("""
                    BEGIN IMMEDIATE;
                    ALTER TABLE users ADD COLUMN user_group TEXT NOT NULL DEFAULT 'normal'
                        CHECK(user_group IN ('normal','white'));
                    CREATE TABLE registration_grants (
                        user_id INTEGER PRIMARY KEY,
                        user_group TEXT NOT NULL CHECK(user_group IN ('normal','white')),
                        granted_by INTEGER NOT NULL, granted_at REAL NOT NULL);
                    PRAGMA user_version=7;
                    COMMIT;
                """)
            except BaseException:
                self.connection.close()
                raise
            version = 7
        if version == 7:
            try:
                self.connection.executescript("""
                    BEGIN IMMEDIATE;
                    ALTER TABLE tasks ADD COLUMN priority INTEGER NOT NULL DEFAULT 0 CHECK(priority IN (0,1));
                    UPDATE tasks SET priority=1 WHERE user_id IN (SELECT user_id FROM users WHERE user_group='white');
                    CREATE INDEX task_priority_sequence ON tasks(priority DESC,queue_sequence);
                    PRAGMA user_version=8;
                    COMMIT;
                """)
            except BaseException:
                self.connection.close()
                raise
            version = 8
        if version == 8:
            try:
                self.connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE download_nodes (
                        id TEXT PRIMARY KEY, label TEXT NOT NULL,
                        token_hash TEXT NOT NULL DEFAULT '',
                        enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
                        concurrency INTEGER NOT NULL DEFAULT 2 CHECK(concurrency BETWEEN 1 AND 16),
                        revision INTEGER NOT NULL DEFAULT 1,
                        deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0,1)),
                        ticket_hash TEXT, ticket_expires REAL,
                        created_at REAL NOT NULL, updated_at REAL NOT NULL);
                    PRAGMA user_version=9;
                    COMMIT;
                """)
            except BaseException:
                self.connection.close()
                raise
            version = 9
        if version == 9:
            try:
                self.connection.executescript("""
                    BEGIN IMMEDIATE;
                    ALTER TABLE download_nodes ADD COLUMN queue_scope TEXT NOT NULL DEFAULT 'all'
                        CHECK(queue_scope IN ('all','normal','white'));
                    PRAGMA user_version=10;
                    COMMIT;
                """)
            except BaseException:
                self.connection.close()
                raise
            version = 10
        if version == 10:
            try:
                self.connection.executescript("""
                    BEGIN IMMEDIATE;
                    ALTER TABLE manga ADD COLUMN removed_at REAL;
                    PRAGMA user_version=11;
                    COMMIT;
                """)
            except BaseException:
                self.connection.close()
                raise
            version = 11
        if version == 11:
            try:
                self.connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE manga_likes (
                        manga_id INTEGER NOT NULL REFERENCES manga(id),
                        user_id INTEGER NOT NULL, created_at REAL NOT NULL,
                        PRIMARY KEY(manga_id,user_id));
                    CREATE TABLE manga_reports (
                        id INTEGER PRIMARY KEY,
                        manga_id INTEGER NOT NULL REFERENCES manga(id),
                        user_id INTEGER NOT NULL, created_at REAL NOT NULL,
                        UNIQUE(manga_id,user_id));
                    CREATE TABLE report_deliveries (
                        report_id INTEGER NOT NULL REFERENCES manga_reports(id),
                        admin_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
                        attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
                        PRIMARY KEY(report_id,admin_id));
                    CREATE INDEX pending_reports ON report_deliveries(status,next_at);
                    PRAGMA user_version=12;
                    COMMIT;
                """)
            except BaseException:
                self.connection.close()
                raise
            version = 12
        if version != 12:
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

    def recent_downloads(self, query=""):
        query = query.strip()
        if len(query) > 200:
            raise ValueError("搜索内容最多 200 个字符")
        fields = ("m.title", "m.source_id", "t.id", "CAST(t.user_id AS TEXT)",
                  "CASE WHEN COALESCE(u.username,'')<>'' THEN '@'||u.username ELSE '' END",
                  "COALESCE(u.display_name,'')")
        where = " WHERE " + " OR ".join(f"instr(lower({field}),lower(?))>0" for field in fields) if query else ""
        rows = self.connection.execute(
            "SELECT t.id,m.title,m.source,m.source_id,t.user_id,u.username,u.display_name,"
            "t.status,t.priority,t.created_at,t.retry_of FROM tasks t "
            "JOIN manga m ON m.id=t.manga_id LEFT JOIN users u ON u.user_id=t.user_id" + where +
            " ORDER BY t.created_at DESC,t.rowid DESC LIMIT 100",
            (query,) * len(fields) if query else ()).fetchall()
        return [dict(row) for row in rows]

    def download_history(self, source, source_id, page=1):
        if not 1 <= page <= 1_000_000:
            raise ValueError("页码无效")
        manga = self.connection.execute(
            "SELECT id,title FROM manga WHERE source=? AND source_id=?", (source, source_id)).fetchone()
        if not manga:
            raise ValueError("该漫画暂无本项目的下载记录")
        query = ("SELECT t.id,t.user_id,t.status,t.created_at,t.updated_at,t.retry_of,"
                 "u.username,u.display_name FROM tasks t LEFT JOIN users u ON u.user_id=t.user_id "
                 "WHERE t.manga_id=?")
        first = self.connection.execute(query + " ORDER BY t.created_at,t.rowid LIMIT 1", (manga["id"],)).fetchone()
        completed = self.connection.execute(
            query + " AND t.status='COMPLETED' ORDER BY t.updated_at,t.rowid LIMIT 1", (manga["id"],)).fetchone()
        total = self.connection.execute("SELECT COUNT(*) FROM tasks WHERE manga_id=?", (manga["id"],)).fetchone()[0]
        rows = self.connection.execute(
            query + " ORDER BY t.created_at DESC,t.rowid DESC LIMIT 5 OFFSET ?",
            (manga["id"], (page - 1) * 5)).fetchall()
        return {"source_id": source_id, "title": manga["title"], "page": page, "total": total,
                "first": dict(first) if first else None,
                "completed": dict(completed) if completed else None,
                "tasks": [dict(row) for row in rows]}

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

    def user_priority(self, user_id):
        row = self.connection.execute("SELECT user_group,blocked FROM users WHERE user_id=?", (user_id,)).fetchone()
        return int(bool(row and row["user_group"] == "white" and not row["blocked"]))

    def check_submission(self, source, source_id, user_id):
        with self.transaction():
            promoted = self.reject_duplicate(source, source_id, user_id)
        if promoted:
            raise ValueError("该漫画已有任务，已将原任务移至白名单优先队列末尾，未创建新任务")

    def reject_duplicate(self, source, source_id, user_id=None):
        record = self.connection.execute(
            "SELECT id,removed_at FROM manga WHERE source=? AND source_id=?", (source, source_id)).fetchone()
        if not record:
            return
        if self.connection.execute(
                "SELECT 1 FROM tasks WHERE manga_id=? AND status='COMPLETED' "
                "AND (? IS NULL OR created_at>?) LIMIT 1",
                (record["id"], record["removed_at"], record["removed_at"])).fetchone():
            raise ValueError("该漫画已下载完成，拒绝重复下载，请前往漫画库阅读")
        active = self.connection.execute(
                "SELECT * FROM tasks WHERE manga_id=? AND status NOT IN ('COMPLETED','FAILED','CANCELLED') LIMIT 1",
                (record["id"],)).fetchone()
        if active:
            if (self.user_priority(user_id) and not active["priority"]
                    and active["status"] in {"QUEUED", "RETRY_WAIT", "PAUSED_DISK"}
                    and not active["cancel_requested"]):
                sequence = self.connection.execute("SELECT COALESCE(MAX(queue_sequence),0)+1 FROM tasks").fetchone()[0]
                self.connection.execute("UPDATE tasks SET priority=1,queue_sequence=?,updated_at=? WHERE id=?",
                                        (sequence, time.time(), active["id"]))
                return True
            raise ValueError("该漫画已有进行中的任务，拒绝重复下载")

    def confirmation(self, manga, user_id, chat_id):
        self.check_submission(manga.source, manga.source_id, user_id)
        token = secrets.token_urlsafe(12)
        self.connection.execute("INSERT INTO confirmations(token,user_id,chat_id,snapshot,expires) VALUES (?,?,?,?,?)",
                                (token, user_id, chat_id, json.dumps(asdict(manga)), time.time() + 600))
        return token

    def dismiss(self, token, user_id, chat_id=None):
        with self.transaction() as db:
            confirmation = db.execute("SELECT * FROM confirmations WHERE token=?", (token,)).fetchone()
            self._check_confirmation(confirmation, user_id, chat_id)
            if confirmation["task_id"]:
                raise ValueError("任务已确认，不能取消预览；请联系管理员在网页下载队列取消")
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
            if task is not None:
                db.execute("UPDATE confirmations SET task_id=? WHERE token=?", (task["id"], token))
        if task is None:
            raise ValueError("该漫画已有任务，已将原任务移至白名单优先队列末尾，未创建新任务")
        return task

    def submit(self, manga, user_id, chat_id, retry_of=None):
        with self.transaction() as db:
            task = self._submit(db, manga, user_id, chat_id, retry_of)
        if task is None:
            raise ValueError("该漫画已有任务，已将原任务移至白名单优先队列末尾，未创建新任务")
        return task

    def _submit(self, db, manga, user_id, chat_id, retry_of=None):
        if not manga.chapters or len({c.source_id for c in manga.chapters}) != len(manga.chapters):
            raise ValueError("章节清单为空或重复")
        # Called under BEGIN IMMEDIATE: another submission cannot pass this
        # check and enqueue the same work between the check and INSERT.
        if self.reject_duplicate(manga.source, manga.source_id, user_id):
            return None
        raw = json.dumps(asdict(manga), ensure_ascii=False)
        directory = library_directory(Path(), manga).as_posix()
        db.execute("INSERT OR IGNORE INTO manga(source,source_id,title,directory,metadata) VALUES (?,?,?,?,?)",
                   (manga.source, manga.source_id, manga.title, directory, raw))
        record = db.execute("SELECT * FROM manga WHERE source=? AND source_id=?", (manga.source, manga.source_id)).fetchone()
        task_id, now = uuid.uuid4().hex, time.time()
        db.execute("INSERT INTO tasks(id,manga_id,snapshot,user_id,chat_id,retry_of,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                   (task_id, record["id"], raw, user_id, chat_id, retry_of, now, now))
        sequence = db.execute("SELECT COALESCE(MAX(queue_sequence),0)+1 FROM tasks").fetchone()[0]
        db.execute("UPDATE tasks SET queue_sequence=?,priority=? WHERE id=?", (sequence, self.user_priority(user_id), task_id))
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

    def next_task(self, exclude=(), queue_scope='all'):
        if queue_scope not in ('all', 'normal', 'white'):
            raise ValueError('节点服务队列无效')
        # Active cancellations still belong to the worker that is unwinding.
        excluded = tuple(exclude)
        exclusion = ' AND id NOT IN (' + ','.join('?' for _ in excluded) + ')' if excluded else ''
        selection = '' if queue_scope == 'all' else ' AND priority=?'
        priorities = () if queue_scope == 'all' else (int(queue_scope == 'white'),)
        with self.transaction() as db:
            row = db.execute("SELECT * FROM tasks WHERE status IN ('QUEUED','RETRY_WAIT','CANCEL_REQUESTED') "
                             "AND (next_at<=? OR cancel_requested=1)" + exclusion + selection +
                             " ORDER BY priority DESC,queue_sequence LIMIT 1", (time.time(), *excluded, *priorities)).fetchone()
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
