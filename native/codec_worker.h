#ifndef SOUNDWEAVER_CODEC_WORKER_H
#define SOUNDWEAVER_CODEC_WORKER_H

#include <stdint.h>
#include <stdio.h>

#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif

static void binary_pipes(void)
{
#ifdef _WIN32
    _setmode(_fileno(stdin), _O_BINARY);
    _setmode(_fileno(stdout), _O_BINARY);
#endif
}

static int read_u32(uint32_t *value)
{
    unsigned char b[4];
    if (fread(b, 1, sizeof(b), stdin) != sizeof(b))
        return 0;
    *value = (uint32_t)b[0] | (uint32_t)b[1] << 8 |
             (uint32_t)b[2] << 16 | (uint32_t)b[3] << 24;
    return 1;
}

static int write_u32(uint32_t value)
{
    unsigned char b[] = {value, value >> 8, value >> 16, value >> 24};
    return fwrite(b, 1, sizeof(b), stdout) == sizeof(b);
}

#endif
