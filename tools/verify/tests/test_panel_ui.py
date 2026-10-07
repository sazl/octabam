"""Firmware-free browser event checks; optional existing Chromium/Node instrument.

The fake HTTP source serves only synthetic status and an empty image. No device,
firmware, package install, or npm dependency is used. Missing browser tooling is
reported as a unittest skip; source-text checks are not browser evidence.
"""
import http.server
import json
import os
import signal
import pathlib
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[3]


class PanelBrowserTests(unittest.TestCase):
    def test_capabilities_guard_real_browser_events(self):
        self.run_browser('capabilities')

    def test_no_frame_status_failure_keeps_waiting_label(self):
        self.run_browser('no-frame-failure')

    def test_delayed_audio_status_cannot_restore_hardware_controls(self):
        self.run_browser('delayed-audio')

    def run_browser(self, mode):
        if os.name != 'posix':
            self.skipTest('optional Chromium process-group instrument requires a POSIX host')
        chromium = shutil.which('chromium') or shutil.which('chromium-browser')
        node = shutil.which('node')
        if not chromium or not node:
            self.skipTest('real browser instrument requires existing Chromium and Node 22+; no dependencies installed')
        if subprocess.run([node, '-p', 'typeof WebSocket'], capture_output=True, text=True).stdout.strip() != 'function':
            self.skipTest('existing Node lacks built-in WebSocket')
        skin_dir = tempfile.TemporaryDirectory(prefix='panel-skin-')
        subprocess.run([shutil.which('python3'), str(ROOT / 'tools/panel/skin/gen_svg.py'), skin_dir.name], check=True, capture_output=True)
        skin_path = pathlib.Path(skin_dir.name)
        skin = ('window.SKIN=' + (skin_path / 'octatrack-elements.json').read_text() + ';window.SKIN_SVG=' + json.dumps((skin_path / 'octatrack.svg').read_text()) + ';').encode()
        state = {'source': 'hardware', 'connection_state': 'live', 'has_frame': True}
        requests = []
        full_requests = []
        audio_gate = threading.Event()
        audio_gate.set()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                from urllib.parse import parse_qs, urlsplit
                path = urlsplit(self.path).path
                q = parse_qs(urlsplit(self.path).query)
                if path == '/fixture':
                    state.update(source=q['source'][0], connection_state=q['state'][0], has_frame=q['frame'][0] == '1')
                    body = b'{}'
                elif path == '/audio-gate':
                    if q.get('hold') == ['1']:
                        audio_gate.clear()
                    else:
                        audio_gate.set()
                    body = b'{}'
                elif path == '/requests':
                    body = json.dumps(full_requests if "full" in q else requests).encode()
                    if 'clear' in q:
                        requests.clear(); full_requests.clear()
                else:
                    requests.append(path)
                    full_requests.append(self.path)
                    if path == '/':
                        body = (ROOT / 'tools/panel/panel.html').read_bytes()
                    elif path == '/skin.js':
                        body = skin
                    elif path == '/status':
                        payload = dict(state, backend=state['source'], read_only=state['source'] == 'hardware', capabilities=['screen'] if state['source'] == 'hardware' else ['screen','leds','controls','emulator','samples','card','audio'], generation=1, seq=1, model=None, device={'serial':'fake-only'}, protocol={'major':1,'minor':0}, build='synthetic', last_contact_at=time.time(), last_snapshot_at=time.time(), last_display_change_at=1, phase='ready', booted=True)
                        if state['source'] == 'legacy':
                            payload.pop('source'); payload['backend'] = 'port'
                        body = b'{invalid' if state['connection_state'] == 'server_error' else json.dumps(payload).encode()
                    elif path == '/map':
                        body = json.dumps({'keys':{'func':[37,5]},'knobs':{'level':48,'A':49},'leds':{}}).encode()
                    elif path == '/audio/status':
                        if not audio_gate.wait(10):
                            self.send_error(504, 'test audio gate timed out'); return
                        body = b'{"ok":true,"sound":true,"end":0}'
                    elif path == '/xfader':
                        body = b'{"ok":true,"pos":0}'
                    else:
                        body = b'{"ok":true,"result":"synthetic"}'
                self.send_response(200)
                self.send_header('Content-Type', 'text/html' if path == '/' else 'text/javascript' if path == '/skin.js' else 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_POST = do_GET

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix='panel-browser-') as profile:
                chrome = subprocess.Popen([chromium,'--headless','--no-sandbox','--disable-gpu','--remote-debugging-port=0',f'--user-data-dir={profile}','about:blank'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                try:
                    marker = pathlib.Path(profile) / 'DevToolsActivePort'
                    deadline = time.monotonic() + 10
                    while not marker.exists() and chrome.poll() is None and time.monotonic() < deadline:
                        time.sleep(.05)
                    self.assertTrue(marker.exists(), 'Chromium must start its real-browser instrument')
                    port = marker.read_text().splitlines()[0]
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list') as response:
                        tabs = json.load(response)
                    ws = next(t['webSocketDebuggerUrl'] for t in tabs if t['type'] == 'page')
                    run = subprocess.run([node,str(pathlib.Path(__file__).with_name('panel_browser.js')),ws,f'http://127.0.0.1:{server.server_port}/',mode], capture_output=True, text=True, timeout=30)
                    self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                finally:
                    # Chromium uses child processes; stop the whole test group
                    # so profile writers cannot race temporary-directory cleanup.
                    try:
                        os.killpg(chrome.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    chrome.wait(timeout=10)
                    time.sleep(.1)
        finally:
            audio_gate.set()
            server.shutdown()
            server.server_close()
            thread.join()
            skin_dir.cleanup()


class NativeSourcePolicyInspection(unittest.TestCase):
    """Regression inspection only: AppKit compilation/runtime needs macOS."""

    def test_emulator_identity_precedes_lifecycle_and_status_consumers(self):
        source = (ROOT / 'tools/panel/app/VirtualPanel.swift').read_text()
        def body(name):
            start = source.index('func ' + name + '(')
            opening = source.index('{', start)
            level = 1
            end = opening + 1
            while level:
                if source[end] == '{': level += 1
                if source[end] == '}': level -= 1
                end += 1
            return source[opening + 1:end - 1]
        for name in ['startServer', 'launch', 'restartServer', 'pollOnce', 'loadPanel', 'whenReady', 'reload', 'noteStatus', 'drainPendingAdds', 'runAddBatch']:
            with self.subTest(consumer=name):
                self.assertIn('emulatorAllowed', body(name), f'{name} must guard persistent source refusal')
        self.assertIn('acceptStatus', body('staleReason'))
        self.assertIn('sourceRefusal == nil', body('terminateForeign'))
        self.assertIn('sourceRefusal == nil', body('spawn'))
        self.assertIn('emulatorVerified', body('get'))
        self.assertIn('sourceRefusal == nil', body('download'))
        policy = body('acceptStatus')
        self.assertIn('sourceRefusal == nil', policy)
        self.assertIn('"port"', policy)
        self.assertIn('browser', policy)
        self.assertNotIn('sourceRefusal = nil', policy, 'refusal must survive errors, reload, and retries')


    def test_reload_retains_foreign_emulator_lifecycle_branch(self):
        source = (ROOT / 'tools/panel/app/VirtualPanel.swift').read_text()
        reload = source.split('@objc func reload(', 1)[1].split('// MARK: samples', 1)[0]
        self.assertNotIn('if ok { self.loadPanel() }', reload)
        self.assertIn('self.web.reload()', reload)
        self.assertIn('self.startServer()', reload)
        self.assertLess(reload.index('self.emulatorAllowed(ok ? status : nil)'), reload.index('self.server.running'))

    def test_pending_save_panel_checks_refusal_before_remembering_directory(self):
        source = (ROOT / 'tools/panel/app/VirtualPanel.swift').read_text()
        save = source.split('func saveRecording(', 1)[1].split('func download(', 1)[0]
        callback = save.split('panel.beginSheetModal(for: window)', 1)[1]
        self.assertIn('self.emulatorAllowed()', callback)
        self.assertLess(callback.index('self.emulatorAllowed()'), callback.index('UserDefaults.standard.set('))
