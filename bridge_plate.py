#!/usr/bin/env python3
"""Make a bridge flow/density test plate from an Orca/Bambu 3MF reference project.

Usage: bridge_plate.py REFERENCE.3mf OUT.3mf COUNT FLOW MIN_DENSITY MAX_DENSITY

The reference project must have one plate holding one object. OUT.3mf
(overwritten if it exists) is the reference project with that object replaced
by COUNT copies of it laid out in a grid, with bridge_flow and
internal_bridge_flow set to FLOW and bridge_density stepping from MIN_DENSITY
to MAX_DENSITY (percent, inclusive). Each copy's name, part names and text are
set to "TENTHS-DENSITY", where TENTHS is the first decimal digit of FLOW, e.g.
"3-104" for flow 1.3 and density 104%. The plate is named "Flow Factor FLOW".

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

MODEL = "3D/3dmodel.model"
MODEL_RELS = "3D/_rels/3dmodel.model.rels"
CONFIG = "Metadata/model_settings.config"
RANGES = "Metadata/layer_config_ranges.xml"
PROJECT = "Metadata/project_settings.config"
GAP = 5  # minimum mm between copies

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
OBJ_META_RE = re.compile(r'^    <metadata key="([^"]+)" value="[^"]*"/>\n', re.M)
PART_RE = re.compile(r"^    <part .*?^    </part>\n", re.M | re.S)
TEXT_RE = re.compile(r'(<slic3rpe:text\b[^>]*\btext=")[^"]*"')


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


def footprint(zin, model_obj, rotation, skip):
    """X/Y bounding box of a top-level object in its own frame, rotated as placed,
    leaving out the components with ids in SKIP."""
    xs, ys = [], []
    meshes = {}
    for comp in re.finditer(r'<component p:path="([^"]+)" objectid="(\d+)"[^>]*transform="([^"]*)"', model_obj):
        path, oid, comp_xform = comp.groups()
        if oid in skip:
            continue
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


def grid(count, box, bed):
    """X/Y translations spreading COUNT objects with footprint BOX evenly over
    the bed, and whether they keep GAP apart and inside it."""
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
        spots.append((x0 + gap_x + col * (w + gap_x) - bx0, y1 - (row + 1) * (h + gap_y) - by0))
    return spots, min(gap_x, gap_y) >= GAP


def build(zin, labels, plate_name):
    model = zin.read(MODEL).decode("utf-8")
    cfg = zin.read(CONFIG).decode("utf-8")
    area = json.loads(zin.read(PROJECT))["printable_area"]
    pts = [tuple(map(float, p.split("x"))) for p in area]
    bed = (min(p[0] for p in pts), min(p[1] for p in pts),
           max(p[0] for p in pts), max(p[1] for p in pts))

    plates = PLATE_RE.findall(cfg)
    if len(plates) != 1:
        sys.exit(f"expected 1 plate, found {len(plates)}")
    plate = plates[0]
    instances = INSTANCE_RE.findall(plate)
    plate_ids = list(dict.fromkeys(meta_value(i, "object_id") for i in instances))
    cfg_objs = {m.group(1): m.group(0) for m in CFG_OBJ_RE.finditer(cfg)}
    if len(plate_ids) != 1:
        found = ", ".join(repr(meta_value(cfg_objs[o], "name")) for o in plate_ids) or "none"
        sys.exit(f"expected exactly 1 object on the plate, found {found}")
    src = plate_ids[0]

    model_objs = {m.group(1): m.group(0) for m in MODEL_OBJ_RE.finditer(model)}
    # First build item / assemble item of each object, in build order.
    items, assemble = {}, {}
    for m in ITEM_RE.finditer(model):
        items.setdefault(m.group(1), m.group(0))
    for m in ASSEMBLE_RE.finditer(cfg):
        assemble.setdefault(m.group(1), m.group(0))

    next_id = max(map(int, model_objs)) + 1
    next_ordinal = max(int(u, 16) for u in
                       re.findall(r'<object [^>]*p:UUID="([0-9a-f]{8})', model)) + 1
    next_identify = max(int(v) for v in re.findall(r'key="identify_id" value="(\d+)"', cfg)) + 1

    # Part ids in the config are the component objectids in 3dmodel.model.
    text_ids = [re.search(r'<part id="(\d+)"', m.group(0)).group(1)
                for m in PART_RE.finditer(cfg_objs[src]) if "<slic3rpe:text" in m.group(0)]
    # Like Orca's bounding box, the footprint counts only model parts, not
    # modifiers, negative volumes or support blockers/enforcers.
    subtypes = dict(re.findall(r'<part id="(\d+)" subtype="([^"]*)"', cfg_objs[src]))
    skip = {i for i, t in subtypes.items() if t != "normal_part"}
    src_xform = re.search(r'transform="([^"]*)"', items[src]).group(1).split()
    spots, fits = grid(len(labels), footprint(zin, model_objs[src], src_xform[:9], skip), bed)
    submodels = {}  # new sub-model files: path -> contents
    next_file = max([int(n) for n in re.findall(r'_(\d+)\.model$', "\n".join(zin.namelist()), re.M)] + [0]) + 1
    new_obj = new_item = new_cfg = new_inst = new_asm = ""
    for n, (label, settings) in enumerate(labels):
        oid = str(next_id + n)
        placed = src_xform[:]
        placed[9:11] = (f"{v:.6g}" for v in spots[n])
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
        inst = re.sub(r'(key="object_id" value=")\d+', rf"\g<1>{oid}", instances[0])
        new_inst += re.sub(r'(key="identify_id" value=")\d+', rf"\g<1>{next_identify + n}", inst)
        if src in assemble:
            new_asm += set_attr(assemble[src], "object_id", oid)

    # 3dmodel.model: replace all objects and items with the copies.
    model = ITEM_RE.sub("", MODEL_OBJ_RE.sub("", model))
    model = model.replace(" </resources>\n", new_obj + " </resources>\n", 1)
    model = model.replace(" </build>\n", new_item + " </build>\n", 1)

    # model_settings.config: likewise for objects, plate instances and assemble items.
    first_obj = CFG_OBJ_RE.search(cfg).start()
    cfg = cfg[:first_obj] + new_cfg + CFG_OBJ_RE.sub("", cfg[first_obj:])
    new_plate = INSTANCE_RE.sub("", plate)
    name_line = f'    <metadata key="plater_name" value="{html.escape(plate_name, quote=True)}"/>\n'
    new_plate, renamed = re.subn(r'^    <metadata key="plater_name" value="[^"]*"/>\n',
                                 lambda m: name_line, new_plate, count=1, flags=re.M)
    if not renamed:
        new_plate = re.sub(r'(^    <metadata key="plater_id" [^\n]*\n)',
                           lambda m: m.group(1) + name_line, new_plate, count=1, flags=re.M)
    new_plate = new_plate.replace("  </plate>\n", new_inst + "  </plate>\n")
    cfg = cfg.replace(plate, new_plate, 1)
    cfg = ASSEMBLE_RE.sub("", cfg)
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

    # Height ranges are keyed by 1-based object index in build order; every
    # copy gets the reference's.
    if RANGES in zin.namelist():
        ranges = zin.read(RANGES).decode("utf-8")
        src_idx = str(list(items).index(src) + 1)
        block = next((m.group(0) for m in RANGE_OBJ_RE.finditer(ranges) if m.group(1) == src_idx), None)
        body = "".join(set_attr(block, "id", str(i)) for i in range(1, len(labels) + 1)) if block else ""
        first = RANGE_OBJ_RE.search(ranges)
        head = ranges[:first.start()] if first else ranges.replace("</objects>", "")
        out[RANGES] = head + body + "</objects>\n"

    return out, dropped, fits


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("reference", help="reference project")
    ap.add_argument("output", help="output project, overwritten if it exists")
    ap.add_argument("count", type=int, help="number of copies")
    ap.add_argument("flow", type=float, help="bridge flow ratio")
    ap.add_argument("min_density", type=int, help="first bridge density, integer percent")
    ap.add_argument("max_density", type=int, help="last bridge density, integer percent")
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
        # tenths to save space
        labels.append((f"{int(args.flow*10)%10}-{density}", {
            "bridge_flow": flow,
            "internal_bridge_flow": flow,
            "bridge_density": f"{density}%",
        }))

    tmp = args.output + ".tmp"
    with zipfile.ZipFile(args.reference) as zin:
        out, dropped, fits = build(zin, labels, f"Flow Factor {flow}")
        names = set(zin.namelist())
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                if info.filename in dropped:
                    continue
                data = out[info.filename].encode("utf-8") if info.filename in out else zin.read(info)
                zout.writestr(info, data, compress_type=info.compress_type)
            for name in out:
                if name not in names:
                    zout.writestr(name, out[name].encode("utf-8"))
    shutil.move(tmp, args.output)

    if not fits:
        print("copies don't fit on the plate; arrange it in the slicer", file=sys.stderr)


if __name__ == "__main__":
    main()
