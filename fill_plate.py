#!/usr/bin/env python3
"""Fill a plate of an Orca/Bambu 3MF project with copies of its "start " object.

Usage: fill_plate.py FILE.3mf PLATE COUNT [-o OUT.3mf]

PLATE is the plate name (or its 1-based number). The plate must hold exactly
one object whose name starts with "start ". Every other object on the plate is
removed, then copies of the start object are added until the plate holds COUNT
objects. Copies are independent objects (own settings) sharing the start
object's meshes, and are named without the "start " prefix. They take the
positions of the removed objects; any beyond that overlap the start object and
need arranging in the slicer. Other plates are untouched. The file is modified
in place (a .bak copy is kept) unless -o is given.
"""
import argparse
import html
import re
import shutil
import sys
import zipfile

MODEL = "3D/3dmodel.model"
MODEL_RELS = "3D/_rels/3dmodel.model.rels"
CONFIG = "Metadata/model_settings.config"
RANGES = "Metadata/layer_config_ranges.xml"
PREFIX = "start "

# The files are edited as text: model_settings.config uses undeclared
# namespace prefixes, and this keeps everything not touched byte-identical.
MODEL_OBJ_RE = re.compile(r'^  <object id="(\d+)".*?^  </object>\n', re.M | re.S)
ITEM_RE = re.compile(r'^  <item objectid="(\d+)"[^>]*/>\n', re.M)
COMPONENT_PATH_RE = re.compile(r'<component p:path="([^"]+)"')
CFG_OBJ_RE = re.compile(r'^  <object id="(\d+)">.*?^  </object>\n', re.M | re.S)
PLATE_RE = re.compile(r"^  <plate>.*?^  </plate>\n", re.M | re.S)
INSTANCE_RE = re.compile(r"^    <model_instance>.*?^    </model_instance>\n", re.M | re.S)
ASSEMBLE_RE = re.compile(r'^   <assemble_item object_id="(\d+)"[^>]*/>\n', re.M)
RANGE_OBJ_RE = re.compile(r'^ <object id="(\d+)">.*?^ </object>\n', re.M | re.S)
REL_RE = re.compile(r'^ <Relationship Target="([^"]+)"[^>]*/>\n', re.M)


def meta_value(block, key):
    m = re.search(rf'<metadata key="{key}" value="([^"]*)"', block)
    return html.unescape(m.group(1)) if m else None


def set_attr(text, attr, value):
    return re.sub(rf'\b{attr}="[^"]*"', f'{attr}="{value}"', text, count=1)


def renumber_uuids(obj, ordinal):
    # Bambu UUIDs start with the object's ordinal (8 hex digits), or for a
    # component, the ordinal (4) then the component index (4); keep that scheme.
    obj = re.sub(r'(<object [^>]*p:UUID=")[0-9a-f]{8}', rf"\g<1>{ordinal:08x}", obj, count=1)
    return re.sub(r'(<component [^>]*p:UUID=")[0-9a-f]{4}', rf"\g<1>{ordinal:04x}", obj)


def find_plate(cfg, plate_arg):
    plates = PLATE_RE.findall(cfg)
    for key in ("plater_name", "plater_id"):  # fall back to the plate number
        matches = [p for p in plates if meta_value(p, key) == plate_arg]
        if matches:
            return matches[0]
    avail = ", ".join(repr(meta_value(p, "plater_name") or "") for p in plates)
    sys.exit(f"no plate {plate_arg!r}; available: {avail}")


