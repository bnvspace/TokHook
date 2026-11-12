from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
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
_DEFAULT_TEMP_ROOT = Path(__file__).resolve().parents[1] / "tmp"
TEMP_ROOT = Path(os.getenv("TTD_TEMP_ROOT", str(_DEFAULT_TEMP_ROOT))).expanduser()
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(2)
ACTIVE_USERS: set[int] = set()
ACTIVE_USERS_LOCK = asyncio.Lock()
DOWNLOAD_SLOT_WAIT_SECONDS = 0.2

HELP_TEXT = (
    "Просто отправь ссылку на TikTok-пост. "
    "Если TikTok ограничивает доступ, можно подключить cookies через .env."
)

SUBSCRIPTION_CALLBACK = "check_subscription"
SUBSCRIBED_STATUSES = {"member", "administrator", "creator"}
ROCKET_EMOJI = '<tg-emoji emoji-id="5188481279963715781">🚀</tg-emoji>'
WHALE_EMOJI = '<tg-emoji emoji-id="5400362079783770689">🐳</tg-emoji>'
LOADING_EMOJI = '<tg-emoji emoji-id="5213452215527677338">⏳</tg-emoji>'
WAVE_EMOJI = '<tg-emoji emoji-id="5368488749523029379">👋</tg-emoji>'
ARROW_EMOJI = '<tg-emoji emoji-id="5933496039523752723">➡️</tg-emoji>'
SUBSCRIPTION_CONFIRMED_TEXT = (
    f"Всё проверено, вы молодец! Можете пользоваться ботом {ROCKET_EMOJI}"
)


class SubscriptionCheckError(RuntimeError):
    """Raised when Telegram did not allow checking channel membership."""


async def _send_emoji_text(action, text: str, **kwargs) -> Message:
    try:
        return await action(text, parse_mode=ParseMode.HTML, **kwargs)
    except TelegramBadRequest as exc:
        fallback = (
            text.replace(ROCKET_EMOJI, "🚀")
            .replace(WHALE_EMOJI, "🐳")
            .replace(LOADING_EMOJI, "⏳")
            .replace(WAVE_EMOJI, "👋")
            .replace(ARROW_EMOJI, "➡️")
        )
        if fallback == text or "message is not modified" in exc.message.lower():
            raise
        return await action(fallback, parse_mode=ParseMode.HTML, **kwargs)


@router.message(Command("strat"))
@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    await _send_emoji_text(
        message.answer,
        _start_text(),
        reply_markup=_subscription_keyboard(),
    )


@router.callback_query(F.data == SUBSCRIPTION_CALLBACK)
async def subscription_callback_handler(callback: CallbackQuery) -> None:
    message = callback.message if isinstance(callback.message, Message) else None
    is_welcome = message is not None and (message.text or "").startswith("Привет! ")
    previous_state = _subscription_button_state(message) if message else None
    checking = is_welcome and previous_state is None

    def render(status: str | None = None) -> str:
        return _start_text(status) if is_welcome else status or _subscription_prompt()

    async def edit_status(text: str, state: bool | None) -> None:
        if message is None:
            return
        try:
            async with asyncio.timeout(3):
                await _send_emoji_text(
                    message.edit_text, text, reply_markup=_subscription_keyboard(state)
                )
        except (TelegramAPIError, TimeoutError):
            logging.debug("Could not update subscription message", exc_info=True)

    if checking:
        await edit_status(render("⏳ Проверяю подписку…"), None)
    try:
        is_subscribed = await _is_subscribed(callback.bot, callback.from_user.id)
    except SubscriptionCheckError:
        logging.exception("Could not check subscription for user %s", callback.from_user.id)
        if checking:
            await edit_status(render(), None)
        await callback.answer(
            "Не удалось проверить подписку. Убедись, что бот добавлен администратором канала.",
            show_alert=True,
        )
        return

    if message and (checking or previous_state is not is_subscribed):
        await edit_status(render(SUBSCRIPTION_CONFIRMED_TEXT if is_subscribed else None), is_subscribed)

    if is_subscribed:
        await callback.answer("Подписка подтверждена ✅")
    else:
        await callback.answer(
            "Подпишись на канал и нажми кнопку ещё раз.",
            show_alert=True,
        )

