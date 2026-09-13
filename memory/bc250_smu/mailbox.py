import threading
import time

from .errors import SmuTimeout
from .transport import Bc250PciTransport


class Bc250Mailbox:
    """One mailbox (cmd/rsp/arg register triple) with a thread lock."""

    SMU_RETURN_OK = 0x01
    SMU_RETURN_FAILED = 0xFF
    SMU_RETURN_UNKNOWN_CMD = 0xFE
    SMU_RETURN_REJECTED_PREREQ = 0xFD
    SMU_RETURN_REJECTED_BUSY = 0xFC
    DONE = {SMU_RETURN_OK, SMU_RETURN_FAILED, SMU_RETURN_UNKNOWN_CMD, SMU_RETURN_REJECTED_PREREQ, SMU_RETURN_REJECTED_BUSY}

    def __init__(self, transport: Bc250PciTransport, queue: int, cmd_addr: int, rsp_addr: int, arg_addr: int, timeout: float = 5.0, lock=None, arg_count=1) -> None:
        self._transport = transport
        self._queue = queue
        self._cmd_addr = cmd_addr
        self._rsp_addr = rsp_addr
        self._arg_addr = arg_addr
        self._timeout = timeout
        self._lock = lock or threading.Lock()
        self._arg_count = arg_count
        self.failed = False

    def send(self, msg_id: int, args=()):
        """Validate all arguments before touching the mailbox."""
        args = tuple(args)
        if len(args) > self._arg_count:
            raise ValueError('too many mailbox arguments')
        if any(type(v) is not int or not 0 <= v <= 0xFFFFFFFF for v in (msg_id, *args)):
            raise ValueError('mailbox values must be unsigned 32-bit integers')
        with self._lock:
            if self.failed:
                raise RuntimeError('mailbox failed; further commands disabled')
            try:
                self._transport.write_smu_reg(self._rsp_addr, 0)
                for i in range(self._arg_count):
                    self._transport.write_smu_reg(self._arg_addr + 4 * i, args[i] if i < len(args) else 0)
                self._transport.write_smu_reg(self._cmd_addr, msg_id)
                return self._wait_done(msg_id)
            except Exception:
                self.failed = True
                raise

    def _wait_done(self, msg_id):
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            status = self._transport.read_smu_reg(self._rsp_addr)
            if status in self.DONE:
                return status, self._transport.read_smu_reg(self._arg_addr)
            time.sleep(0.001)
        raise SmuTimeout(self._queue, msg_id)
