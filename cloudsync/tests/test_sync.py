import logging
from io import BytesIO
import pytest
from typing import NamedTuple

from cloudsync import SyncManager, SyncState, CloudFileNotFoundError, LOCAL, REMOTE, FILE, DIRECTORY
from cloudsync.provider import Provider


class WaitFor(NamedTuple):
    side: int = None
    path: str = None
    hash: bytes = None
    oid: str = None
    exists: bool = True


log = logging.getLogger(__name__)

TIMEOUT = 2


class RunUntilHelper:
    def run_until_found(self: SyncManager, *files, timeout=TIMEOUT):
        log.debug("running until found")
        last_error = None

        def found():
            ok = True

            for info in files:
                if type(info) is tuple:
                    info = WaitFor(side=info[0], path=info[1])

                try:
                    other_info = self.providers[info.side].info_path(info.path)
                except CloudFileNotFoundError:
                    other_info = None

                if other_info is None:
                    nonlocal last_error
                    if info.exists is False:
                        log.debug("waiting not exists %s", info.path)
                        continue
                    log.debug("waiting exists %s", info.path)
                    last_error = CloudFileNotFoundError(info.path)
                    ok = False
                    break

                if info.exists is False:
                    ok = False
                    break

                if info.hash and info.hash != other_info.hash:
                    log.debug("waiting hash %s", info.path)
                    ok = False
                    break

            return ok

        self.run(timeout=timeout, until=found)

        if not found():
            if last_error:
                raise TimeoutError("timed out while waiting: %s" % last_error)
            else:
                raise TimeoutError("timed out while waiting")


class SyncMgrMixin(SyncManager, RunUntilHelper):
    pass


@pytest.fixture(name="sync")
def fixture_sync(mock_provider_generator):
    state = SyncState()

    def translate(to, path):
        if to == LOCAL:
            return "/local" + path.replace("/remote", "")

        if to == REMOTE:
            return "/remote" + path.replace("/local", "")

        raise ValueError()

    # two providers and a translation function that converts paths in one to paths in the other
    sync = SyncMgrMixin(state, (mock_provider_generator(), mock_provider_generator()), translate)

    yield sync

    sync.done()


def test_sync_state_basic():
    state = SyncState()
    state.update(LOCAL, FILE, path="foo", oid="123", hash=b"foo")

    assert state.lookup_path(LOCAL, path="foo")
    assert state.lookup_oid(LOCAL, oid="123")


def test_sync_state_rename():
    state = SyncState()
    state.update(LOCAL, FILE, path="foo", oid="123", hash=b"foo")
    state.update(LOCAL, FILE, path="foo2", oid="123")
    assert state.lookup_path(LOCAL, path="foo2")
    assert not state.lookup_path(LOCAL, path="foo")


def test_sync_state_multi():
    state = SyncState()
    state.update(LOCAL, FILE, path="foo2", oid="123")
    assert state.lookup_path(LOCAL, path="foo2")
    assert not state.lookup_path(LOCAL, path="foo")


