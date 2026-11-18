//
// Created by desert_fox on 2022/7/18.
//
#include "bw64.hpp"
#include <string>
#include "avs3_audio.h"
#include <vector>
#include <cmath>
#include <cassert>

#ifndef M_PI
#define M_PI 3.141592653
#endif

static inline std::vector<float> polar2Cart(std::vector<float> &polar) {
    float x = std::sin(-M_PI * polar[0] / 180.0) * std::cos(M_PI * polar[1] / 180.0) * polar[2];
    float y = std::cos(-M_PI * polar[0] / 180.0) * std::cos(M_PI * polar[1] / 180.0) * polar[2];
    float z = std::sin(M_PI * polar[1] / 180.0) * polar[2];
    return {-x, z, y};
}

int main(int argc, char **argv) {

    std::string input_wave_path{};
    std::string output_wav_path{};

    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "-i") == 0) {
            if (i + 1 > argc) {
                std::cerr << "no input file" << std::endl;
                exit(-1);
            }
            input_wave_path = argv[i + 1];
            i++;
            std::cout << "input_wav path: " << input_wave_path << std::endl;
        } else if (strcmp(argv[i], "-o") == 0) {
            if (i + 1 > argc) {
                std::cerr << "no output file" << std::endl;
                exit(-1);
            }
            output_wav_path = argv[i + 1];
            i++;
            std::cout << "output_wav path: " << output_wav_path << std::endl;
        } else {
            std::cerr << "invalid parameter" << std::endl;
        }
    }

    auto wav = bw64::readFile(input_wave_path);
    int channels = wav->channels();
    uint32_t sampleRate = wav->sampleRate();
    uint64_t numFrames = wav->numberOfFrames();
    uint32_t bitDepth = wav->bitDepth();
    if (channels != 10) {
        std::cerr << "input file is not 5.1.4H";
        exit(-1);
    }

    audio_context *ctx;
    std::vector<int> sourceID;

    const int blockSize = 1024;
    auto ret = audio_create_context(&ctx, AMBISONIC_SEVENTH_ORDER, blockSize, sampleRate);
    assert(ret == SUCCESS);
    ret = audio_initialize_context(ctx);
    assert(ret == SUCCESS);
    ret = audio_commit_scene(ctx);
    assert(ret == SUCCESS);
    std::vector<std::vector<float>> sourcePosition = {
            {30.0f,   0.0f,  1.0f},
            {-30.0f,  0.0f,  1.0f},
            {0.0f,    0.0f,  1.0f},
            {0.0f,    0.0f,  1.0f},
            {110.0f,  0.0f,  1.0f},
            {-110.0f, 0.0f,  1.0f},
            {30.0f,   30.0f, 1.0f},
            {-30.0f,  30.0f, 1.0f},
            {110.0f,  30.0f, 1.0f},
            {-110.0f, 30.0f, 1.0f}
    };

    for (auto &pos : sourcePosition) {
        int id;
        auto xyz = polar2Cart(pos);
        ret = audio_add_source(ctx, SOURCE_SPATIALIZE, xyz.data(), &id);
        assert(ret == SUCCESS);
        sourceID.emplace_back(id);
    }
    ret = audio_update_scene(ctx);
    assert(ret == SUCCESS);

    std::vector<float> file_data(channels * numFrames, 0);
    std::vector<float> buffer(blockSize, 0.0f);
    std::vector<float> out_data(2 * numFrames, .0f);

    wav->read(file_data.data(), numFrames);

    uint64_t readIndex = 0;
    while (readIndex < numFrames) {
        uint64_t currentFrame = readIndex + blockSize > numFrames ? numFrames - readIndex : blockSize;
        for (int i = 0; i < channels; ++i) {
            int id = sourceID[i];
            std::fill(buffer.begin(), buffer.end(), 0.0f);
            for (int j = 0; j < currentFrame; ++j) {
                buffer[j] = file_data[readIndex * channels + j * channels + i];
            }
            ret = audio_submit_source_buffer(ctx, id, buffer.data(), blockSize);
            assert(ret == SUCCESS);
        }
        ret = audio_update_scene(ctx);
        assert(ret == SUCCESS);
        float *out_buffer = out_data.data() + readIndex * 2;
        ret = audio_get_interleaved_binaural_buffer(ctx, out_buffer, blockSize);
        assert(ret == SUCCESS);
        readIndex += currentFrame;
    }

    auto writer = bw64::writeFile(output_wav_path, 2, sampleRate, bitDepth);
    writer->write(out_data.data(), numFrames);

    ret = audio_destroy(ctx);
    assert(ret == SUCCESS);
    return 0;
}