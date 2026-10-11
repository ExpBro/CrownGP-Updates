"""CROWN GP // NITRO EDITION

Original playable Ursina racing project. 4-car single-player race; 3 AI. Persistent coins in Windows AppData.
F6 developer control center, F7 free-look, N noclip, upgrades and detailed
procedural texturing. Run from any directory: python game/main.py.
"""
from __future__ import annotations

import json
import math
import os
import random
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ursina import *
from panda3d.core import ClockObject, AntialiasAttrib, Texture as PandaTexture
from direct.showbase.ShowBaseGlobal import globalClock

from track_math import Circuit, Vec2

ROOT = Path(__file__).resolve().parent.parent
TEXTURES = ROOT / 'assets' / 'textures'
random.seed(4106)

app = Ursina(borderless=False, development_mode=False, vsync=False)
window.title = 'CROWN GP // NITRO EDITION  |  4-car Championship'
window.exit_button.visible = False
window.fps_counter.enabled = True
window.color = color.rgb(119, 171, 219)
window.fullscreen = False
# Some Windows/Panda3D builds clear unused pixels to plain white. Give the
# underlying display region a deliberate sky color instead.
try:
    application.base.setBackgroundColor(.40, .64, .84, 1.)
except Exception:
    pass
# Ursina controls UI depth/sort order.  Do not globally disable depth-test:
# doing so can put buttons in front of their labels on some Panda3D versions.
# Disable world lighting/shaders on 2D UI, so gray stays gray and text stays white.
try:
    camera.ui.setLightOff(1)
    camera.ui.setShaderOff(1)
except (AttributeError, TypeError):
    pass
camera.fov = 74
try:
    application.base.render.setAntialias(AntialiasAttrib.MAuto)
except Exception:
    pass

TRACK = Circuit()
ROAD_HALF = 10.25
GRID_SIZE = 4
LAPS = 3
PALETTE = [
    color.rgb(34, 207, 244), color.rgb(255, 96, 81), color.rgb(251, 189, 56),
    color.rgb(165, 99, 247), color.rgb(81, 227, 147), color.rgb(247, 96, 180),
    color.rgb(244, 245, 250), color.rgb(55, 108, 240), color.rgb(248, 138, 57),
    color.rgb(102, 239, 209), color.rgb(209, 78, 81), color.rgb(194, 220, 57),
]
DRIVER_NAMES = ('YOU', 'BLAZE', 'RAVEN', 'APEX')


# --------------------------------------------------------------------------
# PERSISTENT COIN WALLET - Windows: %LOCALAPPDATA%\CrownGP\coins.json
# --------------------------------------------------------------------------

# Rewards are only granted when the player crosses the finish line. Restarting,
# leaving the race or wrecking cannot earn a placement payout.
PLACE_REWARDS = {1: 10, 2: 5, 3: 4, 4: 2}


def coin_save_location():
    """Save outside the downloaded game so updates cannot erase the wallet."""
    local = os.environ.get('LOCALAPPDATA')
    if local:
        base = Path(local)
    elif os.name == 'nt':
        base = Path.home() / 'AppData' / 'Local'
    else:
        base = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local' / 'share')))
    return base / 'CrownGP' / 'coins.json'


COIN_SAVE_FILE = coin_save_location()


def load_coin_wallet():
    """New installations start with zero; invalid saves never crash the game."""
    defaults = {'coins': 0, 'completed_races': 0, 'last_position': 0}
    try:
        data = json.loads(COIN_SAVE_FILE.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise ValueError('Coin save is not a JSON object')
        for key in defaults:
            n = data.get(key, defaults[key])
            if type(n) is not int or n < 0:
                raise ValueError(f'Invalid saved value: {key}')
            defaults[key] = n
        if defaults['last_position'] > GRID_SIZE:
            defaults['last_position'] = 0
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError) as exc:
        print(f'[CROWN GP] Could not read coin save ({exc}); starting at zero.')
    return defaults


coin_wallet = load_coin_wallet()
coin_paid_this_race = False


def save_coin_wallet():
    """Atomically replace the file to avoid partial JSON on sudden shutdown."""
    temporary = None
    try:
        COIN_SAVE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', dir=COIN_SAVE_FILE.parent,
            prefix='coins_', suffix='.tmp', delete=False
        ) as f:
            temporary = Path(f.name)
            json.dump(coin_wallet, f, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, COIN_SAVE_FILE)
        return True
    except OSError as exc:
        print(f'[CROWN GP] WARNING: Coins could not be saved to {COIN_SAVE_FILE}: {exc}')
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def award_race_coins(position):
    """Return (reward, save_succeeded). Repeated calls cannot pay twice."""
    global coin_paid_this_race
    if coin_paid_this_race or position not in PLACE_REWARDS:
        return 0, True
    coin_paid_this_race = True
    earned = PLACE_REWARDS[position]
    coin_wallet['coins'] += earned
    coin_wallet['completed_races'] += 1
    coin_wallet['last_position'] = position
    saved = save_coin_wallet()
    refresh_coin_labels()
    return earned, saved


def texture_file(name):
    """Load an actual bundled image, bypassing Ursina's name-based asset search.

    Ursina's load_texture() looks in cached asset folders and, on some
    Windows/Python combinations, returns None for absolute OS paths even when
    the PNG exists. The Texture(Path) constructor passes an absolute Filename
    directly to Panda3D's TexturePool. If a driver/decoder still cannot read
    the PNG, fall back to a Pillow-decoded in-memory texture.
    """
    path = (TEXTURES / (name + '.png')).resolve()
    if not path.is_file():
        raise FileNotFoundError(f'Missing bundled asset: {path}. Extract the WHOLE ZIP before running.')

    try:
        tex = Texture(path)  # not load_texture(str(path))
        if getattr(tex, '_texture', None) is None:
            raise ValueError('Panda3D could not decode the PNG directly')
    except (OSError, ValueError, TypeError, RuntimeError) as direct_error:
        try:
            from PIL import Image
            with Image.open(path) as image:
                tex = Texture(image.convert('RGBA'))
            if getattr(tex, '_texture', None) is None:
                raise ValueError('Pillow texture upload did not succeed')
        except Exception as fallback_error:
            raise RuntimeError(
                f'Unable to open texture {path.name}. '
                f'Direct load: {direct_error}; Pillow fallback: {fallback_error}'
            ) from fallback_error

    # Repeating UVs makes asphalt, shoulders, and curbs seamless.
    t = tex._texture
    t.setWrapU(PandaTexture.WMRepeat)
    t.setWrapV(PandaTexture.WMRepeat)
    t.setAnisotropicDegree(8)
    return tex


TEX = {k: texture_file(k) for k in (
    'asphalt', 'grass', 'gravel', 'concrete', 'carbon', 'glass',
    'kerb', 'checker', 'paint', 'rubber', 'metal', 'crowd', 'sponsor', 'shadow',
)}


# Daylight and soft ambience improve dimensional definition of textured cars.
sky_dome = Sky(color=color.rgb(138, 191, 231))
# Keep the sky blue even when scene lighting is bright.
sky_dome.double_sided = True
try:
    sky_dome.setLightOff(1)
    sky_dome.setShaderOff(1)
except (AttributeError, TypeError):
    pass
try:
    sunlight = DirectionalLight(shadows=False)
    sunlight.look_at(Vec3(1, -1.4, -.9))
    AmbientLight(color=color.rgba(135, 149, 169, 255))
except Exception:
    pass  # Keep game playable on graphics drivers without the light pipeline.


