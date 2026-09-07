from __future__ import annotations

import csv
import os
import re
import queue
import shutil
import sys
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass
from collections import deque
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from PIL import Image, ImageOps, ImageTk
except Exception:
    Image = ImageOps = ImageTk = None

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    BaseTk = TkinterDnD.Tk
    DND = True
except Exception:
    DND_FILES = None
    BaseTk = tk.Tk
    DND = False

from recvx_core import (
    AfsArchive,
    AldDocument,
    Codec,
    Elf32,
    ElfTextDocument,
    FontData,
    RdxDocument,
    parse_tbl,
    recvx_pack_literals,
    recvx_pack_prs,
    probe_compressed_rdx,
    probe_compressed_rdx_text,
    backup_once,
)
from recvx_font_texture import find_font_banks
from recvx_scan import (
    Iso9660Reader, IsoAfsArchive, StreamAfsArchive, clean_iso_component, extract_zip_candidates,
    is_recvx_candidate, is_rdx_afs_name, iso_quick_priority, looks_like_iso, replace_afs_entry_streaming,
    replace_afs_entries_streaming, find_iso_entry, replace_iso_entry_from_file, replace_iso_afs_entry_streaming,
    replace_iso_afs_entries_streaming,
)

TITLE = 'Tool RECVX PS2 Texts'
BG = '#0c0e11'
PANEL = '#14171b'
PANEL2 = '#1b1f24'
BORDER = '#30353c'
TEXT = '#f1f2f3'
MUTED = '#9ca3aa'
ACC = '#d6a354'
SEL = '#5a4325'
CANVAS = '#080a0d'

LANG_SUFFIX = {'1': 'ENG', '2': 'FRA', '4': 'SPA', '5': 'GER'}
EU_LANGS = ('ENG', 'FRA', 'SPA', 'GER')
LANG_NUM = {'ENG':'1', 'FRA':'2', 'SPA':'4', 'GER':'5'}
TBL_NAMES = {
    'ENG': 'ENG.tbl', 'FRA': 'FRA.tbl', 'SPA': 'SPA.tbl', 'GER': 'GER.tbl',
    'ARA': 'Custom TBL AR.tbl', 'JPN': 'JPN.tbl',
}
TOKENS = ['[LINE]', '[PAGE]', '[WAIT:001E]', '[ITEM:0001]', '[WHITE]', '[BLUE]', '[RED]', '[GREEN]', '[GREY]', '[SELECT]']

STR = {
'en': {
    'open': 'Open', 'folder': 'Folder', 'save': 'Save', 'export': 'Export CSV', 'import': 'Import CSV',
    'source': 'Source', 'table': 'TBL', 'search': 'Search', 'texts': 'Texts', 'font': 'Font', 'tbl': 'TBL',
    'drop': 'Drop file / folder / ISO', 'editor': 'Text', 'preview': 'Game Preview', 'rtl': 'RTL', 'bounds': 'Bounds', 'spacearea': 'Space Area',
    'openelf': 'Open ELF', 'savefont': 'Save Font', 'mode': 'Spacing Table', 'origin': 'Origin / ENG', 'pal': 'PAL / EU',
    'advance': 'Advance', 'height': 'Box H', 'space': 'Space', 'apply': 'Apply', 'positions': 'Message XY',
    'code': 'Code', 'char': 'Char', 'width': 'W', 'x': 'X', 'y': 'Y', 'value': 'Value', 'savetbl': 'Save TBL',
    'loadtbl': 'Load TBL', 'ready': 'Ready', 'working': 'Scanning', 'saved': 'Saved', 'alltexts': 'All Texts',
    'room': 'Room / File', 'lang': 'Lang', 'texture': 'Font Texture', 'page': 'Page', 'exportpng': 'Export PNG',
    'replacepng': 'Replace PNG', 'no_text': 'No text', 'glyphpage': 'Texture Page', 'screen': '640 × 448',
    'previewbox': 'Preview H only', 'extracttm2': 'Extract Font TM2', 'replacetm2': 'Replace Font TM2',
    'iso_index': 'ISO • Reading file list', 'iso_extract': 'ISO • Loading', 'texts_progress': 'Texts',
    'rooms_progress': 'Rooms', 'rdx_progress': 'RDX', 'scope': 'Show', 'scope_all': 'All', 'scope_rdx': 'RDX', 'scope_elf': 'ELF',
    'reversear': 'Reverse Arabic', 'alreadyrev': 'Arabic text is already reversed', 'tblcustom': 'Custom TBL AR', 'customartbl': 'Custom TBL AR', 'about': 'About',
},
'ar': {
    'open': 'فتح', 'folder': 'مجلد', 'save': 'حفظ', 'export': 'تصدير CSV', 'import': 'استيراد CSV',
    'source': 'المصدر', 'table': 'TBL', 'search': 'بحث', 'texts': 'النصوص', 'font': 'الخط', 'tbl': 'TBL',
    'drop': 'اسقط ملف / مجلد / ISO', 'editor': 'النص', 'preview': 'معاينة اللعبة', 'rtl': 'يمين لليسار', 'bounds': 'الحدود', 'spacearea': 'مساحة الفراغ',
    'openelf': 'فتح ELF', 'savefont': 'حفظ الخط', 'mode': 'جدول المسافات', 'origin': 'الأصلي / ENG', 'pal': 'PAL / EU',
    'advance': 'المسافة', 'height': 'ارتفاع المربع', 'space': 'الفراغ', 'apply': 'تطبيق', 'positions': 'إحداثيات النص',
    'code': 'الكود', 'char': 'الحرف', 'width': 'العرض', 'x': 'X', 'y': 'Y', 'value': 'القيمة', 'savetbl': 'حفظ TBL',
    'loadtbl': 'فتح TBL', 'ready': 'جاهز', 'working': 'فحص', 'saved': 'تم الحفظ', 'alltexts': 'كل النصوص',
    'room': 'الغرفة / الملف', 'lang': 'اللغة', 'texture': 'تكتشر الخط', 'page': 'صفحة', 'exportpng': 'استخراج PNG',
    'replacepng': 'استبدال PNG', 'no_text': 'لا يوجد نص', 'glyphpage': 'صفحة التكتشر', 'screen': '640 × 448',
    'previewbox': 'ارتفاع المعاينة فقط', 'extracttm2': 'استخراج TM2 للخط', 'replacetm2': 'استبدال TM2 للخط',
    'iso_index': 'ISO • قراءة الملفات', 'iso_extract': 'ISO • تحميل', 'texts_progress': 'النصوص',
    'rooms_progress': 'الغرف', 'rdx_progress': 'RDX', 'scope': 'عرض', 'scope_all': 'الكل', 'scope_rdx': 'RDX', 'scope_elf': 'ELF',
    'reversear': 'عكس العربية', 'alreadyrev': 'النص العربي معكوس بالفعل', 'tblcustom': 'Custom TBL AR', 'customartbl': 'Custom TBL AR', 'about': 'About',
},
}


def here() -> Path:
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent



# Arabic visual-form support -------------------------------------------------
# Arabic shaping is driven by the ACTIVE TBL.  We still use Unicode only to
# understand what each presentation-form glyph represents; the actual glyph
# emitted to the game must be one of the characters present in codec.t2c.
_AR_TOKEN_RE = re.compile(r'(\[[^\]]+\]|&[^;]+;)')

def _ar_base_letter(ch: str) -> bool:
    o = ord(ch)
    return 0x0600 <= o <= 0x06FF and unicodedata.category(ch).startswith('L')

def _ar_presentation(ch: str) -> bool:
    o = ord(ch)
    return 0xFB50 <= o <= 0xFDFF or 0xFE70 <= o <= 0xFEFF

def _ar_form_name(ch: str):
    name = unicodedata.name(ch, '')
    for f in ('ISOLATED', 'FINAL', 'INITIAL', 'MEDIAL'):
        if f' {f} FORM' in name:
            return f.lower()
    return None

def _tbl_ar_profile(codec):
    """Build Arabic joining forms strictly from the currently loaded TBL."""
    if codec is None:
        return None
    # Rebuild automatically after the user edits a TBL mapping in the UI.
    sig = tuple(sorted((str(k), int(v)) for k, v in codec.t2c.items() if len(str(k)) == 1 and (_ar_base_letter(str(k)) or _ar_presentation(str(k)))))
    cached = getattr(codec, '_tbl_ar_profile_cache', None)
    if cached and cached[0] == sig:
        return cached[1]
    forms = {}
    ligatures = {}
    visual = set()
    direct = set()
    for token in codec.t2c.keys():
        if len(token) != 1:
            continue
        ch = token
        if _ar_presentation(ch):
            form = _ar_form_name(ch)
            norm = unicodedata.normalize('NFKC', ch)
            if not form:
                continue
            if len(norm) == 1 and _ar_base_letter(norm):
                forms.setdefault(norm, {})[form] = ch
                visual.add(ch)
            elif len(norm) == 2 and all(_ar_base_letter(x) for x in norm):
                ligatures.setdefault(norm, {})[form] = ch
                visual.add(ch)
        elif _ar_base_letter(ch):
            # Some custom tables intentionally map a plain Arabic code point to
            # a standalone game glyph.  It is valid only because it exists in
            # this TBL; never synthesize a glyph that is absent from the table.
            forms.setdefault(ch, {}).setdefault('isolated', ch)
            direct.add(ch)
    profile = {'forms': forms, 'ligatures': ligatures, 'visual': visual, 'direct': direct}
    try: codec._tbl_ar_profile_cache = (sig, profile)
    except Exception: pass
    return profile

def _ar_strip_marks(text: str) -> str:
    out=[]
    for c in text:
        name=unicodedata.name(c, '')
        if 'ARABIC' in name and (unicodedata.combining(c) or unicodedata.category(c) in ('Mn','Me')):
            continue
        out.append(c)
    return ''.join(out)

def _tbl_form(profile, key, wanted):
    table = profile['ligatures'].get(key) if len(key) == 2 else profile['forms'].get(key)
    if not table:
        return None
    fallback = {
        'isolated': ('isolated','final','initial','medial'),
        'final': ('final','isolated','medial','initial'),
        'initial': ('initial','isolated','medial','final'),
        'medial': ('medial','final','initial','isolated'),
    }[wanted]
    for name in fallback:
        if name in table:
            return table[name]
    return next(iter(table.values()), None)

def _tbl_can_prev(profile, key):
    table = profile['ligatures'].get(key) if len(key) == 2 else profile['forms'].get(key, {})
    return 'final' in table or 'medial' in table

def _tbl_can_next(profile, key):
    table = profile['ligatures'].get(key) if len(key) == 2 else profile['forms'].get(key, {})
    return 'initial' in table or 'medial' in table

def _tbl_shape_arabic(text: str, codec) -> str:
    """Shape natural Arabic with ONLY glyph forms contained in *codec*'s TBL."""
    profile = _tbl_ar_profile(codec)
    if not profile:
        return text
    chars = list(_ar_strip_marks(text))
    items=[]; i=0
    while i < len(chars):
        ch=chars[i]
        if _ar_base_letter(ch):
            # The custom table decides whether a Lam-Alef ligature exists.
            if ch == 'ل' and i + 1 < len(chars):
                pair = ch + chars[i+1]
                if pair in profile['ligatures']:
                    items.append(('ar', pair)); i += 2; continue
            if ch not in profile['forms']:
                raise ValueError(f'Arabic character is not available in the active TBL: {ch!r} (U+{ord(ch):04X})')
            items.append(('ar', ch)); i += 1; continue
        items.append(('other', ch)); i += 1

    out=[]
    for idx,(kind,key) in enumerate(items):
        if kind != 'ar':
            out.append(key); continue
        prev_key = items[idx-1][1] if idx > 0 and items[idx-1][0] == 'ar' else None
        next_key = items[idx+1][1] if idx+1 < len(items) and items[idx+1][0] == 'ar' else None
        join_prev = bool(prev_key and _tbl_can_next(profile, prev_key) and _tbl_can_prev(profile, key))
        join_next = bool(next_key and _tbl_can_next(profile, key) and _tbl_can_prev(profile, next_key))
        wanted = 'medial' if join_prev and join_next else 'final' if join_prev else 'initial' if join_next else 'isolated'
        glyph = _tbl_form(profile, key, wanted)
        if glyph is None or glyph not in codec.t2c:
            raise ValueError(f'Arabic {wanted} form is not available in the active TBL for: {key!r}')
        out.append(glyph)
    return ''.join(out)

def _ar_bidi_display(text: str) -> str:
    # The game renderer advances left-to-right. Reverse the visual stream while
    # preserving Latin/number runs internally. The Arabic glyphs have already
    # been selected from the active TBL before this step.
    rev=list(text[::-1]); out=[]; i=0
    def ltrish(c):
        return unicodedata.bidirectional(c) in ('L','EN','AN')
    while i < len(rev):
        if ltrish(rev[i]):
            j=i+1
            while j < len(rev) and ltrish(rev[j]): j += 1
            out.extend(reversed(rev[i:j])); i=j
        else:
            out.append(rev[i]); i += 1
    return ''.join(out)

def _ar_form_kind(ch: str):
    """Return the joining-form kind encoded by one Arabic TBL glyph."""
    if _ar_presentation(ch):
        return _ar_form_name(ch)
    if _ar_base_letter(ch):
        # Plain Arabic entries in a modified TBL are treated as neutral
        # standalone glyphs.  They are not evidence that the whole line is
        # already in the game's left-to-right byte order.
        return 'base'
    return None

def _ar_logical_run_score(chars) -> int:
    """Score how much a shaped Arabic run looks like logical RTL order.

    In logical order a connected word normally begins with INITIAL/MEDIAL-like
    connectivity and ends with FINAL.  A game-ready RECVX run is the reverse of
    that because the engine advances glyphs from left to right.
    """
    forms=[_ar_form_kind(ch) for ch in chars]
    forms=[f for f in forms if f and f != 'base']
    if len(forms) < 2:
        return 0
    score=0
    first,last=forms[0],forms[-1]
    if first == 'initial': score += 4
    elif first == 'isolated': score += 1
    elif first == 'final': score -= 3
    if last == 'final': score += 4
    elif last == 'isolated': score += 1
    elif last == 'initial': score -= 3
    for a,b in zip(forms,forms[1:]):
        opens_next = a in ('initial','medial')
        accepts_prev = b in ('final','medial')
        if opens_next and accepts_prev:
            score += 2
        elif opens_next != accepts_prev:
            score -= 1
    return score

def _ar_presentation_direction(text: str):
    """Return 'logical', 'game', or None for presentation-form Arabic.

    This fixes the old false assumption that *every* U+FBxx/U+FExx string was
    already reversed.  Modified Arabic TBLs commonly decode to presentation
    forms even when the sequence is still in logical Arabic order.
    """
    logical=game=evidence=0
    run=[]
    def flush():
        nonlocal logical,game,evidence,run
        if len(run) >= 2 and any(_ar_presentation(c) for c in run):
            ls=_ar_logical_run_score(run)
            gs=_ar_logical_run_score(list(reversed(run)))
            logical += ls; game += gs; evidence += abs(ls-gs)
        run=[]
    for ch in text:
        if _ar_base_letter(ch) or _ar_presentation(ch):
            run.append(ch)
        else:
            flush()
    flush()
    if evidence < 3 or logical == game:
        return None
    return 'logical' if logical > game else 'game'

def _ar_unshape(text: str) -> str:
    """Convert existing presentation glyphs back to base letters only."""
    out=[]
    for ch in text:
        if _ar_presentation(ch):
            norm=unicodedata.normalize('NFKC', ch)
            out.append(norm if norm else ch)
        else:
            out.append(ch)
    return ''.join(out)

def _prepare_arabic_body(body: str, codec) -> tuple[str, bool]:
    has_base=any(_ar_base_letter(c) for c in body)
    has_pres=any(_ar_presentation(c) for c in body)
    if not (has_base or has_pres):
        return body,False

    # Plain Arabic is entered in natural order. Shape exclusively from the
    # active TBL and reverse the visual stream for RECVX's LTR renderer.
    if has_base and not has_pres:
        shaped=_tbl_shape_arabic(body, codec)
        visual=_ar_bidi_display(shaped)
        return visual, visual != body

    direction=_ar_presentation_direction(body)

    # Presentation-form text can still be in *logical* order.  This is exactly
    # the case the old Reverse Arabic button incorrectly rejected.
    if has_pres and not has_base:
        if direction == 'logical':
            visual=_ar_bidi_display(body)
            return visual, visual != body
        # 'game' means the TBL-decoded glyph stream is already in the exact
        # left-to-right order the game consumes. Ambiguous shaped strings are
        # left untouched to avoid a destructive double reverse.
        return body,False

    # Mixed base + presentation forms can happen after editing one character in
    # an already translated line. Canonicalize through the active TBL. If the
    # shaped portion is already in game order, first recover logical order.
    logical_body=_ar_bidi_display(body) if direction == 'game' else body
    natural=_ar_unshape(logical_body)
    shaped=_tbl_shape_arabic(natural, codec)
    visual=_ar_bidi_display(shaped)
    return visual, visual != body

def _ar_needs_tbl_visual(text: str, codec) -> bool:
    has_base=any(_ar_base_letter(c) for c in text)
    has_pres=any(_ar_presentation(c) for c in text)
    if has_base:
        return True
    if has_pres:
        return _ar_presentation_direction(text) == 'logical'
    return False

