import sys
import threading
import time
import csv
import io
import os
from collections import deque
from datetime import datetime
from typing import Dict, List
import json
import serial
import serial.tools.list_ports
import webview
import uuid
import ctypes

# def check_mac_address():
#     # TARGET_MAC = "9C-6B-00-65-72-14"
#     TARGET_MAC = "C0-BF-BE-33-08-8F"
#     current_mac_int = uuid.getnode()
#     current_mac = ':'.join(['{:02x}'.format((current_mac_int >> ele) & 0xff) 
#                             for ele in range(0, 8*6, 8)][::-1]).upper()
#     current_mac = current_mac.replace(":", "-")

#     if current_mac != TARGET_MAC:
#         ctypes.windll.user32.MessageBoxW(0, f"Unauthorized hardware.\nID: {current_mac}", "Security Error", 16)
#         sys.exit(1)

# if __name__ == '__main__':
#     check_mac_address()
#     print("Starting Scientech Technologies Dashboard...")

import warnings
warnings.filterwarnings('ignore', message='__abstractmethods__')

# ----------------------------- Helpers -----------------------------

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

def moving_average(values: deque, window_size=5):
    """Apply moving average smoothing"""
    if len(values) < window_size:
        return values[-1] if values else 0
    return sum(list(values)[-window_size:]) / window_size

def convert_thrust_g_to_n(thrust_grams):
    """Convert thrust from grams and apply scaling based on range"""
    if thrust_grams <0:
        return 0
    elif 0 <= thrust_grams < 0.150:
        return thrust_grams * (1000*4.4)
    elif 0.150 <= thrust_grams < 0.300:
        return thrust_grams * (1000*4.4)
    elif 0.300 <= thrust_grams < 0.450:
        return thrust_grams * (1000*4.4)
    else:
        return thrust_grams * (1000 *4.4)

def parse_scientech_line(line: str):
    """
    Parses Scientech data format: thrust,temperature,rpm
    Example: 46.69,65.05,4050
    Thrust is in grams, convert with scaling
    """
    s = line.strip()
    if not s:
        return None
    
    parts = s.split(',')
    if len(parts) != 3:
        return None
    
    try:
        thrust_grams = float(parts[0])
        temperature = float(parts[1])
        rpm = float(parts[2])
        
        thrust = convert_thrust_g_to_n(thrust_grams)
        
    except Exception:
        return None
    
    return {
        "thrust": thrust,
        "temperature": temperature,
        "rpm": rpm,
        "timestamp": datetime.now().isoformat()
    }

# ----------------------------- Data History -----------------------------

class DataHistory:
    def __init__(self):
        self.history_size = 500
        self.thrust_history = deque(maxlen=self.history_size)
        self.temp_history = deque(maxlen=self.history_size)
        self.rpm_history = deque(maxlen=self.history_size)
        
        for _ in range(self.history_size):
            self.thrust_history.append(0)
            self.temp_history.append(0)
            self.rpm_history.append(0)
    
    def update(self, data):
        """Update history with new data"""
        self.thrust_history.append(data['thrust'])
        self.temp_history.append(data['temperature'])
        self.rpm_history.append(data['rpm'])
        
        return {
            'thrust': list(self.thrust_history),
            'temperature': list(self.temp_history),
            'rpm': list(self.rpm_history)
        }

# ----------------------------- Data Logger -----------------------------

class DataLogger:
    def __init__(self):
        self.data_buffer = []
        self.fieldnames = ['timestamp', 'thrust', 'temperature', 'rpm']
        self.is_logging = False
        self.start_time = None
        self.max_records = 1000000
        
    def start(self):
        self.data_buffer = []
        self.is_logging = True
        self.start_time = datetime.now()
        
    def stop(self):
        self.is_logging = False
        
    def add_data(self, data: Dict):
        if not self.is_logging:
            return

        if len(self.data_buffer) >= self.max_records:
            remove_count = int(self.max_records * 0.1)
            del self.data_buffer[:remove_count]
            
        record = data.copy()
        record['timestamp'] = datetime.now().isoformat()
        self.data_buffer.append(record)
        
    def get_csv(self):
        """Generate CSV file from logged data"""
        if not self.data_buffer:
            return None
            
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=self.fieldnames)
        writer.writeheader()
        writer.writerows(self.data_buffer)
        
        return output.getvalue()
        
    def clear(self):
        self.data_buffer = []
        self.is_logging = False

# ----------------------------- Serial Worker -----------------------------

