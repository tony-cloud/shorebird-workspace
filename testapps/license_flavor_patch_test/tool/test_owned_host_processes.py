"""Owned-host contracts and benign local Python process-group integration tests."""
import importlib.util
import os
from pathlib import Path
import platform
import select
import signal
import subprocess
import sys
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('owned_host_processes',
    Path(__file__).with_name('owned_host_processes.py'))
owned = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(owned)


class MembershipTest(unittest.TestCase):
    def test_snapshot_filters_group_and_zombie_state_prefixes(self):
        self.assertEqual(owned.parse_live_members(
            '  10 10 Z+\n 11 10 S<s\n 12 12 R+\n 13 10 X\n 14 10 T\n', 10),
            [11, 14])

    def test_malformed_or_empty_snapshot_fails_closed(self):
        for value in ('', ' \n', '1 2', '1 2 S extra', 'PID PGID STAT',
                      '-1 2 S', '1 -2 S', '1 2 !+', '١ 2 S',
                      '-1 2 ?E', '1 -2 ?E', '١ 2 ?E', '1 2 ?E extra'):
            with self.subTest(value=value), self.assertRaises(owned.OwnedProcessError):
                owned.parse_live_members(value, 2)

    def test_actual_macos_unknown_exiting_rows_are_live_only_in_matching_group(self):
        snapshot = ('14039 14292 ?E\n14040 14292 ?E\n14041 14292 ?E\n'
                    '42 42 S\n43 42 Z\n')
        self.assertEqual(owned.parse_live_members(snapshot, 14292), [14039, 14040, 14041])
        self.assertEqual(owned.parse_live_members(snapshot, 42), [42])
        self.assertEqual(owned.parse_live_members(snapshot, 99), [])

    def test_unknown_primary_state_is_conservatively_live_with_or_without_modifiers(self):
        for state in ('?', '?E', '?+'):
            with self.subTest(state=state):
                self.assertEqual(owned.parse_live_members(f'10 10 {state}\n', 10), [10])

    def test_cleanup_timings_are_finite_and_bounded(self):
        for kwargs in ({'term_timeout': float('nan')}, {'kill_timeout': float('inf')},
                       {'term_timeout': -1}, {'poll_interval': 0}, {'ps_timeout': 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                owned.OwnedHostProcesses(**kwargs)

    def test_membership_command_is_direct_and_bounded(self):
        groups = owned.OwnedHostProcesses(ps_timeout=.25)
        with mock.patch.object(owned.subprocess, 'run', return_value=
                               subprocess.CompletedProcess([], 0, '9 9 Z\n10 9 S\n')) as run:
            self.assertEqual(groups.live_members(9), [10])
        run.assert_called_once_with(['/bin/ps', '-axo', 'pid=,pgid=,stat='],
                                    check=True, capture_output=True, text=True, timeout=.25)

    def test_membership_probe_failures_cannot_be_empty_success(self):
        groups = owned.OwnedHostProcesses()
        for error in (subprocess.TimeoutExpired('ps', 2),
                      subprocess.CalledProcessError(1, 'ps'), OSError('unavailable')):
            with self.subTest(error=type(error)), mock.patch.object(
                    owned.subprocess, 'run', side_effect=error), self.assertRaises(
                        owned.OwnedProcessError):
                groups.live_members(9)


class OwnershipTest(unittest.TestCase):
    def setUp(self):
        self.groups = owned.OwnedHostProcesses(term_timeout=0, kill_timeout=0)
        self.process = mock.Mock(pid=1234, reaped=False, _identity_lost=False)
        self.process._internal_poll.return_value = 0
        self.groups.owned[self.process.pid] = self.process

    def test_group_configuration_cannot_be_overridden(self):
        for name, value in (('start_new_session', False), ('preexec_fn', None),
                            ('process_group', 0)):
            with self.subTest(name=name), mock.patch.object(owned, 'RetainedPopen') as spawn:
                with self.assertRaises(owned.OwnedProcessError):
                    self.groups.spawn(['unused'], **{name: value})
                spawn.assert_not_called()

    def test_recycled_id_unowned_and_reaped_never_receive_signals(self):
        for process in (mock.Mock(pid=1234), mock.Mock(pid=9999), self.process):
            if process is self.process:
                process.reaped = True
            with self.subTest(process=process), mock.patch.object(owned.os, 'killpg') as kill:
                with self.assertRaises(owned.OwnedProcessError):
                    self.groups.stop(process)
                kill.assert_not_called()

    def test_identity_is_checked_before_every_signal_even_with_cached_exit(self):
        self.process._internal_poll.side_effect = owned.OwnedProcessError('lost')
        with mock.patch.object(owned.os, 'killpg') as kill:
            with self.assertRaises(owned.OwnedProcessError):
                self.groups._signal(self.process, signal.SIGKILL)
            kill.assert_not_called()

    def test_no_signal_occurs_after_reap(self):
        events = []
        def kill(group, sig):
            self.assertFalse(self.process.reaped)
            events.append(('signal', sig))
        def reap():
            self.process.reaped = True
            events.append(('reap', None))
        self.process._reap_after_cleanup.side_effect = reap
        with mock.patch.object(owned.os, 'killpg', side_effect=kill), mock.patch.object(
                self.groups, 'live_members', return_value=[]):
            self.groups.stop(self.process)
            with self.assertRaises(owned.OwnedProcessError):
                self.groups.stop(self.process)
            self.groups.close()
        self.assertEqual(events, [('signal', signal.SIGTERM), ('reap', None)])
        self.assertFalse(self.groups.owned)

    def test_nonempty_deadline_retains_leader_and_ownership(self):
        with mock.patch.object(owned.os, 'killpg') as kill, mock.patch.object(
                self.groups, 'live_members', return_value=[1235]):
            with self.assertRaises(owned.OwnedProcessError):
                self.groups.stop(self.process)
        self.assertEqual(kill.call_args_list, [mock.call(1234, signal.SIGTERM),
                                               mock.call(1234, signal.SIGKILL)])
        self.process._reap_after_cleanup.assert_not_called()
        self.assertIs(self.groups.owned[1234], self.process)

    def test_unknown_owned_member_cannot_allow_false_empty_cleanup(self):
        with mock.patch.object(owned.os, 'killpg') as kill, \
                mock.patch.object(owned.subprocess, 'run', return_value=
                    subprocess.CompletedProcess([], 0, '1234 1234 Z\n1235 1234 ?E\n')):
            with self.assertRaises(owned.OwnedProcessError):
                self.groups.stop(self.process)
        self.assertEqual(kill.call_args_list, [mock.call(1234, signal.SIGTERM),
                                               mock.call(1234, signal.SIGKILL)])
        self.process._reap_after_cleanup.assert_not_called()
        self.assertIs(self.groups.owned[1234], self.process)

    def test_transient_invalid_snapshot_requires_fresh_owned_empty_evidence(self):
        self.groups.term_timeout = .2
        with mock.patch.object(owned.os, 'killpg') as kill, \
                mock.patch.object(owned.time, 'sleep') as sleep, \
                mock.patch.object(owned.time, 'monotonic', side_effect=[0, .1]), \
                mock.patch.object(owned.subprocess, 'run', side_effect=[
                    subprocess.CompletedProcess([], 0, 'invalid snapshot'),
                    subprocess.CompletedProcess([], 0, '1234 1234 Z\n')]) as probe:
            self.groups.stop(self.process)
        self.assertEqual(probe.call_count, 2)
        sleep.assert_called_once()
        # stop(), _signal(), and both membership attempts each recheck waitid.
        self.assertEqual(self.process._internal_poll.call_count, 4)
        kill.assert_called_once_with(1234, signal.SIGTERM)
        self.process._reap_after_cleanup.assert_called_once()
        self.assertFalse(self.groups.owned)

    def test_persistent_invalid_snapshot_expires_without_reaping_or_escalating(self):
        self.groups.term_timeout = .2
        with mock.patch.object(owned.os, 'killpg') as kill, \
                mock.patch.object(owned.time, 'sleep') as sleep, \
                mock.patch.object(owned.time, 'monotonic', side_effect=[0, .1, .2]), \
                mock.patch.object(owned.subprocess, 'run', return_value=
                    subprocess.CompletedProcess([], 0, 'invalid snapshot')) as probe:
            with self.assertRaises(owned.MembershipSnapshotError):
                self.groups.stop(self.process)
        self.assertEqual(probe.call_count, 2)
        sleep.assert_called_once_with(self.groups.poll_interval)
        self.assertEqual(self.process._internal_poll.call_count, 4)
        kill.assert_called_once_with(1234, signal.SIGTERM)
        self.process._reap_after_cleanup.assert_not_called()
        self.assertIs(self.groups.owned[1234], self.process)

    def test_identity_loss_after_invalid_snapshot_stops_before_another_probe_or_signal(self):
        self.groups.term_timeout = .2
        self.process._internal_poll.side_effect = [0, 0, 0,
            owned.OwnedProcessError('retained identity lost')]
        with mock.patch.object(owned.os, 'killpg') as kill, \
                mock.patch.object(owned.time, 'sleep') as sleep, \
                mock.patch.object(owned.time, 'monotonic', side_effect=[0, .1]), \
                mock.patch.object(owned.subprocess, 'run', return_value=
                    subprocess.CompletedProcess([], 0, 'invalid snapshot')) as probe:
            with self.assertRaisesRegex(owned.OwnedProcessError, 'retained identity lost'):
                self.groups.stop(self.process)
        probe.assert_called_once()
        sleep.assert_called_once()
        kill.assert_called_once_with(1234, signal.SIGTERM)
        self.process._reap_after_cleanup.assert_not_called()
        self.assertIs(self.groups.owned[1234], self.process)

    def test_snapshot_command_failure_is_not_retried_as_invalid_membership(self):
        self.groups.term_timeout = .2
        with mock.patch.object(owned.os, 'killpg') as kill, \
                mock.patch.object(owned.time, 'sleep') as sleep, \
                mock.patch.object(owned.subprocess, 'run', side_effect=OSError('unavailable')) as probe:
            with self.assertRaisesRegex(owned.OwnedProcessError, 'Cannot inspect'):
                self.groups.stop(self.process)
        probe.assert_called_once()
        sleep.assert_not_called()
        kill.assert_called_once_with(1234, signal.SIGTERM)
        self.process._reap_after_cleanup.assert_not_called()
        self.assertIs(self.groups.owned[1234], self.process)

    def test_macos_zombie_only_eperm_requires_exit_and_fresh_empty_snapshot(self):
        with mock.patch.object(owned.platform, 'system', return_value='Darwin'), \
                mock.patch.object(owned.os, 'killpg', side_effect=PermissionError), \
                mock.patch.object(self.groups, 'live_members', return_value=[]) as members:
            self.groups._signal(self.process, signal.SIGTERM)
            members.assert_called_once_with(1234)
        for exit_code, members in ((None, []), (0, [1235])):
            self.process._internal_poll.return_value = exit_code
            with self.subTest(exit_code=exit_code, members=members), \
                    mock.patch.object(owned.platform, 'system', return_value='Darwin'), \
                    mock.patch.object(owned.os, 'killpg', side_effect=PermissionError), \
                    mock.patch.object(self.groups, 'live_members', return_value=members):
                with self.assertRaises(owned.OwnedProcessError):
                    self.groups._signal(self.process, signal.SIGKILL)

    def test_linux_permission_error_is_not_suppressed(self):
        with mock.patch.object(owned.platform, 'system', return_value='Linux'), \
                mock.patch.object(owned.os, 'killpg', side_effect=PermissionError), \
                mock.patch.object(self.groups, 'live_members', return_value=[]):
            with self.assertRaises(owned.OwnedProcessError):
                self.groups._signal(self.process, signal.SIGTERM)

    def test_missing_group_requires_exit_and_empty_snapshot(self):
        self.process._internal_poll.return_value = None
        with mock.patch.object(owned.os, 'killpg', side_effect=ProcessLookupError), \
                mock.patch.object(self.groups, 'live_members', return_value=[]):
            with self.assertRaises(owned.OwnedProcessError):
                self.groups._signal(self.process, signal.SIGTERM)

    def test_close_attempts_each_owned_group_and_keeps_failures(self):
        second = mock.Mock(pid=9999)
        self.groups.owned[9999] = second
        with mock.patch.object(self.groups, 'stop', side_effect=[
                owned.OwnedProcessError('first'), None]) as stop:
            with self.assertRaises(owned.OwnedProcessError):
                self.groups.close()
        self.assertEqual(stop.call_args_list, [mock.call(self.process), mock.call(second)])


@unittest.skipUnless(platform.system() in ('Darwin', 'Linux') and hasattr(os, 'WNOWAIT'),
                     'requires macOS/Linux waitid WNOWAIT')
class RealProcessTest(unittest.TestCase):
    def setUp(self):
        self.groups = owned.OwnedHostProcesses(term_timeout=.15, kill_timeout=3,
                                              poll_interval=.02)
        self.addCleanup(self.groups.close)

    def spawn_python(self, source, **kwargs):
        process = self.groups.spawn([sys.executable, '-c', source], **kwargs)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                self.addCleanup(stream.close)
        return process

    def assert_retained(self, process):
        status = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        self.assertIsNotNone(status)
        self.assertEqual(status.si_pid, process.pid)
        self.assertFalse(process.reaped)

    def test_communicate_natural_exit_retains_group_until_child_cleanup(self):
        source = '''import subprocess, sys
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
print(child.pid, flush=True)
sys.exit(7)
'''
        process = self.spawn_python(source, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True)
        output, error = process.communicate(timeout=5)
        child = int(output.strip())
        self.assertEqual(error, '')
        self.assertEqual(process.returncode, 7)
        self.assertEqual(process.poll(), 7)
        self.assertEqual(process.wait(timeout=0), 7)
        self.assert_retained(process)
        self.assertIn(child, self.groups.live_members(process.pid))
        self.groups.stop(process)
        self.assertTrue(process.reaped)
        self.assertNotIn(child, self.groups.live_members(process.pid))
        with self.assertRaises(ChildProcessError):
            os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        with mock.patch.object(owned.os, 'killpg') as kill:
            with self.assertRaises(owned.OwnedProcessError):
                self.groups.stop(process)
            kill.assert_not_called()

    def test_terminate_then_kill_uncooperative_live_leader(self):
        process = self.spawn_python(
            'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
            'print("ready",flush=True); time.sleep(60)',
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertTrue(select.select([process.stdout], [], [], 5)[0], 'Python readiness timeout')
        self.assertEqual(process.stdout.readline(), 'ready\n')
        with self.assertRaises(subprocess.TimeoutExpired):
            process.wait(timeout=.01)
        self.groups.stop(process)
        self.assertEqual(process.returncode, -signal.SIGKILL)
        self.assertTrue(process.reaped)
        self.assertEqual(self.groups.live_members(process.pid), [])

    def test_external_reap_is_detected_before_signalling_cached_exit(self):
        process = self.spawn_python('pass')
        self.assertEqual(process.wait(timeout=5), 0)
        self.assert_retained(process)
        self.assertEqual(os.waitpid(process.pid, 0)[0], process.pid)
        try:
            with mock.patch.object(owned.os, 'killpg') as kill:
                with self.assertRaises(owned.OwnedProcessError):
                    self.groups.stop(process)
                kill.assert_not_called()
            self.assertIs(self.groups.owned[process.pid], process)
        finally:
            # The test deliberately reaped this child; there is no group left.
            # Remove only this known-lost ownership record to avoid cleanup retry.
            self.groups.owned.pop(process.pid)
            process.reaped = True


if __name__ == '__main__':
    unittest.main()
