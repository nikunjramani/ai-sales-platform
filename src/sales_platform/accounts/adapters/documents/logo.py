"""Business logo checks and re-encoding with Pillow (phase 2, Q16).

An uploaded image is untrusted: image parsers have had many security bugs, and huge images can
exhaust memory ("decompression bombs"). So:
- only PNG and JPEG, recognised by their first bytes (not by the name or declared type);
- at most 1 MB and 4 megapixels, checked from the header before any pixels are decoded;
- the image is decoded once, shrunk to at most 600 x 240 px and saved as a fresh PNG. Nothing
  from the original file (metadata, extra chunks, scripts) survives; only pixels do.
"""

from io import BytesIO

from PIL import Image, UnidentifiedImageError

from sales_platform.accounts.ports.accounts import InvalidLogo

MAX_LOGO_BYTES = 1_000_000
MAX_LOGO_PIXELS = 4_000_000
LOGO_BOX = (600, 240)

_SIGNATURES = {b"\x89PNG\r\n\x1a\n": "PNG", b"\xff\xd8\xff": "JPEG"}


class PillowLogoNormalizer:
    def normalize(self, data: bytes) -> bytes:
        if not data or len(data) > MAX_LOGO_BYTES:
            raise InvalidLogo("The logo must be a PNG or JPEG file of at most 1 MB")
        expected = next(
            (kind for signature, kind in _SIGNATURES.items() if data.startswith(signature)), None
        )
        if expected is None:
            raise InvalidLogo("The logo must be a PNG or JPEG file")
        try:
            with Image.open(BytesIO(data), formats=[expected]) as probe:
                if probe.width * probe.height > MAX_LOGO_PIXELS:
                    raise InvalidLogo("The logo has too many pixels (at most 4 megapixels)")
                probe.verify()
            with Image.open(BytesIO(data), formats=[expected]) as image:
                image.load()
                pixels = image.convert("RGBA")
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError):
            raise InvalidLogo("The logo file could not be read") from None
        pixels.thumbnail(LOGO_BOX)
        output = BytesIO()
        pixels.save(output, format="PNG", optimize=True)
        return output.getvalue()
