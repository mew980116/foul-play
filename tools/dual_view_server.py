"""Local HTTP service for the online DualView decision provider."""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

LAB_ROOT = Path(__file__).resolve().parents[1]
if str(LAB_ROOT) not in sys.path:
    sys.path.insert(0, str(LAB_ROOT))

from fp.live_reference import LiveDualViewPolicy  # noqa: E402


def normalize(value: Any) -> str:
    return "".join(character.lower() for character in str(value or "") if character.isalnum())


def _action_key(state: dict[str, Any], action: dict[str, Any]) -> str | None:
    action_type = action.get("type")
    if action_type == "move":
        key = normalize(action.get("id") or action.get("name"))
        return key + ("-tera" if action.get("tera") else "")
    if action_type == "switch":
        slot = int(action.get("slot", 0) or 0)
        return f"switch {slot}"
    if action_type == "team":
        return f"team {int(action.get('lead', 0) or 0)}"
    return None


def _direct_legal_action(action: dict[str, Any]) -> dict[str, Any]:
    if action.get("type") == "move":
        result = {"type": "move", "slot": int(action.get("slot", 0) or 0)}
        if action.get("tera"):
            result["tera"] = True
        return result
    if action.get("type") == "switch":
        return {"type": "switch", "slot": int(action.get("slot", 0) or 0)}
    if action.get("type") == "team":
        return {"type": "team", "lead": int(action.get("lead", 0) or 0)}
    raise ValueError(f"unsupported legal action type: {action.get('type')}")


def _request_forces_switch(state: dict[str, Any]) -> bool:
    request = state.get("request") or {}
    force_switch = request.get("forceSwitch") or ()
    return any(bool(value) for value in force_switch)


def _projection_reason(state: dict[str, Any], default: str) -> str:
    if _request_forces_switch(state):
        return "forced-switch-projection"
    return default


def choose_legal_action(
    state: dict[str, Any],
    best_action: str,
    action_distribution: dict[str, float] | None = None,
    allow_fallback: bool = True,
) -> tuple[dict[str, Any], str, bool, str | None]:
    """将 Foul Play 的根节点动作名映射为 PS request 中的合法动作。"""
    raw_target = str(best_action or "").strip().lower()
    want_tera = "-tera" in raw_target
    target = raw_target.replace("-tera", "")
    actions = list(state.get("actions") or [])
    if not target:
        raise ValueError("DualView returned an empty best action")

    if target.startswith("switch "):
        target_name = normalize(target.split(" ", 1)[1])
        team = list(state.get("myTeam") or [])
        for action in actions:
            if action.get("type") != "switch":
                continue
            slot = int(action.get("slot", 0) or 0)
            row = next(
                (
                    candidate
                    for candidate in team
                    if int(candidate.get("slot", 0) or 0) == slot
                ),
                None,
            )
            if row is None and 0 < slot <= len(team):
                row = team[slot - 1]
            row = row or {}
            if normalize(row.get("name")) == target_name:
                return {"type": "switch", "slot": slot}, raw_target, False, None
    else:
        target_move = normalize(target.split(" ", 1)[-1])
        for action in actions:
            if action.get("type") != "move":
                continue
            if normalize(action.get("id") or action.get("name")) != target_move:
                continue
            if bool(action.get("tera")) != want_tera:
                continue
            result = {"type": "move", "slot": int(action.get("slot", 0) or 0)}
            if action.get("tera"):
                result["tera"] = True
            return result, raw_target, False, None

    team_match = re.fullmatch(r"team\s+(\d+)", target)
    if team_match:
        return (
            {"type": "team", "lead": int(team_match.group(1))},
            raw_target,
            False,
            None,
        )

    # 如果 Foul Play 选择了 Tera 变体，但当前 request 已经不能 Tera，
    # 优先投影到同一招式的普通版本，不要跳到另一招式。
    if want_tera:
        for action in actions:
            if action.get("type") != "move" or action.get("tera"):
                continue
            if normalize(action.get("id") or action.get("name")) != normalize(target):
                continue
            result = {"type": "move", "slot": int(action.get("slot", 0) or 0)}
            return result, normalize(target), True, "tera-unavailable"

    # request 可能在服务端评估期间发生强制换人或重复通知。
    # 不让一个过期的 bestAction 触发整轮随机 fallback，改为选择当前合法
    # 候选中概率最高的动作，并在响应中显式标记。
    ranked = sorted(
        (action_distribution or {}).items(),
        key=lambda item: item[1],
        reverse=True,
    )
    for candidate, _probability in ranked:
        try:
            action, normalized, _, _reason = choose_legal_action(
                state,
                candidate,
                None,
                allow_fallback=False,
            )
            return (
                action,
                normalized,
                True,
                _projection_reason(state, "raw-best-action-not-legal"),
            )
        except ValueError:
            continue

    if not allow_fallback:
        raise ValueError(f"DualView action is not legal in current request: {best_action}")

    legal_actions = list(state.get("actions") or [])
    if legal_actions:
        action = _direct_legal_action(legal_actions[0])
        return (
            action,
            _action_key(state, legal_actions[0]) or "",
            True,
            _projection_reason(state, "no-legal-action-in-distribution"),
        )
    raise ValueError(f"DualView action is not legal in current request: {best_action}")


