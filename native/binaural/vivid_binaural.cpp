/* Audio Vivid / ADM / HOA Binaural HRTF Renderer CLI (`vivid_binaural`). */
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <map>
#include <sstream>
#include <string>
#include <vector>

#include "avs3_audio.h"
#include "avs3_render_creator.h"
#include "bw64.hpp"
#include "metadata_parser.h"

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

namespace {

constexpr int kBlockSize = 1024;
constexpr int kOutChannels = 2;

struct SpeakerAngle {
    const char *name;
    float az_deg;
    float el_deg;
    bool is_lfe;
};

constexpr SpeakerAngle kSpeakerAngles[] = {
    {"L", 30.0f, 0.0f, false},     {"R", -30.0f, 0.0f, false},
    {"C", 0.0f, 0.0f, false},      {"LFE", 0.0f, 0.0f, true},
    {"LFE2", 0.0f, 0.0f, true},    {"Ls", 110.0f, 0.0f, false},
    {"Rs", -110.0f, 0.0f, false},  {"Lrs", 150.0f, 0.0f, false},
    {"Rrs", -150.0f, 0.0f, false}, {"Lb", 150.0f, 0.0f, false},
    {"Rb", -150.0f, 0.0f, false},  {"Lw", 60.0f, 0.0f, false},
    {"Rw", -60.0f, 0.0f, false},   {"Cs", 180.0f, 0.0f, false},
    {"Cb", 180.0f, 0.0f, false},   {"Lfc", 15.0f, 0.0f, false},
    {"Rfc", -15.0f, 0.0f, false},  {"Ltf", 30.0f, 35.0f, false},
    {"Rtf", -30.0f, 35.0f, false}, {"Tfl", 30.0f, 35.0f, false},
    {"Tfr", -30.0f, 35.0f, false}, {"Tfc", 0.0f, 35.0f, false},
    {"Ltm", 90.0f, 45.0f, false},  {"Rtm", -90.0f, 45.0f, false},
    {"Tsl", 90.0f, 45.0f, false},  {"Tsr", -90.0f, 45.0f, false},
    {"Tsc", 0.0f, 90.0f, false},   {"Ts", 0.0f, 90.0f, false},
    {"Ltr", 135.0f, 35.0f, false}, {"Rtr", -135.0f, 35.0f, false},
    {"Tbl", 135.0f, 35.0f, false}, {"Tbr", -135.0f, 35.0f, false},
    {"Tbc", 180.0f, 35.0f, false}, {"Bfl", 45.0f, -15.0f, false},
    {"Bfr", -45.0f, -15.0f, false},{"Bfc", 0.0f, -15.0f, false},
};

const SpeakerAngle *find_speaker(const std::string &name) {
    for (const auto &s : kSpeakerAngles) {
        if (name == s.name) return &s;
    }
    return nullptr;
}

std::vector<std::string> split_csv(const std::string &csv) {
    std::vector<std::string> out;
    std::stringstream ss(csv);
    std::string item;
    while (std::getline(ss, item, ',')) {
        size_t start = item.find_first_not_of(" \t");
        size_t end = item.find_last_not_of(" \t");
        if (start != std::string::npos) {
            out.push_back(item.substr(start, end - start + 1));
        }
    }
    return out;
}

void polar_to_cart(float az_deg, float el_deg, float out[3]) {
    float az = static_cast<float>(M_PI) * az_deg / 180.0f;
    float el = static_cast<float>(M_PI) * el_deg / 180.0f;
    out[0] = -std::sin(az) * std::cos(el);
    out[1] = std::sin(el);
    out[2] = std::cos(az) * std::cos(el);
}

int render_adm(bw64::Bw64Reader *wav, const std::string &out_path) {
    auto axml = wav->axmlChunk();
    auto chna = wav->chnaChunk();
    if (!axml || !chna) {
        std::cerr << "Input WAV does not contain ADM axml/chna chunks." << std::endl;
        return 2;
    }
    std::stringstream axml_stream;
    axml->write(axml_stream);
    std::string adm_xml = axml_stream.str();
    uint32_t sample_rate = wav->sampleRate();
    uint32_t bit_depth = wav->bitDepth();
    uint64_t num_frames = wav->numberOfFrames();
    uint32_t channels = wav->channels();

    auto meta = AVS3::MetadataParser::getMetadata(adm_xml, static_cast<int>(sample_rate), kBlockSize);
    if (!meta) {
        std::cerr << "Failed to parse ADM metadata." << std::endl;
        return 2;
    }
    std::map<std::string, int> uid_map;
    for (const auto &audio_id : chna->audioIds()) {
        uid_map[audio_id.uid()] = audio_id.trackIndex();
    }
    if (meta->connectAudioTrack(uid_map) != 0) {
        std::cerr << "Failed to map ADM chna tracks." << std::endl;
        return 2;
    }

    AVS3::AVS3Render<AVS3::Metadata> *renderer =
        createRenderByID(AVS3::AVS3RenderID::Binaural_Render, meta, AVS3::File);
    if (!renderer) {
        std::cerr << "Failed to create AVS3 Binaural_Render instance." << std::endl;
        return 1;
    }
    float pos[3] = {0.0f, 0.0f, 0.0f};
    float front[3] = {0.0f, 0.0f, 1.0f};
    float up[3] = {0.0f, 1.0f, 0.0f};
    renderer->setListenerPosition(pos, front, up);

    auto writer = bw64::writeFile(out_path, kOutChannels, sample_rate, bit_depth);
    std::vector<float> in_blk(channels * kBlockSize, 0.0f);
    std::vector<float> out_blk(kOutChannels * kBlockSize, 0.0f);
    for (uint64_t done = 0; done < num_frames; done += kBlockSize) {
        uint64_t n = std::min<uint64_t>(kBlockSize, num_frames - done);
        std::fill(in_blk.begin(), in_blk.end(), 0.0f);
        wav->read(in_blk.data(), n);
        if (renderer->putAudioData(in_blk.data(), static_cast<int>(channels), kBlockSize) != 0 ||
            renderer->getAudioData(out_blk.data(), kBlockSize) != 0) {
            renderer->destroyRender();
            delete renderer;
            std::cerr << "Binaural render processing failed." << std::endl;
            return 1;
        }
        writer->write(out_blk.data(), n);
    }
    renderer->destroyRender();
    delete renderer;
    return 0;
}

int render_direct(
    bw64::Bw64Reader *wav,
    const std::string &out_path,
    int hoa_order,
    const std::vector<std::string> &speakers
) {
    uint32_t sample_rate = wav->sampleRate();
    uint32_t bit_depth = wav->bitDepth();
    uint64_t num_frames = wav->numberOfFrames();
    uint32_t channels = wav->channels();

    audio_context *ctx = nullptr;
    if (audio_create_context(&ctx, AMBISONIC_SEVENTH_ORDER, kBlockSize, static_cast<int>(sample_rate)) != SUCCESS ||
        audio_initialize_context(ctx) != SUCCESS ||
        audio_commit_scene(ctx) != SUCCESS) {
        std::cerr << "Failed to initialize binaural audio context." << std::endl;
        return 1;
    }

    std::vector<int> source_ids(channels, -1);
    std::vector<bool> lfe_mask(channels, false);
    if (hoa_order <= 0) {
        for (uint32_t ch = 0; ch < channels; ++ch) {
            const SpeakerAngle *ang = (ch < speakers.size()) ? find_speaker(speakers[ch]) : nullptr;
            if (ang && ang->is_lfe) {
                lfe_mask[ch] = true;
                continue;
            }
            float xyz[3] = {0.0f, 0.0f, 1.0f};
            if (ang) {
                polar_to_cart(ang->az_deg, ang->el_deg, xyz);
            } else {
                float angle = (channels > 1) ? (60.0f * (0.5f - static_cast<float>(ch) / (channels - 1))) : 0.0f;
                polar_to_cart(angle, 0.0f, xyz);
            }
            int sid = -1;
            if (audio_add_source(ctx, SOURCE_SPATIALIZE, xyz, &sid) != SUCCESS) {
                audio_destroy(ctx);
                std::cerr << "Failed to add binaural source." << std::endl;
                return 1;
            }
            source_ids[ch] = sid;
        }
    }

    auto writer = bw64::writeFile(out_path, kOutChannels, sample_rate, bit_depth);
    std::vector<float> in_blk(channels * kBlockSize, 0.0f);
    std::vector<float> ch_buf(kBlockSize, 0.0f);
    std::vector<float> out_blk(kOutChannels * kBlockSize, 0.0f);

    for (uint64_t done = 0; done < num_frames; done += kBlockSize) {
        uint64_t n = std::min<uint64_t>(kBlockSize, num_frames - done);
        std::fill(in_blk.begin(), in_blk.end(), 0.0f);
        wav->read(in_blk.data(), n);

        for (uint32_t ch = 0; ch < channels; ++ch) {
            for (int f = 0; f < kBlockSize; ++f) {
                ch_buf[f] = in_blk[f * channels + ch];
            }
            if (hoa_order > 0) {
                int order = static_cast<int>(std::floor(std::sqrt(static_cast<double>(ch))));
                int degree = static_cast<int>(ch) - order * order - order;
                audio_submit_ambisonic_channel_buffer(ctx, ch_buf.data(), order, degree, SN3D, 1.0f);
            } else if (source_ids[ch] >= 0) {
                audio_submit_source_buffer(ctx, source_ids[ch], ch_buf.data(), kBlockSize);
            }
        }
        audio_update_scene(ctx);
        audio_get_interleaved_binaural_buffer(ctx, out_blk.data(), kBlockSize);

        // Mix any LFE channels equally to both ears at -6 dB so sub energy is preserved cleanly.
        for (uint32_t ch = 0; ch < channels; ++ch) {
            if (!lfe_mask[ch]) continue;
            for (uint64_t f = 0; f < n; ++f) {
                float lfe = in_blk[f * channels + ch] * 0.5f;
                out_blk[f * 2] = std::max(-1.0f, std::min(1.0f, out_blk[f * 2] + lfe));
                out_blk[f * 2 + 1] = std::max(-1.0f, std::min(1.0f, out_blk[f * 2 + 1] + lfe));
            }
        }
        writer->write(out_blk.data(), n);
    }

    audio_destroy(ctx);
    return 0;
}

}  // namespace

