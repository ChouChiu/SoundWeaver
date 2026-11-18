// macOS bridge for the decoder used by nilaoda/av3a_decoder's reference workflow.
// stdout is an 8-byte (rate, channels) header followed by interleaved float PCM.
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <math.h>
#include <signal.h>
#include <errno.h>
#include <limits.h>
#include <spawn.h>
#include <unistd.h>
#include <sys/wait.h>
#ifdef __APPLE__
#include <mach-o/dyld.h>
#endif
#include "vivid_wav.h"
#include "avs3_rom_com.h"
#include "avs3_prot_com.h"
#include "avs3_prot_dec.h"

typedef struct {
    FILE *file;
    int packetSize, prefix, pid, continuity;
    uint8_t packet[204];
    size_t offset, end;
    long pesRemaining;
} Input;
typedef struct {
    int rate, config, channels, nn, bitrate, payload;
    int profile, bedChannels, objects, hoaOrder, headerBytes, crcOffset;
} Header;
typedef struct { const char *name; double azimuth, elevation; } Speaker;
static const Speaker speakers[] = {
    {"L",-30,0},{"R",30,0},{"C",0,0},{"LFE",0,0},
    {"Ls",-110,0},{"Rs",110,0},{"Lrs",-150,0},{"Rrs",150,0},
    {"Lw",-60,0},{"Rw",60,0},{"Ltf",-30,35},{"Rtf",30,35},
    {"Ltm",-90,45},{"Rtm",90,45},{"Ltr",-135,35},{"Rtr",135,35},
    {"Cs",180,0},{"Ts",0,90}
};
static char errorMessage[256];
static int fail(const char *text) { snprintf(errorMessage,sizeof(errorMessage),"%s",text); return -1; }
static unsigned bits(const uint8_t *p,int start,int count) {
    unsigned value=0; for(int i=0;i<count;i++) value=(value<<1)|((p[(start+i)/8]>>(7-(start+i)%8))&1); return value;
}
static int header(const uint8_t *p,Header *h) {
    if(bits(p,0,16)!=0xfff2 || bits(p,16,1)!=0) return fail("Invalid AVS3 audio sync/header.");
    int profile=bits(p,20,3), nn=bits(p,17,3), fs=bits(p,23,4);
    if(profile>2) return fail("Reserved Audio Vivid profile is unsupported.");
    int pos=35, config=-1, objects=0, bitrate=0, bed=0, hoaOrder=0;
    if(profile==2) {
        hoaOrder=bits(p,pos,4)+1; pos+=4;
        if(hoaOrder<1 || hoaOrder>3) return fail("Unsupported Audio Vivid HOA order (supported: 1..3).");
        config=(hoaOrder==1)?CHANNEL_CONFIG_HOA_ORDER1:(hoaOrder==2?CHANNEL_CONFIG_HOA_ORDER2:CHANNEL_CONFIG_HOA_ORDER3);
        bed=(hoaOrder+1)*(hoaOrder+1);
    } else {
        int soundBed=profile?bits(p,pos,2):1;
        if(profile) pos+=2;
        if(soundBed>1) return fail("Invalid Audio Vivid sound bed type.");
        if(soundBed) {
            config=bits(p,pos,7);pos+=7;
            if(config>10 || config==4 || config==5 || (profile && config==0))
                return fail("Unsupported Audio Vivid bed configuration.");
            bed=config==0?1:0;
            for(int i=0;i<AVS3_SIZE_MC_CONFIG_TABLE;i++)
                if(mcChannelConfigTable[i].channelNumConfig==config) bed=mcChannelConfigTable[i].numChannels;
            if(profile) {
                bitrate=(int)codecBitrateConfigTable[config].bitrateTable[bits(p,pos,4)];pos+=4;
                if(bitrate<=0) return fail("Unsupported Audio Vivid bed bitrate.");
            }
        }
        if(profile) {
            objects=bits(p,pos,7)+1;pos+=7;
            int objectBitrate=(int)codecBitrateConfigTable[0].bitrateTable[bits(p,pos,4)];pos+=4;
            if(objectBitrate<=0) return fail("Unsupported Audio Vivid object bitrate.");
            bitrate+=objects*objectBitrate;
        }
    }
    int resolution=bits(p,pos,2);pos+=2;
    if(profile!=1) {bitrate=(int)codecBitrateConfigTable[config].bitrateTable[bits(p,pos,4)];pos+=4;}
    if(nn>1 || (fs!=2 && fs!=3) || resolution>2 || bitrate<=0)
        return fail("Unsupported Audio Vivid configuration (44.1/48 kHz MAIN/LC audio is supported).");
    int channels=bed+objects;
    if(channels<1 || channels>MAX_CHANNELS) return fail("Invalid Audio Vivid channel count.");
    int headerBytes=(pos+8+7)/8;
    int rate=(int)avs3SamplingRateTable[fs];
    *h=(Header){.rate=rate,.config=config,.channels=channels,.nn=nn,.bitrate=bitrate,
        .payload=(int)ceilf(((int)((float)bitrate/rate*1024)-headerBytes*8)/8.0f),
        .profile=profile,.bedChannels=bed,.objects=objects,.hoaOrder=hoaOrder,.headerBytes=headerBytes,.crcOffset=pos};
    if(h->payload<1 || h->payload>MAX_BS_BYTES) return fail("Invalid Audio Vivid frame length.");
    return 0;
}
// FFmpeg only demuxes MP4 packets; AVS3 decoding and source-channel handling stay in this process.
// Use argv directly, so spaces, parentheses and shell characters in filenames are safe.
static volatile sig_atomic_t demuxPID;
static void stop_demux_signal(int sig) {
    if(demuxPID>0) kill((pid_t)demuxPID,SIGTERM);
    _exit(128+sig);
}
static int demux_mp4(Input *in,const char *path,int firstFrameOnly) {
    char ffmpeg[PATH_MAX];
#ifdef __APPLE__
    char executable[PATH_MAX],resolved[PATH_MAX];
    uint32_t size=sizeof(executable);
    if(_NSGetExecutablePath(executable,&size)!=0 || !realpath(executable,resolved))
        return fail("Cannot locate bundled MP4 demuxer.");
    char *slash=strrchr(resolved,'/');if(!slash)return fail("Invalid decoder path.");*slash=0;
    if(snprintf(ffmpeg,sizeof(ffmpeg),"%s/../OpenJOC/ffmpeg",resolved)>=(int)sizeof(ffmpeg))
        return fail("MP4 demuxer path is too long.");
#else
    const char *env_ffmpeg=getenv("AV3A_FFMPEG_BIN");
    snprintf(ffmpeg,sizeof(ffmpeg),"%s",(env_ffmpeg && env_ffmpeg[0])?env_ffmpeg:"ffmpeg");
#endif
    int fds[2];if(pipe(fds)<0)return fail("Cannot create MP4 demux pipe.");
    posix_spawn_file_actions_t actions;posix_spawn_file_actions_init(&actions);
    posix_spawn_file_actions_adddup2(&actions,fds[1],STDOUT_FILENO);
    posix_spawn_file_actions_addclose(&actions,fds[0]);
    posix_spawn_file_actions_addclose(&actions,fds[1]);
    char *argsFull[]={ffmpeg,"-hide_banner","-nostdin","-v","error","-i",(char *)path,
        "-map","0:a:0","-c:a","copy","-f","data","pipe:1",NULL};
    char *argsInspect[]={ffmpeg,"-hide_banner","-nostdin","-v","error","-i",(char *)path,
        "-map","0:a:0","-c:a","copy","-frames:a","1","-f","data","pipe:1",NULL};
    extern char **environ;pid_t pid;
    // Block cancellation until the child is registered with the signal handler.
    sigset_t blocked,previous;sigemptyset(&blocked);sigaddset(&blocked,SIGTERM);sigaddset(&blocked,SIGINT);
    sigprocmask(SIG_BLOCK,&blocked,&previous);
    posix_spawnattr_t attr;posix_spawnattr_init(&attr);
    posix_spawnattr_setsigmask(&attr,&previous);posix_spawnattr_setflags(&attr,POSIX_SPAWN_SETSIGMASK);
    int status=posix_spawnp(&pid,ffmpeg,&actions,&attr,firstFrameOnly?argsInspect:argsFull,environ);
    if(!status) demuxPID=pid;
    sigprocmask(SIG_SETMASK,&previous,NULL);
    posix_spawnattr_destroy(&attr);posix_spawn_file_actions_destroy(&actions);close(fds[1]);
    if(status) {close(fds[0]);return fail("Cannot launch bundled MP4 demuxer.");}
    fclose(in->file);in->file=fdopen(fds[0],"rb");
    if(!in->file) {close(fds[0]);return fail("Cannot read MP4 demux pipe.");}
    return 0;
}
static int open_input(Input *in,const char *path,int inspect) {
    memset(in,0,sizeof(*in)); in->pid=-1; in->continuity=-1; in->pesRemaining=-1;
    in->file=fopen(path,"rb"); if(!in->file) return fail("Cannot open input audio.");
    uint8_t p[612]; size_t n=fread(p,1,sizeof(p),in->file); rewind(in->file);
    if(n>=7 && p[0]==0xff && p[1]==0xf2) return 0;
    if(n>=12 && !memcmp(p+4,"ftyp",4)) return demux_mp4(in,path,inspect);
    int sizes[]={188,192,204};
    for(int i=0;i<3;i++) {
        int prefix=sizes[i]==192?4:0;
        if(n>(size_t)(prefix+sizes[i]*2) && p[prefix]==0x47 && p[prefix+sizes[i]]==0x47 && p[prefix+sizes[i]*2]==0x47) {
            in->packetSize=sizes[i]; in->prefix=prefix; return 0;
        }
    }
    return fail("No AVS3 audio elementary stream or MPEG-TS stream found.");
}
static int packet_payload(Input *in) {
    for(;;) {
        size_t n=fread(in->packet,1,in->packetSize,in->file);
        if(n==0) return ferror(in->file)?fail("Input read failed."):0;
        if(n!=(size_t)in->packetSize) return fail("Truncated MPEG-TS packet.");
        uint8_t *p=in->packet+in->prefix;
        if(p[0]!=0x47) return fail("Lost MPEG-TS packet sync.");
        int pid=((p[1]&31)<<8)|p[2], control=(p[3]>>4)&3;
        if(in->pid>=0 && pid!=in->pid) continue;
        if(!(control&1)) continue;
        if((p[1]&0x80) || (p[3]&0xc0)) { if(pid==in->pid) return fail("Damaged or encrypted Audio Vivid TS packet."); else continue; }
        size_t start=4, end=188;
        if(control&2) start+=1+p[4];
        if(start>=end) continue;
        int cc=p[3]&15;
        if(pid==in->pid && in->continuity>=0) {
            if(cc==in->continuity) continue;
            if(cc!=((in->continuity+1)&15) && !(control&2 && p[4]>0 && (p[5]&0x80))) return fail("Missing Audio Vivid TS packets.");
        }
        if(p[1]&0x40) {
            if(end-start<9 || p[start]!=0 || p[start+1]!=0 || p[start+2]!=1) continue;
            int length=(p[start+4]<<8)|p[start+5];
            int extra=p[start+8];
            size_t payload=start+9+extra;
            if(payload>end) return fail("Unsupported fragmented PES header.");
            if(in->pid<0) {
                if(end-payload<7 || p[payload]!=0xff || p[payload+1]!=0xf2) continue;
                // Select by sync even for unsupported profiles, so the caller
                // reports the real configuration error rather than an empty TS.
                in->pid=pid;
            }
            if(length && length<3+extra) return fail("Invalid MPEG-TS PES length.");
            in->pesRemaining=length?length-3-extra:-1;
            start=payload;
        } else if(in->pid<0) continue;
        in->continuity=cc;
        if(in->pesRemaining==0) continue;
        if(in->pesRemaining>0 && end-start>(size_t)in->pesRemaining) end=start+in->pesRemaining;
        if(in->pesRemaining>0) in->pesRemaining-=end-start;
        in->offset=in->prefix+start; in->end=in->prefix+end;
        if(start<end) return 1;
    }
}
static int read_bytes(Input *in,uint8_t *p,size_t count) {
    if(!in->packetSize) {
        size_t n=fread(p,1,count,in->file);
        return n==count?1:(n==0 && !ferror(in->file)?0:fail("Truncated Audio Vivid frame."));
    }
    size_t copied=0;
    while(copied<count) {
        if(in->offset==in->end) {
            int status=packet_payload(in);
            if(status<=0) return status==0 && copied>0?fail("Truncated Audio Vivid PES/frame."):status;
        }
        size_t n=in->end-in->offset; if(n>count-copied) n=count-copied;
        memcpy(p+copied,in->packet+in->offset,n); in->offset+=n; copied+=n;
    }
    return 1;
}
static int read_frame(Input *in,uint8_t *head,uint8_t *payload,Header *h) {
    int n=read_bytes(in,head,7); if(n<=0) return n;
    int profile=bits(head,20,3), soundBed=bits(head,35,2);
    int headerBytes=profile==1?(soundBed==0?8:9):7;
    if(headerBytes>7 && read_bytes(in,head+7,headerBytes-7)<=0) return fail("Incomplete Audio Vivid frame header.");
    if(header(head,h)<0) return -1;
    n=read_bytes(in,payload,h->payload); if(n<=0) return fail("Incomplete Audio Vivid frame payload.");
    unsigned crc=(bits(head,27,8)<<8)|bits(head,h->crcOffset,8);
    if(Crc16(payload,h->payload)!=crc) return fail("Audio Vivid frame CRC mismatch.");
    return 1;
}
static const char *source_names(int config) {
    switch(config) {
        case 0:return "C"; case 1:return "L,R"; case 2:return "L,R,C,LFE,Ls,Rs";
        case 3:return "L,R,C,LFE,Ls,Rs,Lrs,Rrs"; case 6:return "L,R,Ls,Rs";
        case 7:return "L,R,C,LFE,Ls,Rs,Ltf,Rtf";
        case 8:return "L,R,C,LFE,Ls,Rs,Ltf,Rtf,Ltr,Rtr";
        case 9:return "L,R,C,LFE,Ls,Rs,Lrs,Rrs,Ltf,Rtf";
        case 10:return "L,R,C,LFE,Ls,Rs,Lrs,Rrs,Ltf,Rtf,Ltr,Rtr";
        case 11:return "ACN0,ACN1,ACN2,ACN3";
        case 12:return "ACN0,ACN1,ACN2,ACN3,ACN4,ACN5,ACN6,ACN7,ACN8";
        case 13:return "ACN0,ACN1,ACN2,ACN3,ACN4,ACN5,ACN6,ACN7,ACN8,ACN9,ACN10,ACN11,ACN12,ACN13,ACN14,ACN15";
    }
    return "";
}
static int names(const char *text,int *indices) {
    char buffer[256]; if(strlen(text)>=sizeof(buffer)) return fail("Invalid target layout.");
    strcpy(buffer,text); int count=0; char *save=NULL;
    for(char *p=strtok_r(buffer,", ",&save);p;p=strtok_r(NULL,", ",&save)) {
        int index=-1;
        for(int i=0;i<(int)(sizeof(speakers)/sizeof(*speakers));i++) if(strcmp(p,speakers[i].name)==0) index=i;
        if(index<0 || count>=24) return fail("Unsupported target speaker name.");
        for(int i=0;i<count;i++) if(indices[i]==index) return fail("Duplicate target speaker.");
        indices[count++]=index;
    }
    return count>0?count:fail("Empty target layout.");
}
static void direction(int index,double v[3]) {
    double az=speakers[index].azimuth*M_PI/180,el=speakers[index].elevation*M_PI/180;
    v[0]=sin(az)*cos(el);v[1]=cos(az)*cos(el);v[2]=sin(el);
}
static void matrix(float mix[24][16],int *src,int ns,int *dst,int nd) {
    memset(mix,0,24*16*sizeof(float));
    for(int s=0;s<ns;s++) {
        int exact=-1; for(int d=0;d<nd;d++) if(src[s]==dst[d]) exact=d;
        if(exact>=0) {mix[exact][s]=1;continue;}
        if(nd==2 && dst[0]==0 && dst[1]==1) {
            double az=speakers[src[s]].azimuth;
            if(src[s]==3) {mix[0][s]=mix[1][s]=0.5f;}
            else if(az<0) mix[0][s]=0.70710678f;
            else if(az>0 && az<180) mix[1][s]=0.70710678f;
            else mix[0][s]=mix[1][s]=0.70710678f;
            continue;
        }
        if(src[s]==3) continue;
        double from[3],best=-2; direction(src[s],from); int nearest=-1;
        for(int d=0;d<nd;d++) {
            if(dst[d]==3) continue;
            double to[3];direction(dst[d],to); double dot=from[0]*to[0]+from[1]*to[1]+from[2]*to[2];
            if(dot>best) {best=dot;nearest=d;}
        }
        if(nearest>=0) mix[nearest][s]=0.70710678f;
    }
}
typedef struct {
    double position[3];
    float gain, staticGain;
    int gainUnit, muted, staticMuted, jump;
} ObjectState;
static float linear_gain(float gain,int unit) {return unit?powf(10,gain/20):gain;}
static void static_metadata(Avs3BasicL1 *meta,ObjectState *state,int channels) {
    for(int s=0;s<channels;s++) {state[s].staticGain=1;state[s].staticMuted=0;state[s].gainUnit=0;}
    for(int o=0;o<meta->numOfObjects && o<8;o++) {
        AudioObject *object=&meta->audioObjectData[o];
        for(int p=0;p<meta->numOfPacks && p<8;p++) {
            AudioPackFormat *pack=&meta->audioPackFormatData[p];int used=0;
            for(int r=0;r<object->numPacks && r<8;r++)if(object->refPackFormatIdx[r]==pack->packFormatIdx)used=1;
            if(!used)continue;
            for(int c=0;c<pack->numChannels && c<32;c++) {
                int channel=pack->hasChannelReuse?pack->transChRef[c]:pack->packFormatStartIdx+c;
                if(channel<0 || channel>=channels)continue;
                state[channel].staticMuted=object->hasMute;
                state[channel].staticGain=object->hasGain?linear_gain(object->gain,object->gainUnit):1;
                state[channel].gainUnit=object->hasGain?object->gainUnit:0;
                for(int j=0;j<meta->numOfChannels && j<32;j++) {
                    AudioChannelFormat *format=&meta->audioChannelFormatData[j];
                    if(format->channelFormatIdx!=pack->refChannelIdx[c] || !format->hasChannelGain)continue;
                    state[channel].staticGain=linear_gain(format->channelGain,format->gainUnit);
                    state[channel].gainUnit=format->gainUnit;
                }
                state[channel].gain=state[channel].staticGain;
            }
        }
    }
}
static int object_source_mix(AVS3DecoderHandle decoder,const Header *h,ObjectState *state,
                             const float bed[24][16],float mix[24][16]) {
    Avs3MetaData *meta=&decoder->hMetadataDec->avs3MetaData;
    if(meta->hasStaticMeta)static_metadata(&meta->avs3MetaDataStatic.avs3BasicL1,state,h->channels);
    if(meta->hasDynamicMeta) {
        Avs3MetaDataDynamic *dm=&meta->avs3MetaDataDynamic;
        if(dm->dmLevel>1) return fail("Unsupported Audio Vivid dynamic metadata level.");
        // The reference decoder emits bed channels followed by object channels,
        // in the same order as the dynamic object metadata records.
        for(int o=0;o<h->objects;o++) {
            ObjectState *obj=&state[h->bedChannels+o];Avs3DmL1MetaData *m=&dm->avs3DmL1MetaData[o];
            obj->muted=dm->muteFlag[o];obj->jump=0;
            if(obj->muted)continue;
            if(m->cartesian) {
                obj->position[0]=m->obj_x;obj->position[1]=m->obj_y;obj->position[2]=m->obj_z;
            } else {
                double az=-m->objAzimuth*M_PI/180,el=m->objElevation*M_PI/180;
                double distance=m->objDistance;
                obj->position[0]=sin(az)*cos(el)*distance;obj->position[1]=cos(az)*cos(el)*distance;obj->position[2]=sin(el)*distance;
            }
            if(m->hasObjGain)obj->gain=linear_gain(m->gain,obj->gainUnit);
            obj->jump=m->jumpPosition;
        }
    }
    memcpy(mix,bed,24*16*sizeof(float));
    for(int s=0;s<h->bedChannels;s++)for(int d=0;d<h->channels;d++)
        mix[d][s]*=state[s].staticMuted?0:state[s].staticGain;
    for(int o=0;o<h->objects;o++) {
        int s=h->bedChannels+o;ObjectState *obj=&state[s];
        float gain=obj->muted || obj->staticMuted?0:obj->gain;
        if(!isfinite(gain))return fail("Invalid Audio Vivid object gain.");
        // Keep each decoded object as its own source channel. AV3A exposes
        // position metadata separately; this bridge does not invent a
        // speaker panning matrix for it.
        mix[s][s]=gain;
    }
    return 0;
}
static void word32(FILE *f,uint32_t n) {uint8_t p[]={n,n>>8,n>>16,n>>24};fwrite(p,1,4,f);}
// Playback sidecars share this decode's frame boundaries and source channel order.
// VVM1: six LE UInt32 words, then one xyz/peak/jump float tuple per source channel
// per 1024-sample frame. PCM contains gain-adjusted source channels before device downmix.
static int playback_frame(FILE *timeline,FILE *sourcePCM,const Header *h,const int *src,
                          const ObjectState *state,const int16_t *pcm) {
    float source[MAX_CHANNELS*FRAME_LEN],points[MAX_CHANNELS*5];
    for(int s=0;s<h->channels;s++) {
        int object=s>=h->bedChannels;
        float gain=object?(state[s].muted || state[s].staticMuted?0:state[s].gain):
            (state[s].staticMuted?0:state[s].staticGain);
        double position[3] = {0.0, 1.0, 0.0};
        if(object)memcpy(position,state[s].position,sizeof(position));
        else if(!h->hoaOrder) direction(src[s],position);
        // Match the room's right/front/up coordinates; the ear plane is halfway up.
        points[s*5]=position[0];points[s*5+1]=position[1];points[s*5+2]=(position[2]+1)*0.5;
        points[s*5+3]=0;points[s*5+4]=object?state[s].jump:1;
        for(int f=0;f<FRAME_LEN;f++) {
            float value=pcm[f*h->channels+s]/32768.0f*gain;
            source[f*h->channels+s]=value;
            points[s*5+3]=fmaxf(points[s*5+3],fabsf(value));
        }
    }
    if(fwrite(source,sizeof(float),h->channels*FRAME_LEN,sourcePCM)!=(size_t)h->channels*FRAME_LEN ||
       fwrite(points,sizeof(float),h->channels*5,timeline)!=(size_t)h->channels*5 ||
       fflush(sourcePCM)!=0 || fflush(timeline)!=0)return fail("Audio Vivid playback sidecar write failed.");
    return 0;
}
int main(int argc,char **argv) {
    setvbuf(stderr,NULL,_IOLBF,0);
    signal(SIGTERM,stop_demux_signal);signal(SIGINT,stop_demux_signal);signal(SIGPIPE,SIG_IGN);
    if(argc<3 || (strcmp(argv[1],"probe") && strcmp(argv[1],"inspect") && strcmp(argv[1],"stream") && strcmp(argv[1],"decode") && strcmp(argv[1],"extract"))) {
        fprintf(stderr,"usage: vivid probe input | inspect input | stream input channels | decode input channels output.wav | extract input output.av3a\n");return 2;
    }
    int inspect=!strcmp(argv[1],"inspect"), probe=!strcmp(argv[1],"probe") || inspect, extract=!strcmp(argv[1],"extract");
    if((!probe && argc<4) || (!strcmp(argv[1],"decode") && argc<5)) return 2;
    Input in;int result=1;FILE *out=NULL,*timeline=NULL,*sourcePCM=NULL;AVS3DecoderHandle decoder=NULL;
    int playbackSidecars=!strcmp(argv[1],"stream") && argc==6;
    if(open_input(&in,argv[2],inspect)<0) goto done;
    uint8_t head[MAX_NBYTES_FRAME_HEADER],payload[MAX_BS_BYTES];Header first={0},current;
    int src[16],dst[24],nd=0;float mix[24][16],bed[24][16],previousMix[24][16];int64_t frames=0;uint64_t written=0;
    ObjectState objects[MAX_CHANNELS]={0};
    for(int s=0;s<MAX_CHANNELS;s++) {objects[s].position[1]=1;objects[s].gain=objects[s].staticGain=1;}
    if(extract) {out=fopen(argv[3],"wb");if(!out){fail("Cannot create elementary stream.");goto done;}}
    int status;
    while((status=read_frame(&in,head,payload,&current))>0) {
        if(!frames) {
            first=current;
            if(!probe && !extract) {
                int rawSource=!strcmp(argv[3],"source");
                if(first.hoaOrder && !rawSource) {
                    fail("Audio Vivid HOA channels are preserved as source ACN channels; speaker-layout mapping is unsupported.");goto done;
                }
                if(first.objects && !rawSource) {
                    fail("Audio Vivid object channels are preserved as source channels; speaker-layout rendering is unsupported.");goto done;
                }
                int ns=(!first.hoaOrder && first.bedChannels)?names(source_names(first.config),src):0;
                if(!first.hoaOrder && ns!=first.bedChannels) goto done;
                if(rawSource) {
                    nd=first.channels;
                    memset(mix,0,sizeof(mix));
                    for(int s=0;s<first.bedChannels;s++)mix[s][s]=1;
                } else {
                    nd=names(argv[3],dst);
                    if(nd<1)goto done;
                    // Fixed beds can only be routed or downmixed to available speakers.
                    for(int d=0;d<nd;d++) {
                        int present=nd==1 && dst[d]==2; // Mono fold-down.
                        for(int s=0;s<ns;s++) if(dst[d]==src[s]) present=1;
                        if(!present) {fail("Target layout adds speakers absent from the Audio Vivid source; silent channel padding is unsupported.");goto done;}
                    }
                    matrix(mix,src,ns,dst,nd);
                }
                memcpy(bed,mix,sizeof(bed));memcpy(previousMix,mix,sizeof(mix));
                decoder=calloc(1,sizeof(*decoder));if(!decoder){fail("Cannot allocate decoder.");goto done;}
                if(!Avs3ParseBsFrameHeader(decoder,head,1,NULL)) {fail("Decoder rejected header.");free(decoder);decoder=NULL;goto done;}
                FILE *model=NULL;if(Avs3InitDecoder(decoder,&model,"")<0) {fail("Decoder initialization failed.");goto done;}
                memset(decoder->hMetadataDec,0,sizeof(*decoder->hMetadataDec));
                if(playbackSidecars) {
                    timeline=fopen(argv[4],"wb");sourcePCM=fopen(argv[5],"wb");
                    if(!timeline || !sourcePCM){fail("Cannot create Audio Vivid playback sidecars.");goto done;}
                    word32(timeline,0x314d5656);word32(timeline,first.rate);word32(timeline,first.bedChannels);
                    word32(timeline,first.objects);word32(timeline,first.config);word32(timeline,FRAME_LEN);
                    if(fflush(timeline)!=0){fail("Cannot write playback metadata header.");goto done;}
                }
                fprintf(stderr,"[INFO] source rate=%d channels=%d configuration=%d model=%s bitrate=%d order=%s bed=%d objects=%d renderer=%s\n",first.rate,first.channels,first.config,first.nn==0?"MAIN":"LC",first.bitrate,source_names(first.config),first.bedChannels,first.objects,rawSource?"source-channel-preservation":"channel-map");
                fprintf(stderr,"[INFO] output channels=%d order=%s format=F32LE mode=%s\n",nd,rawSource?"source channels (bed then objects)":argv[3],argv[1]);
                if(!strcmp(argv[1],"decode")) {
                    out=fopen(argv[4],"wb+");if(!out){fail("Cannot create WAV output.");goto done;}
                    if(vivid_wav_header(out,first.rate,nd,0)<0) {fail("Cannot write WAV header.");goto done;}
                    fprintf(stderr,"[INFO] WAV container=RIFF; automatic RF64 for large files\n");
                } else {out=stdout;word32(out,first.rate);word32(out,nd);fflush(out);}
            }
        } else if(current.config!=first.config || current.rate!=first.rate || current.nn!=first.nn || current.profile!=first.profile || current.objects!=first.objects) {fail("Audio Vivid configuration changes within the stream are unsupported.");goto done;}
        if(extract) {if(fwrite(head,1,current.headerBytes,out)!=(size_t)current.headerBytes || fwrite(payload,1,current.payload,out)!=(size_t)current.payload){fail("Elementary stream write failed.");goto done;}}
        if(decoder) {
            uint16_t crc;if(!Avs3ParseBsFrameHeader(decoder,head,0,&crc)){fail("Invalid frame header.");goto done;}
            memcpy(decoder->hBitstream->bitstream,payload,current.payload);
            int16_t pcm[MAX_CHANNELS*FRAME_LEN];float rendered[24*FRAME_LEN];
            Avs3Decode(decoder,pcm);ResetBitstream(decoder->hBitstream);
            for(int s=first.bedChannels;s<first.channels;s++)objects[s].jump=0;
            if(first.objects) {
                if(object_source_mix(decoder,&first,objects,bed,mix)<0)goto done;
                for(int s=first.bedChannels;s<first.channels;s++)if(!frames || objects[s].jump)
                    for(int d=0;d<nd;d++)previousMix[d][s]=mix[d][s];
            }
            if(playbackSidecars && playback_frame(timeline,sourcePCM,&first,src,objects,pcm)<0)goto done;
            for(int f=0;f<FRAME_LEN;f++) for(int d=0;d<nd;d++) {
                float value=0;for(int s=0;s<first.channels;s++) {
                    float gain=mix[d][s];
                    if(first.objects && s>=first.bedChannels)gain=previousMix[d][s]+(gain-previousMix[d][s])*(f+1)/(float)FRAME_LEN;
                    value+=(float)pcm[f*first.channels+s]/32768*gain;
                }
                rendered[f*nd+d]=value;
            }
            memcpy(previousMix,mix,sizeof(mix));
            if(written>(uint64_t)INT64_MAX-VIVID_WAV_HEADER_SIZE-(uint64_t)FRAME_LEN*nd*4){fail("PCM output exceeds the supported 64-bit file size.");goto done;}
            if(fwrite(rendered,sizeof(float),FRAME_LEN*nd,out)!=(size_t)FRAME_LEN*nd){fail("PCM write failed.");goto done;}
            written+=(uint64_t)FRAME_LEN*nd*4;
        }
        frames++;
        if(inspect) break;
        if(decoder && frames%24==0) fprintf(stderr,"frames=%lld\n",(long long)frames);
    }
    if(status<0 || !frames) {if(!errorMessage[0])fail("No complete Audio Vivid frames found.");goto done;}
    if(demuxPID>0) {
        int exitStatus;pid_t waited;do {waited=waitpid(demuxPID,&exitStatus,0);}while(waited<0 && errno==EINTR);
        demuxPID=0;
        if(waited<0 || !WIFEXITED(exitStatus) || WEXITSTATUS(exitStatus)!=0) {fail("MP4 audio demux failed.");goto done;}
    }
    if(probe) fprintf(stdout,"{\"sampleRate\":%d,\"channels\":%d,\"configuration\":%d,\"bitrate\":%d,\"frames\":%lld,\"duration\":%.9f,\"channelOrder\":\"%s\",\"objectCount\":%d,\"hoaOrder\":%d,\"profile\":%d}\n",first.rate,first.channels,first.config,first.bitrate,(long long)frames,(double)frames*FRAME_LEN/first.rate,source_names(first.config),first.objects,first.hoaOrder,first.profile);
    if(decoder && out!=stdout) {
        if(vivid_wav_header(out,first.rate,nd,written)<0) {fail("Cannot finalize WAV/RF64 header.");goto done;}
        fprintf(stderr,"[INFO] WAV finalized container=%s file_bytes=%llu data_bytes=%llu sample_frames=%llu\n",vivid_wav_is_rf64(written)?"RF64":"RIFF",(unsigned long long)(written+VIVID_WAV_HEADER_SIZE),(unsigned long long)written,(unsigned long long)(written/(nd*4)));
    }
    if(out && (fflush(out)!=0 || ferror(out))) {fail("Output write failed.");goto done;}
    if(decoder) fprintf(stderr,"frames=%lld\n",(long long)frames);
    if(decoder) fprintf(stderr,"[INFO] decoded frames=%lld pcm_frames=%lld pcm_bytes=%llu duration=%.6f\n",(long long)frames,(long long)frames*FRAME_LEN,(unsigned long long)written,(double)frames*FRAME_LEN/first.rate);
    result=0;
done:
    if(decoder) Avs3DecoderDestroy(decoder);
    if(demuxPID>0)kill(demuxPID,SIGTERM);
    if(in.file)fclose(in.file);
    if(demuxPID>0) {
        while(waitpid(demuxPID,NULL,0)<0 && errno==EINTR){}demuxPID=0;
    }
    if(out && out!=stdout && fclose(out)!=0 && !result) {fail("Output close failed.");result=1;}
    if(timeline && fclose(timeline)!=0 && !result){fail("Playback metadata close failed.");result=1;}
    if(sourcePCM && fclose(sourcePCM)!=0 && !result){fail("Source PCM close failed.");result=1;}
    if(result)fprintf(stderr,"[ERROR] Audio Vivid: %s\n",errorMessage[0]?errorMessage:"Decode failed.");
    return result;
}
