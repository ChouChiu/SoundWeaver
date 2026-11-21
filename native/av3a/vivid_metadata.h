#ifndef SOUNDWEAVER_VIVID_METADATA_H
#define SOUNDWEAVER_VIVID_METADATA_H

#include <math.h>

#include "avs3_prot_dec.h"

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

typedef struct {
    double position[3];
    float gain, staticGain, size;
    int gainUnit, muted, staticMuted, jump;
} ObjectState;

static float linear_gain(float gain, int unit)
{
    return unit ? powf(10, gain / 20) : gain;
}

static void static_metadata(Avs3BasicL1 *meta, ObjectState *state, int channels)
{
    for (int ch = 0; ch < channels; ++ch) {
        state[ch].staticGain = 1;
        state[ch].staticMuted = 0;
        state[ch].gainUnit = 0;
    }
    for (int o = 0; o < meta->numOfObjects && o < 8; ++o) {
        const AudioObject *object = &meta->audioObjectData[o];
        for (int p = 0; p < meta->numOfPacks && p < 8; ++p) {
            const AudioPackFormat *pack = &meta->audioPackFormatData[p];
            int used = 0;
            for (int r = 0; r < object->numPacks && r < 8; ++r) {
                if (object->refPackFormatIdx[r] == pack->packFormatIdx)
                    used = 1;
            }
            if (!used)
                continue;
            for (int c = 0; c < pack->numChannels && c < 32; ++c) {
                const int ch = pack->hasChannelReuse
                    ? pack->transChRef[c] : pack->packFormatStartIdx + c;
                if (ch < 0 || ch >= channels)
                    continue;
                ObjectState *source = &state[ch];
                source->staticMuted = object->hasMute;
                source->staticGain = object->hasGain ? linear_gain(object->gain, object->gainUnit) : 1;
                source->gainUnit = object->hasGain ? object->gainUnit : 0;
                for (int j = 0; j < meta->numOfChannels && j < 32; ++j) {
                    const AudioChannelFormat *format = &meta->audioChannelFormatData[j];
                    if (format->channelFormatIdx != pack->refChannelIdx[c] || !format->hasChannelGain)
                        continue;
                    source->staticGain = linear_gain(format->channelGain, format->gainUnit);
                    source->gainUnit = format->gainUnit;
                }
                source->gain = source->staticGain;
            }
        }
    }
}

static int update_object_state(AVS3DecoderHandle decoder, ObjectState *state,
                               int channels, int bed_channels, int object_count)
{
    Avs3MetaData *meta = &decoder->hMetadataDec->avs3MetaData;
    if (meta->hasStaticMeta)
        static_metadata(&meta->avs3MetaDataStatic.avs3BasicL1, state, channels);
    if (!meta->hasDynamicMeta)
        return 0;

    const Avs3MetaDataDynamic *dynamic = &meta->avs3MetaDataDynamic;
    if (dynamic->dmLevel > 1)
        return -1;
    // The decoder emits bed channels followed by dynamic objects in metadata order.
    for (int o = 0; o < object_count; ++o) {
        ObjectState *object = &state[bed_channels + o];
        const Avs3DmL1MetaData *source = &dynamic->avs3DmL1MetaData[o];
        object->muted = dynamic->muteFlag[o];
        object->jump = 0;
        if (object->muted)
            continue;

        if (source->cartesian) {
            object->position[0] = source->obj_x;
            object->position[1] = source->obj_y;
            object->position[2] = source->obj_z;
        } else {
            const double azimuth = -source->objAzimuth * M_PI / 180;
            const double elevation = source->objElevation * M_PI / 180;
            const double distance = source->objDistance;
            object->position[0] = sin(azimuth) * cos(elevation) * distance;
            object->position[1] = cos(azimuth) * cos(elevation) * distance;
            object->position[2] = sin(elevation) * distance;
        }
        object->size = 0;
        if (source->hasObjExtent) {
            // The circle drawn in the monitor uses the largest normalized extent.
            const float width = source->cartesian ? source->objWidth_x : source->objWidth / 360.0f;
            const float height = source->cartesian ? source->objHeight_y : source->objHeight / 360.0f;
            const float depth = source->cartesian ? source->objDepth_z : source->objDepth;
            object->size = fminf(1.0f, fmaxf(0.0f, fmaxf(width, fmaxf(height, depth))));
        }
        if (source->hasObjGain)
            object->gain = linear_gain(source->gain, object->gainUnit);
        object->jump = source->jumpPosition;
    }
    return 0;
}

#endif
