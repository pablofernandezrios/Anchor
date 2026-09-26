#!/bin/sh
# Build the .deb (SPEC 17, 19).
#
# The packaging lives under packaging/deb/debian/ rather than at the root,
# because three formats share this repository and only one of them may own
# the name "debian". dpkg-buildpackage insists on finding it at the source
# root, so the tree is assembled in a scratch directory instead of the
# checkout being rearranged around one packaging format.
#
#   sh tools/build_deb.sh [output-directory]
#
# Needs: debhelper, dh-python, python3-all, python3-setuptools.
set -eu

# Said here rather than left to `dpkg-buildpackage: not found`, which tells
# somebody who has never built a Debian package nothing at all.
missing=""
for tool in dpkg-buildpackage dh; do
	command -v "$tool" >/dev/null 2>&1 || missing="yes"
done
if [ -n "$missing" ]; then
	cat >&2 <<'MSG'
The Debian build tools are not installed. Anchor needs them only to build the
package, not to run. Install them with:

  sudo apt-get install -y debhelper dh-python pybuild-plugin-pyproject \
      python3-all python3-setuptools python3-pytest

Or skip building altogether: install a .deb somebody else built.
MSG
	exit 1
fi

root=$(cd "$(dirname "$0")/.." && pwd)
out=${1:-$root/dist}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

tree=$work/anchor
mkdir -p "$tree"
# Only what is tracked: a stray .venv or build/ would be packaged otherwise.
git -C "$root" archive HEAD | tar -x -C "$tree"
cp -r "$root/packaging/deb/debian" "$tree/debian"
chmod +x "$tree/debian/rules" "$tree/debian/postinst" "$tree/debian/prerm" 2>/dev/null || true

(cd "$tree" && dpkg-buildpackage -us -uc -b)

mkdir -p "$out"
cp "$work"/*.deb "$out"/
echo
echo "Built:"
ls -1 "$out"/*.deb
