from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from tempfile import TemporaryDirectory

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, InputMediaPhoto, Message
from aiogram.utils.chat_action import ChatActionSender

from ttd_bot.config import Settings
from ttd_bot.downloader import DownloadedMedia, TikTokDownloadError, download_tiktok_media, extract_tiktok_url


router = Router()
SETTINGS: Settings | None = None

START_TEXT = (
    "Привет. Пришли ссылку на TikTok, и я верну тебе видео "
    "или изображения из этого поста."
)

HELP_TEXT = (
    "Просто отправь ссылку на TikTok-пост. "
    "Если TikTok ограничивает доступ, можно подключить cookies через .env."
)


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    await message.answer(START_TEXT)


@router.message(Command("help"))
async def help_handler(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(F.text)
async def link_handler(message: Message) -> None:
    url = extract_tiktok_url(message.text or "")
    if not url:
        await message.answer("Пришли ссылку на TikTok-пост сообщением.")
        return

    progress_message = await message.answer("Скачиваю медиа...")

    try:
        with TemporaryDirectory(prefix="ttd_") as temp_dir:
            download_dir = Path(temp_dir)
            async with ChatActionSender.upload_document(chat_id=message.chat.id, bot=message.bot):
                media = await asyncio.to_thread(
                    download_tiktok_media,
                    url,
                    download_dir,
                    _get_settings().tiktok_cookies_path,
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
