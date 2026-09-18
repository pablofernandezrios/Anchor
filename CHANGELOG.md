# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Repository layout, packaging skeleton and project documentation (SPEC 19).
- Continuous integration: ruff, mypy in strict mode, pytest, and a guard that
  keeps the root daemons on the standard library (SPEC 5.4, 18).
- IPC protocol module with schema validation and stable error codes (SPEC 5.2).
- Persistent store with atomic writes and HMAC integrity checks (SPEC 6.1).
- Time reconciliation across boots and suspends, with clock-tamper detection
  (SPEC 6.2).
- Session model with the three levels and the ratchet (SPEC 7.2 to 7.4).
- Engine service authorising clients with `SO_PEERCRED` (SPEC 5.2).
- `anchor status` and `anchor start` in Soft mode, without real blocking
  (SPEC 15).
- Milestone 0 technical spikes and their findings (`docs/spikes/`).

[Unreleased]: https://github.com/pablofernandezrios/anchor/commits/main
