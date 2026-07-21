"""
Algorithm-config section layouts (mirror App/communication/alg_config.h).

Defines the Python-side description of every runtime-configurable algorithm
parameter section for the Node and CCU firmware blobs, so the on-wire payloads
can be (de)serialised, exported to / imported from JSON, and pretty-printed.

A "section" is a contiguous window into the device parameter blob that maps to a
single algorithm and can be read or written independently of the others.

Keep this file in sync with the C structs ``node_alg_config_t`` /
``ccu_alg_config_t`` and the section tables in the firmware.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import List, Tuple

from .protocol import (
    ALG_CONFIG_TAG_NODE, ALG_CONFIG_TAG_CCU, ALG_CONFIG_TAG_ISOMON,
    NODE_ALG_SCHEMA_VER, CCU_ALG_SCHEMA_VER,
    ISOMON_STREAM_SCHEMA_VER, ISOMON_ISO_SCHEMA_VER,
    ALG_SEC_NODE_SC, ALG_SEC_NODE_DV_FILT, ALG_SEC_NODE_DV_CURV,
    ALG_SEC_CCU_ARC, ALG_SEC_CCU_SC, ALG_SEC_CCU_DS,
    ALG_SEC_ISOMON_STREAM, ALG_SEC_ISOMON_ISO,
)


# A field is (name, count, struct_char). struct_char defaults to 'f' (float32).
FieldSpec = Tuple[str, int, str]


@dataclass(frozen=True)
class Section:
    """Description of one algorithm-config section."""
    id: int
    name: str
    offset: int            # byte offset within the device blob
    fields: List[FieldSpec]

    @property
    def fmt(self) -> str:
        s = "<"
        for _name, count, *rest in self.fields:
            ch = rest[0] if rest else "f"
            s += ch * count
        return s

    @property
    def size(self) -> int:
        return struct.calcsize(self.fmt)

    def unpack(self, body: bytes) -> dict:
        """Decode section bytes into an ordered dict of field values.

        Scalar fields (count == 1) decode to a single value; array fields decode
        to a list.
        """
        vals = struct.unpack_from(self.fmt, body, 0)
        out: dict = {}
        i = 0
        for name, count, *_rest in self.fields:
            if count == 1:
                out[name] = vals[i]
            else:
                out[name] = list(vals[i:i + count])
            i += count
        return out

    def pack(self, values: dict) -> bytes:
        """Encode a dict of field values into section bytes.

        Missing fields raise KeyError; array fields must supply exactly ``count``
        elements.
        """
        flat: list = []
        for name, count, *_rest in self.fields:
            v = values[name]
            if count == 1:
                flat.append(v)
            else:
                seq = list(v)
                if len(seq) != count:
                    raise ValueError(
                        f"field '{name}' expects {count} values, got {len(seq)}")
                flat.extend(seq)
        return struct.pack(self.fmt, *flat)


# ---------------------------------------------------------------------------
# Node sections — mirror node_alg_config_t (140-byte blob).
# ---------------------------------------------------------------------------

NODE_SECTIONS: List[Section] = [
    Section(ALG_SEC_NODE_SC, "node_sc", 0, [
        ("sc_b", 9, "f"),          # IIR numerator [3][3] (row-major)
        ("sc_a", 9, "f"),          # IIR denominator [3][3]
        ("sc_g", 1, "f"),          # filter gain
        ("sc_scales", 4, "f"),     # classifier feature scales
        ("sc_weights_w", 4, "f"),  # classifier weights
        ("sc_weights_b", 1, "f"),  # classifier bias
    ]),
    Section(ALG_SEC_NODE_DV_FILT, "node_dv_filt", 112, [
        ("dv_refSourceVoltage", 1, "f"),
        ("dv_maxAmplDeviation", 1, "f"),
        ("dv_disturbanceTime", 1, "f"),
    ]),
    Section(ALG_SEC_NODE_DV_CURV, "node_dv_curv", 124, [
        ("dvc_upperBound_voltage", 1, "f"),
        ("dvc_upperBound_time", 1, "f"),
        ("dvc_lowerBound_voltage", 1, "f"),
        ("dvc_lowerBound_time", 1, "f"),
    ]),
]

# ---------------------------------------------------------------------------
# CCU sections — mirror ccu_alg_config_t.
# ---------------------------------------------------------------------------

CCU_SECTIONS: List[Section] = [
    Section(ALG_SEC_CCU_ARC, "ccu_arc", 0, [
        ("arc_inMin", 6, "f"),    # [2][3] row-major
        ("arc_inMax", 6, "f"),
        ("arc_outMin", 6, "f"),
        ("arc_outMax", 6, "f"),
        ("arc_gain", 6, "f"),
        ("arc_offset", 6, "f"),
        ("arc_flagBias", 3, "f"),
    ]),
    Section(ALG_SEC_CCU_SC, "ccu_sc", 156, [
        ("sc_a", 1, "f"),
        ("sc_i0", 1, "f"),
        ("sc_threshold", 1, "f"),
    ]),
    Section(ALG_SEC_CCU_DS, "ccu_ds", 168, [
        ("ds_z1", 1, "f"),
        ("ds_z2", 1, "f"),
        ("ds_z3", 1, "f"),
        ("ds_id_length", 1, "H"),
    ]),
]

# ---------------------------------------------------------------------------
# ISOMON sections — mirror isomon_stream_config_t / isomon_iso_config_t.
# ---------------------------------------------------------------------------

ISOMON_SECTIONS: List[Section] = [
    Section(ALG_SEC_ISOMON_STREAM, "isomon_stream", 0, [
        ("agg_count", 1, "I"),
        ("stream_enable", 1, "B"),
        ("reserved0", 1, "B"),
        ("reserved1", 1, "H"),
    ]),
    Section(ALG_SEC_ISOMON_ISO, "isomon_iso", 8, [
        ("r3", 1, "f"),
        ("r4", 1, "f"),
        ("r5", 1, "f"),
        ("r6", 1, "f"),
        ("r7", 1, "f"),
        ("r8", 1, "f"),
        ("fault_threshold", 1, "f"),
        ("avg_samples", 1, "I"),
        ("enable", 1, "B"),
        ("reserved", 3, "B"),
    ]),
]


DEVICE_TABLES = {
    "node": {
        "tag": ALG_CONFIG_TAG_NODE,
        "schema": NODE_ALG_SCHEMA_VER,
        "sections": NODE_SECTIONS,
    },
    "ccu": {
        "tag": ALG_CONFIG_TAG_CCU,
        "schema": CCU_ALG_SCHEMA_VER,
        "sections": CCU_SECTIONS,
    },
    "isomon": {
        "tag": ALG_CONFIG_TAG_ISOMON,
        # FW currently defines both stream/iso schema as 1; keep one device-level
        # schema value for compatibility with the on-wire header.
        "schema": max(ISOMON_STREAM_SCHEMA_VER, ISOMON_ISO_SCHEMA_VER),
        "sections": ISOMON_SECTIONS,
    },
}


def device_for_tag(tag: int) -> str | None:
    """Map a record tag to a device name ('node' / 'ccu' / 'isomon')."""
    if tag == ALG_CONFIG_TAG_NODE:
        return "node"
    if tag == ALG_CONFIG_TAG_CCU:
        return "ccu"
    if tag == ALG_CONFIG_TAG_ISOMON:
        return "isomon"
    return None


def sections_for_device(device: str) -> List[Section]:
    return DEVICE_TABLES[device]["sections"]


def section_by_name(device: str, name: str) -> Section | None:
    for s in sections_for_device(device):
        if s.name == name:
            return s
    return None


def section_by_id(device: str, sec_id: int) -> Section | None:
    for s in sections_for_device(device):
        if s.id == sec_id:
            return s
    return None
