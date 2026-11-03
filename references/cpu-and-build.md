# CPU, memory and build

Rules: JM-4, JM-5, JM-18 (see SKILL.md). Hardware measurements marked [ps2port] come from a large
PS2-era 3D game ported to KOS (field-notes.md has the detail).

## Contents
1. The cache, and why layout decides speed
2. Cache-line structs (JM-4)
3. No 64 KB arrays (JM-5)
4. Store queues, movca.l, prefetch
5. Compiler flags (JM-18)
6. Floats and the FPU
7. Alignment traps
8. Library traps

## 1. The cache, and why layout decides speed

The SH4's operand cache is 16 KB, direct-mapped, 32-byte lines; the instruction cache is 8 KB. Direct
mapping means two addresses 16 KB apart (or any multiple) share one line and evict each other. Main RAM
is slow next to the core, so a typical game loop is memory-bound, not ALU-bound: [ps2port] measured IPC of
0.42–0.62 (of 2) with operand-cache stalls at 23–32 % of all cycles.

A consequence: unrelated changes that move code or `.bss` shift timings by a millisecond or more.
Benchmark against a control build from the same day, and if timings matter, pin hot code and data
early in their sections (`ld --section-ordering-file`, binutils 2.44+) [ps2port].

## 2. Cache-line structs (JM-4)

Keep state that is used together in one 32-byte, 32-byte-aligned struct (or a small number of them),
hot fields first, cold fields split out to another struct:

```c
typedef struct __attribute__((aligned(32))) particle {
    float x, y, z;        /* updated every frame */
    float vx, vy, vz;
    uint16_t life, tex;   /* read every frame */
} particle_t;             /* 28 bytes of data, one line */
_Static_assert(sizeof(particle_t) == 32, "one cache line");

typedef struct particle_cold {
    const char *name;     /* debug/editor only: not in the hot line */
    uint32_t spawn_frame;
} particle_cold_t;
```

- Arrays of such structs walk the cache line by line; parallel arrays of separate fields touch one line
  per field per element.
- A cold flag that is checked in a hot loop costs a whole line miss: fold switches a hot loop reads into
  one mode word inside the hot struct [ps2port].
- `aligned(32)` on the type does not align memory you get from `malloc` or your own arena: use
  `memalign(32, n)` / `aligned_alloc(32, n)` or an arena that hands out 32-byte blocks (section 4 says
  why this can be a correctness issue, not just speed).

## 3. No 64 KB arrays (JM-5)

A 64 KB array is four times the operand cache. As a lookup table it misses on most random accesses,
and a miss costs far more than the arithmetic it was meant to save: compute instead, or shrink the
table to a few KB (a 256-entry table plus arithmetic often replaces a 65536-entry one). Several arrays
sized at multiples of 16 KB, walked in step, also map onto the same cache lines and thrash each other.
And main RAM is 16 MB in total: large static tables come straight out of the game's budget.

## 4. Store queues, movca.l, prefetch

- **Store queues** (two of 32 bytes) burst-write to the TA, VRAM or RAM without polluting the cache.
  KOS: `sq_cpy`, `sq_set32`, `sq_lock`/`sq_unlock`; `pvr_dr_*` and `pvr_txr_load` use them. KOS holds the
  SQ lock while a direct-rendered list is open, so any other SQ user (a sound-stream fill, a second
  thread) must take turns; background threads should reach sound RAM via G2 PIO instead [ps2port].
  Measured floor [ps2port]: about 340 ns per 32-byte burst (~94 MB/s), whether into the TA or RAM.
