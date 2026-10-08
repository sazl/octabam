"""EP0 termination requirements for the physical panel exporter."""
import pathlib
import runpy
import unittest
ROOT=pathlib.Path(__file__).resolve().parents[3]
import sys
sys.path.insert(0,str(ROOT/'tools'))

class Termination(unittest.TestCase):
    def test_disable_automatic_zlp_before_enabling_ep0(self):
        mod=runpy.run_path(str(ROOT/'modules/usb-panel-mirror/manifest.py'))['MODULE']
        policy=next((p for p in mod.pokes if p.addr==0x4001d658),None)
        self.assertIsNotNone(policy,'64-byte panel data must retire without an extra hardware ZLP')
        self.assertEqual(policy.expect,bytes.fromhex('00400000'))
        self.assertEqual(policy.write,bytes.fromhex('20400000'))
        self.assertEqual(int.from_bytes(policy.expect,'big')^int.from_bytes(policy.write,'big'),1<<29)

    def test_all_carrier_configs_can_terminate_with_short_final_packet(self):
        # With auto ZLP disabled, a shorter-than-wLength reply that ends at
        # max packet size needs an explicit ZLP. No supported carrier's full
        # configuration has that shape. This catches future descriptor growth.
        desc=runpy.run_path(str(ROOT/'modules/usb-midi/descriptors.py'))
        for output in (None,*desc['HS_LAYOUT']):
            for inputs in ((None,*desc['IN_LAYOUT']) if output else (None,)):
                tables,size=desc['configs'](output,inputs)
                self.assertNotEqual(size%64,0,(output,inputs,'needs explicit control-transfer termination'))
                for table in tables.values():
                    self.assertEqual(len(table),size)

if __name__=='__main__':unittest.main()