class DualViewHandler(BaseHTTPRequestHandler):
    policy: LiveDualViewPolicy
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
            self._write_json(200, {"ok": True, "service": "foul-play-dual-view"})
            return
        self._write_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path not in {"/choice", "/dual-view"}:
            self._write_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > self.max_body_bytes:
            self._write_json(413, {"error": "invalid request size"})
            return
        try:
            state = json.loads(self.rfile.read(length))
            # poke-engine 的 Python binding 不保证同一个 policy 实例并发搜索安全。
            # PS 在换人和强制替补时可能快速发送相邻请求，因此串行化评估。
            with self.policy_lock:
                result = self.policy.evaluate(state)
            action, selected_action, selection_fallback, selection_fallback_reason = choose_legal_action(
                state,
                result.best_action,
                result.actions,
            )
            value = {
                **action,
                "decisionProvider": "dual-view",
                "bestAction": result.best_action,
                "selectedAction": selected_action,
                "selectionFallback": selection_fallback,
                "selectionFallbackReason": selection_fallback_reason,
                "actions": result.actions,
                "selfPolicy": result.self_policy,
                "opponentModelPrediction": result.opponent_model_prediction,
                "opponentPrediction": result.opponent_prediction,
                "opponentExploration": result.opponent_exploration,
                "rootActionScores": result.root_action_scores,
                "searchTimeMs": result.search_time_ms,
                "selfSearchTimeMs": result.self_search_time_ms,
                "opponentSearchTimeMs": result.opponent_search_time_ms,
                "particleCount": result.particle_count,
                "particleDiagnostics": result.particle_diagnostics,
                "actionPairValues": result.action_pair_values,
                "strength": result.strength,
                "referenceMode": result.reference_mode,
            }
            self._write_json(200, value)
        except Exception as error:
            print(f"dual-view request failed: {error}", file=sys.stderr, flush=True)
            self._write_json(400, {"error": str(error)})

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8094)
    parser.add_argument("--particles", type=int, default=1)
    parser.add_argument(
        "--search-time-ms",
        type=int,
        default=None,
        help="兼容参数，同时设置我方和对手 oracle 的搜索预算",
    )
    parser.add_argument("--self-search-time-ms", type=int, default=None)
    parser.add_argument("--opponent-search-time-ms", type=int, default=None)
    parser.add_argument(
        "--opponent-exploration",
        type=float,
        default=0.1,
        help="对手策略与均匀探索混合的比例",
    )
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--strength", type=float, default=1.0)
    parser.add_argument(
        "--mode",
        choices=("diagnostic", "upstream-compatible"),
        default="diagnostic",
    )
    parser.add_argument("--search-parallelism", type=int, default=1)
    args = parser.parse_args()

    DualViewHandler.policy = LiveDualViewPolicy(
        particle_count=args.particles,
        search_time_ms=args.search_time_ms,
        seed=args.seed,
        strength=args.strength,
        self_search_time_ms=args.self_search_time_ms,
        opponent_search_time_ms=args.opponent_search_time_ms,
        opponent_exploration=args.opponent_exploration,
        mode=args.mode,
        search_parallelism=args.search_parallelism,
    )
    server = ThreadingHTTPServer((args.host, args.port), DualViewHandler)
    print(
        f"foul-play-dual-view listening on http://{args.host}:{args.port}",
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
