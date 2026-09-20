# Architecture

How Anchor is put together, kept in step with the code. If this page and the
code disagree, one of them is a bug.

## Shape

Five components, split along the one line that matters: blocking needs root,
and anything the user sees needs the user's session.

```
        anchor (CLI)  anchor-gui  anchor-agent + indicator
                \          |          /
                 \         |         /          user session
        ──────────────── AF_UNIX ─────────────────────────────
                          |                      root
                       anchord  ───────►  anchor-blockerd
                     (the engine)         (DNS, nftables,
                                           policies, apps)
```

The topology is a star. Everything talks to the engine and nothing talks to
anything else. The engine is the only writer of persistent state, which is what
makes the ratchet and the integrity checks meaningful: there is one place where
a change can be refused.

| Component | Binary | Runs as | Status |
|---|---|---|---|
| Engine | `anchord` | root, system service | Implemented (Milestone 1) |
| Blocker | `anchor-blockerd` | root, system service | Web blocking done (Milestone 2); applications in Milestone 4 |
| Agent and indicator | `anchor-agent` | the owner, user service | Milestone 5. Speaks StatusNotifierItem over D-Bus, no AppIndicator library ([ADR 3](adr/0003-speak-statusnotifieritem-over-dbus.md)) |
| Interface | `anchor-gui` | the owner | Milestone 8 |
| Command line | `anchor` | any allowed user | `status`, `start`, `extend`, `cancel`, `valve` |

## Modules

```
src/anchor/
  protocol/    types, error codes, schema validation, message envelopes
  engine/      paths, store, timekeeping, sessions, ratchet, profiles,
               phrases, state, core, service, main
  blocker/     journal, restore, constants, commands, dnswire, matcher,
               recent, attempts, resolver, rules, resolved, policies,
               apps, processes, watcher, daemon, main
  agent/       empty
  gui/         empty
  cli/         durations, client, main
```

`protocol`, `engine` and `blocker` end up inside root processes, so they are
restricted to the standard library (SPEC 5.4). This is enforced by
`tests/unit/test_stdlib_only.py`, which walks each module's syntax tree and
fails on any import that is neither stdlib nor Anchor. Adding a dependency there
needs an ADR.

## Talking to the engine

`AF_UNIX`, `SOCK_STREAM`, one JSON object per line, UTF-8, at
`/run/anchor/engine.sock`.

```json
{"v":1,"id":"<uuid>","type":"session.start","payload":{...}}
{"v":1,"id":"<uuid>","ok":true,"result":{...}}
{"v":1,"id":"<uuid>","ok":false,"error":{"code":"RATCHET_VIOLATION","message":"..."}}
{"v":1,"event":"session.tick","payload":{...}}
```

Every order gets an explicit acknowledgement. Unknown fields are rejected rather
than ignored, so a client's typo fails loudly. A client that sends
`events.subscribe` turns its connection into a one-way event feed.

**Who may speak.** The engine reads the peer's credentials from the kernel with
`SO_PEERCRED` and accepts root and the owner UID recorded at install. A client
never sends an identity, so it cannot claim one. The socket's own permissions
are defence in depth, not the check.

## State on disk

| Path | What |
|---|---|
| `/etc/anchor/anchor.toml` | Owner UID and retention. Written at install, editable by hand. |
| `/var/lib/anchor/config.json` | Profiles, lists, schedules. Signed. |
| `/var/lib/anchor/state.json` | Active session, skips this week, pending exits. Signed. |
| `/var/lib/anchor/stats.db` | Statistics. Milestone 7. |
| `/var/lib/anchor/.key` | HMAC key, mode 0600. |

Writes go to a temporary file, are flushed with `fsync`, renamed over the
target, and the directory is synced too, so a power cut leaves the old file
rather than half of a new one.

`config.json` and `state.json` carry an HMAC. A mismatch during a session is a
rupture, and the engine keeps the stricter reading: the recorded session stands
rather than being dropped, so editing the file is not a way out. The signature
is not a security boundary — root can read the key, as SPEC 16 says plainly. It
makes tampering visible, not impossible.

