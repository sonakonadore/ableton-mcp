"""Local Omnisphere patch catalog and Ableton browser integration."""

import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator, Literal
from xml.etree import ElementTree as ET

from .vst3_preset import component_preset

OmnisphereFormat = Literal["auto", "vst3", "au"]
BROWSER_PATHS = {
    "vst3": "plugins/VST3/Spectrasonics",
    "au": "plugins/AUv2/Spectrasonics",
}
NAME = "Omnisphere"
PATCH_SUFFIX = ".prt_omn"
MAX_INDEX_BYTES = 8 * 1024 * 1024
STATE_LIMIT = 16 * 1024 * 1024
STATE_HEADER = struct.Struct("<QQQ")
STATE_SIGNATURE = (999999999, 1)
STATE_TRAILER = b"\0" * 4
VST3_CLASS_ID = b"84E8DE5F9255222296FAE4133C935A18"
VST3_BUNDLE = "/Library/Audio/Plug-Ins/VST3/Omnisphere.vst3"
LIVE_PRESET_FOLDER = "Omnisphere MCP Presets"
LIVE_PRESET_BROWSER = f"user_library/{LIVE_PRESET_FOLDER}"
LIVE_PRESET_EXT = ".vstpreset"


class OmnisphereError(ValueError):
    pass


def patch_directory() -> Path:
    override = os.environ.get("ABLETON_MCP_OMNISPHERE_PATCHES")
    candidates = (
        [Path(override).expanduser()] if override else [
            base / "Spectrasonics/STEAM/Omnisphere/Settings Library/Patches"
            for base in (Path.home() / "Library/Application Support", Path("/Library/Application Support"))
        ]
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise OmnisphereError(
        "Omnisphere patch library was not found. Set ABLETON_MCP_OMNISPHERE_PATCHES "
        "to the Omnisphere Settings Library/Patches directory."
    )


def _factory_index(database: Path) -> tuple[ET.Element, int]:
    with database.open("rb") as stream:
        header = stream.read(MAX_INDEX_BYTES)
    closing_tag = b"</FileSystem>"
    end = header.find(closing_tag)
    if not header.startswith(b"<FileSystem>") or end < 0:
        raise OmnisphereError(f"Unsupported Omnisphere patch index in {database.name!r}.")
    end += len(closing_tag)
    root = ET.fromstring(header[:end])
    # The vendor index is followed by one line ending and the raw patch slots.
    line_ending = 2 if header[end:end + 2] == b"\r\n" else 1
    if header[end:end + line_ending] not in (b"\r\n", b"\n"):
        raise OmnisphereError(f"Missing patch data delimiter in {database.name!r}.")
    return root, end + line_ending


def _factory_entries(folder: ET.Element, parents: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], ET.Element]]:
    for child in folder:
        name = child.get("name", "")
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            continue
        if child.tag == "DIR":
            yield from _factory_entries(child, (*parents, name))
        elif child.tag == "FILE" and name.casefold().endswith(PATCH_SUFFIX):
            yield (*parents, name), child


def list_patches(root: Path, query: str, source: Literal["all", "factory", "user"], limit: int) -> dict[str, Any]:
    patches: list[dict[str, str]] = []
    skipped_libraries: list[str] = []
    terms = query.casefold().split()

    def add(kind: str, library: str, parts: tuple[str, ...]) -> None:
        name = Path(parts[-1]).stem
        searchable = " ".join((library, *parts[:-1], name)).casefold()
        if all(term in searchable for term in terms):
            patches.append({
                "id": "/".join((kind, library, *parts)),
                "name": name,
                "library": library,
                "category": "/".join(parts[:-1]),
                "source": kind,
            })

    if source in ("all", "factory"):
        for database in sorted((root / "Factory").glob("*.db")):
            try:
                index, _ = _factory_index(database)
                for parts, _ in _factory_entries(index):
                    add("factory", database.stem, parts)
            except (OmnisphereError, ET.ParseError):
                skipped_libraries.append(database.name)

    if source in ("all", "user"):
        user = root / "User"
        for patch in sorted(user.rglob("*" + PATCH_SUFFIX)):
            if patch.is_file():
                add("user", "User", patch.relative_to(user).parts)

    patches.sort(key=lambda patch: (patch["name"].casefold(), patch["id"]))
    return {"patches": patches[:limit], "total_matches": len(patches), "skipped_libraries": skipped_libraries}


