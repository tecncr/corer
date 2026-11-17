# log_dialog.py
#
# Copyright 2026 Nícolas Castillo
#
# SPDX-License-Identifier: GPL-3.0-or-later

from gettext import gettext as _

from gi.repository import Adw, Gio, GLib, Gtk


class LogDialog(Adw.Dialog):
    """Shows the output of the current or last operation."""

    def __init__(self, buffer):
        super().__init__(title=_('Log'), content_width=800, content_height=560)
        self._buffer = buffer

        self._toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()

        copy_button = Gtk.Button(icon_name='corer-edit-copy-symbolic',
                                 tooltip_text=_('Copy'))
        copy_button.connect('clicked', self._on_copy)
        header.pack_start(copy_button)
        save_button = Gtk.Button(icon_name='corer-document-save-symbolic',
                                 tooltip_text=_('Save…'))
        save_button.connect('clicked', self._on_save)
        header.pack_start(save_button)
        view.add_top_bar(header)

        self._stack = Gtk.Stack()
        self._stack.add_named(Adw.StatusPage(
            icon_name='corer-dialog-information-symbolic',
            title=_('No Log Yet'),
            description=_('Output appears here when a device is restored.')),
            'empty')

        self._text = Gtk.TextView(buffer=buffer, editable=False,
                                  cursor_visible=False, monospace=True,
                                  wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                  top_margin=12, bottom_margin=12,
                                  left_margin=12, right_margin=12)
        self._scrolled = Gtk.ScrolledWindow(child=self._text)
        self._stack.add_named(self._scrolled, 'log')
        view.set_content(self._stack)
        self._toasts.set_child(view)
        self.set_child(self._toasts)

        self._buttons = (copy_button, save_button)
        self._changed_handler = buffer.connect('changed', self._on_changed)
        self.connect('closed', self._on_closed)
        self._on_changed(buffer)

    def _on_closed(self, *_args):
        self._buffer.disconnect(self._changed_handler)

    def _get_text(self):
        return self._buffer.get_text(self._buffer.get_start_iter(),
                                     self._buffer.get_end_iter(), False)

    def _on_changed(self, buffer):
        has_text = buffer.get_char_count() > 0
        self._stack.set_visible_child_name('log' if has_text else 'empty')
        for button in self._buttons:
            button.set_sensitive(has_text)
        adjustment = self._scrolled.get_vadjustment()
        at_end = (adjustment.get_value() + adjustment.get_page_size()
                  >= adjustment.get_upper() - 48)
        if at_end:
            GLib.idle_add(self._scroll_to_end)

    def _scroll_to_end(self):
        adjustment = self._scrolled.get_vadjustment()
        adjustment.set_value(adjustment.get_upper())
        return GLib.SOURCE_REMOVE

    def _on_copy(self, *_args):
        self.get_clipboard().set(self._get_text())
        self._toasts.add_toast(Adw.Toast.new(_('Copied to clipboard')))

    def _on_save(self, *_args):
        dialog = Gtk.FileDialog(title=_('Save Log'), modal=True,
                                initial_name='corer.log')
        dialog.save(self.get_root(), None, self._on_save_chosen)

    def _on_save_chosen(self, dialog, result):
        try:
            file = dialog.save_finish(result)
        except GLib.Error:
            return
        try:
            file.replace_contents(self._get_text().encode('utf-8'), None,
                                  False, Gio.FileCreateFlags.REPLACE_DESTINATION,
                                  None)
        except GLib.Error as error:
            self._toasts.add_toast(Adw.Toast.new(error.message))
