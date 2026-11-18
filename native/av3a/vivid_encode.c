/* Standalone CLI entry for Audio Vivid (AVS3-P3) encoder (`vivid_encode`). */
#define _POSIX_C_SOURCE 200809L
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

extern int avs3_encoder(int argc, char *argv[]);

int main(int argc, char *argv[]) {
    if (!getenv("AVS3_MODEL_PATH")) {
        char exe[PATH_MAX];
        ssize_t n = readlink("/proc/self/exe", exe, sizeof(exe) - 1);
        if (n > 0) {
            exe[n] = '\0';
            char *slash = strrchr(exe, '/');
            if (slash && (size_t)(slash - exe) + sizeof("/model.bin") < sizeof(exe)) {
                strcpy(slash + 1, "model.bin");
                setenv("AVS3_MODEL_PATH", exe, 0);
            }
        }
    }
    return avs3_encoder(argc, argv);
}
