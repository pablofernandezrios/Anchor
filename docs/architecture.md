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
| Agent and indicator | `anchor-agent` | the owner, user service | Indicator and notifications done (Milestone 5); the break overlay is Milestone 6. Speaks StatusNotifierItem over D-Bus, no AppIndicator library ([ADR 3](adr/0003-speak-statusnotifieritem-over-dbus.md)) |
| Interface | `anchor-gui` | the owner | Milestone 8 |
| Command line | `anchor` | any allowed user | `status`, `start`, `extend`, `cancel`, `valve` |

## Modules

```
src/anchor/
  protocol/    types, error codes, schema validation, message envelopes
  engine/      paths, store, timekeeping, sessions, breaks, schedules,
               ratchet, profiles, categories, stats, phrases, state, core,
               service, main
  blocker/     journal, restore, constants, commands, dnswire, matcher,
               recent, attempts, resolver, rules, resolved, policies,
               apps, processes, watcher, enforcement, daemon, main
  agent/       feed, indicator, notifications, breakscreen, desktop,
               overlay, main
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

**Following the network never costs the upstreams.** The daemon re-reads
`resolvectl status` every few seconds so a laptop that changes network keeps
working (SPEC 3). During a session that reading is Anchor's own work: every
link points at the resolver, Anchor filters its own address out, and a healthy
machine reads as having no DNS servers at all. Anchor keeps the servers it was
already forwarding to rather than believing that, because the alternative — an
empty upstream list a few seconds into every session — blocks every name on
the machine while the blocked ones stay correctly blocked, which reads to the
user as Anchor taking the whole internet away. A network that has genuinely
lost every server is indistinguishable from here, and keeping the old servers
costs nothing in that case (P4). Links appearing and disappearing, and servers
changing while they are still visible, are still followed.

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

### Categories

A category is the useful unit for a person: one tick that stands for a list of
domains and the applications that go with them. SPEC 9's example is Discord,
where blocking the program without `discord.com` blocks very little, because
the site is the same thing in a browser.

The five SPEC 12 names ship as TOML files in `/usr/share/anchor/categories`,
which the package replaces on every update. A file of the same name in
`/etc/anchor/categories` replaces the shipped one entirely, and a new name adds
a category of the user's own. That is how the specification's two requirements
— lists that stay current *and* lists the user can edit — both hold: they live
apart. Replacing rather than merging is deliberate, since a merge would leave
no way to take a domain out of a shipped list.

A category file that cannot be read is skipped with a warning, and a profile
naming a category that is not installed keeps its other rules. Refusing to
start a session over a stray character in a list would be a worse answer than
blocking less for one session.

The engine resolves categories, not the blocker: it is the engine that decides
what a session blocks, and the blocker receives domains and applications
already merged. In an allowlist profile the category's domains are left out,
because adding things-to-block to the list of what is allowed would invert
their meaning; its applications still apply, since SPEC 9 has no allowlist for
those.

### The two minutes, and after them

`enforcement.py` does the closing, and the two moments are deliberately not the
same. At the start of a session the applications already open may hold work
nobody has saved, so SPEC 7.1 gives them two minutes and a notification, then
`SIGTERM`, then `SIGKILL` ten seconds later. Anchor is friction, not a trap:
taking somebody's unsaved document is not friction, it is damage. Once the
grace has passed, SPEC 9 kills a blocked application immediately on launch,
because a program that has just started has nothing to save.

An application opened *during* the grace is left alone until the grace ends.
The two minutes are a promise about the machine, not about a list of process
identifiers, and a browser that restarts a helper mid-save would otherwise lose
the very work the grace exists to protect.

The deadline belongs to the engine, which sends the seconds remaining with
every poll. Letting the blocker time it itself would make restarting the
blocker worth a fresh two minutes.

Every process of an application is signalled before any of them is waited on,
so closing five windows takes ten seconds rather than fifty, and the wait
happens on its own thread: the poll loop and the watcher both have to keep
listening while it runs. Liveness is read from `/proc/<pid>/stat` rather than
asked of the kernel with signal 0, because a zombie answers signal 0 and would
hold the escalation open for a process that has already died.

What was closed is reported to the engine, which counts it (SPEC 13 asks for
blocked attempts by application as well as by domain) and publishes an event
carrying whether the application was already running or had just been launched,
so the agent can word the notification properly.

## The agent

The agent is the only part of Anchor the user sees all day, and the only one
that is allowed to be absent. It blocks nothing, decides nothing and owns
nothing: it follows the engine and draws what it is told. A machine with no
desktop still blocks (P4); it just says nothing about it.

**Staying attached.** The engine is a system service and the agent a user one,
so they restart independently. The feed reconnects on its own and asks for the
status each time rather than waiting for an event, because with no session
running the engine sends nothing at all and an agent that learned only from
events would show nothing for as long as nothing changed. An outage is logged
once rather than once per attempt, and a connection that worked resets the
backoff: otherwise the fifth package upgrade of a day would leave the
indicator blank for half a minute.

**What the top bar says.** Minutes, rounded up, as the approved mockup shows
them: `2:14`, not `2:14:37`. Seconds in a panel are noise, and rounding down
would leave the last minute reading `0:00` for a full sixty seconds. The item
is hidden when no session runs, because an icon that sits there all day
teaches people to ignore it — but it stays visible, saying the time is
unknown, when the engine cannot be reached during a session. Disappearing
would say "your session ended", and counting down from memory would invent the
one number the indicator exists to be trusted for.

**What it says out loud.** Three notifications, all of them specified: a
blocked site (SPEC 8.3), the two-minute warning naming what will close
(SPEC 7.1), and an application that was closed (SPEC 9). Nothing else. A focus
tool that chats is a focus tool people turn off. The ten-minute limit is not
reimplemented here: it lives in the blocker, which stops counting a domain it
has already reported, and two ideas of what the user has seen would be one too
many. The grace warning is the only critical one, and it expires exactly when
the applications close.

**The bus.** `desktop.py` is the only module that imports PyGObject, so
everything above it is tested on machines with no desktop at all. It
implements `org.kde.StatusNotifierItem` itself rather than using
`libayatana-appindicator`, which is GTK3 and cannot share a process with the
GTK4 interface ([ADR 3](adr/0003-speak-statusnotifieritem-over-dbus.md)). One
item is exported for the life of the agent, its `Status` switching between
`Active` and `Passive`: some panels forget an item that unregisters, and a bar
that stayed empty until the next login would be worse than one extra property
change. The watcher is watched rather than called once, because the extension
can be disabled, enabled, or arrive after the agent.

Drawing is handed to the desktop's own thread. The feed runs on its own, and
D-Bus is not the place to find out what happens when two threads meet.

Which signals a change needs is worked out by a plain function, away from the
bus, because that is the part that has already been wrong. The panel turns a
signal name into a property name by removing its prefix, so the countdown is
announced as `XAyatanaNewLabel` and not `NewLabel`: the second asks the panel
to re-read a property called `Label`, which this interface does not have, and
the label sits still for the whole session. The first run on a real desktop is
what found it. Spike 4 could not: its label never moved.

## Breaks

A session alternates between working and resting, and the pattern belongs to
the profile (SPEC 10, 12). The engine decides, as it decides everything about
a session; the agent draws.

**The phase has a clock of its own**, a `TimeAnchor` exactly like the
session's. That is not tidiness. A break then inherits the reconciliation
SPEC 6.2 already demands of sessions, so suspending the machine, rebooting it
and moving the system clock behave the same way for a break as they do for the
session containing it — and are tested the same way.

**The timer does not care whether you are there.** SPEC 10 says the work timer
keeps running during idle and suspend, and Anchor makes no attempt to detect a
user at the keyboard: a timer that stopped when you stood up is a timer that
never fires. What it does care about is an absence long enough to swallow a
break. Then the break counts as taken and a fresh cycle starts, so eight hours
asleep is one missed break rather than nine. That case publishes nothing: an
overlay counting down a break that ended an hour ago is a lie with a clock on
it.

**Skipping and postponing** follow SPEC 10's table — Flexible both and freely,
Moderate one five-minute postponement and no skipping, Mandatory neither. The
limit counts postponements of the break now owed, not of the session, so
taking a break forgives the last one. Postponing is not skipping: the break
comes back when the borrowed time runs out.

**A break does not unblock anything** unless the profile says so, and even
then only sites. Applications stay blocked through every break, because five
minutes is long enough to lose an hour in one.

### The overlay

[ADR 2](adr/0002-what-a-break-can-and-cannot-enforce.md) settled what a break
can enforce on Wayland: a fullscreen window on every monitor is achievable and
keeping the user inside it is not. So the overlay does not try. It covers
every monitor, says what is happening, and while another window has focus it
keeps asking the compositor to bring it back — every few seconds for as long
as the break lasts, never faster, because a compositor that refuses would
otherwise be asked again on every focus change it causes, which is a loop with
a user inside it.

*Keeps* asking is the part that matters, and it is the part that was wrong
first time round: asking once when focus is lost is a single request a
compositor is free to ignore, after which Anchor would go quiet for the rest
of the break. What GNOME does with those requests is GNOME's decision — often
to mark the window as wanting attention rather than to raise it — and that is
where ADR 2's promise ends. Anchor asks; it does not seize.

A monitor plugged in mid-break gets a window too, rather than becoming the one
screen with the distraction on it.

A Mandatory break says in words that it cannot be skipped or postponed. A
screen with no way out and no explanation looks like a program that has hung,
and the difference between friction and a crash is whether the user can tell
which one they are looking at.

What the overlay says is composed in `breakscreen.py` from the approved
mockup, and tested where there is no display. `overlay.py` is GTK and nothing
else.

## Statistics

SQLite in `/var/lib/anchor/stats.db`, written only by the engine (SPEC 13).
No telemetry, and nothing leaves the machine.

**This is the one place a visited domain is written down.** The working rules
forbid them everywhere else, and they never reach the journal. SPEC 13 limits
even this to how often a name was asked for and how often it was refused:
there is no record of how long anything was looked at, because that would be a
record of a person's day rather than of Anchor's work.

**Focus is stored in day buckets**, split at local midnight when a session
ends. A session from 23:00 to 01:00 is two hours across two days, and saying
so costs one small function; attributing it to the day it began would
overstate one day and empty the next. The bar chart the interface draws is
then a straight sum, and every day in a range appears even when it is empty —
a chart with days missing lies about the shape of a week.

**Breaks are recorded from the counters, not from the events.** An absence
that covers a break counts it as taken and publishes nothing, so a statistic
that counted announcements would lose exactly the breaks nobody was there for.

**Nothing here may end a session.** A statistic is a nice-to-have and a
session is not, so every write is allowed to fail: it is logged and the engine
carries on (P4). That includes a database that will not even open, which is
the case worth naming, because a context manager that raises before it yields
takes its caller down with it.

Retention is swept when a session ends rather than on a timer. A timer that
exists to delete things is a thing that can fail quietly for months, and a
session ending is both frequent enough and already a moment when the engine
is writing.

`anchor stats --delete` is SPEC 13's one action. There is nothing to restore
from afterwards, by design: a private record that quietly survives its own
deletion is not private.

## Schedules

A schedule is days of the week plus a window, and the profile, level and valve
to run in it (SPEC 11). The engine evaluates them on its own tick rather than
through systemd timers: a timer firing into a machine whose engine is still
starting has nowhere to put the session, and the engine already reconciles
time across boots and suspends.

**A late boot joins the window in progress.** The session starts now and ends
when the window does. Nothing is owed for the part that was missed — a
schedule is a promise about a time of day, not a quota of hours.

**A window may cross midnight.** `22:00`–`02:00` belongs to the day it starts
on, and is still running at one in the morning.

**A manual session holds the floor.** A schedule opening while one runs does
not start a second session; it starts when the first ends, if the window is
still open.

**Overlaps merge, and the strictest level wins.** That is one line in the
specification and several decisions here, all resolved towards *more*
blocking, because a merge that could quietly unblock something would make two
schedules weaker than one:

- the level is the strictest, and the valve comes with it;
- blocked domains, applications and categories are the union;
- an allowlist beats a blocklist, since it blocks everything it does not name;
  where two allowlists meet, only what **both** allow stays allowed, and
  anything a blocklist in the same window names is taken out;
- the session ends when the last of them ends;
- everything else — the break pattern above all — comes from the strictest
  schedule's profile.

A scheduled session stores the schedule identifiers rather than a copy of the
merged rules, so one place decides what a schedule blocks. That is safe
because a schedule that is running cannot be edited or deleted: freely before
it starts, as SPEC 11 says, and not during. The way out of the session it
started is the session's own.

**Skips** are three a week, reset on Monday at 00:00 local, and each one is a
rupture (SPEC 11). A Strict scheduled session cannot be skipped at all: its
valve, chosen when the schedule was written, is the only way out. The skipped
occurrence is remembered until it would have ended, because otherwise the next
tick would start it again a second later, which is not what anyone means by
skipping.

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

## Settings

Five things a person may change, and no more: the Firm wait, the length of the
random phrase, how long statistics are kept, the interface's language, and
whether the introduction has run (SPEC 7.2, 13, 14). Everything else about
behaviour belongs to a profile or a schedule, where it can differ on a
Tuesday.

They live in `config.json` beside the profiles and schedules, and two rules
shape them. **Unset is not zero**: a preference nobody has touched is stored
as nothing at all and means "whatever Anchor's default is", so a better
default in a later version reaches everyone who never opened Settings.
**Values cross as text** and are judged by the engine, so `anchor config set`
and the Settings screen cannot disagree about what is allowed.

The two exit knobs refuse to change while a session runs — in *either*
direction. SPEC 7.2 says "never during a session", not "never looser", so
raising the phrase length mid-session is refused too. That is why the refusal
is `SETTING_LOCKED` rather than `RATCHET_VIOLATION`: calling a stricter change
a loosening would be a lie in a code that scripts branch on.

`retention_days` overrides the machine default in `anchor.toml`, so a choice
made in Settings is not quietly overruled by a file only root can edit.

## anchor doctor

Five checks and, for each, what to type (SPEC 15). The interesting part is
that what counts as healthy depends on whether a session is running: Anchor
loads its firewall table, points systemd-resolved at its resolver and writes
the browser policies when a session starts, and undoes all of it when the
session ends. A loaded table is therefore right during a session and **wrong**
outside one — it means something stopped without cleaning up and the machine
is being blocked by nobody.

That turned out to be a real hole rather than a hypothetical, so the blocker
now clears leftovers itself the first time it hears that no session is
running. Once, not once a second.

The indicator check is the one the engine cannot make: the panel lives on the
user's session bus and the engine runs outside it. The engine answers "could
not be checked" and whichever client asked looks for itself. Silence is
reported as silence, never as absence.

The blocker's liveness comes from its own policy poll, which is the only time
it speaks to the engine at all — so no second channel, and nothing to ask
systemd.

## The interface

`anchor-gui` is a thin client like every other: it holds no state the engine
does not, it decides nothing, and closing it stops nothing.

It is built in two halves, the same split the agent uses and for the same
reason. `anchor/gui/home.py`, `start.py`, `profiles.py`, `schedules.py`,
`lists.py`, `stats.py`, `settings.py` and `onboarding.py` decide what every
screen *says* — the words, the numbers, which buttons exist, which are
disabled and why — and are tested without a display. `app.py`, `pages.py`,
`dialogs.py` and `widgets.py` turn those answers into GTK4 and libadwaita
widgets and do nothing else.

`EngineLink` reuses the agent's reconnecting feed for the status and sends
everything else on a worker thread, because GTK has one thread and a socket
that takes five seconds would freeze the window for five. A refusal from the
engine is an answer, not an exception: it arrives as an ordinary `Reply` with
the engine's own sentence, which becomes a toast. An unreachable engine is a
different thing and says so, because a broken machine and a rule saying no
are different screens.

Three details worth keeping:

- **Colours come from libadwaita**, never invented. `accent`, `warning` and
  `error` already meet WCAG AA in both themes and follow the system's choice
  of the two.
- **Icons are asked for with a fallback.** A theme can carry a name and still
  draw the "missing image" square; neither `has_icon` nor `get_file` tells the
  truth about it, but what a lookup *resolved to* does, because GTK renames it
  to `image-missing` when it gives up.
- **Screens are rebuilt, not patched.** Widgets are cheap and half-updated
  state is not: the indicator's own worst bug was a label that never changed.

`tools/screenshot_gui.py` runs the whole thing against a real engine on a
virtual display and photographs every screen. It is how the interface was
checked against the mockups from a machine with no desktop, and it is what
produced the pictures in the README.

## Words

English is the source and Spanish is the translation, through gettext (SPEC
14). The catalogue lives in `po/`, and `tools/po.py` extracts, compiles and
checks it without the GNU gettext tools — continuous integration installs
Python and nothing else, and a check that only runs where a package happens to
be installed is a check that stops running.

Two things that catch real mistakes. A string looked up by index out of a
tuple — weekdays, months, the names of the six pages — gives an extractor a
variable rather than a string, so those are marked with `N_()` where they are
written; without it they would silently never be translated. And a translation
that loses a placeholder does not look wrong in an editor: it crashes when the
sentence is formatted, in front of the one user who reads Spanish. A test
compares the placeholders on both sides of every entry.

Translated words are for reading, never for keys. A badge's colour is looked
up by the level's own name, because in Spanish the label is "Firme".

## Not built yet

The distribution packages. The milestones in the build plan cover them, and
`docs/spikes/` records what was learned before building each one.
