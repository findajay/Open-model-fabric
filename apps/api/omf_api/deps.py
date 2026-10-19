"""FastAPI dependencies giving handlers access to the composition root."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from omf.bootstrap.container import Container


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]
