#ifndef SOUNDWEAVER_MPEGH_STATE_H
#define SOUNDWEAVER_MPEGH_STATE_H

/* Floats reported for each object of one frame, in this order. */
#define SW_MPEGH_OBJECT_FIELDS 8
#define SW_MPEGH_OBJECTS_MAX 24

/* Copy the current frame's object metadata out of the decoder's own OAM state.
 *
 * \`values\` must hold \`SW_MPEGH_OBJECTS_MAX * SW_MPEGH_OBJECT_FIELDS\` floats; the
 * first \`count\` rows are filled with azimuth, elevation, radius, gain, spread
 * width, height, depth and priority, all descaled to the units the object
 * renderer works in (degrees, degrees, distance, linear gain, degrees, degrees,
 * 0..1, 0..7).  Object metadata that the bitstream never updates keeps the
 * values the decoder decoded last, which is what the renderer pans with.
 *
 * Returns 0 on success, -1 when \`handle\` carries no decoded object state.
 */
int sw_mpegh_object_state(void *handle, float *values, unsigned *count);

#endif
