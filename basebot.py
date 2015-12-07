# -*- coding: ascii -*-

"""
Bot library for euphoria.io.
"""

# ---------------------------------------------------------------------------
# Preamble
# ---------------------------------------------------------------------------

# Version.
__version__ = "2.0"

# Modules - Standard library
import sys, os, re, time
import json
import logging
import threading

# Modules - Additional. Must be installed.
import websocket
from websocket import WebSocketException as WSException, \
    WebSocketConnectionClosedException as WSCCException

# Regex for @-mentions
# From github.com/euphoria-io/heim/blob/master/client/lib/stores/chat.js as
# of commit f9d5527beb41ac3e6e0fee0c1f5f4745c49d8f7b (adapted).
_MENTION_DELIMITER = r'[,.!?;&<\'"\s]'
MENTION_RE = re.compile('(?:^|(?<=' + _MENTION_DELIMITER + r'))@(\S+?)(?=' +
                        _MENTION_DELIMITER + '|$)')

# Regex for whitespace.
WHITESPACE_RE = re.compile('\s+')

# Default connection URL template.
URL_TEMPLATE = os.environ.get('BASEBOT_URL_TEMPLATE',
                              'wss://euphoria.io/room/{}/ws')

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def normalize_nick(nick):
    """
    normalize_nick(nick) -> str

    Remove whitespace from the given nick, and perform any other
    normalizations on it.
    """
    return WHITESPACE_RE.sub('', nick).lower()

def format_datetime(timestamp, fractions=True):
    """
    format_datetime(timestamp, fractions=True) -> str

    Produces a string representation of the timestamp similar to
    the ISO 8601 format: "YYYY-MM-DD HH:MM:SS.FFF UTC". If fractions
    is false, the ".FFF" part is omitted. As the platform the bots
    are used on is international, there is little point to use any kind
    of timezone but UTC.

    See also: format_delta()
    """
    ts = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(timestamp))
    if fractions: ts += '.%03d' % (int(timestamp * 1000) % 1000)
    return ts + ' UTC'

