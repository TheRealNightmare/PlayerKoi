"""A minimal STEP AP214 writer for flat plates: extruded profile, round holes.

Why hand-rolled: this machine has no CAD kernel installed -- no cadquery, no
OCP, no FreeCAD, no OpenSCAD -- and the panel is a shape simple enough that a
solid model of it can be written out directly. It is a prism: one closed
profile of lines and arcs, swept a thickness, with cylindrical holes through.

This emits a proper analytic B-rep, not a faceted mesh. The corner fillets are
real CYLINDRICAL_SURFACEs and the holes are real cylinders, so the model
imports as an editable solid with measurable radii rather than as a 64-sided
approximation of one.

THE PART THAT IS EASY TO GET WRONG

A STEP solid is only valid if every face's outward normal points out of the
material and every edge loop winds counter-clockwise about that normal. Get it
backwards and the file still parses, but the importer either inverts the solid
or refuses to sew it. Two rules settle every face here:

  * Traversing a bottom edge along the profile's counter-clockwise direction
    and then going UP gives a normal of `direction x Z`, which points OUT of
    the material for the outer wall. So outer side faces use the loop
    [bottom, up, top reversed, down] as-is.

  * A hole's outward normal points INWARD, toward its own axis -- the material
    is outside it. So hole faces take the same loop reversed, and their
    CYLINDRICAL_SURFACE is flagged same_sense = .F. to flip the natural
    outward-radial normal.

Every edge must also be used exactly twice, once in each direction. That is
checked before the file is written (see `validate`), because a silently
non-manifold solid is the failure that wastes a trip to the laser shop.

Angles are degrees, lengths millimetres, and the profile must run
counter-clockwise in XY. The plate is extruded from z=0 to z=thickness.
"""

import math
from pathlib import Path


def line(start, end):
    return ("line", start, end)


def arc(centre, radius, start_deg, end_deg):
    """A counter-clockwise arc. end_deg must be greater than start_deg."""
    return ("arc", centre, radius, start_deg, end_deg)


def rounded_rect(x, y, w, h, r):
    """A closed counter-clockwise profile, starting at (x + r, y)."""
    x1, y1 = x + w, y + h
    return [
        line((x + r, y), (x1 - r, y)),
        arc((x1 - r, y + r), r, -90, 0),
        line((x1, y + r), (x1, y1 - r)),
        arc((x1 - r, y1 - r), r, 0, 90),
        line((x1 - r, y1), (x + r, y1)),
        arc((x + r, y1 - r), r, 90, 180),
        line((x, y1 - r), (x, y + r)),
        arc((x + r, y + r), r, 180, 270),
    ]


def _at(centre, radius, deg):
    a = math.radians(deg)
    return (centre[0] + radius * math.cos(a), centre[1] + radius * math.sin(a))


def seg_start(s):
    return s[1] if s[0] == "line" else _at(s[1], s[2], s[3])


def seg_end(s):
    return s[2] if s[0] == "line" else _at(s[1], s[2], s[4])


class Plate:
    """One extruded solid: a closed profile, holes, and a thickness."""

    def __init__(self, profile, holes, thickness, name="plate"):
        self.profile = profile
        self.holes = holes          # [(cx, cy, diameter), ...]
        self.thickness = float(thickness)
        self.name = name


