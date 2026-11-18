/* Copyright 2021 Beijing Zitiao Network Technology Co.,
* Licensed under the Apache License, Version 2.0 (the "License");
* you may not use this file except in compliance with the License.
* You may obtain a copy of the License at
*
* http://www.apache.org/licenses/LICENSE-2.0
*
Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

#include <iostream>
#include <cmath>
#include <map>
#include "bw64.hpp"
#include "metadata_parser.h"
#include "avs3_render_creator.h"

constexpr int kBlockSize = 1024;
constexpr int kOutChannels = 2;

using namespace std;

typedef struct InputInfo {
    std::vector<float> *input_data{};
    uint32_t num_frames{};
    uint32_t channels{};
} InputInfo;

int renderAudio(InputInfo &input_info, vector<float> &output_data, shared_ptr<AVS3::Metadata> &metadata);

int main(int argc, char *argv[]) {
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

//    read bw64 file
    auto audioFile = bw64::readFile(input_wave_path);
    if (!audioFile) {
        std::cout << "wav file open fail!" << endl;
        exit(-1);
    }

    auto axml = audioFile->axmlChunk();
    std::stringstream axmlStream;
    axml->write(axmlStream);
    auto metadata_str = axmlStream.str();
    assert(!metadata_str.empty());

    uint32_t sampleRate = audioFile->sampleRate();
    uint32_t bitDepth = audioFile->bitDepth();
    uint64_t numFrames = audioFile->numberOfFrames();
    uint32_t channels = audioFile->channels();

//    parse metadata of bw64 above
    auto metaData = AVS3::MetadataParser::getMetadata(metadata_str, (int) sampleRate, kBlockSize);
    if (!metaData) {
        std::cout << "check metadata fail" << std::endl;
        exit(-2);
    }

    auto chna = audioFile->chnaChunk();
    if (chna == nullptr) {
        std::cerr << "invalid bw64 file";
        exit(-1);
    }
    auto audioIds = chna->audioIds();
    assert(!audioIds.empty());
    std::map<std::string, int> uid_map;
    for (const auto &audioId : audioIds) {
        uid_map[audioId.uid()] = audioId.trackIndex();
    }

//    connect audioTrackUID with real trackIndex
    if (metaData->connectAudioTrack(uid_map) != 0) {
        std::cerr << "audio track connect fail";
        return -1;
    }

    std::cout << "read file..." << std::endl;
    std::vector<float> file_data(channels * numFrames);
    audioFile->read(file_data.data(), numFrames);

    std::vector<float> output_data(kOutChannels * numFrames, 0);
    InputInfo info;
    info.input_data = &file_data;
    info.num_frames = numFrames;
    info.channels = channels;

    std::cout << "start rendering..." << std::endl;

    int result = renderAudio(info, output_data, metaData);

    std::cout << "rendering finish" << std::endl;

    std::cout << "write file..." << std::endl;
    auto output_file = bw64::writeFile(output_wav_path, kOutChannels, sampleRate, bitDepth);
    output_file->write(output_data.data(), numFrames);

    return result;
}

int renderAudio(InputInfo &input_info, vector<float> &output_data, shared_ptr<AVS3::Metadata> &metadata) {

//    create renderer engine with file mode
    AVS3::AVS3Render<AVS3::Metadata> *renderer = createRenderByID(AVS3::AVS3RenderID::Binaural_Render, metadata, AVS3::File);
    if (!renderer) {
        std::cerr << "no available render called BD_Render";
        return -1;
    }

    std::vector<float> position = {.0, .0, .0};
    std::vector<float> front = {.0, .0, 1.0};
    std::vector<float> up = {.0, 1.0, .0};

//  head tracking when necessary
    renderer->setListenerPosition(position.data(), front.data(), up.data());

    for (int i = 0; i + kBlockSize <= input_info.num_frames; i += kBlockSize) {

        float *in_buffer = (input_info.input_data->data()) + i * input_info.channels;
        if (renderer->putAudioData(in_buffer, (int) input_info.channels, kBlockSize) != 0) {
            std::cerr << "put audio fail" << std::endl;
        }
        float * out_buffer = output_data.data() + i * kOutChannels;
        if (renderer->getAudioData(out_buffer, kBlockSize) != 0) {
            std::cerr << "get audio fail" << std::endl;
        }
    }
    renderer->destroyRender();
    delete renderer;
    return 0;
}