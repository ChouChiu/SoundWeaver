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

#ifdef DEBUG_SAVE
extern FILE *fLogDec;
extern FILE *fQuant;
extern FILE *fNf;
#endif


/*
MDCT dequantization and decode
includes RC, dequantization and synthesis transform
I/O params:
    NeuralCodecHandle codecSt                       (i) top level struct handle for neural network codec
    NeuralQcData *neuralQcData                      (i) neural Q/C module data structure, including bs, feature scale and NF params
    float *featureDec                               (o) decoded features
*/
int16_t MdctDequantDecodeVae(
    NeuralCodecHandle codecSt,
    NeuralQcData *neuralQcData,
    float *featureDec
)
{
    int32_t *flattenLatent;                 // flattened latent
    int16_t *cdfIndex;                      // cdf index for each dim of flattened latent
    int32_t **quantizedLatent = NULL;       // quantized latent
    float **dequantizedLatent;              // dequantized latent

    int16_t featDimOut = 0;                 // output dim for each cnn layer
    float **layerInput = NULL;              // pointer to cnn input
    float **layerOutput = NULL;             // pointer to cnn output

    // get related handle
    ModelStructHandle decoderHandle = codecSt->decoderHandle;
    QuantizerHandle quantizerHandle = codecSt->quantizerHandle;
    RangeCoderConfigHandle rangeCoderConfig = codecSt->rangeCoderConfig;
    // get related params
    int16_t numLatentEncode = codecSt->numLatentEncode;
    int16_t numLatentChannels = codecSt->numLatentChannels;

    // Get related handle of qc data
    uint8_t *bitstream = neuralQcData->baseBitstream;
    int16_t numBytes = neuralQcData->baseNumBytes;

    // malloc flattened latent buffer
    flattenLatent = (int32_t *)malloc(sizeof(int32_t) * numLatentEncode * numLatentChannels);
    // set cdf index
    cdfIndex = (int16_t *)malloc(sizeof(int16_t) * numLatentEncode * numLatentChannels);
    for (int16_t i = 0; i < numLatentEncode; i++) {
        for (int16_t j = 0; j < numLatentChannels; j++) {
            cdfIndex[i * numLatentChannels + j] = j;
        }
    }

    // perform range decoding
    RangeDecodeProcess(rangeCoderConfig, flattenLatent, numLatentEncode * numLatentChannels, 
        cdfIndex, bitstream, numBytes);

    // malloc quantized latent buffer
    quantizedLatent = (int32_t **)malloc(sizeof(int32_t *) * numLatentEncode);
    for (int16_t i = 0; i < numLatentEncode; i++) {
        quantizedLatent[i] = (int32_t *)malloc(sizeof(int32_t) * numLatentChannels);
    }
    // put flattened latent into 2D buffers
    for (int16_t i = 0; i < numLatentEncode; i++) {
        for (int16_t j = 0; j < numLatentChannels; j++) {
            quantizedLatent[i][j] = flattenLatent[i*numLatentChannels + j];
        }
    }

    // malloc dequantized latent buffer
    dequantizedLatent = (float **)malloc(sizeof(float *) * numLatentEncode);
    for (int16_t i = 0; i < numLatentEncode; i++) {
        dequantizedLatent[i] = (float *)malloc(sizeof(float) * numLatentChannels);
    }

    // perform dequantization
    LatentDequantize(quantizerHandle, quantizedLatent, dequantizedLatent, numLatentEncode, numLatentChannels);

    // first time, malloc input latent buffer
    layerInput = (float **)malloc(sizeof(float *) * numLatentEncode);
    for (int16_t i = 0; i < numLatentEncode; i++) {
        layerInput[i] = (float *)malloc(sizeof(float) * numLatentChannels);
    }
    // copy input to buffer
    for (int16_t i = 0; i < numLatentEncode; i++) {
        for (int16_t j = 0; j < numLatentChannels; j++) {
            layerInput[i][j] = dequantizedLatent[i][j];
        }
    }

    // Loop for encoder cnn layers
    for (int16_t layerIdx = 0; layerIdx < decoderHandle->numLayers; layerIdx++) {

        // cnn layer handle
        CnnStructHandle cnnLayer;
        cnnLayer = decoderHandle->cnnLayers[layerIdx];

        // last cnn layer handle
        CnnStructHandle cnnLayerLast = NULL;
        if (layerIdx != 0) {
            cnnLayerLast = decoderHandle->cnnLayers[layerIdx - 1];
        }

        // perform transpose cnn convolution
        if (layerIdx == 0) {
#ifndef CONV_TRANS_2PART
            Conv1DTranspose(cnnLayer, layerInput);
#else
            if (cnnLayer->stride == 2) {
                Conv1DTranspose2Part(cnnLayer, layerInput);
            }
            else {
                Conv1DTranspose(cnnLayer, layerInput);
            }
#endif
        }
        else {
#ifndef CONV_TRANS_2PART
            Conv1DTranspose(cnnLayer, cnnLayerLast->featOut);
#else
            if (cnnLayer->stride == 2) {
                Conv1DTranspose2Part(cnnLayer, cnnLayerLast->featOut);
            }
            else {
                Conv1DTranspose(cnnLayer, cnnLayerLast->featOut);
            }
#endif
        }
    }

    // copy last layer output to featureDec
    layerOutput = decoderHandle->cnnLayers[decoderHandle->numLayers - 1]->featOut;
    featDimOut = decoderHandle->cnnLayers[decoderHandle->numLayers - 1]->featDimOut;
    for (int16_t i = 0; i < featDimOut; i++) {
        featureDec[i] = layerOutput[i][0];
    }

#ifdef DEBUG_SAVE
    for (int16_t i = 0; i < featDimOut; i++) {
        fprintf(fLogDec, "%.10f  ", featureDec[i]);
    }
    fprintf(fLogDec, "\n");
#endif

    // free flatenned latent
    free(flattenLatent);
    flattenLatent = NULL;

    // free cdf index
    free(cdfIndex);
    cdfIndex = NULL;

    // free quantized latent
    for (int16_t i = 0; i < numLatentEncode; i++) {
        free(quantizedLatent[i]);
        quantizedLatent[i] = NULL;
    }
    free(quantizedLatent);
    quantizedLatent = NULL;

    // free dequantized latent
    for (int16_t i = 0; i < numLatentEncode; i++) {
        free(dequantizedLatent[i]);
        dequantizedLatent[i] = NULL;
    }
    free(dequantizedLatent);
    dequantizedLatent = NULL;

    // free layerInput
    for (int16_t i = 0; i < numLatentEncode; i++) {
        free(layerInput[i]);
        layerInput[i] = NULL;
    }
    free(layerInput);
    layerInput = NULL;

    return 0;
}


