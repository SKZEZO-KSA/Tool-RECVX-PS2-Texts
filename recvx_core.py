from __future__ import annotations
import os, re, shutil, struct
from dataclasses import dataclass, field
from pathlib import Path

RDX_MAGIC={0x41200000,0x40051EB8}
PARAM_CODES={0xFF02,0xFF03}

def u16(d,o): return struct.unpack_from('<H',d,o)[0]
def u32(d,o): return struct.unpack_from('<I',d,o)[0]
def align(v,a): return (v+a-1)//a*a


def _expand_token_loop(data:bytes,max_output:int=64*1024*1024,stop_at:int|None=None)->tuple[bytes,bool]:
    """Bounded RECVX Expand decoder.

    stop_at permits text-only prefix decoding: the decoder returns as soon as
    that many output bytes exist without walking the large texture tail.
    The boolean result is True only when the stream terminator was reached.
    """
    src=memoryview(data); slen=len(src); pos=0; flags=0; bits=0; out=bytearray()
    while True:
        if stop_at is not None and len(out)>=stop_at:
            return bytes(out[:stop_at]),False
        # Common fast path: eight consecutive literals. This matters both for
        # retail streams and for our conservative literal repacker.
        if bits==0 and pos<slen and int(src[pos])==0xFF:
            if pos+9>slen: raise ValueError('Compressed RDX is truncated')
            if len(out)+8>max_output: raise ValueError('Compressed RDX expands beyond safety limit')
            pos+=1; out.extend(src[pos:pos+8]); pos+=8
            continue
        if bits==0:
            if pos>=slen: raise ValueError('Compressed RDX ended before terminator')
            flags=int(src[pos]); pos+=1; bits=8
        b1=flags&1; flags>>=1; bits-=1
        if b1:
            if pos>=slen: raise ValueError('Compressed RDX is truncated')
            if len(out)>=max_output: raise ValueError('Compressed RDX expands beyond safety limit')
            out.append(src[pos]); pos+=1
            continue
        if bits==0:
            if pos>=slen: raise ValueError('Compressed RDX ended before terminator')
            flags=int(src[pos]); pos+=1; bits=8
        b2=flags&1; flags>>=1; bits-=1
        if not b2:
            if bits==0:
                if pos>=slen: raise ValueError('Compressed RDX ended before terminator')
                flags=int(src[pos]); pos+=1; bits=8
            b3=flags&1; flags>>=1; bits-=1
            if bits==0:
                if pos>=slen: raise ValueError('Compressed RDX ended before terminator')
                flags=int(src[pos]); pos+=1; bits=8
            b4=flags&1; flags>>=1; bits-=1
            if pos>=slen: raise ValueError('Compressed RDX is truncated')
            distance=int(src[pos])-256; pos+=1
            length=((b3<<1)|b4)+2
        else:
            if pos+2>slen: raise ValueError('Compressed RDX is truncated')
            lo=int(src[pos]); hi=int(src[pos+1]); pos+=2
            pair=lo|(hi<<8)
            if pair==0: return bytes(out),True
            distance=(pair>>3)-8192
            n=lo&7
            if n:
                length=n+2
            else:
                if pos>=slen: raise ValueError('Compressed RDX is truncated')
                length=int(src[pos])+1; pos+=1
        span=-distance; olen=len(out)
        if distance>=0 or span>olen: raise ValueError('Invalid compressed RDX back-reference')
        if length<0 or olen+length>max_output: raise ValueError('Compressed RDX expands beyond safety limit')
        start=olen-span
        if length<=span:
            out.extend(out[start:start+length])
        else:
            seed=bytes(out[start:olen])
            if not seed: raise ValueError('Invalid compressed RDX back-reference')
            reps=(length+len(seed)-1)//len(seed)
            out.extend((seed*reps)[:length])


def recvx_expand(data:bytes,max_output:int=64*1024*1024)->bytes:
    out,complete=_expand_token_loop(data,max_output,None)
    if not complete: raise ValueError('Compressed RDX ended before terminator')
    return out


def recvx_expand_text_prefix(data:bytes,max_output:int=64*1024*1024)->bytes:
    """Expand only the first RDX block through the end of sub-block 14 text.

    Large texture/model tails are skipped entirely during browsing and scans.
    A full decode is deferred until the user actually saves that room.
    """
    # First obtain enough bytes to resolve the top-level block pointer.
    head,_=_expand_token_loop(data,max_output,0x60)
    if len(head)<0x24 or u32(head,0) not in RDX_MAGIC:
        raise ValueError('Expanded stream is not RDX')
    text_block=u32(head,0x10)
    if text_block<=0 or text_block+64>max_output:
        raise ValueError('Invalid RDX text block')
    hdr,_=_expand_token_loop(data,max_output,text_block+64)
    sub=[u32(hdr,text_block+i*4) for i in range(16)]
    text_pos=sub[14]
    if text_pos==0:
        return hdr
    nominal_end=sub[15] if sub[15] else u32(hdr,0x14)
    texture_block=u32(hdr,0x20)
    if text_pos < nominal_end <= max_output:
        text_end=nominal_end
    elif text_pos < texture_block <= max_output:
        text_end=texture_block
    else:
        raise ValueError('Invalid RDX text range')
    if text_end<text_block+64:
        raise ValueError('Invalid RDX text end')
    prefix,_=_expand_token_loop(data,max_output,text_end)
    return prefix


def recvx_pack_literals(data:bytes)->bytes:
    """Fast conservative Expand() encoder using literal tokens only."""
    data=bytes(data); n=len(data); out=bytearray(); pos=0
    full=n//8
    for _ in range(full):
        out.append(0xFF); out.extend(data[pos:pos+8]); pos+=8
    rem=n-pos
    if rem:
        if rem<=6:
            # rem literal bits, then terminator bits 0,1 in the same flag byte.
            out.append(((1<<rem)-1) | (1<<(rem+1)))
            out.extend(data[pos:])
            out.extend(b'\x00\x00')
        else: # seven literals: b1=0 fills bit 7; b2=1 starts next flag byte.
            out.append(0x7F); out.extend(data[pos:]); out.append(0x01); out.extend(b'\x00\x00')
    else:
        out.append(0x02); out.extend(b'\x00\x00')
    return bytes(out)


class _PrsBitWriter:
    """PRS control-bit writer. Control bits are LSB-first and interleaved
    with command payloads exactly as the RECVX Expand decoder expects.
    """
    __slots__ = ('out', '_flag_pos', '_bit')
    def __init__(self):
        self.out = bytearray()
        self._flag_pos = -1
        self._bit = 8
    def bit(self, value:int) -> None:
        if self._bit >= 8:
            self._flag_pos = len(self.out)
            self.out.append(0)
            self._bit = 0
        if value:
            self.out[self._flag_pos] |= 1 << self._bit
        self._bit += 1
    def byte(self, value:int) -> None:
        self.out.append(value & 0xFF)
    def bytes2(self, lo:int, hi:int) -> None:
        self.out.append(lo & 0xFF); self.out.append(hi & 0xFF)


