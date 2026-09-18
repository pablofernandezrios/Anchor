# Anchor v1: Product and Technical Specification

- **Status:** Approved by owner, 2026-09-18
- **Owner:** Pablo (product owner, final say on every decision)
- **Audience:** the coding agent that builds Anchor, and future contributors
- **Rule:** this document is the source of truth. If the code needs to deviate, write an ADR in `docs/adr/` and ask the owner first.

---

## 1. Product summary

Anchor is a focus tool for the Linux desktop. It blocks websites and applications during focus sessions, enforces breaks, and runs sessions on weekly schedules. A block cannot be undone in a moment of weakness: leaving early costs time and effort, and every escape is recorded.

The owner builds it for personal daily use, but it is designed and published as a product for any average Linux desktop user.

### 1.1 Design principles

| ID | Principle | Consequence |
|---|---|---|
| P1 | GUI and CLI parity | Every action available in the GUI is available in the CLI, and the reverse. |
| P2 | Friction, not guarantees | Root can always defeat Anchor. The goal: defeating it is tedious, requires knowing the architecture, and is logged. |
| P3 | Ratchet | During an active session, only changes that make it stricter are accepted. |
| P4 | Fail-closed only while blocking | If a daemon dies during an active block, traffic stays blocked until systemd restarts it. With no active block, a crash must never break networking. |
| P5 | Local only | No telemetry, no cloud, no network calls except forwarding DNS. |
| P6 | No feature cuts for novices | Average users get a clear GUI and onboarding. Advanced features stay. |
| P7 | Wayland first | GNOME on Wayland is the reference desktop. |

---

## 2. Scope

### 2.1 In scope for v1

Web blocking (blocklist and allowlist), application blocking, three blocking levels, emergency valve, breaks with notifications or fullscreen overlay, weekly schedules, profiles, predefined categories, local statistics, GTK4 GUI, top-bar indicator, CLI, onboarding, English and Spanish UI, packages for Debian/Ubuntu, Fedora and Arch, CI/CD, public GitHub repository under MIT.

### 2.2 Out of scope for v1

| Item | Reason |
|---|---|
| Blocking by URL path (`youtube.com/shorts` only) | DNS sees domains, not paths. Needs a browser extension. Candidate for v2. |
| Custom "Blocked by Anchor" page | Needs a local certificate authority (TLS interception). Rejected for security. |
| Foreground time per application | GNOME on Wayland does not expose the focused window to other apps. Candidate for v2. |
| Time spent per website | DNS caching makes it unreliable. v1 reports frequency and blocked attempts. |
| Per-user blocking on multi-account machines | Target is a single-user machine. Blocks are global. |
| One-off dated schedules | v1 supports weekly recurring schedules only. |
| Health reminders outside sessions | Breaks exist only inside focus sessions. |
| GNOME on X11 | Removed upstream. Other X11 desktops may work but are not tested. |
| Snap and Flatpak packaging | Sandboxes cannot manage host networking as root. |

---

## 3. Target platform

| Item | Requirement |
|---|---|
| Distributions | Ubuntu 24.04 LTS and later, Debian 13, current Fedora Workstation, Arch Linux |
| Reference system | Ubuntu 26.04 LTS, GNOME 50, Wayland |
| Init | systemd (required) |
| Firewall | nftables (required) |
| Python | 3.12 minimum |
| Desktop | GNOME on Wayland (primary). Others: best effort. |
| Hardware | Laptops that change networks often must work without manual action. |
| Users | One human user per machine. Blocks apply machine-wide. |

---

## 4. Glossary

| Term | Meaning |
|---|---|
| Session | A period during which a profile's blocks are active. Started manually or by a schedule. |
| Profile | Named set of: web rules, app list, break settings. Example: "Study". |
| Level | How hard a session is to leave: Soft, Firm, Strict. Chosen at session start. |
| Valve | Emergency exit, only in Strict. Chosen at session start. |
| Break hardness | How hard a break is to skip: Flexible, Moderate, Mandatory. Set per profile. Independent of level. |
| Schedule | Weekly recurring time window that starts a session automatically. |
| Skip | Ending an active scheduled session early. Limited to 3 per week. |
| Ratchet | Rule that only stricter changes apply during a session. |
| Rupture | Any escape or tampering event: valve used, schedule skipped, manipulation detected. Always recorded. |

