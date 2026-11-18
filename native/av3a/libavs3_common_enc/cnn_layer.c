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

#include "avs3_options.h"
#include "avs3_stat_com.h"
#include "avs3_prot_com.h"

#ifdef NEURAL_QC

/*
Malloc runtime buffers for cnn layer
Note: for conv1D and conv1DTranspose, size of buffers are different
I/O params:
    CnnStructHandle cnnLayer    (i/o) CNN layer structure
*/
void CnnMallocRuntimeBuffer(
    CnnStructHandle cnnLayer
)
{
    int16_t paddingSize;
    int16_t featDimPadded;
    int16_t featDimInterPolated;

    if (cnnLayer->isTranspose == 0) {

        // get total padding size
        paddingSize = (cnnLayer->featDimOut - 1) * cnnLayer->stride + cnnLayer->kernelSize - cnnLayer->featDimIn;
        // get padded feat dim
        featDimPadded = cnnLayer->featDimIn + paddingSize;

        // init InterPolated feature to NULL
        cnnLayer->featureInterPolated = NULL;

        // malloc padded feature
        cnnLayer->featurePadded = (float **)malloc(sizeof(float *) * featDimPadded);
        for (int16_t i = 0; i < featDimPadded; i++) {
            cnnLayer->featurePadded[i] = (float *)malloc(sizeof(float) * cnnLayer->numChannelsIn);
        }

        // malloc columned feature
        cnnLayer->featureCol = (float **)malloc(sizeof(float *) * cnnLayer->featDimIn / cnnLayer->stride);
        for (int16_t i = 0; i < cnnLayer->featDimIn / cnnLayer->stride; i++) {
            cnnLayer->featureCol[i] = (float *)malloc(sizeof(float) * cnnLayer->kernelSize * cnnLayer->numChannelsIn);
        }

        cnnLayer->kernelCol = (float **)malloc(sizeof(float *) * cnnLayer->kernelSize * cnnLayer->numChannelsIn);
        for (int16_t i = 0; i < cnnLayer->kernelSize * cnnLayer->numChannelsIn; i++) {
            cnnLayer->kernelCol[i] = (float *)malloc(sizeof(float) * cnnLayer->numChannelsOut);
        }
    }
    else if (cnnLayer->isTranspose == 1) {

#ifndef CONV_TRANS_2PART
        // get InterPolated feat dim
        featDimInterPolated = cnnLayer->featDimIn * cnnLayer->stride;
        // get total padding size
        paddingSize = (cnnLayer->featDimOut - 1) + cnnLayer->kernelSize - featDimInterPolated;
        // get padded feat dim
        featDimPadded = featDimInterPolated + paddingSize;

        // malloc InterPolated feature
        cnnLayer->featureInterPolated = (float **)malloc(sizeof(float *) * featDimInterPolated);
        for (int16_t i = 0; i < featDimInterPolated; i++) {
            cnnLayer->featureInterPolated[i] = (float *)malloc(sizeof(float) * cnnLayer->numChannelsIn);
        }

        // malloc padded feature
        cnnLayer->featurePadded = (float **)malloc(sizeof(float *) * featDimPadded);
        for (int16_t i = 0; i < featDimPadded; i++) {
            cnnLayer->featurePadded[i] = (float *)malloc(sizeof(float) * cnnLayer->numChannelsIn);
        }

        // malloc columned feature
        cnnLayer->featureCol = (float **)malloc(sizeof(float *) * cnnLayer->featDimIn * cnnLayer->stride);
        for (int16_t i = 0; i < cnnLayer->featDimIn * cnnLayer->stride; i++) {
            cnnLayer->featureCol[i] = (float *)malloc(sizeof(float) * cnnLayer->kernelSize * cnnLayer->numChannelsIn);
        }

        // malloc columned kernel
        cnnLayer->kernelCol = (float **)malloc(sizeof(float *) * cnnLayer->kernelSize * cnnLayer->numChannelsIn);
        for (int16_t i = 0; i < cnnLayer->kernelSize * cnnLayer->numChannelsIn; i++) {
            cnnLayer->kernelCol[i] = (float *)malloc(sizeof(float) * cnnLayer->numChannelsOut);
        }
#else

        // Condition 1: stride is 1
        // use conventional conv1DTranspose, 4 buffers
        // interpolated, padded, featureCol, kernelCol
        if (cnnLayer->stride == 1) {
            // get InterPolated feat dim
            featDimInterPolated = cnnLayer->featDimIn * cnnLayer->stride;
            // get total padding size
            paddingSize = (cnnLayer->featDimOut - 1) + cnnLayer->kernelSize - featDimInterPolated;
            // get padded feat dim
            featDimPadded = featDimInterPolated + paddingSize;

            // malloc InterPolated feature
            cnnLayer->featureInterPolated = (float **)malloc(sizeof(float *) * featDimInterPolated);
            for (int16_t i = 0; i < featDimInterPolated; i++) {
                cnnLayer->featureInterPolated[i] = (float *)malloc(sizeof(float) * cnnLayer->numChannelsIn);
            }

            // malloc padded feature
            cnnLayer->featurePadded = (float **)malloc(sizeof(float *) * featDimPadded);
            for (int16_t i = 0; i < featDimPadded; i++) {
                cnnLayer->featurePadded[i] = (float *)malloc(sizeof(float) * cnnLayer->numChannelsIn);
            }

            // malloc columned feature
            cnnLayer->featureCol = (float **)malloc(sizeof(float *) * cnnLayer->featDimIn * cnnLayer->stride);
            for (int16_t i = 0; i < cnnLayer->featDimIn * cnnLayer->stride; i++) {
                cnnLayer->featureCol[i] = (float *)malloc(sizeof(float) * cnnLayer->kernelSize * cnnLayer->numChannelsIn);
            }

            // malloc columned kernel
            cnnLayer->kernelCol = (float **)malloc(sizeof(float *) * cnnLayer->kernelSize * cnnLayer->numChannelsIn);
            for (int16_t i = 0; i < cnnLayer->kernelSize * cnnLayer->numChannelsIn; i++) {
                cnnLayer->kernelCol[i] = (float *)malloc(sizeof(float) * cnnLayer->numChannelsOut);
            }
        }

        // Condition 2: stride is 2
        // use two part conv1DTranspose (even / odd), 7 buffers
        if (cnnLayer->stride == 2) {
            // get total padding size
            paddingSize = 2;                                        // 2, fixed padding size
            // get padded feat dim
            featDimPadded = cnnLayer->featDimIn + paddingSize;

            // malloc padded feature
            cnnLayer->featurePadded = (float **)malloc(sizeof(float *) * featDimPadded);
            for (int16_t i = 0; i < featDimPadded; i++) {
                cnnLayer->featurePadded[i] = (float *)malloc(sizeof(float) * cnnLayer->numChannelsIn);
            }

            // Odd/even kernelCol
            cnnLayer->kernelColOdd = (float **)malloc(sizeof(float *) * (cnnLayer->kernelSize + 1) / 2 * cnnLayer->numChannelsIn);
            for (int16_t i = 0; i < (cnnLayer->kernelSize + 1) / 2 * cnnLayer->numChannelsIn; i++) {
                cnnLayer->kernelColOdd[i] = (float *)malloc(sizeof(float) * cnnLayer->numChannelsOut);
            }

            cnnLayer->kernelColEven = (float **)malloc(sizeof(float *) * (cnnLayer->kernelSize - 1) / 2 * cnnLayer->numChannelsIn);
            for (int16_t i = 0; i < (cnnLayer->kernelSize - 1) / 2 * cnnLayer->numChannelsIn; i++) {
                cnnLayer->kernelColEven[i] = (float *)malloc(sizeof(float) * cnnLayer->numChannelsOut);
            }

            // Odd/even featureCol
            cnnLayer->featureColOdd = (float **)malloc(sizeof(float *) * cnnLayer->featDimIn);
            for (int16_t i = 0; i < cnnLayer->featDimIn; i++) {
                cnnLayer->featureColOdd[i] = (float *)malloc(sizeof(float) * (cnnLayer->kernelSize + 1) / 2 * cnnLayer->numChannelsIn);
            }

            cnnLayer->featureColEven = (float **)malloc(sizeof(float *) * cnnLayer->featDimIn);
            for (int16_t i = 0; i < cnnLayer->featDimIn; i++) {
                cnnLayer->featureColEven[i] = (float *)malloc(sizeof(float) * (cnnLayer->kernelSize - 1) / 2 * cnnLayer->numChannelsIn);
            }

            // Odd/even featOut, tmp buffer not used for next layer
            cnnLayer->featOutOdd = (float **)malloc(sizeof(float *) * cnnLayer->featDimIn);
            for (int16_t i = 0; i < cnnLayer->featDimIn; i++) {
                cnnLayer->featOutOdd[i] = (float *)malloc(sizeof(float) * cnnLayer->numChannelsOut);
            }

            cnnLayer->featOutEven = (float **)malloc(sizeof(float *) * cnnLayer->featDimIn);
            for (int16_t i = 0; i < cnnLayer->featDimIn; i++) {
                cnnLayer->featOutEven[i] = (float *)malloc(sizeof(float) * cnnLayer->numChannelsOut);
            }
        }

#endif
    }
}


