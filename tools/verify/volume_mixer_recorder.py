"""Native PICKUP recorder interval regression; disposable projects and actual frames."""
from __future__ import annotations
import json
import re
from volume_mixer_probe import PINS, KEYS, boot_port, call, peek, poke, u32, run, key, tap, watch, hits
from volume_mixer_audio import fixture
import ot_project


def recorder_fixture(project, out):
    _, wav = fixture(project,out)
    for path in (out/'project').glob('project.*'):
        data=path.read_bytes()
        for name,value in (('TEMPOx24',2880),('PATTERN_TEMPO_ENABLED',0)):
            data=re.sub(rb'(?m)^'+name.encode()+rb'=\d+',name.encode()+b'='+str(value).encode(),data)
        path.write_bytes(data)
    # Persist complete PICKUP assignments, own recorder slots and immediate
    # finite ONE boundary / MAX ONE2 and HOLD settings in all eight parts.
    for bank in sorted((out/'project').glob('bank*.work')):
        def edit(data):
            for part in range(8):
                base = ot_project.PART_BASE + part*ot_project.PART_STRIDE
                for track in range(8):
                    data[base+ot_project.MTYPE_OFF+track] = 4
                    data[base+0x2d3+track*5+4] = 128+track
                    start = base+ot_project.REC_SETUP_OFF+track*12
                    trig = (0,1,2,0,0,1,1,2)[track]
                    data[start:start+12] = bytes([1,0,7 if trig==0 else 64,trig,0,1,0,0,0,255,255,0])
        ot_project._bank_write(out/'project',int(bank.name[4:6]),edit,guard=False)
    return out/'project', wav


def recorder_state(p, track):
    result=[]
    for bank in range(2):
        data=peek(p,0x80004f1c+672*bank+84*track,84)
        result.append(dict(bank=bank,state=data[2],position=int.from_bytes(data[24:28],'big'),
                           end=int.from_bytes(data[28:32],'big'),length=int.from_bytes(data[32:36],'big')))
    return result


def running(rows):
    return next((row for row in rows if row['state']==2),None)


def select(p,track):
    p.proc.command(f'key 34 {1 << track}','ok'); run(p,80)
    p.proc.command('key 34 0','ok');run(p,80)
    assert peek(p,0x100b14cc,1)[0]==track