---

## 5. Architecture

### 5.1 Processes

Separate processes by privilege boundary. Blocking needs root. Notifications and UI need the user session.

| Component | Binary | Runs as | Lifetime | Responsibility |
|---|---|---|---|---|
| Engine | `anchord` | root, system service | Always | Central coordinator. Sole owner of config, state, schedules, session timers and statistics. |
| Blocker | `anchor-blockerd` | root, system service | Always | DNS resolver, nftables rules, browser policies, app enforcement. Executes engine orders. |
| Agent | `anchor-agent` | user, user service | User session | Notifications, break timer UI, fullscreen break overlay, idle and resume events. |
| GUI | `anchor-gui` | user | On demand | GTK4 client. No business logic. |
| Indicator | part of `anchor-agent` | user | User session | Top-bar indicator and menu. |
| CLI | `anchor` | user | Per command | Translates commands into IPC messages. No business logic. |

Topology is a star. Every component talks only to the engine. The engine is the only writer of persistent state.

### 5.2 IPC

- Transport: `AF_UNIX`, `SOCK_STREAM`.
- Engine socket: `/run/anchor/engine.sock`.
- Framing: one JSON object per line (`\n` terminated), UTF-8.
- Request: `{"v":1,"id":"<uuid>","type":"session.start","payload":{...}}`
- Response: `{"v":1,"id":"<same>","ok":true,"result":{...}}` or `{"v":1,"id":"<same>","ok":false,"error":{"code":"RATCHET_VIOLATION","message":"..."}}`
- Events (engine to subscribers): `{"v":1,"event":"session.tick","payload":{...}}`. Clients subscribe with `events.subscribe`.
- Every order receives an explicit acknowledgement. No fire-and-forget.
- Authorization: the engine reads the peer identity with `SO_PEERCRED`. Accepted: UID 0 and the owner UID recorded at install time. Everyone else is rejected. No passwords in normal use.
- Protocol messages are defined once in a shared module with schema validation. Unknown fields are rejected.

### 5.3 systemd

- `anchord.service` and `anchor-blockerd.service`: `Restart=always`, `RestartSec=1`.
- `anchor-agent.service`: user unit, enabled for the owner.
- During an active Soft/Firm/Strict session the engine writes a runtime drop-in under `/run/systemd/system/<unit>.d/` with `RefuseManualStop=yes` for both root units, then reloads systemd. The drop-in is removed when the session ends. `/run` is cleared on reboot; the engine recreates it on boot if a session is still active.

### 5.4 Runtime dependencies

- Root daemons: Python standard library only, plus distribution-packaged bindings when unavoidable (`python3-nftables` or calls to `nft -j`). No pip packages run as root.
- User components: PyGObject, GTK4, libadwaita, AppIndicator/StatusNotifier bindings from distribution packages.

---

## 6. Persistence and time

### 6.1 Files

| Path | Owner | Content |
|---|---|---|
| `/etc/anchor/anchor.toml` | root | Install-level settings: owner UID, retention, paths. Human-readable. |
| `/var/lib/anchor/config.json` | engine | Profiles, lists, schedules, preferences. Written only through the API. |
| `/var/lib/anchor/state.json` | engine | Active session, skips used this week, pending valve requests. |
| `/var/lib/anchor/stats.db` | engine | SQLite statistics. |
| `/var/lib/anchor/.key` | root, 0600 | HMAC key for integrity checks. |

- Writes are atomic: temporary file, `fsync`, `rename`.
- `config.json` and `state.json` carry an HMAC. A mismatch during an active session is a rupture; the engine keeps the stricter interpretation (P4).
- Editing files by hand during a session has no effect on the running session.

