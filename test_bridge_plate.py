#!/usr/bin/env python3
"""Tests for bridge_plate.py. Run: python3 -m unittest test_bridge_plate

Golden hashes live in golden.json; regenerate with GOLDEN_UPDATE=1 after an
intended output change.
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
import random
import sys
import tempfile
import unittest
import unittest.mock
import zipfile

import bridge_plate as bp

HERE = os.path.dirname(os.path.abspath(__file__))
REFERENCE = os.path.join(HERE, "reference", "reference-0.6n.3mf")
GOLDEN = os.path.join(HERE, "golden.json")
IDENTITY = [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]

# name -> main() arguments after the reference and output paths
GOLDEN_CASES = {
    "default": ["1.3", "6", "90", "2"],
    "keep-text-mesh": ["1.3", "6", "90", "2", "--keep-text-mesh"],
    "single": ["1.5", "1", "100", "0"],
}


def run_main(args):
    """Run main() with ARGS; return (exit code or None, stderr)."""
    err = io.StringIO()
    code = None
    with contextlib.redirect_stderr(err), unittest.mock.patch.object(sys, "argv", ["bridge_plate.py", *args]):
        try:
            bp.main()
        except SystemExit as e:
            code = e.code
    return code, err.getvalue()


def rewrite(src, dst, changes):
    """Copy zip SRC to DST; CHANGES maps entry name -> function(text) -> text
    (or None to delete, or a str for a new entry via a function of "")."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        seen = set()
        for info in zin.infolist():
            seen.add(info.filename)
            data = zin.read(info)
            if info.filename in changes:
                fn = changes[info.filename]
                if fn is None:
                    continue
                data = fn(data.decode("utf-8")).encode("utf-8")
            zout.writestr(info, data)
        for name, fn in changes.items():
            if name not in seen and fn is not None:
                zout.writestr(name, fn("").encode("utf-8"))


def entry_hashes(path):
    with zipfile.ZipFile(path) as z:
        return {i.filename: hashlib.sha256(z.read(i)).hexdigest() for i in z.infolist()}


def random_affine(rng):
    """A random invertible 12-float transform (rotation-ish plus translation)."""
    while True:
        m = [rng.uniform(-3, 3) for _ in range(9)] + [rng.uniform(-50, 50) for _ in range(3)]
        (a, b, c), (d, e, f), (g, h, i) = m[0:3], m[3:6], m[6:9]
        if abs(a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)) > 0.1:
            return m


class TransformTests(unittest.TestCase):
    def assertClose(self, a, b, tol=1e-9):
        self.assertEqual(len(a), len(b))
        for x, y in zip(a, b):
            self.assertAlmostEqual(x, y, delta=tol)

    def test_compose_invert_identity(self):
        rng = random.Random(1)
        for _ in range(50):
            m = random_affine(rng)
            self.assertClose(bp.compose(m, bp.invert(m)), IDENTITY, 1e-8)
            self.assertClose(bp.compose(bp.invert(m), m), IDENTITY, 1e-8)

    def test_compose_order(self):
        rng = random.Random(2)
        a, b = random_affine(rng), random_affine(rng)
        p = (1.0, -2.0, 3.0)
        self.assertClose(bp.xform(bp.compose(a, b), p), bp.xform(a, bp.xform(b, p)), 1e-9)

    def test_xform_12_floats(self):
        m = [0, 1, 0, -1, 0, 0, 0, 0, 1, 10, 20, 30]
        self.assertEqual(bp.xform(m, (1, 2, 3)), (-2 + 10, 1 + 20, 3 + 30))

    def test_xform_9_floats_is_rotation_only(self):
        m = [0, 1, 0, -1, 0, 0, 0, 0, 1]
        self.assertEqual(bp.xform(m, (1, 2, 3)), (-2, 1, 3))

    def test_invert_singular_exits(self):
        with self.assertRaises(SystemExit):
            bp.invert([1, 0, 0, 2, 0, 0, 0, 0, 1, 0, 0, 0])