class SerialWorker:
    def __init__(self, callback):
        self.callback = callback
        self._stop = False
        self._thread = None
        self._ser = None
        self.is_connected = False
        self.current_port = None
        self.current_baud = None
        self._last_disconnect_time = 0
        self._disconnect_notified = False
        
        # Smoothing buffers
        self.thrust_buffer = deque(maxlen=5)
        self.temp_buffer = deque(maxlen=5)
        self.rpm_buffer = deque(maxlen=5)
        
        # Current smoothed values
        self.smoothed_thrust = 0.0
        self.smoothed_temp = 0.0
        self.smoothed_rpm = 0.0
        
        # Data rate calculation
        self.data_count = 0
        self.last_rate_time = time.time()
        self.current_rate = 0.0
        
        # Last data for clients that connect later
        self.last_data = None
        
        # Data history for graphs
        self.data_history = DataHistory()

    def start(self, port: str, baud: int):
        self.stop()
        self._stop = False
        self._disconnect_notified = False
        self.current_port = port
        self.current_baud = baud
        self._thread = threading.Thread(target=self._run, args=(port, baud), daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True
        self.is_connected = False
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None

    def send_command(self, cmd: str):
        """Send command to serial port"""
        if self._ser and self._ser.is_open:
            try:
                self._ser.write(f"{cmd}\n".encode())
                print(f"Sent command: {cmd}")
                return True
            except Exception as e:
                print(f"Error sending command: {e}")
                return False
        return False

    def _run(self, port: str, baud: int):
        buf = b""
        try:
            self._ser = serial.Serial(port, baudrate=baud, timeout=0.5)
            self.is_connected = True
            if self.callback:
                self.callback({
                    'type': 'connected',
                    'message': f"Connected: {port} @ {baud}",
                    'port': port,
                    'baud': baud
                })
        except Exception as e:
            if self.callback:
                self.callback({
                    'type': 'error',
                    'message': f"Serial open failed: {e}",
                    'error_type': 'connection_error'
                })
            return
        
        while not self._stop:
            try:
                if not self._ser.is_open:
                    if not self._disconnect_notified:
                        self._disconnect_notified = True
                        self.is_connected = False
                        if self.callback:
                            self.callback({
                                'type': 'disconnected',
                                'message': 'Device disconnected',
                                'was_unexpected': True
                            })
                    break
                
                chunk = self._ser.read(512)
                if not chunk:
                    time.sleep(0.01)
                    continue
                buf += chunk

                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    s = line.decode("utf-8", errors="ignore").strip()
                    pkt = parse_scientech_line(s)
                    if pkt:
                        smoothed_data = self._apply_smoothing(pkt)
                        
                        self.data_count += 1
                        now = time.time()
                        if now - self.last_rate_time >= 1.0:
                            self.current_rate = self.data_count / (now - self.last_rate_time)
                            self.data_count = 0
                            self.last_rate_time = now
                        
                        smoothed_data['rate'] = self.current_rate
                        
                        history_data = self.data_history.update(smoothed_data)
                        smoothed_data['history'] = history_data
                        
                        self.last_data = smoothed_data.copy()
                        
                        if self.callback:
                            self.callback({
                                'type': 'new_data',
                                'data': smoothed_data
                            })
                        
                        if logger.is_logging:
                            logger.add_data(pkt)
                        
                        time.sleep(0.02)

            except serial.SerialException as e:
                if not self._disconnect_notified:
                    self._disconnect_notified = True
                    self.is_connected = False
                    if self.callback:
                        self.callback({
                            'type': 'disconnected',
                            'message': 'Device disconnected unexpectedly',
                            'was_unexpected': True
                        })
                    break
            except Exception as e:
                current_time = time.time()
                if current_time - self._last_disconnect_time > 1.0:
                    self._last_disconnect_time = current_time
                    if self.callback:
                        self.callback({
                            'type': 'error',
                            'message': f"Serial error: {str(e)[:100]}",
                            'error_type': 'serial_error'
                        })
                time.sleep(0.1)
        
        if not self._disconnect_notified:
            self.is_connected = False
            if self._ser:
                try:
                    self._ser.close()
                except:
                    pass
            self._ser = None
            if self.callback:
                self.callback({
                    'type': 'disconnected',
                    'message': 'Disconnected',
                    'was_unexpected': False
                })

    def _apply_smoothing(self, data: Dict) -> Dict:
        """Apply smoothing to Scientech data"""
        self.thrust_buffer.append(data['thrust'])
        self.temp_buffer.append(data['temperature'])
        self.rpm_buffer.append(data['rpm'])
        
        self.smoothed_thrust = moving_average(self.thrust_buffer, 5)
        self.smoothed_temp = moving_average(self.temp_buffer, 5)
        self.smoothed_rpm = moving_average(self.rpm_buffer, 5)

        return {
            'thrust': self.smoothed_thrust,
            'temperature': self.smoothed_temp,
            'rpm': self.smoothed_rpm,
            'timestamp': datetime.now().isoformat()
        }

# Global instances
logger = DataLogger()
serial_worker = None

# HTML Template with all fixes
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>Scientech Technologies</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 10px;
            overflow-x: hidden;
            height: 100vh;
        }
        
        .dashboard-container {
            display: flex;
            height: calc(100vh - 20px);
            gap: 10px;
        }
        
        .sidebar {
            width: 240px;
            background: rgba(255, 255, 255, 0.95);
            border-radius: 12px;
            padding: 15px 12px;
            box-shadow: 0 8px 20px rgba(0,0,0,0.15);
            display: flex;
            flex-direction: column;
        }
        
        .company-name {
            text-align: center;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 2px solid #e2e8f0;
        }
        
        .company-name h2 {
            color: #2d3748;
            font-size: 18px;
            font-weight: 700;
        }
        
        .parameter-display {
            flex: 1;
        }
        
        .parameter-card {
            background: #f7fafc;
            border-radius: 8px;
            padding: 12px;
            margin-bottom: 12px;
            border-left: 3px solid #667eea;
            box-shadow: 0 4px 6px rgba(0,0,0,0.05);
        }
        
        .parameter-card h3 {
            font-size: 13px;
            color: #718096;
            margin-bottom: 6px;
            font-weight: 600;
        }
        
        .parameter-value {
            font-size: 20px;
            font-weight: 700;
            color: #2d3748;
            font-family: 'Courier New', monospace;
            display: flex;
            align-items: baseline;
        }
        
        .parameter-unit {
            font-size: 12px;
            color: #718096;
            margin-left: 3px;
        }
        
        .controls-section {
            margin-top: 20px;
            padding-top: 20px;
            border-top: 2px solid #e2e8f0;
        }
        
        .control-button {
            width: 100%;
            padding: 15px;
            margin-bottom: 10px;
            border: none;
            border-radius: 8px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s;
        }
        
        .tare-button {
            background: #e53e3e;
            color: white;
        }
        
        .tare-button:hover {
            background: #c53030;
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(229, 62, 62, 0.2);
        }
        
        .logging-section {
            margin-top: 20px;
            background: #f7fafc;
            border-radius: 10px;
            padding: 20px;
            border: 1px solid #e2e8f0;
        }
        
        .logging-title {
            font-size: 16px;
            font-weight: 600;
            color: #2d3748;
            margin-bottom: 15px;
            text-align: center;
        }
        
        .logging-button {
            width: 100%;
            padding: 12px;
            margin-bottom: 8px;
            border: none;
            border-radius: 6px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s;
        }
        
        .start-button {
            background: #38a169;
            color: white;
        }
        
        .start-button:hover {
            background: #2f855a;
            transform: translateY(-1px);
        }
        
        .stop-button {
            background: #e53e3e;
            color: white;
        }
        
        .stop-button:hover {
            background: #c53030;
            transform: translateY(-1px);
        }
        
        .download-button {
            background: #667eea;
            color: white;
        }
        
        .download-button:hover {
            background: #5a67d8;
            transform: translateY(-1px);
        }
        
        .logging-button:disabled {
            background: #ccc;
            cursor: not-allowed;
            transform: none;
            box-shadow: none;
        }
        
        .main-content {
            flex: 1;
            display: flex;
            flex-direction: column;
            gap: 10px;
            min-width: 0;
        }
        
        .serial-card {
            background: rgba(255, 255, 255, 0.95);
            border-radius: 12px;
            padding: 15px;
            box-shadow: 0 8px 20px rgba(0,0,0,0.15);
        }
        
        .card-title {
            font-size: 18px;
            font-weight: 600;
            color: #2d3748;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 2px solid #e2e8f0;
        }
        
        .serial-controls {
            display: flex;
            gap: 15px;
            align-items: center;
            flex-wrap: wrap;
        }
        
        select, input, button {
            padding: 12px 18px;
            border: 2px solid #ddd;
            border-radius: 8px;
            font-size: 14px;
            transition: all 0.3s;
        }
        
        select:focus, input:focus {
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
        }
        
        button {
            background: #667eea;
            color: white;
            border: none;
            cursor: pointer;
            font-weight: 600;
            min-width: 120px;
        }
        
        button:hover {
            background: #5a67d8;
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(0,0,0,0.1);
        }
        
        button:disabled {
            background: #ccc;
            cursor: not-allowed;
            transform: none;
            box-shadow: none;
        }
        
        button.danger {
            background: #e53e3e;
        }
        
        button.danger:hover {
            background: #c53030;
        }
        
        button.success {
            background: #38a169;
        }
        
        button.warning {
            background: #d69e2e;
        }
        
        button.warning:hover {
            background: #b7791f;
        }
        
        button.emergency {
            background: #e53e3e;
            font-weight: 700;
        }
        
        button.emergency:hover {
            background: #c53030;
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(229, 62, 62, 0.3);
        }
        
        .control-section {
            background: rgba(255, 255, 255, 0.95);
            border-radius: 12px;
            padding: 15px;
            box-shadow: 0 8px 20px rgba(0,0,0,0.15);
        }
        
        .control-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 15px;
        }
        
        .control-panel {
            display: flex;
            flex-direction: column;
            gap: 15px;
        }
        
        .control-panel:first-child {
            display: flex;
            flex-direction: column;
            gap: 5px;
        }
        
        .slider-container {
            background: #f7fafc;
            padding: 15px;
            border-radius: 8px;
            border: 1px solid #e2e8f0;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            height: 250px;
            position: relative;
        }
        
        .slider-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 15px;
            width: 100%;
        }
        
        .slider-value-display {
            font-size: 24px;
            font-weight: 700;
            color: #2d3748;
            text-align: center;
            margin: 10px 0;
            font-family: 'Courier New', monospace;
            background: #fff;
            padding: 8px 15px;
            border-radius: 6px;
            border: 2px solid #667eea;
            min-width: 80px;
            cursor: pointer;
            transition: all 0.2s;
        }
        
        .slider-value-display:hover {
            background: #f7fafc;
            border-color: #5a67d8;
        }
        
        .slider-value-display:focus {
            outline: none;
            border-color: #4a50c7;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.2);
        }
        
        .vertical-slider-container {
            display: flex;
            align-items: center;
            justify-content: center;
            width: 100%;
            height: 150px;
            margin: 15px 0;
            position: relative;
        }
        
        .slider-track {
            width: 8px;
            height: 150px;
            background: linear-gradient(to top, #667eea, #764ba2);
            border-radius: 4px;
            position: relative;
            margin: 0 30px;
            box-shadow: inset 0 0 5px rgba(0,0,0,0.2);
        }
        
        .slider-thumb {
            width: 30px;
            height: 30px;
            background: #e53e3e;
            border-radius: 50%;
            position: absolute;
            left: 50%;
            transform: translateX(-50%);
            cursor: grab;
            border: 4px solid white;
            box-shadow: 0 4px 8px rgba(0,0,0,0.3);
            transition: background 0.2s, transform 0.1s;
            z-index: 10;
            top: 120px;
        }
        
        .slider-thumb:hover {
            background: #c53030;
            transform: translateX(-50%) scale(1.1);
        }
        
        .slider-thumb.active {
            background: #4a50c7;
            cursor: grabbing;
            transform: translateX(-50%) scale(1.15);
            box-shadow: 0 6px 12px rgba(0,0,0,0.4);
        }
        
        .slider-thumb.disabled {
            background: #a0aec0;
            cursor: not-allowed;
            transform: translateX(-50%);
            box-shadow: none;
        }
        
        .slider-labels {
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            height: 150px;
            color: #4a5568;
            font-size: 12px;
            font-weight: 600;
        }
        
        .slider-labels-left {
            text-align: right;
            padding-right: 10px;
        }
        
        .slider-labels-right {
            text-align: left;
            padding-left: 10px;
        }
        
        .slider-controls {
            display: flex;
            flex-direction: column;
            gap: 10px;
            width: 100%;
            margin-top: 15px;
        }
        
        .checkbox-row {
            display: flex;
            gap: 15px;
            margin-bottom: 10px;
        }
        
        .checkbox-container {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 8px;
            background: #fff;
            border-radius: 6px;
            border: 1px solid #e2e8f0;
            flex: 1;
        }
        
        .checkbox-container input[type="checkbox"] {
            width: 20px;
            height: 20px;
            cursor: pointer;
            accent-color: #667eea;
        }
        
        .checkbox-container label {
            font-size: 14px;
            font-weight: 600;
            color: #2d3748;
            cursor: pointer;
            flex: 1;
        }
        
        .stop-button-compact {
            background: #e53e3e;
            color: white;
            width: 100%;
            padding: 12px;
            border: none;
            border-radius: 6px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s;
            margin-top: 10px;
        }
        
        .stop-button-compact:hover {
            background: #c53030;
            transform: translateY(-2px);
            box-shadow: 0 4px 8px rgba(229, 62, 62, 0.3);
        }
        
        .stop-button-compact:disabled {
            background: #ccc;
            cursor: not-allowed;
            transform: none;
            box-shadow: none;
        }
        
        .control-panel:last-child {
            display: flex;
            flex-direction: column;
            gap: 15px;
        }
        
        .remark-box {
            background: #f7fafc;
            padding: 20px;
            border-radius: 8px;
            border: 1px solid #e2e8f0;
            font-size: 14px;
            color: #4a5568;
            line-height: 1.6;
            flex: 1;
            min-height: 150px;
        }
        
        .calibration-button {
            background: #d69e2e;
            color: white;
            width: 100%;
            padding: 15px;
            border-radius: 8px;
            border: none;
            font-size: 18px;
            font-weight: 700;
            cursor: pointer;
            transition: all 0.3s;
        }
        
        .calibration-button:hover {
            background: #b7791f;
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(214, 158, 46, 0.3);
        }
        
        .graphs-section {
            flex: 1;
            background: rgba(255, 255, 255, 0.95);
            border-radius: 12px;
            padding: 15px;
            box-shadow: 0 8px 20px rgba(0,0,0,0.15);
            display: flex;
            flex-direction: column;
            min-height: 300px;
        }
        
        .graphs-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
            flex-shrink: 0;
        }
        
        .graphs-container {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 10px;
            flex: 1;
            min-height: 0;
        }
        
        .graph-card {
            background: #1a202c;
            border-radius: 8px;
            padding: 10px;
            display: flex;
            flex-direction: column;
            min-height: 0;
        }
        
        .graph-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 10px;
            flex-shrink: 0;
        }
        
        .graph-title {
            font-size: 14px;
            font-weight: 600;
            color: #fff;
        }
        
        .graph-checkbox {
            display: flex;
            align-items: center;
            gap: 6px;
        }
        
        .graph-checkbox label {
            color: #fff;
            font-size: 11px;
            cursor: pointer;
        }
        
        .graph-plot {
            flex: 1;
            background: rgba(0, 0, 0, 0.3);
            border-radius: 5px;
            min-height: 0;
            position: relative;
        }
        
        .graph-canvas {
            width: 100%;
            height: 100%;
        }
        
        .notification-container {
            position: fixed;
            top: 20px;
            right: 20px;
            z-index: 1000;
            max-width: 400px;
        }
        
        .notification {
            background: white;
            padding: 15px 20px;
            border-radius: 10px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.1);
            margin-bottom: 10px;
            border-left: 5px solid #667eea;
            transform: translateX(100%);
            opacity: 0;
            animation: slideIn 0.3s forwards;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .notification.success {
            border-left-color: #38a169;
        }
        
        .notification.error {
            border-left-color: #e53e3e;
        }
        
        .notification.warning {
            border-left-color: #d69e2e;
        }
        
        .notification.info {
            border-left-color: #4299e1;
        }
        
        .notification-content {
            flex: 1;
        }
        
        .notification-title {
            font-weight: 600;
            margin-bottom: 5px;
        }
        
        .notification-message {
            color: #718096;
            font-size: 14px;
        }
        
        .notification-close {
            background: none;
            border: none;
            color: #a0aec0;
            font-size: 20px;
            cursor: pointer;
            padding: 0 5px;
        }
        
        .modal-overlay {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.5);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 2000;
            opacity: 0;
            visibility: hidden;
            transition: all 0.3s;
        }
        
        .modal-overlay.active {
            opacity: 1;
            visibility: visible;
        }
        
        .modal {
            background: white;
            border-radius: 12px;
            padding: 25px;
            width: 90%;
            max-width: 400px;
            box-shadow: 0 20px 40px rgba(0,0,0,0.2);
            transform: translateY(-20px);
            transition: transform 0.3s;
        }
        
        .modal-overlay.active .modal {
            transform: translateY(0);
        }
        
        .modal-title {
            font-size: 20px;
            font-weight: 600;
            color: #2d3748;
            margin-bottom: 15px;
        }
        
        .modal-message {
            color: #718096;
            margin-bottom: 20px;
            line-height: 1.5;
        }
        
        .modal-input {
            width: 100%;
            padding: 12px;
            border: 2px solid #e2e8f0;
            border-radius: 6px;
            font-size: 16px;
            margin-bottom: 20px;
            text-align: center;
        }
        
        .modal-input:focus {
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
        }
        
        .modal-buttons {
            display: flex;
            gap: 10px;
            justify-content: flex-end;
        }
        
        .modal-btn {
            padding: 10px 20px;
            border: none;
            border-radius: 6px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }
        
        .modal-btn.cancel {
            background: #e2e8f0;
            color: #4a5568;
        }
        
        .modal-btn.cancel:hover {
            background: #cbd5e0;
        }
        
        .modal-btn.confirm {
            background: #667eea;
            color: white;
        }
        
        .modal-btn.confirm:hover {
            background: #5a67d8;
        }
        
        @keyframes slideIn {
            to {
                transform: translateX(0);
                opacity: 1;
            }
        }
        
        @keyframes slideOut {
            to {
                transform: translateX(100%);
                opacity: 0;
            }
        }
        
        .status-indicator {
            display: inline-block;
            width: 10px;
            height: 10px;
            border-radius: 50%;
            margin-right: 8px;
        }
        
        .status-connected {
            background: #38a169;
            box-shadow: 0 0 10px #38a169;
        }
        
        .status-disconnected {
            background: #e53e3e;
        }
        
        .status-logging {
            background: #d69e2e;
            box-shadow: 0 0 10px #d69e2e;
        }
    </style>
