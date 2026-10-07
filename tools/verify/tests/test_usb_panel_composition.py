"""Firmware-free selection rules for the optional mirror dispatcher."""
import pathlib
import runpy
import sys
import unittest
ROOT=pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'tools'))
from remix import ledger, registry

class Composition(unittest.TestCase):
    def setUp(self):
        registry._cache=None
        self.mods=registry.modules()
    def test_core_is_discovered_without_dsp(self):
        self.assertTrue('USB PANEL MIRROR' in self.mods,'core missing')
        self.assertIsNone(self.mods['USB PANEL MIRROR'].dsp)
    def test_exactly_one_dispatcher(self):
        path=ROOT/'modules/usb-panel-mirror/manifest.py'
        self.assertTrue(path.exists(),'mirror selection helper is absent')
        helper=runpy.run_path(str(path))['mirror_inc']
        with self.assertRaisesRegex(ValueError,'USB MIDI'): helper({'USB PANEL MIRROR'})
        with self.assertRaisesRegex(ValueError,'dispatch'): helper({'USB PANEL MIRROR','USB MIDI'})
        outputs=('MAIN','MAIN CUE','MASTER','TRACKS','TRACKS MAIN CUE')
        for output in outputs:
            keys={'USB MIDI','USB PANEL MIRROR','USB AUDIO OUT '+output}
            self.assertIsInstance(helper(keys),str)
            with self.assertRaisesRegex(ValueError,'dispatch'): helper(keys|{'USB PANEL MIRROR STANDALONE'})
        self.assertIsInstance(helper({'USB MIDI','USB PANEL MIRROR','USB PANEL MIRROR STANDALONE'}),str)
    def test_standalone_conflicts_with_each_audio_owner(self):
        self.assertTrue('USB PANEL MIRROR STANDALONE' in self.mods,'adapter missing')
        adapter=self.mods['USB PANEL MIRROR STANDALONE']
        for output in ('MAIN','MAIN CUE','MASTER','TRACKS','TRACKS MAIN CUE'):
            problems=ledger.check([adapter,self.mods['USB PANEL MIRROR'],self.mods['USB MIDI'],self.mods['USB AUDIO OUT '+output]])
            self.assertTrue(any(self.mods['USB AUDIO OUT '+output].name in p for p in problems),problems)
    def test_all_descriptor_forms_are_identical_with_feature(self):
        descriptor=runpy.run_path(str(ROOT/'modules/usb-midi/descriptors.py'))
        for output in descriptor['HS_LAYOUT']:
            for inputs in (None,*descriptor['IN_LAYOUT']):
                keys={'USB MIDI',output}|({inputs} if inputs else set())
                self.assertEqual(descriptor['remix_inc'](keys),descriptor['remix_inc'](keys|{'USB PANEL MIRROR'}))
                complete=keys|{'USB PANEL MIRROR'}
                for key in tuple(complete):complete.update(self.mods[key].requires)
                self.assertEqual(ledger.check([self.mods[key] for key in complete]),[])
        self.assertEqual(descriptor['remix_inc']({'USB MIDI'}),descriptor['remix_inc']({'USB MIDI','USB PANEL MIRROR','USB PANEL MIRROR STANDALONE'}))

    def test_audio_include_feature_off_is_original(self):
        layout=runpy.run_path(str(ROOT/'modules/usb-audio-out-tracks-main-cue/manifest.py'))['layout_inc']
        for i in range(5):
            off=layout(i)({'USB MIDI'})
            self.assertNotIn('USB_PANEL_MIRROR',off)
            on=layout(i)({'USB MIDI','USB PANEL MIRROR'})
            self.assertIn('.set USB_PANEL_MIRROR, 1',on)

    def test_selected_image_gate_and_matrix_are_reached(self):
        gates=self.mods['USB PANEL MIRROR'].gates
        self.assertEqual(len(gates),1)
        self.assertEqual(gates[0].script,'tools/verify/verify_usb_panel.py')
        self.assertTrue(gates[0].remix_arg)
        self.assertEqual(gates[0].stage,'image')
        verifier=runpy.run_path(str(ROOT/gates[0].script))
        accepted,rejected=verifier['matrix_selections']()
        self.assertEqual(rejected,[])
        self.assertEqual(len(accepted),20)
        self.assertEqual(len({(row['output'],row['input']) for row in accepted}),20)
        for row in accepted:
            self.assertEqual(ledger.check([self.mods[key] for key in row['modules']]),[])
if __name__=='__main__':unittest.main()