def browser_plugins(ableton: Any, plugin_format: str) -> list[dict[str, str]]:
    path = BROWSER_PATHS[plugin_format]
    result = ableton.send_command("get_browser_items_at_path", {"path": path})
    error = result.get("error")
    if error in {
        "Unknown or unavailable category: plugins",
        f"Path part '{path.split('/')[1]}' not found",
        "Path part 'Spectrasonics' not found",
    }:
        return []
    if error:
        raise OmnisphereError(f"Could not read Live's {plugin_format} browser: {error}")
    return [
        {"name": NAME, "format": plugin_format, "uri": item["uri"]}
        for item in result["items"]
        if item.get("name") == NAME and item.get("is_loadable")
        and isinstance(item.get("uri"), str) and item["uri"].strip()
    ]


def _read_limited(path: Path) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(STATE_LIMIT + 1)
    if not data or len(data) > STATE_LIMIT:
        raise OmnisphereError("Omnisphere state or patch is empty or exceeds the size limit.")
    return data


def _patch_xml(root: Path, patch_id: str) -> ET.Element:
    segments = patch_id.split("/")
    if len(segments) < 3:
        raise OmnisphereError("Use a patch ID returned by list_omnisphere_patches.")
    source, library, *parts = segments
    if any(
        not segment or segment in (".", "..") or "\\" in segment
        for segment in (library, *parts)
    ) or not parts[-1].casefold().endswith(PATCH_SUFFIX):
        raise OmnisphereError("Use a patch ID returned by list_omnisphere_patches.")
    if source == "user" and library == "User":
        directory = (root / "User").resolve()
        path = directory.joinpath(*parts).resolve()
        if not path.is_relative_to(directory) or not path.is_file():
            raise OmnisphereError(f"User patch {patch_id!r} was not found.")
        data = _read_limited(path)
    elif source == "factory":
        database = root / "Factory" / (library + ".db")
        if not database.is_file():
            raise OmnisphereError(f"Factory library {library!r} was not found.")
        index, base = _factory_index(database)
        entry = next((node for path, node in _factory_entries(index) if path == tuple(parts)), None)
        if entry is None:
            raise OmnisphereError(f"Factory patch {patch_id!r} was not found.")
        try:
            offset, size = int(entry.attrib["offset"]), int(entry.attrib["size"])
        except (KeyError, ValueError) as exc:
            raise OmnisphereError("Invalid Omnisphere patch index entry.") from exc
        with database.open("rb") as stream:
            if offset < 0 or not 0 < size <= STATE_LIMIT or base + offset + size > os.fstat(stream.fileno()).st_size:
                raise OmnisphereError("Omnisphere patch index entry points outside the database.")
            stream.seek(base + offset)
            data = stream.read(size)
        if len(data) != size:
            raise OmnisphereError("The Omnisphere patch database changed while it was being read.")
    else:
        raise OmnisphereError("Use a patch ID returned by list_omnisphere_patches.")

    xml = data.rstrip(b"\0").strip()
    if not xml.startswith((b"<AmberPart ", b"<AmberPart>")):
        raise OmnisphereError("Unsupported Omnisphere part patch.")
    patch = ET.fromstring(xml)
    engine = patch.find("SynthEngine")
    if patch.tag != "AmberPart" or engine is None or _patch_name(engine) is None:
        raise OmnisphereError("Unsupported Omnisphere part patch.")
    return engine


def _patch_name(engine: ET.Element | None) -> str | None:
    description = engine.find(".//ENTRYDESCR") if engine is not None else None
    return description.get("name") if description is not None else None


def _decode_state(raw: bytes) -> ET.Element:
    if len(raw) < STATE_HEADER.size + 5:
        raise OmnisphereError("Truncated Omnisphere processor state.")
    signature, version, length = STATE_HEADER.unpack_from(raw)
    end = STATE_HEADER.size + length
    if (
        (signature, version) != STATE_SIGNATURE or length <= 1
        or raw[end - 1:end] != b"\0" or raw[end:] != STATE_TRAILER
    ):
        raise OmnisphereError("Unsupported Omnisphere processor state envelope.")
    xml = raw[STATE_HEADER.size:end - 1]
    if not xml.startswith((b"<SynthMaster ", b"<SynthMaster>")):
        raise OmnisphereError("Expected an Omnisphere multi.")
    state = ET.fromstring(xml)
    parts = state.findall("SynthSubEngine")
    if len(parts) != 8 or any(len(part.findall("SynthEngine")) != 1 for part in parts):
        raise OmnisphereError("Expected an Omnisphere multi with eight parts.")
    return state


