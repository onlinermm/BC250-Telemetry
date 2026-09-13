#!/bin/bash
set -euo pipefail

# Colors for text
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

source ./install-lib.sh

# --dashboard=v1|v2 — for scripted installs (curl | bash, CI), where there's no
# TTY for the interactive prompt. Any other flag is warned about but doesn't
# fail the install, so install.sh doesn't break on something unknown in the
# future (or on a leftover flag from an older version, like the removed
# --fan-control).
parse_install_args "$@"

echo -e "${BLUE}=== Installing BC-250 Telemetry ===${NC}"
echo -e "Hey, this script will set up monitoring for your board."
echo -e "We'll be using 'sudo' for system files, so the system might ask for your password.\n"

echo -e "${YELLOW}[STEP 1/7]${NC} Choosing memory temperature monitoring..."
MEMORY_EXISTING=0
if systemctl is-enabled --quiet bc250-memory.service 2>/dev/null; then
    MEMORY_EXISTING=1
fi
choose_memory_monitoring "$MEMORY_EXISTING"
if [ "$MEMORY_ENABLED" = 1 ]; then
    echo "  -> Memory monitoring enabled; checking board and payload compatibility"
    sudo python3 -B "$SCRIPT_DIR/memory/collector.py" --check
else
    echo "  -> Memory monitoring disabled"
fi

echo -e "${YELLOW}[STEP 2/7]${NC} Setting up the Nuvoton sensor module (fans)..."

# Only touch the sensor driver if the user has none loaded at all — an
# already-active nct6683/nct6687 (from a previous run, or the distro itself)
# is left exactly as is.
ACTIVE_DRIVER="$(detect_active_sensor_driver)"
if [ -n "$ACTIVE_DRIVER" ]; then
    echo -e "  -> Found an already-loaded driver ($ACTIVE_DRIVER) — leaving the existing setup as is."
    echo -e "${GREEN}✓ Skipped${NC}\n"
elif modinfo nct6683 >/dev/null 2>&1; then
    echo -e "  -> No sensor driver loaded yet, installing nct6683 (monitoring)"
    if load_sensor_driver; then
        write_sensor_driver_configs
        echo -e "${GREEN}✓ nct6683 loaded!${NC}\n"
    else
        echo -e "${YELLOW}⚠ Failed to load nct6683 — fan monitoring will be unavailable.${NC}\n"
    fi
else
    echo -e "${YELLOW}⚠ Module nct6683 not found in the current kernel ($(uname -r)).${NC}"
    echo -e "  Skipping — fan monitoring (nct_*) will be unavailable,"
    echo -e "  the rest of the telemetry (VRM, CPU, GPU, NVMe) will work as usual.\n"
fi

# Source code, if present next to the script, always takes priority over any
# prebuilt binary: otherwise, after editing the .cpp file, install.sh could
# silently keep installing the old binary. Compiling only gives up — falling
# back to a prebuilt ./apu_telemetry, if there is one next to it — when the
# toolchain is missing or the build itself fails; the fallback is always
# announced, never silent.
echo -e "${YELLOW}[STEP 3/7]${NC} Preparing the apu_telemetry binary..."
BUILT=0
if [ -f ./bc250_telemetry.cpp ]; then
    if ! command -v g++ >/dev/null 2>&1; then
        echo -e "${YELLOW}⚠ g++ not found on the system (SteamOS/Bazzite don't have a toolchain out of the box) — can't compile bc250_telemetry.cpp.${NC}"
    else
        echo -e "  -> Found bc250_telemetry.cpp, compiling..."
        echo -e "  -> Trying a static build (-O2 -static), so the binary doesn't depend on system libraries"
        BUILD_LOG=$(mktemp)
        if g++ -std=c++17 -Wall -Wextra -O2 -static -o ./apu_telemetry bc250_telemetry.cpp 2>"$BUILD_LOG"; then
            echo -e "${GREEN}✓ Compiled statically!${NC}"
            BUILT=1
        else
            echo -e "${YELLOW}⚠ Static build failed (no static libc/libstdc++?), trying dynamic...${NC}"
            if g++ -std=c++17 -Wall -Wextra -O2 -o ./apu_telemetry bc250_telemetry.cpp 2>"$BUILD_LOG"; then
                echo -e "${GREEN}✓ Compiled (dynamically)!${NC}"
                BUILT=1
            else
                echo -e "${YELLOW}⚠ Compilation failed:${NC}"
                cat "$BUILD_LOG"
            fi
        fi
        rm -f "$BUILD_LOG"
    fi
