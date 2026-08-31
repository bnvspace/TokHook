from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from tempfile import TemporaryDirectory

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)
from aiogram.utils.chat_action import ChatActionSender

from ttd_bot.config import Settings
from ttd_bot.downloader import DownloadedMedia, TikTokDownloadError, download_tiktok_media, extract_tiktok_url


router = Router()
SETTINGS: Settings | None = None
TEMP_ROOT = Path(__file__).resolve().parents[1] / "tmp"

HELP_TEXT = (
    "Просто отправь ссылку на TikTok-пост. "
    "Если TikTok ограничивает доступ, можно подключить cookies через .env."
)

SUBSCRIPTION_CALLBACK = "check_subscription"
SUBSCRIBED_STATUSES = {"member", "administrator", "creator"}
SUBSCRIPTION_CONFIRMED_TEXT = (
    "Всё проверено, вы молодец! Можете пользоваться ботом 🚀"
)


class SubscriptionCheckError(RuntimeError):
    """Raised when Telegram did not allow checking channel membership."""


@router.message(Command("strat"))
@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    await message.answer(
        _start_text(),
        parse_mode=ParseMode.HTML,
        reply_markup=_subscription_keyboard(),
    )


@router.callback_query(F.data == SUBSCRIPTION_CALLBACK)
async def subscription_callback_handler(callback: CallbackQuery) -> None:
    try:
        is_subscribed = await _is_subscribed(callback.bot, callback.from_user.id)
    except SubscriptionCheckError:
        logging.exception("Could not check subscription for user %s", callback.from_user.id)
        await callback.answer(
            "Не удалось проверить подписку. Убедись, что бот добавлен администратором канала.",
            show_alert=True,
        )
        return

    if is_subscribed:
        await callback.answer("Подписка подтверждена ✅")
        await callback.bot.send_message(
            chat_id=callback.from_user.id,
            text=SUBSCRIPTION_CONFIRMED_TEXT,
        )
    else:
        await callback.answer(
            "Подпишись на канал и нажми кнопку ещё раз.",
            show_alert=True,
        )

    if isinstance(callback.message, Message):
        new_markup = _subscription_keyboard(is_subscribed)
        if _subscription_button_state(callback.message) is not is_subscribed:
            await callback.message.edit_reply_markup(reply_markup=new_markup)


@router.message(Command("help"))
async def help_handler(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(F.text)
async def link_handler(message: Message) -> None:
    url = extract_tiktok_url(message.text or "")
    if not url:
        await message.answer("Пришли ссылку на TikTok-пост сообщением.")
        return

    try:
        is_subscribed = await _is_subscribed(message.bot, message.from_user.id)
    except SubscriptionCheckError:
        logging.exception("Could not check subscription for user %s", message.from_user.id)
        await message.answer(
            "Не удалось проверить подписку. Убедись, что бот добавлен администратором канала.",
            parse_mode=ParseMode.HTML,
            reply_markup=_subscription_keyboard(),
        )
        return

    if not is_subscribed:
        await message.answer(
            _subscription_prompt(),
            parse_mode=ParseMode.HTML,
            reply_markup=_subscription_keyboard(False),
        )
        return

    await message.answer(SUBSCRIPTION_CONFIRMED_TEXT)
    progress_message = await message.answer("Скачиваю медиа...")

    try:
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="ttd_", dir=TEMP_ROOT) as temp_dir:
            download_dir = Path(temp_dir)
            async with ChatActionSender.upload_document(chat_id=message.chat.id, bot=message.bot):
                media = await asyncio.to_thread(
                    download_tiktok_media,
                    url,
                    download_dir,
                    _get_settings().tiktok_cookies_path,
                    _get_settings().camoufox_profile_path,
                )

            await progress_message.edit_text("Отправляю медиа...")
            await _send_media(message, media)
    except TikTokDownloadError as exc:
        await progress_message.edit_text(str(exc))
        return
    except Exception:
        logging.exception("Unhandled error while processing TikTok URL: %s", url)
        await progress_message.edit_text("Что-то пошло не так при обработке ссылки.")
        return

    await progress_message.delete()


async def _send_media(message: Message, media: DownloadedMedia) -> None:
    for video_path in media.videos:
        async with ChatActionSender.upload_video(chat_id=message.chat.id, bot=message.bot):
            await message.answer_video(
                FSInputFile(video_path),
                supports_streaming=True,
            )

    if len(media.images) == 1:
        image_path = media.images[0]
        async with ChatActionSender.upload_photo(chat_id=message.chat.id, bot=message.bot):
            await message.answer_photo(FSInputFile(image_path))
        return

    for chunk in _chunked(media.images, 10):
        if len(chunk) == 1:
            async with ChatActionSender.upload_photo(chat_id=message.chat.id, bot=message.bot):
                await message.answer_photo(FSInputFile(chunk[0]))
            continue

        album = [InputMediaPhoto(media=FSInputFile(path)) for path in chunk]
        async with ChatActionSender.upload_photo(chat_id=message.chat.id, bot=message.bot):
            await message.answer_media_group(album)


def _chunked(paths: list[Path], size: int) -> list[list[Path]]:
    return [paths[index:index + size] for index in range(0, len(paths), size)]


def _channel_url() -> str:
    username = _get_settings().required_channel_username.lstrip("@")
    return f"https://t.me/{username}"


def _start_text() -> str:
    return (
        "Привет! 👋\n"
        "Я помогу скачать медиа из TikTok.\n"
        "Просто отправь мне ссылку на публикацию!\n"
        "Я умею:\n"
        "• скачивать видео из TikTok;\n"
        "• извлекать изображения из фотопостов;\n"
        "• обрабатывать короткие ссылки.\n"
        "Пришли ссылку, и я начну загрузку 🚀\n\n"
        f"Но сначала подпишись на ➡️ <a href=\"{_channel_url()}\">канал</a> "
        "- без этого работать ничего не будет 🤣"
    )


def _subscription_prompt() -> str:
    return (
        f"Сначала подпишись на ➡️ <a href=\"{_channel_url()}\">канал</a> "
        "и нажми кнопку проверки подписки."
    )


def _subscription_keyboard(is_subscribed: bool | None = None) -> InlineKeyboardMarkup:
    if is_subscribed is True:
        button_text = "✅ Подписка подтверждена"
    elif is_subscribed is False:
        button_text = "❌ Подписка не найдена"
    else:
        button_text = "🔍 Проверить подписку"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=button_text, callback_data=SUBSCRIPTION_CALLBACK)]
        ]
    )


def _subscription_button_state(message: Message) -> bool | None:
    markup = message.reply_markup
    if not markup or not markup.inline_keyboard or not markup.inline_keyboard[0]:
        return None

    button_text = markup.inline_keyboard[0][0].text or ""
    if button_text.startswith("✅"):
        return True
    if button_text.startswith("❌"):
        return False
    return None


async def _is_subscribed(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(
            chat_id=_get_settings().required_channel_username,
            user_id=user_id,
        )
    except TelegramAPIError as exc:
        raise SubscriptionCheckError from exc

    if member.status in SUBSCRIBED_STATUSES:
        return True
    return member.status == "restricted" and bool(getattr(member, "is_member", False))


def _get_settings() -> Settings:
    if SETTINGS is None:
        raise RuntimeError("Настройки бота не инициализированы.")
    return SETTINGS


async def main() -> None:
    global SETTINGS

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    SETTINGS = Settings.from_env()
    bot = Bot(SETTINGS.telegram_bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
