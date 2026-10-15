"""Shared code for building mod packs for the web book viewer.

A pack is a folder packs/<mod>/ holding data.json (books, item names, icon index, block models for multiblocks)
plus the atlases it references. Each mod has a small build script (tools/build_<mod>.py) that points this
library at the mod's resources and adds whatever the mod defines in Java (custom recipes, page processors).

Requires Pillow and numpy.
"""
import ast
import json
import math
import operator
import re
import shutil
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ICON = 64                       # icon size in the atlas, as in items.png
ATLAS_COLS = 16
PAGE_WIDTH = 116

warnings = []
def warn(msg):
    if msg not in warnings:
        warnings.append(msg)
        print('warning:', msg)


def split_rl(rl):
    if ':' in rl:
        ns, path = rl.split(':', 1)
    else:
        ns, path = 'minecraft', rl
    return ns, path


def java_format(fmt, *args):
    """Java's String.format for the subset lang files use: %s %d %n$s %% (and widths like %3d)."""
    it = iter(args)
    def rep(m):
        if m.group(0) == '%%':
            return '%'
        pos, width, conv = m.group(1), m.group(2) or '', m.group(3)
        v = args[int(pos) - 1] if pos else next(it, '')
        if conv == 'n':
            return '\n'
        s = str(int(v)) if conv == 'd' and isinstance(v, (int, float)) else str(v)
        return s.rjust(int(width)) if width else s
    return re.sub(r'%%|%(?:(\d+)\$)?(\d+)?([sdn])', rep, fmt)


# ------------------------------------------------------------------ resources
class Resources:
    """Looks things up across several resource roots, first root wins (mod, generated, forge, vanilla...)."""
    def __init__(self, roots):
        self.roots = [Path(r) for r in roots]
        self.lang = {}
        for r in reversed(self.roots):
            for f in r.glob('assets/*/lang/en_us.json'):
                self.lang.update(json.loads(f.read_text(encoding='utf-8')))
        self._json = {}
        self._tags = {}

    def find(self, kind, rl, ext):
        ns, path = split_rl(rl)
        for r in self.roots:
            p = r / kind / ns / path
            p = p.with_name(p.name + ext)
            if p.exists():
                return p
        return None

    def asset(self, sub, rl, ext):
        ns, path = split_rl(rl)
        return self.find('assets', f'{ns}:{sub}/{path}', ext)

    def json(self, path):
        if path not in self._json:
            self._json[path] = json.loads(path.read_text(encoding='utf-8'))
        return self._json[path]

    def t(self, key):
        """Patchouli i18n: a string that is a lang key is replaced by its translation."""
        if isinstance(key, str) and key in self.lang:
            return self.lang[key]
        return key

    def item_exists(self, item):
        return self.asset('models', item.replace(':', ':item/', 1), '.json') is not None

    def localize(self, key, *args):
        """TextHelper.localize: translate, then String.format with the arguments."""
        return java_format(self.lang.get(key, key), *args) if args else self.lang.get(key, key)

    def tag(self, rl):
        """Item ids in an item tag, following nested tags, merged across every root (as the game does)."""
        if rl in self._tags:
            return self._tags[rl]
        self._tags[rl] = []
        ns, path = split_rl(rl)
        out = []
        for r in self.roots:
            p = r / 'data' / ns / 'tags' / 'items' / (path + '.json')
            if not p.exists():
                continue
            for v in self.json(p).get('values', []):
                if isinstance(v, dict):
                    v = v.get('id', '')
                if v.startswith('#'):
                    out += [i for i in self.tag(v[1:]) if i not in out]
                elif v not in out and self.item_exists(v):   # tags may name blocks without items, or absent mods' items
                    out.append(v)
        if not out:
            warn(f'empty or unknown tag #{rl}')
        self._tags[rl] = out
        return out


# ------------------------------------------------------------------ stacks
# Stacks are written the way the viewer expects: a plain item id string, or {i, c, g, l} for count/glint/lore.
# Potions get a synthetic id per potion type (e.g. "minecraft:potion{awkward}") so each has its own icon and name.
EFFECT_COLORS = {
    'MOVEMENT_SPEED': 0x7CAFC6, 'MOVEMENT_SLOWDOWN': 0x5A6C81, 'DIG_SPEED': 0xD9C043, 'DAMAGE_BOOST': 0x932423,
    'HEAL': 0xF82423, 'HARM': 0x430A09, 'JUMP': 0x22FF4C, 'REGENERATION': 0xCD5CAB, 'DAMAGE_RESISTANCE': 0x99453A,
    'FIRE_RESISTANCE': 0xE49A3A, 'WATER_BREATHING': 0x2E5299, 'INVISIBILITY': 0x7F8392, 'NIGHT_VISION': 0x1F1FA1,
    'WEAKNESS': 0x484D48, 'POISON': 0x4E9331, 'SLOW_FALLING': 0xFFEFD1, 'MOLTEN_HEART': 0xF28E0C,
}
# vanilla Potions registry: name -> [(effect, amplifier)]
VANILLA_POTIONS = {
    'water': [], 'mundane': [], 'thick': [], 'awkward': [],
    'night_vision': [('NIGHT_VISION', 0)], 'invisibility': [('INVISIBILITY', 0)], 'leaping': [('JUMP', 0)],
    'fire_resistance': [('FIRE_RESISTANCE', 0)], 'swiftness': [('MOVEMENT_SPEED', 0)], 'slowness': [('MOVEMENT_SLOWDOWN', 0)],
    'turtle_master': [('MOVEMENT_SLOWDOWN', 3), ('DAMAGE_RESISTANCE', 2)], 'water_breathing': [('WATER_BREATHING', 0)],
    'healing': [('HEAL', 0)], 'harming': [('HARM', 0)], 'poison': [('POISON', 0)], 'regeneration': [('REGENERATION', 0)],
    'strength': [('DAMAGE_BOOST', 0)], 'weakness': [('WEAKNESS', 0)], 'slow_falling': [('SLOW_FALLING', 0)],
}
for _n in list(VANILLA_POTIONS):
    if VANILLA_POTIONS[_n]:
        VANILLA_POTIONS['long_' + _n] = VANILLA_POTIONS[_n]
        VANILLA_POTIONS['strong_' + _n] = [(e, a + 1) for e, a in VANILLA_POTIONS[_n]]
