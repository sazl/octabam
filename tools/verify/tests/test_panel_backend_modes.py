"""Asset-free hardware startup and port ownership regression tests."""
import contextlib
import io
import pathlib
import socket
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / 'tools/panel'))
import panel_server as ps
import panel_check as pc
from test_panel_routes import RecordingBackend

class StartupModes(unittest.TestCase):
    def invoke(self, args, backend=None, serve=None):
        backend = backend or RecordingBackend()
        def construct(selector=None, poll_hz=5):
            backend.started = (selector, poll_hz)
            return backend
        module = types.SimpleNamespace(HardwareBackend=construct)
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            for name in ('Panel', 'SamplePool', 'build_card', 'port_available', 'sidecar_save'):
                stack.enter_context(patch.object(ps, name, side_effect=AssertionError('emulator side effect: ' + name)))
            stack.enter_context(patch.object(ps.subprocess, 'Popen', side_effect=AssertionError('spawned a child')))
            stack.enter_context(patch.object(ps, 'ROOT', pathlib.Path(tmp)))
            stack.enter_context(patch.dict(sys.modules, {'hardware_backend': module}))
            stack.enter_context(patch.object(ps.ThreadingHTTPServer, 'serve_forever', serve or (lambda s: None)))
            stack.enter_context(patch.object(sys, 'argv', ['panel_server'] + args))
            stack.enter_context(patch.object(ps.signal, 'signal'))
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                ps.main()
            self.assertEqual(list(pathlib.Path(tmp).iterdir()), [])
        return backend
    def test_hardware_starts_without_emulator_assets_and_closes_selected_backend(self):
        b = self.invoke(['--source', 'hardware', '--port', '0'])
        self.assertTrue(b.closed.is_set())
        self.assertIsNone(ps.Handler.panel)
        self.assertEqual(b.started, (None, 5.0))
    def test_selection_and_poll_rate_reach_only_hardware_constructor(self):
        b = self.invoke(['--source=hardware', '--port=0', '--usb-device=topology:1-2.3', '--usb-poll-hz=7.5'])
        self.assertEqual(b.started, ('topology:1-2.3', 7.5))
    def test_explicit_emulator_options_rejected_before_bind(self):
        for arg in ('--image=x', '--project=x', '--set=X', '--name=X', '--card=x', '--midi-clock', '--port-bin=x', '--port-arg=--dsp', '--audio=x', '--sound=on', '--mki'):
            with self.subTest(arg=arg), patch.object(ps, 'ThreadingHTTPServer', side_effect=AssertionError('bound')):
                with self.assertRaises(SystemExit):
                    self.invoke(['--source=hardware', arg])
    def test_invalid_poll_and_selector_rejected_before_bind(self):
        for arg in ('--usb-poll-hz=nan', '--usb-poll-hz=inf', '--usb-poll-hz=0', '--usb-poll-hz=-1', '--usb-device=arbitrary', '--usb-device=serial:', '--usb-device=topology:1-'):
            with self.subTest(arg=arg), patch.object(ps, 'ThreadingHTTPServer', side_effect=AssertionError('bound')):
                with self.assertRaises(SystemExit):
                    self.invoke(['--source=hardware', arg])
    def test_bind_conflict_has_no_backend_or_filesystem_work(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); sock.listen()
            b = RecordingBackend()
            with self.assertRaises(SystemExit):
                self.invoke(['--source=hardware', '--port', str(sock.getsockname()[1])], backend=b)
            self.assertFalse(hasattr(b, 'started'))
            self.assertFalse(b.closed.is_set())
    def test_startup_failure_closes_bound_server(self):
        captured = []
        original = ps.ThreadingHTTPServer
        def server(*a, **kw):
            s = original(*a, **kw); captured.append(s); return s
        module = types.SimpleNamespace(HardwareBackend=lambda **kw: (_ for _ in ()).throw(RuntimeError('startup')))
        with patch.object(sys, 'argv', ['panel_server', '--source=hardware', '--port=0']), patch.object(ps, 'ThreadingHTTPServer', side_effect=server), patch.dict(sys.modules, {'hardware_backend': module}):
            with self.assertRaises(RuntimeError):
                ps.main()
        self.assertEqual(captured[0].socket.fileno(), -1)

