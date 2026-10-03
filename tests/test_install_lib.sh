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
parse_install_args --components=daemon,web,memory
assert_equals "daemon,web,memory" "$COMPONENTS_FLAG" "component selection flag should be parsed"
parse_install_args
assert_equals "" "$COMPONENTS_FLAG" "component selection resets between calls"
parse_install_args --components=daemon --dry-run
assert_equals "1" "$DRY_RUN_FLAG" "dry-run flag should be parsed"
resolve_install_components
assert_equals "1" "$COMPONENT_DAEMON" "daemon component selected"
assert_equals "0" "$COMPONENT_WEB" "omitted web component remains untouched"
assert_equals "0" "$COMPONENT_MEMORY" "omitted memory component remains untouched"
assert_equals "0" "$COMPONENT_NUVOTON" "omitted sensor setup remains untouched"
PLAN_OUTPUT="$(print_install_plan)"
case "$PLAN_OUTPUT" in
    *"Install components: daemon"*"Preserve existing web service and dashboard config"*"Preserve existing memory service and data"*) ;;
    *) echo "ASSERTION FAILED: dry-run plan must show selections and preserved components" >&2; printf '%s\n' "$PLAN_OUTPUT" >&2; exit 1 ;;
esac
parse_install_args
resolve_install_components
assert_equals "1" "$COMPONENT_DAEMON" "default plan includes daemon"
assert_equals "1" "$COMPONENT_WEB" "default plan includes web"
assert_equals "1" "$COMPONENT_MEMORY" "default plan includes memory choice"
assert_equals "1" "$COMPONENT_NUVOTON" "default plan includes sensor setup"
parse_install_args --components=daemon,unknown
if resolve_install_components 2>/dev/null; then
    echo "ASSERTION FAILED: unknown component must be rejected" >&2
    exit 1
fi
# Kernel sensor module detection (modinfo/modprobe stubbed; a module name
# that can never exist in /sys/module or modules-load.d).
modinfo() { [ "$1" = fake_bc250_mod ]; }
modprobe() { [ "$1" = --showconfig ] && printf '%s\n' "${FAKE_MODPROBE_CONFIG:-}"; }
FAKE_MODPROBE_CONFIG="blacklist fake_bc250_mod"
assert_equals "blacklisted" "$(kernel_module_state fake_bc250_mod)" "blacklisted module is opt-in"
FAKE_MODPROBE_CONFIG="alias dmi*:rn*AMDBC_250*: fake_bc250_mod"
assert_equals "autoload" "$(kernel_module_state fake_bc250_mod)" "installed module autoloads via DMI alias"
assert_equals "" "$(kernel_module_state missing_bc250_mod)" "absent module"
unset -f modinfo modprobe

# Without a terminal, a running/enabled collector is kept and never counts as an
# opt-out; only an explicit --no-memory-temp is one.
parse_install_args
choose_memory_monitoring 1 </dev/null
assert_equals "1" "$MEMORY_ENABLED" "no terminal keeps an existing collector"
assert_equals "0" "$MEMORY_OPT_OUT" "no terminal is not an opt-out"
choose_memory_monitoring 0 </dev/null
assert_equals "0" "$MEMORY_OPT_OUT" "fresh unattended install is not an opt-out either"
parse_install_args --no-memory-temp
choose_memory_monitoring 1 </dev/null
assert_equals "0" "$MEMORY_ENABLED" "explicit --no-memory-temp disables"
assert_equals "1" "$MEMORY_OPT_OUT" "explicit --no-memory-temp is an opt-out"
parse_install_args --memory-temp
choose_memory_monitoring 0 </dev/null
assert_equals "1" "$MEMORY_ENABLED" "--memory-temp enables"
assert_equals "0" "$MEMORY_OPT_OUT" "--memory-temp is not an opt-out"
parse_install_args

