"""Attribute conversion and validation see real field types, not annotation strings.

Every module uses `from __future__ import annotations`, so attrs records each field's
annotation as a string. Enum conversion and generic-type validation both need the type
itself; `chunk_attributes.field_type` resolves it.
"""
from __future__ import annotations

import attrs
import pytest

from usd_chunks import chunk, chunk_attributes
from usd_chunks.chunk_flags import ChunkFlags


def test_an_int_assigned_to_an_enum_field_becomes_the_enum():
    param = chunk.Label("label")
    param.state = 3
    assert type(param.state) is ChunkFlags.State
    assert param.state == ChunkFlags.State(3)


@pytest.mark.parametrize("cls", [chunk.Slider, chunk.Menu, chunk.Vector, chunk.Chunk])
def test_every_field_annotation_on_a_param_resolves(cls):
    unresolved = [f.name for f in attrs.fields(cls) if chunk_attributes.field_type(cls, f) is None]
    assert unresolved == []


def test_an_optional_field_is_not_type_checked():
    parent_field = attrs.fields_dict(chunk.Label)["_chunk_parent"]
    param = chunk.Label("label")
    assert param.validate_attr(parent_field, None)