@dataclass
class Tweaks:
    top_speed: float = 44.0
    acceleration: float = 28.0
    steering: float = 94.0
    boost_force: float = 36.0
    ai_pace: float = 1.0
    time_scale: float = 1.0
    camera_roll: float = 8.0
    gravity_grip: float = 1.0  # traction and offroad slowdown
    fly_speed: float = 35.0
    no_clip: bool = False
    fly_car: bool = False
    god_mode: bool = False
    infinite_nitro: bool = False
    freeze_ai: bool = False
    ghost_cpu: bool = False
    hud_visible: bool = True
    collisions: bool = True


cfg = Tweaks()
DIFFICULTIES = ('Rookie', 'Sport', 'Pro')
QUALITIES = ('Performance', 'Balanced', 'Ultra')
FPS_CAPS = ('60', '120', '144', 'Unlimited')
difficulty_idx = 1
quality_idx = 1
fps_idx = 0
world_root = None
race_active = False
race_started = False
race_done = False
race_seconds = 0.
player_progress = -52.0
player_prev_s = 0.
player_seg_hint = None
camera_roll_now = 0.
free_camera = False
camera_mode = 0
admin_open = False
admin_tab = 0
impact_flash_time = 0.


# --------------------------------------------------------------------------
# ENGINE / TRACK RENDERING
# --------------------------------------------------------------------------

def clampf(n, lo, hi):
    return max(lo, min(hi, n))


def normalized_angle(a):
    return (a + 180.) % 360. - 180.


def make_mesh(vertices, triangles, uvs, up_normal=True):
    if up_normal:
        normals=[(0.,1.,0.)]*len(vertices)
    else:
        sums=[[0.,0.,0.] for _ in vertices]
        for ia,ib,ic in triangles:
            a,b,c=vertices[ia],vertices[ib],vertices[ic]
            u=[b[k]-a[k] for k in range(3)]
            v=[c[k]-a[k] for k in range(3)]
            n=(u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0])
            for idx in (ia,ib,ic):
                for k in range(3):sums[idx][k]+=n[k]
        normals=[]
        for n in sums:
            mag=max(.00001,math.sqrt(sum(x*x for x in n)))
            normals.append(tuple(x/mag for x in n))
    # Ursina's built-in Terrain generates upward-facing surfaces using the
    # opposite triangle winding to the original CROWN GP code. Reversing the
    # triangles fixes the invisible road/grass (backface culling). Only flip
    # the horizontal meshes; wheel/body/barrier normals are authored already.
    if up_normal:
        triangles = [(a, c, b) for a, b, c in triangles]
    return Mesh(vertices=vertices, triangles=triangles, uvs=uvs, normals=normals, mode='triangle')


def track_ribbon(offset_a, offset_b, y, tex_repeat=7.5, stretch_u=1., strip_step=1):
    """One textured ribbon mesh, not hundreds of overlapping cubes.

    offset_a / offset_b are signed meters from track center line.
    """
    vertices, triangles, uvs = [], [], []
    inds = list(range(0, TRACK.count, strip_step)) + [TRACK.count]
    for i in inds:
        p, _, n, _ = TRACK.sample(TRACK.cumulative[i] + .00001)
        a, b = p + n*offset_a, p + n*offset_b
        vertices.extend([(a.x, y, a.z), (b.x, y, b.z)])
        uvs.extend([(0, TRACK.cumulative[i]/tex_repeat),
                    (stretch_u, TRACK.cumulative[i]/tex_repeat)])
    size = len(inds)
    for i in range(size-1):
        j = i+1
        ia, ib, ja, jb = 2*i, 2*i+1, 2*j, 2*j+1
        # Correct winding for flat upper side when offset_a is the negative side.
        if offset_a < offset_b:
            triangles.extend([(ia, ja, ib), (ib, ja, jb)])
        else:
            triangles.extend([(ia, ib, ja), (ib, jb, ja)])
    return make_mesh(vertices, triangles, uvs)


def segmented_centerline(parent):
    verts, tris, uv = [], [], []
    distance = 0.0
    while distance < TRACK.length:
        end = min(distance+4.2, TRACK.length)
        a, _, na, _ = TRACK.sample(distance)
        b, _, nb, _ = TRACK.sample(end)
        pts = [a-na*.13, a+na*.13, b-na*.13, b+nb*.13]
        i = len(verts)
        verts.extend([(p.x, .134, p.z) for p in pts])
        uv.extend([(0, 0), (1, 0), (0, 1), (1, 1)])
        tris.extend([(i, i+2, i+1), (i+1, i+2, i+3)])
        distance += 12.3
    Entity(parent=parent, model=make_mesh(verts, tris, uv),
           texture=TEX['paint'], color=color.rgb(207, 217, 220), double_sided=True)


def vertical_barrier(offset, parent):
    verts, tris, uv = [], [], []
    step = 3
    inds = list(range(0, TRACK.count, step)) + [TRACK.count]
    for i in inds:
        p, _, n, _ = TRACK.sample(TRACK.cumulative[i] + .00001)
        p = p + n*offset
        verts.extend([(p.x, .15, p.z), (p.x, 1.22, p.z)])
        v = TRACK.cumulative[i] / 4.4
        uv.extend([(0, v), (1, v)])
    for i in range(len(inds)-1):
        j = i+1
        tris.extend([(2*i, 2*i+1, 2*j), (2*i+1, 2*j+1, 2*j)])
    Entity(parent=parent, model=make_mesh(verts, tris, uv, False),
           texture=TEX['kerb'], double_sided=True)


def object_box(parent, pos, scale, material='metal', tint=None, rotation=0):
    return Entity(parent=parent, model='cube', texture=TEX[material],
                  color=tint or color.white, position=pos, scale=scale, rotation_y=rotation)


def cone_mesh(sides=9):
    vertices=[(0,.5,0)]
    vertices += [(math.cos(i*2*math.pi/sides)*.5,-.5,math.sin(i*2*math.pi/sides)*.5)
                 for i in range(sides)]
    vertices.append((0,-.5,0))
    triangles=[]
    for i in range(sides):
        j=(i+1)%sides
        triangles.extend([(0,1+j,1+i),(sides+1,1+i,1+j)])
    m=make_mesh(vertices,triangles,[(.5,.5)]+[(i/sides,0) for i in range(sides)]+[(.5,.5)],False)
    return m

CONE=cone_mesh()


def cylinder_mesh(sides=16):
    """A portable cylindrical model: Ursina does not bundle a 'cylinder' model.

    Shared by every tree trunk and wheel, so there are no missing-model
    warnings and each instance still accepts its own material / texture.
    The cylinder's axis is Y; wheels rotate it sideways with rotation_z=90.
    """
    vertices, triangles, uvs = [], [], []
    # Independent side vertices give repeatable textures around the wheel.
    for i in range(sides + 1):
        a = 2 * math.pi * i / sides
        x, z = math.cos(a) * .5, math.sin(a) * .5
        vertices.extend([(x, -.5, z), (x, .5, z)])
        uvs.extend([(i / sides, 0.), (i / sides, 1.)])
    for i in range(sides):
        a = 2 * i
        triangles.extend([(a, a+1, a+2), (a+1, a+3, a+2)])
    # Top and bottom disks have dedicated UVs for proper metal wheel faces.
    for y, is_top in ((.5, True), (-.5, False)):
        center = len(vertices)
        vertices.append((0., y, 0.))
        uvs.append((.5, .5))
        ring = len(vertices)
        for i in range(sides):
            a = 2 * math.pi * i / sides
            x, z = math.cos(a) * .5, math.sin(a) * .5
            vertices.append((x, y, z))
            uvs.append((.5+x, .5+z))
        for i in range(sides):
            j = (i + 1) % sides
            if is_top:
                triangles.append((center, ring+j, ring+i))
            else:
                triangles.append((center, ring+i, ring+j))
    return make_mesh(vertices, triangles, uvs, up_normal=False)


