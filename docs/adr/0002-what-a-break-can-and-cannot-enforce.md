# 2. What a break can and cannot enforce on Wayland

- **Status:** Accepted
- **Date:** 2026-09-20
- **Decided by:** the owner, in the engineering thread
- **Affects:** SPEC 10

## Context

SPEC 10 offers three break hardness levels, the strictest named Mandatory:
no skipping and no postponing. The natural reading is that the break takes the
screen and the user has to sit it out.

Wayland does not allow that, and Milestone 0 spike 5 established why:

- an ordinary client cannot place a window above everything else;
- GNOME does not implement the layer-shell protocol that wlroots compositors
  offer for exactly this;
- a client cannot grab the keyboard, so the overlay cannot swallow the
  shortcut that switches away from it.

None of this is a defect to engineer around. It is deliberate on Wayland's
part, and SPEC P7 makes GNOME on Wayland the reference desktop.

So a fullscreen overlay on every monitor is achievable, and the user being
unable to leave it is not.

## Decision

The three hardness levels stay, and Mandatory keeps its name and its rules:
a Mandatory break cannot be skipped and cannot be postponed.

What changes is what the strictness acts on. Anchor does not try to trap the
user in the overlay. Instead the break is made impossible to miss:

1. **A warning before it starts.** A notification at a configurable lead time,
   one minute by default, so the break never interrupts mid-thought without
   warning.
2. **The time is always visible.** The top-bar indicator and the Home screen
   both show when the next break falls, which the approved mockups already
   show.
3. **An alert when it begins**, alongside the overlay.
4. **The overlay reasserts itself.** If it loses focus during a break, Anchor
   asks the compositor to present it again. This is best effort: GNOME may
   mark the window as demanding attention rather than raising it, which is the
   compositor's decision and not Anchor's to override.

A break therefore ends when its time is up, not when the user dismisses it.
Anchor keeps counting and keeps telling the truth about it.

## Why not force it another way

Rejected, and why:

- **An input grab through a portal or an extension.** Would need a GNOME Shell
  extension whose whole purpose is to take input away from the user. That is a
  large thing to ask someone to install, it breaks on every Shell release, and
  a tool that can seize a keyboard is a worse thing to have on a machine than
  a missed break.
- **Inhibiting the session or blanking the screen.** Punishes the user rather
  than reminding them, and would interfere with anything else running.
- **Quietly dropping the Mandatory level.** The owner wants the distinction,
  and it remains meaningful: skipping and postponing really are refused.

## Consequences

- The README limitations section says plainly that breaks are enforced by
  insistence rather than by force, in the same spirit as "friction, not
  guarantees" (P2).
- Milestone 5 implements the pre-break warning, the alert, and the re-present
  behaviour, with the lead time configurable per profile.
- Statistics still record breaks taken, postponed and skipped (SPEC 13). A
  break the user walked away from counts as taken, which matches the rule in
  SPEC 10 for an absence covering a break.
