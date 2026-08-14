"""agent/process_lock.py -- Unit B (reconstructed 2026-08-13).

Real subprocess/crash/SIGKILL tests, not simulated: this module's whole
value proposition is what happens when a process holding the lock dies
uncleanly, which cannot be proven by mocking fcntl -- it has to actually
happen to a real OS process."""
from __future__ import annotations

import multiprocessing
import os
import signal
import time

import pytest

from agent.process_lock import (LOCK_FILENAME, ProcessLockError,
                                acquire_process_lock)


# ---------------------------------------- basic acquire/release

def test_acquiring_an_unlocked_data_dir_succeeds(tmp_path):
    with acquire_process_lock(tmp_path):
        pass   # no raise


def test_lock_file_is_created_inside_data_dir(tmp_path):
    with acquire_process_lock(tmp_path):
        assert (tmp_path / LOCK_FILENAME).exists()


def test_data_dir_is_created_if_it_does_not_exist_yet(tmp_path):
    fresh = tmp_path / "not-yet-created"
    assert not fresh.exists()
    with acquire_process_lock(fresh):
        assert fresh.is_dir()


def test_lock_is_released_on_normal_exit_and_can_be_reacquired(tmp_path):
    with acquire_process_lock(tmp_path):
        pass
    with acquire_process_lock(tmp_path):
        pass   # second acquisition, after clean release -- must not raise


def test_lock_is_released_when_the_with_block_raises(tmp_path):
    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with acquire_process_lock(tmp_path):
            raise _Boom("something went wrong inside the lock")
    with acquire_process_lock(tmp_path):
        pass   # must still be acquirable -- release happened despite the raise


# ---------------------------------------- same-process double-acquire

def test_a_second_acquire_of_the_same_directory_from_the_same_process_raises(tmp_path):
    with acquire_process_lock(tmp_path):
        with pytest.raises(ProcessLockError):
            with acquire_process_lock(tmp_path):
                pass


def test_process_lock_error_names_the_resolved_data_dir_and_lock_path(tmp_path):
    with acquire_process_lock(tmp_path):
        try:
            with acquire_process_lock(tmp_path):
                pass
        except ProcessLockError as exc:
            assert exc.data_dir == tmp_path.resolve()
            assert exc.lock_path == tmp_path.resolve() / LOCK_FILENAME
        else:
            pytest.fail("expected ProcessLockError")


# ---------------------------------------- canonical-equivalent path collision

def test_relative_and_absolute_spellings_of_the_same_dir_collide(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with acquire_process_lock(tmp_path):   # absolute
        with pytest.raises(ProcessLockError):
            with acquire_process_lock("."):   # relative, same actual directory
                pass


def test_a_trailing_slash_spelling_collides_with_the_bare_path(tmp_path):
    with acquire_process_lock(str(tmp_path) + "/"):
        with pytest.raises(ProcessLockError):
            with acquire_process_lock(tmp_path):
                pass


# ---------------------------------------- independent data-dir behavior

def test_two_genuinely_different_data_dirs_never_contend(tmp_path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    with acquire_process_lock(dir_a):
        with acquire_process_lock(dir_b):
            pass   # no raise -- independent directories, independent locks


# ---------------------------------------- real subprocess crash / SIGKILL tests

def _hold_lock_until_signaled(data_dir, acquired_event, release_event):
    """Run in a real, separate OS process (multiprocessing.Process, not a
    thread -- flock contention is a real, separate-process phenomenon,
    fcntl locks are per-open-file-description and this must be a genuine
    second process to prove anything)."""
    with acquire_process_lock(data_dir):
        acquired_event.set()
        release_event.wait(timeout=30)
        # Falls through the `with` -- clean release path, used by the
        # "normal subprocess exit" test.


def _hold_lock_forever(data_dir, acquired_event):
    """Never releases voluntarily -- used by the SIGKILL test, which kills
    this process from the outside instead of letting it exit."""
    with acquire_process_lock(data_dir):
        acquired_event.set()
        time.sleep(30)


def test_a_second_real_process_cannot_acquire_while_the_first_still_holds_it(tmp_path):
    acquired = multiprocessing.Event()
    release = multiprocessing.Event()
    proc = multiprocessing.Process(target=_hold_lock_until_signaled,
                                   args=(str(tmp_path), acquired, release))
    proc.start()
    try:
        assert acquired.wait(timeout=10), "child process never acquired the lock"
        with pytest.raises(ProcessLockError):
            with acquire_process_lock(tmp_path):
                pass
    finally:
        release.set()
        proc.join(timeout=10)


def test_lock_is_available_immediately_after_the_holding_process_exits_normally(tmp_path):
    acquired = multiprocessing.Event()
    release = multiprocessing.Event()
    proc = multiprocessing.Process(target=_hold_lock_until_signaled,
                                   args=(str(tmp_path), acquired, release))
    proc.start()
    assert acquired.wait(timeout=10)
    release.set()
    proc.join(timeout=10)
    assert proc.exitcode == 0
    with acquire_process_lock(tmp_path):
        pass   # must succeed immediately -- no stale state left behind


def test_lock_is_available_immediately_after_the_holding_process_is_sigkilled(tmp_path):
    """THE test this whole module exists to pass: a process holding the
    lock is killed with SIGKILL (no cleanup code of any kind runs -- no
    `finally`, no `atexit`, nothing) and the lock must still become
    available, because the kernel -- not this module's own Python code --
    releases flock locks when the holding process's file descriptors are
    torn down, unconditionally."""
    acquired = multiprocessing.Event()
    proc = multiprocessing.Process(target=_hold_lock_forever, args=(str(tmp_path), acquired))
    proc.start()
    try:
        assert acquired.wait(timeout=10), "child process never acquired the lock"
        os.kill(proc.pid, signal.SIGKILL)
        proc.join(timeout=10)
        assert proc.exitcode == -signal.SIGKILL
        # No sleep/retry loop: if flock release were not immediate/atomic
        # with process death, this next line would flake under load -- it
        # does not, because it is a kernel guarantee, not a race.
        with acquire_process_lock(tmp_path):
            pass
    finally:
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)