CYLINDER = cylinder_mesh()


def plant_tree(x, z, height, parent):
    trunk = color.rgb(107, 87, 64)
    tree = Entity(parent=parent, position=(x, 0, z))
    Entity(parent=tree, model=CYLINDER, color=trunk, scale=(.58, height*.65, .58), y=height*.36)
    Entity(parent=tree, model=CONE, color=color.rgb(30, random.randint(78, 111), 74),
           scale=(height*.42, height*.92, height*.42), y=height*.87)
    Entity(parent=tree, model=CONE, color=color.rgb(29, random.randint(91, 125), 79),
           scale=(height*.35, height*.73, height*.35), y=height*1.18)


def pit_buildings(parent, quality):
    # Grandstand and low pit-building complex beyond the primary straight.
    object_box(parent, (-10, .03, -120), (285, .32, 18), 'concrete')
    for x in range(-123, 131, 22):
        object_box(parent, (x, 7.6, -134), (20, 14, 12), 'concrete', color.rgb(189, 201, 218))
        object_box(parent, (x, 15.0, -134), (22, .8, 14), 'carbon')
        object_box(parent, (x, 4.9, -127.8), (17.4, 6.7, .28), 'metal', color.rgb(56, 79, 97))
        for bx in (-6, 0, 6):
            object_box(parent, (x+bx, 11., -127.57), (4.7, 2.1, .3), 'glass')
    for gx in (-120, -42, 38, 117):
        # Stand framework and repeating stadium seating.
        object_box(parent, (gx, 4.3, 189), (69, 8.5, 16), 'concrete', color.rgb(96, 116, 141))
        object_box(parent, (gx, 8.65, 185.5), (73, .58, 21), 'metal', color.rgb(204, 218, 232))
        object_box(parent, (gx, 9.1, 180.2), (67, 3.7, .24), 'crowd')
        for row in range(4):
            object_box(parent, (gx, 3.4+row*.85, 180-row*2.3),
                       (66, .34, 2.6), 'carbon', color.rgb(126, 139, 161))


def make_overhead_banner(parent, distance, caption='CROWN GP'):
    p, t, n, _ = TRACK.sample(distance)
    heading = TRACK.heading(distance)
    for side in (-1, 1):
        q = p + n*(ROAD_HALF + 3.8)*side
        object_box(parent, (q.x, 3.7, q.z), (.65, 7.4, .65), 'metal')
    object_box(parent, (p.x, 7.4, p.z), (31, 2.2, 1.1), 'sponsor', rotation=heading)
    for side in (-1, 1):
        q = p + n*11.6*side
        Entity(parent=parent, model='sphere', color=color.azure,
               position=(q.x, 7.35, q.z), scale=.62)


def build_world():
    global world_root
    if world_root is not None:
        destroy(world_root)
    world_root = Entity(enabled=True)
    root = world_root
    q = quality_idx
    # Texture mapping on large terrain repeats the hi-res grass, never stretches.
    verts = [(-640,-.42,-450),(640,-.42,-450),(-640,-.42,470),(640,-.42,470)]
    tris = [(0,2,1),(1,2,3)]
    uvs = [(0,0),(52,0),(0,38),(52,38)]
    # Two-sided as a safety net for platforms with unusual graphics culling.
    # The corrected triangle winding ensures front-facing road surfaces too.
    Entity(parent=root, model=make_mesh(verts,tris,uvs), texture=TEX['grass'],
           double_sided=True, color=color.rgb(155, 203, 153))
    Entity(parent=root, model=track_ribbon(-16,16,-.12,8.),
           texture=TEX['gravel'], double_sided=True)
    Entity(parent=root, model=track_ribbon(-12.55,12.55,.025,7.),
           texture=TEX['concrete'], color=color.rgb(173,185,190),
           double_sided=True)
    Entity(parent=root, model=track_ribbon(-ROAD_HALF, ROAD_HALF,.093,9.3),
           texture=TEX['asphalt'], double_sided=True)
    for sign in (-1,1):
        if sign == 1:
            a,b = ROAD_HALF-.24, ROAD_HALF-.04
            ca,cb = ROAD_HALF+.08, ROAD_HALF+2.12
        else:
            a,b = -ROAD_HALF+.04,-ROAD_HALF+.24
            ca,cb = -ROAD_HALF-2.12,-ROAD_HALF-.08
        Entity(parent=root, model=track_ribbon(a,b,.127,5.,1.),
               texture=TEX['paint'], double_sided=True)
        Entity(parent=root, model=track_ribbon(ca,cb,.134,5.8,1.),
               texture=TEX['kerb'], double_sided=True)
        vertical_barrier(sign*(ROAD_HALF+5.75), root)
    segmented_centerline(root)
    # Checkered start finish line facing perpendicular to the driving direction.
    p, _, n, _ = TRACK.sample(TRACK.start_distance)
    start_tile = object_box(root, (p.x,.175,p.z), (ROAD_HALF*2,.055,3.6), 'checker',
                            rotation=TRACK.heading(TRACK.start_distance))
    make_overhead_banner(root, TRACK.start_distance - 22.)
    make_overhead_banner(root, TRACK.start_distance + TRACK.length*.49)
    # Safety catch fencing with intermittently placed stanchions.
    fence_spacing = 25 if q == 0 else 13.5 if q == 1 else 8.5
    s=0.
    while s < TRACK.length:
        p, _, n, _ = TRACK.sample(s)
        for side in (-1,1):
            pos = p+n*side*(ROAD_HALF+6.8)
            object_box(root, (pos.x, 1.65,pos.z), (.21,3.3,.21), 'metal',color.rgb(155,173,187))
        s+=fence_spacing
    # Structured scenery is positioned outside the road, never on the track.
    pit_buildings(root, q)
    forest_count = 52 if q==0 else 100 if q==1 else 160
    for i in range(forest_count):
        s = (i+.21*random.random())*TRACK.length/forest_count
        p, _, n, _ = TRACK.sample(s)
        side = 1 if i % 2 else -1
        radius = random.uniform(37.,75.)
        x,z = p.x+n.x*side*radius, p.z+n.z*side*radius
        # Keep the paddock and grandstand area clear.
        if -155 < x < 159 and z < -102 or -170 < x < 145 and z > 169:
            continue
        plant_tree(x,z,random.uniform(6,13.5),root)
    # Billboard islands: offset far enough from the road and vehicles.
    for i in range(16):
        s = (i+.3)*TRACK.length/16
        p, _, n, _ = TRACK.sample(s)
        side = -1 if i % 2 else 1
        at = p+n*side*29
        heading = TRACK.heading(s) + (180 if side < 0 else 0)
        object_box(root,(at.x,5,at.z),(11,4.2,.45),'sponsor',rotation=heading)
        for edge in (-4.5,4.5):
            foot=at+Vec2(math.cos(math.radians(heading))*edge,
                          -math.sin(math.radians(heading))*edge)
            object_box(root,(foot.x,2.15,foot.z),(.35,4.3,.35),'metal')
    # Scenic mountains on the horizon; non-colliding geometry.
    if q>0:
        for i in range(24 if q==1 else 38):
            a=2*math.pi*i/(24 if q==1 else 38)
            r=420+random.randrange(-30,55)
            h=random.randint(75,190)
            Entity(parent=root,model=CONE,position=(math.cos(a)*r,h*.34,math.sin(a)*r),
                   scale=(h*1.25,h*.72,h*1.25),color=color.rgb(85,110,129))
    return root


