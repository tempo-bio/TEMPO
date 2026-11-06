"""AU26 V2 protocol and serial ownership. No Qt or camera operations live here."""
from collections import deque
from concurrent.futures import Future
from dataclasses import asdict, dataclass
import itertools
import json
import math
import queue
import threading
import time

PROTOCOL_VERSION = 2
SUPPORTED_PROTOCOLS = (1, 2)
HEARTBEAT_SECONDS = .25
WATCHDOG_MS = 2000


class DeviceRejected(ValueError):
    pass


class ConfirmationRequired(DeviceRejected):
    pass


@dataclass(frozen=True)
class StimulusParameters:
    amplitude_v: float = .6
    frequency_hz: float = 1.
    pulse_width_ms: float = 2.
    channel: int = 0

    def __post_init__(self):
        for value in (self.amplitude_v, self.frequency_hz, self.pulse_width_ms):
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
                raise ValueError('Stimulation settings must be finite numbers.')
        if not 0 <= self.amplitude_v <= 1:
            raise ValueError('Requested voltage must be between 0 and 1 V.')
        if not .1 <= self.frequency_hz <= 100:
            raise ValueError('Frequency must be between 0.1 and 100 Hz.')
        if self.pulse_width_ms < .1 or self.pulse_width_ms * self.frequency_hz >= 1000:
            raise ValueError('Pulse width must be at least 0.1 ms and shorter than the period.')
        if type(self.channel) is not int or not 0 <= self.channel <= 7:
            raise ValueError('Output channel must be between 0 and 7.')

    @property
    def needs_confirmation(self):
        return (self.amplitude_v > .8 or self.frequency_hz > 3 or
                self.pulse_width_ms > 10 or self.pulse_width_ms * self.frequency_hz > 150)

    def command(self, confirmed=False):
        return f'set {self.amplitude_v:.6f} {self.frequency_hz:.6f} {self.pulse_width_ms:.3f} {self.channel} {int(confirmed)}'


@dataclass(frozen=True)
class PacerStatus:
    mode: str
    parameters: StimulusParameters
    cycling: bool
    uptime_ms: int
    watchdog_ms: int
    dac_reference_v: float
    protocol: int = 2
    supports_timing: bool = True

    @classmethod
    def parse(cls, reply):
        protocol = reply.get('protocol')
        if reply.get('device') != 'TEMPO_AU26_V2' or protocol not in SUPPORTED_PROTOCOLS:
            raise ConnectionError('Install TEMPO’s V2 firmware on the Uno R4 Minima, then reconnect.')
        mode = reply.get('mode')
        if mode not in ('REST_DISCHARGE', 'PACING', 'CONSTANT_STIM', 'SQUARE_10HZ', 'DISCONNECTED_HIZ'):
            raise ConnectionError('The stimulator returned an unknown output state.')
        params = StimulusParameters(reply['amplitude_v'], reply['frequency_hz'],
                                    reply['pulse_width_ms'], reply['channel'])
        if type(reply.get('cycling')) is not bool or type(reply.get('uptime_ms')) is not int:
            raise ConnectionError('Malformed stimulator status.')
        if reply.get('watchdog_ms') != WATCHDOG_MS:
            raise ConnectionError('The stimulator does not provide the required communication watchdog.')
        reference = reply.get('dac_reference_v')
        if not isinstance(reference, (float, int)) or not math.isfinite(reference) or reference <= 0:
            raise ConnectionError('Invalid DAC reference in stimulator status.')
        protocol_int = int(protocol)
        supports_timing = (protocol_int == 2)
        return cls(mode, params, reply['cycling'], reply['uptime_ms'], WATCHDOG_MS, float(reference),
                   protocol=protocol_int, supports_timing=supports_timing)

    def to_dict(self):
        return dict(mode=self.mode, parameters=asdict(self.parameters), cycling=self.cycling,
                    uptime_ms=self.uptime_ms, watchdog_ms=self.watchdog_ms,
                    dac_reference_v=self.dac_reference_v, protocol=self.protocol,
                    supports_timing=self.supports_timing)


