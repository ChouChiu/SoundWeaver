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
#include <math.h>

#include "avs3_prot_com.h"
#include "avs3_prot_enc.h"
#include "avs3_rom_com.h"


#ifdef BS_HEADER_COMPAT
static short GetSampleFreqIndex(
    const long sampleFreq
)
{
    short i;
    short idx = 0;

    for (i = 0; i < AVS3_SIZE_FS_TABLE; i++) {
        if (sampleFreq == avs3SamplingRateTable[i]) {
            idx = i;
        }
    }

    return idx;
}

static short GetBitrateIndex(
    const long totalBitrate,
    const int32_t *bitrateTable
)
{
    short bitrateIdx = 0;

    for (int16_t i = 0; i < AVS3_SIZE_BITRATE_TABLE; i++) {
        if (totalBitrate == bitrateTable[i]) {
            bitrateIdx = i;
        }
    }

    return bitrateIdx;
}
#endif


static void packBit(const uint16_t bit, uint8_t **pt, uint8_t *omask)
{
    if (*omask == 0x80) {
        **pt = 0;
    }
    if (bit != 0) {
        **pt = **pt | *omask;
    }
    *omask >>= 1;
    if (*omask == 0) {
        *omask = 0x80;
        (*pt)++;
    }

    return;
}


void ResetIndicesEnc(AVS3_BSTREAM_ENC_HANDLE bsHandle, int16_t maxNumIndices)
{
    bsHandle->numBitsTot = 0;
    bsHandle->nextInd = 0;
    bsHandle->lastInd = -1;

    for (int16_t i = 0; i < maxNumIndices; i++) {
        bsHandle->indiceList[i].nBits = -1;
        bsHandle->indiceList[i].value = 0;
    }

    return;
}


void PushNextIndice(AVS3_BSTREAM_ENC_HANDLE bsHandle, uint16_t value, int16_t nBits)
{
    // check sanity
    if (nBits > 16) {
        fprintf(stderr, "Indice with value %d is trying to use %d bits which exceeds 16 bits!!\n", value, nBits);
        exit(-1);
    }
    else if (bsHandle->nextInd >= MAX_NUM_INDICES) {
        fprintf(stderr, "Total number of indices exceed limit: %d!\n", MAX_NUM_INDICES);
        exit(-1);
    }
    else if (bsHandle->indiceList[bsHandle->nextInd].nBits > 0) {
        fprintf(stderr, "Indice with value %d is trying to re-write an existing indice!\n", value);
    }

    // store values in the list
    bsHandle->indiceList[bsHandle->nextInd].value = value;
    bsHandle->indiceList[bsHandle->nextInd].nBits = nBits;
    bsHandle->nextInd++;

    // update the total number of bits already used
    bsHandle->numBitsTot += nBits;

    return;
}

void IndicesToSerial(const Indice *indiceList, const int16_t numIndices, uint8_t *bitstream, uint32_t *bitstreamSize)
{
    uint16_t mask;
    uint8_t omask;
    uint8_t *pBitstream = bitstream;
    uint32_t numBitsTot = 0;

    omask = (0x80 >> (*bitstreamSize & 0x7));
    pBitstream += *bitstreamSize >> 3;

    for (int16_t i = 0; i < numIndices; i++) {
        if (indiceList[i].nBits != -1) {
            mask = 1 << (indiceList[i].nBits - 1);
            for (int16_t j = 0; j < indiceList[i].nBits; j++) {
                packBit(indiceList[i].value & mask, &pBitstream, &omask);
                mask >>= 1;
            }
            numBitsTot += indiceList[i].nBits;
        }
    }

    *bitstreamSize += numBitsTot;

    return;
}


