# Field notes

Lower-weight lessons from individual projects. A rule in SKILL.md always wins over a note here. Each
note carries its source and how it is known:

- **[ps2port]** — a large PS2-era 3D game (streamed 3D scenes, skinned characters, menus, FMV) ported
  to the Dreamcast on KOS in 2026, measured on real hardware (GDEMU / dcload-ip) unless marked
  otherwise.
- **[read]** — read from KOS source, not tested on hardware. **[hyp]** — relayed or untested.

Notes already folded into the topic references (movca.l header alignment, SQ throughput, flags, stacks,
heap, frame-in-flight hazards) are not repeated here.

## Contents
1. PVR and TA behaviour
2. Depth, blending, filtering
3. VRAM and textures
4. Disc and file I/O
5. Sound (AICA)
6. Input, VMU, saves
7. Tooling, dcload, discs
8. Porting from PS2-era code
9. Local measurements that seemed to justify breaking a rule

## 1. PVR and TA behaviour

- **Opaque is where overdraw is cheap.** The OP list gets per-pixel hidden-surface removal *before*
  texturing; PT and TR texture every pixel they cover. Moving opaque-textured batches into the OP list took
  PVR time from 34–41 to 26–29 ms; one foliage scene went from 34.6 to 19.6 ms by moving layered PT to
  OP where the texture allowed. Submitting PT nearest-first saved nothing. [ps2port]
- **KOS never sets the punch-through alpha reference** (Holly register `0x11c`, PT_ALPHA_REF). It stays
  at its reset value, which dropped texels with partial alpha. Set it yourself. [ps2port, KOS source checked]
- **ISP_FEED_CFG's punch-through chunk size** (reg `0x098`, bits 4–13): KOS writes 64. At 1023, PVR time
  fell 1.3–1.5 ms with identical pixels; at 16 it rose 2.6 ms. [ps2port]
- **The TA is cheap** (~0.15 µs a vertex). Once submission is zero-copy (JM-6), the floor is the SQ
  burst rate; only sending fewer vertices helps. [ps2port]
- **Vertex-buffer overflow locks the PVR**: the screen freezes while the game runs on. Read
  `PVR_TA_VERTBUF_POS` before sending (once per ~64 KB of room, not per strip) and drop whole strips or
  primitives; a strip that loses its EOL vertex hangs the PVR. After a `PARAM_OUTOFMEM` the TA stays
  wedged across frames: write `PVR_RESET_TA` after `pvr_wait_ready()` and call `pvr_sync_reg_buffer()`.
  Keep a reserve so the UI outlives distant geometry. [ps2port]
- **Tile-bin (OPB) overflow shows as holes on the 32-pixel tile grid**, not per-pixel hatching. KOS only
  hooks the TA error interrupts in `PVR_RENDER_DBG` builds, and the IRQ dispatch acknowledges unhooked
  ones silently: hook `ASIC_EVT_PVR_*` yourself to see overflows. KOS's buffer switch latches a harmless
  `OPB_OUTOFMEM` every other frame. [ps2port]
- **Overflow OPB sets are expensive and often oversized.** 6 sets at 32-word bins took 675 KB per TA
  buffer set; peak use measured 31–108 KB; 3 sets (337 KB) were enough. Measure before sizing. [ps2port]
- **`vbuf_doublebuf_disabled` saves no VRAM on stock KOS**: `pvr_allocate_buffers` lays out both TA
  buffer sets regardless; reclaiming ~1.5 MB needs an allocator patch. [ps2port, source checked]
- **Emulators build no tile bins** (Flycast, MAME): they cannot reproduce bin overflow or vertex-buffer
  lock-ups. Test those on hardware. [ps2port]
- **x/y clipping is unnecessary** (tile binning handles off-screen geometry); only the near plane needs
  care. Clipping at w = 1 produced full-screen "veils" from near walls; rejecting triangles with a corner
  nearer than a small w threshold was both correct-looking and cheaper there. [ps2port]
- **Front-buffer read-back is very slow**: 120–155 ms (16-bit) to 170–226 ms (24-bit) for a full screen.
  Use render-to-texture (power-of-two target, 1024x512 = 1 MB at 16-bit) instead. [ps2port]

## 2. Depth, blending, filtering

- **`pvr_set_zclip()` defaults to 0.0001**, in front of far geometry when z = 1/w and w passes 10,000. Set
  it to about 1e-7 for large worlds. [ps2port, default checked in `pvr_buffers.c`]
- **Near-coplanar layers flicker with float depth.** Use `PVR_DEPTHCMP_GEQUAL` (KOS's default is
  `GREATER`) plus a tiny per-batch depth bias (1e-5 to 1e-4), capped (~16 steps) so inner geometry doesn't
  poke through. Layers split across OP/PT/TR can't be ordered by bias, because the lists render in a
  fixed order. [ps2port]
- **TR with depth write writes depth before texturing**, so fully transparent texels still write Z.
  With autosort off, TR draws in submission order: sky and backgrounds first. [ps2port]