# Regression: install.sh runs with pipefail, and the real "modprobe --showconfig"
# prints thousands of lines; an early-exiting grep must not make detection fail.
modinfo() { [ "$1" = fake_bc250_mod ]; }
modprobe() { { echo "blacklist fake_bc250_mod"; seq 1 20000; } ; }
assert_equals "blacklisted" "$(kernel_module_state fake_bc250_mod)" "blacklist found in a long modprobe config under pipefail"
unset -f modinfo modprobe
FEATURE_BIN="$(mktemp)"
{ printf '\177ELF'; printf 'BC250_TELEMETRY_FEATURES=memory_snapshot_v1,vrm_hwmon_v1,config_v1\0'; head -c 100000 /dev/zero; } > "$FEATURE_BIN"
binary_has_feature "$FEATURE_BIN" config_v1 || { echo "ASSERTION FAILED: feature present" >&2; exit 1; }
binary_has_feature "$FEATURE_BIN" memory_snapshot_v1 || { echo "ASSERTION FAILED: first feature present" >&2; exit 1; }
if binary_has_feature "$FEATURE_BIN" config; then echo "ASSERTION FAILED: partial feature name must not match" >&2; exit 1; fi
rm -f "$FEATURE_BIN"

# Detecting a user's manual "blacklist bc250_vrm" (exact directive only).
BL_DIR="$(mktemp -d)"
printf '# comment\nblacklist bc250_vrm\n' > "$BL_DIR/blacklist-vrm.conf"
printf 'blacklist bc250_memory\n# blacklist bc250_vrm\nblacklist bc250_vrm_extra\n' > "$BL_DIR/other.conf"
MODPROBE_DIRS="$BL_DIR" blacklist_files_for bc250_vrm > "$BL_DIR/out"
assert_equals "$BL_DIR/blacklist-vrm.conf" "$(cat "$BL_DIR/out")" "only the exact blacklist directive is reported"
MODPROBE_DIRS="$BL_DIR" blacklist_files_for bc250_memory > "$BL_DIR/out"
assert_equals "$BL_DIR/other.conf" "$(cat "$BL_DIR/out")" "memory blacklist is found separately"
MODPROBE_DIRS="$BL_DIR/missing" blacklist_files_for bc250_vrm > "$BL_DIR/out"
assert_equals "" "$(cat "$BL_DIR/out")" "missing directories are ignored"
rm -rf "$BL_DIR"

BEFORE_TEST_BINARY="$(sha256sum "$ROOT_DIR/apu_telemetry" 2>/dev/null || true)"
DRY_RUN_OUTPUT="$(bash "$ROOT_DIR/install.sh" --components=daemon --dry-run)"
AFTER_TEST_BINARY="$(sha256sum "$ROOT_DIR/apu_telemetry" 2>/dev/null || true)"
assert_equals "$BEFORE_TEST_BINARY" "$AFTER_TEST_BINARY" "dry-run must not build or replace binaries"
case "$DRY_RUN_OUTPUT" in
    *"Install components: daemon"*"Preserve existing web service and dashboard config"*"Preserve existing memory service and data"*) ;;
    *) echo "ASSERTION FAILED: installer dry-run must exit with a daemon-only preservation plan" >&2; printf '%s\n' "$DRY_RUN_OUTPUT" >&2; exit 1 ;;
esac

parse_install_args
assert_equals "" "$DASHBOARD_FLAG" "no flags should leave dashboard flag empty"
assert_equals "auto" "$MEMORY_TEMP_FLAG" "no flag allows the interactive memory step"

parse_install_args --memory-temp --dashboard=v2
assert_equals "1" "$MEMORY_TEMP_FLAG" "memory flag should be parsed"
assert_equals "v2" "$DASHBOARD_FLAG" "memory and dashboard flags can be combined"
parse_install_args
assert_equals "auto" "$MEMORY_TEMP_FLAG" "memory flag should reset between calls"
parse_install_args --no-memory-temp --components=daemon
assert_equals "0" "$MEMORY_TEMP_FLAG" "explicit memory opt-out should be parsed"
assert_equals "daemon" "$COMPONENTS_FLAG" "explicit components are retained"
parse_install_args
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