class ClockSyncModel:
    """Estimates the linear mapping from device us to host monotonic ns with uncertainty."""

    def __init__(self):
        self.samples = deque(maxlen=64)  # tuples: (t_send_ns, t_device_us, t_recv_ns, rtt_ns, t_mid_ns)
        self.alpha = 1000.0  # nominal 1000 ns / 1 us
        self.beta = 0.0
        self.uncertainty_ms = 10.0
        self.total_pulses = 0
        self._last_device_us = None
        self._rollover_offset = 0

    def unwrap_device_us(self, t_us: int) -> int:
        t_us = int(t_us)
        if self._last_device_us is not None and t_us < (self._last_device_us - 0x7FFFFFFF):
            self._rollover_offset += 0x100000000
        self._last_device_us = t_us
        return t_us + self._rollover_offset

    def record_sample(self, t_send_ns, t_device_us, t_recv_ns):
        dev_unwrapped = self.unwrap_device_us(t_device_us)
        rtt_ns = max(0, int(t_recv_ns - t_send_ns))
        t_mid_ns = (int(t_send_ns) + int(t_recv_ns)) / 2.0
        self.samples.append((int(t_send_ns), dev_unwrapped, int(t_recv_ns), rtt_ns, t_mid_ns))
        self._refit()

    def _refit(self):
        if not self.samples:
            return
        min_rtt = min(s[3] for s in self.samples)
        # Filter samples whose RTT is within max(1.5 * min_rtt, min_rtt + 2ms)
        threshold = max(1.5 * min_rtt, min_rtt + 2_000_000)
        filtered = [s for s in self.samples if s[3] <= threshold]
        if not filtered:
            filtered = list(self.samples)

        if len(filtered) == 1:
            _, dev_us, _, rtt, mid_ns = filtered[0]
            self.alpha = 1000.0
            self.beta = mid_ns - 1000.0 * dev_us
            self.uncertainty_ms = max(0.01, min_rtt / 2_000_000.0)
            return

        x = [float(s[1]) for s in filtered]
        y = [float(s[4]) for s in filtered]
        n = len(x)
        mean_x = sum(x) / n
        mean_y = sum(y) / n
        s_xx = sum((xi - mean_x) ** 2 for xi in x)
        s_xy = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))

        if s_xx > 1e-9:
            slope = s_xy / s_xx
            # Hardware crystal tolerance bound [990.0, 1010.0]
            self.alpha = max(990.0, min(1010.0, slope))
        else:
            self.alpha = 1000.0
        self.beta = mean_y - self.alpha * mean_x

        residuals = [abs(yi - (self.alpha * xi + self.beta)) for xi, yi in zip(x, y)]
        jitter_ms = (max(residuals) if residuals else 0.0) / 1_000_000.0
        self.uncertainty_ms = max(0.01, (min_rtt / 2_000_000.0) + jitter_ms)

    def map_device_time(self, t_device_us):
        dev_unwrapped = self.unwrap_device_us(t_device_us)
        mapped_host_ns = int(round(self.alpha * float(dev_unwrapped) + self.beta))
        return mapped_host_ns, float(self.uncertainty_ms)

    def get_alignment_summary(self):
        return {
            'alpha': float(self.alpha),
            'beta': float(self.beta),
            'uncertainty_ms': float(self.uncertainty_ms),
            'total_pulses': int(self.total_pulses),
        }


