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
# NB: a '**/*.py' git pathspec does NOT match files directly under the dir — list the dirs and filter .py.
untracked=$(git ls-files --others --exclude-standard -- src/gabagent installkit | grep '\.py$' || true)
for f in $untracked; do
  base=$(basename "$f" .py)
  [ "$base" = "__init__" ] && continue
  # Module's dotted path, e.g. src/gabagent/x.py -> gabagent.x ; installkit/y.py -> installkit.y
  dotted=$(printf '%s' "$f" | sed -E 's#^src/##; s#\.py$##; s#/#.#g')
  parent=${dotted%.*}; leaf=${dotted##*.}
  # FIRE if tracked code references the dotted path ('import pkg.mod' / 'from pkg.mod import ...') OR
  # the from-parent form ('from pkg import mod'). git grep searches TRACKED files only, so the untracked
  # module can't match itself. Aliased/dynamic imports stay regex-uncatchable — the canary is the backstop.
  if git grep -lFe "$dotted" -- '*.py' 2>/dev/null | grep -q . \
     || git grep -lE "from +${parent//./\\.} +import +([A-Za-z0-9_, ]*[, ])?${leaf}\b" -- '*.py' 2>/dev/null | grep -q .; then
    echo "PARITY FAIL: untracked module '$f' (module '$dotted') is imported by tracked code (New-Module Deploy-Safety SOP)."
    echo "  git-track it in the same commit, or fail-soft guard the import+construction."
    fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  echo "installer-parity pre-filter FAILED."
  exit 1
fi
echo "installer-parity pre-filter OK (canary=$field_count fields; delta base=$base)."
