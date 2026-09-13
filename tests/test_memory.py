"""Offline integration tests; no real SMU, PCI or DMA access."""
import contextlib
import ctypes
import io
import json
import logging
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'memory'))
sys.path.insert(0, str(ROOT / 'web'))
import collector
import patcher
import telemetry
import server
import unlock
from bc250_smu import Bc250Smu, Bc250Mailbox, SmuError, SmuRejected, SmuTimeout
from bc250_smu.transport import Bc250PciTransport
from bc250_smu.primitives import PrimitiveMixin


class FakeTransport:
    def __init__(self):
        self.writes = []
        self.regs = {0x03B10A8C: 0xDEADBEEF}
        self.status = 1

    def open(self): pass
    def close(self): pass

    def write_smu_reg(self, addr, value):
        self.writes.append((addr, value))
        self.regs[addr] = value
        for cmd, rsp in ((0x03B10A20, 0x03B10A80), (0x03B10528, 0x03B10564)):
            if addr == cmd:
                self.regs[rsp] = self.status

    def read_smu_reg(self, addr):
        return self.regs.get(addr, 0)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        with patch('bc250_smu.api.Bc250PciTransport', return_value=self.transport):
            self.smu = Bc250Smu(timeout=0.001)

    def test_temperature_command_does_not_touch_q4(self):
        self.smu.read_chip(3)
        self.assertEqual(self.transport.regs[0x03B10A8C], 0xDEADBEEF)
        self.assertEqual(self.transport.writes, [(0x03B10A80, 0), (0x03B10A88, 3), (0x03B10A20, 5)])

    def test_transfer_retains_six_argument_layout(self):
        self.smu.transfer_engine_dram2smu(1, 2, 3, 4)
        values = [self.transport.regs[0x03B10998 + i*4] for i in range(6)]
        self.assertEqual(values, [0x23, 1, 2, 3, 0, 4])

    def test_shared_lock_and_prevalidation(self):
        self.assertIs(self.smu._queues[2]._lock, self.smu._queues[3]._lock)
        for args in ([1, 2], [-1], [1.2]):
            with self.assertRaises(ValueError):
                self.smu.send_message(3, 5, args)
        for chip in (-1, 8, True):
            with self.assertRaises(ValueError): self.smu.read_chip(chip)
        self.assertEqual(self.transport.writes, [])

    def test_error_status_is_not_unlocked(self):
        for status in (0xFF, 0xFE, 0xFC):
            self.transport.status = status
            with self.assertRaises(SmuRejected): self.smu.secure_access_enabled()
        self.transport.status = 0xFD
        self.assertFalse(self.smu.secure_access_enabled())
        self.transport.status = 1
        self.assertTrue(self.smu.secure_access_enabled())

    def test_timeout_blocks_all_subsequent_commands(self):
        self.transport.status = 0
        with self.assertRaises(SmuTimeout): self.smu.read_chip(0)
        count = len(self.transport.writes)
        with self.assertRaises(SmuError): self.smu.transfer_engine_sram_load(0x748C, 1)
        self.assertEqual(len(self.transport.writes), count)

    def test_short_io_is_rejected(self):
        transport = Bc250PciTransport()
        transport._fd = 99999
        with patch('bc250_smu.transport.os.pwrite', return_value=0):
            with self.assertRaises(OSError): transport.write_smu_reg(0, 0)
        with patch('bc250_smu.transport.os.pwrite', return_value=4), \
                patch('bc250_smu.transport.os.pread', return_value=b'\0'):
            with self.assertRaises(OSError): transport.read_smu_reg(0)


class SramSmu:
    def __init__(self):
        self.memory = bytearray(0x40000)
        self.memory[patcher.HANDLER_REG:patcher.HANDLER_REG+4] = (0x1234).to_bytes(4, 'little')
        self.writes = []
        self.drop_payload = False

    def alive(self): return True

    def smu_read(self, addr, n=1): return bytes(self.memory[addr:addr+n*4])

    def smu_write32(self, addr, value):
        self.writes.append((addr, value))
        if self.drop_payload and patcher.PAYLOAD_START <= addr < patcher.PAYLOAD_END:
            return
        self.memory[addr:addr+4] = value.to_bytes(4, 'little')


