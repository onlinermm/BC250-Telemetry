"""Offline integration tests; no real SMU, PCI or DMA access."""
import contextlib
import ctypes
import fcntl
import hashlib
import io
import json
import logging
import os
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

    def test_packed_command_uses_q3_6(self):
        self.smu.read_packed(1)
        self.assertEqual(self.transport.writes, [(0x03B10A80, 0), (0x03B10A88, 1), (0x03B10A20, 6)])
        for parity in (-1, 2, True):
            with self.assertRaises(ValueError): self.smu.read_packed(parity)

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

    def config_transport(self):
        """Transport over a regular file standing in for PCI config space."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'config'
        path.write_bytes(bytes(256))
        transport = Bc250PciTransport()
        transport._config_path = str(path)
        with patch('bc250_smu.transport.os.geteuid', return_value=0):
            transport.open()
        self.addCleanup(transport.close)
        return transport, path

    def assert_window_free(self, path):
        with open(path, 'rb') as other:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_short_io_is_rejected_and_releases_the_window(self):
        transport, path = self.config_transport()
        with patch('bc250_smu.transport.os.pwrite', return_value=0):
            with self.assertRaises(OSError): transport.write_smu_reg(0, 0)
        self.assert_window_free(path)
        with patch('bc250_smu.transport.os.pwrite', return_value=4), \
                patch('bc250_smu.transport.os.pread', return_value=b'\0'):
            with self.assertRaises(OSError): transport.read_smu_reg(0)
        self.assert_window_free(path)

    def test_address_and_value_share_one_flock(self):
        transport, _ = self.config_transport()
        events = []
        real_flock, real_pwrite, real_pread = fcntl.flock, os.pwrite, os.pread
        def flock(fd, operation):
            events.append('lock' if operation == fcntl.LOCK_EX else 'unlock')
            real_flock(fd, operation)
        def pwrite(fd, data, offset):
            events.append(('write', offset))
            return real_pwrite(fd, data, offset)
        def pread(fd, size, offset):
            events.append(('read', offset))
            return real_pread(fd, size, offset)
        with patch('bc250_smu.transport.fcntl.flock', side_effect=flock), \
                patch('bc250_smu.transport.os.pwrite', side_effect=pwrite), \
                patch('bc250_smu.transport.os.pread', side_effect=pread):
            transport.write_smu_reg(0x03B10A88, 3)
            self.assertEqual(transport.read_smu_reg(0x03B10A80), 3)
        pair = [('write', 0xB8), ('read', 0xBC)]
        self.assertEqual(events, ['lock', ('read', 0xB8), ('write', 0xB8), ('write', 0xBC), ('write', 0xB8), 'unlock',
                                  'lock', ('read', 0xB8), *pair, ('write', 0xB8), 'unlock'])

    def test_pair_between_a_neighbours_address_and_value_keeps_their_address(self):
        # bc250_smu_oc and the governor lock each config access, not the pair.
        transport, path = self.config_transport()
        with open(path, 'r+b') as neighbour:
            neighbour.seek(0xB8)
            neighbour.write(struct.pack('<I', 0x03B10564))
            neighbour.flush()
            transport.write_smu_reg(0x03B10A88, 5)
            self.assertEqual(transport.read_smu_reg(0x03B10A88), 5)
            neighbour.seek(0xB8)
            self.assertEqual(struct.unpack('<I', neighbour.read(4))[0], 0x03B10564)

    def test_pair_waits_for_another_process_holding_the_window(self):
        transport, path = self.config_transport()
        with open(path, 'rb') as other:
            fcntl.flock(other, fcntl.LOCK_EX)
            writer = threading.Thread(target=transport.write_smu_reg, args=(0x03B10A88, 0x12345678))
            writer.start()
            writer.join(0.2)
            self.assertTrue(writer.is_alive())
            self.assertEqual(path.read_bytes()[0xB8:0xC0], bytes(8))
            fcntl.flock(other, fcntl.LOCK_UN)
            writer.join(5)
        self.assertFalse(writer.is_alive())
        self.assertEqual(path.read_bytes()[0xB8:0xC0], struct.pack('<II', 0, 0x12345678))


class SramSmu:
    def __init__(self):
        self.memory = bytearray(0x40000)
        self.memory[patcher.HANDLER_REG:patcher.HANDLER_REG+4] = (0x1234).to_bytes(4, 'little')
        self.memory[0:4] = patcher.SMU_VERSION.to_bytes(4, 'little')
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
    HOOK_REGS = (patcher.PACKED_HANDLER_REG, patcher.REGDUMP_HANDLER_REG, patcher.HANDLER_REG)

    def test_pinned_bin_and_symbols_agree(self):
        payload = patcher.load_payload()
        self.assertEqual(len(payload), patcher.PAYLOAD_SIZE + patcher.BSS_SIZE)
        self.assertEqual(payload[patcher.PAYLOAD_SIZE:], bytes(patcher.BSS_SIZE))
        symbols = {}
        for line in (ROOT / 'memory/payload/SMUPayload.symbols').read_text().splitlines():
            name, kind, value, *size = line.split()
            symbols[name] = (kind, int(value, 16), int(size[0], 16) if size else 0)
        end = patcher.PAYLOAD_START + patcher.PAYLOAD_SIZE
        for reg, name in zip(self.HOOK_REGS, ('umc_read_temp_packed', 'regdump_read', 'umc_read_temp_per_chip')):
            kind, address, size = symbols[name]
            self.assertEqual((kind, address), ('T', patcher.HOOKS[reg]))
            self.assertLessEqual(address + size, end)
        # BSS (dumps and polling counters) is exactly the zeroed tail.
        bss = [(address, size) for kind, address, size in symbols.values() if kind in 'bB']
        self.assertEqual(min(a for a, _ in bss), end)
        self.assertEqual(max(a + n for a, n in bss), end + patcher.BSS_SIZE)
        self.assertEqual(patcher.KNOWN_PAYLOADS[0][:4],
                         ('modern', patcher.HANDLER, patcher.PAYLOAD_SIZE, patcher.PAYLOAD_SHA256))

    def test_rejects_empty_truncated_and_modified_payload(self):
        good = patcher.load_payload()[:patcher.PAYLOAD_SIZE]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'payload'
            for data in (b'', good[:-4], b'\xff'*len(good), good + b'\0'*4):
                path.write_bytes(data)
                with self.assertRaises(SmuError): patcher.load_payload(path)

    def assert_hooks_installed(self, smu):
        for reg, entry in patcher.HOOKS.items():
            self.assertEqual(smu.smu_read(reg), entry.to_bytes(4, 'little'))

    def test_install_verifies_before_handlers_and_restart_skips_unlock(self):
        smu = SramSmu()
        payload = patcher.load_payload()
        with patch.object(patcher, 'unlock') as unlock_mock:
            self.assertEqual(patcher.ensure_patch(smu, payload), 'patched')
            # Code and BSS first, then the hooks with Q3/5 last.
            self.assertEqual([addr for addr, _ in smu.writes[-3:]], list(self.HOOK_REGS))
            self.assertEqual(len(smu.writes), len(payload) // 4 + 3)
            self.assert_hooks_installed(smu)
            count = len(smu.writes)
            self.assertEqual(patcher.ensure_patch(smu, payload, allow_install=False), 'already_patched_modern')
            self.assertEqual(len(smu.writes), count)
            unlock_mock.assert_called_once_with(smu)

    def test_occupied_region_is_never_overwritten(self):
        size = patcher.PAYLOAD_SIZE + patcher.BSS_SIZE
        for offset in (0, patcher.PAYLOAD_SIZE, size - 1):
            with self.subTest(offset=offset):
                smu = SramSmu()
                smu.memory[patcher.PAYLOAD_START + offset] = 0x5A
                with patch.object(patcher, 'unlock') as unlock_mock, \
                        self.assertRaisesRegex(SmuError, 'not empty'):
                    patcher.ensure_patch(smu, patcher.load_payload())
                unlock_mock.assert_not_called()
                self.assertEqual(smu.writes, [])
        # The byte after the region is not ours and does not block installation.
        smu = SramSmu()
        smu.memory[patcher.PAYLOAD_START + size] = 0x5A
        with patch.object(patcher, 'unlock'):
            self.assertEqual(patcher.ensure_patch(smu, patcher.load_payload()), 'patched')

    def test_payload_readback_failure_never_switches_handlers(self):
        smu = SramSmu()
        smu.drop_payload = True
        with patch.object(patcher, 'unlock'), self.assertRaises(SmuError):
            patcher.ensure_patch(smu, patcher.load_payload())
        self.assertFalse(set(patcher.HOOKS) & {addr for addr, _ in smu.writes})

    def test_empty_handlers_can_be_installed_once(self):
        smu = SramSmu()
        smu.memory[patcher.HANDLER_REG:patcher.HANDLER_REG + 4] = bytes(4)
        payload = patcher.load_payload()
        with patch.object(patcher, 'unlock') as unlock_mock:
            self.assertEqual(patcher.ensure_patch(smu, payload), 'patched')
            self.assert_hooks_installed(smu)
            writes = list(smu.writes)
            self.assertEqual(patcher.ensure_patch(smu, payload, allow_install=False), 'already_patched_modern')
            self.assertEqual(smu.writes, writes)
            unlock_mock.assert_called_once()

    def test_invalid_existing_handlers_are_rejected(self):
        cases = [(patcher.HANDLER_REG, 0, False)]
        for reg in patcher.HOOKS:
            cases += [(reg, 1, True), (reg, 0xFFFFFFFF, True), (reg, patcher.PAYLOAD_START, True),
                      (reg, patcher.HOOKS[reg], True) if reg != patcher.HANDLER_REG else (reg, 0x3AAC4, True)]
        for reg, current, allow in cases:
            with self.subTest(reg=hex(reg), current=hex(current), allow_install=allow):
                smu = SramSmu()
                smu.memory[reg:reg + 4] = current.to_bytes(4, 'little')
                with patch.object(patcher, 'unlock') as unlock_mock, self.assertRaises(SmuError):
                    patcher.ensure_patch(smu, patcher.load_payload(), allow_install=allow)
                unlock_mock.assert_not_called()
                self.assertEqual(smu.writes, [])

    def test_wrong_smu_firmware_is_not_unlocked(self):
        smu = SramSmu()
        smu.memory[0:4] = (0x00580700).to_bytes(4, 'little')
        with patch.object(patcher, 'unlock') as unlock_mock, self.assertRaisesRegex(SmuError, 'SMU firmware'):
            patcher.ensure_patch(smu, patcher.load_payload())
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

    def _firmware_install(self, smu, handler, size):
        blob = bytes((i * 7 + 1) & 0xFF for i in range(size))
        smu.memory[patcher.PAYLOAD_START:patcher.PAYLOAD_START + size] = blob
        smu.memory[patcher.HANDLER_REG:patcher.HANDLER_REG + 4] = handler.to_bytes(4, 'little')
        return blob

    def _firmware_bundled(self, smu, hooks=True):
        """The bundled image as a BIOS/EFI installs it, with used BSS."""
        payload = patcher.load_payload()
        smu.memory[patcher.PAYLOAD_START:patcher.PAYLOAD_START + len(payload)] = payload
        end = patcher.PAYLOAD_START + patcher.PAYLOAD_SIZE
        # Mutable BSS after the code must not affect identification.
        smu.memory[end:end + patcher.BSS_SIZE] = b'\x5a' * patcher.BSS_SIZE
        for reg, entry in patcher.HOOKS.items():
            if hooks or reg == patcher.HANDLER_REG:
                smu.memory[reg:reg + 4] = entry.to_bytes(4, 'little')
        return payload

    def test_firmware_bundled_payload_is_accepted_without_writes(self):
        smu = SramSmu()
        self._firmware_bundled(smu)
        with patch.object(patcher, 'unlock') as unlock_mock:
            for mode in ('auto', 'reader-only'):
                self.assertEqual(patcher.ensure_patch(smu, patcher.load_payload(), mode=mode),
                                 'already_patched_modern')
            unlock_mock.assert_not_called()
        self.assertEqual(smu.writes, [])

    def test_firmware_bundled_requires_all_hooks(self):
        smu = SramSmu()
        self._firmware_bundled(smu, hooks=False)
        for mode in ('auto', 'reader-only'):
            with patch.object(patcher, 'unlock') as unlock_mock, self.assertRaisesRegex(SmuError, 'hook'):
                patcher.ensure_patch(smu, patcher.load_payload(), mode=mode)
            unlock_mock.assert_not_called()
        self.assertEqual(smu.writes, [])

    def test_older_firmware_payloads_are_accepted_by_hash_without_writes(self):
        for index, name in ((1, 'original'), (2, 'bounded')):
            smu = SramSmu()
            _, handler, size, _, hooks = patcher.KNOWN_PAYLOADS[index]
            blob = self._firmware_install(smu, handler, size)
            known = list(patcher.KNOWN_PAYLOADS)
            known[index] = (name, handler, size, hashlib.sha256(blob).hexdigest(), hooks)
            with patch.object(patcher, 'KNOWN_PAYLOADS', tuple(known)), \
                    patch.object(patcher, 'unlock') as unlock_mock:
                for mode in ('auto', 'reader-only'):
                    self.assertEqual(patcher.ensure_patch(smu, patcher.load_payload(), mode=mode),
                                     f'already_patched_{name}')
                unlock_mock.assert_not_called()
            self.assertEqual(smu.writes, [])

    def test_known_handler_with_foreign_bytes_is_rejected(self):
        for _, handler, size, _, _ in patcher.KNOWN_PAYLOADS:
            smu = SramSmu()
            self._firmware_install(smu, handler, size)
            with patch.object(patcher, 'unlock') as unlock_mock, self.assertRaises(SmuError):
                patcher.ensure_patch(smu, patcher.load_payload(), mode='reader-only')
            unlock_mock.assert_not_called()
            self.assertEqual(smu.writes, [])

    def test_reader_only_never_installs(self):
        for current in (0, 0x1234):
            smu = SramSmu()
            smu.memory[patcher.HANDLER_REG:patcher.HANDLER_REG + 4] = current.to_bytes(4, 'little')
            with patch.object(patcher, 'unlock') as unlock_mock, self.assertRaisesRegex(SmuError, 'no known'):
                patcher.ensure_patch(smu, patcher.load_payload(), mode='reader-only')
            unlock_mock.assert_not_called()
            self.assertEqual(smu.writes, [])
        with self.assertRaises(SmuError):
            patcher.ensure_patch(SramSmu(), patcher.load_payload(), mode='bogus')

    def test_bios_string_only_relaxed_in_reader_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dmi, pci = root / 'dmi', root / 'pci'
            dmi.mkdir(); pci.mkdir()
            (dmi / 'board_name').write_text('BC-250\n')
            (dmi / 'bios_version').write_text('P3.00-PUNSH-T\n')
            for bdf, vendor, device in (('0000:00:00.0', '0x1022', '0x0000'), ('0000:01:00.0', '0x1002', '0x13fe')):
                (pci / bdf).mkdir()
                (pci / bdf / 'vendor').write_text(vendor)
                (pci / bdf / 'device').write_text(device)
            with self.assertRaises(SmuError): patcher.check_platform(dmi, pci)
            patcher.check_platform(dmi, pci, strict_bios=False)
            (dmi / 'board_name').write_text('Other')
            with self.assertRaises(SmuError): patcher.check_platform(dmi, pci, strict_bios=False)

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
class ProbeTests(unittest.TestCase):
    """collector.probe(): read-only, never writes SRAM or leaves a bad guard."""
    _firmware_install = PatcherTests._firmware_install
    _firmware_bundled = PatcherTests._firmware_bundled

    def _probe(self, smu, runtime, **kwargs):
        with patch.object(collector, 'Bc250Smu', return_value=smu), \
                patch.object(collector, 'kernel_driver_loaded', return_value=False):
            return collector.probe(runtime)

    def test_firmware_bundled(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            smu = SramSmu(); smu.close = lambda: None
            self._firmware_bundled(smu)
            self.assertEqual(self._probe(smu, runtime), 'firmware-modern')
            self.assertEqual(smu.writes, [])
            self.assertEqual(json.loads((runtime / 'patch-state.json').read_text()),
                             {'state': 'ready', 'source': 'firmware'})
            # A repeated probe still knows the BIOS installed it.
            self.assertEqual(self._probe(smu, runtime), 'firmware-modern')

    def test_empty_slot_leaves_no_guard_so_service_can_install(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            smu = SramSmu(); smu.close = lambda: None
            smu.memory[patcher.HANDLER_REG:patcher.HANDLER_REG + 4] = bytes(4)
            self.assertEqual(self._probe(smu, runtime), 'none')
            self.assertFalse((runtime / 'patch-state.json').exists())
            self.assertEqual(smu.writes, [])

    def test_unknown_and_modified_payload(self):
        for setup in (lambda s: None, lambda s: self._firmware_install(s, 0x3AAC4, 176)):
            with tempfile.TemporaryDirectory() as directory:
                smu = SramSmu(); smu.close = lambda: None
                setup(smu)
                self.assertEqual(self._probe(smu, Path(directory)), 'unknown')
                self.assertEqual(smu.writes, [])

    def test_payload_installed_by_linux_this_boot_is_not_firmware(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            (runtime / 'patch-state.json').write_text('{"state": "ready"}')
            smu = SramSmu(); smu.close = lambda: None
            self._firmware_bundled(smu)
            self.assertEqual(self._probe(smu, runtime), 'linux')

    def test_refusals(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            smu = SramSmu(); smu.close = lambda: None
            (runtime / 'patch-state.json').write_text('{"state": "reading"}')
            with self.assertRaisesRegex(SmuError, 'did not finish'): self._probe(smu, runtime)
            (runtime / 'patch-state.json').unlink()
            with (runtime / 'smu.lock').open('a') as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                with self.assertRaisesRegex(SmuError, 'owns the SMU lock'): self._probe(smu, runtime)
            with patch.object(collector, 'kernel_driver_loaded', return_value=True), \
                    self.assertRaisesRegex(SmuError, 'kernel driver'):
                collector.probe(runtime)

    def test_main_probe_exit_codes_and_env_mode(self):
        for label, code in collector.PROBE_EXIT.items():
            with patch.object(collector, 'probe', return_value=label), \
                    patch.object(sys, 'argv', ['collector.py', '--probe']), patch.object(os, 'geteuid', return_value=0), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(collector.main(), code)
                self.assertEqual(out.getvalue().strip(), label)
        with patch.dict(os.environ, {'BC250_MEMORY_MODE': 'reader-only'}), \
                patch.object(collector, 'run', return_value=0) as run_mock, \
                patch.object(sys, 'argv', ['collector.py']), patch.object(os, 'geteuid', return_value=0):
            collector.main()
            self.assertEqual(run_mock.call_args.kwargs['mode'], 'reader-only')


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
        # Never depend on whether the host running the tests has the kernel
        # bc250_memory module loaded.
        driver = patch.object(collector, 'KERNEL_DRIVER', self.root / 'no-kernel-driver')
        driver.start()
        self.addCleanup(driver.stop)

    def reading(self, raw=None):
        smu = Mock()
        smu.read_chip.side_effect = raw if raw is not None else [0x2626, 0x2525, 0x2A2A, 0x2626, 0x2626, 0x2929, 0x2929, 0x2727]
        return collector.sample(smu)

    def test_packed_sample_unpacks_parity_bytes(self):
        smu = Mock()
        # parity 0 -> chips 0,2,4,6; parity 1 -> chips 1,3,5,7 (byte 0 first).
        smu.read_packed.side_effect = [0x29262A26, 0x27292625]
        result = collector.sample(smu, packed=True)
        smu.read_chip.assert_not_called()
        self.assertEqual(result['raw'], [0x26, 0x25, 0x2A, 0x26, 0x26, 0x29, 0x29, 0x27])
        self.assertEqual(result['chips_c'], [36, 34, 44, 36, 36, 42, 42, 38])
        self.assertEqual(read_memory(self._published(result))['hotspot_chip'], 2)

    def test_firmware_timeouts_are_reported_per_chip(self):
        smu = Mock()
        smu.read_packed.side_effect = [0x262626FF, 0x26FE2626]
        result = collector.sample(smu, packed=True)
        self.assertFalse(result['valid'])
        self.assertEqual(result['error'], 'firmware MR3 read timeout: chip 0 stage 1, chip 5 stage 2')
        raw = [0x2626] * 8
        raw[3] = 0xFFFFFFFE
        result = self.reading(raw)
        self.assertEqual((result['status'], result['error']),
                         ('invalid_reading', 'firmware MR3 read timeout: chip 3 stage 2'))

    def _published(self, result):
        path = self.root / 'packed.snapshot'
        collector.publish_snapshot(path, result)
        return path

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
        def one_sample(device, packed=False):
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

    READER_ONLY_FAILURE = {'state': 'failed', 'error': 'no known temperature payload installed '
                           '(Q3/5 handler 0x0003ab34); enable it in BIOS/EFI or use mode=auto'}

    def test_reader_only_rejection_is_retried_and_recognizes_firmware(self):
        guard = self.root/'patch-state.json'
        collector.atomic_json(guard, self.READER_ONLY_FAILURE)
        reading = self.reading()
        def one_sample(device, packed=False):
            collector.STOP.set()
            self.assertTrue(packed)
            return reading
        with patch.object(collector, 'check_platform'), patch.object(collector, 'Bc250Smu'), \
                patch.object(collector, 'ensure_patch', return_value='already_patched_modern') as prepare, \
                patch.object(collector, 'sample', side_effect=one_sample), self.assertLogs(level=logging.INFO):
            self.assertEqual(collector.run(self.root, mode='reader-only'), 0)
        self.assertTrue(prepare.call_args.kwargs['allow_install'])
        self.assertEqual(json.loads(guard.read_text()), {'state': 'ready', 'source': 'firmware'})

    def test_other_failures_are_not_retryable(self):
        for previous in ({'state': 'failed', 'error': 'queue 3 msg 0x05 timed out'},
                         {'state': 'reading'}, {'state': 'preparing'},
                         dict(self.READER_ONLY_FAILURE, source='firmware'),
                         {'state': 'failed', 'error': 'x ' + self.READER_ONLY_FAILURE['error']}):
            with self.subTest(previous=previous):
                self.assertFalse(collector.retryable_rejection(previous))
        self.assertTrue(collector.retryable_rejection(self.READER_ONLY_FAILURE))
        self.assertTrue(collector.retryable_rejection(collector.EMPTY_HANDLER_REJECTION))

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
        smu.read_packed.side_effect = SmuTimeout(3, 6)
        with patch.object(collector, 'check_platform'), \
                patch.object(collector, 'ensure_patch', return_value='patched'), \
                patch.object(collector, 'Bc250Smu', return_value=smu), self.assertLogs(level=logging.ERROR):
            self.assertEqual(collector.run(self.root), 1)
        self.assertEqual(smu.read_packed.call_count, 1)
        smu.read_chip.assert_not_called()
        smu.close.assert_called_once()
        self.assertEqual(json.loads((self.root/'patch-state.json').read_text())['state'], 'failed')
        self.assertFalse(read_memory(self.root/'telemetry')['valid'])

    def test_successful_start_then_clean_stop_preserves_ready_guard(self):
        reading = self.reading()
        def one_sample(smu, packed=False):
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

    def test_kernel_driver_loaded_skips_all_smu_access(self):
        (self.root / 'no-kernel-driver').mkdir()
        with patch.object(collector, 'check_platform') as platform, patch.object(collector, 'Bc250Smu') as smu:
            self.assertEqual(collector.run(self.root), 0)
        platform.assert_not_called()
        smu.assert_not_called()
        self.assertFalse((self.root / 'patch-state.json').exists())
        self.assertFalse((self.root / 'telemetry').exists())

    def test_kernel_driver_loaded_later_stops_polling(self):
        reading = self.reading()
        def driver_appears(smu, packed=False):
            (self.root / 'no-kernel-driver').mkdir()
            return reading
        with patch.object(collector, 'check_platform'), patch.object(collector, 'ensure_patch'), \
                patch.object(collector, 'Bc250Smu'), patch.object(collector, 'sample', side_effect=driver_appears) as sample:
            self.assertEqual(collector.run(self.root, interval=0.01), 0)
        self.assertEqual(sample.call_count, 1)
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
