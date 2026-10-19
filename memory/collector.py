#!/usr/bin/env python3
"""Separate SMU patch/temperature service. No third-party Python dependencies."""
import argparse
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import signal
import threading
import time

from bc250_smu import Bc250Smu, SmuError
from patcher import (HANDLER_REG, MODES, PACKED_PAYLOADS, check_platform, ensure_patch,
                     identify_installed, load_payload)

RUNTIME_DIR = Path('/run/bc250-memory')
# The in-kernel bc250_memory driver (linux-cachyos-bc250, bc250-memory-dkms)
# sends the same SMU queue 3 messages. Running both wedges the SMU, so the
# kernel driver always wins and this collector stands down.
KERNEL_DRIVER = Path('/sys/module/bc250_memory')
STOP = threading.Event()

# Migration of one known earlier validation bug. That exception was raised
# after completed SRAM reads, strictly before unlock or payload writes. Do not
# apply this exception to timeouts, interrupted runs or any other failure.
EMPTY_HANDLER_REJECTION = {
    'state': 'failed',
    'error': 'unexpected original Q3/5 handler: 0x00000000',
}
# Reader-only mode never unlocks or writes; it raises this after completed
# reads when the installed payload is unknown (e.g. a newer BIOS build than
# the collector knew). Such a guard leaves SMU exactly as firmware set it up.
READER_ONLY_REJECTION = 'no known temperature payload installed (Q3/5 handler 0x'


def retryable_rejection(previous):
    """True for a failed guard known to precede any SMU write."""
    if previous == EMPTY_HANDLER_REJECTION:
        return True
    return (isinstance(previous, dict) and set(previous) == {'state', 'error'}
            and previous['state'] == 'failed' and isinstance(previous['error'], str)
            and previous['error'].startswith(READER_ONLY_REJECTION))


def atomic_json(path, data):
    path = Path(path)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as output:
        json.dump(data, output, allow_nan=False)
        output.write('\n')
    temporary.chmod(0o644)
    os.replace(temporary, path)


def publish_snapshot(path, data):
    """Atomic, versioned input for the C++ daemon's combined JSON writer."""
    words = ' '.join(str(value) for value in data['raw']) if data['valid'] else ''
    error = str(data.get('error') or '').replace('\n', ' ').replace('\r', ' ')[:512]
    contents = f"BC250_MEMORY_V1\n{data['sampled_boottime_s']:.9f}\n{data['status']}\n{words}\n{error}\n"
    temporary = path.with_suffix('.tmp')
    temporary.write_text(contents)
    temporary.chmod(0o644)
    os.replace(temporary, path)


def unavailable(status, error=None):
    return {'valid': False, 'status': status, 'error': error,
            'chips_c': [None] * 8, 'average_c': None, 'hotspot_c': None,
            'hotspot_chip': None, 'saturated': False, 'saturated_chips': [],
            'raw': [], 'sampled_boottime_s': time.clock_gettime(time.CLOCK_BOOTTIME)}


# Bounded-wait payloads report firmware polling timeouts in place of MR3:
# full words for Q3/5, bytes for Q3/6. Stage 1 waits for the MRR request to
# be accepted, stage 2 for the MR3 value to arrive.
SINGLE_TIMEOUTS = {0xFFFFFFFF: 1, 0xFFFFFFFE: 2}
PACKED_TIMEOUTS = {0xFF: 1, 0xFE: 2}


def read_raw(smu, packed=False):
    """Eight raw values: MR3 words (Q3/5) or MR3 low bytes (Q3/6 packed)."""
    if not packed:
        return [smu.read_chip(chip) for chip in range(8)]
    raw = [0] * 8
    for parity in (0, 1):
        word = smu.read_packed(parity)
        for n in range(4):
            raw[parity + 2 * n] = (word >> (8 * n)) & 0xFF
    return raw