void WriteStereoBitstream(AVS3EncoderHandle stAvs3, uint8_t *bitstream, uint32_t *bitstreamSize)
{
    int16_t i;
    AVS3_STEREO_ENC_HANDLE hMdctStereo;
    AVS3_BSTREAM_ENC_HANDLE bsHandle = NULL;
    int16_t numUsedIndices = 0;

    hMdctStereo = stAvs3->hMdctStereo;
    bsHandle = hMdctStereo->bsHandle;

#ifndef MCR_INTEGRATE
    // MS decision
    PushNextIndice(bsHandle, hMdctStereo->isMS, NBITS_MS_FLAG);

    //energy balance
    if (hMdctStereo->isMS) {
        PushNextIndice(bsHandle, hMdctStereo->ILD, NBITS_ENERGY_BALENCE);
    }

    // bit split ratio
    PushNextIndice(bsHandle, hMdctStereo->bitsRatio, NBITS_SPLIT_STEREO);
#else
    if (hMdctStereo->useMcr == 0) {
        // MS stereo params
        // MS decision
        PushNextIndice(bsHandle, hMdctStereo->isMS, NBITS_MS_FLAG);

        //energy balance
        if (hMdctStereo->isMS) {
            PushNextIndice(bsHandle, hMdctStereo->ILD, NBITS_ENERGY_BALENCE);
        }

        // bit split ratio
        PushNextIndice(bsHandle, hMdctStereo->bitsRatio, NBITS_SPLIT_STEREO);
    }
    else {
        // MCR stereo params
        MCR_CONFIG_HANDLE mcrConfig = &hMdctStereo->mcrConfig;
        MCR_DATA_HANDLE mcrData = &hMdctStereo->mcrData;

        // isShortWin flag for left channel
        int16_t isShortWin = (stAvs3->hEncCore[0]->transformType == ONLY_SHORT_WINDOW);

        // MCR vq indices
        // for short frame, 8 bits for each vq index
        // for long/transition frame, 9 bits for each vq index
        for (i = 0; i < mcrConfig->vqVecNum[isShortWin]; i++) {
            PushNextIndice(bsHandle, mcrData->vqIdx[0][i], mcrConfig->vqNumBits[isShortWin]);
            PushNextIndice(bsHandle, mcrData->vqIdx[1][i], mcrConfig->vqNumBits[isShortWin]);
        }
    }
#endif

    // count used indices
    numUsedIndices = 0;
    for (i = 0; i < MAX_NUM_INDICES; i++)
    {
        if (bsHandle->indiceList[i].nBits != -1)
        {
            numUsedIndices++;
        }
    }
    // transform to serial by once
    IndicesToSerial(bsHandle->indiceList, numUsedIndices, bitstream, bitstreamSize);

    /* get stereo side bits used */
    hMdctStereo->stereoSideBits = bsHandle->numBitsTot;

    // reset bitstream buffer
    ResetIndicesEnc(bsHandle, MAX_NUM_INDICES);

    return;
}


#ifdef MC_ENABLE
void WriteMcBitstream(
    AVS3EncoderHandle stAvs3,
    uint8_t *bitstream,
    uint32_t *bitstreamSize,
    short chBitRatios[MAX_CHANNELS]
)
{
    int16_t i, pair;
    int32_t channelPairIndex;
    AVS3_MC_ENC_HANDLE hMdctMc;
    AVS3_MC_PAIR_DATA_HANDLE hPair;
    AVS3_BSTREAM_ENC_HANDLE bsHandle = NULL;
    int16_t numUsedIndices = 0;

    hMdctMc = stAvs3->hMcEnc;
    bsHandle = hMdctMc->bsHandle;

#ifdef IMPR_MIX_BIT_ALLOC
    PushNextIndice(bsHandle, hMdctMc->hasSilFlag, NBITS_HASSILFLAG);
    if (hMdctMc->hasSilFlag) {
        for (i = 0; i < hMdctMc->channelNum + hMdctMc->objNum; i++) {
            if ((hMdctMc->lfeExist) && (i == hMdctMc->lfeChIdx)) {
                continue;
            }
            PushNextIndice(bsHandle, hMdctMc->silFlag[i], NBITS_SILFLAG);
        }
    }
#endif

    /* pair cnt */
    PushNextIndice(bsHandle, hMdctMc->pairCnt, PAIR_NUM_DATA_BITS);
#ifndef PAIR_INFOR_ORDER
    for (pair = hMdctMc->pairCnt - 1; pair >= 0; pair--)
#else
    for (pair = 0; pair < hMdctMc->pairCnt; pair++)
#endif
    {
        hPair = &(hMdctMc->hPair[pair]);

        /* pair index */
#ifndef IMPR_MIX_BIT_ALLOC
        channelPairIndex = Pair2IndexMapping(hPair->ch1, hPair->ch2, hMdctMc->coupleChNum);
#else
        channelPairIndex = Pair2IndexMapping(hPair->ch1, hPair->ch2, hMdctMc->channelNum + hMdctMc->objNum);
#endif
        PushNextIndice(bsHandle, channelPairIndex, hMdctMc->bitsPairIndex);

        /* ILD factor */
        PushNextIndice(bsHandle, hMdctMc->mcIld[hPair->ch1], MC_EB_BITS);
        PushNextIndice(bsHandle, hMdctMc->mcIld[hPair->ch2], MC_EB_BITS);

#ifndef MC_ILD_CBQUANT
        /* scaler flag */
        PushNextIndice(bsHandle, hMdctMc->scaleFlag[hPair->ch1], 1);
        PushNextIndice(bsHandle, hMdctMc->scaleFlag[hPair->ch2], 1);
#endif
    }

    /* channel ratio */
#ifndef IMPR_MIX_BIT_ALLOC
    for (i = 0; i < hMdctMc->coupleChNum; i++) {
        PushNextIndice(bsHandle, chBitRatios[i], NBITS_MC_RATIO);
    }
#else
    int j = 0;
    for (i = 0; i < hMdctMc->channelNum + hMdctMc->objNum; i++) {
        if ((hMdctMc->lfeExist) && (i == hMdctMc->lfeChIdx)) {
            continue;
        }
        if (hMdctMc->silFlag[i] == 1) {
            continue;
        }

        PushNextIndice(bsHandle, chBitRatios[j], NBITS_MC_RATIO);
        j++;
    }
#endif

    // count used indices
    numUsedIndices = 0;
    for (i = 0; i < MAX_NUM_INDICES; i++)
    {
        if (bsHandle->indiceList[i].nBits != -1)
        {
            numUsedIndices++;
        }
    }
    // transform to serial by once
    IndicesToSerial(bsHandle->indiceList, numUsedIndices, bitstream, bitstreamSize);

    /* get stereo side bits used */
    hMdctMc->mcSideBits = bsHandle->numBitsTot;

    // reset bitstream buffer
    ResetIndicesEnc(bsHandle, MAX_NUM_INDICES);

    return;
}
#endif


