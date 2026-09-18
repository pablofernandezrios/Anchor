# Milestone 0: findings

The risky assumptions in `SPEC.md`, and what testing them showed. The scripts
are in [`spikes/`](../../spikes/); this page is the record of what they found
and what it means for the code.

Status at the time of writing: **4 of 7 answered**.

| # | Question | Where it runs | Status |
|---|---|---|---|
| 1 | DNS in front of systemd-resolved, surviving network changes | CI virtual machine | Pending a run with systemd-resolved |
| 2 | nftables DNS redirect exempting the resolver | CI virtual machine | **Answered: works** |
| 3 | Netlink proc connector for process events | CI virtual machine | **Answered: works** |
| 4 | StatusNotifier/AppIndicator on current GNOME | Desktop VM | Pending |
| 5 | Fullscreen break overlay on Wayland, multiple monitors | Desktop VM | **Partly answered: limits found** |
| 6 | Browser DoH policy paths | Desktop VM | Pending |
| 7 | Runtime `RefuseManualStop` drop-in | CI virtual machine | Pending a run under systemd |

---

## Spike 2: redirecting DNS without trapping the resolver — works

**The problem.** Anchor redirects all outbound DNS to its own resolver. That
resolver then has to forward upstream, and if its forwarded query is redirected
too, it loops back into itself and nothing resolves. SPEC 8.2 calls for an
exemption "that exempts the resolver process" without saying how.

**What was tested.** A real `table inet` nat output hook redirecting UDP and TCP
port 53, with two candidate exemptions, and real packets sent through it.

**Result.** The firewall mark works and is the right choice:

```
meta mark 0x616e return
meta l4proto { udp, tcp } th dport 53 redirect to :5391
```

The resolver sets the mark on its own sockets with `SO_MARK`. An unmarked query
was redirected; a marked one went straight out.

**Why the mark rather than the user.** `meta skuid` would exempt every process
running as that user. Both root daemons run as root, so would every other root
process on the machine, and the exemption would be trivially borrowed. Setting
`SO_MARK` requires `CAP_NET_ADMIN`, so an ordinary process cannot claim it. That
is a real difference in a tool whose whole purpose is friction.

**Consequence for the code.** `anchor-blockerd` sets `SO_MARK` to `0x616e` on
every upstream socket, and the nftables table carries the `meta mark` return
rule as its first rule. Milestone 2 implements it.

---

## Spike 3: netlink proc connector — works

**The problem.** SPEC 9 wants a blocked application killed the moment it
launches, with a 2-second poll only as a fallback. That needs kernel process
events.

**What was tested.** Subscribing to `CN_IDX_PROC` over `NETLINK_CONNECTOR`,
launching a process, and timing the exec event.

**Result.** The exec event arrived **2.2 ms** after launch. Subscribing and
unsubscribing both worked cleanly.

**The catch.** Binding to the process event group needs `CAP_NET_ADMIN` and a
kernel built with `CONFIG_PROC_EVENTS`. `anchor-blockerd` already holds that
capability for nftables, so nothing extra is needed, but the polling fallback
still has to exist for kernels without the option.

**Consequence for the code.** Application detection is event-driven, with the
2-second poll as a genuine fallback rather than the main path. The 2-minute
grace period of SPEC 7.1 is unaffected.

---

## Spike 5: the Wayland overlay — the specification needs a qualification

Not fully run yet, but the limits are established by how Wayland works, and
they change what SPEC 10 can promise.

**A fullscreen window per monitor is available.** GTK4's
`fullscreen_on_monitor()` covers each monitor, and `Gdk.Display.get_monitors()`
enumerates them, so "fullscreen overlay with countdown on all monitors" is
achievable.

**Two things are not.** Wayland gives no way for an ordinary client to force
itself above everything, and GNOME does not implement the layer-shell protocol
that wlroots compositors offer. A client also cannot grab the keyboard.

**What that means for a Mandatory break.** A Mandatory break cannot mean the
screen is seized: the user can always switch away. It has to mean the overlay
comes back, promptly and repeatedly, for as long as the break lasts. That is a
real difference from what "mandatory" might suggest, and it belongs in the
README's limitations section rather than being quietly glossed.

This needs the owner's agreement before Milestone 5 builds it.

---

## How to run the rest

The four machine-level spikes run unattended:

```sh
gh workflow run "M0 spikes"      # or push a change under spikes/
```

The three desktop spikes need a GNOME session in a disposable VM:

```sh
python3 spikes/run_desktop.py                 # look, do not touch
sudo python3 spikes/run_desktop.py --write    # also write browser policies
```

Take a snapshot first.
