from io import BytesIO
from types import SimpleNamespace

import pytest
from pdfminer import high_level
from pypdf._page import PageObject

from documents import receipt_text_cache as cache_module


def test_extractors_reuse_work_only_within_one_import(monkeypatch):
    from documents import services, views

    calls = []

    def miner(stream, **kwargs):
        calls.append(('miner', stream.getvalue()))
        stream.seek(2)
        return stream.getvalue().decode()

    def page_text(page, **kwargs):
        calls.append(('page', page.indirect_reference.pdf.stream.getvalue()))
        return 'page'

    def parser(content, **kwargs):
        for _ in range(2):
            stream = BytesIO(content)
            assert high_level.extract_text(stream) == content.decode()
            assert stream.tell() == 2
            page = SimpleNamespace(indirect_reference=SimpleNamespace(
                idnum=1, pdf=SimpleNamespace(stream=BytesIO(content))))
            assert PageObject.extract_text(page) == 'page'
        return {'status': 'parsed'}

    monkeypatch.setattr(high_level, 'extract_text', miner)
    monkeypatch.setattr(PageObject, 'extract_text', page_text)
    monkeypatch.setattr(services, 'extract_receipt_fields', parser)
    monkeypatch.setattr(views, 'extract_receipt_fields', parser)
    cache_module.install_receipt_text_cache()
    for content in [b'first', b'second', b'first']:
        services.extract_receipt_fields(content)
        assert cache_module._text_cache.get() is None
    assert len(calls) == 6
    high_level.extract_text(BytesIO(b'first'))
    high_level.extract_text(BytesIO(b'first'))
    assert len(calls) == 8


def test_failed_import_releases_cached_text(monkeypatch):
    from documents import services, views

    def parser(*args, **kwargs):
        raise ValueError('bad input')

    monkeypatch.setattr(services, 'extract_receipt_fields', parser)
    monkeypatch.setattr(views, 'extract_receipt_fields', parser)
    # Restore the extractor wrappers installed by this test on teardown too.
    monkeypatch.setattr(high_level, 'extract_text', high_level.extract_text)
    monkeypatch.setattr(PageObject, 'extract_text', PageObject.extract_text)
    cache_module.install_receipt_text_cache()
    with pytest.raises(ValueError):
        services.extract_receipt_fields(b'bad')
    assert cache_module._text_cache.get() is None