/*
Context model decoding process
for hyper model, context part
I/O params:
    NeuralCodecHandle contextCodecSt                (i/o) context codec st handle
    uint8_t *contextBitstream                       (i)   bitstream for context model
    int16_t *contextNumBytes                        (i)   number of bytes for context bitstream
*/
static int16_t ContextDec(
    NeuralCodecHandle contextCodecSt,
    uint8_t *contextBitstream,
    int16_t contextNumBytes
)
{
    int16_t ctxNumLatentEncode;                     // latent dim for context model
    int16_t ctxNumLatentChannels;                   // latent num channels for context model
    int16_t ctxLatentSize;                          // latent size for context model
    int32_t *ctxFlattenLatent = NULL;               // context model flattend latent, with malloc
    int16_t *ctxCdfIndex = NULL;                    // cdf index for each dim of flattened latent, for ctx model, with malloc
    int32_t **ctxQuantizedLatent = NULL;            // context model quantized latent, with malloc
    float **ctxDequantizedLatent = NULL;            // context model dequantized latent, with malloc

    // get related handle for context model
    ModelStructHandle ctxDecHandle = contextCodecSt->decoderHandle;
    QuantizerHandle ctxQuantizerHandle = contextCodecSt->quantizerHandle;
    RangeCoderConfigHandle ctxRcHandle = contextCodecSt->rangeCoderConfig;
    // get related dims for context model
    ctxNumLatentEncode = contextCodecSt->numLatentEncode;
    ctxNumLatentChannels = contextCodecSt->numLatentChannels;
    ctxLatentSize = ctxNumLatentEncode * ctxNumLatentChannels;

    // malloc flatten latent for context model
    ctxFlattenLatent = (int32_t *)malloc(sizeof(int32_t) * ctxLatentSize);

    // malloc cdf index for context model, set cdf index according to channel idx
    ctxCdfIndex = (int16_t *)malloc(sizeof(int16_t) * ctxLatentSize);
    for (int16_t i = 0; i < ctxNumLatentEncode; i++) {
        for (int16_t j = 0; j < ctxNumLatentChannels; j++) {
            ctxCdfIndex[i * ctxNumLatentChannels + j] = j;
        }
    }

    // perform range decoding
    RangeDecodeProcess(ctxRcHandle, ctxFlattenLatent, ctxLatentSize, ctxCdfIndex,
        contextBitstream, contextNumBytes);

    // malloc and set quantized latent of context model
    ctxQuantizedLatent = (int32_t **)malloc(sizeof(int32_t *) * ctxNumLatentEncode);
    for (int16_t i = 0; i < ctxNumLatentEncode; i++) {
        ctxQuantizedLatent[i] = (int32_t *)malloc(sizeof(int32_t) * ctxNumLatentChannels);
    }
    for (int16_t i = 0; i < ctxNumLatentEncode; i++) {
        for (int16_t j = 0; j < ctxNumLatentChannels; j++) {
            ctxQuantizedLatent[i][j] = ctxFlattenLatent[i * ctxNumLatentChannels + j];
        }
    }

    // malloc dequantized latent for context model
    ctxDequantizedLatent = (float **)malloc(sizeof(float *) * ctxNumLatentEncode);
    for (int16_t i = 0; i < ctxNumLatentEncode; i++) {
        ctxDequantizedLatent[i] = (float *)malloc(sizeof(float) * ctxNumLatentChannels);
    }

    // perform dequantization
    LatentDequantize(ctxQuantizerHandle, ctxQuantizedLatent, ctxDequantizedLatent,
        ctxNumLatentEncode, ctxNumLatentChannels);

    // Loop for context model decoder cnn layers
    for (int16_t layerIdx = 0; layerIdx < ctxDecHandle->numLayers; layerIdx++) {

        // cnn layer handle
        CnnStructHandle cnnLayer;
        cnnLayer = ctxDecHandle->cnnLayers[layerIdx];

        // last cnn layer handle
        CnnStructHandle cnnLayerLast = NULL;
        if (layerIdx != 0) {
            cnnLayerLast = ctxDecHandle->cnnLayers[layerIdx - 1];
        }

        // perform transpose cnn convolution
        if (layerIdx == 0) {
#ifndef CONV_TRANS_2PART
            Conv1DTranspose(cnnLayer, ctxDequantizedLatent);
#else
            if (cnnLayer->stride == 2) {
                Conv1DTranspose2Part(cnnLayer, ctxDequantizedLatent);
            }
            else {
                Conv1DTranspose(cnnLayer, ctxDequantizedLatent);
            }
#endif
        }
        else {
#ifndef CONV_TRANS_2PART
            Conv1DTranspose(cnnLayer, cnnLayerLast->featOut);
#else
            if (cnnLayer->stride == 2) {
                Conv1DTranspose2Part(cnnLayer, cnnLayerLast->featOut);
            }
            else {
                Conv1DTranspose(cnnLayer, cnnLayerLast->featOut);
            }
#endif
        }
    }

    // free memory
    free(ctxFlattenLatent);
    ctxFlattenLatent = NULL;

    free(ctxCdfIndex);
    ctxCdfIndex = NULL;

    for (int16_t i = 0; i < ctxNumLatentEncode; i++) {
        free(ctxQuantizedLatent[i]);
        ctxQuantizedLatent[i] = NULL;
    }
    free(ctxQuantizedLatent);
    ctxQuantizedLatent = NULL;

    for (int16_t i = 0; i < ctxNumLatentEncode; i++) {
        free(ctxDequantizedLatent[i]);
        ctxDequantizedLatent[i] = NULL;
    }
    free(ctxDequantizedLatent);
    ctxDequantizedLatent = NULL;

    return 0;
}


