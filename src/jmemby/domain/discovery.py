"""Strict discovery metadata, separate from the permissive display model."""
from dataclasses import dataclass
import re
import unicodedata

from jmemby.domain.models import Manga

POLICY_VERSION = 1


def normalize(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def tag_value(value):
    value = normalize(value)
    return re.sub(r"[\s_\-‐‑–—]+", "", value)


AI_MARKERS = {"aigenerate", "aigenerated", "ai生成", "ai生成作品", "ai生成物"}
AI_TITLE = re.compile(r"(?<![a-z0-9])ai[\s_\-‐‑–—]*(?:generated?|生成(?:作品|物)?)(?![a-z0-9])")


@dataclass(frozen=True)
class DiscoveryDetail:
    manga: Manga
    tags: tuple[str, ...]
    markers: tuple[str, ...]
    complete: bool
    ai_flag: bool = False
    likes_raw: str = ""
    likes_exact: int | None = None


def evaluate(detail, target):
    if detail.ai_flag:
        return "BLOCK", "AI_FLAG"
    if any(tag_value(t) in AI_MARKERS for t in (*detail.tags, *detail.markers)):
        return "BLOCK", "AI_TAG"
    if AI_TITLE.search(normalize(detail.manga.title)):
        return "BLOCK", "AI_TITLE"
    if not detail.complete:
        return "UNKNOWN", "METADATA_INCOMPLETE"
    targets = [target] if isinstance(target, str) else target
    if not {normalize(t) for t in targets}.intersection(normalize(t) for t in detail.tags):
        return "UNKNOWN", "TARGET_TAG_UNCONFIRMED"
    return "ALLOW", "MATCH"
