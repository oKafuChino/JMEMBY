"""Strict input parsing. Only configured source domains are accepted."""
import re
from urllib.parse import urlparse


def parse_reference(reference: str, domains: tuple[str, ...] = ()) -> str:
    value = reference.strip()
    if re.fullmatch(r"[1-9][0-9]{0,15}", value):
        return value
    parsed = urlparse(value)
    if (parsed.scheme != "https" or parsed.hostname not in domains
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError("只支持数字漫画 ID 或配置白名单中的 HTTPS 链接")
    match = re.fullmatch(r"/album/([1-9][0-9]{0,15})/?", parsed.path)
    if not match:
        raise ValueError("无法从链接解析漫画 ID")
    return match.group(1)
