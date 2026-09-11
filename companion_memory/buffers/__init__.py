"""Frozen FIFO windows and bounded reversible source material."""
from .material import MaterialRecord, build_material, decode_material
__all__ = ['MaterialRecord','build_material','decode_material']
