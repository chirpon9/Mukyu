#!/usr/bin/env python3
"""Builds the Blood Magic pack (packs/bloodmagic/) for the web book viewer.

The pack is generated from Blood Magic's own published resources:
  - the Patchouli book "Sanguine Scientiem" (categories, entries, templates, en_us text)
  - recipes, language file and models from the mod's generated resources
  - ritual and Blood Altar structures and ritual costs, which the mod defines in Java, read from its source
    (only the simple loops and calls those methods use are interpreted; no code from the mod is run)
  - item icons and multiblock block models, rendered here from the mod's models and textures

Usage:
  python tools/build_bloodmagic.py <blood-magic> <forge> <patchouli> <vanilla>

  <blood-magic>  a checkout of https://github.com/WayofTime/BloodMagic (branch 1.20.1)
  <forge>        a checkout of https://github.com/MinecraftForge/MinecraftForge (branch 1.20.x); its
                 src/main/generated/data/forge/tags holds the forge:* item tags recipes use
  <patchouli>    a folder holding assets/patchouli/textures/gui/book_red.png from Patchouli (1.20.1 branch)
  <vanilla>      a folder holding assets/minecraft and data/minecraft extracted from the 1.20.1 client jar

Requires Pillow and numpy.
"""
import re
import shutil
import sys
from pathlib import Path

from packlib import (ROOT, BookBuilder, Resources, Stacks, eval_int, ingredient, java_format, method_body, read_recipe,
                     result_stack, run_java_block, split_top, strip_java_comments, warn, write_pack)

NS = 'bloodmagic'
BOOK = 'guide'


# ------------------------------------------------------------------ registry names from Java
def registry_names(java):
    """Java constant -> registry id, from BloodMagicItems/BloodMagicBlocks' register("id", ...) calls."""
    names = {}
    for f in (java / 'common' / 'item' / 'BloodMagicItems.java', java / 'common' / 'block' / 'BloodMagicBlocks.java'):
        for m in re.finditer(r'(\w+)\s*=\s*\w+\.register\("([a-z0-9_/]+)"', f.read_text(encoding='utf-8')):
            names.setdefault(m.group(1), f'{NS}:{m.group(2)}')
    return names


# ------------------------------------------------------------------ rituals
RUNE_TYPES = ['BLANK', 'WATER', 'FIRE', 'EARTH', 'AIR', 'DUSK', 'DAWN']          # EnumRuneType order
RUNE_CHARS = {'BLANK': 'B', 'WATER': 'W', 'FIRE': 'F', 'EARTH': 'E', 'AIR': 'A', 'DUSK': 'D', 'DAWN': 'd'}
RITUAL_BLOCKS = {'0': 'masterritualstone', 'B': 'ritualstone', 'W': 'waterritualstone', 'F': 'fireritualstone',
                 'E': 'earthritualstone', 'A': 'airritualstone', 'D': 'duskritualstone', 'd': 'lightritualstone'}


def rune_calls(components):
    """on_call for run_java_block: Ritual's addRune/addOffsetRunes/addCornerRunes/addParallelRunes."""
    def on_call(stmt, env):
        stmt = re.sub(r'^this\.', '', stmt)
        m = re.match(r'components\.accept\(new RitualComponent\(new BlockPos\(([^)]*)\)\s*,\s*EnumRuneType\.(\w+)\)\)$', stmt)
        if m:
            components.append((tuple(eval_int(a, env) for a in split_top(m.group(1))), m.group(2)))
            return
        m = re.match(r'(addRune|addOffsetRunes|addCornerRunes|addParallelRunes)\s*\((.*)\)$', stmt, re.S)
        if not m:
            if stmt:
                warn(f'ritual code not understood: {stmt[:80]}')
            return
        args = [a.strip() for a in split_top(m.group(2))][1:]          # drop the `components` consumer
        rune = args[-1].split('.')[-1]
        v = [eval_int(a, env) for a in args[:-1]]
        add = lambda x, y, z: components.append(((x, y, z), rune))
        if m.group(1) == 'addRune':
            add(v[0], v[1], v[2])
        elif m.group(1) == 'addOffsetRunes':
            a, b, y = v
            for x, z in ((a, b), (b, a), (a, -b), (-b, a), (-a, b), (b, -a), (-a, -b), (-b, -a)):
                add(x, y, z)
        elif m.group(1) == 'addCornerRunes':
            o, y = v
            for x, z in ((o, o), (o, -o), (-o, -o), (-o, o)):
                add(x, y, z)
        else:
            o, y = v
            for x, z in ((o, 0), (-o, 0), (0, -o), (0, o)):
                add(x, y, z)
    return on_call