fi

if [ "$BUILT" = "1" ]; then
    if ! verify_binary ./apu_telemetry; then
        echo -e "${RED}✗ The freshly compiled apu_telemetry doesn't look like a valid executable.${NC}"
        exit 1
    fi
    echo -e "${GREEN}✓ Binary ready!${NC}\n"
elif [ -f ./apu_telemetry ]; then
    if [ -f ./bc250_telemetry.cpp ]; then
        echo -e "  -> Falling back to the prebuilt ./apu_telemetry next to the script."
    else
        echo -e "  -> No bc250_telemetry.cpp found, using the existing ./apu_telemetry as-is"
    fi
    chmod +x ./apu_telemetry
    if ! verify_binary ./apu_telemetry; then
        echo -e "${RED}✗ ./apu_telemetry doesn't look like a valid executable (corrupted download? wrong architecture?).${NC}"
        exit 1
    fi
    echo -e "${GREEN}✓ Using the prebuilt binary!${NC}\n"
else
    echo -e "${RED}✗ Neither bc250_telemetry.cpp nor a prebuilt apu_telemetry found next to the script, and compilation wasn't possible.${NC}"
    exit 1
fi

if [ "$MEMORY_ENABLED" = 1 ] && ! supports_memory_snapshot ./apu_telemetry; then
    echo "The selected binary lacks memory snapshot support. Build the current sources or use the updated release."
    exit 1
fi

echo -e "${YELLOW}[STEP 4/7]${NC} Installing the daemon into the system..."
DISTRO_ID=""
if [ -f /etc/os-release ]; then
    DISTRO_ID=$(. /etc/os-release 2>/dev/null && echo "$ID") || DISTRO_ID=""
fi

if [ "$DISTRO_ID" = "steamos" ]; then
    # /usr/local sits in SteamOS's read-only A/B slot and gets fully
    # overwritten by the next OS update regardless of permissions — so we
    # install the binary in /opt instead, which is bind-mounted onto the
    # persistent /home partition (see CLAUDE.md, "Distro-specific pitfalls").
    BIN_DIR="/opt/bc250-telemetry"
    echo -e "  -> Detected SteamOS: ${YELLOW}/usr/local${NC} doesn't survive OS updates,"
    echo -e "     installing the daemon into $BIN_DIR (persistent partition)"
else
    BIN_DIR="/usr/local/bin"
fi
BIN_PATH="$BIN_DIR/apu_telemetry"

echo -e "  -> Copying apu_telemetry into $BIN_DIR/"
sudo mkdir -p "$BIN_DIR"
# If the service is already active, its binary is running right now — a plain
# `cp` to the same path would fail with ETXTBSY ("Text file busy"). We copy
# under a temporary name and atomically swap it in via mv (rename, which
# doesn't touch the inode of the already-running process) — this works the
# same on a fresh install and when updating an already-running service.
sudo cp apu_telemetry "$BIN_PATH.new"
sudo mv -f "$BIN_PATH.new" "$BIN_PATH"
if command -v restorecon >/dev/null 2>&1; then
    echo -e "  -> Detected SELinux (Bazzite) — fixing the binary's context"
    if ! sudo restorecon -v "$BIN_PATH"; then
        echo -e "${YELLOW}⚠ restorecon failed — if SELinux is enforcing, the service may fail to start (check 'journalctl -u apu-telemetry').${NC}"
    fi
fi
echo -e "  -> Copying the apu-telemetry.service unit config"
sed "s|TELEMETRY_BIN_PATH|$BIN_PATH|g" apu-telemetry.service | sudo tee /etc/systemd/system/apu-telemetry.service > /dev/null
echo -e "${GREEN}✓ Done!${NC}\n"

echo -e "${YELLOW}[STEP 5/7]${NC} Choosing the default dashboard..."
# Priority order: --dashboard=... > env BC250_DEFAULT_DASHBOARD > whatever was
# already set in a previously installed unit (so that running `./install.sh`
# again for an update doesn't silently reset the user's choice back to v1) >
# interactive prompt > v1.
EXISTING_DASHBOARD=""
if [ -f /etc/systemd/system/bc250-web.service ]; then
    EXISTING_LINE=$(grep '^Environment=BC250_DEFAULT_DASHBOARD=' /etc/systemd/system/bc250-web.service 2>/dev/null || true)
    # Strip "Environment=" and "BC250_DEFAULT_DASHBOARD=" one at a time —
    # without grep -P/-o, so we don't depend on a PCRE-enabled grep build.
    EXISTING_DASHBOARD="${EXISTING_LINE#*=}"
    EXISTING_DASHBOARD="${EXISTING_DASHBOARD#*=}"
