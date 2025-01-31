from __future__ import annotations

import logging
import random
import typing as t
from collections import defaultdict
from dataclasses import dataclass, field as Field

from django.contrib.auth.models import AbstractBaseUser, AnonymousUser
from django.core.signing import Signer
from django.db import models
from django.db.models.signals import post_save, pre_delete
from django.dispatch.dispatcher import receiver
from django.http import HttpRequest, QueryDict
from django.utils.html import format_html
from django.utils.safestring import SafeString, mark_safe
from pydantic import ValidationError
from uuid6 import uuid7

from djhtmx.tracing import sentry_span

from . import json
from .command_queue import CommandQueue
from .component import (
    LISTENERS,
    REGISTRY,
    BuildAndRender,
    Command,
    Destroy,
    DispatchDOMEvent,
    Emit,
    Execute,
    Focus,
    Open,
    PydanticComponent,
    Redirect,
    Render,
    Signal,
    SkipRender,
    _get_query_patchers,
)
from .introspection import filter_parameters, get_related_fields
from .settings import (
    KEY_SIZE_ERROR_THRESHOLD,
    KEY_SIZE_SAMPLE_PROB,
    KEY_SIZE_WARN_THRESHOLD,
    LOGIN_URL,
    SESSION_TTL,
    conn,
)
from .utils import db, get_model_subscriptions, get_params

signer = Signer()

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SendHtml:
    content: SafeString

    # XXX: Just to debug...
    debug_trace: str | None = None


@dataclass(slots=True)
class PushURL:
    url: str
    command: t.Literal["push_url"] = "push_url"

    @classmethod
    def from_params(cls, params: QueryDict):
        return cls("?" + params.urlencode())


ProcessedCommand = Destroy | Redirect | Open | Focus | DispatchDOMEvent | SendHtml | PushURL