def config_defaults(java):
    """ConfigManager's integer defaults (builder.define("name", 50_000)), for values rituals read from the config."""
    src = (java / 'ConfigManager.java').read_text(encoding='utf-8')
    return {k: int(v.replace('_', '')) for k, v in re.findall(r'(\w+)\s*=\s*builder[^;]*?\.define(?:InRange)?\(\s*"[^"]+"\s*,\s*([\d_]+)', src)}


def read_rituals(java, config):
    rituals = {}
    for f in sorted((java / 'ritual').rglob('*.java')):
        src = f.read_text(encoding='utf-8')
        rm = re.search(r'@RitualRegister\("(\w+)"', src)
        if not rm:
            continue
        src = strip_java_comments(src)
        consts = dict(re.findall(r'static\s+final\s+String\s+(\w+)\s*=\s*"([^"]*)"', src))
        ints = {k: int(v) for k, v in re.findall(r'static\s+final\s+int\s+(\w+)\s*=\s*(-?\d+)\s*;', src)}
        for k, c in re.findall(r'static\s+final\s+int\s+(\w+)\s*=\s*ConfigManager\.COMMON\.(\w+)\.get\(\)', src):
            ints[k] = config[c]
        # instance fields with an initial value (e.g. `private int refreshTime = 20;`), for getters that return them
        fields = {k: int(v) for k, v in re.findall(r'(?:private|protected|public)\s+int\s+(\w+)\s*=\s*(-?\d+)\s*;', src)}
        sm = re.search(r'super\s*\(', src)
        args = [a.strip() for a in split_top(src[sm.end():src.index(';', sm.end())].rsplit(')', 1)[0])]
        # the translation key is a string concatenation such as "ritual." + BloodMagic.MODID + ".waterRitual"
        key = ''.join(NS if 'MODID' in p else p.strip().strip('"') for p in args[-1].split('+'))
        ritual = {'id': rm.group(1), 'key': key, 'crystal': eval_int(args[1], ints), 'activation': eval_int(args[2], ints),
                  'ranges': {}, 'components': [], 'refresh_cost': 0, 'refresh_time': 20}
        for m in re.finditer(r'setMaximumVolumeAndDistanceOfRange\s*\(', src):
            rng, *nums = [a.strip() for a in split_top(src[m.end():src.index(';', m.end())].rsplit(')', 1)[0])]
            try:
                ritual['ranges'][consts.get(rng, rng)] = tuple(eval_int(a, ints) for a in nums)
            except (ValueError, KeyError):
                warn(f'{f.name}: range {rng} uses values that are not constants')
        for name, field in (('getRefreshCost', 'refresh_cost'), ('getRefreshTime', 'refresh_time')):
            body = method_body(src, r'int\s+' + name + r'\s*\(\s*\)\s*\{')
            rv = body and re.search(r'return\s+(\w+)\s*;', body)
            if rv:
                try:
                    ritual[field] = eval_int(rv.group(1), {**fields, **ints})
                except (ValueError, KeyError):
                    warn(f'{f.name}: {name} returns {rv.group(1)}, not a constant')
        body = method_body(src, r'void\s+gatherComponents\s*\([^)]*\)\s*\{')
        if body is None:
            warn(f'no gatherComponents in {f.name}')
        else:
            run_java_block(body, dict(ints), rune_calls(ritual['components']))
        rituals[ritual['id']] = ritual
    return rituals


