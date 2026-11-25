# ableton_mcp_server.py
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
import argparse
import asyncio
import socket
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator, Dict, Any, List, Literal, Union
from xml.etree.ElementTree import ParseError

from .telemetry import record_startup
from .telemetry_decorator import telemetry_tool, rich_telemetry_tool
from .dataset import dataset_enabled, get_recorder
from .dataset.trajectory_decorator import trajectory_tool
from . import omnisphere
from .serum_controls import (
    FX_MODULES, MISSING, RACKS, Mode, SerumEdits, StatePath, describe, edit_state, list_assets, native_default, value_at,
)
from .serum_design import SERUM_CONTROL_HINTS, SERUM_DESIGN_WORKFLOW
from .serum_presets import (
    LIVE_PRESET_BROWSER, LIVE_PRESET_FOLDER, LiveFormat, PresetDetails, check_live_preset,
    export_live_preset, export_preset, import_preset, list_factory_presets,
)
from .serum_render import Audition, render_audio
from .serum_state import SUPPORTED_BUILD, SerumError, Variant, initial_state

ABLETON_HOST = os.environ.get("ABLETON_HOST", "localhost")
ABLETON_PORT = int(os.environ.get("ABLETON_PORT", "9877"))

# Configure logging
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("AbletonMCPServer")

@dataclass
class AbletonConnection:
    host: str
    port: int
    sock: socket.socket = None

    # One socket, several threads: tool calls run on the MCP worker while the
    # passive poller drains events on its own thread. Serialise so their
    # sendall/recv pairs cannot interleave.
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def connect(self) -> bool:
        """Connect to the Ableton Remote Script socket server"""
        if self.sock:
            return True

        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5.0)
            self.sock.connect((self.host, self.port))
            self.sock.settimeout(None)
            logger.info(f"Connected to Ableton at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Ableton at {self.host}:{self.port}: {str(e)}")
            self.sock = None
            return False
    
    def disconnect(self):
        """Disconnect from the Ableton Remote Script"""
        with self._lock:
            if self.sock:
                try:
                    self.sock.close()
                except Exception as e:
                    logger.error(f"Error disconnecting from Ableton: {str(e)}")
                finally:
                    self.sock = None

    def receive_full_response(self, sock, buffer_size=8192):
        """Receive the complete response, potentially in multiple chunks"""
        chunks = []
        sock.settimeout(15.0)  # Increased timeout for operations that might take longer
        
        try:
            while True:
                try:
                    chunk = sock.recv(buffer_size)
                    if not chunk:
                        if not chunks:
                            raise Exception("Connection closed before receiving any data")
                        break
                    
                    chunks.append(chunk)
                    
                    # Check if we've received a complete JSON object
                    try:
                        data = b''.join(chunks)
                        json.loads(data.decode('utf-8'))
                        logger.info(f"Received complete response ({len(data)} bytes)")
                        return data
                    except json.JSONDecodeError:
                        # Incomplete JSON, continue receiving
                        continue
                except socket.timeout:
                    logger.warning("Socket timeout during chunked receive")
                    break
                except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
                    logger.error(f"Socket connection error during receive: {str(e)}")
                    raise
        except Exception as e:
            logger.error(f"Error during receive: {str(e)}")
            raise
            
        # If we get here, we either timed out or broke out of the loop
        if chunks:
            data = b''.join(chunks)
            logger.info(f"Returning data after receive completion ({len(data)} bytes)")
            try:
                json.loads(data.decode('utf-8'))
                return data
            except json.JSONDecodeError:
                raise Exception("Incomplete JSON response received")
        else:
            raise Exception("No data received")

    def send_command(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Send a command to Ableton and return the response.

        Serialised on ``self._lock``: the request and its response are one
        indivisible exchange on a shared socket. Holding the lock across the
        whole round-trip (including reconnect) is what keeps a concurrent
        caller from reading someone else's reply.
        """
        with self._lock:
            return self._send_command_locked(command_type, params)

    def _send_command_locked(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        if not self.sock and not self.connect():
            raise ConnectionError("Not connected to Ableton")

        command = {
            "type": command_type,
            "params": params or {}
        }
        
        # Check if this is a state-modifying command
        is_modifying_command = command_type in [
            "create_midi_track", "create_audio_track", "set_track_name",
            "set_track_color", "set_track_routing", "set_track_send",
            "create_return_track", "set_return_track_name", "delete_return_track",
            "create_clip", "create_audio_clip", "add_notes_to_clip", "set_clip_name",
            "delete_clip",
            "clear_notes_from_clip",
            "set_tempo", "fire_clip", "stop_clip", "set_device_parameter",
            "start_playback", "stop_playback", "load_instrument_or_effect",
            # Arrangement view commands
            "switch_to_arrangement_view", "set_current_song_time",
            "duplicate_session_clip_to_arrangement",
            "create_locator"
        ]

        # Commands whose work on Live's main thread can take noticeably longer
        # than the default modifying-command budget (e.g. importing/decoding a
        # large audio file). Give them a wider socket timeout so we don't time
        # out before the Remote Script's own queue does.
        long_running_commands = {"create_audio_clip": 65.0}
        
        try:
            logger.info(f"Sending command: {command_type} with params: {params}")
            
            # Send the command
            self.sock.sendall(json.dumps(command).encode('utf-8'))
            logger.info(f"Command sent, waiting for response...")
            
            # Set timeout based on command type
            if command_type in long_running_commands:
                timeout = long_running_commands[command_type]
            else:
                timeout = 15.0 if is_modifying_command else 10.0
            self.sock.settimeout(timeout)

            # Receive the response
            response_data = self.receive_full_response(self.sock)
            logger.info(f"Received {len(response_data)} bytes of data")

            # Parse the response
            response = json.loads(response_data.decode('utf-8'))
            logger.info(f"Response parsed, status: {response.get('status', 'unknown')}")

            if response.get("status") == "error":
                logger.error(f"Ableton error: {response.get('message')}")
                raise Exception(response.get("message", "Unknown error from Ableton"))
            
            return response.get("result", {})
        except socket.timeout:
            logger.error("Socket timeout while waiting for response from Ableton")
            self.sock = None
            raise Exception("Timeout waiting for Ableton response")
        except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
            logger.error(f"Socket connection error: {str(e)}")
            self.sock = None
            raise Exception(f"Connection to Ableton lost: {str(e)}")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON response from Ableton: {str(e)}")
            if 'response_data' in locals() and response_data:
                logger.error(f"Raw response (first 200 bytes): {response_data[:200]}")
            self.sock = None
            raise Exception(f"Invalid response from Ableton: {str(e)}")
        except Exception as e:
            logger.error(f"Error communicating with Ableton: {str(e)}")
            self.sock = None
            raise Exception(f"Communication error with Ableton: {str(e)}")

@asynccontextmanager
async def server_lifespan() -> AsyncIterator[None]:
    """Manage server startup and shutdown lifecycle"""
    try:
        logger.info("AbletonMCP server starting up")

        # Record startup event for telemetry
        try:
            record_startup()
        except Exception as e:
            logger.debug(f"Failed to record startup telemetry: {e}")

        try:
            ableton = get_ableton_connection()
            logger.info("Successfully connected to Ableton on startup")
            from .script_handshake import handshake

            handshake(ableton.send_command)
        except Exception as e:
            logger.warning(f"Could not connect to Ableton on startup: {str(e)}")
            logger.warning("Make sure the Ableton Remote Script is running")

        if dataset_enabled():
            try:
                recorder = get_recorder()
                if recorder:
                    logger.info(
                        "Dataset recording enabled → Supabase session %s",
                        recorder.session_id,
                    )
                from .script_handshake import script_has_capability
                from .dataset.passive_poller import start_passive_poller

                # Runs even without Live-UI events: it also settles implicit
                # preferences for agent actions.
                start_passive_poller()
                if not script_has_capability("drain_passive_events"):
                    logger.warning(
                        "Passive Live listeners unavailable — Remote Script outdated "
                        "or not loaded. Restart Ableton after script update."
                    )
            except Exception as e:
                logger.warning(f"Failed to start dataset recorder: {e}")
        else:
            reason = "unknown"
            try:
                from .config import telemetry_config  # noqa: F401
                from .telemetry import get_telemetry_consent, is_telemetry_enabled

                if not is_telemetry_enabled():
                    reason = "telemetry disabled (config.enabled=False or DISABLE_TELEMETRY)"
                elif not get_telemetry_consent():
                    reason = "no dataset consent yet — the user has not opted in"
                else:
                    reason = "ABLETON_MCP_DISABLE_DATASET or unexpected gate failure"
            except Exception:
                reason = "MCP_Server/config.py missing or unreadable (gitignored; required for Supabase)"
            logger.info("Dataset recording off — %s", reason)

        yield
    finally:
        global _ableton_connection
        try:
            from .dataset.passive_poller import stop_passive_poller

            stop_passive_poller()
        except Exception as e:
            logger.debug(f"Failed to stop passive poller: {e}")
        if _ableton_connection:
            logger.info("Disconnecting from Ableton on shutdown")
            _ableton_connection.disconnect()
            _ableton_connection = None
        try:
            recorder = get_recorder()
            if recorder:
                # Bounded wait so queued rows land before the daemon worker
                # is killed at exit.
                recorder.end(timeout=float(
                    os.environ.get("ABLETON_MCP_DATASET_FLUSH_SEC", "5")
                ))
        except Exception as e:
            logger.debug(f"Failed to end dataset session: {e}")
        logger.info("AbletonMCP server shut down")

mcp = FastMCP(
    "AbletonMCP",
    instructions="For a request to create or revise a Serum sound, use get_serum_design_workflow and the Serum tools.",
    host="127.0.0.1",
    stateless_http=True,
    json_response=True,
)
READ_ONLY = ToolAnnotations(readOnlyHint=True)
MIXER_UPDATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)
TrackIndex = Annotated[int, Field(ge=0, strict=True)]
TrackKind = Literal["track", "return"]
RoutingDirection = Literal["input", "output"]

# Global connection for resources
_ableton_connection = None

def get_ableton_connection():
    """Get or create a persistent Ableton connection"""
    global _ableton_connection

    if _ableton_connection is not None and _ableton_connection.sock is not None:
        try:
            # Check if the socket is still alive by peeking for data
            # MSG_PEEK + MSG_DONTWAIT will raise BlockingIOError if alive but no data,
            # or return b'' if the remote end has closed the connection.
            _ableton_connection.sock.setblocking(False)
            try:
                data = _ableton_connection.sock.recv(1, socket.MSG_PEEK)
                if data == b'':
                    raise ConnectionError("Remote end closed")
            except BlockingIOError:
                pass  # Socket is alive, just no data waiting — this is normal
            finally:
                _ableton_connection.sock.setblocking(True)
            return _ableton_connection
        except Exception as e:
            logger.warning(f"Existing connection is no longer valid: {str(e)}")
            try:
                _ableton_connection.disconnect()
            except:
                pass
            _ableton_connection = None
            # Passive events stop arriving while disconnected, so any cached
            # state hash may describe a Live session that has since moved on.
            try:
                recorder = get_recorder()
                if recorder:
                    recorder.invalidate_cached_state()
            except Exception:
                pass
    
    # Connection doesn't exist or is invalid, create a new one
    if _ableton_connection is None:
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(f"Connecting to Ableton at {ABLETON_HOST}:{ABLETON_PORT} (attempt {attempt}/{max_attempts})...")
                _ableton_connection = AbletonConnection(host=ABLETON_HOST, port=ABLETON_PORT)
                if _ableton_connection.connect():
                    logger.info("Created new persistent connection to Ableton")
                    return _ableton_connection
                else:
                    _ableton_connection = None
            except Exception as e:
                logger.error(f"Connection attempt {attempt} failed: {str(e)}")
                if _ableton_connection:
                    _ableton_connection.disconnect()
                    _ableton_connection = None

            if attempt < max_attempts:
                import time
                time.sleep(1.0)
        
        # If we get here, all connection attempts failed
        if _ableton_connection is None:
            logger.error("Failed to connect to Ableton after multiple attempts")
            raise Exception("Could not connect to Ableton. Make sure the Remote Script is running.")
    
    return _ableton_connection


# Core Tool endpoints

@mcp.tool()
def set_dataset_consent(ctx: Context, consent: bool, user_said: str = "") -> str:
    """Record the user's answer to the dataset consent question.

    Call this ONLY after the user has answered in their own words. Never infer
    the answer, never call it on their behalf, and never call it because
    contributing seems helpful — consent the user did not give is not consent.
    If they have not been asked yet, ask first and wait for their reply.

    Parameters:
    - consent: True if the user agreed to contribute, False if they declined
    - user_said: The user's reply, quoted as closely as possible
    """
    # Intentionally undecorated: recording this call would mean collecting data
    # from someone who may have just declined.
    try:
        from .dataset.consent import record_consent

        state = record_consent(consent, quote=user_said)
    except Exception as e:
        logger.error(f"Could not record dataset consent: {str(e)}")
        return f"Error recording consent: {str(e)}"

    if not consent:
        return (
            "Recorded: dataset contribution declined. Nothing is uploaded, and "
            "you will not be asked again."
        )

    try:
        from .telemetry import refresh_consent_from_dataset

        refresh_consent_from_dataset()
        recorder = get_recorder()
        # Under opt-in the poller is almost never started at boot — consent did
        # not exist yet — so a grant has to start it here or passive Live events
        # would be missed for the rest of the session. Idempotent and
        # self-gating, so this is safe even if startup did start it.
        from .dataset.passive_poller import start_passive_poller

        start_passive_poller()
    except Exception as e:
        logger.error(f"Consent saved but recording failed to start: {str(e)}")
        return (
            "Consent saved, but recording could not start in this session. "
            "It will begin after a restart."
        )

    if recorder is None:
        return (
            "Consent saved, but recording is unavailable (telemetry may be "
            f"disabled). State: {state}."
        )
    return (
        "Thank you — this session is now being contributed to the open dataset. "
        "Say so at any time to stop, or set ABLETON_MCP_DISABLE_DATASET=1."
    )


@mcp.tool(annotations=READ_ONLY)
@telemetry_tool("get_session_info")
@trajectory_tool("get_session_info")
def get_session_info(ctx: Context, user_prompt: str = "") -> str:
    """Get detailed information about the current Ableton session

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_session_info")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting session info from Ableton: {str(e)}")
        return f"Error getting session info: {str(e)}"


