# Anchor

Anchor is a focus tool for the Linux desktop. It blocks websites and applications
during focus sessions, enforces breaks, and runs sessions on weekly schedules.

A block cannot be undone in a moment of weakness: leaving early costs time and
effort, and every escape is recorded.

> **Status: 0.1.0, in development.** The engine core is implemented and tested.
> Web blocking, application blocking, the GUI and the packages are not finished
> yet. Do not expect a working install from this branch. See
> [CHANGELOG.md](CHANGELOG.md) for what is done.

## What it does

- **Blocks websites** by domain, in blocklist or allowlist mode. A rule for
  `example.com` covers every subdomain and path.
- **Blocks applications**, including Snap and Flatpak, closing them when a
  session starts and killing them if they are launched during one.
- **Three levels of commitment**, chosen when the session starts.
- **Enforces breaks** with a notification or a fullscreen countdown.
- **Runs on a weekly schedule**, so focus time does not depend on willpower.
- **Keeps local statistics** so you can see where your attention goes.

## Levels

The level decides how hard it is to leave a session early. You pick it at the
start, and it cannot be lowered while the session runs.

| Level | Leaving early | Editing lists | VPN and Tor |
|---|---|---|---|
| **Soft** | Cancel after a 5-minute wait | Free | Allowed |
| **Firm** | Cancel after a long wait, then type a long random text | Add only | Allowed |
| **Strict** | Not possible. Emergency valve only. | Add only | Blocked |

In Strict sessions the only way out is the **emergency valve**, chosen when the
session starts: wait 30 minutes, type a freshly generated long phrase, or both.
Every use of the valve is recorded.

## Install

Packages for Debian/Ubuntu, Fedora and Arch ship with the first release. This
section will carry the real commands once `0.1.0` is tagged.

## Quick start

```sh
anchor start --profile Study --duration 2h30m --level soft
anchor status
anchor extend --by 30m
anchor cancel
```

Every action in the graphical interface is available on the command line, and
the reverse. Run `anchor doctor` if something looks wrong; it checks the DNS
path, the nftables table, browser policies, the daemons and the GNOME indicator,
and prints how to fix what it finds.

## Limitations

Read these before trusting Anchor with anything important.

- **Friction, not guarantees.** Anchor raises the cost of giving in; it does not
  make it impossible. Anyone with root on the machine can defeat it. The goal is
  that defeating it is tedious, requires understanding the architecture, and
  leaves a record.
- **Blocking works on domains, not paths.** Anchor sees DNS queries, so it can
  block `youtube.com` but not only `youtube.com/shorts`.
- **Blocked pages show the browser's normal connection error**, not a friendly
  Anchor page. Showing one would mean intercepting TLS, which is not worth the
  security cost.
- **Allowlist mode needs tuning.** Most sites load resources from other domains,
  so expect to add entries. A small built-in essentials list keeps connectivity
  checks and time sync working.
- **Breaks insist, they do not force.** Wayland gives no way for a program to
  take over your screen, and Anchor does not try. A break you cannot skip or
  postpone is announced before it starts, shown on the top bar, and covers
  every monitor when it begins — but you can still switch away from it. The
  clock keeps running either way.
- **VPN and Tor blocking is best effort.** Common protocols and known relays are
  blocked in Strict sessions; obfuscated tunnels can get through.
- **One user per machine.** Blocks apply to the whole system.
- **GNOME on Wayland is the reference desktop.** Other desktops may work but are
  not tested. GNOME on X11 is not supported.

## Privacy

Anchor is local only. There is no telemetry, no account and no cloud. The only
network traffic it creates is forwarding your DNS queries to the resolver your
network already uses.

Statistics live in a SQLite database on your machine, hold query frequency and
blocked-attempt counts rather than browsing history, are kept for 90 days by
default, and can be deleted entirely with one action. Visited domains never
appear in the system log.

## How this project was built

Anchor was written by an AI coding agent working from a specification
([`SPEC.md`](SPEC.md)) that the project owner wrote, approved and reviews. Design
decisions, deviations and trade-offs are recorded in [`docs/adr/`](docs/adr/).

## License

MIT. See [LICENSE](LICENSE).
