"""M3-3: bounded historical probing (publication check) over an injected fetch."""
from datetime import date

import pytest

TODAY = date(2026, 11, 8)  # latest started quarter: 2026Q4


class Fetch:
    """Fake fetch: quarter -> status (200 found, 404, or CoreError kind)."""
    def __init__(self, outcomes):
        self.outcomes, self.queries = outcomes, []

    def __call__(self, query):
        from reinfolib.storage import CoreError
        self.queries.append(dict(query))
        key = (query['year'], query['quarter'])
        outcome = self.outcomes.get(key, 404)
        if outcome == 200:
            return {'snapshot_id': 'a' * 32, 'upstream_count': 5, 'fetched_at': '2026-11-08T00:00:00Z',
                    'query': dict(query), 'attempts': [{'n': 1, 'status': 200}], 'warnings': []}
        error = CoreError(*outcome) if isinstance(outcome, tuple) else CoreError('not_found', status_code=404)
        error.attempts = [{'n': 1, 'status': error.status_code}] if error.status_code else [{'n': 1, 'error': 'network'}]
        raise error


def probe(fetch, **kwargs):
    from reinfolib.history import probe as run
    return run({'city': '13104', **kwargs.pop('scope', {})}, fetch=fetch, today=TODAY, **kwargs)


def periods(result):
    return [(a['year'], a['quarter'], a['result']) for a in result['attempted']]


def test_latest_started_quarter_and_backward_order():
    from reinfolib.history import latest_started_quarter, quarters_back
    assert latest_started_quarter(date(2026, 11, 8)) == (2026, 4)
    assert latest_started_quarter(date(2026, 1, 1)) == (2026, 1)
    assert quarters_back((2026, 2), 4, earliest=(2005, 3)) == [(2026, 2), (2026, 1), (2025, 4), (2025, 3)]
    assert quarters_back((2005, 4), 5, earliest=(2005, 3)) == [(2005, 4), (2005, 3)]


def test_probes_past_two_404_quarters_until_found():
    fetch = Fetch({(2026, 1): 200})
    result = probe(fetch, max_quarters=8)
    assert result['outcome'] == 'found' and result['stop_reason'] == 'found'
    assert periods(result) == [(2026, 4, 'no_data_404'), (2026, 3, 'no_data_404'),
                               (2026, 2, 'no_data_404'), (2026, 1, 'found')]
    assert result['found']['snapshot_id'] == 'a' * 32 and result['found']['period'] == '2026Q1'
    assert result['live_requests'] == 4
    assert fetch.queries[0] == {'city': '13104', 'year': 2026, 'quarter': 4,
                                'price_classification': '01', 'language': 'ja'}


def test_all_404_in_range_is_not_reported_as_no_data_exists():
    result = probe(Fetch({}), max_quarters=3)
    assert result['outcome'] == 'none_found_in_range' and result['stop_reason'] == 'range_exhausted'
    assert len(result['attempted']) == 3 and result['found'] is None
    assert any('not zero' in note for note in result['notes'])


def test_failure_is_distinct_and_stops_without_concluding():
    fetch = Fetch({(2026, 3): ('upstream', 'network'), (2026, 2): 200})
    result = probe(fetch, max_quarters=8)
    assert result['outcome'] == 'failed' and result['stop_reason'] == 'failure'
    assert periods(result) == [(2026, 4, 'no_data_404'), (2026, 3, 'failed')]
    assert result['attempted'][1]['error_type'] == 'upstream'
    assert result['attempted'][1]['attempts'] == [{'n': 1, 'error': 'network'}]
    assert result['found'] is None


def test_request_budget_smaller_than_range_is_reported():
    result = probe(Fetch({(2025, 1): 200}), max_quarters=8, max_requests=3)
    assert result['outcome'] == 'budget_exhausted' and result['stop_reason'] == 'request_budget_exhausted'
    assert len(result['attempted']) == 3 and result['live_requests'] == 3


def test_contract_prices_stop_at_2021q1():
    result = probe(Fetch({}), max_quarters=8, price_classification='02',
                   newest_year=2021, newest_quarter=3)
    assert [(a['year'], a['quarter']) for a in result['attempted']] == [(2021, 3), (2021, 2), (2021, 1)]
    assert result['stop_reason'] == 'reached_earliest_available'


