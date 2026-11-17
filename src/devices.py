# devices.py
#
# Copyright 2026 Nícolas Castillo
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Detection of attached Apple devices using the libimobiledevice tools."""

import glob
import os
import plistlib
import re
import threading
from dataclasses import dataclass
from gettext import gettext as _

from gi.repository import GLib, GObject

from . import usbmuxd
from .host import run

APPLE_VENDOR = '05ac'
RECOVERY_PRODUCTS = {'1280', '1281', '1282', '1283'}
DFU_PRODUCTS = {'1227', '1222', 'f014', '1881'}

MODE_NORMAL = 'normal'
MODE_RECOVERY = 'recovery'
MODE_DFU = 'dfu'
MODE_UNKNOWN = 'unknown'

_ECID_RE = re.compile(r'ECID:([0-9A-Fa-f]+)')
_POLL_SECONDS = 2
_RETRIES = 8


@dataclass(frozen=True)
class Device:
    key: str
    mode: str
    name: str = ''
    product_type: str = ''
    hardware_model: str = ''
    device_class: str = ''
    version: str = ''
    build: str = ''
    ecid: int = 0
    udid: str = ''
    accessible: bool = True

    @property
    def os_name(self):
        product = self.product_type
        if product.startswith('iPad'):
            return 'iPadOS'
        if product.startswith('AppleTV'):
            return 'tvOS'
        if product.startswith('Watch'):
            return 'watchOS'
        if product.startswith(('AudioAccessory', 'HomePod')):
            return 'audioOS'
        if product.startswith(('Mac', 'iMac', 'VirtualMac')):
            return 'macOS'
        if product.startswith('RealityDevice'):
            return 'visionOS'
        return 'iOS'

    def icon_name(self, display_name=''):
        """Icon for the device.

        Recent Mac product types such as Mac14,2 do not tell laptops and
        desktops apart, so the marketing name is looked at as well.
        """
        product = self.product_type
        if product.startswith('iPad'):
            return 'corer-ipad-symbolic'
        if product.startswith('iPod'):
            return 'corer-ipod-symbolic'
        if product.startswith('MacBook') or 'MacBook' in display_name:
            return 'corer-laptop-symbolic'
        if product.startswith(('Mac', 'iMac', 'VirtualMac')):
            return 'corer-computer-symbolic'
        if product.startswith('AppleTV'):
            return 'corer-tv-symbolic'
        return 'corer-iphone-symbolic'

    @property
    def mode_label(self):
        if self.mode == MODE_RECOVERY:
            return _('Recovery Mode')
        if self.mode == MODE_DFU:
            return _('DFU Mode')
        return ''


def _read(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as file:
            return file.read().strip()
    except OSError:
        return ''


def scan_usb():
    """Return Apple USB devices that could be restorable, from sysfs."""
    found = []
    for vendor_path in glob.glob('/sys/bus/usb/devices/*/idVendor'):
        if _read(vendor_path).lower() != APPLE_VENDOR:
            continue
        base = os.path.dirname(vendor_path)
        product = _read(os.path.join(base, 'idProduct')).lower()
        if product in RECOVERY_PRODUCTS:
            mode = MODE_RECOVERY
        elif product in DFU_PRODUCTS:
            mode = MODE_DFU
        elif product.startswith('12') or product in ('8600', '1901'):
            # Mobile devices in normal or restore mode
            mode = MODE_NORMAL
        else:
            continue
        try:
            bus = int(_read(os.path.join(base, 'busnum')))
            dev = int(_read(os.path.join(base, 'devnum')))
        except ValueError:
            continue
        node = f'/dev/bus/usb/{bus:03d}/{dev:03d}'
        found.append({
            'mode': mode,
            'bus': bus,
            'dev': dev,
            'product_id': product,
            'serial': _read(os.path.join(base, 'serial')),
            'label': _read(os.path.join(base, 'product')),
            'accessible': os.access(node, os.R_OK | os.W_OK),
        })
    found.sort(key=lambda d: (d['bus'], d['dev']))
    return found


class DeviceMonitor(GObject.Object):
    """Watches for devices from a worker thread.

    Emits `changed` on the main loop whenever `devices`, `usb_access_missing`
    or `usbmuxd_state` change.
    """

    __gsignals__ = {
        'changed': (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__()
        self.devices = []
        self.usb_access_missing = False
        self.usbmuxd_state = usbmuxd.IDLE
        self.scanned = False

        self._usbmuxd = usbmuxd.UsbmuxdManager()
        self._display_names = {}
        self._wake = threading.Event()
        self._stop = False
        self._paused = False
        self._force = False
        self._thread = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name='corer-devices')
            self._thread.start()

    def stop(self):
        self._stop = True
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=15)
            self._thread = None
        self._usbmuxd.shutdown()

    def pause(self):
        """Stop querying devices, e.g. while a restore is running."""
        self._paused = True

    def resume(self):
        self._paused = False
        self.refresh()

    def refresh(self):
        self._force = True
        self._wake.set()

    # Worker thread

    def _load_display_names(self):
        names = {}
        status, out = run(['irecovery', '-a'], timeout=10)
        if status == 0:
            for line in out.splitlines():
                parts = line.split(' ', 4)
                if len(parts) == 5:
                    names.setdefault(parts[0], parts[4].strip())
        self._display_names = names

    def _loop(self):
        last_signature = None
        retries = 0
        self._load_display_names()
        while not self._stop:
            if not self._paused:
                usb = scan_usb()
                signature = tuple(
                    (d['bus'], d['dev'], d['product_id'], d['accessible'])
                    for d in usb)
                if signature != last_signature:
                    retries = _RETRIES
                if signature != last_signature or self._force or retries > 0:
                    self._force = False
                    last_signature = signature
                    complete = self._query(usb)
                    retries = 0 if complete else max(retries - 1, 0)
            self._wake.wait(_POLL_SECONDS)
            self._wake.clear()

    def _query(self, usb):
        """Query all devices. Returns False if another attempt is worthwhile."""
        normal_usb = [d for d in usb if d['mode'] == MODE_NORMAL]
        state = self._usbmuxd.ensure(bool(normal_usb))

        devices = []
        if state in (usbmuxd.HOST, usbmuxd.BUNDLED):
            devices += self._query_normal()
        complete = len(devices) >= len(normal_usb)

        access_missing = False
        for entry in usb:
            if entry['mode'] == MODE_NORMAL:
                # The system daemon does not need us to have access.
                if state == usbmuxd.BUNDLED and not entry['accessible']:
                    access_missing = True
                continue
            if not entry['accessible']:
                access_missing = True
            devices.append(self._query_recovery(entry))

        GLib.idle_add(self._publish, devices, access_missing, state)
        return complete

    def _query_normal(self):
        status, out = run(['idevice_id', '-l'], timeout=10)
        if status != 0:
            return []
        devices = []
        for udid in dict.fromkeys(out.split()):
            info = {}
            status, xml = run(['ideviceinfo', '-s', '-x', '-u', udid],
                              timeout=15)
            if status == 0:
                try:
                    info = plistlib.loads(xml.encode('utf-8'))
                except (plistlib.InvalidFileException, ValueError):
                    info = {}
            if not info:
                # Most likely a device in restore mode, which has no
                # lockdown service to ask.
                devices.append(Device(key='udid:' + udid, mode=MODE_UNKNOWN,
                                      udid=udid))
                continue
            product = str(info.get('ProductType', ''))
            ecid = info.get('UniqueChipID', 0)
            devices.append(Device(
                key='udid:' + udid,
                mode=MODE_NORMAL,
                name=str(info.get('DeviceName', '')),
                product_type=product,
                hardware_model=str(info.get('HardwareModel', '')),
                device_class=str(info.get('DeviceClass', '')),
                version=str(info.get('ProductVersion', '')),
                build=str(info.get('BuildVersion', '')),
                ecid=ecid if isinstance(ecid, int) else 0,
                udid=udid,
            ))
        return devices

    def _query_recovery(self, entry):
        match = _ECID_RE.search(entry['serial'])
        ecid = int(match.group(1), 16) if match else 0
        key = f'ecid:{ecid:x}' if ecid else f'usb:{entry["bus"]}:{entry["dev"]}'
        fields = {}
        if entry['accessible']:
            argv = ['irecovery', '-q']
            if ecid:
                argv += ['-i', f'0x{ecid:x}']
            status, out = run(argv, timeout=10)
            if status == 0:
                for line in out.splitlines():
                    name, sep, value = line.partition(':')
                    if sep:
                        fields[name.strip()] = value.strip()
        return Device(
            key=key,
            mode=entry['mode'],
            product_type=fields.get('PRODUCT', ''),
            hardware_model=fields.get('MODEL', ''),
            ecid=ecid,
            accessible=entry['accessible'],
        )

    # Main thread

    def _publish(self, devices, access_missing, state):
        changed = (not self.scanned
                   or devices != self.devices
                   or access_missing != self.usb_access_missing
                   or state != self.usbmuxd_state)
        self.scanned = True
        self.devices = devices
        self.usb_access_missing = access_missing
        self.usbmuxd_state = state
        if changed:
            self.emit('changed')
        return GLib.SOURCE_REMOVE

    def display_name(self, product_type):
        """Marketing name for a product type such as iPhone16,1."""
        return self._display_names.get(product_type, product_type)
