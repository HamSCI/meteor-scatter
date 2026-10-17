# MSQP capture — design

**Status:** approved in conversation 2026-10-17 (Michael Hauan, AC0G).  Tracks sigmond
`tasks/release-v373.md` row #12.  Target: v3.73, in service for the Geminids (peak about
2026-12-13/14).

## 1. Why

HamSCI's Meteor Scatter QSO Party (hamsci.org/node/984, hamsci.org/msqp-rules) asks every
entrant, monitors included, for two things.  First, reception reports on PSKReporter, which
meteor-scatter already sends.  Second, a WAV recording of each decode, gathered after the shower
into one zip and uploaded to the Zenodo community "Ham Radio Science Citizen Investigation"
(procedure: github.com/HamSCI/MSQP, `Zenodo.md`).  The rules also ask for receiver AGC off, so a
recording shows how each ping decays.

Today the client deletes every recording once jt9 has read it.  This design keeps the decoded
ones, records the receiver gain beside them, and carries them as far as an unpublished Zenodo
draft.  A person publishes, because Zenodo forbids any change to a record after publication.

The first version serves AC0G-B4 and the DASI2 stations.  Nothing in it depends on our hardware
or our accounts, so a volunteer's install can use it later without a redesign.  Each operator
uses a Zenodo account of their own; our stations do too.

## 2. Decisions

| Question | Decision |
|---|---|
| Which recordings | Only those in which jt9 decoded something, as WSJT-X's "Save Decoded" does |
| When | Always, year-round, under an age and size cap; the operator picks dates when packaging |
| Channel gain | Channel AGC off, 32-bit float stream, one fixed scale per band |
| Front-end gain | Leave radiod's front-end AGC alone; record its gain beside each recording |
| Noise floor | Watch it per band and warn on drift; never change the scale automatically |
| Zenodo | The station builds a draft through the API with the operator's token; a person publishes |
| Zenodo account | Each operator's own |
| Where the code lives | Inside meteor-scatter: archive in the daemon, two new commands |

## 3. Capture and archive

### 3.1 The channel

For each band the client asks radiod for a channel with AGC off and 32-bit float samples
(`ensure_channel(agc_enable=0, gain=0.0, encoding=F32, …)`).  Today the client passes neither
argument and takes the `usb` preset's AGC.  Changing the parameters makes radiod create a new
channel; the old one expires within about two minutes through its lifetime tag, and decoding
continues across the change.

### 3.2 From float to WAV

The client multiplies each sample by the band's fixed scale (`scale_db`) and writes the 16-bit,
12 kHz WAV that jt9 already reads.  The decoder and the archive therefore hold the same audio.
A ping strong enough to clip at 16 bits still clips; the client counts the clipped samples and
writes the count into the sidecar.

`meteor-scatter msqp calibrate` sets each band's scale.  It measures the band's noise over a few
minutes and chooses the scale that puts the noise about 50 dB below full scale, which leaves
room for strong pings.  Until someone calibrates a band, the client uses a default scale, and
every sidecar from that band says `"scale": "uncalibrated"`.  Decoding never waits on
calibration.

### 3.3 What the client keeps

When jt9 reports at least one decode, the client moves the recording out of the spool and into
the archive instead of deleting it:

```
/var/lib/meteor-scatter/archive/<band>/<YYYYMMDD>/<YYMMDD_HHMMSS>.wav
                                                  <YYMMDD_HHMMSS>.json
```

The file name follows WSJT-X's own pattern; the directory carries the band.  A recording with
no decode goes away as it does today.  The `paths.keep_wav` setting, which kept every recording
without limit, retires.

### 3.4 The sidecar

Each `.json` sidecar holds:

- the reporter id, band and dial frequency
- the slot's UTC start and sample rate
- the band's scale, or `uncalibrated`
- the channel's settings as radiod reported them (AGC, gain, encoding)
- jt9's decode lines for the slot
- the clipped-sample count
- the software version
- the front-end record of §4

### 3.5 The cap

Once an hour the client deletes the oldest archive days beyond `archive_max_days` (default 90)
or `archive_max_gb` (default 2), whichever binds first, and logs each deletion.

### 3.6 The noise-floor watch

The client estimates each band's noise floor from the recordings it already writes, from the
quietest stretch of each slot so that pings do not raise it.  When a band's floor moves more
than 10 dB from its calibrated level and stays there for an hour, the client warns: once in the
journal on the change, once again on recovery, and continuously in `smd status`.  The warning
reports the front-end gain's change over the same hour, so an operator can tell a noisier band
from a moved amplifier.  The client never adjusts the scale itself.  An operator re-runs
`calibrate`, deliberately, between showers; a scale that shifted silently during a shower would
leave its recordings incomparable.

## 4. The front-end gain record

radiod runs a second AGC, on the RX888's variable-gain amplifier, shared by every channel on the
receiver.  It steps at most once a second, steering the ADC level toward the middle of its
thresholds; on B4 it has ranged from 12.75 to 22.55 dB over a day.  Turning off the channel AGC
leaves it running.  Two effects follow.  A step inside a ping, which lasts from milliseconds to
about two seconds, puts into the recording a change of level that the meteor did not make.  And
the overall scale drifts across the day, so a recording's level means nothing by itself.

The front-end AGC belongs to every client on the receiver (T6, GRAPE, WSPR, FT8), keeps the ADC
from clipping, and changes only with a radiod restart.  meteor-scatter therefore leaves it
alone and records it.

