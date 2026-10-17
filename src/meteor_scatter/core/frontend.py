"""radiod's front-end gain, recorded beside each kept recording.

radiod's RX888 front-end AGC steps the VGA at most once a second, shared by every channel.  We do
not touch it; we record it.  FrontendListener joins radiod's status multicast group, sends nothing,
decodes each packet with ka9q.status.decode_status_packet and feeds FrontendLog.  A status packet
tells us the gain changed somewhere between two packets, not at which sample, so a step is reported
as a window of samples.  Times are the host receive time of each packet.

The same packets carry each channel's own settings (preset, AGC, gain, encoding).  FrontendLog keeps
the latest report per frequency, so each sidecar can record what radiod reported for its channel
beside what the client asked for.
"""
from __future__ import annotations

import bisect
import logging
import math
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass, asdict
from typing import Optional

logger = logging.getLogger(__name__)

BASIS = "host receive time of radiod status packets"


def _finite(x):
    """x when it holds a finite number, else None.  A NaN or infinity in one status packet
    would crash int() and write invalid JSON into a sidecar."""
    try:
        return x if x is not None and math.isfinite(x) else None
    except TypeError:
        return None


@dataclass(frozen=True)
class FrontendReading:
    t: float
    rf_gain: Optional[float]
    rf_atten: Optional[float]
    rf_agc: Optional[bool]
    if_power: Optional[float]
    ad_over: Optional[int]

    def _state(self):
        return (self.rf_gain, self.rf_atten, self.rf_agc)


class FrontendLog:
    def __init__(self, keep_sec: float = 300.0, gap_sec: float = 5.0):
        self._keep = keep_sec
        self._gap = gap_sec
        self._readings: list[FrontendReading] = []
        self._channels: dict[int, dict] = {}
        self._lock = threading.Lock()

    def add_channel(self, frequency_hz: float, *, ssrc, preset, agc_enable, gain_db, encoding,
                    t: float) -> None:
        """Keep radiod's latest report of one channel's settings, keyed by its frequency."""
        rep = {"ssrc": ssrc, "preset": preset,
               "agc_enable": None if agc_enable is None else bool(agc_enable),
               "gain_db": gain_db, "encoding": encoding, "received_at": t}
        with self._lock:
            self._channels[int(round(frequency_hz))] = rep

    def channel_reported(self, frequency_hz) -> Optional[dict]:
        """radiod's latest report for the channel on this frequency, or None when none arrived."""
        with self._lock:
            rep = self._channels.get(int(round(frequency_hz)))
            return dict(rep) if rep is not None else None

    def add(self, r: FrontendReading) -> None:
        with self._lock:
            last = self._readings[-1] if self._readings else None
            if last is not None and last._state() == r._state() and r.ad_over == last.ad_over \
                    and r.t - last.t < 1.0:
                return
            self._readings.append(r)
            cut = r.t - self._keep
            while self._readings and self._readings[0].t < cut:
                self._readings.pop(0)

    def latest(self) -> Optional[FrontendReading]:
        with self._lock:
            return self._readings[-1] if self._readings else None

    def window(self, t0: float, t1: float, sample_rate: int) -> dict:
        """Summarise the front end over [t0, t1] (UTC seconds) as sample windows.

        start holds the reading in force at t0: the last one at or before t0 when it is at most
        gap_sec old, else the first one inside the slot when it arrives within gap_sec of t0, else
        None.  end holds the last reading at or before t1 when it is at most gap_sec old, else
        None.  Neither ever carries an older reading forward (spec §4).  The log keeps one
        unchanged reading per second, so end.t can lag the last packet by up to 1 s.  A falling ad_over counter means radiod restarted:
        ad_over_delta and overloaded are then None and ad_over_reset is True (always a bool).
        Dedup can also widen a step's sample_window by up to 1 s.
        """
        with self._lock:
            rs = list(self._readings)
        n = int(round((t1 - t0) * sample_rate))
        to_s = lambda t: max(0, min(n, int(round((t - t0) * sample_rate))))
        times = [x.t for x in rs]
        i0 = bisect.bisect_right(times, t0) - 1          # last reading at/before t0
        i1 = bisect.bisect_right(times, t1)              # first reading after t1
        span = rs[max(i0, 0): i1 + 1]
        if not span or (i0 < 0 and (i1 == 0)):
            return {"basis": BASIS, "frontend_status": "missing", "start": None, "end": None,
                    "steps": [], "missing_sample_windows": [[0, n]],
                    "ad_over_delta": None, "overloaded": None,
                    "ad_over_reset": False}
        before = rs[i0] if i0 >= 0 else None
        inside = [x for x in span if t0 < x.t <= t1]
        steps = []
        for a, b in zip(span, span[1:]):
            if a._state() != b._state() and b.t > t0 and a.t < t1:
                steps.append({"from_db": a.rf_gain, "to_db": b.rf_gain,
                              "atten_from": a.rf_atten, "atten_to": b.rf_atten,
                              "sample_window": [to_s(a.t), to_s(b.t)]})
        missing = []
        edges = ([t0] if before is None else [before.t]) + [x.t for x in inside] + [t1]
        for a, b in zip(edges, edges[1:]):
            if b - a > self._gap:
                missing.append([to_s(a), to_s(b)])
        if before is not None:
            start = before if t0 - before.t <= self._gap else None
        else:
            start = inside[0] if inside and inside[0].t - t0 <= self._gap else None
        last = inside[-1] if inside else before
        end = last if last is not None and t1 - last.t <= self._gap else None
        overs = [x.ad_over for x in ([before] if before else []) + inside if x.ad_over is not None]
        delta = (overs[-1] - overs[0]) if len(overs) >= 2 else None
        reset = delta is not None and delta < 0     # counter fell: radiod restarted
        if reset:
            delta = None                            # overload unknown, never False
        if not missing:
            status = "ok"
        elif missing == [[0, n]]:
            status = "missing"          # no packet covers any part of the window
        else:
            status = "partial"
        return {"basis": BASIS, "frontend_status": status,
                "start": asdict(start) if start else None, "end": asdict(end) if end else None,
                "steps": steps, "missing_sample_windows": missing,
                "ad_over_delta": delta, "overloaded": (delta > 0) if delta is not None else None,
                "ad_over_reset": reset}


