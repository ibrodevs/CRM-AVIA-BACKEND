from __future__ import annotations

import re
from collections import Counter, defaultdict
from decimal import Decimal
from io import BytesIO
from statistics import median


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
        pattern = supplier_pdf._target_amount_pattern(compact_variant, target)
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
            font_name = Counter(character.fontname for character in selected).most_common(1)[0][0]
            font_characters = [character for character in selected if character.fontname == font_name]
            boxes.append({
                "x0": min(character.x0 for character in selected),
                "y0": min(character.y0 for character in selected),
                "x1": max(character.x1 for character in selected),
                "y1": max(character.y1 for character in selected),
                "template": visible[start : end + 1],
                "font_name": font_name,
                "font_size": median(character.size for character in font_characters),
                "baseline": median(character.matrix[5] for character in font_characters),
            })
        if boxes:
            break
    return boxes


def _layout_font_widths(lines):
    """Return observed glyph advances relative to size for embedded fonts."""

    observed = defaultdict(lambda: defaultdict(list))
    for line in lines:
        for character in line["characters"]:
            size = float(character.size or 0)
            advance = float(getattr(character, "adv", 0) or 0)
            if size > 0 and advance >= 0:
                observed[character.fontname][character.get_text()].append(advance / size)
    return {
        font_name: {
            character: median(ratios)
            for character, ratios in characters.items()
            if ratios
        }
        for font_name, characters in observed.items()
    }


def _overlay_text_width(text: str, font_size: float, font_name: str = "", font_widths=None) -> float:
    measured = (font_widths or {}).get(font_name, {})
    digit_widths = [width for character, width in measured.items() if character.isdigit()]
    fallback_digit = median(digit_widths) if digit_widths else 0.56
    widths = {".": 0.28, ",": 0.28, " ": 0.28, "I": 0.28, "T": 0.61, "-": 0.33}
    return sum(
        measured.get(character, fallback_digit if character.isdigit() else widths.get(character, 0.56))
        for character in text
    ) * font_size


def _font_resource_name(fonts, layout_font_name: str):
    """Find the page resource that PDFMiner identified for an amount."""

    wanted = str(layout_font_name or "").lstrip("/")
    for resource_name, reference in fonts.items():
        try:
            base_font = str(reference.get_object().get("/BaseFont") or "").lstrip("/")
        except Exception:
            continue
        if base_font == wanted:
            return str(resource_name)
        if "+" in base_font and "+" in wanted and base_font.split("+", 1)[1] == wanted.split("+", 1)[1]:
            return str(resource_name)
    return None


