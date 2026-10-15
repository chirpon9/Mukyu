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
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

from packlib import ROOT, BookBuilder, Resources, Stacks, call_args, split_top, warn, write_pack

NS = 'enigmaticlegacy'
BOOK = 'the_acknowledgment'


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


def ingredient_of(arg):
    m = re.match(r'Ingredient\.of\((.*)\)$', arg.strip(), re.S)
    if not m:
        raise ValueError('not an Ingredient.of: ' + arg)
    return [a.strip() for a in split_top(m.group(1))]


def read_java_recipes(java):
    potions = {}   # Java constant -> (name, effects)
    src = (java / 'registries' / 'EnigmaticPotions.java').read_text(encoding='utf-8')
    for m in re.finditer(r'(\w+)\s*=\s*new AdvancedPotion\("(\w+)"(.*?)\);', src):
        effects = [(e, int(a or 0)) for e, a in
                   re.findall(r'MobEffectInstance\((?:MobEffects|EnigmaticEffects)\.(\w+),\s*\d+(?:,\s*(\d+))?\)', m.group(3))]
        potions[m.group(1)] = (m.group(2), effects)

    def stack(expr):
        return java_item(re.sub(r'\s+', ' ', expr), potions)

    brewing = defaultdict(list)   # recipe id -> [{'input': [...], 'catalyst': [...], 'output': ...}] in registration order
    helper = (java / 'helpers' / 'PotionHelper.java').read_text(encoding='utf-8')
    main = (java / 'EnigmaticLegacy.java').read_text(encoding='utf-8')
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


# ------------------------------------------------------------------ page processors (ports of EL's Java ones)
class AdvancedBrewingRecipeProcessor:
    def __init__(self, builder, vars):
        self.s = builder.stacks
        r = builder.brewing.get(vars.raw('recipe'), [])
        i = int(vars.raw('index') or 0)
        self.recipe = r[i] if i < len(r) else None
        if self.recipe is None:
            warn(f'no brewing recipe {vars.raw("recipe")}[{i}]')

    def process(self, key):
        if self.recipe is None:
            return None
        for k in ('catalyst', 'input'):
            if key.startswith(k):
                return [self.s.stack(*x) for x in self.recipe[k]]
        if key.startswith('output'):
            return [self.s.stack(*self.recipe['output'])]
        return None


class HiddenRecipeProcessor:
    def __init__(self, builder, vars):
        self.s = builder.stacks
        self.recipe = builder.hidden.get(vars.raw('recipe'))
        if self.recipe is None:
            warn(f'no hidden recipe {vars.raw("recipe")}')

    def process(self, key):
        if self.recipe is None:
            return None
        m = re.match(r'input(\d)', key)
        if m:
            return [self.s.stack(*self.recipe['grid'][int(m.group(1)) - 1])]
        if key.startswith('output'):
            return [self.s.stack(*self.recipe['output'])]
        return None


def el_nbt(stacks, item, nbt):
    pm = re.search(r"EnigmaticPotion:\s*'(\w+)'", nbt)
    if pm:
        sid = stacks.potion_id(item, pm.group(1))
        return {'i': sid, 'g': 1} if stacks.has_glint(sid) else {'i': sid}
    return None


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    el, vanilla = Path(sys.argv[1]), Path(sys.argv[2])
    res = Resources([el / 'src' / 'main' / 'resources', vanilla])
    java = el / 'src' / 'main' / 'java' / 'com' / 'aizistral' / 'enigmaticlegacy'
    brewing, hidden, el_potions = read_java_recipes(java)

    stacks = Stacks(res)
    stacks.mod_potions = el_potions
    stacks.glint_items = {f'{NS}:ultimate_potion', f'{NS}:ultimate_potion_splash', f'{NS}:ultimate_potion_lingering'}
    # items whose model and name depend on NBT; the book only shows these in one state
    stacks.variants = {f'{NS}:enigmatic_eye': (f'{NS}:item/enigmatic_eye_active', f'item.{NS}.enigmatic_eye_active')}
    stacks.nbt_hook = el_nbt

    shutil.rmtree(ROOT / 'img' / NS, ignore_errors=True)
    book_dir = res.find('assets', f'{NS}:patchouli_books/{BOOK}/en_us', '')
    bj = res.json(res.find('data', f'{NS}:patchouli_books/{BOOK}/book', '.json'))
    builder = BookBuilder(res, stacks, NS, BOOK, book_dir, bj)
    builder.brewing, builder.hidden = brewing, hidden
    builder.processors = {'AdvancedBrewingRecipeProcessor': AdvancedBrewingRecipeProcessor, 'HiddenRecipeProcessor': HiddenRecipeProcessor}
    book = builder.build({
        # The book's "version" is a placeholder the mod fills in at runtime; name the mod instead of an edition.
        'subtitle': 'Enigmatic Legacy',
        'icon': stacks.plain(bj['custom_book_item']),
    })
    write_pack(NS, [book], stacks, res)


if __name__ == '__main__':
    main()