- **No subtractive blend.** `INVDESTCOLOR` approximations are possible. [ps2port]
- **Back-face culling only saves fill.** Building orientation-preserving strips cost 8–19 % more vertices,
  a bad trade when CPU-bound. [ps2port]
- **Bilinear filtering wraps at the texture edge**: a captured texture pulled in its unwritten last
  column/row (VRAM garbage). Write or pad edges; repeat edge texels at atlas boundaries; inset layered
  sprites by half a texel. [ps2port]
- **Texture interpolation is perspective-correct**: an impostor/backdrop triangle whose corners differ
  widely in depth smears its image; keep neighbouring depths within a small ratio. [ps2port]
- **16-bit UVs (`PVR_UVFMT_16BIT`)** gave nothing on wide views and were 15–20 % slower on close ones
  (one measurement, unexplained). [ps2port]
- **`MODULATE` clamps at 1.0.** Code that relied on 2x modulation (PS2) needs textures pre-brightened by a
  per-texture factor and vertex colours divided by it; clip by preserving hue, not per channel. [ps2port]

## 3. VRAM and textures

- **The frame buffer's dither**: KOS's `vid_set_mode` enables the PVR dither for RGB555/RGB565 (checked in
  `video.c`), a 2x2 pattern that hatches dark and blended surfaces and crawls with camera motion. Off, it
  truncates (half a step darker, green cast). A 24-bit packed frame buffer (`PM_RGB888P`) was smooth with
  no frame-time change, for 600 KB more VRAM. [ps2port]
- **Budget at 640x480** (one large game): frame buffers 1.2 MB at 16-bit, +600 KB at 24-bit; TA vertex
  buffers 2 x 1,664 KB (peak use 1.1–1.66 MB; 1,280 KB starved scenes); about 2.1 MB left for textures.
  [ps2port]
- **Fragmentation, not total, fails first**: 600 KB free held no 256 KB hole. Reserve large blocks
  (render targets, FMV) at startup before any texture. KOS's "out of PVR memory; texture heap probably
  corrupted" was usually just fragmentation. Texture placement alone moved PVR time by up to ~2.4 ms. [ps2port]
- **Palette RAM is small and global** (1024 entries): per-model 256-colour palettes don't fit a scene with
  many models; VQ suited those better. Rewrite a bank only when no in-flight frame uses it. [ps2port]
- **ARGB8888 palettes measured**: 21.2 vs 15.1 ms PVR time on one screen with filtering, the JM-12 cost
  confirmed. [ps2port]
- **Unfiltered measured**: turning bilinear off saved 4 ms in a foliage-heavy scene (JM-10). [ps2port]
- **ARGB8888 to ARGB4444 palettes, by eye**: in-game menus, fonts (four alpha levels 0/85/170/255 map exactly to
  0/5/10/15) and gauges showed no visible difference in side-by-side shots at the same steps (JM-12). [ps2port]
- **Spin floor to genwait floor**: replacing a `thd_pass()` vblank spin with a genwait sleep plus
  `thd_set_hz(333)` cost nothing measurable and coincided with an intermittent hard hang going away (2 of 3 long
  test runs hung before, 0 of 3 after; small sample) (JM-14, JM-17). [ps2port]
- **VQ mip chains**: box-filter each level and map each 2x2 block to the nearest existing codebook
  entry; threshold ARGB1555 alpha per level so coverage doesn't thin. 0–50 KB per texture; saved
  0.4–1.1 ms where drawn. [ps2port]
- **Conversion choices that worked**: VQ for everything above 32x32, plain twiddled 16-bit below; format by
  alpha content (565 none, 1555 cut-out, 4444 if more than ~2 % partial alpha); crop to the used UV range
  plus a texel, rounded up to a power of two. Large smooth gradients band in VQ: keep those plain. For
  PT textures, prefer 1555/565 (PT draws passing texels at full strength, so 4444's partial alpha is
  wasted), weight alpha fully in the VQ fit, and bleed opaque neighbours' colours into transparent texels
  so bilinear doesn't pull the clear colour into edges. [ps2port]
- **Non-twiddled measured**: full-screen layers drawn from a non-twiddled capture texture rendered 1.5-2x
  slower than from the same texture twiddled (JM-2). [ps2port]
- **If you must twiddle on the console**, use two lookup tables (an x part and a y part, OR-ed), not a bit
  loop per texel. Twiddling at build time is still better (JM-2, JM-9). [ps2port]

## 4. Disc and file I/O

- **KOS iso9660 reports `st_blksize` 512**, so newlib's buffered reads issue one GD command per sector.
  KOS takes the multi-sector DMA path only for whole sectors into a 32-byte-aligned destination. Use
  `setvbuf(f, NULL, _IONBF, 0)` on `/cd` files and read whole sectors into 32-aligned buffers: GDEMU went
  from 3.1 to 5.0 MB/s (5.8 aligned). [ps2port]
- **Read in pieces (~128 KB)** so audio and other threads get serviced between commands. [ps2port]
- **GDEMU hides seek cost**: don't design streaming layouts around it if real GD/CD-R matters. [ps2port]
- [read/hyp] fs_iso9660 mounts the last data track of the low-density TOC (GDI track 1, not the game's
  track 3) and polls the disc every vblank; a remount breaks open files. [ps2port]

