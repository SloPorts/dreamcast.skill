# Rendering: getting geometry to the PVR

Rules: JM-1, JM-3, JM-6, JM-7, JM-8, JM-19, JM-20 (see SKILL.md). API names checked against KOS master, Sept 2026.

## Contents
1. How a frame flows
2. Choosing a submission path (direct, DMA, hybrid)
3. Pattern: direct rendering with zero vertex copies
4. Pattern: DMA list with zero vertex copies
5. Pattern: hybrid init
6. Poly headers: compile once, edit fields, resubmit on change
7. Lists: opaque, punch-through, translucent
8. What not to copy from KOS examples

## 1. How a frame flows

The CPU feeds the Tile Accelerator (TA) a stream of 32-byte (or 64-byte) parameters: a poly header,
then vertices, then the next header, and so on. The TA bins each polygon into per-tile lists ("object
pointer blocks", OPBs) and writes vertex data into the vertex buffer, both in VRAM. When the scene is
finished, the PVR core renders tile by tile from those bins while the CPU starts building the next frame.
So rendering of frame N overlaps TA input of frame N+1, and anything global that the core reads while
rendering (palette RAM, fog table, the PT alpha reference) is read *live* by the frame in flight.

Each list (OP, OP_MOD, TR, TR_MOD, PT) must reach the TA contiguously, once per frame: open it, send all
of it, close it. KOS enforces this with `pvr_list_begin()` / `pvr_list_finish()`.

## 2. Choosing a submission path

| Path | How vertices reach the TA | When it fits |
|---|---|---|
| Direct rendering (DR) | CPU writes each 32-byte vertex into a store queue (`pvr_dr_target`) and flushes it (`pvr_dr_commit`); it goes straight to the TA | Geometry that is generated or transformed as it is submitted; lists you can emit in one pass |
| DMA | CPU writes vertices into a RAM vertex buffer (`pvr_vertbuf_tail`); KOS DMAs each list to the TA at `pvr_scene_finish()` | Lists you must build out of order, or interleave across the frame; frees the CPU while the TA ingests |
| Hybrid (JM-8) | Both in one frame: lists with a RAM vertex buffer go by DMA, lists without one go direct | Most real games: e.g. opaque world direct, translucent effects and UI built over the frame and DMA'd |

Whichever path, the rule is the same: the vertex is written **once**, into its final destination (JM-6).
`pvr_prim()` / `pvr_list_prim()` break that by construction (JM-1): you build a vertex in RAM and they
copy it (`sq_fast_cpy` for direct, `memcpy` into the DMA buffer otherwise).

## 3. Pattern: direct rendering with zero vertex copies

```c
#include <dc/pvr.h>

/* Send a cached header through the store queue. A header is state, not vertex data: sending 32 bytes
   per state change is unavoidable; sending it when nothing changed is waste (JM-7). */
static inline void dr_send_hdr(pvr_dr_state_t *dr, const pvr_poly_hdr_t *hdr) {
    uint32_t *d = (uint32_t *)pvr_dr_target(*dr);
    const uint32_t *s = (const uint32_t *)hdr;
    d[0] = s[0]; d[1] = s[1]; d[2] = s[2]; d[3] = s[3];
    d[4] = s[4]; d[5] = s[5]; d[6] = s[6]; d[7] = s[7];
    pvr_dr_commit(d);
}

void draw_opaque(const draw_t *draws, int n) {
    pvr_dr_state_t dr;
    const pvr_poly_hdr_t *sent = NULL;

    pvr_list_begin(PVR_LIST_OP_POLY);   /* a list with no DMA vertex buffer: goes direct */
    pvr_dr_init(&dr);

    for (int i = 0; i < n; i++) {
        const draw_t *d = &draws[i];               /* draws pre-sorted by header */
        if (d->hdr != sent) {
            dr_send_hdr(&dr, d->hdr);
            sent = d->hdr;
        }
        for (int k = 0; k < d->count; k++) {
            pvr_vertex_t *v = pvr_dr_target(dr);   /* the SQ: the vertex's final home */
            float x, y, z;
            transform_point(&d->pos[k], &x, &y, &z); /* result goes straight into v below */
            v->flags = (k == d->count - 1) ? PVR_CMD_VERTEX_EOL : PVR_CMD_VERTEX;
            v->x = x; v->y = y; v->z = z;
            v->u = d->uv[k].u; v->v = d->uv[k].v;
            v->argb = d->color[k];
            v->oargb = 0;
            pvr_dr_commit(v);
        }
    }
    pvr_list_finish();
}
```

Notes:
- `pvr_dr_target()` is a macro that alternates between the two SQs; take one target per 32-byte write
  and commit it before taking the next.
- Write SQ memory with 32-bit (or paired 64-bit float) stores. No byte stores, no `memcpy` into it.
- Clipping: the TA does not clip against the near plane. Clip (or reject) in the transform loop and
  emit the clipped vertices directly; don't stage the whole mesh in a temporary array to clip it.
- `pvr_list_begin()` on a direct list blocks (via genwait) until the TA has finished the previous
  frame's input, so open direct lists as late as you can and do CPU work for later lists first.

## 4. Pattern: DMA list with zero vertex copies

```c
/* At init: a 32-byte-aligned RAM buffer, twice one frame's worth (KOS double-buffers it). */
static uint8_t tr_vbuf[2 * TR_BYTES_PER_FRAME] __attribute__((aligned(32)));
pvr_set_vertbuf(PVR_LIST_TR_POLY, tr_vbuf, sizeof tr_vbuf);

/* Per frame, anywhere between pvr_scene_begin() and pvr_scene_finish(): */
static const pvr_poly_hdr_t *tr_sent;   /* reset to NULL at scene begin */

void emit_sprite(const pvr_poly_hdr_t *hdr, const sprite_t *s) {
    pvr_vertex_t *v = pvr_vertbuf_tail(PVR_LIST_TR_POLY);
    pvr_vertex_t *start = v;

    if (hdr != tr_sent) {                   /* JM-7: header only on a state change */
        *(pvr_poly_hdr_t *)v++ = *hdr;
        tr_sent = hdr;
    }
    /* write each corner's fields directly into v[0..3] from s; PVR_CMD_VERTEX_EOL on v[3] */
    ...
    pvr_vertbuf_written(PVR_LIST_TR_POLY, (size_t)((uint8_t *)(v + 4) - (uint8_t *)start));
}
```

KOS flushes the data cache over the buffer before the DMA starts (`dma_map_src`), so writing through
a cached pointer is fine. Size the buffer for the worst frame: `pvr_vertbuf_written()` asserts on
overflow in debug builds and silently corrupts in release.

## 5. Pattern: hybrid init (JM-8)

```c
pvr_init_params_t params = {
    .opb_sizes = { PVR_BINSIZE_16, PVR_BINSIZE_0, PVR_BINSIZE_16, PVR_BINSIZE_0, PVR_BINSIZE_16 },
    .vertex_buf_size = 512 * 1024,     /* the TA's vertex buffer in VRAM, not the RAM DMA buffers */
    .dma_enabled = 1,
    .fsaa_enabled = 0,
    .autosort_disabled = 0,
    .opb_overflow_count = 3,
};
pvr_init(&params);

/* DMA'd lists get a RAM buffer; lists you leave without one are submitted directly. */
pvr_set_vertbuf(PVR_LIST_TR_POLY, tr_vbuf, sizeof tr_vbuf);
/* OP and PT have no vertbuf: draw them with pvr_list_begin + pvr_dr_* as in section 3. */
```

The doc comment on `dma_enabled` in `pvr.h` still says every enabled list needs a vertex buffer; the
code (`pvr_list_begin`, `pvr_scene_finish`, `dma_next_list`) explicitly supports lists without one.
Direct lists go to the TA while you submit them; DMA lists follow at `pvr_scene_finish()`. Working
examples: doom64-dc, Sonic Mania Dreamcast (see sources.md).

Disabled lists (`PVR_BINSIZE_0`) cost nothing; enable only the lists you use. Bin size and
`opb_overflow_count` trade VRAM for headroom: geometry that overflows the bins flickers or drops along
tile boundaries.

## 6. Poly headers (JM-7)

A `pvr_poly_hdr_t` is 32 bytes: `cmd`, `mode1`, `mode2`, `mode3` and four words of extra data. KOS
exposes each control word both raw (`hdr.mode2`) and as named bitfields (`hdr.m0` … `hdr.m3`):

| Word | Bitfield highlights |
|---|---|
| `m0` (cmd) | `list_type`, `txr_en`, `gouraud`, `color_fmt`, `modifier_en`, `clip_mode` |
| `m1` | `depth_cmp`, `culling`, `depth_write_dis`, `txr_en` |
| `m2` | `blend_src`, `blend_dst`, `fog_type`, `alpha`, `txralpha_dis`, `u_clamp`/`v_clamp`, `u_flip`/`v_flip`, `filter_mode`, `mip_bias`, `u_size`/`v_size`, `shading` |
| `m3` | `txr_base` (`to_pvr_txr_ptr(vram_ptr)`), `pixel_mode`, `vq_en`, `mipmap_en`, `nontwiddled`, `x32stride` |

Pattern:

```c
typedef struct material {
    pvr_poly_hdr_t hdr;        /* first: 32-byte aligned by its type, one cache line (JM-4) */
    /* ...other per-material state touched at submit time... */
} material_t;

void material_init(material_t *m, pvr_ptr_t tex, int w, int h, int fmt) {   /* at load */
    pvr_poly_cxt_t cxt;
    pvr_poly_cxt_txr(&cxt, PVR_LIST_OP_POLY, fmt, w, h, tex, PVR_FILTER_NONE);
    pvr_poly_compile(&m->hdr, &cxt);
}

/* Per frame: edit, don't recompile. */
m->hdr.m3.txr_base = to_pvr_txr_ptr(next_frame_tex);   /* swap texture (same size/format) */
m->hdr.m1.culling  = PVR_CULLING_NONE;
```

For paletted textures, the palette bank lives in `mode3` bits 21–26 (the same bits as
`PVR_TXRFMT_4BPP_PAL(n)` / `PVR_TXRFMT_8BPP_PAL(n)`), so a palette swap is a masked write:

```c
#define PAL_SEL_MASK (0x3fu << 21)
m->hdr.mode3 = (m->hdr.mode3 & ~PAL_SEL_MASK) | PVR_TXRFMT_8BPP_PAL(bank);   /* 8bpp: bank 0..3 */
```

Then sort or bucket draws by header pointer and send a header only when it differs from the last one
sent (section 3). Identical consecutive headers cost TA bandwidth and nothing else.

### Sprites: four corners in one 64-byte vertex

For 2D quads (sprites, tiles, UI, particles facing the camera), the PVR's sprite primitive takes all
four corners in one 64-byte `pvr_sprite_txr_t` (x/y/z for corners a–c, x/y for d, packed 16-bit UVs)
instead of four 32-byte vertices. It goes through the SQs as two 32-byte writes (two `pvr_dr_target()`
slots). Build the header with `pvr_sprite_cxt_txr()` + `pvr_sprite_compile()` into a
`pvr_sprite_hdr_t`; the sprite's colour lives in the header (`hdr.argb`), so a tint change is a header
field edit plus a resend, and same-tint sprites should be submitted together. `pvr_sprite_compile()`
also begins with `movca.l` on its destination (checked in `libkallisti.a`): keep that header 32-byte
aligned too.

The sprite primitive is always a full quad. That is right for opaque and PT sprites and for small TR
ones, but not for large translucent effect puffs with wide clear margins (next section).

### Effect puffs: draw the frame's outline, not the quad (JM-20)

A TR polygon textures and blends every pixel it covers, whether the texel there is clear or not. Dust
and smoke puffs are mostly soft round blobs in square frames: on [ps2port] only 52–65% of each puff's
quad carried alpha, and drawing each frame's convex outline instead cuts the puffs' fill by about
27–35% (projected from texel coverage, then confirmed on real hardware). Puffs overlap heavily,
so with autosort the saving repeats for each layer.

At build time, for each animation frame:
1. Mark every texel with alpha > 0. Dropping faint texels changes the image; the outline must contain
   all of them.
2. Grow the mask by one texel if the material is filtered, so bilinear taps at the edge stay inside.
3. Take the convex hull and reduce it to at most 8 vertices by moving edges *outward* (remove the vertex
   whose removal, extending its neighbouring edges to meet, adds the least area). Never cut inward.
4. Store the vertices in frame-local coordinates 0..1, in order around the hull.

```c
typedef struct puff_outline {
    uint8_t n;                     /* 3..8 */
    float   p[8][2];               /* frame-local u,v in 0..1, in order around the hull */
} puff_outline_t;                  /* one per animation frame, built offline */

/* Emit one puff into the TR DMA list as a strip, written in place (JM-6). Strip order for a convex
   polygon: p0, p1, p[n-1], p2, p[n-2], ... */
void emit_puff(const puff_outline_t *o, float cx, float cy, float z, float size,
               float u0, float v0, float du, float dv, uint32_t argb) {
    pvr_vertex_t *v = pvr_vertbuf_tail(PVR_LIST_TR_POLY);
    int lo = 1, hi = o->n - 1;
    for (int k = 0; k < o->n; k++) {
        int i = (k == 0) ? 0 : (k & 1) ? lo++ : hi--;
        float fu = o->p[i][0], fv = o->p[i][1];
        v[k].flags = (k == o->n - 1) ? PVR_CMD_VERTEX_EOL : PVR_CMD_VERTEX;
        v[k].x = cx + (fu - 0.5f) * size;
        v[k].y = cy + (fv - 0.5f) * size;
        v[k].z = z;
        v[k].u = u0 + fu * du;  v[k].v = v0 + fv * dv;
        v[k].argb = argb;  v[k].oargb = 0;
    }
    pvr_vertbuf_written(PVR_LIST_TR_POLY, o->n * sizeof(pvr_vertex_t));
}
```

(Send the shared puff header once before the run of puffs, as in section 4. For rotation, rotate the
`(fu - 0.5, fv - 0.5)` offset; the UVs stay as they are.)

The cost is vertices: an 8-sided outline is 256 bytes of TA input against 64 for a sprite. The TA is
cheap (about 0.15 µs a vertex on [ps2port]), so this pays off whenever the puffs are big on screen.
Small, distant puffs can stay quads. A side benefit: colour is per vertex here, not in the header as
with sprites, so puffs with different tints no longer each need their own header.

## 7. Lists

- **Opaque (OP)**: no blending; the core resolves visibility per pixel, so submission order does not
  matter for correctness. The cheapest list: put everything here that can go here.
- **Layered backgrounds (JM-19)**: stacked PT layers (parallax tile maps, foliage cards) are slow, because
  each layer textures every pixel it covers. Put every fully opaque tile, of every layer, in OP with a
  per-layer depth so hidden-surface removal discards what is covered before any texturing; send only
  tiles that actually have transparent texels to PT; flatten layers that never move relative to each
  other at build time.
- **Punch-through (PT)**: alpha-tested cut-outs (foliage, fences, text). Rendered like opaque after the
  alpha test. Keep the header's default blend, `SRCALPHA`/`INVSRCALPHA` as `pvr_poly_cxt_*()` sets it
  (JM-3). If a PT polygon seems to need a different blend, it is really a translucent polygon.
- **Translucent (TR)**: blended. With autosort on (the default), the core sorts per pixel, which costs
  fill rate in proportion to overlapping layers; keep TR coverage small. With `autosort_disabled`, you
  sort and the core blends in submission order.
- **Modifier volumes (OP_MOD/TR_MOD)**: shadow or "inside volume" second parameter sets
  (`pvr_poly_cxt_txr_mod`). They cost bins and fill; enable only if used.

## 8. What not to copy from KOS examples

KOS's examples teach the API, not the fast path. `examples/dreamcast/pvr/pvrmark_strips_direct` sends
its header with `pvr_prim()` before switching to `pvr_dr_target()`, and many examples call
`pvr_poly_compile()` per frame. Both are JM-1 / JM-7 violations in a real renderer.