# --------------------------------------------------------------------------
# CARS / CONTINUOUS RACING AI
# --------------------------------------------------------------------------

def car_shell_mesh():
    """Low-poly aerodynamic shell with tapering front, curved roofline."""
    sections = [
        (-2.34,1.03,.50,.83),(-1.75,1.18,.49,.94),(-.90,1.18,.46,.96),
        (.16,1.10,.44,.94),(1.28,1.16,.43,.84),(2.24,.89,.38,.68),(2.63,.52,.38,.58),
    ]
    verts,uv,tris=[],[],[]
    for z,wide,low,high in sections:
        outline=[(-wide,low),(-wide*.93,high),(wide*.93,high),(wide,low)]
        for x,y in outline:
            verts.append((x,y,z)); uv.append(((x+1.3)/2.6,(z+2.5)/5.))
    for i in range(len(sections)-1):
        for face in range(3):
            a=i*4+face; b=a+1; c=a+4; d=c+1
            tris += [(a,c,b),(b,c,d)]
    # front and rear caps
    tris += [(0,1,2),(0,2,3)]
    k=(len(sections)-1)*4
    tris += [(k,k+2,k+1),(k,k+3,k+2)]
    return make_mesh(verts,tris,uv,False)


SHELL = car_shell_mesh()


class RaceCar(Entity):
    def __init__(self, name, number, body_color, is_player=False):
        super().__init__(model=None)
        self.driver = name
        self.number = number
        self.is_player = is_player
        self.speed = 0.
        self.health = 100.
        self.nitro = 100.
        self.wrecked = False
        self.finished = False
        self.finish_time = None
        self.crash_cooldown = 0.
        self.race_distance = 0.
        self.lane = 0.
        self.target_lane = 0.
        self.bonus_speed = random.uniform(-1.9,2.1)
        self.skill = random.uniform(.88,1.08)
        self.ai_personality = random.uniform(0.,10.)
        self.spin_pivots=[]
        self.car_paint=body_color
        self.shadow = Entity(parent=scene, model='quad', texture=TEX['shadow'],
                             rotation_x=-90, scale=(4.0,6.5),
                             color=color.rgba(19,25,34,135),
                             position=(0,.154,0), double_sided=True)
        # Explicit mesh instead of only untextured cubes.
        Entity(parent=self,model=SHELL,texture=TEX['paint'],color=body_color,double_sided=True)
        object_box(self,(0,.37,0),(2.1,.27,4.18),'carbon')
        object_box(self,(0,.82,2.28),(1.58,.18,.55),'carbon')  # front splitter
        object_box(self,(0,.88,-2.26),(2.48,.16,.75),'carbon')  # rear diffuser
        # Windshield + rear canopy, richly textured reflective teal glass.
        object_box(self,(0,1.22,-.20),(1.63,.52,1.75),'glass', color.rgb(158,211,236))
        object_box(self,(0,1.49,-.20),(1.38,.14,1.39),'carbon',color.rgb(44,51,67))
        object_box(self,(0,1.43,.49),(1.6,.13,.1),'metal',color.rgb(200,229,243))
        # Body stripes / intake / grille.
        object_box(self,(0,.966,1.35),(.27,.022,1.52),'paint',color.rgb(230,244,248))
        for x in (-.68,.68):
            object_box(self,(x,.55,2.42),(.42,.13,.08),'glass',color.rgb(226,247,252))
            object_box(self,(x,.70,-2.36),(.42,.13,.08),'paint',color.rgb(255,56,52))
            object_box(self,(x*1.2,1.02,-.4),(.20,.24,.33),'carbon')  # side mirrors
            object_box(self,(x*1.34,.49,0),(.17,.15,1.16),'carbon')  # rocker panel
        # Functional-looking racing wing.
        for x in (-.76,.76):
            object_box(self,(x,1.18,-1.98),(.13,.55,.18),'metal')
        object_box(self,(0,1.52,-2.05),(2.74,.17,.70),'carbon')
        for x in (-1.26,1.26):
            object_box(self,(x,1.54,-2.03),(.1,.34,.58),'paint',body_color)
        for wx in (-1.22,1.22):
            for wz in (-1.55,1.48):
                wheel_pivot = Entity(parent=self,position=(wx,.48,wz))
                Entity(parent=wheel_pivot,model=CYLINDER,texture=TEX['rubber'],
                       rotation_z=90,scale=(.94,.46,.94),color=color.rgb(88,90,92))
                rim_x = -.27 if wx<0 else .27
                Entity(parent=wheel_pivot,model=CYLINDER,texture=TEX['metal'],
                       position=(rim_x,0,0),rotation_z=90,scale=(.57,.08,.57))
                Entity(parent=wheel_pivot,model='sphere',texture=TEX['metal'],
                       position=(rim_x*1.21,0,0),scale=(.15,.15,.15))
                self.spin_pivots.append(wheel_pivot)
        if not is_player:
            # Visually distinctive racing number plate (visible from above).
            self.ai_offset_id = number

    def tick_wheels(self, dt):
        for wheel in self.spin_pivots:
            wheel.rotation_x -= self.speed * dt * 82.

    def damage(self, amount):
        if self.wrecked or (self.is_player and cfg.god_mode):
            return
        self.health = max(0.,self.health-amount)
        if self.health<=0.:
            self.wrecked=True
            self.speed=0.


# Four racers total: the player starts in position 4, behind three CPUs.
player = RaceCar('YOU', GRID_SIZE, PALETTE[0], is_player=True)
cpus = [RaceCar(DRIVER_NAMES[i+1], i+1, PALETTE[i+1])
        for i in range(GRID_SIZE-1)]
all_cars = [player]+cpus


def grid_position(slot):
    # slot 0 = pole, slot GRID_SIZE-1 = last. Two cars per row.
    row = slot//2
    lane = -3.55 if slot%2==0 else 3.55
    return -(9.0 + row*8.25), lane


def reset_car_to_grid(car, slot):
    dist,lane=grid_position(slot)
    car.race_distance=dist
    car.lane=lane
    car.target_lane=lane
    p=TRACK.offset_point(TRACK.start_distance+dist,lane)
    car.position=Vec3(p.x,.0,p.z)
    car.rotation_y=TRACK.heading(TRACK.start_distance+dist)
    car.rotation_z=0.
    car.speed=0.
    car.health=100.
    car.nitro=100.
    car.wrecked=False
    car.finished=False
    car.finish_time=None
    car.crash_cooldown=0.
    car.shadow.position=Vec3(car.x,.15,car.z)
    car.shadow.rotation_y=car.rotation_y


def reset_race():
    global race_started, race_done, race_seconds, player_progress, player_prev_s, player_seg_hint
    global camera_roll_now, impact_flash_time, coin_paid_this_race
    coin_paid_this_race = False
    for i,cpu in enumerate(cpus):
        reset_car_to_grid(cpu,i)
    reset_car_to_grid(player,GRID_SIZE-1)
    player_progress=player.race_distance
    player_prev_s=(TRACK.start_distance+player_progress)%TRACK.length
    player_seg_hint=None
    race_started=False
    race_done=False
    race_seconds=0.
    camera_roll_now=0.
    impact_flash_time=0.
    finish_text.text=''
    instructions.text='HOLD W TO START THE RACE'
    set_impact_flash(0)
    update_camera(force=True)


