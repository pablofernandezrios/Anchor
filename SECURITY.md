# Security policy

## Scope and threat model

Anchor provides **friction, not guarantees**. It is designed to make giving in
to an impulse costly and visible, not to withstand a determined attacker who
controls the machine. Anyone with root can disable it.

Anchor's mitigations, and what they leave open, are listed in section 16 of
[`SPEC.md`](SPEC.md). In short:

- Root daemons refuse to be stopped by hand while a session is active, but root
  can remove the drop-in that enforces that.
- State files are signed so edits are detected and the stricter reading wins,
  but root can read the signing key.
- Clock changes are tracked against boot time and recorded, but a reboot plus a
  clock change is harder to detect.
- Browser DNS-over-HTTPS is disabled through managed policies and known
  endpoints are blocked, but unknown endpoints may work.
- Anchor targets single-user machines. It does not defend against another local
  user.

Reports that amount to "root can bypass Anchor" are expected and documented
rather than treated as vulnerabilities.

## What is a vulnerability

We do want to hear about, for example:

- A way for a process that is **not** root and **not** the owner UID to give the
  engine orders over the IPC socket.
- Privilege escalation through the daemons, the packaging scripts or the
  systemd units.
- Anchor leaving a machine without working networking, or with rules it does not
  clean up, after removal.
- Visited domains leaking into the system log or anywhere outside the local
  statistics database.

## Reporting

Report privately through GitHub's **Report a vulnerability** button on the
Security tab of the repository, rather than opening a public issue. Please
include the version, the distribution, and the steps to reproduce.

Anchor is a personal project. Expect a first reply within a couple of weeks.
