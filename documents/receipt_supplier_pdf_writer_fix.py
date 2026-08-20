from __future__ import annotations

import re
from io import BytesIO


def _replace_combined_text_all(
    array,
    codec,
    target,
    context: str,
    supplier_pdf,
    *,
    allow_unlabeled: bool = False,
) -> int:
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
    if (
        target.aliases
        and not allow_unlabeled
        and not any(alias.upper() in upper_context for alias in target.aliases)
    ):
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
        for array_index, encoded in zip(positions, encoded_chunks, strict=True):
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


def _equivalent_target_keys(target, targets) -> set[str]:
    """Return CRM fields represented by the same printed numeric value.

    Supplier forms often print one amount several times (fare calculation,
    tariff and total), while CRM tracks those occurrences as separate fields.
    One safe page-scoped replacement can therefore satisfy several equal
    targets; requiring the already replaced source token a second time made the
    whole corrected PDF fail its all-or-nothing guard.
    """

    return {
        candidate.key
        for candidate in targets
        if candidate.old == target.old
        and candidate.new == target.new
        and candidate.page_index == target.page_index
        and candidate.page_markers == target.page_markers
    }


def _financial_pairs(before: dict, after: dict, supplier_pdf) -> list[tuple[object, object]]:
    pairs = []
    for key, _aliases in supplier_pdf._FINANCIAL_FIELDS:
        old = supplier_pdf._decimal(supplier_pdf._value(before, key))
        new = supplier_pdf._decimal(supplier_pdf._value(after, key))
        if old is not None and new is not None:
            pairs.append((old, new))
    for breakdown_key, _aliases in supplier_pdf._BREAKDOWNS:
        old_rows = supplier_pdf._value(before, breakdown_key)
        new_rows = supplier_pdf._value(after, breakdown_key)
        if not isinstance(old_rows, list) or not isinstance(new_rows, list):
            continue
        for old_row, new_row in zip(old_rows, new_rows, strict=False):
            if not isinstance(old_row, dict) or not isinstance(new_row, dict):
                continue
            old = supplier_pdf._decimal(old_row.get("amount"))
            new = supplier_pdf._decimal(new_row.get("amount"))
            if old is not None and new is not None:
                pairs.append((old, new))
    return pairs


def _page_wide_target_keys(before: dict, after: dict, targets, supplier_pdf) -> set[str]:
    """Allow unlabeled replacement only when every equal source field agrees.

    Example: an Aeroflot blank prints the same 6400 as fare calculation, fare
    and total. If all three fields become 6500, every occurrence is safe to
    replace. If only total becomes 6500 because a service fee was added, fare
    remains 6400 and page-wide replacement is deliberately disabled.
    """

    old_group = supplier_pdf._first_group(before)
    new_group = supplier_pdf._first_group(after)
    scope_pairs = {}
    if old_group or new_group:
        for index, (old_child, new_child) in enumerate(zip(old_group, new_group, strict=False)):
            if isinstance(old_child, dict) and isinstance(new_child, dict):
                scope_pairs[index] = _financial_pairs(old_child, new_child, supplier_pdf)
    else:
        scope_pairs[None] = _financial_pairs(before, after, supplier_pdf)

    safe = set()
    for target in targets:
        if isinstance(target.new, str):
            continue
        pairs = scope_pairs.get(target.page_index, scope_pairs.get(None, []))
        equal_source = [new for old, new in pairs if old == target.old]
        if equal_source and all(new == target.new for new in equal_source):
            safe.add(target.key)
    return safe


