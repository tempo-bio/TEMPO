"""Guided, version-pinned Arduino setup. Upload is always an explicit user action."""
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import signal
import ssl
import subprocess
import tarfile
import tempfile
import threading
import time
from urllib.request import urlopen
import zipfile

CLI_VERSION = '1.5.1'
CORE_VERSION = '1.6.0'
FQBN = 'arduino:renesas_uno:minima'
CLI_DOWNLOADS = {
    ('Darwin','arm64'): ('macOS_ARM64.tar.gz','cb952e8c1621c95ef5f1d17831c945e3d0ec5973f89c557a7ec8feb9c4f7d4c9'),
    ('Darwin','x86_64'): ('macOS_64bit.tar.gz','c982e940027996bea9901050e95fae99c59c1dcfee54beedecaf28141e7bf2e7'),
    ('Windows','x86_64'): ('Windows_64bit.zip','fabe42e0eb04d00e776a66178299ff95a46c623dbc260f997e58fd514853dd40'),
    ('Linux','x86_64'): ('Linux_64bit.tar.gz','28a8e119c498a25607821c36cb2dc49e8463941b261a0d99091baa7bc692dd2b'),
    ('Linux','arm64'): ('Linux_ARM64.tar.gz','1e69e077479f300614d4551334e0a33f08ee40b04315d83b8e7e0e94f0d0ee62'),
}


