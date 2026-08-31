from __future__ import annotations

import ipaddress
import json
import logging
import mimetypes
import os
import re
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, Request, build_opener

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
ALLOWED_TIKTOK_HOSTS = {
    "tiktok.com",
    "www.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
    "m.tiktok.com",
}
ALLOWED_MEDIA_HOST_SUFFIXES = (
    ".tiktok.com",
    ".tiktokcdn.com",
    ".tiktokcdn-us.com",
    ".tiktokv.com",
    ".ibytedtos.com",
    ".muscdn.com",
    ".byteimg.com",
    ".pstatp.com",
)
MAX_MEDIA_BYTES = 128 * 1024 * 1024
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_PAGE_BYTES = 16 * 1024 * 1024
MAX_PHOTO_IMAGES = 35
MAX_DOWNLOAD_SECONDS = 180
MAX_REDIRECTS = 5


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
    if not _is_allowed_tiktok_url(url):
        return None
    return url


def download_tiktok_media(
    url: str,
    download_dir: Path,
    cookies_path: Path | None = None,
    camoufox_profile_path: Path | None = None,
) -> DownloadedMedia:
    if not _is_allowed_tiktok_url(url):
        raise TikTokDownloadError("Похоже, ссылка не ведет на TikTok-пост.")

    download_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + MAX_DOWNLOAD_SECONDS
    resolved_url = resolve_tiktok_url(
        url,
        cookies_path,
        timeout=_remaining_timeout(deadline, 30),
        deadline=deadline,
    )
    normalized_url = normalize_tiktok_download_url(resolved_url)

    if is_tiktok_photo_url(resolved_url):
        images = _download_photo_post_images(
            normalized_url,
            download_dir,
            cookies_path,
            deadline=deadline,
        )
        if images:
            return DownloadedMedia(videos=[], images=images)

    info: object | None = None
    download_error = _run_yt_dlp(
        normalized_url,
        download_dir,
        cookies_path,
        timeout=_remaining_timeout(deadline, MAX_DOWNLOAD_SECONDS),
    )

    media_files = _collect_downloaded_media(download_dir)
    if not media_files:
        media_files = _collect_media_from_info(info, download_dir)

    if not media_files:
        media_files = _download_photo_post_images(
            normalized_url,
            download_dir,
            cookies_path,
            deadline=deadline,
        )

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
            deadline=deadline,
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


def resolve_tiktok_url(
    url: str,
    cookies_path: Path | None = None,
    *,
    timeout: float = 30,
    deadline: float | None = None,
) -> str:
    parsed = urlparse(url)
    if TIKTOK_POST_PATH_RE.match(parsed.path):
        return url
    if timeout <= 0:
        return url

    try:
        with _open_url(
            url,
            cookies_path=cookies_path,
            timeout=timeout,
            allowed_redirect_hosts=ALLOWED_TIKTOK_HOSTS,
            deadline=deadline,
        ) as response:
            resolved_url = response.geturl()
            if not _is_allowed_tiktok_url(resolved_url):
                raise TikTokDownloadError(
                    "Ссылка перенаправляет не на TikTok-пост."
                )
            return resolved_url
    except (HTTPError, URLError, OSError):
        return url


def _collect_downloaded_media(download_dir: Path) -> list[Path]:
    resolved_download_dir = download_dir.resolve()
    files = [
        path
        for path in download_dir.rglob("*")
        if (
            path.is_file()
            and path.suffix.lower() in MEDIA_EXTENSIONS
            and _is_path_inside(path, resolved_download_dir)
            and _media_file_is_within_limit(path)
        )
    ]
    return sorted(files, key=lambda path: (path.stat().st_mtime, path.name))