class CheckerModes(unittest.TestCase):
    def test_mismatch_precedes_emulator_driver_constructor(self):
        with patch.object(sys, 'argv', ['panel_check']), patch.object(pc, 'get', return_value={'source': 'hardware', 'backend': 'hardware'}), patch.object(pc, 'Panel', side_effect=AssertionError('emulator driver constructed')):
            with self.assertRaises(SystemExit):
                pc.main()
    def test_hardware_checker_reads_only_observation_routes(self):
        paths = []
        def get(url, **kw):
            path = url.split('http://localhost:8563')[1]; paths.append(path)
            return {'/status': {'source':'hardware', 'backend':'hardware', 'connection_state':'live', 'has_frame':True, 'capabilities':['screen','leds']}, '/screen.png': ps._png_rgb(128,64,[ps.LCD_OFF*128]*64), '/screen.txt': ('.'*128+'\n')*64, '/leds': {'bits':'00'*17, 'ids':{}}}[path]
        with tempfile.TemporaryDirectory() as tmp, patch.object(sys, 'argv', ['panel_check', '--source=hardware', '--out', tmp]), patch.object(pc, 'get', side_effect=get), patch.object(pc, 'Panel', side_effect=AssertionError('emulator driver constructed')):
            self.assertEqual(pc.main(), 0)
        self.assertTrue(set(paths) <= {'/status','/screen.png','/screen.txt','/leds'})

class PortAdapter(unittest.TestCase):
    def panel(self):
        p = ps.Panel.__new__(ps.Panel)
        p.lock = threading.Lock(); p.stop_event = threading.Event()
        p.frame = b'port png'; p.screen_txt = 'port text'; p.led_bits = bytearray(range(64)); p.led_ids = {5: 33}; p.link = None
        p.seq = 7; p.snapshot_at = 100; p.display_changed_at = 90
        p.image = 'image'; p.booted = True; p.ran_ms = 42; p.fault = None; p.phase = 'ready'; p.backend = 'port'; p.backend_note = ''
        p.meter = types.SimpleNamespace(value=123); p.rtmeter = types.SimpleNamespace(value=1.0); p.pace = {'on':'1','rate':'1','ratio':'1'}
        p.restarts = 2; p.card_busy = False; p.clock_note = 'clock'; p.sound = True; p.sound_rt = True; p.sound_note = 'sound'; p.frame_always = True; p.playing = True
        p.card_file = pathlib.Path('card'); p.card_persistent = True; p.card_rw = True; p.card_ejected = False; p.card_mount = None; p.project = ('SET','PROJECT')
        p.actions = ps.queue.Queue()
        return p
    def test_port_status_retains_every_legacy_field_and_values(self):
        p = self.panel(); b = ps.PortPanelBackend(p, model='mkii')
        with patch.object(ps, 'host_nice', return_value=0):
            self.assertEqual(b.status(), {'booted':True,'seq':7,'ran_ms':42,'fault':None,'image':'image','phase':'ready','backend':'port','backend_note':None,'speed':123,'rt':1.0,'pace': {'on':True,'rate':1.0,'ratio':1.0,'lag_ms':None,'slices':None,'reanchors':None,'slept_s':None,'busy_s':None,'stop':None},'nice':0,'restarts':2,'card_busy':False,'clock':'clock','sound':True,'sound_rt':True,'sound_note':'sound','frame_always':True,'pid':ps.os.getpid(),'script_mtime':ps.SCRIPT_MTIME,'playing':True,'card':'card','card_mode':'persistent','card_rw':True,'card_ejected':False,'card_mount':None,'project':{'set':'SET','name':'PROJECT'},'source':'port','read_only':False,'capabilities':sorted(b.capabilities)})
        self.assertEqual(b.snapshot().png, b'port png')
        self.assertEqual(ps.led_payload(b.snapshot().leds), p.led_payload())
        p.led_bits[0] = 99
        self.assertEqual(b.snapshot().leds.bits[0], 99)
    def test_close_suppresses_owner_then_preserves_cleanup_order_once(self):
        p = self.panel(); calls = []
        def record(name):
            def f(*a, **kw):
                self.assertTrue(p.stop_event.is_set()); calls.append(name)
            return f
        p.worker = types.SimpleNamespace(join=record('worker stopped'))
        p._close_take = record('take'); p.output = types.SimpleNamespace(stop=record('audio'))
        p.proc = types.SimpleNamespace(command=record('flush'), quit=record('quit'))
        p._save_sidecar = record('sidecar')
        b = ps.PortPanelBackend(p); b.close(); b.close()
        self.assertEqual(calls, ['worker stopped','take','audio','flush','quit','sidecar'])
    def test_stopped_panel_rejects_work_without_queueing(self):
        p = self.panel(); p.stop_event.set()
        ok, result = p.do(lambda rt: self.fail('executed'), timeout=0)
        self.assertFalse(ok)
        self.assertTrue(p.actions.empty())
    def test_stopped_panel_never_respawns_or_boots_child(self):
        p = self.panel(); p.stop_event.set()
        with patch.object(ps, 'PortProc', side_effect=AssertionError('spawned')):
            self.assertFalse(p._respawn('gone'))
            with self.assertRaises(ps.PortDied):
                p._boot_port()
        self.assertEqual(p.restarts, 2)
    def test_http_response_write_never_holds_port_view_or_status_lock(self):
        p = self.panel(); b = ps.PortPanelBackend(p)
        handler = ps.Handler.__new__(ps.Handler); handler.backend = b
        def send(*a):
            self.assertTrue(p.lock.acquire(blocking=False), 'response holds publication lock')
            p.lock.release()
        handler._send = send
        for path in ('/screen.png', '/screen.txt', '/status', '/leds'):
            handler._view_get(path)

