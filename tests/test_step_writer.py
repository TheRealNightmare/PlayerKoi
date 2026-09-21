"""Tests for the hand-rolled STEP writer.

There is no CAD kernel on this machine to open the output and confirm it is a
valid solid, so these stand in for one. They check the invariants that a
kernel would check when it sews the shell:

  * every #n referenced actually exists
  * every edge is used exactly twice, once in each direction -- the property
    that makes the shell closed and orientable
  * the entity counts are exactly what the topology predicts, so a face or an
    edge quietly going missing is caught rather than showing up as a hole in
    the solid after import

That is not the same as proving the normals point outward; nothing here can.
But it does mean a file that fails is definitely broken, and a file that
passes is broken only in ways that need a kernel to see.
"""

import collections
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import step_writer  # noqa: E402


def read(path):
    with open(path) as fh:
        return fh.read()


def parse(path):
    data = read(path).split("DATA;")[1].split("ENDSEC;")[0]
    return dict(re.findall(r"#(\d+)=(.*?);\n", data, re.S))


def kinds(entities):
    return collections.Counter(
        re.match(r"\(?([A-Z_0-9]+)", body).group(1) for body in entities.values())


def loop_discontinuities(entities):
    """Every edge loop walked end to end, reporting any break in the chain.

    This is the check that actually exercises the orientation flags. An edge
    loop is a list of ORIENTED_EDGEs, and each one's end vertex has to be the
    next one's start vertex -- so a single .T./.F. written the wrong way round
    shows up here as a jump, even though the file still parses and the
    edge-usage count still balances.
    """
    points = {n: tuple(float(v) for v in
                       re.match(r"CARTESIAN_POINT\('',\((.*)\)\)", b).group(1).split(","))
              for n, b in entities.items() if b.startswith("CARTESIAN_POINT")}
    vertices = {n: points[re.match(r"VERTEX_POINT\('',#(\d+)\)", b).group(1)]
                for n, b in entities.items() if b.startswith("VERTEX_POINT")}
    edges, oriented = {}, {}
    for n, b in entities.items():
        m = re.match(r"EDGE_CURVE\('',#(\d+),#(\d+),#(\d+),\.T\.\)", b)
        if m:
            edges[n] = (m.group(1), m.group(2))
        m = re.match(r"ORIENTED_EDGE\('',\*,\*,#(\d+),\.([TF])\.\)", b)
        if m:
            oriented[n] = (m.group(1), m.group(2) == "T")

    problems = []
    for n, b in entities.items():
        m = re.match(r"EDGE_LOOP\('',\((.*)\)\)", b)
        if not m:
            continue
        chain = []
        for ref in m.group(1).split(","):
            edge, forward = oriented[ref[1:]]
            v0, v1 = edges[edge]
            chain.append((v0, v1) if forward else (v1, v0))
        for i, (_, end) in enumerate(chain):
            start = chain[(i + 1) % len(chain)][0]
            if max(abs(a - b_) for a, b_ in zip(vertices[end], vertices[start])) > 1e-9:
                problems.append(f"loop #{n} breaks after edge {i}")
    return problems


def a_plate(holes=(), thickness=5.0, w=100.0, h=80.0, r=5.0):
    return step_writer.Plate(
        step_writer.rounded_rect(0, 0, w, h, r), list(holes), thickness)


class TestProfile(unittest.TestCase):
    def test_rounded_rect_is_a_closed_chain(self):
        """Each segment must start exactly where the last one ended, or the
        solid has a crack down its side that no importer will sew."""
        segs = step_writer.rounded_rect(0, 0, 100, 80, 5)
        self.assertEqual(len(segs), 8)
        for i, seg in enumerate(segs):
            prev_end = step_writer.seg_end(segs[i - 1])
            start = step_writer.seg_start(seg)
            self.assertAlmostEqual(prev_end[0], start[0], places=9, msg=f"seg {i} x")
            self.assertAlmostEqual(prev_end[1], start[1], places=9, msg=f"seg {i} y")

    def test_arcs_and_lines_alternate(self):
        segs = step_writer.rounded_rect(0, 0, 100, 80, 5)
        self.assertEqual([s[0] for s in segs], ["line", "arc"] * 4)

    def test_the_profile_stays_inside_the_requested_rectangle(self):
        segs = step_writer.rounded_rect(0, 0, 100, 80, 5)
        pts = [step_writer.seg_start(s) for s in segs] + \
              [step_writer.seg_end(s) for s in segs]
        self.assertAlmostEqual(min(p[0] for p in pts), 0.0)
        self.assertAlmostEqual(max(p[0] for p in pts), 100.0)
        self.assertAlmostEqual(min(p[1] for p in pts), 0.0)
        self.assertAlmostEqual(max(p[1] for p in pts), 80.0)


class TestValidate(unittest.TestCase):
    def test_accepts_each_edge_used_once_per_direction(self):
        self.assertEqual(step_writer.validate([(1, True), (1, False)]), [])

    def test_rejects_an_edge_used_once(self):
        """A dangling edge is a hole in the shell."""
        problems = step_writer.validate([(7, True)])
        self.assertTrue(any("#7" in p and "1x" in p for p in problems))

    def test_rejects_an_edge_used_twice_the_same_way(self):
        """Same direction twice means the two faces disagree about which side
        is inside -- the solid would import inverted or fail to sew."""
        problems = step_writer.validate([(3, True), (3, True)])
        self.assertTrue(any("same direction" in p for p in problems))

    def test_rejects_an_edge_used_three_times(self):
        problems = step_writer.validate([(2, True), (2, False), (2, True)])
        self.assertTrue(any("#2" in p for p in problems))


