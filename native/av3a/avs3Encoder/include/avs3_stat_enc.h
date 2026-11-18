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

#ifndef AVS3_STAT_ENC_H
#define AVS3_STAT_ENC_H

#include "avs3_options.h"
#include "avs3_cnst_com.h"
#include "avs3_cnst_enc.h"
#include "avs3_stat_com.h"

#ifdef METADATA_EXT
#include "avs3_stat_meta.h"
#endif

// structure for individual bitstream indice
typedef struct avs3_indice_structure {
    uint16_t value;
    int16_t nBits;
}Indice;

// encoder side bitstream handle
typedef struct avs3_bitstream_enc_structure {
    int32_t numBitsTot;
    Indice *indiceList;
    int16_t nextInd;
    int16_t lastInd;
}AVS3_BSTREAM_ENC_DATA, *AVS3_BSTREAM_ENC_HANDLE;

#ifdef METADATA_EXT
// metadata encoder structure
typedef struct Avs3MetaDataEncStructure {
    Avs3MetaData avs3MetaData;
    AVS3_BSTREAM_ENC_HANDLE bsHandle;
}Avs3EncMetaData, *Avs3EncMetaDataHandle;
#endif

/* window type detector data structure */
typedef struct WindowTypeDetectStructure {

    float blockEnergy[NUM_BLOCKS];
    float thresholdEnergy[NUM_BLOCKS + 1];
    float hpHistory[HP_ORDER];
    short preIsTransient[NUM_TRANS_HIST];
    short blockSize;
}WindowTypeDetectData;

#ifdef BWE_DEVELOPE
/* BWE encoder side data structure */
typedef struct BweEncDataStructure {

    float sfbEnvelope[MAX_NUM_SFB_BWE];                             // quantized sfb envelope
    int16_t sfbEnvQIdx[MAX_NUM_SFB_BWE];                            // sfb envelope quantization index
    int16_t whiteningLevel[MAX_NUM_TILE];                           // tile whitening level

    int16_t prevWhiteningLevel[LEN_HIST_WHITENING][MAX_NUM_TILE];   // history of whitening level

}BweEncData, *BweEncDataHandle;
#endif

/* AVS3 encoder single channel abstract */
typedef struct avs3_enc_core_structure
{
    /* frame length */
    short frameLength;
    short lookaheadSamples;

    /* input signal */
    float signalBuffer[FRAME_LEN + FRAME_LEN + N_SAMPLES_LOOKAHEAD];
    float* inputSignal;
    float* lookahead;

    float synthBuffer[MAX_OVL_SIZE];

    /* original spectrum */
    float origSpectrum[FRAME_LEN];

    /* 48kHz windowing signal for LPC */
    float winSignal48kHz[BLOCK_LEN_LONG + BLOCK_LEN_LONG];

    // window type detection handle
    WindowTypeDetectData winTypeDetector;

    /* transform type */
    short transformType;
    short lastTransformType;

    /* Mdct grouping info */
    short numGroups;
    short groupIndicator[NUM_BLOCKS];

#ifdef FD_SHAPING
    /* FD spectrum shaping */
    float memPreemph;                       // preemphsize filter memory in lpc calculation
    float oldLsp[LPC_ORDER];                // unquantized lsp of last frame
    float lsfQ[LPC_ORDER];                  // quantized lsf
    short lsfVqIndex[LSF_CB_NUM_HBR];       // lsf VQ indices, max LSF_CB_NUM_HBR index
    short lsfLbrFlag;                       // low bitrate flag for LSF quantization
#endif

#ifdef TD_SHAPING
    /* TNS handle */
    TnsData tnsData;
#endif

#ifdef BWE_DEVELOPE
    /* BWE */
    int16_t bwePresent;                     // bwe present flag
    BweConfigData bweConfig;                // bwe config info
    BweEncData bweEncData;                  // bwe encoding data

#ifdef DEBUG_NEURAL_QC_LOCAL_SYNTH
    BweDecData bweDecData;
#endif
#endif

#ifdef NEURAL_QC
    /* Neural QC data */
    NeuralQcData neuralQcData;
#endif

    uint16_t coreSideBits;

    /* bitstream handle */
    AVS3_BSTREAM_ENC_HANDLE bsHandle;

    AVS3_CORE_CONFIG_DATA_HANDLE hCoreConfig;

}AVS3_ENC_CORE_DATA, *AVS3_ENC_CORE_HANDLE;