@mcp.tool(annotations=READ_ONLY)
@telemetry_tool("get_remote_script_info")
@trajectory_tool("get_remote_script_info", modifying=False)
def get_remote_script_info(ctx: Context, user_prompt: str = "") -> str:
    """
    Report Ableton Remote Script version and capabilities (handshake).

    Use this to verify the Live-side bridge matches this MCP server package.
    """
    try:
        from .remote_script_install import EXPECTED_REMOTE_SCRIPT_VERSION
        from .script_handshake import get_cached_script_info, handshake

        ableton = get_ableton_connection()
        info = handshake(ableton.send_command)
        cached = dict(get_cached_script_info() or info)
        cached["expected_version"] = EXPECTED_REMOTE_SCRIPT_VERSION
        return json.dumps(cached, indent=2)
    except Exception as e:
        logger.error(f"Error getting remote script info: {str(e)}")
        return f"Error getting remote script info: {str(e)}"

@mcp.tool(annotations=READ_ONLY)
@telemetry_tool("get_track_info")
@trajectory_tool("get_track_info")
def get_track_info(ctx: Context, track_index: int, user_prompt: str = "") -> str:
    """
    Get detailed information about a specific track in Ableton.

    Parameters:
    - track_index: The index of the track to get information about
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_track_info", {"track_index": track_index})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting track info from Ableton: {str(e)}")
        return f"Error getting track info: {str(e)}"


@mcp.tool(annotations=READ_ONLY)
@telemetry_tool("get_clip_notes")
@trajectory_tool("get_clip_notes")
def get_clip_notes(
    ctx: Context,
    track_index: int,
    clip_index: int,
    user_prompt: str = "",
) -> str:
    """
    Read all MIDI notes from a Session-view clip.

    Returns pitch, start_time, duration, velocity, mute (and extended fields when available).

    Parameters:
    - track_index: Track that owns the clip
    - clip_index: Session clip slot index
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability("get_clip_notes")
        if missing:
            return missing
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "get_clip_notes",
            {"track_index": track_index, "clip_index": clip_index},
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting clip notes: {str(e)}")
        return f"Error getting clip notes: {str(e)}"


