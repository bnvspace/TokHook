from __future__ import annotations

import json
import logging
import mimetypes
import re
from dataclasses import dataclass
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


LOGGER = logging.getLogger(__name__)


TIKTOK_URL_RE = re.compile(
    r"https?://(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/[^\s<>\"']+",
    re.IGNORECASE,
)

TIKTOK_POST_PATH_RE = re.compile(
    r"^/@(?P<user>[^/]+)/(?P<kind>video|photo)/(?P<id>\d+)",
    re.IGNORECASE,
)

UNIVERSAL_DATA_RE = re.compile(
    r'<script[^>]+\bid="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS


class TikTokDownloadError(RuntimeError):
    """Raised when the bot could not download media from TikTok."""


@dataclass(slots=True)
class DownloadedMedia:
    videos: list[Path]
    images: list[Path]

    @property
    def is_empty(self) -> bool:
        return not self.videos and not self.images


def extract_tiktok_url(text: str) -> str | None:
    match = TIKTOK_URL_RE.search(text or "")
    if not match:
        return None

    url = match.group(0).rstrip(".,!?)]}>\"'")
    parsed = urlparse(url)
    if not parsed.netloc.lower().endswith("tiktok.com"):
        return None
    return url


def download_tiktok_media(
    url: str,
    download_dir: Path,
    cookies_path: Path | None = None,
    camoufox_profile_path: Path | None = None,
) -> DownloadedMedia:
    resolved_url = resolve_tiktok_url(url, cookies_path)
    normalized_url = normalize_tiktok_download_url(resolved_url)

    if is_tiktok_photo_url(resolved_url):
        images = _download_photo_post_images(normalized_url, download_dir, cookies_path)
        if images:
            return DownloadedMedia(videos=[], images=images)

    ydl_options: dict[str, object] = {
        "paths": {"home": str(download_dir)},
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "windowsfilenames": True,
        "retries": 3,
        "fragment_retries": 3,
        "skip_unavailable_fragments": False,
        "format": "best[ext=mp4]/best",
        "merge_output_format": "mp4",
    }

    if cookies_path:
        ydl_options["cookiefile"] = str(cookies_path)

    info: object | None = None
    download_error: DownloadError | None = None
    try:
        with YoutubeDL(ydl_options) as ydl:
            info = ydl.extract_info(normalized_url, download=True)
    except DownloadError as exc:
        download_error = exc

    media_files = _collect_downloaded_media(download_dir)
    if not media_files:
        media_files = _collect_media_from_info(info)

    if not media_files:
        media_files = _download_photo_post_images(normalized_url, download_dir, cookies_path)

    videos = [path for path in media_files if path.suffix.lower() in VIDEO_EXTENSIONS]
    images = [path for path in media_files if path.suffix.lower() in IMAGE_EXTENSIONS]
    result = DownloadedMedia(videos=videos, images=images)

    if not result.is_empty:
        return result

    if camoufox_profile_path is None:
        camoufox_profile_path = download_dir.parent / "camoufox-profile"

    try:
        from ttd_bot.camoufox_fallback import (
            CamoufoxUnavailable,
            download_tiktok_media_with_camoufox,
        )

        browser_result = download_tiktok_media_with_camoufox(
            normalized_url,
            download_dir,
            camoufox_profile_path,
        )
    except CamoufoxUnavailable:
        browser_result = DownloadedMedia(videos=[], images=[])
    except Exception:
        LOGGER.exception("Camoufox fallback failed for %s", normalized_url)
        browser_result = DownloadedMedia(videos=[], images=[])

    if not browser_result.is_empty:
        return browser_result

    if download_error is not None:
        raise TikTokDownloadError(_friendly_download_error(download_error)) from download_error

    raise TikTokDownloadError("Не удалось найти видео или изображения по этой ссылке.")


def is_tiktok_photo_url(url: str) -> bool:
    parsed = urlparse(url)
    match = TIKTOK_POST_PATH_RE.match(parsed.path)
    return bool(match and match.group("kind").lower() == "photo")


def normalize_tiktok_download_url(url: str) -> str:
    parsed = urlparse(url)
    match = TIKTOK_POST_PATH_RE.match(parsed.path)
    if not match:
        return url

    if match.group("kind").lower() != "photo":
        return url

    normalized_path = f"/@{match.group('user')}/video/{match.group('id')}"
    return urlunparse(parsed._replace(path=normalized_path, query="", fragment=""))


def resolve_tiktok_url(url: str, cookies_path: Path | None = None) -> str:
    parsed = urlparse(url)
    if TIKTOK_POST_PATH_RE.match(parsed.path):
        return url

    try:
        with _open_url(url, cookies_path=cookies_path) as response:
            return response.geturl()
    except (HTTPError, URLError, OSError):
        return url


def _collect_downloaded_media(download_dir: Path) -> list[Path]:
    files = [
        path
        for path in download_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS
    ]
    return sorted(files, key=lambda path: (path.stat().st_mtime, path.name))


def _collect_media_from_info(info: object) -> list[Path]:
    candidates: list[Path] = []

    def visit(node: object) -> None:
        if not isinstance(node, dict):
            return

        for key in ("_filename", "filepath"):
            value = node.get(key)
            if isinstance(value, str):
                path = Path(value)
                if path.exists() and path.suffix.lower() in MEDIA_EXTENSIONS:
                    candidates.append(path)

        requested_downloads = node.get("requested_downloads")
        if isinstance(requested_downloads, list):
            for item in requested_downloads:
                if isinstance(item, dict):
                    value = item.get("filepath")
                    if isinstance(value, str):
                        path = Path(value)
                        if path.exists() and path.suffix.lower() in MEDIA_EXTENSIONS:
                            candidates.append(path)

        entries = node.get("entries")
        if isinstance(entries, Iterable) and not isinstance(entries, (str, bytes)):
            for entry in entries:
                visit(entry)

    visit(info)

    unique_paths: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique_paths.append(path)
    return unique_paths


def _download_photo_post_images(page_url: str, download_dir: Path, cookies_path: Path | None) -> list[Path]:
    try:
        webpage = _download_text(page_url, cookies_path=cookies_path)
    except (HTTPError, URLError, OSError):
        return []

    image_urls = _extract_photo_image_urls(webpage)
    if not image_urls:
        return []

    downloaded_images: list[Path] = []
    for index, image_url in enumerate(image_urls, start=1):
        try:
            response = _open_url(image_url, cookies_path=cookies_path, referer=page_url)
            with response:
                extension = _guess_image_extension(image_url, response.headers.get("Content-Type"))
                target_path = download_dir / f"image_{index:02d}{extension}"
                with target_path.open("wb") as file:
                    file.write(response.read())
        except (HTTPError, URLError, OSError):
            continue
        downloaded_images.append(target_path)

    return downloaded_images


def _extract_photo_image_urls(webpage: str) -> list[str]:
    match = UNIVERSAL_DATA_RE.search(webpage)
    if not match:
        return []

    try:
        universal_data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []

    image_urls: list[str] = []

    def visit(node: object) -> None:
        if isinstance(node, dict):
            image_post = node.get("imagePost")
            if isinstance(image_post, dict):
                for image in image_post.get("images", []):
                    if not isinstance(image, dict):
                        continue
                    url_list = image.get("imageURL", {}).get("urlList", [])
                    if not isinstance(url_list, list):
                        continue
                    first_url = next(
                        (candidate for candidate in url_list if isinstance(candidate, str) and candidate.startswith("http")),
                        None,
                    )
                    if first_url:
                        image_urls.append(first_url)

            for value in node.values():
                visit(value)
            return

        if isinstance(node, list):
            for item in node:
                visit(item)

    visit(universal_data)

    unique_urls: list[str] = []
    seen: set[str] = set()
    for image_url in image_urls:
        if image_url in seen:
            continue
        seen.add(image_url)
        unique_urls.append(image_url)
    return unique_urls


def _download_text(url: str, *, cookies_path: Path | None = None, referer: str | None = None) -> str:
    with _open_url(url, cookies_path=cookies_path, referer=referer) as response:
        return response.read().decode("utf-8", "replace")


def _open_url(url: str, *, cookies_path: Path | None = None, referer: str | None = None):
    handlers = []
    if cookies_path:
        cookie_jar = MozillaCookieJar()
        cookie_jar.load(str(cookies_path), ignore_discard=True, ignore_expires=True)
        handlers.append(HTTPCookieProcessor(cookie_jar))

    opener = build_opener(*handlers)
    headers = {
        "User-Agent": BROWSER_USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    request = Request(url, headers=headers)
    return opener.open(request, timeout=30)


def _guess_image_extension(url: str, content_type: str | None) -> str:
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return suffix

    if content_type:
        extension = mimetypes.guess_extension(content_type.split(";", maxsplit=1)[0].strip().lower()) or ""
        if extension == ".jpe":
            return ".jpg"
        if extension in IMAGE_EXTENSIONS:
            return extension

    return ".jpg"


def _friendly_download_error(exc: DownloadError) -> str:
    message = str(exc).lower()
    if "login required" in message or "cookies" in message:
        return (
            "TikTok запросил авторизацию. Добавь cookies через "
            "TIKTOK_COOKIES_PATH и попробуй снова."
        )
    if "unsupported url" in message:
        return "Похоже, ссылка не распознана как TikTok-пост."
    return (
        "Не получилось скачать медиа с этой ссылки. "
        "Проверь, что пост доступен и ссылка ведет именно на TikTok."
    )
