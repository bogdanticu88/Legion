"""Python functions as tools. The argument model supplies the JSON schema.

    class ReadArgs(BaseModel):
        path: str

    @tool(effect=EffectClass.READ, capabilities=["files.read"], resource=lambda a: a.path)
    async def read_file(args: ReadArgs, ctx: ToolContext) -> str:
        ...

Native tools run inside the harness process. The pipeline decides whether they run; it cannot
limit what their code does once it runs.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Sequence
from typing import Any, get_type_hints

from pydantic import BaseModel, ValidationError

from legion.domain.action import EffectClass
from legion.domain.errors import InvalidArguments
from legion.tools.base import ToolContext, ToolResult, ToolSpec, resource_from_argument


class NativeTool:
    def __init__(
        self,
        fn: Callable[..., Any],
        spec: ToolSpec,
        args_model: type[BaseModel],
        resource: Callable[[Any], str | None] | None,
    ) -> None:
        self._fn = fn
        self._spec = spec
        self._args_model = args_model
        self._resource = resource

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    def _parse(self, arguments: dict[str, Any]) -> BaseModel:
        try:
            return self._args_model.model_validate(arguments)
        except ValidationError as exc:
            raise InvalidArguments(_short(exc)) from exc

    def resource_of(self, arguments: dict[str, Any]) -> str | None:
        if self._resource is None:
            return resource_from_argument(self._spec, arguments)
        value = self._resource(self._parse(arguments))
        if value is not None and (not isinstance(value, str) or not value):
            raise InvalidArguments("resource must be a non-empty string")
        return value

    async def invoke(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        args = self._parse(arguments)
        if inspect.iscoroutinefunction(self._fn):
            result = await self._fn(args, context)
        else:
            result = await asyncio.to_thread(self._fn, args, context)
        return _to_result(result)


def tool(
    *,
    effect: EffectClass,
    capabilities: Sequence[str] = (),
    name: str | None = None,
    description: str | None = None,
    resource: Callable[[Any], str | None] | None = None,
    resource_arg: str | None = None,
    output_schema: dict[str, Any] | None = None,
    timeout_s: float = 30.0,
    max_attempts: int = 2,
    credentials: Sequence[str] = (),
    max_output_chars: int = 20_000,
) -> Callable[[Callable[..., Any]], NativeTool]:
    def wrap(fn: Callable[..., Any]) -> NativeTool:
        params = list(inspect.signature(fn).parameters)
        if len(params) != 2:
            raise TypeError(f"{fn.__name__} must take (args, context)")
        hints = get_type_hints(fn)
        args_model = hints.get(params[0])
        if not (isinstance(args_model, type) and issubclass(args_model, BaseModel)):
            raise TypeError(f"first parameter of {fn.__name__} must be a pydantic model")
        doc = inspect.getdoc(fn) or ""
        spec = ToolSpec(
            name=name or fn.__name__,
            description=description or doc.split("\n\n")[0].strip() or fn.__name__,
            input_schema=args_model.model_json_schema(),
            output_schema=output_schema,
            effect=effect,
            capabilities=tuple(capabilities),
            resource_arg=resource_arg,
            timeout_s=timeout_s,
            max_attempts=max_attempts,
            credentials=tuple(credentials),
            max_output_chars=max_output_chars,
        )
        return NativeTool(fn, spec, args_model, resource)

    return wrap


def _to_result(value: Any) -> ToolResult:
    if isinstance(value, ToolResult):
        return value
    if isinstance(value, str):
        return ToolResult(content=value)
    if isinstance(value, BaseModel):
        return ToolResult(data=value.model_dump(mode="json"))
    if value is None or isinstance(value, dict | list | int | float | bool):
        return ToolResult(data=value)
    raise TypeError(f"tool returned unsupported type {type(value).__name__}")


def _short(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or 'arguments'}: {err['msg']}"
        for err in exc.errors()[:5]
    )
