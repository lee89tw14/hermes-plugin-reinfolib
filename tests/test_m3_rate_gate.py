"""M3-1: profile-scoped cross-process rate gate (flock + atomic state file)."""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 6))
        self.now += seconds


def gate(tmp_path, clock=None, **kwargs):
    from reinfolib.scheduling import RateGate
    clock = clock or FakeClock()
    return RateGate(tmp_path / 'gate', clock=clock, sleep=clock.sleep, **kwargs), clock


def test_second_call_waits_one_second_after_previous_call_ended(tmp_path):
    g, clock = gate(tmp_path)
    def call():
        clock.now += 0.3  # request duration
        return 'ok'
    assert g.run(call) == 'ok'
    assert clock.sleeps == []
    assert g.run(call) == 'ok'
    assert clock.sleeps == [1.0]


def test_spacing_is_shared_by_separate_gate_objects_on_same_directory(tmp_path):
    clock = FakeClock()
    from reinfolib.scheduling import RateGate
    first = RateGate(tmp_path / 'gate', clock=clock, sleep=clock.sleep)
    second = RateGate(tmp_path / 'gate', clock=clock, sleep=clock.sleep)
    first.run(lambda: None)
    clock.now += 0.25
    second.run(lambda: None)
    assert clock.sleeps == [0.75]


def test_deadline_refuses_before_calling_when_wait_exceeds_it(tmp_path):
    from reinfolib.scheduling import GateDeadline
    g, clock = gate(tmp_path)
    g.run(lambda: None)
    called = []
    with pytest.raises(GateDeadline):
        g.run(lambda: called.append(1), deadline=clock.now + 0.5)
    assert called == []
    assert clock.sleeps == []


def test_partial_state_file_is_treated_conservatively_and_repaired(tmp_path):
    g, clock = gate(tmp_path)
    (tmp_path / 'gate').mkdir(exist_ok=True)
    (tmp_path / 'gate' / 'gate-state').write_text('{"last_end": 99')  # torn write
    g.run(lambda: None)
    assert clock.sleeps == [1.0]  # unknown history -> wait a full spacing
    state = json.loads((tmp_path / 'gate' / 'gate-state').read_text())
    assert state['last_end'] == clock.now


def test_crash_during_call_counts_from_recorded_start(tmp_path):
    g, clock = gate(tmp_path)
    (tmp_path / 'gate').mkdir(exist_ok=True)
    # A process died after recording its start but before recording its end.
    (tmp_path / 'gate' / 'gate-state').write_text(json.dumps({'last_start': 999.6, 'last_end': 990.0}))
    g.run(lambda: None)
    assert clock.sleeps == [0.6]


def test_future_timestamp_from_clock_skew_is_clamped(tmp_path):
    g, clock = gate(tmp_path)
    (tmp_path / 'gate').mkdir(exist_ok=True)
    (tmp_path / 'gate' / 'gate-state').write_text(json.dumps({'last_end': clock.now + 3600}))
    g.run(lambda: None)
    assert clock.sleeps == [1.0]


def test_end_is_recorded_even_when_call_raises(tmp_path):
    g, clock = gate(tmp_path)
    def broken():
        clock.now += 0.2
        raise ValueError('boom')
    with pytest.raises(ValueError):
        g.run(broken)
    g.run(lambda: None)
    assert clock.sleeps == [1.0]


def test_state_files_are_private_and_symlinked_directory_is_refused(tmp_path):
    from reinfolib.scheduling import GateUnavailable, RateGate
    g, _ = gate(tmp_path)
    g.run(lambda: None)
    assert (tmp_path / 'gate').stat().st_mode & 0o777 == 0o700
    for name in ('lock', 'gate-state'):
        assert (tmp_path / 'gate' / name).stat().st_mode & 0o777 == 0o600
    (tmp_path / 'real').mkdir()
    (tmp_path / 'link').symlink_to(tmp_path / 'real')
    with pytest.raises(GateUnavailable):
        RateGate(tmp_path / 'link').run(lambda: None)


def test_stale_lock_file_without_holder_does_not_block(tmp_path):
    (tmp_path / 'gate').mkdir(mode=0o700)
    (tmp_path / 'gate' / 'lock').write_text('left behind by a dead process')
    g, clock = gate(tmp_path)
    assert g.run(lambda: 'ok', deadline=clock.now + 0.1) == 'ok'