/*
MDCT dequantization and decode
includes RC, dequantization and synthesis transform
I/O params:
    NeuralCodecHandle baseCodecSt                   (i) top level struct handle for base codec
    NeuralCodecHandle contextCodecSt                (i) top level struct handle for context codec
    NeuralQcData *neuralQcData                      (i) neural Q/C module data structure, including bs, feature scale and NF params
    float featureDec[][2]                           (o) decoded features
    int16_t numLinesNoiseFill                       (i) number of mdct lines for noise filling
    int16_t numGroups                               (i) number of groups for current frame
    int16_t *groupIndicator                         (i) group indicator vector, 0 for transient, 1 for others
*/
int16_t MdctDequantDecodeHyper(
    NeuralCodecHandle baseCodecSt,
    NeuralCodecHandle contextCodecSt,
    NeuralQcData *neuralQcData,
    float featureDec[][2],
    int16_t numLinesNoiseFill,
    int16_t numGroups,
    int16_t *groupIndicator
)
{
    float **ctxDecOutput = NULL;                     // context dec model output buffer, no malloc

    int16_t baseNumLatentEncode;                     // latent dim for base model
    int16_t baseNumLatentChannels;                   // latent num channels for base model
    int16_t baseLatentSize;                          // latent size for base model
    int32_t *baseFlattenLatent = NULL;               // base model flattened latent, with malloc
    int16_t *baseCdfIndex = NULL;                    // cdf index for each dim of flattened latent, for base model, with malloc
    int32_t **baseQuantizedLatent = NULL;            // base model quantized latent, with malloc
    float **baseDequantizedLatent = NULL;            // base model dequantized latent, with malloc
    float **baseDecOutput = NULL;                    // base dec model output buffer, no malloc
    int16_t baseDecDim;                              // base dec model output dim
    int16_t baseDecChannel;                          // base dec model output channels

    // get related handle for base model
    ModelStructHandle baseDecHandle = baseCodecSt->decoderHandle;
    QuantizerHandle baseQuantizerHandle = baseCodecSt->quantizerHandle;
    RangeCoderConfigHandle baseRcHandle = baseCodecSt->rangeCoderConfig;
    // get related dims for base model
    baseNumLatentEncode = baseCodecSt->numLatentEncode;
    baseNumLatentChannels = baseCodecSt->numLatentChannels;
    baseLatentSize = baseNumLatentEncode * baseNumLatentChannels;

    // Get related handle of qc data
    uint8_t *baseBitstream = neuralQcData->baseBitstream;
    int16_t baseNumBytes = neuralQcData->baseNumBytes;
    uint8_t *contextBitstream = neuralQcData->contextBitstream;
    int16_t contextNumBytes = neuralQcData->contextNumBytes;

    // number of latent for noise filling
    int16_t numLatentNF = 0;

    // Decode context bistream
    ContextDec(contextCodecSt, contextBitstream, contextNumBytes);

    // set context dec output to the output buffer of last cnnlayer in context dec model
    ModelStructHandle ctxDecHandle = contextCodecSt->decoderHandle;
    ctxDecOutput = ctxDecHandle->cnnLayers[ctxDecHandle->numLayers - 1]->featOut;
    
    // malloc flatten latent for base model
    baseFlattenLatent = (int32_t *)malloc(sizeof(int32_t) * baseLatentSize);

    // malloc cdf index for base model
    // set cdf index according to ctx model output and context scale table
    baseCdfIndex = (int16_t *)malloc(sizeof(int16_t) * baseLatentSize);
    for (int16_t i = 0; i < baseNumLatentEncode; i++) {
        for (int16_t j = 0; j < baseNumLatentChannels; j++) {
            int16_t index;
            for (index = 0; index < baseCodecSt->numContextScale; index++) {
                if (baseCodecSt->contextScale[index] >= ctxDecOutput[i][j]) {
                    baseCdfIndex[i * baseNumLatentChannels + j] = index;
                    break;
                }
            }
            if (index == baseCodecSt->numContextScale) {
                baseCdfIndex[i * baseCodecSt->numLatentChannels + j] = baseCodecSt->numContextScale - 1;
            }
        }
    }

    // perform range decoding
    RangeDecodeProcess(baseRcHandle, baseFlattenLatent, baseLatentSize, baseCdfIndex, 
        baseBitstream, baseNumBytes);

    // malloc and set quantized latent of base model
    baseQuantizedLatent = (int32_t **)malloc(sizeof(int32_t *) * baseNumLatentEncode);
    for (int16_t i = 0; i < baseNumLatentEncode; i++) {
        baseQuantizedLatent[i] = (int32_t *)malloc(sizeof(int32_t) * baseNumLatentChannels);
    }
    for (int16_t i = 0; i < baseNumLatentEncode; i++) {
        for (int16_t j = 0; j < baseNumLatentChannels; j++) {
            baseQuantizedLatent[i][j] = baseFlattenLatent[i * baseNumLatentChannels + j];
        }
    }

    // malloc dequantized latent for base model
    baseDequantizedLatent = (float **)malloc(sizeof(float *) * baseNumLatentEncode);
    for (int16_t i = 0; i < baseNumLatentEncode; i++) {
        baseDequantizedLatent[i] = (float *)malloc(sizeof(float) * baseNumLatentChannels);
    }

    // perform dequantization
    LatentDequantize(baseQuantizerHandle, baseQuantizedLatent, baseDequantizedLatent,
        baseNumLatentEncode, baseNumLatentChannels);

#ifdef DEBUG_SAVE
    for (int16_t i = 0; i < baseNumLatentEncode; i++) {
        for (int16_t j = 0; j < baseNumLatentChannels; j++) {
            fprintf(fQuant, "%.10f  ", baseDequantizedLatent[i][j]);
        }
        fprintf(fQuant, "\n");
    }
#endif

    // get number of latent to perform noise filling
    numLatentNF = numLinesNoiseFill;
    for (int16_t i = 0; i < baseDecHandle->numLayers; i++) {
        numLatentNF /= baseDecHandle->cnnLayers[i]->stride;
    }
    // perform noise filling
    LatentNoiseFilling(baseDequantizedLatent, baseQuantizerHandle->quantileMedian, baseNumLatentEncode,
        baseNumLatentChannels, numLatentNF, numGroups, groupIndicator, neuralQcData->nfParam, neuralQcData->nfParamQIdx);

#ifdef DEBUG_SAVE
    for (int16_t i = 0; i < baseNumLatentEncode; i++) {
        for (int16_t j = 0; j < baseNumLatentChannels; j++) {
            fprintf(fNf, "%.10f  ", baseDequantizedLatent[i][j]);
        }
        fprintf(fNf, "\n");
    }
#endif

    // dequantization of feature scale
    if (neuralQcData->isFeatAmplified == 0) {
        neuralQcData->featureScale = (float)(neuralQcData->scaleQIdx) / 127.0f;
    }
    else {
        neuralQcData->featureScale = (float)pow(10.0f, (float)(neuralQcData->scaleQIdx) / 86.0f);
    }

    if (neuralQcData->featureScale == 0.0f) {
        neuralQcData->featureScale = 1.0f;
    }

    // inverse feature scaling
    for (int16_t i = 0; i < baseNumLatentEncode; i++) {
        for (int16_t j = 0; j < baseNumLatentChannels; j++) {
            baseDequantizedLatent[i][j] /= neuralQcData->featureScale;
        }
    }

    // Loop for base model decoder cnn layers
    for (int16_t layerIdx = 0; layerIdx < baseDecHandle->numLayers; layerIdx++) {

        // cnn layer handle
        CnnStructHandle cnnLayer;
        cnnLayer = baseDecHandle->cnnLayers[layerIdx];

        // last cnn layer handle
        CnnStructHandle cnnLayerLast = NULL;
        if (layerIdx != 0) {
            cnnLayerLast = baseDecHandle->cnnLayers[layerIdx - 1];
        }

        // perform transpose cnn convolution
        if (layerIdx == 0) {
#ifndef CONV_TRANS_2PART
            Conv1DTranspose(cnnLayer, baseDequantizedLatent);
#else
            if (cnnLayer->stride == 2) {
                Conv1DTranspose2Part(cnnLayer, baseDequantizedLatent);
            }
            else {
                Conv1DTranspose(cnnLayer, baseDequantizedLatent);
            }
#endif
        }
        else {
#ifndef CONV_TRANS_2PART
            Conv1DTranspose(cnnLayer, cnnLayerLast->featOut);
#else
            if (cnnLayer->stride == 2) {
                Conv1DTranspose2Part(cnnLayer, cnnLayerLast->featOut);
            }
            else {
                Conv1DTranspose(cnnLayer, cnnLayerLast->featOut);
            }
#endif
        }
    }

    // copy last layer output to featureDec
    baseDecOutput = baseDecHandle->cnnLayers[baseDecHandle->numLayers - 1]->featOut;
    baseDecDim = baseDecHandle->cnnLayers[baseDecHandle->numLayers - 1]->featDimOut;
    baseDecChannel = baseDecHandle->cnnLayers[baseDecHandle->numLayers - 1]->numChannelsOut;
    for (int16_t i = 0; i < baseDecDim; i++) {
        for (int16_t j = 0; j < baseDecChannel; j++) {
            featureDec[i][j] = baseDecOutput[i][j];
        }
    }

#ifdef DEBUG_SAVE
    for (int16_t i = 0; i < baseDecDim; i++) {
        fprintf(fLogDec, "%.10f  ", featureDec[i]);
    }
    fprintf(fLogDec, "\n");
#endif

    // free memory
    free(baseFlattenLatent);
    baseFlattenLatent = NULL;

    free(baseCdfIndex);
    baseCdfIndex = NULL;

    for (int16_t i = 0; i < baseNumLatentEncode; i++) {
        free(baseQuantizedLatent[i]);
        baseQuantizedLatent[i] = NULL;
    }
    free(baseQuantizedLatent);
    baseQuantizedLatent = NULL;

    for (int16_t i = 0; i < baseNumLatentEncode; i++) {
        free(baseDequantizedLatent[i]);
        baseDequantizedLatent[i] = NULL;
    }
    free(baseDequantizedLatent);
    baseDequantizedLatent = NULL;

    return 0;
}


