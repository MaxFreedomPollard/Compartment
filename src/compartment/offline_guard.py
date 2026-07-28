"""Runtime offline enforcement (invariant I1).

When activated (COMPARTMENT_ASSERT_OFFLINE=1 or --assert-offline), anything
that could put a byte of ours on the wire raises OfflineViolation and the
process aborts loudly. That is three doors, not one:

  1. Sockets. Creating an AF_INET/AF_INET6 socket raises. AF_UNIX and every
     other local family keep working, because local IPC is not networking.
  2. Name resolution. getaddrinfo, gethostbyname, gethostbyname_ex,
     gethostbyaddr, getnameinfo, getfqdn and create_connection raise for any
     host that is not already local. A DNS lookup needs no socket object at
     all, and it puts caller-chosen bytes on the wire in the query labels, so
     resolution alone is enough to empty a vault one label at a time.
  3. The C layer. socket.socket is a Python subclass of the C type
     _socket.socket, so patching the subclass alone leaves the base class
     standing open. The base is replaced too, and a process-wide audit hook
     sits underneath both of them and underneath any reference to
     socket.getaddrinfo that a library captured before we were activated.

What counts as local: no host at all, "localhost", and an address that is
already a literal (resolving a literal asks no one anything). Everything else
is a lookup and is refused.

stdio (the MCP transport) needs no sockets, so normal operation is
unaffected. The ONLY code allowed to bypass this is nothing: even
`setup download-model` refuses to run while the guard is active.

Activation from the environment SEALS the guard: deactivate() then refuses,
so in-process code cannot simply switch the invariant off. Activation from
code (what test fixtures do) stays reversible, so teardown still works.
"""
from __future__ import annotations

from .home import env
import ipaddress
import socket
import sys
import threading
import warnings

import _socket

_original_socket_new = socket.socket.__new__
_real_raw_socket = _socket.socket
_INET_FAMILIES = (socket.AF_INET, socket.AF_INET6)

_RESOLVERS = ("getaddrinfo", "gethostbyname", "gethostbyname_ex",
              "gethostbyaddr", "getnameinfo", "getfqdn", "create_connection")
_original_resolvers = {name: getattr(socket, name) for name in _RESOLVERS
                       if hasattr(socket, name)}

_active = False
_sealed = False
_hook_installed = False
_state = threading.local()


class OfflineViolation(RuntimeError):
    pass


def _violation(what: str) -> OfflineViolation:
    return OfflineViolation(
        f"OFFLINE GUARD: {what}. Compartment never touches the network at "
        "runtime; aborting."
    )


# --------------------------------------------------------------- host policy

_LOCAL_NAMES = frozenset({
    "", "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
})


def _is_local_host(host: object) -> bool:
    """True when resolving `host` asks no one anything: it is absent, it is
    localhost, or it is already a literal address."""
    if host is None:
        return True
    if isinstance(host, (bytes, bytearray)):
        try:
            host = bytes(host).decode("ascii")
        except UnicodeDecodeError:
            return False
    if not isinstance(host, str):
        return False
    name = host.strip().rstrip(".").lower()
    if name in _LOCAL_NAMES:
        return True
    try:  # a literal address (with an optional scope id) needs no lookup
        ipaddress.ip_address(name.split("%", 1)[0].strip("[]"))
        return True
    except ValueError:
        return False


def _reject_lookup(host: object) -> None:
    if not _is_local_host(host):
        raise _violation(
            f"something attempted to resolve the host name {host!r}, which "
            "would send a DNS query"
        )


def _reject_inet(family: object) -> None:
    fam = family if family not in (-1, None) else socket.AF_INET
    if fam in _INET_FAMILIES:
        raise _violation(
            "something attempted to create a network socket"
        )


# ------------------------------------------------------------ socket patches

def _guarded_new(cls, family=-1, type=-1, proto=-1, fileno=None):  # noqa: A002
    _reject_inet(family)
    return _original_socket_new(cls, family, type, proto, fileno)


class _RawSocketIdentity(type):
    """So `isinstance(s, _socket.socket)` keeps meaning what it meant while
    the stand-in below is sitting in the module."""

    def __instancecheck__(cls, obj):
        return isinstance(obj, _real_raw_socket)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, _real_raw_socket)


class _GuardedRawSocket(metaclass=_RawSocketIdentity):
    """Stand-in for the C type `_socket.socket` while the guard is active.

    `socket.socket` is a Python subclass of that C type, so `import _socket;
    _socket.socket(AF_INET, SOCK_STREAM)` walks straight past a patch applied
    to the subclass. The C type itself is immutable from Python, so the name
    in the module is replaced instead; instances handed back are still real
    `_socket.socket` objects.
    """

    def __new__(cls, family=-1, type=-1, proto=-1, fileno=None):  # noqa: A002
        _reject_inet(family)
        return _real_raw_socket(family, type, proto, fileno)

    def __init__(self, family=-1, type=-1, proto=-1, fileno=None):  # noqa: A002
        # socket.py builds its subclass with an explicit, unbound
        # `_socket.socket.__init__(self, ...)`, and that lookup happens at
        # call time, so it lands here while the stand-in is in place. `self`
        # is a real socket, and the local families it is allowed to be must
        # keep working.
        _reject_inet(family)
        _real_raw_socket.__init__(self, family, type, proto, fileno)