def make_pattern(cells, char_of):
    """RegisterPatchouliMultiblocks.makePattern: rows of z for each x, layers from top to bottom."""
    xs = [p[0] for p in cells] + [0]
    ys = [p[1] for p in cells] + [0]
    zs = [p[2] for p in cells] + [0]
    pattern = []
    for y in range(max(ys), min(ys) - 1, -1):
        layer = []
        for x in range(min(xs), max(xs) + 1):
            row = ''
            for z in range(min(zs), max(zs) + 1):
                c = cells.get((x, y, z))
                row += char_of(c) if c is not None else ('0' if (x, y, z) == (0, 0, 0) else '_')
            layer.append(row)
        pattern.append(layer)
    return pattern


def ritual_multiblocks(rituals):
    out = {}
    for rid, r in rituals.items():
        cells = {pos: rune for pos, rune in r['components']}
        pattern = make_pattern(cells, lambda rune: RUNE_CHARS[rune])
        if rid == 'downgrade':           # the mod's manual override that adds the chest
            pattern[2][3] = '_FDC______'
        mapping = {c: f'{NS}:{b}' for c, b in RITUAL_BLOCKS.items()}
        mapping['C'] = 'minecraft:chest'
        out[f'{NS}:{rid}'] = {'pattern': pattern, 'mapping': mapping}
    return out


ALTAR_CHARS = {'BLOODRUNE': 'R', 'NOTAIR': 'P', 'GLOWSTONE': 'G', 'BLOODSTONE': 'S', 'BEACON': 'B', 'CRYSTAL': 'C'}
# blocks shown for each altar component; in game Patchouli cycles through every block that qualifies
ALTAR_BLOCKS = {'0': f'{NS}:altar', 'R': f'{NS}:speedrune', 'r': f'{NS}:blankrune', 'P': 'minecraft:stone_bricks',
                'G': 'minecraft:glowstone', 'S': f'{NS}:largebloodstonebrick', 'B': 'minecraft:beacon'}


def altar_multiblocks(java):
    src = strip_java_comments((java / 'altar' / 'AltarTier.java').read_text(encoding='utf-8'))
    tiers = {}
    for m in re.finditer(r'\b(ONE|TWO|THREE|FOUR|FIVE|SIX)\s*\(\s*\)\s*\{', src):
        body = method_body(src[m.start():], r'void\s+buildComponents\s*\([^)]*\)\s*\{')
        comps = []
        def on_call(stmt, env, comps=comps):
            mm = re.match(r'components\.accept\(new AltarComponent\(new BlockPos\(([^)]*)\)(?:\s*,\s*ComponentType\.(\w+))?\)', stmt)
            if mm:
                pos = tuple(eval_int(a, env) for a in split_top(mm.group(1)))
                comps.append((pos, mm.group(2) or 'NOTAIR'))
                return
            mm = re.match(r'(\w+)\.getAltarComponents\(\)\.forEach\(components\)', stmt)
            if mm:
                comps.extend(tiers[mm.group(1)])
                return
            if stmt:
                warn(f'altar code not understood: {stmt[:80]}')
        run_java_block(body, {}, on_call)
        tiers[m.group(1)] = comps
    out = {}
    for name, comps in tiers.items():
        if name == 'ONE':
            pattern = [['0'], ['_']]
        elif name == 'TWO':              # the mod's special case: unupgraded runes in the corners
            pattern = [['___', '_0_', '___'], ['rRr', 'R_R', 'rRr']]
        else:
            pattern = make_pattern(dict(comps), lambda c: ALTAR_CHARS[c])
        out[f'{NS}:altar_{name.lower()}'] = {'pattern': pattern, 'mapping': dict(ALTAR_BLOCKS)}
    return out


