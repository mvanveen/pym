# pyvim

A vim clone in a single Python file, because why not.

**bold** 

```
uv run pyvim.py [file]
```

## What it is

Modal editor — normal, insert, visual, visual-line, command, search — with splits, a file explorer, syntax highlighting, and a live Python eval runtime baked in. Around 1900 lines of stdlib + pygments. No Rust, no Electron, no LSP daemon that eats 400MB of RAM.

The interesting part isn't the vim emulation. It's that the editor is a live Python object you can reach into while it's running.

## Modes

| Mode | Enter | Exit |
|------|-------|------|
| Normal | `Esc` | — |
| Insert | `i a o I A O` | `Esc` |
| Visual | `v` | `Esc` or `v` |
| Visual Line | `V` | `Esc` or `V` |
| Command | `:` | `Enter` / `Esc` |
| Search | `/` `?` | `Enter` / `Esc` |
| Explorer | `:Ex` | `q` / `Enter` |

## Motions and operators

Standard vim motions: `h j k l`, `w W b B e E`, `0 ^ $`, `gg G`, `{ }`, `f F t T ; ,`, `' `` marks`.

Operators: `d c y > < ~ u U` — all work with motions (`dw`, `ci"`, `y3j`, etc.) and in visual mode.

Everything you'd reach for first: `x X dd cc yy D C`, `p P`, `u` undo, `Ctrl-R` redo, `.` repeat, `*` `#` word search.

## Splits

```
:sp            horizontal split
:vsp           vertical split
Ctrl-W h/j/k/l navigate panes
Ctrl-W w       cycle panes
Ctrl-W q       close pane
```

## Commands

```
:w [file]      write
:q             close pane (quit if last)
:qa            quit all (checks for unsaved changes)
:qa!           force quit all
:e <file>      open file
:Ex            file explorer
:jp            toggle JSON structure preview (.json buffers)
:set nu        line numbers on/off
:noh           clear search highlight
```

## Syntax highlighting

Pygments — supports every language pygments knows. Full-file tokenization so multi-line tokens (docstrings, block comments) are correct. Cached per buffer generation so re-highlighting only happens on actual edits.

## Markdown rendering

`.md` files render inline: heading markers dim, heading text bold+yellow, `**bold**` is bold, `*italic*` is underlined, `` `code` `` is green, `[links](url)` show the text in cyan with the URL dimmed, fenced code blocks are colored through.

The cursor line always shows raw markdown so editing column positions are exact. Move away and it renders. Move back and it opens up. Feels like rich-text editing with a vim brain.

## CSV / TSV rendering

`.csv` and `.tsv` files render as an aligned column grid — box-drawing borders, columns padded to width, the first row treated as a bold header, and each column tinted a different syntax color so fields stay distinct. Quoted cells with embedded delimiters (`"San Francisco, CA"`) parse correctly; over-wide cells truncate with `…`.

It plugs into the same **TABLE mode** as markdown tables — like Excel with vim bindings. The grid stays rendered while you navigate, the status bar shows `TABLE`, and motions move cell-to-cell instead of char-to-char:

```
h / l          previous / next cell (wraps across rows)
j / k          same column, row up / down
Tab / Shift-Tab next / previous cell
```

The cursor snaps to the rendered cell. Press `i` (or any insert key) and the row under the cursor drops back to raw text so buffer columns map 1:1 for exact editing — `Tab` in insert jumps to the next cell too. Move off the row, or leave insert, and it renders back into the grid.

## JSON structure preview

`:jpreview` (aliases `:jp`, `:JsonPreview`) on a `.json` buffer opens a live inspector in a vertical split. Move the cursor over any value and the pane shows **just that subtree**, pretty-printed and pygments-colorized; the status line shows its path, e.g. `json  $.orders[1].id`. Hover a key to preview its value; hover a container to preview the whole thing. Run `:jpreview` again to close it.

```
:jp            toggle the preview pane
               (move over a value → its subtree renders on the right)
```

It parses with a position-aware JSON parser (not line-based), so it works on **minified single-line JSON** too — park the cursor anywhere in a 5KB one-liner and the pane resolves the exact node under it. Long string values wrap inside the pane so row ends aren't clipped. Invalid JSON shows `// invalid JSON` until the buffer parses again. The source stays the focused, editable pane; the preview just follows along.

`:jp` also works on a **`.csv`/`.tsv` buffer** — a common case is a column of giant JSON blobs (API responses) too wide to read in the grid. Open the preview and walk the cells in TABLE mode (`h/j/k/l`); each cell's value is parsed and pretty-printed on the right, with the column name in the status line (`json  raw_exa_response`). Cells that aren't JSON show their raw value instead.

