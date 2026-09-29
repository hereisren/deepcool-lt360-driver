"""Live hardware telemetry for the overlay compositor.

Reads CPU temp/usage/frequency via psutil, and GPU temp/load/power/clock via the
amdgpu sysfs tree (AMD; the discrete card on iGPU + dGPU systems) or nvidia-smi (NVIDIA). Every read is
best-effort: a sensor that isn't present on this machine returns None rather
than raising, so the overlay can just skip that metric.
"""
import glob
import logging
import os
import re
import subprocess
import time

import psutil

log = logging.getLogger("lt360d.sensors")

_CPU_TEMP_LABELS = ("tctl", "tdie", "package id 0", "cpu")


def _read_cpu_temp() -> float | None:
    try:
        temps = psutil.sensors_temperatures()
    except Exception:
        return None
    for chip in ("k10temp", "zenpower", "coretemp"):
        entries = temps.get(chip)
        if not entries:
            continue
        for e in entries:
            label = (e.label or "").lower()
            if any(l in label for l in _CPU_TEMP_LABELS) or not e.label:
                return e.current
        return entries[0].current
    return None


def _read_cpu() -> dict:
    return {
        "cpu_temp": _read_cpu_temp(),
        "cpu_load": psutil.cpu_percent(interval=None),
        "cpu_freq": (psutil.cpu_freq().current if psutil.cpu_freq() else None),
    }


_DRM_CARD_RE = re.compile(r"^card\d+$")   # card0, card1 -- not connectors like card0-DP-1
_AMD_VENDOR = "0x1002"
DISCRETE_VRAM_BYTES = 2 * 2**30          # more dedicated VRAM than this => discrete card


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


def amd_gpu_candidates(drm_root: str = "/sys/class/drm") -> list[dict]:
    """Every amdgpu card, best telemetry target first.

    Ryzen APU + Radeon systems expose the integrated and the discrete GPU through the same driver,
    so "first amdgpu card" is often the idle iGPU. Rank by dedicated VRAM (an iGPU only has a small
    BIOS carve-out), then by boot_vga, then by whether the card has a hwmon node.
    """
    seen, cards = set(), []
    try:
        names = sorted(os.listdir(drm_root), key=lambda n: int(n[4:]) if _DRM_CARD_RE.match(n) else 0)
    except OSError:
        return []
    for name in names:
        if not _DRM_CARD_RE.match(name):
            continue
        device = os.path.realpath(os.path.join(drm_root, name, "device"))
        if device in seen or _read_text(os.path.join(device, "vendor")) != _AMD_VENDOR:
            continue
        seen.add(device)
        hwmons = sorted(glob.glob(os.path.join(device, "hwmon", "hwmon*")))
        vram = _read_int(os.path.join(device, "mem_info_vram_total")) or 0
        cards.append({
            "card": name,
            "device_dir": device,
            "hwmon_dir": hwmons[0] if hwmons else None,
            "vram_total": vram,
            "boot_vga": _read_int(os.path.join(device, "boot_vga")) == 1,
            "discrete": vram > DISCRETE_VRAM_BYTES,
        })
    cards.sort(key=lambda c: (c["discrete"], c["vram_total"], c["boot_vga"], c["hwmon_dir"] is not None),
               reverse=True)
    return cards


