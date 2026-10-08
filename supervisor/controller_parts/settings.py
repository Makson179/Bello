"""Settings service and its explicitly owned per-run state.

Only the declared port can reach the coordinator. Own state is accessed directly;
cross-service operations go through replaceable coordinator callbacks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import compat
from .interfaces import CoordinatorPort


@dataclass(init=False, slots=True)
class SettingsState:
    """Unset fields intentionally remain absent for legacy __new__ construction."""
    pass


class SettingsPort(CoordinatorPort):
    __slots__ = ()
    reads = frozenset({
        '_active_dependency_roots',
        '_active_task_path',
        '_active_workspace_root',
        '_adv_report_controller_agent',
        '_adversary_enabled_for_config',
        '_adversary_intelligence',
        '_adversary_model',
        '_adversary_model_required_for_preflight',
        '_adversary_multi_agent_config',
        '_async_tools_enabled',
        '_canonical_task_text',
        '_cheap_runtime_enabled',
        '_cleanup_preflight_probe_thread',
        '_coder_intelligence',
        '_coder_model',
        '_completion_intelligence',
        '_completion_model',
        '_completion_multi_agent_config',
        '_completion_supervisor_agent',
        '_configure_runtime_triage',
        '_effective_completion_review',
        '_effective_max_adversary_runs',
        '_enabled_subagent_models_for_preflight',
        '_ensure_selected_models_available',
        '_fast_mode',
        '_generate_schema_hash',
        '_generate_schema_hash_async',
        '_log_distiller_config',
        '_multi_agent_config',
        '_persist_model_config',
        '_post_coder_review_enabled',
        '_report_alias_resolution',
        '_revision_coder_active',
        '_revision_coder_enabled',
        '_revision_coder_intelligence',
        '_revision_coder_model',
        '_runtime_enabled',
        '_runtime_intelligence',
        '_runtime_model',
        '_runtime_preflight',
        '_structured_output_self_test',
        '_windows_native_root_read_enabled',
        'adv_report_controller',
        'adversary_enabled',
        'adversary_intelligence',
        'adversary_model',
        'async_tools',
        'clean_workspace',
        'client',
        'coder_intelligence',
        'coder_model',
        'completion_intelligence',
        'completion_model',
        'completion_review',
        'completion_supervisor',
        'declared_grading_roots',
        'fast',
        'finalize',
        'log_distiller',
        'model',
        'overwrite_state',
        'project_config',
        'project_root',
        'runtime_enabled',
        'runtime_intelligence',
        'runtime_model',
        'store',
        'supervisor',
        'supervisor_intelligence',
        'supervisor_model',
        'task_path',
        'tui',
    })
    writes = frozenset({
    })


class Settings:
    """Own settings behavior; borrow only the declared port."""

    def __init__(self, ports: SettingsPort) -> None:
        self.state = SettingsState()
        self.ports = ports

    def _coder_model(self) -> str | None:
        return getattr(self.ports, "coder_model", getattr(self.ports, "model", compat.DEFAULT_MODEL))

    def _revision_coder_enabled(self) -> bool:
        project_config = getattr(self.ports, "project_config", None)
        if project_config is not None:
            return bool(project_config.revision_coder_enabled)
        try:
            return bool(self.ports.store.get_bello_config().revision_coder_enabled)
        except Exception:
            return False

    def _revision_coder_model(self) -> str | None:
        project_config = getattr(self.ports, "project_config", None)
        if project_config is not None:
            return project_config.revision_coder_mod
        try:
            return self.ports.store.get_bello_config().revision_coder_mod or self.ports._coder_model()
        except Exception:
            return self.ports._coder_model()

    def _revision_coder_active(self) -> bool:
        try:
            return bool(self.ports.store.get_bello_config().revision_coder_active)
        except Exception:
            return False

    def _active_coder_model(self) -> str | None:
        if self.ports._revision_coder_active():
            return self.ports._revision_coder_model()
        return self.ports._coder_model()

    def _runtime_model(self) -> str | None:
        return getattr(self.ports, "runtime_model", getattr(self.ports, "supervisor_model", getattr(self.ports, "model", compat.DEFAULT_MODEL)))

    def _supervisor_model(self) -> str | None:
        return self.ports._runtime_model()

    def _completion_model(self) -> str | None:
        return getattr(self.ports, "completion_model", getattr(self.ports, "supervisor_model", getattr(self.ports, "model", compat.DEFAULT_MODEL)))

    def _adversary_model(self) -> str:
        return getattr(self.ports, "adversary_model", compat.ADVERSARY_MODEL)

    def _fast_mode(self) -> bool:
        return bool(getattr(self.ports, "fast", False))

    def _cheap_runtime_enabled(self) -> bool:
        if not self.ports._runtime_enabled():
            return False
        try:
            return bool(self.ports.store.get_bello_config().cheap_runtime)
        except Exception:
            project_config = getattr(self.ports, "project_config", None)
            return bool(project_config.cheap_runtime) if project_config is not None else True

    def _runtime_enabled(self) -> bool:
        override = getattr(self.ports, "runtime_enabled", None)
        if override is not None:
            return bool(override)
        config = getattr(self.ports, "project_config", None)
        if config is not None:
            return bool(getattr(config, "runtime_enabled", True))
        try:
            return bool(self.ports.store.get_bello_config().runtime_enabled)
        except Exception:
            return True

    def _post_coder_review_enabled(self) -> bool:
        return self.ports._effective_completion_review() or self.ports._adversary_model_required_for_preflight()

    def _async_tools_enabled(self) -> bool:
        override = getattr(self.ports, "async_tools", None)
        if override is not None:
            return bool(override)
        config = getattr(self.ports, "project_config", None)
        if config is not None:
            return bool(getattr(config, "async_tools", False))
        return bool(self.ports.store.read_json(compat.CONFIG, {}).get("async_tools", False))

    def _log_distiller_config(self) -> compat.LogDistillerConfig:
        override = getattr(self.ports, "log_distiller", None)
        if override is not None:
            return override
        config = getattr(self.ports, "project_config", None)
        if config is not None:
            return config.log_distiller
        try:
            saved = self.ports.store.read_json(compat.CONFIG, {})
            return compat.LogDistillerConfig(**saved.get("log_distiller", {}))
        except (AttributeError, FileNotFoundError):
            return compat.LogDistillerConfig()

    def _windows_native_root_read_enabled(self) -> bool:
        config = getattr(self.ports, "project_config", None)
        value = (getattr(config, "windows_native_root_read", False) if config is not None
                 else self.ports.store.read_json(compat.CONFIG, {}).get("windows_native_root_read", False))
        if type(value) is not bool:
            raise ValueError("windows_native_root_read must be a boolean")
        return value

    def _effective_completion_review(self) -> bool:
        """Whether the completion review gate is active for this run.

        CLI override wins; otherwise the persisted project-config mirror. With the gate
        off, the independently configured adversary can still review coder readiness.
        """
        override = getattr(self.ports, "completion_review", None)
        if override is not None:
            return bool(override)
        try:
            return bool(self.ports.store.get_bello_config().completion_review_enabled)
        except Exception:
            project_config = getattr(self.ports, "project_config", None)
            if project_config is not None:
                return bool(project_config.completion_review)
            return True

    def _adversary_enabled_for_config(self) -> bool:
        enabled = getattr(self.ports, "adversary_enabled", None)
        if enabled is False:
            return False
        return True

    def _configured_adversary_runs(self, project_config: compat.ProjectConfig) -> int:
        """Adversary pass budget persisted to the run config. Mirrors the project file only —
        CLI overrides (adversary_enabled / adversary_runs) stay runtime-scoped and are applied
        in _effective_max_adversary_runs, matching how the other run settings behave."""
        return max(0, project_config.adversary_runs) if project_config.adversary else 0

    def _project_config_for_persistence(self) -> compat.ProjectConfig:
        config = getattr(self.ports, "project_config", None)
        if config is not None:
            return config
        return compat.ProjectConfig(
            task=compat._workspace_display_path(self.ports.project_root, str(self.ports.task_path)),
            coder_mod=self.ports._coder_model() or compat.DEFAULT_MODEL,
            runtime_mod=self.ports._runtime_model() or compat.DEFAULT_MODEL,
            completion_mod=self.ports._completion_model() or compat.DEFAULT_MODEL,
            adversary_mod=self.ports._adversary_model(),
            coder_intelligence=self.ports._coder_intelligence() or compat.DEFAULT_INTELLIGENCE,
            runtime_intelligence=self.ports._runtime_intelligence() or compat.DEFAULT_INTELLIGENCE,
            completion_intelligence=self.ports._completion_intelligence() or compat.DEFAULT_INTELLIGENCE,
            adversary_intelligence=self.ports._adversary_intelligence() or compat.DEFAULT_INTELLIGENCE,
            speed="fast" if self.ports._fast_mode() else "usual",
            runtime_enabled=self.ports._runtime_enabled(),
            log_distiller=self.ports._log_distiller_config(),
            async_tools=self.ports._async_tools_enabled(),
            windows_native_root_read=self.ports._windows_native_root_read_enabled(),
            start_over=self.ports.overwrite_state,
            adversary=self.ports._adversary_enabled_for_config(),
            clean=self.ports.clean_workspace,
            protected_path=tuple(compat._workspace_display_path(self.ports.project_root, path) for path in self.ports.declared_grading_roots),
        )

    def _runtime_settings_summary(self) -> str:
        protected_paths = (
            ", ".join(compat._workspace_display_path(self.ports.project_root, path) for path in self.ports.declared_grading_roots)
            if self.ports.declared_grading_roots
            else "absent"
        )
        speed = "fast" if self.ports._fast_mode() else "usual"
        multi_agent_summary = compat._format_multi_agent_summary(self.ports._multi_agent_config())
        completion_multi_agent_summary = compat._format_multi_agent_summary(
            self.ports._completion_multi_agent_config()
        )
        adversary_multi_agent_summary = compat._format_multi_agent_summary(
            self.ports._adversary_multi_agent_config()
        )
        revision_coder_summary = "off"
        if self.ports._revision_coder_enabled():
            revision_coder_summary = (
                f"on({self.ports._revision_coder_model()}/{self.ports._revision_coder_intelligence()})"
            )
        return (
            "settings: "
            f"task={compat._workspace_display_path(self.ports.project_root, str(self.ports.task_path))} "
            f"coder-mod={self.ports._coder_model()} "
            f"runtime-mod={self.ports._runtime_model()} "
            f"completion-mod={self.ports._completion_model()} "
            f"adversary-mod={self.ports._adversary_model()} "
            f"coder-intelligence={self.ports._coder_intelligence()} "
            f"revision-coder={revision_coder_summary} "
            f"runtime-intelligence={self.ports._runtime_intelligence()} "
            f"completion-intelligence={self.ports._completion_intelligence()} "
            f"adversary-intelligence={self.ports._adversary_intelligence()} "
            f"speed={speed} "
            f"runtime={compat._format_bool(self.ports._runtime_enabled())} "
            f"async-tools={compat._format_bool(self.ports._async_tools_enabled())} "
            f"windows-native-root-read={compat._format_bool(self.ports._windows_native_root_read_enabled())} "
            f"log-distiller={compat._format_bool(self.ports._log_distiller_config().enabled)} "
            f"cheap-runtime={compat._format_bool(self.ports._cheap_runtime_enabled())} "
            f"multi-agent={multi_agent_summary} "
            f"completion-multi-agent={completion_multi_agent_summary} "
            f"adversary-multi-agent={adversary_multi_agent_summary} "
            f"start-over={compat._format_bool(self.ports.overwrite_state)} "
            f"clean={compat._format_bool(self.ports.clean_workspace)} "
            f"completion-review={compat._format_bool(self.ports._effective_completion_review())} "
            f"adversary={compat._format_bool(self.ports._effective_max_adversary_runs() > 0)} "
            f"protected-path={protected_paths}"
        )

    def _coder_intelligence(self) -> str | None:
        return getattr(self.ports, "coder_intelligence", compat.DEFAULT_INTELLIGENCE)

    def _revision_coder_intelligence(self) -> str | None:
        project_config = getattr(self.ports, "project_config", None)
        if project_config is not None:
            return project_config.revision_coder_intelligence
        try:
            return self.ports.store.get_bello_config().revision_coder_intelligence or self.ports._coder_intelligence()
        except Exception:
            return self.ports._coder_intelligence()

    def _active_coder_intelligence(self) -> str | None:
        if self.ports._revision_coder_active():
            return self.ports._revision_coder_intelligence()
        return self.ports._coder_intelligence()

    def _runtime_intelligence(self) -> str | None:
        return getattr(self.ports, "runtime_intelligence", getattr(self.ports, "supervisor_intelligence", compat.DEFAULT_INTELLIGENCE))

    def _supervisor_intelligence(self) -> str | None:
        return self.ports._runtime_intelligence()

    def _completion_intelligence(self) -> str | None:
        return getattr(
            self.ports,
            "completion_intelligence",
            getattr(self.ports, "supervisor_intelligence", compat.DEFAULT_INTELLIGENCE),
        )

    def _adversary_intelligence(self) -> str | None:
        return getattr(self.ports, "adversary_intelligence", compat.DEFAULT_INTELLIGENCE)

    def _completion_supervisor_agent(self) -> compat.StatelessSupervisorAgent | None:
        return getattr(self.ports, "completion_supervisor", None) or getattr(self.ports, "supervisor", None)

    def _post_coder_review_agent(self) -> compat.StatelessSupervisorAgent | None:
        if self.ports._effective_completion_review():
            return self.ports._completion_supervisor_agent()
        return self.ports._adv_report_controller_agent()

    def _adv_report_controller_agent(self) -> compat.StatelessSupervisorAgent | None:
        return getattr(self.ports, "adv_report_controller", None)

    async def preflight(self) -> None:
        if isinstance(self.ports.client, compat.RuntimeClient):
            await self.ports._runtime_preflight()
            return
        self.ports.tui.status("checking Codex version")
        codex = compat._controller_executable("codex", self.ports.project_root)
        if codex is None:
            raise RuntimeError("trusted codex executable not found")
        version = compat._run_probe([codex, "--version"])[1]
        self.ports.tui.status("checking Codex app-server schema")
        schema_hash = await self.ports._generate_schema_hash_async()
        self.ports.store.update_bello_config(
            lambda cfg: cfg.model_copy(update={"codex_version": version, "appserver_schema_hash": schema_hash})
        )
        self.ports.tui.status("checking Codex account")
        account = await self.ports.client.account_read()
        if account.get("requiresOpenaiAuth") and account.get("account") is None:
            raise RuntimeError("Codex auth missing. Run `codex login` before starting Bello.")
        self.ports.tui.status("checking Codex rate limits")
        try:
            await self.ports.client.account_rate_limits_read()
        except Exception as exc:
            warning = f"Codex rate limit check unavailable; continuing: {exc}"
            self.ports.tui.render("SYSTEM", warning)
            self.ports.store.append_raw_log(
                {
                    "timestamp": compat.datetime.now(compat.timezone.utc).isoformat(),
                    "type": "preflight_warning",
                    "check": "codex_rate_limits",
                    "error_type": exc.__class__.__name__,
                    "error": str(exc),
                }
            )
        self.ports.tui.status("checking available models")
        models_response = await self.ports.client.model_list()
        durable = getattr(self.ports, "_durable_run", None)
        if durable is not None:
            durable.verify_engine_selection()
        self.ports._persist_model_config()
        await self.ports._ensure_selected_models_available(models_response)
        if self.ports.store.get_bello_config().status == compat.BelloStatus.PROVIDER_FAILURE:
            return
        if self.ports._runtime_enabled():
            self.ports.tui.status("checking supervisor structured output")
            await self.ports._structured_output_self_test()
            await self.ports._configure_runtime_triage()
        self.ports.tui.status("checking config requirements")
        await self.ports.client.config_requirements_read()
        self.ports.tui.status("checking coder sandbox and approval settings")
        thread = await self.ports.client.thread_start(
            compat.coder_thread_params(
                self.ports._active_workspace_root(),
                task_path=self.ports._active_task_path(),
                readonly_roots=self.ports._active_dependency_roots(),
                model=self.ports._coder_model(),
                intelligence=self.ports._coder_intelligence(),
                fast=self.ports._fast_mode(),
                multi_agent=self.ports._multi_agent_config(),
            )
        )
        approval_policy = thread.get("approvalPolicy")
        sandbox = thread.get("sandbox")
        thread_id = thread.get("thread", {}).get("id") if isinstance(thread.get("thread"), dict) else None
        if approval_policy != "on-request":
            raise RuntimeError("app-server did not accept on-request coder approval policy")
        expected_sandbox = compat.coder_sandbox_mode()
        if not compat._sandbox_matches_mode(
            sandbox,
            expected_sandbox,
            workspace_root=self.ports._active_workspace_root(),
        ):
            raise RuntimeError(f"app-server did not accept {expected_sandbox} coder sandbox")
        if isinstance(thread_id, str):
            await self.ports._cleanup_preflight_probe_thread(thread_id)

    async def _runtime_preflight(self) -> None:
        from supervisor.runtime.models import parse_model_selection
        from supervisor.runtime.sandbox import SandboxPolicy, SandboxRunner
        self.ports.tui.status("checking Bello execution engines and configured models")
        selected = [self.ports._coder_model()]
        if self.ports._runtime_enabled():
            selected.append(self.ports._runtime_model())
        if self.ports._effective_completion_review():
            selected.append(self.ports._completion_model())
        if self.ports._post_coder_review_enabled() and self.ports._revision_coder_enabled():
            selected.append(self.ports._revision_coder_model())
        if self.ports._adversary_model_required_for_preflight():
            selected.append(self.ports._adversary_model())
        selected.extend(self.ports._enabled_subagent_models_for_preflight())
        self.ports.client.required_models = tuple(dict.fromkeys(selected))
        prepare_engines = getattr(self.ports.client, "prepare_engines", None)
        if callable(prepare_engines):
            if any(model.startswith("claude-code/") for model in self.ports.client.required_models):
                self.ports.tui.status("checking the official Claude Code CLI (one-time verified download if needed)")
            await prepare_engines(self.ports.client.required_models)
        models = await self.ports.client.model_list()
        durable = getattr(self.ports, "_durable_run", None)
        if durable is not None:
            durable.verify_engine_selection()
        self.ports.store.update_bello_config(lambda cfg: cfg.model_copy(update={
            "runtime_name": "bello-codex/pi/claude-code", "runtime_protocol_version": 1,
        }))
        self.ports._persist_model_config()
        await self.ports._ensure_selected_models_available(models)
        if self.ports.store.get_bello_config().status == compat.BelloStatus.PROVIDER_FAILURE:
            return
        self.ports._report_alias_resolution(models)
        # The same enumeration `bello config` uses for its offline checks.
        completion = self.ports._effective_completion_review()
        adversary = self.ports._adversary_model_required_for_preflight()
        policies = [("multi_agent", self.ports._multi_agent_config())]
        if completion:
            policies.append(("completion_multi_agent", self.ports._completion_multi_agent_config()))
        if adversary:
            policies.append(("adversary_multi_agent", self.ports._adversary_multi_agent_config()))
        uses = compat.preflight_profiles(
            coder=(self.ports._coder_model(), self.ports._coder_intelligence()),
            completion=(self.ports._completion_model(), self.ports._completion_intelligence()) if completion else None,
            revision_coder=((self.ports._revision_coder_model(), self.ports._revision_coder_intelligence())
                            if self.ports._post_coder_review_enabled() and self.ports._revision_coder_enabled() else None),
            adversary=(self.ports._adversary_model(), self.ports._adversary_intelligence()) if adversary else None,
            runtime=(self.ports._runtime_model(), self.ports._runtime_intelligence()) if self.ports._runtime_enabled() else None,
            policies=policies,
        )
        profiles = [(use.model, use.effort) for use in uses]
        async_profiles = {(use.model, use.effort) for use in uses if use.role != "runtime"}
        distilled_models = set()
        if self.ports._log_distiller_config().enabled:
            distilled_models.add(compat.parse_model_selection(self.ports._coder_model()).qualified)
            if self.ports._post_coder_review_enabled() and self.ports._revision_coder_enabled():
                distilled_models.add(compat.parse_model_selection(self.ports._revision_coder_model()).qualified)
            coder_agents = self.ports._multi_agent_config()
            if coder_agents.enabled:
                distilled_models.update(compat.parse_model_selection(model).qualified for model in coder_agents.allowed)
        for model, effort in dict.fromkeys(profiles):
            selection = compat.parse_model_selection(model)
            request = {
                # A `default` profile (a model advertising no effort) sends none.
                "model": model, "effort": compat.engine_effort(effort),
                "serviceTier": "priority" if self.ports._fast_mode() else None,
            }
            if (model, effort) not in async_profiles:
                # A model used only for bounded runtime supervision does not
                # need the event-driven coder/reviewer loop capability.
                request["belloRole"] = "runtime"
            if selection.engine == "codex" and selection.qualified in distilled_models:
                # Check native D capability before runtime's paid startup probe,
                # but do not require a patched binary for Codex reviewers alone.
                request["distillerEnabled"] = True
            validation = await self.ports.client.request("model/validate", request)
            if validation.get("valid") is not True:
                raise RuntimeError(f"execution engine could not validate the exact model profile: {model} / {effort}")
        self.ports.tui.status("checking the operating-system sandbox")
        root = self.ports._active_workspace_root()
        probe = await SandboxRunner(SandboxPolicy(
            root=root, mode=compat.coder_sandbox_mode(), readable_roots=self.ports._active_dependency_roots(),
        )).run(
            "echo bello-sandbox-probe", root, 10
        )
        if probe.exit_code != 0 or "bello-sandbox-probe" not in probe.output:
            raise RuntimeError("Bello could not start its required OS sandbox; no model run was started")
        if self.ports._runtime_enabled():
            self.ports.tui.status("checking supervisor structured output")
            await self.ports._structured_output_self_test()
            await self.ports._configure_runtime_triage()

    def _report_alias_resolution(self, models_response: dict[str, compat.Any]) -> None:
        """Make floating-alias resolution visible; the saved alias is not changed."""
        render = getattr(self.ports.tui, "render", None)
        selected = set(getattr(self.ports.client, "required_models", ()) or ())
        data = models_response.get("data") if isinstance(models_response, dict) else None
        if not callable(render) or not isinstance(data, list):
            return
        for item in data:
            if not isinstance(item, dict) or item.get("alias") is not True:
                continue
            qualified, resolved = item.get("qualifiedId"), item.get("resolvedModel")
            if qualified in selected and isinstance(resolved, str) and resolved:
                selected.discard(qualified)  # RuntimeClient lists each entry twice
                render("SYSTEM", f"{qualified} is a floating Claude Code alias; this run uses "
                                 f"{resolved}, as the CLI resolves it now")

    async def _ensure_selected_models_available(self, models_response: dict[str, compat.Any]) -> None:
        result = compat._selected_model_availability(
            models_response,
            coder_model=self.ports._coder_model(),
            revision_coder_model=(
                self.ports._revision_coder_model()
                if self.ports._revision_coder_enabled() and self.ports._post_coder_review_enabled()
                else None
            ),
            runtime_model=self.ports._runtime_model() if self.ports._runtime_enabled() else None,
            completion_model=self.ports._completion_model() if self.ports._effective_completion_review() else None,
            adversary_model=self.ports._adversary_model() if self.ports._adversary_model_required_for_preflight() else None,
            subagent_models=self.ports._enabled_subagent_models_for_preflight(),
        )
        if result.ok:
            return
        readable = compat._readable_available_models(models_response)
        available = ", ".join(readable) if readable else "none reported"
        missing = ", ".join(result.missing_roles)
        hint = (" Choose one with `bello config` or a --<role>-mod provider/model flag; "
                "Bello does not substitute a model." if readable != result.available_models else "")
        message = (
            "model availability preflight failed before coder start: "
            f"selected model(s) are not available from the execution engine: {missing}. "
            f"Available models: {available}.{hint} "
            "The interruption is recorded in .supervisor/FINAL_REPORT.md."
        )
        self.ports.store.append_text_locked(compat.PROGRESS, f"- {message}\n")
        await self.ports.finalize(message, status=compat.BelloStatus.PROVIDER_FAILURE)

    def _enabled_subagent_models_for_preflight(self) -> tuple[str, ...]:
        policies = [self.ports._multi_agent_config()]
        if self.ports._effective_completion_review():
            policies.append(self.ports._completion_multi_agent_config())
        if self.ports._adversary_model_required_for_preflight():
            policies.append(self.ports._adversary_multi_agent_config())
        models: list[str] = []
        for policy in policies:
            if not getattr(policy, "enabled", False):
                continue
            for model in getattr(policy, "allowed", {}):
                if model not in models:
                    models.append(model)
        return tuple(models)

    def _adversary_model_required_for_preflight(self) -> bool:
        enabled = getattr(self.ports, "adversary_enabled", None)
        if enabled is False:
            return False
        if enabled is True:
            return True
        return self.ports.store.get_bello_config().max_adversary_runs > 0

    def _generate_schema_hash(self) -> str:
        codex = compat._controller_executable("codex", self.ports.project_root)
        if codex is None:
            raise RuntimeError("codex executable not found")
        with compat.tempfile.TemporaryDirectory(prefix="bello-appserver-schema-") as tmp_dir:
            out_dir = compat.Path(tmp_dir)
            completed = compat.subprocess.run(
                [codex, "app-server", "generate-json-schema", "--experimental", "--out", str(out_dir)],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError((completed.stdout + completed.stderr).strip() or "app-server schema generation failed")
            required = ["ClientRequest.json", "ServerRequest.json", "TurnStartParams.json", "CommandExecutionRequestApprovalParams.json"]
            for rel in required:
                if not compat._schema_file_exists(out_dir, rel):
                    raise RuntimeError(f"app-server schema missing required file: {rel}")
            if not compat._turn_start_schema_supports_effort(out_dir):
                raise RuntimeError("app-server schema missing required turn effort field for Bello intelligence settings")
            digest = compat.hashlib.sha256()
            for path in sorted(out_dir.rglob("*.json")):
                digest.update(str(path.relative_to(out_dir)).encode("utf-8"))
                digest.update(path.read_bytes())
            return digest.hexdigest()

    async def _generate_schema_hash_async(self) -> str:
        return await compat.asyncio.to_thread(self.ports._generate_schema_hash)

    async def _structured_output_self_test(self) -> None:
        if not self.ports._runtime_enabled():
            return
        agent = compat.StatelessSupervisorAgent(
            self.ports.client,
            self.ports.store,
            self.ports.task_path,
            workspace_root=self.ports._active_workspace_root(),
            task_contents=self.ports._canonical_task_text(),
            model=self.ports._runtime_model(),
            fast=self.ports._fast_mode(),
            intelligence=self.ports._runtime_intelligence(),
        )
        cfg = self.ports.store.get_bello_config()
        packet = compat.SupervisorWakePacket(
            wake_sequence=1,
            latest_event_sequence=cfg.last_event_sequence,
            generation=cfg.generation,
            restart_count=cfg.restart_count,
            task_path=str(self.ports.task_path),
            task_contents="Structured output self-test. Return noop.",
            progress="",
            decisions="",
            last_actions=[],
            health=self.ports.store.get_health().model_dump(mode="json"),
            recent_events=[],
            current_summary="Startup structured-output self-test. Return decision noop.",
            coder_thread_id=None,
            active_coder_turn_id=None,
        )
        decision = await compat.asyncio.wait_for(agent.decide(packet), timeout=240)
        if decision.decision not in {compat.SupervisorDecisionKind.NOOP, compat.SupervisorDecisionKind.PAUSE}:
            raise RuntimeError("structured-output supervisor self-test returned an unexpected decision")
