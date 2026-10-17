"""Durable schedule management, idempotent admission, recovery and summaries."""
import asyncio
import json
import secrets
import time
import uuid
from dataclasses import replace
from pathlib import Path

from jmemby.db.store import TERMINAL, decode_manga
from jmemby.domain.discovery import POLICY_VERSION
from jmemby.services.schedule_time import next_time, latest_due
from jmemby.storage.cbz import contained, validate_cbz
from jmemby.domain.models import Page
from jmemby.domain.errors import JobError
from jmemby.services.schedule_display import reason_label, SORT_LABELS, RUN_LABELS

ACTIVE_RUNS = ("DISCOVERING", "PLANNED", "QUEUED", "CANCELLING")


class ScheduleService:
    def __init__(self, store, users, provider, storage_root, clock=time.time):
        self.store, self.db, self.users = store, store.connection, users
        self.provider, self.root, self.clock = provider, Path(storage_root), clock
        self.discovery_lock = asyncio.Lock()

    def owned(self, schedule_id, actor):
        self.users.require_admin(actor)
        row = self.db.execute("SELECT * FROM schedules WHERE id=? AND owner_user_id=?", (schedule_id, actor)).fetchone()
        if row is None:
            raise ValueError("计划不存在或不属于你")
        return dict(row)

    def create(self, actor, tag, spec, limit=5):
        self.users.require_admin(actor)
        tag = " ".join(tag.split())
        if not tag or len(tag) > 100 or any(ord(c) < 32 for c in tag):
            raise ValueError("请输入 1–100 字符的一个标签")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("每次数量须为 1–20")
        next_at = next_time(spec, self.clock())
        if next_at is None:
            raise ValueError("指定时间已过期或不存在")
        cursor = self.db.execute("INSERT INTO schedules(owner_user_id,chat_id,tag,spec,item_limit,next_run_at,created_at) VALUES (?,?,?,?,?,?,?)",
                                 (actor, actor, tag, json.dumps(spec), limit, next_at, self.clock()))
        return self.owned(cursor.lastrowid, actor)

    def list(self, actor, page=1):
        self.users.require_admin(actor)
        if not 1 <= page <= 100000:
            raise ValueError("页码无效")
        return [dict(r) for r in self.db.execute("SELECT * FROM schedules WHERE owner_user_id=? ORDER BY id DESC LIMIT 10 OFFSET ?", (actor, (page-1)*10))]

    def pause(self, schedule_id, actor):
        self.owned(schedule_id, actor)
        with self.store.transaction():
            self.db.execute("UPDATE schedules SET enabled=0,revision=revision+1 WHERE id=?", (schedule_id,))
            for r in self.db.execute("SELECT id FROM schedule_runs WHERE schedule_id=? AND status IN ('DISCOVERING','PLANNED')", (schedule_id,)).fetchall():
                self.end_run(r[0], "CANCELLED", "计划已暂停，未入队的批次已取消")

    def action(self, schedule_id, actor, action):
        schedule = self.owned(schedule_id, actor)
        if action not in {"enable", "run"} and not action.startswith("cancel:"):
            raise ValueError("无效操作")
        if action.startswith("cancel:"):
            run = self.db.execute("SELECT schedule_id FROM schedule_runs WHERE id=?", (action[7:],)).fetchone()
            if not run or run[0] != schedule_id:
                raise ValueError("批次不存在")
        token = secrets.token_urlsafe(12)
        self.db.execute("INSERT INTO schedule_actions VALUES (?,?,?,?,?,?,NULL)",
                        (token, actor, schedule_id, action, self.clock() + 600, schedule["revision"]))
        return token

    def confirm_action(self, token, actor):
        self.users.require_admin(actor)
        pending = self.db.execute("SELECT * FROM schedule_actions WHERE token=? AND owner_user_id=?", (token, actor)).fetchone()
        if pending and pending["expires"] >= self.clock() and pending["action"].startswith("cancel:"):
            self.owned(pending["schedule_id"], actor)
            if pending["result"]:
                return pending["result"]
            self.cancel_run(pending["action"][7:], actor)
            self.db.execute("UPDATE schedule_actions SET result='已请求取消批次' WHERE token=?", (token,))
            return "已请求取消批次"
        with self.store.transaction():
            row = self.db.execute("SELECT * FROM schedule_actions WHERE token=? AND owner_user_id=?", (token, actor)).fetchone()
            if not row or row["expires"] < self.clock():
                raise ValueError("确认已失效，请重新操作")
            schedule = self.owned(row["schedule_id"], actor)
            if row["result"]:
                return row["result"]
            if row["revision"] != schedule["revision"]:
                raise ValueError("计划已发生变化，请重新确认")
            if row["action"] == "enable":
                next_at = next_time(json.loads(schedule["spec"]), self.clock())
                if next_at is None:
                    raise ValueError("计划时间已过期，请新建计划")
                self.db.execute("UPDATE schedules SET enabled=1,next_run_at=?,revision=revision+1 WHERE id=?", (next_at, schedule["id"]))
                result = "计划已启用"
            else:
                if self.has_active(schedule["id"]):
                    raise ValueError("该计划已有未结束运行")
                result = self.new_run(schedule, self.clock(), "manual")
            self.db.execute("UPDATE schedule_actions SET result=? WHERE token=?", (result, token))
            return result

    def has_active(self, schedule_id):
        return self.db.execute("SELECT 1 FROM schedule_runs WHERE schedule_id=? AND status IN ('DISCOVERING','PLANNED','QUEUED','CANCELLING')", (schedule_id,)).fetchone() is not None

    def new_run(self, schedule, at, trigger, status="DISCOVERING", error=None):
        run_id = uuid.uuid4().hex
        snapshot = dict(schedule)
        snapshot["policy_version"] = POLICY_VERSION
        self.db.execute("INSERT INTO schedule_runs(id,schedule_id,scheduled_for,trigger_type,status,snapshot,created_at,error) VALUES (?,?,?,?,?,?,?,?)",
                        (run_id, schedule["id"], at, trigger, status, json.dumps(snapshot), self.clock(), error))
        if status != "DISCOVERING":
            self.notice(run_id, "finished", f"计划 #{schedule['id']}：{RUN_LABELS.get(status, status)}；{error or ''}")
        return run_id

    def claim_due(self):
        now = self.clock()
        with self.store.transaction():
            rows = self.db.execute("SELECT * FROM schedules WHERE enabled=1 AND next_run_at<=?", (now,)).fetchall()
            for row in rows:
                s = dict(row)
                if not self.users.is_admin(s["owner_user_id"]):
                    self.db.execute("UPDATE schedules SET enabled=0,revision=revision+1 WHERE id=?", (s["id"],))
                    continue
                spec = json.loads(s["spec"])
                due = latest_due(spec, now)
                if due is None or due < s["next_run_at"]:
                    continue
                following = next_time(spec, now)
                self.db.execute("UPDATE schedules SET next_run_at=?,enabled=? WHERE id=?", (following, int(following is not None), s["id"]))
                if self.db.execute("SELECT 1 FROM schedule_runs WHERE schedule_id=? AND scheduled_for=? AND trigger_type='timer'", (s["id"], due)).fetchone():
                    continue
                status, reason = "DISCOVERING", None
                if self.has_active(s["id"]):
                    status, reason = "OVERLAP_SKIPPED", "上一批次尚未结束"
                elif now - due > 1800:
                    status, reason = "MISSED", "错过执行时间超过 30 分钟"
                if due > s["next_run_at"]:
                    reason = (reason + "；" if reason else "") + "更早的执行点已合并跳过"
                self.new_run(s, due, "timer", status, reason)

    def valid_run(self, run):
        snap = json.loads(run["snapshot"])
        s = self.db.execute("SELECT * FROM schedules WHERE id=?", (run["schedule_id"],)).fetchone()
        return bool(s and s["revision"] == snap["revision"] and self.users.is_admin(s["owner_user_id"]))

    def skip(self, source, source_id):
        state = self.existing_state(source, source_id)
        return self.check_existing(*state) if isinstance(state, tuple) else state

    async def skip_async(self, source, source_id):
        state = self.existing_state(source, source_id)
        if isinstance(state, tuple):
            return await asyncio.to_thread(self.check_existing, *state)
        return state

    def existing_state(self, source, source_id):
        if self.db.execute("SELECT 1 FROM auto_download_claims WHERE source=? AND source_id=?", (source, source_id)).fetchone():
            return "AUTO_ALREADY_ATTEMPTED"
        row = self.db.execute("SELECT id,metadata FROM manga WHERE source=? AND source_id=?", (source, source_id)).fetchone()
        if not row:
            return None
        if self.db.execute("SELECT 1 FROM tasks WHERE manga_id=? AND status NOT IN ('COMPLETED','FAILED','CANCELLED')", (row["id"],)).fetchone():
            return "ACTIVE_TASK"
        chapters = [dict(c) for c in self.db.execute("SELECT * FROM chapters WHERE manga_id=?", (row["id"],)).fetchall()]
        return dict(row), chapters

    def check_existing(self, row, chapters):
        try:
            manga = decode_manga(row["metadata"])
            if not chapters:
                return "NEEDS_REPAIR"
            mapping = {c.source_id: c for c in manga.chapters}
            for chapter in chapters:
                if chapter["status"] != "IMPORTED" or not chapter["pages"]:
                    return "NEEDS_REPAIR"
                detail = replace(mapping[chapter["source_id"]], pages=tuple(Page(**p) for p in json.loads(chapter["pages"])))
                _, checksum = validate_cbz(contained(self.root / "library", chapter["path"]), manga, detail)
                if checksum != chapter["checksum"]:
                    return "NEEDS_REPAIR"
            return "ALREADY_IMPORTED"
        except (OSError, ValueError, KeyError, JobError):
            return "NEEDS_REPAIR"

    def save_plan(self, run, result):
        with self.store.transaction():
            current = self.db.execute("SELECT * FROM schedule_runs WHERE id=?", (run["id"],)).fetchone()
            if current["status"] != "DISCOVERING":
                return
            if not self.valid_run(run):
                self.end_run(run["id"], "CANCELLED", "计划已暂停或权限变化")
                return
            self.db.execute("DELETE FROM schedule_run_items WHERE run_id=?", (run["id"],))
            for item in result["items"]:
                self.db.execute("INSERT INTO schedule_run_items(run_id,source,source_id,rank,likes_raw,likes_exact,checked_at,policy_version,decision,reason,snapshot) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (run["id"], item["source"], item["source_id"], item["rank"], item["likes_raw"], item["likes_exact"], item["checked_at"], POLICY_VERSION, item["decision"], item["reason"], json.dumps(item["snapshot"])))
            stats = {k: result[k] for k in ("mode", "scope", "truncated")}
            stats["candidates"] = len(result["items"])
            self.db.execute("UPDATE schedule_runs SET status='PLANNED',stats=? WHERE id=?", (json.dumps(stats), run["id"]))

    async def prepare_admission(self, run):
        items = self.db.execute("SELECT source,source_id FROM schedule_run_items WHERE run_id=? AND decision='ALLOW'", (run["id"],)).fetchall()
        return {(i["source"], i["source_id"]): await self.skip_async(i["source"], i["source_id"]) for i in items}

    def admit(self, run, skips=None):
        # File checks, if needed, happen outside the transaction.
        items = self.db.execute("SELECT * FROM schedule_run_items WHERE run_id=? ORDER BY rank", (run["id"],)).fetchall()
        if skips is None:
            skips = {(i["source"], i["source_id"]): self.skip(i["source"], i["source_id"]) for i in items if i["decision"] == "ALLOW"}
        with self.store.transaction():
            current = self.db.execute("SELECT * FROM schedule_runs WHERE id=?", (run["id"],)).fetchone()
            if current["status"] != "PLANNED":
                return
            if not self.valid_run(run):
                self.end_run(run["id"], "CANCELLED", "规则或权限已变化")
                return
            config = json.loads(run["snapshot"])
            count = 0
            capacity = max(0, 50 - self.pending_count())
            for i in items:
                if i["decision"] != "ALLOW":
                    continue
                reason = skips.get((i["source"], i["source_id"]))
                if self.db.execute("SELECT 1 FROM auto_download_claims WHERE source=? AND source_id=?", (i["source"], i["source_id"])).fetchone():
                    reason = "AUTO_ALREADY_ATTEMPTED"
                active = self.db.execute("SELECT 1 FROM tasks t JOIN manga m ON m.id=t.manga_id WHERE m.source=? AND m.source_id=? AND t.status NOT IN ('COMPLETED','FAILED','CANCELLED')", (i["source"], i["source_id"])).fetchone()
                if active:
                    reason = "ACTIVE_TASK"
                if not reason and count >= min(config["item_limit"], capacity):
                    reason = "NOT_SELECTED"
                if reason:
                    self.db.execute("UPDATE schedule_run_items SET decision='SKIP',reason=? WHERE run_id=? AND source=? AND source_id=?", (reason, run["id"], i["source"], i["source_id"]))
                    continue
                task = self.store._submit(self.db, decode_manga(i["snapshot"]), config["owner_user_id"], config["chat_id"])
                self.db.execute("UPDATE tasks SET origin='schedule',schedule_run_id=?,policy_version=? WHERE id=?", (run["id"], POLICY_VERSION, task["id"]))
                self.db.execute("INSERT INTO auto_download_claims VALUES (?,?,?,?)", (i["source"], i["source_id"], run["id"], task["id"]))
                self.db.execute("UPDATE schedule_run_items SET decision='QUEUED',task_id=? WHERE run_id=? AND source=? AND source_id=?", (task["id"], run["id"], i["source"], i["source_id"]))
                count += 1
            self.db.execute("UPDATE schedule_runs SET status=? WHERE id=?", ("QUEUED" if count else "EMPTY", run["id"]))
            self.notice(run["id"], "queued" if count else "finished", self.summary(run["id"]))

    def pending_count(self):
        return self.db.execute("SELECT COUNT(*) FROM tasks WHERE origin='schedule' AND status NOT IN ('COMPLETED','FAILED','CANCELLED')").fetchone()[0]

    def end_run(self, run_id, status, error=None):
        self.db.execute("UPDATE schedule_runs SET status=?,error=? WHERE id=?", (status, error, run_id))
        self.notice(run_id, "finished", self.summary(run_id))

    def notice(self, run_id, event, text):
        self.db.execute("INSERT OR IGNORE INTO schedule_notifications(run_id,event,text) VALUES (?,?,?)", (run_id, event, text))

    def summary(self, run_id):
        r = self.db.execute("SELECT * FROM schedule_runs WHERE id=?", (run_id,)).fetchone()
        rows = self.db.execute("SELECT reason,COUNT(*) FROM schedule_run_items WHERE run_id=? GROUP BY reason", (run_id,)).fetchall()
        counts = self.db.execute("SELECT t.status,COUNT(*) FROM schedule_run_items i JOIN tasks t ON t.id=i.task_id WHERE i.run_id=? GROUP BY t.status", (run_id,)).fetchall()
        from jmemby.bot.presentation import STATUS
        stats = json.loads(r["stats"])
        scope = "全部时间" if stats.get("scope") == "all" else stats.get("scope", "未确定")
        return (f"计划 #{r['schedule_id']} / 批次 {run_id}\n状态：{RUN_LABELS.get(r['status'], r['status'])}\n"
                + "；".join(f"{STATUS.get(c[0], c[0])} {c[1]} 部" for c in counts) + "\n"
                + "；".join(f"{reason_label(c[0])} {c[1]} 部" for c in rows)
                + f"\n排序：{SORT_LABELS.get(stats.get('mode'), '未确定')}；范围：{scope}"
                + ("；达到候选预算" if stats.get("truncated") else "")
                + f"\n{reason_label(r['error']) if r['error'] else ''}")[:3900]

    def reconcile(self):
        # Persisted intent is replayed after a crash between cancellation requests.
        cancelling = self.db.execute("SELECT id FROM schedule_runs WHERE status='CANCELLING'").fetchall()
        for run in cancelling:
            for task in self.db.execute("SELECT id,user_id FROM tasks WHERE schedule_run_id=? AND status NOT IN ('COMPLETED','FAILED','CANCELLED')", (run[0],)).fetchall():
                self.store.request_cancel(task[0], task[1])
        with self.store.transaction():
            for r in self.db.execute("SELECT id,status FROM schedule_runs WHERE status IN ('QUEUED','CANCELLING')").fetchall():
                statuses = [x[0] for x in self.db.execute("SELECT t.status FROM schedule_run_items i JOIN tasks t ON t.id=i.task_id WHERE i.run_id=?", (r[0],))]
                if statuses and all(s in TERMINAL for s in statuses):
                    status = "CANCELLED" if r["status"] == "CANCELLING" else "FINISHED" if all(s == "COMPLETED" for s in statuses) else "PARTIAL" if "COMPLETED" in statuses else "FAILED"
                    self.end_run(r[0], status)

    def history(self, schedule_id, actor):
        self.owned(schedule_id, actor)
        return [dict(r) for r in self.db.execute("SELECT * FROM schedule_runs WHERE schedule_id=? ORDER BY created_at DESC LIMIT 10", (schedule_id,))]

    def cancel_run(self, run_id, actor):
        r = self.db.execute("SELECT * FROM schedule_runs WHERE id=?", (run_id,)).fetchone()
        if not r:
            raise ValueError("批次不存在")
        self.owned(r["schedule_id"], actor)
        if r["status"] not in ACTIVE_RUNS:
            return
        with self.store.transaction():
            if r["status"] in {"DISCOVERING", "PLANNED"}:
                self.end_run(run_id, "CANCELLED", "管理员取消本次运行；已发布文件保留")
            else:
                self.db.execute("UPDATE schedule_runs SET status='CANCELLING',error=? WHERE id=?", ("管理员请求取消；已发布文件保留", run_id))
        self.reconcile()
