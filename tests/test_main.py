import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
    class FakeMessage:
        def __init__(self) -> None:
            self.reply_markup = main._subscription_keyboard(False)
            self.answer = AsyncMock()
            self.edit_reply_markup = AsyncMock(side_effect=self._apply_markup)

        async def _apply_markup(self, *, reply_markup) -> None:
            self.reply_markup = reply_markup

    monkeypatch.setattr(main, "Message", FakeMessage)
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
    callback.message = FakeMessage()

    asyncio.run(main.subscription_callback_handler(callback))
    asyncio.run(main.subscription_callback_handler(callback))

    assert callback.message.edit_reply_markup.await_count == 1
    assert callback.message.answer.await_count == 2


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
