"""The objects one decoded MPEG-H frame carries, as the decoder reports them.

The worker runs libmpegh with its external rendering interface enabled, so the
decoder hands back both the rendered layout and, per frame, the object signals
with the metadata the object renderer pans them with.  That record is fixed
size: a count, then `OBJECTS_MAX` rows of `OBJECT_FIELDS` floats, which this
module unpacks.  Nothing here decides what the numbers mean for ADM; the units
are the ones the decoder works in:

* azimuth and elevation in degrees, the elevation 0 being the listener's plane,
* radius as a distance factor the renderer uses and ADM has no field for,
* gain as a linear factor the object's own audio has not been through yet,
* spread width and height in degrees and depth as 0..1,
* priority as the dynamic object priority, 0..7.

Where an object's metadata never changes in the bitstream the decoder keeps
what it last decoded, so a static mix reports the same row every frame.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

OBJECT_FIELDS = 8
OBJECTS_MAX = 24
# the count, then every row: the worker always sends the whole table
RECORD_BYTES = 4 + OBJECTS_MAX * OBJECT_FIELDS * 4

_HEADER = struct.Struct("<I")
_ROWS = struct.Struct("<" + str(OBJECTS_MAX * OBJECT_FIELDS) + "f")


@dataclass(frozen=True, slots=True)
class MpeghObject:
    """One object's metadata for one frame."""

    index: int
    azimuth: float
    elevation: float
    radius: float
    gain: float
    width: float
    height: float
    depth: float
    priority: float

    @property
    def audible(self) -> bool:
        """False for an object the metadata mutes or parks at zero gain."""
        return self.gain > 0.0


def read_record(data: bytes) -> tuple[MpeghObject, ...]:
    """The objects of one frame from the worker's record."""
    if len(data) != RECORD_BYTES:
        raise ValueError(f"MPEG-H object record is {len(data)} bytes, not {RECORD_BYTES}")
    count, = _HEADER.unpack_from(data)
    if count > OBJECTS_MAX:
        raise ValueError(f"MPEG-H frame reports {count} objects, more than {OBJECTS_MAX}")
    values = _ROWS.unpack_from(data, _HEADER.size)
    return tuple(
        MpeghObject(
            index,
            *values[index * OBJECT_FIELDS : (index + 1) * OBJECT_FIELDS],
        )
        for index in range(count)
    )