fi
FALLBACK_DASHBOARD="${EXISTING_DASHBOARD:-v1}"

if [ -n "$DASHBOARD_FLAG" ]; then
    CHOSEN_DASHBOARD="$DASHBOARD_FLAG"
    echo -e "  -> Flag --dashboard=$CHOSEN_DASHBOARD"
elif [ -n "${BC250_DEFAULT_DASHBOARD:-}" ]; then
    CHOSEN_DASHBOARD="$BC250_DEFAULT_DASHBOARD"
    echo -e "  -> Environment variable BC250_DEFAULT_DASHBOARD=$CHOSEN_DASHBOARD"
elif [ -t 0 ]; then
    echo -e "  1) v1 — classic HUD"
    echo -e "  2) v2 — animated board diagram"
    # -t 30: if stdin is technically a terminal but no answer will ever come
    # (e.g. an automated run under a pty) — don't hang forever, just quietly
    # fall back to the current/default value, same as for a non-TTY.
    read -r -t 30 -p "  Which one to show on http://<ip>:8090/? [1/2, Enter = current: $FALLBACK_DASHBOARD]: " ANSWER || ANSWER=""
    case "$ANSWER" in
        1) CHOSEN_DASHBOARD="v1" ;;
        2) CHOSEN_DASHBOARD="v2" ;;
        "") CHOSEN_DASHBOARD="$FALLBACK_DASHBOARD" ;;
        *) CHOSEN_DASHBOARD="$ANSWER" ;;
    esac
else
    CHOSEN_DASHBOARD="$FALLBACK_DASHBOARD"
fi

# We accept exactly "v1"/"v2" — anything else (a typo, a corrupted
# environment variable) has no right to stop the install, it should just fall
# back to a safe default.
if [ "$CHOSEN_DASHBOARD" != "v1" ] && [ "$CHOSEN_DASHBOARD" != "v2" ]; then
    echo -e "${YELLOW}⚠ Unrecognized dashboard value \"$CHOSEN_DASHBOARD\", using v1.${NC}"
    CHOSEN_DASHBOARD="v1"
fi
echo -e "${GREEN}✓ The default on \"/\" will be: $CHOSEN_DASHBOARD${NC}\n"

echo -e "${YELLOW}[STEP 6/7]${NC} Installing the web server..."
echo -e "  -> Copying and configuring the bc250-web.service unit"
CURRENT_USER="${SUDO_USER:-$(whoami)}"
if [ -z "${SUDO_USER:-}" ]; then
    echo -e "${YELLOW}⚠ \$SUDO_USER is not set (running as root directly instead of via 'sudo'?) — bc250-web.service will run as \"$CURRENT_USER\".${NC}"
fi
sed -e "s|TELEMETRY_USER|$CURRENT_USER|g" -e "s|TELEMETRY_WEB_DIR|$SCRIPT_DIR/web|g" \
    -e "s|TELEMETRY_DEFAULT_DASHBOARD|$CHOSEN_DASHBOARD|g" \
    web/bc250-web.service | sudo tee /etc/systemd/system/bc250-web.service > /dev/null
echo -e "${GREEN}✓ Done!${NC}\n"