@mcp.tool(annotations=READ_ONLY)
@telemetry_tool("get_device_parameters")
@trajectory_tool("get_device_parameters")
def get_device_parameters(
    ctx: Context,
    track_index: int,
    device_index: int,
    user_prompt: str = "",
) -> str:
    """
    Read all parameters for a device on a track (name, value, min, max).

    Parameters:
    - track_index: Track that owns the device
    - device_index: Index into the track's device chain
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "get_device_parameters",
            {"track_index": track_index, "device_index": device_index},
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting device parameters: {str(e)}")
        return f"Error getting device parameters: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("set_device_parameter")
@trajectory_tool("set_device_parameter")
def set_device_parameter(
    ctx: Context,
    track_index: int,
    device_index: int,
    parameter_index: int,
    value: float,
    user_prompt: str = "",
) -> str:
    """
    Set a device parameter to a specific value.

    Use get_device_parameters first to discover parameter indices and ranges.

    Parameters:
    - track_index: Track that owns the device
    - device_index: Index into the track's device chain
    - parameter_index: Index into device.parameters
    - value: New parameter value (Live parameter units)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "set_device_parameter",
            {
                "track_index": track_index,
                "device_index": device_index,
                "parameter_index": parameter_index,
                "value": value,
            },
        )
        return (
            f"Set {result.get('name', 'parameter')} "
            f"{result.get('old_value')} → {result.get('value')}"
        )
    except Exception as e:
        logger.error(f"Error setting device parameter: {str(e)}")
        return f"Error setting device parameter: {str(e)}"


@mcp.tool(annotations=READ_ONLY)
@telemetry_tool("get_session_snapshot")
@trajectory_tool("get_session_snapshot")
def get_session_snapshot(
    ctx: Context,
    include_notes: bool = True,
    include_params: bool = True,
    user_prompt: str = "",
) -> str:
    """
    Capture a full project state snapshot for trajectory recording.

    Includes session metadata, every track (mixer, devices, session clips,
    arrangement clips), optional MIDI notes, and optional device parameters.
    Used to version musical state S_t → S_{t+1}.

    Parameters:
    - include_notes: Include MIDI note arrays in clips (default True)
    - include_params: Include device parameter values (default True)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability("get_session_snapshot")
        if missing:
            return missing
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "get_session_snapshot",
            {
                "include_notes": include_notes,
                "include_params": include_params,
            },
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting session snapshot: {str(e)}")
        return f"Error getting session snapshot: {str(e)}"


@mcp.tool()
@telemetry_tool("create_midi_track")
@trajectory_tool("create_midi_track")
def create_midi_track(ctx: Context, index: int = -1, user_prompt: str = "") -> str:
    """
    Create a new MIDI track in the Ableton session.

    Parameters:
    - index: The index to insert the track at (-1 = end of list)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_midi_track", {"index": index})
        return f"Created new MIDI track: {result.get('name', 'unknown')}"
    except Exception as e:
        logger.error(f"Error creating MIDI track: {str(e)}")
        return f"Error creating MIDI track: {str(e)}"


@mcp.tool()
@telemetry_tool("create_audio_track")
@trajectory_tool("create_audio_track")
def create_audio_track(ctx: Context, index: int = -1, user_prompt: str = "") -> str:
    """
    Create a new audio track in the Ableton session.

    Use this for recorded or imported audio (samples, stems, vocals). For MIDI
    instruments use create_midi_track instead.

    Parameters:
    - index: The index to insert the track at (-1 = end of list)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_audio_track", {"index": index})
        return f"Created new audio track: {result.get('name', 'unknown')}"
    except Exception as e:
        logger.error(f"Error creating audio track: {str(e)}")
        return f"Error creating audio track: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("set_track_name")
@trajectory_tool("set_track_name")
def set_track_name(ctx: Context, track_index: int, name: str, user_prompt: str = "") -> str:
    """
    Set the name of a track.

    Parameters:
    - track_index: The index of the track to rename
    - name: The new name for the track
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_track_name", {"track_index": track_index, "name": name})
        return f"Renamed track to: {result.get('name', name)}"
    except Exception as e:
        logger.error(f"Error setting track name: {str(e)}")
        return f"Error setting track name: {str(e)}"

def _track_layout_command(command: str, **params: Any) -> dict[str, Any]:
    try:
        return get_ableton_connection().send_command(command, params)
    except Exception as exc:
        if f"Unknown command: {command}" in str(exc):
            raise ToolError(
                "The Ableton Remote Script does not support this tool. "
                "Run ableton-mcp-install-script with the updated package and restart Ableton Live."
            ) from exc
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=READ_ONLY)
def get_track_layout() -> dict[str, Any]:
    """List normal and Return tracks, their names and colors, and normal tracks' Sends.

    The two track lists use separate zero-based indices. Each Send includes its
    destination Return index and name, current parameter value, percentage of the
    parameter range, and Live's display value when available.
    """
    return _track_layout_command("get_track_layout")


@mcp.tool(annotations=READ_ONLY)
def get_track_routing(
    track_index: TrackIndex,
    direction: RoutingDirection,
    track_kind: TrackKind = "track",
) -> dict[str, Any]:
    """Read a track's input or output selectors and the exact choices Live offers.

    Use get_track_layout to find the index. Return tracks support output routing.
    Channel choices depend on the current routing type and may be empty.
    """
    return _track_layout_command(
        "get_track_routing", track_index=track_index, direction=direction, track_kind=track_kind,
    )


@mcp.tool(annotations=MIXER_UPDATE)
def set_track_color(
    track_index: TrackIndex,
    color: Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")],
    track_kind: TrackKind = "track",
) -> dict[str, Any]:
    """Set a normal or Return track's color using #RRGGBB, such as #3366FF.

    Use get_track_layout to find the index. Live selects the closest palette
    color. The response reports the color Live actually applied.
    """
    return _track_layout_command(
        "set_track_color", track_index=track_index, color=color, track_kind=track_kind,
    )


@mcp.tool(annotations=MIXER_UPDATE)
def set_track_routing(
    track_index: TrackIndex,
    direction: RoutingDirection,
    selector: Literal["type", "channel"],
    choice: str,
    track_kind: TrackKind = "track",
) -> dict[str, Any]:
    """Set one input or output routing selector on a track.

    Read get_track_routing first and pass an exact choice from Live's list.
    For both type and channel, set the type first and use the returned channel
    choices. Duplicate choice names are ambiguous. Return tracks support output routing.
    """
    return _track_layout_command(
        "set_track_routing", track_index=track_index, direction=direction,
        selector=selector, choice=choice, track_kind=track_kind,
    )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def create_return_track(name: str) -> dict[str, Any]:
    """Add an empty Return track at the end of Live's Return tracks and name it.

    A Return with the same name is refused. Read the layout before retrying an
    interrupted request.
    """
    return _track_layout_command("create_return_track", name=name)


@mcp.tool(annotations=MIXER_UPDATE)
def set_return_track_name(return_index: TrackIndex, name: str) -> dict[str, Any]:
    """Rename a Return track. Read its index from get_track_layout."""
    return _track_layout_command("set_return_track_name", return_index=return_index, name=name)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False))
def delete_return_track(return_index: TrackIndex, expected_name: str) -> dict[str, Any]:
    """Delete a Return track, its devices and the corresponding Sends.

    Use only when the user asks to delete that Return. Read get_track_layout
    first and supply the current exact name and index. An ambiguous or stale
    name is refused. Refresh the layout after deletion because indices can shift.
    """
    return _track_layout_command(
        "delete_return_track", return_index=return_index, expected_name=expected_name,
    )


