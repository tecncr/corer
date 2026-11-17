# main.py
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

import sys
from gettext import gettext as _

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Adw, Gio, GLib, Gtk

from .const import APP_ID, RESOURCE_PATH, SETTINGS_ID, VERSION
from .devices import DeviceMonitor
from .host import run
from .preferences import PreferencesDialog
from .window import CorerWindow


class CorerApplication(Adw.Application):
    """The main application singleton class."""

    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
                         resource_base_path=RESOURCE_PATH)
        self.settings = None
        self.monitor = None

        self.create_action('quit', self.on_quit_action, ['<primary>q'])
        self.create_action('about', self.on_about_action)
        self.create_action('preferences', self.on_preferences_action,
                           ['<primary>comma'])
        self.set_accels_for_action('win.open-file', ['<primary>o'])
        self.set_accels_for_action('win.show-log', ['<primary>l'])
        self.set_accels_for_action('win.refresh', ['<primary>r', 'F5'])

    def do_startup(self):
        Adw.Application.do_startup(self)
        self.settings = Gio.Settings.new(SETTINGS_ID)
        self.monitor = DeviceMonitor()
        self.monitor.start()

    def do_shutdown(self):
        if self.monitor is not None:
            self.monitor.stop()
        Adw.Application.do_shutdown(self)

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = CorerWindow(self.settings, self.monitor, application=self)
        win.present()

    def on_quit_action(self, *_args):
        # Go through the window so a running restore is not interrupted
        # without asking.
        win = self.props.active_window
        if win:
            win.close()
        else:
            self.quit()

    def on_about_action(self, *_args):
        about = Adw.AboutDialog(
            application_name='Corer',
            application_icon=APP_ID,
            developer_name='Nícolas Castillo',
            version=VERSION,
            website='https://github.com/tecncr/corer',
            issue_url='https://github.com/tecncr/corer/issues',
            # Translators: Replace "translator-credits" with your name/username, and optionally an email or URL.
            translator_credits=_('translator-credits'),
            developers=['Nícolas Castillo'],
            copyright='© 2026 Nícolas Castillo',
            license_type=Gtk.License.GPL_3_0,
        )
        about.add_acknowledgement_section(
            _('Powered By'),
            ['idevicerestore https://github.com/libimobiledevice/idevicerestore',
             'libimobiledevice https://libimobiledevice.org'])
        status, out = run(['idevicerestore', '--version'], timeout=5)
        details = [f'Corer {VERSION} ({APP_ID})']
        if status == 0 and out.strip():
            details.append(out.strip())
        about.set_debug_info('\n'.join(details))
        about.set_debug_info_filename('corer-debug-info.txt')
        about.present(self.props.active_window)

    def on_preferences_action(self, *_args):
        PreferencesDialog(self.settings).present(self.props.active_window)

    def create_action(self, name, callback, shortcuts=None):
        action = Gio.SimpleAction.new(name, None)
        action.connect('activate', callback)
        self.add_action(action)
        if shortcuts:
            self.set_accels_for_action(f'app.{name}', shortcuts)


def main(version):
    """The application's entry point."""
    GLib.set_application_name('Corer')
    app = CorerApplication()
    return app.run(sys.argv)
