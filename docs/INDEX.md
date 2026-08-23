# meteor-scatter documentation index

> **Audience:** all
> **Status:** current
> **Verified against:** meteor-scatter bac2116 on 2026-08-23 — code
> **Canonical for:** the map of this repo's docs

★ = canonical; when two docs disagree the ★ one wins. Suite-wide front door:
[HamSCI/sigmond docs](https://github.com/HamSCI/sigmond/blob/main/docs/README.md).

| Doc | Audience | What it gives you |
|-----|----------|-------------------|
| [../README.md](../README.md) | all | what this client is: an MSK144 meteor-scatter ping recorder/decoder for ka9q-radio, and how to install and configure it |
| [ARCHITECTURE.md](ARCHITECTURE.md) ★ | contributor | internals: the record → `jt9 --msk144` → callhash → `psk.spots` pipeline, module by module |
| [CONFIG.md](CONFIG.md) ★ | operator/contributor | every TOML key and environment variable, with its default and where the code reads it |
| [INSTALL.md](INSTALL.md) ★ | operator/contributor | install and upgrade: prerequisites, `install.sh` step by step, paths, multi-radiod, uninstall |
| [OPERATIONS.md](OPERATIONS.md) ★ | operator/contributor | running it day-to-day: control, logs, `smd watch meteor`, health signs, failure modes, restart cost |
| [REQUIREMENTS.md](REQUIREMENTS.md) ★ | contributor | the formal requirements register (`MTS-*` IDs, gaps, traceability), reconciled to code 2026-06-25 — a few details have since drifted (see SIGMOND-CONTRACT.md "Known drift") |
| [SIGMOND-CONTRACT.md](SIGMOND-CONTRACT.md) ★ | contributor | section-by-section conformance map to sigmond's CLIENT-CONTRACT v0.8, including the gaps and the product-file drift list |
