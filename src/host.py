# host.py
#
# Copyright 2026 Nícolas Castillo
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Helpers for talking to the host system, in and outside the sandbox."""

import os
import subprocess

from gi.repository import Gio, GLib

from .const import RESOURCE_PATH

UDEV_RULES_NAME = '71-corer-apple-devices.rules'
UDEV_RULES_TARGET = '/etc/udev/rules.d/' + UDEV_RULES_NAME

_UDEV_RELOAD = ('udevadm control --reload-rules && '
                'udevadm trigger --subsystem-match=usb --attr-match=idVendor=05ac')
_UDEV_INSTALL_SCRIPT = (f'cat > {UDEV_RULES_TARGET} && '
                        f'chmod 644 {UDEV_RULES_TARGET} && {_UDEV_RELOAD}')


def in_flatpak():
    return os.path.exists('/.flatpak-info')


def host_argv(argv):
    """Return argv adjusted so that it runs on the host system."""
    if in_flatpak():
        return ['flatpak-spawn', '--host', *argv]
    return list(argv)


def run(argv, timeout=10, input_data=None):
    """Run a command and return (exit status, stdout). Never raises."""
    try:
        proc = subprocess.run(argv, input=input_data, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=timeout,
                              check=False)
    except (OSError, subprocess.SubprocessError):
        return -1, ''
    return proc.returncode, proc.stdout.decode('utf-8', 'replace')


def run_on_host(argv, timeout=10):
    return run(host_argv(argv), timeout=timeout)


def udev_rules():
    data = Gio.resources_lookup_data(f'{RESOURCE_PATH}/{UDEV_RULES_NAME}',
                                     Gio.ResourceLookupFlags.NONE)
    return data.get_data()


def udev_manual_commands():
    """Shell commands the user can run to install the udev rule by hand."""
    rules = udev_rules().decode('utf-8')
    return (f"sudo tee {UDEV_RULES_TARGET} > /dev/null << 'EOF'\n"
            f'{rules.rstrip()}\nEOF\n'
            f'sudo sh -c "{_UDEV_RELOAD}"')


def install_udev_rules(callback):
    """Install the udev rule on the host through polkit.

    callback(success) is invoked on the main loop.
    """
    argv = host_argv(['pkexec', 'sh', '-c', _UDEV_INSTALL_SCRIPT])
    try:
        proc = Gio.Subprocess.new(
            argv,
            Gio.SubprocessFlags.STDIN_PIPE
            | Gio.SubprocessFlags.STDOUT_SILENCE
            | Gio.SubprocessFlags.STDERR_SILENCE)
    except GLib.Error:
        callback(False)
        return

    def on_done(proc, result):
        try:
            proc.communicate_finish(result)
            callback(proc.get_successful())
        except GLib.Error:
            callback(False)

    proc.communicate_async(GLib.Bytes.new(udev_rules()), None, on_done)


def restart_host_usbmuxd(callback):
    """Restart the system usbmuxd through polkit. callback(success)."""
    argv = host_argv(['pkexec', 'systemctl', 'restart', 'usbmuxd.service'])
    try:
        proc = Gio.Subprocess.new(
            argv,
            Gio.SubprocessFlags.STDOUT_SILENCE
            | Gio.SubprocessFlags.STDERR_SILENCE)
    except GLib.Error:
        callback(False)
        return

    def on_done(proc, result):
        try:
            proc.wait_finish(result)
            callback(proc.get_successful())
        except GLib.Error:
            callback(False)

    proc.wait_async(None, on_done)
