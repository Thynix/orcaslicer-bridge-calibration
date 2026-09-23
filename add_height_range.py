#!/usr/bin/env python3
"""Add a height range modifier to every object in an Orca/Bambu 3MF project.

Usage: add_height_range.py [FILE.3mf ...] [--min Z] [--max Z] [--speed MM_S] [-o OUT.3mf]

Writes Metadata/layer_config_ranges.xml with a range per object that sets
inner_wall_speed and outer_wall_speed. The range also carries layer_height
(from the project settings), as ranges added in the GUI always do. An existing
range with the same bounds is replaced; other ranges are kept. With no files
given, all *.3mf in the current directory are processed. Files are modified in
place (a .bak copy is kept) unless -o is given.
"""
import argparse
import glob
import json
import re
import shutil
import sys
import zipfile
import xml.etree.ElementTree as ET

MODEL = "3D/3dmodel.model"
PROJECT = "Metadata/project_settings.config"
RANGES = "Metadata/layer_config_ranges.xml"
ITEM_RE = re.compile(r'<item\b[^>]*\bobjectid="(\d+)"')


def fmt(v):
    return f"{v:g}"


def object_count(zin):
    # Range ids are 1-based indices into the model's object list (build order).
    ids = ITEM_RE.findall(zin.read(MODEL).decode("utf-8"))
    return len(dict.fromkeys(ids))


def build_ranges(existing, n_objects, zmin, zmax, options):
    root = ET.fromstring(existing) if existing else ET.Element("objects")
    for idx in range(1, n_objects + 1):
        obj = root.find(f"object[@id='{idx}']")
        if obj is None:
            obj = ET.SubElement(root, "object", id=str(idx))
        for r in obj.findall("range"):
            if float(r.get("min_z")) == zmin and float(r.get("max_z")) == zmax:
                obj.remove(r)
        rng = ET.SubElement(obj, "range", min_z=fmt(zmin), max_z=fmt(zmax))
        for key, val in options.items():
            ET.SubElement(rng, "option", opt_key=key).text = val
        obj[:] = sorted(obj, key=lambda r: float(r.get("min_z")))
    ET.indent(root, space=" ")
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def process(path, zmin, zmax, speed, out):
    with zipfile.ZipFile(path) as zin:
        names = zin.namelist()
        n = object_count(zin)
        settings = json.loads(zin.read(PROJECT)) if PROJECT in names else {}
        options = {
            "layer_height": settings.get("layer_height", "0.2"),
            "inner_wall_speed": fmt(speed),
            "outer_wall_speed": fmt(speed),
        }
        existing = zin.read(RANGES) if RANGES in names else None
        xml = build_ranges(existing, n, zmin, zmax, options).encode("utf-8")
        if out is None:
            shutil.copy2(path, path + ".bak")
        tmp = (out or path) + ".tmp"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                if info.filename != RANGES:
                    zout.writestr(info, zin.read(info), compress_type=info.compress_type)
            zout.writestr(RANGES, xml)
    shutil.move(tmp, out or path)
    print(f"{path}: added {fmt(zmin)}-{fmt(zmax)} mm range (walls {fmt(speed)} mm/s) "
          f"to {n} object(s)" + (f" -> {out}" if out else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="*", help="3mf files (default: *.3mf in cwd)")
    ap.add_argument("--min", type=float, default=4.20, help="range start in mm (default 4.20)")
    ap.add_argument("--max", type=float, default=5.00, help="range end in mm (default 5.00)")
    ap.add_argument("--speed", type=float, default=20, help="wall speed in mm/s (default 20)")
    ap.add_argument("-o", "--output", help="output file (only with a single input)")
    args = ap.parse_args()

    if args.min >= args.max:
        ap.error("--min must be below --max")
    files = args.files or sorted(glob.glob("*.3mf"))
    if not files:
        ap.error("no .3mf files found")
    if args.output and len(files) != 1:
        ap.error("-o requires exactly one input file")
    for f in files:
        process(f, args.min, args.max, args.speed, args.output)


if __name__ == "__main__":
    main()
