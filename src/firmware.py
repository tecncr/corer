# firmware.py
#
# Copyright 2026 Nícolas Castillo
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Signed firmware lookup, the IPSW cache and downloads."""

import hashlib
import json
import os
import plistlib
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from gettext import gettext as _

from gi.repository import GLib

from .const import VERSION

API_URL = 'https://api.ipsw.me/v4/device/{product}?type=ipsw'
USER_AGENT = f'Corer/{VERSION}'
_CHUNK = 1024 * 1024
_MAX_ATTEMPTS = 5


def cache_dir():
    path = os.path.join(GLib.get_user_cache_dir(), 'corer')
    os.makedirs(path, exist_ok=True)
    return path


def version_key(version):
    """Sort key for dotted version strings."""
    key = []
    for part in version.split('.'):
        digits = ''.join(ch for ch in part if ch.isdigit())
        key.append(int(digits) if digits else 0)
    return tuple(key)


class FirmwareError(Exception):
    """An error with a message suitable for showing to the user."""


class Cancelled(Exception):
    pass


@dataclass(frozen=True)
class Firmware:
    product_type: str
    version: str
    build: str
    url: str
    size: int
    sha1: str = ''
    sha256: str = ''

    @property
    def filename(self):
        name = os.path.basename(urllib.parse.urlparse(self.url).path)
        return name or f'{self.product_type}_{self.version}_{self.build}.ipsw'

    @property
    def path(self):
        return os.path.join(cache_dir(), self.filename)

    @property
    def is_cached(self):
        try:
            return os.path.getsize(self.path) == self.size
        except OSError:
            return False


@dataclass(frozen=True)
class LocalFirmware:
    """A firmware file provided by the user."""
    path: str
    version: str
    build: str
    product_types: tuple

    @property
    def filename(self):
        return os.path.basename(self.path)


# Signed firmware list


def _list_cache_path(product_type):
    safe = product_type.replace('/', '_')
    return os.path.join(cache_dir(), f'signed-{safe}.json')


def _parse_firmwares(data, product_type):
    firmwares = []
    for entry in data.get('firmwares', []):
        if not entry.get('signed') or not entry.get('url'):
            continue
        firmwares.append(Firmware(
            product_type=product_type,
            version=str(entry.get('version', '')),
            build=str(entry.get('buildid', '')),
            url=entry['url'],
            size=int(entry.get('filesize') or 0),
            sha1=str(entry.get('sha1sum') or ''),
            sha256=str(entry.get('sha256sum') or ''),
        ))
    firmwares.sort(key=lambda fw: version_key(fw.version), reverse=True)
    return firmwares


def fetch_signed(product_type):
    """Return (firmwares, offline) for a product type, newest first.

    If the network is unavailable the last known list is used, limited to
    files that are already in the cache, and offline is True.
    Raises FirmwareError if nothing can be offered.
    """
    cache_path = _list_cache_path(product_type)
    url = API_URL.format(product=urllib.parse.quote(product_type, safe=','))
    request = urllib.request.Request(url, headers={
        'User-Agent': USER_AGENT, 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
        data = json.loads(raw)
        if data.get('identifier') != product_type:
            raise ValueError('unexpected identifier')
    except (OSError, ValueError) as error:
        try:
            with open(cache_path, 'rb') as file:
                data = json.load(file)
        except (OSError, ValueError):
            raise FirmwareError(
                _('Could not load the list of available versions')) from error
        cached = [fw for fw in _parse_firmwares(data, product_type)
                  if fw.is_cached]
        if not cached:
            raise FirmwareError(
                _('Could not load the list of available versions')) from error
        return cached, True

    try:
        with open(cache_path + '.tmp', 'wb') as file:
            file.write(raw)
        os.replace(cache_path + '.tmp', cache_path)
    except OSError:
        pass
    return _parse_firmwares(data, product_type), False


def fetch_signed_async(product_type, callback):
    """callback(firmwares, offline, error_message) runs on the main loop."""
    def work():
        try:
            firmwares, offline = fetch_signed(product_type)
            GLib.idle_add(callback, firmwares, offline, None)
        except FirmwareError as error:
            GLib.idle_add(callback, [], False, str(error))

    threading.Thread(target=work, daemon=True, name='corer-firmwares').start()


# Local files


def inspect_ipsw(path):
    """Read version information from an IPSW file."""
    try:
        with zipfile.ZipFile(path) as archive:
            with archive.open('BuildManifest.plist') as file:
                manifest = plistlib.load(file)
    except (OSError, KeyError, ValueError, zipfile.BadZipFile,
            plistlib.InvalidFileException) as error:
        raise FirmwareError(_('This is not a valid firmware file')) from error
    return LocalFirmware(
        path=path,
        version=str(manifest.get('ProductVersion', '')),
        build=str(manifest.get('ProductBuildVersion', '')),
        product_types=tuple(manifest.get('SupportedProductTypes', ())),
    )


# Cache management


def cache_size():
    """Size of everything in the cache except the small version lists."""
    base = cache_dir()
    total = 0
    for root, _dirs, files in os.walk(base):
        for name in files:
            if root == base and name.startswith('signed-'):
                continue
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def clear_cache():
    base = cache_dir()
    for name in os.listdir(base):
        path = os.path.join(base, name)
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)
        except OSError:
            pass


def remove_cached(firmware):
    for path in (firmware.path, firmware.path + '.part',
                 firmware.path + '.ok'):
        try:
            os.unlink(path)
        except OSError:
            pass


# Downloads


class Download:
    """Downloads a firmware into the cache and verifies it.

    Callbacks run on the main loop:
      on_progress(phase, done_bytes, total_bytes, bytes_per_second)
        with phase being 'download' or 'verify'
      on_finished(path, error_message, cancelled)
    """

    def __init__(self, firmware, on_progress, on_finished):
        self._firmware = firmware
        self._on_progress = on_progress
        self._on_finished = on_finished
        self._cancel = threading.Event()
        self._last_emit = 0

    def start(self):
        threading.Thread(target=self._run, daemon=True,
                         name='corer-download').start()

    def cancel(self):
        self._cancel.set()

    def _emit(self, phase, done, total, speed=0, force=False):
        now = time.monotonic()
        if force or now - self._last_emit >= 0.2:
            self._last_emit = now
            GLib.idle_add(self._on_progress, phase, done, total, speed)

    def _run(self):
        try:
            path = self._fetch()
            GLib.idle_add(self._on_finished, path, None, False)
        except Cancelled:
            GLib.idle_add(self._on_finished, None, None, True)
        except FirmwareError as error:
            GLib.idle_add(self._on_finished, None, str(error), False)
        except OSError as error:
            GLib.idle_add(self._on_finished, None,
                          error.strerror or str(error), False)

    # Verification

    def _marker(self, path):
        stat = os.stat(path)
        return f'{stat.st_size} {stat.st_mtime_ns}'

    def _is_marked(self, path):
        try:
            with open(path + '.ok', encoding='utf-8') as file:
                return file.read() == self._marker(path)
        except OSError:
            return False

    def _mark(self, path):
        try:
            with open(path + '.ok', 'w', encoding='utf-8') as file:
                file.write(self._marker(path))
        except OSError:
            pass

    def _verify(self, path):
        fw = self._firmware
        if fw.sha256:
            digest, expected = hashlib.sha256(), fw.sha256
        elif fw.sha1:
            digest, expected = hashlib.sha1(), fw.sha1
        else:
            return True
        total = os.path.getsize(path)
        done = 0
        self._emit('verify', 0, total, force=True)
        with open(path, 'rb') as file:
            while True:
                if self._cancel.is_set():
                    raise Cancelled()
                chunk = file.read(_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                done += len(chunk)
                self._emit('verify', done, total)
        return digest.hexdigest().lower() == expected.lower()

    # Download

    def _fetch(self):
        fw = self._firmware
        path = fw.path
        part = path + '.part'

        if fw.is_cached:
            if self._is_marked(path) or self._verify(path):
                self._mark(path)
                return path
            remove_cached(fw)

        if fw.url.startswith('protected:'):
            raise FirmwareError(_('This firmware is not available for download'))

        attempts = 0
        while True:
            have = os.path.getsize(part) if os.path.exists(part) else 0
            if fw.size and have == fw.size:
                break
            if fw.size and have > fw.size:
                os.unlink(part)
                have = 0
            if fw.size:
                free = shutil.disk_usage(cache_dir()).free
                if free < fw.size - have:
                    raise FirmwareError(
                        _('Not enough free disk space to download the firmware'))
            try:
                self._download_range(part, have)
                break
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                if isinstance(error, urllib.error.HTTPError):
                    if error.code == 416:
                        os.unlink(part)
                        continue
                    if error.code < 500:
                        raise FirmwareError(
                            _('The download server refused the request')
                        ) from error
                now_have = os.path.getsize(part) if os.path.exists(part) else 0
                # Only count attempts that made no progress.
                attempts = 0 if now_have > have else attempts + 1
                if attempts >= _MAX_ATTEMPTS:
                    raise FirmwareError(
                        _('The download failed. Check your network connection')
                    ) from error
                if self._cancel.wait(2 * attempts + 1):
                    raise Cancelled() from None

        if fw.size and os.path.getsize(part) != fw.size:
            os.unlink(part)
            raise FirmwareError(_('The downloaded firmware is incomplete'))
        if not self._verify(part):
            os.unlink(part)
            raise FirmwareError(_('The downloaded firmware is corrupted'))
        os.replace(part, path)
        self._mark(path)
        return path

    def _download_range(self, part, have):
        fw = self._firmware
        headers = {'User-Agent': USER_AGENT}
        if have:
            headers['Range'] = f'bytes={have}-'
        request = urllib.request.Request(fw.url, headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            if have and response.status != 206:
                have = 0
            length = response.headers.get('Content-Length')
            total = fw.size or (have + int(length) if length else 0)
            done = have
            window_start = time.monotonic()
            window_bytes = 0
            speed = 0
            self._emit('download', done, total, 0, force=True)
            with open(part, 'ab' if have else 'wb') as file:
                while True:
                    if self._cancel.is_set():
                        raise Cancelled()
                    chunk = response.read(_CHUNK)
                    if not chunk:
                        break
                    file.write(chunk)
                    done += len(chunk)
                    window_bytes += len(chunk)
                    elapsed = time.monotonic() - window_start
                    if elapsed >= 1:
                        speed = window_bytes / elapsed
                        window_start = time.monotonic()
                        window_bytes = 0
                    self._emit('download', done, total, speed)
            if fw.size and done < fw.size:
                raise ConnectionError('connection closed early')
