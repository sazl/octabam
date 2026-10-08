"""Firmware-free browser event checks; optional existing Chromium/Chrome and Node instrument.

The fake HTTP source serves only synthetic status and generated PNG images. No device,
firmware, package install, or npm dependency is used. Missing browser tooling is
reported as a unittest skip; source-text checks are not browser evidence.
"""
import http.server
import json
import os
import signal
import sys
import struct
import zlib
import pathlib
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[3]
BROWSER_START_TIMEOUT_S = 30
BROWSER_STOP_TIMEOUT_S = 3


def _browser_stderr(log):
    size = os.fstat(log.fileno()).st_size
    return os.pread(log.fileno(), min(size, 16384), max(0, size - 16384)).decode('utf-8', 'replace')


def _wait_debugger_page(process, profile, log):
    """Wait for the port file AND a usable page endpoint; keep launch evidence."""
    marker = pathlib.Path(profile) / 'DevToolsActivePort'
    deadline = time.monotonic() + BROWSER_START_TIMEOUT_S
    last = 'DevToolsActivePort has not been published'
    while time.monotonic() < deadline:
        if marker.exists():
            try:
                port = int(marker.read_text().splitlines()[0])
                if not 0 < port < 65536:
                    raise ValueError(f'invalid debugging port {port}')
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list', timeout=.5) as response:
                    tabs = json.load(response)
                if not isinstance(tabs, list):
                    raise ValueError('debugging endpoint did not return a target list')
                page = next((tab for tab in tabs if isinstance(tab, dict) and tab.get('type') == 'page' and tab.get('webSocketDebuggerUrl')), None)
                if page is not None:
                    return page['webSocketDebuggerUrl']
                last = 'debugging HTTP endpoint has no page WebSocket yet'
            except (OSError, ValueError, IndexError) as error:
                last = f'debugging endpoint not ready: {error}'
        code = process.poll()
        if code is not None:
            break
        time.sleep(.05)
    raise AssertionError(f'Browser debugging readiness failed: pid={process.pid}, returncode={process.poll()}, '
                         f'executable={process.args[0]}, last={last}\nBrowser stderr:\n{_browser_stderr(log)}')