def sample(smu, packed=False):
    started = time.clock_gettime(time.CLOCK_BOOTTIME)
    raw = read_raw(smu, packed)
    sentinels = PACKED_TIMEOUTS if packed else SINGLE_TIMEOUTS
    timeouts = [f'chip {chip} stage {sentinels[value]}' for chip, value in enumerate(raw) if value in sentinels]
    codes = [value & 0xFF for value in raw]
    if timeouts or any(value > 80 for value in codes):
        error = ('firmware MR3 read timeout: ' + ', '.join(timeouts) if timeouts
                 else 'temperature code outside JEDEC range 0..80')
        result = unavailable('invalid_reading', error)
        result['raw'] = raw
        result['sampled_boottime_s'] = started
        return result
    chips = [value * 2 - 40 for value in codes]
    saturated = [chip for chip, code in enumerate(codes) if code == 80]
    return {'valid': True, 'status': 'ok', 'error': None, 'chips_c': chips,
            'average_c': sum(chips) / 8, 'hotspot_c': max(chips),
            'hotspot_chip': chips.index(max(chips)), 'raw': raw,
            'saturated': bool(saturated), 'saturated_chips': saturated,
            'sampled_boottime_s': started}


def kernel_driver_loaded():
    return KERNEL_DRIVER.exists()