def _encode_state(state: ET.Element) -> bytes:
    data = ET.tostring(state, encoding="utf-8") + b"\0"
    return STATE_HEADER.pack(*STATE_SIGNATURE, len(data)) + data + STATE_TRAILER


def _run_host(host: Path, bundle: Path, source: Path | str, destination: Path) -> ET.Element:
    command = [
        str(host), str(bundle), "instrument", str(source), str(destination), "-",
        "--class-name", NAME,
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=45, check=False)
    except subprocess.TimeoutExpired as exc:
        raise OmnisphereError("The Omnisphere VST3 host probe timed out.") from exc
    if result.returncode:
        raise OmnisphereError(
            f"The VST3 host probe failed (exit {result.returncode}). "
            "Check the Omnisphere installation and rebuild the host with --class-name support."
        )
    return _decode_state(_read_limited(destination))


def preset_filename(name: str) -> str:
    if (
        not name or name.startswith(".") or name.casefold().endswith(LIVE_PRESET_EXT)
        or any(char in "/\\:" or ord(char) < 32 or ord(char) == 127 for char in name)
        or len(name.encode("utf-8")) + len(LIVE_PRESET_EXT) > 255
    ):
        raise OmnisphereError("Use a preset name without path separators, control characters, or a file extension.")
    return name + LIVE_PRESET_EXT


def export_live_preset(patch_id: str, name: str) -> dict[str, str]:
    """Put a local patch in Part 1 of a fresh multi, verify it, and export it."""
    if sys.platform != "darwin":
        raise OmnisphereError("Omnisphere VST3 preset export currently requires macOS.")
    filename = preset_filename(name)
    patch = _patch_xml(patch_directory(), patch_id)
    default_host = Path.home() / ".cache/ableton-mcp/omnisphere-host"
    host = Path(os.environ.get("ABLETON_MCP_OMNISPHERE_HOST", default_host)).expanduser()
    bundle = Path(os.environ.get("ABLETON_MCP_OMNISPHERE_VST3", VST3_BUNDLE)).expanduser()
    default_library = Path.home() / "Music/Ableton/User Library"
    library = Path(os.environ.get("ABLETON_MCP_LIVE_USER_LIBRARY", default_library)).expanduser()
    if not host.is_absolute() or not host.is_file() or not os.access(host, os.X_OK):
        raise OmnisphereError("Build the host probe and set ABLETON_MCP_OMNISPHERE_HOST to its absolute path.")
    if not bundle.is_absolute() or not bundle.is_dir():
        raise OmnisphereError("Set ABLETON_MCP_OMNISPHERE_VST3 to the absolute Omnisphere.vst3 bundle path.")
    if not library.is_absolute() or not library.is_dir():
        raise OmnisphereError("Set ABLETON_MCP_LIVE_USER_LIBRARY to Live's existing, absolute User Library directory.")

    folder = library / LIVE_PRESET_FOLDER
    destination = folder / filename
    if destination.exists():
        raise OmnisphereError(f"An Omnisphere preset called {filename!r} already exists.")
    with tempfile.TemporaryDirectory() as scratch:
        directory = Path(scratch)
        state = _run_host(host, bundle, "-", directory / "initial.state")
        part = state.findall("SynthSubEngine")[0]
        part.remove(part.find("SynthEngine"))
        part.append(patch)
        edited = directory / "edited.state"
        edited.write_bytes(_encode_state(state))
        verified = directory / "verified.state"
        readback = _run_host(host, bundle, edited, verified)
        if _patch_name(readback.find("SynthSubEngine/SynthEngine")) != _patch_name(patch):
            raise OmnisphereError("The processor did not restore the requested patch in Part 1.")
        raw = component_preset(VST3_CLASS_ID, _read_limited(verified))

    folder.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=folder) as scratch:
        staged = Path(scratch) / filename
        staged.write_bytes(raw)
        try:
            os.link(staged, destination)
        except FileExistsError as exc:
            raise OmnisphereError(f"An Omnisphere preset called {filename!r} already exists.") from exc
    return {"patch_id": patch_id, "name": name, "filename": filename, "path": str(destination)}