def update_player_projection(dt):
    global player_progress, player_prev_s, player_seg_hint
    s,lateral,d,index=TRACK.project(player.x,player.z,hint=player_seg_hint)
    if d>37:  # in no-clip flight, use a full scan for recovery
        s,lateral,d,index=TRACK.project(player.x,player.z)
    player_seg_hint=index
    delta=(s-player_prev_s+TRACK.length*.5)%TRACK.length-TRACK.length*.5
    if abs(delta)<max(12.,dt*170.):
        player_progress+=delta
    player_prev_s=s
    player.race_distance=player_progress
    return s,lateral,d


def drive_player(dt):
    global race_started
    if not race_active or admin_open or free_camera or player.wrecked or race_done:
        return
    gas=held_keys['w'] or held_keys['up arrow']
    brake=held_keys['s'] or held_keys['down arrow']
    steer=float(bool(held_keys['d'] or held_keys['right arrow']))-float(bool(held_keys['a'] or held_keys['left arrow']))
    nitro=bool(held_keys['space'])
    if gas:
        race_started=True
        player.speed+=cfg.acceleration*dt
    elif brake:
        player.speed-= (42. if player.speed>0 else 16.)*dt
    else:
        player.speed-=math.copysign(min(abs(player.speed),11.*dt),player.speed)
    maxspd=cfg.top_speed
    if nitro and (player.nitro>0 or cfg.infinite_nitro) and player.speed>5.:
        player.speed+=cfg.boost_force*dt
        maxspd=cfg.top_speed*1.52
        if not cfg.infinite_nitro:
            player.nitro=max(0.,player.nitro-34*dt)
    else:
        player.nitro=min(100.,player.nitro+11.5*dt)
    player.speed=clampf(player.speed,-18.,maxspd)
    move_steer = min(1.,abs(player.speed)/13.)
    player.rotation_y += steer * cfg.steering * dt * move_steer * (1 if player.speed>=0 else -1)
    player.position += player.forward*player.speed*dt
    if cfg.fly_car:
        climb=float(bool(held_keys['e']))-float(bool(held_keys['q']))
        player.y+=climb*cfg.fly_speed*dt
        player.y=max(.0,player.y)
    elif player.y>0:
        player.y=max(0.,player.y-22*dt)
    player.rotation_z=lerp(player.rotation_z,-steer*min(6.,abs(player.speed)*.13),min(1,dt*5.))
    player.tick_wheels(dt)


def update_ai(dt):
    if not race_started or race_done or cfg.freeze_ai or admin_open:
        return
    diffmult=(.74,1.0,1.16)[difficulty_idx]
    for cpu in cpus:
        if cpu.finished or cpu.wrecked:
            continue
        absolute_s=TRACK.start_distance+cpu.race_distance
        corner=TRACK.turn_strength(absolute_s+12.,16.)
        curve_factor=clampf(1.-corner*2.9,.68,1.0)
        target=(38.5*cpu.skill+cpu.bonus_speed)*diffmult*cfg.ai_pace*curve_factor
        if cpu.speed<target:
            cpu.speed=min(target,cpu.speed+24*dt)
        else:
            cpu.speed=max(target,cpu.speed-29*dt)
        cpu.race_distance+=cpu.speed*dt
        # Lane changes on straights when catching slower traffic.
        nearest=None
        for other in cpus:
            if other is cpu or other.finished:
                continue
            gap=other.race_distance-cpu.race_distance
            if 1.5<gap<15 and abs(other.lane-cpu.lane)<3.5 and other.speed<cpu.speed:
                nearest=other
                break
        if nearest and corner<.055:
            cpu.target_lane=-4.5 if nearest.lane>0 else 4.5
        elif not nearest and abs(cpu.target_lane)>5.2:
            cpu.target_lane=0.
        cpu.lane=lerp(cpu.lane,cpu.target_lane,min(1.,dt*1.1))
        forward_s=TRACK.start_distance+cpu.race_distance
        pos=TRACK.offset_point(forward_s,cpu.lane)
        cpu.position=Vec3(pos.x,0,pos.z)
        cpu.rotation_y=lerp_angle(cpu.rotation_y,TRACK.heading(forward_s+2),min(1,dt*12))
        cpu.tick_wheels(dt)
        if cpu.race_distance>=TRACK.length*LAPS:
            cpu.finished=True
            cpu.finish_time=race_seconds


def set_impact_flash(alpha):
    impact_screen.color=color.rgba(239,50,65,int(alpha))


def impact(amount):
    global impact_flash_time
    if player.crash_cooldown>0:
        return
    player.damage(amount)
    player.crash_cooldown=.65
    player.speed *= -.24
    impact_flash_time=.22
    set_impact_flash(68)


def handle_collision(s,lateral,d,dt):
    if player.crash_cooldown>0:
        player.crash_cooldown=max(0.,player.crash_cooldown-dt)
    if not cfg.collisions or cfg.no_clip or cfg.fly_car or player.y>1.5 or player.wrecked:
        return
    if abs(lateral)>ROAD_HALF-1.5:
        player.speed*= max(.73,1.-cfg.gravity_grip*dt*.85)
    if abs(lateral)>ROAD_HALF+4.75:
        center,norm=TRACK.sample(s)[0],TRACK.sample(s)[2]
        allowed=ROAD_HALF+4.1
        pos=center+norm*(allowed if lateral>0 else -allowed)
        player.x=pos.x
        player.z=pos.z
        impact(5.+min(20.,abs(player.speed)*.29))
    if cfg.ghost_cpu:
        return
    for cpu in cpus:
        if cpu.wrecked: continue
        dx=player.x-cpu.x; dz=player.z-cpu.z
        distance=math.hypot(dx,dz)
        if distance<2.65 and player.crash_cooldown<=0:
            mag=max(distance,.03)
            amount=4.2-distance
            player.x+=dx/mag*amount*.65
            player.z+=dz/mag*amount*.65
            cpu.speed*=.83
            impact(7.+abs(player.speed-cpu.speed)*.25)
            break


def standings():
    return sorted(all_cars,key=lambda c:(c.race_distance if not c.finished else TRACK.length*LAPS+100000-c.finish_time),reverse=True)


def check_finish():
    global race_done
    if race_done or not race_started:
        return
    if player.wrecked:
        race_done=True
        finish_text.text='CAR WRECKED  |  PRESS R TO RESTART'
        return
    if player_progress>=TRACK.length*LAPS:
        player.finished=True
        player.finish_time=race_seconds
        race_done=True
        rank=standings().index(player)+1
        earned, saved = award_race_coins(rank)
        save_note = '' if saved else '  (SAVE FAILED - CHECK DISK PERMISSIONS)'
        finish_text.text=(
            f'FINISHED  P{rank:02d} / {GRID_SIZE}  |  {race_seconds:06.2f}s\n'
            f'+{earned} COINS  |  TOTAL: {coin_wallet["coins"]}{save_note}\n'
            'PRESS R TO RACE AGAIN'
        )



# Standalone gray pixel textures for the UI. These are generated at startup,
# so updating main.py is sufficient: no replacement PNGs are needed.
def ui_solid_texture(rgb, border_rgb):
    from PIL import Image, ImageDraw
    img = Image.new('RGBA', (64, 32), (*rgb, 255))
    pen = ImageDraw.Draw(img)
    pen.rectangle((0, 0, 63, 31), outline=(*border_rgb, 255), width=2)
    result = Texture(img)
    if getattr(result, '_texture', None) is None:
        raise RuntimeError('Failed to create UI texture')
    return result


UI_BUTTON_TEXTURE = ui_solid_texture((82, 82, 82), (133, 133, 133))
UI_PANEL_TEXTURE = ui_solid_texture((17, 28, 44), (31, 50, 66))

# --------------------------------------------------------------------------
# CAMERAS / HUD
# --------------------------------------------------------------------------

