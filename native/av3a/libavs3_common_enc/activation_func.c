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
#include "avs3_prot_com.h"

#ifdef NEURAL_QC

/*
Apply RELU activation function for vector
I/O params:
    const float *srcVec:      (i) source vector, size: len
    int16_t len:              (i) length of vector
    float *destVec            (o) dest vector, size: len
*/

void ApplyReluActFuncVec(
    const float *srcVec,
    int16_t len,
    float *destVec
)
{
    for (int16_t i = 0; i < len; i++) {
        if (srcVec[i] > 0.0f) {
            destVec[i] = srcVec[i];
        }
        else {
            destVec[i] = 0.0f;
        }
    }
}


/*
Apply RELU activation function for 2D
I/O params:
    const float **srcMat            (i) source matrix, size: numRow * numCol
    int16_t numRow                  (i) number of rows
    int16_t numCol                  (i) number of cols
    float **destMat                 (o) dest matrix, size: numRow * numCol
*/
void ApplyReluActFunc2D(
    const float **srcMat,
    int16_t numRow,
    int16_t numCol,
    float **destMat
)
{
    for (int16_t i = 0; i < numRow; i++) {
        ApplyReluActFuncVec(srcMat[i], numCol, destMat[i]);
    }
}


/*
Apply linear activation function for vector
I/O params:
    const float *srcVec:        (i) source vector, size: len
    int16_t len:                (i) length of vector
    float *destVec:             (o) dest vector, size: len
*/

void ApplyLinearActFuncVec(
    const float *srcVec,
    int16_t len,
    float *destVec
)
{
    for (int16_t i = 0; i < len; i++) {
        destVec[i] = srcVec[i];
    }
}


/*
Apply linear activation function for 2D
I/O params:
    const float **srcMat            (i) source matrix, size: numRow * numCol
    int16_t numRow                  (i) number of rows
    int16_t numCol                  (i) number of cols
    float **destMat                 (o) dest matrix, size: numRow * numCol
*/
void ApplyLinearActFunc2D(
    const float **srcMat,
    int16_t numRow,
    int16_t numCol,
    float **destMat
)
{
    for (int16_t i = 0; i < numRow; i++) {
        ApplyLinearActFuncVec(srcMat[i], numCol, destMat[i]);
    }
}


/*
Apply sigmoid activation function for vector
I/O params:
    const float *srcVec:        (i) source vector, size: len
    int16_t len:                (i) length of vector
    float *destVec              (o) dest vector, size: len
*/

void ApplySigmoidActFuncVec(
    const float *srcVec,
    int16_t len,
    float *destVec
)
{
    for (int16_t i = 0; i < len; i++) {
        destVec[i] = 1.0f / (1.0f + (float)exp(-srcVec[i]));
    }
}


/*
Apply sigmoid activation function for 2D
I/O params:
    const float **srcMat            (i) source matrix, size: numRow * numCol
    int16_t numRow                  (i) number of rows
    int16_t numCol                  (i) number of cols
    float **destMat                 (o) dest matrix, size: numRow * numCol
*/
void ApplySigmoidActFunc2D(
    const float **srcMat,
    int16_t numRow,
    int16_t numCol,
    float **destMat
)
{
    for (int16_t i = 0; i < numRow; i++) {
        ApplySigmoidActFuncVec(srcMat[i], numCol, destMat[i]);
    }
}


/*
Apply TANH activation function for vector
I/O params:
    const float *srcVec:        (i) source vector, size: len
    int16_t len:                (i) length of vector
    float *destVec              (o) dest vector, size: len
*/

void ApplyTanhActFuncVec(
    const float *srcVec,
    int16_t len,
    float *destVec
)
{
    for (int16_t i = 0; i < len; i++) {
        destVec[i] = 2.0f / (1.0f + (float)exp(-2.0f * srcVec[i])) - 1.0f;
    }
}


/*
Apply TANH activation function for 2D
I/O params: 
    const float **srcMat            (i) source matrix, size: numRow * numCol
    int16_t numRow                  (i) number of rows
    int16_t numCol                  (i) number of cols
    float **destMat                 (o) dest matrix, size: numRow * numCol
*/

void ApplyTanhActFunc2D(
    const float **srcMat,
    int16_t numRow,
    int16_t numCol,
    float **destMat
)
{
    for (int16_t i = 0; i < numRow; i++) {
        ApplyTanhActFuncVec(srcMat[i], numCol, destMat[i]);
    }
}


/*
Init parameter structure of GDN/IGDN activation function
I/O params:
    FILE *fModel                                (i) model file
    GdnActFuncHandle gdnActFuncParam            (o) GDN/IGDN param handle
    int16_t numChannelsOut                      (i) number of output channels
*/
int16_t InitGdnParam(
    FILE *fModel,
    GdnActFuncHandle gdnActFuncParam,
    int16_t numChannelsOut
)
{
    float beta;
    float gamma;

    // beta param
    gdnActFuncParam->beta = (float *)malloc(sizeof(float) * numChannelsOut);
    for (int16_t i = 0; i < numChannelsOut; i++) {
        fread(&beta, sizeof(float), 1, fModel);
        gdnActFuncParam->beta[i] = beta;
    }

    // gamma param
    gdnActFuncParam->gamma = (float **)malloc(sizeof(float *) * numChannelsOut);
    for (int16_t i = 0; i < numChannelsOut; i++) {
        gdnActFuncParam->gamma[i] = (float *)malloc(sizeof(float) * numChannelsOut);
    }
    for (int16_t i = 0; i < numChannelsOut; i++) {
        for (int16_t j = 0; j < numChannelsOut; j++) {
            fread(&gamma, sizeof(float), 1, fModel);
            gdnActFuncParam->gamma[i][j] = gamma;
        }
    }

    return 0;
}


