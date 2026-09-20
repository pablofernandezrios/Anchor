# Milestone 0: findings

The risky assumptions in `SPEC.md`, and what testing them showed. The scripts
are in [`spikes/`](../../spikes/); this page records what they found and what
it means for the code.

**All 7 answered.** The four that need a booted machine ran on an Ubuntu
24.04 runner with systemd 255; the three that need a GNOME session are waiting
on a desktop VM.

| # | Question | Where | Status |
|---|---|---|---|
| 1 | DNS in front of systemd-resolved | CI VM | **Works**, with a caveat to confirm on a NetworkManager desktop |
| 2 | nftables DNS redirect exempting the resolver | CI VM | **Works** |
| 3 | Netlink proc connector | CI VM | **Works** |
| 4 | StatusNotifier/AppIndicator on GNOME | Desktop VM | **Works** over D-Bus, no AppIndicator library ([ADR 3](../adr/0003-speak-statusnotifieritem-over-dbus.md)) |
| 5 | Fullscreen break overlay on Wayland | Desktop VM | **Works** on one display; multi-monitor untested. Limits settled by [ADR 2](../adr/0002-what-a-break-can-and-cannot-enforce.md) |
| 6 | Browser DoH policy paths | Desktop VM | Firefox confirmed; **Snap DoH and the Chromium family still open** |
| 7 | Runtime `RefuseManualStop` drop-in | CI VM | **Works** |

**Nothing contradicts the specification.** Two points needed decisions and
both are recorded: what a Mandatory break can enforce
([ADR 2](../adr/0002-what-a-break-can-and-cannot-enforce.md)), and how the
indicator reaches the top bar
([ADR 3](../adr/0003-speak-statusnotifieritem-over-dbus.md)).

Three things remain unverified rather than unanswered, and none of them blocks
Milestone 2: the multi-monitor overlay, DNS-over-HTTPS in the Firefox Snap, and
the Chromium-family policy paths on a machine that has those browsers.

---

## Spike 1: DNS in front of systemd-resolved — works

**The question.** SPEC 8.2 says Anchor runs a forwarding resolver and puts
itself in front of the system resolver through a drop-in, following network
changes automatically.

**Result.** Both routes work on systemd 255:

- **A global `DNS=127.0.0.1:<port>` with `Domains=~.`** in
  `/etc/systemd/resolved.conf.d/`. Queries reached Anchor's resolver.
- **Per-link configuration** (`resolvectl dns <link> …` plus a `~.` routing
  domain on each link). Also worked, on all three links present.

Blocking was verified the only way that proves anything: a name that resolves
perfectly well on its own came back `NXDOMAIN`, which only Anchor could have
produced, while an unblocked name still resolved through the forwarder.

### A false result, and what it taught

The first run reported that queries never reached Anchor, and the write-up in
commit `3ce9bc2` concluded that a link's DHCP servers outrank the global
setting. **That conclusion was wrong**, and the correction matters because it
changes what Milestone 2 builds.

The spike had asked for a name under `.invalid`. That is a special-use domain
reserved by RFC 6761, and systemd-resolved answers it locally without asking
any DNS server at all. So the query genuinely never reached Anchor's resolver —
not because the drop-in failed, but because there was nothing to forward. The
same bad probe also made the blocking check meaningless: a `.invalid` name
returns `NXDOMAIN` whether or not Anchor ever sees it, so that check was
passing for the wrong reason. One mistake, two misleading symptoms, in opposite
directions.

**What this means for the code.** systemd-resolved answers some things without
forwarding, so Anchor's resolver will never see them. That is harmless for
special-use domains, but the same mechanism is why the drop-in must set
`Cache=no`: a cached answer is another query Anchor never sees, and a domain
blocked mid-session would keep resolving from cache until the entry expired.

**Still to confirm.** The runner had no NetworkManager. On a desktop where NM
sets per-link DNS and routing domains, the global setting may not win, so
Milestone 2 should configure per-link and re-apply on link changes, treating
the global drop-in as a backstop rather than the mechanism. Spike 4's run on
the desktop VM is the chance to check this.

---

## Spike 2: redirecting DNS without trapping the resolver — works

**The problem.** Anchor redirects all outbound DNS to its own resolver, which
then has to forward upstream. If that forwarded query is redirected too, the
resolver loops back into itself and nothing resolves. SPEC 8.2 asks for an
exemption without saying how.

**Result.** The firewall mark works, and is the right choice:

```
meta mark 0x616e return
meta l4proto { udp, tcp } th dport 53 redirect to :<port>
```

An unmarked query was redirected; a marked one went straight out.

**Why the mark rather than the user.** `meta skuid` would exempt every process
running as that user. Both root daemons run as root, so that would exempt every
root process on the machine, and the exemption could be borrowed by anything.
Setting `SO_MARK` needs `CAP_NET_ADMIN`, so an ordinary process cannot claim
it. In a tool whose entire value is friction, that difference is the point.

**For the code.** `anchor-blockerd` sets `SO_MARK` to `0x616e` on every
upstream socket, and the `inet anchor` table carries the `meta mark` return as
its first rule.

---

## Spike 3: netlink proc connector — works

**Result.** Exec events arrived **0.8 ms** after launch on the runner, and
2.2 ms in a container. Subscribing and unsubscribing both worked cleanly.

**The catch.** Binding to the process event group needs `CAP_NET_ADMIN` and a
kernel built with `CONFIG_PROC_EVENTS`. `anchor-blockerd` already holds that
capability for nftables, so nothing extra is needed — but the 2-second polling
fallback in SPEC 9 still has to exist for kernels without the option.

**For the code.** Application detection is event-driven, with polling as a
genuine fallback rather than the main path.