- **`movca.l`** allocates a cache line for writing without reading it from RAM first. Essential when a
  loop writes whole 32-byte records it never reads [ps2port]. The flip side: it assumes the whole line
  belongs to you. **`pvr_poly_compile()` itself begins with `movca.l` on its destination** (checked in
  KOS's `libkallisti.a`), so compiling a header into memory that is only 16-byte aligned clobbers the
  other half of the line: heap corruption that moves with layout [ps2port].
- **Prefetch** (`__builtin_prefetch(p)` emits `pref @Rn`): the SH4 handles one prefetch at a time, so a
  burst of prefetches gains nothing; prefetching a few records ahead in a streaming loop works [ps2port].

## 5. Compiler flags (JM-18)

The rule: the speed lost without builtins, fast math, `fp-contract=fast`, `fsca` and `fsrra` outweighs
the precision lost with them. What actually reaches the instructions, verified with sh-elf-gcc 15.1 at
`-O2 -m4-single -ml -mfsca -mfsrra` (the KOS defaults):

| Extra flags | `sinf`/`cosf` | `1.0f/sqrtf(x)` | `sqrtf` | `a*b+c` |
|---|---|---|---|---|
| none (stock KOS has `-fno-builtin`) | call into newlib | call | call | `fmac` |
| `-ffast-math -ffp-contract=fast` with `-fno-builtin` still on | call | call | call | `fmac` |
| `-fbuiltin -ffast-math -ffp-contract=fast` | `fsca` (one for a sin+cos pair) | `fsrra` | `fsqrt` | `fmac` |

So build with:

```make
# after KOS_CFLAGS, so these win
CFLAGS += -fbuiltin -ffast-math -ffp-contract=fast
```

or uncomment the matching line in `$KOS_BASE/environ.sh` (it is there, commented, under "Fast Math
Instructions"). `-mfsca`/`-mfsrra` are already in KOS's flags but only take effect under fast math
(`-funsafe-math-optimizations`; `fsrra` also needs `-ffinite-math-only`).

Things to know while following the rule:
- `-ffast-math` implies finite maths: `isnan`/`isinf` checks may be folded away. Test float specials
  by bit pattern if you really need them.
- `fsca` works on an angle quantised to 1/65536 of a turn, accurate to about 2^-21 [ps2port]. Where code
  needs more (a deterministic replay against another platform), add a first-order correction rather
  than dropping fast math.
- **GCC 15.1 ICE with `fsca`** (checked 2026-10-20): in a few functions with `sinf`/`cosf` pairs, the old
  reload pass dies moving `fsca`'s register pair (`internal compiler error: in gen_reg_rtx`, backtrace
  `reload -> gen_move_insn -> operand_subword_force -> copy_to_reg`). `-mlra` (the newer allocator)
  compiles them, but KOS's `environ.sh` calls LRA unstable on SH, so don't switch globally. Build only the
  affected files with `-mno-fsca` (and, under LTO, `-fno-lto` too: SH has no per-function target options,
  so an `-mno-fsca` on the LTO link would remove `fsca` from the whole program). Under LTO the ICE names
  whichever function the trig was inlined into; that function's file usually holds the trig itself.
  Keep fast math everywhere else. In one large game this cost two files; `fsca` went from 16 to 126 sites.
- Fast math and code that tests for NaN: give the few files with deliberate `isnan`/`isfinite` guards
  `-fno-finite-math-only` rather than dropping fast math (they lose `fsrra`, nothing else).
- `-Os` on hot game code cost [ps2port] 20 %; `-O3` helps tight render loops but can slow branchy logic
  through the 8 KB I-cache, so set it per file. `-mrelax` miscompiled (a `bra` in a `jsr` delay slot).

## 6. Floats and the FPU

- KOS 2.2.0+ defaults to `-m4-single`: `double` is real double precision, and every double operation
  switches the FPU's precision mode. Write float literals with `f` (`0.5f`), use `sinf` not `sin`, and
  build with `-Wdouble-promotion` to catch silent promotions. (`-m4-single-only` makes `double` a
  float instead; all libraries must be rebuilt with the same choice.)
- `ftrv` transforms a vector by the 4x4 matrix held in the back register bank (XMTRX) in one
  instruction; `fipr` is a 4-element dot product. Load the matrix once per object and stream vertices
  through it. SH4ZAM (kos-ports `sh4zam`) wraps these: `shz_xmtrx_load_4x4`, `shz_xmtrx_apply_4x4`,
  `shz_xmtrx_transform_vec4`, plus `shz_sincosf`, `shz_inv_sqrtf`, `shz_dot8f`. Loading XMTRX from
  only 4-byte-aligned data needs the `frchg` form; `fmov.d` pairs need 8-byte alignment [ps2port].
- Float to int64 goes through libgcc (`__fixsfdi`, ~71 instructions); stay in int32 range so it is a
  single `ftrc`. Avoid 64-bit division (`__udivdi3`); multiply by a reciprocal [ps2port].

## 7. Alignment traps

The SH4 faults on unaligned 16/32-bit loads and stores that x86 tolerates (e.g. a byte array read as
`uint32_t`). Host builds with UBSan's alignment check find them before the console does [ps2port].
Alignment comes only from the type or an attribute: a `uint32_t buf[8]` is 4-aligned wherever it
lives, so declare 32-byte data as `pvr_poly_hdr_t` / `pvr_vertex_t` or with `aligned(32)`. sh-elf-gcc
15.1 honours that on the stack (it realigns the local at run time; checked), but a linker script's
`SUBALIGN` or LTO's data placement can drop the alignment of statics, and heap/arena memory has only
what the allocator gives [ps2port]. Anything that must be 8- or 32-byte aligned (headers, DMA buffers,
`pvr_prim` data) should be allocated aligned or checked at run time.

## 8. Library traps

- `memcpy` of a few bytes is a library call; use struct assignment or explicit loads [ps2port].
- `getenv`, `snprintf`, `strcmp` per item in a hot loop show up in profiles; resolve once.
- A store through any pointer forces the compiler to reload globals of a compatible type afterwards
  (e.g. writing `float`s to an SQ vertex invalidates every cached `float` global). Copy hot globals into
  locals before the loop [ps2port].
