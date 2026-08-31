import pytest

from ttd_bot.downloader import (
    _extract_photo_image_urls,
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
