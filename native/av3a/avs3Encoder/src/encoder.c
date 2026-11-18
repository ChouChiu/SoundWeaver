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
#include "avs3_cnst_com.h"
#include "avs3_stat_enc.h"
#include "avs3_prot_enc.h"
#include "avs3_extern_encoder.h"

#ifdef DEBUG_META
extern FILE *fori = NULL;
#endif


int avs3_encoder(int argc, char* argv[])
{
    static long frame = 1;
#ifdef SUPPORT_24BIT_INPUT
    int32_t n;
#else
    short n;
#endif

    FILE* fileInput = NULL;
    FILE* fileBitstream = NULL;
#ifdef NEURAL_QC
    FILE *fModel = NULL;
#endif
#ifdef METADATA_DEVELOPE
    FILE* fileMetadata = NULL;
#endif
#ifdef DEBUG_META
    fori = fopen("log_enc.txt", "w");
#endif
    AVS3EncoderHandle stAvs3 = NULL;
#ifdef SUPPORT_24BIT_INPUT
    int8_t buf[MAX_CHANNELS*MAX_FRAME_LEN * 3];         // 3 for 24bit, 3bytes
#endif
    short data[MAX_CHANNELS*MAX_FRAME_LEN];
    short samplesLookahead;
    short numChansInput;

    if ((stAvs3 = (AVS3EncoderHandle)malloc(sizeof(AVS3Encoder))) == NULL)
    {
        fprintf(stderr, "Can not allocate memory for AVS3 encoder structure!\n");
        exit(-1);
    }

#ifdef METADATA_DEVELOPE
    // command line analysis
    Avs3EncoderGetCommandLine(stAvs3, argc, argv, &fileInput, &fileBitstream, &fileMetadata);
#else
    // command line analysis
    Avs3EncoderGetCommandLine(stAvs3, argc, argv, &fileInput, &fileBitstream);
#endif

    // encoder init
    Avs3EncoderInit(stAvs3, &fModel);
   
    samplesLookahead = stAvs3->lookaheadSamples;
    numChansInput = stAvs3->numChansInput;
#ifndef SUPPORT_24BIT_INPUT
    while ((n = (short)fread(data, sizeof(short), (stAvs3->frameLength + samplesLookahead)*numChansInput, fileInput)) > 0)
#else
    while ((n = (int32_t)fread(buf, sizeof(int8_t), (stAvs3->frameLength + samplesLookahead)*numChansInput*(stAvs3->bitDepth >> 3), fileInput)) > 0)
#endif
    {
        fprintf(stdout, "%-8ld\b\b\b\b\b\b\b\b", frame);

#ifdef SUPPORT_24BIT_INPUT
        ConvertBitDepth(buf, data, stAvs3->bitDepth, n);
#endif

#ifdef METADATA_DEVELOPE
        /* metadata encoding */
#ifndef METADATA_EXT
        Avs3MetadataEnc(stAvs3, &fileMetadata);
#else
        Avs3MetadataEnc(stAvs3, fileMetadata);
#endif
#endif

        // frame level encoding
#ifndef SUPPORT_24BIT_INPUT
        Avs3Encode(stAvs3, data, n);
#else
        Avs3Encode(stAvs3, data, n / (stAvs3->bitDepth / 8));       // convert to nSamples
#endif

        /* Write indices to file */
        Avs3FlushBitstream(stAvs3, fileBitstream);

        samplesLookahead = 0;

        frame++;
    }

    fprintf(stdout, "\n\n");
    fprintf(stdout, "AVS3 Encoder finished...\n\n");

    if (fileInput != NULL) 
    {
        fclose(fileInput);
    }

    if (fileBitstream != NULL) 
    {
        fclose(fileBitstream);
    }

#ifdef METADATA_DEVELOPE
    if (fileMetadata != NULL)
    {
        fclose(fileMetadata);
    }
#endif

#ifdef DEBUG_META
    fclose(fori);
#endif

    if (fModel != NULL) 
    {
        fclose(fModel);
    }

    // destroy encoder handle
    Avs3EncoderDestroy(stAvs3);

    return 0;
}