"""Dolby Atmos in E-AC-3 (ETSI TS 103 420): objects rebuilt from the core and rendered to 5.1.4."""

from .decoder import AtmosDecode, AtmosError, decode_atmos, trace_atmos
from .trace import ObjectTrace

__all__ = ["AtmosDecode", "AtmosError", "ObjectTrace", "decode_atmos", "trace_atmos"]
