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
#include <math.h>

#include "avs3_options.h"
#include "avs3_cnst_com.h"
#include "avs3_cnst_enc.h"
#include "avs3_rom_com.h"
#include "avs3_stat_com.h"
#include "avs3_stat_enc.h"
#include "avs3_prot_com.h"
#include "avs3_prot_enc.h"

#ifndef BWE_DEVELOPE
static void InitEncoderCore(AVS3_ENC_CORE_HANDLE hEncCore, Indice *indiceList, const short frameLength)
#else
static void InitEncoderCore(
    AVS3_ENC_CORE_HANDLE hEncCore, 
    Indice *indiceList, 
    const short frameLength,
    const short codecFormat,
    const long totalBitrate,
    const short nChans
)
#endif
{
    AVS3_CORE_CONFIG_DATA_HANDLE hCoreConfig = NULL;
    AVS3_BSTREAM_ENC_HANDLE bsHandle = NULL;

    /* initial input buffer */
    SetZero(hEncCore->signalBuffer, FRAME_LEN + FRAME_LEN + N_SAMPLES_LOOKAHEAD);
    hEncCore->inputSignal = hEncCore->signalBuffer + FRAME_LEN;
    hEncCore->lookahead = hEncCore->inputSignal + FRAME_LEN;

    SetZero(hEncCore->origSpectrum, BLOCK_LEN_LONG);
    SetZero(hEncCore->synthBuffer, MAX_OVL_SIZE);
    
    hEncCore->frameLength = frameLength;
    hEncCore->lookaheadSamples = hEncCore->frameLength / 2;
    hEncCore->coreSideBits = 0;

    // init bitstream handle and hook indice list to top level st
    if ((bsHandle = (AVS3_BSTREAM_ENC_HANDLE)malloc(sizeof(AVS3_BSTREAM_ENC_DATA))) == NULL)
    {
        fprintf(stderr, "Can not allocate memory for AVS3 Encoder Core bitstream structure.\n");
        exit(-1);
    }
    // hook up indice list to top level st
    bsHandle->indiceList = indiceList;
    // reset indice list
    ResetIndicesEnc(bsHandle, MAX_NUM_INDICES);

    // init window type detector
    InitWindowTypeDetect(hEncCore->frameLength, &hEncCore->winTypeDetector);

#ifdef FD_SHAPING
    // init FD spectrum shaping
    hEncCore->memPreemph = 0.0f;
    Mvf2f(mean_lsp, hEncCore->oldLsp, LPC_ORDER);
    SetZero(hEncCore->lsfQ, LPC_ORDER);
    SetShort(hEncCore->lsfVqIndex, 0, LSF_CB_NUM_HBR);
    // low bitrate flag for LSF VQ
    if (((float)totalBitrate / (float)nChans) > LSF_Q_LBR_THRESH) {
        hEncCore->lsfLbrFlag = 0;
    }
    else {
        hEncCore->lsfLbrFlag = 1;
    }
#endif

#ifdef TD_SHAPING
    // init TNS data
    TnsParaInit(&hEncCore->tnsData);
#endif

#ifdef BWE_DEVELOPE
    // init bwe
    hEncCore->bwePresent = GetBwePresent(codecFormat, totalBitrate, nChans);
    if (hEncCore->bwePresent == 1) {
        BweGetConfig(&hEncCore->bweConfig, codecFormat, totalBitrate, nChans);
        InitBweEncData(&hEncCore->bweEncData);

#ifdef DEBUG_NEURAL_QC_LOCAL_SYNTH
        InitBweDecData(&hEncCore->bweDecData);
#endif
    }
#endif

    // init core coder config
    if ((hCoreConfig = (AVS3_CORE_CONFIG_DATA_HANDLE)malloc(sizeof(AVS3_CORE_CONFIG_DATA))) == NULL)
    {
        fprintf(stderr, "Can not allocate memory for AVS3 Encoder Core configure structure.\n");
        exit(-1);
    }

    InitCoreConfig(hCoreConfig, hEncCore->frameLength);
    
    hEncCore->bsHandle = bsHandle;
    hEncCore->hCoreConfig = hCoreConfig;

    return;
}