def prepare_arabic_visual(text: str, codec=None) -> tuple[str, bool]:
    """Return the exact Arabic glyph order RECVX will render left-to-right.

    Control tokens, line/page boundaries, Latin text and number runs are kept
    intact.  Direction is inferred from the Arabic presentation forms in the
    active modified TBL instead of assuming that presentation-form Unicode is
    automatically already reversed.
    """
    has_ar=any(_ar_base_letter(c) or _ar_presentation(c) for c in text)
    if not has_ar:
        return text,False
    if codec is None:
        return text,False
    parts=_AR_TOKEN_RE.split(text); result=[]; changed=False
    for part in parts:
        if not part:
            continue
        if _AR_TOKEN_RE.fullmatch(part):
            result.append(part); continue
        chunks=part.splitlines(keepends=True)
        if not chunks: chunks=[part]
        for chunk in chunks:
            body=chunk; ending=''
            if body.endswith('\r\n'):
                body,ending=body[:-2],'\r\n'
            elif body.endswith(('\n','\r')):
                body,ending=body[:-1],body[-1]
            converted_body,did_change=_prepare_arabic_body(body, codec)
            result.append(converted_body + ending)
            changed = changed or did_change
    converted=''.join(result)
    # Every Arabic glyph shown by Game Preview must resolve through the same
    # active TBL that will be used for save/import.
    for ch in converted:
        if (_ar_base_letter(ch) or _ar_presentation(ch)) and ch not in codec.t2c:
            raise ValueError(f'Arabic output is not present in the active TBL: {ch!r} (U+{ord(ch):04X})')
    return converted, changed

def reverse_arabic_order_explicit(text: str) -> tuple[str, bool]:
    """Explicit user-requested direction toggle, without touching tokens.

    Unlike automatic save normalization, this intentionally works even when
    presentation-form text is already in game order. It is used only by the
    Reverse Arabic button; automatic preview/save still protects against
    accidental double reversal.
    """
    parts=_AR_TOKEN_RE.split(text); result=[]; changed=False
    for part in parts:
        if not part:
            continue
        if _AR_TOKEN_RE.fullmatch(part):
            result.append(part); continue
        chunks=part.splitlines(keepends=True)
        if not chunks: chunks=[part]
        for chunk in chunks:
            body=chunk; ending=''
            if body.endswith('\r\n'):
                body,ending=body[:-2],'\r\n'
            elif body.endswith(('\n','\r')):
                body,ending=body[:-1],body[-1]
            if any(_ar_base_letter(c) or _ar_presentation(c) for c in body):
                rev=_ar_bidi_display(body)
                changed = changed or (rev != body)
                body=rev
            result.append(body + ending)
    return ''.join(result),changed

def detect_game_lang(path: Path) -> str:
    up = '/' + '/'.join(x.upper() for x in path.parts) + '/'
    for d, lang in [('ENG', 'ENG'), ('FRA', 'FRA'), ('SPA', 'SPA'), ('GER', 'GER'), ('ARA', 'ARA'), ('ARABIC', 'ARA'), ('JPN', 'JPN'), ('JAP', 'JPN')]:
        if f'/{d}/' in up:
            return lang
    m = re.search(r'SYSMES(\d*)\.ALD$', path.name, re.I)
    if m:
        return LANG_SUFFIX.get(m.group(1), 'JPN' if not m.group(1) else 'ENG')
    m = re.search(r'(?:RDX_LNK|SYSTEM)(\d*)', path.name, re.I)
    if m:
        return LANG_SUFFIX.get(m.group(1), 'JPN' if not m.group(1) else 'ENG')
    return 'ENG'


def path_matches_game_lang(path_or_name, lang: str) -> bool:
    """True when a RECVX language-specific path belongs to *lang*.

    Shared executables/config files are accepted for every language. European
    language banks use suffixes 1/2/4/5 and/or ENG/FRA/SPA/GER directories.
    """
    lang = (lang or 'ENG').upper()
    raw = str(path_or_name).replace('\\', '/').upper()
    base = raw.rsplit('/', 1)[-1].split(';', 1)[0]
    if base in ('SYSTEM.CNF',) or re.match(r'^(SLES|SLUS|SCES|SCUS|SLPM|SLPS|SLAJ|SLED|SCCS)[_.-]?\d', base):
        return True
    # Explicit language directories always win.
    for code in EU_LANGS:
        if f'/{code}/' in '/' + raw.strip('/') + '/':
            return code == lang
    n = LANG_NUM.get(lang, '1')
    for pat in (r'^SYSMES(\d*)\.ALD$', r'^SYSTEM(\d*)\.AFS$', r'^RDX_LNK(\d*)\.AFS$'):
        m = re.match(pat, base, re.I)
        if m:
            return (m.group(1) or '') == n
    # Loose RDX files without a language marker are useful in extracted
    # single-language folders, so do not reject them here.
    return True


def build_tbl_lookup(root: Path) -> dict[str, Path]:
    by_name = {}
    try:
        for p in root.rglob('*'):
            if p.is_file() and p.suffix.lower() == '.tbl':
                by_name.setdefault(p.name.lower(), p)
    except Exception:
        pass
    out = {}
    for lang, name in TBL_NAMES.items():
        candidates = [name]
        if lang == 'ENG': candidates.append('US.tbl')
        if lang == 'SPA': candidates.append('ES.tbl')
        for n in candidates:
            if n.lower() in by_name:
                out[lang] = by_name[n.lower()]
                break
        if lang not in out:
            bundled = here() / name
            if bundled.exists(): out[lang] = bundled
    return out


@dataclass
class Source:
    label: str
    lang: str
    path: Path
    doc: object
    tbl_path: Path
    codec: Codec
    kind: str
    dirty: bool = False
    afs_path: Path | None = None
    afs_index: int | None = None
    afs_compressed: bool = False
    afs_name: str = ''
    packed_path: Path | None = None
    raw_loader: object | None = None
    iso_path: Path | None = None
    iso_entry_path: str = ''
    iso_afs_index: int | None = None

    @property
    def messages(self):
        return self.doc.messages

    @property
    def source_key(self) -> str:
        if self.afs_path is not None and self.afs_index is not None:
            return f'{self.afs_path.name}:{self.afs_index}'
        if self.packed_path is not None:
            return self.packed_path.name
        return self.path.name

    @property
    def room_name(self) -> str:
        if self.afs_name:
            return Path(self.afs_name).stem
        if self.afs_path is not None and self.afs_index is not None:
            return f'{self.afs_path.stem}:{self.afs_index:04d}'
        return self.path.stem

    def _materialize_full_rdx(self) -> None:
        if self.kind != 'RDX' or not getattr(self.doc, '_text_only_prefix', False):
            return
        if not callable(self.raw_loader):
            raise ValueError('RDX source cannot be materialized for saving')
        stored = self.raw_loader()
        raw = probe_compressed_rdx(stored) if self.afs_compressed else stored
        if raw is None:
            raise ValueError('RDX full decode failed')
        full = RdxDocument(self.path, initial_data=raw)
        if len(full.messages) != len(self.doc.messages):
            raise ValueError('RDX message count changed during materialization')
        for dst, src in zip(full.messages, self.doc.messages):
            dst.words = list(src.words)
        self.doc = full

    def prepare_rdx_batch_payload(self, payload_path: Path, progress=None) -> Path:
        """Prepare one edited RDX for a grouped AFS repack without touching the ISO/AFS yet."""
        if self.kind != 'RDX':
            raise ValueError('Batch payload is RDX-only')
        self._materialize_full_rdx()
        raw = self.doc.rebuild()
        RdxDocument(self.path, initial_data=raw)
        stored = recvx_pack_prs(raw, verify=False, progress=progress) if self.afs_compressed else raw
        if self.afs_compressed and probe_compressed_rdx(stored) != raw:
            raise ValueError('RDX compression verification failed')
        # self.path is a temporary expanded room for AFS/ISO sources. Keeping the
        # rebuilt raw here lets us release large byte arrays before the AFS pass.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(raw)
        payload_path = Path(payload_path)
        payload_path.parent.mkdir(parents=True, exist_ok=True)
        payload_path.write_bytes(stored)
        return payload_path

    def finish_rdx_batch_save(self) -> None:
        self.doc = RdxDocument(self.path)
        self.dirty = False

    def save(self, progress=None) -> None:
        def emit(frac, stage='Save'):
            if progress:
                try: progress(max(0, min(1000, int(float(frac) * 1000))), 1000, stage)
                except Exception: pass

        def mapped(lo, hi):
            span = hi - lo
            return lambda d, t, st: emit(lo + span * (d / max(1, t)), st)

        emit(0.0, 'Prepare')
        # ELF text uses the relocation-aware saver first. When the source came
        # from an ISO, write the resulting ELF back into that same ISO too.
        if self.kind == 'ELF':
            self.doc.codec = self.codec
            self.doc.save(); emit(0.15, 'ELF')
            if self.iso_path is not None and self.iso_entry_path:
                replace_iso_entry_from_file(self.iso_path, self.iso_entry_path, self.path,
                                            progress=mapped(0.15, 0.96))
            else:
                emit(0.96, 'ELF')
            self.doc = ElfTextDocument(self.path, self.lang, self.codec)
            self.dirty = False
            emit(1.0, 'Done')
            return

        self._materialize_full_rdx(); emit(0.04, 'Prepare')
        raw = self.doc.rebuild(); emit(0.08, 'Prepare')
        if self.kind == 'RDX':
            RdxDocument(self.path, initial_data=raw)
        else:
            vtmp = self.path.with_suffix(self.path.suffix + '.verifytmp')
            try:
                vtmp.parent.mkdir(parents=True, exist_ok=True); vtmp.write_bytes(raw); AldDocument(vtmp)
            finally:
                try: vtmp.unlink()
                except OSError: pass

        stored = recvx_pack_prs(raw, verify=False, progress=mapped(0.08, 0.35)) if (self.kind == 'RDX' and self.afs_compressed) else raw
        if self.kind == 'RDX' and self.afs_compressed:
            dec = probe_compressed_rdx(stored)
            if dec != raw:
                raise ValueError('RDX compression verification failed')
        emit(0.38, 'Verify')

        local_write = ((self.afs_path is not None and self.afs_index is not None)
                       or self.packed_path is not None or self.iso_path is None)
        iso_write = self.iso_path is not None and bool(self.iso_entry_path)
        if local_write and iso_write:
            local_lo, local_hi, iso_lo, iso_hi = 0.38, 0.64, 0.64, 0.96
        elif local_write:
            local_lo, local_hi, iso_lo, iso_hi = 0.38, 0.96, 0.96, 0.96
        else:
            local_lo, local_hi, iso_lo, iso_hi = 0.38, 0.38, 0.38, 0.96

        # Normal extracted-folder/AFS writeback.
        if self.afs_path is not None and self.afs_index is not None:
            replace_afs_entry_streaming(self.afs_path, self.afs_index, stored,
                                        progress=mapped(local_lo, local_hi))
        elif self.packed_path is not None:
            emit(local_lo, 'Backup')
            if probe_compressed_rdx(stored) != raw:
                raise ValueError('RDX compression verification failed')
            backup_once(self.packed_path)
            tmp = self.packed_path.with_suffix(self.packed_path.suffix + '.tmp')
            tmp.write_bytes(stored)
            chk = probe_compressed_rdx(tmp.read_bytes())
            if chk != raw:
                try: tmp.unlink()
                except OSError: pass
                raise ValueError('RDX packed file verification failed')
            os.replace(tmp, self.packed_path); emit(local_hi, 'Write')
        elif self.iso_path is None:
            self.doc.save(); emit(local_hi, 'Write')

        # ISO writeback is independent from the temporary extracted copy.
        if iso_write:
            if self.iso_afs_index is not None:
                replace_iso_afs_entry_streaming(self.iso_path, self.iso_entry_path,
                                                self.iso_afs_index, stored,
                                                progress=mapped(iso_lo, iso_hi))
            else:
                tmp_iso_payload = self.path.with_suffix(self.path.suffix + '.isopayloadtmp')
                try:
                    tmp_iso_payload.write_bytes(stored if (self.kind == 'RDX' and self.afs_compressed) else raw)
                    replace_iso_entry_from_file(self.iso_path, self.iso_entry_path, tmp_iso_payload,
                                                progress=mapped(iso_lo, iso_hi))
                finally:
                    try: tmp_iso_payload.unlink()
                    except OSError: pass

        emit(0.98, 'Finalize')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(raw)
        self.doc = RdxDocument(self.path) if self.kind == 'RDX' else AldDocument(self.path)
        self.dirty = False
        emit(1.0, 'Done')


