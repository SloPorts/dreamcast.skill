# Textures, VRAM and palettes

Rules: JM-2, JM-9, JM-10, JM-11, JM-12, JM-13 (see SKILL.md). Supporting notes from KOS's `pvrtex`
README (TapamN) are marked [pvrtex].

## Contents
1. Format choice
2. Twiddling (JM-2)
3. VQ compression
4. Filtering (JM-10, JM-11)
5. Palettes and palette animation (JM-12, JM-13)
6. The working set and VRAM budget (JM-9)
7. Unavoidable runtime textures
8. Converting assets

## 1. Format choice

Pick in this order, stopping at the first that looks acceptable:

1. **4bpp or 8bpp paletted, twiddled**, palette in ARGB1555 / RGB565 / ARGB4444. Smallest, filterable
   (JM-11), animatable through the palette (JM-13).
2. **VQ-compressed 16-bit** (ARGB1555 / RGB565 / ARGB4444). About 1/8 the size of the raw texture plus a
   2 KB codebook; the usual choice for large photographic or world textures.
3. **Raw 16-bit twiddled.** When VQ artefacts are unacceptable and the texture is small.

Never: ARGB8888 palettes (JM-12), non-twiddled anything that you load from disc (JM-2).
Notes [pvrtex]: YUV422 holds gradients better than RGB565 but filters slower. Paletted + VQ + mipmaps
+ bilinear trips a hardware bug that misfilters a few texels per 4x2 (8bpp) / 4x4 (4bpp) block.

Sizes: twiddled textures are powers of two from 8 to 1024 per side; mipmapped textures must also be
square.

## 2. Twiddling (JM-2)

The PVR fetches texels in twiddled (Morton / Z-order) layout, so a 2x2 filter footprint and a rotated
span stay within nearby memory. Non-twiddled ("stride") textures exist for surfaces the hardware writes
row by row; they render slower, "especially when filtered or rotated" [pvrtex], cannot be mipmapped, and
cannot be paletted.

- Twiddle **at build time** (`pvrtex`, `texconv`), load with `pvr_txr_load()` (SQ copy) or
  `pvr_txr_load_dma()`.
- `pvr_txr_load_ex(..., PVR_TXRLOAD_16BPP)` twiddles on the CPU while loading. Acceptable once at load
  for a texture you could not pre-convert; never per frame.
- The legitimate exceptions are hardware-produced surfaces: render-to-texture output
  (`pvr_scene_begin_txr`) and the YUV converter's output for FMV. They are non-twiddled by nature.
  They are the only non-twiddled textures a DC program should have.

## 3. VQ compression

A VQ texture is a codebook of up to 256 entries, each a 2x2 block of 16-bit texels (2 KB), plus one byte
per 2x2 block. A 256x256 16-bit texture shrinks from 128 KB to 2 KB + 16 KB. Smaller codebooks (e.g.
`pvrtex -c 64`) cost quality but improve fill rate [pvrtex]. Keep VQ textures twiddled too (JM-2),
even though `pvrtex` can emit compressed stride textures.

## 4. Filtering (JM-10, JM-11)

- `PVR_FILTER_NONE` (nearest) is the fastest. Make it the default.
- `PVR_FILTER_BILINEAR` costs fill rate; enable it per material where magnification or minification
  artefacts are visible: large world surfaces, scaled sprites. UI drawn 1:1, fonts and pixel art stay
  unfiltered.
- Trilinear is rendered as two passes (`PVR_FILTER_TRILINEAR1` / `TRILINEAR2`): twice the fill.
- Paletted textures filter like any other format. Never expand 4bpp/8bpp to 16bpp to get filtering.
- Mipmaps are what make minified textures cheap and stable (fewer texel-cache misses, no shimmer);
  generate them at build time (`pvrtex -m`) for anything seen at a distance.

## 5. Palettes and palette animation

