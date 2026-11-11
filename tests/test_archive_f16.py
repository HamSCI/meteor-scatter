"""Decoded slots kept as F16 behind msqp.archive_dtype."""
import json
from datetime import date
from pathlib import Path

import numpy as np
import pytest
import zstandard

from meteor_scatter.config import get_msqp
from meteor_scatter.core.archive import Archive, read_slot_iq
from meteor_scatter.core import iq_codec
from meteor_scatter.core.iq import write_iq_wav

T0 = 1797206370.0          # 2026-12-13 23:59:30 UTC
RATE = 12000

# hf-timestd's encode_f16 on PINNED_SAMPLES, computed once from its iq_codec.py.
PINNED_SAMPLES = np.array(
    [0.1 + 0.2j, -0.5 + 0.25j, 1 + 0j, 0j, 0.001 - 0.003j, 3.9 - 3.9j,
     -0.0123 + 0.0456j, 15 + 0.75j], dtype=np.complex64)
PINNED_BYTES = bytes.fromhex("665e666200e80064006c000000000000194425cacd73cdf34cd2d659807b006a")


def _iq(n=2000, amp=0.5):
    rng = np.random.default_rng(7)
    return (amp * (rng.standard_normal(n) + 1j * rng.standard_normal(n))).astype(np.complex64)


@pytest.fixture
def spool(tmp_path):
    d = tmp_path / "spool"
    d.mkdir()
    return d


def _slot_wav(spool, iq):
    p = spool / "261213_235930.iq.wav"
    write_iq_wav(p, iq, RATE)
    return p


def _day(root):
    return root / "10m" / "20261213"


def test_codec_copy_matches_hf_timestd_bytes():
    assert iq_codec.encode_f16(PINNED_SAMPLES) == PINNED_BYTES
    back = iq_codec.decode_iq(PINNED_BYTES, "complex-f16", 4096.0)
    expect = np.frombuffer(PINNED_BYTES, dtype="<f2").astype(np.float32) / np.float32(4096.0)
    assert np.array_equal(back.view(np.float32), expect)
    assert iq_codec.bytes_per_sample("complex-f16") == 4


def test_keep_f16_writes_iq16_and_sidecar(tmp_path, spool):
    iq = _iq()
    a = Archive(tmp_path / "arc", 90, 2, archive_dtype="f16")
    src = _slot_wav(spool, iq)
    dest = a.keep(src, 28145000, T0, {"k": 1})
    assert dest == _day(a.root) / "261213_235930.iq16.zst"
    assert not src.exists()
    raw = zstandard.ZstdDecompressor().decompress(dest.read_bytes())
    assert raw == iq_codec.encode_f16(iq)
    side = json.loads((_day(a.root) / "261213_235930.json").read_text())
    assert side == {"k": 1, "dtype": "complex-f16", "scale": 4096.0, "sample_rate": RATE}
    assert not (_day(a.root) / "261213_235930.wav").exists()
    assert not list(_day(a.root).glob("*.part"))


def test_keep_overflow_keeps_wav(tmp_path, spool):
    iq = _iq()
    iq[5] = 100.0 + 0j                      # 100 * 4096 > 65504
    a = Archive(tmp_path / "arc", 90, 2, archive_dtype="f16")
    src = _slot_wav(spool, iq)
    dest = a.keep(src, 28145000, T0, {"k": 1})
    assert dest == _day(a.root) / "261213_235930.wav"
    assert not src.exists()
    side = json.loads(dest.with_suffix(".json").read_text())
    assert side == {"k": 1, "f16_fallback": "overflow"}
    assert not list(_day(a.root).glob("*.iq16.zst"))
    back, rate = read_slot_iq(dest)
    assert rate == RATE and np.array_equal(back, iq)


def test_default_keeps_wav(tmp_path, spool):
    iq = _iq()
    a = Archive(tmp_path / "arc", 90, 2)
    src = _slot_wav(spool, iq)
    original = src.read_bytes()
    dest = a.keep(src, 28145000, T0, {"k": 1})
    assert dest == _day(a.root) / "261213_235930.wav"
    assert dest.read_bytes() == original
    assert json.loads(dest.with_suffix(".json").read_text()) == {"k": 1}


def test_f16_with_audio_wav_keeps_it_as_is(tmp_path, spool):
    """A mono audio WAV (capture = usb) is no I/Q: f16 leaves it alone."""
    audio = spool / "x.wav"
    audio.write_bytes(b"\0" * 1000)
    a = Archive(tmp_path / "arc", 90, 2, archive_dtype="f16")
    dest = a.keep(audio, 28145000, T0, {"k": 1})
    assert dest.name == "261213_235930.wav" and dest.read_bytes() == b"\0" * 1000
    assert json.loads(dest.with_suffix(".json").read_text()) == {"k": 1}


def test_keep_f16_failure_restores_wav(tmp_path, spool):
    a = Archive(tmp_path / "arc", 90, 2, archive_dtype="f16")
    src = _slot_wav(spool, _iq())
    with pytest.raises(TypeError):
        a.keep(src, 28145000, T0, {"k": object()})
    assert src.exists()
    assert not list(_day(a.root).glob("*"))


def test_read_slot_iq_both_kinds(tmp_path, spool):
    iq = _iq()
    a16 = Archive(tmp_path / "a16", 90, 2, archive_dtype="f16")
    p16 = a16.keep(_slot_wav(spool, iq), 28145000, T0, {})
    a32 = Archive(tmp_path / "a32", 90, 2)
    p32 = a32.keep(_slot_wav(spool, iq), 28145000, T0, {})
    got32, r32 = read_slot_iq(p32)
    got16, r16 = read_slot_iq(p16)
    assert r32 == r16 == RATE
    assert got32.dtype == got16.dtype == np.complex64
    assert np.array_equal(got32, iq)
    assert np.allclose(got16, iq, rtol=2 ** -10, atol=2 ** -12)


def test_trim_counts_both_kinds(tmp_path):
    a = Archive(tmp_path / "arc", 90, 2)
    for day, name in (("20260901", "a.iq16.zst"), ("20261212", "b.wav"), ("20261213", "c.iq16.zst")):
        d = a.root / "6m" / day
        d.mkdir(parents=True)
        (d / name).write_bytes(b"\0" * 100)
        (d / name.split(".")[0]).with_suffix(".json").write_text("{}")
    assert a.prune(date(2026, 12, 14)) == ["6m/20260901"]
    assert a.usage(date(2026, 12, 13))["kept_today"] == 1
    assert [p.name for _, p, _ in a.recordings(date(2026, 12, 1), date(2026, 12, 31))] == \
        ["b.wav", "c.iq16.zst"]
    assert [s.name for _, _, s in a.recordings(date(2026, 12, 1), date(2026, 12, 31))] == \
        ["b.json", "c.json"]
    a.max_bytes = 150                         # two days of 102 bytes (data + json) each
    removed = a.prune(date(2026, 12, 14))
    assert removed == ["6m/20261212"]


def test_config_archive_dtype():
    assert get_msqp({})["archive_dtype"] == "complex64"
    assert get_msqp({"msqp": {"archive_dtype": "f16"}})["archive_dtype"] == "f16"
    with pytest.raises(ValueError, match="archive_dtype"):
        get_msqp({"msqp": {"archive_dtype": "float16"}})
