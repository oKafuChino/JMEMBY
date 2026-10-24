from dataclasses import dataclass, field
import json
import os
import re
from urllib.parse import urlsplit


def integer(name, default, minimum, maximum):
    value = int(os.getenv(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def valid_token(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{32,256}", value)


@dataclass(frozen=True)
class MasterSettings:
    enabled: bool = False
    tokens: dict = field(default_factory=dict, repr=False)
    host: str = "127.0.0.1"
    port: int = 8081
    lease_seconds: int = 90
    job_timeout: int = 1800

    @classmethod
    def from_env(cls):
        if os.getenv("DISTRIBUTED_ENABLED", "false").lower() not in {"true", "1", "yes"}:
            return cls()
        try:
            tokens = json.loads(os.getenv("DOWNLOAD_AGENT_TOKENS", "{}"))
        except ValueError:
            raise ValueError("DOWNLOAD_AGENT_TOKENS must be a JSON object") from None
        if (not isinstance(tokens, dict) or not 1 <= len(tokens) <= 32
                or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", key) or not valid_token(value)
                       for key, value in tokens.items())
                or len(set(tokens.values())) != len(tokens)):
            raise ValueError("DOWNLOAD_AGENT_TOKENS requires 1–32 unique agent IDs and unique random tokens (32+ characters)")
        if os.getenv("PROVIDER_COMMAND"):
            raise ValueError("Distributed downloads currently require the bundled JM adapter")
        return cls(True, tokens, os.getenv("DISTRIBUTED_HOST", "127.0.0.1"),
                   integer("DISTRIBUTED_PORT", 8081, 1, 65535),
                   integer("DISTRIBUTED_LEASE_SECONDS", 90, 30, 600),
                   integer("DISTRIBUTED_JOB_TIMEOUT", 1800, 60, 86400))


@dataclass(frozen=True)
class AgentSettings:
    master_url: str
    agent_id: str
    token: str = field(repr=False)
    concurrency: int = 2
    work_dir: str = "/agent-data"
    operation_timeout: int = 300
    max_page_bytes: int = 30 * 1024 * 1024
    max_pixels: int = 50_000_000
    min_free_space_mb: int = 512

    @classmethod
    def from_env(cls):
        origin = os.getenv("AGENT_MASTER_URL", "").rstrip("/")
        url = urlsplit(origin)
        if (url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password
                or url.path or url.query or url.fragment
                or (url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"})):
            raise ValueError("AGENT_MASTER_URL requires an HTTPS origin (localhost HTTP allowed for SSH tunnels)")
        agent_id, token = os.getenv("AGENT_ID", ""), os.getenv("AGENT_TOKEN", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", agent_id) or not valid_token(token):
            raise ValueError("Configure AGENT_ID and a random AGENT_TOKEN of at least 32 characters")
        return cls(origin, agent_id, token,
                   integer("AGENT_CONCURRENCY", 2, 1, 16), os.getenv("AGENT_WORK_DIR", "/agent-data"),
                   integer("AGENT_OPERATION_TIMEOUT", 300, 10, 3600),
                   integer("AGENT_MAX_PAGE_BYTES", 30 * 1024 * 1024, 1024, 256 * 1024 * 1024),
                   integer("AGENT_MAX_PIXELS", 50_000_000, 1, 100_000_000),
                   integer("AGENT_MIN_FREE_SPACE_MB", 512, 0, 1048576))
