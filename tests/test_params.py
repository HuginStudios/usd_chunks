from usd_chunks import chunk, chunk_interface, chunk_qt, chunk_notice, chunk_registry

# from typing import reveal_type

from functools import wraps


def nice_print(func):
    @wraps(func)
    def _wrapper(*args, **kwargs):
        begin = f"{'-' * 10}* {func.__qualname__} *{'-' * 10}"
        end = "-" * len(begin)

        print(begin)
        res = func(*args, **kwargs)
        print(end, "\n")

        return res

    return _wrapper


@nice_print
def chunkChanged(chunkItem: chunk.Chunk, notice, sender):
    # This works just as groupChanged, but is registered on a Chunk, so will only be sent on Chunk changes
    def get_chunk_str(s):
        if isinstance(s, chunk.Chunk):
            return "Chunk"

        if isinstance(s, chunk.Group):
            return "Group"

        if isinstance(s, chunk.Vector):
            return "Vector"

        if isinstance(s, chunk_interface.ChunkParamBase):
            return s.evaluate_type().name

        if isinstance(s, chunk_interface.AttributeSender):
            return "Attribute"

    msg = f"Chunk Changed: {chunkItem.name} -> {notice.__class__.__name__}"

    step = 4
    indent = step

    for n, s in chunkItem.iter_changes():
        msg += "\n" + " " * indent + f"{get_chunk_str(s)}: {s.name} -> {n.__class__.__name__}"

        if isinstance(s, chunk_interface.AttributeSender):
            msg += f": [New Value] = {s.value}"
        indent += step
    print(msg)


@nice_print
def paramChanged(chunkItem: chunk.ChunkParamBase, notice, sender):
    print(chunkItem.name)


@nice_print
def request_delete(chunkItem: chunk.ChunkParamBase, notice, sender):
    print("TRY DELETE", sender.name)


def main(api):
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

    groupA = chunk.Group("GroupA")
    groupA.add_params(params)

    groupA.set_flag(chunk.ChunkFlags.GroupFlags.COLLAPSABLE)

    groupB = chunk.Group("GroupB")
    groupB.add_params(params)
    groupB.set_flag(chunk.ChunkFlags.GroupFlags.COLLAPSABLE)

    groupC = chunk.Group("GroupC")

    groupC.add_params([groupB, groupA])

    # Create the actual chunk - this is in practice just a ChunkParamGroup with some addiditional behaviour
    myChunk = chunk.Chunk("TestParams", category="Tests", state=chunk.ChunkFlags.State.COLLAPSED)
    myChunk.add_params(params)
    myChunk.add_params([groupB, groupA, groupC])

    myChunk.add_callback(chunkChanged)
    myChunk.add_callback(request_delete, None, True, chunk_notice.ChunkNoticeAlias.CHUNK_DELETE)

    myChunk.print_hierarchy()
    # s = groupA.get_param("Slider")
    # if s:
    #     s.request_delete()

    return myChunk
