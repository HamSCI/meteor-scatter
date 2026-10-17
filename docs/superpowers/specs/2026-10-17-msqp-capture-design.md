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
draft.  The operator submits it for review and a community administrator publishes it.  People
make those last two moves because Zenodo forbids any change to a record after publication.

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
| Zenodo | The station builds a draft through the API with the operator's token; the operator submits it for review; a community administrator publishes |
| Zenodo account | Each operator's own |
| Where the code lives | Inside meteor-scatter: archive in the daemon, two new commands |

## 3. Capture and archive

### 3.1 The channel

For each band the client asks radiod for a channel with AGC off and 32-bit float samples
(`ensure_channel(agc_enable=0, gain=0.0, encoding=F32, …)`).  The channel already streams 32-bit
float, because `receiver_manager` forces f32.  ka9q-python's `create_channel` already sends
`AGC_ENABLE=0` and `GAIN=0.0`.  The client will pass both explicitly, so the request stays
visible in the code.  One check stays pending.  A passive status-packet read on B4's live
channels will confirm that radiod reports AGC off.

### 3.2 From float to WAV

The client writes the 16-bit, 12 kHz WAV that jt9 already reads.  The decoder and the archive
hold the same audio.  A calibrated band uses its fixed scale (`scale_db`).  Until someone
calibrates a band, the writer keeps today's per-slot peak-normalization to −1 dBFS, so decoding
never suffers.  Every sidecar records `wav_gain_db`, the gain the writer applied to that
recording: int16 = sample × 32767 × 10^(wav_gain_db/20).  Each recording therefore stays reducible
to radiod's output level.  A ping strong enough to clip at 16 bits still clips; the client counts
the clipped samples and writes the count into the sidecar.

`meteor-scatter msqp calibrate` sets each band's scale.  It measures the band's noise over a few
minutes and chooses the scale that puts the noise about 50 dB below full scale, which leaves
room for strong pings.  Every sidecar from an uncalibrated band says `"scale": "uncalibrated"`.
Decoding never waits on calibration.

### 3.3 What the client keeps

When jt9 reports at least one decode, the client moves the recording out of the spool and into
the archive instead of deleting it:

```
<archive_dir>/<instance>/<band>/<YYYYMMDD>/<YYMMDD_HHMMSS>.wav
                                           <YYMMDD_HHMMSS>.json
```

`archive_dir` defaults to `/var/lib/meteor-scatter/archive`.  The instance level carries the
reporter id, made safe for a path.  Two radiods on one host would otherwise write the same
`<band>/<day>/<stem>.wav` and overwrite each other's recordings.  The file name follows WSJT-X's
own pattern; the next directory up carries the band.  A recording with
no decode goes away as it does today.  The `paths.keep_wav` setting, which kept every recording
without limit, retires.

### 3.4 The sidecar

Each `.json` sidecar holds:

- the reporter id, band and dial frequency
- the slot's UTC start and sample rate
- the band's scale, or `uncalibrated`
- `wav_gain_db`, the gain the writer applied to the recording (§3.2)
- `noise_floor_dbfs`, the band's noise floor (§3.6)
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
journal on the change, once again on recovery, and in the `msqp` block of `inventory --json`
while it stays open (§6).  The warning
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

One listener per instance reads the status packets radiod already multicasts.  ka9q's
`StatusListener` passes only timing fields to its callbacks, so meteor-scatter runs its own passive
listener and decodes each packet with `ka9q.status.decode_status_packet`.  It sends no poll
and no control traffic, and so cannot disturb T6 or any other client.  From each packet it takes
`rf_gain`, `rf_atten`, `rf_agc`, `if_power` and `ad_over`.  The listener times each reading by the host's receive time of the status packet, and
the sidecar names that basis.  For each kept recording the sidecar holds the reading in force at
the slot's start, every change during the slot, and the last reading.

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
- `description.md`, the MSQP description template filled in, for the record's description field
- `metadata.json`, the record exactly as `draft` will send it

It prints decodes per band per day, the file count, the zip's size, and the recordings that
clipped or overloaded.  The title takes the MSQP form, `<CALL> <GRID6> <YYYYMMDD-DD> Meteor
Scatter QSO Party`.

