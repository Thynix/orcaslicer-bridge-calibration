#!/usr/bin/env python3
"""Print the names of the objects on a plate of an Orca/Bambu 3MF project.

Usage: list_plate_objects.py FILE.3mf [PLATE]

PLATE is the plate name (or its 1-based number). It may be omitted if the
project has only one plate.
"""
import argparse
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

CONFIG = "Metadata/model_settings.config"


def meta(elem):
    return {m.get("key"): m.get("value") for m in elem.findall("metadata")}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file")
    ap.add_argument("plate", nargs="?")
    args = ap.parse_args()

    with zipfile.ZipFile(args.file) as z:
        text = z.read(CONFIG).decode("utf-8")
    # Orca writes elements like <slic3rpe:text> without declaring the prefix.
    root = ET.fromstring(re.sub(r"<(/?)[\w.-]+:", r"<\1", text))

    names = {o.get("id"): meta(o).get("name", "") for o in root.findall("object")}
    plates = [(meta(p), p) for p in root.findall("plate")]

    if args.plate is None:
        if len(plates) != 1:
            avail = ", ".join(repr(m.get("plater_name", "")) for m, _ in plates)
            sys.exit(f"{len(plates)} plates; specify one of: {avail}")
        plate = plates[0][1]
    else:
        matches = [p for m, p in plates if m.get("plater_name") == args.plate]
        if not matches:  # fall back to the plate number
            matches = [p for m, p in plates if m.get("plater_id") == args.plate]
        if not matches:
            avail = ", ".join(repr(m.get("plater_name", "")) for m, _ in plates)
            sys.exit(f"no plate {args.plate!r}; available: {avail}")
        plate = matches[0]

    # A plate lists one model_instance per copy; print each object once.
    seen = set()
    for inst in plate.findall("model_instance"):
        oid = meta(inst).get("object_id")
        if oid not in seen:
            seen.add(oid)
            print(names.get(oid, f"<object {oid}>"))


if __name__ == "__main__":
    main()
