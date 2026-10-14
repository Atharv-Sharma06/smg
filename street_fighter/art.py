"""Drawing code: colour helpers, the fighters (built from simple shapes) and the stage."""
import math
import random

import pygame

from settings import HEIGHT, WHITE, WIDTH

# Body part sizes in pixels at scale 1
THIGH = 30
SHIN = 30
TORSO = 46
UPPER_ARM = 24
FOREARM = 24
HEAD = 13

# Every pose is a dictionary of angles in degrees:
#   0 = the limb points straight down, 90 = forward, 180 = straight up, negative = backwards.
#   Arms and legs are (upper part, lower part) pairs. "lean" tilts the body forward.
POSES = {
    "idle": {"lean": 6, "front_arm": (35, 150), "back_arm": (15, 160),
             "front_leg": (24, 2), "back_leg": (-22, -6)},
    "jump": {"lean": 0, "front_arm": (60, 165), "back_arm": (30, 170),
             "front_leg": (85, -15), "back_leg": (35, -60)},
    "crouch": {"lean": 25, "front_arm": (60, 170), "back_arm": (50, 160),
               "front_leg": (85, -30), "back_leg": (-30, -95)},
    "punch": {"lean": 14, "front_arm": (90, 90), "back_arm": (20, 150),
              "front_leg": (32, 6), "back_leg": (-34, -14)},
    "kick": {"lean": -22, "front_arm": (40, 140), "back_arm": (-25, 110),
             "front_leg": (98, 96), "back_leg": (-8, -6)},
    "air_punch": {"lean": 10, "front_arm": (95, 95), "back_arm": (20, 150),
                  "front_leg": (85, -15), "back_leg": (35, -60)},
    "air_kick": {"lean": -12, "front_arm": (45, 150), "back_arm": (-20, 120),
                 "front_leg": (62, 64), "back_leg": (25, -85)},
    "cast": {"lean": 12, "front_arm": (86, 88), "back_arm": (80, 86),
             "front_leg": (36, 6), "back_leg": (-36, -12)},
    "uppercut": {"lean": 4, "front_arm": (170, 178), "back_arm": (25, 140),
                 "front_leg": (55, -25), "back_leg": (-10, -20)},
    "dash": {"lean": 38, "front_arm": (95, 92), "back_arm": (-70, -50),
             "front_leg": (55, 20), "back_leg": (-65, -45)},
    "hit": {"lean": -24, "front_arm": (-30, 20), "back_arm": (-50, 0),
            "front_leg": (18, 0), "back_leg": (-18, -4)},
    "ko": {"lean": -90, "front_arm": (95, 85), "back_arm": (-100, -80),
           "front_leg": (92, 88), "back_leg": (84, 95)},
    "victory": {"lean": 0, "front_arm": (130, 170), "back_arm": (-35, 60),
                "front_leg": (16, 0), "back_leg": (-16, 0)},
}


# ------------------------------------------------------------------ colours
def blend(color_a, color_b, amount):
    """Mix colour a towards colour b (amount 0 = all a, 1 = all b)."""
    return tuple(int(a + (b - a) * amount) for a, b in zip(color_a, color_b))


def shade(color, factor):
    """Darken (factor < 1) or brighten (factor > 1) a colour."""
    return tuple(max(0, min(255, int(c * factor))) for c in color)


def alternate_costume(look):
    """Lighter outfit for player 2 when both players pick the same fighter."""
    look = dict(look)
    look["gi"] = blend(look["gi"], (235, 235, 235), 0.65)
    return look


def draw_glow(surface, color, center, radius, alpha=90):
    """A soft see-through circle, used for auras, shields and fireballs."""
    radius = int(radius)
    if radius <= 0:
        return
    glow = pygame.Surface((radius * 2, radius * 2), pygame.SRCALPHA)
    for ring in range(3):
        r = int(radius * (1 - ring * 0.25))
        pygame.draw.circle(glow, (*color[:3], int(alpha * (0.35 + ring * 0.3))), (radius, radius), r)
    surface.blit(glow, (center[0] - radius, center[1] - radius))


# ------------------------------------------------------------------ poses
def lerp_pose(pose_a, pose_b, amount):
    """Blend between two poses so moves animate smoothly."""
    result = {}
    for key, a in pose_a.items():
        b = pose_b[key]
        if isinstance(a, tuple):
            result[key] = tuple(x + (y - x) * amount for x, y in zip(a, b))
        else:
            result[key] = a + (b - a) * amount
    return result


def idle_pose(t, phase=0.0):
    """Fighting stance that gently bobs up and down."""
    bob = math.sin(t * 0.1 + phase)
    base = POSES["idle"]
    return {
        "lean": base["lean"] + bob * 1.5,
        "front_arm": (base["front_arm"][0] + bob * 4, base["front_arm"][1] + bob * 3),
        "back_arm": (base["back_arm"][0] + bob * 4, base["back_arm"][1] + bob * 3),
        "front_leg": (base["front_leg"][0] + bob * 4, base["front_leg"][1] - bob * 4),
        "back_leg": (base["back_leg"][0] - bob * 4, base["back_leg"][1] + bob * 4),
    }


