# Milestone 0 spikes

Throwaway prototypes that answer the risky questions in `SPEC.md` before the
code depends on the answers. Findings land in `docs/spikes/`.

They use the standard library only, both because the root daemons must
(SPEC 5.4) and because a spike that needs installing is a worse experiment.
Every spike restores whatever it touches.

## Where each one runs

Four need a real machine but no screen, so CI runs them unattended on a GitHub
Ubuntu runner, which is a full virtual machine rather than a container:

| Spike | Question |
|---|---|
| `s1_dns_resolved.py` | Can Anchor sit in front of systemd-resolved and follow network changes? |
| `s2_nftables_redirect.py` | Can DNS be redirected while exempting Anchor's own resolver? |
| `s3_proc_connector.py` | Does the netlink proc connector report launches promptly? |
| `s7_refuse_manual_stop.py` | Is a runtime `RefuseManualStop` drop-in honoured, and reversible? |

Run them with `.github/workflows/spikes.yml`, or by hand:

```sh
cd spikes && sudo python3 s2_nftables_redirect.py ./results
```

Three need a GNOME session and are run by hand in a disposable virtual machine:

| Spike | Question |
|---|---|
| `s4_indicator.py` | Is there a top-bar indicator on this GNOME? |
| `s5_wayland_overlay.py` | Can a break overlay cover every monitor on Wayland? |
| `s6_browser_doh.py` | Which policy path does each browser really read? |

```sh
python3 spikes/run_desktop.py                 # look, do not touch
sudo python3 spikes/run_desktop.py --write    # also write browser policies
```

Take a VM snapshot first. The spikes restore what they change, but a snapshot
is cheaper than trusting that.

## Reading the results

Each spike writes `<name>.json`. `summarise.py` turns a directory of them into
the tables in `docs/spikes/`:

```sh
python3 spikes/summarise.py results
python3 spikes/summarise.py results --check   # exit 1 if anything failed
```

A `SKIP` means the machine could not answer the question, which is information
about where to ask it, not a failure. A `FAIL` means the specification assumed
something untrue, and that is when to stop and talk to the owner.
