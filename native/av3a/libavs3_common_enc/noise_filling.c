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
#include <float.h>

#include "avs3_options.h"
#include "avs3_cnst_com.h"
#include "avs3_stat_com.h"
#include "avs3_prot_com.h"

#ifdef NEURAL_QC

#ifdef DEBUG_SAVE
extern FILE *fQuant;
#endif


/*
Extract noise filling parameter using latent feature before and after quantization
I/O params:
    float **origLatent              (i) orignal latent feature
    int32_t **quantizedLatent       (i) quantized latent feature
    float *quantileMedian           (i) quantile median for quantizer
    int16_t numLatentDim            (i) dimension of latent feature
    int16_t numLatentChannels       (i) num channels of latent feature
    int16_t numNfDim                (i) dimension of latent feature for NF param extraction
    int16_t numGroups               (i) number of groups for current frame
    int16_t *groupIndicator         (i) group indicator vector, 0 for transient, 1 for others
    float *nfParamQ                 (o) quantized noise filling param
    int16_t *nfParamQIdx            (o) quantization index for noise filling param
*/
void ExtractNfParam(
    float **origLatent,
    int32_t **quantizedLatent,
    float *quantileMedian,
    int16_t numLatentDim,
    int16_t numLatentChannels,
    int16_t numNfDim,
    int16_t numGroups,
    int16_t *groupIndicator,
    float *nfParamQ,
    int16_t *nfParamQIdx
)
{
    int16_t numZeroedParam = 0;
    int16_t numTransientBlock = 0;                          // number of transient blocks
    int16_t numOtherBlock = 0;                              // number of other blocks
    int16_t startIdx[N_GROUP_SHORT_WIN] = { 0 };
    int16_t endIdx[N_GROUP_SHORT_WIN] = { 0 };
    float nfParam[N_GROUP_SHORT_WIN] = { 0.0f };
    float tmp;

    if (numGroups == 1) {
        startIdx[0] = 0;
        startIdx[1] = 0;
        endIdx[0] = numNfDim;
        endIdx[1] = numNfDim;
    }
    else {
        for (int16_t i = 0; i < N_BLOCK_SHORT; i++) {
            if (groupIndicator[i] == 0) {
                numTransientBlock++;
            }
            else {
                numOtherBlock++;
            }
        }

        startIdx[0] = 0;
        endIdx[0] = (short)((float)numNfDim / N_BLOCK_SHORT * numTransientBlock);

        startIdx[1] = numLatentDim / N_BLOCK_SHORT * numTransientBlock;
        endIdx[1] = startIdx[1] + (short)((float)numNfDim / N_BLOCK_SHORT * numOtherBlock);
    }

    for (int16_t group = 0; group < numGroups; group++) {

        // for latent param quantized to zero, sum abs error
        for (int16_t i = 0; i < numLatentChannels; i++) {

            // sum in each channel
            tmp = 0.0f;
            numZeroedParam = 0;

            for (int16_t j = startIdx[group]; j < endIdx[group]; j++) {
                if (quantizedLatent[j][i] == 0) {
                    numZeroedParam++;
                    tmp += (float)fabs(origLatent[j][i] - quantileMedian[i]);
                }
            }

            // add channel average to nf param
            if (numZeroedParam == 0) {
                nfParam[group] += 0.0f;
            }
            else {
                nfParam[group] += tmp / numZeroedParam;
            }
        }

        // average over channels
        nfParam[group] /= numLatentChannels;

        // quantization
        // 3 bit uniform Q, [0, almost 0.30]
        nfParamQIdx[group] = (int16_t)(floor(0.5f + nfParam[group] * 23.34f));
#ifdef BUGFIX_LOCAL_SYNTH
        nfParamQIdx[group] = AVS3_MAX(nfParamQIdx[group], 0);
        nfParamQIdx[group] = AVS3_MIN(nfParamQIdx[group], ((1 << NBITS_NF_PARAM) - 1));
#endif
        nfParamQ[group] = (float)(nfParamQIdx[group]) / 23.34f;
    }

    return;
}