# ------------------------------------------------------------------ page processors (ports of Blood Magic's Java ones)
def recipe_of(builder, vars, kind):
    rid = vars.raw('recipe')
    rec = read_recipe(builder.res, rid) if rid else None
    if rec is not None and kind not in rec.get('type', ''):
        warn(f'recipe {rid} is {rec.get("type")}, expected {kind}')
        rec = None
    return rec


class Processor:
    kind = ''
    def __init__(self, builder, vars):
        self.b, self.s, self.vars = builder, builder.stacks, vars
        self.recipe = recipe_of(builder, vars, self.kind) if self.kind else None
        self.setup()

    def setup(self):
        pass

    def ing(self, i):
        return ingredient(self.s, i)

    def orb(self, tier):
        """AlchemyTableProcessor/PotionFlaskProcessor: the weakest Blood Orb that reaches the recipe's tier."""
        names = {0: 'WEAK_BLOOD_ORB', 1: 'WEAK_BLOOD_ORB', 2: 'APPRENTICE_BLOOD_ORB', 3: 'MAGICIAN_BLOOD_ORB', 4: 'MASTER_BLOOD_ORB'}
        return [self.b.item(names[tier]) if tier in names else 'minecraft:barrier']


class BloodAltarProcessor(Processor):
    kind = 'altar'
    def process(self, key):
        r = self.recipe
        if r is None:
            return None
        return {'input': lambda: self.ing(r['input']), 'output': lambda: [result_stack(self.s, r['output'])],
                'tier': lambda: r.get('upgradeLevel', 0) + 1, 'lp': lambda: r.get('altarSyphon', 0)}.get(key, lambda: None)()


class AlchemyTableProcessor(Processor):
    kind = 'alchemytable'
    def process(self, key):
        r = self.recipe
        if r is None:
            return None
        m = re.match(r'input(\d+)$', key)
        if m:
            i = int(m.group(1)) - 1
            return self.ing(r['input'][i]) if i < len(r['input']) else None
        return {'output': lambda: [result_stack(self.s, r['output'])], 'syphon': lambda: r.get('syphon', 0),
                'time': lambda: r.get('ticks', 0), 'tier': lambda: r.get('upgradeLevel', 0),
                'orb': lambda: self.orb(r.get('upgradeLevel', 0))}.get(key, lambda: None)()


class TartaricForgeProcessor(Processor):
    kind = 'soulforge'
    def process(self, key):
        r = self.recipe
        if r is None:
            return None
        m = re.match(r'input(\d+)$', key)
        if m:
            i = int(m.group(1)) - 1
            return self.ing(r[f'input{i}']) if f'input{i}' in r else None
        souls = r.get('minimumDrain', 0)
        if key == 'output':
            return [result_stack(self.s, r['output'])]
        if key == 'willrequired':
            return fmt_num(souls)
        if key == 'willdrain':
            return fmt_num(r.get('drain', 0))
        if key == 'will':
            # the smallest container that holds the minimum will
            for limit, name in ((1, 'MONSTER_SOUL_RAW'), (64, 'PETTY_GEM'), (256, 'LESSER_GEM'), (1024, 'COMMON_GEM'), (4096, 'GREATER_GEM')):
                if souls <= limit:
                    return [self.b.item(name)]
            return ['minecraft:barrier']
        return None


def fmt_num(v):
    """Java's double-to-string, as the forge page prints will amounts (80.0, 2.5)."""
    return f'{float(v)}' if isinstance(v, (int, float)) else v


