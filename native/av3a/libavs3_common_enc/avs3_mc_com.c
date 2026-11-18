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
#include <stdint.h>
#include <assert.h>
#include <math.h>

#include "avs3_options.h"
#include "avs3_cnst_com.h"
#include "avs3_prot_com.h"

#ifdef MC_ILD_CBQUANT
#include "avs3_rom_com.h"
#endif

#ifdef MC_ENABLE

/*
Get the index of each channel pair
I/O params:
    AVS3_MC_PAIR_DATA_HANDLE hPair
    const short index
    const short channelNum
*/
void Index2PairMapping(
    AVS3_MC_PAIR_DATA_HANDLE hPair,
    const short index,
    const short channelNum
)
{
    short chIdx1, chIdx2;
    short tmp = 0;

    for (chIdx1 = 0; chIdx1 < channelNum - 1; chIdx1++) {

        for (chIdx2 = chIdx1 + 1; chIdx2 < channelNum; chIdx2++) {

            if (tmp == index) {

                hPair->ch1 = chIdx1;
                hPair->ch2 = chIdx2;

                return;
            }
            else {
                tmp++;
            }
        }
    }

    return;
}

/*
Revert to initial channel energy levels using the ratios sent from the encoder
I/O params:
    AVS3_MC_DEC_HANDLE hMcac
    float *spec[MAX_CHANNELS]
*/
static void InverseEnergyBalance(
    AVS3_MC_DEC_HANDLE hMcac,
    float *spec[MAX_CHANNELS]
)
{
    float factor;
    short chIdx;

    for (chIdx = 0; chIdx < hMcac->coupleChNum; chIdx++) {
#ifndef MC_ILD_CBQUANT
        if (hMcac->mcIld[chIdx]) {
            if (hMcac->scaleFlag[chIdx]) {
                factor = (float)hMcac->mcIld[chIdx] / ENERGY_BALENCE_SCOPE;
            }
            else {
                factor = ENERGY_BALENCE_SCOPE / (float)hMcac->mcIld[chIdx];
            }
#else
        if (hMcac->mcIld[chIdx] != MC_ILD_CBLEN) {
            factor = mcIldCodebook[hMcac->mcIld[chIdx]];
#endif
            VMultC(spec[chIdx], factor, spec[chIdx], FRAME_LEN);
        }
        else {
            continue;
        }
    }

    return;
}

/*
MS upmix procedure
I/O params:
    float spec0[]                  (i/o) mid/left channel coefficients
    float spec1[]                  (i/o) side/right channel coefficients
    const short len                (i  ) spectrum line number
*/
static void MsUpmix(
    float spec0[],
    float spec1[],
    const short len
)
{
    float factor = (float)sqrt(2) / 2;
    short specLineIdx;
    float tmpData;

    for (specLineIdx = 0; specLineIdx < len; specLineIdx++) {

        tmpData = spec0[specLineIdx];
        spec0[specLineIdx] = (spec0[specLineIdx] + spec1[specLineIdx]) * factor;
        spec1[specLineIdx] = (tmpData - spec1[specLineIdx]) * factor;
    }

    return;
}

/*
Apply mcac decoding process
I/O params:
    AVS3_MC_DEC_HANDLE hMcac                (i/o) MCAC decoder structure
*/
void Avs3McacDec(
    AVS3_MC_DEC_HANDLE hMcac
)
{
    short pair;
    AVS3_MC_PAIR_DATA_HANDLE hPair;

    for (pair = hMcac->pairCnt - 1; pair >= 0; pair--) {

        hPair = &(hMcac->hPair[pair]);
        MsUpmix(hMcac->mcSpectrum[hPair->ch1], hMcac->mcSpectrum[hPair->ch2], FRAME_LEN);
    }

    InverseEnergyBalance(hMcac, hMcac->mcSpectrum);

    return;

}

/*
Get energies of each channel
I/O params:
    float *spectrum[MAX_CHANNELS]       (i) channel spectrum
    float energy[MAX_CHANNELS]          (o) energy of each channel
    const short nchan                   (i) channel num
*/
void CalcChannelEnergies(
    float *spectrum[MAX_CHANNELS],
    float energy[MAX_CHANNELS],
    const short nchan
)
{
    short ch;

    for (ch = 0; ch < nchan; ch++) {

        energy[ch] = Dotp(spectrum[ch], spectrum[ch], FRAME_LEN);
        energy[ch] = (float)sqrt(energy[ch]);
    }

    return;
}

/*
Get bit ratios for each channel
I/O params:
    short channelNum                        (i) channel number
    float *mdctSpectrum[MAX_CHANNELS]       (i) mdct spectrum for each channel
    short chBitRatios[MAX_CHANNELS]         (o) bit ratio for each channel
    const int totalBrate                    (i) total bit rate
    const int reAllocEna                    (i) to limit max bit enable
*/
void GetChRatio(
    short channelNum,
    float *mdctSpectrum[MAX_CHANNELS],
    short chBitRatios[MAX_CHANNELS],
    const int totalBrate,                   /* i  : total bit rate          */
    const int reAllocEna                    /* i  : to limit max bit enable */
)
{
    float sumEnergy;
    float chRatio;
    short chIdx;
    float energy[MAX_CHANNELS + 1];
    short tmpFactor[MAX_CHANNELS];

    CalcChannelEnergies(mdctSpectrum, energy, channelNum);

    sumEnergy = 1.0f / max(SumFloat(energy, channelNum), AVS3_EPSILON);

    /* calculate ratio for each active channel */
    for (chIdx = 0; chIdx < channelNum; chIdx++) {

        chRatio = energy[chIdx] * sumEnergy;

        /* limit the chIdx rate below the threhold of BIT_FRAME_MAX */
        if (reAllocEna && (chRatio * totalBrate > BIT_FRAME_MAX)) {
            chRatio = (float)BIT_FRAME_MAX / totalBrate;
        }

        tmpFactor[chIdx] = min(BITRATE_MC_RATIO_SCOPE - 1, max(1, (unsigned short)(BITRATE_MC_RATIO_SCOPE * chRatio + 0.5f)));

    }

    MvShort2Short(tmpFactor, chBitRatios, channelNum);

    return;
}


#ifdef IMPR_MIX_BIT_ALLOC
/*
Bits allocation with silence function
    short totalBits                             (i) total number of bits
    const short splitRatio[MAX_CHANNELS]        (i) split ratio of bits
    short channelNum                            (i) number of channels
    short* channelBytes                         (o) allocated number of bytes for each channel
    short silFlag[MAX_CHANNELS]                 (i) the channels silence process flags
    short lfeExist                              (i) if the LFE channel exist
    short lfeBytes                              (i) LFE channel bytes, already allocated
*/
int32_t McBitsAllocationHasSiL(
    int32_t totalBits,
    const short splitRatio[MAX_CHANNELS],
    short channelNum,
    short* channelBytes,
    short silFlag[MAX_CHANNELS],
    short lfeExist,
    short lfeBytes
)
{
    short i, j;
    short mappingChannelBytes[MAX_CHANNELS];
    short mappingSplitRatio[MAX_CHANNELS] = { 0 };
    short NoSilNumStatic;

    NoSilNumStatic = 0;
    for (i = 0; i < channelNum; i++) {
        if (silFlag[i] == 0) {
            NoSilNumStatic++;
        }
    }

    totalBits -= (channelNum - NoSilNumStatic) * SILENCE_BYTES * AVS3_BS_BYTE_SIZE;

    /* main function */
    McBitsAllocation(totalBits, splitRatio, NoSilNumStatic, mappingChannelBytes, lfeExist, lfeBytes);

    /* Reproduce channels to the former */
    j = 0;
    for (i = 0; i < channelNum; i++) {
        if (lfeExist) {
            if (i == LFE_CHANNEL_INDEX) {
                channelBytes[LFE_CHANNEL_INDEX] = lfeBytes;
                continue;
            }
        }

        if (silFlag[i] == 1) {
            channelBytes[i] = SILENCE_BYTES;
        }
        else {
            if (lfeExist && (j == LFE_CHANNEL_INDEX)) {
                j++;
            }
            channelBytes[i] = mappingChannelBytes[j];
            j++;
        }
    }

    return 0;
}
#endif


/*
Bits allocation for MCAC
    short totalBits                             (i) total number of bits
    const short splitRatio[MAX_CHANNELS]        (i) split ratio of bits
    short channelNum                            (i) number of channels
    short* channelBytes                         (o) allocated number of bytes for each channel
    short lfeBytes                              (i) LFE channel bytes, already allocated
*/
void McBitsAllocation(
#ifndef SUPPORT_HIGH_BR_MIX
    short totalBits,
#else
    int32_t totalBits,
#endif
    const short splitRatio[MAX_CHANNELS],
    short channelNum,
    short* channelBytes,
    short lfeExist, 
    short lfeBytes
)
{
    short i, diffBits, bitsAllocated, mostBitsCh, tmp, chNum;
    short leftBits;
    short safeBits[MAX_CHANNELS] = { 0 };
#ifdef MC_ALLOC_BITS_LIMIT
    short reAllocFlag = 0;
    short j, ratioSum;
    short chUsedFlag[MAX_CHANNELS] = { 0 };
#endif

    if (lfeExist) {
        chNum = channelNum - 1;
        totalBits = totalBits / AVS3_BS_BYTE_SIZE - lfeBytes;
    }
    else {
        chNum = channelNum;
        totalBits = totalBits / AVS3_BS_BYTE_SIZE;
    }

    leftBits = totalBits;
#ifdef IMPR_MIX_BIT_ALLOC
    SetShort(safeBits, SILENCE_BYTES, chNum);
    for (i = 0; i < chNum; i++) {
        leftBits -= safeBits[i];
    }
#endif

    /* initial value of bits already given */
    bitsAllocated = 0;

    tmp = 0;
    mostBitsCh = 0;
#ifdef MC_ALLOC_BITS_LIMIT
    ratioSum = 0;
#endif
    for (i = 0; i < chNum; i++) {
        channelBytes[i] = splitRatio[i] * leftBits / BITRATE_MC_RATIO_SCOPE + safeBits[i];
        bitsAllocated += channelBytes[i];

        /* determine channel with most bits (energy) */
        if (channelBytes[i] > tmp) {
            tmp = channelBytes[i];
            mostBitsCh = i;
        }
#ifdef MC_ALLOC_BITS_LIMIT
        ratioSum += splitRatio[i];
#endif
    }

    /* if bits distributed are more than available bits, substract the proportional amount of bits from all channels */
    if (bitsAllocated != totalBits) {
        diffBits = bitsAllocated - totalBits;
        bitsAllocated = 0;

        for (i = 0; i < chNum; i++) {
            channelBytes[i] -= diffBits * splitRatio[i] / BITRATE_MC_RATIO_SCOPE;
            channelBytes[i] = max(safeBits[i], channelBytes[i]);
            bitsAllocated += channelBytes[i];
        }
    }

    /* if there any bits left assign them to the channel with the maximum energy (or more bits) */
    if (totalBits != bitsAllocated) {
        channelBytes[mostBitsCh] += (totalBits - bitsAllocated);
    }

#ifdef MC_ALLOC_BITS_LIMIT
    /* bits limit */
    for (j = 0; j < chNum; j++) {
        reAllocFlag = 0;
        for (i = 0; i < chNum; i++) {

            if ((channelBytes[i] > BIT_FRAME_MAX / 8) && chUsedFlag[i] == 0) {

                reAllocFlag = 1;

                leftBits = channelBytes[i] - BIT_FRAME_MAX / 8;
                channelBytes[i] = BIT_FRAME_MAX / 8;

                chUsedFlag[i] = 1;
                ratioSum -= splitRatio[i];

                break;
            }
        }

        if (reAllocFlag) {
            bitsAllocated = 0;
            for (i = 0; i < chNum; i++) {

                if (chUsedFlag[i] == 1) {
                    continue;
                }

                tmp = splitRatio[i] * leftBits / ratioSum;

                bitsAllocated += tmp;
                channelBytes[i] += tmp;
            }

            if (bitsAllocated != leftBits) {
                channelBytes[mostBitsCh] += (leftBits - bitsAllocated);
            }
        }
        else {
            break;
        }
    }
#endif

    /* channelBytes mapping of channel order */
    if (lfeExist) {
        for (i = channelNum - 1; i > LFE_CHANNEL_INDEX; i--)
        {
            channelBytes[i] = channelBytes[i - 1];
        }
        channelBytes[LFE_CHANNEL_INDEX] = lfeBytes;
    }

    return;
}


short GetLfeAllocBytes(
    const int32_t totalBitrate,
    const int16_t coupleChannels)
{
    short lfeBytes = 10;
    int32_t cpeRate;

    cpeRate = totalBitrate * 2 / coupleChannels;
    /* alloc LFE QC bytes perframe depending on the cpe rate */
    if (cpeRate < 64000) {
        lfeBytes = 10; //3.75kbps
    }
    else if (cpeRate < 96000) {
        lfeBytes = 15; //5.625kbps
    }
    else {
        lfeBytes = 20; //7.5kbps
    }

    return lfeBytes;
}

#endif


#ifdef MC_LFE_PROC
/*
Clear HF mdct lines of LFE channel
I/O params:
    float *mdctSpectrum             (i/o) mdct spectrum of lfe channel
*/
void McLfeProc(
    float *mdctSpectrum
)
{
    int i;

    for (i = LFE_RESERVED_LINES; i < BLOCK_LEN_LONG; i++) {
        mdctSpectrum[i] = 0.0f;
    }

    return;
}

#endif