</head>
<body>
    <div class="dashboard-container">
        <!-- Left Sidebar -->
        <div class="sidebar">
            <div class="company-name">
                <h2>Scientech Technologies</h2>
            </div>
            
            <div class="parameter-display">
                <div class="parameter-card">
                    <h3>Thrust</h3>
                    <div class="parameter-value"><span id="thrust-value">0.00</span><span class="parameter-unit"> gm</span></div>
                </div>
                
                <div class="parameter-card">
                    <h3>Temp</h3>
                    <div class="parameter-value"><span id="temperature-value">0.00</span><span class="parameter-unit"> °C</span></div>
                </div>
                
                <div class="parameter-card">
                    <h3>RPM</h3>
                    <div class="parameter-value"><span id="rpm-value">0</span><span class="parameter-unit"> RPM</span></div>
                </div>
            </div>
            
            <!-- Tare Button Section -->
            <div class="controls-section">
                <button class="control-button tare-button" id="tare-btn">Tare</button>
            </div>
            
            <!-- Data Logging Section -->
            <div class="logging-section">
                <div class="logging-title">Data Logging</div>
                <button class="logging-button start-button" id="start-logging">Start Logging</button>
                <button class="logging-button stop-button" id="stop-logging" disabled>Stop Logging</button>
                <button class="logging-button download-button" id="download-log" disabled>Download CSV</button>
            </div>
        </div>
        
        <!-- Main Content -->
        <div class="main-content">
            <!-- Serial Connection Card -->
            <div class="serial-card">
                <div class="card-title">Serial Connection</div>
                <div class="serial-controls">
                    <select id="port-select">
                        <option value="">Select Port</option>
                    </select>
                    <input type="number" id="baud-input" value="115200" min="1200" max="2000000">
                    <button id="refresh-ports">Refresh Ports</button>
                    <button id="connect-btn">Connect</button>
                    <button id="disconnect-btn" disabled>Disconnect</button>
                    <button class="warning" id="start-btn" disabled>Start</button>
                    <button class="emergency" id="stop-btn" disabled>Stop</button>
                </div>
                <div style="margin-top: 10px; font-size: 12px; color: #718096;">
                    Status: <span id="connection-status"><span class="status-indicator status-disconnected"></span>Disconnected</span>
                </div>
            </div>
            
            <!-- Control Section -->
            <div class="control-section">
                <div class="card-title">Control Panel</div>
                <div class="control-grid">
                    <!-- Left Panel: Vertical Slider controls -->
                    <div class="control-panel">
                        <div class="slider-container">
                            <div class="slider-header">
                                <div class="checkbox-row">
                                    <div class="checkbox-container">
                                        <input type="checkbox" id="slider-enable">
                                        <label for="slider-enable">Enable</label>
                                    </div>
                                    <div class="checkbox-container">
                                        <input type="checkbox" id="safe-mode" checked>
                                        <label for="safe-mode">Safe Mode</label>
                                    </div>
                                </div>
                                <div class="slider-value-display" id="slider-value" contenteditable="true" data-default="0%">0%</div>
                            </div>
                            
                            <div class="vertical-slider-container">
                                <div class="slider-labels slider-labels-left">
                                    <div>100%</div>
                                    <div>75%</div>
                                    <div>50%</div>
                                    <div>25%</div>
                                    <div>0%</div>
                                </div>
                                
                                <div class="slider-track" id="slider-track">
                                    <div class="slider-thumb disabled" id="slider-thumb"></div>
                                </div>
                                
                                <div class="slider-labels slider-labels-right">
                                    <div>100%</div>
                                    <div>75%</div>
                                    <div>50%</div>
                                    <div>25%</div>
                                    <div>0%</div>
                                </div>
                            </div>
                            
                            <div class="slider-controls">
                                <button class="stop-button-compact" id="slider-stop" disabled>STOP</button>
                            </div>
                        </div>
                    </div>
                    
                    <!-- Right Panel: Remark and Calibration -->
                    <div class="control-panel">
                        <div class="remark-box">
                            <strong>System Control Remark:</strong><br>
                            • Use the vertical slider to control output value (0-100%)<br>
                            • Slider is disabled by default - check "Enable Slider" to activate<br>
                            • Click on the percentage value to manually enter a value<br>
                            • With Safe Mode ON: Shows confirmation before sending value<br>
                            • With Safe Mode OFF: Sends value immediately when slider is released<br>
                             <span style="color: #e53e3e; font-weight: bold;">• Make sure to remove the propeller before calibrating the ESC</span><br>
                            • Use calibration button to calibrate the system<br>
                            • Press the STOP button for emergency stop<br>

                        </div>
                        <button class="calibration-button" id="calibrate-btn">Calibration</button>
                    </div>
                </div>
            </div>
            
            <!-- Graphs Section -->
            <div class="graphs-section">
                <div class="graphs-header">
                    <div class="card-title">Real-time Monitoring</div>
                    <div class="data-rate">Data Rate: <span id="data-rate">0.0</span> Hz</div>
                </div>
                
                <div class="graphs-container">
                    <div class="graph-card">
                        <div class="graph-header">
                            <div class="graph-title">Thrust</div>
                            <div class="graph-checkbox">
                                <input type="checkbox" id="thrust-graph-toggle" checked>
                                <label for="thrust-graph-toggle">Show</label>
                            </div>
                        </div>
                        <div id="thrust-graph" class="graph-plot">
                            <canvas id="thrust-canvas" class="graph-canvas"></canvas>
                        </div>
                    </div>
                    
                    <div class="graph-card">
                        <div class="graph-header">
                            <div class="graph-title">Temperature</div>
                            <div class="graph-checkbox">
                                <input type="checkbox" id="temperature-graph-toggle" checked>
                                <label for="temperature-graph-toggle">Show</label>
                            </div>
                        </div>
                        <div id="temperature-graph" class="graph-plot">
                            <canvas id="temperature-canvas" class="graph-canvas"></canvas>
                        </div>
                    </div>
                    
                    <div class="graph-card">
                        <div class="graph-header">
                            <div class="graph-title">RPM</div>
                            <div class="graph-checkbox">
                                <input type="checkbox" id="rpm-graph-toggle" checked>
                                <label for="rpm-graph-toggle">Show</label>
                            </div>
                        </div>
                        <div id="rpm-graph" class="graph-plot">
                            <canvas id="rpm-canvas" class="graph-canvas"></canvas>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    
    <!-- Notification Container -->
    <div class="notification-container" id="notification-container"></div>
    
    <!-- Modal for value confirmation -->
    <div class="modal-overlay" id="value-modal">
        <div class="modal">
            <div class="modal-title">Confirm Value</div>
            <div class="modal-message">You are about to send the following value to the controller:</div>
            <input type="number" id="modal-value-input" class="modal-input" min="0" max="100" step="1" value="0">
            <div class="modal-buttons">
                <button class="modal-btn cancel" id="modal-cancel">Cancel</button>
                <button class="modal-btn confirm" id="modal-confirm">Send</button>
            </div>
        </div>
    </div>
    
    <script>
    // Global variables
    let isConnected = false;
    let isLogging = false;
    let disconnectNotificationShown = false;
    
    // Graph data
    const graphData = {
        thrust: new Array(500).fill(0),
        temperature: new Array(500).fill(0),
        rpm: new Array(500).fill(0)
    };
    
    // Graph colors
    const graphColors = {
        thrust: '#FF6B6B',
        temperature: '#4ECDC4',
        rpm: '#FFD166'
    };
    
    // Graph ranges
    const graphRanges = {
        thrust: { min: 0, max: 1000 },
        temperature: { min: 0, max: 100 },
        rpm: { min: 0, max: 10000 }
    };
    
    // Vertical slider variables
    let isDragging = false;
    let currentSliderValue = 0;
    let sliderTrack = null;
    let sliderThumb = null;
    let lastSentValue = null;
    let isEditingValue = false;
    let pendingValueToSend = null;
    let trackClickedValue = null;
    
    // Modal elements
    const valueModal = document.getElementById('value-modal');
    const modalValueInput = document.getElementById('modal-value-input');
    const modalCancelBtn = document.getElementById('modal-cancel');
    const modalConfirmBtn = document.getElementById('modal-confirm');
    
    // Initialize communication with Python
    function initPyWebView() {
        console.log('PyWebView API initialized');
    }
    
    // Notification system
    function showNotification(title, message, type = 'info', duration = 3000) {
        const container = document.getElementById('notification-container');
        const notification = document.createElement('div');
        notification.className = `notification ${type}`;
        notification.innerHTML = `
            <div class="notification-content">
                <div class="notification-title">${title}</div>
                <div class="notification-message">${message}</div>
            </div>
            <button class="notification-close" onclick="this.parentElement.remove()">×</button>
        `;
        container.appendChild(notification);
        
        if (duration > 0) {
            setTimeout(() => {
                notification.style.animation = 'slideOut 0.3s forwards';
                setTimeout(() => notification.remove(), 300);
            }, duration);
        }
        
        return notification;
    }
    
    // Show modal for value confirmation
    function showValueModal(value) {
        pendingValueToSend = value;
        modalValueInput.value = value;
        valueModal.classList.add('active');
        modalValueInput.focus();
        modalValueInput.select();
    }
    
    // Hide modal
    function hideValueModal() {
        valueModal.classList.remove('active');
        pendingValueToSend = null;
    }
    
    // Handle modal confirm
    modalConfirmBtn.addEventListener('click', () => {
        const value = parseInt(modalValueInput.value);
        if (!isNaN(value) && value >= 0 && value <= 100) {
            hideValueModal();
            updateSliderValue(value);
            // Send value silently - no notification
            sendSliderValueSilently(value);
        }
    });
    
    // Handle modal cancel
    modalCancelBtn.addEventListener('click', () => {
        hideValueModal();
        // If we cancelled a track click, revert to previous value
        if (trackClickedValue !== null) {
            updateSliderValue(trackClickedValue);
            trackClickedValue = null;
        }
    });
    
    // Handle Enter key in modal input
    modalValueInput.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') {
            modalConfirmBtn.click();
        }
    });
    
    // Handle Escape key to close modal
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && valueModal.classList.contains('active')) {
            hideValueModal();
            if (trackClickedValue !== null) {
                updateSliderValue(trackClickedValue);
                trackClickedValue = null;
            }
        }
    });
    
    // Handle serial data from Python
    function handleSerialData(data) {
        updateParameterDisplays(data);
        
        if (data.history) {
            updateGraphData(data.history);
            drawAllGraphs();
        }
    }
    
    // Handle serial status from Python
    function handleSerialStatus(status) {
        console.log('Serial status:', status);
        
        if (status.type === 'connected') {
            isConnected = true;
            disconnectNotificationShown = false;
            
            document.getElementById('connect-btn').disabled = true;
            document.getElementById('disconnect-btn').disabled = false;
            document.getElementById('start-btn').disabled = false;
            document.getElementById('stop-btn').disabled = false;
            
            const statusElement = document.getElementById('connection-status');
            statusElement.innerHTML = '<span class="status-indicator status-connected"></span>Connected';
            
            updateSliderState();
            
            // Show connected notification only
            showNotification('Connected', status.message, 'success');
            
        } else if (status.type === 'disconnected') {
            if (!disconnectNotificationShown) {
                disconnectNotificationShown = true;
                isConnected = false;
                
                document.getElementById('connect-btn').disabled = false;
                document.getElementById('disconnect-btn').disabled = true;
                document.getElementById('start-btn').disabled = true;
                document.getElementById('stop-btn').disabled = true;
                
                const statusElement = document.getElementById('connection-status');
                statusElement.innerHTML = '<span class="status-indicator status-disconnected"></span>Disconnected';
                
                document.getElementById('slider-enable').checked = false;
                updateSliderState();
                
                if (status.was_unexpected) {
                    showNotification('Device Disconnected', 'The USB device was disconnected unexpectedly', 'warning');
                } else {
                    showNotification('Disconnected', 'Disconnected successfully', 'info');
                }
            }
        } else if (status.type === 'error') {
            showNotification('Error', status.message, 'error');
        }
    }
    
    // Handle logging status from Python
    function handleLoggingStatus(status) {
        isLogging = status.is_logging;
        const stopBtn = document.getElementById('stop-logging');
        const startBtn = document.getElementById('start-logging');
        const downloadBtn = document.getElementById('download-log');
        
        if (isLogging) {
            stopBtn.disabled = false;
            startBtn.disabled = true;
            downloadBtn.disabled = true;
            showNotification('Logging Started', 'Data logging has started', 'success');
        } else {
            stopBtn.disabled = true;
            startBtn.disabled = false;
            downloadBtn.disabled = !status.has_data;
            if (status.has_data) {
                showNotification('Logging Stopped', 'Data logging has stopped', 'info');
            }
        }
    }
    
    // Handle download completion
    function handleDownloadComplete(result) {
        if (result.success) {
            const downloadLink = document.createElement('a');
            downloadLink.href = result.filepath;
            downloadLink.download = result.filename;
            downloadLink.style.display = 'none';
            document.body.appendChild(downloadLink);
            downloadLink.click();
            document.body.removeChild(downloadLink);
            
            showNotification('Data Saved', result.message, 'success', 5000);
            const downloadBtn = document.getElementById('download-log');
            downloadBtn.disabled = !result.success;
        } else {
            showNotification('Download Failed', result.message, 'error');
        }
    }
    
    // Update graph data
    function updateGraphData(history) {
        if (history.thrust) {
            graphData.thrust = history.thrust;
            const maxThrust = Math.max(...history.thrust);
            graphRanges.thrust.max = Math.max(maxThrust * 1.1, 100);
        }
        if (history.temperature) {
            graphData.temperature = history.temperature;
            const maxTemp = Math.max(...history.temperature);
            const minTemp = Math.min(...history.temperature);
            graphRanges.temperature.min = Math.min(minTemp * 0.9, 0);
            graphRanges.temperature.max = Math.max(maxTemp * 1.1, 100);
        }
        if (history.rpm) {
            graphData.rpm = history.rpm;
            const maxRPM = Math.max(...history.rpm);
            graphRanges.rpm.max = Math.max(maxRPM * 1.1, 1000);
        }
    }
    
    // Draw a single graph with axis labels
    function drawGraph(canvasId, data, color, range, title) {
        const canvas = document.getElementById(canvasId);
        if (!canvas) return;
        
        const ctx = canvas.getContext('2d');
        const width = canvas.width;
        const height = canvas.height;
        
        // Clear canvas
        ctx.clearRect(0, 0, width, height);
        
        // Set background
        ctx.fillStyle = 'rgba(0, 0, 0, 0.3)';
        ctx.fillRect(0, 0, width, height);
        
        // Calculate padding for axis labels
        const leftPadding = 40;
        const rightPadding = 20;
        const topPadding = 20;
        const bottomPadding = 30;
        const graphWidth = width - leftPadding - rightPadding;
        const graphHeight = height - topPadding - bottomPadding;
        
        // Draw grid
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.1)';
        ctx.lineWidth = 0.5;
        
        // Vertical grid lines
        for (let i = 0; i <= 10; i++) {
            const x = leftPadding + (graphWidth / 10) * i;
            ctx.beginPath();
            ctx.moveTo(x, topPadding);
            ctx.lineTo(x, topPadding + graphHeight);
            ctx.stroke();
        }
        
        // Horizontal grid lines
        for (let i = 0; i <= 5; i++) {
            const y = topPadding + (graphHeight / 5) * i;
            ctx.beginPath();
            ctx.moveTo(leftPadding, y);
            ctx.lineTo(leftPadding + graphWidth, y);
            ctx.stroke();
        }
        
        // Draw Y-axis labels (ordinate)
        ctx.fillStyle = '#fff';
        ctx.font = '11px Arial';
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';
        
        for (let i = 0; i <= 5; i++) {
            const y = topPadding + (graphHeight / 5) * i;
            const value = range.max - (i / 5) * (range.max - range.min);
            
            // Format value based on graph type
            let label;
            if (canvasId === 'thrust-canvas') {
                label = value.toFixed(0) + ' gm';
            } else if (canvasId === 'temperature-canvas') {
                label = value.toFixed(0) + ' °C';
            } else if (canvasId === 'rpm-canvas') {
                label = value.toFixed(0);
            } else {
                label = value.toFixed(0);
            }
            
            ctx.fillText(label, leftPadding - 5, y);
        }
        
        // Draw graph
        if (data.length < 2) return;
        
        const dataRange = range.max - range.min;
        if (dataRange <= 0) return;
        
        ctx.strokeStyle = color;
        ctx.lineWidth = 2;
        ctx.fillStyle = color + '40';
        ctx.beginPath();
        
        // Start path
        const firstX = leftPadding;
        const firstY = topPadding + graphHeight - ((data[0] - range.min) / dataRange) * graphHeight;
        ctx.moveTo(firstX, firstY);
        
        // Draw line
        for (let i = 1; i < data.length; i++) {
            const x = leftPadding + (graphWidth / (data.length - 1)) * i;
            const y = topPadding + graphHeight - ((data[i] - range.min) / dataRange) * graphHeight;
            ctx.lineTo(x, y);
        }
        
        ctx.stroke();
        
        // Fill under the curve
        ctx.lineTo(leftPadding + graphWidth, topPadding + graphHeight);
        ctx.lineTo(leftPadding, topPadding + graphHeight);
        ctx.closePath();
        ctx.fill();
        
        // Draw current value indicator
        const lastValue = data[data.length - 1];
        const lastY = topPadding + graphHeight - ((lastValue - range.min) / dataRange) * graphHeight;
        
        // Draw value circle
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(leftPadding + graphWidth, lastY, 5, 0, Math.PI * 2);
        ctx.fill();
        
        // Draw current value text (top right corner)
        ctx.fillStyle = '#fff';
        ctx.font = '12px Arial';
        ctx.textAlign = 'right';
        
        // Format current value based on graph type
        let currentValueText;
        if (canvasId === 'thrust-canvas') {
            currentValueText = lastValue.toFixed(2) + ' gm';
        } else if (canvasId === 'temperature-canvas') {
            currentValueText = lastValue.toFixed(2) + ' °C';
        } else if (canvasId === 'rpm-canvas') {
            currentValueText = Math.round(lastValue).toString();
        } else {
            currentValueText = lastValue.toFixed(2);
        }
        
        ctx.fillText(currentValueText, width - 10, topPadding + 15);
        
        // Draw X-axis (time axis) at the bottom
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.5)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(leftPadding, topPadding + graphHeight);
        ctx.lineTo(leftPadding + graphWidth, topPadding + graphHeight);
        ctx.stroke();
        
        // Draw Y-axis on the left
        ctx.beginPath();
        ctx.moveTo(leftPadding, topPadding);
        ctx.lineTo(leftPadding, topPadding + graphHeight);
        ctx.stroke();
    }
    
    // Draw all graphs
    function drawAllGraphs() {
        if (document.getElementById('thrust-graph-toggle').checked) {
            drawGraph('thrust-canvas', graphData.thrust, graphColors.thrust, graphRanges.thrust, 'Thrust');
        }
        if (document.getElementById('temperature-graph-toggle').checked) {
            drawGraph('temperature-canvas', graphData.temperature, graphColors.temperature, graphRanges.temperature, 'Temperature');
        }
        if (document.getElementById('rpm-graph-toggle').checked) {
            drawGraph('rpm-canvas', graphData.rpm, graphColors.rpm, graphRanges.rpm, 'RPM');
        }
    }
    
    // Update parameter displays
    function updateParameterDisplays(data) {
        document.getElementById('thrust-value').textContent = data.thrust.toFixed(2);
        document.getElementById('temperature-value').textContent = data.temperature.toFixed(2);
        document.getElementById('rpm-value').textContent = Math.round(data.rpm);
        if (data.rate) {
            document.getElementById('data-rate').textContent = data.rate.toFixed(1);
        }
    }
    
    // Initialize vertical slider
    function initVerticalSlider() {
        sliderTrack = document.getElementById('slider-track');
        sliderThumb = document.getElementById('slider-thumb');
        const sliderValueDisplay = document.getElementById('slider-value');
        
        if (!sliderTrack || !sliderThumb || !sliderValueDisplay) return;
        
        updateSliderThumb(0);
        
        // Mouse events for thumb
        sliderThumb.addEventListener('mousedown', startDrag);
        
        // Mouse events for track
        sliderTrack.addEventListener('mousedown', trackClick);
        
        // Touch events for mobile
        sliderThumb.addEventListener('touchstart', startDragTouch);
        sliderTrack.addEventListener('touchstart', trackClickTouch);
        
        // Global mouse events
        document.addEventListener('mousemove', doDrag);
        document.addEventListener('mouseup', stopDrag);
        
        // Global touch events
        document.addEventListener('touchmove', doDragTouch);
        document.addEventListener('touchend', stopDrag);
        document.addEventListener('touchcancel', stopDrag);
        
        // Enable/disable checkbox
        document.getElementById('slider-enable').addEventListener('change', updateSliderState);
        
        // Safe mode checkbox
        document.getElementById('safe-mode').addEventListener('change', function() {
            console.log('Safe mode:', this.checked);
        });
        
        // Stop button
        document.getElementById('slider-stop').addEventListener('click', stopSlider);
        
        // Manual value input - click on value display
        sliderValueDisplay.addEventListener('click', function() {
            if (!isEditingValue && document.getElementById('slider-enable').checked && isConnected) {
                isEditingValue = true;
                const currentValue = parseInt(this.textContent.replace('%', ''));
                showValueModal(currentValue);
            }
        });
        
        // Handle Enter key in editable display
        sliderValueDisplay.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                const value = parseInt(this.textContent.replace('%', ''));
                if (!isNaN(value) && value >= 0 && value <= 100) {
                    updateSliderValue(value);
                    this.blur();
                    isEditingValue = false;
                    
                    // Show modal for confirmation
                    showValueModal(value);
                }
            } else if (e.key === 'Escape') {
                this.textContent = currentSliderValue + '%';
                this.blur();
                isEditingValue = false;
            }
        });
        
        // Handle blur on editable display
        sliderValueDisplay.addEventListener('blur', function() {
            const value = parseInt(this.textContent.replace('%', ''));
            if (!isNaN(value) && value >= 0 && value <= 100) {
                updateSliderValue(value);
            } else {
                this.textContent = currentSliderValue + '%';
            }
            isEditingValue = false;
        });
    }
    
    function updateSliderState() {
        const enabled = document.getElementById('slider-enable').checked && isConnected;
        const sliderThumb = document.getElementById('slider-thumb');
        const stopBtn = document.getElementById('slider-stop');
        
        console.log('Slider state update - Enabled:', enabled, 'Connected:', isConnected);
        
        if (sliderThumb) {
            if (enabled) {
                sliderThumb.classList.remove('disabled');
                sliderThumb.style.cursor = 'grab';
                sliderThumb.style.opacity = '1';
            } else {
                sliderThumb.classList.add('disabled');
                sliderThumb.style.cursor = 'not-allowed';
                sliderThumb.style.opacity = '0.5';
            }
        }
        
        stopBtn.disabled = !enabled;
    }
    
    function startDrag(e) {
        e.preventDefault();
        if (!document.getElementById('slider-enable').checked || !isConnected) return;
        
        isDragging = true;
        sliderThumb.classList.add('active');
    }
    
    function startDragTouch(e) {
        e.preventDefault();
        if (!document.getElementById('slider-enable').checked || !isConnected) return;
        
        isDragging = true;
        sliderThumb.classList.add('active');
    }
    
    function trackClick(e) {
        if (!document.getElementById('slider-enable').checked || !isConnected) return;
        
        const rect = sliderTrack.getBoundingClientRect();
        const y = e.clientY - rect.top;
        
        let value = Math.round((150 - y) / 150 * 100);
        value = Math.max(0, Math.min(100, value));
        
        // Store the clicked value and update display
        trackClickedValue = value;
        updateSliderValue(value);
        
        // DON'T show modal on click - only update visual
        // Modal will be shown when released (in stopDrag function)
    }
    
    function trackClickTouch(e) {
        if (!document.getElementById('slider-enable').checked || !isConnected) return;
        
        const rect = sliderTrack.getBoundingClientRect();
        const y = e.touches[0].clientY - rect.top;
        
        let value = Math.round((150 - y) / 150 * 100);
        value = Math.max(0, Math.min(100, value));
        
        trackClickedValue = value;
        updateSliderValue(value);
    }
    
    function doDrag(e) {
        if (!isDragging) return;
        
        const rect = sliderTrack.getBoundingClientRect();
        const y = e.clientY - rect.top;
        
        let value = Math.round((150 - y) / 150 * 100);
        value = Math.max(0, Math.min(100, value));
        
        updateSliderValue(value);
    }
    
    function doDragTouch(e) {
        if (!isDragging) return;
        
        const rect = sliderTrack.getBoundingClientRect();
        const y = e.touches[0].clientY - rect.top;
        
        let value = Math.round((150 - y) / 150 * 100);
        value = Math.max(0, Math.min(100, value));
        
        updateSliderValue(value);
    }
    
    function stopDrag() {
        if (isDragging || trackClickedValue !== null) {
            // We were either dragging or clicked the track
            isDragging = false;
            sliderThumb.classList.remove('active');
            
            if (document.getElementById('slider-enable').checked && isConnected) {
                const safeMode = document.getElementById('safe-mode').checked;
                
                if (safeMode) {
                    // With safe mode: show modal on release
                    showValueModal(currentSliderValue);
                } else {
                    // Without safe mode: send immediately on release (silently)
                    sendSliderValueSilently(currentSliderValue);
                }
            }
            
            // Reset track click value
            trackClickedValue = null;
        }
    }
    
    function updateSliderValue(value) {
        currentSliderValue = value;
        document.getElementById('slider-value').textContent = value + '%';
        updateSliderThumb(value);
    }
    
    function updateSliderThumb(value) {
        const clampedValue = Math.max(0, Math.min(100, value));
        const position = 150 - (clampedValue / 100) * 150;
        if (sliderThumb) {
            sliderThumb.style.top = position + 'px';
        }
    }
    
    // Send slider value silently (no notification)
    function sendSliderValueSilently(value = null) {
        const valueToSend = value !== null ? value : currentSliderValue;
        
        if (isConnected && window.pywebview && window.pywebview.api.send_serial) {
            window.pywebview.api.send_serial(valueToSend.toString())
                .then(response => {
                    console.log('Slider value sent silently:', valueToSend);
                    lastSentValue = valueToSend;
                    // No notification for slider values
                })
                .catch(error => {
                    console.error('Failed to send slider value:', error);
                    // Only show error if send fails
                    showNotification('Error', 'Failed to send slider value', 'error');
                });
        } else {
            showNotification('Error', 'Not connected to serial port', 'error');
        }
    }
    
    // Send command with notification (for calibration, tare, etc.)
    function sendCommandWithNotification(cmd, notificationMessage) {
        if (isConnected && window.pywebview && window.pywebview.api.send_serial) {
            window.pywebview.api.send_serial(cmd)
                .then(response => {
                    console.log('Command sent:', cmd);
                    showNotification('Command Sent', notificationMessage, 'success');
                })
                .catch(error => {
                    console.error('Failed to send command:', error);
                    showNotification('Error', 'Failed to send command', 'error');
                });
        } else {
            showNotification('Error', 'Not connected to serial port', 'error');
        }
    }
    
    function stopSlider() {
        if (isConnected && window.pywebview && window.pywebview.api.send_serial) {
            const safeMode = document.getElementById('safe-mode').checked;
            
            if (safeMode) {
                // With safe mode: show modal for stop
                showValueModal(0);
            } else {
                // Without safe mode: send immediately (silently)
                updateSliderValue(0);
                sendSliderValueSilently(0);
            }
        }
    }
    
    function toggleGraph() {
        drawAllGraphs();
    }
    
    // Initialize graph canvases
    function initGraphCanvases() {
        const graphIds = ['thrust-canvas', 'temperature-canvas', 'rpm-canvas'];
        
        graphIds.forEach(id => {
            const canvas = document.getElementById(id);
            if (canvas) {
                const container = canvas.parentElement;
                canvas.width = container.clientWidth;
                canvas.height = container.clientHeight;
            }
        });
        
        drawAllGraphs();
    }
    
    // JavaScript to Python calls
    function refreshPorts() {
        if (window.pywebview && window.pywebview.api.get_ports) {
            window.pywebview.api.get_ports()
                .then(ports => {
                    console.log('Ports received:', ports);
                    const select = document.getElementById('port-select');
                    select.innerHTML = '<option value="">Select Port</option>';
                    ports.forEach(port => {
                        const option = document.createElement('option');
                        option.value = port;
                        option.textContent = port;
                        select.appendChild(option);
                    });
                    
                    if (ports.length === 0) {
                        showNotification('No Ports Found', 'No serial ports detected. Connect a device and refresh.', 'warning');
                    }
                })
                .catch(error => {
                    console.error('Error getting ports:', error);
                    // Don't show notification on initial load
                });
        }
    }
    
    function connectSerial() {
        const port = document.getElementById('port-select').value;
        const baud = document.getElementById('baud-input').value;
        
        if (!port) {
            showNotification('Warning', 'Please select a port', 'warning');
            return;
        }
        
        if (window.pywebview && window.pywebview.api.connect) {
            // Don't show "Connecting" notification
            window.pywebview.api.connect(port, parseInt(baud))
                .then(response => {
                    console.log('Connect response:', response);
                    // Connected notification will be shown by handleSerialStatus
                })
                .catch(error => {
                    console.error('Connect error:', error);
                    showNotification('Error', 'Connection failed: ' + error, 'error');
                });
        }
    }
    
    function disconnectSerial() {
        if (window.pywebview && window.pywebview.api.disconnect) {
            window.pywebview.api.disconnect()
                .then(response => {
                    console.log('Disconnect response:', response);
                    // Disconnected notification will be shown by handleSerialStatus
                })
                .catch(error => {
                    console.error('Disconnect error:', error);
                    showNotification('Error', 'Disconnect failed: ' + error, 'error');
                });
        }
    }
    
    function startLogging() {
        if (window.pywebview && window.pywebview.api.start_logging) {
            window.pywebview.api.start_logging()
                .then(response => {
                    console.log('Start logging response:', response);
                })
                .catch(error => {
                    console.error('Start logging error:', error);
                    showNotification('Error', 'Failed to start logging: ' + error, 'error');
                });
        }
    }
    
    function stopLogging() {
        if (window.pywebview && window.pywebview.api.stop_logging) {
            window.pywebview.api.stop_logging()
                .then(response => {
                    console.log('Stop logging response:', response);
                })
                .catch(error => {
                    console.error('Stop logging error:', error);
                    showNotification('Error', 'Failed to stop logging: ' + error, 'error');
                });
        }
    }
    
    function downloadLog() {
        if (window.pywebview && window.pywebview.api.download_log) {
            window.pywebview.api.download_log()
                .then(response => {
                    console.log('Download log response:', response);
                })
                .catch(error => {
                    console.error('Download log error:', error);
                    showNotification('Error', 'Failed to download log: ' + error, 'error');
                });
        }
    }
    
    // Initialize on page load
    window.addEventListener('load', () => {
        console.log('Page loaded, initializing...');
        
        initGraphCanvases();
        initVerticalSlider();
        updateSliderState();
        
        document.getElementById('refresh-ports').addEventListener('click', refreshPorts);
        document.getElementById('connect-btn').addEventListener('click', connectSerial);
        document.getElementById('disconnect-btn').addEventListener('click', disconnectSerial);
        document.getElementById('start-btn').addEventListener('click', () => {
            sendCommandWithNotification('S', 'Start command sent');
        });
        document.getElementById('stop-btn').addEventListener('click', () => {
            sendCommandWithNotification('D', 'Stop command sent');
        });
        document.getElementById('tare-btn').addEventListener('click', () => {
            sendCommandWithNotification('T', 'Tare command sent');
        });
        document.getElementById('calibrate-btn').addEventListener('click', () => {
            sendCommandWithNotification('C', 'Calibration started');
        });
        document.getElementById('start-logging').addEventListener('click', startLogging);
        document.getElementById('stop-logging').addEventListener('click', stopLogging);
        document.getElementById('download-log').addEventListener('click', downloadLog);
        
        document.getElementById('thrust-graph-toggle').addEventListener('change', toggleGraph);
        document.getElementById('temperature-graph-toggle').addEventListener('change', toggleGraph);
        document.getElementById('rpm-graph-toggle').addEventListener('change', toggleGraph);
        
        let resizeTimeout;
        window.addEventListener('resize', () => {
            clearTimeout(resizeTimeout);
            resizeTimeout = setTimeout(() => {
                initGraphCanvases();
            }, 250);
        });
        
        // Initial ports refresh - suppress error notification
        setTimeout(() => {
            if (window.pywebview && window.pywebview.api.get_ports) {
                window.pywebview.api.get_ports()
                    .then(ports => {
                        const select = document.getElementById('port-select');
                        select.innerHTML = '<option value="">Select Port</option>';
                        ports.forEach(port => {
                            const option = document.createElement('option');
                            option.value = port;
                            option.textContent = port;
                            select.appendChild(option);
                        });
                    })
                    .catch(() => {
                        // Silently fail on initial load
                    });
            }
        }, 500);
        
        console.log('Initialization complete');
    });
    
    // Wait for the pywebviewready event
    window.addEventListener('pywebviewready', () => {
        console.log('PyWebView Bridge Ready');
        initPyWebView();
        // Don't show error if ports fail to load initially
        refreshPorts();
    });
    </script>