def _overlapping_amount_boxes(primary, candidates):
    """Keep co-located duplicate glyph runs used by suppliers for bold text."""

    selected = [primary]
    primary_width = max(primary["x1"] - primary["x0"], 0.01)
    for candidate in candidates:
        if candidate is primary:
            continue
        vertical_gap = abs(
            ((candidate["y0"] + candidate["y1"]) / 2)
            - ((primary["y0"] + primary["y1"]) / 2)
        )
        overlap = max(0, min(primary["x1"], candidate["x1"]) - max(primary["x0"], candidate["x0"]))
        candidate_width = max(candidate["x1"] - candidate["x0"], 0.01)
        if vertical_gap <= 0.75 and overlap / min(primary_width, candidate_width) >= 0.9:
            selected.append(candidate)
    return selected


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
    overlays: dict[int, list[dict]] = {}
    page_font_widths = {
        page_index: _layout_font_widths(lines)
        for page_index, lines in enumerate(pages)
    }
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
                        abs(((box["y0"] + box["y1"]) / 2) - alias_y),
                        abs(box["x0"] - alias_line["bbox"][2]),
                    ),
                )
                if nearby and abs(((nearby[0]["y0"] + nearby[0]["y1"]) / 2) - alias_y) <= 35:
                    for box in _overlapping_amount_boxes(nearby[0], nearby):
                        selected.append({
                            **box,
                            "field_x0": min(box["x0"], alias_line["bbox"][2] + 2),
                        })
            if not selected and len(candidates) == 1:
                selected = [{**candidates[0], "field_x0": 0}]
            for box in selected:
                if box not in target_matches:
                    target_matches.append({"page_index": page_index, **box})

        new_texts = []
        conflict = False
        for match in target_matches:
            page_index = match["page_index"]
            new_text = supplier_pdf._format_like(match["template"], target.new)
            identity = (
                page_index,
                round(match["x0"], 3),
                round(match["y0"], 3),
                round(match["x1"], 3),
                round(match["y1"], 3),
            )
            previous = claimed.get(identity)
            if previous and previous[1] != new_text:
                conflict = True
                break
            new_texts.append((identity, {**match, "text": new_text}))
        if conflict or not new_texts:
            continue
        for identity, overlay in new_texts:
            claimed[identity] = (target.key, overlay["text"])
            page_overlays = overlays.setdefault(overlay["page_index"], [])
            if overlay not in page_overlays:
                page_overlays.append(overlay)
        applied_keys.add(target.key)

    if _set_unapplied(report, targets, applied_keys):
        return None, report

    reader = PdfReader(BytesIO(content), strict=False)
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
        fallback_font_name = NameObject("/CRMCorrection")
        cover_commands = []
        text_commands = []
        page_width = float(page.mediabox.width)
        page_height = float(page.mediabox.height)
        for overlay in page_overlays:
            x0, y0, x1, y1 = (overlay[key] for key in ("x0", "y0", "x1", "y1"))
            new_text = overlay["text"]
            font_size = max(float(overlay.get("font_size") or 0), 1)
            baseline = float(overlay.get("baseline") or (y0 + font_size * 0.2))
            resource_name = _font_resource_name(fonts, overlay.get("font_name"))
            encoded = None
            if resource_name:
                font = fonts[NameObject(resource_name)].get_object()
                encoded = supplier_pdf._encode_text(new_text, supplier_pdf._font_codec(font))
            if encoded is None:
                if fallback_font_name not in fonts:
                    fonts[fallback_font_name] = DictionaryObject({
                        NameObject("/Type"): NameObject("/Font"),
                        NameObject("/Subtype"): NameObject("/Type1"),
                        NameObject("/BaseFont"): NameObject("/Helvetica"),
                    })
                resource_name = str(fallback_font_name)
                report["font_preserved"] = False

            text_width = _overlay_text_width(
                new_text,
                font_size,
                overlay.get("font_name", ""),
                page_font_widths.get(page_index),
            )
            field_x0 = max(float(overlay.get("field_x0") or 0), 0)
            available_width = max(x1 - field_x0, 1)
            if text_width > available_width:
                font_size *= available_width / text_width
                text_width = _overlay_text_width(
                    new_text,
                    font_size,
                    overlay.get("font_name", ""),
                    page_font_widths.get(page_index),
                )
            text_x = max(field_x0, min(x1 - text_width, page_width - text_width))
            # Mask the precise glyph area only. The old +/-0.8pt vertical pad
            # crossed into adjacent six-point rows and visibly clipped them.
            cover_x0 = max(0, min(x0, text_x) - 0.05)
            cover_y0 = max(0, y0 - 0.05)
            cover_x1 = min(page_width, max(x1, text_x + text_width) + 0.05)
            cover_y1 = min(page_height, y1 + 0.05)
            cover_commands.append(
                f"q 1 1 1 rg {cover_x0:.3f} {cover_y0:.3f} "
                f"{cover_x1 - cover_x0:.3f} {cover_y1 - cover_y0:.3f} re f Q\n"
            )
            if encoded is not None:
                operand = f"<{bytes(encoded).hex().upper()}>"
            else:
                operand = f"({_pdf_literal(new_text)})"
            text_commands.append(
                f"BT {resource_name} {font_size:.3f} Tf 0 0 0 rg "
                f"{text_x:.3f} {baseline:.3f} Td {operand} Tj ET\n"
            )
        # Clear every original duplicate before drawing any replacement. Some
        # supplier forms paint the same total twice with a tiny offset to make
        # it bold; interleaving cover/draw erased one of the new layers.
        overlay_data = "".join(cover_commands + text_commands).encode("ascii")
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

    from pypdf.generic import ByteStringObject, FloatObject, TextStringObject

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
        pattern = supplier_pdf._target_amount_pattern(variant, target)
        matches = list(pattern.finditer(combined))
        if not matches:
            continue
        updated = pattern.sub(lambda match: supplier_pdf._target_replacement(match.group(0), target), combined)
        if len(updated) != len(combined):
            return 0

        # Some RZD generators create bold totals by painting the same amount
        # twice and jumping backwards inside one TJ array. The jump was
        # calculated for the original glyph widths. When different digits have
        # different widths, keeping that old jump makes the two new copies drift
        # apart even though both strings were replaced correctly.
        chunk_ranges = []
        chunk_offset = 0
        for array_index, chunk in zip(positions, chunks, strict=True):
            chunk_ranges.append((chunk_offset, chunk_offset + len(chunk), array_index))
            chunk_offset += len(chunk)

        widths = codec.get("widths") if isinstance(codec, dict) else None
        default_width = codec.get("default_width", 1000) if isinstance(codec, dict) else 1000
        if widths and len(matches) > 1:
            for current, following in zip(matches, matches[1:], strict=False):
                current_end = next(
                    (
                        array_index
                        for start, end, array_index in chunk_ranges
                        if start <= current.end() - 1 < end
                    ),
                    None,
                )
                following_start = next(
                    (
                        array_index
                        for start, end, array_index in chunk_ranges
                        if start <= following.start() < end
                    ),
                    None,
                )
                if current_end is None or following_start is None:
                    continue
                numeric_items = []
                for array_index in range(current_end + 1, following_start):
                    item = array[array_index]
                    if isinstance(item, (TextStringObject, ByteStringObject)):
                        continue
                    try:
                        numeric_items.append((array_index, float(item)))
                    except (TypeError, ValueError):
                        continue
                if not numeric_items:
                    continue
                reset_index, reset_value = max(numeric_items, key=lambda pair: abs(pair[1]))
                if abs(reset_value) < 1000:
                    continue
                replacement_text = supplier_pdf._target_replacement(current.group(0), target)
                old_width = sum(float(widths.get(char, default_width)) for char in current.group(0))
                new_width = sum(float(widths.get(char, default_width)) for char in replacement_text)
                array[reset_index] = FloatObject(reset_value + new_width - old_width)

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
        # ``page_index`` is the physical PDF page. A grouped receipt can span
        # multiple pages (for example tickets on pages 0 and 2), so it is not
        # interchangeable with the child index used by ``scope_pairs``.
        child_match = re.match(r"receipt\[(\d+)\]\.", target.key)
        scope_index = int(child_match.group(1)) if child_match else None
        pairs = scope_pairs.get(scope_index, scope_pairs.get(None, []))
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
        pattern = supplier_pdf._target_amount_pattern(variant, target)
        matches = list(pattern.finditer(visible))
        if not matches:
            continue
        updated = raw
        replacements = 0
        for match in reversed(matches):
            replacement_text = supplier_pdf._target_replacement(match.group(0), target)
            encoded = supplier_pdf._encode_text(replacement_text, codec)
            if encoded is None:
                return None, 0
            start = spans[match.start()][0]
            end = spans[match.end() - 1][1]
            updated = updated[:start] + bytes(encoded) + updated[end:]
            replacements += 1
        return ByteStringObject(updated), replacements
    return None, 0