void WriteHoaBitstream(AVS3EncoderHandle stAvs3) 
{
    short i, group;
    AVS3_HOA_ENC_DATA_HANDLE hEncHoa;
    AVS3_BSTREAM_ENC_HANDLE bsHandle = NULL;
    int16_t numUsedIndices = 0;
    short groupChOffset = 0;
    short idx;

    uint8_t* bitstream = stAvs3->bitstream;

    hEncHoa = stAvs3->hEncHoa;
    bsHandle = hEncHoa->bsHandle;

#ifdef AVS3_HOA_FULL_SUPPORT
    PushNextIndice(bsHandle, hEncHoa->sceneType, 4);
    
    PushNextIndice(bsHandle, hEncHoa->hHoaConfig->spatialAnalysis, 1);

    if (hEncHoa->hHoaConfig->spatialAnalysis) {
        PushNextIndice(bsHandle, hEncHoa->numVL, 4);
    }
#else
#ifdef AVS3_HOA_BUG_FIXED
    /* reserved bits */
    PushNextIndice(bsHandle, 0, 2);
#endif
#endif

#ifdef AVS3_HOA_FULL_SUPPORT
    for (i = 0; i < hEncHoa->numVL; i++)
#else
    for (i = 0; i < hEncHoa->numVote; i++)
#endif
    {
        PushNextIndice(bsHandle, hEncHoa->basisIdx[i], HOA_BASIS_BITS);
    }

    /* write bit by group */
    for (group = 0; group < hEncHoa->hHoaConfig->nTotalChanGroups; group++)
    {
        PushNextIndice(bsHandle, hEncHoa->pairIdx[group], 4);

        groupChOffset = hEncHoa->hHoaConfig->groupChOffset[group];

        if (hEncHoa->pairIdx[group] > 0)
        {
            for (i = 0; i < hEncHoa->pairIdx[group]; i++)
            {
                idx = hEncHoa->dmxInfo[group][i].chIdx;

                assert(idx >= 0);
                PushNextIndice(bsHandle, idx, hEncHoa->hHoaConfig->groupIndexBits[group]);

#ifdef AVS3_HOA_BUG_FIXED
                idx = hEncHoa->dmxInfo[group][i].ms;
                assert(idx == DMX_FULL_MS || idx == DMX_SFB_MS);

                PushNextIndice(bsHandle, idx - 1, 1);

                if (idx == DMX_SFB_MS)
                {
                    for (short sfb = 0; sfb < N_SFB_HOA_LBR - 1; sfb++)
                    {
                        PushNextIndice(bsHandle, hEncHoa->dmxInfo[group][i].sfbMask[sfb], 1);
                    }
                }
#endif
            }

#ifndef HOA_ILD_CBQUANT
            /* group energy flag */
            for (i = 0; i < hEncHoa->hHoaConfig->groupChans[group]; i++)
            {
                PushNextIndice(bsHandle, hEncHoa->flagNrg[i + groupChOffset], 1);
            }
#endif

            /* group ILD */
            for (i = 0; i < hEncHoa->hHoaConfig->groupChans[group]; i++)
            {
#ifndef HOA_ILD_CBQUANT
                PushNextIndice(bsHandle, hEncHoa->groupILD[i + groupChOffset], 4);
#else
                PushNextIndice(bsHandle, hEncHoa->groupILD[i + groupChOffset], HOA_ILD_BITS);
#endif
            }
        }

        /* group bits ratio */
        PushNextIndice(bsHandle, hEncHoa->groupBitsRatio[group], 4);

        /* total channels bits ratio */
        for (i = 0; i < hEncHoa->hHoaConfig->groupChans[group]; i++)
        {
            PushNextIndice(bsHandle, hEncHoa->bitsRatio[group][i], 4);
        }
    }


    // count used indices
    numUsedIndices = 0;
    for (i = 0; i < MAX_NUM_INDICES; i++)
    {
        if (bsHandle->indiceList[i].nBits != -1)
        {
            numUsedIndices++;
        }
    }
    // transform to serial by once
    IndicesToSerial(bsHandle->indiceList, numUsedIndices, bitstream, &stAvs3->totalSideBits);

    // reset bitstream buffer
    ResetIndicesEnc(bsHandle, MAX_NUM_INDICES);

    return;
}


