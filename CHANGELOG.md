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
- Web blocking: a forwarding DNS resolver, nftables redirection exempting the
  resolver, systemd-resolved integration that follows network changes, blocklist
  and allowlist modes with a shipped essentials list, managed browser policies
  disabling DNS-over-HTTPS, rejection of addresses resolved before a session, and
  blocked-site notifications limited to one per domain every ten minutes
  (SPEC 8).
- Full restoration of DNS, firewall rules and browser policies, whether or not a
  session is active, through `anchor-blockerd --restore` (SPEC 7.6).
- Leak tests proving a blocked domain cannot be reached through plain DNS, a
  public resolver, DNS over TLS, a shipped DoH endpoint or a cached address
  (SPEC 8.4).
- Anti-evasion: manual stops refused during sessions, the ratchet applied to
  live profile edits, ruptures recorded for a removed firewall table, and VPN
  and Tor blocking in Strict sessions (SPEC 5.3, 7.4, 7.6, 8.2).
- `anchor profile list|show|create|edit|delete` (SPEC 15).
- The graphical interface: GTK4 and libadwaita, the six screens the mockups
  draw, Start Session with its confirmation, and the first-run introduction
  that ends by blocking a sample domain and checking it really stopped
  resolving (SPEC 14).
- The top-bar menu, exported over `com.canonical.dbusmenu`, paying the debt
  ADR 3 recorded. "Open Anchor" opens the interface (SPEC 14.1).
- Spanish, through gettext, with `tools/po.py` to extract, compile and check
  the catalogue without the GNU gettext tools (SPEC 14).
- Settings the engine owns, with `anchor config get|set`. The Firm wait and
  the phrase length refuse to change while a session runs, in either
  direction (SPEC 7.2, 13, 15).
- `anchor doctor`: five checks and what to type for each (SPEC 15).
- `anchor category edit`, which saves your own copy of a category so package
  updates cannot overwrite it (SPEC 12, 15).
- Break patterns, types and hardness are editable through `profile.create`
  and `profile.edit` (SPEC 12).
- `tools/screenshot_gui.py`, which runs the interface against a real engine on
  a virtual display and photographs every screen, and `tools/check_gui.py` for
  the questions only a person at a real desktop can answer.

### Fixed

- The blocker now clears its own leftovers: a firewall table and browser
  policies that outlived the daemon that applied them — after a SIGKILL, a
  power cut or an upgrade mid-session — were left blocking a machine with no
  session behind them until the next reboot (P4, SPEC 7.6).

[Unreleased]: https://github.com/pablofernandezrios/anchor/commits/main
