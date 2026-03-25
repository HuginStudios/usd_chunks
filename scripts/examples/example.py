from usd_chunks import chunk, chunk_core, chunk_notice

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
def chunkChanged(owner: chunk.Chunk, notice: chunk_notice.ChunkChangeNotice, sender):
    # This works just as groupChanged, but is registered on a Chunk, so will only be sent on Chunk changes
    def get_chunk_str(s):
        res = "{}"
        if isinstance(s, chunk_core.ProxyParam):
            res = "Proxy[{}]"
        return res.format(s.__class__.__name__)

    msg = []
    msg.append(f"\nChunk Changed: {owner.name} -> {notice.alias.name}")

    step = 4
    owners = {}

    for alias, s in reversed(list(owner.get_dag_node().iter_changes())):
        if not s or isinstance(s, chunk_core.NameSyncedContainer):
            continue

        _owner = s.get_owner()
        if isinstance(_owner, chunk_core.NameSyncedContainer):
            _owner = _owner.get_owner()

        node = s.get_dag_node()
        ln = node._index
        indent, lineno = owners.get(_owner, (step, ln))

        owners[s] = (indent + step, lineno + 1)

        text = " " * indent + f"{get_chunk_str(s)}: {s.name} -> {alias.name}"

        if isinstance(s, chunk_core.AttributeSender):
            text += f": [New Value] = {s.value}"
        msg.insert(lineno, text)

    owner.get_logger().info("\n" + "\n".join(msg))


@nice_print
def clickedButton(owner: chunk.Button, notice, sender):
    owner.get_logger().info(f"Button clicked: {owner.name}")


def updateLabel(owner: chunk.Label, notice: chunk_notice.PrimSelectionChangedNotice, sender: chunk_core.SceneSender):
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
def listChanged(owner: chunk.Chunk, notice, sender):
    for i in owner.get_params(chunk.is_param):
        owner.get_logger().info(i.name, i.value, isinstance(i, chunk_core.ProxyParam))


def validate_name(param: chunk.LineEdit, sender: chunk_core.AttributeSender):
    if not sender.value:
        return sender.old_value
    return sender.value


def nameChanged(owner: chunk.LineEdit, notice, sender):
    if not (s := notice.data) or s.name != "value":
        return

    if not (c := owner.get_chunk()):
        return

    param_name = s.old_value if s.old_value else "listSlider"
    if p := c.get_param(param_name):
        p.name = s.value


