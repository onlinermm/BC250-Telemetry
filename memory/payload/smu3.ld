PROVIDE(queue_write_status_qid  = 0xfa8);
PROVIDE(queue_read_head_for_qid = 0xffc);
PROVIDE(queue_store_word_head_for_qid  = 0xfe4);

PROVIDE(umc_read = 0x29f8);
PROVIDE(umc_write = 0x2a74);

MEMORY
{
    VECTOR (rx) : ORIGIN = 0x800,    LENGTH = 0x400
    EXTRA_SRAM (rwx) : ORIGIN = 0x0003AA9C, LENGTH = 0x3564
}

SECTIONS
{
    .text :
    {
        . = ALIGN(4);
        *(.literal)
        *(.text)
        *(.text.*)
        . = ALIGN(4);
    } > EXTRA_SRAM

    .rodata :
    {
        . = ALIGN(4);
        *(.rodata)
        *(.rodata.*)
        . = ALIGN(4);
    } > EXTRA_SRAM

    .data :
    {
        . = ALIGN(4);
        *(.data)
        *(.data.*)
        . = ALIGN(4);
    } > EXTRA_SRAM

    .bss :
    {
        . = ALIGN(4);
        *(.bss)
        *(.bss.*)
        *(COMMON)
        . = ALIGN(4);
    } > EXTRA_SRAM
}