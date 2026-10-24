"""Library-only reactions, recommendations and durable admin report delivery."""
import re
import time


# Only successfully imported, non-removed editions appear in discovery.
AVAILABLE = ("EXISTS(SELECT 1 FROM tasks t WHERE t.manga_id=m.id AND t.status='COMPLETED' "
             "AND (m.removed_at IS NULL OR t.created_at>m.removed_at)) "
             "AND EXISTS(SELECT 1 FROM chapters c WHERE c.manga_id=m.id AND c.status='IMPORTED')")


class CommunityService:
    def __init__(self, store, users):
        self.store, self.users = store, users

    def manga(self, actor, source_id):
        self.users.require_member(actor)
        if not isinstance(source_id, str) or not re.fullmatch(r'[1-9][0-9]{0,15}', source_id.strip()):
            raise ValueError('请输入漫画数字 ID，例如 /like 123456 或 /report 123456')
        source_id = str(int(source_id.strip()))
        row = self.store.connection.execute(
            'SELECT m.id,m.source_id,m.title FROM manga m WHERE m.source=? AND m.source_id=? AND ' + AVAILABLE,
            ('jmcomic', source_id)).fetchone()
        if not row:
            raise ValueError('漫画不在当前可阅读的漫画库中，请检查 ID')
        return dict(row)

    def like(self, actor, source_id):
        manga = self.manga(actor, source_id)
        with self.store.transaction() as db:
            added = db.execute('INSERT OR IGNORE INTO manga_likes(manga_id,user_id,created_at) VALUES (?,?,?)',
                               (manga['id'], actor, time.time())).rowcount == 1
            likes = db.execute('SELECT COUNT(*) FROM manga_likes WHERE manga_id=?', (manga['id'],)).fetchone()[0]
        return {**manga, 'added': added, 'likes': likes}

    def report(self, actor, source_id):
        manga = self.manga(actor, source_id)
        if not self.users.admin_ids:
            raise ValueError('尚未配置管理员，暂时无法提交举报')
        with self.store.transaction() as db:
            inserted = db.execute('INSERT OR IGNORE INTO manga_reports(manga_id,user_id,created_at) VALUES (?,?,?)',
                                  (manga['id'], actor, time.time())).rowcount == 1
            if inserted:
                report_id = db.execute('SELECT id FROM manga_reports WHERE manga_id=? AND user_id=?',
                                       (manga['id'], actor)).fetchone()[0]
                db.executemany('INSERT INTO report_deliveries(report_id,admin_id) VALUES (?,?)',
                               [(report_id, admin) for admin in sorted(self.users.admin_ids)])
        return {**manga, 'added': inserted}

    def random(self, actor):
        self.users.require_member(actor)
        row = self.store.connection.execute(
            'SELECT m.source_id,m.title,(SELECT COUNT(*) FROM manga_likes l WHERE l.manga_id=m.id) AS likes '
            "FROM manga m WHERE m.source='jmcomic' AND " + AVAILABLE + ' ORDER BY RANDOM() LIMIT 1').fetchone()
        return dict(row) if row else None

    def rank(self, actor):
        self.users.require_member(actor)
        return [dict(row) for row in self.store.connection.execute(
            'SELECT m.source_id,m.title,COUNT(l.user_id) AS likes FROM manga m '
            "JOIN manga_likes l ON l.manga_id=m.id WHERE m.source='jmcomic' AND " + AVAILABLE +
            ' GROUP BY m.id ORDER BY likes DESC,m.id ASC LIMIT 10')]

    def pending_reports(self):
        return [dict(row) for row in self.store.connection.execute(
            'SELECT d.report_id,d.admin_id,d.attempts,r.user_id,m.source_id,m.title FROM report_deliveries d '
            'JOIN manga_reports r ON r.id=d.report_id JOIN manga m ON m.id=r.manga_id '
            "WHERE d.status='PENDING' AND r.status='PENDING' AND d.next_at<=? ORDER BY r.id,d.admin_id LIMIT 20", (time.time(),))]

    def report_pending(self, report_id):
        row = self.store.connection.execute('SELECT status FROM manga_reports WHERE id=?', (report_id,)).fetchone()
        return bool(row and row['status'] == 'PENDING')

    def review_list(self, actor, page=1, status='PENDING', query=''):
        self.users.require_admin(actor)
        if type(page) is not int or not 1 <= page <= 1000000:
            raise ValueError('页码无效')
        if status not in ('PENDING', 'RESOLVED', 'DISMISSED', 'all'):
            raise ValueError('举报状态无效')
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError('搜索关键词最多 200 个字符')
        clauses, params = [], []
        if status != 'all':
            clauses.append('r.status=?'); params.append(status)
        if query.strip():
            pattern = '%' + query.strip().replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
            clauses.append("(m.title LIKE ? ESCAPE '!' OR m.source_id LIKE ? ESCAPE '!' OR CAST(r.user_id AS TEXT) LIKE ? ESCAPE '!')")
            params.extend([pattern]*3)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        source = ' FROM manga_reports r JOIN manga m ON m.id=r.manga_id'
        total = self.store.connection.execute('SELECT COUNT(*)' + source + where, params).fetchone()[0]
        rows = self.store.connection.execute(
            'SELECT r.*,m.source_id,m.title,u.username,u.display_name,'
            "EXISTS(SELECT 1 FROM tasks t WHERE t.manga_id=m.id AND t.status NOT IN ('COMPLETED','FAILED','CANCELLED')) AS busy"
            + source + ' LEFT JOIN users u ON u.user_id=r.user_id' + where +
            " ORDER BY CASE WHEN r.status='PENDING' THEN 0 ELSE 1 END,r.created_at DESC,r.id DESC LIMIT 20 OFFSET ?",
            (*params, (page-1)*20)).fetchall()
        pending = self.store.connection.execute("SELECT COUNT(*) FROM manga_reports WHERE status='PENDING'").fetchone()[0]
        return {'reports': [dict(r) for r in rows], 'total': total, 'pending': pending, 'page': page, 'limit': 20}

    def review_target(self, actor, report_id):
        self.users.require_admin(actor)
        if type(report_id) is not int or report_id <= 0:
            raise ValueError('举报编号无效')
        row = self.store.connection.execute('SELECT * FROM manga_reports WHERE id=?', (report_id,)).fetchone()
        if not row:
            raise ValueError('举报不存在')
        if row['status'] != 'PENDING':
            raise ValueError('该举报已处理，请刷新列表')
        return dict(row)

    def dismiss_report(self, actor, report_id):
        self.review_target(actor, report_id)
        with self.store.transaction() as db:
            changed = db.execute("UPDATE manga_reports SET status='DISMISSED',handled_by=?,handled_at=? "
                                 "WHERE id=? AND status='PENDING'", (actor, time.time(), report_id)).rowcount
            if not changed:
                raise ValueError('该举报已处理，请刷新列表')
            db.execute("UPDATE report_deliveries SET status='SKIPPED' WHERE report_id=? AND status='PENDING'", (report_id,))

    def delivered(self, report_id, admin_id, status='SENT'):
        self.store.connection.execute('UPDATE report_deliveries SET status=? WHERE report_id=? AND admin_id=?',
                                      (status, report_id, admin_id))

    def defer(self, report_id, admin_id, delay):
        self.store.connection.execute('UPDATE report_deliveries SET attempts=attempts+1,next_at=? '
                                      'WHERE report_id=? AND admin_id=?', (time.time()+delay, report_id, admin_id))
