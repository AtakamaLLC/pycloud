import pytest
from unittest.mock import patch

from pycloud import Event, CloudFileNotFoundError

@pytest.fixture
def gdrive():
    return 'a'

@pytest.fixture
def dropbox():
    return 'b'

@pytest.fixture(params=['gdrive', 'dropbox'])
def provider(request, gdrive, dropbox):
    return {'gdrive': gdrive, 'b': dropbox}[request.param]

@pytest.fixture
def env():
    return None

def test_connect(provider):
    assert provider.connected

# todo: should work with file-likes rather than path  question: magically?

def test_upload(env, provider):
    temp = env.temp_file(fill_bytes=32)

    hash0 = provider.local_hash(temp)

    cloud_id1, hash1 = provider.upload(temp, "/dest")

    cloud_id2, hash2 = provider.upload(temp, "/dest", cloud_id=cloud_id1)

    assert cloud_id1 == cloud_id2

    assert hash0 == hash1

    assert hash1 == hash2

    assert provider.exists("/dest")

    cloud_id3, hash3 = provider.download("/dest", temp)

    assert cloud_id1 == cloud_id3

    assert hash1 == hash3


def test_walk(env, provider):
    temp = env.temp_file(fill_bytes=32)
    cloud_id1, hash1 = provider.upload(temp, "/dest")
    assert not provider.walked

    for e in provider.events(timeout=1):
        if e is None:
            break
        assert provider.walked
        assert e.path = "/dest"
        assert e.cloud_id
        assert e.mtime
        assert e.exists
        assert e.source = Event.REMOTE

def test_event_basic(env, provider):
    for e in provider.events(timeout=1):
        if e is None:
            break
        assert False, "no events here!"

    assert provider.walked

    temp = env.temp_file(fill_bytes=32)
    cloud_id1, hash1 = provider.upload(temp, "/dest")

    for e in provider.events(timeout=1):
        if e is None:
            break

        assert e.path = "/dest"
        assert e.cloud_id
        assert e.mtime
        assert e.exists
        assert e.source = Event.REMOTE

    provider.delete(cloud_id=e.cloud_id)

    with pytest.raises(CloudFileNotFoundError):
        provider.delete(cloud_id=e.cloud_id)
   
    for e in provider.events(timeout=1):
        if e is None:
            break

        assert e.path = "/dest"
        assert e.cloud_id
        assert e.mtime
        assert not e.exists
        assert e.source = Event.REMOTE

def test_api_failure(provider):
    # assert that the cloud 
    # a) uses an api function
    # b) does not trap CloudTemporaryError's

    with patch.object(provider, "api", side_effect=lambda *a, **k: raise CloudTemporaryError("fake disconned")):
        with pytest.raises(CloudTemporaryError):
            provider.exists("/notexists")


