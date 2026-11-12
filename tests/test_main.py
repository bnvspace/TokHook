import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, User

from ttd_bot import main
from ttd_bot.config import Settings


def test_custom_emoji_rejection_keeps_download_progress_message() -> None:
    rejection = TelegramBadRequest(
        method=SendMessage(chat_id=42, text="test"), message="Custom emoji are not allowed"
    )
    action = AsyncMock(side_effect=[rejection, None])

    asyncio.run(main._send_emoji_text(action, f"Скачиваю… {main.LOADING_EMOJI}"))

    assert action.await_count == 2
    action.assert_awaited_with("Скачиваю… ⏳", parse_mode=main.ParseMode.HTML)


def test_tiktok_emoji_is_added_to_text_without_changing_links() -> None:
    action = AsyncMock()
    asyncio.run(main._send_emoji_text(action, '<a href="https://t.me/TikTok">канал</a> и TikTok-пост.'))
    action.assert_awaited_once_with(
        f'<a href="https://t.me/TikTok">канал</a> и пост TikTok {main.TIKTOK_EMOJI}.',
        parse_mode=main.ParseMode.HTML,
    )


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
    callback.bot.send_message.assert_not_awaited()


def test_subscription_callback_updates_only_the_welcome_status(monkeypatch) -> None:
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
        text=main._start_text(),
        reply_markup=main._subscription_keyboard(),
    )
    callback.message = message

    async def apply_edit(text, *, reply_markup, parse_mode) -> None:
        object.__setattr__(message, "text", text)
        object.__setattr__(message, "reply_markup", reply_markup)

    with (
        patch.object(Message, "answer", new_callable=AsyncMock) as answer_mock,
        patch.object(Message, "edit_text", new_callable=AsyncMock) as edit_mock,
    ):
        edit_mock.side_effect = apply_edit
        asyncio.run(main.subscription_callback_handler(callback))
        asyncio.run(main.subscription_callback_handler(callback))

    assert edit_mock.await_count == 2
    pending_text = edit_mock.await_args_list[0].args[0]
    assert pending_text.replace(f" {main.TIKTOK_EMOJI}", "") == main._start_text("⏳ Проверяю подписку…")
    assert message.text.replace(f" {main.TIKTOK_EMOJI}", "") == main._start_text(main.SUBSCRIPTION_CONFIRMED_TEXT)
    assert main._subscription_button_state(message) is True
    assert callback.answer.await_count == 2
    answer_mock.assert_not_awaited()


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
        lambda *args, **kwargs: main.DownloadedMedia(videos=[], images=[]),
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

    answer_mock.assert_awaited_once_with(
        f"▰▱▱  Проверяю ссылку и готовлю загрузку… {main.LOADING_EMOJI}",
        parse_mode=main.ParseMode.HTML,
    )
    send_media_mock.assert_awaited_once()
