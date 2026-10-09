#!/usr/bin/env python3
"""Bounded read-only probe of the PROVISIONAL USB panel mirror (no flashing).

Needs PyUSB (uv sync --extra emu) and native libusb. Physical firmware exporting
this protocol and audio coexistence remain unverified. Only standard device
STRING discovery and INFO/BEGIN/READ/RELEASE vendor IN requests are issued.
Topology selects a location and does not prove physical unit identity.

Examples:
  python tools/hw/usb_panel.py info
  python tools/hw/usb_panel.py --selector serial:UNIT capture --output out/snapshot.dat
  python tools/hw/usb_panel.py watch --count 10
"""
import argparse
from dataclasses import asdict
import json
import logging
from pathlib import Path
import sys
import time

if __package__ in (None,''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tools.panel import usb_mirror as m
from tools.panel import usb_mirror_protocol as p


def _positive_int(value):
    number=int(value)
    if number<=0: raise argparse.ArgumentTypeError('must be positive')
    return number


def _duration(value):
    import math
    number=float(value)
    if not math.isfinite(number) or number<=0: raise argparse.ArgumentTypeError('must be finite and positive')
    return number


def _report(transport,response,info):
    label={int(p.Model.UNKNOWN):'unknown',int(p.Model.MKI):'MKI',int(p.Model.MKII):'MKII'}.get(info.model,f'unknown ({info.model})')
    return dict(provisional=True,device=transport.identity.as_dict(),
                protocol=dict(major=response.header.major,minor=response.header.minor),
                model_label=label,info=asdict(info),build_id_is_prefix=True,
                epoch=response.header.epoch,generation=response.header.generation,
                flags=response.header.flags,last_contact_unix=time.time())


def _emit(report):
    print(json.dumps(report,sort_keys=True),flush=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--selector',help='serial:<serial> or topology:<bus>-<port>[.<port>]')
    parser.add_argument('--timeout-ms',type=_positive_int,default=p.USB_TIMEOUT_MS)
    parser.add_argument('--verbose',action='store_true',help='log STRING discovery separately from mirror transfers')
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('info',help='one INFO report with exact metadata and limits')
    capture=commands.add_parser('capture',help='one validated canonical body to user-selected local file')
    capture.add_argument('--output',type=Path,required=True)
    watch=commands.add_parser('watch',help='bounded INFO heartbeat / validated generation log')
    watch.add_argument('--count',type=_positive_int)
    watch.add_argument('--duration',type=_duration)
    args=parser.parse_args(argv)
    if args.command=='watch' and args.count is None and args.duration is None:
        parser.error('watch needs --count or --duration to bound polling')
    if args.timeout_ms>60000: parser.error('--timeout-ms must be <=60000')
    try: selector=m.DeviceSelector.parse(args.selector)
    except ValueError as error: parser.error(str(error))
    if args.verbose: logging.basicConfig(level=logging.DEBUG,format='%(name)s: %(message)s')
    transport=None
    try:
        transport=m.open_transport(selector,timeout_ms=args.timeout_ms)
        client=m.SnapshotClient(transport,connection_id=1,timeout_ms=args.timeout_ms)
        if args.command in ('info','capture'):
            response,info=client.info()
            report=_report(transport,response,info)
            if args.command=='capture':
                snapshot=client.snapshot(response,info)
                args.output.write_bytes(snapshot.body)
                report.update(output=str(args.output),snapshot=asdict(snapshot.identity),summary=asdict(snapshot.summary))
            _emit(report)
            return 0
        deadline=time.monotonic()+args.duration if args.duration is not None else None
        count=0; previous=None; interval=p.DEFAULT_MIN_POLL_MS/1000
        while (args.count is None or count<args.count) and (deadline is None or time.monotonic()<deadline):
            start=time.monotonic(); count+=1
            try:
                response,info=client.info(); interval=max(info.min_poll_ms,p.DEFAULT_MIN_POLL_MS)/1000
                report=_report(transport,response,info)
                current=(response.header.epoch,response.header.generation)
                report['changed']=current!=previous
                if current!=previous:
                    snapshot=client.snapshot(response,info)
                    report.update(snapshot=asdict(snapshot.identity),summary=asdict(snapshot.summary))
                    previous=current
                _emit(report)
            except m.TransportError as error:
                _emit(dict(error=error.code,message=str(error),contact_verified=False,sample=count))
                if error.code in ('disconnected','closed'): return 1
            if args.count is not None and count>=args.count: break
            pause=max(0,interval-(time.monotonic()-start))
            if deadline is not None: pause=min(pause,max(0,deadline-time.monotonic()))
            if pause: time.sleep(pause)
        return 0
    except (m.TransportError,OSError) as error:
        _emit(dict(error=getattr(error,'code','local_io_error'),message=str(error)))
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if transport is not None: transport.close()

if __name__=='__main__': raise SystemExit(main())
