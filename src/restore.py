# restore.py
#
# Copyright 2026 Nícolas Castillo
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Runs idevicerestore and reports its progress."""

import os
import re
import shlex
import signal
import time
from gettext import gettext as _

from gi.repository import Gio, GLib, GObject

from . import firmware

_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')
_PROGRESS_RE = re.compile(r'^progress: (\d+) ([0-9.]+)$')
# Per-component progress printed in plain progress mode, e.g. "Verifying:   0.4"
_COMPONENT_RE = re.compile(r'^[^:<>]{1,48}:\s+[01]\.\d$')
_ERROR_RE = re.compile(r'<Error>\s*(?:ERROR:\s*)?(.+)$')

STEP_LABELS = (
    _('Detecting Device'),
    _('Preparing Device'),
    _('Sending Filesystem'),
    _('Verifying Filesystem'),
    _('Installing Firmware'),
    _('Updating Baseband'),
    _('Updating Components'),
    _('Sending Images'),
)


_KEEP_LOGS = 40


def logs_dir():
    path = os.path.join(GLib.get_user_state_dir(), 'corer', 'logs')
    os.makedirs(path, exist_ok=True)
    return path


def new_log_path():
    """Path for a new session log. Old logs beyond the newest ones are removed."""
    base = logs_dir()
    old = sorted(name for name in os.listdir(base)
                 if name.startswith('restore-') and name.endswith('.log'))
    for name in old[:-(_KEEP_LOGS - 1)]:
        try:
            os.unlink(os.path.join(base, name))
        except OSError:
            pass
    return os.path.join(base, time.strftime('restore-%Y%m%d-%H%M%S.log'))


def step_label(step):
    if 0 <= step < len(STEP_LABELS):
        return STEP_LABELS[step]
    return _('Restoring')


def build_argv(settings, device, ipsw_path, erase=False, shsh_only=False,
               logfile=None):
    # idevicerestore only prints up to info level on stdout. Everything else,
    # including all debug output, goes to its log file only.
    argv = ['idevicerestore', '--plain-progress', '--no-input',
            f'--logfile={logfile or "NONE"}',
            '--cache-path', firmware.cache_dir()]
    if device.ecid:
        argv += ['--ecid', f'0x{device.ecid:x}']
    elif device.udid:
        argv += ['--udid', device.udid]
    if shsh_only:
        argv.append('--shsh')
    elif erase:
        argv.append('--erase')
    for key, flag in (('debug', '--debug'),
                      ('allow-restore-mode', '--restore-mode'),
                      ('ignore-errors', '--ignore-errors'),
                      ('exclude-baseband', '--exclude'),
                      ('keep-personalized', '--keep-pers'),
                      ('custom-firmware', '--custom'),
                      ('no-restore', '--no-restore')):
        if settings.get_boolean(key):
            argv.append(flag)
    server = settings.get_string('signing-server').strip()
    if server:
        argv += ['--server', server]
    variant = settings.get_string('variant').strip()
    if variant:
        argv += ['--variant', variant]
    argv.append(ipsw_path)
    return argv


class RestoreRunner(GObject.Object):
    __gsignals__ = {
        # step, fraction of the step
        'progress': (GObject.SignalFlags.RUN_FIRST, None, (int, float)),
        'log': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # success, cancelled, last error message
        'finished': (GObject.SignalFlags.RUN_FIRST, None, (bool, bool, str)),
    }

    def __init__(self, argv):
        super().__init__()
        self._argv = argv
        self._proc = None
        self._stream = None
        self._cancelled = False
        self._last_error = ''
        self._kill_source = 0
        self._eof = False
        self._pending = b''
        self._last_line = ''
        self._status = 'unknown status'
        self._exited = False
        self._success = False

    def start(self):
        self.emit('log', '$ ' + shlex.join(self._argv))
        try:
            self._proc = Gio.Subprocess.new(
                self._argv,
                Gio.SubprocessFlags.STDOUT_PIPE
                | Gio.SubprocessFlags.STDERR_MERGE)
        except GLib.Error as error:
            self.emit('log', error.message)
            self.emit('finished', False, False,
                      _('idevicerestore could not be started'))
            return
        self._stream = self._proc.get_stdout_pipe()
        self._read_next()
        self._proc.wait_async(None, self._on_exit)

    def cancel(self):
        if self._proc is None or self._cancelled:
            return
        self._cancelled = True
        self._proc.send_signal(signal.SIGINT)
        self._kill_source = GLib.timeout_add_seconds(10, self._force_exit)

    def _force_exit(self):
        self._kill_source = 0
        if self._proc is not None:
            self._proc.force_exit()
        return GLib.SOURCE_REMOVE

    def _read_next(self):
        self._stream.read_bytes_async(8192, GLib.PRIORITY_DEFAULT, None,
                                      self._on_bytes)

    def _on_bytes(self, stream, result):
        try:
            data = stream.read_bytes_finish(result).get_data()
        except GLib.Error:
            data = b''
        if not data:
            if self._pending:
                self._handle_line(self._clean(self._pending))
                self._pending = b''
            self._eof = True
            self._maybe_finish()
            return
        # Carriage returns separate in-place updates of the same line.
        lines = (self._pending + data).replace(b'\r', b'\n').split(b'\n')
        self._pending = lines.pop()
        for line in lines:
            self._handle_line(self._clean(line))
        self._read_next()

    @staticmethod
    def _clean(raw):
        return _ANSI_RE.sub('', raw.decode('utf-8', 'replace')).rstrip()

    def _handle_line(self, line):
        if not line:
            return
        match = _PROGRESS_RE.match(line)
        if match:
            try:
                fraction = min(max(float(match.group(2)), 0.0), 1.0)
            except ValueError:
                fraction = 0.0
            self.emit('progress', int(match.group(1)), fraction)
            return
        if _COMPONENT_RE.match(line):
            return
        match = _ERROR_RE.search(line)
        if match:
            self._last_error = match.group(1).strip()
        self._last_line = line
        self.emit('log', line)

    def _on_exit(self, proc, result):
        try:
            proc.wait_finish(result)
            self._success = proc.get_successful()
            if proc.get_if_exited():
                self._status = f'exit status {proc.get_exit_status()}'
            else:
                self._status = f'signal {proc.get_term_sig()}'
        except GLib.Error:
            self._success = False
        if self._kill_source:
            GLib.source_remove(self._kill_source)
            self._kill_source = 0
        self._exited = True
        self._maybe_finish()

    def _maybe_finish(self):
        # Wait for the remaining output so the last error is not lost.
        if not (self._eof and self._exited) or self._proc is None:
            return
        self._proc = None
        message = ''
        if not self._success:
            # Early failures are printed without a log level.
            message = self._last_error or self._last_line
        self.emit('log', f'idevicerestore finished with {self._status}')
        self.emit('finished', self._success,
                  self._cancelled and not self._success, message)
