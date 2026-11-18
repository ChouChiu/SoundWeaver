/* ====================================================================================================================

  The copyright in this software is being made available under the License included below.
  No express or implied licenses to any party's patent rights are granted by this license.

  Copyright (c) 2022, HUAWEI TECHNOLOGIES CO., LTD. All rights reserved.
  Copyright (c) 2022, XIAOMI COMMUNICATIONS CO., LTD. All rights reserved.
  Copyright (c) 2022, BEIJING ZITIAO NETWORK TECHNOLOGY CO., LTD. All rights reserved.
  Copyright (c) 2022, BEIJING SINECORE MICROSEMI TECHNOLOGY CO., LTD. All rights reserved.
  Copyright (c) 2022, WAVARTS TECHNOLOGIES CO., LTD. All rights reserved.
  Copyright (c) 2022, PEKING UNIVERSITY. All rights reserved.
  Copyright (c) 2022, TSINGHUA UNIVERSITY. All rights reserved.

  Redistribution and use in source and binary forms, with or without modification, are permitted only for
  the purpose of developing standards within Audio and Video Coding Standard Workgroup of China (AVS) and for testing and
  promoting such standards. The following conditions are required to be met:

    * Redistributions of source code must retain the above copyright notice, this list of conditions and
      the following disclaimer.
    * Redistributions in binary form must reproduce the above copyright notice, this list of conditions and
      the following disclaimer in the documentation and/or other materials provided with the distribution.
    * The name of the above copyright owners may not be used to endorse or promote products derived from
      this software without specific prior written permission.

  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES,
  INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
  ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
  INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
  SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
  THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
  ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

==================================================================================================================== */

#include <stdlib.h>
#include <stdio.h>
#include <stdint.h>
#include <math.h>
#include <assert.h>
#include "avs3_options.h"
#include "avs3_cnst_com.h"
#include "avs3_rom_com.h"
#include "avs3_stat_com.h"
#include "avs3_prot_com.h"


#ifdef BWE_DEVELOPE

