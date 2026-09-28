"""Live hardware telemetry for the overlay compositor.

Reads CPU temp/usage/frequency via psutil, and GPU temp/load/clock via the
amdgpu sysfs hwmon tree (AMD) or nvidia-smi/NVML (NVIDIA). Every read is
best-effort: a sensor that isn't present on this machine returns None rather
than raising, so the overlay can just skip that metric.
"""
import glob
import logging
import os
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


class _AmdGpu:
    """AMD GPU via /sys/class/drm/card*/device/hwmon/*."""

    def __init__(self):
        self.hwmon_dir = None
        self.card_dir = None
        for card in sorted(glob.glob("/sys/class/drm/card[0-9]*/device")):
            vendor_path = os.path.join(card, "vendor")
            if not os.path.isfile(vendor_path):
                continue
            try:
                with open(vendor_path) as f:
                    vendor = f.read().strip()
            except OSError:
                continue
            if vendor != "0x1002":  # AMD PCI vendor id
                continue
            hwmons = glob.glob(os.path.join(card, "hwmon", "hwmon*"))
            if hwmons:
                self.card_dir = card
                self.hwmon_dir = hwmons[0]
                break

    @property
    def available(self) -> bool:
        return self.hwmon_dir is not None

    def _read_int(self, *parts) -> int | None:
        path = os.path.join(*parts)
        try:
            with open(path) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    def read(self) -> dict:
        temp = self._read_int(self.hwmon_dir, "temp1_input")
        power = self._read_int(self.hwmon_dir, "power1_average")
        clock = None
        freq_path = os.path.join(self.card_dir, "pp_dpm_sclk")
        try:
            with open(freq_path) as f:
                for line in f:
                    if "*" in line:
                        clock = float(line.split(":")[1].strip().rstrip("*").strip().rstrip("Mhz").strip())
                        break
        except (OSError, ValueError, IndexError):
            pass
        load = self._read_int(self.card_dir, "gpu_busy_percent")
        return {
            "gpu_temp": temp / 1000.0 if temp is not None else None,
            "gpu_load": load,
            "gpu_power": power / 1_000_000.0 if power is not None else None,
            "gpu_clock": clock,
        }


class _NvidiaGpu:
    """NVIDIA GPU via nvidia-smi (works without root, no extra deps)."""

    QUERY = "temperature.gpu,utilization.gpu,clocks.gr,power.draw"

    def __init__(self):
        self._available = self._probe()

    def _probe(self) -> bool:
        try:
            subprocess.run(["nvidia-smi", "-L"], capture_output=True, timeout=2, check=True)
            return True
        except Exception:
            return False

    @property
    def available(self) -> bool:
        return self._available

    def read(self) -> dict:
        try:
            out = subprocess.run(
                ["nvidia-smi", f"--query-gpu={self.QUERY}", "--format=csv,noheader,nounits"],
                capture_output=True, timeout=2, text=True, check=True,
            )
            temp, load, clock, power = (p.strip() for p in out.stdout.strip().split(",")[:4])
            return {
                "gpu_temp": float(temp),
                "gpu_load": float(load),
                "gpu_clock": float(clock),
                "gpu_power": float(power),
            }
        except Exception as e:
            log.debug("nvidia-smi read failed: %s", e)
            return {"gpu_temp": None, "gpu_load": None, "gpu_clock": None, "gpu_power": None}


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

    def read(self) -> dict:
        data = {"time": time.strftime("%H:%M:%S"), "date": time.strftime("%Y-%m-%d")}
        data.update(_read_cpu())
        mem = psutil.virtual_memory()
        data.update({"ram_percent": mem.percent, "ram_used": mem.used / 2**30, "ram_total": mem.total / 2**30})
        if self._gpu is not None:
            data.update(self._gpu.read())
        else:
            data.update({"gpu_temp": None, "gpu_load": None, "gpu_clock": None, "gpu_power": None})
        return data