def _collect_media_from_info(info: object, download_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    resolved_download_dir = download_dir.resolve()

    def visit(node: object) -> None:
        if not isinstance(node, dict):
            return

        for key in ("_filename", "filepath"):
            value = node.get(key)
            if isinstance(value, str):
                path = Path(value)
                if (
                    path.exists()
                    and path.suffix.lower() in MEDIA_EXTENSIONS
                    and _is_path_inside(path, resolved_download_dir)
                    and _media_file_is_within_limit(path)
                ):
                    candidates.append(path)

        requested_downloads = node.get("requested_downloads")
        if isinstance(requested_downloads, list):
            for item in requested_downloads:
                if isinstance(item, dict):
                    value = item.get("filepath")
                    if isinstance(value, str):
                        path = Path(value)
                        if (
                            path.exists()
                            and path.suffix.lower() in MEDIA_EXTENSIONS
                            and _is_path_inside(path, resolved_download_dir)
                            and _media_file_is_within_limit(path)
                        ):
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


def _download_photo_post_images(
    page_url: str,
    download_dir: Path,
    cookies_path: Path | None,
    *,
    deadline: float | None = None,
) -> list[Path]:
    if deadline is None:
        deadline = time.monotonic() + MAX_DOWNLOAD_SECONDS
    if _remaining_timeout(deadline, 30) <= 0:
        return []

    try:
        webpage = _download_text(
            page_url,
            cookies_path=cookies_path,
            timeout=_remaining_timeout(deadline, 30),
            deadline=deadline,
        )
    except (HTTPError, URLError, OSError):
        return []

    image_urls = _extract_photo_image_urls(webpage)
    if not image_urls:
        return []

    downloaded_images: list[Path] = []
    for index, image_url in enumerate(image_urls[:MAX_PHOTO_IMAGES], start=1):
        timeout = _remaining_timeout(deadline, 30)
        if timeout <= 0:
            break
        try:
            response = _open_url(
                image_url,
                cookies_path=cookies_path,
                referer=page_url,
                timeout=timeout,
                public_media_only=True,
                deadline=deadline,
            )
            with response:
                content = _read_response_limited(
                    response,
                    MAX_IMAGE_BYTES,
                    deadline=deadline,
                )
                if not content:
                    continue
                extension = _guess_image_extension(image_url, response.headers.get("Content-Type"))
                target_path = download_dir / f"image_{index:02d}{extension}"
                target_path.write_bytes(content)
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


def _download_text(
    url: str,
    *,
    cookies_path: Path | None = None,
    referer: str | None = None,
    timeout: float = 30,
    deadline: float | None = None,
) -> str:
    with _open_url(
        url,
        cookies_path=cookies_path,
        referer=referer,
        timeout=timeout,
        allowed_redirect_hosts=ALLOWED_TIKTOK_HOSTS,
        deadline=deadline,
    ) as response:
        content = _read_response_limited(response, MAX_PAGE_BYTES, deadline=deadline)
        if content is None:
            raise OSError("TikTok page exceeds the configured size limit.")
        return content.decode("utf-8", "replace")


def _open_url(
    url: str,
    *,
    cookies_path: Path | None = None,
    referer: str | None = None,
    timeout: float = 30,
    cookie_header: str | None = None,
    allowed_redirect_hosts: set[str] | None = None,
    public_media_only: bool = False,
    deadline: float | None = None,
):
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise URLError("Request exceeded the download deadline.")
        timeout = min(timeout, remaining)
    if allowed_redirect_hosts is not None and not _host_in_set(url, allowed_redirect_hosts):
        raise TikTokDownloadError("Ссылка ведет на недопустимый адрес.")
    if public_media_only and not _is_allowed_media_url(url):
        raise URLError("Media URL is not a public HTTP(S) address.")

    handlers = [
        _SafeRedirectHandler(
            allowed_hosts=allowed_redirect_hosts,
            public_media_only=public_media_only,
            deadline=deadline,
        )
    ]
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
    if cookie_header:
        headers["Cookie"] = cookie_header
    request = Request(url, headers=headers)
    return opener.open(request, timeout=timeout)


class _SafeRedirectHandler(HTTPRedirectHandler):
    handler_order = 499

    def __init__(
        self,
        *,
        allowed_hosts: set[str] | None,
        public_media_only: bool,
        deadline: float | None = None,
    ) -> None:
        super().__init__()
        self.allowed_hosts = allowed_hosts
        self.public_media_only = public_media_only
        self.deadline = deadline
        self.redirect_count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        remaining: float | None = None
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise URLError("Redirect chain exceeded the download deadline.")
        if self.redirect_count >= MAX_REDIRECTS:
            raise URLError("Redirect chain exceeded the configured limit.")
        self.redirect_count += 1
        target_url = urljoin(req.full_url, newurl)
        if self.allowed_hosts is not None and not _host_in_set(target_url, self.allowed_hosts):
            raise TikTokDownloadError("Ссылка перенаправляет не на TikTok-пост.")
        if self.public_media_only and not _is_allowed_media_url(target_url):
            raise URLError("Media redirect is not a public HTTP(S) address.")
        if remaining is not None:
            req.timeout = max(0.01, remaining)
        return super().redirect_request(req, fp, code, msg, headers, target_url)


def _read_response_limited(
    response: object,
    max_bytes: int,
    *,
    deadline: float | None = None,
) -> bytes | None:
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                return None
        except ValueError:
            pass

    chunks: list[bytes] = []
    total = 0
    while True:
        remaining: float | None = None
        if deadline is not None and time.monotonic() >= deadline:
            return None
        if deadline is not None:
            remaining = max(0.001, deadline - time.monotonic())
            _set_response_timeout(response, remaining)
        chunk = response.read(min(64 * 1024, max_bytes - total + 1))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            return None
    return b"".join(chunks)


def _set_response_timeout(response: object, timeout: float) -> None:
    """Best-effort timeout update for urllib's underlying socket."""

    candidates = [
        getattr(response, "fp", None),
        getattr(getattr(response, "fp", None), "raw", None),
        getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None),
    ]
    for candidate in candidates:
        setter = getattr(candidate, "settimeout", None)
        if callable(setter):
            try:
                setter(timeout)
            except OSError:
                pass
            return


