from usd_chunks import chunk, chunk_notice
import pprint


def param_callback(owner, notice, sender):
    print("Param callback: ", owner.name, notice.alias)


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
    # print(sender)
    print("CALLBACK")

    # node = owner.get_dag_node()
    # inputs = get_inputs_dict(node)
    # print(print_hierarchy(inputs))
    # print(notice.data.name)


def eval_a(slider, value):
    return value


def main():
    a = chunk.Chunk("A")
    a_param = chunk.Slider("A_param")
    # a_param.set_evaluator("value", eval_a)

    # a_param.get_attribute_sender("value")._evaluation_function = evaluate_a
    # a_param.add_callback
    a.add_param(a_param)

    b = chunk.Chunk("B")
    b_param = chunk.Slider("B_param")
    b.add_param(b_param)

    # c = chunk.Chunk("C")
    # c_param = chunk.Slider("C_param")
    # c.add_param(c_param)
    # c.add_input(a)

    d = chunk.Chunk("D")
    d_param = chunk.Button("Execute")
    d.add_param(d_param)
    d.add_callback(callback)

    d.add_input(a)
    d.add_input(b)
    # d.add_input(c)

    return [a, b, d]


def config():
    return chunk.DefaulConfig(chunk.StandardCategory.EXAMPLES)
