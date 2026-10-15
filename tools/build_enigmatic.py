#!/usr/bin/env python3
"""Builds the Enigmatic Legacy pack (packs/enigmaticlegacy/) for the web book viewer.

The pack is generated from Enigmatic Legacy's own published resources:
  - the Patchouli book "The Acknowledgment" (categories, entries, templates, en_us text)
  - crafting/smelting recipes from the mod's data folder
  - brewing and "hidden" recipes, which the mod defines in Java, read from its source
  - item icons, rendered here from the mod's item/block models and textures

Usage:
  python tools/build_enigmatic.py <enigmatic-legacy> <vanilla>

  <enigmatic-legacy>  a checkout of https://github.com/Aizistral-Studios/Enigmatic-Legacy (branch 1.20.X)
  <vanilla>           a folder holding assets/minecraft and data/minecraft extracted from the 1.20.1 client jar
                      (needed for vanilla item icons, names and the two vanilla recipes the book shows)

Requires Pillow and numpy.
"""
import json
import math
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
NS = 'enigmaticlegacy'
BOOK = 'the_acknowledgment'
OUT = ROOT / 'packs' / NS
IMG_OUT = ROOT / 'img' / NS
ICON = 64                       # icon size in the atlas, as in items.png
ATLAS_COLS = 16
PAGE_WIDTH = 116

warnings = []
def warn(msg):
    warnings.append(msg)
    print('warning:', msg)


# ------------------------------------------------------------------ resources
class Resources:
    def __init__(self, el, vanilla):
        self.el_res = el / 'src' / 'main' / 'resources'
        self.el_java = el / 'src' / 'main' / 'java' / 'com' / 'aizistral' / 'enigmaticlegacy'
        self.roots = [self.el_res, vanilla]
        self.lang = {}
        for r in reversed(self.roots):
            for f in r.glob('assets/*/lang/en_us.json'):
                self.lang.update(json.loads(f.read_text(encoding='utf-8')))
        self._json = {}

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


def split_rl(rl):
    if ':' in rl:
        ns, path = rl.split(':', 1)
    else:
        ns, path = 'minecraft', rl
    return ns, path


# ------------------------------------------------------------------ stacks
# Stacks are written the way the viewer expects: a plain item id string, or {i, c, g, l} for count/glint/lore.
# Potions get a synthetic id per potion type (e.g. "enigmaticlegacy:ultimate_potion{ultimate_healing}")
# so that each one has its own coloured icon and name.
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
    """Collects every stack the book shows, so icons and names can be generated for exactly those."""
    def __init__(self, res, el_potions):
        self.res = res
        self.el_potions = el_potions      # name -> effects
        self.ids = {}                     # icon id -> {'item': real item id, 'potion': name|None}

    def potion_id(self, item, potion):
        sid = f'{item}{{{potion}}}'
        self.ids[sid] = {'item': item, 'potion': potion}
        return sid

    def plain(self, item):
        self.ids.setdefault(item, {'item': item, 'potion': None})
        return item

    def parse(self, s):
        """Patchouli stack string: id[#count][{nbt}]"""
        s = s.strip()
        m = re.match(r'^([a-z0-9_.-]+:[a-z0-9_/.-]+)(?:#(\d+))?(\{.*\})?$', s)
        if not m:
            warn(f'unparsed stack {s!r}')
            return None
        item, count, nbt = m.group(1), m.group(2), m.group(3) or ''
        out = {}
        pm = re.search(r"EnigmaticPotion:\s*'(\w+)'", nbt)
        if pm:
            out['i'] = self.potion_id(item, pm.group(1))
            if self.has_glint(out['i']):
                out['g'] = 1
        else:
            out['i'] = self.plain(item)
        for ench in re.findall(r"id:\s*'([a-z0-9_]+:[a-z0-9_]+)'", nbt):
            out['g'] = 1
            ns, path = split_rl(ench)
            out.setdefault('l', []).append(self.res.t(f'enchantment.{ns}.{path}'))
        if count and int(count) != 1:
            out['c'] = int(count)
        return out['i'] if list(out) == ['i'] else out

    def has_glint(self, sid):
        """1.20.1 potions glint when they carry effects; Enigmatic Legacy's ultimate potions always do."""
        info = self.ids[sid]
        if 'ultimate_potion' in info['item']:
            return True
        return info['item'] in POTION_ITEMS and bool(VANILLA_POTIONS.get(info['potion']))

    def name(self, sid):
        info = self.ids[sid]
        ns, path = split_rl(info['item'])
        if info['potion'] is not None:
            p = info['potion']
            if ns == 'minecraft':
                base = re.sub(r'^(long|strong)_', '', p)
                return self.res.t(f'item.minecraft.{path}.effect.{base}')
            return self.res.t(f'item.{ns}.{path}.effect.{p}')
        if info['item'] in VARIANTS:
            return self.res.t(VARIANTS[info['item']][1])
        for k in (f'item.{ns}.{path}', f'block.{ns}.{path}'):
            if k in self.res.lang:
                return self.res.lang[k]
        warn(f'no name for {sid}')
        return path

    def tint(self, sid):
        info = self.ids[sid]
        if info['potion'] is not None:
            p = info['potion']
            effects = VANILLA_POTIONS.get(p) if info['item'] in POTION_ITEMS else self.el_potions.get(p)
            if effects is None:
                warn(f'unknown potion {p}')
                effects = []
            return {0: potion_color(effects)}
        return {0: ITEM_TINTS[info['item']]} if info['item'] in ITEM_TINTS else {}


