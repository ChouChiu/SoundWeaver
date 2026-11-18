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
#include <string.h>
#include "avs3_stat_enc.h"
#include "avs3_cnst_enc.h"
#include "avs3_cnst_com.h"
#include "avs3_prot_com.h"
#include "avs3_rom_com.h"


#ifdef SUPPORT_24BIT_INPUT
void ConvertBitDepth(int8_t* buf, short* data, const short bitDepth, const int32_t samples)
{
    int32_t i, j;
    short hb, lb;
    const short step = bitDepth >> 3;
    const short offset = (bitDepth == 16) ? 0 : 1;

    if (bitDepth == 16 || bitDepth == 24) {
        for (i = 0, j = 0; i < samples; i += step, j++) {

            lb = ((short)buf[i + offset]) & 0xFF;
            hb = ((short)buf[i + offset + 1]) << 8;
            data[j] = hb | lb;
        }
    } 
    else {
        fprintf(stderr, "not supported bit depth!\n");
    }

    return;
}
#endif

static void GetHoaInputChannels(AVS3EncoderHandle stAvs3, const short order) 
{
    switch (order)
    {
    case 0:
        stAvs3->numChansInput = 1;
        break;
    case 1:
        stAvs3->numChansInput = 4;
        break;
    case 2:
        stAvs3->numChansInput = 9;
        break;
    case 3:
        stAvs3->numChansInput = 16;
        break;
    case 4:
        stAvs3->numChansInput = 25;
        break;
    default:
        break;
    }
}


#ifdef BS_HEADER_COMPAT
static ChannelNumConfig GetMcChannelNumConfig(
    const char *mcConfigStr
)
{
    ChannelNumConfig channelNumConfig;

    for (int16_t i = 0; i < AVS3_SIZE_MC_CONFIG_TABLE; i++) {
        if (strcmp(mcConfigStr, mcChannelConfigTable[i].mcCmdString) == 0) {
            channelNumConfig = mcChannelConfigTable[i].channelNumConfig;
        }
    }

    return channelNumConfig;
}

static short GetMcNumChannels(
    const char *mcConfigStr
)
{
    short numChannels;

    for (int16_t i = 0; i < AVS3_SIZE_MC_CONFIG_TABLE; i++) {
        if (strcmp(mcConfigStr, mcChannelConfigTable[i].mcCmdString) == 0) {
            numChannels = mcChannelConfigTable[i].numChannels;
        }
    }

    return numChannels;
}

static ChannelNumConfig GetHoaChannelNumConfig(
    short hoaOrder
)
{
    if (hoaOrder == 1) {
        return CHANNEL_CONFIG_HOA_ORDER1;
    }
    else if (hoaOrder == 2) {
        return CHANNEL_CONFIG_HOA_ORDER2;
    }
    else if (hoaOrder == 3) {
        return CHANNEL_CONFIG_HOA_ORDER3;
    }

    return CHANNEL_CONFIG_UNKNOWN;
}
#endif


#ifdef AVS3_EXTENSION_WAV_READ
static short ChunkIdCompare(const int8_t* str1, const int8_t* str2, const short size)
{
    short i;
    for (i = 0; i < size; i++)
    {
        if (str1[i] != str2[i])
        {
            return AVS3_FALSE;
        }
    }

    return AVS3_TRUE;
}
#endif

