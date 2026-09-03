#!/usr/bin/env bash
# dev-install.sh -- copy the skill into a Claude Code skills dir for local testing.
# The allowlist below is the same set the harness Dockerfile copies into the image,
# so what you test locally is what runs headlessly.
#
# Usage:
#   ./dev-install.sh                 # -> ~/.claude/skills/envbuild        (user-level)
#   ./dev-install.sh <skills-dir>    # -> <skills-dir>/envbuild            (e.g. project .claude/skills)
#
# Re-run after editing any skill file; open a new Claude Code session to pick it up.
set -euo pipefail

name="envbuild"
src="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # repo root = this script's own dir

skills_dir="${1:-$HOME/.claude/skills}"
dest="$skills_dir/$name"

for required in SKILL.md config.ini compose.yaml compose.host.yaml specs src/driver.py \
                harness/Dockerfile; do
  test -e "$src/$required" || { echo "$required missing in $src"; exit 1; }
done

# Clear only the skill's own targets so renamed/removed files do not linger.
rm -rf "$dest/SKILL.md" "$dest/config.ini" "$dest/compose.yaml" "$dest/compose.host.yaml" \
       "$dest/run.sh" \
       "$dest/specs" "$dest/src" "$dest/scripts" "$dest/harness" "$dest/fixtures"
mkdir -p "$dest/specs" "$dest/src" "$dest/scripts" "$dest/harness/pi" "$dest/fixtures"

cp "$src/SKILL.md" "$src/config.ini" "$src/compose.yaml" "$src/compose.host.yaml" "$dest/"
cp "$src/run.sh" "$dest/run.sh"
cp "$src"/specs/*.md         "$dest/specs/"
cp "$src"/src/*.py           "$dest/src/"
cp "$src"/scripts/*.py       "$dest/scripts/"
cp -r "$src"/harness/.       "$dest/harness/"
cp -r "$src"/fixtures/.      "$dest/fixtures/"

echo "Installed $name -> $dest"
find "$dest" -type f | sort
