"""AI-Powered HUD Enclosure v6 -- build123d parametric model.

Open-top tray design for touch-enabled HUD display:
  1. Display face exposed on top surface (no cover plate) for touch input
  2. 4 retaining tabs hold display edges (picture-frame clip style)
  3. USB-C + FPC cable opening on -Y wall (bottom edge)
  4. Camera housing: compact bump-out on +Y edge (top), lens faces -Y (road)
  5. Camera bracket: simple box, M2 set screw tilt via housing +Y wall

Orientation convention (as placed on dashboard):
  -Y = bottom (USB-C, FPC cable exit)
  +Y = top    (camera housing, lens faces -Y toward road)

Side view (left=bottom/-Y, right=top/+Y):

                    [cam housing]  +Y (top, camera)
   +--+ tab    tab +--+/          lens faces left (-Y)
   |  |  DISPLAY   |  |           screen face exposed (touch!)
   |  |============|  |           display ledge
   |  | cable zone |  |
   |  |    PCB     |  |
   +--+============+--+
   [USB-C] [FPC]                  -Y (bottom, power + cables)
   ~~~~~~~~~~~~~~~~                dashboard

Top view (looking DOWN at exposed display):

    +Y (top, camera housing here)
    +--[tab]-[cam housing]-[tab]---+
    |  +------------------------+  |
    |  |  Display 71.9x70.2    |  |  <- touch surface exposed
    |  |  (visible area)       |  |
    |  +------------------------+  |
    +---[tab]--[FPC]--[tab]--------+
    -Y (bottom, USB-C + FPC cable exit)

Parts (3 STL files):
  1. bottom_shell  - base, USB-C on -Y wall, vents, screw bosses, anti-slip pads
  2. top_shell     - open-top tray, display ledge + tabs, camera housing
  3. cam_bracket   - closed-bottom open-top box, M2 set screw tilt
"""

from build123d import *
from build123d import export_stl, export_step

try:
    from ocp_vscode import show
except ImportError:
    show = None

# ============================================================
# PARAMETERS (mm) -- adjust after test-fit printing
# ============================================================

# -- Component dimensions --
DISPLAY_W        = 84.2         # display module width
DISPLAY_H        = 84.2         # display module height
DISPLAY_T        = 3.5          # display module thickness
DISPLAY_ACTIVE_W = 71.9         # visible area width
DISPLAY_ACTIVE_H = 70.2         # visible area height

PCB_W     = 50.0                # Luckfox Pico Ultra
PCB_H     = 50.0
PCB_T     = 1.6
PCB_COMP  = 8.5                 # tallest component on PCB
PCB_TOTAL = PCB_T + PCB_COMP    # ~10.1

CAM_W      = 25.0               # SC3336 camera module
CAM_D      = 24.0
CAM_H      = 18.0
CAM_LENS_D = 14.0

GPS_W = 20.0                    # E108-GN03D
GPS_D = 22.0

# -- Port dimensions (from connector datasheets) --
USBC_W = 9.0;  USBC_H = 3.5    # USB-C female

# -- Port offsets on board (USB-C centered on Edge A) --
USBC_BRD_X = 0.0

# -- Enclosure geometry --
WALL = 2.5
TOL  = 0.5

INNER_W = DISPLAY_W + TOL * 2              # 85.2
INNER_H = DISPLAY_H + TOL * 2              # 85.2
OUTER_W = INNER_W + WALL * 2               # 90.2
OUTER_H = INNER_H + WALL * 2               # 90.2

# Vertical stack (bottom to top):
BOTTOM_WALL  = WALL                         # 2.5
PCB_ZONE     = PCB_TOTAL + 2.0             # 12.1
CABLE_ZONE   = 15.0                         # FPC bend radius (>= 1.5cm)
DISPLAY_ZONE = DISPLAY_T + 1.0             # 4.5 (display + gap below)
TOP_RIM      = 1.0                          # rim height above display (no cover plate)

TOTAL_Z = BOTTOM_WALL + PCB_ZONE + CABLE_ZONE + DISPLAY_ZONE + TOP_RIM  # 35.1
SPLIT_Z = BOTTOM_WALL + PCB_ZONE + CABLE_ZONE / 2  # 22.1 (mid cable zone)

# PCB position: centered X, pushed to -Y wall (Edge A = USB-C flush with -Y)
PCB_CLEARANCE = 1.0
PCB_CX = 0.0                                                   # centered
PCB_CY = -(INNER_H / 2 - PCB_H / 2 - PCB_CLEARANCE)           # -16.6

