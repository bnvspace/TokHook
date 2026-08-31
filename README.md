# Telegram bot для TikTok-ссылок

Бот принимает ссылку на TikTok-пост и в ответ отправляет медиа из этой ссылки:

- видео, если это обычный TikTok-видео-пост;
- изображения, если это фотопост.

## Что понадобится

- Python 3.10+
- токен Telegram-бота от `@BotFather`
- желательно свежий `yt-dlp` (в `requirements.txt` уже указана актуальная версия)
- для некоторых TikTok-ссылок могут понадобиться cookies из браузера

## Установка

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Заполни `.env`:

```env
TELEGRAM_BOT_TOKEN=твой_токен_бота
TIKTOK_COOKIES_PATH=
REQUIRED_CHANNEL_USERNAME=@GlitchTMA
```

Если TikTok отдает ошибку доступа, укажи путь к cookies-файлу в формате Netscape:

```env
TIKTOK_COOKIES_PATH=C:\path\to\tiktok_cookies.txt
```

## Запуск

```powershell
python -m ttd_bot.main
```

## Как бот работает

1. Пользователь запускает бота и проверяет подписку на канал.
2. Пользователь отправляет ссылку на TikTok.
3. Бот скачивает медиа через `yt-dlp` во временную папку.
4. Если найдено видео, бот отправляет видео.
5. Если найдены изображения, бот отправляет их как фото.

## Важные замечания

- TikTok иногда режет доступ по региону, cookies или антибот-защите.
- Для некоторых сложных случаев `yt-dlp` может требовать обновления.
- Фотопосты TikTok иногда приходят как набор изображений, иногда как видео. Бот отправляет то, что удалось реально скачать.
- Для проверки подписки бот должен быть добавлен администратором канала из `REQUIRED_CHANNEL_USERNAME`.

## Быстрая проверка

```powershell
pytest
python -m ttd_bot.main
```
