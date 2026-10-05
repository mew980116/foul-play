"""Local HTTP sidecar for the online Foul Play reference policy."""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# Add the lab root when this script is started from the tools directory.
LAB_ROOT = Path(__file__).resolve().parents[1]
if str(LAB_ROOT) not in sys.path:
    sys.path.insert(0, str(LAB_ROOT))

from fp.live_reference import LiveReferencePolicy  # noqa: E402


SCHEMA_VERSION = "foul-play-reference/v1"
REFERENCE_CONTRACT_VERSION = "reference-contract/v0.1"
TRANSLATION_VERSION = "ps-adapter-state-sync/v0.4.1"


def normalize(value: Any) -> str:
    return "".join(
        character.lower()
        for character in str(value or "")
        if character.isalnum()
    )


def action_key(action: dict[str, Any], state: dict[str, Any]) -> str | None:
    action_type = action.get("type")
    if action_type == "move":
        move = action.get("id") or action.get("name")
        if not move:
            return None
        return normalize(move) + ("-tera" if action.get("tera") else "")
    if action_type == "switch":
        slot = int(action.get("slot", 0) or 0)
        team = list(state.get("myTeam") or [])
        row = next(
            (
                item
                for item in team
                if int(item.get("slot", 0) or 0) == slot
            ),
            None,
        )
        if row is None and 0 < slot <= len(team):
            row = team[slot - 1]
        name = normalize((row or {}).get("name"))
        return f"switch {name}" if name else f"switch {slot}"
    if action_type == "team":
        return f"team {int(action.get('lead', 0) or 0)}"
    return None


def direct_legal_action(action: dict[str, Any]) -> dict[str, Any]:
    action_type = action.get("type")
    if action_type == "move":
        result = {
            "type": "move",
            "slot": int(action.get("slot", 0) or 0),
        }
        if action.get("tera"):
            result["tera"] = True
        return result
    if action_type == "switch":
        return {"type": "switch", "slot": int(action.get("slot", 0) or 0)}
    if action_type == "team":
        return {"type": "team", "lead": int(action.get("lead", 0) or 0)}
    raise ValueError(f"unsupported legal action type: {action_type}")


def request_forces_switch(state: dict[str, Any]) -> bool:
    request = state.get("request") or {}
    force_switch = request.get("forceSwitch") or ()
    return any(bool(value) for value in force_switch)


def projection_reason(state: dict[str, Any], default: str) -> str:
    if request_forces_switch(state):
        return "forced-switch-projection"
    return default


def choose_legal_action(
    state: dict[str, Any],
    best_action: str,
    action_distribution: dict[str, float] | None = None,
) -> tuple[dict[str, Any], str, bool, str | None]:
    """把 Foul Play 原始动作投影到当前 PS request 的合法动作集合。"""
    raw_target = str(best_action or "").strip().lower()
    want_tera = "-tera" in raw_target
    target = raw_target.replace("-tera", "")
    actions = list(state.get("actions") or [])
    if not target:
        raise ValueError("Foul Play returned an empty best action")

    # PS 在 recharge 期间只接受协议层的 recharge 动作。reference 不能把
    # 这是一个引擎内部的 No Move 再投影为普通招式或换人。
    if any(
        action.get("type") == "move"
        and normalize(action.get("id") or action.get("name")) == "recharge"
        for action in actions
    ):
        recharge_action = next(
            action
            for action in actions
            if action.get("type") == "move"
            and normalize(action.get("id") or action.get("name")) == "recharge"
        )
        return direct_legal_action(recharge_action), "recharge", False, None

    if target.startswith("switch "):
        target_name = normalize(target.split(" ", 1)[1])
        for action in actions:
            if action.get("type") != "switch":
                continue
            if action_key(action, state) == f"switch {target_name}":
                selected = action_key(action, state) or raw_target
                return direct_legal_action(action), selected, False, None
    else:
        target_move = normalize(target.split(" ", 1)[-1])
        for action in actions:
            if action.get("type") != "move":
                continue
            if normalize(action.get("id") or action.get("name")) != target_move:
                continue
            if bool(action.get("tera")) != want_tera:
                continue
            selected = action_key(action, state) or raw_target
            return direct_legal_action(action), selected, False, None

    team_match = re.fullmatch(r"team\s+(\d+)", target)
    if team_match:
        lead = int(team_match.group(1))
        for action in actions:
            if action.get("type") == "team" and int(action.get("lead", 0) or 0) == lead:
                return direct_legal_action(action), f"team {lead}", False, None

    ranked = sorted(
        (action_distribution or {}).items(),
        key=lambda item: item[1],
        reverse=True,
    )
    for candidate, _probability in ranked:
        candidate_target = str(candidate or "").strip().lower().replace("-tera", "")
        for action in actions:
            if action_key(action, state) == candidate_target:
                selected = action_key(action, state) or candidate_target
                return (
                    direct_legal_action(action),
                    selected,
                    True,
                    projection_reason(state, "raw-best-action-not-legal"),
                )

    if actions:
        action = actions[0]
        return (
            direct_legal_action(action),
            action_key(action, state) or "",
            True,
            projection_reason(state, "no-legal-action-in-reference-distribution"),
        )
    raise ValueError("current PS request contains no legal actions")