### 6.2 Time rules

- Session end is stored as an absolute UTC timestamp (`ends_at`).
- Time runs during suspend. A session that ends while the laptop sleeps is over on resume.
- On boot and on resume, the engine reconciles: expired sessions end, active ones resume with the remaining time.
- Clock tampering: the engine tracks elapsed time with `CLOCK_BOOTTIME` within a boot. A wall-clock jump that disagrees with boot time is logged as a rupture, and the session keeps the boot-time-based end.

---

## 7. Sessions and levels

### 7.1 Starting a session

1. The user picks a profile, duration, level and (for Strict) valve.
2. Manual sessions: maximum duration at start is **8 hours**.
3. A confirmation dialog lists the consequences: end time, how to exit, VPN/Tor blocking (Strict), and which apps will close.
4. On confirmation, apps in the profile that are running get a notification and **2 minutes** to save work. Then `SIGTERM`, then `SIGKILL` after 10 seconds.

### 7.2 Levels

| Level | Cancel | Change lists during session | VPN and Tor |
|---|---|---|---|
| Soft | After a 5-minute wait | Free | Allowed |
| Firm | After a long wait plus typing a long random text | Add only | Allowed |
| Strict | Not possible. Valve only. | Add only | Blocked |

Default Firm wait: 15 minutes. Default random text: 150 characters. Both configurable in settings, never during a session.

### 7.3 Extending

- Any session can be extended by any amount. The 8-hour cap applies only at start.
- Every extension shows the new end time and asks for confirmation.

### 7.4 Ratchet

During a session, accepted: add domains, add apps, extend. Rejected with `RATCHET_VIOLATION`: remove, shorten, lower level, change valve, switch allowlist to blocklist.

### 7.5 Valve (Strict only)

Chosen at start, cannot change during the session:

- **Wait:** request unlock, wait 30 minutes. The request can be withdrawn.
- **Random phrase:** type a long phrase generated fresh each time. Paste is disabled in the GUI; the CLI reads from the terminal.
- **Both.**

Every valve use is a rupture.

### 7.6 Tampering and uninstall

- `systemctl stop` on root units is refused during sessions (5.3).
- Detected manipulation (HMAC mismatch, missing nftables table, clock jump) is a rupture and the engine restores rules.
- **Uninstalling is always allowed.** The package removal scripts restore DNS configuration, nftables, browser policies and systemd drop-ins, whether or not a session is active.

---

## 8. Web blocking

### 8.1 Rules

- Profile mode: **blocklist** (block listed domains) or **allowlist** (block everything except listed domains).
- A rule for `example.com` covers all its subdomains and every path.
- Allowlist mode shows a clear warning: many sites load resources from other domains and need extra entries. Anchor always allows a small built-in "essentials" list (connectivity checks, time sync) so the system keeps working.

### 8.2 Mechanism

- **Resolver:** `anchor-blockerd` runs a local forwarding DNS resolver. It parses only the question section, answers `NXDOMAIN` for blocked names, and forwards everything else unchanged. Implemented with the standard library.
- **Integration:** Anchor sits in front of the system resolver without replacing it. With systemd-resolved: a drop-in routes all queries to Anchor, and Anchor forwards to the DNS servers of the current network link. Network changes must be picked up automatically. Distributions without systemd-resolved need an equivalent path. Validate both in Milestone 0.
- **nftables:** table `inet anchor`. Redirect all outbound DNS (UDP/TCP 53) from any process except the resolver itself to the local resolver. During sessions: reject DNS over TLS (853) and DNS-over-HTTPS endpoints from a shipped list.
- **Browser DoH:** install managed policies that disable DoH in Firefox (including the Snap build) and Chromium-based browsers (Chrome, Chromium, Brave, Edge). Validate paths per packaging format in Milestone 0.
- **Open connections:** the resolver keeps a short-lived map of recent answers. At session start, connections to IPs of blocked domains are dropped (conntrack) and those IPs are rejected for the session.
- **VPN and Tor (Strict only):** block common VPN protocols (WireGuard, OpenVPN, IPsec default ports) and Tor relay traffic using a shipped list. Documented as best effort.

