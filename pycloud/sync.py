from typing import NamedTuple, Any

from .runnable import Runnable

class State(NamedTuple):
    exists: bool
    hash: bytes
    path: str
    change: float
    id: str


LOCAL = 0
REMOTE = 1

def other(index):
    return 1-index

class Sync:
    FILE = "file"
    DIRECTORY = "dir"

    def __init__(self):
        self.states = (State(), State())
        self.sync_exists = None
        self.sync_hash = None
        self.sync_path = None

    def update(self, providers):
        for i in (LOCAL,REMOTE):
            if self.states[i].change:
                # get latest info from provider
                self.states[i].hash = None
                self.states[i].path = self.states[i].path
                if self.file_type == Sync.FILE:
                    self.states[i].hash = providers[i].hash(self.states[i].oid)
                    self.states[i].exists = self.states[i].hash
                else:
                    self.states[i].exists = providers[i].exists(self.states[i].oid)
            else:
                # trust local sync state
                self.states[i].exists = self.sync_exists
                self.states[i].hash  = self.sync_hash[i]
                self.states[i].path = self.sync_path[i]

    def hash_conflict():
        if self.sync.sync_hash:
            return self.states[0].hash != sync.sync_hash[0] and self.states[1].hash != sync.sync_hash[1]

    def path_conflict():
        if self.sync.sync_path:
            return self.states[0].path != sync.sync_path[0] and self.states[1].path != sync.sync_path[1]

class SyncManager(Runnable):
    def __init__(self, syncs, providers, translate):
        self.syncs = syncs
        self.providers = providers
        self.translate = translate

        assert len(self.providers) == 2

    def do(self):
        for sync in self.syncs.changes():
            self.sync(sync)

    def sync(self, sync):
        sync.update(self.providers)

        if sync.hash_conflict():
            self.handle_hash_conflict(sync, info)

        if sync.path_conflict():
            self.handle_path_conflict(sync, info)

        for i in (LOCAL, REMOTE):
            if sync.states[i].change:
                self.embrace_change(sync, i, other(i))

    def embrace_change(self, sync, changed, other):
        # see if there are other entries for the same path, but other ids
        ents = self.syncs.get_path(changed, sync.states[changed].path)

        if len(ents) == 1:
            assert ent[0] == sync
            self.providers[other].delete(sync.states[other].oid)

        self.states.remove(sync)

class SyncState:
    pass



