"""Canonical multi-tag input, with legacy single-tag snapshot compatibility."""
import json
import re

from jmemby.domain.discovery import normalize

MAX_TAGS = 10


def parse_tags(value):
    """Commas/newlines separate tags; spaces remain part of a tag."""
    values = re.split(r"[,，、\r\n]+", value) if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)):
        raise ValueError("标签须为文本或标签列表")
    tags, seen = [], set()
    for raw in values:
        if not isinstance(raw, str):
            raise ValueError("每个标签须为文本")
        tag = " ".join(raw.split())
        if not tag:
            continue
        if len(tag) > 100 or any(ord(c) < 32 for c in tag):
            raise ValueError("每个标签须为 1–100 字符")
        key = normalize(tag)
        if key not in seen:
            seen.add(key)
            tags.append(tag)
    if not 1 <= len(tags) <= MAX_TAGS:
        raise ValueError(f"请输入 1–{MAX_TAGS} 个标签，用逗号或换行分隔")
    return tags


def schedule_tags(config):
    """Read schedules and frozen runs without reinterpreting old literal tags."""
    spec = config.get("spec", {})
    if isinstance(spec, str):
        spec = json.loads(spec)
    return parse_tags(spec["tags"]) if "tags" in spec else parse_tags([config["tag"]])