## 5. Sound (AICA)

- **KOS `snd_stream` pitfalls**: the callback works in bytes whatever the parameter names say; the
  start-sync uses a 32-bit channel mask, so a stream on channel 32+ is never keyed on (allocate stream
  channels first); a callback returning less than asked leaves stale data playing; a refused stereo start
  left state that DMA'd into sound RAM address 0 (the ARM firmware). [ps2port]
- **A ring-service thread** that refills a long-stream ADPCM ring by G2 PIO under `g2_lock` every 40 ms,
  sending no AICA commands, gave zero underruns through 3 s main-thread stalls. [ps2port]
- **AICA facts**: loop registers are 16-bit (max 65,534 samples a region); Yamaha ADPCM silence is 0x80
  bytes, not zeros (zeros drift to a DC offset); KOS keeps the bottom 192 KB of sound RAM;
  `spu_memload`/`spu_memread` work in 32-bit words; the firmware command queue releases by timestamp at
  ~10 ms granularity. Decimating a sample by k means lowering its tune by 12·log2(k). [ps2port]
- ALdc (OpenAL) lacks mid-sample loop points and its buffer-queue streaming left gaps. [ps2port]

## 6. Input, VMU, saves

- **Maple status updates once per vblank**: logic that steps several times per frame sees the same
  sample. Stick range is -128..127 (dead zone ~16 worked); analog triggers want hysteresis (on 0x60, off
  0x40). A+B+X+Y+Start is the standard reset; disable it during VMU writes. [ps2port]
- **VMU access is slow and blocking**: ~17 ms per block read, ~80 ms per block written. Do it off the main
  thread or behind a "saving" screen. [ps2port]
- **`vmufs_write` overwrite is not atomic** (frees, then writes, then updates the directory): write a
  temporary file and rename. KOS has no VMU format function. [ps2port]
- Raw deflate with a 512-byte window shrank a 26 KB save to ~3.1 KB. [ps2port]

## 7. Tooling, dcload, discs

- **dcload-ip `/pc` reads busy-wait with interrupts off**: an open takes ~0.1 s, small reads 35–200 ms, a
  multi-MB single read froze the console, and continuous background reads plus a long main-thread read
  hung it until a physical reset. No thread (audio included) runs during those reads. Load the program
  over dcload but stream assets from a disc image (e.g. on GDEMU). [ps2port]
- `kos-tool` truncates the argument string at 256 bytes (counting argv[0] and the NULs); `kos-tool -r`
  only works while dcload answers. [ps2port]
- **Console output is expensive**: each line over dcload is a round trip, and with no loader output goes
  to serial at 115200 and stalls the game. Throttle and turn it off in release. [ps2port]
- **A watchdog thread** that prints the main thread's PC, PR and the code addresses on its stack every
  few seconds, then exits back to dcload after a timeout, made hangs debuggable without frame pointers
  and without power-cycling. [ps2port]
- `mkdcdisc -N` skips data-track padding (only needed for optical drives). [ps2port]

## 8. Porting from PS2-era code

- **SH4 faults on unaligned accesses x86 tolerates**; build the host side with UBSan's alignment check.
  Undefined pointer arithmetic that "worked" elsewhere (counting a pointer down to NULL) became an
  infinite loop under SH4 GCC. [ps2port]
- **Single precision everywhere**: an x87 host build (80-bit intermediates) diverged from the DC; a host
  build with `-msse2 -mfpmath=sse` matched it frame for frame. Game logic tolerated fast-math
  differences. [ps2port]
- **Precomputed visibility** (per-cell potentially-visible sets) cut a world render pass from 17.6 to
  14.9 ms and one combat scene from 11.3 to 1.7 ms; ~128-triangle clusters were the best culling granularity.
  [ps2port]
- **Software MPEG is too slow beside game logic** (pl_mpeg ~31 ms a 448x256 picture). Pre-encoded YUV422 VQ
  frames from `pvrtex`, LZ4-compressed, cost ~4.9 ms a picture plus 0.1–0.2 ms upload; LZ4 is 3–4x cheaper
  to decode than zstd on the SH4. A movie worker thread needs ~64 KB of stack. [ps2port]

## 9. Local measurements that seemed to justify breaking a rule

One project measured each of these, found a small or no gain from the rule, and kept the
other practice. The rules take precedence. Measure when you like, but a small
local measurement is not a reason to break a rule:

- Projecting each vertex once into a 32-byte record and then copying records per strip corner (JM-6).
- Keeping ARGB8888 palettes because the frame was CPU-bound anyway (JM-12).
- Timing phases with `timer_us_gettime64` (JM-15) and repurposing `PRFC0` for cache events (JM-16).
- A vblank counter polled with `thd_pass()` for the frame-rate floor (JM-17), and `thd_sleep` polls
  instead of genwait to dodge a semaphore race.
- Leaving out `-ffast-math` because it gained ~0.3 ms and clashed with LTO (JM-18).
- Never raising the scheduler rate while relying on `thd_sleep(2)` polls (JM-14).