ITEM_TINTS = {'minecraft:lily_pad': 0x71C35C}
# items whose model and name depend on NBT; the book only shows these in one state
VARIANTS = {f'{NS}:enigmatic_eye': (f'{NS}:item/enigmatic_eye_active', f'item.{NS}.enigmatic_eye_active')}


# ------------------------------------------------------------------ Java-defined recipes
def java_item(expr, potions):
    """Turns a Java stack expression from EL's source into (item id, potion name|None)."""
    expr = expr.strip()
    m = re.match(r'(?:new ItemStack\()?(?:PotionHelper\.)?createVanillaPotion\(Items\.(\w+),\s*Potions\.(\w+)\)\)?$', expr)
    if m:
        return 'minecraft:' + m.group(1).lower(), m.group(2).lower()
    m = re.match(r'(?:PotionHelper\.)?createAdvancedPotion\(EnigmaticItems\.(\w+),\s*EnigmaticPotions\.(\w+)\)$', expr)
    if m:
        return f'{NS}:' + m.group(1).lower(), potions[m.group(2)][0]
    m = re.match(r'(?:new ItemStack\()?(Items|Blocks|EnigmaticItems)\.(\w+)\)?$', expr)
    if m:
        return (NS if m.group(1) == 'EnigmaticItems' else 'minecraft') + ':' + m.group(2).lower(), None
    raise ValueError('cannot read Java stack ' + expr)


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


def ingredient_of(arg):
    m = re.match(r'Ingredient\.of\((.*)\)$', arg.strip(), re.S)
    if not m:
        raise ValueError('not an Ingredient.of: ' + arg)
    return [a.strip() for a in split_top(m.group(1))]


def split_top(s):
    depth, out, cur = 0, [], ''
    for c in s:
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
        if c == ',' and depth == 0:
            out.append(cur)
            cur = ''
        else:
            cur += c
    out.append(cur)
    return out


