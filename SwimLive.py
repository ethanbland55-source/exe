import serial
import serial.tools.list_ports
import time
import json
import asyncio
import websockets
import threading
from typing import Dict, List, Tuple, Optional
from pathlib import Path
from queue import Queue
import tkinter as tk
from tkinter import ttk
import base64

class SwimLiveSystem:
    """
    Unified system combining CTS Gen7 reader, COM port file receiver, and HTML generation.
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
    LANE_CHANNELS = range(0x01, 0x08)

    
    # Network settings
    WEBSOCKET_PORT = 8001

    # Timer sync settings
    TIMER_SYNC_INTERVAL = 0.1  # Sync every 100ms for better accuracy
    TIMER_LATENCY_COMPENSATION = 0.25  # Compensate for ~250ms lag
    DQ_STALE_TIMEOUT = 5.0 # Ignore DQ flags in first 5 seconds of a race
    
    def __init__(self, cts_port: str, receiver_port: str, baud: int = 9600):
        # Base directory setup
        self.base_dir = Path.home() / "Documents" / "Swim Live"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        
        self.event_files_path = self.base_dir / "Events"
        self.event_files_path.mkdir(parents=True, exist_ok=True)
        
        self.html_dir = self.base_dir
        
        # Serial settings
        self.cts_port = cts_port
        self.receiver_port = receiver_port
        self.baud = baud
        self.parity = serial.PARITY_EVEN
        
        # Initialize display buffer
        self.display = [[self.SPACE_ASCII] * self.CHARS_PER_CHANNEL for _ in range(self.CHANNELS)]
        
        # Stream state
        self.stream_state = {"data_readout": False, "channel": 0}
        
        # Caches
        self.event_cache: Dict[str, str] = {}
        self.swimmer_cache: Dict[str, List[List[str]]] = {}
        
        # State tracking
        self.last_event = ""
        self.last_heat = ""
        self.last_event_name = ""
        self.last_race_time = ""
        self.last_swimmers = {}
        self.last_finish_times = {}
        self.lane_time_counts = {str(i): 0 for i in range(1, 9)}  # Track how many times received per lane
        self.expected_times_per_lane = 1  # Default to 1 (finish only)
        self.active_lanes = set()  # Set of active lane numbers
        self.last_active_lanes = set()  # Previous state for change detection
        self.last_lane_check_time = 0
        self.lane_check_interval = 0.5
        self._saved_sent_for_heat = False
        self._dq_sent_for_heat = set() # ADD THIS LINE to track DQ lanes per heat
        
        # Timer state
        self.timer_running = False
        self.timer_start_time = None
        self.timer_offset = 0.0
        self._last_sync_time = 0
        
        # WebSocket components
        self.websocket_clients = set()
        self.data_queue = Queue()
        self.running = False
        
        # COM receiver state
        self.com_buffers = {}
        self.com_line_buffer = ""
        
        # Setup serial connections
        self._setup_serial()
        
        # Generate HTML files
        self._generate_html_files()
        
    def _setup_serial(self) -> None:
        """Initialize serial connections."""
        # CTS Gen7 connection
        self.cts_serial = serial.Serial(
            port=self.cts_port,
            baudrate=self.baud,
            bytesize=serial.EIGHTBITS,
            parity=self.parity,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.001,  # Very short timeout for non-blocking
            rtscts=False,
            dsrdtr=False
        )
        
        # File receiver connection
        self.receiver_serial = serial.Serial(
            port=self.receiver_port,
            baudrate=115200,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.001,  # Very short timeout for non-blocking
            rtscts=False,
            dsrdtr=False
        )
    
    def _generate_html_files(self) -> None:
        """Generate EventTimer.html and LaneStarts.html in the base directory."""
        
        # EventTimer.html
        event_timer_html = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Event Timer</title>
    <style>
        body {
            margin: 0;
            font-family: Helvetica, serif;
            background-color: transparent;
            color: white;
        }
        .boxes-container {
            display: flex;
            flex-direction: column;
            gap: 0;
            width: 550px;
            margin: 50px auto;
            overflow: hidden;
        }
        .box {
            background-color: #42008f;
            padding: 8px 15px;
            box-shadow: 0 4px 8px rgba(0, 0, 0, 0.2);
            text-align: center;
            white-space: nowrap;
            overflow: hidden;
            max-width: 0;
            opacity: 0;
            transition: max-width 0.8s cubic-bezier(0.45, 0, 0.25, 1), opacity 0.8s ease;
        }
        .box.show {
            max-width: 600px;
            opacity: 1;
        }
        #timer-box {
            display: flex;
            align-items: center;
            justify-content: flex-end;
            margin-left: auto;
            background-color: #FFD700;
        }
        #event-box {
            display: flex;
            align-items: center;
            justify-content: flex-end;
            margin-left: auto;
            background-color: #42008f;
        }
        #heat-box {
            display: flex;
            align-items: center;
            justify-content: flex-end;
            margin-left: auto;
            background-color: #1a1a1a;
        }
        #heat-box.show {
            opacity: 0.9;
        }
        .timer {
            font-size: 1.4rem;
            font-weight: bold;
            width: 10.3ch;
            text-align: center;
        }
        .event {
            font-size: 1.4rem;
            font-weight: bold;
            line-height: 1.2;
            white-space: nowrap;
        }
        .heat {
            font-size: 1.4rem;
            font-weight: bold;
            line-height: 1.2;
            white-space: nowrap;
        }
    </style>
</head>
<body>
<div class="boxes-container">
    <div class="box" id="timer-box">
        <div class="timer" id="timer">00:00.0</div>
    </div>
    <div class="box" id="event-box">
        <div class="event" id="event-name">OPEN/MALE 100M BACKSTROKE</div>
    </div>
    <div class="box" id="heat-box">
        <div class="heat" id="heat-name">HEAT 12</div>
    </div>
</div>
<script>
    const timerElement = document.getElementById('timer');
    const eventNameElement = document.getElementById('event-name');
    const heatNameElement = document.getElementById('heat-name');
    const timerBox = document.getElementById('timer-box');
    const eventBox = document.getElementById('event-box');
    const heatBox = document.getElementById('heat-box');
    let ws;
    let isVisible = false;
    let timerRunning = false;
    let timerStartTime = 0;
    let timerOffset = 0;
    let animationFrameId = null;
    let raceFinished = false; // Flag to prevent timer restart after first place finish

    function formatTime(seconds) {
        if (seconds <= 0) return "00:00.0";
        const minutes = Math.floor(seconds / 60);
        const secs = Math.floor(seconds % 60);
        const tenths = Math.floor((seconds % 1) * 10);
        return `${minutes.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}.${tenths}`;
    }

    function updateTimer() {
        if (!timerRunning) return;
        const now = performance.now();
        const elapsed = (now - timerStartTime) / 1000;
        const currentTime = timerOffset + elapsed;
        timerElement.textContent = formatTime(currentTime);
        updateVisibility(true);
        animationFrameId = requestAnimationFrame(updateTimer);
    }

    function startTimer(initialTime, timestamp) {
        timerRunning = true;
        timerStartTime = performance.now();
        timerOffset = initialTime;
        if (animationFrameId) cancelAnimationFrame(animationFrameId);
        updateTimer();
    }

    function stopTimer() {
        timerRunning = false;
        if (animationFrameId) {
            cancelAnimationFrame(animationFrameId);
            animationFrameId = null;
        }
        timerElement.textContent = "00:00.0";
        updateVisibility(false);
    }

    function syncTimer(timeData) {
        if (timeData.running) {
            // Don't restart timer if race has already finished
            if (raceFinished) {
                console.log('[Timer] Race finished - ignoring timer sync');
                return;
            }
            const networkDelay = (Date.now() / 1000) - timeData.timestamp;
            const adjustedTime = timeData.time + networkDelay;
            startTimer(adjustedTime, timeData.timestamp);
        } else {
            // Timer stopped on CTS - ignore this, we hide on first finish instead
            console.log('[Timer] CTS timer stopped - ignoring');
        }
    }

    function updateVisibility(running) {
        const boxes = [timerBox, eventBox, heatBox];
        if (running && !isVisible) {   
            boxes.forEach(box => box.classList.add('show')); 
            isVisible = true;
        } else if (!running && isVisible) {
            boxes.forEach(box => box.classList.remove('show')); 
            isVisible = false;
        }
    }

    function handleFinishTime(finishData) {
        // Check if this is a FINISH type (not SPLIT) with place 1
        if (finishData.type === "FINISH" && finishData.place === "1") {
            console.log('[Timer] First place finish detected - hiding timer');
            raceFinished = true; // Set flag to prevent restart
            stopTimer();
        }
    }

    function connectWebSocket() {
        ws = new WebSocket('ws://localhost:8001');
        ws.onmessage = (message) => {
            const data = JSON.parse(message.data);
            if (data.timerSync !== undefined) syncTimer(data.timerSync);
            if (data.eventName !== undefined) {
                eventNameElement.textContent = data.eventName;
                raceFinished = false; // Reset flag when new event starts
            }
            if (data.heatName !== undefined) {
                heatNameElement.textContent = data.heatName;
                raceFinished = false; // Reset flag when new heat starts
            }
            if (data.finishTime !== undefined) handleFinishTime(data.finishTime);
        };
        ws.onclose = () => setTimeout(connectWebSocket, 1000);
        ws.onerror = (error) => console.error('WebSocket error:', error);
    }

    connectWebSocket();
    window.addEventListener('beforeunload', () => {
        if (animationFrameId) cancelAnimationFrame(animationFrameId);
        if (ws) ws.close();
    });
</script>
</body>
</html>'''
        
        # LaneStarts.html - FIXED VERSION
        lane_starts_html = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Lane Starts - Cube Projection</title>
    <style>
        body {
            margin: 0;
            padding: 0;
            background-color: transparent;
            display: flex;
			flex-direction: column;
            justify-content: center;
            align-items: center;
            height: 100vh;
			font-family: Helvetica, serif;
			overflow: hidden;
			perspective: 1500px;
			perspective-origin: 50% 50%;
        }
		
		@keyframes expandLaneNumber {
			from {
				transform: scale(0);
				opacity: 0;
			}
			to {
				transform: scale(1);
				opacity: 0.9;
			}
		}

		@keyframes fadeInFromLeft {
			from {
				opacity: 0;
				clip-path: inset(0 100% 0 0);
			}
			to {
				opacity: 0.9;
				clip-path: inset(0 0 0 0);
			}
		}
		
		@keyframes fadeOut {
			from {
				opacity: 0.9;
			}
			to {
				opacity: 0;
			}
		}

		#laneWrapper {
			transform-style: preserve-3d;
			position: relative;
		}

		/* Each lane cube wrapper */
        .lane-cube {
			transform-style: preserve-3d;
			position: relative;
			opacity: 0;
			transition: transform 0.3s ease;
        }

		/* The actual lane container is the bottom face of the cube */
        .container {
            display: flex;
            gap: 8px;
			transform-style: preserve-3d;
        }

        .box {
            height: 112px;
            background-color: #42008f;
			opacity: 0.9;
			display: flex;
            justify-content: space-between;
            align-items: center;
            position: relative;
        }

        .LaneNumber {
            width: 88px;
			justify-content: center; 
			animation: expandLaneNumber 1s ease forwards;
			transform-origin: center;
			opacity: 0;
        }

        .SwimmerInfo {
            width: 762px;
			display: flex;
            justify-content: space-between;
            align-items: center;
			animation: fadeInFromLeft 1s ease forwards;
			opacity: 0;
        }
		
		.LaneNumber .content {
            font-size: 96px;
			font-weight: bold;
            color: #FFFF00;
            opacity: 0.95;
        }
		
		.SwimmerInfo .name {
            font-size: 52px;
            font-weight: bold;
            color: #FFFFFF;
            opacity: 0.95;
			margin-left: 16px;
			white-space: nowrap
        }

        .SwimmerInfo .club {
			font-size: 36px;
			font-weight: normal;
			color: #FFFFFF;
			text-transform: uppercase;
			white-space: nowrap;
			overflow: hidden;
			text-align: right;
			padding-right: 16px;
			display: block;
			width: calc(100% - 16px);
			box-sizing: border-box;
			transform-origin: right center;
		}
		
		.fade-out {
			animation: fadeOut 1s ease forwards;
		}

		/* Control panel */
		.control-panel {
			position: fixed;
			top: 20px;
			right: 20px;
			background: rgba(0, 0, 0, 0.95);
			padding: 25px;
			border-radius: 12px;
			color: white;
			font-family: Arial, sans-serif;
			font-size: 14px;
			z-index: 10000;
			width: 450px;
			max-height: 90vh;
			overflow-y: auto;
			box-shadow: 0 8px 32px rgba(0,0,0,0.8);
			border: 2px solid #00D9FF;
			display: none;
		}

		.control-panel.visible {
			display: block;
		}

		.control-panel h2 {
			margin: 0 0 20px 0;
			font-size: 24px;
			color: #00D9FF;
			text-align: center;
			border-bottom: 2px solid #00D9FF;
			padding-bottom: 12px;
		}

		.control-section {
			background: rgba(255,255,255,0.05);
			padding: 18px;
			border-radius: 8px;
			margin-bottom: 18px;
			border: 1px solid rgba(0, 217, 255, 0.3);
		}

		.control-section h3 {
			margin: 0 0 15px 0;
			font-size: 18px;
			color: #00D9FF;
		}

		.control-group {
			margin-bottom: 18px;
		}

		.control-group label {
			display: flex;
			justify-content: space-between;
			align-items: center;
			margin-bottom: 8px;
			font-size: 14px;
			color: #ddd;
			font-weight: bold;
		}

		.control-group input[type="range"] {
			width: 100%;
			height: 6px;
			margin-top: 5px;
		}

		.value-display {
			color: #00D9FF;
			font-weight: bold;
			font-size: 16px;
			min-width: 80px;
			text-align: right;
		}

		.button-group {
			display: flex;
			gap: 10px;
			margin-top: 15px;
		}

		.button-group button {
			flex: 1;
			padding: 12px;
			background: #00D9FF;
			border: none;
			border-radius: 6px;
			color: black;
			cursor: pointer;
			font-size: 14px;
			font-weight: bold;
			transition: all 0.2s;
		}

		.button-group button:hover {
			background: #00B8D4;
			transform: translateY(-2px);
			box-shadow: 0 4px 8px rgba(0, 217, 255, 0.4);
		}

		.button-group button.secondary {
			background: #666;
			color: white;
		}

		.button-group button.secondary:hover {
			background: #555;
		}

		.instructions {
			background: rgba(0, 217, 255, 0.15);
			border: 2px solid #00D9FF;
			padding: 12px;
			border-radius: 8px;
			font-size: 13px;
			line-height: 1.6;
			color: #ccc;
			margin-bottom: 18px;
		}

		.instructions strong {
			color: #00D9FF;
			display: block;
			margin-bottom: 6px;
			font-size: 14px;
		}

		.preset-buttons {
			display: grid;
			grid-template-columns: 1fr 1fr;
			gap: 8px;
			margin-top: 12px;
		}

		.preset-buttons button {
			padding: 10px;
			background: #9C27B0;
			border: none;
			border-radius: 6px;
			color: white;
			cursor: pointer;
			font-size: 13px;
			font-weight: bold;
			transition: all 0.2s;
		}

		.preset-buttons button:hover {
			background: #7B1FA2;
			transform: translateY(-1px);
		}

		.lane-select {
			display: grid;
			grid-template-columns: repeat(4, 1fr);
			gap: 8px;
			margin-bottom: 15px;
		}

		.lane-select button {
			padding: 10px;
			background: rgba(0, 217, 255, 0.2);
			border: 2px solid rgba(0, 217, 255, 0.3);
			border-radius: 6px;
			color: white;
			cursor: pointer;
			font-size: 14px;
			font-weight: bold;
			transition: all 0.2s;
		}

		.lane-select button.active {
			background: #00D9FF;
			color: black;
			border-color: #00D9FF;
		}

		.lane-select button:hover {
			background: rgba(0, 217, 255, 0.4);
		}

		.all-lanes-label {
			color: #00D9FF;
			font-weight: bold;
			margin-bottom: 10px;
			display: block;
		}
    </style>
</head>
<body>
	<div id="laneWrapper">
		<div class="lane-cube" id="cube1" data-lane="1">
			<div class="container" id="lane1">
				<div class="box LaneNumber">
					<div class="content">1</div>
				</div>
				<div class="box SwimmerInfo">
					<div class="name"></div>
					<div class="club"></div>
				</div>
			</div>
		</div>
		<div class="lane-cube" id="cube2" data-lane="2">
			<div class="container" id="lane2">
				<div class="box LaneNumber">
					<div class="content">2</div>
				</div>
				<div class="box SwimmerInfo">
					<div class="name"></div>
					<div class="club"></div>
				</div>
			</div>
		</div>
		<div class="lane-cube" id="cube3" data-lane="3">
			<div class="container" id="lane3">
				<div class="box LaneNumber">
					<div class="content">3</div>
				</div>
				<div class="box SwimmerInfo">
					<div class="name"></div>
					<div class="club"></div>
				</div>
			</div>
		</div>
		<div class="lane-cube" id="cube4" data-lane="4">
			<div class="container" id="lane4">
				<div class="box LaneNumber">
					<div class="content">4</div>
				</div>
				<div class="box SwimmerInfo">
					<div class="name"></div>
					<div class="club"></div>
				</div>
			</div>
		</div>
		<div class="lane-cube" id="cube5" data-lane="5">
			<div class="container" id="lane5">
				<div class="box LaneNumber">
					<div class="content">5</div>
				</div>
				<div class="box SwimmerInfo">
					<div class="name"></div>
					<div class="club"></div>
				</div>
			</div>
		</div>
		<div class="lane-cube" id="cube6" data-lane="6">
			<div class="container" id="lane6">
				<div class="box LaneNumber">
					<div class="content">6</div>
				</div>
				<div class="box SwimmerInfo">
					<div class="name"></div>
					<div class="club"></div>
				</div>
			</div>
		</div>
		<div class="lane-cube" id="cube7" data-lane="7">
			<div class="container" id="lane7">
				<div class="box LaneNumber">
					<div class="content">7</div>
				</div>
				<div class="box SwimmerInfo">
					<div class="name"></div>
					<div class="club"></div>
				</div>
			</div>
		</div>
		<div class="lane-cube" id="cube8" data-lane="8">
			<div class="container" id="lane8">
				<div class="box LaneNumber">
					<div class="content">8</div>
				</div>
				<div class="box SwimmerInfo">
					<div class="name"></div>
					<div class="club"></div>
				</div>
			</div>
		</div>
	</div>

	<div class="control-panel" id="controlPanel">
		<h2>🎲 Cube Projection System</h2>
		
		<div class="instructions">
			<strong>True IASAS Method:</strong>
			Each lane is the bottom face of an imaginary cube. Rotate the cube in 3D space to project the graphic onto the pool surface from your camera's viewpoint!
		</div>

		<div class="control-section">
			<h3>Camera Perspective</h3>
			
			<div class="control-group">
				<label>
					<span>Perspective Distance</span>
					<span class="value-display" id="perspectiveValue">1500px</span>
				</label>
				<input type="range" id="perspective" min="500" max="3000" step="50" value="1500">
			</div>

			<div class="control-group">
				<label>
					<span>View X Origin</span>
					<span class="value-display" id="perspectiveXValue">50%</span>
				</label>
				<input type="range" id="perspectiveX" min="0" max="100" step="1" value="50">
			</div>

			<div class="control-group">
				<label>
					<span>View Y Origin</span>
					<span class="value-display" id="perspectiveYValue">50%</span>
				</label>
				<input type="range" id="perspectiveY" min="0" max="100" step="1" value="50">
			</div>
		</div>

		<div class="control-section">
			<h3>Select Lane to Adjust</h3>
			<div class="lane-select">
				<button onclick="selectLane('all')" class="active" id="btnAll">ALL</button>
				<button onclick="selectLane(1)" id="btn1">L1</button>
				<button onclick="selectLane(2)" id="btn2">L2</button>
				<button onclick="selectLane(3)" id="btn3">L3</button>
				<button onclick="selectLane(4)" id="btn4">L4</button>
				<button onclick="selectLane(5)" id="btn5">L5</button>
				<button onclick="selectLane(6)" id="btn6">L6</button>
				<button onclick="selectLane(7)" id="btn7">L7</button>
				<button onclick="selectLane(8)" id="btn8">L8</button>
			</div>

			<span class="all-lanes-label" id="controlLabel">Adjusting: ALL LANES</span>

			<div class="control-group">
				<label>
					<span>Rotate X (Tilt)</span>
					<span class="value-display" id="rotateXValue">60°</span>
				</label>
				<input type="range" id="rotateX" min="0" max="90" step="0.5" value="60">
			</div>

			<div class="control-group">
				<label>
					<span>Rotate Y (Turn)</span>
					<span class="value-display" id="rotateYValue">0°</span>
				</label>
				<input type="range" id="rotateY" min="-45" max="45" step="0.5" value="0">
			</div>

			<div class="control-group">
				<label>
					<span>Rotate Z (Roll)</span>
					<span class="value-display" id="rotateZValue">0°</span>
				</label>
				<input type="range" id="rotateZ" min="-15" max="15" step="0.1" value="0">
			</div>

			<div class="control-group">
				<label>
					<span>Translate X (Left/Right)</span>
					<span class="value-display" id="translateXValue">0px</span>
				</label>
				<input type="range" id="translateX" min="-500" max="500" step="5" value="0">
			</div>

			<div class="control-group">
				<label>
					<span>Translate Y (Up/Down)</span>
					<span class="value-display" id="translateYValue">0px</span>
				</label>
				<input type="range" id="translateY" min="-500" max="500" step="5" value="0">
			</div>

			<div class="control-group">
				<label>
					<span>Translate Z (Depth)</span>
					<span class="value-display" id="translateZValue">0px</span>
				</label>
				<input type="range" id="translateZ" min="-800" max="400" step="10" value="0">
			</div>
		</div>

		<div class="control-section">
			<h3>Quick Presets</h3>
			<div class="preset-buttons">
				<button onclick="applyPreset('flat')">Flat View</button>
				<button onclick="applyPreset('broadcast')">Broadcast</button>
				<button onclick="applyPreset('olympic')">Olympic</button>
				<button onclick="applyPreset('overhead')">Overhead</button>
			</div>
		</div>

		<div class="button-group">
			<button onclick="resetSelected()" class="secondary">Reset Selected</button>
			<button onclick="copySettings()">Copy Values</button>
		</div>

		<div style="margin-top: 12px; padding: 12px; background: rgba(0, 217, 255, 0.2); border-radius: 8px; text-align: center; color: #00D9FF; font-size: 12px;">
			Each lane = bottom of cube • Rotate cube to project
		</div>
	</div>

	<script>
	// Club name scaling
	document.addEventListener("DOMContentLoaded", () => {
		const clubElements = document.querySelectorAll(".club");
		const targetText = "LBORO UNI";
		const measureElement = document.createElement("span");
		measureElement.style.visibility = "hidden";
		measureElement.style.position = "absolute";
		measureElement.style.fontSize = "36px";
		measureElement.style.fontWeight = "normal";
		measureElement.style.whiteSpace = "nowrap";
		measureElement.textContent = targetText;
		document.body.appendChild(measureElement);

		const targetWidth = measureElement.offsetWidth;
		document.body.removeChild(measureElement);

		clubElements.forEach(element => {
			const textWidth = getTextWidth(element.textContent, getComputedStyle(element));
			const scaleFactor = textWidth > targetWidth ? targetWidth / textWidth : 1;
			element.style.transform = `scaleX(${scaleFactor})`;
		});

		function getTextWidth(text, style) {
			const canvas = document.createElement("canvas");
			const context = canvas.getContext("2d");
			context.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
			return context.measureText(text).width;
		}
	});
	</script>
	
	<script>
	// Name font size adjustment
	document.addEventListener("DOMContentLoaded", () => {
		const laneContainers = document.querySelectorAll(".container");

		laneContainers.forEach(container => {
			const swimmerNameElement = container.querySelector(".name");

			if (!swimmerNameElement || swimmerNameElement.textContent.trim() === "") {
				container.parentElement.style.visibility = "hidden";
			} else {
				adjustFontSize(swimmerNameElement, 0.65);
			}
		});

		function adjustFontSize(element, maxWidthPercentage) {
			const parentWidth = element.parentElement.offsetWidth;
			const maxWidth = parentWidth * maxWidthPercentage;

			let fontSize = parseInt(window.getComputedStyle(element).fontSize, 10);

			while (element.offsetWidth > maxWidth && fontSize > 1) {
				fontSize -= 1;
				element.style.fontSize = `${fontSize}px`;
			}
		}
	});
	</script>
	
	<script>
	// Animation timing
	document.addEventListener("DOMContentLoaded", () => {
		const laneGroups = [
			[4, 5],
			[3, 6],
			[2, 7],
			[1, 8]  
		];
		const findGroupIndex = (number) => {
			return laneGroups.findIndex(group => group.includes(number));
		};
		const laneCubes = document.querySelectorAll(".lane-cube");

		laneCubes.forEach(cube => {
			const laneNumber = cube.querySelector(".LaneNumber");
			const swimmerInfo = cube.querySelector(".SwimmerInfo");

			setTimeout(() => {
				cube.style.opacity = 1;
			}, 100);

			const groupIndex = findGroupIndex(parseInt(cube.querySelector(".LaneNumber .content").textContent.trim(), 10));
			laneNumber.style.animationDelay = (groupIndex * 0.5).toString() + "s";
			swimmerInfo.style.animationDelay = (groupIndex * 0.5 + 0.75).toString() + "s";
		});
	});
	</script>
	
	<script>
		// Cube projection settings - each lane cube can be independently transformed
		const cubeSettings = {
			perspective: 1500,
			perspectiveX: 50,
			perspectiveY: 50,
			lanes: {
				1: { rotateX: 60, rotateY: 0, rotateZ: 0, translateX: 0, translateY: 0, translateZ: 0 },
				2: { rotateX: 60, rotateY: 0, rotateZ: 0, translateX: 0, translateY: 0, translateZ: 0 },
				3: { rotateX: 60, rotateY: 0, rotateZ: 0, translateX: 0, translateY: 0, translateZ: 0 },
				4: { rotateX: 60, rotateY: 0, rotateZ: 0, translateX: 0, translateY: 0, translateZ: 0 },
				5: { rotateX: 60, rotateY: 0, rotateZ: 0, translateX: 0, translateY: 0, translateZ: 0 },
				6: { rotateX: 60, rotateY: 0, rotateZ: 0, translateX: 0, translateY: 0, translateZ: 0 },
				7: { rotateX: 60, rotateY: 0, rotateZ: 0, translateX: 0, translateY: 0, translateZ: 0 },
				8: { rotateX: 60, rotateY: 0, rotateZ: 0, translateX: 0, translateY: 0, translateZ: 0 }
			}
		};

		let selectedLane = 'all';
		let isInteracting = false;
		let interactTimeout = null;

		document.addEventListener("DOMContentLoaded", () => {
			setupControls();
			applyAllCubeTransforms();
			detectInteractMode();
		});

		function detectInteractMode() {
			const panel = document.getElementById('controlPanel');

			document.addEventListener('mousemove', () => {
				if (!isInteracting) {
					isInteracting = true;
					panel.classList.add('visible');
				}
				
				clearTimeout(interactTimeout);
				interactTimeout = setTimeout(() => {
					isInteracting = false;
					panel.classList.remove('visible');
				}, 3000);
			});

			document.addEventListener('click', () => {
				isInteracting = true;
				panel.classList.add('visible');
				
				clearTimeout(interactTimeout);
				interactTimeout = setTimeout(() => {
					isInteracting = false;
					panel.classList.remove('visible');
				}, 5000);
			});
		}

		function selectLane(lane) {
			selectedLane = lane;
			
			// Update button states
			document.querySelectorAll('.lane-select button').forEach(btn => {
				btn.classList.remove('active');
			});
			if (lane === 'all') {
				document.getElementById('btnAll').classList.add('active');
				document.getElementById('controlLabel').textContent = 'Adjusting: ALL LANES';
			} else {
				document.getElementById('btn' + lane).classList.add('active');
				document.getElementById('controlLabel').textContent = 'Adjusting: LANE ' + lane;
			}
			
			// Update sliders to show current values
			updateSlidersForSelection();
		}

		function updateSlidersForSelection() {
			if (selectedLane === 'all') {
				// Show lane 1's values as reference
				const settings = cubeSettings.lanes[1];
				document.getElementById('rotateX').value = settings.rotateX;
				document.getElementById('rotateY').value = settings.rotateY;
				document.getElementById('rotateZ').value = settings.rotateZ;
				document.getElementById('translateX').value = settings.translateX;
				document.getElementById('translateY').value = settings.translateY;
				document.getElementById('translateZ').value = settings.translateZ;
			} else {
				const settings = cubeSettings.lanes[selectedLane];
				document.getElementById('rotateX').value = settings.rotateX;
				document.getElementById('rotateY').value = settings.rotateY;
				document.getElementById('rotateZ').value = settings.rotateZ;
				document.getElementById('translateX').value = settings.translateX;
				document.getElementById('translateY').value = settings.translateY;
				document.getElementById('translateZ').value = settings.translateZ;
			}
			updateValueDisplays();
		}

		function updateValueDisplays() {
			document.getElementById('perspectiveValue').textContent = cubeSettings.perspective + 'px';
			document.getElementById('perspectiveXValue').textContent = cubeSettings.perspectiveX + '%';
			document.getElementById('perspectiveYValue').textContent = cubeSettings.perspectiveY + '%';
			document.getElementById('rotateXValue').textContent = document.getElementById('rotateX').value + '°';
			document.getElementById('rotateYValue').textContent = document.getElementById('rotateY').value + '°';
			document.getElementById('rotateZValue').textContent = document.getElementById('rotateZ').value + '°';
			document.getElementById('translateXValue').textContent = document.getElementById('translateX').value + 'px';
			document.getElementById('translateYValue').textContent = document.getElementById('translateY').value + 'px';
			document.getElementById('translateZValue').textContent = document.getElementById('translateZ').value + 'px';
		}

		function setupControls() {
			// Perspective controls
			document.getElementById('perspective').addEventListener('input', (e) => {
				cubeSettings.perspective = parseFloat(e.target.value);
				document.body.style.perspective = cubeSettings.perspective + 'px';
				updateValueDisplays();
			});

			document.getElementById('perspectiveX').addEventListener('input', (e) => {
				cubeSettings.perspectiveX = parseFloat(e.target.value);
				document.body.style.perspectiveOrigin = `${cubeSettings.perspectiveX}% ${cubeSettings.perspectiveY}%`;
				updateValueDisplays();
			});

			document.getElementById('perspectiveY').addEventListener('input', (e) => {
				cubeSettings.perspectiveY = parseFloat(e.target.value);
				document.body.style.perspectiveOrigin = `${cubeSettings.perspectiveX}% ${cubeSettings.perspectiveY}%`;
				updateValueDisplays();
			});

			// Cube transform controls
			document.getElementById('rotateX').addEventListener('input', (e) => {
				const value = parseFloat(e.target.value);
				if (selectedLane === 'all') {
					for (let i = 1; i <= 8; i++) {
						cubeSettings.lanes[i].rotateX = value;
					}
				} else {
					cubeSettings.lanes[selectedLane].rotateX = value;
				}
				applyAllCubeTransforms();
				updateValueDisplays();
			});

			document.getElementById('rotateY').addEventListener('input', (e) => {
				const value = parseFloat(e.target.value);
				if (selectedLane === 'all') {
					for (let i = 1; i <= 8; i++) {
						cubeSettings.lanes[i].rotateY = value;
					}
				} else {
					cubeSettings.lanes[selectedLane].rotateY = value;
				}
				applyAllCubeTransforms();
				updateValueDisplays();
			});

			document.getElementById('rotateZ').addEventListener('input', (e) => {
				const value = parseFloat(e.target.value);
				if (selectedLane === 'all') {
					for (let i = 1; i <= 8; i++) {
						cubeSettings.lanes[i].rotateZ = value;
					}
				} else {
					cubeSettings.lanes[selectedLane].rotateZ = value;
				}
				applyAllCubeTransforms();
				updateValueDisplays();
			});

			document.getElementById('translateX').addEventListener('input', (e) => {
				const value = parseFloat(e.target.value);
				if (selectedLane === 'all') {
					for (let i = 1; i <= 8; i++) {
						cubeSettings.lanes[i].translateX = value;
					}
				} else {
					cubeSettings.lanes[selectedLane].translateX = value;
				}
				applyAllCubeTransforms();
				updateValueDisplays();
			});

			document.getElementById('translateY').addEventListener('input', (e) => {
				const value = parseFloat(e.target.value);
				if (selectedLane === 'all') {
					for (let i = 1; i <= 8; i++) {
						cubeSettings.lanes[i].translateY = value;
					}
				} else {
					cubeSettings.lanes[selectedLane].translateY = value;
				}
				applyAllCubeTransforms();
				updateValueDisplays();
			});

			document.getElementById('translateZ').addEventListener('input', (e) => {
				const value = parseFloat(e.target.value);
				if (selectedLane === 'all') {
					for (let i = 1; i <= 8; i++) {
						cubeSettings.lanes[i].translateZ = value;
					}
				} else {
					cubeSettings.lanes[selectedLane].translateZ = value;
				}
				applyAllCubeTransforms();
				updateValueDisplays();
			});

			// Initialize displays
			updateValueDisplays();
		}

		function applyAllCubeTransforms() {
			// Apply transform to each cube
			for (let i = 1; i <= 8; i++) {
				const cube = document.getElementById('cube' + i);
				const settings = cubeSettings.lanes[i];
				
				// The cube is rotated and translated in 3D space
				// The lane graphic (bottom face) appears projected onto the pool
				cube.style.transform = `
					translateX(${settings.translateX}px)
					translateY(${settings.translateY}px)
					translateZ(${settings.translateZ}px)
					rotateX(${settings.rotateX}deg)
					rotateY(${settings.rotateY}deg)
					rotateZ(${settings.rotateZ}deg)
				`;
			}
		}

		function applyPreset(preset) {
			switch(preset) {
				case 'flat':
					// Flat view - no rotation
					for (let i = 1; i <= 8; i++) {
						cubeSettings.lanes[i] = {
							rotateX: 0,
							rotateY: 0,
							rotateZ: 0,
							translateX: 0,
							translateY: (i - 1) * 120,
							translateZ: 0
						};
					}
					break;
				case 'broadcast':
					// Standard broadcast angle - Olympic style
					cubeSettings.perspective = 1500;
					cubeSettings.perspectiveX = 50;
					cubeSettings.perspectiveY = 50;
					for (let i = 1; i <= 8; i++) {
						cubeSettings.lanes[i] = {
							rotateX: 60,
							rotateY: 0,
							rotateZ: 0,
							translateX: 0,
							translateY: (i - 1) * 120,
							translateZ: -100 - (i - 1) * 50
						};
					}
					break;
				case 'olympic':
					// True Olympic pool perspective
					cubeSettings.perspective = 1800;
					cubeSettings.perspectiveX = 50;
					cubeSettings.perspectiveY = 40;
					for (let i = 1; i <= 8; i++) {
						cubeSettings.lanes[i] = {
							rotateX: 65,
							rotateY: 0,
							rotateZ: 0,
							translateX: 0,
							translateY: (i - 1) * 115,
							translateZ: -150 - (i - 1) * 60
						};
					}
					break;
				case 'overhead':
					// Overhead referee view
					cubeSettings.perspective = 2000;
					cubeSettings.perspectiveX = 50;
					cubeSettings.perspectiveY = 50;
					for (let i = 1; i <= 8; i++) {
						cubeSettings.lanes[i] = {
							rotateX: 80,
							rotateY: 0,
							rotateZ: 0,
							translateX: 0,
							translateY: (i - 1) * 110,
							translateZ: -200 - (i - 1) * 30
						};
					}
					break;
			}
			
			// Update body perspective
			document.body.style.perspective = cubeSettings.perspective + 'px';
			document.body.style.perspectiveOrigin = `${cubeSettings.perspectiveX}% ${cubeSettings.perspectiveY}%`;
			
			// Update controls
			document.getElementById('perspective').value = cubeSettings.perspective;
			document.getElementById('perspectiveX').value = cubeSettings.perspectiveX;
			document.getElementById('perspectiveY').value = cubeSettings.perspectiveY;
			
			updateSlidersForSelection();
			applyAllCubeTransforms();
		}

		function resetSelected() {
			if (selectedLane === 'all') {
				for (let i = 1; i <= 8; i++) {
					cubeSettings.lanes[i] = {
						rotateX: 60,
						rotateY: 0,
						rotateZ: 0,
						translateX: 0,
						translateY: 0,
						translateZ: 0
					};
				}
			} else {
				cubeSettings.lanes[selectedLane] = {
					rotateX: 60,
					rotateY: 0,
					rotateZ: 0,
					translateX: 0,
					translateY: 0,
					translateZ: 0
				};
			}
			
			updateSlidersForSelection();
			applyAllCubeTransforms();
		}

		function copySettings() {
			let settingsText = `CAMERA PERSPECTIVE:\nperspective: ${cubeSettings.perspective}px\nperspectiveX: ${cubeSettings.perspectiveX}%\nperspectiveY: ${cubeSettings.perspectiveY}%\n\nLANE CUBES:\n`;
			
			for (let i = 1; i <= 8; i++) {
				const s = cubeSettings.lanes[i];
				settingsText += `Lane ${i}: rotateX=${s.rotateX}° rotateY=${s.rotateY}° rotateZ=${s.rotateZ}° translateX=${s.translateX}px translateY=${s.translateY}px translateZ=${s.translateZ}px\n`;
			}
			
			navigator.clipboard.writeText(settingsText).then(() => {
				alert('Settings copied to clipboard!');
			});
		}
	</script>
	
	<script>
		const wsUrl = "ws://localhost:8001";
		let timerStartDetected = false;

		function connectWebSocket() {
			const ws = new WebSocket(wsUrl);

			ws.onmessage = (event) => {
				const data = JSON.parse(event.data);

				if (data.timerSync && data.timerSync.running && !timerStartDetected) {
					timerStartDetected = true;
					triggerFadeOut();
				}

				if (data.lanes) {
                                    timerStartDetected = false;
                                    
                                    hideAllLanes();
                                    
                                    setTimeout(() => {
                                        refreshLanesWithAnimation();
                                        
                                        // Get active lanes from the data (if available)
                                        const activeLanes = data.activeLanes || [];
                                        
                                        Object.keys(data.lanes).forEach(laneNum => {
                                            const lane = data.lanes[laneNum];
                                            const container = document.getElementById(`lane${laneNum}`);
                                            if (container) {
                                                const nameEl = container.querySelector(".name");
                                                const clubEl = container.querySelector(".club");

                                                nameEl.textContent = lane.name || "";
                                                clubEl.textContent = lane.club || "";

                                                // Hide if: no name/club OR lane is turned off
                                                const laneNumber = parseInt(laneNum);
                                                const isLaneOff = activeLanes.length > 0 && !activeLanes.includes(laneNumber);
                                                
                                                if (!lane.name && !lane.club || isLaneOff) {
                                                    container.parentElement.style.visibility = "hidden";
                                                } else {
                                                    container.parentElement.style.visibility = "visible";
                                                }
                                            }
                                        });
                                    }, 100);
                                }

                                if (data.activeLanes !== undefined) {
                                    const activeLanes = data.activeLanes;
                                    
                                    for (let i = 1; i <= 8; i++) {
                                        const container = document.getElementById(`lane${i}`);
                                        if (container && container.parentElement.style.visibility === "visible") {
                                            const isLaneOff = activeLanes.length > 0 && !activeLanes.includes(i);
                                            if (isLaneOff) {
                                                container.parentElement.classList.add("fade-out");
                                                setTimeout(() => {
                                                    container.parentElement.style.visibility = "hidden";
                                                    container.parentElement.classList.remove("fade-out");
                                                }, 1000);
                                            }
                                        }
                                    }
                                }
			};

			ws.onclose = () => {
				setTimeout(connectWebSocket, 1000);
			};
		}

		function hideAllLanes() {
			const laneCubes = document.querySelectorAll(".lane-cube");
			laneCubes.forEach(cube => {
				cube.style.opacity = "0";
				cube.style.display = "none";
				
				const laneNumber = cube.querySelector(".LaneNumber");
				const swimmerInfo = cube.querySelector(".SwimmerInfo");
				laneNumber.style.animation = "none";
				swimmerInfo.style.animation = "none";
			});
		}

		function refreshLanesWithAnimation() {
			const laneGroups = [
				[4, 5],
				[3, 6],
				[2, 7],
				[1, 8]  
			];
			
			const findGroupIndex = (number) => {
				return laneGroups.findIndex(group => group.includes(number));
			};
			
			const laneCubes = document.querySelectorAll(".lane-cube");
			laneCubes.forEach(cube => {
				const laneNumber = cube.querySelector(".LaneNumber");
				const swimmerInfo = cube.querySelector(".SwimmerInfo");
				
				cube.classList.remove("fade-out");
				cube.style.display = "block";
				
				void cube.offsetWidth;
				
				const laneNum = parseInt(cube.querySelector(".LaneNumber .content").textContent.trim(), 10);
				const groupIndex = findGroupIndex(laneNum);
				
				laneNumber.style.animation = "expandLaneNumber 1s ease forwards";
				laneNumber.style.animationDelay = (groupIndex * 0.5).toString() + "s";
				
				swimmerInfo.style.animation = "fadeInFromLeft 1s ease forwards";
				swimmerInfo.style.animationDelay = (groupIndex * 0.5 + 0.75).toString() + "s";
				
				setTimeout(() => {
					cube.style.opacity = 1;
				}, 100);
			});
		}

		function triggerFadeOut() {
			const laneCubes = document.querySelectorAll(".lane-cube");
			laneCubes.forEach(cube => {
				cube.classList.add("fade-out");
				cube.addEventListener("animationend", () => {
					cube.style.display = "none";
				}, { once: true });
			});
		}

		window.addEventListener("load", connectWebSocket);
	</script>
</body>
</html>'''

        split_times_html = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Transparent Background with Boxes</title>
    <style>
        /* Ensure the page background is fully transparent */
        body {
            margin: 0;
            padding: 0;
            background-color: transparent;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            font-family: Helvetica, serif; /* Change font */
        }

        .container {
            display: flex;
            flex-direction: column; /* Arrange items vertically */
        }

        .row {
            display: flex;
        }

        .row:first-child + .row {
            margin-top: 8px; /* Add gap between the first and second rows */
        }

        .box {
            height: 40px; /* Default height */
            background-color: #42008f; /* Default color */
            display: flex;
            align-items: center;
            position: relative;
        }

        .LaneNumber {
            width: 28px; /* Default width */
            display: flex;
            justify-content: center;
        }

        .SwimmerInfo {
            width: 300px; /* Default width */
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .LaneNumber .content {
            font-size: 28px; /* Default font size */
            color: #FFFF00; /* Default color */
        }

        .SwimmerInfo .name {
            font-size: 24px; /* Adjust font size to fit the box */
            text-transform: uppercase;
            color: #FFFFFF; /* White color */
            margin-left: 8px; /* Add padding before swimmer name */
            white-space: nowrap;
        }

        .SwimmerInfo .splitTime {
            font-size: 24px; /* Base font size */
            font-weight: normal;
            color: #FFFF00; /* Yellow text */
            white-space: nowrap; /* Prevent wrapping */
            overflow: hidden; /* Hide overflow */
            text-align: right; /* Right-align text */
            padding-right: 8px; /* Keep consistent padding */
            display: block; /* Block for consistent width */
            width: calc(100% - 16px); /* Ensures container width minus padding */
            box-sizing: border-box; /* Include padding in width calculation */
            transform-origin: right center; /* Scale text from the right */
        }
		
		
		/* Animation for container fade out - faster */
		@keyframes fadeOut {
			from {
				opacity: 0.975;
			}
			to {
				opacity: 0;
			}
		}

		/* Animation for rows appearing - smooth float from right */
		@keyframes zoomInFromRight {
			0% {
				opacity: 0;
				transform: translateX(250px);
			}
			100% {
				opacity: 0.975;
				transform: translateX(0);
			}
		}

		.row {
			transform: translateX(0);
		}

		.row.zoom-in {
			animation: zoomInFromRight 0.6s ease-in-out forwards;
		}

    </style>
    <script>
		document.addEventListener("DOMContentLoaded", () => {
			const rows = document.querySelectorAll(".row");

			rows.forEach(row => {
				const splitTime = row.querySelector(".splitTime").textContent.trim();
				if (!splitTime) {
					row.style.visibility = 'hidden'; // Hide instead of remove
				}
			});
		});
	</script>
	<script>
	document.addEventListener("DOMContentLoaded", () => {
		const nameElements = document.querySelectorAll(".name");

		nameElements.forEach(nameElement => {
			adjustFontSize(nameElement, 0.60);
		});

		function adjustFontSize(element, maxWidthPercentage) {
			const parentWidth = element.parentElement.offsetWidth;
			const maxWidth = parentWidth * maxWidthPercentage;

			let fontSize = parseInt(window.getComputedStyle(element).fontSize, 10);

			while (element.offsetWidth > maxWidth && fontSize > 1) {
				fontSize -= 1;
				element.style.fontSize = `${fontSize}px`;
			}
		}
	});
	</script>
	<!-- 1. Font size adjustment function -->
<script>
document.addEventListener("DOMContentLoaded", () => {
    const nameElements = document.querySelectorAll(".name");

    nameElements.forEach(nameElement => {
        adjustFontSize(nameElement, 0.60);
    });
});

function adjustFontSize(element, maxWidthPercentage) {
    const parentWidth = element.parentElement.offsetWidth;
    const maxWidth = parentWidth * maxWidthPercentage;

    let fontSize = parseInt(window.getComputedStyle(element).fontSize, 10);

    while (element.offsetWidth > maxWidth && fontSize > 1) {
        fontSize -= 1;
        element.style.fontSize = `${fontSize}px`;
    }
}
</script>

<!-- 2. Hide empty rows on load -->
<script>
document.addEventListener("DOMContentLoaded", () => {
    const rows = document.querySelectorAll(".row");

    rows.forEach(row => {
        const splitTime = row.querySelector(".splitTime").textContent.trim();
        if (!splitTime) {
            row.style.visibility = 'hidden';
        }
    });
});
</script>

<!-- 3. All helper functions BEFORE WebSocket -->
<script>
// Format time function
function formatTime(time) {
    return time.replace(/^0+:?0*/, '') || '0.00';
}

// Update split time display
function updateSplitTime(data) {
    const place = data.finishTime.place;
    const lane = data.finishTime.lane;
    const fullName = data.finishTime.swimmer;
    const time = data.finishTime.time;
    
    
    
    // Show the container when we have splits to display
    const container = document.querySelector('.container');
    container.style.visibility = 'visible';
    container.style.opacity = '0.975';
    container.classList.remove('fade-out');
    
    // Format name as E.BLAND (first initial + . + last name)
    const nameParts = fullName.trim().split(' ');
    let formattedName = '';
    if (nameParts.length >= 2) {
        const firstName = nameParts[0];
        const lastName = nameParts.slice(1).join(' ');
        formattedName = `${firstName.charAt(0)}.${lastName}`.toUpperCase();
    } else {
        formattedName = fullName.toUpperCase();
    }
    
    // Find the row by place (id)
    const row = document.getElementById(place);
    
    if (row) {
        
		// Set initial transform state BEFORE making visible
        row.style.opacity = '0';
        row.style.transform = 'translateX(250px)';
        row.classList.remove('zoom-in');
        
        // Update content first
        const laneNumberContent = row.querySelector('.LaneNumber .content');
        if (laneNumberContent) {
            laneNumberContent.textContent = lane;
        }
        
        const nameElement = row.querySelector('.SwimmerInfo .name');
        if (nameElement) {
            nameElement.textContent = formattedName;
            adjustFontSize(nameElement, 0.60);
        }
        
        const splitTimeElement = row.querySelector('.SwimmerInfo .splitTime');
        if (splitTimeElement) {
            let displayTime;
            
            if (parseInt(place) === 1) {
                displayTime = formatTime(time);
            } else if (leaderState.leaderTime) {
                displayTime = calculateTimeDifference(time, leaderState.leaderTime);
            } else {
                displayTime = formatTime(time);
            }
            
            splitTimeElement.textContent = displayTime;
        }
        
        // Show and animate the row
        row.style.visibility = 'visible';
       // Use setTimeout to trigger animation
        setTimeout(() => {
            row.classList.add('zoom-in');
        }, 10);
        
        // Use setTimeout to ensure the removal takes effect
        setTimeout(() => {
            row.classList.add('zoom-in');
           
        }, 10);
        
    } else {
        
    }
}

// Leader state tracking
let leaderState = {
    lastSplitTime: null,
    timeNumber: 0,
    place: null,
    softResetTimeout: null,
	leaderTime: null
};

// Calculate time difference from leader
function calculateTimeDifference(swimmerTime, leaderTime) {
    // Convert times from MM:SS.ss or SS.ss format to total seconds
    function timeToSeconds(time) {
        const parts = time.split(':');
        if (parts.length === 2) {
            // Format: MM:SS.ss
            const minutes = parseInt(parts[0]);
            const seconds = parseFloat(parts[1]);
            return minutes * 60 + seconds;
        } else {
            // Format: SS.ss
            return parseFloat(time);
        }
    }
    
    const swimmerSeconds = timeToSeconds(swimmerTime);
    const leaderSeconds = timeToSeconds(leaderTime);
    const difference = swimmerSeconds - leaderSeconds;
    
    // Format as +X.XX
    return '+' + difference.toFixed(2);
}

// Check if split time should be shown
function shouldShowSplitTime(data) {
    const finishTime = data.finishTime;
    const place = parseInt(finishTime.place);
    const timeNumber = finishTime.timeNumber;
    const currentTime = Date.now();
    
    // Always show first place
    if (place === 1) {
        leaderState.lastSplitTime = currentTime;
        leaderState.timeNumber = timeNumber;
        leaderState.place = place;
		leaderState.leaderTime = finishTime.time;  // ADD THIS LINE
        
        
        // Clear any existing soft reset timeout
        if (leaderState.softResetTimeout) {
            clearTimeout(leaderState.softResetTimeout);
        }
        
        // Schedule soft reset for 10 seconds after leader's split
        leaderState.softResetTimeout = setTimeout(() => {
            
            softResetRows();
        }, 10000);
        
        return true;
    }
    
    // Check if they've been lapped
    if (timeNumber < leaderState.timeNumber) {
        
        return false;
    }
    
    // Check if more than 10 seconds since leader's last split
    if (leaderState.lastSplitTime) {
        const timeSinceLeader = (currentTime - leaderState.lastSplitTime) / 1000;
        if (timeSinceLeader > 10) {
            
            return false;
        }
    }
    
    return true;
}

// Soft reset - clear display but keep tracking
function softResetRows() {
    
    
    const container = document.querySelector('.container');
   
    
    // Add fade out animation
    container.classList.add('fade-out');
    
    // Wait for animation to complete before hiding
    setTimeout(() => {
       
        container.style.visibility = 'hidden';
        container.style.opacity = '0';
        container.classList.remove('fade-out');
        
        const rows = document.querySelectorAll('.row');
        
        rows.forEach(row => {
            const laneNumberContent = row.querySelector('.LaneNumber .content');
            if (laneNumberContent) {
                laneNumberContent.textContent = '';
            }
            
            const nameElement = row.querySelector('.SwimmerInfo .name');
            if (nameElement) {
                nameElement.textContent = '';
            }
            
            const splitTimeElement = row.querySelector('.SwimmerInfo .splitTime');
            if (splitTimeElement) {
                splitTimeElement.textContent = '';
            }
            
            row.style.visibility = 'hidden';
            row.classList.remove('zoom-in');
        });
    }, 500);
}

// Full reset - clear everything including tracking
function resetAllRows() {
    
    
    if (leaderState.softResetTimeout) {
        clearTimeout(leaderState.softResetTimeout);
    }
    
    leaderState = {
        lastSplitTime: null,
        timeNumber: 0,
        place: null,
        softResetTimeout: null,
		leaderTime: null
    };
    const container = document.querySelector('.container');
    
    // Add fade out animation
    container.classList.add('fade-out');
    
    // Wait for animation to complete
    setTimeout(() => {
        container.style.visibility = 'hidden';
        container.classList.remove('fade-out');
        
        const rows = document.querySelectorAll('.row');
        
        rows.forEach(row => {
            const laneNumberContent = row.querySelector('.LaneNumber .content');
            if (laneNumberContent) {
                laneNumberContent.textContent = '';
            }
            
            const nameElement = row.querySelector('.SwimmerInfo .name');
            if (nameElement) {
                nameElement.textContent = '';
            }
            
            const splitTimeElement = row.querySelector('.SwimmerInfo .splitTime');
            if (splitTimeElement) {
                splitTimeElement.textContent = '';
            }
            
            row.style.visibility = 'hidden';
            row.classList.remove('zoom-in');
        });
    }, 500);
}

// Timer state tracking
let timerWasRunning = false;

// Handle timer sync messages
function handleTimerSync(data) {
    const isRunning = data.timerSync.running;
    const container = document.querySelector('.container');
    
    if (!isRunning && timerWasRunning) {
        container.style.visibility = 'hidden';
        resetAllRows();
        timerWasRunning = false;
    } else if (isRunning) {
        timerWasRunning = true;
    }
}
</script>

<!-- 4. WebSocket connection LAST -->
<script>
const wsUrl = "ws://localhost:8001";
let ws;

function connectWebSocket() {
    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
        console.log('[WS] Connected to Swim Live System');
    };

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);

        // Handle timer sync
        if (data.timerSync) {
            handleTimerSync(data);
        }

        // Only process split times
        if (data.finishTime && data.finishTime.type === "SPLIT") {
            
            if (shouldShowSplitTime(data)) {
                updateSplitTime(data);
            }
        }
    };

    ws.onclose = () => {
        console.log('[WS] Connection closed. Reconnecting...');
        setTimeout(connectWebSocket, 1000);
    };

    ws.onerror = (error) => {
        console.error('[WS] WebSocket error:', error);
    };
}

window.addEventListener('load', connectWebSocket);

window.addEventListener('beforeunload', () => {
    if (ws) ws.close();
});
</script>

</head>
<body>
    <div class="container">
        <!-- Row 1 -->
        <div class="row" id ="1">
            <div class="box LaneNumber">
                <div class="content"></div>
            </div>
            <div class="box SwimmerInfo">
                <div class="name"></div>
                <div class="splitTime"></div>
            </div>
        </div>
        <!-- Row 2 -->
        <div class="row" id ="2">
            <div class="box LaneNumber">
                <div class="content"></div>
            </div>
            <div class="box SwimmerInfo">
                <div class="name"></div>
                <div class="splitTime"></div>
            </div>
        </div>
        <!-- Row 3 -->
        <div class="row" id ="3">
            <div class="box LaneNumber">
                <div class="content"></div>
            </div>
            <div class="box SwimmerInfo">
                <div class="name"></div>
                <div class="splitTime"></div>
            </div>
        </div>
        <!-- Row 4 -->
        <div class="row" id ="4">
            <div class="box LaneNumber">
                <div class="content"></div>
            </div>
            <div class="box SwimmerInfo">
                <div class="name"></div>
                <div class="splitTime"></div>
            </div>
        </div>
		<!-- Row 5 -->
        <div class="row" id ="5">
            <div class="box LaneNumber">
                <div class="content"></div>
            </div>
            <div class="box SwimmerInfo">
                <div class="name"></div>
                <div class="splitTime"></div>
            </div>
        </div>
		<!-- Row 6 -->
        <div class="row" id ="6">
            <div class="box LaneNumber">
                <div class="content"></div>
            </div>
            <div class="box SwimmerInfo">
                <div class="name"></div>
                <div class="splitTime"></div>
            </div>
        </div>
		<!-- Row 7 -->
        <div class="row" id ="7">
            <div class="box LaneNumber">
                <div class="content"></div>
            </div>
            <div class="box SwimmerInfo">
                <div class="name"></div>
                <div class="splitTime"></div>
            </div>
        </div>
		<!-- Row 8 -->
        <div class="row" id ="8">
            <div class="box LaneNumber">
                <div class="content"></div>
            </div>
            <div class="box SwimmerInfo">
                <div class="name"></div>
                <div class="splitTime"></div>
            </div>
        </div>
    </div>
</body>
</html>'''
        lane_ends_html = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Lane Ends - Improved</title>
    <style>
        body {
            margin: 0;
            padding: 0;
            background-color: transparent;
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            height: 100vh;
            font-family: Helvetica, serif;
            gap: 4px;
            overflow: hidden;
            perspective: 1500px;
            perspective-origin: 50% 50%;
        }

        #laneWrapper {
            transform-style: preserve-3d;
            position: relative;
        }

        @keyframes fadeInFromLeft {
            from {
                opacity: 0;
                clip-path: inset(0 100% 0 0);
            }
            to {
                opacity: 0.85;
                clip-path: inset(0 0 0 0);
            }
        }

        @keyframes expandPosition {
            from {
                transform: scale(0);
                opacity: 0;
            }
            to {
                transform: scale(1);
                opacity: 0.85;
            }
        }

        @keyframes fadeOut {
            from {
                opacity: 0.85;
            }
            to {
                opacity: 0;
            }
        }

        .container {
            display: flex;
            gap: 8px;
            opacity: 0;
            visibility: hidden;
            transform-style: preserve-3d;
        }

        .box {
            height: 112px;
            background-color: #42008f;
            opacity: 0.85;
            display: flex;
            justify-content: center;
            align-items: center;
            position: relative;
        }

        .SwimmerInfo {
            display: flex;
            align-items: center;
            justify-content: flex-start;
            animation: fadeInFromLeft 1s ease forwards;
            opacity: 0;
        }

        .SwimmerInfo .name {
            font-size: 52px;
            font-weight: bold;
            color: #FFFFFF;
            opacity: 0.95;
            margin-left: 16px;
            margin-right: 16px;
            white-space: nowrap;
        }

        .SwimmerTime {
            display: flex;
            justify-content: flex-end;
            align-items: center;
            animation: fadeInFromLeft 1s ease forwards;
            animation-delay: 0.5s;
            opacity: 0;
            width: 200px;
        }

        .SwimmerTime .time {
            font-size: 48px;
            font-weight: bold;
            color: #FFFFFF;
            white-space: nowrap;
            margin-right: 16px;
            transform: scaleY(1.4);
            transform-origin: center;
        }

        .LaneNumber {
            width: 88px;
            justify-content: center;
            transform-origin: center;
            animation: expandPosition 1s ease forwards;
            animation-delay: 1s;
            opacity: 0;
        }

        .LaneNumber .position {
            font-size: 96px;
            font-weight: bold;
            color: #FFFF00;
            opacity: 0.95;
        }

        .fade-out {
            animation: fadeOut 1s ease forwards;
        }

        /* Control panel */
        .control-panel {
            position: fixed;
            top: 20px;
            right: 20px;
            background: rgba(0, 0, 0, 0.95);
            padding: 25px;
            border-radius: 12px;
            color: white;
            font-family: Arial, sans-serif;
            font-size: 14px;
            z-index: 10000;
            width: 450px;
            max-height: 90vh;
            overflow-y: auto;
            box-shadow: 0 8px 32px rgba(0,0,0,0.8);
            border: 2px solid #00D9FF;
            display: none;
        }

        .control-panel.visible {
            display: block;
        }

        .control-panel h2 {
            margin: 0 0 20px 0;
            font-size: 24px;
            color: #00D9FF;
            text-align: center;
            border-bottom: 2px solid #00D9FF;
            padding-bottom: 12px;
        }

        .control-section {
            background: rgba(255,255,255,0.05);
            padding: 18px;
            border-radius: 8px;
            margin-bottom: 18px;
            border: 1px solid rgba(0, 217, 255, 0.3);
        }

        .control-section h3 {
            margin: 0 0 15px 0;
            font-size: 18px;
            color: #00D9FF;
        }

        .control-group {
            margin-bottom: 18px;
        }

        .control-group label {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 8px;
            font-size: 14px;
            color: #ddd;
            font-weight: bold;
        }

        .control-group input[type="range"] {
            width: 100%;
            height: 6px;
            margin-top: 5px;
        }

        .value-display {
            color: #00D9FF;
            font-weight: bold;
            font-size: 16px;
            min-width: 80px;
            text-align: right;
        }

        .button-group {
            display: flex;
            gap: 10px;
            margin-top: 15px;
        }

        .button-group button {
            flex: 1;
            padding: 12px;
            background: #00D9FF;
            border: none;
            border-radius: 6px;
            color: black;
            cursor: pointer;
            font-size: 14px;
            font-weight: bold;
            transition: all 0.2s;
        }

        .button-group button:hover {
            background: #00B8D4;
            transform: translateY(-2px);
            box-shadow: 0 4px 8px rgba(0, 217, 255, 0.4);
        }

        .button-group button.secondary {
            background: #666;
            color: white;
        }

        .button-group button.secondary:hover {
            background: #555;
        }

        .instructions {
            background: rgba(0, 217, 255, 0.15);
            border: 2px solid #00D9FF;
            padding: 12px;
            border-radius: 8px;
            font-size: 13px;
            line-height: 1.6;
            color: #ccc;
            margin-bottom: 18px;
        }

        .instructions strong {
            color: #00D9FF;
            display: block;
            margin-bottom: 6px;
            font-size: 14px;
        }

        .preset-buttons {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
            margin-top: 12px;
        }

        .preset-buttons button {
            padding: 10px;
            background: #9C27B0;
            border: none;
            border-radius: 6px;
            color: white;
            cursor: pointer;
            font-size: 13px;
            font-weight: bold;
            transition: all 0.2s;
        }

        .preset-buttons button:hover {
            background: #7B1FA2;
            transform: translateY(-1px);
        }

        .lane-select {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 8px;
            margin-bottom: 15px;
        }

        .lane-select button {
            padding: 10px;
            background: rgba(0, 217, 255, 0.2);
            border: 2px solid rgba(0, 217, 255, 0.3);
            border-radius: 6px;
            color: white;
            cursor: pointer;
            font-size: 14px;
            font-weight: bold;
            transition: all 0.2s;
        }

        .lane-select button.active {
            background: #00D9FF;
            color: black;
            border-color: #00D9FF;
        }

        .lane-select button:hover {
            background: rgba(0, 217, 255, 0.4);
        }

        .all-lanes-label {
            color: #00D9FF;
            font-weight: bold;
            margin-bottom: 10px;
            display: block;
        }
    </style>
</head>
<body>
    <div id="laneWrapper">
    <div class="container" id="1">
        <div class="box SwimmerInfo">
            <div class="name"></div>
        </div>
        <div class="box SwimmerTime">
            <div class="time"></div>
        </div>
        <div class="box LaneNumber">
            <div class="position"></div>
        </div>
    </div>
    <div class="container" id="2">
        <div class="box SwimmerInfo">
            <div class="name"></div>
        </div>
        <div class="box SwimmerTime">
            <div class="time"></div>
        </div>
        <div class="box LaneNumber">
            <div class="position"></div>
        </div>
    </div>
    <div class="container" id="3">
        <div class="box SwimmerInfo">
            <div class="name"></div>
        </div>
        <div class="box SwimmerTime">
            <div class="time"></div>
        </div>
        <div class="box LaneNumber">
            <div class="position"></div>
        </div>
    </div>
    <div class="container" id="4">
        <div class="box SwimmerInfo">
            <div class="name"></div>
        </div>
        <div class="box SwimmerTime">
            <div class="time"></div>
        </div>
        <div class="box LaneNumber">
            <div class="position"></div>
        </div>
    </div>
    <div class="container" id="5">
        <div class="box SwimmerInfo">
            <div class="name"></div>
        </div>
        <div class="box SwimmerTime">
            <div class="time"></div>
        </div>
        <div class="box LaneNumber">
            <div class="position"></div>
        </div>
    </div>
    <div class="container" id="6">
        <div class="box SwimmerInfo">
            <div class="name"></div>
        </div>
        <div class="box SwimmerTime">
            <div class="time"></div>
        </div>
        <div class="box LaneNumber">
            <div class="position"></div>
        </div>
    </div>
    <div class="container" id="7">
        <div class="box SwimmerInfo">
            <div class="name"></div>
        </div>
        <div class="box SwimmerTime">
            <div class="time"></div>
        </div>
        <div class="box LaneNumber">
            <div class="position"></div>
        </div>
    </div>
    <div class="container" id="8">
        <div class="box SwimmerInfo">
            <div class="name"></div>
        </div>
        <div class="box SwimmerTime">
            <div class="time"></div>
        </div>
        <div class="box LaneNumber">
            <div class="position"></div>
        </div>
    </div>
    </div>

    <div class="control-panel" id="controlPanel">
        <h2>🎯 Lane Ends Projection</h2>
        
        <div class="instructions">
            <strong>Cube Projection System:</strong>
            Each finish result is the bottom face of an imaginary cube. Rotate the cube in 3D space to project the graphic onto the pool surface from your camera's viewpoint!
        </div>

        <div class="control-section">
            <h3>Camera Perspective</h3>
            
            <div class="control-group">
                <label>
                    <span>Perspective Distance</span>
                    <span class="value-display" id="perspectiveValue">1500px</span>
                </label>
                <input type="range" id="perspective" min="500" max="3000" step="50" value="1500">
            </div>

            <div class="control-group">
                <label>
                    <span>View X Origin</span>
                    <span class="value-display" id="perspectiveXValue">50%</span>
                </label>
                <input type="range" id="perspectiveX" min="0" max="100" step="1" value="50">
            </div>

            <div class="control-group">
                <label>
                    <span>View Y Origin</span>
                    <span class="value-display" id="perspectiveYValue">50%</span>
                </label>
                <input type="range" id="perspectiveY" min="0" max="100" step="1" value="50">
            </div>
        </div>

        <div class="control-section">
            <h3>Select Lane to Adjust</h3>
            <div class="lane-select">
                <button onclick="selectLane('all')" class="active" id="btnAll">ALL</button>
                <button onclick="selectLane(1)" id="btn1">L1</button>
                <button onclick="selectLane(2)" id="btn2">L2</button>
                <button onclick="selectLane(3)" id="btn3">L3</button>
                <button onclick="selectLane(4)" id="btn4">L4</button>
                <button onclick="selectLane(5)" id="btn5">L5</button>
                <button onclick="selectLane(6)" id="btn6">L6</button>
                <button onclick="selectLane(7)" id="btn7">L7</button>
                <button onclick="selectLane(8)" id="btn8">L8</button>
            </div>

            <span class="all-lanes-label" id="controlLabel">Adjusting: ALL LANES</span>

            <div class="control-group">
                <label>
                    <span>Rotate X (Tilt)</span>
                    <span class="value-display" id="rotateXValue">0°</span>
                </label>
                <input type="range" id="rotateX" min="0" max="90" step="0.5" value="0">
            </div>

            <div class="control-group">
                <label>
                    <span>Rotate Y (Turn)</span>
                    <span class="value-display" id="rotateYValue">0°</span>
                </label>
                <input type="range" id="rotateY" min="-45" max="45" step="0.5" value="0">
            </div>

            <div class="control-group">
                <label>
                    <span>Rotate Z (Roll)</span>
                    <span class="value-display" id="rotateZValue">0°</span>
                </label>
                <input type="range" id="rotateZ" min="-15" max="15" step="0.1" value="0">
            </div>

            <div class="control-group">
                <label>
                    <span>Translate X (Left/Right)</span>
                    <span class="value-display" id="translateXValue">0px</span>
                </label>
                <input type="range" id="translateX" min="-500" max="500" step="5" value="0">
            </div>

            <div class="control-group">
                <label>
                    <span>Translate Y (Up/Down)</span>
                    <span class="value-display" id="translateYValue">0px</span>
                </label>
                <input type="range" id="translateY" min="-500" max="500" step="5" value="0">
            </div>

            <div class="control-group">
                <label>
                    <span>Translate Z (Depth)</span>
                    <span class="value-display" id="translateZValue">0px</span>
                </label>
                <input type="range" id="translateZ" min="-800" max="400" step="10" value="0">
            </div>
        </div>

        <div class="control-section">
            <h3>Quick Presets</h3>
            <div class="preset-buttons">
                <button onclick="applyPreset('flat')">Flat View</button>
                <button onclick="applyPreset('broadcast')">Broadcast</button>
                <button onclick="applyPreset('olympic')">Olympic</button>
                <button onclick="applyPreset('overhead')">Overhead</button>
            </div>
        </div>

        <div class="button-group">
            <button onclick="resetSelected()" class="secondary">Reset Selected</button>
            <button onclick="copySettings()">Copy Values</button>
        </div>

        <div style="margin-top: 12px; padding: 12px; background: rgba(0, 217, 255, 0.2); border-radius: 8px; text-align: center; color: #00D9FF; font-size: 12px;">
            Each result = bottom of cube • Rotate cube to project
        </div>
    </div>

    <script>
        // Cube projection settings
        const cubeSettings = {
            perspective: 1500,
            perspectiveX: 50,
            perspectiveY: 50,
            lanes: {
                1: { rotateX: 0, rotateY: 0, rotateZ: 0, translateX: 0, translateY: 0, translateZ: 0 },
                2: { rotateX: 0, rotateY: 0, rotateZ: 0, translateX: 0, translateY: 0, translateZ: 0 },
                3: { rotateX: 0, rotateY: 0, rotateZ: 0, translateX: 0, translateY: 0, translateZ: 0 },
                4: { rotateX: 0, rotateY: 0, rotateZ: 0, translateX: 0, translateY: 0, translateZ: 0 },
                5: { rotateX: 0, rotateY: 0, rotateZ: 0, translateX: 0, translateY: 0, translateZ: 0 },
                6: { rotateX: 0, rotateY: 0, rotateZ: 0, translateX: 0, translateY: 0, translateZ: 0 },
                7: { rotateX: 0, rotateY: 0, rotateZ: 0, translateX: 0, translateY: 0, translateZ: 0 },
                8: { rotateX: 0, rotateY: 0, rotateZ: 0, translateX: 0, translateY: 0, translateZ: 0 }
            }
        };

        let selectedLane = 'all';
        let isInteracting = false;
        let interactTimeout = null;

        window.addEventListener('load', function() {
            setupCubeControls();
            applyCubeTransforms();
            detectInteractMode();
        });

        function detectInteractMode() {
            const panel = document.getElementById('controlPanel');

            document.addEventListener('mousemove', () => {
                if (!isInteracting) {
                    isInteracting = true;
                    panel.classList.add('visible');
                }
                
                clearTimeout(interactTimeout);
                interactTimeout = setTimeout(() => {
                    isInteracting = false;
                    panel.classList.remove('visible');
                }, 3000);
            });

            document.addEventListener('click', () => {
                isInteracting = true;
                panel.classList.add('visible');
                
                clearTimeout(interactTimeout);
                interactTimeout = setTimeout(() => {
                    isInteracting = false;
                    panel.classList.remove('visible');
                }, 5000);
            });
        }

        function selectLane(lane) {
            selectedLane = lane;
            
            document.querySelectorAll('.lane-select button').forEach(btn => {
                btn.classList.remove('active');
            });
            
            if (lane === 'all') {
                document.getElementById('btnAll').classList.add('active');
                document.getElementById('controlLabel').textContent = 'Adjusting: ALL LANES';
            } else {
                document.getElementById('btn' + lane).classList.add('active');
                document.getElementById('controlLabel').textContent = 'Adjusting: LANE ' + lane;
            }
            
            updateSlidersForSelection();
        }

        function updateSlidersForSelection() {
            const settings = selectedLane === 'all' ? cubeSettings.lanes[1] : cubeSettings.lanes[selectedLane];
            document.getElementById('rotateX').value = settings.rotateX;
            document.getElementById('rotateY').value = settings.rotateY;
            document.getElementById('rotateZ').value = settings.rotateZ;
            document.getElementById('translateX').value = settings.translateX;
            document.getElementById('translateY').value = settings.translateY;
            document.getElementById('translateZ').value = settings.translateZ;
            updateValueDisplays();
        }

        function updateValueDisplays() {
            document.getElementById('perspectiveValue').textContent = cubeSettings.perspective + 'px';
            document.getElementById('perspectiveXValue').textContent = cubeSettings.perspectiveX + '%';
            document.getElementById('perspectiveYValue').textContent = cubeSettings.perspectiveY + '%';
            document.getElementById('rotateXValue').textContent = document.getElementById('rotateX').value + '°';
            document.getElementById('rotateYValue').textContent = document.getElementById('rotateY').value + '°';
            document.getElementById('rotateZValue').textContent = document.getElementById('rotateZ').value + '°';
            document.getElementById('translateXValue').textContent = document.getElementById('translateX').value + 'px';
            document.getElementById('translateYValue').textContent = document.getElementById('translateY').value + 'px';
            document.getElementById('translateZValue').textContent = document.getElementById('translateZ').value + 'px';
        }

        function setupCubeControls() {
            document.getElementById('perspective').addEventListener('input', (e) => {
                cubeSettings.perspective = parseFloat(e.target.value);
                document.body.style.perspective = cubeSettings.perspective + 'px';
                updateValueDisplays();
            });

            document.getElementById('perspectiveX').addEventListener('input', (e) => {
                cubeSettings.perspectiveX = parseFloat(e.target.value);
                document.body.style.perspectiveOrigin = `${cubeSettings.perspectiveX}% ${cubeSettings.perspectiveY}%`;
                updateValueDisplays();
            });

            document.getElementById('perspectiveY').addEventListener('input', (e) => {
                cubeSettings.perspectiveY = parseFloat(e.target.value);
                document.body.style.perspectiveOrigin = `${cubeSettings.perspectiveX}% ${cubeSettings.perspectiveY}%`;
                updateValueDisplays();
            });

            ['rotateX', 'rotateY', 'rotateZ', 'translateX', 'translateY', 'translateZ'].forEach(prop => {
                document.getElementById(prop).addEventListener('input', (e) => {
                    const value = parseFloat(e.target.value);
                    if (selectedLane === 'all') {
                        for (let i = 1; i <= 8; i++) {
                            cubeSettings.lanes[i][prop] = value;
                        }
                    } else {
                        cubeSettings.lanes[selectedLane][prop] = value;
                    }
                    applyCubeTransforms();
                    updateValueDisplays();
                });
            });

            updateValueDisplays();
        }

        function applyCubeTransforms() {
            for (let i = 1; i <= 8; i++) {
                const container = document.getElementById(i.toString());
                const settings = cubeSettings.lanes[i];
                
                container.style.transform = `
                    translateX(${settings.translateX}px)
                    translateY(${settings.translateY}px)
                    translateZ(${settings.translateZ}px)
                    rotateX(${settings.rotateX}deg)
                    rotateY(${settings.rotateY}deg)
                    rotateZ(${settings.rotateZ}deg)
                `;
            }
        }

        function applyPreset(preset) {
            switch(preset) {
                case 'flat':
                    for (let i = 1; i <= 8; i++) {
                        cubeSettings.lanes[i] = {
                            rotateX: 0, rotateY: 0, rotateZ: 0,
                            translateX: 0, translateY: 0, translateZ: 0
                        };
                    }
                    break;
                case 'broadcast':
                    cubeSettings.perspective = 1500;
                    cubeSettings.perspectiveX = 50;
                    cubeSettings.perspectiveY = 50;
                    for (let i = 1; i <= 8; i++) {
                        cubeSettings.lanes[i] = {
                            rotateX: 60, rotateY: 0, rotateZ: 0,
                            translateX: 0, translateY: 0, translateZ: -100
                        };
                    }
                    break;
                case 'olympic':
                    cubeSettings.perspective = 1800;
                    cubeSettings.perspectiveX = 50;
                    cubeSettings.perspectiveY = 40;
                    for (let i = 1; i <= 8; i++) {
                        cubeSettings.lanes[i] = {
                            rotateX: 65, rotateY: 0, rotateZ: 0,
                            translateX: 0, translateY: 0, translateZ: -150
                        };
                    }
                    break;
                case 'overhead':
                    cubeSettings.perspective = 2000;
                    cubeSettings.perspectiveX = 50;
                    cubeSettings.perspectiveY = 50;
                    for (let i = 1; i <= 8; i++) {
                        cubeSettings.lanes[i] = {
                            rotateX: 80, rotateY: 0, rotateZ: 0,
                            translateX: 0, translateY: 0, translateZ: -200
                        };
                    }
                    break;
            }
            
            document.body.style.perspective = cubeSettings.perspective + 'px';
            document.body.style.perspectiveOrigin = `${cubeSettings.perspectiveX}% ${cubeSettings.perspectiveY}%`;
            document.getElementById('perspective').value = cubeSettings.perspective;
            document.getElementById('perspectiveX').value = cubeSettings.perspectiveX;
            document.getElementById('perspectiveY').value = cubeSettings.perspectiveY;
            
            updateSlidersForSelection();
            applyCubeTransforms();
        }

        function resetSelected() {
            if (selectedLane === 'all') {
                for (let i = 1; i <= 8; i++) {
                    cubeSettings.lanes[i] = { rotateX: 0, rotateY: 0, rotateZ: 0, translateX: 0, translateY: 0, translateZ: 0 };
                }
            } else {
                cubeSettings.lanes[selectedLane] = { rotateX: 0, rotateY: 0, rotateZ: 0, translateX: 0, translateY: 0, translateZ: 0 };
            }
            
            updateSlidersForSelection();
            applyCubeTransforms();
        }

        function copySettings() {
            let text = `CAMERA PERSPECTIVE:\nperspective: ${cubeSettings.perspective}px\nperspectiveX: ${cubeSettings.perspectiveX}%\nperspectiveY: ${cubeSettings.perspectiveY}%\n\nLANE TRANSFORMS:\n`;
            for (let i = 1; i <= 8; i++) {
                const s = cubeSettings.lanes[i];
                text += `Lane ${i}: rotateX=${s.rotateX}° rotateY=${s.rotateY}° rotateZ=${s.rotateZ}° translateX=${s.translateX}px translateY=${s.translateY}px translateZ=${s.translateZ}px\n`;
            }
            navigator.clipboard.writeText(text).then(() => alert('Settings copied to clipboard!'));
        }
    </script>

    <script>
        function formatTime(time) {
            return time.replace(/^0+:?0*/, '') || '0.00';
        }

        function formatName(fullName) {
            return fullName.trim().split(' ').map(function(word) {
                return word.charAt(0).toUpperCase() + word.slice(1).toLowerCase();
            }).join(' ');
        }

        function measureTextWidth(text, fontSize, fontWeight) {
            const canvas = document.createElement('canvas');
            const context = canvas.getContext('2d');
            context.font = fontWeight + ' ' + fontSize + 'px Helvetica';
            return context.measureText(text).width;
        }

        function adjustFontSize(element, maxWidth) {
            let fontSize = parseInt(window.getComputedStyle(element).fontSize, 10);
            
            while (element.offsetWidth > maxWidth && fontSize > 1) {
                fontSize -= 1;
                element.style.fontSize = fontSize + 'px';
            }
        }

        let raceState = {
            activeLanes: [],
            totalActive: 0,
            finishedLanes: new Set(),
            lastFinishTime: null,
            hideTimeout: null
        };

        function updateFinishTime(data) {
            const lane = data.finishTime.lane;
            const place = data.finishTime.place;
            const fullName = data.finishTime.swimmer;
            const time = data.finishTime.time;

       

            raceState.lastFinishTime = Date.now();
            
            // Track this lane as finished
            raceState.finishedLanes.add(lane);

            const container = document.getElementById(lane);

            if (container) {
        

                const formattedName = formatName(fullName);
                const formattedTime = formatTime(time);

                const defaultTimeWidth = 200;
                const actualTimeWidth = measureTextWidth(formattedTime, 48, 'bold') + 32;
                const timeWidth = Math.max(defaultTimeWidth, actualTimeWidth);
                
                const timeBox = container.querySelector('.SwimmerTime');
                timeBox.style.width = timeWidth + 'px';

                const totalWidth = 850;
                const swimmerInfoWidth = totalWidth - timeWidth - 88 - 16;
                const swimmerInfoBox = container.querySelector('.SwimmerInfo');
                swimmerInfoBox.style.width = swimmerInfoWidth + 'px';

                const timeElement = container.querySelector('.SwimmerTime .time');
                if (timeElement) {
                    timeElement.textContent = formattedTime;
                }

                const nameElement = container.querySelector('.SwimmerInfo .name');
                if (nameElement) {
                    nameElement.textContent = formattedName;
                    nameElement.style.fontSize = '52px';
                    
                    setTimeout(function() {
                        const maxNameWidth = swimmerInfoWidth - 32;
                        adjustFontSize(nameElement, maxNameWidth);
                    }, 10);
                }

                const positionElement = container.querySelector('.LaneNumber .position');
                if (positionElement) {
                    positionElement.textContent = place;
                }

                container.style.visibility = 'visible';
                container.style.opacity = '1';

                const swimmerInfo = container.querySelector('.SwimmerInfo');
                const swimmerTime = container.querySelector('.SwimmerTime');
                const laneNumber = container.querySelector('.LaneNumber');
                
                swimmerInfo.style.animation = 'none';
                swimmerTime.style.animation = 'none';
                laneNumber.style.animation = 'none';

                void swimmerInfo.offsetWidth;
                void swimmerTime.offsetWidth;
                void laneNumber.offsetWidth;

                swimmerInfo.style.animation = 'fadeInFromLeft 1s ease forwards';
                swimmerTime.style.animation = 'fadeInFromLeft 1s ease forwards';
                swimmerTime.style.animationDelay = '0.5s';
                laneNumber.style.animation = 'expandPosition 1s ease forwards';
                laneNumber.style.animationDelay = '1s';

            } else {
                console.error('[FINISH] Could not find container with lane id:', lane);
            }
        }

        function checkIfAllFinished() {
            // Only check if we have active lanes data
            if (raceState.totalActive === 0) {
      
                return;
            }


            // Check if all active lanes have finished
            const allFinished = raceState.activeLanes.every(function(lane) {
                return raceState.finishedLanes.has(lane);
            });

            if (allFinished && raceState.totalActive > 0) {
        
            }
        }


        function hideAndResetContainers() {
            const containers = document.querySelectorAll('.container');

            containers.forEach(function(container) {
                container.classList.add('fade-out');
            });

            setTimeout(function() {
                containers.forEach(function(container) {
                    const positionElement = container.querySelector('.LaneNumber .position');
                    if (positionElement) positionElement.textContent = '';

                    const nameElement = container.querySelector('.SwimmerInfo .name');
                    if (nameElement) {
                        nameElement.textContent = '';
                        nameElement.style.fontSize = '52px';
                    }

                    const timeElement = container.querySelector('.SwimmerTime .time');
                    if (timeElement) timeElement.textContent = '';

                    container.style.visibility = 'hidden';
                    container.style.opacity = '0';
                    container.classList.remove('fade-out');

                    container.querySelector('.SwimmerInfo').style.width = '';
                    container.querySelector('.SwimmerTime').style.width = '';

                    const swimmerInfo = container.querySelector('.SwimmerInfo');
                    const swimmerTime = container.querySelector('.SwimmerTime');
                    const laneNumber = container.querySelector('.LaneNumber');
                    
                    if (swimmerInfo) swimmerInfo.style.animation = 'none';
                    if (swimmerTime) swimmerTime.style.animation = 'none';
                    if (laneNumber) laneNumber.style.animation = 'none';
                });

                raceState = {
                    activeLanes: [],
                    totalActive: 0,
                    finishedLanes: new Set(),
                    lastFinishTime: null,
                    hideTimeout: null
                };
            }, 1000);
        }

        function trackSplitLane(data) {
            const lane = data.finishTime.lane;
   
        }

        function updateActiveLanes(data) {
            if (data.activeLanes && data.totalActive !== undefined) {
                raceState.activeLanes = data.activeLanes;
                raceState.totalActive = data.totalActive;
   
                
                // Check if all have finished (in case this update comes after some finishes)
                checkIfAllFinished();
            }
        }

        const wsUrl = "ws://localhost:8001";
        let ws;

        function connectWebSocket() {
            ws = new WebSocket(wsUrl);

            ws.onopen = function() {
                console.log('[WS] Connected to Swim Live System');
            };

            ws.onmessage = function(event) {
                const data = JSON.parse(event.data);


                if (data.status === "SAVED") {
                    hideAndResetContainers();
                    return; // Stop processing further
                }
                
                // Update active lanes whenever we receive them
                if (data.activeLanes !== undefined) {
                    updateActiveLanes(data);
                }

                if (data.finishTime && data.finishTime.type === "SPLIT") {
                    trackSplitLane(data);
                }

                if (data.finishTime && data.finishTime.type === "FINISH") {
                  

                    updateFinishTime(data);
                }
            };

            ws.onclose = function() {
                console.log('[WS] Connection closed. Reconnecting...');
                setTimeout(connectWebSocket, 1000);
            };

            ws.onerror = function(error) {
                console.error('[WS] WebSocket error:', error);
            };
        }

        window.addEventListener('load', connectWebSocket);

        window.addEventListener('beforeunload', function() {
            if (ws) ws.close();
        });
    </script>
</body>
</html>'''
        
        # Write HTML files
        with open(self.html_dir / "EventTimer.html", 'w', encoding='utf-8') as f:
            f.write(event_timer_html)
        with open(self.html_dir / "LaneStarts.html", 'w', encoding='utf-8') as f:
            f.write(lane_starts_html)
        with open(self.html_dir / "SplitTimes.html", 'w', encoding='utf-8') as f:
            f.write(split_times_html)
        with open(self.html_dir / "LaneEnds.html", 'w', encoding='utf-8') as f:
            f.write(lane_ends_html)
        
        print(f"[OK] Generated HTML files in: {self.html_dir}")
    
    # CTS Gen7 Reader Methods
    def _read_event_file(self, event_id: str) -> List[str]:
        """Read and parse event files."""
        if not event_id or event_id == 'N/A':
            return []
        filename = f"E{event_id}.scb"
        filepath = self.event_files_path / filename
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return [line.rstrip() for line in f.readlines()]
        except (FileNotFoundError, IOError):
            return []

    def _extract_distance_from_event_name(self, event_name: str) -> Optional[int]:
        """Extract distance in meters from event name.
        Returns: distance in meters (e.g., 50, 100, 200) or None
        """
        words = event_name.split()
        for word in words:
            # Look for pattern like "50M", "100M", "200M", etc.
            word_lower = word.lower()
            if word_lower.endswith('m') and word_lower[:-1].isdigit():
                try:
                    return int(word_lower[:-1])
                except ValueError:
                    pass
        return None
    
    def _get_event_name(self, event_id: str) -> str:
        """Extract and clean event name."""
        if not event_id or event_id == 'N/A':
            return event_id
        if event_id in self.event_cache:
            return self.event_cache[event_id]
        lines = self._read_event_file(event_id)
        if not lines:
            self.event_cache[event_id] = event_id
            return event_id
        raw = lines[0].strip().lstrip("#").replace(" /", "/").replace("/", " / ")
        words = [w.strip() for w in raw.split() if w.strip()]
        gender_section = ""
        distance = ""
        stroke = ""
        for w in words:
            if "open" in w.lower():
                gender_section = "Open/Male"
                break
            elif "female" in w.lower():
                gender_section = "Female"
                break
        for w in words:
            wl = w.lower()
            if wl.endswith("m") and wl[:-1].isdigit():
                distance = w
                break
        stroke_map = {
            "back": "Backstroke", "br": "Breaststroke", "breast": "Breaststroke",
            "fly": "Butterfly", "butter": "Butterfly", "bu": "Butterfly",
            "free": "Freestyle", "fr": "Freestyle", "im": "Individual Medley"
        }
        for w in words:
            wl = w.lower()
            for key, full in stroke_map.items():
                if wl.startswith(key):
                    stroke = full
                    break
            if stroke:
                break
        result = " ".join(filter(None, [gender_section, distance, stroke])).strip().upper()
        if not result:
            result = raw.upper()
        self.event_cache[event_id] = result
        return result
    
    def _parse_swimmer_data(self, event_id: str) -> List[List[str]]:
        """Parse swimmer data from event file."""
        if not event_id or event_id == 'N/A':
            return []
        if event_id in self.swimmer_cache:
            return self.swimmer_cache[event_id]
        lines = self._read_event_file(event_id)
        if len(lines) < 10:
            self.swimmer_cache[event_id] = []
            return []
        heats = []
        heat_num = 1
        while True:
            start_line = 10 * heat_num - 9
            end_line = start_line + 8
            if start_line >= len(lines):
                break
            heat_swimmers = []
            for i in range(start_line, min(end_line, len(lines))):
                line = lines[i].strip()
                heat_swimmers.append("" if line == "--" else line)
            while len(heat_swimmers) < 8:
                heat_swimmers.append("")
            if any(swimmer.strip() for swimmer in heat_swimmers):
                heats.append(heat_swimmers[:8])
                heat_num += 1
            else:
                break
        self.swimmer_cache[event_id] = heats
        return heats
    
    def _format_swimmer_data(self, raw_data: str) -> Dict[str, str]:
        """Format swimmer data."""
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
                return {"name": f"{forename.strip()} {surname.strip()}", "club": club_code}
            else:
                return {"name": name_part, "club": club_code}
        except Exception:
            return {"name": "", "club": ""}
    
    def _get_swimmers_for_heat(self, event_id: str, heat_num: str) -> Dict[str, Dict[str, str]]:
        """Get swimmers for a specific heat."""
        default = {str(i): {"name": "", "club": ""} for i in range(1, 9)}
        if not event_id or not heat_num or heat_num == 'N/A':
            return default
        try:
            heat_number = int(heat_num)
            if heat_number < 1:
                return default
        except ValueError:
            return default
        all_heats = self._parse_swimmer_data(event_id)
        if not all_heats or heat_number > len(all_heats):
            return default
        heat_swimmers = all_heats[heat_number - 1]
        return {str(i): self._format_swimmer_data(swimmer_data) for i, swimmer_data in enumerate(heat_swimmers, 1)}
        
    def _process_byte(self, byte_in: int) -> None:
        """Process incoming byte from CTS Gen7."""
        if byte_in > self.CONTROL_BYTE_THRESHOLD:
            self.stream_state["data_readout"] = (byte_in & 1) == 0
            self.stream_state["channel"] = ((byte_in >> 1) & 0x1F) ^ 0x1F
            channel = self.stream_state["channel"]
            if channel >= self.CHANNELS:
                return
            if byte_in > self.CLEAR_CHANNEL_THRESHOLD:
                self.display[channel] = [self.SPACE_ASCII] * self.CHARS_PER_CHANNEL
        else:
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
        """Get character from display buffer."""
        if channel >= self.CHANNELS or offset >= self.CHARS_PER_CHANNEL:
            return self.BLANK_CHAR
        ch = self.display[channel][offset]
        return self.BLANK_CHAR if ch in (self.SPACE_ASCII, 63) else chr(ch)
    
    def _get_event_and_heat(self) -> Tuple[str, str]:
        """Extract event and heat information."""
        channel = self.EVENT_HEAT_CHANNEL
        event = ''.join(self._get_char(channel, i) for i in range(3)).strip()
        heat = ''.join(self._get_char(channel, i) for i in range(5, 8)).strip()
        return event, heat
    
    def _get_race_time(self) -> str:
        """Extract race time."""
        channel = self.RACE_TIME_CHANNEL
        time_str = (
            f"{self._get_char(channel, 2)}{self._get_char(channel, 3)}:"
            f"{self._get_char(channel, 4)}{self._get_char(channel, 5)}."
            f"{self._get_char(channel, 6)}{self._get_char(channel, 7)}"
        ).strip()
        return time_str

    def _get_lane_time(self, lane: int) -> tuple:
        """Extract finish time and place for a specific lane (1-8).
        Returns: (time_string, place_string)
        """
        if lane < 1 or lane > 8:
            return ("", "")
        
        channel = 0x01 + (lane - 1)  # Channels 0x01-0x08 map to lanes 1-8
        
        # Extract all 8 characters from the channel
        chars = [self._get_char(channel, i) for i in range(8)]
        
        # Position 0 = Lane number
        # Position 1 = Place
        # Position 2 = Minutes tens
        # Position 3 = Minutes ones
        # Position 4 = Seconds tens
        # Position 5 = Seconds ones
        # Position 6 = Hundredths tens
        # Position 7 = Hundredths ones
        
        place_str = chars[1].strip()
        
        # Extract each digit, replacing spaces/blanks with '0'
        min_tens = chars[2] if chars[2].strip() else '0'
        min_ones = chars[3] if chars[3].strip() else '0'
        sec_tens = chars[4] if chars[4].strip() else '0'
        sec_ones = chars[5] if chars[5].strip() else '0'
        hun_tens = chars[6] if chars[6].strip() else '0'
        hun_ones = chars[7] if chars[7].strip() else '0'
        
        # Build the time components
        minutes = f"{min_tens}{min_ones}"
        seconds = f"{sec_tens}{sec_ones}"
        hundredths = f"{hun_tens}{hun_ones}"
        
        # If seconds are all zeros/spaces, no valid time
        if seconds == "00" and hundredths == "00" and minutes == "00":
            return ("", "")
        
        time_str = f"{minutes}:{seconds}.{hundredths}"
        
        return (time_str, place_str)

    def _is_lane_active(self, lane: int) -> bool:
        """Check if a lane is active (not turned off).
        A lane is ON when position 0 shows the lane number.
        A lane is OFF when position 0 is blank.
        
        Args:
            lane: Lane number 1-8
            
        Returns:
            True if lane is ON, False if turned OFF
        """
        if lane < 1 or lane > 8:
            return False
        
        channel = 0x01 + (lane - 1)  # Channels 0x01-0x08 map to lanes 1-8
        
        # Position 0 shows lane number when ON, blank when OFF
        lane_number_char = self._get_char(channel, 0)
        
        return lane_number_char == str(lane)

    def _check_lane_activity(self) -> None:
        """Check which lanes are active (turned ON on console AND have a swimmer).
        Simple check: if position 0 has the lane number, it's ON.
        Uses debouncing to filter out mid-read glitches.
        """
        current_time = time.time()
        
        # Check every 0.3 seconds
        if current_time - self.last_lane_check_time < 0.3:
            return
        
        self.last_lane_check_time = current_time
        
        current_active = set()
        
        # Check both: lane is ON AND has a swimmer
        for lane in range(1, 9):
            lane_str = str(lane)
            
            # Check if lane is turned ON (position 0 shows lane number)
            is_on = self._is_lane_active(lane)
            
            # Check if lane has a swimmer assigned
            swimmer_info = self.last_swimmers.get(lane_str, {})
            has_swimmer = bool(swimmer_info.get("name", "").strip())
            
            # Lane is only active if BOTH conditions are met
            if is_on and has_swimmer:
                current_active.add(lane)
        
        # Debouncing: require same state 2 times in a row before accepting
        if not hasattr(self, '_pending_active_lanes'):
            self._pending_active_lanes = None
        
        if current_active == self._pending_active_lanes:
            # Same state as last check, this is stable - send it if different from current
            if current_active != self.last_active_lanes:
                lane_activity_data = {
                    "activeLanes": sorted(list(current_active)),
                    "totalActive": len(current_active)
                }
                self._send_websocket_data(lane_activity_data)
                
                self.last_active_lanes = current_active.copy()
                self.active_lanes = current_active
        
        # Update pending state for next check
        self._pending_active_lanes = current_active
    
    def _parse_time_to_seconds(self, time_str: str) -> Optional[float]:
        """Convert time string to seconds."""
        try:
            clean_time = time_str.strip()
            if ":" in clean_time and "." in clean_time:
                parts = clean_time.split(":")
                minutes = int(parts[0].strip()) if parts[0].strip() else 0
                sec_parts = parts[1].split(".")
                seconds = int(sec_parts[0].strip())
                frac_str = sec_parts[1].strip()
                if len(frac_str) == 1:
                    return minutes * 60 + seconds + int(frac_str) / 10.0
                else:
                    return minutes * 60 + seconds + int(frac_str[:2]) / 100.0
            elif "." in clean_time:
                parts = clean_time.split(".")
                seconds = int(parts[0].strip())
                frac_str = parts[1].strip()
                if len(frac_str) == 1:
                    return seconds + int(frac_str) / 10.0
                else:
                    return seconds + int(frac_str[:2]) / 100.0
        except (ValueError, IndexError):
            pass
        return None

    def _is_race_data_complete(self) -> bool:
        """Checks if all active lanes have recorded their final expected time."""
        # If no lanes are active or we expect 0 times (like a DQ race), return False
        if not self.active_lanes or self.expected_times_per_lane == 0:
            return False
        
        for lane in self.active_lanes:
            lane_str = str(lane)
            
            # Check if the lane has received the required number of times
            if self.lane_time_counts.get(lane_str, 0) < self.expected_times_per_lane:
                return False
        
        # All active lanes have received their final expected time
        return True
    
    def _send_websocket_data(self, data: Dict) -> None:
        """Queue data to be sent to WebSocket clients."""
        if self.running:
            self.data_queue.put(data)
    
    def _handle_event_change(self, event: str, heat: str) -> None:
        """Handle event/heat changes."""
        data_to_send = {}
        if event != self.last_event:
            event_name = self._get_event_name(event)
            self.last_event_name = event_name
            data_to_send["eventName"] = event_name.upper()
            display_heat = f"HEAT {heat}" if heat else 'N/A'
            data_to_send["heatName"] = display_heat
            swimmers = self._get_swimmers_for_heat(event, heat)
            self.last_swimmers = swimmers
            data_to_send["lanes"] = swimmers
            self.last_finish_times = {}
            # Calculate expected times per lane based on distance
            distance = self._extract_distance_from_event_name(event_name)
            if distance:
                self.expected_times_per_lane = distance // 50
            else:
                self.expected_times_per_lane = 1  # Default to finish only

            # Reset lane time counters
            self.lane_time_counts = {str(i): 0 for i in range(1, 9)}
            # Reset saved state for new event
            self._saved_sent_for_heat = False
            self._dq_sent_for_heat = set()

            print(f"[{time.strftime('%H:%M:%S')}] Event: {event_name} | Heat: {heat}")
            print(f"[INFO] Distance: {distance}m - Expecting {self.expected_times_per_lane} time(s) per lane")
        elif heat != self.last_heat:
            display_heat = f"HEAT {heat}" if heat else 'N/A'
            data_to_send["heatName"] = display_heat
            swimmers = self._get_swimmers_for_heat(event, heat)
            self.last_swimmers = swimmers
            data_to_send["lanes"] = swimmers
            self.last_finish_times = {}
            self.lane_time_counts = {str(i): 0 for i in range(1, 9)}

            self._saved_sent_for_heat = False
            self._dq_sent_for_heat = set()
            
            print(f"[{time.strftime('%H:%M:%S')}] Heat changed: {display_heat}")
        
        if data_to_send:
            self._send_websocket_data(data_to_send)
        
        self.last_event, self.last_heat = event, heat
    
    def _handle_time_update(self, race_time: str) -> None:
        """Handle race time updates with improved accuracy."""
        current_seconds = self._parse_time_to_seconds(race_time)
        cleaned = race_time.replace(" ", "").replace(":", "").replace(".", "")
        is_empty = cleaned == "" or not any(c.isdigit() and c != '0' for c in cleaned)
        
        if is_empty or current_seconds is None or current_seconds <= 0.09:
            if self.timer_running:
                self.timer_running = False

                if not self._saved_sent_for_heat and self._is_race_data_complete():
                    self._send_websocket_data({"status": "SAVED"})
                    self._saved_sent_for_heat = True
                    print(f"[{time.strftime('%H:%M:%S')}] Race data complete and timer stopped - sending SAVED status.")
                
                self._send_websocket_data({"timerSync": {"running": False, "time": 0.0, "timestamp": time.time()}})
                print(f"[{time.strftime('%H:%M:%S')}] Timer stopped")
        else:
            # Apply latency compensation to get closer to real timer
            compensated_time = current_seconds + self.TIMER_LATENCY_COMPENSATION
            
            if not self.timer_running:
                self.timer_running = True
                self.timer_start_time = time.time()
                self.timer_offset = compensated_time
                # Send timer sync AND active lanes when timer starts
                self._send_websocket_data({
                    "timerSync": {"running": True, "time": compensated_time, "timestamp": time.time()},
                    "activeLanes": sorted(list(self.active_lanes)),
                    "totalActive": len(self.active_lanes)
                })
                print(f"[{time.strftime('%H:%M:%S')}] Timer started: {race_time}")
            else:
                # More frequent syncs for better accuracy
                if time.time() - self._last_sync_time >= self.TIMER_SYNC_INTERVAL:
                    self._send_websocket_data({"timerSync": {"running": True, "time": compensated_time, "timestamp": time.time()}})
                    self._last_sync_time = time.time()
        
        self.last_race_time = race_time

    def _handle_finish_times(self) -> None:
        """Check and handle finish time updates for all lanes."""

        if not self.timer_running:
            return
        
        for lane in range(1, 9):
            lane_time, place = self._get_lane_time(lane)
            lane_str = str(lane)

            # Check for a DQ flag (often placed in the 'place' slot as a non-digit character)
            is_dq = bool(place.strip()) and not place.strip().isdigit()
            
            if is_dq:
                
                # NEW LOGIC START: Check if DQ for this lane has already been sent
                if lane in self._dq_sent_for_heat:
                    self.last_finish_times[lane_str] = "DQ" # Still mark as DQ to prevent time processing
                    continue
                
                if self.timer_running and self.timer_start_time is not None:
                    elapsed_time = time.time() - self.timer_start_time
                    if elapsed_time < self.DQ_STALE_TIMEOUT:
                        # Log the filtering action and skip sending
                        swimmer_info = self.last_swimmers.get(lane_str, {})
                        swimmer_name = swimmer_info.get("name", f"Lane {lane}")
                        
                        # We still set last_finish_times to DQ to avoid processing as a time later
                        self.last_finish_times[lane_str] = "DQ"
                        continue 
                # --- END DQ TIMEOUT CHECK ---
                
                # Get swimmer info for console log
                swimmer_info = self.last_swimmers.get(lane_str, {})
                swimmer_name = swimmer_info.get("name", f"Lane {lane}")
                
                # Log and mark as sent
                print(f"[{time.strftime('%H:%M:%S')}] --- DQ FLAG SENT --- Lane: {lane} | Swimmer: {swimmer_name} | Code: {place.strip()}")
                self._dq_sent_for_heat.add(lane)
                
                # Send the simplified WebSocket message
                self._send_websocket_data({
                    "disqualification": {
                        "lane": lane,
                        "tag": "disqualification"
                    }
                })
                
                # Mark as DQ to prevent future time processing for this lane
                self.last_finish_times[lane_str] = "DQ" 
                continue # Skip the rest of the time processing loop
            # --- END NEW BLOCK ---
            
            # Skip if empty
            if not lane_time:
                continue

            # Ignore times in the first second of race - these are old times from previous race
            if self.timer_running:
                current_race_time = self._parse_time_to_seconds(self.last_race_time)
                if current_race_time and current_race_time < 1.0:
                    continue
                    
            # Clean the time string (remove spaces)
            time_cleaned = lane_time.replace(" ", "")
            
            # Skip if same as last time
            if time_cleaned == self.last_finish_times.get(lane_str, ""):
                continue
            
            # Validate: must have colon and decimal point
            if ':' not in time_cleaned or '.' not in time_cleaned:
                continue
            
            # Check if it has actual digit content (not all zeros/spaces)
            digits_only = time_cleaned.replace(":", "").replace(".", "")
            if not any(c.isdigit() and c != '0' for c in digits_only):
                continue
            
            # Must have at least 3 meaningful digits
            meaningful_digits = [c for c in digits_only if c.isdigit() and c != '0']
            if len(meaningful_digits) < 2:
                continue
            
            try:
                # Parse and format properly
                parts = time_cleaned.split(':')
                if len(parts) != 2:
                    continue
                
                seconds_part = parts[1]
                if '.' not in seconds_part:
                    continue
                
                sec_split = seconds_part.split('.')
                if len(sec_split) != 2:
                    continue
                
                # Build formatted time with zero-padding
                minutes = parts[0].strip() if parts[0].strip() and parts[0].strip().isdigit() else '00'
                seconds = sec_split[0].strip() if sec_split[0].strip() else '00'
                hundredths = sec_split[1].strip() if sec_split[1].strip() else '00'
                
                # Pad to 2 digits
                minutes = minutes.zfill(2)
                seconds = seconds.zfill(2)
                hundredths = hundredths.ljust(2, '0')[:2]
                
                formatted_time = f"{minutes}:{seconds}.{hundredths}"
                
            except (ValueError, IndexError) as e:
                continue
            
            # Get swimmer info
            swimmer_info = self.last_swimmers.get(lane_str, {})
            swimmer_name = swimmer_info.get("name", "")
            
            # Skip if no swimmer in this lane
            if not swimmer_name:
                continue
            
            # Store the finish time
            self.last_finish_times[lane_str] = time_cleaned

            # Increment time count for this lane
            self.lane_time_counts[lane_str] += 1
            time_number = self.lane_time_counts[lane_str]

            # Determine if this is a split or finish time
            if time_number < self.expected_times_per_lane:
                time_type = "SPLIT"
                time_label = f"Split {time_number}"
            else:
                time_type = "FINISH"
                time_label = "Finish"

            # Format place display (show for both splits and finishes)
            place_display = f", {place}" if place and place.isdigit() else ""

            # Send to WebSocket clients
            finish_data = {
                "finishTime": {
                    "lane": lane,
                    "time": formatted_time,
                    "place": place if place and place.isdigit() else "",
                    "swimmer": swimmer_name,
                    "type": time_type,
                    "timeNumber": time_number,
                    "label": time_label
                }
            }
            self._send_websocket_data(finish_data)

    
    # COM Port File Receiver Methods
    def _run_com_receiver(self) -> None:
        """Run COM port file receiver in separate thread."""
        print(f"[OK] COM receiver started on {self.receiver_port}")
        
        while self.running:
            try:
                # Read data with non-blocking timeout
                data = self.receiver_serial.read(4096)
                if data:
                    # Decode and add to line buffer
                    try:
                        decoded = data.decode('utf-8', errors='ignore')
                        self.com_line_buffer += decoded
                        
                        # Process complete lines
                        while '\n' in self.com_line_buffer:
                            line, self.com_line_buffer = self.com_line_buffer.split('\n', 1)
                            if line.strip():
                                self._process_com_packet(line.strip())
                    except Exception as e:
                        print(f"[ERROR] Decode error: {e}")
                else:
                    # Small sleep when no data
                    time.sleep(0.001)
                    
            except Exception as e:
                if self.running:
                    print(f"[ERROR] COM receiver error: {e}")
                time.sleep(0.1)
    
    def _process_com_packet(self, packet_str: str) -> None:
        """Process incoming COM packet."""
        try:
            if not packet_str:
                return
            
            header_json = json.loads(packet_str)
            name = header_json.get("name")
            seq = header_json.get("seq")
            final = header_json.get("final", False)
            size = header_json.get("size", 0)
            content_b64 = header_json.get("content", "")
            
            if not name:
                return
            
            key = name
            if key not in self.com_buffers:
                self.com_buffers[key] = {"parts": {}, "final": False, "size": size}
            
            buf = self.com_buffers[key]
            
            if not final and content_b64:
                try:
                    rawbytes = base64.b64decode(content_b64)
                    buf["parts"][seq] = rawbytes
                    print(f"[RX] Received chunk {seq} for {name} ({len(rawbytes)} bytes)")
                except Exception as e:
                    print(f"[ERROR] Base64 decode error: {e}")
                    return
            elif final:
                buf["final"] = True
                print(f"[OK] Final marker received for {name}")
            
            # If complete, save file immediately
            if buf["final"] and buf["parts"]:
                self._save_complete_file(name, buf)
                
        except json.JSONDecodeError as e:
            print(f"[ERROR] JSON decode error: {e}")
        except Exception as e:
            print(f"[ERROR] Error processing COM packet: {e}")
    
    def _save_complete_file(self, name: str, buf: Dict) -> None:
        """Save a complete file."""
        try:
            indexes = sorted(buf["parts"].keys())
            outp = self.event_files_path / name
            
            with open(outp, "wb") as f:
                for i in indexes:
                    f.write(buf["parts"][i])
            
            total_bytes = sum(len(buf["parts"][i]) for i in indexes)
            print(f"[SAVED] {name} ({total_bytes} bytes, {len(indexes)} chunks)")
            
            # Clear cache for this event if it's an event file
            if name.startswith("E") and name.endswith(".scb"):
                event_id = name[1:-4]
                self.event_cache.pop(event_id, None)
                self.swimmer_cache.pop(event_id, None)
            
            # Remove from buffer
            del self.com_buffers[name]
            
        except Exception as e:
            print(f"[ERROR] Error saving file {name}: {e}")
    
    # WebSocket Server Methods
    async def _websocket_handler(self, websocket):
        """Handle individual WebSocket connections."""
        self.websocket_clients.add(websocket)
        print(f"[WS] Client connected (total: {len(self.websocket_clients)})")
        
        try:
            initial_data = {
                "eventName": (self.last_event_name or 'N/A').upper(),
                "heatName": f"HEAT {self.last_heat}" if self.last_heat else 'N/A',
                "lanes": self.last_swimmers if self.last_swimmers else {str(i): {"name": "", "club": ""} for i in range(1, 9)},
                "timerSync": {
                    "running": self.timer_running,
                    "time": self._parse_time_to_seconds(self.last_race_time) or 0.0,
                    "timestamp": time.time()
                },
                "activeLanes": sorted(list(self.active_lanes)),
                "totalActive": len(self.active_lanes)
            }
            await websocket.send(json.dumps(initial_data))
            
            # Keep connection alive
            await websocket.wait_closed()
            
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            print(f"[ERROR] WebSocket handler error: {e}")
        finally:
            self.websocket_clients.discard(websocket)
            print(f"[WS] Client disconnected (remaining: {len(self.websocket_clients)})")
    
    async def _websocket_broadcaster(self):
        """Broadcast data to all WebSocket clients."""
        while self.running:
            try:
                if not self.data_queue.empty():
                    data = self.data_queue.get()
                    message = json.dumps(data)
                    
                    if self.websocket_clients:
                        disconnected_clients = set()
                        for client in list(self.websocket_clients):
                            try:
                                await client.send(message)
                            except websockets.exceptions.ConnectionClosed:
                                disconnected_clients.add(client)
                            except Exception as e:
                                print(f"[ERROR] Error sending to client: {e}")
                                disconnected_clients.add(client)
                        
                        self.websocket_clients -= disconnected_clients
                
                await asyncio.sleep(0.01)
            except Exception as e:
                print(f"[ERROR] Broadcaster error: {e}")
                await asyncio.sleep(0.1)
    
    def _run_websocket_server(self):
        """Run WebSocket server."""
        async def start_server():
            try:
                server = await websockets.serve(
                    self._websocket_handler,
                    "localhost",
                    self.WEBSOCKET_PORT,
                    ping_interval=20,
                    ping_timeout=10
                )
                print(f"[WS] WebSocket server started on ws://localhost:{self.WEBSOCKET_PORT}")
                
                broadcaster_task = asyncio.create_task(self._websocket_broadcaster())
                
                try:
                    await asyncio.Future()  # Run forever
                except asyncio.CancelledError:
                    pass
                finally:
                    broadcaster_task.cancel()
                    try:
                        await broadcaster_task
                    except asyncio.CancelledError:
                        pass
                    server.close()
                    await server.wait_closed()
                    
            except Exception as e:
                print(f"[ERROR] WebSocket server error: {e}")
        
        try:
            asyncio.run(start_server())
        except Exception as e:
            print(f"[ERROR] Failed to start WebSocket server: {e}")
    
    def run(self) -> None:
        """Main execution loop."""
        self.running = True
        
        # Start COM receiver thread
        com_thread = threading.Thread(target=self._run_com_receiver, daemon=True)
        com_thread.start()
        
        # Start WebSocket server thread
        websocket_thread = threading.Thread(target=self._run_websocket_server, daemon=True)
        websocket_thread.start()
        
        # Give threads time to start
        time.sleep(0.5)
        
        print("\n" + "="*60)
        print("SWIM LIVE SYSTEM STARTED")
        print("="*60)
        print(f"[DIR] Base directory: {self.base_dir}")
        print(f"[DIR] Event files: {self.event_files_path}")
        print(f"[DIR] HTML files: {self.html_dir}")
        print(f"[CTS] CTS Port: {self.cts_port} @ {self.baud} baud")
        print(f"[COM] Receiver Port: {self.receiver_port} @ 115200 baud")
        print(f"[WS] WebSocket: ws://localhost:{self.WEBSOCKET_PORT}")
        print("="*60)
        print("\n[OK] System ready - monitoring CTS Gen7 and file receiver...")
        print("="*60 + "\n")
        
        try:
            buffer = bytearray()
            
            # Get initial state
            initial_event, initial_heat = self._get_event_and_heat()
            initial_race_time = self._get_race_time()
            
            if initial_event or initial_heat:
                self._handle_event_change(initial_event, initial_heat)
                # After handling event change, expected_times_per_lane is set
                print(f"[INIT] Loaded existing event - expecting {self.expected_times_per_lane} time(s) per lane")
                if initial_race_time:
                    self._handle_time_update(initial_race_time)
            
            # Main loop - continuous polling without blocking
            while self.running:
                # Read CTS data
                data = self.cts_serial.read(256)
                if data:
                    buffer.extend(data)
                    for byte_val in buffer:
                        self._process_byte(byte_val)
                    buffer.clear()
                
                # Check for changes
                event, heat = self._get_event_and_heat()
                race_time = self._get_race_time()
                
                if event != self.last_event or heat != self.last_heat:
                    self._handle_event_change(event, heat)
                
                if race_time != self.last_race_time:
                    self._handle_time_update(race_time)

                self._handle_finish_times()

                self._check_lane_activity()
                
                # Very small sleep to prevent CPU spinning while remaining responsive
                time.sleep(0.001)
        
        except KeyboardInterrupt:
            print("\n[STOP] Stopping system...")
        except Exception as e:
            print(f"\n[ERROR] Error in main loop: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.close()
    
    def close(self) -> None:
        """Clean up resources."""
        print("\n[SHUTDOWN] Shutting down...")
        self.running = False
        
        # Close serial connections
        try:
            if hasattr(self, 'cts_serial') and self.cts_serial.is_open:
                self.cts_serial.close()
                print("[OK] CTS serial port closed")
        except:
            pass
        
        try:
            if hasattr(self, 'receiver_serial') and self.receiver_serial.is_open:
                self.receiver_serial.close()
                print("[OK] Receiver serial port closed")
        except:
            pass
        
        # Close WebSocket connections
        for client in list(self.websocket_clients):
            try:
                asyncio.run(client.close())
            except:
                pass
        
        print("[OK] Swim Live System stopped.")


class COMPortSelector:
    """GUI for selecting COM ports."""
    
    def __init__(self):
        self.selected_cts_port = None
        self.selected_receiver_port = None
        self.root = tk.Tk()
        self.root.title("Swim Live - Select COM Ports")
        self.root.geometry("500x400")
        self.root.resizable(False, False)
        self.root.attributes('-topmost', True)
        self.root.after(100, lambda: self.root.attributes('-topmost', False))
        
        # Center window
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() // 2) - (250)
        y = (self.root.winfo_screenheight() // 2) - (200)
        self.root.geometry(f"500x400+{x}+{y}")
        self.root.focus_force()
        self._create_widgets()
        
    def _create_widgets(self):
        """Create GUI widgets."""
        # Title
        title_frame = tk.Frame(self.root, bg="#42008f", height=70)
        title_frame.pack(fill=tk.X)
        title_frame.pack_propagate(False)
        
        title_label = tk.Label(
            title_frame,
            text="Swim Live System",
            font=("Helvetica", 20, "bold"),
            bg="#42008f",
            fg="white"
        )
        title_label.pack(expand=True)
        
        # Content frame
        content_frame = tk.Frame(self.root, padx=30, pady=20)
        content_frame.pack(fill=tk.BOTH, expand=True)
        
        # CTS Port selection
        cts_label = tk.Label(
            content_frame,
            text="CTS Gen7 COM Port:",
            font=("Helvetica", 12, "bold")
        )
        cts_label.pack(pady=(10, 5), anchor=tk.W)
        
        self.cts_port_var = tk.StringVar()
        ports = self._get_available_ports()
        
        if not ports:
            error_label = tk.Label(
                content_frame,
                text="[ERROR] No COM ports detected!",
                font=("Helvetica", 11),
                fg="red"
            )
            error_label.pack(pady=20)
            
            retry_btn = tk.Button(
                content_frame,
                text="Refresh Ports",
                command=self._refresh_ports,
                font=("Helvetica", 11),
                bg="#4CAF50",
                fg="white",
                padx=20,
                pady=8
            )
            retry_btn.pack()
        else:
            self.cts_port_var.set(ports[0] if ports else "")
            cts_dropdown = ttk.Combobox(
                content_frame,
                textvariable=self.cts_port_var,
                values=ports,
                state="readonly",
                font=("Helvetica", 11),
                width=35
            )
            cts_dropdown.pack(pady=(0, 20))
            
            # Receiver Port selection
            receiver_label = tk.Label(
                content_frame,
                text="File Receiver COM Port:",
                font=("Helvetica", 12, "bold")
            )
            receiver_label.pack(pady=(0, 5), anchor=tk.W)
            
            self.receiver_port_var = tk.StringVar()
            receiver_ports = [p for p in ports if p != self.cts_port_var.get()]
            if receiver_ports:
                self.receiver_port_var.set(receiver_ports[0])
            
            receiver_dropdown = ttk.Combobox(
                content_frame,
                textvariable=self.receiver_port_var,
                values=ports,
                state="readonly",
                font=("Helvetica", 11),
                width=35
            )
            receiver_dropdown.pack(pady=(0, 10))
            
            # Info label
            info_label = tk.Label(
                content_frame,
                text="[INFO] Select different ports for CTS and File Receiver",
                font=("Helvetica", 9),
                fg="gray"
            )
            info_label.pack(pady=(0, 20))
            
            # Buttons
            button_frame = tk.Frame(content_frame)
            button_frame.pack(pady=20)
            
            start_btn = tk.Button(
                button_frame,
                text="Start System",
                command=self._on_start,
                font=("Helvetica", 12, "bold"),
                bg="#4CAF50",
                fg="white",
                padx=30,
                pady=12,
                cursor="hand2"
            )
            start_btn.pack(side=tk.LEFT, padx=5)
            
            refresh_btn = tk.Button(
                button_frame,
                text="Refresh",
                command=self._refresh_ports,
                font=("Helvetica", 11),
                bg="#666",
                fg="white",
                padx=20,
                pady=12,
                cursor="hand2"
            )
            refresh_btn.pack(side=tk.LEFT, padx=5)
        
        # Footer
        footer_label = tk.Label(
            content_frame,
            text="Connect CTS Gen7 and ensure COM ports are available",
            font=("Helvetica", 9),
            fg="gray"
        )
        footer_label.pack(side=tk.BOTTOM, pady=(10, 0))
    
    def _get_available_ports(self) -> List[str]:
        """Get list of available COM ports."""
        ports = serial.tools.list_ports.comports()
        return [port.device for port in ports]
    
    def _refresh_ports(self):
        """Refresh the port list."""
        for widget in self.root.winfo_children():
            widget.destroy()
        self._create_widgets()
    
    def _on_start(self):
        """Handle start button click."""
        self.selected_cts_port = self.cts_port_var.get()
        self.selected_receiver_port = self.receiver_port_var.get()
        
        if self.selected_cts_port == self.selected_receiver_port:
            tk.messagebox.showerror(
                "Error",
                "CTS Port and Receiver Port must be different!"
            )
            return
        
        self.root.destroy()
    
    def show(self) -> Tuple[Optional[str], Optional[str]]:
        """Show the dialog and return selected ports."""
        self.root.mainloop()
        return self.selected_cts_port, self.selected_receiver_port


def main():
    """Main entry point."""
    selector = COMPortSelector()
    cts_port, receiver_port = selector.show()
    
    if not cts_port or not receiver_port:
        print("[ERROR] COM ports not selected. Exiting.")
        return
    
    try:
        system = SwimLiveSystem(
            cts_port=cts_port,
            receiver_port=receiver_port,
            baud=9600
        )
        system.run()
    except serial.SerialException as e:
        print(f"\n[ERROR] Serial port error: {e}")
        print("Ensure devices are connected and ports are not in use.")
    except Exception as e:
        print(f"\n[ERROR] Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
