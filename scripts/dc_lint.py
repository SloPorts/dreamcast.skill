#!/usr/bin/env python3
"""Scan Dreamcast/KallistiOS sources and build files for violations of the dreamcast skill's rules.

Usage: dc_lint.py [--json] [--kos-env PATH] [--exclude DIR ...] PATH [PATH ...]

Hits are candidates to read in context, not verdicts. Severities:
  error  a direct rule violation (e.g. pvr_prim, ARGB8888 palette, timer_us_gettime)
  warn   likely violation; check it (e.g. a non-twiddled format, a 64 KB array)
  info   worth a look (e.g. where pvr_poly_compile is called)
Exit status is 1 when any error is found, else 0.
"""
import argparse
import json
import os
import re
import sys

SRC_EXT = {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".inl", ".inc"}
BUILD_NAMES = {"makefile", "gnumakefile", "cmakelists.txt", "meson.build", "environ.sh"}
BUILD_EXT = {".mk", ".cmake", ".mak"}
SKIP_DIRS = {".git", ".svn", ".hg", "node_modules", "__pycache__"}

# (rule, severity, regex, message) applied per line of comment-stripped source.
LINE_CHECKS = [
    ("JM-1", "error", r"\bpvr_(?:list_)?prim\s*\(",
     "pvr_prim/pvr_list_prim copy a primitive already built in RAM; write it in place via "
     "pvr_dr_target() or pvr_vertbuf_tail()"),
    ("JM-2", "warn", r"PVR_TXRFMT_NONTWIDDLED|PVR_TXRFMT_STRIDE|\.nontwiddled\s*=\s*(?:1|true)\b",
     "non-twiddled texture: acceptable only for render-to-texture or YUV-converter targets"),
    ("JM-9", "info", r"\bpvr_txr_load_ex\s*\(",
     "twiddles on the CPU while loading; pre-twiddle at build time (pvrtex) and use pvr_txr_load"),
    ("JM-12", "error", r"\bPVR_PAL_ARGB8888\b",
     "ARGB8888 palette mode cuts fill rate severely (about half with filtering); use 1555/565/4444"),
    ("JM-15", "error", r"\btimer_(?:ms|us)_gettime(?:64)?\s*\(",
     "timer_ms/us_gettime* cause slowdown when used for timing; use perf_cntr_timer_ns() (JM-16)"),
    ("JM-16", "warn", r"\bperf_cntr_timer_disable\s*\(",
     "disables the nanosecond perf timer; perf_cntr_timer_ns() silently falls back to the TMU"),
    ("JM-17", "error", r"\btimer_spin_(?:sleep|delay_us|delay_ns)\s*\(",
     "spinning on a timer; block on genwait_wait() and wake from the event's interrupt"),
    ("JM-17", "error", r"\bwhile\s*\(.*\b(?:timer_\w*gettime\w*|perf_cntr_timer_ns)\s*\(.*\)\s*;",
     "busy-wait on a timer; block on genwait_wait() instead"),
    ("JM-17", "warn", r"\bthd_pass\s*\(\s*\)",
     "thd_pass() in a wait loop is still a spin; block on genwait_wait() instead"),
    ("JM-6", "warn", r"\b(?:memcpy|memmove|sq_cpy|sq_fast_cpy)\s*\(.*\b(?:pvr_vertex\w*|verts?|vtx\w*|vertices)\b",
     "copying vertex data; write each vertex once, into its final destination"),
]

PT_BLEND = re.compile(r"blend\.(?:src|dst)\s*=|\bm2\.blend_(?:src|dst)\s*=")
POLY_COMPILE = re.compile(r"\bpvr_poly_compile\s*\(")
PER_FRAME_HINT = re.compile(r"\bpvr_(?:list_begin|scene_begin|dr_target|dr_init|vertbuf_tail)\b")
USES_KOS = re.compile(r"#\s*include\s*[<\"](?:kos\.h|kos/|dc/|arch/)")
SET_HZ = re.compile(r"\bthd_set_hz\s*\(")

TYPE_SIZES = [
    (r"(?:u?int8_t|uint8|int8|u8|s8|(?:unsigned\s+|signed\s+)?char|bool|_Bool)", 1),
    (r"(?:u?int16_t|uint16|int16|u16|s16|(?:unsigned\s+)?short)", 2),
    (r"(?:u?int32_t|uint32|int32|u32|s32|f32|float|(?:unsigned\s+)?int|(?:unsigned\s+)?long|unsigned)", 4),
    (r"(?:u?int64_t|uint64|int64|u64|s64|double|(?:unsigned\s+)?long\s+long)", 8),
    (r"(?:pvr_vertex_t|pvr_poly_hdr_t|pvr_sprite_hdr_t)", 32),
]
ARRAY_DECL = re.compile(
    r"(?:^|[;{}(,]|\b(?:static|const|volatile|extern)\b)\s*(?P<type>(?:unsigned\s+|signed\s+)?[A-Za-z_]\w*(?:\s+long)?)"
    r"\s+\**\s*(?P<name>[A-Za-z_]\w*)\s*(?P<dims>(?:\[[^\[\]]+\])+)\s*(?:=|;|__attribute__)")
