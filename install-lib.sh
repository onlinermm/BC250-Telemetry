#!/bin/bash

DASHBOARD_FLAG=""
MEMORY_TEMP_FLAG=auto

parse_install_args() {
    DASHBOARD_FLAG=""
    MEMORY_TEMP_FLAG=auto

    for arg in "$@"; do
        case "$arg" in
            --memory-temp)
                MEMORY_TEMP_FLAG=1
                ;;
            --no-memory-temp)
                MEMORY_TEMP_FLAG=0
                ;;
            --dashboard=*)
                DASHBOARD_FLAG="${arg#--dashboard=}"
                ;;
            *)
                echo "Warning: unrecognized option '$arg' — ignoring it." >&2
                ;;
        esac
    done
}

# Empty/invalid input retains the existing setting (off on a fresh install).
memory_choice() {
    local answer="$1" fallback="$2"
    case "$answer" in
        y|Y|yes|YES|так|Так|1) echo 1 ;;
        n|N|no|NO|ні|Ні|0) echo 0 ;;
        "") echo "$fallback" ;;
        *) echo "Unrecognized answer; keeping the current memory setting." >&2
           echo "$fallback" ;;
    esac
}

choose_memory_monitoring() {
    local existing="$1" answer="" hint="y/N"
    MEMORY_ENABLED="$existing"
    if [ "$MEMORY_TEMP_FLAG" != auto ]; then
        MEMORY_ENABLED="$MEMORY_TEMP_FLAG"
    elif [ -t 0 ]; then
        [ "$existing" = 1 ] && hint="Y/n"
        read -r -t 30 -p "Enable GDDR6 memory temperatures (experimental, BC-250 P3.0)? [$hint]: " answer || answer=""
        MEMORY_ENABLED="$(memory_choice "$answer" "$existing")"
    fi
}

# Echoes "nct6683" or "nct6687" if either sensor module is already loaded,
# empty otherwise. install.sh doesn't touch anything when this comes back non-
# empty — an already-active driver (from a previous run, bc250-monitoring, or
# the distro itself) is left as is.
detect_active_sensor_driver() {
    if lsmod 2>/dev/null | grep -q '^nct6687 '; then
        echo "nct6687"
    elif lsmod 2>/dev/null | grep -q '^nct6683 '; then
        echo "nct6683"
    else
        echo ""
    fi
}

# Preferred driver config: the in-kernel nct6683 is read-only on this board and
# can't drive PWM fan curves, so we autoload the out-of-tree nct6687 instead
# and keep nct6683 from ever binding to the same chip.
write_nct6687_driver_configs() {
    sudo mkdir -p /etc/modules-load.d /etc/modprobe.d
    echo "nct6687" | sudo tee /etc/modules-load.d/99-sensors.conf > /dev/null
    sudo tee /etc/modprobe.d/sensors.conf > /dev/null <<EOF
blacklist nct6683
options nct6687 force=true
EOF
}

# Build and load the out-of-tree nct6687 driver (PWM fan control) via DKMS.
# Returns 0 on success; on any failure (missing toolchain/headers, failed
# clone or dkms build) it cleans up after itself and returns 1, so install.sh
# can fall back to the in-kernel, monitoring-only nct6683.
install_nct6687_via_dkms() {
    local builddir=""
    for tool in git make gcc dkms; do
        command -v "$tool" >/dev/null 2>&1 || return 1
    done
    # Kernel headers for the running kernel are required to build anything.
    [ -d "/lib/modules/$(uname -r)/build" ] || return 1

    builddir="$(mktemp -d)"
    if ! git clone --depth 1 --quiet https://github.com/Fred78290/nct6687d.git \
        "$builddir" 2>/dev/null; then
        rm -rf "$builddir"
        return 1
    fi

    # Keep the BC-250 "CPU Fan"/"Pump Fan" labels correct (both fan config
    # variants get swapped) — see https://github.com/pavelhot-oss/bc250-monitoring.
    if [ -f "$SCRIPT_DIR/50-nct6687-labels.patch" ]; then
        if git -C "$builddir" apply --check "$SCRIPT_DIR/50-nct6687-labels.patch" 2>/dev/null; then
            git -C "$builddir" apply "$SCRIPT_DIR/50-nct6687-labels.patch"
        else
            echo -e "${YELLOW}⚠ Label patch no longer applies to the nct6687d source — installing without it.${NC}"
        fi
    fi

    # Idempotent: re-runs over an already-DKMS-installed driver re-register it.
    sudo dkms remove nct6687d/1 --all 2>/dev/null || true
    sudo rm -rf /usr/src/nct6687d-1

    if ! sudo dkms add "$builddir" 2>/dev/null ||
       ! sudo dkms build nct6687d/1 2>/dev/null ||
       ! sudo dkms install nct6687d/1 --force 2>/dev/null; then
        echo -e "${YELLOW}⚠ nct6687 DKMS build failed — see /var/lib/dkms/nct6687d/1/build/make.log if it exists.${NC}"
        rm -rf "$builddir"
        return 1
    fi
    rm -rf "$builddir"
}

# Fallback (monitoring only — no PWM control).
write_sensor_driver_configs() {
    sudo mkdir -p /etc/modules-load.d /etc/modprobe.d
    echo "nct6683" | sudo tee /etc/modules-load.d/99-sensors.conf > /dev/null
    echo "options nct6683 force=true" | sudo tee /etc/modprobe.d/sensors.conf > /dev/null
}

# Fallback (monitoring only — no PWM control).
load_sensor_driver() {
    sudo modprobe nct6683 force=true
}

# Rejects a "successful" build/prebuilt binary that isn't actually a valid
# ELF executable — a corrupted download, wrong architecture, or empty
# placeholder would otherwise sail through STEP 2 and only surface much
# later, when systemd tries (and fails) to start the service.
verify_binary() {
    local bin="$1"
    if [ ! -s "$bin" ]; then
        return 1
    fi
    local magic
    magic="$(head -c4 "$bin" 2>/dev/null | od -An -tx1 | tr -d ' \n')"
    [ "$magic" = "7f454c46" ]
}

supports_memory_snapshot() {
    verify_binary "$1" &&
        LC_ALL=C grep -aFq 'BC250_TELEMETRY_FEATURES=memory_snapshot_v1' "$1"
}
