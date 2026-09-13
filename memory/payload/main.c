extern void queue_write_status_qid(int qid, unsigned int status);
extern void queue_store_word_head_for_qid(int qid, unsigned int data);
extern unsigned int queue_read_head_for_qid(int qid);

extern unsigned int umc_read(int zero,unsigned int addr,int size);
extern void umc_write(int zero,unsigned int addr,unsigned int data,int size);

void umc_read_temp_per_chip(int qid) {

    int umc_id = queue_read_head_for_qid(qid);

    umc_write(0,(0x53a24 | (umc_id << 20)),2,2);
    umc_write(0,(0x53a1c | (umc_id << 20)),5,2);
    
    while (((int)5 != umc_read(0, (0x53a20 | (umc_id << 20)), 2))) {};
    while (((int)0x1234 == umc_read(0, (0x53a2c | (umc_id << 20)), 2))) {};
    
    queue_store_word_head_for_qid(qid, umc_read(0, (0x53a2c | (umc_id << 20)), 2));
    queue_write_status_qid(qid, 0x01);   
}