static void WriteFdShapingBitstream(AVS3_BSTREAM_ENC_HANDLE bsHandle, short *lsfVqIndex, const short lsfLbrFlag)
{
    if (lsfLbrFlag == 0) {
        // high bitrate LSF VQ indices
        PushNextIndice(bsHandle, lsfVqIndex[0], LSF_STAGE1_CB1_NBITS_HBR);
        PushNextIndice(bsHandle, lsfVqIndex[1], LSF_STAGE1_CB2_NBITS_HBR);
        PushNextIndice(bsHandle, lsfVqIndex[2], LSF_STAGE2_CB1_NBITS_HBR);
        PushNextIndice(bsHandle, lsfVqIndex[3], LSF_STAGE2_CB2_NBITS_HBR);
        PushNextIndice(bsHandle, lsfVqIndex[4], LSF_STAGE2_CB3_NBITS_HBR);
        PushNextIndice(bsHandle, lsfVqIndex[5], LSF_STAGE2_CB4_NBITS_HBR);
        PushNextIndice(bsHandle, lsfVqIndex[6], LSF_STAGE2_CB5_NBITS_HBR);
    }
    else if (lsfLbrFlag == 1) {
        // low bitrate LSF VQ indices
        PushNextIndice(bsHandle, lsfVqIndex[0], LSF_STAGE1_CB1_NBITS_LBR);
        PushNextIndice(bsHandle, lsfVqIndex[1], LSF_STAGE1_CB2_NBITS_LBR);
        PushNextIndice(bsHandle, lsfVqIndex[2], LSF_STAGE2_CB1_NBITS_LBR);
        PushNextIndice(bsHandle, lsfVqIndex[3], LSF_STAGE2_CB2_NBITS_LBR);
        PushNextIndice(bsHandle, lsfVqIndex[4], LSF_STAGE2_CB3_NBITS_LBR);
    }

    return;
}


#ifdef TD_SHAPING

static void WriteTnsBitstream(AVS3_BSTREAM_ENC_HANDLE bsHandle, TnsData *tnsData)
{
    TnsBsParam *tnsBsParam = NULL;

    for (int16_t i = 0; i < TNS_MAX_FILTER_NUM; i++) {

        // get handle
        tnsBsParam = &tnsData->bsParam[i];

        // write enable flag
        PushNextIndice(bsHandle, tnsBsParam->enable, TNS_NBITS_ENABLE);

        // filter enabled, write order and huffman codes
        if (tnsBsParam->enable == 1) {

            // order
            PushNextIndice(bsHandle, tnsBsParam->order - 1, TNS_NBITS_ORDER);

            // loop over orders
            for (int16_t j = 0; j < tnsBsParam->order; j++) {

                // write huffman codes
                PushNextIndice(bsHandle, tnsBsParam->parcorHuffCode[j], tnsBsParam->parcorNbits[j]);
            }
        }
    }

    return;
}

#endif