VANILLA_POTIONS['strong_turtle_master'] = [('MOVEMENT_SLOWDOWN', 5), ('DAMAGE_RESISTANCE', 3)]
POTION_ITEMS = {'minecraft:potion', 'minecraft:splash_potion', 'minecraft:lingering_potion'}


def potion_color(effects):
    """PotionUtils.getColor"""
    if not effects:
        return 0x385DC6
    r = g = b = 0.0
    n = 0
    for eff, amp in effects:
        c = EFFECT_COLORS[eff]
        w = amp + 1
        r += w * ((c >> 16) & 255) / 255
        g += w * ((c >> 8) & 255) / 255
        b += w * (c & 255) / 255
        n += w
    return (int(r / n * 255) << 16) | (int(g / n * 255) << 8) | int(b / n * 255)


class Stacks:
    """Collects every stack the book shows, so icons and names can be generated for exactly those.

    Mod scripts customise it through:
      mod_potions     potion name -> effects, for a mod's own potion items
      variants        item -> (model, lang key) for items whose look depends on NBT
      item_tints      item -> tint colour of layer 0
      nbt_hook        f(stacks, item, nbt) -> stack dict or None, for mod-specific NBT
    """
    def __init__(self, res):
        self.res = res
        self.mod_potions = {}
        self.variants = {}
        self.item_tints = {'minecraft:lily_pad': 0x71C35C}
        self.glint_items = set()
        self.nbt_hook = None
        self.custom_ingredients = {}      # ingredient "type" -> f(stacks, ingredient) -> list of stacks
        self.ids = {}                     # icon id -> {'item': real item id, 'potion': name|None}

    def potion_id(self, item, potion):
        sid = f'{item}{{{potion}}}'
        self.ids[sid] = {'item': item, 'potion': potion}
        return sid

    def plain(self, item):
        self.ids.setdefault(item, {'item': item, 'potion': None})
        return item

    def has_glint(self, sid):
        """1.20.1 potions glint when they carry effects."""
        info = self.ids[sid]
        if info['item'] in self.glint_items:
            return True
        return info['item'] in POTION_ITEMS and bool(VANILLA_POTIONS.get(info['potion']))

    def stack(self, item, potion=None, count=1):
        """A stack for the viewer: plain id string, or a dict when it has a potion glint or a count."""
        sid = self.potion_id(item, potion) if potion is not None else self.plain(item)
        out = {'i': sid}
        if self.has_glint(sid):
            out['g'] = 1
        if count != 1:
            out['c'] = count
        return sid if list(out) == ['i'] else out

    def parse(self, s):
        """Patchouli stack string: id[#count][{nbt}]"""
        s = s.strip()
        m = re.match(r'^([a-z0-9_.-]+:[a-z0-9_/.-]+)(?:#(\d+))?(\{.*\})?$', s)
        if not m:
            warn(f'unparsed stack {s!r}')
            return None
        item, count, nbt = m.group(1), m.group(2), m.group(3) or ''
        return self.from_nbt(item, nbt, int(count or 1))

    def from_nbt(self, item, nbt, count=1):
        out = None
        if self.nbt_hook and nbt:
            out = self.nbt_hook(self, item, nbt)
        if out is None:
            pm = re.search(r'Potion:\s*"minecraft:(\w+)"', nbt) if item in POTION_ITEMS else None
            out = {'i': self.potion_id(item, pm.group(1)) if pm else self.plain(item)}
            if self.has_glint(out['i']):
                out['g'] = 1
        elif isinstance(out, str):
            out = {'i': out}
        for ench in re.findall(r"id:\s*['\"]([a-z0-9_]+:[a-z0-9_]+)['\"]", nbt):
            out['g'] = 1
            ns, path = split_rl(ench)
            out.setdefault('l', []).append(self.res.t(f'enchantment.{ns}.{path}'))
        if count != 1:
            out['c'] = count
        return out['i'] if list(out) == ['i'] else out

    def name(self, sid):
        info = self.ids[sid]
        ns, path = split_rl(info['item'])
        if info['potion'] is not None:
            p = info['potion']
            if ns == 'minecraft':
                base = re.sub(r'^(long|strong)_', '', p)
                return self.res.t(f'item.minecraft.{path}.effect.{base}')
            return self.res.t(f'item.{ns}.{path}.effect.{p}')
        if info['item'] in self.variants:
            return self.res.t(self.variants[info['item']][1])
        for k in (f'item.{ns}.{path}', f'block.{ns}.{path}'):
            if k in self.res.lang:
                return self.res.lang[k]
        warn(f'no name for {sid}')
        return path.replace('_', ' ').title()

    def tint(self, sid):
        info = self.ids[sid]
        if info['potion'] is not None:
            p = info['potion']
            effects = VANILLA_POTIONS.get(p) if info['item'] in POTION_ITEMS else self.mod_potions.get(p)
            if effects is None:
                warn(f'unknown potion {p}')
                effects = []
            return {0: potion_color(effects)}
        return {0: self.item_tints[info['item']]} if info['item'] in self.item_tints else {}


# ------------------------------------------------------------------ models
def load_texture(res, rl):
    p = res.asset('textures', rl, '.png')
    if p is None:
        warn(f'missing texture {rl}')
        return None
    im = Image.open(p).convert('RGBA')
    if im.height > im.width and im.height % im.width == 0:      # animated: first frame
        im = im.crop((0, 0, im.width, im.width))
    return im


def model_chain(res, ref):
    """Follows a model's parents. Returns (list of model dicts child-first, builtin kind or None)."""
    chain, node, seen = [], ref, 0
    while node is not None and seen < 30:
        seen += 1
        if isinstance(node, str):
            ns, path = split_rl(node)
            if path in ('builtin/generated', 'builtin/entity'):
                return chain, path.split('/')[1]
            p = res.asset('models', f'{ns}:{path}', '.json')
            if p is None:
                warn(f'missing model {node}')
                return chain, None
            node = res.json(p)
        if node.get('loader') == 'forge:separate_transforms':
            node = node.get('perspectives', {}).get('gui') or node.get('base')
            continue
        if node.get('loader') == 'forge:fluid_container':
            # Forge draws the fluid into the bucket at runtime; the empty vanilla bucket stands in for it
            node = 'minecraft:item/bucket'
            continue
        if not node.get('parent') and not node.get('textures') and not node.get('elements') and node.get('overrides'):
            node = node['overrides'][0]['model']
            continue
        chain.append(node)
        node = node.get('parent')
    return chain, None


