#!/usr/bin/env bash
# firstrun_smoke.sh — first-run fail-soft gate against a REALLY absent optional package.
#
# The default AUR install carries only the PKGBUILD `depends` (pacman does NOT auto-install optdepends).
# `anthropic` and `playwright` are pyproject CORE deps demoted to optdepends — so a fresh user runs WITHOUT
# them. This gate reproduces that install (clean venv with the core deps MINUS the optdepend-gated ones,
# wheel --no-deps) and asserts every optdepend-gated seam FAILS SOFT (a friendly error / clean omission),
# never a raw ModuleNotFoundError. It is the behavioral backstop the packaging superset check (Gate 1) is
# blind to — an in-tree pytest cannot uninstall a dependency it is running under.
#
# Inject-verify: remove any guard (e.g. the _load_claudette try/except) and this gate must FAIL.
set -euo pipefail

# The optdepend-gated packages — pyproject-core but PKGBUILD-optdepend, so absent on a default install.
# KEEP THIS LIST EXPLICIT: a NEW optional backend added later must be added here (and given a fail-soft
# guard) or it silently escapes this assertion.
OPTDEP_GATED="anthropic playwright"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# /tmp may be noexec on dev boxes; put the venv somewhere executable (CI $HOME is fine).
SMOKE_DIR="${SMOKE_DIR:-$HOME/.cache/gab-firstrun-smoke}"
VENV="$SMOKE_DIR/venv"

echo "firstrun-smoke: building wheel…"
rm -rf "$SMOKE_DIR"; mkdir -p "$SMOKE_DIR"
uv build --wheel --out-dir "$SMOKE_DIR/dist" >/dev/null
WHL="$(ls -t "$SMOKE_DIR"/dist/*.whl | head -1)"

echo "firstrun-smoke: creating clean venv with core deps MINUS ($OPTDEP_GATED)…"
python3 -m venv "$VENV"
# Core deps from pyproject, dropping the optdepend-gated ones — reproduces a default `pacman -S gabagent`.
mapfile -t CORE < <(python3 - "$OPTDEP_GATED" <<'PY'
import re, sys, tomllib, pathlib
gated = set(sys.argv[1].split())
pp = tomllib.loads(pathlib.Path("pyproject.toml").read_text())
for spec in pp["project"].get("dependencies", []):
    name = re.split(r"[<>=!~ \[;]", spec.strip(), maxsplit=1)[0]
    if name.lower() not in gated:
        print(spec)
PY
)
"$VENV/bin/pip" install -q "${CORE[@]}"
"$VENV/bin/pip" install -q --no-deps "$WHL"

# Confirm the gated packages really are absent (guards against a polluted base venv false-passing).
for pkg in $OPTDEP_GATED; do
    if "$VENV/bin/pip" show "$pkg" >/dev/null 2>&1; then
        echo "firstrun-smoke: FAIL — '$pkg' is present in the clean venv; cannot test its absence." >&2
        exit 1
    fi
done

echo "firstrun-smoke: asserting every optdepend-gated seam fails SOFT…"
"$VENV/bin/python" - <<'PY'
import sys
ANTHROPIC_HINT = "anthropic"
# 0) CLI import + the default backend must work with the gated packages absent.
import gabagent.cli  # noqa: F401

from gabagent.config.loader import load_config
from gabagent.api.factory import build_client, anthropic_configured, anthropic_available
from gabagent.api.rate_limit import UsageTracker

fails = []

# 1) the availability probe must report absent.
if anthropic_available():
    fails.append("anthropic_available() True in a venv without anthropic")

# 2) key set but package absent -> not configured (ladder omits the rung instead of crashing).
cfg = load_config({})
cfg.claude.api_key = "sk-test"
if anthropic_configured(cfg) is not False:
    fails.append("anthropic_configured() not False despite absent package")

# 3) explicit provider=claude -> friendly RuntimeError, never a raw ModuleNotFoundError.
cfg.provider = "claude"
try:
    build_client(cfg, UsageTracker(simple_model="x"))
    fails.append("build_client(claude) did not raise (expected friendly error)")
except ModuleNotFoundError as e:
    fails.append(f"build_client(claude) raised RAW ModuleNotFoundError: {e}")
except RuntimeError as e:
    if ANTHROPIC_HINT not in str(e):
        fails.append(f"build_client(claude) RuntimeError lacks an install hint: {e}")

# 4) the browser command -> friendly RuntimeError, never a raw ModuleNotFoundError.
import asyncio
import gabagent.commands.browser as browser

class _Ctx:
    persistent_browser = None

try:
    asyncio.run(browser.ensure_browser(_Ctx()))
    fails.append("ensure_browser did not raise (expected friendly error)")
except ModuleNotFoundError as e:
    fails.append(f"ensure_browser raised RAW ModuleNotFoundError: {e}")
except RuntimeError:
    pass  # friendly

if fails:
    print("firstrun-smoke: FAIL")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("firstrun-smoke: OK — CLI runs and every optdepend-gated seam fails soft.")
PY

echo "firstrun-smoke: PASS"
