#!/usr/bin/env python3
"""Fill a plate of an Orca/Bambu 3MF project with bridge flow/density test copies.

Usage: bridge_plate.py FILE.3mf COUNT FLOW MIN_DENSITY MAX_DENSITY [-o OUT.3mf]

The project must have a plate named "start" holding exactly one object, and at
most one other plate. That other plate is emptied and reused, or a new plate
is added if there is none. COUNT copies of the start object are laid out in a
grid on it, with bridge_flow and internal_bridge_flow set to FLOW and
bridge_density stepping from MIN_DENSITY to MAX_DENSITY (percent, inclusive).
Each copy's name, part names and text are set to "FLOW-DENSITY", e.g.
"1.3-104", and the plate is named "Flow Factor FLOW". The start plate is
untouched. The file is modified in place (a .bak copy is kept) unless -o is
given.

Only the text settings are changed, not the text mesh: the copies share the
start object's text geometry until the text is regenerated in the slicer.
"""
import argparse
import html
import json
import math
import re
import shutil
import sys
import zipfile

from fill_plate import (
    ASSEMBLE_RE, CFG_OBJ_RE, COMPONENT_PATH_RE, CONFIG, INSTANCE_RE, ITEM_RE,
    MODEL, MODEL_OBJ_RE, MODEL_RELS, PLATE_RE, RANGE_OBJ_RE, RANGES, REL_RE,
    meta_value, renumber_uuids, set_attr,
)

OBJ_META_RE = re.compile(r'^    <metadata key="([^"]+)" value="[^"]*"/>\n', re.M)
PART_RE = re.compile(r"^    <part .*?^    </part>\n", re.M | re.S)
TEXT_RE = re.compile(r'(<slic3rpe:text\b[^>]*\btext=")[^"]*"')
START = "start"
PROJECT = "Metadata/project_settings.config"
GAP = 5  # minimum mm between copies
# Orca lays plates out in a grid, LOGICAL_PART_PLATE_GAP apart (PartPlate.cpp).
PLATE_GAP = 1 / 5
PLATE_META_DROP_RE = re.compile(
    r'^    <metadata key="(?:thumbnail_file|thumbnail_no_light_file|top_file|pick_file)"[^\n]*\n', re.M)


def fmt(value):
    return f"{round(value, 4):g}"


def relabel(obj_cfg, oid, label, settings):
    """Return a config <object> block with a new id, settings, and name, part names and text set to label."""
    obj_cfg = set_attr(obj_cfg, "id", oid)
    head_end = obj_cfg.index("    <part ") if "    <part " in obj_cfg else obj_cfg.index("  </object>")
    head, rest = obj_cfg[:head_end], obj_cfg[head_end:]
    # Object-level metadata: name first, then keys sorted, as Orca writes them.
    meta = {m.group(1): meta_value(m.group(0), m.group(1)) for m in OBJ_META_RE.finditer(head)}
    meta.pop("name", None)
    meta.update(settings)
    lines = [(k, meta[k]) for k in sorted(meta)]
    head = OBJ_META_RE.sub("", head) + "".join(
        f'    <metadata key="{k}" value="{html.escape(v, quote=True)}"/>\n'
        for k, v in [("name", label)] + lines)
    esc = html.escape(label, quote=True)

    def relabel_part(m):
        part = re.sub(r'(<metadata key="name" value=")[^"]*', rf"\g<1>{esc}", m.group(0), count=1)
        return TEXT_RE.sub(lambda t: f'{t.group(1)}{esc}"', part)

    return head + PART_RE.sub(relabel_part, rest)


def xform(values, point):
    """Apply a 3MF transform (12 floats, row-vector convention) to a point."""
    m = list(map(float, values))
    x, y, z = point
    return tuple(x * m[i] + y * m[3 + i] + z * m[6 + i] + m[9 + i] for i in range(3))