class GridTests(unittest.TestCase):
    BED = (0, 0, 200, 200)

    def test_single_copy_centred(self):
        spots, fits = bp.grid(1, (0, 0, 20, 20), self.BED)
        self.assertTrue(fits)
        self.assertEqual(spots, [(90.0, 90.0)])

    def test_wraps_to_rows(self):
        # max_cols = (200 - 5) // (50 + 5) = 3, so 4 copies -> 2 rows of 2
        spots, fits = bp.grid(4, (0, 0, 50, 10), self.BED)
        self.assertTrue(fits)
        self.assertEqual(len({x for x, _ in spots}), 2)
        self.assertEqual(len({y for _, y in spots}), 2)

    def test_first_row_is_top(self):
        spots, _ = bp.grid(4, (0, 0, 50, 10), self.BED)
        self.assertGreater(spots[0][1], spots[2][1])
        self.assertLess(spots[0][0], spots[1][0])

    def test_box_offset_subtracted(self):
        a, _ = bp.grid(1, (0, 0, 20, 20), self.BED)
        b, _ = bp.grid(1, (5, 7, 25, 27), self.BED)
        self.assertEqual((a[0][0] - 5, a[0][1] - 7), b[0])

    def test_too_big_does_not_fit(self):
        _, fits = bp.grid(1, (0, 0, 199, 20), self.BED)
        self.assertFalse(fits)
        _, fits = bp.grid(30, (0, 0, 40, 40), self.BED)
        self.assertFalse(fits)

    def test_exclude_overlap(self):
        _, fits = bp.grid(1, (0, 0, 20, 20), self.BED, exclude=(95, 95, 105, 105))
        self.assertFalse(fits)

    def test_exclude_clear(self):
        _, fits = bp.grid(1, (0, 0, 20, 20), self.BED, exclude=(0, 0, 10, 10))
        self.assertTrue(fits)


class ParseTests(unittest.TestCase):
    def test_flow_ratio_valid(self):
        self.assertEqual(bp.flow_ratio("1.3"), 1.3)
        self.assertEqual(bp.flow_ratio(" 1.3"), 1.3)
        self.assertEqual(bp.flow_ratio("1"), 1.0)
        self.assertEqual(bp.flow_ratio("1.9"), 1.9)

    def test_flow_ratio_invalid(self):
        for text in ("nan", "inf", "-inf", "abc", "", "0.95", "1.95", "0.9", "2.0", "1.25"):
            with self.subTest(text=text), self.assertRaises(argparse.ArgumentTypeError):
                bp.flow_ratio(text)

    def test_density_percent(self):
        self.assertEqual(bp.density_percent("10"), 10)
        self.assertEqual(bp.density_percent("125"), 125)
        for text in ("9", "126", "abc", "", "50.5"):
            with self.subTest(text=text), self.assertRaises(argparse.ArgumentTypeError):
                bp.density_percent(text)

    def test_fmt(self):
        self.assertEqual(bp.fmt(1.0), "1")
        self.assertEqual(bp.fmt(1.3), "1.3")
        self.assertEqual(bp.fmt(1.30001), "1.3")
        self.assertEqual(bp.fmt(1.12345), "1.1235")


class ReindexLinesTests(unittest.TestCase):
    def test_replicates_source_line(self):
        text = "object_id=2|0;1.5;3;2\n"
        self.assertEqual(bp.reindex_lines(text, "2", 3),
                         "object_id=1|0;1.5;3;2\nobject_id=2|0;1.5;3;2\nobject_id=3|0;1.5;3;2\n")

    def test_keeps_header_lines(self):
        text = "brim_points_format_version=1\nobject_id=1|a\nobject_id=2|b\n"
        self.assertEqual(bp.reindex_lines(text, "2", 2),
                         "brim_points_format_version=1\nobject_id=1|b\nobject_id=2|b\n")

    def test_missing_object_returns_none(self):
        self.assertIsNone(bp.reindex_lines("object_id=1|a\n", "2", 2))
        self.assertIsNone(bp.reindex_lines("header=1\n", "1", 2))


