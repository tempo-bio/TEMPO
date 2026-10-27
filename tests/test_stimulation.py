"""Serial protocol tests use a transcript transport, never a physical port."""
from dataclasses import asdict
import json
import threading
import time
import unittest
from unittest.mock import patch
from tempo_stimulation import (PacerService, StimulusParameters, PacerStatus,
    ConfirmationRequired, DeviceRejected)


def reply(request_id=1, mode='REST_DISCHARGE', parameters=None, **extra):
    parameters = parameters or StimulusParameters()
    return dict(id=request_id, device='TEMPO_AU26_V2', protocol=1, result='ok', mode=mode,
                cycling=False, uptime_ms=100, watchdog_ms=2000,dac_reference_v=4.77,
                **asdict(parameters),**extra)


class TranscriptPort:
    def __init__(self,*args,**kwargs):
        self.rx=bytearray(); self.commands=[]; self.closed=False
        self.parameters=StimulusParameters(); self.mode='REST_DISCHARGE'
        self.fail=False; self.replies=[]
    def write(self,payload):
        request,command=payload.decode().strip().split(' ',1)
        self.commands.append(command)
        if self.fail: raise OSError('USB disconnected')
        result='ok'
        if command.startswith('set '):
            _,v,hz,ms,ch,confirmed=command.split()
            params=StimulusParameters(float(v),float(hz),float(ms),int(ch))
            if params.needs_confirmation and confirmed=='0': result='confirmation_required'
            else: self.parameters=params
        if command in ('hello','stop'): self.mode='REST_DISCHARGE'
        if command=='pace': self.mode='PACING'
        message=reply(int(request),self.mode,self.parameters)
        message['result']=result
        self.rx.extend(json.dumps(message).encode()+b'\n')
        return len(payload)
    def read(self,size):
        if not self.rx: time.sleep(.001)
        value=bytes(self.rx[:size]); del self.rx[:size]; return value
    def close(self): self.closed=True


class StimulationTests(unittest.TestCase):
    def test_invalid_or_nonfinite_parameters_never_reach_transport(self):
        for changes in (dict(amplitude_v=float('nan')),dict(frequency_hz=0),dict(channel=True),
                        dict(pulse_width_ms=1000),dict(amplitude_v=1.1),dict(pulse_width_ms=.01)):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                StimulusParameters(**changes)

    def test_confirmation_thresholds_and_atomic_parameter_command(self):
        self.assertFalse(StimulusParameters().needs_confirmation)
        self.assertTrue(StimulusParameters(frequency_hz=4).needs_confirmation)
        self.assertTrue(StimulusParameters(frequency_hz=3,pulse_width_ms=51).needs_confirmation)
        self.assertEqual(StimulusParameters().command(),'set 0.600000 1.000000 2.000 0 0')

    def test_wrong_firmware_or_missing_watchdog_rejected(self):
        for changes in (dict(protocol=2),dict(device='different'),dict(watchdog_ms=0),dict(mode='UNKNOWN')):
            value=reply(); value.update(changes)
            with self.subTest(changes=changes),self.assertRaises(ConnectionError): PacerStatus.parse(value)

    def exchange_service(self,port):
        service=PacerService(); service._board=port
        return service

    def test_stale_response_cannot_acknowledge_new_command(self):
        port=TranscriptPort(); port.rx.extend(json.dumps(reply(99,mode='PACING')).encode()+b'\n')
        service=self.exchange_service(port)
        self.assertEqual(service._exchange('stop').mode,'REST_DISCHARGE')

    def test_confirmation_does_not_partially_apply(self):
        port=TranscriptPort(); service=self.exchange_service(port)
        requested=StimulusParameters(amplitude_v=.9,frequency_hz=2,channel=3)
        with self.assertRaises(ConfirmationRequired): service._exchange(requested.command())
        self.assertEqual(port.parameters,StimulusParameters())
        self.assertEqual(service._exchange(requested.command(True)).parameters,requested)

    def test_missing_matching_reply_times_out(self):
        port=TranscriptPort()
        def stale(payload):
            port.rx.extend(json.dumps(reply(100)).encode()+b'\n'); return len(payload)
        port.write=stale
        with self.assertRaises(TimeoutError): self.exchange_service(port)._exchange('stop',timeout=.02)

    def test_stop_preempts_waiting_for_an_ordinary_command_response(self):
        port=TranscriptPort(); original=port.write
        port.write=lambda payload: len(payload) if payload.decode().strip().endswith('pace') else original(payload)
        service=self.exchange_service(port); service.connected=True
        stopped=service.submit('stop')
        with self.assertRaisesRegex(DeviceRejected,'interrupted'): service._exchange('pace')
        self.assertEqual(stopped.result().mode,'REST_DISCHARGE')

    def test_heartbeat_worker_and_ordered_final_discharge(self):
        port=TranscriptPort(); service=PacerService(serial_factory=lambda *a,**k:port)
        service.connect('fixture').result(3)
        try:
            service.submit(StimulusParameters().command()).result(1)
            service.submit('pace').result(1)
            time.sleep(.35)
            self.assertIn('ping',port.commands)
            self.assertEqual(service.submit('stop').result(1).mode,'REST_DISCHARGE')
        finally:
            service.close().result(2)
        self.assertEqual(port.commands[-1],'stop'); self.assertTrue(port.closed)

    def test_stop_invalidates_queued_start_without_cancelling_other_stops(self):
        service=PacerService(); service.connected=True
        start=service.submit('pace'); first_stop=service.submit('stop'); second_stop=service.submit('stop')
        queued=[]
        while not service._queue.empty(): queued.append(service._queue.get())
        self.assertEqual([x[3] for x in queued],['stop','stop','pace'])
        self.assertNotEqual(queued[-1][2],service._generation)

    def test_link_loss_never_reports_confirmed_stop(self):
        port=TranscriptPort(); service=PacerService(serial_factory=lambda *a,**k:port)
        service.connect('fixture').result(3)
        port.fail=True
        with self.assertRaises(OSError): service.submit('pace').result(2)
        with self.assertRaises(OSError): service.closed.result(2)
        self.assertIsNone(service.status); self.assertFalse(service.connected)


if __name__=='__main__': unittest.main()
