"""Review ebebd09 residual 2: only gate/storage failures map to rate_gate_unavailable."""
import pytest

from reinfolib.scheduling import GateUnavailable
from reinfolib.transport import TransportFailure, request


class Gate:
    def __init__(self, error):
        self.error = error

    def run(self, call, deadline):
        raise self.error


def wire(*args):
    raise AssertionError('wire must not be called')


@pytest.mark.parametrize('error', [GateUnavailable('x'), PermissionError('private /path'), OSError('private')])
def test_gate_storage_failures_are_storage(error):
    with pytest.raises(TransportFailure) as caught:
        request('https://example.invalid', {}, timeout=1, cap=1, transport=wire, gate=Gate(error),
                sleep=lambda s: pytest.fail('no backoff'))
    assert (caught.value.kind, caught.value.detail, caught.value.attempts) == ('storage', 'rate_gate_unavailable', [])


@pytest.mark.parametrize('error', [RuntimeError('private'), KeyError('private'), TypeError('private')])
def test_program_errors_in_gate_are_internal_not_storage(error):
    with pytest.raises(TransportFailure) as caught:
        request('https://example.invalid', {}, timeout=1, cap=1, transport=wire, gate=Gate(error),
                sleep=lambda s: pytest.fail('no backoff'))
    failure = caught.value
    assert (failure.kind, failure.detail, failure.attempts, failure.stop_reason) == (
        'internal', 'rate_gate_failed', [], 'nonretryable')
    assert 'private' not in str(failure.args)
