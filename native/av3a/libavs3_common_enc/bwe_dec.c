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
#include "avs3_cnst_com.h"
#include "avs3_rom_com.h"
#include "avs3_stat_com.h"
#include "avs3_prot_com.h"


#ifdef BWE_DEVELOPE

/*
Init decoder side bwe data structure
I/O params:
    BweDecDataHandle bweDecData         (i/o) decoder side bwe data handle
*/
void InitBweDecData(
    BweDecDataHandle bweDecData
)
{
    SetFloat(bweDecData->sfbEnvelope, 0.0f, MAX_NUM_SFB_BWE);
    SetShort(bweDecData->sfbEnvQIdx, 0, MAX_NUM_SFB_BWE);
    SetShort(bweDecData->whiteningLevel, 0, MAX_NUM_TILE);

    SetFloat(bweDecData->bweSpectrum, 0.0f, BLOCK_LEN_LONG);

    return;
}


/* 
Copy spectrum from source tile to target tile
I/O params:
    BweConfigHandle bweConfig           (i)   bwe config handle
    BweDecDataHandle bweDecData         (i/o) decoder side bwe data handle
    float *mdctSpectrum                 (i)   decoded mdct spectrum
*/
static void BweCopySpectrum(
    BweConfigHandle bweConfig,
    BweDecDataHandle bweDecData,
    float *mdctSpectrum
)
{
    int16_t i;
    int16_t tileIdx;
    int16_t srcLineIdx;

    // clear buffer
    SetFloat(bweDecData->bweSpectrum, 0.0f, BLOCK_LEN_LONG);

    // copy spectrum below bwe start line
    Mvf2f(mdctSpectrum, bweDecData->bweSpectrum, bweConfig->bweStartLine);

    // copy from src tile to target tile
    for (tileIdx = 0; tileIdx < bweConfig->numTiles; tileIdx++) {

        srcLineIdx = bweConfig->srcTiles[tileIdx];

        for (i = bweConfig->targetTiles[tileIdx]; i < bweConfig->targetTiles[tileIdx + 1]; i++) {
            bweDecData->bweSpectrum[i] = mdctSpectrum[srcLineIdx];
            srcLineIdx++;
        }
    }

    return;
}


/*
Middle level whitening of bwe spectrum, by moving average
I/O params:
    float *inSpectrum               (i) input mdct spectrum
    float *outSpectrum              (o) output mdct spectrum
    int16_t start                   (i) start bin index of spectrum
    int16_t stop                    (i) stop bin index of spectrum
    int16_t averageSize             (i) average size in whitening
*/
static void BweSpecWhiteningMid(
    float *inSpectrum,
    float *outSpectrum,
    int16_t start, 
    int16_t stop,
    int16_t averageSize
)
{
    float squareSum;
    float averageSpec;

    for (int16_t i = start; i < stop; i++) {
        
        squareSum = 0.0f;
        averageSpec = 0.0f;

        for (int16_t j = (i - averageSize); j < (i + averageSize + 1); j++) {
            squareSum += inSpectrum[j] * inSpectrum[j];
        }

        averageSpec = squareSum / (float)(2 * averageSize + 1);
        averageSpec = (float)sqrt(averageSpec);

#ifndef BWE_TUNING
        outSpectrum[i] = inSpectrum[i] / averageSpec;
#else
        if (averageSpec == 0.0f) {
            outSpectrum[i] = inSpectrum[i];
        }
        else {
            outSpectrum[i] = inSpectrum[i] / averageSpec;
        }
#endif
    }

    return;
}


/*
High level whitening of bwe spectrum, by noise substitution
I/O params:
    float *inSpectrum               (i) input mdct spectrum
    float *outSpectrum              (o) output mdct spectrum
    int16_t start                   (i) start bin index of spectrum
    int16_t stop                    (i) stop bin index of spectrum
*/
static void BweSpecWhiteningHigh(
    float *inSpectrum,
    float *outSpectrum,
    int16_t start,
    int16_t stop
)
{
    int16_t i;
    float absSum = 0.0f;

    // get abs spectrum sum in current range
    for (i = start; i < stop; i++) {
        absSum += (float)fabs(inSpectrum[i]);
    }

    if (absSum > 0.0f) {
        for (i = start; i < stop; i++) {
            // [-1, 1] range random noise
            outSpectrum[i] = ((float)rand() / (float)RAND_MAX) * 2.0f - 1.0f;
        }
    }
    else {
        // clear spectrum
        for (i = start; i < stop; i++) {
            outSpectrum[i] = 0.0f;
        }
    }

    return;
}


