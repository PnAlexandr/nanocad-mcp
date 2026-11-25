"""Dispatch upstream use cases only through the currently granted document."""
import asyncio
from jsonschema import validate
from evans.application.use_case_factory import UseCaseFactory
from evans.domain.history_registry import set_history_registry
from evans.domain.parameter_registry import set_parameter_registry
from evans.infrastructure.cad_repository import CadRepository
from evans.infrastructure.http_bridge import HttpCadBridge
from evans.presentation.tool_validation import validate_tool_input
from evans.routes import ROUTES
from tool_catalog import EXTRA, require_enabled

_scope = None


def invoke(public_name, arguments, request, scope, check_target):
    global _scope
    if scope != _scope:
        set_history_registry(None)
        set_parameter_registry(None)
        _scope = scope
    factory = UseCaseFactory(CadRepository(HttpCadBridge(request)))

    def dispatch(name, args):
        check_target()
        require_enabled("extra_" + name)
        validate(args, EXTRA["extra_" + name].inputSchema)
        validate_tool_input(name, args)
        owner, method = ROUTES[name]
        if name == "create_grid_axis" and "type" in args:
            args = {**args, "grid_type": args["type"]}
            del args["type"]
        return getattr(getattr(factory, owner), method)(**args)

    name = public_name.removeprefix("extra_")
    if name == "replay_history":
        async def replay_one(tool, args):
            if tool == "replay_history":
                raise ValueError("Вложенный повтор истории недоступен")
            result = dispatch(tool, args)
            if isinstance(result, dict) and result.get("success") is False:
                raise ValueError(result.get("error", "Ошибка выполнения"))
            return result
        return asyncio.run(factory.history.replay_history(replay_one))
    return dispatch(name, arguments)