class ARCProcessor(Processor):
    kind = 'arc'
    def fluid_text(self, f):
        if not f:
            return self.b.res.localize('guide.patchouli.bloodmagic.arc_processor.no_fluid')
        ns, path = f['fluid'].split(':')
        name = self.b.res.lang.get(f'fluid_type.{ns}.{path}') or self.b.res.lang.get(f'block.{ns}.{path}') or path
        return self.b.res.localize('guide.patchouli.bloodmagic.arc_processor.fluid', f.get('amount', 0), name)

    def process(self, key):
        r = self.recipe
        if r is None:
            return None
        outputs = [result_stack(self.s, r['output'])] + [result_stack(self.s, a['type']) for a in r.get('addedoutput', [])]
        chances = [(a.get('chance', 0), a.get('mainchance', 0)) for a in r.get('addedoutput', [])]
        m = re.match(r'(output|chance|show_chance)(\d+)$', key)
        if m:
            i = int(m.group(2))
            if m.group(1) == 'output':
                return [outputs[i - 1]] if i - 1 < len(outputs) else None
            if i - 2 >= len(chances) or i < 2:
                return None
            if m.group(1) == 'show_chance':
                return True
            c = sum(chances[i - 2]) * 100
            return '<1' if c < 1 else round(c)
        fin, fout = r.get('inputFluid'), r.get('outputFluid')
        return {'show_fluid_tooltip': lambda: bool(fin or fout), 'input': lambda: self.ing(r['input']),
                'tool': lambda: self.ing(r['tool']), 'tooltip_fluid_input': lambda: self.fluid_text(fin),
                'tooltip_fluid_output': lambda: self.fluid_text(fout)}.get(key, lambda: None)()


class PotionFlaskProcessor(Processor):
    kind = 'flask'
    def flask(self, effects=(), item=None):
        """An alchemy flask; its effects are listed in the tooltip like the game's flask tooltip."""
        s = {'i': self.s.plain(item or f'{NS}:alchemy_flask')}
        lore = [self.b.res.t('effect.' + e.replace(':', '.')) for e in effects]
        if lore:
            s['l'] = lore
        return s

    def setup(self):
        r = self.recipe or {}
        t = r.get('type', '').split('_')[-1]
        effect = [r['effect']] if r.get('effect') else []
        if t == 'potioneffect':            # adds an effect to an empty flask
            self.example, self.output = self.flask(), self.flask(effect)
        elif t == 'potiontransform':
            self.example = self.flask(r.get('inputEffect', []))
            self.output = self.flask([o['effect'] for o in r.get('outputEffect', [])])
        elif t == 'potionflasktransform':
            self.example, self.output = self.flask(), result_stack(self.s, r['output'])
        else:                              # length / potency / fill / cycle change the flask's existing effect
            self.example = self.flask(effect)
            self.output = self.flask(effect)

    def process(self, key):
        r = self.recipe
        if r is None:
            return None
        m = re.match(r'input(\d+)$', key)
        if m:
            i = int(m.group(1)) - 1
            if i < len(r['input']):
                return self.ing(r['input'][i])
            return [self.example] if i == len(r['input']) else None
        return {'output': lambda: [self.output], 'syphon': lambda: r.get('syphon', 0), 'time': lambda: r.get('ticks', 0),
                'tier': lambda: r.get('upgradeLevel', 0), 'orb': lambda: self.orb(r.get('upgradeLevel', 0))}.get(key, lambda: None)()


class AlchemyArrayProcessor(Processor):
    kind = 'array'
    def process(self, key):
        r = self.recipe
        if r is None:
            return None
        return {'baseinput': lambda: self.ing(r['baseinput']), 'addedinput': lambda: self.ing(r['addedinput']),
                'output': lambda: [result_stack(self.s, r['output'])]}.get(key, lambda: None)()


class LivingArmourDowngradeRecipeProcessor(Processor):
    kind = 'livingdowngrade'
    def process(self, key):
        return self.ing(self.recipe['input']) if self.recipe and key == 'input' else None


