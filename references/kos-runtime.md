# KOS runtime: scheduler, timing, frame pacing, threads, memory

Rules: JM-14, JM-15, JM-16, JM-17 (see SKILL.md). API behaviour checked against KOS master source,
Sept 2026. Hardware notes marked [ps2port] come from a large PS2-era 3D game ported to KOS
(field-notes.md).

## Contents
1. Scheduler rate (JM-14)
2. Timing and profiling (JM-15, JM-16)
3. Waiting and frame pacing (JM-17)
4. The frame in flight: what the CPU may touch
5. Threads and stacks
6. Main RAM

## 1. Scheduler rate (JM-14)

KOS reschedules on a timer tick, `THD_SCHED_HZ` = 100 by default: 10 ms. That tick is also when a
thread that has become runnable actually gets the CPU:

- `genwait_wake_*()` (from a vblank or PVR interrupt, say) only puts the sleeper back on the run queue
  (`thd_add_to_runnable`); it does not preempt. The idle thread just executes `sleep`. So the woken
  thread waits for the next tick: up to 10 ms of a 16.7 ms frame at the default rate.
- `thd_sleep(ms)` and genwait timeouts resolve at the same granularity.

Fix, once, early in `main()`:

```c
#include <kos/thread.h>
thd_set_hz(333);   /* ~3 ms ticks; KOS stores 1000/hz in whole ms, so 333 -> 3 ms */
```

About 333 Hz fixes more than it hurts; much higher spends real CPU on scheduler interrupts (jnmartin's
own N64 ports call `thd_set_hz(300)`). JM-14 and JM-17 belong together: genwait pacing is only as
precise as the tick.

## 2. Timing and profiling (JM-15, JM-16)

Don't time with `timer_ms_gettime*()` or `timer_us_gettime*()` (JM-15): they cause slowdown when used for
timing. Each call reads the TMU2 counter and underflow flag twice from the peripheral bus, retrying on a
race, then does a 64-bit multiply; scattered through a frame it adds up and perturbs what it measures.

For fine-grained timing use the CPU performance counter (JM-16):

```c
#include <dc/perfctr.h>

uint64_t t0 = perf_cntr_timer_ns();
do_the_thing();
acc_ns += perf_cntr_timer_ns() - t0;     /* accumulate; print every N frames, not every frame */
```

- Converting cycles to microseconds without a 64-bit divide (`__udivdi3`): with `c` the 48-bit count,
  `hi = c >> 32`, `lo = (uint32_t)c`, and 2^32 = 200 x 21474836 + 96, `floor(c / 200)` modulo 2^32 is
  `hi * 21474836 + (lo + hi * 96) / 200`, adding 21474836 and 96 once if `lo + hi * 96` carried. This
  wraps like a 32-bit microsecond counter, so `now - then` differences still work.
- KOS enables it at boot on `PRFC0`. It counts **active CPU cycles**: it stops while the CPU sleeps
  (idle thread, waiting on genwait). That is what you want for "how long does this code take", and wrong
  for "how long did the frame take, including waits" (count vblanks for that).
- If something calls `perf_cntr_timer_disable()` or reprograms `PRFC0` (e.g. to count cache stalls),
  `perf_cntr_timer_ns()` silently falls back to the TMU at 80 ns resolution. If you use the counters for
  cache-miss events, put them on a separate profiling build.
- Printing is slow: every console line over dcload is a round trip to the host, and serial output at
  115200 baud stalls the game. Accumulate and print rarely [ps2port].
- Statistical profiling that worked [ps2port]: a 500 Hz TMU1 interrupt sampling PC and PR, written to
  a file through KOS's fs (not newlib `FILE`), symbolised with `sh-elf-addr2line`.

## 3. Waiting and frame pacing (JM-17)

Never spin on a timer or a flag to wait for something: no `while (timer() < t);`, no
`while (!flag) thd_pass();`, no `timer_spin_sleep()`. Spinning burns the CPU that other threads (audio,
I/O) need, and the timer reads themselves are the JM-15 cost. Block on a genwait object and wake it from
the event's interrupt. KOS's own `pvr_wait_ready()` works this way.

Vblank frame pacing (see the Super Mario 64, Mario Kart 64 and Star Fox 64 Dreamcast ports for
production versions):

