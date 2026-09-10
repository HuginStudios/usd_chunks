"""What a chunk is CALLED in USD: prim type, root scope, and the exec-input naming.

Naming, not a schema and not just constants. "Schema" in USD means an IsA or API schema
with generated classes and a registered type, and nothing here defines one. "Constants"
would be true of the eight names below and false of the four functions that parse and
derive them -- iter_exec_inputs walks a prim.

Split out of `plugins/chunk_usd` so the vocabulary can be read without the machinery. That
module is five thousand lines and pulls in two hundred and fifty others -- reasonable for
something that owns stage sync, layer routing and load/export, and far too much to ask of
anything that only wants to know whether an attribute is an exec input.

The callers that need exactly this and nothing else:

- the graph engine, which types ports and walks connections;
- an offline port schema -- "what ports does this chunk have" answered from `main()`, with
  no stage and no GUI, which is the catalog an agent composing a network needs;
- a marketplace listing advertising a chunk's interface at publish time;
- a non-Qt host (Blender, an ImGui panel) that wants port identity without the session.

Nothing here touches a stage, a layer or a session. `chunk_usd` re-exports every name, so
existing code is unaffected and there is one definition rather than two.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover -- typing only, no import at runtime
    from pxr import Usd

#: The prim type a chunk is authored as.
CHUNK_PRIM_TYPENAME = "Chunk"

#: /chunks is a scope, like /lights or /render. Nothing lives AT it: the default chunkgroup
#: sits under it and everything else under that. Typing it Chunk made the scope look like a
#: chunk prim to anything reading the stage back.
CHUNKS_ROOT = "/chunks"
CHUNKS_ROOT_TYPENAME = "Scope"

CHUNK_TYPE_ATTR = "info:chunkType"
INPUTS_PREFIX = "inputs:"
EXEC_INPUT = "inputs:chunk"
EXEC_OUTPUT = "outputs:chunk"

# Merge chunks fan several exec branches into one node, so they need more than the single
# inputs:chunk every other chunk has. Extra slots are numbered from 1 -- slot 0 deliberately
# keeps the plain EXEC_INPUT name, so any consumer that only ever looks up inputs:chunk
# (most of them: Edit Target resolution, exec healing, auto-chain on create) transparently
# sees the merge's PRIMARY input, which is the answer those consumers want anyway. Only code
# that genuinely needs every branch -- edge drawing, tail detection, disconnect -- iterates.
EXEC_INPUT_NUMBERED = re.compile(r"^inputs:chunk([1-9]\d*)$")


def is_exec_input_name(name: str) -> bool:
    """Whether an attribute name is an exec input -- inputs:chunk, or inputs:chunk<N>."""
    return name == EXEC_INPUT or bool(EXEC_INPUT_NUMBERED.match(name))


def exec_input_index(name: str) -> int:
    """The slot an exec input names. inputs:chunk is slot 0."""
    match = EXEC_INPUT_NUMBERED.match(name)
    return int(match.group(1)) if match else 0


def exec_input_name(index: int) -> str:
    """The attribute name for a slot. Slot 0 is the plain inputs:chunk, deliberately."""
    return EXEC_INPUT if index <= 0 else f"inputs:chunk{index}"


def iter_exec_inputs(prim: "Usd.Prim") -> list:
    """A prim's exec inputs, ordered by slot -- strongest branch first.

    Sorted by the PARSED index, never by name: GetAttributes() is alphabetical, which puts
    inputs:chunk10 before inputs:chunk2. Slot order decides which branch's opinion wins, so
    a string sort would silently mis-rank a merge with ten or more inputs.
    """
    if not (prim and prim.IsValid()):
        return []
    attrs = [a for a in prim.GetAttributes() if is_exec_input_name(a.GetName())]
    attrs.sort(key=lambda a: exec_input_index(a.GetName()))
    return attrs
