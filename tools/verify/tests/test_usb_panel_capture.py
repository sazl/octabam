"""Execute the assembled observer on synthetic, firmware-free wire messages.

The independent oracle is the existing PanelLink decoder. No stock bytes
or captured firmware traffic are fixtures. Missing optional CF instruments
skip execution; the source-presence test remains unconditional.
"""
import importlib.util
import pathlib
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'tools/panel'))
from panel_link import PanelLink

SOURCE = ROOT / 'modules/usb-panel-mirror/panel_capture.s'
INSTRUMENTS = all(shutil.which(n) for n in ('m68k-elf-as', 'm68k-elf-ld', 'm68k-elf-objcopy', 'm68k-elf-nm')) and importlib.util.find_spec('unicorn')


class CaptureSource(unittest.TestCase):
    def test_observer_exists(self):
        self.assertTrue(SOURCE.is_file(), 'accepted-byte observer is not implemented')


@unittest.skipUnless(INSTRUMENTS, 'needs optional ColdFire assembler and Unicorn')
class Capture(unittest.TestCase):
    def setUp(self):
        from unicorn import Uc, UC_ARCH_M68K, UC_MODE_BIG_ENDIAN, UC_HOOK_CODE
        from unicorn.m68k_const import UC_CPU_M68K_CFV4E
        self.temp = tempfile.TemporaryDirectory(prefix='usb-panel-capture-')
        self.addCleanup(self.temp.cleanup)
        work = pathlib.Path(self.temp.name)
        (work/'remix.inc').write_text((ROOT/'modules/usb-panel-mirror/protocol.inc').read_text())
        subprocess.run(['m68k-elf-as', '-I', str(work), '-mcpu=54455', '-o', str(work/'capture.o'), str(SOURCE)], check=True, capture_output=True)
        objects=[str(work/'capture.o')]
        subprocess.run(['m68k-elf-as','-I',str(work),'-mcpu=54455','-o',str(work/'boot.o'),str(ROOT/'modules/usb-panel-mirror/panel_boot.s')],check=True,capture_output=True)
        objects.append(str(work/'boot.o'))
        for name in getattr(self,'extra_sources',()):
            source=ROOT/'modules/usb-panel-mirror'/name
            subprocess.run(['m68k-elf-as','-I',str(work),'-mcpu=54455','-o',str(work/(name+'.o')),str(source)],check=True,capture_output=True)
            objects.append(str(work/(name+'.o')))
        subprocess.run(['m68k-elf-ld', '-Ttext=0x100000', '-o', str(work/'capture.elf'), *objects], check=True, capture_output=True)
        subprocess.run(['m68k-elf-objcopy', '-O', 'binary', str(work/'capture.elf'), str(work/'capture.bin')], check=True)
        lines = subprocess.check_output(['m68k-elf-nm', str(work/'capture.elf')], text=True).splitlines()
        self.symbols = {p[2]: int(p[0],16) for p in (s.split() for s in lines) if len(p)==3}
        self.uc = Uc(UC_ARCH_M68K, UC_MODE_BIG_ENDIAN)
        self.uc.ctl_set_cpu_model(UC_CPU_M68K_CFV4E)
        self.uc.mem_map(0x100000, 0x20000)
        self.uc.mem_write(0x100000, (work/'capture.bin').read_bytes())
        self.uc.mem_map(0x20000, 0x10000)
        self.uc.mem_map(0xfc07c000,0x1000)
        self.cost = 0
        def count(*_): self.cost += 1
        self.uc.hook_add(UC_HOOK_CODE, count)
        self.max_cost = 0

    def read(self, name, n): return bytes(self.uc.mem_read(self.symbols[name], n))
    def integer(self, name): return int.from_bytes(self.read(name,4),'big')
    def feed(self, data):
        from unicorn.m68k_const import UC_M68K_REG_A7, UC_M68K_REG_D2, UC_M68K_REG_PC, UC_M68K_REG_SR
        for value in data:
            self.uc.reg_write(UC_M68K_REG_SR,0x2700)
            self.uc.reg_write(UC_M68K_REG_A7,0x2fff0)
            self.uc.reg_write(UC_M68K_REG_D2,value)
            self.uc.mem_write(0x2fff0,struct.pack('>I',0x20000))
            self.cost=0
            self.uc.emu_start(self.symbols['pm_accept'],0x20000,count=1000)
            self.assertEqual(self.uc.reg_read(UC_M68K_REG_PC),0x20000)
            self.assertEqual(self.uc.reg_read(UC_M68K_REG_A7),0x2fff4)
            self.max_cost=max(self.max_cost,self.cost)

    def test_all_blocks_and_led_bounds_match_independent_decoder(self):
        wire=b''.join(bytes([0x10|page,col])+bytes(((page*19+col+i)^0x81)&255 for i in range(8))
                      for page in range(8) for col in range(0,128,8))
        wire+=b''.join(bytes([0x20|r if r<16 else 0xa0|(r-16),r*7&255]) for r in range(32))
        wire+=b''.join(bytes([0x30|(i&15),i]) for i in range(256))+b'\xb7\x00'
        oracle=PanelLink(); oracle.feed(wire)
        self.feed(wire)
        self.assertEqual(self.read('pm_lcd',1024),bytes(oracle.frame))
        self.assertEqual(self.integer('pm_lcd_count'),128)
        self.assertEqual(self.read('pm_row_values',32),bytes(oracle.led_rows[i] for i in range(32)))
        self.assertEqual(self.read('pm_level_values',256),bytes(oracle.leds[i] for i in range(256)))
        self.assertEqual(self.integer('pm_backlight_known'),1)
        self.assertLessEqual(self.max_cost+12,128, f'worst observer {self.max_cost}, plus wrapper budget12')

    def test_real_tap_preserves_registers_stack_and_saved_sr(self):
        from unicorn import UC_HOOK_CODE
        import unicorn.m68k_const as r
        self.uc.mem_map(0x400b9000,0x1000)
        self.uc.mem_map(0x40010000,0x1000)
        registers=[getattr(r,'UC_M68K_REG_D'+str(i)) for i in range(8)]+[getattr(r,'UC_M68K_REG_A'+str(i)) for i in range(7)]
        for name,resume in [('pm_byte_tap',0x40010af0),('pm_ring_tap',0x40010b70)]:
            self.uc.reg_write(r.UC_M68K_REG_SR,0x2700)
            self.uc.reg_write(r.UC_M68K_REG_A7,0x2fff0)
            values=[0x12340000+i for i in range(15)];values[2]=0x43;values[1]=0x2000
            for reg,v in zip(registers,values):self.uc.reg_write(reg,v)
            self.cost=0;self.uc.emu_start(self.symbols[name],resume,count=1000)
            self.assertEqual(self.uc.reg_read(r.UC_M68K_REG_PC),resume)
            self.assertEqual([self.uc.reg_read(reg) for reg in registers],values)
            self.assertEqual(self.uc.reg_read(r.UC_M68K_REG_A7),0x2fff0)
            self.assertLessEqual(self.cost,128)

    def test_preloader_fifo_never_executes_dram_then_hands_off(self):
        import unicorn.m68k_const as r
        # Exercise the actual image-resident tap while its dispatch pointer is
        # still zero. No writes to the DRAM observer may occur until handoff.
        self.uc.mem_map(0x400b9000,0x1000)
        self.uc.mem_map(0x40010000,0x1000)
        self.uc.mem_map(0x40000000,0x1000)
        self.uc.mem_map(0x4000f000,0x1000)
        self.uc.mem_write(0x4000f938,b'\x4e\x75')
        wire=b'\x10\x00'+b'\x81'*8
        for value in wire:
            self.uc.reg_write(r.UC_M68K_REG_SR,0x2700)
            self.uc.reg_write(r.UC_M68K_REG_A7,0x2fff0)
            self.uc.reg_write(r.UC_M68K_REG_D2,value)
            self.cost=0
            self.uc.emu_start(self.symbols['pm_boot_byte_tap'],0x40010af0,count=1000)
            self.assertLessEqual(self.cost,128)
        self.assertEqual(self.integer('pm_boot_count'),len(wire))
        self.assertEqual(self.integer('pm_generation'),0)
        self.uc.emu_start(self.symbols['pm_after_loader'],0x40000518,count=10000)
        self.assertEqual(self.integer('pm_generation'),1)
        self.assertEqual(self.read('pm_lcd',8),b'\x81'*8)
        self.assertEqual(self.integer('pm_boot_dispatch'),self.symbols['pm_accept'])
        # The worst complete-block capture goes through the genuine tap and
        # its dispatch, not a separately estimated wrapper allowance.
        self.feed(b'\x10\x08'+b'\xff'*7)
        self.uc.reg_write(r.UC_M68K_REG_A7,0x2fff0)
        self.uc.reg_write(r.UC_M68K_REG_D2,255)
        self.cost=0;self.uc.emu_start(self.symbols['pm_boot_byte_tap'],0x40010af0,count=1000)
        self.assertLessEqual(self.cost,128)
        self.assertEqual(self.integer('pm_lcd_count'),2)

    def test_preloader_fifo_overflow_fails_closed(self):
        import unicorn.m68k_const as r
        self.uc.mem_map(0x400b9000,0x1000);self.uc.mem_map(0x40010000,0x1000)
        self.uc.mem_map(0x40000000,0x1000);self.uc.mem_map(0x4000f000,0x1000)
        self.uc.mem_write(0x4000f938,b'\x4e\x75')
        self.uc.mem_write(self.symbols['pm_boot_count'],struct.pack('>I',3072))
        self.uc.reg_write(r.UC_M68K_REG_SR,0x2700);self.uc.reg_write(r.UC_M68K_REG_A7,0x2fff0)
        self.uc.reg_write(r.UC_M68K_REG_D2,0x43)
        self.uc.emu_start(self.symbols['pm_boot_byte_tap'],0x40010af0,count=1000)
        self.assertEqual(self.integer('pm_boot_count'),3072)
        self.assertEqual(self.integer('pm_boot_overflow'),1)
        self.uc.emu_start(self.symbols['pm_after_loader'],0x40000518,count=1000)
        self.assertEqual(self.integer('pm_active'),0)
        self.assertEqual(self.integer('pm_faults'),1)

    def test_partial_message_never_commits_and_duplicate_is_not_new_coverage(self):
        self.feed(b'\x10\x00'+b'\xff'*7)
        self.assertEqual(self.integer('pm_generation'),0)
        self.assertEqual(self.integer('pm_lcd_count'),0)
        self.feed(b'\xff')
        self.assertEqual(self.integer('pm_lcd_count'),1)
        self.feed(b'\x10\x00'+b'\x00'*8)
        self.assertEqual(self.integer('pm_lcd_count'),1)
        self.assertEqual(self.integer('pm_generation'),2)

    def test_unknown_and_bad_column_fail_closed(self):
        self.feed(b'\x10\x03'+bytes(8))
        self.assertEqual(self.integer('pm_active'),0)
        self.assertEqual(self.integer('pm_faults'),1)
        self.feed(b'\x10\x00'+bytes(8))
        self.assertEqual(self.integer('pm_lcd_count'),0)

    def test_boot_and_palette_records_do_not_become_state(self):
        self.feed(b'\x43\x60\x02\x70\x00\x74\x00\xb5\x10\x00\x20\x00\xb7')
        self.assertEqual(self.integer('pm_active'),1)
        self.assertEqual(self.integer('pm_lcd_count'),0)
        self.assertEqual(self.integer('pm_backlight_known'),0)
        self.assertEqual(self.integer('pm_messages'),5)