def test_held_lock_times_out_at_deadline(tmp_path):
    import fcntl
    from reinfolib.scheduling import GateDeadline, RateGate
    directory = tmp_path / 'gate'
    RateGate(directory).run(lambda: None)
    holder = open(directory / 'lock', 'a+')
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        start = time.monotonic()
        with pytest.raises(GateDeadline):
            RateGate(directory, spacing=0.0).run(lambda: None, deadline=time.time() + 0.3)
        assert time.monotonic() - start < 2
    finally:
        holder.close()


def test_threads_never_overlap_and_keep_spacing(tmp_path):
    from reinfolib.scheduling import RateGate
    spans, lock = [], threading.Lock()
    def worker():
        def call():
            start = time.monotonic()
            time.sleep(0.02)
            with lock:
                spans.append((start, time.monotonic()))
        RateGate(tmp_path / 'gate', spacing=0.1).run(call, deadline=time.time() + 10)
    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    spans.sort()
    assert len(spans) == 4
    for (_, end), (start, _) in zip(spans, spans[1:]):
        assert start - end >= 0.095


CHILD = r'''
import json, sys, time
sys.path.insert(0, sys.argv[1])
from reinfolib.scheduling import RateGate
def call():
    start = time.time(); time.sleep(0.03)
    return [start, time.time()]
print(json.dumps(RateGate(sys.argv[2], spacing=0.15).run(call, deadline=time.time() + 20)))
'''


def test_processes_share_spacing(tmp_path):
    procs = [subprocess.Popen([sys.executable, '-c', CHILD, str(ROOT), str(tmp_path / 'gate')],
                              stdout=subprocess.PIPE, text=True) for _ in range(4)]
    spans = sorted(json.loads(p.communicate(timeout=30)[0]) for p in procs)
    assert all(p.returncode == 0 for p in procs)
    for (_, end), (start, _) in zip(spans, spans[1:]):
        assert start - end >= 0.145


KILLED = r'''
import sys, time
sys.path.insert(0, sys.argv[1])
from reinfolib.scheduling import RateGate
def call():
    print('in-call', flush=True); time.sleep(60)
RateGate(sys.argv[2], spacing=0.5).run(call)
'''


def test_killed_holder_releases_lock_and_next_process_waits_from_its_start(tmp_path):
    from reinfolib.scheduling import RateGate
    child = subprocess.Popen([sys.executable, '-c', KILLED, str(ROOT), str(tmp_path / 'gate')],
                             stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == 'in-call'
    os.kill(child.pid, signal.SIGKILL)
    child.wait(timeout=10)
    state = json.loads((tmp_path / 'gate' / 'gate-state').read_text())
    killed_start = state['last_start']
    began = []
    RateGate(tmp_path / 'gate', spacing=0.5).run(lambda: began.append(time.time()),
                                                 deadline=time.time() + 10)
    assert began and began[0] - killed_start >= 0.495


def test_v2_production_fetch_uses_profile_rate_gate(tmp_path, monkeypatch):
    from reinfolib import adapter, core
    from reinfolib.scheduling import RateGate
    seen = {}
    def fake_fetch(query, **kwargs):
        seen.update(kwargs)
        raise core.CoreError('not_found', status_code=404)
    monkeypatch.setattr(adapter, 'data_root', lambda: tmp_path / 'v2')
    monkeypatch.setattr(core, 'fetch_transactions', fake_fetch)
    import service
    monkeypatch.setattr(service, '_secret', lambda name: 'k' * 16)
    with pytest.raises(adapter.AdapterFailure):
        adapter.fetch_snapshot({'city': '13104', 'year': 2024, 'quarter': 1})
    assert isinstance(seen['gap'], RateGate)
    assert seen['gap'].directory == tmp_path / 'v2' / 'rate-gate'
    assert seen['gap'].spacing == 1.0


def test_legacy_quarter_keeps_its_per_process_gap():
    import client
    assert type(client.DEFAULT_GAP).__name__ == 'Gap'
    assert client.Client(client.parse_origin(''), 'k' * 16).gap is client.DEFAULT_GAP
