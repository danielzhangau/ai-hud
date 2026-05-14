"""AI-Powered HUD Enclosure -- build123d parametric model.

Wedge-shaped dashboard-mount enclosure for windshield-reflection HUD.
Screen and camera face upward toward the windshield.

Components:
  - Luckfox Pico Ultra:  50.0 x 50.0 x ~10mm (PCB 1.6 + headers 8.5)
  - LF40-480480-ARK:     84.2 x 84.2 x ~3.5mm (active 71.9x70.2)
  - SC3336 Camera (B):   25.0 x 24.0 x 18.0mm (incl. lens)
  - E108-GN03D GPS:      20.0 x 22.0 x  7.8mm (incl. ceramic antenna)

Layout (top view, driver side = +Y):
  +------------------------------------+
  |          windshield side           |
  |  +------------------------------+ |
  |  |                              | |
  |  |     480x480 display          | |
  |  |     (visible window)         | |
  |  |                              | |
  |  +------------------------------+ |
  |                     [GPS]  [CAM]  |
  +------------------------------------+
            driver side (high)

Side view (wedge):
  Driver(high)          Windshield(low)
      ___________________________
     /                           |
    /  CAM  PCB  GPS             |
   /  ========================== |
  /   display + adapter board    |
  |______________________________|
  ====== anti-slip pad ===========

Usage:
  python hud_enclosure.py            # Export STL files
  # Or open in VS Code with ocp-vscode for live preview
"""

from build123d import *
try:
    from ocp_vscode import show  # VS Code live preview (install: pip install ocp-vscode)
except ImportError:
    show = None  # CLI mode -- export only, no preview

# ============================================================
# PARAMETERS (all dimensions in mm)
# ============================================================

# -- Component dimensions --
DISPLAY_W = 84.2          # Display outline width
DISPLAY_H = 84.2          # Display outline height
DISPLAY_T = 3.5           # Display thickness (panel + adapter board)
DISPLAY_ACTIVE_W = 71.9   # Visible area width
DISPLAY_ACTIVE_H = 70.2   # Visible area height

PCB_W = 50.0              # Luckfox Pico Ultra width
PCB_H = 50.0              # Luckfox Pico Ultra height
PCB_T = 1.6               # PCB thickness
PCB_HEADER_H = 8.5        # Pin header height above PCB
PCB_TOTAL_H = PCB_T + PCB_HEADER_H  # ~10.1mm

CAM_W = 25.0              # SC3336 camera width
CAM_H = 24.0              # SC3336 camera depth
CAM_T = 18.0              # Camera height (incl. lens)
CAM_LENS_D = 14.0         # Lens barrel diameter (approx)

GPS_W = 20.0              # E108-GN03D width
GPS_H = 22.0              # E108-GN03D depth
GPS_T = 7.8               # GPS height (incl. ceramic antenna)

# -- Enclosure design --
WALL = 2.5                # Wall thickness
TOL = 0.5                 # Component clearance tolerance (per side)
WEDGE_ANGLE = 8.0         # Tilt angle (degrees) for windshield reflection

# Derived outer dimensions
INNER_W = DISPLAY_W + TOL * 2          # ~85.2
INNER_H = DISPLAY_H + TOL * 2          # ~85.2
OUTER_W = INNER_W + WALL * 2           # ~90.2
OUTER_H = INNER_H + WALL * 2           # ~90.2

# Internal stack heights
CABLE_SPACE = 5.0         # Space for FPC cables between display and PCB
INTERNAL_H = (DISPLAY_T + CABLE_SPACE + PCB_TOTAL_H)  # ~18.6mm

# Wedge: driver side (high) accommodates camera on top of PCB
HIGH_SIDE = INTERNAL_H + CAM_T + WALL * 2    # ~43.1mm (camera sticks up)
LOW_SIDE = INTERNAL_H + WALL * 2             # ~23.6mm

# USB-C cutout
USBC_W = 12.0             # USB-C opening width
USBC_H = 7.0              # USB-C opening height

# Vent grille
VENT_SLOT_W = 2.0         # Individual vent slot width
VENT_SLOT_L = 30.0        # Vent slot length
VENT_SPACING = 4.0        # Center-to-center spacing
VENT_COUNT = 5            # Number of vent slots

# Anti-slip pad recess
PAD_DEPTH = 1.0           # Recess depth for silicone pad
PAD_INSET = 8.0           # Inset from edge

# Shell split
SPLIT_H_RATIO = 0.35      # Bottom shell is 35% of total height (display side)

# Snap-fit / screw
SCREW_D = 2.5             # M2.5 screw hole diameter
SCREW_BOSS_D = 6.0        # Screw boss outer diameter
SCREW_BOSS_H = 8.0        # Screw boss height

