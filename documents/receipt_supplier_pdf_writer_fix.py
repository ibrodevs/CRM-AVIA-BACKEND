from __future__ import annotations

from io import BytesIO
import re


def _replace_combined_text_all(array, codec, target, context: str, supplier_pdf) -> int:
    """Replace every duplicate of one amount inside a single TJ text run.

    RZD coupons can paint the same итог twice in one TJ array. Replacing only
    the first occurrence leaves a visually inconsistent supplier blank. We
    redistribute a same-length replacement back into the original text chunks,
    so all existing glyph positions/kerning operators and the embedded font are
    preserved. If the replacement changes text length, the structured path
    leaves the run untouched and the raw-stream fallback gets a chance to patch
    it without asking pypdf to parse the malformed text object.
    """

    from pypdf.generic import ByteStringObject, TextStringObject

    positions = [
        index
        for index, item in enumerate(array)
        if isinstance(item, (TextStringObject, ByteStringObject))
    ]
    if not positions:
        return 0
    chunks = [supplier_pdf._decode_text(array[index], codec) for index in positions]
    combined = "".join(chunks)
    upper_context = (context + " " + combined).upper()
    if target.aliases and not any(alias.upper() in upper_context for alias in target.aliases):
        return 0

    for variant in supplier_pdf._amount_variants(target.old):
        pattern = re.compile(r"(?<!\d)" + re.escape(variant) + r"(?!\d)")
        matches = list(pattern.finditer(combined))
        if not matches:
            continue
        updated = pattern.sub(lambda match: supplier_pdf._format_like(match.group(0), target.new), combined)
        if len(updated) != len(combined):
            return 0
        offset = 0
        encoded_chunks = []
        for chunk in chunks:
            replacement_chunk = updated[offset : offset + len(chunk)]
            offset += len(chunk)
            encoded = supplier_pdf._encode_text(replacement_chunk, codec)
            if encoded is None:
                return 0
            encoded_chunks.append(encoded)
        for array_index, encoded in zip(positions, encoded_chunks):
            array[array_index] = encoded
        return len(matches)
    return 0


def _write_reader_pages(reader) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    if reader.metadata:
        metadata = {
            str(key): str(value)
            for key, value in reader.metadata.items()
            if key and value is not None
        }
        if metadata:
            writer.add_metadata(metadata)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _patch_supplier_pdf_structured(content: bytes, before: dict, after: dict) -> tuple[bytes | None, dict]:
    """Use pypdf's parsed operators when the supplier stream is valid."""

    from pypdf import PdfReader
    from pypdf.generic import ArrayObject, ByteStringObject, ContentStream, TextStringObject
    from documents import receipt_supplier_pdf_patch as supplier_pdf

    targets = supplier_pdf._collect_targets(before, after)
    report = {
        "requested": len(targets),
        "applied": 0,
        "replacements": 0,
        "unapplied": [],
        "font_preserved": True,
        "source_immutable": True,
        "strategy": "content_stream",
        "fallback": False,
    }
    if not targets:
        return None, report

    reader = PdfReader(BytesIO(content), strict=False)
    applied_keys: set[str] = set()

    for page_index, page in enumerate(reader.pages):
        resources = page.get("/Resources") or {}
        font_resources = resources.get("/Font") or {}
        codecs = {
            str(name): supplier_pdf._font_codec(reference.get_object())
            for name, reference in font_resources.items()
        }
        stream = ContentStream(page.get_contents(), reader)
        active_font = None
        recent_text: list[str] = []
        page_changed = False

        for operands, operator in stream.operations:
            if operator == b"Tf" and operands:
                active_font = str(operands[0])
                continue

            codec = codecs.get(active_font)
            if operator == b"TJ" and operands and isinstance(operands[0], ArrayObject):
                array = operands[0]
                visible = "".join(
                    supplier_pdf._decode_text(item, codec)
                    for item in array
                    if isinstance(item, (TextStringObject, ByteStringObject))
                )
                context = " ".join(recent_text[-12:])
                for target in targets:
                    if target.key in applied_keys:
                        continue
                    if target.page_index is not None and target.page_index != page_index:
                        continue
                    replacements = _replace_combined_text_all(array, codec, target, context, supplier_pdf)
                    if replacements:
                        applied_keys.add(target.key)
                        report["applied"] += 1
                        report["replacements"] += replacements
                        page_changed = True
                        visible = "".join(
                            supplier_pdf._decode_text(item, codec)
                            for item in array
                            if isinstance(item, (TextStringObject, ByteStringObject))
                        )
                if visible:
                    recent_text.append(visible)
                continue

            if operator not in (b"Tj", b"'", b'"') or not operands:
                continue
            item = operands[-1]
            if not isinstance(item, (TextStringObject, ByteStringObject)):
                continue

            visible = supplier_pdf._decode_text(item, codec)
            context = " ".join(recent_text[-12:]) + " " + visible
            updated = visible
            changed_targets: list[str] = []

            for target in targets:
                if target.key in applied_keys:
                    continue
                if target.page_index is not None and target.page_index != page_index:
                    continue
                if target.aliases and not any(alias.upper() in context.upper() for alias in target.aliases):
                    continue
                for variant in supplier_pdf._amount_variants(target.old):
                    pattern = re.compile(r"(?<!\d)" + re.escape(variant) + r"(?!\d)")
                    if not pattern.search(updated):
                        continue
                    candidate, replacements = pattern.subn(
                        lambda match: supplier_pdf._format_like(match.group(0), target.new),
                        updated,
                    )
                    if len(candidate) != len(updated):
                        continue
                    updated = candidate
                    changed_targets.append(target.key)
                    report["replacements"] += replacements
                    break

            if changed_targets:
                encoded = supplier_pdf._encode_text(updated, codec)
                if encoded is not None:
                    operands[-1] = encoded
                    page_changed = True
                    for key in changed_targets:
                        applied_keys.add(key)
                        report["applied"] += 1
            if updated:
                recent_text.append(updated)

        if page_changed:
            page.replace_contents(stream)

    report["unapplied"] = [target.key for target in targets if target.key not in applied_keys]
    if report["unapplied"]:
        return None, report
    return _write_reader_pages(reader), report