The description fields come from three places.  The station supplies the callsign (its reporter
id), the six-character grid (the first six characters of `[station] grid_square`), "Receive Only", the bands the archive holds, the
equipment (RX888 and radiod), and "NA" for transmitter power, as the MSQP template asks of a
receive-only station.  The client writes a comment
explaining the AGC setting, the front-end record in the sidecars, and the software version.  The
operator supplies, once, in the instance config's `[msqp]` block: operator names and calls, a
contact email, and the antenna.  `package` refuses while any of those three is missing and names
the missing one.

`package` writes only locally and always runs.

### 5.2 `meteor-scatter msqp draft <package-dir>`

The MSQP's procedure (github.com/HamSCI/MSQP, `Zenodo.md`) has the operator do everything in
Zenodo's web form: choose the community, upload the zip, `wsjtx_log.adi` and `ALL.TXT`, ask Zenodo
to generate a DOI, fill in the fields, and finally press "Submit for review".  A community
administrator then reviews the submission and either publishes it with its DOI or asks for more
information.  `draft` carries every step up to that last button, and stops there.

The command reads the operator's token from `/etc/meteor-scatter/zenodo-token` (mode 0600).  It
creates a draft and uploads the zip and `ALL.TXT`.  It sets the metadata the MSQP form asks for:
resource type dataset, access public, the MSQP title, the filled description, licence CC-BY-4.0,
and creators from the operator fields.  It reserves a DOI, which the web form calls "generate
DOI".  It attaches the draft to the community.  It never submits the draft for review and never
publishes it.  It writes the draft's id into the package directory, so a second run resumes that
draft rather than creating another.  It prints the draft's URL.  The operator opens the draft,
checks it, and presses "Submit for review"; the community administrator publishes.

The operator needs a Zenodo account and a personal access token.  The MSQP's procedure covers the
account: sign up with ORCID (which it recommends), GitHub, or an institutional email address.  The
token goes beyond that procedure, which never mentions one, so meteor-scatter's OPERATIONS.md
adds the step: in Zenodo, Applications, Personal access tokens, create a token with the scope
that writes deposits, and save it into `/etc/meteor-scatter/zenodo-token`.

`draft` refuses, naming the reason and the fix, when the site's upload mode for meteor-scatter
reads anything but `upload`, or when no token exists.

### 5.3 To settle before code lands

- Zenodo's identifier for the community.  The MSQP documents give only its display name.
- Which API carries the steps above.  The MSQP's "Submit for review" shows that Zenodo now runs
  InvenioRDM, where a community joins a record through a review request rather than a metadata
  field.  The plan expects InvenioRDM's records API (a draft, its files, a reserved DOI, and a
  review request created but not submitted); the legacy deposit API may lack the community
  step.  The exact token scopes follow from the same answer.
- Calls against `sandbox.zenodo.org` with a test token settle all three before code lands.

These questions stay open until the operator probes the sandbox.  `msqp draft` refuses while
`msqp.zenodo_community` is empty.

## 6. Settings and sigmond

The instance config gains:

```toml
[msqp]
archive_max_days = 90
archive_max_gb   = 2
zenodo_community = ""                     # the MSQP community's Zenodo id; draft refuses while empty
zenodo_token_path = "/etc/meteor-scatter/zenodo-token"
operator         = ""                     # names and calls
contact_email    = ""
antenna          = ""
comments         = ""                     # optional

[radiod.msk144.scale_db]                  # keyed by each entry of freqs_hz; calibrate writes these
"28145000" = 0.0
"50260000" = 0.0

[radiod.msk144.noise_cal_dbfs]            # same keys; the band's noise floor when calibrated
"28145000" = 0.0
"50260000" = 0.0
```

`config show` and `config apply` cover the new keys as they cover the existing ones.

The client's `inventory --json` reports MSQP state, per band: the archive's size and oldest day,
decodes kept today, the noise floor against its calibrated level, the front-end status, and any
open warning.  Rendering that state in `smd status` moves to a sigmond row on the v3.73 list.
`deploy.toml` names the archive directory so sigmond's disk accounting sees it.

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
leave a draft that carries its files, metadata and reserved DOI, joined to the community, and
not yet submitted for review.  No test touches production Zenodo.

On B4, after an announcement on the claude-bus: deploy, run `calibrate`, and prove with an
MSK144 test transmission on an antenna close enough to hear it (as on 2026-07-24) that a decode
lands in the archive with its sidecar.  Then watch a day of front-end steps arrive in the
sidecars.

## 9. Order of delivery

Capture, archive, sidecar and the noise-floor watch ship first; they start filling the archive.
`package` and `draft` follow.  Should v3.73 run late, the second pair can come in a point
release, since the archive holds 90 days.
