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

#include "avs3_cnst_com.h"
#include "avs3_prot_com.h"
#include "avs3_prot_enc.h"

static void Avs3EncoderConfig(AVS3EncoderHandle stAvs3)
{
    if(stAvs3->avs3CodecFormat == AVS3_HOA_FORMAT)
    {
        stAvs3->numChansInput = stAvs3->hEncHoa->hHoaConfig->nTotalChansInput;
    }

    return;
}

// pre-analysis for core coder
// including transient detection and windowing/transform
void Avs3PreAnalysis(AVS3EncoderHandle stAvs3, const short nChans, const short LenFrame)
{
    short ch;
    AVS3_ENC_CORE_HANDLE hEncCore = NULL;

    /* Transient Detection */
    for (ch = 0; ch < nChans; ch++)
    {
        hEncCore = stAvs3->hEncCore[ch];
        hEncCore->transformType = WindowTypeDetect(&hEncCore->winTypeDetector, (hEncCore->lookahead - FRAME_LEN / 2),
            hEncCore->frameLength, stAvs3->initFrame);
    }

    /* Time Domain to Frequency Domain */
    CoreSignalAnalysis(stAvs3, nChans, LenFrame);

    /* Grouping mdct spectrum in short window frame */
    for (ch = 0; ch < nChans; ch++)
    {
        hEncCore = stAvs3->hEncCore[ch];
        if (hEncCore->transformType == ONLY_SHORT_WINDOW) 
        {
            MdctSpectrumInterleave(hEncCore->origSpectrum, hEncCore->frameLength, N_BLOCK_SHORT);
        }
    }

#ifdef IMPR_MIX_BIT_ALLOC
    /* Get silence flag for mc and mix mode */
    if (stAvs3->avs3CodecFormat == AVS3_MC_FORMAT && stAvs3->enableSilDetect == 1) {
        McMixGetSilenceFlag(stAvs3, nChans, LenFrame);
    }
#endif

#ifdef MC_LFE_PROC
    /* Clear HF mdct lines for LFE channel in MC mode */
#ifndef MIX_DEVELOPE
    if (stAvs3->avs3CodecFormat == AVS3_MC_FORMAT) {
#else
    if (stAvs3->avs3CodecFormat == AVS3_MC_FORMAT || 
        stAvs3->avs3CodecFormat == AVS3_MIX_FORMAT) {
#endif
        for (ch = 0; ch < nChans; ch++) {
            // has lfe and lfe is current channel
            if (stAvs3->hMcEnc->lfeExist && stAvs3->hMcEnc->lfeChIdx == ch) {
                hEncCore = stAvs3->hEncCore[ch];
                McLfeProc(hEncCore->origSpectrum);
            }
        }
    }
#endif

#ifdef FD_SHAPING
    /* LPC analysis and spectrum shaping */
    for (ch = 0; ch < nChans; ch++) {
        Avs3FdSpectrumShaping(stAvs3->hEncCore[ch], ch);
    }
#endif

#ifdef TD_SHAPING
    /* temporal noise shaping */
    for (ch = 0; ch < nChans; ch++) {

        hEncCore = stAvs3->hEncCore[ch];
        TnsEnc(&hEncCore->tnsData, hEncCore->origSpectrum, hEncCore->transformType == ONLY_SHORT_WINDOW);
    }
#endif

#ifdef BWE_DEVELOPE
    /* Encoder side bwe */
    for (ch = 0; ch < nChans; ch++) {

        hEncCore = stAvs3->hEncCore[ch];

        if (hEncCore->bwePresent == 1) {
            BweApplyEnc(&hEncCore->bweConfig, &hEncCore->bweEncData, hEncCore->origSpectrum, NULL,
                hEncCore->transformType == ONLY_LONG_WINDOW);
        }
    }
#endif

    /* Write core side bits into bitstream */
    /* Including window type, fd shaping, td shaping and BWE */
    WriteCoreSideBitstream(stAvs3, nChans, stAvs3->bitstream, &stAvs3->totalSideBits);
}


// QC function
void Avs3Qc(
    AVS3EncoderHandle stAvs3,
    short *target,
    const short nChans
)
{
    short ch;
    AVS3_ENC_CORE_HANDLE hEncCore = NULL;

    float featureIn[FRAME_LEN][2];          // 2D feature map for neural qc

#ifdef NEURAL_QC
    /* Neural QC process */
    for (ch = 0; ch < nChans; ch++) {

        hEncCore = stAvs3->hEncCore[ch];

        // copy feature to 2D feature map
        for (int16_t i = 0; i < FRAME_LEN; i++) {
            featureIn[i][0] = hEncCore->origSpectrum[i];
            featureIn[i][1] = 0.0f;
        }

        // get number of spectral lines for NF calculation
        int16_t numLinesNonZero = 0;
        if (hEncCore->bwePresent) {
            numLinesNonZero = hEncCore->bweConfig.bweStartLine;
        }
        else {
            numLinesNonZero = hEncCore->frameLength;
        }

        // Init neural QC data structure
        InitNeuralQcData(&hEncCore->neuralQcData);

        // MDCT QC process
#ifndef SUPPORT_NNTYPE_LC
        MdctQuantEncodeHyper(stAvs3->baseCodecSt, stAvs3->contextCodecSt, &hEncCore->neuralQcData,
            featureIn, hEncCore->frameLength, 1, numLinesNonZero, hEncCore->numGroups, hEncCore->groupIndicator, target[ch]);
#else
        if (stAvs3->nnTypeConfig == NN_TYPE_DEFAULT_MAIN) {
            MdctQuantEncodeHyper(stAvs3->baseCodecSt, stAvs3->contextCodecSt, &hEncCore->neuralQcData,
                featureIn, hEncCore->frameLength, 1, numLinesNonZero, hEncCore->numGroups, hEncCore->groupIndicator, target[ch]);
        }
        else if (stAvs3->nnTypeConfig == NN_TYPE_DEFAULT_LC) {
            MdctQuantEncodeHyperLc(stAvs3->baseCodecSt, stAvs3->contextCodecSt, &hEncCore->neuralQcData,
                featureIn, hEncCore->frameLength, 1, numLinesNonZero, hEncCore->numGroups, hEncCore->groupIndicator, target[ch]);
        }
#endif

        // post proc for nf param, only for low bitrate, harmonic or tone signals
        // only for short window
        if (hEncCore->transformType != ONLY_SHORT_WINDOW) {
            NfParamPostProc(&hEncCore->neuralQcData, hEncCore->origSpectrum, numLinesNonZero, stAvs3->totalBitrate, nChans);
        }
    }

    // write QC bitstream for all channels
    WriteQcBitstream(stAvs3, nChans, stAvs3->bitstream, &stAvs3->totalSideBits);
#endif

    return;
}


#ifdef DEBUG_NEURAL_QC_LOCAL_SYNTH
void Avs3LocalInverseQC(
    AVS3EncoderHandle stAvs3,
    const short nChans
)
{
    short ch;
    AVS3_ENC_CORE_HANDLE hEncCore = NULL;

    for (ch = 0; ch < nChans; ch++)
    {
        hEncCore = stAvs3->hEncCore[ch];

        // get number of spectral lines for NF calculation
        int16_t numLinesNoiseFill = 0;
        if (hEncCore->bwePresent) {
            numLinesNoiseFill = hEncCore->bweConfig.bweStartLine;
        }
        else {
            numLinesNoiseFill = hEncCore->frameLength;
        }

#ifdef NEURAL_QC
        float featureOut[FRAME_LEN][2];

#ifndef SUPPORT_NNTYPE_LC
        MdctDequantDecodeHyper(stAvs3->baseCodecSt, stAvs3->contextCodecSt, &hEncCore->neuralQcData,
            featureOut, numLinesNoiseFill, hEncCore->numGroups, hEncCore->groupIndicator);
#else
        if (stAvs3->nnTypeConfig == NN_TYPE_DEFAULT_MAIN) {
            MdctDequantDecodeHyper(stAvs3->baseCodecSt, stAvs3->contextCodecSt, &hEncCore->neuralQcData,
                featureOut, numLinesNoiseFill, hEncCore->numGroups, hEncCore->groupIndicator);
        }
        else if (stAvs3->nnTypeConfig == NN_TYPE_DEFAULT_LC) {
            MdctDequantDecodeHyperLc(stAvs3->baseCodecSt, stAvs3->contextCodecSt, &hEncCore->neuralQcData,
                featureOut, numLinesNoiseFill, hEncCore->numGroups, hEncCore->groupIndicator);

        }
#endif

        for (int16_t i = 0; i < FRAME_LEN; i++) {
            hEncCore->origSpectrum[i] = featureOut[i][0];
        }
#endif

        SpectrumDegroupingDec(hEncCore->origSpectrum, hEncCore->frameLength, hEncCore->transformType, hEncCore->groupIndicator);
    }

    return;
}
#endif


// frame level core coding function
void Avs3CoreEncode(AVS3EncoderHandle stAvs3, float data[MAX_CHANNELS][MAX_FRAME_LEN], const short lenFrame, const short nChans)
{
    short ch;
    const short offset = stAvs3->initFrame ? 0 : stAvs3->lookaheadSamples;
    const short delay = stAvs3->initFrame ? stAvs3->lookaheadSamples : 0;

#ifdef NEURAL_QC
    short target[MAX_CHANNELS] = { 0 };
#endif

#ifdef NEURAL_QC
    AVS3_ENC_CORE_HANDLE hEncCore = NULL;
#endif

#ifdef MCR_INTEGRATE
    int16_t nChansQc = nChans;              // number channels for QC
#endif

    // copy input data to core buffer
    for (ch = 0; ch < nChans; ch++)
    {
        Mvf2f(data[ch], stAvs3->hEncCore[ch]->inputSignal + offset, lenFrame + delay);
    }

    /* Transient Detection & Windowing Signal */
    Avs3PreAnalysis(stAvs3, nChans, lenFrame);

    if (stAvs3->avs3CodecFormat == AVS3_MONO_FORMAT)
    {
#ifdef MONO_INTEGRATE
        Avs3MonoEncoder(stAvs3, target);
#endif
    }
    else if (stAvs3->avs3CodecFormat == AVS3_STEREO_FORMAT)
    {
#ifndef MCR_INTEGRATE
        // stereo ms decision, downmix and bit split
        Avs3StereoEncoder(stAvs3, target);
#else
        if (stAvs3->hMdctStereo->useMcr == 0) {
            // stereo ms decision, downmix and bit split
            Avs3StereoEncoder(stAvs3, target);
        }
        else {
            // MCR stereo, number channels for QC is 1
            Avs3StereoMcrEncoder(stAvs3, target);
            nChansQc = 1;
        }
#endif
    }
    else if (stAvs3->avs3CodecFormat == AVS3_MC_FORMAT)
    {
#ifdef MC_ENABLE
        Avs3McEncoder(stAvs3, target);
#endif
    }
    else if (stAvs3->avs3CodecFormat == AVS3_HOA_FORMAT)
    {
        /* HOA down-mix and bits split */
        Avs3HoaCoreEncoder(stAvs3, target);
    }
#ifdef MIX_DEVELOPE
    else if (stAvs3->avs3CodecFormat == AVS3_MIX_FORMAT)
    {
        Avs3MixEncoder(stAvs3, target);
    }
#endif
    else
    {
        // Todo
    }

    /* Neural QC process */
#ifndef MCR_INTEGRATE
    Avs3Qc(stAvs3, target, nChans);
#else
    Avs3Qc(stAvs3, target, nChansQc);
#endif

#ifdef DEBUG_NEURAL_QC_LOCAL_SYNTH
    float localOutput[MAX_CHANNELS][BLOCK_LEN_LONG];

    /* Local decoder */
#ifndef MCR_INTEGRATE
    Avs3LocalInverseQC(stAvs3, nChans);
#else
    Avs3LocalInverseQC(stAvs3, nChansQc);
#endif

    // stereo mode inverse process
    if (stAvs3->avs3CodecFormat == AVS3_STEREO_FORMAT) {

        AVS3_STEREO_ENC_HANDLE hMdctStereo = stAvs3->hMdctStereo;
        AVS3_ENC_CORE_HANDLE hEncCoreL = stAvs3->hEncCore[0];
        AVS3_ENC_CORE_HANDLE hEncCoreR = stAvs3->hEncCore[1];

#ifndef MCR_INTEGRATE
        if (hMdctStereo->isMS == 1) {
            StereoInvMsProcess(hEncCoreL->origSpectrum, hEncCoreR->origSpectrum, stAvs3->frameLength, hMdctStereo->ILD);
        }
#else
        if (hMdctStereo->useMcr == 0) {
            if (hMdctStereo->isMS == 1) {
                StereoInvMsProcess(hEncCoreL->origSpectrum, hEncCoreR->origSpectrum, stAvs3->frameLength, hMdctStereo->ILD);
            }
        }
        else {
            McrDecode(&hMdctStereo->mcrData, &hMdctStereo->mcrConfig, hEncCoreL->origSpectrum, hEncCoreR->origSpectrum,
                hEncCoreL->transformType == ONLY_SHORT_WINDOW);
        }
#endif
    }

#ifdef MC_ENABLE
#ifndef MIX_DEVELOPE
    if (stAvs3->avs3CodecFormat == AVS3_MC_FORMAT)
#else
    if (stAvs3->avs3CodecFormat == AVS3_MC_FORMAT ||
        stAvs3->avs3CodecFormat == AVS3_MIX_FORMAT)
#endif
    {
        int pairCnt;
        int k, ii;
        AVS3_MC_PAIR_DATA_HANDLE hPair;

        for (k = 0; k < nChans; k++)
        {
            stAvs3->hMcDec->mcSpectrum[k] = stAvs3->hMcEnc->mcSpectrum[k];
        }

        stAvs3->hMcDec->channelNum = stAvs3->hMcEnc->channelNum;
        stAvs3->hMcDec->coupleChNum = stAvs3->hMcEnc->coupleChNum;
        stAvs3->hMcDec->lfeChIdx = stAvs3->hMcEnc->lfeChIdx;
        stAvs3->hMcDec->lfeExist = stAvs3->hMcEnc->lfeExist;
        stAvs3->hMcDec->lfeBytes = stAvs3->hMcEnc->lfeBytes;
        stAvs3->hMcDec->pairCnt = stAvs3->hMcEnc->pairCnt;
        stAvs3->hMcDec->bitsPairIndex = stAvs3->hMcEnc->bitsPairIndex;

        for (ii = 0; ii < MAX_CHANNELS; ii++)
        {
#ifndef MC_ILD_CBQUANT
            stAvs3->hMcDec->mcIld[ii] = 0;
            stAvs3->hMcDec->scaleFlag[ii] = 0;
#else
            stAvs3->hMcDec->mcIld[ii] = MC_ILD_CBLEN;
#endif
        }

        for (pairCnt = stAvs3->hMcDec->pairCnt - 1; pairCnt >= 0; pairCnt--) {

            hPair = &(stAvs3->hMcEnc->hPair[pairCnt]);

            stAvs3->hMcDec->hPair[pairCnt].ch1 = hPair->ch1;
            stAvs3->hMcDec->hPair[pairCnt].ch2 = hPair->ch2;

            stAvs3->hMcDec->mcIld[hPair->ch1] = stAvs3->hMcEnc->mcIld[hPair->ch1];
            stAvs3->hMcDec->mcIld[hPair->ch2] = stAvs3->hMcEnc->mcIld[hPair->ch2];

#ifndef MC_ILD_CBQUANT
            stAvs3->hMcDec->scaleFlag[hPair->ch1] = stAvs3->hMcEnc->scaleFlag[hPair->ch1];
            stAvs3->hMcDec->scaleFlag[hPair->ch2] = stAvs3->hMcEnc->scaleFlag[hPair->ch2];
#endif
        }

        Avs3McacDec(stAvs3->hMcDec);
    }
#endif

    for (ch = 0; ch < nChans; ch++)
    {
        hEncCore = stAvs3->hEncCore[ch];

#ifdef BWE_DEVELOPE
        if (hEncCore->bwePresent == 1) {
            // copy data from encoder side
            for (int16_t i = 0; i < hEncCore->bweConfig.numSfb; i++) {
                hEncCore->bweDecData.sfbEnvQIdx[i] = hEncCore->bweEncData.sfbEnvQIdx[i];
            }
            for (int16_t i = 0; i < hEncCore->bweConfig.numTiles; i++) {
                hEncCore->bweDecData.whiteningLevel[i] = hEncCore->bweEncData.whiteningLevel[i];
            }

            BweApplyDec(&hEncCore->bweConfig, &hEncCore->bweDecData, hEncCore->origSpectrum);
        }
#endif

#ifdef TD_SHAPING
        TnsDec(&hEncCore->tnsData, hEncCore->origSpectrum, hEncCore->transformType == ONLY_SHORT_WINDOW);
#endif

#ifdef FD_SHAPING
        Avs3FdInvSpectrumShaping(hEncCore->lsfVqIndex, hEncCore->origSpectrum, hEncCore->lsfLbrFlag);

#ifdef POST_SHAPING
        // post processing the shaped spectrum, in low bitrate
        if (hEncCore->lsfLbrFlag == 1 && hEncCore->transformType != ONLY_SHORT_WINDOW) {
            SpecPostShaping(hEncCore->origSpectrum, hEncCore->bweConfig.bweStartLine, 1);
        }
#endif
#endif

#ifdef MC_LFE_PROC
        /* Clear HF mdct lines for LFE channel in MC mode */
#ifndef MIX_DEVELOPE
        if (stAvs3->avs3CodecFormat == AVS3_MC_FORMAT) {
#else
        if (stAvs3->avs3CodecFormat == AVS3_MC_FORMAT || 
            stAvs3->avs3CodecFormat == AVS3_MIX_FORMAT) {
#endif
            // has lfe and lfe is current channel
            if (stAvs3->hMcEnc->lfeExist && stAvs3->hMcEnc->lfeChIdx == ch) {
                hEncCore = stAvs3->hEncCore[ch];
                McLfeProc(hEncCore->origSpectrum);
            }
        }
#endif

        if (hEncCore->transformType == ONLY_SHORT_WINDOW)
        {
            MdctSpectrumDeinterleave(hEncCore->origSpectrum, hEncCore->frameLength, N_BLOCK_SHORT);
        }

        Avs3LocalDecoder(hEncCore, localOutput[ch]);
    }

#ifdef SIMULATING_HOA_DECODER
    /* HOA post synthesis */
    if (stAvs3->avs3CodecFormat == AVS3_HOA_FORMAT)
    {
        for (ch = 0; ch < nChans; ch++)
        {
            Mvf2f(localOutput[ch], stAvs3->hEncHoa->decSignalInput[ch], lenFrame);
        }

        /* Synthesis HOA signal */
        LocalHoaPostSynthesisFilter(stAvs3->hEncHoa, lenFrame);

        for (ch = 0; ch < stAvs3->hEncHoa->hHoaConfig->nTotalChansInput; ch++)
        {
            Mvf2f(stAvs3->hEncHoa->decHoaDelayBuffer[ch] + lenFrame, stAvs3->hEncHoa->decHoaDelayBuffer[ch], lenFrame);
        }
    }
#endif

    if (!stAvs3->initFrame)
    {
        short* out_data = (short*)malloc(sizeof(short)*BLOCK_LEN_LONG*nChans);
        Avs3SynthOutput(localOutput, BLOCK_LEN_LONG, nChans, out_data);
        dbgwrite(out_data, sizeof(short)*nChans, BLOCK_LEN_LONG, 1, "local_synthesis_core_encoder.pcm");
        if (out_data) free(out_data);
    }
#endif

    /* Update input signal */
    for (ch = 0; ch < nChans; ch++)
    {
        Mvf2f(stAvs3->hEncCore[ch]->inputSignal, stAvs3->hEncCore[ch]->signalBuffer, lenFrame);
        Mvf2f(stAvs3->hEncCore[ch]->lookahead, stAvs3->hEncCore[ch]->inputSignal, stAvs3->hEncCore[ch]->lookaheadSamples);

        stAvs3->hEncCore[ch]->lastTransformType = stAvs3->hEncCore[ch]->transformType;
    }

    return;
}


// top level frame level encoding function
void Avs3Encode(AVS3EncoderHandle stAvs3, const short *data, const short samples)
{
    short i, ch;
    short nChans;
    short nSamplesPerChan;
    short inputFrameLength;
    float dataFloat[MAX_CHANNELS][MAX_FRAME_LEN];

    Avs3EncoderConfig(stAvs3);

    nChans = stAvs3->numChansInput;
    nSamplesPerChan = samples / nChans;
    inputFrameLength = stAvs3->frameLength;

    // short to float
    for (ch = 0; ch < nChans; ch++)
    {
        for (i = 0; i < nSamplesPerChan; i++)
        {
            dataFloat[ch][i] = (float)data[i*nChans + ch];
        }
    }
    // padding zeros if not sufficient data
    if (nSamplesPerChan < inputFrameLength)
    {
        for (ch = 0; ch < nChans; ch++)
        {
            SetZero(dataFloat[ch] + nSamplesPerChan, inputFrameLength - nSamplesPerChan);
        }
    }

    // HOA processing, before core coding
    if (stAvs3->avs3CodecFormat == AVS3_HOA_FORMAT)
    {
        Avs3HOAEncoder(stAvs3, dataFloat, inputFrameLength);

        Avs3HOAReconfig(stAvs3, &nChans);
    }

    // core coding
    Avs3CoreEncode(stAvs3, dataFloat, inputFrameLength, nChans);

    return;
}