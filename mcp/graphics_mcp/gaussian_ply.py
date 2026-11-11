"""Structural check for the binary Gaussian PLY exported by local web-scan."""
import re

PLY_PREFIX_BYTES = 16 * 1024
_REQUIRED = {b"x", b"y", b"z", b"opacity", *(f"f_dc_{i}".encode() for i in range(3)),
             *(f"scale_{i}".encode() for i in range(3)), *(f"rot_{i}".encode() for i in range(4))}


def gaussian_vertex_count(prefix: bytes, size: int) -> int:
    error = "Gaussian PLY 文件头或顶点布局无效"
    end = re.search(rb"\nend_header\r?\n", prefix)
    if not end:
        raise ValueError(error)
    lines = prefix[:end.start()].splitlines()
    if lines[:2] != [b"ply", b"format binary_little_endian 1.0"]:
        raise ValueError(error)
    count = None
    fields = set()
    for line in lines[2:]:
        parts = line.split()
        if parts and parts[0] in {b"comment", b"obj_info"}:
            continue
        if (parts[:2] == [b"element", b"vertex"] and len(parts) == 3 and count is None
                and re.fullmatch(rb"[0-9]{1,7}", parts[2])):
            count = int(parts[2])
        elif (parts[:1] == [b"property"] and len(parts) == 3 and count is not None
              and parts[1] in {b"float", b"float32"} and parts[2] not in fields):
            fields.add(parts[2])
        else:
            raise ValueError(error)
    sh = {name for name in fields if name.startswith(b"f_rest_")}
    if (count is None or not 0 < count <= 2_000_000 or not _REQUIRED <= fields
            or len(sh) not in {0, 9, 24, 45} or sh != {f"f_rest_{i}".encode() for i in range(len(sh))}):
        raise ValueError(error)
    if end.end() + count * len(fields) * 4 != size:
        raise ValueError("Gaussian PLY 文件长度与顶点布局不符，产物可能不完整")
    return count
