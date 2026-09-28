"""Read SFTP v3 metadata through an OpenSSH subsystem.

OpenSSH handles authentication and encryption. Names and attributes are read
from binary protocol fields without parsing shell commands or ``ls`` output.
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


def _encode_sftp_string(value):
    """Encode a byte string or UTF-8 text as an SFTP string."""
    if isinstance(value, str):
        value = value.encode("utf-8")
    return struct.pack(">I", len(value)) + value


class _Packet:
    """Read and validate fields in an SFTP response packet."""
    def __init__(self, data):
        self.data = data
        self.offset = 0

    def read_bytes(self, size):
        """Read the requested bytes, rejecting truncated packets."""
        end = self.offset + size
        if end > len(self.data):
            raise DownloadError("Truncated SFTP metadata packet")
        result = self.data[self.offset:end]
        self.offset = end
        return result

    def read_uint32(self):
        """Read an unsigned 32-bit integer in network byte order."""
        return struct.unpack(">I", self.read_bytes(4))[0]

    def read_string(self):
        """Read a length-prefixed byte string."""
        return self.read_bytes(self.read_uint32())

    def read_filename(self):
        """Read a UTF-8 filename from a length-prefixed string."""
        try:
            return self.read_string().decode("utf-8")
        except UnicodeError:
            raise DownloadError("SFTP filename must contain valid UTF-8") from None

    def check_packet_end(self):
        """Reject unconsumed bytes after the expected fields."""
        if self.offset != len(self.data):
            raise DownloadError("Unexpected trailing SFTP metadata")

    def read_attributes(self):
        """Read size, mode, and modification time; retain unknowns as None."""
        flags = self.read_uint32()
        if flags & ~_KNOWN_ATTRIBUTES:
            raise DownloadError("Unsupported SFTP attribute flags")
        size = struct.unpack(">Q", self.read_bytes(8))[0] if flags & 1 else None
        if flags & 2:
            self.read_bytes(8)  # uid and gid
        mode = self.read_uint32() if flags & 4 else None
        mtime = None
        if flags & 8:
            self.read_uint32()  # atime
            mtime = self.read_uint32()
        if flags & 0x80000000:
            count = self.read_uint32()
            if count > (len(self.data) - self.offset) // 8:
                raise DownloadError("Invalid SFTP attribute count")
            for _ in range(count):
                self.read_string()
                self.read_string()
        return {"size": size, "mode": mode, "mtime": mtime}


class SftpMetadata:
    """
    Read SFTP metadata through a dedicated OpenSSH subsystem process.

    Requests run sequentially with size limits, timeouts, and cancellation
    checks. Use this object as a context manager to close the process and pipes.

    Args:
        transport (SshBackend): Shared SSH connection and session limits.
    """

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
            self.process = self.transport._start_process(
                self.transport.ssh + self.transport._ssh_options()
                + ["-T", "-s", "--", self.context.remote.host, "sftp"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, bufsize=0,
            )
            self.selector = selectors.DefaultSelector()
            for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
                os.set_blocking(pipe.fileno(), False)
            self.selector.register(self.process.stdout, selectors.EVENT_READ)
            self.selector.register(self.process.stderr, selectors.EVENT_READ)
            self._start_request_timer()
            self._send_packet(b"\x01" + struct.pack(">I", 3))
            kind, packet = self._receive_packet()
            if kind != 2 or packet.read_uint32() != 3:
                raise CapabilityError("Server must support SFTP version 3")
            while packet.offset < len(packet.data):
                packet.read_string()
                packet.read_string()  # Ignore unneeded negotiated extensions.
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.process is not None:
                self.transport._stop_process(self.process)
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

    def _start_request_timer(self):
        """Start the timeout for one metadata request."""
        self.deadline = time.monotonic() + self.context.listing_timeout

    def _check_request_active(self):
        """Check cancellation and the current request deadline."""
        self.context.check_cancelled()
        if time.monotonic() >= self.deadline:
            raise DownloadError("SFTP metadata request exceeded its time limit")

    def _poll_streams(self):
        """Buffer available output and report whether stdin is writable."""
        self._check_request_active()
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

    def _send_packet(self, payload):
        """Write one size-limited packet while checking cancellation."""
        if len(payload) > _MAX_PACKET:
            raise DownloadError("SFTP metadata request exceeds its size limit")
        data = memoryview(struct.pack(">I", len(payload)) + payload)
        self.selector.register(self.process.stdin, selectors.EVENT_WRITE)
        try:
            while data:
                if self._poll_streams():
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

    def _read_bytes(self, size):
        """Read the requested bytes within the current deadline."""
        while len(self.buffer) < size:
            self._poll_streams()
            self.context.check_cancelled()
            if (len(self.buffer) < size
                    and self.process.stdout.fileno() not in self.selector.get_map()):
                raise DownloadError("SFTP subsystem returned truncated metadata")
        self._check_request_active()
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def _receive_packet(self):
        """Read a response type and its validated packet body."""
        length = struct.unpack(">I", self._read_bytes(4))[0]
        if not 1 <= length <= _MAX_PACKET:
            raise DownloadError("Invalid or oversized SFTP metadata packet")
        data = self._read_bytes(length)
        return data[0], _Packet(data[1:])

    def _send_request(self, kind, data, expected, *, eof=False):
        """Send a request and validate its response identifier and status."""
        self._start_request_timer()
        self.request_id = (self.request_id + 1) % (2 ** 32)
        self._send_packet(bytes([kind]) + struct.pack(">I", self.request_id) + data)
        response, packet = self._receive_packet()
        if packet.read_uint32() != self.request_id:
            raise DownloadError("Mismatched SFTP response identifier")
        if response == 101:
            status = packet.read_uint32()
            packet.read_string()  # Untrusted server diagnostics are never echoed.
            packet.read_string()
            packet.check_packet_end()
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
        """Return the canonical path reported by the SFTP server."""
        packet = self._send_request(16, _encode_sftp_string(path), 104)
        if packet.read_uint32() != 1:
            raise DownloadError("Invalid SFTP canonical path response")
        result = packet.read_filename()
        packet.read_string()
        packet.read_attributes()
        packet.check_packet_end()
        return result

    def lstat(self, path):
        """Read file attributes without following a final symlink."""
        packet = self._send_request(7, _encode_sftp_string(path), 105)
        result = packet.read_attributes()
        packet.check_packet_end()
        return result

    def iterdir(self, path):
        """
        Yield names and attributes from one remote directory.

        Args:
            path (str): Absolute directory path on the server.

        Yields:
            tuple: Filename and attribute dictionary. Missing sizes, modes,
            and modification times are None.

        Raises:
            DownloadError: If the directory cannot be read or responses are invalid.
        """
        packet = self._send_request(11, _encode_sftp_string(path), 102)
        handle = packet.read_string()
        packet.check_packet_end()
        if not handle or len(handle) > 256:
            raise DownloadError("Invalid SFTP directory handle")
        completed = False
        try:
            while True:
                packet = self._send_request(
                    12, _encode_sftp_string(handle), 104, eof=True
                )
                if packet is None:
                    completed = True
                    return
                count = packet.read_uint32()
                if count == 0 or count > (len(packet.data) - packet.offset) // 12:
                    raise DownloadError("Invalid SFTP directory entry count")
                entries = []
                for _ in range(count):
                    name = packet.read_filename()
                    packet.read_string()  # Human-readable longname is not parsed.
                    entries.append((name, packet.read_attributes()))
                packet.check_packet_end()
                yield from entries
        finally:
            if not self.context.cancelled.is_set():
                try:
                    self._send_request(4, _encode_sftp_string(handle), 101)
                except DownloadError:
                    if completed:
                        raise