def update_camera(force=False, dt=0.016):
    global camera_roll_now
    if free_camera and not force:
        return
    if camera_mode==1:  # bumper cam
        at=player.position + player.forward*.85+Vec3(0,1.32,0)
        target=player.position+player.forward*12+Vec3(0,1.5,0)
        fov=91
    elif camera_mode==2:  # tactical overhead
        at=player.position-player.forward*7+Vec3(0,34,0)
        target=player.position+player.forward*12
        fov=73
    else:
        speed_distance=clampf(abs(player.speed)*.038,0,2.3)
        at=player.position-player.forward*(12.2+speed_distance)+Vec3(0,5.75+speed_distance*.2,0)
        target=player.position+player.forward*7+Vec3(0,1.18,0)
        fov=75+min(12,abs(player.speed)*.15)
    camera.position = at if force else lerp(camera.position,at,min(1.,dt*5.8))
    camera.look_at(target)
    if camera_mode!=2:
        steer=float(bool(held_keys['a'] or held_keys['left arrow']))-float(bool(held_keys['d'] or held_keys['right arrow']))
        camera_roll_now=lerp(camera_roll_now,steer*cfg.camera_roll*min(1,abs(player.speed)/17.),min(1,dt*4.))
        camera.rotation_z=camera_roll_now
    camera.fov=lerp(camera.fov,fov,min(1,dt*2.5))


def move_freecam(dt):
    if admin_open:
        return
    try:
        camera.rotation_y += mouse.velocity[0]*105
        camera.rotation_x = clampf(camera.rotation_x-mouse.velocity[1]*105,-86,86)
    except Exception:
        pass
    vel=Vec3(0,0,0)
    if held_keys['w']: vel+=camera.forward
    if held_keys['s']: vel-=camera.forward
    if held_keys['a']: vel-=camera.right
    if held_keys['d']: vel+=camera.right
    if held_keys['e']: vel+=Vec3(0,1,0)
    if held_keys['q']: vel-=Vec3(0,1,0)
    if vel.length()>0.01:
        camera.position+=vel.normalized()*cfg.fly_speed*(2.5 if held_keys['shift'] else 1.)*dt


def panel_bg(x, y, w, h, opacity=180, parent=None):
    # Textured panel = no unexplained WHITE quadrilaterals on Windows.
    panel = Entity(parent=parent if parent is not None else camera.ui,
                   model='quad', texture=UI_PANEL_TEXTURE, color=color.white,
                   position=(x, y, .20), scale=(w, h),
                   double_sided=True)
    try:
        panel.setLightOff(1)
        panel.setShaderOff(1)
    except (AttributeError, TypeError):
        pass
    return panel


def ui_text(text,x,y,scale=1.,shade=None,origin=(-.5,0),parent=None):
    return Text(parent=parent if parent is not None else camera.ui,
                text=text,position=(x,y,-.16),scale=scale,
                color=shade if shade is not None else color.rgb(231,240,252),
                origin=origin)


hud=Entity(parent=camera.ui)
# Keep the HUD readable without big background quads covering the track.
# (All speed, rank, lap and nitro text still works.)
rank_label=ui_text(f'P{GRID_SIZE} / {GRID_SIZE}',-.855,.434,1.6,color.rgb(54,223,249),parent=hud)
lap_label=ui_text('LAP 1 / 3',-.85,.367,.94,parent=hud)
speed_label=ui_text('000',.43,.429,1.65,color.rgb(254,255,255),parent=hud)
ui_text('KM/H',.69,.429,.65,color.rgb(131,212,239),parent=hud)
clock_label=ui_text('00:00.00',0,.441,1.0,origin=(0,0),parent=hud)
coin_hud_label=ui_text(f'COINS  {coin_wallet["coins"]}',0,.389,.82,
                       color.rgb(255,214,91),origin=(0,0),parent=hud)
boost_label=ui_text('NITRO',.36,-.43,.82,color.rgb(95,222,247),parent=hud)
health_label=ui_text('INTEGRITY',-.85,-.43,.79,parent=hud)
boost_bar_bg=Entity(parent=hud,model='quad',color=color.rgb(29,53,65),position=(.71,-.442,-.08),scale=(.23,.02))
boost_bar=Entity(parent=hud,model='quad',color=color.rgb(26,211,242),position=(.71,-.442,-.09),scale=(.23,.02))
health_bar=Entity(parent=hud,model='quad',color=color.rgb(106,234,173),position=(-.49,-.442,-.09),scale=(.115,.018))
instructions=ui_text('HOLD W TO START THE RACE',0,.18,1.65,
                     color.rgb(235,245,249),origin=(0,0),parent=hud)
finish_text=ui_text('',0,.12,1.45,color.rgb(230,246,252),origin=(0,0),parent=hud)
small_controls=ui_text('WASD DRIVE  |  SPACE NITRO  |  F6 ADMIN  |  F7 FREECAM  |  N NOCLIP',
                       0,-.483,.70,color.rgb(202,221,237),origin=(0,0),parent=hud)
impact_screen=Entity(parent=hud,model='quad',scale=(2.2,1.25),z=.06,
                     color=color.rgba(240,40,50,0))

# Track diagram in a real-time minimap, with one dot for each of 4 racers.
mini_root=Entity(parent=hud)
map_back=Entity(parent=mini_root,model='quad',texture=TEX['carbon'],
                color=color.rgb(92,111,132),position=(.708,.222,.01),scale=(.305,.186))
map_dots=[]
# Track centerline preview drawn using very small UI quads.
for i in range(0,TRACK.count,3):
    a=TRACK.points[i]
    x=.708+a.x*.00066
    y=.222+a.z*.00061
    Entity(parent=mini_root,model='quad',color=color.rgba(218,233,241,185),
           position=(x,y,-.08),scale=(.0035,.0035))
for car in all_cars:
    ent=Entity(parent=mini_root,model='quad',color=color.rgb(30,230,245) if car.is_player else color.rgb(248,124,91),
               position=(.708,.222,-.12),scale=.011 if car.is_player else .007)
    map_dots.append(ent)


def update_hud():
    order=standings()
    pos=order.index(player)+1
    rank_label.text=f'P{pos:02d} / {GRID_SIZE}'
    lap_label.text=f'LAP {min(LAPS,max(1,math.floor(player_progress/TRACK.length)+1))} / {LAPS}'
    speed_label.text=f'{int(abs(player.speed)*6.2):03d}'
    mins,secs=divmod(race_seconds,60)
    clock_label.text=f'{int(mins):02d}:{secs:05.2f}'
    boost_bar.scale_x=max(.001,.23*player.nitro/100)
    boost_bar.x=.595+boost_bar.scale_x*.5
    health_bar.scale_x=max(.001,.115*player.health/100)
    health_bar.x=-.5475+health_bar.scale_x*.5
    if player.health<32:health_bar.color=color.rgb(244,85,82)
    else:health_bar.color=color.rgb(106,234,173)
    for car,dot in zip(all_cars,map_dots):
        dot.position=(.708+car.x*.00066,.222+car.z*.00061,-.12)
    hud.enabled=cfg.hud_visible and race_active and not admin_open


# --------------------------------------------------------------------------
# MENUS / INTERACTIVE ADMIN CONSOLE
# --------------------------------------------------------------------------

