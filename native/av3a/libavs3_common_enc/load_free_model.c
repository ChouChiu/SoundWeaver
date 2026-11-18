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
#include "avs3_cnst_com.h"
#include "avs3_stat_com.h"
#include "avs3_prot_com.h"


#ifdef NEURAL_QC

static int16_t InitEncoder(
    FILE *fModel,
    ModelStructHandle encoderHandle,
    int16_t featDimIn
)
{
    int16_t numLayers;
    int16_t featDimInLayer;     // intput feature dim for each layer

    // Number of layers
    fread(&numLayers, sizeof(int16_t), 1, fModel);
    encoderHandle->numLayers = numLayers;
    //(stdout, "encoder num layers: %d\n", encoderHandle->numLayers);

    // init cnn layers
    for (int16_t i = 0; i < MAX_LAYERS; i++) {
        encoderHandle->cnnLayers[i] = NULL;
    }
    for (int16_t i = 0; i < encoderHandle->numLayers; i++) {

        // malloc each layer
        encoderHandle->cnnLayers[i] = (CnnStructHandle)malloc(sizeof(CnnStruct));
        if (encoderHandle->cnnLayers[i] == NULL) {
            fprintf(stderr, "Malloc encoder cnn layer error!!\n");
            exit(-1);
        }

        // get input feature dim for each layer
        if (i == 0) {
            featDimInLayer = featDimIn;
        }
        else {
            featDimInLayer = encoderHandle->cnnLayers[i - 1]->featDimOut;
        }

        // init cnn layer
        InitCnnLayer(fModel, encoderHandle->cnnLayers[i], 0, featDimInLayer);
    }

    return 0;
}


static int16_t DestroyEncoder(
    ModelStructHandle encoderHandle
)
{
    for (int16_t i = 0; i < encoderHandle->numLayers; i++) {

        DestroyCnnLayer(encoderHandle->cnnLayers[i]);

        free(encoderHandle->cnnLayers[i]);
        encoderHandle->cnnLayers[i] = NULL;
    }

    return 0;
}


static int16_t InitDecoder(
    FILE *fModel,
    ModelStructHandle decoderHandle,
    int16_t featDimIn
)
{
    int16_t numLayers;
    int16_t featDimInLayer;     // intput feature dim for each layer

    // Number of layers
    fread(&numLayers, sizeof(int16_t), 1, fModel);
    decoderHandle->numLayers = numLayers;
    //fprintf(stdout, "decoder num layers: %d\n", decoderHandle->numLayers);

    // init cnn layers
    for (int16_t i = 0; i < MAX_LAYERS; i++) {
        decoderHandle->cnnLayers[i] = NULL;
    }
    for (int16_t i = 0; i < decoderHandle->numLayers; i++) {

        // malloc each layer
        decoderHandle->cnnLayers[i] = (CnnStructHandle)malloc(sizeof(CnnStruct));
        if (decoderHandle->cnnLayers[i] == NULL) {
            fprintf(stderr, "Malloc decoder cnn layer error!!\n");
            exit(-1);
        }

        // get input feature dim for each layer
        if (i == 0) {
            featDimInLayer = featDimIn;
        }
        else {
            featDimInLayer = decoderHandle->cnnLayers[i - 1]->featDimOut;
        }

        // init cnn layer
        InitCnnLayer(fModel, decoderHandle->cnnLayers[i], 1, featDimInLayer);
    }

    return 0;
}


static int16_t DestroyDecoder(
    ModelStructHandle decoderHandle
)
{
    for (int16_t i = 0; i < decoderHandle->numLayers; i++) {

        DestroyCnnLayer(decoderHandle->cnnLayers[i]);

        free(decoderHandle->cnnLayers[i]);
        decoderHandle->cnnLayers[i] = NULL;
    }

    return 0;
}


static int16_t InitContextScale(
    FILE *fModel,
    int16_t *numContextScale,
    float **contextScale
)
{
    // get number of context scale
    fread(numContextScale, sizeof(int16_t), 1, fModel);

    // get context scales
    *contextScale = (float *)malloc(sizeof(float) * (*numContextScale));
    fread(*contextScale, sizeof(float), *numContextScale, fModel);

    return 0;
}