class TestWrittenFile(unittest.TestCase):
    def write(self, plates):
        fd, path = tempfile.mkstemp(suffix=".step")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        step_writer.write(path, plates)
        return path

    def test_a_plain_plate_writes_and_parses(self):
        entities = parse(self.write([a_plate()]))
        self.assertGreater(len(entities), 100)

    def test_every_reference_resolves(self):
        entities = parse(self.write([a_plate(holes=[(50, 40, 3.3)])]))
        for n, body in entities.items():
            for ref in re.findall(r"#(\d+)", body):
                self.assertIn(ref, entities, f"#{n} points at missing #{ref}")

    def test_face_count_matches_the_topology(self):
        """10 faces for the prism -- top, bottom, 4 straight walls, 4 corner
        fillets -- plus 2 half-cylinders per hole."""
        for n_holes in (0, 1, 6):
            holes = [(10 + 10 * i, 40, 3.3) for i in range(n_holes)]
            entities = parse(self.write([a_plate(holes=holes)]))
            self.assertEqual(kinds(entities)["ADVANCED_FACE"], 10 + 2 * n_holes,
                             f"with {n_holes} holes")

    def test_surface_and_vertex_counts_match_the_topology(self):
        entities = parse(self.write([a_plate(holes=[(50, 40, 3.3), (20, 20, 3.3)])]))
        c = kinds(entities)
        self.assertEqual(c["PLANE"], 6)                  # 4 walls + top + bottom
        self.assertEqual(c["CYLINDRICAL_SURFACE"], 4 + 2 * 2)   # corners + holes
        self.assertEqual(c["VERTEX_POINT"], 16 + 4 * 2)
        self.assertEqual(c["EDGE_CURVE"], 24 + 6 * 2)
        self.assertEqual(c["MANIFOLD_SOLID_BREP"], 1)
        self.assertEqual(c["CLOSED_SHELL"], 1)

    def test_extents_match_the_requested_plate(self):
        path = self.write([a_plate(thickness=6.0, w=120.0, h=90.0)])
        pts = [tuple(map(float, m)) for m in re.findall(
            r"CARTESIAN_POINT\('',\(([-\d.E+]+),([-\d.E+]+),([-\d.E+]+)\)\)",
            read(path))]
        xs, ys, zs = zip(*pts)
        self.assertAlmostEqual(min(xs), 0.0)
        self.assertAlmostEqual(max(xs), 120.0)
        self.assertAlmostEqual(min(ys), 0.0)
        self.assertAlmostEqual(max(ys), 90.0)
        self.assertAlmostEqual(min(zs), 0.0)
        self.assertAlmostEqual(max(zs), 6.0, msg="thickness must reach the top face")

    def test_holes_are_the_diameter_asked_for(self):
        """A hole written at the wrong radius is a panel that will not bolt
        down, and it is invisible in every 2D preview."""
        path = self.write([a_plate(holes=[(50, 40, 3.3)])])
        radii = [float(r) for r in re.findall(r"CIRCLE\('',#\d+,([\d.E+]+)\)",
                                              read(path))]
        self.assertEqual(sorted(set(radii)), [1.65, 5.0])   # hole, corner fillet

    def test_every_edge_loop_is_continuous(self):
        """The orientation flags must actually chain up, holes included."""
        entities = parse(self.write([a_plate(holes=[(50, 40, 3.3), (20, 20, 3.3)])]))
        self.assertEqual(loop_discontinuities(entities), [])

    def test_two_plates_make_two_solids(self):
        entities = parse(self.write([a_plate(), a_plate()]))
        self.assertEqual(kinds(entities)["MANIFOLD_SOLID_BREP"], 2)
        self.assertEqual(kinds(entities)["CLOSED_SHELL"], 2)

    def test_units_are_millimetres(self):
        body = read(self.write([a_plate()]))
        self.assertIn("SI_UNIT(.MILLI.,.METRE.)", body)

    def test_reals_always_carry_a_decimal_point(self):
        """'5' is an integer in STEP and a type error where a real is wanted."""
        num = step_writer.StepFile.num
        self.assertEqual(num(5), "5.")
        self.assertEqual(num(0), "0.")
        self.assertIn(".", num(1.65))


class TestGeneratedBoard(unittest.TestCase):
    """The real artifact, if it has been generated."""

    PATH = os.path.join(os.path.dirname(__file__), "..",
                        "design", "ChessBot_V2_board.step")

    def setUp(self):
        if not os.path.exists(self.PATH):
            self.skipTest("run tools/make_board.py first")

    def test_it_is_two_panels_with_sixteen_holes(self):
        c = kinds(parse(self.PATH))
        self.assertEqual(c["MANIFOLD_SOLID_BREP"], 2)
        # 6 holes on panel 1 + 10 on panel 2, two half-cylinders each, plus
        # 4 corner fillets per panel.
        self.assertEqual(c["CYLINDRICAL_SURFACE"], 2 * 4 + 2 * 16)
        self.assertEqual(c["ADVANCED_FACE"], (10 + 2 * 6) + (10 + 2 * 10))

    def test_every_edge_loop_is_continuous(self):
        self.assertEqual(loop_discontinuities(parse(self.PATH)), [])

    def test_the_panels_are_570_square(self):
        pts = [tuple(map(float, m)) for m in re.findall(
            r"CARTESIAN_POINT\('',\(([-\d.E+]+),([-\d.E+]+),([-\d.E+]+)\)\)",
            read(self.PATH))]
        xs, ys, zs = zip(*pts)
        self.assertAlmostEqual(max(ys), 570.0)
        self.assertAlmostEqual(max(zs), 5.0)
        # Two panels side by side with the gap carried over from the original.
        self.assertAlmostEqual(max(xs), 570.0 * 2 + 13.6, places=3)


if __name__ == "__main__":
    unittest.main()
