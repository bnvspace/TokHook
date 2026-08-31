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
    camoufox_profile_path: Path
    required_channel_username: str

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

        raw_profile_path = os.getenv("CAMOUFOX_PROFILE_PATH", "").strip()
        camoufox_profile_path = (
            Path(raw_profile_path).expanduser()
            if raw_profile_path
            else Path(__file__).resolve().parents[1] / "tmp" / "camoufox-profile"
        )

        required_channel_username = os.getenv(
            "REQUIRED_CHANNEL_USERNAME",
            "@GlitchTMA",
        ).strip()
        if not required_channel_username:
            raise RuntimeError("Переменная REQUIRED_CHANNEL_USERNAME не задана.")
        if (
            not required_channel_username.startswith("@")
            and not required_channel_username.startswith("-")
        ):
            required_channel_username = f"@{required_channel_username}"

        return cls(
            telegram_bot_token=telegram_bot_token,
            tiktok_cookies_path=tiktok_cookies_path,
            camoufox_profile_path=camoufox_profile_path,
            required_channel_username=required_channel_username,
        )
