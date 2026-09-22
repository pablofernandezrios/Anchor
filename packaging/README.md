# Packaging

The three package formats SPEC 17 requires.

The `.deb` is built and installed; the other two are still skeletons, checked
by tests but never built. Building the first one found four defects nothing
else would have: two missing build dependencies, a test step that could not
import the package it was testing, and a set of links that shipped the whole
source tree inside the binary package.

    sh tools/build_deb.sh          # writes dist/anchor_<version>_all.deb
    sudo dpkg -i dist/anchor_*.deb
    sudo apt-get -f install        # if anything is missing

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