@mcp.tool(annotations=MIXER_UPDATE)
def set_track_send(
    track_index: TrackIndex,
    return_index: TrackIndex,
    percent: Annotated[float, Field(ge=0, le=100, strict=True, allow_inf_nan=False)],
    expected_return_name: str | None = None,
) -> dict[str, Any]:
    """Set a normal track's Send to a Return by percentage of Live's parameter range.

    Use get_track_layout to find the source track and destination Return indices.
    Pass the destination's current name to guard against a stale Return index.
    0 is the minimum and 100 is the maximum. Read the returned display value
    for the level shown in Live. Percentage specifies the parameter range, not dB.
    """
    return _track_layout_command(
        "set_track_send", track_index=track_index, return_index=return_index, percent=percent,
        expected_return_name=expected_return_name,
    )


@mcp.tool()
@rich_telemetry_tool("create_clip")
@trajectory_tool("create_clip")
def create_clip(ctx: Context, track_index: int, clip_index: int, length: float = 4.0, user_prompt: str = "") -> str:
    """
    Create a new MIDI clip in the specified track and clip slot.

    Parameters:
    - track_index: The index of the track to create the clip in
    - clip_index: The index of the clip slot to create the clip in
    - length: The length of the clip in beats (default: 4.0)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_clip", {
            "track_index": track_index, 
            "clip_index": clip_index, 
            "length": length
        })
        return f"Created new clip at track {track_index}, slot {clip_index} with length {length} beats"
    except Exception as e:
        logger.error(f"Error creating clip: {str(e)}")
        return f"Error creating clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("create_audio_clip")
@trajectory_tool("create_audio_clip")
def create_audio_clip(ctx: Context, track_index: int, clip_index: int, path: str, user_prompt: str = "") -> str:
    """
    Create a new audio clip in an audio track's clip slot by importing a file.

    Requires Ableton Live 12.0.5 or newer — the underlying
    ClipSlot.create_audio_clip Live API was introduced in 12.0.5 and is not
    available in earlier 12.0.x releases.

    Parameters:
    - track_index: The index of the audio track to create the clip in
    - clip_index: The index of the clip slot to create the clip in
    - path: Absolute path to a supported audio file (e.g. a .wav). The target
      track must be an audio track and the clip slot must be empty.
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_audio_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
            "path": path
        })
        return f"Created audio clip '{result.get('name', 'clip')}' at track {track_index}, slot {clip_index} (length {result.get('length', '?')} beats)"
    except Exception as e:
        logger.error(f"Error creating audio clip: {str(e)}")
        return f"Error creating audio clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("add_notes_to_clip", capture_notes=True)
@trajectory_tool("add_notes_to_clip")
def add_notes_to_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    notes: List[Dict[str, Union[int, float, bool]]],
    user_prompt: str = ""
) -> str:
    """
    Add MIDI notes to a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - notes: List of note dictionaries, each with pitch, start_time, duration, velocity, and mute
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("add_notes_to_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
            "notes": notes
        })
        return f"Added {len(notes)} notes to clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error adding notes to clip: {str(e)}")
        return f"Error adding notes to clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("clear_notes_from_clip")
def clear_notes_from_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    user_prompt: str = ""
) -> str:
    """
    Remove all MIDI notes from a Session clip.

    Writes are additive (add_notes_to_clip only appends), so to truly *modify*
    a clip you clear it first, then add the new notes. Use this with
    get_clip_notes and add_notes_to_clip for a real read -> modify -> write
    loop: read the notes, edit the list, clear_notes_from_clip, then
    add_notes_to_clip the edited notes.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability("clear_notes_from_clip")
        if missing:
            return missing
        ableton = get_ableton_connection()
        result = ableton.send_command("clear_notes_from_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return "Cleared {n} note(s) from clip '{name}' (track {t}, slot {c})".format(
            n=result.get("cleared_count", "?"),
            name=result.get("clip_name", "clip"),
            t=track_index,
            c=clip_index,
        )
    except Exception as e:
        logger.error(f"Error clearing notes from clip: {str(e)}")
        return f"Error clearing notes from clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_clip_name")
@trajectory_tool("set_clip_name")
def set_clip_name(ctx: Context, track_index: int, clip_index: int, name: str, user_prompt: str = "") -> str:
    """
    Set the name of a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - name: The new name for the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_clip_name", {
            "track_index": track_index,
            "clip_index": clip_index,
            "name": name
        })
        return f"Renamed clip at track {track_index}, slot {clip_index} to '{name}'"
    except Exception as e:
        logger.error(f"Error setting clip name: {str(e)}")
        return f"Error setting clip name: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_arrangement_clip_name")
@trajectory_tool("set_arrangement_clip_name")
def set_arrangement_clip_name(ctx: Context, track_index: int, clip_index: int, name: str, user_prompt: str = "") -> str:
    """
    Set the name of a clip placed in the Arrangement timeline.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip within track.arrangement_clips, in the
      same order returned by get_arrangement_clips (i.e. ordered by start_time)
    - name: The new name for the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_arrangement_clip_name", {
            "track_index": track_index,
            "clip_index": clip_index,
            "name": name
        })
        return f"Renamed arrangement clip at track {track_index}, index {clip_index} to '{name}'"
    except Exception as e:
        logger.error(f"Error setting arrangement clip name: {str(e)}")
        return f"Error setting arrangement clip name: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_tempo")
