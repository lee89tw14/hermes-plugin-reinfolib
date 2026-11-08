"""Review ebebd09 residual 1: live_requests counts HTTP requests actually sent."""
from datetime import date

from reinfolib.history import probe
from reinfolib.storage import CoreError

TODAY = date(2026, 11, 8)


def failing(detail, attempts):
    def fetch(query):
        error = CoreError('storage' if detail == 'rate_gate_unavailable' else 'upstream', detail)
        error.attempts = attempts
        raise error
    return fetch


def test_gate_failure_before_any_http_reports_zero_live_requests():
    result = probe({'city': '13104'}, fetch=failing('rate_gate_unavailable', []), today=TODAY)
    assert result['outcome'] == 'failed'
    assert result['live_requests'] == 0
    assert result['attempted_periods'] == 1


def test_retried_period_counts_every_sent_request():
    attempts = [{'n': 1, 'error': 'network'}, {'n': 2, 'error': 'network'}, {'n': 3, 'error': 'network'}]
    result = probe({'city': '13104'}, fetch=failing('network', attempts), today=TODAY)
    assert result['live_requests'] == 3 and result['attempted_periods'] == 1


def test_fetch_without_attempt_metadata_counts_one_request():
    def fetch(query):
        raise CoreError('not_found', status_code=404)
    result = probe({'city': '13104'}, fetch=fetch, today=TODAY, max_quarters=2)
    assert result['live_requests'] == 2 and result['attempted_periods'] == 2
