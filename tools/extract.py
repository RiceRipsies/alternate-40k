#!/usr/bin/env python3
"""
tools/extract.py
Extracts unit data from Alternate 40k codex PDFs → writes JSON to data/

Usage (run from the project root):
    python3 tools/extract.py                  # process all PDFs
    python3 tools/extract.py ork-codex.pdf    # process one PDF

PDFs must be in the project root alongside this tools/ folder.
Requires: pdftotext (brew install poppler)
"""

import re
import json
import subprocess
import sys
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────
ROOT     = Path(__file__).parent.parent
SOURCE_DIR = ROOT / 'source'
DATA_DIR = ROOT / 'data'
DATA_DIR.mkdir(exist_ok=True)

# ── Slot header recognition ───────────────────────────────────────────────────
# Maps regex → slot key.  Ordered so the most-specific patterns come first.
SLOT_PATTERNS = [
    (re.compile(r'^HQ\b',                   re.I), 'hq'),
    (re.compile(r'^Advisors?\b',            re.I), 'advisors'),
    (re.compile(r'^Troops\b',               re.I), 'troops'),
    (re.compile(r'^Elites?\b',              re.I), 'elites'),
    (re.compile(r'^Fast Attacks?\b',        re.I), 'fast_attack'),
    (re.compile(r'^Heavy Support\b',        re.I), 'heavy_support'),
    (re.compile(r'^Flyers?\b',              re.I), 'flyers'),
    (re.compile(r'^Dedicated Transports?\b',re.I), 'dedicated_transport'),
    (re.compile(r'^Lords? of War\b',        re.I), 'lord_of_war'),
    (re.compile(r'^Fortifications?\b',      re.I), 'fortifications'),
]

# Lines that look like slot headers but are actually table-of-contents entries
# (they contain lots of dots used for page number alignment)
TOC_LINE_RE = re.compile(r'\.{4,}')

# ── Line-level regexes ────────────────────────────────────────────────────────
STAT_HEADER_RE   = re.compile(r'^M\s+WS\s+BS\s+S\s+(?:FA\s+SA\s+RA\s+)?T?\s*W\s+I\s+A\s+Ld\s+Sv\s*$', re.I)
STAT_VALUES_RE   = re.compile(r'^-?\d+\s+[\d\+\-]+\s+[\d\+\-]+')   # starts with numeric stats
POINTS_RE        = re.compile(r'^Points:\s*(\d+)')
COMPOSITION_RE   = re.compile(r'^Composition:\s*$')
RULES_HDR_RE     = re.compile(r'^Rules\s*$')
WARGEAR_HDR_RE   = re.compile(r'^Wargear\s*$', re.I)
OPTIONS_HDR_RE   = re.compile(r'^Options\s*$', re.I)
SP_WG_HDR_RE     = re.compile(r'^Special Wargear:\s*$', re.I)
SP_WG_UPG_RE     = re.compile(r'^Special Wargear Upgrades:\s*$', re.I)
UPGRADE_LINE_RE  = re.compile(r'^([A-Z]+(?:\s*\+\d+\s*points?)?)\s+(.+?)\s+\+(\d+)\s+points?', re.I)
# Capitalised May / Any starts a new option (case-sensitive — avoids matching lowercase
# continuation lines like "may swap Boltgun" that are word-wrapped chapter-variant text).
OPTION_LINE_RE   = re.compile(r'^(?:May|Any)\b')
# Lines like "Sergeant(s) may swap…", "One Marine may swap…" — subject BEFORE the word "may"
OPTION_SUBJ_RE   = re.compile(r'^\w[\w\s()]+\s(?:may|can)\b', re.I)
# Chapter-specific option lines (e.g. "Any Space Wolf Dreadnought may swap...") — drop from generic codex
# Matches any option line that mentions a specific chapter name — used to drop
# cross-chapter variant options from parent codexes like space-marines.json.
CHAPTER_VARIANT_RE = re.compile(
    r'\b(?:Space\s+Wolf(?:ves)?|Blood\s+Angels?|Iron\s+Hands?|Ultramarines?|'
    r'Dark\s+Angels?|Imperial\s+Fists?|Salamanders?|Raven\s+Guard|White\s+Scars?|'
    r'Black\s+Templars?|Crimson\s+Fists?|Grey\s+Knights?|Deathwatch|'
    r'Death\s+Guard|Thousand\s+Sons?)\b',
    re.I
)
# Lines that are clearly weapon-table column headers or noise
WEAPON_HDR_RE    = re.compile(r'^Selection\s*$|^Name\s*$|^Range\s*$|^AP\s*$|^Rules\s*$')
# Lone single-letter / selection-code lines that are weapon-table rows
WEAPON_ROW_RE    = re.compile(r'^[A-Z]{1,3}(?:\s+\+\d+\s+points?)?\s*$')
# Vehicle stat header has FA SA RA columns
VEHICLE_RE       = re.compile(r'FA\s+SA\s+RA', re.I)

# Sub-section labels that appear between slot headers and unit names — not unit names
SUBSECTION_LABELS = {
    'generic', 'unique', 'infantry', 'monstrous infantry', 'vehicles', 'vehicle',
    'tanks', 'tank', 'monsters', 'monster', 'artillery', 'steeds', 'steed',
    'dragstaz', 'swarms', 'swarm', 'combat walkers', 'combat walker',
    'weapon platforms', 'armoured cars', 'sentinels', 'aircraft',
    'fellblade chassis', 'spartan chassis', 'baneblade chassis', 'macharius chassis',
    'marauder chassis', 'support', 'c\'tan shards', 'other', 'monoliths',
    'fast attacks', 'heavy support', 'support tanks',
    # Group-header labels that appear between a slot header and the actual unit name
    'battle tanks', 'self-propelled guns', 'support vehicles',
}


# ── Helper ────────────────────────────────────────────────────────────────────
def _normalise_text(text: str) -> str:
    """Normalise Unicode punctuation to ASCII equivalents for consistent parsing."""
    return (text
        .replace('‘', "'").replace('’', "'")   # curly single quotes → '
        .replace('“', '"').replace('”', '"')   # curly double quotes → "
        .replace('–', '-').replace('—', '-')   # en/em dash → -
    )