class App(BaseTk):
    def __init__(self):
        super().__init__()
        self.title(TITLE)
        try:
            self.iconbitmap(default=str(here() / 'recvx_claire.ico'))
        except Exception:
            pass
        self.geometry('1040x700')
        self.minsize(820, 560)
        self.option_add('*Font', ('Segoe UI', 9))

        self.ui = 'en'
        self.sources: list[Source] = []
        self.source: Source | None = None
        self.msg = None
        self.view_options = []
        self.view_mode = ('none', None)
        self.tree_map = {}
        self.root_folder: Path | None = None
        self.temp: list[Path] = []
        self.font: FontData | None = None
        self.elf_path: Path | None = None
        self.font_banks = []
        self.font_bank = None
        self.height_overrides: dict[int, int] = {}
        self.selected_code: int | None = None
        self.preview_page_index = 0
        self._preview_refs = []
        self._font_ref = None
        self._loading = False
        self._scan_thread = None
        self._font_thread = None
        self.iso_source_path: Path | None = None
        self._root_generation = 0
        self._archive_serial = 0
        self._last_drop_path = ''
        self._last_drop_time = 0.0
        self._rdx_items = []
        self._win_drop_old = {}
        self._win_drop_proc = None
        self._win_drop_api = None
        self._ui_queue = queue.Queue()
        self.game_lang = 'ENG'
        self._iso_entries_cache = None
        self._iso_entries_cache_key = None
        self._text_row_queue = deque()
        self._text_row_serial = 0
        self._source_index_by_id = {}
        self._text_row_job = None
        self._font_started_generation = -1
        self._preview_transform = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 12.0, 12.0)
        self._lang_cache = {}
        self._input_identity = ''
        self.text_scope_value = 'ALL'
        self._save_busy = False
        self._save_thread = None
        self.custom_tbl_paths: dict[str, Path] = {}
        self.custom_ar_tbl_path = here() / 'Custom TBL AR.tbl'
        self._custom_ar_codec_cache = None
        # The Arabic TBL is a byte/glyph CONVERTER, not the storage decoder.
        # Keep it separate from the SPA/FRA/GER/ENG source codec so changing
        # tabs/scopes can never decode already-converted bytes back to Arabic.
        self._tbl_edit_path: Path | None = None
        self._tbl_edit_codec: Codec | None = None
        self._tbl_edit_is_ar = False
        self._manual_ar_order = False
        # Text preview uses a fixed 10x10 top-left inset.  X/Y editing is
        # intentionally disabled; only font-cell sizing remains mouse-editable.
        self._font_resize = None

        self._theme()
        self._build()
        self._drop_all()
        self.protocol('WM_DELETE_WINDOW', self.on_close)
        self.after(25, self._drain_ui_queue)

    def _post_ui(self, func, *args, **kwargs):
        self._ui_queue.put((func, args, kwargs))

    def _drain_ui_queue(self):
        # Never drain an unbounded worker queue in one Tk callback. Under a
        # large ISO this was enough to make Windows report the app as hung.
        deadline = time.perf_counter() + 0.008
        handled = 0
        try:
            while handled < 80 and time.perf_counter() < deadline:
                func, args, kwargs = self._ui_queue.get_nowait()
                try:
                    func(*args, **kwargs)
                except Exception as ex:
                    self.err(ex)
                handled += 1
        except queue.Empty:
            pass
        try:
            self.after(10 if handled else 25, self._drain_ui_queue)
        except tk.TclError:
            pass

    def t(self, key):
        return STR[self.ui].get(key, key)

    def show_about(self):
        messagebox.showinfo('About', 'Developed by SKZEZO')

    def _theme(self):
        self.configure(bg=BG)
        s = ttk.Style(self)
        try: s.theme_use('clam')
        except Exception: pass
        s.configure('.', background=BG, foreground=TEXT, fieldbackground=PANEL2, font=('Segoe UI', 9))
        s.configure('TFrame', background=BG)
        s.configure('TLabel', background=BG, foreground=TEXT)
        s.configure('TLabelframe', background=BG, foreground=MUTED)
        s.configure('TLabelframe.Label', background=BG, foreground=MUTED)
        s.configure('TButton', background=PANEL2, foreground=TEXT, bordercolor=BORDER, padding=(7, 4))
        s.map('TButton', background=[('active', '#272c33'), ('pressed', '#30363e')])
        s.configure('Accent.TButton', background=ACC, foreground='#111318', bordercolor=ACC, padding=(9, 5))
        s.configure('Treeview', rowheight=23, background=PANEL, fieldbackground=PANEL, foreground=TEXT, borderwidth=0)
        s.map('Treeview', background=[('selected', SEL)], foreground=[('selected', TEXT)])
        s.configure('Treeview.Heading', background=PANEL2, foreground=MUTED, relief='flat')
        s.configure('TNotebook', background=PANEL, borderwidth=0)
        s.configure('TNotebook.Tab', background=PANEL2, foreground=MUTED, padding=(10, 5))
        s.map('TNotebook.Tab', background=[('selected', SEL)], foreground=[('selected', TEXT)])
        s.configure('TCombobox', fieldbackground='#22272e', background='#22272e', foreground=TEXT, arrowcolor=TEXT)
        s.map('TCombobox', fieldbackground=[('readonly', '#22272e')], foreground=[('readonly', TEXT)])
        s.configure('Horizontal.TProgressbar', troughcolor=PANEL2, background=ACC)

    def _build(self):
        for w in self.winfo_children(): w.destroy()
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        bar = ttk.Frame(self, padding=(8, 7, 8, 4))
        bar.grid(row=0, column=0, sticky='ew')
        bar.columnconfigure(9, weight=1, minsize=90)
        ttk.Button(bar, text=self.t('open'), command=self.open_file).grid(row=0, column=0, padx=(0, 4))
        ttk.Button(bar, text=self.t('folder'), command=self.open_folder).grid(row=0, column=1, padx=4)
        ttk.Button(bar, text=self.t('save'), style='Accent.TButton', command=self.save_source).grid(row=0, column=2, padx=4)
        ttk.Button(bar, text=self.t('export'), command=self.export_csv).grid(row=0, column=3, padx=4)
        ttk.Button(bar, text=self.t('import'), command=self.import_csv).grid(row=0, column=4, padx=4)
        self.source_combo = ttk.Combobox(bar, state='readonly', width=21)
        self.source_combo.grid(row=0, column=5, padx=(9, 4))
        self.source_combo.bind('<<ComboboxSelected>>', self.change_view)
        self.tbl_combo = ttk.Combobox(bar, state='readonly', values=list(EU_LANGS), width=6)
        self.tbl_combo.grid(row=0, column=6, padx=4)
        self.tbl_combo.set(self.game_lang)
        self.tbl_combo.bind('<<ComboboxSelected>>', self.change_game_lang)
        self.ui_combo = ttk.Combobox(bar, state='readonly', values=['English', 'العربية'], width=8)
        self.ui_combo.set('العربية' if self.ui == 'ar' else 'English')
        self.ui_combo.grid(row=0, column=7, padx=4)
        self.ui_combo.bind('<<ComboboxSelected>>', self.change_ui)
        self.scope_combo = ttk.Combobox(bar, state='readonly', width=7,
                                        values=[self.t('scope_all'), self.t('scope_rdx'), self.t('scope_elf')])
        self.scope_combo.grid(row=0, column=8, padx=(7,4))
        self.scope_combo.current({'ALL':0,'RDX':1,'ELF':2}.get(self.text_scope_value,0))
        self.scope_combo.bind('<<ComboboxSelected>>', self.change_text_scope)
        self.search = tk.StringVar()
        ent = ttk.Entry(bar, textvariable=self.search)
        ent.grid(row=0, column=9, sticky='ew')
        ent.bind('<KeyRelease>', lambda _e: self.refresh_texts())
        ttk.Button(bar, text=self.t('about'), width=7, command=self.show_about).grid(row=0, column=10, padx=(6, 0))

        self.nb = ttk.Notebook(self)
        self.nb.grid(row=1, column=0, sticky='nsew', padx=8, pady=(2, 5))
        self._text_tab()
        self._font_tab()
        self._tbl_tab()
        self.nb.bind('<<NotebookTabChanged>>', self._notebook_changed)

        foot = ttk.Frame(self, padding=(8, 2, 8, 5))
        foot.grid(row=2, column=0, sticky='ew')
        foot.columnconfigure(0, weight=1)
        self.status = tk.StringVar(value=self.t('ready'))
        ttk.Label(foot, textvariable=self.status).grid(row=0, column=0, sticky='w')
        self.progress = ttk.Progressbar(foot, maximum=100, length=180)
        self.progress.grid(row=0, column=1, padx=8)
        self._refresh_all()

    def _text_tab(self):
        tab = ttk.Frame(self.nb)
        self.text_tab = tab
        self.nb.add(tab, text=self.t('texts'))
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        pw = ttk.Panedwindow(tab, orient='horizontal')
        pw.grid(row=0, column=0, sticky='nsew')

        left = ttk.Frame(pw)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)
        self.text_tree = ttk.Treeview(left, columns=('lang', 'room', 'id', 'text'), show='headings', selectmode='browse')
        for col, title, width, stretch in [
            ('lang', self.t('lang'), 54, False), ('room', self.t('room'), 125, False),
            ('id', 'ID', 52, False), ('text', self.t('texts'), 300, True),
        ]:
            self.text_tree.heading(col, text=title)
            self.text_tree.column(col, width=width, stretch=stretch)
        ys = ttk.Scrollbar(left, orient='vertical', command=self.text_tree.yview)
        self.text_tree.configure(yscrollcommand=ys.set)
        self.text_tree.grid(row=0, column=0, sticky='nsew')
        ys.grid(row=0, column=1, sticky='ns')
        self.text_tree.bind('<<TreeviewSelect>>', self.pick_text)
        pw.add(left, weight=4)

        right = ttk.Frame(pw, padding=(8, 0, 0, 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=2, minsize=150)
        right.rowconfigure(5, weight=3, minsize=220)
        ttk.Label(right, text=self.t('editor')).grid(row=0, column=0, sticky='w')
        tb = ttk.Frame(right)
        tb.grid(row=1, column=0, sticky='ew', pady=3)
        for i, tok in enumerate(TOKENS):
            ttk.Button(tb, text=tok, width=9, command=lambda x=tok: self.insert_token(x)).grid(row=i // 6, column=i % 6, padx=1, pady=1)
        editor_box = ttk.Frame(right)
        editor_box.grid(row=2, column=0, sticky='nsew')
        editor_box.columnconfigure(0, weight=1); editor_box.rowconfigure(0, weight=1)
        self.editor = tk.Text(editor_box, wrap='word', undo=True, bg=PANEL, fg=TEXT, insertbackground=TEXT, selectbackground=SEL,
                              relief='flat', highlightthickness=1, highlightbackground=BORDER, padx=8, pady=7)
        eys = ttk.Scrollbar(editor_box, orient='vertical', command=self.editor.yview)
        self.editor.configure(yscrollcommand=eys.set)
        self.editor.grid(row=0, column=0, sticky='nsew')
        eys.grid(row=0, column=1, sticky='ns')
        self.editor.bind('<<Modified>>', self.editor_changed)

        pb = ttk.Frame(right)
        pb.grid(row=3, column=0, sticky='ew', pady=(6, 3))
        self.rtl = tk.BooleanVar(value=False)
        # Kept only for compatibility with old project state. Text preview never
        # draws glyph-cell grids; the red box belongs to the Font tab only.
        self.show_bounds = tk.BooleanVar(value=False)
        self.show_space_area = tk.BooleanVar(value=False)
        ttk.Checkbutton(pb, text=self.t('rtl'), variable=self.rtl, command=self.draw_preview).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(pb, text=self.t('reversear'), command=self.reverse_arabic_editor).grid(row=0, column=1, padx=(8, 10))
        ttk.Button(pb, text='‹', width=3, command=lambda: self.step_preview_page(-1)).grid(row=0, column=2, padx=(6, 2))
        self.page_label = ttk.Label(pb, text='1/1')
        self.page_label.grid(row=0, column=3, padx=3)
        ttk.Button(pb, text='›', width=3, command=lambda: self.step_preview_page(1)).grid(row=0, column=4, padx=2)
        self.metrics = ttk.Label(pb, text='', foreground=MUTED)
        pb.columnconfigure(5, weight=1)

        ttk.Label(right, text=self.t('preview')).grid(row=4, column=0, sticky='w')
        preview_box = ttk.Frame(right)
        preview_box.grid(row=5, column=0, sticky='nsew')
        preview_box.columnconfigure(0, weight=1); preview_box.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(preview_box, bg=CANVAS, highlightthickness=0, height=230)
        pvy = ttk.Scrollbar(preview_box, orient='vertical', command=self.canvas.yview)
        pvx = ttk.Scrollbar(preview_box, orient='horizontal', command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=pvy.set, xscrollcommand=pvx.set)
        self.canvas.grid(row=0, column=0, sticky='nsew')
        pvy.grid(row=0, column=1, sticky='ns')
        pvx.grid(row=1, column=0, sticky='ew')
        self.canvas.bind('<Configure>', lambda _e: self.draw_preview(focus=False))
        pw.add(right, weight=6)

    def _font_tab(self):
        tab = ttk.Frame(self.nb, padding=6)
        self.font_tab = tab
        self.nb.add(tab, text=self.t('font'))
        tab.columnconfigure(0, weight=2)
        tab.columnconfigure(1, weight=3)
        tab.rowconfigure(1, weight=1)

        bar = ttk.Frame(tab)
        bar.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 5))
        bar.columnconfigure(11, weight=1)
        ttk.Button(bar, text=self.t('openelf'), command=self.open_elf).grid(row=0, column=0, padx=(0, 4))
        ttk.Button(bar, text=self.t('savefont'), style='Accent.TButton', command=self.save_font).grid(row=0, column=1, padx=4)
        self.font_mode = ttk.Combobox(bar, state='readonly', values=[self.t('origin'), self.t('pal')], width=16)
        self.font_mode.current(0)
        self.font_mode.grid(row=0, column=2, padx=5)
        self.font_mode.bind('<<ComboboxSelected>>', lambda _e: (self.refresh_font(), self.draw_preview()))
        ttk.Label(bar, text=self.t('space')).grid(row=0, column=3, padx=(8, 2))
        self.space_var = tk.IntVar(value=14)
        sp = ttk.Spinbox(bar, from_=1, to=64, textvariable=self.space_var, width=4, command=self.apply_space)
        sp.grid(row=0, column=4)
        sp.bind('<KeyRelease>', lambda _e: self.apply_space(silent=True))
        self.font_bank_combo = ttk.Combobox(bar, state='readonly', width=20)
        self.font_bank_combo.grid(row=0, column=5, padx=(10, 3))
        self.font_bank_combo.bind('<<ComboboxSelected>>', self.change_font_bank)
        self.font_page_combo = ttk.Combobox(bar, state='readonly', width=5)
        self.font_page_combo.grid(row=0, column=6, padx=3)
        self.font_page_combo.bind('<<ComboboxSelected>>', lambda _e: self.draw_font_preview())
        ttk.Button(bar, text=self.t('exportpng'), command=self.export_font_png).grid(row=0, column=7, padx=3)
        ttk.Button(bar, text=self.t('replacepng'), command=self.replace_font_png).grid(row=0, column=8, padx=3)
        ttk.Button(bar, text=self.t('extracttm2'), command=self.export_font_tm2).grid(row=0, column=9, padx=3)
        ttk.Button(bar, text=self.t('replacetm2'), command=self.replace_font_tm2).grid(row=0, column=10, padx=3)
        self.elf_label = ttk.Label(bar, text='')
        self.elf_label.grid(row=0, column=11, sticky='e')

        fl = ttk.Frame(tab)
        fl.grid(row=1, column=0, sticky='nsew', padx=(0, 5))
        fl.columnconfigure(0, weight=1)
        fl.rowconfigure(0, weight=1)
        self.font_tree = ttk.Treeview(fl, columns=('code', 'char', 'adv', 'page', 'u', 'v'), show='headings', selectmode='browse')
        for col, title, width, stretch in [
            ('code', self.t('code'), 74, False), ('char', self.t('char'), 80, True), ('adv', self.t('advance'), 65, False),
            ('page', self.t('page'), 46, False), ('u', 'U', 42, False), ('v', 'V', 42, False),
        ]:
            self.font_tree.heading(col, text=title)
            self.font_tree.column(col, width=width, stretch=stretch)
        fys = ttk.Scrollbar(fl, orient='vertical', command=self.font_tree.yview)
        self.font_tree.configure(yscrollcommand=fys.set)
        self.font_tree.grid(row=0, column=0, sticky='nsew')
        fys.grid(row=0, column=1, sticky='ns')
        self.font_tree.bind('<<TreeviewSelect>>', self.pick_glyph)
        edit = ttk.Frame(fl)
        edit.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(5, 0))
        self.adv_var = tk.IntVar(value=14)
        self.h_var = tk.IntVar(value=28)
        ttk.Label(edit, text=self.t('advance')).grid(row=0, column=0)
        adv_box = ttk.Spinbox(edit, from_=0, to=255, textvariable=self.adv_var, width=5, command=self.live_apply_glyph)
        adv_box.grid(row=0, column=1, padx=3)
        adv_box.bind('<KeyRelease>', lambda _e: self.live_apply_glyph())
        ttk.Label(edit, text=self.t('previewbox')).grid(row=0, column=2, padx=(10, 0))
        h_box = ttk.Spinbox(edit, from_=1, to=64, textvariable=self.h_var, width=5, command=lambda: self.live_apply_glyph(preview_only=True))
        h_box.grid(row=0, column=3, padx=3)
        h_box.bind('<KeyRelease>', lambda _e: self.live_apply_glyph(preview_only=True))
        ttk.Button(edit, text=self.t('apply'), command=self.apply_glyph).grid(row=0, column=4, padx=8)
        self.actual_box_label = ttk.Label(edit, text='14×28', foreground=ACC)
        self.actual_box_label.grid(row=0, column=5, padx=(4, 0))

        fr = ttk.Frame(tab)
        fr.grid(row=1, column=1, sticky='nsew')
        fr.columnconfigure(0, weight=1)
        fr.rowconfigure(0, weight=1)
        self.font_canvas = tk.Canvas(fr, bg=CANVAS, highlightthickness=0)
        self.font_canvas.grid(row=0, column=0, sticky='nsew')
        self.font_canvas.bind('<Configure>', lambda _e: self.draw_font_preview())
        self.font_canvas.bind('<ButtonPress-1>', self.font_canvas_press)
        self.font_canvas.bind('<B1-Motion>', self.font_canvas_drag)
        self.font_canvas.bind('<ButtonRelease-1>', self.font_canvas_release)


    def _tbl_tab(self):
        tab = ttk.Frame(self.nb, padding=6)
        self.nb.add(tab, text=self.t('tbl'))
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        b = ttk.Frame(tab)
        b.grid(row=0, column=0, sticky='ew', pady=(0, 5))
        ttk.Button(b, text=self.t('loadtbl'), command=self.open_tbl).grid(row=0, column=0, padx=(0, 5))
        ttk.Button(b, text=self.t('customartbl'), command=self.use_custom_ar_tbl).grid(row=0, column=1, padx=5)
        ttk.Button(b, text=self.t('savetbl'), style='Accent.TButton', command=self.save_tbl).grid(row=0, column=2, padx=5)
        self.tbl_path_label = ttk.Label(b, text='')
        self.tbl_path_label.grid(row=0, column=3, padx=10)
        body = ttk.Frame(tab)
        body.grid(row=1, column=0, sticky='nsew')
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.map_tree = ttk.Treeview(body, columns=('code', 'value'), show='headings', selectmode='browse')
        self.map_tree.heading('code', text=self.t('code'))
        self.map_tree.heading('value', text=self.t('value'))
        self.map_tree.column('code', width=100, stretch=False)
        self.map_tree.column('value', width=350)
        self.map_tree.grid(row=0, column=0, sticky='nsew')
        self.map_tree.bind('<<TreeviewSelect>>', self.pick_map)
        ys = ttk.Scrollbar(body, orient='vertical', command=self.map_tree.yview)
        self.map_tree.configure(yscrollcommand=ys.set)
        ys.grid(row=0, column=1, sticky='ns')
        ed = ttk.Frame(body)
        ed.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(5, 0))
        self.map_code = tk.StringVar()
        self.map_value = tk.StringVar()
        ttk.Entry(ed, textvariable=self.map_code, width=10).grid(row=0, column=0, padx=(0, 4))
        ttk.Entry(ed, textvariable=self.map_value).grid(row=0, column=1, sticky='ew', padx=4)
        ed.columnconfigure(1, weight=1)
        ttk.Button(ed, text=self.t('apply'), command=self.apply_map).grid(row=0, column=2, padx=4)

    def _walk_widgets(self):
        stack = [self]
        seen = set()
        out = []
        while stack:
            w = stack.pop()
            k = str(w)
            if k in seen:
                continue
            seen.add(k); out.append(w)
            try:
                stack.extend(w.winfo_children())
            except Exception:
                pass
        return out

    def _drop_all(self):
        # Register every visible child so Explorer drops work anywhere on the
        # interface, including Treeview/Text/Canvas areas. tkinterdnd2 is the
        # primary route; native WM_DROPFILES is a Windows fallback so drag/drop
        # still works when the optional Python package is missing/broken.
        widgets = self._walk_widgets()
        if DND:
            for w in widgets:
                try:
                    w.drop_target_register(DND_FILES)
                    w.dnd_bind('<<Drop>>', self.on_drop)
                except Exception:
                    pass
        self._install_win_dropfiles(widgets)

    def _install_win_dropfiles(self, widgets=None):
        if sys.platform != 'win32':
            return
        try:
            import ctypes
            from ctypes import wintypes
            shell32 = ctypes.windll.shell32
            user32 = ctypes.windll.user32
            WM_DROPFILES = 0x0233
            GWL_WNDPROC = -4
            PTR = ctypes.c_ssize_t
            WNDPROC = ctypes.WINFUNCTYPE(PTR, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
            set_wnd = user32.SetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else user32.SetWindowLongW
            set_wnd.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
            set_wnd.restype = ctypes.c_void_p
            call_wnd = user32.CallWindowProcW
            call_wnd.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            call_wnd.restype = PTR
            shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
            shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
            shell32.DragQueryFileW.restype = wintypes.UINT
            shell32.DragFinish.argtypes = [wintypes.HANDLE]
            self._win_drop_api = (set_wnd, call_wnd, shell32, GWL_WNDPROC)

            if self._win_drop_proc is None:
                @WNDPROC
                def wndproc(hwnd, msg, wparam, lparam):
                    if msg == WM_DROPFILES:
                        paths = []
                        try:
                            count = shell32.DragQueryFileW(wparam, 0xFFFFFFFF, None, 0)
                            for i in range(count):
                                n = shell32.DragQueryFileW(wparam, i, None, 0)
                                buf = ctypes.create_unicode_buffer(n + 1)
                                shell32.DragQueryFileW(wparam, i, buf, n + 1)
                                if buf.value:
                                    paths.append(buf.value)
                        finally:
                            shell32.DragFinish(wparam)
                        if paths:
                            try:
                                self.after(0, self._accept_drop_paths, paths)
                            except Exception:
                                pass
                        return 0
                    old = self._win_drop_old.get(int(hwnd))
                    if old:
                        return call_wnd(old, hwnd, msg, wparam, lparam)
                    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
                self._win_drop_proc = wndproc

            proc_ptr = ctypes.cast(self._win_drop_proc, ctypes.c_void_p)
            for w in (widgets or self._walk_widgets()):
                try:
                    hwnd = int(w.winfo_id())
                    if not hwnd:
                        continue
                    shell32.DragAcceptFiles(hwnd, True)
                    # Allow Explorer drops even if the tool was started elevated.
                    try:
                        filt = getattr(user32, 'ChangeWindowMessageFilterEx', None)
                        if filt:
                            filt(hwnd, WM_DROPFILES, 1, None)
                            filt(hwnd, 0x0049, 1, None)  # WM_COPYGLOBALDATA
                    except Exception:
                        pass
                    if hwnd in self._win_drop_old:
                        continue
                    ctypes.set_last_error(0)
                    old = set_wnd(hwnd, GWL_WNDPROC, proc_ptr)
                    if old:
                        self._win_drop_old[hwnd] = old
                except Exception:
                    continue
        except Exception:
            pass

    def _restore_win_dropfiles(self):
        if not self._win_drop_api or not self._win_drop_old:
            return
        try:
            set_wnd, _call_wnd, shell32, GWL_WNDPROC = self._win_drop_api
            for hwnd, old in list(self._win_drop_old.items()):
                try:
                    shell32.DragAcceptFiles(hwnd, False)
                    set_wnd(hwnd, GWL_WNDPROC, old)
                except Exception:
                    pass
        finally:
            self._win_drop_old.clear()

    def _accept_drop_paths(self, paths):
        if not paths:
            return
        raw = str(paths[0])
        now = time.monotonic()
        # tkinterdnd2 + WM_DROPFILES can both report the same Explorer drop.
        if raw == self._last_drop_path and now - self._last_drop_time < 0.75:
            return
        self._last_drop_path = raw; self._last_drop_time = now
        self.open_path(Path(raw))

    def on_drop(self, event):
        try:
            paths = list(self.tk.splitlist(event.data))
        except Exception:
            paths = []
        self._accept_drop_paths(paths)
        return 'break'

    def change_ui(self, _e=None):
        self.ui = 'ar' if self.ui_combo.get() == 'العربية' else 'en'
        self._restore_win_dropfiles()
        self._build()
        self._drop_all()

    def set_status(self, text): self.status.set(text)

    def open_file(self):
        p = filedialog.askopenfilename(title=TITLE, filetypes=[('RECVX', '*.iso *.ald *.rdx *.afs *.zip *.*'), ('All', '*.*')])
        if p: self.open_path(Path(p))

    def open_folder(self):
        p = filedialog.askdirectory(title=TITLE)
        if p: self.open_path(Path(p))

    def open_path(self, path: Path):
        path = Path(path)
        if not path.exists():
            return
        # A custom TBL can be dropped anywhere on the window without
        # discarding the already-scanned game/RDX language cache.
        if path.is_file() and path.suffix.lower() == '.tbl':
            try: self.set_tbl(path)
            except Exception as ex: self.err(ex)
            return
        try:
            ident = str(path.resolve()).casefold()
        except Exception:
            ident = str(path).casefold()
        if ident != self._input_identity:
            self._input_identity = ident
            self._lang_cache.clear()
        # Invalidate any previous archive extraction immediately. A new drop
        # must never be ignored just because an older scan is still finishing.
        self._archive_serial += 1
        serial = self._archive_serial
        # Cancel stale folder/RDX workers as soon as a new drop arrives.
        self._root_generation += 1
        if path.is_dir():
            self.iso_source_path = None
            self.begin_load_root(path)
            return
        suffix = path.suffix.lower()
        if suffix == '.iso' or looks_like_iso(path):
            self.iso_source_path = path
            self.scan_archive(path, 'iso', serial)
            return
        if suffix == '.zip':
            self.iso_source_path = None
            self.scan_archive(path, 'zip', serial)
            return
        self.iso_source_path = None
        self.begin_load_root(path.parent, select_path=path)

    def scan_archive(self, path: Path, kind: str, serial: int | None = None):
        if serial is None:
            self._archive_serial += 1
            serial = self._archive_serial
        root = Path(tempfile.mkdtemp(prefix='recvx_iso_'))
        self.temp.append(root)
        self.set_status(self.t('iso_index') if kind == 'iso' else self.t('working'))
        self.progress['value'] = 1
        scan_lang = self.game_lang

        def cancelled():
            return serial != self._archive_serial or scan_lang != self.game_lang

        def work():
            try:
                if kind == 'iso':
                    st = path.stat()
                    cache_key = (str(path.resolve()).casefold(), int(st.st_size), int(st.st_mtime_ns))
                    with Iso9660Reader(path) as iso:
                        if self._iso_entries_cache_key == cache_key and self._iso_entries_cache is not None:
                            entries = self._iso_entries_cache
                            self._post_ui(self.set_status, f'{self.t("iso_index")} • cached • {scan_lang}')
                            self._post_ui(self.progress.configure, value=7)
                        else:
                            def visit(count, name):
                                if cancelled(): return
                                if count <= 8 or count % 128 == 0:
                                    self._post_ui(self.set_status, f'{self.t("iso_index")} • {count} • {Path(name).name}')
                            entries = [e for e in iso.entries(cancel=cancelled, visit=visit) if not e.is_dir]
                            if cancelled(): raise InterruptedError('ISO scan cancelled')
                            self._iso_entries_cache_key = cache_key
                            self._iso_entries_cache = entries
                        # Critical speed rule: a PAL disc has four 205-room RDX
                        # banks. Scan only the selected language instead of all 820.
                        rdx_entries = [e for e in entries if is_rdx_afs_name(e.path) and path_matches_game_lang(e.path, scan_lang)]
                        quick = [e for e in entries
                                 if is_recvx_candidate(e.path) and not is_rdx_afs_name(e.path)
                                 and path_matches_game_lang(e.path, scan_lang)]
                        quick.sort(key=lambda e: iso_quick_priority(e.path))
                        total_bytes = sum(max(0, e.size) for e in quick) or 1
                        completed_bytes = 0
                        for i, ent in enumerate(quick, 1):
                            if cancelled(): raise InterruptedError('ISO scan cancelled')
                            target = root.joinpath(*[clean_iso_component(x) for x in ent.path.split('/')])
                            base_done = completed_bytes
                            last_value = [-1]
                            def chunk_progress(done, size, ent=ent, i=i, base_done=base_done):
                                if cancelled(): return
                                value = 8 + int(14 * (base_done + done) / total_bytes)
                                if value != last_value[0]:
                                    last_value[0] = value
                                    self._post_ui(self.progress.configure, value=max(8, min(22, value)))
                                    # One status update per percentage, not per chunk.
                                    self._post_ui(self.set_status, f'{self.t("iso_extract")} {i}/{len(quick)} • {Path(ent.path).name}')
                            iso.extract_entry(ent, target, progress=chunk_progress)
                            completed_bytes += ent.size
                    if cancelled(): raise InterruptedError('ISO scan cancelled')
                    self._post_ui(self.progress.configure, value=22)
                    self._post_ui(self.begin_load_root, root, None, rdx_entries, quick)
                else:
                    def zprog(a, b, name):
                        if cancelled(): return
                        value = 2 + int(a * 20 / max(1, b))
                        self._post_ui(self.progress.configure, value=max(2, min(22, value)))
                        if a == b or a % 8 == 0:
                            self._post_ui(self.set_status, f'{self.t("working")} • {a}/{b} • {Path(name).name}')
                    extract_zip_candidates(path, root, zprog, cancel=cancelled)
                    if cancelled(): raise InterruptedError('Archive scan cancelled')
                    self._post_ui(self.begin_load_root, root)
            except InterruptedError:
                shutil.rmtree(root, ignore_errors=True)
            except Exception as ex:
                if not cancelled(): self._post_ui(self.err, ex)

        threading.Thread(target=work, daemon=True).start()

    def _reset_live_root(self, root: Path):
        self.sources = []
        self.source = None
        self.msg = None
        self.view_options = []
        self.view_mode = ('none', None)
        self.tree_map = {}
        self.root_folder = Path(root)
        self._rdx_items = []
        self._font_started_generation = -1
        self._text_row_serial += 1
        self._text_row_queue.clear()
        self._source_index_by_id = {}
        self._live_message_count = 0
        self._live_room_count = 0
        self.font_banks = []
        self.font_bank = None
        self.font = None
        self.elf_path = None
        try:
            self.source_combo.set('')
            self.source_combo['values'] = []
            self.text_tree.delete(*self.text_tree.get_children())
            self.editor.delete('1.0', 'end')
        except Exception:
            pass

    def _sync_source_combo_live(self):
        old = self.view_mode
        langs = []
        for src in self.sources:
            if src.lang not in langs:
                langs.append(src.lang)
        opts = []
        labels = []
        for lang in langs:
            opts.append(('all', lang))
            labels.append(f'{lang} | {self.t("alltexts")}')
            for idx, src in enumerate(self.sources):
                if src.lang == lang:
                    opts.append(('source', idx))
                    labels.append(src.label)
        self.view_options = opts
        self.source_combo['values'] = labels
        if not opts:
            self.source_combo.set('')
            return
        if old in opts:
            pos = opts.index(old)
            self.source_combo.current(pos)
            return
        self.source_combo.current(0)
        self.set_view(opts[0])

    def _append_live_text_rows(self, batch):
        if not batch or self.view_mode[0] != 'all':
            return
        lang = self.view_mode[1]
        for src in batch:
            if src.lang != lang:
                continue
            for m in src.messages:
                self._text_row_queue.append((src, m))
        serial = self._text_row_serial
        self.after(1, self._drain_text_rows, serial)

    def _live_sources_update(self, generation, batch, scanned=0, total=0, force_refresh=False):
        if generation != self._root_generation:
            return
        was_empty = not self.sources
        old_view = self.view_mode
        if batch:
            base=len(self.sources)
            self.sources.extend(batch)
            for j,src in enumerate(batch):
                self._source_index_by_id[id(src)] = base+j
                self._live_message_count += len(src.messages)
                if src.kind == 'RDX': self._live_room_count += 1

        # Rebuilding a Combobox containing 200+ rooms for every six rooms was
        # quadratic UI work. Build it on first paint and at the final batch only.
        if was_empty and batch:
            self._sync_source_combo_live()
        elif force_refresh:
            self._sync_source_combo_live()

        if batch and not was_empty and old_view == self.view_mode:
            self._append_live_text_rows(batch)
        elif force_refresh and was_empty and self.view_mode[0] != 'none':
            self.refresh_texts()

        status = f'{self.t("texts_progress")} {self._live_message_count} • {self.t("rooms_progress")} {self._live_room_count}'
        if total:
            status += f' • {self.t("rdx_progress")} {min(scanned,total)}/{total}'
            value = 24 + int(46 * min(scanned, total) / max(1, total))
            self.progress['value'] = max(float(self.progress['value']), min(70, value))
        else:
            self.progress['value'] = max(float(self.progress['value']), 24)
        self.set_status(status)

    @staticmethod
    def _fast_afs_count(path: Path) -> int:
        try:
            with Path(path).open('rb') as f:
                head = f.read(8)
            if len(head) == 8 and head[:4] == b'AFS\0':
                n = int.from_bytes(head[4:8], 'little')
                return n if 0 <= n <= 100000 else 0
        except Exception:
            pass
        return 0

    def begin_load_root(self, root: Path, select_path: Path | None = None, iso_rdx_entries=None, iso_quick_entries=None):
        self._root_generation += 1
        generation = self._root_generation
        root = Path(root)
        self._reset_live_root(root)
        self.set_status(self.t('working'))
        self.progress['value'] = max(float(self.progress['value']), 23 if iso_rdx_entries is not None else 5)

        # Map each extracted quick ISO file back to its original ISO entry path.
        # Saving must target the ISO, not only this temporary extraction tree.
        iso_local_map = {}
        if iso_quick_entries is not None and self.iso_source_path is not None:
            for _ent in iso_quick_entries:
                _local = root.joinpath(*[clean_iso_component(x) for x in _ent.path.split('/')])
                try: iso_local_map[str(_local.resolve()).casefold()] = _ent.path
                except Exception: iso_local_map[str(_local).casefold()] = _ent.path

        # Font-page TM2 work is lazy; text scan gets full disk/CPU priority.
        custom_tbls = dict(self.custom_tbl_paths)

        def worker():
            workdir = Path(tempfile.mkdtemp(prefix='recvx_rdx_'))
            try:
                # One recursive walk only, and retain only files that can affect
                # RECVX text/font loading. Large movie/audio trees are ignored.
                def root_candidate(p):
                    n=p.name.upper(); ext=p.suffix.lower()
                    if ext == '.tbl' or ext == '.rdx': return True
                    if ext == '.ald' and n.startswith('SYSMES'): return True
                    if ext == '.afs' and (n.startswith('SYSTEM') or 'RDX' in n): return True
                    return bool(re.match(r'(?i)^(SLES|SLUS|SLPM|SCES|SCUS)[_.-]?\d', p.name))
                all_files = []
                for dirpath, _dirnames, filenames in os.walk(root):
                    if generation != self._root_generation:
                        shutil.rmtree(workdir, ignore_errors=True); return
                    dp=Path(dirpath)
                    for name in filenames:
                        p=dp/name
                        if root_candidate(p): all_files.append(p)
                by_name = {p.name.lower():p for p in all_files if p.suffix.lower()=='.tbl'}
                tbls = {}
                for _lang,_name in TBL_NAMES.items():
                    _candidates=[_name]
                    if _lang=='ENG': _candidates.append('US.tbl')
                    if _lang=='SPA': _candidates.append('ES.tbl')
                    for _n in _candidates:
                        if _n.lower() in by_name:
                            tbls[_lang]=by_name[_n.lower()]; break
                    if _lang not in tbls:
                        _bundled=here()/_name
                        if _bundled.exists(): tbls[_lang]=_bundled
                files = sorted((p for p in all_files if path_matches_game_lang(p, self.game_lang)), key=lambda p: str(p).casefold())
                sources = []
                rdx_items = []
                pending = []
                codec_cache = {}

                def codec_for(lang):
                    if lang in codec_cache:
                        return codec_cache[lang]
                    tp = custom_tbls.get(lang) or tbls.get(lang) or (here() / TBL_NAMES.get(lang, 'ENG.tbl'))
                    c2t, t2c = parse_tbl(tp)
                    codec_cache[lang] = (tp, Codec(c2t, t2c))
                    return codec_cache[lang]

                def _iso_path_for_local(p):
                    if p is None or self.iso_source_path is None:
                        return ''
                    try: key = str(Path(p).resolve()).casefold()
                    except Exception: key = str(Path(p)).casefold()
                    return iso_local_map.get(key, '')

                def make_source(kind, p, doc, afs=None, idx=None, comp=False, afs_name='', packed_path=None, lang_hint=None, raw_loader=None, iso_entry_path='', iso_afs_index=None):
                    lang = lang_hint or detect_game_lang(Path(afs) if afs is not None else Path(packed_path) if packed_path is not None else Path(p))
                    tp, codec = codec_for(lang)
                    room = Path(afs_name).stem if afs_name else Path(p).stem
                    label = f'{lang} | {kind} | {room}'
                    mapped = iso_entry_path or _iso_path_for_local(afs) or _iso_path_for_local(packed_path) or _iso_path_for_local(p)
                    mapped_idx = iso_afs_index
                    if mapped_idx is None and afs is not None and mapped:
                        mapped_idx = idx
                    return Source(label, lang, Path(p), doc, tp, codec, kind, False,
                                  Path(afs) if afs is not None else None, idx, comp,
                                  afs_name or '', Path(packed_path) if packed_path is not None else None, raw_loader,
                                  Path(self.iso_source_path) if (self.iso_source_path is not None and mapped) else None, mapped, mapped_idx)

                def load_stream_afs_member(path, index):
                    arc = StreamAfsArchive(Path(path))
                    try:
                        return arc.entry_bytes(index)
                    finally:
                        try: arc.close()
                        except Exception: pass

                def load_iso_afs_member(iso_path, ent_or_path, index):
                    ent = find_iso_entry(Path(iso_path), ent_or_path if isinstance(ent_or_path, str) else ent_or_path.path)
                    arc = IsoAfsArchive(Path(iso_path), ent)
                    try:
                        return arc.entry_bytes(index)
                    finally:
                        try: arc.close()
                        except Exception: pass

                def post_pending(scanned=0, total=0, force=False):
                    nonlocal pending
                    if pending or force:
                        batch = pending
                        pending = []
                        self._post_ui(self._live_sources_update, generation, batch, scanned, total, force)

                # ELF contains additional PAL text (save/load, retry, demo/pause
                # prompts, etc.). Scan it with only the currently selected TBL.
                # This is very small work compared with hundreds of RDX rooms, so
                # surface it immediately before the RDX bank scan.
                elf = next((p for p in files if re.match(r'(?i)^(SLES|SLUS|SLPM|SCES|SCUS)[_.-]?\d', p.name)), None)
                if elf is not None:
                    try:
                        tp, codec = codec_for(self.game_lang)
                        edoc = ElfTextDocument(elf, self.game_lang, codec)
                        if edoc.messages:
                            esrc = make_source('ELF', elf, edoc, lang_hint=self.game_lang)
                            sources.append(esrc); pending.append(esrc); post_pending(0, 0, True)
                    except Exception:
                        pass

                # -------- Fast first paint: SYSMES before any RDX decompression.
                loose_sysmes = [p for p in files if re.fullmatch(r'sysmes\d*\.ald', p.name, re.I) and detect_game_lang(p) == self.game_lang]
                loose_sysmes_langs = {detect_game_lang(p) for p in loose_sysmes}
                for pth in loose_sysmes:
                    if generation != self._root_generation:
                        shutil.rmtree(workdir, ignore_errors=True); return
                    try:
                        doc = AldDocument(pth)
                        if doc.messages:
                            src = make_source('SYSMES', pth, doc)
                            sources.append(src); pending.append(src)
                            post_pending(0, 0, True)
                    except Exception:
                        pass

                # SYSTEM*.AFS entry 0 fallback when a loose SYSMES is absent.
                for pth in [p for p in files if p.suffix.lower() == '.afs' and re.fullmatch(r'system\d*\.afs', p.name, re.I) and detect_game_lang(p) == self.game_lang]:
                    lang = detect_game_lang(pth)
                    if lang in loose_sysmes_langs:
                        continue
                    try:
                        afs = AfsArchive(pth)
                        raw = afs.entry_bytes(0)
                        ep = workdir / f'{pth.stem}_SYSMES.ald'
                        ep.write_bytes(raw)
                        doc = AldDocument(ep)
                        if doc.messages:
                            src = make_source('SYSMES', ep, doc, pth, 0, False, afs.entry_name(0) or 'SYSMES', lang_hint=lang)
                            sources.append(src); pending.append(src)
                            post_pending(0, 0, True)
                    except Exception:
                        pass

                # Loose RDX files are usually few; process them before large AFS banks.
                loose_rdx = [p for p in files if p.suffix.lower() == '.rdx' and path_matches_game_lang(p, self.game_lang)]
                for pth in loose_rdx:
                    if generation != self._root_generation:
                        shutil.rmtree(workdir, ignore_errors=True); return
                    try:
                        raw = pth.read_bytes(); comp = False; packed = None; ep = pth; loader = None
                        if not RdxDocument.probe_bytes(raw):
                            dec = probe_compressed_rdx_text(raw)
                            if dec is None:
                                continue
                            comp = True; packed = pth; loader = (lambda p=pth: Path(p).read_bytes())
                            ep = workdir / f'{pth.stem}_expanded.rdx'; raw = dec
                        doc = RdxDocument(ep, initial_data=raw)
                        if comp: doc._text_only_prefix = True
                        rdx_items.append((ep, None, None, comp, pth.name, packed))
                        if doc.messages:
                            src = make_source('RDX', ep, doc, None, None, comp, pth.name, packed, raw_loader=loader)
                            sources.append(src); pending.append(src)
                            post_pending(0, 0, True)
                    except Exception:
                        pass

                # Build the RDX AFS work list without extracting any whole ISO AFS.
                folder_rdx_afs = [p for p in files if p.suffix.lower() == '.afs' and 'RDX' in p.name.upper() and detect_game_lang(p) == self.game_lang]
                total_rdx = sum(self._fast_afs_count(p) for p in folder_rdx_afs)
                iso_jobs = []
                if iso_rdx_entries is not None and self.iso_source_path and self.iso_source_path.exists():
                    for ent in iso_rdx_entries:
                        if not path_matches_game_lang(ent.path, self.game_lang):
                            continue
                        try:
                            afs = IsoAfsArchive(self.iso_source_path, ent)
                            iso_jobs.append((ent, afs))
                            total_rdx += afs.count
                        except Exception:
                            pass
                scanned = 0

                def consume_member(raw, name, container_label, idx, compressed_hint=False, outer_path=None, iso_readonly=False, lang_hint=None, raw_loader=None, iso_entry_path='', iso_afs_index=None):
                    nonlocal scanned
                    compressed = False; text_only = False
                    if not RdxDocument.probe_bytes(raw):
                        dec = probe_compressed_rdx_text(raw)
                        if dec is None:
                            return
                        raw = dec; compressed = True; text_only = True
                    safe_stem = re.sub(r'[^A-Za-z0-9_.-]+', '_', Path(name or f'RDX_{idx:04d}').stem)
                    unique = re.sub(r'[^A-Za-z0-9_.-]+', '_', Path(container_label).stem)
                    ep = workdir / f'{unique}_{idx:04d}_{safe_stem}.rdx'
                    try:
                        doc = RdxDocument(ep, initial_data=raw)
                        if text_only: doc._text_only_prefix = True
                    except Exception:
                        return
                    if not doc.messages:
                        return
                    source_outer = None if iso_readonly else outer_path
                    src = make_source('RDX', ep, doc, source_outer, idx if source_outer is not None else None,
                                      compressed, name, None, lang_hint=lang_hint, raw_loader=raw_loader,
                                      iso_entry_path=iso_entry_path, iso_afs_index=iso_afs_index)
                    sources.append(src); pending.append(src)

                # Folder/ZIP RDX AFS. One AFS at a time to cap memory usage.
                for afs_path in folder_rdx_afs:
                    if generation != self._root_generation:
                        shutil.rmtree(workdir, ignore_errors=True); return
                    try:
                        afs = StreamAfsArchive(afs_path)
                    except Exception:
                        continue
                    lang = detect_game_lang(afs_path)
                    for idx in range(afs.count):
                        if generation != self._root_generation:
                            shutil.rmtree(workdir, ignore_errors=True); return
                        name = afs.entry_name(idx) or f'RDX_{idx:04d}.rdx'
                        try:
                            consume_member(afs.entry_bytes(idx), name, afs_path.name, idx,
                                           outer_path=afs_path, lang_hint=lang,
                                           raw_loader=(lambda p=afs_path, i=idx: load_stream_afs_member(p, i)))
                        except Exception:
                            pass
                        scanned += 1
                        if pending and (len(pending) >= 24 or scanned % 32 == 0):
                            post_pending(scanned, total_rdx)
                        elif scanned % 32 == 0:
                            self._post_ui(self._live_sources_update, generation, [], scanned, total_rdx, False)
                    try:
                        afs.close()
                    except Exception:
                        pass

                # ISO RDX AFS: direct member reads, no giant temporary AFS copies.
                for ent, afs in iso_jobs:
                    if generation != self._root_generation:
                        shutil.rmtree(workdir, ignore_errors=True); return
                    lang = detect_game_lang(Path(ent.path))
                    for idx in range(afs.count):
                        if generation != self._root_generation:
                            shutil.rmtree(workdir, ignore_errors=True); return
                        name = afs.entry_name(idx) or f'RDX_{idx:04d}.rdx'
                        try:
                            consume_member(afs.entry_bytes(idx), name, ent.path, idx,
                                           iso_readonly=True, lang_hint=lang,
                                           raw_loader=(lambda ip=self.iso_source_path, ep=ent.path, i=idx: load_iso_afs_member(ip, ep, i)),
                                           iso_entry_path=ent.path, iso_afs_index=idx)
                        except Exception:
                            pass
                        scanned += 1
                        if pending and (len(pending) >= 24 or scanned % 32 == 0):
                            post_pending(scanned, total_rdx)
                        elif scanned % 32 == 0:
                            self._post_ui(self._live_sources_update, generation, [], scanned, total_rdx, False)
                    try:
                        afs.close()
                    except Exception:
                        pass

                post_pending(scanned, total_rdx, True)
                self._post_ui(self.finish_load_root, generation, root, workdir, sources, elf, select_path, rdx_items)
            except Exception as ex:
                shutil.rmtree(workdir, ignore_errors=True)
                self._post_ui(self.err, ex)

        self._scan_thread = threading.Thread(target=worker, daemon=True)
        self._scan_thread.start()

    def finish_load_root(self, generation, root, workdir, sources, elf, select_path, rdx_items=None):
        if generation != self._root_generation:
            shutil.rmtree(workdir, ignore_errors=True)
            return
        self.temp.append(workdir)
        # Queue ordering guarantees live batches arrived before this final event.
        # Use the worker's list only as a safety net if a UI rebuild happened mid-scan.
        if len(self.sources) != len(sources):
            self.sources = list(sources)
            self._sync_source_combo_live()
            self.refresh_texts()
        self.root_folder = root
        self._rdx_items = list(rdx_items or [])
        self._lang_cache[self.game_lang] = {
            'root': Path(root), 'sources': list(self.sources), 'elf': Path(elf) if elf else None,
            'rdx_items': list(self._rdx_items), 'workdir': Path(workdir)
        }
        if select_path and self.sources:
            try:
                target = Path(select_path).resolve()
                for i, opt in enumerate(self.view_options):
                    if opt[0] != 'source':
                        continue
                    src = self.sources[opt[1]]
                    try:
                        if src.path.resolve() == target or (src.afs_path and src.afs_path.resolve() == target) or (src.packed_path and src.packed_path.resolve() == target):
                            self.source_combo.current(i); self.set_view(opt); break
                    except Exception:
                        pass
            except Exception:
                pass
        if elf:
            try:
                self.load_elf(elf)
                if self.game_lang in self._lang_cache:
                    self._lang_cache[self.game_lang]['font'] = self.font
                    self._lang_cache[self.game_lang]['elf_path'] = self.elf_path
            except Exception:
                pass
        self.progress['value'] = 100
        messages = self._live_message_count if hasattr(self,'_live_message_count') else sum(len(src.messages) for src in self.sources)
        rooms = self._live_room_count if hasattr(self,'_live_room_count') else sum(1 for src in self.sources if src.kind == 'RDX')
        self.set_status(f'{self.t("ready")} • {self.game_lang} • {self.t("texts_progress")} {messages} • {self.t("rooms_progress")} {rooms}')
        if not self.sources:
            self.editor.delete('1.0', 'end')
            self.editor.insert('1.0', self.t('no_text'))

    def _rebuild_view_options(self, select_path=None):
        langs = []
        for s in self.sources:
            if s.lang not in langs: langs.append(s.lang)
        self.view_options = []
        labels = []
        for lang in langs:
            self.view_options.append(('all', lang))
            labels.append(f'{lang} | {self.t("alltexts")}')
            for idx, s in enumerate(self.sources):
                if s.lang == lang:
                    self.view_options.append(('source', idx))
                    labels.append(s.label)
        self.source_combo['values'] = labels
        choice = 0
        if select_path:
            try: target = Path(select_path).resolve()
            except Exception: target = None
            if target:
                for i, opt in enumerate(self.view_options):
                    if opt[0] != 'source': continue
                    s = self.sources[opt[1]]
                    try:
                        if s.path.resolve() == target or (s.afs_path and s.afs_path.resolve() == target) or (s.packed_path and s.packed_path.resolve() == target):
                            choice = i; break
                    except Exception: pass
        if self.view_options:
            self.source_combo.current(choice)
            self.set_view(self.view_options[choice])
        else:
            self.view_mode = ('none', None)
            self.set_active_source(None)

    def _scope_accepts(self, src: Source) -> bool:
        mode = self.text_scope_value
        if mode == 'ELF':
            return src.kind == 'ELF'
        if mode == 'RDX':
            # The RDX view intentionally includes SYSMES because both are the
            # non-ELF game-message sources users expect when excluding ELF.
            return src.kind != 'ELF'
        return True

    def current_view_sources(self) -> list[Source]:
        kind, payload = self.view_mode
        if kind == 'all':
            return [s for s in self.sources if s.lang == payload and self._scope_accepts(s)]
        if kind == 'source' and isinstance(payload, int) and 0 <= payload < len(self.sources):
            src = self.sources[payload]
            return [src] if self._scope_accepts(src) else []
        return []

    def change_text_scope(self, _e=None):
        if not self.commit(silent=True):
            return
        idx = self.scope_combo.current() if hasattr(self, 'scope_combo') else 0
        self.text_scope_value = ('ALL','RDX','ELF')[idx if 0 <= idx <= 2 else 0]

        # Scope is a global filter, not a source selector.  Always return to the
        # language-wide view when the user changes Show; otherwise switching
        # ELF -> All while a single ELF source is selected still shows only ELF.
        lang = self.source.lang if self.source and self.source.lang in EU_LANGS else self.game_lang
        target = ('all', lang)
        self.view_mode = target
        for i, opt in enumerate(self.view_options):
            if opt == target:
                try: self.source_combo.current(i)
                except Exception: pass
                break
        vs = self.current_view_sources()
        self.set_active_source(self.source if self.source in vs else (vs[0] if vs else None), refresh_text=False)
        self.refresh_texts()

    def change_view(self, _e=None):
        if not self.commit(silent=True): return
        i = self.source_combo.current()
        if 0 <= i < len(self.view_options): self.set_view(self.view_options[i])

    def set_view(self, option):
        self.view_mode = option
        vs = self.current_view_sources()
        active = self.source if self.source in vs else (vs[0] if vs else None)
        self.set_active_source(active, refresh_text=False)
        self.refresh_texts()

    def set_active_source(self, source: Source | None, refresh_text=True):
        self.source = source
        self.msg = None
        self.preview_page_index = 0
        if source:
            if source.lang in EU_LANGS:
                self.tbl_combo.set(source.lang)
            self._auto_select_font_bank(source.lang)
        self.refresh_tbl()
        self.refresh_font()
        self.editor.delete('1.0', 'end')
        if refresh_text: self.refresh_texts()
        self.draw_preview()

    def _activate_lang_cache(self, lang: str) -> bool:
        cached = self._lang_cache.get(lang)
        if not cached:
            return False
        root = Path(cached['root'])
        if not root.exists():
            self._lang_cache.pop(lang, None)
            return False
        self._root_generation += 1
        self._reset_live_root(root)
        self.sources = list(cached['sources'])
        self.root_folder = root
        self._rdx_items = list(cached.get('rdx_items') or [])
        self._source_index_by_id = {id(src): i for i,src in enumerate(self.sources)}
        self._live_message_count = sum(len(src.messages) for src in self.sources)
        self._live_room_count = sum(1 for src in self.sources if src.kind == 'RDX')
        self._sync_source_combo_live()
        if self.view_mode[0] == 'none' and self.sources:
            self.set_view(('all', lang))
        else:
            self.refresh_texts()
        cached_font = cached.get('font')
        if cached_font is not None:
            self.font = cached_font
            self.elf_path = cached.get('elf_path') or cached.get('elf')
            try:
                self.space_var.set(self.font.space_advance)
                self.elf_label.configure(text=Path(self.elf_path).name if self.elf_path else '')
                self.refresh_font(); self.refresh_positions()
            except Exception:
                pass
        else:
            elf = cached.get('elf')
            if elf and Path(elf).exists():
                try:
                    self.load_elf(Path(elf))
                    cached['font'] = self.font; cached['elf_path'] = self.elf_path
                except Exception:
                    pass
        banks = cached.get('font_banks')
        if banks:
            self.font_banks = banks
            self.font_bank_combo['values'] = [b.label for b in banks]
            self._auto_select_font_bank(lang)
        self.progress['value'] = 100
        self.set_status(f'{self.t("ready")} • {lang} • cache • {self.t("texts_progress")} {self._live_message_count} • {self.t("rooms_progress")} {self._live_room_count}')
        return True

    def change_game_lang(self, _e=None):
        lang = (self.tbl_combo.get() or 'ENG').upper()
        if lang not in EU_LANGS:
            return
        changed = lang != self.game_lang
        self.game_lang = lang
        if hasattr(self, 'font_mode'):
            self.font_mode.current(0 if lang == 'ENG' else 1)
            if self.font:
                self.refresh_font(); self.draw_preview(focus=True)
        if not changed:
            return
        # A language is decoded only once per opened game. Returning to it is
        # instant and never re-expands its RDX bank.
        if self._activate_lang_cache(lang):
            return
        if self.iso_source_path and self.iso_source_path.exists():
            self._archive_serial += 1
            self._root_generation += 1
            self.scan_archive(self.iso_source_path, 'iso', self._archive_serial)
        elif self.root_folder and self.root_folder.exists():
            # For an extracted game opened directly, root_folder is the original
            # folder. For cached ISO language roots this path is temporary, so
            # use the original ISO branch above whenever applicable.
            self._root_generation += 1
            self.begin_load_root(self.root_folder)

    def change_tbl_mode(self, _e=None):
        # Kept for old callers; the top combo is now the European game-language
        # selector. Custom/Arabic tables remain loadable from the TBL tab.
        if not self.source: return
        p = here() / TBL_NAMES.get(self.game_lang, 'ENG.tbl')
        if p.exists(): self.set_tbl(p)

    def _notebook_changed(self, _e=None):
        try:
            selected = self.nametowidget(self.nb.select())
        except Exception:
            return
        if selected is getattr(self, 'font_tab', None) and self.root_folder and not self.font_banks and self._font_started_generation != self._root_generation:
            self._font_started_generation = self._root_generation
            self._scan_fonts_async(self.root_folder, self._root_generation)
        if selected is getattr(self, 'text_tab', None):
            # X/Y entries are a live preview draft until Apply is pressed.
            # Returning to Texts must show that draft without mutating the ELF.
            self.draw_preview(focus=True)

    @staticmethod
    def _contains_arabic_text(text: str) -> bool:
        return any(_ar_base_letter(ch) or _ar_presentation(ch) for ch in text)

    def _load_custom_ar_codec(self):
        path = Path(self.custom_ar_tbl_path)
        if self._tbl_edit_is_ar and self._tbl_edit_codec is not None and self._tbl_edit_path is not None:
            try:
                same = Path(self._tbl_edit_path).resolve() == path.resolve()
            except Exception:
                same = Path(self._tbl_edit_path) == path
            if same:
                return path, self._tbl_edit_codec
        if not path.exists():
            raise FileNotFoundError(f'Custom TBL AR not found: {path.name}')
        try:
            st = path.stat()
            sig = (str(path.resolve()), st.st_mtime_ns, st.st_size)
        except Exception:
            sig = (str(path), 0, 0)
        cached = self._custom_ar_codec_cache
        if cached and cached[0] == sig:
            return path, cached[1]
        c2t, t2c = parse_tbl(path)
        codec = Codec(c2t, t2c)
        self._custom_ar_codec_cache = (sig, codec)
        return path, codec

    @staticmethod
    def _is_arabic_tbl_codec(codec: Codec | None) -> bool:
        if codec is None:
            return False
        return any(isinstance(tok, str) and len(tok) == 1 and (_ar_base_letter(tok) or _ar_presentation(tok))
                   for tok in codec.t2c)

    def _tbl_ui_codec(self):
        """Codec shown in TBL/font labels only; never changes stored text decoding."""
        if self._tbl_edit_codec is not None:
            return self._tbl_edit_codec
        return self.source.codec if self.source else None

    def _bridge_visual_arabic_to_storage(self, visual: str, arcodec: Codec, storage_codec: Codec):
        """Map Arabic-TBL glyph bytes to the symbols of the storage TBL.

        This is the exact localization workflow used by the modified font:
        Arabic glyph -> byte/word from Custom TBL AR -> symbol at the SAME
        byte/word in SPA (or the active storage-language TBL). The returned
        text is therefore what must actually appear in the Text editor/file.
        """
        words=[]; i=0
        n=len(visual)
        while i<n:
            ch=visual[i]
            if ch in '\r\n':
                if ch=='\r' and i+1<n and visual[i+1]=='\n':
                    i += 1
                words.append(0xFF00); i += 1; continue

            # Parameter/control tokens are semantic and must survive unchanged.
            m=re.match(r'\[(?:HEX|0x):([0-9A-Fa-f]{1,4})\]', visual[i:])
            if m:
                words.append(int(m.group(1),16)); i += len(m.group(0)); continue
            m=re.match(r'\[(WAIT|TIME|ITEM):([0-9A-Fa-f]{1,4})\]', visual[i:], re.I)
            if m:
                op=0xFF03 if m.group(1).upper()=='ITEM' else 0xFF02
                words.extend([op,int(m.group(2),16)]); i += len(m.group(0)); continue
            if visual.startswith('[PAGE]',i):
                words.extend([0xFF02,0]); i += 6; continue

            # Prefer the Arabic TBL for every symbol it defines, not only the
            # letters. This matters for punctuation whose byte was repurposed.
            matched=False
            for tok in arcodec.tokens:
                if visual.startswith(tok,i):
                    words.append(arcodec.t2c[tok]); i += len(tok); matched=True; break
            if matched:
                continue
            if ch in arcodec.t2c:
                words.append(arcodec.t2c[ch]); i += 1; continue

            # Non-Arabic text is never reversed/remapped. This also keeps mixed
            # Latin product names valid when the Arabic TBL has no such glyph.
            if _ar_base_letter(ch) or _ar_presentation(ch):
                raise ValueError(f'Arabic character is not available in Custom TBL AR: {ch!r} (U+{ord(ch):04X})')

            # Preserve any storage-side token such as [WHITE]/[BLUE].
            if ch == '[':
                close=visual.find(']',i+1)
                if close != -1:
                    token=visual[i:close+1]
                    try:
                        tw=storage_codec.encode_text(token)
                        words.extend(tw); i=close+1; continue
                    except Exception:
                        pass
            words.extend(storage_codec.encode_text(ch)); i += 1

        storage_text=storage_codec.decode_words(words)
        # Round-trip is mandatory: the SPA/storage symbols must reproduce the
        # exact bytes selected by the Arabic TBL before we expose them to user.
        check=storage_codec.encode_text(storage_text)
        if list(check) != list(words):
            raise ValueError('TBL byte bridge round-trip failed')
        return storage_text, words

    def _arabic_to_storage(self, text: str, storage_codec: Codec | None = None):
        """Arabic input -> shaped/reversed Arabic TBL -> storage-TBL symbols."""
        if storage_codec is None:
            storage_codec = self.source.codec if self.source else None
        if storage_codec is None:
            raise ValueError('No storage TBL is active')
        if not self._contains_arabic_text(text):
            words=storage_codec.encode_text(text)
            return text, words, False
        _path, arcodec = self._load_custom_ar_codec()
        visual, _ = prepare_arabic_visual(text, arcodec)
        storage_text, words = self._bridge_visual_arabic_to_storage(visual, arcodec, storage_codec)
        return storage_text, words, storage_text != text

    def _apply_codec_to_language(self, lang: str, path: Path, codec: Codec):
        """Apply one TBL mapping to every live/cached source of *lang*."""
        self.custom_tbl_paths[lang] = Path(path)
        seen = set()
        def apply(src):
            if src.lang != lang or id(src) in seen:
                return
            seen.add(id(src))
            src.tbl_path = Path(path)
            # Each source gets its own mapping dictionaries so later TBL edits
            # cannot accidentally mutate every cached source by reference.
            src.codec = Codec(dict(codec.c2t), dict(codec.t2c))
            if hasattr(src.doc, 'codec'):
                try: src.doc.codec = src.codec
                except Exception: pass
        for src in self.sources:
            apply(src)
        for cache in self._lang_cache.values():
            for src in cache.get('sources', []):
                apply(src)

    def use_custom_ar_tbl(self):
        try:
            path, codec = self._load_custom_ar_codec()
            # IMPORTANT: Arabic TBL is only the glyph/byte bridge. Do not assign
            # it to Source.codec; Source.codec must stay SPA/FRA/GER/ENG so a
            # tab/scope change cannot turn stored surrogate symbols back Arabic.
            self._tbl_edit_path = Path(path)
            self._tbl_edit_codec = Codec(dict(codec.c2t), dict(codec.t2c))
            self._tbl_edit_is_ar = True
            self.set_status(f'Custom TBL AR • {path.name}')
            self.refresh_tbl(); self.refresh_font(); self.draw_preview(focus=True)
        except Exception as ex:
            self.err(ex)

    def _codec_for_save_text(self, text: str):
        """Always return the real storage codec.

        Arabic is converted through Custom TBL AR and then back to the storage
        TBL symbols before saving. Never swap Source.codec to Arabic.
        """
        return self.source.codec if self.source else None

    def set_tbl(self, path: Path):
        path=Path(path)
        c,t=parse_tbl(path)
        codec=Codec(c,t)
        if self._is_arabic_tbl_codec(codec):
            # An Arabic-capable TBL is a conversion table. Keep the actual game
            # text decoder (SPA etc.) untouched and show this table only in the
            # TBL/font mapping UI.
            self.custom_ar_tbl_path = path
            self._custom_ar_codec_cache = None
            self._tbl_edit_path = path
            self._tbl_edit_codec = Codec(dict(c), dict(t))
            self._tbl_edit_is_ar = True
            self.set_status(f'{self.t("tblcustom")} • {path.name}')
            self.refresh_tbl(); self.refresh_font(); self.draw_preview(focus=True)
            return

        # Non-Arabic tables remain normal storage tables for the chosen language.
        lang=(self.source.lang if self.source and self.source.lang in EU_LANGS else self.game_lang)
        self.custom_tbl_paths[lang]=path
        self._tbl_edit_path = None
        self._tbl_edit_codec = None
        self._tbl_edit_is_ar = False

        def apply_to(src):
            if src.lang != lang: return
            src.tbl_path=path
            src.codec=Codec(dict(c), dict(t))
            if hasattr(src.doc, 'codec'):
                try: src.doc.codec=src.codec
                except Exception: pass

        seen=set()
        for src in self.sources:
            if id(src) not in seen:
                apply_to(src); seen.add(id(src))
        cached=self._lang_cache.get(lang)
        if cached:
            for src in cached.get('sources', []):
                if id(src) not in seen:
                    apply_to(src); seen.add(id(src))
        self.set_status(f'{self.t("tblcustom")} • {lang} • {path.name}')
        self.refresh_texts(); self.refresh_tbl(); self.refresh_font()
        if self.source and self.msg:
            self.load_editor()
        else:
            self.draw_preview()

    def refresh_texts(self):
        if not hasattr(self, 'text_tree'): return
        self._text_row_serial += 1
        serial = self._text_row_serial
        self.text_tree.delete(*self.text_tree.get_children())
        self.tree_map = {}
        self._text_row_queue.clear()
        for src in self.current_view_sources():
            for m in src.messages:
                self._text_row_queue.append((src, m))
        self.after(1, self._drain_text_rows, serial)

    def _drain_text_rows(self, serial):
        if serial != self._text_row_serial or not hasattr(self, 'text_tree'):
            return
        q = self.search.get().casefold().strip() if hasattr(self, 'search') else ''
        source_index = self._source_index_by_id
        if len(source_index) != len(self.sources):
            source_index = {id(s): i for i, s in enumerate(self.sources)}; self._source_index_by_id = source_index
        deadline = time.perf_counter() + 0.004
        added = 0
        while self._text_row_queue and added < 90 and time.perf_counter() < deadline:
            src, m = self._text_row_queue.popleft()
            si = source_index.get(id(src))
            if si is None:
                continue
            tx = src.codec.decode_words(m.words)
            # Texts list must show the exact character order stored in the game.
            # Tk/Windows applies RTL bidi to Arabic automatically, which can make
            # a reversed game string look natural (or the opposite).  Force only
            # the LIST DISPLAY to LTR so reversed stays reversed and non-reversed
            # stays non-reversed.  This never changes msg.words or exported CSV.
            pv = self.storage_order_preview(tx)
            row_name = getattr(m, 'display_name', src.room_name) if src.kind == 'ELF' else src.room_name
            if q and q not in tx.casefold() and q not in pv.casefold() and q not in row_name.casefold() and q not in str(m.index):
                continue
            iid = f's{si}_m{m.index}_{m.block}'
            if self.text_tree.exists(iid):
                continue
            self.tree_map[iid] = (src, m)
            self.text_tree.insert('', 'end', iid=iid, values=(src.lang, row_name[:42], m.index, pv[:180]))
            added += 1
        if self._text_row_queue:
            self.after(1, self._drain_text_rows, serial)

    def pick_text(self, _e=None):
        if self._save_busy:
            return
        sel = self.text_tree.selection()
        if not sel: return
        pair = self.tree_map.get(sel[0])
        if not pair: return
        src, msg = pair
        if self.msg is not None and (src is not self.source or msg is not self.msg) and not self.commit(): return
        if src is not self.source:
            self.source = src
            if src.lang in EU_LANGS:
                self.tbl_combo.set(src.lang)
            self._auto_select_font_bank(src.lang)
            self.refresh_tbl()
        self.msg = msg
        self.preview_page_index = 0
        if self.root_folder and not self.font_banks and self._font_started_generation != self._root_generation:
            self._font_started_generation = self._root_generation
            self._scan_fonts_async(self.root_folder, self._root_generation)
        self.load_editor()

    def load_editor(self):
        self._manual_ar_order = False
        self._loading = True
        self.editor.delete('1.0', 'end')
        if self.source and self.msg:
            self.editor.insert('1.0', self.source.codec.decode_words(self.msg.words))
        self.editor.edit_modified(False)
        self._loading = False
        try: self.editor.see('1.0'); self.canvas.yview_moveto(0.0); self.canvas.xview_moveto(0.0)
        except Exception: pass
        self.draw_preview()

    def editor_changed(self, _e=None):
        if self._loading:
            self.editor.edit_modified(False); return
        if self.editor.edit_modified():
            self._manual_ar_order = False
            if self.source: self.source.dirty = True
            self.editor.edit_modified(False)
            self.preview_page_index = 0
            self.draw_preview()

    def insert_token(self, token):
        self.editor.insert('insert', token)
        self.editor.focus_set()
        self.draw_preview()

    def reverse_arabic_editor(self):
        """Arabic only: reverse/shape, map by Arabic TBL byte, then show SPA symbol."""
        raw=self.editor.get('1.0', 'end-1c')
        if not self._contains_arabic_text(raw) or not self.source:
            return
        try:
            converted, _words, changed = self._arabic_to_storage(raw, self.source.codec)
        except Exception as ex:
            self.err(ex); return
        if not changed:
            return
        self._loading=True
        try:
            self.editor.delete('1.0','end'); self.editor.insert('1.0',converted)
            self.editor.edit_modified(False)
        finally:
            self._loading=False
        self._manual_ar_order = False
        self.source.dirty=True
        self.preview_page_index=0
        self.draw_preview(focus=True)

    def commit(self, silent=False):
        if not self.source or not self.msg: return True
        try:
            raw_text=self.editor.get('1.0', 'end-1c')
            codec=self.source.codec
            if self._contains_arabic_text(raw_text):
                save_text, words, converted = self._arabic_to_storage(raw_text, codec)
                if converted:
                    # Text must remain in the same storage representation after
                    # moving to another section/tab. Never reload it as Arabic.
                    self._loading=True
                    try:
                        self.editor.delete('1.0','end'); self.editor.insert('1.0',save_text)
                        self.editor.edit_modified(False)
                    finally:
                        self._loading=False
            else:
                save_text=raw_text
                words=codec.encode_text(save_text)
        except Exception as ex:
            if not silent: messagebox.showerror(TITLE, str(ex))
            return False
        if list(words) != list(self.msg.words):
            self.msg.words = list(words)
            self.source.dirty = True
        self._manual_ar_order = False
        return True

    def clean_preview(text):
        text = text.replace('[LINE]', '\n').replace('[PAGE]', '\n\n')
        text = re.sub(r'\[(?:WAIT|TIME|ITEM):[0-9A-Fa-f]+\]', '', text)
        text = re.sub(r'\[(?:HEX:[0-9A-Fa-f]+|WHITE|BLUE|RED|GREEN|GREY|CLEAR|SELECT|END|0x[0-9A-Fa-f]+)\]', '', text)
        return text

    @classmethod
    def storage_order_preview(cls, text):
        """Display the exact stored glyph sequence in the Texts tree.

        Arabic is strong RTL Unicode, so Treeview can visually reorder it even
        though decode_words() returned the exact game byte/TBL order.  U+202D
        (LTR Override) is display-only and cancels that UI bidi reordering.
        Therefore a game-reversed Arabic line visibly stays reversed, while an
        unreversed Arabic line visibly stays unreversed.  Other languages are
        returned untouched.
        """
        clean = cls.clean_preview(text)
        shown = []
        for line in clean.split('\n'):
            if cls._contains_arabic_text(line):
                line = '\u202d' + line + '\u202c'
            shown.append(line)
        return ' ↵ '.join(shown)

    def width_array(self):
        if not self.font: return None
        return self.font.origin if self.font_mode.current() == 0 else self.font.eu

    def font_advance(self, code: int) -> int:
        arr = self.width_array()
        if arr is not None and 0 <= code < len(arr): return int(arr[code])
        return 28

    def current_words(self):
        if not self.source: return []
        try:
            raw = self.editor.get('1.0', 'end-1c')
            if self._contains_arabic_text(raw):
                _storage_text, words, _changed = self._arabic_to_storage(raw, self.source.codec)
                return list(words)
            return self.source.codec.encode_text(raw)
        except Exception:
            return []

    def split_preview_pages(self, words):
        pages = [[]]
        i = 0
        while i < len(words):
            w = words[i]
            if w == 0xFF02 and i + 1 < len(words):
                param = words[i + 1]
                if param == 0:
                    pages.append([])
                i += 2
                continue
            if w == 0xFE08:
                pages.append([]); i += 1; continue
            if w == 0xFF03 and i + 1 < len(words):
                pages[-1].extend(words[i:i+2]); i += 2; continue
            pages[-1].append(w); i += 1
        return pages or [[]]

    def step_preview_page(self, delta):
        pages = self.split_preview_pages(self.current_words())
        self.preview_page_index = max(0, min(len(pages) - 1, self.preview_page_index + delta))
        try: self.canvas.yview_moveto(0.0); self.canvas.xview_moveto(0.0)
        except Exception: pass
        self.draw_preview()

    def message_xy(self):
        # Fixed, predictable preview origin: 10 px from the real game-screen
        # top-left.  There are deliberately no editable X/Y controls.
        return 10.0, 10.0

    def _rtl_words(self, words):
        lines = []; cur = []; i = 0
        while i < len(words):
            w = words[i]
            if w == 0xFF00:
                lines.append(cur); lines.append([0xFF00]); cur = []; i += 1; continue
            if w in (0xFF02, 0xFF03) and i + 1 < len(words):
                cur.extend(words[i:i+2]); i += 2; continue
            cur.append(w); i += 1
        lines.append(cur)
        out = []
        for seg in lines:
            if seg == [0xFF00]: out += seg
            else: out += list(reversed(seg))
        return out

    def layout_words(self, words):
        sx, sy = self.message_xy()
        x, y, start = sx, sy, sx
        out = []
        color = 0
        i = 0
        if hasattr(self, 'rtl') and self.rtl.get(): words = self._rtl_words(words)
        while i < len(words):
            w = words[i]
            if w == 0xFF00:
                x = start; y += 30; i += 1; continue
            if w == 0xFF01:
                adv = int(self.font.space_advance) if self.font else int(self.space_var.get() or 14)
                out.append((None, ' ', x, y, adv, self.height_overrides.get(0xFF01, 28), color, 'space'))
                x += adv; i += 1; continue
            if w == 0xFF02:
                i += 2 if i + 1 < len(words) else 1; continue
            if w == 0xFF03:
                # Item name is dynamic. Reserve one cell so layout is visible.
                out.append((None, '[ITEM]', x, y, 28, 28, color, 'item')); x += 28
                i += 2 if i + 1 < len(words) else 1; continue
            if w == 0xFF04:
                # Selection marker stores this position and later draws glyph 0x1C.
                out.append((0x1C, '>', x, y, 28, 28, color, 'select')); x += 28; i += 1; continue
            if 0xFE01 <= w <= 0xFE05:
                color = w - 0xFE01; i += 1; continue
            if w >= 0xFE00:
                i += 1; continue
            adv = self.font_advance(w)
            ui_codec = self._tbl_ui_codec()
            char = ui_codec.c2t.get(w, f'{w:04X}') if ui_codec else ''
            hh = self.height_overrides.get(w, 28)
            out.append((w, char, x, y, adv, hh, color, 'glyph'))
            x += adv; i += 1
        return out

    def _tinted_glyph(self, image, color):
        if Image is None or ImageOps is None or image is None: return None
        targets = [(245, 245, 245), (32, 224, 255), (255, 32, 32), (32, 255, 32), (110, 110, 110)]
        target = targets[max(0, min(color, len(targets) - 1))]
        lum = ImageOps.grayscale(image.convert('RGB'))
        # Decoded PS2 font pages are opaque with a black background. Use
        # luminance as coverage so only the glyph is drawn in the simulator.
        mx = max(1, lum.getextrema()[1])
        alpha = lum.point(lambda v: min(255, int(v * 255 / mx)))
        out = Image.new('RGBA', image.size, target + (0,))
        out.putalpha(alpha)
        return out

    def draw_preview(self, focus=True):
        """Render the whole current message with a tight orange content frame.

        Text begins at the fixed 10x10 inset. The orange frame follows the real
        rendered content and ends with a small margin above, below and to the
        right. Long and multi-line messages are measured first and auto-fitted
        as one unit so no line is clipped.
        """
        if not hasattr(self, 'canvas'):
            return
        c = self.canvas
        old_x = c.xview()[0] if c.xview() else 0.0
        old_y = c.yview()[0] if c.yview() else 0.0
        c.delete('all')
        self._preview_refs = []

        pages = self.split_preview_pages(self.current_words())
        self.preview_page_index = max(0, min(len(pages) - 1, self.preview_page_index))
        if hasattr(self, 'page_label'):
            self.page_label.configure(text=f'{self.preview_page_index + 1}/{len(pages)}')
        words = pages[self.preview_page_index] if pages else []
        layout = self.layout_words(words)

        # Fixed text origin is 10x10.  Frame starts at 0x0, which gives the text
        # a 10px top/left inset, and ends 10px after the furthest visible content.
        sx, sy = self.message_xy()
        pad_right = 10.0
        pad_bottom = 10.0

        # Width uses the actual visible 28px glyph cell, not only Advance. This
        # prevents narrow-advance letters from visually escaping the frame.
        right = sx
        if layout:
            for _code, _char, x, _y, adv, _h, _col, typ in layout:
                cell_w = float(adv) if typ == 'space' else float(max(28, adv))
                right = max(right, x + max(1.0, cell_w))

        # Count line commands directly so empty/trailing lines still get room.
        line_count = 1 + sum(1 for w in words if w == 0xFF00)
        bottom = sy + (max(1, line_count) - 1) * 30.0 + 28.0
        if layout:
            bottom = max(bottom, max(y + 28.0 for _code,_char,_x,y,_adv,_h,_col,_typ in layout))

        frame_w = max(20.0, right + pad_right)
        frame_h = max(20.0, bottom + pad_bottom)

        # Fit the complete content frame to the current preview area. Short text
        # stays at 100%; only oversized content is scaled down.
        c.update_idletasks()
        cw = max(160.0, float(c.winfo_width()))
        ch = max(120.0, float(c.winfo_height()))
        margin = 8.0
        avail_w = max(40.0, cw - margin * 2.0 - 2.0)
        avail_h = max(40.0, ch - margin * 2.0 - 2.0)
        scale = min(1.0, avail_w / frame_w, avail_h / frame_h)
        scale = max(0.035, scale)
        ox = margin
        oy = margin
        self._preview_transform = (scale, ox, oy)

        # One clean orange frame only; no red grid/boxes in Texts.
        c.create_rectangle(ox, oy, ox + frame_w * scale, oy + frame_h * scale,
                           outline=ACC, fill=CANVAS, width=1)

        bank = self.font_bank
        for code, char, x, y, adv, box_h, color, typ in layout:
            X = ox + x * scale
            Y = oy + y * scale
            glyph_px = max(1, round(28 * scale))
            if code is not None and bank is not None and ImageTk is not None:
                glyph = bank.glyph_image(code)
                glyph = self._tinted_glyph(glyph, color) if glyph is not None else None
                if glyph is not None:
                    glyph = glyph.resize((glyph_px, glyph_px),
                                         Image.Resampling.NEAREST if glyph_px >= 28 else Image.Resampling.LANCZOS)
                    ref = ImageTk.PhotoImage(glyph)
                    self._preview_refs.append(ref)
                    c.create_image(X, Y, image=ref, anchor='nw')
            elif typ == 'item':
                c.create_text(X + 3*scale, Y + 7*scale, text='ITEM', fill=MUTED, anchor='w',
                              font=('Segoe UI', max(5, int(9*scale))))
            elif typ != 'space' and char and not str(char).startswith('['):
                c.create_text(X + 10*scale, Y + 10*scale, text=char, fill=TEXT,
                              font=('Segoe UI', max(5, int(11*scale))))

        # Keep diagnostics hidden from the normal interface.
        if hasattr(self, 'metrics'):
            self.metrics.configure(text='')

        virtual_w = max(cw, ox + frame_w * scale + margin)
        virtual_h = max(ch, oy + frame_h * scale + margin)
        c.configure(scrollregion=(0, 0, virtual_w, virtual_h))
        if focus:
            c.xview_moveto(0.0)
            c.yview_moveto(0.0)
        else:
            try:
                c.xview_moveto(old_x); c.yview_moveto(old_y)
            except Exception:
                pass

    # Preview position is fixed at 10x10; mouse X/Y dragging is disabled.
    def canvas_click(self, event):
        return

    def save_source(self):
        if self._save_busy:
            return
        if self.source is not None and not self.commit():
            return
        dirty = [s for s in self.sources if s.dirty]
        if not dirty:
            self.progress['value'] = 100
            self.set_status(self.t('saved'))
            return

        current_src = self.source
        current_msg_index = getattr(self.msg, 'index', None) if self.msg is not None else None
        current_old_name = getattr(self.msg, 'display_name', None) if self.msg is not None else None
        self._save_busy = True
        try:
            self.editor.configure(state='disabled')
        except Exception:
            pass
        # Repack progress always starts from zero.  100 is reserved exclusively
        # for _finish_sources_save(), so 100% really means the write is complete.
        self.progress['value'] = 0
        self.set_status(('إعادة بناء' if self.ui == 'ar' else 'Repack') + ' 0%')

        def repack_progress(percent, src=None, stage=''):
            if not self._save_busy:
                return
            p = max(0, min(99, int(percent)))
            self.progress['value'] = p
            label = 'إعادة بناء' if self.ui == 'ar' else 'Repack'
            extra = ''
            if src is not None:
                extra = f' • {src.kind} • {src.room_name}'
            # Keep the status compact; only show the active heavy phase.
            if stage and stage not in ('Done', 'PRS'):
                extra += f' • {stage}'
            self.set_status(f'{label} {p}%{extra}')
            try:
                self.update_idletasks()
            except Exception:
                pass

        def worker():
            total_units = max(1, len(dirty))

            def post_fraction(base_units, span_units, frac, src, stage=''):
                frac = max(0.0, min(1.0, float(frac)))
                percent = ((float(base_units) + float(span_units) * frac) / total_units) * 99.0
                self._post_ui(repack_progress, percent, src, stage)

            try:
                # Group RDX rooms that live in the same AFS. One AFS rebuild per
                # group, while the progress bar receives fine-grained PRS/AFS/ISO
                # callbacks so the window remains visibly active during long work.
                jobs = []
                grouped = {}
                for src in dirty:
                    key = None
                    if src.kind == 'RDX':
                        if src.iso_path is not None and src.iso_entry_path and src.iso_afs_index is not None and src.afs_path is None:
                            try: ip = str(src.iso_path.resolve()).casefold()
                            except Exception: ip = str(src.iso_path).casefold()
                            key = ('iso', ip, src.iso_entry_path.casefold())
                        elif src.afs_path is not None and src.iso_path is None and src.afs_index is not None:
                            try: ap = str(src.afs_path.resolve()).casefold()
                            except Exception: ap = str(src.afs_path).casefold()
                            key = ('afs', ap)
                    if key is None:
                        jobs.append(('one', [src]))
                    elif key in grouped:
                        grouped[key][1].append(src)
                    else:
                        job = ('batch', [src], key)
                        grouped[key] = job
                        jobs.append(job)

                done = 0
                for job in jobs:
                    if job[0] == 'one' or len(job[1]) == 1:
                        src = job[1][0]
                        base = done
                        post_fraction(base, 1, 0.0, src, 'Prepare')
                        src.save(progress=lambda d, t, st, b=base, ss=src:
                                 post_fraction(b, 1, d / max(1, t), ss, st))
                        done += 1
                        post_fraction(base, 1, 1.0, src, 'Done')
                        continue

                    group = job[1]; key = job[2]
                    group_n = len(group); base = done
                    td = Path(tempfile.mkdtemp(prefix='recvx_repack_batch_'))
                    try:
                        replacements = {}
                        # Payload preparation/compression = first 35% of this AFS job.
                        for n, src in enumerate(group):
                            idx = src.iso_afs_index if key[0] == 'iso' else src.afs_index
                            payload = td / f'{int(idx):05d}_{n:03d}.bin'
                            prep0 = 0.35 * (n / max(1, group_n))
                            prep_span = 0.35 / max(1, group_n)
                            post_fraction(base, group_n, prep0, src, 'Prepare')
                            src.prepare_rdx_batch_payload(
                                payload,
                                progress=lambda d, t, st, p0=prep0, ps=prep_span, ss=src:
                                    post_fraction(base, group_n, p0 + ps * (d / max(1, t)), ss, st)
                            )
                            replacements[int(idx)] = payload
                            post_fraction(base, group_n, prep0 + prep_span, src, 'Prepare')

                        # AFS/ISO write + verification = next 60%.
                        first = group[0]
                        if key[0] == 'iso':
                            replace_iso_afs_entries_streaming(
                                first.iso_path, first.iso_entry_path, replacements,
                                progress=lambda d, t, st:
                                    post_fraction(base, group_n, 0.35 + 0.60 * (d / max(1, t)), first, st)
                            )
                        else:
                            replace_afs_entries_streaming(
                                first.afs_path, replacements,
                                progress=lambda d, t, st:
                                    post_fraction(base, group_n, 0.35 + 0.60 * (d / max(1, t)), first, st)
                            )

                        # Reload changed rooms = last 5%.
                        for n, src in enumerate(group, 1):
                            src.finish_rdx_batch_save()
                            post_fraction(base, group_n, 0.95 + 0.05 * n / group_n, src, 'Finalize')
                        done += group_n
                    finally:
                        shutil.rmtree(td, ignore_errors=True)
                self._post_ui(self._finish_sources_save, current_src, current_msg_index, current_old_name, None)
            except Exception as ex:
                self._post_ui(self._finish_sources_save, current_src, current_msg_index, current_old_name, ex)
        self._save_thread = threading.Thread(target=worker, daemon=True)
        self._save_thread.start()

    def _finish_sources_save(self, current_src, msg_index, old_name, error):
        self._save_busy = False
        try:
            self.editor.configure(state='normal')
        except Exception:
            pass
        if error is not None:
            self.err(error); return
        if any(s.iso_path is not None for s in self.sources):
            self._iso_entries_cache = None
            self._iso_entries_cache_key = None
        if current_src is not None and current_src in self.sources:
            self.source = current_src
            if current_src.kind == 'ELF':
                self.msg = next((m for m in current_src.messages if getattr(m, 'display_name', None) == old_name), None)
                if self.elf_path and self.elf_path.resolve() == current_src.path.resolve():
                    try:
                        e = Elf32(current_src.path); self.font = FontData(e); self.space_var.set(self.font.space_advance)
                        if current_src.lang in self._lang_cache:
                            self._lang_cache[current_src.lang]['font'] = self.font; self._lang_cache[current_src.lang]['elf_path'] = self.elf_path
                        self.refresh_font(); self.refresh_positions()
                    except Exception:
                        pass
            elif msg_index is not None:
                self.msg = next((m for m in current_src.messages if m.index == msg_index), None)
            if self.msg is not None:
                self.load_editor()
        self.refresh_texts()
        self.progress['value'] = 100
        self.set_status(self.t('saved'))


    def export_csv(self):
        if not self.commit(): return
        view = self.current_view_sources()
        if not view: return
        scope_name = {'ALL':'All','RDX':'RDX','ELF':'ELF'}.get(self.text_scope_value, 'All')
        default = f'{view[0].lang}_{scope_name}_Texts.csv' if len(view) > 1 else f'{view[0].room_name}.csv'
        p = filedialog.asksaveasfilename(defaultextension='.csv', initialfile=default, filetypes=[('CSV', '*.csv')])
        if not p: return
        with open(p, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f)
            w.writerow(['Language', 'Kind', 'Source', 'Room', 'ID', 'Text'])
            for src in view:
                for msg in src.messages:
                    room = getattr(msg, 'display_name', src.room_name) if src.kind == 'ELF' else src.room_name
                    w.writerow([src.lang, src.kind, src.source_key, room, msg.index, src.codec.decode_words(msg.words)])

    def _csv_message_for_row(self, src, row):
        """Resolve a CSV row to the exact message it names.

        ELF rows are keyed by their exported Room/display_name first because
        symbol order can differ after an ELF has already been edited.  RDX and
        SYSMES keep the normal numeric ID behavior.
        """
        room = (row.get('Room') or '').strip()
        try:
            wanted_id = int(row.get('ID', '-1'))
        except Exception:
            wanted_id = -1
        if src.kind == 'ELF' and room:
            for m in src.messages:
                if getattr(m, 'display_name', '') == room:
                    return m
        if 0 <= wanted_id < len(src.messages):
            m = src.messages[wanted_id]
            if src.kind != 'ELF' or not room or getattr(m, 'display_name', '') == room:
                return m
        if room:
            for m in src.messages:
                name = getattr(m, 'display_name', src.room_name if src.kind != 'ELF' else '')
                if name == room:
                    return m
        return None

    def _encode_import_text(self, src, raw_csv_text):
        """CSV import: only Arabic is reversed/remapped; all other languages stay exact."""
        if not self._contains_arabic_text(raw_csv_text):
            return src.codec.encode_text(raw_csv_text)
        _storage_text, words, _changed = self._arabic_to_storage(raw_csv_text, src.codec)
        return list(words)

    def import_csv(self):
        view = self.current_view_sources()
        if not view: return
        p = filedialog.askopenfilename(filetypes=[('CSV', '*.csv')])
        if not p: return
        try:
            rows = list(csv.DictReader(open(p, encoding='utf-8-sig', newline='')))
            by_key = {}
            for s in view:
                key=(s.lang.upper(), s.kind.upper(), s.source_key.casefold())
                by_key[key]=s
                # ELF exported from an ISO/folder may differ only in path/name
                # casing.  Keep a basename alias without weakening Kind/Language.
                by_key[(s.lang.upper(), s.kind.upper(), Path(s.source_key).name.casefold())]=s
            changed = 0
            touched = set()
            for r in rows:
                lang=(r.get('Language') or '').upper()
                kind=(r.get('Kind') or '').upper()
                source=(r.get('Source') or '').casefold()
                src = by_key.get((lang, kind, source))
                if src is None:
                    src = by_key.get((lang, kind, Path(r.get('Source') or '').name.casefold()))
                if src is None and len(view) == 1:
                    src = view[0]
                if src is None:
                    continue
                msg = self._csv_message_for_row(src, r)
                if msg is None:
                    continue
                words = self._encode_import_text(src, r.get('Text', ''))
                if list(words) == list(msg.words):
                    continue
                msg.words = list(words)
                src.dirty = True
                touched.add(id(src))
                changed += 1
            self.msg = None
            self.editor.delete('1.0', 'end')
            # An ELF-only CSV import must not strand the browser on one ELF
            # source.  If Show is All, restore the full language-wide source set
            # immediately (RDX + SYSMES + ELF).
            if self.text_scope_value == 'ALL':
                lang = self.source.lang if self.source and self.source.lang in EU_LANGS else self.game_lang
                target = ('all', lang)
                self.view_mode = target
                for i, opt in enumerate(self.view_options):
                    if opt == target:
                        try: self.source_combo.current(i)
                        except Exception: pass
                        break
            self.refresh_texts(); self.draw_preview()
            self.set_status(f'{changed} • {len(touched)}')
        except Exception as ex:
            self.err(ex)

    def open_elf(self):
        p = filedialog.askopenfilename(title=TITLE, filetypes=[('PS2 ELF', '*.*')])
        if p:
            try: self.load_elf(Path(p))
            except Exception as ex: self.err(ex)

    def load_elf(self, path: Path):
        e = Elf32(path)
        self.font = FontData(e)
        self.elf_path = Path(path)
        self.space_var.set(self.font.space_advance)
        self.elf_label.configure(text=Path(path).name)
        self.refresh_font(); self.draw_preview()

    def refresh_font(self):
        if not hasattr(self, 'font_tree'): return
        self.font_tree.delete(*self.font_tree.get_children())
        arr = self.width_array()
        codec = self._tbl_ui_codec()
        codes = set(range(len(arr))) if arr is not None else set()
        if codec:
            codes.update(c for c in codec.c2t if 0 <= c < 0xFE00)
        # Never rescan every RDX message merely because the user clicked a row.
        # Known TBL codes already cover the European fonts; only the currently
        # selected message contributes any extra/unknown glyph codes.
        if self.msg is not None:
            for c in self.msg.words:
                if 0 <= c < 0xFE00:
                    codes.add(c)
        for code in sorted(codes):
            adv = int(arr[code]) if arr is not None and code < len(arr) else 28
            ch = codec.c2t.get(code, '') if codec else ''
            local = code % 324
            self.font_tree.insert('', 'end', iid=str(code), values=(f'0x{code:04X}', ch if ch != ' ' else 'SPACE', adv, code // 324, (local % 18) * 14, (local // 18) * 14))

    def pick_glyph(self, _e=None):
        sel = self.font_tree.selection()
        if not sel: return
        code = int(sel[0])
        self.selected_code = code
        self.adv_var.set(self.font_advance(code))
        self.h_var.set(self.height_overrides.get(code, 28))
        if hasattr(self, 'actual_box_label'): self.actual_box_label.configure(text=f'{self.font_advance(code)}×28')
        page = code // 324
        if self.font_bank and page < len(self.font_bank.pages):
            self.font_page_combo.current(page)
        self.draw_font_preview(); self.draw_preview()

    def select_font_code(self, code):
        if hasattr(self, 'font_tree') and self.font_tree.exists(str(code)):
            self.font_tree.selection_set(str(code)); self.font_tree.see(str(code)); self.pick_glyph()

    def live_apply_glyph(self, preview_only=False):
        """Apply the selected glyph values immediately.

        Advance is a real game value (FontSz) and therefore affects the next
        glyph both in this preview and after Save Font. Box H is only a visual
        helper because the retail engine always draws a 28px-high quad.
        """
        if self.selected_code is None:
            return False
        try:
            adv = int(self.adv_var.get()); h = int(self.h_var.get())
        except Exception:
            return False
        if not (0 <= adv <= 255 and 1 <= h <= 64):
            return False
        arr = self.width_array()
        if not preview_only and self.font and arr is not None and self.selected_code < len(arr):
            if int(arr[self.selected_code]) != adv:
                arr[self.selected_code] = adv
                self.font.dirty = True
        self.height_overrides[self.selected_code] = h
        if hasattr(self, 'actual_box_label'): self.actual_box_label.configure(text=f'{adv}×28')
        self.draw_preview(focus=True)
        self.draw_font_preview()
        return True

    def apply_glyph(self):
        if self.selected_code is None:
            return
        try:
            if not self.live_apply_glyph(preview_only=False):
                raise ValueError('Advance 0..255 / Preview H 1..64')
            code = self.selected_code
            self.refresh_font()
            if self.font_tree.exists(str(code)):
                self.font_tree.selection_set(str(code)); self.font_tree.see(str(code))
            self.draw_preview(focus=True)
        except Exception as ex:
            self.err(ex)

    def apply_space(self, silent=False):
        if not self.font: return
        try: value = int(self.space_var.get())
        except Exception: return
        if 1 <= value <= 64:
            self.font.space_advance = value
            self.font.dirty = True
            self.height_overrides.setdefault(0xFF01, 28)
            self.draw_preview()

    def refresh_positions(self):
        return

    def load_pair(self):
        return

    def apply_position(self):
        return

    def save_font(self):
        if not self.font: return
        try:
            self.live_apply_glyph(preview_only=False); self.apply_space(); self.font.save(); self.draw_preview(focus=True); self.set_status(self.t('saved'))
        except Exception as ex:
            self.err(ex)

    def _scan_fonts_async(self, root: Path, generation: int):
        if Image is None:
            self.progress['value'] = 100
            return
        work = Path(tempfile.mkdtemp(prefix='recvx_font_'))
        self.temp.append(work)
        def worker():
            try:
                banks = find_font_banks(root, work, self.game_lang)
                self._post_ui(self._finish_font_scan, generation, banks)
            except Exception:
                self._post_ui(self._finish_font_scan, generation, [])
        self._font_thread = threading.Thread(target=worker, daemon=True)
        self._font_thread.start()

    def _finish_font_scan(self, generation, banks):
        if generation != self._root_generation: return
        self.font_banks = banks
        if self.game_lang in self._lang_cache:
            self._lang_cache[self.game_lang]['font_banks'] = banks
        self.font_bank_combo['values'] = [b.label for b in banks]
        if banks:
            self._auto_select_font_bank(self.source.lang if self.source else banks[0].lang)
        # Font discovery runs in parallel with the text/RDX scan. Do not jump
        # the global progress bar to 75% just because the small font phase ended.
        self.draw_preview(); self.draw_font_preview()

    def _auto_select_font_bank(self, lang):
        if not self.font_banks: return
        idx = next((i for i, b in enumerate(self.font_banks) if b.lang == lang), None)
        if idx is None and lang == 'ARA': idx = next((i for i, b in enumerate(self.font_banks) if b.lang in ('SPA', 'ENG')), None)
        if idx is None: idx = 0
        self.font_bank_combo.current(idx)
        self.font_bank = self.font_banks[idx]
        self._refresh_font_pages()

    def change_font_bank(self, _e=None):
        i = self.font_bank_combo.current()
        if 0 <= i < len(self.font_banks):
            self.font_bank = self.font_banks[i]
            self._refresh_font_pages(); self.draw_preview(); self.draw_font_preview()

    def _refresh_font_pages(self):
        n = len(self.font_bank.pages) if self.font_bank else 0
        self.font_page_combo['values'] = [str(i) for i in range(n)]
        page = (self.selected_code // 324) if self.selected_code is not None else 0
        if n:
            self.font_page_combo.current(min(page, n - 1))
        self.draw_font_preview()

    def selected_texture_page(self):
        i = self.font_page_combo.current()
        return i if i >= 0 else 0

    def _font_canvas_geometry(self):
        if not self.font_bank:
            return None
        page = self.selected_texture_page()
        image = self.font_bank.page_image(page)
        if image is None:
            return None
        w = max(1, self.font_canvas.winfo_width())
        h = max(1, self.font_canvas.winfo_height())
        sc = max(0.01, min((w - 12) / image.width, (h - 12) / image.height))
        dw = image.width * sc; dh = image.height * sc
        ox = (w - dw) / 2; oy = (h - dh) / 2
        unit_x = image.width / 256.0; unit_y = image.height / 256.0
        return page, image, sc, ox, oy, unit_x, unit_y

    def draw_font_preview(self):
        if not hasattr(self, 'font_canvas'): return
        c = self.font_canvas; c.delete('all'); self._font_ref = None
        w = max(1, c.winfo_width()); h = max(1, c.winfo_height())
        c.create_rectangle(1, 1, w - 2, h - 2, outline=BORDER)
        geom = self._font_canvas_geometry()
        if not geom or ImageTk is None: return
        page, image, sc, ox, oy, unit_x, unit_y = geom
        dw = max(1, int(image.width * sc)); dh = max(1, int(image.height * sc))
        preview = image.resize((dw, dh), Image.Resampling.NEAREST if sc >= 1 else Image.Resampling.LANCZOS)
        self._font_ref = ImageTk.PhotoImage(preview)
        c.create_image(ox, oy, image=self._font_ref, anchor='nw')

        code = self.selected_code
        if code is not None and code // 324 == page:
            local = code % 324
            gx = (local % 18) * 14 * unit_x; gy = (local // 18) * 14 * unit_y
            gw = 14 * unit_x; gh = 14 * unit_y
            x0 = ox + gx * sc; y0 = oy + gy * sc
            # Orange = original complete texture cell, exactly as before.
            c.create_rectangle(x0, y0, ox + (gx + gw) * sc, oy + (gy + gh) * sc,
                               outline=ACC, width=2)

    def font_canvas_press(self, event):
        geom = self._font_canvas_geometry()
        if not geom: return
        page, image, sc, ox, oy, unit_x, unit_y = geom
        px = (event.x - ox) / max(sc, 1e-6)
        py = (event.y - oy) / max(sc, 1e-6)
        cell_w = 14 * unit_x; cell_h = 14 * unit_y
        col = int(px // cell_w); row = int(py // cell_h)
        if not (0 <= col < 18 and 0 <= row < 18):
            self._font_resize = None
            return
        code = page * 324 + row * 18 + col
        self.selected_code = code
        if self.font_tree.exists(str(code)):
            self.select_font_code(code)
        else:
            self.adv_var.set(self.font_advance(code))
            self.h_var.set(self.height_overrides.get(code, 28))
            self.draw_font_preview(); self.draw_preview(focus=False)
        gx = col * cell_w; gy = row * cell_h
        self._font_resize = (code, sc, ox, oy, gx, gy, cell_w, cell_h)

    def font_canvas_drag(self, event):
        if not self._font_resize:
            return
        code, sc, ox, oy, gx, gy, cell_w, cell_h = self._font_resize
        if self.selected_code != code:
            return
        px = (event.x - ox) / max(sc, 1e-6)
        adv = int(round(28.0 * (px - gx) / max(cell_w, 1e-6)))
        adv = max(0, min(255, adv))
        self.adv_var.set(adv)
        if hasattr(self, 'actual_box_label'):
            self.actual_box_label.configure(text=f'{adv}×28')
        self.draw_font_preview()
        self.draw_preview(focus=False)

    def font_canvas_release(self, event):
        if not self._font_resize:
            return
        self._font_resize = None
        # Commit only the real Advance on release. Height stays a preview helper.
        self.live_apply_glyph(preview_only=False)

    # Compatibility for older bindings.
    def font_canvas_click(self, event):
        self.font_canvas_press(event)
        self.font_canvas_release(event)

    def export_font_png(self):
        if not self.font_bank: return
        page = self.selected_texture_page()
        p = filedialog.asksaveasfilename(defaultextension='.png', initialfile=f'{self.font_bank.afs_path.stem}_font_{page}.png', filetypes=[('PNG', '*.png')])
        if p:
            try: self.font_bank.export_page(page, Path(p))
            except Exception as ex: self.err(ex)

    def replace_font_png(self):
        if not self.font_bank or Image is None: return
        p = filedialog.askopenfilename(filetypes=[('Images', '*.png *.bmp *.tga *.jpg *.jpeg'), ('All', '*.*')])
        if not p: return
        try:
            image = Image.open(p).convert('RGBA')
            self.font_bank.replace_page(self.selected_texture_page(), image)
            self._refresh_font_pages(); self.draw_preview(); self.set_status(self.t('saved'))
        except Exception as ex:
            self.err(ex)

















    def export_font_tm2(self):
        if not self.font_bank:
            return
        page = self.selected_texture_page()
        p = filedialog.asksaveasfilename(
            defaultextension='.tm2',
            initialfile=f'{self.font_bank.afs_path.stem}_font_{page}.tm2',
            filetypes=[('TM2', '*.tm2'), ('All', '*.*')],
        )
        if not p:
            return
        try:
            self.font_bank.export_page_tm2(page, Path(p))
            self.set_status(self.t('saved'))
        except Exception as ex:
            self.err(ex)

    def replace_font_tm2(self):
        if not self.font_bank:
            return
        p = filedialog.askopenfilename(filetypes=[('TM2', '*.tm2 *.TM2'), ('All', '*.*')])
        if not p:
            return
        try:
            self.font_bank.replace_page_tm2(self.selected_texture_page(), Path(p))
            self._refresh_font_pages()
            self.draw_preview()
            self.set_status(self.t('saved'))
        except Exception as ex:
            self.err(ex)

    def refresh_tbl(self):
        if not hasattr(self, 'map_tree'): return
        self.map_tree.delete(*self.map_tree.get_children())
        codec=self._tbl_ui_codec()
        if codec is None:
            self.tbl_path_label.configure(text='')
            return
        if self._tbl_edit_codec is not None and self._tbl_edit_path is not None:
            self.tbl_path_label.configure(text=Path(self._tbl_edit_path).name)
        elif self.source:
            self.tbl_path_label.configure(text=self.source.tbl_path.name)
        else:
            self.tbl_path_label.configure(text='')
        for code, value in sorted(codec.c2t.items()):
            self.map_tree.insert('', 'end', iid=str(code), values=(f'{code:04X}', value))

    def pick_map(self, _e=None):
        s = self.map_tree.selection()
        codec=self._tbl_ui_codec()
        if not s or codec is None: return
        code = int(s[0]); self.map_code.set(f'{code:04X}'); self.map_value.set(codec.c2t.get(code, ''))

    def apply_map(self):
        codec=self._tbl_ui_codec()
        if codec is None: return
        try:
            code = int(self.map_code.get(), 16); value = self.map_value.get()
            codec.c2t[code] = value
            t2c = {}
            for c, v in codec.c2t.items():
                if v and v not in t2c: t2c[v] = c
            codec.t2c = t2c
            codec.tokens = sorted([x for x in t2c if len(x) > 1 and x.startswith('[')], key=len, reverse=True)
            try: delattr(codec, '_tbl_ar_profile_cache')
            except Exception: pass
            if self._tbl_edit_is_ar:
                self._custom_ar_codec_cache = None
            self.refresh_tbl(); self.refresh_texts(); self.refresh_font(); self.draw_preview()
        except Exception as ex:
            self.err(ex)

    def open_tbl(self):
        p = filedialog.askopenfilename(filetypes=[('TBL', '*.tbl'), ('All', '*.*')])
        if p: self.set_tbl(Path(p))

    def save_tbl(self):
        codec=self._tbl_ui_codec()
        if codec is None: return
        initial=(Path(self._tbl_edit_path).name if self._tbl_edit_path is not None else self.source.tbl_path.name if self.source else 'table.tbl')
        p = filedialog.asksaveasfilename(defaultextension='.tbl', initialfile=initial, filetypes=[('TBL', '*.tbl')])
        if not p: return
        Path(p).write_text('\n'.join(f'{c:04X}={v}' for c, v in sorted(codec.c2t.items())) + '\n', encoding='utf-8')
        if self._tbl_edit_codec is not None:
            self._tbl_edit_path=Path(p)
            if self._tbl_edit_is_ar:
                self.custom_ar_tbl_path=Path(p)
                self._custom_ar_codec_cache=None
        elif self.source:
            self.source.tbl_path = Path(p)
        self.refresh_tbl(); self.set_status(self.t('saved'))

    def _refresh_all(self):
        if hasattr(self, 'source_combo'):
            labels = []
            for kind, payload in self.view_options:
                if kind == 'all': labels.append(f'{payload} | {self.t("alltexts")}')
                elif 0 <= payload < len(self.sources): labels.append(self.sources[payload].label)
            self.source_combo['values'] = labels
        self.refresh_texts(); self.refresh_tbl(); self.refresh_font()
        if self.font:
            self.space_var.set(self.font.space_advance)
            self.elf_label.configure(text=self.elf_path.name if self.elf_path else '')
            self.refresh_positions()
        if hasattr(self, 'font_bank_combo'):
            self.font_bank_combo['values'] = [b.label for b in self.font_banks]
            if self.font_bank in self.font_banks:
                self.font_bank_combo.current(self.font_banks.index(self.font_bank)); self._refresh_font_pages()
        self.draw_preview(); self.draw_font_preview()

    def err(self, error):
        self.progress['value'] = 0
        messagebox.showerror(TITLE, str(error))
        self.set_status(self.t('ready'))

    def on_close(self):
        self._archive_serial += 1
        self._root_generation += 1
        dirty = any(s.dirty for s in self.sources) or bool(self.font and self.font.dirty)
        if dirty and not messagebox.askyesno(TITLE, 'Unsaved changes / تغييرات غير محفوظة'):
            return
        self._restore_win_dropfiles()
        for p in self.temp: shutil.rmtree(p, ignore_errors=True)
        self.destroy()


def main():
    App().mainloop()


if __name__ == '__main__':
    main()
