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
from patcher import check_platform, ensure_patch, load_payload

RUNTIME_DIR = Path('/run/bc250-memory')
STOP = threading.Event()

# Migration of one known earlier validation bug. That exception was raised
# after completed SRAM reads, strictly before unlock or payload writes. Do not
# apply this exception to timeouts, interrupted runs or any other failure.
EMPTY_HANDLER_REJECTION = {
    'state': 'failed',
    'error': 'unexpected original Q3/5 handler: 0x00000000',
}


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


def sample(smu):
    started = time.clock_gettime(time.CLOCK_BOOTTIME)
    raw = [smu.read_chip(chip) for chip in range(8)]
    codes = [value & 0xFF for value in raw]
    if any(value > 80 for value in codes):
        result = unavailable('invalid_reading', 'temperature code outside JEDEC range 0..80')
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


def run(runtime=RUNTIME_DIR, interval=3.0):
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
            check_platform()
            previous = None
            retry_empty_slot = False
            if guard.exists():
                previous = json.loads(guard.read_text())
                retry_empty_slot = previous == EMPTY_HANDLER_REJECTION
                if not retry_empty_slot and (not isinstance(previous, dict) or previous.get('state') != 'ready'):
                    raise SmuError('previous SMU operation did not finish; reboot before retrying')
                if retry_empty_slot:
                    logging.info('Retrying the earlier empty-handler rejection; no patch was written by that attempt')
            # Opening the descriptor does not issue commands. An access-denied
            # error here should not poison the rest of the boot.
            smu = Bc250Smu()
            atomic_json(guard, {'state': 'preparing'})
            hardware_started = True
            state = ensure_patch(smu, payload, allow_install=previous is None or retry_empty_slot)
            atomic_json(guard, {'state': 'ready'})
            logging.info('SMU %s; polling eight chips every %.1f seconds', state, interval)
            while not STOP.is_set():
                # Mark each transaction too: a killed reader must not be
                # restarted onto a potentially still-running firmware request.
                atomic_json(guard, {'state': 'reading'})
                result = sample(smu)
                atomic_json(guard, {'state': 'ready'})
                publish_snapshot(output, result)
                STOP.wait(interval)
            publish_snapshot(output, unavailable('stopped'))
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interval', type=float, default=3.0)
    parser.add_argument('--check', action='store_true', help='validate platform/payload without accessing SMU')
    args = parser.parse_args()
    if not math.isfinite(args.interval) or not 1 <= args.interval <= 5:
        parser.error('--interval must be between 1 and 5 seconds')
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    if args.check:
        try:
            load_payload()
            check_platform()
        except (SmuError, OSError) as error:
            logging.error('Preflight check failed: %s', error)
            return 1
        print('Platform and bundled payload checks passed (SMU was not accessed).')
        return 0
    if os.geteuid() != 0:
        parser.error('SMU access requires root')
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: STOP.set())
    return run(interval=args.interval)


if __name__ == '__main__':
    raise SystemExit(main())
