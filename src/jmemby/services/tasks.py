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
        manga = await self.provider.get_manga(source_id)
        self.authorize(user_id)  # Account may have been blocked during the network request.
        return manga, self.store.confirmation(manga, user_id, chat_id)

    def confirm(self, token, user_id):
        self.authorize(user_id)
        return self.store.confirm(token, user_id)

    def get(self, task_id, user_id):
        self.authorize(user_id)
        task = self.store.get(task_id)
        if not task or task["user_id"] != user_id:
            raise ValueError("任务不存在")
        return task

    def retry(self, task_id, user_id):
        task = self.get(task_id, user_id)
        if task["status"] != "FAILED":
            raise ValueError("只能重试失败的任务")
        return self.store.submit(decode_manga(task["snapshot"]), user_id, task["chat_id"], task_id)
