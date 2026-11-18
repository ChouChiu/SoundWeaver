CFLAGS += -Wall -fPIC -O3 -g -std=c99 -I./include -I../libavs3_common

C_SOURCES = $(wildcard *.c) $(wildcard */*.c)

OBJS = $(subst .c,.o,$(C_SOURCES))

SHARED = libavs3Encoder.so

STATIC = libavs3Encoder.a

.PHONY: all clean

all: $(SHARED) $(STATIC)

$(SHARED): $(OBJS)
	$(CC) -shared -o $@ $^

$(STATIC): $(OBJS)
	ar crus $@ $^
	ranlib $@


-include depends
depends: $(C_SOURCES)
	$(CC) -M $(CFLAGS) $^ > $@

install: all
	@echo nothing to install

clean:
	@rm -f $(OBJS) .*.dep $(SHARED) $(STATIC)
