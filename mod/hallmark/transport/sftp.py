"""Read-only SFTP v3 metadata over an operation-owned OpenSSH subsystem.

OpenSSH owns authentication and encryption. This module only frames the small
metadata protocol, so names and attributes never pass through shell/ls parsing.
"""

from __future__ import annotations

import os
import selectors
import struct
import subprocess
import time

from .base import CapabilityError, DownloadError, RemoteObjectMissing


_MAX_PACKET = 1024 * 1024
_KNOWN_ATTRIBUTES = 0x8000000F


def _string(value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return struct.pack(">I", len(value)) + value


class _Packet:
    def __init__(self, data):
        self.data = data
        self.offset = 0

    def take(self, size):
        end = self.offset + size
        if end > len(self.data):
            raise DownloadError("Truncated SFTP metadata packet")
        result = self.data[self.offset:end]
        self.offset = end
        return result

    def uint32(self):
        return struct.unpack(">I", self.take(4))[0]

    def string(self):
        return self.take(self.uint32())

    def text(self):
        try:
            return self.string().decode("utf-8")
        except UnicodeError:
            raise DownloadError("SFTP filename must contain valid UTF-8") from None

    def finish(self):
        if self.offset != len(self.data):
            raise DownloadError("Unexpected trailing SFTP metadata")

    def attributes(self):
        flags = self.uint32()
        if flags & ~_KNOWN_ATTRIBUTES:
            raise DownloadError("Unsupported SFTP attribute flags")
        size = struct.unpack(">Q", self.take(8))[0] if flags & 1 else None
        if flags & 2:
            self.take(8)  # uid and gid
        mode = self.uint32() if flags & 4 else None
        mtime = None
        if flags & 8:
            self.uint32()  # atime
            mtime = self.uint32()
        if flags & 0x80000000:
            count = self.uint32()
            if count > (len(self.data) - self.offset) // 8:
                raise DownloadError("Invalid SFTP attribute count")
            for _ in range(count):
                self.string()
                self.string()
        return {"size": size, "mode": mode, "mtime": mtime}


class SftpMetadata:
    """One bounded, cancellable request at a time on a binary subsystem pipe."""

    def __init__(self, transport):
        self.transport = transport
        self.context = transport.context
        self.process = None
        self.selector = None
        self.buffer = bytearray()
        self.stderr_size = 0
        self.request_id = 0
        self.deadline = None
        self._slot = False

    def __enter__(self):
        self.transport.prepare()
        while not self.transport._slots.acquire(timeout=0.05):
            self.context.check_cancelled()
        self._slot = True
        try:
            self.process = self.transport._spawn(
                self.transport.ssh + self.transport._options()
                + ["-T", "-s", "--", self.context.remote.host, "sftp"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, bufsize=0,
            )
            self.selector = selectors.DefaultSelector()
            for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
                os.set_blocking(pipe.fileno(), False)
            self.selector.register(self.process.stdout, selectors.EVENT_READ)
            self.selector.register(self.process.stderr, selectors.EVENT_READ)
            self._new_deadline()
            self._send(b"\x01" + struct.pack(">I", 3))
            kind, packet = self._receive()
            if kind != 2 or packet.uint32() != 3:
                raise CapabilityError("Server must support SFTP version 3")
            while packet.offset < len(packet.data):
                packet.string()
                packet.string()  # Ignore unneeded negotiated extensions.
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.process is not None:
                self.transport._stop(self.process)
        finally:
            if self.selector is not None:
                self.selector.close()
            if self.process is not None:
                for pipe in (
                    self.process.stdin, self.process.stdout, self.process.stderr,
                ):
                    pipe.close()
            if self._slot:
                self.transport._slots.release()
                self._slot = False

    def _new_deadline(self):
        self.deadline = time.monotonic() + self.context.listing_timeout

    def _check(self):
        self.context.check_cancelled()
        if time.monotonic() >= self.deadline:
            raise DownloadError("SFTP metadata request exceeded its time limit")

    def _pump(self):
        self._check()
        writable = False
        for key, events in self.selector.select(timeout=0.05):
            if events & selectors.EVENT_WRITE:
                writable = True
                continue
            try:
                chunk = os.read(key.fileobj.fileno(), 65536)
            except BlockingIOError:
                continue
            if not chunk:
                self.selector.unregister(key.fileobj)
            elif key.fileobj is self.process.stdout:
                self.buffer.extend(chunk)
                if len(self.buffer) > _MAX_PACKET + 4:
                    raise DownloadError("SFTP metadata packet exceeds its size limit")
            else:
                self.stderr_size += len(chunk)
                if self.stderr_size > 65536:
                    raise DownloadError("SSH diagnostics exceeded their size limit")
        return writable

    def _send(self, payload):
        if len(payload) > _MAX_PACKET:
            raise DownloadError("SFTP metadata request exceeds its size limit")
        data = memoryview(struct.pack(">I", len(payload)) + payload)
        self.selector.register(self.process.stdin, selectors.EVENT_WRITE)
        try:
            while data:
                if self._pump():
                    try:
                        written = os.write(self.process.stdin.fileno(), data)
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        self.context.check_cancelled()
                        raise DownloadError("SFTP subsystem disconnected") from None
                    data = data[written:]
        finally:
            self.selector.unregister(self.process.stdin)

    def _read(self, size):
        while len(self.buffer) < size:
            self._pump()
            self.context.check_cancelled()
            if (len(self.buffer) < size
                    and self.process.stdout.fileno() not in self.selector.get_map()):
                raise DownloadError("SFTP subsystem returned truncated metadata")
        self._check()
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def _receive(self):
        length = struct.unpack(">I", self._read(4))[0]
        if not 1 <= length <= _MAX_PACKET:
            raise DownloadError("Invalid or oversized SFTP metadata packet")
        data = self._read(length)
        return data[0], _Packet(data[1:])

    def _request(self, kind, data, expected, *, eof=False):
        self._new_deadline()
        self.request_id = (self.request_id + 1) % (2 ** 32)
        self._send(bytes([kind]) + struct.pack(">I", self.request_id) + data)
        response, packet = self._receive()
        if packet.uint32() != self.request_id:
            raise DownloadError("Mismatched SFTP response identifier")
        if response == 101:
            status = packet.uint32()
            packet.string()  # Untrusted server diagnostics are never echoed.
            packet.string()
            packet.finish()
            if status == 1 and eof:
                return None
            if status == 0 and expected == 101:
                return packet
            if status == 2:
                raise RemoteObjectMissing("Remote metadata object does not exist")
            if status == 8:
                raise CapabilityError("Server does not support SFTP metadata access")
            raise DownloadError(f"SFTP metadata operation failed (status {status})")
        if response != expected:
            raise DownloadError("Unexpected SFTP metadata response")
        return packet

    def realpath(self, path):
        packet = self._request(16, _string(path), 104)
        if packet.uint32() != 1:
            raise DownloadError("Invalid SFTP canonical path response")
        result = packet.text()
        packet.string()
        packet.attributes()
        packet.finish()
        return result

    def lstat(self, path):
        packet = self._request(7, _string(path), 105)
        result = packet.attributes()
        packet.finish()
        return result

    def iterdir(self, path):
        packet = self._request(11, _string(path), 102)
        handle = packet.string()
        packet.finish()
        if not handle or len(handle) > 256:
            raise DownloadError("Invalid SFTP directory handle")
        completed = False
        try:
            while True:
                packet = self._request(12, _string(handle), 104, eof=True)
                if packet is None:
                    completed = True
                    return
                count = packet.uint32()
                if count == 0 or count > (len(packet.data) - packet.offset) // 12:
                    raise DownloadError("Invalid SFTP directory entry count")
                entries = []
                for _ in range(count):
                    name = packet.text()
                    packet.string()  # Human-readable longname is not parsed.
                    entries.append((name, packet.attributes()))
                packet.finish()
                yield from entries
        finally:
            if not self.context.cancelled.is_set():
                try:
                    self._request(4, _string(handle), 101)
                except DownloadError:
                    if completed:
                        raise
