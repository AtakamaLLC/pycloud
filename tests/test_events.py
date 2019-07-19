import os
import pytest

from pycloud import EventManager

from .fixtures import MockProvider

@pytest.fixture(name="manager")
def fixture_manager():
    return EventManager(MockProvider())  # TODO extend this to take any provider

def test_event_basic(util, manager):
    provider = manager.provider
    temp = util.temp_file(fill_bytes=32)
    info = provider.upload(temp, "/dest")

    # this is normally a blocking function that runs forever
    def done():
        return os.path.exists(local_path)

    # loop the sync until the file is found
    manager.run(timeout=1, until=done)

    local_path = manager.local_path("/fandango")

    util.fill_bytes(local_path, count=32)

    manager.local_event(path=local_path, exists=True)

    # loop the sync until the file is found
    manager.sync(timeout=1, until=done)

    info = provider.info("/fandango")

    assert info.hash == provider.local_hash(temp)
    assert info.cloud_id
