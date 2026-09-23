#!/usr/bin/env python3
"""Change the font of every text modifier in an Orca/Bambu/Prusa 3MF project.

Usage: set_text_font.py FONT [FILE.3mf ...] [-o OUT.3mf]

Rewrites <slic3rpe:text> elements in Metadata/model_settings.config, keeping each
entry's descriptor type (Linux/Windows/macOS) and font size. With no files given,
all *.3mf in the current directory are processed. Files are modified in place
(a .bak copy is kept) unless -o is given.
"""
import argparse
import glob
import re
import shutil
import sys
import zipfile
from xml.sax.saxutils import quoteattr

CONFIG = "Metadata/model_settings.config"
TEXT_RE = re.compile(r"<slic3rpe:text\b[^>]*/>")
ATTR_RE = r'(\b{}=)"([^"]*)"'


def get_attr(elem, name):
    m = re.search(ATTR_RE.format(name), elem)
    return m.group(2) if m else None


def set_attr(elem, name, value):
    q = quoteattr(value)
    new, n = re.subn(ATTR_RE.format(name), lambda m: m.group(1) + q, elem)
    if n == 0:
        new = re.sub(r"\s*/>$", f" {name}={q} />", elem)
    return new


def new_descriptor(desc, desc_type, font):
    if desc is None:
        return None
    if desc_type == "wxFontDescriptor_Windows":
        # version;pointSize;lfHeight;...;lfPitchAndFamily;faceName
        parts = desc.split(";")
        return ";".join(parts[:15] + [font])
    # Linux (Pango "Family [Style] Size") and others: keep the trailing size
    size = desc.rsplit(" ", 1)[-1] if " " in desc else ""
    return f"{font} {size}" if re.fullmatch(r"\d+(\.\d+)?", size) else font


def replace_text(elem, font):
    desc = new_descriptor(get_attr(elem, "font_descriptor"),
                          get_attr(elem, "font_descriptor_type"), font)
    if desc is not None:
        elem = set_attr(elem, "font_descriptor", desc)
    return set_attr(elem, "face_name", font)


def process(path, font, out):
    with zipfile.ZipFile(path) as zin:
        if CONFIG not in zin.namelist():
            print(f"{path}: no {CONFIG}, skipped", file=sys.stderr)
            return
        cfg = zin.read(CONFIG).decode("utf-8")
        new_cfg, count = TEXT_RE.subn(lambda m: replace_text(m.group(0), font), cfg)
        if out is None:
            shutil.copy2(path, path + ".bak")
        tmp = (out or path) + ".tmp"
        with zipfile.ZipFile(tmp, "w") as zout:
            for info in zin.infolist():
                data = new_cfg.encode("utf-8") if info.filename == CONFIG else zin.read(info)
                zout.writestr(info, data, compress_type=info.compress_type)
    shutil.move(tmp, out or path)
    print(f"{path}: set {count} text modifier(s) to '{font}'" + (f" -> {out}" if out else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("font", help="font face name, e.g. 'DejaVu Sans'")
    ap.add_argument("files", nargs="*", help="3mf files (default: *.3mf in cwd)")
    ap.add_argument("-o", "--output", help="output file (only with a single input)")
    args = ap.parse_args()

    files = args.files or sorted(glob.glob("*.3mf"))
    if not files:
        ap.error("no .3mf files found")
    if args.output and len(files) != 1:
        ap.error("-o requires exactly one input file")
    for f in files:
        process(f, args.font, args.output)


if __name__ == "__main__":
    main()
