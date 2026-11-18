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
#include "avs3_stat_com.h"

#ifdef NEURAL_QC

/*
Init quantizer structure
I/O params:
    FILE *fModel                        (i)   model file handle
    QuantizerHandle quantizerHandle     (i/o) quantizer handle
    int16_t numChannels                 (i)   channel number for latent
*/
int16_t InitQuantizer(
    FILE *fModel,
    QuantizerHandle quantizerHandle,
    int16_t numChannels
)
{
    float tmp;

    // get number of feature channels for quantization
    quantizerHandle->numChannels = numChannels;

    // get quantile medians
    quantizerHandle->quantileMedian = (float *)malloc(sizeof(float) * quantizerHandle->numChannels);
    if (quantizerHandle->quantileMedian == NULL) {
        fprintf(stderr, "Malloc quantile median error!\n");
        exit(-1);
    }
    for (int i = 0; i < quantizerHandle->numChannels; i++) {
        fread(&tmp, sizeof(float), 1, fModel);
        quantizerHandle->quantileMedian[i] = tmp;
    }

    return 0;
}


/*
Latent parameter quantization
I/O params:
    QuantizerHandle quantizerHandle         (i) quantizer handle, include quantization offset
    float **featureIn                       (i) input feature map, size: featureDim * numChannels
    int32_t **featureOut                    (o) output feature map, size: featureDime * numChannels
    int16_t featureDim                      (i) feature dim
    int16_t numChannels                     (i) number channels
*/
int16_t LatentQuantize(
    QuantizerHandle quantizerHandle,
    float **featureIn,
    int32_t **featureOut,
    int16_t featureDim,
    int16_t numChannels
)
{
    float tmp;
    float half = 0.5f;

    // check number of channels
    if (numChannels != quantizerHandle->numChannels) {
        fprintf(stderr, "The channel number of input feature does not match quantizer's numChannels!!\n");
        exit(-1);
    }

    // loop over each dim
    for (int16_t i = 0; i < featureDim; i++) {
        for (int16_t j = 0; j < numChannels; j++) {
            tmp = featureIn[i][j] + half - quantizerHandle->quantileMedian[j];
            featureOut[i][j] = (int32_t)(floor(tmp));
        }
    }

    return 0;
}


/*
Latent parameter dequantization
I/O params:
    QuantizerHandle quantizerHandle         (i) quantizer handle
    int32_t **featureIn                     (i) input feature map
    float **featureOut                      (o) output feature map
    int16_t featureDim                      (i) feature dim
    int16_t numChannels                     (i) number channels
*/
int16_t LatentDequantize(
    QuantizerHandle quantizerHandle,
    int32_t **featureIn,
    float **featureOut,
    int16_t featureDim,
    int16_t numChannels
)
{
    // check number of channels
    if (numChannels != quantizerHandle->numChannels) {
        fprintf(stderr, "The channel number of input feature does not match quantizer's numChannels!!\n");
        exit(-1);
    }

    // loop over each dim
    for (int16_t i = 0; i < featureDim; i++) {
        for (int16_t j = 0; j < numChannels; j++) {
            featureOut[i][j] = (float)featureIn[i][j] + quantizerHandle->quantileMedian[j];
        }
    }

    return 0;
}


int16_t DestroyQuantizer(
    QuantizerHandle quantizerHandle
)
{
    free(quantizerHandle->quantileMedian);
    quantizerHandle->quantileMedian = NULL;

    return 0;
}

#endif