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

### `anchor profile list|show|create|edit|delete`

The named sets of rules a session uses.

```
$ anchor profile list
Study
Work

$ anchor profile show Study
Study · blocklist
  Domains: reddit.com, youtube.com
  Apps: discord
  Breaks: 50/10 · overlay · moderate
```

Editing says what to add and what to take away, rather than replacing a list:

```
anchor profile edit Study --add-domain x.com --add-app steam
anchor profile edit Study --remove-domain youtube.com
anchor profile edit Study --mode allowlist
anchor profile edit Study --no-block-vpn
```

**While a session is running, only stricter changes are accepted** (SPEC 7.4).
Adding domains, apps and categories is fine. Removing anything, switching an
allowlist back to a blocklist, and turning off VPN blocking are refused with
`RATCHET_VIOLATION` and exit code 4, and nothing is written:

```
$ anchor profile edit Study --remove-domain youtube.com
anchor: cannot unblock youtube.com during a session
$ echo $?
4
```

A profile a session is enforcing cannot be deleted either. A profile nothing is
using can be edited and deleted freely, because there is no way to move a
running session onto it.

### `anchor category list|show`

The shipped bundles of domains and applications a profile can tick (SPEC 12).
A category blocks both at once: "Social media" covers `discord.com` *and*
Discord itself, which is SPEC 9's own example.

```
$ anchor category list
games        Games (14 domains, 7 apps)
news         News (17 domains, 0 apps)
shopping     Shopping (16 domains, 0 apps)
social       Social media (17 domains, 6 apps)
video        Video (16 domains, 3 apps)

$ anchor category show social
Social media (social)
  Domains: bsky.app, discord.com, discordapp.com, ...
  Apps: com.discordapp.Discord.desktop, discord.desktop, ...
```

The first column is the identifier, which is what a profile stores and what
`show` takes. A name nothing answers to exits 1.

The five shipped categories live in `/usr/share/anchor/categories` and are
replaced whenever the package is updated. To change one, copy it to
`/etc/anchor/categories/` and edit that: a file of the same name there replaces
the shipped one entirely, and a file with a new name adds a category of your
own. `anchor category edit` is not built yet; the files are plain TOML and are
meant to be edited.

In an allowlist profile a category still blocks its applications, but its
domains are ignored: adding them to the list of what is allowed would turn
"block social media" into "social media is the only thing you may read".

### Planned

`anchor skip`, `anchor category edit`, `anchor schedule`, `anchor stats`,
`anchor config` and `anchor doctor` are specified in SPEC 15 and arrive with
Milestones 6, 7 and 9.

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
