"""Which user is on the other end of a connection (Linux; ADR-029).

``LocalPageGuard`` refuses requests from other users of the same machine. On
Linux the kernel can say who that is: both ends of a loopback connection live
on this machine, and its ``sock_diag`` netlink interface — the one ``ss`` uses
— looks up the client's socket by the connection's addresses and reports the
uid that owns it. A lookup takes about 20 µs however many connections the
machine holds, so every request is looked up and nothing is cached: a cache
could only be keyed by the client's address and port, and another user can
take a port over the moment it is freed.

``owner()`` answers with the client's uid, or with one of two words:

* ``"gone"`` — the client hung up before anyone looked. Its request would
  still run, and sending a write and leaving at once is exactly how another
  user would get one past the guard with nothing left to identify. Nobody is
  waiting for the answer, so refusing it costs the server's own user nothing.
* ``"unknown"`` — nobody can tell: there is no netlink here (Windows, macOS),
  the kernel refused the question, or the address is not one a real socket
  has (the test client's ``"testclient"``). Let through, as before.

``allowed()`` lets in the server's own user, root and ``"unknown"``.
"""

from __future__ import annotations

import errno
import ipaddress
import os
import socket
import struct
from collections.abc import Callable
from typing import Literal, NamedTuple

GONE: Literal["gone"] = "gone"
UNKNOWN: Literal["unknown"] = "unknown"

Owner = int | Literal["gone", "unknown"]
Address = tuple[str, int]


class Socket(NamedTuple):
    """One TCP socket, as ``sock_diag`` reports it."""

    state: int  # include/net/tcp_states.h
    uid: int
    inode: int


# ``lookup(local, remote)``: the TCP socket whose own end is ``local`` and whose
# other end is ``remote``; ``None`` when there is none; ``OSError`` when it
# cannot be told.
Lookup = Callable[[Address, Address], Socket | None]

TCP_ESTABLISHED = 1

# include/uapi/linux/{netlink,sock_diag,inet_diag}.h
_NETLINK_SOCK_DIAG = 4
_SOCK_DIAG_BY_FAMILY = 20
_NLM_F_REQUEST = 1
_NLMSG_ERROR = 2
_ALL_STATES = 0xFFFFFFFF
_NO_COOKIE = 0xFFFFFFFF  # match the socket by its addresses alone
_HEADER = struct.Struct("=IHHII")  # nlmsghdr: length, type, flags, seq, pid
_MESSAGE_SIZE = 72  # inet_diag_msg
_STATE_AT = _HEADER.size + 1
_PORTS_AT = _HEADER.size + 4  # idiag_sport, idiag_dport, network order
_UID_INODE_AT = _HEADER.size + 4 + 48 + 12  # idiag_uid, idiag_inode

# The longest one lookup may take. It runs synchronously inside the async
# guard, so this is also how long it can stall the whole server in the worst
# case (normally ~20 µs) — do not raise it.
_TIMEOUT_S = 0.5

# The server's own user; ``None`` where the platform has no uids (Windows).
_SERVER_UID: int | None = os.geteuid() if hasattr(os, "geteuid") else None


def _is_ipv4(host: str) -> bool:
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        return False
    return True


def request(local: Address, remote: Address) -> bytes:
    """A ``SOCK_DIAG_BY_FAMILY`` request for exactly one IPv4 TCP socket."""
    sockid = struct.pack(
        "!HH16s16s",
        local[1],
        remote[1],
        ipaddress.IPv4Address(local[0]).packed,
        ipaddress.IPv4Address(remote[0]).packed,
    ) + struct.pack("=III", 0, _NO_COOKIE, _NO_COOKIE)
    body = struct.pack("=BBBBI", socket.AF_INET, socket.IPPROTO_TCP, 0, 0, _ALL_STATES) + sockid
    header = _HEADER.pack(_HEADER.size + len(body), _SOCK_DIAG_BY_FAMILY, _NLM_F_REQUEST, 1, 0)
    return header + body


def parse_reply(reply: bytes, local: Address, remote: Address) -> Socket | None:
    """The socket in the kernel's answer to ``request(local, remote)``."""
    if len(reply) < _HEADER.size:
        raise OSError("sock_diag answered with a truncated message")
    kind = _HEADER.unpack_from(reply)[1]
    if kind == _NLMSG_ERROR:
        if len(reply) < _HEADER.size + 4:
            raise OSError("sock_diag answered with a truncated error")
        (error,) = struct.unpack_from("=i", reply, _HEADER.size)
        if -error == errno.ENOENT:  # no such socket
            return None
        raise OSError(-error, os.strerror(-error))
    if kind != _SOCK_DIAG_BY_FAMILY or len(reply) < _HEADER.size + _MESSAGE_SIZE:
        raise OSError("sock_diag answered with something else")
    # With no connected socket on these addresses the kernel falls back to one
    # listening on the local port: that is not the socket asked for. The family
    # is not checked: a dual-stack client of ``::ffff:127.0.0.1`` is found by
    # this IPv4 lookup and answers as AF_INET6, its fields where IPv4's are.
    if struct.unpack_from("!HH", reply, _PORTS_AT) != (local[1], remote[1]):
        return None
    uid, inode = struct.unpack_from("=II", reply, _UID_INODE_AT)
    return Socket(state=reply[_STATE_AT], uid=uid, inode=inode)


def sock_diag(local: Address, remote: Address) -> Socket | None:
    """``Lookup`` through the kernel; ``OSError`` wherever netlink is missing."""
    family = getattr(socket, "AF_NETLINK", None)
    if family is None:
        raise OSError("this platform has no netlink")
    with socket.socket(family, socket.SOCK_RAW, _NETLINK_SOCK_DIAG) as netlink:
        netlink.settimeout(_TIMEOUT_S)
        netlink.send(request(local, remote))
        return parse_reply(netlink.recv(8192), local, remote)


def owner(client: Address | None, server: Address | None, *, lookup: Lookup = sock_diag) -> Owner:
    """Who sent a request: the uid owning the client's socket, ``"gone"`` or
    ``"unknown"`` (module docstring). ``client`` and ``server`` are the ASGI
    scope's."""
    if client is None:
        # uvicorn could not read the address: the client reset the connection
        # before the server accepted it.
        return GONE
    if server is None or not (_is_ipv4(client[0]) and _is_ipv4(server[0])):
        return UNKNOWN
    try:
        peer = lookup(client, server)
        if peer is not None:
            # A closed client socket lingers as an orphan (or in TIME_WAIT)
            # with inode 0 — on 4.18, 6.12 and mainline alike. Its uid is no
            # sign: 0 (root) on 4.18 and 6.12, the real owner's on mainline.
            # So judge by the inode; a check on the uid alone lets the
            # hang-up through as root.
            return GONE if peer.inode == 0 else peer.uid
        # No client socket at all. If the server's end is still connected, the
        # client lives where this kernel cannot see it (another network
        # namespace, as with WSL2's mirrored networking); if not, it is gone.
        own = lookup(server, client)
    except OSError:
        return UNKNOWN
    return UNKNOWN if own is not None and own.state == TCP_ESTABLISHED else GONE


def allowed(who: Owner, *, me: int | None = _SERVER_UID) -> bool:
    """Whether the guard lets ``who`` in: the server's own user, root, or a
    peer nobody can tell."""
    return who in (UNKNOWN, 0, me)
