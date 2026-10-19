"""Install only the pinned P3.0 payload, then verify it before switching Q3 hooks."""
import hashlib
import logging
from pathlib import Path

from bc250_smu.errors import SmuError
from unlock import unlock

PAYLOAD_START = 0x3AA9C
PAYLOAD_END = 0x3E000
# SMU firmware version word of the BC-250 P3.0 BIOS the payload links against.
SMU_VERSION_ADDR = 0x0
SMU_VERSION = 0x00580600
# Q3 handler slots: 8 bytes each, a handler word followed by a flags word
# that is never written. Q3/5 reads one chip, Q3/6 four chips packed, Q3/7
# returns the register dump captured on a firmware read timeout.
HANDLER_REG = 0x748C
PACKED_HANDLER_REG = 0x7494
REGDUMP_HANDLER_REG = 0x749C
# Bundled upstream payload (main.c at 4a2221d): code, then zeroed BSS.
HANDLER = 0x3AB34
HOOKS = {HANDLER_REG: HANDLER, PACKED_HANDLER_REG: 0x3AC34, REGDUMP_HANDLER_REG: 0x3AD54}
PAYLOAD_SHA256 = '32ab902bb07a5a00cdb54f8686b8944fed8cb30abf394c395d5e8d421f756529'
PAYLOAD_SIZE = 764
BSS_SIZE = 144
# Payloads this collector can read: (name, Q3/5 handler, code bytes from
# PAYLOAD_START, SHA-256, other hooks). A handler is accepted only when the
# SRAM code matches the pinned size and hash and every listed hook points at
# its entry. Only `modern` is bundled for installation; the others are older
# builds a BIOS/EFI may install, identified by hash and never installed here.
# Mutable BSS after the code changes after requests, so it is not hashed.
KNOWN_PAYLOADS = (
    ('modern', HANDLER, PAYLOAD_SIZE, PAYLOAD_SHA256,
     {reg: entry for reg, entry in HOOKS.items() if reg != HANDLER_REG}),
    ('original', 0x3AAC4, 176, 'b31908460e932a615d9eafb6b3112e6448994f9f6fa656d1d80a8616ac1df4df', {}),
    ('bounded', 0x3AAC8, 212, '4ba2528c0441b6fd72c1e255af6dd14d16fbc0f831f9a75275a63a2e1233f328', {}),
)
# Payloads that implement Q3/6 packed reads and Q3/7 register dumps.
PACKED_PAYLOADS = frozenset({'modern'})
MODES = ('auto', 'reader-only')
PAYLOAD_FILE = Path(__file__).resolve().parent / 'payload' / 'SMUPayload.bin'


def load_payload(path=PAYLOAD_FILE):
    """Bundled code followed by its zero-initialized BSS, ready to upload."""
    data = Path(path).read_bytes()
    if len(data) != PAYLOAD_SIZE or hashlib.sha256(data).hexdigest() != PAYLOAD_SHA256:
        raise SmuError('payload size/hash does not match the bundled P3.0 build')
    return data + bytes(BSS_SIZE)


def check_platform(dmi=Path('/sys/class/dmi/id'), pci=Path('/sys/bus/pci/devices'), strict_bios=True):
    # DMI is a coarse compatibility gate, not a proof of the SMU firmware
    # contents. Custom firmware using the same DMI strings is unsupported.
    # Reader-only mode relaxes the BIOS string (custom BIOS builds may rename
    # it); it relies on identify_installed() to prove the live payload.
    board = (dmi / 'board_name').read_text().strip()
    bios = (dmi / 'bios_version').read_text().strip()
    board_id = board.upper().replace('-', '').replace(' ', '')
    if board_id not in ('BC250', 'AMDBC250') or (strict_bios and bios.upper() not in ('P3.0', 'P3.00')):
        raise SmuError(f'unsupported board/BIOS: {board!r} / {bios!r}; expected BC-250 P3.0')
    root_vendor = (pci / '0000:00:00.0' / 'vendor').read_text().strip().lower()
    if root_vendor != '0x1022':
        raise SmuError('PCI root is not AMD')
    for device in pci.iterdir():
        if ((device / 'vendor').read_text().strip().lower() == '0x1002'
                and (device / 'device').read_text().strip().lower() == '0x13fe'):
            return
    raise SmuError('BC-250 GPU (1002:13fe) not found')


