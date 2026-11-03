---
name: dreamcast
description: Hardware-proven rules and KallistiOS idioms for Sega Dreamcast programming — PowerVR2 (PVR/TA) rendering, poly headers, direct vs DMA vertex submission, twiddled/VQ/paletted textures, SH4 cache and fast-math, KOS scheduler, timers and genwait frame pacing — collected from experienced DC developers. Use this skill whenever you write, review, port, profile or debug code that targets the Dreamcast: anything including <kos.h> or <dc/pvr.h>, calling pvr_*, sq_*, thd_*, genwait_*, maple_* or snd_* APIs, built with sh-elf-gcc or KOS_CFLAGS, or mentioning KallistiOS/KOS, SH4, PVR, tile accelerator, dcload, GDEMU or a "DC port" — even when the question is only about one function, a texture format, a build flag or a frame-rate drop and the skill isn't named.
---

# Dreamcast programming

Rules and idioms for code that runs on the Sega Dreamcast under KallistiOS (KOS). The rules come from
developers who have shipped full-speed DC ports; each carries an ID naming its source (`JM-n` =
jnmartin, numbered as in his original list, see `references/sources.md`). Every KOS API named here was
checked against KOS source (master of Sept 2026, GCC 15.1).

## How much weight the rules carry

The rules below are binding. They encode measured hardware behaviour that is easy to get wrong and
expensive to discover, so when the codebase you are working in does something different, treat the
codebase as the thing to fix — flag it, and fix it if that is the task — never as precedent to imitate.
If a rule seems to block what the user asked for, say so and explain the trade-off rather than quietly
breaking it. `references/field-notes.md` holds lower-weight observations from individual projects; a
rule always beats a field note, and a field note beats a guess.

## The machine

SH4 (SH7091) at 200 MHz, 16 MB main RAM, 8 MB VRAM, 2 MB sound RAM. 16 KB direct-mapped operand cache
and 8 KB instruction cache, 32-byte lines. Two 32-byte store queues (SQs) for burst writes to the
PVR, VRAM or RAM. FPU with `fipr`, `ftrv` (4x4 matrix in the back bank), `fsca` (sin/cos) and `fsrra`
(1/sqrt). The PowerVR2 (CLX2) is a tile-based deferred renderer: the Tile Accelerator (TA) bins
polygons per tile into VRAM while the core renders the previous frame from those bins. Five lists
(opaque, opaque modifier, translucent, translucent modifier, punch-through), each submitted contiguously
once per frame.

## The rules

### Getting geometry to the PVR

- **JM-1 — Don't use `pvr_prim()` or `pvr_list_prim()`.** Both take a primitive you already built in RAM
  and copy it again: `pvr_prim` pushes it through the SQs with `sq_fast_cpy`, or for a DMA list hands it
  to `pvr_list_prim`, which `memcpy`s it into the vertex buffer. KOS's own examples use `pvr_prim` for
  headers; don't inherit that habit.
- **JM-6 — The optimal number of times to copy a vertex is zero.** Write each vertex's final fields
  straight into its destination as the transform produces them: the SQ address from `pvr_dr_target()`
  (direct rendering), or `pvr_vertbuf_tail()` for a DMA list (then `pvr_vertbuf_written()`). No staging
  array of `pvr_vertex_t`, no copy between transform, clip and submit.
- **JM-7 — Cache poly headers; edit fields, don't recompile; resubmit only when required.** Compile each
  material's `pvr_poly_hdr_t` once, at load. To change one property later, write its field
  (`hdr.m1.culling`, `hdr.m2.filter_mode`, `hdr.m3.txr_base`, …) instead of rebuilding a
  `pvr_poly_cxt_t` and calling `pvr_poly_compile()`. Sort draws by header and send one only when it
  differs from the last one sent.
- **JM-8 — Direct rendering and DMA rendering can be used together in the same frame.** With
  `pvr_init_params_t.dma_enabled = 1`, a list whose `vertex_buf_sizes[]` entry is 0 is submitted directly
  through the SQs; the others are DMA'd at `pvr_scene_finish()`. Choose per list. See doom64-dc or Sonic
  Mania Dreamcast for working examples (links in `references/sources.md`).
- **JM-19 — Layered punch-through tile backgrounds are slow.** Every PT layer textures every pixel it
  covers; stacked PT layers multiply the fill. Send fully opaque tiles of every layer to the opaque list
  with per-layer depth (its hidden-surface removal then drops the covered pixels before texturing), keep
  only tiles with holes in PT, or flatten static layers into one at build time.
- **JM-20 — Draw each translucent effect puff (dust, smoke) as its frame's alpha outline, not the full
  quad.** TR textures and blends every pixel a polygon covers, clear texels included; on [ps2port] dust
  only 52–65% of each puff's quad carried alpha, so a convex polygon fitted per animation frame cuts the
  puff's fill by about 27–35%. Fit the outlines at build time; the few extra vertices cost far less than
  the fill they save.
- **JM-3 — Don't change the default blend mode on punch-through (PT) headers.** Keep the
  `SRCALPHA`/`INVSRCALPHA` that `pvr_poly_cxt_*()` sets for `PVR_LIST_PT_POLY`. A polygon that needs a
  different blend belongs in the translucent list.

### Textures

- **JM-2 — Don't use untwiddled textures.** Twiddled (Morton-order) layout is what the PVR samples
  efficiently, and paletted formats and mipmaps need it. Twiddle at build time, never per frame.
