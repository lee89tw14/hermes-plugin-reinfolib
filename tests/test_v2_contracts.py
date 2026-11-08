"""Replay de-identified shapes captured by opt-in live tests, never synthetic API data."""
from datetime import date
import json
from pathlib import Path
import pytest
from client import Gap
from reinfolib.core import fetch_transactions
from reinfolib.storage import SnapshotStore

FIXTURES = json.loads((Path(__file__).parent / 'fixtures' / 'v2-live-contracts.json').read_text())


@pytest.mark.parametrize('case', FIXTURES['cases'])
def test_replay_observed_contract(case, tmp_path):
    calls = []
    def transport(url, headers, timeout, cap):
        calls.append(url)
        return 200, {}, json.dumps(case['body'], ensure_ascii=False).encode()
    snap = fetch_transactions(case['query'], key='offline-replay-unit-key-1234567890',
                              storage=SnapshotStore(tmp_path), transport=transport,
                              gap=Gap(now=lambda: 0, sleep=lambda _: None), today=date(2026, 11, 8))
    assert len(calls) == 1 and snap['upstream_count'] == case['fixture_row_count'] == 1
    assert snap['records'] == case['body']['data']
    assert len(snap['records'][0]) == 29
    assert case['source_upstream_count'] > case['fixture_row_count']