/*
Get bwe present flag
I/O params:
    const int16_t avs3Format            (i) avs3 format info
    const int32_t totalBitrate          (i) codec total bitrate per second
    const int16_t numChannels           (i) number of channels for core coder
    ret                                 (o) bwe present flag
*/
int16_t GetBwePresent(
    const int16_t avs3Format,
    const int32_t totalBitrate,
    const int16_t numChannels
)
{
    int16_t isBwePresent = 0;
    int32_t bitratePerCpe;

    if (avs3Format == AVS3_MONO_FORMAT) {
        if (totalBitrate <= 96000) {
            isBwePresent = 1;
        }
    }
    else if (avs3Format == AVS3_STEREO_FORMAT) {
        if (totalBitrate <= 128000) {
            isBwePresent = 1;
        }
    }
#ifndef MIX_DEVELOPE
    else if (avs3Format == AVS3_MC_FORMAT) {
#else
    else if (avs3Format == AVS3_MC_FORMAT || avs3Format == AVS3_MIX_FORMAT) {
#endif
        bitratePerCpe = (int32_t)((float)(totalBitrate * STEREO_CHANNELS) / (float)numChannels);
        if (bitratePerCpe <= 128000) {
            isBwePresent = 1;
        }
    }
#ifdef AVS3_HOA_FULL_SUPPORT
    else if (avs3Format == AVS3_INNER_FOA_FORMAT || avs3Format == AVS3_INNER_HOA2_FORMAT || avs3Format == AVS3_INNER_HOA3_FORMAT)
    {
        if (avs3Format == AVS3_INNER_FOA_FORMAT)
        {
            bitratePerCpe = (int32_t)((float)(totalBitrate * STEREO_CHANNELS) / (float)numChannels);
            if (bitratePerCpe <= 128000) {
                isBwePresent = 1;
            }
        }
        else if (avs3Format == AVS3_INNER_HOA2_FORMAT)
        {
            if (totalBitrate <= 480000) {
                isBwePresent = 1;
            }
        }
        else {
            isBwePresent = 1;
        }
    }
#else
    else if (avs3Format == AVS3_HOA_FORMAT) {
#ifdef AVS3_HOA_BWE
        isBwePresent = 1;
#else
        bitratePerCpe = (int32_t)((float)(totalBitrate * STEREO_CHANNELS) / (float)numChannels);
        if (bitratePerCpe <= 128000) {
            isBwePresent = 1;
        }
#endif
    }
#endif

    return isBwePresent;
}


/*
Get bwe bitrate index for bwe configuration
I/O params:
    const int16_t avs3Format            (i) avs3 format info
    const int32_t totalBitrate          (i) codec total bitrate per second
    const int16_t numChannels           (i) number of channels for core coder
    ret                                 (o) bwe bitrate index
*/
static BweRateIndex BweGetRateIndex(
    const int16_t avs3Format,
    const int32_t totalBitrate,
    const int16_t numChannels
)
{
    int16_t bitRateIndex = BWE_BITRATE_FB_UNKNOWN;
    int32_t bitratePerCpe;

    if (avs3Format == AVS3_MONO_FORMAT) {

        // BWE available bitrate for Mono
        // 32/48/64/96 kbps
        if (totalBitrate <= 32000) {
            bitRateIndex = BWE_BITRATE_FB_MONO_32K;
        }
#ifndef BRTABLE_ALIGN
        else if (totalBitrate == 44000 || totalBitrate == 48000) {
#else
        else if (totalBitrate == 44000 || totalBitrate == 56000) {
#endif
            bitRateIndex = BWE_BITRATE_FB_MONO_48K;
        }
        else if (totalBitrate == 64000 || totalBitrate == 72000) {
            bitRateIndex = BWE_BITRATE_FB_MONO_64K;
        }
#ifndef BRTABLE_ALIGN
        else if (totalBitrate == 96000) {
#else
        else if (totalBitrate == 80000 || totalBitrate == 96000) {
#endif
            bitRateIndex = BWE_BITRATE_FB_MONO_96K;
        }
    }
    else if (avs3Format == AVS3_STEREO_FORMAT) {

        // BWE available bitrate for stereo
        // 48/64/96/128 kbps
#ifndef MIX_EXT
        if (totalBitrate == 48000) {
#else
        if (totalBitrate <= 48000) {
#endif
            bitRateIndex = BWE_BITRATE_FB_STEREO_48K;
        }
#ifndef MIX_EXT
        else if (totalBitrate == 64000) {
#else
        else if (totalBitrate <= 64000) {
#endif
            bitRateIndex = BWE_BITRATE_FB_STEREO_64K;
        }
#ifndef MIX_EXT
        else if (totalBitrate == 96000) {
#else
        else if (totalBitrate <= 96000) {
#endif
            bitRateIndex = BWE_BITRATE_FB_STEREO_96K;
        }
#ifndef MIX_EXT
        else if (totalBitrate == 128000) {
#else
        else if (totalBitrate <= 128000) {
#endif
            bitRateIndex = BWE_BITRATE_FB_STEREO_128K;
        }
    }
#ifndef MIX_DEVELOPE
    else if (avs3Format == AVS3_MC_FORMAT) {
#else
    else if (avs3Format == AVS3_MC_FORMAT || avs3Format == AVS3_MIX_FORMAT) {
#endif

        // get bitrate per cpe to determine BWE bitrate index
        bitratePerCpe = (int32_t)((float)(totalBitrate * STEREO_CHANNELS) / (float)numChannels);

#ifndef BWE_TUNING
        if (bitratePerCpe <= 48000) {
            bitRateIndex = BWE_BITRATE_FB_STEREO_48K;
        }
        else if (bitratePerCpe <= 64000) {
            bitRateIndex = BWE_BITRATE_FB_STEREO_64K;
        }
        else if (bitratePerCpe <= 96000) {
            bitRateIndex = BWE_BITRATE_FB_STEREO_96K;
        }
        else if (bitratePerCpe <= 128000) {
            bitRateIndex = BWE_BITRATE_FB_STEREO_128K;
        }
#else
#ifdef MC_TUNING
        if (bitratePerCpe <= 56000) {
            bitRateIndex = BWE_BITRATE_FB_MC_CPE_48K;
        }
        else if (bitratePerCpe <= 75000) {
            bitRateIndex = BWE_BITRATE_FB_MC_CPE_64K;
        }
        else if (bitratePerCpe <= 108000) {
            bitRateIndex = BWE_BITRATE_FB_MC_CPE_96K;
        }
        else if (bitratePerCpe <= 128000) {
            bitRateIndex = BWE_BITRATE_FB_MC_CPE_128K;
        }
#else
        if (bitratePerCpe <= 48000) {
            bitRateIndex = BWE_BITRATE_FB_MC_CPE_48K;
        }
        else if (bitratePerCpe <= 64000) {
            bitRateIndex = BWE_BITRATE_FB_MC_CPE_64K;
        }
        else if (bitratePerCpe <= 96000) {
            bitRateIndex = BWE_BITRATE_FB_MC_CPE_96K;
        }
        else if (bitratePerCpe <= 128000) {
            bitRateIndex = BWE_BITRATE_FB_MC_CPE_128K;
        }
#endif
#endif
    }
#ifdef AVS3_HOA_FULL_SUPPORT
    else if (avs3Format == AVS3_INNER_FOA_FORMAT || avs3Format == AVS3_INNER_HOA2_FORMAT
    || avs3Format == AVS3_INNER_HOA3_FORMAT)
    {
    if (avs3Format == AVS3_INNER_FOA_FORMAT) {
        if (totalBitrate <= 128000)
        {
            bitRateIndex = BWE_BITRATE_FB_HOA_LOW;
        }
        else if (totalBitrate == 192000) {
            bitRateIndex = BWE_BITRATE_FB_HOA_MIDDLE;
        }
        else if (totalBitrate == 256000) {
            bitRateIndex = BWE_BITRATE_FB_HOA_HIGH;
        }
    }
    else if (avs3Format == AVS3_INNER_HOA2_FORMAT) {
        if (totalBitrate == 192000) {
            bitRateIndex = BWE_BITRATE_FB_HOA_ELOW;
        }
        else if (totalBitrate == 256000) {
            bitRateIndex = BWE_BITRATE_FB_HOA_LOW;
        }
        else if (totalBitrate == 320000) {
            bitRateIndex = BWE_BITRATE_FB_HOA_MIDDLE;
        }
        else if (totalBitrate >= 384000 && totalBitrate <= 480000) {
            bitRateIndex = BWE_BITRATE_FB_HOA_HIGH;
        }
    }
    else if (avs3Format == AVS3_INNER_HOA3_FORMAT) {
        if (totalBitrate >= 256000 && totalBitrate <= 384000) {
            bitRateIndex = BWE_BITRATE_FB_HOA_LOW;
        }
        else if (totalBitrate == 512000) {
            bitRateIndex = BWE_BITRATE_FB_HOA_MIDDLE;
        }
        else if (totalBitrate >= 640000) {
            bitRateIndex = BWE_BITRATE_FB_HOA_HIGH;
        }
        else
        {
            assert(!"Not support HOA bitrate!\n");
        }
    }
    }
#else
    else if (avs3Format == AVS3_HOA_FORMAT) {

#ifdef AVS3_HOA_BWE
        if (totalBitrate <= HOA_BITRATE_256K) {
            bitRateIndex = BWE_BITRATE_HOA_256K;
        }
        else if (totalBitrate <= HOA_BITRATE_384K) {
            bitRateIndex = BWE_BITRATE_HOA_384K;
        }
        else if (totalBitrate <= HOA_BITRATE_512K) {
            bitRateIndex = BWE_BITRATE_HOA_512K;
        }
        else if (totalBitrate <= HOA_BITRATE_768K) {
            bitRateIndex = BWE_BITRATE_HOA_768K;
        }
        else if (totalBitrate <= HOA_BITRATE_896K) {
            bitRateIndex = BWE_BITRATE_HOA_896K;
        }
        else 
        {
            assert(!"Not support HOA bitrate!\n");
        }
#else
        // get bitrate per cpe to determine BWE bitrate index
        bitratePerCpe = (int32_t)((float)(totalBitrate * STEREO_CHANNELS) / (float)numChannels);

        if (bitratePerCpe <= 48000) {
            bitRateIndex = BWE_BITRATE_FB_STEREO_48K;
        }
        else if (bitratePerCpe <= 64000) {
            bitRateIndex = BWE_BITRATE_FB_STEREO_64K;
        }
        else if (bitratePerCpe <= 96000) {
            bitRateIndex = BWE_BITRATE_FB_STEREO_96K;
        }
        else if (bitratePerCpe <= 128000) {
            bitRateIndex = BWE_BITRATE_FB_STEREO_128K;
        }
#endif
    }
#endif

    return bitRateIndex;
}


/*
Get bwe configuration
I/O params:
    BweConfigHandle bweConfig           (i/o) bwe config handle
    const int16_t avs3Format            (i)   avs3 format info
    const int32_t totalBitrate          (i)   codec total bitrate per second
    const int16_t numChannels           (i)   number of channels for core coder
*/
void BweGetConfig(
    BweConfigHandle bweConfig,
    const int16_t avs3Format,
    const int32_t totalBitrate,
    const int16_t numChannels
)
{
    int16_t i;
    int16_t bitRateIndex;
    const int16_t *targetTiles;
    const int16_t *sfbTable;

    // get bwe bitrate index for configuration
    bitRateIndex = BweGetRateIndex(avs3Format, totalBitrate, numChannels);
    if (bitRateIndex == BWE_BITRATE_FB_UNKNOWN) {
        fprintf(stderr, "Error in BWE bitrate configuration!!\n");
    }

    // get number tiles
    bweConfig->numTiles = bweTargetTileTable[bitRateIndex][0];

    // get target tile table
    SetShort(bweConfig->targetTiles, 0, MAX_NUM_TILE + 1);
    targetTiles = &bweTargetTileTable[bitRateIndex][1];
    for (i = 0; i < bweConfig->numTiles + 1; i++) {
        bweConfig->targetTiles[i] = targetTiles[i];
    }

    // get bwe start and stop line
    bweConfig->bweStartLine = bweConfig->targetTiles[0];
    bweConfig->bweStopLine = bweConfig->targetTiles[bweConfig->numTiles];

    // get number of sfbs
    bweConfig->numSfb = bweSfbTable[bitRateIndex][0];

    // get sfb table
    SetShort(bweConfig->sfbTable, 0, MAX_NUM_SFB_BWE + 1);
    sfbTable = &bweSfbTable[bitRateIndex][1];
    for (i = 0; i < bweConfig->numSfb + 1; i++) {
        bweConfig->sfbTable[i] = sfbTable[i];
    }

    // get source tile table
    SetShort(bweConfig->srcTiles, 0, MAX_NUM_TILE);
    for (i = 0; i < bweConfig->numTiles; i++) {
        bweConfig->srcTiles[i] = bweSrcTileTable[bitRateIndex][i];
    }

    // get sfb-tile wrap table
    SetShort(bweConfig->sfbTileWrap, 0, MAX_NUM_TILE + 1);
    for (i = 0; i < bweConfig->numTiles + 1; i++) {
        bweConfig->sfbTileWrap[i] = bweSfbTileWrapTable[bitRateIndex][i];
    }

    return;
}


/*
Get SFM parameter for spectrum whitening decision
I/O params:
    float *enerSpec             (i) mdct energy spectrum
    float *logEnerSpec          (i) log mdct energy spectrum
    int16_t start               (i) start bin index of spectrum
    int16_t stop                (i) stop bin index of spectrum
    ret                         (o) SFM parameter
*/
float BweGetSfm(
    float *enerSpec,
    float *logEnerSpec,
    int16_t start,
    int16_t stop
)
{
    int16_t i;
    float num = 0.0f;
    float denom = 1.0f;
    float sfm = 1.0f;

    for (i = start; i < stop; i++) {
        num += logEnerSpec[i];
        denom += enerSpec[i];
    }

    num /= (float)(stop - start);
    denom /= (float)(stop - start);

    if (denom != 0.0f) {
        sfm = min(1.0f, (float)pow(2.0, num + 0.5) / denom);
    }

    return sfm;
}


/*
Get peak-average-ratio of log mdct energy spectrum
I/O params:
    float *logEnerSpec              (i) log mdct energy spectrum
    int16_t start                   (i) start bin index of spectrum
    int16_t stop                    (i) stop bin index of spectrum
    ret                             (o) peak-average-ratio parameter
*/
float BweGetPeakAvgRatio(
    float *logEnerSpec,
    int16_t start,
    int16_t stop
)
{
    int16_t i;
    float maxLineEner = 0.0f;
    float avgLineEner = 0.0f;
    float peakAvgRatio = 0.0f;

    for (i = start; i < stop; i++) {
        if (maxLineEner < logEnerSpec[i]) {
            maxLineEner = logEnerSpec[i];
        }
        avgLineEner += logEnerSpec[i];
    }
    avgLineEner /= (float)(stop - start);

    if (avgLineEner == 0.0f) {
        avgLineEner = 0.01f;
    }

    peakAvgRatio = max(1.0f, maxLineEner / avgLineEner);

    return peakAvgRatio;
}

#endif