/*
Free runtime buffers for cnn layer
Note: for conv1D and conv1DTranspose, size of buffers are different
I/O params:
    CnnStructHandle cnnLayer    (i/o) CNN layer structure
*/
void CnnFreeRuntimeBuffer(
    CnnStructHandle cnnLayer
)
{
    int16_t paddingSize;
    int16_t featDimPadded;
    int16_t featDimInterPolated;

    if (cnnLayer->isTranspose == 0) {

        // get total padding size
        paddingSize = (cnnLayer->featDimOut - 1) * cnnLayer->stride + cnnLayer->kernelSize - cnnLayer->featDimIn;
        // get padded feat dim
        featDimPadded = cnnLayer->featDimIn + paddingSize;

        // free memory
        for (int16_t i = 0; i < featDimPadded; i++) {
            free(cnnLayer->featurePadded[i]);
            cnnLayer->featurePadded[i] = NULL;
        }
        free(cnnLayer->featurePadded);
        cnnLayer->featurePadded = NULL;

        for (int16_t i = 0; i < cnnLayer->featDimIn / cnnLayer->stride; i++) {
            free(cnnLayer->featureCol[i]);
            cnnLayer->featureCol[i] = NULL;
        }
        free(cnnLayer->featureCol);
        cnnLayer->featureCol = NULL;

        for (int16_t i = 0; i < cnnLayer->kernelSize * cnnLayer->numChannelsIn; i++) {
            free(cnnLayer->kernelCol[i]);
            cnnLayer->kernelCol[i] = NULL;
        }
        free(cnnLayer->kernelCol);
        cnnLayer->kernelCol = NULL;
    }
    else if (cnnLayer->isTranspose == 1) {

#ifndef CONV_TRANS_2PART
        // get InterPolated feat dim
        featDimInterPolated = cnnLayer->featDimIn * cnnLayer->stride;
        // get total padding size
        paddingSize = (cnnLayer->featDimOut - 1) + cnnLayer->kernelSize - featDimInterPolated;
        // get padded feat dim
        featDimPadded = featDimInterPolated + paddingSize;

        // free memory
        for (int16_t i = 0; i < featDimInterPolated; i++) {
            free(cnnLayer->featureInterPolated[i]);
            cnnLayer->featureInterPolated[i] = NULL;
        }
        free(cnnLayer->featureInterPolated);
        cnnLayer->featureInterPolated = NULL;

        for (int16_t i = 0; i < featDimPadded; i++) {
            free(cnnLayer->featurePadded[i]);
            cnnLayer->featurePadded[i] = NULL;
        }
        free(cnnLayer->featurePadded);
        cnnLayer->featurePadded = NULL;

        for (int16_t i = 0; i < cnnLayer->featDimIn / cnnLayer->stride; i++) {
            free(cnnLayer->featureCol[i]);
            cnnLayer->featureCol[i] = NULL;
        }
        free(cnnLayer->featureCol);
        cnnLayer->featureCol = NULL;

        for (int16_t i = 0; i < cnnLayer->kernelSize * cnnLayer->numChannelsIn; i++) {
            free(cnnLayer->kernelCol[i]);
            cnnLayer->kernelCol[i] = NULL;
        }
        free(cnnLayer->kernelCol);
        cnnLayer->kernelCol = NULL;
#else

        if (cnnLayer->stride == 1) {
            // get InterPolated feat dim
            featDimInterPolated = cnnLayer->featDimIn * cnnLayer->stride;
            // get total padding size
            paddingSize = (cnnLayer->featDimOut - 1) + cnnLayer->kernelSize - featDimInterPolated;
            // get padded feat dim
            featDimPadded = featDimInterPolated + paddingSize;

            // free memory
            for (int16_t i = 0; i < featDimInterPolated; i++) {
                free(cnnLayer->featureInterPolated[i]);
                cnnLayer->featureInterPolated[i] = NULL;
            }
            free(cnnLayer->featureInterPolated);
            cnnLayer->featureInterPolated = NULL;

            for (int16_t i = 0; i < featDimPadded; i++) {
                free(cnnLayer->featurePadded[i]);
                cnnLayer->featurePadded[i] = NULL;
            }
            free(cnnLayer->featurePadded);
            cnnLayer->featurePadded = NULL;

            for (int16_t i = 0; i < cnnLayer->featDimIn / cnnLayer->stride; i++) {
                free(cnnLayer->featureCol[i]);
                cnnLayer->featureCol[i] = NULL;
            }
            free(cnnLayer->featureCol);
            cnnLayer->featureCol = NULL;

            for (int16_t i = 0; i < cnnLayer->kernelSize * cnnLayer->numChannelsIn; i++) {
                free(cnnLayer->kernelCol[i]);
                cnnLayer->kernelCol[i] = NULL;
            }
            free(cnnLayer->kernelCol);
            cnnLayer->kernelCol = NULL;
        }

        if (cnnLayer->stride == 2) {
            // get total padding size
            paddingSize = 2;                                        // 2, fixed padding size
            // get padded feat dim
            featDimPadded = cnnLayer->featDimIn + paddingSize;

            for (int16_t i = 0; i < featDimPadded; i++) {
                free(cnnLayer->featurePadded[i]);
                cnnLayer->featurePadded[i] = NULL;
            }
            free(cnnLayer->featurePadded);
            cnnLayer->featurePadded = NULL;

            for (int16_t i = 0; i < (cnnLayer->kernelSize + 1) / 2 * cnnLayer->numChannelsIn; i++) {
                free(cnnLayer->kernelColOdd[i]);
                cnnLayer->kernelColOdd[i] = NULL;
            }
            free(cnnLayer->kernelColOdd);
            cnnLayer->kernelColOdd = NULL;

            for (int16_t i = 0; i < (cnnLayer->kernelSize - 1) / 2 * cnnLayer->numChannelsIn; i++) {
                free(cnnLayer->kernelColEven[i]);
                cnnLayer->kernelColEven[i] = NULL;
            }
            free(cnnLayer->kernelColEven);
            cnnLayer->kernelColEven = NULL;

            for (int16_t i = 0; i < cnnLayer->featDimIn; i++) {
                free(cnnLayer->featureColOdd[i]);
                cnnLayer->featureColOdd[i] = NULL;
            }
            free(cnnLayer->featureColOdd);
            cnnLayer->featureColOdd = NULL;

            for (int16_t i = 0; i < cnnLayer->featDimIn; i++) {
                free(cnnLayer->featureColEven[i]);
                cnnLayer->featureColEven[i] = NULL;
            }
            free(cnnLayer->featureColEven);
            cnnLayer->featureColEven = NULL;

            for (int16_t i = 0; i < cnnLayer->featDimIn; i++) {
                free(cnnLayer->featOutOdd[i]);
                cnnLayer->featOutOdd[i] = NULL;
            }
            free(cnnLayer->featOutOdd);
            cnnLayer->featOutOdd = NULL;

            for (int16_t i = 0; i < cnnLayer->featDimIn; i++) {
                free(cnnLayer->featOutEven[i]);
                cnnLayer->featOutEven[i] = NULL;
            }
            free(cnnLayer->featOutEven);
            cnnLayer->featOutEven = NULL;
        }

#endif
    }
}