@trajectory_tool("set_tempo")
def set_tempo(ctx: Context, tempo: float, user_prompt: str = "") -> str:
    """
    Set the tempo of the Ableton session.

    Parameters:
    - tempo: The new tempo in BPM
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_tempo", {"tempo": tempo})
        return f"Set tempo to {tempo} BPM"
    except Exception as e:
        logger.error(f"Error setting tempo: {str(e)}")
        return f"Error setting tempo: {str(e)}"


SERUM_VARIANTS = {"instrument": "Serum 2", "fx": "Serum 2 FX"}
SERUM_BROWSER_PATHS = {
    "vst3": "plugins/VST3/Xfer Records",
    "au": "plugins/AUv2/Xfer Records",
}


@mcp.tool(annotations=READ_ONLY)
def list_omnisphere_plugins() -> dict[str, list[dict[str, str]]]:
    """List the loadable Omnisphere VST3 and Audio Units entries in Live."""
    ableton = get_ableton_connection()
    try:
        return {"plugins": [
            item
            for plugin_format in omnisphere.BROWSER_PATHS
            for item in omnisphere.browser_plugins(ableton, plugin_format)
        ]}
    except omnisphere.OmnisphereError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=READ_ONLY)
def list_omnisphere_patches(
    query: Annotated[str, Field(max_length=200)] = "",
    source: Literal["all", "factory", "user"] = "all",
    limit: Annotated[int, Field(ge=1, le=200)] = 30,
) -> dict[str, Any]:
    """Search installed Omnisphere part patches by name, library or category.

    Pass a returned patch id to export_omnisphere_live_preset. This searches
    the libraries on the computer running the MCP server.
    """
    try:
        return omnisphere.list_patches(omnisphere.patch_directory(), query, source, limit)
    except (omnisphere.OmnisphereError, OSError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def export_omnisphere_live_preset(
    patch_id: Annotated[str, Field(min_length=1, max_length=512)],
    name: Annotated[str, Field(min_length=1, max_length=96)],
) -> dict[str, str]:
    """Export a verified Omnisphere Part 1 patch as a VST3 preset in Live's User Library.

    Use a patch id from list_omnisphere_patches and a new preset name. Requires
    Omnisphere VST3 and the macOS host probe. Call load_omnisphere_plugin with
    preset_name after Live indexes the export.
    """
    try:
        return omnisphere.export_live_preset(patch_id, name)
    except (omnisphere.OmnisphereError, OSError, ParseError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False))
def load_omnisphere_plugin(
    track_index: TrackIndex,
    plugin_format: omnisphere.OmnisphereFormat = "auto",
    preset_name: Annotated[str, Field(min_length=1, max_length=96)] | None = None,
) -> dict[str, Any]:
    """Load Omnisphere on an empty Live MIDI track, optionally with an exported patch.

    Auto prefers VST3 and falls back to AU for the unconfigured instrument.
    An exported preset requires VST3. Check get_track_info after a request
    before retrying.
    """
    ableton = get_ableton_connection()
    track = ableton.send_command("get_track_info", {"track_index": track_index})
    if not track.get("is_midi_track") or track.get("devices"):
        raise ToolError("Select an empty MIDI track for Omnisphere.")
    try:
        if preset_name is not None:
            if plugin_format == "au":
                raise ToolError("Exported Omnisphere presets require VST3.")
            filename = omnisphere.preset_filename(preset_name)
            browser = ableton.send_command("get_browser_items_at_path", {"path": omnisphere.LIVE_PRESET_BROWSER})
            candidates = [
                {"name": omnisphere.NAME, "format": "vst3", "uri": item["uri"]}
                for item in browser.get("items", [])
                if item.get("name") == filename and item.get("is_loadable")
                and isinstance(item.get("uri"), str) and item["uri"].strip()
            ]
            if len(candidates) != 1:
                raise ToolError(f"Live has not indexed exactly one loadable {filename!r} in {omnisphere.LIVE_PRESET_FOLDER}.")
        else:
            formats = omnisphere.BROWSER_PATHS if plugin_format == "auto" else (plugin_format,)
            candidates = []
            for candidate_format in formats:
                candidates = omnisphere.browser_plugins(ableton, candidate_format)
                if candidates:
                    break
            if not candidates:
                raise ToolError(f"Omnisphere is unavailable in Live for format {plugin_format!r}.")
            if len(candidates) != 1:
                raise ToolError("Live reports multiple matching Omnisphere plug-ins.")
    except omnisphere.OmnisphereError as exc:
        raise ToolError(str(exc)) from exc

    plugin = candidates[0]
    try:
        result = ableton.send_command("load_browser_item", {"track_index": track_index, "item_uri": plugin["uri"]})
    except Exception as exc:
        raise ToolError(f"Could not confirm the Omnisphere load request. Check the device chain before retrying. {exc}") from exc
    if result.get("loaded") is not True:
        raise ToolError("Live did not acknowledge the Omnisphere load request.")
    return {**plugin, "track_index": track_index, "preset_name": preset_name, "status": "load_requested"}


@mcp.prompt(title="Design a Serum sound", description="Turn a natural-language sound brief into a rendered Serum preset.")
def serum_sound_design(brief: str) -> str:
    return f"{SERUM_DESIGN_WORKFLOW}\n{SERUM_CONTROL_HINTS}\nSound brief\n\n{brief}"


@mcp.tool(annotations=READ_ONLY)
def get_serum_design_workflow() -> dict[str, str]:
    """Get the authoring workflow for a user's natural-language Serum 2 or Serum 2 FX request.

    Use this for creation or revision before calling the state-edit, audio
    comparison, and preset-save tools. The MCP client interprets the brief.
    """
    return {"workflow": SERUM_DESIGN_WORKFLOW, "control_hints": SERUM_CONTROL_HINTS}


@mcp.tool(annotations=READ_ONLY)
def list_serum_assets(
    mode: Mode,
    search: str = "",
    offset: Annotated[int, Field(ge=0, strict=True)] = 0,
    limit: Annotated[int, Field(ge=1, le=200, strict=True)] = 50,
) -> dict[str, Any]:
    """Find installed Serum assets by oscillator mode. Use a returned relative path in edit_serum_state."""
    try:
        return list_assets(mode, search, offset, limit)
    except (SerumError, OSError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=READ_ONLY)
def get_serum_control_state(
    variant: Variant,
    state_id: str | None = None,
    path: StatePath | None = None,
) -> dict[str, Any]:
    """Inspect Serum 2.1.5 control sections in an isolated processor.

    Omit state_id for a fresh instance. Omit path for section names and FX choices.
    Use a path such as ["Oscillator0", "plainParams"] to read an individual section.
    """
    try:
        state = initial_state(variant, state_id)
        if path is None:
            return {
                "variant": variant, "build": SUPPORTED_BUILD, "sections": sorted(state.data),
                "effect_modules": list(FX_MODULES), "effect_buses": list(RACKS),
            }
        value = value_at(state.data, path)
        source = "processor"
        if value is MISSING:
            value = native_default(state.data, path)
            source = "native_default"
        if value is MISSING:
            raise SerumError(f"Serum state path is absent: {path}")
        if path == ["ClipPlayer0", "activeClips"] and state_id is not None:
            source = "preset"
        return {"variant": variant, "state_id": state_id, "path": path,
                "value": describe(value, path[-1]), "source": source}
    except (SerumError, OSError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def edit_serum_state(variant: Variant, edits: SerumEdits, base_state_id: str | None = None) -> dict[str, Any]:
    """Change Serum 2.1.5 settings in an isolated VST3 instance and verify processor readback.

    Start from Init or pass the state_id from a previous edit. Values take a path
    and native value. For example ["Env0","plainParams","kParamAttack"] takes
    an envelope attack value. Oscillator assets select an installed wavetable or
    sample by category-relative path. Embedded assets store a local PCM16 WAV
    inside the patch. Effect chains replace one bus in the given order.
    Inspect with get_serum_control_state before issuing unfamiliar edits.
    """
    try:
        return edit_state(variant, edits, base_state_id)
    except (SerumError, OSError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def save_serum_preset(variant: Variant, state_id: str, details: PresetDetails) -> dict[str, Any]:
    """Save a verified state as a new native .SerumPreset. Returns its absolute path.

    Load the file in the matching Serum editor in your DAW. A name conflict
    leaves the existing file intact. Use a different name for a revision.
    """
    try:
        return export_preset(variant, state_id, details)
    except (SerumError, OSError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=READ_ONLY)
def list_serum_factory_presets(
    variant: Variant | None = None,
    search: str = "",
    offset: Annotated[int, Field(ge=0, strict=True)] = 0,
    limit: Annotated[int, Field(ge=1, le=200, strict=True)] = 50,
) -> dict[str, Any]:
    """Find installed Factory presets by name or category such as Pad.

    Pass an entry's absolute path and variant to import_serum_preset.
    """
    try:
        return list_factory_presets(variant, search, offset, limit)
    except (SerumError, OSError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def import_serum_preset(variant: Variant, path: str) -> dict[str, Any]:
    """Open a supported native Serum preset as an editable processor state.

    Pass the absolute .SerumPreset path. Use the returned state_id in edit_serum_state.
    The installed Serum 2.1.5 processor migrates supported older Factory formats.
    normalized_sections lists where the processor changed or omitted native file data.
    preserved_preset_clips lists active clip indices carried into the editable state.
    """
    try:
        return import_preset(variant, path)
    except (SerumError, OSError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def export_serum_live_preset(
    variant: Variant, state_id: str, name: Annotated[str, Field(min_length=1, max_length=96)],
    plugin_format: LiveFormat = "vst3",
) -> dict[str, Any]:
    """Write a new Serum VST3 or Audio Units preset to Live's User Library.

    Let Live index the file, then use load_serum_live_preset with the same name.
    Use save_serum_preset to create the cross-host native .SerumPreset file.
    """
    try:
        return export_live_preset(variant, state_id, name, plugin_format)
    except (SerumError, OSError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False))
def load_serum_live_preset(
    variant: Variant, track_index: TrackIndex, name: Annotated[str, Field(min_length=1, max_length=96)],
    plugin_format: LiveFormat = "vst3",
) -> dict[str, Any]:
    """Request loading a named Serum preset from the managed Live User Library folder.

    Export the preset first with export_serum_live_preset. Use get_track_info
    to confirm Live's device chain after the load request.
    """
    try:
        filename = check_live_preset(variant, name, plugin_format)
    except (SerumError, OSError) as exc:
        raise ToolError(str(exc)) from exc
    ableton = get_ableton_connection()
    track = ableton.send_command("get_track_info", {"track_index": track_index})
    if variant == "instrument" and not track.get("is_midi_track"):
        raise ToolError("Serum 2 instrument presets require a MIDI track.")
    browser = ableton.send_command("get_browser_items_at_path", {"path": LIVE_PRESET_BROWSER})
    matches = [item for item in browser.get("items", []) if item.get("name") == filename and item.get("is_loadable")]
    if len(matches) != 1 or not matches[0].get("uri"):
        raise ToolError(f"Live has not indexed exactly one loadable {filename!r} in User Library/{LIVE_PRESET_FOLDER}.")
    uri = matches[0]["uri"]
    result = ableton.send_command("load_browser_item", {"track_index": track_index, "item_uri": uri})
    if result.get("loaded") is not True:
        raise ToolError(f"Live did not acknowledge the load request for {filename!r}. Check the device chain before retrying.")
    return {"variant": variant, "track_index": track_index, "filename": filename,
            "format": plugin_format, "uri": uri, "status": "load_requested"}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def render_serum_audio(
    variant: Variant, state_id: str | None = None, audition: Audition | None = None,
    reference_state_id: str | None = None,
) -> dict[str, Any]:
    """Render a Serum patch in isolation and return a WAV and measured sound traits.

    Omit state_id to audition Init. Instrument notes specify MIDI pitch, start
    and length in seconds, optional velocity 0–1 and MIDI channel 0–15. For FX,
    provide audition.audio_path for a local PCM WAV or omit it for a test signal.
    Pass a reference_state_id to compare two patches using the same input.
    """
    try:
        return render_audio(variant, state_id, audition or Audition(), reference_state_id)
    except (SerumError, OSError) as exc:
        raise ToolError(str(exc)) from exc


def _serum_browser_items(ableton: AbletonConnection, plugin_format: str) -> list[dict[str, str]]:
    path = SERUM_BROWSER_PATHS[plugin_format]
    result = ableton.send_command("get_browser_items_at_path", {"path": path})
    error = result.get("error")
    if error:
        if error in {
            "Unknown or unavailable category: plugins",
            f"Path part '{path.split('/')[1]}' not found",
            "Path part 'Xfer Records' not found",
        }:
            return []
        raise ToolError(f"Could not read Live's {plugin_format} browser: {error}")

    matches = []
    for item in result["items"]:
        uri = item.get("uri")
        if (
            item.get("name") in SERUM_VARIANTS.values()
            and item.get("is_loadable")
            and isinstance(uri, str)
            and uri.strip()
        ):
            matches.append({"name": item["name"], "format": plugin_format, "uri": uri})
    return matches


@mcp.tool(annotations=READ_ONLY)
def list_serum_plugins() -> dict[str, list[dict[str, str]]]:
    """List the Serum 2 and Serum 2 FX entries Live can load, with format and browser URI."""
    ableton = get_ableton_connection()
    return {
        "plugins": [
            item
            for plugin_format in SERUM_BROWSER_PATHS
            for item in _serum_browser_items(ableton, plugin_format)
        ]
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False))
def load_serum_plugin(
    track_index: TrackIndex,
    variant: Literal["instrument", "fx"],
    plugin_format: Literal["auto", "vst3", "au"] = "auto",
) -> dict[str, Any]:
    """Ask Live to load Serum 2 on a MIDI track or Serum 2 FX in an audio signal chain.

    Find the current regular track index with get_track_layout. Auto prefers VST3 and
    uses AU only if Live cannot find the requested VST3 entry. Read get_track_info
    after the request to check the device chain, especially before retrying a failure.
    """
    ableton = get_ableton_connection()
    name = SERUM_VARIANTS[variant]
    formats = SERUM_BROWSER_PATHS if plugin_format == "auto" else (plugin_format,)
    for candidate in formats:
        matches = [item for item in _serum_browser_items(ableton, candidate) if item["name"] == name]
        if not matches:
            continue
        if len(matches) > 1:
            raise ToolError(f"Live reports multiple loadable {candidate} entries named {name}.")
        if variant == "instrument":
            track = ableton.send_command("get_track_info", {"track_index": track_index})
            if not track.get("is_midi_track"):
                raise ToolError("Serum 2 requires a MIDI track. Use variant='fx' for Serum 2 FX.")

        plugin = matches[0]
        try:
            result = ableton.send_command(
                "load_browser_item", {"track_index": track_index, "item_uri": plugin["uri"]}
            )
        except Exception as exc:
            raise ToolError(
                f"Could not confirm Live accepted the load request for {name}. "
                f"Check the device chain before retrying. {exc}"
            ) from exc
        if result.get("loaded") is not True:
            raise ToolError(f"Live did not acknowledge the load request for {name}.")
        return {**plugin, "track_index": track_index, "status": "load_requested"}

    raise ToolError(
        f"{name} is unavailable in Live for format '{plugin_format}'. "
        "Enable the plug-in source in Live and rescan."
    )


@mcp.tool()
@rich_telemetry_tool("load_instrument_or_effect")
@trajectory_tool("load_instrument_or_effect")
def load_instrument_or_effect(ctx: Context, track_index: int, uri: str, user_prompt: str = "") -> str:
    """
    Load an instrument or effect onto a track using its URI.

    Parameters:
    - track_index: The index of the track to load the instrument on
    - uri: The URI of the instrument or effect to load (e.g., 'query:Synths#Instrument%20Rack:Bass:FileId_5116')
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": uri
        })
        
        # Check if the instrument was loaded successfully
        if result.get("loaded", False):
            new_devices = result.get("new_devices", [])
            if new_devices:
                return f"Loaded instrument with URI '{uri}' on track {track_index}. New devices: {', '.join(new_devices)}"
            else:
                devices = result.get("devices_after", [])
                return f"Loaded instrument with URI '{uri}' on track {track_index}. Devices on track: {', '.join(devices)}"
        else:
            return f"Failed to load instrument with URI '{uri}'"
    except Exception as e:
        logger.error(f"Error loading instrument by URI: {str(e)}")
        return f"Error loading instrument by URI: {str(e)}"

