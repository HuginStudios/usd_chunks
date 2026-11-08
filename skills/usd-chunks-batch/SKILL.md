---
name: usd-chunks-batch
description: Set up headless usdchunks commands (run, batch, sweep, run --live / send) with param overrides for pipeline and farm jobs, from an existing network .usda. Use when asked to cook a network from the command line, override params, process many files with one network, generate dataset variants, or wire usd_chunks into a pipeline or CI job.
---

# Headless runs and batch jobs

`usdchunks` cooks a network `.usda` with no UI. Every subcommand's `--help` has examples;
the background is in `docs/index.md` ("Interactive or headless") and `docs/scripting.md`
("Live sessions").

## 1. Pick the command

| Job | Command |
| --- | --- |
| Cook once, maybe with new values, write the result | `usdchunks run net.usda [Chunk.param=value ...] -o out.usda` |
| The same network once per input file a glob matches | `usdchunks batch net.usda Chunk.path='in/*.usdc' -o 'out/{1}.usda'` |
| Every variant of a Wedge (datasets, variations) | `usdchunks sweep net.usda --out ./data [--count N]` |
| Many small changes to one open network (look-dev, tuning) | `usdchunks run net.usda --live`, then `usdchunks send net.usda Chunk.param=value --wait` |

## 2. Check the network first

```
usdchunks validate net.usda -v     # every chunk type resolves; exits 1 if one is missing
usdchunks info net.usda            # each ChunkGroup's description and exposed params
```

If `validate` fails, the job's machine is missing chunks: set `USD_CHUNK_DIRS` to the
folders that hold them (`;`-separated on Windows, `:` elsewhere). A chunk with
`requirements` asks before installing on a terminal and only warns in CI. Set
`USD_CHUNK_AUTO_INSTALL=1` to install or `0` to never install (`docs/install.md`).

`run` cooks only what the network's **Output** reaches, the same as the graph. A file with
no Output or View cooks everything; `--all` forces that.

## 3. Write the overrides

Each override is `Name=value`, applied after the file loads:

- `Chunk.param=value`: the chunk's node name, then the param name, for example
  `Transform.tx=5`. A param inside a group is found by its own name, or by its dotted
  group path (`Chunk.group.param`). Its own name is its full name, so a Light's intensity
  is `inputs:intensity`. (Only `send` also matches the end of a name.)
- `param=value` with no chunk: the first chunk (ChunkGroup interfaces first) that has that
  param. Use it for a group's lofted params, as `usdchunks info` lists them.
- Values are read as the param's type. Numbers as written; a checkbox takes
  `true/false/1/0/yes/no/on/off`; a vector or colour takes comma-separated numbers
  (`Color.color=0.1,0.6,0.9`); a string as is.
- **A bad override doesn't stop the run.** It prints `Chunk not found`, `Param not found`
  or `Cannot set` and the network cooks with its saved value. Make the job fail on those
  lines, or check the output yourself.
- Quote anything with `*`, spaces or `{}`: single quotes in bash and PowerShell, double
  quotes in cmd.

## 4. Useful flags

`run`:

- `-o FILE` writes the flattened result without Chunk prims. With `--procedural` it keeps
  them, so the output reopens as a live network.
- `--relative` writes asset paths relative to the output, so a folder of results can move.
- `-p` prints the flattened stage. `--timings` reports where the time went.
- `--bake` writes each cook layer's result to its own file.
- `commit()` hooks (Export USD, Render, KITTI Writer) run once, after everything cooked.

`batch`:

- Every string param holding a glob (`*`, `?`, `[...]`) is a pattern, relative to the
  **network's** folder, not the shell's. The pattern that matches existing files drives
  the batch. `-o` and other patterns are filled from its wildcards, left to right, and
  `{1}` reuses the first match: `assets/fern.usdc` with `-o 'lib/{1}/{1}.usda'` →
  `lib/fern/fern.usda`. `**` keeps subfolders.
- `--driver Chunk.param` picks the driver when more than one pattern matches files.
- Without `-o`, something in the network (Export USD, Render) has to write to a path with a
  wildcard; that's checked before the first run.
- `--jobs N` runs N at a time. Each run is its own `usdchunks run` process.
- `--relative`, `--procedural` and `--all` mean the same as for `run`.
- **Always run `--dry-run` first.** It prints the folder patterns resolve against, every
  file matched and where each result goes, and writes nothing. Show it to the user before
  the real run.
- It exits 1 and lists the failed inputs if any run failed.

`sweep`: `--out DIR` is required. `--only 3,7` regenerates single variants, `--jobs N` runs
them concurrently, and `--verify` re-cooks and diffs against `--out`. Overrides apply to
every variant.

## 5. Hand it over

Give the user the exact command(s), with:

- the environment it needs: a Python with `pxr` (the `standalone` extra or the host's
  Python), `USD_CHUNK_DIRS`, and `USD_CHUNK_AUTO_INSTALL` for CI;
- the `--dry-run` output for a batch;
- for a pipeline script, a wrapper that fails the job on a non-zero exit **and** on the
  "not found" / "Cannot set" lines above.

Try the command on one input first (a `run` with the same overrides, or a batch whose glob
matches one file) before you suggest a farm-sized job.
