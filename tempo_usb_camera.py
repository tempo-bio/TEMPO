"""Windows DirectShow discovery and stream negotiation for USB video inputs."""

from contextlib import ExitStack, contextmanager
import ctypes as c
from dataclasses import dataclass
from math import isfinite
from struct import unpack_from
from uuid import UUID

import cv2


# This is the camera-index limit of the OpenCV DirectShow backend.
DIRECTSHOW_CAMERA_LIMIT = 20

_GUID = c.c_ubyte * 16
_POINTER = c.c_void_p
_OUT_POINTER = c.POINTER(_POINTER)
_HRESULT = c.c_int32
_VIDEO_TYPE = UUID("73646976-0000-0010-8000-00aa00389b71").bytes_le
_VIDEO_HEADER_OFFSET = {
    UUID("05589f80-c356-11ce-bf01-00aa0055595a").bytes_le: 48,  # VIDEOINFOHEADER
    UUID("f72a76a0-eb0a-11d0-ace4-0000c0cc16ba").bytes_le: 72,  # VIDEOINFOHEADER2
}
_FOURCC_SUFFIX = UUID("00000000-0000-0010-8000-00aa00389b71").bytes_le[4:]

# Subtypes with explicit support in OpenCV's DirectShow backend. Bayer data
# needs camera-specific color interpretation, so it is left to the default graph.
_FOURCC_NAMES = ("Y16 ", "Y800", "Y8  ", "GREY", "MJPG", "YUY2", "UYVY",
                 "NV12", "I420", "YUYV", "YVYU", "IYUV", "YV12", "AYUV",
                 "Y41P", "Y411", "Y211", "YVU9")
_FORMAT_LABELS = {name.encode("ascii") + _FOURCC_SUFFIX: name.strip() for name in _FOURCC_NAMES}
for _rgb_subtype, _label in ((0x7D, "RGB24"), (0x7E, "RGB32"), (0x7C, "RGB555"), (0x7B, "RGB565")):
    _FORMAT_LABELS[UUID(f"e436eb{_rgb_subtype:02x}-524f-11ce-9f53-0020af0ba770").bytes_le] = _label


@dataclass(frozen=True)
class DirectShowDevice:
    name: str
    path: str = ""


@dataclass(frozen=True)
class DirectShowFormat:
    size: tuple[int, int]
    fourcc: int
    label: str


class _Variant(c.Structure):
    # VARIANT's union can hold a pair of pointers, even when we only read BSTRs.
    _fields_ = [("kind", c.c_uint16), ("reserved", c.c_uint16 * 3),
                ("bstr", _POINTER), ("record_info", _POINTER)]


class _MediaType(c.Structure):
    _fields_ = [("major", _GUID), ("subtype", _GUID), ("fixed_size", c.c_int32),
                ("temporal_compression", c.c_int32), ("sample_size", c.c_uint32),
                ("format_type", _GUID), ("unknown", _POINTER), ("format_size", c.c_uint32),
                ("format_data", _POINTER)]


def _guid(value):
    return _GUID.from_buffer_copy(UUID(value).bytes_le)


def _method(interface, slot, *parameters, result=_HRESULT):
    vtable = c.cast(interface, c.POINTER(c.POINTER(_POINTER)))[0]
    return c.WINFUNCTYPE(result, _POINTER, *parameters)(vtable[slot])


def _check(status, action):
    if status < 0:
        raise OSError(f"{action} failed (0x{status & 0xFFFFFFFF:08X}).")
    return status


def _release(interface):
    _method(interface, 2, result=c.c_uint32)(interface)  # IUnknown::Release


