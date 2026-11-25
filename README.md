<div align="center">

# Ableton MCP

**Connect Ableton Live to Codex, ChatGPT, Claude, and other MCP clients**

Prompt-assisted music production, end-to-end track creation, and Live session and arrangement manipulation — driven by AI.

[![PyPI Version](https://img.shields.io/pypi/v/ableton-mcp?color=blue)](https://pypi.org/project/ableton-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Discord](https://img.shields.io/badge/Discord-join-5865F2?logo=discord&logoColor=white)](https://discord.gg/JK4hNKGprW)

[**Setup Video**](https://youtu.be/iJWJqyVuPS8) · [**Discord**](https://discord.gg/JK4hNKGprW) · [**Issues**](https://github.com/ahujasid/ableton-mcp/issues)

</div>

---

## Quickstart

Three steps: install `uv`, point your MCP client at the server, install the Ableton Remote Script.

**1. Install uv**

```bash
# macOS
brew install uv
```

Otherwise, install from [uv's official website](https://docs.astral.sh/uv/getting-started/installation/).

> **Warning:** Do not proceed before installing uv.

**2. Add the MCP server to your client**

For **Codex** or **ChatGPT**, follow [OpenAI client setup](#openai-client-setup). Those commands run this GitHub fork so the MCP server and the installed Live script come from the same revision.

<details open>
<summary><b>Claude Desktop</b> — Settings → Developer → Edit Config</summary>

```json
{
    "mcpServers": {
        "AbletonMCP": {
            "command": "uvx",
            "args": [
                "ableton-mcp"
            ]
        }
    }
}
```
</details>

<details>
<summary><b>Cursor</b> — Settings → MCP</summary>

Paste this as a command:

```
uvx ableton-mcp
```
</details>

> **Warning:** Only run one instance of the MCP server (either on Cursor or Claude Desktop), not both.

**3. Install the Ableton Remote Script**

```bash
uvx --from ableton-mcp ableton-mcp-install-script
uvx --from ableton-mcp ableton-mcp-install-script --list-targets   # preview target folders first
```

**4. Connect**

1. Launch Ableton Live
2. Go to **Settings/Preferences → Link, Tempo & MIDI**
3. In the **Control Surface** dropdown, select **AbletonMCP**
4. Set **Input** and **Output** to **None**

Ask your MCP client to read the current tempo and track count before editing the Set.

---

## Table of Contents

- [Quickstart](#quickstart)
- [OpenAI client setup](#openai-client-setup)
- [Features](#features)
- [Components](#components)
- [Installation](#installation)
  - [Prerequisites](#prerequisites)
  - [Claude for Desktop Integration](#claude-for-desktop-integration)
  - [Cursor Integration](#cursor-integration)
  - [Installing the Ableton Remote Script](#installing-the-ableton-remote-script)
- [Usage](#usage)
  - [Starting the Connection](#starting-the-connection)
  - [Using with Claude](#using-with-claude)
  - [Capabilities](#capabilities)
  - [Example Commands](#example-commands)
- [Troubleshooting](#troubleshooting)
- [Technical Details](#technical-details)
- [Limitations & Security Considerations](#limitations--security-considerations)
- [Telemetry](#telemetry)
- [Join the Community](#join-the-community)
- [Contributing](#contributing)
- [Disclaimer](#disclaimer)

---

## Features

| | |
|---|---|
| **Two-way communication** | Connect Claude AI to Ableton Live through a socket-based server |
| **Track manipulation** | Create, modify, and manipulate MIDI and audio tracks |
| **Instrument and effect selection** | Claude can access and load the right instruments, effects and sounds from Ableton's library |
| **Clip creation** | Create and edit MIDI clips with notes |
| **Arrangement view composition** | Build full songs autonomously in Arrangement View, including sections like intro, buildup, drop, breakdown, and outro |
| **Session control** | Start and stop playback, fire clips, and control transport across Session View and Arrangement View |
| **Anonymous telemetry** | Usage tracking to help improve the tool (can be disabled) |

## Components

The system consists of two main components:

1. **Ableton Remote Script** (`AbletonMCP_Remote_Script/__init__.py`) — a MIDI Remote Script for Ableton Live that creates a socket server to receive and execute commands
2. **MCP Server** (`MCP_Server/server.py`) — a Python server that implements the Model Context Protocol and connects to the Ableton Remote Script

---

## OpenAI client setup

Run these commands on the computer that runs Live. With `uv` installed, clone this fork and install its bundled Remote Script:

```bash
git clone https://github.com/dev-universe/ableton-mcp.git "$HOME/ableton-mcp"
cd "$HOME/ableton-mcp"
uv sync --locked
uv run --locked ableton-mcp-install-script
```

Restart Live and open **Settings → Tempo & MIDI** (called **Link, Tempo & MIDI** in some versions). Set an unused **Control Surface** row to **AbletonMCP**, with **Input** and **Output** both **None**. Open a blank Live Set for the first run.

### Codex on the same computer

Register the default `stdio` server from a terminal:

```bash
codex mcp add ableton \
  --env ABLETON_MCP_DISABLE_TELEMETRY=1 \
  --env ABLETON_MCP_DISABLE_DATASET=1 \
  -- "$(command -v uv)" run --locked --directory "$HOME/ableton-mcp" ableton-mcp
codex mcp get ableton
```

Open a new local Codex session. Restart the desktop app if the server list is stale. Ask it to use `ableton.get_session_info` to report the current tempo and number of tracks. Check the result against Live. Review write-tool requests before approving them. See [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

### Organize tracks in a Live Set

These tools require the bundled Remote Script 1.8.0 or later. If you already installed the fork, update your checkout, run `uv run --locked ableton-mcp-install-script`, then reopen Live and start a new local chat in the ChatGPT desktop app.

- Use `get_track_layout` to read track colors, Return tracks, and Sends. Regular tracks and Returns have separate indices.
- Use `set_track_name` to rename a regular track and `set_track_color` to color a regular or Return track. Live applies the closest color in its palette.
- Use `get_track_routing` before `set_track_routing`. Set the input or output type first, then choose a channel from the refreshed options. Return tracks support output routing.
- Use `create_return_track`, `set_return_track_name`, and `delete_return_track` to manage Returns. Deleting requires the current Return name and index. `set_track_send` sets a regular track's Send to 0–100% of the Live parameter range.

Try this in a blank Set. “Read my track layout. Rename the first audio track to `01 Source` and give it a red color. Create a Return named `Practice FX`, set that track's Send to it to 25%, then read back the changes. Keep playback stopped.”

### Use Serum 2 and Serum 2 FX

Install Serum 2, enable the VST3 or Audio Units source in Live's Plug-Ins
settings, and rescan. `list_serum_plugins` shows the entries Live can load.
See the [Serum 2 host interface observations](docs/serum-2-host-interface.md).

Use `get_track_layout` to find the current index of a regular track. Call
`load_serum_plugin` with `variant="instrument"` for Serum 2 on a MIDI track,
or `variant="fx"` for Serum 2 FX on an audio track or after an instrument.
Choose `plugin_format="vst3"` or `"au"`, or leave `"auto"` to prefer VST3.
Check `get_track_info` for the resulting device chain before retrying a load.

To adjust a plug-in control, add it to the device panel with Live's
**Configure** mode. Use `get_device_parameters` to find its current index,
value, and range, then pass that index and a value to `set_device_parameter`.
Parameter assignments belong to each instance. [Ableton Live manual](https://www.ableton.com/en/live-manual/12/using-plug-ins/).

For isolated sound design, see [Serum processor controls](docs/serum-control-reference.md).
For portable files, embedded audio, and loading onto Live tracks, see
[Serum preset tools](docs/serum-presets.md).
To render patches with MIDI or audio input and measure revisions, see
[Serum auditions](docs/serum-audition.md).
To ask an MCP assistant for a sound in your own words, see
[natural-language Serum sound design](docs/serum-sound-design.md).
Use them to inspect settings, choose installed assets, and verify a batch of
changes before making the next edit.

### Use Omnisphere

Use `list_omnisphere_plugins` to find Omnisphere in Live and
`list_omnisphere_patches` to search installed Factory and User part patches.
On macOS, `export_omnisphere_live_preset` places a selected patch in Part 1
of a verified VST3 preset. Use `load_omnisphere_plugin` to load it on an empty
MIDI track. See [Omnisphere setup and compatibility](docs/omnisphere.md).

### ChatGPT web via Secure MCP Tunnel

Run the HTTP server in a terminal and leave it open:

```bash
cd "$HOME/ableton-mcp"
uv run --locked ableton-mcp --transport streamable-http
```

The MCP endpoint is `http://127.0.0.1:8765/mcp`. Use `--port` if 8765 is busy and update the tunnel profile accordingly. Both this endpoint and Live's raw control socket on port 9877 should stay on loopback. The HTTP server relies on the private tunnel and its workspace permissions for remote access. HTTP mode disables upstream telemetry and dataset recording and omits consent, dataset feedback, and local audio file import tools.

1. In [Platform tunnel settings](https://platform.openai.com/settings/organization/tunnels), create a tunnel associated with the ChatGPT workspace. Use a restricted runtime key with Tunnels Read and Use. The person creating it needs Tunnels Read and Manage.
2. Install the official `tunnel-client` for your Mac. In a second terminal, make `CONTROL_PLANE_API_KEY` available to that process via a secure local input. Configure the `sample_mcp_remote_no_auth` profile with `--mcp-server-url http://127.0.0.1:8765/mcp` and your tunnel ID.
3. Run `tunnel-client doctor --profile ableton-live --explain`, then `tunnel-client run --profile ableton-live`, using the same profile name you chose at setup.
4. In [ChatGPT Plugins](https://chatgpt.com/plugins), use **Add custom MCP server → Tunnel** and select that tunnel. Install your plugin and use it in a new chat. Start with a read request, then approve a small edit in the blank Set and verify it in Live.

See the [Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels), [tunnel-client onboarding](https://github.com/openai/tunnel-client/blob/master/docs/onboarding.md), and [ChatGPT custom MCP guide](https://developers.openai.com/api/docs/guides/custom-mcp-server) for account access and setup. The Mac, Live, the HTTP server, and `tunnel-client` must all be available while ChatGPT uses the plugin. Stop the two terminal processes with **Control+C** when finished.

---

## Installation

### Prerequisites

- **Ableton Live** 11 or 12
- **Python** 3.10 or newer for the MCP server (managed by `uv`)
- **uv** package manager

If you're on Mac, please install uv as:

```
brew install uv
```

Otherwise, install from [uv's official website](https://docs.astral.sh/uv/getting-started/installation/)

> **Warning:** Do not proceed before installing uv.

### Claude for Desktop Integration

[Follow along with the setup instructions video](https://youtu.be/iJWJqyVuPS8)

Go to **Claude → Settings → Developer → Edit Config → `claude_desktop_config.json`** to include the following:

```json
{
    "mcpServers": {
        "AbletonMCP": {
            "command": "uvx",
            "args": [
                "ableton-mcp"
            ]
        }
    }
}
```

### Cursor Integration

Run ableton-mcp without installing it permanently through uvx. Go to **Cursor Settings → MCP** and paste this as a command:

```
uvx ableton-mcp
```

> **Warning:** Only run one instance of the MCP server (either on Cursor or Claude Desktop), not both.

### Claude Code Integration

In the terminal, run:

```
claude mcp add AbletonMCP uvx ableton-mcp
```

### Installing the Ableton Remote Script

[Follow along with the setup instructions video](https://youtu.be/iJWJqyVuPS8)

Install the Remote Script with:

```bash
uvx --from ableton-mcp ableton-mcp-install-script
uvx --from ableton-mcp ableton-mcp-install-script --list-targets   # preview target folders first
```

> If you installed the package with `pip` or `pipx`, the command is on your PATH directly — just run `ableton-mcp-install-script`.

This copies the matching Remote Script into your Ableton **User Library**'s `Remote Scripts` folder — the location Live (10.1.13+) scans for third-party control surface scripts. The installer reads the User Library location from Live's `Library.cfg`, falling back to the default (`~/Music/Ableton/User Library` on macOS, `Documents\Ableton\User Library` on Windows). If a different version of the script is already there, the existing file is backed up to `__init__.py.bak` before being replaced.

If your User Library lives somewhere non-standard and isn't detected, point the installer at it directly:

```bash
uvx --from ableton-mcp ableton-mcp-install-script --target "/path/to/User Library/Remote Scripts"
```

> The legacy `Preferences/User Remote Scripts` folder (used for instant-mapping configs, not Python control surfaces) is no longer targeted by default; pass `--legacy` if you need it for an old Live version.

Then **restart Ableton** (or re-select the AbletonMCP control surface) so Live loads it. Re-run the command after upgrading the package — the server logs a warning when the loaded script version doesn't match what it expects.

> **Note:** The server does **not** install the script on startup. Writing into Ableton's preferences directory is an explicit action, not a side effect of launching a server.

**First-time Ableton setup:**

1. Run `uvx --from ableton-mcp ableton-mcp-install-script`
2. Launch Ableton Live
3. Go to **Settings/Preferences → Link, Tempo & MIDI**
4. In the **Control Surface** dropdown, select **AbletonMCP**
5. Set **Input** and **Output** to **None**

<details>
<summary><b>Manual fallback locations (User Library → Remote Scripts)</b></summary>

- **macOS:** `~/Music/Ableton/User Library/Remote Scripts/AbletonMCP/`
- **Windows:** `C:\Users\[Username]\Documents\Ableton\User Library\Remote Scripts\AbletonMCP\`

If you've moved your User Library, use its actual location (shown in Live under **Preferences → Library → Location of User Library**), and create the `Remote Scripts` folder inside it if it doesn't exist yet.
</details>

The MCP server and Remote Script share a version handshake (`get_remote_script_info`). If they diverge, newer tools degrade gracefully until Live is restarted.

---

## Usage

### Starting the Connection

1. Ensure the Ableton Remote Script is loaded in Ableton Live
2. Make sure the MCP server is configured in Claude Desktop or Cursor
3. The connection should be established automatically when you interact with Claude

### Using with Claude

Once the config file has been set on Claude, and the remote script is running in Ableton, you will see a hammer icon with tools for the Ableton MCP.

### Capabilities

- Get session and track information
- Create and modify MIDI and audio tracks
- Create full song arrangements from start to finish in Arrangement View
- Create, edit, and trigger clips
- Control playback
- Load instruments and effects from Ableton's browser
- Add notes to MIDI clips
- Change tempo and other session parameters

### Example Commands

Here are some examples of what you can ask Claude to do:

| Prompt | Demo |
|---|---|
| *"Create an 80s synthwave track"* | [Watch](https://youtu.be/VH9g66e42XA) |
| *"Create a Metro Boomin style hip-hop beat"* | |
| *"Create a full arrangement with an intro, buildup, drop, breakdown, and outro"* | |
| *"Create a new MIDI track with a synth bass instrument"* | |
| *"Add reverb to my drums"* | |
| *"Create a 4-bar MIDI clip with a simple melody"* | |
| *"Get information about the current Ableton session"* | |
| *"Load a 808 drum rack into the selected track"* | |
| *"Add a jazz chord progression to the clip in track 1"* | |
| *"Set the tempo to 120 BPM"* | |
| *"Play the clip in track 2"* | |

---

## Troubleshooting

| Problem | Fix |
|---|---|
| **Connection issues** | Make sure the Ableton Remote Script is loaded, and the MCP server is configured on Claude |
| **Timeout errors** | Try simplifying your requests or breaking them into smaller steps |
| **Have you tried turning it off and on again?** | If you're still having connection errors, try restarting both Claude and Ableton Live |

## Technical Details

### Communication Protocol

The system uses a simple JSON-based protocol over TCP sockets:

- **Commands** are sent as JSON objects with a `type` and optional `params`
- **Responses** are JSON objects with a `status` and `result` or `message`

## Limitations & Security Considerations

- Creating complex musical arrangements might need to be broken down into smaller steps
- The tool is designed to work with Ableton's default devices and browser items
- Always save your work before extensive experimentation

---

## Telemetry

There are two tiers, and they have different defaults.

**Anonymous telemetry — on by default.** A random install ID, a per-run session ID, which tools ran, whether they succeeded, and how long they took. This is what counts active users and catches broken tools. It contains none of your content: no prompts, no MIDI, no track or clip names, no device settings.

**Dataset recording — off by default, opt-in.** Everything with your work in it: prompts, MIDI notes, track and clip names, and device settings, which may be published as part of an open dataset used to train music-production models. Nothing here is collected unless you explicitly turn it on.

To see exactly what data each tier collects, see the [Terms & Data Use](TERMS.md).

### Opting in to dataset recording

Either set the environment variable before starting the server:

```bash
export ABLETON_MCP_ENABLE_DATASET=true
```

…or answer the question when your client asks it. On your first tool call you're asked once — as a dialog if your client supports MCP elicitation, otherwise as a message in the chat — and answering yes turns it on from that point. Your answer is stored in `~/.ableton-mcp/consent.json`. Declining, or never answering, records nothing. If you dismiss the dialog without choosing, that isn't treated as an answer and you may be asked again in a later session.

### Opting out of anonymous telemetry

Set one of these before starting the MCP server:

```bash
export ABLETON_MCP_DISABLE_TELEMETRY=true
```

Or use any of these alternatives:

- `DISABLE_TELEMETRY=true`
- `MCP_DISABLE_TELEMETRY=true`

This also disables dataset recording. `ABLETON_MCP_DISABLE_DATASET=true` turns off dataset recording only, and overrides any stored grant.

For Claude Desktop, add the environment variable to your config:

```json
{
    "mcpServers": {
        "AbletonMCP": {
            "command": "uvx",
            "args": ["ableton-mcp"],
            "env": {
                "ABLETON_MCP_DISABLE_TELEMETRY": "true"
            }
        }
    }
}
```

---

## Join the Community

Give feedback, get inspired, and build on top of the MCP: [**Discord**](https://discord.gg/JK4hNKGprW)

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## Disclaimer

This is a third-party integration and not made by Ableton. Made by [Siddharth](https://x.com/sidahuj).

---

<div align="center">

**If Ableton MCP is useful to you, consider starring the repo**

</div>
