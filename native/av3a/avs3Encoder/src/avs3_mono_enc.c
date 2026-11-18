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

#ifdef MONO_INTEGRATE

void Avs3MonoEncoder(
    AVS3EncoderHandle stAvs3,
    short* channelBytes
)
{
    short availableBits = 0;
    short availableBytes = 0;

    int16_t numGroups;
    AVS3_ENC_CORE_HANDLE hEncCore = stAvs3->hEncCore[0];

    // grouping for short window
    SpectrumGroupingEnc(hEncCore->origSpectrum, hEncCore->frameLength, hEncCore->transformType,
        hEncCore->groupIndicator, &hEncCore->numGroups);
    numGroups = hEncCore->numGroups;

    // write grouping bitstream
    WriteGroupBitstream(stAvs3, 1, stAvs3->bitstream, &stAvs3->totalSideBits);

    // get num of available bytes
#ifndef SUPPORT_NNTYPE_LC
    availableBits = GetAvailableBits(stAvs3->bitsPerFrame, stAvs3->totalSideBits, &numGroups, 1);
#else
#ifndef SUPPORT_HIGH_BR_MIX
    availableBits = GetAvailableBits(stAvs3->bitsPerFrame, stAvs3->totalSideBits, &numGroups, 1, stAvs3->nnTypeConfig);
#else
    availableBits = (short)GetAvailableBits(stAvs3->bitsPerFrame, stAvs3->totalSideBits, &numGroups, 1, stAvs3->nnTypeConfig);
#endif
#endif
    availableBytes = (short)floor((float)availableBits / 8.0f);

    channelBytes[0] = availableBytes;

    return;
}

#endif