def test_sync_basic(sync: "SyncMgrMixin"):
    remote_parent = "/remote"
    local_parent = "/local"
    remote_path1 = Provider.join(remote_parent, "stuff1")
    local_path1 = sync.translate(LOCAL, remote_path1)
    local_path1.replace("\\", "/")
    assert local_path1 == "/local/stuff1"
    Provider.join(local_parent, "stuff2")  # "/local/stuff2"
    remote_path2 = Provider.join(remote_parent, "stuff2")  # "/remote/stuff2"

    sync.providers[LOCAL].mkdir(local_parent)
    sync.providers[REMOTE].mkdir(remote_parent)
    linfo = sync.providers[LOCAL].create(local_path1, BytesIO(b"hello"))

    # inserts info about some local path
    sync.syncs.update(LOCAL, FILE, path=local_path1,
                      oid=linfo.oid, hash=linfo.hash)

    sync.syncs.update(LOCAL, FILE, oid=linfo.oid, exists=True)

    assert sync.syncs.entry_count() == 1

    rinfo = sync.providers[REMOTE].create(remote_path2, BytesIO(b"hello2"))

    # inserts info about some cloud path
    sync.syncs.update(REMOTE, FILE, oid=rinfo.oid,
                      path=remote_path2, hash=rinfo.hash)

    def done():
        has_info = [None] * 4
        try:
            has_info[0] = sync.providers[LOCAL].info_path("/local/stuff1")
            has_info[1] = sync.providers[LOCAL].info_path("/local/stuff2")
            has_info[2] = sync.providers[REMOTE].info_path("/remote/stuff2")
            has_info[3] = sync.providers[REMOTE].info_path("/remote/stuff2")
        except CloudFileNotFoundError as e:
            log.debug("waiting for %s", e)
            pass

        return all(has_info)

    # loop the sync until the file is found
    sync.run(timeout=TIMEOUT, until=done)

    assert done()

    info = sync.providers[LOCAL].info_path("/local/stuff2")
    assert info.hash == sync.providers[LOCAL].hash_oid(info.oid)
    assert info.oid
    log.debug("all syncs %s", sync.syncs.get_all())


def test_sync_rename(sync):
    remote_parent = "/remote"
    local_parent = "/local"
    local_path1 = Provider.join(local_parent, "stuff1")  # "/local/stuff1"
    local_path2 = Provider.join(local_parent, "stuff2")  # "/local/stuff2"
    remote_path1 = Provider.join(remote_parent, "stuff1")  # "/remote/stuff1"
    remote_path2 = Provider.join(remote_parent, "stuff2")  # "/remote/stuff2"

    sync.providers[LOCAL].mkdir(local_parent)
    sync.providers[REMOTE].mkdir(remote_parent)
    linfo = sync.providers[LOCAL].create(local_path1, BytesIO(b"hello"))

    # inserts info about some local path
    sync.syncs.update(LOCAL, FILE, path=local_path1,
                      oid=linfo.oid, hash=linfo.hash)

    sync.run_until_found((REMOTE, remote_path1))

    sync.syncs.update(LOCAL, FILE, path=local_path2,
                      oid=linfo.oid, hash=linfo.hash)

    sync.run_until_found((REMOTE, remote_path2))

    assert sync.providers[REMOTE].info_path("/remote/stuff") is None


def test_sync_hash(sync):
    remote_parent = "/remote"
    local_parent = "/local"
    local_path1 = "/local/stuff1"
    remote_path1 = "/remote/stuff1"

    sync.providers[LOCAL].mkdir(local_parent)
    sync.providers[REMOTE].mkdir(remote_parent)
    linfo = sync.providers[LOCAL].create(local_path1, BytesIO(b"hello"))

    # inserts info about some local path
    sync.syncs.update(LOCAL, FILE, path=local_path1,
                      oid=linfo.oid, hash=linfo.hash)

    sync.run_until_found((REMOTE, remote_path1))

    linfo = sync.providers[LOCAL].upload(linfo.oid, BytesIO(b"hello2"))

    sync.syncs.update(LOCAL, FILE, linfo.oid, hash=linfo.hash)

    sync.run_until_found(WaitFor(REMOTE, remote_path1, hash=linfo.hash))

    info = sync.providers[REMOTE].info_path(remote_path1)

    check = BytesIO()
    sync.providers[REMOTE].download(info.oid, check)

    assert check.getvalue() == b"hello2"


