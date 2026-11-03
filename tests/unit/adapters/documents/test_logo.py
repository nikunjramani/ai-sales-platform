from collections.abc import Callable
from io import BytesIO

import pytest
from PIL import Image

from sales_platform.adapters.documents.logo import PillowLogoNormalizer
from sales_platform.ports.accounts import InvalidLogo

type ImageFactory = Callable[[str, int, int], bytes]


@pytest.mark.parametrize("image_format", ["PNG", "JPEG"])
def test_png_and_jpeg_become_a_small_fresh_png(make_image: ImageFactory, image_format: str) -> None:
    result = PillowLogoNormalizer().normalize(make_image(image_format, 1200, 300))
    with Image.open(BytesIO(result)) as image:
        assert image.format == "PNG"
        assert image.width <= 600 and image.height <= 240
        assert not image.info.get("comment")


def test_metadata_does_not_survive(make_image: ImageFactory) -> None:
    from PIL.PngImagePlugin import PngInfo

    info = PngInfo()
    info.add_text("Comment", "<script>alert(1)</script>")
    output = BytesIO()
    Image.new("RGB", (50, 50)).save(output, format="PNG", pnginfo=info)
    result = PillowLogoNormalizer().normalize(output.getvalue())
    assert b"script" not in result


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"GIF89a" + b"\x00" * 100,
        b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
        b"\x89PNG\r\n\x1a\n" + b"broken",
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 1_000_001,
    ],
    ids=["empty", "gif", "svg", "broken-png", "over-1-MB"],
)
def test_other_or_broken_files_are_refused(data: bytes) -> None:
    with pytest.raises(InvalidLogo):
        PillowLogoNormalizer().normalize(data)


def test_too_many_pixels_are_refused_before_decoding(make_image: ImageFactory) -> None:
    huge = make_image("PNG", 2500, 2000)  # 5 megapixels, small file (one colour)
    assert len(huge) < 1_000_000
    with pytest.raises(InvalidLogo, match="pixels"):
        PillowLogoNormalizer().normalize(huge)


def test_a_jpeg_named_png_is_read_by_its_bytes(make_image: ImageFactory) -> None:
    # Only the first bytes decide the type; the declared name/type is never trusted.
    assert PillowLogoNormalizer().normalize(make_image("JPEG", 40, 40)).startswith(b"\x89PNG")
