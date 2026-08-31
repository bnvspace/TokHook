from pathlib import Path

import pytest

from ttd_bot.downloader import (
    MAX_IMAGE_BYTES,
    TikTokDownloadError,
    _collect_media_from_info,
    _collect_downloaded_media,
    _extract_photo_image_urls,
    _read_response_limited,
    extract_tiktok_url,
    is_tiktok_photo_url,
    normalize_tiktok_download_url,
    resolve_tiktok_url,
)
from ttd_bot.camoufox_fallback import _extract_media_urls


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "держи https://www.tiktok.com/@someone/video/1234567890",
            "https://www.tiktok.com/@someone/video/1234567890",
        ),
        (
            "short link https://vm.tiktok.com/ZM1234567/",
            "https://vm.tiktok.com/ZM1234567/",
        ),
        (
            "https://vt.tiktok.com/ZSabc1234/?foo=bar",
            "https://vt.tiktok.com/ZSabc1234/?foo=bar",
        ),
    ],
)
def test_extract_tiktok_url_returns_first_valid_url(text: str, expected: str) -> None:
    assert extract_tiktok_url(text) == expected


def test_extract_tiktok_url_strips_trailing_punctuation() -> None:
    assert (
        extract_tiktok_url("смотри: https://www.tiktok.com/@name/video/42).")
        == "https://www.tiktok.com/@name/video/42"
    )


def test_extract_tiktok_url_returns_none_for_non_tiktok_text() -> None:
    assert extract_tiktok_url("https://example.com/video/42") is None


def test_extract_tiktok_url_rejects_lookalike_host() -> None:
    assert extract_tiktok_url("https://tiktok.com.evil.example/@name/video/42") is None


def test_normalize_tiktok_download_url_converts_photo_post_to_video_post() -> None:
    assert (
        normalize_tiktok_download_url(
            "https://www.tiktok.com/@mrfog/photo/7525699347456019726?is_from_webapp=1&sender_device=pc"
        )
        == "https://www.tiktok.com/@mrfog/video/7525699347456019726"
    )


def test_is_tiktok_photo_url_detects_photo_posts() -> None:
    assert is_tiktok_photo_url("https://www.tiktok.com/@mrfog/photo/7525699347456019726")
    assert not is_tiktok_photo_url("https://www.tiktok.com/@mrfog/video/7525699347456019726")


def test_extract_photo_image_urls_reads_image_post_data() -> None:
    webpage = """
    <script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">
    {
      "__DEFAULT_SCOPE__": {
        "webapp.video-detail": {
          "itemInfo": {
            "itemStruct": {
              "imagePost": {
                "images": [
                  {"imageURL": {"urlList": ["https://example.com/1.jpg", "https://backup/1.jpg"]}},
                  {"imageURL": {"urlList": ["https://example.com/2.webp"]}}
                ]
              }
            }
          }
        }
      }
    }
    </script>
    """

    assert _extract_photo_image_urls(webpage) == [
        "https://example.com/1.jpg",
        "https://example.com/2.webp",
    ]


def test_extract_media_urls_reads_video_and_image_post_data() -> None:
    webpage = """
    <script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">
    {
      "__DEFAULT_SCOPE__": {
        "webapp.video-detail": {
          "itemInfo": {
            "itemStruct": {
              "video": {
                "playAddr": {"urlList": ["https://cdn.example/video.mp4"]}
              },
              "imagePost": {
                "images": [
                  {"imageURL": {"urlList": ["https://cdn.example/image.jpg"]}}
                ]
              }
            }
          }
        }
      }
    }
    </script>
    """

    candidates = _extract_media_urls(webpage)

    assert candidates.videos == ["https://cdn.example/video.mp4"]
    assert candidates.images == ["https://cdn.example/image.jpg"]


def test_resolve_tiktok_url_returns_redirect_target_for_short_link(monkeypatch: pytest.MonkeyPatch) -> None:
    class DummyResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def geturl(self) -> str:
            return "https://www.tiktok.com/@gallerieapp/photo/7634004117156908318?_r=1&_t=ZS-96CU8sYh4f2"

    monkeypatch.setattr(
        "ttd_bot.downloader._open_url",
        lambda url, cookies_path=None, referer=None: DummyResponse(),
    )

    assert resolve_tiktok_url("https://vt.tiktok.com/ZS9n2pqYV/") == (
        "https://www.tiktok.com/@gallerieapp/photo/7634004117156908318?_r=1&_t=ZS-96CU8sYh4f2"
    )


def test_resolve_tiktok_url_rejects_external_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    class DummyResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def geturl(self) -> str:
            return "https://example.com/redirected"

    monkeypatch.setattr(
        "ttd_bot.downloader._open_url",
        lambda url, cookies_path=None, referer=None: DummyResponse(),
    )

    with pytest.raises(TikTokDownloadError, match="перенаправляет"):
        resolve_tiktok_url("https://vt.tiktok.com/ZS9n2pqYV/")


def test_read_response_limited_stops_after_limit() -> None:
    class DummyResponse:
        headers: dict[str, str] = {}

        def read(self, size: int) -> bytes:
            return b"x" * (size + 1)

    assert _read_response_limited(DummyResponse(), 16) is None


def test_collect_media_ignores_oversize_files_and_external_paths(tmp_path: Path) -> None:
    inside = tmp_path / "ok.jpg"
    inside.write_bytes(b"jpg")
    oversized = tmp_path / "oversized.jpg"
    oversized.touch()
    with oversized.open("r+b") as file:
        file.truncate(MAX_IMAGE_BYTES + 1)
    outside = tmp_path.parent / "outside.mp4"
    outside.write_bytes(b"video")

    try:
        assert _collect_downloaded_media(tmp_path) == [inside]
        assert _collect_media_from_info(
            {"_filename": str(outside), "entries": [{"filepath": str(inside)}]},
            tmp_path,
        ) == [inside]
    finally:
        outside.unlink(missing_ok=True)
