"""Which user is on the other end of a connection (Linux; ADR-029).

``LocalPageGuard`` refuses requests from other users of the same machine. On
Linux the kernel can say who that is: both ends of a loopback connection live
on this machine, and its ``sock_diag`` netlink interface — the one ``ss`` uses
— looks up the client's socket by the connection's addresses and reports the
uid that owns it. A lookup takes about 20 µs however many connections the
machine holds, so every request is looked up and nothing is cached: a cache
could only be keyed by the client's address and port, and another user can
take a port over the moment it is freed.

Whether this machine can be asked is settled once, before the server opens
its port (``sock_diag``): the kernel must find a connection this process makes
to itself, as this process's. Only the kernel's own answer settles it. A test
that could not be made — another user can hold every free port for a moment —
raises instead, and the server does not start: read as "nobody can tell", it
would let everyone in for the server's life. The netlink socket that found it
is then kept for the server's life, so a lookup needs no new file descriptor.

``owner()`` answers with the client's uid, or with one of three words:

* ``"gone"`` — the client hung up before anyone looked. Its request would
  still run, and sending a write and leaving at once is exactly how another
  user would get one past the guard with nothing left to identify. Nobody is
  waiting for the answer, so refusing it costs the server's own user nothing.
* ``"failed"`` — this machine can tell, but the question failed this time.
  Refused: a client able to make it fail must not get in by it, and using up
  the server's file descriptors once did exactly that.
* ``"unknown"`` — nobody can tell: there is no netlink here (Windows, macOS),
  the kernel cannot answer for TCP (no ``tcp_diag``), this process's uid has
  no mapping in its user namespace (every user then reads as the same
  overflow uid), or the address is not one a real socket has (the test
  client's ``"testclient"``). Let through, as before.

``allowed()`` lets in the server's own user, root and ``"unknown"``.
"""

from __future__ import annotations

import errno
import ipaddress
import os
import socket
import struct
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Literal, NamedTuple

GONE: Literal["gone"] = "gone"
FAILED: Literal["failed"] = "failed"
UNKNOWN: Literal["unknown"] = "unknown"

Owner = int | Literal["gone", "failed", "unknown"]
Address = tuple[str, int]


class Socket(NamedTuple):
    """One TCP socket, as ``sock_diag`` reports it."""

    state: int  # include/net/tcp_states.h
    uid: int
    inode: int


# ``lookup(local, remote)``: the TCP socket whose own end is ``local`` and whose
# other end is ``remote``; ``None`` when there is none; ``CannotTell`` where
# nobody can; ``OSError`` when the question failed.
Lookup = Callable[[Address, Address], Socket | None]


class CannotTell(Exception):
    """Raised by a lookup on a machine that cannot say who owns a socket."""

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

# A netlink socket that failed to open for one of these may open next time:
# not "nobody can tell".
_OUT_OF_RESOURCES = frozenset({errno.EMFILE, errno.ENFILE, errno.ENOBUFS, errno.ENOMEM})


def _is_ipv4(host: str) -> bool:
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        return False
    return True


def request(local: Address, remote: Address, *, device: int = 0, seq: int = 1) -> bytes:
    """A ``SOCK_DIAG_BY_FAMILY`` request for exactly one IPv4 TCP socket: one
    bound to no device, or to the one with index ``device``."""
    sockid = struct.pack(
        "!HH16s16s",
        local[1],
        remote[1],
        ipaddress.IPv4Address(local[0]).packed,
        ipaddress.IPv4Address(remote[0]).packed,
    ) + struct.pack("=III", device, _NO_COOKIE, _NO_COOKIE)
    body = struct.pack("=BBBBI", socket.AF_INET, socket.IPPROTO_TCP, 0, 0, _ALL_STATES) + sockid
    header = _HEADER.pack(_HEADER.size + len(body), _SOCK_DIAG_BY_FAMILY, _NLM_F_REQUEST, seq, 0)
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


