import os
import time
import logging
import tempfile
import shutil
import random
import json
from hashlib import md5
from base64 import b64encode
from enum import Enum
from typing import Union
from abc import ABC, abstractmethod

from typing import Optional, Tuple, Any, List, Dict, Set
from cloudsync.provider import Provider
from cloudsync.types import OType

__all__ = ['SyncManager', 'SyncState', 'SyncEntry', 'Storage', 'LOCAL', 'REMOTE', 'FILE', 'DIRECTORY']

from cloudsync.exceptions import CloudFileNotFoundError, CloudFileExistsError
from cloudsync.types import DIRECTORY, FILE

from .runnable import Runnable

log = logging.getLogger(__name__)

# useful for converting oids and pointer nubmers into digestible nonces


def debug_sig(t, size=3):
    if not t:
        return 0
    return b64encode(md5(str(t).encode()).digest()).decode()[0:size]

# adds a repr to some classes


class Reprable:                                     # pylint: disable=too-few-public-methods
    def __repr__(self):
        return self.__class__.__name__ + ":" + debug_sig(id(self)) + str(self.__dict__)

# safe ternary, don't allow traditional comparisons


class Exists(Enum):
    UNKNOWN = None
    EXISTS = True
    TRASHED = False

    def __bool__(self):
        raise ValueError("never bool enums")


UNKNOWN = Exists.UNKNOWN
EXISTS = Exists.EXISTS
TRASHED = Exists.TRASHED


# state of a single object
class SideState(Reprable):                          # pylint: disable=too-few-public-methods
    def __init__(self, side: int):
        self.side: int = side                            # just for assertions
        self.hash: Optional[bytes] = None           # hash at provider
        # time of last change (we maintain this)
        self.changed: Optional[float] = None
        self.sync_hash: Optional[bytes] = None      # hash at last sync
        self.sync_path: Optional[str] = None        # path at last sync
        self.path: Optional[str] = None             # path at provider
        self.oid: Optional[str] = None              # oid at provider
        self._exists: Exists = UNKNOWN               # exists at provider

    @property
    def exists(self):
        return self._exists

    # allow traditional sets of ternary
    @exists.setter
    def exists(self, val: Union[bool, Exists]):
        if val is False:
            val = TRASHED
        if val is True:
            val = EXISTS
        if val is None:
            val = UNKNOWN

        if type(val) != Exists:
            raise ValueError("use enum for exists")

        self._exists = val


# these are not really local or remote
# but it's easier to reason about using these labels
LOCAL = 0
REMOTE = 1
FINISHED = 1
REQUEUE = 0


def other_side(index):
    return 1-index


class Storage(ABC):
    @abstractmethod
    def create(self, tag: str, serialization: bytes) -> Any:
        """ take a serialization str, upsert it in sqlite, return the row id of the row as a persistence id"""
        ...

    @abstractmethod
    def update(self, tag: str, serialization: bytes, eid: Any):
        """ take a serialization str, upsert it in sqlite, return the row id of the row as a persistence id"""
        ...

    @abstractmethod
    def delete(self, tag: str, eid: Any):
        """ take a serialization str, upsert it in sqlite, return the row id of the row as a persistence id"""
        ...

    @abstractmethod
    def read_all(self, tag: str) -> Dict[Any, bytes]:
        """yield all the serialized strings in a generator"""
        ...


