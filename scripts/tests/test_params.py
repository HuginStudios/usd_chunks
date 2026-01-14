from usd_chunks import chunk, chunk_core, chunk_notice


def chunkChanged(owner, notice, sender):
    # This works just as groupChanged, but is registered on a Chunk, so will only be sent on Chunk changes
    def get_chunk_str(s):
        if isinstance(s, chunk.Chunk):
            return "Chunk"

        if isinstance(s, chunk.Group):
            return "Group"

        if isinstance(s, chunk.Vector):
            return "Vector"

        if isinstance(s, chunk_core.ChunkParamBase):
            return s.evaluate_type().name

        if isinstance(s, chunk_core.AttributeSender):
            return "Attribute"

    msg = f"Chunk Changed: {owner.name} -> {notice.__class__.__name__}"

    step = 4
    indent = step

    for n, s in owner.iter_changes():
        msg += "\n" + " " * indent + f"{get_chunk_str(s)}: {s.name} -> {n.__class__.__name__}"

        if isinstance(s, chunk_core.AttributeSender):
            msg += f": [New Value] = {s.value}"
        indent += step

    expected = """Chunk Changed: TestParams -> ChunkChangeNotice
    FLOAT_SLIDER: newName -> ParamChangeNotice
        Attribute: name -> AttributeChangeNotice: [New Value] = newName"""

    if msg != expected:
        raise ValueError("Something went wrong")


def paramChanged(chunkItem: chunk.ChunkParamBase, notice, sender):
    pass


def request_delete(chunkItem: chunk.ChunkParamBase, notice, sender):
    pass


def main():
    params = [
        chunk.Slider("Slider"),
        chunk.Spinbox("Spinbox"),
        chunk.Menu("Menu", menu_items=["Item1", "Item2"]),
        chunk.Button("Button"),
        chunk.Label("Label"),
        chunk.Checkbox("CheckBox"),
        chunk.LineEdit("LineEdit"),
        chunk.FileBrowser("FileBrowser"),
        chunk.Separator("Separator"),
    ]

    import inspect

    expected = "<Signature (self, name='', label='', tooltip='', icon='', usd_attr=None, user_data=NOTHING, layout=<LayoutFlags.HORIZONTAL: 0>, state=<State.NONE: 0>, flags=<IntFlag.0: 0>, default_value=None, custom_widget=None, menu_items=NOTHING) -> None>"
    signature = inspect.signature(chunk.Menu.__init__).__repr__()

    if signature != expected:
        raise ValueError("__init__ signature failed", signature)

    m = chunk.Menu(
        "name",
        "label",
        "tooltip",
        "C:/Users/Viktor/Desktop/render/sparsit.exr",
        None,
        {"user": "data"},
        chunk.ChunkFlags.LayoutFlags.VERTICAL,
        chunk.ChunkFlags.State.NONE,
        chunk.ChunkFlags.ParamFlags.LABELED,
        "default_value",
        None,
        ["item1", "item2"],
    )

    groupA = chunk.Group("GroupA")
    groupA.add_params(params)

    groupA.set_flag(chunk.ChunkFlags.GroupFlags.COLLAPSABLE)

    groupB = chunk.Group("GroupB")
    groupB.add_params(params, clone=True)
    groupB.set_flag(chunk.ChunkFlags.GroupFlags.COLLAPSABLE)

    groupC = chunk.Group("GroupC")
    groupC.add_params([groupB.clone(), groupA.clone()])

    # Create the actual chunk - this is in practice just a ChunkParamGroup with some addiditional behaviour
    myChunk = chunk.Chunk("TestParams", category="Tests")
    myChunk.add_params(params, clone=True)

    for i in range(1):
        myChunk.add_params([groupA, groupC, groupB])

    myChunk.add_callback(chunkChanged)
    myChunk.add_callback(request_delete, None, True, chunk_notice.ChunkNoticeAlias.CHUNK_DELETED)

    # myChunk.print_hierarchy()

    if s := myChunk.get_param("Slider"):
        s.name = "newName"

    if not myChunk.get_param("newName"):
        raise NameError("Failed to rename")
    return myChunk
