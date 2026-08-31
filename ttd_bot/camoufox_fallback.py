from __future__ import annotations

import json
import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse

from ttd_bot.downloader import (
    BROWSER_USER_AGENT,
    DownloadedMedia,
    IMAGE_EXTENSIONS,
    UNIVERSAL_DATA_RE,
    VIDEO_EXTENSIONS,
)


LOGGER = logging.getLogger(__name__)
MAX_MEDIA_BYTES = 128 * 1024 * 1024
_CAMOUFOX_LOCK = Lock()


class CamoufoxUnavailable(RuntimeError):
    """Raised when the optional browser fallback is not installed."""


@dataclass(slots=True)
class _MediaUrls:
    videos: list[str]
    images: list[str]


def download_tiktok_media_with_camoufox(
    page_url: str,
    download_dir: Path,
    profile_dir: Path,
) -> DownloadedMedia:
    """Open a TikTok page in Camoufox and download URLs exposed by its JS state."""

    try:
        from camoufox.sync_api import Camoufox
    except ImportError as exc:
        raise CamoufoxUnavailable(
            "Camoufox не установлен в окружении бота."
        ) from exc

    with _CAMOUFOX_LOCK:
        return _download_with_camoufox(
            Camoufox,
            page_url=page_url,
            download_dir=download_dir,
            profile_dir=profile_dir,
        )


def _download_with_camoufox(
    camoufox_factory: object,
    *,
    page_url: str,
    download_dir: Path,
    profile_dir: Path,
) -> DownloadedMedia:
    profile_dir.mkdir(parents=True, exist_ok=True)
    download_dir.mkdir(parents=True, exist_ok=True)

    with camoufox_factory(
        headless="virtual",
        os="linux",
        persistent_context=True,
        user_data_dir=str(profile_dir),
    ) as context:
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(15_000)
        page.set_default_navigation_timeout(60_000)

        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=60_000)
        except Exception:
            # TikTok may keep a challenge request open after the useful page state
            # has already arrived. The DOM is still worth inspecting below.
            LOGGER.warning("Camoufox navigation did not finish for %s", page_url)

        page.wait_for_timeout(3_500)
        webpage = page.content()
        candidates = _extract_media_urls(webpage)

        for locator in ("video", "source"):
            for index in range(page.locator(locator).count()):
                element = page.locator(locator).nth(index)
                for attribute in ("src", "data-src"):
                    value = element.get_attribute(attribute)
                    if value and value.startswith("http"):
                        candidates.videos.append(value)

        candidates = _deduplicate_media_urls(candidates)
        return _download_media_urls(
            context,
            candidates,
            download_dir=download_dir,
            referer=page_url,
        )


def _extract_media_urls(webpage: str) -> _MediaUrls:
    match = UNIVERSAL_DATA_RE.search(webpage)
    if not match:
        return _MediaUrls(videos=[], images=[])

    try:
        universal_data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return _MediaUrls(videos=[], images=[])

    videos: list[str] = []
    images: list[str] = []

    def add_url(target: list[str], value: object) -> None:
        if isinstance(value, str) and value.startswith("http"):
            target.append(value)
        elif isinstance(value, dict):
            for nested in value.values():
                add_url(target, nested)
        elif isinstance(value, list):
            for nested in value:
                add_url(target, nested)

    def visit(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                normalized_key = str(key).lower()
                if normalized_key in {
                    "playaddr",
                    "downloadaddr",
                    "playapi",
                    "downloadapi",
                    "playaddrstruct",
                }:
                    add_url(videos, value)
                elif normalized_key == "imageurl":
                    add_url(images, value)
                visit(value)
            return

        if isinstance(node, list):
            for item in node:
                visit(item)

    visit(universal_data)
    return _MediaUrls(videos=videos, images=images)


def _deduplicate_media_urls(candidates: _MediaUrls) -> _MediaUrls:
    def unique(urls: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            result.append(url)
        return result

    return _MediaUrls(videos=unique(candidates.videos), images=unique(candidates.images))


def _download_media_urls(
    context: object,
    candidates: _MediaUrls,
    *,
    download_dir: Path,
    referer: str,
) -> DownloadedMedia:
    videos: list[Path] = []
    images: list[Path] = []
    request_context = context.request
    headers = {
        "Referer": referer,
        "User-Agent": BROWSER_USER_AGENT,
        "Accept": "*/*",
    }

    for index, url in enumerate(candidates.videos, start=1):
        media_path = _download_one_media(
            request_context,
            url,
            download_dir / f"video_{index:02d}",
            headers=headers,
            allowed_extensions=VIDEO_EXTENSIONS,
        )
        if media_path:
            videos.append(media_path)
            break

    for index, url in enumerate(candidates.images, start=1):
        media_path = _download_one_media(
            request_context,
            url,
            download_dir / f"image_{index:02d}",
            headers=headers,
            allowed_extensions=IMAGE_EXTENSIONS,
        )
        if media_path:
            images.append(media_path)

    return DownloadedMedia(videos=videos, images=images)


def _download_one_media(
    request_context: object,
    url: str,
    target_without_extension: Path,
    *,
    headers: dict[str, str],
    allowed_extensions: set[str],
) -> Path | None:
    try:
        response = request_context.get(
            url,
            headers=headers,
            timeout=60_000,
            fail_on_status_code=False,
        )
        if not response.ok:
            return None

        content = response.body()
        if not content or len(content) > MAX_MEDIA_BYTES:
            return None

        extension = _guess_media_extension(
            url,
            response.headers.get("content-type"),
            allowed_extensions,
            content,
        )
        if extension is None:
            return None

        target_path = target_without_extension.with_suffix(extension)
        target_path.write_bytes(content)
        return target_path
    except Exception:
        LOGGER.warning("Camoufox media request failed for %s", _safe_url(url))
        return None


def _guess_media_extension(
    url: str,
    content_type: str | None,
    allowed_extensions: set[str],
    content: bytes,
) -> str | None:
    if allowed_extensions == VIDEO_EXTENSIONS:
        if content.startswith(b"\x00\x00\x00") or content.startswith(b"RIFF"):
            return ".mp4"
        return None

    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return ".webp"

    if content_type:
        extension = mimetypes.guess_extension(content_type.split(";", 1)[0].strip().lower()) or ""
        if extension == ".jpe":
            extension = ".jpg"
        if extension in allowed_extensions:
            return extension

    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in allowed_extensions else None


def _safe_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
