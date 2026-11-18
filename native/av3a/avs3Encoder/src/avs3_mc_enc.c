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
#include <float.h>

#include "avs3_options.h"
#include "avs3_cnst_com.h"
#include "avs3_stat_enc.h"
#include "avs3_prot_com.h"
#include "avs3_prot_enc.h"

#ifdef MC_ILD_CBQUANT
#include "avs3_rom_com.h"
#endif

#ifdef MC_ENABLE

#ifndef ONLY_PAIR_ILD
/*
Get global ILD
I/O params:
    AVS3_MC_ENC_HANDLE hMcacEnc             (i)   MCAC encoder handle
    float *spectrum[MAX_CHANNELS]           (i/o) MDCT spectrum
    float energy[MAX_CHANNELS]                 (i)   buffer with energies for each channel, MCAC used
    const short chNum                       (i)   number of channels
*/
static void EnergyBalanceMode1(
    AVS3_MC_ENC_HANDLE hMcacEnc,
    float *spectrum[MAX_CHANNELS],
    float energy[MAX_CHANNELS],
    const short chNum
)
{
    short chIdx;
    float avgEnergy, factor, factorUsed;

    /*Initializations*/
    SetShort(hMcacEnc->scaleFlag, 0, hMcacEnc->coupleChNum);

    /*calculate mean energy*/
    avgEnergy = max(Mean(energy, hMcacEnc->coupleChNum), AVS3_EPSILON);

    for (chIdx = 0; chIdx < hMcacEnc->coupleChNum; chIdx++) {

        /*change it to avgEnergy if final solution*/
        if (energy[chIdx] > avgEnergy) {

            factor = avgEnergy / energy[chIdx];
            hMcacEnc->mcIld[chIdx] = max(1, min(ENERGY_BALENCE_SCOPE - 1, (short)(ENERGY_BALENCE_SCOPE * factor + 0.5f)));
            factorUsed = (float)hMcacEnc->mcIld[chIdx] / ENERGY_BALENCE_SCOPE;
        }
        else {

            factor = energy[chIdx] / avgEnergy;
            hMcacEnc->scaleFlag[chIdx] = 1;
            hMcacEnc->mcIld[chIdx] = max(1, min(ENERGY_BALENCE_SCOPE - 1, (short)(ENERGY_BALENCE_SCOPE * factor + 0.5f)));
            factorUsed = (float)ENERGY_BALENCE_SCOPE / hMcacEnc->mcIld[chIdx];
        }

        VMultC(spectrum[chIdx], factorUsed, spectrum[chIdx], FRAME_LEN);
    }

    return;
}
#endif


/*
Get correlation matrix of all channels
I/O params:
    AVS3_MC_ENC_HANDLE hMcacEnc                         (i) MCAC handle
    float *spectrum[MAX_CHANNELS]                       (i) MDCT spectrum
    float crossCorrelationMat[MAX_CHANNELS][MAX_CHANNELS]       (o) cross-correlation matrix
*/
static void GetCorrelationMatrix(
    AVS3_MC_ENC_HANDLE hMcacEnc,
    float *spectrum[MAX_CHANNELS],
    float energy[MAX_CHANNELS],
    float crossCorrelationMat[MAX_CHANNELS][MAX_CHANNELS]
)
{
    short ch1, ch2;
    float tmp;

    /* correlation: */
    for (ch1 = 0; ch1 < hMcacEnc->coupleChNum; ch1++) {

        for (ch2 = ch1; ch2 < hMcacEnc->coupleChNum; ch2++) {

            crossCorrelationMat[ch1][ch2] = 0;

            if (*(hMcacEnc->frameType[ch1]) == *(hMcacEnc->frameType[ch2])) {
                // if energy factor between channels exceeds threshold ,set crossCorrelation value to zero
                if ((energy[ch1] / energy[ch2] > 2) || (energy[ch1] / energy[ch2] < 0.5f)) {
                    continue;
                }
                crossCorrelationMat[ch1][ch2] = Dotp(spectrum[ch1], spectrum[ch2], FRAME_LEN);
            }
        }
    }

    /* normalize */
    for (ch1 = 0; ch1 < hMcacEnc->coupleChNum; ch1++) {

        for (ch2 = ch1 + 1; ch2 < hMcacEnc->coupleChNum; ch2++) {

            tmp = (float)sqrt(crossCorrelationMat[ch1][ch1] * crossCorrelationMat[ch2][ch2]);

            if (tmp > 0.f) {
                crossCorrelationMat[ch1][ch2] /= tmp;
            }
            else {
                crossCorrelationMat[ch1][ch2] = 0.f;
            }

        }
    }

    return;
}


/*
Searches for the best correlated channel pair
I/O params:
    AVS3_MC_ENC_HANDLE hMcacEnc                     (i) MCAC handle
    short *channel1                                     (o) first channel of selected channel-pair
    short *channel2                                     (o) second channel of selected channel-pair
    float *maxCorrVal                                 (o) normalized cross correlation value of selected channel pair
    float corrMatrix[MAX_CHANNELS][MAX_CHANNELS]   (i) cross-correlation matrix
*/
static void SearchMaxCorrValue(
    AVS3_MC_ENC_HANDLE hMcacEnc,
    short *channel1,
    short *channel2,
    float *maxCorrVal,
    float crossCorrelationMat[MAX_CHANNELS][MAX_CHANNELS]
)
{
    short chIdx1, chIdx2;

    *channel1 = -1;
    *channel2 = -1;
    *maxCorrVal = 0.f;

    for (chIdx1 = 0; chIdx1 < hMcacEnc->coupleChNum; chIdx1++)
    {
        for (chIdx2 = chIdx1 + 1; chIdx2 < hMcacEnc->coupleChNum; chIdx2++)
        {
            if ((float)fabs(*maxCorrVal) < (float)fabs(crossCorrelationMat[chIdx1][chIdx2]))
            {
                *maxCorrVal = crossCorrelationMat[chIdx1][chIdx2];

                *channel1 = chIdx1;
                *channel2 = chIdx2;
            }
        }
    }

    return;
}


/*
MS down mix process
I/O params:
    float *spectrum[MAX_CHANNELS]       (i) MDCT spectrum
    const short channel1                     (i) first channel of channel-pair
    const short channel2                     (i) second channel of channel-pair
*/
static void MsDownmix(
    float *spectrum[MAX_CHANNELS],
    const short ch1,
    const short ch2
)
{
    short specIdx;
    float tmp;
    float factor = (float)sqrt(2) / 2;

    for (specIdx = 0; specIdx < FRAME_LEN; specIdx++)
    {
        tmp = spectrum[ch1][specIdx];
        spectrum[ch1][specIdx] = (spectrum[ch1][specIdx] + spectrum[ch2][specIdx]) * factor;
        spectrum[ch2][specIdx] = (tmp - spectrum[ch2][specIdx]) * factor;
    }

    return;
}


