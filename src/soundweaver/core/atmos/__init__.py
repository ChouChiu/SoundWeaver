"""Dolby Atmos in E-AC-3 (ETSI TS 103 420): objects rebuilt from the core and rendered to a speaker layout."""

from .adm_reader import ADMInfo, ADMReadError, PreparedADM, is_adm_bwf
from .decoder import AtmosDecode, AtmosError, decode_atmos, trace_atmos
from .obj_restore import RestoreConfig, RestoreResult, restore_adm_file
from .trace import ObjectTrace

__all__ = [
    "ADMInfo",
    "ADMReadError",
    "AtmosDecode",
    "AtmosError",
    "ObjectTrace",
    "PreparedADM",
    "RestoreConfig",
    "RestoreResult",
    "decode_atmos",
    "is_adm_bwf",
    "restore_adm_file",
    "trace_atmos",
]