# Corner rounding
FILLET_R = 3.0            # Outer edge fillet radius
INNER_FILLET_R = 1.5      # Inner cavity fillet radius


# ============================================================
# HELPER: Wedge base shape
# ============================================================
def make_wedge_box(width, depth, h_front, h_back, fillet_r=0):
    """Create a wedge-shaped box.

    Args:
        width:   X dimension (left-right)
        depth:   Y dimension (windshield=front=-Y, driver=back=+Y)
        h_front: height at windshield side (Y=0)
        h_back:  height at driver side (Y=depth)
        fillet_r: corner fillet radius (0 = no fillet)
    """
    # Four corner points of the wedge profile (Y-Z plane, looking from +X)
    # Bottom-front, bottom-back, top-back, top-front
    with BuildPart() as wedge:
        with BuildSketch(Plane.XZ.offset(-depth / 2)) as profile:
            # 2D profile: trapezoid in Y-Z plane
            with BuildLine():
                l1 = Line((0, 0), (depth, 0))              # bottom edge
                l2 = Line((depth, 0), (depth, h_back))      # back (driver) edge
                l3 = Line((depth, h_back), (0, h_front))    # top (sloped) edge
                l4 = Line((0, h_front), (0, 0))             # front (windshield) edge
            make_face()
        extrude(amount=width, both=True)
        if fillet_r > 0:
            # Fillet the 4 long vertical edges
            vertical_edges = wedge.edges().filter_by(Axis.X)
            if vertical_edges:
                fillet(vertical_edges, radius=fillet_r)
    return wedge.part


# ============================================================
# BOTTOM SHELL (display side, sits on dashboard)
# ============================================================
def make_bottom_shell():
    """Bottom shell: holds the display, sits on dashboard."""
    split_h_front = LOW_SIDE * SPLIT_H_RATIO
    split_h_back = HIGH_SIDE * SPLIT_H_RATIO

    with BuildPart() as bottom:
        # Outer wedge
        outer = make_wedge_box(OUTER_W, OUTER_H, split_h_front, split_h_back, FILLET_R)
        add(outer)

        # Hollow out -- inner cavity
        inner_front = split_h_front - WALL
        inner_back = split_h_back - WALL
        inner = make_wedge_box(INNER_W, INNER_H, inner_front, inner_back)
        with Locations((0, 0, WALL)):
            add(inner, mode=Mode.SUBTRACT)

        # Display window cutout (through bottom wall)
        with BuildSketch(Plane.XY) as display_win:
            RectangleRounded(DISPLAY_ACTIVE_W, DISPLAY_ACTIVE_H, 1.0)
        extrude(amount=WALL, mode=Mode.SUBTRACT)

        # Display lip/ledge -- inset shelf to hold display panel
        with BuildSketch(Plane.XY.offset(WALL)) as display_shelf:
            RectangleRounded(DISPLAY_W + TOL, DISPLAY_H + TOL, 0.5)
            RectangleRounded(DISPLAY_ACTIVE_W - 1.0, DISPLAY_ACTIVE_H - 1.0, 0.5,
                             mode=Mode.SUBTRACT)
        extrude(amount=DISPLAY_T + 0.5, mode=Mode.SUBTRACT)

        # Anti-slip pad recesses on bottom face
        pad_w = OUTER_W - PAD_INSET * 2
        pad_h = OUTER_H - PAD_INSET * 2
        with BuildSketch(Plane.XY) as pad_recess:
            RectangleRounded(pad_w, pad_h, 2.0)
            RectangleRounded(pad_w - 6, pad_h - 6, 2.0, mode=Mode.SUBTRACT)
        extrude(amount=-PAD_DEPTH, mode=Mode.SUBTRACT)

        # Screw bosses at 4 corners (for top shell attachment)
        boss_inset = WALL + 3.0
        boss_positions = [
            (OUTER_W / 2 - boss_inset, OUTER_H / 2 - boss_inset),
            (-OUTER_W / 2 + boss_inset, OUTER_H / 2 - boss_inset),
            (OUTER_W / 2 - boss_inset, -OUTER_H / 2 + boss_inset),
            (-OUTER_W / 2 + boss_inset, -OUTER_H / 2 + boss_inset),
        ]
        for bx, by in boss_positions:
            with Locations((bx, by, WALL)):
                Cylinder(SCREW_BOSS_D / 2, split_h_back - WALL,
                         align=(Align.CENTER, Align.CENTER, Align.MIN))
                Hole(SCREW_D / 2, split_h_back - WALL)

    return bottom.part


