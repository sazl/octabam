"""Synthetic execution of accepted-input ColdFire hooks; no firmware fixtures."""
import pathlib
import struct
import subprocess
import unittest

from tools.verify.tests import test_usb_panel_capture as instrument

INSTRUMENTS = instrument.INSTRUMENTS


@unittest.skipUnless(INSTRUMENTS, 'needs optional ColdFire assembler and Unicorn')
class Inputs(instrument.Capture):
    def setUp(self):
        self.assertTrue((pathlib.Path(__file__).resolve().parents[3]/'modules/usb-panel-mirror/panel_input.s').is_file(), 'accepted input observer absent')
        super().setUp()
        self.uc.mem_map(0x46100000, 0x1000)
        self.uc.mem_map(0x400d1000, 0x1000)
        self.uc.mem_map(0x40092000, 0x2000)
        self.uc.mem_map(0x46c90000, 0x1000)
        self.uc.mem_write(0x400d16cc,b'\xff'*4)

    def call_input(self,name,**values):
        import unicorn.m68k_const as r
        self.uc.reg_write(r.UC_M68K_REG_SR,0x2700)
        self.uc.reg_write(r.UC_M68K_REG_A7,0x2fff0)
        self.uc.mem_write(0x2fff0,struct.pack('>I',0x20000))
        for name_,value in values.items():self.uc.reg_write(getattr(r,'UC_M68K_REG_'+name_),value)
        self.cost=0
        self.uc.emu_start(self.symbols[name],0x20000,count=1000)
        self.assertEqual(self.uc.reg_read(r.UC_M68K_REG_PC),0x20000)
        return self.cost

    def seed(self, rows=bytes(8)):
        self.uc.mem_write(0x46100b18,rows)
        self.call_input('pm_input_seed')

    def keys(self,row,value):
        self.call_input('pm_input_key',D1=row,D3=value)

    def encoder(self,row,delta):
        self.call_input('pm_input_encoder',D1=row,D3=delta&255)

    def record(self):return self.read('pm_inputs',168)
    def counters(self):return struct.unpack('>64H',self.record()[12:140])
    def turns(self):return struct.unpack('>14H',self.record()[140:])

    def test_held_at_start_and_edges_wrap_without_duplicate_presses(self):
        self.seed(bytes([3])+bytes(7))
        self.assertEqual(self.record()[:12],bytes([0xb8,1,5,0,3])+bytes(7))
        self.assertEqual(self.counters(),(0,)*64)
        generation=self.integer('pm_generation')
        self.keys(0,3);self.assertEqual(self.integer('pm_generation'),generation)
        self.keys(0,0);self.keys(0,1);self.keys(0,1)
        self.assertEqual(self.counters()[:2],(1,0))
        self.uc.mem_write(self.symbols['pm_inputs']+12,b'\xff\xff')
        self.keys(0,0);self.keys(0,1)
        self.assertEqual(self.counters()[0],0)
        self.keys(7,127)
        self.assertEqual(self.counters()[56:63],(1,)*7)
        before=self.record();self.keys(8,255)
        self.assertEqual(self.record(),before)

    def test_encoder_both_directions_negative_128_and_wrap(self):
        self.seed()
        self.encoder(0,7);self.encoder(0,-3);self.encoder(6,-128)
        self.assertEqual(self.turns()[:2],(7,3))
        self.assertEqual(self.turns()[-2:],(0,128))
        self.uc.mem_write(self.symbols['pm_inputs']+140,b'\xff\xfe')
        self.encoder(0,3);self.assertEqual(self.turns()[0],1)
        before=self.record();generation=self.integer('pm_generation')
        self.encoder(7,2);self.encoder(0,0)
        self.assertEqual(self.record(),before)
        self.assertEqual(self.integer('pm_generation'),generation)

    def test_fader_baseline_uses_only_proven_stock_last_position(self):
        for stock,known,fader in ((0xffffffff,5,0),(128,5,0),(255,5,0),(127,7,0),(64,7,63),(0,7,127)):
            self.uc.mem_write(0x400d16cc,struct.pack('>I',stock))
            self.uc.mem_write(self.symbols['pm_inputs']+2,bytes(166))
            self.seed()
            self.assertEqual(self.record()[2:4],bytes((known,fader)))
            self.assertEqual(self.counters(),(0,)*64)

    def test_invalid_calibrated_values_do_not_corrupt_input_record(self):
        self.seed()
        for stock in (128,255,0xffffffff):
            before=self.record();generation=self.integer('pm_generation')
            self.call_input('pm_input_fader',D1=stock)
            self.assertEqual(self.record(),before)
            self.assertEqual(self.integer('pm_generation'),generation)

    def test_fader_unknown_until_calibrated_value_and_orientation(self):
        self.seed()
        self.assertEqual(self.record()[2:4],b'\x05\x00')
        for calibrated,expected in [(127,0),(64,63),(0,127)]:
            self.call_input('pm_input_fader',D1=calibrated)
            self.assertEqual(self.record()[2:4],bytes([7,expected]))
        generation=self.integer('pm_generation')
        self.call_input('pm_input_fader',D1=0)
        self.assertEqual(self.integer('pm_generation'),generation)

    def install_fader_callback_fixtures(self):
        # Authored callback boundary fixtures, not firmware slices. The final
        # image gate separately executes both real stock callback functions.
        work=pathlib.Path(self.temp.name)
        source=work/'fader-callbacks.s'
        source.write_text(f""".text
        move.l %d2,-(%sp)
        moveq #127,%d2
        sub.l 8(%sp),%d2
        andi.l #255,%d2
        jmp {self.symbols['pm_fader_inverted_tap']:#x}
        .org 0x20
        move.l (%sp)+,%d2
        rts
        move.l %d2,-(%sp)
        moveq #0,%d2
        move.b 11(%sp),%d2
        jmp {self.symbols['pm_fader_tap']:#x}
        .org 0x40
        move.l (%sp)+,%d2
        rts
""")
        subprocess.run(['m68k-elf-as','-mcpu=54455','-o',str(work/'fader.o'),str(source)],check=True,capture_output=True)
        subprocess.run(['m68k-elf-objcopy','-O','binary',str(work/'fader.o'),str(work/'fader.bin')],check=True,capture_output=True)
        self.uc.mem_write(0x40092f88,(work/'fader.bin').read_bytes())

    def callback(self,address,value):
        import unicorn.m68k_const as r
        self.uc.reg_write(r.UC_M68K_REG_SR,0x251f)
        self.uc.reg_write(r.UC_M68K_REG_A7,0x2fff0)
        self.uc.reg_write(r.UC_M68K_REG_D2,0x12345678)
        self.uc.mem_write(0x2fff0,struct.pack('>II',0x20000,value))
        self.uc.emu_start(address,0x20000,count=1000)
        self.assertEqual(self.uc.reg_read(r.UC_M68K_REG_PC),0x20000)
        self.assertEqual(self.uc.reg_read(r.UC_M68K_REG_A7),0x2fff4)
        self.assertEqual(self.uc.reg_read(r.UC_M68K_REG_D2),0x12345678)
        self.assertEqual(bytes(self.uc.mem_read(0x2fff4,4)),struct.pack('>I',value))

    def test_normal_and_factory_inverted_callbacks_keep_arguments_and_orientation(self):
        self.seed();self.install_fader_callback_fixtures()
        for address,inverted in ((0x40092fac,False),(0x40092f88,True)):
            for received in (0,64,127):
                self.callback(address,received)
                stock=127-received if inverted else received
                self.assertEqual(int.from_bytes(self.uc.mem_read(0x400d16cc,4),'big'),stock)
                self.assertEqual(self.record()[2:4],bytes((7,127-stock)))
                generation=self.integer('pm_generation')
                self.callback(address,received)
                self.assertEqual(self.integer('pm_generation'),generation,'duplicate callback is no new input change')
                self.assertEqual(self.counters(),(0,)*64)
                self.assertEqual(self.turns(),(0,)*14)

    def test_reports_before_seed_do_not_create_history(self):
        self.keys(0,255);self.encoder(0,4);self.call_input('pm_input_fader',D1=64)
        self.assertEqual(self.record()[2:],bytes(166))
        self.assertEqual(self.integer('pm_generation'),0)

    def test_encoder_observation_precedes_stock_ui_descriptor_gate(self):
        from unicorn import UC_HOOK_CODE
        import unicorn.m68k_const as r
        self.seed()
        self.uc.mem_write(0x400d16b4,struct.pack('>I',6))
        # Zero descriptor/pending words, like a UI with no assigned parameter.
        self.uc.reg_write(r.UC_M68K_REG_A6,0x46c90250)
        self.uc.reg_write(r.UC_M68K_REG_D1,99)
        self.uc.reg_write(r.UC_M68K_REG_D3,128)
        self.uc.reg_write(r.UC_M68K_REG_SR,0x2700)
        self.uc.reg_write(r.UC_M68K_REG_A7,0x2fff0)
        destinations=(0x400924ea,0x4009250c,0x40092526)
        def stop(uc,pc,*_):
            if pc in destinations:uc.emu_stop()
        hook=self.uc.hook_add(UC_HOOK_CODE,stop)
        self.uc.emu_start(self.symbols['pm_encoder_tap'],0x20000,count=1000)
        self.uc.hook_del(hook)
        self.assertEqual(self.uc.reg_read(r.UC_M68K_REG_PC),0x400924ea,'physical observation must precede UI descriptor filtering')
        self.assertEqual(self.uc.reg_read(r.UC_M68K_REG_D1),6,'displaced stock row load must replay')
        self.assertEqual(self.turns()[-2:],(0,128))

    def test_actual_detours_preserve_registers_sr_stack_and_replay(self):
        import unicorn.m68k_const as r
        registers=[getattr(r,'UC_M68K_REG_D'+str(i)) for i in range(8)]+[getattr(r,'UC_M68K_REG_A'+str(i)) for i in range(7)]
        self.seed()
        self.uc.mem_write(0x400d16b4,struct.pack('>I',2))
        for name,resume,pending in [('pm_key_tap',0x400923c6,0),('pm_encoder_tap',0x400924ea,0),('pm_fader_tap',0x40092fc8,0),('pm_fader_inverted_tap',0x40092fa8,0)]:
            values=[0x12340000+i for i in range(15)];values[1]=2;values[2]=64;values[3]=255;values[14]=0x46c90250
            self.uc.mem_write(0x46c90258,struct.pack('>I',pending))
            for reg,v in zip(registers,values):self.uc.reg_write(reg,v)
            self.uc.reg_write(r.UC_M68K_REG_SR,0x251f);self.uc.reg_write(r.UC_M68K_REG_A7,0x2fff0)
            self.cost=0;self.uc.emu_start(self.symbols[name],resume,count=1000)
            self.assertEqual(self.uc.reg_read(r.UC_M68K_REG_PC),resume)
            self.assertEqual([self.uc.reg_read(reg) for reg in registers],values)
            self.assertEqual(self.uc.reg_read(r.UC_M68K_REG_A7),0x2fff0)
            # Replayed row load or committed-value store clears Z/N/V/C and retains X.
            expected_sr=0x2510
            # Unicorn's reg_read(SR) omits lazy CCR; read with the CPU instruction.
            self.uc.mem_write(resume,b'\x40\xc0')  # move.w sr,d0
            self.uc.emu_start(resume,resume+2,count=1)
            self.assertEqual(self.uc.reg_read(r.UC_M68K_REG_D0)&0xffff,expected_sr)
            self.assertLessEqual(self.cost,128,'bounded complete-row observer incl all wrappers')


