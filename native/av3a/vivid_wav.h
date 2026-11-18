#ifndef VIVID_WAV_H
#define VIVID_WAV_H

#include <stdint.h>
#include <stdio.h>
#include <string.h>

enum { VIVID_WAV_HEADER_SIZE = 80 };

static void vivid_wav_le(uint8_t *p, uint64_t value, unsigned count) {
    for (unsigned i = 0; i < count; i++) p[i] = (uint8_t)(value >> (8 * i));
}

static int vivid_wav_is_rf64(uint64_t bytes) {
    // Core Audio miscounts the last frames when the total RIFF file crosses
    // 4 GiB even if its file-size-minus-eight field still fits in UInt32.
    return bytes > UINT32_MAX - VIVID_WAV_HEADER_SIZE;
}

// Reserve the ds64 footprint as JUNK so promoting RIFF to RF64 never moves PCM.
// All lengths exclude padding; F32 interleaved PCM is always even-sized.
static int vivid_wav_header(FILE *file, int rate, int channels, uint64_t bytes) {
    if (rate <= 0 || channels <= 0 || channels > 24 ||
        bytes % ((uint64_t)channels * 4) || bytes > INT64_MAX - VIVID_WAV_HEADER_SIZE) return -1;
    uint8_t h[VIVID_WAV_HEADER_SIZE] = {0};
    int rf64 = vivid_wav_is_rf64(bytes);
    memcpy(h, rf64 ? "RF64" : "RIFF", 4);
    vivid_wav_le(h + 4, rf64 ? UINT32_MAX : bytes + sizeof(h) - 8, 4);
    memcpy(h + 8, "WAVE", 4);
    memcpy(h + 12, rf64 ? "ds64" : "JUNK", 4);
    vivid_wav_le(h + 16, 28, 4);
    if (rf64) {
        vivid_wav_le(h + 20, bytes + sizeof(h) - 8, 8);
        vivid_wav_le(h + 28, bytes, 8);
        vivid_wav_le(h + 36, bytes / ((uint64_t)channels * 4), 8);
    }
    memcpy(h + 48, "fmt ", 4);
    vivid_wav_le(h + 52, 16, 4);
    vivid_wav_le(h + 56, 3, 2); // IEEE float
    vivid_wav_le(h + 58, channels, 2);
    vivid_wav_le(h + 60, rate, 4);
    vivid_wav_le(h + 64, (uint64_t)rate * channels * 4, 4);
    vivid_wav_le(h + 68, channels * 4, 2);
    vivid_wav_le(h + 70, 32, 2);
    memcpy(h + 72, "data", 4);
    vivid_wav_le(h + 76, rf64 ? UINT32_MAX : bytes, 4);
    if (fseeko(file, 0, SEEK_SET) != 0 || fwrite(h, 1, sizeof(h), file) != sizeof(h)) return -1;
    return 0;
}

#endif