Palette RAM holds **1024 entries, one 32-bit register each, whatever the format**. One format is active
for all of it (`pvr_set_pal_format`). A 4bpp texture selects one of 64 banks of 16 entries
(`PVR_TXRFMT_4BPP_PAL(n)`), an 8bpp texture one of 4 banks of 256 (`PVR_TXRFMT_8BPP_PAL(n)`).

**Format (JM-12).** `PVR_PAL_ARGB1555`, `PVR_PAL_RGB565` or `PVR_PAL_ARGB4444`. Never
`PVR_PAL_ARGB8888`: it cuts fill rate severely, and with filtering KOS's `pvr_pal.h` documents it at
about half speed. ARGB4444 when you need graded alpha, ARGB1555 for cut-out alpha, RGB565 for opaque.

**Animation (JM-13).** Writing all 1024 registers per frame is much cheaper than twiddling and uploading
even a few kilobytes of texture. Use it for colour cycling (water, lava, lights), flashes, team colours,
fades of a single texture, damage tints:

```c
pvr_set_pal_format(PVR_PAL_ARGB1555);          /* once, at init */

/* per frame: cycle 16 entries of an 8bpp texture's bank */
void water_tick(uint32_t t) {
    for (uint32_t i = 0; i < 16; i++)
        pvr_set_pal_entry(WATER_BANK * 256 + WATER_FIRST + i, water_cycle[(i + t) & 15]);
}
```

Swapping a texture's whole palette is a header field edit, not an upload (rendering.md section 6):

```c
m->hdr.mode3 = (m->hdr.mode3 & ~(0x3fu << 21)) | PVR_TXRFMT_4BPP_PAL(bank);   /* bank 0..63 */
```

Palette RAM is a register file the core reads while it renders the previous frame (rendering.md
section 1). If a mid-frame palette write ever shows as a frame with mixed colours, alternate between two
banks: write the bank the frame in flight is not using, then point the header at it.

## 6. The working set and VRAM budget (JM-9)

8 MB of VRAM holds, before any texture:

- Frame buffers: 640x480 at 16 bits is 600 KB each, double-buffered; 24/32-bit output costs 1.5–2x.
- The TA's vertex buffer and tile bins (OPBs), each double-buffered by KOS, plus any OPB overflow sets.
  These scale with `pvr_init_params_t` (`vertex_buf_size`, `opb_sizes`, `opb_overflow_count`).

`pvr_mem_available()` after `pvr_init()` tells you what is left. Keep the textures for one scene inside
that, load them at scene start, and free them as a group at scene end; allocating and freeing
individual textures during play fragments `pvr_mem` and invites stalls.

Keep the per-frame set small too: the texture cache works best when consecutive draws reuse textures.
Sort by header (which means by texture), share textures via atlases where UVs allow, and prefer palettes
over duplicate textures for colour variants.

## 7. Unavoidable runtime textures

FMV frames, streamed content, render-to-texture effects. Then:

- Allocate the VRAM once, at a fixed size, and reuse it; double-buffer it (two textures, swap
  `hdr.m3.txr_base`) so you never write a texture the frame in flight is sampling.
- Upload with the SQs (`pvr_txr_load`) or DMA (`pvr_txr_load_dma`), pre-twiddled if the source can be.
- Prefer the hardware producing the texture (YUV converter, render-to-texture) over the CPU producing
  it.

## 8. Converting assets

`pvrtex` ships with KOS (`$KOS_BASE/utils/pvrtex`) and covers all of the above:

```
pvrtex -i in.png -o out.dt                                   # twiddled, auto format
pvrtex -i in.png -o out.dt -f rgb565 -c -m                   # VQ + mipmaps
pvrtex -i in.png -o out.dt -f pal8bpp -C 64 -d               # 8bpp, 64 colours, dithered; out.dt.pal
pvrtex -i in.png -o out.dt -f argb4444 -d -c 64 -m quality   # small codebook (faster fill)
```

`texconv` (tvspelsfreak) is the older equivalent.
