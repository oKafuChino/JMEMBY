from __future__ import annotations

from dataclasses import dataclass, field
import os
import json
import sys


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    return default if value in (None, "") else int(value)


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_bot_token: str = field(repr=False)
    allowed_user_ids: frozenset[int]
    database_url: str
    storage_root: str
    komga_base_url: str
    komga_public_url: str
    komga_library_id: str
    komga_library_root: str
    download_concurrency: int = 2
    retry_max_attempts: int = 3
    min_free_space_gb: int = 5
    resume_free_space_gb: int = 10
    provider_command: str = ""
    komga_username: str = ""
    komga_password: str = field(default="", repr=False)
    provider_domains: tuple[str, ...] = ()
    operation_timeout: int = 300
    max_page_bytes: int = 30 * 1024 * 1024
    max_pixels: int = 50_000_000
    max_chapter_pages: int = 2000
    max_task_bytes: int = 10 * 1024**3

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        users = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").strip()
        if not token or token == "replace-me":
            raise ValueError("TELEGRAM_BOT_TOKEN must be configured")
        if not users:
            raise ValueError("TELEGRAM_ALLOWED_USER_IDS must contain at least one ID")
        try:
            allowed = frozenset(int(item.strip()) for item in users.split(",") if item.strip())
        except ValueError as exc:
            raise ValueError("TELEGRAM_ALLOWED_USER_IDS must contain numeric IDs") from exc
        if not allowed or any(user <= 0 for user in allowed):
            raise ValueError("TELEGRAM_ALLOWED_USER_IDS must contain at least one ID")
        minimum, resume = _int("MIN_FREE_SPACE_GB", 5), _int("RESUME_FREE_SPACE_GB", 10)
        if minimum < 0 or resume <= minimum:
            raise ValueError("RESUME_FREE_SPACE_GB must be greater than MIN_FREE_SPACE_GB")
        return cls(
            telegram_bot_token=token,
            allowed_user_ids=allowed,
            database_url=os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/app/jmemby.db"),
            storage_root=os.getenv("STORAGE_ROOT", "/storage"),
            komga_base_url=os.getenv("KOMGA_BASE_URL", "http://komga:25600"),
            komga_public_url=os.getenv("KOMGA_PUBLIC_URL", ""),
            komga_library_id=os.getenv("KOMGA_LIBRARY_ID", ""),
            komga_library_root=os.getenv("KOMGA_LIBRARY_ROOT", "/data"),
            download_concurrency=_int("DOWNLOAD_CONCURRENCY", 2),
            retry_max_attempts=_int("RETRY_MAX_ATTEMPTS", 3),
            min_free_space_gb=minimum,
            resume_free_space_gb=resume,
            provider_command=os.getenv("PROVIDER_COMMAND") or json.dumps([sys.executable, "-m", "jmemby.providers.jm_adapter"]),
            komga_username=os.getenv("KOMGA_USERNAME", ""),
            komga_password=os.getenv("KOMGA_PASSWORD", ""),
            provider_domains=tuple(x.strip().lower() for x in os.getenv("PROVIDER_DOMAINS", "").split(",") if x.strip()),
            operation_timeout=_int("OPERATION_TIMEOUT", 300),
            max_page_bytes=_int("MAX_PAGE_BYTES", 30 * 1024 * 1024),
            max_pixels=_int("MAX_PIXELS", 50_000_000),
            max_chapter_pages=_int("MAX_CHAPTER_PAGES", 2000),
            max_task_bytes=_int("MAX_TASK_BYTES", 10 * 1024**3),
        )

    def __post_init__(self):
        for name in ("download_concurrency", "retry_max_attempts", "operation_timeout", "max_page_bytes", "max_pixels", "max_chapter_pages", "max_task_bytes"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