</body>
</html>
'''

# ----------------------------- PyWebView App -----------------------------

class ScientechDashboard:
    def __init__(self):
        self.window = None
        self.serial_worker = None
        self.last_ports = []
        
    def init_bridge(self):
        """Initialize the bridge - called from JavaScript"""
        print("Bridge initialized")
        return {"status": "ready"}
    
    def handle_serial_callback(self, data):
        """Handle callback from serial worker"""
        if self.window:
            try:
                if data['type'] == 'new_data':
                    simplified_data = {
                        'thrust': data['data']['thrust'],
                        'temperature': data['data']['temperature'],
                        'rpm': data['data']['rpm'],
                        'rate': data['data']['rate'],
                        'history': data['data'].get('history', {})
                    }
                    self.window.evaluate_js(f'handleSerialData({json.dumps(simplified_data)})')
                else:
                    self.window.evaluate_js(f'handleSerialStatus({json.dumps(data)})')
            except Exception as e:
                print(f"Error sending data to UI: {e}")
    
    def get_ports(self):
        """Get available serial ports"""
        self.last_ports = [p.device for p in serial.tools.list_ports.comports()]
        print(f"Found ports: {self.last_ports}")
        return self.last_ports
    
    def connect(self, port, baud):
        """Connect to serial port"""
        print(f"Connecting to {port} @ {baud}")
        if self.serial_worker:
            self.serial_worker.stop()
        
        self.serial_worker = SerialWorker(self.handle_serial_callback)
        self.serial_worker.start(port, baud)
        return {"success": True, "message": "Connection initiated"}
    
    def disconnect(self):
        """Disconnect from serial port"""
        print("Disconnecting...")
        if self.serial_worker:
            self.serial_worker.stop()
            self.serial_worker = None
            if self.window:
                self.window.evaluate_js(f'handleSerialStatus({json.dumps({"type": "disconnected", "message": "Disconnected by user", "was_unexpected": False})})')
        return {"success": True, "message": "Disconnect initiated"}
    
    def send_serial(self, cmd):
        """Send command to serial port"""
        print(f"Sending command: {cmd}")
        if self.serial_worker and self.serial_worker.is_connected:
            success = self.serial_worker.send_command(str(cmd))
            if success:
                print(f"Command '{cmd}' sent successfully")
                return {"success": True, "message": f"Command '{cmd}' sent"}
            else:
                print(f"Failed to send command '{cmd}'")
                return {"success": False, "message": f"Failed to send command '{cmd}'"}
        else:
            print("No serial worker available or not connected")
            return {"success": False, "message": "Not connected to serial port"}
    
    def start_logging(self):
        """Start data logging"""
        print("Starting logging...")
        logger.start()
        if self.window:
            self.window.evaluate_js(f'handleLoggingStatus({json.dumps({"is_logging": logger.is_logging, "has_data": len(logger.data_buffer) > 0})})')
        return {"success": True, "message": "Logging started"}
    
    def stop_logging(self):
        """Stop data logging"""
        print("Stopping logging...")
        logger.stop()
        if self.window:
            self.window.evaluate_js(f'handleLoggingStatus({json.dumps({"is_logging": logger.is_logging, "has_data": len(logger.data_buffer) > 0})})')
        return {"success": True, "message": "Logging stopped"}
    
    def download_log(self):
        """Download logged data as CSV"""
        print("Downloading log...")
        csv_data = logger.get_csv()
        if not csv_data:
            result = {"success": False, "message": "No data to download"}
            self.window.evaluate_js(f'handleDownloadComplete({json.dumps(result)})')
            return result
        
        if getattr(sys, 'frozen', False):
            app_dir = os.path.dirname(sys.executable)
        else:
            app_dir = os.path.dirname(os.path.abspath(__file__))
        
        data_dir = os.path.join(app_dir, "data")
        try:
            os.makedirs(data_dir, exist_ok=True)
        except Exception as e:
            result = {"success": False, "message": f"Cannot create data directory: {str(e)}"}
            self.window.evaluate_js(f'handleDownloadComplete({json.dumps(result)})')
            return result
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"Scientech_Data_{timestamp}.csv"
        filepath = os.path.join(data_dir, filename)
        
        try:
            with open(filepath, 'w', newline='', encoding='utf-8') as f:
                f.write(csv_data)
            
            relative_path = os.path.join("data", filename)
            absolute_path = os.path.abspath(filepath)
            
            result = {
                "success": True, 
                "message": "Data saved successfully", 
                "filename": filename, 
                "filepath": absolute_path, 
                "relative_path": relative_path
            }
            self.window.evaluate_js(f'handleDownloadComplete({json.dumps(result)})')
            return result
        except Exception as e:
            error_msg = str(e)
            if "Permission denied" in error_msg:
                error_msg = f"Permission denied. Current directory: {app_dir}"
            result = {"success": False, "message": error_msg}
            self.window.evaluate_js(f'handleDownloadComplete({json.dumps(result)})')
            return result
    
    def run(self):
        """Run the application"""
        self.window = webview.create_window(
            'Scientech Technologies Dashboard',
            html=HTML_TEMPLATE,
            js_api=self,
            width=1600,
            height=900,
            resizable=True,
            fullscreen=False,
            text_select=False
        )
        
        webview.start(
            debug=False,
            http_server=False,
            gui='edgechromium',
            localization={}
        )

# ----------------------------- Main -----------------------------

if __name__ == '__main__':
    print("Starting Scientech Technologies Dashboard...")
    print("Press Ctrl+C to stop the application")
    
    app = ScientechDashboard()
    
    try:
        app.run()
    except KeyboardInterrupt:
        print("\nShutting down...")
        if app.serial_worker:
            app.serial_worker.stop()
        sys.exit(0)