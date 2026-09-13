#!/usr/bin/env python3
"""Generate the repetitive per-chip memory markup in v2/index.html.

The eight GDDR6 chips need three near-identical blocks in index.html: radial
gradient defs, board-diagram halo groups, and the readout cells. Rather than
hand-maintain eight copies of each (an eight-way edit for every tweak), this
script expands one template per block from the CHIPS table below and also
refreshes the ``?v=`` cache-busting hashes on the CSS/JS assets from their
current contents.

The markup stays static in index.html so the offline test
(``node tests/test_v2_memory.js``) keeps validating the real IDs; this script
is the single source of truth for those regions, not a runtime dependency.

    python3 web/v2/build_markup.py           # rewrite index.html in place
    python3 web/v2/build_markup.py --check    # fail if index.html is stale (CI)
"""
import argparse
import hashlib
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
INDEX = HERE / 'index.html'

# Physical layout from the board owner's top/X-ray view (see index.html and
# memory/README.md). Order is the upstream UMC/chip index 0..7.
CHIPS = [
    ('U27', 'translate(411.5,257.7) rotate(45)'),
    ('U29', 'translate(472.9,297.1) rotate(0)'),
    ('U31', 'translate(534.4,297.1) rotate(0)'),
    ('U33', 'translate(595.9,257.7) rotate(-45)'),
    ('U43', 'translate(595.9,102.7) rotate(45)'),
    ('U41', 'translate(534.4,63.3) rotate(0)'),
    ('U39', 'translate(472.9,63.3) rotate(0)'),
    ('U37', 'translate(411.5,102.7) rotate(-45)'),
]

# Assets whose ?v= query string is refreshed from a hash of their contents.
CACHE_BUST_ASSETS = ('style.css', 'bc250-board.css', 'memory.js', 'app.js')


def _join(blocks, indent):
    """Join per-chip blocks, re-indenting every line after the first block.

    The region regex matches from the first element (its leading indent is
    outside the match), so the first block starts flush and each later block
    is prefixed with the captured indent.
    """
    return ('\n' + indent).join(blocks)


def gradients(indent):
    block = (
        '<radialGradient id="bc-memory-gradient-{n}" color="#38bdf8">\n'
        '{i}    <stop offset="0%" stop-color="currentColor" stop-opacity=".85"></stop>\n'
        '{i}    <stop offset="45%" stop-color="currentColor" stop-opacity=".5"></stop>\n'
        '{i}    <stop offset="100%" stop-color="currentColor" stop-opacity="0"></stop>\n'
        '{i}</radialGradient>'
    )
    return _join([block.format(n=n, i=indent) for n in range(8)], indent)


def halos(indent):
    block = (
        '<g id="bc-memory-{n}" class="bc-memory-chip" data-designator="{d}" transform="{t}" opacity="0">\n'
        '{i}    <title id="bc-memory-title-{n}">Chip {n} · {d}: unavailable</title>\n'
        '{i}    <ellipse id="bc-memory-glow-{n}" rx="30" ry="35" fill="url(#bc-memory-gradient-{n})" opacity="0"></ellipse>\n'
        '{i}    <rect class="bc-memory-hotspot" x="-14.15" y="-18.2" width="28.3" height="36.4" rx="3" fill="none" stroke="#fb923c" stroke-width="2.2" opacity="0"></rect>\n'
        '{i}</g>'
    )
    return _join([block.format(n=n, d=CHIPS[n][0], t=CHIPS[n][1], i=indent) for n in range(8)], indent)


def cells(indent):
    block = (
        '<div class="memory-chip" id="v2-memory-cell-{n}"><div class="memory-chip-reading">'
        '<span class="memory-label" title="Chip {n}" aria-label="Chip {n}">'
        '<svg class="memory-chip-icon" id="v2-memory-icon-{n}" viewBox="0 0 16 16" fill="none" '
        'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" '
        'aria-hidden="true" focusable="false">'
        '<rect x="4" y="4" width="8" height="8" rx="1.2" fill="currentColor" fill-opacity=".24"></rect>'
        '<path d="M6 1.5V4m4-2.5V4M6 12v2.5m4-2.5v2.5M1.5 6H4m-2.5 4H4m8-4h2.5M12 10h2.5"></path>'
        '</svg><span>{n}</span></span>'
        '<div><span id="v2-memory-chip-{n}">—</span><span class="memory-unit">°C</span></div></div>'
        '<div class="mini-bar" aria-hidden="true"><div class="mini-bar-fill" id="v2-memory-bar-{n}"></div></div></div>'
    )
    return _join([block.format(n=n) for n in range(8)], indent)


# (regex, generator) pairs. Each regex captures the leading indent of the first
# element in the region; the rest of the region is replaced with fresh markup.
REGIONS = [
    (re.compile(r'(?s)(?P<ind>[ \t]*)<radialGradient id="bc-memory-gradient-0".*?</radialGradient>(?=\s*</defs>)'),
     gradients),
    (re.compile(r'(?s)(?P<ind>[ \t]*)<g id="bc-memory-0" class="bc-memory-chip".*?</g>(?=\s*</g>)'),
     halos),
    (re.compile(r'(?s)(?P<ind>[ \t]*)<div class="memory-chip" id="v2-memory-cell-0">.*?id="v2-memory-bar-7"></div></div></div>'),
     cells),
]


def render(html):
    for pattern, build in REGIONS:
        match = pattern.search(html)
        if not match:
            raise SystemExit(f'anchor not found for {build.__name__}; index.html structure changed')
        indent = match.group('ind')
        html = html[:match.start()] + indent + build(indent) + html[match.end():]
    for asset in CACHE_BUST_ASSETS:
        digest = hashlib.sha256((HERE / asset).read_bytes()).hexdigest()[:12]
        html = re.sub(re.escape(asset) + r'\?v=[0-9a-f]+', f'{asset}?v={digest}', html)
    return html


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='exit non-zero if index.html is out of date')
    args = parser.parse_args()
    current = INDEX.read_text()
    updated = render(current)
    if args.check:
        if current != updated:
            print('index.html is stale; run: python3 web/v2/build_markup.py', file=sys.stderr)
            return 1
        print('index.html memory markup and cache-bust hashes are up to date.')
        return 0
    if current != updated:
        INDEX.write_text(updated)
        print('index.html updated.')
    else:
        print('index.html already up to date.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
