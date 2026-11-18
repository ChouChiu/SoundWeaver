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
#include <math.h>
#include <stdlib.h>
#include <assert.h>

#include "avs3_options.h"
#include "avs3_cnst_com.h"
#include "avs3_rom_com.h"
#include "avs3_prot_com.h"


#ifdef FD_SHAPING

/*
compute the LSP coefficients from the quanted LSF coefficients
I/O params:
    const float *lsf                          (i)   quanted LSF coefficients
    float *lsp                                (o)   LSP coefficients
    const short lpcOrder                      (i)   LSP order
    const int samplingRate                    (i)   the sampling rate of the input signal
*/
void LsfToLsp(
    const float *lsf, 
    float *lsp, 
    const short lpcOrder, 
    const int samplingRate
)
{
    short i;

    /* convert LSFs to LSPs */
    for (i = 0; i < lpcOrder; i++) {
        lsp[i] = (float)cos(lsf[i] * AVS3_PI / (samplingRate / 2));
    }

    return;
}


/*
find the polynomial F1(z) or F2(z) from the LSPs
I/O params:
    const float lsp[]                               (i)   LSP coefficients
    float polynomialCoffs[]                         (o)   polynomial coefficients
    const short numCoffs                            (i)   the order of polynomial coefficients
    short flag                                      (i)   the polynomial number (F1(z) or F2(z))
*/
static void GetLsppol(
    const float lsp[], 
    float polynomialCoffs[], 
    const short numCoffs, 
    short flag
)                          
{
    float tmpf;
    const float *posLsp;
    short i, j;

    posLsp = lsp + flag - 1;

    polynomialCoffs[0] = 1.0f;
    tmpf = -2.0f * *posLsp;
    polynomialCoffs[1] = tmpf;

    for (i = 2; i <= numCoffs; i++) {
        posLsp += 2;
        tmpf = -2.0f * *posLsp;
        polynomialCoffs[i] = tmpf * polynomialCoffs[i - 1] + 2.0f * polynomialCoffs[i - 2];

        for (j = i - 1; j > 1; j--) {
            polynomialCoffs[j] += tmpf * polynomialCoffs[j - 1] + polynomialCoffs[j - 2];
        }

        polynomialCoffs[1] += tmpf;
    }

    return;
}


/*
compute LPC parameters from the quantized LSP parameters
I/O params:
    const float *lsp                          (i)   LSP coefficients
    float *lpc                                (o)   LPC coefficients
    const short lpcOrder                      (i)   LSP order
*/
void LspToLpc(
    const float *lsp, 
    float *lpc, 
    const short lpcOrder
)
{
    float poly1[LSP_ROOT_NUM + 1], poly2[LSP_ROOT_NUM + 1];
    short i, k, nc;
    float *pPoly1, *pPoly2, *pPoly1Tmp, *pPoly2Tmp, *pLpc1, *pLpc2;

    nc = lpcOrder / 2;

    /* -----------------------------------------------------*
    * Find the polynomials F1(z) and F2(z)               *
    * ----------------------------------------------------- */

    GetLsppol(lsp, poly1, nc, 1);
    GetLsppol(lsp, poly2, nc, 2);

    /* -----------------------------------------------------*
    * Multiply F1(z) by (1+z^-1) and F2(z) by (1-z^-1)   *
    * ----------------------------------------------------- */

    pPoly1 = poly1 + nc;
    pPoly1Tmp = pPoly1 - 1;
    pPoly2 = poly2 + nc; /* Version using indices            */
    pPoly2Tmp = pPoly2 - 1;
    k = nc - 1;
    for (i = 0; i <= k; i++) {
        *pPoly1-- += *pPoly1Tmp--;
        *pPoly2-- -= *pPoly2Tmp--;
    }

    /* -----------------------------------------------------*
    * A(z) = (F1(z)+F2(z))/2                             *
    * F1(z) is symmetric and F2(z) is antisymmetric      *
    * ----------------------------------------------------- */

    pLpc1 = lpc;
    *pLpc1++ = 1.0;
    pLpc2 = lpc + lpcOrder;
    pPoly1 = poly1 + 1;
    pPoly2 = poly2 + 1;
    for (i = 0; i <= k; i++) {
        *pLpc1++ = 0.5f * (*pPoly1 + *pPoly2);
        *pLpc2-- = 0.5f * (*pPoly1++ - *pPoly2++);
    }

    return;
}