@contextmanager
def _video_inputs():
    """Keep the ordered DirectShow monikers and COM apartment alive for the caller."""
    ole32 = c.WinDLL("ole32")
    ole32.CoInitializeEx.argtypes = [_POINTER, c.c_uint32]
    ole32.CoInitializeEx.restype = _HRESULT
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    ole32.CoCreateInstance.argtypes = [c.POINTER(_GUID), _POINTER, c.c_uint32,
                                      c.POINTER(_GUID), _OUT_POINTER]
    ole32.CoCreateInstance.restype = _HRESULT
    ole32.CoTaskMemFree.argtypes = [_POINTER]
    ole32.CoTaskMemFree.restype = None

    device_class = _guid("62be5d10-60eb-11d0-bd3b-00a0c911ce86")
    device_enum_iid = _guid("29840822-5b84-11d0-bd3b-00a0c911ce86")
    video_category = _guid("860bb310-5d01-11d0-bd3b-00a0c911ce86")

    with ExitStack() as resources:
        status = ole32.CoInitializeEx(None, 2)
        # A caller can already own an apartment of another type.
        if (status & 0xFFFFFFFF) != 0x80010106:
            _check(status, "Initializing Windows camera discovery")
            resources.callback(ole32.CoUninitialize)

        device_enum = _POINTER()
        _check(ole32.CoCreateInstance(c.byref(device_class), None, 1, c.byref(device_enum_iid),
                                      c.byref(device_enum)), "Creating the Windows device enumerator")
        resources.callback(_release, device_enum)

        moniker_enum = _POINTER()
        create_category = _method(device_enum, 3, c.POINTER(_GUID), _OUT_POINTER, c.c_uint32)
        status = _check(create_category(device_enum, c.byref(video_category), c.byref(moniker_enum), 0),
                        "Finding DirectShow video inputs")
        monikers = []
        if status == 0:
            resources.callback(_release, moniker_enum)
            next_moniker = _method(moniker_enum, 3, c.c_uint32, _OUT_POINTER, _POINTER)
            for index in range(DIRECTSHOW_CAMERA_LIMIT + 1):
                moniker = _POINTER()
                if _check(next_moniker(moniker_enum, 1, c.byref(moniker), None),
                          "Reading a DirectShow video input") == 1:
                    break  # S_FALSE means the enumeration is complete.
                resources.callback(_release, moniker)
                if index == DIRECTSHOW_CAMERA_LIMIT:
                    raise ValueError("Disconnect extra video inputs. DirectShow supports up to 20 in this build.")
                monikers.append(moniker)
        yield ole32, monikers


def _read_input_identity(moniker):
    bag = _POINTER()
    iid = _guid("55272a00-42cb-11ce-8135-00aa004bb851")
    # IMoniker::BindToStorage returns a property bag without opening the stream.
    bind = _method(moniker, 9, _POINTER, _POINTER, c.POINTER(_GUID), _OUT_POINTER)
    if bind(moniker, None, None, c.byref(iid), c.byref(bag)) < 0:
        return None

    try:
        variant_clear = c.WinDLL("oleaut32").VariantClear
        variant_clear.argtypes = [c.POINTER(_Variant)]
        variant_clear.restype = _HRESULT
        read = _method(bag, 3, c.c_wchar_p, c.POINTER(_Variant), _POINTER)

        def text(key):
            value = _Variant()
            try:
                if read(bag, key, c.byref(value), None) >= 0 and value.kind == 8 and value.bstr:
                    return c.wstring_at(value.bstr).strip()
                return ""
            finally:
                variant_clear(c.byref(value))

        return DirectShowDevice(text("FriendlyName"), text("DevicePath"))
    finally:
        _release(bag)


def directshow_devices():
    """List Windows video inputs in the moniker order OpenCV uses to open them."""
    with _video_inputs() as (_, monikers):
        devices = {}
        for index, moniker in enumerate(monikers):
            device = _read_input_identity(moniker)
            if device is not None:
                devices[index] = device
        return devices