@mcp.tool()
@telemetry_tool("fire_clip")
@trajectory_tool("fire_clip")
def fire_clip(ctx: Context, track_index: int, clip_index: int, user_prompt: str = "") -> str:
    """
    Start playing a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("fire_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return f"Started playing clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error firing clip: {str(e)}")
        return f"Error firing clip: {str(e)}"

@mcp.tool()
@telemetry_tool("stop_clip")
@trajectory_tool("stop_clip")
def stop_clip(ctx: Context, track_index: int, clip_index: int, user_prompt: str = "") -> str:
    """
    Stop playing a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("stop_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return f"Stopped clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error stopping clip: {str(e)}")
        return f"Error stopping clip: {str(e)}"


@mcp.tool()
@telemetry_tool("delete_clip")
def delete_clip(ctx: Context, track_index: int, clip_index: int, user_prompt: str = "") -> str:
    """
    Delete the clip in the given clip slot, freeing it for reuse.

    Use this before create_clip when you want to overwrite an existing clip
    (create_clip itself refuses to write into an occupied slot).

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot to clear
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability("delete_clip")
        if missing:
            return missing
        ableton = get_ableton_connection()
        result = ableton.send_command("delete_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error deleting clip: {str(e)}")
        return f"Error deleting clip: {str(e)}"


@mcp.tool()
@telemetry_tool("start_playback")
@trajectory_tool("start_playback")
def start_playback(ctx: Context, user_prompt: str = "") -> str:
    """Start playing the Ableton session.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("start_playback")
        return "Started playback"
    except Exception as e:
        logger.error(f"Error starting playback: {str(e)}")
        return f"Error starting playback: {str(e)}"

@mcp.tool()
@telemetry_tool("stop_playback")
@trajectory_tool("stop_playback")
def stop_playback(ctx: Context, user_prompt: str = "") -> str:
    """Stop playing the Ableton session.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("stop_playback")
        return "Stopped playback"
    except Exception as e:
        logger.error(f"Error stopping playback: {str(e)}")
        return f"Error stopping playback: {str(e)}"

@mcp.tool(annotations=READ_ONLY)
@rich_telemetry_tool("get_browser_tree")
@trajectory_tool("get_browser_tree")
def get_browser_tree(ctx: Context, category_type: str = "all", user_prompt: str = "") -> str:
    """
    Get a hierarchical tree of browser categories from Ableton.

    Parameters:
    - category_type: Type of categories to get ('all', 'instruments', 'sounds', 'drums', 'audio_effects', 'midi_effects')
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_browser_tree", {
            "category_type": category_type
        })
        
        # Check if we got any categories
        if "available_categories" in result and len(result.get("categories", [])) == 0:
            available_cats = result.get("available_categories", [])
            return (f"No categories found for '{category_type}'. "
                   f"Available browser categories: {', '.join(available_cats)}")
        
        # Format the tree in a more readable way
        total_folders = result.get("total_folders", 0)
        formatted_output = f"Browser tree for '{category_type}' (showing {total_folders} folders):\n\n"
        
        def format_tree(item, indent=0):
            output = ""
            if item:
                prefix = "  " * indent
                name = item.get("name", "Unknown")
                path = item.get("path", "")
                has_more = item.get("has_more", False)
                
                # Add this item
                output += f"{prefix}• {name}"
                if path:
                    output += f" (path: {path})"
                if has_more:
                    output += " [...]"
                output += "\n"
                
                # Add children
                for child in item.get("children", []):
                    output += format_tree(child, indent + 1)
            return output
        
        # Format each category
        for category in result.get("categories", []):
            formatted_output += format_tree(category)
            formatted_output += "\n"
        
        return formatted_output
    except Exception as e:
        error_msg = str(e)
        if "Browser is not available" in error_msg:
            logger.error(f"Browser is not available in Ableton: {error_msg}")
            return f"Error: The Ableton browser is not available. Make sure Ableton Live is fully loaded and try again."
        elif "Could not access Live application" in error_msg:
            logger.error(f"Could not access Live application: {error_msg}")
            return f"Error: Could not access the Ableton Live application. Make sure Ableton Live is running and the Remote Script is loaded."
        else:
            logger.error(f"Error getting browser tree: {error_msg}")
            return f"Error getting browser tree: {error_msg}"