def _stop_browser(process):
    """Terminate the owned process group, escalate, and reap the launcher."""
    def send(sig):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            if process.poll() is None:
                raise
            # macOS can return EPERM as the reaped group's last members vanish.
            # Accept that race only after checking that no live group member remains.
            listing = subprocess.run(['ps', '-axo', 'pid=,pgid=,stat='], capture_output=True, text=True, timeout=2)
            if listing.returncode:
                raise
            for line in listing.stdout.splitlines():
                fields = line.split()
                if len(fields) >= 3 and int(fields[1]) == process.pid and not fields[2].startswith(('Z', 'X')):
                    raise
    send(signal.SIGTERM)
    try:
        process.wait(timeout=BROWSER_STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        send(signal.SIGKILL)
        process.wait(timeout=BROWSER_STOP_TIMEOUT_S)
    finally:
        # The launcher may exit before its children; stop remaining owned writers.
        send(signal.SIGKILL)


class PanelBrowserTests(unittest.TestCase):
    def test_capabilities_guard_real_browser_events(self):
        self.run_browser('capabilities')

    def test_no_frame_status_failure_reports_header_error(self):
        self.run_browser('no-frame-failure')

    def test_delayed_audio_status_cannot_restore_hardware_controls(self):
        self.run_browser('delayed-audio')

    def test_reconnect_refreshes_same_device_generation_publication(self):
        self.run_browser('publication')

    def test_backend_restart_reuses_seq_with_and_without_observed_failure(self):
        self.run_browser('lifecycle')

    def test_failed_png_retains_pixels_and_retries_without_false_live_status(self):
        self.run_browser('image-failure')

    def test_late_png_cannot_replace_new_publication_or_emulator_source(self):
        self.run_browser('late-image')

    def test_physical_inputs_holds_taps_turns_fader_and_tab_baselines(self):
        self.run_browser('inputs')

    def test_quiet_refresh_keeps_verified_pixels_bright_without_overlay(self):
        self.run_browser('quiet-refresh')

    def test_known_fader_is_visible_inside_scaled_panel_and_moves(self):
        print(self.run_browser('fader-geometry').strip())

    def test_cached_hardware_inputs_have_bounded_browser_poll_latency(self):
        print(self.run_browser('poll-latency').strip())

    def test_delayed_initial_skin_waits_for_panel_readiness(self):
        self.run_browser('delayed-initial')

    def test_delayed_second_tab_skin_waits_for_panel_readiness(self):
        self.run_browser('delayed-tab')

    def run_browser(self, mode):
        if os.name != 'posix':
            self.skipTest('optional Chromium process-group instrument requires a POSIX host')
        chromium = (os.environ.get('PANEL_BROWSER') or shutil.which('chromium') or shutil.which('chromium-browser')
                    or shutil.which('google-chrome'))
        if not chromium:
            mac_chrome = pathlib.Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
            chromium = str(mac_chrome) if mac_chrome.is_file() else None
        node = shutil.which('node')
        if not chromium or not node:
            self.skipTest('real browser instrument requires existing Chromium/Chrome and Node 22+; no dependencies installed')
        if subprocess.run([node, '-p', 'typeof WebSocket'], capture_output=True, text=True).stdout.strip() != 'function':
            self.skipTest('existing Node lacks built-in WebSocket')
        skin_dir = tempfile.TemporaryDirectory(prefix='panel-skin-')
        subprocess.run([sys.executable, str(ROOT / 'tools/panel/skin/gen_svg.py'), skin_dir.name], check=True, capture_output=True)
        skin_path = pathlib.Path(skin_dir.name)
        skin = ('window.SKIN=' + (skin_path / 'octatrack-elements.json').read_text() + ';window.SKIN_SVG=' + json.dumps((skin_path / 'octatrack.svg').read_text().replace('id="screen"', 'id="skin-screen"')) + ';').encode()
        state = {'source': 'hardware', 'connection_state': 'live', 'has_frame': True,
                 'instance_id': 'first', 'image_red': 0, 'image_fail': False,
                 'generation': 127, 'seq': 1, 'protocol': {'major': 1, 'minor': 0, 'epoch': 1, 'connection_id': 1}}
        def png(red):
            def chunk(kind, data):
                return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
            return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
                    + chunk(b'IDAT', zlib.compress(bytes((0, red, 0, 0)))) + chunk(b'IEND', b''))
        images = {red: png(red) for red in (0, 128, 255)}
        requests = []
        full_requests = []
        image_gate = threading.Event()
        image_gate.set()
        audio_gate = threading.Event()
        audio_gate.set()
        page_gate = threading.Event()
        page_gate.set()
        page_release_scheduled = threading.Event()
        page_delay = {"seconds": 1.5 if mode == "delayed-initial" else None}
        if mode == "delayed-initial":
            page_gate.clear()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                from urllib.parse import parse_qs, urlsplit
                path = urlsplit(self.path).path
                q = parse_qs(urlsplit(self.path).query)
                if path == '/fixture':
                    state.update(source=q['source'][0], connection_state=q['state'][0], has_frame=q['frame'][0] == '1')
                    if 'seq' in q:
                        state['seq'] = int(q['seq'][0])
                        state['image_red'] = 0 if state['seq'] == 1 else 255
                        state['protocol'].update(epoch=state['seq'], connection_id=state['seq'])
                    for key in ('instance_id', 'image_red', 'image_fail'):
                        if key in q:
                            state[key] = q[key][0] if key == 'instance_id' else int(q[key][0])
                    body = b'{}'
                elif path == '/input-fixture':
                    for key, value in json.loads(q['data'][0]).items():
                        if key == 'epoch':
                            state['protocol']['epoch'] = value
                        else:
                            state[key] = value
                    body = b'{}'
                elif path == '/page-gate':
                    page_gate.clear() if q.get('hold') == ['1'] else page_gate.set()
                    page_release_scheduled.clear()
                    page_delay['seconds'] = int(q['delay'][0]) / 1000 if 'delay' in q else None
                    body = b'{}'
                elif path == '/image-gate':
                    image_gate.clear() if q.get('hold') == ['1'] else image_gate.set()
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
                        if not page_gate.is_set() and page_delay['seconds'] is not None and not page_release_scheduled.is_set():
                            page_release_scheduled.set()
                            threading.Timer(page_delay['seconds'], page_gate.set).start()
                        if not page_gate.wait(10):
                            self.send_error(504, 'test page gate timed out'); return
                        body = skin
                    elif path == '/status':
                        payload = dict(state, backend=state['source'], read_only=state['source'] == 'hardware', capabilities=state.get('capabilities', ['screen']) if state['source'] == 'hardware' else ['screen','leds','controls','emulator','samples','card','audio'], model=None, device={'serial':'fake-only'}, build='synthetic', last_contact_at=time.time(), last_snapshot_at=time.time(), last_display_change_at=1, phase='ready', booted=True)
                        if state['source'] == 'legacy':
                            payload.pop('source'); payload['backend'] = 'port'
                        body = b'{invalid' if state['connection_state'] == 'server_error' else json.dumps(payload).encode()
                    elif path == '/screen.png':
                        body = b'invalid PNG' if state['image_fail'] else images[state['image_red']]
                        if not image_gate.wait(10):
                            self.send_error(504, 'test image gate timed out'); return
                    elif path == '/map':
                        body = (ROOT / 'tools/panel/key_map.json').read_bytes()
                    elif path == '/audio/status':
                        if not audio_gate.wait(10):
                            self.send_error(504, 'test audio gate timed out'); return
                        body = b'{"ok":true,"sound":true,"end":0}'
                    elif path == '/xfader':
                        body = b'{"ok":true,"pos":0}'
                    else:
                        body = b'{"ok":true,"result":"synthetic"}'
                self.send_response(200)
                self.send_header('Content-Type', 'text/html' if path == '/' else 'text/javascript' if path == '/skin.js' else 'image/png' if path == '/screen.png' else 'application/json')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # superseded browser image request was cancelled

            do_POST = do_GET

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix='panel-browser-') as profile, tempfile.TemporaryFile() as browser_log:
                chrome = subprocess.Popen([chromium,'--headless','--no-sandbox','--disable-gpu','--remote-debugging-port=0',f'--user-data-dir={profile}','about:blank'], stdout=subprocess.DEVNULL, stderr=browser_log, start_new_session=True)
                failure = None
                try:
                    ws = _wait_debugger_page(chrome, profile, browser_log)
                    try:
                        run = subprocess.run([node,str(pathlib.Path(__file__).with_name('panel_browser.js')),ws,f'http://127.0.0.1:{server.server_port}/',mode], capture_output=True, text=True, timeout=60)
                    except subprocess.TimeoutExpired as error:
                        raise AssertionError(f'Browser scenario {mode} timed out after debugging readiness\n'
                                             f'stdout={error.stdout!r}\nstderr={error.stderr!r}\nBrowser stderr:\n{_browser_stderr(browser_log)}') from error
                    self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                except BaseException as error:
                    failure = error
                    raise
                finally:
                    try:
                        _stop_browser(chrome)
                    except Exception as error:
                        details = f'Browser cleanup also failed: {error}\nBrowser stderr:\n{_browser_stderr(browser_log)}'
                        if failure is None:
                            raise AssertionError(details) from error
                        if hasattr(failure, 'add_note'):
                            failure.add_note(details)
                        else:
                            failure.args = (*failure.args, details)
        finally:
            image_gate.set()
            audio_gate.set()
            page_gate.set()
            server.shutdown()
            server.server_close()
            thread.join()
            skin_dir.cleanup()
        return run.stdout


