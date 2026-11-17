id_conf = configuration_data()
id_conf.set('APP_ID', application_id)
id_conf.set('bindir', get_option('prefix') / get_option('bindir'))

desktop_file = i18n.merge_file(
        input: configure_file(
                  input: 'com.tecncr.Corer.desktop.in.in',
                 output: '@0@.desktop.in'.format(application_id),
          configuration: id_conf,
        ),
       output: '@0@.desktop'.format(application_id),
         type: 'desktop',
       po_dir: '../po',
      install: true,
  install_dir: get_option('datadir') / 'applications'
)

desktop_utils = find_program('desktop-file-validate', required: false)
if desktop_utils.found()
  test('Validate desktop file', desktop_utils, args: [desktop_file])
endif

appstream_file = i18n.merge_file(
        input: configure_file(
                  input: 'com.tecncr.Corer.metainfo.xml.in.in',
                 output: '@0@.metainfo.xml.in'.format(application_id),
          configuration: id_conf,
        ),
       output: '@0@.metainfo.xml'.format(application_id),
       po_dir: '../po',
      install: true,
  install_dir: get_option('datadir') / 'metainfo'
)

appstreamcli = find_program('appstreamcli', required: false, disabler: true)
test('Validate appstream file', appstreamcli,
     args: ['validate', '--no-net', '--explain', appstream_file])

install_data('com.tecncr.Corer.gschema.xml',
  install_dir: get_option('datadir') / 'glib-2.0' / 'schemas'
)

compile_schemas = find_program('glib-compile-schemas', required: false, disabler: true)
test('Validate schema file',
     compile_schemas,
     args: ['--strict', '--dry-run', meson.current_source_dir()])

configure_file(
          input: 'com.tecncr.Corer.service.in',
         output: '@0@.service'.format(application_id),
  configuration: id_conf,
    install_dir: get_option('datadir') / 'dbus-1' / 'services'
)

# Shipped for packagers. The application also carries a copy in its resources
# so it can install the rule on the host from inside the Flatpak sandbox.
install_data('71-corer-apple-devices.rules',
  install_dir: pkgdatadir
)

subdir('icons')