When a value is taller than the pane, jump into the preview with `Ctrl-W l` (or `Ctrl-W w`) and scroll it with normal vim bindings — `j`/`k`, `Ctrl-D`/`Ctrl-U`, `gg`/`G`, `/search`. It stays put while focused; `Ctrl-W h` returns to the source, and moving to a new value/cell refreshes the preview from the top.

Unlike the markdown/csv overlays — which are bound one-screen-line-per-buffer-line — this is a separate pane, which is what lets it reflow a node into a multi-line tree regardless of how the source is formatted.

## Python eval

This is the part that got out of hand.

**`Ctrl-E`** in normal mode evals the current line. In visual/visual-line mode it evals the selection. Output is inserted as `# >> ...` comment lines immediately below, with full undo support.

**`:py`** drops into an interactive `code.interact()` REPL with `ed`, `buf`, and `pane` in scope. `Ctrl-D` returns to the editor.

**`:py <expr>`** evals an expression inline and shows the result in the status bar.

Before running a selection, pyvim does static analysis (via `ast`) to figure out what the selection needs — imports, function definitions, class definitions — and auto-runs those first. You can `Ctrl-E` any block in a Python file without having to manually eval the imports section first.

The eval namespace persists for the whole session. State accumulates. Define a function in one `Ctrl-E`, call it from another.

### What you can do in the REPL

```python
# inspect the live buffer
>>> buf.lines[:5]
>>> buf.filename

# edit with undo
>>> buf.save_undo()
>>> buf.set_line(3, 'x = 99')
>>> buf.save()

# or reach directly into the list
>>> buf.lines[0] = '#!/usr/bin/env python3'
>>> buf._gen += 1   # invalidate highlight cache

# jump cursor
>>> pane.cursor.row = 42

# run a transformation
>>> for i, l in enumerate(buf.lines):
...     buf.lines[i] = l.rstrip()
>>> buf._gen += 1; buf.modified = True

# AST-level refactoring
>>> import ast
>>> tree = ast.parse('\n'.join(buf.lines))
>>> # mutate the tree
>>> buf.lines = ast.unparse(tree).splitlines(); buf._gen += 1
```

The editor resumes on `Ctrl-D` and picks up all mutations on the next draw.

### The eval playground

`playground.py` has a set of self-contained blocks to try — gaussian histogram, fibonacci bar chart, markov chain trained on the file's own source, a `sort_selection(r1, r2)` helper, and a block that edits the file's own header timestamp. Open it, `V`-select any block, `Ctrl-E`.

## Architecture

```
Buffer          list[str] + undo stack + edit generation counter
Pane            Buffer + cursor + scroll + screen geometry (y/x/h/w)
Layout tree     _Leaf / _Split BSP tree, recomputed each draw
Editor          mode state machine + all keybindings + draw loop
```

Syntax highlighting: `_pg_highlight(buf)` tokenizes the full buffer once with pygments, maps character offsets to (row, col), caches by `(id(buf), buf._gen)`.

Markdown rendering: `_md_highlight(buf)` — separate path from pygments, returns `(visual_line, spans)` per row. Cursor row and visual-selected rows shown raw so buffer column positions stay valid.

CSV rendering: `_csv_highlight(buf)` — parses each line with the `csv` module (honors quoted delimiters), computes per-column widths, and returns the same `(visual_line, spans)` shape, cached by `(id(buf), buf._gen)`. The cell-navigation operators (`_cell_next/_prev/_down/_up`) are shared with markdown tables via dispatch helpers (`_cells_at_row`, `_cell_at_in_row`, `_on_cell_row`) that pick pipe-table vs comma/tab geometry, so one set of TABLE-mode bindings drives both. Insert-mode and visual rows show raw; `_place_cursor` remaps the buffer column onto the rendered cell.

JSON preview: `_json_parse(text)` is a hand-rolled tokenizer + recursive-descent parser that annotates every value node with its `(start, end)` char span (stdlib `json` has no source positions), cached per `(id(buf), buf._gen)`. `_json_node_at(root, offset)` finds the smallest node under the cursor (key hover → its value); the `(source_pane, preview_pane)` tuple in `self._json_preview` is refreshed each `draw()` from the source pane's cursor, writing the pretty-printed subtree into a `[json-preview].json` buffer so the normal pygments path colorizes it for free.

Python eval: `_resolve_deps(r1, r2)` does AST analysis to find what a selection's free variables need, then runs exactly those imports/defs in order.

## Running

```bash
# with uv (recommended)
uv run pyvim.py
uv run pyvim.py somefile.py

# or directly if pygments is installed
python3 pyvim.py
```

Requires Python 3.9+ (uses `ast.unparse`). The only dependency is `pygments`, which is optional — syntax highlighting degrades gracefully without it if you remove the import.

## Known gaps

- No mouse support
- No macros (`q` register) yet
- No `%` bracket matching
- Ctrl-W navigation has been through several iterations of geometry bugs and may still have edge cases
- No `%` bracket matching yet

![](mona_lisa.png)
