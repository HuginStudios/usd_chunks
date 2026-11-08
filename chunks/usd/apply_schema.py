from __future__ import annotations

import logging

from usd_chunks import chunk

from pxr import Usd, Tf

logger = logging.getLogger(__name__)


def apply(owner: chunk.Chunk, notice, sender):
    menu = owner.get_param("schemas", chunk.is_param_t(str))

    # The "prim" param (not live viewport selection) so this is replayable on reload --
    # nothing is selected in the viewport at load time, but the param's own stored path
    # restores normally like any other value.
    prim_param = owner.get_param("prim", chunk.is_inst(chunk.LineEdit))
    prims = prim_param.get_prims() if prim_param else []
    if not prims:
        return

    schema_reg = Usd.SchemaRegistry()
    # Takes the type name the menu lists ("UsdPhysicsRigidBodyAPI") and the name a .usda
    # file shows ("PhysicsRigidBodyAPI") alike.
    schema = schema_reg.GetTypeFromName(menu.value)
    if not schema or not schema_reg.IsAppliedAPISchema(schema):
        if menu.value:
            logger.warning("%s: no API schema called %r, nothing applied", owner.name, menu.value)
        return

    for prim in prims:
        prim.ApplyAPI(schema)


def execute(node: chunk.Chunk) -> None:
    """Reserved top-level reactive hook -- load_chunks calls this once per chunk after
    bulk-restoring its param values, so a reloaded Apply Schema actually reapplies the
    saved schema instead of leaving the target prim untouched (apply is a Button callback,
    which load's value-restore deliberately does not trigger).
    """
    apply(node, None, None)


def update(owner: chunk.Chunk, notice, sender: chunk.LineEdit):
    stage = owner.get_stage()
    if not stage:
        return

    param = owner.get_param("schemas", chunk.is_inst(chunk.Menu))
    # Empty until a prim is picked, and this also runs when the chunk is created.
    prims = sender.get_prims()
    prim: Usd.Prim | None = prims[0] if prims else None

    if not prim or prim == stage.GetPseudoRoot():
        param.menu_items = []
        return

    schema_reg = Usd.SchemaRegistry()
    schema_list = []
    for t in Tf.Type.FindByName("UsdAPISchemaBase").GetAllDerivedTypes():
        if not (schema_reg.IsAppliedAPISchema(t) or schema_reg.IsMultipleApplyAPISchema(t)):
            continue

        if schema_reg.IsAppliedAPISchema(t) and not schema_reg.IsMultipleApplyAPISchema(t):
            try:
                if prim.CanApplyAPI(t):
                    schema_list.append(t.typeName)
            except Tf.ErrorException:
                # A schema whose plugin registered it half-way: UsdMtlxMaterialXConfigAPI
                # raises "IsA() was given an Unknown base type", which emptied the menu.
                continue

    param.menu_items = schema_list


def main():
    # The list depends on the picked prim, so a name from a script or a saved scene is kept
    # as given rather than swapped for the first entry, which then got applied instead.
    menu = chunk.Menu("schemas", "Schemas", dynamic_items=True)
    primSel = chunk.LineEdit(
        "prim", flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION
    )

    btn = chunk.Button("apply", "Apply")

    myChunk = chunk.Chunk("Apply Schema")
    myChunk.add_params([primSel, menu, btn])

    myChunk.add_callback(update, sender=primSel, notice_alias=chunk.ChunkNoticeAlias.VALUE_CHANGED, send_on_create=True)
    myChunk.add_callback(apply, sender=btn)

    return myChunk


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD,
        icon="tag",
        description="""
        Applies a USD API schema (UsdPhysics, UsdLux and other applied APIs) to a prim.

        Pick the schema in Schemas and press Apply: it is applied with ApplyAPI. Only applied
        API schemas are accepted; an unknown name logs a warning and applies nothing. A
        reopened network applies its saved schema again.
        """,
    )
