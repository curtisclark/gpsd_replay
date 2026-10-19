"""
web_ui.py - Lightweight HTTP + WebSocket control UI for gpsd-replay.

Uses only the standard library (http.server + threading).
WebSocket upgrade is handled manually (RFC 6455) to avoid dependencies.
"""

import base64
import datetime
import hashlib
import http.server
import json
import logging
import os
import struct
import threading
import time
from typing import TYPE_CHECKING, Callable, List, Optional
from urllib.parse import urlparse, parse_qs

if TYPE_CHECKING:
    from .engine import PlaybackEngine

logger = logging.getLogger(__name__)

WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

_FILE_EXTENSIONS = (".nmea", ".gpx", ".log", ".txt", ".ubx")


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>gpsd-replay</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
  :root {
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --accent: #58a6ff; --green: #3fb950; --yellow: #d29922;
    --red: #f85149; --text: #c9d1d9; --muted: #8b949e;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; overflow: hidden; }
  body { background: var(--bg); color: var(--text); font-family: 'Courier New', monospace; display: flex; flex-direction: column; }
  header { flex-shrink: 0; padding: 16px 24px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 12px; }
  header h1 { font-size: 1.1rem; color: var(--accent); }
  .badge { font-size: 0.7rem; padding: 2px 8px; border-radius: 12px; background: var(--surface); border: 1px solid var(--border); }
  .badge.playing { border-color: var(--green); color: var(--green); }
  .badge.paused  { border-color: var(--yellow); color: var(--yellow); }
  .badge.stopped { border-color: var(--red); color: var(--red); }
  main { flex: 1; display: grid; grid-template-columns: 40% 1fr; gap: 20px; padding: 24px; overflow: hidden; min-height: 0; }
  #left-col { display: flex; flex-direction: column; gap: 20px; overflow-y: auto; min-height: 0; }
  #right-col { display: flex; flex-direction: column; min-height: 0; }
  #map-card { flex: 1; display: flex; flex-direction: column; min-height: 0; }
  #map-card .card-header { display: flex; align-items: center; margin-bottom: 14px; flex-shrink: 0; }
  #map-card .card-header h2 { margin-bottom: 0; }
  #map-card .card-header .map-btns { margin-left: auto; display: flex; gap: 8px; }
  #map { flex: 1; border-radius: 6px; min-height: 0; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 20px; }
  .card h2 { font-size: 0.8rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.1em; margin-bottom: 14px; }
  .coords { font-size: 1.5rem; color: var(--text); letter-spacing: -0.02em; }
  .coords span { color: var(--muted); font-size: 0.9rem; }
  .meta-grid { display: flex; gap: 16px; margin-top: 14px; }
  .meta-col { flex: 1; display: flex; flex-direction: column; gap: 8px; }
  .meta-col-label { font-size: 0.65rem; color: var(--accent); text-transform: uppercase; letter-spacing: 0.1em; padding-bottom: 4px; border-bottom: 1px solid var(--border); margin-bottom: 2px; }
  .meta-item label { display: block; font-size: 0.7rem; color: var(--muted); margin-bottom: 2px; }
  .meta-item value { font-size: 1.1rem; }
  .progress-wrap { margin-top: 8px; }
  .progress-bar-bg { background: var(--bg); border-radius: 4px; height: 6px; cursor: pointer; position: relative; }
  .progress-bar-fill { background: var(--accent); height: 100%; border-radius: 4px; transition: width 0.3s; pointer-events: none; }
  .progress-label { display: flex; justify-content: space-between; font-size: 0.75rem; color: var(--muted); margin-top: 4px; }
  .controls { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
  button { background: var(--surface); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 8px 18px; cursor: pointer; font-family: inherit; font-size: 0.85rem; transition: border-color 0.15s, background 0.15s; }
  button:hover { border-color: var(--accent); background: #1c2333; }
  button.primary { background: var(--accent); color: #0d1117; border-color: var(--accent); font-weight: bold; }
  button.primary:hover { opacity: 0.85; }
  button.danger { border-color: var(--red); color: var(--red); }
  button.sm { padding: 4px 10px; font-size: 0.75rem; }
  .speed-wrap { display: flex; align-items: center; gap: 8px; margin-left: auto; }
  .speed-wrap label { font-size: 0.8rem; color: var(--muted); }
  .speed-wrap input[type=range] { width: 120px; accent-color: var(--accent); }
  #speed-val { min-width: 3em; text-align: right; color: var(--accent); }
  .loop-wrap { display: flex; align-items: center; gap: 6px; font-size: 0.85rem; }
  .loop-wrap input { accent-color: var(--accent); }
  .conn-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--red); display: inline-block; margin-right: 6px; }
  .conn-dot.ok { background: var(--green); }
  #log { height: 160px; overflow-y: auto; font-size: 0.75rem; color: var(--muted); padding: 10px; background: var(--bg); border-radius: 4px; border: 1px solid var(--border); }
  #log .entry { border-bottom: 1px solid var(--border); padding: 2px 0; }
  #log .ts { color: var(--accent); margin-right: 6px; }
  #file-card { display: none; }
  #file-select { background: var(--bg); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 7px 10px; font-family: inherit; font-size: 0.85rem; flex: 1; min-width: 0; cursor: pointer; }
  #file-select:focus { outline: none; border-color: var(--accent); }
  #active-file { font-size: 0.8rem; color: var(--muted); margin-top: 6px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .badge.manual { border-color: #a371f7; color: #a371f7; }
  .mode-toggle { display: flex; gap: 4px; margin-left: 12px; }
  .mode-btn { padding: 3px 12px; font-size: 0.75rem; border-radius: 4px; cursor: pointer; border: 1px solid var(--border); background: var(--surface); color: var(--text); font-family: inherit; transition: border-color 0.15s, background 0.15s; }
  .mode-btn.active { background: var(--accent); color: #0d1117; border-color: var(--accent); font-weight: bold; }
  .mode-btn:hover:not(.active) { border-color: var(--accent); }
  .manual-grid { display: grid; grid-template-columns: auto 1fr; gap: 8px 12px; align-items: center; font-size: 0.85rem; }
  .manual-grid label { color: var(--muted); white-space: nowrap; }
  .manual-grid input[type=number] { background: var(--bg); color: var(--text); border: 1px solid var(--border); border-radius: 4px; padding: 5px 8px; font-family: inherit; font-size: 0.85rem; width: 100%; }
  .manual-grid input[type=number]:focus { outline: none; border-color: var(--accent); }
  /* Leaflet overrides for dark theme */
  .leaflet-container { background: #0d1117; }
  .leaflet-control-zoom a { background: #161b22; color: #c9d1d9; border-color: #30363d; }
  .leaflet-control-zoom a:hover { background: #1c2333; }
  .leaflet-control-attribution { background: rgba(13,17,23,0.8); color: #8b949e; font-size: 0.65rem; }
  .leaflet-control-attribution a { color: #58a6ff; }
</style>
</head>
<body>
<header>
  <h1>&#9889; gpsd-replay</h1>
  <span id="state-badge" class="badge stopped">STOPPED</span>
  <div class="mode-toggle">
    <button id="mode-replay-btn" class="mode-btn active" onclick="setMode('replay')">Replay</button>
    <button id="mode-manual-btn" class="mode-btn" onclick="setMode('manual')">Manual</button>
  </div>
  <span style="margin-left:auto;font-size:0.8rem;">
    <span id="conn-dot" class="conn-dot"></span><span id="conn-label">disconnected</span>
  </span>
</header>
<main>
  <div id="left-col">
    <div class="card" id="manual-card" style="display:none">
      <h2>Manual GPS</h2>
      <div class="manual-grid">
        <label>Lat</label>
        <input type="number" id="m-lat" step="0.000001" placeholder="40.7128">
        <label>Lon</label>
        <input type="number" id="m-lon" step="0.000001" placeholder="-74.0060">
        <label>Alt (m)</label>
        <input type="number" id="m-alt" step="0.1" value="0">
        <label>Speed (kn)</label>
        <input type="number" id="m-speed" step="0.1" value="0" min="0">
        <label>Bearing (&#176;)</label>
        <input type="number" id="m-bearing" step="1" value="0" min="0" max="360">
      </div>
      <div style="margin-top:12px;display:flex;gap:10px;align-items:center;">
        <button class="primary" onclick="applyManual()">Apply</button>
        <span style="font-size:0.75rem;color:var(--muted);">Tip: click the map to set position</span>
      </div>
    </div>

    <div class="card" id="file-card">
      <h2>File</h2>
      <div class="controls">
        <select id="file-select"><option value="">Select a file&#8230;</option></select>
        <button class="primary" onclick="loadFile()">Load</button>
      </div>
      <div id="active-file"></div>
    </div>

    <div class="card">
      <h2>Position</h2>
      <div class="coords" id="coords">&#8212; <span>lat</span> &nbsp; &#8212; <span>lon</span></div>
      <div style="font-size:0.85rem;color:var(--muted);margin-top:6px;display:flex;gap:20px;">
        <span id="replay-ts">&#8212;</span>
        <span id="live-ts-wrap" style="display:none;"><span style="color:var(--green);">&#9679;</span> Live: <span id="live-ts"></span></span>
      </div>
      <div class="meta-grid">
        <div class="meta-col">
          <div class="meta-col-label">Speed</div>
          <div class="meta-item"><label>Knots</label><value id="speed-knots">&#8212;</value></div>
          <div class="meta-item"><label>MPH</label><value id="speed-mph">&#8212;</value></div>
          <div class="meta-item"><label>KPH</label><value id="speed-kph">&#8212;</value></div>
          <div class="meta-item"><label>m/s</label><value id="speed-mps">&#8212;</value></div>
        </div>
        <div class="meta-col">
          <div class="meta-col-label">Altitude</div>
          <div class="meta-item"><label>HAE (m)</label><value id="alt-hae">&#8212;</value></div>
          <div class="meta-item"><label>HAE (ft)</label><value id="alt-hae-ft">&#8212;</value></div>
          <div class="meta-item"><label>MSL (m)</label><value id="alt-msl">&#8212;</value></div>
          <div class="meta-item"><label>MSL (ft)</label><value id="alt-msl-ft">&#8212;</value></div>
        </div>
        <div class="meta-col">
          <div class="meta-col-label">Course</div>
          <div class="meta-item"><label>Bearing (&#176;)</label><value id="bearing">&#8212;</value></div>
        </div>
      </div>
    </div>

    <div class="card" id="playback-card">
      <h2>Playback</h2>
      <div class="progress-wrap">
        <div class="progress-bar-bg" id="prog-bar">
          <div class="progress-bar-fill" id="prog-fill" style="width:0%"></div>
        </div>
        <div class="progress-label">
          <span id="frame-cur">0</span>
          <span id="frame-total">0</span>
        </div>
      </div>
      <div class="controls" style="margin-top:16px;">
        <button class="primary" onclick="cmd('play')">&#9654; Play</button>
        <button onclick="cmd('pause')">&#9646;&#9646; Pause</button>
        <button class="danger" onclick="cmd('stop')">&#9632; Stop</button>
        <div class="loop-wrap">
          <input type="checkbox" id="loop-cb" onchange="setLoop(this.checked)">
          <label for="loop-cb">Loop</label>
        </div>
        <div class="speed-wrap">
          <label>Speed</label>
          <input type="range" min="0.1" max="10" step="0.1" value="1" id="speed-slider" oninput="setSpeed(this.value)">
          <span id="speed-val">1.0×</span>
        </div>
      </div>
      <div class="controls" style="margin-top:10px;">
        <button onclick="skipFrames(-1)">&#9664;&#9664; Back</button>
        <button onclick="skipFrames(1)">Forward &#9654;&#9654;</button>
        <div class="speed-wrap">
          <label>Frames</label>
          <input type="range" min="1" max="100" step="1" value="10" id="skip-slider" oninput="document.getElementById('skip-val').textContent=this.value">
          <span id="skip-val" style="min-width:3em;text-align:right;color:var(--accent);">10</span>
        </div>
      </div>
    </div>

    <div class="card" id="session-card">
      <h2>Session</h2>
      <div id="session-content" style="display:grid;grid-template-columns:auto 1fr;gap:4px 14px;font-size:0.8rem;"></div>
    </div>

    <div class="card">
      <h2>Event Log</h2>
      <div id="log"></div>
    </div>
  </div>

  <div id="right-col">
    <div class="card" id="map-card">
      <div class="card-header">
        <h2>Map</h2>
        <div class="map-btns">
          <button class="sm" id="follow-btn" onclick="toggleFollow()">Follow: ON</button>
          <button class="sm" onclick="clearPath()">Clear Path</button>
        </div>
      </div>
      <div id="map"></div>
    </div>
  </div>
</main>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
// --- Map setup ---
const map = L.map('map', { zoomControl: true }).setView([0, 0], 13);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  maxZoom: 19
}).addTo(map);

const posMarker = L.circleMarker([0, 0], {
  radius: 7, fillColor: '#58a6ff', color: '#ffffff', weight: 2, fillOpacity: 1
}).addTo(map);

const pathLine = L.polyline([], { color: '#3fb950', weight: 2, opacity: 0.75 }).addTo(map);

let pathCoords = [];
let prevFrameIndex = -1;
let followMap = true;
let mapReady = false;
let currentMode = 'replay';
let hasFiles = false;

map.on('click', function(e) {
  if (currentMode === 'manual') {
    document.getElementById('m-lat').value = e.latlng.lat.toFixed(6);
    document.getElementById('m-lon').value = e.latlng.lng.toFixed(6);
  }
});

function updateMap(lat, lon, frameIndex, state) {
  if (lat === 0 && lon === 0) return;
  // Reset path on backward seek or when stopped
  if (frameIndex < prevFrameIndex - 2 || state === 'stopped') {
    pathCoords = [];
    pathLine.setLatLngs([]);
  }
  prevFrameIndex = frameIndex;
  posMarker.setLatLng([lat, lon]);
  if (state === 'playing') {
    pathCoords.push([lat, lon]);
    if (pathCoords.length > 2000) pathCoords.shift();
    pathLine.setLatLngs(pathCoords);
  }
  if (followMap) {
    if (!mapReady) { map.setView([lat, lon], 14); mapReady = true; }
    else { map.panTo([lat, lon]); }
  }
}

function toggleFollow() {
  followMap = !followMap;
  document.getElementById('follow-btn').textContent = 'Follow: ' + (followMap ? 'ON' : 'OFF');
  if (followMap) map.panTo(posMarker.getLatLng());
}

function clearPath() {
  pathCoords = [];
  pathLine.setLatLngs([]);
}

// --- WebSocket ---
let ws = null, reconnectTimer = null;

function connect() {
  const host = location.hostname || 'localhost';
  ws = new WebSocket(`ws://${host}:${location.port || 8765}/ws`);
  ws.onopen = () => {
    document.getElementById('conn-dot').classList.add('ok');
    document.getElementById('conn-label').textContent = 'connected';
    log('WebSocket connected');
  };
  ws.onclose = () => {
    document.getElementById('conn-dot').classList.remove('ok');
    document.getElementById('conn-label').textContent = 'disconnected';
    log('WebSocket disconnected &#8212; retrying in 3s&#8230;');
    reconnectTimer = setTimeout(connect, 3000);
  };
  ws.onerror = () => {};
  ws.onmessage = (e) => {
    try { handleMsg(JSON.parse(e.data)); } catch {}
  };
}

function handleMsg(m) {
  if (m.type === 'status') {
    const s = m.data;
    document.getElementById('state-badge').textContent = s.state.toUpperCase();
    document.getElementById('state-badge').className = 'badge ' + s.state.toLowerCase();
    if (s.state === 'manual' && currentMode !== 'manual') setMode('manual', true);
    document.getElementById('coords').innerHTML =
      `${s.lat.toFixed(7)} <span>lat</span> &nbsp; ${s.lon.toFixed(7)} <span>lon</span>`;
    document.getElementById('speed-knots').textContent = s.speed_knots?.toFixed(1) ?? '—';
    document.getElementById('speed-mph').textContent = s.speed_mph?.toFixed(1) ?? '—';
    document.getElementById('speed-kph').textContent = s.speed_kph?.toFixed(1) ?? '—';
    document.getElementById('speed-mps').textContent = s.speed_mps?.toFixed(1) ?? '—';
    document.getElementById('alt-msl').textContent = s.alt_msl?.toFixed(1) ?? '—';
    document.getElementById('alt-msl-ft').textContent = s.alt_msl_ft?.toFixed(1) ?? '—';
    document.getElementById('alt-hae').textContent = s.alt_hae != null ? s.alt_hae.toFixed(1) : '—';
    document.getElementById('alt-hae-ft').textContent = s.alt_hae_ft != null ? s.alt_hae_ft.toFixed(1) : '—';
    document.getElementById('bearing').textContent = s.bearing?.toFixed(1) ?? '—';
    document.getElementById('replay-ts').textContent = (s.live_ts ? 'File: ' : '') + (s.ts || '—');
    const liveWrap = document.getElementById('live-ts-wrap');
    if (s.live_ts) {
      document.getElementById('live-ts').textContent = s.live_ts;
      liveWrap.style.display = '';
    } else {
      liveWrap.style.display = 'none';
    }
    document.getElementById('active-file').textContent = s.current_file || '';
    document.getElementById('frame-cur').textContent = s.frame_index;
    document.getElementById('frame-total').textContent = s.total_frames;
    const pct = s.total_frames > 0 ? (s.frame_index / s.total_frames * 100) : 0;
    document.getElementById('prog-fill').style.width = pct + '%';
    document.getElementById('speed-slider').value = s.speed;
    document.getElementById('speed-val').textContent = parseFloat(s.speed).toFixed(1) + '×';
    document.getElementById('loop-cb').checked = s.loop;
    updateMap(s.lat, s.lon, s.frame_index, s.state);
  } else if (m.type === 'log') {
    log(m.data);
  }
}

function cmd(action) { send({ action }); }

function setSpeed(v) {
  document.getElementById('speed-val').textContent = parseFloat(v).toFixed(1) + '×';
  send({ action: 'speed', value: parseFloat(v) });
}

function skipFrames(dir) {
  const n = parseInt(document.getElementById('skip-slider').value) || 10;
  const cur = parseInt(document.getElementById('frame-cur').textContent) || 0;
  const total = parseInt(document.getElementById('frame-total').textContent) || 0;
  const target = Math.max(0, Math.min(total, cur + dir * n));
  send({ action: 'seek', value: target });
}

function setLoop(v) { send({ action: 'loop', value: v }); }

document.getElementById('prog-bar').addEventListener('click', function(e) {
  const rect = this.getBoundingClientRect();
  const frac = (e.clientX - rect.left) / rect.width;
  const total = parseInt(document.getElementById('frame-total').textContent) || 0;
  send({ action: 'seek', value: Math.round(frac * total) });
});

function send(obj) {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj));
}

let logEntries = [];
function log(msg) {
  const ts = new Date().toTimeString().slice(0, 8);
  logEntries.unshift(`<div class="entry"><span class="ts">${ts}</span>${msg}</div>`);
  if (logEntries.length > 50) logEntries.pop();
  document.getElementById('log').innerHTML = logEntries.join('');
}

function setMode(mode, silent) {
  if (currentMode === mode) return;
  currentMode = mode;
  document.getElementById('mode-replay-btn').classList.toggle('active', mode === 'replay');
  document.getElementById('mode-manual-btn').classList.toggle('active', mode === 'manual');
  document.getElementById('manual-card').style.display = mode === 'manual' ? 'block' : 'none';
  document.getElementById('file-card').style.display = (mode === 'replay' && hasFiles) ? 'block' : 'none';
  document.getElementById('playback-card').style.display = mode === 'manual' ? 'none' : 'block';
  if (mode === 'replay' && !silent) send({ action: 'clear_manual' });
}

function applyManual() {
  const lat = parseFloat(document.getElementById('m-lat').value);
  const lon = parseFloat(document.getElementById('m-lon').value);
  const alt = parseFloat(document.getElementById('m-alt').value) || 0;
  const speed_knots = parseFloat(document.getElementById('m-speed').value) || 0;
  const bearing = parseFloat(document.getElementById('m-bearing').value) || 0;
  if (isNaN(lat) || isNaN(lon)) { log('Enter a valid lat/lon first'); return; }
  send({ action: 'set_manual', lat, lon, alt, speed_knots, bearing });
}

connect();
setInterval(() => { if (ws && ws.readyState === 1) send({ action: 'status' }); }, 500);

// File picker
async function refreshFiles() {
  try {
    const res = await fetch('/api/files');
    if (!res.ok) return;
    const files = await res.json();
    const sel = document.getElementById('file-select');
    const prev = sel.value;
    sel.innerHTML = '<option value="">Select a file&#8230;</option>';
    files.forEach(f => {
      const opt = document.createElement('option');
      opt.value = f; opt.textContent = f;
      sel.appendChild(opt);
    });
    if (prev) sel.value = prev;
    hasFiles = files.length > 0;
    document.getElementById('file-card').style.display = (hasFiles && currentMode === 'replay') ? 'block' : 'none';
  } catch(e) {}
}
refreshFiles();

(async function loadSession() {
  try {
    const res = await fetch('/api/config');
    if (!res.ok) return;
    const cfg = await res.json();
    const el = document.getElementById('session-content');
    const rows = [
      ['Source',    cfg.source_type?.toUpperCase()],
      ['File',      cfg.file],
      ['TCP',       cfg.tcp ? `${cfg.tcp_host}:${cfg.tcp_port}` : 'disabled'],
      ['NMEA Raw',  cfg.nmea_port ? `${cfg.tcp_host}:${cfg.nmea_port}` : 'disabled'],
      ['PTY',       cfg.pty ? cfg.pty_path : 'disabled'],
      ['Web UI',    `:${cfg.ui_port}`],
      ['File Dir',  cfg.file_dir || '—'],
      ['Speed',     cfg.speed + '×'],
      ['Loop',      cfg.loop ? 'yes' : 'no'],
      ['Live Time', cfg.live_time ? 'yes' : 'no'],
    ];
    el.innerHTML = rows.map(([k, v]) =>
      `<span style="color:var(--muted)">${k}</span><span style="color:var(--text);word-break:break-all">${v ?? '—'}</span>`
    ).join('');
  } catch(e) {}
})();

function loadFile() {
  const sel = document.getElementById('file-select');
  const name = sel.value;
  if (!name) return;
  pathCoords = []; pathLine.setLatLngs([]); mapReady = false;
  send({ action: 'load_file', value: name });
}
</script>
</body>
</html>
"""


class WebSocketClient:
    def __init__(self, conn, addr):
        self.conn = conn
        self.addr = addr

    def send_text(self, text: str):
        data = text.encode()
        length = len(data)
        if length <= 125:
            header = bytes([0x81, length])
        elif length <= 65535:
            header = bytes([0x81, 126]) + struct.pack(">H", length)
        else:
            header = bytes([0x81, 127]) + struct.pack(">Q", length)
        self.conn.sendall(header + data)

    def recv_text(self) -> Optional[str]:
        """Read one WebSocket text frame (masked, from browser)."""
        try:
            b1, b2 = self.conn.recv(2)
            masked = b2 & 0x80
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self.conn.recv(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self.conn.recv(8))[0]
            if masked:
                mask = self.conn.recv(4)
                payload = bytearray(self.conn.recv(length))
                for i in range(len(payload)):
                    payload[i] ^= mask[i % 4]
                return payload.decode("utf-8", errors="replace")
            else:
                return self.conn.recv(length).decode("utf-8", errors="replace")
        except Exception:
            return None


class WebUIServer:
    """
    Minimal HTTP + WebSocket server for the control UI.
    No external dependencies.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8765,
        file_dir: Optional[str] = None,
        loader: Optional[Callable[[str], list]] = None,
        config: Optional[dict] = None,
    ):
        self.host = host
        self.port = port
        self._file_dir = file_dir
        self._loader = loader
        self._config: dict = config or {}
        self._current_file: str = ""
        self._engine: Optional["PlaybackEngine"] = None
        self._ws_clients: List[WebSocketClient] = []
        self._ws_lock = threading.Lock()
        self._server_sock = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def attach_engine(self, engine: "PlaybackEngine"):
        self._engine = engine

    def start(self):
        import socket as _socket
        self._server_sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        self._server_sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        self._server_sock.bind((self.host, self.port))
        self._server_sock.listen(10)
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True, name="webui-accept")
        self._thread.start()

        # Status broadcast thread
        self._bcast_thread = threading.Thread(target=self._broadcast_loop, daemon=True, name="webui-bcast")
        self._bcast_thread.start()

        logger.info("Web UI available at http://%s:%d", self.host if self.host != "0.0.0.0" else "localhost", self.port)

    def stop(self):
        self._running = False
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass

    def _accept_loop(self):
        while self._running:
            try:
                conn, addr = self._server_sock.accept()
                threading.Thread(target=self._handle_conn, args=(conn, addr), daemon=True).start()
            except Exception:
                if self._running:
                    pass

    def _handle_conn(self, conn, addr):
        try:
            conn.settimeout(10)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(1024)
                if not chunk:
                    return
                data += chunk

            headers_raw, _, body = data.partition(b"\r\n\r\n")
            header_lines = headers_raw.decode(errors="replace").splitlines()
            if not header_lines:
                return

            method, path, _ = (header_lines[0] + "  ").split(" ", 2)
            headers = {}
            for h in header_lines[1:]:
                if ": " in h:
                    k, v = h.split(": ", 1)
                    headers[k.lower()] = v

            # WebSocket upgrade
            if headers.get("upgrade", "").lower() == "websocket":
                self._ws_handshake(conn, addr, headers)
                return

            # HTTP
            conn.settimeout(5)
            if path == "/" or path == "/index.html":
                resp = HTML.encode()
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n"
                    + f"Content-Length: {len(resp)}\r\n\r\n".encode()
                    + resp
                )
            elif path == "/api/config":
                resp = json.dumps(self._config).encode()
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                    + f"Content-Length: {len(resp)}\r\n\r\n".encode()
                    + resp
                )
            elif path == "/api/files":
                resp = json.dumps(self._list_files()).encode()
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                    + f"Content-Length: {len(resp)}\r\n\r\n".encode()
                    + resp
                )
            else:
                conn.sendall(b"HTTP/1.1 404 Not Found\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
        except Exception as e:
            logger.debug("HTTP handler error: %s", e)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _ws_handshake(self, conn, addr, headers):
        key = headers.get("sec-websocket-key", "")
        accept = base64.b64encode(
            hashlib.sha1((key + WS_MAGIC).encode()).digest()
        ).decode()
        conn.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            + f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode()
        )

        ws = WebSocketClient(conn, addr)
        with self._ws_lock:
            self._ws_clients.append(ws)
        logger.debug("WebSocket client connected: %s", addr)

        conn.settimeout(30)
        try:
            while self._running:
                text = ws.recv_text()
                if text is None:
                    break
                self._handle_ws_msg(ws, text)
        finally:
            with self._ws_lock:
                if ws in self._ws_clients:
                    self._ws_clients.remove(ws)
            try:
                conn.close()
            except Exception:
                pass
            logger.debug("WebSocket client disconnected: %s", addr)

    def _handle_ws_msg(self, ws: WebSocketClient, text: str):
        try:
            msg = json.loads(text)
        except Exception:
            return

        action = msg.get("action")
        engine = self._engine

        if action == "play" and engine:
            engine.play()
            self._log_broadcast("Playback started")
        elif action == "pause" and engine:
            engine.pause()
            self._log_broadcast("Playback paused")
        elif action == "stop" and engine:
            engine.stop()
            self._log_broadcast("Playback stopped")
        elif action == "speed" and engine:
            v = float(msg.get("value", 1.0))
            engine.set_speed(v)
        elif action == "loop" and engine:
            engine.set_loop(bool(msg.get("value", False)))
        elif action == "seek" and engine:
            engine.seek(int(msg.get("value", 0)))
            self._log_broadcast(f"Seeked to frame {msg.get('value')}")
        elif action == "load_file":
            self._handle_load_file(msg.get("value", ""))
        elif action == "set_manual":
            self._handle_set_manual(msg)
        elif action == "clear_manual":
            self._handle_clear_manual()
        elif action == "status":
            self._send_status(ws)

    def _list_files(self) -> List[str]:
        if not self._file_dir or not os.path.isdir(self._file_dir):
            return []
        return sorted(
            f for f in os.listdir(self._file_dir)
            if os.path.isfile(os.path.join(self._file_dir, f))
            and f.lower().endswith(_FILE_EXTENSIONS)
        )

    def _handle_load_file(self, filename: str):
        if not filename or not self._file_dir or not self._loader or not self._engine:
            self._log_broadcast("File load not available")
            return
        # Prevent path traversal
        safe = os.path.basename(filename)
        full_path = os.path.join(self._file_dir, safe)
        if not os.path.isfile(full_path):
            self._log_broadcast(f"File not found: {safe}")
            return
        try:
            frames = self._loader(full_path)
            if not frames:
                self._log_broadcast(f"No frames loaded from {safe}")
                return
            self._engine.load_frames(frames)
            self._current_file = safe
            self._log_broadcast(f"Loaded {len(frames)} frames from {safe}")
        except Exception as e:
            logger.warning("Error loading file %s: %s", safe, e)
            self._log_broadcast(f"Error loading {safe}: {e}")

    def _handle_set_manual(self, msg: dict):
        if not self._engine:
            return
        from .sources import GPSFrame
        from .nmea import make_gprmc, make_gpgga
        try:
            lat = float(msg["lat"])
            lon = float(msg["lon"])
            alt = float(msg.get("alt", 0.0))
            speed_knots = float(msg.get("speed_knots", 0.0))
            bearing = float(msg.get("bearing", 0.0))
        except (KeyError, TypeError, ValueError) as e:
            self._log_broadcast(f"Invalid manual params: {e}")
            return
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        frame = GPSFrame(
            ts=now,
            lat=lat,
            lon=lon,
            alt=alt,
            speed_knots=speed_knots,
            bearing=bearing,
            sentences=[
                make_gprmc(lat, lon, speed_knots, bearing, now),
                make_gpgga(lat, lon, alt, ts=now),
            ],
        )
        self._engine.set_manual(frame)
        self._log_broadcast(
            f"Manual: {lat:.6f}, {lon:.6f}  alt={alt:.1f}m  "
            f"spd={speed_knots:.1f}kn  hdg={bearing:.1f}°"
        )

    def _handle_clear_manual(self):
        if not self._engine:
            return
        self._engine.clear_manual()
        self._log_broadcast("Returned to replay mode")

    def _send_status(self, ws: Optional[WebSocketClient] = None):
        if not self._engine:
            return
        st = self._engine.status
        frame = self._engine.current_frame

        payload = json.dumps({
            "type": "status",
            "data": {
                "state": st.state.name.lower(),
                "frame_index": st.frame_index,
                "total_frames": st.total_frames,
                "speed": st.speed,
                "loop": st.loop,
                "lat": frame.lat if frame else 0.0,
                "lon": frame.lon if frame else 0.0,
                "alt_msl": frame.alt if frame else 0.0,
                "alt_msl_ft": (frame.alt * 3.28084) if frame else 0.0,
                "alt_hae": frame.alt_hae if frame else None,
                "alt_hae_ft": (frame.alt_hae * 3.28084) if frame and frame.alt_hae is not None else None,
                "speed_knots": frame.speed_knots if frame else 0.0,
                "speed_mph": (frame.speed_knots * 1.15078) if frame else 0.0,
                "speed_kph": (frame.speed_knots * 1.852) if frame else 0.0,
                "speed_mps": (frame.speed_knots * 0.514444) if frame else 0.0,
                "bearing": frame.bearing if frame else 0.0,
                "ts": (
                    time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
                    if st.state.name == "MANUAL"
                    else (frame.ts.strftime("%Y-%m-%d %H:%M:%S") if frame else "")
                ),
                "live_ts": (
                    time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
                    if self._config.get("live_time") else None
                ),
                "current_file": self._current_file,
            },
        })
        targets = [ws] if ws else self._snapshot_clients()
        for client in targets:
            try:
                client.send_text(payload)
            except Exception:
                pass

    def _log_broadcast(self, msg: str):
        payload = json.dumps({"type": "log", "data": msg})
        for client in self._snapshot_clients():
            try:
                client.send_text(payload)
            except Exception:
                pass

    def _broadcast_loop(self):
        while self._running:
            time.sleep(0.5)
            self._send_status()

    def _snapshot_clients(self) -> List[WebSocketClient]:
        with self._ws_lock:
            return list(self._ws_clients)
