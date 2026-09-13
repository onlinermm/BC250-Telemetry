"""Upstream SRAM/DMA primitives needed by the patcher."""
import ctypes
import os

from .errors import SmuError

PAGE = 4096

_LIBC = None


def _libc():
    global _LIBC
    if _LIBC is None:
        _LIBC = ctypes.CDLL("libc.so.6")
        _LIBC.mmap.restype = ctypes.c_void_p
        _LIBC.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]
        _LIBC.mlock.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        _LIBC.mlock.restype = ctypes.c_int
        _LIBC.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        _LIBC.munmap.restype = ctypes.c_int
    return _LIBC


def _phys_of(va):
    with open("/proc/self/pagemap", "rb") as f:
        f.seek((va // PAGE) * 8)
        e = __import__("struct").unpack("<Q", f.read(8))[0]
    pfn = e & ((1 << 55) - 1)
    return pfn * PAGE if (e & (1 << 63)) and pfn else None


def alloc_page():
    """Mlocked 4096-byte page for transfer targets; free with free_page()."""
    if os.sysconf('SC_PAGE_SIZE') != PAGE:
        raise RuntimeError('SMU DMA requires 4096-byte host pages')
    va = _libc().mmap(None, PAGE, 3, 0x22, -1, 0)
    if not va or va in (-1, 2 ** 64 - 1):
        raise RuntimeError("mmap failed")
    if _libc().mlock(ctypes.c_void_p(va), PAGE) != 0:
        _libc().munmap(ctypes.c_void_p(va), PAGE)
        raise RuntimeError("mlock failed")
    try:
        phys = _phys_of(va)
    except Exception:
        _libc().munmap(ctypes.c_void_p(va), PAGE)
        raise
    if phys is None:
        _libc().munmap(ctypes.c_void_p(va), PAGE)
        raise RuntimeError("pagemap failed")
    return va, phys


def free_page(va):
    """Release a page from alloc_page()."""
    _libc().munmap(ctypes.c_void_p(va), PAGE)


class PrimitiveMixin:
    """SMU SRAM reads and writes, serialized across their component commands."""

    def smu_read(self, addr: int, n: int = 1):
        """Read n 32-bit words from SMU-local addr (n <= 18)."""
        if not 1 <= n <= 18 or addr < 0 or addr % 4 or addr + n * 4 > 0x40000:
            raise ValueError('invalid SMU SRAM read range')
        va, phys = alloc_page()
        try:
            with self._lock:
                self.transfer_engine_sram_load(addr, n)
                # A successful mailbox status alone does not prove DMA wrote
                # the host page. Two distinct fills distinguish actual zero
                # SRAM from an untouched/partially written destination buffer.
                results = []
                for fill in (0xA5, 0x5A):
                    ctypes.memset(va, fill, n * 4)
                    self.transfer_engine_smu2dram(phys >> 32, phys & 0xFFFFFFFF, n)
                    results.append(bytes(ctypes.string_at(va, n * 4)))
                if results[0] != results[1]:
                    raise SmuError(f'SMU SRAM DMA verification failed at 0x{addr:08x}; destination was not consistently written')
                return results[0]
        finally:
            free_page(va)

    def smu_write32(self, addr: int, value: int):
        """Write one 32-bit word to SMU-local addr (gated)."""
        if type(addr) is not int or addr < 0 or addr % 4 or addr + 4 > 0x40000:
            raise ValueError('invalid SMU SRAM write address')
        if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
            raise ValueError('invalid SMU SRAM word')
        with self._lock:
            self.sec_set_write_ptr(addr)
            self.sec_write_through32(value)
