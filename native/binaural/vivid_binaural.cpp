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
    // CICP / 360RA speaker labels
    {"Lc", 60.0f, 0.0f, false},    {"Rc", -60.0f, 0.0f, false},
    {"Lss", 90.0f, 0.0f, false},   {"Rss", -90.0f, 0.0f, false},
    {"Leos", 60.0f, 0.0f, false},  {"Reos", -60.0f, 0.0f, false},
    {"Lv", 45.0f, 35.0f, false},   {"Rv", -45.0f, 35.0f, false},
    {"Cv", 0.0f, 35.0f, false},    {"Lvr", 135.0f, 35.0f, false},
    {"Rvr", -135.0f, 35.0f, false},{"Lvss", 90.0f, 35.0f, false},
    {"Rvss", -90.0f, 35.0f, false},{"Cvr", 180.0f, 35.0f, false},
    {"Lvs", 135.0f, 35.0f, false}, {"Rvs", -135.0f, 35.0f, false},
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

struct Biquad {
    double b0 = 1.0, b1 = 0.0, b2 = 0.0, a1 = 0.0, a2 = 0.0;
    double z1[2] = {0.0, 0.0};
    double z2[2] = {0.0, 0.0};

    static Biquad shelf(double fs, double fc, double gain_db, bool high) {
        Biquad q;
        const double A = std::pow(10.0, gain_db / 40.0);
        const double w0 = 2.0 * M_PI * fc / fs;
        const double cosw = std::cos(w0);
        const double sinw = std::sin(w0);
        const double beta = std::sqrt(2.0 * A) * sinw;
        double a0 = 1.0;
        if (high) {
            q.b0 = A * ((A + 1.0) + (A - 1.0) * cosw + beta);
            q.b1 = -2.0 * A * ((A - 1.0) + (A + 1.0) * cosw);
            q.b2 = A * ((A + 1.0) + (A - 1.0) * cosw - beta);
            a0 = (A + 1.0) - (A - 1.0) * cosw + beta;
            q.a1 = 2.0 * ((A - 1.0) - (A + 1.0) * cosw);
            q.a2 = (A + 1.0) - (A - 1.0) * cosw - beta;
        } else {
            q.b0 = A * ((A + 1.0) - (A - 1.0) * cosw + beta);
            q.b1 = 2.0 * A * ((A - 1.0) - (A + 1.0) * cosw);
            q.b2 = A * ((A + 1.0) - (A - 1.0) * cosw - beta);
            a0 = (A + 1.0) + (A - 1.0) * cosw + beta;
            q.a1 = -2.0 * ((A - 1.0) + (A + 1.0) * cosw);
            q.a2 = (A + 1.0) + (A - 1.0) * cosw - beta;
        }
        q.b0 /= a0;
        q.b1 /= a0;
        q.b2 /= a0;
        q.a1 /= a0;
        q.a2 /= a0;
        return q;
    }

    float process(int ch, float x) {
        const double y = b0 * x + z1[ch];
        z1[ch] = b1 * x - a1 * y + z2[ch];
        z2[ch] = b2 * x - a2 * y;
        return static_cast<float>(y);
    }
};

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
    int active_speakers = 0;
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
            ++active_speakers;
        }
        float spk_gain = std::min(1.0f, std::sqrt(2.0f / static_cast<float>(std::max(1, active_speakers))));
        for (int sid : source_ids) {
            if (sid >= 0) {
                audio_set_source_gain(ctx, sid, spk_gain);
            }
        }
    }

    const float lfe_gain = 0.5f * std::min(1.0f, std::sqrt(2.0f / static_cast<float>(std::max(1, active_speakers))));
    std::vector<float> in_blk(channels * kBlockSize, 0.0f);
    std::vector<float> ch_buf(kBlockSize, 0.0f);
    std::vector<float> out_blk(kOutChannels * kBlockSize, 0.0f);
    std::vector<float> rendered;
    rendered.reserve(static_cast<size_t>(num_frames) * kOutChannels);
    float peak = 0.0f;

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

        // Mix any LFE channels equally to both ears with headroom scaling.
        for (uint32_t ch = 0; ch < channels; ++ch) {
            if (!lfe_mask[ch]) continue;
            for (uint64_t f = 0; f < n; ++f) {
                float lfe = in_blk[f * channels + ch] * lfe_gain;
                out_blk[f * 2] += lfe;
                out_blk[f * 2 + 1] += lfe;
            }
        }
        for (uint64_t i = 0; i < n * kOutChannels; ++i) {
            peak = std::max(peak, std::abs(out_blk[i]));
        }
        rendered.insert(rendered.end(), out_blk.begin(), out_blk.begin() + static_cast<ptrdiff_t>(n * kOutChannels));
    }

    // Compensate Ambisonic low-mid buildup and raw HRTF high-frequency roll-off.
    Biquad low_shelf = Biquad::shelf(static_cast<double>(sample_rate), 240.0, -3.5, false);
    Biquad high_shelf = Biquad::shelf(static_cast<double>(sample_rate), 3200.0, 4.5, true);
    peak = 0.0f;
    for (size_t i = 0; i + 1 < rendered.size(); i += 2) {
        float l = high_shelf.process(0, low_shelf.process(0, rendered[i]));
        float r = high_shelf.process(1, low_shelf.process(1, rendered[i + 1]));
        rendered[i] = l;
        rendered[i + 1] = r;
        peak = std::max(peak, std::max(std::abs(l), std::abs(r)));
    }

    if (peak > 0.98f) {
        const float norm = 0.98f / peak;
        for (float &s : rendered) {
            s *= norm;
        }
    }
    auto writer = bw64::writeFile(out_path, kOutChannels, sample_rate, bit_depth);
    for (size_t pos = 0; pos < rendered.size(); pos += static_cast<size_t>(kBlockSize) * kOutChannels) {
        uint64_t frames_left = (rendered.size() - pos) / kOutChannels;
        uint64_t n = std::min<uint64_t>(kBlockSize, frames_left);
        writer->write(rendered.data() + pos, n);
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
