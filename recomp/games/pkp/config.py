# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-game configuration of Pokémon Pokopia (Nintendo Switch 2, title 0400A3A01EE4C000).

Supported builds of main are listed in BUILDS (build id -> display version); the setup tool checks the build id. The
lifter itself is build-neutral: the hook sites of main are in main.sites.
Modules: main at 0x7100000000, sdk, subsdk0 (nn::movie) and rtld at the runtime's bases.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from nxrecomp.config import GameConfig, ModuleSpec          # noqa: E402

BUILDS = {
    '76A489F3BD3642FA5E18BD83D32C98BACC5F7D0E': '2.0.1',
}


class Config(GameConfig):
    name = 'pkp'
    title_id = '0400A3A01EE4C000'
    version = '2.0.1'
    build_id = '76A489F3BD3642FA5E18BD83D32C98BACC5F7D0E'       # of main (first 20 bytes; the rest of the 32-byte field is zero)
    # in NxModule.index order; translation units about 45k instructions each (main has 10.3 M instruction words)
    modules = [
        ModuleSpec('main', 0x7100000000, 'main', shards=230, sites='main.sites'),
        ModuleSpec('sdk', 0x7500000000, 'sdk', shards=45),
        ModuleSpec('subsdk0', 0x7600000000, 'subsdk0', shards=4),
        ModuleSpec('rtld', 0x7700000000, 'rtld', shards=1, entry=True),
    ]
    overrides = {}
    # registers and flags in C locals (nxrecomp/config.py). 'exact' stores the flags at every call, return and tail exit; 'lean' stores
    # them only where a summary of the whole module says they can be read, which costs a second pass over every function. Measured on
    # this game: 'exact' lifts in 24 s where 'lean' takes 65 s (main, 24 jobs), and it is faster in play too - 267.6 fps against 256.3
    # in the same in-world scene, 1 % low 192.8 against 152.5. The summary pass was costing both.
    reg_locals = 'exact'
    # rtld's process entry (0x0) has no FDE, symbol or direct branch, so discovery never roots it (runtime/docs/S6.md)
    extra_entries = {'rtld': [0x0]}