def resolve_textures(chain):
    tex = {}
    for m in reversed(chain):
        tex.update(m.get('textures', {}))
    def get(ref, depth=0):
        while ref and ref.startswith('#') and depth < 10:
            ref = tex.get(ref[1:])
            depth += 1
        return ref
    return get, tex


def tint_image(im, rgb):
    a = np.asarray(im).astype(np.float32)
    a[..., 0] *= ((rgb >> 16) & 255) / 255
    a[..., 1] *= ((rgb >> 8) & 255) / 255
    a[..., 2] *= (rgb & 255) / 255
    return Image.fromarray(a.clip(0, 255).astype(np.uint8))


def rot_matrix(axis, deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    if axis == 'x':
        return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
    if axis == 'y':
        return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def face_corners(d, f, t):
    """Corner 0 (texture top-left), corner 1 (bottom-left), corner 3 (top-right) of a face, as in vanilla FaceInfo."""
    x0, y0, z0 = f
    x1, y1, z1 = t
    return {
        'down':  [(x0, y0, z1), (x0, y0, z0), (x1, y0, z1)],
        'up':    [(x0, y1, z0), (x0, y1, z1), (x1, y1, z0)],
        'north': [(x1, y1, z0), (x1, y0, z0), (x0, y1, z0)],
        'south': [(x0, y1, z1), (x0, y0, z1), (x1, y1, z1)],
        'west':  [(x0, y1, z0), (x0, y0, z0), (x0, y1, z1)],
        'east':  [(x1, y1, z1), (x1, y0, z1), (x1, y1, z0)],
    }[d]


def default_uv(d, f, t):
    return {
        'down': [f[0], 16 - t[2], t[0], 16 - f[2]], 'up': [f[0], f[2], t[0], t[2]],
        'north': [16 - t[0], 16 - t[1], 16 - f[0], 16 - f[1]], 'south': [f[0], 16 - t[1], t[0], 16 - f[1]],
        'west': [f[2], 16 - t[1], t[2], 16 - f[1]], 'east': [16 - t[2], 16 - t[1], 16 - f[2], 16 - f[1]],
    }[d]


def obj_faces(res, model, get):
    """Faces of a Forge OBJ model (loader forge:obj): positions in block units, uvs 0..1 from the bottom-left."""
    path = res.find('assets', model['model'], '')
    if path is None:
        warn(f'missing OBJ {model["model"]}')
        return
    verts, uvs, mat, mtl_tex = [], [], None, {}
    for line in path.read_text(encoding='utf-8').splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == 'mtllib':
            mtl = path.with_name(parts[1])
            cur = None
            if mtl.exists():
                for ml in mtl.read_text(encoding='utf-8').splitlines():
                    mp = ml.split()
                    if mp and mp[0] == 'newmtl':
                        cur = mp[1]
                    elif mp and mp[0] == 'map_Kd' and cur:
                        mtl_tex[cur] = mp[1]
        elif parts[0] == 'v':
            verts.append(np.array([float(x) for x in parts[1:4]]) * 16)
        elif parts[0] == 'vt':
            uvs.append((float(parts[1]), float(parts[2])))
        elif parts[0] == 'usemtl':
            mat = parts[1]
        elif parts[0] == 'f':
            idx = [q.split('/') for q in parts[1:]]
            corners = [verts[int(i[0]) - 1] for i in idx]
            fuv = []
            for i in idx:
                u, v = uvs[int(i[1]) - 1] if len(i) > 1 and i[1] else (0, 0)
                fuv.append((u * 16, (1 - v if model.get('flip_v') else v) * 16))
            tex = mtl_tex.get(mat, '#default')
            ref = get(tex) if tex.startswith('#') else tex
            if ref and len(corners) in (3, 4):
                yield 'obj', corners, fuv, ref, None, True


def element_faces(chain, res=None):
    """Yields every textured face of a model: (dir, corners in model pixels, uv per corner (0..16), texture ref,
    tintindex, shade). Box elements give quads [top-left, bottom-left, bottom-right, top-right]; OBJ models give
    their own triangles and quads."""
    get, _ = resolve_textures(chain)
    obj = next((m for m in chain if m.get('loader') == 'forge:obj'), None)
    if obj is not None:
        yield from obj_faces(res, obj, get)
        return
    elements = next((m['elements'] for m in chain if 'elements' in m), [])
    for el in elements:
        f, t = el['from'], el['to']
        er = None
        if 'rotation' in el:
            r = el['rotation']
            o = np.array(r.get('origin', [8, 8, 8]), dtype=float)
            M = rot_matrix(r['axis'], r['angle'])
            if r.get('rescale'):
                k = 1 / math.cos(math.radians(r['angle']))
                M = M @ np.diag([1 if r['axis'] == 'x' else k, 1 if r['axis'] == 'y' else k, 1 if r['axis'] == 'z' else k])
            er = (lambda M, o: (lambda v: M @ (v - o) + o))(M, o)
        for d, face in el.get('faces', {}).items():
            ref = get(face.get('texture', ''))
            if not ref:
                continue
            c0, c1, c3 = [np.array(c, dtype=float) for c in face_corners(d, f, t)]
            c2 = c1 + c3 - c0
            corners = [c0, c1, c2, c3]
            if er is not None:
                corners = [er(c) for c in corners]
            uv = face.get('uv') or default_uv(d, f, t)
            shift = (face.get('rotation', 0) // 90) % 4
            def corner_uv(i):     # BlockFaceUV: vertex i takes the uv of corner (i + rotation/90) % 4
                k = (i + shift) % 4
                return (uv[0] if k in (0, 1) else uv[2], uv[1] if k in (0, 3) else uv[3])
            yield d, corners, [corner_uv(i) for i in range(4)], ref, face.get('tintindex'), el.get('shade', True)


# Lighting.setupFor3DItems: two diffuse lights, transformed into GUI render space
def _gui_lights():
    m = np.diag([1.0, -1.0, 1.0]) @ rot_matrix('y', -22.5) @ rot_matrix('x', 135)
    return [m @ (np.array(v) / np.linalg.norm(v)) for v in ((0.2, 1.0, -0.7), (-0.2, 1.0, 0.7))]
GUI_LIGHTS = _gui_lights()


def render_flat(res, chain, tints):
    get, tex = resolve_textures(chain)
    out = Image.new('RGBA', (ICON, ICON))
    i = 0
    while f'layer{i}' in tex:
        im = load_texture(res, get('#layer' + str(i)))
        if im is not None:
            if i in tints:
                im = tint_image(im, tints[i])
            out.alpha_composite(im.resize((ICON, ICON), Image.NEAREST))
        i += 1
    return out


def render_3d(res, chain, tints):
    display = {}
    for m in reversed(chain):
        if 'gui' in m.get('display', {}):
            display = m['display']['gui']
    rot = display.get('rotation', [0, 0, 0])
    trans = np.array(display.get('translation', [0, 0, 0]), dtype=float) / 16
    scale = np.array(display.get('scale', [1, 1, 1]), dtype=float)
    R = rot_matrix('x', rot[0]) @ rot_matrix('y', rot[1]) @ rot_matrix('z', rot[2])

    S = ICON
    color = np.zeros((S, S, 4), dtype=np.float32)
    depth = np.full((S, S), -np.inf)
    ys, xs = np.mgrid[0:S, 0:S]
    px, py = xs + 0.5, ys + 0.5
    textures = {}

    def transform(v):
        """model pixels -> GUI item space (y up, +z towards the viewer), one unit = the 16px slot"""
        return R @ ((v / 16 - 0.5) * scale) + trans

    def to_screen(v):
        return np.array([S / 2 + v[0] * S, S / 2 - v[1] * S, v[2]])

    for d, corners, uvs, ref, ti, shade in element_faces(chain, res):
        if ref not in textures:
            textures[ref] = load_texture(res, ref)
        tex = textures[ref]
        if tex is None:
            continue
        if ti is not None and ti in tints:
            key = (ref, ti)
            if key not in textures:
                textures[key] = tint_image(tex, tints[ti])
            tex = textures[key]
        arr = np.asarray(tex).astype(np.float32)
        th, tw = arr.shape[:2]
        model = [transform(c) for c in corners]
        screen = [to_screen(m) for m in model]
        # outward normal, then into render space (y down) for Lighting.setupFor3DItems' two diffuse lights
        n = np.cross(model[1] - model[0], model[2] - model[0])
        n = n / (np.linalg.norm(n) or 1)
        n = np.array([n[0], -n[1], n[2]])
        light = min(1.0, 0.4 + 0.6 * sum(max(0.0, float(np.dot(L, n))) for L in GUI_LIGHTS)) if shade else 1.0
        for tri in ((0, 1, 2), (0, 2, 3))[:len(corners) - 2]:
            p0, p1, p2 = (screen[i] for i in tri)
            det = (p1[0] - p0[0]) * (p2[1] - p0[1]) - (p2[0] - p0[0]) * (p1[1] - p0[1])
            if abs(det) < 1e-9:
                continue
            # barycentric weights of every pixel centre (orthographic, so they interpolate uv and depth exactly)
            w1 = ((px - p0[0]) * (p2[1] - p0[1]) - (p2[0] - p0[0]) * (py - p0[1])) / det
            w2 = ((p1[0] - p0[0]) * (py - p0[1]) - (px - p0[0]) * (p1[1] - p0[1])) / det
            w0 = 1 - w1 - w2
            inside = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
            if not inside.any():
                continue
            z = w0 * p0[2] + w1 * p1[2] + w2 * p2[2]
            (u0, v0), (u1, v1), (u2, v2) = (uvs[i] for i in tri)
            u = w0 * u0 + w1 * u1 + w2 * u2
            v = w0 * v0 + w1 * v1 + w2 * v2
            tx = np.clip(np.floor(u / 16 * tw).astype(int), 0, tw - 1)
            ty = np.clip(np.floor(v / 16 * th).astype(int), 0, th - 1)
            sample = arr[ty, tx]
            ok = inside & (sample[..., 3] > 25) & (z > depth)
            depth[ok] = z[ok]
            color[ok, :3] = sample[ok, :3] * light
            color[ok, 3] = 255
    return Image.fromarray(color.clip(0, 255).astype(np.uint8))


class MainAtlas:
    """The site's main icon atlas. Items drawn by block entity renderers in game (chests, shields, mob heads)
    cannot be rendered from their models, so their icons are taken from here."""
    def __init__(self):
        self.index = json.loads((ROOT / 'data.json').read_text(encoding='utf-8'))['icons']
        self.img = Image.open(ROOT / 'items.png').convert('RGBA')

    def get(self, item):
        n = self.index['index'].get(item)
        if n is None:
            return None
        S, cols = self.index['size'], self.index['cols']
        x, y = (n % cols) * S, (n // cols) * S
        return self.img.crop((x, y, x + S, y + S)).resize((ICON, ICON), Image.NEAREST)


def render_icon(res, sid, stacks, main_atlas):
    info = stacks.ids[sid]
    item = info['item']
    ns, path = split_rl(item)
    chain, builtin = model_chain(res, stacks.variants[item][0] if item in stacks.variants else f'{ns}:item/{path}')
    if builtin == 'entity' or not chain:
        im = main_atlas.get(item)
        if im is None:
            warn(f'cannot render {item}')
        return im
    tints = stacks.tint(sid)
    if builtin == 'generated' or not any('elements' in m or m.get('loader') == 'forge:obj' for m in chain):
        return render_flat(res, chain, tints)
    return render_3d(res, chain, tints)


# ------------------------------------------------------------------ block models for multiblocks
class Blocks:
    """Block models in the viewer's multiblock format: quads with positions and uvs in pixels, and an index into a
    texture atlas of 16px tiles (the same layout as the main blocks.png)."""
    TILE = 16
    COLS = 16
    FACE_ROT = {'x': {'north': 'down', 'down': 'south', 'south': 'up', 'up': 'north'},
                'y': {'north': 'east', 'east': 'south', 'south': 'west', 'west': 'north'}}

    def __init__(self, res):
        self.res = res
        self.models = {}
        self.tiles = []
        self.tile_index = {}

    def tile(self, ref):
        if ref not in self.tile_index:
            im = load_texture(self.res, ref)
            if im is None:
                return None
            self.tile_index[ref] = len(self.tiles)
            self.tiles.append(im.resize((self.TILE, self.TILE), Image.NEAREST))
        return self.tile_index[ref]

    def state_model(self, block):
        """The model a block's default state uses, with the blockstate's x/y rotation."""
        p = self.res.asset('blockstates', block, '.json')
        if p is None:
            warn(f'no blockstate for {block}')
            return None, 0, 0
        bs = self.res.json(p)
        if 'variants' in bs:
            v = bs['variants'].get('') or next(iter(bs['variants'].values()))
        else:
            parts = [part['apply'] for part in bs.get('multipart', []) if 'when' not in part] or [bs['multipart'][0]['apply']]
            v = parts[0]
        if isinstance(v, list):
            v = v[0]
        return v['model'], v.get('x', 0), v.get('y', 0)

    def add(self, block, model=None):
        block = block.split('[')[0]
        if block in self.models:
            return
        rx = ry = 0
        if model is None:
            model, rx, ry = self.state_model(block)
            if model is None:
                return
        chain, builtin = model_chain(self.res, model)
        quads = []
        # blockstate rotations turn the model about the block centre: x first, then y (vanilla Variant)
        M = rot_matrix('y', -ry) @ rot_matrix('x', -rx)
        for d, corners, uvs, ref, ti, shade in element_faces(chain, self.res):
            if len(corners) == 3:          # the viewer draws quads; a repeated corner makes a triangle
                corners, uvs = corners + [corners[2]], uvs + [uvs[2]]
            t = self.tile(ref)
            if t is None:
                continue
            pts = [M @ (c - 8) + 8 for c in corners]
            f = d
            for _ in range((rx // 90) % 4):
                f = self.FACE_ROT['x'].get(f, f)
            for _ in range((ry // 90) % 4):
                f = self.FACE_ROT['y'].get(f, f)
            quads.append({'v': [round(float(x), 4) for p in pts for x in p], 'uv': [round(float(x), 4) for uv in uvs for x in uv],
                          't': t, 'f': f})
        if not quads:
            warn(f'no geometry for block {block}')
        self.models[block] = quads

    def write(self, out_dir, ns):
        rows = max(1, math.ceil(len(self.tiles) / self.COLS))
        atlas = Image.new('RGBA', (self.COLS * self.TILE, rows * self.TILE))
        for i, im in enumerate(self.tiles):
            atlas.paste(im, ((i % self.COLS) * self.TILE, (i // self.COLS) * self.TILE))
        atlas.save(out_dir / 'blocks.png', optimize=True)
        return {'file': f'packs/{ns}/blocks.png', 'cols': self.COLS, 'rows': rows, 'tile': self.TILE, 'blocks': self.models}


# ------------------------------------------------------------------ recipes
def read_recipe(res, rl):
    ns, path = split_rl(rl)
    p = res.find('data', f'{ns}:recipes/{path}', '.json')
    if p is None:
        warn(f'missing recipe {rl}')
        return None
    return res.json(p)


def ingredient(stacks, ing):
    """A recipe ingredient -> list of stacks to cycle through."""
    if ing is None:
        return []
    if isinstance(ing, dict) and ing.get('type') in stacks.custom_ingredients:
        return stacks.custom_ingredients[ing['type']](stacks, ing)
    if isinstance(ing, list):
        out = []
        for i in ing:
            out += ingredient(stacks, i)
        return out
    if 'tag' in ing:
        return [stacks.plain(i) for i in stacks.res.tag(ing['tag'])]
    if 'item' in ing:
        if 'nbt' in ing:
            nbt = ing['nbt'] if isinstance(ing['nbt'], str) else json.dumps(ing['nbt'])
            return [stacks.from_nbt(ing['item'], nbt)]
        return [stacks.plain(ing['item'])]
    if 'ingredient' in ing:        # forge:nbt and similar wrappers
        return ingredient(stacks, ing['ingredient'])
    if 'children' in ing:          # forge:compound
        return ingredient(stacks, ing['children'])
    warn(f'unsupported ingredient {ing}')
    return []


def result_stack(stacks, r):
    if isinstance(r, str):
        return stacks.plain(r)
    item = r.get('item') or r.get('id')
    nbt = r.get('nbt')
    if nbt is not None and not isinstance(nbt, str):
        nbt = json.dumps(nbt)
    s = stacks.from_nbt(item, nbt or '', r.get('count', 1))
    return s


def convert_crafting(stacks, rec):
    toast = stacks.plain('minecraft:crafting_table')
    if 'pattern' in rec:
        pat = rec['pattern']
        w = max(len(row) for row in pat)
        cells = []
        for row in pat:
            for ch in row.ljust(w):
                cells.append([] if ch == ' ' else ingredient(stacks, rec['key'].get(ch)))
        return {'out': result_stack(stacks, rec['result']), 'toast': toast, 'shaped': 1, 'w': w, 'in': cells}
    if 'ingredients' in rec:
        return {'out': result_stack(stacks, rec['result']), 'toast': toast, 'shaped': 0,
                'in': [ingredient(stacks, i) for i in rec['ingredients']]}
    warn(f'unsupported crafting recipe type {rec.get("type")}')
    return None


TOASTS = {'minecraft:smelting': 'minecraft:furnace', 'minecraft:blasting': 'minecraft:blast_furnace',
          'minecraft:smoking': 'minecraft:smoker', 'minecraft:campfire_cooking': 'minecraft:campfire'}


def convert_cooking(stacks, rec):
    return {'in': [ingredient(stacks, rec['ingredient'])], 'out': result_stack(stacks, rec['result']),
            'toast': stacks.plain(TOASTS.get(rec['type'], 'minecraft:furnace'))}


def stack_id(s):
    return s if isinstance(s, str) else s and s.get('i')


# ------------------------------------------------------------------ Java source helpers
def call_args(src, start):
    """Splits the argument list of the call whose '(' is at src[start] into top-level argument strings."""
    depth, args, cur = 0, [], ''
    for i in range(start, len(src)):
        c = src[i]
        if c == '(':
            depth += 1
            if depth == 1:
                continue
        elif c == ')':
            depth -= 1
            if depth == 0:
                args.append(cur)
                return [a.strip() for a in args if a.strip()], i
        if depth == 1 and c == ',':
            args.append(cur)
            cur = ''
        elif depth >= 1:
            cur += c
    raise ValueError('unbalanced call')


def split_top(s):
    depth, out, cur = 0, [], ''
    for c in s:
        if c in '([{':
            depth += 1
        elif c in ')]}':
            depth -= 1
        if c == ',' and depth == 0:
            out.append(cur)
            cur = ''
        else:
            cur += c
    out.append(cur)
    return out


def strip_java_comments(src):
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    return re.sub(r'//[^\n]*', '', src)


def method_body(src, signature_regex):
    """The {...} body of the first method whose declaration matches the regex, or None."""
    m = re.search(signature_regex, src)
    if not m:
        return None
    i = src.index('{', m.end() - 1 if src[m.end() - 1] == '{' else m.end())
    depth = 0
    for j in range(i, len(src)):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return src[i + 1:j]
    return None


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.FloorDiv: operator.floordiv,
        ast.Div: operator.floordiv, ast.Mod: operator.mod, ast.USub: operator.neg, ast.UAdd: operator.pos}


def eval_int(expr, env):
    """Evaluates an integer expression from Java source (numbers, variables, + - * / %) without running code."""
    def ev(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.Name):
            return env[n.id]
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        raise ValueError('unsupported expression ' + expr)
    return int(ev(ast.parse(expr.strip(), mode='eval').body))


def run_java_block(body, env, on_call):
    """A tiny interpreter for the loop-and-call code mods use to list structure components.

    Handles `for (int v = a; v <= b; v++/v--/v += n) {...}` (with or without braces) and plain statements; each
    statement is handed to on_call(statement, env). Nothing from the source is executed as code."""
    i, n = 0, len(body)
    while i < n:
        while i < n and body[i] in ' \t\r\n;':
            i += 1
        if i >= n:
            break
        if body.startswith('{', i):
            depth, j = 0, i
            while True:
                depth += (body[j] == '{') - (body[j] == '}')
                if depth == 0:
                    break
                j += 1
            run_java_block(body[i + 1:j], env, on_call)
            i = j + 1
            continue
        m = re.match(r'for\s*\(', body[i:])
        if m:
            args, end = call_args(body, i + m.end() - 1)
            header = body[i + m.end():end]
            init, cond, step = [s.strip() for s in header.split(';')]
            im = re.match(r'(?:int\s+)?(\w+)\s*=\s*(.+)$', init)
            var = im.group(1)
            j = end + 1
            while body[j] in ' \t\r\n':
                j += 1
            if body[j] == '{':
                depth, k = 0, j
                while True:
                    depth += (body[k] == '{') - (body[k] == '}')
                    if depth == 0:
                        break
                    k += 1
                inner, i = body[j + 1:k], k + 1
            else:
                k = body.index(';', j)
                inner, i = body[j:k + 1], k + 1
            cm = re.match(r'(\w+)\s*(<=|<|>=|>|!=)\s*(.+)$', cond)
            sm = re.match(r'(?:\+\+(\w+)|(\w+)\+\+|--(\w+)|(\w+)--|(\w+)\s*([+-])=\s*(.+))$', step)
            if sm.group(1) or sm.group(2):
                delta = 1
            elif sm.group(3) or sm.group(4):
                delta = -1
            else:
                delta = eval_int(sm.group(7), env) * (1 if sm.group(6) == '+' else -1)
            local = dict(env)
            local[var] = eval_int(im.group(2), env)
            cmp = {'<=': operator.le, '<': operator.lt, '>=': operator.ge, '>': operator.gt, '!=': operator.ne}[cm.group(2)]
            guard = 0
            while cmp(local[var], eval_int(cm.group(3), local)) and guard < 10000:
                run_java_block(inner, local, on_call)
                local[var] += delta
                guard += 1
            continue
        j = body.find(';', i)
        if j < 0:
            j = n
        # a statement may contain parentheses with ';' only inside strings, which these methods never use
        on_call(body[i:j].strip(), env)
        i = j + 1


# ------------------------------------------------------------------ files
def img_path(rl):
    """ns:textures/gui/x.png -> img/ns/textures/gui/x.png (copied there by copy_image)"""
    ns, path = split_rl(rl)
    return f'img/{ns}/{path}'


def copy_image(res, rl):
    ns, path = split_rl(rl)
    if not path.endswith('.png'):
        path += '.png'
    rl = f'{ns}:{path}'
    dst = ROOT / img_path(rl)
    if dst.exists():
        return img_path(rl)
    src = res.find('assets', rl, '')
    if src is None:
        warn(f'missing image {rl}')
        return None
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return img_path(rl)


# ------------------------------------------------------------------ templates
class Vars:
    """Patchouli's variable lookup for one template instance: its processor first, then the page's own fields
    (prefixed with the include's "as" name for included templates)."""
    def __init__(self, page, prefix='', processor=None, parent=None):
        self.page, self.prefix, self.processor = page, prefix, processor

    def raw(self, key):
        return self.page.get(self.prefix + key)

    def get(self, key):
        if self.processor is not None:
            v = self.processor.process(key)
            if v is not None:
                return v
        return self.raw(key)


class BookBuilder:
    """Converts one Patchouli book (book.json + en_us folder) into the viewer's format.

    A mod script can extend it with:
      page_handlers   page type -> f(builder, page, links, index) -> viewer page dict
      processors      Java processor class name -> f(builder, vars) -> object with process(key) (None = not handled)
      multiblocks     multiblock id -> {'pattern': [[...]], 'mapping': {char: block id}}
    """
    def __init__(self, res, stacks, ns, book, book_dir, book_json):
        self.res, self.stacks, self.ns, self.book_name = res, stacks, ns, book
        self.book_dir = Path(book_dir)
        self.book_json = book_json
        self.page_handlers = {}
        self.processors = {}
        self.multiblocks = {}
        self.blocks = Blocks(res)
        self.templates = {}
        for p in (self.book_dir / 'templates').glob('**/*.json'):
            rel = p.relative_to(self.book_dir / 'templates').with_suffix('').as_posix()
            self.templates[f'{ns}:{rel}'] = json.loads(p.read_text(encoding='utf-8'))

    # ---- values
    def full_id(self, s):
        return s if ':' in s else f'{self.ns}:{s}'

    def icon(self, s):
        if isinstance(s, str) and (':textures/' in s or s.endswith('.png')):
            p = copy_image(self.res, s)
            return {'tex': p} if p else None
        return self.stacks.parse(s)

    def stack_list(self, s):
        """A Patchouli item value: a stack string (comma-separated alternatives), a list, or stacks already built."""
        if s is None or s == '':
            return []
        if isinstance(s, list):
            return [x for v in s for x in self.stack_list(v)]
        if isinstance(s, dict):
            return [s]
        if s in self.stacks.ids:      # an id a processor already built, e.g. a synthetic "item{potion}" id
            return [s]
        parts, depth, cur = [], 0, ''
        for ch in s:     # commas inside {nbt} belong to the stack
            depth += (ch in '{[') - (ch in '}]')
            if ch == ',' and depth == 0:
                parts.append(cur)
                cur = ''
            else:
                cur += ch
        parts.append(cur)
        return [x for x in (self.stacks.parse(p) for p in parts) if x]

    # ---- templates
    def interpolate(self, value, vars):
        """"#key" -> the variable itself; "...#key#..." -> each variable as text."""
        if not isinstance(value, str):
            return value
        if re.fullmatch(r'#[\w.]+', value):
            return vars.get(value[1:])
        def rep(m):
            v = vars.get(m.group(1))
            if isinstance(v, bool):
                return 'true' if v else 'false'
            return '' if v is None else str(v)
        return re.sub(r'#([\w.]+)#', rep, value)

    def truthy(self, v):
        return v not in (None, False, '', 'false', 0, [])

    def template(self, name, page, prefix='', dx=0, dy=0, processor_vars=None):
        """Renders template `name` for `page`. Returns (components, stacks the page should link to its recipe)."""
        tpl = self.templates.get(self.full_id(name))
        if tpl is None:
            warn(f'unknown template {name}')
            return [], []
        vars = Vars(page, prefix)
        if tpl.get('processor'):
            factory = self.processors.get(tpl['processor'].split('.')[-1])
            if factory is None:
                warn(f'no processor for {tpl["processor"]}')
            else:
                vars.processor = factory(self, vars)
        comps, links = [], []
        for inc in tpl.get('include', []):
            c, l = self.template(inc['template'], page, prefix + inc['as'] + '.', dx + int(inc.get('x', 0)), dy + int(inc.get('y', 0)))
            comps += c
            links += l
        for c in tpl.get('components', []):
            typ = c['type'].split(':')[-1]
            if 'guard' in c and not self.truthy(self.interpolate(c['guard'], vars)):
                continue
            x, y = int(self.interpolate(c.get('x', 0), vars) or 0), int(self.interpolate(c.get('y', 0), vars) or 0)
            if typ == 'header':
                comps.append({'t': 'header', 'x': (PAGE_WIDTH // 2 if x == -1 else x) + dx, 'y': (0 if y == -1 else y) + dy,
                              'text': self.res.t(str(self.interpolate(c['text'], vars) or '')), 'centered': c.get('centered', True)})
            elif typ == 'text':
                comps.append({'t': 'text', 'x': x + dx, 'y': y + dy, 'w': c.get('max_width', PAGE_WIDTH),
                              'text': self.res.t(str(self.interpolate(c['text'], vars) or ''))})
            elif typ == 'image':
                src = copy_image(self.res, str(self.interpolate(c['image'], vars)))
                comps.append({'t': 'image', 'x': x + dx, 'y': y + dy, 'src': src, 'u': c.get('u', 0), 'v': c.get('v', 0),
                              'w': c['width'], 'h': c['height'], 'tw': c.get('texture_width', 256), 'th': c.get('texture_height', 256),
                              'scale': c.get('scale', 1)})
            elif typ == 'item':
                items = self.stack_list(self.interpolate(c['item'], vars))
                if not items:
                    continue
                if str(c.get('link_recipe', False)).lower() == 'true':
                    links.append(items[0])
                comps.append({'t': 'item', 'x': x + dx, 'y': y + dy, 'items': items,
                              'framed': str(c.get('framed', False)).lower() == 'true'})
            elif typ == 'tooltip':
                lines = [self.res.t(str(self.interpolate(t, vars))) for t in c.get('tooltip', [])]
                comps.append({'t': 'tooltip', 'x': x + dx, 'y': y + dy, 'w': c.get('width', 16), 'h': c.get('height', 16), 'lines': lines})
            elif typ == 'separator':
                comps.append({'t': 'separator', 'x': x + dx, 'y': y + dy})
            else:
                warn(f'unsupported template component {typ}')
        return comps, links

    # ---- pages
    def recipes(self, p, keys, convert):
        out = []
        for k in keys:
            if p.get(k):
                rec = read_recipe(self.res, p[k])
                r = rec and convert(self.stacks, rec)
                if r:
                    out.append(r)
        return out

    def page(self, p, links, index):
        t = self.res.t
        typ = p['type']
        if typ.startswith('patchouli:'):
            typ = typ[len('patchouli:'):]
        handler = self.page_handlers.get(typ)
        if handler:
            return handler(self, p, links, index)
        text = t(p.get('text', ''))
        title = {'title': t(p['title'])} if p.get('title') else {}
        if typ == 'text':
            return {'type': 'text', 'text': text, **title}
        if typ == 'crafting':
            recs = self.recipes(p, ('recipe', 'recipe2'), convert_crafting)
            links += [[r['out'], index] for r in recs]
            return {'type': 'crafting', 'r': recs, 'text': text, **title}
        if typ in ('smelting', 'blasting', 'smoking', 'campfire_cooking'):
            recs = self.recipes(p, ('recipe', 'recipe2'), convert_cooking)
            links += [[r['out'], index] for r in recs]
            return {'type': 'processing', 'r': recs, 'text': text, **title}
        if typ == 'spotlight':
            items = self.stack_list(p.get('item'))
            if p.get('link_recipe') and items:
                links.append([items[0], index])
            return {'type': 'spotlight', 'items': items, 'text': text, **title}
        if typ == 'image':
            return {'type': 'image', 'images': [copy_image(self.res, i) for i in p['images']], 'border': bool(p.get('border')),
                    'text': text, **title}
        if typ == 'empty':
            return {'type': 'empty', 'filler': p.get('draw_filler', True)}
        if typ == 'relations':
            return {'type': 'relations', 'entries': [self.full_id(e) for e in p.get('entries', [])], 'text': text, **title}
        if typ == 'multiblock':
            mb = p.get('multiblock') or self.multiblocks.get(p.get('multiblock_id', ''))
            if mb is None:
                warn(f'unknown multiblock {p.get("multiblock_id")}')
                return {'type': 'text', 'text': text, 'title': t(p.get('name', ''))}
            for block in set(mb['mapping'].values()):
                self.blocks.add(block)
            return {'type': 'multiblock', 'name': t(p.get('name', '')), 'mb': mb, 'visualize': False, 'text': text}
        if typ == 'link':
            return {'type': 'link', 'text': text, 'url': p.get('url'), 'link_text': t(p.get('link_text', '')), **title}
        if self.full_id(typ) in self.templates:
            comps, l = self.template(typ, p)
            links += [[s, index] for s in l]
            return {'type': 'template', 'c': comps}
        warn(f'unsupported page type {typ}')
        return {'type': 'text', 'text': text}

    def flag_shown(self, flag):
        """Whether an entry or page with this Patchouli "flag" appears in the web book.

        In game, a flag hides content unless it holds: "mod:curios" needs Curios installed, "!mod:x" needs x absent,
        "debug" needs debug mode, other names are config options a mod sets. The web book has no modpack, so this
        decides what a reader sees. Called with None for content that has no flag.

        The site is a complete reference, so it behaves as if every mod were installed: "mod:x" content is shown and
        "!mod:x" content (written for players without x) is hidden. Other flags are shown."""
        if not flag:
            return True
        flag = flag.strip()
        return not flag.startswith('!mod:')

    def build(self, extra=None):
        res, t = self.res, self.res.t
        bj = self.book_json
        cats = []
        for f in sorted((self.book_dir / 'categories').glob('**/*.json')):
            c = res.json(f)
            cid = f'{self.ns}:' + f.relative_to(self.book_dir / 'categories').with_suffix('').as_posix()
            cat = {'id': cid, 'name': t(c['name']), 'desc': t(c.get('description', '')), 'icon': self.icon(c['icon']),
                   'sortnum': c.get('sortnum', 0)}
            if c.get('parent'):
                cat['parent'] = self.full_id(c['parent'])
            if c.get('secret'):
                cat['secret'] = 1
            cats.append(cat)
        entries = []
        for f in sorted((self.book_dir / 'entries').glob('**/*.json')):
            e = res.json(f)
            if not self.flag_shown(e.get('flag')):
                continue
            eid = f'{self.ns}:' + f.relative_to(self.book_dir / 'entries').with_suffix('').as_posix()
            links, pages = [], []
            # hidden pages are dropped before numbering, as in game, so spreads and recipe links stay right
            for i, p in enumerate(p for p in e['pages'] if self.flag_shown(p.get('flag'))):
                pg = self.page(p, links, i)
                if p.get('anchor'):
                    pg['anchor'] = p['anchor']
                pages.append(pg)
            for item, pg in e.get('extra_recipe_mappings', {}).items():
                if not self.res.item_exists(item.split('#')[0].split('{')[0]):
                    continue           # Patchouli ignores mappings to items that don't exist
                for s in self.stack_list(item):
                    links.append([s, pg])
            entry = {'id': eid, 'name': t(e['name']), 'icon': self.icon(e['icon']), 'category': self.full_id(e['category']),
                     'pages': pages, 'sortnum': e.get('sortnum', 0)}
            for k, dk in (('priority', 'priority'), ('secret', 'secret'), ('read_by_default', 'readByDefault')):
                if e.get(k):
                    entry[dk] = 1
            if links:
                entry['stacks'] = links
            entries.append(entry)
        book = {
            'id': f'{self.ns}:{self.book_name}', 'name': t(bj['name']), 'landing': t(bj.get('landing_text', '')),
            'texture': copy_image(res, bj.get('book_texture', 'patchouli:textures/gui/book_brown.png')),
            'filler': copy_image(res, bj.get('filler_texture', 'patchouli:textures/gui/page_filler.png')),
            'crafting': copy_image(res, bj.get('crafting_texture', 'patchouli:textures/gui/crafting.png')), 'version': 1,
            'subtitle': t(bj.get('subtitle', '')),
            'showProgress': bool(bj.get('show_progress', True)), 'advancementsEnabled': True,
            'icon': self.stacks.plain(bj.get('custom_book_item', f'patchouli:guide_book')),
            'textColor': bj.get('text_color', '000000'), 'headerColor': bj.get('header_color', '333333'),
            'nameplateColor': bj.get('nameplate_color', 'FFDD00'), 'linkColor': bj.get('link_color', '0000EE'),
            'linkHoverColor': bj.get('link_hover_color', '8800EE'), 'progressBarColor': bj.get('progress_bar_color', 'FFFF55'),
            'progressBarBackground': bj.get('progress_bar_background', 'DDDDDD'), 'macros': bj.get('macros', {}),
            # Patchouli books default to the narrow "uniform" font (use_blocky_font: false), so their text fits the
            # page in game. The viewer draws the wider default font; shrinking overflowing text keeps pages readable.
            'overflow': 'resize', 'categories': cats, 'entries': entries,
        }
        book.update(extra or {})
        return book


# ------------------------------------------------------------------ output
def write_pack(ns, books, stacks, res, blocks=None):
    out = ROOT / 'packs' / ns
    out.mkdir(parents=True, exist_ok=True)
    ids = sorted(stacks.ids)
    rows = max(1, math.ceil(len(ids) / ATLAS_COLS))
    atlas = Image.new('RGBA', (ATLAS_COLS * ICON, rows * ICON))
    index, names = {}, {}
    main_atlas = MainAtlas()
    for n, sid in enumerate(ids):
        im = render_icon(res, sid, stacks, main_atlas)
        if im is not None:
            atlas.alpha_composite(im, ((n % ATLAS_COLS) * ICON, (n // ATLAS_COLS) * ICON))
        index[sid] = n
        names[sid] = stacks.name(sid)
    atlas.save(out / 'items.png', optimize=True)
    data = {'books': books, 'itemNames': names,
            'icons': {'file': f'packs/{ns}/items.png', 'size': ICON, 'cols': ATLAS_COLS, 'index': index}}
    if blocks is not None and blocks.models:
        data['blocks'] = blocks.write(out, ns)
    (out / 'data.json').write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    n_entries = sum(len(b['entries']) for b in books)
    print(f'{n_entries} entries, {sum(len(b["categories"]) for b in books)} categories, {len(ids)} icons'
          + (f', {len(blocks.models)} block models' if blocks is not None and blocks.models else '') + f' -> packs/{ns}')
    if warnings:
        print(f'{len(warnings)} warnings')
