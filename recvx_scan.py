from __future__ import annotations

import os
import re
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

SECTOR = 2048
IO_CHUNK = 16 * 1024 * 1024


def is_recvx_candidate(name: str) -> bool:
    p = name.replace('\\', '/').upper()
    base = p.rsplit('/', 1)[-1]
    if re.fullmatch(r'SYSMES\d*\.ALD(?:;\d+)?', base):
        return True
    if base.endswith('.TBL') or base.endswith('.TBL;1'):
        return True
    if base in {'SYSTEM.CNF', 'SYSTEM.CNF;1'}:
        return True
    if re.match(r'^(SLES|SLUS|SCES|SCUS|SLPM|SLPS|SLAJ|SLED|SCCS)[_.-]?\d', base):
        return True
    if base.endswith('.ALD') or '.ALD;' in base:
        return True
    if ('RDX' in p or re.fullmatch(r'SYSTEM\d*\.AFS(?:;\d+)?', base)) and (
        base.endswith(('.AFS', '.BIN', '.DAT', '.LNK', '.RDX')) or ';1' in base
    ):
        return True
    return False


def is_rdx_afs_name(name: str) -> bool:
    p = name.replace('\\', '/').upper()
    base = p.rsplit('/', 1)[-1].split(';', 1)[0]
    return base.endswith('.AFS') and 'RDX' in base


def iso_quick_priority(name: str) -> tuple[int, str]:
    """Put text/table files first, then ELF, then SYSTEM AFS/font assets."""
    p = name.replace('\\', '/').upper()
    base = p.rsplit('/', 1)[-1].split(';', 1)[0]
    if re.fullmatch(r'SYSMES\d*\.ALD', base):
        n = 0
    elif base.endswith('.TBL'):
        n = 1
    elif re.match(r'^(SLES|SLUS|SCES|SCUS|SLPM|SLPS|SLAJ|SLED|SCCS)[_.-]?\d', base):
        n = 2
    elif re.fullmatch(r'SYSTEM\d*\.AFS', base):
        n = 3
    else:
        n = 4
    return n, p


def clean_iso_component(name: str) -> str:
    name = name.split(';', 1)[0].rstrip('.')
    name = name.replace(':', '_').replace('\\', '_').replace('/', '_')
    return name or '_'


@dataclass(frozen=True)
class IsoEntry:
    path: str
    lba: int
    size: int
    is_dir: bool
    record_offset: int = -1


