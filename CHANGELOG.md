# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-30

First public release.

### Added
- Async (`AsyncQuMixer`) and blocking (`QuMixer`) clients for Allen & Heath Qu
  series mixers (Qu-16, Qu-24, Qu-32, Qu-Pac, Qu-SB) over MIDI-on-TCP.
- Full parameter registry with golden-vector-tested NRPN encoding, unit
  tapers, a local state cache with freshness tracking, change events, scene
  recall handling, snapshot/diff, live metering and a raw MIDI-strip sub-API.
- `qumixer-py` command line tool: `probe`, `set`, `set-name`, `monitor`,
  `dump-state`, `recall`, `record`, `replay`.
- `FakeQu`, an in-process fake console for testing without hardware.
- Opt-in hardware conformance suite and taper calibration tool.

### Known limitations
- No parameter has yet been verified against physical hardware (nothing is at
  `Tier.VERIFIED`) and several tapers are interpolations. See
  `docs/tiers.md` and `docs/hardware-verification.md`.
