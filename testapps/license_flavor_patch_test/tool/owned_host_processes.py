"""Bounded cleanup of process groups created by this harness on macOS/Linux.

The unreaped session leader pins its PID and process-group ID, even when callers
use poll(), wait(), or communicate(). Only successful group cleanup reaps it.
Descendants which deliberately leave the owned process group are out of scope.
"""
from __future__ import annotations

import math
import os
import platform
import signal
import subprocess
import time


class OwnedProcessError(RuntimeError):
    """Ownership or bounded process-group cleanup could not be verified."""


class RetainedPopen(subprocess.Popen):
    """Popen with non-reaping exit observation until its owner finishes cleanup."""

    def __init__(self, *args, **kwargs):
        if (platform.system() not in ('Darwin', 'Linux') or
                not all(hasattr(os, name) for name in
                        ('waitid', 'WNOWAIT', 'WEXITED', 'P_PID'))):
            raise OwnedProcessError('Owned host processes require macOS/Linux waitid WNOWAIT')
        self.reaped = False
        self._identity_lost = False
        super().__init__(*args, **kwargs)

    def _internal_poll(self, *args, **kwargs):
        if self.reaped:
            return self.returncode
        with self._waitpid_lock:
            if self._identity_lost:
                raise OwnedProcessError('Owned process leader identity was lost')
            try:
                status = os.waitid(os.P_PID, self.pid,
                                   os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                self._identity_lost = True
                raise OwnedProcessError('Owned process leader was reaped outside its owner') from None
            if status is not None:
                if status.si_code == os.CLD_EXITED:
                    self.returncode = status.si_status
                elif status.si_code in (os.CLD_KILLED, os.CLD_DUMPED):
                    self.returncode = -status.si_status
                else:
                    raise OwnedProcessError('Unexpected owned process wait status')
            elif self.returncode is not None:
                # Cached exit status must never substitute for retained identity.
                self._identity_lost = True
                raise OwnedProcessError('Owned process retained exit status disappeared')
        return self.returncode

    def _wait(self, timeout):
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            code = self._internal_poll()
            if code is not None:
                return code
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(.01)

    def _reap_after_cleanup(self):
        if self.reaped or self._internal_poll() is None:
            raise OwnedProcessError('Cannot reap an unretained or running owned leader')
        with self._waitpid_lock:
            try:
                waited, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                self._identity_lost = True
                raise OwnedProcessError('Owned process leader identity was lost before reap') from None
            if waited != self.pid:
                raise OwnedProcessError('Owned leader was not ready for bounded reap')
            self.returncode = os.waitstatus_to_exitcode(status)
            self.reaped = True


def parse_live_members(output, group):
    """Parse a complete `ps -axo pid=,pgid=,stat=` snapshot, ignoring zombies."""
    if not isinstance(output, str) or not output.strip():
        raise OwnedProcessError('Empty host process membership snapshot')
    live = set()
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if (len(fields) != 3 or not fields[0].isascii() or
                not fields[1].isascii() or not fields[0].isdecimal() or
                not fields[1].isdecimal() or not fields[2][0].isascii() or
                not fields[2][0].isalpha()):
            raise OwnedProcessError('Invalid host process membership snapshot')
        pid, pgid = int(fields[0]), int(fields[1])
        if pgid == group and fields[2][0].upper() not in ('Z', 'X'):
            live.add(pid)
    return sorted(live)


class OwnedHostProcesses:
    """Signal only groups still pinned by their own retained child leader.

    Failed cleanup keeps ownership and the leader unreaped, allowing a bounded
    retry without ever signalling a recycled process-group ID.
    """

    def __init__(self, *, term_timeout=5, kill_timeout=5, poll_interval=.05,
                 ps_timeout=2):
        timings = (term_timeout, kill_timeout, poll_interval, ps_timeout)
        if (not all(math.isfinite(value) for value in timings) or
                term_timeout < 0 or kill_timeout < 0 or poll_interval <= 0 or
                ps_timeout <= 0):
            raise ValueError('Invalid owned-process cleanup timing')
        self.owned = {}
        self.term_timeout = term_timeout
        self.kill_timeout = kill_timeout
        self.poll_interval = poll_interval
        self.ps_timeout = ps_timeout

    def spawn(self, command, **kwargs):
        if any(name in kwargs for name in
               ('start_new_session', 'preexec_fn', 'process_group')):
            raise OwnedProcessError('Owned process group settings cannot be overridden')
        process = RetainedPopen(command, start_new_session=True, **kwargs)
        self.owned[process.pid] = process
        return process

    def _assert_owned(self, process):
        if (self.owned.get(process.pid) is not process or process.reaped or
                process._identity_lost):
            raise OwnedProcessError('Refusing to signal a group without its retained owned leader')
        # Always recheck waitid, including after an earlier poll observed exit.
        return process._internal_poll()

    def live_members(self, group):
        try:
            result = subprocess.run(
                ['/bin/ps', '-axo', 'pid=,pgid=,stat='], check=True,
                capture_output=True, text=True, timeout=self.ps_timeout)
        except (OSError, subprocess.SubprocessError, UnicodeError):
            raise OwnedProcessError('Cannot inspect owned host process group') from None
        return parse_live_members(result.stdout, group)

    def _signal(self, process, sig):
        self._assert_owned(process)
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError) as error:
            # Darwin may report EPERM for a group containing only its retained
            # zombie. Tolerate that only with fresh, positive cleanup evidence.
            if isinstance(error, PermissionError) and platform.system() != 'Darwin':
                raise OwnedProcessError('Cannot signal retained owned process group') from error
            if self._assert_owned(process) is None or self.live_members(process.pid):
                raise OwnedProcessError('Cannot signal retained owned process group') from error

    def _wait_empty(self, process, timeout, *, kill=False):
        deadline = time.monotonic() + timeout
        while True:
            exited = self._assert_owned(process) is not None
            members = self.live_members(process.pid)
            if exited and not members:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if kill:
                self._signal(process, signal.SIGKILL)
            time.sleep(min(self.poll_interval, remaining))

    def stop(self, process):
        if process is None:
            return
        self._assert_owned(process)
        self._signal(process, signal.SIGTERM)
        if not self._wait_empty(process, self.term_timeout):
            self._signal(process, signal.SIGKILL)
            if not self._wait_empty(process, self.kill_timeout, kill=True):
                raise OwnedProcessError('Owned host group still has live members after bounded cleanup')
        # No group signals are allowed after this final, explicit waitpid.
        process._reap_after_cleanup()
        del self.owned[process.pid]

    def close(self):
        failures = []
        for process in list(self.owned.values()):
            try:
                self.stop(process)
            except Exception as error:
                failures.append(error)
        if failures:
            raise OwnedProcessError('Owned host process cleanup failed') from failures[0]
