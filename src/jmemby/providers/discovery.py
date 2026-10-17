"""Optional JSON discovery capability; legacy providers fail closed."""
import re

from jmemby.db.store import decode_manga
from jmemby.domain.discovery import DiscoveryDetail
from jmemby.domain.errors import JobError


class DiscoveryProvider:
    async def search_tag(self, tag, page):
        result = await self.request({"operation": "search_tag", "tag": tag, "page": page})
        if result.get("mode") not in {"SOURCE_LIKES_DESC", "EXACT_LIKES_DESC"}:
            raise JobError("DISCOVERY_UNSUPPORTED", "数据源未证明支持点赞排序")
        ids = result.get("ids")
        if (not isinstance(ids, list) or len(ids) > 200
                or any(not isinstance(i, str) or not re.fullmatch(r"[0-9]+", i) for i in ids)
                or not isinstance(result.get("scope"), str) or not 1 <= len(result["scope"]) <= 80
                or type(result.get("last")) is not bool):
            raise JobError("PROVIDER_PROTOCOL", "搜索结果格式无效")
        return result

    async def get_discovery_detail(self, source_id):
        result = await self.request({"operation": "discovery_detail", "id": source_id})
        try:
            manga = decode_manga(result["manga"])
            if (manga.source != self.source or manga.source_id != source_id or not manga.chapters
                    or not isinstance(manga.title, str) or not manga.title or len(manga.title) > 10000):
                raise ValueError()
            fields = [result.get("tags"), result.get("markers")]
            complete = result.get("complete") is True
            if any(not isinstance(f, list) or len(f) > 1000 or any(
                    not isinstance(t, str) or len(t) > 1000 for t in f) for f in fields):
                complete = False
                fields = [[], []]
            likes = result.get("likes_exact")
            if likes is not None and (type(likes) is not int or likes < 0):
                raise ValueError()
            flag = result.get("ai_flag", False)
            if type(flag) is not bool:
                complete = False
            return DiscoveryDetail(manga, tuple(fields[0]), tuple(fields[1]), complete,
                                   flag is True, str(result.get("likes_raw", ""))[:80], likes)
        except (KeyError, TypeError, ValueError) as exc:
            raise JobError("PROVIDER_PROTOCOL", "严格详情响应无效") from exc
