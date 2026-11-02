"""What a chunk is CALLED in USD: prim type, root scope, and the exec-input naming.

Naming, not a schema and not just constants. "Schema" in USD means an IsA or API schema
with generated classes and a registered type, and nothing here defines one. "Constants"
would be true of the eight names below and false of the four functions that parse and
derive them -- iter_exec_inputs walks a prim.

Split out of `plugins/chunk_stage` so the vocabulary can be read without the machinery. That
module is five thousand lines and pulls in two hundred and fifty others -- reasonable for
something that owns stage sync, layer routing and load/export, and far too much to ask of
anything that only wants to know whether an attribute is an exec input.

The callers that need exactly this and nothing else:

- the graph engine, which types ports and walks connections;
- an offline port schema -- "what ports does this chunk have" answered from `main()`, with
  no stage and no GUI, which is the catalog an agent composing a network needs;
- a marketplace listing advertising a chunk's interface at publish time;
- a non-Qt host (Blender, an ImGui panel) that wants port identity without the session.

Nothing here touches a stage, a layer or a session. `chunk_stage` re-exports every name, so
existing code is unaffected and there is one definition rather than two.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover -- typing only, no import at runtime
    from pxr import Usd

#: The prim type a chunk is authored as.
CHUNK_PRIM_TYPENAME = "Chunk"

#: The prim type of a backdrop or sticky note in a chunk graph (UsdUI.Backdrop), and the
#: attribute that marks a sticky note. See usd_chunks/graph/backdrops.py.
BACKDROP_TYPENAME = "Backdrop"
STICKY_NOTE_ATTR = "chunk:stickyNote"

#: /chunks is a scope, like /lights or /render. Nothing lives AT it: the default chunkgroup
#: sits under it and everything else under that. Typing it Chunk made the scope look like a
#: chunk prim to anything reading the stage back.
CHUNKS_ROOT = "/chunks"
CHUNKS_ROOT_TYPENAME = "Scope"

CHUNK_TYPE_ATTR = "info:chunkType"

#: The chunk module backing every context. "context == chunkgroup": a CONTEXT chunk with no
#: module was only ever a stand-in from before this type existed. The default chunkgroup sits
#: under the /chunks scope at /chunks/<name>.
CHUNKGROUP_MODULE = "chunkgroup"

#: Digest of the chunk module's source, as it was when this chunk last cooked -- next to
#: info:chunkType because it says which code, where the type says which module. Stamped by the
#: cook and carried through saves untouched, so a file records what produced its result.
#: A chunk that has not cooked has none; a file older than the attribute has none either,
#: and neither counts as a change.
MODULE_HASH_ATTR = "info:moduleHash"

INPUTS_PREFIX = "inputs:"
EXEC_INPUT = "inputs:chunk"
EXEC_OUTPUT = "outputs:chunk"

# An Edit Target chunk publishes the identifier of the layer it currently points at onto
# its own prim under this attribute. The presence of the attr marks an exec-chain boundary
# for chunk_utils.get_edit_layer's upstream walk; its value names the layer everything
# downstream of it authors into. Publishing it (rather than importing the Edit Target
# script's resolution logic into core) keeps core decoupled from the script -- same
# published-marker pattern as chunk:lofted / set_chunk_lofted.
EDIT_TARGET_MARKER = "chunk:editTargetLayer"

# Which view slot drives a graph's cook, on the graph's context prim (0 or absent: none, so
# the graph delivers its Output). Here rather than in graph.engine, which writes it, so the
# stage code can read it too.
ACTIVE_VIEW_ATTR = "chunk:activeView"

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


def iter_exec_inputs(prim: Usd.Prim) -> list:
    """A prim's exec inputs, ordered by slot -- strongest branch first.

    Sorted by the PARSED index, never by name: GetAttributes() is alphabetical, which puts
    inputs:chunk10 before inputs:chunk2. Slot order decides which branch's opinion wins, so
    a string sort would silently mis-rank a merge with ten or more inputs.

    Args:
        prim: The chunk prim.

    Returns:
        The exec input attributes, in slot order.
    """
    if not (prim and prim.IsValid()):
        return []
    attrs = [a for a in prim.GetAttributes() if is_exec_input_name(a.GetName())]
    attrs.sort(key=lambda a: exec_input_index(a.GetName()))
    return attrs