@router.message(Command("help"))
async def help_handler(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(F.text)
async def link_handler(message: Message) -> None:
    url = extract_tiktok_url(message.text or "")
    if not url:
        await message.answer("Пришли ссылку на TikTok-пост сообщением.")
        return

    if message.from_user is None:
        return
    user_id = message.from_user.id
    async with ACTIVE_USERS_LOCK:
        already_active = user_id in ACTIVE_USERS
        if not already_active:
            ACTIVE_USERS.add(user_id)
    if already_active:
        await message.answer("Предыдущая ссылка ещё обрабатывается. Подожди немного.")
        return

    progress_message: Message | None = None

    try:
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
            await _send_emoji_text(
                message.answer,
                _subscription_prompt(),
                reply_markup=_subscription_keyboard(False),
            )
            return

        progress_message = await _send_emoji_text(
            message.answer,
            f"{LOADING_EMOJI} ▰▱▱  Проверяю ссылку и готовлю загрузку…",
        )
        try:
            await asyncio.wait_for(
                DOWNLOAD_SEMAPHORE.acquire(),
                timeout=DOWNLOAD_SLOT_WAIT_SECONDS,
            )
        except asyncio.TimeoutError:
            await progress_message.edit_text(
                "Сейчас бот занят другими загрузками. Попробуй ещё раз через несколько секунд."
            )
            return

        try:
            TEMP_ROOT.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(prefix="ttd_", dir=TEMP_ROOT) as temp_dir:
                download_dir = Path(temp_dir)
                async with ChatActionSender.upload_document(chat_id=message.chat.id, bot=message.bot):
                    media = await _download_with_progress(
                        progress_message,
                        download_tiktok_media,
                        url,
                        download_dir,
                        _get_settings().tiktok_cookies_path,
                        _get_settings().camoufox_profile_path,
                    )

                await _edit_progress(progress_message, "▰▰▰  Медиа скачано. Отправляю в Telegram…")
                await _send_media(message, media)
        finally:
            DOWNLOAD_SEMAPHORE.release()
    except TikTokDownloadError as exc:
        if progress_message is not None:
            await progress_message.edit_text(str(exc))
        else:
            await message.answer(str(exc))
        return
    except Exception:
        logging.exception("Unhandled error while processing TikTok URL: %s", url)
        if progress_message is not None:
            await progress_message.edit_text("Что-то пошло не так при обработке ссылки.")
        else:
            await message.answer("Что-то пошло не так при обработке ссылки.")
        return
    finally:
        ACTIVE_USERS.discard(user_id)

    if progress_message is not None:
        await progress_message.delete()


async def _edit_progress(message: Message, text: str) -> None:
    try:
        async with asyncio.timeout(5):
            await _send_emoji_text(message.edit_text, f"{LOADING_EMOJI} {text}")
    except (TelegramAPIError, TimeoutError):
        logging.debug("Could not edit download progress", exc_info=True)


async def _download_with_progress(message: Message, download, *args) -> DownloadedMedia:
    loop = asyncio.get_running_loop()
    started = loop.time()
    stage = "Открываю ссылку TikTok"

    def report_stage(value: str) -> None:
        nonlocal stage
        stage = value

    task = asyncio.create_task(asyncio.to_thread(download, *args, on_stage=report_stage))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=4)
            if done:
                return task.result()
            elapsed = int(loop.time() - started)
            await _edit_progress(message, f"▰▰▱  {stage}… {elapsed} сек.")
    finally:
        if not task.done():
            task.cancel()


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


def _start_text(status: str | None = None) -> str:
    welcome = (
        f"Привет! {WAVE_EMOJI}\n"
        "Я помогу скачать медиа из TikTok.\n"
        "Просто отправь мне ссылку на публикацию!\n"
        "Я умею:\n"
        "• скачивать видео из TikTok;\n"
        "• извлекать изображения из фотопостов;\n"
        "• обрабатывать короткие ссылки.\n"
        f"Пришли ссылку, и я начну загрузку {ROCKET_EMOJI}\n\n"
    )
    return welcome + (status or (
        f"Но сначала подпишись на {ARROW_EMOJI} <a href=\"{_channel_url()}\">канал</a> "
        f"- без этого работать ничего не будет {WHALE_EMOJI}"
    ))


def _subscription_prompt() -> str:
    return (
        f"Сначала подпишись на {ARROW_EMOJI} <a href=\"{_channel_url()}\">канал</a> "
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