class LivingArmourUpgradeInfoTable(Processor):
    def setup(self):
        uid = self.vars.raw('upgrade') or ''
        p = (self.b.res.find('data', uid.replace(':', ':living_armor/', 1), '.json')
             or self.b.res.find('data', uid.replace(':', ':living_armor/downgrade/', 1), '.json'))
        self.upgrade = self.b.res.json(p) if p else None
        if self.upgrade is None:
            warn(f'unknown living armour upgrade {uid}')
        self.extra = self.vars.raw('text') or ''

    def process(self, key):
        if self.upgrade is None or key != 'table':
            return None
        res = self.b.res
        levels = self.upgrade['levels']
        lvl_word = res.localize('guide.patchouli.bloodmagic.living_armour_upgrade_table.level')
        pts_word = res.localize('guide.patchouli.bloodmagic.living_armour_upgrade_table.upgrade_points')
        lw, pw = len(str(len(levels))), len(str(levels[-1]['cost']))
        out = ''
        for i, lv in enumerate(levels):        # LivingUpgrade: one line per experience threshold, in order
            out += java_format(f'%s %{lw}d: %{pw}d %s$(br)', lvl_word, i + 1, lv['cost'], pts_word)
        return out + '$(br2)' + self.extra


class RitualInfoProcessor(Processor):
    LANG = 'guide.patchouli.bloodmagic.ritual_info.'
    DIVINER = 'tooltip.bloodmagic.diviner.'
    CRYSTALS = {'corrosive': 'CORROSIVE_CRYSTAL', 'destructive': 'DESTRUCTIVE_CRYSTAL', 'steadfast': 'STEADFAST_CRYSTAL',
                'vengeful': 'VENGEFUL_CRYSTAL'}

    def setup(self):
        res = self.b.res
        self.ritual = self.b.rituals.get(self.vars.raw('ritual') or '')
        if self.ritual is None:
            warn(f'unknown ritual {self.vars.raw("ritual")}')
            return
        self.page_type = self.vars.raw('page_type') or 'info'
        self.heading, self.item, info_set, range_info = '', None, False, ''
        key = self.ritual['key']
        if self.page_type == 'info':
            self.blurb, info_set = res.localize(key + '.info'), True
        elif self.page_type == 'raw':
            self.blurb, info_set = res.localize(key + '.default.info'), True
            self.heading = self.lead_title()
            self.item = [self.b.item('RAW_CRYSTAL')]
        elif self.page_type in self.CRYSTALS:
            self.item = [self.b.item(self.CRYSTALS[self.page_type])]
        else:
            vol, h, v = self.ritual['ranges'].get(self.page_type, (0, 0, 0))
            if self.page_type not in self.ritual['ranges']:
                warn(f'ritual {self.ritual["id"]} has no range {self.page_type}')
            range_info = res.localize(self.LANG + 'range_formatter',
                                      res.localize(self.LANG + 'full_range') if vol == 0 else vol, h, v)
        if not info_set:
            self.blurb = res.localize(f'{key}.{self.page_type}.info')
            self.heading = self.lead_title()
        self.blurb += range_info
        for text, code in self.vars.raw('text_overrides') or []:
            repl = res.localize(self.LANG + 'text_override_formatter', code, text).replace('\\$', '$')
            self.blurb = re.sub(r'\b%s\b' % re.escape(text), lambda m: repl, self.blurb)
        if self.vars.raw('heading_override'):
            self.heading = self.vars.raw('heading_override')
        if self.vars.raw('item_override'):
            self.item = self.b.stack_list(self.vars.raw('item_override'))
        self.extra = self.vars.raw('text') or ''

    def lead_title(self):
        """For blurbs that start with "(Title) ...", the title becomes the heading and is removed from the text."""
        if self.blurb.startswith('('):
            title = self.blurb[1:self.blurb.index(')')]
            self.blurb = re.sub(r'^\(' + re.escape(title) + r'\) ', '', self.blurb, count=1)
            return title
        return ''

    def info_page(self):
        res, r = self.b.res, self.ritual
        counts = {}
        for _, rune in r['components']:
            counts[rune] = counts.get(rune, 0) + 1
        runes = ''.join(res.localize(self.LANG + 'counter_formatter', f'$({t.lower()})',
                                     res.localize(self.DIVINER + t.lower() + 'Rune', counts[t]))
                        for t in RUNE_TYPES if counts.get(t))
        total = res.localize(self.DIVINER + 'totalRune', len(r['components']))
        crystal = {0: lambda: res.localize(self.LANG + 'weak_activation_crystal_link', res.localize('item.bloodmagic.activationcrystalweak')),
                   1: lambda: res.localize(self.LANG + 'awakened_activation_crystal_link', res.localize('item.bloodmagic.activationcrystalawakened'))
                   }.get(r['crystal'], lambda: res.localize('item.bloodmagic.activationcrystalcreative'))()
        cost = res.localize(self.LANG + 'activation_cost', r['activation'])
        upkeep = res.localize(self.LANG + 'upkeep_cost', r['refresh_cost'], r['refresh_time']) if r['refresh_cost'] else ''
        return res.localize(self.LANG + 'info_formatter', self.blurb, runes, total, crystal, cost, upkeep)

    def process(self, key):
        if self.ritual is None:
            return None
        if key == 'auto_text':
            text = self.info_page() if self.page_type == 'info' else self.blurb
            return text + self.b.res.localize('guide.patchouli.bloodmagic.common.double_new_line', self.extra)
        if key == 'heading':
            return self.heading
        if key == 'item':
            return self.item
        return None


