"""Write a Steinberg VST3 preset containing a component state chunk."""

import struct


def component_preset(class_id: bytes, processor: bytes) -> bytes:
    header_size = 48
    header = struct.pack("<4si32sq", b"VST3", 1, class_id, header_size + len(processor))
    chunk_list = struct.pack("<4si4sqq", b"List", 1, b"Comp", header_size, len(processor))
    return header + processor + chunk_list