def test_sync_rm(sync):
    remote_parent = "/remote"
    local_parent = "/local"
    local_path1 = Provider.join(local_parent, "stuff1")  # "/local/stuff1"
    remote_path1 = Provider.join(remote_parent, "stuff1")  # "/remote/stuff1"

    sync.providers[LOCAL].mkdir(local_parent)
    sync.providers[REMOTE].mkdir(remote_parent)
    linfo = sync.providers[LOCAL].create(local_path1, BytesIO(b"hello"))

    # inserts info about some local path
    sync.syncs.update(LOCAL, FILE, path=local_path1,
                      oid=linfo.oid, hash=linfo.hash)

    sync.run_until_found((REMOTE, remote_path1))

    sync.providers[LOCAL].delete(linfo.oid)
    sync.syncs.update(LOCAL, FILE, linfo.oid, exists=False)

    sync.run_until_found(WaitFor(REMOTE, remote_path1, exists=False))

    assert sync.providers[REMOTE].info_path(remote_path1) is None


def test_sync_mkdir(sync):
    local_dir1 = "/local"
    local_path1 = "/local/stuff"
    remote_dir1 = "/remote"
    remote_path1 = "/remote/stuff"

    local_dir_oid1 = sync.providers[LOCAL].mkdir(local_dir1)
    local_path_oid1 = sync.providers[LOCAL].mkdir(local_path1)

    # inserts info about some local path
    sync.syncs.update(LOCAL, DIRECTORY, path=local_dir1,
                      oid=local_dir_oid1)
    sync.syncs.update(LOCAL, DIRECTORY, path=local_path1,
                      oid=local_path_oid1)

    sync.run_until_found((REMOTE, remote_dir1))
    sync.run_until_found((REMOTE, remote_path1))

    log.debug("delete")
    sync.providers[LOCAL].delete(local_path_oid1)
    sync.syncs.update(LOCAL, FILE, local_path_oid1, exists=False)

    log.debug("wait for delete")
    sync.run_until_found(WaitFor(REMOTE, remote_path1, exists=False))

    assert sync.providers[REMOTE].info_path(remote_path1) is None


def test_sync_conflict_simul(sync):
    remote_parent = "/remote"
    local_parent = "/local"
    local_path1 = Provider.join(local_parent, "stuff1")  # "/local/stuff1"
    remote_path1 = Provider.join(remote_parent, "stuff1")  # "/remote/stuff1"

    sync.providers[LOCAL].mkdir(local_parent)
    sync.providers[REMOTE].mkdir(remote_parent)

    linfo = sync.providers[LOCAL].create(local_path1, BytesIO(b"hello"))
    rinfo = sync.providers[REMOTE].create(remote_path1, BytesIO(b"goodbye"))

    # inserts info about some local path
    sync.syncs.update(LOCAL, FILE, path=local_path1,
                      oid=linfo.oid, hash=linfo.hash)
    sync.syncs.update(REMOTE, FILE, path=remote_path1,
                      oid=rinfo.oid, hash=rinfo.hash)

    sync.run_until_found(
            (REMOTE, "/remote/stuff1.conflicted"),
            (LOCAL, "/local/stuff1.conflicted"),
            (REMOTE, "/remote/stuff1"),
            (LOCAL, "/local/stuff1")
            )

    sync.providers[LOCAL].log_debug_state("LOCAL")
    sync.providers[REMOTE].log_debug_state("REMOTE")

    b1 = BytesIO()
    b2 = BytesIO()
    sync.providers[REMOTE].download_path("/remote/stuff1.conflicted", b1)
    sync.providers[REMOTE].download_path("/remote/stuff1", b2)

    # both files are intact
    assert b1.getvalue() != b2.getvalue()
    assert b1.getvalue() in (b"hello", b"goodbye")
    assert b2.getvalue() in (b"hello", b"goodbye")


