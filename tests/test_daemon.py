"""End-to-end tests of apu_telemetry against a synthetic sysfs tree.

The daemon runs one cycle (--once) with --sysfs-root/--run-dir pointing into
a temporary directory, so no real hardware, /run or I2C bus is touched. Any
I2C adapter in a fixture uses a bus number (200+) that has no /dev node.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
DAEMON = None

VRM_CHANNELS = {
    'in0': ('VIN (12V Input)', 12100), 'in1': ('CPU Voltage', 781), 'in2': ('GPU Core Voltage', 698),
    'curr1': ('CPU Current', 20000), 'curr2': ('GPU Current', 5200),
    'temp1': ('CPU VRM Temp', 38000), 'temp2': ('GPU VRM Temp', 40000),
    'power1': ('CPU Power', 1), 'power2': ('GPU Power', 1),
}
CHIPS = [36, 34, 44, 36, 36, 42, 42, 38]


@unittest.skipUnless(shutil.which('g++'), 'g++ is required to build the daemon')
class DaemonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global DAEMON
        build = tempfile.TemporaryDirectory()
        cls.addClassCleanup(build.cleanup)
        DAEMON = Path(build.name) / 'apu_telemetry'
        subprocess.run(['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-O2',
                        str(ROOT / 'bc250_telemetry.cpp'), '-o', str(DAEMON)], check=True)

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.sys = self.root / 'sys'
        self.run_dir = self.root / 'run'
        self.run_dir.mkdir()
        (self.sys / 'class/hwmon').mkdir(parents=True)
        (self.sys / 'bus/i2c/devices').mkdir(parents=True)
        self.config = self.root / 'telemetry.conf'
        self.snapshot = self.root / 'memory-snapshot'
        self.hwmon_count = 0

    # ---------------------------------------------------------- fixtures --
    def hwmon(self, name, attrs):
        path = self.sys / 'class/hwmon' / f'hwmon{self.hwmon_count}'
        self.hwmon_count += 1
        path.mkdir()
        (path / 'name').write_text(name + '\n')
        for attr, value in attrs.items():
            (path / attr).write_text(f'{value}\n')
        return path

    def vrm(self, channels=VRM_CHANNELS, extra=None):
        attrs = {}
        for channel, (label, value) in channels.items():
            attrs[f'{channel}_label'] = label
            attrs[f'{channel}_input'] = value
        attrs.update(extra or {})
        return self.hwmon('bc250_vrm', attrs)

    def memory(self, chips=CHIPS):
        attrs = {'temp1_label': 'VRAM Hotspot', 'temp1_input': max(chips) * 1000,
                 'temp2_label': 'VRAM Average', 'temp2_input': sum(chips) * 125}
        for i, chip in enumerate(chips):
            attrs[f'temp{i + 3}_label'] = f'VRAM Chip {i}'
            attrs[f'temp{i + 3}_input'] = chip * 1000
        return self.hwmon('bc250_memory', attrs)

    def adapter(self, bus, name, driver=None):
        devices = self.sys / 'bus/i2c/devices'
        (devices / f'i2c-{bus}').mkdir()
        (devices / f'i2c-{bus}' / 'name').write_text(name + '\n')
        if driver:
            client = devices / f'{bus}-0060'
            client.mkdir()
            target = self.sys / 'bus/i2c/drivers' / driver
            target.mkdir(parents=True, exist_ok=True)
            os.symlink(target, client / 'driver')

    def collector_snapshot(self, status='ok', words='9509 11308 10537 10537 11051 10794 10794 10537'):
        now = time.clock_gettime(time.CLOCK_BOOTTIME)
        self.snapshot.write_text(f'BC250_MEMORY_V1\n{now:.9f}\n{status}\n{words}\n\n')

    def run_daemon(self, config='', *args):
        self.config.write_text(config)
        result = subprocess.run(
            [str(DAEMON), '--once', f'--sysfs-root={self.sys}', f'--run-dir={self.run_dir}',
             f'--config={self.config}', f'--memory-snapshot={self.snapshot}', *args],
            capture_output=True, text=True, timeout=20, env={'PATH': os.environ.get('PATH', '')})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.log = result.stderr
        return json.loads((self.run_dir / 'apu_telemetry.json').read_text())

    # ------------------------------------------------------------- tests --
    def test_kernel_vrm_and_memory_drivers(self):
        self.vrm(extra={'temp1_max': 100000, 'temp1_crit': 125000, 'curr1_crit': 60000})
        self.memory()
        data = self.run_daemon()
        self.assertEqual(data['sources'], {'vrm': 'hwmon', 'memory': 'hwmon'})
        cpu, gpu = data['hardware']['cpu'], data['hardware']['gpu']
        self.assertTrue(cpu['valid'] and gpu['valid'])
        self.assertAlmostEqual(cpu['vin'], 12.1, places=2)
        self.assertAlmostEqual(cpu['vout'], 0.781, places=3)
        self.assertAlmostEqual(cpu['iout'], 20.0, places=1)
        self.assertAlmostEqual(cpu['pout'], 15.6, places=1)   # VOUT*IOUT, not power1_input
        self.assertEqual(cpu['temp'], 38.0)
        self.assertEqual(gpu['temp'], 40.0)
        self.assertFalse(cpu['temp_warning'] or cpu['temp_fault'] or cpu['iout_fault'])
        self.assertTrue(data['hardware']['total_power_valid'])
        memory = data['memory']
        self.assertTrue(memory['valid'])
        self.assertEqual(memory['chips_c'], CHIPS)
        self.assertEqual(memory['hotspot_c'], 44)
        self.assertEqual(memory['hotspot_chip'], 2)
        self.assertEqual(memory['average_c'], 38.5)
        self.assertEqual((self.run_dir / 'bc250/cpu_vrm_temp').read_text(), '38000\n')
        self.assertEqual((self.run_dir / 'bc250/memory_hotspot_temp').read_text(), '44000\n')

    def test_channels_are_matched_by_label_not_index(self):
        swapped = dict(VRM_CHANNELS)
        swapped['temp1'], swapped['temp2'] = VRM_CHANNELS['temp2'], VRM_CHANNELS['temp1']
        swapped['temp1'] = ('GPU VRM Temp', 55000)
        swapped['temp2'] = ('CPU VRM Temp', 33000)
        self.vrm(swapped)
        data = self.run_daemon()
        self.assertEqual(data['hardware']['cpu']['temp'], 33.0)
        self.assertEqual(data['hardware']['gpu']['temp'], 55.0)

    def test_limits_drive_warning_and_fault_flags(self):
        hot = dict(VRM_CHANNELS, temp1=('CPU VRM Temp', 110000), curr1=('CPU Current', 70000))
        self.vrm(hot, extra={'temp1_max': 100000, 'temp1_crit': 125000, 'curr1_crit': 60000})
        cpu = self.run_daemon()['hardware']['cpu']
        self.assertTrue(cpu['temp_warning'])
        self.assertFalse(cpu['temp_fault'])
        self.assertTrue(cpu['iout_fault'])

    def break_channel(self, path, attr):
        (path / attr).unlink()
        (path / attr).mkdir()   # read() fails, like an EIO from the driver

    def test_failed_current_read_keeps_rail_and_temperature_valid(self):
        # Near 0 A the driver rejects a quarter of the current reads (EIO).
        # That must read as ~0 A, not take the temperature down with it.
        self.break_channel(self.vrm(), 'curr1_input')
        data = self.run_daemon()
        cpu = data['hardware']['cpu']
        self.assertTrue(cpu['valid'])
        self.assertEqual(cpu['iout'], 0.0)
        self.assertEqual(cpu['pout'], 0.0)
        self.assertEqual(cpu['temp'], 38.0)
        self.assertTrue(data['hardware']['total_power_valid'])
        self.assertEqual((self.run_dir / 'bc250/cpu_vrm_temp').read_text(), '38000\n')

    def test_failed_temperature_invalidates_only_its_rail(self):
        self.break_channel(self.vrm(), 'temp1_input')
        data = self.run_daemon()
        self.assertFalse(data['hardware']['cpu']['valid'])
        self.assertTrue(data['hardware']['gpu']['valid'])
        self.assertFalse(data['hardware']['total_power_valid'])
        self.assertFalse((self.run_dir / 'bc250/cpu_vrm_temp').exists())
        self.assertTrue((self.run_dir / 'bc250/gpu_vrm_temp').exists())

    def test_foreign_driver_on_pmic_address_blocks_raw_pmbus(self):
        self.adapter(201, 'SMBus PIIX4 adapter port 0 at 0b00', driver='some_driver')
        data = self.run_daemon()
        self.assertEqual(data['sources']['vrm'], 'none')
        self.assertIn("owned by kernel driver 'some_driver'", self.log)
        self.assertFalse(data['hardware']['cpu']['valid'])

    def test_explicit_pmbus_is_refused_while_kernel_driver_is_bound(self):
        self.vrm()
        self.adapter(202, 'SMBus PIIX4 adapter port 0 at 0b00', driver='bc250_vrm')
        data = self.run_daemon('vrm_source = pmbus\n')
        self.assertEqual(data['sources']['vrm'], 'none')
        self.assertIn('raw PMBus access disabled', self.log)

    def test_only_piix4_adapters_are_probe_candidates(self):
        self.adapter(203, 'AMDGPU DM i2c hw bus 0')
        data = self.run_daemon()
        self.assertEqual(data['sources']['vrm'], 'none')
        self.assertIn('no AMD SMBus (PIIX4) adapter found', self.log)

    def test_missing_dev_node_is_not_a_hardware_probe(self):
        self.adapter(204, 'SMBus PIIX4 adapter port 0 at 0b00')
        self.run_daemon()
        self.assertIn('cannot open /dev/i2c-*', self.log)
        self.assertNotIn('no VRM PMIC answered', self.log)

    def test_vrm_off_and_collector_memory(self):
        self.vrm()
        self.memory()
        self.collector_snapshot()
        data = self.run_daemon('vrm_source = off\nmemory_source = collector\n')
        self.assertEqual(data['sources'], {'vrm': 'off', 'memory': 'collector'})
        self.assertFalse(data['hardware']['cpu']['valid'])
        self.assertTrue(data['memory']['valid'])
        self.assertEqual(data['memory']['raw'][0], 9509)
        self.assertIn('collide on SMU queue 3', self.log)

    def test_auto_memory_falls_back_to_collector(self):
        self.collector_snapshot()
        data = self.run_daemon()
        self.assertEqual(data['sources']['memory'], 'collector')
        self.assertTrue(data['memory']['valid'])
        self.assertNotIn('collide', self.log)

    def test_memory_off_hides_the_panel(self):
        self.memory()
        data = self.run_daemon('memory_source = off\n')
        self.assertEqual(data['memory']['status'], 'unavailable')
        self.assertFalse(data['memory']['valid'])

    def test_out_of_range_memory_reading_is_rejected(self):
        self.memory([36, 34, 470, 36, 36, 42, 42, 38])
        memory = self.run_daemon()['memory']
        self.assertFalse(memory['valid'])
        self.assertEqual(memory['status'], 'invalid_reading')

    def test_bad_config_warns_but_still_runs(self):
        self.vrm()
        data = self.run_daemon('vrm_source = bogus\nfuture_key = 1\npoll_interval_ms = 5\n')
        self.assertEqual(data['sources']['vrm'], 'hwmon')
        self.assertIn("invalid value 'bogus'", self.log)
        self.assertIn("unknown key 'future_key'", self.log)
        self.assertIn('poll_interval_ms=700', self.log)

    def test_check_config(self):
        good = subprocess.run([str(DAEMON), '--check-config', f'--config={ROOT / "config/bc250-telemetry.conf"}'],
                              capture_output=True, text=True)
        self.assertEqual(good.returncode, 0, good.stderr)
        self.config.write_text('memory_source = maybe\n')
        bad = subprocess.run([str(DAEMON), '--check-config', f'--config={self.config}'],
                             capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)

    def test_run_files_off(self):
        self.vrm()
        self.run_daemon('run_files = off\n')
        self.assertEqual(list((self.run_dir / 'bc250').iterdir()), [])

    def wait_for(self, predicate, timeout=8.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data = json.loads((self.run_dir / 'apu_telemetry.json').read_text())
                if predicate(data):
                    return data
            except (OSError, ValueError):
                pass
            time.sleep(0.05)
        self.fail('daemon never reached the expected state')

    def test_hold_last_value_then_invalidate_and_sighup_reload(self):
        path = self.vrm()
        self.config.write_text('poll_interval_ms = 250\n')
        proc = subprocess.Popen(
            [str(DAEMON), f'--sysfs-root={self.sys}', f'--run-dir={self.run_dir}',
             f'--config={self.config}', f'--memory-snapshot={self.snapshot}'],
            stderr=subprocess.PIPE, text=True)
        self.addCleanup(proc.stderr.close)
        self.addCleanup(proc.wait, 5)
        self.addCleanup(proc.terminate)
        self.wait_for(lambda d: d['hardware']['cpu']['valid'])
        # A transient read failure keeps the last good value for a moment...
        (path / 'temp1_input').unlink()
        (path / 'temp1_input').mkdir()
        failed_at = time.monotonic()
        time.sleep(1.0)
        self.assertTrue(json.loads((self.run_dir / 'apu_telemetry.json').read_text())['hardware']['cpu']['valid'])
        # ...but a persistent one marks the rail invalid instead of freezing it.
        self.wait_for(lambda d: not d['hardware']['cpu']['valid'])
        self.assertGreater(time.monotonic() - failed_at, 2.5)
        self.assertTrue(json.loads((self.run_dir / 'apu_telemetry.json').read_text())['hardware']['gpu']['valid'])
        # Config changes apply on SIGHUP (systemctl reload), no restart needed.
        self.config.write_text('vrm_source = off\n')
        proc.send_signal(signal.SIGHUP)
        self.wait_for(lambda d: d['sources']['vrm'] == 'off')
        proc.terminate()
        self.assertEqual(proc.wait(5), 0)
        log = proc.stderr.read()
        self.assertIn('reloading configuration', log)
        # State changes are logged once, not once per 250 ms cycle.
        self.assertEqual(log.count('VRM source: kernel bc250_vrm driver'), 1)

    def test_feature_marker_for_installer(self):
        data = DAEMON.read_bytes()
        self.assertIn(b'BC250_TELEMETRY_FEATURES=memory_snapshot_v1,', data)
        self.assertIn(b'config_v1', data)


if __name__ == '__main__':
    unittest.main()