@unittest.skipUnless(INSTRUMENTS, 'needs optional ColdFire assembler and Unicorn')
class Snapshot(Capture):
    extra_sources=('panel_snapshot.s',)
    def setUp(self):
        self.assertTrue((ROOT/'modules/usb-panel-mirror/panel_snapshot.s').exists(), 'snapshot publisher absent')
        super().setUp()
    def call(self,name):
        from unicorn.m68k_const import UC_M68K_REG_SR,UC_M68K_REG_A7,UC_M68K_REG_PC
        self.uc.reg_write(UC_M68K_REG_SR,0x2000)
        self.uc.reg_write(UC_M68K_REG_A7,0x2fff0)
        self.uc.mem_write(0x2fff0,struct.pack('>I',0x20000))
        self.uc.emu_start(self.symbols[name],0x20000,count=500000)
        self.assertEqual(self.uc.reg_read(UC_M68K_REG_PC),0x20000)
    def put(self,name,value):self.uc.mem_write(self.symbols[name],struct.pack('>I',value))
    def test_canonical_snapshot_crc_and_static_screen(self):
        import zlib
        wire=b''.join(bytes([0x10|p,c])+bytes([p^c^0x81])*8 for p in range(8) for c in range(0,128,8))
        wire+=b'\xa1\x80\x21\x08\x3f\xff\x31\x00\xb7\x00'
        self.feed(wire)
        self.put('pm_lease_state',1);self.put('pm_token',1)
        self.call('pm_publish')
        self.assertEqual(self.integer('pm_lease_state'),2)
        body=self.read('pm_body',self.integer('pm_body_length'))
        expected=wire[:1280]+b'\x21\x08\xa1\x80\x31\x00\x3f\xff\xb7\x00'
        self.assertEqual(body,expected)
        self.assertEqual(self.integer('pm_body_crc'),zlib.crc32(expected))
        # A fresh lease on unchanged state still produces a complete snapshot.
        self.put('pm_lease_state',1);self.put('pm_token',2)
        self.uc.mem_write(0xfc07c00c,struct.pack('>I',13200000))
        self.call('pm_publish')
        self.assertEqual(self.integer('pm_lease_state'),2)
        self.assertEqual(self.read('pm_body',len(body)),body)
    def test_writer_preemption_discards_attempt(self):
        from unicorn import UC_HOOK_CODE
        self.feed(b''.join(bytes([0x10|p,c])+bytes(8) for p in range(8) for c in range(0,128,8)))
        self.put('pm_lease_state',1);self.put('pm_token',1)
        once=[False]
        def preempt(uc,pc,*_):
            if pc==self.symbols['pm_copy_checkpoint'] and not once[0]:
                once[0]=True;self.put('pm_generation',self.integer('pm_generation')+1)
        hook=self.uc.hook_add(UC_HOOK_CODE,preempt)
        self.call('pm_publish');self.uc.hook_del(hook)
        self.assertTrue(once[0]);self.assertEqual(self.integer('pm_lease_state'),1)
        self.call('pm_publish');self.assertEqual(self.integer('pm_lease_state'),2)


