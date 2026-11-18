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

#include "avs3_options.h"
#include "avs3_cnst_com.h"
#include "avs3_cnst_enc.h"
#include "avs3_stat_com.h"
#include "avs3_stat_enc.h"
#include "avs3_prot_com.h"
#include "avs3_prot_enc.h"


void Avs3StereoEncoder(
    AVS3EncoderHandle stAvs3, 
    short* channelBytes
)
{
    short frameLength = stAvs3->frameLength;
    float selfCorrL, selfCorrR, crossCorr;

    float lrRatio;

    int16_t numGroups[STEREO_CHANNELS];         // num groups for each channel
    short availableBits = 0;

    AVS3_STEREO_ENC_HANDLE hMdctStereo = stAvs3->hMdctStereo;
    AVS3_ENC_CORE_HANDLE hEncCoreL = stAvs3->hEncCore[0];
    AVS3_ENC_CORE_HANDLE hEncCoreR = stAvs3->hEncCore[1];

    // Init ms flag
    hMdctStereo->isMS = 0;

    // Cross-Correlation
    crossCorr = Dotp(hEncCoreL->origSpectrum, hEncCoreR->origSpectrum, frameLength);
    selfCorrL = Dotp(hEncCoreL->origSpectrum, hEncCoreL->origSpectrum, frameLength);
    selfCorrR = Dotp(hEncCoreR->origSpectrum, hEncCoreR->origSpectrum, frameLength);

    crossCorr = (float)fabs(crossCorr) / (float)(sqrt(selfCorrL) * sqrt(selfCorrR));

    // L/R energy ratio
    lrRatio = (float)(sqrt(selfCorrL) / sqrt(selfCorrR));

    // down mix decision
    if (hEncCoreL->transformType == hEncCoreR->transformType && crossCorr > TH_CROSS_CORR)
    {
        if (lrRatio < LR_ENGERY_RATIO_H && lrRatio > LR_ENGERY_RATIO_L)
        {
            hMdctStereo->isMS = 1;

            StereoMsProcess(hEncCoreL->origSpectrum, hEncCoreR->origSpectrum, frameLength, &hMdctStereo->ILD);
        }
    }

    // grouping for short window
    for (int16_t i = 0; i < STEREO_CHANNELS; i++) {

        AVS3_ENC_CORE_HANDLE hEncCore = stAvs3->hEncCore[i];
        SpectrumGroupingEnc(hEncCore->origSpectrum, hEncCore->frameLength, hEncCore->transformType,
            hEncCore->groupIndicator, &hEncCore->numGroups);

        numGroups[i] = hEncCore->numGroups;
    }
    // write grouping bitstream
    WriteGroupBitstream(stAvs3, STEREO_CHANNELS, stAvs3->bitstream, &stAvs3->totalSideBits);

    // bit allocation
    ComputeBitsRatio(hEncCoreL->origSpectrum, hEncCoreR->origSpectrum, frameLength, hMdctStereo->isMS, &hMdctStereo->bitsRatio);

    // write stereo bits
    WriteStereoBitstream(stAvs3, stAvs3->bitstream, &stAvs3->totalSideBits);

    // calculate available bits
#ifndef SUPPORT_NNTYPE_LC
    availableBits = GetAvailableBits(stAvs3->bitsPerFrame, stAvs3->totalSideBits, numGroups, STEREO_CHANNELS);
#else
#ifndef SUPPORT_HIGH_BR_MIX
    availableBits = GetAvailableBits(stAvs3->bitsPerFrame, stAvs3->totalSideBits, numGroups, STEREO_CHANNELS, stAvs3->nnTypeConfig);
#else
    availableBits = (short)GetAvailableBits(stAvs3->bitsPerFrame, stAvs3->totalSideBits, numGroups, STEREO_CHANNELS, stAvs3->nnTypeConfig);
#endif
#endif

    // allcoation bits between channels
    StereoBitsAllocation(availableBits, stAvs3->hMdctStereo->bitsRatio, channelBytes);

    return;
}


#ifdef MCR_INTEGRATE

void Avs3StereoMcrEncoder(
    AVS3EncoderHandle stAvs3,
    short* channelBytes
)
{
    int16_t numGroups;                      // num groups for each channel
    short availableBits = 0;

    AVS3_STEREO_ENC_HANDLE hMdctStereo = stAvs3->hMdctStereo;
    AVS3_ENC_CORE_HANDLE hEncCoreL = stAvs3->hEncCore[0];
    AVS3_ENC_CORE_HANDLE hEncCoreR = stAvs3->hEncCore[1];

    // MCR encoding process, use transformType for left channel
    McrEncode(&hMdctStereo->mcrData, &hMdctStereo->mcrConfig, hEncCoreL->origSpectrum, hEncCoreR->origSpectrum,
        hEncCoreL->transformType == ONLY_SHORT_WINDOW);

    // grouping for short window
    // only for left channel in mcr mode
    SpectrumGroupingEnc(hEncCoreL->origSpectrum, hEncCoreL->frameLength, hEncCoreL->transformType,
        hEncCoreL->groupIndicator, &hEncCoreL->numGroups);
    numGroups = hEncCoreL->numGroups;

    // write grouping bitstream
    // only for left channel in mcr mode
    WriteGroupBitstream(stAvs3, 1, stAvs3->bitstream, &stAvs3->totalSideBits);

    // write stereo bits
    WriteStereoBitstream(stAvs3, stAvs3->bitstream, &stAvs3->totalSideBits);

    // calculate available bits
    // channel num is 1 for mcr mode
#ifndef SUPPORT_NNTYPE_LC
    availableBits = GetAvailableBits(stAvs3->bitsPerFrame, stAvs3->totalSideBits, &numGroups, 1);
#else
#ifndef SUPPORT_HIGH_BR_MIX
    availableBits = GetAvailableBits(stAvs3->bitsPerFrame, stAvs3->totalSideBits, &numGroups, 1, stAvs3->nnTypeConfig);
#else
    availableBits = (short)GetAvailableBits(stAvs3->bitsPerFrame, stAvs3->totalSideBits, &numGroups, 1, stAvs3->nnTypeConfig);
#endif
#endif

    // allcoation bits between channels
    // all bits for left channel in mcr mode
    channelBytes[0] = (short)floor((float)availableBits / 8.0f);
    channelBytes[1] = 0;

    return;
}

#endif