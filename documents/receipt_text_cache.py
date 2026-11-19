"""Reuse identical extractor calls only within one receipt recognition request.

No document text is shared between requests, tenants, or files. Both extractors
still run, preserving the existing parser's quality comparison and fallbacks.
"""
from contextvars import ContextVar
from functools import wraps
from hashlib import sha256

_text_cache = ContextVar("receipt_text_cache", default=None)
_MAX_TEXT_BYTES = 8 * 1024 * 1024


def _memo(cache, key, loader):
    if key in cache["values"]:
        cache["hits"] += 1
        return cache["values"][key]
    value = loader()
    if isinstance(value, str) and cache["bytes"] + len(value.encode("utf-8")) <= _MAX_TEXT_BYTES:
        cache["values"][key] = value
        cache["bytes"] += len(value.encode("utf-8"))
    return value


def install_receipt_text_cache():
    from pdfminer import high_level
    from pypdf._page import PageObject

    from documents import services, views

    if getattr(services.extract_receipt_fields, "_request_text_cache", False):
        return
    miner = high_level.extract_text
    page_text = PageObject.extract_text
    parser = services.extract_receipt_fields

    @wraps(miner)
    def cached_miner(source, *args, **kwargs):
        cache = _text_cache.get()
        # BytesIO is what receipt parsers pass. Bypass paths, generators and
        # unusual arguments rather than changing external extractor semantics.
        if cache is None or not hasattr(source, "getvalue"):
            return miner(source, *args, **kwargs)
        try:
            fingerprint = sha256(source.getvalue()).hexdigest()
            key = ("pdfminer", fingerprint, source.tell(), repr(args), repr(sorted(kwargs.items())))
        except (AttributeError, TypeError, ValueError):
            return miner(source, *args, **kwargs)
        value = _memo(cache, key, lambda: miner(source, *args, **kwargs))
        # Preserve the position left by the extractor even on cache hits.
        end = cache["positions"].get(key)
        if end is None:
            cache["positions"][key] = source.tell()
        else:
            source.seek(end)
        return value

    @wraps(page_text)
    def cached_page(page, *args, **kwargs):
        cache = _text_cache.get()
        ref = getattr(page, "indirect_reference", None)
        source = getattr(getattr(ref, "pdf", None), "stream", None)
        # Visitor callbacks have side effects and must always execute.
        if cache is None or not ref or not hasattr(source, "getvalue") or any(callable(v) for v in (*args, *kwargs.values())):
            return page_text(page, *args, **kwargs)
        try:
            fingerprint = sha256(source.getvalue()).hexdigest()
            key = ("pypdf", fingerprint, ref.idnum, repr(args), repr(sorted(kwargs.items())))
        except (AttributeError, TypeError, ValueError):
            return page_text(page, *args, **kwargs)
        return _memo(cache, key, lambda: page_text(page, *args, **kwargs))

    @wraps(parser)
    def scoped_parser(content, *, mime="", name=""):
        if _text_cache.get() is not None:
            return parser(content, mime=mime, name=name)
        cache = {"values": {}, "positions": {}, "bytes": 0, "hits": 0}
        token = _text_cache.set(cache)
        try:
            result = parser(content, mime=mime, name=name)
            return result
        finally:
            _text_cache.reset(token)

    scoped_parser._request_text_cache = True
    high_level.extract_text = cached_miner
    PageObject.extract_text = cached_page
    services.extract_receipt_fields = scoped_parser
    views.extract_receipt_fields = scoped_parser