# ============================================================
# TOP SHELL (component side, faces windshield)
# ============================================================
def make_top_shell():
    """Top shell: covers PCB, camera, GPS. Camera hole on driver side."""
    split_h_front = LOW_SIDE * (1 - SPLIT_H_RATIO)
    split_h_back = HIGH_SIDE * (1 - SPLIT_H_RATIO)

    with BuildPart() as top:
        # Outer wedge
        outer = make_wedge_box(OUTER_W, OUTER_H, split_h_front, split_h_back, FILLET_R)
        add(outer)

        # Hollow out
        inner_front = split_h_front - WALL
        inner_back = split_h_back - WALL
        inner = make_wedge_box(INNER_W, INNER_H, inner_front, inner_back)
        with Locations((0, 0, 0)):
            add(inner, mode=Mode.SUBTRACT)

        # Camera lens hole (driver side, +Y)
        cam_hole_y = OUTER_H / 2 - WALL - CAM_W / 2 - TOL
        cam_hole_z = split_h_back - WALL - CAM_T / 2
        with Locations((OUTER_W / 4, cam_hole_y, cam_hole_z)):
            Hole(CAM_LENS_D / 2 + 1.0, WALL + 2)

        # GPS window (thin wall for signal transparency, driver side)
        gps_x = -OUTER_W / 4
        gps_y = OUTER_H / 2 - WALL - GPS_W / 2 - TOL
        gps_z = split_h_back - WALL / 2
        with BuildSketch(Plane.XY.offset(split_h_back - WALL)) as gps_thin:
            with Locations((gps_x, gps_y)):
                Rectangle(GPS_H + 2, GPS_W + 2)
        # Thin out the wall above GPS to 0.8mm (RF transparent)
        extrude(amount=WALL - 0.8, mode=Mode.SUBTRACT)

        # USB-C cutout (back/driver side wall)
        usbc_z = split_h_back * 0.4
        with BuildSketch(Plane.XZ.offset(OUTER_H / 2)) as usbc:
            with Locations((0, usbc_z)):
                RectangleRounded(USBC_W, USBC_H, 1.5)
        extrude(amount=-WALL - 1, mode=Mode.SUBTRACT)

        # Ventilation slots (top surface, windshield side)
        vent_y = -OUTER_H / 4  # Windshield half
        vent_start_x = -(VENT_COUNT - 1) * VENT_SPACING / 2
        for i in range(VENT_COUNT):
            vx = vent_start_x + i * VENT_SPACING
            with BuildSketch(Plane.XY.offset(split_h_front - WALL)) as vent:
                with Locations((vx, vent_y)):
                    RectangleRounded(VENT_SLOT_W, VENT_SLOT_L, 0.5)
            extrude(amount=WALL + 1, mode=Mode.SUBTRACT)

        # Screw holes at 4 corners (matching bottom shell bosses)
        boss_inset = WALL + 3.0
        screw_positions = [
            (OUTER_W / 2 - boss_inset, OUTER_H / 2 - boss_inset),
            (-OUTER_W / 2 + boss_inset, OUTER_H / 2 - boss_inset),
            (OUTER_W / 2 - boss_inset, -OUTER_H / 2 + boss_inset),
            (-OUTER_W / 2 + boss_inset, -OUTER_H / 2 + boss_inset),
        ]
        for sx, sy in screw_positions:
            with Locations((sx, sy, 0)):
                Hole(SCREW_D / 2, WALL + 1)

    return top.part


# ============================================================
# ASSEMBLY (for visualization)
# ============================================================
def make_assembly():
    """Create exploded view for visualization."""
    bottom = make_bottom_shell()
    top = make_top_shell()

    split_h_back = HIGH_SIDE * SPLIT_H_RATIO

    # Position top shell above bottom (exploded by 15mm)
    top_moved = top.moved(Location((0, 0, split_h_back + 15)))

    return bottom, top_moved


# ============================================================
# EXPORT
# ============================================================
if __name__ == "__main__":
    import os

    out_dir = os.path.dirname(os.path.abspath(__file__))

    print("Building bottom shell...")
    bottom = make_bottom_shell()

    print("Building top shell...")
    top = make_top_shell()

    # Export STL for 3D printing
    bottom_stl = os.path.join(out_dir, "hud_bottom_shell.stl")
    top_stl = os.path.join(out_dir, "hud_top_shell.stl")
    export_stl(bottom, bottom_stl)
    export_stl(top, top_stl)
    print(f"Exported: {bottom_stl}")
    print(f"Exported: {top_stl}")

    # Export STEP for professional CAD interchange
    bottom_step = os.path.join(out_dir, "hud_bottom_shell.step")
    top_step = os.path.join(out_dir, "hud_top_shell.step")
    export_step(bottom, bottom_step)
    export_step(top, top_step)
    print(f"Exported: {bottom_step}")
    print(f"Exported: {top_step}")

    print("\nDone! Open hud_enclosure.py in VS Code with ocp-vscode for 3D preview.")
    print("Or import .step files into any CAD software for further editing.")