/*
Apply whitening to mdct spectrum
I/O params:
    BweConfigHandle bweConfig           (i) bwe config handle
    BweDecDataHandle bweDecData         (o) decoder side bwe data handle
*/
static void BweApplyWhitening(
    BweConfigHandle bweConfig,
    BweDecDataHandle bweDecData
)
{
    int16_t tileIdx;
    float whitenedSpectrum[BLOCK_LEN_LONG] = { 0.0f };

    // loop over tiles
    for (tileIdx = 0; tileIdx < bweConfig->numTiles; tileIdx++) {

        // get tile related info
        int16_t tileStartLine = bweConfig->targetTiles[tileIdx];
        int16_t tileStopLine = bweConfig->targetTiles[tileIdx + 1];
        int16_t tileWidth = tileStopLine - tileStartLine;

        if (bweDecData->whiteningLevel[tileIdx] == BWE_WHITENING_OFF) {
            // whitening off, copy spectrum
            Mvf2f(bweDecData->bweSpectrum + tileStartLine, whitenedSpectrum + tileStartLine, tileWidth);
        }
        else if (bweDecData->whiteningLevel[tileIdx] == BWE_WHITENING_MID) {
            // whitening middle, divide spectrum by the moving averaged spectrum
            BweSpecWhiteningMid(bweDecData->bweSpectrum, whitenedSpectrum, tileStartLine, tileStopLine, LEN_WHITEN_AVERAGE);
        }
        else if (bweDecData->whiteningLevel[tileIdx] == BWE_WHITENING_HIGH) {
            // whitening high, using random noise
            BweSpecWhiteningHigh(bweDecData->bweSpectrum, whitenedSpectrum, tileStartLine, tileStopLine);
        }
    }

    // copy whitened spectrum back to buffer
    Mvf2f(whitenedSpectrum, bweDecData->bweSpectrum, BLOCK_LEN_LONG);

    return;
}


/*
Apply SFB envelope to whitened spectrum
I/O params:
    BweConfigHandle bweConfig               (i) bwe config handle
    BweDecDataHandle bweDecData             (i) decoder side bwe data handle
    float *mdctSpectrum                     (o) bwe processed mdct spectrum
*/
static void BweApplyEnvelope(
    BweConfigHandle bweConfig,
    BweDecDataHandle bweDecData, 
    float *mdctSpectrum
)
{
    int16_t i;
    int16_t sfbIdx;
    int16_t sfbWidth;
    float targetEner, currEner;
    float gainSfb;

    for (sfbIdx = 0; sfbIdx < bweConfig->numSfb; sfbIdx++) {

        // sfb width
        sfbWidth = bweConfig->sfbTable[sfbIdx + 1] - bweConfig->sfbTable[sfbIdx];

        // curr energy of whitened spectrum
        currEner = 0.0f;
        for (i = bweConfig->sfbTable[sfbIdx]; i < bweConfig->sfbTable[sfbIdx + 1]; i++) {
            currEner += bweDecData->bweSpectrum[i] * bweDecData->bweSpectrum[i];
        }
        currEner /= sfbWidth;

        // target energy of current sfb
        targetEner = (float)pow(2.0f, bweDecData->sfbEnvQIdx[sfbIdx] / 4.24966f - 4.0f);

        // get sfb gain
        if (currEner != 0.0f) {
            gainSfb = (float)sqrt(targetEner / currEner);
        }
        else {
            gainSfb = 1.0f;
        }

        // apply gain to whitened spectrum
        for (i = bweConfig->sfbTable[sfbIdx]; i < bweConfig->sfbTable[sfbIdx + 1]; i++) {
            bweDecData->bweSpectrum[i] *= gainSfb;
        }

        // copy bwe spectrum to mdct spectrum buffer
        for (i = bweConfig->sfbTable[sfbIdx]; i < bweConfig->sfbTable[sfbIdx + 1]; i++) {
            mdctSpectrum[i] = bweDecData->bweSpectrum[i];
        }
    }

    // clear spectrum beyond BWE stop line
    for (i = bweConfig->bweStopLine; i < BLOCK_LEN_LONG; i++) {
        mdctSpectrum[i] = 0.0f;
    }

    return;
}


/*
Decoder side bwe process
I/O params:
    BweConfigHandle bweConfig           (i)   bwe config handle
    BweDecDataHandle bweDecData         (i)   decoder side bwe data handle
    float *mdctSpectrum                 (i/o) mdct spectrum before and after bwe
*/
void BweApplyDec(
    BweConfigHandle bweConfig,
    BweDecDataHandle bweDecData,
    float *mdctSpectrum
)
{
    BweCopySpectrum(bweConfig, bweDecData, mdctSpectrum);

    BweApplyWhitening(bweConfig, bweDecData);

    BweApplyEnvelope(bweConfig, bweDecData, mdctSpectrum);

    return;
}

#endif