def run(runtime=RUNTIME_DIR, interval=3.0, mode='auto'):
    if kernel_driver_loaded():
        logging.info('bc250_memory kernel driver is loaded and owns SMU memory reads; '
                     'apu-telemetry reads it via hwmon. Not starting the collector.')
        return 0
    runtime.mkdir(parents=True, exist_ok=True, mode=0o755)
    # Keep the inode and guard across restarts, but /run clears them at boot.
    # This coordinates our service only; unrelated SMU tools must be stopped.
    with (runtime / 'smu.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logging.error('another memory collector owns the SMU lock')
            return 1
        output = runtime / 'telemetry'
        guard = runtime / 'patch-state.json'
        smu = None
        hardware_started = False
        try:
            publish_snapshot(output, unavailable('starting'))
            payload = load_payload()  # Must precede any SMU access.
            check_platform(strict_bios=mode != 'reader-only')
            previous = None
            if guard.exists():
                previous = json.loads(guard.read_text())
                if retryable_rejection(previous):
                    logging.info('Retrying the earlier rejection (%s); that attempt wrote nothing to SMU',
                                 previous['error'])
                    previous = None
                elif not isinstance(previous, dict) or previous.get('state') != 'ready':
                    raise SmuError('previous SMU operation did not finish; reboot before retrying')
            # Opening the descriptor does not issue commands. An access-denied
            # error here should not poison the rest of the boot.
            smu = Bc250Smu()
            atomic_json(guard, {'state': 'preparing'})
            hardware_started = True
            state = ensure_patch(smu, payload, allow_install=previous is None, mode=mode)
            # Remember that firmware, not this host, installed the payload, so
            # the installer's --probe can tell the two apart later this boot.
            extra = {}
            if (previous is None and state.startswith('already_patched')) or \
                    (isinstance(previous, dict) and previous.get('source') == 'firmware'):
                extra = {'source': 'firmware'}
            atomic_json(guard, {'state': 'ready', **extra})
            described = {'patched': 'SMU payload installed',
                         'already_patched_modern': 'SMU payload already installed',
                         'already_patched_original': 'original SMU payload already installed (BIOS/EFI)',
                         'already_patched_bounded': 'bounded SMU payload already installed (BIOS/EFI)'}
            packed = state == 'patched' or state.removeprefix('already_patched_') in PACKED_PAYLOADS
            logging.info('%s; polling eight chips every %.1f seconds (%s reads)',
                         described.get(state, f'SMU {state}'), interval,
                         'packed Q3/6' if packed else 'single Q3/5')
            while not STOP.is_set():
                if kernel_driver_loaded():
                    logging.warning('bc250_memory kernel driver was loaded; stopping the collector '
                                    'to avoid SMU queue 3 collisions')
                    break
                # Mark each transaction too: a killed reader must not be
                # restarted onto a potentially still-running firmware request.
                atomic_json(guard, {'state': 'reading', **extra})
                result = sample(smu, packed)
                atomic_json(guard, {'state': 'ready', **extra})
                publish_snapshot(output, result)
                STOP.wait(interval)
            publish_snapshot(output, unavailable('stopped'))
            logging.info('Stopped polling; the SMU payload stays installed until reboot')
            return 0
        except Exception as error:
            logging.exception('memory telemetry stopped')
            if hardware_started:
                atomic_json(guard, {'state': 'failed', 'error': str(error)})
            publish_snapshot(output, unavailable('error', str(error)))
            return 1
        finally:
            if smu is not None:
                smu.close()


PROBE_EXIT = {'firmware-original': 0, 'firmware-bounded': 0, 'firmware-modern': 0, 'none': 3, 'unknown': 4, 'linux': 5}


def probe(runtime=RUNTIME_DIR):
    """Read-only: report which temperature payload is installed, and by whom.

    Never unlocks and never writes SMU SRAM. Labels: firmware-original,
    firmware-bounded, firmware-modern, none (empty slot), unknown (foreign handler or modified
    bytes), linux (this host already used the SMU this boot, so the payload's
    origin cannot be told). Raises SmuError/OSError when the SMU is unusable.
    """
    if kernel_driver_loaded():
        raise SmuError('bc250_memory kernel driver owns SMU memory reads')
    runtime.mkdir(parents=True, exist_ok=True, mode=0o755)
    with (runtime / 'smu.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SmuError('another memory collector owns the SMU lock')
        guard = runtime / 'patch-state.json'
        previous = json.loads(guard.read_text()) if guard.exists() else None
        if retryable_rejection(previous):
            previous = None
        if previous is not None and (not isinstance(previous, dict) or previous.get('state') != 'ready'):
            raise SmuError('previous SMU operation did not finish; reboot before retrying')
        origin = None if previous is None else previous.get('source')
        smu = Bc250Smu()
        try:
            atomic_json(guard, {'state': 'reading', **({'source': origin} if origin else {})})
            if not smu.alive():
                raise SmuError('SMU failed its initial probe')
            current = int.from_bytes(smu.smu_read(HANDLER_REG), 'little')
            try:
                known = identify_installed(smu, current)
            except SmuError:
                known = None
            if known:
                label = f'firmware-{known}'
            else:
                label = 'none' if current == 0 else 'unknown'
            if known and previous is not None and origin != 'firmware':
                label = 'linux'
            elif known and previous is None:
                origin = 'firmware'
            if previous is None and not known:
                # Nothing was written, and an empty slot must stay installable
                # by the service later this boot: do not leave a guard behind.
                guard.unlink()
            else:
                atomic_json(guard, {'state': 'ready', **({'source': origin} if origin else {})})
            return label
        except BaseException as error:
            atomic_json(guard, {'state': 'failed', 'error': str(error)})
            raise
        finally:
            smu.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interval', type=float, default=3.0)
    parser.add_argument('--mode', choices=MODES, default=os.environ.get('BC250_MEMORY_MODE', 'auto'),
                        help='reader-only: never unlock/upload; use a payload installed by BIOS/EFI')
    parser.add_argument('--probe', action='store_true',
                        help='read-only: print which payload is installed (firmware-*/none/unknown/linux)')
    parser.add_argument('--check', action='store_true', help='validate platform/payload without accessing SMU')
    args = parser.parse_args()
    if not math.isfinite(args.interval) or not 1 <= args.interval <= 5:
        parser.error('--interval must be between 1 and 5 seconds')
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    if args.check:
        try:
            load_payload()
            check_platform(strict_bios=args.mode != 'reader-only')
        except (SmuError, OSError) as error:
            logging.error('Preflight check failed: %s', error)
            return 1
        print('Platform and bundled payload checks passed (SMU was not accessed).')
        return 0
    if os.geteuid() != 0:
        parser.error('SMU access requires root')
    if args.probe:
        try:
            label = probe()
        except (SmuError, OSError, ValueError) as error:
            logging.error('Probe failed: %s', error)
            return 1
        print(label)
        return PROBE_EXIT[label]
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: STOP.set())
    return run(interval=args.interval, mode=args.mode)


if __name__ == '__main__':
    raise SystemExit(main())
