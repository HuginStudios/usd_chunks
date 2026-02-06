from usd_chunks import chunk, chunk_core, chunk_notice, chunk_registry

from functools import wraps

import usd_chunks.chunk_flags


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
def chunkChanged(owner, notice, sender):
    # This works just as groupChanged, but is registered on a Chunk, so will only be sent on Chunk changes
    def get_chunk_str(s):
        res = "{}"
        if isinstance(s, chunk_core.ProxyParam):
            res = "Proxy[{}]"
        if isinstance(s, chunk.Chunk):
            return res.format("Chunk")

        if isinstance(s, chunk.Group):
            return res.format("Group")

        if isinstance(s, chunk.Vector):
            return res.format("Vector")

        if isinstance(s, chunk_core.ChunkParamBase):
            return res.format(s.evaluate_type().name)

        if isinstance(s, chunk_core.AttributeSender):
            return res.format("Attribute")

    msg = f"Chunk Changed: {sender.name} -> {notice.__class__.__name__}"

    step = 4
    indent = step

    for n, s in sender.iter_changes():
        msg += "\n" + " " * indent + f"{get_chunk_str(s)}: {s.name} -> {n.__class__.__name__}"

        if isinstance(s, chunk_core.AttributeSender):
            msg += f": [New Value] = {s.value}"
        indent += step
    print(msg)


@nice_print
def paramChanged(owner, notice, sender):
    print(sender.name, sender.value)


@nice_print
def paramValueChanged(owner, notice, sender: chunk_notice.ChunkNoticePropagator):
    print(notice, sender)


@nice_print
def clickedButton(owner, notice, sender):
    print("Button clicked: ", owner.name)


@nice_print
def primSelectionChanged(owner, notice: chunk_notice.PrimSelectionChangedNotice, sender: chunk_notice.SceneSender):
    print("Prim selection changed: ", notice.data.value)


@nice_print
def propSelectionChanged(owner, notice: chunk_notice.PropSelectionChangedNotice, sender: chunk_notice.SceneSender):
    print("Prop selection changed: ", notice.data.value)


def updateLabel(owner, notice: chunk_notice.PrimSelectionChangedNotice, sender: chunk.ChunkParamBase):
    """An example callback function that updates the value of the chunkItem to the selected prim path when a new selection occurs

    Args:
        chunkItem (chunk.ChunkParam): The ChunkParamaBase object that was used to to add this callback
        notice (chunk_notice.PropSelectionChangedNotice): The notice object. This is just an identifier for the type of notice sent
        sender (chunk_notice.UsdviewSelectionNotice): The actual sender
    """
    if not notice.data:
        return
    path = notice.data[0].GetPath()
    if path.isEmpty:
        return

    owner.value = str(path)


@nice_print
def listChanged(owner, notice, sender):
    for i in owner.get_params():
        if isinstance(i, chunk_core.ChunkParamBase):
            print(i.name, i.value, isinstance(i, chunk_core.ProxyParam))


def nameChanged(owner, notice, sender: chunk.LineEdit):
    on, os = sender.get_last_change()
    if not os:
        return

    c = owner.get_chunk()
    if not os.old_value:
        p = c.get_param("listSlider")
    else:
        p = c.get_param(os.old_value)

    if not p:
        return

    p.name = os.value


