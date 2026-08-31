import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.types import Chat, Message, User

from ttd_bot import main
from ttd_bot.config import Settings


def _callback(status: str) -> SimpleNamespace:
    bot = SimpleNamespace(
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status=status)),
        send_message=AsyncMock(),
    )
    return SimpleNamespace(
        bot=bot,
        from_user=SimpleNamespace(id=42),
        message=None,
        answer=AsyncMock(),
    )


def test_subscription_callback_checks_and_responds_on_every_press(monkeypatch) -> None:
    monkeypatch.setattr(
        main,
        "SETTINGS",
        Settings(
            telegram_bot_token="test",
            tiktok_cookies_path=None,
            camoufox_profile_path=main.TEMP_ROOT,
            required_channel_username="@GlitchTMA",
        ),
    )
    callback = _callback("member")

    asyncio.run(main.subscription_callback_handler(callback))
    asyncio.run(main.subscription_callback_handler(callback))

    assert callback.bot.get_chat_member.await_count == 2
    assert callback.answer.await_count == 2
    assert callback.bot.send_message.await_count == 2


def test_subscription_callback_updates_button_once_but_confirms_every_press(monkeypatch) -> None:
    monkeypatch.setattr(
        main,
        "SETTINGS",
        Settings(
            telegram_bot_token="test",
            tiktok_cookies_path=None,
            camoufox_profile_path=main.TEMP_ROOT,
            required_channel_username="@GlitchTMA",
        ),
    )
    callback = _callback("member")
    message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=1, type="private"),
        from_user=User(id=42, is_bot=False, first_name="Test"),
        reply_markup=main._subscription_keyboard(False),
    )
    callback.message = message

    async def apply_markup(*, reply_markup) -> None:
        object.__setattr__(message, "reply_markup", reply_markup)

    with (
        patch.object(Message, "answer", new_callable=AsyncMock) as answer_mock,
        patch.object(Message, "edit_reply_markup", new_callable=AsyncMock) as edit_mock,
    ):
        edit_mock.side_effect = apply_markup
        asyncio.run(main.subscription_callback_handler(callback))
        asyncio.run(main.subscription_callback_handler(callback))

    assert edit_mock.await_count == 1
    assert answer_mock.await_count == 2


def test_subscription_callback_answers_negative_on_every_press(monkeypatch) -> None:
    monkeypatch.setattr(
        main,
        "SETTINGS",
        Settings(
            telegram_bot_token="test",
            tiktok_cookies_path=None,
            camoufox_profile_path=main.TEMP_ROOT,
            required_channel_username="@GlitchTMA",
        ),
    )
    callback = _callback("left")

    asyncio.run(main.subscription_callback_handler(callback))
    asyncio.run(main.subscription_callback_handler(callback))

    assert callback.bot.get_chat_member.await_count == 2
    assert callback.answer.await_count == 2
    assert callback.answer.await_args_list[0].kwargs["show_alert"] is True
    assert callback.bot.send_message.await_count == 0