/*
compute spectrum shaping gain in frequency domain
I/O params:
    const float *weightedLpcCoeffs                  (i)   LPC coefficients
    float *lpcGain                                  (o)   spectrum shaping gain in frequency domain
    const short lpcOrder                            (i)   LSP order
*/
static void GetLpcGain(
    const float *weightedLpcCoeffs, 
    float *lpcGain, 
    const short lpcOrder
)
{
    short i, j, n, fftLen;
    float tmp;
    float realPart[FFT_TABLE_SIZE512];
    float imagPart[FFT_TABLE_SIZE512];
    float rawLpcGain[FFT_TABLE_SIZE512];
    float interpLpcGain[BLOCK_LEN_LONG];

    // ratio of interpolation, curr 4
    n = BLOCK_LEN_LONG / (FFT_TABLE_SIZE512 / 2);
    // fft length, 512
    fftLen = FFT_TABLE_SIZE512;

    // rotation of LPC coefficients
    for (i = 0; i < lpcOrder + 1; i++) {
        tmp = (float)(((float)i) * AVS3_PI / (float)(fftLen));
        realPart[i] = (float)(weightedLpcCoeffs[i] * cos(tmp));
        imagPart[i] = (float)(-weightedLpcCoeffs[i] * sin(tmp));
    }
    for (; i < fftLen; i++) {
        realPart[i] = 0.f;
        imagPart[i] = 0.f;
    }

    // perform fft on zero padded lpc with rotation
    FFT(realPart, imagPart, FFT_TABLE_SIZE512);

    // raw lpc gain
    for (i = 0; i < fftLen; i++) {
        rawLpcGain[i] = (float)(1.0f / sqrt(realPart[i] * realPart[i] + imagPart[i] * imagPart[i]));
    }

    // interploation of lpc gain, from 256 points to 1024 points
    // linear interpolation
    for (i = 0; i < fftLen / 2; i++) {
        interpLpcGain[n * i] = rawLpcGain[i];
        interpLpcGain[n * i + 1] = rawLpcGain[i] + (rawLpcGain[i + 1] - rawLpcGain[i]) / n;
        interpLpcGain[n * i + 2] = rawLpcGain[i] + 2.0f * ((rawLpcGain[i + 1] - rawLpcGain[i]) / n);
        interpLpcGain[n * i + 3] = rawLpcGain[i] + 3.0f * ((rawLpcGain[i + 1] - rawLpcGain[i]) / n);
    }

    // get lpc gain for each sfb
    for (i = 0; i < N_SFB_FB_LONG; i++) {

        // sum interpolated gain in each sfb
        tmp = 0.0f;
        for (j = sfb_table_fb_long[i]; j < sfb_table_fb_long[i + 1]; j++) {
            tmp += interpLpcGain[j];
        }

        // get averaged gain in sfb
        tmp /= (float)sfb_len_fb_long[i];

        // set averaged gain to each bin of current sfb
        for (j = sfb_table_fb_long[i]; j < sfb_table_fb_long[i + 1]; j++) {
            lpcGain[j] = tmp;
        }
    }

    return;
}


/*
shaping the input signal in frequency domain
I/O params:
    float *mdctSpectrum                             (i/o) the orignal/shaped MDCT coefficients of the input siganl
    float *lpcQuantCoeffs                           (i)   recovered LPC coefficients
    float *lpcGain                                  (i/o) spectrum shaping gain in frequency domain
    const short lpcOrder                            (i)   LSP order
    const short len                                 (i)   frame length
    const short noInverse                           (i)   spectrum shaping gain in encoder/decoder state
*/
void SpectrumShaping(
    float *mdctSpectrum, 
    float *lpcQuantCoeffs, 
    float *lpcGain, 
    const short lpcOrder, 
    const short len, 
    const short noInverse
)
{
    short i, mdctGainLen;
    float gammaLpc, weightedLpcQuantCoeffs[LPC_ORDER + 1];
    float weightedFactorBuf[LPC_ORDER + 1] = {0.0f};

    /* weighting the quantized LPC coefficients */
    gammaLpc = GAMMA_LPC;
    weightedFactorBuf[0] = 1.0f;
    for (i = 1; i <= lpcOrder; i++)
    {
        weightedFactorBuf[i] = weightedFactorBuf[i - 1] * gammaLpc;
    }
    for (i = 0; i <= lpcOrder; i++)
    {
        weightedLpcQuantCoeffs[i] = weightedFactorBuf[i] * lpcQuantCoeffs[i];
    }

    /* calculate lpc gain */
    mdctGainLen = FFT_TABLE_SIZE512 / 2;
    SetZero(lpcGain, BLOCK_LEN_LONG);
    GetLpcGain(weightedLpcQuantCoeffs, lpcGain, LPC_ORDER);

    if (noInverse) {
        /* shaping the original mdct spectrum */
        for (i = 0; i < BLOCK_LEN_LONG; i++) {
            mdctSpectrum[i] = mdctSpectrum[i] / lpcGain[i];
        }
    }
    else {
        /* shaping the quantized mdct spectrum */
        for (i = 0; i < BLOCK_LEN_LONG; i++) {
            mdctSpectrum[i] = mdctSpectrum[i] * lpcGain[i];
        }
    }

    return;
}


#ifdef POST_SHAPING

/*
post processing of the shaped signal in frequency domain
I/O params:
    float *mdctSpectrum                             (i/o) the shaped/postproc MDCT coefficients of the input siganl
    int16_t numLinesCore                            (i)   number of lines in core band
    const short isInverse                           (i)   inverse shaping or not
*/
void SpecPostShaping(
    float *mdctSpectrum,
    int16_t numLinesCore,
    const short isInverse
)
{
    int16_t i;
#ifndef POST_SHAPING
    int16_t startLine = 176;            // 4.125kHz in 48kHz, 2048 frame length
    float startFac = 1.5f;
#else
    int16_t startLine = 128;            // 3kHz in 48kHz, 2048 frame length
    float startFac = 1.0f;
#endif
    float endFac = 2.2f;
    float facStep;

    facStep = (endFac - startFac) / (numLinesCore - startLine);

    if (isInverse == 0) {
        for (i = startLine; i < numLinesCore; i++) {
            mdctSpectrum[i] *= (startFac + (i - startLine) * facStep);
        }
    }
    else {
        for (i = startLine; i < numLinesCore; i++) {
            mdctSpectrum[i] /= (startFac + (i - startLine) * facStep);
        }
    }

    return;
}

#endif

#endif