#ifdef BWE_DEVELOPE
static void WriteBweBitstream(AVS3_BSTREAM_ENC_HANDLE bsHandle, BweConfigHandle bweConfig, BweEncDataHandle bweEncData)
{
    short i;

    // write sfb envelope
    for (i = 0; i < bweConfig->numSfb; i++) {
        PushNextIndice(bsHandle, bweEncData->sfbEnvQIdx[i], NBITS_BWE_ENV);
    }

    // write whitening level
    for (i = 0; i < bweConfig->numTiles; i++) {
        if (bweEncData->whiteningLevel[i] == BWE_WHITENING_OFF) {
            PushNextIndice(bsHandle, 0, NBITS_BWE_WHITEN_ONOFF);
        }
        else if (bweEncData->whiteningLevel[i] == BWE_WHITENING_MID) {
            PushNextIndice(bsHandle, 1, NBITS_BWE_WHITEN_ONOFF);
            PushNextIndice(bsHandle, 0, NBITS_BWE_WHITEN_MIDHIGH);
        }
        else if (bweEncData->whiteningLevel[i] == BWE_WHITENING_HIGH) {
            PushNextIndice(bsHandle, 1, NBITS_BWE_WHITEN_ONOFF);
            PushNextIndice(bsHandle, 1, NBITS_BWE_WHITEN_MIDHIGH);
        }
    }

    return;
}
#endif


void WriteCoreSideBitstream(AVS3EncoderHandle stAvs3, int16_t nChans, uint8_t *bitstream, uint32_t *bitstreamSize)
{
    int16_t i, j;
    AVS3_ENC_CORE_HANDLE hEncCore = NULL;
    AVS3_BSTREAM_ENC_HANDLE bsHandle = NULL;
    int16_t numUsedIndices = 0;

    for (i = 0; i < nChans; i++) {

        hEncCore = stAvs3->hEncCore[i];
        bsHandle = hEncCore->bsHandle;

        // Transform type
        PushNextIndice(bsHandle, hEncCore->transformType, NBITS_TRANSFORM_TYPE);

        // Spectrum shaping, LSF params
        WriteFdShapingBitstream(bsHandle, hEncCore->lsfVqIndex, hEncCore->lsfLbrFlag);

        // TNS params
#ifdef TD_SHAPING
        WriteTnsBitstream(bsHandle, &hEncCore->tnsData);
#endif

        // BWE params
#ifdef BWE_DEVELOPE
        if (hEncCore->bwePresent == 1) {
            WriteBweBitstream(bsHandle, &hEncCore->bweConfig, &hEncCore->bweEncData);
        }
#endif
    }

    // indice to serial
    for (i = 0; i < nChans; i++) {

        hEncCore = stAvs3->hEncCore[i];
        bsHandle = hEncCore->bsHandle;

        // count used indices
        numUsedIndices = 0;
        for (j = 0; j < MAX_NUM_INDICES; j++) {
            if (bsHandle->indiceList[j].nBits != -1) {
                numUsedIndices++;
            }
        }

        // transform to serial by once
        IndicesToSerial(bsHandle->indiceList, numUsedIndices, bitstream, bitstreamSize);
    }

    // reset bitstream buffer
    for (i = 0; i < nChans; i++) {
        hEncCore = stAvs3->hEncCore[i];
        ResetIndicesEnc(hEncCore->bsHandle, MAX_NUM_INDICES);
    }

    return;
}


void WriteGroupBitstream(
    AVS3EncoderHandle stAvs3,
    int16_t nChans,
    uint8_t *bitstream,
    uint32_t *bitstreamSize
)
{
    int16_t i, j;
    AVS3_ENC_CORE_HANDLE hEncCore = NULL;
    AVS3_BSTREAM_ENC_HANDLE bsHandle = NULL;
    int16_t numUsedIndices = 0;

    // grouping info
    for (i = 0; i < nChans; i++) {

        hEncCore = stAvs3->hEncCore[i];
        bsHandle = hEncCore->bsHandle;

        if (hEncCore->transformType == ONLY_SHORT_WINDOW) {

            if (hEncCore->numGroups == 1) {
                PushNextIndice(bsHandle, 0, 1);
            }
            else {
                PushNextIndice(bsHandle, 1, 1);

                for (int16_t j = 0; j < N_BLOCK_SHORT; j++) {
                    PushNextIndice(bsHandle, hEncCore->groupIndicator[j], 1);
                }
            }
        }
    }

    // indice to serial
    for (i = 0; i < nChans; i++) {

        hEncCore = stAvs3->hEncCore[i];
        bsHandle = hEncCore->bsHandle;

        // count used indices
        numUsedIndices = 0;
        for (j = 0; j < MAX_NUM_INDICES; j++) {
            if (bsHandle->indiceList[j].nBits != -1) {
                numUsedIndices++;
            }
        }

        // transform to serial by once
        IndicesToSerial(bsHandle->indiceList, numUsedIndices, bitstream, bitstreamSize);
    }

    // reset bitstream buffer
    for (i = 0; i < nChans; i++) {
        hEncCore = stAvs3->hEncCore[i];
        ResetIndicesEnc(hEncCore->bsHandle, MAX_NUM_INDICES);
    }

    return;
}


