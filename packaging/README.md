# Packaging

Skeletons for the three package formats SPEC 17 requires. They are structure,
not finished packages: building and publishing land in Milestone 9.

| Directory | Format | Target |
|---|---|---|
| `deb/` | `.deb` | Ubuntu 24.04 and later, Debian 13 |
| `rpm/` | `.rpm` | Fedora Workstation |
| `arch/` | `PKGBUILD` | Arch Linux |
| `systemd/` | unit files | all of them |

Snap and Flatpak are deliberately absent: a sandboxed package cannot manage
host networking as root (SPEC 2.2).

## What install and removal must do

**Install** creates the directories, records the owner's UID in
`/etc/anchor/anchor.toml`, and enables the services.

**Removal restores the system, whether or not a session is active** (SPEC 7.6).
Uninstalling is always allowed, and it must put back:

- the DNS configuration, including any systemd-resolved drop-in and per-link
  settings Anchor applied;
- the `inet anchor` nftables table, which is simply deleted;
- the managed browser policies Anchor wrote, without disturbing policies that
  were already there;
- the runtime `RefuseManualStop` drop-ins under `/run/systemd/system/`.

A machine that has had Anchor removed must have working networking and no
leftover rules. Milestone 2 implements the restoration and the test that
proves it; until then the removal scripts here only take down what Milestone 1
actually installs.
