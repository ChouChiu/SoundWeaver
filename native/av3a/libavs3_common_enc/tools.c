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
#include <assert.h>

#include "avs3_prot_com.h"

void SetZero(float *vec, const short len)
{
    short i;

    for (i = 0; i < len; i++)
    {
        *vec++ = 0.f;
    }

    return;
}

void SetFloat(float y[], const float val, const short N)
{
    short i;

    for (i = 0; i < N; i++)
    {
        y[i] = val;
    }

    return;
}

void SetShort(short y[],const short a, const short N)
{
    short i;

    for (i = 0; i < N; i++)
    {
        y[i] = a;
    }

    return;
}

#ifdef MIX_DEVELOPE
void SetUShort(unsigned short y[], const unsigned short a, const short N)
{
    short i;

    for (i = 0; i < N; i++)
    {
        y[i] = a;
    }

    return;
}
#endif

#ifdef NEURAL_QC
void SetUC(uint8_t y[], const uint8_t a, const short N)
{
    short i;

    for (i = 0; i < N; i++)
    {
        y[i] = a;
    }

    return;
}
#endif

void Mvf2f(const float x[], float y[], const short n)
{
    short i;

    if (n <= 0)
    {
        return;
    }

    if (y < x)
    {
        for (i = 0; i < n; i++)
        {
            y[i] = x[i];
        }
    }
    else
    {
        for (i = n - 1; i >= 0; i--)
        {
            y[i] = x[i];
        }
    }

    return;
}

float VLinalgNorm(float* vec,const short len)
{
    assert(vec != NULL && len >= 1);

    float result = 0.f;

    for (short i = 0; i < len; i++)
    {
        result += vec[i] * vec[i];
    }

    result = (float)sqrt(result);

    return result;
}

float Dotp(const float  x[], const float  y[], const short  n)
{
    short i;
    float suma;

    suma = x[0] * y[0];

    for (i = 1; i < n; i++)
    {
        suma += x[i] * y[i];
    }

    return suma;
}

void MvShort2Short(const short x[], short y[], const short n )
{
    short i;

    if (n <= 0)
    {
        /* cannot transfer vectors with size 0 */
        return;
    }

    if (y < x)
    {
        for (i = 0; i < n; i++)
        {
            y[i] = x[i];
        }
    }
    else
    {
        for (i = n - 1; i >= 0; i--)
        {
            y[i] = x[i];
        }
    }

    return;
}


unsigned long MvFloat2Short(const float x[], short y[], const short n)
{
    short i;
    float temp;
    unsigned long noClipping = 0;

    if (n <= 0)
    {
        /* cannot transfer vectors with size 0 */
        return 0;
    }

    if ((void*)y < (const void*)x)
    {
        for (i = 0; i < n; i++)
        {
            temp = x[i];
            temp = (float)floor(temp + 0.5f);

            if (temp > 32767.0f)
            {
                temp = 32767.0f;
                noClipping++;
            }
            else if (temp < -32768.0f)
            {
                temp = -32768.0f;
                noClipping++;
            }

            y[i] = (short)temp;
        }
    }
    else
    {
        for (i = n - 1; i >= 0; i--)
        {
            temp = x[i];
            temp = (float)floor(temp + 0.5f);

            if (temp > 32767.0f)
            {
                temp = 32767.0f;
                noClipping++;
            }
            else if (temp < -32768.0f)
            {
                temp = -32768.0f;
                noClipping++;
            }

            y[i] = (short)temp;
        }
    }

    return noClipping;
}

void Vadd(const float x1[], const float x2[], float y[], const short N)
{
    short i;

    for (i = 0; i < N; i++)
    {
        y[i] = x1[i] + x2[i];
    }

    return;
}

void VMult(const float x1[], const float x2[], float y[], const short N)
{
    short i;

    for (i = 0; i < N; i++)
    {
        y[i] = x1[i] * x2[i];
    }

    return;
}

void SwapS(short *a, short *b)
{
    short tmp = *a;
    *a = *b;
    *b = tmp;
}

void SortS(short *x, const short len)
{
    short i, j;
    short min;

    if (x == NULL)
    {
        return;
    }

    for (i = 0; i < len - 1; i++)
    {
        min = i;
        for (j = i + 1; j < len; j++)
        {
            if (x[j] < x[min])
            {
                min = j;
            }
        }

        SwapS(&x[min], &x[i]);
    }
}

float SumFloat(const float *x, const short len)
{
    short i;
    float tmp;

    tmp = 0.f;
    for (i = 0; i < len; i++)
    {
        tmp += x[i];
    }

    return tmp;
}


void VMultC(const float x[], const float c, float y[], const short N)
{
    short i;

    for (i = 0; i < N; i++)
    {
        y[i] = c * x[i];
    }

    return;
}


#ifdef NEURAL_QC
void MatrixMult(
    float **matrixA,
    float **matrixB,
    float **matrixOut,
    int16_t numRow,
    int16_t numColA,
    int16_t numColB
)
{
    for (int16_t i = 0; i < numRow; i++) {
        SetFloat(matrixOut[i], 0.0f, numColB);
    }

    for (int16_t i = 0; i < numRow; i++) {
        for (int16_t j = 0; j < numColB; j++) {
#ifndef GEMM_REFORM_ENC
            for (int16_t k = 0; k < numColA; k++) {
                matrixOut[i][j] += matrixA[i][k] * matrixB[k][j];
            }
#else
            // 8 part
            int16_t k = 0;
            float temp1 = 0.0;
            float temp2 = 0.0;
            float temp3 = 0.0;
            float temp4 = 0.0;
            for (k = 0; k < numColA - 7; k+= 8) {
                temp1 += matrixA[i][k] * matrixB[k][j];
                temp1 += matrixA[i][k + 1] * matrixB[k + 1][j];

                temp2 += matrixA[i][k + 2] * matrixB[k + 2][j];
                temp2 += matrixA[i][k + 3] * matrixB[k + 3][j];

                temp3 += matrixA[i][k + 4] * matrixB[k + 4][j];
                temp3 += matrixA[i][k + 5] * matrixB[k + 5][j];

                temp4 += matrixA[i][k + 6] * matrixB[k + 6][j];
                temp4 += matrixA[i][k + 7] * matrixB[k + 7][j];
            }

            float outTmp1, outTmp2;
            outTmp1 = temp1 + temp2;
            outTmp2 = temp3 + temp4;
            matrixOut[i][j] = outTmp1 + outTmp2;

            // tail part
            float temp = 0.0;
            for (; k < numColA; k++) {
                temp += matrixA[i][k] * matrixB[k][j];
            }
            matrixOut[i][j] = matrixOut[i][j] + temp;
#endif
        }
    }

    return;
}
#endif

#ifdef MC_ENABLE
void SetC(
    int8_t y[],
    const int8_t a,
    const short N
)
{
    short i;

    for (i = 0; i < N; i++)
    {
        y[i] = a;
    }

    return;
}

float Mean(
    const float *vec,
    const short lvec
)
{
    float tmp;

    tmp = SumFloat(vec, lvec) / (float)lvec;

    return tmp;
}

#endif