/*
Init CNN layer structure
I/O params:
    FILE *fModel                (i) model file
    CnnStructHandle cnnLayer    (o) CNN layer structure
    int16_t isTranspose         (i) flag for conv transpose
*/
int16_t InitCnnLayer(
    FILE *fModel,
    CnnStructHandle cnnLayer,
    int16_t isTranspose, 
    int16_t featDimIn
)
{
    int16_t padding;
    int16_t stride;
    int16_t useBias;
    int16_t activationFunc;
    int16_t kernelSize;
    int16_t numChannelsIn;
    int16_t numChannelsOut;
    float kernelCoef;
    float biasCoef;

    // set isTranspose flag
    cnnLayer->isTranspose = isTranspose;

    // get padding info
    fread(&padding, sizeof(int16_t), 1, fModel);
    if (padding == 0) {
        cnnLayer->padding = SAME;
    }
    else if (padding == 1) {
        cnnLayer->padding = VALID;
    }
    //fprintf(stdout, "Layer padding type: %d\n", cnnLayer->padding);

    // get stride
    fread(&stride, sizeof(int16_t), 1, fModel);
    cnnLayer->stride = stride;
    //fprintf(stdout, "Layer stride: %d\n", cnnLayer->stride);

    // get useBias
    fread(&useBias, sizeof(int16_t), 1, fModel);
    cnnLayer->useBias = useBias;
    //fprintf(stdout, "Layer useBias: %d\n", cnnLayer->useBias);

    // get activation function
    fread(&activationFunc, sizeof(int16_t), 1, fModel);
    cnnLayer->activationFunc = (TypeActFunc)activationFunc;
    //fprintf(stdout, "Layer activationFunc: %d\n", cnnLayer->activationFunc);

    // get kernel size
    fread(&kernelSize, sizeof(int16_t), 1, fModel);
    cnnLayer->kernelSize = kernelSize;
    //fprintf(stdout, "Layer kernel size: %d\n", cnnLayer->kernelSize);

    // get num channels in
    fread(&numChannelsIn, sizeof(int16_t), 1, fModel);
    cnnLayer->numChannelsIn = numChannelsIn;
    //fprintf(stdout, "Layer number channels in: %d\n", cnnLayer->numChannelsIn);

    // get num channels out
    fread(&numChannelsOut, sizeof(int16_t), 1, fModel);
    cnnLayer->numChannelsOut = numChannelsOut;
    //fprintf(stdout, "Layer number channels out: %d\n", cnnLayer->numChannelsOut);

    // get kernel parameters
    cnnLayer->kernel = NULL;

    int16_t chNum1, chNum2;
    if (cnnLayer->isTranspose == 0) {
        // conv, kernelSize * numChannelsIn * numChannelsOut
        chNum1 = cnnLayer->numChannelsIn;
        chNum2 = cnnLayer->numChannelsOut;
    }
    else {
        // conv transpose, kernelSize * numChannelsOut * numChannelsIn
        chNum1 = cnnLayer->numChannelsOut;
        chNum2 = cnnLayer->numChannelsIn;
    }

    // memory malloc
    cnnLayer->kernel = (float ***)malloc(sizeof(float**) * cnnLayer->kernelSize);
    for (int16_t i = 0; i < cnnLayer->kernelSize; i++) {
        cnnLayer->kernel[i] = (float **)malloc(sizeof(float *) * chNum1);
        for (int16_t j = 0; j < chNum1; j++) {
            cnnLayer->kernel[i][j] = (float *)malloc(sizeof(float) * chNum2);
        }
    }

    // read coefficients from file
    // dim 1: kernel size
    for (int16_t i = 0; i < cnnLayer->kernelSize; i++) {
        // dim 2: num channels input
        for (int16_t j = 0; j < chNum1; j++) {
            // dim 3: num channels output
            for (int16_t k = 0; k < chNum2; k++) {
                fread(&kernelCoef, sizeof(float), 1, fModel);
                cnnLayer->kernel[i][j][k] = kernelCoef;
            }
        }
    }

    // get bias parameters
    cnnLayer->bias = NULL;
    if (cnnLayer->useBias == 1) {
        cnnLayer->bias = (float *)malloc(sizeof(float) * cnnLayer->numChannelsOut);
        for (int16_t i = 0; i < cnnLayer->numChannelsOut; i++) {
            fread(&biasCoef, sizeof(float), 1, fModel);
            cnnLayer->bias[i] = biasCoef;
        }
    }

    // get GDN/DN related parameters
    // TBD: DN parameters
    cnnLayer->gdnActFuncParam = NULL;
    if (cnnLayer->activationFunc == GDN || cnnLayer->activationFunc == IGDN) {

        cnnLayer->gdnActFuncParam = (GdnActFuncHandle)malloc(sizeof(GdnActFuncStruct));
        if (cnnLayer->gdnActFuncParam == NULL) {
            fprintf(stderr, "Error in malloc GdnActFuncStruct in initCnnLayer func!!\n");
            exit(-1);
        }

        InitGdnParam(fModel, cnnLayer->gdnActFuncParam, cnnLayer->numChannelsOut);
    }

    // get in/output feature dim
    cnnLayer->featDimIn = featDimIn;
    if (cnnLayer->isTranspose == 0) {
        cnnLayer->featDimOut = featDimIn / cnnLayer->stride;
    }
    else {
        cnnLayer->featDimOut = featDimIn * cnnLayer->stride;
    }

    // malloc output feature buffer
    cnnLayer->featOut = (float **)malloc(sizeof(float *) * cnnLayer->featDimOut);
    for (int16_t i = 0; i < cnnLayer->featDimOut; i++) {
        cnnLayer->featOut[i] = (float *)malloc(sizeof(float) * cnnLayer->numChannelsOut);
    }

    // malloc runtime buffer for cnn
    CnnMallocRuntimeBuffer(cnnLayer);

    return 0;
}