class StepFile:
    """Accumulates entities and renders the ISO-10303-21 text."""

    def __init__(self, name="part"):
        self.name = name
        self.lines = []             # entity bodies, index 0 -> #1

    def add(self, body):
        self.lines.append(body)
        return len(self.lines)      # the #n that was just assigned

    # ------------------------------------------------------------ primitives

    @staticmethod
    def num(v):
        """STEP reals always carry a decimal point; 5 would be an integer."""
        out = f"{float(v):.9G}"
        if "." not in out and "E" not in out:
            out += "."
        return out

    def point(self, x, y, z):
        return self.add(f"CARTESIAN_POINT('',({self.num(x)},{self.num(y)},{self.num(z)}))")

    def direction(self, x, y, z):
        return self.add(f"DIRECTION('',({self.num(x)},{self.num(y)},{self.num(z)}))")

    def placement(self, origin, axis, ref):
        p = self.point(*origin)
        a = self.direction(*axis)
        r = self.direction(*ref)
        return self.add(f"AXIS2_PLACEMENT_3D('',#{p},#{a},#{r})")

    def vertex(self, xyz):
        return self.add(f"VERTEX_POINT('',#{self.point(*xyz)})")

    def line_curve(self, start, direction):
        p = self.point(*start)
        d = self.direction(*direction)
        v = self.add(f"VECTOR('',#{d},1.)")
        return self.add(f"LINE('',#{p},#{v})")

    def circle_curve(self, centre, z, radius):
        pl = self.placement((centre[0], centre[1], z), (0, 0, 1), (1, 0, 0))
        return self.add(f"CIRCLE('',#{pl},{self.num(radius)})")

    def edge(self, v0, v1, curve):
        return self.add(f"EDGE_CURVE('',#{v0},#{v1},#{curve},.T.)")

    def face(self, bounds, surface, same_sense):
        """bounds: [(edge_loop_id, is_outer)], already oriented."""
        refs = []
        for loop, outer in bounds:
            kind = "FACE_OUTER_BOUND" if outer else "FACE_BOUND"
            refs.append(self.add(f"{kind}('',#{loop},.T.)"))
        joined = ",".join(f"#{r}" for r in refs)
        flag = ".T." if same_sense else ".F."
        return self.add(f"ADVANCED_FACE('',({joined}),#{surface},{flag})")

    def loop(self, oriented):
        """oriented: [(edge_id, forward_bool)]"""
        ids = [self.add(f"ORIENTED_EDGE('',*,*,#{e},{'.T.' if f else '.F.'})")
               for e, f in oriented]
        joined = ",".join(f"#{i}" for i in ids)
        return self.add(f"EDGE_LOOP('',({joined}))")


