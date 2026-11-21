/* Expose the MPEG-H decoder's init/execute API over binary pipes.
 * Control values are LE32.  Setup: CICP index, raw-frame flag, and a flags word.
 * The initial reply is input capacity with bit 31 set to indicate float output
 * and bit 30 set for the object-signal protocol this worker implements.
 * Each request supplies a refill length and bytes.
 * Each reply contains status, consumed bytes, init flag, sample rate, channels,
 * render byte count, object-metadata byte count, OAM metadata byte count,
 * channel metadata byte count and object-signal byte count; the payloads follow
 * in that order: object metadata (a count and 24 rows of 8 floats), the OAM
 * metadata bitstream, the channel metadata bitstream and the object signals as
 * interleaved 24-bit PCM.
 *
 * With the object flag the decoder runs with its external rendering interface
 * enabled: it keeps rendering the CICP layout as before, and additionally hands
 * out the object signals and the objects' metadata, which is what the ADM
 * export is built from.  Frames that carry no render output (the preroll) are
 * left without payloads so signal and metadata stay aligned.
 */
#include <stdlib.h>
#include <string.h>

#include "../codec_worker.h"
#include "soundweaver_state.h"
#include "impeghd_type_def.h"
#include "impeghd_memory_standards.h"
#include "impeghd_api.h"
#include "impeghd_config_params.h"

#define SW_OBJECT_FLAG 0x1u
#define SW_CAPACITY_FLOAT 0x80000000u
#define SW_CAPACITY_OBJECTS 0x40000000u
#define SW_METADATA_BYTES 768
#define SW_SIGNAL_CHANNELS_MAX 64
#define SW_SIGNAL_BYTES (3u * 1024u * SW_SIGNAL_CHANNELS_MAX)
#define SW_METADATA_BYTES_OBJECTS (4u + SW_MPEGH_OBJECTS_MAX * SW_MPEGH_OBJECT_FIELDS * 4u)

static pVOID allocate(UWORD32 size, UWORD32 alignment)
{
    return malloc((size_t)size + alignment);
}

