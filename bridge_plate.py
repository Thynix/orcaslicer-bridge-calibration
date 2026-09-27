#!/usr/bin/env python3
"""Fill a plate of an Orca/Bambu 3MF project with bridge flow/density test copies.

Usage: bridge_plate.py FILE.3mf COUNT FLOW MIN_DENSITY MAX_DENSITY [-o OUT.3mf]

The project must have a plate named "reference" holding exactly one object, and
at most one other plate. That other plate is emptied and reused, or a new plate
is added if there is none. COUNT copies of the reference object are placed on
it at the X/Y positions of the plate's former objects, in their plate order, or
laid out in a grid if there were fewer than COUNT of them, with bridge_flow and
internal_bridge_flow set to FLOW and bridge_density stepping from MIN_DENSITY
to MAX_DENSITY (percent, inclusive).
Each copy's name, part names and text are set to "FLOW-DENSITY", e.g.
"1.3-104", and the plate is named "Flow Factor FLOW". The reference plate is
untouched. The file is modified in place (a .bak copy is kept) unless -o is
given.

Each copy's text part gets its own sub-model file holding an empty mesh, which
a patched OrcaSlicer (branch rebuild-empty-text-on-load) rebuilds from the text
settings on load. Stock Orca drops such text parts.
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
START = "reference"
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


def empty_text_mesh(zin, model_obj, text_id, path):
    """Point MODEL_OBJ's text component at a new sub-model file PATH holding an
    empty mesh; return the updated object and the new file's contents."""
    comp = re.search(rf'<component p:path="([^"]+)" objectid="{text_id}" p:UUID="([0-9a-f]{{8}})[^>]*>',
                     model_obj)
    src_path, uuid_prefix = comp.groups()
    src = zin.read(src_path.lstrip("/")).decode("utf-8")
    uuid_suffix = re.search(rf'^  <object id="{text_id}" p:UUID="[0-9a-f]{{8}}([^"]*)"', src, re.M).group(1)
    head = src[:src.index(" <resources>\n") + len(" <resources>\n")]
    tail = src[src.index(" </resources>\n"):]
    # Sub-model object UUIDs share the prefix of the component referencing them.
    obj = (f'  <object id="{text_id}" p:UUID="{uuid_prefix}{uuid_suffix}" type="model">\n'
           "   <mesh>\n    <vertices>\n    </vertices>\n    <triangles>\n    </triangles>\n   </mesh>\n"
           "  </object>\n")
    new_comp = comp.group(0).replace(f'p:path="{src_path}"', f'p:path="{path}"', 1)
    return model_obj.replace(comp.group(0), new_comp, 1), head + obj + tail


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
    gone = {meta_value(i, "object_id") for i in instances}

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
    # Former positions on the target plate, in plate order: instance N of an
    # object is its Nth build item.
    item_xforms = {}
    for m in ITEM_RE.finditer(model):
        item_xforms.setdefault(m.group(1), []).append(re.search(r'transform="([^"]*)"', m.group(0)).group(1).split())
    old_spots = [tuple(item_xforms[meta_value(i, "object_id")][int(meta_value(i, "instance_id"))][9:11])
                 for i in instances]
    if len(old_spots) >= len(labels):
        spots, fits = old_spots[:len(labels)], True
    else:
        spots, fits = grid(len(labels), box, bed, origin)
        spots = [tuple(f"{v:.6g}" for v in spot) for spot in spots]

    # Part ids in the config are the component objectids in 3dmodel.model.
    text_ids = [re.search(r'<part id="(\d+)"', m.group(0)).group(1)
                for m in PART_RE.finditer(cfg_objs[src]) if "<slic3rpe:text" in m.group(0)]
    submodels = {}  # new sub-model files: path -> contents
    next_file = max([int(n) for n in re.findall(r'_(\d+)\.model$', "\n".join(zin.namelist()), re.M)] + [0]) + 1
    new_obj = new_item = new_cfg = new_inst = new_asm = ""
    copies = []  # new object ids, in order
    for n, (label, settings) in enumerate(labels):
        oid = str(next_id + n)
        copies.append(oid)
        placed = src_xform[:]
        placed[9:11] = spots[n]
        obj = renumber_uuids(set_attr(model_objs[src], "id", oid), next_ordinal + n)
        for text_id in text_ids:
            path = f"/3D/Objects/{label}_{next_file}.model"
            next_file += 1
            obj, submodels[path] = empty_text_mesh(zin, obj, text_id, path)
        new_obj += obj
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
        rels = REL_RE.sub(lambda m: m.group(0) if m.group(1) in used else "", rels)
        next_rel = max(map(int, re.findall(r'Id="rel-(\d+)"', rels)), default=0) + 1
        new_rels = "".join(
            f' <Relationship Target="{path}" Id="rel-{next_rel + i}" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
            for i, path in enumerate(submodels))
        out[MODEL_RELS] = rels.replace("</Relationships>", new_rels + "</Relationships>", 1)
    out.update({path.lstrip("/"): body for path, body in submodels.items()})

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

    return out, dropped, fits


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file")
    ap.add_argument("count", type=int, help="number of copies to add")
    ap.add_argument("flow", type=float, help="bridge flow ratio")
    ap.add_argument("min_density", type=int, help="first bridge density, integer percent")
    ap.add_argument("max_density", type=int, help="last bridge density, integer percent")
    ap.add_argument("-o", "--output", help="output file (default: modify in place)")
    args = ap.parse_args()
    if args.count < 1:
        ap.error("count must be at least 1")
    span = abs(args.max_density - args.min_density)
    if args.count > 1 and span % (args.count - 1):
        valid = [d + 1 for d in range(1, span + 1) if span % d == 0]
        ap.error(f"count {args.count} gives non-integer densities from {args.min_density} to "
                 f"{args.max_density}; valid counts: 1, " + ", ".join(map(str, valid)))

    flow = fmt(args.flow)
    step = (args.max_density - args.min_density) // max(args.count - 1, 1)
    labels = []
    for n in range(args.count):
        density = str(args.min_density + step * n)
        labels.append((f"{flow}-{density}", {
            "bridge_flow": flow,
            "internal_bridge_flow": flow,
            "bridge_density": f"{density}%",
        }))

    with zipfile.ZipFile(args.file) as zin:
        out, dropped, fits = build(zin, labels, f"Flow Factor {flow}")
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
            for name in out.keys() - set(zin.namelist()):
                zout.writestr(name, out[name].encode("utf-8"))
    shutil.move(tmp, dest)

    print(f"{args.file} -> {args.output}" if args.output else "")
    if not fits:
        print("copies don't fit on the plate; arrange it in the slicer", file=sys.stderr)


if __name__ == "__main__":
    main()
