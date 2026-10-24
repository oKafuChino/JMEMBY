from jmemby.db.store import decode_manga
from jmemby.providers.reference import parse_reference


class TaskService:
    def __init__(self, store, provider, domains=(), users=None):
        self.store, self.provider, self.domains = store, provider, domains
        self.users = users

    def authorize(self, user_id):
        if self.users:
            self.users.require_member(user_id)

    async def preview(self, reference, user_id, chat_id):
        self.authorize(user_id)
        source_id = parse_reference(reference, self.domains)
        self.store.check_submission(getattr(self.provider, "source", "jmcomic"), source_id, user_id)
        manga = await self.provider.get_manga(source_id)
        self.authorize(user_id)  # Account may have been blocked during the network request.
        return manga, self.store.confirmation(manga, user_id, chat_id)

    async def download(self, reference, user_id, chat_id):
        """Submit /jm immediately; recheck membership and duplicates after I/O."""
        self.authorize(user_id)
        source_id = parse_reference(reference, self.domains)
        self.store.check_submission(getattr(self.provider, 'source', 'jmcomic'), source_id, user_id)
        manga = await self.provider.get_manga(source_id)
        self.authorize(user_id)
        return self.store.submit(manga, user_id, chat_id)

    def confirm(self, token, user_id, chat_id=None, *, notify_telegram=True):
        self.authorize(user_id)
        return self.store.confirm(token, user_id, chat_id, notify_telegram=notify_telegram)

    def get(self, task_id, user_id):
        self.authorize(user_id)
        task = self.store.get(task_id)
        if not task or task["user_id"] != user_id:
            raise ValueError("任务不存在")
        return task

    def recent_downloads(self, user_id, query=""):
        if not self.users:
            raise ValueError("此功能仅管理员可用")
        self.users.require_admin(user_id)
        return self.store.recent_downloads(query)

    def queue_page(self, actor, page=1, queue='all'):
        if not self.users:
            raise ValueError('此功能仅管理员可用')
        self.users.require_admin(actor)
        if type(page) is not int or not 1 <= page <= 1000000:
            raise ValueError('页码无效')
        if queue not in ('all', 'normal', 'white'):
            raise ValueError('队列无效')
        where, params = ('', ()) if queue == 'all' else (' WHERE t.priority=?', (int(queue == 'white'),))
        rows = self.store.connection.execute(
            'SELECT t.id,m.title,m.source_id,t.user_id,t.status,t.priority,t.progress,t.created_at,'
            't.assigned_node_id,t.assigned_node_label FROM tasks t JOIN manga m ON m.id=t.manga_id' + where +
            ' ORDER BY t.created_at DESC,t.id DESC LIMIT 20 OFFSET ?', (*params, (page-1)*20)).fetchall()
        coordinator = getattr(self, 'download_coordinator', None)
        current = {a['task']['task_id']: {'id': a['id'], 'label': a['label']}
                   for a in coordinator.snapshot()['agents'] if a.get('task')} if coordinator else {}
        result = []
        for row in rows:
            task = dict(row)
            node_id, label = task.pop('assigned_node_id'), task.pop('assigned_node_label')
            task['node'] = current.get(task['id']) or ({'id': node_id, 'label': label or node_id} if node_id else None)
            task['node_current'] = task['id'] in current
            result.append(task)
        return result

    def download_history(self, reference, user_id, page=1):
        if not self.users:
            raise ValueError("此功能仅管理员可用")
        self.users.require_admin(user_id)
        source_id = parse_reference(reference, self.domains)
        return self.store.download_history(getattr(self.provider, "source", "jmcomic"), source_id, page)

    def retry(self, task_id, user_id, chat_id=None):
        task = self.get(task_id, user_id)
        if task["status"] != "FAILED":
            raise ValueError("只能重试失败的任务")
        return self.store.submit(decode_manga(task["snapshot"]), user_id,
                                 task["chat_id"] if chat_id is None else chat_id, task_id,
                                 notify_telegram=bool(task['notify_telegram']))
