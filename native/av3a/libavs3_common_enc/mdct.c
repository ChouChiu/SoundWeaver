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

#include <math.h>
#include "avs3_prot_com.h"

void av3a_IMDCT(float *signal, const short N)
{
    short i;

    float xi[MAX_FFT_TABLE_SIZE];
    float xr[MAX_FFT_TABLE_SIZE];

    float tempr, tempi, c, s, cold; /* temps for pre and post twiddle */
    const float freq = 2.0f * AVS3_PI / N;
    const float cfreq = (float)cos(freq);
    const float sfreq = (float)sin(freq);
    const float cosfreq8 = (float)cos(freq * FACTOR_TWIDDLE_SHORT);
    const float sinfreq8 = (float)sin(freq * FACTOR_TWIDDLE_SHORT);

    c = cosfreq8;
    s = sinfreq8;

    for (i = 0; i < (N >> 2); i++) {
        tempr = -signal[2 * i];
        tempi = signal[(N >> 1) - 1 - 2 * i];

        xr[i] = tempr * c - tempi * s;
        xi[i] = tempi * c + tempr * s;

        cold = c;
        c = c * cfreq - s * sfreq;
        s = s * cfreq + cold * sfreq;
    }

    /* Perform in-place complex IFFT of length N/4 */
    switch (N) {
    case BLOCK_LEN_SHORT * 2:
        IFFT(xr, xi, FFT_TABLE_SIZE64);
        break;
    case BLOCK_LEN_HALF_LONG * 2:
        IFFT(xr, xi, FFT_TABLE_SIZE256);
        break;
    case BLOCK_LEN_LONG * 2:
        IFFT(xr, xi, FFT_TABLE_SIZE512);
        break;
    default:
        break;
    }

    /* prepare for recurrence relations in post-twiddle */
    c = cosfreq8;
    s = sinfreq8;

    /* post-twiddle FFT output and then get output data */
    for (i = 0; i < (N >> 2); i++) {

        /* get post-twiddled FFT output  */
        tempr = NORM_MDCT_FACTOR * (xr[i] * c - xi[i] * s);
        tempi = NORM_MDCT_FACTOR * (xi[i] * c + xr[i] * s);

        /* fill in output values */
        signal[(N >> 1) + (N >> 2) - 1 - 2 * i] = tempr;
        if (i < (N >> 3))
        {
            signal[(N >> 1) + (N >> 2) + 2 * i] = tempr;
        }
        else
        {
            signal[2 * i - (N >> 2)] = -tempr;
        }

        signal[(N >> 2) + 2 * i] = tempi;
        if (i < (N >> 3))
        {
            signal[(N >> 2) - 1 - 2 * i] = -tempi;
        }
        else
        {
            signal[(N >> 2) + N - 1 - 2 * i] = tempi;
        }

        /* use recurrence to prepare cosine and sine for next value of i */
        cold = c;
        c = c * cfreq - s * sfreq;
        s = s * cfreq + cold * sfreq;
    }

#ifdef NEURAL_QC
    for (i = 0; i < N; i++) {
        signal[i] = (float)(signal[i] * sqrt(N));
    }
#endif
}

void MDCT(float *signal, float* output, const short N)
{
    short i, n;

    float xi[MAX_FFT_TABLE_SIZE];
    float xr[MAX_FFT_TABLE_SIZE];
    float tmpReal, tmpImg, c, s, cold;
    const float freq = 2.0f * AVS3_PI / N;
    const float cfreq = (float)cos(freq);
    const float sfreq = (float)sin(freq);
    const float cosfreq8 = (float)cos(freq * FACTOR_TWIDDLE_SHORT);
    const float sinfreq8 = (float)sin(freq * FACTOR_TWIDDLE_SHORT);

    c = cosfreq8;
    s = sinfreq8;

    for (i = 0; i < (N >> 2); i++)
    {
        n = (N >> 1) - 1 - 2 * i;

        if (i < (N >> 3))
        {
            tmpReal = signal[(N >> 2) + n] + signal[N + (N >> 2) - 1 - n];
        }
        else
        {
            tmpReal = signal[(N >> 2) + n] - signal[(N >> 2) - 1 - n];
        }

        n = 2 * i;
        if (i < (N >> 3))
        {
            tmpImg = signal[(N >> 2) + n] - signal[(N >> 2) - 1 - n];
        }
        else
        {
            tmpImg = signal[(N >> 2) + n] + signal[N + (N >> 2) - 1 - n];
        }

        /* calculate pre-twiddled FFT input */
        xr[i] = tmpReal * c + tmpImg * s;
        xi[i] = tmpImg * c - tmpReal * s;

        /* use recurrence to prepare cosine and sine for next value of i */
        cold = c;
        c = c * cfreq - s * sfreq;
        s = s * cfreq + cold * sfreq;
    }

    switch (N) {
    case BLOCK_LEN_SHORT * 2:
        FFT(xr, xi, FFT_TABLE_SIZE64);
        break;
    case BLOCK_LEN_HALF_LONG * 2:
        FFT(xr, xi, FFT_TABLE_SIZE256);
        break;
    case BLOCK_LEN_LONG * 2:
        FFT(xr, xi, FFT_TABLE_SIZE512);
        break;
    default:
        break;
    }

    c = cosfreq8;
    s = sinfreq8;

    for (i = 0; i < (N >> 2); i++)
    {
        tmpReal = (float)(1.f / NORM_MDCT_FACTOR) * (xr[i] * c + xi[i] * s);
        tmpImg = (float)(1.f / NORM_MDCT_FACTOR) * (xi[i] * c - xr[i] * s);

        signal[2 * i] = -tmpReal;
        signal[(N >> 1) - 1 - 2 * i] = tmpImg; 
        signal[(N >> 1) + 2 * i] = -tmpImg; 
        signal[N - 1 - 2 * i] = tmpReal;

        cold = c;
        c = c * cfreq - s * sfreq;
        s = s * cfreq + cold * sfreq;
    }

    /* Output data */
    for (i = 0; i < N >> 1; i++)
    {
#ifndef NEURAL_QC
        output[i] = signal[i];
#else
        output[i] = (float)(signal[i] / sqrt(N));
#endif
    }
}