#!/bin/sh
# bootstrap.sh — the delivery spine for the Gab-Agent installer.
#
# Runs BEFORE Python/uv necessarily exist, so it is intentionally tiny POSIX sh (not vendored Python):
# it provisions the interpreter, then hands off to the workstation wizard. This is the one channel that
# reaches both Arch and Debian (the AUR package is an Arch-only convenience on top).
#
# uv policy: use it if present; else offer to install via the distro package manager; else print the
# official command. Auto-install only behind --yes — never an unprompted `curl … | sh`.
#
# Usage:  ./bootstrap.sh [--yes]
set -eu

ASSUME_YES=0
for arg in "$@"; do
    case "$arg" in
        --yes|-y) ASSUME_YES=1 ;;
        *) printf 'Unknown argument: %s\n' "$arg" >&2; exit 2 ;;
    esac
done

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$REPO_DIR"

info() { printf '\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }

detect_pm() {
    # Echo the distro package-manager install command prefix, or nothing if unknown.
    if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        for id in ${ID:-} ${ID_LIKE:-}; do
            case "$id" in
                arch|archlinux|cachyos|endeavouros|manjaro) echo "sudo pacman -S --needed uv"; return ;;
                debian|ubuntu|raspbian|linuxmint|pop) echo "sudo apt install -y uv"; return ;;
                fedora|rhel|centos) echo "sudo dnf install -y uv"; return ;;
            esac
        done
    fi
}

ensure_uv() {
    if command -v uv >/dev/null 2>&1; then
        return 0
    fi
    info "uv (the Python provisioner) is not installed."
    PM_CMD=$(detect_pm)
    if [ -n "${PM_CMD:-}" ]; then
        note "Install command: $PM_CMD"
        if [ "$ASSUME_YES" -eq 1 ]; then
            # shellcheck disable=SC2086
            $PM_CMD && return 0
        else
            printf '  Install uv now with that command? [y/N] '
            read -r reply
            case "$reply" in
                y|Y)
                    # shellcheck disable=SC2086
                    $PM_CMD && return 0 ;;
            esac
        fi
    fi
    note "Install uv manually, then re-run ./bootstrap.sh :"
    note "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    note "  (or see https://docs.astral.sh/uv/getting-started/installation/ )"
    return 1
}

info "Gab-Agent bootstrap"
ensure_uv || exit 1

# Run the workstation wizard in-place via uv (provisions a matching Python + the package deps as needed).
info "Launching the installer…"
exec uv run --quiet python -m gabagent.install "$@"