void NfParamPostProc(
    NeuralQcData *neuralQcData,
    float *mdctSpectrum,
    int16_t numLinesForNf,
    int32_t totalBitrate,
    int16_t nChans
)
{
    int32_t bitratePerCh;
    float mdctEnerSpec[BLOCK_LEN_LONG] = { 0.0f };
    float logMdctEnerSpec[BLOCK_LEN_LONG] = { 0.0f };
    float sfm, peakAvgRatio;

    bitratePerCh = (int32_t)(totalBitrate / (float)nChans);

    if (bitratePerCh <= 32000) {

        // get ener spec and log ener spec for core band
        for (int16_t i = 0; i < numLinesForNf; i++) {
            mdctEnerSpec[i] = mdctSpectrum[i] * mdctSpectrum[i];
            logMdctEnerSpec[i] = max(0.0f, (float)(log10(max(FLT_MIN, mdctEnerSpec[i])) / log10(2.0)));
        }

        // get sfm and peak-average ratio for core band
        sfm = BweGetSfm(mdctEnerSpec, logMdctEnerSpec, 0, numLinesForNf);
        peakAvgRatio = BweGetPeakAvgRatio(logMdctEnerSpec, 0, numLinesForNf);

        // get final sfm param
        sfm /= peakAvgRatio;

        // if sfm smaller than threshold, lower NF param by one step
        if (sfm < 0.003f) {
            for (int16_t i = 0; i < N_GROUP_SHORT_WIN; i++) {
                neuralQcData->nfParamQIdx[i] = max(0, neuralQcData->nfParamQIdx[i] - 1);
                // dequantize noise filling parameter
                neuralQcData->nfParam[i] = (float)(neuralQcData->nfParamQIdx[i]) / 23.34f;
            }
        }
    }

    return;
}


/*
Add generated noise to dequantized latent feature
I/O params:
    float **dequantizedLatent           (i/o) dequantized latent feature
    float *quantileMedian               (i)   quantile median for quantizer
    int16_t numLatentDim                (i)   dimension of latent feature
    int16_t numLatentChannels           (i)   number of channels for latent feature
    int16_t numNfDim                    (i)   dimension of latent feature for NF param extraction
    int16_t numGroups                   (i)   number of groups for current frame
    int16_t *groupIndicator             (i)   group indicator vector, 0 for transient, 1 for others
    float *nfParamQ                     (o)   quantized noise filling param
    int16_t nfParamQIdx                 (i)   quantization index of noise filling param
*/
void LatentNoiseFilling(
    float **dequantizedLatent,
    float *quantileMedian,
    int16_t numLatentDim,
    int16_t numLatentChannels,
    int16_t numNfDim,
    int16_t numGroups,
    int16_t *groupIndicator,
    float *nfParamQ,
    int16_t *nfParamQIdx
)
{
    float noise;
    int16_t numTransientBlock = 0;                          // number of transient blocks
    int16_t numOtherBlock = 0;                              // number of other blocks
    int16_t startIdx[N_GROUP_SHORT_WIN] = { 0 };
    int16_t endIdx[N_GROUP_SHORT_WIN] = { 0 };

    if (numGroups == 1) {
        startIdx[0] = 0;
        startIdx[1] = 0;
        endIdx[0] = numNfDim;
        endIdx[1] = numNfDim;
    }
    else {
        for (int16_t i = 0; i < N_BLOCK_SHORT; i++) {
            if (groupIndicator[i] == 0) {
                numTransientBlock++;
            }
            else {
                numOtherBlock++;
            }
        }

        startIdx[0] = 0;
        endIdx[0] = (short)((float)numNfDim / N_BLOCK_SHORT * numTransientBlock);

        startIdx[1] = numLatentDim / N_BLOCK_SHORT * numTransientBlock;
        endIdx[1] = startIdx[1] + (short)((float)numNfDim / N_BLOCK_SHORT * numOtherBlock);
    }

    for (int16_t group = 0; group < numGroups; group++) {

        // dequantize noise filling parameter
        nfParamQ[group] = (float)(nfParamQIdx[group]) / 23.34f;

        // add noise to dequantized latent
        // if dequantized value equals quantile median
        for (int16_t i = startIdx[group]; i < endIdx[group]; i++) {
            for (int16_t j = 0; j < numLatentChannels; j++) {
                if (dequantizedLatent[i][j] == quantileMedian[j]) {
                    // generate base noise, [-1, 1]
                    noise = (float)rand() / (float)RAND_MAX;
                    noise = noise * 2.0f - 1.0f;
                    // apply nf param
                    noise *= nfParamQ[group];
                    // add noise
                    dequantizedLatent[i][j] += noise;
                }
            }
        }
    }

    return;
}

#endif