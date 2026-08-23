from __future__ import annotations

import re
from decimal import Decimal
from io import BytesIO


def _layout_lines(content: bytes):
    """Return page text lines and exact character boxes for visual fallback."""

    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTChar, LTContainer, LTTextLine

    def walk(node):
        if isinstance(node, LTTextLine):
            yield node
            return
        if isinstance(node, LTContainer):
            for child in node:
                yield from walk(child)

    def walk_characters(node):
        if isinstance(node, LTChar):
            yield node
            return
        if isinstance(node, LTContainer):
            for child in node:
                yield from walk_characters(child)

    def group_characters(characters):
        """Build visual lines for PDFs that wrap all text in an LTFigure.

        Some supplier generators expose thousands of valid LTChar objects but
        no LTTextLine objects at all. Grouping by baseline keeps the same box
        based replacement path available for those PDFs.
        """

        rows = []
        for character in sorted(characters, key=lambda item: (-item.y0, item.x0)):
            row = next(
                (
                    candidate
                    for candidate in rows
                    if abs(candidate["baseline"] - character.y0)
                    <= max(1.5, character.height * 0.18)
                ),
                None,
            )
            if row is None:
                row = {"baseline": character.y0, "characters": []}
                rows.append(row)
            row["characters"].append(character)
        return rows

    pages = []
    for page_index, layout in enumerate(extract_pages(BytesIO(content))):
        lines = []
        for line in walk(layout):
            characters = [child for child in line if isinstance(child, LTChar)]
            if not characters:
                continue
            lines.append({
                "page": page_index,
                "text": line.get_text(),
                "characters": characters,
                "bbox": line.bbox,
            })
        if not lines:
            for row in group_characters(list(walk_characters(layout))):
                characters = sorted(row["characters"], key=lambda character: character.x0)
                lines.append({
                    "page": page_index,
                    "text": "".join(character.get_text() for character in characters),
                    "characters": characters,
                    "bbox": (
                        min(character.x0 for character in characters),
                        min(character.y0 for character in characters),
                        max(character.x1 for character in characters),
                        max(character.y1 for character in characters),
                    ),
                })
        pages.append(lines)
    return pages


def _token(value) -> str:
    return "".join(character for character in str(value or "").casefold() if character.isalnum())


def _set_unapplied(report: dict, targets, applied_keys: set[str]) -> bool:
    """Record missing targets and return whether a required one is missing."""

    missing = [target for target in targets if target.key not in applied_keys]
    report["unapplied"] = [target.key for target in missing if target.required]
    report["optional_unapplied"] = [target.key for target in missing if not target.required]
    report["required"] = sum(1 for target in targets if target.required)
    return bool(report["unapplied"])


def _amount_boxes(line, target, supplier_pdf):
    characters = line["characters"]
    visible = "".join(character.get_text() for character in characters)
    boxes = []
    for variant in supplier_pdf._amount_variants(target.old):
        compact_variant = variant.replace(" ", "")
        pattern = re.compile(r"(?<!\d)" + re.escape(compact_variant) + r"(?!\d)")
        for match in pattern.finditer(visible.replace(" ", "")):
            compact_visible = visible.replace(" ", "")
            if match.end() < len(compact_visible) and re.match(
                r"[.,]\d", compact_visible[match.end() : match.end() + 2]
            ):
                continue
            # Amounts contain only digits, separators and optional grouping
            # spaces.  PDFMiner usually exposes grouping spaces as LTChar; map
            # compact offsets back to the original character positions.
            compact_positions = [
                index for index, character in enumerate(visible) if character != " "
            ]
            if match.end() > len(compact_positions):
                continue
            start = compact_positions[match.start()]
            end = compact_positions[match.end() - 1]
            selected = characters[start : end + 1]
            boxes.append((
                min(character.x0 for character in selected),
                min(character.y0 for character in selected),
                max(character.x1 for character in selected),
                max(character.y1 for character in selected),
                visible[start : end + 1],
            ))
        if boxes:
            break
    return boxes


def _overlay_text_width(text: str, font_size: float) -> float:
    widths = {".": 0.28, ",": 0.28, " ": 0.28, "I": 0.28, "T": 0.61, "-": 0.33}
    return sum(widths.get(character, 0.56) for character in text) * font_size