def _operation_text_contexts(stream, codecs, supplier_pdf) -> dict[int, str]:
    """Build a tight previous/current/next text window for each text operator."""

    from pypdf.generic import ArrayObject, ByteStringObject, TextStringObject

    active_font = None
    entries: list[tuple[int, str]] = []
    for operation_index, (operands, operator) in enumerate(stream.operations):
        if operator == b"Tf" and operands:
            active_font = str(operands[0])
            continue
        codec = codecs.get(active_font)
        visible = ""
        if operator == b"TJ" and operands and isinstance(operands[0], ArrayObject):
            visible = "".join(
                supplier_pdf._decode_text(item, codec)
                for item in operands[0]
                if isinstance(item, (TextStringObject, ByteStringObject))
            )
        elif operator in (b"Tj", b"'", b'"') and operands:
            item = operands[-1]
            if isinstance(item, (TextStringObject, ByteStringObject)):
                visible = supplier_pdf._decode_text(item, codec)
        if visible:
            entries.append((operation_index, visible))

    contexts = {}
    for position, (operation_index, _visible) in enumerate(entries):
        nearby = entries[max(0, position - 2): position + 3]
        contexts[operation_index] = " ".join(text for _index, text in nearby)
    return contexts


def _replace_text_operand(
    value,
    codec,
    target,
    context: str,
    supplier_pdf,
    *,
    allow_unlabeled: bool = False,
):
    """Replace only amount glyph bytes and preserve the rest of a Tj string.

    Some embedded CID fonts expose reversible codes for digits but not for the
    adjacent Cyrillic currency suffix. Re-encoding ``10000РУБ`` as a whole then
    fails even though replacing the five numeric glyphs is fully supported.
    Byte spans are calculated from the original font encoding, so the suffix,
    font resource and layout remain untouched.
    """

    from pypdf.generic import ByteStringObject

    visible = supplier_pdf._decode_text(value, codec)
    upper_context = (context + " " + visible).upper()
    if (
        target.aliases
        and not allow_unlabeled
        and not any(alias.upper() in upper_context for alias in target.aliases)
    ):
        return None, 0

    raw = supplier_pdf._original_bytes(value)
    spans: list[tuple[int, int]] = []
    if isinstance(codec, dict) and codec.get("kind") == "single-byte":
        spans = [(index, index + 1) for index in range(len(raw))]
    elif isinstance(codec, dict) and codec.get("kind") == "multibyte":
        encoding = codec.get("encoding")
        try:
            encoded_text = raw.decode(encoding)
            cursor = 0
            for character in encoded_text:
                chunk = character.encode(encoding)
                spans.append((cursor, cursor + len(chunk)))
                cursor += len(chunk)
        except Exception:
            spans = []
    if len(spans) != len(visible):
        return None, 0

    for variant in supplier_pdf._amount_variants(target.old):
        pattern = re.compile(r"(?<!\d)" + re.escape(variant) + r"(?!\d)")
        matches = list(pattern.finditer(visible))
        if not matches:
            continue
        updated = raw
        replacements = 0
        for match in reversed(matches):
            replacement_text = supplier_pdf._format_like(match.group(0), target.new)
            encoded = supplier_pdf._encode_text(replacement_text, codec)
            if encoded is None:
                return None, 0
            start = spans[match.start()][0]
            end = spans[match.end() - 1][1]
            updated = updated[:start] + bytes(encoded) + updated[end:]
            replacements += 1
        return ByteStringObject(updated), replacements
    return None, 0


