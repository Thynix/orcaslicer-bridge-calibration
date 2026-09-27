#!/usr/bin/env python3
"""Make a bridge flow/density test plate from an Orca/Bambu 3MF reference project.

Usage: bridge_plate.py REFERENCE.3mf OUT.3mf COUNT FLOW MIN_DENSITY MAX_DENSITY

The reference project must have one plate holding one object. OUT.3mf
(overwritten if it exists) is the reference project with that object replaced
by COUNT copies of it laid out in a grid, with bridge_flow and
internal_bridge_flow set to FLOW and bridge_density stepping from MIN_DENSITY
to MAX_DENSITY (percent, inclusive). Each copy is named "FLOW-DENSITY", e.g.
"1.3-104" for flow 1.3 and density 104%. Its text parts' text and names are
set to "TENTHS-DENSITY", where TENTHS is the first decimal digit of FLOW, e.g.
"3-104"; other part names are kept. FLOW must be 1.0 to 1.9 in steps of 0.1 so
TENTHS identifies it. The plate is named "Flow Factor FLOW".

Each copy's text part gets its own sub-model file holding an empty mesh, which
a patched OrcaSlicer (branch text-rebuild/integration, 14b41380ed) rebuilds from
the text settings on load. Its component transform becomes the text frame, comp *
T(c) * fix^-1 (c: centre of the old mesh's bounding box, fix: the shape's
transform, which is dropped). Stock Orca silently drops such text parts, leaving
the copies unlabelled.
"""
import argparse
import html
import json
import math
import os
import re
import sys
import zipfile

MODEL = "3D/3dmodel.model"
MODEL_RELS = "3D/_rels/3dmodel.model.rels"
CONFIG = "Metadata/model_settings.config"
RANGES = "Metadata/layer_config_ranges.xml"
PROFILES = "Metadata/layer_heights_profile.txt"
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
SHAPE_RE = re.compile(r'<slic3rpe:shape\b[^>]*>')


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
    """Format a setting value like Orca: at most 4 decimals, no trailing zeros."""
    return f"{round(value, 4):g}"


def flow_ratio(text):
    # The text shows only the tenths to fit the tile's text area, so the flow
    # must be one of 1.0-1.9 for them to identify it (within Orca's (0, 2]).
    value = float(text)
    if not (math.isfinite(value) and 1 <= value <= 1.9 and abs(value * 10 - round(value * 10)) < 1e-9):
        raise argparse.ArgumentTypeError(f"{text!r} is not a flow ratio from 1.0 to 1.9 in steps of 0.1 "
                                         "(the label shows only the tenths digit)")
    return round(value, 1)


def density_percent(text):
    # Orca's bridge_density range.
    value = int(text)
    if not 10 <= value <= 125:
        raise argparse.ArgumentTypeError(f"{text!r} is not a density from 10 to 125 (Orca's bridge_density range)")
    return value


def relabel(obj_cfg, oid, name, text, settings):
    """Return a config <object> block with a new id, settings and name, and its
    text parts' text and names set to TEXT."""
    obj_cfg = set_attr(obj_cfg, "id", oid)
    # head is the <object> line and its object-level metadata; rest is the parts
    # and closing tag.
    head_end = obj_cfg.index("    <part ") if "    <part " in obj_cfg else obj_cfg.index("  </object>")
    head, rest = obj_cfg[:head_end], obj_cfg[head_end:]
    # Rebuild the object-level metadata so new keys land where Orca writes
    # them: name first, then keys sorted.
    meta = {m.group(1): meta_value(m.group(0), m.group(1)) for m in OBJ_META_RE.finditer(head)}
    meta.pop("name", None)
    meta.update(settings)
    meta_items = [(k, meta[k]) for k in sorted(meta)]
    head = OBJ_META_RE.sub("", head) + "".join(
        f'    <metadata key="{k}" value="{html.escape(v, quote=True)}"/>\n'
        for k, v in [("name", name)] + meta_items)
    esc = html.escape(text, quote=True)

    def relabel_part(m):
        part = m.group(0)
        if "<slic3rpe:text" not in part:
            return part
        part = re.sub(r'(<metadata key="name" value=")[^"]*', rf"\g<1>{esc}", part, count=1)
        # The fix transform is folded into the component transform by empty_text_mesh.
        part = SHAPE_RE.sub(lambda s: re.sub(r' transform="[^"]*"', "", s.group(0)), part)
        return TEXT_RE.sub(lambda t: f'{t.group(1)}{esc}"', part)

    return head + PART_RE.sub(relabel_part, rest)


