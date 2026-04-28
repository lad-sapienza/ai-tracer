#!/usr/bin/env bash
# release.sh — interactive release helper for AITracer by LAD
#
# Steps
#   1. Prompt for bump type (major / minor / fix) and a release message.
#   2. Update version in metadata.txt, main.py, backend/app.py.
#   3. Stage all modified tracked files, show the diff summary, commit.
#   4. Create a git tag.
#   5. Optionally push branch + tag to origin.
#   6. Remove stale zips and build a fresh distributable zip.

set -euo pipefail
cd "$(dirname "$0")"

# ── terminal helpers ──────────────────────────────────────────────────────
_green()  { printf '\033[32m%s\033[0m\n' "$*"; }
_yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
_red()    { printf '\033[31m%s\033[0m\n' "$*"; }
_bold()   { printf '\033[1m%s\033[0m\n' "$*"; }
_confirm() { local a; read -rp "$1 [y/N] " a; [[ "$a" =~ ^[Yy]$ ]]; }

# ── read current version ──────────────────────────────────────────────────
CURRENT=$(grep '^version=' plugin/metadata.txt | cut -d= -f2)
IFS='.' read -r MAJ MIN PAT <<< "$CURRENT"

echo ""
_bold "Current version: v${CURRENT}"
echo ""

# ── choose bump type ──────────────────────────────────────────────────────
echo "Bump type:"
echo "  1) major   →  v$((MAJ+1)).0.0"
echo "  2) minor   →  v${MAJ}.$((MIN+1)).0"
echo "  3) fix     →  v${MAJ}.${MIN}.$((PAT+1))"
echo ""
read -rp "Choice [1/2/3]: " BUMP_CHOICE
echo ""

case "$BUMP_CHOICE" in
    1) NEW_VER="$((MAJ+1)).0.0" ;;
    2) NEW_VER="${MAJ}.$((MIN+1)).0" ;;
    3) NEW_VER="${MAJ}.${MIN}.$((PAT+1))" ;;
    *) _red "Invalid choice. Aborting."; exit 1 ;;
esac

# ── release message ───────────────────────────────────────────────────────
read -rp "Release message: " MSG
[[ -z "$MSG" ]] && { _red "Message required. Aborting."; exit 1; }

echo ""
_bold "→ v${NEW_VER}: ${MSG}"
echo ""
_confirm "Proceed?" || { echo "Aborted."; exit 0; }
echo ""

# ── bump version in source files ──────────────────────────────────────────
# macOS sed needs '' after -i; GNU sed does not — try both.
_sed() { sed -i '' "$@" 2>/dev/null || sed -i "$@"; }

_sed "s/^version=.*/version=${NEW_VER}/"                                   plugin/metadata.txt
_sed "s/PLUGIN_VERSION = \"[^\"]*\"/PLUGIN_VERSION = \"${NEW_VER}\"/"      plugin/main.py
_sed "s/APP_VERSION = \"[^\"]*\"/APP_VERSION = \"${NEW_VER}\"/"            plugin/backend/app.py

_green "✓ Version updated to ${NEW_VER} in metadata.txt, main.py, backend/app.py"
echo ""

# ── stage & commit ────────────────────────────────────────────────────────
# Stage every modification to a tracked file (version files + any pending work).
git add -u

STAGED=$(git diff --cached --name-only)
if [[ -z "$STAGED" ]]; then
    _yellow "Nothing staged — skipping commit."
else
    echo "Files to commit:"
    echo "$STAGED" | sed 's/^/  /'
    echo ""
    _confirm "Commit these?" || { echo "Aborted."; exit 0; }

    git commit -m "$(printf 'v%s: %s\n\nCo-Authored-By: Julian Bogdani <julian.bogdani@uniroma1.it>' \
        "${NEW_VER}" "${MSG}")"
    _green "✓ Committed"
fi
echo ""

# ── tag ───────────────────────────────────────────────────────────────────
if git rev-parse "v${NEW_VER}" >/dev/null 2>&1; then
    _yellow "Tag v${NEW_VER} already exists — skipping."
else
    git tag "v${NEW_VER}"
    _green "✓ Tagged v${NEW_VER}"
fi
echo ""

# ── push ──────────────────────────────────────────────────────────────────
if _confirm "Push main + tag to origin?"; then
    git push origin main
    git push origin "v${NEW_VER}"
    _green "✓ Pushed"
    echo ""
fi

# ── package ───────────────────────────────────────────────────────────────
echo "Removing old zips…"
find dist -maxdepth 1 -name '*.zip' -delete 2>/dev/null || true

bash package.sh "${NEW_VER}"
echo ""
_green "✓ Release v${NEW_VER} complete."
echo ""
