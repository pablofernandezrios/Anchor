# 1. Run the Milestone 0 spikes and the Milestone 1 core concurrently

- **Status:** Accepted
- **Date:** 2026-09-18
- **Decided by:** the owner, in the engineering thread

## Context

The build plan runs milestones strictly in order: Milestone 0 validates risky
assumptions with throwaway prototypes, reports to the owner, and waits for a
go-ahead before Milestone 1 begins.

Four of the seven spikes need a booted system with systemd, nftables and kernel
process events. Three need a real GNOME session on Wayland. The development
environment for this work is a container with neither: systemd is not PID 1,
there is no display, and nested virtualisation is unavailable.

Serialising the milestones would therefore mean doing nothing at all until the
owner had run all seven spikes by hand.

## Decision

Run them concurrently, in three tiers:

1. **Pure logic in the development environment.** The engine core, the protocol,
   the persistence layer and their tests need no privileges.
2. **A GitHub Actions Ubuntu runner** for the four machine-level spikes. Those
   runners are full virtual machines with root and systemd, so the spikes run
   unattended and become part of continuous integration rather than a one-off
   result.
3. **The owner's disposable desktop VM** for the three that need GNOME.

Milestone 0 findings are still reported to the owner before anything touching
real blocking is built.

## Why this is safe

No Milestone 1 module depends on a spike outcome. The ratchet, the session
model, the signed state files and the time reconciliation are decided by the
specification, not by whether systemd-resolved can be fronted or whether GNOME
shows an indicator. The spikes gate Milestone 2 and beyond, which is where the
code meets DNS, nftables and the desktop, and that gate stays shut.

The safety rules are untouched: nothing is applied to the owner's host machine,
and the emergency valve, the uninstall restoration and the fail-open behaviour
are built and tested before Strict mode is enabled in any test.

## Consequences

- The owner's manual spike work drops from seven to three, and none of those
  three touch DNS, nftables or systemd.
- Spike results are reproducible and re-checked on a schedule, instead of being
  a screenshot from one afternoon.
- The build plan's reporting rhythm is kept: a Milestone 0 report still comes
  before Milestone 2.
- Two spikes were answered immediately, because the development container does
  have `CAP_NET_ADMIN`: the nftables redirect and the netlink proc connector.
