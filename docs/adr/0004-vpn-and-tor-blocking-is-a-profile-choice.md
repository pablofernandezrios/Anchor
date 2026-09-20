# 4. VPN and Tor blocking is a profile choice, on by default

- **Status:** Accepted
- **Date:** 2026-09-20
- **Decided by:** the owner, in the engineering thread
- **Deviates from:** SPEC 8.2

## Context

SPEC 8.2 says a Strict session blocks VPN and Tor: the default ports of
WireGuard, OpenVPN and IPsec, plus Tor relays from a shipped list. The reason
is good. A tunnel carries DNS somewhere Anchor cannot see, so it is a way
around the block.

The cost lands somewhere the specification does not discuss. A Strict session
cannot be cancelled, so a user whose work VPN stops at the same moment their
distractions do has no way back: the only exit is the emergency valve, which
costs a thirty-minute wait or a long typed phrase and is recorded as a rupture.
Losing access to work for that long, in the middle of the focus session that
was meant to protect the work, is a bad trade.

There is also an asymmetry in what port blocking achieves. A corporate VPN on
standard ports breaks reliably. A tunnel over TCP 443 is indistinguishable from
ordinary HTTPS and is not blocked at all. So the rule as written is most likely
to stop a legitimate VPN and least likely to stop a determined one, which is
why SPEC 8.2 already calls it best effort.

## Decision

Blocking VPN and Tor becomes a per-profile setting, `block_vpn_and_tor`,
defaulting to **on**. Strict sessions still block tunnels unless the profile
says otherwise.

Turning it off is a change that loosens a profile, so the ratchet refuses it
during a session (SPEC 7.4). It can only be chosen beforehand, which makes it a
decision taken with a clear head rather than an escape available in the moment.
That is the same reasoning as the level and the valve, both of which are fixed
when a session starts.

The setting has no effect below Strict, where SPEC 7.2 allows VPN and Tor
anyway.

## Consequences

- The default behaviour is what SPEC 8.2 describes, so a user who does nothing
  gets the specified product.
- Someone who needs a VPN for work can keep it, at the price of deciding in
  advance and of a profile that is visibly less strict.
- It is not an escape hatch. Flipping it mid-session is refused with
  `RATCHET_VIOLATION`, exactly like removing a blocked domain.
- The README's limitations section says that tunnel blocking is by port and
  does not stop a tunnel carried over 443.
- The setting is in the profile rather than in global settings, because it
  belongs with the other things a profile decides about what a session blocks.