class Repository:
    """An in-memory (cheap) mapping of component IDs to its states.

    When an HTMX request comes, all the state from all the components are
    placed in a registry.  This way we can instantiate components if/when
    needed.

    For instance, if a component is subscribed to an event and the event fires
    during the request, that component is rendered.

    """

    @staticmethod
    def new_session_id():
        return f"djhtmx:{uuid7().hex}"

    @classmethod
    def from_request(
        cls,
        request: HttpRequest,
    ) -> Repository:
        """Get or build the Repository from the request.

        If the request has already a Repository attached, return it without
        further processing.

        Otherwise, build the repository from the request's POST and attach it
        to the request.

        """
        if (result := getattr(request, "htmx_repo", None)) is None:
            if (signed_session := request.META.get("HTTP_HX_SESSION")) and not bool(
                request.META.get("HTTP_HX_BOOSTED")
            ):
                session_id = signer.unsign(signed_session)
            else:
                session_id = cls.new_session_id()

            session = Session(session_id)

            result = cls(
                user=getattr(request, "user", AnonymousUser()),
                session=session,
                params=get_params(request),
            )
            setattr(request, "htmx_repo", result)
        return result

    @classmethod
    def from_websocket(
        cls,
        user: AbstractBaseUser | AnonymousUser,
    ):
        return cls(
            user=user,
            session=Session(cls.new_session_id()),  # TODO: take the session from the websocket url
            params=get_params(None),
        )

    @staticmethod
    def load_states_by_id(states: list[str]) -> dict[str, dict[str, t.Any]]:
        return {
            state["id"]: state for state in [json.loads(signer.unsign(state)) for state in states]
        }

    @staticmethod
    def load_subscriptions(
        states_by_id: dict[str, dict[str, t.Any]], subscriptions: dict[str, str]
    ) -> dict[str, set[str]]:
        subscriptions_to_ids: dict[str, set[str]] = defaultdict(set)
        for component_id, component_subscriptions in subscriptions.items():
            # Register query string subscriptions
            component_name = states_by_id[component_id]["hx_name"]
            for patcher in _get_query_patchers(component_name):
                subscriptions_to_ids[patcher.signal_name].add(component_id)

            # Register other subscriptions
            for subscription in component_subscriptions.split(","):
                subscriptions_to_ids[subscription].add(component_id)
        return subscriptions_to_ids

    def __init__(
        self,
        user: AbstractBaseUser | AnonymousUser,
        session: Session,
        params: QueryDict,
    ):
        self.user = user
        self.session = session
        self.session_signed_id = signer.sign(session.id)
        self.params = params

    # Component life cycle & management

    def unregister_component(self, component_id: str):
        # delete component state
        self.session.unregister_component(component_id)

    async def adispatch_event(  # pragma: no cover
        self,
        component_id: str,
        event_handler: str,
        event_data: dict[str, t.Any],
    ) -> t.AsyncIterable[ProcessedCommand]:
        commands = CommandQueue([Execute(component_id, event_handler, event_data)])

        # Listen to model signals during execution
        @receiver(post_save, weak=True)
        @receiver(pre_delete, weak=True)
        def _listen_to_post_save_and_pre_delete(
            sender: type[models.Model],
            instance: models.Model,
            created: bool = None,
            **kwargs,
        ):
            if created is None:
                action = "deleted"
            elif created:
                action = "created"
            else:
                action = "updated"

            signals = get_model_subscriptions(instance, actions=(action,))
            for field in get_related_fields(sender):
                fk_id = getattr(instance, field.name)
                signal = f"{field.related_model_name}.{fk_id}.{field.relation_name}"
                signals.update((signal, f"{signal}.{action}"))

            if signals:
                commands.append(Signal(signals))

        # Command loop
        try:
            while commands:
                processed_commands = self._run_command(commands)
                while command := await db(next)(processed_commands, None):
                    yield command
        except ValidationError as e:
            if any(
                e
                for error in e.errors()
                if error["type"] == "is_instance_of" and error["loc"] == ("user",)
            ):
                yield Redirect(LOGIN_URL)
            else:
                raise e

    def dispatch_event(
        self,
        component_id: str,
        event_handler: str,
        event_data: dict[str, t.Any],
    ) -> t.Iterable[ProcessedCommand]:
        commands = CommandQueue([Execute(component_id, event_handler, event_data)])

        # Listen to model signals during execution
        @receiver(post_save, weak=True)
        @receiver(pre_delete, weak=True)
        def _listen_to_post_save_and_pre_delete(
            sender: type[models.Model],
            instance: models.Model,
            created: bool = None,
            **kwargs,
        ):
            if created is None:
                action = "deleted"
            elif created:
                action = "created"
            else:
                action = "updated"

            signals = get_model_subscriptions(instance, actions=(action,))
            for field in get_related_fields(sender):
                fk_id = getattr(instance, field.name)
                signal = f"{field.related_model_name}.{fk_id}.{field.relation_name}"
                signals.update((signal, f"{signal}.{action}"))

            if signals:
                commands.append(Signal(signals))

        # Command loop
        try:
            while commands:
                for command in self._run_command(commands):
                    yield command
        except ValidationError as e:
            if any(
                e
                for error in e.errors()
                if error["type"] == "is_instance_of" and error["loc"] == ("user",)
            ):
                yield Redirect(LOGIN_URL)
            else:
                raise e

    def _run_command(self, commands: CommandQueue) -> t.Generator[ProcessedCommand, None, None]:
        command = commands.pop()
        logger.debug("COMMAND: %s", command)
        commands_to_append: list[Command] = []
        match command:
            case Execute(component_id, event_handler, event_data):
                match self.get_component_by_id(component_id):
                    case Destroy() as command:
                        yield command
                    case component:
                        handler = getattr(component, event_handler)
                        handler_kwargs = filter_parameters(handler, event_data)
                        emited_commands = handler(**handler_kwargs)
                        yield from self._process_emited_commands(
                            component, emited_commands, commands, during_execute=True
                        )

            case SkipRender(component):
                self.session.store(component)

            case BuildAndRender(component_type, state, oob):
                component = self.build(component_type.__name__, state)
                commands_to_append.append(Render(component, oob=oob))

            case Render(component, template, oob, lazy):
                html = self.render_html(component, oob=oob, template=template, lazy=lazy)
                yield SendHtml(html, debug_trace=f"{component.hx_name}({component.id})")

            case Destroy(component_id) as command:
                self.unregister_component(component_id)
                yield command

            case Emit(event):
                for component in self.get_components_by_names(*LISTENERS[type(event)]):
                    logger.debug("< AWAKED: %s id=%s", component.hx_name, component.id)
                    emited_commands = component._handle_event(event)  # type: ignore
                    yield from self._process_emited_commands(
                        component, emited_commands, commands, during_execute=False
                    )

            case Signal(signals):
                for component_or_destroy in self.get_components_subscribed_to(signals):
                    match component_or_destroy:
                        case Destroy() as command:
                            yield command
                        case component:
                            logger.debug("< AWAKED: %s id=%s", component.hx_name, component.id)
                            commands_to_append.append(Render(component))

            case Open() | Redirect() | Focus() | DispatchDOMEvent() as command:
                yield command

        commands.extend(commands_to_append)
        self.session.flush()

    def _process_emited_commands(
        self,
        component: PydanticComponent,
        emmited_commands: t.Iterable[Command] | None,
        commands: CommandQueue,
        during_execute: bool,
    ) -> t.Iterable[ProcessedCommand]:
        component_was_rendered = False
        commands_to_add: list[Command] = []
        for command in emmited_commands or []:
            component_was_rendered = component_was_rendered or (
                isinstance(command, (SkipRender, Render)) and command.component.id == component.id
            )
            if (
                component_was_rendered
                and during_execute
                and isinstance(command, Render)
                and command.lazy is None
            ):
                # make partial updates not lazy during_execute
                command.lazy = False
            commands_to_add.append(command)

        if not component_was_rendered:
            commands_to_add.append(
                Render(component, lazy=False if during_execute else component.lazy)
            )

        if signals := self.update_params_from(component):
            yield PushURL.from_params(self.params)
            commands_to_add.append(Signal(signals))

        commands.extend(commands_to_add)
        self.session.store(component)

    def get_components_subscribed_to(
        self, signals: set[str]
    ) -> t.Iterable[PydanticComponent | Destroy]:
        return (
            self.get_component_by_id(c_id)
            for c_id in sorted(self.session.get_component_ids_subscribed_to(signals))
        )

    def update_params_from(self, component: PydanticComponent) -> set[str]:
        """Updates self.params based on the state of the component

        Return the set of signals that should be triggered as the result of
        the update.

        """
        updated_params: set[str] = set()
        if patchers := _get_query_patchers(component.hx_name):
            for patcher in patchers:
                updated_params.update(
                    patcher.get_updates_for_params(
                        getattr(component, patcher.field_name, None),
                        self.params,
                    )
                )
        return updated_params

    def get_component_by_id(self, component_id: str):
        """Return (possibly build) the component by its ID.

        If the component was already built, get it unchanged, otherwise build
        it from the request's payload and return it.

        If the `component_id` cannot be found, raise a KeyError.

        """
        if state := self.session.get_state(component_id):
            return self.build(state["hx_name"], state, retrieve_state=False)
        else:
            logger.error(
                "Component with id {} not found in session {}", component_id, self.session.id
            )
            return Destroy(component_id)

    def build(self, component_name: str, state: dict[str, t.Any], retrieve_state: bool = True):
        """Build (or update) a component's state."""

        with sentry_span("Repository.build", component_name=component_name):
            # Retrieve state from storage
            if retrieve_state and (component_id := state.get("id")):
                state = (self.session.get_state(component_id) or {}) | state

            # Patch it with whatever is the the GET params if needed
            for patcher in _get_query_patchers(component_name):
                state |= patcher.get_update_for_state(self.params)

            # Inject component name and user
            kwargs = state | {
                "hx_name": component_name,
                "user": None if isinstance(self.user, AnonymousUser) else self.user,
            }
            return REGISTRY[component_name](**kwargs)

    def get_components_by_names(self, *names: str) -> t.Iterable[PydanticComponent]:
        # go over awaken components
        for name in names:
            for state in self.session.get_all_states():
                if state["hx_name"] == name:
                    yield self.build(name, {"id": state["id"]})

    def render_html(
        self,
        component: PydanticComponent,
        oob: str | None = None,
        template: str | None = None,
        lazy: bool | None = None,
    ) -> SafeString:
        lazy = component.lazy if lazy is None else lazy
        with sentry_span(
            "Repository.render_html",
            component_name=component.hx_name,
            oob=str(oob),
            template=str(template),
            lazy=str(lazy),
        ):
            self.session.store(component)

            context = {
                "htmx_repo": self,
                "hx_oob": oob == "true",
                "this": component,
            }

            if lazy:
                template = template or component._template_name_lazy
                context |= {"hx_lazy": True} | component._get_lazy_context()
            else:
                context |= component._get_context()

            html = mark_safe(component._get_template(template)(context).strip())

            # if performing some kind of append, the component has to be wrapped
            if oob and oob != "true":
                html = mark_safe(
                    "".join([
                        format_html('<div hx-swap-oob="{oob}">', oob=oob),
                        html,
                        "</div>",
                    ])
                )
            return html


