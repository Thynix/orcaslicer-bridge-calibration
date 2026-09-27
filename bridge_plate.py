#!/usr/bin/env python3
"""Make a bridge flow/density test plate from an OrcaSlicer 3MF reference
project.

Usage: bridge_plate.py REFERENCE.3mf OUT.3mf COUNT FLOW MIN_DENSITY MAX_DENSITY

The reference project must have one plate holding one object. OUT.3mf
(overwritten if it exists) is the reference project with that object replaced
by COUNT copies of it laid out in a grid, with bridge_flow and
internal_bridge_flow set to FLOW and bridge_density stepping from MIN_DENSITY
to MAX_DENSITY (percent, inclusive). Each copy is named "FLOW-DENSITY", e.g.
"1.3-104" for flow 1.3 and density 104% (FLOW is written as Orca writes the
setting, so 1.0 is "1", not "1.0"). Its text parts' text and names are set to
"TENTHS-DENSITY", where TENTHS is the first decimal digit of FLOW, e.g.
"3-104"; other part names are kept. FLOW must be 1.0 to 1.9 in steps of 0.1 so
TENTHS identifies it. The plate is named "Flow Factor FLOW".

Each copy's text part gets its own sub-model file holding an empty mesh, which
a patched OrcaSlicer rebuilds from the text settings on load. Its component
transform becomes the text frame, comp * T(c) * fix^-1 (c: centre of the old
mesh's bounding box, fix: the shape's transform, which is dropped). Stock
OrcaSlicer silently drops such text parts, leaving the copies unlabelled.
With --keep-text-mesh, each copy's text part instead keeps sharing the
reference's original (unmodified) mesh, so stock OrcaSlicer doesn't drop it;
the part still shows the reference's original text until edited by hand.
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
CUT_INFO = "Metadata/cut_information.xml"
PROFILES = "Metadata/layer_heights_profile.txt"
BRIM_EARS = "Metadata/brim_ear_points.txt"
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
ASSEMBLE_RE = re.compile(r'^   <assemble_item [^>]*/>\n', re.M)
# layer_config_ranges.xml and cut_information.xml are both boost::ptree XML
# with the same beautified indentation.
XML_OBJ_RE = re.compile(r'^ <object id="(\d+)">.*?^ </object>\n', re.M | re.S)
REL_RE = re.compile(r'^ <Relationship Target="([^"]+)"[^>]*/>\n', re.M)
OBJ_META_RE = re.compile(r'^    <metadata key="([^"]+)" value="[^"]*"/>\n', re.M)
PART_RE = re.compile(r"^    <part .*?^    </part>\n", re.M | re.S)
TEXT_RE = re.compile(r'(<slic3rpe:text\b[^>]*\btext=")[^"]*"')
SHAPE_RE = re.compile(r'<slic3rpe:shape\b[^>]*>')
VERTEX_RE = re.compile(r'<vertex x="([^"]+)" y="([^"]+)" z="([^"]+)"')


def meta_value(block, key):
    m = re.search(rf'<metadata key="{key}" value="([^"]*)"', block)
    return html.unescape(m.group(1)) if m else None


def set_meta(block, key, value):
    """Set metadata KEY's value in BLOCK to VALUE, the write-side partner of meta_value."""
    return re.sub(rf'(<metadata key="{key}" value=")[^"]*', lambda m: m.group(1) + value, block, count=1)


def set_attr(text, attr, value):
    return re.sub(rf'\b{attr}="[^"]*"', f'{attr}="{value}"', text, count=1)


def attr(text, name):
    """Read attribute NAME from TEXT, the read-side partner of set_attr."""
    return re.search(rf'\b{name}="([^"]*)"', text).group(1)


def floats(text):
    return list(map(float, text.split()))


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
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not (math.isfinite(value) and 1 <= value <= 1.9 and abs(value * 10 - round(value * 10)) < 1e-9):
        raise argparse.ArgumentTypeError(f"{text!r} is not a flow ratio from 1.0 to 1.9 in steps of 0.1 "
                                         "(the label shows only the tenths digit)")
    return round(value, 1)


