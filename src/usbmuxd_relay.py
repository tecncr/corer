# usbmuxd_relay.py
#
# Copyright 2026 Nícolas Castillo
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""A usbmuxd proxy that hides restarts of the daemon from its clients.

The system usbmuxd usually exits when the last device goes away and is started
again when one shows up, which happens several times during a restore: the
device leaves normal mode for recovery mode and comes back in restore mode.

libusbmuxd copes badly when the daemon is reached through something that keeps
accepting connections while the daemon is down, such as a socket activated
forwarder. Its event listener reconnects in a tight loop and leaks a file
descriptor on every attempt, until the process has none left and can neither
see the device come back nor open it over USB.

This relay sits in between. A client that subscribes to device events while
the daemon is down is kept waiting, and its subscription is delivered once the
daemon is back, so the client sees the device arrive as if nothing happened.
Every other request fails right away, as it would without a daemon.

It only uses the standard library and does not depend on a main loop.
"""

import os
import plistlib
import select
import socket
import struct
import threading

_HEADER = struct.Struct('<IIII')
_MAX_REQUEST = 1 << 20
_BUFFER = 1 << 18
_RETRY_SECONDS = 0.25
_MESSAGE_LISTEN = 3
_MESSAGE_PLIST = 8


def _close(sock):
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _receive(sock, size):
    """Read exactly `size` bytes; fewer are returned only at end of stream."""
    data = b''
    while len(data) < size:
        try:
            chunk = sock.recv(size - len(data))
        except OSError:
            break
        if not chunk:
            break
        data += chunk
    return data


def _is_listen(header, payload):
    _length, version, message, _tag = header
    if version == 0:
        return message == _MESSAGE_LISTEN
    if message != _MESSAGE_PLIST:
        return False
    try:
        request = plistlib.loads(payload)
    except Exception:  # pylint: disable=broad-except
        return False
    return isinstance(request, dict) and request.get('MessageType') == 'Listen'


class Relay:
    """Forwards connections made to `listen_path` to one of `upstreams`."""

    def __init__(self, listen_path, upstreams):
        self.listen_path = listen_path
        self._upstreams = tuple(upstreams)
        self._server = None
        self._thread = None
        self._lock = threading.Lock()
        self._sockets = set()
        self._stopped = threading.Event()

    def start(self):
        try:
            os.unlink(self.listen_path)
        except OSError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(self.listen_path)
            os.chmod(self.listen_path, 0o600)
            server.listen(64)
        except OSError:
            server.close()
            return False
        self._server = server
        self._thread = threading.Thread(target=self._accept_loop, daemon=True,
                                        name='corer-usbmuxd-relay')
        self._thread.start()
        return True

    def alive(self):
        return (self._thread is not None and self._thread.is_alive()
                and os.path.exists(self.listen_path))

    def stop(self):
        self._stopped.set()
        _close(self._server)
        with self._lock:
            sockets = list(self._sockets)
        for sock in sockets:
            _close(sock)
        try:
            os.unlink(self.listen_path)
        except OSError:
            pass

    # Bookkeeping, so that stop() can interrupt blocked threads

    def _track(self, sock):
        with self._lock:
            self._sockets.add(sock)

    def _release(self, sock):
        if sock is None:
            return
        with self._lock:
            self._sockets.discard(sock)
        _close(sock)

    # Worker threads

    def _accept_loop(self):
        while not self._stopped.is_set():
            try:
                client, _address = self._server.accept()
            except OSError:
                break
            self._track(client)
            threading.Thread(target=self._serve, args=(client,), daemon=True,
                             name='corer-usbmuxd-relay-client').start()

    def _connect(self):
        for path in self._upstreams:
            upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                upstream.connect(path)
                return upstream
            except OSError:
                upstream.close()
        return None

    def _open(self, request):
        """Send the first request and wait for the daemon to react.

        Returns (socket, first bytes of the answer), or (None, b'') when no
        daemon answered. The upstream may itself be a forwarder that accepts
        the connection and drops it when the daemon is down, so a connection
        only counts once something came back.
        """
        upstream = self._connect()
        if upstream is None:
            return None, b''
        self._track(upstream)
        try:
            upstream.sendall(request)
            answer = upstream.recv(_BUFFER)
        except OSError:
            answer = b''
        if not answer:
            self._release(upstream)
            return None, b''
        return upstream, answer

    def _client_gone(self, client):
        """Wait a moment; tells whether the waiting client hung up."""
        try:
            readable, _w, _x = select.select([client], [], [], _RETRY_SECONDS)
            if readable:
                return not client.recv(1, socket.MSG_PEEK)
        except (OSError, ValueError):
            return True
        return False

    def _serve(self, client):
        upstream = None
        try:
            raw = _receive(client, _HEADER.size)
            if len(raw) < _HEADER.size:
                return
            header = _HEADER.unpack(raw)
            size = header[0] - _HEADER.size
            if size < 0 or size > _MAX_REQUEST:
                return
            payload = _receive(client, size)
            if len(payload) < size:
                return
            request = raw + payload
            listening = _is_listen(header, payload)

            upstream, answer = self._open(request)
            while upstream is None:
                if (not listening or self._stopped.is_set()
                        or self._client_gone(client)):
                    return
                upstream, answer = self._open(request)

            client.sendall(answer)
            # When the daemon hangs up the conversation is over. libusbmuxd
            # never closes an event connection that failed, so waiting for
            # the client to do it would wait forever.
            back = threading.Thread(target=self._pump,
                                    args=(upstream, client, True), daemon=True,
                                    name='corer-usbmuxd-relay-pump')
            back.start()
            self._pump(client, upstream, False)
            back.join()
        except OSError:
            pass
        finally:
            self._release(upstream)
            self._release(client)

    @staticmethod
    def _pump(source, sink, hang_up):
        try:
            while True:
                data = source.recv(_BUFFER)
                if not data:
                    break
                sink.sendall(data)
        except OSError:
            pass
        # Pass the end of the stream on. Unless told to hang up, the other
        # direction may still have data in flight.
        try:
            sink.shutdown(socket.SHUT_RDWR if hang_up else socket.SHUT_WR)
        except OSError:
            pass
