CC=./xtensa-esp32-elf-gcc-5.2.0/bin/xtensa-esp32-elf-gcc
OBJCOPY=./xtensa-esp32-elf-gcc-5.2.0/bin/xtensa-esp32-elf-objcopy

CFLAGS = -mlongcalls \
           -mtext-section-literals \
          -Wall \
          -nostdlib \
          -Os \

LDFLAGS = -nostdlib \
          -T smu3.ld \
          -lgcc \


DEPS=

OBJS= main.o

NAME=SMUPayload

ELF=$(NAME).elf
BIN=$(NAME).bin

all: $(BIN)


%.o: %.c $(DEPS)
	$(CC) -c -o $@ $< $(CFLAGS)

%.o: %.S $(DEPS)
	$(CC) -c -o $@ $< $(CFLAGS)

$(NAME).elf: $(OBJS) smu3.ld
	$(CC) -o $@ $(OBJS) $(LDFLAGS)

%.bin: %.elf
	$(OBJCOPY) -O binary $< $@

.PHONY: clean
clean:
	rm -rf $(ELF) $(BIN) $(OBJS)