extern void queue_write_status_qid(int qid, unsigned int status);
extern void queue_store_word_head_for_qid(int qid, unsigned int data);
extern unsigned int queue_read_head_for_qid(int qid);

extern unsigned int umc_read(int zero,unsigned int addr,int size);
extern void umc_write(int zero,unsigned int addr,unsigned int data,int size);

#define umc_read32(umc, addr)        umc_read(0, (addr) | ((umc)<<20), 2)
#define umc_write32(umc, addr, val)  umc_write(0, (addr) | ((umc)<<20), (val), 2)

#define SMU_FW_OK   0x01
#define SMU_FW_ERR  0xff

#define REG_WAIT_MAX    100000

#define WAIT_COND(cond, res) \
    while(1) { \
        static unsigned int c = 0; \
        if (cond) { c=0; res=0; break; }\
        if (++c >= REG_WAIT_MAX) { c=0; res=1; break; }\
    }

unsigned int regdump_arr[8][4] = { 0 };

void reg_dump(int umc_id) {
    regdump_arr[umc_id][0] = umc_read32(umc_id, 0x53a1c);
    regdump_arr[umc_id][1] = umc_read32(umc_id, 0x53a20);
    regdump_arr[umc_id][2] = umc_read32(umc_id, 0x53a24);
    regdump_arr[umc_id][3] = umc_read32(umc_id, 0x53a2c);
}
/**
 * @brief Read temperature code from individual GDDR6 memory chip.
 *
 * Reads the temperature register (MR3) from UMC selected by UMC ID.
 *
 * @param qid Queue ID supplied by the SMU message dispatcher.
 *
 * @warning This function must only be invoked through the SMU mailbox
 *          message dispatch mechanism. Direct calls from other firmware
 *          code are not supported.
 *
 * @retval SMU_FW_OK  The request was processed.
 * @retval SMU_FW_ERR Invalid UMC ID; the response is not produced.
 *
 * @note read timeouts are encoded in the response byte:
 *       0xFFFFFFFF for Stage 1 and 0xFFFFFFFE for Stage 2.
 */
void umc_read_temp_per_chip(int qid) {
    unsigned int umc_id;
    unsigned int res = 0;
    int wres = 0;

    umc_id = queue_read_head_for_qid(qid);
    if(umc_id > 7) {
        queue_write_status_qid(qid, SMU_FW_ERR);
        return;
    };

    umc_write32(umc_id, 0x53a20, 0);
    umc_write32(umc_id, 0x53a2c, 0);
    umc_write32(umc_id, 0x53a24, 2);
    umc_write32(umc_id, 0x53a1c, 5);

    WAIT_COND(5 == umc_read32(umc_id, 0x53a20), wres);
    if(wres) {
        reg_dump(umc_id);
        res = -1;
        goto exit;
    }

    WAIT_COND(0x1234 != umc_read32(umc_id, 0x53a2c), wres);
    if(wres) {
        reg_dump(umc_id);
        res = -2;
        goto exit;
    }

    res = umc_read32(umc_id, 0x53a2c);
exit:
    queue_store_word_head_for_qid(qid, res);
    queue_write_status_qid(qid, SMU_FW_OK);
}
/**
 * @brief Read temperature codes from four GDDR6 memory chips.
 *
 * Reads the temperature register (MR3) from four UMCs selected by
 * the specified parity and packs the results into a 32-bit response.
 *
 * @param qid Queue ID supplied by the SMU message dispatcher.
 *
 * @details
 * The request payload specifies the UMC parity:
 * - 0: UMCs 0, 2, 4, 6
 * - 1: UMCs 1, 3, 5, 7
 *
 * Each temperature code occupies one byte in the response.
 *
 * @warning This function must only be invoked through the SMU mailbox
 *          message dispatch mechanism. Direct calls from other firmware
 *          code are not supported.
 *
 * @retval SMU_FW_OK  The request was processed.
 * @retval SMU_FW_ERR Invalid parity; the response is not produced.
 *
 * @note Individual UMC read timeouts are encoded in the corresponding
 *       response byte: 0xFF for Stage 1 and 0xFE for Stage 2.
 */
void umc_read_temp_packed(int qid) {
    unsigned int res = 0;
    unsigned int parity;
    unsigned int umc_id;
    int wres;

    parity = queue_read_head_for_qid(qid);
    if(parity > 1) {
        queue_write_status_qid(qid, SMU_FW_ERR);
        return;
    };
    /*
    * Packed result:
    * byte 0 -> UMC parity + 0
    * byte 1 -> UMC parity + 2
    * byte 2 -> UMC parity + 4
    * byte 3 -> UMC parity + 6
    */
    for(int i = 0; i < 8; i+=2) {
        umc_id = i | parity;
        umc_write32(umc_id, 0x53a20, 0);
        umc_write32(umc_id, 0x53a2c, 0);
        umc_write32(umc_id, 0x53a24, 2);
        umc_write32(umc_id, 0x53a1c, 5);

        WAIT_COND(5 == umc_read32(umc_id, 0x53a20), wres);
        if(wres) {
            res |= 0xFFu<<(i<<2); // set res -1 mean wait stage 1 timeout
            reg_dump(umc_id);
            continue;
        }
        WAIT_COND(0x1234 != umc_read32(umc_id, 0x53a2c), wres);
        if(wres) {
            res |= 0xFEu<<(i<<2); // set res -2 mean wait stage 2 timeout
            reg_dump(umc_id);
            continue;
        }
        res |= (umc_read32(umc_id, 0x53a2c) & 0xFFu) << (i<<2);
    }

    queue_store_word_head_for_qid(qid, res);
    queue_write_status_qid(qid, SMU_FW_OK);
}
/**
 * @brief Read a previously captured UMC register dump.
 *
 * Retrieves a register value from the dump using
 * the specified UMC ID and register ID.
 *
 * @param qid Queue ID supplied by the SMU message dispatcher.
 *
 * @warning This function must only be invoked through the SMU mailbox
 *          message dispatch mechanism. Direct calls from other firmware
 *          code are not supported.
 *
 * @details
 * Register IDs map to the following UMC registers:
 * 0: 0x53a1c
 * 1: 0x53a20
 * 2: 0x53a24
 * 3: 0x53a2c
 *
 * @retval SMU_FW_OK  The request was processed.
 * @retval SMU_FW_ERR The UMC ID or register ID is invalid. No register value is returned.
 *
 */
void regdump_read(int qid) {
    unsigned int buf, umc, reg, res;

    buf = queue_read_head_for_qid(qid);
    reg = buf & 0xFFu;
    umc = (buf>>8) & 0xFFu;

    if(reg > 3 || umc > 7) {
        queue_write_status_qid(qid, SMU_FW_ERR);
        return;
    }

    res = regdump_arr[umc][reg];
    regdump_arr[umc][reg] = 0xffffffff;
    queue_store_word_head_for_qid(qid, res);
    queue_write_status_qid(qid, SMU_FW_OK);
}