typedef struct avs3_hoa_enc_pair_structure
{
    short ch1;
    short ch2;
    short chIdx;
    short ms;

#ifdef AVS3_HOA_BUG_FIXED
    short sfbMask[N_SFB_HOA_LBR];
#endif

} HOA_DMX_INFO;

typedef struct avs3_hoa_point_structure
{
    short idx;
    float value;
} HOA_Point;

typedef struct avs3_hoa_enc_data_structure
{
    short numVote;
    short basisIdx[MAX_HOA_BASIS];
    short lastBasisIdx[MAX_HOA_BASIS];

    float hoaSignalBuffer[MAX_HOA_CHANNELS][HOA_LEN_TRANSFORM];
    float origSpecturm[MAX_HOA_CHANNELS][HOA_LEN_FRAME48k];
    float synthBuffer[MAX_HOA_CHANNELS][HOA_OVERLAP_SIZE];

#ifdef AVS3_HOA_VL_SELECTION
    short numSource;
    float frameVote[L_HOA_BASIS_COLS];
    float nrgRatioMax;
    float direcSignalNrgRatio;
    short basisIdxMax[MAX_HOA_BASIS];
#endif

    /* DMX info */
    short bitsRatio[MAX_HOA_DMX_GROUPS][MAX_HOA_DMX_CHANNELS];
    short groupILD[MAX_HOA_DMX_CHANNELS];
#ifndef HOA_ILD_CBQUANT
    short flagNrg[MAX_HOA_DMX_CHANNELS];
#endif
    short pairIdx[MAX_HOA_DMX_GROUPS];
    short lastPairIdx[MAX_HOA_DMX_GROUPS];
    short pairFlag[MAX_HOA_DMX_CHANNELS];
    short groupBitsRatio[MAX_HOA_DMX_GROUPS];
    HOA_DMX_INFO dmxInfo[MAX_HOA_DMX_GROUPS][MAX_HOA_DMX_CHANNELS / 2];

    float* coreSpectrum[MAX_HOA_CHANNELS];
    short transformType[MAX_HOA_CHANNELS];

#ifdef AVS3_HOA_FULL_SUPPORT
    /* scene type */
    short sceneType;
    short numVL;
#endif

    HOA_Point points[HOA_LEN_FRAME48k];

    /* Bitstream for HOA */
    AVS3_BSTREAM_ENC_HANDLE bsHandle;

    AVS3_HOA_CONFIG_DATA_HANDLE hHoaConfig;

} AVS3_HOA_ENC_DATA, *AVS3_HOA_ENC_DATA_HANDLE;

/* AVS3 mono structure */
typedef struct avs3_mono_mdct_data_structure
{
    float* mdctSpectrum;

}AVS3_MONO_ENC_DATA, *AVS3_MONO_ENC_HANDLE;

/* AVS3 MDCT Stereo structure */
typedef struct avs3_stereo_mdct_data_structure
{
    float* mdctSpectrum[STEREO_CHANNELS];

    short stereoSideBits;

    short isMS;
    short bitsRatio;
    short ILD;

#ifdef MCR_INTEGRATE
    // for mcr mode in low bitrate
    int16_t useMcr;
    MCR_CONFIG mcrConfig;
    MCR_DATA mcrData;
#endif

    /* Bitstream handle */
    AVS3_BSTREAM_ENC_HANDLE bsHandle;

}AVS3_STEREO_MDCT_ENC_DATA, *AVS3_STEREO_ENC_HANDLE;


#ifdef MC_ENABLE
#ifdef IMPR_MIX_BIT_ALLOC
/* Mix mode bit allocation strategy */
typedef enum {
    MIX_ALLOC_STRATEGY_INTERNAL = 0,
    MIX_ALLOC_STRATEGY_OBJ2BED = 1
} mixAllocStrategyMode;
#endif

