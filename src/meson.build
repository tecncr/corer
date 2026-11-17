moduledir = pkgdatadir / 'corer'

gnome.compile_resources('corer',
  'corer.gresource.xml',
  gresource_bundle: true,
  install: true,
  install_dir: pkgdatadir,
)

conf = configuration_data()
conf.set('PYTHON', py_installation.full_path())
conf.set('VERSION', meson.project_version())
conf.set('APP_ID', application_id)
conf.set('PROFILE', profile)
conf.set('localedir', get_option('prefix') / get_option('localedir'))
conf.set('pkgdatadir', pkgdatadir)

configure_file(
  input: 'corer.in',
  output: 'corer',
  configuration: conf,
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x'
)

configure_file(
  input: 'const.py.in',
  output: 'const.py',
  configuration: conf,
  install: true,
  install_dir: moduledir,
)

corer_sources = [
  '__init__.py',
  'devices.py',
  'firmware.py',
  'host.py',
  'log_dialog.py',
  'main.py',
  'preferences.py',
  'restore.py',
  'usbmuxd.py',
  'usbmuxd_relay.py',
  'window.py',
]

install_data(corer_sources, install_dir: moduledir)
