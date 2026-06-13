"""Tests for the MSK144_SETTLE_* env-var overrides on the settled-capture gate.

The defaults are tuned for bare-metal hosts with GPS PPS chrony
discipline.  Operators on VMs or hosts with looser timing need
to bump the threshold so the gate doesn't always time out.  These
tests verify the env helpers behave deterministically — invalid
input falls back to the default, scale is applied consistently,
and the class constants reflect what's in the environment at
class-load time.
"""
from __future__ import annotations

import importlib
import os
import sys
import unittest
from unittest.mock import patch


def _reload_recorder():
    """Force re-import so env-var-derived class attrs are recomputed."""
    if 'msk144_recorder.core.recorder' in sys.modules:
        del sys.modules['msk144_recorder.core.recorder']
    return importlib.import_module('msk144_recorder.core.recorder')


class EnvFloatHelperTests(unittest.TestCase):
    """`_env_float` is the parsing primitive for the timing knobs.

    It must apply `scale` to both env values AND defaults so the
    caller can state the default in the same unit the env var uses.
    A bug where scale was only applied to env values (and not the
    default) gave SETTLE_MAX_OFFSET_S = 100.0 (seconds!) instead of
    0.0001 when the env var was unset — silently disabling the gate.
    """

    def test_default_scaled(self):
        # No env var → returns default * scale.
        from msk144_recorder.core.recorder import _env_float
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('MSK144_TEST_KNOB', None)
            self.assertAlmostEqual(
                _env_float('MSK144_TEST_KNOB', 100.0, scale=1e-6),
                0.0001,
            )

    def test_env_scaled(self):
        # Env present and valid → env * scale.
        from msk144_recorder.core.recorder import _env_float
        with patch.dict(os.environ, {'MSK144_TEST_KNOB': '1500'}):
            self.assertAlmostEqual(
                _env_float('MSK144_TEST_KNOB', 100.0, scale=1e-6),
                0.0015,
            )

    def test_invalid_falls_back_scaled(self):
        from msk144_recorder.core.recorder import _env_float
        with patch.dict(os.environ, {'MSK144_TEST_KNOB': 'nonsense'}):
            self.assertAlmostEqual(
                _env_float('MSK144_TEST_KNOB', 100.0, scale=1e-6),
                0.0001,
            )

    def test_negative_rejected(self):
        from msk144_recorder.core.recorder import _env_float
        with patch.dict(os.environ, {'MSK144_TEST_KNOB': '-5'}):
            self.assertAlmostEqual(
                _env_float('MSK144_TEST_KNOB', 100.0, scale=1e-6),
                0.0001,
            )

    def test_zero_rejected(self):
        # Zero would mean "instantly settled" — almost certainly a typo.
        from msk144_recorder.core.recorder import _env_float
        with patch.dict(os.environ, {'MSK144_TEST_KNOB': '0'}):
            self.assertAlmostEqual(
                _env_float('MSK144_TEST_KNOB', 100.0, scale=1e-6),
                0.0001,
            )


class EnvIntHelperTests(unittest.TestCase):
    def test_default(self):
        from msk144_recorder.core.recorder import _env_int
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('MSK144_TEST_CYCLES', None)
            self.assertEqual(_env_int('MSK144_TEST_CYCLES', 3), 3)

    def test_env_override(self):
        from msk144_recorder.core.recorder import _env_int
        with patch.dict(os.environ, {'MSK144_TEST_CYCLES': '7'}):
            self.assertEqual(_env_int('MSK144_TEST_CYCLES', 3), 7)

    def test_invalid_falls_back(self):
        from msk144_recorder.core.recorder import _env_int
        with patch.dict(os.environ, {'MSK144_TEST_CYCLES': 'abc'}):
            self.assertEqual(_env_int('MSK144_TEST_CYCLES', 3), 3)

    def test_zero_rejected(self):
        # Zero cycles would never settle.
        from msk144_recorder.core.recorder import _env_int
        with patch.dict(os.environ, {'MSK144_TEST_CYCLES': '0'}):
            self.assertEqual(_env_int('MSK144_TEST_CYCLES', 3), 3)


class SettleConstantOverrideTests(unittest.TestCase):
    """Class constants on Msk144Recorder must reflect env vars present
    when the module is imported."""

    def test_defaults(self):
        with patch.dict(os.environ, {}, clear=False):
            for k in ('MSK144_SETTLE_MAX_OFFSET_US',
                      'MSK144_SETTLE_REQUIRED_CYCLES',
                      'MSK144_SETTLE_POLL_SEC',
                      'MSK144_SETTLE_TIMEOUT_SEC'):
                os.environ.pop(k, None)
            mod = _reload_recorder()
            self.assertAlmostEqual(mod.Msk144Recorder.SETTLE_MAX_OFFSET_S, 0.0001)
            self.assertEqual(mod.Msk144Recorder.SETTLE_REQUIRED_CYCLES, 3)
            self.assertAlmostEqual(mod.Msk144Recorder.SETTLE_POLL_SEC, 5.0)
            self.assertAlmostEqual(mod.Msk144Recorder.SETTLE_TIMEOUT_SEC, 60.0)

    def test_overrides(self):
        env = {
            'MSK144_SETTLE_MAX_OFFSET_US': '1500',
            'MSK144_SETTLE_REQUIRED_CYCLES': '5',
            'MSK144_SETTLE_POLL_SEC': '2',
            'MSK144_SETTLE_TIMEOUT_SEC': '120',
        }
        with patch.dict(os.environ, env):
            mod = _reload_recorder()
            self.assertAlmostEqual(mod.Msk144Recorder.SETTLE_MAX_OFFSET_S, 0.0015)
            self.assertEqual(mod.Msk144Recorder.SETTLE_REQUIRED_CYCLES, 5)
            self.assertAlmostEqual(mod.Msk144Recorder.SETTLE_POLL_SEC, 2.0)
            self.assertAlmostEqual(mod.Msk144Recorder.SETTLE_TIMEOUT_SEC, 120.0)


if __name__ == '__main__':
    unittest.main()
