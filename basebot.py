# -*- coding: ascii -*-

"""
Bot library for euphoria.io.

Important functions and classes:
normalize_nick() : Normalize a nick (remove whitespace and convert it to
                   lower case). Useful for comparison of @-mentions.
parse_command()  : Split a string by whitespace and return a Token list.
format_datetime(): Format a UNIX timestamp nicely.
format_delta()   : Format a timestamp difference nicely.

Packet           : An Euphorian packet.
Message          : Representing a single message.
SessionView      : Representing a single session.

HeimEndpoint     : A bare-bones implementation of the API; useful for
                   minimalistic clients, or alternative expansion.
LoggingEndpoint  : HeimEndpoint maintaining a user list and chat logs on
                   demand.
BaseBot          : LoggingEndpoint supporting in-chat commands.
Bot              : BaseBot conforming to the botrulez
                   (github.com/jedevc/botrulez).
BotManager       : Class coordinating multiple Bot (or, more exactly,
                   HeimEndpoint) instances.
"""

# ---------------------------------------------------------------------------
# Preamble
# ---------------------------------------------------------------------------

# Version.
__version__ = "2.0"

# Modules - Standard library
import sys, os, re, time
import collections, json
import optparse
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

def scan_mentions(line):
    """
    scan_mentions(line) -> list

    Scan the given message for @-mentions and return them as a list of Token
    instances (in order).
    """
    ret, offset, l = [], 0, len(line)
    while offset < l:
        m = MENTION_RE.search(line, offset)
        if not m: break
        ret.append(Token(m.group(), m.start()))
        offset = m.end()
    return ret

def parse_command(line):
    """
    parse_command(line) -> list

    Parse a single-string command line into a list of Token-s (separated by
    whitespace in the original string).
    """
    ret, offset, l = [], 0, len(line)
    while offset < l:
        wm = WHITESPACE_RE.search(line, offset)
        if not wm:
            ret.append(Token(line[offset:], offset))
            break
        elif wm.start() == offset:
            offset = wm.end()
            continue
        ret.append(Token(line[offset:wm.start()], offset))
        offset = wm.end()
    return ret