SAFE_EXPR = re.compile(r"^[0-9a-fA-FxXuUlL\s+\-*/()<>]+$")
DEFINE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)[ \t]+(\S[^\n]*?)[ \t]*$", re.M)
IDENT = re.compile(r"\b[A-Za-z_]\w*\b")

FLAG_FNO_BUILTIN = re.compile(r"(?<![\w-])-fno-builtin(?![\w-])")
FLAG_FBUILTIN = re.compile(r"(?<![\w-])-fbuiltin(?![\w-])")
FLAG_FAST_MATH = re.compile(r"(?<![\w-])-ffast-math(?![\w-])")
FLAG_FP_CONTRACT = re.compile(r"-ffp-contract=fast")
FLAG_NO_FSCA = re.compile(r"(?<![\w-])-mno-(?:fsca|fsrra)(?![\w-])")


def strip_c_comments(text):
    """Blank out // and /* */ comments and string literals, keeping line structure."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i)); i = j
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join(ch if ch == "\n" else " " for ch in text[i:j])); i = j
        elif c in "\"'":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            j = min(j + 1, n)
            out.append(c + " " * max(0, j - i - 2) + (c if j - i >= 2 else "")); i = j
        else:
            out.append(c); i += 1
    return "".join(out)


def strip_build_comments(text):
    return "\n".join(re.sub(r"(^|\s)#.*$", r"\1", line) for line in text.split("\n"))


FUNC_HEADER = re.compile(r"^[A-Za-z_][\w\s\*]*?\b([A-Za-z_]\w*)\s*\([^;]*$")


def enclosing_function(lines, i):
    """Name of the nearest column-0 function header at or above line i ('' if none)."""
    for j in range(i, -1, -1):
        l = lines[j]
        if l[:1] in (" ", "\t", "#", "}", "{", "") or l.startswith(("typedef", "struct", "static const", "return")):
            continue
        m = FUNC_HEADER.match(l)
        if m:
            return m.group(1)
    return ""


CXT_CALL = re.compile(r"\bpvr_poly_cxt_\w+\s*\(")


def pt_context(lines, i, back=20):
    """True if the nearest pvr_poly_cxt_*() call above line i names the PT list."""
    for j in range(i, max(-1, i - back), -1):
        if CXT_CALL.search(lines[j]):
            return "PT_POLY" in lines[j] + (lines[j + 1] if j + 1 < len(lines) else "")
    return False


def file_defines(text):
    """Object-like #defines of one file, so that buf[MAX_N * 4] can be sized."""
    return {m.group(1): m.group(2) for m in DEFINE.finditer(text)}


def expand(expr, defines):
    for _ in range(8):   # nested defines, a few levels deep
        new = IDENT.sub(lambda m: f"({defines[m.group(0)]})" if m.group(0) in defines else m.group(0), expr)
        if new == expr:
            break
        expr = new
    return expr


def eval_dims(dims, defines=None):
    total = 1
    for d in re.findall(r"\[([^\[\]]+)\]", dims):
        if defines:
            d = expand(d, defines)
        if not SAFE_EXPR.match(d) or "**" in d:
            return None
        expr = re.sub(r"(?<=[0-9a-fA-F])[uUlL]+\b", "", d)
        try:
            v = eval(expr, {"__builtins__": {}}, {})  # digits and operators only (SAFE_EXPR)
        except Exception:
            return None
        if not isinstance(v, int) or v <= 0:
            return None
        total *= v
    return total


def type_size(t):
    t = " ".join(t.split())
    for pat, size in TYPE_SIZES:
        if re.fullmatch(pat, t):
            return size
    return None


def walk(paths, excludes):
    for root in paths:
        if os.path.isfile(root):
            yield root
            continue
        for d, dirs, files in os.walk(root):
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS and os.path.join(d, x) not in excludes
                       and x not in excludes]
            for f in files:
                yield os.path.join(d, f)