---

## Spike 7: the `RefuseManualStop` drop-in — works

The whole cycle behaved as SPEC 5.3 assumes, on systemd 255:

- the unit stops normally when no drop-in is present, which is what
  uninstalling depends on (SPEC 7.6);
- with `RefuseManualStop=yes` written to
  `/run/systemd/system/<unit>.d/anchor-session.conf` and a `daemon-reload`,
  `systemctl stop` was **refused** and the service stayed active;
- removing the drop-in and reloading handed control straight back;
- `/run` is a tmpfs, so a reboot clears it and the engine rewrites it if a
  session is still running.

**For the code.** The mitigation in SPEC 16 holds as described, with the
residual risk unchanged: root can delete the drop-in.

---

## Spike 5: the Wayland overlay — SPEC 10 needs a qualification

Not yet run, but the limits follow from how Wayland works and they change what
a break can promise.

**Available.** GTK4's `fullscreen_on_monitor()` covers each monitor and
`Gdk.Display.get_monitors()` enumerates them, so "fullscreen overlay with
countdown on all monitors" is achievable.

**Not available.** Wayland gives no way for an ordinary client to force itself
above everything, and GNOME does not implement the layer-shell protocol that
wlroots compositors offer. A client also cannot grab the keyboard.

**So a Mandatory break cannot mean the screen is seized.** The user can always
switch away.

**Settled.** The owner's decision, recorded in
[ADR 2](../adr/0002-what-a-break-can-and-cannot-enforce.md): the three hardness
levels stay and Mandatory still refuses skipping and postponing, but the break
is enforced by being impossible to miss rather than by force. A warning before
it starts, the time always visible, an alert when it begins, and an overlay
that asks to be presented again if it loses focus. Milestone 5 builds that.

---

## Spike 4: the top-bar indicator — works, and drops a dependency

Run on Ubuntu GNOME on Wayland.

**The GNOME side is in place.** `org.kde.StatusNotifierWatcher` is registered
and `ubuntu-appindicators@ubuntu.com` is enabled, so SPEC 14.1's assumption
holds on Ubuntu and onboarding only has to detect their absence elsewhere.

**The library route is out.** `AyatanaAppIndicator3` is a GTK3 library and
Anchor's interface is GTK4. A process cannot load both, so that route would
have pushed the indicator out of `anchor-agent` into a process of its own,
against SPEC 5.1.

**The D-Bus route works.** Registering an `org.kde.StatusNotifierItem` directly
with Gio — icon, title, and `XAyatanaLabel` for the remaining time — was
accepted by the watcher, and the watcher listed the item back. No GTK at all is
involved in reaching the bus.

So the indicator stays inside the GTK4 agent as SPEC 5.1 describes,
`gir1.2-ayatanaappindicator3-0.1` leaves the dependency lists, and no GTK3 is
needed anywhere in Anchor. Recorded as
[ADR 3](../adr/0003-speak-statusnotifieritem-over-dbus.md). The cost is the
menu: `libayatana-appindicator` supplies `com.canonical.dbusmenu` for free and
Anchor now exports it itself, which Milestone 8 carries.

### A third self-grading row

The spike had a "check by eye" row that reported `PASS` no matter what the
person watching saw. It graded an instruction rather than an observation, which
is the same failure as the `.invalid` probe and the missing-package verdict:
a check that cannot fail teaches nothing.

Rows needing a human now report `LOOK` and ask a question by name. They are
never a pass.

---

## Spike 5: the break overlay — works on one display

On Wayland, GTK4 opened a fullscreen window on each detected monitor and held
it for five seconds. `Gdk.Display.get_monitors()` enumerated the display and
`fullscreen_on_monitor()` covered it.

**Multi-monitor is untested.** The VM has one display, so the case SPEC 10
actually asks for has not been exercised. This needs a second display attached
before Milestone 5 relies on it; it is not a risk to the design, since the code
path is per-monitor either way.

The limits are unchanged and settled by
[ADR 2](../adr/0002-what-a-break-can-and-cannot-enforce.md): no way to force a
window above everything, no keyboard grab, so a Mandatory break means the
overlay returns rather than the screen being seized.

---

## Spike 6: browser policy paths — Firefox confirmed, the Snap still open

On the desktop VM:

| Browser | Found at | Policy path |
|---|---|---|
| Firefox (deb) | `/usr/bin/firefox` | `/etc/firefox/policies/policies.json` |
| Firefox (Snap) | `/snap/firefox/current/…` | `/etc/firefox/policies/policies.json` |

Chrome, Chromium, Brave and Edge were not installed, so their paths are still
only what the documentation claims.

**The open question is the Snap.** Both Firefox builds are present and both are
supposed to read `/etc/firefox/policies`, but a confined Snap reading `/etc` is
exactly the assumption SPEC 8.2 rests on, and the spike cannot prove it from
the outside. It needs one manual check: with the policy written, open
`about:policies` in the Snap Firefox and confirm `DNSOverHTTPS` shows as locked
off.

Until that is confirmed, **Snap Firefox is assumed able to bypass Anchor
through DoH**. That is not fatal to Milestone 2: the nftables rules block the
DoH endpoints on the shipped list regardless of what any browser is configured
to do, and the managed policy is the belt to that braces. But if the policy
does not apply, the README has to say so, because Snap Firefox is the default
browser on Ubuntu.

---

## Running the rest

Four machine-level spikes, unattended:

```sh
gh workflow run "M0 spikes"      # or push a change under spikes/
```

Three desktop spikes, in a disposable VM with a fresh snapshot:

```sh
python3 spikes/run_desktop.py                 # look, do not touch
sudo python3 spikes/run_desktop.py --write    # also write browser policies
```
