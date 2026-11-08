"""Profile-scoped cross-process rate gate for outbound Reinfolib requests.

One directory (normally ``<profile plugin-data>/jp-reinfolib/v2/rate-gate``) holds:

- ``lock``: an advisory ``flock`` held for the whole outbound call. The kernel drops it
  when the holder exits or is killed, so a leftover file never blocks ("stale lock").
- ``gate-state``: ``{"last_start": t, "last_end": t}`` in wall-clock seconds, replaced
  atomically. ``last_start`` is written *before* the call, so a process that dies mid
  request still pushes the next start ``spacing`` seconds past its own start.

Unknown or torn state is treated conservatively (wait a full spacing) and rewritten.
A timestamp further in the future than one spacing (clock skew) is clamped to "now".

The 1-second spacing is this plugin's local policy; the official manual asks only for
spaced, non-continuous requests and documents no numeric limit (help/apiManual Q.3).
It coordinates processes of one profile only; another profile or host using the same
key is not coordinated.

Reusable API (M4 tile transport should reuse it)::

    gate = RateGate(directory, spacing=1.0)
    result = gate.run(call, deadline=time.time() + 60)   # call() -> anything

``run`` raises ``GateDeadline`` without calling when the lock or the spacing wait
would pass ``deadline`` and ``GateUnavailable`` when the directory is unsafe.
"""
import errno
import fcntl
import json
import math
import os
import time
from pathlib import Path

DEFAULT_SPACING = 1.0


class GateDeadline(Exception):
    """The call was not started: waiting for the lock or spacing would pass the deadline."""


class GateUnavailable(Exception):
    """The gate directory is unsafe (symlink) or cannot be used."""


class RateGate:
    def __init__(self, directory, *, spacing=DEFAULT_SPACING, clock=None, sleep=None, poll=0.05):
        self.directory = Path(directory)
        self.spacing = float(spacing)
        self._clock = clock or time.time
        self._sleep = sleep or time.sleep
        self._poll = poll

    # -- filesystem -----------------------------------------------------------------
    def _open_dir(self):
        if any(path.is_symlink() for path in (self.directory, *self.directory.parents)):
            raise GateUnavailable('symlink_gate_directory')
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            os.fchmod(fd, 0o700)
            return fd
        except OSError:
            raise GateUnavailable('gate_directory_unavailable') from None

    def _read_state(self, dir_fd):
        try:
            fd = os.open('gate-state', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        except FileNotFoundError:
            return {}
        except OSError:
            return None
        try:
            with os.fdopen(fd, 'rb') as handle:
                data = json.loads(handle.read(4096).decode('utf-8'))
        except (ValueError, UnicodeError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        values = [data.get(name) for name in ('last_start', 'last_end')]
        if any(v is not None and (type(v) not in (int, float) or not math.isfinite(v)) for v in values):
            return None
        return data

    def _write_state(self, dir_fd, state):
        temporary = 'gate-state.tmp'
        try:
            os.unlink(temporary, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                     dir_fd=dir_fd)
        with os.fdopen(fd, 'w') as handle:
            handle.write(json.dumps(state))
        os.replace(temporary, 'gate-state', src_dir_fd=dir_fd, dst_dir_fd=dir_fd)

    # -- gate -----------------------------------------------------------------------
    def _acquire(self, dir_fd, deadline):
        try:
            lock_fd = os.open('lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
            os.fchmod(lock_fd, 0o600)
        except OSError:
            raise GateUnavailable('gate_lock_unavailable') from None
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return lock_fd
            except OSError as error:
                if error.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    os.close(lock_fd)
                    raise GateUnavailable('gate_lock_unavailable') from None
            if deadline is not None and self._clock() + self._poll > deadline:
                os.close(lock_fd)
                raise GateDeadline('lock_wait_exceeds_deadline')
            self._sleep(self._poll)

    def run(self, call, *, deadline=None):
        """Run ``call()`` under the profile lock, at least ``spacing`` s after the previous
        call (any process) ended or, if it crashed, started. ``deadline`` is absolute
        ``clock()`` time; nothing is sent when it cannot be met."""
        dir_fd = self._open_dir()
        try:
            lock_fd = self._acquire(dir_fd, deadline)
            try:
                state = self._read_state(dir_fd)
                now = self._clock()
                if state is None:
                    reference = now  # torn/unknown history: assume a call just ended
                    state = {}
                else:
                    stamps = [v for v in (state.get('last_start'), state.get('last_end')) if v is not None]
                    reference = max(stamps) if stamps else None
                    if reference is not None and reference > now + self.spacing:
                        reference = now  # clock skew: do not block for an unbounded time
                wait = 0.0 if reference is None else reference + self.spacing - now
                if wait > 0:
                    if deadline is not None and now + wait > deadline:
                        raise GateDeadline('spacing_exceeds_deadline')
                    self._sleep(wait)
                start = self._clock()
                try:
                    self._write_state(dir_fd, {'last_start': start, 'last_end': state.get('last_end')})
                except OSError:
                    raise GateUnavailable('gate_state_unavailable') from None
                try:
                    return call()
                finally:
                    try:
                        self._write_state(dir_fd, {'last_start': start, 'last_end': self._clock()})
                    except OSError:
                        pass  # last_start already persisted; the next caller stays conservative
            finally:
                os.close(lock_fd)  # releases the flock
        finally:
            os.close(dir_fd)
