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

#ifndef AVS3_PROT_ENC_H
#define AVS3_PROT_ENC_H

#include <stdio.h>

#include "avs3_options.h"
#include "avs3_stat_enc.h"

void Avs3EncoderGetCommandLine(
    AVS3EncoderHandle stAvs3, 
    int argc, 
    char *argv[], 
    FILE  **fileInput, 
    FILE  **fileBitstream
#ifdef METADATA_DEVELOPE
    , FILE  **fileMetadata
#endif
);

#ifdef SUPPORT_24BIT_INPUT
void ConvertBitDepth(int8_t* buf, short* data, const short bitDepth, const int32_t samples);
#endif

void Avs3EncoderInit(AVS3EncoderHandle stAvs3,FILE** fModel);

void Avs3EncCreateStereo(AVS3EncoderHandle stAvs3);

void Avs3EncCreateMono(AVS3EncoderHandle stAvs3);

void Avs3Encode(AVS3EncoderHandle stAvs3, const short *data, const short samples);

void Avs3CoreEncode(AVS3EncoderHandle stAvs3, float data[MAX_CHANNELS][MAX_FRAME_LEN], const short lenFrame, const short nChans);

void Avs3EncoderDestroy(AVS3EncoderHandle stAvs3);

void Avs3PreAnalysis(AVS3EncoderHandle stAvs3, const short nChans, const short LenFrame);

#ifdef IMPR_MIX_BIT_ALLOC
void McMixGetSilenceFlag(
    AVS3EncoderHandle stAvs3,
    const short nChans,
    const short lenFrame);
#endif

// trans detection functions
void InitWindowTypeDetect(const short frameLength, WindowTypeDetectData *winTypeDetector);

int16_t WindowTypeDetect(WindowTypeDetectData *winTypeDetector, float const * inPut, const short frameLength, const short initFrame);

void CoreSignalAnalysis(AVS3EncoderHandle stAvs3, const short nChans, const short lenFrame);

void Avs3LocalDecoder(AVS3_ENC_CORE_HANDLE hEncCore, float output[BLOCK_LEN_LONG]);

// HOA functions
void Avs3HOAEncoder(AVS3EncoderHandle stAvs3, float data[MAX_CHANNELS][MAX_FRAME_LEN], const short lenFrame);

void Avs3HOAReconfig(AVS3EncoderHandle stAvs3,short* nChans);

void Avs3EncCreateHoa(AVS3EncoderHandle stAvs3);

int Avs3HoaSVD(float a[HOA_LEN_FRAME48k][L_HOA_BASIS_ROWS], int m, int n, float* w, float v[HOA_LEN_FRAME48k][L_HOA_BASIS_ROWS]);

// stereo functions
void Avs3StereoEncoder(
    AVS3EncoderHandle stAvs3,
    short* channelBytes
);

#ifdef MCR_INTEGRATE
void Avs3StereoMcrEncoder(
    AVS3EncoderHandle stAvs3,
    short* channelBytes
);
#endif

#ifdef MONO_INTEGRATE
// mono functions
void Avs3MonoEncoder(
    AVS3EncoderHandle stAvs3,
    short* channelBytes
);
#endif

#ifdef MC_ENABLE
void Avs3EncCreateMc(
    AVS3EncoderHandle stAvs3
);

void Avs3McEncoder(
    AVS3EncoderHandle stAvs3,
    short* channelBytes
);
#endif

#ifdef MIX_DEVELOPE
void Avs3EncCreateMix(
    AVS3EncoderHandle stAvs3
);

void Avs3MixEncoder(
    AVS3EncoderHandle stAvs3,
    short* channelBytes
);
#endif

#ifdef METADATA_DEVELOPE
void Avs3MetadataEnc(
    AVS3EncoderHandle stAvs3, 
#ifndef METADATA_EXT
    FILE** fMetadata
#else
    FILE* fMetadata
#endif
);
#endif

#ifdef SIMULATING_HOA_DECODER
void LocalHoaCoreDec(AVS3_HOA_ENC_DATA_HANDLE hEncHoa, float output[MAX_HOA_CHANNELS][HOA_LEN_FRAME48k], const short lenFrame);

void LocalHoaPostSynthesisFilter(AVS3_HOA_ENC_DATA_HANDLE hDecHoa, const short frameLength);
#endif

void Avs3HoaCoreEncoder(AVS3EncoderHandle stAvs3, short* channelBytes);

#ifdef FD_SHAPING
/* LPC analysis and spectrum shaping */
void Avs3FdSpectrumShaping(
    AVS3_ENC_CORE_HANDLE hEncCore,
    int16_t chIdx
);

void LsfQuantEnc(
    const float *lsf,
    float *quantLsf,
    short *lsfVqIndex,
    const short lpcOrder,
    const short lsfLbrFlag
);
#endif

#ifdef BWE_DEVELOPE
// bwe functions
void InitBweEncData(
    BweEncDataHandle bweEncData
);

void BweApplyEnc(
    BweConfigHandle bweConfig,
    BweEncDataHandle bweEncData,
    float *mdctSpectrum,
    float *powerSpectrum,
    int16_t isLongWin
);
#endif

void Avs3FlushBitstream(AVS3EncoderHandle stAvs3, FILE *fBitstream);

void ResetIndicesEnc(AVS3_BSTREAM_ENC_HANDLE bsHandle, int16_t maxNumIndices);

void PushNextIndice(AVS3_BSTREAM_ENC_HANDLE bsHandle, uint16_t value, int16_t totalSideBits);

void IndicesToSerial(const Indice *indiceList, const int16_t numIndices, uint8_t *bitstream, uint32_t *bitstreamSize);

void WriteCoreSideBitstream(AVS3EncoderHandle stAvs3, int16_t nChans, uint8_t *bitstream, uint32_t *bitstreamSize);

void WriteGroupBitstream(
    AVS3EncoderHandle stAvs3,
    int16_t nChans,
    uint8_t *bitstream,
    uint32_t *bitstreamSize
);

void WriteQcBitstream(AVS3EncoderHandle stAvs3, int16_t nChans, uint8_t *bitstream, uint32_t *bitstreamSize);

void WriteStereoBitstream(AVS3EncoderHandle stAvs3, uint8_t *bitstream, uint32_t *bitstreamSize);

#ifdef MC_ENABLE
void WriteMcBitstream(
    AVS3EncoderHandle stAvs3, 
    uint8_t *bitstream, 
    uint32_t *bitstreamSize, 
    short chBitRatios[MAX_CHANNELS]
);
#endif

void WriteHoaBitstream(AVS3EncoderHandle stAvs3);

#endif // AVS3_PROT_ENC_H