/* AVS3 MC ENC structure */
typedef struct avs3_mc_enc_data_structure
{
    float* mcSpectrum[MAX_CHANNELS];
    short mcSideBits;
    /* Bitstream handle */
    AVS3_BSTREAM_ENC_HANDLE bsHandle;

    short *frameType[MAX_CHANNELS];                         // Long Frame or short Frame
    short channelNum;                                       // channel num
    short coupleChNum;                                      // channel num without LFE
#ifdef IMPR_MIX_BIT_ALLOC
    short isMixed;                                          // 1 for bed+obj or pure obj, 0 for only sound bed
    short objNum;                                           // object number
    float bedBitsRatio;                                     // bed bits ratio in the whole bits
    short mixAllocStrategy;                                 // mix mode bit allocation strategy, 0 for internel, 1 for obj2bed
    short hasSilFlag;                                       // set to 0 if no silence frame in all channels
    short silFlag[MAX_CHANNELS];                            // silFlag for each channel
#endif
    short lfeChIdx;                                         // LFE channel idx
    short lfeExist;                                         // if have LFE channel
    short lfeBytes;                                         // LFE allocated bytes per frame
    unsigned short mcIld[MAX_CHANNELS];                     // ILD parameter , bs used
#ifndef MC_ILD_CBQUANT
    short scaleFlag[MAX_CHANNELS];                          // ILD parameter , bs used
#endif
    short pairCnt;                                          // pairing num , bs used
    short bitsPairIndex;                                    // bits in bitstream for Pair index
    AVS3_MC_PAIR_DATA hPair[MAX_CHANNELS / 2];              // pairing structure , bs use index

}AVS3_MC_ENC_DATA, *AVS3_MC_ENC_HANDLE;
#endif


/* Main encoder abstract */
typedef struct avs3_main_encoder_structure
{
    short initFrame;                                // flag for first frame

    long  inputFs;                                  // input sample rate
    short bitDepth;                                 // bit depth or resolution of audio signal, 16/24
    long  totalBitrate;                             // total bitrate
    long  lastTotalBrate;                        
#ifdef BS_HEADER_COMPAT
    ChannelNumConfig channelNumConfig;              // channel number config, for bitrate table selection
#endif
    short numChansInput;                            
#ifdef MIX_DEVELOPE
    short numObjsInput;                             // number of input object channels, only for mix mode
#ifdef MIX_EXT
    long bitratePerObj;                             // bitrate for each obj
    long bitrateBedMc;                              // bitrate for mc sound bed
    short soundBedType;                             // type of sound bed, 0 for none (only objs), 1 for mc or hoa
    short isMixedContent;                           // flag for mixed content, i.e. with objects
    short hasLfe;                                   // LFE flag, for mixed content: pure obj or stereo soundbed, no LFE
#endif
#endif

#ifdef IMPR_MIX_BIT_ALLOC
    short enableSilDetect;                          // for MC/Mix mode, 1 for enable silence frame detect
    float silThrehold;                              // for MC/Mix mode, silence frame threshold in dB
#endif

    short avs3CodecFormat;
    short avs3CodecCore;                                 
    short bwidth;
    short frameLength;
    short lookaheadSamples;
#ifndef SUPPORT_HIGH_BR_MIX
    short bitsPerFrame;
#else
    int32_t bitsPerFrame;
#endif

#ifdef SUPPORT_NNTYPE_LC
    NnTypeConfig nnTypeConfig;                      // neural network model type config
#endif

#ifdef NEURAL_QC
    /* Neural QC model type and handles */
    TypeModel modelType;
    NeuralCodecHandle baseCodecSt;
    NeuralCodecHandle contextCodecSt;
#endif

    /* Bitstream indice list for all channels and side info */
    Indice bsIndiceList[MAX_NUM_BS_PARTS][MAX_NUM_INDICES];

    /* Bitstream buffer */
    uint8_t bitstream[MAX_BS_BYTES];

    /* Payload size */
    uint32_t totalSideBits;

    /* Core encoder abstract */
    AVS3_ENC_CORE_HANDLE hEncCore[MAX_CHANNELS];

    /* AVS3 HOA encoder handle */
    AVS3_HOA_ENC_DATA_HANDLE hEncHoa;

#ifdef MC_ENABLE
    /* AVS3 Multi Channel encoder handle */
    AVS3_MC_ENC_HANDLE hMcEnc;

#ifdef DEBUG_NEURAL_QC_LOCAL_SYNTH
    /* AVS3 Multi Channel local decoder handle */
    AVS3_MC_DEC_HANDLE hMcDec;
#endif
#endif

    /* AVS3 Stereo encoder handle */
    AVS3_STEREO_ENC_HANDLE hMdctStereo;

    /* AVS3 Mono encoder handle */
    AVS3_MONO_ENC_HANDLE hMono;

#ifdef METADATA_EXT
    /* Metadata */
    Avs3EncMetaDataHandle hAvs3MetaData;
#endif

} AVS3Encoder, *AVS3EncoderHandle;


#endif // AVS3_STAT_ENC_H