def test_sync_conflict_path(sync):
    remote_parent = "/remote"
    local_parent = "/local"
    local_path1 = "/local/stuff"
    remote_path1 = "/remote/stuff"
    local_path2 = "/local/stuff-l"
    remote_path2 = "/remote/stuff-r"

    sync.providers[LOCAL].mkdir(local_parent)
    sync.providers[REMOTE].mkdir(remote_parent)

    linfo = sync.providers[LOCAL].create(local_path1, BytesIO(b"hello"))

    # inserts info about some local path
    sync.syncs.update(LOCAL, FILE, path=local_path1,
                      oid=linfo.oid, hash=linfo.hash)

    sync.run_until_found((REMOTE, remote_path1))

    rinfo = sync.providers[REMOTE].info_path(remote_path1)

    assert len(sync.syncs.get_all()) == 1

    ent = sync.syncs.get_all().pop()

    sync.providers[REMOTE].log_debug_state("BEFORE")

    sync.providers[LOCAL].rename(linfo.oid, local_path2)
    sync.providers[REMOTE].rename(rinfo.oid, remote_path2)

    sync.providers[REMOTE].log_debug_state("AFTER")

    sync.syncs.update(LOCAL, FILE, path=local_path2,
                      oid=linfo.oid, hash=linfo.hash)

    assert len(sync.syncs.get_all()) == 1
    assert ent[REMOTE].oid == rinfo.oid

    sync.syncs.update(REMOTE, FILE, path=remote_path2,
                      oid=rinfo.oid, hash=rinfo.hash)

    assert len(sync.syncs.get_all()) == 1

    # currently defers to the alphabetcially greater name, rather than conflicting
    sync.run_until_found((LOCAL, "/local/stuff-r"))

    assert not sync.providers[LOCAL].exists_path(local_path1)
    assert not sync.providers[LOCAL].exists_path(local_path2)


def test_sync_cycle(sync):
    sync: SyncMgrMixin
    l_parent = "/local"
    r_parent = "/remote"
    lp1, lp2, lp3 = "/local/a", "/local/b", "/local/c",
    rp1, rp2, rp3 = "/remote/a", "/remote/b", "/remote/c",
    templ = "/local/d"

    sync.providers[LOCAL].mkdir(l_parent)
    sync.providers[REMOTE].mkdir(r_parent)

    linfo1 = sync.providers[LOCAL].create(lp1, BytesIO(b"hello1"))
    sync.syncs.update(LOCAL, FILE, path=lp1, oid=linfo1.oid, hash=linfo1.hash)
    sync.run_until_found((REMOTE, rp1), timeout=1)
    rinfo1 = sync.providers[REMOTE].info_path(rp1)

    linfo2 = sync.providers[LOCAL].create(lp2, BytesIO(b"hello2"))
    sync.syncs.update(LOCAL, FILE, path=lp2, oid=linfo2.oid, hash=linfo2.hash)
    sync.run_until_found((REMOTE, rp2))
    rinfo2 = sync.providers[REMOTE].info_path(rp2)

    linfo3 = sync.providers[LOCAL].create(lp3, BytesIO(b"hello3"))
    sync.syncs.update(LOCAL, FILE, path=lp3, oid=linfo3.oid, hash=linfo3.hash)
    sync.run_until_found((REMOTE, rp3))
    rinfo3 = sync.providers[REMOTE].info_path(rp3)

    sync.providers[REMOTE].log_debug_state("BEFORE")
    sync.providers[LOCAL].rename(linfo1.oid, templ)
    sync.providers[LOCAL].rename(linfo3.oid, lp1)
    sync.providers[LOCAL].rename(linfo2.oid, lp3)
    sync.providers[LOCAL].rename(linfo1.oid, lp2)

    sync.syncs.update(LOCAL, FILE, path=lp2, oid=linfo1.oid, hash=linfo1.hash)
    sync.syncs.update(LOCAL, FILE, path=lp3, oid=linfo2.oid, hash=linfo2.hash)
    sync.syncs.update(LOCAL, FILE, path=lp1, oid=linfo3.oid, hash=linfo3.hash)
    assert len(sync.syncs.get_all()) == 3
    sync.providers[REMOTE].log_debug_state("MIDDLE")

    sync.run(until=lambda:not sync.syncs.has_changes(), timeout=1)
    sync.providers[REMOTE].log_debug_state("AFTER")