def fill(zin, plate_arg, count):
    model = zin.read(MODEL).decode("utf-8")
    cfg = zin.read(CONFIG).decode("utf-8")

    plate = find_plate(cfg, plate_arg)
    instances = INSTANCE_RE.findall(plate)
    plate_ids = list(dict.fromkeys(meta_value(i, "object_id") for i in instances))
    cfg_objs = {m.group(1): m.group(0) for m in CFG_OBJ_RE.finditer(cfg)}
    names = {oid: meta_value(cfg_objs[oid], "name") or "" for oid in plate_ids}
    starts = [oid for oid in plate_ids if names[oid].startswith(PREFIX)]
    if len(starts) != 1:
        found = ", ".join(repr(names[o]) for o in starts) or "none"
        sys.exit(f"expected exactly 1 object named {PREFIX!r}... on the plate, found {found}")
    src = starts[0]
    removed = [oid for oid in plate_ids if oid != src]
    gone = set(removed)

    model_objs = {m.group(1): m.group(0) for m in MODEL_OBJ_RE.finditer(model)}
    items = {m.group(1): m.group(0) for m in ITEM_RE.finditer(model)}
    old_order = [m.group(1) for m in ITEM_RE.finditer(model)]
    assemble = {m.group(1): m.group(0) for m in ASSEMBLE_RE.finditer(cfg)}
    src_instance = next(i for i in instances if meta_value(i, "object_id") == src)

    next_id = max(map(int, model_objs)) + 1
    next_ordinal = max(int(u, 16) for u in
                       re.findall(r'<object [^>]*p:UUID="([0-9a-f]{8})', model)) + 1
    next_identify = max(int(v) for v in re.findall(r'key="identify_id" value="(\d+)"', cfg)) + 1
    src_xform = re.search(r'transform="([^"]*)"', items[src]).group(1).split()
    slots = [re.search(r'transform="([^"]*)"', items[o]).group(1).split() for o in removed]

    new_obj = new_item = new_cfg = new_inst = new_asm = ""
    copies = []  # new object ids, in order
    for n in range(count - 1):
        oid = str(next_id + n)
        copies.append(oid)
        xform = src_xform[:]
        if n < len(slots):  # take the x/y of a removed object's position
            xform[9:11] = slots[n][9:11]
        new_obj += renumber_uuids(set_attr(model_objs[src], "id", oid), next_ordinal + n)
        item = set_attr(items[src], "objectid", oid)
        item = re.sub(r'(p:UUID=")[0-9a-f]{8}', rf"\g<1>{int(oid):08x}", item, count=1)
        new_item += set_attr(item, "transform", " ".join(xform))
        obj_cfg = set_attr(cfg_objs[src], "id", oid)
        name = html.escape(names[src][len(PREFIX):], quote=True)
        obj_cfg = re.sub(r'(<metadata key="name" value=")[^"]*', rf"\g<1>{name}", obj_cfg, count=1)
        new_cfg += obj_cfg
        inst = re.sub(r'(key="object_id" value=")\d+', rf"\g<1>{oid}", src_instance)
        new_inst += re.sub(r'(key="identify_id" value=")\d+', rf"\g<1>{next_identify + n}", inst)
        if src in assemble:
            new_asm += set_attr(assemble[src], "object_id", oid)

    # 3dmodel.model: drop removed objects/items, append copies.
    model = MODEL_OBJ_RE.sub(lambda m: "" if m.group(1) in gone else m.group(0), model)
    model = ITEM_RE.sub(lambda m: "" if m.group(1) in gone else m.group(0), model)
    model = model.replace(" </resources>\n", new_obj + " </resources>\n", 1)
    model = model.replace(" </build>\n", new_item + " </build>\n", 1)

    # model_settings.config: objects, this plate's instances, assemble items.
    cfg = CFG_OBJ_RE.sub(lambda m: "" if m.group(1) in gone else m.group(0), cfg)
    kept = [i for i in instances if meta_value(i, "object_id") not in gone]
    new_plate = INSTANCE_RE.sub("", plate)
    new_plate = new_plate.replace("  </plate>\n", "".join(kept) + new_inst + "  </plate>\n")
    cfg = cfg.replace(plate, new_plate, 1)
    last_obj = list(CFG_OBJ_RE.finditer(cfg))[-1]
    cfg = cfg[:last_obj.end()] + new_cfg + cfg[last_obj.end():]
    cfg = ASSEMBLE_RE.sub(lambda m: "" if m.group(1) in gone else m.group(0), cfg)
    cfg = cfg.replace("  </assemble>\n", new_asm + "  </assemble>\n", 1)

    # Sub-model files no longer referenced by any component.
    used = set(COMPONENT_PATH_RE.findall(model))
    all_paths = {"/" + n for n in zin.namelist() if n.startswith("3D/Objects/")}
    dropped = {p.lstrip("/") for p in all_paths - used}

    out = {MODEL: model, CONFIG: cfg}
    if MODEL_RELS in zin.namelist():
        rels = zin.read(MODEL_RELS).decode("utf-8")
        out[MODEL_RELS] = REL_RE.sub(lambda m: m.group(0) if m.group(1) in used else "", rels)

    # Height ranges are keyed by 1-based object index in build order.
    if RANGES in zin.namelist():
        ranges = zin.read(RANGES).decode("utf-8")
        by_idx = {m.group(1): m.group(0) for m in RANGE_OBJ_RE.finditer(ranges)}
        by_obj = {oid: by_idx.get(str(i)) for i, oid in enumerate(old_order, 1)}
        new_order = [o for o in old_order if o not in gone] + copies
        body = ""
        for i, oid in enumerate(new_order, 1):
            block = by_obj.get(oid if oid not in copies else src)
            if block:
                body += set_attr(block, "id", str(i))
        head = ranges[:RANGE_OBJ_RE.search(ranges).start()] if by_idx else ranges.replace("</objects>", "")
        out[RANGES] = head + body + "</objects>\n"

    return out, dropped, names[src], removed


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file")
    ap.add_argument("plate")
    ap.add_argument("count", type=int, help="number of objects on the plate afterwards")
    ap.add_argument("-o", "--output", help="output file (default: modify in place)")
    args = ap.parse_args()
    if args.count < 1:
        ap.error("count must be at least 1")

    with zipfile.ZipFile(args.file) as zin:
        out, dropped, name, removed = fill(zin, args.plate, args.count)
        if args.output is None:
            shutil.copy2(args.file, args.file + ".bak")
        dest = args.output or args.file
        tmp = dest + ".tmp"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                if info.filename in dropped:
                    continue
                data = out[info.filename].encode("utf-8") if info.filename in out else zin.read(info)
                zout.writestr(info, data, compress_type=info.compress_type)
    shutil.move(tmp, dest)

    print(f"{args.file}: plate {args.plate!r}: removed {len(removed)}, "
          f"now {args.count} x {name!r}" + (f" -> {args.output}" if args.output else ""))
    if args.count - 1 > len(removed):
        print(f"{args.count - 1 - len(removed)} copies overlap the start object; "
              "arrange the plate in the slicer", file=sys.stderr)


if __name__ == "__main__":
    main()
