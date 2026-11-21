# MPEG-H 3D Audio decoder sources

Upstream: https://github.com/ittiam-systems/libmpegh (MPEG-H 3D Audio Low Complexity
Profile decoder, ISO/IEC 23008-3). The vendored tree is the upstream `decoder/` and
`test/` sources plus the release docs and smoke-test suite, unmodified apart from the
changes listed below.

## Local changes

1. **Object state accessor** — `decoder/soundweaver_state.c` and `.h` are additions.
   The public API renders to a CICP layout and exposes nothing about the objects, so
   this file reads the per-object metadata (azimuth, elevation, radius, gain, spreads,
   priority) out of `ia_dec_data_struct.str_obj_ren_dec_state.str_obj_md_dec_state` on
   the same handle `impeghd_api.c` already casts. It is compiled into
   `ia_mpeghd_lib` so it shares the library's compile definitions: `LC_LEVEL_4`
   changes struct sizes, and reading the state from a translation unit built without
   it would read the wrong offsets.

2. **`decoder/impeghd_api.c`** — carries the modifications this integration needs to
   build with the system toolchain; behaviour of the decode path is unchanged.

3. **`decoder/impeghd_config_params.h`** and `decoder/ia_core_coder_decode_main.c` —
   local build/default tweaks, not part of the object export.

## How SoundWeaver uses it

`native/mpegh/soundweaver_worker.c` drives the decoder over binary pipes. Setup carries
the CICP layout, the raw-frame flag and an object flag; with the object flag the decoder
runs with its external rendering interface enabled, and each reply additionally carries
the object signals (interleaved 24-bit PCM), the ERI metadata bitstreams, and the fixed
object record the accessor above fills. `soundweaver.core.codecs.mpegh` reads that
protocol and `mpegh_adm` turns it into ADM BWF.

Upgrading the decoder: re-apply the three items above, keep `MAX_NUM_OAM_OBJS` at 24 or
widen `SW_MPEGH_OBJECTS_MAX` to match, and confirm the worker reply still carries ten
32-bit fields.
