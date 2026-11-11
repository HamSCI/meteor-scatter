"""msqp packages and `msqp wav` read F16 kept slots and still export 32-bit float WAV."""
import argparse
import io
import json
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timezone

import numpy as np

from meteor_scatter.core.archive import Archive
from meteor_scatter.core.iq import read_iq_bytes, write_iq_wav
from meteor_scatter.msqp_cli import build_package, cmd_wav

CFG = {"operator": "Michael Hauan AC0G", "contact_email": "a@b.c", "antenna": "dipole",
       "comments": ""}
T_F16 = 1797206370.0       # 2026-12-13 23:59:30 UTC
T_F32 = 1797206400.0       # 2026-12-14 00:00:00 UTC
SCALE_DB = 0.0


def _iq(seed):
    rng = np.random.default_rng(seed)
    return (0.05 * (rng.standard_normal(3000) + 1j * rng.standard_normal(3000))
            ).astype(np.complex64)


def _keep(archive, tmp, t, iq):
    src = tmp / "iq-src.wav"
    write_iq_wav(src, iq, 12000, 50260000)
    iso = datetime.fromtimestamp(t, timezone.utc).isoformat()
    return archive.keep(src, 50260000, t, {
        "slot_start_utc": iso, "dial_freq_hz": 50260000, "clipped_samples": 0,
        "frontend": {"overloaded": False}, "capture": "iq", "iq_sample_rate": 12000,
        "iq_encoding": "f32le", "iq_center_hz": 50260000, "scale_db": SCALE_DB,
        "wav_gain_db": SCALE_DB, "timing": {"slot_start_utc_exact": t}, "gain_chain": {},
        "decodes": [{"snr": -2, "dt": 0.3, "audio_freq_hz": 1500, "message": "CQ X"}]})


def test_package_mixed_wav_and_iq16(tmp_path):
    iq16, iq32 = _iq(1), _iq(2)
    f16 = Archive(tmp_path / "archive", 90, 2, archive_dtype="f16")
    f32 = Archive(tmp_path / "archive", 90, 2)
    p16 = _keep(f16, tmp_path, T_F16, iq16)
    p32 = _keep(f32, tmp_path, T_F32, iq32)
    assert p16.name.endswith(".iq16.zst") and p32.name.endswith(".wav")

    r = build_package(f32, CFG, "AC0G/B4", "EM38ww", date(2026, 12, 13), date(2026, 12, 14),
                      tmp_path / "msqp", "v1")
    assert r.files == 2 and r.skipped == []
    with zipfile.ZipFile(r.zip_path) as z:
        names = sorted(z.namelist())
        assert names == [
            "6m/20261213/261213_235930.json", "6m/20261213/261213_235930.wav",
            "6m/20261214/261214_000000.json", "6m/20261214/261214_000000.wav",
            "README.txt", "classification.csv"]
        assert not any(".iq16" in n or n.endswith(".zst") for n in names)
        got16, rate16 = read_iq_bytes(z.read("6m/20261213/261213_235930.wav"))
        got32, _ = read_iq_bytes(z.read("6m/20261214/261214_000000.wav"))
        side16 = json.loads(z.read("6m/20261213/261213_235930.json"))
        side32 = json.loads(z.read("6m/20261214/261214_000000.json"))
    assert rate16 == 12000 and got16.dtype == np.complex64
    assert np.array_equal(got32, iq32)
    # F16 keeps 11 significant bits: error at most 2^-11 of each value, plus the subnormal floor.
    assert np.all(np.abs(got16.real - iq16.real) <= np.abs(iq16.real) * 2 ** -11 + 2 ** -24 / 4096)
    assert np.all(np.abs(got16.imag - iq16.imag) <= np.abs(iq16.imag) * 2 ** -11 + 2 ** -24 / 4096)
    assert side16["dtype"] == "complex-f16" and side16["scale_db"] == SCALE_DB
    assert "dtype" not in side32
    assert "<stem>.wav" in (r.dir / "README.txt").read_text()


def test_msqp_wav_from_iq16(tmp_path):
    iq = _iq(3)
    f16 = Archive(tmp_path / "a16", 90, 2, archive_dtype="f16")
    f32 = Archive(tmp_path / "a32", 90, 2)
    p16 = _keep(f16, tmp_path, T_F16, iq)
    p32 = _keep(f32, tmp_path, T_F16, iq)
    assert p16.name.endswith(".iq16.zst")

    def run(slot, out):
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            rc = cmd_wav(argparse.Namespace(slot=str(slot), instance=None, output=str(out)))
        assert rc == 0, err.getvalue()
        return out

    def pcm(path):
        raw = path.read_bytes()
        return np.frombuffer(raw[raw.index(b"data") + 8:], "<i2").astype(np.float64)

    a = pcm(run(p16, tmp_path / "from16.wav"))
    b = pcm(run(p32, tmp_path / "from32.wav"))
    assert a.shape == b.shape and np.abs(b).max() > 100
    assert np.sqrt(np.mean((a - b) ** 2)) <= 1.0


def test_totals_count_an_f16_slot_at_its_package_size(tmp_path):
    from meteor_scatter.msqp_cli import _package_size
    iq = _iq(4)
    p16 = _keep(Archive(tmp_path / "a16", 90, 2, archive_dtype="f16"), tmp_path, T_F16, iq)
    p32 = _keep(Archive(tmp_path / "a32", 90, 2), tmp_path, T_F16, iq)
    assert _package_size(p16) == p32.stat().st_size
