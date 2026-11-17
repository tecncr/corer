# window.py
#
# Copyright 2026 Nícolas Castillo
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import threading
import time
from gettext import gettext as _

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from . import devices as dev
from . import firmware, host, usbmuxd
from .const import APP_ID, PROFILE, RESOURCE_PATH, VERSION
from .log_dialog import LogDialog
from .restore import (RestoreRunner, build_argv, new_log_path, step_label)

KIND_RESTORE = 'restore'
KIND_SHSH = 'shsh'

ICON_DOWNLOAD = 'corer-folder-download-symbolic'
ICON_VERIFY = 'corer-security-high-symbolic'
ICON_STOP = 'corer-process-stop-symbolic'
# Indexed by idevicerestore's progress step. The first one, detecting the
# device, shows the device itself.
STEP_ICONS = (
    None,
    'corer-emblem-system-symbolic',
    'corer-document-send-symbolic',
    ICON_VERIFY,
    'corer-system-software-install-symbolic',
    'corer-network-cellular-signal-excellent-symbolic',
    'corer-application-x-firmware-symbolic',
    'corer-media-flash-symbolic',
)


@Gtk.Template(resource_path=f'{RESOURCE_PATH}/window.ui')
class CorerWindow(Adw.ApplicationWindow):
    __gtype_name__ = 'CorerWindow'

    toast_overlay = Gtk.Template.Child()
    banner = Gtk.Template.Child()
    stack = Gtk.Template.Child()
    device_icon = Gtk.Template.Child()
    device_title = Gtk.Template.Child()
    device_subtitle = Gtk.Template.Child()
    device_row = Gtk.Template.Child()
    firmware_row = Gtk.Template.Child()
    firmware_spinner = Gtk.Template.Child()
    firmware_retry_button = Gtk.Template.Child()
    file_row = Gtk.Template.Child()
    file_clear_button = Gtk.Template.Child()
    erase_row = Gtk.Template.Child()
    recovery_button = Gtk.Template.Child()
    action_button = Gtk.Template.Child()
    progress_icon = Gtk.Template.Child()
    progress_title = Gtk.Template.Child()
    progress_subtitle = Gtk.Template.Child()
    progress_bar = Gtk.Template.Child()
    progress_detail = Gtk.Template.Child()
    cancel_button = Gtk.Template.Child()
    done_page = Gtk.Template.Child()
    error_page = Gtk.Template.Child()

    def __init__(self, settings, monitor, **kwargs):
        super().__init__(**kwargs)
        self.settings = settings
        self.monitor = monitor

        self._devices = []
        self._device = None
        self._firmwares = []
        self._firmware_cache = {}
        self._firmware_state = 'idle'  # idle, loading, ready, error
        self._firmware_offline = False
        self._firmware_error = ''
        self._local = None
        self._updating = False

        self._busy = False
        self._kind = KIND_RESTORE
        self._erasing = False
        self._source = None
        self._download = None
        self._runner = None
        self._cancelling = False
        self._close_when_done = False
        self._inhibit_cookie = 0
        self._result_shown = False
        self._banner_action = None
        self._last_attempt = None
        self._recovery_pending = False

        self.log_buffer = Gtk.TextBuffer()
        self._log_file = None

        if PROFILE == 'development':
            self.add_css_class('devel')

        settings.bind('window-width', self, 'default-width',
                      Gio.SettingsBindFlags.DEFAULT)
        settings.bind('window-height', self, 'default-height',
                      Gio.SettingsBindFlags.DEFAULT)
        settings.bind('window-maximized', self, 'maximized',
                      Gio.SettingsBindFlags.DEFAULT)

        self._add_action('open-file', self._on_open_file)
        self._add_action('show-log', self._on_show_log)
        self._add_action('refresh', lambda *_args: self.monitor.refresh())
        self._add_action('save-shsh', self._on_save_shsh)

        self.connect('close-request', self._on_close_request)
        self._monitor_handler = monitor.connect('changed',
                                                self._on_devices_changed)
        self.connect('destroy',
                     lambda *_args: monitor.disconnect(self._monitor_handler))
        self._on_devices_changed(monitor)

    def _add_action(self, name, callback):
        action = Gio.SimpleAction.new(name, None)
        action.connect('activate', callback)
        self.add_action(action)

    def _toast(self, message):
        self.toast_overlay.add_toast(Adw.Toast.new(message))

    # Devices

    def _device_title(self, device):
        if device.name:
            return device.name
        return (self.monitor.display_name(device.product_type)
                or _('Apple Device'))

    def _device_icon(self, device):
        return device.icon_name(self.monitor.display_name(device.product_type))

    def _device_subtitle(self, device):
        parts = []
        model = self.monitor.display_name(device.product_type)
        if model and model != self._device_title(device):
            parts.append(model)
        if device.version:
            parts.append(f'{device.os_name} {device.version}')
        if device.mode_label:
            parts.append(device.mode_label)
        return ' · '.join(parts)

    def _on_devices_changed(self, monitor):
        if self._busy:
            return
        self._devices = list(monitor.devices)
        previous = self._device.key if self._device else None
        selected = 0
        for index, device in enumerate(self._devices):
            if device.key == previous:
                selected = index
                break

        self._updating = True
        self.device_row.set_model(Gtk.StringList.new(
            [self._device_title(d) for d in self._devices]))
        if self._devices:
            self.device_row.set_selected(selected)
        self.device_row.set_visible(len(self._devices) > 1)
        self._updating = False

        self._set_device(self._devices[selected] if self._devices else None)
        self._update_banner()
        self._update_page()

    @Gtk.Template.Callback()
    def on_device_selected(self, *_args):
        if self._updating:
            return
        index = self.device_row.get_selected()
        if 0 <= index < len(self._devices):
            self._set_device(self._devices[index])

    def _set_device(self, device):
        previous = self._device
        self._device = device
        if device is None:
            self._update_controls()
            return
        self.device_icon.set_from_icon_name(self._device_icon(device))
        self.device_title.set_label(self._device_title(device))
        subtitle = self._device_subtitle(device)
        self.device_subtitle.set_label(subtitle)
        self.device_subtitle.set_visible(bool(subtitle))
        if previous is None or previous.product_type != device.product_type:
            self._load_firmwares()
        self._update_controls()

    def _update_banner(self):
        monitor = self.monitor
        if self._busy:
            self.banner.set_revealed(False)
        elif monitor.usb_access_missing:
            self.banner.set_title(
                _('Corer needs permission to access this device over USB'))
            self.banner.set_button_label(_('_Set Up'))
            self._banner_action = self._setup_usb_access
            self.banner.set_revealed(True)
        elif monitor.usbmuxd_state == usbmuxd.UNRESPONSIVE:
            self.banner.set_title(
                _('The system device service (usbmuxd) is not responding'))
            self.banner.set_button_label(_('_Restart It'))
            self._banner_action = self._restart_usbmuxd
            self.banner.set_revealed(True)
        elif monitor.usbmuxd_state == usbmuxd.UNAVAILABLE:
            self.banner.set_title(
                _('The device service (usbmuxd) is not available'))
            self.banner.set_button_label(None)
            self._banner_action = None
            self.banner.set_revealed(True)
        else:
            self.banner.set_revealed(False)

    def _update_page(self):
        if self._busy:
            page = 'progress'
        elif self._result_shown:
            return
        elif not self.monitor.scanned:
            page = 'loading'
        elif self._devices:
            page = 'device'
        else:
            page = 'empty'
        self.stack.set_visible_child_name(page)

    @Gtk.Template.Callback()
    def on_banner_button_clicked(self, *_args):
        if self._banner_action:
            self._banner_action()

    def _setup_usb_access(self):
        self.banner.set_sensitive(False)

        def done(success):
            self.banner.set_sensitive(True)
            if success:
                self._toast(_('USB access was set up'))
                self.monitor.refresh()
            else:
                self._show_manual_setup()

        host.install_udev_rules(done)

    def _restart_usbmuxd(self):
        self.banner.set_sensitive(False)

        def done(success):
            self.banner.set_sensitive(True)
            if success:
                self._toast(_('The device service was restarted'))
            else:
                self._toast(_('Could not restart it. Run “sudo systemctl '
                              'restart usbmuxd” in a terminal'))
            self.monitor.refresh()

        host.restart_host_usbmuxd(done)

    def _show_manual_setup(self):
        commands = host.udev_manual_commands()
        dialog = Adw.AlertDialog(
            heading=_('Set Up USB Access Manually'),
            body=_('The setup could not be completed. Run these commands in a '
                   'terminal to install the device access rule, then reconnect '
                   'the device.'))
        label = Gtk.Label(label=commands, selectable=True, xalign=0,
                          wrap=True,
                          wrap_mode=Pango.WrapMode.WORD_CHAR)
        label.add_css_class('monospace')
        label.add_css_class('caption')
        scrolled = Gtk.ScrolledWindow(child=label, max_content_height=200,
                                      propagate_natural_height=True)
        scrolled.add_css_class('card')
        label.set_margin_top(12)
        label.set_margin_bottom(12)
        label.set_margin_start(12)
        label.set_margin_end(12)
        dialog.set_extra_child(scrolled)
        dialog.add_response('close', _('_Close'))
        dialog.add_response('copy', _('C_opy Commands'))
        dialog.set_response_appearance('copy', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('copy')
        dialog.set_close_response('close')

        def on_response(_dialog, response):
            if response == 'copy':
                self.get_clipboard().set(commands)
                self._toast(_('Copied to clipboard'))

        dialog.connect('response', on_response)
        dialog.present(self)

    # Firmware

    def _load_firmwares(self, force=False):
        device = self._device
        self._firmwares = []
        self._firmware_offline = False
        if device is None or not device.product_type:
            self._firmware_state = 'idle'
            self._fill_firmware_row()
            return
        product = device.product_type
        if not force and product in self._firmware_cache:
            self._firmwares, self._firmware_offline = \
                self._firmware_cache[product]
            self._firmware_state = 'ready'
            self._fill_firmware_row()
            return

        self._firmware_state = 'loading'
        self._fill_firmware_row()

        def loaded(firmwares, offline, error):
            if not error and not offline:
                self._firmware_cache[product] = (firmwares, offline)
            current = self._device
            if current is None or current.product_type != product:
                return GLib.SOURCE_REMOVE
            if error:
                self._firmware_state = 'error'
                self._firmware_error = error
            else:
                self._firmware_state = 'ready'
                self._firmwares = firmwares
                self._firmware_offline = offline
            self._fill_firmware_row()
            self._update_controls()
            return GLib.SOURCE_REMOVE

        firmware.fetch_signed_async(product, loaded)

    def _fill_firmware_row(self):
        device = self._device
        os_name = device.os_name if device else ''
        self._updating = True
        self.firmware_row.set_model(Gtk.StringList.new(
            [f'{os_name} {fw.version} ({fw.build})' for fw in self._firmwares]))
        if self._firmwares:
            # Prefer firmware that is already downloaded over the newest one.
            cached = [i for i, fw in enumerate(self._firmwares) if fw.is_cached]
            self.firmware_row.set_selected(cached[0] if cached else 0)
        self._updating = False
        self._update_controls()

    def _selected_firmware(self):
        index = self.firmware_row.get_selected()
        if self._firmware_state == 'ready' and 0 <= index < len(self._firmwares):
            return self._firmwares[index]
        return None

    @Gtk.Template.Callback()
    def on_firmware_selected(self, *_args):
        if not self._updating:
            self._update_controls()

    @Gtk.Template.Callback()
    def on_firmware_retry_clicked(self, *_args):
        self._load_firmwares(force=True)

    def _local_is_compatible(self):
        device, local = self._device, self._local
        if not local or not device or not device.product_type:
            return True
        return (not local.product_types
                or device.product_type in local.product_types)

    def _local_is_signed(self):
        """False only if we know the file's build is no longer signed."""
        if (not self._local or self._firmware_state != 'ready'
                or self._firmware_offline or not self._local.build):
            return True
        return any(fw.build == self._local.build for fw in self._firmwares)

    def _update_controls(self):
        device = self._device
        state = self._firmware_state
        local = self._local
        selected = self._selected_firmware()

        # Version row
        self.firmware_spinner.set_visible(state == 'loading')
        self.firmware_retry_button.set_visible(state == 'error')
        self.firmware_row.set_sensitive(local is None and state == 'ready'
                                        and bool(self._firmwares))
        self.firmware_row.remove_css_class('error')
        if state == 'loading':
            subtitle = _('Looking for available versions…')
        elif state == 'error':
            subtitle = self._firmware_error
        elif state == 'idle':
            subtitle = _('Choose a firmware file for this device')
        elif not self._firmwares:
            subtitle = _('No firmware is currently signed for this device')
        elif local is not None:
            subtitle = _('Using the firmware file')
        elif selected is None:
            subtitle = ''
        elif selected.is_cached:
            subtitle = _('Downloaded')
        else:
            # Translators: {size} is a file size such as “8.1 GB”
            subtitle = _('{size} download').format(
                size=GLib.format_size(selected.size))
        if (self._firmware_offline and state == 'ready' and local is None
                and self._firmwares):
            subtitle = _('Offline — showing downloaded firmware')
        self.firmware_row.set_subtitle(subtitle)

        # File row
        self.file_clear_button.set_visible(local is not None)
        self.file_row.remove_css_class('error')
        if local is None:
            self.file_row.set_subtitle(_('Use an IPSW file from this computer'))
        elif not self._local_is_compatible():
            self.file_row.set_subtitle(
                # Translators: {filename} is the name of a firmware file
                _('{filename} is not compatible with this device').format(
                    filename=local.filename))
            self.file_row.add_css_class('error')
        else:
            parts = [local.filename]
            if local.version:
                parts.append(f'{local.version} ({local.build})'
                             if local.build else local.version)
            self.file_row.set_subtitle(' · '.join(parts))

        # Action button
        erase = self.erase_row.get_active()
        self.action_button.set_label(
            _('_Erase and Restore') if erase else _('_Update'))
        if erase:
            self.action_button.remove_css_class('suggested-action')
            self.action_button.add_css_class('destructive-action')
        else:
            self.action_button.remove_css_class('destructive-action')
            self.action_button.add_css_class('suggested-action')

        ready = self._can_start()
        self.action_button.set_sensitive(ready)
        self.lookup_action('save-shsh').set_enabled(ready)
        self.lookup_action('open-file').set_enabled(not self._busy)

        # Recovery mode button
        if device is not None and self._can_exit_recovery(device):
            self.recovery_button.set_label(_('Exit _Recovery Mode'))
            self.recovery_button.set_visible(True)
        elif device is not None and self._can_enter_recovery(device):
            self.recovery_button.set_label(_('Enter _Recovery Mode'))
            self.recovery_button.set_visible(True)
        else:
            self.recovery_button.set_visible(False)
        self.recovery_button.set_sensitive(
            not self._busy and not self._recovery_pending)

    @staticmethod
    def _can_exit_recovery(device):
        return device.mode == dev.MODE_RECOVERY and device.accessible

    @staticmethod
    def _can_enter_recovery(device):
        # Needs the lockdown service, which only a device that is fully
        # started and reachable through usbmuxd has.
        return device.mode == dev.MODE_NORMAL and bool(device.udid)

    def _can_start(self):
        device = self._device
        if self._busy or device is None or not device.accessible:
            return False
        if self._local is not None:
            return self._local_is_compatible()
        return self._selected_firmware() is not None

    @Gtk.Template.Callback()
    def on_erase_toggled(self, *_args):
        self._update_controls()

    def _on_open_file(self, *_args):
        ipsw_filter = Gtk.FileFilter()
        ipsw_filter.set_name(_('Firmware Files'))
        ipsw_filter.add_pattern('*.ipsw')
        ipsw_filter.add_pattern('*.IPSW')
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(ipsw_filter)
        dialog = Gtk.FileDialog(title=_('Choose Firmware File'), modal=True,
                                filters=filters, default_filter=ipsw_filter)
        dialog.open(self, None, self._on_file_chosen)

    def _on_file_chosen(self, dialog, result):
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return
        path = file.get_path() if file else None
        if not path:
            self._toast(_('This file cannot be used'))
            return

        def work():
            try:
                local = firmware.inspect_ipsw(path)
                GLib.idle_add(done, local, None)
            except firmware.FirmwareError as error:
                GLib.idle_add(done, None, str(error))

        def done(local, error):
            if error:
                self._toast(error)
            else:
                self._local = local
                self._update_controls()
            return GLib.SOURCE_REMOVE

        threading.Thread(target=work, daemon=True).start()

    @Gtk.Template.Callback()
    def on_file_clear_clicked(self, *_args):
        self._local = None
        self._update_controls()

    # Starting

    def _target_description(self, source):
        device = self._device
        if source.version:
            return f'{device.os_name} {source.version}'
        return source.filename

    @Gtk.Template.Callback()
    def on_action_clicked(self, *_args):
        if not self._can_start():
            return
        device = self._device
        source = self._local or self._selected_firmware()
        erase = self.erase_row.get_active()
        name = self._device_title(device)
        target = self._target_description(source)

        if erase:
            # Translators: {name} is the name of a device
            heading = _('Erase “{name}”?').format(name=name)
            # Translators: {version} is a system version such as “iOS 18.1”
            body = [_('All content and settings will be permanently erased and '
                      '{version} will be installed. This cannot be undone.'
                      ).format(version=target)]
        else:
            heading = _('Update “{name}”?').format(name=name)
            body = [_('{version} will be installed and your content and '
                      'settings will be kept. Make sure you have a backup '
                      'before continuing.').format(version=target)]
            if (device.version and source.version
                    and firmware.version_key(source.version)
                    < firmware.version_key(device.version)):
                body.append(_('This version is older than the one on the '
                              'device. Downgrading without erasing is likely '
                              'to fail and can cause complete data loss.'))
        if self._local is not None and not self._local_is_signed():
            body.append(_('This firmware no longer appears to be signed, so '
                          'the device will probably refuse it.'))
        body.append(_('Keep the device connected until the process finishes.'))

        dialog = Adw.AlertDialog(heading=heading, body='\n\n'.join(body))
        dialog.add_response('cancel', _('_Cancel'))
        if erase:
            dialog.add_response('start', _('_Erase and Restore'))
            dialog.set_response_appearance(
                'start', Adw.ResponseAppearance.DESTRUCTIVE)
        else:
            dialog.add_response('start', _('_Update'))
            dialog.set_response_appearance(
                'start', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('cancel')
        dialog.set_close_response('cancel')

        def on_response(_dialog, response):
            if response == 'start' and self._can_start():
                self._begin(KIND_RESTORE, source, erase)

        dialog.connect('response', on_response)
        dialog.present(self)

    def _on_save_shsh(self, *_args):
        if self._can_start():
            self._begin(KIND_SHSH, self._local or self._selected_firmware(),
                        False)

    def _begin(self, kind, source, erase):
        self._busy = True
        self._last_attempt = (kind, source, erase, self._device)
        self._kind = kind
        self._erasing = erase
        self._source = source
        self._cancelling = False
        self._result_shown = False
        self.log_buffer.set_text('')
        self.monitor.pause()
        self._open_log_file()
        self._log_session_header(kind, source, erase)

        app = self.get_application()
        if app is not None:
            self._inhibit_cookie = app.inhibit(
                self,
                Gtk.ApplicationInhibitFlags.LOGOUT
                | Gtk.ApplicationInhibitFlags.SUSPEND,
                _('A device is being restored'))

        self.cancel_button.set_sensitive(True)
        self.progress_subtitle.set_label(self._device_title(self._device))
        self.progress_bar.set_fraction(0)
        self.progress_detail.set_label('')
        self._update_controls()
        self._update_banner()
        self._update_page()

        if isinstance(source, firmware.Firmware):
            if source.is_cached:
                self._set_stage(_('Verifying Firmware'), ICON_VERIFY)
            else:
                self._set_stage(_('Downloading Firmware'), ICON_DOWNLOAD)
            self._download = firmware.Download(
                source, self._on_download_progress, self._on_download_finished)
            self._download.start()
        else:
            self._run_restore(source.path)

    def _set_stage(self, title, icon_name):
        self.progress_title.set_label(title)
        self.progress_icon.set_from_icon_name(icon_name)

    def _set_step(self, step):
        icon_name = STEP_ICONS[step] if 0 <= step < len(STEP_ICONS) else None
        if icon_name is None:
            icon_name = (self._device_icon(self._device) if self._device
                         else 'corer-iphone-symbolic')
        self._set_stage(step_label(step), icon_name)

    # Download

    def _on_download_progress(self, phase, done, total, speed):
        if self._download is None or self._cancelling:
            return GLib.SOURCE_REMOVE
        if phase == 'verify':
            self._set_stage(_('Verifying Firmware'), ICON_VERIFY)
        else:
            self._set_stage(_('Downloading Firmware'), ICON_DOWNLOAD)
        if total > 0:
            self.progress_bar.set_fraction(min(done / total, 1.0))
            # Translators: download progress, e.g. “1.2 GB of 8.1 GB”
            detail = _('{done} of {total}').format(
                done=GLib.format_size(done), total=GLib.format_size(total))
        else:
            self.progress_bar.pulse()
            detail = GLib.format_size(done)
        if phase == 'download' and speed > 0:
            # Translators: download speed, e.g. “12.5 MB/s”
            detail += ' · ' + _('{speed}/s').format(
                speed=GLib.format_size(int(speed)))
        self.progress_detail.set_label(detail)
        return GLib.SOURCE_REMOVE

    def _on_download_finished(self, path, error, cancelled):
        self._download = None
        if cancelled:
            self._append_log('Download cancelled')
            self._finish(False, True, '')
        elif error:
            self._append_log(f'Download failed: {error}')
            self._finish(False, False, error)
        else:
            self._append_log(f'Firmware ready: {path}')
            self._run_restore(path)
        return GLib.SOURCE_REMOVE

    # Restore

    def _run_restore(self, path):
        self._set_step(0)
        self.progress_bar.set_fraction(0)
        self.progress_detail.set_label('')
        argv = build_argv(self.settings, self._device, path,
                          erase=self._erasing,
                          shsh_only=self._kind == KIND_SHSH,
                          logfile=self._detail_log_path())
        self._runner = RestoreRunner(argv)
        self._runner.connect('progress', self._on_restore_progress)
        self._runner.connect('log', lambda _runner, line: self._append_log(line))
        self._runner.connect('finished', self._on_restore_finished)
        self._runner.start()

    def _on_restore_progress(self, _runner, step, fraction):
        if self._cancelling:
            return
        self._set_step(step)
        self.progress_bar.set_fraction(fraction)
        self.progress_detail.set_label(f'{round(fraction * 100)} %')

    def _on_restore_finished(self, _runner, success, cancelled, message):
        self._runner = None
        self._finish(success, cancelled, message)

    def _append_log(self, line):
        self.log_buffer.insert(self.log_buffer.get_end_iter(), line + '\n')
        if self._log_file is not None:
            try:
                # Flushed per line so the log survives a crash.
                self._log_file.write(time.strftime('%H:%M:%S ') + line + '\n')
                self._log_file.flush()
            except OSError:
                self._log_file = None

    def _open_log_file(self):
        self._close_log_file()
        try:
            self._log_file = open(new_log_path(), 'w', encoding='utf-8')
        except OSError:
            self._log_file = None

    def _detail_log_path(self):
        """Where idevicerestore writes its own, complete log."""
        if self._log_file is None:
            return None
        return self._log_file.name[:-len('.log')] + '.idevicerestore.log'

    def _close_log_file(self):
        if self._log_file is not None:
            try:
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None

    def _log_session_header(self, kind, source, erase):
        device = self._device
        _status, tool = host.run(['idevicerestore', '--version'], timeout=5)
        lines = [
            f'Corer {VERSION} ({APP_ID}), sandboxed: {host.in_flatpak()}',
            tool.strip() or 'idevicerestore: version unknown',
            f'usbmuxd: {self.monitor.usbmuxd_state}, socket: '
            + os.environ.get(usbmuxd.ENV_VAR, 'default'),
            f'Device: {device}',
            f'Operation: {kind}, erase: {erase}',
            f'Source: {source}',
            'Settings: ' + ', '.join(
                f'{key}={self.settings.get_value(key).print_(False)}'
                for key in ('debug', 'allow-restore-mode', 'ignore-errors',
                            'exclude-baseband', 'keep-personalized',
                            'custom-firmware', 'no-restore', 'signing-server',
                            'variant', 'keep-firmware')),
        ]
        if self._log_file is not None:
            lines.append(f'Log file: {self._log_file.name}')
            lines.append(f'Detailed idevicerestore log: {self._detail_log_path()}')
        for line in lines:
            self._append_log(line)

    @Gtk.Template.Callback()
    def on_cancel_clicked(self, *_args):
        if self._runner is None:
            self._cancel()
            return
        dialog = Adw.AlertDialog(
            heading=_('Stop the Process?'),
            body=_('Interrupting now can leave the device unusable until it '
                   'is restored again.'))
        dialog.add_response('continue', _('_Continue'))
        dialog.add_response('stop', _('_Stop'))
        dialog.set_response_appearance('stop',
                                       Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('continue')
        dialog.set_close_response('continue')

        def on_response(_dialog, response):
            if response == 'stop':
                self._cancel()

        dialog.connect('response', on_response)
        dialog.present(self)

    def _cancel(self):
        if not self._busy or self._cancelling:
            return
        self._cancelling = True
        self.cancel_button.set_sensitive(False)
        self._set_stage(_('Stopping…'), ICON_STOP)
        self.progress_detail.set_label('')
        if self._download is not None:
            self._download.cancel()
        if self._runner is not None:
            self._runner.cancel()

    def _finish(self, success, cancelled, message):
        kind = self._kind
        source = self._source
        self._append_log(f'Result: success={success}, cancelled={cancelled}')
        self._close_log_file()
        self._busy = False
        self._download = None
        self._source = None

        app = self.get_application()
        if self._inhibit_cookie and app is not None:
            app.uninhibit(self._inhibit_cookie)
        self._inhibit_cookie = 0

        if (success and kind == KIND_RESTORE
                and isinstance(source, firmware.Firmware)
                and not self.settings.get_boolean('keep-firmware')):
            firmware.remove_cached(source)

        if self._close_when_done:
            self.monitor.resume()
            self.destroy()
            return

        if cancelled:
            self._toast(_('Stopped'))
        elif success:
            if kind == KIND_SHSH:
                title = _('SHSH Blobs Saved')
                description = os.path.join(firmware.cache_dir(), 'shsh')
            elif self._erasing:
                title = _('Device Restored')
                description = _('The device will restart and show the setup '
                                'assistant.')
            else:
                title = _('Device Updated')
                description = _('The device will restart when it has finished '
                                'updating.')
            self.done_page.set_title(title)
            self.done_page.set_description(description)
            self._result_shown = True
            self.stack.set_visible_child_name('done')
            self._notify(title)
        else:
            title = (_('Could Not Save SHSH Blobs') if kind == KIND_SHSH
                     else _('Restore Failed'))
            self.error_page.set_title(title)
            self.error_page.set_description(
                GLib.markup_escape_text(message) if message
                else _('Check the log for details.'))
            self._result_shown = True
            self.stack.set_visible_child_name('error')
            self._notify(title)

        self._update_controls()
        self._update_banner()
        self._update_page()
        self.monitor.resume()

    def _notify(self, title):
        app = self.get_application()
        if app is None or self.is_active():
            return
        notification = Gio.Notification.new(title)
        notification.set_body(self.progress_subtitle.get_label())
        app.send_notification('finished', notification)

    @Gtk.Template.Callback()
    def on_retry_clicked(self, *_args):
        """Run the failed operation again on the same device.

        The device may have changed mode in the meantime, for example when a
        restore failed right after it entered recovery mode.
        """
        if self._busy or self._last_attempt is None:
            return
        kind, source, erase, previous = self._last_attempt
        match = None
        for device in self.monitor.devices:
            if device.key == previous.key or (
                    previous.ecid and device.ecid == previous.ecid):
                match = device
                break
        if match is None or not match.accessible:
            self.monitor.refresh()
            self._toast(_('The device was not found. Reconnect it and try '
                          'again'))
            return
        self._devices = list(self.monitor.devices)
        self._device = match
        self._begin(kind, source, erase)

    @Gtk.Template.Callback()
    def on_result_dismissed(self, *_args):
        self._result_shown = False
        self._on_devices_changed(self.monitor)

    # Other actions

    def _on_show_log(self, *_args):
        LogDialog(self.log_buffer).present(self)

    @Gtk.Template.Callback()
    def on_recovery_clicked(self, *_args):
        device = self._device
        if self._busy or self._recovery_pending or device is None:
            return
        if self._can_exit_recovery(device):
            argv = ['irecovery', '-n']
            if device.ecid:
                argv += ['-i', f'0x{device.ecid:x}']
            started = _('The device is restarting')
            failed = _('Could not exit recovery mode')
        elif self._can_enter_recovery(device):
            argv = ['ideviceenterrecovery', device.udid]
            started = _('The device is restarting in recovery mode')
            failed = _('Could not enter recovery mode. Unlock the device and '
                       'trust this computer')
        else:
            return

        def work():
            status, _out = host.run(argv, timeout=30)
            GLib.idle_add(done, status == 0)

        def done(success):
            self._recovery_pending = False
            self._toast(started if success else failed)
            self._update_controls()
            self.monitor.refresh()
            return GLib.SOURCE_REMOVE

        self._recovery_pending = True
        self._update_controls()
        threading.Thread(target=work, daemon=True).start()

    def _on_close_request(self, *_args):
        if not self._busy:
            return False
        if self._runner is None:
            # Only downloading: safe to stop, the download resumes next time.
            self._close_when_done = True
            self._cancel()
            return True
        dialog = Adw.AlertDialog(
            heading=_('Stop and Quit?'),
            body=_('A device is being restored. Interrupting now can leave it '
                   'unusable until it is restored again.'))
        dialog.add_response('continue', _('_Continue'))
        dialog.add_response('stop', _('_Stop and Quit'))
        dialog.set_response_appearance('stop',
                                       Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('continue')
        dialog.set_close_response('continue')

        def on_response(_dialog, response):
            if response == 'stop':
                if self._busy:
                    self._close_when_done = True
                    self._cancel()
                else:
                    self.close()

        dialog.connect('response', on_response)
        dialog.present(self)
        return True