/*
Refresh the cross correlation matrix with modified spectra after stereo box processing
I/O params:
    AVS3_MC_ENC_HANDLE hMcacEnc
    const short channel1
    const short channel2
    float corrMatrix[MAX_CHANNELS][MAX_CHANNELS]
*/
static void RefreshCrossCorrelationMatrix(
    AVS3_MC_ENC_HANDLE hMcacEnc,
    const short channel1,
    const short channel2,
    float corrMatrix[MAX_CHANNELS][MAX_CHANNELS]
)
{
    short chIdx1, chIdx2;

    for (chIdx1 = 0; chIdx1 < hMcacEnc->coupleChNum; chIdx1++) {

        for (chIdx2 = chIdx1; chIdx2 < hMcacEnc->coupleChNum; chIdx2++) {

            if (*(hMcacEnc->frameType[chIdx1]) == *(hMcacEnc->frameType[chIdx2])) {

                if (chIdx1 == channel1 || chIdx2 == channel2 || chIdx1 == channel2 || chIdx2 == channel1) {
                    corrMatrix[chIdx1][chIdx2] = 0;
                }
            }
        }
    }

    return;
}

/*
Get the index of each channel pair
I/O params:
    const short chIdx1
    const short chIdx2
    const short channelNum
*/
short Pair2IndexMapping(
    const short ch1,
    const short ch2,
    const short channelNum
)
{
    short index;
    short channel1, channel2;

    index = 0;

    for (channel1 = 0; channel1 < channelNum - 1; channel1++) {

        for (channel2 = channel1 + 1; channel2 < channelNum; channel2++) {

            if ((channel1 == ch1) && (channel2 == ch2)) {
                return index;
            }
            else {
                index++;
            }
        }
    }

    return -1;
}

