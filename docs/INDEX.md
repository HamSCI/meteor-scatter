# meteor-scatter documentation index

> **Audience:** all
> **Status:** current
> **Verified against:** meteor-scatter 21cd2f4 on 2026-08-23 — code
> **Canonical for:** the map of this repo's docs

★ = canonical; when two docs disagree the ★ one wins. Suite-wide front door:
[HamSCI/sigmond docs](https://github.com/HamSCI/sigmond/blob/main/docs/README.md).

| Doc | Audience | What it gives you |
|-----|----------|-------------------|
| ⚠ | all | ARCHITECTURE/CONFIG/INSTALL/OPERATIONS/SIGMOND-CONTRACT are a stale copy of psk-recorder's text (they say FT8/FT4; this client decodes MSK144 via `jt9 --msk144`). `REQUIREMENTS.md` is the one accurate document. Truthing is scheduled (docs program Phase 3). |
| [../README.md](../README.md) | all | what this client is: FT4/FT8 spot recorder and PSK Reporter uploader for ka9q-radio (see warning above) |
| [ARCHITECTURE.md](ARCHITECTURE.md) ★ | contributor | internals: the high-level pipeline |
| [CONFIG.md](CONFIG.md) ★ | operator/contributor | TOML config reference |
| [INSTALL.md](INSTALL.md) | contributor | production install: deps, systemd |
| [OPERATIONS.md](OPERATIONS.md) ★ | operator/contributor | running it day-to-day: starting/stopping, logs, health, troubleshooting |
| [REQUIREMENTS.md](REQUIREMENTS.md) | contributor | formal requirements, reconciled to code — the accurate document (MSK144, not FT4/FT8) |
| [SIGMOND-CONTRACT.md](SIGMOND-CONTRACT.md) ★ | contributor | conformance map to sigmond's CLIENT-CONTRACT |
