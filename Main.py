import serial
import time
import os
import json
import asyncio
import websockets
from typing import Dict, List, Tuple, Optional
from pathlib import Path
import threading
from queue import Queue

class CTSGen7Reader:
    """
    Optimized CTS Gen7 data reader with WebSocket integration and finish time extraction.
    """
    
    # Constants
    SPACE_ASCII = 0x20
    BLANK_CHAR = " "
    CHANNELS = 32
    CHARS_PER_CHANNEL = 8
    CONTROL_BYTE_THRESHOLD = 0x7F
    CLEAR_CHANNEL_THRESHOLD = 190
    
    # Channel mappings
    EVENT_HEAT_CHANNEL = 0x0C
    RACE_TIME_CHANNEL = 0x00
    LANE_CHANNELS = range(0x01, 0x07)  # Channels 1-6 for lanes 1-6
    
    def __init__(self, port: str = "COM9", baud: int = 9600, 
                 event_files_path: str = r"C:\Users\commu\OneDrive\Documents\Otters\Swim Live\Session 01",
                 websocket_port: int = 8001):
        self.port = port
        self.baud = baud
        self.parity = serial.PARITY_EVEN
        self.event_files_path = Path(event_files_path)
        self.websocket_port = websocket_port
        
        # Initialize display buffer (32 channels, 8 chars each)
        self.display = [[self.SPACE_ASCII] * self.CHARS_PER_CHANNEL for _ in range(self.CHANNELS)]
        
        # Stream state
        self.stream_state = {
            "data_readout": False,
            "channel": 0
        }
        
        # Cache for event names to avoid repeated file reads
        self.event_cache: Dict[str, str] = {}
        
        # Cache for swimmer data to avoid repeated file reads
        self.swimmer_cache: Dict[str, List[List[str]]] = {}
        
        # State tracking for change detection
        self.last_event = ""
        self.last_heat = ""
        self.last_event_name = ""
        self.last_race_time = ""
        self.last_swimmers = []
        
        # Finish time tracking
        self.last_finish_times = {}  # Track last known finish times per lane
        
        # Timer state tracking
        self.timer_running = False
        self.timer_start_time = None
        self.timer_offset = 0.0  # For handling timer that doesn't start at 0
        
        # WebSocket components
        self.websocket_clients = set()
        self.data_queue = Queue()
        self.running = False
        
        # Pre-compile frequently used operations
        self._setup_serial()
        
    def _setup_serial(self) -> None:
        """Initialize serial connection with optimal settings."""
        self.serial = serial.Serial(
            port=self.port,
            baudrate=self.baud,
            bytesize=serial.EIGHTBITS,
            parity=self.parity,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.01,  # Non-blocking reads
            rtscts=False,
            dsrdtr=False
        )
        
    def _read_event_file(self, event_id: str) -> List[str]:
        """
        Common function to read and parse event files.
        Returns list of lines with whitespace stripped.
        """
        if not event_id or event_id == 'N/A':
            return []
            
        # Construct file path
        filename = f"E{event_id}.scb"
        filepath = self.event_files_path / filename
        
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            # Remove newlines and strip whitespace
            return [line.rstrip() for line in lines]
            
        except (FileNotFoundError, IOError):
            return []
    
    def _clean_event_name(self, raw_name: str) -> str:
        """
        Extract first word and last two words from event name.
        Optimized for common swimming event formats.
        """
        words = [w for w in raw_name.split() if w.strip()]
        
        if len(words) <= 3:
            return ' '.join(words)
            
        return f"{words[0]} {words[-2]} {words[-1]}"
    
    def _get_event_name(self, event_id: str) -> str:
        """
        Efficiently retrieve and cache event names from files.
        """
        if not event_id or event_id == 'N/A':
            return event_id
            
        # Check cache first
        if event_id in self.event_cache:
            return self.event_cache[event_id]
            
        # Read file using common function
        lines = self._read_event_file(event_id)
        
        if not lines:
            self.event_cache[event_id] = event_id
            return event_id
            
        event_name = lines[0].strip()
        
        if not event_name:
            self.event_cache[event_id] = event_id
            return event_id
            
        # Clean event name efficiently
        cleaned_name = event_name.lstrip('#').rstrip()
        
        # Remove "heats" suffix (case insensitive)
        if cleaned_name.lower().endswith(' heats'):
            cleaned_name = cleaned_name[:-6].strip()
        elif cleaned_name.lower().endswith('heats'):
            cleaned_name = cleaned_name[:-5].strip()
            
        # Extract key words
        cleaned_name = self._clean_event_name(cleaned_name)
        
        # Cache and return
        self.event_cache[event_id] = cleaned_name
        return cleaned_name
    
    def _parse_swimmer_data(self, event_id: str) -> List[List[str]]:
        """
        Parse swimmer data from event file and return list of heats with swimmer info.
        Each heat is a list of 8 lanes (empty lanes as empty strings).
        Uses formula: Heat N starts at line (10*N - 9), ends at line (10*N - 9 + 8)
        Line numbers are 0-based (Python indexing).
        """
        if not event_id or event_id == 'N/A':
            return []
            
        # Check cache first
        if event_id in self.swimmer_cache:
            return self.swimmer_cache[event_id]
            
        # Read file using common function
        lines = self._read_event_file(event_id)
        
        if len(lines) < 10:  # Need at least 10 lines for heat 1
            self.swimmer_cache[event_id] = []
            return []
        
        heats = []
        
        # Process heats using the formula: start = 10*N - 9, end = start + 8
        heat_num = 1
        while True:
            start_line = 10 * heat_num - 9  # Formula for start line (0-based)
            end_line = start_line + 8       # 8 lanes per heat
            
            # Check if we have enough lines
            if start_line >= len(lines):
                break
                
            # Extract this heat's data
            heat_swimmers = []
            for i in range(start_line, min(end_line, len(lines))):
                line = lines[i].strip()
                if line == "--":
                    heat_swimmers.append("")  # Empty lane
                else:
                    heat_swimmers.append(line)  # Swimmer data
            
            # Pad to exactly 8 lanes if needed
            while len(heat_swimmers) < 8:
                heat_swimmers.append("")
                
            # Only add heats that have at least one swimmer
            if any(swimmer.strip() for swimmer in heat_swimmers):
                heats.append(heat_swimmers[:8])  # Ensure exactly 8 lanes
                heat_num += 1
            else:
                # If heat is completely empty, we've reached the end
                break
        
        # Cache and return
        self.swimmer_cache[event_id] = heats
        return heats
    
    def _format_swimmer_data(self, raw_data: str) -> Dict[str, str]:
        """
        Format swimmer data from 'SURNAME,Forename          --CLUBCODE'
        into { "name": "Forename Surname", "club": "CLUBCODE" }
        """
        if not raw_data or raw_data == "--":
            return {"name": "", "club": ""}

        try:
            parts = raw_data.split('--')
            if len(parts) != 2:
                return {"name": "", "club": ""}

            name_part = parts[0].strip()
            club_code = parts[1].strip()

            if ',' in name_part:
                surname, forename = name_part.split(',', 1)
                surname = surname.strip()
                forename = forename.strip()
                return {"name": f"{forename} {surname}", "club": club_code}
            else:
                return {"name": name_part, "club": club_code}

        except Exception:
            return {"name": "", "club": ""}
    
    def _get_swimmers_for_heat(self, event_id: str, heat_num: str) -> Dict[str, Dict[str, str]]:
        """
        Return dict of lanes → { name, club }
        """
        if not event_id or not heat_num or heat_num == 'N/A':
            return {str(i): {"name": "", "club": ""} for i in range(1, 9)}

        try:
            heat_number = int(heat_num)
            if heat_number < 1:
                return {str(i): {"name": "", "club": ""} for i in range(1, 9)}
        except ValueError:
            return {str(i): {"name": "", "club": ""} for i in range(1, 9)}

        all_heats = self._parse_swimmer_data(event_id)
        if not all_heats or heat_number > len(all_heats):
            return {str(i): {"name": "", "club": ""} for i in range(1, 9)}

        heat_swimmers = all_heats[heat_number - 1]

        lanes = {}
        for i, swimmer_data in enumerate(heat_swimmers, 1):
            lanes[str(i)] = self._format_swimmer_data(swimmer_data)

        return lanes
    
    def _process_byte(self, byte_in: int) -> None:
        """
        Process incoming byte with optimized logic.
        """
        if byte_in > self.CONTROL_BYTE_THRESHOLD:  # Control byte
            self.stream_state["data_readout"] = (byte_in & 1) == 0
            self.stream_state["channel"] = ((byte_in >> 1) & 0x1F) ^ 0x1F
            
            channel = self.stream_state["channel"]
            if channel >= self.CHANNELS:
                return
                
            # Clear channel if needed
            if byte_in > self.CLEAR_CHANNEL_THRESHOLD:
                self.display[channel] = [self.SPACE_ASCII] * self.CHARS_PER_CHANNEL
                
        else:  # Data byte
            if not self.stream_state["data_readout"]:
                return
                
            segment_num = (byte_in & 0xF0) >> 4
            if segment_num >= self.CHARS_PER_CHANNEL:
                return
                
            segment_data = byte_in & 0x0F
            channel = self.stream_state["channel"]
            
            if channel > 0 and segment_data == 0:
                self.display[channel][segment_num] = self.SPACE_ASCII
            else:
                self.display[channel][segment_num] = (segment_data ^ 0x0F) + 48
    
    def _get_char(self, channel: int, offset: int) -> str:
        """Get character from display buffer with bounds checking."""
        if channel >= self.CHANNELS or offset >= self.CHARS_PER_CHANNEL:
            return self.BLANK_CHAR
            
        ch = self.display[channel][offset]
        return self.BLANK_CHAR if ch in (self.SPACE_ASCII, 63) else chr(ch)
    
    def _get_event_and_heat(self) -> Tuple[str, str]:
        """Extract event and heat information from channel 0x0C."""
        channel = self.EVENT_HEAT_CHANNEL
        event = ''.join(self._get_char(channel, i) for i in range(3)).strip()
        heat = ''.join(self._get_char(channel, i) for i in range(5, 8)).strip()
        return event, heat
    
    def _get_race_time(self) -> str:
        """Extract race time from channel 0x00."""
        channel = self.RACE_TIME_CHANNEL
        time_str = (
            f"{self._get_char(channel, 2)}{self._get_char(channel, 3)}:"
            f"{self._get_char(channel, 4)}{self._get_char(channel, 5)}."
            f"{self._get_char(channel, 6)}{self._get_char(channel, 7)}"
        )
        return time_str.strip()
    
    def _get_lane_data(self, lane_channel: int) -> Optional[Dict[str, any]]:
        """
        Extract lane data from a channel.
        Returns dict with lane, place, and time, or None if no data.
        
        Lane format: [Lane#][Place][MM][MM][SS][SS][HH][HH]
        Where:
          - Position 0: Lane number
          - Position 1: Place (1st, 2nd, etc.)
          - Position 2-3: Minutes
          - Position 4-5: Seconds
          - Position 6-7: Hundredths of a second
        """
        # Get all 8 characters from this lane channel
        lane_num = self._get_char(lane_channel, 0).strip()
        place = self._get_char(lane_channel, 1).strip()
        min_tens = self._get_char(lane_channel, 2).strip()
        min_ones = self._get_char(lane_channel, 3).strip()
        sec_tens = self._get_char(lane_channel, 4).strip()
        sec_ones = self._get_char(lane_channel, 5).strip()
        hun_tens = self._get_char(lane_channel, 6).strip()
        hun_ones = self._get_char(lane_channel, 7).strip()
        
        # Check if we have valid time data (place must be present for finish time)
        if not place or not sec_tens or not sec_ones:
            return None
        
        # Build the time string
        minutes = f"{min_tens if min_tens else '0'}{min_ones if min_ones else '0'}"
        seconds = f"{sec_tens if sec_tens else '0'}{sec_ones if sec_ones else '0'}"
        hundredths = f"{hun_tens if hun_tens else '0'}{hun_ones if hun_ones else '0'}"
        
        time_str = f"{minutes}:{seconds}.{hundredths}"
        
        # Calculate total seconds
        try:
            total_seconds = (int(minutes) * 60 + 
                           int(seconds) + 
                           int(hundredths) / 100.0)
        except ValueError:
            return None
        
        return {
            "lane": lane_num if lane_num else str(lane_channel),
            "place": place,
            "time": time_str,
            "totalSeconds": total_seconds
        }
    
    def _get_all_finish_times(self) -> List[Dict[str, any]]:
        """
        Get finish times for all lanes with data.
        Returns list sorted by place.
        """
        results = []
        
        for channel in self.LANE_CHANNELS:
            lane_data = self._get_lane_data(channel)
            if lane_data:
                results.append(lane_data)
        
        # Sort by place (handle empty places)
        results.sort(key=lambda x: int(x['place']) if x['place'].isdigit() else 999)
        
        return results
    
    def _print_finish_times(self, finish_times: List[Dict[str, any]]) -> None:
        """Print finish times to console in a formatted table."""
        if not finish_times:
            return
        
        print("\n" + "="*50)
        print("FINISH TIMES")
        print("="*50)
        print(f"{'Place':<8} {'Lane':<8} {'Time':<12}")
        print("-"*50)
        
        for result in finish_times:
            place = result['place'] if result['place'] else '-'
            lane = result['lane']
            time = result['time']
            print(f"{place:<8} {lane:<8} {time:<12}")
        
        print("="*50 + "\n")
    
    def _parse_time_to_seconds(self, time_str: str) -> Optional[float]:
        """Convert time string MM:SS.HH to seconds (float)."""
        try:
            # Clean the input - strip spaces first
            clean_time = time_str.strip()
            
            if ":" in clean_time and "." in clean_time:
                # Format: MM:SS.HH or M:SS.H or :SS.H
                parts = clean_time.split(":")
                
                # Handle minutes (may be empty string)
                minutes_str = parts[0].strip()
                minutes = int(minutes_str) if minutes_str else 0
                
                sec_parts = parts[1].split(".")
                seconds = int(sec_parts[0].strip())
                
                # Handle tenths/hundredths
                frac_str = sec_parts[1].strip()
                if len(frac_str) == 1:
                    # Single digit - tenths
                    tenths = int(frac_str)
                    total_seconds = minutes * 60 + seconds + tenths / 10.0
                else:
                    # Two digits - hundredths
                    hundredths = int(frac_str[:2])
                    total_seconds = minutes * 60 + seconds + hundredths / 100.0
                
                return total_seconds
                
            elif "." in clean_time and ":" not in clean_time:
                # Format: SS.H or SS.HH (no minutes)
                parts = clean_time.split(".")
                seconds = int(parts[0].strip())
                
                # Handle tenths/hundredths
                frac_str = parts[1].strip()
                if len(frac_str) == 1:
                    tenths = int(frac_str)
                    total_seconds = seconds + tenths / 10.0
                else:
                    hundredths = int(frac_str[:2])
                    total_seconds = seconds + hundredths / 100.0
                
                return total_seconds
                
        except (ValueError, IndexError) as e:
            pass
        
        return None
    
    def _send_websocket_data(self, data: Dict) -> None:
        """Queue data to be sent to WebSocket clients."""
        if self.websocket_clients:
            self.data_queue.put(data)
    
    def _handle_event_change(self, event: str, heat: str) -> None:
        """Handle event/heat changes and send updates."""
        data_to_send = {}
        
        # Update event name only when event changes
        if event != self.last_event:
            event_name = self._get_event_name(event)
            self.last_event_name = event_name
            data_to_send["eventName"] = event_name.upper()

            # Always update heat name when event changes
            display_heat = f"HEAT {heat}" if heat else 'N/A'
            data_to_send["heatName"] = display_heat
    
            # Also update swimmers when event changes
            swimmers = self._get_swimmers_for_heat(event, heat)
            self.last_swimmers = swimmers
            data_to_send["lanes"] = swimmers
            
            # Clear finish times when event changes
            self.last_finish_times = {}
            
        # Update heat and swimmers only when heat changes
        elif heat != self.last_heat:
            display_heat = f"HEAT {heat}" if heat else 'N/A'
            data_to_send["heatName"] = display_heat
            
            # Get swimmer information for the new heat
            swimmers = self._get_swimmers_for_heat(event, heat)
            self.last_swimmers = swimmers
            data_to_send["lanes"] = swimmers
            
            # Clear finish times when heat changes
            self.last_finish_times = {}
        
        # Always include eventHidden when event/heat changes
        data_to_send["eventHidden"] = False
        
        # Send WebSocket update if there are changes
        if data_to_send:
            self._send_websocket_data(data_to_send)
        
        self.last_event, self.last_heat = event, heat
    
    def _handle_time_update(self, race_time: str) -> None:
        """Handle race time updates and send sync data to WebSocket."""
        # Parse the time to seconds
        current_seconds = self._parse_time_to_seconds(race_time)
        
        # Check if timer shows all spaces or zeros (not running)
        cleaned = race_time.replace(" ", "").replace(":", "").replace(".", "")
        is_empty = cleaned == "" or not any(c.isdigit() and c != '0' for c in cleaned)
        
        
        if is_empty or current_seconds is None or current_seconds == 0:
            # Timer is stopped/reset
            if self.timer_running:
                # Timer just stopped
                self.timer_running = False
                data = {
                    "timerSync": {
                        "running": False,
                        "time": 0.0,
                        "timestamp": time.time()
                    }
                }
                self._send_websocket_data(data)
        else:
            # Timer has a non-zero value - it's running
            if not self.timer_running:
                # Timer just started
                self.timer_running = True
                self.timer_start_time = time.time()
                self.timer_offset = current_seconds
                
                data = {
                    "timerSync": {
                        "running": True,
                        "time": current_seconds,
                        "timestamp": time.time()
                    }
                }
                self._send_websocket_data(data)
            else:
                # Timer is running - send periodic sync to prevent drift
                # Only sync every ~0.5 seconds to minimize traffic
                if not hasattr(self, '_last_sync_time'):
                    self._last_sync_time = 0
                
                if time.time() - self._last_sync_time >= 0.5:
                    data = {
                        "timerSync": {
                            "running": True,
                            "time": current_seconds,
                            "timestamp": time.time()
                        }
                    }
                    self._send_websocket_data(data)
                    self._last_sync_time = time.time()
        
        self.last_race_time = race_time
    
    def _handle_finish_times(self) -> None:
        """Check for finish times and print when they change."""
        finish_times = self._get_all_finish_times()
        
        if not finish_times:
            return
        
        # Convert to comparable format (dict by lane)
        current_times = {ft['lane']: ft for ft in finish_times}
        
        # Check if finish times have changed
        if current_times != self.last_finish_times:
            # Print the finish times
            self._print_finish_times(finish_times)
            
            # Update last known state
            self.last_finish_times = current_times
    
    async def _websocket_handler(self, websocket):
        """Handle individual WebSocket connections."""
        self.websocket_clients.add(websocket)
        
        try:
            # Send initial state
            initial_data = {
                "eventName": (self.last_event_name or 'N/A').upper(),
                "heatName": f"HEAT {self.last_heat}" if self.last_heat else 'N/A',
                "eventHidden": False,
                "lanes": self.last_swimmers if self.last_swimmers else {
                    str(i): {"name": "", "club": ""} for i in range(1, 9)
                },
                "timerSync": {
                    "running": self.timer_running,
                    "time": self._parse_time_to_seconds(self.last_race_time) or 0.0,
                    "timestamp": time.time()
                }
            }
            await websocket.send(json.dumps(initial_data))
            
            # Keep connection alive
            await websocket.wait_closed()
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.websocket_clients.discard(websocket)
    
    async def _websocket_broadcaster(self):
        """Broadcast data to all WebSocket clients."""
        while self.running:
            try:
                # Check for data to broadcast
                if not self.data_queue.empty():
                    data = self.data_queue.get()
                    message = json.dumps(data)
                    
                    # Send to all connected clients
                    if self.websocket_clients:
                        disconnected_clients = set()
                        for client in self.websocket_clients:
                            try:
                                await client.send(message)
                            except websockets.exceptions.ConnectionClosed:
                                disconnected_clients.add(client)
                        
                        # Remove disconnected clients
                        self.websocket_clients -= disconnected_clients
                
                await asyncio.sleep(0.01)  # Small delay to prevent CPU spinning
                
            except Exception as e:
                await asyncio.sleep(0.1)
    
    def _run_websocket_server(self):
        """Run the WebSocket server in a separate thread."""
        async def start_server():
            async def handler(websocket):
                await self._websocket_handler(websocket)
            
            server = await websockets.serve(
                handler,
                "localhost",
                self.websocket_port
            )
            
            # Start the broadcaster
            broadcaster_task = asyncio.create_task(self._websocket_broadcaster())
            
            try:
                await server.wait_closed()
            finally:
                broadcaster_task.cancel()
                try:
                    await broadcaster_task
                except asyncio.CancelledError:
                    pass
        
        # Run the async server
        asyncio.run(start_server())
    
    def run(self) -> None:
        """Main execution loop with WebSocket integration."""
        self.running = True
        
        # Start WebSocket server in separate thread
        websocket_thread = threading.Thread(target=self._run_websocket_server, daemon=True)
        websocket_thread.start()
        
        print("Listening for CTS Gen7 data with WebSocket broadcasting... (Ctrl+C to stop)")
        print(f"Event files directory: {self.event_files_path.resolve()}")
        print(f"WebSocket server: ws://localhost:{self.websocket_port}")
        print("Timer optimized with client-side interpolation")
        print("Monitoring for finish times...\n")
        
        try:
            buffer = bytearray()
            
            # Initial data update
            initial_event, initial_heat = self._get_event_and_heat()
            initial_race_time = self._get_race_time()
            
            if initial_event or initial_heat:
                self._handle_event_change(initial_event, initial_heat)
                
                if initial_race_time:
                    self._handle_time_update(initial_race_time)
                
                print("--------------------\n")
            
            while True:
                # Read data in larger chunks
                data = self.serial.read(256)
                if data:
                    buffer.extend(data)
                    
                    for byte_val in buffer:
                        self._process_byte(byte_val)
                    buffer.clear()
                
                # Check for changes
                event, heat = self._get_event_and_heat()
                race_time = self._get_race_time()
                
                # Handle event/heat changes
                if event != self.last_event or heat != self.last_heat:
                    self._handle_event_change(event, heat)
                
                # Handle time updates
                if race_time != self.last_race_time:
                    self._handle_time_update(race_time)
                
                # Check for finish times
                self._handle_finish_times()
                    
        except KeyboardInterrupt:
            print("\nStopping...")
        finally:
            self.close()
    
    def close(self) -> None:
        """Clean up resources."""
        self.running = False
        
        if hasattr(self, 'serial') and self.serial.is_open:
            self.serial.close()
            
        print("CTS Gen7 Reader stopped.")


def main():
    """Main entry point with configurable parameters."""
    CONFIG = {
        'port': "COM9",
        'baud': 9600,
        'event_files_path': r"C:\Users\commu\OneDrive\Documents\Otters\Swim Live\Events",
        'websocket_port': 8001
    }
    
    reader = CTSGen7Reader(**CONFIG)
    reader.run()


if __name__ == "__main__":
    main()