def main():

    # Create the actual chunk - this is in practice just a chunk.Group with some addiditional behaviour
    myChunk = chunk.Chunk("Hello World")

    # Add a callback. With no arguments, the callback will be called whenever the chunk or any of its children is changed
    myChunk.add_callback(chunkChanged)

    # A simple checkbox
    checkbox = chunk.Checkbox("enable")
    # Add a parameter to the chunk
    myChunk.add_param(checkbox)

    # A simple callback. This is called whenever anything on the checkbox is changed
    #     checkbox.add_callback(paramChanged)

    # A more excplicit callback. Only triggers paramChanged when checkbox.value changes
    #     checkbox.add_callback(paramValueChanged, sender=checkbox.get_attribute_sender("value"))

    # A float slider
    slider = chunk.Slider("slider")

    # Change the layout flag to modify the layout
    #   slider = chunk.Slider("slider", layout=chunk.ChunkFlags.LayoutFlags.VERTICAL)

    # A Group is a container of other params or groups
    sliderGroup = chunk.Group("SliderGroup")

    # Adding a callback on a group will by default call method when anything on the group changes, i.e any child params updated
    #   sliderGroup.add_callback(groupChanged)

    # One can also be more explicit when adding callbacks, for example only listening to attribute changed notices.
    # Any child of this group will send this notice, and iteratively send it again with every owner as a sender.
    # Sending the notice through the hierarchy allows doing this:
    #   sliderGroup.add_callback(paramValueChanged, notice_alias=chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED)
    # instead of this:
    #   sliderGroup.add_callback(paramValueChanged, sender=sliderGroup.get_param("slider").get_attribute_sender("value"))
    # The difference being we can subscribe to all ATTRIBUTE_CHANGED notices in the notice chain instead of only the ones from the specified sender.

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

    # Sets the sliderGroup to collapsed by default. States are managed like flags, and can be combined similarly
    sliderGroup.set_state(usd_chunks.chunk_flags.ChunkFlags.State.COLLAPSED)

    # In order to add the same param multiple times to a chunk, use clone(). This will copy the param and its
    # attributes. Connections / callbacks will not be copied.
    # In order to retrive the new instances of the slider, you'll need to retrive it from the group as such:
    #    slider = sliderGroup.getParam("slider1")
    # Duplicate param names will be suffixed with an index. First instance of a param will not be suffixed.
    # This results in these params on the group:
    #   ('slider', 'slider1', 'slider2')
    sliderGroup.add_params([slider, slider.clone(), slider.clone()])

    # Add the group of sliders to the chunk
    myChunk.add_param(sliderGroup)

    # A vector is an extension of the group, with a single param repeated length times. The suffix replace the default
    # numbered suffix for duplicated params.
    # Accessing params on a vector can be done through __setitem__ (vec["X"] or vec[0]) as well as get_param and similar group functions
    sliderVec = chunk.Vector("SliderVec", param=chunk.Slider(), length=3, suffixes="XYZ")

    # Modifying valus on a Vector can be dome like so:
    #    sliderVec[0] = 10
    #    sliderVec["X"] = 10
    #    sliderVec.value = (1, 2, 3)

    myChunk.add_param(sliderVec)

    # A button will call 'execute' when clicked, emitting the PARAM_CHANGED notice. No value is used with the button
    button = chunk.Button("button")
    button.add_callback(clickedButton)
    myChunk.add_param(button)

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
    myChunk.add_param(fileBrowse)

    # A simple menu
    menu = chunk.Menu("menu", menu_items=["hello0", "hello1"])
    myChunk.add_param(menu)
    # By default, the menu enforces its value to exist in the menu_items list, so menu.value = "hello3" would fail
    # validation and revert to default value of menu_items[0]

    # A seperator holds no value, is just there for the UI.
    sep = chunk.Separator(layout=usd_chunks.chunk_flags.ChunkFlags.LayoutFlags.HORIZONTAL)
    myChunk.add_param(sep)

    # A label - Not directly modifyable in the UI
    label = chunk.Label("label")
    myChunk.add_param(label)

    # Add the updateLabel as a callback - this will be called whenever the prim selection changes in UsdView. Any UsdView notices will be sent from
    # the chunk_notice.SceneSender. As the label does not emit  the PRIM_SELECTION_CHANGED itself, we need to either specify a sender (the SceneSender in this cas),
    # or listen globally for the notice.
    label.add_callback(
        updateLabel, listen_globally=True, notice_alias=chunk_notice.ChunkNoticeAlias.PRIM_SELECTION_CHANGED
    )

    # Example of a list group. The list groups params are proxied 'size' times. Modifying the parameters of the listGroup will automatically
    # modify all proxies, with the exception of the 'value' attribute
    listGroup = chunk.ListGroup("listGroup")

    listSlider = chunk.Slider("listSlider")
    listGroup.add_param(listSlider)

    # Setting the size of the ListGroup will create N proxies of the params held by the ListGroup
    listGroup.size = 1

    # Accessing a proxy parameter of the ListGroup should be done with get_indexed_param()
    if p := listGroup.get_indexed_param(listSlider, 0):
        assert isinstance(p, chunk.Slider)
        p.value = 10

    # A text edit is a parameter for editing strings. In this case, the callback will rename the listSlider, showing how the proxies will update.
    nameEdit = chunk.LineEdit("list_name")
    nameEdit.add_callback(nameChanged, notice_alias=chunk.ChunkNoticeAlias.ATTRIBUTE_CHANGED)

    # Add a function to be called whenever the 'value' attribute changes. The return value of the function will be the new value of the param
    nameEdit.set_function("value", validate_name)
    myChunk.add_params([nameEdit, listGroup])

    # Prints the hierarchy of the chunk. Useful for debugging chunks
    # myChunk.print_hierarchy()

    return myChunk


def config():
    return chunk.DefaulConfig(category=chunk.StandardCategory.EXAMPLES)