def _replace_operand_characters(value, codec, replacements: dict[int, str], supplier_pdf):
    """Replace selected characters without rebuilding an entire PDF string.

    A number of railway and voucher generators emit every visible glyph in a
    separate ``Tj`` operation.  Re-encoding the whole operand is unnecessary
    and can fail when the adjacent currency suffix is absent from ToUnicode.
    Updating only the known character byte spans preserves the supplier font,
    text matrix, kerning and baseline exactly.
    """

    from pypdf.generic import ByteStringObject

    if not replacements:
        return value
    raw = supplier_pdf._original_bytes(value)
    visible = supplier_pdf._decode_text(value, codec)
    spans: list[tuple[int, int]] = []
    if isinstance(codec, dict) and codec.get("kind") == "single-byte":
        spans = [(index, index + 1) for index in range(len(raw))]
    elif isinstance(codec, dict) and codec.get("kind") == "multibyte":
        encoding = codec.get("encoding")
        try:
            decoded = raw.decode(encoding)
            cursor = 0
            for character in decoded:
                chunk = character.encode(encoding)
                spans.append((cursor, cursor + len(chunk)))
                cursor += len(chunk)
        except Exception:
            spans = []
    if len(spans) != len(visible):
        return None

    updated = raw
    for character_index, replacement in sorted(replacements.items(), reverse=True):
        if character_index < 0 or character_index >= len(spans):
            return None
        encoded = supplier_pdf._encode_text(replacement, codec)
        if encoded is None:
            return None
        start, end = spans[character_index]
        if len(encoded) != end - start:
            return None
        updated = updated[:start] + bytes(encoded) + updated[end:]
    return ByteStringObject(updated)


