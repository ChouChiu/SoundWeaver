"""Dolby Atmos stems, cover gradient, waveform, lyric and sound field videos."""

import os

# The DSP runs many small matrix products per audio frame, where BLAS threads
# cost more than they save, and the Atmos decode already runs one process per
# core.  Set before anything imports numpy, here and in every worker process.
for _variable in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_variable, "1")