```c
#include <kos/genwait.h>
#include <dc/vblank.h>
#include <arch/irq.h>

static volatile uint32_t vbl_count;

static void on_vblank(uint32_t code, void *data) {   /* interrupt context */
    (void)code; (void)data;
    vbl_count++;
    genwait_wake_all((void *)&vbl_count);
}

void pacing_init(void) {
    thd_set_hz(333);                       /* JM-14: else the wakeup can land 10 ms late */
    vblank_handler_add(on_vblank, NULL);
}

/* Block until n vblanks have passed since the last call: n = 1 for 60 fps, 2 for 30 fps.
   A frame that already ran long doesn't wait at all. */
void pacing_wait(uint32_t n) {
    static uint32_t last;
    irq_mask_t old = irq_disable();       /* check-then-sleep without missing a wake */
    while ((int32_t)(vbl_count - (last + n)) < 0)
        genwait_wait((void *)&vbl_count, "pacing", 0, NULL);   /* 0 = no timeout */
    last = vbl_count;
    irq_restore(old);
}
```

Details from jnmartin's ports (sm64-dc, mk64-dc):

- **Use `genwait_wake_all`** in the vblank handler: if two threads sleep on the counter (the frame loop
  and an audio thread, say), `wake_one` wakes only one of them per vblank and starves the other.
- **Pace on a constant beat, never on a measured frame length.** Wait for `last + 2`; after a slow frame
  set `last` to the current count (resync) instead of carrying a debt. Pacing on what the previous frame
  measured ratchets: one hitch measures 3 vblanks, so the next waits 3, and so on.
- Their loops do the check without disabling interrupts; a vblank that slips between the check and the
  sleep then costs one extra vblank. The `irq_disable()` form above avoids that.
- **Profiling**: keep `PRFC0` as KOS's timer and put the event you are studying (a stall mode) on `PRFC1`.

Wall time is still needed for things that span sleeps (I/O durations, an audio clock, timeouts). Read it
sparingly, a few times a frame, never per primitive: mk64-dc's comment records ~10 ms a frame lost to
`timer_us_gettime64` at 5 reads a triangle (sf64-dc). jnmartin's ports use `timer_ns_gettime64()` for that
OS-level clock. Loops that poll a controller with `thd_sleep` (menus) can count passes instead of
reading a timer.

The same shape serves any producer/consumer hand-off: a loader thread finishing a file, an audio ring
needing a refill, a DMA completion callback. Prefer it to semaphores with timeouts: [ps2port] hit an
assertion race between `sem_wait_timed` and `sem_signal` (`sem.c`, "woken == 1").

## 4. The frame in flight: what the CPU may touch

KOS builds frame N+1 while the PVR renders frame N (rendering.md section 1). `pvr_wait_ready()` returns
when the TA is ready for new input, which is when the PVR *starts* rendering the previous frame, not when
it finishes [ps2port]. Therefore:

- Don't free and reuse VRAM a frame might still sample: defer frees by two frame begins or call
  `pvr_wait_render_done()` [ps2port].
- Don't overwrite a texture, palette bank or fog table that the frame in flight uses. Double-buffer
  dynamic textures and palette banks. Writing the fog table during a render hung the console [ps2port].
- A screenshot taken right after `pvr_wait_ready()` shows the previous frame [ps2port].
- Stop render threads before exiting: `pvr_wait_ready()` asserts on `pvr_state.valid` at shutdown
  otherwise [ps2port].

## 5. Threads and stacks

- `thd_create()` takes a 32 KB stack from the C heap. Use `thd_create_ex()` with explicit (ideally
  static) stacks, and paint them to measure the high-water mark [ps2port].
- The main thread runs on the kernel stack (`THD_KERNEL_STACK_SIZE`, 64 KB by default): no large arrays
  on the stack in `main()`'s call tree [ps2port].
- An I/O thread one priority level more urgent than default (`PRIO_DEFAULT - 1`) lets the main thread run
  while it waits on GD-ROM DMA [ps2port].
- The store queues are shared: KOS holds the SQ lock while a direct-rendered PVR list is open. Other
  threads moving data to sound RAM should use G2 PIO (`spu_memload`) rather than SQs [ps2port].

## 6. Main RAM

16 MB, shared by code, data, the C heap, thread stacks and KOS. What runs out first is contiguous space,
not total space: [ps2port] had 7.4 MB free and no 2.3 MB hole.

- Allocate big, long-lived blocks first and from one end; pool small long-lived objects; free per-scene
  data as a unit.
- Leave the C heap headroom (tens of KB) even if you run your own arena: newlib `FILE` buffers, sound
  streams and VMU access allocate from it, and growing `.bss` can make file opens and audio fail with
  "Out of memory" [ps2port]. Wrapping `malloc` (`-Wl,--wrap=malloc`) to log large and failed requests finds
  the culprit fast.
