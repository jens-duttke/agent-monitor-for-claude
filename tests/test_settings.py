"""Tests for the settings: reading order, validation, the settings dialog's description and saving."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import settings
from agent_monitor_for_claude.settings import Settings

_LANGUAGES = {'de', 'en', 'pt-BR'}


class _Locations:
    """Temporary stand-ins for every place a settings file is looked for."""

    def __init__(self, root: Path) -> None:
        self.app = root / 'app'
        self.config = root / 'config' / 'AgentMonitorForClaude'
        self.home = root / 'home'
        self.custom = root / 'custom-claude'
        self.app.mkdir()
        (self.home / '.claude').mkdir(parents=True)

    @property
    def portable_file(self) -> Path:
        return self.app / settings.SETTINGS_FILENAME

    @property
    def config_file(self) -> Path:
        return self.config / settings.SETTINGS_FILENAME

    @property
    def home_file(self) -> Path:
        return self.home / '.claude' / settings.SETTINGS_FILENAME

    @property
    def custom_file(self) -> Path:
        return self.custom / settings.SETTINGS_FILENAME


def _write(path: Path, data: object, encoding: str = 'utf-8') -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding=encoding)


class _SettingsTestCase(unittest.TestCase):
    """Points every location at a temp dir and gives each test its own store."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.places = _Locations(Path(temp.name))
        environment = {key: value for key, value in os.environ.items() if key != 'CLAUDE_CONFIG_DIR'}
        for patcher in (
            mock.patch.object(settings, '_app_dir', return_value=self.places.app),
            mock.patch.object(settings, 'user_config_dir', return_value=self.places.config),
            mock.patch.object(settings.Path, 'home', return_value=self.places.home),
            mock.patch.dict(os.environ, environment, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def load(self) -> settings._Loaded:
        """Read the files as at start and put the result in force for this test."""
        loaded = settings._load()
        patcher = mock.patch.object(settings, '_store', settings._Store(loaded))
        patcher.start()
        self.addCleanup(patcher.stop)
        return loaded


class ValidateTest(unittest.TestCase):
    def test_unknown_key_is_reported_and_dropped(self) -> None:
        # A misspelled key (the most common hand-edit mistake) must be reported,
        # like a type error on a known key - not silently ignored.
        valid, errors = settings._validate({'pollinterval': 3, 'poll_interval': 5})

        self.assertEqual(valid, {'poll_interval': 5})
        self.assertEqual(errors, [{'key': 'pollinterval', 'code': 'unknown'}])

    def test_known_valid_keys_pass_untouched(self) -> None:
        data = {'poll_interval': 5, 'language': 'de', 'include_completed': True, 'wsl': False, 'window_width': 900}
        valid, errors = settings._validate(dict(data))

        self.assertEqual(valid, data)
        self.assertEqual(errors, [])

    def test_a_bool_key_refuses_anything_but_a_bool(self) -> None:
        valid, errors = settings._validate({'wsl': 'yes', 'include_completed': 1})

        self.assertEqual(valid, {})
        self.assertEqual({error['key'] for error in errors}, {'wsl', 'include_completed'})
        self.assertTrue(all(error['code'] == 'type' for error in errors))

    def test_a_number_key_refuses_a_bool_a_float_and_a_string(self) -> None:
        for value in (True, 5.0, '5', None):
            with self.subTest(value=value):
                valid, errors = settings._validate({'poll_interval': value})
                self.assertEqual(valid, {})
                self.assertEqual(errors, [{'key': 'poll_interval', 'code': 'type', 'expected': 'integer'}])

    def test_bounds_are_reported_with_the_limit(self) -> None:
        _, low = settings._validate({'poll_interval': 0})
        _, high = settings._validate({'poll_interval': 3601})
        _, zero = settings._validate({'ended_max_age': 0})

        self.assertEqual(low, [{'key': 'poll_interval', 'code': 'minimum', 'limit': 1}])
        self.assertEqual(high, [{'key': 'poll_interval', 'code': 'maximum', 'limit': 3600}])
        self.assertEqual(zero, [])

    def test_a_language_must_look_like_a_locale_code(self) -> None:
        for good in ('', 'de', 'pt-BR', 'zh-CN'):
            self.assertEqual(settings._validate({'language': good})[1], [], good)
        for bad in ('../en', 'de_DE.UTF-8', 'x', 7):
            self.assertNotEqual(settings._validate({'language': bad})[1], [], bad)


class ReadingOrderTest(_SettingsTestCase):
    def test_no_file_means_the_defaults(self) -> None:
        loaded = self.load()

        self.assertEqual(loaded.settings, Settings())
        self.assertEqual(loaded.source, 'none')
        self.assertIsNone(loaded.path)

    def test_the_file_next_to_the_executable_wins(self) -> None:
        _write(self.places.portable_file, {'poll_interval': 2})
        _write(self.places.config_file, {'poll_interval': 3})

        loaded = self.load()

        self.assertEqual(loaded.settings.poll_interval, 2)
        self.assertEqual(loaded.source, 'portable')

    def test_the_configuration_directory_comes_before_the_earlier_locations(self) -> None:
        _write(self.places.config_file, {'poll_interval': 3})
        _write(self.places.home_file, {'poll_interval': 4})

        loaded = self.load()

        self.assertEqual(loaded.settings.poll_interval, 3)
        self.assertEqual(loaded.source, 'config')

    def test_an_earlier_location_is_still_read(self) -> None:
        _write(self.places.home_file, {'poll_interval': 4})

        loaded = self.load()

        self.assertEqual(loaded.settings.poll_interval, 4)
        self.assertEqual(loaded.source, 'legacy')
        self.assertEqual(loaded.path, self.places.home_file)

    def test_a_custom_claude_config_dir_comes_before_the_home_one(self) -> None:
        _write(self.places.custom_file, {'poll_interval': 6})
        _write(self.places.home_file, {'poll_interval': 4})

        with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.places.custom)}):
            loaded = self.load()

        self.assertEqual(loaded.settings.poll_interval, 6)

    def test_a_byte_order_mark_is_tolerated(self) -> None:
        _write(self.places.config_file, '{"poll_interval": 9}', encoding='utf-8-sig')

        self.assertEqual(self.load().settings.poll_interval, 9)

    def test_an_invalid_entry_falls_back_alone(self) -> None:
        _write(self.places.config_file, {'poll_interval': 'fast', 'ended_max_age': 60, 'speling': 1})

        loaded = self.load()

        self.assertEqual(loaded.settings.poll_interval, Settings().poll_interval)
        self.assertEqual(loaded.settings.ended_max_age, 60)
        self.assertEqual({error['key'] for error in loaded.errors}, {'poll_interval', 'speling'})

    def test_malformed_json_is_reported_and_yields_the_defaults(self) -> None:
        _write(self.places.config_file, '{"poll_interval": ')

        loaded = self.load()

        self.assertEqual(loaded.settings, Settings())
        self.assertEqual([error['code'] for error in loaded.errors], ['invalid_json'])

    def test_a_json_value_that_is_no_object_is_reported(self) -> None:
        _write(self.places.config_file, [1, 2])

        self.assertEqual([error['code'] for error in self.load().errors], ['not_object'])

    def test_errors_at_start_are_shown_in_a_dialog_naming_the_file(self) -> None:
        _write(self.places.config_file, {'pollinterval': 3})
        loaded = settings._load()

        with mock.patch.object(settings, 'show_error_box') as dialog:
            settings._report(loaded)

        dialog.assert_called_once()
        self.assertIn('pollinterval', dialog.call_args[0][0])
        self.assertIn(str(self.places.config_file), dialog.call_args[0][0])

    def test_a_clean_file_shows_no_dialog(self) -> None:
        _write(self.places.config_file, {'_comment': 'notes', 'poll_interval': 5})
        loaded = settings._load()

        with mock.patch.object(settings, 'show_error_box') as dialog:
            settings._report(loaded)

        dialog.assert_not_called()