def _fragmented_text_entries(stream, codecs, supplier_pdf):
    """Return one searchable string plus exact operand/character mappings."""

    from pypdf.generic import ArrayObject, ByteStringObject, TextStringObject

    active_font = None
    active_font_size = None
    combined: list[str] = []
    entries: list[dict] = []
    offset = 0
    for operation_index, (operands, operator) in enumerate(stream.operations):
        if operator == b"Tf" and operands:
            active_font = str(operands[0])
            active_font_size = operands[1] if len(operands) > 1 else None
            continue
        values = []
        if operator == b"TJ" and operands and isinstance(operands[0], ArrayObject):
            values = [
                (array_index, value)
                for array_index, value in enumerate(operands[0])
                if isinstance(value, (TextStringObject, ByteStringObject))
            ]
        elif operator in (b"Tj", b"'", b'"') and operands:
            value = operands[-1]
            if isinstance(value, (TextStringObject, ByteStringObject)):
                values = [(None, value)]
        codec = codecs.get(active_font)
        for array_index, value in values:
            visible = supplier_pdf._decode_text(value, codec)
            if not visible:
                continue
            entries.append({
                "operation_index": operation_index,
                "array_index": array_index,
                "value": value,
                "codec": codec,
                "font_name": active_font,
                "font_size": active_font_size,
                "start": offset,
                "end": offset + len(visible),
                "visible": visible,
            })
            combined.append(visible)
            offset += len(visible)
    return "".join(combined), entries