def walk_pose(cycle):
    swing = math.sin(cycle)
    return {
        "lean": 8,
        "front_arm": (35 - 8 * swing, 150),
        "back_arm": (15 + 8 * swing, 160),
        "front_leg": (8 + 26 * swing, -6 + 20 * swing),
        "back_leg": (8 - 26 * swing, -6 - 20 * swing),
    }


def victory_pose(t):
    pump = math.sin(t * 0.15)
    pose = dict(POSES["victory"])
    pose["front_arm"] = (130 + pump * 8, 170 + pump * 4)
    return pose


def _step(start, angle, length):
    rad = math.radians(angle)
    return (start[0] + math.sin(rad) * length, start[1] + math.cos(rad) * length)


def pose_joints(pose):
    """Work out where every joint is for a pose.

    Coordinates are local: +x is the way the fighter faces, +y is down, and
    everything is shifted so the lowest body part rests on y = 0.
    """
    lean = pose["lean"]
    hip = (0.0, 0.0)
    joints = {
        "hip": hip,
        "neck": _step(hip, 180 - lean, TORSO),
        "shoulder": _step(hip, 180 - lean, TORSO - 6),
        "head": _step(hip, 180 - lean, TORSO + HEAD + 2),
    }
    for side in ("front", "back"):
        upper, lower = pose[side + "_arm"]
        joints[side + "_elbow"] = _step(joints["shoulder"], upper, UPPER_ARM)
        joints[side + "_hand"] = _step(joints[side + "_elbow"], lower, FOREARM)
        thigh, shin = pose[side + "_leg"]
        joints[side + "_knee"] = _step(hip, thigh, THIGH)
        joints[side + "_foot"] = _step(joints[side + "_knee"], shin, SHIN)

    lowest = max(y for _, y in joints.values())
    lowest = max(lowest, joints["head"][1] + HEAD) + 5
    return {name: (x, y - lowest) for name, (x, y) in joints.items()}


# ------------------------------------------------------------------ fighter drawing
def _bone(surface, color, a, b, width_a, width_b=None):
    """A thick, rounded line from a to b (optionally thicker at one end)."""
    width_b = width_a if width_b is None else width_b
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / length, dx / length
    ha, hb = width_a / 2, width_b / 2
    points = [(a[0] + nx * ha, a[1] + ny * ha), (b[0] + nx * hb, b[1] + ny * hb),
              (b[0] - nx * hb, b[1] - ny * hb), (a[0] - nx * ha, a[1] - ny * ha)]
    pygame.draw.polygon(surface, color, points)
    pygame.draw.circle(surface, color, a, ha)
    pygame.draw.circle(surface, color, b, hb)


def draw_fighter(surface, x, y, facing, look, pose, scale=1.0, flash=False, tint=None, ko=False):
    """Draw a fighter whose feet are at (x, y).

    look   - a character dictionary (only the colours are used)
    pose   - a pose dictionary (see POSES)
    facing - 1 for right, -1 for left
    tint   - optional (colour, amount) to recolour the fighter, e.g. when frozen
    """
    joints = pose_joints(pose)
    s = scale

    def pt(name):
        jx, jy = joints[name]
        return (x + jx * facing * s, y + jy * s)

    gi, trim, skin, hair = look["gi"], look["trim"], look["skin"], look["hair"]
    if flash:
        gi = trim = skin = hair = WHITE
    elif tint:
        tint_color, amount = tint
        gi, trim, skin, hair = (blend(c, tint_color, amount) for c in (gi, trim, skin, hair))

    leg_w, arm_w = 13 * s, 10 * s
    hip, neck, shoulder = pt("hip"), pt("neck"), pt("shoulder")

    def leg(side, gi_color, skin_color):
        knee, foot = pt(side + "_knee"), pt(side + "_foot")
        _bone(surface, gi_color, hip, knee, leg_w)
        _bone(surface, gi_color, knee, foot, leg_w, leg_w * 0.85)
        _bone(surface, skin_color, foot, (foot[0] + 9 * s * facing, foot[1] + 1 * s), 7 * s)

    def arm(side, gi_color, skin_color, glove_color):
        elbow, hand = pt(side + "_elbow"), pt(side + "_hand")
        _bone(surface, gi_color, shoulder, elbow, arm_w * 1.1)
        _bone(surface, skin_color, elbow, hand, arm_w)
        pygame.draw.circle(surface, glove_color, hand, 7 * s)

    # back limbs are darker so the body looks a bit 3D
    leg("back", shade(gi, 0.7), shade(skin, 0.7))
    arm("back", shade(gi, 0.7), shade(skin, 0.7), shade(trim, 0.7))

    # torso (wider at the chest) with an open collar
    _bone(surface, gi, hip, neck, 22 * s, 30 * s)
    collar = (neck[0] + (hip[0] - neck[0]) * 0.18, neck[1] + (hip[1] - neck[1]) * 0.18)
    pygame.draw.circle(surface, skin, collar, 6 * s)

    leg("front", gi, skin)

    # belt, drawn across the torso just above the hips
    dx, dy = neck[0] - hip[0], neck[1] - hip[1]
    length = math.hypot(dx, dy) or 1.0
    ux, uy = dx / length, dy / length
    bx, by = hip[0] + ux * 7 * s, hip[1] + uy * 7 * s
    _bone(surface, trim, (bx - uy * 12 * s, by + ux * 12 * s), (bx + uy * 12 * s, by - ux * 12 * s), 6 * s)

    # head: hair, face, headband with flowing tails, eye
    hx, hy = pt("head")
    radius = HEAD * s
    pygame.draw.circle(surface, hair, (hx, hy), radius)
    pygame.draw.circle(surface, skin, (hx + 2 * s * facing, hy + 2.5 * s), radius - 1.5 * s)
    band_y = hy - 3 * s
    pygame.draw.line(surface, trim, (hx - radius * facing, band_y), (hx + (radius - 1) * facing, band_y),
                     max(2, int(4 * s)))
    flutter = math.sin(pygame.time.get_ticks() * 0.012 + x * 0.05) * 3 * s
    tail_x = hx - radius * facing
    for drop, reach in ((4, 13), (9, 10)):
        pygame.draw.line(surface, trim, (tail_x, band_y),
                         (tail_x - reach * s * facing, band_y + drop * s + flutter * reach / 13),
                         max(2, int(3 * s)))
    ex, ey = hx + 6 * s * facing, hy - 0.5 * s
    if ko:
        e = 2.5 * s
        pygame.draw.line(surface, (30, 30, 30), (ex - e, ey - e), (ex + e, ey + e), max(1, int(1.5 * s)))
        pygame.draw.line(surface, (30, 30, 30), (ex - e, ey + e), (ex + e, ey - e), max(1, int(1.5 * s)))
    else:
        pygame.draw.circle(surface, (25, 25, 25), (ex, ey), max(1.5, 2 * s))

    arm("front", gi, skin, trim)


