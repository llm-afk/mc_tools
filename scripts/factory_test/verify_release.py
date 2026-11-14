"""Verify the workstation executable, sources and runtime dependencies."""
import argparse
import json
from pathlib import Path

from .version import VERSION

REQUIRED_CHECKS = (
    'ok', 'frozen', 'version_display', 'debug_monitor_on_connect', 'debug_settings_accessible',
    'adapter_single_auto_connect', 'mode_handoff_async', 'debug_auto_connect', 'gui_event_processing_bounded',
    'waveform_retained', 'waveform_time_axis_stable', 'waveform_target_solid', 'slider_wheel',
    'hardware_revision_entries', 'revision_whitelist_removed', 'debug_enable_without_sn_whitelist',
    'firmware_decimal', 'legacy_config_field_removed', 'live_targets_default', 'send_controls_removed',
    'position_wheel_step_0_1', 'waveform_thin', 'waveform_60fps', 'printer_named_default',
    'environment_summary', 'diagnostic_copy_redacted', 'single_instance_lock', 'configuration_events_fenced',
    'shutdown_waits_for_workers', 'printer_service_ui', 'printer_service_lifecycle', 'boot_log_export',
    'firmware_first_step', 'firmware_folder_auto', 'operator_manual',
    'startup_log_visible', 'network_state_local_only', 'log_scroll_preserved', 'operator_action_prompts', 'production_layout_stable',
)


def verify(release):
    from .build_release import ROOT, digest, source_files, firmware_record

    release = Path(release).resolve()
    manifest = json.loads((release/'release-manifest.json').read_bytes())
    assert manifest['version'] == VERSION and manifest['channel'] == 'production'
    check = manifest['isolated_self_check']
    assert all(check.get(name) is True for name in REQUIRED_CHECKS)
    assert check['version'] == VERSION and check['simulated_steps'] == 11
    assert check['usb_open_attempts'] == 0 and check['hardware_or_network_used'] is False
    assert manifest['printer_renderer_check']['ok'] is True
    assert manifest['printer_raw_helper_compile_check'] == dict(ok=True, hardwareUsed=False)
    assert manifest['printer_tests']['ok'] is True and manifest['printer_tests']['tests'] > 0
    proof = manifest['regression']
    sources = {p.relative_to(ROOT).as_posix(): digest(p) for p in source_files()}
    assert proof['ok'] is True and proof['version'] == VERSION and proof['hardware_or_network_used'] is False
    assert proof['sources'] == sources == manifest['source_files']
    assert digest(release/'电机产测软件.exe') == manifest['sha256']
    for name, expected in manifest['files'].items():
        assert digest(release/name) == expected, name
    assert all(manifest['files'][name] == expected for name, expected in sources.items())
    current_app = firmware_record(release)
    return dict(ok=True, version=VERSION, executable=str(release/'电机产测软件.exe'),
                files=len(manifest['files']), source_files=len(sources), guarded_tests=proof['guarded_tests'],
                printer_tests=manifest['printer_tests']['tests'],
                source_matches_release=True, hardware_or_network_used=False,
                firmware_version=current_app['version'], firmware_file=current_app['file'],
                firmware_matches_build=current_app == manifest['firmware_app'])


def main():
    from .build_release import ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path, default=ROOT)
    args = parser.parse_args()
    print(json.dumps(verify(args.release), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
