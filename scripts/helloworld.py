from usd_chunks import chunk, chunk_interface, chunk_notice, chunk_registry

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
def chunkChanged(notice, sender):
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

    msg = f"Chunk Changed: {sender.name} -> {notice.__class__.__name__}"

    step = 4
    indent = step

    for n, s in sender.iter_changes():
        msg += "\n" + " " * indent + f"{get_chunk_str(s)}: {s.name} -> {n.__class__.__name__}"

        if isinstance(s, chunk_interface.AttributeSender):
            msg += f": [New Value] = {s.value}"
        indent += step
    print(msg)


@nice_print
def paramChanged(notice, sender):
    print(sender.name, sender.value)


@nice_print
def paramValueChanged(notice, sender):
    owner = sender.get_owner()
    print(sender.name, sender.value, "owner: ", owner.name if owner else None)


@nice_print
def clickedButton(notice, sender):
    print("Button clicked: ", sender.name)


@nice_print
def primSelectionChanged(notice: chunk_notice.PrimSelectionChangedNotice, sender: chunk_notice.UsdviewSender):
    print("Prim selection changed: ", sender.selectedPrims)


@nice_print
def propSelectionChanged(notice: chunk_notice.PropSelectionChangedNotice, sender: chunk_notice.UsdviewSender):
    print("Prop selection changed: ", sender.selectedProp)


def updateLabel(chunkItem, notice: chunk_notice.PrimSelectionChangedNotice, sender: chunk_notice.UsdviewSender):
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