# single entry in the syncs state collection
class SyncEntry(Reprable):
    def __init__(self, otype: Optional[OType], storage_init: Optional[Tuple[Any, bytes]] = None):
        super().__init__()
        self.__states: List[SideState] = [SideState(0), SideState(1)]
        self.otype: OType = otype
        self.temp_file: Optional[str] = None
        self.discarded: bool = False
        self.storage_id: Any = None
        self.dirty: bool = True
        if storage_init is not None:
            self.storage_id = storage_init[0]
            self.deserialize(storage_init)
            self.dirty = False

    def serialize(self) -> bytes:
        """converts SyncEntry into a json str"""
        def side_state_to_dict(side_state: SideState) -> dict:
            ret = dict()
            ret['side'] = side_state.side
            ret['hash'] = side_state.hash.hex() if isinstance(side_state.hash, bytes) else None
            ret['changed'] = side_state.changed
            ret['sync_hash'] = side_state.sync_hash.hex() if isinstance(side_state.sync_hash, bytes) else None
            ret['path'] = side_state.path
            ret['sync_path'] = side_state.sync_path
            ret['oid'] = side_state.oid
            ret['exists'] = side_state.exists.value
            # storage_id does not get serialized, it always comes WITH a serialization when deserializing
            return ret

        ser = dict()
        ser['side0'] = side_state_to_dict(self.__states[0])
        ser['side1'] = side_state_to_dict(self.__states[1])
        ser['otype'] = self.otype.value
        ser['temp_file'] = self.temp_file
        ser['discarded'] = self.discarded
        return json.dumps(ser).encode('utf-8')

    def deserialize(self, storage_init: Tuple[Any, bytes]):
        """loads the values in the serialization dict into self"""
        def dict_to_side_state(side, side_dict: dict) -> SideState:
            side_state = SideState(side)
            side_state.side = side_dict['side']
            side_state.hash = bytes.fromhex(side_dict['hash']) if side_dict['hash'] else None
            side_state.changed = side_dict['changed']
            side_state.sync_hash = bytes.fromhex(side_dict['sync_hash']) if side_dict['sync_hash'] else None
            side_state.sync_path = side_dict['sync_path']
            side_state.path = side_dict['path']
            side_state.oid = side_dict['oid']
            side_state.exists = side_dict['exists']
            return side_state

        self.storage_id = storage_init[0]
        ser: dict = json.loads(storage_init[1].decode('utf-8'))
        self.__states = [dict_to_side_state(0, ser['side0']),
                         dict_to_side_state(1, ser['side1'])]
        self.otype = OType(ser['otype'])
        self.temp_file = ser['temp_file']
        self.discarded = ser['discarded']

    def __getitem__(self, i):
        return self.__states[i]

    def __setitem__(self, i, val):
        assert type(val) is SideState
        assert val.side is None or val.side == i
        self.__states[i] = val
        self.dirty = True

    def get_latest_state(self, providers):
        #        log.debug("before update state %s", self)
        for i in (LOCAL, REMOTE):
            if self[i].changed:
                # get latest info from provider
                if self.otype == FILE:
                    self[i].hash = providers[i].hash_oid(self[i].oid)
                    self[i].exists = EXISTS if self[i].hash else TRASHED
                else:
                    self[i].exists = providers[i].exists_oid(self[i].oid)
                self.dirty = True
    #        log.debug("after update state %s", self)

    def hash_conflict(self):
        if self[0].hash and self[1].hash:
            return self[0].hash != self[0].sync_hash and self[1].hash != self[1].sync_hash
        return False

    def path_conflict(self):
        if self[0].path and self[1].path:
            return self[0].path != self[0].sync_path and self[1].path != self[1].sync_path
        return False

    def is_path_change(self, changed):
        return self[changed].path != self[changed].sync_path

    def is_creation(self, changed):
        return not self[changed].sync_path

    def discard(self):
        self.discarded = True
        self.dirty = True

    def pretty(self):
        if self.discarded:
            return "DISCARDED"

        def secs(t):
            if t:
                return str(round(t % 300, 3)).replace(".", "")
            else:
                return 0

        ret = "S%3s I%3s T%5s C%6s P%20s O%6s SPE%20s D%1s C%6s P%20s O%16s SPE%s" % (
            debug_sig(id(self)),  # S
            self.storage_id,  # I
            self.otype.value,  # T
            secs(self[LOCAL].changed),  # C
            self[LOCAL].path,  # P
            debug_sig(self[LOCAL].oid),  # O
            str(self[LOCAL].sync_path) + ":" + str(self[LOCAL].exists.value),  # SPE
            "T" if self.discarded else "F",  # D
            secs(self[REMOTE].changed),  # C
            self[REMOTE].path,  # P
            debug_sig(self[REMOTE].oid),  # O
            str(self[REMOTE].sync_path) + ":" + str(self[REMOTE].exists.value)  # SPE
        )

        return ret


