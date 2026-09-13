"""Only the upstream operations needed to patch SMU and read memory temperature."""
import threading

from .errors import SmuError, SmuRejected
from .mailbox import Bc250Mailbox
from .primitives import PrimitiveMixin
from .transport import Bc250PciTransport


class Bc250Smu(PrimitiveMixin):
    def __init__(self, timeout=5.0):
        self._lock = threading.RLock()
        self._transport = Bc250PciTransport()
        self._transport.open()
        self._queues = {
            2: Bc250Mailbox(self._transport, 2, 0x03B10528, 0x03B10564,
                           0x03B10998, timeout, self._lock, arg_count=6),
            # Q3 has one argument here. Writing six words clobbers Q4's arg0.
            3: Bc250Mailbox(self._transport, 3, 0x03B10A20, 0x03B10A80,
                           0x03B10A88, timeout, self._lock, arg_count=1),
        }

    def close(self):
        self._transport.close()

    def send_message(self, queue_id, msg_id, args=(), check_status=True):
        with self._lock:
            if any(q.failed for q in self._queues.values()):
                raise SmuError('SMU communication failed; refusing further commands this run')
            status, value = self._queues[queue_id].send(msg_id, args)
            if check_status and status != Bc250Mailbox.SMU_RETURN_OK:
                raise SmuRejected(queue_id, msg_id, status)
            return status, value

    def alive(self):
        _, value = self.send_message(3, 1, [0x5EED0000])
        return value == 0x5EED0001

    def secure_access_enabled(self):
        status, _ = self.sec_smn_read32(0x0005A870)
        if status == Bc250Mailbox.SMU_RETURN_REJECTED_PREREQ:
            return False
        if status != Bc250Mailbox.SMU_RETURN_OK:
            raise SmuRejected(3, 0x2A, status)
        return True

    def sec_set_write_ptr(self, addr):
        return self.send_message(3, 0x28, [addr])

    def sec_write_through32(self, value):
        return self.send_message(3, 0x29, [value])

    def sec_smn_read32(self, addr):
        return self.send_message(3, 0x2A, [addr], check_status=False)

    def transfer_engine_sram_load(self, src, words):
        return self.send_message(2, 0x0A, [0x1F, 0, src, words])

    def transfer_engine_smu2dram(self, hi, lo, words):
        return self.send_message(2, 0x0A, [0x14, hi, lo, words, 0, 0])

    def transfer_engine_dram2smu(self, hi, lo, words, key):
        return self.send_message(2, 0x0A, [0x23, hi, lo, words, 0, key])

    def q2_0x23_append(self, args):
        # Preserve the upstream overflow primitive's unchecked status: the
        # effect is verified by DMA readback and the strict debug-gate probe.
        return self.send_message(2, 0x23, args, check_status=False)

    def read_chip(self, chip):
        if type(chip) is not int or not 0 <= chip < 8:
            raise ValueError('chip must be in 0..7')
        return self.send_message(3, 5, [chip])[1]
