#!/usr/bin/env python3
"""Copy the single height range modifier in an Orca/Bambu 3MF project to all objects.

Usage: copy_height_range.py [FILE.3mf ...] [-o OUT.3mf]

Reads Metadata/layer_config_ranges.xml, which must contain exactly one range
(on any object), and rewrites it so every object carries an identical copy.
With no files given, all *.3mf in the current directory are processed. Files
are modified in place (a .bak copy is kept) unless -o is given.
"""
import argparse
import copy
import glob
import re
import shutil
import sys
import zipfile
import xml.etree.ElementTree as ET

MODEL = "3D/3dmodel.model"
RANGES = "Metadata/layer_config_ranges.xml"
ITEM_RE = re.compile(r'<item\b[^>]*\bobjectid="(\d+)"')


def object_count(zin):
    # Range ids are 1-based indices into the model's object list (build order).
    ids = ITEM_RE.findall(zin.read(MODEL).decode("utf-8"))
    return len(dict.fromkeys(ids))


def build_ranges(existing, n_objects):
    ranges = ET.fromstring(existing).findall("object/range")
    if len(ranges) != 1:
        raise ValueError(f"expected exactly 1 height range, found {len(ranges)}")
    rng = ranges[0]
    root = ET.Element("objects")
    for idx in range(1, n_objects + 1):
        ET.SubElement(root, "object", id=str(idx)).append(copy.deepcopy(rng))
    ET.indent(root, space=" ")
    xml = '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
    return xml, rng


def process(path, out):
    with zipfile.ZipFile(path) as zin:
        if RANGES not in zin.namelist():
            print(f"{path}: no {RANGES}, skipped", file=sys.stderr)
            return False
        n = object_count(zin)
        try:
            xml, rng = build_ranges(zin.read(RANGES), n)
        except ValueError as e:
            print(f"{path}: {e}, skipped", file=sys.stderr)
            return False
        if out is None:
            shutil.copy2(path, path + ".bak")
        tmp = (out or path) + ".tmp"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                data = xml.encode("utf-8") if info.filename == RANGES else zin.read(info)
                zout.writestr(info, data, compress_type=info.compress_type)
    shutil.move(tmp, out or path)
    opts = ", ".join(f"{o.get('opt_key')}={o.text}" for o in rng.findall("option"))
    print(f"{path}: copied {rng.get('min_z')}-{rng.get('max_z')} mm range ({opts}) "
          f"to {n} object(s)" + (f" -> {out}" if out else ""))
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="*", help="3mf files (default: *.3mf in cwd)")
    ap.add_argument("-o", "--output", help="output file (only with a single input)")
    args = ap.parse_args()

    files = args.files or sorted(glob.glob("*.3mf"))
    if not files:
        ap.error("no .3mf files found")
    if args.output and len(files) != 1:
        ap.error("-o requires exactly one input file")
    ok = [process(f, args.output) for f in files]
    sys.exit(0 if all(ok) else 1)


if __name__ == "__main__":
    main()
