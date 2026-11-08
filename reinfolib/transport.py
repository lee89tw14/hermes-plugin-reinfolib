"""Bounded retry over any injected Transport, optionally through a RateGate.

Reusable API (also intended for the M4 tile transport)::

    from reinfolib.transport import RetryPolicy, request, TransportFailure
    response = request(url, headers, timeout=30, cap=8_000_000,
                       policy=RetryPolicy(), transport=inner, gate=rate_gate)
    response.status, response.headers, response.payload, response.attempts, response.stop_reason

``inner`` is any ``Transport``: ``(url, headers, timeout, cap) -> (status, headers, bytes)``
(e.g. ``client.default_transport`` or a gated test transport). Every attempt
passes through ``gate.run(call, deadline=...)`` when a gate is given.

Policy (GET only):
- 429: wait ``Retry-After`` (delta-seconds or HTTP-date). A value above
  ``retry_after_cap`` stops instead of waiting less than the server asked. A missing or
  unparseable header falls back to backoff.
- 500/502/503/504 and network errors/timeouts: exponential backoff
  ``backoff_base * 2**(n-1)`` capped at ``backoff_max``.
- Any other status (400/401/403/404, 2xx, ...) is returned at once, never retried.
- At most ``max_attempts`` attempts and ``deadline`` seconds in total, including gate
  waits; each attempt's timeout is clipped to the remaining deadline.

``response.attempts`` lists ``{"n", "status"|"error", "retry_after_s"?, "wait_s"?}``.
Only fixed strings are recorded; URLs, headers, keys and exception text never are.
``TransportFailure(kind, detail, attempts)`` is raised when no HTTP status is available.
"""
import math
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

from .scheduling import GateDeadline, GateUnavailable

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    backoff_base: float = 1.0
    backoff_max: float = 8.0
    retry_after_cap: float = 30.0
    deadline: float = 90.0

    def backoff(self, attempt):
        return min(self.backoff_max, self.backoff_base * 2 ** (attempt - 1))


DEFAULT_POLICY = RetryPolicy()


@dataclass
class Response:
    status: int
    headers: dict
    payload: bytes
    attempts: list = field(default_factory=list)
    stop_reason: str = 'completed'


class TransportFailure(Exception):
    """No usable HTTP response. kind/detail are fixed strings; attempts as recorded."""
    def __init__(self, kind, detail, attempts, stop_reason='transport_failure'):
        super().__init__(kind, detail)
        self.kind, self.detail, self.attempts = kind, detail, attempts
        self.stop_reason = stop_reason


def retry_after_seconds(headers, now):
    """Seconds from Retry-After (delta-seconds or HTTP-date); None if absent/invalid."""
    value = next((str(v) for k, v in headers.items() if str(k).lower() == 'retry-after'), None)
    if value is None:
        return None
    value = value.strip()
    if value.isdigit() and len(value) <= 9:
        return float(int(value))
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when is None or when.tzinfo is None:
        return None
    seconds = when.timestamp() - now
    return max(0.0, seconds) if math.isfinite(seconds) else None


def _classify(error):
    """Map a transport exception to (retryable, kind, detail) using fixed strings only."""
    kind = getattr(error, 'error_type', None)
    detail = getattr(error, 'detail', None)
    if isinstance(error, TimeoutError) or kind == 'timeout':
        return True, 'timeout', 'timeout'
    if kind is not None and kind != 'upstream':
        safe = detail if detail in ('too_big', 'short_body', 'bad_gzip', 'bad_encoding') else 'bad_body'
        return False, 'invalid_response', safe
    if isinstance(error, OSError) or kind == 'upstream':
        return True, 'upstream', 'network'
    return False, 'internal', 'transport_failed'


class _WireFailure(Exception):
    """Distinguish exceptions from the wire callback from gate failures."""
    def __init__(self, error):
        self.error = error


def request(url, headers, *, timeout, cap, policy=DEFAULT_POLICY, transport, gate=None,
            clock=None, sleep=None, absolute_deadline=None):
    clock = clock or time.time
    sleep = sleep or time.sleep
    end = clock() + policy.deadline
    if absolute_deadline is not None:
        end = min(end, absolute_deadline)
    attempts = []
    last = None  # last Response, or (kind, detail) for an exception
    stop = 'max_attempts'
    for number in range(1, policy.max_attempts + 1):
        remaining = end - clock()
        if remaining <= 0:
            stop = 'deadline'
            break
        def send():
            # Gate contention/spacing consumes the same request deadline.
            remaining = end - clock()
            if remaining <= 0:
                raise GateDeadline('send_deadline')
            try:
                return transport(url, headers, min(timeout, remaining), cap)
            except Exception as error:
                raise _WireFailure(error) from None
        try:
            status, response_headers, payload = (gate.run(send, deadline=end) if gate is not None
                                                 else send())
        except GateDeadline:
            stop = 'deadline'
            if not attempts:
                raise TransportFailure('timeout', 'rate_gate_deadline', attempts, 'deadline') from None
            break
        except _WireFailure as failure:
            retryable, kind, detail = _classify(failure.error)
            attempts.append({'n': number, 'error': detail if kind != 'upstream' else 'network'})
            last = (kind, detail)
            if not retryable:
                raise TransportFailure(kind, detail, attempts, 'nonretryable') from None
            wait = policy.backoff(number)
        except (GateUnavailable, OSError):
            raise TransportFailure('storage', 'rate_gate_unavailable', attempts, 'nonretryable') from None
        except Exception:
            raise TransportFailure('internal', 'rate_gate_failed', attempts, 'nonretryable') from None
        else:
            record = {'n': number, 'status': status}
            attempts.append(record)
            last = Response(status, response_headers, payload, attempts)
            if status not in RETRY_STATUSES:
                last.stop_reason = 'completed'
                return last
            wait = policy.backoff(number)
            if status == 429:
                hinted = retry_after_seconds(response_headers or {}, clock())
                if hinted is not None:
                    record['retry_after_s'] = hinted
                    if hinted > policy.retry_after_cap:
                        stop = 'retry_after_exceeds_cap'
                        break
                    wait = hinted
        if number == policy.max_attempts:
            break
        if clock() + wait > end:
            stop = 'deadline'
            break
        attempts[-1]['wait_s'] = wait
        sleep(wait)
    if isinstance(last, Response):
        last.stop_reason = stop
        return last
    kind, detail = last if last else ('timeout', 'deadline')
    raise TransportFailure(kind, detail, attempts, stop)