void Avs3EncCreateMono(AVS3EncoderHandle stAvs3)
{
    AVS3_MONO_ENC_HANDLE hMono = NULL;

    if ((stAvs3->hEncCore[0] = (AVS3_ENC_CORE_HANDLE)malloc(sizeof(AVS3_ENC_CORE_DATA))) == NULL)
    {
        fprintf(stderr, "Can not allocate memory for AVS3 encoder core data structure.\n");
        exit(-1);
    }

#ifndef BWE_DEVELOPE
    InitEncoderCore(stAvs3->hEncCore[0], NULL ,stAvs3->frameLength);
#else
#ifdef MONO_INTEGRATE
    InitEncoderCore(stAvs3->hEncCore[0], stAvs3->bsIndiceList[0], stAvs3->frameLength,
        stAvs3->avs3CodecFormat, stAvs3->totalBitrate, 1);
#else
    InitEncoderCore(stAvs3->hEncCore[0], NULL, stAvs3->frameLength,
        stAvs3->avs3CodecFormat, stAvs3->totalBitrate, 1);
#endif
#endif

    /* create mono structure */
    if ((hMono = (AVS3_MONO_ENC_HANDLE)malloc(sizeof(AVS3_MONO_ENC_DATA))) == NULL)
    {
        fprintf(stderr, "Can not allocate memory for AVS3 Mono data structure.\n");
        exit(-1);
    }

    stAvs3->hMono = hMono;
}

// create stereo encoder
void Avs3EncCreateStereo(AVS3EncoderHandle stAvs3)
{
    short ch;
    AVS3_STEREO_ENC_HANDLE hMdctStereo = NULL;
    AVS3_BSTREAM_ENC_HANDLE bsHandle = NULL;

    // core coder st malloc and init
    for (ch = 0; ch < STEREO_CHANNELS; ch++)
    {
        if ((stAvs3->hEncCore[ch] = (AVS3_ENC_CORE_HANDLE)malloc(sizeof(AVS3_ENC_CORE_DATA))) == NULL)
        {
            fprintf(stderr, "Can not allocate memory for AVS3 encoder core data structure.\n");
            exit(-1);
        }

        // init core encoder
#ifndef BWE_DEVELOPE
        InitEncoderCore(stAvs3->hEncCore[ch], stAvs3->bsIndiceList[ch], stAvs3->frameLength);
#else
        InitEncoderCore(stAvs3->hEncCore[ch], stAvs3->bsIndiceList[ch], stAvs3->frameLength, 
            stAvs3->avs3CodecFormat, stAvs3->totalBitrate, STEREO_CHANNELS);
#endif
    }

    // stereo st malloc
    if ((hMdctStereo = (AVS3_STEREO_ENC_HANDLE)malloc(sizeof(AVS3_STEREO_MDCT_ENC_DATA))) == NULL)
    {
        fprintf(stderr, "Can not allocate memory for AVS3 MDCT Stereo data structure.\n");
        exit(-1);
    }

#ifdef MCR_INTEGRATE
    // set on/off of MCR
    hMdctStereo->useMcr = 0;
    if (stAvs3->totalBitrate <= TH_BR_MCR_STEREO) {
        hMdctStereo->useMcr = 1;
    }
    // init MCR config and data
    if (hMdctStereo->useMcr == 1) {
        InitMcrConfig(&hMdctStereo->mcrConfig);
        InitMcrData(&hMdctStereo->mcrData);
    }
#endif

    // init bitstream handle and hook indice list to top level st
    if ((bsHandle = (AVS3_BSTREAM_ENC_HANDLE)malloc(sizeof(AVS3_BSTREAM_ENC_DATA))) == NULL)
    {
        fprintf(stderr, "Can not allocate memory for AVS3 Encoder Core bitstream structure.\n");
        exit(-1);
    }

    // hook up indice list to top level st
    bsHandle->indiceList = stAvs3->bsIndiceList[BS_INDEX_STEREO];

    // reset indice list
    ResetIndicesEnc(bsHandle, MAX_NUM_INDICES);

    hMdctStereo->stereoSideBits = 0;

    hMdctStereo->bsHandle = bsHandle;

    stAvs3->hMdctStereo = hMdctStereo;

    return;
}


