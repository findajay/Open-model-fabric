"""The machine the API process runs on.

Detects CPU, memory and NVIDIA GPUs (via ``nvidia-smi`` when installed). No GPU is
required; absence is reported as ``accelerator: none``. Lifecycle operations
(provision/start/stop) are not supported: the API cannot start the machine it runs on.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from omf.core.domain.models import (
    ComputeEnvironment,
    ComputeOperation,
    ComputeResources,
    ComputeState,
    GpuInfo,
    utcnow,
)
from omf.core.ports.compute import ComputeProvider

ADAPTER_TYPE = "local"


class LocalComputeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = "Local machine"
    detect_gpus: bool = True


def _meminfo() -> tuple[int | None, int | None]:
    path = Path("/proc/meminfo")
    if path.exists():
        values: dict[str, int] = {}
        for line in path.read_text().splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                values[key] = int(parts[0]) * 1024
        return values.get("MemTotal"), values.get("MemAvailable")
    try:  # POSIX fallback (macOS)
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        return total, None
    except (ValueError, OSError, AttributeError):
        return None, None


def _cpu_model() -> str | None:
    path = Path("/proc/cpuinfo")
    if path.exists():
        for line in path.read_text().splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    return platform.processor() or None


def _nvidia_gpus() -> list[GpuInfo]:
    exe = shutil.which("nvidia-smi")
    if exe is None:
        return []
    try:
        out = subprocess.run(  # noqa: S603 - fixed executable and arguments
            [
                exe,
                "--query-gpu=name,memory.total,memory.used,driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    gpus = []
    for row in out.strip().splitlines():
        cols = [c.strip() for c in row.split(",")]
        if len(cols) >= 4:
            gpus.append(
                GpuInfo(
                    name=cols[0],
                    memory_total_bytes=int(float(cols[1]) * 1024 * 1024)
                    if cols[1].replace(".", "").isdigit()
                    else None,
                    memory_used_bytes=int(float(cols[2]) * 1024 * 1024)
                    if cols[2].replace(".", "").isdigit()
                    else None,
                    driver_version=cols[3],
                )
            )
    return gpus


def detect_resources(detect_gpus: bool = True) -> ComputeResources:
    total, available = _meminfo()
    gpus = _nvidia_gpus() if detect_gpus else []
    accelerator = "cuda" if gpus else "none"
    if not gpus and platform.system() == "Darwin" and platform.machine() == "arm64":
        accelerator = "mps"
    return ComputeResources(
        cpu_count=os.cpu_count(),
        cpu_model=_cpu_model(),
        architecture=platform.machine(),
        memory_total_bytes=total,
        memory_available_bytes=available,
        gpus=gpus,
        accelerator=accelerator,
        source="detected",
    )


class LocalComputeAdapter(ComputeProvider):
    def __init__(self, environment_id: str, config: LocalComputeConfig) -> None:
        super().__init__(environment_id)
        self._config = config

    def supported_operations(self) -> list[ComputeOperation]:
        return [
            ComputeOperation.DESCRIBE,
            ComputeOperation.GET_RESOURCES,
            ComputeOperation.GET_STATUS,
        ]

    async def get_resources(self) -> ComputeResources:
        return await asyncio.to_thread(detect_resources, self._config.detect_gpus)

    async def get_status(self) -> ComputeState:
        return ComputeState.ONLINE

    async def describe_environment(self) -> ComputeEnvironment:
        return ComputeEnvironment(
            id=self.environment_id,
            kind=ADAPTER_TYPE,
            display_name=self._config.display_name,
            ephemeral=False,
            managed="automatic",
            state=await self.get_status(),
            resources=await self.get_resources(),
            operations=self.supported_operations(),
            details={
                "os": platform.system(),
                "os_release": platform.release(),
                "python": platform.python_version(),
            },
            checked_at=utcnow(),
        )