def _build_plate(sf, plate, usage):
    """Adds one plate's faces, returns the CLOSED_SHELL id.

    `usage` collects (edge_id, forward) so validate() can prove the shell is
    closed and manifold before anything is written out.
    """
    t = plate.thickness
    segs = plate.profile
    n = len(segs)

    # --- outer profile: vertices, then bottom / top / vertical edges
    starts = [seg_start(s) for s in segs]
    vb = [sf.vertex((p[0], p[1], 0.0)) for p in starts]
    vt = [sf.vertex((p[0], p[1], t)) for p in starts]

    def profile_edge(i, z, verts):
        s = segs[i]
        j = (i + 1) % n
        if s[0] == "line":
            p0, p1 = seg_start(s), seg_end(s)
            d = (p1[0] - p0[0], p1[1] - p0[1], 0.0)
            length = math.hypot(d[0], d[1])
            curve = sf.line_curve((p0[0], p0[1], z), (d[0] / length, d[1] / length, 0.0))
        else:
            curve = sf.circle_curve(s[1], z, s[2])
        return sf.edge(verts[i], verts[j], curve)

    eb = [profile_edge(i, 0.0, vb) for i in range(n)]
    et = [profile_edge(i, t, vt) for i in range(n)]
    ev = [sf.edge(vb[i], vt[i],
                  sf.line_curve((starts[i][0], starts[i][1], 0.0), (0, 0, 1)))
          for i in range(n)]

    faces = []

    # --- side walls. Loop [bottom, up, top reversed, down] gives an outward
    # normal for a counter-clockwise profile; see the module docstring.
    for i in range(n):
        j = (i + 1) % n
        oriented = [(eb[i], True), (ev[j], True), (et[i], False), (ev[i], False)]
        usage.extend(oriented)
        s = segs[i]
        if s[0] == "line":
            p0, p1 = seg_start(s), seg_end(s)
            dx, dy = p1[0] - p0[0], p1[1] - p0[1]
            length = math.hypot(dx, dy)
            dx, dy = dx / length, dy / length
            outward = (dy, -dx, 0.0)        # direction x Z
            surf = sf.add(f"PLANE('',#{sf.placement((p0[0], p0[1], 0.0), outward, (dx, dy, 0.0))})")
            same = True
        else:
            surf = sf.add(
                f"CYLINDRICAL_SURFACE('',"
                f"#{sf.placement((s[1][0], s[1][1], 0.0), (0, 0, 1), (1, 0, 0))},"
                f"{sf.num(s[2])})")
            same = True                     # natural normal is outward-radial
        faces.append(sf.face([(sf.loop(oriented), True)], surf, same))

    # --- holes: two 180-degree faces each, so no seam edge is needed
    hole_bottom_loops, hole_top_loops = [], []
    for cx, cy, dia in plate.holes:
        r = dia / 2.0
        p_right, p_left = (cx + r, cy), (cx - r, cy)
        b0, b1 = sf.vertex((*p_right, 0.0)), sf.vertex((*p_left, 0.0))
        t0, t1 = sf.vertex((*p_right, t)), sf.vertex((*p_left, t))

        cb, ct = sf.circle_curve((cx, cy), 0.0, r), sf.circle_curve((cx, cy), t, r)
        a_bot, b_bot = sf.edge(b0, b1, cb), sf.edge(b1, b0, cb)   # 0->180, 180->360
        a_top, b_top = sf.edge(t0, t1, ct), sf.edge(t1, t0, ct)
        v_right, v_left = sf.edge(b0, t0, sf.line_curve((*p_right, 0.0), (0, 0, 1))), \
            sf.edge(b1, t1, sf.line_curve((*p_left, 0.0), (0, 0, 1)))

        # Reversed relative to the outer wall, because a hole's outward normal
        # points inward -- and same_sense .F. flips the surface to match.
        # Each half gets its own surface entity: sharing one between two faces
        # is legal but trips some importers' sewing.
        for up, top_edge, down, bot_edge in (
            (v_right, a_top, v_left, a_bot),
            (v_left, b_top, v_right, b_bot),
        ):
            oriented = [(up, True), (top_edge, True), (down, False), (bot_edge, False)]
            usage.extend(oriented)
            surf = sf.add(
                f"CYLINDRICAL_SURFACE('',"
                f"#{sf.placement((cx, cy, 0.0), (0, 0, 1), (1, 0, 0))},{sf.num(r)})")
            faces.append(sf.face([(sf.loop(oriented), True)], surf, False))

        # Inner bounds of the flat faces wind opposite to their outer bound.
        hole_bottom_loops.append([(a_bot, True), (b_bot, True)])
        hole_top_loops.append([(b_top, False), (a_top, False)])

    # --- bottom face: normal -Z, so outer bound runs clockwise seen from +Z
    bottom_outer = [(eb[i], False) for i in reversed(range(n))]
    bounds = [(sf.loop(bottom_outer), True)]
    usage.extend(bottom_outer)
    for inner in hole_bottom_loops:
        bounds.append((sf.loop(inner), False))
        usage.extend(inner)
    plane_b = sf.add(f"PLANE('',#{sf.placement((0, 0, 0.0), (0, 0, 1), (1, 0, 0))})")
    faces.append(sf.face(bounds, plane_b, False))

    # --- top face: normal +Z
    top_outer = [(et[i], True) for i in range(n)]
    bounds = [(sf.loop(top_outer), True)]
    usage.extend(top_outer)
    for inner in hole_top_loops:
        bounds.append((sf.loop(inner), False))
        usage.extend(inner)
    plane_t = sf.add(f"PLANE('',#{sf.placement((0, 0, t), (0, 0, 1), (1, 0, 0))})")
    faces.append(sf.face(bounds, plane_t, True))

    joined = ",".join(f"#{f}" for f in faces)
    return sf.add(f"CLOSED_SHELL('',({joined}))")


