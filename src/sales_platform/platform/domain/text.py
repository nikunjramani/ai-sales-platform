"""Moved here by ADR 0026 (module order)."""

MAX_ERROR_CHARACTERS = 300


class UnrenderableContent(Exception):
    """The content cannot be put into the PDF faithfully (e.g. unsupported characters).

    Rendering never drops or replaces characters silently: a changed name or description in a
    commercial document is worse than no document.
    """


# The built-in PDF fonts show Windows-1252 (Western European) text only.
PDF_TEXT_ENCODING = "cp1252"


def printable_text(value: object, field: str) -> str:
    """One line of text the PDF can show exactly, or UnrenderableContent.

    Shared by the renderer and by the business profile form (phase 2), so a saved profile can
    never make every later PDF of the business fail.
    """
    text = " ".join(str(value).split())
    try:
        text.encode(PDF_TEXT_ENCODING)
    except UnicodeEncodeError:
        raise UnrenderableContent(
            f"{field} contains characters the PDF fonts cannot show"
        ) from None
    return text


def clean_error(message: str) -> str:
    """Short, single-line error text for delivery records (no stack traces or payloads)."""
    text = " ".join(message.split()) or "Unknown error"
    return text[:MAX_ERROR_CHARACTERS]