@dataclass(slots=True)
class Session:
    id: str

    read: bool = False
    is_dirty: bool = False

    # dict[component_id -> state]
    states: dict[str, str] = Field(default_factory=dict)

    # dict[component_id -> set[signals]]
    subscriptions: defaultdict[str, set[str]] = Field(default_factory=lambda: defaultdict(set))

    # set[component_id]
    unregistered: set[str] = Field(default_factory=set)

    def store(self, component: PydanticComponent):
        state = component.model_dump_json()
        if self.states.get(component.id) != state:
            self.states[component.id] = state
            self.is_dirty = True

        subscriptions = component._get_all_subscriptions()
        if self.subscriptions[component.id] != subscriptions:
            self.subscriptions[component.id] = subscriptions
            self.is_dirty = True

    def unregister_component(self, component_id: str):
        self.states.pop(component_id, None)
        self.subscriptions.pop(component_id, None)
        self.unregistered.add(component_id)
        self.is_dirty = True

    def get_state(self, component_id: str) -> dict[str, t.Any] | None:
        self._ensure_read()
        if state := self.states.get(component_id):
            return json.loads(state)

    def get_component_ids_subscribed_to(self, signals: set[str]) -> t.Iterable[str]:
        self._ensure_read()
        for component_id, subscribed_to in self.subscriptions.items():
            if signals.intersection(subscribed_to):
                yield component_id

    def get_all_states(self) -> t.Iterable[dict[str, t.Any]]:
        self._ensure_read()
        return [json.loads(state) for state in self.states.values()]

    def _ensure_read(self):
        if not self.read:
            subscriptions_were_read = False
            for component_id, state in conn.hgetall(f"{self.id}:states").items():  # type: ignore
                component_id = component_id.decode()
                if component_id == "__subs__":
                    # dict[component_id -> list[signals]]
                    for component_id, signals in json.loads(state).items():
                        self.subscriptions[component_id] = set(signals)
                    subscriptions_were_read = True
                else:
                    self.states[component_id] = state.decode()

            # TODO: delete later, backwards compatible method
            if not subscriptions_were_read:
                _, keys = conn.sscan(f"{self.id}:subs")  # type: ignore
                for key in keys:
                    signal, component_id = key.decode().rsplit(":", 1)
                    self.subscriptions[component_id].add(signal)
            self.read = True

    def flush(self, ttl: int = SESSION_TTL):
        if self.is_dirty:
            key = f"{self.id}:states"
            if self.unregistered:
                conn.hdel(key, *self.unregistered)
                self.unregistered.clear()
            if self.states:
                conn.hset(key, mapping=self.states)
            conn.hset(key, "__subs__", json.dumps(self.subscriptions))
            conn.expire(key, ttl)
            # The command MEMORY USAGE is considered slow:
            # https://redis.io/docs/latest/commands/memory-usage/
            #
            # So we perform a trivial sampling with some prob to test the memory usage of the state.
            probe = random.random() <= KEY_SIZE_SAMPLE_PROB
            if probe and isinstance(usage := conn.memory_usage(key), int):
                if KEY_SIZE_ERROR_THRESHOLD and usage > KEY_SIZE_ERROR_THRESHOLD:
                    logger.error(
                        "HTMX session's size (%s) exceeded the size threshold %s",
                        usage,
                        KEY_SIZE_ERROR_THRESHOLD,
                    )
                elif KEY_SIZE_WARN_THRESHOLD and usage > KEY_SIZE_WARN_THRESHOLD:
                    logger.warning(
                        "HTMX session's size (%s) exceeded the size threshold %s",
                        usage,
                        KEY_SIZE_WARN_THRESHOLD,
                    )
            self.is_dirty = False