int main(void)
{
    uint32_t cicp, raw, flags, length;
    binary_pipes();
    if (!read_u32(&cicp) || !read_u32(&raw) || !read_u32(&flags))
        return 1;
    ia_input_config input = {0};
    ia_output_config output = {0};
    input.ui_cicp_layout_idx = cicp;
    input.ui_raw_flag = raw;
    input.ui_mhas_flag = IMPEGHD_CONFIG_PARAM_MHAS_FLAG_DFLT_VAL;
    input.ui_effect = IMPEGHD_CONFIG_PARAM_EFFECT_TYPE_DFLT_VAL;
    input.i_preset_id = IMPEGHD_CONFIG_PARAM_PRESET_ID_DFLT_VAL;
    input.ui_pcm_wd_sz = IMPEGHD_PCM_FLOAT32;
    output.malloc_mpegh = allocate;
    output.free_mpegh = free;

    const int objects = (flags & SW_OBJECT_FLAG) != 0;
    unsigned char *oam_md = NULL, *ch_md = NULL, *hoa_md = NULL, *signals = NULL;
    if (objects) {
        ch_md = calloc(SW_METADATA_BYTES, 1);
        oam_md = calloc(SW_METADATA_BYTES, 1);
        hoa_md = calloc(SW_METADATA_BYTES, 1);
        signals = calloc(SW_SIGNAL_BYTES, 1);
        if (!ch_md || !oam_md || !hoa_md || !signals)
            return 1;
        input.extrn_rend_flag = 1;
        input.ptr_ext_ren_ch_data_buf = ch_md;
        input.ptr_ext_ren_oam_data_buf = oam_md;
        input.ptr_ext_ren_hoa_data_buf = hoa_md;
        input.ptr_ext_ren_pcm_buf = signals;
    }

    IA_ERRORCODE error = ia_mpegh_dec_create(&input, &output);
    if (error) {
        fprintf(stderr, "MPEG-H initialization error 0x%08x.\n", (uint32_t)error);
        return 1;
    }
    const ia_mem_info_table *in = &output.mem_info_table[IA_MEMTYPE_INPUT];
    const ia_mem_info_table *out = &output.mem_info_table[IA_MEMTYPE_OUTPUT];
    uint32_t capacity = output.ui_inp_buf_size | SW_CAPACITY_FLOAT;
    if (objects)
        capacity |= SW_CAPACITY_OBJECTS;
    int ok = write_u32(capacity) && fflush(stdout) == 0;
    unsigned char *buffer = in->mem_ptr;
    size_t pending = 0;
    static float rows[SW_MPEGH_OBJECTS_MAX * SW_MPEGH_OBJECT_FIELDS];
    while (ok && read_u32(&length)) {
        if (length > output.ui_inp_buf_size - pending ||
            fread(buffer + pending, 1, length, stdin) != length ||
            pending + length == 0) {
            ok = 0;
            break;
        }
        pending += length;
        input.num_inp_bytes = pending;
        output.num_out_bytes = 0;
        if (output.ui_init_done)
            error = ia_mpegh_dec_execute(output.pv_ia_process_api_obj, &input, &output);
        else
            error = ia_mpegh_dec_init(output.pv_ia_process_api_obj, &input, &output);

        const int bytes = output.num_out_bytes;
        uint32_t oam_len = 0, ch_len = 0, signal_len = 0, meta_len = 0;
        unsigned count = 0;
        if (objects && bytes > 0) {
            oam_len = output.oam_data_present ? (uint32_t)output.oam_md_payload_length : 0;
            ch_len = output.ch_data_present ? (uint32_t)output.ch_md_payload_length : 0;
            signal_len = (uint32_t)output.pcm_payload_length;
            if ((oam_len && oam_len > SW_METADATA_BYTES) ||
                (ch_len && ch_len > SW_METADATA_BYTES) ||
                signal_len > SW_SIGNAL_BYTES) {
                fprintf(stderr, "MPEG-H object payload exceeds the worker buffers.\n");
                ok = 0;
                break;
            }
            if (sw_mpegh_object_state(output.pv_ia_process_api_obj, rows, &count) != 0)
                count = 0;
            meta_len = SW_METADATA_BYTES_OBJECTS;
        }
        ok = bytes >= 0 && (uint32_t)bytes <= out->ui_size &&
             write_u32((uint32_t)error) && write_u32(output.i_bytes_consumed) &&
             write_u32(output.ui_init_done) && write_u32(output.i_samp_freq) &&
             write_u32(output.i_num_chan) && write_u32((uint32_t)bytes) &&
             write_u32(meta_len) && write_u32(oam_len) && write_u32(ch_len) &&
             write_u32(signal_len) &&
             fwrite(out->mem_ptr, 1, bytes, stdout) == (size_t)bytes;
        if (ok && meta_len) {
            unsigned char header[4] = {count, count >> 8, count >> 16, count >> 24};
            ok = fwrite(header, 1, sizeof(header), stdout) == sizeof(header) &&
                 fwrite(rows, 1, sizeof(rows), stdout) == sizeof(rows);
        }
        if (ok && oam_len)
            ok = fwrite(oam_md, 1, oam_len, stdout) == (size_t)oam_len;
        if (ok && ch_len)
            ok = fwrite(ch_md, 1, ch_len, stdout) == (size_t)ch_len;
        if (ok && signal_len)
            ok = fwrite(signals, 1, signal_len, stdout) == (size_t)signal_len;
        ok = ok && fflush(stdout) == 0;
        int consumed = output.i_bytes_consumed;
        if (consumed > 0 && (size_t)consumed <= pending) {
            pending -= consumed;
            memmove(buffer, buffer + consumed, pending);
        } else {
            break;
        }
    }
    free(ch_md);
    free(oam_md);
    free(hoa_md);
    free(signals);
    ia_mpegh_dec_delete(&output);
    if (!ok || ferror(stdin)) {
        fprintf(stderr, "Invalid MPEG-H worker request or output buffer.\n");
        return 1;
    }
    return 0;
}