static void Avs3WavReader(FILE  **fileInput)
{

#ifdef AVS3_EXTENSION_WAV_READ
    unsigned int tmp = 0;
    AVS3_WAVE_CHUNK_DATA chunkData;
    int8_t dataChunk[4] = { 'd','a','t','a' };
#endif

    if (*fileInput == NULL) 
    {
        fprintf(stderr, "Error: Can not read  wave file header\n");
        exit(-1);
    }

    AVS3_WAVE_HEADER_DATA waveHeader;

    fread(&waveHeader, sizeof(waveHeader), 1, *fileInput);

    fprintf(stderr, "Input sampling rate =   %d\n", waveHeader.sampleRate);
    fprintf(stderr, "Input Channels =        %d\n", waveHeader.numChannels);
    fprintf(stderr, "Input Bit depth =       %d\n", waveHeader.bitDepth);
    fprintf(stderr, "Input SubchunkSize =    %d\n", waveHeader.subchunkSize);

#ifdef AVS3_EXTENSION_WAV_READ
    /* extend RIFF subChunk */
    if (waveHeader.subchunkSize != 0x10)
    {
        /* seek to extend RIFF subChunk. */
        fseek(*fileInput, sizeof(waveHeader) - sizeof(chunkData), SEEK_SET);

        /* read extension size with 2 bytes. */
        fread(&tmp, 2, 1, *fileInput);

        /* skip extension size */
        fseek(*fileInput, tmp, SEEK_CUR);

        /* read extension RIFF chunk or data chunk. */
        fread(&chunkData, sizeof(chunkData), 1, *fileInput);

        /* data chunk only */
        if (ChunkIdCompare(chunkData.chunkID, dataChunk, sizeof(chunkData.chunkID) / sizeof(int8_t)))
        {
            return;
        }

        /* skip RIFF size */
        fseek(*fileInput, chunkData.chunkSize, SEEK_CUR);

        /* read wav header data chunk */
        fread(&chunkData, sizeof(chunkData), 1, *fileInput);

        /* data chunk */
        if (ChunkIdCompare(chunkData.chunkID, dataChunk, sizeof(chunkData.chunkID) / sizeof(int8_t)))
        {
            return;
        }
        else
        {
            fprintf(stderr, "AVS3 read wav header failed.\n");
            exit(-1);
        }
    }
#else
    /* extra bytes need to be skipped. */
    fseek(*fileInput, waveHeader.subchunkSize - FORMAL_SUBCHUNK_SIZE, SEEK_CUR);
#endif

    return;
}


