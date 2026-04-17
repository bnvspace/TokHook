import pytest

from ttd_bot.downloader import (
    _extract_photo_image_urls,
    extract_tiktok_url,
    is_tiktok_photo_url,
    normalize_tiktok_download_url,
)


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