/*
InterPolating for 1D feature
insert (stride-1) zeros before each dim of feature
I/O params:
    float **featureIn               (i) input feature map, size: featDimInterPolated/stride * numChannelsIn
    float **featureInterPolated     (o) interpolated feature map, size: featDimInterPolated * numChannelsIn
    int16_t featDimInterPolated     (i) feature dim after interpolation
    int16_t stride                  (i) stride
    int16_t numChannelsIn           (i) number of input channels
*/
static void InterPolating1D(
    float **featureIn,
    float **featureInterPolated,
    int16_t featDimInterPolated,
    int16_t stride,
    int16_t numChannelsIn
)
{

    // set InterPolated  feature to zero
    for (int16_t i = 0; i < featDimInterPolated; i++) {
        SetFloat(featureInterPolated[i], 0.0f, numChannelsIn);
    }

    // copy input feature to InterPolated feature
    for (int16_t i = 0; i < featDimInterPolated / stride; i++) {
        for (int16_t j = 0; j < numChannelsIn; j++) {
            featureInterPolated[stride * (i + 1) - 1][j] = featureIn[i][j];
        }
    }

    return;
}

/*
1D padding function for 'SAME'
I/O params:
    float **featureIn               (i) input feature map, size: featDimIn * numChannelsIn
    float **featurePadded           (o) padded feature map, size: (featDimIn + paddingSizeBegin + paddingSizeEnd) * numChannelsIn
    int16_t featDimIn               (i) input feature dim
    int16_t paddingSizeBegin        (i) padding size at the beginning
    int16_t paddingSizeEnd          (i) padding size at the end
    int16_t numChannelsIn           (i) channel of input feature
*/
static void PaddingSame1D(
    float **featureIn,
    float **featurePadded,
    int16_t featDimIn,
    int16_t paddingSizeBegin,
    int16_t paddingSizeEnd,
    int16_t numChannelsIn
)
{
    int16_t featDimPadded;

    featDimPadded = featDimIn + paddingSizeBegin + paddingSizeEnd;

    // set padded feature to zero
    for (int16_t i = 0; i < featDimPadded; i++) {
        SetFloat(featurePadded[i], 0.0f, numChannelsIn);
    }

    // copy input feature to padded feature
    for (int16_t i = paddingSizeBegin; i < featDimIn + paddingSizeBegin; i++) {
        for (int16_t j = 0; j < numChannelsIn; j++) {
            featurePadded[i][j] = featureIn[i - paddingSizeBegin][j];
        }
    }

    return;
}


/*
Feature to col conversion function for 1D
I/O params:
    float **featurePadded           (i) padded feature map, size: (featDimIn + paddingSize) * numChannelsIn
    float **featureCol              (o) columned feature map, size: (featDimIn/stride) * (kernelSize * numChannelsIn)
    int16_t featDimIn               (i) input feature dim
    int16_t kernelSize              (i) kernel size
    int16_t numChannelsIn           (i) number of input channels
    int16_t stride                  (i) stride
*/
static void FeatureToCol1D(
    float **featurePadded,
    float **featureCol,
    int16_t featDimIn,
    int16_t kernelSize,
    int16_t numChannelsIn,
    int16_t stride
)
{
    for (int16_t i = 0; i < (featDimIn / stride); i++) {
        for (int16_t j = 0; j < numChannelsIn; j++) {
            for (int16_t k = 0; k < kernelSize; k++) {
                featureCol[i][j * kernelSize + k] = featurePadded[i * stride + k][j];
            }
        }
    }

    return;
}


/*
Kernel to col conversion function for 1D
I/O params:
    float ***kernel                 (i) kernel param, size: kernelSize * numChannelsIn * numChannelsOut
    float **kernelCol               (o) columned kernel param, (kernelSize * numChannelsIn) * numChannelsOut
    int16_t kernelSize              (i) kernel size
    int16_t numChannelsIn           (i) number of input channels
    int16_t numChannelsOut          (i) number of output channels
*/
static void KernelToCol1D(
    float ***kernel,
    float **kernelCol,
    int16_t kernelSize,
    int16_t numChannelsIn,
    int16_t numChannelsOut
)
{
    for (int16_t i = 0; i < kernelSize; i++) {
        for (int16_t j = 0; j < numChannelsIn; j++) {
            for (int16_t k = 0; k < numChannelsOut; k++) {
                kernelCol[j * kernelSize + i][k] = kernel[i][j][k];
            }
        }
    }

    return;
}


