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
| Blocker | `anchor-blockerd` | root, system service | Stub; Milestones 2 and 4 |
| Agent and indicator | `anchor-agent` | the owner, user service | Milestone 5 |
| Interface | `anchor-gui` | the owner | Milestone 8 |
| Command line | `anchor` | any allowed user | `status`, `start`, `extend`, `cancel`, `valve` |

## Modules

```
src/anchor/
  protocol/    types, error codes, schema validation, message envelopes
  engine/      paths, store, timekeeping, sessions, ratchet, profiles,
               phrases, state, core, service, main
  blocker/     stub
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

## Not built yet

Web blocking, application blocking, breaks, schedules, statistics, the
interface and the packages. The milestones in the build plan cover them, and
`docs/spikes/` records what was learned before building each one.