void WriteQcBitstream(AVS3EncoderHandle stAvs3, int16_t nChans, uint8_t *bitstream, uint32_t *bitstreamSize)
{
    int16_t i, j;
    AVS3_ENC_CORE_HANDLE hEncCore = NULL;
    AVS3_BSTREAM_ENC_HANDLE bsHandle = NULL;
    NeuralQcData *neuralQcData = NULL;
    int16_t numUsedIndices = 0;

    for (i = 0; i < nChans; i++) {

        hEncCore = stAvs3->hEncCore[i];
        bsHandle = hEncCore->bsHandle;
        neuralQcData = &hEncCore->neuralQcData;

#ifndef SUPPORT_NNTYPE_LC
        // isFeatAmplified
        PushNextIndice(bsHandle, neuralQcData->isFeatAmplified, NBITS_IS_FEAT_AMPLIFIED);

        // feature scale
        PushNextIndice(bsHandle, neuralQcData->scaleQIdx, NBITS_FEATURE_SCALE);
#else
        if (stAvs3->nnTypeConfig == NN_TYPE_DEFAULT_MAIN) {
            // isFeatAmplified
            PushNextIndice(bsHandle, neuralQcData->isFeatAmplified, NBITS_IS_FEAT_AMPLIFIED);

            // feature scale
            PushNextIndice(bsHandle, neuralQcData->scaleQIdx, NBITS_FEATURE_SCALE);
        }
        else if (stAvs3->nnTypeConfig == NN_TYPE_DEFAULT_LC) {
            // feature scale
            PushNextIndice(bsHandle, neuralQcData->scaleQIdx, NBITS_FEATURE_SCALE_LC);
        }
#endif

        // noise filling level
        // one/two nf params
        if (hEncCore->numGroups == 1) {
            PushNextIndice(bsHandle, neuralQcData->nfParamQIdx[0], NBITS_NF_PARAM);
        }
        else {
            PushNextIndice(bsHandle, neuralQcData->nfParamQIdx[0], NBITS_NF_PARAM);
            PushNextIndice(bsHandle, neuralQcData->nfParamQIdx[1], NBITS_NF_PARAM);
        }

        // context number bytes
        PushNextIndice(bsHandle, neuralQcData->contextNumBytes, NBITS_CONTEXT_NUM_BYTES);

        // context bitstream
        for (j = 0; j < neuralQcData->contextNumBytes; j++)
        {
            PushNextIndice(bsHandle, neuralQcData->contextBitstream[j], 8);
        }

        // base bitstream
        for (j = 0; j < neuralQcData->baseNumBytes; j++) 
        {
            PushNextIndice(bsHandle, neuralQcData->baseBitstream[j], 8);
        }
    }

    // indice to serial
    for (i = 0; i < nChans; i++) {

        hEncCore = stAvs3->hEncCore[i];
        bsHandle = hEncCore->bsHandle;

        // count used indices
        numUsedIndices = 0;
        for (j = 0; j < MAX_NUM_INDICES; j++) {
            if (bsHandle->indiceList[j].nBits != -1) {
                numUsedIndices++;
            }
        }

        // transform to serial by once
        IndicesToSerial(bsHandle->indiceList, numUsedIndices, bitstream, bitstreamSize);
    }

    // reset bitstream buffer
    for (i = 0; i < nChans; i++) {
        hEncCore = stAvs3->hEncCore[i];
        ResetIndicesEnc(hEncCore->bsHandle, MAX_NUM_INDICES);
    }

    return;
}


#ifdef BS_HEADER_COMPAT

