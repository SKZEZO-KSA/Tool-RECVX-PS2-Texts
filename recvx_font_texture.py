from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from recvx_core import AfsArchive
import tm2_core

LANG_SUFFIX = {'1': 'ENG', '2': 'FRA', '4': 'SPA', '5': 'GER'}


def system_lang(path: Path) -> str:
    m = re.fullmatch(r'system(\d*)\.afs', path.name, re.I)
    if not m:
        return 'ENG'
    s = m.group(1)
    return LANG_SUFFIX.get(s, 'JPN' if not s else 'ENG')


@dataclass
class FontTextureBank:
    lang: str
    afs_path: Path
    entry_index: int
    entry_name: str
    work_path: Path
    pages: list
    images: list[Image.Image]

    @property
    def label(self) -> str:
        return f'{self.lang} | {self.afs_path.name}'

    def refresh(self) -> None:
        containers = tm2_core.scan_file(self.work_path)
        pics = []
        for c in containers:
            for pic in c.pictures:
                if pic.safe_to_decode and pic.width == 512 and pic.height == 512:
                    pics.append(pic)
        self.pages = pics[:8]
        self.images = [tm2_core.decode_picture(p).convert('RGBA') for p in self.pages]

    def page_image(self, page: int) -> Image.Image | None:
        if 0 <= page < len(self.images):
            return self.images[page]
        return None

    def glyph_image(self, code: int) -> Image.Image | None:
        if code < 0:
            return None
        page = code // 324
        local = code % 324
        image = self.page_image(page)
        if image is None:
            return None
        # message.c uses 14 UV units in a 256-unit texture space. The decoded
        # PAL texture is 512x512, so each on-disk glyph cell is 28x28 pixels.
        unit_x = image.width / 256.0
        unit_y = image.height / 256.0
        cell_w = max(1, round(14 * unit_x))
        cell_h = max(1, round(14 * unit_y))
        x = round((local % 18) * 14 * unit_x)
        y = round((local // 18) * 14 * unit_y)
        if x >= image.width or y >= image.height:
            return None
        return image.crop((x, y, min(image.width, x + cell_w), min(image.height, y + cell_h)))

    def export_page(self, page: int, out_path: Path) -> None:
        image = self.page_image(page)
        if image is None:
            raise ValueError('Font texture page is unavailable')
        image.save(out_path)

    def export_page_tm2(self, page: int, out_path: Path) -> None:
        if not (0 <= page < len(self.pages)):
            raise ValueError('Font texture page is unavailable')
        picture = self.pages[page]
        out_path.write_bytes(tm2_core.read_container_bytes(picture.container))

    def replace_page_tm2(self, page: int, replacement_path: Path) -> None:
        if not (0 <= page < len(self.pages)):
            raise ValueError('Font texture page is unavailable')
        picture = self.pages[page]
        patches = tm2_core.build_tm2_picture_replacement_patches(picture, replacement_path)
        if patches:
            tm2_core.apply_patches_transactional(patches, create_backup=False)
        raw = self.work_path.read_bytes()
        AfsArchive(self.afs_path).replace_entry(self.entry_index, raw)
        self.refresh()

    def replace_page(self, page: int, replacement: Image.Image) -> None:
        if not (0 <= page < len(self.pages)):
            raise ValueError('Font texture page is unavailable')
        picture = self.pages[page]
        patches = tm2_core.build_replacement_patches(picture, replacement)
        if patches:
            tm2_core.apply_patches_transactional(patches, create_backup=False)
        # The edited UNK remains the same size. Repack it into SYSTEM*.AFS;
        # AfsArchive creates a .bak once and verifies the rebuilt archive.
        raw = self.work_path.read_bytes()
        AfsArchive(self.afs_path).replace_entry(self.entry_index, raw)
        self.refresh()


def _entry_font_score(index: int, pics: list) -> int:
    p512 = [p for p in pics if p.safe_to_decode and p.width == 512 and p.height == 512]
    if not p512:
        return -1
    score = len(p512) * 4
    if index == 1:
        score += 20
    if len(p512) >= 4:
        score += 20
    return score


def find_font_banks(root: Path, work_root: Path, lang: str | None = None) -> list[FontTextureBank]:
    root = Path(root)
    work_root = Path(work_root)
    work_root.mkdir(parents=True, exist_ok=True)
    banks: list[FontTextureBank] = []
    candidates = [
        p for p in root.rglob('*')
        if p.is_file() and re.fullmatch(r'system\d*\.afs', p.name, re.I)
    ]
    if lang:
        wanted = lang.upper()
        filtered = [p for p in candidates if system_lang(p) == wanted]
        if filtered:
            candidates = filtered
    for afs_path in sorted(candidates, key=lambda p: p.name.lower()):
        try:
            afs = AfsArchive(afs_path)
        except Exception:
            continue
        best = None
        # Retail RECVX places the message-font TM2 bank in AFS entry 1.
        # Try that one first; scan a few neighboring entries only as fallback.
        order = [1] if afs.count > 1 else [0]
        order += [i for i in range(min(6, afs.count)) if i not in order]
        for pos, idx in enumerate(order):
            try:
                raw = afs.entry_bytes(idx)
            except Exception:
                continue
            if b'TIM2' not in raw:
                continue
            work = work_root / f'{afs_path.stem}_{idx:05d}.unk'
            work.write_bytes(raw)
            try:
                containers = tm2_core.scan_file(work)
            except Exception:
                continue
            pics = [pic for c in containers for pic in c.pictures]
            score = _entry_font_score(idx, pics)
            if score >= 0 and (best is None or score > best[0]):
                best = (score, idx, work, pics)
            # Entry 1 with several 512x512 pages is the confirmed font bank;
            # stop immediately instead of scanning unrelated TM2 assets.
            if idx == 1 and score >= 36:
                break
        if best is None:
            continue
        _score, idx, work, pics = best
        p512 = [p for p in pics if p.safe_to_decode and p.width == 512 and p.height == 512][:8]
        try:
            images = [tm2_core.decode_picture(p).convert('RGBA') for p in p512]
        except Exception:
            continue
        if not images:
            continue
        banks.append(FontTextureBank(
            lang=system_lang(afs_path),
            afs_path=afs_path,
            entry_index=idx,
            entry_name=afs.entry_name(idx) or f'{idx:05d}.UNK',
            work_path=work,
            pages=p512,
            images=images,
        ))
    return banks
