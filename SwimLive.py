import serial
import serial.tools.list_ports
import time
import json
import requests
import asyncio
import websockets
import threading
from typing import Dict, List, Tuple, Optional
from pathlib import Path
from queue import Queue
import tkinter as tk
from tkinter import ttk
from obswebsocket import obsws, requests
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

    
    def __init__(self, cts_port: str, receiver_port: str, baud: int = 9600, test_mode: bool = False):
        # Base directory setup
        self.base_dir = Path.home() / "Documents" / "Swim Live"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        
        self.event_files_path = self.base_dir / "Events"
        self.event_files_path.mkdir(parents=True, exist_ok=True)
        
        self.html_dir = self.base_dir

        self.test_mode = test_mode
        
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
        self._last_scene_check = 0
        
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

        # OBS WebSocket setup
        self.obs_host = "localhost"
        self.obs_port = 4455  # Default OBS WebSocket port
        self.obs_password = "123456"  # <-- Set this to your OBS password

        try:
            self.obs = obsws(self.obs_host, self.obs_port, self.obs_password)
            self.obs.connect()
            print("[OBS] Connected successfully")
        except Exception as e:
            self.obs = None
            print(f"[OBS] Connection to OBS failed: {e}")

        
    def _setup_serial(self) -> None:
        """Initialize serial connections."""
        if self.test_mode:
            print("[TEST MODE] Skipping serial port initialization")
            self.cts_serial = None
            self.receiver_serial = None
            return
        
        # CTS Gen7 connection
        self.cts_serial = serial.Serial(
            port=self.cts_port,
            baudrate=self.baud,
            bytesize=serial.EIGHTBITS,
            parity=self.parity,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.001,
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
            timeout=0.001,
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
			/* Adjusted Perspective to match high/oblique angle */
			perspective: 1500px; 
			perspective-origin: 50% 35%;
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
			/* This wrapper helps position the whole group */
			transform: translateY(-200px); 
		}

		/* Each lane cube wrapper */
        .lane-cube {
			transform-style: preserve-3d;
			position: absolute; /* Changed to absolute to stack them and allow independent positioning */
			opacity: 0;
			transition: transform 0.3s ease;
			/* Set common defaults */
			top: 0;
			left: 0;
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

		/* Control panel styles (KEEP) */
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
		<h2>ðŸŽ² Cube Projection System</h2>
		
		<div class="instructions">
			<strong>Per-Lane Fine-Tuning:</strong>
			Select an individual lane (L1-L8) to adjust its position and rotation. The **Translate Z** slider has a greatly increased range to handle severe perspective changes.
		</div>

		<div class="control-section">
			<h3>Camera Perspective (Global)</h3>
			
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
					<span class="value-display" id="perspectiveYValue">35%</span>
				</label>
				<input type="range" id="perspectiveY" min="0" max="100" step="1" value="35">
			</div>
		</div>

		<div class="control-section">
			<h3>Select Lane to Adjust (Individual)</h3>
			<div class="lane-select">
				<button onclick="selectLane('all')" id="btnAll">ALL</button>
				<button onclick="selectLane(1)" id="btn1">L1</button>
				<button onclick="selectLane(2)" id="btn2">L2</button>
				<button onclick="selectLane(3)" id="btn3">L3</button>
				<button onclick="selectLane(4)" id="btn4" class="active">L4</button>
				<button onclick="selectLane(5)" id="btn5">L5</button>
				<button onclick="selectLane(6)" id="btn6">L6</button>
				<button onclick="selectLane(7)" id="btn7">L7</button>
				<button onclick="selectLane(8)" id="btn8">L8</button>
			</div>

			<span class="all-lanes-label" id="controlLabel">Adjusting: LANE 4</span>

			<div class="control-group">
				<label>
					<span>Rotate X (Tilt)</span>
					<span class="value-display" id="rotateXValue">68Â°</span>
				</label>
				<input type="range" id="rotateX" min="0" max="90" step="0.5" value="68">
			</div>

			<div class="control-group">
				<label>
					<span>Rotate Y (Turn/Skew)</span>
					<span class="value-display" id="rotateYValue">0Â°</span>
				</label>
				<input type="range" id="rotateY" min="-45" max="45" step="0.5" value="0">
			</div>

			<div class="control-group">
				<label>
					<span>Rotate Z (Roll)</span>
					<span class="value-display" id="rotateZValue">0Â°</span>
				</label>
				<input type="range" id="rotateZ" min="-15" max="15" step="0.1" value="0">
			</div>

			<div class="control-group">
				<label>
					<span>Translate X (Left/Right)</span>
					<span class="value-display" id="translateXValue">0px</span>
				</label>
				<input type="range" id="translateX" min="-1000" max="1000" step="5" value="0">
			</div>

			<div class="control-group">
				<label>
					<span>Translate Y (Up/Down)</span>
					<span class="value-display" id="translateYValue">0px</span>
				</label>
				<input type="range" id="translateY" min="-1000" max="1000" step="5" value="0">
			</div>

			<div class="control-group">
				<label>
					<span>Translate Z (Depth/Scale)</span>
					<span class="value-display" id="translateZValue">0px</span>
				</label>
				<input type="range" id="translateZ" min="-2000" max="1000" step="10" value="0">
			</div>
		</div>

		<div class="control-section">
			<h3>Quick Presets</h3>
			<div class="preset-buttons">
				<button onclick="applyPreset('calibrated')">Calibrated (Current Image)</button>
				<button onclick="applyPreset('flat')">Flat View</button>
				<button onclick="applyPreset('broadcast')">Broadcast</button>
				<button onclick="applyPreset('olympic')">Olympic</button>
			</div>
		</div>

		<div class="button-group">
			<button onclick="resetSelected()" class="secondary">Reset Selected</button>
			<button onclick="copySettings()">Copy Values</button>
		</div>

		<div style="margin-top: 12px; padding: 12px; background: rgba(0, 217, 255, 0.2); border-radius: 8px; text-align: center; color: #00D9FF; font-size: 12px;">
			Each lane = bottom of cube â€¢ Rotate cube to project
		</div>
	</div>

	<script>
	// Club name scaling (KEEP)
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
	// Name font size adjustment (KEEP)
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
	// Animation timing (KEEP)
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
		// --- Custom Calibration Data for Your Image ---
		const CALIBRATED_SETTINGS = {
			perspective: 1500,
			perspectiveX: 50,
			perspectiveY: 35, 
			lanes: {
				// Base Rotation (Rotate X) is ~68 deg
				// Trans Y/Z is adjusted to create convergence towards lane 1 and separation between lanes.
				1: { rotateX: 69.5, rotateY: 3.5, rotateZ: 0.1, translateX: -120, translateY: -250, translateZ: 350 }, // Furthest away, most skewed/foreshortened
				2: { rotateX: 69, rotateY: 1.5, rotateZ: 0.1, translateX: -70, translateY: -170, translateZ: 250 },
				3: { rotateX: 68.5, rotateY: 0.5, rotateZ: 0.0, translateX: -20, translateY: -80, translateZ: 120 },
				4: { rotateX: 68, rotateY: 0, rotateZ: 0, translateX: 0, translateY: 0, translateZ: 0 }, // Center/Reference Lane
				5: { rotateX: 68, rotateY: 0, rotateZ: 0, translateX: 0, translateY: 100, translateZ: -100 },
				6: { rotateX: 67.5, rotateY: -0.5, rotateZ: 0.0, translateX: 20, translateY: 220, translateZ: -240 },
				7: { rotateX: 67, rotateY: -1.5, rotateZ: -0.1, translateX: 70, translateY: 360, translateZ: -380 },
				8: { rotateX: 66.5, rotateY: -3.5, rotateZ: -0.1, translateX: 130, translateY: 500, translateZ: -500 } // Closest, least foreshortened
			}
		};

		const BROADCAST_SETTINGS = { // Standard/Flatter preset
			perspective: 1500,
			perspectiveX: 50,
			perspectiveY: 50,
			lanes: {
				1: { rotateX: 60, rotateY: 0, rotateZ: 0, translateX: 0, translateY: -360, translateZ: 250 },
				2: { rotateX: 60, rotateY: 0, rotateZ: 0, translateX: 0, translateY: -240, translateZ: 150 },
				3: { rotateX: 60, rotateY: 0, rotateZ: 0, translateX: 0, translateY: -120, translateZ: 50 },
				4: { rotateX: 60, rotateY: 0, rotateZ: 0, translateX: 0, translateY: 0, translateZ: -50 },
				5: { rotateX: 60, rotateY: 0, rotateZ: 0, translateX: 0, translateY: 120, translateZ: -150 },
				6: { rotateX: 60, rotateY: 0, rotateZ: 0, translateX: 0, translateY: 240, translateZ: -250 },
				7: { rotateX: 60, rotateY: 0, rotateZ: 0, translateX: 0, translateY: 360, translateZ: -350 },
				8: { rotateX: 60, rotateY: 0, rotateZ: 0, translateX: 0, translateY: 480, translateZ: -450 }
			}
		};
		// --- End Calibration Data ---


		let cubeSettings = JSON.parse(JSON.stringify(CALIBRATED_SETTINGS)); // Start with calibrated
		let selectedLane = 4; // Start with Lane 4 selected
		let isInteracting = false;
		let interactTimeout = null;

		document.addEventListener("DOMContentLoaded", () => {
			setupControls();
			applyAllCubeTransforms();
			detectInteractMode();
			selectLane(4); // Start with Lane 4 selected for fine-tuning
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
				document.getElementById('controlLabel').textContent = 'Adjusting: ALL LANES (WARNING: Edits apply uniformly)';
			} else {
				document.getElementById('btn' + lane).classList.add('active');
				document.getElementById('controlLabel').textContent = 'Adjusting: LANE ' + lane;
			}
			
			// Update sliders to show current values
			updateSlidersForSelection();
		}

		function updateSlidersForSelection() {
			const referenceLane = (selectedLane === 'all' || selectedLane === null) ? 4 : selectedLane; // Use lane 4 as a central reference for ALL
			
			// Apply perspective settings globally
			document.body.style.perspective = cubeSettings.perspective + 'px';
			document.body.style.perspectiveOrigin = `${cubeSettings.perspectiveX}% ${cubeSettings.perspectiveY}%`;

			// Update perspective controls directly from cubeSettings
			document.getElementById('perspective').value = cubeSettings.perspective;
			document.getElementById('perspectiveX').value = cubeSettings.perspectiveX;
			document.getElementById('perspectiveY').value = cubeSettings.perspectiveY;

			// Update transform controls from selected/reference lane
			const settings = cubeSettings.lanes[referenceLane];
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
			document.getElementById('rotateXValue').textContent = document.getElementById('rotateX').value + 'Â°';
			document.getElementById('rotateYValue').textContent = document.getElementById('rotateY').value + 'Â°';
			document.getElementById('rotateZValue').textContent = document.getElementById('rotateZ').value + 'Â°';
			document.getElementById('translateXValue').textContent = document.getElementById('translateX').value + 'px';
			document.getElementById('translateYValue').textContent = document.getElementById('translateY').value + 'px';
			document.getElementById('translateZValue').textContent = document.getElementById('translateZ').value + 'px';
		}

		function setupControls() {
			// Perspective controls
			document.getElementById('perspective').addEventListener('input', (e) => {
				cubeSettings.perspective = parseFloat(e.target.value);
				updateSlidersForSelection(); // Applies new global settings
				applyAllCubeTransforms();
			});

			document.getElementById('perspectiveX').addEventListener('input', (e) => {
				cubeSettings.perspectiveX = parseFloat(e.target.value);
				updateSlidersForSelection(); 
				applyAllCubeTransforms();
			});

			document.getElementById('perspectiveY').addEventListener('input', (e) => {
				cubeSettings.perspectiveY = parseFloat(e.target.value);
				updateSlidersForSelection(); 
				applyAllCubeTransforms();
			});

			// Cube transform controls
			const transformControls = ['rotateX', 'rotateY', 'rotateZ', 'translateX', 'translateY', 'translateZ'];
			transformControls.forEach(control => {
				document.getElementById(control).addEventListener('input', (e) => {
					const value = parseFloat(e.target.value);
					const prop = control;

					if (selectedLane === 'all') {
						// Apply uniform change to all lanes
						for (let i = 1; i <= 8; i++) {
							cubeSettings.lanes[i][prop] = value;
						}
					} else {
						// Apply change only to the selected lane
						cubeSettings.lanes[selectedLane][prop] = value;
					}
					applyAllCubeTransforms();
					updateValueDisplays();
				});
			});

			// Initialize displays
			updateSlidersForSelection();
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
			let newSettings = {};
			if (preset === 'calibrated') {
				newSettings = JSON.parse(JSON.stringify(CALIBRATED_SETTINGS));
			} else if (preset === 'broadcast') {
				newSettings = JSON.parse(JSON.stringify(BROADCAST_SETTINGS));
			} else if (preset === 'olympic') {
				// Olympic Preset (similar to broadcast but slightly steeper)
				newSettings = JSON.parse(JSON.stringify(BROADCAST_SETTINGS));
				newSettings.perspective = 1800;
				newSettings.perspectiveY = 40;
				for (let i = 1; i <= 8; i++) {
					newSettings.lanes[i].rotateX = 65;
					newSettings.lanes[i].translateZ -= 50;
				}
			} else if (preset === 'flat') {
				// Flat View - no perspective changes
				newSettings.perspective = 10000;
				newSettings.perspectiveX = 50;
				newSettings.perspectiveY = 50;
				for (let i = 1; i <= 8; i++) {
					newSettings.lanes[i] = {
						rotateX: 0,
						rotateY: 0,
						rotateZ: 0,
						translateX: (i - 4.5) * 400, // Spread lanes horizontally
						translateY: 0,
						translateZ: 0
					};
				}
			}
			
			cubeSettings = newSettings;
			
			// Update controls and apply transforms
			updateSlidersForSelection();
			applyAllCubeTransforms();
		}

		function resetSelected() {
			const resetSettings = CALIBRATED_SETTINGS.lanes;
			
			if (selectedLane === 'all') {
				// Reset all lanes to the Calibrated starting configuration
				cubeSettings = JSON.parse(JSON.stringify(CALIBRATED_SETTINGS));
			} else {
				// Reset single lane to its Calibrated starting value
				cubeSettings.lanes[selectedLane] = JSON.parse(JSON.stringify(resetSettings[selectedLane]));
			}
			
			// Force update of global perspective settings on screen if needed
			document.body.style.perspective = cubeSettings.perspective + 'px';
			document.body.style.perspectiveOrigin = `${cubeSettings.perspectiveX}% ${cubeSettings.perspectiveY}%`;
			
			updateSlidersForSelection();
			applyAllCubeTransforms();
		}

		function copySettings() {
			let settingsText = `CAMERA PERSPECTIVE (body style):\nperspective: ${cubeSettings.perspective}px\nperspectiveX: ${cubeSettings.perspectiveX}%\nperspectiveY: ${cubeSettings.perspectiveY}%\n\nLANE CUBES (Independent Transforms):\n`;
			
			for (let i = 1; i <= 8; i++) {
				const s = cubeSettings.lanes[i];
				settingsText += `Lane ${i}: rotateX=${s.rotateX}Â° rotateY=${s.rotateY}Â° rotateZ=${s.rotateZ}Â° translateX=${s.translateX}px translateY=${s.translateY}px translateZ=${s.translateZ}px\n`;
			}
			
			navigator.clipboard.writeText(settingsText).then(() => {
				alert('Settings copied to clipboard!');
			});
		}
	</script>
	
	<script>
		// The WebSocket connection logic (KEEP)
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
        <h2>ðŸŽ¯ Lane Ends Projection</h2>
        
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
                    <span class="value-display" id="rotateXValue">0Â°</span>
                </label>
                <input type="range" id="rotateX" min="0" max="90" step="0.5" value="0">
            </div>

            <div class="control-group">
                <label>
                    <span>Rotate Y (Turn)</span>
                    <span class="value-display" id="rotateYValue">0Â°</span>
                </label>
                <input type="range" id="rotateY" min="-45" max="45" step="0.5" value="0">
            </div>

            <div class="control-group">
                <label>
                    <span>Rotate Z (Roll)</span>
                    <span class="value-display" id="rotateZValue">0Â°</span>
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
            Each result = bottom of cube â€¢ Rotate cube to project
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
        let laneResults = {};


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
            document.getElementById('rotateXValue').textContent = document.getElementById('rotateX').value + 'Â°';
            document.getElementById('rotateYValue').textContent = document.getElementById('rotateY').value + 'Â°';
            document.getElementById('rotateZValue').textContent = document.getElementById('rotateZ').value + 'Â°';
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
                text += `Lane ${i}: rotateX=${s.rotateX}Â° rotateY=${s.rotateY}Â° rotateZ=${s.rotateZ}Â° translateX=${s.translateX}px translateY=${s.translateY}px translateZ=${s.translateZ}px\n`;
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
        announcer_html = r'''<style>
    #livestream-wrapper {
        position: fixed;
        top: 0;
        left: 0;
        width: 100vw;
        height: 100vh;
        z-index: 9999;
        display: flex;
        justify-content: center;
        align-items: center;
        background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
    }

    #livestream-wrapper * {
        box-sizing: border-box;
    }

    .livestream-login-container {
        background: rgba(255, 255, 255, 0.95);
        padding: 40px;
        border-radius: 12px;
        box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
        width: 90%;
        max-width: 400px;
    }

    .livestream-login-container h1 {
        text-align: center;
        color: #1a1a2e;
        margin-bottom: 30px;
        font-size: 24px;
        margin-top: 0;
    }

    .livestream-form-group {
        margin-bottom: 20px;
    }

    .livestream-form-group label {
        display: block;
        margin-bottom: 8px;
        color: #333;
        font-weight: 500;
    }

    .livestream-form-group input {
        width: 100%;
        padding: 12px;
        border: 2px solid #ddd;
        border-radius: 6px;
        font-size: 16px;
        transition: border-color 0.3s;
    }

    .livestream-form-group input:focus {
        outline: none;
        border-color: #4a5568;
    }

    .livestream-password-container {
        position: relative;
    }

    .livestream-toggle-password {
        position: absolute;
        right: 12px;
        top: 50%;
        transform: translateY(-50%);
        background: none;
        border: none;
        color: #666;
        cursor: pointer;
        font-size: 14px;
        padding: 4px 8px;
    }

    .livestream-toggle-password:hover {
        color: #333;
    }

    .livestream-submit-btn {
        width: 100%;
        padding: 14px;
        background: #1a1a2e;
        color: white;
        border: none;
        border-radius: 6px;
        font-size: 16px;
        font-weight: 600;
        cursor: pointer;
        transition: background 0.3s;
    }

    .livestream-submit-btn:hover {
        background: #16213e;
    }

    .livestream-error-message {
        color: #e53e3e;
        text-align: center;
        margin-top: 15px;
        font-size: 14px;
        display: none;
    }

    .livestream-error-message.show {
        display: block;
    }

    .livestream-fullscreen-view {
        display: none;
        position: fixed;
        top: 0;
        left: 0;
        width: 100vw;
        height: 100vh;
        background: linear-gradient(135deg, #192954 0%, #004A90 100%);
        animation: livestreamGradientFlow 8s ease infinite;
        background-size: 200% 200%;
        z-index: 10000;
    }

    .livestream-gala-title {
        position: absolute;
        top: 15px;
        left: 0;
        right: 0;
        width: 100%;
        text-align: center;
        color: white !important;
        font-family: Arial, sans-serif !important;
        font-size: 48px !important;
        font-weight: 700 !important;
        margin: 0;
        padding: 0;
        z-index: 10001;
    }

    .livestream-event-info {
        position: absolute;
        top: 87px;
        left: 10px;
        color: white !important;
        font-family: Arial, sans-serif !important;
        font-size: 30px !important;
        font-weight: 700 !important;
        margin: 0;
        padding: 0;
        z-index: 10001;
        transition: opacity 0.3s ease;
    }

    .livestream-headers {
        position: absolute;
        top: 135px;
        left: 32px;
        right: 32px;
        display: flex;
        align-items: center;
        color: white !important;
        font-family: Arial, sans-serif !important;
        font-size: 30px !important;
        font-weight: 700 !important;
        margin: 0;
        padding: 0;
        z-index: 10001;
        transition: opacity 0.3s ease;
    }

    .livestream-header-lane {
        width: 50px;
        color: white !important;
    }

    .livestream-header-name {
        width: 450px;
        color: white !important;
        margin-left: 20px;
    }

    .livestream-header-club {
        flex: 1;
        color: white !important;
        margin-left: 200px;
    }

    .livestream-header-time {
        width: 120px;
        text-align: center;
        color: white !important;
        margin-right: 60px;
    }

    .livestream-header-place {
        width: 80px;
        text-align: center;
        color: white !important;
    }

    .livestream-header-lp {
        width: 80px;
        text-align: center;
        color: white !important;
    }

    .livestream-row {
        position: absolute;
        left: 32px;
        right: 32px;
        display: flex;
        align-items: center;
        color: white !important;
        font-family: Arial, sans-serif !important;
        font-size: 30px !important;
        font-weight: 700 !important;
        margin: 0;
        padding: 0;
        z-index: 10001;
        transition: opacity 0.3s ease;
    }

    .livestream-row-lane {
        width: 50px;
        color: white !important;
    }

    .livestream-row-name {
        width: 450px;
        color: white !important;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        margin-left: 20px;
        transition: opacity 0.3s ease;
    }

    .livestream-row-club {
        flex: 1;
        color: white !important;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        padding-right: 20px;
        margin-left: 200px;
        transition: opacity 0.3s ease;
    }

    .livestream-row-time {
        width: 120px;
        text-align: center;
        color: white !important;
        margin-right: 60px;
        transition: opacity 0.3s ease, color 0.3s ease;
    }

    .livestream-row-time.dq {
        color: #ff4444 !important;
    }

    .livestream-row-place {
        width: 80px;
        text-align: center;
        color: white !important;
        transition: opacity 0.3s ease;
    }

    .livestream-row-lp {
        width: 80px;
        text-align: center;
        color: white !important;
        transition: opacity 0.3s ease;
    }
	
	/* --- MEDAL BANNERS --- */
    .livestream-row.medal-gold {
        background-color: rgba(255, 215, 0, 0.4); /* Gold with transparency */
        box-shadow: 0 0 10px rgba(255, 215, 0, 0.8);
    }
    
    .livestream-row.medal-silver {
        background-color: rgba(192, 192, 192, 0.4); /* Silver with transparency */
        box-shadow: 0 0 10px rgba(192, 192, 192, 0.8);
    }
    
    .livestream-row.medal-bronze {
        background-color: rgba(205, 127, 50, 0.4); /* Bronze with transparency */
        box-shadow: 0 0 10px rgba(205, 127, 50, 0.8);
    }
    
    /* Ensure all text fields within the row are covered by the color transition */
    .livestream-row {
        transition: background-color 0.4s ease, box-shadow 0.4s ease;
    }

    .livestream-fullscreen-view.active {
        display: block;
    }

    @keyframes livestreamGradientFlow {
        0% {
            background-position: 0% 50%;
        }
        50% {
            background-position: 100% 50%;
        }
        100% {
            background-position: 0% 50%;
        }
    }

    @keyframes fadeIn {
        from {
            opacity: 0;
        }
        to {
            opacity: 1;
        }
    }

    .fade-in {
        animation: fadeIn 0.3s ease;
    }

    .updating {
        opacity: 0.5;
        transition: opacity 0.2s ease;
    }
</style>

<div id="livestream-wrapper">
    <div class="livestream-login-container" id="livestreamLoginContainer">
        <h1>Live Stream Access</h1>
        <form id="livestreamLoginForm" onsubmit="return false;">
            <div class="livestream-form-group">
                <label for="livestreamUsername">Username</label>
                <input type="text" id="livestreamUsername" name="username" required autocomplete="username">
            </div>
            <div class="livestream-form-group">
                <label for="livestreamPassword">Password</label>
                <div class="livestream-password-container">
                    <input type="password" id="livestreamPassword" name="password" required autocomplete="current-password">
                    <button type="button" class="livestream-toggle-password" id="livestreamTogglePassword">Show</button>
                </div>
            </div>
            <button type="button" class="livestream-submit-btn" id="livestreamSubmitBtn">Login</button>
            <div class="livestream-error-message" id="livestreamErrorMessage">Invalid username or password</div>
        </form>
    </div>

    <div class="livestream-fullscreen-view" id="livestreamFullscreenView">
        <h1 class="livestream-gala-title">Carnforth Otters Gala</h1>
        <p class="livestream-event-info" id="livestreamEventInfo">Event 522 Open/Male 200m Butterfly</p>
        
        <div class="livestream-headers" id="livestreamHeaders">
            <span class="livestream-header-lane">L</span>
            <span class="livestream-header-name">Name</span>
            <span class="livestream-header-club">Club</span>
            <span class="livestream-header-time">Time</span>
            <span class="livestream-header-place">P</span>
            <span class="livestream-header-lp">Lp</span>
        </div>

        <div class="livestream-row" style="top: 180px;" data-lane="1">
            <span class="livestream-row-lane">1</span>
            <span class="livestream-row-name" data-field="name"></span>
            <span class="livestream-row-club" data-field="club"></span>
            <span class="livestream-row-time" data-field="time"></span>
            <span class="livestream-row-place" data-field="place"></span>
            <span class="livestream-row-lp" data-field="lp"></span>
        </div>

        <div class="livestream-row" style="top: 230px;" data-lane="2">
            <span class="livestream-row-lane">2</span>
            <span class="livestream-row-name" data-field="name"></span>
            <span class="livestream-row-club" data-field="club"></span>
            <span class="livestream-row-time" data-field="time"></span>
            <span class="livestream-row-place" data-field="place"></span>
            <span class="livestream-row-lp" data-field="lp"></span>
        </div>

        <div class="livestream-row" style="top: 280px;" data-lane="3">
            <span class="livestream-row-lane">3</span>
            <span class="livestream-row-name" data-field="name"></span>
            <span class="livestream-row-club" data-field="club"></span>
            <span class="livestream-row-time" data-field="time"></span>
            <span class="livestream-row-place" data-field="place"></span>
            <span class="livestream-row-lp" data-field="lp"></span>
        </div>

        <div class="livestream-row" style="top: 330px;" data-lane="4">
            <span class="livestream-row-lane">4</span>
            <span class="livestream-row-name" data-field="name"></span>
            <span class="livestream-row-club" data-field="club"></span>
            <span class="livestream-row-time" data-field="time"></span>
            <span class="livestream-row-place" data-field="place"></span>
            <span class="livestream-row-lp" data-field="lp"></span>
        </div>

        <div class="livestream-row" style="top: 380px;" data-lane="5">
            <span class="livestream-row-lane">5</span>
            <span class="livestream-row-name" data-field="name"></span>
            <span class="livestream-row-club" data-field="club"></span>
            <span class="livestream-row-time" data-field="time"></span>
            <span class="livestream-row-place" data-field="place"></span>
            <span class="livestream-row-lp" data-field="lp"></span>
        </div>

        <div class="livestream-row" style="top: 430px;" data-lane="6">
            <span class="livestream-row-lane">6</span>
            <span class="livestream-row-name" data-field="name"></span>
            <span class="livestream-row-club" data-field="club"></span>
            <span class="livestream-row-time" data-field="time"></span>
            <span class="livestream-row-place" data-field="place"></span>
            <span class="livestream-row-lp" data-field="lp"></span>
        </div>

        <div class="livestream-row" style="top: 480px;" data-lane="7">
            <span class="livestream-row-lane">7</span>
            <span class="livestream-row-name" data-field="name"></span>
            <span class="livestream-row-club" data-field="club"></span>
            <span class="livestream-row-time" data-field="time"></span>
            <span class="livestream-row-place" data-field="place"></span>
            <span class="livestream-row-lp" data-field="lp"></span>
        </div>

        <div class="livestream-row" style="top: 530px;" data-lane="8">
            <span class="livestream-row-lane">8</span>
            <span class="livestream-row-name" data-field="name"></span>
            <span class="livestream-row-club" data-field="club"></span>
            <span class="livestream-row-time" data-field="time"></span>
            <span class="livestream-row-place" data-field="place"></span>
            <span class="livestream-row-lp" data-field="lp"></span>
        </div>
    </div>
</div>

<script>
(function() {
    // Login functionality
    const togglePassword = document.getElementById('livestreamTogglePassword');
    const passwordInput = document.getElementById('livestreamPassword');
    const submitBtn = document.getElementById('livestreamSubmitBtn');
    const errorMessage = document.getElementById('livestreamErrorMessage');
    const loginContainer = document.getElementById('livestreamLoginContainer');
    const fullscreenView = document.getElementById('livestreamFullscreenView');
	const CLUB_MAPPING = {
    'TENS': '1066 Swimmers',
    'ENTX': '1930 ASC',
    'FRSS': '4 Shires Swimming Club',
    'ANTW': 'A.N.T. Swimming Club Of Somerset',
    'ABVY': 'Aberavon Swimming Club',
    'ACDY': 'Aberdare Comets Diving Club',
    'NANX': 'Aberdeen ASC',
    'NAVX': 'Aberdeen Diving Club',
    'NADX': 'Aberdeen Dolphins ASC',
    'NABX': 'Aberdeen University Diving Club',
    'NAUX': 'Aberdeen University Swimming and WPC',
    'ABAY': 'Abergavenny Swimming Club',
    'ABTY': 'Abertillery Piranhas Swimming Club',
    'ABRY': 'Aberystwyth and District Swimming Club',
    'ABIS': 'Abingdon Vale SC',
    'ACEL': 'AC East London',
    'ASBW': 'Academy Swim Team Burnham',
    'ACCN': 'Accrington SC',
    'ADWE': 'Adwick (Doncaster) SC',
    'AVSY': 'Afan Valley Swimming Club',
    'WAMX': 'Airdrie and Monkland ASC',
    'AIRE': 'Aireborough SC',
    'ALBS': 'Albatross DC',
    'NAOX': 'Alford Otters ASC',
    'ALFA': 'Alfreton Swimming Club',
    'WAAX': 'Alloa ASC',
    'ALNE': 'Alnwick Dolphin SC',
    'ALSN': 'Alsager SC',
    'ALTS': 'Alton & District Swimming Club',
    'ALTN': 'Altrincham SC',
    'AMES': 'Amersham SC',
    'AMMY': 'Amman Valley Swimming Club',
    'ANCL': 'Anaconda SC',
    'ANDS': 'Andover Swimming & WP Club',
    'WANX': 'Annan ASC',
    'APWS': 'Applemore and Waterside SC',
    'AFHE': 'Aqua Force Swimming Academy Hartlepool',
    'ROCN': 'Aquabears Of Rochdale SC',
    'AJNE': 'AquaJets Newcastle SC',
    'AQUT': 'Aqualina (Stevenage) Artistic Swimming Club',
    'AASS': 'Aquaoaks Artistic Swimming Club (Sevenoaks)',
    'AQNL': 'Aquavision North London Swim Club',
    'ARFY': 'Arfon Swimming Club',
    'UACX': 'Argyll & Clyde Swim Team',
    'EABX': 'Armadale Barracudas ASC',
    'ARME': 'Armthorpe & District SC',
    'ARNA': 'Arnold SC',
    'ARTS': 'Arun Tridents SC',
    'TESQ': 'ASA Live Test Club',
    'ATEA': 'Asa Temporary cat 2 club',
    'ASRS': 'Ascot Royals Swim Club',
    'ASHA': 'Ashbourne & District SC',
    'ASFS': 'Ashford School',
    'ASHS': 'Ashford Town SC',
    'ASHE': 'Ashington SC',
    'ASHN': 'Ashton Central SC',
    'AULN': 'Ashton-Under-Lyne SC',
    'SPAE': 'Askern Spa ASC',
    'ASTM': 'Aston (Birmingham) SC',
    'ATHN': 'Atherton and Leigh ASC',
    'ATLS': 'Atlantis Swimming Club',
    'AVOL': 'Avondale SC',
    'AYBS': 'Aylesbury & District SC',
    'WARX': 'Ayr Diving Club',
    'BACW': 'Backwell SC',
    'BACA': 'Bakewell Swimming Club',
    'WBBX': 'Balfron Barracudas',
    'BANS': 'Banbury SC',
    'NBBX': 'Banchory Beavers ASC',
    'NBMX': 'Banff and Buchan Masters',
    'BBDY': 'Bangor Diving Club',
    'BACL': 'Barking & Dagenham Aquatics Club',
    'SPAL': 'Barnes SC',
    'BANL': 'Barnet Copthall SC',
    'BWCL': 'Barnet Water Polo Club',
    'BARE': 'Barnsley SC',
    'BARW': 'Barnstaple SC',
    'BARN': 'Barrow ASC',
    'BARY': 'Barry Swimming Club',
    'BAST': 'Basildon and Phoenix SC',
    'BBFS': 'Basingstoke Bluefins SC',
    'BSSA': 'Bassetlaw Swim Squad',
    'BJSS': 'Bassett J.S.F. Swimming Club',
    'BADW': 'Bath Dolphin SC',
    'BATI': 'Bath Performance Centre',
    'EBEX': 'Bathgate ASC',
    'BFDS': 'Beachfield Swimming Squad',
    'BCNS': 'Beacon SC',
    'BEAS': 'Beau Sejour Barracuda SC',
    'BEVT': 'Beavers Masters Bedford SC',
    'BEBN': 'Bebington SC',
    'BEKL': 'Beckenham SC',
    'MODT': 'Bedford Swim Squad',
    'BEPT': 'Bedford Water Polo Club',
    'WBLX': 'Bellshill Sharks ASC',
    'BEMA': 'Belper Marlin Swimming Club',
    'BERW': 'Bere Regis & District SC',
    'BKHT': 'Berkhamsted SC',
    'BSBS': 'Berkshire & S Bucks ASA',
    'BEGL': 'Bethnal Green SC',
    'BEBE': 'Beverley Barracudas SC',
    'BXHS': 'Bexhill SC',
    'BEML': 'Bexley Masters SC',
    'BEXL': 'Bexley SC',
    'BXPS': 'Bexley Water Polo Club (Swanley)',
    'BCCS': 'Bicester Blue Fins SC',
    'BIDM': 'Biddulph SC',
    'BWPT': 'Biggleswade SC',
    'BILE': 'Billingham Forum SC',
    'BILM': 'Bilston SC',
    'SNPA': 'Bingham Penguins SC',
    'BIGE': 'Bingley SC',
    'BICA': 'Bircotes SC',
    'BKDN': 'Birkenhead SC',
    'BMSM': 'Birmingham Marlins Swimming Club',
    'BIRM': 'Birmingham Masters',
    'BIRE': 'Birtley SC',
    'BIST': 'Bishops Stortford SC',
    'BSAS': 'Bishops Waltham Mitres SwimClub',
    'BATL': 'BJSC Tooting',
    'BCPM': 'Black Country n Potteries Masters SC',
    'BLAS': 'Black Lion SC',
    'BLCN': 'Blackburn Centurion SC',
    'BAQN': 'Blackpool Aquatics ASC',
    'MBEX': 'Blairgowrie ASC',
    'BLFW': 'Blandford SC',
    'WBEX': 'Blantyre ASC',
    'BLAE': 'Blaydon & District SC',
    'BAES': 'Bletchley and District SC',
    'BMAS': 'Blue Marlins Water Polo Club',
    'BLYE': 'Blyth Lifeguard & Swimming Club',
    'BBSM': 'Blythe Barracudas SC',
    'WBSX': 'Bo\'ness ASC',
    'BLDM': 'Boldmere Swimming and Water Polo Club',
    'BOLE': 'Boldon C.A. Swim Club',
    'BTMN': 'Bolton Metro Swimming Squad',
    'BLNN': 'Bolton SC',
    'NBAX': 'Bon Accord Thistle ASC',
    'BNLN': 'Bootle & North Liverpool SC',
    'BWFL': 'Bor of Waltham Forest (Gators)',
    'UBEX': 'Borders Elite Swim Team',
    'BORE': 'Borocuda Teesside ASC',
    'BAME': 'Borough of Barnsley SC',
    'BBST': 'Borough of Broxbourne Swim Squad',
    'HAWL': 'Borough of Harrow S C',
    'BOKE': 'Borough of Kirklees SC',
    'BOST': 'Borough of Southend Swimming and Training Club',
    'BOSE': 'Borough Of Stockton Swim Scheme',
    'BOSA': 'Boston Amateur SC',
    'BOTT': 'Bottisham SC',
    'BORS': 'Bourne End SC',
    'BCSW': 'Bournemouth Collegiate School SC',
    'BTHW': 'Bournemouth SC',
    'BOXS': 'Box Hill SC',
    'BSCA': 'Brackley Swimming Club',
    'BRKS': 'Bracknell & Wokingham SC',
    'BDSE': 'Bradford Dolphin SC',
    'BGSE': 'Bradford Grammar School Swimming Club',
    'BRAE': 'Bradford SC',
    'BOAW': 'Bradford-On-Avon ASC',
    'BTRT': 'Braintree & Bocking SC',
    'BRMA': 'Bramcote Swimming Club',
    'BSTA': 'Braunstone SC',
    'MBBX': 'Brechin Beavers ASC',
    'BREY': 'Brecon Swimming Club',
    'BOBL': 'Brent Dolphins SC',
    'BART': 'Brentwood Artistic Swimming Club',
    'BRET': 'Brentwood SC',
    'NBDX': 'Bridge of Don ASC',
    'BRIN': 'Bridgefield SC',
    'BFWN': 'Bridgefield Water Polo Club',
    'BRCY': 'Bridgend County Swim Squad',
    'BSCY': 'Bridgend Swim Club',
    'BRIW': 'Bridgwater Amateur Swimming Club',
    'BRDE': 'Bridlington SC',
    'BBAW': 'Bridport Barracuda',
    'BRGE': 'Brighouse SC',
    'BCSS': 'Brighton College Swimming Club',
    'BRDS': 'Brighton Dolphin SC',
    'BSGW': 'Bristol and South Gloucestershire Swimming Club',
    'BRHW': 'Bristol Henleaze SC',
    'BRNW': 'Bristol North SC',
    'BRAS': 'British Army',
    'BRXW': 'Brixham SC',
    'BSLS': 'Broadstairs Lifeguard & SC',
    'BRDM': 'Broadway (Walsall)',
    'BRON': 'Broadway SC (North)',
    'NBHX': 'Broch SC',
    'BDAT': 'Brocket (Hatfield) Diving Academy',
    'BROW': 'Brockworth Swimming Club',
    'BRYL': 'Bromley SC',
    'BRBL': 'Brompton SC',
    'BROM': 'Bromsgrove SC',
    'BROL': 'Broomfield Park SC',
    'BRBT': 'Broxbourne SC',
    'EBNX': 'Broxburn & District ASC',
    'NBKX': 'Buckie ASC',
    'BUCS': 'Buckingham Swans',
    'BUCY': 'Buckley Swimming Club',
    'NBNX': 'Bucksburn ASC',
    'BUCL': 'BUCS',
    'BUDW': 'Bude Sharks',
    'BOSW': 'Burnham-on-Sea SC',
    'BUBN': 'Burnley BOBCATS ADM SC',
    'EBDX': 'Burntisland ASC',
    'BRWM': 'Burntwood Swimming Club',
    'BDCN': 'Burscough Diving Club',
    'BURM': 'Burton Amateur SC',
    'BRYN': 'Bury and Elton SC',
    'BWPN': 'Bury Water Polo Club',
    'BUST': 'Bushey Amateur Swimming Club',
    'BUXA': 'Buxton & District SC',
    'CAPY': 'Caerphilly County Swim Squad',
    'CALY': 'Caldicot Swimming Club',
    'UCDX': 'Caledonia',
    'USCX': 'Caledonia Synchro',
    'CMIL': 'Cally Masters Islington',
    'CAFW': 'Calne Alpha Four ASC',
    'CALA': 'Calverton and Bingham SC',
    'CAAY': 'Cambrian Aquatics Academy',
    'CDTT': 'Cambridge Dive Team',
    'CAUT': 'Cambridge University Swimming & Water Polo Club',
    'CSCL': 'Camden Swiss Cottage SC',
    'CAMW': 'Camelford Stingers',
    'CHEM': 'Camp Hill Edwardians SC',
    'CAHM': 'Camp Hill SC',
    'PHYM': 'Cannock Phoenix Swimming Club',
    'CNVT': 'Canvey Island SC',
    'CRNW': 'Caradon SC',
    'ECNX': 'Cardenden ASC',
    'CDMY': 'Cardiff Masters Swimming Club',
    'CMUY': 'Cardiff Metropolitan University',
    'CSCY': 'Cardigan Swimming Club - Clwb Nofio Aberteifi',
    'CAQN': 'Carlisle Aquatics',
    'CARA': 'Carlton Forum SC',
    'CAMY': 'Carmarthen & District Swimming Club',
    'CWPY': 'Carmarthen Water Polo Club',
    'CMMY': 'Carmarthenshire Masters Swimming Club',
    'CARW': 'Carn Brea and Helston SC',
    'ECEX': 'Carnegie ASC',
    'CDON': 'Carnforth & Dist Otters',
    'MCCX': 'Carnoustie Claymores',
    'CMAW': 'Carrick Masters',
    'CWPE': 'Castleford WPC',
    'CAWS': 'Cawpra SC',
    'CEDY': 'Celtic Dolphins Swimming Club',
    'CHAN': 'Chadderton SC',
    'CHAS': 'Chalfont Otters SC',
    'CHPE': 'Chapeltown SC',
    'CHSM': 'Chase SC',
    'CSDM': 'Cheadle (Staffs) & District',
    'CHLL': 'Cheam Marcuda SC',
    'CHDW': 'Cheddar Kingfishers SC',
    'CHET': 'Chelmsford City SC',
    'CWSL': 'Chelsea & Westminster SC',
    'CASW': 'Cheltenham Artistic Swimming Club',
    'CPAW': 'Cheltenham Phoenix Aquatics Club',
    'CHEW': 'Cheltenham S & W P Club',
    'CHEY': 'Chepstow & District Swimming Club',
    'CHMS': 'Chesham SC',
    'CHRN': 'Cheshire County WPSA',
    'CHST': 'Cheshunt Swimming Club',
    'CLSE': 'Chester-Le-Street SC',
    'CFDA': 'Chesterfield Swimming Club',
    'CHIS': 'Chichester (Cormorant) SC',
    'CHIW': 'Chippenham ASC',
    'CHIY': 'Chirk Dragons Swimming Club',
    'CMSL': 'Chislehurst Millennium Swim Squad',
    'CHML': 'Chiswick Masters',
    'CMAN': 'Chorley Marlins SC',
    'CIRW': 'Cirencester SC',
    'CWPL': 'Citizens Water Polo Club',
    'BHMM': 'City of Birmingham SC',
    'CBEE': 'City of Bradford Esprit Diving',
    'COBE': 'City of Bradford SC',
    'BRIS': 'City of Brighton and Hove',
    'COBW': 'City of Bristol SC',
    'CAMT': 'City of Cambridge SC',
    'CANS': 'City of Canterbury SC',
    'COCY': 'City of Cardiff',
    'COCN': 'City of Chester Swimming Club',
    'COVM': 'City of Coventry SC',
    'DMRE': 'City of Doncaster Masters Swim Squad',
    'ELYT': 'City Of Ely Amateur Swimming Club',
    'WCGX': 'City Of Glasgow Swim Team',
    'HERM': 'City of Hereford SC',
    'LDCE': 'City of Leeds Diving Club',
    'LDSE': 'City of Leeds SC',
    'LSYE': 'City Of Leeds Synchronised SC',
    'CLWE': 'City of Leeds Water Polo Club',
    'COLA': 'City of Leicester SC',
    'CLPA': 'City of Linc Pentaqua SC',
    'LPLN': 'City of Liverpool SC',
    'MANN': 'City of Manchester Aquatics',
    'CMWN': 'City of Manchester WPC',
    'CMKS': 'City Of Milton Keynes',
    'NWMY': 'City Of Newport Swimming & Water Polo Club',
    'NORT': 'City of Norwich SC',
    'OXFS': 'City Of Oxford SC',
    'COWS': 'City Of Oxford Water Polo Club',
    'PETT': 'City Of Peterborough SC',
    'CPAN': 'City of Preston Aquatics SC',
    'SAFN': 'City of Salford SC',
    'CSAN': 'City of Salford Synchronised Swimming Club',
    'SHDE': 'City of Sheffield Diving Club',
    'COSE': 'City of Sheffield Swim Squad Ltd',
    'SWPE': 'City of Sheffield WPC',
    'COSS': 'City of Southampton SC',
    'SOST': 'City of Southend on Sea SC',
    'SAST': 'City of St Albans SC',
    'CITM': 'City of Stoke SC (Cosacss)',
    'SUNE': 'City of Sunderland ASC',
    'SWAY': 'City of Swansea Aquatics Club',
    'WAKE': 'City of Wakefield SC',
    'CLAT': 'Clacton Swimming Club',
    'CLDE': 'Cleethorpes & Dist SC',
    'CLEW': 'Clevedon SC',
    'CLIN': 'Clitheroe Dolphins SC',
    'BANY': 'Clwb Nofio Bangor Swim Club',
    'CANY': 'Clwb Nofio Caernarfon',
    'PBPY': 'Clwb Nofio P.B.P.',
    'IOAY': 'Clwb Nofio Ynys Mon | Isle of Anglesey Swimming Club',
    'WCKX': 'Clydebank ASC',
    'COAA': 'Coalville SC',
    'COKN': 'Cockermouth SC',
    'COPT': 'Colchester Phoenix ASC',
    'COLT': 'Colchester Swimming & Water Polo Club',
    'COLN': 'Colne SC',
    'CONY': 'Connahs Quay Swimming Club',
    'CONE': 'Consett SC',
    'COPN': 'Copeland ASC',
    'CORA': 'Corby ASC',
    'CSDA': 'Corby Steel Diving',
    'CWLW': 'Cornwall ASA',
    'CORW': 'Corsham A.S.C.',
    'CSHY': 'Corwen Sharks Swimming Club',
    'CRAS': 'Cranleigh SC',
    'CRWS': 'Crawley SC',
    'CREW': 'Crediton & District SC',
    'CREN': 'Crewe Flyers SC',
    'CRON': 'Crosby SC',
    'CROL': 'Croydon Amphibians SC',
    'CPML': 'Crystal Palace Masters Swimming Club',
    'NCOX': 'Cults Otters ASC',
    'WCDX': 'Cumbernauld ASC',
    'CUMN': 'Cumbria ASA',
    'ECRX': 'Cupar and District Swimming Club',
    'CDWY': 'Cwm Draig Water Polo Club',
    'CWMY': 'Cwmbran Swimming Club',
    'DAST': 'Dacorum Artistic SC',
    'SADT': 'Dacorum Diving Club',
    'DDME': 'Darlington Dolphin Masters SC',
    'DARE': 'Darlington SC',
    'DARS': 'Dartford District SC',
    'DSCW': 'Dartmouth Swimming Club',
    'DMAN': 'Darwen Masters Swimming Club',
    'DAVA': 'Daventry Dolphins SC',
    'DAWW': 'Dawlish SC',
    'DTRS': 'Deal Tri Masters Swim Club',
    'DCME': 'Dearne Valley SC',
    'DEBT': 'Deben SC',
    'DEEA': 'Deepings SC',
    'NDDX': 'Delting Dolphins ASC',
    'DENY': 'Denbigh Dragons Swim Club',
    'DDTY': 'Denbighshire Development Team',
    'DSSN': 'Denton Artistic Swimming Club',
    'DEXA': 'Derby Excel Swimming Club',
    'DEPA': 'Derby Phoenix SC',
    'DRBA': 'Derbyshire ASA',
    'DADT': 'Dereham & District ASC',
    'DEVE': 'Derwent Valley SC',
    'DAGE': 'Derwentside & Gateshead Swim Team',
    'NDNX': 'Deveron ASC',
    'DEVW': 'Devizes ASC',
    'DVNW': 'Devon County ASA',
    'DERW': 'Devonport RSA',
    'DBYE': 'Dewsbury SC',
    'DABS': 'Didcot & Barramundi SC',
    'NDIX': 'Dingwall ASC',
    'DINW': 'Dinnaton SC',
    'DOTT': 'Diss Otters SC',
    'DLAL': 'Dive London Aquatics Club',
    'DODE': 'Doncaster Dartes SC',
    'DORS': 'Dorking Swimming Club',
    'DRSW': 'Dorset County ASA',
    'DOUN': 'Douglas SC',
    'DOVM': 'Dove Valley SC',
    'DLCS': 'Dover Lifeguard Swimming Club',
    'DSST': 'Down Syndrome Swimming Great Britain',
    'DFDE': 'Driffield SC',
    'DTDM': 'Droitwich Dolphins SC',
    'DRDA': 'Dronfield Dolphins SC',
    'DTSW': 'DTs',
    'DUKN': 'Dukinfield Marlins ASC',
    'DUDL': 'Dulwich Dolphins',
    'WDSX': 'Dumfries ASC',
    'MDCX': 'Dundee City Aquatics SC',
    'MDDX': 'Dundee Diving',
    'MDUX': 'Dundee University Swimming & WPC',
    'EDDX': 'Dunedin Swim Team',
    'EDEX': 'Dunfermline ASC',
    'EDUX': 'Dunfermline Water Polo Club',
    'DUAT': 'Dunmow Atlantis SC',
    'WDNX': 'Dunoon ASC',
    'EDSX': 'Duns ASC',
    'DUBT': 'Dunstable SC',
    'DUWT': 'Dunstable Water Polo Club',
    'DURE': 'Durham City ASC',
    'DURW': 'Durrington Otters',
    'DDOW': 'Dursley Dolphins',
    'NDAX': 'Dyce (Aberdeen) ASC',
    'EALL': 'Ealing SC',
    'EANT': 'East Anglian Swallow Tails',
    'EAGS': 'East Grinstead Swimming Club',
    'EIES': 'East Invicta eXcel',
    'WEKX': 'East Kilbride ASC',
    'ELEE': 'East Leeds SC',
    'UELX': 'East Lothian Swim Team',
    'MLDA': 'East Midland Region',
    'EAST': 'East Region',
    'NESX': 'East Sutherland Swimming Club',
    'EASS': 'Eastbourne SC',
    'EOTL': 'Eastern Otters Water Polo Club',
    'EATS': 'Eastleigh SC',
    'EASL': 'Eaton Square SC',
    'ECKA': 'Eckington SC',
    'EDPS': 'Edenbridge Piranhas SC',
    'EEDX': 'Edinburgh Diving Club',
    'EESX': 'Edinburgh Synchro ASC',
    'EEUX': 'Edinburgh University',
    'EDLE': 'Edlington SC',
    'EEWS': 'Electric Eels of Windsor SC',
    'NENX': 'Elgin SC',
    'ECSM': 'Ellesmere College Swimming Academy',
    'ELLN': 'Ellesmere Port SC',
    'ELMS': 'Elmbridge Phoenix SC',
    'EMSL': 'Eltham Stingrays',
    'WEAX': 'Enable Arion SC',
    'ENSL': 'Enfield Swim Squad',
    'ERME': 'English Roses Masters WPC',
    'EPPT': 'Epping Forest Dist SC',
    'EPSS': 'Epsom District SC',
    'ERIL': 'Erith & District SC',
    'ESTE': 'Eston SC',
    'ETEA': 'Etwall Eagles SC',
    'EVEN': 'Everton Swimming Assoc',
    'EVEM': 'Evesham SC',
    'EMAW': 'Exe Masters Swimming Club',
    'EXCW': 'Exeter City Swimming Club',
    'EUAW': 'Exeter University Alumni Swimming Club',
    'ECWW': 'Exeter University Water Polo Club',
    'EXEW': 'Exeter Waterpolo & Swimming Club',
    'EXMW': 'Exmouth Swimming Club',
    'EEHX': 'Eyemouth & District ASC',
    'UFTX': 'Falkirk Inter-Region Swim Team',
    'WFOX': 'Falkirk Otters ASC',
    'FANS': 'Fareham Nomads SC',
    'FARS': 'Farnham SC',
    'EFPX': 'Fauldhouse Penguins SC',
    'FAVS': 'Faversham SC',
    'FLXT': 'Felixstowe SC',
    'FELL': 'Feltham SC',
    'EFAX': 'Ferry Amateur Swim Team',
    'UFNX': 'Fife North East Swim Team',
    'EFEX': 'Fife Synchronised Swimming Club',
    'EFSX': 'Fins CSC',
    'FISY': 'Fishguard Flyers Swimming Club',
    'FLEN': 'Fleetwood Mako\'s',
    'FLIY': 'Flint Swimming Club',
    'MBDT': 'Flitwick Dolphins SC',
    'FSTN': 'Flixton SC',
    'FOLS': 'Folkestone SC',
    'FODW': 'Forest of Dean SC',
    'MFRX': 'Forfar ASC',
    'FSCN': 'Formby Swimming Club',
    'NFBX': 'Forres Blu Fins ASC',
    'WFTX': 'Forth Valley Tridents',
    'WFVX': 'Forth Valley WPC',
    'FORL': 'Forward Hillingdon Squad',
    'NFSX': 'Free Style SC',
    'FMSL': 'Frognal Masters Swimming Club',
    'FROW': 'Frome SC',
    'GRDA': 'Gainsborough Dolphins SC',
    'EGAX': 'Galashiels ASC',
    'NGHX': 'Garioch ASC',
    'GTGN': 'Garstang SC',
    'GARN': 'Garston SC',
    'GHSE': 'Gateshead Artistic Swimming Club',
    'GGEY': 'Gele Gators Swimming Club',
    'WNSX': 'Glasgow Nomads SC',
    'WGUX': 'Glasgow University Swim Team',
    'WWMX': 'Glasgow Western Masters ASC',
    'EGSX': 'Glenrothes ASC',
    'GLOA': 'Glossop ASC',
    'GLUW': 'Gloucester ASA',
    'GLOW': 'Gloucester City SC',
    'GLMW': 'Gloucester Masters SC',
    'GODS': 'Godalming ASC',
    'GOSS': 'Gosport Dolphins SC',
    'WGHX': 'Grangemouth ASC',
    'GRNA': 'Grantham SC',
    'KWPA': 'Grantham Water Polo Club',
    'NGRX': 'Grantown-on-Spey SC',
    'GRAS': 'Gravesend & Northfleet SC',
    'BODS': 'Great Britain Deaf Swimming Club',
    'GBPE': 'Great Britain Police',
    'GHON': 'Great Harwood Otters SC',
    'GYAT': 'Great Yarmouth SC',
    'GASA': 'Green Arrows SSC',
    'GWRL': 'Greenwich Royals SC',
    'EGEX': 'Grove ASC',
    'GUES': 'Guernsey Swimming Club',
    'GWPS': 'Guernsey Water Polo LBG',
    'GUIS': 'Guildford City Swimming Club',
    'CRNS': 'Guildford Water Polo Club',
    'GUIE': 'Guisborough SC',
    'HAGL': 'Hackney Anaconda',
    'EHNX': 'Haddington & District ASC',
    'HADT': 'Hadleigh and Sudbury Swimming Club',
    'EHAX': 'Hailes ASC',
    'HAIS': 'Hailsham SC',
    'HALM': 'Halesowen SC',
    'HSWT': 'Halesworth & District SC',
    'HALE': 'Halifax SC',
    'HAST': 'Halstead Swimming Club',
    'WIDN': 'Halton SC',
    'HAQS': 'Hamble Aquatics Swim Team',
    'HASE': 'Hambleton Swim Squad',
    'WHBX': 'Hamilton Baths ASC',
    'WHNX': 'Hamilton Dolphins',
    'WHWX': 'Hamilton Water Polo Club',
    'HNTS': 'Hampshire County ASA',
    'HGSM': 'Handsworth Grammar Sch Old Boys',
    'HABL': 'Haringey Aquatics',
    'HALT': 'Harlow Penguin SC',
    'HRPT': 'Harpenden Swimming Club',
    'HARN': 'Harpurhey SC',
    'HARE': 'Harrogate District SC',
    'HDCE': 'Harrogate Diving Club',
    'HRWL': 'Harrow Scout & Guide SC',
    'HARS': 'Hart Swimming Club',
    'HATE': 'Hartlepool SC',
    'HDPT': 'Harwich Dovercourt/Parkeston SC',
    'HSMS': 'Haslemere Swimming Club',
    'HSGS': 'Hastings Seagull SC',
    'HATT': 'Hatfield SC',
    'HVWS': 'Havant & Waterlooville SC',
    'HAVY': 'Haverfordwest Swimming Club',
    'ECDL': 'Havering Cormorants Diving Club',
    'EHTX': 'Hawick & Teviotdale ASC',
    'HAZN': 'Hazel Gr/Bramhall (Saracens) SC',
    'HOVY': 'Heads of the Valleys Swimming Club',
    'EHMX': 'Heart Of Midlothian ASC',
    'HTHM': 'Heath Town SC',
    'HEBE': 'Hebburn Metro SC',
    'HEES': 'Hedge End Swimming Club',
    'WHHX': 'Helensburgh ASC',
    'HEMT': 'Hemel Hempstead SC',
    'HLSS': 'Henley Leisure Swimming Club',
    'HENS': 'Henley SC',
    'HNBS': 'Herne Bay L&SC',
    'HCEW': 'Heron Swim Team Somerset',
    'HERT': 'Hertford SC',
    'HRTT': 'Hertfordshire Aquatics Club',
    'HETE': 'Hetton SC',
    'HDCS': 'Highgate DC (Kent)',
    'NHDX': 'Highland Disability Swim Team',
    'UHIX': 'Highland Swim Team',
    'TDCW': 'Highworth Phoenix Diving Club',
    'HISL': 'Hillingdon Swimming Club',
    'HINA': 'Hinckley SC',
    'HINN': 'Hindley SC',
    'HITT': 'Hitchin SC',
    'HODT': 'Hoddesdon SC',
    'HOLY': 'Holywell Swimming Club',
    'HONW': 'Honiton SC',
    'HORA': 'Horncastle Otters SC',
    'HORL': 'Hornchurch SC',
    'HORW': 'Horton & Broadway Swimming Club',
    'HLCN': 'Horwich Leisure Centre SC',
    'HOJL': 'Hounslow Jets',
    'HBAN': 'Howe Bridge Aces SC',
    'HOYN': 'Hoylake SC',
    'HUKA': 'Hucknall SC',
    'RRHA': 'Hucknall WPC',
    'HUOE': 'Huddersfield Otters SC',
    'HWPE': 'Hull Water Polo Club',
    'HUNT': 'Huntingdon Piranhas SC',
    'NHYX': 'Huntly ASC',
    'HYDN': 'Hyde Seal Swimming Club',
    'HYTS': 'Hythe Aqua',
    'HYAS': 'Hythe Artistic Swimming Club',
    'ILFW': 'Ilfracombe SC',
    'ILKA': 'Ilkeston SC',
    'ILKE': 'Ilkley SC',
    'ILMW': 'Ilminster Swimming Club',
    'IMPT': 'Impington SC',
    'EISX': 'Incas',
    'ISMS': 'InSync - Milton Keynes Synchronised Swimming Club',
    'WIEX': 'Inverclyde ASC',
    'WIMX': 'Inverclyde Masters ASC',
    'EIHX': 'Inverleith',
    'NISX': 'Inverness ASC',
    'INVS': 'Invicta WPC',
    'WIJX': 'Islay & Jura Dolphins ASC',
    'IOMN': 'Isle of Man Swimming Club',
    'IOMS': 'Isle Of Wight Marlins Swim Club',
    'JLDS': 'Jersey Long Distance',
    'JERS': 'Jersey SC',
    'JWPS': 'Jersey Water Polo Association',
    'EKOX': 'Kelso ASC',
    'KENN': 'Kendal SC',
    'KEMM': 'Kenilworth Masters SC',
    'KNTQ': 'Kent County ASA',
    'KWSS': 'Kent Weald Swim Squad',
    'SAYW': 'Kernow Artistic Swimming Club',
    'KETA': 'Kettering Amateur Swimming Club',
    'KEYW': 'Keynsham SC',
    'KAGS': 'Kidlington & Gosford SC',
    'KILL': 'Killerwhales SC (Havering)',
    'WKKX': 'Kilmarnock ASC',
    'KIMA': 'Kimberley SC',
    'KINE': 'Kingfishers Scarborough SC',
    'KCSL': 'Kings Cormorants SC',
    'KINT': 'Kings Langley SC',
    'KKFW': 'Kingsbridge Kingfishers SC',
    'KAQM': 'Kingsbury Aquarius SC',
    'KLDS': 'Kingston Artistic Swimming Club',
    'WKNX': 'Kingston ASC',
    'KIRL': 'Kingston Royals SC',
    'KUHE': 'Kingston Upon Hull SC',
    'MKOX': 'Kinross Otters ASC',
    'WKEX': 'Kintyre ASC',
    'KIPE': 'Kippax SC',
    'EKYX': 'Kirkcaldy ASC',
    'WKTX': 'Kirkcudbright SC',
    'KIRN': 'Kirkham & Wesham SC',
    'WKHX': 'Kirkintilloch & Kilsyth ASC',
    'KNTE': 'Knottingley ASC',
    'KNUN': 'Knutsford Amateur Swimming Club',
    'WLKX': 'Lanark',
    'LNCN': 'Lancashire County WPSA',
    'LTBN': 'Lancashire Tridents (Blackburn)',
    'LANN': 'Lancaster City SC',
    'LCSS': 'Lancing College Swimming Club',
    'LARS': 'Larkfield SC',
    'WLAX': 'Larkhall Avondale ASC',
    'WLWX': 'Larkhall Water Polo Club',
    'LSCW': 'Launceston SC',
    'SPAM': 'Leamington Spa SC',
    'LEAL': 'Leander SC',
    'LETS': 'Leatherhead SC',
    'LADM': 'Ledbury & Malvern SC',
    'LUAE': 'Leeds University Aquatics SC',
    'LEEM': 'Leek ASC',
    'LEMA': 'Leicester Masters',
    'PENA': 'Leicester Penguins SC',
    'LSHA': 'Leicester Sharks Swimming Club',
    'LECA': 'Leicestershire ASA',
    'LBOT': 'Leighton Buzzard Otters Swimming Club for the Disabled',
    'LBZT': 'Leighton Buzzard SC',
    'LDST': 'Leiston & District Swimming Club',
    'ELHX': 'Leith ASC',
    'NLKX': 'Lerwick ASC',
    'LCHT': 'Letchworth ASC',
    'LEWS': 'Lewes SC',
    'LWPL': 'Lewisham Water Polo Club',
    'LEYN': 'Leyland Barracudas Swimming Club',
    'KEYL': 'Leyton SC',
    'LICM': 'Lichfield SC',
    'LTSA': 'Lincoln Trident Swimming Academy',
    'VULA': 'Lincoln Vulcans SC',
    'LNCA': 'Lincolnshire ASA',
    'LCRT': 'Linslade Crusaders Swimming Club',
    'LIVN': 'Liverpool Penguins SC',
    'ELDX': 'Livingston & District Dolphins',
    'ELNX': 'Livingston Swim Club',
    'LNDY': 'Llandudno Swimming Club',
    'LLLY': 'Llanelli Swimming Club',
    'NLRX': 'Lochaber Leisure Centre Swim Team',
    'LSSS': 'Locks Heath Swim Squad',
    'LOFE': 'Loftus Dolphins SC',
    'WLDX': 'Lomond SC',
    'EWPL': 'London Bor of Enfield WPC',
    'LHOL': 'London Borough of Hounslow',
    'LBRL': 'London Borough of Redbridge SC',
    'LODL': 'London Disability SC',
    'LONL': 'London Region',
    'LRSL': 'London Regional Synchronised SC',
    'LEDA': 'Long Eaton SC',
    'ELRX': 'Lothian Racers SC',
    'LOGI': 'Loughborough Performance Centre',
    'LOUA': 'Loughborough Town SC',
    'LCLA': 'Loughborough University Swimming',
    'LOMT': 'Loughton Masters Swimming Club',
    'LODA': 'Louth Dolphins SC',
    'LCMM': 'Lucton Typhoon SC',
    'LUDM': 'Ludlow Swimming Club',
    'LKDT': 'Luton Diving Club',
    'LYDW': 'Lydney SC',
    'YLSN': 'Lytham St Annes SC',
    'MADS': 'Maidenhead ASC',
    'MAIS': 'Maidstone SC',
    'WMSX': 'Making Waves ASC',
    'WENT': 'Maldon Sharks Swimming Club',
    'MALW': 'Malmesbury Marlins ASC',
    'MDCE': 'Maltby Diving Club',
    'MNWN': 'Manchester & North West Disability SC',
    'MADN': 'Manchester Aquatics Centre DC',
    'MANI': 'Manchester Performance Centre',
    'MSWN': 'Manchester Sharks WPC',
    'MTCN': 'Manchester TC Swimming Club',
    'MANA': 'Mansfield SC',
    'MART': 'March Marlins SC',
    'NSHM': 'Market Drayton SC',
    'MKHA': 'Market Harborough SC',
    'MARW': 'Marlborough Penguins ASC',
    'MPLN': 'Marple SC',
    'MATA': 'Matlock & District SC',
    'MWPA': 'Matlock Water Polo Club',
    'MAXS': 'Maxwell SC',
    'MMSS': 'Medway Artistic Swimming Club',
    'MEMS': 'Medway Maritime Swimming Club',
    'MELW': 'Melksham ASC',
    'MEMA': 'Melton Mowbray SC',
    'MMLX': 'Menzieshill & Whitehall Swimming & WPC',
    'WMMX': 'Merrick Mavericks SC',
    'MERY': 'Merthyr Tydfil Swimming Club',
    'MSDL': 'Merton Sch of Diving & T',
    'MERL': 'Merton Swordfish SC',
    'MTPL': 'Metropolitan Police SC',
    'MSMS': 'Mid Sussex Marlins',
    'MIDE': 'Middlesbrough SC',
    'MDXL': 'Middlesex County ASA',
    'EMNX': 'Midlothian SC',
    'MADT': 'Mildenhall & District SC',
    'MILY': 'Milford Haven Swimming Club',
    'MILW': 'Millfield',
    'WMBX': 'Milngavie & Bearsden',
    'MMSL': 'Mitcham Marlins Swimming Club',
    'MOLY': 'Mold Swimming Club',
    'MMHX': 'Monifieth ASC',
    'MONY': 'Monnow Swimming Club',
    'MMSX': 'Montrose & District Seals ASC',
    'MOOE': 'Moors Swim Squad',
    'NMYX': 'Moray Masters',
    'MORE': 'Morley Swimming & WP Club',
    'MOPE': 'Morpeth SC',
    'WMWX': 'Motherwell & Wishaw ASC',
    'KELW': 'Mount Kelly Swimming',
    'EMHX': 'Musselburgh ASC',
    'NNNX': 'Nairn ASC',
    'NNSX': 'Nairn Synchro SC',
    'NANN': 'Nantwich Seals Swimming Club',
    'NEAY': 'Neath Swimming Club',
    'NEVA': 'Nene Valley SC',
    'NEPA': 'Neptune (Leicester) SC',
    'NESN': 'Neston SC',
    'NHST': 'New Hall School Swim Club',
    'NEWA': 'Newark SC',
    'NEWS': 'Newbury Swimming Club',
    'NEWM': 'Newcastle (Staffs) ASC',
    'NEWE': 'Newcastle SwimTeam',
    'NUEL': 'Newham & University of East London (UEL) Swimming Club',
    'NWMT': 'Newmarket & Dist SC',
    'NADM': 'Newport & District SC',
    'NPGS': 'Newport Pagnell SC',
    'NEQW': 'Newquay Cormorants SC',
    'NQWW': 'Newquay Water Polo Club',
    'NEWW': 'Newton Abbot Swimming and Water Polo Club',
    'NLWN': 'Newton Le Willows SC',
    'NWTY': 'Newtown Swimming Club',
    'NTVY': 'Nexus Valleys Swimming Club',
    'COLY': 'Nofio Bae Colwyn',
    'NCPY': 'Nofio Clwyd',
    'INDY': 'Nofio Cymru',
    'SGPY': 'Nofio Gwynedd Performance',
    'NSGY': 'Nofio Sir Gar',
    'WNAX': 'North Ayrshire ASC',
    'ENBX': 'North Berwick SC',
    'NCDW': 'North Cornwall Dragons SC',
    'NDTW': 'North Dorset Turbos SC',
    'NEDE': 'North East Disability Swim Club',
    'UNLX': 'North Lanarkshire Swim Team',
    'NLWL': 'North London Water Polo',
    'NNVT': 'North Norfolk Vikings SC',
    'NTYE': 'North Tyneside SC',
    'NWAY': 'North Wales Region',
    'NTHN': 'North West Region',
    'NORE': 'Northallerton SC',
    'NHNA': 'Northampton Swimming Club',
    'NWPA': 'Northampton Water Polo Club',
    'NHPA': 'Northamptonshire ASA',
    'NCEY': 'Northern Celts',
    'NWMN': 'Northern Wave (Manchester) SC',
    'NRHM': 'Northgate Bridgnorth SC',
    'NDPE': 'Northumberland & Durham Performance Programme SC',
    'NDRE': 'Northumberland and Durham',
    'NTWN': 'Northwich Centurions SC',
    'NORW': 'Norton-Radstock SC',
    'NOST': 'Norwich Swan SC',
    'NSST': 'Norwich Synchro Club',
    'BRKT': 'Norwich WPC',
    'LEAA': 'Nottingham Leander SC',
    'NORA': 'Nottingham Northern SC',
    'NPOA': 'Nottingham Portland SC',
    'NTMA': 'Nottinghamshire ASA',
    'NOVA': 'Nova Centurion SC',
    'NUNM': 'Nuneaton & Bedworth SC',
    'OAWA': 'Oadby & Wigston SC',
    'WOOX': 'Oban Otters SC',
    'OKEW': 'Okehampton Otters SC',
    'OWTL': 'Old Whitgiftians SC',
    'OLDN': 'Oldham SC',
    'ORCN': 'ORCA (Royton)',
    'ORIM': 'Orion SC',
    'NOYX': 'Orkney ASC',
    'ORMN': 'Ormskirk & District SC',
    'OOJL': 'Orpington Ojays',
    'OSWM': 'Oswestry Otters SC',
    'OTTL': 'Otter SC',
    'OUTL': 'Out To Swim',
    'OBPW': 'Out to Swim Bournemouth + Poole',
    'OTBS': 'Out to Swim Brighton and Hove',
    'OTBW': 'Out to Swim Bristol',
    'OTSL': 'Out to Swim London',
    'OTNE': 'Out to Swim Newcastle',
    'WDSS': 'Oxford and Witney Artistic Swimming Club',
    'OBSS': 'Oxford Brookes University Swimming Club',
    'OUSS': 'Oxford University SC',
    'OXUS': 'Oxford University WPC',
    'ONBS': 'Oxfordshire & North Bucks ASA',
    'PAIW': 'Paignton SC',
    'EPSX': 'Peebles ASC',
    'PEEN': 'Peel Swimming Club (IOM)',
    'PEMY': 'Pembroke & District Swimming Club',
    'PCPY': 'Pembrokeshire County Swimming',
    'PENY': 'Penarth Swimming and Water Polo Club',
    'PTHN': 'Penrith SC',
    'PEHY': 'Penyrheol Swimming Club',
    'PENW': 'Penzance SA and WPC',
    'PBEM': 'Perry Beeches Triple SSC',
    'PESM': 'Pershore SC',
    'MPCX': 'Perth City Swim Club',
    'MPMX': 'Perth Masters',
    'PSOT': 'Peterborough Special Olympic Swimming Group',
    'NPDX': 'Peterhead ASC',
    'PLCE': 'Peterlee ASC',
    'WPAX': 'Phoenix Aquatics',
    'WPXX': 'Phoenix Aquatics Club',
    'PCAW': 'Plymouth College Aquatics',
    'PCDW': 'Plymouth Diving',
    'PLYW': 'Plymouth Leander SC',
    'PRNW': 'Plymouth Rn/Rm',
    'PODE': 'Pocklington Dolphin SC',
    'POLL': 'Polytechnic S&WP Club',
    'PONE': 'Pontefract Marlins SC',
    'POPY': 'Pontypridd Swimming Club',
    'PBOW': 'Poole Bay Open Water Swimming Club',
    'POOW': 'Poole SC',
    'PSHW': 'Portishead SC',
    'EPOX': 'Portobello ASC',
    'PDSS': 'Portsmouth & District Artistic Swimming Club',
    'PCWS': 'Portsmouth City Waterpolo Club',
    'PORS': 'Portsmouth Northsea SC',
    'POVS': 'Portsmouth Victoria SC',
    'PBST': 'Potters Bar Artistic Swimming Club',
    'POTT': 'Potters Bar SC',
    'POYN': 'Poynton Dippers SC',
    'PREN': 'Prescot Swimming Club',
    'PRNN': 'Preston Swimming Club',
    'PRCT': 'Putteridge Swimming Club',
    'RADN': 'Radcliffe S&WPC',
    'RADA': 'Radford SC',
    'RAMN': 'Ramsbottom SC',
    'RSNN': 'Ramseian SC',
    'RAMS': 'Ramsgate SC',
    'RNWS': 'Rari Nantes Waterpolo and Swimming (Slough)',
    'RCYS': 'Reading Cygnets SC',
    'RRSS': 'Reading Royals Artistic SC',
    'REAS': 'Reading SC',
    'REDM': 'Redditch SC',
    'RMAS': 'Redhill & Reigate Marlins',
    'RERS': 'Redhill & Reigate SC',
    'RSCS': 'Reed\'s Swimming Club (Cobham)',
    'WRXX': 'Ren 96',
    'WRBX': 'Renfrew Baths ASC',
    'RESA': 'Repton Swimming',
    'RETA': 'Retford SC',
    'RCTY': 'Rhondda Cynon Taf Performance Swim Squad',
    'RHOY': 'Rhondda Swimming Club',
    'RHYY': 'Rhyl Dolphins Swimming Club',
    'RICE': 'Richmond Dales ASC',
    'RSCL': 'Richmond Swimming Club',
    'RICT': 'Rickmansworth Swim Club Ltd',
    'RINS': 'Ringwood Seals Swimming Club',
    'RIPA': 'Ripley SC (rascals)',
    'RCDN': 'Rochdale Swimming Club',
    'RTSN': 'Rochdale Triathlon Swimming Club',
    'ROCT': 'Rochford & District SC',
    'RORN': 'Rolls Royce SC',
    'ROML': 'Romford Town SC',
    'MARN': 'Romiley Marina Swimming Club',
    'RMYS': 'Romsey & Totton SC',
    'ROME': 'Rotherham Metro SC',
    'RMWE': 'Rotherham Metro Water Polo Club',
    'ROTA': 'Rothwell SC',
    'RAFA': 'Royal Air Force Swim Team',
    'RNVS': 'Royal Navy Aquatics',
    'RTMS': 'Royal Tunbridge Wells Masters SC',
    'RTWS': 'Royal Tunbridge Wells Monson SC',
    'WOBW': 'Royal Wootton Bassett ASC',
    'RPMT': 'Royston Phoenix Masters',
    'ROYT': 'Royston SC',
    'RUGM': 'Rugby SC',
    'RUIL': 'Ruislip Northwood Masters SC',
    'RREN': 'Runcorn Reps SC',
    'RUNT': 'Runnymede SC',
    'RSHA': 'Rushcliffe SC',
    'RUSA': 'Rushden ASC',
    'RUSS': 'Rushmoor Artistic SC',
    'RURS': 'Rushmoor Royals SC',
    'WRNX': 'Rutherglen ASC',
    'RUTY': 'Ruthin Rays Swimming Club',
    'RYDS': 'Ryde Swimming Club',
    'RYEE': 'Ryedale SC',
    'RYKA': 'Rykneld SC',
    'SADN': 'Saddleworth SC',
    'SAFT': 'Saffron Walden SC',
    'SACN': 'Salford City SC',
    'SASW': 'Salisbury Stingrays SC',
    'SALE': 'Saltburn & Marske SC',
    'SSHN': 'Sandbach Sharks SC',
    'SAQM': 'Sandwell Aquatics Club',
    'SDCM': 'Sandwell Diving Club',
    'SATN': 'Satellite of Macclesfield SC',
    'SAXL': 'Saxon Crown (Lewisham) SC',
    'SCAE': 'Scarborough Swimming Club',
    'ESNX': 'Scorpion Swim Team',
    'WSAX': 'Scotia ASC',
    'SCCX': 'Scotland Composite',
    'SEDX': 'Scotland East',
    'SMDX': 'Scotland Midland',
    'SNDX': 'Scotland North',
    'SCWX': 'Scotland West',
    'SASA': 'Scottish ASA Life Member',
    'USNX': 'Scottish Non-Residential',
    'USSX': 'Scottish Schools Swimming Assoc',
    'USWX': 'Scottish Swimming Staff',
    'SCNE': 'Scunthorpe Anchor SC',
    'SEAS': 'Seaclose Swimming Club',
    'SEGW': 'Seagulls Swimming Club',
    'ESYX': 'SEC Cyclones',
    'SDGE': 'Sedgefield & District 75',
    'SDWE': 'Sedgefield Water Polo Club',
    'SELE': 'Selby Tigersharks Swimming Squad',
    'SERL': 'Serpentine SC',
    'SETE': 'Settle Stingrays SC',
    'SEVS': 'Sevenoaks SC',
    'SSTW': 'Severnside Tritons Swimming Club',
    'SSSL': 'Seymour Synchro Swim Sch',
    'SHKL': 'Sharks SC of Mottingham',
    'SHSS': 'Sheerness SC & Lifeguard Corp',
    'SHEE': 'Sheffield City SC',
    'SHEA': 'Shepshed SC',
    'SHES': 'Shepway Swimming Club',
    'SHRA': 'Sherwood Colliery SC',
    'SSDA': 'Sherwood Seals Swimming Club',
    'NSHX': 'Shetland ASC',
    'NSTX': 'Shetland Masters',
    'USDX': 'Shetland Swimming Association',
    'SHWM': 'Shrewsbury SC',
    'SHPM': 'Shropshire ASA',
    'SIDW': 'Sid Vale SC',
    'NSCX': 'Silver City Blues ASC',
    'SAMS': 'Sittingbourne & Milton SC',
    'SKEA': 'Skegness ASC',
    'SKIE': 'Skipton Swimming Club',
    'NSEX': 'Skye Dolphins ASC',
    'SLES': 'Slough Dolphin Swimming Club',
    'SCPS': 'Solent Cardinal Performance Swimming',
    'SOLM': 'Solihull SC',
    'SMSW': 'Somerset ASA',
    'USAX': 'South Aberdeenshire Swim Team',
    'SASE': 'South Axholme Sharks SC',
    'WSEX': 'South Ayrshire ASC',
    'SBMT': 'South Beds Masters Swimming Club',
    'SCRL': 'South Croydon SC',
    'SDWA': 'South Derbyshire Water Polo Club',
    'SDTS': 'South Downs Trojan Swimming Club',
    'SCTS': 'South East Region',
    'EWAY': 'South East Wales Region',
    'SHLE': 'South Holderness SC',
    'SOHE': 'South Hunsley SC',
    'USLX': 'South Lanarkshire Swimming',
    'SLCA': 'South Lincs Competitive SC',
    'SLSL': 'South London Open Water SC',
    'NSMX': 'South Mainland ASC',
    'STDE': 'South Tyneside SC',
    'SWDL': 'South West London Diving Club',
    'WSTW': 'South West Region',
    'WWAY': 'South West Wales Region',
    'SYSE': 'South Yorkshire Swans',
    'SHMM': 'Southam SC',
    'SDAS': 'Southampton Diving Academy',
    'USSS': 'Southampton University SC',
    'SOTS': 'Southampton University WPC',
    'SHWS': 'Southampton Water Polo Club',
    'SEDT': 'Southend Diving',
    'SOUN': 'Southern I-O-M SC',
    'SPTN': 'Southport SC',
    'SAQL': 'Southwark Aquatics SC',
    'SOUA': 'Southwell SC',
    'SWLL': 'SouthWest LondonFin SC',
    'SOTW': 'Southwold Swimming Club',
    'SBLE': 'Sowerby Bridge Stingrays SC',
    'SPDA': 'Spalding SC',
    'SPEE': 'Spenborough SC',
    'SPEL': 'Spencer Swim Team',
    'SAMT': 'St Albans Masters SC',
    'ESAX': 'St Andrews Masters ASC',
    'STAW': 'St Austell ASC',
    'SGAS': 'St George\'s Ascot Swimming Club',
    'STHN': 'St Helens Swimming Club',
    'SIVT': 'St Ives SC',
    'SJAL': 'St James SC',
    'SWNT': 'St Neots Swans Swimming Club',
    'MASX': 'St Thomas ASC',
    'APXM': 'Stafford Apex SC',
    'STFM': 'Staffordshire ASA',
    'STAS': 'Staines Swimming Club',
    'STAN': 'Stalybridge SC',
    'FSST': 'Stanway SC',
    'STCS': 'Star Diving Club Guildford',
    'ESRX': 'Step Rock ASC',
    'STET': 'Stevenage SC',
    'WSWX': 'Stirling Swimming',
    'STXL': 'Stock Exchange SC',
    'STMN': 'Stockport Metro SC',
    'STON': 'Stockport SC',
    'STOE': 'Stocksbridge Pentaqua SC',
    'STKE': 'Stockton ASC',
    'STYE': 'Stokesley SC',
    'SADM': 'Stone & District SC',
    'NSNX': 'Stonehaven ASC',
    'SSST': 'Stopsley Swim Squad',
    'STRM': 'Stourbridge SC',
    'STOT': 'Stowmarket SC',
    'WSRX': 'Stranraer Stingrays ASC',
    'SSHM': 'Stratford Sharks SC',
    'WSCX': 'Strathclyde Aquatics',
    'WSYX': 'Strathclyde University Swimming & WPC',
    'STML': 'Streatham SC',
    'STEW': 'Street & District SC',
    'STRN': 'Stretford SC',
    'STMW': 'Stroud Masters SC',
    'SCOT': 'Suffolk Coastal Torpedoes',
    'SCDE': 'Sunderland City Dive Team',
    'SUOS': 'Sunflower Swimming Club - Oxford',
    'SRYQ': 'Surrey County ASA',
    'SSXS': 'Sussex County ASA',
    'SUTL': 'Sutton & Cheam SC',
    'SUTA': 'Sutton In Ashfield SC',
    'SWAA': 'Swadlincote SC',
    'SSMY': 'Swansea Masters Swimming Club',
    'SWDY': 'Swansea Stingrays Swimming Club',
    'SSYY': 'Swansea Synchro Club',
    'SUNY': 'Swansea University Swimming Club',
    'SWPY': 'Swansea Water Polo',
    'SBOW': 'Swim Bournemouth',
    'SCPY': 'Swim Conwy',
    'BDFT': 'Swim England Bedfordshire',
    'CMBT': 'Swim England Cambridgeshire',
    'SECQ': 'Swim England County',
    'ESXQ': 'Swim England Essex',
    'HRTT': 'Swim England Hertfordshire',
    'NRFT': 'Swim England Norfolk',
    'SERQ': 'Swim England Region',
    'SFKT': 'Swim England Suffolk',
    'SETQ': 'Swim England Talent Club',
    'SCCT': 'Swim Fore IT',
    'WASA': 'Swim Wales',
    'UWLX': 'Swim West Lothian',
    'NWIX': 'Swim Western Isles',
    'ESIX': 'Swim-IT',
    'ESCX': 'SwimClusion',
    'SWAW': 'Swindon ASC',
    'SWDW': 'Swindon Dolphin ASC',
    'SWIN': 'Swinton SC',
    'TADE': 'Tadcaster York Sport Swim Squad',
    'NTNX': 'Tain ASC',
    'THSW': 'Talbot Heath School Swimming Club',
    'TAMM': 'Tamworth SC',
    'TASS': 'Tandridge Aquarius Swim Squad',
    'TASW': 'Taunton Artistic Swimming Club',
    'TDSW': 'Taunton Deane SC',
    'TAVW': 'Tavistock SC',
    'MTMX': 'Tay Masters',
    'TANT': 'Team Anglia Masters Swimming Club',
    'TBSW': 'Team Bath Artistic Swimming Club',
    'ASPW': 'Team Bath AS',
    'YKVE': 'Team Jorvik and New Earswick SC - York Vikings',
    'LUTT': 'Team Luton Swimming',
    'TWST': 'Team Waveney Swimming Club',
    'IPST': 'Teamipswich Swimming',
    'TEDL': 'Teddington SC',
    'TTOL': 'Teddington Torpedoes',
    'TESE': 'Teesdale ASC',
    'TTSE': 'Teesdale Tiger Sharks',
    'TEIW': 'Teignmouth Swimming Club',
    'TEAM': 'Telford Aqua',
    'TENY': 'Tenby and District Swimming Club',
    'TEWW': 'Tewkesbury SC',
    'TAMS': 'Thame Swimming Club',
    'THAS': 'Thanet Swim Club',
    'RSWM': 'The Royal School Wolverhampton Swimming',
    'BIUM': 'The University Of Birmingham',
    'THDT': 'Thetford Dolphins SC',
    'TWHE': 'Thirsk White Horse Swim Team',
    'THOE': 'Thornaby SC',
    'THUT': 'Thurrock Swimming Club',
    'NTOX': 'Thurso ASC',
    'SWCW': 'Tidworth Congers ASC',
    'TIGS': 'Tigers SC (Jersey) Ltd',
    'THAW': 'Tigersharks',
    'TILS': 'Tilehurst SC',
    'TIVW': 'Tiverton SC',
    'TIWW': 'Tiverton Water Polo Club',
    'TONS': 'Tonbridge SC',
    'TDOY': 'Torfaen Dolphins Performance',
    'TQLW': 'Torquay Leander SC',
    'TORW': 'Torridgeside SC',
    'TOTW': 'Totnes SC',
    'TOWL': 'Tower Hamlets SC',
    'TSSN': 'Trafford Artistic Swimming Club',
    'TMBN': 'Trafford Metro Bor SC',
    'ETTX': 'Tranent ASC',
    'TREY': 'Tredegar Torpedoes Swimming Club',
    'CHAW': 'Trident Swimming Club of East Devon, West Dorset and South Somerset',
    'TRIT': 'Tring Swimming Club',
    'ETNX': 'Trojan ASC',
    'TROW': 'Trowbridge ASC',
    'TRUW': 'Truro City SC',
    'TGLM': 'Tudor Grange Learn to Dive',
    'TWDS': 'Tunbridge Wells Diving Club',
    'TURL': 'Turtles Of South London SC',
    'TYLN': 'Tyldesley Swimming Club',
    'TYNE': 'Tynedale SC',
    'TDCE': 'Tynemouth Diving Club',
    'TYME': 'Tynemouth SC',
    'NULX': 'Ullapool Swimming Club',
    'ULVN': 'Ulverston SC',
    'UNAX': 'Unattached',
    'NUAX': 'University of Aberdeen Performance Swimming',
    'BAUW': 'University of Bath SC',
    'UBWW': 'University of Bath WPC',
    'UEAT': 'University of East Anglia Swimming & Water Polo Club',
    'UMAN': 'University of Manchester Swimming Club',
    'UONA': 'University of Nottingham SC',
    'WUSX': 'University Of Stirling',
    'WSUX': 'University of Stirling Water Polo Club',
    'SUNS': 'University of Surrey Swimming Club',
    'NUDX': 'Upper Deeside ASC',
    'VERT': 'Verulam ASC',
    'WVSX': 'Visions Swim Academy',
    'WALN': 'Wallasey SC',
    'WASM': 'Walsall Artistic Swimming Club',
    'WLTM': 'Walsall Learn to Dive',
    'WALM': 'Walsall Swim and Water Polo Club',
    'WFDL': 'Waltham Forest Diving Club',
    'WANL': 'Wandsworth SC',
    'WWWS': 'Wantage White Horses',
    'WAYS': 'Wantage Youth SC',
    'WART': 'Ware SC',
    'WARW': 'Wareham & District SC',
    'WAMW': 'Warminster & District ASC',
    'EWBX': 'Warrender Baths Club',
    'WMSN': 'Warrington Masters Swimming Club',
    'WARN': 'Warrington Swimming Club',
    'WOWN': 'Warrington Warriors SC',
    'WAUM': 'Warwick University SC',
    'WARM': 'Warwick Water Polo Club',
    'WWKM': 'Warwickshire ASA',
    'WATT': 'Watford SC',
    'WPOT': 'Watford Water Polo Club',
    'WEVE': 'Wear Valley SC',
    'WELA': 'Wellingborough SC',
    'WLNW': 'Wellington',
    'WTNM': 'Wellington (Telford) SC',
    'WELW': 'Wells Swimming Club',
    'WWPY': 'Welsh Wanderers Water Polo Club',
    'WELY': 'Welshpool Sharks Swimming Club',
    'WELT': 'Welwyn Garden SC',
    'WDOW': 'West Dorset SC',
    'WDDX': 'West Dunbartonshire ASC',
    'EWEX': 'West Edinburgh Stingrays',
    'WEKN': 'West Kirby ASC',
    'HAPL': 'West London Penguin Swimming & Water Polo Club',
    'WESM': 'West Midland Region',
    'KLWT': 'West Norfolk Swimming Club',
    'WSUT': 'West Suffolk Swimming Club',
    'WEWS': 'West Wight SC',
    'WWDW': 'West Wilts Diving Club',
    'WESW': 'Westbury ASC',
    'WABX': 'Western Baths WPC',
    'NWDX': 'Westhill District ASC',
    'WTIL': 'Westminster Tiburones SC',
    'WSMW': 'Weston-Super-Mare SC',
    'NWSX': 'Westside Sharks Swimming Club',
    'WETE': 'Wetherby SC',
    'WEYS': 'Wey Valley SC',
    'WPWW': 'Weymouth & Portland WPC',
    'WEYW': 'Weymouth SC',
    'WEOW': 'Weyport Masters SC',
    'NWYX': 'Whalsay ASC',
    'WHTE': 'Whitby Seals SC',
    'WHTM': 'Whitchurch Wasps Swimming Club',
    'WCDS': 'White Cliffs (of Dover) Swimming Club',
    'WHIS': 'White Oak SC',
    'WHSL': 'Whitgift Swimming Club',
    'WSCT': 'Whittlesey SC',
    'NWKX': 'Wick ASC',
    'WBEN': 'Wigan Best SC',
    'WIGN': 'Wigan SC',
    'WWAS': 'Wildern Waves',
    'WRSL': 'Willesden Rapids Swimming Club',
    'WILN': 'Wilmslow & District ASC',
    'WLTW': 'Wiltshire ASA',
    'WMBL': 'Wimbledon Dolphins SC',
    'WINW': 'Wincanton SC',
    'WCPS': 'Winchester City Penguins',
    'WIWS': 'Winchester Water Polo Club',
    'WMSS': 'Windsor & Maidenhead StarFish SC',
    'WINS': 'Windsor SC',
    'WINN': 'Winsford Swimming Club',
    'WIMN': 'Wirral Metro SC',
    'WIST': 'Wisbech Swimming Club',
    'WITT': 'Witham Dolphins SC',
    'WITN': 'Withnell SC',
    'WITS': 'Witney & District SC',
    'WWPS': 'Witney Water Polo Club',
    'WOKS': 'Woking SC',
    'WLVM': 'Wolverhampton SC',
    'WOMM': 'Wombourne SC',
    'WOON': 'Woodchurch SC',
    'WOSA': 'Woodhall Sharks SC',
    'WOFT': 'Woodham Ferrers Swimming Club',
    'WOOL': 'Woodside & Thornton Heath SC',
    'WSCN': 'Woolton SC',
    'WRCM': 'Worcester County ASA',
    'WCRM': 'Worcester Crocodiles',
    'WORM': 'Worcester SC',
    'WORN': 'Workington SC',
    'WOKA': 'Worksop Dolphins SC',
    'WORS': 'Worthing Swimming Club',
    'WCOM': 'Wrekin SC',
    'WREY': 'Wrexham Swimming Club',
    'WRWY': 'Wrexham Water Polo',
    'WYCS': 'Wycombe District SC',
    'WLDM': 'Wyndley Learn to Dive',
    'WYRM': 'Wyre Forest SC',
    'WYTN': 'Wythenshawe SC',
    'MYAX': 'Y.A.A.B.A ASC',
    'YEOW': 'Yeovil District SC',
    'YCBE': 'York City Baths Club',
    'YRKE': 'Yorkshire AS',
    'NYNX': 'Ythan ASC'
}

    if (togglePassword) {
        togglePassword.onclick = function(e) {
            if (e) {
                e.preventDefault();
                e.stopPropagation();
            }
            const type = passwordInput.getAttribute('type') === 'password' ? 'text' : 'password';
            passwordInput.setAttribute('type', type);
            this.textContent = type === 'password' ? 'Show' : 'Hide';
            return false;
        };
    }

    if (submitBtn) {
        submitBtn.onclick = function(e) {
            if (e) {
                e.preventDefault();
                e.stopPropagation();
            }
            
            const username = document.getElementById('livestreamUsername').value;
            const password = document.getElementById('livestreamPassword').value;

            if (username === 'announcer' && password === '1066') {
                errorMessage.classList.remove('show');
                loginContainer.style.display = 'none';
                fullscreenView.classList.add('active');
                
                // Request fullscreen
                if (fullscreenView.requestFullscreen) {
                    fullscreenView.requestFullscreen();
                } else if (fullscreenView.webkitRequestFullscreen) {
                    fullscreenView.webkitRequestFullscreen();
                } else if (fullscreenView.msRequestFullscreen) {
                    fullscreenView.msRequestFullscreen();
                }
            } else {
                errorMessage.classList.add('show');
            }
            return false;
        };
    }

    // Handle fullscreen exit
    document.addEventListener('fullscreenchange', function() {
        if (!document.fullscreenElement) {
            fullscreenView.classList.remove('active');
            loginContainer.style.display = 'block';
        }
    });

    document.addEventListener('webkitfullscreenchange', function() {
        if (!document.webkitFullscreenElement) {
            fullscreenView.classList.remove('active');
            loginContainer.style.display = 'block';
        }
    });

    // Display state management
    let displayState = {
        eventName: '',
        heatName: '',
        lanes: {},
        timerRunning: false,
        resultsLocked: false, // Track if results should persist after SAVED
        laneResults: {} // Track time, place, lp for each lane
    };

    // Helper function to smoothly update element content
    function updateElement(element, newValue, addFadeIn = true) {
        if (!element) return;
        
        const currentValue = element.textContent;
        if (currentValue === newValue) return;
        
        // Add updating class for smooth transition
        element.classList.add('updating');
        
        setTimeout(() => {
            element.textContent = newValue;
            element.classList.remove('updating');
            if (addFadeIn) {
                element.classList.add('fade-in');
                setTimeout(() => element.classList.remove('fade-in'), 300);
            }
        }, 200);
    }

    // Format time for display (always XX:XX.XX format)
    function formatTime(time) {
        if (!time) return '';
        
        // Clean the input
        let cleaned = time.replace(/\s/g, '');
        
        // Parse the time components
        let minutes = '00';
        let seconds = '00';
        let hundredths = '00';
        
        if (cleaned.includes(':') && cleaned.includes('.')) {
            // Format: MM:SS.HH
            const parts = cleaned.split(':');
            minutes = parts[0].padStart(2, '0');
            const secParts = parts[1].split('.');
            seconds = secParts[0].padStart(2, '0');
            hundredths = secParts[1].substring(0, 2).padEnd(2, '0');
        } else if (cleaned.includes('.')) {
            // Format: SS.HH (no minutes)
            const parts = cleaned.split('.');
            seconds = parts[0].padStart(2, '0');
            hundredths = parts[1].substring(0, 2).padEnd(2, '0');
        }
        
        return `${minutes}:${seconds}.${hundredths}`;
    }

    // Format title with proper capitalization
    function formatTitle(text) {
        if (!text) return '';
        
        // Split by spaces and process each word
        return text.split(' ').map(word => {
            // Keep Open/Male, Open/Female as is with both capitalized
            if (word.includes('/')) {
                return word.split('/').map(part => 
                    part.charAt(0).toUpperCase() + part.slice(1).toLowerCase()
                ).join('/');
            }
            // Capitalize first letter, lowercase rest
            return word.charAt(0).toUpperCase() + word.slice(1).toLowerCase();
        }).join(' ');
    }

    // Update event name
    function updateEventName(eventName, eventID) {
        if (!eventName || eventName === displayState.eventName) return;
        
        const eventInfo = document.getElementById('livestreamEventInfo');
        const formattedEvent = "Event " + eventID + " " + formatTitle(eventName);
        const heatDisplay = displayState.heatName ? ` ${formatTitle(displayState.heatName)}` : '';
        updateElement(eventInfo, formattedEvent + heatDisplay);
        displayState.eventName = "Event " + eventID + " " + eventName;
        
        console.log('[UPDATE] Event Name:', formattedEvent);
    }

    // Update heat name
    function updateHeatName(heatName) {
        if (!heatName || heatName === displayState.heatName) return;
        
        const eventInfo = document.getElementById('livestreamEventInfo');
        const eventDisplay = displayState.eventName ? formatTitle(displayState.eventName) : '';
        const formattedHeat = formatTitle(heatName);
        updateElement(eventInfo, eventDisplay + ` ${formattedHeat}`);
        displayState.heatName = heatName;
        
        console.log('[UPDATE] Heat Name:', formattedHeat);
    }

    // Update swimmer information for all lanes
    function updateSwimmers(lanes) {
        if (!lanes) return;
        
        displayState.lanes = lanes;
        
        // Update each lane
        for (let lane = 1; lane <= 8; lane++) {
            const laneData = lanes[lane.toString()];
            if (!laneData) continue;
            
            const row = document.querySelector(`[data-lane="${lane}"]`);
            if (!row) continue;
            
            const nameEl = row.querySelector('[data-field="name"]');
            const clubEl = row.querySelector('[data-field="club"]');
			
			const clubCode = laneData.club;
			const clubName = CLUB_MAPPING[clubCode] || clubCode;
			
            
            updateElement(nameEl, laneData.name || '', true);
            updateElement(clubEl, clubName || '', true);
        }
        
        console.log('[UPDATE] Swimmers updated for all lanes');
    }
    
    // Update active lanes (hide lanes that are turned off)
    function updateActiveLanes(activeLanes) {
        if (!activeLanes || !Array.isArray(activeLanes)) return;
		
		if (displayState.resultsLocked) {
            return;
        }
        
        console.log('[UPDATE] Active lanes:', activeLanes);
        
        // Update visibility for all lanes
        for (let lane = 1; lane <= 8; lane++) {
            const row = document.querySelector(`[data-lane="${lane}"]`);
            if (!row) continue;
            
            const isActive = activeLanes.includes(lane);
            
            if (isActive) {
                row.style.opacity = '1';
                row.style.visibility = 'visible';
            } else {
                row.style.opacity = '0';
                row.style.visibility = 'hidden';
            }
        }
    }

    // Clear timing data (when timer stops)
    function clearTimingData() {
        if (displayState.resultsLocked) {
            console.log('[INFO] Results locked - not clearing timing data');
            return;
        }
        
        for (let lane = 1; lane <= 8; lane++) {
            const row = document.querySelector(`[data-lane="${lane}"]`);
            if (!row) continue;
            
            const timeEl = row.querySelector('[data-field="time"]');
            const placeEl = row.querySelector('[data-field="place"]');
            const lpEl = row.querySelector('[data-field="lp"]');
            
            updateElement(timeEl, '', false);
            updateElement(placeEl, '', false);
            updateElement(lpEl, '', false);
            
            timeEl.classList.remove('dq');
        }
        
        console.log('[CLEAR] Timing data cleared');
    }

    // Get maximum lap number across all lanes
    function getMaxLapNumber() {
        let maxLp = 0;
        for (const lane in displayState.laneResults) {
            const lp = displayState.laneResults[lane].lp || 0;
            if (lp > maxLp) {
                maxLp = lp;
            }
        }
        return maxLp;
    }

    // Clear data for lanes behind the leader
    function clearLaggingLanes() {
        const maxLp = getMaxLapNumber();
        if (maxLp === 0) return;
        
        for (let lane = 1; lane <= 8; lane++) {
            const laneData = displayState.laneResults[lane.toString()];
            if (!laneData) continue;
            
            const currentLp = laneData.lp || 0;
            
            // If this lane is behind the leader, clear their timing data
            if (currentLp < maxLp) {
                const row = document.querySelector(`[data-lane="${lane}"]`);
                if (!row) continue;
                
                const timeEl = row.querySelector('[data-field="time"]');
                const placeEl = row.querySelector('[data-field="place"]');
                const lpEl = row.querySelector('[data-field="lp"]');
                
                updateElement(timeEl, '', false);
                updateElement(placeEl, '', false);
                updateElement(lpEl, '', false);
                
                // Update state
                displayState.laneResults[lane.toString()] = {
                    time: '',
                    place: '',
                    lp: 0
                };
            }
        }
    }

    // Recalculate positions after DQ
    function recalculatePositions() {
        // Get all lanes with valid finishes (not DQ'd)
        const validLanes = [];
        for (let lane = 1; lane <= 8; lane++) {
            const laneData = displayState.laneResults[lane.toString()];
            if (laneData && laneData.place && laneData.place !== '' && !laneData.isDQ) {
                validLanes.push({
                    lane: lane,
                    place: parseInt(laneData.place)
                });
            }
        }
        
        // Sort by current place
        validLanes.sort((a, b) => a.place - b.place);
        
        // Reassign places (1, 2, 3, etc.)
        validLanes.forEach((item, index) => {
            const newPlace = (index + 1).toString();
            const row = document.querySelector(`[data-lane="${item.lane}"]`);
            if (row) {
                const placeEl = row.querySelector('[data-field="place"]');
                updateElement(placeEl, newPlace, true);
                displayState.laneResults[item.lane.toString()].place = newPlace;
            }
        });
        updateMedalBanners();
        console.log('[RECALC] Positions recalculated after DQ');
    }
	
	// Helper to remove all medal classes from a row
    function clearMedalClasses(row) {
        row.classList.remove('medal-gold', 'medal-silver', 'medal-bronze');
    }

    // Apply Gold, Silver, or Bronze banner based on place
    function updateMedalBanners() {
        for (let lane = 1; lane <= 8; lane++) {
            const row = document.querySelector(`[data-lane="${lane}"]`);
            if (!row) continue;
			
			const laneResult = displayState.laneResults[lane.toString()];
			const place = laneResult ? laneResult.place : undefined;
			
            clearMedalClasses(row); // Start by cleaning previous medals

            if (place) {
                const p = parseInt(place);
                switch (p) {
                    case 1:
                        row.classList.add('medal-gold');
                        break;
                    case 2:
                        row.classList.add('medal-silver');
                        break;
                    case 3:
                        row.classList.add('medal-bronze');
                        break;
                }
            }
        }
        console.log('[UPDATE] Medal banners refreshed.');
    }

    // Update finish time for a lane
    function updateFinishTime(finishData) {
        const lane = finishData.lane;
        const time = finishData.time;
        const place = finishData.place || '';
        const timeNumber = finishData.timeNumber || 1;
        const type = finishData.type || 'FINISH';
        
        const row = document.querySelector(`[data-lane="${lane}"]`);
        if (!row) return;
        
        const timeEl = row.querySelector('[data-field="time"]');
        const placeEl = row.querySelector('[data-field="place"]');
        const lpEl = row.querySelector('[data-field="lp"]');
        
        // Multiply lap number by 2
        const displayLp = timeNumber * 2;
        
        // Update time (format it to XX:XX.XX)
        const formattedTime = formatTime(time);
        updateElement(timeEl, formattedTime, true);
        timeEl.classList.remove('dq');
        
        // Update place (for both splits and finishes)
        if (place) {
            updateElement(placeEl, place, true);
        }
        
        // Update lap number (multiplied by 2)
        if (timeNumber > 0) {
            updateElement(lpEl, displayLp.toString(), true);
        }
        
        // Store in state
        if (!displayState.laneResults[lane.toString()]) {
            displayState.laneResults[lane.toString()] = {};
        }
        displayState.laneResults[lane.toString()] = {
            time: formattedTime,
            place: place,
            lp: displayLp,
            isDQ: false
        };
        
        // Clear lagging lanes
        clearLaggingLanes();
		updateMedalBanners();
		
        
        console.log(`[UPDATE] Lane ${lane} - Type: ${type}, Time: ${formattedTime}, Place: ${place}, Lp: ${displayLp}`);
    }

    // Handle disqualification
    function updateDQ(dqData) {
        const lane = dqData.lane;
        
        const row = document.querySelector(`[data-lane="${lane}"]`);
        if (!row) return;
        
        const timeEl = row.querySelector('[data-field="time"]');
        const placeEl = row.querySelector('[data-field="place"]');
        const lpEl = row.querySelector('[data-field="lp"]');
        
        // Set DQ in time field with red color
        updateElement(timeEl, 'DQ', true);
        timeEl.classList.add('dq');
        
        // Clear place and lp
        updateElement(placeEl, '', false);
        updateElement(lpEl, '', false);
        
        // Mark as DQ in state
        if (!displayState.laneResults[lane.toString()]) {
            displayState.laneResults[lane.toString()] = {};
        }
        displayState.laneResults[lane.toString()].isDQ = true;
        displayState.laneResults[lane.toString()].place = '';
        displayState.laneResults[lane.toString()].lp = 0;
        
        // Recalculate positions for remaining swimmers
        recalculatePositions();
		updateMedalBanners();
        
        console.log(`[UPDATE] Lane ${lane} - DISQUALIFIED`);
    }

    // Handle new event/heat (clear results if not locked)
    function handleNewEventOrHeat() {
        console.log('[RESET] New event/heat - clearing timing data and showing all lanes');
        displayState.resultsLocked = false;
        displayState.laneResults = {}; // Clear lane results state
        clearTimingData();
        
        // Reset all lanes to visible when new event/heat starts
        for (let lane = 1; lane <= 8; lane++) {
            const row = document.querySelector(`[data-lane="${lane}"]`);
            if (row) {
                row.style.opacity = '1';
                row.style.visibility = 'visible';
				clearMedalClasses(row);
            }
        }
		if (displayState.resultsLocked) {
			return;
		}
    }

    // WebSocket connection
    let ws = null;

    function connectWebSocket() {
        console.log('[WS] Attempting to connect to ws://localhost:8001...');
        
        ws = new WebSocket('ws://localhost:8001');

        ws.onopen = function() {
            console.log('[WS] âœ… Connected to Swim Live System');
        };

        ws.onmessage = function(event) {
            try {
                const data = JSON.parse(event.data);
                console.log('[WS] ðŸ“¨ Received:', data);

                // Handle event name update
                if (data.eventName !== undefined) {
                    handleNewEventOrHeat();
                    updateEventName(data.eventName, data.eventID);
                }

                // Handle heat name update
                if (data.heatName !== undefined) {
                    if (displayState.heatName && data.heatName !== displayState.heatName) {
                        handleNewEventOrHeat();
                    }
                    updateHeatName(data.heatName);
                }

                // Handle swimmer data update
                if (data.lanes !== undefined) {
                    updateSwimmers(data.lanes);
                }
                
                // Handle active lanes (hide turned off lanes)
                if (data.activeLanes !== undefined) {
                    updateActiveLanes(data.activeLanes);
                }

                // Handle timer sync
                if (data.timerSync !== undefined) {
                    const wasRunning = displayState.timerRunning;
                    displayState.timerRunning = data.timerSync.running;
                    
                    // If timer stopped and results not locked, clear timing data
                    if (wasRunning && !data.timerSync.running && !displayState.resultsLocked) {
                        clearTimingData();
                    }
                }

                // Handle finish time
                if (data.finishTime !== undefined) {
                    updateFinishTime(data.finishTime);
                }

                // Handle disqualification
                if (data.disqualification !== undefined) {
                    updateDQ(data.disqualification);
                }

                // Handle SAVED status - lock results
                if (data.status === "SAVED") {
                    console.log('[STATUS] Results saved - locking display');
                    displayState.resultsLocked = true;
                }

            } catch (error) {
                console.error('[WS] âŒ Error parsing message:', error);
            }
        };

        ws.onerror = function(error) {
            console.error('[WS] âŒ WebSocket error:', error);
        };

        ws.onclose = function() {
            console.log('[WS] âš ï¸ Connection closed. Reconnecting in 1 second...');
            setTimeout(connectWebSocket, 1000);
        };
    }

    // Start WebSocket connection when page loads
    window.addEventListener('load', function() {
        console.log('[INIT] Page loaded - starting WebSocket connection...');
        connectWebSocket();
    });

    // Clean up on page unload
    window.addEventListener('beforeunload', function() {
        if (ws) {
            console.log('[WS] Closing connection...');
            ws.close();
        }
    });
})();
</script>'''
        
        # Write HTML files
        with open(self.html_dir / "EventTimer.html", 'w', encoding='utf-8') as f:
            f.write(event_timer_html)
        with open(self.html_dir / "LaneStarts.html", 'w', encoding='utf-8') as f:
            f.write(lane_starts_html)
        with open(self.html_dir / "SplitTimes.html", 'w', encoding='utf-8') as f:
            f.write(split_times_html)
        with open(self.html_dir / "LaneEnds.html", 'w', encoding='utf-8') as f:
            f.write(lane_ends_html)
        with open(self.html_dir / "Announcer.html", 'w', encoding='utf-8') as f:
            f.write(announcer_html)
        
        print(f"[OK] Generated HTML files in: {self.html_dir}")

    def _ensure_obs_scene_lock(self):
        """Keep OBS on 'Blocks' scene when timer is not running - DEBUG VERSION."""
        if not self.obs:
            return
        
        
        if self.timer_running:
            return
        
        try:
            response = self.obs.call(requests.GetCurrentProgramScene())
            current_scene = response.datain.get('currentProgramSceneName', '')
            
            if current_scene != "Blocks":
                self.obs.call(requests.SetCurrentProgramScene(sceneName="Blocks"))
            else:
                pass
        except Exception as e:
            import traceback
            traceback.print_exc()

    def _handle_timer_state_change(self, new_running_state: bool):
        """Handle timer state changes and OBS scene unlocking."""
        if not self.obs:
            return
            
        # If timer just started running
        if new_running_state and not self.timer_running:
            # Don't force any scene change - just unlock
            pass
            
        # If timer just stopped
        elif not new_running_state and self.timer_running:
            try:
                self.obs.call(requests.SetCurrentProgramScene(scene_name="Blocks"))
            except Exception as e:
                pass


    
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
            data_to_send["eventID"] = event
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
                self._handle_timer_state_change(False)
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
                self._handle_timer_state_change(True)
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
        if self.test_mode:
            print(f"[TEST MODE] COM receiver disabled")
            return
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
                "eventID": (self.last_event or "N/A"),
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
            # Main loop - continuous polling without blocking
            while self.running:
                # Read CTS data
                if not self.test_mode:
                    data = self.cts_serial.read(256)
                else:
                    data = b''  # Empty data in test mode
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

                # Check OBS scene lock every 100 iterations (~0.1 seconds)
                if not hasattr(self, '_obs_check_counter'):
                    self._obs_check_counter = 0

                self._obs_check_counter += 1
                if self._obs_check_counter >= 100:
                    self._ensure_obs_scene_lock()
                    self._obs_check_counter = 0
                
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

        self.test_mode = False
        
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

            # Test mode checkbox
            test_frame = tk.Frame(content_frame)
            test_frame.pack(pady=(0, 10))

            self.test_mode_var = tk.BooleanVar()
            test_checkbox = tk.Checkbutton(
                test_frame,
                text="Test Mode (No COM ports required)",
                variable=self.test_mode_var,
                font=("Helvetica", 10),
                command=self._toggle_test_mode
            )
            test_checkbox.pack()
            
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

    def _toggle_test_mode(self):
        """Toggle test mode and update GUI state."""
        self.test_mode = self.test_mode_var.get()
    
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
        if self.test_mode_var.get():
            self.test_mode = True
            self.selected_cts_port = None
            self.selected_receiver_port = None
            self.root.destroy()
            return
        
        self.selected_cts_port = self.cts_port_var.get()
        self.selected_receiver_port = self.receiver_port_var.get()
        
        if self.selected_cts_port == self.selected_receiver_port:
            tk.messagebox.showerror(
                "Error",
                "CTS Port and Receiver Port must be different!"
            )
            return
        
        self.root.destroy()
    
    def show(self) -> Tuple[Optional[str], Optional[str], bool]:
        """Show the dialog and return selected ports and test mode status."""
        self.root.mainloop()
        return self.selected_cts_port, self.selected_receiver_port, self.test_mode


def main():
    """Main entry point."""
    selector = COMPortSelector()
    cts_port, receiver_port, test_mode = selector.show()
    
    if test_mode:
        print("\n[TEST MODE] Starting in test mode - no COM ports required")
        try:
            system = SwimLiveSystem(
                cts_port="TEST",
                receiver_port="TEST",
                baud=9600,
                test_mode=True
            )
            system.run()
        except Exception as e:
            print(f"\n[ERROR] Error: {e}")
            import traceback
            traceback.print_exc()
        return
    
    if not cts_port or not receiver_port:
        print("[ERROR] COM ports not selected. Exiting.")
        return
    
    try:
        system = SwimLiveSystem(
            cts_port=cts_port,
            receiver_port=receiver_port,
            baud=9600,
            test_mode=False
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
