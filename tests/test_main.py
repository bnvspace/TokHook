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


def test_link_handler_does_not_repeat_subscription_confirmation(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        main,
        "SETTINGS",
        Settings(
            telegram_bot_token="test",
            tiktok_cookies_path=None,
            camoufox_profile_path=tmp_path,
            required_channel_username="@GlitchTMA",
        ),
    )
    monkeypatch.setattr(main, "TEMP_ROOT", tmp_path)
    monkeypatch.setattr(main, "_is_subscribed", AsyncMock(return_value=True))
    monkeypatch.setattr(
        main,
        "download_tiktok_media",
        lambda *args: main.DownloadedMedia(videos=[], images=[]),
    )
    send_media_mock = AsyncMock()
    monkeypatch.setattr(main, "_send_media", send_media_mock)

    class _NoopChatAction:
        @classmethod
        def upload_document(cls, **kwargs):
            return cls()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(main, "ChatActionSender", _NoopChatAction)

    message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=1, type="private"),
        from_user=User(id=42, is_bot=False, first_name="Test"),
        text="https://vt.tiktok.com/ZSVqrd8mD/",
    )
    progress_message = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())

    with patch.object(Message, "answer", new_callable=AsyncMock) as answer_mock:
        answer_mock.return_value = progress_message
        asyncio.run(main.link_handler(message))

    assert [call.args[0] for call in answer_mock.await_args_list] == ["Скачиваю медиа..."]
    send_media_mock.assert_awaited_once()