class SockDiag:
    """The kernel's lookup, over one netlink socket kept for the server's life.

    Each lookup used to open a socket of its own, and a client that had used
    up the server's file descriptors made every one of them fail.
    """

    def __init__(self, netlink: socket.socket) -> None:
        self._netlink = netlink
        self._lock = threading.Lock()
        self._seq = 0

    def lookup(self, local: Address, remote: Address) -> Socket | None:
        """``Lookup``. The kernel finds a socket bound to a device
        (``SO_BINDTODEVICE``, open to any user since Linux 5.7) only when asked
        with that device's index, so a miss is asked again of every device."""
        found = self._ask(local, remote, 0)
        if found is None:
            for device, _name in socket.if_nameindex():
                found = self._ask(local, remote, device)
                if found is not None:
                    break
        return found

    def _ask(self, local: Address, remote: Address, device: int) -> Socket | None:
        with self._lock:
            self._seq = self._seq % 0xFFFFFFFF + 1
            self._netlink.send(request(local, remote, device=device, seq=self._seq))
            while True:
                reply, (sender, _groups) = self._netlink.recvfrom(8192)
                # Skip what the kernel did not send, and a late answer to a
                # question that timed out before.
                if (
                    sender == 0
                    and len(reply) >= _HEADER.size
                    and _HEADER.unpack_from(reply)[3] == self._seq
                ):
                    return parse_reply(reply, local, remote)


def answers_for_tcp(diag: SockDiag) -> bool:
    """Whether ``diag`` finds a connection this process makes to itself, as
    this process's. Without ``tcp_diag`` the kernel finds no TCP socket at all,
    and every client would read as gone — the server's own user included.

    ``OSError`` when the test could not be made: no free port, or no answer in
    time. An error the kernel answers with is its answer. The connection is
    looked up by its own end's address, not by the one ``accept`` hands back:
    another user may connect to the test's port first."""
    with socket.create_server(("127.0.0.1", 0)) as listener:
        server = listener.getsockname()
        with socket.create_connection(server, timeout=_TIMEOUT_S) as client_end:
            inode = os.fstat(client_end.fileno()).st_ino
            mine = Socket(TCP_ESTABLISHED, os.geteuid(), inode)
            try:
                found = diag.lookup(client_end.getsockname(), server)
            except TimeoutError:
                raise
            except OSError:
                return False
            return found == mine


def uid_mapped(uid_map: str, uid: int) -> bool:
    """Whether ``uid`` has a mapping in ``uid_map`` (``/proc/self/uid_map``'s
    text). In a user namespace without one, every user's socket reads as the
    overflow uid — this process's own too — so uids tell nobody apart."""
    for line in uid_map.splitlines():
        fields = line.split()
        if len(fields) == 3 and int(fields[0]) <= uid < int(fields[0]) + int(fields[2]):
            return True
    return False


def open_sock_diag() -> SockDiag | None:
    """The kernel's lookup, or ``None`` where it cannot tell users apart;
    ``OSError`` when that could not be found out this time."""
    family = getattr(socket, "AF_NETLINK", None)
    if family is None:
        return None
    try:
        uid_map = Path("/proc/self/uid_map").read_text(encoding="ascii")
    except OSError:
        uid_map = None
    if uid_map is not None and not uid_mapped(uid_map, os.geteuid()):
        return None
    try:
        netlink = socket.socket(family, socket.SOCK_RAW, _NETLINK_SOCK_DIAG)
    except OSError as error:
        if error.errno in _OUT_OF_RESOURCES:
            raise
        return None
    netlink.settimeout(_TIMEOUT_S)
    diag = SockDiag(netlink)
    try:
        if answers_for_tcp(diag):
            return diag
    except OSError:
        netlink.close()
        raise
    netlink.close()
    return None


_opened = False
_diag: SockDiag | None = None
_open_lock = threading.Lock()


def sock_diag() -> SockDiag | None:
    """This process's ``open_sock_diag()``, opened on first use and asked again
    after an ``OSError``. The server asks before it opens its port: no client
    can be using up its file descriptors yet."""
    global _opened, _diag
    with _open_lock:
        if not _opened:
            _diag = open_sock_diag()
            _opened = True
        return _diag


def kernel_lookup(local: Address, remote: Address) -> Socket | None:
    """``Lookup`` through ``sock_diag()``."""
    diag = sock_diag()
    if diag is None:
        raise CannotTell
    return diag.lookup(local, remote)


def owner(
    client: Address | None, server: Address | None, *, lookup: Lookup = kernel_lookup
) -> Owner:
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
    except CannotTell:
        return UNKNOWN
    except OSError:
        return FAILED
    return UNKNOWN if own is not None and own.state == TCP_ESTABLISHED else GONE


def allowed(who: Owner, *, me: int | None = _SERVER_UID) -> bool:
    """Whether the guard lets ``who`` in: the server's own user, root, or a
    peer nobody can tell."""
    return who in (UNKNOWN, 0, me)
