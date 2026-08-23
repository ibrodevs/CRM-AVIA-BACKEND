from __future__ import annotations


def _font_widths(font, inverse):
    """Return Unicode glyph widths in the PDF's 1000-unit text space."""

    widths_by_code = {}
    default_width = 1000.0
    try:
        if str(font.get("/Subtype")) == "/Type0":
            descendants = font.get("/DescendantFonts") or []
            descendant = descendants[0].get_object() if descendants else {}
            default_width = float(descendant.get("/DW", 1000))
            values = list(descendant.get("/W") or [])
            index = 0
            while index < len(values):
                first = int(values[index])
                second = values[index + 1]
                if isinstance(second, list):
                    for offset, width in enumerate(second):
                        widths_by_code[first + offset] = float(width)
                    index += 2
                    continue
                last = int(second)
                width = float(values[index + 2])
                for code in range(first, last + 1):
                    widths_by_code[code] = width
                index += 3
        else:
            first = int(font.get("/FirstChar", 0))
            for offset, width in enumerate(font.get("/Widths") or []):
                widths_by_code[first + offset] = float(width)
    except (AttributeError, IndexError, TypeError, ValueError):
        # Width data is optional and malformed vendor PDFs are common. Text
        # replacement remains safe without the overprint adjustment below.
        widths_by_code = {}

    widths = {}
    for character, encoded in inverse.items():
        code = ord(encoded) if isinstance(encoded, str) else int(encoded)
        widths[character] = widths_by_code.get(code, default_width)
    return widths, default_width


def _font_maps(font):
    """Return the font encoding and ToUnicode map across supported pypdf APIs."""

    # pypdf 6 exposes get_encoding() as the lower-level mapping API. Older
    # versions also exposed build_char_map_from_dict(), so keep compatibility
    # with both while using the same normalized result below.
    try:
        from pypdf._cmap import get_encoding

        encoding, char_map = get_encoding(font)
        return encoding, char_map
    except (ImportError, AttributeError, TypeError, ValueError):
        pass

    try:
        from pypdf._cmap import build_char_map_from_dict

        _subtype, _space, encoding, char_map = build_char_map_from_dict(200, font)
        return encoding, char_map
    except Exception:
        return None, None


def _font_codec(font):
    """Build a reversible codec for the font already embedded in a supplier PDF.

    Type0/CID fonts expose a multibyte encoding + ToUnicode map, while common
    Type1 fonts expose a byte-code -> Unicode dictionary. Supporting both lets
    the correction path reuse the exact original font resource instead of
    drawing a replacement text layer with a look-alike font.
    """

    encoding, char_map = _font_maps(font)

    if isinstance(encoding, str) and isinstance(char_map, dict):
        inverse = {
            unicode_char: encoded_char
            for encoded_char, unicode_char in char_map.items()
            if isinstance(encoded_char, str)
            and isinstance(unicode_char, str)
            and len(unicode_char) == 1
        }
        widths, default_width = _font_widths(font, inverse)
        return {
            "kind": "multibyte",
            "encoding": encoding,
            "char_map": char_map,
            "inverse": inverse,
            "base_font": str(font.get("/BaseFont") or ""),
            "widths": widths,
            "default_width": default_width,
        }

    if isinstance(encoding, dict):
        byte_map = {
            int(code): str(unicode_char)
            for code, unicode_char in encoding.items()
            if isinstance(code, int)
            and isinstance(unicode_char, str)
            and len(unicode_char) == 1
        }
        # A font may still provide extra ToUnicode entries. For one-byte codes
        # they are useful when the base encoding does not contain a character.
        if isinstance(char_map, dict):
            for encoded_char, unicode_char in char_map.items():
                if not isinstance(encoded_char, str) or len(encoded_char) != 1:
                    continue
                if not isinstance(unicode_char, str) or len(unicode_char) != 1:
                    continue
                code = ord(encoded_char)
                if 0 <= code <= 255:
                    byte_map[code] = unicode_char
        inverse = {}
        for code, unicode_char in byte_map.items():
            inverse.setdefault(unicode_char, code)
        widths, default_width = _font_widths(font, inverse)
        return {
            "kind": "single-byte",
            "byte_map": byte_map,
            "inverse": inverse,
            "base_font": str(font.get("/BaseFont") or ""),
            "widths": widths,
            "default_width": default_width,
        }

    return None


def _original_bytes(value) -> bytes:
    raw = getattr(value, "original_bytes", None)
    if raw is not None:
        return bytes(raw)
    try:
        return bytes(value)
    except Exception:
        return str(value).encode("latin1", errors="ignore")


def _decode_text(value, codec) -> str:
    if codec is None:
        return str(value)

    raw = _original_bytes(value)
    if codec.get("kind") == "single-byte":
        byte_map = codec.get("byte_map") or {}
        return "".join(byte_map.get(byte, chr(byte)) for byte in raw)

    if codec.get("kind") == "multibyte":
        encoding = codec.get("encoding")
        char_map = codec.get("char_map") or {}
        try:
            encoded_text = raw.decode(encoding)
        except Exception:
            return str(value)
        return "".join(char_map.get(char, char) for char in encoded_text)

    return str(value)


def _encode_text(text: str, codec):
    from pypdf.generic import ByteStringObject

    if codec is None:
        return None

    inverse = codec.get("inverse") or {}
    if codec.get("kind") == "single-byte":
        output = bytearray()
        for char in text:
            code = inverse.get(char)
            if code is None and not inverse and ord(char) < 128:
                code = ord(char)
            if code is None or not 0 <= int(code) <= 255:
                return None
            output.append(int(code))
        return ByteStringObject(bytes(output))

    if codec.get("kind") == "multibyte":
        encoding = codec.get("encoding")
        encoded_chars = []
        for char in text:
            encoded = inverse.get(char)
            if encoded is None:
                if not inverse and ord(char) < 128:
                    encoded = char
                else:
                    return None
            encoded_chars.append(encoded)
        try:
            return ByteStringObject("".join(encoded_chars).encode(encoding))
        except Exception:
            return None

    return None


def install_receipt_supplier_pdf_font_codec() -> None:
    from documents import receipt_supplier_pdf_patch as supplier_pdf

    if getattr(supplier_pdf._font_codec, "_supports_supplier_fonts", False):
        return

    _font_codec._supports_supplier_fonts = True
    supplier_pdf._font_codec = _font_codec
    supplier_pdf._original_bytes = _original_bytes
    supplier_pdf._decode_text = _decode_text
    supplier_pdf._encode_text = _encode_text