class PacerService:
    """One thread owns the port, heartbeats, ordered commands, and final stop.

    Callers receive futures, including on connect/disconnect. Stop invalidates
    queued output commands; an interrupted exchange never causes a retry/start.
    Serial exchanges have bounded reads/writes and never run on the GUI thread.
    """
    def __init__(self, serial_factory=None, clock=time.monotonic):
        self.serial_factory = serial_factory
        self.clock = clock
        self.status = None
        self.error = ''
        self.connected = False
        self.port = ''
        self.ready = Future()
        self.closed = Future()
        self._queue = queue.PriorityQueue()
        self._ids = itertools.count(1)
        self._order = itertools.count()
        self._guard = threading.Lock()
        self._generation = 0
        self._closing = threading.Event()
        self._thread = None
        self._board = None
        self._rx = bytearray()
        self.clock_model = ClockSyncModel()
        self._pulse_events = deque(maxlen=10000)
        self._pulse_lock = threading.Lock()
        self._pending_syncs = {}
        self._sync_nonce_seq = itertools.count(1)

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def connect(self, port):
        if self._thread is not None:
            raise RuntimeError('Create a fresh connection after disconnecting.')
        if not port:
            raise ValueError('Choose a stimulator serial port.')
        self.port = port
        self._thread = threading.Thread(target=self._run, name='TEMPO stimulator', daemon=True)
        self._thread.start()
        return self.ready

    def submit(self, command):
        future = Future()
        if not self.connected or self._closing.is_set():
            future.set_exception(ConnectionError('The stimulator is not connected.'))
            return future
        with self._guard:
            if command in ('stop', 'isolate'):
                self._generation += 1
            self._queue.put((0 if command in ('stop', 'isolate') else 1, next(self._order),
                             self._generation, command, future))
        return future

    def close(self):
        self._closing.set()
        if self._thread is None and not self.closed.done():
            self.closed.set_result(None)
        return self.closed

    def _exchange(self, command, timeout=.8):
        request_id = next(self._ids)
        payload = f'{request_id} {command}\n'.encode('ascii')
        if self._board.write(payload) != len(payload):
            raise ConnectionError('The stimulation command was only partially sent; output is unconfirmed.')
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            if command not in ('stop','isolate','hello'):
                with self._queue.mutex:
                    urgent = bool(self._queue.queue and self._queue.queue[0][0] == 0)
                if urgent:
                    _, _, _, stop_command, stop_future = self._queue.get_nowait()
                    try:
                        stopped = self._exchange(stop_command)
                    except Exception as exc:
                        stop_future.set_exception(exc)
                        raise
                    stop_future.set_result(stopped)
                    if command in ('ping','status'):
                        return stopped
                    raise DeviceRejected('Command interrupted by Stop Stimulation.')
            chunk = self._board.read(512)
            self._rx.extend(chunk)
            if len(self._rx) > 8192:
                raise ConnectionError('Invalid or incompatible stimulator response. Install TEMPO’s V2 firmware.')
            while b'\n' in self._rx:
                raw, _, remainder = self._rx.partition(b'\n')
                self._rx[:] = remainder
                try:
                    reply = json.loads(raw)
                except (ValueError, UnicodeError):
                    continue  # Legacy firmware text cannot acknowledge a command.
                if not isinstance(reply, dict):
                    continue
                if reply.get('evt'):
                    self._handle_event(reply)
                    continue
                if reply.get('id') != request_id:
                    continue
                status = PacerStatus.parse(reply)
                self.status = status
                if reply.get('result') == 'confirmation_required':
                    raise ConfirmationRequired('These values exceed the V2 device confirmation thresholds.')
                if reply.get('result') != 'ok':
                    raise DeviceRejected(reply.get('error') or 'The stimulator rejected this command.')
                if command in ('stop', 'hello') and status.mode != 'REST_DISCHARGE':
                    raise ConnectionError('The stimulator did not confirm resting discharge.')
                if command == 'pace' and (status.mode != 'PACING' or status.cycling):
                    raise ConnectionError('The stimulator did not confirm single-channel pacing.')
                if command.startswith('set '):
                    values = command.split()
                    expected = StimulusParameters(float(values[1]), float(values[2]), float(values[3]), int(values[4]))
                    actual = status.parameters
                    if status.mode != 'REST_DISCHARGE' or actual.channel != expected.channel or any(
                        abs(a-b) > .00051 for a,b in zip(
                            (actual.amplitude_v, actual.frequency_hz, actual.pulse_width_ms),
                            (expected.amplitude_v, expected.frequency_hz, expected.pulse_width_ms))):
                        raise ConnectionError('Applied stimulation settings do not match the request.')
                return status
        raise TimeoutError('No matching stimulator acknowledgment. Output is unconfirmed; install TEMPO’s V2 firmware if needed.')

    def _send_sync(self):
        if self._board is None:
            return
        nonce = next(self._sync_nonce_seq)
        t_send = int(round(self.clock() * 1e9))
        self._pending_syncs[nonce] = t_send
        payload = f'sync {nonce}\n'.encode('ascii')
        try:
            self._board.write(payload)
        except Exception:
            pass

    def _handle_event(self, reply):
        try:
            evt = reply.get('evt')
            if evt == 'sync_ack':
                nonce = reply.get('nonce')
                t_device_us = reply.get('t_us')
                t_recv = int(round(self.clock() * 1e9))
                if nonce in self._pending_syncs and t_device_us is not None:
                    device_t_us_int = int(t_device_us)
                    t_send = self._pending_syncs.pop(nonce)
                    self.clock_model.record_sample(t_send, device_t_us_int, t_recv)
            elif evt == 'edge':
                seq = reply.get('seq')
                edge = reply.get('edge')
                t_device_us = reply.get('t_us')
                if seq is not None and edge is not None and t_device_us is not None:
                    seq_int = int(seq)
                    edge_int = int(edge)
                    device_t_us_int = int(t_device_us)
                    mapped_ns, uncert = self.clock_model.map_device_time(device_t_us_int)
                    self.clock_model.total_pulses += 1
                    event = {
                        'seq': seq_int,
                        'edge': edge_int,
                        'device_t_us': device_t_us_int,
                        'mapped_host_ns': mapped_ns,
                        'uncertainty_ms': uncert,
                    }
                    with self._pulse_lock:
                        self._pulse_events.append(event)
        except (ValueError, TypeError, OverflowError):
            return

    def _drain_rx(self):
        if self._board is None:
            return
        try:
            chunk = self._board.read(512)
            if chunk:
                self._rx.extend(chunk)
        except Exception:
            return
        if len(self._rx) > 8192:
            del self._rx[:-4096]
        while b'\n' in self._rx:
            raw, _, remainder = self._rx.partition(b'\n')
            self._rx[:] = remainder
            try:
                reply = json.loads(raw)
            except (ValueError, UnicodeError):
                continue
            if isinstance(reply, dict) and reply.get('evt'):
                self._handle_event(reply)

    def pop_recent_pulses(self):
        with self._pulse_lock:
            pulses = list(self._pulse_events)
            self._pulse_events.clear()
            return pulses

    def get_pulses_in_window(self, start_host_ns, end_host_ns):
        with self._pulse_lock:
            return [p for p in self._pulse_events if start_host_ns <= p['mapped_host_ns'] <= end_host_ns]

    def get_timing_alignment(self):
        if not getattr(self.status, 'supports_timing', False):
            return 'unsupported_firmware'
        summary = self.clock_model.get_alignment_summary()
        return summary

    @property
    def timing_alignment(self):
        return self.get_timing_alignment()

    @property
    def supports_timing(self):
        return getattr(self.status, 'supports_timing', False)

    def _run(self):
        failure = None
        try:
            factory = self.serial_factory
            if factory is None:
                import serial
                factory = serial.Serial
            self._board = factory(self.port, baudrate=115200, timeout=.02, write_timeout=.2)
            self._board.dtr = True
            self._board.rts = True
            if self._closing.wait(1.5):
                raise ConnectionError('Connection cancelled before startup finished.')
            self._exchange('hello')  # Hello also stops output; validates identity before enabling controls.
            self.connected = True
            self.ready.set_result(self.status)
            next_heartbeat = self.clock()
            next_sync = self.clock()
            while not self._closing.is_set():
                now = self.clock()
                # Heartbeats outrank ordinary queued work, preventing starvation.
                if now >= next_heartbeat:
                    next_heartbeat = now + HEARTBEAT_SECONDS
                    self._exchange('ping')
                if getattr(self.status, 'supports_timing', False) and now >= next_sync:
                    next_sync = now + 1.0
                    self._send_sync()
                try:
                    _, _, generation, command, future = self._queue.get(timeout=.02)
                except queue.Empty:
                    self._drain_rx()
                    continue
                if generation != self._generation and command not in ('stop', 'isolate'):
                    future.set_exception(DeviceRejected('Command cancelled by Stop Stimulation.'))
                    continue
                try:
                    value = self._exchange(command)
                except DeviceRejected as exc:
                    future.set_exception(exc)
                except Exception as exc:
                    future.set_exception(exc)
                    raise
                else:
                    future.set_result(value)
        except Exception as exc:
            failure = exc
            self.error = str(exc)
            self.status = None
            if not self.ready.done():
                self.ready.set_exception(exc)
        finally:
            self.connected = False
            if self._board is not None:
                try:
                    # Attempt even after link failure; never report success without an acknowledgment.
                    self._exchange('stop')
                except Exception as exc:
                    failure = failure or exc
                    self.status = None
                finally:
                    try:
                        self._board.close()
                    except Exception as exc:
                        failure = failure or exc
            while not self._queue.empty():
                *_, future = self._queue.get_nowait()
                future.set_exception(ConnectionError('Stimulator connection closed.'))
            if failure is None:
                self.closed.set_result(self.status)
            else:
                self.closed.set_exception(failure)


def serial_ports():
    from serial.tools.list_ports import comports
    ports = list(comports())
    ports.sort(key=lambda port: ('uno r4 minima' not in (port.description or '').lower(),port.device))
    return [(port.device, f'{port.device} · {port.description}') for port in ports]
