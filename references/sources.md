# Sources

## Contributors

### jnmartin (JM-1 … JM-20)

Experienced Dreamcast developer (https://github.com/jnmartin84). His list, verbatim, numbered in his order (rule 16 refers to rule
15 as "fifteen"):

1. "dont use pvr_prim or pvr_list_prim"
2. "dont use untwiddled textures"
3. "dont change the default blend mode on PT poly headers"
4. "keep all related state combined in cache-line sized and aligned structs when possible"
5. "do not make 64kb arrays"
6. "the optimal number of times to copy a vertex is ZERO"
7. "poly headers should be cached/reused for as long as possible; when poly headers need modifying, use
   direct field access instead of recompiling; poly headers should only resubmitted when required"
8. "Direct rendering and DMA rendering can be used together in the same frame. See doom64-dc or Sonic
   Mania Dreamcast for proof/examples"
9. "keep your texture working set as compact as possible and avoid having to dynamically create/upload
   textures at all costs if possible"
10. "unfiltered textured polys are faster than filtered textured polys"
11. "paletted textures can also be filtered"
12. "never use ARGB8888 palette mode, cuts your fill rate severely; filtering and argb8888 you might as
    well throw the whole thing in the trash"
13. "palette animation is much cheaper than texture animation; setting 1024 color registers per frame is
    much faster than twiddling and uploading kilobytes of textures"
14. "kos has a miserably low scheduling rate by default, 10 ms granularity; an optimal rate that fixes
    more things than it hurts is around 333hz"
15. "never use timer_ms_gettime* timer_us_gettime* functions, they cause slowdown when used for timing"
16. "as a corollary to fifteen, use the nanosecond perf timer for fine-grained timing if needed"
17. "never spin on timer calls to wait for something; use genwait_wait/genwait_wake* ; see super mario
    64 / mario kart 64 / star fox 64 dreamcast ports for examples of using it for frame pacing"
18. "the performance loss from not using builtin/fastmath/fp-contract=fast/fsca/fsrra outweighs the
    precision/accuracy lost from the math approximations"
19. "layered punch-through polygon tile backgrounds are SLOW"
20. "Drawing each puff as its frame's outline instead of the full quad would cut the fill by about 27–35%.
    Texel coverage shows only 52–65% of each puff's quad carries alpha, so a per-frame convex polygon
    could cut the wasted fill" (written by jnmartin's Claude agent about dust effects in the [ps2port]
    game; confirmed on real hardware)

**Precedence:** these rules rank above any project's own code and documents (SKILL.md, "How much
weight the rules carry"). Where a project disagrees, the rule wins.

**Rationale provenance.** The rules are jnmartin's. The explanations in SKILL.md and the references
were added while building the skill:

| Rule | Explanation is… |
|---|---|
| JM-1 | Checked in KOS source: `pvr_prim` copies via `sq_fast_cpy` or `pvr_list_prim`'s `memcpy` |
| JM-2 | Supported by KOS's `pvrtex` README (stride textures slower, esp. filtered/rotated; paletted must be twiddled) |
| JM-3 | No rationale recorded; the rule stands on its own |
| JM-5 | Inferred from the SH4's 16 KB direct-mapped operand cache; not stated by the source |
| JM-8 | Checked in KOS source: lists without a RAM vertex buffer are submitted directly in DMA mode |
| JM-12 | KOS's `pvr_pal.h` documents the same ("cut about in half" with filtering); [ps2port] measured 21.2 vs 15.1 ms |
| JM-14 | Checked in KOS source: default `THD_SCHED_HZ` 100; genwait wakeups wait for the next tick |
| JM-15 | Mechanism (TMU register reads + 64-bit maths per call) read from KOS `timer.c`; the slowdown is jnmartin's observation |
| JM-16 | Checked in KOS `perfctr.h`: `perf_cntr_timer_ns()` counts active CPU cycles only |
| JM-19 | Matches [ps2port] measurements: layered PT foliage was half the PVR time; PT to OP took one scene from 34.6 to 19.6 ms |
| JM-20 | Coverage figures from [ps2port] dust-puff frames (52–65% of each quad carries alpha); the 27–35% fill saving was projected from them and has since been confirmed on real hardware |
| JM-18 | Verified with sh-elf-gcc 15.1: without `-fbuiltin` no `fsca`/`fsrra`/`fsqrt` is emitted even under `-ffast-math` |

## Other sources

- **[pvrtex]** — `$KOS_BASE/utils/pvrtex/README` (TapamN, 2023): texture formats, twiddling, VQ codebook
  size and fill rate, hardware filtering bug with paletted VQ mipmaps.
- **KOS source** — KallistiOS master (Sept 2026): every API name and default quoted in this skill.
- **[ps2port]** — a large PS2-era 3D game ported to KOS (2026): see field-notes.md.

## Reference projects

Named by jnmartin as examples of the rules in production. Read their source when you need a worked
example of hybrid rendering (JM-8) or genwait frame pacing (JM-17).

| Project | Shows | Link |
|---|---|---|
| doom64-dc | Direct + DMA rendering in one frame; header caching; palettes | https://github.com/jnmartin84/doom64-dc |
| Sonic Mania Dreamcast | Direct + DMA rendering in one frame | https://github.com/michael-fadely/Sonic-Mania-Decompilation |
| Super Mario 64 Dreamcast port | genwait frame pacing | https://github.com/jnmartin84/sm64-dc |
| Mario Kart 64 Dreamcast port | genwait frame pacing | https://github.com/jnmartin84/mk64-dc |
| Star Fox 64 Dreamcast port | genwait frame pacing | https://github.com/jnmartin84/sf64-dc |

## Adding knowledge

1. Paste the contributor's points verbatim under a new heading here, with an ID prefix (initials +
   number, e.g. `AB-1`), numbered in their order.
2. Add each to the matching topic in SKILL.md as one or two lines: the rule, then why. Put patterns,
   numbers and caveats in the topic reference file.
3. Fill in the provenance table: checked in source, measured, or inferred.
4. If a new point conflicts with an existing rule, don't merge either silently: record both here and ask
   the skill's maintainer which wins.
5. Project observations that aren't rules go in field-notes.md with their source tag.
6. If the new rule is mechanically detectable, add a check to `scripts/dc_lint.py`.
