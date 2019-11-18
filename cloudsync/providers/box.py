import threading
import logging
import json
import webbrowser
import hashlib
import time
import arrow
import requests

from typing import Optional, Generator, Union

from boxsdk import Client, JWTAuth, OAuth2
from boxsdk.object.item import Item as box_item
from boxsdk.object.folder import Folder as box_folder
from boxsdk.object.file import File as box_file
from boxsdk.exception import BoxException, BoxAPIException  # , BoxAPIException, BoxNetworkException, BoxOAuthException
from boxsdk.session.session import Session, AuthorizedSession
from cloudsync.utils import debug_args
from cloudsync import Provider, OInfo, DIRECTORY, FILE, NOTKNOWN, Event, DirInfo, OType

from cloudsync.oauth import OAuthConfig, OAuthToken

from cloudsync.exceptions import CloudTokenError, CloudDisconnectedError, CloudFileNotFoundError, \
    CloudFileExistsError, CloudException, CloudCursorError
from cloudsync import Provider, OInfo, Hash, DirInfo, Cursor, Event, LongPollManager

log = logging.getLogger(__name__)
logging.getLogger('boxsdk.network.default_network').setLevel(logging.ERROR)
logging.getLogger('urllib3.connectionpool').setLevel(logging.INFO)

# TODO:
#   refactor _api to produce the client or a box_object, or consider if I want to switch to the RESTful api instead
#   add caching for the object id's and types