def xform(m, point):
    """Apply a 3MF transform (12 floats, row-vector convention) to a point;
    given only the first 9, apply just the rotation."""
    t = m[9:] or (0.0,) * 3
    x, y, z = point
    return tuple(x * m[i] + y * m[3 + i] + z * m[6 + i] + t[i] for i in range(3))


def compose(a, b):
    """The 3MF transform applying B, then A."""
    return [v for r in (0, 3, 6) for v in xform(a[:9], b[r:r + 3])] + list(xform(a, b[9:]))


def invert(m):
    """Inverse of a 3MF transform."""
    (a, b, c), (d, e, f), (g, h, i) = m[0:3], m[3:6], m[6:9]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    inv = [x / det for x in (e * i - f * h, c * h - b * i, b * f - c * e,
                             f * g - d * i, a * i - c * g, c * d - a * f,
                             d * h - e * g, b * g - a * h, a * e - b * d)]
    return inv + [-v for v in xform(inv, m[9:])]


def footprint(submodels, model_obj, rotation, skip):
    """X/Y bounding box of a top-level object in its own frame, rotated as placed,
    leaving out the components with ids in SKIP."""
    xs, ys = [], []
    for comp in re.finditer(r'<component p:path="([^"]+)" objectid="(\d+)"[^>]*transform="([^"]*)"', model_obj):
        path, oid, comp_xform = comp.groups()
        if oid in skip:
            continue
        body = re.search(rf'<object id="{oid}".*?</object>', submodels[path], re.S).group(0)
        comp_m = list(map(float, comp_xform.split()))
        for v in re.finditer(r'<vertex x="([^"]+)" y="([^"]+)" z="([^"]+)"', body):
            x, y, _ = xform(rotation, xform(comp_m, map(float, v.groups())))
            xs.append(x)
            ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def empty_text_mesh(submodels, model_obj, text_id, fix, path):
    """Point MODEL_OBJ's text component at a new sub-model file PATH holding an
    empty mesh, with its transform set to the text frame; return the updated
    object and the new file's contents. SUBMODELS maps the reference's sub-model
    paths to their contents; FIX is the shape's transform or None."""
    # Component objectids are only unique per file, so the text part's must be unambiguous.
    comps = list(re.finditer(rf'<component p:path="([^"]+)" objectid="{text_id}" p:UUID="([0-9a-f]{{8}})[^>]*>',
                             model_obj))
    if len(comps) != 1:
        sys.exit(f"expected 1 component for text part {text_id}, found {len(comps)}")
    comp = comps[0]
    src_path, uuid_prefix = comp.groups()
    src_text = submodels[src_path]
    src_obj = re.search(rf'^  <object id="{text_id}" p:UUID="[0-9a-f]{{8}}([^"]*)".*?</object>', src_text, re.M | re.S)
    uuid_suffix = src_obj.group(1)
    head = src_text[:src_text.index(" <resources>\n") + len(" <resources>\n")]
    tail = src_text[src_text.index(" </resources>\n"):]
    # Sub-model object UUIDs share the prefix of the component referencing them.
    obj = (f'  <object id="{text_id}" p:UUID="{uuid_prefix}{uuid_suffix}" type="model">\n'
           "   <mesh>\n    <vertices>\n    </vertices>\n    <triangles>\n    </triangles>\n   </mesh>\n"
           "  </object>\n")
    new_comp = comp.group(0).replace(f'p:path="{src_path}"', f'p:path="{path}"', 1)
    # Orca centres a loaded text mesh and undoes that with the fix transform; a
    # rebuilt one isn't, so the text frame is comp * T(c) * fix^-1, with c the
    # centre of the mesh's bounding box. A mesh already stripped keeps its frame.
    verts = [tuple(map(float, v)) for v in
             re.findall(r'<vertex x="([^"]+)" y="([^"]+)" z="([^"]+)"', src_obj.group(0))]
    if verts:
        c = [(min(v[i] for v in verts) + max(v[i] for v in verts)) / 2 for i in range(3)]
        frame = [1, 0, 0, 0, 1, 0, 0, 0, 1] + c
        if fix:
            frame = compose(frame, invert(fix))
        comp_xform = list(map(float, re.search(r'transform="([^"]*)"', comp.group(0)).group(1).split()))
        new_comp = set_attr(new_comp, "transform", " ".join(f"{v:.9g}" for v in compose(comp_xform, frame)))
    return model_obj.replace(comp.group(0), new_comp, 1), head + obj + tail


