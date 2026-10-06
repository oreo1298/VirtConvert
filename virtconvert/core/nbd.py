"""A small NBD server on a Unix socket.

qemu-img can read from an NBD server like from a file. VirtConvert&Create uses this
to give qemu-img a merged view of a differencing chain (see chain.py) or a physical
disk opened by the root helper (devices.py): qemu-img reads the disk through this
server and writes the target format itself, so no temporary copy is needed.
Writing a virtual disk onto a physical one goes the other way: the server is
writable and qemu-img writes the disk's contents into it (writedisk.py).

Implements the fixed newstyle handshake (NBD_OPT_GO / NBD_OPT_INFO /
NBD_OPT_EXPORT_NAME) and NBD_CMD_READ / NBD_CMD_DISC with simple replies; a writable
export adds NBD_CMD_WRITE, NBD_CMD_WRITE_ZEROES and NBD_CMD_FLUSH. Other options are
refused with NBD_REP_ERR_UNSUP, which clients handle.
"""

from __future__ import annotations

import errno
import os
import shutil
import socket
import struct
import tempfile
import threading
from typing import Protocol

NBDMAGIC = 0x4E42444D41474943
IHAVEOPT = 0x49484156454F5054
REPLY_MAGIC = 0x3E889045565A9
REQUEST_MAGIC = 0x25609513
SIMPLE_REPLY_MAGIC = 0x67446698

FLAG_FIXED_NEWSTYLE = 1
FLAG_NO_ZEROES = 2
FLAG_HAS_FLAGS = 1
FLAG_READ_ONLY = 2
FLAG_SEND_FLUSH = 1 << 2
FLAG_SEND_WRITE_ZEROES = 1 << 6
FLAG_CAN_MULTI_CONN = 1 << 8

OPT_EXPORT_NAME, OPT_ABORT, OPT_LIST, OPT_INFO, OPT_GO = 1, 2, 3, 6, 7
REP_ACK, REP_SERVER, REP_INFO = 1, 2, 3
REP_ERR_UNSUP = (1 << 31) + 1
REP_ERR_INVALID = (1 << 31) + 3
REP_ERR_UNKNOWN = (1 << 31) + 6
INFO_EXPORT, INFO_BLOCK_SIZE = 0, 3

CMD_READ, CMD_WRITE, CMD_DISC, CMD_FLUSH, CMD_CACHE, CMD_WRITE_ZEROES = 0, 1, 2, 3, 5, 6
MAX_REQUEST = 32 << 20
PREFERRED_BLOCK = 2 << 20


class Source(Protocol):
    size: int

    def read(self, offset: int, length: int) -> bytes: ...


class Target(Source, Protocol):
    """What a writable export needs."""

    def write(self, offset: int, data: bytes) -> None: ...

    def zero(self, offset: int, length: int) -> None: ...

    def flush(self) -> None: ...


def _socket_parent() -> str:
    """A folder short enough for a Unix socket path (at most 107 bytes on Linux)."""
    for candidate in (os.environ.get("XDG_RUNTIME_DIR"), tempfile.gettempdir(), "/tmp", "/dev/shm"):
        if candidate and len(candidate) < 70 and os.path.isdir(candidate) and os.access(candidate, os.W_OK):
            return candidate
    return tempfile.gettempdir()


def _recv(sock: socket.socket, n: int) -> bytes:
    data = bytearray()
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("client closed the connection")
        data += chunk
    return bytes(data)


