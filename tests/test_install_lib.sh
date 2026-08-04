#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/install-lib.sh"

assert_equals() {
    local expected="$1"
    local actual="$2"
    local message="$3"
    if [ "$expected" != "$actual" ]; then
        echo "ASSERTION FAILED: $message" >&2
        echo "  expected: $expected" >&2
        echo "  actual:   $actual" >&2
        exit 1
    fi
}

parse_install_args --dashboard=v2
assert_equals "v2" "$DASHBOARD_FLAG" "dashboard flag should be parsed"

parse_install_args
assert_equals "" "$DASHBOARD_FLAG" "no flags should leave dashboard flag empty"

WARNING_OUTPUT="$(parse_install_args --fan-control 2>&1 1>/dev/null)"
if [ -z "$WARNING_OUTPUT" ]; then
    echo "ASSERTION FAILED: an unrecognized flag should print a warning to stderr" >&2
    exit 1
fi
assert_equals "" "$DASHBOARD_FLAG" "an unrecognized flag should not set the dashboard flag"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

printf '\x7fELFjunkbytes' > "$TMPDIR/valid.bin"
if ! verify_binary "$TMPDIR/valid.bin"; then
    echo "ASSERTION FAILED: a file starting with the ELF magic should verify" >&2
    exit 1
fi

printf 'not an elf file' > "$TMPDIR/bogus.bin"
if verify_binary "$TMPDIR/bogus.bin"; then
    echo "ASSERTION FAILED: a non-ELF file should fail verification" >&2
    exit 1
fi

: > "$TMPDIR/empty.bin"
if verify_binary "$TMPDIR/empty.bin"; then
    echo "ASSERTION FAILED: an empty file should fail verification" >&2
    exit 1
fi

if ! verify_binary "$(command -v ls)"; then
    echo "ASSERTION FAILED: a real system binary (ls) should verify" >&2
    exit 1
fi

echo "install-lib tests passed"
