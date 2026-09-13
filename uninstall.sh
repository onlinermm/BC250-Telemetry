#!/bin/bash
set -euo pipefail

# Colors for text
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${RED}=== Uninstalling BC-250 Telemetry ===${NC}"
echo -e "We'll now safely stop and remove all files added during installation.\n"

# install.sh installs the binary into /opt/bc250-telemetry on SteamOS and into
# /usr/local/bin elsewhere (see CLAUDE.md) — we take the real path from the
# already-installed unit rather than guessing, while the unit file still exists.
BIN_PATH=$(sed -n 's/^ExecStart=//p' /etc/systemd/system/apu-telemetry.service 2>/dev/null || true)
if [ -z "$BIN_PATH" ]; then
    BIN_PATH="/usr/local/bin/apu_telemetry"
fi

echo -e "${YELLOW}[STEP 1/4]${NC} Stopping and disabling services..."
sudo systemctl disable --now apu-telemetry 2>/dev/null || true
sudo systemctl disable --now bc250-web.service 2>/dev/null || true
sudo systemctl disable --now bc250-memory.service 2>/dev/null || true
echo -e "${GREEN}✓ Services stopped!${NC}\n"

echo -e "${YELLOW}[STEP 2/4]${NC} Removing systemd unit files..."
sudo rm -f /etc/systemd/system/apu-telemetry.service
sudo rm -f /etc/systemd/system/bc250-web.service
sudo rm -f /etc/systemd/system/bc250-memory.service
sudo systemctl daemon-reload
sudo systemctl reset-failed 2>/dev/null || true
echo -e "${GREEN}✓ Service configs removed!${NC}\n"

echo -e "${YELLOW}[STEP 3/4]${NC} Removing the binary..."
sudo rm -f "$BIN_PATH"
sudo rmdir "$(dirname "$BIN_PATH")" 2>/dev/null || true # remove /opt/bc250-telemetry if it's empty
sudo rm -f /run/apu_telemetry.json /run/apu_telemetry.tmp
sudo rm -rf /run/bc250
sudo rm -rf /opt/bc250-memory
sudo rm -f /run/bc250-memory/telemetry /run/bc250-memory/telemetry.tmp /run/bc250-memory/smu.lock
# Preserve the operation guard (patch-state.json) until reboot: uninstall must
# not allow a reinstall to retry an interrupted SMU operation in the same boot.
echo -e "${GREEN}✓ Binary removed from the system!${NC}\n"

echo -e "${YELLOW}[STEP 4/4]${NC} Removing Nuvoton configs (optional)..."
echo -e "  -> Removing autoload for the sensor module"
sudo rm -f /etc/modules-load.d/99-sensors.conf /etc/modprobe.d/sensors.conf
# We don't unload the module right now (rmmod) to avoid breaking anything while it's in use, it'll go away after a reboot
echo -e "${GREEN}✓ Module configs removed!${NC}\n"

echo -e "${BLUE}=== Uninstall complete! ===${NC}"
echo -e "Installed files and services have been removed."
echo -e "If memory monitoring was used, reboot to reload firmware and clear its runtime guard."
