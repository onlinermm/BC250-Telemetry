#!/bin/bash

# modinfo/modprobe live in /usr/sbin on Debian/Ubuntu, which is not in a
# regular user's PATH there.
case ":$PATH:" in *:/usr/sbin:*) ;; *) PATH="$PATH:/usr/sbin:/sbin" ;; esac

DASHBOARD_FLAG=""
MEMORY_TEMP_FLAG=auto
COMPONENTS_FLAG=""
DRY_RUN_FLAG=0

parse_install_args() {
    DASHBOARD_FLAG=""
    MEMORY_TEMP_FLAG=auto
    COMPONENTS_FLAG=""
    DRY_RUN_FLAG=0

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
            --components=*)
                COMPONENTS_FLAG="${arg#--components=}"
                ;;
            --dry-run)
                DRY_RUN_FLAG=1
                ;;
            *)
                echo "Warning: unrecognized option '$arg' — ignoring it." >&2
                ;;
        esac
    done
}

# Empty/invalid input retains the existing setting (off on a fresh install).
# Only an explicit "no" (or --no-memory-temp) is an opt-out: a missing answer,
# no terminal or a timeout must never remove a working memory collector.
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

# Sets MEMORY_ENABLED (install/keep the collector) and MEMORY_OPT_OUT (the user
# explicitly declined, so an installed collector may be removed).
choose_memory_monitoring() {
    local existing="$1" answer="" hint="y/N"
    MEMORY_ENABLED="$existing"
    MEMORY_OPT_OUT=0
    if [ "$MEMORY_TEMP_FLAG" != auto ]; then
        MEMORY_ENABLED="$MEMORY_TEMP_FLAG"
        if [ "$MEMORY_TEMP_FLAG" = 0 ]; then MEMORY_OPT_OUT=1; fi
    elif [ -t 0 ]; then
        [ "$existing" = 1 ] && hint="Y/n"
        read -r -t 30 -p "Enable GDDR6 memory temperatures (experimental, BC-250 P3.0)? [$hint]: " answer || answer=""
        MEMORY_ENABLED="$(memory_choice "$answer" "$existing")"
        case "$answer" in n|N|no|NO|ні|Ні|0) MEMORY_OPT_OUT=1 ;; esac
    fi
}

# An installed collector counts as "existing" when it is enabled or running
# right now; a unit the user deliberately left disabled is not re-enabled.
memory_service_existing() {
    systemctl is-enabled --quiet bc250-memory.service 2>/dev/null ||
        systemctl is-active --quiet bc250-memory.service 2>/dev/null
}

# Echoes "nct6683" or "nct6687" if either sensor module is already loaded,
# empty otherwise. install.sh only sets up nct6683 when this comes back empty
# — an already-active driver (from a previous run, or the distro itself) is
# left as is.
detect_active_sensor_driver() {
    # Capture first: "cmd | grep -q" fails under pipefail when grep exits early
    # and cmd dies of SIGPIPE.
    local loaded
    loaded="$(lsmod 2>/dev/null || true)"
    if grep -q '^nct6687 ' <<<"$loaded"; then
        echo "nct6687"
    elif grep -q '^nct6683 ' <<<"$loaded"; then
        echo "nct6683"
    else
        echo ""
    fi
}

