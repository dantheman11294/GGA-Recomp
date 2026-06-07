import struct

def verify_table(rom, start_offset):
    print(f"--- Verifying table at 0x{start_offset:X} ---")
    for i in range(20):
        offset = start_offset + (i * 8)
        w0, w1 = struct.unpack_from('>II', rom, offset)
        print(f"Entry {i:02d} (@0x{offset:X}): w0=0x{w0:08X}, w1=0x{w1:08X}")
        # Stop if we hit a likely terminator
        if w0 == 0 and w1 == 0:
            print("Found terminator.")
            break

if __name__ == "__main__":
    rom = open("gga.us.z64", "rb").read()
    verify_table(rom, 0x3A5C)
