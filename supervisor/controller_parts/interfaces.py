"""Composition and backwards-compatible access to service-owned state.

Ports declare the precise coordinator names a service can read or write. State
stays in one slotted record per service; legacy controller attributes are views
of those records, never copies. Construction is lazy so ``__new__`` callers keep
the historical distinction between an unset attribute and an initialized value.
"""

from functools import wraps
from inspect import iscoroutinefunction
from typing import Any, Callable

from . import compat


class CoordinatorPort:
    """A bounded interface, not an unrestricted controller proxy.

    Reads include explicitly declared callbacks and borrowed infrastructure.
    Writes are declared lifecycle transitions on another owner's state. All
    callback lookup is late, preserving instance and class monkeypatches.
    """

    __slots__ = ("_target",)
    reads: frozenset[str] = frozenset()
    writes: frozenset[str] = frozenset()

    def __init__(self, target: Any) -> None:
        object.__setattr__(self, "_target", target)

    def __getattr__(self, name: str) -> Any:
        if name not in self.reads:
            raise AttributeError(f"{type(self).__name__} does not expose {name}")
        return getattr(self._target, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name not in self.writes:
            raise AttributeError(f"{type(self).__name__} cannot change {name}")
        setattr(self._target, name, value)

    def __delattr__(self, name: str) -> None:
        if name not in self.writes:
            raise AttributeError(f"{type(self).__name__} cannot delete {name}")
        delattr(self._target, name)


class OwnedField:
    """Compatibility descriptor forwarding to exactly one state owner."""

    def __init__(self, owner: str, name: str) -> None:
        self.owner = owner
        self.name = name

    def __get__(self, instance: Any, owner: type | None = None) -> Any:
        if instance is None:
            return self
        return getattr(instance._service(self.owner).state, self.name)

    def __set__(self, instance: Any, value: Any) -> None:
        setattr(instance._service(self.owner).state, self.name, value)

    def __delete__(self, instance: Any) -> None:
        delattr(instance._service(self.owner).state, self.name)


def service_method(owner: str, implementation: Callable[..., Any]) -> Callable[..., Any]:
    """Keep the callable class/instance API while dispatching to composition.

    ``wraps`` retains signatures and points source inspection at the
    implementation. Async entry points remain recognizable as coroutine
    functions. Both wrappers are normal methods and remain replaceable.
    """

    if iscoroutinefunction(implementation):
        @wraps(implementation)
        async def invoke_async(controller: Any, *args: Any, **kwargs: Any) -> Any:
            return await implementation(controller._service(owner), *args, **kwargs)

        return invoke_async

    @wraps(implementation)
    def invoke(controller: Any, *args: Any, **kwargs: Any) -> Any:
        return implementation(controller._service(owner), *args, **kwargs)

    return invoke