def make_button(text,x,y,w=.25,h=.07,callback=None,parent=None,tint=None):
    # Some Ursina releases construct Button.text_entity only if initial text is
    # truthy. Passing text='' together with text_color makes their setter do
    # None.color = ..., crashing before the main menu even appears.
    # A space is visually blank but forces creation of the text entity.
    b=Button(parent=parent if parent is not None else camera.ui,
             text=text if text else ' ', position=(x,y,-.13),
             scale=(w,h), texture=UI_BUTTON_TEXTURE, color=color.white,
             highlight_color=color.rgb(215,215,215),
             pressed_color=color.rgb(170,170,170))
    # Force unlit gray texture, never the driver-dependent default white quad.
    try:
        b.setLightOff(1)
        b.setShaderOff(1)
    except (AttributeError, TypeError):
        pass
    # The child label must be in FRONT of the button's quad (negative Z).
    if getattr(b, 'text_entity', None) is not None:
        b.text_entity.color=color.white
        b.text_entity.z=-.08
        try:
            b.text_entity.setLightOff(1)
        except (AttributeError, TypeError):
            pass
    if callback is not None:
        b.on_click=callback
    return b


menu_root=Entity(parent=camera.ui)
# The original full-size menu backdrop was a white rectangle on this PC.
# Leave the track visible behind the gray buttons instead.
# panel_bg(0,0,1.06,.83,229,menu_root)
ui_text('C R O W N   G P',0,.312,2.35,color.rgb(62,225,246),origin=(0,0),parent=menu_root)
ui_text('N I T R O   E D I T I O N',0,.241,1.12,origin=(0,0),parent=menu_root)
ui_text(f'{GRID_SIZE} DRIVERS   |   {GRID_SIZE-1} CPUs   |   {LAPS} LAPS',0,.171,.93,
        color.rgb(186,210,225),origin=(0,0),parent=menu_root)
coin_menu_label=ui_text(f'COINS  {coin_wallet["coins"]}',0,.125,.89,
                        color.rgb(255,214,91),origin=(0,0),parent=menu_root)


def refresh_coin_labels():
    # Synchronize the menu balance and in-race coin counter.
    coin_menu_label.text = f'COINS  {coin_wallet["coins"]}'
    coin_hud_label.text = f'COINS  {coin_wallet["coins"]}'

# Give these buttons their actual labels at construction instead of a
# temporary whitespace string. This avoids a needless text-state transition.
menu_difficulty = make_button(f'DIFFICULTY  <  {DIFFICULTIES[difficulty_idx].upper()}  >',
                              0,.072,.44,.065,parent=menu_root)
menu_quality = make_button(f'GRAPHICS  <  {QUALITIES[quality_idx].upper()}  >',
                           0,-.012,.44,.065,parent=menu_root)
menu_fps = make_button(f'FPS LIMIT  <  {FPS_CAPS[fps_idx]}  >',
                       0,-.096,.44,.065,parent=menu_root)
start_btn=make_button('RACE NOW',0,-.196,.45,.075,parent=menu_root,tint=color.rgb(12,156,175))
quit_btn=make_button('EXIT',0,-.286,.45,.060,parent=menu_root,tint=color.rgb(101,43,63))
ui_text('F6 OPENS DEV TOOLS  |  ESC RETURNS TO MENU',0,-.375,.75,
        color.rgb(168,191,205),origin=(0,0),parent=menu_root)


def set_fps_cap():
    target=FPS_CAPS[fps_idx]
    if target=='Unlimited':
        globalClock.setMode(ClockObject.MNormal)
    else:
        globalClock.setMode(ClockObject.MLimited)
        globalClock.setFrameRate(float(target))


def menu_labels():
    menu_difficulty.text=f'DIFFICULTY  <  {DIFFICULTIES[difficulty_idx].upper()}  >'
    menu_quality.text=f'GRAPHICS  <  {QUALITIES[quality_idx].upper()}  >'
    menu_fps.text=f'FPS LIMIT  <  {FPS_CAPS[fps_idx]}  >'


def change_menu_setting(which):
    global difficulty_idx, quality_idx, fps_idx
    if which=='difficulty':
        difficulty_idx=(difficulty_idx+1)%len(DIFFICULTIES)
    elif which=='quality':
        quality_idx=(quality_idx+1)%len(QUALITIES)
        build_world()
    else:
        fps_idx=(fps_idx+1)%len(FPS_CAPS)
        set_fps_cap()
    menu_labels()

# Handle these three settings via menu hitboxes in input(). Their native
# callbacks are deliberately no-ops to prevent double changes on mouse-up.
# This also works when a platform's UI collider stops receiving hover events.
menu_difficulty.on_click=lambda:None
menu_quality.on_click=lambda:None
menu_fps.on_click=lambda:None
quit_btn.on_click=application.quit
menu_labels()


def activate_menu_setting_at(mouse_x, mouse_y):
    """Change exactly one menu setting when a click hits its visible button.

    Called on mouse-down independently of Ursina hover/collision. Each button
    uses UI-coordinate bounds, matching its center and scale in camera.ui.
    """
    for button, name in ((menu_difficulty, 'difficulty'),
                         (menu_quality, 'quality'),
                         (menu_fps, 'fps')):
        if (abs(mouse_x - button.x) <= button.scale_x * .5 and
                abs(mouse_y - button.y) <= button.scale_y * .5):
            change_menu_setting(name)
            print(f'[CROWN GP] Changed menu setting: {name}')
            return True
    return False

admin_root=Entity(parent=camera.ui,enabled=False)
panel_bg(0,0,1.12,.925,241,admin_root)
ui_text('DEVELOPER CONTROL CENTER',-.51,.407,1.28,color.rgb(66,225,242),parent=admin_root)
ui_text('F6 TO CLOSE   |   LIVE TUNING',.505,.405,.67,color.rgb(149,183,201),origin=(.5,0),parent=admin_root)
ui_text('Single-player sandbox controls. Values apply immediately.',-.51,.352,.68,
        color.rgb(160,190,206),parent=admin_root)
admin_tabs=[]
admin_pages=[]


def show_tab(tab):
    global admin_tab
    admin_tab=tab
    for i,page in enumerate(admin_pages):
        page.enabled=(i==tab)
    for i,button in enumerate(admin_tabs):
        button.color=color.white if i==tab else color.rgb(205,205,205)


for n,name in enumerate(('PERFORMANCE','CHEATS / FREECLIP','WORLD / TOOLS')):
    admin_tabs.append(make_button(name,(-.346+n*.348),.278,.326,.063,
                                  callback=lambda i=n:show_tab(i),parent=admin_root))
    admin_pages.append(Entity(parent=admin_root,enabled=(n==0)))

param_specs = (
    ('top_speed','TOP SPEED',20.,170.,5.,'m/s'),
    ('acceleration','ACCELERATION',5.,120.,5.,'m/s2'),
    ('steering','STEERING',25.,245.,10.,'deg/s'),
    ('boost_force','NITRO FORCE',0.,130.,5.,'boost'),
    ('ai_pace','CPU SPEED',.1,2.8,.1,'x'),
    ('time_scale','GAME SPEED',.2,3.0,.2,'x'),
    ('fly_speed','FREE-FLY SPEED',5.,160.,5.,'m/s'),
)
param_labels={}


def update_param_label(key):
    label=param_labels.get(key)
    if label:
        unit=next(spec[5] for spec in param_specs if spec[0]==key)
        val=getattr(cfg,key)
        label.text=f'{val:.1f} {unit}' if unit=='x' else f'{val:.0f} {unit}'


def change_param(key,change,minimum,maximum):
    setattr(cfg,key,round(clampf(getattr(cfg,key)+change,minimum,maximum),3))
    update_param_label(key)