def _pdf_literal(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _financial_differences(before: dict, after: dict, *, prefix: str = ""):
    """Return every user-visible price change, including newly entered values."""

    from documents import receipt_supplier_pdf_patch as supplier_pdf

    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}
    old_group = supplier_pdf._first_group(before)
    new_group = supplier_pdf._first_group(after)
    if old_group or new_group:
        differences = []
        for index, (old_child, new_child) in enumerate(
            zip(old_group, new_group, strict=False)
        ):
            differences.extend(
                _financial_differences(
                    old_child,
                    new_child,
                    prefix=f"{prefix}receipt[{index}].",
                )
            )
        return differences

    keys = (
        "fare",
        "taxes",
        "fees",
        "ticketCost",
        "reservedSeatCost",
        "supplierCost",
        "agencyServiceFee",
        "additionalFees",
        "markup",
        "discount",
        "commission",
        "total",
    )
    differences = []
    for key in keys:
        old = supplier_pdf._decimal(supplier_pdf._value(before, key))
        new = supplier_pdf._decimal(supplier_pdf._value(after, key))
        if new is None or old == new:
            continue
        differences.append((f"{prefix}{key}", old, new))

    old_output = supplier_pdf._value(before, "output")
    new_output = supplier_pdf._value(after, "output")
    old_mode = old_output.get("priceMode") if isinstance(old_output, dict) else None
    new_mode = new_output.get("priceMode") if isinstance(new_output, dict) else None
    if new_mode and old_mode != new_mode:
        differences.append((f"{prefix}priceMode", old_mode, new_mode))
    return differences


def _append_financial_correction_page(
    content: bytes,
    before: dict,
    after: dict,
) -> tuple[bytes | None, dict]:
    """Append an explicit correction sheet when source glyphs cannot be edited.

    Image-only PDFs and PDFs with path-rendered text do not have a safe amount
    box to replace. Publishing the unchanged file would be misleading, so the
    working copy receives a compact, machine-readable correction page. The
    supplier pages and the separately stored source version remain untouched.
    """

    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    differences = _financial_differences(before, after)
    report = {
        "requested": len(differences),
        "applied": 0,
        "replacements": 0,
        "unapplied": [],
        "optional_unapplied": [],
        "font_preserved": True,
        "source_immutable": True,
        "strategy": "financial_correction_appendix",
        "fallback": True,
    }
    if not differences:
        return None, report

    def rendered(value) -> str:
        if value is None:
            return "NOT SET"
        if isinstance(value, Decimal):
            return format(value, "f")
        return str(value).upper()

    service = str(
        after.get("service_kind")
        or after.get("service_type")
        or before.get("service_kind")
        or before.get("service_type")
        or "OTHER"
    ).upper()
    lines = [
        "CRM PRICE CORRECTION - WORKING COPY",
        "Supplier source pages are preserved without changes.",
        f"SERVICE: {service}",
        "",
        "FIELD | BEFORE | AFTER",
        *(
            f"{key} | {rendered(old)} | {rendered(new)}"
            for key, old, new in differences
        ),
    ]

    reader = PdfReader(BytesIO(content), strict=False)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    font_name = NameObject("/CRMCorrection")
    page_width = 595.28
    page_height = 841.89
    lines_per_page = 42
    for start in range(0, len(lines), lines_per_page):
        page = writer.add_blank_page(width=page_width, height=page_height)
        resources = DictionaryObject()
        resources[NameObject("/Font")] = DictionaryObject({
            font_name: DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            })
        })
        page[NameObject("/Resources")] = resources
        commands = []
        for row_index, line in enumerate(lines[start : start + lines_per_page]):
            font_size = 15 if start == 0 and row_index == 0 else 9
            y = 792 - row_index * 17
            safe_line = line.encode("ascii", errors="replace").decode("ascii")
            commands.append(
                f"BT /CRMCorrection {font_size} Tf 45 {y} Td "
                f"({_pdf_literal(safe_line)}) Tj ET\n"
            )
        stream = DecodedStreamObject()
        stream.set_data("".join(commands).encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)

    output = BytesIO()
    writer.write(output)
    report["applied"] = len(differences)
    report["replacements"] = len(differences)
    report["appended_pages"] = (len(lines) + lines_per_page - 1) // lines_per_page
    return output.getvalue(), report