def main():
    # A simple checkbox
    checkbox = chunk.Checkbox("enable")

    # A simple callback. This is called whenever anything on the checkbox is changed
    #     checkbox.add_callback(paramChanged)

    # A more excplicit callback. Only triggers paramChanged when checkbox.value changes
    #     checkbox.add_callback(paramValueChanged, sender=checkbox.get_attribute_sender("value"))

    # A slider with horizontal layout
    slider = chunk.Slider("slider")

    # Change the layout flag to modify the layout
    #   slider = chunk.Slider("slider", layout=chunk.ChunkFlags.LayoutFlags.VERTICAL)

    # A ChunkParamGroup is a container of ChunkParams or other ChunkParamGroups
    sliderGroup = chunk.Group("SliderGroup")

    # Adding a callback on a group will by default call method when anything on the group changes, i.e any child params updated
    #   sliderGroup.add_callback(groupChanged)

    # One can also be more explicit when adding callbacks, for example only listening to attribute changed notices.
    # Any child of this group will send this notice, and iteratively send it again with every owner as a sender.
    # Sending the notice through the hierarchy allows doing this:
    #   sliderGroup.add_callback(paramValueChanged, notice_alias=chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED)
    # instead of this:
    #   sliderGroup.add_callback(paramValueChanged, sender=sliderGroup.get_param("slider").get_attribute_sender("value"))
    # The difference being we can subscribe to all ATTRIBUTE_CHANGED notices instead of only the ones from the specified sender.

    ####################
    # FLAGS AND STATES #
    ####################

    # Make the group collapsable and labeled. Flags and states can be combined with bitwise operator:
    #   & (AND), | (OR), ^ (XOR), ~ (NOT)
    sliderGroup.set_flag(
        usd_chunks.chunk_flags.ChunkFlags.GroupFlags.COLLAPSABLE | usd_chunks.chunk_flags.ChunkFlags.GroupFlags.FRAMED
    )

    # Similarly, flags and states can be unset
    #   sliderGroup.unset_flag(chunk.ChunkFlags.GroupFlags.COLLAPSABLE)

    # Sets the sliderGroup to collapsed by default.
    sliderGroup.set_state(usd_chunks.chunk_flags.ChunkFlags.State.COLLAPSED)

    # Groups default layout flags are LABELED | FRAMED
    # Chunks default layout flags are LABELED | FRAMED | COLLAPSABLE

    # In order to add the same param multiple times to a chunk, use clone(). This will copy the param and its
    # attributes. Connections / callbacks will not be copied.
    # In order to retrive the new instances of the slider, you'll need to retrive it from the group as such:
    #    slider = sliderGroup.getParam("slider1")
    # Duplicate param names will be suffixed with an index. First instance of a param will not be suffixed.
    # This results in these params on the group:
    #   ('slider', 'slider1', 'slider2')
    sliderGroup.add_params([slider, slider.clone(), slider.clone()])

    # A vector is an extension of the group, with a single param repeated length times. The suffix replace the default
    # numbered suffix for duplicated params.
    # Accessing params on a vector can be done through __setitem__ (vec["sliderX"] or vec[0]) as well as get_param and similar group functions
    sliderVec = chunk.Vector("SliderVec", param=chunk.Slider(), length=3, suffixes="XYZ")
    sliderVec[0] = 10

    # # A button will call the callback just like any other param, but does not care about its value
    button = chunk.Button("button")
    button.add_callback(clickedButton)

    # Get access to the ChunkRegistry singleton. This is responsible for loading/unloading the chunks, and will send ChunkNoticeAlias.CHUNK_LOADED when a
    # chunk was loaded. It also stores the SceneSender, which sends UI related notices such as PRIM_SELECTION_CHANGED and PROP_SELECTION_CHANGED
    #   reg = chunk_registry.ChunkRegistry()
    # Accessing the scene in this function may result in some errors as the stage is not properly loaded at the time this function
    # runs. So access in to the reg.scene should be reserved to callbacks

    # A label - Not directly modifyable in the UI
    label = chunk.Label("label")

    # Add the updateLabel as a callback - this will be called whenever the prim selection changes in UsdView. Any UsdView notices will be sent from
    # the chunk_notice.SceneSender. As the label does not emit send the PRIM_SELECTION_CHANGED itself, we need to either specify a sender (the SceneSender in this cas),
    # or listen globally for the notice.
    # label.add_callback(
    #     updateLabel, listen_globally=True, notice_alias=chunk_notice.ChunkNoticeAlias.PRIM_SELECTION_CHANGED
    # )
    label.add_callback(
        updateLabel, listen_globally=True, notice_alias=chunk_notice.ChunkNoticeAlias.PRIM_SELECTION_CHANGED
    )

    # Example of a list group. The list groups params are proxied 'size' times. Modifying the parameters of the listGroup will automatically
    # modify all proxies, with the exception of the 'value' attribute
    listSlider = chunk.Slider("listSlider")
    listSlider.add_callback(paramValueChanged)

    nestedListGroup = chunk.Group("nestedGroup")
    nestedListGroup.add_params([listSlider])

    listGroup = chunk.ListGroup("listGroup")
    listGroup.add_params([nestedListGroup])
    listGroup.size = 2

    if p := listGroup.get_indexed_param(listSlider, 1):
        p.value = 10

    # A text edit is a parameter for editing strings. In this case, the callback will rename the listSlider, showing how the proxies will update.
    nameEdit = chunk.LineEdit("list_name")
    nameEdit.add_callback(nameChanged)

    # A FileBrowser is a LineEdit with a button for launching a File Browser dialog. The filtering of file names
    # is done by appending all extensions to the pattern, so this would result in a filter:
    #   '*.jpg *.png *.tif'
    # If pattern is not set, an asterix '*' will be used. If no extensions are set, the filter will use only the pattern
    fileBrowse = chunk.FileBrowser(
        "line_edit",
        pattern="*",
        extensions=[".jpg", ".png", ".tif"],
        flags=usd_chunks.chunk_flags.ChunkFlags.ParamFlags.FILE_EXISTING
        | usd_chunks.chunk_flags.ChunkFlags.ParamFlags.LABELED,
    )

    # A simple menu
    menu = chunk.Menu("menu", menu_items=["hello0", "hello1"])
    # By default, the menu enforces its value to exist in the menu_items list, so menu.value = "hello3" would fail
    # validation and revert to default value of menu_items[0]

    # A seperator holds no value, is just there for the UI.
    sep = chunk.Separator(layout=usd_chunks.chunk_flags.ChunkFlags.LayoutFlags.HORIZONTAL)

    # Create the actual chunk - this is in practice just a chunk.Group with some addiditional behaviour
    myChunk = chunk.Chunk("Hello World")
    myChunk.add_params([label, checkbox, button, sep, menu, fileBrowse, sliderGroup, sliderVec, nameEdit, listGroup])

    # Add a callback. With no arguments, the callback will be called whenever any of tthe children on the chunk is changed
    myChunk.add_callback(chunkChanged)

    # Prints the hierarchy of the chunk. Useful for debugging chunks
    # myChunk.print_hierarchy()

    return myChunk


def config():
    return chunk.DefaulConfig(load=True, category=chunk.StandardCategory.EXAMPLES)