# ---------------------------------------------------------- resolver patches

def _guarded_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
    _reject_lookup(host)
    return _original_resolvers["getaddrinfo"](host, port, family, type, proto,
                                              flags)


def _guarded_gethostbyname(hostname):
    _reject_lookup(hostname)
    return _original_resolvers["gethostbyname"](hostname)


def _guarded_gethostbyname_ex(hostname):
    _reject_lookup(hostname)
    return _original_resolvers["gethostbyname_ex"](hostname)


def _guarded_gethostbyaddr(ip_address):
    raise _violation(
        f"something attempted a reverse lookup of {ip_address!r}, which would "
        "send a DNS query"
    )


def _guarded_getnameinfo(sockaddr, flags):
    if not flags & socket.NI_NUMERICHOST:
        raise _violation(
            "something attempted to name-resolve an address, which would send "
            "a DNS query (pass NI_NUMERICHOST to format it locally instead)"
        )
    _state.numeric_nameinfo = getattr(_state, "numeric_nameinfo", 0) + 1
    try:
        return _original_resolvers["getnameinfo"](sockaddr, flags)
    finally:
        _state.numeric_nameinfo -= 1


def _guarded_getfqdn(name=""):
    raise _violation(
        "something asked for a fully qualified domain name, which would send "
        "a DNS query"
    )


def _guarded_create_connection(address, *args, **kwargs):
    host = address[0] if isinstance(address, (tuple, list)) and address else address
    raise _violation(
        f"something attempted to open a connection to {host!r}"
    )


_GUARDED_RESOLVERS = {
    "getaddrinfo": _guarded_getaddrinfo,
    "gethostbyname": _guarded_gethostbyname,
    "gethostbyname_ex": _guarded_gethostbyname_ex,
    "gethostbyaddr": _guarded_gethostbyaddr,
    "getnameinfo": _guarded_getnameinfo,
    "getfqdn": _guarded_getfqdn,
    "create_connection": _guarded_create_connection,
}


# ----------------------------------------------------------------- the floor

def _audit(event: str, args: tuple) -> None:
    """The layer nothing in the process can route around.

    CPython raises these events from inside the C implementations, so this
    catches a direct `_socket.socket(...)`, and a `getaddrinfo` reference a
    library captured before the guard was activated, both of which a patched
    name in a module cannot see. An audit hook can never be uninstalled, so it
    checks the flag rather than being added and removed.
    """
    if not _active:
        return
    if event == "socket.__new__":
        _reject_inet(args[1] if len(args) > 1 else -1)
    elif event == "socket.getaddrinfo" or event == "socket.gethostbyname":
        _reject_lookup(args[0] if args else None)
    elif event == "socket.gethostbyaddr":
        _guarded_gethostbyaddr(args[0] if args else None)
    elif event == "socket.getnameinfo":
        if not getattr(_state, "numeric_nameinfo", 0):
            raise _violation(
                "something attempted to name-resolve an address, which would "
                "send a DNS query"
            )


# --------------------------------------------------------------- public API

def activate(*, seal: bool = False) -> None:
    """Turn the guard on. Idempotent: calling it again changes nothing except
    that `seal=True` can still seal an already active guard."""
    global _active, _hook_installed, _sealed
    if seal:
        _sealed = True
    if _active:
        return
    if not _hook_installed:
        sys.addaudithook(_audit)
        _hook_installed = True
    socket.socket.__new__ = _guarded_new
    _socket.socket = _GuardedRawSocket
    for name, fn in _GUARDED_RESOLVERS.items():
        if name in _original_resolvers:
            setattr(socket, name, fn)
    _active = True


def deactivate() -> bool:
    """Turn the guard off, if that is allowed. Returns True if it is now off.

    A guard activated from the environment is sealed and refuses: on a machine
    that asked for the offline invariant, no library, plugin or memory pack
    loaded into this process gets to switch it back off by calling one public
    function. Refusing quietly rather than raising is deliberate, because this
    is called from test teardown that must not blow up.

    A guard activated from code (what the test fixtures do) stays reversible,
    so teardown still leaves the process clean.
    """
    global _active
    if _sealed:
        if _active:
            warnings.warn(
                "offline guard was activated from the environment and is "
                "sealed; refusing to deactivate it",
                RuntimeWarning, stacklevel=2)
        return not _active
    socket.socket.__new__ = _original_socket_new
    _socket.socket = _real_raw_socket
    for name, fn in _original_resolvers.items():
        setattr(socket, name, fn)
    _active = False
    return True


def is_active() -> bool:
    return _active


def is_sealed() -> bool:
    """True when the guard was activated from the environment and so cannot be
    deactivated by anything in this process."""
    return _sealed


def activate_from_env() -> None:
    if env("ASSERT_OFFLINE") == "1":
        activate(seal=True)
