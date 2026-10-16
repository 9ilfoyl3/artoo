"""Domain-neutral deterministic decimal calculator."""

from __future__ import annotations

import ast
from decimal import Decimal, DivisionByZero, InvalidOperation, ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, ROUND_UP, localcontext

from app.agent.tools.base import BaseTool, ToolResult

_MAX_EXPRESSION_CHARS = 1_000
_MAX_AST_DEPTH = 20
_MAX_POWER = 8
_MAX_PRECISION = 12
_ROUNDING = {
    "half_up": ROUND_HALF_UP,
    "half_even": ROUND_HALF_EVEN,
    "down": ROUND_DOWN,
    "up": ROUND_UP,
}


class _SafeDecimalEvaluator:
    def __init__(self) -> None:
        self._depth = 0

    def evaluate(self, expression: str) -> Decimal:
        tree = ast.parse(expression, mode="eval")
        return self._node(tree.body)

    def _node(self, node: ast.AST) -> Decimal:
        self._depth += 1
        try:
            if self._depth > _MAX_AST_DEPTH:
                raise ValueError("expression is too deeply nested")
            if isinstance(node, ast.Constant):
                return self._constant(node.value)
            if isinstance(node, ast.UnaryOp):
                value = self._node(node.operand)
                if isinstance(node.op, ast.USub):
                    return -value
                if isinstance(node.op, ast.UAdd):
                    return value
                raise ValueError("unsupported unary operator")
            if isinstance(node, ast.BinOp):
                left = self._node(node.left)
                right = self._node(node.right)
                if isinstance(node.op, ast.Add):
                    return left + right
                if isinstance(node.op, ast.Sub):
                    return left - right
                if isinstance(node.op, ast.Mult):
                    return left * right
                if isinstance(node.op, ast.Div):
                    return left / right
                if isinstance(node.op, ast.Mod):
                    return left % right
                if isinstance(node.op, ast.Pow):
                    if right != right.to_integral_value():
                        raise ValueError("decimal exponents are not supported")
                    if abs(right) > _MAX_POWER:
                        raise ValueError(f"exponent must be between -{_MAX_POWER} and {_MAX_POWER}")
                    return left ** int(right)
                raise ValueError("unsupported binary operator")
            if isinstance(node, ast.Call):
                return self._call(node)
            raise ValueError("unsupported expression node")
        finally:
            self._depth -= 1

    @staticmethod
    def _constant(value: object) -> Decimal:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("only finite numeric literals are allowed")
        if isinstance(value, float) and (value != value or value in {float("inf"), float("-inf")}):
            raise ValueError("NaN and infinity are not allowed")
        return Decimal(str(value))

    def _call(self, node: ast.Call) -> Decimal:
        if node.keywords:
            raise ValueError("keyword arguments are not supported")
        if not isinstance(node.func, ast.Name):
            raise ValueError("only whitelisted functions are supported")
        name = node.func.id
        values = [self._node(argument) for argument in node.args]
        if name == "sum" and values:
            return sum(values, Decimal("0"))
        if name == "min" and values:
            return min(values)
        if name == "max" and values:
            return max(values)
        if name == "percent" and len(values) == 2:
            return values[0] / values[1] * Decimal("100")
        if name == "round" and len(values) in {1, 2}:
            places = int(values[1]) if len(values) == 2 else 0
            if places < 0 or places > _MAX_PRECISION:
                raise ValueError(f"round places must be between 0 and {_MAX_PRECISION}")
            quantum = Decimal("1").scaleb(-places)
            return values[0].quantize(quantum, rounding=ROUND_HALF_UP)
        raise ValueError(f"unsupported function or arguments: {name}")


class CalculateTool(BaseTool):
    """Evaluate bounded decimal arithmetic without business semantics."""

    @property
    def name(self) -> str:
        return "calculate"

    @property
    def description(self) -> str:
        return (
            "Evaluate deterministic decimal arithmetic. Supports +, -, *, /, %, "
            "integer powers, parentheses, and sum/min/max/percent/round. Use this "
            "for arithmetic instead of calculating inside reasoning. The tool does "
            "not choose formulas or interpret business rules."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "Arithmetic expression, for example (750+1120-1600).",
                },
                "precision": {
                    "type": "integer",
                    "description": "Result decimal places (0-12).",
                    "default": 2,
                },
                "rounding": {
                    "type": "string",
                    "enum": list(_ROUNDING),
                    "default": "half_up",
                },
            },
            "required": ["expression"],
        }

    async def execute(self, args: dict) -> ToolResult:
        expression = str(args.get("expression") or "").strip()
        if not expression:
            return ToolResult(success=False, error="expression is required")
        if len(expression) > _MAX_EXPRESSION_CHARS:
            return ToolResult(success=False, error="expression is too long")
        try:
            precision = int(args.get("precision", 2))
        except (TypeError, ValueError):
            return ToolResult(success=False, error="precision must be an integer")
        if precision < 0 or precision > _MAX_PRECISION:
            return ToolResult(
                success=False,
                error=f"precision must be between 0 and {_MAX_PRECISION}",
            )
        rounding = str(args.get("rounding") or "half_up")
        if rounding not in _ROUNDING:
            return ToolResult(
                success=False,
                error=f"rounding must be one of: {', '.join(_ROUNDING)}",
            )

        try:
            with localcontext() as context:
                context.prec = 40
                value = _SafeDecimalEvaluator().evaluate(expression)
                quantum = Decimal("1").scaleb(-precision)
                rounded = value.quantize(quantum, rounding=_ROUNDING[rounding])
        except (SyntaxError, ValueError, InvalidOperation, DivisionByZero, OverflowError) as exc:
            return ToolResult(success=False, error=f"invalid expression: {exc}")
        except Exception as exc:  # noqa: BLE001 - malformed arithmetic must not crash the Agent
            return ToolResult(success=False, error=f"calculation failed: {exc}")

        result = format(rounded, "f")
        return ToolResult(
            success=True,
            output=f"{expression} = {result}",
            data={
                "expression": expression,
                "result": result,
                "precision": precision,
                "rounding": rounding,
            },
        )