/*
Kernel to col conversion function for 1D transpose conv
I/O params:
    float ***kernel                 (i) kernel param, size: kernelSize * numChannelsIn * numChannelsOut
    float **kernelCol               (o) columned kernel param, (kernelSize * numChannelsIn) * numChannelsOut
    int16_t kernelSize              (i) kernel size
    int16_t numChannelsIn           (i) number of input channels
    int16_t numChannelsOut          (i) number of output channels
Note:
    Difference with KernelToCol1D:
        kernelCol[j * kernelSize + i][k] = kernel[i][j][k] to:
        kernelCol[j * kernelSize + i][k] = kernel[kernelSize - i - 1][k][j]
    Important: Flip channel index and kernel index at the same time. 
*/
static void KernelToCol1DTranspose(
    float ***kernel,
    float **kernelCol,
    int16_t kernelSize,
    int16_t numChannelsIn,
    int16_t numChannelsOut
)
{
    for (int16_t i = 0; i < kernelSize; i++) {
        for (int16_t j = 0; j < numChannelsIn; j++) {
            for (int16_t k = 0; k < numChannelsOut; k++) {
                kernelCol[j * kernelSize + i][k] = kernel[kernelSize - i - 1][k][j];
            }
        }
    }

    return;
}


#ifdef CONV_TRANS_2PART
/*
Kernel to col conversion function for 1D transpose conv: Odd part of kernel
I/O params:
    float ***kernel                 (i) kernel param, size: kernelSize * numChannelsIn * numChannelsOut
    float **kernelCol               (o) columned kernel param, (kernelSize * numChannelsIn) * numChannelsOut
    int16_t kernelSize              (i) kernel size
    int16_t numChannelsIn           (i) number of input channels
    int16_t numChannelsOut          (i) number of output channels
Note:
    Difference with KernelToCol1D:
        kernelCol[j * kernelSize + i][k] = kernel[i][j][k] to:
        kernelCol[j * kernelSize + i][k] = kernel[kernelSize - i - 1][k][j]
    Important: Flip channel index and kernel index at the same time.
Difference to KernelToCol1DTranspose:
    i for kernel size, step is 2, start from 0
    kernelCol idx: i to i/2
    for odd part:
      if kernelSize = 3, odd part is 0 and 2
      if kernelSize = 5, odd part is 0, 2 and 4
*/
static void KernelToCol1DTransposeOdd(
    float ***kernel,
    float **kernelCol,
    int16_t kernelSize,
    int16_t numChannelsIn,
    int16_t numChannelsOut
)
{
    int16_t kernelSizeOdd = (kernelSize + 1) / 2;

    for (int16_t i = 0; i < kernelSize; i += 2) {
        for (int16_t j = 0; j < numChannelsIn; j++) {
            for (int16_t k = 0; k < numChannelsOut; k++) {
                kernelCol[j * kernelSizeOdd + i / 2][k] = kernel[kernelSize - i - 1][k][j];
            }
        }
    }

    return;
}


/*
Kernel to col conversion function for 1D transpose conv: Even part of kernel
I/O params:
    float ***kernel                 (i) kernel param, size: kernelSize * numChannelsIn * numChannelsOut
    float **kernelCol               (o) columned kernel param, (kernelSize * numChannelsIn) * numChannelsOut
    int16_t kernelSize              (i) kernel size
    int16_t numChannelsIn           (i) number of input channels
    int16_t numChannelsOut          (i) number of output channels
Note:
    Difference with KernelToCol1D:
        kernelCol[j * kernelSize + i][k] = kernel[i][j][k] to:
        kernelCol[j * kernelSize + i][k] = kernel[kernelSize - i - 1][k][j]
    Important: Flip channel index and kernel index at the same time.
Difference to KernelToCol1DTranspose:
    i for kernel size, step is 2, start from 1
    kernelCol idx: i to (i-1)/2
    for even part:
      if kernelSize = 3, even part is 1
      if kernelSize = 5, even part is 1, 3
*/
static void KernelToCol1DTransposeEven(
    float ***kernel,
    float **kernelCol,
    int16_t kernelSize,
    int16_t numChannelsIn,
    int16_t numChannelsOut
)
{
    int16_t kernelSizeEven = (kernelSize - 1) / 2;

    for (int16_t i = 1; i < kernelSize; i += 2) {
        for (int16_t j = 0; j < numChannelsIn; j++) {
            for (int16_t k = 0; k < numChannelsOut; k++) {
                kernelCol[j * kernelSizeEven + (i - 1) / 2][k] = kernel[kernelSize - i - 1][k][j];
            }
        }
    }

    return;
}
#endif


/*
Add bias to feature map
I/O params:
    float **feature             (i/o) feature map, in place add, size: featureDime * numChannels
    float *bias                 (i)   bias parameter, size: numChannels
    int16_t featureDim          (i)   feature dim
    int16_t numChannels         (i)   number of channels
*/
static void AddBias(
    float **feature,
    float *bias,
    int16_t featureDim,
    int16_t numChannels
)
{
    for (int16_t i = 0; i < featureDim; i++) {
        for (int16_t j = 0; j < numChannels; j++) {
            feature[i][j] += bias[j];
        }
    }

    return;
}


