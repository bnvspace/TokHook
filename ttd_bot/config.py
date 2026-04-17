from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


@dataclass(slots=True)
class Settings:
    telegram_bot_token: str
    tiktok_cookies_path: Path | None

    @classmethod
    def from_env(cls) -> "Settings":
        telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not telegram_bot_token:
            raise RuntimeError("Переменная окружения TELEGRAM_BOT_TOKEN не задана.")

        raw_cookies_path = os.getenv("TIKTOK_COOKIES_PATH", "").strip()
        tiktok_cookies_path = Path(raw_cookies_path).expanduser() if raw_cookies_path else None
        if tiktok_cookies_path and not tiktok_cookies_path.exists():
            raise RuntimeError(
                f"Файл cookies не найден: {tiktok_cookies_path}"
            )

        return cls(
            telegram_bot_token=telegram_bot_token,
            tiktok_cookies_path=tiktok_cookies_path,
        )
