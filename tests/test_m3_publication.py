"""Official homepage announcement lookup; never execute website JavaScript."""
import json
import pytest


@pytest.mark.parametrize('page', [
    '<script src="https://attacker.invalid/source.js"></script>',
    '<script src="//attacker.invalid/source.js"></script>',
    '<script src="/_next/static/chunks/../../source.js"></script>',
    'お知らせを読み込み中',
])
def test_unrecognised_homepage_fails_closed_without_external_requests(page):
    from reinfolib.publication import lookup, HOME
    calls = []
    def transport(url, headers, timeout, cap):
        calls.append(url)
        assert 'Ocp-Apim-Subscription-Key' not in headers
        return 200, {}, page.encode()
    result = lookup(transport=transport)
    assert result['status'] == 'unavailable'
    assert result['announcements'] == [] and result['fetched_at'] is None
    assert calls == [HOME]


def test_unavailable_announcement_does_not_stop_quarter_probe():
    from reinfolib.history import probe
    from reinfolib.storage import CoreError
    calls = []
    def fetch(query):
        calls.append(query)
        raise CoreError('not_found', status_code=404)
    result = probe({'city': '13102'}, fetch=fetch, max_quarters=1,
                   announcement_lookup=lambda end: {'status': 'unavailable', 'announcements': []})
    assert len(calls) == 1
    assert result['publication']['status'] == 'unavailable'
    assert result['outcome'] == 'none_found_in_range'

from datetime import date


def test_history_checks_announcements_before_probing_without_inferring_scope():
    from reinfolib.history import probe
    from reinfolib.storage import CoreError
    events = []
    now = [100.0]
    def announcement(end):
        events.append('announcement')
        assert end == 102
        now[0] += 1
        return {'status': 'checked', 'announcements': [{'text': '令和8年4月～6月を追加'}]}
    def fetch(query, end):
        events.append(query['quarter'])
        assert end - now[0] == 1
        now[0] += 1
        raise CoreError('not_found', status_code=404)
    result = probe({'city': '13102'}, fetch=None, bounded_fetch=fetch,
                   announcement_lookup=announcement, clock=lambda: now[0], deadline_seconds=2,
                   today=date(2026, 11, 8))
    assert events == ['announcement', 4]
    assert result['publication']['status'] == 'checked'
    assert result['attempted'][0]['result'] == 'no_data_404'
    assert result['found'] is None and result['stop_reason'] == 'deadline'


def test_public_website_flow_extracts_dated_transaction_announcements():
    from reinfolib.publication import lookup, HOME, NEWS
    calls = []
    website_key = 'synthetic-public-website-token'
    def transport(url, headers, timeout, cap):
        calls.append((url, headers))
        assert 0 < timeout <= 15 and cap <= 2_000_000
        if url == HOME:
            return 200, {}, b'<script src="/_next/static/chunks/site.js"></script>'
        if url.endswith('/site.js'):
            return 200, {}, ('baseURL:"https://www.reinfolib.mlit.go.jp/in-api",'
                             'headers:{"Ocp-Apim-Subscription-Key":"'+website_key+'"}').encode()
        assert url == NEWS
        assert headers['Ocp-Apim-Subscription-Key'] == website_key
        return 200, {}, json.dumps({'data': [
            {'publish_from': '2026-10-30T00:00:00', 'content': '<p>不動産取引価格情報を更新しました。</p>', 'important_flag': False},
            {'publish_from': '2026-10-01T00:00:00', 'content': '地価公示を更新しました。', 'important_flag': False}
        ]}, ensure_ascii=False).encode()
    result = lookup(transport=transport)
    assert result['status'] == 'checked'
    assert result['source_url'] == HOME and result['backing_url'] == NEWS
    assert result['announcements'] == [{'published_at': '2026-10-30T00:00:00',
                                      'text': '不動産取引価格情報を更新しました。'}]
    assert result['fetched_at'].endswith('Z')
    assert website_key not in json.dumps(result)
    assert len(calls) == 3