class SyncState:
    def __init__(self, storage: Optional[Storage] = None, tag: Optional[str] = None):
        self._oids = ({}, {})
        self._paths = ({}, {})
        self._changeset = set()
        self._storage: Optional[Storage] = storage
        self._tag = tag
        if self._storage:
            storage_dict = self._storage.read_all(tag)
            for eid, ent_ser in storage_dict.items():
                ent = SyncEntry(None, (eid, ent_ser))
                for side in [LOCAL, REMOTE]:
                    path, oid = ent[side].path, ent[side].oid
                    if path not in self._paths[side]:
                        self._paths[side][path] = {}
                    self._paths[side][path][oid] = ent
                    self._oids[side][oid] = ent

    def _change_path(self, side, ent, path):
        assert type(ent) is SyncEntry
        assert ent[side].oid

        if ent[side].path:
            if ent[side].path in self._paths[side]:
                self._paths[side][ent[side].path].pop(ent[side].oid, None)
            if not self._paths[side][ent[side].path]:
                del self._paths[side][ent[side].path]
        if path:
            if path not in self._paths[side]:
                self._paths[side][path] = {}
            self._paths[side][path][ent[side].oid] = ent
            ent[side].path = path
            ent.dirty = True

    def _change_oid(self, side, ent, oid):
        assert type(ent) is SyncEntry

        if ent[side].oid:
            self._oids[side].pop(ent[side].oid, None)
        if oid:
            self._oids[side][oid] = ent
            ent[side].oid = oid
            ent.dirty = True

    def lookup_oid(self, side, oid):
        try:
            return self._oids[side][oid]
        except KeyError:
            return []

    def lookup_path(self, side, path):
        try:
            return self._paths[side][path].values()
        except KeyError:
            return []

    def rename_dir(self, side, from_dir, to_dir, is_subpath, replace_path):
        """
        when a directory changes, utility to rename all kids
        """
        remove = []

        # TODO: refactor this so that a list of affected items is gathered, then the alterations happen to the final
        #    list, which will avoid having to remove after adding, which feels mildly risky
        # TODO: is this function called anywhere? ATM, it looks like no... It should be called or removed
        # TODO: it looks like this loop has a bug... items() does not return path, sub it returns path, Dict[oid, sub]
        for path, sub in self._paths[side].items():
            if is_subpath(from_dir, sub.path):
                sub.path = replace_path(sub.path, from_dir, to_dir)
                remove.append(path)
                self._paths[side][sub.path] = sub
                sub.dirty = True

        for path in remove:
            self._paths[side].pop(path)

    def update_entry(self, ent, side, oid, path=None, hash=None, exists=True):  # pylint: disable=redefined-builtin
        if oid is not None:
            self._change_oid(side, ent, oid)

        if path is not None:
            self._change_path(side, ent, path)

        if hash is not None:
            ent[side].hash = hash
            ent.dirty = True

        if exists is not None:
            ent[side].exists = exists
            ent.dirty = True

    def storage_update(self, ent: SyncEntry):
        log.debug("storage_update eid%s", ent.storage_id)
        if self._storage is not None:
            if ent.storage_id is not None:
                if ent.discarded:
                    log.debug("storage_update deleting eid%s", ent.storage_id)
                    self._storage.delete(self._tag, ent.storage_id)
                else:
                    self._storage.update(self._tag, ent.serialize(), ent.storage_id)
            else:
                assert not ent.discarded
                new_id = self._storage.create(self._tag, ent.serialize())
                ent.storage_id = new_id
                log.debug("storage_update creating eid%s", ent.storage_id)
            ent.dirty = False

    def __len__(self):
        return len(self.get_all())

    def update(self, side, otype, oid, path=None, hash=None, exists=True):   # pylint: disable=redefined-builtin
        ent = self.lookup_oid(side, oid)
        if not ent:
            log.debug("creating new entry because %s not found", debug_sig(oid))
            ent = SyncEntry(otype)
        self.update_entry(ent, side, oid, path, hash, exists)
        log.debug("event changed %s", ent)

        ent[side].changed = time.time()
        self._changeset.add(ent)
        self.storage_update(ent)

    def change(self):
        # for now just get a random one
        if self._changeset:
            ret = random.sample(self._changeset, 1)[0]
            if ret.discarded:
                self._changeset.remove(ret)
                return self.change()
            return ret
        return None

    def has_changes(self):
        return bool(self._changeset)

    def finished(self, ent):
        if ent[1].changed or ent[0].changed:
            log.info("not marking finished: %s", ent)
            return
        self._changeset.remove(ent)

    def pretty_print(self, ignore_dirs=False):
        ret = ""
        for e in self.get_all():
            e: SyncEntry
            if ignore_dirs:
                if e.otype == DIRECTORY:
                    continue
            if e.discarded:
                continue

            ret += e.pretty() + "\n"
        return ret

    def get_all(self, discarded=False) -> Set['SyncState']:
        ents = set()
        for ent in self._oids[LOCAL].values():
            assert ent
            if ent.discarded and not discarded:
                continue
            ents.add(ent)
        for ent in self._oids[REMOTE].values():
            assert ent
            if ent.discarded and not discarded:
                continue
            ents.add(ent)

        return ents

    def entry_count(self):
        return len(self.get_all())