/*
1D convolution with stride and activation function
I/O params:
    CnnStructHandle cnnLayer        (i/o) cnn layer structure, include output feature buffer
    float **featureIn               (i)   input feature map, size: cnnLayer->featDimIn * cnnLayer->numChannelsIn
*/
int16_t Conv1D(
    CnnStructHandle cnnLayer,
    float **featureIn
)
{
    int16_t paddingSize;
    int16_t paddingSizeBegin, paddingSizeEnd;
    int16_t featDimPadded;
    float **featurePadded = NULL;
    float **featureCol = NULL;
    float **kernelCol = NULL;

    // padding
    if (cnnLayer->padding == VALID) {
        featurePadded = featureIn;
    }
    else if (cnnLayer->padding == SAME) {

        // get total padding size
        paddingSize = (cnnLayer->featDimOut - 1) * cnnLayer->stride + cnnLayer->kernelSize - cnnLayer->featDimIn;
        if (paddingSize < 0) {
            fprintf(stderr, "Error configuration in Conv1D, paddingSize < 0!!\n");
            exit(-1);
        }
        // get padded feat dim
        featDimPadded = cnnLayer->featDimIn + paddingSize;

        // get padding size at begin and end of feature
        if (paddingSize % 2 != 0) {
            // odd padding size, more padding at the end
            paddingSizeBegin = max((paddingSize - 1) / 2, 0);
            paddingSizeEnd = (paddingSize + 1) / 2;
        }
        else {
            // even padding size, same padding size at both side
            paddingSizeBegin = paddingSize / 2;
            paddingSizeEnd = paddingSize / 2;
        }

        // malloc padded feature
        featurePadded = cnnLayer->featurePadded;
        // perform padding
        PaddingSame1D(featureIn, featurePadded, cnnLayer->featDimIn, paddingSizeBegin, paddingSizeEnd, cnnLayer->numChannelsIn);
    }

#ifdef DEBUG_PRINT
    fprintf(stdout, "featurePadded:\n");
    for (int16_t i = 0; i < featDimPadded; i++) {
        for (int16_t j = 0; j < cnnLayer->numChannelsIn; j++) {
            fprintf(stdout, "%.6f, ", featurePadded[i][j]);
        }
        fprintf(stdout, "\n");
    }
    fprintf(stdout, "\n");
#endif

    // feature to col
    if (cnnLayer->padding == SAME) {
        // malloc columned feature
        featureCol = cnnLayer->featureCol;
        // perform feature to col
        FeatureToCol1D(featurePadded, featureCol, cnnLayer->featDimIn, cnnLayer->kernelSize, cnnLayer->numChannelsIn, cnnLayer->stride);
    }

#ifdef DEBUG_PRINT
    fprintf(stdout, "feat2col:\n");
    for (int16_t i = 0; i < cnnLayer->featDimIn / cnnLayer->stride; i++) {
        for (int16_t j = 0; j < cnnLayer->kernelSize * cnnLayer->numChannelsIn; j++) {
            fprintf(stdout, "%.6f, ", featureCol[i][j]);
        }
        fprintf(stdout, "\n");
    }
    fprintf(stdout, "\n");
#endif

    // kernel to col
    // malloc columned kernel
    kernelCol = cnnLayer->kernelCol;
    // perform kernel to col
    KernelToCol1D(cnnLayer->kernel, kernelCol, cnnLayer->kernelSize, cnnLayer->numChannelsIn, cnnLayer->numChannelsOut);

#ifdef DEBUG_PRINT
    fprintf(stdout, "kernel2col:\n");
    for (int16_t i = 0; i < cnnLayer->kernelSize * cnnLayer->numChannelsIn; i++) {
        for (int16_t j = 0; j < cnnLayer->numChannelsOut; j++) {
            fprintf(stdout, "%.6f, ", kernelCol[i][j]);
        }
        fprintf(stdout, "\n");
    }
    fprintf(stdout, "\n");
#endif

    // perform convolution by matrix mult
    MatrixMult(featureCol, kernelCol, cnnLayer->featOut,
        cnnLayer->featDimIn / cnnLayer->stride, cnnLayer->kernelSize * cnnLayer->numChannelsIn, cnnLayer->numChannelsOut);

#ifdef DEBUG_PRINT
    fprintf(stdout, "featureOut:\n");
    for (int16_t i = 0; i < cnnLayer->featDimOut; i++) {
        for (int16_t j = 0; j < cnnLayer->numChannelsOut; j++) {
            fprintf(stdout, "%.6f, ", cnnLayer->featOut[i][j]);
        }
        fprintf(stdout, "\n");
    }
    fprintf(stdout, "\n");
#endif

    // add bias to feature
    if (cnnLayer->useBias == 1) {
        AddBias(cnnLayer->featOut, cnnLayer->bias, cnnLayer->featDimOut, cnnLayer->numChannelsOut);
    }

#ifdef DEBUG_PRINT
    fprintf(stdout, "featureOut with bias:\n");
    for (int16_t i = 0; i < cnnLayer->featDimOut; i++) {
        for (int16_t j = 0; j < cnnLayer->numChannelsOut; j++) {
            fprintf(stdout, "%.6f, ", cnnLayer->featOut[i][j]);
        }
        fprintf(stdout, "\n");
    }
    fprintf(stdout, "\n");
#endif

    // apply activation function
    if (cnnLayer->activationFunc == LINEAR) {
        ApplyLinearActFunc2D(cnnLayer->featOut, cnnLayer->featDimOut, cnnLayer->numChannelsOut, cnnLayer->featOut);
    }
    else if (cnnLayer->activationFunc == RELU) {
        ApplyReluActFunc2D(cnnLayer->featOut, cnnLayer->featDimOut, cnnLayer->numChannelsOut, cnnLayer->featOut);
    }
    else if (cnnLayer->activationFunc == GDN) {
        ApplyGdnActFunc(cnnLayer->gdnActFuncParam, cnnLayer->featOut, cnnLayer->featDimOut, cnnLayer->numChannelsOut, cnnLayer->featOut);
    }
    else if (cnnLayer->activationFunc == IGDN) {
        ApplyIgdnActFunc(cnnLayer->gdnActFuncParam, cnnLayer->featOut, cnnLayer->featDimOut, cnnLayer->numChannelsOut, cnnLayer->featOut);
    }

#ifdef DEBUG_PRINT
    fprintf(stdout, "featureOut after activation:\n");
    for (int16_t i = 0; i < cnnLayer->featDimOut; i++) {
        for (int16_t j = 0; j < cnnLayer->numChannelsOut; j++) {
            fprintf(stdout, "%.6f, ", cnnLayer->featOut[i][j]);
        }
        fprintf(stdout, "\n");
    }
    fprintf(stdout, "\n");
#endif

    return 0;
}