class DmaReadTests(unittest.TestCase):
    def test_sram_read_requires_dma_to_replace_both_fill_patterns(self):
        page = ctypes.create_string_buffer(4096)
        va = ctypes.addressof(page)
        smu = PrimitiveMixin()
        smu._lock = threading.RLock()
        smu.transfer_engine_sram_load = Mock()
        with patch('bc250_smu.primitives.alloc_page', return_value=(va, 0x1000)), \
                patch('bc250_smu.primitives.free_page'):
            for length in (0, 2, 4):
                smu.transfer_engine_smu2dram = Mock(side_effect=lambda *args: ctypes.memset(va, 0, length))
                if length < 4:
                    with self.assertRaisesRegex(SmuError, 'DMA verification failed'):
                        smu.smu_read(0x748C)
                else:
                    self.assertEqual(smu.smu_read(0x748C), b'\0'*4)
            smu.transfer_engine_smu2dram = Mock(side_effect=lambda *args: ctypes.memset(va, 0xA5, 4))
            self.assertEqual(smu.smu_read(0x748C), b'\xA5'*4)


class PatcherTests(unittest.TestCase):
    def test_pinned_bin_and_elf_agree(self):
        binary = patcher.load_payload()
        elf = (ROOT / 'memory/payload/SMUPayload.elf').read_bytes()
        hdr = struct.unpack_from('<16sHHIIIIIHHHHHH', elf)
        sections = [struct.unpack_from('<10I', elf, hdr[6] + i*hdr[11]) for i in range(hdr[12])]
        text = sections[1]
        self.assertEqual(text[3], patcher.PAYLOAD_START)
        self.assertEqual(binary, elf[text[4]:text[4]+text[5]])
        symtab = next(s for s in sections if s[1] == 2)
        string_section = sections[symtab[6]]
        strings = elf[string_section[4]:string_section[4]+string_section[5]]
        found = {}
        for offset in range(symtab[4], symtab[4]+symtab[5], symtab[9]):
            name, value, *_ = struct.unpack_from('<IIIBBH', elf, offset)
            found[strings[name:].split(b'\0')[0]] = value
        self.assertEqual(found[b'umc_read_temp_per_chip'], patcher.HANDLER)

    def test_rejects_empty_truncated_and_modified_payload(self):
        good = patcher.load_payload()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'payload'
            for data in (b'', good[:-4], b'\xff'*len(good), good + b'\0'):
                path.write_bytes(data)
                with self.assertRaises(SmuError): patcher.load_payload(path)

    def test_install_verifies_before_handler_and_restart_skips_unlock(self):
        smu = SramSmu()
        payload = patcher.load_payload()
        with patch.object(patcher, 'unlock') as unlock_mock:
            self.assertEqual(patcher.ensure_patch(smu, payload), 'patched')
            self.assertEqual(smu.writes[-1], (patcher.HANDLER_REG, patcher.HANDLER))
            count = len(smu.writes)
            self.assertEqual(patcher.ensure_patch(smu, payload, allow_install=False), 'already_patched')
            self.assertEqual(len(smu.writes), count)
            unlock_mock.assert_called_once_with(smu)

    def test_payload_readback_failure_never_switches_handler(self):
        smu = SramSmu()
        smu.drop_payload = True
        with patch.object(patcher, 'unlock'), self.assertRaises(SmuError):
            patcher.ensure_patch(smu, patcher.load_payload())
        self.assertNotIn(patcher.HANDLER_REG, [addr for addr, _ in smu.writes])

    def test_empty_handler_can_be_installed_once(self):
        smu = SramSmu()
        smu.memory[patcher.HANDLER_REG:patcher.HANDLER_REG + 4] = bytes(4)
        payload = patcher.load_payload()
        with patch.object(patcher, 'unlock') as unlock_mock:
            self.assertEqual(patcher.ensure_patch(smu, payload), 'patched')
            self.assertEqual(smu.writes[-1], (patcher.HANDLER_REG, patcher.HANDLER))
            writes = list(smu.writes)
            self.assertEqual(patcher.ensure_patch(smu, payload, allow_install=False), 'already_patched')
            self.assertEqual(smu.writes, writes)
            unlock_mock.assert_called_once()

    def test_empty_handler_after_ready_and_invalid_pointers_are_rejected(self):
        for current, allow in ((0, False), (1, True), (0xFFFFFFFF, True), (patcher.PAYLOAD_START, True)):
            with self.subTest(current=current, allow_install=allow):
                smu = SramSmu()
                smu.memory[patcher.HANDLER_REG:patcher.HANDLER_REG + 4] = current.to_bytes(4, 'little')
                with patch.object(patcher, 'unlock') as unlock_mock, self.assertRaises(SmuError):
                    patcher.ensure_patch(smu, patcher.load_payload(), allow_install=allow)
                unlock_mock.assert_not_called()
                self.assertEqual(smu.writes, [])

    def test_mismatched_existing_patch_and_reinstall_are_rejected(self):
        smu = SramSmu()
        with patch.object(patcher, 'unlock') as unlock_mock:
            with self.assertRaises(SmuError):
                patcher.ensure_patch(smu, patcher.load_payload(), allow_install=False)
            smu.smu_write32(patcher.HANDLER_REG, patcher.HANDLER)
            with self.assertRaises(SmuError): patcher.ensure_patch(smu, patcher.load_payload())
            unlock_mock.assert_not_called()

    def test_platform_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dmi, pci = root / 'dmi', root / 'pci'
            dmi.mkdir(); pci.mkdir()
            (dmi / 'board_name').write_text('BC-250\n')
            (dmi / 'bios_version').write_text('P3.00\n')
            for bdf, vendor, device in (('0000:00:00.0', '0x1022', '0x0000'), ('0000:01:00.0', '0x1002', '0x13fe')):
                path = pci / bdf
                path.mkdir()
                (path / 'vendor').write_text(vendor)
                (path / 'device').write_text(device)
            for board in ('BC-250', 'AMD BC-250', 'BC250'):
                (dmi / 'board_name').write_text(board)
                patcher.check_platform(dmi, pci)
            (dmi / 'board_name').write_text('Some other AMD board')
            with self.assertRaises(SmuError): patcher.check_platform(dmi, pci)
            (dmi / 'board_name').write_text('AMD BC-250')
            (dmi / 'bios_version').write_text('P2.0')
            with self.assertRaises(SmuError): patcher.check_platform(dmi, pci)

    def test_check_reports_expected_failure_without_traceback_or_smu_access(self):
        for error in (SmuError('unsupported board'), PermissionError('DMI denied')):
            with self.subTest(error=error), patch.object(sys, 'argv', ['collector.py', '--check']), \
                    patch.object(collector, 'check_platform', side_effect=error), \
                    patch.object(collector, 'Bc250Smu') as smu, self.assertLogs(level=logging.ERROR) as logs:
                self.assertEqual(collector.main(), 1)
                self.assertIn(str(error), logs.output[0])
                smu.assert_not_called()

    def test_unlock_noop_dma_cannot_pass_readback(self):
        smu = Mock()
        smu.alive.return_value = True
        smu.secure_access_enabled.return_value = False
        smu.smu_read.side_effect = lambda addr, n=1: b'\1\0\0\0' if addr == unlock.DBG_DISABLE else bytes(n*4)
        page = ctypes.create_string_buffer(4096)
        with contextlib.redirect_stdout(io.StringIO()), patch.object(unlock, '_subq4_cur_idx', 0):
            with self.assertRaisesRegex(SmuError, 'fake table did not land'):
                unlock._do_unlock(smu, ctypes.addressof(page), 0)
        smu.sec_smn_read32.assert_not_called()

    def test_unlock_restores_actual_dynamic_region_and_saved_values(self):
        page = ctypes.create_string_buffer(4096)
        va = ctypes.addressof(page)

        class UnlockSmu(SramSmu):
            def secure_access_enabled(self):
                return self.memory[unlock.DBG_DISABLE] == 0

            def q2_0x23_append(self, args):
                self.memory[0x18DF0:0x18DF4] = b'xxxx'
                self.memory[0x19780:0x19790] = b'x'*16
                return 1, 0

            def transfer_engine_dram2smu(self, hi, lo, words, key):
                if key == 3:
                    self.staged = ctypes.string_at(va, words*4)
                    self.memory[0x7B1C:0x7B3C] = self.staged
                else:
                    self.memory[unlock.DBG_DISABLE:unlock.DBG_DISABLE+4] = bytes(4)
                    self.memory[unlock.NEW_ENTRY_ADDR:unlock.NEW_ENTRY_ADDR+4] = b'xxxx'

            def transfer_engine_smu2dram(self, hi, lo, words):
                ctypes.memmove(va, self.staged, len(self.staged))

            def sec_smn_read32(self, addr): return 1, 0

        smu = UnlockSmu()
        smu.memory[unlock.DBG_DISABLE] = 1
        smu.memory[0x18DF0:0x18FE0] = b'R'*0x1F0
        smu.memory[0x19780:0x19790] = b'S'*16
        before = bytes(smu.memory)
        with contextlib.redirect_stdout(io.StringIO()), patch.object(unlock, '_subq4_cur_idx', 0):
            self.assertTrue(unlock._do_unlock(smu, va, 0))
        expected = bytearray(before)
        expected[unlock.DBG_DISABLE] = 0
        self.assertEqual(smu.memory, expected)


