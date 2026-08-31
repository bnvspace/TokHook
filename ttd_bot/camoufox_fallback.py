from __future__ import annotations

import json
import logging
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock, Timer
from urllib.parse import urlparse

from ttd_bot.downloader import (
    BROWSER_USER_AGENT,
    DownloadedMedia,
    IMAGE_EXTENSIONS,
    MAX_DOWNLOAD_SECONDS,
    MAX_IMAGE_BYTES,
    MAX_MEDIA_BYTES,
    MAX_PHOTO_IMAGES,
    UNIVERSAL_DATA_RE,
    VIDEO_EXTENSIONS,
    _open_url,
    _remaining_timeout,
    _set_response_timeout,
)


LOGGER = logging.getLogger(__name__)
_CAMOUFOX_LOCK = Lock()
MAX_VIDEO_CANDIDATES = 8


class CamoufoxUnavailable(RuntimeError):
    """Raised when the optional browser fallback is not installed."""


def _abort_camoufox_page(page: object) -> None:
    """Best-effort watchdog abort for sync Playwright calls without a timeout arg."""

    try:
        page.close()
    except Exception:
        LOGGER.warning("Camoufox watchdog could not close the page")


@dataclass(slots=True)
class _MediaUrls:
    videos: list[str]
    images: list[str]


def download_tiktok_media_with_camoufox(
    page_url: str,
    download_dir: Path,
    profile_dir: Path,
    deadline: float | None = None,
) -> DownloadedMedia:
    """Open a TikTok page in Camoufox and download URLs exposed by its JS state."""

    try:
        from camoufox.sync_api import Camoufox
    except ImportError as exc:
        raise CamoufoxUnavailable(
            "Camoufox не установлен в окружении бота."
        ) from exc

    if deadline is None:
        deadline = time.monotonic() + MAX_DOWNLOAD_SECONDS
    remaining = _remaining_timeout(deadline, MAX_DOWNLOAD_SECONDS)
    if remaining <= 0 or not _CAMOUFOX_LOCK.acquire(timeout=remaining):
        return DownloadedMedia(videos=[], images=[])
    try:
        return _download_with_camoufox(
            Camoufox,
            page_url=page_url,
            download_dir=download_dir,
            profile_dir=profile_dir,
            deadline=deadline,
        )
    finally:
        _CAMOUFOX_LOCK.release()


def _download_with_camoufox(
    camoufox_factory: object,
    *,
    page_url: str,
    download_dir: Path,
    profile_dir: Path,
    deadline: float,
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
        watchdog = Timer(
            max(0.0, deadline - time.monotonic()),
            _abort_camoufox_page,
            args=(page,),
        )
        watchdog.daemon = True
        watchdog.start()
        try:
            page.set_default_timeout(15_000)
            page.set_default_navigation_timeout(60_000)

            try:
                navigation_timeout = _remaining_timeout(deadline, 60)
                if navigation_timeout <= 0:
                    return DownloadedMedia(videos=[], images=[])
                page.goto(
                    page_url,
                    wait_until="domcontentloaded",
                    timeout=max(1, int(navigation_timeout * 1000)),
                )
            except Exception:
                # TikTok may keep a challenge request open after the useful page state
                # has already arrived. The DOM is still worth inspecting below.
                LOGGER.warning("Camoufox navigation did not finish for %s", page_url)

            remaining = _remaining_timeout(deadline, 15)
            if remaining <= 0:
                return DownloadedMedia(videos=[], images=[])
            page.set_default_timeout(max(1, int(remaining * 1000)))
            page.wait_for_timeout(int(min(3_500, remaining * 1000)))
            if _remaining_timeout(deadline, 15) <= 0:
                return DownloadedMedia(videos=[], images=[])
            webpage = page.content()
            if _remaining_timeout(deadline, 15) <= 0:
                return DownloadedMedia(videos=[], images=[])
            candidates = _extract_media_urls(webpage)

            for locator in ("video", "source"):
                if _remaining_timeout(deadline, 15) <= 0:
                    break
                elements = page.locator(locator)
                for index in range(elements.count()):
                    if _remaining_timeout(deadline, 15) <= 0:
                        break
                    element = elements.nth(index)
                    for attribute in ("src", "data-src"):
                        if _remaining_timeout(deadline, 15) <= 0:
                            break
                        value = element.get_attribute(attribute)
                        if value and value.startswith("http"):
                            candidates.videos.append(value)

            candidates = _deduplicate_media_urls(candidates)
            candidates.videos = candidates.videos[:MAX_VIDEO_CANDIDATES]
            candidates.images = candidates.images[:MAX_PHOTO_IMAGES]
            return _download_media_urls(
                context,
                candidates,
                download_dir=download_dir,
                referer=page_url,
                deadline=deadline,
            )
        finally:
            watchdog.cancel()


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
    deadline: float,
) -> DownloadedMedia:
    videos: list[Path] = []
    images: list[Path] = []
    headers = {
        "Referer": referer,
        "User-Agent": BROWSER_USER_AGENT,
        "Accept": "*/*",
    }

    for index, url in enumerate(candidates.videos, start=1):
        if _remaining_timeout(deadline, 60) <= 0:
            break
        media_path = _download_one_media(
            context,
            url,
            download_dir / f"video_{index:02d}",
            headers=headers,
            allowed_extensions=VIDEO_EXTENSIONS,
            timeout=_remaining_timeout(deadline, 60),
            deadline=deadline,
        )
        if media_path:
            videos.append(media_path)
            break

    for index, url in enumerate(candidates.images, start=1):
        if _remaining_timeout(deadline, 60) <= 0:
            break
        media_path = _download_one_media(
            context,
            url,
            download_dir / f"image_{index:02d}",
            headers=headers,
            allowed_extensions=IMAGE_EXTENSIONS,
            timeout=_remaining_timeout(deadline, 60),
            deadline=deadline,
        )
        if media_path:
            images.append(media_path)

    return DownloadedMedia(videos=videos, images=images)


