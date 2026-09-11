"""Whole-call waiting budgets retain started jobs and forbid late new writes.

A caller may stop waiting while a SQLite point read is still running. That read
keeps its owner, but the expired preparation cannot subsequently start a command.
Once a command was issued, its original confirmation reference remains available.
"""
from contextvars import ContextVar
import asyncio
from dataclasses import dataclass
from functools import wraps
from typing import Callable,Coroutine,ParamSpec,TypeVar
import time
from companion_memory.persistence import RecoveryHandle
from .results import Failed,Rejected,Unconfirmed


@dataclass(slots=True)
class CallBudget:
    deadline:float
    stopped:bool=False
    reference:RecoveryHandle | None=None
    owner:asyncio.Task | None=None


budget:ContextVar[CallBudget | None]=ContextVar('runtime_call_budget',default=None)
P=ParamSpec('P')
R=TypeVar('R')


def expired() -> bool:
    current=budget.get()
    return current is not None and (current.stopped or time.monotonic()>=current.deadline)


def bounded(operation:str,*,read_only:bool=False):
    """Wrap a runtime-owned public operation without cancelling its resource owner."""
    def decorate(function:Callable[P,Coroutine[object,object,R]]) -> Callable[P,Coroutine[object,object,R | Failed | Rejected | Unconfirmed]]:
        @wraps(function)
        async def wrapped(*args:P.args,**kwargs:P.kwargs) -> R | Failed | Rejected | Unconfirmed:
            from .service import RuntimeService
            runtime=args[0]
            if type(runtime) is not RuntimeService:raise TypeError('A native runtime coordinator is required.')
            selected=operation
            if operation=='accept_event' and (len(args)>3 and args[3] is True or kwargs.get('resolve') is True):selected='resolve_acceptance'
            return await runtime._bounded_call(selected,lambda:function(*args,**kwargs),read_only)
        return wrapped
    return decorate