@unittest.skipUnless(INSTRUMENTS, 'needs optional ColdFire assembler and Unicorn')
class Control(Snapshot):
    extra_sources=('panel_snapshot.s','usb_panel.s')
    def setUp(self):
        super().setUp()
        self.uc.mem_map(0x46c8c000,0x1000)
        self.uc.mem_map(0xfc0b0000,0x2000)
        self.uc.mem_map(0x4ec95000,0x1000)
        self.uc.mem_map(0x4001d000,0x1000)
        self.uc.mem_write(0x4001d498,b'\x4e\x75')
        from unicorn import UC_HOOK_MEM_READ
        self.uc.hook_add(UC_HOOK_MEM_READ,lambda uc,*args:uc.mem_write(0xfc0b01b4,bytes(4)),begin=0xfc0b01b4,end=0xfc0b01b7)
        # CPU accesses the private reply through the uncached SDRAM alias.
        self.uc.mem_map(0x8100000,0x20000)
    def request(self,req,value=0,index=0,length=32,bm=0xc0):
        self.uc.mem_write(0x46c8ce08,struct.pack('<BBHHH',bm,req,value,index,length))
        self.call('pm_ctrl')
        return bytes(self.uc.mem_read(self.symbols['pm_reply']+0x8000000,64))
    def test_info_and_malformed_lengths(self):
        from usb_mirror_protocol import parse_response,parse_info,SetupRequest
        raw=self.request(0x57,length=64)
        response=parse_response(raw,request=SetupRequest(0xc0,0x57,0,0,64))
        h=response.header; info=parse_info(response)
        self.assertEqual(h.status,0);self.assertEqual(h.kind,1)
        self.assertEqual(info.max_response,64)
        self.assertEqual(self.request(0x57,length=65)[6],5)
        before=self.read('pm_lease_state',4)
        self.request(0x58,value=1,length=31)
        self.assertEqual(self.read('pm_lease_state',4),before)
        self.assertTrue(int.from_bytes(self.uc.mem_read(0xfc0b01c0,4),'big')&0x10000)
    def test_lease_duplicate_competing_release_and_expiry(self):
        self.feed(b''.join(bytes([0x10|p,c])+bytes(8) for p in range(8) for c in range(0,128,8)))
        a=self.request(0x58,value=7);self.assertEqual(a[6],1)
        token=int.from_bytes(a[16:18],'big');self.assertNotEqual(token,0)
        self.assertEqual(self.request(0x58,value=7)[16:18],a[16:18])
        self.assertEqual(self.request(0x58,value=8)[6],2)
        self.call('pm_publish')
        ready=self.request(0x58,value=7);self.assertEqual(ready[6],0)
        total=int.from_bytes(ready[18:20],'big')
        self.assertEqual(self.request(0x59,value=token,index=total)[6],0)
        self.assertEqual(self.request(0x59,value=token,index=total+1)[6],5)
        self.assertEqual(self.request(0x5a,value=token)[6],0)
        self.assertEqual(self.request(0x5a,value=token)[6],0)
        self.assertEqual(self.request(0x59,value=token,length=64)[6],4)
        self.request(0x58,value=9)
        self.uc.mem_write(0xfc07c00c,struct.pack('>I',132000001))
        self.assertEqual(self.request(0x58,value=9)[6],4)

    def test_token_rollover_changes_epoch_and_skips_zero(self):
        self.feed(b''.join(bytes([0x10|p,c])+bytes(8) for p in range(8) for c in range(0,128,8)))
        self.put('pm_next_token',0xfffe)
        last=self.request(0x58,value=7)
        self.assertEqual(int.from_bytes(last[16:18],'big'),0xffff)
        self.request(0x5a,value=0xffff)
        first=self.request(0x58,value=8)
        self.assertEqual(first[6],1)
        self.assertEqual(int.from_bytes(first[16:18],'big'),1)
        self.assertEqual(int.from_bytes(first[8:12],'big'),int.from_bytes(last[8:12],'big')+1)
        self.assertEqual(self.request(0x59,value=0xffff,length=64)[6],4)
        self.put('pm_epoch',0xffffffff)
        self.call('pm_transport_reset')
        self.assertEqual(self.integer('pm_epoch'),1)
        self.assertEqual(self.integer('pm_lease_state'),0)
        self.assertEqual(self.integer('pm_lcd_count'),128)

if __name__=='__main__': unittest.main()