# Port positions on enclosure walls
USBC_WALL_X = PCB_CX + USBC_BRD_X          # 0.0 (centered on -Y wall)

# Port Z height (from enclosure bottom)
PORT_Z_USBC = BOTTOM_WALL + PCB_T + USBC_H / 2 + 1    # ~5.85

FILLET_R     = 2.0
SCREW_D      = 2.5
SCREW_BOSS_D = 6.0
BOSS_INSET   = WALL + 4.0                   # 6.5 from outer edge

# Camera housing (bump-out from +Y edge of top shell)
TURRET_WALL = 3.0                           # up from 2.5 for strength
TURRET_W  = 37.0                            # housing exterior X (bracket 28 + 1.5 clearance/side + 3 wall/side)
TURRET_D  = 30.0                            # housing exterior Y (bracket 21 + 1.5 clearance/side + 3 wall/side)
TURRET_H  = CAM_D + 10                      # 34 (rotated cam Z=24 + bracket wall + housing walls + clearance)
TURRET_CX = -15.0                           # driver side (RHD: -X = right of car)

# Buttress rib dimensions
RIB_LENGTH = 12.0                           # along shell surface
RIB_THICK  = 2.5                            # rib thickness

# Camera bracket
CAM_BKT_WALL = 1.5                          # thinner walls to fit turret interior

# M2 set screw for tilt adjustment (through housing +Y wall)
SETSCREW_D = 2.0                            # M2 thread diameter

# Display retaining tabs (picture-frame clip style)
TAB_L        = 10.0                         # tab length along wall edge
TAB_OVERHANG = 1.5                          # inward overlap over display border

# Cable routing notch at shell split (internal, groove fill only)
CABLE_SLOT_W = 35.0                        # width for LCD FPC + CAM MIPI side by side

# Anti-slip pad dimensions
PAD_D = 8.0
PAD_H = 0.5


# ============================================================
# BOTTOM SHELL
# ============================================================
def make_bottom_shell():
    """Base: dashboard-facing. USB-C on -Y wall, vents, screw bosses, anti-slip pads."""
    h = SPLIT_Z  # 22.1

    with BuildPart() as bp:
        # -- Main box --
        Box(OUTER_W, OUTER_H, h, align=(Align.CENTER, Align.CENTER, Align.MIN))
        fillet(bp.edges().filter_by(Axis.Z), radius=FILLET_R)

        # -- Hollow interior --
        with Locations((0, 0, BOTTOM_WALL)):
            Box(INNER_W, INNER_H, h,
                align=(Align.CENTER, Align.CENTER, Align.MIN),
                mode=Mode.SUBTRACT)

        # -- USB-C cutout: -Y wall (road/windshield side), centered --
        with BuildSketch(Plane.XZ.offset(-OUTER_H / 2)):
            with Locations((USBC_WALL_X, PORT_Z_USBC)):
                RectangleRounded(USBC_W + 2, USBC_H + 2, 0.8)
        extrude(amount=WALL * 2, mode=Mode.SUBTRACT)

        # -- Bottom vent slots (5 parallel) --
        for i in range(5):
            vx = (i - 2) * 10.0
            with BuildSketch(Plane.XY):
                with Locations((vx, 0)):
                    RectangleRounded(2.5, 40.0, 0.8)
            extrude(amount=-BOTTOM_WALL * 2, mode=Mode.SUBTRACT)

        # -- Screw bosses (4 corners) --
        boss_h = h - BOTTOM_WALL
        for sx in [1, -1]:
            for sy in [1, -1]:
                bx = sx * (OUTER_W / 2 - BOSS_INSET)
                by = sy * (OUTER_H / 2 - BOSS_INSET)
                with Locations((bx, by, BOTTOM_WALL)):
                    Cylinder(SCREW_BOSS_D / 2, boss_h,
                             align=(Align.CENTER, Align.CENTER, Align.MIN))
                    Cylinder(SCREW_D / 2, boss_h,
                             align=(Align.CENTER, Align.CENTER, Align.MIN),
                             mode=Mode.SUBTRACT)

        # -- Mating lip (top rim for shell alignment) --
        lip_w = 1.5
        lip_h = 2.0
        with Locations((0, 0, h)):
            Box(OUTER_W - WALL, OUTER_H - WALL, lip_h,
                align=(Align.CENTER, Align.CENTER, Align.MIN))
            Box(OUTER_W - WALL - lip_w * 2, OUTER_H - WALL - lip_w * 2, lip_h,
                align=(Align.CENTER, Align.CENTER, Align.MIN),
                mode=Mode.SUBTRACT)

        # -- Anti-slip pads (4 corner circles on bottom face) --
        for sx in [1, -1]:
            for sy in [1, -1]:
                px = sx * (OUTER_W / 2 - 8)
                py = sy * (OUTER_H / 2 - 8)
                with Locations((px, py, 0)):
                    Cylinder(PAD_D / 2, PAD_H,
                             align=(Align.CENTER, Align.CENTER, Align.MAX))

    return bp.part