NATIVE_READER = None


def read_memory(path, now=None):
    args = [str(NATIVE_READER), str(path)]
    if now is not None:
        args.append(str(now))
    return json.loads(subprocess.check_output(args, text=True))


@unittest.skipUnless(shutil.which('g++'), 'g++ is required to build the native reader harness')
class CollectorAndApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global NATIVE_READER
        build = tempfile.TemporaryDirectory()
        cls.addClassCleanup(build.cleanup)
        source = Path(build.name) / 'reader.cpp'
        source.write_text('#include "memory_telemetry.h"\n#include <iostream>\n'
                          'int main(int argc, char **argv) { std::cout << memory_telemetry::read_json('
                          'argv[1], argc > 2 ? std::stod(argv[2]) : memory_telemetry::boot_seconds()); }\n')
        NATIVE_READER = Path(build.name) / 'reader'
        subprocess.run(['g++', '-std=c++11', '-Wall', '-Wextra', '-Werror', '-I', str(ROOT),
                        str(source), '-o', str(NATIVE_READER)], check=True)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        collector.STOP.clear()
        self.addCleanup(collector.STOP.clear)

    def reading(self, raw=None):
        smu = Mock()
        smu.read_chip.side_effect = raw if raw is not None else [0x2626, 0x2525, 0x2A2A, 0x2626, 0x2626, 0x2929, 0x2929, 0x2727]
        return collector.sample(smu)

    def test_sample_and_api_merge_preserve_base(self):
        base = self.root / 'base.json'
        memory = self.root / 'memory.json'
        original = {'hardware': {'cpu': {'valid': True}}, 'software': {'cpu_temp_c': 50}, 'cooling': {'fan_rpm': 1000}}
        collector.atomic_json(base, original)
        collector.publish_snapshot(memory, self.reading())
        combined = dict(original, memory=read_memory(memory))
        collector.atomic_json(base, combined)
        merged = telemetry.read_telemetry(base)
        self.assertEqual({k: merged[k] for k in original}, original)
        self.assertEqual(json.loads(base.read_text()), combined)
        self.assertEqual(merged['memory']['average_c'], 38.5)
        self.assertEqual(merged['memory']['hotspot_c'], 44)
        self.assertEqual(merged['memory']['hotspot_chip'], 2)
        self.assertTrue(merged['memory']['valid'])
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_invalid_codes_and_saturation(self):
        self.assertFalse(self.reading([0xFFFF]*8)['valid'])
        saturated = self.reading([80, 0, 20, 30, 40, 50, 60, 70])
        self.assertEqual(saturated['saturated_chips'], [0])
        self.assertEqual(saturated['chips_c'][:2], [120, -40])
        self.assertTrue(saturated['saturated'])

    def test_missing_corrupt_stale_and_future_memory(self):
        path = self.root / 'memory.json'
        self.assertEqual(read_memory(path)['status'], 'unavailable')
        for text in ('{', '[]', '{"valid": true}', '{"valid": true, "sampled_boottime_s": NaN}'):
            path.write_text(text)
            self.assertEqual(read_memory(path)['status'], 'invalid_data')
        data = self.reading()
        data['sampled_boottime_s'] = 100
        collector.publish_snapshot(path, data)
        stale = read_memory(path, now=116)
        self.assertEqual(stale['status'], 'stale')
        self.assertEqual(stale['age_ms'], 16000)
        self.assertEqual(stale['chips_c'], [None]*8)
        self.assertEqual(read_memory(path, now=99)['status'], 'invalid_data')

    def test_memory_errors_do_not_remove_main_telemetry(self):
        path = self.root / 'base.json'
        canonical = {'hardware': {'ok': True}, 'memory': read_memory(self.root/'missing')}
        collector.atomic_json(path, canonical)
        self.assertEqual(telemetry.read_telemetry(path), canonical)
        self.assertFalse(canonical['memory']['valid'])
        self.assertIn('error', telemetry.read_telemetry(self.root/'absent'))

    def test_snapshot_errors_are_escaped_and_bad_words_rejected(self):
        path = self.root / 'snapshot'
        data = collector.unavailable('error', 'failure "quoted"\nsecond line')
        collector.publish_snapshot(path, data)
        self.assertEqual(read_memory(path)['error'], 'failure "quoted" second line')
        for words in ('1 2', '0 0 0 0 0 0 0 81', '0 0 0 0 0 0 0 4294967296',
                      '0 0 0 0 0 0 0 0 trailing'):
            path.write_text('BC250_MEMORY_V1\n100\nok\n' + words + '\n\n')
            self.assertEqual(read_memory(path, 101)['status'], 'invalid_data')

    def test_failed_guard_prevents_hardware_open_on_restart(self):
        for previous in ({'state': 'reading'}, {'state': 'preparing'}, None, [],
                         {'state': 'failed', 'error': str(SmuTimeout(3, 5))},
                         {'state': 'failed', 'error': 'payload readback mismatch; handler was not switched'}):
            with self.subTest(previous=previous):
                collector.atomic_json(self.root / 'patch-state.json', previous)
                with patch.object(collector, 'check_platform'), patch.object(collector, 'Bc250Smu') as smu, \
                        self.assertLogs(level=logging.ERROR):
                    self.assertEqual(collector.run(self.root), 1)
                smu.assert_not_called()
                self.assertEqual(json.loads((self.root/'patch-state.json').read_text()), previous)

    def test_legacy_empty_handler_failure_recovers_with_real_patch_sequence(self):
        collector.atomic_json(self.root/'patch-state.json', collector.EMPTY_HANDLER_REJECTION)
        smu = SramSmu()
        smu.memory[patcher.HANDLER_REG:patcher.HANDLER_REG + 4] = bytes(4)
        smu.close = Mock()
        reading = self.reading()
        def one_sample(device):
            collector.STOP.set()
            self.assertEqual(device.smu_read(patcher.HANDLER_REG), patcher.HANDLER.to_bytes(4, 'little'))
            return reading
        with patch.object(collector, 'check_platform'), patch.object(collector, 'Bc250Smu', return_value=smu), \
                patch.object(patcher, 'unlock') as unlock_mock, \
                patch.object(collector, 'sample', side_effect=one_sample):
            self.assertEqual(collector.run(self.root), 0)
            self.assertEqual(json.loads((self.root/'patch-state.json').read_text()), {'state': 'ready'})
            writes = list(smu.writes)
            collector.STOP.clear()
            self.assertEqual(collector.run(self.root), 0)
            self.assertEqual(smu.writes, writes)
            unlock_mock.assert_called_once()

    def test_pci_open_failure_does_not_create_operation_guard(self):
        with patch.object(collector, 'check_platform'), \
                patch.object(collector, 'Bc250Smu', side_effect=PermissionError('PCI denied')), \
                self.assertLogs(level=logging.ERROR):
            self.assertEqual(collector.run(self.root), 1)
        self.assertFalse((self.root/'patch-state.json').exists())

    def test_preflight_failure_precedes_hardware_and_guard(self):
        with patch.object(collector, 'load_payload', side_effect=SmuError('bad payload')), \
                patch.object(collector, 'Bc250Smu') as smu, self.assertLogs(level=logging.ERROR):
            self.assertEqual(collector.run(self.root), 1)
        smu.assert_not_called()
        self.assertFalse((self.root/'patch-state.json').exists())

    def test_timeout_is_published_and_blocks_retry(self):
        smu = Mock()
        smu.read_chip.side_effect = SmuTimeout(3, 5)
        with patch.object(collector, 'check_platform'), \
                patch.object(collector, 'ensure_patch', return_value='patched'), \
                patch.object(collector, 'Bc250Smu', return_value=smu), self.assertLogs(level=logging.ERROR):
            self.assertEqual(collector.run(self.root), 1)
        self.assertEqual(smu.read_chip.call_count, 1)
        smu.close.assert_called_once()
        self.assertEqual(json.loads((self.root/'patch-state.json').read_text())['state'], 'failed')
        self.assertFalse(read_memory(self.root/'telemetry')['valid'])

    def test_successful_start_then_clean_stop_preserves_ready_guard(self):
        reading = self.reading()
        def one_sample(smu):
            collector.STOP.set()
            return reading
        with patch.object(collector, 'check_platform'), patch.object(collector, 'ensure_patch') as prepare, \
                patch.object(collector, 'Bc250Smu'), patch.object(collector, 'sample', side_effect=one_sample):
            self.assertEqual(collector.run(self.root), 0)
            self.assertTrue(prepare.call_args.kwargs['allow_install'])
            collector.STOP.clear()
            self.assertEqual(collector.run(self.root), 0)
            self.assertFalse(prepare.call_args.kwargs['allow_install'])
        self.assertEqual(json.loads((self.root/'patch-state.json').read_text())['state'], 'ready')
        self.assertEqual(read_memory(self.root/'telemetry')['status'], 'stopped')

    def test_second_collector_cannot_overwrite_first_snapshot(self):
        import fcntl
        path = self.root/'telemetry'
        path.write_text('existing')
        with (self.root/'smu.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertLogs(level=logging.ERROR): self.assertEqual(collector.run(self.root), 1)
        self.assertEqual(path.read_text(), 'existing')

    def test_http_endpoint_contains_additive_memory_block(self):
        class Request:
            def __init__(self): self.response = bytearray()
            def makefile(self, mode, buffering):
                return io.BytesIO(b'GET /api/telemetry HTTP/1.0\r\n\r\n')
            def sendall(self, data): self.response.extend(data)
        request = Request()
        data = {'hardware': {'cpu': {'valid': True}}, 'memory': {'valid': False}}
        with patch.object(server.telemetry, 'read_telemetry', return_value=data), \
                patch.object(server.Handler, 'log_message'):
            server.Handler(request, ('127.0.0.1', 1), Mock())
        headers, body = bytes(request.response).split(b'\r\n\r\n', 1)
        self.assertIn(b'200 OK', headers)
        self.assertEqual(json.loads(body), data)


if __name__ == '__main__': unittest.main()
