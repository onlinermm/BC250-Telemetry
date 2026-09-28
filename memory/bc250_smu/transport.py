import contextlib
import fcntl
import os
import struct
import threading


class Bc250PciTransport:
    """PCI config-space SMN window of the root device. Requires root."""

    def __init__(self, bdf: str = "0000:00:00.0"):
        self._config_path = f"/sys/bus/pci/devices/{bdf}/config"
        self._fd = None
        # flock is owned by the open file, so it does not separate our threads.
        self._thread_lock = threading.Lock()

    def open(self) -> None:
        if self._fd is None:
            if os.geteuid() != 0:
                raise PermissionError("SMN window needs root")
            self._fd = os.open(self._config_path, os.O_RDWR | os.O_CLOEXEC)

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    @contextlib.contextmanager
    def _window(self):
        """Hold the SMN window for one address/value pair.

        The window is shared hardware state. Other BC-250 SMU tools
        (bc250_smu_oc, cyan-skillfish-governor-smu) take an exclusive flock on
        this config file, so taking it here keeps their writes from landing
        between our address and our value. Tools that do not lock are not
        excluded.

        Those tools lock each config access separately, so our pair can still
        land between their address and their value. Restoring the address
        they selected keeps their next access on their own register.
        """
        with self._thread_lock:
            fcntl.flock(self._fd, fcntl.LOCK_EX)
            try:
                selected = self._read_word(0xB8)
                try:
                    yield
                finally:
                    self._write_word(selected, 0xB8)
            finally:
                fcntl.flock(self._fd, fcntl.LOCK_UN)

    def read_smu_reg(self, reg: int) -> int:
        with self._window():
            self._write_word(reg, 0xB8)
            return self._read_word(0xBC)

    def write_smu_reg(self, reg: int, value: int) -> None:
        with self._window():
            self._write_word(reg, 0xB8)
            self._write_word(value, 0xBC)

    def _read_word(self, offset):
        data = os.pread(self._fd, 4, offset)
        if len(data) != 4:
            raise OSError('short PCI config read')
        return struct.unpack("<I", data)[0]

    def _write_word(self, value, offset):
        if os.pwrite(self._fd, struct.pack('<I', value), offset) != 4:
            raise OSError('short PCI config write')
