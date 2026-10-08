"""Guard the cache boundary the cacheless port cannot reproduce.

No firmware fixtures. Hardware PDBG6/8/9/10 established the failing boundary;
these checks prevent routing boot around its image-resident synchronization.
"""
import pathlib
import runpy
import shutil
import subprocess
import tempfile
import unittest
ROOT = pathlib.Path(__file__).resolve().parents[3]
import sys
sys.path.insert(0, str(ROOT/'tools'))

class BootCache(unittest.TestCase):
    def test_handoff_enters_image_resident_sync(self):
        module = runpy.run_path(str(ROOT/'modules/usb-panel-mirror/manifest.py'))['MODULE']
        handoff = next(d for d in module.detours if d.site == 0x40000512)
        unit = next(u for u in module.linked if u.label == handoff.unit)
        self.assertFalse(unit.dram, 'boot must synchronize IC before fetching DRAM instructions')
        self.assertEqual(handoff.symbol, 'pm_cache_handoff')
        refs = {r.addr: r for r in module.symbol_refs}
        self.assertEqual(refs[0x400d7b00].symbol, 'pm_accept')
        self.assertEqual(refs[0x400d7b04].symbol, '_edata')
        self.assertEqual(refs[0x400d7b08].symbol, 'pm_after_loader')

    @unittest.skipUnless(shutil.which('m68k-elf-as'), 'needs ColdFire assembler')
    def test_sync_encoding_and_private_target_slots(self):
        with tempfile.TemporaryDirectory() as name:
            work = pathlib.Path(name)
            source = ROOT/'modules/usb-panel-mirror/panel_cache.s'
            subprocess.run(['m68k-elf-as', '-mcpu=54455', '-o', str(work/'cache.o'), str(source)], check=True, capture_output=True)
            subprocess.run(['m68k-elf-ld', '-Ttext=0x400d7a00', '-o', str(work/'cache.elf'), str(work/'cache.o')], check=True, capture_output=True)
            subprocess.run(['m68k-elf-objcopy', '-O', 'binary', str(work/'cache.elf'), str(work/'cache.bin')], check=True)
            raw = (work/'cache.bin').read_bytes()
            dis = subprocess.check_output(['m68k-elf-objdump', '-d', str(work/'cache.elf')], text=True)
            self.assertEqual(raw[0x100:0x10c], bytes(12))
            self.assertEqual(len(raw), 0x10c)
            self.assertEqual(dis.count('cpushl ic'), 5)
            self.assertNotIn('cpushl dc', dis)
            self.assertNotIn('cpushl bc', dis)
            self.assertNotIn('movec', dis, 'retain stock CACR/ACR settings')
            self.assertIn('rts', dis)


class CacheTransfer(unittest.TestCase):
    def test_range_coverage_and_entry_state(self):
        try:
            from unicorn import Uc, UC_ARCH_M68K, UC_MODE_BIG_ENDIAN, UC_HOOK_CODE
            import unicorn.m68k_const as r
        except ImportError:
            self.skipTest('needs optional Unicorn')
        with tempfile.TemporaryDirectory() as name:
            work = pathlib.Path(name)
            subprocess.run(['m68k-elf-as','-mcpu=54455','-o',str(work/'u.o'),str(ROOT/'modules/usb-panel-mirror/panel_cache.s')],check=True,capture_output=True)
            subprocess.run(['m68k-elf-ld','-Ttext=0x400d7a00','-o',str(work/'u.elf'),str(work/'u.o')],check=True,capture_output=True)
            subprocess.run(['m68k-elf-objcopy','-O','binary',str(work/'u.elf'),str(work/'u.bin')],check=True)
            uc=Uc(UC_ARCH_M68K,UC_MODE_BIG_ENDIAN)
            uc.ctl_set_cpu_model(r.UC_CPU_M68K_CFV4E)
            uc.mem_map(0x400d7000,4096);uc.mem_map(0x20000,4096);uc.mem_map(0x40a96000,4096)
            uc.mem_write(0x400d7a00,(work/'u.bin').read_bytes())
            first,end,target=0x40a95e7c,0x40a9b060,0x40a9603a
            uc.mem_write(0x400d7b00,b''.join(n.to_bytes(4,'big') for n in (first,end,target)))
            regs=[getattr(r,'UC_M68K_REG_D'+str(i)) for i in range(8)]+[getattr(r,'UC_M68K_REG_A'+str(i)) for i in range(7)]
            values=[0x12340000+i for i in range(15)]
            uc.reg_write(r.UC_M68K_REG_SR,0x2700);uc.reg_write(r.UC_M68K_REG_A7,0x20ff0)
            for reg,value in zip(regs,values):uc.reg_write(reg,value)
            pushed=[]
            def instruction(machine,pc,size,data):
                if machine.mem_read(pc,2)==b'\xf4\xa8':
                    # Instrument addresses only. This deliberately does not
                    # pretend to reproduce hardware instruction-cache behavior.
                    pushed.append(machine.reg_read(r.UC_M68K_REG_A0))
                    machine.reg_write(r.UC_M68K_REG_PC,pc+2)
            uc.hook_add(UC_HOOK_CODE,instruction)
            uc.emu_start(0x400d7a00,target,count=30000)
            self.assertEqual(uc.reg_read(r.UC_M68K_REG_PC),target)
            self.assertEqual([uc.reg_read(reg) for reg in regs],values)
            self.assertEqual(uc.reg_read(r.UC_M68K_REG_A7),0x20ff0)
            self.assertEqual(uc.reg_read(r.UC_M68K_REG_SR),0x2700)
            expected=[s+w for s in range(0,4096,16) for w in range(4)]
            expected+=list(range(first&~15,end,16))
            expected+=[a+0x08000000 for a in range(first&~15,end,16)]
            self.assertEqual(pushed,expected)

if __name__ == '__main__':
    unittest.main()