# ============================================================
# TOP SHELL (open-top tray for touch display)
# ============================================================
def make_top_shell():
    """Open-top tray: display face exposed for touch, retaining tabs, camera housing."""
    h = TOTAL_Z - SPLIT_Z  # 13.0

    with BuildPart() as bp:
        # -- Main box --
        Box(OUTER_W, OUTER_H, h, align=(Align.CENTER, Align.CENTER, Align.MIN))
        fillet(bp.edges().filter_by(Axis.Z), radius=FILLET_R)

        # -- Hollow interior (fully open top, no cover plate) --
        Box(INNER_W, INNER_H, h,
            align=(Align.CENTER, Align.CENTER, Align.MIN),
            mode=Mode.SUBTRACT)

        # -- Display ledge (display drops in from top, face exposed) --
        # Assembly: drop display face-up into tray, edges rest on ledge,
        # retaining tabs hold display down. Screen faces up for touch + HUD.
        ledge_z = h - TOP_RIM - DISPLAY_T     # 8.5 (display bottom face)
        with Locations((0, 0, ledge_z)):
            Box(INNER_W, INNER_H, DISPLAY_T,
                align=(Align.CENTER, Align.CENTER, Align.MIN))
            Box(DISPLAY_ACTIVE_W, DISPLAY_ACTIVE_H, DISPLAY_T,
                align=(Align.CENTER, Align.CENTER, Align.MIN),
                mode=Mode.SUBTRACT)

        # -- FPC cable opening in ledge (-Y side, near USB-C) --
        # Offset inward to avoid overlap with -Y retaining tab
        with Locations((0, -(INNER_H / 2 - 12), ledge_z)):
            Box(30, 18, DISPLAY_T + 1,
                align=(Align.CENTER, Align.CENTER, Align.MIN),
                mode=Mode.SUBTRACT)

        # -- Display retaining tabs (4 per wall edge, picture-frame clips) --
        # Tabs sit at display face level in the rim zone, overlap display border
        tab_z = h - TOP_RIM                    # 12.0 (display face level)
        for tx, ty, tw, td, ax, ay in [
            ( INNER_W / 2, 0, TAB_OVERHANG, TAB_L, Align.MAX, Align.CENTER),  # +X
            (-INNER_W / 2, 0, TAB_OVERHANG, TAB_L, Align.MIN, Align.CENTER),  # -X
            (0,  INNER_H / 2, TAB_L, TAB_OVERHANG, Align.CENTER, Align.MAX),  # +Y
            (0, -INNER_H / 2, TAB_L, TAB_OVERHANG, Align.CENTER, Align.MIN),  # -Y
        ]:
            with Locations((tx, ty, tab_z)):
                Box(tw, td, TOP_RIM, align=(ax, ay, Align.MIN))

        # -- Camera housing (compact bump-out from +Y edge, lens faces -Y) --
        turret_cy = OUTER_H / 2 + TURRET_D / 2 - WALL
        turret_inner_w = TURRET_W - TURRET_WALL * 2   # 31
        turret_inner_d = TURRET_D - TURRET_WALL * 2   # 24

        # Solid housing body
        with Locations((TURRET_CX, turret_cy, h)):
            Box(TURRET_W, TURRET_D, TURRET_H,
                align=(Align.CENTER, Align.CENTER, Align.MIN))

        # Hollow housing interior
        with Locations((TURRET_CX, turret_cy, h - 1)):
            Box(turret_inner_w, turret_inner_d, TURRET_H + 1,
                align=(Align.CENTER, Align.CENTER, Align.MIN),
                mode=Mode.SUBTRACT)

        # Lens opening on housing -Y wall (camera faces forward toward road)
        lens_z = h + CAM_BKT_WALL + CAM_D / 2
        lens_y = turret_cy - TURRET_D / 2 + TURRET_WALL / 2
        with Locations((TURRET_CX, lens_y, lens_z)):
            Cylinder(CAM_LENS_D / 2 + 1.5, TURRET_WALL + 2,
                     rotation=(90, 0, 0),
                     align=(Align.CENTER, Align.CENTER, Align.CENTER),
                     mode=Mode.SUBTRACT)

        # FPC cable slot through housing floor (16x5mm)
        with Locations((TURRET_CX, OUTER_H / 2, h)):
            Box(16, TURRET_WALL * 3, 5,
                align=(Align.CENTER, Align.CENTER, Align.CENTER),
                mode=Mode.SUBTRACT)

        # -- Buttress gusset ribs (4 triangular ribs at housing base corners) --
        turret_left = TURRET_CX - TURRET_W / 2
        turret_right = TURRET_CX + TURRET_W / 2
        turret_back = OUTER_H / 2

        for (rx, ry_dir) in [
            (turret_left, 1),     # left-back
            (turret_right, 1),    # right-back
            (turret_left, -1),    # left-front (on shell surface)
            (turret_right, -1),   # right-front (on shell surface)
        ]:
            rib_base_y = turret_back
            with BuildSketch(Plane.YZ.offset(rx)):
                with Locations((rib_base_y - ry_dir * RIB_LENGTH / 2,
                                h + TURRET_H / 2)):
                    pts = [
                        (0, -TURRET_H / 2),
                        (0, TURRET_H / 2),
                        (-ry_dir * RIB_LENGTH, -TURRET_H / 2),
                    ]
                    Polygon(pts)
            extrude(amount=RIB_THICK, mode=Mode.ADD)

        # -- M2 set screw hole through housing +Y wall (outermost, accessible) --
        screw_z = h + TURRET_H - 5
        screw_y = turret_cy + TURRET_D / 2 - TURRET_WALL / 2
        with Locations((TURRET_CX, screw_y, screw_z)):
            Cylinder(SETSCREW_D / 2, TURRET_WALL + 2,
                     rotation=(90, 0, 0),
                     align=(Align.CENTER, Align.CENTER, Align.CENTER),
                     mode=Mode.SUBTRACT)

        # GPS: no thin-wall window needed -- open-top design gives GPS direct
        # sky view through display material (better than plastic wall)

        # -- Screw holes (4 corners, through-hole) --
        for sx in [1, -1]:
            for sy in [1, -1]:
                bx = sx * (OUTER_W / 2 - BOSS_INSET)
                by = sy * (OUTER_H / 2 - BOSS_INSET)
                with Locations((bx, by, 0)):
                    Cylinder(SCREW_D / 2, h,
                             align=(Align.CENTER, Align.CENTER, Align.MIN),
                             mode=Mode.SUBTRACT)

        # -- Mating groove (bottom rim, receives bottom shell lip) --
        lip_w = 1.5
        groove_h = 2.3
        groove_tol = 0.2
        Box(OUTER_W - WALL + groove_tol, OUTER_H - WALL + groove_tol, groove_h,
            align=(Align.CENTER, Align.CENTER, Align.MIN),
            mode=Mode.SUBTRACT)
        Box(OUTER_W - WALL - lip_w * 2 - groove_tol,
            OUTER_H - WALL - lip_w * 2 - groove_tol, groove_h,
            align=(Align.CENTER, Align.CENTER, Align.MIN))

        # -- Cable routing notch in groove fill (-Y side, near USB-C) --
        groove_fill_y = (OUTER_H - WALL - lip_w * 2 - groove_tol) / 2
        with Locations((0, -groove_fill_y, 0)):
            Box(CABLE_SLOT_W, lip_w + groove_tol + 1, groove_h,
                align=(Align.CENTER, Align.CENTER, Align.MIN),
                mode=Mode.SUBTRACT)

    return bp.part