# enable by itself doesn't restart an already-active service (it only enables
# autostart) — so for a freshly built binary/unit we explicitly restart it,
# otherwise the old service would keep running with the old code in memory.
echo -e "${YELLOW}[STEP 7/7]${NC} Starting the services..."
if [ "$MEMORY_ENABLED" = "1" ]; then
    echo "  -> Installing the separate SMU patch and memory collector service"
    # Stop before replacing Python modules. The per-boot operation guard lives
    # in /run and is deliberately preserved across updates and reinstalls.
    if [ -f /etc/systemd/system/bc250-memory.service ]; then
        sudo systemctl stop bc250-memory.service
    fi
    sudo install -d -m 0755 /opt/bc250-memory/bc250_smu /opt/bc250-memory/payload
    sudo install -m 0644 memory/*.py memory/LICENSE.upstream memory/UPSTREAM.md memory/README.md /opt/bc250-memory/
    sudo install -m 0644 memory/bc250_smu/*.py /opt/bc250-memory/bc250_smu/
    sudo install -m 0644 memory/payload/SMUPayload.bin /opt/bc250-memory/payload/
    sudo install -m 0644 memory/bc250-memory.service /etc/systemd/system/bc250-memory.service
    if command -v restorecon >/dev/null 2>&1; then
        sudo restorecon -R /opt/bc250-memory || true
    fi
elif [ -f /etc/systemd/system/bc250-memory.service ]; then
    echo "  -> Disabling and removing the previously installed memory collector"
    sudo systemctl disable --now bc250-memory.service
    sudo rm -f /etc/systemd/system/bc250-memory.service
    # Opting out removes the on-disk SMU-patching code, not just the unit.
    sudo rm -rf /opt/bc250-memory
    sudo rm -f /run/bc250-memory/telemetry
    # The per-boot operation guard is deliberately preserved (reboot clears
    # it) so a same-boot reinstall cannot retry an interrupted SMU operation.
fi
echo -e "  -> Reloading systemd, enabling autostart, and (re)starting the services"
sudo systemctl daemon-reload
if ! sudo systemctl enable apu-telemetry.service bc250-web.service; then
    echo -e "${YELLOW}⚠ Failed to enable one or both services for autostart — they may not survive a reboot. Continuing anyway.${NC}"
fi
sudo systemctl restart apu-telemetry.service
sudo systemctl restart bc250-web.service
if [ "$MEMORY_ENABLED" = "1" ]; then
    sudo systemctl enable bc250-memory.service
    sudo systemctl restart bc250-memory.service
fi

# `systemctl restart` only confirms systemd accepted the request — it returns
# success even if the process crashes right after (verified: a unit whose
# ExecStart exits immediately still gets exit code 0 from `restart`). Give it
# a moment, then check the actual state before declaring victory.
sleep 2
SERVICES_OK=1
if ! systemctl is-active --quiet apu-telemetry.service; then
    echo -e "${RED}✗ apu-telemetry.service did not stay running:${NC}"
    systemctl status apu-telemetry.service --no-pager -l | tail -n 8
    SERVICES_OK=0
fi
if ! systemctl is-active --quiet bc250-web.service; then
    echo -e "${RED}✗ bc250-web.service did not stay running:${NC}"
    systemctl status bc250-web.service --no-pager -l | tail -n 8
    SERVICES_OK=0
fi
if [ "$MEMORY_ENABLED" = "1" ] && ! systemctl is-active --quiet bc250-memory.service; then
    echo -e "${RED}✗ bc250-memory.service did not stay running; check journalctl -u bc250-memory.${NC}"
    sudo journalctl -u bc250-memory.service -b -n 30 --no-pager || true
    SERVICES_OK=0
fi
if [ "$MEMORY_ENABLED" = "1" ] && systemctl is-active --quiet bc250-memory.service; then
    echo "Memory service started; patching/first sample may still be in progress."
    echo "Check /api/telemetry -> memory.status and journalctl -u bc250-memory."
fi

if [ "$SERVICES_OK" = "1" ]; then
    echo -e "${GREEN}✓ Services started!${NC}\n"
else
    echo -e "${YELLOW}⚠ One or more services aren't staying up — see above. Check 'journalctl -u <service>' for details.${NC}\n"
fi

if [ "$SERVICES_OK" = "1" ]; then
    echo -e "${BLUE}=== Installation complete! ===${NC}"
else
    echo -e "${RED}=== Installation finished, but with problems ===${NC}"
fi
echo -e "Your monitoring is now running. Data lives in /run/apu_telemetry.json."
echo -e "Memory is included in the same JSON file; /api/telemetry serves that snapshot."
echo -e "PMBus/VRM (+ GDDR6, if enabled) sensors for CoolerControl/MangoHud: /run/bc250/ (see mangohud/MangoHud-bc250.conf)"
if [ "$CHOSEN_DASHBOARD" = "v2" ]; then
    echo -e "Web UI (default v2 — animated board diagram): http://localhost:8090"
    echo -e "The classic HUD (v1) is still available at http://localhost:8090/index.html"
else
    echo -e "Web UI: http://localhost:8090"
    echo -e "The animated board diagram (v2) is available at http://localhost:8090/v2/"
fi
echo -e "To change the choice later: ${YELLOW}sudo ./install.sh --dashboard=v1${NC} (or v2)"
echo -e "To check the status, you can use:"
echo -e "  systemctl status apu-telemetry"
echo -e "  systemctl status bc250-web.service"
echo -e "\nTo remove everything, just run ${YELLOW}./uninstall.sh${NC}"

if [ "$SERVICES_OK" != "1" ]; then
    exit 1
fi