def _replace_fragmented_text_all(
    stream,
    codecs,
    target,
    supplier_pdf,
    *,
    allow_unlabeled: bool = False,
) -> int:
    """Replace amounts split into one-glyph text operations in place.

    The replacement must keep the supplier's printed character count.  This is
    normally guaranteed by ``_format_like`` (grouping and decimal precision are
    copied from the source).  Keeping one glyph per original slot is what makes
    the result stable in Acrobat/Infix: there is no overlay, no new font and no
    recalculated baseline.
    """

    combined, entries = _fragmented_text_entries(stream, codecs, supplier_pdf)
    if not combined or not entries:
        return 0

    selected_matches: list[tuple[int, int, str]] = []
    for variant in supplier_pdf._amount_variants(target.old):
        pattern = supplier_pdf._target_amount_pattern(variant, target)
        for match in pattern.finditer(combined):
            replacement = supplier_pdf._target_replacement(match.group(0), target)
            if len(replacement) != len(match.group(0)):
                continue
            context = combined[max(0, match.start() - 120): match.end() + 120].upper()
            if (
                target.aliases
                and not allow_unlabeled
                and not any(alias.upper() in context for alias in target.aliases)
            ):
                continue
            selected_matches.append((match.start(), match.end(), replacement))
        if selected_matches:
            break
    if not selected_matches:
        return 0

    entry_replacements: dict[int, dict[int, str]] = defaultdict(dict)
    preferred_fonts: dict[int, str] = {}

    def font_family(font_name: str | None) -> str:
        codec = codecs.get(font_name) or {}
        base_font = str(codec.get("base_font") or "").lstrip("/")
        base_font = re.sub(r"^[A-Z]{6}\+", "", base_font)
        # ArialMT and Arial are the same regular face. Keep style markers such
        # as Bold/Italic so genuinely different faces are never unified.
        return re.sub(r"MT$", "", base_font, flags=re.IGNORECASE).casefold()

    def font_style(font_name: str | None) -> str:
        family = font_family(font_name)
        if "bold" in family:
            return "bold"
        if "italic" in family or "oblique" in family:
            return "italic"
        if "medium" in family:
            return "medium"
        return "regular"

    observed_by_font: dict[str | None, set[str]] = defaultdict(set)
    for entry in entries:
        observed_by_font[entry["font_name"]].update(entry["visible"])

    for start, end, replacement in selected_matches:
        match_entry_indices: list[int] = []
        for combined_index, new_character in zip(range(start, end), replacement, strict=True):
            for entry_index, entry in enumerate(entries):
                if entry["start"] <= combined_index < entry["end"]:
                    entry_replacements[entry_index][combined_index - entry["start"]] = new_character
                    if entry_index not in match_entry_indices:
                        match_entry_indices.append(entry_index)
                    break
        match_fonts = [entries[index]["font_name"] for index in match_entry_indices]
        distinct_fonts = {font for font in match_fonts if font is not None}
        families = {font_family(font) for font in distinct_fonts}
        if distinct_fonts and len(families) == 1 and "" not in families:
            family = next(iter(families))
            compatible_fonts = [
                font
                for font in sorted(codecs)
                if font_family(font) == family
                if supplier_pdf._encode_text(replacement, codecs.get(font)) is not None
            ]
            if not compatible_fonts:
                target_style = font_style(match_fonts[0])
                compatible_fonts = [
                    font
                    for font in sorted(codecs)
                    if set(replacement).issubset(observed_by_font[font])
                    and supplier_pdf._encode_text(replacement, codecs.get(font)) is not None
                ]
                compatible_fonts.sort(
                    key=lambda font: (
                        font_style(font) == target_style,
                        len(observed_by_font[font]),
                    ),
                    reverse=True,
                )
            if compatible_fonts:
                # Prefer the broadest embedded subset. A narrow subset can
                # advertise Identity-H while physically omitting new digits,
                # which renders them as squares despite correct extraction.
                same_family_fonts = [
                    font for font in compatible_fonts if font_family(font) == family
                ]
                canonical_font = (
                    max(
                        same_family_fonts,
                        key=lambda font: len((codecs.get(font) or {}).get("inverse") or {}),
                    )
                    if same_family_fonts
                    else compatible_fonts[0]
                )
                for entry_index in match_entry_indices:
                    preferred_fonts[entry_index] = canonical_font

    updated_values = {}
    font_switches: dict[int, tuple[str, object, str, object]] = {}
    for entry_index, replacements in entry_replacements.items():
        entry = entries[entry_index]
        desired = "".join(
            replacements.get(index, character)
            for index, character in enumerate(entry["visible"])
        )
        target_font = entry["font_name"]
        target_codec = entry["codec"]
        preferred_font = preferred_fonts.get(entry_index)
        if preferred_font is not None and preferred_font != target_font:
            # A local font switch is safe only for a standalone text operand;
            # otherwise unchanged TJ chunks would still use the old encoding.
            same_operation = [
                (candidate_index, candidate)
                for candidate_index, candidate in enumerate(entries)
                if candidate["operation_index"] == entry["operation_index"]
            ]
            operation_fully_targeted = all(
                candidate_index in entry_replacements
                and preferred_fonts.get(candidate_index) == preferred_font
                for candidate_index, _candidate in same_operation
            )
            if len(same_operation) != 1 and not operation_fully_targeted:
                return 0
            target_font = preferred_font
            target_codec = codecs[target_font]
            encoded = supplier_pdf._encode_text(desired, target_codec)
            if encoded is None:
                return 0
            from pypdf.generic import ByteStringObject

            updated = ByteStringObject(bytes(encoded))
            font_switches[entry["operation_index"]] = (
                target_font,
                entry["font_size"],
                entry["font_name"],
                entry["font_size"],
            )
        else:
            updated = _replace_operand_characters(
                entry["value"], target_codec, replacements, supplier_pdf
            )
        if updated is None:
            return 0
        updated_values[entry_index] = updated

    for entry_index, updated in updated_values.items():
        entry = entries[entry_index]
        operands, _operator = stream.operations[entry["operation_index"]]
        if entry["array_index"] is None:
            operands[-1] = updated
        else:
            operands[0][entry["array_index"]] = updated
    if font_switches:
        from pypdf.generic import FloatObject, NameObject

        for operation_index, switch in sorted(font_switches.items(), reverse=True):
            target_font, target_size, original_font, original_size = switch
            size = FloatObject(float(target_size or original_size or 1))
            stream.operations.insert(
                operation_index,
                ([NameObject(target_font), size], b"Tf"),
            )
            stream.operations.insert(
                operation_index + 2,
                ([NameObject(original_font), FloatObject(float(original_size or size))], b"Tf"),
            )
    return len(selected_matches)


