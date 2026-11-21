/* Read the MPEG-H decoder's object metadata state for ADM export.
 *
 * The decoder renders objects internally and keeps their positions in
 * \`ia_dec_data_struct.str_obj_ren_dec_state.str_obj_md_dec_state\`; nothing on
 * the public API exposes them.  This file mirrors the casts \`impeghd_api.c\`
 * already makes on the same handle, so the include list below is that file's
 * preamble plus \`ia_core_coder_create.h\` for the data struct itself.
 */

#include <string.h>

#include <impeghd_type_def.h>
#include "impeghd_api.h"
#include "impd_drc_common.h"
#include "impd_drc_extr_delta_coded_info.h"
#include "impd_drc_struct.h"
#include "impeghd_intrinsics_flt.h"
#include "ia_core_coder_defines.h"
#include "ia_core_coder_bitbuffer.h"
#include "ia_core_coder_interface.h"
#include "ia_core_coder_info.h"
#include "ia_core_coder_rom.h"
#include "ia_core_coder_dec.h"
#include "ia_core_coder_cnst.h"
#include "ia_core_coder_acelp_info.h"
#include "ia_core_coder_api_defs.h"
#include "ia_core_coder_apicmd_standards.h"
#include "ia_core_coder_igf_data_struct.h"
#include "ia_core_coder_stereo_lpd.h"
#include "impeghd_tbe_dec.h"
#include "ia_core_coder_tns_usac.h"
#include "ia_core_coder_main.h"
#include "ia_core_coder_arith_dec.h"
#include "ia_core_coder_channelinfo.h"
#include "ia_core_coder_channel.h"
#include "ia_core_coder_constants.h"
#include "ia_core_coder_definitions.h"
#include "ia_core_coder_env_extr.h"
#include "ia_core_coder_function_selector.h"
#include "impeghd_memory_standards.h"
#include "impeghd_mhas_parse.h"
#include "impeghd_hoa_common_values.h"
#include "impeghd_hoa_matrix_struct.h"
#include "ia_core_coder_config.h"
#include "impeghd_config_params.h"
#include "impeghd_cicp_defines.h"
#include "impeghd_cicp_struct_def.h"
#include "impeghd_binaural.h"
#include "impeghd_ele_interaction_intrfc.h"
#include "impeghd_mae_mp4_intrfc.h"
#include "impeghd_error_codes.h"
#include "impeghd_fft_ifft_rom.h"
#include "impeghd_format_conv_defines.h"
#include "impeghd_format_conv_data.h"
#include "impeghd_hoa_data_types.h"
#include "impeghd_hoa_dec_struct.h"
#include "impeghd_hoa_frame_params.h"
#include "impeghd_hoa_nfc_filtering.h"
#include "impeghd_hoa_spatial_decoder_struct.h"
#include "impeghd_hoa_space_positions.h"
#include "impeghd_hoa_simple_mtrx.h"
#include "impeghd_hoa_render_mtrx.h"
#include "impeghd_hoa_renderer.h"
#include "impeghd_hoa_decoder.h"
#include "impeghd_hoa_matrix.h"
#include "impeghd_hoa_spatial_decoder.h"
#include "impeghd_3d_vec_struct_def.h"
#include "impeghd_oam_dec_defines.h"
#include "impeghd_oam_dec_struct_def.h"
#include "impeghd_obj_ren_dec_defines.h"
#include "impeghd_obj_ren_dec_struct_def.h"
#include "impeghd_uni_drc_struct.h"
#include "impeghd_binaural_renderer.h"
#include "impeghd_peak_limiter_struct_def.h"
#include "impeghd_resampler.h"
#include "impeghd_cicp_2_geometry.h"
#include "ia_core_coder_struct.h"
#include "ia_core_coder_struct_def.h"
#include "ia_core_coder_create.h"

#include "soundweaver_state.h"

int sw_mpegh_object_state(void *handle, float *values, unsigned *count)
{
    ia_mpegh_dec_api_struct *decoder = (ia_mpegh_dec_api_struct *)handle;
    if (decoder == NULL || decoder->p_state_mpeghd == NULL)
        return -1;
    ia_dec_data_struct *data = (ia_dec_data_struct *)decoder->p_state_mpeghd->pstr_dec_data;
    if (data == NULL)
        return -1;
    const ia_oam_dec_state_struct *state = &data->str_obj_ren_dec_state.str_obj_md_dec_state;
    WORD32 objects = state->num_objects;
    if (objects < 0)
        objects = 0;
    if (objects > MAX_NUM_OAM_OBJS)
        objects = MAX_NUM_OAM_OBJS;
    memset(values, 0, SW_MPEGH_OBJECTS_MAX * SW_MPEGH_OBJECT_FIELDS * sizeof(float));
    for (WORD32 o = 0; o < objects; o++) {
        float *row = values + (size_t)o * SW_MPEGH_OBJECT_FIELDS;
        /* The first sub-frame of the frame: its metadata holds until the next one. */
        row[0] = state->azimuth_descaled[o];
        row[1] = state->elevation_descaled[o];
        row[2] = state->radius_descaled[o];
        row[3] = state->gain_descaled[o];
        row[4] = state->spread_width_descaled[o];
        row[5] = state->spread_height_descaled[o];
        row[6] = state->spread_depth_descaled[o];
        row[7] = state->dyn_obj_priority_descaled[o];
    }
    *count = (unsigned)objects;
    return 0;
}
