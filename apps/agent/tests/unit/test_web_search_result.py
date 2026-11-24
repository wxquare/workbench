import pytest

from workbench_agent.tools.web_search_result import validate_web_search_result


def result(**changes):
    value = {
        'schema_version': 'web-search-result-v1',
        'status': 'ok',
        'results': [{
            'title': 'SQLite documentation',
            'url': 'https://docs.example.com/sqlite',
            'snippet': 'Use WAL for concurrent readers and one writer.',
            'published_at': None,
        }],
        'retrieved_at': '2026-11-24T00:00:00Z',
        'truncated': False,
        'domain_policy_id': 'public-docs-v1',
        'content_kind': 'search_snippet',
    }
    value.update(changes)
    return value


def validate(value):
    return validate_web_search_result(
        value,
        expected_policy_id='public-docs-v1',
        allowed_hosts=('docs.example.com',),
    )


def test_valid_search_snippet_is_normalized():
    assert validate(result())['results'][0]['url'] == (
        'https://docs.example.com/sqlite'
    )


def test_approved_https_url_may_have_query_and_fragment():
    value = result()
    value['results'][0]['url'] = (
        'https://docs.example.com/sqlite?view=wal#concurrency'
    )
    assert validate(value)['results'][0]['url'].endswith('#concurrency')


@pytest.mark.parametrize(
    'url',
    [
        'http://docs.example.com/a',
        'javascript:alert(1)',
        'data:text/plain,x',
        'file:///etc/passwd',
        'https://user:pass@docs.example.com/a',
        'https://docs.example.com.attacker.test/a',
        'https://127.0.0.1/a',
        'https://[::1]/a',
        'https://169.254.169.254/a',
    ],
)
def test_unsafe_or_unapproved_urls_are_rejected(url):
    value = result()
    value['results'][0]['url'] = url
    with pytest.raises(ValueError):
        validate(value)


def test_failure_statuses_cannot_carry_results():
    with pytest.raises(ValueError):
        validate(result(status='empty'))


def test_wrong_policy_and_missing_timezone_are_rejected():
    value = result()
    value['domain_policy_id'] = 'other'
    with pytest.raises(ValueError, match='POLICY'):
        validate(value)
    value = result()
    value['retrieved_at'] = '2026-11-24T00:00:00'
    with pytest.raises(ValueError):
        validate(value)