def read_java_recipes(res):
    potions = {}   # Java constant -> (name, effects)
    src = (res.el_java / 'registries' / 'EnigmaticPotions.java').read_text(encoding='utf-8')
    for m in re.finditer(r'(\w+)\s*=\s*new AdvancedPotion\("(\w+)"(.*?)\);', src):
        effects = [(e, int(a or 0)) for e, a in
                   re.findall(r'MobEffectInstance\((?:MobEffects|EnigmaticEffects)\.(\w+),\s*\d+(?:,\s*(\d+))?\)', m.group(3))]
        potions[m.group(1)] = (m.group(2), effects)

    def stack(expr):
        return java_item(re.sub(r'\s+', ' ', expr), potions)

    brewing = defaultdict(list)   # recipe id -> [{'input': [...], 'catalyst': [...], 'output': ...}] in registration order
    helper = (res.el_java / 'helpers' / 'PotionHelper.java').read_text(encoding='utf-8')
    main = (res.el_java / 'EnigmaticLegacy.java').read_text(encoding='utf-8')
    # the main class registers recall/twisted potions, then calls PotionHelper's register* methods in this order
    order = ['registerCommonPotions', 'registerBasicUltimatePotions', 'registerSplashUltimatePotions', 'registerLingeringUltimatePotions']
    for m in re.finditer(r'new SpecialBrewingRecipe\(', main):
        args, _ = call_args(main, m.end() - 1)
        inp, cat, out, rid = args
        name = re.search(r'ResourceLocation\(MODID,\s*"(\w+)"\)', rid).group(1)
        brewing[f'{NS}:{name}'].append({'input': [stack(x) for x in ingredient_of(inp)],
                                       'catalyst': [stack(x) for x in ingredient_of(cat)],
                                       'output': stack(re.match(r'new ItemStack\((.*)\)$', out).group(1))})
    for meth in order:
        body_start = helper.index(f'public static void {meth}()')
        body_end = helper.find('public static void ', body_start + 10)
        body = helper[body_start:body_end if body_end > 0 else len(helper)]
        for m in re.finditer(r'new ComplexBrewingRecipe\(', body):
            args, _ = call_args(body, m.end() - 1)
            mp, out = args[0], args[1]
            pairs, _ = call_args(mp, mp.index('('))
            inputs, catalysts = [], []
            for k in range(0, len(pairs), 2):
                inputs += [stack(x) for x in ingredient_of(pairs[k])]
                catalysts += [stack(x) for x in ingredient_of(pairs[k + 1])]
            o = stack(out)
            brewing[f'{NS}:{o[1]}'].append({'input': inputs, 'catalyst': catalysts, 'output': o})

    hidden = {}
    for m in re.finditer(r'HiddenRecipe\.addRecipe\(', main):
        args, _ = call_args(main, m.end() - 1)
        items = [stack(re.match(r'new ItemStack\((.*)\)$', a, re.S).group(1)) for a in args]
        hidden[items[0][0]] = {'output': items[0], 'grid': items[1:]}
    el_potions = {name: effects for name, effects in potions.values()}
    return brewing, hidden, el_potions


# ------------------------------------------------------------------ item icon rendering
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


# Lighting.setupFor3DItems: two diffuse lights, transformed into GUI render space
def _gui_lights():
    m = np.diag([1.0, -1.0, 1.0]) @ rot_matrix('y', -22.5) @ rot_matrix('x', 135)
    ls = []
    for v in ((0.2, 1.0, -0.7), (-0.2, 1.0, 0.7)):
        v = np.array(v) / np.linalg.norm(v)
        ls.append(m @ v)
    return ls
GUI_LIGHTS = _gui_lights()