class ReferenceHandler(BaseHTTPRequestHandler):
    policy: LiveReferencePolicy
    policy_lock = threading.Lock()
    max_body_bytes = 8 * 1024 * 1024

    def _write_json(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._write_json(200, {
                "ok": True,
                "service": "foul-play-live-reference",
                "schemaVersion": SCHEMA_VERSION,
                "referenceContractVersion": REFERENCE_CONTRACT_VERSION,
                "translationVersion": TRANSLATION_VERSION,
                "mode": self.policy.mode,
                "searchTimeMs": self.policy.search_time_ms,
                "particleCount": self.policy.particle_count,
                "searchParallelism": self.policy.search_parallelism,
                "seed": self.policy.seed,
            })
            return
        self._write_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path not in {"/reference", "/choice"}:
            self._write_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > self.max_body_bytes:
            self._write_json(413, {"error": "invalid request size"})
            return
        try:
            state = json.loads(self.rfile.read(length))
            with self.policy_lock:
                result = self.policy.evaluate(state)
            action, selected_action, selection_fallback, fallback_reason = choose_legal_action(
                state,
                result.best_action,
                result.actions,
            )
        except Exception as error:
            self._write_json(400, {"error": str(error)})
            return
        self._write_json(
            200,
            {
                "schemaVersion": SCHEMA_VERSION,
                "referenceContractVersion": REFERENCE_CONTRACT_VERSION,
                "translationVersion": TRANSLATION_VERSION,
                **action,
                "decisionProvider": "foul-play-reference",
                "bestAction": result.best_action,
                "rawBestAction": result.raw_best_action or result.best_action,
                "selectedAction": selected_action,
                "selectionFallback": selection_fallback,
                "selectionFallbackReason": fallback_reason,
                "actions": result.actions,
                "searchTimeMs": result.search_time_ms,
                "particleCount": result.particle_count,
                "referenceMode": result.reference_mode,
                "selectionMode": result.selection_mode,
                "selectionCandidates": list(result.selection_candidates),
                "worldDiagnostics": list(result.world_diagnostics),
                "dynamicSampling": result.dynamic_sampling,
                "searchTimePerWorldMs": result.search_time_per_world_ms,
            },
        )

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8093)
    parser.add_argument("--particles", type=int, default=1)
    parser.add_argument("--search-time-ms", type=int, default=50)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument(
        "--mode",
        choices=("diagnostic", "upstream-compatible"),
        default="diagnostic",
    )
    parser.add_argument("--search-parallelism", type=int, default=1)
    args = parser.parse_args()

    ReferenceHandler.policy = LiveReferencePolicy(
        particle_count=args.particles,
        search_time_ms=args.search_time_ms,
        seed=args.seed,
        mode=args.mode,
        search_parallelism=args.search_parallelism,
    )
    server = ThreadingHTTPServer((args.host, args.port), ReferenceHandler)
    print(
        f"foul-play-live-reference listening on http://{args.host}:{args.port}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
