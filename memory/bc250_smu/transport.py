import os
import struct


class Bc250PciTransport:
    """PCI config-space SMN window of the root device. Requires root."""

    def __init__(self, bdf: str = "0000:00:00.0"):
        self._config_path = f"/sys/bus/pci/devices/{bdf}/config"
        self._fd = None

    def open(self) -> None:
        if self._fd is None:
            if os.geteuid() != 0:
                raise PermissionError("SMN window needs root")
            self._fd = os.open(self._config_path, os.O_RDWR | os.O_CLOEXEC)

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def read_smu_reg(self, reg: int) -> int:
        self._write_word(reg, 0xB8)
        data = os.pread(self._fd, 4, 0xBC)
        if len(data) != 4:
            raise OSError('short PCI config read')
        return struct.unpack("<I", data)[0]

    def write_smu_reg(self, reg: int, value: int) -> None:
        self._write_word(reg, 0xB8)
        self._write_word(value, 0xBC)

    def _write_word(self, value, offset):
        if os.pwrite(self._fd, struct.pack('<I', value), offset) != 4:
            raise OSError('short PCI config write')