def test_explicit_newest_quarter_must_not_be_in_the_future():
    from reinfolib.storage import CoreError
    with pytest.raises(CoreError) as caught:
        probe(Fetch({}), newest_year=2027, newest_quarter=1)
    assert caught.value.detail == 'future_quarter'


@pytest.mark.parametrize('bad', [{'max_quarters': 0}, {'max_quarters': 21}, {'max_quarters': True},
                                 {'max_requests': 0}, {'newest_year': 2026}, {'newest_quarter': 2}])
def test_bounds_are_validated(bad):
    from reinfolib.storage import CoreError
    with pytest.raises(CoreError) as caught:
        probe(Fetch({}), **bad)
    assert caught.value.error_type == 'invalid_argument'


def test_station_and_area_scopes_pass_through():
    fetch = Fetch({(2026, 4): 200})
    from reinfolib.history import probe as run
    run({'area': '13'}, fetch=fetch, today=TODAY, max_quarters=1)
    assert fetch.queries[-1]['area'] == '13' and 'city' not in fetch.queries[-1]
    run({'station': '003696'}, fetch=fetch, today=TODAY, max_quarters=1, price_classification='both')
    assert fetch.queries[-1] == {'station': '003696', 'year': 2026, 'quarter': 4,
                                 'price_classification': 'both', 'language': 'ja'}


KEY = 'synthetic-history-key-123456'
FIXTURE = (__import__('pathlib').Path(__file__).parent / 'fixtures' / 'xit001_ok.json').read_text()


def fixture_for(year, quarter):
    import json
    body = json.loads(FIXTURE)
    body['data'][0]['Period'] = f'{year}年第{quarter}四半期'
    return json.dumps(body, ensure_ascii=False).encode()


@pytest.fixture
def tool_env(tmp_path, monkeypatch):
    import client, service
    from urllib.parse import parse_qs, urlsplit
    from reinfolib import adapter, research_tools
    from reinfolib.transport import RetryPolicy
    monkeypatch.setattr(adapter, 'data_root', lambda: tmp_path / 'v2')
    monkeypatch.setattr(adapter, 'RETRY_POLICY', RetryPolicy(backoff_base=0.01))
    monkeypatch.setattr(adapter, 'production_gate', lambda: None)
    monkeypatch.setattr(research_tools, 'today', lambda: TODAY)
    monkeypatch.setattr(service, '_secret', lambda name: KEY)
    calls, published = [], {}
    def transport(url, headers, timeout, cap):
        q = parse_qs(urlsplit(url).query)
        period = (int(q['year'][0]), int(q['quarter'][0]))
        calls.append(period)
        if period in published:
            return 200, {}, fixture_for(*period)
        return 404, {}, b''
    monkeypatch.setattr(client, 'default_transport', transport)
    return calls, published


def test_history_tool_registered_and_dispatches(tool_env):
    import json
    from reinfolib import adapter
    calls, published = tool_env
    published[(2026, 2)] = True
    assert 'reinfolib_history' in adapter.DEFINITIONS
    result = json.loads(adapter.dispatch('reinfolib_history', {'city': '13102', 'max_quarters': 6}))
    assert result['ok'] is True and result['outcome'] == 'found'
    assert calls == [(2026, 4), (2026, 3), (2026, 2)]
    assert [a['result'] for a in result['attempted']] == ['no_data_404', 'no_data_404', 'found']
    assert result['attempted'][0]['attempts'] == [{'n': 1, 'status': 404}]
    snapshot_id = result['found']['snapshot_id']
    page = json.loads(adapter.dispatch('reinfolib_records', {'snapshot_id': snapshot_id}))
    assert page['ok'] is True and page['upstream_count'] == 1
    assert result['attribution'] and result['credit']
    assert KEY not in json.dumps(result)


@pytest.mark.parametrize('args', [{}, {'city': '13102', 'area': '13'}, {'city': '1310'},
                                  {'city': '13102', 'max_quarters': 50}, {'city': '13102', 'url': 'x'}])
def test_history_tool_rejects_bad_arguments_without_requests(tool_env, args):
    import json
    from reinfolib import adapter
    calls, _ = tool_env
    result = json.loads(adapter.dispatch('reinfolib_history', args))
    assert result['ok'] is False and result['error_type'] == 'invalid_argument'
    assert calls == []
