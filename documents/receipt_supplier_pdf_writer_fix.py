from __future__ import annotations

from io import BytesIO
import re


def patch_supplier_pdf(content: bytes, before: dict, after: dict) -> tuple[bytes | None, dict]:
    """Patch financial text operators and write the already modified pages.

    PdfWriter.clone_document_from_reader() re-clones the reader's original object
    graph and can therefore resurrect the pre-edit content stream.  Adding the
    modified PageObjects writes the changed stream while preserving the page's
    original resources (including the exact embedded font objects).
    """

    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, ByteStringObject, ContentStream, TextStringObject
    from documents import receipt_supplier_pdf_patch as supplier_pdf

    targets = supplier_pdf._collect_targets(before, after)
    report = {
        "requested": len(targets),
        "applied": 0,
        "unapplied": [],
        "font_preserved": True,
        "source_immutable": True,
    }
    if not targets:
        return None, report

    reader = PdfReader(BytesIO(content))
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
                    if supplier_pdf._replace_combined_text(array, codec, target, context):
                        applied_keys.add(target.key)
                        report["applied"] += 1
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
                    match = re.search(r"(?<!\d)" + re.escape(variant) + r"(?!\d)", updated)
                    if not match:
                        continue
                    replacement = supplier_pdf._format_like(match.group(0), target.new)
                    updated = updated[: match.start()] + replacement + updated[match.end() :]
                    changed_targets.append(target.key)
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
    return output.getvalue(), report


def install_receipt_supplier_pdf_writer_fix() -> None:
    from documents import receipt_supplier_pdf_patch as supplier_pdf

    if getattr(supplier_pdf.patch_supplier_pdf, "_writes_modified_pages", False):
        return
    patch_supplier_pdf._writes_modified_pages = True
    supplier_pdf.patch_supplier_pdf = patch_supplier_pdf