### 8.3 User feedback

- Blocked sites show the browser's standard connection error.
- A notification tells the user Anchor blocked the site: at most **one per domain every 10 minutes**.
- The indicator shows the blocked-attempt count for the session.

### 8.4 Leak testing

Automated tests must prove a blocked domain does not resolve through: plain DNS, direct queries to public resolvers, DNS over TLS, DoH endpoints on the list, and cached IPs.

---

## 9. Application blocking

- Blocklist only. No allowlist for apps.
- The GUI lists installed applications (from `.desktop` entries, including Snap and Flatpak exports) with icon and name. The user ticks the ones to block.
- Identification by executable path and cgroup (`snap.<name>.*`, `app-flatpak-<id>-*`), never by process name.
- Detection: kernel process events (netlink proc connector) plus a 2-second polling fallback.
- At session start: 2-minute grace period with notification (7.1).
- During the session: a blocked app is killed immediately on launch, with a notification.
- Categories can bundle an app with its domains. Example: "Discord" blocks the app and `discord.com`.

---

## 10. Breaks

- Breaks exist only during sessions.
- Patterns: 25/5, 50/10, 90/20, custom. Optional long break every N cycles.
- Type, per profile: notification only, or fullscreen overlay with countdown on all monitors.
- Hardness, per profile, independent of session level:

| Hardness | Skip | Postpone |
|---|---|---|
| Flexible | Yes | Yes, unlimited |
| Moderate | No | Once, 5 minutes |
| Mandatory | No | No |

- Breaks do **not** unblock sites or apps. A profile option can allow blocked sites during breaks.
- The work timer keeps running during idle and suspend. If an absence covers a break, the break counts as taken and a new work cycle starts.

---

## 11. Schedules

- Weekly recurring: days of week plus a time window.
- Each schedule sets its profile, level and (Strict) valve.
- The 8-hour cap does not apply to schedules.
- Before a scheduled session starts, the schedule can be edited or deleted freely.
- Once active, a Soft or Firm scheduled session can be **skipped**. Limit: **3 skips per week**, reset Monday 00:00 local time. Every skip is a rupture.
- A Strict scheduled session cannot be skipped. Only its valve applies.
- If the machine boots late, the session starts immediately with the remaining time.
- Overlapping schedules: rules are merged and the strictest level wins.
- Schedules are evaluated by the engine, not by systemd timers.

---

## 12. Profiles and categories

- A profile contains: web mode, enabled categories, custom domains, blocked apps, break pattern, break type, break hardness, "allow sites during breaks" flag.
- Shipped categories: Social media, Video, News, Games, Shopping. Stored as data files, editable by the user, updated with the package.

---

## 13. Statistics

- Stored locally in SQLite. No telemetry.
- Views: day, week, month.
- Metrics: focus hours, sessions completed, blocked attempts by domain and by app, breaks taken/postponed/skipped, ruptures by type (valve, skip, tampering).
- Web data: query frequency and blocked attempts only. No time per site.
- Retention: configurable, default 90 days. One action deletes all statistics.
- Logs (`journalctl`) never contain visited domains.

---

## 14. GUI

- GTK4 + libadwaita via PyGObject. Native GNOME look. Follows system light/dark theme.
- UI languages: English (default) and Spanish, via gettext.
- Screens: Home, Profiles, Schedules, Lists, Statistics, Settings, plus Start Session, Confirmation and fullscreen Break.
- Reference mockup: `docs/mockups/` (exported from the approved design canvas).
- Start Session is a single screen with profile defaults preloaded and a confirmation dialog.
- In Strict sessions, Home replaces "Cancel session" with "Emergency valve".
- Onboarding on first run: explains levels, valve and allowlist; ends with a live test that blocks a sample domain and verifies it.
- Accessibility: keyboard navigation, screen reader labels, WCAG AA contrast.

### 14.1 Top-bar indicator