def footprint(zin, model_obj, rotation):
    """X/Y bounding box of a top-level object in its own frame, rotated as placed."""
    xs, ys = [], []
    meshes = {}
    for comp in re.finditer(r'<component p:path="([^"]+)" objectid="(\d+)"[^>]*transform="([^"]*)"', model_obj):
        path, oid, comp_xform = comp.groups()
        if path not in meshes:
            meshes[path] = zin.read(path.lstrip("/")).decode("utf-8")
        body = re.search(rf'<object id="{oid}".*?</object>', meshes[path], re.S).group(0)
        for v in re.finditer(r'<vertex x="([^"]+)" y="([^"]+)" z="([^"]+)"', body):
            x, y, _ = xform(rotation + ["0"] * 3, xform(comp_xform.split(), map(float, v.groups())))
            xs.append(x)
            ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def plate_origin(index, plate_count, bed):
    x0, y0, x1, y1 = bed
    cols = math.ceil(round(math.sqrt(plate_count), 6))  # compute_colum_count
    row, col = divmod(index, cols)
    return col * (x1 - x0) * (1 + PLATE_GAP), -row * (y1 - y0) * (1 + PLATE_GAP)


def grid(count, box, bed, origin):
    """X/Y translations spreading COUNT objects with footprint BOX evenly over the bed."""
    bx0, by0, bx1, by1 = box
    w, h = bx1 - bx0, by1 - by0
    x0, y0, x1, y1 = bed
    max_cols = max(1, int((x1 - x0 - GAP) // (w + GAP)))
    rows = math.ceil(count / max_cols)
    cols = math.ceil(count / rows)
    gap_x = (x1 - x0 - cols * w) / (cols + 1)
    gap_y = (y1 - y0 - rows * h) / (rows + 1)
    spots = []
    for n in range(count):
        row, col = divmod(n, cols)
        spots.append((origin[0] + x0 + gap_x + col * (w + gap_x) - bx0,
                      origin[1] + y1 - (row + 1) * (h + gap_y) - by0))
    return spots, gap_y >= GAP


def build(zin, labels, plate_name):
    model = zin.read(MODEL).decode("utf-8")
    cfg = zin.read(CONFIG).decode("utf-8")
    area = json.loads(zin.read(PROJECT))["printable_area"]
    pts = [tuple(map(float, p.split("x"))) for p in area]
    bed = (min(p[0] for p in pts), min(p[1] for p in pts),
           max(p[0] for p in pts), max(p[1] for p in pts))

    plates = PLATE_RE.findall(cfg)
    names = [meta_value(p, "plater_name") or "" for p in plates]
    if names.count(START) != 1 or len(plates) > 2:
        sys.exit(f"expected a plate named {START!r} and at most one other; found "
                 + ", ".join(map(repr, names)))
    start_plate = plates[names.index(START)]
    start_instances = INSTANCE_RE.findall(start_plate)
    start_ids = list(dict.fromkeys(meta_value(i, "object_id") for i in start_instances))
    cfg_objs = {m.group(1): m.group(0) for m in CFG_OBJ_RE.finditer(cfg)}
    if len(start_ids) != 1:
        found = ", ".join(repr(meta_value(cfg_objs[o], "name")) for o in start_ids) or "none"
        sys.exit(f"expected exactly 1 object on the {START!r} plate, found {found}")
    src = start_ids[0]

    if len(plates) == 2:
        plate = plates[1 - names.index(START)]
        target_index = plates.index(plate)
    else:  # new plate after the start plate, without its thumbnails
        plate = ""
        target_index = 1
        new_id = max(int(meta_value(p, "plater_id")) for p in plates) + 1
        blank = PLATE_META_DROP_RE.sub("", INSTANCE_RE.sub("", start_plate))
        blank = re.sub(r'(key="plater_id" value=")\d+', rf"\g<1>{new_id}", blank, count=1)
    instances = INSTANCE_RE.findall(plate)
    removed = list(dict.fromkeys(meta_value(i, "object_id") for i in instances))
    gone = set(removed)

    model_objs = {m.group(1): m.group(0) for m in MODEL_OBJ_RE.finditer(model)}
    items = {m.group(1): m.group(0) for m in ITEM_RE.finditer(model)}
    old_order = [m.group(1) for m in ITEM_RE.finditer(model)]
    assemble = {m.group(1): m.group(0) for m in ASSEMBLE_RE.finditer(cfg)}
    src_instance = start_instances[0]

    next_id = max(map(int, model_objs)) + 1
    next_ordinal = max(int(u, 16) for u in
                       re.findall(r'<object [^>]*p:UUID="([0-9a-f]{8})', model)) + 1
    next_identify = max(int(v) for v in re.findall(r'key="identify_id" value="(\d+)"', cfg)) + 1
    src_xform = re.search(r'transform="([^"]*)"', items[src]).group(1).split()
    box = footprint(zin, model_objs[src], src_xform[:9])
    origin = plate_origin(target_index, max(len(plates), 2), bed)
    spots, fits = grid(len(labels), box, bed, origin)

    new_obj = new_item = new_cfg = new_inst = new_asm = ""
    copies = []  # new object ids, in order
    for n, (label, settings) in enumerate(labels):
        oid = str(next_id + n)
        copies.append(oid)
        placed = src_xform[:]
        placed[9:11] = (f"{v:.6g}" for v in spots[n])
        new_obj += renumber_uuids(set_attr(model_objs[src], "id", oid), next_ordinal + n)
        item = set_attr(items[src], "objectid", oid)
        item = re.sub(r'(p:UUID=")[0-9a-f]{8}', rf"\g<1>{int(oid):08x}", item, count=1)
        new_item += set_attr(item, "transform", " ".join(placed))
        new_cfg += relabel(cfg_objs[src], oid, label, settings)
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
    new_plate = INSTANCE_RE.sub("", plate) if plate else blank
    name_line = f'    <metadata key="plater_name" value="{html.escape(plate_name, quote=True)}"/>\n'
    new_plate, renamed = re.subn(r'^    <metadata key="plater_name" value="[^"]*"/>\n',
                                 lambda m: name_line, new_plate, count=1, flags=re.M)
    if not renamed:
        new_plate = re.sub(r'(^    <metadata key="plater_id" [^\n]*\n)',
                           lambda m: m.group(1) + name_line, new_plate, count=1, flags=re.M)
    new_plate = new_plate.replace("  </plate>\n", new_inst + "  </plate>\n")
    if plate:
        cfg = cfg.replace(plate, new_plate, 1)
    else:
        last_plate = list(PLATE_RE.finditer(cfg))[-1]
        cfg = cfg[:last_plate.end()] + new_plate + cfg[last_plate.end():]
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

    return out, dropped, removed, fits, bool(plate)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file")
    ap.add_argument("count", type=int, help="number of copies to add")
    ap.add_argument("flow", type=float, help="bridge flow ratio")
    ap.add_argument("min_density", type=float, help="first bridge density, percent")
    ap.add_argument("max_density", type=float, help="last bridge density, percent")
    ap.add_argument("-o", "--output", help="output file (default: modify in place)")
    args = ap.parse_args()
    if args.count < 1:
        ap.error("count must be at least 1")

    flow = fmt(args.flow)
    step = (args.max_density - args.min_density) / max(args.count - 1, 1)
    labels = []
    for n in range(args.count):
        density = fmt(args.min_density + step * n)
        labels.append((f"{flow}-{density}", {
            "bridge_flow": flow,
            "internal_bridge_flow": flow,
            "bridge_density": f"{density}%",
        }))

    with zipfile.ZipFile(args.file) as zin:
        out, dropped, removed, fits, reused = build(zin, labels, f"Flow Factor {flow}")
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

    print(f"{args.file}: {'reused' if reused else 'added'} plate 'Flow Factor {flow}'"
          + (f", removed {len(removed)}" if removed else "") + ", added "
          + ", ".join(label for label, _ in labels)
          + (f" -> {args.output}" if args.output else ""))
    if not fits:
        print("copies don't fit on the plate; arrange it in the slicer", file=sys.stderr)
    print("text meshes are unchanged; regenerate each text in the slicer", file=sys.stderr)


if __name__ == "__main__":
    main()
