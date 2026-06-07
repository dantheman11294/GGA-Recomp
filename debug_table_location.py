import struct

def debug_table(rom_path, target_start):
    with open(rom_path, "rb") as f:
        rom = f.read()
    
    # We suspect the table might be just before the data segment
    # Let's look at a 512-byte window before 0xA50800
    search_start = target_start - 0x200
    print(f"--- Scanning for table entries pointing to 0x{target_start:X} ---")
    
    for i in range(0, 0x200, 4):
        offset = search_start + i
        val = struct.unpack_from('>I', rom, offset)[0]
        if val == target_start:
            print(f"Found pointer to 0x{target_start:X} at ROM offset 0x{offset:X}")

if __name__ == "__main__":
    debug_table("gga.us.z64", 0xA50800)