@unittest.skipUnless(INSTRUMENTS, 'needs optional ColdFire assembler and Unicorn')
class InputSnapshot(instrument.Snapshot):
    def test_input_suffix_maximum_crc_coherence_and_frozen_lease(self):
        import zlib
        from unicorn import UC_HOOK_CODE
        self.feed(b''.join(bytes([0x10|p,c])+bytes(8) for p in range(8) for c in range(0,128,8)))
        self.feed(b''.join(bytes([0x20|r if r<16 else 0xa0|(r-16),r]) for r in range(32)))
        self.feed(b''.join(bytes([0x30|(i&15),i]) for i in range(256))+b'\xb7\x01')
        inputs=bytes([0xb8,1,7,63])+bytes(range(8))+bytes((i&255 for i in range(156)))
        self.uc.mem_write(self.symbols['pm_inputs'],inputs)
        self.put('pm_lease_state',1);self.put('pm_token',1)
        once=[False]
        def preempt(uc,pc,*_):
            if pc==self.symbols['pm_input_copy_checkpoint'] and not once[0]:
                once[0]=True;self.put('pm_generation',self.integer('pm_generation')+1)
                uc.mem_write(self.symbols['pm_inputs']+3,b'\x40')
        hook=self.uc.hook_add(UC_HOOK_CODE,preempt)
        self.call('pm_publish');self.uc.hook_del(hook)
        self.assertTrue(once[0]);self.assertEqual(self.integer('pm_lease_state'),1)
        self.call('pm_publish')
        self.assertEqual(self.integer('pm_body_length'),2026)
        body=self.read('pm_body',2026)
        self.assertEqual(body[-168:],inputs[:3]+b'\x40'+inputs[4:])
        self.assertEqual(self.integer('pm_body_crc'),zlib.crc32(body))
        self.uc.mem_write(self.symbols['pm_inputs']+3,b'\x41')
        self.put('pm_generation',self.integer('pm_generation')+1)
        self.call('pm_publish')
        self.assertEqual(self.read('pm_body',2026),body,'READY lease body stays immutable')

if __name__=='__main__':unittest.main()
