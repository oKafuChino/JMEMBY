"""Bounded discovery, with an auditable decision for every unique candidate."""
from dataclasses import asdict
import inspect
import time

from jmemby.domain.discovery import evaluate, POLICY_VERSION
from jmemby.domain.errors import JobError


class DiscoveryService:
    def __init__(self, provider, skip, clock=time.time):
        self.provider, self.skip, self.clock = provider, skip, clock

    async def collect(self, tag, limit, check=lambda: None):
        items, seen, mode, scope = [], set(), None, None
        truncated = False
        enough = False
        # Collect the bounded range before exact sorting; source rank is never
        # replaced with the completion order of detail requests.
        for page in range(1, 11):
            check()
            result = await self.provider.search_tag(tag, page)
            check()
            if result.get("mode") not in {"SOURCE_LIKES_DESC", "EXACT_LIKES_DESC"}:
                raise JobError("DISCOVERY_UNSUPPORTED", "数据源未证明点赞排序能力")
            if mode is not None and (mode != result["mode"] or scope != result.get("scope")):
                raise JobError("PROVIDER_PROTOCOL", "分页排序模式或时间范围发生变化")
            mode, scope = result["mode"], result.get("scope", "unspecified")
            fresh = [i for i in result["ids"] if i not in seen]
            if not fresh:
                truncated = bool(result["ids"])
                break
            for source_id in fresh:
                if source_id in seen:
                    continue
                if len(seen) >= 200:
                    truncated = True
                    break
                seen.add(source_id)
                check()
                reason = self.skip(self.provider.source, source_id)
                if inspect.isawaitable(reason):
                    reason = await reason
                check()
                item = {"source": self.provider.source, "source_id": source_id,
                        "rank": len(seen), "likes_raw": "", "likes_exact": None,
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
                        item["decision"], item["reason"] = evaluate(detail, tag)
                        if mode == "EXACT_LIKES_DESC" and detail.likes_exact is None:
                            item.update(decision="UNKNOWN", reason="LIKES_UNKNOWN")
                    except JobError as exc:
                        if exc.retryable or exc.code == "DISCOVERY_UNSUPPORTED":
                            raise
                        item.update(decision="UNKNOWN", reason=exc.code)
                items.append(item)
                if mode == "SOURCE_LIKES_DESC" and sum(i["decision"] == "ALLOW" for i in items) >= limit + 5:
                    enough = True
                    break
            if enough:
                break
            if result["last"] or len(seen) >= 200:
                truncated = not result["last"]
                break
            if page == 10:
                truncated = True
        if mode == "EXACT_LIKES_DESC":
            items.sort(key=lambda i: (-(i["likes_exact"] if i["likes_exact"] is not None else -1), i["source_id"]))
            for rank, item in enumerate(items, 1):
                item["rank"] = rank
        # Retain approved reserve candidates for conflicts during submission.
        return {"items": items, "mode": mode, "scope": scope, "truncated": truncated}