#ifndef ONLY_PAIR_ILD
/*
Get available chs's energies depending on the pairInfo
I/O params:
    const char pair[MAX_CHANNELS/2][2]          (i) pair info
    float energyIn[MAX_CHANNELS]                (i) active energies input
    float energyOut[MAX_CHANNELS]               (o) energies output
    const short activeChans                     (i) number of actived channels
*/
short GetAvailableChsEnergies(
    const char pair[MAX_CHANNELS / 2][2],
    float energyIn[MAX_CHANNELS],
    float energyOut[MAX_CHANNELS],
    const short activeChans
)
{
    unsigned char i;
    int num;

    num = 0;
#ifndef BUGFIX_MC_16CH
    for (i = 0; i <= activeChans / 2; i++) {
#else
    for (i = 0; i < activeChans / 2; i++) {
#endif

        /* all pair is included  */
        if (pair[i][0] == -1) {
            break;
        }
        else {
            /* put the pair chIdx indexes into energyOuts  */
            assert(pair[i][0] < MAX_CHANNELS);
            assert(pair[i][1] < MAX_CHANNELS);

            energyOut[num++] = energyIn[pair[i][0]];
            energyOut[num++] = energyIn[pair[i][1]];
        }
    }

    /* return the paired channels' num  */
    return num;
}
#endif


#ifdef MC_ILD_CBQUANT
/*
Mc mode ild param scalar quantization
*/
static uint16_t McIldQuantization(
    float ild,
    float *ildQ)
{
    float dist;
    float distMin = FLT_MAX;
    uint16_t idx = 0;

    int16_t range;
    int16_t idxTmp;
    float ildTmp;

    range = 1 << (MC_EB_BITS - 1);

    if (ild <= 1.0f) {
        idxTmp = AVS3_MIN(AVS3_MAX((int16_t)(ild * range + 0.5f), 1), range - 1);
        ildTmp = (float)idxTmp / range;
    }
    else {
        idxTmp = AVS3_MIN(AVS3_MAX((int16_t)(1.0f / ild * range + 0.5f), 1), range - 1);
        ildTmp = (float)range / idxTmp;
    }

    for (int16_t i = 0; i < MC_ILD_CBLEN; i++) {
        dist = ildTmp - mcIldCodebook[i];
        dist *= dist;

        if (dist < distMin) {
            distMin = dist;
            idx = i;
        }
    }

    *ildQ = mcInvIldCodebook[idx];

    return idx;
}
#endif


/*
Get Pair ILD between channel1 and channel2
I/O params:
    AVS3_MC_ENC_HANDLE hMcacEnc             (i)   MCAC handle
    float *spectrum[MAX_CHANNELS]           (i/o) MDCT spectrum
    const short chNum                       (i)   number of channels
    const short channel1                         (i)   first channel of channel-pair
    const short channel2                         (i)   second channel of channel-pair
*/
static void EnergyBalanceMode2(
    AVS3_MC_ENC_HANDLE hMcacEnc,
    float *spectrum[MAX_CHANNELS],
    const short nchan,
    const short ch1,
    const short ch2
)
{
    short ch;
    float nrg[MAX_CHANNELS];
    float meanE, ratio, qratio;
    float tmp_nrg[MAX_CHANNELS];
    short map_ch[2];
    short cnt;

    // Get channel energies
    CalcChannelEnergies(spectrum, nrg, nchan);

    for (ch = 0; ch < hMcacEnc->coupleChNum; ch++) {
        if (ch == ch1) {
            tmp_nrg[0] = nrg[ch];
            map_ch[0] = ch1;
        }

        if (ch == ch2) {
            tmp_nrg[1] = nrg[ch];
            map_ch[1] = ch2;
        }
    }

    /*calculate Mean energy*/
    meanE = max(Mean(tmp_nrg, 2), AVS3_EPSILON);

    for (cnt = 0; cnt < 2; cnt++) {

        ch = map_ch[cnt];

#ifndef MC_ILD_CBQUANT
        /*change it to avgEnergy if final solution*/
        if (tmp_nrg[cnt] > meanE) {
            ratio = meanE / tmp_nrg[cnt];
            hMcacEnc->mcIld[ch] = max(1, min(ENERGY_BALENCE_SCOPE - 1, (short)(ENERGY_BALENCE_SCOPE * ratio + 0.5f)));
            qratio = (float)hMcacEnc->mcIld[ch] / ENERGY_BALENCE_SCOPE;
            hMcacEnc->scaleFlag[ch] = 0;
        }
        else {
            ratio = tmp_nrg[cnt] / meanE;
            hMcacEnc->scaleFlag[ch] = 1;
            hMcacEnc->mcIld[ch] = max(1, min(ENERGY_BALENCE_SCOPE - 1, (short)(ENERGY_BALENCE_SCOPE * ratio + 0.5f)));
            qratio = (float)ENERGY_BALENCE_SCOPE / hMcacEnc->mcIld[ch];
        }
#else
        ratio = tmp_nrg[cnt] / meanE;
        hMcacEnc->mcIld[ch] = McIldQuantization(ratio, &qratio);
#endif

        VMultC(spectrum[ch], qratio, spectrum[ch], FRAME_LEN);
    }

    return;
}

/*
Get the absolute value of the matrix
I/O params:
    AVS3_MC_ENC_HANDLE hMcacEnc                     (i) MCAC handle
    float corrMatrix[MAX_CHANNELS][MAX_CHANNELS]   (o) cross-correlation matrix
*/
static void GetMatrixAbs(
    AVS3_MC_ENC_HANDLE hMcacEnc,
    float crossCorrelationMat[MAX_CHANNELS][MAX_CHANNELS]
)
{
    short ch1, ch2;

    /* correlation: */
    for (ch1 = 0; ch1 < hMcacEnc->coupleChNum; ch1++)
    {
        for (ch2 = ch1; ch2 < hMcacEnc->coupleChNum; ch2++)
        {
            crossCorrelationMat[ch1][ch2] = (float)fabs(crossCorrelationMat[ch1][ch2]);
        }
    }

    return;
}


/*
Sort the correlation elements
I/O params:
    AVS3_MC_ENC_HANDLE hMcacEnc                         (i)   MCAC handle
    float corrMatrix[MAX_CHANNELS][MAX_CHANNELS,       (o)   cross-correlation matrix
    McacSort corrSort[]                                 (o)   sort correlation
*/
static unsigned int GetCorrElementsSorted(
    AVS3_MC_ENC_HANDLE hMcacEnc,
    float crossCorrelationMat[MAX_CHANNELS][MAX_CHANNELS],
    McacSort corrSort[]
)
{
    short ch1, ch2;
    float tmp_max_corr;
    int i, j;
    unsigned int sortNum = 0;
    short nchans = hMcacEnc->coupleChNum;
    float tmpMatrix[MAX_CHANNELS][MAX_CHANNELS] = { 0.0f };

    for (i = 0; i < MAX_CHANNELS; i++) {
        for (j = 0; j < MAX_CHANNELS; j++) {
            tmpMatrix[i][j] = (float)fabs(crossCorrelationMat[i][j]);
        }
    }


    for (i = 1; i < nchans / 2; i++) {

        SearchMaxCorrValue(hMcacEnc, &ch1, &ch2, &tmp_max_corr, tmpMatrix);

        if (tmp_max_corr < WHOLE_COUPLE_THREHOLD) {
            break;
        }

        corrSort[sortNum].xcorr = tmp_max_corr;
        corrSort[sortNum].ch1 = ch1;
        corrSort[sortNum].ch2 = ch2;
        sortNum++;

        tmpMatrix[ch1][ch2] = 0;

    }

    return sortNum;
}

/*
Get the global Max correlation
I/O params:
    AVS3_MC_ENC_HANDLE hMcacEnc                         (i)   MCAC handle
    float corrMatrix[MAX_CHANNELS][MAX_CHANNELS]       (o)   cross-correlation matrix
    char  maxCpe[MAX_CHANNELS / 2][2]                   (o)   max correlation pair
*/
float GetGlobalMaxCorrelation(
    AVS3_MC_ENC_HANDLE hMcacEnc,
    float crossCorrelationMat[MAX_CHANNELS][MAX_CHANNELS],
    char  maxCpe[MAX_CHANNELS / 2][2]
)
{
    short ch1, ch2;
    float max_corr, tmp_max_corr, sum_corr;
    int pair;
    int i, j, k;
    unsigned int sortNum;
    short nchans = hMcacEnc->coupleChNum;
    float tmpMatrix[MAX_CHANNELS][MAX_CHANNELS] = { 0.0f };
    short  tmpCpe[MAX_CHANNELS / 2][2], tmp[2];
    McacSort xCorrSort[MAX_CHANNELS * (MAX_CHANNELS - 1) / 2];

    for (i = 0; i < MAX_CHANNELS * (MAX_CHANNELS - 1) / 2; i++) {
        xCorrSort[i].ch1 = 0;
        xCorrSort[i].ch2 = 0;
        xCorrSort[i].xcorr = 0;
    }

    sortNum = GetCorrElementsSorted(hMcacEnc, crossCorrelationMat, xCorrSort);

    max_corr = 0;
    for (i = 0; (unsigned int)i < sortNum; i++) {

        sum_corr = 0.0;

        for (j = 0; j < MAX_CHANNELS / 2; j++) {
            for (k = 0; k < 2; k++) {
                tmpCpe[j][k] = -1;
            }
        }

        sum_corr += xCorrSort[i].xcorr;

        tmpCpe[0][0] = xCorrSort[i].ch1;
        tmpCpe[0][1] = xCorrSort[i].ch2;

        for (j = 0; j < MAX_CHANNELS; j++) {
            for (k = 0; k < MAX_CHANNELS; k++) {
                tmpMatrix[j][k] = crossCorrelationMat[j][k];
            }
        }

        RefreshCrossCorrelationMatrix(hMcacEnc, xCorrSort[i].ch1, xCorrSort[i].ch2, tmpMatrix);

        for (pair = 1; pair < nchans / 2; pair++) {

            SearchMaxCorrValue(hMcacEnc, &ch1, &ch2, &tmp_max_corr, tmpMatrix);

            if (tmp_max_corr < WHOLE_COUPLE_THREHOLD) {
                break;
            }

            tmpCpe[pair][0] = ch1;
            tmpCpe[pair][1] = ch2;

            sum_corr += tmp_max_corr;

            RefreshCrossCorrelationMatrix(hMcacEnc, ch1, ch2, tmpMatrix);
        }

        /* save max cpe */
        if (max_corr < sum_corr) {
            for (pair = 0; pair < nchans / 2; pair++) {
                maxCpe[pair][0] = (char)tmpCpe[pair][0];
                maxCpe[pair][1] = (char)tmpCpe[pair][1];
                max_corr = sum_corr;
            }
        }
    }

    pair = 0;
    for (i = 0; i < MAX_CHANNELS / 2; i++) {
        if (maxCpe[i][0] != -1) {
            pair++;
        }
        else {
            break;
        }
    }

    for (i = 0; i < pair - 1; i++) {
        for (j = 0; j < pair - 1 - i; j++) {
            if (maxCpe[j][0] > maxCpe[j + 1][0]) {
                tmp[0] = maxCpe[j][0];
                tmp[1] = maxCpe[j][1];

                maxCpe[j][0] = maxCpe[j + 1][0];
                maxCpe[j][1] = maxCpe[j + 1][1];

                maxCpe[j + 1][0] = (char)tmp[0];
                maxCpe[j + 1][1] = (char)tmp[1];
            }
        }
    }

    return max_corr;
}


#ifndef ONLY_PAIR_ILD
/*
Get energy uniform value of multi channels
I/O params:
    AVS3_MC_ENC_HANDLE hMcacEnc                         (i)   MCAC handle
    float *spectrum[MAX_CHANNELS]                       (i/o) spectrum
    const short nchan                                   (i)   number of channels
    float energy[MAX_CHANNELS]                          (i/o) buffer with energies for each channel
    float energyUniform[MAX_CHANNELS]                   (o)   active channel uniform energies
*/
static void GetEnergyUniformVal(
    AVS3_MC_ENC_HANDLE hMcacEnc,
    float *spectrum[MAX_CHANNELS],
    const short nchan,
    float energy[MAX_CHANNELS],
    float energyUniform[MAX_CHANNELS]
)
{
    short ch;
    float nrg_max = 0.0;

    for (ch = 0; ch < hMcacEnc->coupleChNum; ch++) {
        if (nrg_max < energy[ch]) {
            nrg_max = energy[ch];
        }
    }

    if (nrg_max > 0) {
        for (ch = 0; ch < hMcacEnc->coupleChNum; ch++) {
            energyUniform[ch] = energy[ch] / nrg_max;
        }
    }
    else {
        for (ch = 0; ch < hMcacEnc->coupleChNum; ch++) {
            energyUniform[ch] = 0;
        }
    }

    return;
}


/*
Get energy deviation measurement
I/O params:
    AVS3_MC_ENC_HANDLE hMcacEnc                     (i)   MCAC handle
    float *spectrum[MAX_CHANNELS]                   (i/o) MDCT spectrum
    const short chNum                               (i)   number of channels
    const char  CpeSet[MAX_CHANNELS / 2][2]         (i)   the channel index of pairs
    float energy[MAX_CHANNELS]                         (i/o) buffer with energies for each channel
    int *pairNum                                    (o)   the pair num
*/
static float GetEnergyDeviationMeasurement(
    AVS3_MC_ENC_HANDLE hMcacEnc,
    float *spectrum[MAX_CHANNELS],
    const short nchan,
    const char  CpeSet[MAX_CHANNELS / 2][2],
    float nrg[MAX_CHANNELS],
    int *pairNum
)
{
    short ch;
    float energyUniform[MAX_CHANNELS];	     		/* active channel uniform energies  */
    float energyUniformPaired[MAX_CHANNELS];	    /* paired channels' uniform energies  */
    int pairedChNum;
    float energyUniform_avg;
    float deviation_max = 0;

    /*debug*/
    static int noPairNum;

    /* get energy uniform value */
    GetEnergyUniformVal(hMcacEnc, spectrum, nchan, nrg, energyUniform);

    energyUniform_avg = SumFloat(energyUniform, hMcacEnc->coupleChNum) / hMcacEnc->coupleChNum;

    /* all energies are zero */
    if (energyUniform_avg == 0) {
        return 0.0;
    }

    /* get paired energy uniform value */
    pairedChNum = GetAvailableChsEnergies(CpeSet, energyUniform, energyUniformPaired, hMcacEnc->coupleChNum);

    *pairNum = pairedChNum;

    for (ch = 0; ch < pairedChNum; ch++) {
        if (energyUniformPaired[ch] > energyUniform_avg) {
            if (energyUniformPaired[ch] / energyUniform_avg > deviation_max) {
                deviation_max = energyUniformPaired[ch] / energyUniform_avg;
            }
        }
    }

    return deviation_max;
}
#endif

/*
judge moduleMode(MCAC or MCAC) and ILDMode(PairILD or ALLILD)
I/O params:
    AVS3_MC_ENC_HANDLE hMcacEnc                                 (i/o) MCAC encoder structure
    char  maxCpe[MAX_CHANNELS / 2][2]                           (o)   max correlation pair
    float *maxCorrelation                                       (o)   max correlation sum
    float correletionMatrix[MAX_CHANNELS][MAX_CHANNELS]         (o)   correlation matrix
    float energy[MAX_CHANNELS]                                     (i/o) buffer with energies for each channel
    int   *pairILDMode                                          (o)   pair ILD mode
*/
void JudgeMode(
    AVS3_MC_ENC_HANDLE hMcacEnc,
    char  maxCpe[MAX_CHANNELS / 2][2],
    float *maxCorrelation,
    float correletionMatrix[MAX_CHANNELS][MAX_CHANNELS],
    float nrg[MAX_CHANNELS],
    int   *pairILDMode
)
{
    short i, k;
    float tmpMatrix[MAX_CHANNELS][MAX_CHANNELS] = { 0.0 };
    char  subMaxCpe[MAX_CHANNELS / 2][2];
    short nchan;
#ifndef ONLY_PAIR_ILD
    int pairNum;
    float dev_mcac;
    int pairILD_flag;
#endif

    nchan = hMcacEnc->channelNum;

    for (i = 0; i < nchan; i++)
    {
        hMcacEnc->mcIld[i] = 0;
    }

    for (i = 0; i < hMcacEnc->coupleChNum; i++)
    {
        for (k = 0; k < hMcacEnc->coupleChNum; k++)
        {
            tmpMatrix[i][k] = 0.0;
        }
    }

    for (i = 0; i < hMcacEnc->coupleChNum / 2; i++)
    {
        for (k = 0; k < 2; k++)
        {
            subMaxCpe[i][k] = -1;
        }
    }

    /* get channel energies */
    CalcChannelEnergies(hMcacEnc->mcSpectrum, nrg, nchan);
    /* get correlation matrix */
    GetCorrelationMatrix(hMcacEnc, hMcacEnc->mcSpectrum, nrg, correletionMatrix);

    //GetMatrixAbs(hMcacEnc, correletionMatrix);

    for (i = 0; i < hMcacEnc->coupleChNum; i++)
    {
        for (k = 0; k < hMcacEnc->coupleChNum; k++)
        {
            tmpMatrix[i][k] = correletionMatrix[i][k];
        }
    }


    *maxCorrelation = GetGlobalMaxCorrelation(hMcacEnc, correletionMatrix, maxCpe);

#ifndef ONLY_PAIR_ILD
    /* get deviation  */
    dev_mcac = GetEnergyDeviationMeasurement(hMcacEnc, hMcacEnc->mcSpectrum, hMcacEnc->channelNum, maxCpe, nrg, &pairNum);


    pairILD_flag = 0;
    if (dev_mcac > DEVIA_THREHOLD) {
        pairILD_flag = 1;
    }

    /* judge the ild mode */
    if (pairILD_flag)
    {
        *pairILDMode = PAIR_ILD_MODE;
    }
    else
    {
        *pairILDMode = ALL_ILD_MODE;
    }
#else
    *pairILDMode = PAIR_ILD_MODE;
#endif
}


#ifdef IMPR_MIX_BIT_ALLOC
/* Calculate the input signal energy to get hasSilFlag and silFlag[ch]
    AVS3EncoderHandle stAvs3                           (i)
    const short nChans                                 (i)
    const short lenFrame                               (i)
*/
void McMixGetSilenceFlag(
    AVS3EncoderHandle stAvs3,
    const short nChans,
    const short lenFrame)
{
    short ch;
    AVS3_ENC_CORE_HANDLE hEncCore = NULL;
    float energy[MAX_CHANNELS];
    float energyDB[MAX_CHANNELS];

    stAvs3->hMcEnc->hasSilFlag = 0;
    for (ch = 0; ch < nChans; ch++)
    {
        hEncCore = stAvs3->hEncCore[ch];
        energy[ch] = Dotp(hEncCore->origSpectrum, hEncCore->origSpectrum, FRAME_LEN) / FRAME_LEN;
        energyDB[ch] = 10.0f * (float)log10(energy[ch] / 65535.0f / 65535.0f);

        stAvs3->hMcEnc->silFlag[ch] = 0;
        if (energyDB[ch] < stAvs3->silThrehold) {
            if (stAvs3->hMcEnc->lfeExist && (ch == LFE_CHANNEL_INDEX)) {
                continue;
            }
            stAvs3->hMcEnc->silFlag[ch] = 1;
            stAvs3->hMcEnc->hasSilFlag = 1;
        }
    }
}


/*
Get float point bit ratios for each channel
I/O params:
    short channelNum                            (i) channel number
    float *mdctSpectrum[MAX_CHANNELS]           (i) mdct spectrum for each channel
    float chBitRatiosFloat[MAX_CHANNELS]        (o) float bit ratio for each channel
*/
static void GetChRatioFloat(
    short channelNum,
    float *mdctSpectrum[MAX_CHANNELS],
    float chBitRatiosFloat[MAX_CHANNELS]
)
{
    float sumEnergy;
    short chIdx;
    float energy[MAX_CHANNELS + 1];

    CalcChannelEnergies(mdctSpectrum, energy, channelNum);

    sumEnergy = 1.0f / max(SumFloat(energy, channelNum), AVS3_EPSILON);

    /* calculate ratio for each active channel */
    for (chIdx = 0; chIdx < channelNum; chIdx++) {
        chBitRatiosFloat[chIdx] = energy[chIdx] * sumEnergy;
    }

    return;
}


/*
Get fixed-point bit ratios for each channel from channel bytes
I/O params:
    short channelNum                        (i) channel number
    short chBytes[MAX_CHANNELS]             (i) channel bytes for each channel
    short chBitRatios[MAX_CHANNELS]         (o) fix-point bit ratio for each channel
*/
static void GetChRatioFromBytes(
    short channelNum,
    short chBytes[MAX_CHANNELS],
    short chBitRatios[MAX_CHANNELS]
)
{
    int32_t sumBytes;
    float chRatio;
    short chIdx, i;
    short tmpFactor[MAX_CHANNELS];

    sumBytes = 0;
    for (i = 0; i < channelNum; i++) {
        sumBytes += chBytes[i];
    }

    /* calculate ratio for each active channel */
    for (chIdx = 0; chIdx < channelNum; chIdx++) {
        chRatio = (float)chBytes[chIdx] / sumBytes;
        tmpFactor[chIdx] = min(BITRATE_MC_RATIO_SCOPE - 1, max(1, (unsigned short)(BITRATE_MC_RATIO_SCOPE * chRatio + 0.5f)));
    }

    MvShort2Short(tmpFactor, chBitRatios, channelNum);

    return;
}


/* Get splitObjBits from availableBits */
static int32_t MixSplitObjBits(
    float bedBitsRatio, 
    int32_t availableBits)
{
    int32_t splitObjBits;
    int32_t splitBedBits;

    splitBedBits = (int32_t)(availableBits * bedBitsRatio) / AVS3_BS_BYTE_SIZE * AVS3_BS_BYTE_SIZE;
    splitObjBits = availableBits - splitBedBits;

    return splitObjBits;
}
#endif


/*
Apply MCAC algorithm to input channels
I/O params:
    AVS3_MC_ENC_HANDLE hMcacEnc                             (i/o) MCAC encoder structure
    char  maxCpe[MAX_CHANNELS / 2][2]                       (i)   max correlation pair
    float maxCorrelation                                    (i)   max correlation sum
    float correletionMatrix[MAX_CHANNELS][MAX_CHANNELS]     (i)   correlation matrix
    float energy[MAX_CHANNELS]                                 (i)   buffer with energies for each channe
    const short ildMode                                     (i)   ild mode
*/
void Avs3McacEnc(
    AVS3_MC_ENC_HANDLE hMcacEnc,
    char  maxCpe[MAX_CHANNELS / 2][2],
    float maxCorrelation,
    float correletionMatrix[MAX_CHANNELS][MAX_CHANNELS],
    float energy[MAX_CHANNELS],
    const short ildMode
)
{
#ifndef ONLY_PAIR_ILD
    short ch, i, ch1, ch2;
#else
    short i, ch1, ch2;
#endif
    short pairCnt;
    short cpEle[MAX_CHANNELS];
    short inactiveBoxDetected;

    short flag_mono;
    short indx_pair;
    short no_couple_flag;
    short nchan;
#ifndef ONLY_PAIR_ILD
    float qratio;
#endif

    inactiveBoxDetected = 0;
    SetShort(cpEle, 0, MAX_CHANNELS);

    nchan = hMcacEnc->channelNum;

    for (i = 0; i < MAX_CHANNELS; i++) {
        hMcacEnc->mcIld[i] = 0;
    }

#ifndef ONLY_PAIR_ILD
    if (ildMode == ALL_ILD_MODE) {
        /* Normalize energies with global ILD */
        EnergyBalanceMode1(hMcacEnc, hMcacEnc->mcSpectrum, energy, nchan);
    }
#endif

    no_couple_flag = 0;
    if (maxCorrelation < WHOLE_COUPLE_THREHOLD) {
        no_couple_flag = 1;
    }

    pairCnt = 0;
    flag_mono = 0;
    ch1 = 0;
    ch2 = 1;
    indx_pair = 0;

    if (!no_couple_flag) {
        while (pairCnt < hMcacEnc->coupleChNum) {
            if (pairCnt < hMcacEnc->coupleChNum / 2) {

                if (flag_mono) {
                    indx_pair++;
                }

                if (indx_pair >= hMcacEnc->coupleChNum / 2) {
                    break;
                }

                ch1 = maxCpe[indx_pair][0];
                ch2 = maxCpe[indx_pair][1];

                if ((ch1 == -1) || (ch2 == -1)) {
                    break;
                }

                /* added correlation limit */
                if ((float)fabs(correletionMatrix[ch1][ch2]) < WHOLE_COUPLE_THREHOLD) {
                    indx_pair++;
                    continue;
                }

                hMcacEnc->hPair[pairCnt].ch1 = ch1;
                hMcacEnc->hPair[pairCnt].ch2 = ch2;

                if (ildMode == PAIR_ILD_MODE) {
                    EnergyBalanceMode2(hMcacEnc, hMcacEnc->mcSpectrum, nchan, ch1, ch2);
                }

                /* calculate all related values: */
                MsDownmix(hMcacEnc->mcSpectrum, ch1, ch2);

                flag_mono = 0;

                cpEle[ch1] = 1;
                cpEle[ch2] = 1;

                pairCnt++;
                indx_pair++;
            }
            else
            {
                break;
            }
        }
    }

    /*save number of boxes for next frame*/
    hMcacEnc->pairCnt = pairCnt;

#ifndef ONLY_PAIR_ILD
    if (ildMode == ALL_ILD_MODE) {
        for (ch = 0; ch < hMcacEnc->coupleChNum; ch++) {
            if ((!cpEle[ch]) || hMcacEnc->pairCnt == 0) {
                if (hMcacEnc->scaleFlag[ch]) {
                    qratio = (float)hMcacEnc->mcIld[ch] / ENERGY_BALENCE_SCOPE;
                }
                else {
                    qratio = (float)ENERGY_BALENCE_SCOPE / hMcacEnc->mcIld[ch];
                }

                VMultC(hMcacEnc->mcSpectrum[ch], qratio, hMcacEnc->mcSpectrum[ch], FRAME_LEN);
                hMcacEnc->mcIld[ch] = 0;
            }
        }
    }
#endif

    return;
}


#ifdef IMPR_MIX_BIT_ALLOC
/*
Get float ratio of bedAllocBits / TotalAllocBits
    int32_t totalBitrate                           (i) total BitRate
    short bedChNum                              (i) bed channel num
    short objChNum                              (i) obj channel num
*/
static float SplitTotalBits(
    int32_t totalBitrate,
    short bedChNum,
    short objChNum)
{
    float tmpRatio_f = 0.0f;

    /* one example, to be fixed */
    if (totalBitrate == 384000) {
        tmpRatio_f = 0.583335280;
    }
    else if (totalBitrate == 768000) {
        tmpRatio_f = 0.507143021;
    }
    else {
        tmpRatio_f = (float)bedChNum / (bedChNum + objChNum);
    }

    return tmpRatio_f;
}


static void UpdateMixAllocStrategy(
    AVS3EncoderHandle stAvs3,
    int32_t totalBitrate)
{
    AVS3_MC_ENC_HANDLE hMcacEnc;
    short numBedNoSil, safetyChNum;
    short ch;

    hMcacEnc = stAvs3->hMcEnc;
    numBedNoSil = 0;
    safetyChNum = (short)(totalBitrate * FRAME_LEN / AVS3_SAMPLING_48KHZ / BIT_FRAME_MAX) + 1;

    /* case1:  silEna = 0 */
    if (stAvs3->enableSilDetect == 0) {
        hMcacEnc->mixAllocStrategy = MIX_ALLOC_STRATEGY_INTERNAL;
        return;
    }

    /* case2:  bedOnly or ObjOnly */
    if ((hMcacEnc->isMixed && (stAvs3->soundBedType == 1)) == 0) {
        hMcacEnc->mixAllocStrategy = MIX_ALLOC_STRATEGY_INTERNAL;
        return;
    }

    /* case3:  silEna = 1 and Mix */
    /* get numBedNoSil */
    for (ch = 0; ch < hMcacEnc->channelNum; ch++) {
        if ((hMcacEnc->lfeExist) && (ch == hMcacEnc->lfeChIdx)) {
            continue;
        }
        if (hMcacEnc->silFlag[ch] == 1) {
            continue;
        }
        numBedNoSil++;
    }

    /* judge availableBits */
    if (numBedNoSil >= safetyChNum) {
        hMcacEnc->mixAllocStrategy = MIX_ALLOC_STRATEGY_OBJ2BED;
    }
    else {
        hMcacEnc->mixAllocStrategy = MIX_ALLOC_STRATEGY_INTERNAL;
    }

    return;
}


static void McAllocPreProc(
    AVS3EncoderHandle stAvs3,
    int32_t totalBits,
    float chBitRatiosFloat[MAX_CHANNELS],
    short chBitRatios[MAX_CHANNELS])
{
    AVS3_MC_ENC_HANDLE hMcacEnc;
    short safetyChNumBed, safetyChNumObj;
    short i, j, k, m, ch;
    short numBedNoSil, numObjNoSil, numAllNoSil;
    short updateChNumBed, updateChNumObj;
    int32_t objBits, bedBits;
    int32_t objSilLeftBits;
    int32_t bytesAlloc;
    int32_t objAvgBytes[MAX_CHANNELS] = { 0 };
    short silFlagReorder[MAX_CHANNELS] = { 0 };
    float ratioSumBedNoSil;
    float mappingBedRatioFloatNoSil[MAX_CHANNELS] = { 0.0f };
    int32_t bedChBytes[MAX_CHANNELS] = { 0 };
    short chBytes[MAX_CHANNELS] = { 0 };
    short chBitRatiosFloatNotZeroNum, changeNum;

    hMcacEnc = stAvs3->hMcEnc;
    objBits = 0;
    bedBits = 0;

    if (hMcacEnc->isMixed) {
        if (stAvs3->soundBedType == 1) { /* MIX */
            objBits = MixSplitObjBits(hMcacEnc->bedBitsRatio, totalBits);
        }
        else { /* only objs */
            objBits = totalBits;
        }

        /* get objAvgBytes of mode MIX_ALLOC_STRATEGY_OBJ2BED */
        if ((hMcacEnc->objNum > 0) && (hMcacEnc->mixAllocStrategy == MIX_ALLOC_STRATEGY_OBJ2BED)) {
            bytesAlloc = 0;
            for (ch = hMcacEnc->channelNum; ch < hMcacEnc->channelNum + hMcacEnc->objNum; ch++) {
                objAvgBytes[ch] = objBits / AVS3_BS_BYTE_SIZE / hMcacEnc->objNum;
                bytesAlloc += objAvgBytes[ch];
            }

            if (bytesAlloc < objBits / AVS3_BS_BYTE_SIZE) {
                for (ch = hMcacEnc->channelNum; ch < hMcacEnc->channelNum + hMcacEnc->objNum; ch++) {
                    objAvgBytes[ch] += 1;
                    bytesAlloc += 1;
                    if (bytesAlloc >= objBits / AVS3_BS_BYTE_SIZE) {
                        break;
                    }
                }
            }
        }
    }

    /* get numBedNoSil and numObjNoSil */
    numBedNoSil = 0;
    numObjNoSil = 0;
    objSilLeftBits = 0;
    for (ch = 0; ch < hMcacEnc->channelNum; ch++) {
        if ((hMcacEnc->lfeExist) && (ch == hMcacEnc->lfeChIdx)) {
            continue;
        }
        if (hMcacEnc->silFlag[ch] == 1) {
            continue;
        }
        numBedNoSil++;
    }

    for (ch = hMcacEnc->channelNum; ch < hMcacEnc->channelNum + hMcacEnc->objNum; ch++) {
        if (hMcacEnc->silFlag[ch] == 1) {
            objSilLeftBits += AVS3_BS_BYTE_SIZE * (objAvgBytes[ch] - SILENCE_BYTES);
            continue;
        }
        numObjNoSil++;
    }

    /* get safetyChNum */
    safetyChNumBed = 0;
    safetyChNumObj = 0;
    /* mix(both bed and obj) */
    if ((hMcacEnc->isMixed) && (stAvs3->soundBedType == 1)) {
        if (hMcacEnc->mixAllocStrategy == MIX_ALLOC_STRATEGY_OBJ2BED) {
            objBits -= objSilLeftBits;
        }
        bedBits = totalBits - objBits;
        safetyChNumBed = (short)(bedBits / BIT_FRAME_MAX) + 1;
        if (hMcacEnc->mixAllocStrategy == MIX_ALLOC_STRATEGY_INTERNAL) {
            safetyChNumObj = (short)(objBits / BIT_FRAME_MAX) + 1;
        }
    }
    else {
        /* bed only or obj only */
        if ((hMcacEnc->isMixed) && (stAvs3->soundBedType == 0)) {
            safetyChNumObj = (short)(totalBits / BIT_FRAME_MAX) + 1;
            objBits = totalBits;
        }
        else {
            safetyChNumBed = (short)(totalBits / BIT_FRAME_MAX) + 1;
            bedBits = totalBits;
        }
    }

    /* update silFlag */
    updateChNumBed = 0;
    updateChNumObj = 0;
    if (numObjNoSil < safetyChNumObj) {
        updateChNumObj = safetyChNumObj - numObjNoSil;
    }

    if (numBedNoSil < safetyChNumBed) {
        updateChNumBed = safetyChNumBed - numBedNoSil;
    }

    if (updateChNumBed > 0) {
        /* update silFlag (bed part) */
        for (i = 0; i < hMcacEnc->channelNum; i++) {
            if (hMcacEnc->silFlag[i] == 1) {
                if (updateChNumBed > 0) {
                    updateChNumBed--;
                    hMcacEnc->silFlag[i] = 0;
                }
                else {
                    break;
                }
            }
        }
    }

    if (updateChNumObj > 0) {
        /* update silFlag (bed part) */
        for (i = hMcacEnc->channelNum; i < hMcacEnc->channelNum + hMcacEnc->objNum; i++) {
            if (hMcacEnc->silFlag[i] == 1) {
                if (updateChNumObj > 0) {
                    updateChNumObj--;
                    numObjNoSil++;
                    hMcacEnc->silFlag[i] = 0;
                }
                else {
                    break;
                }
            }
        }
    }

    /* get objAvgBytes of mode MIX_ALLOC_STRATEGY_OBJ2BED */
    if ((hMcacEnc->objNum > 0) && (numObjNoSil > 0) && (hMcacEnc->mixAllocStrategy == MIX_ALLOC_STRATEGY_INTERNAL)) {
        bytesAlloc = 0;
        for (ch = hMcacEnc->channelNum; ch < hMcacEnc->channelNum + hMcacEnc->objNum; ch++) {
            if (hMcacEnc->silFlag[ch] == 0) {
                objAvgBytes[ch] = objBits / AVS3_BS_BYTE_SIZE / numObjNoSil;
                bytesAlloc += objAvgBytes[ch];
            }
        }

        if (bytesAlloc < objBits / AVS3_BS_BYTE_SIZE) {
            for (ch = hMcacEnc->channelNum; ch < hMcacEnc->channelNum + hMcacEnc->objNum; ch++) {
                if (hMcacEnc->silFlag[ch] == 0) {
                    objAvgBytes[ch] += 1;
                    bytesAlloc += 1;
                    if (bytesAlloc >= objBits / AVS3_BS_BYTE_SIZE) {
                        break;
                    }
                }
            }
        }
    }

    /* silFlag reorder, put LFE channel to the last ,the input make sure LFE channel's silFlag is always 0 */
    i = 0;
    for (ch = 0; ch < hMcacEnc->channelNum + hMcacEnc->objNum; ch++)
    {
        if (ch != LFE_CHANNEL_INDEX || !hMcacEnc->lfeExist)
        {
            silFlagReorder[i] = hMcacEnc->silFlag[ch];
            i++;
        }
    }

    if (hMcacEnc->lfeExist)
    {
        silFlagReorder[hMcacEnc->channelNum + hMcacEnc->objNum - 1] = hMcacEnc->silFlag[LFE_CHANNEL_INDEX];
    }

    /* calculate bed ratioFloat sum */
    ratioSumBedNoSil = 0.0f;
    chBitRatiosFloatNotZeroNum = 0;
    for (i = 0; i < hMcacEnc->coupleChNum; i++) {
        if (silFlagReorder[i] == 0) {
            ratioSumBedNoSil += chBitRatiosFloat[i];
            if (chBitRatiosFloat[i] > 0) {
                chBitRatiosFloatNotZeroNum++;
            }
        }
    }

    /* to be fixed */
    if (chBitRatiosFloatNotZeroNum < safetyChNumBed) {
        changeNum = safetyChNumBed - chBitRatiosFloatNotZeroNum;
        for (i = 0; i < hMcacEnc->coupleChNum; i++) {
            if (chBitRatiosFloat[i] == 0) {
                chBitRatiosFloat[i] = 0.01f;
                ratioSumBedNoSil += chBitRatiosFloat[i];
                changeNum--;
                if (changeNum <= 0) {
                    break;
                }
            }
        }
    }

    j = 0;
    for (i = 0; i < hMcacEnc->coupleChNum; i++) {
        if (silFlagReorder[i] == 0) {
            bedChBytes[j] = (int32_t)(chBitRatiosFloat[i] / ratioSumBedNoSil * bedBits) / AVS3_BS_BYTE_SIZE;
            j++;
        }
    }

    j = 0;
    k = 0;
    m = 0;
    for (ch = 0; ch < hMcacEnc->coupleChNum + hMcacEnc->objNum; ch++) {
        if ((silFlagReorder[ch] == 0) && (ch < hMcacEnc->coupleChNum)) {
            chBytes[k] = (short)bedChBytes[j];
            j++;
            k++;
        }

        if ((silFlagReorder[ch] == 0) && (ch >= hMcacEnc->coupleChNum)) {
            if (hMcacEnc->lfeExist) {
                chBytes[k] = (short)objAvgBytes[ch + 1];
            }
            else {
                chBytes[k] = (short)objAvgBytes[ch];
            }
            m++;
            k++;
        }
    }

    numAllNoSil = 0;
    for (i = 0; i < hMcacEnc->channelNum + hMcacEnc->objNum; i++) {
        if ((hMcacEnc->lfeExist) && (ch == hMcacEnc->lfeChIdx)) {
            continue;
        }
        if (hMcacEnc->silFlag[i] == 0) {
            numAllNoSil++;
        }
    }

    /* get silFlag=0 ch ratio */
    GetChRatioFromBytes(numAllNoSil, chBytes, chBitRatios);

    return;
}

#endif


/*
MCAC encoder side interface
    AVS3EncoderHandle stAvs3        // (i/o) encoder handle
    short* channelBytes             // (o)   splited bytes for QC
*/
void Avs3McEncoder(
    AVS3EncoderHandle stAvs3,
    short* channelBytes
)
{
    AVS3_MC_ENC_HANDLE hMcacEnc;
    char  maxCpe[MAX_CHANNELS / 2][2];
    float maxCorrelation;
    float correletionMatrix[MAX_CHANNELS][MAX_CHANNELS];
    float nrg[MAX_CHANNELS];                                /* buffer with energies for each channel */
    int cpeIdx;
    int ildMode;
#ifndef SUPPORT_HIGH_BR_MIX
    short availableBits = 0;
#else
    int32_t availableBits = 0;
#endif

#ifndef IMPR_MIX_BIT_ALLOC
    short chBitRatios[MAX_CHANNELS];
#else
    short chBitRatios[MAX_CHANNELS] = { 0 };
    float chBitRatiosFloat[MAX_CHANNELS] = { 0.0f };
#endif

    int16_t numGroups[MAX_CHANNELS];         // num groups for each channel

    hMcacEnc = stAvs3->hMcEnc;

    for (cpeIdx = 0; cpeIdx < MAX_CHANNELS / 2; cpeIdx++)
    {
        SetC(maxCpe[cpeIdx], -1, 2);
    }

    // decision of mcac mode
    JudgeMode(hMcacEnc, maxCpe, &maxCorrelation, correletionMatrix, nrg, &ildMode);

    // apply mcac
    Avs3McacEnc(hMcacEnc, maxCpe, maxCorrelation, correletionMatrix, nrg, ildMode);

    // grouping for short window
#ifndef IMPR_MIX_BIT_ALLOC
    for (int16_t i = 0; i < hMcacEnc->channelNum; i++) {
#else
    for (int16_t i = 0; i < hMcacEnc->channelNum + hMcacEnc->objNum; i++) {
#endif

        AVS3_ENC_CORE_HANDLE hEncCore = stAvs3->hEncCore[i];
        SpectrumGroupingEnc(hEncCore->origSpectrum, hEncCore->frameLength, hEncCore->transformType,
            hEncCore->groupIndicator, &hEncCore->numGroups);

        numGroups[i] = hEncCore->numGroups;
    }
    // write grouping bitstream
#ifndef IMPR_MIX_BIT_ALLOC
    WriteGroupBitstream(stAvs3, hMcacEnc->channelNum, stAvs3->bitstream, &stAvs3->totalSideBits);
#else
    WriteGroupBitstream(stAvs3, hMcacEnc->channelNum + hMcacEnc->objNum, stAvs3->bitstream, &stAvs3->totalSideBits);
#endif

    // get bits allocation factor
#ifndef IMPR_MIX_BIT_ALLOC
    GetChRatio(hMcacEnc->coupleChNum, hMcacEnc->mcSpectrum, chBitRatios, stAvs3->totalBitrate * FRAME_LEN / AVS3_SAMPLING_48KHZ, 1);
#else
    GetChRatioFloat(hMcacEnc->coupleChNum, hMcacEnc->mcSpectrum, chBitRatiosFloat);
    if (hMcacEnc->isMixed) {
        if (stAvs3->soundBedType == 1) {
            hMcacEnc->bedBitsRatio = SplitTotalBits(stAvs3->totalBitrate, hMcacEnc->channelNum, hMcacEnc->objNum);
            UpdateMixAllocStrategy(stAvs3, stAvs3->totalBitrate);
        }
    }
    McAllocPreProc(stAvs3, stAvs3->bitsPerFrame - stAvs3->totalSideBits, chBitRatiosFloat, chBitRatios);
#endif

    // write MCAC mode bitstream
    WriteMcBitstream(stAvs3, stAvs3->bitstream, &stAvs3->totalSideBits, chBitRatios);

    // get available bits
#ifndef SUPPORT_NNTYPE_LC
    availableBits = GetAvailableBits(stAvs3->bitsPerFrame, stAvs3->totalSideBits, numGroups, hMcacEnc->channelNum);
#else
#ifndef IMPR_MIX_BIT_ALLOC
    availableBits = GetAvailableBits(stAvs3->bitsPerFrame, stAvs3->totalSideBits, numGroups, hMcacEnc->channelNum, stAvs3->nnTypeConfig);
#else
    availableBits = GetAvailableBits(stAvs3->bitsPerFrame, stAvs3->totalSideBits, numGroups, hMcacEnc->channelNum + hMcacEnc->objNum, stAvs3->nnTypeConfig);
#endif
#endif

    // MCAC bit allocation
#ifndef IMPR_MIX_BIT_ALLOC
    McBitsAllocation(availableBits, chBitRatios, hMcacEnc->channelNum, channelBytes, hMcacEnc->lfeExist, hMcacEnc->lfeBytes);
#else
    McBitsAllocationHasSiL(availableBits, chBitRatios, hMcacEnc->channelNum + hMcacEnc->objNum,
        channelBytes, hMcacEnc->silFlag, hMcacEnc->lfeExist, hMcacEnc->lfeBytes);
#endif
}

#endif