# ------------------------------------------------------------------ stage
def make_stage(seed=7):
    """Paint the background once: a city street at sunset."""
    rng = random.Random(seed)
    stage = pygame.Surface((WIDTH, HEIGHT))
    horizon = 400
    sky_top, sky_bottom = (22, 16, 54), (250, 130, 90)

    def sky(y):
        return blend(sky_top, sky_bottom, max(0.0, min(1.0, y / horizon)) ** 1.4)

    for y in range(horizon + 60):
        pygame.draw.line(stage, sky(y), (0, y), (WIDTH, y))

    for _ in range(60):  # stars
        sx, sy = rng.randrange(WIDTH), rng.randrange(170)
        stage.set_at((sx, sy), blend((255, 255, 255), sky(sy), sy / 200))

    # striped retro sun
    sun_x, sun_y = 700, 300
    pygame.draw.circle(stage, (255, 215, 120), (sun_x, sun_y), 95)
    pygame.draw.circle(stage, (255, 180, 100), (sun_x, sun_y + 30), 70)
    for i in range(7):
        stripe_y = sun_y + 8 + i * 13
        pygame.draw.rect(stage, sky(stripe_y), (sun_x - 100, stripe_y, 200, 2 + i))

    # distant mountains
    points = [(0, horizon)]
    for mx in range(0, WIDTH + 120, 120):
        points.append((mx, horizon - rng.randint(70, 160)))
        points.append((mx + 60, horizon - rng.randint(30, 90)))
    points.append((WIDTH, horizon))
    pygame.draw.polygon(stage, (95, 50, 105), points)

    # city skyline with lit windows
    bx = -10
    while bx < WIDTH:
        width, height = rng.randint(45, 95), rng.randint(50, 180)
        top = horizon - height
        pygame.draw.rect(stage, (42, 26, 58), (bx, top, width, height + 60))
        for wy in range(top + 10, horizon + 40, 14):
            for wx in range(bx + 7, bx + width - 8, 12):
                if rng.random() < 0.3:
                    pygame.draw.rect(stage, (255, 205, 110), (wx, wy, 5, 7))
        bx += width + rng.randint(2, 14)

    # street floor with perspective lines
    floor_top = 440
    for y in range(floor_top, HEIGHT):
        t = (y - floor_top) / (HEIGHT - floor_top)
        pygame.draw.line(stage, blend((100, 72, 82), (45, 32, 42), t), (0, y), (WIDTH, y))
    vanish_x, vanish_y = WIDTH // 2, 330
    for i in range(-14, 15):
        base_x = WIDTH // 2 + i * 100
        k = (HEIGHT - floor_top) / (HEIGHT - vanish_y)
        top_x = base_x + (vanish_x - base_x) * k
        pygame.draw.line(stage, (115, 85, 95), (base_x, HEIGHT), (top_x, floor_top), 1)
    for k in range(1, 9):
        line_y = floor_top + int((HEIGHT - floor_top) * (k / 9) ** 1.6)
        pygame.draw.line(stage, (115, 85, 95), (0, line_y), (WIDTH, line_y), 1)
    pygame.draw.line(stage, (170, 115, 120), (0, floor_top), (WIDTH, floor_top), 3)
    return stage
