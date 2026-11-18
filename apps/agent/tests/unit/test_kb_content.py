import pytest
from pydantic import ValidationError

from workbench_agent.knowledge.chunking import chunk_markdown, normalize_markdown
from workbench_agent.knowledge.models import KBDraft, draft_hash


def test_normalized_content_and_stable_chunks():
    body = normalize_markdown(b'\xef\xbb\xbf# Stock\r\n\r\nCheck retry.\r\n')
    assert body == '# Stock\n\nCheck retry.\n'
    chunks = chunk_markdown(body)
    assert chunks == chunk_markdown(body)
    assert chunks[0].line_start == 1
    assert all(len(chunk.content) <= 1500 and chunk.content in body for chunk in chunks)


def test_draft_hash_covers_scope_and_limits_utf8_bytes():
    a = KBDraft(title='库存', kind='faq', body_md='检查库存', service='stock')
    b = KBDraft(title='库存', kind='faq', body_md='检查库存', service='order')
    assert draft_hash(a) != draft_hash(b)
    with pytest.raises(ValidationError):
        KBDraft(title='长内容', kind='faq', body_md='中' * 6000)


@pytest.mark.parametrize('raw', [b'', b'  \n', b'\xff', b'bad\x00text'])
def test_import_rejects_invalid_text(raw):
    with pytest.raises((ValueError, UnicodeError)):
        normalize_markdown(raw)


@pytest.mark.parametrize('scope', ['', 'all', '*', 'stock,order'])
def test_scope_cannot_be_ambiguous(scope):
    with pytest.raises(ValidationError):
        KBDraft(title='库存', kind='faq', body_md='正文', service=scope)


def test_fenced_headings_and_long_lines_have_stable_original_locations():
    body = '# Root\n```md\n# Not a heading\n```\n## Repeated\n' + 'x' * 1501 + '\n## Repeated\nend\n'
    chunks = chunk_markdown(body)
    assert len(chunks) >= 3
    assert all(chunk.content in body and len(chunk.content) <= 1500 for chunk in chunks)
    assert all('Not a heading' not in chunk.heading_path for chunk in chunks)
    assert len({chunk.chunk_id for chunk in chunks}) == len(chunks)


def test_identical_segments_of_one_long_line_have_distinct_source_identities():
    chunks = chunk_markdown('x' * 3000)
    assert len(chunks) == 2
    assert chunks[0].content == chunks[1].content
    assert chunks[0].chunk_id != chunks[1].chunk_id