static int16_t LoadModel(
    FILE *fModel,
    NeuralCodecHandle *codecSt,
    int16_t hasContext, 
    int16_t numFeatEncode
)
{
    // Create codec structure
    (*codecSt) = (NeuralCodecHandle)malloc(sizeof(NeuralCodecStruct));
    if ((*codecSt) == NULL) {
        fprintf(stderr, "Malloc codec structure error!\n");
        exit(-1);
    }

    // Init input feature dims
    (*codecSt)->numLinesEncode = numFeatEncode;

    // Create encoder structure
    (*codecSt)->encoderHandle = (ModelStructHandle)malloc(sizeof(ModelStruct));
    if ((*codecSt)->encoderHandle == NULL) {
        fprintf(stderr, "Malloc encoder structure error!\n");
        exit(-1);
    }
    // Init encoder
    InitEncoder(fModel, (*codecSt)->encoderHandle, (*codecSt)->numLinesEncode);

    // Init output latent dims
    ModelStructHandle encoderHandle = (*codecSt)->encoderHandle;
    (*codecSt)->numLatentEncode = encoderHandle->cnnLayers[encoderHandle->numLayers - 1]->featDimOut;
    // Init output latent channels
    (*codecSt)->numLatentChannels = encoderHandle->cnnLayers[encoderHandle->numLayers - 1]->numChannelsOut;

    // Create decoder structure
    (*codecSt)->decoderHandle = (ModelStructHandle)malloc(sizeof(ModelStruct));
    if ((*codecSt)->decoderHandle == NULL) {
        fprintf(stderr, "Malloc decoder structure error!\n");
        exit(-1);
    }
    // Init decoder
    InitDecoder(fModel, (*codecSt)->decoderHandle, (*codecSt)->numLatentEncode);

    // Create quantizer structure
    (*codecSt)->quantizerHandle = (QuantizerHandle)malloc(sizeof(QuantizerStruct));
    if ((*codecSt)->quantizerHandle == NULL) {
        fprintf(stderr, "Malloc quantizer handle error!\n");
        exit(-1);
    }
    // Init quantizer
    InitQuantizer(fModel, (*codecSt)->quantizerHandle, (*codecSt)->numLatentChannels);

    // Init context scale for hyper-prior base model
    (*codecSt)->numContextScale = 0;
    (*codecSt)->contextScale = NULL;
    if (hasContext == 1) {
        InitContextScale(fModel, &((*codecSt)->numContextScale), &(*codecSt)->contextScale);
    }

    // Create range coder structure
    (*codecSt)->rangeCoderConfig = (RangeCoderConfigHandle)malloc(sizeof(RangeCoderConfigStruct));
    if ((*codecSt)->rangeCoderConfig == NULL) {
        fprintf(stderr, "Malloc range coder handle error!\n");
        exit(-1);
    }
    // Init range coder config
    if (hasContext == 0) {
        // for vae, numCdf is numLatentChannels
        InitRangeCoderConfig(fModel, (*codecSt)->rangeCoderConfig, (*codecSt)->numLatentChannels);
    }
    else {
        // for hyper, numCdf is numContextScale
        InitRangeCoderConfig(fModel, (*codecSt)->rangeCoderConfig, (*codecSt)->numContextScale);
    }

    return 0;
}


static int16_t DestroyModel(
    NeuralCodecHandle *codecSt
)
{
    if (codecSt == NULL || (*codecSt) == NULL) {
        fprintf(stderr, "Null codec structure in DestroyModel func!!\n");
        exit(-1);
    }

    // destroy encoder
    if ((*codecSt)->encoderHandle == NULL) {
        fprintf(stderr, "Null encoder structure in DestroyModel func!!\n");
        exit(-1);
    }
    DestroyEncoder((*codecSt)->encoderHandle);
    free((*codecSt)->encoderHandle);
    (*codecSt)->encoderHandle = NULL;

    // destroy decoder
    if ((*codecSt)->decoderHandle == NULL) {
        fprintf(stderr, "Null decoder structure in DestroyModel func!!\n");
        exit(-1);
    }
    DestroyDecoder((*codecSt)->decoderHandle);
    free((*codecSt)->decoderHandle);
    (*codecSt)->decoderHandle = NULL;

    // destroy quantizer
    if ((*codecSt)->quantizerHandle == NULL) {
        fprintf(stderr, "Null quantizer handle in DestroyModel func!!\n");
        exit(-1);
    }
    DestroyQuantizer((*codecSt)->quantizerHandle);
    free((*codecSt)->quantizerHandle);
    (*codecSt)->quantizerHandle = NULL;

    // destroy context scale
    if ((*codecSt)->contextScale != NULL) {
        free((*codecSt)->contextScale);
        (*codecSt)->contextScale = NULL;
    }

    // destroy range coder
    if ((*codecSt)->rangeCoderConfig == NULL) {
        fprintf(stderr, "Null range coder handle in DestroyModel func!!\n");
        exit(-1);
    }
    DestroyRangeCoderConfig((*codecSt)->rangeCoderConfig);
    free((*codecSt)->rangeCoderConfig);
    (*codecSt)->rangeCoderConfig = NULL;

    // destroy codec st
    free(*codecSt);
    *codecSt = NULL;

    return 0;
}


int16_t InitNeuralCodec(
    FILE *fModel,
    NeuralCodecHandle *baseCodecSt,
    NeuralCodecHandle *contextCodecSt,
    TypeModel modelType
)
{
    // Init codec according to model type
    if (modelType == VAE) {
        // init base model
        LoadModel(fModel, baseCodecSt, 0, BLOCK_LEN_LONG);
        // no context model
        contextCodecSt = NULL;
    }
    else {
        // init base model
        LoadModel(fModel, baseCodecSt, 1, BLOCK_LEN_LONG);
        // init context model
        int16_t numFeatEncode = (*baseCodecSt)->numLatentEncode;
        LoadModel(fModel, contextCodecSt, 0, numFeatEncode);
    }

    return 0;
}


int16_t DestroyNeuralCodec(
    NeuralCodecHandle *baseCodecSt,
    NeuralCodecHandle *contextCodecSt
)
{
    // deinit base codec
    DestroyModel(baseCodecSt);

    // deinit context codec
    if ((*contextCodecSt) != NULL) {
        DestroyModel(contextCodecSt);
    }

    return 0;
}


int16_t InitNeuralQcData(
    NeuralQcData *neuralQcData
)
{
    SetUC(neuralQcData->baseBitstream, 0, MAX_QC_BS_LENGTH);
    neuralQcData->baseNumBytes = 0;

    SetUC(neuralQcData->contextBitstream, 0, MAX_QC_BS_LENGTH);
    neuralQcData->contextNumBytes = 0;

    neuralQcData->featureScale = 1.0f;
    neuralQcData->isFeatAmplified = 0;
    neuralQcData->scaleQIdx = -1;

    SetFloat(neuralQcData->nfParam, 0.0f, N_GROUP_SHORT_WIN);
    SetShort(neuralQcData->nfParamQIdx, -1, N_GROUP_SHORT_WIN);

    return 0;
}

#endif