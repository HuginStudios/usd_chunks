from usd_chunks import chunk, chunk_qt, chunk_notice, chunk_registry


def groupChanged(chunkItem: chunk.ChunkParamGroup, notice, sender):
    """When a group changes, the chunkItem and sender will be the group object.
    The ChunkParamGroup holds the changes to the last changes parameters, we can use this to find what was changed on the group

    Args:
        chunkItem (chunk.ChunkParamGroup): The changed group
        notice (Tf.Notice): The notice that was sent
        sender (Any): The sender object. Will be the same as chunkItem in most cases
    """
    msg = f"Group Changed: {chunkItem.name}: "
    lNotice, lSender = chunkItem.get_last_change()

    step = 4
    indent = step
    while chunk_notice.is_notice_type(lNotice, chunk_notice.NoticeType.GROUP_CHANGED):
        msg += "\n" + " " * indent + f"Group: {lSender.name}"
        indent += step
        lNotice, lSender = lSender.get_last_change()

    if chunk_notice.is_notice_type(lNotice, chunk_notice.NoticeType.PARAM_CHANGED):
        msg += "\n" + " " * indent + f"Param: {lSender.name}. Value: {lSender.value}"

    print(msg)


def chunkChanged(chunkItem: chunk.Chunk, notice, sender):
    # This works just as groupChanged, but is registered on a Chunk, so will only be sent on Chunk changes
    msg = f"Chunk Changed: {chunkItem.name}: "
    lNotice, lSender = chunkItem.get_last_change()

    step = 4
    indent = step
    while chunk_notice.is_notice_type(lNotice, chunk_notice.NoticeType.GROUP_CHANGED):
        msg += "\n" + " " * indent + f"Group: {lSender.name}"
        indent += step
        lNotice, lSender = lSender.get_last_change()

    if chunk_notice.is_notice_type(lNotice, chunk_notice.NoticeType.PARAM_CHANGED):
        msg += "\n" + " " * indent + f"Param: {lSender.name}. Value: {lSender.value}"

    print(msg)


def sliderChanged(chunkItem: chunk.ChunkParam, notice, sender):
    print("Slider changed: ", chunkItem.name)


def clickedButton(chunkItem: chunk.ChunkParam, notice, sender):
    print("Button clicked: ", chunkItem.name)


def primSelectionChanged(
    chunkItem, notice: chunk_notice.PrimSelectionChangedNotice, sender: chunk_notice.UsdviewNotice
):
    print("Prim selection changed: ", sender.selectedPrims)


def propSelectionChanged(
    chunkItem, notice: chunk_notice.PropSelectionChangedNotice, sender: chunk_notice.UsdviewNotice
):
    print("Prop selection changed: ", sender.selectedProp)


def updateLabel(
    chunkItem: chunk.ChunkParam, notice: chunk_notice.PropSelectionChangedNotice, sender: chunk_notice.UsdviewNotice
):
    """An example callback function that updates the value of the chunkItem to the selected prim path when a new selection occurs

    Args:
        chunkItem (chunk.ChunkParam): The ChunkParamaBase object that was used to to add this callback
        notice (chunk_notice.PropSelectionChangedNotice): The notice object. This is just an identifier for the type of notice sent
        sender (chunk_notice.UsdviewSelectionNotice): The actual sender
    """
    if not sender.selectedPrims:
        return
    path = sender.selectedPrims[0].GetPath()
    if path.isEmpty:
        return
    chunkItem.value = str(path)


"""
A callback function registered to a ChunkParamBase object (Chunk, ChunkParam, ChunkParamGroup), should take 3 arguments:
    1 - The ChunkParamBase object that was used to add the callback
    2 - The notice that was sent
    3 - The sender object. In most cases, this is the same object as 1, but for stage and UsdView notices, this could be the stage or UsdviewNotice objects.
"""


def main(api):
    # A simple checkbox
    checkbox = chunk.ChunkParam()
    checkbox.param_type = chunk.ChunkParamType.CHECKBOX
    checkbox.layout_flags = chunk.ChunkLayoutFlags.HORIZONTAL
    checkbox.name = "enable"

    # A slider
    slider = chunk.ChunkParam()
    slider.param_type = chunk.ChunkParamType.FLOAT_SLIDER
    slider.layout_flags = chunk.ChunkLayoutFlags.HORIZONTAL
    slider.name = "slider"
    # Example of adding a callback to a param. By default, the callback will be emitted when the value of the parameter changes
    # Contrary to adding the callback to the chunk, this will automatically connect to the param and only call mathod when this param changes
    slider.add_callback(sliderChanged)

    # A ChunkParamGroup is a container of ChunkParams or other ChunkParamGroups
    sliderGroup = chunk.ChunkParamGroup()
    # It should be safe to add the same param multiple times. The group will create copies, rename duplicates and rewire the connections.
    # When modifying the original slider, only the first instance of the slider will be modified. In order to retrive the copies, you'll need to 
    # retrive it from the group as such:
    #   sliderGroup.getParam("slider1")
    sliderGroup.add_params([slider, slider, slider])
    # Adding a callback on a group will by default call method when anything on the group changes, i.e any child params updated
    sliderGroup.add_callback(groupChanged)

    # A button will call the callback just like any other param, but does not care about its value
    button = chunk.ChunkParam()
    button.param_type = chunk.ChunkParamType.BUTTON
    button.name = "button"
    # A callback for this button
    button.add_callback(clickedButton)

    # A label - Not directly modifyable in the UI
    label = chunk.ChunkParam()
    label.param_type = chunk.ChunkParamType.LABEL
    label.name = "label"
    # Add the updateLabel as a callback - this will be called whenever the selection changes in UsdView. We should listen_globally here as the notice is sent from object other than the param
    label.add_callback(updateLabel, listen_globally=True, notice_type=chunk_notice.NoticeType.PRIM_SELECTION_CHANGED)

    # A text edit is a parameter for editing strings
    textEdit = chunk.ChunkParam()
    textEdit.param_type = chunk.ChunkParamType.LINEEDIT
    textEdit.name = "line_edit"

    # A simple menu
    menu = chunk.ChunkParam()
    menu.param_type = chunk.ChunkParamType.STRING_MENU
    menu.name = "menu"
    menu.menu_items = ["hello0", "hello2"]

    # A seperator holds no value, is just there for the UI
    sep = chunk.ChunkParam()
    sep.param_type = chunk.ChunkParamType.SEPARATOR
    sep.layout_flags = chunk.ChunkLayoutFlags.HORIZONTAL

    # Create the actual chunk - this is in practice just a ChunkParamGroup with some addiditional behaviour
    myChunk = chunk.Chunk()
    myChunk.name = "Hello World"  # Name sets the label of the chunk
    myChunk.category = "Examples"  # Category sets the category this chunk should be desplayed in
    myChunk.add_params([label, checkbox, sliderGroup, button, sep, textEdit, menu])  # Add parameters to this chunk

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