#ifdef SUPPORT_NNTYPE_LC

/*
MDCT dequantization and decode, LC profile
includes RC, dequantization and synthesis transform
I/O params:
    NeuralCodecHandle baseCodecSt                   (i) top level struct handle for base codec
    NeuralCodecHandle contextCodecSt                (i) top level struct handle for context codec
    NeuralQcData *neuralQcData                      (i) neural Q/C module data structure, including bs, feature scale and NF params
    float featureDec[][2]                           (o) decoded features
    int16_t numLinesNoiseFill                       (i) number of mdct lines for noise filling
    int16_t numGroups                               (i) number of groups for current frame
    int16_t *groupIndicator                         (i) group indicator vector, 0 for transient, 1 for others
*/
int16_t MdctDequantDecodeHyperLc(
    NeuralCodecHandle baseCodecSt,
    NeuralCodecHandle contextCodecSt,
    NeuralQcData *neuralQcData,
    float featureDec[][2],
    int16_t numLinesNoiseFill,
    int16_t numGroups,
    int16_t *groupIndicator
)
{
    float **ctxDecOutput = NULL;                     // context dec model output buffer, no malloc

    int16_t baseNumLatentEncode;                     // latent dim for base model
    int16_t baseNumLatentChannels;                   // latent num channels for base model
    int16_t baseLatentSize;                          // latent size for base model
    int32_t *baseFlattenLatent = NULL;               // base model flattened latent, with malloc
    int16_t *baseCdfIndex = NULL;                    // cdf index for each dim of flattened latent, for base model, with malloc
    int32_t **baseQuantizedLatent = NULL;            // base model quantized latent, with malloc
    float **baseDequantizedLatent = NULL;            // base model dequantized latent, with malloc
    float **baseDecOutput = NULL;                    // base dec model output buffer, no malloc
#if 0
    int16_t baseDecDim;                              // base dec model output dim
    int16_t baseDecChannel;                          // base dec model output channels
#endif

    // get related handle for base model
    ModelStructHandle baseDecHandle = baseCodecSt->decoderHandle;
    QuantizerHandle baseQuantizerHandle = baseCodecSt->quantizerHandle;
    RangeCoderConfigHandle baseRcHandle = baseCodecSt->rangeCoderConfig;
    // get related dims for base model
    baseNumLatentEncode = baseCodecSt->numLatentEncode;
    baseNumLatentChannels = baseCodecSt->numLatentChannels;
    baseLatentSize = baseNumLatentEncode * baseNumLatentChannels;

    // Get related handle of qc data
    uint8_t *baseBitstream = neuralQcData->baseBitstream;
    int16_t baseNumBytes = neuralQcData->baseNumBytes;
    uint8_t *contextBitstream = neuralQcData->contextBitstream;
    int16_t contextNumBytes = neuralQcData->contextNumBytes;

    // number of latent for noise filling
    int16_t numLatentNF = 0;

    // Decode context bistream
    ContextDec(contextCodecSt, contextBitstream, contextNumBytes);

    // set context dec output to the output buffer of last cnnlayer in context dec model
    ModelStructHandle ctxDecHandle = contextCodecSt->decoderHandle;
    ctxDecOutput = ctxDecHandle->cnnLayers[ctxDecHandle->numLayers - 1]->featOut;

    // malloc flatten latent for base model
    baseFlattenLatent = (int32_t *)malloc(sizeof(int32_t) * baseLatentSize);

    // malloc cdf index for base model
    // set cdf index according to ctx model output and context scale table
    baseCdfIndex = (int16_t *)malloc(sizeof(int16_t) * baseLatentSize);
    for (int16_t i = 0; i < baseNumLatentEncode; i++) {
        for (int16_t j = 0; j < baseNumLatentChannels; j++) {
            int16_t index;
            for (index = 0; index < baseCodecSt->numContextScale; index++) {
                if (baseCodecSt->contextScale[index] >= ctxDecOutput[i][j]) {
                    baseCdfIndex[i * baseNumLatentChannels + j] = index;
                    break;
                }
            }
            if (index == baseCodecSt->numContextScale) {
                baseCdfIndex[i * baseCodecSt->numLatentChannels + j] = baseCodecSt->numContextScale - 1;
            }
        }
    }

    // perform range decoding
    RangeDecodeProcess(baseRcHandle, baseFlattenLatent, baseLatentSize, baseCdfIndex,
        baseBitstream, baseNumBytes);

    // malloc and set quantized latent of base model
    baseQuantizedLatent = (int32_t **)malloc(sizeof(int32_t *) * baseNumLatentEncode);
    for (int16_t i = 0; i < baseNumLatentEncode; i++) {
        baseQuantizedLatent[i] = (int32_t *)malloc(sizeof(int32_t) * baseNumLatentChannels);
    }
    for (int16_t i = 0; i < baseNumLatentEncode; i++) {
        for (int16_t j = 0; j < baseNumLatentChannels; j++) {
            baseQuantizedLatent[i][j] = baseFlattenLatent[i * baseNumLatentChannels + j];
        }
    }

    // malloc dequantized latent for base model
    baseDequantizedLatent = (float **)malloc(sizeof(float *) * baseNumLatentEncode);
    for (int16_t i = 0; i < baseNumLatentEncode; i++) {
        baseDequantizedLatent[i] = (float *)malloc(sizeof(float) * baseNumLatentChannels);
    }

    // perform dequantization
    LatentDequantize(baseQuantizerHandle, baseQuantizedLatent, baseDequantizedLatent,
        baseNumLatentEncode, baseNumLatentChannels);

#ifdef DEBUG_SAVE
    for (int16_t i = 0; i < baseNumLatentEncode; i++) {
        for (int16_t j = 0; j < baseNumLatentChannels; j++) {
            fprintf(fQuant, "%.10f  ", baseDequantizedLatent[i][j]);
        }
        fprintf(fQuant, "\n");
    }
#endif

    // get number of latent to perform noise filling
#if 0
    numLatentNF = numLinesNoiseFill;
    for (int16_t i = 0; i < baseDecHandle->numLayers; i++) {
        numLatentNF /= baseDecHandle->cnnLayers[i]->stride;
    }
#else
    numLatentNF = numLinesNoiseFill / baseCodecSt->numLatentChannels;
#endif
    // perform noise filling
    LatentNoiseFilling(baseDequantizedLatent, baseQuantizerHandle->quantileMedian, baseNumLatentEncode,
        baseNumLatentChannels, numLatentNF, numGroups, groupIndicator, neuralQcData->nfParam, neuralQcData->nfParamQIdx);

#ifdef DEBUG_SAVE
    for (int16_t i = 0; i < baseNumLatentEncode; i++) {
        for (int16_t j = 0; j < baseNumLatentChannels; j++) {
            fprintf(fNf, "%.10f  ", baseDequantizedLatent[i][j]);
        }
        fprintf(fNf, "\n");
    }
#endif

    // dequantization of feature scale
#if 0
    if (neuralQcData->isFeatAmplified == 0) {
        neuralQcData->featureScale = (float)(neuralQcData->scaleQIdx) / 127.0f;
    }
    else {
        neuralQcData->featureScale = (float)pow(10.0f, (float)(neuralQcData->scaleQIdx) / 86.0f);
    }
#else
    neuralQcData->featureScale = (float)pow(10.0f, ((float)(neuralQcData->scaleQIdx) - 255.0f) / 31.875f);
#endif

    if (neuralQcData->featureScale == 0.0f) {
        neuralQcData->featureScale = 1.0f;
    }

    // inverse feature scaling
#if 0
    for (int16_t i = 0; i < baseNumLatentEncode; i++) {
        for (int16_t j = 0; j < baseNumLatentChannels; j++) {
            baseDequantizedLatent[i][j] /= neuralQcData->featureScale;
        }
    }
#else
    for (int16_t i = 0; i < baseNumLatentEncode; i++) {
        for (int16_t j = 0; j < baseNumLatentChannels; j++) {
            featureDec[i * baseNumLatentChannels + j][0] = baseDequantizedLatent[i][j] / neuralQcData->featureScale;
        }
    }
#endif

#if 0
    // Loop for base model decoder cnn layers
    for (int16_t layerIdx = 0; layerIdx < baseDecHandle->numLayers; layerIdx++) {

        // cnn layer handle
        CnnStructHandle cnnLayer;
        cnnLayer = baseDecHandle->cnnLayers[layerIdx];

        // last cnn layer handle
        CnnStructHandle cnnLayerLast = NULL;
        if (layerIdx != 0) {
            cnnLayerLast = baseDecHandle->cnnLayers[layerIdx - 1];
        }

        // perform transpose cnn convolution
        if (layerIdx == 0) {
            Conv1DTranspose(cnnLayer, baseDequantizedLatent);
        }
        else {
            Conv1DTranspose(cnnLayer, cnnLayerLast->featOut);
        }
    }

    // copy last layer output to featureDec
    baseDecOutput = baseDecHandle->cnnLayers[baseDecHandle->numLayers - 1]->featOut;
    baseDecDim = baseDecHandle->cnnLayers[baseDecHandle->numLayers - 1]->featDimOut;
    baseDecChannel = baseDecHandle->cnnLayers[baseDecHandle->numLayers - 1]->numChannelsOut;
    for (int16_t i = 0; i < baseDecDim; i++) {
        for (int16_t j = 0; j < baseDecChannel; j++) {
            featureDec[i][j] = baseDecOutput[i][j];
        }
    }
#endif

#ifdef DEBUG_SAVE
    for (int16_t i = 0; i < baseDecDim; i++) {
        fprintf(fLogDec, "%.10f  ", featureDec[i]);
    }
    fprintf(fLogDec, "\n");
#endif

    // free memory
    free(baseFlattenLatent);
    baseFlattenLatent = NULL;

    free(baseCdfIndex);
    baseCdfIndex = NULL;

    for (int16_t i = 0; i < baseNumLatentEncode; i++) {
        free(baseQuantizedLatent[i]);
        baseQuantizedLatent[i] = NULL;
    }
    free(baseQuantizedLatent);
    baseQuantizedLatent = NULL;

    for (int16_t i = 0; i < baseNumLatentEncode; i++) {
        free(baseDequantizedLatent[i]);
        baseDequantizedLatent[i] = NULL;
    }
    free(baseDequantizedLatent);
    baseDequantizedLatent = NULL;

    return 0;
}

#endif

#endif