One listener per instance reads the status packets radiod already multicasts.  It sends no poll
and no control traffic, and so cannot disturb T6 or any other client.  From each packet it takes
`rf_gain`, `rf_atten`, `rf_agc`, `if_power` and `ad_over`.  For each kept recording the sidecar
holds the reading in force at the slot's start, every change during the slot, and the last
reading.

A status packet shows that the gain changed somewhere between two packets, not at which sample.
The sidecar therefore gives each step as a window of samples, from the last packet with the old
value to the first with the new, and never invents a single position.  A rise in `ad_over`
during the slot marks the recording as overloaded.  When no packet arrives for more than five
seconds, the sidecar marks that stretch `"frontend_status": "missing"` and does not carry the
last value forward.

## 5. Packaging and the Zenodo draft

### 5.1 `meteor-scatter msqp package --from <date> --to <date>`

The command builds `/var/lib/meteor-scatter/msqp/<title>/`, containing:

- `<title>.zip`, the WAVs and sidecars for those UTC days, by band
- `ALL.TXT`, built in WSJT-X's format from the station's own decode log for the same days (a
  monitor makes no QSOs, so no ADIF)
- `description.md`, the MSQP description template filled in
- `metadata.json`, the record exactly as `draft` will send it

It prints decodes per band per day, the file count, the zip's size, and the recordings that
clipped or overloaded.  The title takes the MSQP form, `<CALL> <GRID6> <YYYYMMDD-DD> Meteor
Scatter QSO Party`.

The description fields come from three places.  The station supplies the callsign (its reporter
id), the six-character grid (the first six characters of `[station] grid_square`), "Receive Only", the bands the archive holds, the
equipment (RX888 and radiod), and "N/A" for transmitter power.  The client writes a comment
explaining the AGC setting, the front-end record in the sidecars, and the software version.  The
operator supplies, once, in the instance config's `[msqp]` block: operator names and calls, a
contact email, and the antenna.  `package` refuses while any of those three is missing and names
the missing one.

`package` writes only locally and always runs.

### 5.2 `meteor-scatter msqp draft <package-dir>`

The command reads the operator's token from `/etc/meteor-scatter/zenodo-token` (mode 0600; scope
`deposit:write`).  Through Zenodo's deposit API it creates a draft, uploads the zip, `ALL.TXT` and
`description.md`, and sets the metadata: resource type dataset, licence CC-BY-4.0, the community,
and creators taken from the operator fields.  It never calls publish.  It writes the draft's id
into the package directory, so a second run resumes that draft rather than creating another.  It
prints the draft's URL; the operator reviews the draft on Zenodo and publishes it, which hands it
to the community's curators.

`draft` refuses, naming the reason and the fix, when the site's upload mode for meteor-scatter
reads anything but `upload`, or when no token exists.

### 5.3 To settle before code lands

- Zenodo's identifier for the community.  The MSQP documents give only its display name.
- Whether Zenodo, now on InvenioRDM, still accepts a community in draft metadata, or instead
  takes it as a review request at publish time.  One read-only call against
  `sandbox.zenodo.org` with a test token answers both.

## 6. Settings and sigmond

The instance config gains:

```toml
[msqp]
archive_max_days = 90
archive_max_gb   = 2
operator         = ""                     # names and calls
contact_email    = ""
antenna          = ""
comments         = ""                     # optional

[radiod.msk144.scale_db]                  # keyed by each entry of freqs_hz; calibrate writes these
"28145000" = 0.0
"50260000" = 0.0
```

`config show` and `config apply` cover the new keys as they cover the existing ones.

`smd status` and the client's inventory report, per band: the archive's size and oldest day,
decodes kept today, the noise floor against its calibrated level, the front-end status, and any
open warning.  `deploy.toml` names the archive directory so sigmond's disk accounting sees it.

## 7. Failures

Each failure reports once and stays visible in `status`; none hides.

| Failure | Behaviour |
|---|---|
| Disk full, or the archive unwritable | Decoding and spots continue; archiving stops with one warning and resumes by itself once a write succeeds |
| A move into the archive fails | The client logs it, deletes the recording, and counts it in `status` |
| Front-end status goes quiet | The sidecar marks the stretch `missing` (§4) |
| `package` or `draft` refuses | It names the reason and the fix, and exits non-zero |

## 8. Tests

Unit tests:

- the keep-or-delete decision, watched to fail against a mutant that keeps every recording
- pruning against each cap
- float-to-16-bit scaling and the clip count
- the front-end step windows, from a synthetic sequence of status packets, including a gap
- the noise-floor watch: one warning on the change, one on recovery, none in between
- packaging from a fixture archive: title, `ALL.TXT` format, description fields, the refusal on a
  missing operator field
- `draft` against mocked HTTP: create, resume without a second draft, and each refusal

Zenodo sandbox: `draft` runs end to end against `sandbox.zenodo.org` with a test token and must
leave an unpublished draft in the community.  No test touches production Zenodo.

On B4, after an announcement on the claude-bus: deploy, run `calibrate`, and prove with an
MSK144 test transmission on an antenna close enough to hear it (as on 2026-07-24) that a decode
lands in the archive with its sidecar.  Then watch a day of front-end steps arrive in the
sidecars.

## 9. Order of delivery

Capture, archive, sidecar and the noise-floor watch ship first; they start filling the archive.
`package` and `draft` follow.  Should v3.73 run late, the second pair can come in a point
release, since the archive holds 90 days.