IP_MULTICAST_ALL_LINUX = 49


def configure_status_socket(s, group: str, port: int) -> None:
    """Bind to the group address (not the wildcard) and join it; receive only, never send.

    Binding to 0.0.0.0 would deliver status packets from every group any socket on the host
    joined on this port, mixing another radiod's front-end gain into this log.  Binding to the
    group filters by destination; IP_MULTICAST_ALL=0 stops Linux delivering other groups'
    packets to a socket that joined only this one.
    """
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    if sys.platform.startswith("linux"):
        try:
            s.setsockopt(socket.IPPROTO_IP,
                         getattr(socket, "IP_MULTICAST_ALL", IP_MULTICAST_ALL_LINUX), 0)
        except OSError:
            pass
    s.bind((group, port))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 struct.pack("=4s4s", socket.inet_aton(group), socket.inet_aton("0.0.0.0")))
    s.settimeout(0.5)


class FrontendListener:
    """Passive: joins the status group, decodes, never sends."""

    def __init__(self, status_address: str, log: FrontendLog):
        self._address = status_address
        self._log = log
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None
        self._last_warn: Optional[float] = None

    def _open(self) -> socket.socket:
        from ka9q.status_listener import DEFAULT_STATUS_PORT
        from ka9q.utils import resolve_multicast_address
        group = resolve_multicast_address(self._address, timeout=5.0)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            configure_status_socket(s, group, DEFAULT_STATUS_PORT)
        except Exception:
            s.close()
            raise
        return s

    def start(self) -> None:
        if self._running:
            return
        self._sock = self._open()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="frontend-listener")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._sock:
            self._sock.close()

    def _warn(self, e: Exception, now: float) -> None:
        # a persistent fault logs once, then at most once a minute
        if self._last_warn is None or now - self._last_warn >= 60.0:
            self._last_warn = now
            logger.warning("frontend listener: %s", e)

    def _loop(self) -> None:
        from ka9q.status import decode_status_packet
        while self._running:
            try:
                buf, _ = self._sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError as e:
                self._warn(e, time.monotonic())
                time.sleep(1.0)
                continue
            try:
                st = decode_status_packet(buf)
            except Exception:
                continue
            if st is None:
                continue
            try:
                self._ingest(st, time.time())
            except Exception as e:  # noqa: BLE001 — one bad packet must not stop the listener
                self._warn(e, time.monotonic())

    def _ingest(self, st, now: float) -> None:
        """One decoded status packet: the channel's own settings, then the front end."""
        freq = _finite(st.frequency)
        if freq is not None:
            self._log.add_channel(freq, ssrc=st.ssrc, preset=st.preset,
                                  agc_enable=st.agc_enable, gain_db=_finite(st.gain),
                                  encoding=st.encoding_name, t=now)
        fe = st.frontend
        rf_gain, ad_over = _finite(fe.rf_gain), _finite(fe.ad_over)
        if rf_gain is None and ad_over is None:
            return
        self._log.add(FrontendReading(t=now, rf_gain=rf_gain, rf_atten=_finite(fe.rf_atten),
                                      rf_agc=fe.rf_agc, if_power=_finite(fe.if_power),
                                      ad_over=ad_over))
