#!/usr/bin/env bash
# Installer-parity delta pre-filter (Gate 3) — see the Installer Parity HARD SOP in CLAUDE.md.
#
# A fast, delta-scoped nudge for the pending change. NOT the primary forcing-function — that is the
# automatic pytest suite (tests/unit/test_installer_parity.py, Gates 1/1b/2), run in CI. This catches
# two things a diff review misses:
#   1. a new user-facing config knob (a `Field(...)` added to config/models.py) that may need installer /
#      docs wiring — ADVISORY (prints a reminder; never blocks);
#   2. a git-untracked local module that tracked code imports — BLOCKS (exit 1): the New-Module
#      Deploy-Safety SOP (the satellite rsync syncs git-tracked files only, so a tracked importer of an
#      untracked module is a guaranteed satellite crash).
#
# It also self-guards with a CANARY: the knob detector must still match its idiom, or it is mis-wired and
# silently green — a scan that finds nothing is a false pass, not a pass.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

MODELS="src/gabagent/config/models.py"
CANARY_FLOOR=25          # current tree has ~34 `Field(`; a scan below this floor = detector mis-wired
fail=0

# --- Canary: the knob idiom must still be detectable -------------------------------------------------
field_count=$(grep -cE "Field\(" "$MODELS" || true)
if [ "${field_count:-0}" -lt "$CANARY_FLOOR" ]; then
  echo "PARITY FAIL (canary): only $field_count 'Field(' matches in $MODELS (floor $CANARY_FLOOR)."
  echo "  The config-knob detector is mis-wired or the file moved — a zero/low match is a FALSE GREEN."
  fail=1
fi

# --- Delta base: merge-base with origin/master (not HEAD) --------------------------------------------
base="HEAD"
if git rev-parse --verify -q origin/master >/dev/null; then
  base="$(git merge-base origin/master HEAD)"
fi

# --- (advisory) new config knobs in the delta -------------------------------------------------------
new_fields=$(git diff "$base"...HEAD -- "$MODELS" 2>/dev/null | grep -E "^\+.*: .*= *Field\(" | sed -E 's/^\+ *//' || true)
if [ -n "$new_fields" ]; then
  echo "PARITY NOTE: new config knob(s) added since $base — confirm installer/docs wiring per the SOP:"
  echo "$new_fields" | sed 's/^/    /'
fi

# --- (blocking) untracked local module imported by tracked code -------------------------------------
untracked=$(git ls-files --others --exclude-standard -- 'src/gabagent/**/*.py' 'installkit/**/*.py' || true)
for f in $untracked; do
  mod=$(basename "$f" .py)
  [ "$mod" = "__init__" ] && continue
  if git grep -lE "(^|[^.])\b(import +${mod}\b|from +[A-Za-z0-9_.]*\.?${mod} +import)" -- '*.py' \
       | grep -vqx "$f"; then
    echo "PARITY FAIL: untracked module '$f' is imported by tracked code (New-Module Deploy-Safety SOP)."
    echo "  git-track it in the same commit, or fail-soft guard the import+construction."
    fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  echo "installer-parity pre-filter FAILED."
  exit 1
fi
echo "installer-parity pre-filter OK (canary=$field_count fields; delta base=$base)."