class MetaTests(unittest.TestCase):
    def test_set_and_get_meta(self):
        block = '<metadata key="a" value="x"/>\n<metadata key="b" value="y"/>\n'
        self.assertEqual(bp.meta_value(block, "b"), "y")
        self.assertIsNone(bp.meta_value(block, "c"))
        self.assertEqual(bp.meta_value(bp.set_meta(block, "a", "z"), "a"), "z")

    def test_renumber_uuids(self):
        obj = ('<object id="4" p:UUID="00000001-aaaa" type="model">'
               '<component p:UUID="00010000-bbbb"/><component p:UUID="00010001-cccc"/>')
        out = bp.renumber_uuids(obj, 0x2a)
        self.assertIn('p:UUID="0000002a-aaaa"', out)
        self.assertIn('p:UUID="002a0000-bbbb"', out)
        self.assertIn('p:UUID="002a0001-cccc"', out)


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def make(self, name, args):
        out = self.path(name)
        code, err = run_main([REFERENCE, out, *args])
        return code, err, out

    def test_golden(self):
        result = {}
        for name, args in GOLDEN_CASES.items():
            code, _, out = self.make(name + ".3mf", args)
            self.assertIsNone(code, name)
            result[name] = entry_hashes(out)
        if os.environ.get("GOLDEN_UPDATE"):
            with open(GOLDEN, "w") as f:
                json.dump(result, f, indent=1, sort_keys=True)
                f.write("\n")
        with open(GOLDEN) as f:
            golden = json.load(f)
        for name in GOLDEN_CASES:
            with self.subTest(case=name):
                self.assertEqual(result[name], golden[name])

    def test_output_reproducible(self):
        _, _, a = self.make("a.3mf", GOLDEN_CASES["default"])
        _, _, b = self.make("b.3mf", GOLDEN_CASES["default"])
        with open(a, "rb") as fa, open(b, "rb") as fb:
            self.assertEqual(fa.read(), fb.read())

    def test_copies_named_and_labelled(self):
        _, _, out = self.make("o.3mf", GOLDEN_CASES["default"])
        with zipfile.ZipFile(out) as z:
            cfg = z.read(bp.CONFIG).decode()
        for density in (90, 92, 94, 96, 98, 100):
            self.assertIn(f'value="1.3-{density}"', cfg)
            self.assertIn(f'text="3-{density}"', cfg)
        self.assertIn('key="plater_name" value="Flow Factor 1.3"', cfg)

    def test_invalid_counts(self):
        for args, fragment in (
                (["1.3", "0", "90", "2"], "at least 1"),
                (["1.3", "2", "90", "0"], "non-zero density_step"),
                (["1.3", "3", "120", "10"], "outside Orca's bridge_density range"),
        ):
            with self.subTest(args=args):
                code, err, _ = self.make("x.3mf", args)
                self.assertEqual(code, 2)
                self.assertIn(fragment, err)

    def test_output_same_as_reference_rejected(self):
        code, err = run_main([REFERENCE, REFERENCE, "1.3", "6", "90", "2"])
        self.assertEqual(code, 2)
        self.assertIn("must differ", err)


class ErrorPathTests(unittest.TestCase):
    """Crafted variants of the reference that build() must reject."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def expect_error(self, changes, fragment):
        variant = os.path.join(self.tmp.name, "variant.3mf")
        rewrite(REFERENCE, variant, changes)
        code, err = run_main([variant, os.path.join(self.tmp.name, "out.3mf"), "1.3", "6", "90", "2"])
        self.assertIsInstance(code, str, err)
        self.assertIn(fragment, code)

    def test_two_plates(self):
        def dup(cfg):
            plate = bp.PLATE_RE.search(cfg).group(0)
            return cfg.replace("  <assemble>", plate + "  <assemble>", 1)
        self.expect_error({bp.CONFIG: dup}, "expected 1 plate, found 2")

    def test_two_instances(self):
        def dup(cfg):
            inst = bp.INSTANCE_RE.search(cfg).group(0)
            return cfg.replace("  </plate>", inst + "  </plate>", 1)
        self.expect_error({bp.CONFIG: dup}, "exactly 1 instance")

    def test_missing_instance_id(self):
        def strip(cfg):
            return cfg.replace('      <metadata key="instance_id" value="0"/>\n', "", 1)
        self.expect_error({bp.CONFIG: strip}, "model_instance has no instance_id")

    def test_extra_object(self):
        def add(model):
            extra = '  <object id="5" p:UUID="00000002-aaaa" type="model">\n  </object>\n'
            return model.replace(" </resources>", extra + " </resources>", 1)
        self.expect_error({bp.MODEL: add}, "extra")

    def test_cut_object(self):
        cut = ('<?xml version="1.0" encoding="utf-8"?>\n<objects>\n <object id="1">\n'
               '  <cut_id id="7" check_sum="1" connectors_cnt="0"/>\n </object>\n</objects>\n')
        self.expect_error({bp.CUT_INFO: lambda _: cut}, "part of a cut")

    def test_not_a_project(self):
        self.expect_error({bp.CONFIG: None}, "not an Orca/Bambu project")


if __name__ == "__main__":
    unittest.main()