for i,(key,name,minv,maxv,step,unit) in enumerate(param_specs):
    y=.183-i*.075
    ui_text(name,-.49,y,.79,parent=admin_pages[0])
    param_labels[key]=ui_text('',.138,y,.79,color.rgb(139,229,237),parent=admin_pages[0])
    make_button('-',.335,y,.065,.054,callback=lambda k=key,s=step,a=minv,b=maxv:change_param(k,-s,a,b),parent=admin_pages[0])
    make_button('+',.425,y,.065,.054,callback=lambda k=key,s=step,a=minv,b=maxv:change_param(k,s,a,b),parent=admin_pages[0])
    update_param_label(key)


bool_specs = (
    ('no_clip','NOCLIP / THROUGH WALLS','N bypasses road and vehicle collisions'),
    ('fly_car','FLYING CAR','Hold Q to descend / E to climb'),
    ('god_mode','INVINCIBILITY','No damage from impacts'),
    ('infinite_nitro','INFINITE NITRO','Never drains while boosting'),
    ('freeze_ai','PAUSE ALL 3 CPUs','Hold opponents at current positions'),
    ('ghost_cpu','GHOST OPPONENTS','Player passes through CPU cars'),
)
toggle_buttons={}


def refresh_toggles():
    for key,btn in toggle_buttons.items():
        state=getattr(cfg,key)
        btn.text='ON' if state else 'OFF'
        btn.color=color.white if state else color.rgb(185,185,185)


def toggle_cfg(key):
    setattr(cfg,key,not getattr(cfg,key))
    if key=='fly_car' and cfg.fly_car:
        cfg.no_clip=True
    refresh_toggles()


for i,(key,label,desc) in enumerate(bool_specs):
    y=.184-i*.080
    ui_text(label,-.492,y,.85,parent=admin_pages[1])
    ui_text(desc,-.492,y-.027,.58,color.rgb(145,175,194),parent=admin_pages[1])
    toggle_buttons[key]=make_button('',.396,y-.007,.139,.055,
                                     callback=lambda k=key:toggle_cfg(k),parent=admin_pages[1])
refresh_toggles()


def toggle_freecam():
    global free_camera
    free_camera=not free_camera
    if not free_camera:
        update_camera(force=True)
    mouse.locked=free_camera and not admin_open
    mouse.visible=not mouse.locked
    tools_feedback.text='FREECAM ON - F7 TO RETURN' if free_camera else 'CHASE CAMERA RESTORED'


def teleport_track(metres):
    global player_progress,player_prev_s,player_seg_hint
    player_progress+=metres
    at_s=TRACK.start_distance+player_progress
    p=TRACK.offset_point(at_s,0.)
    player.position=Vec3(p.x,0.,p.z)
    player.rotation_y=TRACK.heading(at_s)
    player_prev_s=at_s%TRACK.length
    player_seg_hint=None
    player.race_distance=player_progress
    tools_feedback.text=f'TELEPORTED {metres:+.0f} METERS'


def repair_car():
    player.health=100.
    player.wrecked=False
    player.speed=0.
    player.nitro=100.
    tools_feedback.text='CAR FULLY REPAIRED AND REFUELED'


def reset_dev_settings():
    global cfg
    cfg=Tweaks()
    for key,*_ in param_specs:update_param_label(key)
    refresh_toggles()
    tools_feedback.text='DEFAULT SETTINGS RESTORED'


def cycle_camera():
    global camera_mode
    camera_mode=(camera_mode+1)%3
    tools_feedback.text=('CHASE CAM','BUMPER CAM','AERIAL CAM')[camera_mode]

world_buttons=(
    ('REPAIR / REFUEL',repair_car),
    ('CAMERA MODE (C)',cycle_camera),
    ('FREE CAMERA (F7)',toggle_freecam),
    ('TELEPORT +200 m',lambda:teleport_track(200)),
    ('TELEPORT -100 m',lambda:teleport_track(-100)),
    ('RESTART RACE (R)',reset_race),
    ('RESET ALL DEV SETTINGS',reset_dev_settings),
)
for i,(name,action) in enumerate(world_buttons):
    y=.181-i*.07
    make_button(name,-.19,y,.59,.058,callback=action,parent=admin_pages[2])
tools_feedback=ui_text('F6 toggles panel  |  F7 freecam  |  N noclip',0,-.355,.70,
                       color.rgb(124,218,231),origin=(0,0),parent=admin_pages[2])


def toggle_admin():
    global admin_open
    admin_open=not admin_open
    admin_root.enabled=admin_open
    if admin_open:
        show_tab(admin_tab)
    mouse.locked=free_camera and not admin_open
    mouse.visible=not mouse.locked
    if race_active:
        hud.enabled=cfg.hud_visible and not admin_open


def enter_race():
    global race_active,free_camera,admin_open
    build_world()  # apply graphics quality; avoid accumulating duplicate scenes
    world_root.enabled=True
    race_active=True
    free_camera=False
    admin_open=False
    admin_root.enabled=False
    menu_root.enabled=False
    for car in all_cars:
        car.enabled=True
    mouse.locked=False
    mouse.visible=True
    reset_race()
    hud.enabled=cfg.hud_visible


def show_menu():
    global race_active,free_camera,admin_open
    race_active=False
    free_camera=False
    admin_open=False
    admin_root.enabled=False
    hud.enabled=False
    menu_root.enabled=True
    world_root.enabled=True
    camera.position=(.0,245,-207)
    camera.look_at(Vec3(0,0,7))
    camera.fov=78
    mouse.locked=False
    mouse.visible=True

start_btn.on_click=enter_race


def input(key):
    global camera_mode
    # Robust independent menu input: setting changes do not rely on
    # Button.hovered or the engine's per-button click dispatch.
    if menu_root.enabled and not race_active and not admin_open:
        if key == 'left mouse down':
            if activate_menu_setting_at(mouse.position.x, mouse.position.y):
                return
        # Keyboard shortcuts are useful when mouse input is unavailable.
        menu_keys = {'1': 'difficulty', '2': 'quality', '3': 'fps'}
        if key in menu_keys:
            change_menu_setting(menu_keys[key])
            return
    if key=='f6':
        toggle_admin()
        return
    if key=='escape':
        if admin_open:
            toggle_admin()
        elif race_active:
            show_menu()
        else:
            application.quit()
        return
    if key=='f7':
        toggle_freecam()
        return
    if key=='n':
        toggle_cfg('no_clip')
        tools_feedback.text='NOCLIP ENABLED' if cfg.no_clip else 'NOCLIP DISABLED'
        return
    if key=='r' and race_active:
        reset_race()
        return
    if key=='c' and race_active:
        cycle_camera()
        return
    if key=='tab' and admin_open:
        show_tab((admin_tab+1)%3)


def update():
    global race_seconds,impact_flash_time
    dt=min(.06,time.dt)*cfg.time_scale
    if free_camera:
        move_freecam(dt)
    if not race_active:
        return
    if not admin_open:
        drive_player(dt)
        if race_started and not race_done:
            race_seconds+=dt
            instructions.text=''
        update_ai(dt)
        s,lateral,road_dist=update_player_projection(dt)
        if not race_done:
            handle_collision(s,lateral,road_dist,dt)
            check_finish()
        if not free_camera:
            update_camera(dt=dt)
        if impact_flash_time>0:
            impact_flash_time=max(0.,impact_flash_time-dt)
            if impact_flash_time==0.:
                set_impact_flash(0)
    for car in all_cars:
        car.shadow.position=Vec3(car.x,.155,car.z)
        car.shadow.rotation_y=car.rotation_y
        car.shadow.enabled=car.enabled and (car.y<12)
    update_hud()


set_fps_cap()
build_world()
for car in all_cars:
    reset_car_to_grid(car,GRID_SIZE-1 if car.is_player else cpus.index(car))
show_menu()
app.run()
#HELP