`ANCHOR_ROOT`, or `--root`, relocates the whole tree for development.

## Time

Session ends are absolute UTC timestamps, but the wall clock belongs to the
user, so it cannot be the only authority.

Within one boot, `CLOCK_BOOTTIME` decides. It counts time spent suspended,
which is what makes "a session that ends while the laptop sleeps is over on
resume" true, and it cannot be set. The engine compares it against the wall
clock on every tick; a disagreement beyond 60 seconds is tampering, recorded as
a rupture, and the boot clock's answer is kept.

When that happens both wall-clock readings are re-pegged to the clock the
machine now shows. Re-pegging only the end would leave the origin stale and
every later tick would report the same jump as a fresh rupture.

Across a reboot `CLOCK_BOOTTIME` restarts, so the absolute timestamp is all
that survives. SPEC 16 records that as a known gap: a reboot plus a clock
change is harder to detect.

## Sessions

A `Session` is immutable. Every transition returns a new one, so a change can
be validated and persisted before it is swapped in, and a refused change cannot
leave a half-applied session behind.

**Leaving** always costs waiting and typing, in amounts the level sets:

| Level | Wait | Phrase |
|---|---|---|
| Soft | 5 minutes | no |
| Firm | 15 minutes (configurable) | 150 characters (configurable) |
| Strict | cancelling is refused; the valve applies | |
| Valve: wait | 30 minutes | no |
| Valve: phrase | none | yes |
| Valve: both | 30 minutes | yes |

A pending exit takes effect by itself once its wait runs out, unless withdrawn.
SPEC 7.2 and 7.5 describe leaving as "cancel after a wait" and "request unlock,
wait 30 minutes", with no second confirmation; requiring one would quietly turn
a 30-minute wait into an indefinite one for anyone who walks away. **This
reading is flagged for the owner.**

Waits are measured against the boot clock, so moving the wall clock forward
does not satisfy one.

**The ratchet** refuses anything that loosens a running session. Its sense
inverts for allowlists: there the list names what stays reachable, so adding an
entry opens a site and removing one closes it. Handling both directions with
one rule is what stops an allowlist session being unwound one entry at a time.

## Web blocking

Four mechanisms, layered, because any one of them alone has a way around it.

**The firewall is the universal one.** `nft` loads a single table, `inet
anchor`, whose first rule returns on Anchor's own mark and whose second
redirects every UDP and TCP query on port 53 to Anchor's resolver. It catches
every process regardless of what `/etc/resolv.conf` says, so blocking works on
a machine with no systemd-resolved at all. The mark exemption has to be first,
or the resolver's own forwarded queries come straight back to it; `SO_MARK`
needs `CAP_NET_ADMIN`, so no ordinary process can claim it.

**The resolver decides.** It reads only the question, answers `NXDOMAIN` for a
blocked name so the browser shows its ordinary error page, and forwards
everything else as bytes. When no upstream answers it says `SERVFAIL` rather
than `NXDOMAIN`, because claiming a name does not exist is a lie clients cache.
The one thing it reads out of an answer is the address records, for the
short-lived map below.

**systemd-resolved is pointed at Anchor** where it exists, per link with a `~.`
routing domain and a runtime drop-in as backstop. That saves a hop, and
`Cache=no` stops a domain blocked mid-session from resolving out of the cache.
Where resolved does not exist, upstreams come from `/etc/resolv.conf`. Anchor
refuses to redirect DNS when it can find no upstream at all: a redirect with
nowhere to forward is a total loss of name resolution, far worse than not
blocking, and indistinguishable from it to the user.

**Encrypted DNS is closed off.** Managed policies disable DoH in Firefox and
the Chromium family, and the firewall rejects DNS over TLS and the shipped DoH
endpoints on both TCP and UDP 443. The policies only apply when a browser
starts, so the firewall is what covers a browser already running.

Addresses resolved just before a session started are rejected for its duration,
from a bounded in-memory map that expires on its own and is emptied when the
session ends. Without it, a page already open keeps loading.