def _replace_targets_in_nested_stream(
    stream,
    codecs,
    targets,
    page_index,
    page,
    page_text_token,
    page_wide_keys,
    applied_keys,
    supplier_pdf,
):
    """Apply the structured replacement rules inside a Form XObject."""

    from pypdf.generic import ArrayObject, ByteStringObject, TextStringObject

    operation_contexts = _operation_text_contexts(stream, codecs, supplier_pdf)
    active_font = None
    changed = False
    target_keys: set[str] = set()
    replacements_total = 0

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
                if target.key in applied_keys or target.key in target_keys:
                    continue
                if not supplier_pdf._target_matches_page(
                    target, page_index, page, page_text_token
                ):
                    continue
                replacements = 0
                for array_index, value in enumerate(list(array)):
                    if not isinstance(value, (TextStringObject, ByteStringObject)):
                        continue
                    replacement_item, item_replacements = _replace_text_operand(
                        value,
                        codec,
                        target,
                        context,
                        supplier_pdf,
                        allow_unlabeled=target.key in page_wide_keys,
                    )
                    if replacement_item is not None:
                        array[array_index] = replacement_item
                        replacements += item_replacements
                if not replacements:
                    replacements = _replace_combined_text_all(
                        array,
                        codec,
                        target,
                        context,
                        supplier_pdf,
                        allow_unlabeled=target.key in page_wide_keys,
                    )
                if replacements:
                    replacements_total += replacements
                    changed = True
                    if target.key in page_wide_keys:
                        target_keys.update(
                            _equivalent_target_keys(target, targets) & page_wide_keys
                        )
                    else:
                        target_keys.add(target.key)
            continue

        if operator not in (b"Tj", b"'", b'"') or not operands:
            continue
        item = operands[-1]
        if not isinstance(item, (TextStringObject, ByteStringObject)):
            continue
        visible = supplier_pdf._decode_text(item, codec)
        context = operation_contexts.get(operation_index, visible)
        updated_item = item
        changed_targets = []
        for target in targets:
            if target.key in applied_keys or target.key in target_keys:
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
                changed_targets.append(target)
                replacements_total += replacements
        if changed_targets:
            operands[-1] = updated_item
            changed = True
            for target in changed_targets:
                if target.key in page_wide_keys:
                    target_keys.update(
                        _equivalent_target_keys(target, targets) & page_wide_keys
                    )
                else:
                    target_keys.add(target.key)

    for target in targets:
        if target.key in applied_keys or target.key in target_keys:
            continue
        if not supplier_pdf._target_matches_page(
            target, page_index, page, page_text_token
        ):
            continue
        replacements = _replace_fragmented_text_all(
            stream,
            codecs,
            target,
            supplier_pdf,
            allow_unlabeled=target.key in page_wide_keys,
        )
        if not replacements:
            continue
        replacements_total += replacements
        changed = True
        if target.key in page_wide_keys:
            target_keys.update(
                _equivalent_target_keys(target, targets) & page_wide_keys
            )
        else:
            target_keys.add(target.key)
    return changed, target_keys, replacements_total