class BoxProvider(Provider):  # pylint: disable=too-many-instance-attributes, too-many-public-methods
    additional_invalid_characters = ''
    events_to_track = ['ITEM_COPY', 'ITEM_CREATE', 'ITEM_MODIFY', 'ITEM_MOVE', 'ITEM_RENAME', 'ITEM_TRASH',
                       'ITEM_UNDELETE_VIA_TRASH', 'ITEM_UPLOAD']

    _auth_url = 'https://account.box.com/api/oauth2/authorize'
    _token_url = "https://api.box.com/oauth2/token"
    _scopes = []
    base_box_url = 'https://api.box.com/2.0'
    events_endpoint = '/events'
    long_poll_timeout = 120

    def __init__(self, oauth_config: Optional[OAuthConfig] = None):
        super().__init__()

        self.__cursor = None
        self.__client = None
        self.__long_poll_config = {}
        self.__long_poll_session = requests.Session()

        self.api_key = None
        self.refresh_token = None
        self.mutex = threading.RLock()

        self._oauth_config = oauth_config
        self._long_poll_manager = LongPollManager(self.short_poll, self.long_poll)
        self.events = self._long_poll_manager
        self._long_poll_manager.start()

    def _store_refresh_token(self, access_token, refresh_token):
        self.__creds = {"api_key": access_token,
                        "refresh_token": refresh_token,
                       }
        self._oauth_config.creds_changed(self.__creds)

    def interrupt_auth(self):
        self._oauth_config.shutdown()

    def authenticate(self):
        logging.error('authenticating')
        try:
            self._oauth_config.start_auth(self._auth_url, self._scopes)
            token = self._oauth_config.wait_auth(self._token_url, include_client_id=True)
        except Exception as e:
            log.error("oauth error %s", e)
            raise CloudTokenError(str(e))

        return {"api_key": token.access_token,
                "refresh_token": token.refresh_token,
               }

    def get_quota(self):
        with self._api() as client:
            user = client.user(user_id='me').get()

            logging.error(dir(user))

            res = {
                'used': user.space_used,
                'limit': user.space_amount,
                'login': user.login,
            }
            log.debug("quota %s", res)
            return res

    def connect(self, creds):
        log.debug('Connecting to box')
        if not self.__client:
            self.__creds = creds

            jwt_token = creds.get('jwt_token')
            api_key = creds.get('api_key', self.api_key)
            refresh_token = creds.get('refresh_token', None)

            if not jwt_token:
                if not ((self._oauth_config.app_id and self._oauth_config.app_secret) and (refresh_token or api_key)):
                    raise CloudTokenError("require app_id/secret and either api_key or refresh token")

            try:
                with self.mutex:
                    if jwt_token:
                        jwt_dict = json.loads(jwt_token)
                        sdk = JWTAuth.from_settings_dictionary(jwt_dict)
                        self.__client = Client(sdk)
                        self.__creds['api_key'] = sdk.access_token
                    else:
                        if not refresh_token:
                            raise CloudTokenError("Missing refresh token")
                        box_session = Session()
                        box_kwargs = box_session.get_constructor_kwargs()
                        box_oauth = OAuth2(client_id=self._oauth_config.app_id,
                                           client_secret=self._oauth_config.app_secret,
                                           access_token=self.__creds["api_key"],
                                           refresh_token=self.__creds["refresh_token"],
                                           store_tokens=self._store_refresh_token)

                        box_session = AuthorizedSession(box_oauth, **box_kwargs)
                        self.__client = Client(box_oauth, box_session)
                with self._api() as client:
                    self.connection_id = client.user(user_id='me').get().id
            except BoxException:
                log.exception("Error during connect")
                self.disconnect()
                raise CloudTokenError()

    def disconnect(self):
        self.__client = None
        self.connection_id = None

    def reconnect(self):
        if not self.connected:
            self.connect(self.__creds)

    # def junk_api(self, method, *args, **kwargs):  # pylint: disable=arguments-differ
    #     log.debug("_api: %s (%s)", method, debug_args(args, kwargs))
    #
    #     with self.mutex:
    #         if not self.client:
    #             raise CloudDisconnectedError("currently disconnected")
    #
    #         try:
    #             return getattr(self.client, method)(*args, **kwargs).get()
    #             # TODO add an exception for when the key is expired
    #             #       also create a provider test that verifies the behavior when the api_key goes bad
    #             #       to verify that the callback for the refresh_token is called when it changes
    #             #       also create a box test that verifies that when the api_token is refreshed that the
    #             #       refresh_token changes
    #         except BoxException:
    #             self.refresh_api_key()
    #             self.write_refresh_token_to_database()
    #             try:
    #                 return getattr(self.client, method)(*args, **kwargs)
    #             except Exception as e:
    #                 logging.error(e)
    #         except Exception as e:
    #             logging.error(e)
        # noinspection PyMethodParameters
    class Guard:
        def __init__(self, client, box):
            self.__client = client
            self.__box = box

        def __enter__(self) -> Client:
            self.__box.mutex.__enter__()
            return self.__client

        def __exit__(self, ty, ex, tb):
            self.__box.mutex.__exit__(ty, ex, tb)

            if ex:
                try:
                    raise ex
                except (TimeoutError,):
                    self.__box.disconnect()
                    raise CloudDisconnectedError("disconnected on timeout")
                except BoxAPIException as e:
                    if e.status == 400 and e.code == 'folder_not_empty':
                        raise CloudFileExistsError()
                    elif e.status == 404 and e.code == 'not_found':
                        raise CloudFileNotFoundError()
                    elif e.status == 404 and e.code == 'trashed':
                        raise CloudFileNotFoundError()
                    elif e.status == 409 and e.code == 'item_name_in_use':
                        raise CloudFileExistsError()
                    else:
                        log.exception("unknown box exception: \n%s", e)
                except CloudException:
                    raise
                except Exception as e:
                    pass

    def _api(self, *args, **kwargs) -> Guard:
        needs_client = kwargs.get('needs_client', None)
        if needs_client and not self.__client:
            raise CloudDisconnectedError("currently disconnected")
        return self.Guard(self.__client, self)

    @property
    def name(self):
        return 'box'

    @property
    def latest_cursor(self):
        with self._api() as client:
            res = str(client.events().get_latest_stream_position())
            if res:
                return res
            else:
                return None

    @property
    def current_cursor(self) -> Cursor:
        if not self.__cursor:
            self.__cursor = self.latest_cursor
        return self.__cursor

    @current_cursor.setter
    def current_cursor(self, val: Cursor) -> None:  # pylint: disable=no-self-use, unused-argument
        if val is None:
            val = self.latest_cursor
        if not isinstance(val, str) and val is not None:
            raise CloudCursorError(val)
        self.__cursor = val

    # def _long_poll_loop_test(self):
    #     while True:
    #         try:
    #             self.__polling_found_zero.wait()
    #             change_found = self._long_poll(self.long_poll_timeout)
    #             if change_found:
    #                 self.__polling_found_zero.clear()
    #                 self.__long_polling_stopped.set()
    #         except Exception as e:
    #             log.error("long poll loop got unhandled exception %s", e)
    #             time.sleep(15)
    def long_poll(self, timeout=long_poll_timeout):
        log.debug("inside long_poll")
        try:
            if self.__long_poll_config.get('retries_remaining', 0) < 1:
                log.debug("creds = %s", self.__creds)
                headers = {'Authorization': 'Bearer %s' % (self.__creds['api_key'], )}
                log.debug("headers: %s", headers)
                srv_resp = self.__long_poll_session.options(self.base_box_url + self.events_endpoint,
                                                            headers=headers)
                log.debug("response content is %s, %s", srv_resp.status_code, srv_resp.content)
                if not (200 <= srv_resp.status_code < 300):
                    raise CloudTokenError
                server_json = srv_resp.json().get('entries')[0]
                self.__long_poll_config = {
                    "url": server_json.get('url'),
                    "retries_remaining": server_json.get('max_retries'),
                    "retry_timeout": server_json.get('retry_timeout')
                }
            srv_resp: requests.Response = self.__long_poll_session.get(self.__long_poll_config.get('url'),
                                                    timeout=timeout)  # long poll
            log.debug("server message is %s", srv_resp.get('message'))
            return srv_resp.get('message') == 'new_change'
        except requests.exceptions.ReadTimeout:  # need new long poll server:
            log.debug('Timeout during long poll')
            return False
        # TODO except boxerror.too_many_retries (or whatever the exception is called)
        finally:
            self.__long_poll_config['retries_remaining'] = self.__long_poll_config.get('retries_remaining', 1) - 1

    def events(self) -> Generator[Event, None, None]:
        pass

    def short_poll(self) -> Generator[Event, None, None]:
        # see: https://developer.box.com/en/reference/resources/realtime-servers/
        stream_position = self.current_cursor
        while True:
            log.debug("inside short_poll()", change)
            with self._api() as client:
                response = client.events().get_events(limit=100, stream_position=stream_position)
                new_position = response.get('next_stream_position')
                for change in (i for i in response.get('entries') if i.get('event_type')):
                    log.debug("got event %s", change)
                    log.debug("event type is %s", change.get('event_type'))
                    ts = arrow.get(change.get('created_at')).float_timestamp
                    change_source = change.get('source')
                    if isinstance(change_source, box_item):
                        otype = DIRECTORY if type(change_source) is box_folder else FILE
                        oid = change_source.id
                        path = self._get_path(change_source)
                        ohash = change_source.sha1 if type(change_source) is box_file else None
                        exists = change_source.trashed_at is None
                    else:
                        continue

                    event = Event(otype, oid, path, ohash, exists, ts, new_cursor=new_position)

                    yield event

                if new_position and stream_position and new_position != stream_position:
                    self.__cursor = new_position
                stream_position = new_position

    # noinspection DuplicatedCode
    def _walk(self, path, oid):
        for ent in self.listdir(oid):
            current_path = self.join(path, ent.name)
            event = Event(otype=ent.otype, oid=ent.oid, path=current_path, hash=ent.hash, exists=True, mtime=time.time())
            log.debug("walk %s", event)
            yield event
            if ent.otype == DIRECTORY:
                if self.exists_oid(ent.oid):
                    yield from self._walk(current_path, ent.oid)

    def walk(self, path, since=None):
        info = self.info_path(path)
        if not info:
            raise CloudFileNotFoundError(path)
        yield from self._walk(path, info.oid)


    def upload(self, oid, file_like, metadata=None) -> OInfo:
        with self._api():
            box_object: box_file = self._get_box_object(oid=oid)  # don't specify object_type here
            if box_object is None:
                raise CloudFileNotFoundError()
            if box_object.object_type != 'file':
                raise CloudFileExistsError()
            new_object = box_object.update_contents_with_stream(file_like)
            retval = self._get_oinfo(new_object)
            return retval

    def create(self, path, file_like, metadata=None) -> OInfo:
        with self._api():
            parent, base = self.split(path)
            parent_object = self._get_box_object(path=parent, object_type=DIRECTORY)
            if parent_object is None:
                raise CloudFileNotFoundError()
            # TODO: implement preflight_check on the upload_stream() call
            new_object = parent_object.upload_stream(file_stream=file_like, file_name=base)
            retval = self._get_oinfo(new_object, parent_path=parent)
            return retval

    def download(self, oid, file_like):
        with self._api():
            box_object: box_file = self._get_box_object(oid=oid)
            if box_object is None:
                raise CloudFileNotFoundError()
            box_object.download_to(writeable_stream=file_like)

    def rename(self, oid, path) -> str:
        with self._api():
            box_object: box_file = self._get_box_object(oid=oid)
            if box_object is None:
                raise CloudFileNotFoundError()
            info = self._get_oinfo(box_object)
            if info.path:
                old_path = info.path
            else:
                old_path = self._get_path(box_object)
            old_parent, old_base = self.split(old_path)
            new_parent, new_base = self.split(path)
            if new_parent == old_parent:
                try:
                    with self._api():
                        retval = box_object.rename(new_base)
                except CloudFileExistsError:
                    if box_object.object_type == 'file':
                        raise
                    # are we renaming a folder over another empty folder?
                    box_conflict = self._get_box_object(path=path)
                    if box_conflict is None:  # should't happen... we just got a FEx error
                        raise
                    if box_conflict.object_type == 'folder' and box_conflict.item_collection['total_count'] == 0:
                        box_conflict.delete()
                        return self.rename(oid, path)
                    else:
                        raise
            else:
                new_parent_object = self._get_box_object(path=new_parent)
                if new_parent_object is None:
                    raise CloudFileNotFoundError()
                retval = box_object.move(parent_folder=new_parent_object, name=new_base)
            return retval.id

    def mkdir(self, path) -> str:
        try:
            with self._api() as client:
                parent, base = self.split(path)
                parent_object: box_folder = self._get_box_object(path=parent)
                if parent_object is None:
                    raise CloudFileNotFoundError()
                if parent_object.object_type != 'folder':
                    raise CloudFileExistsError()
                child_object: box_folder = parent_object.create_subfolder(base)
                return child_object.object_id
        except CloudFileExistsError as e:
            try:
                box_object = self._get_box_object(path=path)
            except Exception:
                raise e
            if box_object is None or box_object.object_type != 'folder':
                raise
            else:
                return box_object.object_id

    def delete(self, oid):
        with self._api():
            box_object = self._get_box_object(oid=oid)
            if box_object is None:
                return
            if box_object.object_type == 'file':
                box_object.delete()
            else:
                box_object.delete(recursive=False)

    def exists_oid(self, oid):
        try:
            with self._api():
                box_object = self._get_box_object(oid=oid)
                if box_object is None:
                    return False
                box_object.get()
                return True
        except CloudFileNotFoundError:
            return False

    def exists_path(self, path) -> bool:
        return self.info_path(path) is not None

    def listdir(self, oid) -> Generator[DirInfo, None, None]:
        # optionally takes a path, to make creating the OInfo cheaper, so that it doesn't need to figure out the path
        with self._api() as client:
            parent_object = self._get_box_object(oid=oid)
            if parent_object is None:
                raise CloudFileNotFoundError()
            entries = parent_object.item_collection['entries']
            for entry in entries:
                if type(entry) is dict:  # Apparently, get_box_object by path returns dicts and by oid returns objects?
                    raise NotImplementedError
                    # retval = self._get_oinfo_from_collection_entry(entry)
                else:
                    retval = self._get_dirinfo(entry)
                if retval is not None:
                    yield retval

    def hash_data(self, file_like) -> Hash:
        # get a hash from a filelike that's the same as the hash i natively use
        sha1 = hashlib.sha1()
        for c in iter(lambda: file_like.read(32768), b''):
            sha1.update(c)
        return sha1.hexdigest()

    # def _get_(self, path) V-> OInfo:
    #     retval = OInfo
    #     if path == '/' or path == '':
    #         return OInfo(oid='0', otype=DIRECTORY)

    def _get_path(self, box_object: box_item, expensive=False) -> Optional[str]:
        path_collection = None
        if hasattr(box_object, 'path_collection'):
            path_collection = box_object.path_collection
        if path_collection:
            return self._get_path_from_collection(path_collection, box_object.name)
        else:
            raise NotImplementedError  # should this be path="", or maybe do the box_object.get(), or some other thing?

    def _get_path_from_collection(self, path_collection: dict, base_name: str):
        retval_list = []
        try:
            entries = path_collection['entries']
        except Exception:
            raise;
        for entry in entries:
            if entry.id != '0':
                retval_list.append(entry.name)
        if base_name:
            retval_list.append(base_name)
        return self.join(retval_list)

    def _get_dirinfo(self, box_object: Union[box_file, box_folder], parent_path=None) -> Optional[DirInfo]:
        oinfo = self._get_oinfo(box_object, parent_path)
        retval = DirInfo(otype=oinfo.otype, oid=oinfo.oid, hash=oinfo.hash, path=oinfo.path, name=box_object.name,
                         mtime=None, shared=False, readonly=False)
        return retval

    def _get_oinfo(self, box_object: Union[box_file, box_folder], parent_path=None) -> Optional[OInfo]:
        if box_object is None:
            return None

        obj_type = DIRECTORY if box_object.object_type == 'folder' else FILE
        if parent_path:
            path = self.join(parent_path, box_object.name)
        else:
            path = None  # self._get_path(box_object)
        return OInfo(
            oid=box_object.object_id,
            path=path,
            otype=obj_type,
            hash=None if obj_type == DIRECTORY else box_object.sha1
        )

    def _get_oinfo_from_collection_entry(self, entry: Union[box_file, box_folder]) -> Optional[OInfo]:
        if entry is None:
            return None
        box_info = entry.get()
        if box_info.get('item_status') != "active":
            return None

        obj_type = DIRECTORY if box_info.get('type') == 'folder' else FILE
        return OInfo(
            oid=box_info.get('id'),
            path=self._get_path_from_collection(box_info.get('path_collection'), box_info['name']),
            otype=obj_type,
            hash=None if obj_type==DIRECTORY else box_info.get('sha1')
        )

    def info_path(self, path: str) -> Optional[OInfo]:
        # otype: OType  # fsobject type     (DIRECTORY or FILE)
        # oid: str  # fsobject id
        # hash: Any  # fsobject hash     (better name: ohash)
        # path: Optional[str]  # path
        if path == "/" or path == '':
            with self._api() as client:
                return self._get_oinfo(client.root_folder().get())

        box_object = self._get_box_object(path=path)
        parent, _ = self.split(path)
        return self._get_oinfo(box_object, parent_path=parent)

    def _get_box_object(self, oid=None, path=None, object_type: OType = None) -> Optional[Union[box_folder, box_file]]:
        with self._api():
            try:
                unsafe_box_object = self._unsafe_get_box_object(oid=oid, path=path, object_type=object_type)
                retval = unsafe_box_object
                return retval
            except CloudFileNotFoundError:
                return None
            except CloudFileExistsError:
                raise
            # except Exception as e:
            #     return None

    def _unsafe_get_box_object(self, oid=None, path=None, object_type: OType = None):
        # this is unsafe because it returns an object that can hit the api outside of the guard
        # only call this function within another guard, and don't use the return value outside of that guard
        assert oid or path
        box_object = None
        with self._api() as client:
            if oid:
                if object_type == FILE:
                    box_object = client.file(file_id=oid)
                elif object_type == DIRECTORY:
                    box_object = client.folder(folder_id=oid)
                else:
                    try:
                        with self._api():
                            box_object = client.file(file_id=oid)
                            return box_object.get()  # allows the local exception handler to be in effect for this call
                    except (CloudFileExistsError, CloudFileNotFoundError):
                        box_object = client.folder(folder_id=oid)
                        return box_object.get()
                    except Exception as e:
                        log.exception(e)
                        raise
            else:
                if path == '/' or path == '':
                    root = client.root_folder()
                    root2 = root.get()
                    return root2
                parent, base = self.split(path)
                parent_object: box_folder = self._get_box_object(path=parent, object_type=DIRECTORY)
                if not parent_object:
                    return None
                if parent_object.object_type != 'folder':
                    raise CloudFileExistsError
                collection = parent_object.item_collection
                collection_entries = collection['entries']
                offset = collection['total_count']
                while True:
                    entry_count = 0
                    for entry in collection_entries:
                        entry: box_file
                        entry_count += 1
                        if entry.name == base:
                            found_type = DIRECTORY if entry.object_type == 'folder' else FILE
                            if object_type is not None and found_type != object_type:
                                raise CloudFileExistsError()
                            return self._get_box_object(oid=entry.object_id, object_type=found_type)
                    if entry_count == 0:
                        break
                    collection_entries = parent_object.get_items(offset=offset)
                    offset = collection_entries.next_pointer()
            if box_object is None:
                return None
            return box_object.get()

    def info_oid(self, oid, use_cache=True) -> Optional[OInfo]:
        with self._api():
            box_object = self._get_box_object(oid=oid)
            oinfo = self._get_oinfo(box_object)
            if oinfo and not oinfo.path:
                # expensive
                oinfo.path = self._get_path(box_object)
            return oinfo

    def get_parent_id(self, path):
        if not path:
            return None
        parent, _ = self.split(path)
        parent_info = self.info_path(parent)
        if not parent_info:
            raise CloudFileNotFoundError("parent %s must exist" % parent)
        return parent_info.oid

    def refresh_api_key(self):
        # Use the refresh token to get a new api key and refresh token
        raise NotImplementedError

    def write_refresh_token_to_database(self):
        raise NotImplementedError
