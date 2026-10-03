"""Live hardware telemetry for the overlay compositor.

CPU temperature comes straight from sysfs (k10temp/zenpower on AMD, coretemp on Intel, then generic
cpu_thermal/acpitz hwmon chips and /sys/class/thermal zones); CPU load/frequency and RAM via psutil.
GPU temp/load/power/clock come from the DRM sysfs tree (amdgpu; Intel i915/xe) of the best card --
the discrete one on iGPU + dGPU systems -- or from nvidia-smi on NVIDIA. Every read is best-effort:
a sensor that isn't present on this machine returns None (shown as "N/A") rather than raising.

All sysfs roots are parameters so the whole module can be exercised against a mocked tree.
"""
import glob
import logging
import os
import re
import shutil
import subprocess
import time

import psutil

log = logging.getLogger("lt360d.sensors")

HWMON_ROOT = "/sys/class/hwmon"
THERMAL_ROOT = "/sys/class/thermal"
DRM_ROOT = "/sys/class/drm"


# ---------------------------------------------------------------- small sysfs helpers

def _read_text(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read(256).strip()
    except OSError:
        return None


def _read_int(path: str) -> int | None:
    text = _read_text(path)
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


def _first_int(directory: str, *names: str) -> int | None:
    """Value of the first file in `names` that exists in `directory` and holds an integer."""
    for name in names:
        v = _read_int(os.path.join(directory, name))
        if v is not None:
            return v
    return None


def _natural(path: str) -> list:
    """Sort key so hwmon10 comes after hwmon9 and temp10_input after temp9_input."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", os.path.basename(path))]


def _temp_inputs(hwmon_dir: str) -> list[str]:
    return sorted(glob.glob(os.path.join(hwmon_dir, "temp*_input")), key=_natural)


def _pick_temp_input(hwmon_dir: str, prefer_labels: tuple[str, ...] = ()) -> str | None:
    """The temp*_input whose label matches the first of `prefer_labels` found, else temp1_input, else the
    lowest-numbered one."""
    inputs = _temp_inputs(hwmon_dir)
    if not inputs:
        return None
    labels = {p: (_read_text(p[:-len("_input")] + "_label") or "").lower() for p in inputs}
    for want in prefer_labels:
        for p in inputs:
            if labels[p].startswith(want):
                return p
    first = os.path.join(hwmon_dir, "temp1_input")
    return first if first in labels else inputs[0]


# ---------------------------------------------------------------- CPU temperature

# (hwmon chip name, preferred temp labels), in priority order. Tdie is the real die temperature where a
# chip reports both (Tctl can carry a fan-curve offset on some Ryzens).
_CPU_HWMON_CHIPS = (
    ("k10temp", ("tdie", "tctl")),
    ("zenpower", ("tdie", "tctl")),
    ("coretemp", ("package id",)),
    ("cpu_thermal", ()),          # Raspberry Pi / ARM SoCs
    ("cpu-thermal", ()),
    ("soc_thermal", ()),
)
_CPU_THERMAL_ZONE_TYPES = ("x86_pkg_temp", "cpu-thermal", "cpu_thermal", "soc_thermal", "acpitz")
CPU_TEMP_REDISCOVER = 15.0  # seconds between retries when no sensor was found (e.g. k10temp loaded later)


def _is_amd_cpu() -> bool:
    try:
        with open("/proc/cpuinfo") as f:
            return any(l.startswith("vendor_id") and "AuthenticAMD" in l for l in f)
    except OSError:
        return False


def _acpi_zone_is_cpu() -> bool:
    """acpitz is a board-level ACPI zone. On Intel it tracks the package; on Ryzen boards it is typically a
    fixed ~16-20 C reading that has nothing to do with the CPU, so it must never stand in for Tctl."""
    return not _is_amd_cpu()


def find_cpu_temp_input(hwmon_root: str = HWMON_ROOT, thermal_root: str = THERMAL_ROOT) -> str | None:
    """Path of the file holding the CPU temperature in millidegrees, or None if this machine has none."""
    chips: dict[str, list[str]] = {}
    for h in sorted(glob.glob(os.path.join(hwmon_root, "hwmon*")), key=_natural):
        chips.setdefault(_read_text(os.path.join(h, "name")) or "", []).append(h)
    for chip, labels in _CPU_HWMON_CHIPS:
        for h in chips.get(chip, []):
            p = _pick_temp_input(h, labels)
            if p:
                return p
    zones = {}
    for z in sorted(glob.glob(os.path.join(thermal_root, "thermal_zone*")), key=_natural):
        zones.setdefault(_read_text(os.path.join(z, "type")) or "", z)
    for kind in _CPU_THERMAL_ZONE_TYPES:
        if kind == "acpitz" and not _acpi_zone_is_cpu():
            continue
        if kind in zones and os.path.isfile(os.path.join(zones[kind], "temp")):
            return os.path.join(zones[kind], "temp")
    for h in chips.get("acpitz", []) if _acpi_zone_is_cpu() else []:  # last resort: ACPI zone
        p = _pick_temp_input(h)
        if p:
            return p
    if _is_amd_cpu() and not getattr(find_cpu_temp_input, "_warned", False):
        find_cpu_temp_input._warned = True
        log.warning("No AMD CPU temperature sensor (k10temp/zenpower) is loaded, so CPU temp shows N/A. "
                    "Try `sudo modprobe k10temp`; if it is blacklisted (zenpower3-dkms does that), remove the "
                    "blacklist or the package.")
    return None


class _CpuTemp:
    """Finds the CPU temperature file once, then just reads it; looks again if it disappears."""

    def __init__(self, hwmon_root: str = HWMON_ROOT, thermal_root: str = THERMAL_ROOT):
        self.roots = (hwmon_root, thermal_root)
        self.path = None
        self._next_search = 0.0

    def read(self) -> float | None:
        if self.path is None:
            now = time.monotonic()
            if now < self._next_search:
                return None
            self._next_search = now + CPU_TEMP_REDISCOVER
            self.path = find_cpu_temp_input(*self.roots)
            if self.path:
                log.info("CPU temperature: %s", self.path)
        if self.path is None:
            return None
        v = _read_int(self.path)
        if v is None:
            self.path = None  # module unloaded / hwmon renumbered: rediscover
            return None
        return v / 1000.0


# ---------------------------------------------------------------- GPUs in /sys/class/drm

_DRM_CARD_RE = re.compile(r"^card\d+$")   # card0, card1 -- not connectors like card0-DP-1
_AMD_VENDOR, _INTEL_VENDOR = "0x1002", "0x8086"
_VENDOR_NAMES = {_AMD_VENDOR: "AMD", _INTEL_VENDOR: "Intel"}
_INTEL_IGPU_SLOT = "0000:00:02.0"         # Intel integrated graphics always sits at 00:02.0
DISCRETE_VRAM_BYTES = 2 * 2**30          # more dedicated VRAM than this => discrete AMD card


def gpu_candidates(drm_root: str = DRM_ROOT) -> list[dict]:
    """Every AMD/Intel DRM card, best telemetry target first.

    Ryzen APU + Radeon (or Intel iGPU + Arc) systems expose two cards, and "first card" is often the idle
    iGPU. Rank discrete before integrated, then by dedicated VRAM (an AMD iGPU only has a small BIOS
    carve-out), then boot_vga, then whether the card has a hwmon node. An iGPU-only machine still gets
    its iGPU.
    """
    seen, cards = set(), []
    try:
        names = sorted((n for n in os.listdir(drm_root) if _DRM_CARD_RE.match(n)), key=lambda n: int(n[4:]))
    except OSError:
        return []
    for name in names:
        device = os.path.realpath(os.path.join(drm_root, name, "device"))
        vendor = _read_text(os.path.join(device, "vendor"))
        if device in seen or vendor not in _VENDOR_NAMES:
            continue
        seen.add(device)
        hwmons = sorted(glob.glob(os.path.join(device, "hwmon", "hwmon*")), key=_natural)
        vram = _read_int(os.path.join(device, "mem_info_vram_total")) or 0
        if vendor == _AMD_VENDOR:
            discrete = vram > DISCRETE_VRAM_BYTES
        else:  # i915/xe expose no VRAM size here; Arc cards are anything but the fixed iGPU slot
            discrete = os.path.basename(device) != _INTEL_IGPU_SLOT
        cards.append({
            "card": name,
            "card_dir": os.path.join(drm_root, name),
            "device_dir": device,
            "vendor": _VENDOR_NAMES[vendor],
            "hwmon_dir": hwmons[0] if hwmons else None,
            "vram_total": vram,
            "boot_vga": _read_int(os.path.join(device, "boot_vga")) == 1,
            "discrete": discrete,
        })
    cards.sort(key=lambda c: (c["discrete"], c["vram_total"], c["boot_vga"], c["hwmon_dir"] is not None),
               reverse=True)
    return cards


def amd_gpu_candidates(drm_root: str = DRM_ROOT) -> list[dict]:
    return [c for c in gpu_candidates(drm_root) if c["vendor"] == "AMD"]


class _DrmGpu:
    """AMD (amdgpu) or Intel (i915/xe) GPU via sysfs. Temperature, load, power and clock are all read from
    the one card picked by gpu_candidates(), so a multi-GPU box never mixes readings from two devices.
    """

    def __init__(self, drm_root: str = DRM_ROOT):
        candidates = gpu_candidates(drm_root)
        best = candidates[0] if candidates else None
        self.card = best["card"] if best else None
        self.card_path = best["card_dir"] if best else None      # /sys/class/drm/cardN (i915 freq files)
        self.card_dir = best["device_dir"] if best else None     # the PCI device
        self.hwmon_dir = best["hwmon_dir"] if best else None
        self.vendor = best["vendor"] if best else None
        self._energy = None  # (µJ, monotonic s) for cards that only expose energy1_input
        if best:
            log.info("%s GPU telemetry: %s (%s, %.1f GiB VRAM%s) of %d card(s)", self.vendor, self.card,
                     self.card_dir, best["vram_total"] / 2**30, ", discrete" if best["discrete"] else "",
                     len(candidates))

    @property
    def available(self) -> bool:
        return self.card_dir is not None

    def _temp(self) -> float | None:
        if not self.hwmon_dir:
            return None
        # amdgpu: edge (temp1), then junction (temp2); xe: pkg; i915 dGPU: temp1
        path = _pick_temp_input(self.hwmon_dir, ("edge", "junction", "pkg") if self.vendor == "AMD" else ("pkg",))
        v = _read_int(path) if path else None
        return v / 1000.0 if v is not None else None

    def _power(self) -> float | None:
        if not self.hwmon_dir:
            return None
        # RDNA3/4 kernels may only expose the instantaneous power1_input; older ones power1_average
        uw = _first_int(self.hwmon_dir, "power1_input", "power1_average")
        if uw is not None:
            return uw / 1_000_000.0
        uj = _read_int(os.path.join(self.hwmon_dir, "energy1_input"))  # Intel Arc: cumulative µJ
        if uj is None:
            return None
        now, prev = time.monotonic(), self._energy
        self._energy = (uj, now)
        if prev is None or now - prev[1] < 0.2 or uj < prev[0]:
            return None
        return (uj - prev[0]) / (now - prev[1]) / 1_000_000.0

    def _clock(self) -> float | None:
        if self.vendor == "AMD":
            try:
                with open(os.path.join(self.card_dir, "pp_dpm_sclk")) as f:
                    for line in f:
                        if "*" in line:
                            return float(line.split(":")[1].strip().rstrip("*").strip().rstrip("Mhz").strip())
            except (OSError, ValueError, IndexError):
                pass
            return None
        v = _first_int(self.card_path, "gt_act_freq_mhz", "gt_cur_freq_mhz")                   # i915
        if v is None:
            for p in sorted(glob.glob(os.path.join(self.card_dir, "tile*", "gt*", "freq0", "act_freq"))):
                v = _read_int(p)                                                               # xe
                if v:
                    break
        return float(v) if v is not None else None

    def read(self) -> dict:
        return {
            "gpu_temp": self._temp(),
            "gpu_load": _read_int(os.path.join(self.card_dir, "gpu_busy_percent")),  # amdgpu only
            "gpu_power": self._power(),
            "gpu_clock": self._clock(),
        }


_AmdGpu = _DrmGpu  # older name


# ---------------------------------------------------------------- NVIDIA

_GPU_NONE = {"gpu_temp": None, "gpu_load": None, "gpu_clock": None, "gpu_power": None}


class _NvidiaGpu:
    """NVIDIA GPU via nvidia-smi (works without root, no extra deps). With several cards, the one with
    the most memory is used for every reading. Results are cached for MIN_INTERVAL so nvidia-smi is never
    spawned more than once a second, and a failing nvidia-smi is retried only every RETRY_INTERVAL.
    """

    QUERY = "temperature.gpu,utilization.gpu,clocks.gr,power.draw,memory.total"
    MIN_INTERVAL = 1.0
    RETRY_INTERVAL = 10.0

    def __init__(self, exe: str = "nvidia-smi"):
        self.exe = shutil.which(exe)
        self._cache, self._next = dict(_GPU_NONE), 0.0
        self._available = self.exe is not None and self._probe()

    def _probe(self) -> bool:
        try:
            subprocess.run([self.exe, "-L"], capture_output=True, timeout=2, check=True, stdin=subprocess.DEVNULL)
            return True
        except Exception:
            return False

    @property
    def available(self) -> bool:
        return self._available

    @staticmethod
    def _num(field: str) -> float | None:
        try:
            return float(field.strip())
        except ValueError:
            return None  # "[N/A]", "[Not Supported]"

    def read(self) -> dict:
        now = time.monotonic()
        if now < self._next:
            return dict(self._cache)
        try:
            out = subprocess.run(
                [self.exe, f"--query-gpu={self.QUERY}", "--format=csv,noheader,nounits"],
                capture_output=True, timeout=2, text=True, check=True, stdin=subprocess.DEVNULL,
            )
            rows = [[self._num(f) for f in line.split(",")] for line in out.stdout.splitlines() if line.count(",") >= 4]
            if not rows:
                raise ValueError(f"unexpected nvidia-smi output: {out.stdout[:80]!r}")
            temp, load, clock, power, _mem = max(rows, key=lambda r: r[4] or 0)[:5]
            self._cache = {"gpu_temp": temp, "gpu_load": load, "gpu_clock": clock, "gpu_power": power}
            self._next = now + self.MIN_INTERVAL
        except Exception as e:
            log.debug("nvidia-smi read failed: %s", e)
            self._cache, self._next = dict(_GPU_NONE), now + self.RETRY_INTERVAL
        return dict(self._cache)


# ---------------------------------------------------------------- public API

def format_watts(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.0f}W"


def add_gpu_power_aliases(data: dict) -> dict:
    """gpu_wattage is an alias of gpu_power; *_str are ready-to-print ("85W")."""
    power = data.get("gpu_power")
    data["gpu_wattage"] = power
    data["gpu_power_str"] = data["gpu_wattage_str"] = format_watts(power)
    return data


class SensorReader:
    """Polls CPU/GPU telemetry, cheaply enough to run once a second. Never raises: a missing sensor (no GPU,
    no hwmon, no nvidia-smi) is None.
    """

    def __init__(self, hwmon_root: str = HWMON_ROOT, thermal_root: str = THERMAL_ROOT, drm_root: str = DRM_ROOT,
                 nvidia_smi: str = "nvidia-smi"):
        psutil.cpu_percent(interval=None)  # prime the internal counter
        self._cpu_temp = _CpuTemp(hwmon_root, thermal_root)
        self._gpu = None
        for backend in (lambda: _NvidiaGpu(nvidia_smi), lambda: _DrmGpu(drm_root)):
            try:
                candidate = backend()
            except Exception:
                log.exception("GPU telemetry backend failed to initialise")
                continue
            if candidate.available:
                self._gpu = candidate
                log.info("GPU telemetry backend: %s", type(candidate).__name__)
                break
        if self._gpu is None:
            log.info("no supported GPU telemetry backend found; GPU readings will show N/A")

    @property
    def gpu_backend(self):
        return self._gpu

    def _cpu(self) -> dict:
        try:
            freq = psutil.cpu_freq()
        except Exception:
            freq = None
        return {
            "cpu_temp": self._cpu_temp.read(),
            "cpu_load": psutil.cpu_percent(interval=None),
            "cpu_freq": freq.current if freq else None,
        }

    def read(self) -> dict:
        data = {"time": time.strftime("%H:%M:%S"), "date": time.strftime("%Y-%m-%d")}
        data.update(self._cpu())
        mem = psutil.virtual_memory()
        data.update({"ram_percent": mem.percent, "ram_used": mem.used / 2**30, "ram_total": mem.total / 2**30})
        gpu = dict(_GPU_NONE)
        if self._gpu is not None:
            try:
                gpu.update(self._gpu.read())
            except Exception:
                log.exception("GPU read failed")
        data.update(gpu)
        return add_gpu_power_aliases(data)
