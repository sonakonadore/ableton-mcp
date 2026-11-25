# Omnisphere in Ableton Live

`list_omnisphere_plugins` discovers the VST3 and Audio Units entries indexed
by Live. `load_omnisphere_plugin` loads the instrument on an empty MIDI track.
The default prefers VST3. Use `get_track_info` to verify the resulting device
chain before retrying a load.

## Load an installed patch

Patch export was exercised with Omnisphere 2.8.6c on Apple silicon. It requires
the installed VST3, its STEAM library, and the local host probe. Compile the
probe from the repository root using the
[Steinberg VST3 SDK](https://github.com/steinbergmedia/vst3sdk). Set
`VST3_SDK` to a directory containing `pluginterfaces`.

```sh
mkdir -p "$HOME/.cache/ableton-mcp"
xcrun clang++ -std=c++17 -O2 -I "$VST3_SDK" -framework CoreFoundation \
  tools/serum_host.cpp -o "$HOME/.cache/ableton-mcp/omnisphere-host"
```

1. Search with `list_omnisphere_patches`, for example with `query="funk lead"`.
   Search terms match the name, category, or library. Use the returned `id`
   to distinguish patches with the same name.
2. Call `export_omnisphere_live_preset` with that `patch_id` and a new
   `name`. The tool writes a VST3 preset to `Omnisphere MCP Presets`
   under Live's User Library.
3. Once Live indexes the export, call `load_omnisphere_plugin` with
   `track_index` and the same name as `preset_name`. The patch is in Part 1
   on MIDI channel 1.

Use `ABLETON_MCP_OMNISPHERE_PATCHES` for a custom patch directory,
`ABLETON_MCP_OMNISPHERE_VST3` for a custom VST3 bundle path,
`ABLETON_MCP_OMNISPHERE_HOST` for a custom probe location, or
`ABLETON_MCP_LIVE_USER_LIBRARY` for a relocated Live User Library.
Factory presets keep their references to the installed STEAM content.

## Host behavior

The Factory databases in the tested installation start with an XML directory
index. Patch positions in that index are relative to the bytes after the
index's closing line. User part patches reside in individual `.prt_omn`
files. The host opens a fresh processor, reads its eight-part multi via
`IComponent::getState`, inserts the local patch into Part 1, and submits it
to a fresh instance through `IComponent::setState`. It reads the result back
before writing the verified component state to a Steinberg VST3 preset.
The observed processor state starts with three little-endian 64-bit words,
followed by a NUL-terminated `SynthMaster` XML document and four zero bytes.
The exporter validates that layout before writing a preset.

Static inspection of the arm64 Mach-O bundle located the VST3 factory and
bundle lifecycle exports. The native string table also contained
`SynthMaster`, `SynthSubEngine`, and `AmberPart` with code references.
Runtime validation used the installed processor. A factory patch was
exported, loaded into Live via the MCP tools, and saved in a Set. The patch
name survived in Part 1 of Live's stored component state. Rendering the
saved state with the Set's eight-bar MIDI phrase produced output in all
eight bars.

For editing exposed controls, enable **Host Automation** on an Omnisphere
control, then move it while Live's **Configure** mode is active. Use
`get_device_parameters` and `set_device_parameter` on the resulting Live
device parameters. See the
[Spectrasonics host automation manual](https://support.spectrasonics.net/manual/Omnisphere/concepts/automation/page01.html).