#ifdef MC_ENABLE
// create multi channel encoder
void Avs3EncCreateMc(AVS3EncoderHandle stAvs3)
{
    short ch;
    short i;
    AVS3_BSTREAM_ENC_HANDLE bsHandle = NULL;

    AVS3_MC_ENC_HANDLE hMdctMc = NULL;
    AVS3_MC_DEC_HANDLE hMdctMcDec = NULL;

    // core coder st malloc and init
    for (ch = 0; ch < stAvs3->numChansInput; ch++)
    {
        if ((stAvs3->hEncCore[ch] = (AVS3_ENC_CORE_HANDLE)malloc(sizeof(AVS3_ENC_CORE_DATA))) == NULL)
        {
            fprintf(stderr, "Can not allocate memory for AVS3 encoder core data structure.\n");
            exit(-1);
        }

        // init core encoder
#ifndef BWE_DEVELOPE
        InitEncoderCore(stAvs3->hEncCore[ch], stAvs3->bsIndiceList[ch], stAvs3->frameLength);
#else

#ifndef MIX_EXT
        if (LFE_STATUS) {
#else
        if (stAvs3->hasLfe) {
#endif
            // if LFE exist, exclude LFE channel from number channels in core init
            InitEncoderCore(stAvs3->hEncCore[ch], stAvs3->bsIndiceList[ch], stAvs3->frameLength,
                stAvs3->avs3CodecFormat, stAvs3->totalBitrate, stAvs3->numChansInput - 1);
        }
        else {
            InitEncoderCore(stAvs3->hEncCore[ch], stAvs3->bsIndiceList[ch], stAvs3->frameLength,
                stAvs3->avs3CodecFormat, stAvs3->totalBitrate, stAvs3->numChansInput);
        }
#endif
    }

    /* ENC init */
    if ((hMdctMc = (AVS3_MC_ENC_HANDLE)malloc(sizeof(AVS3_MC_ENC_DATA))) == NULL)
    {
        fprintf(stderr, "Can not allocate memory for AVS3 MDCT Mc data structure.\n");
        exit(-1);
    }
    stAvs3->hMcEnc = hMdctMc;

#ifndef IMPR_MIX_BIT_ALLOC
    hMdctMc->channelNum = stAvs3->numChansInput;
#else
    hMdctMc->hasSilFlag = 0;
    SetShort(hMdctMc->silFlag, 0, MAX_CHANNELS);
    hMdctMc->bedBitsRatio = 0.0f;
    if (stAvs3->isMixedContent == 1) {
        hMdctMc->channelNum = stAvs3->numChansInput - stAvs3->numObjsInput;
        hMdctMc->isMixed = 1;
        if (stAvs3->soundBedType == 1) {
            hMdctMc->mixAllocStrategy = MIX_ALLOC_STRATEGY_OBJ2BED;
        }
        else {
            hMdctMc->mixAllocStrategy = MIX_ALLOC_STRATEGY_INTERNAL;
        }
        hMdctMc->objNum = stAvs3->numObjsInput;
    }
    else {
        hMdctMc->mixAllocStrategy = MIX_ALLOC_STRATEGY_INTERNAL;
        hMdctMc->channelNum = stAvs3->numChansInput;
        hMdctMc->isMixed = 0;

        /* obj releted */
        hMdctMc->objNum = 0;
    }
#endif

#ifndef MIX_EXT
    hMdctMc->lfeExist = LFE_STATUS;
    hMdctMc->lfeChIdx = LFE_CHANNEL_INDEX;
#else
    // set lfe status
    hMdctMc->lfeExist = stAvs3->hasLfe;
    if (hMdctMc->lfeExist == 0) {
        hMdctMc->lfeChIdx = -1;
    }
    else {
        hMdctMc->lfeChIdx = LFE_CHANNEL_INDEX;
    }
#endif

    if (hMdctMc->lfeExist) {
        hMdctMc->coupleChNum = hMdctMc->channelNum - 1;
#ifndef IMPR_MIX_BIT_ALLOC
        hMdctMc->lfeBytes = GetLfeAllocBytes(stAvs3->totalBitrate, hMdctMc->coupleChNum);
#else
        if (hMdctMc->isMixed && (stAvs3->soundBedType == 1)) {
            hMdctMc->lfeBytes = GetLfeAllocBytes(stAvs3->bitrateBedMc, hMdctMc->coupleChNum);
        }
        else {
            hMdctMc->lfeBytes = GetLfeAllocBytes(stAvs3->totalBitrate, hMdctMc->coupleChNum);
        }
#endif
    }
    else {
        hMdctMc->coupleChNum = hMdctMc->channelNum;
        hMdctMc->lfeBytes = 0;
    }

    hMdctMc->pairCnt = 0;
#ifndef IMPR_MIX_BIT_ALLOC
    hMdctMc->bitsPairIndex = max(1, (short)(floor((log(hMdctMc->coupleChNum * (hMdctMc->coupleChNum - 1) / 2 - 1) / log(2.))) + 1));
#else
    hMdctMc->bitsPairIndex = max(1, (short)(floor((log((hMdctMc->channelNum + hMdctMc->objNum) *
        (hMdctMc->channelNum + hMdctMc->objNum - 1) / 2 - 1) / log(2.))) + 1));
#endif

    i = 0;
#ifndef IMPR_MIX_BIT_ALLOC
    for (ch = 0; ch < stAvs3->numChansInput; ch++)
    {
        if (ch != hMdctMc->lfeChIdx || !hMdctMc->lfeExist)
        {
            hMdctMc->mcSpectrum[i] = stAvs3->hEncCore[ch]->origSpectrum;
            hMdctMc->frameType[i] = &(stAvs3->hEncCore[ch]->transformType);
            i++;
        }
    }

    if (hMdctMc->lfeExist)
    {
        hMdctMc->mcSpectrum[stAvs3->numChansInput - 1] = stAvs3->hEncCore[hMdctMc->lfeChIdx]->origSpectrum;
        hMdctMc->frameType[stAvs3->numChansInput - 1] = &(stAvs3->hEncCore[hMdctMc->lfeChIdx]->transformType);
    }
#else
    for (ch = 0; ch < stAvs3->numChansInput - stAvs3->numObjsInput; ch++)
    {
        if (ch != hMdctMc->lfeChIdx || !hMdctMc->lfeExist)
        {
            hMdctMc->mcSpectrum[i] = stAvs3->hEncCore[ch]->origSpectrum;
            hMdctMc->frameType[i] = &(stAvs3->hEncCore[ch]->transformType);
            i++;
        }
    }

    if (hMdctMc->lfeExist)
    {
        hMdctMc->mcSpectrum[stAvs3->numChansInput - stAvs3->numObjsInput - 1] = stAvs3->hEncCore[hMdctMc->lfeChIdx]->origSpectrum;
        hMdctMc->frameType[stAvs3->numChansInput - stAvs3->numObjsInput - 1] = &(stAvs3->hEncCore[hMdctMc->lfeChIdx]->transformType);
    }

    for (ch = stAvs3->numChansInput - stAvs3->numObjsInput; ch < stAvs3->numChansInput; ch++)
    {
        hMdctMc->mcSpectrum[ch] = stAvs3->hEncCore[ch]->origSpectrum;
        hMdctMc->frameType[ch] = &(stAvs3->hEncCore[ch]->transformType);
    }
#endif

    // init bitstream handle and hook indice list to top level st
    if ((bsHandle = (AVS3_BSTREAM_ENC_HANDLE)malloc(sizeof(AVS3_BSTREAM_ENC_DATA))) == NULL)
    {
        fprintf(stderr, "Can not allocate memory for AVS3 Encoder Core bitstream structure.\n");
        exit(-1);
    }

    // hook up indice list to top level st
    bsHandle->indiceList = stAvs3->bsIndiceList[BS_INDEX_MC];

    // reset indice list
    ResetIndicesEnc(bsHandle, MAX_NUM_INDICES);

    hMdctMc->mcSideBits = 0;

    hMdctMc->bsHandle = bsHandle;

#ifdef DEBUG_NEURAL_QC_LOCAL_SYNTH
    /* DEC init , for local decoder */
    if ((hMdctMcDec = (AVS3_MC_DEC_HANDLE)malloc(sizeof(AVS3_MC_DEC_DATA))) == NULL)
    {
        fprintf(stderr, "Can not allocate memory for AVS3 MDCT Mc dec data structure.\n");
        exit(-1);
    }
    stAvs3->hMcDec = hMdctMcDec;

    hMdctMcDec->channelNum = stAvs3->numChansInput;

#ifndef MIX_EXT
    hMdctMcDec->lfeExist = LFE_STATUS;

    hMdctMcDec->lfeChIdx = LFE_CHANNEL_INDEX;
#else
    hMdctMcDec->lfeExist = hMdctMc->lfeExist;
    hMdctMcDec->lfeChIdx = hMdctMc->lfeChIdx;
#endif

    if (hMdctMcDec->lfeExist) {
        hMdctMcDec->coupleChNum = hMdctMcDec->channelNum - 1;
    }
    else {
        hMdctMcDec->coupleChNum = hMdctMcDec->channelNum;
    }
    hMdctMcDec->pairCnt = 0;
    hMdctMcDec->bitsPairIndex = max(1, (short)(floor((log(hMdctMcDec->coupleChNum * (hMdctMcDec->coupleChNum - 1) / 2 - 1) / log(2.))) + 1));

    for (ch = 0; ch < stAvs3->numChansInput; ch++)
    {
        hMdctMcDec->mcSpectrum[ch] = hMdctMc->mcSpectrum[ch];
    }

#endif

    return;
}
#endif


// create hoa encoder
void Avs3EncCreateHoa(AVS3EncoderHandle stAvs3) 
{
    short i, ch, group, offset;
    AVS3_HOA_ENC_DATA_HANDLE hEncHoa = NULL;
    AVS3_HOA_CONFIG_DATA_HANDLE hHoaConfig = NULL;
    AVS3_BSTREAM_ENC_HANDLE bsHandle = NULL;

    /* HOA data structure */
    if ((hEncHoa = (AVS3_HOA_ENC_DATA_HANDLE)malloc(sizeof(AVS3_HOA_ENC_DATA))) == NULL)
    {
        fprintf(stderr, "Can not allocate memory for AVS3 HOA data structure.\n");
        exit(-1);
    }

    /* HOA data configure structure */
    if ((hHoaConfig = (AVS3_HOA_CONFIG_DATA_HANDLE)malloc(sizeof(AVS3_HOA_CONFIG_DATA))) == NULL)
    {
        fprintf(stderr, "Can not allocate memory for AVS3 HOA data configuration structure.\n");
        exit(-1);
    }

    // init hoa config
    Avs3HoaInitConfig(hHoaConfig, stAvs3->numChansInput, stAvs3->frameLength, stAvs3->bwidth, stAvs3->totalBitrate);

    // init core coder for hoa transport channels
    for (ch = 0; ch < hHoaConfig->nTotalChansTransport; ch++)
    {
        if ((stAvs3->hEncCore[ch] = (AVS3_ENC_CORE_HANDLE)malloc(sizeof(AVS3_ENC_CORE_DATA))) == NULL)
        {
            fprintf(stderr, "Can not allocate memory for AVS3 encoder core data structure.\n");
            exit(-1);
        }

        // init core encoder
#ifndef BWE_DEVELOPE
        InitEncoderCore(stAvs3->hEncCore[ch], stAvs3->bsIndiceList[ch], stAvs3->frameLength);
#else

#ifdef AVS3_HOA_FULL_SUPPORT
        InitEncoderCore(stAvs3->hEncCore[ch], stAvs3->bsIndiceList[ch], stAvs3->frameLength,
            hHoaConfig->innerFormat, stAvs3->totalBitrate, hHoaConfig->nTotalChansTransport);
#else
        InitEncoderCore(stAvs3->hEncCore[ch], stAvs3->bsIndiceList[ch], stAvs3->frameLength,
            stAvs3->avs3CodecFormat, stAvs3->totalBitrate, hHoaConfig->nTotalChansTransport);
#endif

#endif
    }

    if ((bsHandle = (AVS3_BSTREAM_ENC_HANDLE)malloc(sizeof(AVS3_BSTREAM_ENC_DATA))) == NULL)
    {
        fprintf(stderr, "Can not allocate memory for AVS3 Encoder Core bitstream structure.\n");
        exit(-1);
    }

    // hook up indice list to top level st
    bsHandle->indiceList = stAvs3->bsIndiceList[BS_INDEX_HOA];

    // reset indice list
    ResetIndicesEnc(bsHandle, MAX_NUM_INDICES);

    // init hoa data structure
    for (ch = 0; ch < MAX_HOA_CHANNELS; ch++) 
    {
        SetZero(hEncHoa->hoaSignalBuffer[ch], HOA_LEN_TRANSFORM);
        SetZero(hEncHoa->origSpecturm[ch], HOA_LEN_FRAME48k);
        SetZero(hEncHoa->synthBuffer[ch], HOA_OVERLAP_SIZE);
    }

    for (group = 0; group < hHoaConfig->nTotalChanGroups; group++)
    {
        for (i = 0; i < MAX_HOA_DMX_CHANNELS / 2; i++)
        {
            hEncHoa->dmxInfo[group][i].ch1 = -1;
            hEncHoa->dmxInfo[group][i].ch2 = -1;
            hEncHoa->dmxInfo[group][i].ms = AVS3_FALSE;
            hEncHoa->dmxInfo[group][i].chIdx = -1;
        }

        offset = hHoaConfig->groupChOffset[group];
        for (i = 0; i < hHoaConfig->groupChans[group]; i++) 
        {
            stAvs3->hEncCore[i + offset]->bwePresent = hHoaConfig->groupBwe[group];
        }
    }

    SetShort(hEncHoa->basisIdx, 0, MAX_HOA_BASIS);
    SetShort(hEncHoa->lastBasisIdx, 0, MAX_HOA_BASIS);
    SetShort(hEncHoa->pairIdx, 0, MAX_HOA_DMX_GROUPS);
    SetShort(hEncHoa->lastPairIdx, 0, MAX_HOA_DMX_GROUPS);
    SetShort(hEncHoa->pairFlag, 0, MAX_HOA_DMX_CHANNELS);

#ifdef AVS3_HOA_VL_SELECTION
    hEncHoa->direcSignalNrgRatio = 0.f;
    SetShort(hEncHoa->basisIdxMax, 0, MAX_HOA_BASIS);
    SetZero(hEncHoa->frameVote, L_HOA_BASIS_COLS);
#endif

#ifdef AVS3_HOA_FULL_SUPPORT
    hEncHoa->numVL = hHoaConfig->spatialAnalysis ? hHoaConfig->nTotalForeChans : 0;
    hEncHoa->numVote = hEncHoa->numVL;
    hEncHoa->sceneType = 0;
#else
    hEncHoa->numVote = hHoaConfig->spatialAnalysis ? hHoaConfig->nTotalForeChans : 0;
#endif

    hEncHoa->bsHandle = bsHandle;
    hEncHoa->hHoaConfig = hHoaConfig;

    // set handle to st
    stAvs3->hEncHoa = hEncHoa;

    return;
}


#ifdef MIX_DEVELOPE
// Creat mix encoder
void Avs3EncCreateMix(AVS3EncoderHandle stAvs3)
{
    // use MC encoder as core coder
    Avs3EncCreateMc(stAvs3);

    return;
}
#endif


#ifdef METADATA_EXT
void Avs3EncCreateMetaData(AVS3EncoderHandle stAvs3)
{
    Avs3EncMetaDataHandle hAvs3MetaData = NULL;
    AVS3_BSTREAM_ENC_HANDLE bsHandle = NULL;

    if ((hAvs3MetaData = (Avs3EncMetaDataHandle)malloc(sizeof(Avs3EncMetaData))) == NULL) {
        fprintf(stderr, "Can not allocate memory for AVS3 MetaData structure.\n");
        exit(-1);
    }

    stAvs3->hAvs3MetaData = hAvs3MetaData;

    if ((bsHandle = (AVS3_BSTREAM_ENC_HANDLE)malloc(sizeof(AVS3_BSTREAM_ENC_DATA))) == NULL) {
        fprintf(stderr, "Can not allocate memory for AVS3 MetaData BStream structure.\n");
        exit(-1);
    }

    bsHandle->indiceList = stAvs3->bsIndiceList[BS_INDEX_META];

    ResetIndicesEnc(bsHandle, MAX_NUM_INDICES);

    hAvs3MetaData->bsHandle = bsHandle;

    return;
}
#endif


// init encoder st
void Avs3EncoderInit(AVS3EncoderHandle stAvs3, FILE** fModel)
{
    short ch;

    // first frame indicator
    stAvs3->initFrame = 1;

    // number of look ahead samples
    stAvs3->lookaheadSamples = stAvs3->frameLength / 2;

    // number of bits used
    stAvs3->totalSideBits = 0;

    /* set number of bits per frame */
#ifndef SUPPORT_HIGH_BR_MIX
    stAvs3->bitsPerFrame = (short)(((float)stAvs3->totalBitrate / (float)stAvs3->inputFs) * stAvs3->frameLength);
#else
    stAvs3->bitsPerFrame = (int32_t)(((float)stAvs3->totalBitrate / (float)stAvs3->inputFs) * stAvs3->frameLength);
#endif

#ifdef BS_HEADER_COMPAT
    /* subtract frame bs header bits */
#ifndef MIX_EXT
    if (stAvs3->avs3CodecFormat == AVS3_MONO_FORMAT) {
        stAvs3->bitsPerFrame -= NBITS_FRAME_HEADER_MONO;
    }
    else if (stAvs3->avs3CodecFormat == AVS3_STEREO_FORMAT) {
        stAvs3->bitsPerFrame -= NBITS_FRAME_HEADER_STEREO;
    }
    else if (stAvs3->avs3CodecFormat == AVS3_MC_FORMAT) {
        stAvs3->bitsPerFrame -= NBITS_FRAME_HEADER_MC;
    }
    else if (stAvs3->avs3CodecFormat == AVS3_HOA_FORMAT) {
        stAvs3->bitsPerFrame -= NBITS_FRAME_HEADER_HOA;
    }
    else if (stAvs3->avs3CodecFormat == AVS3_MIX_FORMAT) {
        stAvs3->bitsPerFrame -= NBITS_FRAME_HEADER_MIX;
    }
#else
    if (stAvs3->isMixedContent == 0) {
        if (stAvs3->avs3CodecFormat == AVS3_MONO_FORMAT) {
            stAvs3->bitsPerFrame -= NBITS_FRAME_HEADER_MONO;
        }
        else if (stAvs3->avs3CodecFormat == AVS3_STEREO_FORMAT) {
            stAvs3->bitsPerFrame -= NBITS_FRAME_HEADER_STEREO;
        }
        else if (stAvs3->avs3CodecFormat == AVS3_MC_FORMAT) {
            stAvs3->bitsPerFrame -= NBITS_FRAME_HEADER_MC;
        }
        else if (stAvs3->avs3CodecFormat == AVS3_HOA_FORMAT) {
            stAvs3->bitsPerFrame -= NBITS_FRAME_HEADER_HOA;
        }
    }
    else {
        if (stAvs3->soundBedType == 0) {
            stAvs3->bitsPerFrame -= NBITS_FRAME_HEADER_MIX_SBT0;
        }
        else if (stAvs3->soundBedType == 1) {
            stAvs3->bitsPerFrame -= NBITS_FRAME_HEADER_MIX_SBT1;
        }
    }
#endif
#endif

#ifdef NEURAL_QC
    // open model file (env override -> executable-adjacent -> ./model.bin -> /lib/model.bin)
    {
        const char *env_model = getenv("AVS3_MODEL_PATH");
        const char *candidates[4] = { env_model, "model.bin", "/lib/model.bin", NULL };
        *fModel = NULL;
        for (int ci = 0; candidates[ci] != NULL; ci++) {
            if (candidates[ci][0] && (*fModel = fopen(candidates[ci], "rb")) != NULL) {
                break;
            }
        }
        if (*fModel == NULL) {
            fprintf(stderr, "Can not open AVS3 encoder model file (set AVS3_MODEL_PATH or place model.bin next to vivid_encode).\n");
            exit(-1);
        }
    }

    // init neural qc handles
    stAvs3->modelType = HYPER;
    InitNeuralCodec(*fModel, &stAvs3->baseCodecSt, &stAvs3->contextCodecSt, stAvs3->modelType);
#endif

    // init core st handle to null
    for (ch = 0; ch < MAX_CHANNELS; ch++)
    {
        stAvs3->hEncCore[ch] = NULL;
    }

    // init encoder according to mode info
    if (stAvs3->avs3CodecFormat == AVS3_MONO_FORMAT)
    {
        Avs3EncCreateMono(stAvs3);
    }
    else if (stAvs3->avs3CodecFormat == AVS3_STEREO_FORMAT)
    {
        Avs3EncCreateStereo(stAvs3);
    }
    else if (stAvs3->avs3CodecFormat == AVS3_MC_FORMAT)
    {
#ifdef MC_ENABLE
        Avs3EncCreateMc(stAvs3);
#endif
    }
    else if (stAvs3->avs3CodecFormat == AVS3_HOA_FORMAT)
    {
        Avs3EncCreateHoa(stAvs3);
    }
#ifdef MIX_DEVELOPE
    else if (stAvs3->avs3CodecFormat == AVS3_MIX_FORMAT)
    {
        Avs3EncCreateMix(stAvs3);
    }
#endif
    else
    {
        // Todo:
    }

#ifdef METADATA_EXT
    // Init metadata handle
    Avs3EncCreateMetaData(stAvs3);
#endif

    return;
}

// destroy encoder struct
void Avs3EncoderDestroy(AVS3EncoderHandle stAvs3)
{
    short i;

    // free core encoder st
    for (i = 0; i < MAX_CHANNELS; i++)
    {
        if (stAvs3->hEncCore[i] != NULL)
        {
            if (stAvs3->hEncCore[i]->hCoreConfig != NULL)
            {
                free(stAvs3->hEncCore[i]->hCoreConfig);
                stAvs3->hEncCore[i]->hCoreConfig = NULL;
            }

            free(stAvs3->hEncCore[i]);
            stAvs3->hEncCore[i] = NULL;
        }
    }

    // free st for different modes
    if (stAvs3->avs3CodecFormat == AVS3_MONO_FORMAT)
    {
        if (stAvs3->hMono != NULL)
        {
            free(stAvs3->hMono);
            stAvs3->hMono = NULL;
        }
    }
    else if (stAvs3->avs3CodecFormat == AVS3_STEREO_FORMAT)
    {
        if (stAvs3->hMdctStereo->bsHandle != NULL)
        {
            free(stAvs3->hMdctStereo->bsHandle);
            stAvs3->hMdctStereo->bsHandle = NULL;
        }

        if (stAvs3->hMdctStereo != NULL)
        {
            free(stAvs3->hMdctStereo);
            stAvs3->hMdctStereo = NULL;
        }
    }
    else if (stAvs3->avs3CodecFormat == AVS3_HOA_FORMAT)
    {
        if (stAvs3->hEncHoa->hHoaConfig != NULL)
        {
            free(stAvs3->hEncHoa->hHoaConfig);
            stAvs3->hEncHoa->hHoaConfig = NULL;
        }

        if (stAvs3->hEncHoa->bsHandle != NULL) 
        {
            free(stAvs3->hEncHoa->bsHandle);
            stAvs3->hEncHoa->bsHandle = NULL;
        }

        if (stAvs3->hEncHoa != NULL)
        {
            free(stAvs3->hEncHoa);
            stAvs3->hEncHoa = NULL;
        }
    }
    else if (stAvs3->avs3CodecFormat == AVS3_MC_FORMAT) 
    {
        if (stAvs3->hMcEnc->bsHandle != NULL)
        {
            free(stAvs3->hMcEnc->bsHandle);
            stAvs3->hMcEnc->bsHandle = NULL;
        }

        if (stAvs3->hMcEnc != NULL)
        {
            free(stAvs3->hMcEnc);
            stAvs3->hMcEnc = NULL;
        }
    }
    else if (stAvs3->avs3CodecFormat == AVS3_MIX_FORMAT)
    {
        // same as MC mode
        if (stAvs3->hMcEnc->bsHandle != NULL)
        {
            free(stAvs3->hMcEnc->bsHandle);
            stAvs3->hMcEnc->bsHandle = NULL;
        }

        if (stAvs3->hMcEnc != NULL)
        {
            free(stAvs3->hMcEnc);
            stAvs3->hMcEnc = NULL;
        }
    }

#ifdef METADATA_EXT
    // metadata handle
    if (stAvs3->hAvs3MetaData->bsHandle != NULL) {
        free(stAvs3->hAvs3MetaData->bsHandle);
        stAvs3->hAvs3MetaData->bsHandle = NULL;
    }
    if (stAvs3->hAvs3MetaData != NULL) {
        free(stAvs3->hAvs3MetaData);
        stAvs3->hAvs3MetaData = NULL;
    }
#endif

#ifdef NEURAL_QC
    // free neural qc handle
    DestroyNeuralCodec(&stAvs3->baseCodecSt, &stAvs3->contextCodecSt);

    if (stAvs3->baseCodecSt != NULL)
    {
        free(stAvs3->baseCodecSt);
        stAvs3->baseCodecSt = NULL;
    }

    if (stAvs3->contextCodecSt != NULL)
    {
        free(stAvs3->contextCodecSt);
        stAvs3->contextCodecSt = NULL;
    }
#endif

    // free top level st
    if (stAvs3 != NULL)
    {
        free(stAvs3);
        stAvs3 = NULL;
    }
}