/*
Destroy parameter structure of GDN/IGDN activation function
I/O params:
    GdnActFuncHandle gdnActFuncParam                (i/o) GDN/IGDN param handle
    int16_t numChannelsOut                          (i)   number of output channels
*/
int16_t DestroyGdnParam(
    GdnActFuncHandle gdnActFuncParam,
    int16_t numChannelsOut
)
{
    // beta param
    free(gdnActFuncParam->beta);
    gdnActFuncParam->beta = NULL;

    // gamma param
    for (int16_t i = 0; i < numChannelsOut; i++) {
        free(gdnActFuncParam->gamma[i]);
        gdnActFuncParam->gamma[i] = NULL;
    }
    free(gdnActFuncParam->gamma);
    gdnActFuncParam->gamma = NULL;

    return 0;
}


/*
Apply GDN activation function for input feature map
Current version is for 2D feature map, i.e. dimFeat * numChannel
I/O params:
    GdnActFuncHandle gdnActFuncParam:           (i) parameter st for GDN activation func
    const float **featureIn                     (i) input feature map, 2D, dimFeat * numChannel
    int16_t dimFeat                             (i) feature dim, 1st dim of feature map
    int16_t numChannel                          (i) channel number, 2nd dim of feature map
    float **featureOut                          (o) output feature map, 2D
*/

void ApplyGdnActFunc(
    GdnActFuncHandle gdnActFuncParam,
    const float **featureIn,
    int16_t dimFeat,
    int16_t numChannel,
    float **featureOut
)
{
    float **squaredFeature;
    float **tmpFeature;

    // calculate x[j]^2
    squaredFeature = (float **)malloc(sizeof(float *) * dimFeat);
    for (int16_t i = 0; i < dimFeat; i++) {
        squaredFeature[i] = (float *)malloc(sizeof(float) * numChannel);
    }
    for (int16_t i = 0; i < dimFeat; i++) {
        for (int16_t j = 0; j < numChannel; j++) {
            squaredFeature[i][j] = featureIn[i][j] * featureIn[i][j];
        }
    }

    // tmp feature buffer
    tmpFeature = (float **)malloc(sizeof(float *) * dimFeat);
    for (int16_t i = 0; i < dimFeat; i++) {
        tmpFeature[i] = (float *)malloc(sizeof(float) * numChannel);
    }

    // apply GDN
    MatrixMult(squaredFeature, gdnActFuncParam->gamma, tmpFeature, dimFeat, numChannel, numChannel);
    // division for GDN
    for (int16_t i = 0; i < dimFeat; i++) {
        for (int16_t j = 0; j < numChannel; j++) {
            featureOut[i][j] = featureIn[i][j] / (float)(sqrt(tmpFeature[i][j] + gdnActFuncParam->beta[j]));
        }
    }

    for (int16_t i = 0; i < dimFeat; i++) {
        free(squaredFeature[i]);
        squaredFeature[i] = NULL;
    }
    free(squaredFeature);
    squaredFeature = NULL;

    for (int16_t i = 0; i < dimFeat; i++) {
        free(tmpFeature[i]);
        tmpFeature[i] = NULL;
    }
    free(tmpFeature);
    tmpFeature = NULL;
}


/*
Apply IGDN activation function for input feature map
Current version is for 2D feature map, i.e. dimFeat * numChannel
I/O params:
    GdnActFuncHandle gdnActFuncParam:           (i) parameter st for GDN activation func
    const float **featureIn                     (i) input feature map, 2D, dimFeat * numChannel
    int16_t dimFeat                             (i) feature dim, 1st dim of feature map
    int16_t numChannel                          (i) channel number, 2nd dim of feature map
    float **featureOut                          (o) output feature map, 2D
*/

void ApplyIgdnActFunc(
    GdnActFuncHandle gdnActFuncParam,
    const float **featureIn,
    int16_t dimFeat,
    int16_t numChannel,
    float **featureOut
)
{
    float **squaredFeature;
    float **tmpFeature;

    // calculate x[j]^2
    squaredFeature = (float **)malloc(sizeof(float *) * dimFeat);
    for (int16_t i = 0; i < dimFeat; i++) {
        squaredFeature[i] = (float *)malloc(sizeof(float) * numChannel);
    }
    for (int16_t i = 0; i < dimFeat; i++) {
        for (int16_t j = 0; j < numChannel; j++) {
            squaredFeature[i][j] = featureIn[i][j] * featureIn[i][j];
        }
    }

    // tmp feature buffer
    tmpFeature = (float **)malloc(sizeof(float *) * dimFeat);
    for (int16_t i = 0; i < dimFeat; i++) {
        tmpFeature[i] = (float *)malloc(sizeof(float) * numChannel);
    }

    // apply IGDN
    MatrixMult(squaredFeature, gdnActFuncParam->gamma, tmpFeature, dimFeat, numChannel, numChannel);
    // multiply for IGDN
    for (int16_t i = 0; i < dimFeat; i++) {
        for (int16_t j = 0; j < numChannel; j++) {
            featureOut[i][j] = featureIn[i][j] * (float)(sqrt(tmpFeature[i][j] + gdnActFuncParam->beta[j]));
        }
    }

    for (int16_t i = 0; i < dimFeat; i++) {
        free(squaredFeature[i]);
        squaredFeature[i] = NULL;
    }
    free(squaredFeature);
    squaredFeature = NULL;

    for (int16_t i = 0; i < dimFeat; i++) {
        free(tmpFeature[i]);
        tmpFeature[i] = NULL;
    }
    free(tmpFeature);
    tmpFeature = NULL;
}
#endif