# State of a BC-250 kernel sensor module (bc250_vrm / bc250_memory, from
# linux-cachyos-bc250 or the Hexxeh *-dkms packages):
#   loaded      - loaded right now
#   autoload    - not loaded yet, but will be at boot: listed in modules-load.d,
#                 or its modalias matches this machine's DMI data (and it is
#                 not blacklisted)
#   blacklisted - installed but disabled
#   available   - installed, but nothing loads it: no modules-load.d entry and
#                 no matching alias (newer linux-cachyos-bc250 builds ship
#                 bc250_memory this way; it is opt-in)
#   ""          - not installed for the running kernel
# MODALIAS_FILE may be overridden (tests).
kernel_module_state() {
    local mod="$1"
    if [ -d "/sys/module/$mod" ]; then
        echo loaded
        return
    fi
    if grep -qsx "$mod" /etc/modules-load.d/*.conf /usr/lib/modules-load.d/*.conf /run/modules-load.d/*.conf; then
        echo autoload
        return
    fi
    if ! modinfo "$mod" >/dev/null 2>&1; then
        echo ""
        return
    fi
    # Capture command output first: under pipefail, "cmd | grep -q" reports
    # failure when grep exits early and cmd dies of SIGPIPE (the real
    # "modprobe --showconfig" prints ~2000 lines).
    local config resolved modalias
    config="$(modprobe --showconfig 2>/dev/null || true)"
    if grep -qx "blacklist $mod" <<<"$config"; then
        echo blacklisted
        return
    fi
    modalias="$(cat "${MODALIAS_FILE:-/sys/devices/virtual/dmi/id/modalias}" 2>/dev/null || true)"
    resolved=""
    if [ -n "$modalias" ]; then resolved="$(modprobe -R "$modalias" 2>/dev/null || true)"; fi
    if grep -qx "$mod" <<<"$resolved"; then
        echo autoload
    else
        echo available
    fi
}

# Prints the modprobe.d files that blacklist the given module (one per line).
# Matches only an exact "blacklist <module>" directive, ignoring comments.
# MODPROBE_DIRS may be overridden (tests).
blacklist_files_for() {
    local mod="$1" dir f
    for dir in ${MODPROBE_DIRS:-/etc/modprobe.d /usr/lib/modprobe.d /run/modprobe.d /lib/modprobe.d}; do
        [ -d "$dir" ] || continue
        for f in "$dir"/*.conf; do
            [ -f "$f" ] || continue
            if grep -Eq "^[[:space:]]*blacklist[[:space:]]+$mod[[:space:]]*(#.*)?$" "$f"; then
                echo "$f"
            fi
        done
    done
}

# True when the kernel bc250_memory driver owns (or will own after a reboot)
# GDDR6 reads. bc250-memory.service must not run alongside it: both send SMU
# queue 3 messages and collide (SMU wedges, fans ramp up).
kernel_memory_driver_active() {
    case "$(kernel_module_state bc250_memory)" in
        loaded|autoload) return 0 ;;
        *) return 1 ;;
    esac
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

# Feature tokens are listed in the BC250_TELEMETRY_FEATURES marker string
# compiled into the daemon; inspecting it never executes the binary.
binary_has_feature() {
    verify_binary "$1" || return 1
    local features
    features="$(LC_ALL=C grep -aoE 'BC250_TELEMETRY_FEATURES=[a-z0-9_,]+' "$1" || true)"
    features="${features%%$'\n'*}"
    case ",${features#*=}," in
        *",$2,"*) return 0 ;;
        *) return 1 ;;
    esac
}

# Resolve an explicit component list into flags. Default preserves the original
# full install behavior; omitted optional components are preserved, never removed.
resolve_install_components() {
    COMPONENT_DAEMON=0 COMPONENT_WEB=0 COMPONENT_MEMORY=0 COMPONENT_NUVOTON=0
    local list="${COMPONENTS_FLAG:-daemon,web,memory,nuvoton}" item
    [ -n "$COMPONENTS_FLAG" ] || list="daemon,web,memory,nuvoton"
    local IFS=,
    for item in $list; do
        case "$item" in
            daemon) COMPONENT_DAEMON=1 ;;
            web) COMPONENT_WEB=1 ;;
            memory) COMPONENT_MEMORY=1 ;;
            nuvoton) COMPONENT_NUVOTON=1 ;;
            "") echo "Empty component name" >&2; return 1 ;;
            *) echo "Unknown install component: $item" >&2; return 1 ;;
        esac
    done
    [ "$COMPONENT_DAEMON" = 1 ] || { echo "daemon component is required" >&2; return 1; }
}

print_install_plan() {
    local selected=""
    [ "$COMPONENT_DAEMON" = 1 ] && selected="daemon"
    [ "$COMPONENT_WEB" = 1 ] && selected="${selected:+$selected,}web"
    [ "$COMPONENT_MEMORY" = 1 ] && selected="${selected:+$selected,}memory"
    [ "$COMPONENT_NUVOTON" = 1 ] && selected="${selected:+$selected,}nuvoton"
    printf 'Install components: %s\n' "$selected"
    [ "$COMPONENT_WEB" = 1 ] || printf 'Preserve existing web service and dashboard config\n'
    [ "$COMPONENT_MEMORY" = 1 ] || printf 'Preserve existing memory service and data\n'
    [ "$COMPONENT_NUVOTON" = 1 ] || printf 'Preserve existing Nuvoton configuration\n'
}
