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
        p.output_lock = threading.Lock()
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

class AudioOutputCloseRace(unittest.TestCase):
    """A request-owned stream must not outlive the selected backend owner."""
    def panel(self):
        p = PortAdapter().panel()
        p.output = None; p.output_note = None; p.output_lock = threading.Lock()
        p.audio_mode = 'main'; p.sound_busy = False
        p.worker = types.SimpleNamespace(join=lambda: None)
        p._close_take = lambda rt: None
        p.proc = None; p._save_sidecar = lambda: None
        return p
    def output(self):
        class Output:
            name = 'recording output'; index = 1; channels = 2
            running = True
            def __init__(self):
                self.stops = []
            def stop(self, reason=None):
                self.stops.append(reason); self.running = False
            def channel_map(self):
                return ['main L/R']
            def status(self):
                return {'running':self.running, 'latency_ms':0}
        return Output()
    def devices(self):
        return [{'index':1,'name':'recording output','channels':2}]
    def test_in_flight_constructor_cannot_publish_running_stream_after_close(self):
        p = self.panel(); backend = ps.PortPanelBackend(p)
        entered = threading.Event(); release = threading.Event(); closing = threading.Event(); close_finished = threading.Event()
        p._close_take = lambda rt: closing.set()
        output = self.output(); replies = []; errors = []
        def construct(*args):
            entered.set()
            if not release.wait(2):
                raise RuntimeError('test constructor was not released')
            return output
        def create():
            try:
                replies.append(p.set_output('1'))
            except BaseException as error:
                errors.append(error)
        def close():
            try:
                backend.close()
            except BaseException as error:
                errors.append(error)
            finally:
                close_finished.set()
        creator = threading.Thread(target=create, daemon=True)
        closer = threading.Thread(target=close, daemon=True)
        with patch.object(ps, 'sounddevice', return_value=(object(), None)), patch.object(ps, 'audio_devices', return_value=(self.devices(),None)), patch.object(ps, 'AudioOutput', side_effect=construct), contextlib.redirect_stdout(io.StringIO()):
            try:
                creator.start(); self.assertTrue(entered.wait(1))
                closer.start(); self.assertTrue(closing.wait(1))
                self.assertTrue(p.stop_event.is_set())
                self.assertFalse(close_finished.wait(0.1), 'close returned with constructor-owned audio still live')
            finally:
                release.set(); creator.join(2); closer.join(2)
        self.assertFalse(creator.is_alive()); self.assertFalse(closer.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(output.running, 'created output remains live after backend close')
        self.assertIsNone(p.output)
        self.assertEqual(output.stops, ['server exit'])
        self.assertFalse(replies[0][0], 'closed request reported a newly running output')
        self.assertTrue(p.actions.empty(), 'closed request queued emulator capture work')
        backend.close()
        self.assertEqual(output.stops, ['server exit'])
    def test_closed_panel_rejects_creation_before_audio_enumeration(self):
        p = self.panel(); backend = ps.PortPanelBackend(p); backend.close()
        with patch.object(ps, 'sounddevice', side_effect=AssertionError('enumerated after close')), patch.object(ps, 'AudioOutput', side_effect=AssertionError('created after close')):
            ok, reply = p.set_output('1')
        self.assertFalse(ok); self.assertEqual(reply['error'],'server closing')
        self.assertIsNone(p.output); self.assertTrue(p.actions.empty())
    def test_capture_request_does_not_queue_after_close(self):
        p = self.panel(); p.stop_event.set()
        p._queue_capture(ps.OUTPUT_CAPTURE)
        self.assertTrue(p.actions.empty())
    def test_open_panel_keeps_device_reuse_and_output_off_behavior(self):
        p = self.panel(); output = self.output()
        with patch.object(ps, 'sounddevice', return_value=(object(),None)), patch.object(ps, 'audio_devices', return_value=(self.devices(),None)), patch.object(ps, 'AudioOutput', return_value=output) as constructor, contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(p.set_output('1')[0])
            self.assertIs(p.output,output)
            self.assertTrue(p.set_output('1')[0])
            constructor.assert_called_once()
            self.assertTrue(p.set_output('off')[0])
        self.assertIsNone(p.output)
        self.assertEqual(output.stops,['stopped: /audio/output?device=off'])

class PortImageLaunch(unittest.TestCase):
    def panel(self, *, script=False, arguments=()):
        p = ps.Panel.__new__(ps.Panel)
        p.port_bin = 'recording.py' if script else 'recording-port'
        p.image = 'selected-image'; p.card_file = 'selected-card'
        p.card_rw = True; p.project = ('SET','PROJECT'); p.internal_clock = True
        p.port_args = list(arguments); p.sound_wanted = True; p.rt_wanted = True
        return p
    def test_real_port_validates_complete_effective_launch_after_user_flags(self):
        calls = []
        def launch(argv):
            calls.append(list(argv))
            return list(argv) + ['--main-park','validated:pair']
        module = types.SimpleNamespace(launch_args=launch)
        p = self.panel(arguments=['--mkii','--custom'])
        with patch.dict(sys.modules, {'port_image':module}):
            argv = p._port_argv()
        self.assertEqual(calls, [['recording-port','--image','selected-image','--card','selected-card','--interactive','--card-rw','--mount','--set','SET','--project','PROJECT','--internal-clock','--mkii','--custom','--dsp-rt']])
        self.assertEqual(argv,calls[0]+['--main-park','validated:pair'])
        self.assertEqual(p.port_args,['--mkii','--custom'])
    def test_legacy_python_standin_does_not_load_image_metadata(self):
        class Forbidden:
            def __getattr__(self,name):
                raise AssertionError('standin loaded image metadata')
        p = self.panel(script=True,arguments=['--dsp'])
        with patch.dict(sys.modules, {'port_image':Forbidden()}):
            argv = p._port_argv()
        self.assertEqual(argv[:2],[sys.executable,'recording.py'])
        self.assertNotIn('--main-park',argv)
        self.assertEqual(argv.count('--dsp'),1)
    def test_explicit_main_park_is_passed_unchanged_to_validation(self):
        calls = []
        def launch(argv):
            calls.append(list(argv)); return list(argv)
        p = self.panel(arguments=['--main-park','explicit:pair'])
        with patch.dict(sys.modules, {'port_image':types.SimpleNamespace(launch_args=launch)}):
            argv = p._port_argv()
        self.assertEqual(calls,[argv])
        self.assertEqual(argv[argv.index('--main-park')+1],'explicit:pair')
    def test_metadata_conflict_prevents_child_creation(self):
        p = self.panel(arguments=['--main-park','wrong:pair'])
        p.stop_event = threading.Event(); p.phase = 'booting'
        def conflict(argv):
            raise ValueError('main park conflicts with validated image')
        with patch.dict(sys.modules, {'port_image':types.SimpleNamespace(launch_args=conflict)}), patch.object(ps,'PortProc',side_effect=AssertionError('child spawned despite conflict')):
            with self.assertRaisesRegex(ValueError,'conflicts with validated image'):
                p._boot_port()
    def test_hardware_startup_never_imports_port_image_helper(self):
        class Forbidden:
            def __getattr__(self,name):
                raise AssertionError('hardware loaded image metadata')
        with patch.dict(sys.modules, {'port_image':Forbidden()}):
            StartupModes().invoke(['--source=hardware','--port=0'])

class PortImageAssociation(unittest.TestCase):
    """Caller integration with the real helper and invented ELF/image bytes."""
    panel = PortImageLaunch.panel
    def setUp(self):
        from test_port_image import PortImage
        import port_image
        self.fixture = PortImage()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.install_default_metadata()
        root = patch.object(port_image, 'ROOT', self.fixture.root)
        root.start(); self.addCleanup(root.stop)
    def selected_panel(self, arguments=()):
        panel = self.panel(arguments=arguments)
        panel.image = self.fixture.image
        return panel
    def pair(self):
        syms = self.fixture.symbols
        return f"{syms['mirror_idle_park']:#x}:{syms['mirror_idle_resume']:#x}"
    def test_current_image_receives_bound_markers_and_equal_override_is_preserved(self):
        argv = self.selected_panel()._port_argv()
        self.assertEqual(argv[-2:], ['--main-park',self.pair()])
        argv = self.selected_panel(['--main-park',self.pair()])._port_argv()
        self.assertEqual(argv.count('--main-park'),1)
    def test_conflicting_or_duplicate_effective_options_refuse_launch(self):
        for arguments in (['--main-park','0x47000012:0x47000000'],
                          ['--image',str(self.fixture.image)],
                          ['--main-park=' + self.pair()],
                          ['--image=' + str(self.fixture.image)]):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.selected_panel(arguments)._port_argv()
    def test_unrelated_default_loader_preserves_argv_without_inferred_park(self):
        expected = self.selected_panel()._port_argv()[:-2]
        image = bytearray(self.fixture.image.read_bytes()); image[-1] ^= 1
        self.fixture.image.write_bytes(image)
        for arguments in ([], ['--main-park', self.pair()]):
            with self.subTest(arguments=arguments):
                argv = self.selected_panel(arguments)._port_argv()
                self.assertEqual(argv, expected[:-1] + arguments + expected[-1:])
    def test_missing_default_metadata_preserves_unknown_dram_launch(self):
        expected = self.selected_panel()._port_argv()[:-2]
        (self.fixture.root / 'out/platform/loader.elf').unlink()
        self.assertEqual(self.selected_panel()._port_argv(), expected)
    def test_known_mirror_corrupt_branch_and_conflict_prevent_child_creation(self):
        def rejected(arguments, error):
            panel = self.selected_panel(arguments)
            panel.stop_event = threading.Event()
            with patch.object(ps, 'PortProc', side_effect=AssertionError('child spawned despite known mirror defect')):
                with self.assertRaisesRegex(ValueError, error):
                    panel._boot_port()
        rejected(['--main-park', '0x47000012:0x47000000'], 'conflict')
        park = self.fixture.symbols['mirror_idle_park'] - self.fixture.base
        self.fixture.raw = self.fixture.raw[:park] + bytes.fromhex('4e71') + self.fixture.raw[park + 2:]
        self.fixture.write_fixture()
        self.fixture.install_default_metadata()
        rejected([], 'branch')
    def test_stock_image_keeps_existing_arguments_without_elf_metadata(self):
        image = bytearray(self.fixture.image.read_bytes())
        image[0x1f896:0x1f89c] = bytes.fromhex('4eb940098a2c')
        self.fixture.image.write_bytes(image)
        (self.fixture.root / 'out/platform/runtime/runtime.elf').unlink()
        argv = self.selected_panel()._port_argv()
        self.assertNotIn('--main-park',argv)
        self.assertIn('--mkii',self.selected_panel(['--mkii'])._port_argv())
