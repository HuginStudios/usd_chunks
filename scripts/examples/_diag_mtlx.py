"""Diagnostic for a MaterialX wire that does not drive -- paste into usdview's interpreter.

    exec(open(r"C:\\Users\\danen\\hugin\\usd_chunks\\scripts\\examples\\_diag_mtlx.py").read())

A graph wire only reaches the real shaders if BOTH hops are authored:

  hop 1  <downstream chunk>.inputs:<port>  ->  <upstream chunk> prim
  hop 2  <upstream chunk>.outputs:<name>   ->  <upstream real shader>.outputs:<name>

wire_shader_input walks hop 1 to find the upstream chunk, then reads hop 2 off that
chunk's outputs: attr to learn which shader to connect to. If hop 2 is missing it clears
the downstream shader input instead, and nothing drives.
"""

from usd_chunks import chunk_registry

MODULE = "mtlx_shader"


def diagnose():
    reg = chunk_registry.ChunkRegistry()
    scene = reg.scene
    stage = scene.get_stage()

    chunks = [c for c in scene.get_chunks() if c.get_param("nodedef") is not None]
    print(f"MaterialX chunks: {len(chunks)}")

    for c in chunks:
        prim_path = None
        for attr in ("_exec_prim_path", "prim_path"):
            if p := getattr(c, attr, None):
                prim_path = str(p)
                break
        prim = stage.GetPrimAtPath(prim_path) if prim_path else None

        print()
        print("=" * 72)
        print("chunk    :", c.name, "|", c.get_param("nodedef").value)
        print("chunk prim:", prim_path, "valid:", bool(prim and prim.IsValid()))
        print("Path      :", c.get_param("prim_path").value)

        if not (prim and prim.IsValid()):
            continue

        print("-- hop 2: this chunk's outputs -> its real shader --")
        found_out = False
        for a in prim.GetAttributes():
            n = a.GetName()
            if n.startswith("outputs:") and n != "outputs:chunk":
                found_out = True
                print(f"   {n} ({a.GetTypeName()}) -> {[str(x) for x in a.GetConnections()] or 'NO CONNECTIONS'}")
        if not found_out:
            print("   NO outputs: ATTRS AT ALL")

        print("-- hop 1: this chunk's connected inputs --")
        found_in = False
        for a in prim.GetAttributes():
            n = a.GetName()
            if n.startswith("inputs:") and n != "inputs:chunk" and a.GetConnections():
                found_in = True
                print(f"   {n} <- {[str(x) for x in a.GetConnections()]}")
        if not found_in:
            print("   (no connected inputs)")

        print("-- the real shader it authored --")
        path = c.get_param("prim_path").value
        for candidate in (f"{path}/shader", path):
            sh = stage.GetPrimAtPath(candidate)
            if sh and sh.IsValid() and sh.GetTypeName() == "Shader":
                print("   ", candidate, sh.GetAttribute("info:id").Get())
                for a in sh.GetAttributes():
                    if a.GetName().startswith("inputs:") and a.GetConnections():
                        print(f"      {a.GetName()} <- {[str(x) for x in a.GetConnections()]}")
                break

        # An expression here means _author_connection drove the param instead of the
        # shader attr -- it resolves against a *param* on the source, which an output
        # authored as a bare attr does not have.
        for name in ("base_color", "Base:base_color"):
            if p := c.get_param(name.split(":")[-1]):
                raw = getattr(p, "raw_value", None)
                if isinstance(raw, str) and "@" in raw:
                    print("   base_color expression:", raw)
                break


diagnose()