/*
1D transpose convolution with stride and activation function
I/O params:
    CnnStructHandle cnnLayer        (i/o) cnn layer structure, include output feature buffer
    float **featureIn               (i)   input feature map, size: cnnLayer->featDimIn * cnnLayer->numChannelsIn
*/
int16_t Conv1DTranspose(
    CnnStructHandle cnnLayer,
    float **featureIn
)
{
    int16_t paddingSize;
    int16_t paddingSizeBegin, paddingSizeEnd;
    int16_t featDimPadded;
    int16_t featDimInterPolated;
    float **featureInterPolated = NULL;
    float **featurePadded = NULL;
    float **featureCol = NULL;
    float **kernelCol = NULL;

    // padding
    if (cnnLayer->padding == VALID) {
        featurePadded = featureIn;
    }
    else if (cnnLayer->padding == SAME) {

        // get InterPolated feat dim
        featDimInterPolated = cnnLayer->featDimIn * cnnLayer->stride;
        // get total padding size
        paddingSize = (cnnLayer->featDimOut - 1) + cnnLayer->kernelSize - featDimInterPolated;
        if (paddingSize < 0) {
            fprintf(stderr, "Error configuration in Conv1DTranspose, paddingSize < 0!!\n");
            exit(-1);
        }
        // get padded feat dim
        featDimPadded = featDimInterPolated + paddingSize;

        // get padding size at begin and end of feature
        if (paddingSize % 2 != 0) {
            // odd padding size, more padding at the end
            paddingSizeBegin = max((paddingSize - 1) / 2, 0);
            paddingSizeEnd = (paddingSize + 1) / 2;
        }
        else {
            // even padding size, same padding size at both side
            paddingSizeBegin = paddingSize / 2;
            paddingSizeEnd = paddingSize / 2;
        }

        // malloc InterPolated feature
        featureInterPolated = cnnLayer->featureInterPolated;
        // perform InterPolating
        InterPolating1D(featureIn, featureInterPolated, featDimInterPolated, cnnLayer->stride, cnnLayer->numChannelsIn);

        // malloc padded feature
        featurePadded = cnnLayer->featurePadded;
        // perform padding
        PaddingSame1D(featureInterPolated, featurePadded, featDimInterPolated, paddingSizeBegin, paddingSizeEnd, cnnLayer->numChannelsIn);
    }

#ifdef DEBUG_PRINT
    fprintf(stdout, "featurePadded:\n");
    for (int16_t i = 0; i < featDimPadded; i++) {
        for (int16_t j = 0; j < cnnLayer->numChannelsIn; j++) {
            fprintf(stdout, "%.6f, ", featurePadded[i][j]);
        }
        fprintf(stdout, "\n");
    }
    fprintf(stdout, "\n");
#endif

    // feature to col
    if (cnnLayer->padding == SAME) {
        // malloc columned feature
        featureCol = cnnLayer->featureCol;
        // perform feature to col
        FeatureToCol1D(featurePadded, featureCol, cnnLayer->featDimIn * cnnLayer->stride, cnnLayer->kernelSize, cnnLayer->numChannelsIn, 1);
    }

#ifdef DEBUG_PRINT
    fprintf(stdout, "feat2col:\n");
    for (int16_t i = 0; i < cnnLayer->featDimIn * cnnLayer->stride; i++) {
        for (int16_t j = 0; j < cnnLayer->kernelSize * cnnLayer->numChannelsIn; j++) {
            fprintf(stdout, "%.6f, ", featureCol[i][j]);
        }
        fprintf(stdout, "\n");
    }
    fprintf(stdout, "\n");
#endif

    // kernel to col
    // malloc columned kernel
    kernelCol = cnnLayer->kernelCol;
    // perform kernel to col
    KernelToCol1DTranspose(cnnLayer->kernel, kernelCol, cnnLayer->kernelSize, cnnLayer->numChannelsIn, cnnLayer->numChannelsOut);

#ifdef DEBUG_PRINT
    fprintf(stdout, "kernel2col:\n");
    for (int16_t i = 0; i < cnnLayer->kernelSize * cnnLayer->numChannelsIn; i++) {
        for (int16_t j = 0; j < cnnLayer->numChannelsOut; j++) {
            fprintf(stdout, "%.6f, ", kernelCol[i][j]);
        }
        fprintf(stdout, "\n");
    }
    fprintf(stdout, "\n");
#endif

    // perform convolution by matrix mult
    MatrixMult(featureCol, kernelCol, cnnLayer->featOut,
        cnnLayer->featDimIn * cnnLayer->stride, cnnLayer->kernelSize * cnnLayer->numChannelsIn, cnnLayer->numChannelsOut);

#ifdef DEBUG_PRINT
    fprintf(stdout, "featureOut:\n");
    for (int16_t i = 0; i < cnnLayer->featDimOut; i++) {
        for (int16_t j = 0; j < cnnLayer->numChannelsOut; j++) {
            fprintf(stdout, "%.6f, ", cnnLayer->featOut[i][j]);
        }
        fprintf(stdout, "\n");
    }
    fprintf(stdout, "\n");
#endif

    // add bias to feature
    if (cnnLayer->useBias == 1) {
        AddBias(cnnLayer->featOut, cnnLayer->bias, cnnLayer->featDimOut, cnnLayer->numChannelsOut);
    }

#ifdef DEBUG_PRINT
    fprintf(stdout, "featureOut with bias:\n");
    for (int16_t i = 0; i < cnnLayer->featDimOut; i++) {
        for (int16_t j = 0; j < cnnLayer->numChannelsOut; j++) {
            fprintf(stdout, "%.6f, ", cnnLayer->featOut[i][j]);
        }
        fprintf(stdout, "\n");
    }
    fprintf(stdout, "\n");
#endif

    // apply activation function
    if (cnnLayer->activationFunc == LINEAR) {
        ApplyLinearActFunc2D(cnnLayer->featOut, cnnLayer->featDimOut, cnnLayer->numChannelsOut, cnnLayer->featOut);
    }
    else if (cnnLayer->activationFunc == RELU) {
        ApplyReluActFunc2D(cnnLayer->featOut, cnnLayer->featDimOut, cnnLayer->numChannelsOut, cnnLayer->featOut);
    }
    else if (cnnLayer->activationFunc == GDN) {
        ApplyGdnActFunc(cnnLayer->gdnActFuncParam, cnnLayer->featOut, cnnLayer->featDimOut, cnnLayer->numChannelsOut, cnnLayer->featOut);
    }
    else if (cnnLayer->activationFunc == IGDN) {
        ApplyIgdnActFunc(cnnLayer->gdnActFuncParam, cnnLayer->featOut, cnnLayer->featDimOut, cnnLayer->numChannelsOut, cnnLayer->featOut);
    }

#ifdef DEBUG_PRINT
    fprintf(stdout, "featureOut after activation:\n");
    for (int16_t i = 0; i < cnnLayer->featDimOut; i++) {
        for (int16_t j = 0; j < cnnLayer->numChannelsOut; j++) {
            fprintf(stdout, "%.6f, ", cnnLayer->featOut[i][j]);
        }
        fprintf(stdout, "\n");
    }
    fprintf(stdout, "\n");
#endif

    return 0;
}