def read_bytes(smu, start, size):
    return b''.join(smu.smu_read(addr, min(18, (start + size - addr) // 4))
                    for addr in range(start, start + size, 72))


def identify_installed(smu, current):
    """Name of the known payload behind Q3/5 handler `current`, else None.

    Read-only. A known handler address with different SRAM bytes is an error:
    never run (or overwrite) code we cannot identify.
    """
    for name, handler, size, sha256, hooks in KNOWN_PAYLOADS:
        if current == handler:
            if hashlib.sha256(read_bytes(smu, PAYLOAD_START, size)).hexdigest() != sha256:
                raise SmuError('Q3/5 points at an unexpected payload; reboot before retrying')
            for reg, entry in hooks.items():
                hook = int.from_bytes(smu.smu_read(reg), 'little')
                if hook != entry:
                    raise SmuError(f'{name} payload hook 0x{reg:04x} is 0x{hook:08x}, '
                                   f'expected 0x{entry:08x}; reboot before retrying')
            return name
    return None


def ensure_patch(smu, payload, allow_install=True, mode='auto'):
    """Returns 'patched' or 'already_patched_<name>' for a known installed payload.

    mode='reader-only' never unlocks or writes: it only accepts a payload that
    firmware (or an earlier run) already installed.
    """
    if mode not in MODES:
        raise SmuError(f'unknown memory mode: {mode!r}')
    if not smu.alive():
        raise SmuError('SMU failed its initial probe')
    current = int.from_bytes(smu.smu_read(HANDLER_REG), 'little')
    known = identify_installed(smu, current)
    if known:
        return f'already_patched_{known}'
    if mode == 'reader-only':
        raise SmuError(f'no known temperature payload installed (Q3/5 handler 0x{current:08x}); '
                       'enable it in BIOS/EFI or use mode=auto')
    if not allow_install:
        raise SmuError('SMU patch changed during this boot; reboot before retrying')
    if len(payload) != PAYLOAD_SIZE + BSS_SIZE or PAYLOAD_START + len(payload) > PAYLOAD_END:
        raise SmuError('payload does not fit the reserved SMU region')
    version = int.from_bytes(smu.smu_read(SMU_VERSION_ADDR), 'little')
    if version != SMU_VERSION:
        raise SmuError(f'unsupported SMU firmware 0x{version:08x}; expected 0x{SMU_VERSION:08x}')
    # Slots may be empty before installation (observed on P3.00 with verified
    # SRAM transfers). Reject pointers into our region (a partial or foreign
    # patch) and anything that is not a plausible firmware handler.
    previous = {reg: int.from_bytes(smu.smu_read(reg), 'little') for reg in HOOKS}
    for reg, value in previous.items():
        if value != 0 and (not 0x800 <= value < 0x40000 or PAYLOAD_START <= value < PAYLOAD_END):
            raise SmuError(f'unexpected original Q3/{(reg - 0x7464) // 8} handler: 0x{value:08x}')
    # Code or data there may belong to another SMU patch that does not use
    # these slots. Like the BIOS installer, only fill an entirely empty region.
    if any(read_bytes(smu, PAYLOAD_START, len(payload))):
        raise SmuError(f'SMU region 0x{PAYLOAD_START:05x}-0x{PAYLOAD_START + len(payload):05x} '
                       'is not empty; refusing to overwrite it')

    logging.info('Preparing SMU patch; previous Q3/5,6,7 handlers: %s',
                 ', '.join(f'0x{value:08x}' for value in previous.values()))
    unlock(smu)
    logging.info('SMU access ready; uploading and verifying payload')
    for offset in range(0, len(payload), 4):
        smu.smu_write32(PAYLOAD_START + offset, int.from_bytes(payload[offset:offset + 4], 'little'))
    if read_bytes(smu, PAYLOAD_START, len(payload)) != payload:
        raise SmuError('payload readback mismatch; handlers were not switched')
    # Q3/5 last: the collector identifies the payload by it, so it only
    # appears once the auxiliary hooks are in place.
    for reg in (PACKED_HANDLER_REG, REGDUMP_HANDLER_REG, HANDLER_REG):
        smu.smu_write32(reg, HOOKS[reg])
        if int.from_bytes(smu.smu_read(reg), 'little') != HOOKS[reg]:
            raise SmuError(f'handler 0x{reg:04x} readback mismatch')
    logging.info('SMU payload and Q3/5, Q3/6, Q3/7 handlers verified')
    return 'patched'