def _patch_supplier_pdf_structured(content: bytes, before: dict, after: dict) -> tuple[bytes | None, dict]:
    """Use pypdf's parsed operators when the supplier stream is valid."""

    from pypdf import PdfReader
    from pypdf.generic import ArrayObject, ByteStringObject, ContentStream, TextStringObject

    from documents import receipt_supplier_pdf_patch as supplier_pdf

    targets = supplier_pdf._collect_targets(before, after)
    page_wide_keys = _page_wide_target_keys(before, after, targets, supplier_pdf)
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
        operation_contexts = _operation_text_contexts(stream, codecs, supplier_pdf)
        active_font = None
        page_changed = False
        page_target_keys: set[str] = set()

        for operation_index, (operands, operator) in enumerate(stream.operations):
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
                context = operation_contexts.get(operation_index, visible)
                for target in targets:
                    if target.key in applied_keys:
                        continue
                    if not supplier_pdf._target_matches_page(target, page_index, page):
                        continue
                    replacements = _replace_combined_text_all(
                        array,
                        codec,
                        target,
                        context,
                        supplier_pdf,
                        allow_unlabeled=target.key in page_wide_keys,
                    )
                    if replacements:
                        if target.key in page_wide_keys:
                            page_target_keys.update(
                                _equivalent_target_keys(target, targets) & page_wide_keys
                            )
                        else:
                            page_target_keys.add(target.key)
                        report["replacements"] += replacements
                        page_changed = True
                        visible = "".join(
                            supplier_pdf._decode_text(item, codec)
                            for item in array
                            if isinstance(item, (TextStringObject, ByteStringObject))
                        )
                continue

            if operator not in (b"Tj", b"'", b'"') or not operands:
                continue
            item = operands[-1]
            if not isinstance(item, (TextStringObject, ByteStringObject)):
                continue

            visible = supplier_pdf._decode_text(item, codec)
            context = operation_contexts.get(operation_index, visible)
            changed_targets: list[str] = []
            updated_item = item

            for target in targets:
                if target.key in applied_keys:
                    continue
                if not supplier_pdf._target_matches_page(target, page_index, page):
                    continue
                replacement_item, replacements = _replace_text_operand(
                    updated_item,
                    codec,
                    target,
                    context,
                    supplier_pdf,
                    allow_unlabeled=target.key in page_wide_keys,
                )
                if replacement_item is not None:
                    updated_item = replacement_item
                    changed_targets.append(target.key)
                    report["replacements"] += replacements

            if changed_targets:
                operands[-1] = updated_item
                page_changed = True
                for key in changed_targets:
                    target = next(candidate for candidate in targets if candidate.key == key)
                    if key in page_wide_keys:
                        page_target_keys.update(
                            _equivalent_target_keys(target, targets) & page_wide_keys
                        )
                    else:
                        page_target_keys.add(key)

        if page_changed:
            page.replace_contents(stream)
            report["applied"] += len(page_target_keys - applied_keys)
            applied_keys.update(page_target_keys)

    report["unapplied"] = [target.key for target in targets if target.key not in applied_keys]
    if report["unapplied"]:
        return None, report
    return _write_reader_pages(reader), report


def _encoded_bytes(text: str, codec, supplier_pdf) -> bytes | None:
    encoded = supplier_pdf._encode_text(text, codec)
    if encoded is not None:
        try:
            return bytes(encoded)
        except Exception:
            pass

    # Identity-H supplier PDFs often contain literal UTF-16BE strings without a
    # complete ToUnicode inverse map. Direct encoding is safe for raw matching:
    # if those bytes are not actually present we simply get no match.
    if isinstance(codec, dict) and codec.get("kind") == "multibyte":
        encoding = codec.get("encoding")
        if isinstance(encoding, str):
            try:
                return text.encode(encoding)
            except Exception:
                return None
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


def _find_raw_replacements(
    data: bytes,
    target,
    codecs,
    supplier_pdf,
    *,
    allow_unlabeled: bool = False,
) -> list[tuple[int, int, bytes]]:
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
    if target.aliases and not aliases and not allow_unlabeled:
        return []
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
                    if not any(alias in context for alias in aliases) and not allow_unlabeled:
                        continue
                found[(index, end)] = new_bytes

    return [(start, end, replacement) for (start, end), replacement in sorted(found.items())]


def _patch_supplier_pdf_raw(content: bytes, before: dict, after: dict) -> tuple[bytes | None, dict]:
    """Fallback for malformed but viewer-renderable supplier content streams."""

    from pypdf import PdfReader
    from pypdf.generic import DecodedStreamObject, NameObject

    from documents import receipt_supplier_pdf_patch as supplier_pdf

    targets = supplier_pdf._collect_targets(before, after)
    page_wide_keys = _page_wide_target_keys(before, after, targets, supplier_pdf)
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
            if target.key in applied_keys or target.key in page_target_keys:
                continue
            if not supplier_pdf._target_matches_page(target, page_index, page):
                continue
            replacements = _find_raw_replacements(
                data,
                target,
                codecs,
                supplier_pdf,
                allow_unlabeled=target.key in page_wide_keys,
            )
            if not replacements:
                continue
            for start, end, replacement in replacements:
                page_replacements[(start, end)] = replacement
            if target.key in page_wide_keys:
                page_target_keys.update(_equivalent_target_keys(target, targets) & page_wide_keys)
            else:
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
