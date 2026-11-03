"""Receipt ordering and injected sync failures; these are not power-loss tests."""

import errno
import os
import stat

import pytest

from agent_dispatch import handoffs
from agent_dispatch.handoffs import Handoffs
from fakes import FakeHub, FakeHerdr
from test_dispatch import TASK, dispatcher


def test_file_sync_then_rename_then_receipt_directory_sync(tmp_path, monkeypatch):
    store = Handoffs(tmp_path)
    store.directory.mkdir()
    inode = store.directory.stat().st_ino
    namespace_inode = store.directory.parent.stat().st_ino
    events = []
    sync, replace = os.fsync, os.replace

    def fsync(fd):
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode):
            events.append("file-sync")
        elif info.st_ino == inode:
            events.append("receipt-directory-sync")
        elif info.st_ino == namespace_inode:
            events.append("namespace-sync")
        return sync(fd)

    def rename(*args):
        events.append("rename")
        return replace(*args)

    monkeypatch.setattr(handoffs.os, "fsync", fsync)
    monkeypatch.setattr(handoffs.os, "replace", rename)
    store.write(TASK, "delivering")
    assert (
        events.index("file-sync") < events.index("rename") < events.index("receipt-directory-sync")
    )
    assert events.index("namespace-sync") < events.index("file-sync")
    assert store.read(TASK) == "delivering"


@pytest.mark.parametrize("error", [errno.EIO, errno.EINVAL, errno.ENOTSUP])
def test_uncertain_receipt_directory_sync_failure_blocks_prompt_and_retry(
    tmp_path, monkeypatch, error
):
    fake = FakeHub()
    fake.request(TASK)
    herdr = FakeHerdr()
    d, _, _ = dispatcher(tmp_path, fake, herdr=herdr)
    sync = os.fsync

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode) and d.handoffs.read(TASK) == "delivering":
            raise OSError(error, "injected receipt directory sync failure")
        return sync(fd)

    monkeypatch.setattr(handoffs.os, "fsync", fsync)
    with pytest.raises(OSError):
        d.reconcile()
    assert "agent prompt" not in herdr.verbs()
    assert fake.rows[TASK]["dispatch"] == "Requested" and not fake.patches
    monkeypatch.setattr(handoffs.os, "fsync", sync)
    resumed, _, _ = dispatcher(tmp_path, fake, herdr=herdr)
    resumed.reconcile()
    assert "agent prompt" not in herdr.verbs()
    assert fake.rows[TASK]["dispatch"] == "Requested"


def test_darwin_requires_full_sync_after_fsync(monkeypatch):
    events = []
    monkeypatch.setattr(handoffs.sys, "platform", "darwin")
    monkeypatch.setattr(handoffs.fcntl, "F_FULLFSYNC", 51, raising=False)
    monkeypatch.setattr(handoffs.os, "fsync", lambda fd: events.append(("fsync", fd)))
    monkeypatch.setattr(handoffs.fcntl, "fcntl", lambda fd, op: events.append(("full", fd, op)))
    handoffs._sync(42)
    assert events == [("fsync", 42), ("full", 42, 51)]


@pytest.mark.parametrize("error", [errno.EINVAL, errno.ENOTSUP, errno.EIO])
def test_darwin_full_sync_errors_are_not_downgraded(monkeypatch, error):
    monkeypatch.setattr(handoffs.sys, "platform", "darwin")
    monkeypatch.setattr(handoffs.fcntl, "F_FULLFSYNC", 51, raising=False)
    monkeypatch.setattr(handoffs.os, "fsync", lambda fd: None)

    def fail(fd, op):
        raise OSError(error, "injected full sync failure")

    monkeypatch.setattr(handoffs.fcntl, "fcntl", fail)
    with pytest.raises(OSError) as caught:
        handoffs._sync(42)
    assert caught.value.errno == error


def test_other_posix_uses_fsync_without_darwin_operation(monkeypatch):
    events = []
    monkeypatch.setattr(handoffs.sys, "platform", "linux")
    monkeypatch.setattr(handoffs.os, "fsync", lambda fd: events.append(fd))

    def forbidden(*args):
        raise AssertionError("Darwin operation on another platform")

    monkeypatch.setattr(handoffs.fcntl, "fcntl", forbidden)
    handoffs._sync(42)
    assert events == [42]


def test_directory_descriptor_closes_after_sync_failure(tmp_path, monkeypatch):
    opened = []
    original = os.open

    def open_directory(*args):
        fd = original(*args)
        opened.append(fd)
        return fd

    def fail(fd):
        raise OSError(errno.EIO, "injected")

    monkeypatch.setattr(handoffs.os, "open", open_directory)
    monkeypatch.setattr(handoffs, "_sync", fail)
    with pytest.raises(OSError):
        handoffs._sync_directory(tmp_path)
    with pytest.raises(OSError) as caught:
        os.fstat(opened[0])
    assert caught.value.errno == errno.EBADF


def test_namespace_sync_failure_prevents_even_blank_agent_creation(tmp_path, monkeypatch):
    fake = FakeHub()
    fake.request(TASK)
    d, herdr, _ = dispatcher(tmp_path, fake)
    sync = os.fsync

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "namespace sync failed")
        return sync(fd)

    monkeypatch.setattr(handoffs.os, "fsync", fsync)
    with pytest.raises(OSError):
        d.reconcile()
    assert "tab create" not in herdr.verbs() and fake.patches == []