PROCESSORS = {c.__name__: c for c in (BloodAltarProcessor, AlchemyTableProcessor, TartaricForgeProcessor, ARCProcessor,
                                      PotionFlaskProcessor, AlchemyArrayProcessor, LivingArmourDowngradeRecipeProcessor,
                                      LivingArmourUpgradeInfoTable, RitualInfoProcessor)}


def main():
    if len(sys.argv) != 5:
        print(__doc__)
        sys.exit(2)
    bm, forge, patchouli, vanilla = (Path(a) for a in sys.argv[1:])
    res = Resources([bm / 'src' / 'main' / 'resources', bm / 'src' / 'generated' / 'resources',
                     forge / 'src' / 'main' / 'generated', patchouli, vanilla])
    java = bm / 'src' / 'main' / 'java' / 'wayoftime' / 'bloodmagic'
    names = registry_names(java)

    stacks = Stacks(res)
    # Patchouli's guide book item carries this book's model and name
    stacks.variants = {'patchouli:guide_book': (f'{NS}:item/book', 'guide.bloodmagic.name')}
    # IngredientBloodOrb: any Blood Orb of at least the given tier
    orbs = ['WEAK_BLOOD_ORB', 'APPRENTICE_BLOOD_ORB', 'MAGICIAN_BLOOD_ORB', 'MASTER_BLOOD_ORB', 'ARCHMAGE_BLOOD_ORB']
    stacks.custom_ingredients[f'{NS}:bloodorb'] = lambda st, ing: [
        st.plain(names[o]) for o in orbs[ing.get('orb_tier', 1) - 1:] if o in names and res.item_exists(names[o])]

    shutil.rmtree(ROOT / 'img' / NS, ignore_errors=True)
    book_dir = res.find('assets', f'{NS}:patchouli_books/{BOOK}/en_us', '')
    bj = res.json(res.find('data', f'{NS}:patchouli_books/{BOOK}/book', '.json'))
    builder = BookBuilder(res, stacks, NS, BOOK, book_dir, bj)
    builder.item = lambda const: stacks.plain(names[const])
    builder.rituals = read_rituals(java, config_defaults(java))
    builder.processors = PROCESSORS
    builder.multiblocks = {**ritual_multiblocks(builder.rituals), **altar_multiblocks(java)}
    # chests are drawn by a block entity renderer in game; a plank box stands in for the one ritual that uses it
    builder.blocks.add('minecraft:chest', {'textures': {'all': 'minecraft:block/oak_planks'},
                                           'elements': [{'from': [1, 0, 1], 'to': [15, 14, 15], 'faces': {
                                               d: {'texture': '#all'} for d in ('down', 'up', 'north', 'south', 'west', 'east')}}]})
    book = builder.build({'icon': stacks.plain('patchouli:guide_book')})
    write_pack(NS, [book], stacks, res, builder.blocks)


if __name__ == '__main__':
    main()