def recvx_pack_prs(data:bytes, *, max_candidates:int=64, verify:bool=True, progress=None)->bytes:
    """Compress a PS2 RECVX RDX with the game's native PRS/Expand format.

    The old literal-only stream was valid, but it made compressed RDX members
    much larger than the retail files.  Real LZ back-references keep the AFS
    member close to its normal size and avoid unnecessary archive/ISO growth.
    """
    from array import array

    src = bytes(data)
    n = len(src)
    bw = _PrsBitWriter()
    if progress:
        try: progress(0, max(1, n), 'PRS')
        except Exception: pass
    if n == 0:
        bw.bit(0); bw.bit(1); bw.bytes2(0, 0)
        if progress:
            try: progress(1, 1, 'PRS')
            except Exception: pass
        return bytes(bw.out)

    # 20-bit chained hash for 3-byte sequences. This keeps lookup fast without
    # the memory cost of a full 24-bit table.  The exact bytes are rechecked.
    HSIZE = 1 << 20
    HMASK = HSIZE - 1
    head3 = array('i', [-1]) * HSIZE
    prev3 = array('i', [-1]) * n
    # Exact latest 2-byte occurrence is enough for the compact 2-byte short copy.
    head2 = array('i', [-1]) * 65536

    def h3(p:int) -> int:
        x = (src[p] << 16) | (src[p+1] << 8) | src[p+2]
        x ^= x >> 11
        x *= 0x45D9F3B
        x ^= x >> 13
        return x & HMASK

    def add_pos(p:int) -> None:
        if p + 1 < n:
            k2 = (src[p] << 8) | src[p+1]
            head2[k2] = p
        if p + 2 < n:
            h = h3(p)
            prev3[p] = head3[h]
            head3[h] = p

    pos = 0
    report_step = max(1, n // 160)
    report_at = report_step
    def report_progress(force=False):
        nonlocal report_at
        if progress and (force or pos >= report_at or pos >= n):
            try: progress(min(pos, n), max(1, n), 'PRS')
            except Exception: pass
            report_at = pos + report_step

    while pos < n:
        best_len = 0
        best_span = 0
        remain = n - pos
        max_len = 256 if remain >= 256 else remain

        if remain >= 3:
            h = h3(pos)
            cand = head3[h]
            checked = 0
            while cand >= 0 and checked < max_candidates:
                span = pos - cand
                if span > 8192:
                    # Chain is newest first, so older candidates are farther away.
                    break
                nxt = prev3[cand]
                checked += 1
                if (src[cand] == src[pos] and src[cand+1] == src[pos+1]
                        and src[cand+2] == src[pos+2]):
                    ln = 3
                    # Compare against the original uncompressed bytes. Overlap is
                    # legal in PRS and the decoder reproduces it by repeated copy.
                    while ln < max_len and src[cand + ln] == src[pos + ln]:
                        ln += 1
                    if ln > best_len:
                        best_len = ln
                        best_span = span
                        if ln == max_len:
                            break
                cand = nxt

        # A short-copy command can encode a useful 2-byte match within 256 bytes.
        if best_len < 3 and remain >= 2:
            k2 = (src[pos] << 8) | src[pos+1]
            cand2 = head2[k2]
            if cand2 >= 0:
                span2 = pos - cand2
                if 1 <= span2 <= 256:
                    best_len = 2
                    best_span = span2

        # Prefer the compact 1-byte-offset command whenever the chosen match fits.
        if best_len >= 2 and best_span <= 256 and best_len <= 5:
            ln = best_len
            bw.bit(0); bw.bit(0)
            code = ln - 2
            bw.bit((code >> 1) & 1); bw.bit(code & 1)
            bw.byte((256 - best_span) & 0xFF)
            for p in range(pos, pos + ln): add_pos(p)
            pos += ln
            report_progress()
            continue

        if best_len >= 3:
            ln = best_len
            # pair==0 is the EOF marker. At the extreme -8192 distance an
            # extended-length command would also produce zero, so use <=9 bytes.
            if best_span == 8192 and ln >= 10:
                ln = 9
            if 3 <= ln <= 9:
                size_code = ln - 2
            else:
                size_code = 0
                if ln > 256: ln = 256
            upper = 8192 - best_span
            pair = (upper << 3) | size_code
            if pair != 0:
                bw.bit(0); bw.bit(1)
                bw.bytes2(pair & 0xFF, (pair >> 8) & 0xFF)
                if size_code == 0:
                    bw.byte(ln - 1)
                for p in range(pos, pos + ln): add_pos(p)
                pos += ln
                report_progress()
                continue

        # Literal byte.
        bw.bit(1); bw.byte(src[pos]); add_pos(pos); pos += 1
        report_progress()

    report_progress(force=True)
    # PRS terminator: long-copy opcode with a zero pair.
    bw.bit(0); bw.bit(1); bw.bytes2(0, 0)
    packed = bytes(bw.out)
    # Never return an invalid stream.  The caller also verifies the full RDX,
    # but keeping this local check protects loose users of the helper too.
    if verify and recvx_expand(packed, max_output=max(64 * 1024 * 1024, n + 1)) != src:
        raise ValueError('PRS compression round-trip failed')
    return packed


def probe_compressed_rdx(raw:bytes):
    try:
        dec=recvx_expand(raw)
        if len(dec)>=0x60 and u32(dec,0) in RDX_MAGIC: return dec
    except Exception:
        pass
    return None


def probe_compressed_rdx_text(raw:bytes):
    try:
        dec=recvx_expand_text_prefix(raw)
        if len(dec)>=0x60 and u32(dec,0) in RDX_MAGIC: return dec
    except Exception:
        pass
    return None

def backup_once(path:Path):
    bak=path.with_suffix(path.suffix+'.bak')
    if not bak.exists(): shutil.copy2(path,bak)
    return bak

def parse_tbl(path:Path):
    """Read a 16-bit RECVX TBL, accepting normal and byte-swapped tables.

    Some community tables are written as the two on-disk bytes (00FF=[LINE],
    0700=...) while the editor works with host-order 16-bit words
    (FF00=[LINE], 0007=...).  Detect that layout from the engine control codes
    and normalize it transparently, so a user-modified TBL can be loaded as-is.
    """
    pairs=[]
    for raw in Path(path).read_text(encoding='utf-8-sig',errors='replace').splitlines():
        line=raw.strip('\r\n')
        if '=' not in line: continue
        k,v=line.split('=',1)
        try: c=int(k.strip(),16)
        except Exception: continue
        if 0 <= c <= 0xFFFF:
            pairs.append((c,v))
    def sw16(v): return ((v & 0xFF) << 8) | ((v >> 8) & 0xFF)
    # Strong signatures from RECVX message control codes.  Use values as well
    # as numeric positions so ordinary Latin/Japanese tables are not guessed.
    normal=swapped=0
    for c,v in pairs:
        vu=v.strip().upper()
        if vu=='[LINE]':
            normal += 8 if c==0xFF00 else 0
            swapped += 8 if c==0x00FF else 0
        if v==' ':
            normal += 3 if c==0xFF01 else 0
            swapped += 3 if c==0x01FF else 0
        if v=='→':
            normal += 2 if c==0xFF04 else 0
            swapped += 2 if c==0x04FF else 0
    do_swap=swapped>normal
    c2t={}
    for c,v in pairs:
        c2t[sw16(c) if do_swap else c]=v
    t2c={}
    for c,v in c2t.items():
        if v not in t2c and v!='': t2c[v]=c
    return c2t,t2c

class Codec:
    def __init__(self,c2t,t2c):
        self.c2t=c2t; self.t2c=t2c
        self.tokens=sorted([x for x in t2c if len(x)>1 and x.startswith('[')],key=len,reverse=True)
    def decode_words(self,words):
        out=[]; i=0
        while i<len(words):
            w=words[i]
            if w==0xFF02 and i+1<len(words):
                param=words[i+1]; out.append('[PAGE]' if param==0 else f'[WAIT:{param:04X}]'); i+=2; continue
            if w==0xFF03 and i+1<len(words): out.append(f'[ITEM:{words[i+1]:04X}]'); i+=2; continue
            v=self.c2t.get(w)
            out.append(v if v not in (None,'') else f'[HEX:{w:04X}]')
            i+=1
        return ''.join(out)
    def encode_text(self,text):
        out=[]; i=0
        while i<len(text):
            if text[i] in '\r\n':
                if text[i]=='\r' and i+1<len(text) and text[i+1]=='\n': i+=1
                out.append(0xFF00); i+=1; continue
            m=re.match(r'\[(?:HEX|0x):([0-9A-Fa-f]{1,4})\]',text[i:])
            if m: out.append(int(m.group(1),16)); i+=len(m.group(0)); continue
            m=re.match(r'\[(WAIT|TIME|ITEM):([0-9A-Fa-f]{1,4})\]',text[i:],re.I)
            if m:
                op=0xFF03 if m.group(1).upper()=='ITEM' else 0xFF02
                out.extend([op,int(m.group(2),16)]); i+=len(m.group(0)); continue
            if text.startswith('[PAGE]',i): out.extend([0xFF02,0]); i+=6; continue
            matched=False
            for tok in self.tokens:
                if text.startswith(tok,i): out.append(self.t2c[tok]); i+=len(tok); matched=True; break
            if matched: continue
            ch=text[i]
            if ch in self.t2c: out.append(self.t2c[ch]); i+=1; continue
            raise ValueError(f'Unsupported character: {ch!r} (U+{ord(ch):04X})')
        return out

@dataclass
class Msg:
    block:int
    index:int
    words:list[int]
    suffix:bytes=b''

@dataclass
class AldBlock: messages:list[Msg]=field(default_factory=list)

class AldDocument:
    kind='SYSMES'
    def __init__(self,path): self.path=Path(path); self.blocks=[]; self.trailing=b''; self.load()
    def load(self):
        d=self.path.read_bytes(); self.original=d; self.blocks=[]; pos=0; bno=0
        while pos+4<=len(d):
            span=u32(d,pos)
            if span==0xFFFFFFFF: break
            base=pos+4; end=base+span
            if span<4 or end>len(d): raise ValueError('Invalid ALD')
            count=u32(d,base); table_end=base+4+count*4
            if count>100000 or table_end>end: raise ValueError('Invalid ALD table')
            ptrs=[u32(d,base+4+i*4) for i in range(count)]; msgs=[]
            for i,rel in enumerate(ptrs):
                start=base+rel; stop=base+(ptrs[i+1] if i+1<count else span)
                q=start; words=[]
                while q+2<=stop:
                    w=u16(d,q); q+=2
                    if w==0xFFFF and (not words or words[-1] not in PARAM_CODES): break
                    words.append(w)
                else: raise ValueError('ALD text terminator missing')
                msgs.append(Msg(bno,i,words,d[q:stop]))
            self.blocks.append(AldBlock(msgs)); pos=end; bno+=1
        self.trailing=d[pos:]
        if not self.blocks: raise ValueError('No ALD text blocks')
    @property
    def messages(self): return [m for b in self.blocks for m in b.messages]
    def rebuild(self):
        out=bytearray()
        for bi,b in enumerate(self.blocks):
            raws=[]
            for i,m in enumerate(b.messages):
                m.block=bi;m.index=i
                raw=b''.join(struct.pack('<H',w&0xffff) for w in m.words)+b'\xff\xff'+m.suffix; raws.append(raw)
            rel=4+4*len(raws); ptrs=[]
            for raw in raws: ptrs.append(rel); rel+=len(raw)
            payload=bytearray(struct.pack('<I',len(raws)))
            for p in ptrs: payload+=struct.pack('<I',p)
            for raw in raws: payload+=raw
            out+=struct.pack('<I',len(payload))+payload
        return bytes(out)+self.trailing
    def save(self):
        backup_once(self.path); raw=self.rebuild(); tmp=self.path.with_suffix(self.path.suffix+'.tmp'); tmp.write_bytes(raw); AldDocument(tmp); os.replace(tmp,self.path)

class RdxDocument:
    kind='RDX'
    def __init__(self,path, initial_data:bytes|bytearray|memoryview|None=None):
        self.path=Path(path); self.messages=[]; self._initial_data=bytes(initial_data) if initial_data is not None else None; self.load()
    @staticmethod
    def probe_bytes(d): return len(d)>=0x60 and u32(d,0) in RDX_MAGIC
    def load(self):
        d=self._initial_data if self._initial_data is not None else self.path.read_bytes(); self._initial_data=None; self.original=d
        if not self.probe_bytes(d): raise ValueError('Invalid RDX')
        self.text_block=u32(d,0x10); self.next_block=u32(d,0x14); self.texture_block=u32(d,0x20)
        if not (0<self.text_block<len(d)-64): raise ValueError('Invalid RDX text block')
        self.sub=[u32(d,self.text_block+i*4) for i in range(16)]
        self.text_pos=self.sub[14]
        if self.text_pos==0: self.messages=[]; self.reserved=0; return
        nominal_end=(self.sub[15] if self.sub[15] else self.next_block)
        # RECV Editor's safe-growth strategy relocates sub-block 14 to the old
        # texture-block position without rewriting sub-block 15.  In that case
        # the NEW texture block is the true upper bound for relocated text.
        if self.text_pos < nominal_end <= len(d):
            self.text_end=nominal_end
        elif self.text_pos < self.texture_block <= len(d):
            self.text_end=self.texture_block
        else:
            raise ValueError('Invalid RDX text range')
        self.reserved=self.text_end-self.text_pos
        count=u32(d,self.text_pos)
        if count>100000 or self.text_pos+4+count*4>self.text_end: raise ValueError('Invalid RDX text table')
        ptrs=[u32(d,self.text_pos+4+i*4) for i in range(count)]; msgs=[]
        for i,rel in enumerate(ptrs):
            q=self.text_pos+rel; words=[]; limit=self.text_end
            if not (self.text_pos<=q<limit): raise ValueError('Invalid RDX pointer')
            while q+2<=limit:
                w=u16(d,q); q+=2
                if w==0xFFFF and (not words or words[-1] not in PARAM_CODES): break
                words.append(w)
            else: raise ValueError('RDX text terminator missing')
            msgs.append(Msg(0,i,words))
        self.messages=msgs
    def build_text_block(self):
        raws=[]; offsets=[]; p=0
        for m in self.messages:
            raw=b''.join(struct.pack('<H',w&0xffff) for w in m.words)+b'\xff\xff'; raws.append(raw); offsets.append(p); p+=len(raw)
        out=bytearray(struct.pack('<I',len(raws)))
        for p in offsets: out+=struct.pack('<I',p+4+4*len(raws))
        for raw in raws: out+=raw
        while len(out)%4: out.append(0xff)
        return bytes(out)
    def rebuild(self):
        if not self.text_pos: return self.original
        text=self.build_text_block(); d=bytearray(self.original)
        if len(text)<=self.reserved:
            d[self.text_pos:self.text_pos+self.reserved]=text+b'\x00'*(self.reserved-len(text)); return bytes(d)
        old_tex=self.texture_block
        if not (0<old_tex<=len(d)): raise ValueError('RDX texture block unavailable for safe growth')
        tex=bytes(d[old_tex:])
        d[self.text_pos:self.text_end]=b'\x00'*self.reserved
        struct.pack_into('<I',d,self.text_block+14*4,old_tex)
        # place new text where texture block used to be, then append relocated texture block
        prefix=bytes(d[:old_tex]); out=bytearray(prefix); out+=text
        new_tex=align(len(out),16); out+=b'\x00'*(new_tex-len(out)); out+=tex
        struct.pack_into('<I',out,0x20,new_tex)
        if len(tex)>=4:
            cnt=u32(tex,0)
            if cnt<10000 and 4+cnt*4<=len(tex):
                delta=new_tex-old_tex
                for i in range(cnt):
                    off=new_tex+4+i*4; ptr=u32(out,off); struct.pack_into('<I',out,off,ptr+delta)
        return bytes(out)
    def save(self):
        backup_once(self.path); raw=self.rebuild(); tmp=self.path.with_suffix(self.path.suffix+'.tmp'); tmp.write_bytes(raw); RdxDocument(tmp); os.replace(tmp,self.path); self.load()

class AfsArchive:
    ALIGN=0x800
    def __init__(self,path):
        self.path=Path(path); self.data=self.path.read_bytes()
        if self.data[:4]!=b'AFS\0': raise ValueError('Not AFS')
        self.count=u32(self.data,4)
        if self.count>100000 or 8+self.count*8>len(self.data): raise ValueError('Invalid AFS')
        self.entries=[struct.unpack_from('<II',self.data,8+i*8) for i in range(self.count)]
        ex=8+self.count*8
        self.dir_ptr_pos=None; self.dir_off=0; self.dir_size=0
        # AFS permits padding between the TOC and the optional filename-directory
        # pointer. Search the header area for a valid directory descriptor rather
        # than assuming it is immediately after the TOC.
        valid_offsets=[off for off,size in self.entries if off and size]
        first_data=min(valid_offsets) if valid_offsets else len(self.data)
        scan_end=min(first_data, ex+0x1000, len(self.data)-7)
        for q in range(ex, max(ex,scan_end)+1, 4):
            if q+8>len(self.data): break
            doff,dsize=struct.unpack_from('<II',self.data,q)
            if not doff or dsize < self.count*48 or doff+dsize>len(self.data):
                continue
            # First filename should be printable or empty. This rejects accidental
            # offset/size pairs in padding while accepting unnamed directories.
            rec=self.data[doff:doff+32]
            raw=bytes(rec).split(b'\0',1)[0]
            if raw and any(c<0x20 for c in raw):
                continue
            self.dir_ptr_pos=q; self.dir_off=doff; self.dir_size=dsize; break
    def entry_bytes(self,index):
        off,size=self.entries[index]
        if off+size>len(self.data): raise ValueError('AFS entry outside file')
        return bytes(self.data[off:off+size])
    def entry_name(self,index):
        if not (0<=index<self.count): return ''
        if self.dir_off and self.dir_size>=self.count*48 and self.dir_off+self.dir_size<=len(self.data):
            rec=self.data[self.dir_off+index*48:self.dir_off+(index+1)*48]
            raw=bytes(rec[:32]).split(b'\0',1)[0]
            if raw:
                for enc in ('ascii','cp932','latin1'):
                    try:
                        n=raw.decode(enc).strip()
                        if n:return n
                    except Exception:pass
        return ''
    def extract_rdx(self,outdir,progress=None,cancel=None):
        outdir=Path(outdir); outdir.mkdir(parents=True,exist_ok=True); paths=[]
        for i in range(self.count):
            if cancel and cancel(): raise InterruptedError('RDX scan cancelled')
            name=self.entry_name(i) or f'RDX_{i:04d}.rdx'
            if progress: progress(i,self.count,name)
            raw=self.entry_bytes(i); compressed=False
            if not (len(raw)>=4 and u32(raw,0) in RDX_MAGIC):
                dec=probe_compressed_rdx(raw)
                if dec is None: continue
                raw=dec; compressed=True
            stem=Path(name).stem or f'RDX_{i:04d}'
            safe=re.sub(r'[^A-Za-z0-9_.-]+','_',stem)
            p=outdir/f'{safe}.rdx'
            if p.exists(): p=outdir/f'{safe}_{i:04d}.rdx'
            p.write_bytes(raw); paths.append((p,i,compressed,name))
        if progress: progress(self.count,self.count,'Done')
        return paths
    def replace_entry(self,index,new_raw:bytes):
        if not 0<=index<self.count: raise IndexError(index)
        new_raw=bytes(new_raw)
        old_off,old_size=self.entries[index]
        # Fixed-size asset replacement (font TM2, etc.) can be patched in place.
        # This preserves every AFS offset, directory record, padding byte and
        # total file size, and avoids rebuilding a multi-megabyte archive.
        if len(new_raw)==old_size and old_off+old_size<=len(self.data):
            original=bytes(self.data[old_off:old_off+old_size])
            if original==new_raw:
                return
            backup_once(self.path)
            out=bytearray(self.data); out[old_off:old_off+old_size]=new_raw
            tmp=self.path.with_suffix(self.path.suffix+'.tmp')
            tmp.write_bytes(out)
            check=AfsArchive(tmp)
            if check.entries!=self.entries or len(check.data)!=len(self.data):
                try: tmp.unlink()
                except OSError: pass
                raise ValueError('AFS fixed-size patch verification failed')
            os.replace(tmp,self.path); self.__init__(self.path)
            return
        payloads=[self.entry_bytes(i) for i in range(self.count)]
        payloads[index]=new_raw
        valid=[off for off,size in self.entries if off and size]
        first=min(valid) if valid else align(16+self.count*8,self.ALIGN)
        header=bytearray(self.data[:first])
        if len(header)<8+self.count*8+8: header.extend(b'\0'*(8+self.count*8+8-len(header)))
        out=bytearray(header)
        pos=first; new_entries=[]
        for raw in payloads:
            pos=align(pos,self.ALIGN)
            if len(out)<pos: out.extend(b'\0'*(pos-len(out)))
            off=pos; out.extend(raw); pos=len(out); new_entries.append((off,len(raw)))
        if self.dir_off and self.dir_size and self.dir_off+self.dir_size<=len(self.data):
            block=bytearray(self.data[self.dir_off:self.dir_off+self.dir_size])
            # Standard 48-byte AFS filename records keep file size at +44.
            if len(block)>=self.count*48:
                for i,(_off,size) in enumerate(new_entries):
                    struct.pack_into('<I',block,i*48+44,size)
            pos=align(len(out),self.ALIGN)
            if len(out)<pos: out.extend(b'\0'*(pos-len(out)))
            ndoff=len(out); out.extend(block)
            ptr=self.dir_ptr_pos if self.dir_ptr_pos is not None else 8+self.count*8
            if ptr+8>len(out): raise ValueError('AFS directory pointer is outside header')
            struct.pack_into('<II',out,ptr,ndoff,len(block))
        elif self.dir_ptr_pos is not None:
            struct.pack_into('<II',out,self.dir_ptr_pos,0,0)
        for i,(off,size) in enumerate(new_entries): struct.pack_into('<II',out,8+i*8,off,size)
        backup_once(self.path); tmp=self.path.with_suffix(self.path.suffix+'.tmp'); tmp.write_bytes(out); AfsArchive(tmp); os.replace(tmp,self.path); self.__init__(self.path)

class Elf32:
    """Small ELF32 little-endian helper used by the RECVX PS2 build.

    The retail PAL executable keeps full section/symbol/relocation metadata.
    That lets us relocate text safely instead of overwriting the bytes that
    happen to follow a longer translated string.
    """
    def __init__(self,path):
        self.path=Path(path); self.data=bytearray(self.path.read_bytes())
        self.sections=[]; self.program_headers=[]; self.symbols={}; self.symbol_records={}; self.symbol_list=[]; self.relocs={}
        self._parse()
    def _parse(self):
        d=self.data
        if len(d)<52 or d[:4]!=b'\x7fELF' or d[4]!=1 or d[5]!=1: raise ValueError('Not ELF32 LE')
        self.phoff=u32(d,0x1c); self.shoff=u32(d,0x20); self.phents=u16(d,0x2a); self.phnum=u16(d,0x2c)
        self.shents=u16(d,0x2e); self.shnum=u16(d,0x30); self.shstrndx=u16(d,0x32)
        if self.phoff and self.phents>=32 and self.phoff+self.phents*self.phnum<=len(d):
            for i in range(self.phnum):
                o=self.phoff+i*self.phents
                typ,off,vaddr,paddr,filesz,memsz,flags,al=struct.unpack_from('<IIIIIIII',d,o)
                self.program_headers.append({'index':i,'hdr_off':o,'type':typ,'off':off,'vaddr':vaddr,'paddr':paddr,'filesz':filesz,'memsz':memsz,'flags':flags,'align':al})
        if not self.shoff or self.shents<40 or self.shoff+self.shents*self.shnum>len(d): raise ValueError('ELF symbols unavailable')
        rawsecs=[]
        for i in range(self.shnum):
            o=self.shoff+i*self.shents
            v=struct.unpack_from('<IIIIIIIIII',d,o)
            rawsecs.append({'index':i,'hdr_off':o,'name_off':v[0],'type':v[1],'flags':v[2],'addr':v[3],'off':v[4],'size':v[5],'link':v[6],'info':v[7],'align':v[8],'entsize':v[9]})
        shstr=b''
        if 0<=self.shstrndx<len(rawsecs):
            ss=rawsecs[self.shstrndx]
            if ss['off']+ss['size']<=len(d): shstr=bytes(d[ss['off']:ss['off']+ss['size']])
        for sec in rawsecs:
            no=sec['name_off']; name=''
            if no<len(shstr):
                e=shstr.find(b'\0',no)
                if e>=0:name=shstr[no:e].decode('ascii','ignore')
            sec['name']=name; self.sections.append(sec)
        # Parse all symbol tables, retaining symbol-table file offsets so a
        # relocated symbol can be updated for subsequent edits/reopens.
        for sec in self.sections:
            if sec['type']!=2 or sec['link']>=len(self.sections): continue
            ss=self.sections[sec['link']]
            if ss['off']+ss['size']>len(d): continue
            st=bytes(d[ss['off']:ss['off']+ss['size']]); es=sec['entsize'] or 16
            local=[]
            for idx,o in enumerate(range(sec['off'],sec['off']+sec['size'],es)):
                if o+16>len(d): break
                no,val,size=struct.unpack_from('<III',d,o); info=d[o+12]; other=d[o+13]; sh=u16(d,o+14)
                name=''
                if no<len(st):
                    e=st.find(b'\0',no)
                    if e>=0:name=st[no:e].decode('ascii','ignore')
                rec={'index':idx,'sym_off':o,'name':name,'value':val,'size':size,'info':info,'other':other,'shndx':sh,'symtab_sec':sec['index']}
                local.append(rec)
                if name:
                    self.symbol_records[name]=rec
                    if sh<len(self.sections):
                        tgt=self.sections[sh]
                        if tgt['type']!=8 and val>=tgt['addr']:
                            fo=tgt['off']+val-tgt['addr']
                            if 0<=fo<=len(d) and fo+size<=len(d): self.symbols[name]=(fo,size)
            # The retail file has one .symtab. Keep its order for r_info lookup.
            if len(local)>len(self.symbol_list): self.symbol_list=local
        # Parse SHT_REL and key relocations by the referenced symbol name.
        for sec in self.sections:
            if sec['type']!=9 or (sec['entsize'] or 8)<8: continue
            es=sec['entsize'] or 8
            for o in range(sec['off'],sec['off']+sec['size'],es):
                if o+8>len(d):break
                roff,rinfo=struct.unpack_from('<II',d,o); si=rinfo>>8; typ=rinfo&0xff
                if 0<=si<len(self.symbol_list):
                    name=self.symbol_list[si].get('name','')
                    if name:self.relocs.setdefault(name,[]).append({'offset':roff,'type':typ,'rel_off':o,'sym_index':si})
    def resolve(self,name):
        if name in self.symbols:return name
        for k in self.symbols:
            if k.startswith(name+'$'): return k
        return None
    def resolve_record(self,name):
        k=name if name in self.symbol_records else None
        if k is None:
            for x in self.symbol_records:
                if x.startswith(name+'$'): k=x; break
        return (k,self.symbol_records.get(k)) if k else (None,None)
    def has(self,name): return self.resolve(name) is not None
    def bytes_at(self,name):
        k=self.resolve(name)
        if not k: raise KeyError(name)
        o,s=self.symbols[k]; return bytes(self.data[o:o+s])
    def patch(self,name,raw):
        k=self.resolve(name)
        if not k: raise KeyError(name)
        o,s=self.symbols[k]
        if len(raw)!=s: raise ValueError('size mismatch')
        self.data[o:o+s]=raw
    def va_to_offset(self,va:int):
        for sec in self.sections:
            if sec['type']==8 or not (sec['flags']&2): continue
            if sec['addr']<=va<sec['addr']+sec['size']:
                off=sec['off']+(va-sec['addr'])
                if 0<=off<len(self.data):return off
        for ph in self.program_headers:
            if ph['type']==1 and ph['vaddr']<=va<ph['vaddr']+ph['filesz']:
                off=ph['off']+(va-ph['vaddr'])
                if 0<=off<len(self.data):return off
        return None
    def offset_to_va(self,off:int):
        for sec in self.sections:
            if sec['type']==8 or not (sec['flags']&2): continue
            if sec['off']<=off<sec['off']+sec['size']: return sec['addr']+(off-sec['off'])
        for ph in self.program_headers:
            if ph['type']==1 and ph['off']<=off<ph['off']+ph['filesz']: return ph['vaddr']+(off-ph['off'])
        return None
    def symbol_va(self,name):
        k,rec=self.resolve_record(name)
        if not rec: raise KeyError(name)
        return int(rec['value'])
    def _set_section_range(self,sec,new_off,new_size):
        struct.pack_into('<II',self.data,sec['hdr_off']+16,new_off,new_size)
        sec['off']=new_off; sec['size']=new_size
    def _set_program_range(self,ph,new_off,new_filesz,new_memsz=None):
        struct.pack_into('<I',self.data,ph['hdr_off']+4,new_off)
        struct.pack_into('<I',self.data,ph['hdr_off']+16,new_filesz)
        struct.pack_into('<I',self.data,ph['hdr_off']+20,new_filesz if new_memsz is None else new_memsz)
        ph['off']=new_off; ph['filesz']=new_filesz; ph['memsz']=new_filesz if new_memsz is None else new_memsz
    def alloc_heap(self,raw:bytes,alignment:int=4):
        raw=bytes(raw)
        heap=next((s for s in self.sections if s.get('name')=='heap'),None)
        if heap is None: raise ValueError('ELF heap section unavailable')
        ph=next((x for x in self.program_headers if x['type']==1 and x['vaddr']==heap['addr']),None)
        if ph is None: raise ValueError('ELF heap load segment unavailable')
        if heap['size']==0:
            start=align(len(self.data),max(16,alignment))
            if len(self.data)<start:self.data.extend(b'\0'*(start-len(self.data)))
            heap_off=start; heap_size=0
            self._set_section_range(heap,heap_off,0); self._set_program_range(ph,heap_off,0,0)
        else:
            heap_off=heap['off']; heap_size=heap['size']
            expected=heap_off+heap_size
            if expected>len(self.data): raise ValueError('ELF heap section outside file')
            # Our private text pool is always appended at EOF. Refuse to create a
            # non-contiguous load segment if a foreign tool changed that layout.
            if expected!=len(self.data):
                # Trailing zero padding is harmless and can be absorbed.
                if any(self.data[expected:]): raise ValueError('ELF heap text pool is not at end of file')
                heap_size=len(self.data)-heap_off
        pos=align(heap_off+heap_size,alignment)
        if len(self.data)<pos:self.data.extend(b'\0'*(pos-len(self.data)))
        va=heap['addr']+(pos-heap_off)
        self.data.extend(raw)
        new_size=len(self.data)-heap_off
        self._set_section_range(heap,heap_off,new_size); self._set_program_range(ph,heap_off,new_size,new_size)
        return va
    def _patch_relocations_for_symbol(self,name,old_value,new_value):
        rels=sorted(self.relocs.get(name,[]),key=lambda x:x['offset'])
        if not rels: raise ValueError(f'No relocations for ELF text symbol: {name}')
        pending=[]; patched=0
        for r in rels:
            fo=self.va_to_offset(r['offset'])
            if fo is None or fo+4>len(self.data): continue
            typ=r['type']
            if typ==5: # R_MIPS_HI16
                pending.append((r,fo)); continue
            if typ==6: # R_MIPS_LO16; applies to pending HI16 entries
                loww=u32(self.data,fo); lowimm=loww&0xffff; signed_low=lowimm if lowimm<0x8000 else lowimm-0x10000
                if pending:
                    first_hi=u32(self.data,pending[0][1])&0xffff
                    current=((first_hi<<16)+signed_low)&0xffffffff
                    addend=(current-old_value)&0xffffffff
                else:
                    addend=0
                target=(new_value+addend)&0xffffffff
                new_hi=((target+0x8000)>>16)&0xffff; new_lo=target&0xffff
                for _rh,hfo in pending:
                    hw=u32(self.data,hfo); struct.pack_into('<I',self.data,hfo,(hw&0xffff0000)|new_hi); patched+=1
                pending.clear()
                struct.pack_into('<I',self.data,fo,(loww&0xffff0000)|new_lo); patched+=1
                continue
            if typ==2: # R_MIPS_32, preserve the linked addend
                w=u32(self.data,fo); addend=(w-old_value)&0xffffffff
                struct.pack_into('<I',self.data,fo,(new_value+addend)&0xffffffff); patched+=1
                continue
            # Text objects in this build use HI16/LO16 (and occasionally 32).
            raise ValueError(f'Unsupported relocation type {typ} for {name}')
        if pending: raise ValueError(f'Unpaired HI16 relocation for {name}')
        if patched==0: raise ValueError(f'No patchable relocations for {name}')
        return patched
    def relocate_symbol(self,name,new_value:int,new_size:int):
        k,rec=self.resolve_record(name)
        if not rec: raise KeyError(name)
        old=int(rec['value'])
        self._patch_relocations_for_symbol(k,old,new_value)
        heap=next((s for s in self.sections if s.get('name')=='heap'),None)
        if heap is None: raise ValueError('ELF heap section unavailable')
        struct.pack_into('<I',self.data,rec['sym_off']+4,new_value)
        struct.pack_into('<I',self.data,rec['sym_off']+8,new_size)
        struct.pack_into('<H',self.data,rec['sym_off']+14,heap['index'])
        rec['value']=new_value; rec['size']=new_size; rec['shndx']=heap['index']
        self.symbols[k]=(self.va_to_offset(new_value),new_size)
    @staticmethod
    def _lui_float_int(imm:int):
        bits=(imm & 0xffff)<<16
        f=struct.unpack('<f',struct.pack('<I',bits))[0]
        if f==f and 1.0<=f<=64.0 and float(int(f))==f:return int(f)
        return None
    def _space_sites(self):
        per={}
        for name in ('bhControlMessage','bhMesLen'):
            k=self.resolve(name)
            if not k: continue
            off,size=self.symbols[k]; raw=self.data[off:off+size]; ff=[]
            for j in range(0,len(raw)-3,4):
                w=u32(raw,j)
                if (w>>26)==0x0d and (w&0xffff)==0xff01: ff.append(j)
            if not ff: continue
            start=ff[0]; end=min(len(raw),start+0x220); sites=[]
            for j in range(start,end,4):
                w=u32(raw,j)
                if (w>>26)!=0x0f: continue
                v=self._lui_float_int(w&0xffff)
                if v is not None: sites.append((off+j,w&0xffff,v))
            per[name]=sites
        if not per:return [],[]
        sets=[{v for _,_,v in a} for a in per.values() if a]
        common=set.intersection(*sets) if len(sets)>=2 else set(); wanted={14,16}|common; sites=[]
        for a in per.values(): sites.extend((o,im,v) for o,im,v in a if v in wanted)
        return sites,sorted({v for _,_,v in sites})
    def patch_space_advance(self,value:int):
        if not (1<=value<=64): raise ValueError('Space: 1..64')
        bits=struct.unpack('<I',struct.pack('<f',float(value)))[0]
        if bits&0xffff: raise ValueError('Unsupported space value')
        imm=(bits>>16)&0xffff; sites,_=self._space_sites()
        if not sites: raise ValueError('Space patch pattern not found')
        for off,_oldimm,_oldv in sites:
            w=u32(self.data,off); struct.pack_into('<I',self.data,off,(w&0xffff0000)|imm)
        if len(sites)<2: raise ValueError('Space patch pattern not found')
        return len(sites)
    def read_space_advances(self):
        sites,vals=self._space_sites()
        if not sites:return []
        counts={}
        for _,_,v in sites:counts[v]=counts.get(v,0)+1
        repeated=[v for v,c in counts.items() if c>=2]
        return sorted(repeated) if repeated else vals
    def save(self):
        backup_once(self.path); tmp=self.path.with_suffix(self.path.suffix+'.tmp'); tmp.write_bytes(self.data); Elf32(tmp); os.replace(tmp,self.path)


@dataclass
class ElfTextMsg:
    block:int
    index:int
    words:list[int]
    symbol:str
    entry:int=-1
    strategy:str='direct'
    original_words:list[int]=field(default_factory=list)
    capacity:int=0
    display_name:str='ELF'


class ElfTextDocument:
    """Editable TBL text found in the PAL PS2 ELF.

    Save/load messages use a relative-offset table and can therefore point to
    newly allocated text in the ELF's existing zero-sized ``heap`` PT_LOAD.
    Other named message objects are relocated using the executable's own MIPS
    relocation records. This permits shorter/longer strings without overwriting
    neighboring code/data.
    """
    kind='ELF'
    SAVELOAD={'ENG':'SaveLoadMessage_UK','FRA':'SaveLoadMessage_FRA','GER':'SaveLoadMessage_GER','SPA':'SaveLoadMessage_SPA'}
    def __init__(self,path,lang,codec):
        self.path=Path(path); self.lang=(lang or 'ENG').upper(); self.codec=codec; self.messages=[]; self.load()
    @staticmethod
    def _symbol_lang(name):
        u=name.upper()
        if '_FRA' in u or u.startswith('FRA_'):return 'FRA'
        if '_GER' in u or u.startswith('GER_'):return 'GER'
        if '_SPA' in u or u.startswith('SPA_'):return 'SPA'
        if '_UK' in u or 'UK_IRE_USA' in u or '_ORIGIN' in u:return 'ENG'
        return None
    @staticmethod
    def _text_hint(name):
        u=name.upper()
        return any(x in u for x in ('MES','MESSAGE','PAUSE','SAVE','LOAD','TEXT','CAPTION','TITLE'))
    def _read_words_va(self,elf,va,limit_words=16384):
        off=elf.va_to_offset(va)
        if off is None: raise ValueError('ELF text pointer outside loaded file data')
        words=[]; p=off
        while p+2<=len(elf.data) and len(words)<limit_words:
            w=u16(elf.data,p); p+=2
            if w==0xffff and (not words or words[-1] not in PARAM_CODES): return words,p-off
            words.append(w)
        raise ValueError('ELF text terminator missing')
    def _is_text_words(self,words,name=''):
        if not words:return False
        normal=0; alpha=0; spaces=0
        for i,w in enumerate(words):
            if w in PARAM_CODES:
                continue
            if w>=0xfe00:
                if w not in self.codec.c2t and w not in (0xFE00,0xFE01,0xFE02,0xFE03,0xFE04,0xFE05,0xFE08,0xFE09,0xFF00,0xFF01,0xFF04): return False
                continue
            v=self.codec.c2t.get(w)
            if not v:return False
            normal+=1
            if v==' ':spaces+=1
            alpha+=sum(ch.isalpha() for ch in v)
        if self._text_hint(name):return normal>=1 and (alpha>=1 or spaces>0)
        if normal<2:return False
        return normal>=8 and alpha>=max(4,normal//2)
    def _direct_symbol_message(self,elf,name,idx):
        k,rec=elf.resolve_record(name)
        if not rec or rec['size']<4 or rec['size']>0x10000 or rec['size']%2:return None
        if (rec['info']&0xf)!=1:return None # STT_OBJECT
        off=elf.va_to_offset(rec['value'])
        if off is None or off+rec['size']>len(elf.data):return None
        words=[]; p=off; end=off+rec['size']; found=False
        while p+2<=end:
            w=u16(elf.data,p); p+=2
            if w==0xffff and (not words or words[-1] not in PARAM_CODES): found=True; break
            # A direct text object starts at byte zero and contains only TBL codes.
            if w==0 or (w<0xfe00 and not self.codec.c2t.get(w)):
                return None
            if w>=0xfe00 and w not in self.codec.c2t and w not in (0xFE00,0xFE01,0xFE02,0xFE03,0xFE04,0xFE05,0xFE08,0xFE09,0xFF00,0xFF01,0xFF02,0xFF03,0xFF04):return None
            words.append(w)
        if not found:return None
        # Named message objects may intentionally be shortened to an empty
        # string. Keep them discoverable after save so a later edit can grow
        # them again without losing the relocation metadata.
        if words and not self._is_text_words(words,name):return None
        if not words and not self._text_hint(name):return None
        return ElfTextMsg(0,idx,words,k,-1,'direct',list(words),int(rec['size']),k)
    def load(self):
        elf=Elf32(self.path); out=[]; seen=set(); idx=0
        # Structured save/load table: u32 count + u32 offsets relative to the
        # table base. Offsets can safely target the heap text pool after edits.
        sname=self.SAVELOAD.get(self.lang)
        if sname and elf.has(sname):
            base=elf.symbol_va(sname); boff=elf.va_to_offset(base)
            if boff is not None and boff+4<=len(elf.data):
                count=u32(elf.data,boff)
                if 0<count<=512 and boff+4+count*4<=len(elf.data):
                    for ent in range(count):
                        rel=u32(elf.data,boff+4+ent*4); va=(base+rel)&0xffffffff
                        try:words,_span=self._read_words_va(elf,va)
                        except Exception:continue
                        m=ElfTextMsg(0,idx,words,sname,ent,'rel_table',list(words),0,f'{sname}[{ent:02d}]')
                        out.append(m);seen.add((sname,ent));idx+=1
        # All named 16-bit TBL message objects for the chosen PAL language.
        skip=set(self.SAVELOAD.values())
        records=sorted(elf.symbol_records.items(),key=lambda kv:(kv[1]['value'],kv[0]))
        for name,rec in records:
            if name in skip:continue
            sl=self._symbol_lang(name)
            if sl and sl!=self.lang:continue
            # Common objects are accepted only with a text-like symbol name.
            if not sl and not self._text_hint(name):continue
            m=self._direct_symbol_message(elf,name,idx)
            if m is None:continue
            out.append(m);seen.add((name,-1));idx+=1
        self.messages=out
    def _encode_raw(self,words):
        return b''.join(struct.pack('<H',w&0xffff) for w in words)+b'\xff\xff'
    def _key(self,m):return (m.symbol,m.entry)
    def save(self):
        changed=[m for m in self.messages if list(m.words)!=list(m.original_words)]
        if not changed:return
        elf=Elf32(self.path)
        for m in changed:
            raw=self._encode_raw(m.words)
            if m.strategy=='rel_table':
                base=elf.symbol_va(m.symbol); boff=elf.va_to_offset(base)
                if boff is None:raise ValueError('ELF SaveLoad table unavailable')
                count=u32(elf.data,boff)
                if not 0<=m.entry<count:raise ValueError('ELF SaveLoad index changed')
                newva=elf.alloc_heap(raw,2); rel=(newva-base)&0xffffffff
                struct.pack_into('<I',elf.data,boff+4+m.entry*4,rel)
            else:
                k,rec=elf.resolve_record(m.symbol)
                if not rec:raise ValueError(f'ELF symbol disappeared: {m.symbol}')
                oldva=int(rec['value']); cap=int(rec['size']); off=elf.va_to_offset(oldva)
                if off is None:raise ValueError(f'ELF symbol outside file: {m.symbol}')
                if len(raw)<=cap:
                    elf.data[off:off+cap]=raw+b'\0'*(cap-len(raw))
                else:
                    newva=elf.alloc_heap(raw,2)
                    elf.relocate_symbol(k,newva,len(raw))
        tmp=self.path.with_suffix(self.path.suffix+'.elftexttmp')
        try:
            tmp.write_bytes(elf.data)
            verify=ElfTextDocument(tmp,self.lang,self.codec)
            got={verify._key(x):x for x in verify.messages}
            for m in changed:
                v=got.get(self._key(m))
                if v is None or list(v.words)!=list(m.words):raise ValueError(f'ELF text verification failed: {m.display_name}')
            Elf32(tmp)
            backup_once(self.path); os.replace(tmp,self.path)
        finally:
            try:
                if tmp.exists():tmp.unlink()
            except OSError:pass
        self.load()

class FontData:
    REQUIRED=('FontSz_Origin','FontSz_FRA_SPA_GER')
    OPTIONAL=('mes_spos_Origin','mes_spos_PAL','set_pos_Origin','set_pos_PAL')
    def __init__(self,elf):
        self.elf=elf
        if not any(elf.has(x) for x in self.REQUIRED): raise ValueError('Font table not found')
        self.origin=list(elf.bytes_at('FontSz_Origin')) if elf.has('FontSz_Origin') else []
        self.eu=list(elf.bytes_at('FontSz_FRA_SPA_GER')) if elf.has('FontSz_FRA_SPA_GER') else []
        self.positions={}
        for n in self.OPTIONAL:
            if elf.has(n):
                raw=elf.bytes_at(n)
                if len(raw)%4==0:self.positions[n]=list(struct.unpack('<'+'f'*(len(raw)//4),raw))
        sp=elf.read_space_advances(); self.space_advance=sp[0] if sp else 14; self.dirty=False
    def save(self):
        if self.origin and self.elf.has('FontSz_Origin'): self.elf.patch('FontSz_Origin',bytes(max(0,min(255,int(x))) for x in self.origin))
        if self.eu and self.elf.has('FontSz_FRA_SPA_GER'): self.elf.patch('FontSz_FRA_SPA_GER',bytes(max(0,min(255,int(x))) for x in self.eu))
        for n,v in self.positions.items(): self.elf.patch(n,struct.pack('<'+'f'*len(v),*v))
        self.elf.patch_space_advance(int(self.space_advance)); self.elf.save(); self.dirty=False
