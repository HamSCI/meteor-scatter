"""sigmond Receiver Channels TUI parser for msk144-recorder.

Loaded by sigmond at TUI time via ``[client_features.receiver_channels]``
in ``deploy.toml`` (parser_file / parser_attr).  Pure function over a
parsed config dict — no msk144-recorder runtime state required, no
imports from msk144_recorder internals.  See
``sigmond/docs/ADD-A-CLIENT.md`` and ``sigmond/lib/sigmond/
client_features.py`` for the contract.
"""

from __future__ import annotations

from typing import Optional

from sigmond.ka9q_encoding import ENCODING_INTS, encoding_to_int


def parse_receiver_channels(
    cfg: dict,
) -> tuple[str, set[int], Optional[int]]:
    """Return ``(status_dns, configured_freqs_hz, encoding_int)`` from
    a msk144-recorder per-instance config.

    msk144-recorder lays out one or more [[radiod]] blocks; each radiod
    binds a single ``[radiod.msk144]`` sub-block with its own
    ``freqs_hz`` list (the 10 m / 6 m MSK144 dial frequencies).
    Encoding may be set per-mode; defaults to s16be when unset.
    """
    blocks = cfg.get("radiod") or []
    if isinstance(blocks, dict):
        blocks = [blocks]
    status = ""
    freqs: set[int] = set()
    encoding: Optional[int] = None
    for b in blocks:
        if not status:
            status = str(b.get("status") or "")
        for mode in ("msk144",):
            m = b.get(mode) or {}
            for hz in m.get("freqs_hz", []) or []:
                try:
                    freqs.add(int(hz))
                except (TypeError, ValueError):
                    continue
            if encoding is None and m.get("encoding"):
                encoding = encoding_to_int(m["encoding"])
    if encoding is None and freqs:
        encoding = ENCODING_INTS["s16be"]
    return status, freqs, encoding