def _remaining_timeout(deadline: float, default: float) -> float:
    return min(default, max(0.0, deadline - time.monotonic()))


def _is_path_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent)
    except ValueError:
        return False
    return True


def _media_file_is_within_limit(path: Path) -> bool:
    try:
        max_bytes = MAX_IMAGE_BYTES if path.suffix.lower() in IMAGE_EXTENSIONS else MAX_MEDIA_BYTES
        return path.stat().st_size <= max_bytes
    except OSError:
        return False


def _is_allowed_tiktok_url(url: str) -> bool:
    return _host_in_set(url, ALLOWED_TIKTOK_HOSTS)


def _host_in_set(url: str, allowed_hosts: set[str]) -> bool:
    hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    return hostname in allowed_hosts


def _is_public_http_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False

    hostname = parsed.hostname.rstrip(".")
    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        try:
            addresses = [
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
            ]
        except (OSError, ValueError):
            return False

    return bool(addresses) and all(address.is_global for address in addresses)


def _is_allowed_media_url(url: str) -> bool:
    if not _is_public_http_url(url):
        return False
    hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    return hostname in ALLOWED_TIKTOK_HOSTS or any(
        hostname.endswith(suffix) for suffix in ALLOWED_MEDIA_HOST_SUFFIXES
    )


def _run_yt_dlp(
    url: str,
    download_dir: Path,
    cookies_path: Path | None,
    *,
    timeout: float,
) -> DownloadError | None:
    if timeout <= 0:
        return DownloadError("yt-dlp exceeded the download deadline.")

    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--paths",
        str(download_dir),
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        "--no-progress",
        "--restrict-filenames",
        "--windows-filenames",
        "--retries",
        "2",
        "--fragment-retries",
        "2",
        "--socket-timeout",
        "30",
        "--max-filesize",
        str(MAX_MEDIA_BYTES),
        "--no-skip-unavailable-fragments",
        "--format",
        "best[ext=mp4]/best",
        "--merge-output-format",
        "mp4",
    ]
    if cookies_path:
        command.extend(["--cookies", str(cookies_path)])
    command.append(url)

    popen_kwargs: dict[str, object] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    try:
        process = subprocess.Popen(command, **popen_kwargs)
        if os.name != "nt":
            try:
                process._ttd_process_group = os.getpgid(process.pid)
            except OSError:
                pass
        try:
            stdout, stderr = process.communicate(timeout=max(0.01, timeout))
        except subprocess.TimeoutExpired:
            _terminate_process_tree(process)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                _terminate_process_tree(process)
                try:
                    process.kill()
                except OSError:
                    pass
                _reap_process(process)
                for stream in (
                    getattr(process, "stdout", None),
                    getattr(process, "stderr", None),
                ):
                    if stream is not None:
                        stream.close()
            return DownloadError("yt-dlp exceeded the download deadline.")
    except OSError as exc:
        return DownloadError(f"Не удалось запустить yt-dlp: {exc}")

    if process.returncode == 0:
        return None

    details = (stderr or stdout or "yt-dlp exited with an error").strip()
    return DownloadError(details[-4_000:])


def _terminate_process_tree(process: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            if process.poll() is not None:
                return
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
        else:
            descendants = _descendant_pids(process.pid)
            process_group = getattr(process, "_ttd_process_group", None)
            if process_group is None:
                if process.poll() is not None:
                    process_group = None
                else:
                    process_group = os.getpgid(process.pid)
            if process_group is not None:
                try:
                    os.killpg(process_group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            for descendant in descendants:
                try:
                    os.kill(descendant, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass


def _descendant_pids(root_pid: int) -> list[int]:
    """Find descendants even when a browser creates a separate process group."""

    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return []

    children: dict[int, list[int]] = {}
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "stat").read_text(encoding="utf-8")
        except OSError:
            continue
        match = re.match(r"^\d+ \(.*\) \S+ (\d+) ", status)
        if match is None:
            continue
        parent_pid = int(match.group(1))
        children.setdefault(parent_pid, []).append(int(entry.name))

    descendants: list[int] = []
    pending = list(children.get(root_pid, []))
    while pending:
        pid = pending.pop()
        descendants.append(pid)
        pending.extend(children.get(pid, []))
    return descendants


def _reap_process(process: subprocess.Popen) -> bool:
    """Reap a killed child within a bounded interval."""

    wait = getattr(process, "wait", None)
    if not callable(wait):
        return False
    try:
        wait(timeout=1)
        return True
    except (subprocess.TimeoutExpired, OSError):
        try:
            process.kill()
        except OSError:
            pass
        try:
            wait(timeout=1)
            return True
        except (subprocess.TimeoutExpired, OSError):
            LOGGER.error("Could not reap timed-out yt-dlp process %s", process.pid)
            return False


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