def _decode_format(subtype, format_type, data):
    offset = _VIDEO_HEADER_OFFSET.get(format_type)
    if offset is None or subtype not in _FORMAT_LABELS or len(data) < offset + 40:
        return None
    header_size, width, height = unpack_from("<Iii", data, offset)
    if header_size < 40 or not 0 < width <= 32768 or not 0 < abs(height) <= 32768:
        return None
    return DirectShowFormat((width, abs(height)), int.from_bytes(subtype[:4], "little", signed=True),
                            _FORMAT_LABELS[subtype])


def _pin_formats(pin, ole32):
    """Read IAMStreamConfig modes from one output pin."""
    direction = c.c_int32()
    query_direction = _method(pin, 9, c.POINTER(c.c_int32))  # IPin::QueryDirection
    if query_direction(pin, c.byref(direction)) < 0 or direction.value != 1:
        return

    config_iid = _guid("c6e13340-30ac-11d0-a18c-00a0c9118956")
    config = _POINTER()
    query = _method(pin, 0, c.POINTER(_GUID), _OUT_POINTER)
    if query(pin, c.byref(config_iid), c.byref(config)) < 0:
        return

    try:
        get_count = _method(config, 5, c.POINTER(c.c_int32), c.POINTER(c.c_int32))
        count, size = c.c_int32(), c.c_int32()
        if get_count(config, c.byref(count), c.byref(size)) < 0 or not 0 < size.value <= 4096:
            return
        capabilities = c.create_string_buffer(size.value)
        get_caps = _method(config, 6, c.c_int32, c.POINTER(c.POINTER(_MediaType)), _POINTER)
        for index in range(min(count.value, 256)):
            media = c.POINTER(_MediaType)()
            status = get_caps(config, index, c.byref(media), capabilities)
            if not media:
                continue
            try:
                media_type = media.contents
                if status == 0 and bytes(media_type.major) == _VIDEO_TYPE and media_type.format_data:
                    data = c.string_at(media_type.format_data, min(media_type.format_size, 112))
                    mode = _decode_format(bytes(media_type.subtype), bytes(media_type.format_type), data)
                    if mode is not None:
                        yield mode
            finally:
                if media_type.unknown:
                    _release(media_type.unknown)
                ole32.CoTaskMemFree(media_type.format_data)
                ole32.CoTaskMemFree(c.cast(media, _POINTER))
    finally:
        _release(config)


def directshow_formats(index, expected_device=None):
    """Read advertised formats from the selected input and release its filter."""
    with _video_inputs() as (ole32, monikers), ExitStack() as resources:
        if index >= len(monikers):
            raise RuntimeError("The camera list changed. Run Autodetect again and select your camera.")
        moniker = monikers[index]
        if expected_device is not None and _read_input_identity(moniker) != expected_device:
            raise RuntimeError("The camera list changed. Run Autodetect again and select your camera.")

        source = _POINTER()
        filter_iid = _guid("56a86895-0ad4-11ce-b03a-0020af0ba770")
        bind = _method(moniker, 8, _POINTER, _POINTER, c.POINTER(_GUID), _OUT_POINTER)
        _check(bind(moniker, None, None, c.byref(filter_iid), c.byref(source)),
               "Opening the DirectShow capture filter")
        resources.callback(_release, source)

        pin_enum = _POINTER()
        enum_pins = _method(source, 10, _OUT_POINTER)  # IBaseFilter::EnumPins
        _check(enum_pins(source, c.byref(pin_enum)), "Listing DirectShow capture pins")
        resources.callback(_release, pin_enum)
        next_pin = _method(pin_enum, 3, c.c_uint32, _OUT_POINTER, _POINTER)

        modes = {}
        for _ in range(16):
            pin = _POINTER()
            if _check(next_pin(pin_enum, 1, c.byref(pin), None), "Reading a DirectShow capture pin") == 1:
                break
            try:
                modes.update(dict.fromkeys(_pin_formats(pin, ole32)))
            finally:
                _release(pin)
        return list(modes)


def reported_camera_fps(capture):
    """Return a usable stream rate when the backend reports one."""
    try:
        fps = capture.get(cv2.CAP_PROP_FPS)
    except cv2.error:
        return None
    return fps if isfinite(fps) and fps > 0 else None