int main(int argc, char **argv) {
    std::string in_path;
    std::string out_path;
    std::string speakers_csv;
    int hoa_order = 0;

    for (int i = 1; i < argc; ++i) {
        if (std::strcmp(argv[i], "-i") == 0 && i + 1 < argc) {
            in_path = argv[++i];
        } else if (std::strcmp(argv[i], "-o") == 0 && i + 1 < argc) {
            out_path = argv[++i];
        } else if (std::strcmp(argv[i], "--speakers") == 0 && i + 1 < argc) {
            speakers_csv = argv[++i];
        } else if (std::strcmp(argv[i], "--hoa") == 0 && i + 1 < argc) {
            hoa_order = std::atoi(argv[++i]);
        }
    }

    if (in_path.empty() || out_path.empty()) {
        std::cerr << "usage: vivid_binaural -i input.wav -o output.wav [--speakers L,R,... | --hoa 1..3]" << std::endl;
        return 2;
    }

    auto wav = bw64::readFile(in_path);
    if (!wav) {
        std::cerr << "Failed to open input WAV: " << in_path << std::endl;
        return 1;
    }

    if (hoa_order > 0 || !speakers_csv.empty()) {
        return render_direct(wav.get(), out_path, hoa_order, split_csv(speakers_csv));
    }
    if (wav->axmlChunk() && wav->chnaChunk()) {
        return render_adm(wav.get(), out_path);
    }
    std::vector<std::string> fallback;
    if (wav->channels() == 6) fallback = {"L", "R", "C", "LFE", "Ls", "Rs"};
    else if (wav->channels() == 8) fallback = {"L", "R", "C", "LFE", "Lb", "Rb", "Ls", "Rs"};
    else if (wav->channels() == 10) fallback = {"L", "R", "C", "LFE", "Ls", "Rs", "Tfl", "Tfr", "Tbl", "Tbr"};
    else if (wav->channels() == 12) fallback = {"L", "R", "C", "LFE", "Lb", "Rb", "Ls", "Rs", "Tfl", "Tfr", "Tbl", "Tbr"};
    return render_direct(wav.get(), out_path, 0, fallback);
}