def main(api):
    # A simple checkbox
    checkbox = chunk.Checkbox("enable")

    # A simple callback. This is called whenever anything on the checkbox is changed
    checkbox.add_callback(paramChanged)

    # A more excplicit callback. Only triggers paramChanged when checkbox.value changes
    checkbox.add_callback(paramValueChanged, sender=checkbox.get_attribute_sender("value"))

    # A slider with horizontal layout
    slider = chunk.Slider("slider")
    # Change the layout flag to modify the layout
    #   slider = chunk.Slider("slider", layout=chunk.ChunkFlags.LayoutFlags.VERTICAL)

    # Example of adding a callback to a param. By default, the callback will be emitted when the value of the parameter changes.
    #   slider.add_callback(sliderChanged)

    # A ChunkParamGroup is a container of ChunkParams or other ChunkParamGroups
    sliderGroup = chunk.Group("SliderGroup")

    ####################
    # FLAGS AND STATES #
    ####################

    # Make the group collapsable and labeled. Flags and states can be combined with bitwise operator:
    #   & (AND), | (OR), ^ (XOR), and ~ (INVERT)
    sliderGroup.set_flag(chunk.ChunkFlags.GroupFlags.COLLAPSABLE | chunk.ChunkFlags.GroupFlags.FRAMED)

    # Similarly, flags and states can be unset
    #   sliderGroup.unset_flag(chunk.ChunkFlags.GroupFlags.COLLAPSABLE)

    # Sets the sliderGroup to collapsed by default.
    sliderGroup.set_state(chunk.ChunkFlags.State.COLLAPSED)

    # Groups default flags are LABELED | FRAMED
    # Chunks default flags are LABELED | FRAMED | COLLAPSABLE
    slider.set_flag(chunk.ChunkFlags.ParamFlags.LABELED)

    # In order to add the same param multiple times to a chunk, use clone(). This will copy the param and its
    # attributes. Connections / callbacks will not be copied.
    # In order to retrive the new instances of the slider, you'll need to retrive it from the group as such:
    #    slider = sliderGroup.getParam("slider1")
    # Duplicate param names will be suffixed with an index. First instance of a param will not be suffixed.
    # This results in these params on the group:
    #   ('slider', 'slider1', 'slider2')
    sliderGroup.add_params([slider, slider.clone(), slider.clone()])
    # Adding a callback on a group will by default call method when anything on the group changes, i.e any child params updated
    #   sliderGroup.add_callback(groupChanged)

    # A vector is an extension of the group, with a single param repeated length times. The suffix replace the default
    # numbered suffix for duplicated params.
    # Accessing params on a vector can be done through __setitem__ (vec["sliderX"] or vec[0]) as well as get_param and similar group functions
    sliderVec = chunk.Vector(
        "SliderVec", param=chunk.Slider(flags=chunk.ChunkFlags.ParamFlags.LABELED), length=3, suffixes="XYZ"
    )

    # A button will call the callback just like any other param, but does not care about its value
    button = chunk.Button("button")
    # A callback for this button
    button.add_callback(clickedButton)

    # Get access to the ChunkRegistry singleton. This is responsible for loading/unloading the chunks, and will send ChunkNoticeAlias.CHUNK_LOADED when a
    # chunk was loaded. It also stores the UsdviewSender, which sends UI related notices such as PRIM_SELECTION_CHANGED and PROP_SELECTION_CHANGED
    reg = chunk_registry.ChunkRegistry()

    # A label - Not directly modifyable in the UI
    label = chunk.Label("label")
    # Add the updateLabel as a callback - this will be called whenever the prim selection changes in UsdView. Any UsdView notices will be sent from
    # the registrys usdview_sender (chunks_notices.UsdviewSender). As the notice is sent from object other than the param, we should enable send_self.
    # This will include the label param in the arguments passed to the method:
    #   method(chunkObject: chunk.Label, notice: Tf.Notice, sender: chunk_notice.UsdViewNotice)
    # without it, modifying the param would be more complicated.
    # By default, send_self is False, and thus the chunk object is not included in the arguments. Usually though, the sender and chunk object are the same so send_self can be ignored.
    label.add_callback(
        updateLabel,
        sender=reg.usdview_sender,
        notice_alias=chunk_notice.ChunkNoticeAlias.PRIM_SELECTION_CHANGED,
        send_self=True,
    )
    # Without access to the usdview_sender, one can enable listen_globally, which will trigger updateLabel for all PRIM_SELECTION_CHANGED notices, regardless of the sender.
    # In this case, the results would be the same, as there should not be more than 1 instance of the usdview_sender, i.e. only the one sender throughout the lifetime of the registry
    # label.add_callback(
    #     updateLabel,
    #     listen_globally=True,
    #     notice_alias=chunk_notice.ChunkNoticeAlias.PRIM_SELECTION_CHANGED,
    #     send_self=True,
    # )

    # A text edit is a parameter for editing strings
    textEdit = chunk.LineEdit("line_edit")

    # A FileBrowser is a LineEdit with a button for launching a File Browser dialog. The filtering of file names
    # is done by appending all extensions to the pattern, so this would result in a filter:
    #   '*.jpg *.png *.tif'
    # If pattern is not set, an asterix '*' will be used. If no extesions are set, the filter will use only the pattern
    fileBrowse = chunk.FileBrowser(
        "line_edit", pattern="*", extensions=[".jpg", ".png", ".tif"], flags=chunk.ChunkFlags.ParamFlags.FILE_EXISTING
    )

    # A simple menu
    menu = chunk.Menu("menu", menu_items=["hello0", "hello1"])

    # A seperator holds no value, is just there for the UI.
    sep = chunk.Separator(layout=chunk.ChunkFlags.LayoutFlags.HORIZONTAL)

    # Create the actual chunk - this is in practice just a ChunkParamGroup with some addiditional behaviour
    myChunk = chunk.Chunk("Hello World", category="Examples")
    myChunk.add_params([label, checkbox, button, sep, textEdit, menu, fileBrowse, sliderGroup, sliderVec])

    # Add a callback. With no arguments, the callback will be called whenever any of tthe children on the chunk is changed
    myChunk.add_callback(chunkChanged)

    # Prints the hierarchy of the chunk. Useful for debugging chunks
    #   myChunk.print_hierarchy()

    x = myChunk.get_param("SliderVec.sliderX")

    return myChunk