def density_percent(text):
    # Orca's bridge_density range.
    try:
        value = int(text)
    except ValueError:
        value = -1
    if not 10 <= value <= 125:
        raise argparse.ArgumentTypeError(f"{text!r} is not a density from 10 to 125 (Orca's bridge_density range)")
    return value


def relabel(obj_cfg, oid, name, text, settings, keep_text_mesh=False):
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
        f'    <metadata key="{k}" value="{html.escape(v)}"/>\n'
        for k, v in [("name", name)] + meta_items)
    esc = html.escape(text)

    def relabel_part(m):
        part = m.group(0)
        if "<slic3rpe:text" not in part:
            return part
        part = set_meta(part, "name", esc)
        # The fix transform is folded into the component transform by empty_text_mesh,
        # but only when empty_text_mesh runs (not with --keep-text-mesh).
        if not keep_text_mesh:
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
    if not det:
        sys.exit("reference text part's shape transform is singular")
    inv = [x / det for x in (e * i - f * h, c * h - b * i, b * f - c * e,
                             f * g - d * i, a * i - c * g, c * d - a * f,
                             d * h - e * g, b * g - a * h, a * e - b * d)]
    return inv + [-v for v in xform(inv, m[9:])]


def footprint(submodels, model_obj, rotation, skip):
    """X/Y bounding box of a top-level object in its own frame, rotated as placed,
    leaving out the components with ids in SKIP."""
    xs, ys = [], []
    for comp in re.finditer(r'<component[^>]*/>', model_obj):
        comp = comp.group(0)
        oid = attr(comp, "objectid")
        if oid in skip:
            continue
        body = re.search(rf'<object id="{oid}".*?</object>', submodels[attr(comp, "p:path")], re.S).group(0)
        comp_m = floats(attr(comp, "transform"))
        for v in VERTEX_RE.finditer(body):
            x, y, _ = xform(rotation, xform(comp_m, map(float, v.groups())))
            xs.append(x)
            ys.append(y)
    if not xs:
        sys.exit("reference object has no model-part components")
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
    if src_obj is None:
        sys.exit(f"text part {text_id}'s sub-model object doesn't match the expected p:UUID format")
    uuid_suffix = src_obj.group(1)
    head = src_text[:src_text.index(" <resources>\n") + len(" <resources>\n")]
    tail = src_text[src_text.index(" </resources>\n"):]
    # Sub-model object UUIDs share the prefix of the component referencing them.
    obj = (f'  <object id="{text_id}" p:UUID="{uuid_prefix}{uuid_suffix}" type="model">\n'
           "   <mesh>\n    <vertices>\n    </vertices>\n    <triangles>\n    </triangles>\n   </mesh>\n"
           "  </object>\n")
    new_comp = set_attr(comp.group(0), "p:path", path)
    # Orca centres a loaded text mesh and undoes that with the fix transform; a
    # rebuilt one isn't, so the text frame is comp * T(c) * fix^-1, with c the
    # centre of the mesh's bounding box. A mesh already stripped keeps its frame.
    verts = [tuple(map(float, v)) for v in VERTEX_RE.findall(src_obj.group(0))]
    if verts:
        c = [(min(v[i] for v in verts) + max(v[i] for v in verts)) / 2 for i in range(3)]
        frame = [1, 0, 0, 0, 1, 0, 0, 0, 1] + c
        if fix:
            frame = compose(frame, invert(fix))
        comp_xform = floats(attr(comp.group(0), "transform"))
        new_comp = set_attr(new_comp, "transform",
                            " ".join(f"{v:.9g}" for v in compose(comp_xform, frame)))  # Orca's transform precision
    return model_obj.replace(comp.group(0), new_comp, 1), head + obj + tail


def rename_plate(plate, name):
    """Set a config <plate> block's plater_name, adding it after plater_id if missing."""
    name_line = f'    <metadata key="plater_name" value="{html.escape(name)}"/>\n'
    plate, renamed = re.subn(r'^    <metadata key="plater_name" value="[^"]*"/>\n',
                             lambda m: name_line, plate, count=1, flags=re.M)
    if not renamed:
        plate = re.sub(r'(^    <metadata key="plater_id" [^\n]*\n)',
                       lambda m: m.group(1) + name_line, plate, count=1, flags=re.M)
    return plate