def rename_plate(plate, name):
    """Set a config <plate> block's plater_name, adding it after plater_id if missing."""
    name_line = f'    <metadata key="plater_name" value="{html.escape(name, quote=True)}"/>\n'
    plate, renamed = re.subn(r'^    <metadata key="plater_name" value="[^"]*"/>\n',
                             lambda m: name_line, plate, count=1, flags=re.M)
    if not renamed:
        plate = re.sub(r'(^    <metadata key="plater_id" [^\n]*\n)',
                       lambda m: m.group(1) + name_line, plate, count=1, flags=re.M)
    return plate


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


def build(zin, variants, plate_name):
    model = zin.read(MODEL).decode("utf-8")
    cfg = zin.read(CONFIG).decode("utf-8")
    area = json.loads(zin.read(PROJECT))["printable_area"]
    pts = [tuple(map(float, p.split("x"))) for p in area]
    bed = (min(p[0] for p in pts), min(p[1] for p in pts),
           max(p[0] for p in pts), max(p[1] for p in pts))

    plates = PLATE_RE.findall(cfg)
    if len(plates) != 1:
        sys.exit(f"expected 1 plate, found {len(plates)}")
    ref_plate = plates[0]
    instances = INSTANCE_RE.findall(ref_plate)
    plate_ids = list(dict.fromkeys(meta_value(i, "object_id") for i in instances))
    cfg_objs = {m.group(1): m.group(0) for m in CFG_OBJ_RE.finditer(cfg)}
    if len(plate_ids) != 1:
        found = ", ".join(repr(meta_value(cfg_objs[o], "name")) for o in plate_ids) or "none"
        sys.exit(f"expected exactly 1 object on the plate, found {found}")
    src_id = plate_ids[0]

    model_objs = {m.group(1): m.group(0) for m in MODEL_OBJ_RE.finditer(model)}
    # First build item / assemble item of each object, in build order.
    items, assemble = {}, {}
    for m in ITEM_RE.finditer(model):
        items.setdefault(m.group(1), m.group(0))
    for m in ASSEMBLE_RE.finditer(cfg):
        assemble.setdefault(m.group(1), m.group(0))

    next_id = max(map(int, model_objs)) + 1
    # Also unique per file: the object UUID ordinal prefix (see renumber_uuids)
    # and the config's identify_id.
    next_ordinal = max(int(u, 16) for u in
                       re.findall(r'<object [^>]*p:UUID="([0-9a-f]{8})', model)) + 1
    next_identify = max(int(v) for v in re.findall(r'key="identify_id" value="(\d+)"', cfg)) + 1
    # Part ids in the config are the component objectids in 3dmodel.model;
    # text part id -> the shape's fix transform, if any.
    text_parts = {}
    for m in PART_RE.finditer(cfg_objs[src_id]):
        if "<slic3rpe:text" in m.group(0):
            shape = SHAPE_RE.search(m.group(0))
            fix = shape and re.search(r'\btransform="([^"]*)"', shape.group(0))
            text_parts[re.search(r'<part id="(\d+)"', m.group(0)).group(1)] = (
                list(map(float, fix.group(1).split())) if fix else None)
    # Like Orca's bounding box, the footprint counts only model parts, not
    # modifiers, negative volumes or support blockers/enforcers.
    subtypes = dict(re.findall(r'<part id="(\d+)" subtype="([^"]*)"', cfg_objs[src_id]))
    skip = {i for i, t in subtypes.items() if t != "normal_part"}
    src_xform = re.search(r'transform="([^"]*)"', items[src_id]).group(1).split()
    # The reference's sub-model files, read once for the footprint and text parts.
    src_submodels = {p: zin.read(p.lstrip("/")).decode("utf-8")
                     for p in dict.fromkeys(COMPONENT_PATH_RE.findall(model_objs[src_id]))}
    spots, fits = grid(len(variants), footprint(src_submodels, model_objs[src_id],
                                                list(map(float, src_xform[:9])), skip), bed)
    submodels = {}  # new sub-model files: path -> contents
    # Orca names sub-model files "<name>_<n>.model"; keep <n> unique across the project.
    next_file = max((int(m.group(1)) for n in zin.namelist()
                     if (m := re.search(r'_(\d+)\.model$', n))), default=0) + 1
    new_obj = new_item = new_cfg = new_inst = new_asm = ""
    for n, (name, text, settings) in enumerate(variants):
        oid = str(next_id + n)
        placed = src_xform[:]
        placed[9:11] = (f"{v:.6g}" for v in spots[n])  # mm; not a setting, so not fmt()
        obj = renumber_uuids(set_attr(model_objs[src_id], "id", oid), next_ordinal + n)
        for text_id, fix in text_parts.items():
            path = f"/3D/Objects/{name}_{next_file}.model"
            next_file += 1
            obj, submodels[path] = empty_text_mesh(src_submodels, obj, text_id, fix, path)
        new_obj += obj
        item = set_attr(items[src_id], "objectid", oid)
        item = re.sub(r'(p:UUID=")[0-9a-f]{8}', rf"\g<1>{int(oid):08x}", item, count=1)
        new_item += set_attr(item, "transform", " ".join(placed))
        new_cfg += relabel(cfg_objs[src_id], oid, name, text, settings)
        inst = re.sub(r'(key="object_id" value=")\d+', rf"\g<1>{oid}", instances[0])
        new_inst += re.sub(r'(key="identify_id" value=")\d+', rf"\g<1>{next_identify + n}", inst)
        if src_id in assemble:
            new_asm += set_attr(assemble[src_id], "object_id", oid)

    # 3dmodel.model: replace all objects and items with the copies.
    model = ITEM_RE.sub("", MODEL_OBJ_RE.sub("", model))
    model = model.replace(" </resources>\n", new_obj + " </resources>\n", 1)
    model = model.replace(" </build>\n", new_item + " </build>\n", 1)

    # model_settings.config: likewise for objects, plate instances and assemble items.
    first_obj = CFG_OBJ_RE.search(cfg).start()
    cfg = cfg[:first_obj] + new_cfg + CFG_OBJ_RE.sub("", cfg[first_obj:])
    new_plate = rename_plate(INSTANCE_RE.sub("", ref_plate), plate_name)
    new_plate = new_plate.replace("  </plate>\n", new_inst + "  </plate>\n")
    cfg = cfg.replace(ref_plate, new_plate, 1)
    cfg = ASSEMBLE_RE.sub("", cfg)
    cfg = cfg.replace("  </assemble>\n", new_asm + "  </assemble>\n", 1)

    # Sub-model files no longer referenced by any component.
    used = set(COMPONENT_PATH_RE.findall(model))
    all_paths = {"/" + n for n in zin.namelist() if n.startswith("3D/Objects/")}
    dropped = {p.lstrip("/") for p in all_paths - used}

    out = {MODEL: model, CONFIG: cfg}
    if MODEL_RELS in zin.namelist():
        rels = zin.read(MODEL_RELS).decode("utf-8")
        rels = REL_RE.sub(lambda m: "" if m.group(1).startswith("/3D/Objects/") and m.group(1) not in used
                          else m.group(0), rels)
        next_rel = max(map(int, re.findall(r'Id="rel-(\d+)"', rels)), default=0) + 1
        new_rels = "".join(
            f' <Relationship Target="{path}" Id="rel-{next_rel + i}" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
            for i, path in enumerate(submodels))
        out[MODEL_RELS] = rels.replace("</Relationships>", new_rels + "</Relationships>", 1)
    out.update({path.lstrip("/"): body for path, body in submodels.items()})

    # Height ranges and variable layer height profiles are keyed by 1-based
    # object index in build order; every copy gets the reference's.
    src_idx = str(list(items).index(src_id) + 1)
    if RANGES in zin.namelist():
        ranges = zin.read(RANGES).decode("utf-8")
        block = next((m.group(0) for m in RANGE_OBJ_RE.finditer(ranges) if m.group(1) == src_idx), None)
        body = "".join(set_attr(block, "id", str(i)) for i in range(1, len(variants) + 1)) if block else ""
        first = RANGE_OBJ_RE.search(ranges)
        head = ranges[:first.start()] if first else ranges.replace("</objects>", "")
        out[RANGES] = head + body + "</objects>\n"
    if PROFILES in zin.namelist():
        # One "object_id=N|z0;h0;z1;h1;..." line per object.
        profile = re.search(rf"^object_id={src_idx}\|(.*)$", zin.read(PROFILES).decode("utf-8"), re.M)
        if profile:
            out[PROFILES] = "".join(f"object_id={i}|{profile.group(1)}\n" for i in range(1, len(variants) + 1))
        else:
            dropped.add(PROFILES)

    return out, dropped, fits


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("reference", help="reference project")
    ap.add_argument("output", help="output project, overwritten if it exists")
    ap.add_argument("count", type=int, help="number of copies")
    ap.add_argument("flow", type=flow_ratio, help="bridge flow ratio, 1.0 to 1.9 in steps of 0.1")
    ap.add_argument("min_density", type=density_percent, help="first bridge density, integer percent, 10 to 125")
    ap.add_argument("max_density", type=density_percent, help="last bridge density, integer percent, 10 to 125")
    args = ap.parse_args()
    if os.path.exists(args.output) and os.path.samefile(args.reference, args.output):
        ap.error("output must differ from the reference")
    if args.count < 1:
        ap.error("count must be at least 1")
    span = abs(args.max_density - args.min_density)
    if args.count > 1 and not span:
        ap.error(f"count {args.count} needs different min and max densities, or the copies are identical")
    if args.count == 1 and span:
        ap.error("count 1 needs equal min and max densities, or max is ignored")
    if args.count > 1 and span % (args.count - 1):
        # count - 1 steps must divide the span, so valid counts are d + 1 for divisors d of span.
        valid = [d + 1 for d in range(1, span + 1) if span % d == 0]
        ap.error(f"count {args.count} gives non-integer densities from {args.min_density} to "
                 f"{args.max_density}; valid counts: " + ", ".join(map(str, valid)))

    flow = fmt(args.flow)
    step = (args.max_density - args.min_density) // max(args.count - 1, 1)
    variants = []
    for n in range(args.count):
        density = str(args.min_density + step * n)
        variants.append((f"{flow}-{density}", f"{flow[2:] or 0}-{density}", {
            "bridge_flow": flow,
            "internal_bridge_flow": flow,
            "bridge_density": f"{density}%",
        }))

    tmp = args.output + ".tmp"
    with zipfile.ZipFile(args.reference) as zin:
        out, dropped, fits = build(zin, variants, f"Flow Factor {flow}")
        names = set(zin.namelist())
        # New entries take the reference model's timestamp so output is reproducible.
        date_time = zin.getinfo(MODEL).date_time
        zout = zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED)
        try:
            with zout:
                for info in zin.infolist():
                    if info.filename in dropped:
                        continue
                    data = out[info.filename].encode("utf-8") if info.filename in out else zin.read(info)
                    zout.writestr(info, data, compress_type=info.compress_type)
                for name in out:
                    if name not in names:
                        info = zipfile.ZipInfo(name, date_time)
                        info.external_attr = 0o600 << 16
                        zout.writestr(info, out[name].encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
            os.replace(tmp, args.output)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    print("note: text labels need OrcaSlicer branch text-rebuild/integration; "
          "stock Orca drops them", file=sys.stderr)

    if not fits:
        print("copies don't fit on the plate; arrange it in the slicer", file=sys.stderr)


if __name__ == "__main__":
    main()