class DescribeTest(_SettingsTestCase):
    def test_describes_values_defaults_and_the_file_a_save_writes(self) -> None:
        _write(self.places.config_file, {'ended_max_age': 60})
        self.load()

        description = settings.describe_settings()

        self.assertEqual(description['values']['ended_max_age'], 60)
        self.assertEqual(description['defaults']['ended_max_age'], Settings().ended_max_age)
        self.assertEqual(description['path'], str(self.places.config_file))
        self.assertTrue(description['exists'])
        self.assertEqual(description['source'], 'config')
        self.assertEqual(description['legacy_path'], '')
        self.assertEqual([spec['key'] for spec in description['specs']][0], 'language')

    def test_an_earlier_location_is_named_and_the_save_target_is_the_configuration_directory(self) -> None:
        _write(self.places.home_file, {'poll_interval': 4})
        self.load()

        description = settings.describe_settings()

        self.assertEqual(description['legacy_path'], str(self.places.home_file))
        self.assertEqual(description['path'], str(self.places.config_file))
        self.assertFalse(description['exists'])

    def test_the_wsl_setting_is_offered_on_windows_only(self) -> None:
        self.load()

        with mock.patch.object(settings, 'IS_WINDOWS', False):
            linux_keys = {spec['key'] for spec in settings.describe_settings()['specs']}
        with mock.patch.object(settings, 'IS_WINDOWS', True):
            windows_keys = {spec['key'] for spec in settings.describe_settings()['specs']}

        self.assertNotIn('wsl', linux_keys)
        self.assertIn('wsl', windows_keys)

    def test_the_load_errors_are_described(self) -> None:
        _write(self.places.config_file, {'poll_interval': 0})
        self.load()

        self.assertEqual(settings.describe_settings()['errors'], [{'key': 'poll_interval', 'code': 'minimum', 'limit': 1}])

    def test_the_description_is_json_serializable(self) -> None:
        self.load()

        json.dumps(settings.describe_settings())