# ============================================================
# CAMERA TILT BRACKET
# ============================================================
def make_cam_bracket():
    """Closed-bottom open-top box holding camera rotated 90 deg (lens -Y).

    Camera original: 25(W)x24(D)x18(H), lens on +Z face.
    Rotated -90 deg around X: lens now on -Y face.
    After rotation: 25(X) x 18(Y) x 24(Z).

    Snug fit (no internal tolerance) -- clearance provided by housing cavity.
    Drops into housing from top. M2 set screw through housing +Y wall
    pushes bracket top, bottom edge pivots on housing floor for tilt.
    """
    # Interior sized for rotated camera (snug, clearance in housing)
    inner_w = CAM_W                             # 25 (X)
    inner_d = CAM_H                             # 18 (Y, was camera height)
    inner_h = CAM_D                             # 24 (Z, was camera depth)
    outer_w = inner_w + CAM_BKT_WALL * 2       # 28
    outer_d = inner_d + CAM_BKT_WALL * 2       # 21
    h = inner_h + CAM_BKT_WALL                 # 25.5 (closed bottom, open top)

    with BuildPart() as bp:
        # Outer box (open top for camera assembly)
        Box(outer_w, outer_d, h, align=(Align.CENTER, Align.CENTER, Align.MIN))
        # Hollow from top
        with Locations((0, 0, CAM_BKT_WALL)):
            Box(inner_w, inner_d, h,
                align=(Align.CENTER, Align.CENTER, Align.MIN),
                mode=Mode.SUBTRACT)

        # Lens hole on -Y face (camera faces forward)
        lens_z = CAM_BKT_WALL + inner_h / 2
        with Locations((0, -outer_d / 2, lens_z)):
            Cylinder(CAM_LENS_D / 2 + 0.5, CAM_BKT_WALL * 2,
                     rotation=(90, 0, 0),
                     align=(Align.CENTER, Align.CENTER, Align.CENTER),
                     mode=Mode.SUBTRACT)

        # FPC cable slot on +Y face (cable exits back of rotated camera)
        with Locations((0, outer_d / 2, CAM_BKT_WALL + inner_h / 2)):
            Box(12, CAM_BKT_WALL + 1, 8,
                align=(Align.CENTER, Align.CENTER, Align.CENTER),
                mode=Mode.SUBTRACT)

    return bp.part


