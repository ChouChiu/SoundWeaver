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

#include <stdio.h>
#include <stdlib.h>
#include <assert.h>

#include "avs3_options.h"
#include "avs3_cnst_com.h"
#include "avs3_rom_com.h"
#include "avs3_prot_com.h"


#ifdef FD_SHAPING

/*
reorder the quanted LSF coefficients
I/O params:
    float *quantLsf                                 (i/o) original/reordered quanted LSF coefficients
    const float minGap                              (i)   minimal gap between two LSF coefficients
    const short lpcOrder                            (i)   LSP order
*/
static void QuantLsfReorder(
    float *quantLsf, 
    const float minGap, 
    const short lpcOrder
)
{
    short i;
    float minLsf, maxLsf;

    minLsf = minGap;
    for (i = 0; i < lpcOrder; i++) {
        if (quantLsf[i] < minLsf) {
            quantLsf[i] = minLsf;
        }
        minLsf = quantLsf[i] + minGap;
    }

    maxLsf = (float)(AVS3_SAMPLING_48KHZ / 2) - minGap;
    for (i = lpcOrder - 1; i >= 0; i--) {
        if (quantLsf[i] > maxLsf) {
            quantLsf[i] = maxLsf;
        }
        maxLsf = quantLsf[i] - minGap;
    }

    return;
}


/*
decode the LSF coefficients in high bitrate using 46bit CB
I/O params:
    const short *vqIndex                            (i/o) the index of codebook for the LSF coefficients
    float *quantLsf                                 (o)   quanted LSF coefficients
    const short lpcOrder                            (i)   LSP order
*/
void LsfQuantDecHbr(
    const short *vqIndex, 
    float *quantLsf, 
    const short lpcOrder
)
{
    short i;
    float decLsf[LPC_ORDER];

    SetZero(decLsf, LPC_ORDER);
    for (i = 0; i < LSF_STAGE1_LEN1_HBR; i++) {
        decLsf[i] = lsf_stage1_CB1_hbr[vqIndex[0] * LSF_STAGE1_LEN1_HBR + i];
    }

    for (i = LSF_STAGE1_LEN1_HBR; i < LPC_ORDER; i++) {
        decLsf[i] = lsf_stage1_CB2_hbr[vqIndex[1] * LSF_STAGE1_LEN2_HBR + (i - LSF_STAGE1_LEN1_HBR)];
    }

    for (i = 0; i < LSF_STAGE2_LEN1_HBR; i++) {
        decLsf[i] += lsf_stage2_CB1_hbr[vqIndex[2] * LSF_STAGE2_LEN1_HBR + i];
        decLsf[LSF_STAGE2_LEN1_HBR + i] += lsf_stage2_CB2_hbr[vqIndex[3] * LSF_STAGE2_LEN2_HBR + i];
        decLsf[2 * LSF_STAGE2_LEN1_HBR + i] += lsf_stage2_CB3_hbr[vqIndex[4] * LSF_STAGE2_LEN3_HBR + i];
        decLsf[3 * LSF_STAGE2_LEN1_HBR + i] += lsf_stage2_CB4_hbr[vqIndex[5] * LSF_STAGE2_LEN4_HBR + i];
    }

    for (i = 0; i < LSF_STAGE2_LEN5_HBR; i++) {
        decLsf[4 * LSF_STAGE2_LEN1_HBR + i] += lsf_stage2_CB5_hbr[vqIndex[6] * LSF_STAGE2_LEN5_HBR + i];
    }

    for (i = 0; i < LPC_ORDER; i++) {
        quantLsf[i] = decLsf[i] + mean_lsf[i];
    }

    QuantLsfReorder(quantLsf, LSF_MIN_GAP, LPC_ORDER);

    return;
}


/*
decode the LSF coefficients in low bitrate with 36 bit CB
I/O params:
const short *vqIndex                            (i/o) the index of codebook for the LSF coefficients
float *quantLsf                                 (o)   quanted LSF coefficients
const short lpcOrder                            (i)   LSP order
*/
void LsfQuantDecLbr(
    const short *vqIndex,
    float *quantLsf,
    const short lpcOrder
)
{
    short i;
    float decLsf[LPC_ORDER];

    SetZero(decLsf, LPC_ORDER);
    for (i = 0; i < LSF_STAGE1_LEN1_LBR; i++) {
        decLsf[i] = lsf_stage1_CB1_lbr[vqIndex[0] * LSF_STAGE1_LEN1_LBR + i];
    }

    for (i = LSF_STAGE1_LEN1_LBR; i < LPC_ORDER; i++) {
        decLsf[i] = lsf_stage1_CB2_lbr[vqIndex[1] * LSF_STAGE1_LEN2_LBR + (i - LSF_STAGE1_LEN1_LBR)];
    }

    for (i = 0; i < LSF_STAGE2_LEN1_LBR; i++) {
        decLsf[i] += lsf_stage2_CB1_lbr[vqIndex[2] * LSF_STAGE2_LEN1_LBR + i];
    }

    for (i = 0; i < LSF_STAGE2_LEN2_LBR; i++) {
        decLsf[LSF_STAGE2_LEN1_LBR + i] += lsf_stage2_CB2_lbr[vqIndex[3] * LSF_STAGE2_LEN2_LBR + i];
    }
    for (i = 0; i < LSF_STAGE2_LEN3_LBR; i++) {
        decLsf[LSF_STAGE2_LEN1_LBR + LSF_STAGE2_LEN2_LBR + i] += lsf_stage2_CB3_lbr[vqIndex[4] * LSF_STAGE2_LEN3_LBR + i];
    }

    for (i = 0; i < LPC_ORDER; i++) {
        quantLsf[i] = decLsf[i] + mean_lsf[i];
    }

    QuantLsfReorder(quantLsf, LSF_MIN_GAP, LPC_ORDER);
    return;
}


/*
LSF Dequantization function, using different CB
I/O params:
const short *vqIndex                            (i/o) the index of codebook for the LSF coefficients
float *quantLsf                                 (o)   quanted LSF coefficients
const short lpcOrder                            (i)   LSP order
const short lsfLbrFlag                          (i)   low bitrate flag for LSF quantization
*/
void LsfQuantDec(
    const short *vqIndex,
    float *quantLsf,
    const short lpcOrder,
    const short lsfLbrFlag
)
{
    if (lsfLbrFlag == 0) {
        // high bitrate, using 46bit CB
        LsfQuantDecHbr(vqIndex, quantLsf, lpcOrder);
    }
    else if (lsfLbrFlag == 1) {
        // low bitrate, using 36bit CB
        LsfQuantDecLbr(vqIndex, quantLsf, lpcOrder);
    }

    return;
}

#endif