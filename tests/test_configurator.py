"""Tests for `msk144-recorder config init|edit` (CONTRACT-v0.5 §14)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = str(REPO_ROOT / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from msk144_recorder import configurator


def _ns(**kwargs):
    base = dict(non_interactive=True, reconfig=False,
                config=None, radiod_id=None)
    base.update(kwargs)
    return SimpleNamespace(**base)


class FieldSubstitutionTests(unittest.TestCase):
    """Line-oriented [station] / [[radiod]] field replacement."""

    def test_station_field_replace(self):
        body = (
            '[station]\n'
            'callsign    = "OLD"\n'
            'grid_square = "AA00aa"\n'
            '\n'
            '[paths]\n'
            'callsign    = "PATHS_NOT_TOUCHED"\n'  # different section
        )
        out = configurator._replace_station_field(body, 'callsign', 'AC0G')
        self.assertIn('callsign    = "AC0G"', out)
        self.assertIn('callsign    = "PATHS_NOT_TOUCHED"', out)

    def test_radiod_field_replace_first_block_only(self):
        body = (
            '[[radiod]]\n'
            'id            = "old1"\n'
            'radiod_status = "old1.local"\n'
            '\n'
            '[radiod.msk144]\n'
            'sample_rate = 12000\n'
            '\n'
            '[[radiod]]\n'
            'id            = "old2"\n'
            'radiod_status = "old2.local"\n'
        )
        out = configurator._replace_radiod_field(body, 0, 'id', 'NEW1')
        self.assertIn('id            = "NEW1"', out)
        self.assertIn('id            = "old2"', out)

    def test_radiod_field_replace_second_block(self):
        body = (
            '[[radiod]]\n'
            'id            = "first"\n'
            '\n'
            '[[radiod]]\n'
            'id            = "second"\n'
        )
        out = configurator._replace_radiod_field(body, 1, 'id', 'NEW')
        self.assertIn('id            = "first"', out)
        self.assertIn('id            = "NEW"', out)
        self.assertNotIn('id            = "second"', out)

    def test_radiod_subtable_does_not_terminate_block(self):
        body = (
            '[[radiod]]\n'
            'id            = "x"\n'
            '\n'
            '[radiod.msk144]\n'
            'sample_rate = 12000\n'
            '\n'
            'radiod_status = "x.local"\n'  # Still in the [[radiod]] block scope.
        )
        # The radiod_status under [radiod.msk144] is also a candidate, but our
        # implementation considers anything before the next [[radiod]] or
        # other top-level section as part of the block — that's fine for
        # the simple template shape we ship.
        out = configurator._replace_radiod_field(body, 0, 'radiod_status',
                                                 'NEW.local')
        self.assertIn('radiod_status = "NEW.local"', out)


class ReporterDefaultTests(unittest.TestCase):
    """CONTRACT-v0.5 §14.6: reporter naming convention for msk144-recorder."""

    def _clear(self):
        for k in ('SIGMOND_RADIOD_COUNT', 'SIGMOND_RADIOD_INDEX'):
            os.environ.pop(k, None)

    def test_single_radiod_uses_bare_call(self):
        with mock.patch.dict(os.environ, {'SIGMOND_RADIOD_COUNT': '1'},
                             clear=False):
            self.assertEqual(
                configurator._default_reporter_callsign('AC0G'), 'AC0G')

    def test_count_unset_treated_as_single(self):
        self._clear()
        self.assertEqual(
            configurator._default_reporter_callsign('AC0G'), 'AC0G')

    def test_multi_radiod_appends_slash_b_index(self):
        with mock.patch.dict(os.environ, {
            'SIGMOND_RADIOD_COUNT': '3',
            'SIGMOND_RADIOD_INDEX': '2',
        }, clear=False):
            self.assertEqual(
                configurator._default_reporter_callsign('AC0G'), 'AC0G/B2')

    def test_multi_radiod_falls_back_to_b1_when_no_index(self):
        with mock.patch.dict(os.environ, {'SIGMOND_RADIOD_COUNT': '2'},
                             clear=False):
            os.environ.pop('SIGMOND_RADIOD_INDEX', None)
            self.assertEqual(
                configurator._default_reporter_callsign('AC0G'), 'AC0G/B1')

    def test_empty_call_yields_empty(self):
        self.assertEqual(configurator._default_reporter_callsign(''), '')


class InitCommandTests(unittest.TestCase):
    def test_writes_template_with_env_defaults_single_radiod(self):
        # Single radiod → bare callsign in [station].callsign.
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / 'cfg.toml'
            args = _ns(config=target, non_interactive=True)
            with mock.patch.dict(os.environ, {
                'STATION_CALL':           'AC0G',
                'STATION_GRID':           'EM38',
                'SIGMOND_INSTANCE':       'bee1-rx888',
                'SIGMOND_RADIOD_STATUS':  'bee1-status.local',
                'SIGMOND_RADIOD_COUNT':   '1',
            }, clear=False):
                os.environ.pop('SIGMOND_RADIOD_INDEX', None)
                rc = configurator.cmd_config_init(args)

            self.assertEqual(rc, 0)
            text = target.read_text()
            self.assertIn('callsign    = "AC0G"', text)
            self.assertIn('grid_square = "EM38"', text)
            # RADIOD-IDENTIFICATION.md §3.1: template's canonical field
            # is `status` (the multicast mDNS name).  The legacy `id`
            # line is a commented example only.
            self.assertIn('status = "bee1-status.local"', text)
            self.assertNotIn('status = "<configure-via-config-init>"', text)

    def test_writes_template_with_env_defaults_multi_radiod(self):
        # COUNT > 1 → callsign defaults to AC0G/B<index>.
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / 'cfg.toml'
            args = _ns(config=target, non_interactive=True)
            with mock.patch.dict(os.environ, {
                'STATION_CALL':           'AC0G',
                'STATION_GRID':           'EM38',
                'SIGMOND_INSTANCE':       'radiod-1',
                'SIGMOND_RADIOD_STATUS':  'r1-status.local',
                'SIGMOND_RADIOD_COUNT':   '3',
                'SIGMOND_RADIOD_INDEX':   '2',
            }, clear=False):
                rc = configurator.cmd_config_init(args)
            self.assertEqual(rc, 0)
            text = target.read_text()
            self.assertIn('callsign    = "AC0G/B2"', text)

    def test_refuses_to_overwrite_without_reconfig(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / 'cfg.toml'
            target.write_text('[station]\ncallsign = "EXISTING"\n')
            args = _ns(config=target, non_interactive=True)
            rc = configurator.cmd_config_init(args)

            self.assertEqual(rc, 1)
            self.assertEqual(target.read_text(),
                             '[station]\ncallsign = "EXISTING"\n')

    def test_reconfig_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / 'cfg.toml'
            target.write_text('[station]\ncallsign = "OLD"\n')
            args = _ns(config=target, non_interactive=True, reconfig=True)
            with mock.patch.dict(os.environ,
                                 {'STATION_CALL': 'AC0G'}, clear=False):
                rc = configurator.cmd_config_init(args)
            self.assertEqual(rc, 0)
            self.assertIn('callsign    = "AC0G"', target.read_text())

    def test_uses_safe_defaults_when_env_unset(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / 'cfg.toml'
            args = _ns(config=target, non_interactive=True)
            cleared = {k: '' for k in (
                'STATION_CALL', 'STATION_GRID',
                'SIGMOND_INSTANCE', 'SIGMOND_RADIOD_STATUS',
            )}
            with mock.patch.dict(os.environ, cleared, clear=False):
                # mock.patch.dict with empty strings still leaves keys present;
                # delete them so the configurator's `os.environ.get` returns ''.
                for k in cleared:
                    os.environ.pop(k, None)
                rc = configurator.cmd_config_init(args)
            self.assertEqual(rc, 0)
            text = target.read_text()
            # Template values get used as the safe fallback.  Phase 6:
            # the canonical `status` field is the only radiod
            # identifier — `id` no longer appears in the template.
            self.assertIn('callsign    = "YOURCALL"', text)
            self.assertIn('status = "my-rx888-status.local"', text)


class EditCommandTests(unittest.TestCase):
    def _initial_config(self) -> str:
        return (
            '[station]\n'
            'callsign    = "OLDCALL"\n'
            'grid_square = "AA00aa"\n'
            '\n'
            '[[radiod]]\n'
            'id            = "old-id"\n'
            'radiod_status = "old.local"\n'
            '\n'
            '[radiod.msk144]\n'
            'sample_rate = 12000\n'
        )

    def test_non_interactive_displays_only(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / 'cfg.toml'
            initial = self._initial_config()
            target.write_text(initial)
            args = _ns(config=target, non_interactive=True)
            rc = configurator.cmd_config_edit(args)
            self.assertEqual(rc, 0)
            # File untouched.
            self.assertEqual(target.read_text(), initial)

    def test_errors_when_target_absent(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / 'absent.toml'
            args = _ns(config=target, non_interactive=True)
            rc = configurator.cmd_config_edit(args)
            self.assertEqual(rc, 1)

    def test_radiod_id_arg_focuses_correct_block(self):
        body = (
            '[station]\ncallsign = "X"\ngrid_square = "Y"\n'
            '\n[[radiod]]\nid            = "first"\n'
            'radiod_status = "f.local"\n'
            '\n[[radiod]]\nid            = "second"\n'
            'radiod_status = "s.local"\n'
        )
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / 'cfg.toml'
            target.write_text(body)
            args = _ns(config=target, non_interactive=True,
                       radiod_id='second')
            # Just confirm dispatch finds the right block (returns 0).
            rc = configurator.cmd_config_edit(args)
            self.assertEqual(rc, 0)


class DiscoveryFlowTests(unittest.TestCase):
    """RADIOD-IDENTIFICATION.md §4 — ka9q-python discovery-driven init."""

    def test_single_radiod_used_when_env_empty(self):
        """Non-interactive: env empty but exactly one radiod discoverable
        → use its `hostname` (mDNS multicast name)."""
        _clear_env('SIGMOND_RADIOD_STATUS')
        with mock.patch.object(
                configurator, '_discover_radiods',
                return_value=[{
                    "name": "radiod@bee1",
                    "hostname": "bee1-status.local",
                    "address": "239.205.73.40",
                    "port": 5006,
                }]):
            with mock.patch.dict(os.environ, {'SIGMOND_INSTANCE': 'bee1-rx888'},
                                 clear=False):
                args = _ns(non_interactive=True)
                values = configurator._collect_init_values(args)
        self.assertEqual(values['radiod_status'], 'bee1-status.local')

    def test_zero_radiods_falls_back_to_placeholder(self):
        """Non-interactive + env empty + no discovery → placeholder shape."""
        _clear_env('SIGMOND_RADIOD_STATUS')
        with mock.patch.object(configurator, '_discover_radiods',
                               return_value=[]):
            with mock.patch.dict(os.environ, {'SIGMOND_INSTANCE': 'rx2'},
                                 clear=False):
                args = _ns(non_interactive=True)
                values = configurator._collect_init_values(args)
        self.assertEqual(values['radiod_status'], 'rx2-status.local')

    def test_env_wins_over_discovery(self):
        """Non-interactive + SIGMOND_RADIOD_STATUS set → env wins even
        if discovery returns a different value."""
        with mock.patch.object(
                configurator, '_discover_radiods',
                return_value=[{
                    "name": "other",
                    "hostname": "other.local",
                    "address": "239.0.0.1",
                    "port": 5006,
                }]):
            with mock.patch.dict(os.environ, {
                'SIGMOND_RADIOD_STATUS': 'operator-set.local',
                'SIGMOND_INSTANCE':      'bee1-rx888',
            }, clear=False):
                args = _ns(non_interactive=True)
                values = configurator._collect_init_values(args)
        self.assertEqual(values['radiod_status'], 'operator-set.local')

    def test_derive_label_from_status_strips_suffixes(self):
        from msk144_recorder.configurator import _derive_label_from_status
        self.assertEqual(_derive_label_from_status('bee1-status.local'), 'bee1')
        self.assertEqual(_derive_label_from_status('rx888.local'), 'rx888')
        self.assertEqual(_derive_label_from_status('plain-name'), 'plain-name')
        self.assertEqual(_derive_label_from_status(''), 'default')


def _clear_env(*names):
    """Helper: remove env vars (no-op if absent)."""
    for n in names:
        os.environ.pop(n, None)


if __name__ == '__main__':
    unittest.main()