class Iso9660Reader:
    """Small ISO9660 reader sufficient for PS2 disc browsing/extraction."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = self.path.open('rb')
        self.root_lba = 0
        self.root_size = 0
        self._parse_volume_descriptors()

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    @staticmethod
    def _u32le(buf: bytes, off: int) -> int:
        return struct.unpack_from('<I', buf, off)[0]

    def _parse_volume_descriptors(self) -> None:
        found = False
        for sector_no in range(16, 128):
            self._fh.seek(sector_no * SECTOR)
            vd = self._fh.read(SECTOR)
            if len(vd) != SECTOR:
                break
            if vd[1:6] != b'CD001':
                continue
            vtype = vd[0]
            if vtype == 1:
                rec = vd[156:]
                if not rec or rec[0] < 34:
                    raise ValueError('ISO9660 root directory record is invalid')
                self.root_lba = self._u32le(rec, 2)
                self.root_size = self._u32le(rec, 10)
                found = True
                break
            if vtype == 255:
                break
        if not found:
            raise ValueError('Not a standard ISO9660 image (PVD not found)')

    def _read_dir(
        self,
        lba: int,
        size: int,
        prefix: str,
        seen: set[tuple[int, int]],
        cancel: Callable[[], bool] | None = None,
        visit: Callable[[int, str], None] | None = None,
        counter: list[int] | None = None,
    ) -> list[IsoEntry]:
        if cancel and cancel():
            raise InterruptedError('ISO scan cancelled')
        key = (lba, size)
        if key in seen:
            return []
        seen.add(key)
        self._fh.seek(lba * SECTOR)
        data = self._fh.read(size)
        out: list[IsoEntry] = []
        pos = 0
        while pos < len(data):
            if cancel and cancel():
                raise InterruptedError('ISO scan cancelled')
            rec_len = data[pos]
            if rec_len == 0:
                pos = ((pos // SECTOR) + 1) * SECTOR
                continue
            if pos + rec_len > len(data) or rec_len < 34:
                break
            rec_pos = pos
            rec = data[pos:pos + rec_len]
            extent = self._u32le(rec, 2)
            dlen = self._u32le(rec, 10)
            flags = rec[25]
            nlen = rec[32]
            raw_name = rec[33:33 + nlen]
            pos += rec_len
            if raw_name in (b'\x00', b'\x01'):
                continue
            name = raw_name.decode('ascii', errors='replace')
            clean = clean_iso_component(name)
            full = f'{prefix}/{clean}' if prefix else clean
            is_dir = bool(flags & 0x02)
            ent = IsoEntry(full, extent, dlen, is_dir, lba * SECTOR + rec_pos)
            out.append(ent)
            if counter is not None:
                counter[0] += 1
                if visit and (counter[0] <= 8 or counter[0] % 32 == 0):
                    visit(counter[0], full)
            if is_dir and dlen > 0:
                out.extend(self._read_dir(extent, dlen, full, seen, cancel, visit, counter))
        return out

    def entries(
        self,
        cancel: Callable[[], bool] | None = None,
        visit: Callable[[int, str], None] | None = None,
    ) -> list[IsoEntry]:
        return self._read_dir(self.root_lba, self.root_size, '', set(), cancel, visit, [0])

    def extract_entry(
        self, entry: IsoEntry, target: Path,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        if entry.is_dir:
            target.mkdir(parents=True, exist_ok=True)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        self._fh.seek(entry.lba * SECTOR)
        remaining = entry.size
        done = 0
        with target.open('wb') as out:
            while remaining > 0:
                chunk = self._fh.read(min(4 * 1024 * 1024, remaining))
                if not chunk:
                    raise IOError(f'Unexpected end of ISO while reading {entry.path}')
                out.write(chunk)
                remaining -= len(chunk)
                done += len(chunk)
                if progress:
                    progress(done, entry.size)

    def read_entry(self, entry: IsoEntry, offset: int = 0, size: int | None = None) -> bytes:
        if entry.is_dir:
            raise ValueError('Cannot read a directory extent')
        if offset < 0 or offset > entry.size:
            raise ValueError('ISO entry offset is outside the file')
        if size is None:
            size = entry.size - offset
        if size < 0 or offset + size > entry.size:
            raise ValueError('ISO entry read exceeds the file extent')
        self._fh.seek(entry.lba * SECTOR + offset)
        data = self._fh.read(size)
        if len(data) != size:
            raise IOError(f'Unexpected end of ISO while reading {entry.path}')
        return data


class IsoAfsArchive:
    """Read an AFS archive directly from its ISO extent without copying the AFS.

    Only the requested AFS member is read from disc, which is much faster for
    large RDX_LNK*.AFS files than extracting the entire container first.
    """

    def __init__(self, iso_path: Path, entry: IsoEntry):
        self.iso_path = Path(iso_path)
        self.iso_entry = entry
        self.base = int(entry.lba) * SECTOR
        self.size = int(entry.size)
        self.count = 0
        self.entries: list[tuple[int, int]] = []
        self.dir_off = 0
        self.dir_size = 0
        self.dir_ptr_pos = None
        self._dir = b''
        self._fh = self.iso_path.open('rb')
        f = self._fh
        try:
            f.seek(self.base)
            first = f.read(8)
            if len(first) != 8 or first[:4] != b'AFS\0':
                raise ValueError(f'{entry.path} is not an AFS archive')
            self.count = struct.unpack_from('<I', first, 4)[0]
            if self.count > 100000:
                raise ValueError('Invalid AFS entry count')
            toc_size = 8 + self.count * 8
            if toc_size > self.size:
                raise ValueError('AFS TOC exceeds ISO extent')
            f.seek(self.base + 8)
            toc = f.read(self.count * 8)
            if len(toc) != self.count * 8:
                raise ValueError('Truncated AFS TOC')
            self.entries = [struct.unpack_from('<II', toc, i * 8) for i in range(self.count)]
            for off, length in self.entries:
                if off < 0 or length < 0 or off + length > self.size:
                    raise ValueError('AFS member exceeds ISO extent')
            valid = [off for off, length in self.entries if off and length]
            first_data = min(valid) if valid else self.size
            scan_end = min(first_data, toc_size + 0x1000, self.size)
            if scan_end > toc_size:
                f.seek(self.base + toc_size)
                tail = f.read(scan_end - toc_size)
                header = first + toc + tail
            else:
                header = first + toc
            for q in range(toc_size, max(toc_size, scan_end - 7), 4):
                if q + 8 > len(header):
                    break
                doff, dsize = struct.unpack_from('<II', header, q)
                if not doff or dsize < self.count * 48 or doff + dsize > self.size:
                    continue
                f.seek(self.base + doff)
                rec = f.read(min(32, dsize))
                raw = rec.split(b'\0', 1)[0]
                if raw and any(c < 0x20 for c in raw):
                    continue
                self.dir_ptr_pos = q
                self.dir_off, self.dir_size = doff, dsize
                f.seek(self.base + doff)
                self._dir = f.read(dsize)
                if len(self._dir) != dsize:
                    self._dir = b''
                    self.dir_off = self.dir_size = 0
                break
        except Exception:
            try:
                self._fh.close()
            except Exception:
                pass
            raise

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass

    def __del__(self):
        self.close()

    def entry_name(self, index: int) -> str:
        if not (0 <= index < self.count):
            return ''
        if self._dir and len(self._dir) >= self.count * 48:
            raw = self._dir[index * 48:index * 48 + 32].split(b'\0', 1)[0]
            if raw:
                for enc in ('ascii', 'cp932', 'latin1'):
                    try:
                        name = raw.decode(enc).strip()
                        if name:
                            return name
                    except Exception:
                        pass
        return ''

    def entry_bytes(self, index: int) -> bytes:
        if not (0 <= index < self.count):
            raise IndexError(index)
        off, size = self.entries[index]
        self._fh.seek(self.base + off)
        data = self._fh.read(size)
        if len(data) != size:
            raise IOError('Truncated AFS member in ISO')
        return data


class StreamAfsArchive:
    """Low-memory read-only AFS reader for large RDX_LNK archives.

    Unlike recvx_core.AfsArchive this class never loads the whole archive into
    RAM. It keeps one file handle and reads only the requested member.
    """
    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = self.path.open('rb')
        self.size = self.path.stat().st_size
        self.count = 0
        self.entries: list[tuple[int, int]] = []
        self.dir_off = 0
        self.dir_size = 0
        self.dir_ptr_pos = None
        self._dir = b''
        try:
            f = self._fh
            first = f.read(8)
            if len(first) != 8 or first[:4] != b'AFS\0':
                raise ValueError(f'{self.path.name} is not an AFS archive')
            self.count = struct.unpack_from('<I', first, 4)[0]
            if self.count > 100000:
                raise ValueError('Invalid AFS entry count')
            toc_size = 8 + self.count * 8
            if toc_size > self.size:
                raise ValueError('AFS TOC exceeds file size')
            toc = f.read(self.count * 8)
            if len(toc) != self.count * 8:
                raise ValueError('Truncated AFS TOC')
            self.entries = [struct.unpack_from('<II', toc, i * 8) for i in range(self.count)]
            for off, length in self.entries:
                if off < 0 or length < 0 or off + length > self.size:
                    raise ValueError('AFS member exceeds file size')
            valid = [off for off, length in self.entries if off and length]
            first_data = min(valid) if valid else self.size
            scan_end = min(first_data, toc_size + 0x1000, self.size)
            if scan_end > toc_size:
                f.seek(toc_size)
                tail = f.read(scan_end - toc_size)
                header = first + toc + tail
            else:
                header = first + toc
            for q in range(toc_size, max(toc_size, scan_end - 7), 4):
                if q + 8 > len(header):
                    break
                doff, dsize = struct.unpack_from('<II', header, q)
                if not doff or dsize < self.count * 48 or doff + dsize > self.size:
                    continue
                f.seek(doff)
                rec = f.read(min(32, dsize))
                raw = rec.split(b'\0', 1)[0]
                if raw and any(c < 0x20 for c in raw):
                    continue
                self.dir_ptr_pos = q
                self.dir_off, self.dir_size = doff, dsize
                f.seek(doff)
                self._dir = f.read(dsize)
                if len(self._dir) != dsize:
                    self._dir = b''
                    self.dir_off = self.dir_size = 0
                break
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def entry_name(self, index: int) -> str:
        if not (0 <= index < self.count):
            return ''
        if self._dir and len(self._dir) >= self.count * 48:
            raw = self._dir[index * 48:index * 48 + 32].split(b'\0', 1)[0]
            if raw:
                for enc in ('ascii', 'cp932', 'latin1'):
                    try:
                        name = raw.decode(enc).strip()
                        if name:
                            return name
                    except Exception:
                        pass
        return ''

    def entry_bytes(self, index: int) -> bytes:
        if not (0 <= index < self.count):
            raise IndexError(index)
        off, size = self.entries[index]
        self._fh.seek(off)
        data = self._fh.read(size)
        if len(data) != size:
            raise IOError('Truncated AFS member')
        return data



def _iso_norm_path(name: str) -> str:
    return '/'.join(clean_iso_component(x) for x in str(name).replace('\\', '/').split('/') if x).casefold()


def find_iso_entry(iso_path: Path, entry_path: str) -> IsoEntry:
    wanted = _iso_norm_path(entry_path)
    with Iso9660Reader(Path(iso_path)) as iso:
        for ent in iso.entries():
            if not ent.is_dir and _iso_norm_path(ent.path) == wanted:
                return ent
    raise FileNotFoundError(f'ISO entry not found: {entry_path}')


def _write_both_u32(buf: bytearray, off: int, value: int) -> None:
    struct.pack_into('<I', buf, off, int(value))
    struct.pack_into('>I', buf, off + 4, int(value))


def _patch_iso_record(f, record_offset: int, lba: int, size: int) -> None:
    if record_offset < 0:
        raise ValueError('ISO directory record offset unavailable')
    f.seek(record_offset)
    rec_len_raw = f.read(1)
    if not rec_len_raw:
        raise IOError('ISO directory record is missing')
    rec_len = rec_len_raw[0]
    if rec_len < 34:
        raise ValueError('ISO directory record is invalid')
    f.seek(record_offset)
    rec = bytearray(f.read(rec_len))
    if len(rec) != rec_len:
        raise IOError('ISO directory record is truncated')
    _write_both_u32(rec, 2, lba)
    _write_both_u32(rec, 10, size)
    f.seek(record_offset)
    f.write(rec)


def _patch_iso_volume_space(f, sectors: int) -> None:
    # Update every ISO9660 primary/supplementary volume descriptor encountered
    # before the terminator. This keeps appended replacement extents visible.
    for sector_no in range(16, 128):
        pos = sector_no * SECTOR
        f.seek(pos)
        vd = bytearray(f.read(SECTOR))
        if len(vd) != SECTOR:
            break
        if vd[1:6] != b'CD001':
            continue
        if vd[0] in (1, 2):
            _write_both_u32(vd, 80, sectors)
            f.seek(pos)
            f.write(vd)
        if vd[0] == 255:
            break


def replace_iso_entry_from_file(iso_path: Path, entry_path: str, new_file: Path, progress=None) -> IsoEntry:
    """Replace one ISO9660 file while keeping the same ISO filename."""
    import hashlib
    import tempfile

    def emit(frac, stage):
        if progress:
            try: progress(max(0, min(1000, int(float(frac) * 1000))), 1000, stage)
            except Exception: pass

    iso_path = Path(iso_path)
    new_file = Path(new_file)
    ent = find_iso_entry(iso_path, entry_path)
    new_size = new_file.stat().st_size
    old_alloc = ((ent.size + SECTOR - 1) // SECTOR) * SECTOR
    old_file_size = iso_path.stat().st_size

    rollback = None
    meta_backup = None
    appended = False
    expected_hash = None
    emit(0.0, 'ISO')
    try:
        with iso_path.open('r+b', buffering=0) as f:
            if ent.record_offset < 0:
                raise ValueError('ISO directory record offset unavailable')
            f.seek(ent.record_offset)
            rec_len_b = f.read(1)
            if not rec_len_b or rec_len_b[0] < 34:
                raise ValueError('ISO directory record is invalid')
            rec_len = rec_len_b[0]
            f.seek(ent.record_offset)
            old_rec = f.read(rec_len)
            vd_backups = []
            for sector_no in range(16, 128):
                pos = sector_no * SECTOR
                f.seek(pos); vd = f.read(SECTOR)
                if len(vd) != SECTOR: break
                if vd[1:6] != b'CD001': continue
                if vd[0] in (1, 2): vd_backups.append((pos, vd))
                if vd[0] == 255: break
            meta_backup = (old_rec, vd_backups)
            copy_hash = hashlib.sha256()

            if new_size <= old_alloc:
                fd, rbname = tempfile.mkstemp(prefix='recvx_iso_rollback_', suffix='.bin')
                os.close(fd); rollback = Path(rbname)
                f.seek(ent.lba * SECTOR)
                with rollback.open('wb') as rb:
                    remain = old_alloc; copied = 0; denom = max(1, old_alloc)
                    while remain:
                        b = f.read(min(remain, IO_CHUNK))
                        if not b: raise IOError('ISO source extent is truncated')
                        rb.write(b); remain -= len(b); copied += len(b)
                        emit(0.02 + 0.18 * copied / denom, 'Backup')
                f.seek(ent.lba * SECTOR)
                copied = 0; denom = max(1, new_size)
                with new_file.open('rb') as src:
                    while True:
                        b = src.read(IO_CHUNK)
                        if not b: break
                        f.write(b); copy_hash.update(b); copied += len(b)
                        emit(0.20 + 0.48 * copied / denom, 'ISO write')
                pad = old_alloc - new_size
                zero = b'\0' * min(IO_CHUNK, max(1, pad))
                while pad > 0:
                    n = min(pad, len(zero)); f.write(zero[:n]); pad -= n
                _patch_iso_record(f, ent.record_offset, ent.lba, new_size)
                new_lba = ent.lba
            else:
                appended = True
                new_start = ((old_file_size + SECTOR - 1) // SECTOR) * SECTOR
                new_lba = new_start // SECTOR
                f.seek(old_file_size)
                if new_start > old_file_size:
                    f.write(b'\0' * (new_start - old_file_size))
                copied = 0; denom = max(1, new_size)
                with new_file.open('rb') as src:
                    while True:
                        b = src.read(IO_CHUNK)
                        if not b: break
                        f.write(b); copy_hash.update(b); copied += len(b)
                        emit(0.05 + 0.63 * copied / denom, 'ISO write')
                end = f.tell()
                aligned_end = ((end + SECTOR - 1) // SECTOR) * SECTOR
                if aligned_end > end:
                    f.write(b'\0' * (aligned_end - end))
                _patch_iso_record(f, ent.record_offset, new_lba, new_size)
                _patch_iso_volume_space(f, aligned_end // SECTOR)
            expected_hash = copy_hash.digest()
            emit(0.70, 'ISO flush')
            f.flush(); os.fsync(f.fileno())

        fresh = find_iso_entry(iso_path, entry_path)
        if fresh.size != new_size or fresh.lba != new_lba:
            raise ValueError('ISO directory verification failed')
        h = hashlib.sha256(); checked = 0; denom = max(1, fresh.size)
        with iso_path.open('rb') as f:
            f.seek(fresh.lba * SECTOR); remain = fresh.size
            while remain:
                b = f.read(min(remain, IO_CHUNK))
                if not b: raise IOError('ISO replacement verification truncated')
                h.update(b); remain -= len(b); checked += len(b)
                emit(0.72 + 0.27 * checked / denom, 'Verify')
        if expected_hash is None or h.digest() != expected_hash:
            raise ValueError('ISO replacement data verification failed')
        emit(1.0, 'Done')
        return fresh
    except Exception:
        try:
            with iso_path.open('r+b', buffering=0) as f:
                if rollback is not None and rollback.exists():
                    f.seek(ent.lba * SECTOR)
                    with rollback.open('rb') as rb:
                        while True:
                            b = rb.read(IO_CHUNK)
                            if not b: break
                            f.write(b)
                if meta_backup is not None:
                    old_rec, vd_backups = meta_backup
                    f.seek(ent.record_offset); f.write(old_rec)
                    for pos, vd in vd_backups:
                        f.seek(pos); f.write(vd)
                if appended:
                    f.truncate(old_file_size)
                f.flush(); os.fsync(f.fileno())
        except Exception:
            pass
        raise
    finally:
        if rollback is not None:
            try: rollback.unlink()
            except OSError: pass

def replace_iso_entry_bytes(iso_path: Path, entry_path: str, new_raw: bytes) -> IsoEntry:
    import tempfile
    fd, name = tempfile.mkstemp(prefix='recvx_iso_payload_', suffix='.bin')
    os.close(fd)
    p = Path(name)
    try:
        p.write_bytes(bytes(new_raw))
        return replace_iso_entry_from_file(iso_path, entry_path, p)
    finally:
        try: p.unlink()
        except OSError: pass


def _payload_size(payload) -> int:
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return len(payload)
    return Path(payload).stat().st_size


def _copy_payload(payload, out, progress=None) -> int:
    total = _payload_size(payload)
    done = 0
    if progress:
        try: progress(0, max(1, total))
        except Exception: pass
    if isinstance(payload, (bytes, bytearray, memoryview)):
        b = bytes(payload); out.write(b); done = len(b)
        if progress:
            try: progress(done, max(1, total))
            except Exception: pass
        return done
    with Path(payload).open('rb') as src:
        while True:
            b = src.read(IO_CHUNK)
            if not b: break
            out.write(b); done += len(b)
            if progress:
                try: progress(done, max(1, total))
                except Exception: pass
    return done


def _payload_digest(payload) -> bytes:
    import hashlib
    h = hashlib.sha256()
    if isinstance(payload, (bytes, bytearray, memoryview)):
        h.update(bytes(payload)); return h.digest()
    with Path(payload).open('rb') as src:
        while True:
            b = src.read(IO_CHUNK)
            if not b: break
            h.update(b)
    return h.digest()


def _file_region_digest(path: Path, offset: int, size: int) -> bytes:
    import hashlib
    h = hashlib.sha256(); remain = int(size)
    with Path(path).open('rb') as f:
        f.seek(int(offset))
        while remain:
            b = f.read(min(remain, IO_CHUNK))
            if not b: raise IOError('Verification region is truncated')
            h.update(b); remain -= len(b)
    return h.digest()


def _afs_allocations(entries, dir_off: int, total_size: int) -> dict[int, int]:
    result = {}
    offsets = sorted({off for off, size in entries if off > 0})
    for i, (off, size) in enumerate(entries):
        if off <= 0:
            result[i] = 0; continue
        bounds = [x for x in offsets if x > off]
        if dir_off and dir_off > off: bounds.append(dir_off)
        bounds.append(total_size)
        result[i] = max(0, min(bounds) - off)
    return result


def _build_afs_replacement(src_path: Path, src_base: int, src_size: int, entries,
                           count: int, dir_ptr_pos, dir_off: int, dir_size: int,
                           directory: bytes, replacements: Mapping[int, object],
                           out_path: Path, alignment: int = 0x800, progress=None) -> None:
    """Stream one rebuilt AFS in a single pass. Replacements may be bytes or files."""
    valid = [off for off, size in entries if off and size]
    first_data = min(valid) if valid else ((8 + count * 8 + alignment - 1) // alignment) * alignment
    build_total = first_data + len(directory)
    for i, (_off, size) in enumerate(entries):
        build_total += _payload_size(replacements[i]) if i in replacements else int(size)
    build_total = max(1, build_total)
    build_done = 0

    def emit(stage='AFS build', value=None):
        if not progress: return
        try: progress(min(build_total, build_done if value is None else int(value)), build_total, stage)
        except Exception: pass

    out_path = Path(out_path)
    emit()
    with Path(src_path).open('rb') as inf, out_path.open('w+b') as out:
        inf.seek(src_base)
        header = bytearray(inf.read(first_data))
        if len(header) < first_data: raise IOError('AFS header truncated')
        out.write(header); build_done += len(header); emit()
        pos = first_data; new_entries = []
        for i, (off, size) in enumerate(entries):
            if off == 0 and size == 0 and i not in replacements:
                new_entries.append((0, 0)); continue
            pos = (pos + alignment - 1) // alignment * alignment
            cur = out.tell()
            if cur < pos: out.write(b'\0' * (pos - cur))
            noff = pos
            if i in replacements:
                payload = replacements[i]
                base_done = build_done
                psize = _payload_size(payload)
                _copy_payload(payload, out, progress=lambda d, t, b=base_done: emit(value=b + d))
                nsize = psize; build_done += psize
            else:
                inf.seek(src_base + off); remain = size
                while remain:
                    chunk = inf.read(min(remain, IO_CHUNK))
                    if not chunk: raise IOError('AFS member truncated during rebuild')
                    out.write(chunk); remain -= len(chunk); build_done += len(chunk); emit()
                nsize = size
            pos = out.tell(); new_entries.append((noff, nsize))

        if directory and dir_size:
            block = bytearray(directory)
            if len(block) >= count * 48:
                for i, (_off, nsize) in enumerate(new_entries):
                    struct.pack_into('<I', block, i * 48 + 44, nsize)
            pos = (out.tell() + alignment - 1) // alignment * alignment
            if out.tell() < pos: out.write(b'\0' * (pos - out.tell()))
            new_dir_off = out.tell(); out.write(block); build_done += len(block); emit()
        else:
            new_dir_off = 0

        for i, (off, nsize) in enumerate(new_entries):
            out.seek(8 + i * 8); out.write(struct.pack('<II', off, nsize))
        if dir_ptr_pos is not None:
            out.seek(dir_ptr_pos)
            out.write(struct.pack('<II', new_dir_off, len(directory) if new_dir_off else 0))
        emit('AFS flush', build_total)
        out.flush(); os.fsync(out.fileno())
    if progress:
        try: progress(build_total, build_total, 'AFS build')
        except Exception: pass

def _verify_afs_payloads(path: Path, replacements: Mapping[int, object], progress=None) -> None:
    arc = StreamAfsArchive(Path(path))
    items = list(replacements.items()); total = max(1, len(items))
    try:
        if progress:
            try: progress(0, total, 'Verify')
            except Exception: pass
        for n, (index, payload) in enumerate(items, 1):
            if not (0 <= int(index) < arc.count): raise IndexError(index)
            off, size = arc.entries[int(index)]
            if size != _payload_size(payload):
                raise ValueError('AFS replacement size verification failed')
            if _file_region_digest(path, off, size) != _payload_digest(payload):
                raise ValueError('AFS replacement data verification failed')
            if progress:
                try: progress(n, total, 'Verify')
                except Exception: pass
    finally:
        arc.close()

def replace_iso_afs_entries_streaming(iso_path: Path, entry_path: str,
                                      replacements: Mapping[int, object],
                                      alignment: int = 0x800, progress=None) -> IsoEntry:
    """Replace many members of one AFS inside an ISO with a single repack."""
    import tempfile, shutil
    def emit(frac, stage):
        if progress:
            try: progress(max(0, min(1000, int(float(frac) * 1000))), 1000, stage)
            except Exception: pass

    iso_path = Path(iso_path)
    reps = {int(i): v for i, v in replacements.items()}
    if not reps:
        emit(1.0, 'Done')
        return find_iso_entry(iso_path, entry_path)
    emit(0.0, 'AFS')
    ent = find_iso_entry(iso_path, entry_path)
    arc = IsoAfsArchive(iso_path, ent)
    try:
        for i in reps:
            if not (0 <= i < arc.count): raise IndexError(i)
        entries = list(arc.entries); count = arc.count
        dir_off, dir_size, dir_ptr_pos = arc.dir_off, arc.dir_size, arc.dir_ptr_pos
        directory = bytes(arc._dir)
        allocs = _afs_allocations(entries, dir_off, arc.size)
    finally:
        arc.close()

    if all(_payload_size(payload) <= allocs.get(i, 0) for i, payload in reps.items()):
        td = Path(tempfile.mkdtemp(prefix='recvx_iso_afs_journal_'))
        journal = td / 'rollback.bin'
        base = ent.lba * SECTOR
        segments = []; old_meta = []
        try:
            sorted_ids = sorted(reps, key=lambda x: entries[x][0])
            journal_total = max(1, sum(max(entries[i][1], _payload_size(reps[i])) for i in sorted_ids))
            journal_done = 0
            with iso_path.open('r+b', buffering=0) as f, journal.open('wb') as rb:
                for i in sorted_ids:
                    old_off, old_size = entries[i]; nsize = _payload_size(reps[i])
                    span = max(old_size, nsize)
                    f.seek(base + old_off); old = f.read(span)
                    if len(old) != span: raise IOError('ISO AFS rollback span is truncated')
                    rbpos = rb.tell(); rb.write(old); segments.append((base + old_off, span, rbpos))
                    journal_done += span; emit(0.03 + 0.17 * journal_done / journal_total, 'Backup')
                    f.seek(base + 8 + i * 8 + 4); old_toc = f.read(4)
                    old_dir = None
                    if directory and dir_off and len(directory) >= (i + 1) * 48:
                        f.seek(base + dir_off + i * 48 + 44); old_dir = f.read(4)
                    old_meta.append((i, old_toc, old_dir))
                try:
                    write_total = max(1, sum(_payload_size(reps[i]) for i in sorted_ids)); write_done = 0
                    for i in sorted_ids:
                        old_off, old_size = entries[i]; payload = reps[i]; nsize = _payload_size(payload)
                        f.seek(base + old_off)
                        base_written = write_done
                        _copy_payload(payload, f, progress=lambda d, t, b=base_written: emit(0.20 + 0.47 * (b + d) / write_total, 'AFS write'))
                        write_done += nsize
                        if nsize < old_size: f.write(b'\0' * (old_size - nsize))
                        f.seek(base + 8 + i * 8 + 4); f.write(struct.pack('<I', nsize))
                        if directory and dir_off and len(directory) >= (i + 1) * 48:
                            f.seek(base + dir_off + i * 48 + 44); f.write(struct.pack('<I', nsize))
                    emit(0.70, 'AFS flush'); f.flush(); os.fsync(f.fileno())
                except Exception:
                    rb.flush()
                    for abs_off, span, rbpos in segments:
                        rb.seek(rbpos); f.seek(abs_off)
                        remain = span
                        while remain:
                            b = rb.read(min(remain, IO_CHUNK))
                            if not b: break
                            f.write(b); remain -= len(b)
                    for i, old_toc, old_dir in old_meta:
                        f.seek(base + 8 + i * 8 + 4); f.write(old_toc)
                        if old_dir is not None:
                            f.seek(base + dir_off + i * 48 + 44); f.write(old_dir)
                    f.flush(); os.fsync(f.fileno())
                    raise

            chk_ent = find_iso_entry(iso_path, entry_path)
            chk = IsoAfsArchive(iso_path, chk_ent)
            try:
                total = max(1, len(reps))
                for n, (i, payload) in enumerate(reps.items(), 1):
                    off, size = chk.entries[i]
                    if size != _payload_size(payload): raise ValueError('ISO AFS size verification failed')
                    if _file_region_digest(iso_path, chk.base + off, size) != _payload_digest(payload):
                        raise ValueError('ISO AFS data verification failed')
                    emit(0.72 + 0.27 * n / total, 'Verify')
            finally:
                chk.close()
            emit(1.0, 'Done')
            return chk_ent
        finally:
            shutil.rmtree(td, ignore_errors=True)

    td = Path(tempfile.mkdtemp(prefix='recvx_iso_afs_batch_'))
    afs_tmp = td / 'archive.afs'
    try:
        _build_afs_replacement(iso_path, ent.lba * SECTOR, ent.size, entries, count,
                               dir_ptr_pos, dir_off, dir_size, directory, reps,
                               afs_tmp, alignment,
                               progress=lambda d, t, st: emit(0.02 + 0.53 * d / max(1, t), st))
        _verify_afs_payloads(afs_tmp, reps,
                             progress=lambda d, t, st: emit(0.55 + 0.10 * d / max(1, t), st))
        result = replace_iso_entry_from_file(iso_path, entry_path, afs_tmp,
                                             progress=lambda d, t, st: emit(0.65 + 0.35 * d / max(1, t), st))
        emit(1.0, 'Done')
        return result
    finally:
        shutil.rmtree(td, ignore_errors=True)

def replace_iso_afs_entry_streaming(iso_path: Path, entry_path: str, index: int, new_raw: bytes, progress=None) -> IsoEntry:
    return replace_iso_afs_entries_streaming(iso_path, entry_path, {int(index): bytes(new_raw)}, progress=progress)

def extract_iso_candidates(
    iso_path: Path,
    out_root: Path,
    progress: Callable[[int, int, str], None] | None = None,
    cancel: Callable[[], bool] | None = None,
    defer_rdx_afs: bool = False,
) -> tuple[int, list[str]]:
    out_root.mkdir(parents=True, exist_ok=True)
    with Iso9660Reader(iso_path) as iso:
        all_entries = [e for e in iso.entries(cancel=cancel) if not e.is_dir]
        selected = [e for e in all_entries if is_recvx_candidate(e.path)]
        if defer_rdx_afs:
            selected = [e for e in selected if not is_rdx_afs_name(e.path)]
        selected.sort(key=lambda e: iso_quick_priority(e.path))
        for i, ent in enumerate(selected, 1):
            if cancel and cancel():
                raise InterruptedError('ISO scan cancelled')
            if progress:
                progress(i - 1, max(1, len(selected)), ent.path)
            target = out_root.joinpath(*[clean_iso_component(x) for x in ent.path.split('/')])
            iso.extract_entry(ent, target)
        if progress:
            progress(len(selected), max(1, len(selected)), 'Done')
        return len(selected), [e.path for e in selected]


def extract_zip_candidates(
    zip_path: Path,
    out_root: Path,
    progress: Callable[[int, int, str], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> tuple[int, list[str]]:
    out_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, 'r') as zf:
        infos = [i for i in zf.infolist() if not i.is_dir() and is_recvx_candidate(i.filename)]
        infos.sort(key=lambda i: iso_quick_priority(i.filename))
        for idx, info in enumerate(infos, 1):
            if cancel and cancel():
                raise InterruptedError('Archive scan cancelled')
            if progress:
                progress(idx - 1, max(1, len(infos)), info.filename)
            parts = [clean_iso_component(p) for p in info.filename.replace('\\', '/').split('/') if p not in ('', '.', '..')]
            target = out_root.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, 'r') as src, target.open('wb') as dst:
                while True:
                    chunk = src.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
        if progress:
            progress(len(infos), max(1, len(infos)), 'Done')
        return len(infos), [i.filename for i in infos]


def looks_like_iso(path: Path) -> bool:
    try:
        with path.open('rb') as f:
            f.seek(16 * SECTOR)
            pvd = f.read(7)
        return len(pvd) == 7 and pvd[1:6] == b'CD001'
    except Exception:
        return False


def _backup_once(path: Path, progress=None) -> Path:
    import shutil
    path = Path(path); bak = path.with_suffix(path.suffix + '.bak')
    if bak.exists():
        if progress:
            try: progress(1, 1, 'Backup')
            except Exception: pass
        return bak
    total = max(1, path.stat().st_size); done = 0
    with path.open('rb') as src, bak.open('wb') as dst:
        while True:
            b = src.read(IO_CHUNK)
            if not b: break
            dst.write(b); done += len(b)
            if progress:
                try: progress(done, total, 'Backup')
                except Exception: pass
        dst.flush(); os.fsync(dst.fileno())
    try: shutil.copystat(path, bak)
    except OSError: pass
    if progress:
        try: progress(total, total, 'Backup')
        except Exception: pass
    return bak

def replace_afs_entries_streaming(path: Path, replacements: Mapping[int, object], alignment: int = 0x800, progress=None) -> None:
    """Replace many AFS members with one write/rebuild operation."""
    import time, gc
    def emit(frac, stage):
        if progress:
            try: progress(max(0, min(1000, int(float(frac) * 1000))), 1000, stage)
            except Exception: pass

    path = Path(path)
    reps = {int(i): v for i, v in replacements.items()}
    if not reps:
        emit(1.0, 'Done'); return
    emit(0.0, 'AFS')
    arc = StreamAfsArchive(path)
    try:
        for i in reps:
            if not (0 <= i < arc.count): raise IndexError(i)
        entries = list(arc.entries); count = arc.count
        dir_ptr_pos = arc.dir_ptr_pos
        dir_off, dir_size = arc.dir_off, arc.dir_size
        directory = bytes(arc._dir)
        total_size = arc.size
        allocs = _afs_allocations(entries, dir_off, total_size)
    finally:
        arc.close()

    if all(_payload_size(payload) <= allocs.get(i, 0) for i, payload in reps.items()):
        _backup_once(path, progress=lambda d, t, st: emit(0.02 + 0.23 * d / max(1, t), st))
        sorted_ids = sorted(reps, key=lambda x: entries[x][0])
        write_total = max(1, sum(_payload_size(reps[i]) for i in sorted_ids)); write_done = 0
        with path.open('r+b', buffering=0) as f:
            for i in sorted_ids:
                old_off, old_size = entries[i]; payload = reps[i]; nsize = _payload_size(payload)
                f.seek(old_off); base_written = write_done
                _copy_payload(payload, f, progress=lambda d, t, b=base_written: emit(0.25 + 0.45 * (b + d) / write_total, 'AFS write'))
                write_done += nsize
                if nsize < old_size: f.write(b'\0' * (old_size - nsize))
                f.seek(8 + i * 8 + 4); f.write(struct.pack('<I', nsize))
                if directory and dir_off and len(directory) >= (i + 1) * 48:
                    f.seek(dir_off + i * 48 + 44); f.write(struct.pack('<I', nsize))
            emit(0.72, 'AFS flush'); f.flush(); os.fsync(f.fileno())
        _verify_afs_payloads(path, reps, progress=lambda d, t, st: emit(0.74 + 0.25 * d / max(1, t), st))
        emit(1.0, 'Done')
        return

    tmp_path = path.with_suffix(path.suffix + '.tmp')
    try:
        _build_afs_replacement(path, 0, total_size, entries, count, dir_ptr_pos,
                               dir_off, dir_size, directory, reps, tmp_path, alignment,
                               progress=lambda d, t, st: emit(0.02 + 0.78 * d / max(1, t), st))
        _verify_afs_payloads(tmp_path, reps, progress=lambda d, t, st: emit(0.80 + 0.15 * d / max(1, t), st))
        bak = path.with_suffix(path.suffix + '.bak')
        moved_to_backup = False
        if not bak.exists():
            backup_error = None
            for attempt in range(8):
                try:
                    emit(0.96, 'Swap'); os.replace(path, bak); moved_to_backup = True; backup_error = None; break
                except PermissionError as exc:
                    backup_error = exc; gc.collect(); time.sleep(0.05 * (attempt + 1))
            if backup_error is not None:
                raise backup_error
        last_error = None
        for attempt in range(8):
            try:
                emit(0.98, 'Swap'); os.replace(tmp_path, path); last_error = None; break
            except PermissionError as exc:
                last_error = exc; gc.collect(); time.sleep(0.05 * (attempt + 1))
        if last_error is not None:
            if moved_to_backup and bak.exists() and not path.exists():
                try: os.replace(bak, path)
                except OSError: pass
            raise last_error
        emit(1.0, 'Done')
    except Exception:
        try: tmp_path.unlink()
        except OSError: pass
        raise

def replace_afs_entry_streaming(path: Path, index: int, new_raw: bytes, alignment: int = 0x800, progress=None) -> None:
    replace_afs_entries_streaming(path, {int(index): bytes(new_raw)}, alignment, progress=progress)

