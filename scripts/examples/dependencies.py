from usd_chunks import chunk, chunk_notice
import pprint


def param_callback(owner, notice, sender):
    print("Param callback: ", owner.name, owner.value)
    # owner.value *= 2


def run_execute(owner: chunk.Chunk, notice, sender):
    owner.execute()


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


def callback(owner: chunk.Chunk, notice: chunk_notice.ChunkNotice, sender):
    a = owner.get_input(0)
    b = owner.get_input(1)
    c = owner.get_input(2)

    if not (a and b and c):
        print("Inputs failed")
        return

    assert isinstance(a, chunk.Chunk)
    assert isinstance(b, chunk.Chunk)
    assert isinstance(c, chunk.Chunk)

    ap = a.get_param("A_param")
    bp = b.get_param("B_param")
    cp = c.get_param("C_param")
    print(a.name, b.name, c.name, "->", (ap.value, bp.value, cp.value))

def c_callback(owner: chunk.Slider, notice: chunk_notice.ChunkNotice, sender):
    a = owner.get_input(0)
    b = owner.get_input(1)

    if not (a and b):
        print("Inputs failed")
        return

    assert isinstance(a, chunk.Slider)
    assert isinstance(b, chunk.Slider)

    res = a.value * b.value
    owner.value = res
    print("Setting C: ", res)


def eval_a(param, value):
    res = value * 2
    print(f"EVAL {param.name}", value, "->", res)
    return res


def eval_b(param, value):
    pass


def main():
    a = chunk.Chunk("A")
    a_param = chunk.Slider("A_param")
    a.add_param(a_param)

    b = chunk.Chunk("B")
    b_param = chunk.Slider("B_param")
    b.add_param(b_param)

    c = chunk.Chunk("C")
    c_param = chunk.Slider("C_param")
    c.add_param(c_param)
    c_param.add_input(a_param)
    c_param.add_input(b_param)
    c_param.add_callback(c_callback)

    d = chunk.Chunk("D")
    d_param = chunk.Button("Execute")
    d.add_param(d_param)
    d.add_callback(callback)
    d.add_callback(run_execute, sender=d_param)

    d.add_input(a)
    d.add_input(b)
    d.add_input(c)

    return [a, b, c, d]


def config():
    return chunk.DefaulConfig(chunk.StandardCategory.EXAMPLES)