def _encoded_bytes(text: str, codec, supplier_pdf) -> bytes | None:
    encoded = supplier_pdf._encode_text(text, codec)
    if encoded is None:
        return None
    try:
        return bytes(encoded)
    except Exception:
        return None


def _page_codecs(page, supplier_pdf) -> list:
    resources = page.get("/Resources") or {}
    font_resources = resources.get("/Font") or {}
    codecs = []
    for reference in font_resources.values():
        try:
            codec = supplier_pdf._font_codec(reference.get_object())
        except Exception:
            codec = None
        if codec is not None:
            codecs.append(codec)
    return codecs


def _alias_patterns(target, codecs, supplier_pdf) -> list[bytes]:
    patterns: list[bytes] = []
    for alias in target.aliases:
        for codec in codecs:
            encoded = _encoded_bytes(str(alias), codec, supplier_pdf)
            if encoded and encoded not in patterns:
                patterns.append(encoded)
    return patterns


def _digit_patterns(codec, supplier_pdf) -> list[bytes]:
    patterns = []
    for digit in "0123456789":
        encoded = _encoded_bytes(digit, codec, supplier_pdf)
        if encoded:
            patterns.append(encoded)
    return patterns


def _has_digit_boundary(data: bytes, start: int, end: int, digit_patterns: list[bytes]) -> bool:
    prefix = data[:start]
    suffix = data[end:]
    if any(prefix.endswith(pattern) for pattern in digit_patterns):
        return False
    if any(suffix.startswith(pattern) for pattern in digit_patterns):
        return False
    return True