def render_3d(res, chain, tints):
    get, _ = resolve_textures(chain)
    elements = next((m['elements'] for m in chain if 'elements' in m), [])
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

    def transform(p, er):
        """model pixels -> GUI item space (y up, +z towards the viewer), one unit = the 16px slot"""
        v = np.array(p, dtype=float)
        if er is not None:
            v = er(v)
        v = v / 16 - 0.5
        return R @ (v * scale) + trans

    def to_screen(v):
        return np.array([S / 2 + v[0] * S, S / 2 - v[1] * S, v[2]])

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
            if ref not in textures:
                textures[ref] = load_texture(res, ref)
            tex = textures[ref]
            if tex is None:
                continue
            ti = face.get('tintindex')
            if ti is not None and ti in tints:
                key = (ref, ti)
                if key not in textures:
                    textures[key] = tint_image(tex, tints[ti])
                tex = textures[key]
            arr = np.asarray(tex).astype(np.float32)
            m0, m1, m3 = [transform(c, er) for c in face_corners(d, f, t)]
            c0, c1, c3 = to_screen(m0), to_screen(m1), to_screen(m3)
            ea, eb = c3 - c0, c1 - c0          # a runs along the texture's u, b along v
            det = ea[0] * eb[1] - ea[1] * eb[0]
            if abs(det) < 1e-6:
                continue
            dx, dy = px - c0[0], py - c0[1]
            a = (dx * eb[1] - dy * eb[0]) / det
            b = (ea[0] * dy - ea[1] * dx) / det
            inside = (a >= 0) & (a < 1) & (b >= 0) & (b < 1)
            if not inside.any():
                continue
            z = c0[2] + a * ea[2] + b * eb[2]
            # texture coordinates, honouring the face's uv and rotation (BlockFaceUV)
            uv = face.get('uv') or default_uv(d, f, t)
            shift = (face.get('rotation', 0) // 90) % 4
            def corner_uv(i):
                k = (i + shift) % 4
                return (uv[0] if k in (0, 1) else uv[2], uv[1] if k in (0, 3) else uv[3])
            u0, v0 = corner_uv(0)
            u1, v1 = corner_uv(1)
            u3, v3 = corner_uv(3)
            u = u0 + a * (u3 - u0) + b * (u1 - u0)
            v = v0 + a * (v3 - v0) + b * (v1 - v0)
            th, tw = arr.shape[:2]
            tx = np.clip((u / 16 * tw).astype(int), 0, tw - 1)
            ty = np.clip((v / 16 * th).astype(int), 0, th - 1)
            sample = arr[ty, tx]
            # outward normal, then into render space (y down) for Lighting.setupFor3DItems' two diffuse lights
            n = np.cross(m1 - m0, m3 - m0)
            n = n / (np.linalg.norm(n) or 1)
            n = np.array([n[0], -n[1], n[2]])
            light = 1.0
            if el.get('shade', True):
                light = min(1.0, 0.4 + 0.6 * sum(max(0.0, float(np.dot(L, n))) for L in GUI_LIGHTS))
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
    chain, builtin = model_chain(res, VARIANTS[item][0] if item in VARIANTS else f'{ns}:item/{path}')
    if builtin == 'entity' or not chain:
        im = main_atlas.get(item)
        if im is None:
            warn(f'cannot render {item}')
        return im
    tints = stacks.tint(sid)
    has_elements = any('elements' in m for m in chain)
    if builtin == 'generated' or not has_elements:
        return render_flat(res, chain, tints)
    return render_3d(res, chain, tints)


# ------------------------------------------------------------------ recipes
def read_recipe(res, rl):
    ns, path = split_rl(rl)
    p = res.find('data', f'{ns}:recipes/{path}', '.json')
    if p is None:
        warn(f'missing recipe {rl}')
        return None
    return res.json(p)


def ingredient(stacks, ing):
    if ing is None:
        return []
    if isinstance(ing, list):
        out = []
        for i in ing:
            out += ingredient(stacks, i)
        return out
    if 'item' in ing:
        return [stacks.plain(ing['item'])]
    warn(f'unsupported ingredient {ing}')
    return []


def result_stack(stacks, r):
    if isinstance(r, str):
        return stacks.plain(r)
    s = stacks.plain(r['item'])
    return {'i': s, 'c': r['count']} if r.get('count', 1) != 1 else s


def convert_crafting(stacks, rec):
    if 'pattern' in rec:
        pat = rec['pattern']
        w = max(len(row) for row in pat)
        cells = []
        for row in pat:
            for ch in row.ljust(w):
                cells.append([] if ch == ' ' else ingredient(stacks, rec['key'].get(ch)))
        return {'out': result_stack(stacks, rec['result']), 'toast': stacks.plain('minecraft:crafting_table'), 'shaped': 1, 'w': w, 'in': cells}
    if 'ingredients' in rec:
        return {'out': result_stack(stacks, rec['result']), 'toast': stacks.plain('minecraft:crafting_table'), 'shaped': 0,
                'in': [ingredient(stacks, i) for i in rec['ingredients']]}
    warn(f'unsupported crafting recipe type {rec.get("type")}')
    return None


TOASTS = {'minecraft:smelting': 'minecraft:furnace', 'minecraft:blasting': 'minecraft:blast_furnace',
          'minecraft:smoking': 'minecraft:smoker', 'minecraft:campfire_cooking': 'minecraft:campfire'}


def convert_cooking(stacks, rec):
    return {'in': [ingredient(stacks, rec['ingredient'])], 'out': result_stack(stacks, rec['result']),
            'toast': stacks.plain(TOASTS.get(rec['type'], 'minecraft:furnace'))}


# ------------------------------------------------------------------ book
def img_path(rl):
    """enigmaticlegacy:textures/gui/x.png -> img/enigmaticlegacy/textures/gui/x.png (copied there by copy_image)"""
    ns, path = split_rl(rl)
    return f'img/{ns}/{path}'


def copy_image(res, rl):
    ns, path = split_rl(rl)
    src = res.find('assets', f'{ns}:{path}', '')
    if src is None:
        warn(f'missing image {rl}')
        return None
    dst = ROOT / img_path(rl)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return img_path(rl)


class BookBuilder:
    def __init__(self, res, stacks, brewing, hidden):
        self.res, self.stacks, self.brewing, self.hidden = res, stacks, brewing, hidden
        self.book_dir = res.el_res / 'assets' / NS / 'patchouli_books' / BOOK / 'en_us'
        self.templates = {p.stem: json.loads(p.read_text(encoding='utf-8')) for p in (self.book_dir / 'templates').glob('*.json')}

    def icon(self, s):
        if ':textures/' in s or s.endswith('.png'):
            p = copy_image(self.res, s)
            return {'tex': p} if p else None
        return self.stacks.parse(s)

    def stack_list(self, s):
        if s is None:
            return []
        if isinstance(s, list):
            return [x for v in s for x in self.stack_list(v)]
        parts, depth, cur = [], 0, ''
        for ch in s:     # comma-separated stacks; commas inside {nbt} belong to the stack
            depth += (ch in '{[') - (ch in '}]')
            if ch == ',' and depth == 0:
                parts.append(cur)
                cur = ''
            else:
                cur += ch
        parts.append(cur)
        return [x for x in (self.stacks.parse(p) for p in parts) if x]

    # Patchouli template components -> the viewer's template components
    def template(self, name, page, values):
        comps = []
        for c in self.templates[name]['components']:
            typ = c['type'].split(':')[-1]
            def val(v):
                if isinstance(v, str) and v.startswith('#'):
                    return values.get(v[1:], page.get(v[1:]))
                return v
            x, y = c.get('x', 0), c.get('y', 0)
            if typ == 'header':
                comps.append({'t': 'header', 'x': PAGE_WIDTH // 2 if x == -1 else x, 'y': 0 if y == -1 else y,
                              'text': self.res.t(val(c['text']) or ''), 'centered': x == -1 or c.get('centered', True)})
            elif typ == 'text':
                comps.append({'t': 'text', 'x': x, 'y': y, 'w': c.get('max_width', PAGE_WIDTH), 'text': self.res.t(val(c['text']) or '')})
            elif typ == 'image':
                src = copy_image(self.res, c['image'])
                comps.append({'t': 'image', 'x': x, 'y': y, 'src': src, 'u': c.get('u', 0), 'v': c.get('v', 0),
                              'w': c['width'], 'h': c['height'], 'tw': c.get('texture_width', 256), 'th': c.get('texture_height', 256),
                              'scale': c.get('scale', 1)})
            elif typ == 'item':
                v = val(c['item'])
                items = v if isinstance(v, list) else self.stack_list(v)
                comps.append({'t': 'item', 'x': x, 'y': y, 'items': items, 'framed': str(c.get('framed', False)).lower() == 'true'})
            else:
                warn(f'unsupported template component {typ}')
        return {'type': 'template', 'c': comps}

    def java_stack(self, item_potion):
        item, potion = item_potion
        if potion is None:
            return self.stacks.plain(item)
        sid = self.stacks.potion_id(item, potion)
        return {'i': sid, 'g': 1} if self.stacks.has_glint(sid) else sid

    def page(self, p, stack_links, index):
        t = self.res.t
        typ = p['type']
        text = t(p.get('text', ''))
        if typ == 'text':
            out = {'type': 'text', 'text': text}
            if p.get('title'):
                out['title'] = t(p['title'])
            return out
        if typ == 'crafting':
            recs = []
            for k in ('recipe', 'recipe2'):
                if p.get(k):
                    rec = read_recipe(self.res, p[k])
                    r = rec and convert_crafting(self.stacks, rec)
                    if r:
                        recs.append(r)
                        stack_links.append([r['out'], index])
            out = {'type': 'crafting', 'r': recs, 'text': text}
            if p.get('title'):
                out['title'] = t(p['title'])
            return out
        if typ in ('smelting', 'blasting', 'smoking', 'campfire_cooking'):
            recs = []
            for k in ('recipe', 'recipe2'):
                if p.get(k):
                    rec = read_recipe(self.res, p[k])
                    if rec:
                        r = convert_cooking(self.stacks, rec)
                        recs.append(r)
                        stack_links.append([r['out'], index])
            out = {'type': 'processing', 'r': recs, 'text': text}
            if p.get('title'):
                out['title'] = t(p['title'])
            return out
        if typ == 'spotlight':
            items = self.stack_list(p.get('item'))
            if p.get('link_recipe') and items:
                stack_links.append([items[0], index])
            out = {'type': 'spotlight', 'items': items, 'text': text}
            if p.get('title'):
                out['title'] = t(p['title'])
            return out
        if typ == 'image':
            out = {'type': 'image', 'images': [copy_image(self.res, i) for i in p['images']], 'border': bool(p.get('border')), 'text': text}
            if p.get('title'):
                out['title'] = t(p['title'])
            return out
        if typ == 'empty':
            return {'type': 'empty', 'filler': p.get('draw_filler', True)}
        if typ == f'{NS}:brewing':
            r = self.brewing.get(p['recipe'], [])
            i = int(p.get('index', 0))
            if i >= len(r):
                warn(f'no brewing recipe {p["recipe"]}[{i}]')
                return self.template('brewing', p, {'heading': p.get('heading')})
            rec = r[i]
            out = self.java_stack(rec['output'])
            stack_links.append([out, index])
            return self.template('brewing', p, {
                'catalyst': [self.java_stack(s) for s in rec['catalyst']],
                'input': [self.java_stack(s) for s in rec['input']],
                'output': [out]})
        if typ == f'{NS}:hidden_crafting':
            h = self.hidden.get(p['recipe'])
            if not h:
                warn(f'no hidden recipe {p["recipe"]}')
                return self.template('hidden_crafting', p, {})
            vals = {f'input{k + 1}': [self.java_stack(s)] for k, s in enumerate(h['grid'])}
            vals['output'] = [self.java_stack(h['output'])]
            stack_links.append([vals['output'][0], index])
            return self.template('hidden_crafting', p, vals)
        if typ == f'{NS}:armor_showcase':
            return self.template('armor_showcase', p, {f'input{k}': self.stack_list(p.get(f'input{k}')) for k in range(1, 5)})
        warn(f'unsupported page type {typ}')
        return {'type': 'text', 'text': text}

    def build(self):
        res, t = self.res, self.res.t
        bj = res.json(res.find('data', f'{NS}:patchouli_books/{BOOK}/book', '.json'))
        cats = []
        for f in sorted((self.book_dir / 'categories').glob('**/*.json')):
            c = res.json(f)
            cid = f'{NS}:' + f.relative_to(self.book_dir / 'categories').with_suffix('').as_posix()
            cat = {'id': cid, 'name': t(c['name']), 'desc': t(c.get('description', '')), 'icon': self.icon(c['icon']),
                   'sortnum': c.get('sortnum', 0)}
            if c.get('parent'):
                cat['parent'] = c['parent'] if ':' in c['parent'] else f'{NS}:{c["parent"]}'
            cats.append(cat)
        entries = []
        for f in sorted((self.book_dir / 'entries').glob('**/*.json')):
            e = res.json(f)
            eid = f'{NS}:' + f.relative_to(self.book_dir / 'entries').with_suffix('').as_posix()
            links = []
            pages = []
            for i, p in enumerate(e['pages']):
                pg = self.page(p, links, i)
                if p.get('anchor'):
                    pg['anchor'] = p['anchor']
                pages.append(pg)
            cat = e['category'] if ':' in e['category'] else f'{NS}:{e["category"]}'
            entry = {'id': eid, 'name': t(e['name']), 'icon': self.icon(e['icon']), 'category': cat, 'pages': pages,
                     'sortnum': e.get('sortnum', 0)}
            for k, dk in (('priority', 'priority'), ('secret', 'secret'), ('read_by_default', 'readByDefault')):
                if e.get(k):
                    entry[dk] = 1
            if links:
                entry['stacks'] = links
            entries.append(entry)
        book = {
            'id': f'{NS}:{BOOK}', 'name': t(bj['name']), 'landing': t(bj.get('landing_text', '')),
            'texture': copy_image(res, bj['book_texture']), 'filler': copy_image(res, bj['filler_texture']),
            'crafting': 'img/patchouli/textures/gui/crafting.png', 'version': 1,
            # The book's "version" is a placeholder the mod fills in at runtime; name the mod instead of an edition.
            'subtitle': 'Enigmatic Legacy',
            'showProgress': bool(bj.get('show_progress', True)), 'advancementsEnabled': True,
            'icon': self.stacks.plain(bj.get('custom_book_item', f'{NS}:{BOOK}')),
            'textColor': bj.get('text_color', '000000'), 'headerColor': bj.get('header_color', '333333'),
            'nameplateColor': bj.get('nameplate_color', 'FFDD00'), 'linkColor': bj.get('link_color', '0000EE'),
            'linkHoverColor': bj.get('link_hover_color', '8800EE'), 'progressBarColor': bj.get('progress_bar_color', 'FFFF55'),
            'progressBarBackground': bj.get('progress_bar_background', 'DDDDDD'), 'macros': bj.get('macros', {}),
            # In game this book uses the narrow "uniform" font (use_blocky_font: false), so its text fits the page.
            # The viewer draws the default font, which is wider; shrinking overflowing text keeps every page readable.
            'overflow': 'resize', 'categories': cats, 'entries': entries,
        }
        return book


# ------------------------------------------------------------------ main
def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    res = Resources(Path(sys.argv[1]), Path(sys.argv[2]))
    brewing, hidden, el_potions = read_java_recipes(res)
    stacks = Stacks(res, el_potions)
    if (IMG_OUT).exists():
        shutil.rmtree(IMG_OUT)
    book = BookBuilder(res, stacks, brewing, hidden).build()

    OUT.mkdir(parents=True, exist_ok=True)
    ids = sorted(stacks.ids)
    rows = math.ceil(len(ids) / ATLAS_COLS)
    atlas = Image.new('RGBA', (ATLAS_COLS * ICON, rows * ICON))
    index, names = {}, {}
    main_atlas = MainAtlas()
    for n, sid in enumerate(ids):
        im = render_icon(res, sid, stacks, main_atlas)
        if im is not None:
            atlas.alpha_composite(im, ((n % ATLAS_COLS) * ICON, (n // ATLAS_COLS) * ICON))
        index[sid] = n
        names[sid] = stacks.name(sid)
    atlas.save(OUT / 'items.png', optimize=True)
    data = {
        'books': [book],
        'itemNames': names,
        'icons': {'file': f'packs/{NS}/items.png', 'size': ICON, 'cols': ATLAS_COLS, 'index': index},
    }
    (OUT / 'data.json').write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print(f'{len(book["entries"])} entries, {len(book["categories"])} categories, {len(ids)} icons -> {OUT.relative_to(ROOT)}')
    if warnings:
        print(f'{len(warnings)} warnings')


if __name__ == '__main__':
    main()