def _clone_form_stream(form):
    """Clone a Form and its resource dictionaries before page-local edits."""

    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    clone = DecodedStreamObject()
    for key, value in form.items():
        if str(key) not in {"/Length", "/Filter", "/DecodeParms", "/Resources"}:
            clone[key] = value
    resources = form.get("/Resources")
    if resources is not None:
        resources = resources.get_object()
        cloned_resources = DictionaryObject()
        for key, value in resources.items():
            if str(key) == "/XObject":
                cloned_resources[NameObject("/XObject")] = DictionaryObject({
                    nested_name: nested_reference
                    for nested_name, nested_reference in value.get_object().items()
                })
            else:
                cloned_resources[key] = value
        clone[NameObject("/Resources")] = cloned_resources
    clone.set_data(form.get_data())
    return clone


def _patch_page_form_xobjects(
    resources,
    reader,
    targets,
    page_index,
    page,
    page_text_token,
    page_wide_keys,
    applied_keys,
    supplier_pdf,
):
    """Patch page-local clones of nested Form XObjects recursively."""

    from pypdf.generic import ContentStream, NameObject

    xobject_resources = resources.get("/XObject") if resources else None
    if xobject_resources is None:
        return False, set(), 0
    xobjects = xobject_resources.get_object()
    changed_any = False
    all_keys: set[str] = set()
    replacements_total = 0

    for resource_name, reference in list(xobjects.items()):
        original = reference.get_object()
        if str(original.get("/Subtype")) != "/Form":
            continue
        form = _clone_form_stream(original)
        form_resources = form.get("/Resources") or resources
        form_resources = form_resources.get_object()
        fonts = form_resources.get("/Font") or {}
        fonts = fonts.get_object()
        codecs = {
            str(name): supplier_pdf._font_codec(font_reference.get_object())
            for name, font_reference in fonts.items()
        }
        stream = ContentStream(form, reader)
        changed, keys, replacements = _replace_targets_in_nested_stream(
            stream,
            codecs,
            targets,
            page_index,
            page,
            page_text_token,
            page_wide_keys,
            applied_keys | all_keys,
            supplier_pdf,
        )
        nested_changed, nested_keys, nested_replacements = _patch_page_form_xobjects(
            form_resources,
            reader,
            targets,
            page_index,
            page,
            page_text_token,
            page_wide_keys,
            applied_keys | all_keys | keys,
            supplier_pdf,
        )
        if not changed and not nested_changed:
            continue
        if changed:
            form.set_data(stream.get_data())
        xobjects[NameObject(str(resource_name))] = form
        changed_any = True
        all_keys.update(keys)
        all_keys.update(nested_keys)
        replacements_total += replacements + nested_replacements
    return changed_any, all_keys, replacements_total


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
                    replacements = 0
                    # Prefer replacing a complete amount inside its existing
                    # string item. Unlike the combined TJ fallback this safely
                    # supports a different byte/character length while keeping
                    # the original text matrix and font resource.
                    for array_index, value in enumerate(list(array)):
                        if not isinstance(value, (TextStringObject, ByteStringObject)):
                            continue
                        replacement_item, item_replacements = _replace_text_operand(
                            value,
                            codec,
                            target,
                            context,
                            supplier_pdf,
                            allow_unlabeled=target.key in page_wide_keys,
                        )
                        if replacement_item is not None:
                            array[array_index] = replacement_item
                            replacements += item_replacements
                    if not replacements:
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

        # Some wkhtmltopdf/Ghostscript supplier files draw every amount glyph
        # using a separate Tj plus an absolute text position. The per-operator
        # loop above cannot see a complete number in those files. Patch the
        # remaining page-scoped targets character-for-character so every
        # original glyph slot, baseline and embedded font stays untouched.
        for target in targets:
            if target.key in applied_keys or target.key in page_target_keys:
                continue
            if not supplier_pdf._target_matches_page(
                target, page_index, page, page_text_token
            ):
                continue
            replacements = _replace_fragmented_text_all(
                stream,
                codecs,
                target,
                supplier_pdf,
                allow_unlabeled=target.key in page_wide_keys,
            )
            if not replacements:
                continue
            report["replacements"] += replacements
            page_changed = True
            if target.key in page_wide_keys:
                page_target_keys.update(
                    _equivalent_target_keys(target, targets) & page_wide_keys
                )
            else:
                page_target_keys.add(target.key)

        form_changed, form_target_keys, form_replacements = _patch_page_form_xobjects(
            resources,
            reader,
            targets,
            page_index,
            page,
            page_text_token,
            page_wide_keys,
            applied_keys | page_target_keys,
            supplier_pdf,
        )
        if form_changed:
            page_changed = True
            page_target_keys.update(form_target_keys)
            report["replacements"] += form_replacements

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