def is_build_file(path):
    base = os.path.basename(path).lower()
    return base in BUILD_NAMES or os.path.splitext(base)[1] in BUILD_EXT or base.startswith("makefile")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--kos-env", help="KOS environ.sh to include in the build-flag check "
                    "(default: $KOS_BASE/environ.sh, else /opt/toolchains/dc/kos/environ.sh)")
    ap.add_argument("--exclude", action="append", default=[], help="directory name or path to skip")
    args = ap.parse_args()

    hits = []

    def hit(rule, sev, path, line, msg, code=""):
        hits.append({"rule": rule, "severity": sev, "file": path, "line": line,
                     "message": msg, "code": code.strip()[:160]})

    uses_kos = False
    has_set_hz = False
    build_texts = {}

    for path in walk(args.paths, set(args.exclude)):
        ext = os.path.splitext(path)[1].lower()
        if is_build_file(path):
            try:
                build_texts[path] = strip_build_comments(open(path, errors="replace").read())
            except OSError:
                pass
            continue
        if ext not in SRC_EXT:
            continue
        try:
            raw = open(path, errors="replace").read()
        except OSError:
            continue
        if USES_KOS.search(raw):
            uses_kos = True
        text = strip_c_comments(raw)
        lines = text.split("\n")
        raw_lines = raw.split("\n")
        if SET_HZ.search(text):
            has_set_hz = True
        file_has_pt = "PVR_LIST_PT_POLY" in text
        defines = file_defines(text)

        frame_lines = [i for i, l in enumerate(lines) if PER_FRAME_HINT.search(l)]
        for i, l in enumerate(lines):
            for rule, sev, pat, msg in LINE_CHECKS:
                if re.search(pat, l):
                    hit(rule, sev, path, i + 1, msg, raw_lines[i])
            if POLY_COMPILE.search(l):
                fn = enclosing_function(lines, i)
                setup = re.search(r"init|load|setup|create|compile|start|prepare|build_hdr|headers", fn, re.I)
                near = not setup and any(abs(i - j) <= 40 for j in frame_lines)
                hit("JM-7", "warn" if near else "info", path, i + 1,
                    ("pvr_poly_compile near per-frame submission code: compile headers once at load, "
                     "then edit fields (hdr.m0..m3) and resubmit only on change") if near else
                    "pvr_poly_compile: make sure this runs at load, not per frame", raw_lines[i])
            if file_has_pt and PT_BLEND.search(l) and pt_context(lines, i):
                hit("JM-3", "warn", path, i + 1,
                    "blend changed on what looks like a PT header: PT headers keep the default "
                    "SRCALPHA/INVSRCALPHA (a poly that needs another blend belongs in TR)", raw_lines[i])
            for m in ARRAY_DECL.finditer(l):
                count = eval_dims(m.group("dims"), defines)
                if count is None:
                    continue
                size = type_size(m.group("type"))
                nbytes = count * (size or 1)
                if nbytes >= 65536:
                    exact = f"{nbytes // 1024} KB" if size else f">= {count} elements (element size unknown)"
                    hit("JM-5", "warn", path, i + 1,
                        f"array '{m.group('name')}' is {exact}: larger than 4x the 16 KB operand cache",
                        raw_lines[i])

    if uses_kos and not has_set_hz:
        hit("JM-14", "warn", "(tree)", 0,
            "no thd_set_hz() call found: KOS schedules at 100 Hz (10 ms) by default; call thd_set_hz(333) at startup")

    kos_env = args.kos_env or os.path.join(os.environ.get("KOS_BASE", "/opt/toolchains/dc/kos"), "environ.sh")
    env_text = ""
    if os.path.isfile(kos_env):
        env_text = strip_build_comments(open(kos_env, errors="replace").read())

    if build_texts or env_text:
        all_build = "\n".join(build_texts.values()) + "\n" + env_text
        fno_builtin = FLAG_FNO_BUILTIN.search(all_build)
        fbuiltin = FLAG_FBUILTIN.search(all_build)
        if fno_builtin and not fbuiltin:
            hit("JM-18", "error", "(build)", 0,
                "-fno-builtin is set (KOS default) and -fbuiltin is not: sinf/cosf/sqrtf stay calls into "
                "newlib even with -ffast-math; add -fbuiltin")
        if not FLAG_FAST_MATH.search(all_build):
            hit("JM-18", "error", "(build)", 0,
                "no -ffast-math in the project's build files or KOS environ.sh: fsca/fsrra are never "
                "emitted; add -fbuiltin -ffast-math -ffp-contract=fast")
        if not FLAG_FP_CONTRACT.search(all_build):
            hit("JM-18", "info", "(build)", 0,
                "-ffp-contract=fast not set explicitly (GCC's GNU-mode default is already fast; "
                "-std=c99/c11 without gnu turns it off)")
        for p, t in build_texts.items():
            for i, l in enumerate(t.split("\n")):
                if FLAG_NO_FSCA.search(l):
                    hit("JM-18", "warn", p, i + 1,
                        "-mno-fsca/-mno-fsrra: acceptable only per file to dodge a compiler ICE, with the "
                        "instruction then called directly", l)

    order = {"error": 0, "warn": 1, "info": 2}
    hits.sort(key=lambda h: (order[h["severity"]], h["rule"], h["file"], h["line"]))

    if args.json:
        json.dump(hits, sys.stdout, indent=1)
        print()
    else:
        if not hits:
            print("dc_lint: no findings")
        for h in hits:
            loc = f"{h['file']}:{h['line']}" if h["line"] else h["file"]
            print(f"{loc}: {h['severity']} [{h['rule']}] {h['message']}")
            if h["code"]:
                print(f"    {h['code']}")
        counts = {s: sum(1 for h in hits if h["severity"] == s) for s in order}
        print(f"\n{counts['error']} error, {counts['warn']} warn, {counts['info']} info"
              + (f"  (KOS env: {kos_env})" if env_text else "  (no KOS environ.sh found)"))
    return 1 if any(h["severity"] == "error" for h in hits) else 0


if __name__ == "__main__":
    sys.exit(main())
