# Expressions

An expression makes one param read its value from another param. It's written in the
param's value, like `@Box.size@ * 2`.

usd_chunks also works with USD's own `${NAME}` variables. Those are a different thing and
are covered [at the end of this page](#stage-variables-name).

## 🔗 Writing an expression

A reference is `@` + chunk name + `.` + param name + `@`:

```
@Box.size@
```

- **Chunk name** is the node's name in the graph, like `Box` or `Transform1`.
- **Param name** is the param's `name`, not its label. A param called `size` with the
  label "Box Size" is `@Box.size@`.
- The param can be inside a group. `@Box.size@` still finds it.
- A [Vector](chunk-basics.md#groups) can be read as one value: `@Box.translate@`.
- A row of a list is read by its index, from 0: `@Fence.post[2]@` is the third row's
  `post`. It works inside text too: `@Fence.post[2]@/geo`. Delete a row and refs to the
  rows after it are renumbered to follow them. A ref to the deleted row is removed,
  and that param goes back to its default.

## ✏️ Setting one

**Wire it in the graph.** Connecting a value port writes `@Node.param@` for you.

**Number and checkbox params:** right-click the param and choose **Set Expression...**.
Type the expression and press OK. To remove it, right-click and choose **Clear
Expression**. That puts the param back to its default.

**Text params** (like a prim path): type the expression straight into the field.

A param with an expression has a blue, italic label. Hover the label to see the
expression. A number or checkbox field shows the result. A text field shows the
expression.

**From Python:** set `.value` to the text. `.value` gives the result, `.raw_value` gives
the text.

```py
node.get_param("height").value = "@Box.size@ * 2"
node.get_param("height").value      # 20.0
node.get_param("height").raw_value  # "@Box.size@ * 2"
```

## ➗ Maths

On a number param the expression is worked out as a sum. You can use `+`, `-`, `*`, `/`,
`//`, `%`, `**` and brackets, with plain numbers and `@refs@`:

```
@Box.size@ * 2
(@Box.size@ + @Lid.height@) / 2
-@Box.size@
```

- An int param gives a whole number. The decimal part is cut off, so `10.4` becomes `10`.
- Only the operators above work. Functions like `sin()` and comparisons like `>` don't. If
  it can't be worked out (this includes dividing by zero), the param gets the text instead
  of a number.

## 🔤 Building text

On a text param each `@ref@` is swapped for its value. Use it to build a path or a name:

```
@Box.primpath_location@/lid
```

If `Box`'s prim path is `/World/box`, this gives `/World/box/lid`.

A number is written the way Python prints it. A float `3.0` becomes `3.0`, not `3`.

## 📌 Good to know

- **It updates live.** Change `Box.size` and every param that reads it updates.
- **Same graph only.** An expression can only reach chunks in the same graph. A chunk
  inside a ChunkGroup can't read a chunk outside it, or the other way round. To pass a
  value in or out, right-click the param's label and choose **Loft up** (see
  [ChunkGroup](core-chunks.md#chunkgroup)).
- **Renames are followed.** Renaming a chunk or a param updates every expression that
  points at it, right away and in the USD.
- **Wrong names are ignored.** If `@Box.size@` can't be found, the log says
  `Expression ref @Box.size@ not found`. A lone `@Box.size@` then gives the param's
  default.
- **Loops are stopped.** If A reads B and B reads A, the log says `Expression cycle` and
  the params keep their old values.
- **It's saved in the USD.** Each `@ref@` is a USD connection on the chunk prim. A text
  param stores the expression as its value. Any other param stores the result as its value
  and the expression text in `customData` under `chunk:expression`.

## 🌐 Stage variables (`${NAME}`)

These are USD's expression variables, not a usd_chunks feature. They live on the stage,
not on a chunk. You set them with the **Stage Expression** chunk (`stage_expression`): add a row with a name, a
type and a value. It writes them to the session layer, so the file you opened isn't
changed.

USD only reads `${NAME}` in **asset paths** and **variant selections**. The asset path
must be a whole USD expression: in backticks, with the path in double quotes. For example,
in an **Inline USD** chunk (`inline_usd`):

```
#usda 1.0

def Xform "Set" (
    references = @`"/assets/set_${SHOT}.usda"`@
)
{
}
```

Change `SHOT` on the Stage Expression chunk and the reference switches to the other file.
Without the backticks, USD looks for a file literally called `set_${SHOT}.usda`.

The file fields on Reference, Payload and Sublayer don't take a `${NAME}` path yet. They
check that the file exists, and a path with `${SHOT}` in it doesn't.

- `${NAME}` in a param that isn't a file path stays as plain text, and the log warns you.
  Inline USD also logs this warning, but its text goes to USD as it is, so the reference
  still works.
- To run a different part of the graph based on a variable, use the **Stage Expression
  Switch** chunk (`stage_expression_switch`). Pick the variable and type a pattern like `sh01* sh02*`. If it matches,
  the lower input runs. If not, the upper input runs.
- A variable can be a string, int, bool or list of strings. There is no float.
- If you add a variable that didn't exist when the stage was opened, a reference that
  already failed to load won't pick it up. Reopen the stage. Changing an existing variable
  works live.
