import struct

def find_table(rom):
    # Scan for pairs of offsets (w0, w1) where w0 < w1 and both are within ROM size
    # A valid file table usually starts with a few small, sequential offsets.
    for i in range(0x1000, 0x100000, 4):
        try:
            w0, w1 = struct.unpack_from('>II', rom, i)
            # Filter out obvious junk: look for offsets starting around 0x1000
            if 0x1000 < w0 < 0x800000 and w0 < w1 < 0xF00000:
                print(f"Possible table entry at 0x{i:X}: w0=0x{w0:08X}, w1=0x{w1:08X}")
        except:
            continue

if __name__ == "__main__":
    rom = open("gga.us.z64", "rb").read()
    find_table(rom)