def _find_raw_replacements(data: bytes, target, codecs, supplier_pdf) -> list[tuple[int, int, bytes]]:
    """Find amount bytes without parsing PDF string syntax.

    Some client supplier PDFs contain Identity-H UTF-16BE literal strings whose
    low byte happens to equal ')' or another PDF delimiter. PDF viewers render
    them, but pypdf's ContentStream parser rejects the stream. The amount itself
    is still encoded with the page's existing font. We therefore search only for
    the font-encoded number, require a matching financial label nearby, and
    splice only those bytes. No text layer, font, coordinates, or graphics state
    is replaced.
    """

    aliases = _alias_patterns(target, codecs, supplier_pdf)
    found: dict[tuple[int, int], bytes] = {}

    for codec in codecs:
        digit_patterns = _digit_patterns(codec, supplier_pdf)
        for variant in supplier_pdf._amount_variants(target.old):
            old_bytes = _encoded_bytes(variant, codec, supplier_pdf)
            if not old_bytes:
                continue
            replacement_text = supplier_pdf._format_like(variant, target.new)
            new_bytes = _encoded_bytes(replacement_text, codec, supplier_pdf)
            if new_bytes is None:
                continue

            offset = 0
            while True:
                index = data.find(old_bytes, offset)
                if index < 0:
                    break
                end = index + len(old_bytes)
                offset = index + 1
                if not _has_digit_boundary(data, index, end, digit_patterns):
                    continue
                if aliases:
                    context = data[max(0, index - 1400): min(len(data), end + 700)]
                    if not any(alias in context for alias in aliases):
                        continue
                found[(index, end)] = new_bytes

    return [(start, end, replacement) for (start, end), replacement in sorted(found.items())]


def _patch_supplier_pdf_raw(content: bytes, before: dict, after: dict) -> tuple[bytes | None, dict]:
    """Fallback for malformed but viewer-renderable supplier content streams."""

    from pypdf import PdfReader
    from pypdf.generic import DecodedStreamObject, NameObject
    from documents import receipt_supplier_pdf_patch as supplier_pdf

    targets = supplier_pdf._collect_targets(before, after)
    report = {
        "requested": len(targets),
        "applied": 0,
        "replacements": 0,
        "unapplied": [],
        "font_preserved": True,
        "source_immutable": True,
        "strategy": "raw_stream",
        "fallback": True,
    }
    if not targets:
        return None, report

    reader = PdfReader(BytesIO(content), strict=False)
    applied_keys: set[str] = set()

    for page_index, page in enumerate(reader.pages):
        contents = page.get_contents()
        if contents is None:
            continue
        data = contents.get_data()
        codecs = _page_codecs(page, supplier_pdf)
        if not codecs:
            continue

        page_replacements: dict[tuple[int, int], bytes] = {}
        page_target_keys: set[str] = set()
        for target in targets:
            if target.key in applied_keys:
                continue
            if target.page_index is not None and target.page_index != page_index:
                continue
            replacements = _find_raw_replacements(data, target, codecs, supplier_pdf)
            if not replacements:
                continue
            for start, end, replacement in replacements:
                page_replacements[(start, end)] = replacement
            page_target_keys.add(target.key)

        if not page_replacements:
            continue

        # Apply from the end so replacements with different byte lengths cannot
        # invalidate offsets of earlier matches in the stream.
        for (start, end), replacement in sorted(page_replacements.items(), reverse=True):
            data = data[:start] + replacement + data[end:]
            report["replacements"] += 1

        stream = DecodedStreamObject()
        stream.set_data(data)
        page[NameObject("/Contents")] = stream
        applied_keys.update(page_target_keys)

    report["applied"] = len(applied_keys)
    report["unapplied"] = [target.key for target in targets if target.key not in applied_keys]
    if report["unapplied"]:
        return None, report
    return _write_reader_pages(reader), report


def patch_supplier_pdf(content: bytes, before: dict, after: dict) -> tuple[bytes | None, dict]:
    """Patch a supplier PDF, falling back to raw font-encoded stream edits."""

    structured_report = None
    structured_error = None
    try:
        corrected, structured_report = _patch_supplier_pdf_structured(content, before, after)
        if corrected is not None:
            return corrected, structured_report
    except Exception as exc:  # malformed streams are common in supplier PDFs
        structured_error = type(exc).__name__

    corrected, report = _patch_supplier_pdf_raw(content, before, after)
    if structured_error:
        report["structured_error"] = structured_error
    elif structured_report and structured_report.get("unapplied"):
        report["structured_unapplied"] = structured_report["unapplied"]
    return corrected, report


def install_receipt_supplier_pdf_writer_fix() -> None:
    from documents import receipt_supplier_pdf_patch as supplier_pdf

    if getattr(supplier_pdf.patch_supplier_pdf, "_writes_modified_pages", False):
        return
    patch_supplier_pdf._writes_modified_pages = True
    supplier_pdf.patch_supplier_pdf = patch_supplier_pdf