@mcp.tool(annotations=READ_ONLY)
@rich_telemetry_tool("get_browser_items_at_path")
@trajectory_tool("get_browser_items_at_path")
def get_browser_items_at_path(ctx: Context, path: str, user_prompt: str = "") -> str:
    """
    Get browser items at a specific path in Ableton's browser.

    Parameters:
    - path: Path in the format "category/folder/subfolder"
            where category is one of the available browser categories in Ableton
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_browser_items_at_path", {
            "path": path
        })
        
        # Check if there was an error with available categories
        if "error" in result and "available_categories" in result:
            error = result.get("error", "")
            available_cats = result.get("available_categories", [])
            return (f"Error: {error}\n"
                   f"Available browser categories: {', '.join(available_cats)}")
        
        return json.dumps(result, indent=2)
    except Exception as e:
        error_msg = str(e)
        if "Browser is not available" in error_msg:
            logger.error(f"Browser is not available in Ableton: {error_msg}")
            return f"Error: The Ableton browser is not available. Make sure Ableton Live is fully loaded and try again."
        elif "Could not access Live application" in error_msg:
            logger.error(f"Could not access Live application: {error_msg}")
            return f"Error: Could not access the Ableton Live application. Make sure Ableton Live is running and the Remote Script is loaded."
        elif "Unknown or unavailable category" in error_msg:
            logger.error(f"Invalid browser category: {error_msg}")
            return f"Error: {error_msg}. Please check the available categories using get_browser_tree."
        elif "Path part" in error_msg and "not found" in error_msg:
            logger.error(f"Path not found: {error_msg}")
            return f"Error: {error_msg}. Please check the path and try again."
        else:
            logger.error(f"Error getting browser items at path: {error_msg}")
            return f"Error getting browser items at path: {error_msg}"

@mcp.tool()
@rich_telemetry_tool("load_drum_kit")
@trajectory_tool("load_drum_kit")
def load_drum_kit(ctx: Context, track_index: int, rack_uri: str, kit_path: str, user_prompt: str = "") -> str:
    """
    Load a drum rack and then load a specific drum kit into it.

    Parameters:
    - track_index: The index of the track to load on
    - rack_uri: The URI of the drum rack to load (e.g., 'Drums/Drum Rack')
    - kit_path: Path to the drum kit inside the browser (e.g., 'drums/acoustic/kit1')
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        
        # Step 1: Load the drum rack
        result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": rack_uri
        })
        
        if not result.get("loaded", False):
            return f"Failed to load drum rack with URI '{rack_uri}'"
        
        # Step 2: Get the drum kit items at the specified path
        kit_result = ableton.send_command("get_browser_items_at_path", {
            "path": kit_path
        })
        
        if "error" in kit_result:
            return f"Loaded drum rack but failed to find drum kit: {kit_result.get('error')}"
        
        # Step 3: Find a loadable drum kit
        kit_items = kit_result.get("items", [])
        loadable_kits = [item for item in kit_items if item.get("is_loadable", False)]
        
        if not loadable_kits:
            return f"Loaded drum rack but no loadable drum kits found at '{kit_path}'"
        
        # Step 4: Load the first loadable kit
        kit_uri = loadable_kits[0].get("uri")
        load_result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": kit_uri
        })
        
        return f"Loaded drum rack and kit '{loadable_kits[0].get('name')}' on track {track_index}"
    except Exception as e:
        logger.error(f"Error loading drum kit: {str(e)}")
        return f"Error loading drum kit: {str(e)}"

# ── Arrangement view tools ────────────────────────────────────────────────────

@mcp.tool()
@telemetry_tool("switch_to_arrangement_view")
@trajectory_tool("switch_to_arrangement_view")
def switch_to_arrangement_view(ctx: Context, user_prompt: str = "") -> str:
    """Switch Ableton's main window to the Arrangement view.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        ableton.send_command("switch_to_arrangement_view")
        return "Switched to Arrangement view"
    except Exception as e:
        logger.error(f"Error switching to arrangement view: {str(e)}")
        return f"Error switching to arrangement view: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("set_arrangement_time")
@trajectory_tool("set_arrangement_time")
def set_arrangement_time(ctx: Context, time: float, user_prompt: str = "") -> str:
    """
    Move the arrangement playhead to a specific position.

    Parameters:
    - time: Position in beats from the start of the arrangement (e.g. 8.0 = bar 3 in 4/4)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_current_song_time", {"time": time})
        return f"Playhead moved to beat {result.get('current_song_time', time)}"
    except Exception as e:
        logger.error(f"Error setting arrangement time: {str(e)}")
        return f"Error setting arrangement time: {str(e)}"


@mcp.tool(annotations=READ_ONLY)
@telemetry_tool("get_arrangement_clips")
@trajectory_tool("get_arrangement_clips")
def get_arrangement_clips(ctx: Context, track_index: int, user_prompt: str = "") -> str:
    """
    List all clips placed in the Arrangement timeline for a track.

    Returns each clip's name, start_time, end_time, length, and type.

    Parameters:
    - track_index: The index of the track to inspect
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_arrangement_clips", {"track_index": track_index})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting arrangement clips: {str(e)}")
        return f"Error getting arrangement clips: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("duplicate_to_arrangement")
@trajectory_tool("duplicate_to_arrangement")
def duplicate_to_arrangement(
    ctx: Context,
    track_index: int,
    clip_index: int,
    destination_time: float,
    user_prompt: str = ""
) -> str:
    """
    Copy a Session-view clip into the Arrangement timeline.

    Uses Live's track.duplicate_clip_to_arrangement() API (Live 11 / 12).
    The clip is placed at destination_time beats from the start of the
    arrangement on the same track it lives in.

    Typical workflow:
      1. create_clip / add_notes_to_clip to build a Session clip
      2. Call duplicate_to_arrangement once per bar/section you need
      3. Call switch_to_arrangement_view to confirm the result in Live

    Parameters:
    - track_index:       Index of the track that owns the Session clip
    - clip_index:        Index of the clip slot in that track (Session view)
    - destination_time:  Beat position in the arrangement to place the clip
                         (e.g. 0.0 = start, 8.0 = bar 3 in 4/4)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "duplicate_session_clip_to_arrangement",
            {
                "track_index": track_index,
                "clip_index": clip_index,
                "destination_time": destination_time
            }
        )
        clip_name = result.get("clip_name", "clip")
        track_name = result.get("track_name", f"track {track_index}")
        return (
            f"Duplicated '{clip_name}' from Session slot {clip_index} "
            f"on '{track_name}' to arrangement at beat {destination_time}"
        )
    except Exception as e:
        logger.error(f"Error duplicating clip to arrangement: {str(e)}")
        return f"Error duplicating clip to arrangement: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("create_locator")