def capture(model, composed, project, wav, out, active):
    image,symbols=composed[:2]
    from verify_volume_mixer import load_composed, install_ui_mailbox, chord
    KEYS['REC2']=(0x25,0x10)
    report={'active':active,'intervals':[],'chords':[]}
    with boot_port(model,image,out,project=project,audio_input=wav) as p:
        if active:
            s=load_composed(p,composed)
        else:
            for address,expected,_ in PINS.values(): poke(p,address,bytes.fromhex(expected))
            install_ui_mailbox(p)
            run(p,100) # match the active loader-wrapper pre-roll
        p.proc.command('frame on','ok');run(p,100)
        assert u32(p,0x80001814)==2880,'recorder fixture tempo is not 120 BPM'
        for track,trig in enumerate((0,1,2)):
            select(p,track)
            watch(p,[0x40097924,0x400977cc,0x40006b18,0x40006edc])
            before=recorder_state(p,track)
            assert not running(before)
            key(p,'REC1'); pressed=recorder_state(p,track)
            run(p,500);held=recorder_state(p,track)
            expected_running = not active or trig==2
            assert bool(running(pressed))==expected_running,(trig,'press',pressed)
            assert bool(running(held))==expected_running,(trig,'held',held)
            if expected_running:
                advance=running(held)['position']-running(pressed)['position']
                assert 21900 <= advance <= 22200,(trig,'held duration',advance)
            held_status=call(p,0x40097168,track)
            assert held_status == (2 if expected_running else 0),(trig,'native held status',held_status)
            key(p,'REC1',False);released=recorder_state(p,track)
            final_status=stopped_status=None
            if trig==2:
                assert not running(released),(trig,'release did not stop',released)
                length=max(row['length'] for row in released)
                assert 24500 <= length <= 28000,('lost held interval',length)
                run(p,200)
                assert recorder_state(p,track)==released,'HOLD length/state changed after release'
                final_status=call(p,0x40097168,track)
                assert final_status==1,('HOLD did not return to native playback',final_status)
            else:
                assert running(released),(trig,'release did not retain/start recording',released)
                run(p,300);post=recorder_state(p,track)
                advance=(running(post)['position']-running(released)['position']) % (running(post)['length'] or 0x100000000)
                assert 13000 <= advance <= 13500,(trig,'post-release progression',released,post)
                tap(p,'REC2')
                run(p,700) # ONE finishes at its finite boundary; ONE2 finishes immediately.
                final_status=call(p,0x40097168,track)
                released=recorder_state(p,track)
                print(f'{model} active={active} TRIG={trig} REC2 status={final_status} banks={released}',flush=True)
                assert final_status==1,(trig,'REC2 did not finish into native playback',final_status,released)
                length=max(row['length'] for row in released)
                assert length > (13000 if active else 35000),(trig,'finalized length',length)
                tap(p,'REC2')
                stopped_status=call(p,0x40097168,track)
                assert stopped_status==0,(trig,'second REC2 did not stop playback',stopped_status)
            print(f'{model} active={active} TRIG={trig} length={length} final={released}',flush=True)
            records=hits(p)
            assert any(h[1]==0x40006b18 for h in records),'engine never armed a recorder'
            report['intervals'].append(dict(trig=trig,before=before,pressed=pressed,held=held,held_status=held_status,final=released,
                                            native_after_finish=final_status,native_after_stop=stopped_status,
                                            length=length,router_edges=[h[10:12] for h in records if h[1]==0x40097924]))
            p.proc.command('watch off','ok')
        if active:
            for track,trig,reverse in ((3,0,False),(4,0,True),(5,1,False),(6,1,True)):
                select(p,track);before=recorder_state(p,track)
                watch(p,[0x40097924,0x40006b18])
                key(p,'REC1');run(p,200);key(p,'MIXER')
                assert u32(p,s['vm_window']) and u32(p,s['vm_page'])==1
                for name in (('REC1','MIXER') if reverse else ('MIXER','REC1')):key(p,name,False)
                run(p,200)
                assert not hits(p),'consumed chord reached recorder'
                assert recorder_state(p,track)==before,'consumed chord changed engine recorder'
                tap(p,'NO')
                report['chords'].append(dict(trig=trig,release_reverse=reverse,unchanged=True))
            select(p,7);key(p,'REC1');run(p,200)
            assert running(recorder_state(p,7))
            key(p,'MIXER');assert not u32(p,s['vm_window']),'HOLD opened recording-sensitive shortcut'
            key(p,'MIXER',False);key(p,'REC1',False)
            assert not running(recorder_state(p,7))
            tap(p,'MIXER') # close stock mixer
            chord(p,'MIDI');tap(p,'RIGHT')
            assert u32(p,s['vm_window']) and u32(p,s['vm_page'])==1
            tap(p,'NO')
            report['hold_shortcut']='native recording retained; MIDI+MIXER then RIGHT opens inputs'
        report['passed']=True
    (out/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    return report


def run_recorder_cases(model,out,project,composed):
    assert composed,'recorder regression requires actual composed runtime'
    out.mkdir(parents=True,exist_ok=True)
    target,wav=recorder_fixture(project,out/'fixture')
    reports=[capture(model,composed,target,wav,out/name,active)
             for name,active in (('stock-handlers',False),('module',True))]
    stock,fixed=reports
    assert stock['intervals'][0]['length']==fixed['intervals'][0]['length']==44100,'ONE boundary length changed'
    difference=stock['intervals'][1]['length']-fixed['intervals'][1]['length']
    assert 24000 <= difference <= 28000,('ONE2 start deferral interval',difference)
    assert abs(stock['intervals'][2]['length']-fixed['intervals'][2]['length']) <= 32, 'HOLD duration changed beyond two audio blocks'
    result=dict(model=model,passed=True,reference=stock,module=fixed)
    (out/'report.json').write_text(json.dumps(result,indent=2)+'\n')
    return result
