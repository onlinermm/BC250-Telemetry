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
assert_equals "auto" "$MEMORY_TEMP_FLAG" "no flag allows the interactive memory step"

parse_install_args --memory-temp --dashboard=v2
assert_equals "1" "$MEMORY_TEMP_FLAG" "memory flag should be parsed"
assert_equals "v2" "$DASHBOARD_FLAG" "memory and dashboard flags can be combined"
parse_install_args
assert_equals "auto" "$MEMORY_TEMP_FLAG" "memory flag should reset between calls"
choose_memory_monitoring 0 </dev/null
assert_equals "0" "$MEMORY_ENABLED" "unattended fresh installs default to off"
choose_memory_monitoring 1 </dev/null
assert_equals "1" "$MEMORY_ENABLED" "unattended updates preserve enabled monitoring"
parse_install_args --no-memory-temp
choose_memory_monitoring 1 </dev/null
assert_equals "0" "$MEMORY_ENABLED" "explicit disabling overrides existing settings"
parse_install_args --memory-temp
choose_memory_monitoring 0 </dev/null
assert_equals "1" "$MEMORY_ENABLED" "explicit enabling works without a TTY"
assert_equals "1" "$(memory_choice yes 0)" "yes enables monitoring"
assert_equals "0" "$(memory_choice no 1)" "no disables monitoring"
assert_equals "1" "$(memory_choice '' 1)" "Enter preserves enabled monitoring"
parse_install_args

WARNING_OUTPUT="$(parse_install_args --fan-control 2>&1 1>/dev/null)"
if [ -z "$WARNING_OUTPUT" ]; then
    echo "ASSERTION FAILED: an unrecognized flag should print a warning to stderr" >&2
    exit 1
fi
assert_equals "" "$DASHBOARD_FLAG" "an unrecognized flag should not set the dashboard flag"

# install_nct6687_via_dkms must return 1 (allowing the nct6683 fallback) rather
# than erroring out when the build toolchain isn't available. With a trimmed
# PATH no tool is found, so the function short-circuits before ever using sudo.
OLD_PATH="$PATH"
PATH="/nonexistent"
if install_nct6687_via_dkms; then
    echo "ASSERTION FAILED: install_nct6687_via_dkms must fail when the toolchain is unavailable" >&2
    exit 1
fi
PATH="$OLD_PATH"

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

if supports_memory_snapshot "$TMPDIR/valid.bin"; then
    echo "ASSERTION FAILED: an older ELF must not advertise memory support" >&2
    exit 1
fi
printf '\x7fELF\0BC250_TELEMETRY_FEATURES=memory_snapshot_v1\0' > "$TMPDIR/memory.bin"
if ! supports_memory_snapshot "$TMPDIR/memory.bin"; then
    echo "ASSERTION FAILED: explicit memory feature metadata should be recognized" >&2
    exit 1
fi
printf 'BC250_TELEMETRY_FEATURES=memory_snapshot_v1' > "$TMPDIR/not-elf"
if supports_memory_snapshot "$TMPDIR/not-elf"; then
    echo "ASSERTION FAILED: a marker in a non-ELF file is not a valid binary" >&2
    exit 1
fi

# Actual optimized builds caught the original bug: GCC need not retain a
# filesystem path as one contiguous string in the executable.
if command -v g++ >/dev/null 2>&1; then
    g++ -O2 -o "$TMPDIR/optimized-daemon" "$ROOT_DIR/bc250_telemetry.cpp"
    if ! supports_memory_snapshot "$TMPDIR/optimized-daemon"; then
        echo "ASSERTION FAILED: optimized daemon must retain its feature marker" >&2
        exit 1
    fi
fi

echo "install-lib tests passed"