class SyncManager(Runnable):
    def __init__(self, syncs, providers: Tuple[Provider, Provider], translate):
        self.syncs: SyncState = syncs
        self.providers = providers
        self.providers[LOCAL].debug_name = "local"
        self.providers[REMOTE].debug_name = "remote"
        self.translate = translate
        self.tempdir = tempfile.mkdtemp(suffix=".cloudsync")

        assert len(self.providers) == 2

    def do(self):
        sync: SyncEntry = self.syncs.change()
        if sync:
            log.debug("doing eid%s", sync.storage_id)
            self.sync(sync)
            self.syncs.storage_update(sync)

    def done(self):
        log.info("cleanup %s", self.tempdir)
        shutil.rmtree(self.tempdir)

    def sync(self, sync):
        log.debug("syncing eid%s", sync.storage_id)
        sync.get_latest_state(self.providers)

        if sync.hash_conflict():
            self.handle_hash_conflict(sync)
            return

        if sync.path_conflict():
            self.handle_path_conflict(sync)
            return

        for i in (LOCAL, REMOTE):
            if sync[i].changed:
                response = self.embrace_change(sync, i, other_side(i))
                if response == FINISHED:
                    self.finished(i, sync)
                break

    def temp_file(self, ohash):
        # prefer big random name over NamedTemp which can infinite loop in odd situations!
        return Provider.join(self.tempdir, ohash)  # Not a fan of importing Provider into sync.py for this...

    def finished(self, side, sync):
        sync[side].changed = None
        self.syncs.finished(sync)

        if sync.temp_file:
            try:
                os.unlink(sync.temp_file)
            except Exception:  # TODO: what is this actually trying to catch? FNFE? Everything?
                pass
            sync.temp_file = None

    def download_changed(self, changed, sync):
        sync.temp_file = sync.temp_file or self.temp_file(
            str(sync[changed].hash))

        assert sync[changed].oid

        if os.path.exists(sync.temp_file):
            return True

        try:
            self.providers[changed].download(
                sync[changed].oid, open(sync.temp_file + ".tmp", "wb"))
            os.rename(sync.temp_file + ".tmp", sync.temp_file)
            return True
        except CloudFileNotFoundError:
            log.debug("download from %s failed fnf, switch to not exists",
                      self.providers[changed].debug_name)
            sync[changed].exists = TRASHED
            return False

    def mkdirs(self, prov, path):
        log.debug("mkdirs %s", path)
        try:
            oid = prov.mkdir(path)
            # todo update state
        except CloudFileExistsError:
            # todo: mabye CloudFileExistsError needs to have an oid and/or path in it
            # at least optionally
            info = prov.info_path(path)
            if info:
                oid = info.oid
            else:
                raise
        except CloudFileNotFoundError:
            ppath, _ = prov.split(path)
            if ppath == path:
                raise
            log.debug("mkdirs parent, %s", ppath)
            oid = self.mkdirs(prov, ppath)
            try:
                oid = prov.mkdir(path)
                # todo update state
            except CloudFileNotFoundError:
                raise CloudFileExistsError("f'ed up mkdir")
        return oid

    def mkdir_synced(self, changed, sync, translated_path):
        synced = other_side(changed)
        # see if there are other entries for the same path, but other ids
        ents = list(self.syncs.lookup_path(changed, sync[changed].path))
        ents = [ent for ent in ents if ent != sync]
        if ents:
            for ent in ents:
                if ent.otype == DIRECTORY:
                    # these we can toss, they are other folders
                    # keep the current one, since it exists for sure
                    ent.discard()
                    self.syncs.storage_update(ent)
        ents = [ent for ent in ents if not ent.discarded]
        ents = [ent for ent in ents if TRASHED not in (
            ent[changed].exists, ent[synced].exists)]

        if ents:
            raise NotImplementedError(
                "What to do if we create a folder when there's already a FILE")

        try:
            log.debug("translated %s as path %s",
                      sync[changed].path, translated_path)
            oid = self.mkdirs(self.providers[synced], translated_path)

            # could have made a dir that already existed
            ents = list(self.syncs.lookup_path(changed, sync[changed].path))
            ents = [ent for ent in ents if ent != sync]

            for ent in ents:
                if ent.otype == DIRECTORY:
                    log.debug("discard duplicate dir entry, caused by a mkdirs")
                    ent.discard()
                    self.syncs.storage_update(ent)

            log.debug("mkdir %s as path %s oid %s",
                      self.providers[synced].debug_name, translated_path, debug_sig(oid))
            sync[synced].sync_path = translated_path
            sync[changed].sync_path = sync[changed].path

            self.syncs.update_entry(
                sync, synced, exists=True, oid=oid, path=translated_path)
        except CloudFileNotFoundError:
            log.debug("mkdir %s : %s failed fnf, TODO fix mkdir code and stuff",
                      self.providers[synced].debug_name, translated_path)
            raise NotImplementedError("TODO mkdir, and make syncs etc")

    def upload_synced(self, changed, sync):
        synced = other_side(changed)
        try:
            info = self.providers[synced].upload(
                sync[synced].oid, open(sync.temp_file, "rb"))
            log.debug("upload to %s as path %s",
                      self.providers[synced].debug_name, sync[synced].sync_path)
            sync[synced].sync_hash = info.hash
            if info.path:
                sync[synced].sync_path = info.path
            else:
                sync[synced].sync_path = sync[synced].path
            sync[changed].sync_hash = sync[changed].hash
            sync[changed].sync_path = sync[changed].path

            self.syncs.update_entry(
                sync, synced, exists=True, oid=info.oid, path=sync[synced].sync_path)
        except CloudFileNotFoundError:
            log.debug("upload to %s failed fnf, TODO fix mkdir code and stuff",
                      self.providers[synced].debug_name)
            raise NotImplementedError("TODO mkdir, and make syncs etc")

    def _create_synced(self, changed, sync, translated_path):
        synced = other_side(changed)
        log.debug("create on %s as path %s",
                  self.providers[synced].debug_name, translated_path)
        info = self.providers[synced].create(
            translated_path, open(sync.temp_file, "rb"))
        sync[synced].sync_hash = info.hash
        if info.path:
            sync[synced].sync_path = info.path
        else:
            sync[synced].sync_path = translated_path
        sync[changed].sync_hash = sync[changed].hash
        sync[changed].sync_path = sync[changed].path
        self.syncs.update_entry(
            sync, synced, exists=True, oid=info.oid, path=sync[synced].sync_path)

    def create_synced(self, changed, sync, translated_path):
        synced = other_side(changed)
        try:
            self._create_synced(changed, sync, translated_path)
            return FINISHED
        except CloudFileNotFoundError:
            parent, _ = self.providers[synced].split(translated_path)
            self.mkdirs(self.providers[synced], parent)
            self._create_synced(changed, sync, translated_path)
            return FINISHED
        except CloudFileExistsError:
            # there's a folder in the way, let that resolve later
            return REQUEUE

    def delete_synced(self, sync, changed, synced):
        log.debug("try sync deleted %s", sync[changed].path)
        # see if there are other entries for the same path, but other ids
        ents = list(self.syncs.lookup_path(changed, sync[changed].path))
        ents = [ent for ent in ents if ent != sync]

        if not ents:
            if sync[synced].oid:
                try:
                    self.providers[synced].delete(sync[synced].oid)
                except CloudFileNotFoundError:
                    pass
            else:
                log.debug("was never synced, ignoring deletion")
            sync[synced].exists = TRASHED
        else:
            has_log = False
            for ent in ents:
                if ent.is_creation(changed):
                    log.debug("discard delete, pending create %s", sync)
                    has_log = True
            if not has_log:
                log.warning("conflict delete %s <-> %s", ents, sync)
            sync.discard()

    def check_disjoint_create(self, sync, changed, synced, translated_path):
        # check for creation of a new file with another in the table

        if sync.otype != FILE:
            return False

        ents = list(self.syncs.lookup_path(synced, translated_path))

        # filter for exists
        other_ents = [ent for ent in ents if ent != sync]
        if not other_ents:
            return False

        log.debug("found matching other ents %s", other_ents)

        # ignoring trashed entries with different oids on the same path
        if all(TRASHED in (ent[synced].exists, ent[changed].exists) for ent in other_ents):
            return False

        other_untrashed_ents = [ent for ent in other_ents if TRASHED not in (
            ent[synced].exists, ent[changed].exists)]

        assert len(other_untrashed_ents) == 1

        self.handle_split_conflict(
            other_untrashed_ents[0], synced, sync, changed)

        return True

    def handle_path_change_or_creation(self, sync, changed, synced):
        if not sync[changed].path:
            self.update_sync_path(sync, changed)
            if sync[changed].exists == TRASHED:
                return REQUEUE

        translated_path = self.translate(synced, sync[changed].path)
        if translated_path is None:
            # ignore these
            return FINISHED

        if not sync[changed].path:
            log.debug("can't sync, no path %s", sync)

        if sync.is_creation(changed):
            # never synced this before, maybe there's another local path with
            # the same name already?
            if self.check_disjoint_create(sync, changed, synced, translated_path):
                return REQUEUE

        if sync.is_creation(changed):
            assert not sync[changed].sync_hash
            # looks like a new file

            if sync.otype == DIRECTORY:
                self.mkdir_synced(changed, sync, translated_path)
            elif not self.download_changed(changed, sync):
                pass
            elif sync[synced].oid:
                self.upload_synced(changed, sync)
            else:
                return self.create_synced(changed, sync, translated_path)
        else:
            assert sync[synced].oid
            log.debug("rename %s %s",
                      sync[synced].sync_path, translated_path)
            self.providers[synced].rename(
                sync[synced].oid, translated_path)
            sync[synced].path = translated_path
            sync[synced].sync_path = translated_path
            sync[changed].sync_path = sync[changed].path
        return FINISHED

    def embrace_change(self, sync, changed, synced):
        log.debug("embrace %s", sync)

        if sync[changed].exists == TRASHED:
            self.delete_synced(sync, changed, synced)
            return FINISHED

        if sync.is_path_change(changed) or sync.is_creation(changed):
            return self.handle_path_change_or_creation(sync, changed, synced)

        if sync[changed].hash != sync[changed].sync_hash:
            # not a new file, which means we must have last sync info

            log.debug("needs upload: %s", sync)

            assert sync[synced].oid

            self.download_changed(changed, sync)
            self.upload_synced(changed, sync)
            return FINISHED

        log.info("nothing changed %s, but changed is true", sync)
        return FINISHED

    def update_sync_path(self, sync, changed):
        assert sync[changed].oid

        info = self.providers[changed].info_oid(sync[changed].oid)
        if not info:
            sync[changed].exists = TRASHED
            return

        if not info.path:
            assert False, "impossible sync, no path %s" % sync[changed]

        self.syncs.update_entry(
            sync, changed, sync[changed].oid, path=info.path, exists=True)

    def handle_hash_conflict(self, sync):
        # split the sync in two
        defer_ent, defer_side, replace_ent, replace_side = self.syncs.split(
            sync)

        self.handle_split_conflict(
            defer_ent, defer_side, replace_ent, replace_side)

    def handle_split_conflict(self, defer_ent, defer_side, replace_ent, replace_side):
        defer = defer_ent[defer_side]
        replace = replace_ent[replace_side]

        log.debug("DEFER %s", defer)
        log.debug("REPLACE %s", replace)

        conflict_path = replace.path + ".conflicted"
        self.providers[replace.side].rename(replace.oid, conflict_path)
        self.syncs.update_entry(replace_ent, replace_side,
                                replace.oid, path=conflict_path)

        # force download of other side
        defer.changed = time.time()

    def handle_path_conflict(self, sync):
        # consistent handling
        path1 = sync[0].path
        path2 = sync[1].path
        if path1 > path2:
            pick = 0
        else:
            pick = 1
        picked = sync[pick]
        other = sync[other_side(pick)]
        other_path = self.translate(other.side, picked.path)
        if other_path is None:
            return 
        log.debug("renaming to handle path conflict: %s -> %s",
                  other.oid, other_path)
        self.providers[other.side].rename(other.oid, other_path)
        self.syncs.update_entry(sync, other.side, other.oid, path=other_path)