# ============================================================
# EXPORT & PREVIEW
# ============================================================
if __name__ == "__main__":
    import os

    out_dir = os.path.dirname(os.path.abspath(__file__))

    builders = [
        ("bottom_shell", "Building bottom shell...", make_bottom_shell),
        ("top_shell",    "Building top shell...",    make_top_shell),
        ("cam_bracket",  "Building camera bracket...", make_cam_bracket),
    ]

    parts = {}
    for name, msg, fn in builders:
        print(msg)
        try:
            parts[name] = fn()
            print(f"  OK: {name}")
        except Exception as e:
            print(f"  FAILED: {name} -- {e}")
            import traceback
            traceback.print_exc()

    exported = 0
    for name, obj in parts.items():
        stl_path = os.path.join(out_dir, f"hud_{name}.stl")
        stp_path = os.path.join(out_dir, f"hud_{name}.step")
        export_stl(obj, stl_path)
        export_step(obj, stp_path)
        print(f"  Exported: hud_{name}.stl/.step")
        exported += 1

    print(f"\n{exported}/{len(builders)} parts exported to {out_dir}/")
    print(f"Enclosure: {OUTER_W:.1f} x {OUTER_H:.1f} x {TOTAL_Z:.1f} mm (main body)")
    print(f"Display: face exposed at top, {TOP_RIM:.0f}mm rim + 4 retaining tabs")
    print(f"Camera housing adds {TURRET_H:.0f}mm height, {TURRET_D - WALL:.0f}mm depth beyond +Y wall")
    print(f"USB-C on -Y wall (road side), PCB at Y={PCB_CY:.1f}")

    # OCP CAD Viewer (exploded assembly)
    if show is not None:
        gap = 30
        show_parts, show_names, show_colors = [], [], []

        if "bottom_shell" in parts:
            show_parts.append(parts["bottom_shell"])
            show_names.append("Bottom Shell")
            show_colors.append("slategray")
        if "top_shell" in parts:
            show_parts.append(parts["top_shell"].moved(Location((0, 0, SPLIT_Z + gap))))
            show_names.append("Top Shell (open-top tray)")
            show_colors.append("steelblue")
        if "cam_bracket" in parts:
            show_parts.append(parts["cam_bracket"].moved(
                Location((OUTER_W / 2 + 25, 20, SPLIT_Z + gap + 20))))
            show_names.append("Camera Bracket")
            show_colors.append("darkorange")
        if show_parts:
            show(*show_parts, names=show_names, colors=show_colors)
            print("Sent to OCP CAD Viewer.")