class NbdServer:
    """Serves SOURCE, read-only unless WRITABLE (then SOURCE is a Target). Use as a context
    manager; `url` is for qemu-img."""

    def __init__(self, source: Source, export: str = "virtconvert", writable: bool = False):
        self.source = source
        self.export = export
        self.writable = writable
        self.error: BaseException | None = None
        self._dir = tempfile.mkdtemp(prefix="vc-", dir=_socket_parent())
        self.socket_path = os.path.join(self._dir, "nbd.sock")
        self._sock: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._clients: list[socket.socket] = []
        self._stopping = threading.Event()

    @property
    def url(self) -> str:
        return f"nbd+unix:///{self.export}?socket={self.socket_path}"

    def start(self) -> "NbdServer":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(self.socket_path)
            os.chmod(self.socket_path, 0o600)
            sock.listen(4)
        except OSError:
            sock.close()
            shutil.rmtree(self._dir, ignore_errors=True)
            raise
        self._sock = sock
        t = threading.Thread(target=self._accept_loop, name="nbd-accept", daemon=True)
        t.start()
        self._threads.append(t)
        return self

    def stop(self) -> None:
        self._stopping.set()
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._sock.close()
            self._sock = None
        for client in list(self._clients):
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for t in self._threads:
            t.join(timeout=5)
        shutil.rmtree(self._dir, ignore_errors=True)

    def __enter__(self) -> "NbdServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # ------------------------------------------------------------------ connections

    def _accept_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                client, _ = self._sock.accept()
            except OSError:
                return
            self._clients.append(client)
            t = threading.Thread(target=self._serve, args=(client,), name="nbd-client", daemon=True)
            t.start()
            self._threads.append(t)

    def _serve(self, sock: socket.socket) -> None:
        try:
            if self._handshake(sock):
                self._transmission(sock)
        except (ConnectionError, OSError):
            pass
        except Exception as exc:  # noqa: BLE001 - reported to whoever runs qemu-img
            self.error = exc
        finally:
            try:
                sock.close()
            except OSError:
                pass
            if sock in self._clients:
                self._clients.remove(sock)

    def _transmission_flags(self) -> int:
        if self.writable:
            return FLAG_HAS_FLAGS | FLAG_SEND_FLUSH | FLAG_SEND_WRITE_ZEROES
        return FLAG_HAS_FLAGS | FLAG_READ_ONLY | FLAG_CAN_MULTI_CONN

    def _handshake(self, sock: socket.socket) -> bool:
        sock.sendall(struct.pack(">QQH", NBDMAGIC, IHAVEOPT, FLAG_FIXED_NEWSTYLE | FLAG_NO_ZEROES))
        client_flags, = struct.unpack(">I", _recv(sock, 4))
        no_zeroes = bool(client_flags & FLAG_NO_ZEROES)
        while True:
            magic, option, length = struct.unpack(">QII", _recv(sock, 16))
            if magic != IHAVEOPT:
                return False
            data = _recv(sock, length) if length else b""
            if option == OPT_EXPORT_NAME:
                sock.sendall(struct.pack(">QH", self.source.size, self._transmission_flags())
                             + (b"" if no_zeroes else bytes(124)))
                return True
            if option == OPT_ABORT:
                self._reply(sock, option, REP_ACK)
                return False
            if option == OPT_LIST:
                name = self.export.encode()
                self._reply(sock, option, REP_SERVER, struct.pack(">I", len(name)) + name)
                self._reply(sock, option, REP_ACK)
            elif option in (OPT_INFO, OPT_GO):
                if len(data) < 6:
                    self._reply(sock, option, REP_ERR_INVALID)
                    continue
                self._reply(sock, option, REP_INFO,
                            struct.pack(">HQH", INFO_EXPORT, self.source.size, self._transmission_flags()))
                self._reply(sock, option, REP_INFO,
                            struct.pack(">HIII", INFO_BLOCK_SIZE, 1, PREFERRED_BLOCK, MAX_REQUEST))
                self._reply(sock, option, REP_ACK)
                if option == OPT_GO:
                    return True
            else:
                self._reply(sock, option, REP_ERR_UNSUP)

    @staticmethod
    def _reply(sock: socket.socket, option: int, kind: int, data: bytes = b"") -> None:
        sock.sendall(struct.pack(">QIII", REPLY_MAGIC, option, kind, len(data)) + data)

    def _transmission(self, sock: socket.socket) -> None:
        size = self.source.size
        while not self._stopping.is_set():
            magic, _flags, cmd, cookie, offset, length = struct.unpack(">IHHQQI", _recv(sock, 28))
            if magic != REQUEST_MAGIC:
                return
            if cmd == CMD_DISC:
                return
            if cmd == CMD_READ:
                if offset + length > size or length > MAX_REQUEST:
                    sock.sendall(struct.pack(">IIQ", SIMPLE_REPLY_MAGIC, errno.EINVAL, cookie))
                    continue
                try:
                    data = self.source.read(offset, length)
                except Exception as exc:  # noqa: BLE001 - the client gets EIO, we keep the reason
                    self.error = exc
                    sock.sendall(struct.pack(">IIQ", SIMPLE_REPLY_MAGIC, errno.EIO, cookie))
                    continue
                sock.sendall(struct.pack(">IIQ", SIMPLE_REPLY_MAGIC, 0, cookie) + data)
            elif cmd == CMD_WRITE:
                if length > MAX_REQUEST:
                    _discard(sock, length)
                    self._simple(sock, cookie, errno.EINVAL)
                    continue
                data = _recv(sock, length)
                if not self.writable:
                    self._simple(sock, cookie, errno.EPERM)
                elif offset + length > size:
                    self._simple(sock, cookie, errno.ENOSPC)
                else:
                    self._simple(sock, cookie, self._call(self.source.write, offset, data))
            elif cmd == CMD_WRITE_ZEROES:
                if not self.writable:
                    self._simple(sock, cookie, errno.EPERM)
                elif offset + length > size:
                    self._simple(sock, cookie, errno.ENOSPC)
                else:
                    self._simple(sock, cookie, self._call(self.source.zero, offset, length))
            elif cmd == CMD_FLUSH and self.writable:
                self._simple(sock, cookie, self._call(self.source.flush))
            elif cmd in (CMD_FLUSH, CMD_CACHE):
                self._simple(sock, cookie, 0)
            else:
                self._simple(sock, cookie, errno.EINVAL)

    @staticmethod
    def _simple(sock: socket.socket, cookie: int, error: int) -> None:
        sock.sendall(struct.pack(">IIQ", SIMPLE_REPLY_MAGIC, error, cookie))

    def _call(self, method, *args) -> int:
        """Run a write, zero or flush of the target: 0, or the error for the client (the reason is
        kept for whoever runs qemu-img)."""
        try:
            method(*args)
            return 0
        except Exception as exc:  # noqa: BLE001
            self.error = exc
            return errno.EIO


def _discard(sock: socket.socket, n: int) -> None:
    while n > 0:
        n -= len(_recv(sock, min(n, 1 << 20)))
