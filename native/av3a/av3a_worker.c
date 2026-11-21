/* Per-frame AVS3-P3 decoding. The Python caller handles containers and routing.
 * Setup: LE32 channels with the high bit set, and bed channels. Request: LE32 payload size, a
 * nine-byte padded AATF header, and the compressed payload. Reply: planar
 * float32 synthesis samples in PCM16 units and six float32 metadata fields per channel.
 */
#include <stdlib.h>
#include <string.h>

#include "../codec_worker.h"
#include "avs3_prot_com.h"
#include "vivid_metadata.h"

static int decode_frames(void)
{
    AVS3DecoderHandle decoder = NULL;
    ObjectState objects[MAX_CHANNELS] = {0};
    uint8_t header[MAX_NBYTES_FRAME_HEADER] = {0};
    uint8_t payload[MAX_BS_BYTES];
    float pcm[MAX_CHANNELS][FRAME_LEN];
    uint32_t channels, bed_channels, payload_size;
    const char *error = "Invalid Audio Vivid worker request.";

    const uint32_t float_protocol = UINT32_C(0x80000000);
    if (!read_u32(&channels) || !read_u32(&bed_channels) || !(channels & float_protocol))
        goto done;
    channels &= ~float_protocol;
    if (channels == 0 || channels > MAX_CHANNELS || bed_channels > channels)
        goto done;

    for (uint32_t ch = 0; ch < channels; ++ch) {
        objects[ch].position[1] = 1;
        objects[ch].gain = objects[ch].staticGain = 1;
    }
    while (read_u32(&payload_size)) {
        if (payload_size == 0 || payload_size > sizeof(payload) ||
            fread(header, 1, 9, stdin) != 9 ||
            fread(payload, 1, payload_size, stdin) != payload_size)
            goto done;
        if (!decoder) {
            AVS3DecoderHandle state = calloc(1, sizeof(*state));
            if (!state || !Avs3ParseBsFrameHeader(state, header, 1, NULL)) {
                free(state);
                error = "Audio Vivid decoder rejected the initial frame header.";
                goto done;
            }
            FILE *model = NULL;
            if (Avs3InitDecoder(state, &model, "") < 0) {
                free(state);
                error = "Audio Vivid decoder initialization failed.";
                goto done;
            }
            decoder = state;
            memset(decoder->hMetadataDec, 0, sizeof(*decoder->hMetadataDec));
        }
        uint16_t crc;
        if (!Avs3ParseBsFrameHeader(decoder, header, 0, &crc) ||
            Crc16(payload, payload_size) != crc) {
            error = "Audio Vivid decoder rejected the frame header or CRC.";
            goto done;
        }
        memcpy(decoder->hBitstream->bitstream, payload, payload_size);
        Avs3DecodeFloat(decoder, pcm);
        ResetBitstream(decoder->hBitstream);

        if (channels > bed_channels) {
            for (uint32_t ch = bed_channels; ch < channels; ++ch)
                objects[ch].jump = 0;
            if (update_object_state(decoder, objects, channels, bed_channels,
                                    channels - bed_channels) < 0) {
                error = "Unsupported Audio Vivid dynamic metadata level.";
                goto done;
            }
        }
        float metadata[MAX_CHANNELS][6];
        for (uint32_t ch = 0; ch < channels; ++ch) {
            const ObjectState *obj = &objects[ch];
            const int is_object = ch >= bed_channels;
            const int muted = obj->staticMuted || (is_object && obj->muted);
            metadata[ch][0] = (float)obj->position[0];
            metadata[ch][1] = (float)obj->position[1];
            metadata[ch][2] = (float)((obj->position[2] + 1) * 0.5);
            metadata[ch][3] = muted ? 0 : (is_object ? obj->gain : obj->staticGain);
            metadata[ch][4] = is_object ? (float)obj->jump : 1;
            metadata[ch][5] = obj->size;
        }
        error = "Audio Vivid worker could not write PCM.";
        const size_t samples = FRAME_LEN * channels;
        if (fwrite(pcm, sizeof(float), samples, stdout) != samples ||
            fwrite(metadata, sizeof(*metadata), channels, stdout) != channels ||
            fflush(stdout) != 0)
            goto done;
        error = "Invalid Audio Vivid worker request.";
    }
    if (feof(stdin))
        error = NULL;
done:
    if (decoder)
        Avs3DecoderDestroy(decoder);
    if (error)
        fprintf(stderr, "%s\n", error);
    return error != NULL;
}

int main(void)
{
    binary_pipes();
    return decode_frames();
}
