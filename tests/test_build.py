"""
Tests for the build script's code signing.

Everything here runs without the signing token: signtool is replaced by a
recording stub, so what is checked is the decision around it - that a build
without `signing.env` stays unsigned, that a configured signature which fails
stops the build instead of leaving an unsigned EXE behind, and that a signature
is always asked for with SHA-256 and a timestamp.

The signing itself is exercised by hand, the token being a physical device.
"""
from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import build

_CONFIGURED = {'SIGNING_THUMBPRINT': 'abc', 'SIGNING_TIMESTAMP_URL': 'http://ts.example/tsa', 'SIGNING_TIMESTAMP_FALLBACK_URL': 'http://ts2.example/tsa'}


class SigningConfigTest(unittest.TestCase):
    def test_a_missing_file_means_no_signing(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            with mock.patch.object(build, 'SIGNING_CONFIG', Path(base) / 'signing.env'):
                self.assertIsNone(build._signing_config())

    def test_it_reads_the_keys_past_comments_and_blank_lines(self) -> None:
        config = self._config('# a comment\n\nSIGNING_THUMBPRINT = abc \n\nSIGNING_TIMESTAMP_URL=http://ts.example/tsa\n')
        self.assertEqual(config, {'SIGNING_THUMBPRINT': 'abc', 'SIGNING_TIMESTAMP_URL': 'http://ts.example/tsa'})

    def test_a_value_keeps_its_own_equals_signs(self) -> None:
        # A timestamp server is addressed with a query string often enough that
        # splitting on every '=' would silently truncate the URL.
        config = self._config('SIGNING_THUMBPRINT=abc\nSIGNING_TIMESTAMP_URL=http://ts.example/tsa?policy=1\n')
        self.assertEqual(config['SIGNING_TIMESTAMP_URL'], 'http://ts.example/tsa?policy=1')

    def test_a_line_that_is_not_key_value_stops_the_build(self) -> None:
        with self.assertRaises(SystemExit):
            self._config('SIGNING_THUMBPRINT=abc\nSIGNING_TIMESTAMP_URL\n')

    def test_a_missing_or_empty_required_key_stops_the_build(self) -> None:
        texts = ('SIGNING_TIMESTAMP_URL=http://ts.example/tsa\n', 'SIGNING_THUMBPRINT=\nSIGNING_TIMESTAMP_URL=http://ts.example/tsa\n',
                 'SIGNING_THUMBPRINT=abc\n')
        for text in texts:
            with self.subTest(text=text), self.assertRaises(SystemExit):
                self._config(text)

    def _config(self, text: str) -> dict[str, str] | None:
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / 'signing.env'
            path.write_text(text, encoding='utf-8')
            with mock.patch.object(build, 'SIGNING_CONFIG', path), contextlib.redirect_stdout(io.StringIO()):
                return build._signing_config()


class SigntoolLookupTest(unittest.TestCase):
    def test_it_takes_the_newest_sdk(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            self._plant(base, '10.0.19041.0')
            newest = self._plant(base, '10.0.26100.0')
            self.assertEqual(self._lookup(base), newest)

    def test_a_directory_that_is_not_a_version_sorts_last(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            self._plant(base, 'preview')
            numbered = self._plant(base, '10.0.19041.0')
            self.assertEqual(self._lookup(base), numbered)

    def test_no_installed_sdk_stops_the_build(self) -> None:
        with tempfile.TemporaryDirectory() as base, self.assertRaises(SystemExit):
            self._lookup(base)

    def _plant(self, base: str, version: str) -> Path:
        tool = Path(base) / 'Windows Kits' / '10' / 'bin' / version / 'x64' / 'signtool.exe'
        tool.parent.mkdir(parents=True)
        tool.touch()

        return tool

    def _lookup(self, base: str) -> Path:
        environment = {'ProgramFiles(x86)': base, 'ProgramFiles': str(Path(base) / 'absent')}
        with mock.patch.dict(build.os.environ, environment, clear=True), contextlib.redirect_stdout(io.StringIO()):
            return build._signtool()


class SignTest(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[list[str]] = []

    def test_a_build_without_a_configuration_leaves_the_exe_unsigned(self) -> None:
        self._sign(config=None)
        self.assertEqual(self.calls, [])

    def test_a_signature_is_always_asked_for_with_sha256_and_a_timestamp(self) -> None:
        # Without a timestamp the signature stops verifying the day the
        # certificate expires, which would turn a download published a year
        # earlier into an unsigned one.
        self._sign()
        command = self.calls[0]
        self.assertEqual(command[1], 'sign')
        self.assertEqual(command[command.index('/sha1') + 1], 'abc')
        self.assertEqual(command[command.index('/fd') + 1], 'SHA256')
        self.assertEqual(command[command.index('/tr') + 1], 'http://ts.example/tsa')
        self.assertEqual(command[command.index('/td') + 1], 'SHA256')

    def test_a_signed_exe_is_verified_rather_than_trusted(self) -> None:
        self._sign()
        self.assertEqual([command[1] for command in self.calls], ['sign', 'verify'])
        self.assertIn('/pa', self.calls[1])
        self.assertIn('/tw', self.calls[1])

    def test_a_failed_signature_tries_the_fallback_and_then_stops_the_build(self) -> None:
        with self.assertRaises(SystemExit):
            self._sign(results=[1, 1])

        self.assertEqual([command[command.index('/tr') + 1] for command in self.calls], ['http://ts.example/tsa', 'http://ts2.example/tsa'])

    def test_a_signature_that_works_on_the_fallback_is_accepted(self) -> None:
        self._sign(results=[1, 0, 0])
        self.assertEqual([command[1] for command in self.calls], ['sign', 'sign', 'verify'])

    def test_a_failed_verification_stops_the_build(self) -> None:
        # The EXE is signed at this point, so nothing but the exit code keeps a
        # signature this machine cannot verify off a release page.
        with self.assertRaises(SystemExit):
            self._sign(results=[0, 1])

    def test_without_a_fallback_a_failed_signature_is_not_repeated(self) -> None:
        config = {'SIGNING_THUMBPRINT': 'abc', 'SIGNING_TIMESTAMP_URL': 'http://ts.example/tsa'}
        with self.assertRaises(SystemExit):
            self._sign(config=config, results=[1])

        self.assertEqual(len(self.calls), 1)

    def _sign(self, config: dict[str, str] | None = _CONFIGURED, results: list[int] | None = None) -> None:
        pending = list(results or [0, 0])

        def record(command: list[str]) -> int:
            self.calls.append(command)

            return pending.pop(0) if pending else 0

        with mock.patch.object(build, '_signing_config', return_value=config), \
                mock.patch.object(build, '_signtool', return_value=Path('signtool.exe')), \
                mock.patch.object(build.subprocess, 'call', side_effect=record), \
                contextlib.redirect_stdout(io.StringIO()):
            build._sign(Path('dist') / 'AgentMonitorForClaude.exe')


if __name__ == '__main__':
    unittest.main()
