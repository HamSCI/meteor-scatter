"""Reporter-id spellings: one name, three forms.

    display   AC0G/B4      what a person types and reads
    storage   AC0G=B4      file names, directory names, config keys
    systemd   AC0G\\x3dB4  unit names, *.wants links, %i

A copy of the rules in sigmond's lib/sigmond/instance.py, which stays the
original.  This client keeps its own copy so it runs on a host, or in a
venv, without sigmond.  tests/data/reporter_id_vectors.json is a
byte-identical copy of sigmond's table, and tests/test_reporter_id.py runs
this module against it; sigmond's tests/test_reporter_id_drift.py fails
when the two tables differ.  psk-recorder, meteor-scatter and
wspr-recorder carry this same file.
"""
import re

_XHH = re.compile(r"\\x([0-9a-fA-F]{2})")
_RID = re.compile(r"^[A-Z0-9][A-Z0-9=-]*[A-Z0-9]$")
_UNIT_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789:-_.")


def unescape_systemd_instance(name: str) -> str:
    """AC0G\\x3dB4 -> AC0G=B4.  A plain name comes back unchanged."""
    return _XHH.sub(lambda m: chr(int(m.group(1), 16)), name)


def to_storage(text: str) -> str:
    """Any spelling of a reporter id -> storage form: unescape, upper-case,
    '/' -> '='.  Reporter ids are upper case only, so ac0g/b4 names AC0G=B4.
    Raises ValueError on text that is no reporter id (bee1-status.local)."""
    rid = unescape_systemd_instance(text.strip()).upper().replace("/", "=")
    if not _RID.fullmatch(rid):    # `$` alone lets "AC0G\n" through
        raise ValueError(f"not a reporter id: {text!r}")
    return rid


def systemd_instance(name: str) -> str:
    """Storage form -> the instance as a unit name carries it.  '=' becomes
    \\x3d; letters, digits and '-' stay (systemd-escape would turn '-' into
    \\x2d and name a different unit)."""
    return "".join(
        c if c in _UNIT_CHARS else "".join(f"\\x{b:02x}" for b in c.encode())
        for c in name)


def display(name: str) -> str:
    """Any spelling -> what a person reads (AC0G/B4)."""
    prev = None
    while name != prev:
        prev, name = name, unescape_systemd_instance(name)
    return name.replace("=", "/")