#ifdef CONV_TRANS_2PART
/*
1D transpose convolution with stride and activation function
2 parts version for stride is 2
remove useless mult for interpolated zeros, by two time conv op
I/O params:
    CnnStructHandle cnnLayer        (i/o) cnn layer structure, include output feature buffer
    float **featureIn               (i)   input feature map, size: cnnLayer->featDimIn * cnnLayer->numChannelsIn
*/
int16_t Conv1DTranspose2Part(
    CnnStructHandle cnnLayer,
    float **featureIn
)
{
    int16_t paddingSize;
    int16_t paddingSizeBegin, paddingSizeEnd;
    int16_t featDimPadded;
    float **featurePadded = NULL;

    // padding
    if (cnnLayer->padding == VALID) {
        featurePadded = featureIn;
    }
    else if (cnnLayer->padding == SAME) {
        // get total padding size
        // for two part version, padding size fixed to 2
        paddingSize = 2;
        // get padded feat dim
        featDimPadded = cnnLayer->featDimIn + paddingSize;

        // get padding size at begin and end of feature
        if (paddingSize % 2 != 0) {
            // odd padding size, more padding at the end
            paddingSizeBegin = max((paddingSize - 1) / 2, 0);
            paddingSizeEnd = (paddingSize + 1) / 2;
        }
        else {
            // even padding size, same padding size at both side
            paddingSizeBegin = paddingSize / 2;
            paddingSizeEnd = paddingSize / 2;
        }

        // malloc padded feature
        featurePadded = cnnLayer->featurePadded;
        // perform padding
        PaddingSame1D(featureIn, featurePadded, cnnLayer->featDimIn, paddingSizeBegin,
            paddingSizeEnd, cnnLayer->numChannelsIn);
    }

    if (cnnLayer->padding == SAME) {
        // perform feature to col
        // Odd part, kernel size is (kernelSize+1)/2
        FeatureToCol1D(featurePadded, cnnLayer->featureColOdd, cnnLayer->featDimIn,
            (cnnLayer->kernelSize + 1) / 2, cnnLayer->numChannelsIn, 1);

        // Even part, kernel size is (kernelSize-1)/2
        if (cnnLayer->kernelSize == 3) {
            // for kernel size 3
            // kernel size for even part is 1, use featureIn instead of featurePadded
            FeatureToCol1D(featureIn, cnnLayer->featureColEven, cnnLayer->featDimIn,
                (cnnLayer->kernelSize - 1) / 2, cnnLayer->numChannelsIn, 1);
        }
        else if (cnnLayer->kernelSize == 5) {
            // for kernel size 5
            // use featurePadded
            FeatureToCol1D(featurePadded, cnnLayer->featureColEven, cnnLayer->featDimIn,
                (cnnLayer->kernelSize - 1) / 2, cnnLayer->numChannelsIn, 1);
        }
    }

    // perform kernel to col
    // use two part kernel to col transform
    KernelToCol1DTransposeOdd(cnnLayer->kernel, cnnLayer->kernelColOdd, cnnLayer->kernelSize,
        cnnLayer->numChannelsIn, cnnLayer->numChannelsOut);

    KernelToCol1DTransposeEven(cnnLayer->kernel, cnnLayer->kernelColEven, cnnLayer->kernelSize,
        cnnLayer->numChannelsIn, cnnLayer->numChannelsOut);

    // perform convolution by matrix mult
    MatrixMult(cnnLayer->featureColOdd, cnnLayer->kernelColOdd, cnnLayer->featOutOdd,
        cnnLayer->featDimIn, (cnnLayer->kernelSize + 1) / 2 * cnnLayer->numChannelsIn, cnnLayer->numChannelsOut);

    MatrixMult(cnnLayer->featureColEven, cnnLayer->kernelColEven, cnnLayer->featOutEven,
        cnnLayer->featDimIn, (cnnLayer->kernelSize - 1) / 2 * cnnLayer->numChannelsIn, cnnLayer->numChannelsOut);

    // Interleave to get output feature
    if (cnnLayer->kernelSize == 5) {
        // for kernelSize 5, even part first
        for (int16_t i = 0; i < cnnLayer->featDimIn; i++) {
            for (int16_t j = 0; j < cnnLayer->numChannelsOut; j++) {
                cnnLayer->featOut[2 * i][j] = cnnLayer->featOutEven[i][j];
                cnnLayer->featOut[2 * i + 1][j] = cnnLayer->featOutOdd[i][j];
            }
        }
    }
    else if (cnnLayer->kernelSize == 3) {
        // for kernelSize 3, odd part first
        for (int16_t i = 0; i < cnnLayer->featDimIn; i++) {
            for (int16_t j = 0; j < cnnLayer->numChannelsOut; j++) {
                cnnLayer->featOut[2 * i][j] = cnnLayer->featOutOdd[i][j];
                cnnLayer->featOut[2 * i + 1][j] = cnnLayer->featOutEven[i][j];
            }
        }
    }

    // add bias to feature
    if (cnnLayer->useBias == 1) {
        AddBias(cnnLayer->featOut, cnnLayer->bias, cnnLayer->featDimOut, cnnLayer->numChannelsOut);
    }

    // apply activation function
    if (cnnLayer->activationFunc == LINEAR) {
        ApplyLinearActFunc2D(cnnLayer->featOut, cnnLayer->featDimOut, cnnLayer->numChannelsOut, cnnLayer->featOut);
    }
    else if (cnnLayer->activationFunc == RELU) {
        ApplyReluActFunc2D(cnnLayer->featOut, cnnLayer->featDimOut, cnnLayer->numChannelsOut, cnnLayer->featOut);
    }
    else if (cnnLayer->activationFunc == GDN) {
        ApplyGdnActFunc(cnnLayer->gdnActFuncParam, cnnLayer->featOut, cnnLayer->featDimOut, cnnLayer->numChannelsOut, cnnLayer->featOut);
    }
    else if (cnnLayer->activationFunc == IGDN) {
        ApplyIgdnActFunc(cnnLayer->gdnActFuncParam, cnnLayer->featOut, cnnLayer->featDimOut, cnnLayer->numChannelsOut, cnnLayer->featOut);
    }

    return 0;
}
#endif


/*
Destroy cnn layer structure
I/O params:
    CnnStructHandle cnnLayer        (i/o) cnn layer structure handle
    int16_t isTranspos              (i) flag for conv transpose
*/
int16_t DestroyCnnLayer(
    CnnStructHandle cnnLayer
)
{
    // free kernel
    int16_t chNum;
    if (cnnLayer->isTranspose == 0) {
        // conv, kernelSize * numChannelsIn * numChannelsOut
        chNum = cnnLayer->numChannelsIn;
    }
    else {
        // conv, kernelSize * numChannelsOut * numChannelsIn
        chNum = cnnLayer->numChannelsOut;
    }
    for (int16_t i = 0; i < cnnLayer->kernelSize; i++)
    {
        for (int16_t j = 0; j < chNum; j++) {
            free(cnnLayer->kernel[i][j]);
            cnnLayer->kernel[i][j] = NULL;
        }
        free(cnnLayer->kernel[i]);
        cnnLayer->kernel[i] = NULL;
    }
    free(cnnLayer->kernel);
    cnnLayer->kernel = NULL;

    // free bias
    if (cnnLayer->bias != NULL) {
        free(cnnLayer->bias);
        cnnLayer->bias = NULL;
    }

    // free gdn param
    if (cnnLayer->gdnActFuncParam != NULL) {
        DestroyGdnParam(cnnLayer->gdnActFuncParam, cnnLayer->numChannelsOut);
        free(cnnLayer->gdnActFuncParam);
        cnnLayer->gdnActFuncParam = NULL;
    }

    // free output feature buffer
    for (int16_t i = 0; i < cnnLayer->featDimOut; i++) {
        free(cnnLayer->featOut[i]);
        cnnLayer->featOut[i] = NULL;
    }
    free(cnnLayer->featOut);
    cnnLayer->featOut = NULL;

    // free runtime buffer
    CnnFreeRuntimeBuffer(cnnLayer);

    return 0;
}

#endif