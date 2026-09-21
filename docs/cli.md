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

`--json` and `--root` work before the command or after it, so both
`anchor --json stats` and `anchor stats --json` do the same thing.

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

### `anchor break skip|postpone`

The break that is running, if the profile lets you avoid it (SPEC 10). The
overlay has these as buttons; this is the same thing from a terminal, because
SPEC 15 asks for parity with the interface.

```
$ anchor break postpone
Study · Firm
2:14:37 remaining · ends at 13:30
...
```

What each hardness allows:

| Hardness | `skip` | `postpone` |
|---|---|---|
| Flexible | Yes | Yes, as often as you like |
| Moderate | No | Once, five minutes |
| Mandatory | No | No |

A refusal exits 4, like every other refusal on purpose:

```
$ anchor break skip
anchor: a mandatory break cannot be skipped
$ echo $?
4
```

The limit counts postponements of the break now owed, not of the session, so
taking one forgives the last.

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

### `anchor schedule list|show|create|edit|delete`

Sessions that start on their own (SPEC 11). A schedule is days of the week
plus a window, and the profile, level and valve to run in it.

```
$ anchor schedule list
3f2a91c4  Mornings         Mon Wed Fri    09:00–13:00  firm  ← running now
8b1d0e77  Evenings         Tue Thu        18:00–20:00  soft

Skips left this week: 3 of 3

$ anchor schedule create Evenings --profile Work \
      --day tuesday --day thursday --from 18:00 --to 20:00 --level soft
```

A window may cross midnight: `--from 22:00 --to 02:00` runs into the small
hours of the next day, and belongs to the day it starts on.

**A schedule can be edited and deleted freely until it starts.** While it is
running, both are refused with exit code 4: a schedule is changed before it
begins, not during. The way out of the session it started is the session's
own.

Where two schedules overlap, their rules are merged and the strictest level
wins. The merge always resolves towards more blocking — the union of what they
block, and an allowlist beating a blocklist — because a merge that could
unblock something would make two schedules weaker than one.

### `anchor skip`

Skip the scheduled session running now (SPEC 11). Three a week, reset on
Monday at 00:00, and every one is recorded as a rupture.

```
$ anchor skip
$ anchor skip
anchor: a Strict scheduled session cannot be skipped; only its valve applies
$ echo $?
4
```

A skip applies to that one occurrence. Tomorrow's run of the same schedule
still happens.

### `anchor stats [--day|--week|--month]`

What Anchor has been doing (SPEC 13). The week runs Monday to Sunday and the
month is the calendar month, as the interface draws them. Without a range, it
shows this week.

```
$ anchor stats --week
2026-09-21 to 2026-09-27

  Focus                21 h 12 min
  Sessions completed   14
  Blocked attempts     96
  Ruptures             1

Blocked most often
  youtube.com                              41
  reddit.com                               22
  Discord (app)                             9

Breaks: 23 taken · 5 postponed · 0 skipped
Ruptures: 0 valve · 1 skipped schedule · 0 tampering
```

Applications appear in the same ranked list as domains, marked `(app)`,
because "what did I try to reach" is one question.

#### `anchor stats --delete`

The one action that deletes every statistic (SPEC 13). It asks first, and
there is nothing to restore from: Anchor keeps no copy, because a private
record that quietly survives its own deletion is not private.

```
$ anchor stats --delete
This deletes every statistic Anchor has recorded. It cannot be undone.
Type 'delete' to go ahead:
```

Anything other than `delete` exits 2 and changes nothing. It works during a
session and is not subject to the ratchet: statistics are a record of what
Anchor did, not part of what it is enforcing.

Statistics older than the retention window are swept whenever a session ends.
The window is 90 days unless `anchor.toml` says otherwise.

### `anchor config get|set`

The handful of settings Anchor keeps for you (SPEC 7.2, 13, 14). Everything
else about behaviour belongs to a profile or a schedule, where it can differ
by the day.

```
$ anchor config get
firm_wait_seconds = 900  (default)
    How long a Firm session makes you wait before it lets go. Not during a session.
phrase_length = 150  (default)
    How many characters the random phrase has. Not during a session.
retention_days = 90  (default)
    How long statistics are kept before they are forgotten.
language =   (default)
    The interface language. Empty follows the desktop.
onboarding_done = False  (default)
    Whether the first-run introduction has been completed.

$ anchor config set firm_wait_seconds 1800
firm_wait_seconds is now 1800.
```

`(default)` means nobody has chosen: the value shown is Anchor's own, and a
later version may improve it. Choosing the same number pins it.

Values are sent as text and judged by the engine, so `anchor config` and the
Settings screen cannot disagree about what is allowed.

The Firm wait and the phrase length are settled when a session starts and
refuse to change until it ends — in either direction, because SPEC 7.2 says
"never during a session", not "never looser":

```
$ anchor config set firm_wait_seconds 60
anchor: firm_wait_seconds cannot be changed while a session is running. ...
$ echo $?
4
```

`retention_days` overrides the machine default in `anchor.toml`, so a choice
made here is not quietly overruled by a file only root can edit.

### `anchor doctor`

Checks that Anchor can do its job, and prints what to type when it cannot
(SPEC 15). Five checks: the blocker daemon, the DNS path, the firewall table,
the browser policies and the top-bar indicator.

```
$ anchor doctor
[  ok   ] The blocker daemon
          Checked in 1 s ago.
[problem] The DNS path
          No upstream DNS server could be found, in systemd-resolved or in
          /etc/resolv.conf. ...
          Try: Check this machine's network connection, then: resolvectl status
[  ok   ] The firewall table
          inet anchor is not loaded.
[  ok   ] Browser policies
          Found: Firefox, Chromium.
[ note  ] The top-bar indicator
          No StatusNotifierWatcher is running ...
          Try: Install the AppIndicator extension ...

1 thing(s) to look at.
```

What counts as healthy depends on whether a session is running. Anchor loads
its firewall table, points systemd-resolved at its resolver and writes the
browser policies when a session starts, and undoes all of it when the session
ends — so a loaded table is right during a session and **wrong** outside one:
it means something stopped without cleaning up and the machine is being
blocked by nobody. `anchor-blockerd --restore` clears it, and the blocker now
clears it by itself the first time it hears that no session is running.

A missing indicator extension is a `note`, not a `problem`: Anchor blocks
exactly as well without it, and the time left is still on Home and in
`anchor status`. Exit code 1 for a problem, 0 for notes only.

The indicator check is the one the engine cannot make: the panel lives on the
user's session bus and the engine runs outside it, so the engine answers
"could not be checked" and whichever client asked looks for itself.

### Planned

`anchor category edit` is specified in SPEC 15 and arrives with Milestone 10.

## Exit codes

Stable. Scripts may rely on them.

| Code | Meaning |
|---|---|
| 0 | Success. |
| 1 | The engine refused for a reason without its own code. |
| 2 | The command line was wrong. |
| 3 | No session is running. |
| 4 | Refused on purpose: the ratchet, a Strict cancellation, the skip limit, the 8-hour cap, a setting a session has frozen. |
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
`INVALID_CONFIG`, `SETTING_LOCKED`, `INTEGRITY_FAILURE`, `NOT_IMPLEMENTED`,
`INTERNAL`.

## Who may run these

Root and the user recorded when Anchor was installed. The engine reads the
caller's identity from the kernel, so it cannot be claimed by asking. Anyone
else gets `UNAUTHORIZED` and exit code 5.
