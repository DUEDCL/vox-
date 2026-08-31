"""Windows 采集端点的输入音量：读得到、设得动、静音看得见。纯 ctypes，零新依赖。

**这个文件的存在理由是使用者说过三次的同一句话**：「真正的最佳效果应该是无论何种设备、
音量，都能准确的识别唤醒词」。在它之前，那件事是人的责任 —— 而实测那个可用窗口很窄：
系统输入音量约 100 时每句唤醒词都削波（峰值 1.000、声纹相似度 0.34–0.48 进不去），调到 7
才开始命中但又偏轻，而窗口位置取决于用哪只麦克风、戴不戴耳机、离多远。

`core/audio/gain.py` 的软件增益能救「偏轻」，**救不了削波** —— 削波发生在声卡的 ADC 里，
到这里已经是一排平顶。所以那一端只能在 OS 那一侧解决，而 OS 那一侧此前是一个我们只会
「建议用户去改」的东西。2026-09-01 实测（同一台机器，`.vox-ref/probe_level.py`）：

    耳机 (沉麟的耳机)             level = 0.01   ← 「这只麦克风坏了」的真身
    麦克风阵列 (Realtek(R) Audio)  level = 0.82   ← 「说话就削波」的真身

两个症状是同一个可读的数字。读不到它的时候我们只能猜设备坏了。

**为什么是 ctypes 而不是 pycaw。** 这个能力（读写 Core Audio 端点音量）是通用能力，先例
是 pycaw（MIT、纯本机、不夺架构所有权，三条筛选都过）。不引它的唯一理由是代价比较：
这里要用的是 4 个接口的 6 个方法，而 pycaw 会带上 comtypes —— 一个会在导入期生成代码的
运行时。对一个「新增依赖若含云调用或 telemetry 直接否决」的项目来说，能用 200 行标准库
写完的东西不值得再多两个包。如果以后要碰会话音量、设备通知、或者 eRender 那一侧，
这个判断应当重新做一次，那时 pycaw 是正确答案。

**失败一律降级成「读不到」，永不抛进音频路径。** 没有输入音量控制的设备是存在的
（`Activate` 返回 E_NOINTERFACE 的虚拟设备、远程会话里的端点），而那时唤醒仍然应该工作。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

_S_OK = 0
_S_FALSE = 1
#: ``RPC_E_CHANGED_MODE``：线程上已经有 COM，只是模式不同。**这不是错误**，而且它一定会
#: 发生 —— `sounddevice`/PortAudio 在这个进程里先把线程初始化成了 STA（2026-09-01 实测
#: 0x80010106）。当成 `S_FALSE` 处理：照常用，出去时不反初始化。
_RPC_E_CHANGED_MODE = -2147417850  # 0x80010106
_CLSCTX_ALL = 23
_COINIT_MULTITHREADED = 0
#: EDataFlow: eRender = 0, eCapture = 1。这个文件只碰 eCapture —— 改输出音量不是它的事。
_E_CAPTURE = 1
#: ERole: eConsole = 0（「默认设备」在设置面板里指的就是这一个）
_ROLE_CONSOLE = 0
_DEVICE_STATE_ACTIVE = 1
_STGM_READ = 0
#: PKEY_Device_FriendlyName。友好名和 `sounddevice` 报的 MME/DirectSound/WASAPI 名**逐字
#: 相同**（2026-09-01 实测三个端点都对得上），所以设备匹配用精确相等就够，不需要模糊匹配。
_PKEY_FRIENDLY = ("{A45C254E-DF1C-4EFD-8020-67D146A850E0}", 14)


class LevelUnavailable(RuntimeError):
    """读不到 / 设不动。带上原因，因为「这台机器不支持」和「名字对不上」要分开看。"""


@dataclass(frozen=True)
class Endpoint:
    """一个采集端点当下的样子。``level`` 是 0.0–1.0，和设置面板里那根滑条同一个量。"""

    name: str
    level: float
    muted: bool
    default: bool = False

    def describe(self) -> dict[str, object]:
        return {
            "name": self.name,
            "level": round(self.level, 4),
            "muted": self.muted,
            "default": self.default,
        }


# -- COM 底座 ----------------------------------------------------------------
#
# 只有 4 个接口的 6 个方法，所以按 vtable 序号直接调，不生成接口类。序号写在调用点旁边
# （出处是 Windows SDK 的 mmdeviceapi.h / endpointvolume.h / propsys.h），因为一个错的
# 序号会调到相邻的方法上 —— 那种错不报错，只给一个荒谬的数字。


def _ctypes():
    """晚绑定：非 Windows 上这个模块必须能被导入（测试要跑）。"""
    if sys.platform != "win32":
        raise LevelUnavailable("输入音量控制只在 Windows 上有（其余平台交给系统混音器）")
    import ctypes

    return ctypes


def _guid_type(ctypes):
    """GUID 结构体，**全模块只此一份**。

    每次调用都新定义一个 `class GUID` 的话，两次 `_guid()` 得到的是两个互不兼容的
    ctypes 类型 —— 把其中一个赋给另一个的字段时 ctypes 会拒绝，而那个错误看起来像
    「COM 调用失败」。
    """
    cached = _GUID_CACHE.get("type")
    if cached is None:
        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_uint32),
                ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        cached = _GUID_CACHE["type"] = GUID
    return cached


_GUID_CACHE: dict[str, object] = {}


def _guid(ctypes, text: str):
    guid = _guid_type(ctypes)()
    if ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(guid)) != _S_OK:
        raise LevelUnavailable(f"bad GUID {text}")
    return guid


def _method(ctypes, obj, index: int, restype, *argtypes):
    """取 ``obj`` 的 vtable 第 ``index`` 项，按给定签名包成可调用。"""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])


def _check(ctypes, name: str, code: int) -> None:
    if code != _S_OK:
        raise LevelUnavailable(f"{name} 失败：0x{code & 0xFFFFFFFF:08X}")


class _Session:
    """一次 COM 会话：进来 CoInitializeEx，出去把拿过的接口全 Release 掉。

    **Release 不是可选的。** 这些方法会被控制台每次刷新状态时调到，漏一个引用就是一个
    每几秒泄一次的句柄。``CoUninitialize`` 只在我们自己初始化成功时调 —— 线程上已经有
    COM（``S_FALSE``）时反初始化会把别人的那一份也拆了。
    """

    def __init__(self) -> None:
        self.ctypes = _ctypes()
        self._owned: list[object] = []
        self._uninit = False

    def __enter__(self) -> _Session:
        code = self.ctypes.windll.ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
        if code == _S_OK:
            self._uninit = True
        elif code not in (_S_FALSE, _RPC_E_CHANGED_MODE):
            raise LevelUnavailable(f"CoInitializeEx 失败：0x{code & 0xFFFFFFFF:08X}")
        return self

    def __exit__(self, *_exc) -> None:
        ctypes = self.ctypes
        while self._owned:
            obj = self._owned.pop()
            try:
                _method(ctypes, obj, 2, ctypes.c_ulong)(obj)  # IUnknown::Release
            except Exception:  # noqa: BLE001 - 拆的时候出事不该盖住真正的错误
                pass
        if self._uninit:
            ctypes.windll.ole32.CoUninitialize()

    def keep(self, obj):
        self._owned.append(obj)
        return obj

    def enumerator(self):
        ctypes = self.ctypes
        out = ctypes.c_void_p()
        _check(
            ctypes,
            "CoCreateInstance(MMDeviceEnumerator)",
            ctypes.windll.ole32.CoCreateInstance(
                ctypes.byref(_guid(ctypes, "{BCDE0395-E52F-467C-8E3D-C4579291692E}")),
                None,
                _CLSCTX_ALL,
                ctypes.byref(_guid(ctypes, "{A95664D2-9614-4F35-A746-DE8DB63617E6}")),
                ctypes.byref(out),
            ),
        )
        return self.keep(out)

    def default_name(self, enumerator) -> str:
        """默认采集端点的名字。只用来给列表里的那一行打个 ``default`` 标记。"""
        ctypes = self.ctypes
        out = ctypes.c_void_p()
        # IMMDeviceEnumerator::GetDefaultAudioEndpoint = vtable[4]
        code = _method(
            ctypes, enumerator, 4, ctypes.HRESULT,
            ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p),
        )(enumerator, _E_CAPTURE, _ROLE_CONSOLE, ctypes.byref(out))
        if code != _S_OK:
            return ""
        self.keep(out)
        return self.name_of(out)

    def devices(self, enumerator) -> list:
        ctypes = self.ctypes
        collection = ctypes.c_void_p()
        # IMMDeviceEnumerator::EnumAudioEndpoints = vtable[3]
        _check(
            ctypes, "EnumAudioEndpoints",
            _method(
                ctypes, enumerator, 3, ctypes.HRESULT,
                ctypes.c_int, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p),
            )(enumerator, _E_CAPTURE, _DEVICE_STATE_ACTIVE, ctypes.byref(collection)),
        )
        self.keep(collection)
        count = ctypes.c_uint()
        # IMMDeviceCollection::GetCount = vtable[3], ::Item = vtable[4]
        _check(
            ctypes, "GetCount",
            _method(ctypes, collection, 3, ctypes.HRESULT, ctypes.POINTER(ctypes.c_uint))(
                collection, ctypes.byref(count)
            ),
        )
        item = _method(
            ctypes, collection, 4, ctypes.HRESULT, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)
        )
        found = []
        for index in range(count.value):
            one = ctypes.c_void_p()
            if item(collection, index, ctypes.byref(one)) == _S_OK:
                found.append(self.keep(one))
        return found

    def name_of(self, device) -> str:
        """友好名。取不到就返回空串 —— 一个没有名字的端点匹配不上任何东西，不是故障。"""
        ctypes = self.ctypes

        class PROPERTYKEY(ctypes.Structure):
            _fields_ = [("fmtid", type(_guid(ctypes, _PKEY_FRIENDLY[0]))), ("pid", ctypes.c_ulong)]

        class PROPVARIANT(ctypes.Structure):
            _fields_ = [
                ("vt", ctypes.c_ushort),
                ("r1", ctypes.c_ubyte),
                ("r2", ctypes.c_ubyte),
                ("r3", ctypes.c_ulong),
                ("data", ctypes.c_byte * 16),
            ]

        store = ctypes.c_void_p()
        # IMMDevice::OpenPropertyStore = vtable[4]
        if _method(
            ctypes, device, 4, ctypes.HRESULT, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)
        )(device, _STGM_READ, ctypes.byref(store)) != _S_OK:
            return ""
        self.keep(store)
        key = PROPERTYKEY()
        key.fmtid = _guid(ctypes, _PKEY_FRIENDLY[0])
        key.pid = _PKEY_FRIENDLY[1]
        value = PROPVARIANT()
        # IPropertyStore::GetValue = vtable[5]
        if _method(
            ctypes, store, 5, ctypes.HRESULT,
            ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT),
        )(store, ctypes.byref(key), ctypes.byref(value)) != _S_OK:
            return ""
        # VT_LPWSTR：指针在结构体偏移 8 处（前面是 vt + 3 个保留字段 + 对齐）。
        text = ctypes.cast(
            ctypes.addressof(value) + 8, ctypes.POINTER(ctypes.c_wchar_p)
        ).contents.value
        return text or ""

    def volume(self, device):
        """``IAudioEndpointVolume``，或者 ``None``（有些虚拟设备没有音量控制）。"""
        ctypes = self.ctypes
        out = ctypes.c_void_p()
        # IMMDevice::Activate = vtable[3]
        code = _method(
            ctypes, device, 3, ctypes.HRESULT,
            ctypes.POINTER(_guid_type(ctypes)), ctypes.c_ulong, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )(
            device,
            ctypes.byref(_guid(ctypes, "{5CDF2C82-841E-4546-9722-0CF74078229A}")),
            _CLSCTX_ALL,
            None,
            ctypes.byref(out),
        )
        return self.keep(out) if code == _S_OK else None

    def read(self, volume) -> tuple[float, bool]:
        ctypes = self.ctypes
        scalar = ctypes.c_float()
        # IAudioEndpointVolume::GetMasterVolumeLevelScalar = vtable[9]
        _check(
            ctypes, "GetMasterVolumeLevelScalar",
            _method(ctypes, volume, 9, ctypes.HRESULT, ctypes.POINTER(ctypes.c_float))(
                volume, ctypes.byref(scalar)
            ),
        )
        muted = ctypes.c_int()
        # IAudioEndpointVolume::GetMute = vtable[15]
        code = _method(ctypes, volume, 15, ctypes.HRESULT, ctypes.POINTER(ctypes.c_int))(
            volume, ctypes.byref(muted)
        )
        return float(scalar.value), bool(muted.value) if code == _S_OK else False

    def write(self, volume, value: float) -> None:
        ctypes = self.ctypes
        # IAudioEndpointVolume::SetMasterVolumeLevelScalar = vtable[7]
        _check(
            ctypes, "SetMasterVolumeLevelScalar",
            _method(
                ctypes, volume, 7, ctypes.HRESULT, ctypes.c_float, ctypes.c_void_p
            )(volume, ctypes.c_float(value), None),
        )

    def unmute(self, volume) -> None:
        ctypes = self.ctypes
        # IAudioEndpointVolume::SetMute = vtable[14]。**取消静音和设音量是两件事**：
        # 一只被静音的麦克风把音量调到 100 仍然出零，而那正是「设备看起来是死的」。
        _method(ctypes, volume, 14, ctypes.HRESULT, ctypes.c_int, ctypes.c_void_p)(
            volume, 0, None
        )


# -- 公开的四个动作 -----------------------------------------------------------


def endpoints() -> tuple[Endpoint, ...]:
    """全部**活动**采集端点的名字、音量、静音状态。

    只列 ``DEVICE_STATE_ACTIVE``：拔掉的耳机仍然在注册表里，把它列出来只会让「我该选哪
    一个」更难。名字和 `sounddevice` 报的逐字相同，所以调用方可以直接拿设备名来对。
    """
    with _Session() as session:
        enumerator = session.enumerator()
        default = session.default_name(enumerator)
        found = []
        for device in session.devices(enumerator):
            name = session.name_of(device)
            volume = session.volume(device)
            if not name or volume is None:
                continue
            level, muted = session.read(volume)
            found.append(Endpoint(name, level, muted, name == default and bool(default)))
        return tuple(found)


def read_level(name: str) -> Endpoint:
    """按**精确名字**找一个端点。找不到就抛，不猜。

    不做模糊匹配是刻意的：同一台机器上有「麦克风 (Realtek…)」和「麦克风阵列 (Realtek…)」，
    前者是后者的前缀。设错另一只设备的音量属于「改了但没生效」，而那是本项目最不能容忍
    的一类 bug。
    """
    wanted = (name or "").strip()
    if not wanted:
        raise LevelUnavailable("没有给设备名")
    for endpoint in endpoints():
        if endpoint.name == wanted:
            return endpoint
    raise LevelUnavailable(f"没有名叫「{wanted}」的活动采集端点")


def set_level(name: str, value: float, *, unmute: bool = True) -> Endpoint:
    """把某个端点的输入音量设成 ``value``（0.0–1.0），顺带取消静音。返回设完之后重读的值。

    **重读而不是回显参数。** 有些驱动只支持有级的音量（实测个别设备只有 0/50/100 三挡），
    把请求值原样报回去等于报一个没发生的事。
    """
    wanted = (name or "").strip()
    if not wanted:
        raise LevelUnavailable("没有给设备名")
    target = max(0.0, min(1.0, float(value)))
    with _Session() as session:
        enumerator = session.enumerator()
        for device in session.devices(enumerator):
            if session.name_of(device) != wanted:
                continue
            volume = session.volume(device)
            if volume is None:
                raise LevelUnavailable(f"「{wanted}」没有音量控制（虚拟设备常见）")
            if unmute:
                session.unmute(volume)
            session.write(volume, target)
            level, muted = session.read(volume)
            return Endpoint(wanted, level, muted, False)
    raise LevelUnavailable(f"没有名叫「{wanted}」的活动采集端点")


def unavailable_reason() -> str:
    """空串 = 能用。否则是一句能贴到界面上的原因。"""
    try:
        endpoints()
    except LevelUnavailable as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001 - COM 的意外一律降级成「读不到」
        return f"{type(exc).__name__}: {exc}"
    return ""


def device_name(selector: int | str | None) -> str:
    """`sounddevice` 的设备选择子（索引 / 名字片段 / None=默认）→ 设备名。

    **索引会漂。** `config/voice.toml` 里那句「填索引，不要填名字片段」解决的是
    「同一个物理设备在四种 host API 下重复出现」，代价是索引取决于当时插着什么 ——
    2026-09-01 实测：`device = "2"` 在 08-29 是「耳机 (沉麟的耳机)」，现在是
    「麦克风阵列 (Realtek(R) Audio)」。所以每一处报设备的地方都该报**名字**，
    让漂移看得见。
    """
    try:
        import sounddevice as sd  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise LevelUnavailable(f"sounddevice 不可用：{type(exc).__name__}") from exc
    try:
        info = sd.query_devices(selector, "input") if selector is not None else sd.query_devices(
            kind="input"
        )
    except Exception as exc:  # noqa: BLE001 - 选错设备的报错要原样带出去
        raise LevelUnavailable(f"设备 {selector!r} 打不开：{exc}") from exc
    return str(info.get("name", "") or "")


__all__ = [
    "Endpoint",
    "LevelUnavailable",
    "device_name",
    "endpoints",
    "read_level",
    "set_level",
    "unavailable_reason",
]
