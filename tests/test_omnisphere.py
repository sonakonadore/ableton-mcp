from unittest.mock import Mock
from xml.etree import ElementTree as ET

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from MCP_Server import omnisphere, server


PATCH = b'<AmberPart><SynthEngine><ENTRYDESCR name="Funk Lead"/></SynthEngine></AmberPart>'


def test_factory_catalog_and_exact_part_lookup(tmp_path):
    factory = tmp_path / "Factory"
    factory.mkdir()
    (factory / "Example.db").write_bytes(
        (f'<FileSystem>\n<DIR name="Synth Mono"><FILE name="Funk Lead.prt_omn" offset="3" '
         f'size="{len(PATCH)}"/></DIR>\n</FileSystem>\r\n').encode() + b"pad" + PATCH
    )
    (factory / "Unsupported.db").write_bytes(b"different file format")
    patch_id = "factory/Example/Synth Mono/Funk Lead.prt_omn"
    assert omnisphere.list_patches(tmp_path, "lead mono", "factory", 1) == {
        "patches": [{
            "id": patch_id, "name": "Funk Lead", "category": "Synth Mono",
            "source": "factory", "library": "Example",
        }],
        "total_matches": 1,
        "skipped_libraries": ["Unsupported.db"],
    }
    assert omnisphere._patch_name(omnisphere._patch_xml(tmp_path, patch_id)) == "Funk Lead"


@pytest.mark.parametrize("patch_id", ["factory", "factory/../Patches/a.prt_omn", "user/User/../a.prt_omn"])
def test_patch_id_rejects_traversal_and_incomplete_ids(tmp_path, patch_id):
    with pytest.raises(omnisphere.OmnisphereError, match="Use a patch ID"):
        omnisphere._patch_xml(tmp_path, patch_id)


def test_processor_state_requires_a_complete_supported_envelope():
    state = ET.fromstring("<SynthMaster>" + "<SynthSubEngine><SynthEngine/></SynthSubEngine>" * 8 + "</SynthMaster>")
    wire = omnisphere._encode_state(state)
    assert ET.tostring(omnisphere._decode_state(wire)) == ET.tostring(state)
    with pytest.raises(omnisphere.OmnisphereError, match="envelope"):
        omnisphere._decode_state(wire[:-1])


def test_load_uses_exact_preset_on_empty_midi_track(monkeypatch):
    connection = Mock()
    monkeypatch.setattr(server, "get_ableton_connection", lambda: connection)
    connection.send_command.side_effect = [
        {"is_midi_track": True, "devices": []},
        {"items": [
            {"name": "Lead old.vstpreset", "is_loadable": True, "uri": "old"},
            {"name": "Lead.vstpreset", "is_loadable": True, "uri": "selected"},
        ]},
        {"loaded": True},
    ]
    assert server.load_omnisphere_plugin(2, preset_name="Lead")["status"] == "load_requested"
    connection.send_command.assert_called_with("load_browser_item", {"track_index": 2, "item_uri": "selected"})


def test_load_falls_back_to_au_and_never_loads_twice(monkeypatch):
    connection = Mock()
    monkeypatch.setattr(server, "get_ableton_connection", lambda: connection)
    connection.send_command.side_effect = [
        {"is_midi_track": True, "devices": []},
        {"items": []},
        {"items": [{"name": "Omnisphere", "is_loadable": True, "uri": "au"}]},
        {"loaded": True},
        {"is_midi_track": True, "devices": [{"name": "Omnisphere"}]},
    ]
    assert server.load_omnisphere_plugin(2)["format"] == "au"
    with pytest.raises(ToolError, match="empty MIDI track"):
        server.load_omnisphere_plugin(2)
    assert sum(call.args[0] == "load_browser_item" for call in connection.send_command.call_args_list) == 1