def _attempt_modes(index, resolution, expected_device, outcomes):
    if resolution:
        yield resolution, None, ""
        yield resolution, cv2.VideoWriter_fourcc(*"MJPG"), "MJPG"
    else:
        yield None, None, ""

    try:
        advertised = directshow_formats(index, expected_device)
    except OSError as error:
        advertised = []
        outcomes.append(f"Could not read the driver's advertised formats ({error}).")

    width, height = resolution or (1280, 720)
    advertised.sort(key=lambda mode: (
        abs(mode.size[0] - width) / width + abs(mode.size[1] - height) / height,
        mode.label not in {"Y16", "Y800", "Y8", "GREY", "MJPG"},
    ))
    # Try a bounded set of driver modes before the common compatibility modes.
    for mode in advertised[:8]:
        yield mode.size, mode.fourcc, mode.label
    yield None, None, ""
    yield (1280, 720), cv2.VideoWriter_fourcc(*"MJPG"), "MJPG"
    yield (640, 480), cv2.VideoWriter_fourcc(*"YUY2"), "YUY2"


def open_directshow_camera(index, resolution=None, expected_device=None):
    """Open one selected device and require a decoded frame before accepting it."""
    if not 0 <= index < DIRECTSHOW_CAMERA_LIMIT:
        raise ValueError("Choose a camera number from 0 to 19.")
    try:
        devices = directshow_devices()
    except OSError:
        if expected_device is not None:
            raise
    else:
        if expected_device is not None and devices.get(index) != expected_device:
            raise RuntimeError("The camera list changed. Run Autodetect again and select your camera.")
        if index not in devices:
            raise RuntimeError(f"Windows does not list a DirectShow video input at camera number {index}.")
        expected_device = devices[index]

    outcomes, tried = [], set()
    y16 = cv2.VideoWriter_fourcc(*"Y16 ")
    for size, fourcc, label in _attempt_modes(index, resolution, expected_device, outcomes):
        attempt = size, fourcc
        if attempt in tried:
            continue
        tried.add(attempt)
        mode = f"{size[0]} × {size[1]}" if size else "camera default"
        if label:
            mode += f" {label}"
        params = []
        if size:
            params.extend((cv2.CAP_PROP_FRAME_WIDTH, size[0], cv2.CAP_PROP_FRAME_HEIGHT, size[1]))
        if fourcc is not None:
            params.extend((cv2.CAP_PROP_FOURCC, fourcc))
        cap = cv2.VideoCapture()
        connected = False
        try:
            if not cap.open(index, cv2.CAP_DSHOW, params):
                outcomes.append(f"{mode} did not open.")
                continue
            try:
                negotiated_fourcc = cap.get(cv2.CAP_PROP_FOURCC)
            except cv2.error:
                negotiated_fourcc = None
            monochrome16 = y16 in (fourcc, negotiated_fourcc)
            if monochrome16 and not cap.set(cv2.CAP_PROP_CONVERT_RGB, 0):
                outcomes.append(f"{mode} could not preserve 16-bit monochrome pixels.")
                continue
            ok, frame = cap.read()
            if not ok or frame is None or not frame.size:
                outcomes.append(f"{mode} opened without returning a video frame.")
                continue
            height, width = frame.shape[:2]
            if resolution and size == resolution and (width, height) != resolution:
                outcomes.append(f"{mode} returned {width} × {height}.")
                continue
            if monochrome16 and (frame.ndim != 2 or frame.dtype.name != "uint16"):
                outcomes.append(f"{mode} did not return a 16-bit monochrome frame.")
                continue
            connected = True
            return cap, width, height
        except (cv2.error, OSError, RuntimeError) as error:
            outcomes.append(f"{mode} failed ({error}).")
        finally:
            if not connected:
                cap.release()
    raise RuntimeError(f"Camera {index} did not deliver video.\n" + "\n".join(outcomes))
