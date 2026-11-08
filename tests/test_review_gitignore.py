"""P3-3: Git ignores local secrets and generated property data."""
from pathlib import Path
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('path', ['.env', '.env.local', 'config/test.env',
    'exports/report.csv', 'snapshots/record.json', 'work/exports/result.json'])
def test_local_sensitive_artifacts_are_ignored(path):
    result = subprocess.run(['git', 'check-ignore', '--no-index', path], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.strip() == path


def test_contract_fixtures_remain_trackable():
    result = subprocess.run(['git', 'check-ignore', '--no-index', 'tests/fixtures/v2-live-contracts.json'], cwd=ROOT, capture_output=True)
    assert result.returncode == 1
