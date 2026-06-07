#!/usr/bin/env python3
"""Decompress GGA ROM. Nisitenma-Ichigo table @0x2C794.
   bit31 of size word: set=>zlib, clear=>LZKN64. Decompress in-place into shared buffer
   (mirrors runtime's output_rom.subspan(rom_offset) so back-refs see prior files)."""
import struct, sys, zlib

GGA_FILE_TABLE_OFFSET = 0x2C794
MAXIMUM_ROM_SIZE = 0x4000000

def lzkn64_decompress_into(src, out, dst):
    """Decompress LZKN64 'src' (starts with 4-byte size word) writing into 'out' at 'dst'.
    Back-references read from 'out' (the shared buffer), matching the runtime. Returns bytes written."""
    ip = 4
    start = dst
    op = dst
    csize = struct.unpack_from('>I', src, 0)[0] & 0x7FFFFFFF
    while ip < csize:
        cmd = src[ip]; ip += 1
        if cmd <= 0x7F:
            length = (cmd & 0x7C) >> 2
            ofb = (cmd & 0x03) << 8
            osb = src[ip]; ip += 1
            off = (ofb | osb) & 0x3FF
            length += 2
            for _ in range(length):
                out[op] = out[op - off]; op += 1
        elif cmd <= 0x9F:
            length = cmd & 0x1F
            for _ in range(length):
                out[op] = src[ip]; op += 1; ip += 1
        elif cmd <= 0xDF:
            length = cmd & 0x1F; val = src[ip]; ip += 1
            length += 2
            for _ in range(length):
                out[op] = val; op += 1
        elif cmd <= 0xFE:
            length = cmd & 0x1F; length += 2
            for _ in range(length):
                out[op] = 0; op += 1
        elif cmd == 0xFF:
            length = src[ip] & 0xFF; ip += 1; length += 2
            for _ in range(length):
                out[op] = 0; op += 1
    return op - start

def decompress_rom(rom, fto):
    out = bytearray(MAXIMUM_ROM_SIZE); out[:len(rom)] = rom
    rom_offset = struct.unpack_from('>I', rom, fto)[0] & 0x7FFFFFFF
    entry = fto; files=0; zc=0; lc=0; rc=0
    while True:
        w0 = struct.unpack_from('>I', rom, entry)[0]
        w1 = struct.unpack_from('>I', rom, entry+4)[0]
        if (w0 & 0x7FFFFFFF)==0 or (w0 & 0x7FFFFFFF) >= len(rom): break
        comp=(w0>>31)&1; foff=w0&0x7FFFFFFF; nextoff=w1&0x7FFFFFFF; fsize=nextoff-foff
        if fsize==0:
            struct.pack_into('>I',out,entry,rom_offset)
            struct.pack_into('>I',out,entry+4,rom_offset)
            entry+=4; files+=1; continue
        if comp:
            # detect format by the bytes AFTER the 4-byte size word
            tag = rom[foff+4]
            if tag == 0x78:           # zlib magic (0x78 0x9C / 0x78 0xDA)
                d = zlib.decompressobj()
                dec = d.decompress(bytes(rom[foff+4:foff+4+fsize])) + d.flush()
                out[rom_offset:rom_offset+len(dec)] = dec
                written = len(dec); zc+=1
            else:                     # lzkn64, in-place
                written = lzkn64_decompress_into(rom[foff:], out, rom_offset); lc+=1
        else:
            out[rom_offset:rom_offset+fsize] = rom[foff:foff+fsize]
            written = fsize; rc+=1
        struct.pack_into('>I',out,entry,rom_offset)
        nxt = rom_offset + ((written+0xF)&~0xF)
        struct.pack_into('>I',out,entry+4,nxt)
        rom_offset = nxt; entry+=4; files+=1
    return out, rom_offset, files, zc, lc, rc

def main():
    inp=sys.argv[1] if len(sys.argv)>1 else "gga.us.z64"
    outp=sys.argv[2] if len(sys.argv)>2 else "gga.us.decompressed.z64"
    rom=open(inp,"rb").read()
    assert len(rom)==0x1000000
    print(f"game code @0x3B: {bytes(rom[0x3B:0x3F])!r}")
    out,fsz,files,zc,lc,rc=decompress_rom(rom,GGA_FILE_TABLE_OFFSET)
    print(f"processed {files} entries: {zc} zlib, {lc} lzkn64, {rc} raw; final=0x{fsz:X}")
    fs=fsz-1
    for s in (1,2,4,8,16): fs|=fs>>s
    fs+=1; out=out[:fs]
    print(f"rounded size=0x{fs:X}")
    open(outp,"wb").write(out); print(f"wrote {outp}")

if __name__ == "__main__":
    main()
