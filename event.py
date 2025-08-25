from dataclasses import dataclass
from collections.abc import Callable
from typing import Any
from threading import Lock

class Event:
    @dataclass    
    class Function:
        function: Callable[[Any], None]
        args: list[Any]
        kwargs: dict[str, Any]

    def __init__(self) -> None:
        self._functions: list[Event.Function] = []
        self._lock = Lock()

    def connect(self, function: Callable[[Any], None], args: list[Any] = [], kwargs: dict[str, Any] = {}) -> None:
        with self._lock:
            self._functions.append(Event.Function(function, args, kwargs))

    def emit(self, *args, **kwargs) -> None:
        with self._lock:
            for func in self._functions:
                func.function(*args, *func.args, **(func.kwargs | kwargs))
                