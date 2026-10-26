import os

import pytest

from jzbot.infra import instance_lock


def test_same_path_rejected_then_reusable(tmp_path):
    path = tmp_path / ".instance.lock"
    first = instance_lock.SingleInstanceLock(path)
    second = instance_lock.SingleInstanceLock(path)

    first.acquire()
    try:
        with pytest.raises(instance_lock.AlreadyRunningError):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    assert second.acquired
    second.release()


def test_linux_flock_nonblocking_semantics(tmp_path, monkeypatch):
    class FakeFcntl:
        LOCK_EX = 1
        LOCK_NB = 2
        LOCK_UN = 4
        locked = False
        acquire_flags = []

        @classmethod
        def flock(cls, file_descriptor, flags):
            if flags == cls.LOCK_UN:
                cls.locked = False
                return
            cls.acquire_flags.append(flags)
            if cls.locked:
                raise BlockingIOError("locked")
            cls.locked = True

    monkeypatch.setattr(instance_lock, "_fcntl", FakeFcntl)
    monkeypatch.setattr(instance_lock, "_msvcrt", None)
    first = instance_lock.SingleInstanceLock(tmp_path / "linux.lock")
    second = instance_lock.SingleInstanceLock(tmp_path / "linux.lock")

    first.acquire()
    with pytest.raises(instance_lock.AlreadyRunningError):
        second.acquire()
    assert FakeFcntl.acquire_flags == [
        FakeFcntl.LOCK_EX | FakeFcntl.LOCK_NB,
        FakeFcntl.LOCK_EX | FakeFcntl.LOCK_NB,
    ]
    first.release()
    second.acquire()
    second.release()


def test_windows_import_has_no_fcntl_dependency():
    # This suite itself imports the module on Windows.  On Linux the same
    # assertion documents that the conditional branch remains platform-aware.
    if os.name == "nt":
        assert instance_lock._fcntl is None
        assert instance_lock._msvcrt is not None
    assert instance_lock.SingleInstanceLock is not None