class PortShutdown(unittest.TestCase):
    def test_pending_command_cancels_before_cleanup_without_killing_child(self):
        proc = ps.PortProc.__new__(ps.PortProc)
        proc.lock = threading.Lock(); proc.lines = ps.queue.Queue(); proc.log = []; proc.commands = 0
        proc.cancel = threading.Event(); sent = threading.Event(); results = []
        proc.proc = types.SimpleNamespace(stdin=types.SimpleNamespace(write=lambda data: sent.set(),flush=lambda: None))
        proc.kill = lambda: self.fail('cleanup must own child quit')
        def command():
            try:
                proc.command('pacestatus','pacestatus',timeout=1)
            except ps.PortDied as error:
                results.append(str(error))
        worker = threading.Thread(target=command,daemon=True); worker.start()
        self.assertTrue(sent.wait(1)); proc.cancel.set(); worker.join(0.4)
        self.assertFalse(worker.is_alive(),'pending command blocks close')
        self.assertEqual(results,['server closing'])
    def test_cleanup_commands_still_run_after_cancellation(self):
        proc = ps.PortProc.__new__(ps.PortProc)
        proc.lock = threading.Lock(); proc.lines = ps.queue.Queue(); proc.log = []; proc.commands = 0
        proc.cancel = threading.Event(); proc.cancel.set()
        proc.lines.put('card flushed')
        sent = []
        proc.proc = types.SimpleNamespace(stdin=types.SimpleNamespace(write=sent.append,flush=lambda: None))
        self.assertEqual(proc.command('card flush','card',timeout=1,closing=True),'card flushed')
        self.assertEqual(sent,[b'card flush\n'])

class StoppedReboot(unittest.TestCase):
    def test_stopped_reboot_cannot_repeat_take_or_card_cleanup(self):
        p = PortAdapter().panel(); p.stop_event.set()
        p._stop_child = lambda: self.fail('stopped reboot cleans child')
        self.assertFalse(p._reboot_port('reboot'))