def _download_one_media(
    context: object,
    url: str,
    target_without_extension: Path,
    *,
    headers: dict[str, str],
    allowed_extensions: set[str],
    timeout: float,
    deadline: float,
) -> Path | None:
    try:
        cookie_header = _browser_cookie_header(context, url)
        response = _open_url(
            url,
            referer=headers["Referer"],
            timeout=timeout,
            cookie_header=cookie_header,
            public_media_only=True,
            deadline=deadline,
        )
        status = getattr(response, "status", None) or response.getcode()
        if status and status >= 400:
            return None

        max_bytes = MAX_IMAGE_BYTES if allowed_extensions == IMAGE_EXTENSIONS else MAX_MEDIA_BYTES
        with response:
            return _save_response_limited(
                response,
                url,
                target_without_extension,
                response.headers.get("content-type"),
                allowed_extensions,
                max_bytes,
                deadline,
            )
    except Exception:
        LOGGER.warning("Camoufox media request failed for %s", _safe_url(url))
        return None


def _save_response_limited(
    response: object,
    url: str,
    target_without_extension: Path,
    content_type: str | None,
    allowed_extensions: set[str],
    max_bytes: int,
    deadline: float | None = None,
) -> Path | None:
    """Stream a browser-discovered media response without buffering it in RAM."""

    part_path = target_without_extension.with_name(target_without_extension.name + ".part")
    total_bytes = 0
    prefix = bytearray()
    try:
        with part_path.open("wb") as output:
            while True:
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    _set_response_timeout(response, max(0.001, remaining))
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    return None
                if len(prefix) < 512:
                    prefix.extend(chunk[: 512 - len(prefix)])
                output.write(chunk)

        if total_bytes == 0:
            return None
        extension = _guess_media_extension(
            url,
            content_type,
            allowed_extensions,
            bytes(prefix),
        )
        if extension is None:
            return None

        target_path = target_without_extension.with_suffix(extension)
        part_path.replace(target_path)
        return target_path
    finally:
        part_path.unlink(missing_ok=True)


def _browser_cookie_header(context: object, url: str) -> str | None:
    try:
        cookies = context.cookies(url)
    except Exception:
        return None
    pairs = [
        f"{cookie['name']}={cookie['value']}"
        for cookie in cookies
        if isinstance(cookie, dict) and cookie.get("name") and cookie.get("value")
    ]
    return "; ".join(pairs) or None


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
