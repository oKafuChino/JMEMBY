"""Admin library management; keep task history while removing owned CBZ files."""
import logging
from pathlib import Path
import stat
import time

from jmemby.db.store import TERMINAL

logger = logging.getLogger(__name__)


class LibraryService:
    def __init__(self, store, users, storage_root, komga, coordinator=None):
        self.store, self.users, self.komga = store, users, komga
        self.root = Path(storage_root).resolve() / 'library'
        self.coordinator = coordinator

    def list(self, actor, page=1, query=''):
        self.users.require_admin(actor)
        if type(page) is not int or not 1 <= page <= 1000000:
            raise ValueError('页码无效')
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError('搜索关键词最多 200 个字符')
        query = query.strip()
        where = (" WHERE (m.removed_at IS NULL OR EXISTS (SELECT 1 FROM tasks t "
                 "WHERE t.manga_id=m.id AND t.created_at>m.removed_at))")
        params = ()
        if query:
            pattern = '%' + query.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
            where += " AND (m.title LIKE ? ESCAPE '!' OR m.source_id LIKE ? ESCAPE '!')"
            params = (pattern, pattern)
        total = self.store.connection.execute('SELECT COUNT(*) FROM manga m' + where, params).fetchone()[0]
        rows = self.store.connection.execute(
            "SELECT m.id,m.source,m.source_id,m.title,COUNT(c.id) AS chapters,"
            "SUM(CASE WHEN c.status IN ('PUBLISHED','IMPORTED') THEN 1 ELSE 0 END) AS saved_chapters,"
            "COALESCE(SUM(c.size),0) AS size,"
            "(SELECT COUNT(*) FROM manga_likes l WHERE l.manga_id=m.id) AS likes,"
            "origin.user_id AS source_user_id,u.username AS source_username,"
            "u.display_name AS source_display_name,"
            "EXISTS(SELECT 1 FROM tasks t WHERE t.manga_id=m.id AND "
            "t.status NOT IN ('COMPLETED','FAILED','CANCELLED')) AS busy "
            'FROM manga m LEFT JOIN chapters c ON c.manga_id=m.id '
            # Prefer the successful downloader in this library lifecycle. Old
            # completions before removal must not own a later user's re-download.
            'LEFT JOIN tasks origin ON origin.id=COALESCE('
            "(SELECT t.id FROM tasks t WHERE t.manga_id=m.id AND t.status='COMPLETED' "
            'AND (m.removed_at IS NULL OR t.created_at>m.removed_at) '
            'ORDER BY t.updated_at,t.rowid LIMIT 1),'
            '(SELECT t.id FROM tasks t WHERE t.manga_id=m.id '
            'AND (m.removed_at IS NULL OR t.created_at>m.removed_at) '
            'ORDER BY t.created_at DESC,t.rowid DESC LIMIT 1)) '
            'LEFT JOIN users u ON u.user_id=origin.user_id' + where +
            ' GROUP BY m.id ORDER BY m.id DESC LIMIT 20 OFFSET ?', (*params, (page-1)*20)).fetchall()
        return {'manga': [dict(row) for row in rows], 'total': total, 'page': page, 'limit': 20}

    def _path(self, relative):
        # Never resolve a link into a different file and then unlink that target.
        path = Path(relative)
        if path.is_absolute() or path.drive or '..' in path.parts or not path.parts:
            raise ValueError('漫画文件路径无效，已拒绝移除')
        current = self.root
        for part in (None, *path.parts):
            if part is not None:
                current = current / part
            if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
                raise ValueError('漫画路径含链接，已拒绝移除')
        if not current.resolve().is_relative_to(self.root.resolve()) or current == self.root:
            raise ValueError('漫画文件路径超出漫画库')
        return current

    def remove_files(self, actor, manga_id):
        """Synchronous on the shared event loop: claims cannot race the deletion.

        Validate every owned path first. Only unlink recorded files, never recursively
        remove a directory. Missing files are accepted so interrupted removals can retry.
        The removal marker is committed only after every unlink has succeeded.
        """
        self.users.require_admin(actor)
        if type(manga_id) is not int or manga_id <= 0:
            raise ValueError('漫画编号无效')
        manga = self.store.connection.execute('SELECT * FROM manga WHERE id=?', (manga_id,)).fetchone()
        if not manga:
            raise ValueError('漫画不存在')
        tasks = self.store.connection.execute('SELECT id,status FROM tasks WHERE manga_id=?', (manga_id,)).fetchall()
        active = {b['task_id'] for b in self.coordinator.books.values()} if self.coordinator else set()
        if any(t['status'] not in TERMINAL or t['id'] in active for t in tasks):
            raise ValueError('该漫画还有未结束的任务，请先取消任务并等待节点释放后移除')
        records = self.store.connection.execute('SELECT path FROM chapters WHERE manga_id=?', (manga_id,)).fetchall()
        directory = self._path(manga['directory'])
        paths = []
        for record in records:
            path = self._path(record['path'])
            if path.parent != directory or path.suffix.lower() != '.cbz':
                raise ValueError('漫画章节路径不匹配，已拒绝移除')
            try:
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise ValueError('漫画章节不是普通文件，已拒绝移除')
            except FileNotFoundError:
                pass
            paths.append(path)
        try:
            for path in paths:
                path.unlink(missing_ok=True)
            # Empty directory only; unrelated files are never removed.
            try:
                directory.rmdir()
            except OSError:
                pass
        except OSError as exc:
            logger.warning('library_remove_failed manga=%s actor=%s type=%s', manga_id, actor, type(exc).__name__)
            raise ValueError('部分文件未能移除，请检查磁盘权限后重试；记录仍保留') from exc
        with self.store.transaction() as db:
            db.execute('UPDATE manga SET removed_at=? WHERE id=?', (time.time(), manga_id))
            db.execute("UPDATE channel_deliveries SET status='SKIPPED',last_error='REMOVED' WHERE status='PENDING' AND task_id IN "
                       "(SELECT id FROM tasks WHERE manga_id=?)", (manga_id,))
            db.execute("UPDATE chapters SET status='MISSING',pages=NULL,size=NULL,checksum=NULL,komga_id=NULL WHERE manga_id=?", (manga_id,))
            db.execute("UPDATE manga_reports SET status='RESOLVED',handled_by=?,handled_at=? "
                       "WHERE manga_id=? AND status='PENDING'", (actor, time.time(), manga_id))
            db.execute("UPDATE report_deliveries SET status='SKIPPED' WHERE status='PENDING' AND report_id IN "
                       "(SELECT id FROM manga_reports WHERE manga_id=? AND status<>'PENDING')", (manga_id,))
        logger.info('library_removed manga=%s actor=%s', manga_id, actor)

    async def remove(self, actor, manga_id):
        try:
            self.remove_files(actor, manga_id)
        except OSError as exc:
            logger.warning('library_access_failed manga=%s type=%s', manga_id, type(exc).__name__)
            raise ValueError('无法访问漫画文件，请检查路径和权限后重试') from exc
        warning = None
        try:
            await self.komga.scan()
        except Exception as exc:
            logger.warning('library_scan_failed manga=%s type=%s', manga_id, type(exc).__name__)
            warning = '漫画文件已移除，但 Komga 扫描请求失败，请在 Komga 手动扫描漫画库'
        return {'ok': True, 'warning': warning}
