# usbmuxd.py
#
# Copyright 2026 Nícolas Castillo
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Makes sure a usbmuxd is reachable.

The host's usbmuxd is preferred. Its socket usually only exists while a
device is attached and is re-created whenever the daemon restarts, which a
Flatpak sandbox cannot follow because socket bind mounts are fixed at launch.
Inside the sandbox the host socket is therefore reached through a forwarder
(systemd-socket-proxyd in the user's session) listening in the runtime
directory that is shared with the host.

The tools never talk to that forwarder directly. They go through our own relay
(see usbmuxd_relay), which keeps them from noticing that the daemon restarts
whenever a device changes mode.

If the host has no usbmuxd at all, the bundled one is started as the current
user on a private socket.
"""

import os
import plistlib
import shutil
import socket
import struct
import subprocess
import time

from gi.repository import GLib

from .const import APP_ID
from .host import in_flatpak, run_on_host
from .usbmuxd_relay import Relay

ENV_VAR = 'USBMUXD_SOCKET_ADDRESS'
HOST_SOCKETS = ('/run/usbmuxd', '/var/run/usbmuxd')

# States
IDLE = 'idle'                # nothing reachable, nothing needed yet
HOST = 'host'                # host daemon (directly or through the relay)
BUNDLED = 'bundled'          # our own instance
UNAVAILABLE = 'unavailable'  # needed, but nothing could be started
UNRESPONSIVE = 'unresponsive'  # the daemon accepts connections but is hung

# Units left behind by an instance that did not exit cleanly are replaced.
_PROXY_SCRIPT = '''
systemctl --user stop "$1.socket" "$1.service" 2>/dev/null
systemctl --user reset-failed "$1.socket" "$1.service" 2>/dev/null
rm -f "$2"
for d in /usr/lib/systemd /lib/systemd /usr/libexec; do
  if [ -x "$d/systemd-socket-proxyd" ]; then
    exec systemd-run --user --collect --quiet --unit="$1" \
      --socket-property=ListenStream="$2" "$d/systemd-socket-proxyd" "$3"
  fi
done
exit 1
'''
_PROXY_UNIT = 'corer-usbmuxd-proxy-' + APP_ID.replace('.', '-')


def _can_connect(path):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(1)
    try:
        sock.connect(path)
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _responds(path):
    """Whether the daemon behind the socket answers a device list request."""
    body = plistlib.dumps({
        'MessageType': 'ListDevices',
        'ClientVersionString': 'corer',
        'ProgName': 'corer',
        'kLibUSBMuxVersion': 3,
    })
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(4)
    try:
        sock.connect(path)
        sock.sendall(struct.pack('<IIII', 16 + len(body), 1, 8, 1) + body)
        return len(sock.recv(16)) > 0
    except OSError:
        return False
    finally:
        sock.close()


class UsbmuxdManager:
    """Not thread safe; call ensure() from a single thread."""

    def __init__(self):
        self._custom = bool(os.environ.get(ENV_VAR))
        self._proc = None
        self._proxy_socket = None
        self._relay = None
        self._host_has_usbmuxd = None
        self._misses = 0
        self.state = IDLE

    def _runtime_dir(self):
        base = GLib.get_user_runtime_dir()
        if in_flatpak():
            # Shared between the sandbox and the host, at the same path.
            base = os.path.join(base, 'app', APP_ID)
        os.makedirs(base, exist_ok=True)
        return base

    def _set_socket(self, path):
        if path:
            os.environ[ENV_VAR] = 'UNIX:' + path
        else:
            os.environ.pop(ENV_VAR, None)

    # Host relay (sandbox only)

    def _host_usbmuxd_installed(self):
        if self._host_has_usbmuxd is None:
            status, _out = run_on_host(
                ['sh', '-c', 'command -v usbmuxd >/dev/null 2>&1 || '
                             'test -S /run/usbmuxd || test -S /var/run/usbmuxd'])
            self._host_has_usbmuxd = status == 0
        return self._host_has_usbmuxd

    def _start_proxy(self):
        """Forward a socket in the shared runtime directory to the host's."""
        path = os.path.join(self._runtime_dir(), 'usbmuxd-host')
        status, _out = run_on_host(
            ['sh', '-c', _PROXY_SCRIPT, 'sh', _PROXY_UNIT, path,
             HOST_SOCKETS[0]])
        if status != 0:
            return False
        for _ in range(20):
            if os.path.exists(path):
                self._proxy_socket = path
                return True
            time.sleep(0.1)
        self._stop_proxy()
        return False

    def _stop_proxy(self):
        run_on_host(['systemctl', '--user', 'stop', _PROXY_UNIT + '.socket',
                     _PROXY_UNIT + '.service'])
        if self._proxy_socket:
            try:
                os.unlink(self._proxy_socket)
            except OSError:
                pass
        self._proxy_socket = None

    def _start_relay(self):
        """Start the relay the tools connect to. Returns False on failure."""
        # Without a user session manager to forward through, the socket bound
        # at launch is the only thing there is. It stops working once the
        # daemon restarts, but the relay still keeps the tools well behaved.
        self._start_proxy()
        upstreams = ([self._proxy_socket] if self._proxy_socket
                     else HOST_SOCKETS)
        relay = Relay(os.path.join(self._runtime_dir(), 'usbmuxd'), upstreams)
        if not relay.start():
            self._stop_relay()
            return False
        self._relay = relay
        self._set_socket(relay.listen_path)
        return True

    def _stop_relay(self):
        if self._relay:
            self._relay.stop()
            self._relay = None
        if self._proxy_socket:
            self._stop_proxy()

    def _relay_healthy(self):
        if not self._relay.alive():
            return False
        return not self._proxy_socket or os.path.exists(self._proxy_socket)

    # Bundled daemon

    def _start_bundled(self):
        binary = shutil.which('usbmuxd')
        if not binary:
            return False
        path = os.path.join(self._runtime_dir(), 'usbmuxd-bundled')
        config_dir = os.path.join(GLib.get_user_data_dir(), 'corer', 'lockdown')
        os.makedirs(config_dir, exist_ok=True)
        log_dir = os.path.join(GLib.get_user_state_dir(), 'corer', 'logs')
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, 'usbmuxd.log')
        try:
            # Keep the log of the previous session only.
            os.replace(log_path, log_path + '.old')
        except OSError:
            pass
        try:
            os.unlink(path)
        except OSError:
            pass
        try:
            self._proc = subprocess.Popen(
                [binary, '-f', '-v', '-S', path, '-P', 'NONE', '-C', config_dir,
                 '-l', log_path],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
        except OSError:
            self._proc = None
            return False
        for _ in range(30):
            if self._proc.poll() is not None:
                break
            if _can_connect(path):
                self._set_socket(path)
                return True
            time.sleep(0.1)
        self._stop_bundled()
        return False

    def _stop_bundled(self):
        if self._proc:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None

    # Public API

    def ensure(self, needed):
        """Return the current state, starting helpers if a device needs one.

        `needed` tells whether a device that requires usbmuxd is attached.
        """
        state = self._ensure(needed)
        if state == HOST and needed and not self._host_responds():
            state = UNRESPONSIVE
        self.state = state
        return self.state

    def _host_responds(self):
        address = os.environ.get(ENV_VAR, '')
        if address.startswith('UNIX:'):
            return _responds(address[5:])
        if address:
            return True  # TCP address chosen by the user; not probed
        return any(_responds(p) for p in HOST_SOCKETS if os.path.exists(p))

    def _ensure(self, needed):
        if self._custom:
            return HOST

        if self._proc:
            if self._proc.poll() is None:
                return BUNDLED
            self._proc = None
            self._set_socket(None)

        if self._relay:
            if not self._relay_healthy():
                self._stop_relay()
                self._set_socket(None)
            elif self._proxy_socket:
                return HOST
            else:
                return self._direct_state(needed)

        if in_flatpak():
            if self._host_usbmuxd_installed():
                if not self._start_relay():
                    return UNAVAILABLE if needed else IDLE
                if self._proxy_socket:
                    return HOST
                return self._direct_state(needed)
            return BUNDLED if self._start_bundled() else UNAVAILABLE

        if any(_can_connect(p) for p in HOST_SOCKETS):
            self._misses = 0
            return HOST
        if not needed:
            self._misses = 0
            return IDLE
        # Give a udev-activated system daemon time to come up before
        # competing with it for the device.
        self._misses += 1
        if self._misses < 3:
            return IDLE
        return BUNDLED if self._start_bundled() else UNAVAILABLE

    @staticmethod
    def _direct_state(needed):
        if any(_can_connect(p) for p in HOST_SOCKETS):
            return HOST
        return UNAVAILABLE if needed else IDLE

    def shutdown(self):
        self._stop_bundled()
        self._stop_relay()
        if not self._custom:
            self._set_socket(None)