class FirmwareInstaller:
    def __init__(self, root, progress=lambda text: None, cancelled=None, platform_key=None):
        self.root = Path(root)
        self.progress = progress
        self.cancelled = cancelled or threading.Event()
        machine = platform.machine().lower()
        machine = {'amd64':'x86_64','aarch64':'arm64'}.get(machine,machine)
        self.platform_key = platform_key or (platform.system(),machine)
        self.executable = self.root / ('arduino-cli-' + CLI_VERSION) / ('arduino-cli.exe' if self.platform_key[0] == 'Windows' else 'arduino-cli')
        self.config = self.root / 'arduino-cli.yaml'
        self.uploading = False
        self._phase_lock = threading.Lock()
        self.serial_number = None

    def cancel_setup(self):
        with self._phase_lock:
            if self.uploading:
                return False
            self.cancelled.set()
            return True

    def _check(self):
        if self.cancelled.is_set():
            raise InterruptedError('Firmware setup cancelled before upload. Existing firmware was retained.')

    def ensure_cli(self):
        self._check()
        if self.platform_key not in CLI_DOWNLOADS:
            raise RuntimeError('Guided firmware setup is unavailable on this platform. Use the manual upload instructions.')
        suffix, expected = CLI_DOWNLOADS[self.platform_key]
        filename = 'arduino-cli_' + CLI_VERSION + '_' + suffix
        executable_digest = self.executable.with_suffix(self.executable.suffix + '.sha256')
        if self.executable.is_file() and executable_digest.is_file():
            actual = hashlib.sha256(self.executable.read_bytes()).hexdigest()
            if actual == executable_digest.read_text().strip():
                return self.executable
        self.progress('Downloading verified Arduino setup tools…')
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
        url = f'https://github.com/arduino/arduino-cli/releases/download/v{CLI_VERSION}/{filename}'
        chunks = []
        with urlopen(url,timeout=30,context=context) as response:
            while True:
                self._check()
                block = response.read(1024*1024)
                if not block: break
                chunks.append(block)
                if sum(map(len,chunks)) > 100*1024*1024:
                    raise RuntimeError('Arduino tools download exceeded its expected size.')
        payload = b''.join(chunks)
        if hashlib.sha256(payload).hexdigest() != expected:
            raise RuntimeError('Arduino tools checksum did not match. Nothing was installed; retry setup.')
        # Extract only the known executable; never extract paths from the archive.
        if suffix.endswith('.zip'):
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                binary = archive.read('arduino-cli.exe')
        else:
            with tarfile.open(fileobj=io.BytesIO(payload),mode='r:gz') as archive:
                member = archive.getmember('arduino-cli')
                if not member.isfile(): raise RuntimeError('Invalid Arduino tools archive.')
                binary = archive.extractfile(member).read()
        self.executable.parent.mkdir(parents=True,exist_ok=True)
        self.executable.write_bytes(binary)
        self.executable.chmod(0o755)
        executable_digest.write_text(hashlib.sha256(binary).hexdigest())
        return self.executable

    def prepare_config(self):
        self.root.mkdir(parents=True,exist_ok=True)
        # Isolate board packages and downloads from any user's Arduino IDE setup.
        values = {key: str((self.root/name).resolve()) for key,name in
                  (('data','data'),('downloads','downloads'),('user','sketchbook'))}
        self.config.write_text('directories:\n' + ''.join(f'  {key}: {json.dumps(value)}\n' for key,value in values.items()))

    def run(self, args, timeout=600, cancellable=True):
        if cancellable: self._check()
        command = [str(self.executable),'--config-file',str(self.config),*args]
        options = {'creationflags':subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session':True}
        with tempfile.TemporaryFile(mode='w+b') as output:
            process = subprocess.Popen(command,stdout=output,stderr=subprocess.STDOUT,**options)
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Arduino setup timed out. Reconnect the board and retry.')
                    if cancellable: self._check()
                    time.sleep(.05)
            except BaseException:
                if os.name == 'nt':
                    subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,timeout=10)
                else:
                    os.killpg(process.pid,signal.SIGTERM)
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    if os.name == 'nt': process.kill()
                    else: os.killpg(process.pid,signal.SIGKILL)
                    process.wait(timeout=5)
                raise
            output.seek(0)
            text = output.read().decode('utf-8',errors='replace')
        if process.returncode:
            raise RuntimeError('Arduino setup failed. Check the USB cable and board; close other serial applications, then retry.\n' + text[-6000:])
        return text

    def prepare(self):
        self.ensure_cli()
        self.prepare_config()
        self.progress('Checking Arduino board support…')
        listing = json.loads(self.run(['core','list','--format','json']))
        platforms = listing.get('platforms',[]) if isinstance(listing,dict) else listing
        installed = any(p.get('id') == 'arduino:renesas_uno' and
                        p.get('installed') == CORE_VERSION for p in platforms)
        if not installed:
            self.progress('Preparing Uno R4 Minima board support…')
            self.run(['core','update-index'])
            self.run(['core','install','arduino:renesas_uno@' + CORE_VERSION])

    def identify(self, port):
        listing = json.loads(self.run(['board','list','--format','json'],timeout=30))
        ports = listing.get('detected_ports',[]) if isinstance(listing,dict) else listing
        for item in ports:
            if item.get('port',{}).get('address') == port:
                if any(board.get('fqbn') == FQBN for board in item.get('matching_boards',[])):
                    return item
                raise RuntimeError('The selected port is not identified as an Uno R4 Minima. Choose the correct board; automatic upload was not attempted.')
        raise RuntimeError('The selected Arduino port is no longer available. Refresh the port list and retry.')

    def install(self, port, sketch, verify=None):
        if not port: raise ValueError('Choose the Uno R4 Minima serial port.')
        self.prepare()
        original_board = self.identify(port)
        original_properties = (original_board or {}).get('port',{}).get('properties',{})
        self.serial_number = original_properties.get('serialNumber')
        self.progress('Compiling TEMPO stimulation firmware…')
        self.run(['compile','--fqbn',FQBN,'--warnings','all',str(sketch)],timeout=300)
        board = self.identify(port)
        new_properties = (board or {}).get('port',{}).get('properties',{})
        if original_properties.get('serialNumber') and original_properties.get('serialNumber') != new_properties.get('serialNumber'):
            raise RuntimeError('The selected Arduino changed during setup. Choose its port and start again.')
        # Never cancel a flash halfway through; the dialog keeps close disabled.
        with self._phase_lock:
            self._check()
            self.uploading = True
        try:
            self.progress('Uploading firmware. Keep the Arduino connected…')
            self.run(['upload','--port',port,'--fqbn',FQBN,str(sketch)],timeout=120,cancellable=False)
            self.progress('Reconnecting and verifying the firmware…')
            if verify is not None:
                port = verify(port) or port
        finally:
            self.uploading = False
        return port


def verify_firmware(port, serial_number=None):
    from tempo_stimulation import PacerService
    last_error = None
    for attempt in range(4):
        service = PacerService()
        try:
            current_port = port
            if serial_number:
                from serial.tools.list_ports import comports
                matching = [item.device for item in comports() if item.serial_number == serial_number]
                if len(matching) != 1:
                    raise ConnectionError('Waiting for the selected Arduino to reconnect.')
                current_port = matching[0]
            service.connect(current_port).result(timeout=5)
            service.close().result(timeout=3)
            return current_port
        except Exception as exc:
            try: service.close().result(timeout=2)
            except Exception: pass
            last_error = exc
            time.sleep(.5)
    raise RuntimeError('Upload finished, but TEMPO could not verify the firmware. Refresh ports and reconnect the stimulator. ' + str(last_error))