def _non_overlapping_raw_replacements(
    replacements: list[tuple[int, int, bytes]],
) -> list[tuple[int, int, bytes]]:
    """Keep the most specific byte match when currency and amount overlap.

    Closing an IT fare produces candidates for both ``RUB17205`` and its
    nested ``17205``. Applying both with offsets from the original stream makes
    the shorter match delete bytes after the fare. Prefer the widest match and
    only then keep disjoint occurrences elsewhere on the page.
    """

    selected: list[tuple[int, int, bytes]] = []
    for candidate in sorted(
        replacements,
        key=lambda item: (-(item[1] - item[0]), item[0], item[1]),
    ):
        start, end, _replacement = candidate
        if any(start < chosen_end and chosen_start < end for chosen_start, chosen_end, _ in selected):
            continue
        selected.append(candidate)
    return sorted(selected, key=lambda item: (item[0], item[1]))


def _strip_null_whitespace_outside_literals(data: bytes) -> tuple[bytes, int]:
    """Normalize CPDF's UTF-16 spacing without touching encoded text bytes.

    dompdf/CPDF sometimes emits NUL whitespace between operands in a TJ array.
    PDF viewers tolerate it, but after a stream edit their recovery can stop at
    the fare row and hide the rest of the page. NUL is only removed outside
    literal strings; embedded Identity-H glyph data remains byte-for-byte
    intact.
    """

    output = bytearray()
    depth = 0
    escaped = False
    removed = 0
    for value in data:
        if depth:
            output.append(value)
            if escaped:
                escaped = False
            elif value == 0x5C:  # backslash
                escaped = True
            elif value == 0x28:  # (
                depth += 1
            elif value == 0x29:  # )
                depth -= 1
            continue
        if value == 0:
            removed += 1
            continue
        output.append(value)
        if value == 0x28:
            depth = 1
    return bytes(output), removed


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
            for source_text, replacement_text in supplier_pdf._target_source_variants(variant, target):
                old_bytes = _encoded_bytes(source_text, codec, supplier_pdf)
                if not old_bytes:
                    continue
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

    return _non_overlapping_raw_replacements([
        (start, end, replacement)
        for (start, end), replacement in found.items()
    ])


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
        "stream_repairs": 0,
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

        # CPDF's NUL separators are harmless in the immutable source, but can
        # make viewers stop after the edited fare row once byte lengths change.
        # Normalize only pages using a multibyte font and only outside strings.
        if any(
            isinstance(codec, dict) and codec.get("kind") == "multibyte"
            for codec in codecs
        ):
            data, repairs = _strip_null_whitespace_outside_literals(data)
            report["stream_repairs"] += repairs

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