// parse command line
/* Todo: Use safety function. */
void Avs3EncoderGetCommandLine(
    AVS3EncoderHandle stAvs3, 
    int argc, 
    char *argv[], 
    FILE  **fileInput, 
    FILE  **fileBitstream
#ifdef METADATA_DEVELOPE
    , FILE  **fileMetadata
#endif
) 
{
    short i = 1;
    int tmp;
    char strTmp[AVS3_FILENAME_MAX];

#ifdef MIX_EXT
    // init codec st params
    stAvs3->numObjsInput = 0;
    stAvs3->bitratePerObj = 0;
    stAvs3->bitrateBedMc = 0;
    stAvs3->soundBedType = 0;
    stAvs3->isMixedContent = 0;

    stAvs3->hasLfe = 1;             // init to 1 for most cases
#endif

#ifdef SUPPORT_NNTYPE_LC
    stAvs3->nnTypeConfig = NN_TYPE_DEFAULT_MAIN;
#endif

#ifdef IMPR_MIX_BIT_ALLOC
    // for MC/Mix mode, init silence detect params
    stAvs3->enableSilDetect = 1;
    stAvs3->silThrehold = -95.0f;
#endif

    while (i < argc - 4)
    {
        // bandwidth info
        if (strcmp(toUpper(argv[i]), "-MAX_BAND") == 0)
        {
            strncpy(strTmp, argv[i + 1], sizeof(strTmp));

            if (strcmp(toUpper(strTmp), "-SWB") == 0)
            {
                stAvs3->bwidth = SWB;
            }
            else if (strcmp(toUpper(strTmp), "-FB") == 0 || strcmp(toUpper(strTmp), "FB") == 0)
            {
                stAvs3->bwidth = FB;
            }

            i += 2;
        }
        // bit depth info
        else if (strcmp(toUpper(argv[i]), "-BITDEPTH") == 0) 
        {
            i++;
            if (i < argc - 4)
            {
                if (sscanf(argv[i], "%d", &tmp) > 0)
                {
                    i++;
                }
            }
            stAvs3->bitDepth = (short)tmp;
        }
        // mono mode
        else if (strcmp(toUpper(argv[i]), "-MONO") == 0)
        {
            // Todo:
            i++;
            stAvs3->numChansInput = 1;
            stAvs3->avs3CodecFormat = AVS3_MONO_FORMAT;
            stAvs3->avs3CodecCore = AVS3_MDCT_CORE;
#ifdef BS_HEADER_COMPAT
            stAvs3->channelNumConfig = CHANNEL_CONFIG_MONO;
#endif
        }
        // stereo mode
        else if (strcmp(toUpper(argv[i]), "-STEREO") == 0) 
        {
            i++;
            stAvs3->numChansInput = 2;
            stAvs3->avs3CodecFormat = AVS3_STEREO_FORMAT;
            stAvs3->avs3CodecCore = AVS3_MDCT_CORE;
#ifdef BS_HEADER_COMPAT
            stAvs3->channelNumConfig = CHANNEL_CONFIG_STEREO;
#endif
        }
        // MC mode
        else if (strcmp(toUpper(argv[i]), "-MC") == 0)
        {
#ifdef MC_ENABLE
            i++;
            stAvs3->avs3CodecFormat = AVS3_MC_FORMAT;
            stAvs3->avs3CodecCore = AVS3_MDCT_CORE;

#ifdef BS_HEADER_COMPAT
            if (i < argc - 4) {
                stAvs3->channelNumConfig = GetMcChannelNumConfig(argv[i]);
                stAvs3->numChansInput = GetMcNumChannels(argv[i]);

                i++;
            }
#endif

#ifdef IMPR_MIX_BIT_ALLOC
            // for MC 4.0 config, no LFE channel
            if (stAvs3->channelNumConfig == CHANNEL_CONFIG_MC_4_0) {
                stAvs3->hasLfe = 0;
            }
#endif

#endif
        }
        // HOA mode
        else if (strcmp(toUpper(argv[i]), "-HOA") == 0)
        {
            i++;
            stAvs3->avs3CodecFormat = AVS3_HOA_FORMAT;
            stAvs3->avs3CodecCore = AVS3_MDCT_CORE;

            if (i < argc - 4)
            {
                if (sscanf(argv[i], "%d", &tmp) > 0)
                {
                    i++;
                }
            }

#ifdef BS_HEADER_COMPAT
            stAvs3->channelNumConfig = GetHoaChannelNumConfig(tmp);
#endif

            GetHoaInputChannels(stAvs3, tmp);
        }
#ifdef MIX_DEVELOPE
        // Mix mode
        else if (strcmp(toUpper(argv[i]), "-MIX") == 0) {
            i++;

#ifndef MIX_EXT
            stAvs3->avs3CodecFormat = AVS3_MIX_FORMAT;
            stAvs3->avs3CodecCore = AVS3_MDCT_CORE;

            if (i < argc - 4) {
                if (sscanf(argv[i], "%d", &tmp) > 0) {
                    i++;
                }
            }
            stAvs3->numChansInput = tmp;

            if (i < argc - 4) {
                if (sscanf(argv[i], "%d", &tmp) > 0) {
                    i++;
                }
            }
            stAvs3->numObjsInput = tmp;

            if (i < argc - 4)
            {
                if (stAvs3->numObjsInput > 0)
                {
                    if ((*fileMetadata = fopen(argv[i], "rb")) == NULL)
                    {
                        fprintf(stderr, "Error: input metadata file %s could not be opened\n\n", argv[i]);
                        exit(-1);
                    }

                    fprintf(stdout, "Metadata  file:       %s\n", argv[i]);
                }

                i++;
            }
#else
            stAvs3->isMixedContent = 1;
            stAvs3->avs3CodecCore = AVS3_MDCT_CORE;

            // sound bed type
            if (i < argc - 4) {
                if (sscanf(argv[i], "%d", &tmp) > 0) {
                    i++;
                }
            }
            stAvs3->soundBedType = (short)tmp;

            // for different sound bed type
            if (stAvs3->soundBedType == 0) {

                // Only objs, no sound bed
                // number of input objs
                if (i < argc - 4) {
                    if (sscanf(argv[i], "%d", &tmp) > 0) {
                        i++;
                    }
                }
                stAvs3->numObjsInput = (short)tmp;

                // total input channels, equal to number of input objs
                stAvs3->numChansInput = stAvs3->numObjsInput;

                // bitrate for each obj
                if (i < argc - 4) {
                    if (sscanf(argv[i], "%d", &tmp) > 0) {
                        i++;
                    }
                }
                stAvs3->bitratePerObj = tmp;

                // get codec format from num objs
                if (stAvs3->numObjsInput == 1) {
                    stAvs3->avs3CodecFormat = AVS3_MONO_FORMAT;
                }
                else if (stAvs3->numObjsInput == 2) {
                    stAvs3->avs3CodecFormat = AVS3_STEREO_FORMAT;
                }
                else if (stAvs3->numObjsInput >= 3) {
                    stAvs3->avs3CodecFormat = AVS3_MC_FORMAT;
                }

                // channelNumConfig not used for pure objs
                stAvs3->channelNumConfig = CHANNEL_CONFIG_UNKNOWN;

                // total bitrate, only objs
                stAvs3->totalBitrate = stAvs3->numObjsInput * stAvs3->bitratePerObj;

                // for pure objs, lfe not exist
                stAvs3->hasLfe = 0;
            }
            else if (stAvs3->soundBedType == 1) {

                // MC+objs
                // sound bed config
                if (i < argc - 4) {
                    stAvs3->channelNumConfig = GetMcChannelNumConfig(argv[i]);
                    stAvs3->numChansInput = GetMcNumChannels(argv[i]);

                    i++;
                }

                // bitrate for sound bed
                if (i < argc - 4) {
                    if (sscanf(argv[i], "%d", &tmp) > 0) {
                        i++;
                    }
                }
                stAvs3->bitrateBedMc = tmp;

                // number of input objs
                if (i < argc - 4) {
                    if (sscanf(argv[i], "%d", &tmp) > 0) {
                        i++;
                    }
                }
                stAvs3->numObjsInput = (short)tmp;

                // bitrate for each obj
                if (i < argc - 4) {
                    if (sscanf(argv[i], "%d", &tmp) > 0) {
                        i++;
                    }
                }
                stAvs3->bitratePerObj = tmp;

                // total input channels, MC numCh + numObjs
                stAvs3->numChansInput += stAvs3->numObjsInput;

                // total bitrate, sound bed and objs together
                stAvs3->totalBitrate = stAvs3->bitrateBedMc + stAvs3->numObjsInput * stAvs3->bitratePerObj;

                // get codec format
                stAvs3->avs3CodecFormat = AVS3_MC_FORMAT;

                // for sound bed + obj mix
#ifndef IMPR_MIX_BIT_ALLOC
                // if sound bed is stereo, no LFE, if sound bed is mc, with lfe
                if (stAvs3->channelNumConfig == CHANNEL_CONFIG_STEREO) {
#else
                // if sound bed is stereo/MC4.0, no LFE, if sound bed is other mc configs, with lfe
                if (stAvs3->channelNumConfig == CHANNEL_CONFIG_STEREO ||
                    stAvs3->channelNumConfig == CHANNEL_CONFIG_MC_4_0) {
#endif
                    stAvs3->hasLfe = 0;
                }
                else {
                    stAvs3->hasLfe = 1;
                }
            }
#endif
        }
#endif
#ifdef METADATA_EXT
        // Metadata file
        else if (strcmp(toUpper(argv[i]), "-META_FILE") == 0) {
            i++;

            // open metadata file
            if ((*fileMetadata = fopen(argv[i], "rb")) == NULL) {
                fprintf(stderr, "Error: input metadata file %s could not be opened\n\n", argv[i]);
                exit(-1);
            }
            i++;
        }
#endif
#ifdef SUPPORT_NNTYPE_LC
        // Neural network model config
        else if (strcmp(toUpper(argv[i]), "-NN_TYPE") == 0) {
            i++;

            if (sscanf(argv[i], "%d", &tmp) > 0) {
                stAvs3->nnTypeConfig = (NnTypeConfig)tmp;
                i++;
            }
        }
#endif
#ifdef IMPR_MIX_BIT_ALLOC
        // For MC/Mix mode, set silence detect enable flag and silence threshold
        else if (strcmp(toUpper(argv[i]), "-ENABLE_SIL") == 0) {
            i++;

            if (sscanf(argv[i], "%d", &tmp) > 0) {
                stAvs3->enableSilDetect = (short)tmp;
                i++;
            }

            if (stAvs3->enableSilDetect == 1) {
                stAvs3->silThrehold = (float)atof(argv[i]);
                stAvs3->silThrehold = AVS3_MIN(stAvs3->silThrehold, -65);
                i++;
            }
        }
#endif
        else 
        {
            // Todo:
        }
    }

    // get bitrate
    if (i < argc - 2)
    {
        if (sscanf(argv[i], "%d", &tmp) == 1) {
#ifndef MIX_EXT
            stAvs3->totalBitrate = tmp;
#else
            // for mix mode, total bitrate is get previously
            // for single content, get from here
            if (stAvs3->isMixedContent == 0) {
                stAvs3->totalBitrate = tmp;
            }
#endif
        }
        else 
        {
            // Todo
        }

        // Todo: Check input bitrate if is supported!

        i++;
    }

    // get input sampling rate
    if (i < argc - 2)
    {
#ifndef SUPPORT_MORE_FS
        stAvs3->inputFs = (long)atoi(argv[i]) * 1000;
#else
        stAvs3->inputFs = (long)(atof(argv[i]) * 1000);
#endif

        // Todo: Check input sampling frequency if is supported!
        
        stAvs3->frameLength = GetFrameLength(stAvs3->inputFs);

        i++;
    }
    else
    {
        fprintf(stderr, "Error: no input sampling frequency specified\n\n");
    }

    /* Input file */
    if (i < argc - 1)
    {
        const char* strSuffix = ".wav";
        const char* startPtr = strrchr(argv[i], '.');

        if ((*fileInput = fopen(argv[i], "rb")) == NULL)
        {
            fprintf(stderr, "Error: input audio file %s could not be opened\n\n", argv[i]);
            exit(-1);
        }
        fprintf(stdout, "Input audio file:       %s\n", argv[i]);
        
        if (strncmp(startPtr, strSuffix, strlen(strSuffix)) == 0)
        {
            fprintf(stdout, "Input audio file type:  WAVE\n");

            Avs3WavReader(fileInput);
        }
        else 
        {
            fprintf(stdout, "Input audio file type:  PCM\n");
            fprintf(stderr, "Input sampling rate =   %d\n", stAvs3->inputFs);
            fprintf(stderr, "Input Channels =        %d\n", stAvs3->numChansInput);
        }
        i++;
    }
    else
    {
        fprintf(stderr, "Error: no input file specified\n\n");
    }

    /* Bitstream */
    if (i < argc)
    {
        if ((*fileBitstream = fopen(argv[i], "wb")) == NULL)
        {
            fprintf(stderr, "Error: output bitstream file %s could not be opened\n\n", argv[i]);
        }

        fprintf(stdout, "Output bitstream file:  %s\n", argv[i]);
        i++;
    }
    else
    {
        fprintf(stderr, "Error: no output bitstream file specified\n\n");
    }

    return;
}
