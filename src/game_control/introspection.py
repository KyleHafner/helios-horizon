"""Bounded signature inspection for fixed internal compatibility seams."""

from __future__ import annotations

import inspect
from functools import lru_cache
from typing import Any, Callable


@lru_cache(maxsize=256)
def _function_parameters(
    function: Callable[..., Any], bound: bool
) -> tuple[inspect.Parameter, ...]:
    parameters = tuple(inspect.signature(function).parameters.values())
    return parameters[1:] if bound and parameters else parameters


def signature_parameters(
    function: Callable[..., Any],
) -> tuple[inspect.Parameter, ...]:
    """Cache stable functions without retaining injected service instances."""
    underlying = getattr(function, "__func__", None)
    if inspect.isfunction(underlying):
        return _function_parameters(underlying, True)
    if inspect.isfunction(function):
        return _function_parameters(function, False)
    return tuple(inspect.signature(function).parameters.values())


__all__ = ["signature_parameters"]