class SaveTest(_SettingsTestCase):
    def save(self, values: object) -> dict:
        return settings.save_settings(values, _LANGUAGES)

    def test_a_save_writes_the_configuration_directory_and_puts_the_values_in_force(self) -> None:
        self.load()

        result = self.save({'poll_interval': 2, 'include_completed': True})

        self.assertTrue(result['ok'])
        self.assertEqual(json.loads(self.places.config_file.read_text(encoding='utf-8')), {'poll_interval': 2, 'include_completed': True})
        self.assertEqual(settings.current_settings().poll_interval, 2)
        self.assertTrue(settings.current_settings().include_completed)
        self.assertEqual(result['path'], str(self.places.config_file))

    def test_only_values_that_differ_from_their_default_are_written(self) -> None:
        _write(self.places.config_file, {'poll_interval': 2})
        self.load()

        self.save(dict(vars(Settings())))

        self.assertEqual(json.loads(self.places.config_file.read_text(encoding='utf-8')), {})

    def test_left_out_keys_keep_the_value_in_force(self) -> None:
        _write(self.places.config_file, {'ended_max_age': 60})
        self.load()

        self.save({'poll_interval': 2})

        self.assertEqual(settings.current_settings().ended_max_age, 60)
        self.assertEqual(json.loads(self.places.config_file.read_text(encoding='utf-8')), {'ended_max_age': 60, 'poll_interval': 2})

    def test_comment_keys_survive_even_one_added_while_the_app_runs(self) -> None:
        _write(self.places.config_file, {'_comment': 'first', 'poll_interval': 2})
        self.load()
        _write(self.places.config_file, {'_comment': 'edited', '_note': 'added', 'poll_interval': 2})

        self.save({'poll_interval': 3})

        written = json.loads(self.places.config_file.read_text(encoding='utf-8'))
        self.assertEqual(written, {'_comment': 'edited', '_note': 'added', 'poll_interval': 3})

    def test_a_portable_file_is_saved_back_next_to_the_executable(self) -> None:
        _write(self.places.portable_file, {'poll_interval': 2})
        self.load()

        self.save({'poll_interval': 3})

        self.assertEqual(json.loads(self.places.portable_file.read_text(encoding='utf-8')), {'poll_interval': 3})
        self.assertFalse(self.places.config_file.exists())

    def test_a_save_moves_values_from_an_earlier_location_and_leaves_that_file_alone(self) -> None:
        _write(self.places.home_file, {'_comment': 'mine', 'ended_max_age': 60})
        original = self.places.home_file.read_text(encoding='utf-8')
        self.load()

        result = self.save({'poll_interval': 2})

        self.assertTrue(result['ok'])
        self.assertEqual(self.places.home_file.read_text(encoding='utf-8'), original)
        written = json.loads(self.places.config_file.read_text(encoding='utf-8'))
        self.assertEqual(written, {'_comment': 'mine', 'ended_max_age': 60, 'poll_interval': 2})
        self.assertEqual(settings.describe_settings()['legacy_path'], '')
        # The next start reads the moved file, not the old one.
        self.assertEqual(settings._load().path, self.places.config_file)

    def test_an_invalid_value_is_refused_and_nothing_is_written(self) -> None:
        self.load()

        result = self.save({'poll_interval': 0, 'ended_max_age': 'soon'})

        self.assertFalse(result['ok'])
        self.assertEqual({error['key'] for error in result['errors']}, {'poll_interval', 'ended_max_age'})
        self.assertFalse(self.places.config_file.exists())
        self.assertEqual(settings.current_settings(), Settings())

    def test_an_unknown_key_is_refused(self) -> None:
        self.load()

        result = self.save({'theme': 'light'})

        self.assertFalse(result['ok'])
        self.assertEqual(result['errors'], [{'key': 'theme', 'code': 'unknown'}])

    def test_a_language_without_a_locale_file_is_refused(self) -> None:
        self.load()

        refused = self.save({'language': 'xx'})
        accepted = self.save({'language': 'pt-BR'})
        automatic = self.save({'language': ''})

        self.assertEqual(refused['errors'], [{'key': 'language', 'code': 'language'}])
        self.assertTrue(accepted['ok'])
        self.assertTrue(automatic['ok'])

    def test_anything_but_a_mapping_is_refused(self) -> None:
        self.load()

        for values in (None, [], 'poll_interval=2', 5):
            with self.subTest(values=values):
                self.assertFalse(self.save(values)['ok'])

    def test_the_result_says_what_changed_and_how_it_applies(self) -> None:
        self.load()

        result = self.save({'poll_interval': 2, 'window_width': 900, 'language': 'de', 'wsl': True})

        self.assertEqual(result['changed'], ['language', 'poll_interval', 'window_width'])
        self.assertEqual(result['restart'], ['window_width'])
        self.assertTrue(result['reload'])

    def test_a_save_that_changes_nothing_reports_no_change(self) -> None:
        self.load()

        result = self.save({'poll_interval': Settings().poll_interval})

        self.assertTrue(result['ok'])
        self.assertEqual(result['changed'], [])
        self.assertFalse(result['reload'])

    def test_the_write_is_atomic_and_leaves_no_temporary_file(self) -> None:
        self.load()

        self.save({'poll_interval': 2})

        self.assertEqual(sorted(path.name for path in self.places.config.iterdir()), [settings.SETTINGS_FILENAME])

    def test_a_failed_write_is_reported_and_keeps_the_settings_in_force(self) -> None:
        _write(self.places.config_file, {'poll_interval': 2})
        self.load()

        with mock.patch.object(settings.os, 'replace', side_effect=PermissionError(13, 'Access is denied')):
            result = self.save({'poll_interval': 3})

        self.assertFalse(result['ok'])
        self.assertEqual(result['errors'], [{'key': '', 'code': 'write', 'detail': 'Access is denied'}])
        self.assertEqual(settings.current_settings().poll_interval, 2)
        self.assertEqual(json.loads(self.places.config_file.read_text(encoding='utf-8')), {'poll_interval': 2})
        self.assertEqual(sorted(path.name for path in self.places.config.iterdir()), [settings.SETTINGS_FILENAME])

    def test_a_save_clears_the_load_errors(self) -> None:
        _write(self.places.config_file, {'poll_interval': 0, 'speling': 1})
        self.load()

        self.save({})

        self.assertEqual(settings.describe_settings()['errors'], [])
        self.assertEqual(json.loads(self.places.config_file.read_text(encoding='utf-8')), {})


if __name__ == '__main__':
    unittest.main()
