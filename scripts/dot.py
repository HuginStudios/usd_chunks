from usd_chunks import chunk


def execute(node):
    stage = node.get_stage()
    stage.GetEditTarget().GetLayer().Clear()


def main():
    return chunk.Chunk("Dot")


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.UTILS,
        description="Pure passthrough — routes the execution chain without modifying the stage. Fan-out by connecting multiple downstream chunks to the single outputs:chunk.",
    )
