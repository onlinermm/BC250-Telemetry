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
# empty otherwise. install.sh only sets up nct6683 when this comes back empty
# — an already-active driver (from a previous run, or the distro itself) is
# left as is.
detect_active_sensor_driver() {
    if lsmod 2>/dev/null | grep -q '^nct6687 '; then
        echo "nct6687"
    elif lsmod 2>/dev/null | grep -q '^nct6683 '; then
        echo "nct6683"
    else
        echo ""
    fi
}

write_sensor_driver_configs() {
    sudo mkdir -p /etc/modules-load.d /etc/modprobe.d
    echo "nct6683" | sudo tee /etc/modules-load.d/99-sensors.conf > /dev/null
    echo "options nct6683 force=true" | sudo tee /etc/modprobe.d/sensors.conf > /dev/null
}

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
