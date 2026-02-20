from usd_chunks import chunk


# def run_execute(owner: chunk.Chunk, notice, sender):
#     owner.execute()

def print_hierarchy(data, indent=0):
    istr = ""
    msg = ""
    if indent:
        sepstr = "|"
        if indent > 1:
            sepstr = "| " * (indent - 1) + "|"
        istr += f"{sepstr}\n{sepstr}{'_' * 2}"

    for node, inputs in data.items():
        msg += istr + node.name + "\n"
        if inputs:
            msg += print_hierarchy(inputs, indent + 1)
    return msg


def get_inputs_dict(node):
    inputs = {}
    for n in node.get_inputs():
        inputs.update(get_inputs_dict(n))
    return {node: inputs}


def callback(owner: chunk.Chunk, notice, sender):
    print("Calling D")
    # a = owner.get_dependency(0)
    # b = owner.get_dependency(1)
    # c = owner.get_dependency(2)

    # if not (a and b and c):
    #     print("Inputs failed")
    #     return

    # assert isinstance(a, chunk.Chunk)
    # assert isinstance(b, chunk.Chunk)
    # assert isinstance(c, chunk.Chunk)

    # ap = a.get_param("A_param")
    # bp = b.get_param("B_param")
    # cp = c.get_param("C_param")

    # assert isinstance(ap, chunk.Slider)
    # assert isinstance(bp, chunk.Slider)
    # assert isinstance(cp, chunk.Slider)

    # print(f"({a.name} * {b.name} = {c.name}): {ap.value} * {bp.value} = {cp.value}")


def c_function(owner: chunk.Slider, attrib):
    a = owner.get_dependency(0)
    b = owner.get_dependency(1)

    if not (a and b):
        print("Inputs failed")
        return

    assert isinstance(a, chunk.Slider)
    assert isinstance(b, chunk.Slider)

    return a.value * b.value

    ## ==================

    # a = owner.get_dependency(0)
    # if not a:
    #     print("Inputs failed")
    #     return

    # assert isinstance(a, chunk.Slider)
    # return a.value

# def c_callback(pwner, notice, sender):
#     print("C CALLBACK", notice)


def chunk_changed(owner, notice, sender):
    print("Chunk Changed", owner.name)

def main():
    a = chunk.Chunk("A")
    a_param = chunk.Slider("A_param")
    a.add_param(a_param)

    b = chunk.Chunk("B")
    b_param = chunk.Slider("B_param")
    b_param.add_dependency(a_param)
    b.add_param(b_param)
    # b.add_dependency(a)

    c = chunk.Chunk("C")
    c_param = chunk.Slider("C_param")
    c_param.add_dependency(b_param)
    c.add_param(c_param)

    # c_param.add_dependency(a_param)
    # c_param.set_function("value", c_function)

    a.add_callback(chunk_changed)
    b.add_callback(chunk_changed)
    c.add_callback(chunk_changed)

    d = chunk.Chunk("D")
    d_param = chunk.Button("Execute")
    d.add_param(d_param)

    # d.add_callback(callback, notice_alias=chunk.ChunkNoticeAlias.PARAM_CHANGED)
    # d.add_callback(run_execute, sender=d_param)

    d.add_dependency(a)
    d.add_dependency(b)
    d.add_dependency(c)

    return [a, b, c, d]
    # return [a,b,c]


def config():
    return chunk.DefaulConfig(chunk.StandardCategory.EXAMPLES, load=True)
