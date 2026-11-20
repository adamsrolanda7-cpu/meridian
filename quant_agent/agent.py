"""Boucle d'agent : provider -> outils -> vérification -> réponse.

L'agent ne calcule aucun chiffre lui-même. Il peut seulement :
- appeler un outil autorisé ;
- lire le résultat ;
- répondre avec les run_id et les sources.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from meridian.config import ROOT

from .jsonutil import to_jsonable
from .policy import Policy
from .providers import Provider, ProviderError
from .quotas import QuotaManager
from .tools import ToolRegistry


@dataclass
class ToolTrace:
    name: str
    arguments: dict[str, Any]
    ok: bool
    duration_ms: int
    error: str | None = None
    run_ids: list[str] = field(default_factory=list)


@dataclass
class TurnResult:
    answer: str
    session_id: str
    traces: list[ToolTrace] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)
    tokens: int = 0
    provider: str = "mock"
    mode: str = "kraken"


class Agent:
    def __init__(
        self,
        provider: Provider,
        tools: ToolRegistry,
        *,
        policy: Policy | None = None,
        quotas: QuotaManager | None = None,
        root: str | None = None,
    ):
        self.provider = provider
        self.tools = tools
        self.policy = policy or Policy()
        self.quotas = quotas or QuotaManager()
        self.root = root or ROOT
        self.audit_path = os.path.join(self.root, "runs", "agent", "audit.jsonl")

    def ask(
        self,
        text: str,
        *,
        history: list[dict[str, Any]] | None = None,
        session_id: str | None = None,
    ) -> TurnResult:
        session_id = session_id or uuid.uuid4().hex[:8]
        self.quotas.check("messages")
        self.quotas.check("tokens")
        self.provider.begin_turn(text)

        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": self.policy.system_prompt(self.quotas.snapshot()),
            }
        ]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": text})

        traces: list[ToolTrace] = []
        tool_calls_used = 0
        tokens_used = 0
        answer = ""
        started = time.monotonic()

        try:
            for _step in range(self.policy.max_steps):
                if time.monotonic() - started > self.policy.max_seconds:
                    answer = (
                        "Je m'arrête : le budget de temps de la politique "
                        f"({int(self.policy.max_seconds)} s) est dépassé. "
                        "Réduis la demande ou relance avec un mode plus large."
                    )
                    break
                try:
                    response = self.provider.complete(messages, self.tools.schemas())
                except ProviderError as exc:
                    answer = (
                        "Le provider de modèle a échoué : "
                        f"{exc}\n\nLe CLI reste utilisable en provider mock "
                        "(défaut) ou avec un autre modèle."
                    )
                    break

                usage = response.usage or {}
                try:
                    tokens = int(usage.get("total_tokens") or 0)
                except (TypeError, ValueError):
                    tokens = 0
                if tokens:
                    tokens_used += tokens
                    self.quotas.add_tokens(tokens)

                if not response.tool_calls:
                    answer = (response.text or "").strip() or "Je n'ai pas de réponse."
                    break

                assistant_calls = [
                    {
                        "id": call.id,
                        "name": call.name,
                        "arguments": to_jsonable(call.arguments),
                    }
                    for call in response.tool_calls
                ]
                messages.append(
                    {
                        "role": "assistant",
                        "content": response.text or "",
                        "tool_calls": assistant_calls,
                    }
                )

                for call in response.tool_calls:
                    if tool_calls_used >= self.policy.max_tool_calls:
                        result = {
                            "ok": False,
                            "tool": call.name,
                            "error": (
                                "budget d'outils atteint "
                                f"({self.policy.max_tool_calls}). "
                                "Réduis la demande ou utilise le mode deep."
                            ),
                            "duration_ms": 0,
                        }
                    elif not self.policy.can_call_tool(call.name):
                        result = {
                            "ok": False,
                            "tool": call.name,
                            "error": f"outil non autorisé par la politique : {call.name}",
                            "duration_ms": 0,
                        }
                    else:
                        result = self.tools.call(call.name, call.arguments)
                    tool_calls_used += 1

                    run_ids = self._extract_run_ids(result)
                    trace = ToolTrace(
                        name=call.name,
                        arguments=to_jsonable(call.arguments),
                        ok=bool(result.get("ok")),
                        duration_ms=int(result.get("duration_ms") or 0),
                        error=None if result.get("ok") else str(result.get("error")),
                        run_ids=run_ids,
                    )
                    traces.append(trace)
                    self._audit(session_id, trace)

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "name": call.name,
                            "content": json.dumps(
                                to_jsonable(result), ensure_ascii=False
                            ),
                        }
                    )
            else:
                answer = (
                    "Je m'arrête : la limite d'étapes de la politique est "
                    "atteinte sans réponse finale. Reformule ou utilise un "
                    "mode plus large."
                )
        finally:
            self.quotas.consume("messages")

        run_ids: list[str] = []
        for trace in traces:
            for run_id in trace.run_ids:
                if run_id not in run_ids:
                    run_ids.append(run_id)

        return TurnResult(
            answer=answer,
            session_id=session_id,
            traces=traces,
            run_ids=run_ids,
            tokens=tokens_used,
            provider=getattr(self.provider, "name", "unknown"),
            mode=self.policy.name,
        )

    @staticmethod
    def _extract_run_ids(result: dict[str, Any]) -> list[str]:
        data = result.get("data") or {}
        if not isinstance(data, dict):
            return []
        ids: list[str] = []
        if isinstance(data.get("run_id"), str):
            ids.append(data["run_id"])
        runs = data.get("runs")
        if isinstance(runs, list):
            for run in runs:
                if isinstance(run, dict) and isinstance(run.get("run_id"), str):
                    ids.append(run["run_id"])
        return [item for item in ids if item.startswith("bt_")]

    def _audit(self, session_id: str, trace: ToolTrace) -> None:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "session_id": session_id,
            "tool": trace.name,
            "arguments": trace.arguments,
            "ok": trace.ok,
            "duration_ms": trace.duration_ms,
            "error": trace.error,
            "run_ids": trace.run_ids,
            "mode": self.policy.name,
            "provider": getattr(self.provider, "name", "unknown"),
        }
        try:
            os.makedirs(os.path.dirname(self.audit_path), exist_ok=True)
            with open(self.audit_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            # L'audit ne doit pas casser une réponse déjà calculée.
            pass