def format_datetime(timestamp, fractions=True):
    """
    format_datetime(timestamp, fractions=True) -> str

    Produces a string representation of the timestamp similar to the
    ISO 8601 format: "YYYY-MM-DD HH:MM:SS.FFF UTC". If fractions is false,
    the ".FFF" part is omitted. As the platform the bots are used on is
    international, there is little point to use any kind of timezone but
    UTC.
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

def spawn_thread(_target, *_args, **_kwds):
    """
    spawn_thread(_target, *args, **_kwds) -> threading.Thread

    Utility function for spawning background threads.
    Create a threading.Thread instance configured with the given parameters,
    make it daemonic, start it, and return.
    """
    thr = threading.Thread(target=_target, args=_args, kwargs=_kwds)
    thr.setDaemon(True)
    thr.start()
    return thr

class Token(str):
    """
    Token(obj, offset) -> new instance

    A string that is at a certain offset inside some other string. The offset
    if exposed as an attribute.
    """

    def __new__(cls, obj, offset):
        inst = str.__new__(cls, obj)
        inst.offset = offset
        return inst

    def __repr__(self):
        return '%s(%s, %r)' % (self.__class__.__name__, str.__repr__(self),
                               self.offset)

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

    def __repr__(self):
        return '%s(%s)' % (self.__class__.__name__, dict.__repr__(self))

    def __getattr__(self, name):
        if name not in self._exports_:
            raise AttributeError(name)
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name, value):
        if name.startswith('_'):
            return dict.__setattr__(self, name, value)
        elif name not in self._exports_:
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
        "Initializer. See class docstring for invocation details."
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

# Documented by word-of-mouth.
class PersonalAccountView(AccountView):
    """
    PersonalAccountView is an AccountView with an additional Email field.

    Attributes:
    email: the email of the account
    id   : the id of the account (inherited)
    name : the name that the holder of the account goes by (inherited)
    """
    # Overrides parent class value.
    _exports_ = ('email', 'id', 'name')

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
    mention_list: Tuple of Token instances listing all the @-mentions in the
                  message (including the @ signs).
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
                self.__mention_list = tuple(scan_mentions(self.content))
            return self.__mention_list

    @property
    def mention_set(self):
        with self.__lock:
            if self.__mention_set is None:
                self.__mention_set = frozenset(i[1:]
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

class UserList(object):
    """
    UserList() -> new instance

    An iterable list of SessionView objects, with methods for modification
    and quick search.
    """

    def __init__(self):
        "Initializer. See class docstring for invocation details."
        self._list = []
        self._by_session_id = {}
        self._by_agent_id = {}
        self._by_name = {}
        self._lock = threading.RLock()

    def __iter__(self):
        """
        __iter__() -> iterator

        Iterate over all elements in self.
        """
        return iter(self.list())

    def add(self, *lst):
        """
        add(*lst) -> None

        Add all the SessionView-s in lst to self, unless already there.
        """
        with self._lock:
            for i in lst:
                if i.session_id in self._by_session_id:
                    orig = self._by_session_id.pop(i.session_id)
                    self._list.remove(orig)
                    self._by_agent_id[orig.id].remove(orig)
                    self._by_name[orig.name].remove(orig)
                self._list.append(i)
                self._by_session_id[i.session_id] = i
                self._by_agent_id.setdefault(i.id, []).append(i)
                self._by_name.setdefault(i.name, []).append(i)

    def remove(self, *lst):
        """
        remove(*lst) -> None

        Remove all the SessionView-s in lst from self (unless not there
        at all).
        """
        with self._lock:
            for i in lst:
                try:
                    orig = self._by_session_id.pop(i.session_id)
                except KeyError:
                    continue
                self._list.remove(orig)
                self._by_agent_id.get(orig.id, []).remove(orig)
                try:
                    self._by_name.get(orig.name, []).remove(orig)
                except ValueError:
                    pass

    def remove_matching(self, pattern):
        """
        remove_matching(pattern) -> None

        Remove all the SessionView-s from self where all the items present
        in pattern equal to the corresponding ones in the element; i.e.,
        a pattern of {'name': 'test'} will remove all entries with a 'name'
        value of 'test'. An empty pattern will remove all users.
        Used to implement the partition network-event.
        """
        if not pattern:
            self.clear()
            return
        with self._lock:
            rml, it = [], pattern.items()
            for i in self._list:
                for k, v in it:
                    try:
                        if i[k] != v:
                            break
                    except KeyError:
                        break
                else:
                    rml.append(i)
            self.remove(*rml)

    def clear(self):
        """
        clear() -> None

        Remove everything from self.
        """
        with self._lock:
            self._list[:] = ()
            self._by_session_id.clear()
            self._by_agent_id.clear()
            self._by_name.clear()

    def list(self):
        """
        list() -> list

        Return a (Python) list holding all the SessionViews currently in
        here.
        """
        with self._lock:
            return list(self._list)

    def for_session(self, id):
        """
        for_session(id) -> SessionView

        Return the SessionView corresponding session ID from self.
        Raises a KeyError if the given session is not known.
        """
        with self._lock:
            return self._by_session_id[id]

    def for_agent(self, id):
        """
        for_agent(id) -> list

        Return all the SessionViews known with the given agent ID as a list.
        """
        with self._lock:
            return list(self._by_agent_id.get(id, ()))

    def for_name(self, name):
        """
        for_name(name) -> list

        Return all the SessionViews known with the given name as a list.
        """
        with self._lock:
            return list(self._by_name.get(name, ()))

class MessageTree(object):
    """
    MessageTree() -> new instance

    Class representing a threaded chat log. Note that, because of Heim's
    never-forget policy, "deleted" messages are actually only flagged as
    such, and not "physically" deleted. Editing messages happens by
    re-adding them.
    """

    def __init__(self):
        "Initializer. See class docstring for invocation details."
        self._messages = {}
        self._children = {}
        self._earliest = None
        self._latest = None
        self._lock = threading.RLock()

    def __iter__(self):
        """
        __iter__() -> iterator

        Iterate over all elements in self in order.
        """
        return iter(self.list())

    def __getitem__(self, key):
        """
        __getitem__(key) -> Message

        Equivalent to self.get(key).
        """
        return self.get(key)

    def add(self, *lst):
        """
        add(*lst) -> None

        Incorporate all the messages in lst into self.
        """
        sorts = {}
        with self._lock:
            for msg in lst:
                self._messages[msg.id] = msg
                c = self._children.setdefault(msg.parent, [])
                if msg.id not in c: c.append(msg.id)
                if self._earliest is None or self._earliest.id > msg.id:
                    self._earliest = msg
                if self._latest is None or self._latest.id <= msg.id:
                    self._latest = msg
                sorts[id(c)] = c
            for l in sorts.values(): l.sort()

    def clear(self):
        """
        clear() -> None

        (Actually) remove all the messages from self.
        """
        with self._lock:
            self._messages.clear()
            self._children.clear()
            self._earliest = None
            self._latest = None

    def earliest(self):
        """
        earliest() -> Message

        Return the earliest message in self, or None of none.
        """
        with self._lock:
            return self._earliest

    def latest(self):
        """
        latest() -> Message

        Return the latest message in self, or None of none.
        """
        with self._lock:
            return self._latest

    def get(self, id):
        """
        get(id) -> Message

        Return the message corresponding to the given ID, or raise KeyError
        if no such message present.
        """
        with self._lock:
            return self._messages[id]

    def list(self, parent=None):
        """
        list(parent=None) -> list

        Return all the messages for the given parent (None for top-level
        messages) in an ordered list.
        """
        with self._lock:
            return [self._messages[i]
                    for i in self._children.get(parent, ())]

    def all(self):
        """
        all() -> list

        Return an ordered list containing all the messages in self.
        """
        with self._lock:
            l = list(self._messages.values())
            l.sort(key=lambda m: m.id)
            return l

# ---------------------------------------------------------------------------
# "Main" classes
# ---------------------------------------------------------------------------

class HeimEndpoint(object):
    """
    HeimEndpoint(**config) -> new instance

    Endpoint for the Heim protocol. Provides state about this endpoint and
    the connection, methods to submit commands, as well as call-back methods
    for some incoming replies/events, and dynamic handlers for arbitrary
    incoming packets. Re-connects are handled transparently.

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
                  is received. If None, no nick-name is set. Defaults to
                  the value of the NICKNAME class attribute (which, in turn,
                  defaults to None).
    passcode    : Passcode for private rooms. Sent during (re-)connection.
                  Defaults to None; no passcode is sent in that case.
    retry_count : Amount of re-connection attempts until an operation (a
                  connect or a send) fails. Defaults to 4.
    retry_delay : Amount of seconds to wait before a re-connection attempt.
                  Defaults to 10 seconds.
    timeout     : (Low-level) Connection timeout. Defaults to 60 seconds (as
                  the Heim server sends pings every 30 seconds, the
                  connection is either dead after that time, or generally
                  unstable).
    handlers    : Packet-type-to-list-of-callables mapping storing handlers
                  for incoming packets.
                  Handlers are called with the packet as the only argument;
                  the packet's '_self' item is set to the HeimEndpoint
                  instance that received the packet.
                  Handlers for the (virtual) packet type None (i.e. the None
                  singleton) are called for *any* packet, similarly to
                  handle_early() (but *after* the built-in handlers).
                  While commands and replies should be handled by the
                  call-back mechanism, built-in handler methods (on_*();
                  not in the mapping) are present for the asynchronous
                  events.
                  Event handlers are (indirectly) called from the input loop,
                  and should therefore finish quickly, or offload the work
                  to a separate thread. Mind that the Heim server will kick
                  any clients unreponsive for too long times!
                  While account-related event handlers are present, actual
                  support for accounts is lacking, and has to be implemented
                  manually.
    logger      : logging.Logger instance to log to. Defaults to the root
                  logger (at time of creation).
    manager     : BotManager instance responsible for this HeimEndpoint.

    Access to the attributes should be serialized using the instance lock
    (available in the lock attribute). The __enter__ and __exit__ methods
    of the lock are exposed, so "with self:" can be used instead of "with
    self.lock:". For convenience, packet handlers are called in a such
    context; if sections explicitly need not to be protected, manual calls
    to self.lock.release() and self.lock.acquire() become necessary.
    Note that, to actually take effect, changes to the roomname, nickname
    and passcode attributes must be peformed by using the corresponding
    set_*() methods (or by performing the necessary actions oneself).

    *Remember to call the parent class' methods, as some of its interna are
    implemented there!*

    Other attributes (not assignable by keyword arguments):
    cmdid       : ID of the next command packet to be sent. Used internally.
    callbacks   : Mapping of command ID-s to callables; used to implement
                  reply callbacks. Invoked after generic handlers.
    eff_nickname: The nick-name as the server returned it. May differ from
                  the one sent (truncation etc.).
    session_id  : Own session ID, or None if not connected.
    lock        : Attribute access lock. Must be acquired whenever an
                  attribute is changed, or when multiple accesses to an
                  attribute should be atomic.
    """

    # Default nick-name. Can be overridden by subclasses.
    NICKNAME = None

    def __init__(self, **config):
        "Initializer. See class docstring for invocation details."
        self.url_template = config.get('url_template', URL_TEMPLATE)
        self.roomname = config.get('roomname', None)
        self.nickname = config.get('nickname', self.NICKNAME)
        self.passcode = config.get('passcode', None)
        self.retry_count = config.get('retry_count', 4)
        self.retry_delay = config.get('retry_delay', 10)
        self.timeout = config.get('timeout', 60)
        self.handlers = config.get('handlers', {})
        self.logger = config.get('logger', logging.getLogger())
        self.manager = config.get('manager', None)
        self.cmdid = 0
        self.callbacks = {}
        self.eff_nickname = None
        self.session_id = None
        self.lock = threading.RLock()
        # Actual connection.
        self._connection = None
        # Whether someone is poking the connection.
        self._connecting = False
        # Condition variable to serialize all on.
        self._conncond = threading.Condition(self.lock)
        # Whether the session was properly initiated.
        self._logged_in = False
        # Whether a nick-name was set for the first time.
        self._nick_set = False

    def __enter__(self):
        return self.lock.__enter__()
    def __exit__(self, *args):
        return self.lock.__exit__(*args)

    def _make_connection(self, url, timeout):
        """
        _make_connection(url, timeout) -> JSONWebSocket

        Actually connect to url, with a time-out setting of timeout.
        Returns the object produced, or raises an exception.
        Can be hooked by subclasses.
        """
        self.logger.info('Connecting to %s...' % url)
        ret = JSONWebSocket(websocket.create_connection(url, timeout))
        self.logger.info('Connected.')
        return ret

    def _attempt(self, func, exchook=None):
        """
        _attempt(func, exchook=None) -> object

        Attempt to run func; if it raises an exception, re-try using the
        specified parameters (retry_count and retry_delay).
        func is called with three arguments, the zero-based trial counter,
        and the amount of re-tries that will be attempted, and the exception
        that happened during the last attempt, or None if none.
        exchook (if not None) is called immediately after an exception is
        caught, with the same arguments as func (and the exception object
        filled in); it may re-raise the exception to abort instantly. If
        exchook is None, a warning message will be logged.
        If the last attempt fails, the exception that indicated the
        failure is re-raised.
        If the function call succeeds, the return value of func is passed
        out.
        """
        with self.lock:
            count, delay = self.retry_count, self.retry_delay
        exc = None
        for i in range(count + 1):
            if i: time.sleep(delay)
            try:
                return func(i, count, exc)
            except Exception as e:
                exc = e
                if exchook is None:
                    self.logger.warning('Operation failed!', exc_info=True)
                else:
                    exchook(i, count, exc)
                if i == count:
                    raise
                continue

    def _attempt_reconnect(self, func):
        """
        _attempt_reconnect(func) -> object

        Same as _attempt(), but each repeated call of func is preceded to one
        of _reconnect(). Additional rather internal modifications are
        applied.
        """
        def callback(i, n, exc):
            if i: self._reconnect()
            return func(i, n, exc)
        def exchook(i, n, exc):
            if exc and i != n and not isinstance(exc, WSCCException):
                self.logger.warning('Operation failed (%r); '
                    'will re-connect...' % exc)
        return self._attempt(callback, exchook)

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
            timeout = self.timeout
        conn = None
        try:
            conn = self._attempt(
                lambda c, a, e: self._make_connection(url, timeout))
        finally:
            with self._conncond:
                self._connecting = False
                self._connection = conn
                if conn is not None:
                    self.handle_connect()
                self._conncond.notifyAll()

    def _disconnect(self, ok, final):
        """
        _disconnect(ok, final) -> None

        Internal back-end for close(). Takes care of synchronization.
        ok can be used to specify whether this was a "clean" close.
        final specifies whether this is a "final" close, after that
        the bot will not try to re-connect.
        """
        if ok:
            self.logger.info('Closing...')
        else:
            self.logger.info('Closing!')
        with self._conncond:
            while self._connecting:
                self._conncond.wait()
            if self._logged_in:
                self.handle_logout(ok, final)
                self._logged_in = False
            self._nick_set = False
            conn = self._connection
            self._connection = None
            self.handle_close(ok, final)
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
            if not self._connecting:
                self._connection = None
            while self._connecting:
                self._conncond.wait()
            if self._connection is not None:
                return
            if self._logged_in:
                self.handle_logout(False, False)
                self._logged_in = False
            self._nick_set = False
            self.handle_close(False, False)
            self._connecting = True
            url = self.url_template.format(self.roomname)
            timeout = self.timeout
        conn = None
        try:
            conn = self._make_connection(url, timeout)
        finally:
            with self._conncond:
                self._connecting = False
                self._connection = conn
                if conn is not None:
                    self.handle_connect()
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
        self._disconnect(True, True)

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

    def handle_connect(self):
        """
        handle_connect() -> None

        Called after a connection attempt succeeded.
        """
        pass

    def handle_close(self, ok, final):
        """
        handle_close(ok, final) -> None

        Called after a connection failed (or was normally closed).
        The ok parameter tells whether the close was normal (ok is true)
        or abnormal (ok is false); final tells whether the bot will try
        to re-connect itself (final is false) or not (final is true).
        If ok is true, messages may be sent.
        """
        self.eff_nickname = None
        self.session_id = None
        if self.manager: self.manager.handle_close(self, ok, final)

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
            return self._attempt_reconnect(
                lambda c, a, e: self.recv_raw(False))
        conn = self.get_connection()
        if conn is None:
            raise NoConnectionError('Not connected')
        return conn.recv()

    def send_raw(self, obj, retry=True):
        """
        send_raw(obj, retry=True) -> object

        Try to send a single object over the connection.
        My raise a websocket.WebSocketException, or a NoConnectionError
        if not connected.
        If retry is true, the operation will be re-tried (after
        re-connects) before failing entirely.
        """
        if retry:
            return self._attempt_reconnect(
                lambda c, a, e: self.send_raw(obj, False))
        conn = self.get_connection()
        if conn is None:
            raise NoConnectionError('Not connected')
        return conn.send(obj)

    def handle(self, packet):
        """
        handle(packet) -> None

        Handle a single packet.
        After wrapping structures in the reply into the corresponding
        record classes, handle_early(), built-in handlers, generic type
        handlers, and call-backs are invoked (in that order).
        The '_self' item of the packet is set to the HeimEndpoint instance
        the packet is handled by, to aid external call-backs.
        """
        try:
            packet = self._postprocess_packet(packet)
        except KeyError:
            pass
        with self.lock:
            # Global handler.
            self.handle_early(packet)
            # Built-in handlers
            p, t = packet, packet.get('type')
            if   t == 'bounce-event'      : self.on_bounce_event(p)
            elif t == 'disconnect-event'  : self.on_disconnect_event(p)
            elif t == 'edit-message-event': self.on_edit_message_event(p)
            elif t == 'hello-event'       : self.on_hello_event(p)
            elif t == 'join-event'        : self.on_join_event(p)
            elif t == 'login-event'       : self.on_login_event(p)
            elif t == 'logout-event'      : self.on_logout_event(p)
            elif t == 'network-event'     : self.on_network_event(p)
            elif t == 'nick-event'        : self.on_nick_event(p)
            elif t == 'part-event'        : self.on_part_event(p)
            elif t == 'ping-event'        : self.on_ping_event(p)
            elif t == 'send-event'        : self.on_send_event(p)
            elif t == 'snapshot-event'    : self.on_snapshot_event(p)
            # Special built-in handler.
            if t is not None and t.endswith('-reply'): self.handle_reply(p)
            # Typeless handlers
            self._run_handlers(None, packet)
            # Type handlers
            tp = packet.get('type')
            if tp: self._run_handlers(tp, packet)
            # Call-backs
            cb = self.callbacks.pop(packet.get('id'), None)
            if callable(cb): cb(packet)
            # Other global handler.
            self.handle_any(packet)

    def _postprocess_packet(self, packet):
        """
        _postprocess_packet(packet) -> dict

        Wrap structures in packet into the corresponding wrapper classes.
        The '_self' item of packet is set to the HeimEndpoint instance the
        method is called on.
        Used by handle(). May or may not modify the given dict, or any of
        its members, as well as return an entirely new one, as it actually
        does.
        May raise a KeyError if the packet is missing required fields.
        """
        tp = packet['type']
        if tp in ('get-message-reply', 'send-reply', 'edit-message-reply',
                  'edit-message-event', 'send-event'):
            packet['data'] = self._postprocess_message(packet['data'])
        elif tp == 'log-reply':
            data = packet['data']
            data['log'] = [self._postprocess_message(m) for m in data['log']]
        elif tp == 'who-reply':
            packet['data'] = [self._postprocess_sessionview(e)
                              for e in packet['data']]
        elif tp == 'hello-event':
            data = packet['data']
            try:
                data['account'] = self._postprocess_personalaccountview(
                    data['account'])
            except KeyError:
                pass
            data['session'] = self._postprocess_sessionview(data['session'])
        elif tp in ('join-event', 'part-event'):
            packet['data'] = self._postprocess_sessionview(packet['data'])
        elif tp == 'snapshot-event':
            data = packet['data']
            data['listing'] = [self._postprocess_sessionview(e)
                               for e in data['listing']]
            data['log'] = [self._postprocess_message(m) for m in data['log']]
        packet['_self'] = self
        return Packet(packet)

    def _postprocess_message(self, msg):
        """
        _postprocess_message(msg) -> dict

        Wrap a Message structure into the corresponding wrapper class.
        Used by _postpocess_packet().
        """
        msg['sender'] = self._postprocess_sessionview(msg['sender'])
        return Message(msg)

    def _postprocess_sessionview(self, view):
        """
        _postprocess_sessionview(view) -> dict

        Wrap a SessionView structure into the corresponding wrapper class.
        Used by _postpocess_packet().
        """
        return SessionView(view)

    def _postprocess_personalaccountview(self, view):
        """
        _postprocess_personalaccountview(view) -> dict

        Wrap a PersonalAccountView structure into the corresponding wrapper
        class.
        Used by _postprocess_packet().
        """
        return PersonalAccountView(view)

    def handle_early(self, packet):
        """
        handle_early(packet) -> None

        Handle a single post-processed packet.
        Can be used as a catch-all handler; called by handle().
        """
        pass

    def handle_reply(self, packet):
        """
        handle_reply(packet) -> None

        Handle an arbitrary command reply.
        Useful for checking command replies non-specifically. Called by
        handle().
        """
        if packet.type == 'nick-reply':
            self.eff_nickname = packet.data['to']
            if not self._nick_set:
                self.handle_nick_set()
                self._nick_set = True

    def on_bounce_event(self, packet):
        """
        on_bounce_event(packet) -> None

        Built-in event packet handler. Used internally for the login
        procedure.
        """
        #if ('passcode' in packet.data.get('auth_options', ()) and
        #        self.passcode is not None):
        # Reporting of the possibility to authenticate using a passcode is
        # NYI (as of 2015-12-19), so just try it:
        if self.passcode is not None:
            self.set_passcode()

    def on_disconnect_event(self, packet):
        """
        on_disconnect_event(packet) -> None

        Built-in event packet handler. Used internally for the login
        procedure.
        """
        # Gah! Hardcoded messages!
        if packet.get('reason') == 'authentication changed':
            self.reconnect()

    def on_hello_event(self, packet):
        """
        on_hello_event(packet) -> None

        Built-in event packet handler.
        """
        # Not mentioned in the API docs (last time I checked), but this
        # packet is the *very* first one the server sends.
        self.session_id = packet.data['session'].session_id

    def on_join_event(self, packet):
        """
        on_join_event(packet) -> None

        Built-in event packet handler.
        """
        pass

    def on_login_event(self, packet):
        """
        on_login_event(packet) -> None

        Built-in event packet handler.
        """
        pass

    def on_logout_event(self, packet):
        """
        on_logout_event(packet) -> None

        Built-in event packet handler.
        """
        pass

    def on_network_event(self, packet):
        """
        on_network_event(packet) -> None

        Built-in event packet handler.
        """
        pass

    def on_nick_event(self, packet):
        """
        on_nick_event(packet) -> None

        Built-in event packet handler.
        """
        pass

    def on_edit_message_event(self, packet):
        """
        on_edit_message_event(packet) -> None

        Built-in event packet handler.
        """
        pass

    def on_part_event(self, packet):
        """
        on_part_event(packet) -> None

        Built-in event packet handler. Used internally for the login
        procedure.
        """
        pass

    def on_ping_event(self, packet):
        """
        on_ping_event(packet) -> None

        Handle a ping-event with a ping-reply.
        The only client-side reply required by the protocol.
        """
        self.send_packet('ping-reply', time=packet.get('time'))

    def on_send_event(self, packet):
        """
        on_send_event(packet) -> None

        Built-in event packet handler.
        """
        pass

    def on_snapshot_event(self, packet):
        """
        on_snapshot_event(packet) -> None

        Built-in event packet handler.
        """
        self.logger.info('Logged in.')
        self.handle_login()
        self._logged_in = True
        self.set_nickname()

    def _run_handlers(self, pkttype, packet):
        """
        _run_handlers(pkttype, packet) -> None

        Run the handlers for type pkttype on packet. pkttype must be not the
        same as the type of packet.
        """
        with self.lock:
            for h in self.handlers.get(pkttype, ()):
                h(packet)

    def handle_any(self, packet):
        """
        handle_any(packet) -> None

        Handle an arbitrary packet.
        Called by handle(), last in the handler chain.
        """
        pass

    def handle_login(self):
        """
        handle_login() -> None

        Called when a session is initialized (but before setting the
        nick-name, if any; after handle_connect()), or after a successful
        re-connect. A session may not be estabilished at all for a connection
        (like, when the bot tries to connect to a private room and does not
        have appropriate credentials).
        """
        pass

    def handle_nick_set(self):
        """
        handle_nick_set() -> None

        Called after a session is estabilished, and a nick-name is set for
        the first time.
        May not be called at all for the reasons described in handle_login(),
        or if no nick-name is set at all (during log-in); will however be
        called if that happens later.
        There is no counterpart method; use handle_logout() if necessary.
        """
        pass

    def handle_logout(self, ok, final):
        """
        handle_logout(ok, final) -> None

        Called when a session ends or before a re-connect; before
        handle_close().
        If ok is true, messages may be sent.
        """
        pass

    def handle_single(self):
        """
        handle_single() -> None

        Receive and process a single packet.
        """
        self.handle(self.recv_raw())

    def handle_loop(self):
        """
        handle_loop() -> None

        Receive packets until the connection collapses.
        """
        while 1: self.handle_single()

    def add_handler(self, pkttype, handler):
        """
        add_handler(pkttype, handler) -> None

        Register a handler for handling packets of type pkttype.
        """
        with self.lock:
            l = self.handlers.setdefault(pkttype, [])
            if handler not in l: l.append(handler)

    def remove_handler(self, handler):
        """
        remove_handler(handler) -> None

        Remove any bindings of the given handler.
        """
        with self.lock:
            for e in self.handlers.values():
                try:
                    e.remove(handler)
                except ValueError:
                    pass

    def set_callback(self, id, cb):
        """
        set_callback(id, cb) -> None

        Set the callback for the given message ID. Override the previously
        set one, or, if cb is None, remove it.
        """
        with self.lock:
            if cb is None:
                self.callbacks.pop(id, None)
            else:
                self.callbacks[id] = cb

    def send_packet_raw(self, type, callback=None, data=Ellipsis):
        """
        send_packet_raw(type, callback=None, data=Ellipsis) -> str

        Send a packet to the server.
        Differently to send_packet(), keyword arguments are not used,
        and arbitrary data can therefore be specified. Returns the
        serial ID of the packet sent.
        """
        with self.lock:
            cmdid = str(self.cmdid)
            self.cmdid += 1
            if callback is not None:
                self.callbacks[cmdid] = callback
        pkt = {'type': type, 'id': cmdid}
        if data is not Ellipsis: pkt['data'] = data
        self.send_raw(pkt)
        return cmdid

    def send_packet(_self, _type, _callback=None, **_data):
        """
        send_packet(_type, _callback=None, **_data) -> str

        Send a packet to the server.
        The packet type is specified as a positional argument, an optional
        callback for handling the server's reply may be specified as well;
        the payload of the packet is passed as keyword arguments. Returns
        the sequential ID of the packet sent.
        May raise any exception send_raw() raises.
        """
        return _self.send_packet_raw(_type, _callback, _data)

    def set_roomname(self, room=None):
        """
        set_roomname(room=None) -> None

        Set the roomname attribute, and (as a "side effect") connect to
        that room (if already connected). If room is None, perform no
        action.
        """
        if room is None: return
        with self.lock:
            self.roomname = room
            if self.get_connection() is not None:
                reconn = True
            else:
                reconn = False
        if reconn: self.reconnect()

    def set_nickname(self, nick=Ellipsis):
        """
        set_nickname(nick=Ellipsis) -> msgid or None

        Set the nickname attribute to nick (unless nick is Ellipsis), and
        send a corresponding command to the server (if connected, and
        nickname is non-None).
        Returns the sequential message ID if a command was sent.
        """
        with self.lock:
            # Ellipsis FTW!
            if nick is not Ellipsis: self.nickname = nick
            if (self.get_connection() is not None and
                    self.nickname is not None):
                self.logger.info('Setting nickname: %r' % self.nickname)
                return self.send_packet('nick', name=self.nickname)

    def set_passcode(self, code=Ellipsis):
        """
        set_passcode(code=Ellipsis) -> msgid or None

        Set the passcode attribute to code (unless code is Ellipsis), and
        send a corresponding command to the server (if connected, and
        passcode is non-None).
        Returns the sequential message ID if a command was sent.
        """
        with self.lock:
            if code is not Ellipsis: self.passcode = code
            if (self.get_connection() is not None and
                    self.passcode is not None):
                self.logger.info('Authenticating with passcode...')
                return self.send_packet('auth', type='passcode',
                                        passcode=self.passcode)

    def main(self):
        """
        main() -> None

        "Main" method. Connects to the configured room, runs an event loop,
        and closes whenever that aborts (normally or due to an exception).
        """
        self.connect()
        ok = True
        try:
            self.handle_loop()
        except Exception:
            ok = False
            self.logger.error('Crashed!', exc_info=True)
            raise
        finally:
            self._disconnect(ok, True)

class LoggingEndpoint(HeimEndpoint):
    """
    LoggingEndpoint(**config) -> New instance.

    A HeimEndpoint that maintains a user list and chat logs on demand.
    See HeimEndpoint on configuration details.

    Additional attributes (configurable through keyword arguments):
    log_users    : Maintain a user list (if false, it will be empty; defaults
                   to False).
    log_messages : Maintain a chat log (if false, it will be empty; defaults
                   to False).
    chat_handlers: List of handler functions to call when new chat messages
                   arrive. Invoked like handle_chat(); after all other
                   handlers.

    If log_users or log_messages are changed during operation, the values in
    the corresponding list (see below) cannot be relied upon.

    ...More additional attributes:
    users   : A UserList, holding the current user list (or nothing).
    messages: A MessageTree, holding the chat logs (in "natural" order;
              or nothing).
    """

    def __init__(self, **config):
        "Initializer. See class docstring for invocation details."
        HeimEndpoint.__init__(self, **config)
        self.log_users = config.get('log_users', False)
        self.log_messages = config.get('log_messages', False)
        self.chat_handlers = config.get('chat_handlers', [])
        self.users = UserList()
        self.messages = MessageTree()

    def handle_close(self, ok, final):
        "See HeimEndpoint.handle_close() for details."
        HeimEndpoint.handle_close(self, ok, final)
        self.users.clear()
        self.messages.clear()

    def handle_early(self, packet):
        "See HeimEndpoint.handle_early() for details."
        HeimEndpoint.handle_early(self, packet)
        if self.log_users:
            if packet.type == 'who-reply':
                self.users.add(*packet.data)
            elif packet.type == 'snapshot-event':
                self.users.add(*packet.data['listing'])
            elif packet.type == 'network-event':
                if packet.data['type'] == 'partition':
                    self.users.remove_matching({
                        'server_id': packet.data['server_id'],
                        'server_era': packet.data['server_era']})
            elif packet.type == 'nick-event':
                usr = self.users.for_session(packet.data['session_id'])
                usr.name = packet.data['to']
            elif packet.type == 'join-event':
                self.users.add(packet.data)
            elif packet.type == 'part-event':
                self.users.remove(packet.data)
        if self.log_messages:
            if packet.type in ('edit-message-reply', 'send-reply',
                               'edit-message-event', 'send-event'):
                self.messages.add(packet.data)
            elif packet.type == 'get-message-reply':
                self.messages.add(packet.data)
            elif packet.type == 'log-reply':
                self.messages.add(*packet.data['log'])
            elif packet.type == 'snapshot-event':
                self.messages.add(*packet.data['log'])

    def handle_any(self, packet):
        "See HeimEndpoint.handle_any() for details."
        HeimEndpoint.handle_any(self, packet)
        if packet.type in ('edit-message-reply', 'send-reply',
                           'edit-message-event', 'send-event'):
            self._run_chat_handlers(packet.data, {
                'own': packet.type.endswith('-reply'),
                'edit': packet.type.startswith('edit-'),
                'long': False,
                'live': (packet.type == 'send-event'),
                'packet': packet,
                'self': self})
        elif packet.type == 'get-message-reply':
            sid = packet.data.sender.session_id
            self._run_chat_handlers(packet.data, {
                'own': (sid == self.session_id), 'edit': False,
                'long': True, 'live': False, 'packet': packet,
                'self': self})
        elif packet.type == 'log-reply':
            self.handle_logs(packet.data['log'],
                {'snapshot': False, 'raw': packet, 'self': self})
        elif packet.type == 'snapshot-event':
            self.handle_logs(packet.data['log'],
                {'snapshot': True, 'raw': packet, 'self': self})

    def _run_chat_handlers(self, msg, meta):
        """
        _run_chat_handlers(msg, meta) -> None

        Invoke handle_chat() and all the handlers in chat_handlers with the
        given arguments.
        """
        self.handle_chat(msg, meta)
        for h in self.chat_handlers:
            h(msg, meta)

    def handle_chat(self, msg, meta):
        """
        handle_chat(msg, meta) -> None

        Invoked for every "live" chat message received.
        msg is the Message being dealt with; meta is a dict storing certain
        properties of the message:
        own   : Whether the message is an own message (either from a command
                reply, or (in case of get-message) from the same session ID
                as the current one).
        edit  : Whether the message comes from an edit-event or -reply (i.e.,
                whether the messages was edited post factum).
        long  : Whether the message comes from a get-message-reply and has
                the entire (possibly long) content in it (check the truncated
                member of the message, just to be sure).
        live  : Equivalent to the expression (not own and not edit and
                not long). Useful for testing whether this is a "live"
                message to be replied to.
        packet: The packet the message originated from.
        self  : The LoggingEndpoint instance this command is invoked from.
        """
        pass

    def handle_logs(self, msglist, meta):
        """
        handle_logs(self, msglist, meta) -> None

        Invoked for every piece of past chat logs received.
        msglist is a list of Message-s; meta is a dict containing
        meta-information:
        snapshot: Whether the messages came from a snapshot-event (whichever
                  use one might have from that).
        packet  : The packet the messages originated from.
        self    : The LoggingEndpoint instance this command is invoked from.
        """
        pass

    def send_chat(self, content, parent=None, **meta):
        """
        send_chat(content, parent=None, **meta) -> seqid

        Send a chat message. content is the content of the message, parent
        the message ID to reply to (or None for starting a new thread).
        Items from meta are copied into the packet without further
        examination.
        The sequential ID of the packet sent is returned.
        A call-back may be specified by using the _callback keyword
        argument.
        """
        self.logger.info('Sending message: %r' % (content,))
        return self.send_packet('send', content=content, parent=parent,
                                **meta)

    def add_chat_handler(self, handler):
        """
        add_chat_handler(handler) -> None

        Add the given chat handler to self.
        """
        with self.lock:
            if handler in self.chat_handlers: return
            self.chat_handlers.append(handler)

    def remove_chat_handler(self, handler):
        """
        remove_chat_handler(handler) -> None

        Remove the given chat handler from self.
        """
        with self.lock:
            try:
                self.chat_handlers.remove(handler)
            except ValueError:
                pass

    def refresh_users(self):
        """
        refresh_users() -> None

        Clear the user list, and send a request to re-fill it.
        Note that the actual user list update will happen asynchronously.
        Returns the ID of the packet sent.
        """
        with self.lock:
            self.users.clear()
            return self.send_packet('who')

    def refresh_logs(self, n=100):
        """
        refresh_logs(n=100) -> None

        Clear the message logs, and send a request to re-fill them
        (partially). n is the amount of messages to request.
        Note that the actual logs update will happen asynchronously.
        Returns the ID of the packet sent.
        """
        with self.lock:
            self.messages.clear()
            return self.send_packet('log', n=n)

# ;)
class BaseBot(LoggingEndpoint):
    """
    BaseBot(roomname=None, **config) -> new instance

    A LoggingEndpoint that supports commands.
    For symmetry with Bot (and because this is the only setting necessary to
    start a bot), the roomname configuration value is provided as the only
    positional argument (it may still be specified as a keyword argument).

    Attributes (settable via config):
    command_handlers: Mapping of command name strings to lists of handler
                      callables for the command. Called as handle_command(),
                      see there. Handlers for the None command (similarly to
                      packet handlers for None) are called for any command.
    """

    def __init__(self, roomname=None, **config):
        "Initializer. See class docstring for details."
        LoggingEndpoint.__init__(self, roomname=roomname, **config)
        self.command_handlers = config.get('command_handlers', {})

    def _run_chat_handlers(self, msg, meta):
        """
        _run_chat_handlers(msg, meta) -> None

        Invoke handle_chat() and all the handlers in chat_handlers with the
        given arguments; also run command handlers.
        (Overriding same-named method from LoggingEndpoint.)
        """
        LoggingEndpoint._run_chat_handlers(self, msg, meta)
        if msg.content.startswith('!'):
            parts = parse_command(msg.content)
            self.logger.info('Got command: ' +
                             ' '.join(map(repr, map(str, parts))))
            meta = {'line': msg.content, 'msg': msg, 'msg_meta': meta,
                    'msgid': msg.id, 'packet': meta['packet']}
            self.handle_command(parts, meta)
            self._run_command_handlers(None, parts, meta)
            cmd = parts[0][1:]
            if cmd: self._run_command_handlers(cmd, parts, meta)

    def _run_command_handlers(self, cmd, cmdline, meta):
        """
        _run_command_handlers(cmd, cmdline, meta) -> None

        Run the handlers for command cmd with the parameters cmdline and
        meta.
        cmd must not necessarily represent the command encoded in
        cmdline.
        """
        for h in self.command_handlers.get(cmd, ()):
            h(msg, meta)

    def handle_command(self, cmdline, meta):
        """
        handle_command(cmdline, meta) -> None

        Handle an arbitrary command. cmdline is a list of Tokens-s, as
        returned by parse_command() (differently from the command names
        handlers can be registered for, the very first token includes
        the leading exclamation mark); meta is a dictionary holding
        meta-data:
        line    : The complete command line (the content of the Message the
                  command it in).
        msg     : The Message the command stems from.
        msg_meta: Meta-data about msg, as described in handle_chat() in
                  LoggingEndpoint.
        packet  : The Packet the message comes from.
        msgid   : The ID of message.
        """
        pass

    def add_command_handler(self, cmd, handler):
        """
        add_command_handler(cmd, handler) -> None

        Register a handler for the given command (specified without the
        leading exclamation mark).
        """
        with self.lock:
            l = self.command_handlers.setdefault(cmd, [])
            if handler not in l: l.append(handler)

    def remove_command_handler(self, handler):
        """
        remove_handler(handler) -> None

        Remove any bindings of the given handler.
        """
        with self.lock:
            for e in self.command_handlers.values():
                try:
                    e.remove(handler)
                except ValueError:
                    pass

class Bot(BaseBot):
    """
    Bot(roomname=None, **config) -> new instance

    A BaseBot that implements the botrulez (github.com/jedevc/botrulez):
    !ping[ @myname]  -> Reply with a "Pong!".
    !help[ @myname]  -> Reply with a help message.
    !uptime @myhname -> Reply with a message informing about this bot's
                        current uptime, of the kind:
                        /me is up since <datetime> (<timediff>)

    Instance variables (settable via config):
    do_stdcommands: Whether the standard commands should be respected at all.
                    Defaults to True.
    ping_text     : The text to reply with to a (general) !ping command. May
                    be None to indicate that the command should be ignored.
                    Defaults to "Pong!".
    spec_ping_text: The text to reply with to a specific !ping command. May
                    be None as well. Defaults to Ellipsis, which means the
                    value of ping_text will be used.
    short_help    : A short (preferably one-line) message to reply with to a
                    !help command. May be None to ignore the command. The
                    default is the class attribute SHORT_HELP, which defaults
                    to None.
    long_help     : Message to reply with to a specific !help command. May be
                    long and elaborate, of whichever style is appropriate, or
                    None not to reply at all; if Ellipsis, short_help is used
                    instead. The default is the class attribute LONG_HELP,
                    which itself defaults to Ellipsis.
    do_uptime     : Boolean indicating whether the !uptime command should be
                    replied to. Defaults to True.
    do_gen_uptime : Boolean indicating whether the generic !uptime command
                    should be replied to. Defaults to False, as the botrulez
                    discourage it, but still provided for symmetry to the
                    other commands.
    aliases       : List of alternate nick-names to accept in commands as
                    oneself instead of the "current" one (useful for "gauge
                    bots", which display information in their nick-name).
                    Defaults to an empty list.
    started       : UNIX timestamp of when the bot started. Defaults to the
                    time when the constructor was called.
    """

    # Default short_help value.
    SHORT_HELP = None

    # Default long_help value.
    LONG_HELP = Ellipsis

    def __init__(self, roomname=None, **config):
        "Initializer. See class docstring for invocation details."
        BaseBot.__init__(self, roomname, **config)
        self.do_stdcommands = config.get('do_stdcommands', True)
        self.ping_text = config.get('ping_text', 'Pong!')
        self.spec_ping_text = config.get('spec_ping_text', Ellipsis)
        self.short_help = config.get('short_help', self.SHORT_HELP)
        self.long_help = config.get('long_help', self.LONG_HELP)
        self.do_uptime = config.get('do_uptime', True)
        self.do_gen_uptime = config.get('do_gen_uptime', False)
        self.aliases = config.get('aliases', [])
        self.started = config.get('started', time.time())

    def handle_command(self, cmdline, meta):
        """
        handle_command(cmdline, meta) -> None

        Handle an arbitrary command.
        See BaseBot.handle_command() for details.
        Overridden to implement the botrulez commands.
        """
        # Convenience function for choosing a reply and sending it.
        def reply(text, alttext=None):
            if text is Ellipsis:
                text = alttext
            if text is not None:
                self.send_chat(text, meta['msgid'])
        # Convenience function for checking if the command is specific and
        # matches myself.
        def nick_matches():
            if len(cmdline) != 2:
                return False
            ms = cmdline[1]
            if not ms.startswith('@'): return False
            nn = normalize_nick(ms[1:])
            if nn == normnick:
                return True
            for i in self.aliases:
                if nn == normalize_nick(i):
                    return True
            return False
        # Call parent class method.
        BaseBot.handle_command(self, cmdline, meta)
        # Don't continue if no command or explicitly forbidden.
        if not cmdline or not self.do_stdcommands:
            return
        # Used in nick_matches().
        normnick = normalize_nick(self.eff_nickname or self.nickname or '')
        # Actual commands.
        if cmdline[0] == '!ping':
            if len(cmdline) == 1:
                reply(self.ping_text)
            elif nick_matches():
                reply(self.spec_ping_text, self.ping_text)
        elif cmdline[0] == '!help':
            if len(cmdline) == 1:
                reply(self.short_help)
            elif nick_matches():
                reply(self.long_help, self.short_help)
        elif cmdline[0] == '!uptime':
            if (self.do_uptime and len(cmdline) == 1 or
                    self.do_gen_uptime and nick_matches()):
                if self.started is None:
                    reply("/me Uptime information is N/A")
                else:
                    reply('/me is up since %s (%s)' % (
                        format_datetime(self.started),
                        format_timedelta(time.time() - self.started)))

class MiniBot(Bot):
    """
    MiniBot(roomname=None, **config) -> new instance

    A Bot that provides convenience features for quick development.

    Configuration values (mirrored in attributes):
    regexes   : A regex-object mapping for message handling, or a sequence of
                regex-object pairs, which will be converted to an ordered
                mapping implicitly. Each incoming message is checked against
                the keys of it (in the order the mapping's iterator returns
                them) by re.match(), and, if the regex matches, the
                corresponding value will be processed as described below.
    match_self: Whether messages from oneself should be replied to. Defaults
                to False, meaning no.
    match_all : Whether every regex should be checked against every message
                (True), or whether matching should stop after the first regex
                found (False; the default).

    Call-back handling:
    1. If the object is callable, step 4 will consider the result of the
       object's call on the match object as the first argument, and a
       dictionary with additional meta-data as a second argument:
       self    : The MiniBot instance that started the call.
       msg     : The Message the text matched came from.
       msg_meta: Meta-data about msg, as described in handle_chat() in
                 LoggingEndpoint.
       msgid   : The ID of msg.
       packet  : The packet the message was from.
       reply   : A function that, taking a single string as an argument,
                 sends a reply to the message that caused this call-back,
                 with the given string as the contents.
       (Steps 2 and 3 are skipped in this case.)
    2. If the object is not callable, but a tuple or list of strings, step 3
       is performed on each element of it (in order), otherwise, on the
       object itself.
    3. The object is applied the match's expand method, allowing to re-use
       matched groups in the reply.
    4. One (or possibly more) replies to the "original" message are
       constructed from the object:
       If the object is None, nothing is replied;
       if it is a single string, it is replied with;
       if it is a sequence of strings, each element is replied with.
    """

    def __init__(self, roomname=None, **config):
        "Initializer. See class docstring for details."
        Bot.__init__(self, roomname, **config)
        self.regexes = config.get('regexes', {})
        self.match_self = config.get('match_self', False)
        self.match_all = config.get('match_all', False)
        self._orig_regexes = self.regexes
        if not hasattr(self.regexes, 'keys'):
            self.regexes = collections.OrderedDict(self.regexes)

    def handle_chat(self, msg, meta):
        "See Bot.handle_chat() for details."
        if not self.match_self and meta['own']:
            return
        text, logged = msg.content, False
        for k, v in self.regexes.items():
            m = re.match(k, text)
            if not m: continue
            if not logged:
                self.logger.info('Trigger message: %r' % text)
                logged = True
            self._process_callbacks(msg, meta, m, v)
            if not self.match_all:
                return

    def _process_callbacks(self, msg, meta, match, value):
        """
        _process_callbacks(msg, meta, match, value) -> None

        Handle a chat message call-back.
        Used internally.
        """
        if callable(value):
            reply = lambda text: self.send_chat(text, msg.id)
            value = value(match, {'self': self, 'msg': msg,
                'msg_meta': meta, 'msgid': msg.id,
                'packet': meta['packet'], 'reply': reply})
            expand = False
        else:
            expand = True
        if value is None:
            rlist = ()
        elif isinstance(value, (list, tuple)):
            rlist = value
        else:
            rlist = (value,)
        for v in rlist:
            if expand: v = match.expand(v)
            self.send_chat(v, msg.id)

class BotManager(object):
    """
    BotManager(**config) -> new instance

    Class coordinating multiple Bot (or, equally, HeimEndpoint) instances;
    providing simple spawning and re-starting.

    config specifies configuration values:
    botcls         : Bot class for creating new bots. It is assumed that
                     instances can be constructed as with the HeimEndpoint
                     class. Defaults to None, meaning no new bots can be
                     created (from the BotManager instance).
    botcfg         : Dictionary of configuration values to pass to
                     newly-created bots. Defaults to the keyword argument
                     dictionary itself, simplifying combined configuration.
    botname        : A symbolic name of the bot. Defaults to '<Bot>', unless
                     botcls is specified and has a BOTNAME attribute (whose
                     value is used in that case).
    bots           : A list of bots to be used. The manager attribute of all
                     entries will be re-assigned to self. Defaults to an
                     empty list.
    respawn_crashed: Re-spawn bots that crashed, if they were of the same
                     class that botcls is. Defaults to False.
    respawn_delay  : Wait for this amount of seconds before re-spawning a
                     bot. Defaults to 60.
    logger         : Logger to use. Defaults to the root logger.

    Additional instance variables:
    lock: A threading.RLock instance used for serializing attribute access.
          The __enter__ and __exit__ methods of the lock are exposed under
          the same name.
    """

    @classmethod
    def early_init(cls, config):
        """
        early_init(config) -> None

        Perform early initialization when run_main() is run.
        config is the dictionary of the arguments specified to run_main();
        it can be modified.
        The default implementation does nothing.
        """
        pass

    @classmethod
    def prepare_parser(cls, parser, config):
        """
        prepare_parser(parser, config) -> None

        Add custom options to parser (an optparse.OptionParser) instance.
        config is the dictionary of the arguments specified to run_main().
        The default implementation adds the --url-template, --nickname,
        --retry-count, --retry-delay, --loglevel, and --logfile options,
        whereof the first four map to corresponding HeimEndpoint keyword
        arguments, and the last two are used be BotManager.prepare_main().
        """
        parser.add_option('--url-template', dest='url_template')
        parser.add_option('--nickname', dest='nickname')
        parser.add_option('--retry-count', type=int, dest='retry_count')
        parser.add_option('--retry-delay', type=float, dest='retry_delay')
        parser.add_option('--loglevel', dest='loglevel',
                          default=config.get('loglevel', logging.INFO))
        parser.add_option('--logfile', dest='logfile',
                          default=config.get('logfile'))

    @classmethod
    def prepare_main(cls, options, arguments, config):
        """
        prepare_main(options, arguments, config) -> (bots, config)

        Perform final preparations for running; return parameters suitable
        for from_config().
        options and arguments are a optparse.Values object and a list of
        remaining positional arguments, respectively; config is the
        dictionary of arguments specified to run_main().
        The default implementation initializes the logging module according
        to the given options in addition to preparing the return value,
        arguments and config are passed through as it, after the options
        noted in prepare_parser() as "mapped to corresponding HeimEndpoint
        keyword arguments" that are not None are copied into config.
        """
        kwds = {'stream': sys.stderr}
        if options.logfile is not None:
            if isinstance(options.logfile, str):
                kwds['filename'] = options.logfile
            else:
                kwds['stream'] = options.logfile
        loglevel = options.loglevel
        logging.basicConfig(format='[%(asctime)s %(name)s %(levelname)s] '
            '%(message)s', datefmt='%Y-%m-%d %H:%M:%S', level=loglevel,
            **kwds)
        for name in ('url_template', 'nickname', 'retry_count',
                     'retry_delay'):
            value = getattr(options, name)
            if value is not None:
                config[name] = value
        return (arguments, config)

    @classmethod
    def from_config(cls, bots, config):
        """
        from_config(cls, bots, config) -> new instance

        Create a BotManager from configuration pairs (in config) and bot
        definitions (in bots). config should contain a botcls entry, or
        the call will fail (unless no bots are specified at all).
        bots is a list of tuples, which will be passed to the make_bot()
        method "unpackedly" (i.e., not as a single argument, but as as many
        as there are entries). If an entry of bots is a single string, it is
        regarded as either a "bare" room name, or a room name followed
        -- after a colon -- by a passcode, e.g., 'test' parses to ('test',),
        and 'top:secret' parses to ('top', 'secret').
        """
        mgr = cls(**config)
        for d in bots:
            if isinstance(d, str):
                room, sep, passcode = d.partition(':')
                if sep:
                    d = (room, passcode)
                else:
                    d = (room,)
            mgr.add_bot(mgr.make_bot(*d))
        return mgr

    def __init__(self, **config):
        "Initializer. See class docstring for invocation details."
        self.botcls = config.get('botcls', None)
        self.botcfg = config.get('botcfg', config)
        self.botname = config.get('botname',
                                  getattr(self.botcls, 'BOTNAME', '<Bot>'))
        self.bots = config.get('bots', [])
        self.respawn_crashed = config.get('respawn_crashed', False)
        self.respawn_delay = config.get('respawn_delay', 60.0)
        self.logger = config.get('logger', logging.getLogger())
        self.lock = threading.RLock()
        self._shutting_down = False
        self._joincond = threading.Condition(self.lock)
        for b in self.bots:
            with b.lock:
                b.manager = self

    def __enter__(self):
        return self.lock.__enter__()
    def __exit__(self, *args):
        return self.lock.__exit__(*args)

    def start(self):
        """
        start() -> None

        Spawn all the bots managed by this instance. May spawn "ghost" copies
        of bots already running. Does not wait for bots to finish.
        """
        self.logger.info('Starting %s...' % self.botname)
        with self.lock:
            for b in self.bots:
                spawn_thread(b.main)

    def shutdown(self):
        """
        shutdown() -> None

        Stop all the bots current running.
        """
        with self.lock:
            self._shutting_down = True
            l = list(self.bots)
        for b in l:
            b.close()
        with self._joincond:
            self._joincond.notifyAll()

    def join(self):
        """
        join() -> None

        Wait until all bots are removed from self (like, by being stopped).
        """
        with self.lock:
            while self.bots:
                # Remain responsive in Py2K.
                self._joincond.wait(10)

    def make_bot(self, roomname=Ellipsis, passcode=Ellipsis,
                 nickname=Ellipsis, logger=Ellipsis):
        """
        make_bot(roomname=Ellipss, passcode=Ellipsis, nickname=Ellipsis,
                 logger=Ellipsis) -> botcls instance

        Create a new Bot (or, HeimEndpoint) instance according to the
        internal configuration.
        If logger is Ellipsis, a new logger with the name informing about
        the room name and the nick-name of the bot is created; if it is
        None, the logger of self is inherited, otherwise, it is passed
        to the bot unchanged.
        For configuration, self.botcfg is taken as a default, and a
        dictionary made out of the non-Ellipsis arguments is merged with
        that, overriding any values from botcfg; that is used as the
        configuration for the new bot.
        May raise a TypeError if self.botcls is None.
        """
        with self.lock:
            if self.botcls is None:
                raise TypeError('Bot class not specified')
            cls = self.botcls
            cfg = dict(self.botcfg)
            if roomname is not Ellipsis: cfg['roomname'] = roomname
            if passcode is not Ellipsis: cfg['passcode'] = passcode
            if nickname is not Ellipsis: cfg['nickname'] = nickname
            if logger is Ellipsis:
                rn = None if roomname is Ellipsis else roomname
                nn = None if nickname is Ellipsis else nickname
                nn = nn or self.botname
                if not rn:
                    if not nn:
                        name = '<bot>'
                    else:
                        name = nn
                else:
                    if not nn:
                        name = '<bot>@' + rn
                    else:
                        name = nn + '@' + rn
                cfg['logger'] = logging.getLogger(name)
            elif logger is None:
                cfg['logger'] = self.logger
            else:
                cfg['logger'] = logger
        return cls(**cfg)

    def add_bot(self, bot):
        """
        add_bot(bot) -> None

        Add a bot to self; if already there, do nothing.
        Sets the bot's manager attribute to self (in any case).
        """
        with self.lock:
            if not bot in self.bots:
                self.bots.append(bot)
        with bot.lock:
            bot.manager = self

    def remove_bot(self, bot):
        """
        remove_bot(bot) -> None

        Remove a bot from self (if present).
        Sets the bot's manager attribute to None (in any case).
        """
        with bot.lock:
            bot.manager = None
        with self.lock:
            try:
                self.bots.remove(bot)
            except ValueError:
                pass
            if self._shutting_down:
                self._joincond.notifyAll()
                return

    def swap_bots(self, old, new):
        """
        swap_bots(old, new) -> None

        Remove a bot and add a new bot atomically.
        See add_bot() and remove_bot() for semantics.
        Used to prevent shutdowns due to all bots being respawned at the
        same time.
        """
        with old.lock:
            old.manager = None
        with self.lock:
            try:
                self.bots.remove(old)
            except ValueError:
                pass
            if not new in self.bots:
                self.bots.append(new)
        with new.lock:
            new.manager = self

    def handle_close(self, bot, ok, final):
        """
        handle_close(bot, ok, final) -> None

        Invoked by bot when it closes; ok tells whether the close was
        "clean"; final tells whether the bot will try to re-connect itself
        (final is true) or not (final is false). May respawn the bot if
        self is configured accordingly, and (in particular) bot is an
        instance of self.botcls.
        """
        def respawner():
            time.sleep(timeout)
            b.main()
        if not final:
            return
        try:
            with self.lock:
                if not isinstance(bot, self.botcls):
                    return
                elif not self.respawn_crashed:
                    return
                timeout = self.respawn_delay
            with bot.lock:
                r = bot.roomname
                p = bot.passcode
                n = bot.nickname
            if r is None:
                return
            try:
                b = self.make_bot(r, p, n)
            except TypeError:
                return
            self.swap_bots(bot, b)
            bot = None
            spawn_thread(respawner)
        finally:
            if bot:
                self.remove_bot(bot)
                with self._joincond:
                    self._joincond.notifyAll()

    def main(self):
        """
        main() -> None

        Spawn the pre-configured bots, and wait for all of them to finish.
        Equivalent to calling first start(), then join().
        """
        self.start()
        self.join()

def run_main(botcls=Ellipsis, **config):
    """
    run_main(botcls=Ellipsis, **config) -> None

    Initialize a BotManager, seed it with configuration from command-line
    arguments, and call its main() method. The botcls positional argument (if
    not Ellipsis) is copied into config before starting. Apart from values
    passed through to the BotManager constructor, the following configuration
    values are provided:
    mgrcls: Bot manager class to use. Defaults to the "bare" BotManager.
    argv  : List of positional arguments to parse (defaults to sys.argv[1:]).
    """
    if botcls is not Ellipsis:
        config['botcls'] = botcls
    mgrcls = config.get('mgrcls', BotManager)
    mgrcls.early_init(config)
    parser = optparse.OptionParser()
    mgrcls.prepare_parser(parser, config)
    options, arguments = parser.parse_args(config.get('argv', sys.argv[1:]))
    bots, cfg = mgrcls.prepare_main(options, arguments, config)
    inst = mgrcls.from_config(bots, cfg)
    try:
        inst.main()
    except KeyboardInterrupt:
        inst.shutdown()
        inst.join()

def run_minibot(**config):
    """
    run_minibot(**config) -> None

    Wrapper around run_main(), supplying the MiniBot class as a default for
    the botcls argument.
    """
    config.setdefault('botcls', MiniBot)
    run_main(**config)
