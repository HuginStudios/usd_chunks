from usd_chunks import chunk


class MyCustomChunk(chunk.Chunk):
    def __init__(self, startCount=10):
        super().__init__(name="MyCustomChunk")

        self.counter = startCount

        p1 = chunk.Button("click")
        p1.add_callback(self.increase_count, send_on_create=False)
        self.add_param(p1)

    def increase_count(self, notice, sender):
        self.counter += 1
        self.get_logger().info(f"Clicked! Counter at {self.counter}")


def main():
    return MyCustomChunk(7)


def config():
    return chunk.DefaulConfig(chunk.StandardCategory.EXAMPLES)