def format_delta(delta, fractions=True):
    """
    format_delta(delta, fractions=True) -> str

    Format a time difference. delta is a numeric value holding the time
    difference to be formatted in seconds. The return value is composed
    like that: "[- ][Xd ][Xh ][Xm ][X[.FFF]s]", with the brackets indicating
    possible omission. If fractions is False, or the given time is an
    integer, the fractional part is omitted. All components are included as
    needed, so the result for 3600 would be "1h". As a special case, the
    result for 0 is "0s" (instead of nothing).

    See also: format_datetime()
    """
    if not fractions:
        delta = int(delta)
    if delta == 0: return '0s'
    ret = []
    if delta < 0:
        ret.append('-')
        delta = -delta
    if delta >= 86400:
        ret.append('%dd' % (delta // 86400))
        delta %= 86400
    if delta >= 3600:
        ret.append('%dh' % (delta // 3600))
        delta %= 3600
    if delta >= 60:
        ret.append('%dm' % (delta // 60))
        delta %= 60
    if delta != 0:
        if delta % 1 != 0:
            ret.append('%ss' % round(delta, 3))
        else:
            ret.append('%ds' % delta)
    return ' '.join(ret)

class Record(dict):
    """
    Record(...) -> new instance

    A dictionary that exports some items as attributes as well as provides
    static defaults for some keys. Can be constructed in any way a dict
    can.
    """

    # Export list.
    _exports_ = ()

    # Defaults mapping.
    _defaults_ = {}

    def __getattr__(self, name):
        if name not in self._exports_:
            raise AttributeError(name)
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name, value):
        if name not in self._exports_:
            raise AttributeError(name)
        try:
            self[name] = value
        except KeyError:
            raise AttributeError(name)

    def __delattr__(self, name):
        if name not in self._exports_:
            raise AttributeError(name)
        try:
            del self[name]
        except KeyError:
            raise AttributeError(name)

    def __missing__(self, key):
        return self._defaults_[key]

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class BasebotException(Exception):
    "Base exception class."

class NoRoomError(BasebotException):
    "No room specified before HeimEndpoint.connect() call."

class NoConnectionError(BasebotException):
    "HeimEndpoint currently connected."

# ---------------------------------------------------------------------------
# Lowest abstraction layer.
# ---------------------------------------------------------------------------

class JSONWebSocket:
    """
    JSONWebSocketWrapper(ws) -> new instance

    JSON-reading/writing WebSocket wrapper.
    Provides recv()/send() methods that transparently encode/decode JSON.
    Reads and writes are serialized with independent locks; the reading
    lock is to be acquired "outside" the write lock.
    """

    def __init__(self, ws):
        """
        __init__(ws) -> None

        Initializer. See class docstring for invocation details.
        """
        self.ws = ws
        self.rlock = threading.RLock()
        self.wlock = threading.RLock()

    def _recv_raw(self):
        """
        _recv_raw() -> str

        Receive a WebSocket frame, and return it unmodified.
        Raises a websocket.WebSocketConnectionClosedException (aliased to
        WSCCException in this module) if the underlying connection closed.
        """
        with self.rlock:
            return self.ws.recv()

    def recv(self):
        """
        recv() -> object

        Receive a single WebSocket frame, decode it using JSON, and return
        the resulting object.
        Raises a websocket.WebSocketConnectionClosedException (aliased to
        WSCCException in this module) if the underlying connection closed.
        """
        return json.loads(self._recv_raw())

    def _send_raw(self, data):
        """
        _send_raw(data) -> None

        Send the given data without modification.
        Raises a websocket.WebSocketConnectionClosedException (aliased to
        WSCCException in this module) if the underlying connection closed.
        """
        with self.wlock:
            self.ws.send(data)

    def send(self, obj):
        """
        send(obj) -> None

        JSON-encode the given object, and send it.
        Raises a websocket.WebSocketConnectionClosedException (aliased to
        WSCCException in this module) if the underlying connection closed.
        """
        self._send_raw(json.dumps(obj))

    def close(self):
        """
        close() -> None

        Close this connection. Repeated calls will succeed immediately.
        """
        self.ws.close()

# ---------------------------------------------------------------------------
# Euphorian protocol.
# ---------------------------------------------------------------------------

# Constructed after github.com/euphoria-io/heim/blob/master/doc/api.md as of
# commit 03906c0594c6c7ab5e15d1d8aa5643c847434c97.

class Packet(Record):
    """
    The "basic" members any packet must/may have.

    Attributes:
    id              :  client-generated id for associating replies with
                       commands (optional)
    type            :  the name of the command, reply, or event
    data            :  the payload of the command, reply, or event (optional)
    error           :  this field appears in replies if a command fails
                       (optional)
    throttled       :  this field appears in replies to warn the client that
                       it may be flooding; the client should slow down its
                       command rate (defaults to False)
    throttled_reason:  if throttled is true, this field describes why
                       (optional)
    """
    _exports_ = ('id', 'type', 'data', 'error', 'throttled',
                 'throttled_reason')

class AccountView(Record):
    """
    AccountView describes an account and its preferred names.

    Attributes:
    id  : the id of the account
    name: the name that the holder of the account goes by
    """
    _exports_ = ('id', 'name')

class Message(Record):
    """
    A Message is a node in a Room's Log. It corresponds to a chat message, or
    a post, or any broadcasted event in a room that should appear in the log.

    Attributes:
    id               : the id of the message (unique within a room)
    parent           : the id of the message's parent, or null if top-level
                       (optional)
    previous_edit_id : the edit id of the most recent edit of this message,
                       or None if it's never been edited (optional)
    time             : the unix timestamp of when the message was posted
    sender           : the view of the sender's session (SessionView)
    content          : the content of the message (client-defined)
    encryption_key_id: the id of the key that encrypts the message in storage
                       (optional)
    edited           : the unix timestamp of when the message was last edited
                       (optional)
    deleted          : the unix timestamp of when the message was deleted
                       (optional)
    truncated        : if true, then the full content of this message is not
                       included (see get-message to obtain the message with
                       full content) (optional)

    All optional attributes default to None.

    Additional read-only properties:
    mention_list: Tuple of (offset, string) pairs listing all the @-mentions
                  in the message (including the @ signs).
    mention_set : frozenset of names @-mentioned in the message (excluding
                  the @ signs).
    """
    _exports_ = ('id', 'parent', 'previous_edit_id', 'time', 'sender',
                 'content', 'encryption_key_id', 'edited', 'deleted',
                 'truncated')

    _defaults_ = {'parent': None, 'previous_edit_id': None,
                  'encryption_key_id': None, 'edited': None, 'deleted': None,
                  'truncated': None}

    def __init__(__self, *__args, **__kwds):
        Record.__init__(__self, *__args, **__kwds)
        __self.__lock = threading.RLock()
        __self.__mention_list = None
        __self.__mention_set = None

    def __setitem__(self, key, value):
        with self.__lock:
            Record.__setitem__(self, key, value)
            self.__mention_list = None
            self.__mention_set = None

    @property
    def mention_list(self):
        with self.__lock:
            if self.__mention_list is None:
                l, s, o = [], self.content, 0
                ls = len(s)
                while o < ls:
                    m = MENTION_RE.search(s, o)
                    if not m: break
                    l.append((m.start(), m.group()))
                    o = m.end()
                self.__mention_list = tuple(l)
            return self.__mention_list

    @property
    def mention_set(self):
        with self.__lock:
            if self.__mention_set is None:
                self.__mention_set = frozenset(i[1][1:]
                    for i in self.__mention_list)
            return self.__mention_set

class SessionView(Record):
    """
    SessionView describes a session and its identity.

    Attributes:
    id        : the id of an agent or account
    name      : the name-in-use at the time this view was captured
    server_id : the id of the server that captured this view
    server_era: the era of the server that captured this view
    session_id: id of the session, unique across all sessions globally
    is_staff  : if true, this session belongs to a member of staff (defaults
                to False)
    is_manager: if true, this session belongs to a manager of the room
                (defaults to False)

    Additional read-only properties:
    is_account: Whether this session has an account.
    is_agent  : Whether this session is neither a bot nor has an account.
    is_bot    : Whether this is a bot.
    norm_name : Normalized name.
    """
    _exports_ = ('id', 'name', 'server_id', 'server_era', 'session_id',
                 'is_staff', 'is_manager')

    _defaults_ = {'is_staff': False, 'is_manager': False}

    @property
    def is_account(self):
        return self['id'].startswith('account:')

    @property
    def is_agent(self):
        return self['id'].startswith('agent:')

    @property
    def is_bot(self):
        return self['id'].startswith('bot:')

    @property
    def norm_name(self):
        return normalize_nick(self.name)

class HeimEndpoint(object):
    """
    HeimEndpoint(**config) -> new instance

    Endpoint for the Heim protocol. Provides methods to submit commands,
    as well as call-back methods for incoming replies/events. Re-connects
    are handled transparently.

    Attributes (assignable by keyword arguments):
    url_template: Template to construct URLs from. Its format() method
                  will be called with the room name as the only argument.
                  Defaults to the global URL_TEMPLATE variable, which, in
                  turn, may be overridden by the environment variable
                  BASEBOT_URL_TEMPLATE (if set when the module is
                  initialized).
    roomname    : Name of room to connect to. Defaults to None. Must be
                  explicitly set for the connection to succeed.
    nickname    : Nick-name to set on connection. Updated when a nick-reply
                  is received. Defaults to None; in that case, no nick-name
                  is set.
    passcode    : Passcode for private rooms. Sent during (re-)connection.
                  Defaults to None; no passcode is sent in that case.
    retry_count : Amount of re-connection attempts until an operation (a
                  connect or a send) fails.
    retry_delay : Amount of seconds to wait before a re-connection attempt.

    Access to the attributes should be serialized using the instance lock
    (available in the lock attribute). The __enter__ and __exit__ methods
    of the lock are exposed, so "with self:" can be used instead of "with
    self.lock:".

    Other attributes (not assignable by keyword arguments):
    lock        : Attribute access lock. Must be acquired whenever an
                  attribute is changed, or when multiple accesses to an
                  attribute should be atomic.
    """

    def __init__(self, **config):
        """
        __init__(self, **config) -> None

        Constructor. See class docstring for usage.
        """
        self.url_template = config.get('url_template', URL_TEMPLATE)
        self.roomname = config.get('roomname', None)
        self.nickname = config.get('nickname', None)
        self.passcode = config.get('passcode', None)
        self.retry_count = config.get('retry_count', 4)
        self.retry_delay = config.get('retry_delay', 10)
        self.lock = threading.RLock()
        # Actual connection.
        self._connection = None
        # Whether someone is poking the connection.
        self._connecting = False
        # Condition variable to serialize all on.
        self._conncond = threading.Condition(self.lock)

    def __enter__(self):
        return self.lock.__enter__()
    def __exit__(self, *args):
        return self.lock.__exit__(*args)

    def _make_connection(self, url):
        """
        _make_connection(url) -> JSONWebSocket

        Actually connect to url.
        Returns the object produced, or raises an exception.
        Can be hooked by subclasses.
        """
        return JSONWebSocket(websocket.create_connection(url))

    def _attempt(self, func):
        """
        _attempt(func) -> object

        Attempt to run func; if it raises an exception, re-try using the
        specified parameters (retry_count and retry_delay).
        Func is called with two arguments, the zero-based trial counter,
        and amount of re-tries that will be attempted.
        If the last attempt fails, the exception that indicated the
        failure is raised.
        If the function call succeeds, the return value of func is passed
        out.
        """
        with self.lock:
            count, delay = self.retry_count, self.retry_delay
        for i in range(count + 1):
            if i: time.sleep(delay)
            try:
                return func(i, count)
            except Exception:
                if i == count:
                    raise
                continue

    def _connect(self):
        """
        _connect() -> None

        Internal back-end for connect(). Takes care of synchronization.
        """
        with self._conncond:
            if self.roomname is None:
                raise NoRoomError('No room specified')
            while self._connecting:
                self._conncond.wait()
            if self._connection is not None:
                return
            self._connecting = True
            url = self.url_template.format(self.roomname)
        conn = None
        try:
            conn = self._attempt(lambda c, a: self._make_connection(url))
        finally:
            with self._conncond:
                self._connecting = False
                self._connection = conn
                self._conncond.notifyAll()

    def _disconnect(self):
        """
        _disconnect() -> None

        Internal back-end for close(). Takes care of synchronization.
        """
        with self._conncond:
            while self._connecting:
                self._conncond.wait()
            conn = self._connection
            self._connection = None
            self._conncond.notifyAll()
        if conn is not None:
            conn.close()

    def _reconnect(self):
        """
        _reconnect() -> None

        Considering the current connection to be broken, discard it
        forcefully (unless another attempt to re-connect is already
        happening), and try to connect again (only once).
        """
        with self._conncond:
            while self._connecting:
                self._conncond.wait()
            else:
                self._connection = None
            if self._connection is not None:
                return
            self._connecting = True
        conn = None
        try:
            conn = self._make_connection(url)
        finally:
            with self._conncond:
                self._connecting = False
                self._connection = conn
                self._conncond.notifyAll()

    def connect(self):
        """
        connect() -> None

        Connect to the configured room.
        Return instantly if already connected.
        Raises a NoRoomError is no room is specified, or a
        websocket.WebSocketException if the connection attempt(s) fail.
        Re-connections are tried.
        """
        self._connect()

    def close(self):
        """
        close() -> None

        Close the current connection (if any).
        Raises a websocket.WebSocketError is something unexpected happens.
        """
        self._disconnect()

    def reconnect(self):
        """
        reconnect() -> None

        Disrupt the current connection (if any) and estabilish a new one.
        Raises a NoRoomError if no room to connect to is specified.
        Raises a websocket.WebSocketException if the connection attempt
        fails.
        """
        self.close()
        self.connect()

    def get_connection(self):
        """
        get_connection() -> JSONWebSocket

        Obtain a reference to the current connection. Waits for all pending
        connects to finish. May return None if not connected.
        """
        with self._conncond:
            while self._connecting:
                self._conncond.wait()
            return self._connection

    def recv_raw(self, retry=True):
        """
        recv_raw(retry=True) -> object

        Receive a single object from the server, and return it.
        May raise a websocket.WebSocketException, or a NoConnectionError
        if not connected.
        If retry is true, the operation will be re-tried (after
        re-connects) before failing entirely.
        """
        if retry:
            return self._attempt(lambda c, a: self.recv_raw(False))
        conn = self.get_connection()
        if conn is None:
            raise NoConnectionError('Not connected')
        return conn.recv()

    def send_raw(self, obj, retry=True):
        """
        send_raw(obj, retry=True( -> object

        Try to send a single object over the connection.
        My raise a websocket.WebSocketException, or a NoConnectionError
        if not connected.
        If retry is true, the operation will be re-tried (after
        re-connects) before failing entirely.
        """
        if retry:
            return self._attempt(lambda c, a: self.send_raw(obj, False))
        conn = self.get_connection()
        if conn is None:
            raise NoConnectionError('Not connected')
        return conn.send(obj)

    def receive_single(self):
        """
        receive_single() -> None

        Receive and process a single packet.
        """
        packet = self.recv_raw()
        self.process_incoming(packet)

    def receive_loop(self):
        """
        receive_loop() -> None

        Receive packets until the connection collapses.
        """
        while 1: self.receive_single()

    def process_incoming(self, packet):
        """
        process_incoming(packet) -> None

        Process a single packet.
        """
        pass