- Always visible while a session runs: icon plus remaining time.
- Menu: profile and level, remaining time and end time, next break, blocked attempts, "Extend session", "Open Anchor".
- Uses StatusNotifierItem/AppIndicator. Ubuntu ships the required GNOME extension. On other GNOME distributions, onboarding detects its absence and explains how to install it.
- No floating window.

---

## 15. CLI

Full parity with the GUI. Human-readable output by default; `--json` on every command.

```
anchor status
anchor start --profile NAME --duration 2h30m --level soft|firm|strict [--valve wait|phrase|both]
anchor extend --by 30m
anchor cancel                     # Soft/Firm, with the level's friction
anchor valve request|withdraw|phrase
anchor skip                       # active Soft/Firm schedule
anchor profile list|show|create|edit|delete
anchor category list|show|edit
anchor schedule list|show|create|edit|delete
anchor stats [--day|--week|--month]
anchor config get|set
anchor doctor
```

- `anchor doctor` checks DNS path, nftables table, browser policies, daemons, indicator extension, and prints fixes.
- Stable exit codes, documented in `docs/cli.md`.

---

## 16. Security model

| Threat | Mitigation | Residual risk |
|---|---|---|
| User stops services | `RefuseManualStop` drop-in during sessions | Root can remove the drop-in |
| User edits state files | HMAC, engine restores stricter state, rupture logged | Root can read the key |
| User changes system clock | `CLOCK_BOOTTIME` tracking, rupture logged | Reboot plus clock change is harder to detect |
| Browser DoH | Managed policies plus DoH endpoint blocking | Unknown DoH endpoints |
| VPN or Tor (Strict) | Port and relay list blocking | Obfuscated tunnels |
| Other local user | Out of scope (single-user target) | Documented |
| Malicious local process sends IPC | `SO_PEERCRED` UID check | Processes running as the owner |
| Dependency supply chain as root | Stdlib only in root daemons | Distribution packages |

The README documents that Anchor provides friction, not guarantees.

---

## 17. Packaging and distribution

- `.deb` and `.rpm` attached to every GitHub release.
- PPA for Ubuntu, COPR for Fedora, AUR package for Arch.
- Install scripts: create directories, record owner UID, enable services.
- Removal scripts: full network and policy restoration (7.6).
- Package upgrades during a session restart daemons; the session survives from disk state.

---

## 18. Quality and CI/CD

- Formatting and lint: ruff. Types: mypy in strict mode for core packages. Tests: pytest.
- Coverage target: 80% for engine logic (sessions, ratchet, schedules, breaks, time).
- CI on GitHub Actions for every push and pull request:
  - lint, types, unit tests;
  - package builds;
  - install tests in containers: Ubuntu 24.04, Ubuntu 26.04, Debian 13, Fedora, Arch;
  - end-to-end tests on a VM runner with real nftables: start session, leak tests (8.4), daemon restart, uninstall leaves networking clean.
- Releases: pushing a version tag builds packages, generates the changelog, publishes signed checksums.

---

## 19. Repository

- Public GitHub repository on the owner's personal account. License: MIT.
- Everything in English: code, comments, commits, issues, documentation, README.
- Conventional Commits.
- Required files: `README.md`, `LICENSE`, `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `CHANGELOG.md`, issue and PR templates, `docs/architecture.md`, `docs/cli.md`, `docs/adr/`.
- README contents: what it does, screenshots, install per distribution, quick start, levels explained, limitations (friction not guarantee, DNS granularity, Wayland notes), privacy statement, and a clear note that the project was developed by an AI coding agent under the owner's specification and review.

---

## 20. Versioning

- Semantic Versioning.
- `0.x` while the owner uses it daily for 2 to 4 weeks.
- `1.0.0` when no severe issues appear in that period.

---

## 21. Pending owner confirmations

Defaults chosen by the spec author, to confirm or change:

1. Firm level wait: 15 minutes.
2. Random text length: 150 characters.
3. Statistics retention: 90 days.