def validate(usage):
    """Every edge used exactly twice, once forward and once reversed.

    This is the invariant that makes the shell closed and orientable. A file
    that fails it may still open, but it will import as a surface soup or an
    inside-out solid, which is the sort of thing you discover after the panel
    has been cut.
    """
    seen = {}
    for edge, forward in usage:
        seen.setdefault(edge, []).append(forward)
    problems = []
    for edge, flags in sorted(seen.items()):
        if len(flags) != 2:
            problems.append(f"edge #{edge} used {len(flags)}x, expected 2")
        elif sorted(flags) != [False, True]:
            problems.append(f"edge #{edge} used twice in the same direction")
    return problems


def write(path, plates, name="ChessBot panel"):
    """Write `plates` as one STEP assembly. Returns the path."""
    sf = StepFile(name)
    usage = []
    shells = [_build_plate(sf, p, usage) for p in plates]

    problems = validate(usage)
    if problems:
        raise ValueError("refusing to write a non-manifold solid:\n  "
                         + "\n  ".join(problems))

    solids = [sf.add(f"MANIFOLD_SOLID_BREP('{p.name}',#{s})")
              for p, s in zip(plates, shells)]

    # Units: millimetres, and an uncertainty that says how close two points
    # have to be before the importer treats them as the same point.
    length = sf.add("(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.))")
    angle = sf.add("(NAMED_UNIT(*)PLANE_ANGLE_UNIT()SI_UNIT($,.RADIAN.))")
    solid_angle = sf.add("(NAMED_UNIT(*)SI_UNIT($,.STERADIAN.)SOLID_ANGLE_UNIT())")
    tol = sf.add(f"UNCERTAINTY_MEASURE_WITH_UNIT(LENGTH_MEASURE(1.E-07),#{length},"
                 f"'distance_accuracy_value','confusion accuracy')")
    ctx = sf.add(
        f"(GEOMETRIC_REPRESENTATION_CONTEXT(3)"
        f"GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT((#{tol}))"
        f"GLOBAL_UNIT_ASSIGNED_CONTEXT((#{length},#{angle},#{solid_angle}))"
        f"REPRESENTATION_CONTEXT('',''))")

    origin = sf.placement((0, 0, 0), (0, 0, 1), (1, 0, 0))
    items = ",".join(f"#{s}" for s in solids + [origin])
    brep = sf.add(f"ADVANCED_BREP_SHAPE_REPRESENTATION('{name}',({items}),#{ctx})")

    app = sf.add("APPLICATION_CONTEXT('automotive design')")
    sf.add(f"APPLICATION_PROTOCOL_DEFINITION('international standard',"
           f"'automotive_design',2000,#{app})")
    pdc = sf.add(f"PRODUCT_DEFINITION_CONTEXT('part definition',#{app},'design')")
    pc = sf.add(f"PRODUCT_CONTEXT('',#{app},'mechanical')")
    product = sf.add(f"PRODUCT('{name}','{name}','',(#{pc}))")
    pdf_ = sf.add(f"PRODUCT_DEFINITION_FORMATION('','',#{product})")
    pd = sf.add(f"PRODUCT_DEFINITION('design','',#{pdf_},#{pdc})")
    pds = sf.add(f"PRODUCT_DEFINITION_SHAPE('','',#{pd})")
    sf.add(f"SHAPE_DEFINITION_REPRESENTATION(#{pds},#{brep})")

    body = "\n".join(f"#{i + 1}={line};" for i, line in enumerate(sf.lines))
    text = (
        "ISO-10303-21;\n"
        "HEADER;\n"
        "FILE_DESCRIPTION((''),'2;1');\n"
        f"FILE_NAME('{Path(path).name}','',(''),(''),"
        "'MicroChess tools/step_writer.py','','');\n"
        "FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));\n"
        "ENDSEC;\n"
        "DATA;\n"
        f"{body}\n"
        "ENDSEC;\n"
        "END-ISO-10303-21;\n"
    )
    Path(path).write_text(text)
    return path
