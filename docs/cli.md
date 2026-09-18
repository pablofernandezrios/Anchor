# Command line

Anchor's command line has full parity with the graphical interface (SPEC P1):
anything one can do, the other can too. Output is readable by default and
machine-readable with `--json`.

> Commands marked **planned** are defined in `SPEC.md` and arrive with their
> milestone. Asking for one today gets a clear `NOT_IMPLEMENTED` rather than a
> confusing error.

## Global options

| Option | Meaning |
|---|---|
| `--json` | Print the engine's reply verbatim, including the envelope. |
| `--root PATH` | Talk to an engine running on a relocated tree. Development only. |
| `--version` | Print the version. |

## Commands

### `anchor status`

What is running, how long is left, and what it would take to leave.

```
$ anchor status
Study · Firm
2:14:37 remaining · ends at 13:30
Blocked attempts: 7
```

With no session:

```
$ anchor status
No session is running.
Schedule skips left this week: 3 of 3
```

### `anchor start`

```
anchor start --profile NAME --duration 2h30m --level soft|firm|strict [--valve wait|phrase|both]
```

| Option | Meaning |
|---|---|
| `--profile` | Which profile's rules to apply. |
| `--duration` | `2h30m`, `90m`, `45s`, or a bare number meaning minutes. At most 8 h at start (SPEC 7.1). |
| `--level` | How hard the session is to leave. |
| `--valve` | The emergency exit. Required for Strict, refused for anything else. |

The 8-hour cap applies to what you ask for at the start. A session can be
extended past it afterwards.

### `anchor extend --by 30m`

Makes the active session longer. Any amount, any number of times. There is no
matching way to shorten one: that is the ratchet (SPEC 7.4).

### `anchor cancel`

Leaves a Soft or Firm session, at that level's price. The first call starts the
wait; the session ends by itself when the wait runs out.

```
$ anchor cancel
Study · Firm
2:14:37 remaining · ends at 13:30

Leaving in 15 min, unless you withdraw the request.
```

Firm sessions also ask for a phrase once the wait is over:

```
$ anchor cancel --phrase 'tone heal coat hold rule this part rise ...'
Session ended.
```

| Option | Meaning |
|---|---|
| `--phrase TEXT` | The phrase Anchor generated. Firm only. |
| `--withdraw` | Change your mind and keep the session. |

Strict sessions refuse this command. Use the valve.

### `anchor valve request|withdraw|phrase`

The emergency exit from a Strict session, in whichever form was chosen when the
session started. **Every use is recorded as a rupture** (SPEC 7.5).

```
$ anchor valve request        # start the 30-minute wait
$ anchor valve withdraw       # change your mind
$ anchor valve phrase         # type the phrase; prompts if not given
```

The phrase is generated fresh every time. The interface disables pasting; a
terminal cannot, so the command simply reads what you type.

### Planned

`anchor skip`, `anchor profile`, `anchor category`, `anchor schedule`,
`anchor stats`, `anchor config` and `anchor doctor` are specified in SPEC 15
and arrive with Milestones 6, 7 and 9.

## Exit codes

Stable. Scripts may rely on them.

| Code | Meaning |
|---|---|
| 0 | Success. |
| 1 | The engine refused for a reason without its own code. |
| 2 | The command line was wrong. |
| 3 | No session is running. |
| 4 | Refused on purpose: the ratchet, a Strict cancellation, the skip limit, the 8-hour cap. |
| 5 | Not allowed to give the engine orders. |
| 6 | The engine could not be reached. |
| 7 | The price has not been paid yet: the wait is not over, or the phrase was wrong. |

## Error codes

`--json` replies carry a stable `error.code`:

`BAD_REQUEST`, `UNSUPPORTED_VERSION`, `UNKNOWN_TYPE`, `UNAUTHORIZED`,
`RATCHET_VIOLATION`, `NO_ACTIVE_SESSION`, `SESSION_ALREADY_ACTIVE`,
`DURATION_TOO_LONG`, `INVALID_DURATION`, `CANCEL_FORBIDDEN`,
`WAIT_NOT_ELAPSED`, `PHRASE_MISMATCH`, `VALVE_NOT_REQUESTED`,
`SKIP_LIMIT_REACHED`, `SKIP_FORBIDDEN`, `UNKNOWN_PROFILE`, `UNKNOWN_SCHEDULE`,
`INVALID_CONFIG`, `INTEGRITY_FAILURE`, `NOT_IMPLEMENTED`, `INTERNAL`.

## Who may run these

Root and the user recorded when Anchor was installed. The engine reads the
caller's identity from the kernel, so it cannot be claimed by asking. Anyone
else gets `UNAUTHORIZED` and exit code 5.