`tests/system/leak_test.py` proves all five routes in SPEC 8.4 are closed, on a
real machine, on every push.

### Fail-open, concretely

Nothing that can break networking outlives Anchor.

- With no session there are no rules at all, so an idle blocker that dies
  cannot take the network with it.
- During a session the rules stay in the kernel even if the daemon dies, so
  traffic stays blocked until systemd restarts it (P4).
- The resolved drop-in lives in `/run`, so a reboot clears it. Per-link DNS is
  runtime state for the same reason.
- Everything Anchor writes goes through a journal that records what was there
  before, and `anchor-blockerd --restore` puts it all back. The journal is
  unsigned on purpose: uninstalling is always allowed, and an integrity check
  could only refuse to give someone their machine back.

## Application blocking

Three questions, answered by three modules, because each has a different way of
being wrong.

**What is installed?** `apps.py` reads `.desktop` entries from the system and
user directories, including the ones Snap and Flatpak export. An entry gives a
name, an icon and a command; the command is stripped of its field codes and any
`env VAR=value` prefix, and then resolved to an absolute path. Resolving it at
discovery time is deliberate: entries commonly say `vim` rather than
`/usr/bin/vim`, and a bare name cannot be compared against a running process, so
an unresolved entry would quietly never match anything.

**Is this process that application?** `processes.py` compares the executable
behind `/proc/<pid>/exe` and, for sandboxed applications, the cgroup:
`snap.<name>.` for a Snap, `app-flatpak-<id>-` for a Flatpak. A sandboxed
application is matched by its cgroup alone, because every Flatpak runs through
the same wrapper binary and the executable would match all of them. The process
name is never used, as SPEC 9 requires: copying a binary under another name
changes the name and not the file.

**Has something just started?** `watcher.py` subscribes to the kernel's process
connector over netlink and reports each exec. Spike 3 measured that event
arriving 0.8 ms after the launch, against up to two seconds for polling, and for
a blocked application that is the difference between a window that never appears
and one the user gets to look at. Subscribing needs `CAP_NET_ADMIN` and a kernel
built with `CONFIG_PROC_EVENTS`, so when it is refused the watcher says so once
and polls `/proc` every two seconds instead. Callers cannot tell the difference
beyond the delay. The baseline for polling is taken before `start()` returns,
so an application opened immediately afterwards counts as a launch rather than
as something that was already running. Only the standard library is used, so
the netlink messages are packed and unpacked by hand.

A handler that raises is logged and the watch continues: missing every later
launch would be a far worse failure than missing this one.

## Anti-evasion

Everything here is friction rather than a lock, as P2 requires, and each piece
says plainly what it does not stop.

**A session refuses to be stopped by hand.** While one runs, the engine writes
`RefuseManualStop=yes` into a runtime drop-in for both root units. Removing it
takes root and one command; that is the point, not a flaw.

**Only stricter changes are accepted.** Profile edits go through the ratchet
before anything is written, so adding is free and removing is refused with
`RATCHET_VIOLATION`. A refused edit changes nothing, including on disk. The
ratchet protects the running session rather than the whole configuration file:
a profile nothing is using can be edited freely, because a session cannot be
moved onto it.

**Manipulation is noticed and recorded.** The three kinds SPEC 7.6 names each
become a rupture: a signature mismatch on the state file, a clock that
disagrees with the boot clock, and Anchor's nftables table going missing. In
the last case the blocker puts the rules back itself, because waiting for an
instruction would leave the machine unblocked meanwhile, and reports the
attempt so the engine can record it.

**Strict sessions block tunnels**, if the profile asks (ADR 4). The usual VPN
and Tor ports are rejected for TCP and dropped for UDP, from a shipped list.
This is honestly lopsided: a VPN on its default port stops, and one carried
over TCP 443 is indistinguishable from HTTPS and does not. Blocking 443 would
mean blocking the web.

## Not built yet

Enforcing application blocks, breaks, schedules, statistics, the interface
and the packages. The milestones in the build plan cover them, and
`docs/spikes/` records what was learned before building each one.
