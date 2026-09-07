from __future__ import annotations

import dataclasses
import mmap
import os
import shutil
import struct
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional, Sequence

from PIL import Image

TIM2_MAGIC = b"TIM2"
FILE_HEADER_SIZE = 16
PICTURE_HEADER_MIN_SIZE = 48
SUPPORTED_VERSIONS = {3, 4}
SUPPORTED_FORMAT_IDS = {0, 1}
SUPPORTED_IMAGE_TYPES = {1, 2, 3, 4, 5}
MAX_PICTURES = 1024
MAX_DIMENSION = 16384
SCAN_PROGRESS_STEP = 64 * 1024 * 1024


class TM2Error(Exception):
    pass


class TM2ValidationError(TM2Error):
    pass


class TM2UnsupportedError(TM2Error):
    pass


@dataclasses.dataclass(slots=True)
class Patch:
    source_path: str
    offset: int
    data: bytes
    expected: bytes
    description: str = ""

    @property
    def end(self) -> int:
        return self.offset + len(self.data)


@dataclasses.dataclass(slots=True)
class TM2Picture:
    container: "TM2Container"
    index: int
    header_offset: int
    total_size: int
    clut_size: int
    image_size: int
    header_size: int
    clut_colors: int
    picture_format: int
    mipmap_count: int
    clut_type: int
    image_type: int
    width: int
    height: int
    gs_tex0: int
    gs_tex1: int
    gs_regs: int
    gs_tex_clut: int
    image_offset: int
    clut_offset: int
    end_offset: int

    @property
    def source_path(self) -> str:
        return self.container.source_path

    @property
    def absolute_header_offset(self) -> int:
        return self.container.offset + self.header_offset

    @property
    def absolute_image_offset(self) -> int:
        return self.container.offset + self.image_offset

    @property
    def absolute_clut_offset(self) -> int:
        return self.container.offset + self.clut_offset

    @property
    def base_image_type(self) -> int:
        return self.image_type & 0x3F

    @property
    def image_type_flags(self) -> int:
        return self.image_type & 0xC0

    @property
    def base_clut_type(self) -> int:
        return self.clut_type & 0x3F

    @property
    def clut_type_flags(self) -> int:
        return self.clut_type & 0xC0

    @property
    def bpp(self) -> int:
        return {1: 16, 2: 32, 3: 32, 4: 4, 5: 8}.get(self.base_image_type, 0)

    @property
    def image_type_name(self) -> str:
        return {
            1: "RGBA16",
            2: "RGB32",
            3: "RGBA32",
            4: "Indexed 4-bit",
            5: "Indexed 8-bit",
        }.get(self.base_image_type, f"Type {self.base_image_type}")

    @property
    def indexed(self) -> bool:
        return self.base_image_type in (4, 5)

    @property
    def expected_palette_colors(self) -> int:
        return 16 if self.base_image_type == 4 else 256 if self.base_image_type == 5 else 0

    @property
    def clut_entry_size(self) -> int:
        if self.base_clut_type == 1:
            return 2
        if self.base_clut_type in (2, 3):
            return 4
        return 0

    @property
    def palette_span(self) -> int:
        if not self.indexed or self.clut_colors <= 0:
            return 0
        return self.clut_colors * self.clut_entry_size

    @property
    def palette_count(self) -> int:
        span = self.palette_span
        if span <= 0:
            return 0
        return max(1, self.clut_size // span)

    @property
    def base_image_size(self) -> int:
        pixels = self.width * self.height
        image_type = self.base_image_type
        if image_type == 1:
            return pixels * 2
        if image_type in (2, 3):
            return pixels * 4
        if image_type == 4:
            return (pixels + 1) // 2
        if image_type == 5:
            return pixels
        return 0

    @property
    def safe_to_decode(self) -> bool:
        if self.base_image_type not in SUPPORTED_IMAGE_TYPES:
            return False
        if self.image_type_flags:
            return False
        if self.base_image_size <= 0 or self.base_image_size > self.image_size:
            return False
        if self.indexed:
            if self.clut_entry_size not in (2, 4):
                return False
            if self.palette_span <= 0 or self.palette_span > self.clut_size:
                return False
        return True

    @property
    def index_capacity(self) -> int:
        """Number of palette entries addressable by the base pixel stream.

        TIM2 files found in games occasionally declare a CLUT larger than the
        nominal 16/256 entries. The base 4/8-bit indices still cannot address
        beyond 16/256, so replacement edits only the addressable entries and
        preserves any extra CLUT banks byte-for-byte.
        """
        if not self.indexed:
            return 0
        return min(self.clut_colors, self.expected_palette_colors)

    @property
    def safe_to_replace(self) -> bool:
        # Replacement is intentionally tied to successful decoding. We patch
        # only the base image level and the addressable CLUT bytes, so mipmap
        # payloads, extra CLUT banks, headers and offsets stay untouched.
        if not self.safe_to_decode:
            return False
        if self.indexed and self.index_capacity <= 0:
            return False
        return True

    @property
    def replacement_mode(self) -> str:
        if not self.safe_to_replace:
            return "view-only"
        if not self.indexed:
            return "direct-base" if self.mipmap_count > 1 else "direct"
        mode = "indexed-full" if self.palette_count == 1 else "indexed-multi"
        return mode + ("+mipmaps" if self.mipmap_count > 1 else "")


@dataclasses.dataclass(slots=True)
class TM2Container:
    source_path: str
    offset: int
    source_size: int
    version: int
    format_id: int
    picture_count: int
    header_span: int
    declared_size: int
    pictures: list[TM2Picture]
    embedded_path: str = ""
    embedded_offset: int = 0

    @property
    def end_offset(self) -> int:
        return self.offset + self.declared_size

    @property
    def display_source(self) -> str:
        if self.embedded_path:
            return f"{os.path.basename(self.source_path)}::{self.embedded_path}"
        return self.source_path

    @property
    def display_offset(self) -> int:
        return self.embedded_offset if self.embedded_path else self.offset


def _unpack_from(fmt: str, buffer: mmap.mmap | bytes | bytearray | memoryview, offset: int):
    size = struct.calcsize(fmt)
    if offset < 0 or offset + size > len(buffer):
        raise TM2ValidationError("Unexpected end of file")
    return struct.unpack_from(fmt, buffer, offset)


def _expected_min_image_bytes(image_type: int, width: int, height: int) -> int:
    image_type &= 0x3F
    pixels = width * height
    if image_type == 1:
        return pixels * 2
    if image_type in (2, 3):
        return pixels * 4
    if image_type == 4:
        return (pixels + 1) // 2
    if image_type == 5:
        return pixels
    return 0


def parse_tm2_at(
    buffer: mmap.mmap | bytes | bytearray | memoryview,
    offset: int,
    source_path: str,
    source_size: Optional[int] = None,
) -> TM2Container:
    source_size = len(buffer) if source_size is None else source_size
    if offset < 0 or offset + FILE_HEADER_SIZE > len(buffer):
        raise TM2ValidationError("TIM2 header is truncated")
    if bytes(buffer[offset:offset + 4]) != TIM2_MAGIC:
        raise TM2ValidationError("TIM2 signature does not match")

    version = buffer[offset + 4]
    format_id = buffer[offset + 5]
    picture_count = _unpack_from("<H", buffer, offset + 6)[0]

    if version not in SUPPORTED_VERSIONS:
        raise TM2ValidationError(f"Unsupported TIM2 revision: {version}")
    if format_id not in SUPPORTED_FORMAT_IDS:
        raise TM2ValidationError(f"Unsupported TIM2 alignment mode: {format_id}")
    if not (1 <= picture_count <= MAX_PICTURES):
        raise TM2ValidationError(f"Invalid TIM2 picture count: {picture_count}")

    header_span = 128 if format_id == 1 else FILE_HEADER_SIZE
    if offset + header_span > len(buffer):
        raise TM2ValidationError("TIM2 aligned header is truncated")

    cursor = header_span
    pictures: list[TM2Picture] = []

    for picture_index in range(picture_count):
        absolute = offset + cursor
        if absolute + PICTURE_HEADER_MIN_SIZE > len(buffer):
            raise TM2ValidationError("Picture header is truncated")

        fields = _unpack_from("<IIIHHBBBBHHQQII", buffer, absolute)
        (
            total_size,
            clut_size,
            image_size,
            picture_header_size,
            clut_colors,
            picture_format,
            mipmap_count,
            clut_type,
            image_type,
            width,
            height,
            gs_tex0,
            gs_tex1,
            gs_regs,
            gs_tex_clut,
        ) = fields

        base_image_type = image_type & 0x3F
        if total_size < PICTURE_HEADER_MIN_SIZE:
            raise TM2ValidationError("Invalid picture total size")
        if picture_header_size < PICTURE_HEADER_MIN_SIZE or picture_header_size > total_size:
            raise TM2ValidationError("Invalid picture header size")
        if image_size <= 0:
            raise TM2ValidationError("Picture contains no image data")
        if not (1 <= width <= MAX_DIMENSION and 1 <= height <= MAX_DIMENSION):
            raise TM2ValidationError(f"Invalid texture dimensions: {width}x{height}")
        if base_image_type not in SUPPORTED_IMAGE_TYPES:
            raise TM2ValidationError(f"Unknown TIM2 image type: {image_type}")
        if not (1 <= mipmap_count <= 16):
            raise TM2ValidationError(f"Invalid mipmap count: {mipmap_count}")

        minimum_image = _expected_min_image_bytes(image_type, width, height)
        if minimum_image <= 0 or minimum_image > image_size:
            raise TM2ValidationError(
                f"Image data is too small for {width}x{height} type {image_type}"
            )
        if total_size < picture_header_size + image_size + clut_size:
            raise TM2ValidationError("Picture fields overlap")

        picture_end = cursor + total_size
        if offset + picture_end > len(buffer) or offset + picture_end > source_size:
            raise TM2ValidationError("Picture data extends beyond source file")

        if base_image_type in (4, 5):
            base_clut_type = clut_type & 0x3F
            entry_size = 2 if base_clut_type == 1 else 4 if base_clut_type in (2, 3) else 0
            if not entry_size:
                raise TM2ValidationError(f"Unsupported CLUT type: {clut_type}")
            if clut_colors <= 0 or clut_size < clut_colors * entry_size:
                raise TM2ValidationError("Indexed texture has an invalid CLUT")

        image_offset = cursor + picture_header_size
        clut_offset = image_offset + image_size
        picture = TM2Picture(
            container=None,  # type: ignore[arg-type]
            index=picture_index,
            header_offset=cursor,
            total_size=total_size,
            clut_size=clut_size,
            image_size=image_size,
            header_size=picture_header_size,
            clut_colors=clut_colors,
            picture_format=picture_format,
            mipmap_count=mipmap_count,
            clut_type=clut_type,
            image_type=image_type,
            width=width,
            height=height,
            gs_tex0=gs_tex0,
            gs_tex1=gs_tex1,
            gs_regs=gs_regs,
            gs_tex_clut=gs_tex_clut,
            image_offset=image_offset,
            clut_offset=clut_offset,
            end_offset=picture_end,
        )
        pictures.append(picture)
        cursor = picture_end

    container = TM2Container(
        source_path=os.path.abspath(source_path),
        offset=offset,
        source_size=source_size,
        version=version,
        format_id=format_id,
        picture_count=picture_count,
        header_span=header_span,
        declared_size=cursor,
        pictures=pictures,
    )
    for picture in pictures:
        picture.container = container
    return container




@dataclasses.dataclass(slots=True)
class ISOFileEntry:
    path: str
    extent: int
    size: int
    offset: int


ISO_SECTOR_SIZE = 2048
ISO_PVD_SECTOR = 16
ISO_STANDARD_ID = b"CD001"
MAX_ISO_ENTRIES = 500_000
MAX_ISO_DEPTH = 64


def is_iso9660(path: str | os.PathLike[str]) -> bool:
    """Return True when *path* contains a standard ISO9660 primary volume descriptor."""
    try:
        with open(path, "rb") as f:
            f.seek(ISO_PVD_SECTOR * ISO_SECTOR_SIZE)
            head = f.read(7)
        return len(head) == 7 and head[0] == 1 and head[1:6] == ISO_STANDARD_ID
    except OSError:
        return False


def _iso_name(raw: bytes) -> str:
    if raw in (b"\x00", b"\x01"):
        return ""
    name = raw.decode("ascii", errors="replace")
    if ";" in name:
        name = name.split(";", 1)[0]
    if name.endswith("."):
        name = name[:-1]
    return name


def list_iso_files(path: str | os.PathLike[str]) -> list[ISOFileEntry]:
    """Read the ISO9660 directory tree without extracting the image.

    PS2 discs normally use ISO9660. We deliberately use only the primary
    descriptor and fixed extents so replacements can be written in-place without
    moving any file or changing the filesystem layout.
    """
    path = os.path.abspath(os.fspath(path))
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(ISO_PVD_SECTOR * ISO_SECTOR_SIZE)
        pvd = f.read(ISO_SECTOR_SIZE)
        if len(pvd) != ISO_SECTOR_SIZE or pvd[0] != 1 or pvd[1:6] != ISO_STANDARD_ID:
            raise TM2ValidationError("The selected image is not a supported ISO9660 disc image")
        block_size = struct.unpack_from("<H", pvd, 128)[0]
        if block_size <= 0 or block_size > 65536:
            raise TM2ValidationError("Invalid ISO9660 logical block size")
        root_len = pvd[156]
        if root_len < 34 or 156 + root_len > len(pvd):
            raise TM2ValidationError("ISO9660 root directory record is invalid")
        root = pvd[156:156 + root_len]
        root_extent = struct.unpack_from("<I", root, 2)[0]
        root_size = struct.unpack_from("<I", root, 10)[0]

        result: list[ISOFileEntry] = []
        visited_dirs: set[tuple[int, int]] = set()

        def walk(extent: int, data_size: int, prefix: str, depth: int) -> None:
            if depth > MAX_ISO_DEPTH:
                raise TM2ValidationError("ISO directory nesting is too deep")
            key = (extent, data_size)
            if key in visited_dirs:
                return
            visited_dirs.add(key)
            start = extent * block_size
            end = start + data_size
            if start < 0 or data_size < 0 or end > size:
                raise TM2ValidationError("ISO directory extent exceeds the disc image")
            f.seek(start)
            data = f.read(data_size)
            pos = 0
            while pos < len(data):
                record_len = data[pos]
                if record_len == 0:
                    pos = ((pos // block_size) + 1) * block_size
                    continue
                if record_len < 34 or pos + record_len > len(data):
                    raise TM2ValidationError("Malformed ISO9660 directory record")
                rec = data[pos:pos + record_len]
                name_len = rec[32]
                if 33 + name_len > len(rec):
                    raise TM2ValidationError("Malformed ISO9660 file identifier")
                raw_name = rec[33:33 + name_len]
                name = _iso_name(raw_name)
                child_extent = struct.unpack_from("<I", rec, 2)[0]
                child_size = struct.unpack_from("<I", rec, 10)[0]
                flags = rec[25]
                pos += record_len
                if not name:
                    continue
                child_path = f"{prefix}/{name}" if prefix else f"/{name}"
                child_offset = child_extent * block_size
                if child_offset < 0 or child_size < 0 or child_offset + child_size > size:
                    continue
                if flags & 0x02:
                    walk(child_extent, child_size, child_path, depth + 1)
                else:
                    # Multi-extent files are uncommon on PS2 discs. Skipping a
                    # continuation extent is safer than treating disjoint extents
                    # as one writable file.
                    if flags & 0x80:
                        continue
                    result.append(ISOFileEntry(child_path, child_extent, child_size, child_offset))
                    if len(result) > MAX_ISO_ENTRIES:
                        raise TM2ValidationError("ISO contains too many directory entries")

        walk(root_extent, root_size, "", 0)
        return result


def scan_iso(
    path: str | os.PathLike[str],
    cancel: Optional[Callable[[], bool]] = None,
    progress: Optional[Callable[[int, int, str, int, int], None]] = None,
    entry_filter: Optional[Callable[[ISOFileEntry], bool]] = None,
    found_callback: Optional[Callable[[TM2Container], None]] = None,
) -> list[TM2Container]:
    """Scan every ISO9660 file extent for valid TIM2 containers.

    Returned container offsets are absolute positions in the ISO, which means
    the existing fixed-size patch writer can replace texture bytes directly in
    the disc image while preserving the ISO filesystem and every file extent.
    """
    path = os.path.abspath(os.fspath(path))
    physical_size = os.path.getsize(path)
    entries = list_iso_files(path)
    if entry_filter is not None:
        entries = [e for e in entries if entry_filter(e)]
    total = sum(e.size for e in entries) or 1
    done_total = 0
    found: list[TM2Container] = []

    with open(path, "rb") as stream:
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            for index, entry in enumerate(entries, 1):
                if cancel and cancel():
                    raise InterruptedError("Scan cancelled")
                start = entry.offset
                end = entry.offset + entry.size
                cursor = start
                while cursor <= end - 4:
                    if cancel and cancel():
                        raise InterruptedError("Scan cancelled")
                    position = mm.find(TIM2_MAGIC, cursor, end)
                    if position < 0:
                        break
                    try:
                        container = parse_tm2_at(mm, position, path, physical_size)
                    except TM2ValidationError:
                        cursor = position + 1
                    else:
                        if container.end_offset <= end:
                            container.embedded_path = entry.path
                            container.embedded_offset = position - start
                            found.append(container)
                            if found_callback:
                                found_callback(container)
                            cursor = max(position + 4, container.end_offset)
                        else:
                            cursor = position + 1
                done_total += entry.size
                if progress:
                    progress(done_total, total, entry.path, index, len(entries))
    return found

def scan_file(
    path: str | os.PathLike[str],
    cancel: Optional[Callable[[], bool]] = None,
    progress: Optional[Callable[[int, int], None]] = None,
    found_callback: Optional[Callable[[TM2Container], None]] = None,
    start_offset: int = 0,
    end_offset: int | None = None,
) -> list[TM2Container]:
    path = os.path.abspath(os.fspath(path))
    file_size = os.path.getsize(path)
    start_offset = max(0, min(int(start_offset or 0), file_size))
    end_offset = file_size if end_offset is None else max(start_offset, min(int(end_offset), file_size))
    if end_offset - start_offset < FILE_HEADER_SIZE:
        if progress:
            progress(file_size, file_size)
        return []

    found: list[TM2Container] = []
    with open(path, "rb") as stream:
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            cursor = start_offset
            last_progress = start_offset
            while cursor <= end_offset - 4:
                if cancel and cancel():
                    raise InterruptedError("Scan cancelled")
                position = mm.find(TIM2_MAGIC, cursor, end_offset)
                if position < 0:
                    break
                try:
                    container = parse_tm2_at(mm, position, path, file_size)
                except TM2ValidationError:
                    cursor = position + 1
                else:
                    if container.end_offset <= end_offset:
                        found.append(container)
                        if found_callback:
                            found_callback(container)
                        cursor = max(position + 4, position + container.declared_size)
                    else:
                        cursor = position + 1

                if progress and cursor - last_progress >= SCAN_PROGRESS_STEP:
                    last_progress = cursor
                    progress(min(cursor - start_offset, end_offset - start_offset), end_offset - start_offset)
            if progress:
                progress(end_offset - start_offset, end_offset - start_offset)
    return found


def iter_files(paths: Iterable[str | os.PathLike[str]]) -> Iterator[str]:
    seen: set[str] = set()
    for raw in paths:
        path = os.path.abspath(os.fspath(raw))
        if path in seen:
            continue
        seen.add(path)
        if os.path.isfile(path):
            yield path
            continue
        if not os.path.isdir(path):
            continue
        for root, dirs, files in os.walk(path, followlinks=False):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
            for name in files:
                candidate = os.path.join(root, name)
                if os.path.isfile(candidate) and not os.path.islink(candidate):
                    yield candidate


def _read_range(path: str, offset: int, size: int) -> bytes:
    if size < 0 or offset < 0:
        raise TM2ValidationError("Invalid read range")
    with open(path, "rb") as stream:
        stream.seek(offset)
        data = stream.read(size)
    if len(data) != size:
        raise TM2ValidationError("Source file changed or became truncated")
    return data


def _read_range_overlay(path: str, offset: int, size: int, patches: Sequence[Patch] = ()) -> bytes:
    data = bytearray(_read_range(path, offset, size))
    end = offset + size
    canonical = os.path.abspath(path)
    for patch in patches:
        if os.path.abspath(patch.source_path) != canonical:
            continue
        left = max(offset, patch.offset)
        right = min(end, patch.end)
        if left >= right:
            continue
        data[left - offset:right - offset] = patch.data[left - patch.offset:right - patch.offset]
    return bytes(data)


def read_container_bytes(container: TM2Container, patches: Sequence[Patch] = ()) -> bytes:
    return _read_range_overlay(
        container.source_path, container.offset, container.declared_size, patches
    )


def _container_layout_signature(container: TM2Container) -> tuple:
    picture_signatures = []
    for picture in container.pictures:
        picture_signatures.append((
            picture.header_offset,
            picture.total_size,
            picture.clut_size,
            picture.image_size,
            picture.header_size,
            picture.clut_colors,
            picture.picture_format,
            picture.mipmap_count,
            picture.clut_type,
            picture.image_type,
            picture.width,
            picture.height,
            picture.gs_tex0,
            picture.gs_tex1,
            picture.gs_regs,
            picture.gs_tex_clut,
            picture.image_offset,
            picture.clut_offset,
            picture.end_offset,
        ))
    return (
        container.version,
        container.format_id,
        container.picture_count,
        container.header_span,
        container.declared_size,
        tuple(picture_signatures),
    )


def build_tm2_container_replacement_patches(
    target: TM2Container,
    replacement_path: str | os.PathLike[str],
) -> list[Patch]:
    """Use another TM2 as a fixed-layout donor without replacing target headers.

    The donor must contain exactly one valid TM2 container with the same complete
    structural layout. Only each picture's data area after its header is copied.
    This preserves the target TIM2 header, picture headers, offsets, and file size.
    """
    replacement_path = os.path.abspath(os.fspath(replacement_path))
    donors = scan_file(replacement_path)
    if len(donors) != 1:
        raise TM2ValidationError(
            "Replacement file must contain exactly one valid TM2 container"
        )
    donor = donors[0]
    if _container_layout_signature(target) != _container_layout_signature(donor):
        raise TM2ValidationError(
            "Replacement TM2 structure does not exactly match the selected TM2"
        )

    patches: list[Patch] = []
    for target_picture, donor_picture in zip(target.pictures, donor.pictures):
        target_offset = target_picture.absolute_image_offset
        donor_offset = donor_picture.absolute_image_offset
        data_size = target_picture.end_offset - target_picture.image_offset
        if data_size <= 0 or data_size != donor_picture.end_offset - donor_picture.image_offset:
            raise TM2ValidationError("Replacement TM2 picture data size changed")
        replacement_data = _read_range(donor.source_path, donor_offset, data_size)
        original_data = _read_range(target.source_path, target_offset, data_size)
        patches.append(Patch(
            target.source_path,
            target_offset,
            replacement_data,
            original_data,
            f"TM2 container picture {target_picture.index} data",
        ))

    for patch in patches:
        if len(patch.data) != len(patch.expected):
            raise TM2ValidationError("Replacement TM2 patch changed length")
        if patch.offset < target.offset or patch.end > target.end_offset:
            raise TM2ValidationError("Replacement TM2 patch exceeds the selected container")
    return patches


def _decode_16(value: int) -> tuple[int, int, int, int]:
    r5 = value & 0x1F
    g5 = (value >> 5) & 0x1F
    b5 = (value >> 10) & 0x1F
    a1 = (value >> 15) & 1
    r = (r5 << 3) | (r5 >> 2)
    g = (g5 << 3) | (g5 >> 2)
    b = (b5 << 3) | (b5 >> 2)
    return r, g, b, 255 if a1 else 0


def _encode_16(r: int, g: int, b: int, a: int) -> int:
    return (
        ((r >> 3) & 0x1F)
        | (((g >> 3) & 0x1F) << 5)
        | (((b >> 3) & 0x1F) << 10)
        | ((1 if a >= 128 else 0) << 15)
    )


def _alpha_storage_max(raw: bytes) -> int:
    """Detect the alpha convention for direct RGBA32 pixel payloads."""
    if not raw:
        return 128
    alphas = raw[3::4]
    return 255 if alphas and max(alphas) > 128 else 128


def _palette_alpha_storage_max(
    raw: bytes, picture: "TM2Picture", indices: bytes | bytearray | memoryview | None = None
) -> int:
    """Detect 0x80-vs-0xFF alpha using palette entries the image actually uses.

    Some real PS2 TIM2 files contain unused CLUT entries whose alpha is 0xFF
    while every referenced color uses the GS-style 0..0x80 convention. Looking
    at the whole CLUT therefore makes an opaque texture appear 50% transparent
    in the PC preview.  CSM1 palette ordering is accounted for before matching
    texel indices to CLUT alpha values.
    """
    if picture.base_clut_type != 3 or not raw:
        return 255
    count = min(picture.clut_colors, len(raw) // 4)
    alphas = [raw[i * 4 + 3] for i in range(count)]
    if count >= 32 and _clut_is_csm1(picture):
        alphas = _palette_twiddle(alphas)
    if indices is not None:
        used = sorted({int(index) for index in indices if 0 <= int(index) < len(alphas)})
        selected = [alphas[index] for index in used]
        if selected:
            alphas = selected
    return 255 if alphas and max(alphas) > 128 else 128


def detect_picture_alpha_mode(
    picture: "TM2Picture", palette_index: int = 0, patches: Sequence[Patch] = ()
) -> tuple[int, int]:
    """Return ``(storage_max, used_palette_entries)`` for diagnostics/UI."""
    if picture.base_image_type == 3:
        raw = _read_range_overlay(
            picture.source_path, picture.absolute_image_offset, picture.base_image_size, patches
        )
        return _alpha_storage_max(raw), picture.width * picture.height
    if picture.indexed and picture.base_clut_type == 3:
        raw_image = _read_range_overlay(
            picture.source_path, picture.absolute_image_offset, picture.base_image_size, patches
        )
        indices = _unpack_indices(raw_image, picture)
        palette_index = max(0, min(palette_index, picture.palette_count - 1))
        span = picture.palette_span
        raw_palette = _read_range_overlay(
            picture.source_path, picture.absolute_clut_offset + palette_index * span, span, patches
        )
        return _palette_alpha_storage_max(raw_palette, picture, indices), len(set(indices))
    return 255, 0


def _alpha_stored_to_rgba(alpha: int, storage_max: int) -> int:
    storage_max = 255 if storage_max > 128 else 128
    return max(0, min(255, int(round(alpha * 255 / storage_max))))


def _alpha_rgba_to_stored(alpha: int, storage_max: int) -> int:
    storage_max = 255 if storage_max > 128 else 128
    return max(0, min(storage_max, int(round(alpha * storage_max / 255))))


def _palette_twiddle(colors: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    # CSM1 swaps 8-color banks inside every group of 32 entries.
    if len(colors) < 32:
        return list(colors)
    result: list[tuple[int, int, int, int]] = []
    for start in range(0, len(colors), 32):
        block = colors[start:start + 32]
        if len(block) == 32:
            result.extend(block[0:8])
            result.extend(block[16:24])
            result.extend(block[8:16])
            result.extend(block[24:32])
        else:
            result.extend(block)
    return result


def _clut_is_csm1(picture: TM2Picture) -> bool:
    return ((picture.gs_tex0 >> 55) & 1) == 0


def _decode_palette(
    raw: bytes, picture: TM2Picture, alpha_storage_max: int | None = None
) -> list[tuple[int, int, int, int]]:
    count = picture.clut_colors
    entry_size = picture.clut_entry_size
    if len(raw) < count * entry_size:
        raise TM2ValidationError("Palette is truncated")
    colors: list[tuple[int, int, int, int]] = []
    if picture.base_clut_type == 1:
        for i in range(count):
            colors.append(_decode_16(struct.unpack_from("<H", raw, i * 2)[0]))
    elif picture.base_clut_type == 2:
        for i in range(count):
            r, g, b, _unused = raw[i * 4:i * 4 + 4]
            colors.append((r, g, b, 255))
    elif picture.base_clut_type == 3:
        alpha_max = alpha_storage_max or _palette_alpha_storage_max(raw[:count * 4], picture)
        for i in range(count):
            r, g, b, a = raw[i * 4:i * 4 + 4]
            colors.append((r, g, b, _alpha_stored_to_rgba(a, alpha_max)))
    else:
        raise TM2UnsupportedError(f"Unsupported CLUT type: {picture.clut_type}")
    if count >= 32 and _clut_is_csm1(picture):
        colors = _palette_twiddle(colors)
    return colors


def _encode_palette(
    colors: list[tuple[int, int, int, int]],
    picture: TM2Picture,
    original_raw: bytes,
    alpha_storage_max: int | None = None,
) -> bytes:
    if len(colors) != picture.clut_colors:
        raise TM2ValidationError("Palette color count changed")
    disk_colors = _palette_twiddle(colors) if (
        len(colors) >= 32 and _clut_is_csm1(picture)
    ) else list(colors)
    out = bytearray()
    if picture.base_clut_type == 1:
        for r, g, b, a in disk_colors:
            out += struct.pack("<H", _encode_16(r, g, b, a))
    elif picture.base_clut_type == 2:
        for i, (r, g, b, _a) in enumerate(disk_colors):
            unused = original_raw[i * 4 + 3] if i * 4 + 3 < len(original_raw) else 0
            out += bytes((r, g, b, unused))
    elif picture.base_clut_type == 3:
        alpha_max = alpha_storage_max or _palette_alpha_storage_max(
            original_raw[:picture.clut_colors * 4], picture
        )
        for r, g, b, a in disk_colors:
            out += bytes((r, g, b, _alpha_rgba_to_stored(a, alpha_max)))
    else:
        raise TM2UnsupportedError(f"Unsupported CLUT type: {picture.clut_type}")
    if len(out) != picture.palette_span:
        raise TM2ValidationError("Encoded palette size changed")
    return bytes(out)


def _unpack_indices(raw_image: bytes, picture: TM2Picture) -> bytes:
    pixel_count = picture.width * picture.height
    if picture.base_image_type == 5:
        return raw_image[:pixel_count]
    if picture.base_image_type == 4:
        indices = bytearray(pixel_count)
        out = 0
        for value in raw_image:
            if out < pixel_count:
                indices[out] = value & 0x0F
                out += 1
            if out < pixel_count:
                indices[out] = (value >> 4) & 0x0F
                out += 1
        return bytes(indices)
    raise TM2UnsupportedError("Not an indexed texture")


def _pack_indices(indices: bytes, picture: TM2Picture) -> bytes:
    if picture.base_image_type == 5:
        return bytes(indices)
    if picture.base_image_type == 4:
        out = bytearray()
        for i in range(0, len(indices), 2):
            low = indices[i] & 0x0F
            high = indices[i + 1] & 0x0F if i + 1 < len(indices) else 0
            out.append(low | (high << 4))
        return bytes(out)
    raise TM2UnsupportedError("Not an indexed texture")


def decode_picture(
    picture: TM2Picture,
    palette_index: int = 0,
    patches: Sequence[Patch] = (),
) -> Image.Image:
    if not picture.safe_to_decode:
        raise TM2UnsupportedError("This TIM2 variant cannot be decoded")
    raw_image = _read_range_overlay(
        picture.source_path,
        picture.absolute_image_offset,
        picture.base_image_size,
        patches,
    )
    width, height = picture.width, picture.height
    pixel_count = width * height
    image_type = picture.base_image_type

    if image_type == 1:
        pixels = [
            _decode_16(struct.unpack_from("<H", raw_image, i * 2)[0])
            for i in range(pixel_count)
        ]
    elif image_type == 2:
        pixels = [
            (raw_image[i], raw_image[i + 1], raw_image[i + 2], 255)
            for i in range(0, pixel_count * 4, 4)
        ]
    elif image_type == 3:
        alpha_max = _alpha_storage_max(raw_image)
        pixels = [
            (
                raw_image[i], raw_image[i + 1], raw_image[i + 2],
                _alpha_stored_to_rgba(raw_image[i + 3], alpha_max),
            )
            for i in range(0, pixel_count * 4, 4)
        ]
    elif image_type in (4, 5):
        palette_index = max(0, min(palette_index, picture.palette_count - 1))
        span = picture.palette_span
        palette_raw = _read_range_overlay(
            picture.source_path,
            picture.absolute_clut_offset + palette_index * span,
            span,
            patches,
        )
        indices = _unpack_indices(raw_image, picture)
        alpha_max = _palette_alpha_storage_max(palette_raw, picture, indices)
        palette = _decode_palette(palette_raw, picture, alpha_max)
        pixels = [
            palette[index] if index < len(palette) else (255, 0, 255, 255)
            for index in indices
        ]
    else:
        raise TM2UnsupportedError(f"Unsupported image type: {picture.image_type}")

    image = Image.new("RGBA", (width, height))
    image.putdata(pixels)
    return image


def _quantize_rgba(
    image: Image.Image, color_count: int
) -> tuple[bytes, list[tuple[int, int, int, int]]]:
    rgba = image.convert("RGBA")
    pixels = list(rgba.getdata())

    # Preserve exact RGBA colors whenever the edited image already fits the
    # original palette capacity. This avoids needless quality loss and keeps
    # transparent colors distinct from opaque black.
    exact_map: dict[tuple[int, int, int, int], int] = {}
    exact_indices = bytearray()
    overflow = False
    for color in pixels:
        index = exact_map.get(color)
        if index is None:
            if len(exact_map) >= color_count:
                overflow = True
                break
            index = len(exact_map)
            exact_map[color] = index
        exact_indices.append(index)
    if not overflow:
        colors = list(exact_map.keys())
        colors.extend([(0, 0, 0, 0)] * (color_count - len(colors)))
        return bytes(exact_indices), colors

    quantized = rgba.quantize(
        colors=color_count,
        method=Image.Quantize.FASTOCTREE,
        dither=Image.Dither.NONE,
    )
    indices = bytes(quantized.getdata())
    rgb_palette = quantized.getpalette() or []
    rgb_palette.extend([0] * max(0, color_count * 3 - len(rgb_palette)))

    alpha_sum = [0] * color_count
    alpha_count = [0] * color_count
    for index, alpha in zip(indices, rgba.getchannel("A").getdata()):
        if index < color_count:
            alpha_sum[index] += int(alpha)
            alpha_count[index] += 1

    colors: list[tuple[int, int, int, int]] = []
    for i in range(color_count):
        r = rgb_palette[i * 3]
        g = rgb_palette[i * 3 + 1]
        b = rgb_palette[i * 3 + 2]
        a = int(round(alpha_sum[i] / alpha_count[i])) if alpha_count[i] else 0
        colors.append((r, g, b, a))
    return indices, colors


def _normalize_clut_color(
    color: tuple[int, int, int, int], picture: TM2Picture
) -> tuple[int, int, int, int]:
    """Return the exact RGBA color representable by this picture's CLUT."""
    r, g, b, a = color
    if picture.base_clut_type == 1:
        return _decode_16(_encode_16(r, g, b, a))
    if picture.base_clut_type == 2:
        return (r, g, b, 255)
    if picture.base_clut_type == 3:
        # Keep the desired visual alpha here. _encode_palette() converts it
        # using the alpha convention detected from the target palette bytes.
        return (r, g, b, max(0, min(255, a)))
    raise TM2UnsupportedError(f"Unsupported CLUT type: {picture.clut_type}")


def _build_multi_palette_data(
    image: Image.Image,
    old_indices: bytes,
    palettes: list[list[tuple[int, int, int, int]]],
    picture: TM2Picture,
    selected_palette: int,
) -> tuple[bytes, list[list[tuple[int, int, int, int]]]]:
    capacity = picture.index_capacity
    if capacity <= 0:
        raise TM2UnsupportedError("Indexed texture has no addressable CLUT entries")
    quantized_indices, quantized_palette = _quantize_rgba(image, capacity)
    quantized_palette = [
        _normalize_clut_color(color, picture) for color in quantized_palette
    ]
    desired_pixels = [
        quantized_palette[index] for index in quantized_indices
    ]

    other_palette_ids = [
        palette_id for palette_id in range(len(palettes))
        if palette_id != selected_palette
    ]
    vectors = [
        tuple(palettes[palette_id][index] for palette_id in other_palette_ids)
        for index in range(capacity)
    ]
    pixel_pairs = [
        (vectors[old_index], desired_color)
        for old_index, desired_color in zip(old_indices, desired_pixels)
    ]
    pair_counts = Counter(pixel_pairs)

    if len(pair_counts) <= capacity:
        available = set(range(capacity))
        pair_to_index: dict[
            tuple[tuple[tuple[int, int, int, int], ...], tuple[int, int, int, int]], int
        ] = {}
        selected_colors = palettes[selected_palette]

        for group, color in sorted(
            pair_counts, key=lambda pair: pair_counts[pair], reverse=True
        ):
            exact = [
                index for index in available
                if vectors[index] == group and selected_colors[index] == color
            ]
            same_group = [
                index for index in available if vectors[index] == group
            ]
            if exact:
                chosen = min(exact)
            elif same_group:
                chosen = min(same_group)
            else:
                chosen = min(available)
            available.remove(chosen)
            pair_to_index[(group, color)] = chosen

        new_indices = bytes(pair_to_index[pair] for pair in pixel_pairs)
        rebuilt_palettes = [list(colors) for colors in palettes]
        for (group, selected_color), index in pair_to_index.items():
            rebuilt_palettes[selected_palette][index] = selected_color
            for vector_position, palette_id in enumerate(other_palette_ids):
                rebuilt_palettes[palette_id][index] = group[vector_position]
        return new_indices, rebuilt_palettes

    color_to_index: dict[tuple[int, int, int, int], int] = {}
    selected_colors: list[tuple[int, int, int, int]] = []
    remapped_indices = bytearray()
    for color in desired_pixels:
        index = color_to_index.get(color)
        if index is None:
            index = len(selected_colors)
            color_to_index[color] = index
            selected_colors.append(color)
        remapped_indices.append(index)

    rebuilt_palettes = [list(colors) for colors in palettes]
    for index, color in enumerate(selected_colors):
        rebuilt_palettes[selected_palette][index] = color

    for palette_id in other_palette_ids:
        buckets = [Counter() for _ in range(len(selected_colors))]
        old_palette = palettes[palette_id]
        for old_index, new_index in zip(old_indices, remapped_indices):
            buckets[new_index][old_palette[old_index]] += 1
        for index, bucket in enumerate(buckets):
            if bucket:
                rebuilt_palettes[palette_id][index] = bucket.most_common(1)[0][0]

    return bytes(remapped_indices), rebuilt_palettes

def build_replacement_patches(
    picture: TM2Picture,
    source_image: Image.Image,
    palette_index: int = 0,
) -> list[Patch]:
    if not picture.safe_to_replace:
        raise TM2UnsupportedError(
            "This texture cannot be replaced because it cannot be decoded safely."
        )

    # NEAREST is intentional: it never invents blended colors before CLUT
    # generation. This matches the color-safe behavior of the TIM tool and is
    # especially important for 4-bit/8-bit indexed textures.
    source_rgba = source_image.convert("RGBA")
    image = source_rgba if source_rgba.size == (picture.width, picture.height) else source_rgba.resize(
        (picture.width, picture.height), Image.Resampling.NEAREST
    )
    # A PNG exported by this tool and imported again unchanged must be a true
    # no-op. This prevents palette reordering or alpha/channel drift during a
    # simple extract/repack cycle.
    current_visual = decode_picture(picture, palette_index)
    if current_visual.tobytes() == image.tobytes():
        return []

    original_image = _read_range(
        picture.source_path, picture.absolute_image_offset, picture.base_image_size
    )
    patches: list[Patch] = []
    image_type = picture.base_image_type

    if image_type in (4, 5):
        palette_index = max(0, min(palette_index, picture.palette_count - 1))
        palette_offset = picture.absolute_clut_offset + palette_index * picture.palette_span
        original_palette = _read_range(
            picture.source_path, palette_offset, picture.palette_span
        )

        if picture.palette_count == 1:
            capacity = picture.index_capacity
            indices, colors = _quantize_rgba(image, capacity)
            encoded_image = _pack_indices(indices, picture)
            if len(encoded_image) != picture.base_image_size:
                raise TM2ValidationError("Encoded pixel data size changed")
            patches.append(Patch(
                picture.source_path,
                picture.absolute_image_offset,
                encoded_image,
                original_image,
                f"TM2 picture {picture.index} pixel indices",
            ))
            # Preserve non-addressable extra CLUT entries when a game declares
            # a larger-than-standard palette bank. Detect alpha from CLUT entries
            # referenced by the original texels, not from unused padding colors.
            old_indices = _unpack_indices(original_image, picture)
            alpha_max = _palette_alpha_storage_max(original_palette, picture, old_indices)
            full_colors = _decode_palette(original_palette, picture, alpha_max)
            full_colors[:capacity] = colors
            encoded_palette = _encode_palette(
                full_colors, picture, original_palette, alpha_max
            )
            patches.append(Patch(
                picture.source_path,
                palette_offset,
                encoded_palette,
                original_palette,
                f"TM2 picture {picture.index} palette {palette_index}",
            ))
        else:
            palette_raw_values = [
                _read_range(
                    picture.source_path,
                    picture.absolute_clut_offset + palette_id * picture.palette_span,
                    picture.palette_span,
                )
                for palette_id in range(picture.palette_count)
            ]
            old_indices = _unpack_indices(original_image, picture)
            palette_alpha_modes = [
                _palette_alpha_storage_max(raw_palette, picture, old_indices)
                for raw_palette in palette_raw_values
            ]
            palette_values = [
                _decode_palette(raw_palette, picture, alpha_max)
                for raw_palette, alpha_max in zip(palette_raw_values, palette_alpha_modes)
            ]
            indices, rebuilt_palettes = _build_multi_palette_data(
                image, old_indices, palette_values, picture, palette_index
            )
            encoded_image = _pack_indices(indices, picture)
            if len(encoded_image) != picture.base_image_size:
                raise TM2ValidationError("Encoded pixel data size changed")
            if encoded_image != original_image:
                patches.append(Patch(
                    picture.source_path,
                    picture.absolute_image_offset,
                    encoded_image,
                    original_image,
                    f"TM2 picture {picture.index} pixel indices",
                ))

            for palette_id, (raw_palette, colors, alpha_max) in enumerate(
                zip(palette_raw_values, rebuilt_palettes, palette_alpha_modes)
            ):
                encoded_palette = _encode_palette(
                    colors, picture, raw_palette, alpha_max
                )
                if encoded_palette == raw_palette:
                    continue
                patches.append(Patch(
                    picture.source_path,
                    picture.absolute_clut_offset + palette_id * picture.palette_span,
                    encoded_palette,
                    raw_palette,
                    f"TM2 picture {picture.index} palette {palette_id}",
                ))

    elif image_type == 1:
        out = bytearray()
        for r, g, b, a in image.getdata():
            out += struct.pack("<H", _encode_16(r, g, b, a))
        encoded = bytes(out)
        patches.append(Patch(
            picture.source_path, picture.absolute_image_offset, encoded,
            original_image, f"TM2 picture {picture.index} RGBA16 pixels"
        ))
    elif image_type == 2:
        out = bytearray()
        for i, (r, g, b, _a) in enumerate(image.getdata()):
            out += bytes((r, g, b, original_image[i * 4 + 3]))
        patches.append(Patch(
            picture.source_path, picture.absolute_image_offset, bytes(out),
            original_image, f"TM2 picture {picture.index} RGB32 pixels"
        ))
    elif image_type == 3:
        out = bytearray()
        alpha_max = _alpha_storage_max(original_image)
        for r, g, b, a in image.getdata():
            out += bytes((r, g, b, _alpha_rgba_to_stored(a, alpha_max)))
        patches.append(Patch(
            picture.source_path, picture.absolute_image_offset, bytes(out),
            original_image, f"TM2 picture {picture.index} RGBA32 pixels"
        ))
    else:
        raise TM2UnsupportedError(f"Unsupported image type: {picture.image_type}")

    for patch in patches:
        if len(patch.data) != len(patch.expected):
            raise TM2ValidationError("A replacement patch changed length")
        if patch.offset < 0 or patch.end > picture.container.source_size:
            raise TM2ValidationError("Replacement patch exceeds source file")
        # Hard invariant requested for game safety: replacement data may never
        # overlap the 16-byte TIM2 file header or the selected picture header.
        protected_file_end = picture.container.offset + FILE_HEADER_SIZE
        if patch.offset < protected_file_end and patch.end > picture.container.offset:
            raise TM2ValidationError("Replacement attempted to modify TIM2 file header")
        if patch.offset < picture.absolute_image_offset and patch.end > picture.absolute_header_offset:
            raise TM2ValidationError("Replacement attempted to modify TIM2 picture header")
    return patches


def build_tm2_picture_replacement_patches(
    target_picture: TM2Picture,
    replacement_path: str | os.PathLike[str],
    target_palette_index: int = 0,
    donor_palette_index: int = 0,
) -> list[Patch]:
    """Replace one target picture from any decodable TIM2 donor picture.

    Unlike the strict container donor mode, the donor does not need the same
    header layout, dimensions, pixel type, palette type, or file size. Its
    first decodable picture is rendered to RGBA and then encoded into the
    target picture's existing fixed layout.
    """
    replacement_path = os.path.abspath(os.fspath(replacement_path))
    containers = scan_file(replacement_path)
    donor_picture = None
    for container in containers:
        for picture in container.pictures:
            if picture.safe_to_decode:
                donor_picture = picture
                break
        if donor_picture is not None:
            break
    if donor_picture is None:
        raise TM2ValidationError("Replacement TM2 contains no decodable picture")
    donor_image = decode_picture(donor_picture, donor_palette_index)
    return build_replacement_patches(target_picture, donor_image, target_palette_index)


def merge_patches(patches: Iterable[Patch]) -> list[Patch]:
    ordered = sorted(patches, key=lambda p: (os.path.abspath(p.source_path), p.offset))
    deduped: list[Patch] = []
    for patch in ordered:
        if deduped:
            previous = deduped[-1]
            same_file = os.path.abspath(previous.source_path) == os.path.abspath(patch.source_path)
            if same_file and patch.offset < previous.end:
                if (
                    patch.offset == previous.offset
                    and patch.data == previous.data
                    and patch.expected == previous.expected
                ):
                    continue
                raise TM2ValidationError(
                    f"Replacement patches overlap in {os.path.basename(patch.source_path)}"
                )
        deduped.append(patch)
    return deduped


def apply_patches_transactional(
    patches: Iterable[Patch],
    expected_sizes: Optional[dict[str, int]] = None,
    create_backup: bool = True,
) -> dict[str, str]:
    grouped: dict[str, list[Patch]] = defaultdict(list)
    for patch in merge_patches(patches):
        grouped[os.path.abspath(patch.source_path)].append(patch)

    saved: dict[str, str] = {}
    for path, file_patches in grouped.items():
        original_size = os.path.getsize(path)
        if expected_sizes and path in expected_sizes and original_size != expected_sizes[path]:
            raise TM2ValidationError(
                f"Source file size changed since scan: {os.path.basename(path)}"
            )
        with open(path, "rb") as current:
            for patch in file_patches:
                if patch.end > original_size:
                    raise TM2ValidationError("A patch exceeds the original file size")
                current.seek(patch.offset)
                if current.read(len(patch.expected)) != patch.expected:
                    raise TM2ValidationError(
                        f"Source bytes changed since replacement was prepared: "
                        f"{os.path.basename(path)} @ 0x{patch.offset:X}"
                    )

        directory = os.path.dirname(path) or "."
        fd, temp_path = tempfile.mkstemp(prefix=".skzezo_tm2_", suffix=".tmp", dir=directory)
        os.close(fd)
        try:
            shutil.copy2(path, temp_path)
            with open(temp_path, "r+b") as output:
                for patch in file_patches:
                    output.seek(patch.offset)
                    written = output.write(patch.data)
                    if written != len(patch.data):
                        raise OSError("Incomplete patch write")
                output.flush()
                os.fsync(output.fileno())

            if os.path.getsize(temp_path) != original_size:
                raise TM2ValidationError("Output file size changed")
            backup_path = ""
            if create_backup:
                backup_path = path + ".bak"
                if not os.path.exists(backup_path):
                    shutil.copy2(path, backup_path)
            os.replace(temp_path, path)
            if os.path.getsize(path) != original_size:
                raise TM2ValidationError("Saved file verification failed")
            saved[path] = backup_path
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise
    return saved


def apply_patches_to_copy(
    source_path: str,
    destination_path: str,
    patches: Iterable[Patch],
) -> None:
    source_path = os.path.abspath(source_path)
    destination_path = os.path.abspath(destination_path)
    relevant = [
        p for p in merge_patches(patches)
        if os.path.abspath(p.source_path) == source_path
    ]
    original_size = os.path.getsize(source_path)
    with open(source_path, "rb") as current:
        for patch in relevant:
            current.seek(patch.offset)
            if current.read(len(patch.expected)) != patch.expected:
                raise TM2ValidationError("Source bytes changed before Save Copy")
    shutil.copy2(source_path, destination_path)
    with open(destination_path, "r+b") as output:
        for patch in relevant:
            output.seek(patch.offset)
            output.write(patch.data)
        output.flush()
        os.fsync(output.fileno())
    if os.path.getsize(destination_path) != original_size:
        raise TM2ValidationError("Copied output size changed")


def validate_source_unchanged(container: TM2Container) -> bool:
    try:
        return os.path.getsize(container.source_path) == container.source_size
    except OSError:
        return False


def unique_export_name(picture: TM2Picture, index: int) -> str:
    stem = Path(picture.container.embedded_path or picture.source_path).stem
    return (
        f"{stem}_off_{picture.container.display_offset:08X}_pic_{picture.index:03d}_"
        f"{picture.width}x{picture.height}_{index:04d}.png"
    )