def pdf_to_text(pdf_path: Path) -> str:
    result = subprocess.run(
        ['pdftotext', str(pdf_path), '-'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"pdftotext failed for {pdf_path}: {result.stderr}")
    return _normalise_text(result.stdout)


def pdf_to_text_layout(pdf_path: Path) -> str:
    result = subprocess.run(
        ['pdftotext', '-layout', str(pdf_path), '-'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"pdftotext -layout failed for {pdf_path}: {result.stderr}")
    return _normalise_text(result.stdout)


def parse_weapon_tables_from_layout(layout_text: str) -> dict:
    """
    Parse weapon selection tables from pdftotext -layout output.
    Returns: {unit_name: {code: [{name, pts_delta, range, S, AP, type}]}}
    """
    WEAPON_TABLE_HDR = re.compile(r'Selection\s+Name\s+Range', re.I)
    UNIT_STAT_HDR    = re.compile(r'^(.+?)\s{2,}M\s+WS\s+BS\s+S\b', re.I)
    ENTRY_RE         = re.compile(r'^([A-Z][A-Z0-9]*(?:\s+\+\d+\s+points?)?)\s+(.+)')
    PTS_RE           = re.compile(r'\+(\d+)\s+points?', re.I)
    RANGE_STOP       = re.compile(r'^\d|^Melee$|^Flame$|^\*$', re.I)
    AP_RE            = re.compile(r'^(\d+\+|\*|-)\s*(.*)')

    lines = layout_text.split('\n')

    unit_headers = []
    for i, raw in enumerate(lines):
        clean = raw.lstrip('\x0c').strip()
        m = UNIT_STAT_HDR.match(clean)
        if m:
            name = m.group(1).strip()
            if name and 'selection' not in name.lower():
                unit_headers.append((i, name))

    result = {}

    for i, raw in enumerate(lines):
        clean = raw.lstrip('\x0c')
        if not WEAPON_TABLE_HDR.search(clean):
            continue

        unit_name = None
        for uh_idx, uh_name in reversed(unit_headers):
            if uh_idx < i:
                unit_name = uh_name
                break
        if not unit_name:
            continue

        table: dict = {}
        current_entry = None
        j = i + 1
        while j < len(lines):
            raw_line = lines[j]
            clean_line = raw_line.lstrip('\x0c')
            stripped = clean_line.strip()

            if UNIT_STAT_HDR.match(stripped) or WEAPON_TABLE_HDR.search(clean_line):
                break

            m = ENTRY_RE.match(stripped)
            if m:
                code_raw   = m.group(1).strip()
                rest       = m.group(2)
                rest_parts = re.split(r'\s{2,}', rest)

                # Extract name — stop at range-like tokens
                raw_name_chunk = rest_parts[0] if rest_parts else ''
                name_words: list = []
                range_from_name = ''
                for word in raw_name_chunk.split():
                    if name_words and RANGE_STOP.match(word):
                        # Only treat a number/Melee/Flame token as range if
                        # at least one name word has been collected first.
                        # Leading numbers (e.g. "2 Linked Heavy Bolters") are
                        # part of the weapon name, not the range column.
                        range_from_name = word
                        break
                    name_words.append(word)
                name = ' '.join(name_words)

                # Build ordered stat columns
                stat_parts = ([range_from_name] if range_from_name else []) + rest_parts[1:]
                range_val  = stat_parts[0].strip() if len(stat_parts) > 0 else ''
                s_val      = stat_parts[1].strip() if len(stat_parts) > 1 else ''
                ap_rest    = ' '.join(p.strip() for p in stat_parts[2:]) if len(stat_parts) > 2 else ''
                ap_m       = AP_RE.match(ap_rest)
                ap_val     = ap_m.group(1) if ap_m else ap_rest
                type_text  = ap_m.group(2).strip() if ap_m else ''

                pts_m     = PTS_RE.search(code_raw)
                pts_delta = int(pts_m.group(1)) if pts_m else 0
                code      = PTS_RE.sub('', code_raw).strip()

                if name:
                    current_entry = {
                        'name': name, 'pts_delta': pts_delta,
                        'range': range_val, 'S': s_val, 'AP': ap_val, 'type': type_text,
                    }
                    table.setdefault(code, []).append(current_entry)
                j += 1
                continue

            leading = len(clean_line) - len(clean_line.lstrip())
            if stripped and current_entry:
                if stripped.lower().startswith('or'):
                    pass  # skip combo-weapon alternate profile lines
                elif 1 <= leading <= 45:
                    # Name continuation (weapon names can start anywhere from col 1-30,
                    # so their wrapped second lines land in the same range)
                    first_word = stripped.split()[0]
                    if not first_word[0].isdigit():
                        cont_parts = re.split(r'\s{2,}', stripped)
                        current_entry['name'] += ' ' + cont_parts[0].strip()
                        if len(cont_parts) > 1:
                            current_entry['type'] = (current_entry['type'] + ' ' + ' '.join(cont_parts[1:])).strip()
                elif leading > 45:
                    # Rules-text continuation (far-right column, typically col 50+)
                    current_entry['type'] = (current_entry['type'] + ' ' + stripped).strip()

            j += 1

        if table:
            # Deduplicate: same (name, pts_delta) within a code
            for code, entries in table.items():
                seen = set()
                deduped = []
                for e in entries:
                    key = (e['name'].lower(), e['pts_delta'])
                    if key not in seen:
                        seen.add(key)
                        deduped.append(e)
                table[code] = deduped
            result[unit_name] = table

    return result


def identify_slot(line: str) -> str | None:
    """Return slot key if line is a slot-section header, else None."""
    s = line.strip()
    if TOC_LINE_RE.search(s):
        return None
    for pattern, key in SLOT_PATTERNS:
        if pattern.match(s):
            return key
    return None


def parse_stat_values(line: str, is_vehicle: bool) -> dict:
    """Parse a stat-value line into a dict.  Returns {} on failure."""
    parts = line.split()
    keys_inf = ['M','WS','BS','S','T','W','I','A','Ld','Sv']
    keys_veh = ['M','WS','BS','S','FA','SA','RA','W','I','A','Ld','Sv']
    keys = keys_veh if is_vehicle else keys_inf
    if len(parts) < len(keys) - 1:
        return {}
    return {k: parts[i] for i, k in enumerate(keys) if i < len(parts)}


# ── Core parser ───────────────────────────────────────────────────────────────
def parse_codex(text: str, source_name: str, weapon_tables: dict | None = None) -> dict:
    """
    Parse raw pdftotext output for one codex.
    Returns a dict with faction metadata and slot→units mapping.
    """
    lines = [l.rstrip() for l in text.split('\n')]
    n = len(lines)

    # ── 1. Assign each line to a slot key (forward scan) ────────────────────
    line_slot = [None] * n          # slot key for each line
    current_slot = None
    for i, raw in enumerate(lines):
        s = identify_slot(raw)
        if s:
            current_slot = s
        line_slot[i] = current_slot

    # ── 2. Find all stat-header positions ────────────────────────────────────
    stat_header_idx = [
        i for i, l in enumerate(lines)
        if STAT_HEADER_RE.match(l.strip())
    ]

    # ── 3. Find all Points: positions ────────────────────────────────────────
    points_idx = [
        i for i, l in enumerate(lines)
        if POINTS_RE.match(l.strip())
    ]

    if not points_idx:
        print(f"  [warn] No 'Points:' lines found in {source_name}")
        return {'name': source_name, 'slots': {}}

    # ── 4. Build unit blocks ──────────────────────────────────────────────────
    # Each unit block is anchored at its Points: line.
    # We scan backwards to find the stat header, then parse everything.
    units_by_slot: dict[str, list] = {}

    for pi in points_idx:
        pts_match = POINTS_RE.match(lines[pi].strip())
        if not pts_match:
            continue
        points_base = int(pts_match.group(1))

        # ── Find the stat header above this Points: line ──────────────────
        stat_hi = None
        for sh in reversed(stat_header_idx):
            if sh < pi:
                stat_hi = sh
                break
        if stat_hi is None:
            continue

        is_vehicle = bool(VEHICLE_RE.search(lines[stat_hi]))

        # ── Stat value lines (one per model, immediately follow header) ───
        stat_val_lines = []
        j = stat_hi + 1
        _sv_cont_re = re.compile(r'^\d\+$')   # bare save continuation, e.g. "3+"
        while j < pi and j < stat_hi + 10:
            stripped = lines[j].strip()
            if not stripped:
                j += 1
                continue
            if STAT_VALUES_RE.match(stripped):
                stat_val_lines.append(stripped)
                j += 1
            elif stat_val_lines and _sv_cont_re.match(stripped):
                # Sv wrapped to next line — merge onto preceding stat row
                stat_val_lines[-1] += ' ' + stripped
                j += 1
            else:
                break

        # Fallback: in 2-column PDFs stat values may appear anywhere in the window
        # stat_hi..next_stat_hi. Scan that full range for lines consisting entirely
        # of stat-like tokens with enough values to form a valid row.
        if not stat_val_lines:
            next_stat_hi = next((sh for sh in stat_header_idx if sh > pi), n)
            scan_end = min(next_stat_hi, pi + 60, n)
            _stat_only_re = re.compile(r'^(?:\d+[\+\-]?|-)(?:\s+(?:\d+[\+\-]?|-))+$')
            for k in range(stat_hi + 1, scan_end):
                s2 = lines[k].strip()
                if s2 and _stat_only_re.match(s2) and parse_stat_values(s2, is_vehicle):
                    stat_val_lines.append(s2)

        # ── Model names (lines between previous blank/header and stat_hi) ─
        # Collect non-empty lines going backwards; skip sub-section labels.
        # Stop at a blank line once we have at least one name — the blank line
        # separates the unit name block from the weapon-table noise above it.
        raw_names = []
        k = stat_hi - 1
        while k >= max(0, stat_hi - 25):
            stripped = lines[k].strip()
            if not stripped:
                if raw_names:
                    break   # blank line after names = end of name block
                k -= 1
                continue
            # Hard stops: slot headers, points, composition = truly outside this unit
            if (identify_slot(lines[k]) or
                    POINTS_RE.match(stripped) or
                    COMPOSITION_RE.match(stripped)):
                break
            # Section content headers mean everything collected so far belonged to
            # that section (wargear items, not the unit name) — clear and keep going
            if (WARGEAR_HDR_RE.match(stripped) or
                    SP_WG_HDR_RE.match(stripped) or
                    SP_WG_UPG_RE.match(stripped) or
                    OPTIONS_HDR_RE.match(stripped) or
                    RULES_HDR_RE.match(stripped)):
                raw_names.clear()
                k -= 1
                continue
            # Skip known sub-section label lines
            if stripped.lower() not in SUBSECTION_LABELS:
                raw_names.insert(0, stripped)
            k -= 1

        # raw_names is [unit_name, model1, model2, ...]
        # If unit_name == model1 (common for single-model units), deduplicate.
        if not raw_names:
            unit_name = source_name
            model_name_list = [source_name]
        elif len(raw_names) == 1:
            unit_name = raw_names[0]
            model_name_list = [raw_names[0]]
        else:
            unit_name = raw_names[0]
            model_name_list = raw_names[1:]
            # Deduplicate: if first model name repeats the unit name, keep just one
            if model_name_list and model_name_list[0] == unit_name:
                model_name_list = model_name_list  # keep — it IS the model name

        # Drop phantom chassis-label models: in some PDFs a vehicle chassis name
        # (e.g. "Dreadnought") appears on the stat-header row in the left column,
        # ending up as the first model name but having no corresponding stat line.
        _CHASSIS_LABELS = {'dreadnought'}
        if (stat_val_lines and
                len(model_name_list) > len(stat_val_lines) and
                model_name_list[0].lower() in _CHASSIS_LABELS):
            model_name_list = model_name_list[1:]

        # Build model entries
        models = []
        for mi, mn in enumerate(model_name_list):
            stats = {}
            if mi < len(stat_val_lines):
                stats = parse_stat_values(stat_val_lines[mi], is_vehicle)
            models.append({'name': mn, 'stats': stats, 'rules': []})
        # If more stat lines than named models, keep extra stats on last model
        if len(stat_val_lines) > len(models) and models:
            models[-1]['stats'] = parse_stat_values(stat_val_lines[-1], is_vehicle)

        # ── Section between stat values and Points: ───────────────────────
        section_start = stat_hi + 1 + len(stat_val_lines)
        section_lines = [l.strip() for l in lines[section_start:pi] if l.strip()]

        fixed_wargear = []
        options_text  = []
        upgrades      = []   # list of {code, name, pts_delta}

        state = 'NONE'
        current_model_wg = None
        in_chapter_variant = False  # True while inside a chapter-specific option block

        for sl in section_lines:
            # Section headers
            if WARGEAR_HDR_RE.match(sl):
                state = 'WARGEAR'; continue
            if OPTIONS_HDR_RE.match(sl):
                state = 'OPTIONS'; continue
            if SP_WG_UPG_RE.match(sl):
                state = 'UPGRADES'; continue
            if SP_WG_HDR_RE.match(sl):
                state = 'SP_WG'; continue
            # Weapon table noise – skip
            if WEAPON_HDR_RE.match(sl):
                state = 'WEAPON_TABLE'; continue
            if state == 'WEAPON_TABLE':
                continue

            if state == 'WARGEAR':
                # "ModelName:" lines introduce per-model wargear sections
                if sl.endswith(':') and not sl.startswith('Special'):
                    current_model_wg = sl[:-1]
                elif fixed_wargear and sl and (
                    sl[0].islower() or
                    # Single-word uppercase continuation of a numbered item:
                    # "2 Dreadnought Missile" + "Launchers", "2 Linked Acheron Flame" + "Cannons".
                    # Exclude single-letter mount codes (H/S/T) and "None".
                    (' ' not in sl and not sl[0].isdigit() and
                     len(sl) > 1 and sl.lower() != 'none' and
                     fixed_wargear[-1] and fixed_wargear[-1][0].isdigit())
                ):
                    fixed_wargear[-1] += ' ' + sl
                else:
                    fixed_wargear.append(sl)

            elif state == 'OPTIONS':
                is_new_opt = OPTION_LINE_RE.match(sl) or OPTION_SUBJ_RE.match(sl)
                is_chapter = CHAPTER_VARIANT_RE.search(sl)
                if is_new_opt:
                    if is_chapter:
                        in_chapter_variant = True
                    else:
                        in_chapter_variant = False
                        options_text.append(sl)
                elif not in_chapter_variant and options_text and not sl.endswith(':'):
                    if is_chapter:
                        in_chapter_variant = True
                    else:
                        options_text[-1] += ' ' + sl

            elif state == 'UPGRADES':
                m = UPGRADE_LINE_RE.match(sl)
                if m:
                    upgrades.append({
                        'code': m.group(1).strip(),
                        'name': m.group(2).strip(),
                        'pts_delta': int(m.group(3)),
                    })
                elif upgrades:
                    # Effect text on the next line (stat modifier descriptions)
                    upgrades[-1].setdefault('effects', []).append(sl)

        # ── Composition ───────────────────────────────────────────────────
        composition = ''
        if pi + 1 < n and COMPOSITION_RE.match(lines[pi + 1].strip()):
            comp_parts = []
            cj = pi + 2
            while cj < n and cj < pi + 6:
                stripped = lines[cj].strip()
                if not stripped or RULES_HDR_RE.match(stripped):
                    break
                comp_parts.append(stripped)
                cj += 1
            composition = ' '.join(comp_parts)

        # ── Rules ─────────────────────────────────────────────────────────
        rules_start = None
        for ri in range(pi, min(pi + 8, n)):
            if RULES_HDR_RE.match(lines[ri].strip()):
                rules_start = ri + 1
                break

        if rules_start is not None:
            current_model_idx = 0
            # Weapon type line, e.g. "Pistol 1, Dakka" — weapon stat, not a rule
            WEAPON_TYPE_RE = re.compile(
                r'^(?:Pistol|Assault|Heavy|Rapid Fire|Grenade|Melee)\s+[\d1]', re.I)
            # Stat modifier line: "M-2, W+1..." or "FA +1, SA..." or "Sv-1..."
            STAT_MOD_RE = re.compile(
                r'^(?:[MWTSAI]|FA|SA|RA|BS|WS)\s*[+\-]\d|^Sv\s*[+\-]|^Ld\s*[+\-]', re.I)
            # Upgrade cost in rules section, e.g. "A Mega Armour +18 points"
            UPGRADE_COST_RE = re.compile(r'\+\d+\s+points?\s*$', re.I)
            # Em-dash or spaced hyphen separating rule name from description
            RULE_DASH_RE = re.compile(r'\s[–\-]\s')

            skip_continuation = False   # True after stat-modifier lines
            in_named_rule = False       # True after a rule-with-dash line

            for ri in range(rules_start, min(rules_start + 80, n)):
                raw_line = lines[ri]
                stripped = raw_line.strip()

                # Hard stops
                if STAT_HEADER_RE.match(stripped) or identify_slot(raw_line):
                    break
                if WEAPON_HDR_RE.match(stripped) or WEAPON_ROW_RE.match(stripped):
                    break
                # Form-feed = PDF page break; column interleaving starts here
                if '\x0c' in raw_line:
                    break

                if not stripped:
                    skip_continuation = False
                    continue

                # "ModelName:" line — switches which model receives rules
                if (stripped.endswith(':') and
                        not stripped.startswith(('–', '-')) and
                        len(stripped) < 40):
                    skip_continuation = False
                    in_named_rule = False
                    mn_candidate = stripped[:-1]
                    found = next(
                        (idx for idx, m in enumerate(models)
                         if m['name'] == mn_candidate),
                        None
                    )
                    if found is not None:
                        current_model_idx = found
                    elif current_model_idx + 1 < len(models):
                        current_model_idx += 1   # label doesn't match; advance
                    continue

                # Skip lines starting with lowercase or digit (description continuations)
                if stripped[0].islower() or stripped[0].isdigit():
                    continue

                # Skip stat modifier lines and mark continuation skip
                if STAT_MOD_RE.match(stripped) or UPGRADE_COST_RE.search(stripped):
                    skip_continuation = True
                    in_named_rule = False
                    continue

                # Skip continuation lines after stat modifiers
                if skip_continuation:
                    # A line with a dash signals a new named rule; allow it through
                    if not RULE_DASH_RE.search(stripped):
                        continue

                # Skip weapon type lines
                if WEAPON_TYPE_RE.match(stripped):
                    continue

                # Extract rule name: text before the first dash separator
                rule_name = RULE_DASH_RE.split(stripped)[0].strip()
                has_dash = bool(RULE_DASH_RE.search(stripped))

                if has_dash:
                    skip_continuation = False
                    in_named_rule = True
                else:
                    # Bare keyword line
                    words = rule_name.split()
                    # Reject if any word starts with lowercase
                    if any(w[0].islower() for w in words
                           if w and not w[0].isdigit() and w[0] not in '+-()'):
                        continue
                    # Reject list fragments (first word ends with comma,
                    # or entire line ends with comma)
                    if words and words[0].endswith(','):
                        continue
                    if rule_name.endswith(','):
                        continue
                    # After a named-rule description, reject long lines (≥4 words)
                    # — they're likely description continuations, not rule names.
                    if in_named_rule and len(words) >= 4:
                        continue
                    in_named_rule = False

                # Final noise filters
                if (not rule_name or
                        WEAPON_HDR_RE.match(rule_name) or
                        WEAPON_ROW_RE.match(rule_name) or
                        STAT_MOD_RE.match(rule_name) or
                        re.match(r'^[\d\+\-]', rule_name) or
                        len(rule_name) > 60):
                    continue

                if models:
                    models[current_model_idx]['rules'].append(rule_name)

        # ── Post-rules scan: Special Wargear Upgrades after Points: ──────
        # In 2-column PDFs the upgrades section may appear after "Points:" in
        # the text stream even though it logically belongs to this unit.
        if rules_start is not None:
            post_state = 'NONE'
            for ri in range(rules_start, min(rules_start + 120, n)):
                s2 = lines[ri].strip()
                if SP_WG_UPG_RE.match(s2):
                    post_state = 'UPGRADES'; continue
                if post_state == 'UPGRADES':
                    # Stop at the next unit's stat header or slot header
                    if STAT_HEADER_RE.match(s2) or identify_slot(lines[ri]):
                        break
                    m2 = UPGRADE_LINE_RE.match(s2)
                    if m2:
                        upgrades.append({
                            'code': m2.group(1).strip(),
                            'name': m2.group(2).strip(),
                            'pts_delta': int(m2.group(3)),
                        })

        # ── Determine slot ────────────────────────────────────────────────
        slot = line_slot[pi] or 'unknown'

        # ── Build options list for builder ────────────────────────────────
        # Keep original text for code matching; strip trailing codes for display
        # e.g. "swap Boltgun for OR or A" → "swap Boltgun"
        _CODE_TAIL_RE = re.compile(
            r'(?:'
            r'\s+(?:for(?:\s+the\s+same)?|or|of\s+each|one)\s+[A-Z]{1,4}'   # "for H", "for the same R"
            r'|\s+up\s+to\s+(?:\d+|two|three|four|five|six|seven|eight|nine|ten)\s+[A-Z]{1,4}'  # "up to 2 M"
            r'|\s+[A-Z]{1,3}'                                                  # bare trailing code " H"
            r')+\s*$'
        )
        options = []
        for opt_line in options_text:
            options.append({'_raw': opt_line, 'description': opt_line, 'choices': []})

        # Weapon table for this unit (from layout pass)
        unit_wt = (weapon_tables or {}).get(unit_name, {})

        def _opt_matches_code(opt_desc: str, code: str) -> bool:
            return (f'for {code}' in opt_desc or
                    f'one {code}' in opt_desc or
                    f'up to one {code}' in opt_desc or
                    f'or {code}' in opt_desc or
                    opt_desc.endswith(f' {code}') or
                    bool(re.search(rf'\b{re.escape(code)}\b', opt_desc)))

        # Attach weapon table choices (all R/P/M/G/etc. entries) to matching options
        for code, entries in unit_wt.items():
            for opt in options:
                if _opt_matches_code(opt['description'], code):
                    for entry in entries:
                        opt['choices'].append({
                            'name': entry['name'],
                            'pts_delta': entry['pts_delta'],
                        })
                    break  # each code matches at most one option

        # Attach upgrade costs (Special Wargear Upgrades: A/B/C/etc.) to matching options
        for upg in upgrades:
            code = upg['code'].rstrip('0123456789 ').strip()
            matched = False
            for opt in options:
                if _opt_matches_code(opt['description'], code):
                    opt['choices'].append({
                        'name': upg['name'],
                        'pts_delta': upg['pts_delta'],
                    })
                    matched = True
                    break
            if not matched:
                # Only create freestanding options for generic single-letter codes (A, B, E…)
                # Multi-letter codes (SWE, BAE, SWW…) are chapter-specific and irrelevant here
                if len(code) <= 2:
                    options.append({
                        'description': f'May take {upg["name"]}',
                        'choices': [{'name': upg['name'], 'pts_delta': upg['pts_delta']}],
                    })

        # Clean option descriptions: strip trailing codes and remove _raw field
        for opt in options:
            opt.pop('_raw', None)
            opt['description'] = _CODE_TAIL_RE.sub('', opt['description']).strip()

        # Drop orphaned option fragments (e.g. "May take" with no item name and no choices)
        _FRAG_RE = re.compile(r'^May (?:take|swap|replace|use|upgrade)\s*$', re.I)
        options = [o for o in options if not (_FRAG_RE.match(o['description']) and not o['choices'])]

        weapons = {}
        for code, entries in unit_wt.items():
            for entry in entries:
                name = entry['name']
                if name and name not in weapons:
                    weapons[name] = {
                        'range': entry.get('range', ''),
                        'S':     entry.get('S', ''),
                        'AP':    entry.get('AP', ''),
                        'type':  entry.get('type', ''),
                    }

        # Reject entries whose name looks like a section header or stray value
        if (unit_name.endswith(':') or
                len(unit_name) <= 2 or
                unit_name[0].isdigit() or                      # "0-1 Lord of War Slots" etc.
                re.match(r'^[A-Z0-9\+\-]+$', unit_name) or   # bare stat/code like "AP", "3+"
                re.match(r'^[A-Z]{1,4}\s*\+\d+\s+points?', unit_name) or  # "P +5 points", "T +33 points"
                STAT_HEADER_RE.match(unit_name) or             # stat header captured as name
                re.match(r'^(?:Heavy|Rapid Fire|Assault|Pistol|Ordnance|Grenade|Melee)\s+\d', unit_name) or
                SP_WG_HDR_RE.match(unit_name) or
                SP_WG_UPG_RE.match(unit_name) or
                WEAPON_HDR_RE.match(unit_name)):                           # "Name", "Range", "AP" etc.
            continue

        # ── Clean up fixed_wargear ────────────────────────────────────
        _NONE_SUFFIX_RE = re.compile(r'\s+[Nn]one$')
        _STAT_KEYS = {'M','WS','BS','S','FA','SA','RA','T','W','I','A','Ld','Sv'}
        cleaned_wg = []
        for wg in fixed_wargear:
            wg = _NONE_SUFFIX_RE.sub('', wg).strip()
            if not wg or wg.lower() == 'none':
                continue
            if re.match(r'^[Mm]ay\s+(?:take|replace|swap)', wg):
                continue
            # Skip stat header rows (full or partial) and stat value rows
            if STAT_HEADER_RE.match(wg):
                continue
            if STAT_VALUES_RE.match(wg):
                continue
            if all(t in _STAT_KEYS for t in wg.split()):
                continue
            cleaned_wg.append(wg)
        fixed_wargear = cleaned_wg

        unit = {
            'name':         unit_name,
            'points_base':  points_base,
            'composition':  composition,
            'models':       models,
            'fixed_wargear':fixed_wargear,
            'options':      options,
            'weapons':      weapons,
        }

        units_by_slot.setdefault(slot, []).append(unit)

    return units_by_slot


# ── Faction name extraction ────────────────────────────────────────────────────
def extract_faction_name(text: str) -> str:
    """First non-empty line of the PDF is usually the faction name."""
    for line in text.split('\n'):
        s = line.strip()
        if s and not s.startswith('"'):
            return s
    return 'Unknown'


def extract_army_rules(text: str) -> dict:
    """
    Extract named rules with descriptions from the army abilities section.
    Stops at 'Common Wargear' or the first slot header.
    Returns dict: {rule_name: description_text}
    """
    RULE_DEF_RE  = re.compile(r'^([A-Z][A-Za-z\'\s\d!]{1,60}?)\s+[–\-]\s+(.+)')
    STOP_WORD_RE = re.compile(r'^(?:Common Wargear|Chapter Rules|Clan Rules|Dynasty Rules)\s*$', re.I)
    PAGE_NUM_RE  = re.compile(r'^\d+$')

    lines = text.split('\n')
    rules: dict = {}
    cur_name: str | None = None
    cur_parts: list = []

    def flush():
        if cur_name and cur_parts:
            rules[cur_name] = ' '.join(cur_parts)

    for line in lines:
        s = line.strip()
        if identify_slot(line) or POINTS_RE.match(s) or STOP_WORD_RE.match(s):
            flush()
            break
        if PAGE_NUM_RE.match(s) or not s:
            continue

        m = RULE_DEF_RE.match(s)
        if m:
            flush()
            cur_name = m.group(1).strip()
            cur_parts = [m.group(2).strip()]
        elif cur_name:
            cur_parts.append(s)

    flush()

    # Secondary scan: pick up inline rule definitions that appear within unit
    # sections (2-column PDFs often place rule defs in the right column, which
    # pdftotext plain mode surfaces after slot headers).
    # Also handles bullet-point format: "• RuleName – desc" or "• RuleName: desc"
    _WEAPON_STAT_RE = re.compile(
        r'^\d+"\s+(?:Rapid Fire|Heavy|Assault|Pistol|Ordnance|Melee|S\d|AP)'  # range + weapon type
        r'|^Melee\s+S\s*(?:User|\+|\d)'                                        # melee weapon stat
        r'|^\d+\s+[\d\+\-]+\s+[\d\+\-]',                                       # bare stat values
        re.I,
    )
    BULLET_RE = re.compile(r'^[•\-]\s+([A-Z][A-Za-z\'\s\d!]{1,60}?)\s*[–\-:]\s+(.+)')
    cur_name = None
    cur_parts = []
    in_unit_section = False

    def flush2():
        if cur_name and cur_parts:
            desc = ' '.join(cur_parts)
            if _WEAPON_STAT_RE.search(desc):
                return
            # Store under both the raw name and a de-pluralised form so that
            # "Soulless Machines" (PDF) is found as "Soulless Machine" (unit rule).
            names_to_store = [cur_name]
            if cur_name.endswith('s') and not cur_name.endswith('ss'):
                names_to_store.append(cur_name[:-1])
            for n in names_to_store:
                if n not in rules:
                    rules[n] = desc

    for line in lines:
        s = line.strip()
        if identify_slot(line):
            in_unit_section = True
        if not s or PAGE_NUM_RE.match(s):
            if in_unit_section:
                flush2(); cur_name = None; cur_parts = []
            continue
        # Match inline dash-format definitions (anywhere in text)
        m = RULE_DEF_RE.match(s) or BULLET_RE.match(s)
        if m:
            flush2()
            cur_name = m.group(1).strip()
            cur_parts = [m.group(2).strip()]
        elif cur_name and in_unit_section and not identify_slot(line) and not STAT_HEADER_RE.match(s):
            cur_parts.append(s)

    flush2()
    return rules


_SLOT_NAME_TO_KEY = {
    'troop': 'troops', 'troops': 'troops',
    'elite': 'elites', 'elites': 'elites',
    'fast attack': 'fast_attack',
    'heavy support': 'heavy_support',
    'flyer': 'flyers', 'flyers': 'flyers',
    'hq': 'hq',
    'advisor': 'advisors', 'advisors': 'advisors',
    'lord of war': 'lord_of_war',
    'dedicated transport': 'dedicated_transport',
    'fortification': 'fortifications', 'fortifications': 'fortifications',
}

def _slot_key(name: str) -> str | None:
    n = name.strip().lower()
    return _SLOT_NAME_TO_KEY.get(n) or _SLOT_NAME_TO_KEY.get(n.rstrip('s'))

def parse_force_org_rule(description: str) -> dict | None:
    """
    Detect force-organisation modifiers in a rule description.
    Returns a dict with zero or more of:
      troops_eligible: {units: [str], slot: str}
      slot_changes:    [{slot: str, max?: int, delta?: int}]
    Returns None if no force-org content found.
    """
    result: dict = {}

    # "may treat X, Y and Z as Troop Slots (or their respective Slots)"
    TREAT_AS_RE = re.compile(
        r'(?:may|can) treat (.+?) as (\w+(?:\s+\w+)?)\s+slots?', re.I)
    m = TREAT_AS_RE.search(description)
    if m:
        units_raw  = re.sub(r'\([^)]*\)', '', m.group(1))   # strip parentheticals
        target_key = _slot_key(m.group(2))
        if target_key:
            parts = re.split(r',|\band\b', units_raw)
            units = [p.strip() for p in parts if p.strip()]
            if units:
                result['troops_eligible'] = {'units': units, 'slot': target_key}

    # "Lose all X Slots"  /  "gain +N Y Slots"
    LOSE_RE = re.compile(r'lose all (\w+(?:\s+\w+)?)\s+slots?', re.I)
    GAIN_RE = re.compile(r'gain \+?(\d+) (\w+(?:\s+\w+)?)\s+slots?', re.I)
    changes: list = []
    for m in LOSE_RE.finditer(description):
        k = _slot_key(m.group(1))
        if k:
            changes.append({'slot': k, 'max': 0})
    for m in GAIN_RE.finditer(description):
        k = _slot_key(m.group(2))
        if k:
            changes.append({'slot': k, 'delta': int(m.group(1))})
    if changes:
        result['slot_changes'] = changes

    return result if result else None


def extract_common_wargear(text: str) -> dict:
    """
    Parse the 'Common Wargear' section into {name: description} dict.
    Handles '• Name - description' format and multi-line continuations.
    """
    SECTION_START = re.compile(r'^Common Wargear\s*$', re.I)
    SECTION_END   = re.compile(
        r'^(Chapters?|Clans?|Dynasties|Regiments?|Sects?|Brotherhoods?|Warbands?'
        r'|Army Abilities|Hive Fleets?|HQ\b|Advisors?\b|Troops\b|Elites?\b'
        r'|Fast Attacks?\b|Heavy Support\b|Flyers?\b|Lords? of War\b'
        r'|Dedicated Transports?\b|Fortifications?\b|Warlord Traits?\b)\s*$',
        re.I,
    )
    BULLET = re.compile(r'^[•\*]\s+(.+?)\s+-\s+(.+)')

    lines = text.split('\n')
    in_section = False
    wargear: dict = {}
    current_name: str | None = None

    for line in lines:
        s = line.strip()
        if not in_section:
            if SECTION_START.match(s):
                in_section = True
            continue
        if SECTION_END.match(s) or (identify_slot(line) and s):
            break
        m = BULLET.match(s)
        if m:
            current_name = m.group(1).strip()
            wargear[current_name] = m.group(2).strip()
        elif current_name and s and not re.match(r'^\d+$', s) and not s.startswith('o '):
            # Continuation line (skip sub-bullets like "o 6\" Aura...")
            wargear[current_name] += ' ' + s

    return wargear


def extract_subfactions(text: str) -> list[dict]:
    """
    Find Chapter / Clan / Dynasty / Regiment sections and extract subfaction names
    and their rules.  Returns list of {name, rules: [{name, description}]} dicts.
    """
    SUBFACTION_SECTION = re.compile(
        r'^(Chapters?|Clans?|Dynasties|Regiments?|Sects?|Brotherhoods?|Warbands?)\s*$', re.I
    )
    SUBFACTION_NAME = re.compile(r'^([A-Z][A-Za-z\s\'-]+):$')
    SUBFACTION_RULE = re.compile(r'^-\s+([A-Za-z][A-Za-z\s\'-]+?):\s+(.+)')
    AVERAGE_RE = re.compile(r'^Average:', re.I)

    lines = text.split('\n')
    in_section = False
    subfactions: list = [{'name': 'Average', 'rules': []}]
    seen = {'average'}
    current_sub: dict | None = None

    for line in lines:
        s = line.strip()
        if SUBFACTION_SECTION.match(s):
            in_section = True
            current_sub = None
            continue
        if not in_section:
            continue
        if identify_slot(line):
            break
        if AVERAGE_RE.match(s):
            current_sub = subfactions[0]  # point at Average entry to capture its rules
            continue

        m_name = SUBFACTION_NAME.match(s)
        if m_name:
            name = m_name.group(1).strip()
            if name.lower() not in seen:
                current_sub = {'name': name, 'rules': []}
                subfactions.append(current_sub)
                seen.add(name.lower())
            continue

        if current_sub is not None:
            m_rule = SUBFACTION_RULE.match(s)
            if m_rule:
                rule = {
                    'name':        m_rule.group(1).strip(),
                    'description': m_rule.group(2).strip(),
                }
                current_sub['rules'].append(rule)
            elif s and current_sub['rules'] and not s.startswith('-'):
                # Skip bare page numbers
                if re.match(r'^\d+$', s):
                    continue
                current_sub['rules'][-1]['description'] += ' ' + s

    # Post-process: attach force_org to any rule whose description matches
    for sf in subfactions:
        for rule in sf.get('rules', []):
            fo = parse_force_org_rule(rule['description'])
            if fo:
                rule['force_org'] = fo

    return subfactions


# ── Per-codex metadata ────────────────────────────────────────────────────────
def extract_metadata(text: str) -> dict:
    """Extract difficulty, description snippets."""
    diff_re = re.compile(r'Army Difficulty\s+1-5:\s*(\d)', re.I)
    desc_re = re.compile(r'What (?:are|is) the? (.+?)\?', re.I)

    diff = 2
    m = diff_re.search(text)
    if m:
        diff = int(m.group(1))

    return {'difficulty': diff}


# ── Main entry point ──────────────────────────────────────────────────────────
# Map PDF filename stems to the IDs used in factions.json
FILENAME_TO_ID = {
    'space-marines-codex': 'space-marines',
    'ork-codex':           'orks',
    'imperial-guard-codex':'imperial-guard',
    'eldar-codex':         'eldar',
    'necron-codex':        'necrons',
    'tyranid-codex':       'tyranids',
    'chaos-undivided-codex': 'chaos-undivided',
    'tau-empire-codex':    'tau-empire',
    'dark-eldar-codex-1':  'dark-eldar',
    'grey-knight-codex':   'grey-knights',
    'custode-codex':       'custodes',
    'imperial-knight-codex': 'imperial-knights',
    'chaos-knights-codex': 'chaos-knights',
    'inquisition-codex':   'inquisition',
    'sisters-of-battle-codex-2': 'sisters-of-battle',
    'squat-codex':         'squats',
    'genestealer-cults-1': 'genestealer-cults',
    'leviathan-codex':     'leviathan',
    'behemoth-codex':      'behemoth',
    'kraken-codex':        'kraken',
}


def process_pdf(pdf_path: Path) -> dict | None:
    stem = pdf_path.stem
    faction_id = FILENAME_TO_ID.get(stem, stem)

    print(f"Processing {pdf_path.name}…", end=' ', flush=True)
    try:
        text        = pdf_to_text(pdf_path)
        layout_text = pdf_to_text_layout(pdf_path)
    except RuntimeError as e:
        print(f"SKIP ({e})")
        return None

    faction_name  = extract_faction_name(text)
    meta          = extract_metadata(text)
    army_rules    = extract_army_rules(text)
    subfactions   = extract_subfactions(text)
    common_wargear = extract_common_wargear(layout_text)
    weapon_tables = parse_weapon_tables_from_layout(layout_text)
    slots         = parse_codex(text, faction_name, weapon_tables)

    unit_count = sum(len(v) for v in slots.values())
    print(f"→ {unit_count} units across {len(slots)} slots")

    return {
        'id':          faction_id,
        'name':        faction_name,
        'difficulty':  meta['difficulty'],
        'rules':       army_rules,
        'wargear':     common_wargear,
        'subfactions': subfactions,
        'slots':       slots,
    }


def update_factions_index(processed: list[dict]):
    """Merge newly extracted faction metadata into data/factions.json."""
    index_path = DATA_DIR / 'factions.json'
    existing = []
    if index_path.exists():
        with open(index_path) as f:
            existing = json.load(f)

    existing_by_id = {f['id']: f for f in existing}
    for codex in processed:
        fid = codex['id']
        entry = existing_by_id.get(fid, {'id': fid})
        entry.setdefault('name', codex['name'])
        entry.setdefault('category', 'Unknown')
        entry['difficulty'] = codex['difficulty']
        entry['file'] = f"{fid}.json"
        existing_by_id[fid] = entry

    with open(index_path, 'w') as f:
        json.dump(list(existing_by_id.values()), f, indent=2)
    print(f"\nUpdated data/factions.json ({len(existing_by_id)} factions)")


UNIT_TYPE_NAMES = [
    'Titanic Monster', 'Titanic Vehicle', 'Monstrous Infantry',
    'Combat Walker', 'Independent Character',
    'Infantry', 'Monster', 'Vehicle', 'Swarm', 'Fortification', 'Tank',
]

def extract_core_keywords(text: str) -> dict:
    """
    Extract keyword definitions from the core rulebook.
    Handles two formats:
      1. Terminology section: 'Keyword – description' with indented continuations
      2. Unit Types section: prose paragraphs starting with a known unit type name
    Returns dict: {keyword: description}
    """
    keywords: dict = {}

    # ── Terminology / Common Rules ────────────────────────────────────────────
    TERM_RE = re.compile(r'^([A-Z][A-Za-z\s#\(\)!/]{1,60}?)\s+[–\-]\s+(.+)')
    CONT_RE = re.compile(r'^\s{4,}(.+)')      # 4+ leading spaces = continuation
    PAGE_RE = re.compile(r'^\s*Page \d+ of \d+\s*$', re.I)
    RECAP_RE = re.compile(r'^RECAP\b', re.I)

    term_start = text.find('Terminology and Common Rules')
    if term_start != -1:
        section = text[term_start:]
        cur_name: str | None = None
        cur_parts: list = []

        def flush():
            if cur_name and cur_parts:
                keywords[cur_name] = ' '.join(cur_parts)

        for line in section.split('\n'):
            line = line.replace('\x0c', '')   # strip form-feed page breaks
            s = line.rstrip()
            if PAGE_RE.match(s) or RECAP_RE.match(s.strip()):
                continue
            m = TERM_RE.match(s)
            if m:
                flush()
                cur_name  = m.group(1).strip()
                cur_parts = [m.group(2).strip()]
            elif cur_name and CONT_RE.match(s):
                cur_parts.append(s.strip())
            elif s.strip() == '' and cur_name:
                # blank line ends a definition
                flush()
                cur_name  = None
                cur_parts = []
        flush()

    # ── Unit Types ─────────────────────────────────────────────────────────────
    ut_start = text.find('Unit Types:')
    if ut_start != -1:
        # Find the end of the Unit Types section (next major section header)
        next_section = text.find('\nObjectives', ut_start)
        ut_text = text[ut_start: next_section if next_section != -1 else ut_start + 8000]

        cur_name = None
        cur_parts = []

        def flush_ut():
            if cur_name and cur_parts:
                full = ' '.join(cur_parts)
                # Only store if not already captured from Terminology
                if cur_name not in keywords:
                    keywords[cur_name] = full

        for line in ut_text.split('\n'):
            s = line.strip()
            if not s or PAGE_RE.match(line):
                continue
            matched_type = next(
                (n for n in UNIT_TYPE_NAMES if s.startswith(n + ' ') or s.startswith(n + 's ')),
                None,
            )
            if matched_type:
                flush_ut()
                cur_name  = matched_type
                cur_parts = [s]
            elif cur_name and s and not s.startswith('Independent Character'):
                cur_parts.append(s)

        flush_ut()

    # ── Manual supplement: universal rules contextually described in rulebook ──
    # These rules are referenced throughout but lack a formal "Name – def" block.
    _SUPPLEMENT = {
        'Flying':       'Flyers ignore terrain and vertical distance when moving. If the model cannot be safely positioned on a terrain piece, it must move or land elsewhere.',
        'High Altitude':'This model must move its minimum movement of 20" in a straight line each Movement Phase, making a single turn of up to 90 degrees at the end. High Altitude models may opt to move off any table edge to enter Reserves, re-entering from your Deployment Zone the following turn. Enemies that lack AA, Flying, or High Altitude suffer -1 To Hit against this model. Blast weapons may fire Snap Shots at High Altitude models; Indirect weapons may never target them.',
        'Hover':        'This model switches from High Altitude to Hover mode. While Hovering it follows normal vehicle/monster movement rules instead of High Altitude rules, and may be targeted and hit normally.',
        'Fly':          'This model ignores terrain and vertical distance when moving, treating all ground as open terrain. It may not benefit from area terrain cover saves while flying.',
        'Open Topped':  'Add +2 to the result when rolling on the Vehicle Damage Chart against this model after a Penetration. Embarked models may fire their weapons from inside as if using Fire Ports, and may be hit by Blast and Template weapons that overlap the vehicle.',
        'Immobile':     'This model cannot Move, Advance, Charge, Flee, or Pivot for any reason.',
        'Unique':       'Only one model or unit with this rule may be included in your army. You may not take duplicates.',
        'Steed':        'Mounted Infantry. The model is mounted on a steed or bike and gains the relevant movement bonuses but must roll Dangerous Terrain tests when entering terrain other than Hills, may not climb terrain, and may not board Transports.',
        'Infiltrate':   'After all other units and Infiltrators are deployed, place this unit at least 12" from enemy models, alternating with the opponent. These do not move during Scout moves.',
        'Stealth':      'This model gains +1 to Cover Saves. If in the open it has a 6+ Cover Save. This stacks with Going to Ground up to a maximum of a 3+ Cover Save.',
        'Fleet':        'This model may reroll its Advance distance in the Shooting Phase.',
        'Counterattack':'When this unit is successfully charged, it may immediately make a free round of Melee attacks before the normal combat begins, provided it passes a Leadership test.',
        'Jump':         'This model is Jump Infantry. It may move as if Flying during the Movement Phase, ignoring terrain and vertical distance. It may still be charged and fight in Melee normally.',
        'Immobilized':       'The vehicle permanently Moves, Advances, Charges, and Flees at half speed for the rest of the game. This effect can stack, rounding down to the nearest whole number until it hits 0.',
        'Building':          'Buildings are Fortifications placed during Terrain setup. They have levels — as a Building loses Wounds equal to a level, the top habitable level collapses. Models may embark inside a Building from any edge within 1" and benefit from the same rules as being inside a Transport. A destroyed Building follows Force Disembark rules.',
        'Psychic Mastery Level #': 'This model has # Psychic Mastery Level(s). It may cast one spell per Psychic Phase for each Psychic Mastery Level it has (but each spell only once per Phase). It adds its Psychic Mastery Level to the 2d6 casting roll. It may also attempt to Deny the Witch once per Psychic Phase.',
    }
    for name, desc in _SUPPLEMENT.items():
        if name not in keywords:
            keywords[name] = desc

    return keywords


def process_core_rules():
    """Extract keywords from the core rulebook and write data/core-keywords.json."""
    pdf_path = SOURCE_DIR / 'alternate-40k-core-rule-book.pdf'
    if not pdf_path.exists():
        print(f"Core rulebook not found at {pdf_path}")
        return

    result = subprocess.run(
        ['pdftotext', '-layout', str(pdf_path), '-'],
        capture_output=True, text=True,
    )
    text = result.stdout
    if not text.strip():
        print("pdftotext returned no text for core rulebook")
        return

    keywords = extract_core_keywords(text)
    out_path = DATA_DIR / 'core-keywords.json'
    with open(out_path, 'w') as f:
        json.dump(keywords, f, indent=2, sort_keys=True)
    print(f"Wrote {len(keywords)} core keywords → {out_path}")


def build_global_weapons_registry(processed: list) -> None:
    """
    Collect every weapon name→profile from all extracted codexes
    and write data/weapons.json.  Used as a cross-codex fallback lookup.
    """
    registry: dict = {}
    for codex in processed:
        for units in codex.get('slots', {}).values():
            for unit in units:
                for name, profile in unit.get('weapons', {}).items():
                    if name and name not in registry and profile.get('range'):
                        registry[name] = profile
    out_path = DATA_DIR / 'weapons.json'
    with open(out_path, 'w') as f:
        json.dump(registry, f, indent=2, sort_keys=True)
    print(f"Wrote {len(registry)} weapon profiles → {out_path}")


def build_global_wargear_registry(processed: list) -> None:
    """
    Merge all codex-level wargear description dicts into data/wargear.json.
    Used as a cross-codex fallback for non-weapon equipment lookups.
    """
    registry: dict = {}
    for codex in processed:
        for name, desc in codex.get('wargear', {}).items():
            if name and name not in registry:
                registry[name] = desc
    out_path = DATA_DIR / 'wargear.json'
    with open(out_path, 'w') as f:
        json.dump(registry, f, indent=2, sort_keys=True)
    print(f"Wrote {len(registry)} wargear descriptions → {out_path}")


def build_global_rules_registry(processed: list) -> None:
    """
    Merge all codex-level rules dicts into data/common-rules.json.
    Used as a cross-codex fallback when a rule isn't in the current codex.
    Rules that appear in multiple codexes with consistent text are included;
    any rule found in at least one codex is included.
    """
    registry: dict = {}
    for codex in processed:
        for name, desc in codex.get('rules', {}).items():
            if name and name not in registry:
                registry[name] = desc
    out_path = DATA_DIR / 'common-rules.json'
    with open(out_path, 'w') as f:
        json.dump(registry, f, indent=2, sort_keys=True)
    print(f"Wrote {len(registry)} common rules → {out_path}")


def main():
    targets = sys.argv[1:]

    # PDFs that are not unit codexes (core rules, scenarios, etc.)
    NON_CODEX = {
        'alternate-40k-core-rule-book.pdf',
        'alternate-40k-rules-scenario-the-meat-grinder.pdf',
        'alternate-40k-rules-slow-grow-packet.pdf',
    }

    if targets:
        pdf_paths = [SOURCE_DIR / t for t in targets]
    else:
        pdf_paths = sorted(
            p for p in SOURCE_DIR.glob('*.pdf')
            if p.name not in NON_CODEX
        )

    if not pdf_paths:
        print("No PDFs found. Run from the project root with PDFs present.")
        sys.exit(1)

    processed = []
    for pdf_path in pdf_paths:
        if not pdf_path.exists():
            print(f"Not found: {pdf_path}")
            continue
        codex = process_pdf(pdf_path)
        if codex is None:
            continue
        out_path = DATA_DIR / f"{codex['id']}.json"
        with open(out_path, 'w') as f:
            json.dump(codex, f, indent=2)
        processed.append(codex)

    if processed:
        update_factions_index(processed)
        build_global_weapons_registry(processed)
        build_global_wargear_registry(processed)
        build_global_rules_registry(processed)
        print(f"\nDone. {len(processed)} codex file(s) written to data/")

    if not targets:
        process_core_rules()


if __name__ == '__main__':
    main()
