CFLAGS += -Wall -fPIC -O3 -g -std=c99

C_SOURCES = $(wildcard *.c)

OBJS = $(subst .c,.o,$(C_SOURCES))

SHARED = libavs3_common.so

STATIC = libavs3_common.a

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
