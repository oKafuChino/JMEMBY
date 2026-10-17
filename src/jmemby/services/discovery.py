"""Bounded discovery, with an auditable decision for every unique candidate."""
from dataclasses import asdict
from collections import deque
import inspect
import time

from jmemby.domain.discovery import evaluate, POLICY_VERSION
from jmemby.domain.errors import JobError
from jmemby.domain.schedule_tags import parse_tags


class DiscoveryService:
    def __init__(self, provider, skip, clock=time.time):
        self.provider, self.skip, self.clock = provider, skip, clock

    async def collect(self, tag, limit, check=lambda: None):
        # A string remains a literal single tag for existing callers/snapshots.
        tags = parse_tags([tag] if isinstance(tag, str) else tag)
        multiple = len(tags) > 1
        items, seen, mode, scope = [], set(), None, None
        truncated = False
        enough = False
        searches = deque((value, 1) for value in tags)
        tag_seen = {value: set() for value in tags}
        candidates = {value: deque() for value in tags}
        tag_modes = {}
        # Round-robin pages share the original 10-search/200-candidate budget.
        # Independent source ranks cannot establish likes order across tags.
        for _ in range(10):
            tag, page = searches.popleft()
            check()
            result = await self.provider.search_tag(tag, page)
            check()
            if result.get("mode") not in {"SOURCE_LIKES_DESC", "EXACT_LIKES_DESC"}:
                raise JobError("DISCOVERY_UNSUPPORTED", "数据源未证明点赞排序能力")
            source_mode = result["mode"]
            page_scope = result.get("scope", "unspecified")
            if ((tag in tag_modes and tag_modes[tag] != source_mode)
                    or (scope is not None and scope != page_scope)):
                raise JobError("PROVIDER_PROTOCOL", "分页排序模式或时间范围发生变化")
            tag_modes[tag] = source_mode
            mode, scope = "EXACT_LIKES_DESC" if multiple else source_mode, page_scope
            fresh = [i for i in result["ids"] if i not in tag_seen[tag]]
            tag_seen[tag].update(result["ids"])
            if not fresh:
                truncated |= bool(result["ids"])
            elif not result["last"]:
                searches.append((tag, page + 1))
            if multiple:
                # Fetch bounded pages first, then distribute the detail budget
                # across tags. A large first page must not starve later tags.
                candidates[tag].extend(fresh)
                fresh = []
            for source_id in fresh:
                if source_id in seen:
                    continue
                if len(seen) >= 200:
                    truncated = True
                    break
                seen.add(source_id)
                check()
                items.append(await self._inspect(source_id, tags, mode, len(seen), check))
                if mode == "SOURCE_LIKES_DESC" and sum(i["decision"] == "ALLOW" for i in items) >= limit + 5:
                    enough = True
                    break
            if enough:
                break
            if not searches or len(seen) >= 200:
                truncated |= bool(searches)
                break
        else:
            truncated |= bool(searches)
        if multiple:
            queues = deque(candidates.values())
            while queues and len(seen) < 200:
                queue = queues.popleft()
                while queue and queue[0] in seen:
                    queue.popleft()
                if not queue:
                    continue
                source_id = queue.popleft()
                seen.add(source_id)
                items.append(await self._inspect(source_id, tags, mode, len(seen), check))
                if queue:
                    queues.append(queue)
            truncated |= any(source_id not in seen for queue in queues for source_id in queue)
        if mode == "EXACT_LIKES_DESC":
            items.sort(key=lambda i: (-(i["likes_exact"] if i["likes_exact"] is not None else -1), i["source_id"]))
            for rank, item in enumerate(items, 1):
                item["rank"] = rank
        # Retain approved reserve candidates for conflicts during submission.
        return {"items": items, "mode": mode, "scope": scope, "truncated": truncated}

    async def _inspect(self, source_id, tags, mode, rank, check):
        check()
        reason = self.skip(self.provider.source, source_id)
        if inspect.isawaitable(reason):
            reason = await reason
        check()
        item = {"source": self.provider.source, "source_id": source_id,
                "rank": rank, "likes_raw": "", "likes_exact": None,
                "checked_at": self.clock(), "policy_version": POLICY_VERSION,
                "decision": "SKIP", "reason": reason or "", "snapshot": None}
        if not reason:
            try:
                detail = await self.provider.get_discovery_detail(source_id)
                check()
                if detail.manga.source_id != source_id or detail.manga.source != self.provider.source:
                    raise JobError("PROVIDER_PROTOCOL", "详情身份不匹配")
                item.update(likes_raw=detail.likes_raw, likes_exact=detail.likes_exact,
                            checked_at=self.clock(), snapshot=asdict(detail.manga))
                item["decision"], item["reason"] = evaluate(detail, tags)
                if (mode == "EXACT_LIKES_DESC" and detail.likes_exact is None
                        and item["decision"] == "ALLOW"):
                    item.update(decision="UNKNOWN", reason="LIKES_UNKNOWN")
            except JobError as exc:
                if exc.retryable or exc.code == "DISCOVERY_UNSUPPORTED":
                    raise
                item.update(decision="UNKNOWN", reason=exc.code)
        return item