def reindex_lines(text, src_idx, count):
    """TEXT is an "object_id=N|..." file (layer_heights_profile.txt or
    brim_ear_points.txt) with one line per 1-based object index in build
    order, plus optional non-"object_id=" header lines (e.g.
    brim_points_format_version=). Return it with the reference object's own
    line, if any, replicated to ids 1..COUNT; or None if it has none."""
    line = re.search(rf"^object_id={src_idx}\|(.*)$", text, re.M)
    if not line:
        return None
    header = "".join(l + "\n" for l in text.splitlines() if not l.startswith("object_id="))
    return header + "".join(f"object_id={i}|{line.group(1)}\n" for i in range(1, count + 1))


def find_xml_obj(text, src_idx):
    """The <object id="SRC_IDX">...</object> block in a boost::ptree XML file
    (layer_config_ranges.xml or cut_information.xml), or None."""
    return next((m.group(0) for m in XML_OBJ_RE.finditer(text) if m.group(1) == src_idx), None)


def bbox(points):
    return (min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points))


def grid(count, box, bed, exclude=None):
    """X/Y translations spreading COUNT objects with footprint BOX evenly over
    the bed, and whether they keep at least GAP from the bed edges and each
    other and clear of EXCLUDE (a bed_exclude_area bounding box, or None)."""
    bx0, by0, bx1, by1 = box
    w, h = bx1 - bx0, by1 - by0
    x0, y0, x1, y1 = bed
    max_cols = max(1, int((x1 - x0 - GAP) // (w + GAP)))
    rows = math.ceil(count / max_cols)
    cols = math.ceil(count / rows)
    gap_x = (x1 - x0 - cols * w) / (cols + 1)
    gap_y = (y1 - y0 - rows * h) / (rows + 1)
    fits = min(gap_x, gap_y) >= GAP
    spots = []
    for n in range(count):
        row, col = divmod(n, cols)
        cx0, cy0 = x0 + gap_x + col * (w + gap_x), y1 - (row + 1) * (h + gap_y)
        if exclude is not None and cx0 < exclude[2] and cx0 + w > exclude[0] and cy0 < exclude[3] and cy0 + h > exclude[1]:
            fits = False
        spots.append((cx0 - bx0, cy0 - by0))
    return spots, fits


def build(zin, variants, plate_name, keep_text_mesh=False):
    names = set(zin.namelist())
    model = zin.read(MODEL).decode("utf-8")
    if CONFIG not in names:
        sys.exit("not an Orca/Bambu project: no Metadata/model_settings.config")
    cfg = zin.read(CONFIG).decode("utf-8")
    proj = json.loads(zin.read(PROJECT))
    bed = bbox([tuple(map(float, p.split("x"))) for p in proj["printable_area"]])
    # Same "XxY" format as printable_area; missing, empty or a degenerate
    # (zero-area) box, e.g. ["0x0"], excludes nothing.
    excl_pts = [tuple(map(float, p.split("x"))) for p in proj.get("bed_exclude_area") or []]
    exclude = bbox(excl_pts) if excl_pts else None
    if exclude is not None and (exclude[2] <= exclude[0] or exclude[3] <= exclude[1]):
        exclude = None

    plates = PLATE_RE.findall(cfg)
    if len(plates) != 1:
        sys.exit(f"expected 1 plate, found {len(plates)}")
    ref_plate = plates[0]
    instances = INSTANCE_RE.findall(ref_plate)
    if len(instances) != 1:
        sys.exit(f"expected exactly 1 instance of the plate's object, found {len(instances)}")
    plate_ids = list(dict.fromkeys(meta_value(i, "object_id") for i in instances))
    cfg_objs = {m.group(1): m.group(0) for m in CFG_OBJ_RE.finditer(cfg)}
    if len(plate_ids) != 1:
        found = ", ".join(repr(meta_value(cfg_objs.get(o, ""), "name") or f"id {o}") for o in plate_ids) or "none"
        sys.exit(f"expected exactly 1 object on the plate, found {found}")
    src_id = plate_ids[0]

    model_objs = {m.group(1): m.group(0) for m in MODEL_OBJ_RE.finditer(model)}
    extra = set(model_objs) - {src_id}
    if extra:
        extra = ", ".join(repr(meta_value(cfg_objs.get(o, ""), "name") or f"id {o}")
                          for o in sorted(extra, key=int))
        sys.exit(f"expected only the plate's object in the model, found extra (off-plate?): {extra}")

    # Build items of each object, in build order (index = instance_id; src_idx
    # below relies on the order); assemble items keyed by (object_id, instance_id).
    items, assemble = {}, {}
    for m in ITEM_RE.finditer(model):
        items.setdefault(m.group(1), []).append(m.group(0))
    for m in ASSEMBLE_RE.finditer(cfg):
        item = m.group(0)
        if "instance_id" not in item:
            sys.exit("assemble_item has no instance_id; re-save the reference in a recent OrcaSlicer")
        assemble[(attr(item, "object_id"), attr(item, "instance_id"))] = item

    next_id = max(map(int, model_objs)) + 1
    # Also unique per file: the object UUID ordinal prefix (see renumber_uuids)
    # and the config's identify_id.
    next_ordinal = max(int(u, 16) for u in
                       re.findall(r'<object [^>]*p:UUID="([0-9a-f]{8})', model)) + 1
    next_identify = max(int(v) for v in re.findall(r'key="identify_id" value="(\d+)"', cfg)) + 1
    if "<text_info" in cfg_objs[src_id]:
        sys.exit("re-save the reference in OrcaSlicer to convert its text")
    # Part ids in the config are the component objectids in 3dmodel.model;
    # text part id -> the shape's fix transform, if any.
    text_parts = {}
    for m in PART_RE.finditer(cfg_objs[src_id]):
        if "<slic3rpe:text" in m.group(0):
            part_id = attr(m.group(0), "id")
            shape = SHAPE_RE.search(m.group(0))
            if not shape:
                sys.exit(f"text part {part_id} has no <slic3rpe:shape>, so Orca can't rebuild it")
            fix = re.search(r'\btransform="([^"]*)"', shape.group(0))
            text_parts[part_id] = floats(fix.group(1)) if fix else None
    # Like Orca's bounding box, the footprint counts only model parts, not
    # modifiers, negative volumes or support blockers/enforcers.
    subtypes = dict(re.findall(r'<part id="(\d+)" subtype="([^"]*)"', cfg_objs[src_id]))
    skip = {i for i, t in subtypes.items() if t != "normal_part"}
    # The build item and assemble item for the instance actually on the plate,
    # which need not be instance 0 (e.g. instance 0 sits on another plate).
    k = int(meta_value(instances[0], "instance_id"))
    if k >= len(items.get(src_id, [])):
        sys.exit(f"plate instance has instance_id {k}, but object {src_id} has no matching build item")
    src_item = items[src_id][k]
    src_xform = attr(src_item, "transform").split()
    # The reference's sub-model files, read once for the footprint and text parts.
    # p:path is XML-escaped, but the zip entry name is raw.
    src_submodels = {p: zin.read(html.unescape(p).lstrip("/")).decode("utf-8")
                     for p in dict.fromkeys(COMPONENT_PATH_RE.findall(model_objs[src_id]))}
    spots, fits = grid(len(variants), footprint(src_submodels, model_objs[src_id],
                                                [float(v) for v in src_xform[:9]], skip), bed, exclude)
    submodels = {}  # new sub-model files: path -> contents
    # Orca names sub-model files "<name>_<n>.model"; keep <n> unique across the project.
    next_file = max((int(m.group(1)) for n in names
                     if (m := re.search(r'_(\d+)\.model$', n))), default=0) + 1
    new_obj = new_item = new_cfg = new_inst = new_asm = ""
    for n, (name, text, settings) in enumerate(variants):
        oid = str(next_id + n)
        placed = src_xform[:]
        placed[9:11] = (f"{v:.6g}" for v in spots[n])  # mm; not a setting, so not fmt()
        obj = renumber_uuids(set_attr(model_objs[src_id], "id", oid), next_ordinal + n)
        if not keep_text_mesh:
            for text_id, fix in text_parts.items():
                path = f"/3D/Objects/{name}_{next_file}.model"
                next_file += 1
                obj, submodels[path] = empty_text_mesh(src_submodels, obj, text_id, fix, path)
        new_obj += obj
        item = set_attr(src_item, "objectid", oid)
        item = re.sub(r'(p:UUID=")[0-9a-f]{8}', rf"\g<1>{int(oid):08x}", item, count=1)
        new_item += set_attr(item, "transform", " ".join(placed))
        new_cfg += relabel(cfg_objs[src_id], oid, name, text, settings, keep_text_mesh)
        # Each copy is a single instance, so it's instance 0.
        inst = set_meta(instances[0], "object_id", oid)
        inst = set_meta(inst, "instance_id", "0")
        new_inst += set_meta(inst, "identify_id", str(next_identify + n))
        if (src_id, str(k)) in assemble:
            asm = set_attr(assemble[(src_id, str(k))], "object_id", oid)
            new_asm += set_attr(asm, "instance_id", "0")

    # 3dmodel.model: replace all objects and items with the copies.
    model = ITEM_RE.sub("", MODEL_OBJ_RE.sub("", model))
    model = model.replace(" </resources>\n", new_obj + " </resources>\n", 1)
    model = model.replace(" </build>\n", new_item + " </build>\n", 1)

    # model_settings.config: likewise for objects, plate instances and assemble items.
    first_obj = CFG_OBJ_RE.search(cfg).start()
    cfg = cfg[:first_obj] + new_cfg + CFG_OBJ_RE.sub("", cfg[first_obj:])
    new_plate = rename_plate(INSTANCE_RE.sub("", ref_plate), plate_name)
    new_plate = new_plate.replace("  </plate>\n", new_inst + "  </plate>\n", 1)
    cfg = cfg.replace(ref_plate, new_plate, 1)
    cfg = ASSEMBLE_RE.sub("", cfg)
    cfg = cfg.replace("  </assemble>\n", new_asm + "  </assemble>\n", 1)

    # Sub-model files no longer referenced by any component. p:path is
    # XML-escaped; unescape it to compare against the raw zip entry names.
    used_esc = set(COMPONENT_PATH_RE.findall(model))
    used = {html.unescape(p) for p in used_esc}
    all_paths = {"/" + n for n in names if n.startswith("3D/Objects/")}
    dropped = {p.lstrip("/") for p in all_paths - used}

    # A kept reference file's original text mesh is itself no longer
    # referenced (each copy points at its own empty-mesh file instead);
    # prune that dead object so Orca doesn't parse and discard it every time.
    # p:path here stays XML-escaped, matching src_submodels' (escaped) keys.
    referenced = set(re.findall(r'p:path="([^"]+)" objectid="(\d+)"', model))
    pruned = {}
    for path in used_esc & src_submodels.keys():
        body = MODEL_OBJ_RE.sub(lambda m: m.group(0) if (path, m.group(1)) in referenced else "",
                                src_submodels[path])
        if body != src_submodels[path]:
            pruned[path.lstrip("/")] = body

    out = {MODEL: model, CONFIG: cfg}
    if MODEL_RELS in names:
        rels = zin.read(MODEL_RELS).decode("utf-8")
        rels = REL_RE.sub(lambda m: "" if m.group(1).startswith("/3D/Objects/")
                          and html.unescape(m.group(1)) not in used else m.group(0), rels)
        next_rel = max(map(int, re.findall(r'Id="rel-(\d+)"', rels)), default=0) + 1
        new_rels = "".join(
            f' <Relationship Target="{path}" Id="rel-{next_rel + i}" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
            for i, path in enumerate(submodels))
        out[MODEL_RELS] = rels.replace("</Relationships>", new_rels + "</Relationships>", 1)
    out.update({path.lstrip("/"): body for path, body in submodels.items()})
    out.update(pruned)

    # Height ranges, cut information, variable layer height profiles and brim
    # ear points are all keyed by 1-based object index in build order; every
    # copy gets the reference's, or the file is dropped if it has none.
    src_idx = str(list(items).index(src_id) + 1)
    if RANGES in names:
        ranges = zin.read(RANGES).decode("utf-8")
        block = find_xml_obj(ranges, src_idx)
        if block:
            body = "".join(set_attr(block, "id", str(i)) for i in range(1, len(variants) + 1))
            out[RANGES] = ranges[:XML_OBJ_RE.search(ranges).start()] + body + "</objects>\n"
        else:
            dropped.add(RANGES)
    if CUT_INFO in names:
        cut = zin.read(CUT_INFO).decode("utf-8")
        block = find_xml_obj(cut, src_idx)
        cut_id = block and re.search(r'<cut_id id="(\d+)"', block)
        if cut_id and cut_id.group(1) != "0":
            # The copies would all share the reference's cut id, linking them
            # as parts of one cut, which isn't a sensible reference.
            sys.exit("reference object is part of a cut; not a sensible reference")
        dropped.add(CUT_INFO)
    for path in (PROFILES, BRIM_EARS):
        if path in names:
            lines = reindex_lines(zin.read(path).decode("utf-8"), src_idx, len(variants))
            if lines is None:
                dropped.add(path)
            else:
                out[path] = lines

    return out, dropped, fits, bool(text_parts), names


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("reference", help="reference project")
    ap.add_argument("output", help="output project, overwritten if it exists")
    ap.add_argument("count", type=int, help="number of copies; COUNT - 1 must divide MAX_DENSITY - MIN_DENSITY")
    ap.add_argument("flow", type=flow_ratio, help="bridge flow ratio, 1.0 to 1.9 in steps of 0.1")
    ap.add_argument("min_density", type=density_percent, help="first bridge density, integer percent, 10 to 125")
    ap.add_argument("max_density", type=density_percent, help="last bridge density, integer percent, 10 to 125")
    ap.add_argument("--keep-text-mesh", action="store_true",
                     help="keep each copy's text part on the reference's original, unmodified mesh "
                          "(so stock OrcaSlicer doesn't drop it), instead of an empty mesh a patched "
                          "OrcaSlicer rebuilds per-copy; the text will read the reference's original "
                          "label until edited by hand")
    args = ap.parse_args()
    if os.path.exists(args.output) and os.path.samefile(args.reference, args.output):
        ap.error("output must differ from the reference")
    if args.count < 1:
        ap.error("count must be at least 1")
    span = abs(args.max_density - args.min_density)
    if args.count > 1 and not span:
        ap.error(f"count {args.count} needs different min and max densities; "
                 "otherwise the copies would be identical")
    if args.count == 1 and span:
        ap.error("count 1 needs equal min and max densities")
    if args.count > 1 and span % (args.count - 1):
        # count - 1 steps must divide the span, so valid counts are d + 1 for divisors d of span.
        valid = [d + 1 for d in range(1, span + 1) if span % d == 0]
        ap.error(f"count {args.count} gives non-integer densities from {args.min_density} to "
                 f"{args.max_density}; valid counts: " + ", ".join(map(str, valid)))

    flow = fmt(args.flow)
    tenths = round(args.flow * 10) % 10
    step = (args.max_density - args.min_density) // max(args.count - 1, 1)
    variants = []
    for n in range(args.count):
        density = str(args.min_density + step * n)
        variants.append((f"{flow}-{density}", f"{tenths}-{density}", {
            "bridge_flow": flow,
            "internal_bridge_flow": flow,
            "bridge_density": f"{density}%",
        }))

    tmp = args.output + ".tmp"
    with zipfile.ZipFile(args.reference) as zin:
        out, dropped, fits, has_text, names = build(zin, variants, f"Flow Factor {flow}", args.keep_text_mesh)
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
                        zout.writestr(info, out[name].encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
            os.replace(tmp, args.output)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    if has_text:
        if args.keep_text_mesh:
            print("note: --keep-text-mesh kept each copy's text part on the reference's original mesh; "
                  "it reads the reference's original label until edited by hand in OrcaSlicer.", file=sys.stderr)
        else:
            print("note: Automatically regenerating text labels on load needs Thynix's OrcaSlicer branch rebuild-text-with-missing-mesh; "
                  "stock OrcaSlicer drops them. See https://github.com/Thynix/OrcaSlicer/tree/rebuild-text-with-missing-mesh", file=sys.stderr)

    if not fits:  # also true if a copy overlaps bed_exclude_area
        print("warning: copies don't fit on the plate; arrange it in the slicer", file=sys.stderr)


if __name__ == "__main__":
    main()