class BrowserLaunchDiagnosticsTests(unittest.TestCase):
    def _failed_browser(self, hang):
        if os.name != 'posix' or not shutil.which('node'):
            self.skipTest('browser harness process checks require POSIX and Node')
        with tempfile.TemporaryDirectory(prefix='panel-fake-browser-') as directory:
            browser = pathlib.Path(directory) / 'fake-browser'
            ready = pathlib.Path(directory) / 'ready'
            code = [f'#!{sys.executable}', 'import pathlib, signal, sys, time',
                    "sys.stderr.write('intentional browser startup failure\\n'); sys.stderr.flush()"]
            if hang:
                code += ['signal.signal(signal.SIGTERM, signal.SIG_IGN)',
                         "profile = pathlib.Path(next(a.split('=', 1)[1] for a in sys.argv if a.startswith('--user-data-dir=')))",
                         "(profile / 'DevToolsActivePort').write_text('1\\n/fake\\n')", f'pathlib.Path({str(ready)!r}).touch()', 'while True: time.sleep(1)']
            else:
                code += [f'pathlib.Path({str(ready)!r}).touch()', 'sys.exit(7)']
            browser.write_text('\n'.join(code) + '\n'); browser.chmod(0o755)
            processes = []
            real_popen = subprocess.Popen
            def launch(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                if args[0][0] == str(browser):
                    processes.append(process)
                    deadline = time.monotonic() + 3
                    while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(ready.exists(), 'fake process must reach its deliberately failing state before the startup clock begins')
                return process
            try:
                with mock.patch.dict(os.environ, {'PANEL_BROWSER': str(browser)}), mock.patch.object(sys.modules[__name__], 'BROWSER_START_TIMEOUT_S', .3, create=True), mock.patch('subprocess.Popen', side_effect=launch):
                    try:
                        PanelBrowserTests('test_capabilities_guard_real_browser_events').run_browser('capabilities')
                    except Exception as error:
                        result = error
                    else:
                        self.fail('fake browser must fail before browser assertions run')
                self.assertIsInstance(result, AssertionError, 'launch failure must survive cleanup without becoming TimeoutExpired')
                self.assertIn('intentional browser startup failure', str(result), 'real browser stderr must explain launch failures')
                self.assertIn('debugging', str(result), 'failure must identify the unmet debugging readiness stage')
                if not hang:
                    self.assertIn('7', str(result), 'early process exit must report its return code')
                self.assertTrue(processes and all(process.poll() is not None for process in processes), 'browser processes must be reaped even when SIGTERM is ignored')
            finally:
                for process in processes:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=3)

    def test_post_reap_permission_error_requires_group_disappearance(self):
        if os.name != 'posix':
            self.skipTest('owned process-group check requires POSIX')
        process = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)
        process.wait(timeout=3)
        original = os.killpg
        def signal_group(pid, sig):
            if sig == signal.SIGKILL:
                raise PermissionError('simulated disappearing process group')
            return original(pid, sig)
        with mock.patch('os.killpg', side_effect=signal_group):
            _stop_browser(process)

    def test_post_reap_permission_error_does_not_hide_live_owned_child(self):
        if os.name != 'posix' or not hasattr(os, 'fork'):
            self.skipTest('owned child process-group check requires POSIX fork')
        process = subprocess.Popen([sys.executable, '-c', 'import os,time; child=os.fork(); time.sleep(60) if child==0 else None'], start_new_session=True)
        process.wait(timeout=3)
        original = os.killpg
        def signal_group(pid, sig):
            if sig == signal.SIGKILL:
                raise PermissionError('live owned child cannot be signalled')
        try:
            with mock.patch('os.killpg', side_effect=signal_group), self.assertRaisesRegex(PermissionError, 'live owned child'):
                _stop_browser(process)
        finally:
            try:
                original(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_early_browser_exit_reports_stderr_and_return_code(self):
        self._failed_browser(False)

    def test_debugger_startup_timeout_keeps_cause_and_kills_stubborn_browser(self):
        self._failed_browser(True)


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