- **JM-9 — Keep the texture working set compact; avoid creating or uploading textures at runtime at all
  costs.** VRAM is shared with the frame buffers and the TA's bins and vertex buffers, and every runtime
  upload costs CPU time to twiddle and bus time to move. Convert at build time, load at scene start.
- **JM-10 — Unfiltered textured polys are faster than filtered ones.** Default to `PVR_FILTER_NONE`;
  turn filtering on per material where it visibly matters, not globally.
- **JM-11 — Paletted textures can also be filtered.** Don't expand a 4bpp/8bpp texture to 16bpp just to
  get bilinear filtering.
- **JM-12 — Never use the ARGB8888 palette mode.** It cuts fill rate severely; with filtering, KOS's own
  `pvr_pal.h` documents it at about half speed. Use `PVR_PAL_ARGB1555`, `RGB565` or `ARGB4444`.
- **JM-13 — Palette animation is much cheaper than texture animation.** Rewriting all 1024 palette
  entries per frame (`pvr_set_pal_entry`) is far cheaper than twiddling and uploading kilobytes of
  texture. Animate colours through palettes and switch palette banks in the header.

### CPU, memory and build

- **JM-4 — Keep related state together in cache-line-sized (32-byte), cache-line-aligned structs where
  possible.** The operand cache is small and direct-mapped; state touched together should share lines.
- **JM-5 — Do not make 64 KB arrays.** A 64 KB table is four times the operand cache, so random lookups
  mostly miss; arrays sized at multiples of 16 KB also alias each other's cache lines when walked side
  by side. Compute instead of looking up, or shrink the table.
- **JM-18 — Use builtins, fast math, `-ffp-contract=fast`, `fsca` and `fsrra`.** The speed lost without
  them outweighs the precision lost with them. Build with `-fbuiltin -ffast-math -ffp-contract=fast`
  (KOS adds `-mfsca -mfsrra`). Stock KOS ships `-fno-builtin` with this line commented out, and
  `-fno-builtin` alone keeps `sinf`/`sqrtf` as calls into newlib even under `-ffast-math`.

### Kernel, timing and frame pacing

- **JM-14 — Raise the scheduler rate to about 333 Hz.** KOS defaults to 100 Hz (10 ms granularity,
  `THD_SCHED_HZ`); call `thd_set_hz(333)` at startup. It fixes more than it hurts.
- **JM-15 — Never use `timer_ms_gettime*` or `timer_us_gettime*`;** they cause slowdown when used for
  timing.
- **JM-16 — Corollary: use the nanosecond perf timer, `perf_cntr_timer_ns()`, for fine-grained timing.**
  It counts *active* CPU cycles only (it stops while the CPU sleeps), so it measures the cost of code,
  not wall time across a wait.
- **JM-17 — Never spin on timer calls to wait for something; use `genwait_wait()` / `genwait_wake_*()`.**
  Frame pacing is a vblank handler that wakes a genwait object. See the Super Mario 64, Mario Kart 64
  and Star Fox 64 Dreamcast ports (links in `references/sources.md`).

## Working on a task

**Writing new DC code.** Before writing a renderer, mesh or sprite submission path, texture pipeline,
main loop or timing code, read the matching reference below; each has a KOS code pattern that already
follows the rules. Default to the patterns rather than inventing a submission scheme.

**Reviewing, porting or auditing existing code.** Run the checker first, then read every hit in context:

```
python3 ${CLAUDE_SKILL_DIR}/scripts/dc_lint.py <source dirs...>
```

(`${CLAUDE_SKILL_DIR}` is the directory this SKILL.md is in; Claude Code fills it in, other agents
substitute the path themselves.) The script needs only Python 3; `--json` gives machine-readable
output, and the exit status is 1 when any error is found.

It greps for the mechanical violations (`pvr_prim`, untwiddled formats, ARGB8888 palettes, banned
timers, spin waits, 64 KB arrays, missing `thd_set_hz`, fast-math flags). Its hits are candidates, not
verdicts: a hardware render-to-texture target is non-twiddled by nature, for instance. The rules it
cannot see — vertex copies through staging arrays (JM-6), headers recompiled per frame (JM-7), state
scattered across cache lines (JM-4), runtime texture creation (JM-9), full-quad effect puffs (JM-20) —
need reading.

Report each finding as: rule ID, `file:line`, what the code does, why it costs, the fix.

**Investigating performance.** Measure before and after with `perf_cntr_timer_ns()` around the section
in question (JM-16), accumulate over many frames, and print rarely; console output itself is slow.
Then check the rules in the order the profile points to.

## References

| File | Read it when |
|---|---|
| `references/rendering.md` | Submitting geometry: direct rendering, DMA vertex buffers, hybrid mode, header caching, list order, PT/TR choice, sprites and effect puffs |
| `references/textures.md` | Choosing formats, converting assets, VRAM budget, filtering, palettes and palette animation |
| `references/cpu-and-build.md` | Data layout, cache, store queues, compiler flags, float precision, SH4 maths |
| `references/kos-runtime.md` | Main loop, frame pacing, scheduler rate, timing and profiling |
| `references/field-notes.md` | Lower-weight lessons from individual ports (debugging, tooling, budgets), each with its source |
| `references/sources.md` | Who each rule comes from (verbatim), reference projects, how to add knowledge |

## Adding knowledge

New rules from an experienced developer go into `references/sources.md` verbatim under their name with
a new ID prefix (e.g. `AB-1`), then into the topic section above (one or two lines, with the why) and
the matching reference file. A rule that conflicts with an existing one is not merged silently: record
both in `sources.md` and ask the skill's maintainer which wins. Project-specific observations that aren't
rules go in `references/field-notes.md`.
