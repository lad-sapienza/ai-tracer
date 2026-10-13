#!/usr/bin/env bash
# check.sh — pre-release validation for AITracer by LAD
#
# Mirrors the checks run by the QGIS Plugin Repository on upload:
#   1. Every plugin .py file compiles.
#   2. No unqualified Qt6 enum members (e.g. QFrame.HLine) — see check_qt6.py.
#   3. Bandit reports no issues (the repository blocks plugins with findings).
#
# Usage: ./check.sh
# Requires bandit:  pip install bandit

set -euo pipefail
cd "$(dirname "$0")"

_green() { printf '\033[32m%s\033[0m\n' "$*"; }
_red()   { printf '\033[31m%s\033[0m\n' "$*"; }

# ── 1. syntax ─────────────────────────────────────────────────────────────
if ! find plugin -name '*.py' -not -path '*/__pycache__/*' -print0 \
        | xargs -0 python3 -m py_compile; then
    _red "✗ Syntax check failed."
    exit 1
fi
_green "✓ Python files compile"

# ── 2. Qt6 enums ──────────────────────────────────────────────────────────
if python3 check_qt6.py plugin; then
    _green "✓ Qt6 enums: all fully qualified"
else
    _red "✗ Unqualified enum members found — the QGIS Plugin Repository would flag them."
    exit 1
fi

# ── 3. bandit ─────────────────────────────────────────────────────────────
if command -v bandit >/dev/null 2>&1; then
    BANDIT=(bandit)
elif python3 -m bandit --version >/dev/null 2>&1; then
    BANDIT=(python3 -m bandit)
else
    _red "✗ bandit not found. Install it with: pip install bandit"
    exit 1
fi

if "${BANDIT[@]}" -r plugin -q; then
    _green "✓ Bandit: no issues"
else
    _red "✗ Bandit reported issues — the QGIS Plugin Repository would block this version."
    exit 1
fi