def _is_financial_alias_line(target, line_text: str) -> bool:
    key = target.key.rsplit(".", 1)[-1]
    token = _token(line_text)
    if key == "ticketCost":
        return "тариф" in token or "fare" in token
    if key == "reservedSeatCost":
        return "тариф" in token or "fare" in token or "reservation" in token
    return True


def _patch_supplier_pdf_overlay(content: bytes, before: dict, after: dict) -> tuple[bytes | None, dict]:
    """Visually replace amounts when a supplier's font cannot be re-encoded.

    The fallback is deliberately all-or-nothing. PDFMiner must locate every
    requested old value next to its financial label on the intended ticket
    page. Only those exact character boxes are covered; new numeric text is
    then painted in a standard PDF font. The source bytes are never modified.
    """

    from pypdf import PdfReader
    from pypdf.generic import (
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
    )

    from documents import receipt_supplier_pdf_patch as supplier_pdf

    targets = supplier_pdf._collect_targets(before, after)
    report = {
        "requested": len(targets),
        "applied": 0,
        "replacements": 0,
        "unapplied": [],
        "font_preserved": True,
        "source_immutable": True,
        "strategy": "labeled_visual_overlay",
        "fallback": True,
    }
    if not targets:
        return None, report
    try:
        pages = _layout_lines(content)
    except Exception as exc:
        report["layout_error"] = type(exc).__name__
        _set_unapplied(report, targets, set())
        return None, report

    claimed: dict[tuple[int, float, float, float, float], tuple[str, str]] = {}
    overlays: dict[int, list[tuple[float, float, float, float, str]]] = {}
    applied_keys: set[str] = set()
    for target in targets:
        target_matches = []
        for page_index, lines in enumerate(pages):
            page_text = _token(" ".join(line["text"] for line in lines))
            if not supplier_pdf._target_matches_page(
                target,
                page_index,
                None,
                page_text,
            ):
                continue
            candidates = [
                box
                for line in lines
                for box in _amount_boxes(line, target, supplier_pdf)
            ]
            if not candidates:
                continue
            alias_lines = [
                line for line in lines
                if _is_financial_alias_line(target, line["text"])
                and any(_token(alias) and _token(alias) in _token(line["text"]) for alias in target.aliases)
            ]
            selected = []
            for alias_line in alias_lines:
                alias_y = (alias_line["bbox"][1] + alias_line["bbox"][3]) / 2
                nearby = sorted(
                    candidates,
                    key=lambda box: (
                        abs(((box[1] + box[3]) / 2) - alias_y),
                        abs(box[0] - alias_line["bbox"][2]),
                    ),
                )
                if nearby and abs(((nearby[0][1] + nearby[0][3]) / 2) - alias_y) <= 35:
                    selected.append(nearby[0])
            if not selected and len(candidates) == 1:
                selected = candidates
            for box in selected:
                if box not in target_matches:
                    target_matches.append((page_index, *box))

        new_texts = []
        conflict = False
        for page_index, x0, y0, x1, y1, template in target_matches:
            new_text = supplier_pdf._format_like(template, target.new)
            identity = (page_index, round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3))
            previous = claimed.get(identity)
            if previous and previous[1] != new_text:
                conflict = True
                break
            new_texts.append((identity, page_index, x0, y0, x1, y1, new_text))
        if conflict or not new_texts:
            continue
        for identity, page_index, x0, y0, x1, y1, new_text in new_texts:
            claimed[identity] = (target.key, new_text)
            overlay = (x0, y0, x1, y1, new_text)
            if overlay not in overlays.setdefault(page_index, []):
                overlays[page_index].append(overlay)
        applied_keys.add(target.key)

    if _set_unapplied(report, targets, applied_keys):
        return None, report

    reader = PdfReader(BytesIO(content), strict=False)
    font_name = NameObject("/CRMCorrection")
    for page_index, page_overlays in overlays.items():
        page = reader.pages[page_index]
        resources = page.get("/Resources")
        if resources is None:
            resources = DictionaryObject()
            page[NameObject("/Resources")] = resources
        else:
            resources = resources.get_object()
        fonts = resources.get("/Font")
        if fonts is None:
            fonts = DictionaryObject()
            resources[NameObject("/Font")] = fonts
        else:
            fonts = fonts.get_object()
        fonts[font_name] = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })

        commands = []
        for x0, y0, x1, y1, new_text in page_overlays:
            height = max(y1 - y0, 1)
            font_size = max(height * 0.88, 5)
            text_width = _overlay_text_width(new_text, font_size)
            text_x = x1 - text_width
            cover_x = min(x0, text_x) - 0.8
            cover_width = max(x1, text_x + text_width) - cover_x + 0.8
            commands.append(
                f"q 1 1 1 rg {cover_x:.3f} {y0 - 0.8:.3f} {cover_width:.3f} {height + 1.6:.3f} re f Q\n"
                f"BT /CRMCorrection {font_size:.3f} Tf 0 0 0 rg {text_x:.3f} {y0 + height * 0.10:.3f} Td "
                f"({_pdf_literal(new_text)}) Tj ET\n"
            )
        overlay_data = "".join(commands).encode("ascii")
        contents = page.get_contents()
        if contents is None:
            original_data = b""
        else:
            original_data = contents.get_data()
        combined_stream = DecodedStreamObject()
        # Supplier generators sometimes leave a scale/flip CTM active at the
        # end of their stream. Isolate it so PDFMiner's page coordinates map
        # directly to the correction overlay.
        combined_stream.set_data(b"q\n" + original_data + b"\nQ\n" + overlay_data)
        page[NameObject("/Contents")] = combined_stream
        report["replacements"] += len(page_overlays)

    report["applied"] = len(applied_keys)
    return _write_reader_pages(reader), report


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
        for _index, old_row, new_row in supplier_pdf._paired_breakdown_rows(old_rows, new_rows):
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
    from pypdf.generic import (
        ArrayObject,
        ByteStringObject,
        ContentStream,
        NameObject,
        TextStringObject,
    )

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
        page_text_token = None
        if any(
            target.page_markers and target.page_index is None
            for target in targets
        ):
            try:
                page_text_token = supplier_pdf._page_marker_token(page.extract_text() or "")
            except Exception:
                page_text_token = ""
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
                    if not supplier_pdf._target_matches_page(
                        target, page_index, page, page_text_token
                    ):
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
                if not supplier_pdf._target_matches_page(
                    target, page_index, page, page_text_token
                ):
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
            page[NameObject("/Contents")] = stream
            report["applied"] += len(page_target_keys - applied_keys)
            applied_keys.update(page_target_keys)

    if _set_unapplied(report, targets, applied_keys):
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
        page_text_token = None
        if any(
            target.page_markers and target.page_index is None
            for target in targets
        ):
            try:
                page_text_token = supplier_pdf._page_marker_token(page.extract_text() or "")
            except Exception:
                page_text_token = ""

        page_replacements: dict[tuple[int, int], bytes] = {}
        page_target_keys: set[str] = set()
        for target in targets:
            if target.key in applied_keys or target.key in page_target_keys:
                continue
            if not supplier_pdf._target_matches_page(
                target, page_index, page, page_text_token
            ):
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
    if _set_unapplied(report, targets, applied_keys):
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
    if corrected is None:
        overlay_corrected, overlay_report = _patch_supplier_pdf_overlay(content, before, after)
        if overlay_corrected is not None:
            if structured_error:
                overlay_report["structured_error"] = structured_error
            if report.get("unapplied"):
                overlay_report["raw_unapplied"] = report["unapplied"]
            return overlay_corrected, overlay_report
        appendix_corrected, appendix_report = _append_financial_correction_page(
            content,
            before,
            after,
        )
        if appendix_corrected is not None:
            if structured_error:
                appendix_report["structured_error"] = structured_error
            if report.get("unapplied"):
                appendix_report["raw_unapplied"] = report["unapplied"]
            if overlay_report.get("unapplied"):
                appendix_report["overlay_unapplied"] = overlay_report["unapplied"]
            return appendix_corrected, appendix_report
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
