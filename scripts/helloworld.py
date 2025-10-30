from usd_chunks import chunk, chunk_interface, chunk_qt, chunk_notice, chunk_registry

# from typing import reveal_type

from functools import wraps


def nice_print(func):
    @wraps(func)
    def _wrapper(*args, **kwargs):
        begin = f"{'-' * 10}* {func.__qualname__} *{'-' * 10}"
        end = '-' * len(begin)

        print(begin)
        res = func(*args, **kwargs)
        print(end,"\n")

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
def checkboxChanged(chunkItem: chunk.ChunkParamBase, notice, sender):
    print(chunkItem.name, hash(chunkItem))


@nice_print
def sliderChanged(chunkItem: chunk.ChunkParamBase, notice, sender):
    print(chunkItem.name, chunkItem.value, hash(chunkItem))


# def clickedButton(chunkItem: chunk.ChunkParamBase, notice, sender):
#     print("Button clicked: ", chunkItem.name)


# def primSelectionChanged(
#     chunkItem, notice: chunk_notice.PrimSelectionChangedNotice, sender: chunk_notice.UsdviewNotice
# ):
#     print("Prim selection changed: ", sender.selectedPrims)


# def propSelectionChanged(
#     chunkItem, notice: chunk_notice.PropSelectionChangedNotice, sender: chunk_notice.UsdviewNotice
# ):
#     print("Prop selection changed: ", sender.selectedProp)


# def updateLabel(
#     chunkItem: chunk.ChunkParamBase, notice: chunk_notice.PropSelectionChangedNotice, sender: chunk_notice.UsdviewNotice
# ):
#     """An example callback function that updates the value of the chunkItem to the selected prim path when a new selection occurs

#     Args:
#         chunkItem (chunk.ChunkParam): The ChunkParamaBase object that was used to to add this callback
#         notice (chunk_notice.PropSelectionChangedNotice): The notice object. This is just an identifier for the type of notice sent
#         sender (chunk_notice.UsdviewSelectionNotice): The actual sender
#     """
#     if not sender.selectedPrims:
#         return
#     path = sender.selectedPrims[0].GetPath()
#     if path.isEmpty:
#         return
#     chunkItem.value = str(path)


"""
A callback function registered to a ChunkParamBase object (Chunk, ChunkParam, ChunkParamGroup), should take 3 arguments:
    1 - The ChunkParamBase object that was used to add the callback
    2 - The notice that was sent
    3 - The sender object. In most cases, this is the same object as 1, but for stage and UsdView notices, this could be the stage or UsdviewNotice objects.
"""


def main(api):
    # A simple checkbox
    checkbox = chunk.ChunkParam(chunk_interface.CheckboxTemplate(), "enable")

    # A slider
    slider = chunk.ChunkParam(
        chunk_interface.SliderTemplate(), "slider", layout_flags=chunk.Flags.ChunkLayoutFlags.HORIZONTAL
    )
    # Example of adding a callback to a param. By default, the callback will be emitted when the value of the parameter changes
    # Contrary to adding the callback to the chunk, this will automatically connect to the param and only call mathod when this param changes
    slider.add_callback(sliderChanged)

    # A ChunkParamGroup is a container of ChunkParams or other ChunkParamGroups
    sliderGroup = chunk.ChunkParamGroup(params=[slider, slider, slider])
    # It should be safe to add the same param multiple times. The group will create copies, rename duplicates and rewire the connections.
    # When modifying the original slider, only the first instance of the slider will be modified. In order to retrive the copies, you'll need to
    # retrive it from the group as such:
    #   sliderGroup.getParam("slider1")
    # # Adding a callback on a group will by default call method when anything on the group changes, i.e any child params updated
    sliderGroup.add_callback(groupChanged)

    # A button will call the callback just like any other param, but does not care about its value
    button = chunk.ChunkParam(chunk_interface.ButtonTemplate(), "button")
    # # A callback for this button
    button.add_callback(clickedButton)

    # A label - Not directly modifyable in the UI
    label = chunk.ChunkParam(chunk_interface.LabelTemplate(), "label")
    # Add the updateLabel as a callback - this will be called whenever the selection changes in UsdView. We should listen_globally here as the notice is sent from object other than the param
    label.add_callback(updateLabel, listen_globally=True, notice_type=chunk_notice.NoticeType.PRIM_SELECTION_CHANGED)

    # A text edit is a parameter for editing strings
    textEdit = chunk.ChunkParam(chunk_interface.LineEditTemplate(), "line_edit")

    # A simple menu
    menu = chunk.ChunkParam(chunk_interface.MenuTemplate(menu_items=["hello0", "hello1"]), "menu")

    # A seperator holds no value, is just there for the UI
    sep = chunk.ChunkParam(chunk_interface.SeparatorTemplate(), layout_flags=chunk.ChunkLayoutFlags.HORIZONTAL)

    # Create the actual chunk - this is in practice just a ChunkParamGroup with some addiditional behaviour
    myChunk = chunk.Chunk(
        "Hello World", category="Examples", children=[label, checkbox, sliderGroup, button, sep, textEdit, menu]
    )
    # Parameters can also be added after construction
    # myChunk.add_params([label, checkbox, sliderGroup, button, sep, textEdit, menu])

    # Add a callback. With no arguments, the callback will be called whenever anything on the chunk is changed
    myChunk.add_callback(chunkChanged)

    # Example showing how to add a callback to PRIM_SELECTION_CHANGED notices. These are not emitted on the Chunk, so need to listen globally
    myChunk.add_callback(
        primSelectionChanged, listen_globally=True, notice_type=chunk_notice.NoticeType.PRIM_SELECTION_CHANGED
    )

    # # Similarly as above, but we can subscribe to the actual sender. It should be safe to access the ChunkRegistry here
    reg = chunk_registry.ChunkRegistry()
    myChunk.add_callback(
        propSelectionChanged, sender=reg.stage_sender, notice_type=chunk_notice.NoticeType.PROP_SELECTION_CHANGED
    )

    # Return the chunk
    return myChunk

