"""CPU/GPU core-count detection for the BC-250.

Split out of the telemetry daemon on purpose: unlike temperature/clock/power,
this is topology info that only changes when someone actively unlocks more
cores/CUs (a reboot for CPU cores, or a live WGP toggle for GPU CUs) — it
doesn't need the daemon's 700ms polling cadence, and living here means
checking it doesn't require rebuilding and reinstalling the C++ binary.
"""

import ctypes
import os
import re
import subprocess

# AMDGPU_INFO_DEV_INFO — same query bc250-toolkit and bc250-cu-live-manager
# use (via the same libdrm_amdgpu.so.1 call), fills a struct
# drm_amdgpu_info_device. cu_active_number sits at byte offset 48 in that
# struct — cross-checked against bc250-cu-live-manager, which reads the
# cu_bitmap array starting right after it, at offset 56.
_AMDGPU_INFO_DEV_INFO = 0x16
_CU_ACTIVE_NUMBER_OFFSET = 48
_QUERY_BUF_SIZE = 256

# bc250-cu-live-manager toggles CU dispatch by writing SPI/CC/RLC registers
# live, via umr, *after* the amdgpu driver has already initialized and
# cached cu_active_number — so it never sees that live change. Its own
# status view doesn't trust the ioctl either; it re-reads the live SPI
# register through umr. umr flatly refuses to run without root though, and
# this (unprivileged) web service has no business asking for that — so
# instead this falls back to the boot profile bc250-cu-live-manager itself
# saved, which is world-readable. Best-effort: correct as long as nobody
# live-toggled WGPs since without re-running `write-service-table`.
_CU_LIVE_MANAGER_CONF = '/etc/bc250-cu-live-manager.conf'
_CU_LIVE_MANAGER_SERVICE = 'bc250-cu-live-manager.service'


def _service_active(name):
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', name],
            capture_output=True, text=True, timeout=2,
        )
        return result.stdout.strip() == 'active'
    except (OSError, subprocess.SubprocessError):
        return False


def _read_cu_live_manager_masks():
    """BC250_WGP_MASKS=SE0.SH0,SE0.SH1,SE1.SH0,SE1.SH1 — each a 5-bit WGP
    dispatch mask (bit per WGP, 2 CU per WGP), same encoding
    bc250-cu-live-manager itself uses for its "CUs active & routed" total."""
    try:
        with open(_CU_LIVE_MANAGER_CONF) as f:
            text = f.read()
    except OSError:
        return None
    match = re.search(r'^BC250_WGP_MASKS=(.+)$', text, re.MULTILINE)
    if not match:
        return None
    try:
        masks = [int(v.strip(), 0) for v in match.group(1).split(',')]
    except ValueError:
        return None
    if not masks:
        return None
    return sum(bin(mask & 0x1f).count('1') * 2 for mask in masks)


def read_cpu_physical_cores():
    """Physical core count, straight from cpuinfo's own "cpu cores" field —
    not threads/2, since a core disabled by binning isn't guaranteed to
    leave exactly half the threads intact."""
    try:
        with open('/proc/cpuinfo') as f:
            for line in f:
                if line.startswith('cpu cores'):
                    return int(line.split(':', 1)[1].strip())
    except (OSError, ValueError):
        pass
    return None


def _open_render_node():
    dri = '/dev/dri'
    if not os.path.isdir(dri):
        return -1
    for name in sorted(os.listdir(dri)):
        if name.startswith('renderD'):
            try:
                return os.open(os.path.join(dri, name), os.O_RDWR)
            except OSError:
                continue
    return -1


def _read_driver_cu_active():
    """Active Compute Unit count per the amdgpu kernel driver's own cached
    topology — correct for a boot-time unlock (e.g. a kernel patch clearing
    the harvest mask before CU enumeration runs), but stale for anything
    that changes dispatch registers live, after the driver already cached
    this number."""
    try:
        libdrm = ctypes.CDLL('libdrm_amdgpu.so.1')
    except OSError:
        return None

    fd = _open_render_node()
    if fd < 0:
        return None

    dev = ctypes.c_void_p()
    try:
        major, minor = ctypes.c_uint32(), ctypes.c_uint32()
        rc = libdrm.amdgpu_device_initialize(fd, ctypes.byref(major), ctypes.byref(minor), ctypes.byref(dev))
        if rc != 0:
            return None

        buf = (ctypes.c_uint8 * _QUERY_BUF_SIZE)()
        rc = libdrm.amdgpu_query_info(dev, _AMDGPU_INFO_DEV_INFO, len(buf), ctypes.byref(buf))
        if rc != 0:
            return None

        cu_active = int.from_bytes(bytes(buf[_CU_ACTIVE_NUMBER_OFFSET:_CU_ACTIVE_NUMBER_OFFSET + 4]), 'little')
        return cu_active if cu_active > 0 else None
    finally:
        if dev.value:
            libdrm.amdgpu_device_deinitialize(dev)
        os.close(fd)


def read_gpu_cu_active():
    if _service_active(_CU_LIVE_MANAGER_SERVICE):
        live_manager_count = _read_cu_live_manager_masks()
        if live_manager_count:
            return live_manager_count
    return _read_driver_cu_active()


def read_topology():
    return {
        'cpu_physical_cores': read_cpu_physical_cores(),
        'gpu_cu_active': read_gpu_cu_active(),
    }


if __name__ == '__main__':
    print(read_topology())