class _AmdGpu:
    """AMD GPU via sysfs. Temperature, load, power and clock are all read from the one card picked by
    amd_gpu_candidates(), so a multi-GPU box never mixes readings from two devices.
    """

    def __init__(self, drm_root: str = "/sys/class/drm"):
        candidates = amd_gpu_candidates(drm_root)
        best = candidates[0] if candidates else None
        self.card = best["card"] if best else None
        self.card_dir = best["device_dir"] if best else None
        self.hwmon_dir = best["hwmon_dir"] if best else None
        if best:
            log.info("AMD GPU telemetry: %s (%s, %.1f GiB VRAM%s) of %d amdgpu card(s)", self.card, self.card_dir,
                     best["vram_total"] / 2**30, ", discrete" if best["discrete"] else "", len(candidates))

    @property
    def available(self) -> bool:
        return self.card_dir is not None

    def _clock(self) -> float | None:
        try:
            with open(os.path.join(self.card_dir, "pp_dpm_sclk")) as f:
                for line in f:
                    if "*" in line:
                        return float(line.split(":")[1].strip().rstrip("*").strip().rstrip("Mhz").strip())
        except (OSError, ValueError, IndexError):
            pass
        return None

    def read(self) -> dict:
        temp = power = None
        if self.hwmon_dir:
            temp = _first_int(self.hwmon_dir, "temp1_input", "temp2_input")          # edge, else junction
            # RDNA3/4 kernels may only expose the instantaneous power1_input; older ones power1_average
            power = _first_int(self.hwmon_dir, "power1_input", "power1_average")   # microwatts
        return {
            "gpu_temp": temp / 1000.0 if temp is not None else None,
            "gpu_load": _read_int(os.path.join(self.card_dir, "gpu_busy_percent")),
            "gpu_power": power / 1_000_000.0 if power is not None else None,
            "gpu_clock": self._clock(),
        }


class _NvidiaGpu:
    """NVIDIA GPU via nvidia-smi (works without root, no extra deps). With several cards, the one with
    the most memory is used for every reading.
    """

    QUERY = "temperature.gpu,utilization.gpu,clocks.gr,power.draw,memory.total"

    def __init__(self):
        self._available = self._probe()

    def _probe(self) -> bool:
        try:
            subprocess.run(["nvidia-smi", "-L"], capture_output=True, timeout=2, check=True, stdin=subprocess.DEVNULL)
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
        try:
            out = subprocess.run(
                ["nvidia-smi", f"--query-gpu={self.QUERY}", "--format=csv,noheader,nounits"],
                capture_output=True, timeout=2, text=True, check=True, stdin=subprocess.DEVNULL,
            )
            rows = [[self._num(f) for f in line.split(",")] for line in out.stdout.splitlines() if line.count(",") >= 4]
            if not rows:
                raise ValueError(f"unexpected nvidia-smi output: {out.stdout[:80]!r}")
            temp, load, clock, power, _mem = max(rows, key=lambda r: r[4] or 0)[:5]
            return {"gpu_temp": temp, "gpu_load": load, "gpu_clock": clock, "gpu_power": power}
        except Exception as e:
            log.debug("nvidia-smi read failed: %s", e)
            return {"gpu_temp": None, "gpu_load": None, "gpu_clock": None, "gpu_power": None}


def format_watts(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.0f}W"


def add_gpu_power_aliases(data: dict) -> dict:
    """gpu_wattage is an alias of gpu_power; *_str are ready-to-print ("85W")."""
    power = data.get("gpu_power")
    data["gpu_wattage"] = power
    data["gpu_power_str"] = data["gpu_wattage_str"] = format_watts(power)
    return data


class SensorReader:
    """Polls CPU/GPU telemetry, cheaply enough to run once a second."""

    def __init__(self):
        psutil.cpu_percent(interval=None)  # prime the internal counter
        self._gpu = None
        for backend in (_NvidiaGpu, _AmdGpu):
            try:
                candidate = backend()
            except Exception:
                continue
            if candidate.available:
                self._gpu = candidate
                log.info("GPU telemetry backend: %s", backend.__name__)
                break
        if self._gpu is None:
            log.info("no supported GPU telemetry backend found")

    @property
    def gpu_backend(self):
        return self._gpu

    def read(self) -> dict:
        data = {"time": time.strftime("%H:%M:%S"), "date": time.strftime("%Y-%m-%d")}
        data.update(_read_cpu())
        mem = psutil.virtual_memory()
        data.update({"ram_percent": mem.percent, "ram_used": mem.used / 2**30, "ram_total": mem.total / 2**30})
        if self._gpu is not None:
            data.update(self._gpu.read())
        else:
            data.update({"gpu_temp": None, "gpu_load": None, "gpu_clock": None, "gpu_power": None})
        return add_gpu_power_aliases(data)