def create_locator(
    ctx: Context,
    name: str,
    time: float,
    user_prompt: str = ""
) -> str:
    """
    Create a named locator (cue point) in the Arrangement at a beat position.

    If a locator already exists at that beat (within ~1e-3 tolerance) it is
    renamed instead of toggled off. Time is in beats from the start of the
    arrangement (e.g. 0.0 = start, 16.0 = bar 5 in 4/4).

    Parameters:
    - name: The locator label (e.g. "Chorus", "Verse 1", "Drop")
    - time: Beat position where the locator should sit
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability("create_locator")
        if missing:
            return missing
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "create_locator",
            {"name": name, "time": time}
        )
        return (
            f"Locator '{result.get('name', name)}' set at beat "
            f"{result.get('time', time)}"
        )
    except Exception as e:
        logger.error(f"Error creating locator: {str(e)}")
        return f"Error creating locator: {str(e)}"


# ── Dataset / preference tools (Supabase trajectory recording) ─────────────────

@mcp.tool()
@telemetry_tool("submit_intent")
@trajectory_tool("submit_intent", modifying=False)
def submit_intent(
    ctx: Context,
    text: str,
    level: int = 5,
    user_prompt: str = "",
) -> str:
    """
    Record the human's creative intent for subsequent actions in this session.

    Call this before a sequence of edits so trajectory steps are conditioned on intent.
    Levels: 1=atomic, 2=musical op, 3=section, 4=song, 5=creative goal.

    Requires telemetry consent. Data is stored in Supabase (same project as telemetry).

    Parameters:
    - text: Natural-language intent (e.g. "make the chorus feel bigger")
    - level: Hierarchical intent level 1–5 (default 5)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        event = recorder.set_intent(text=text, level=level)
        return (
            f"Recorded intent {event.intent_id} (level {level}): {text!r}. "
            f"Session: {recorder.session_id}"
        )
    except Exception as e:
        logger.error(f"Error submitting intent: {str(e)}")
        return f"Error submitting intent: {str(e)}"


@mcp.tool()
@telemetry_tool("rate_last_action")
@trajectory_tool("rate_last_action", modifying=False)
def rate_last_action(
    ctx: Context,
    rating: str,
    tags: str = "",
    note: str = "",
    user_prompt: str = "",
) -> str:
    """
    Rate the most recent recorded action (or the track after an edit).

    Ratings: better | same | worse | keep | reject | thumbs_up | thumbs_down
    Optional tags (comma-separated): groove, harmony, melody, sound, arrangement,
    energy, mix, emotion

    Requires telemetry consent.

    Parameters:
    - rating: Preference label
    - tags: Comma-separated aspect tags
    - note: Optional free-text reason
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        event = recorder.record_preference(
            rating=rating.strip().lower(),
            tags=tag_list,
            note=note or None,
        )
        target = event.target_action_id or "none"
        return f"Recorded preference {rating!r} for action {target}"
    except Exception as e:
        logger.error(f"Error rating action: {str(e)}")
        return f"Error rating action: {str(e)}"


@mcp.tool()
@telemetry_tool("prefer_candidate")
@trajectory_tool("prefer_candidate", modifying=False)
def prefer_candidate(
    ctx: Context,
    candidate_a: str,
    candidate_b: str,
    winner: str,
    reason: str = "",
    user_prompt: str = "",
) -> str:
    """
    Record a pairwise preference between two candidate actions or auditions.

    winner should be 'a', 'b', or an explicit candidate id matching A or B.

    Requires telemetry consent.

    Parameters:
    - candidate_a: Id / URI / label for option A
    - candidate_b: Id / URI / label for option B
    - winner: 'a', 'b', or the winning id
    - reason: Optional reason ("C has the right attack")
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        w = winner.strip().lower()
        if w == "a":
            w = candidate_a
        elif w == "b":
            w = candidate_b
        event = recorder.record_preference(
            rating="pairwise",
            winner=w,
            candidate_a=candidate_a,
            candidate_b=candidate_b,
            note=reason or None,
        )
        return f"Recorded pairwise preference: winner={w!r} (event {event.event_id})"
    except Exception as e:
        logger.error(f"Error recording pairwise preference: {str(e)}")
        return f"Error recording pairwise preference: {str(e)}"


@mcp.tool()
@telemetry_tool("reject_last_action")
@trajectory_tool("reject_last_action", modifying=False)
def reject_last_action(
    ctx: Context,
    reason: str = "",
    user_prompt: str = "",
) -> str:
    """
    Mark the last action as rejected (exploration / preference boundary).

    Use when the human undoes or discards an agent edit. Requires telemetry consent.

    Parameters:
    - reason: Optional why it was rejected
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        event = recorder.record_preference(
            rating="reject",
            note=reason or None,
        )
        return f"Recorded rejection for action {event.target_action_id or 'none'}"
    except Exception as e:
        logger.error(f"Error rejecting action: {str(e)}")
        return f"Error rejecting action: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("record_audition")
@trajectory_tool("record_audition", modifying=False)
def record_audition(
    ctx: Context,
    uri: str,
    kept: bool = False,
    search_query: str = "",
    dwell_ms: float = 0.0,
    user_prompt: str = "",
) -> str:
    """
    Log a browser/preset/sample audition (keep or reject) for preference learning.

    Call once per candidate auditioned. Does not load the device — use
    load_instrument_or_effect when kept=True and you want to commit.

    Requires telemetry consent.

    Parameters:
    - uri: Browser item URI (or stable preset/sample id)
    - kept: Whether this candidate was kept
    - search_query: Optional search text that led here (e.g. "analog bass")
    - dwell_ms: Optional time spent auditioning
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        event = recorder.record_audition(
            uri=uri,
            kept=kept,
            search_query=search_query or None,
            dwell_ms=dwell_ms if dwell_ms > 0 else None,
        )
        status = "kept" if kept else "rejected"
        return f"Recorded audition ({status}) uri={uri!r} event={event.event_id}"
    except Exception as e:
        logger.error(f"Error recording audition: {str(e)}")
        return f"Error recording audition: {str(e)}"



async def run_server(transport: str) -> None:
    # Stateless HTTP opens a protocol session per request. Keep one Live connection per process.
    async with server_lifespan():
        if transport == "streamable-http":
            await mcp.run_streamable_http_async()
        else:
            await mcp.run_stdio_async()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Ableton Live MCP server")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--port", type=int, default=8765, help="Loopback HTTP port (default: 8765)")
    args = parser.parse_args(argv)

    if args.transport == "streamable-http":
        if not 1 <= args.port <= 65535:
            parser.error("--port must be between 1 and 65535")
        mcp.settings.port = args.port

        # A remote chat should not change machine-wide consent or import a local file by path.
        os.environ["ABLETON_MCP_DISABLE_TELEMETRY"] = "1"
        os.environ["ABLETON_MCP_DISABLE_DATASET"] = "1"
        for tool in (
            "create_audio_clip", "set_dataset_consent", "submit_intent", "rate_last_action",
            "prefer_candidate", "reject_last_action", "record_audition",
        ):
            mcp.remove_tool(tool)

    try:
        asyncio.run(run_server(args.transport))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
