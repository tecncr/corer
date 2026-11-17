# preferences.py
#
# Copyright 2026 Nícolas Castillo
#
# SPDX-License-Identifier: GPL-3.0-or-later

import threading
from gettext import gettext as _

from gi.repository import Adw, Gio, GLib, Gtk

from . import firmware
from .restore import logs_dir


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, settings):
        super().__init__(content_width=720, content_height=600)
        self._settings = settings

        # Firmware

        page = Adw.PreferencesPage(title=_('Firmware'),
                                   icon_name='corer-folder-download-symbolic')
        self.add(page)

        group = Adw.PreferencesGroup(
            title=_('Downloads'),
            description=_('Downloaded firmware is stored so that it does not '
                          'have to be downloaded again.'))
        page.add(group)
        group.add(self._switch(
            'keep-firmware', _('_Keep Downloaded Firmware'),
            _('Turn off to delete firmware after a successful restore')))

        self._cache_row = Adw.ActionRow(title=_('Stored Firmware'),
                                        subtitle=_('Calculating…'))
        open_button = Gtk.Button(icon_name='corer-folder-open-symbolic',
                                 tooltip_text=_('Open Folder'),
                                 valign=Gtk.Align.CENTER)
        open_button.add_css_class('flat')
        open_button.connect('clicked', self._on_open_cache)
        self._cache_row.add_suffix(open_button)
        self._clear_button = Gtk.Button(label=_('_Delete'), use_underline=True,
                                        valign=Gtk.Align.CENTER,
                                        sensitive=False)
        self._clear_button.add_css_class('destructive-action')
        self._clear_button.connect('clicked', self._on_clear_cache)
        self._cache_row.add_suffix(self._clear_button)
        group.add(self._cache_row)

        # Advanced

        page = Adw.PreferencesPage(title=_('Advanced'),
                                   icon_name='corer-applications-engineering-symbolic')
        self.add(page)

        group = Adw.PreferencesGroup(
            title=_('Restore'),
            description=_('These options can leave a device unable to start. '
                          'Change them only if you know what they do.'))
        page.add(group)
        group.add(self._switch(
            'allow-restore-mode', _('Restore From _Restore Mode'),
            _('Allow starting with a device that is already in restore mode')))
        group.add(self._switch(
            'ignore-errors', _('_Ignore Errors'),
            _('Try to continue after certain errors, like a failed baseband '
              'update')))
        group.add(self._switch(
            'exclude-baseband', _('Skip _Baseband'),
            _('Do not upgrade NOR and baseband on legacy devices')))
        group.add(self._switch(
            'no-restore', _('_Stop After Ramdisk'),
            _('Boot the restore ramdisk without restoring')))

        group = Adw.PreferencesGroup(title=_('Firmware'))
        page.add(group)
        group.add(self._switch(
            'custom-firmware', _('_Custom Firmware'),
            _('Restore custom firmware. Requires a bootrom exploit')))
        group.add(self._entry('variant', _('Restore _Variant')))
        group.add(self._entry('signing-server', _('Signing Server _URL'),
                              Gtk.InputPurpose.URL))

        group = Adw.PreferencesGroup(title=_('Troubleshooting'))
        page.add(group)
        group.add(self._switch(
            'debug', _('_Detailed Log'),
            _('Include all debugging output from idevicerestore in the log')))
        logs_row = Adw.ActionRow(
            title=_('Log Files'),
            subtitle=_('Every restore is recorded, even if Corer quits '
                       'unexpectedly'))
        logs_button = Gtk.Button(icon_name='corer-folder-open-symbolic',
                                 tooltip_text=_('Open Folder'),
                                 valign=Gtk.Align.CENTER)
        logs_button.add_css_class('flat')
        logs_button.connect('clicked', self._on_open_logs)
        logs_row.add_suffix(logs_button)
        group.add(logs_row)
        group.add(self._switch(
            'keep-personalized', _('Keep _Personalized Components'),
            _('Write personalized components to files for debugging')))

        self._refresh_cache_size()

    def _switch(self, key, title, subtitle):
        row = Adw.SwitchRow(title=title, subtitle=subtitle, use_underline=True)
        self._settings.bind(key, row, 'active', Gio.SettingsBindFlags.DEFAULT)
        return row

    def _entry(self, key, title, purpose=Gtk.InputPurpose.FREE_FORM):
        row = Adw.EntryRow(title=title, use_underline=True,
                           input_purpose=purpose)
        self._settings.bind(key, row, 'text', Gio.SettingsBindFlags.DEFAULT)
        return row

    def _refresh_cache_size(self):
        def work():
            GLib.idle_add(done, firmware.cache_size())

        def done(size):
            self._cache_row.set_subtitle(GLib.format_size(size))
            self._clear_button.set_sensitive(size > 0)
            return GLib.SOURCE_REMOVE

        threading.Thread(target=work, daemon=True).start()

    def _on_open_cache(self, *_args):
        launcher = Gtk.FileLauncher.new(
            Gio.File.new_for_path(firmware.cache_dir()))
        launcher.launch(self.get_root(), None, None)

    def _on_open_logs(self, *_args):
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(logs_dir()))
        launcher.launch(self.get_root(), None, None)

    def _on_clear_cache(self, *_args):
        dialog = Adw.AlertDialog(
            heading=_('Delete Stored Firmware?'),
            body=_('Firmware will be downloaded again the next time it is '
                   'needed.'))
        dialog.add_response('cancel', _('_Cancel'))
        dialog.add_response('delete', _('_Delete'))
        dialog.set_response_appearance('delete',
                                       Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('cancel')
        dialog.set_close_response('cancel')

        def on_response(_dialog, response):
            if response != 'delete':
                return
            self._clear_button.set_sensitive(False)

            def work():
                firmware.clear_cache()
                GLib.idle_add(self._refresh_cache_size)

            threading.Thread(target=work, daemon=True).start()

        dialog.connect('response', on_response)
        dialog.present(self)
