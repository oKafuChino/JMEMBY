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
            "WHERE d.status='PENDING' AND d.next_at<=? ORDER BY r.id,d.admin_id LIMIT 20", (time.time(),))]

    def delivered(self, report_id, admin_id, status='SENT'):
        self.store.connection.execute('UPDATE report_deliveries SET status=? WHERE report_id=? AND admin_id=?',
                                      (status, report_id, admin_id))

    def defer(self, report_id, admin_id, delay):
        self.store.connection.execute('UPDATE report_deliveries SET attempts=attempts+1,next_at=? '
                                      'WHERE report_id=? AND admin_id=?', (time.time()+delay, report_id, admin_id))
