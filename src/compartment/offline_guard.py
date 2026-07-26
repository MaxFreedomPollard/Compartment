"""Runtime offline enforcement (invariant I1).

When activated (COMPARTMENT_ASSERT_OFFLINE=1 or --assert-offline), creating any
INET/INET6 socket raises OfflineViolation and the process aborts loudly.
stdio (the MCP transport) needs no sockets, so normal operation is
unaffected. The ONLY code allowed to bypass this is nothing: even
`setup download-model` refuses to run while the guard is active.
"""
from __future__ import annotations

from .home import env, home
import os
import socket

_original_socket_new = socket.socket.__new__
_active = False


class OfflineViolation(RuntimeError):
    pass


def _guarded_new(cls, family=-1, type=-1, proto=-1, fileno=None):  # noqa: A002
    fam = family if family != -1 else socket.AF_INET
    if fam in (socket.AF_INET, socket.AF_INET6):
        raise OfflineViolation(
            "OFFLINE GUARD: something attempted to create a network socket. "
            "Compartment never touches the network at runtime; aborting."
        )
    return _original_socket_new(cls, family, type, proto, fileno)


def activate() -> None:
    global _active
    if _active:
        return
    socket.socket.__new__ = _guarded_new
    _active = True


def deactivate() -> None:
    global _active
    socket.socket.__new__ = _original_socket_new
    _active = False


def is_active() -> bool:
    return _active


def activate_from_env() -> None:
    if env("ASSERT_OFFLINE") == "1":
        activate()