static void Avs3WriteBsFrameHeaderCompat(
    AVS3EncoderHandle stAvs3,
    FILE *fBitstream
)
{
    // header bs buffer
    uint8_t headerBs[100] = { 0 };
    uint32_t headerBsBits = 0;
    uint32_t headerBsBytes = 0;

    // bs indice list and handle
    int16_t numUsedIndices = 0;
    Indice bsIndiceList[MAX_NUM_INDICES];
    AVS3_BSTREAM_ENC_DATA bsData;
    AVS3_BSTREAM_ENC_HANDLE bsHandle = &bsData;

    // Init bs handle
    bsHandle->indiceList = bsIndiceList;
    ResetIndicesEnc(bsHandle, MAX_NUM_INDICES);

    // payload bitstrem buffer
    uint8_t* bitstream = stAvs3->bitstream;
    uint32_t bitsFrame = (uint32_t)(stAvs3->bitsPerFrame);
    uint32_t bytesFrame = (uint32_t)(ceil((float)stAvs3->bitsPerFrame / 8));
    
    // Get CRC16
    uint16_t crcResult = 0;
    crcResult = Crc16(bitstream, bytesFrame);

    // Sync word, 12 bits
    uint16_t syncWord = SYNC_WORD_COMPAT;                               // in binary, 1111 1111 1111
    PushNextIndice(bsHandle, syncWord, NBITS_SYNC_WORD);

    // audio codec id, 4 bits
    uint16_t audioCodecId = 2;                                          // 2 for AVS3 HW branch
    PushNextIndice(bsHandle, audioCodecId, NBITS_AUDIO_CODEC_ID);

    // anc data index, fixed to 0 in HW branch, 1 bit
    uint16_t ancDataIndex = 0;
    PushNextIndice(bsHandle, ancDataIndex, NBITS_ANC_DATA_INDEX);

#ifdef SUPPORT_NNTYPE_LC
    // NN type, 3 bit
    // 0 for default main, 1 for default lc
    PushNextIndice(bsHandle, stAvs3->nnTypeConfig, NBITS_NN_TYPE);
#endif

    // coding profile, 3 bit
    // 0 for mono/stereo/mc, 1 for channel + obj mix, 2 for hoa
    uint16_t codingProfile;
#ifndef MIX_EXT
    if (stAvs3->avs3CodecFormat == AVS3_MONO_FORMAT ||
        stAvs3->avs3CodecFormat == AVS3_STEREO_FORMAT ||
        stAvs3->avs3CodecFormat == AVS3_MC_FORMAT) {
        codingProfile = 0;
    }
    else if (stAvs3->avs3CodecFormat == AVS3_MIX_FORMAT) {
        codingProfile = 1;
    }
    else if (stAvs3->avs3CodecFormat == AVS3_HOA_FORMAT) {
        codingProfile = 2;
    }
#else
    if (stAvs3->isMixedContent == 1) {
        codingProfile = 1;
    }
    else if (stAvs3->avs3CodecFormat == AVS3_MONO_FORMAT ||
        stAvs3->avs3CodecFormat == AVS3_STEREO_FORMAT ||
        stAvs3->avs3CodecFormat == AVS3_MC_FORMAT) {
        codingProfile = 0;
    }
    else if (stAvs3->avs3CodecFormat == AVS3_HOA_FORMAT) {
        codingProfile = 2;
    }
#endif
    PushNextIndice(bsHandle, codingProfile, NBITS_CODING_PROFILE);

    // sampling rate index, 4 bit
    uint16_t samplingRateIdx;
    samplingRateIdx = GetSampleFreqIndex(stAvs3->inputFs);
    PushNextIndice(bsHandle, samplingRateIdx, NBITS_SAMPLING_RATE_INDEX);

    // first part of CRC, 8 bits
    PushNextIndice(bsHandle, crcResult >> 8, AVS3_BS_BYTE_SIZE);

    uint16_t channelNumIdx;
    if (codingProfile == 0) {
        // channel number index
        // for mono/stereo/mc, 7 bits
        channelNumIdx = (uint16_t)stAvs3->channelNumConfig;
        PushNextIndice(bsHandle, channelNumIdx, NBITS_CHANNEL_NUMBER_INDEX);
    }
    else if (codingProfile == 1) {
#ifndef MIX_EXT
        // for channel + obj mix
        // channel number index, 7 bits
        channelNumIdx = (uint16_t)stAvs3->channelNumConfig;
        PushNextIndice(bsHandle, channelNumIdx, NBITS_CHANNEL_NUMBER_INDEX);
        // object number, 7 bits
        PushNextIndice(bsHandle, stAvs3->numObjsInput - 1, NBITS_NUM_OBJS);
#else
        // sound bed type
        PushNextIndice(bsHandle, stAvs3->soundBedType, NBITS_SOUNDBED_TYPE);

        if (stAvs3->soundBedType == 0) {
            // for only objs
            // object number, 7 bits
            PushNextIndice(bsHandle, stAvs3->numObjsInput - 1, NBITS_NUM_OBJS);
            // bitrate index for each obj, 4 bits
            uint16_t bitrateIdxPerObj;
            bitrateIdxPerObj = GetBitrateIndex(stAvs3->bitratePerObj, codecBitrateConfigTable[CHANNEL_CONFIG_MONO].bitrateTable);
            PushNextIndice(bsHandle, bitrateIdxPerObj, NBITS_BITRATE_INDEX);
        }
        else if (stAvs3->soundBedType == 1) {
            // for MC+objs
            // channel number index, 7 bits
            channelNumIdx = (uint16_t)stAvs3->channelNumConfig;
            PushNextIndice(bsHandle, channelNumIdx, NBITS_CHANNEL_NUMBER_INDEX);
            // bitrate index for sound bed, 4 bits
            uint16_t bitrateIdxBedMc;
            bitrateIdxBedMc = GetBitrateIndex(stAvs3->bitrateBedMc, codecBitrateConfigTable[stAvs3->channelNumConfig].bitrateTable);
            PushNextIndice(bsHandle, bitrateIdxBedMc, NBITS_BITRATE_INDEX);

            // object number, 7 bits
            PushNextIndice(bsHandle, stAvs3->numObjsInput - 1, NBITS_NUM_OBJS);
            // bitrate index for each obj, 4 bits
            uint16_t bitrateIdxPerObj;
            bitrateIdxPerObj = GetBitrateIndex(stAvs3->bitratePerObj, codecBitrateConfigTable[CHANNEL_CONFIG_MONO].bitrateTable);
            PushNextIndice(bsHandle, bitrateIdxPerObj, NBITS_BITRATE_INDEX);
        }
#endif
    }
    else if (codingProfile == 2) {
        // for HOA, 4 bits
        // Note, for hoa, channelNumIdx is not transmitted
        channelNumIdx = (uint16_t)stAvs3->channelNumConfig;
        PushNextIndice(bsHandle, stAvs3->hEncHoa->hHoaConfig->order - 1, NBITS_HOA_ORDER);
    }

    // resolution, i.e. bitDepth, 2 bits
    uint16_t resolution = -1;
    if (stAvs3->bitDepth == 8) {
        resolution = 0;
    }
    else if (stAvs3->bitDepth == 16) {
        resolution = 1;
    }
    else if (stAvs3->bitDepth == 24) {
        resolution = 2;
    }
    PushNextIndice(bsHandle, resolution, NBITS_RESOLUTION);

#ifndef MIX_EXT
    // bitrate index, 4 bits
    uint16_t bitrateIdx;
    bitrateIdx = GetBitrateIndex(stAvs3->totalBitrate, codecBitrateConfigTable[channelNumIdx].bitrateTable);
    PushNextIndice(bsHandle, bitrateIdx, NBITS_BITRATE_INDEX);
#else
    if (stAvs3->isMixedContent == 0) {
        // bitrate index, 4 bits
        uint16_t bitrateIdx;
        bitrateIdx = GetBitrateIndex(stAvs3->totalBitrate, codecBitrateConfigTable[channelNumIdx].bitrateTable);
        PushNextIndice(bsHandle, bitrateIdx, NBITS_BITRATE_INDEX);
    }
#endif

    // second part of CRC, 8 bits
    PushNextIndice(bsHandle, crcResult & 0xFF, AVS3_BS_BYTE_SIZE);

    // count used indices
    numUsedIndices = 0;
    for (int16_t i = 0; i < MAX_NUM_INDICES; i++) {
        if (bsHandle->indiceList[i].nBits != -1) {
            numUsedIndices++;
        }
    }
    // transform to serial by once
    IndicesToSerial(bsHandle->indiceList, numUsedIndices, headerBs, &headerBsBits);

    // reset bitstream buffer
    ResetIndicesEnc(bsHandle, MAX_NUM_INDICES);

    // write frame header to file
    headerBsBytes = (uint32_t)(ceil((float)headerBsBits / 8));
    fwrite(headerBs, sizeof(uint8_t), headerBsBytes, fBitstream);

    return;
}

#endif


void Avs3FlushBitstream(AVS3EncoderHandle stAvs3, FILE *fBitstream)
{
    size_t retBits = 0;
    uint8_t* bitstream = stAvs3->bitstream;
    uint32_t bitsFrame = (uint32_t)(stAvs3->bitsPerFrame);
    uint32_t bytesFrame = (uint32_t)(ceil((float)stAvs3->bitsPerFrame / 8));

    /* bit stream verification */
    assert((bitsFrame - stAvs3->totalSideBits) < AVS3_BS_PADDING);

#ifdef BS_HEADER_COMPAT
    /* Write frame header */
    Avs3WriteBsFrameHeaderCompat(stAvs3, fBitstream);
#endif

    /* write side information */
    if ((retBits = fwrite(bitstream, sizeof(uint8_t), bytesFrame, fBitstream)) != bytesFrame)
    {
        fprintf(stderr, "Error write bitstream!\n");
        exit(-1);
    }

    // reset total side bits counter
    stAvs3->totalSideBits = 0;

    // reset bitstream buffer
    SetUC(stAvs3->bitstream, 0, MAX_BS_BYTES);

    // reset first frame indicator
    stAvs3->initFrame = 0;

    return;
}
