"""Read simple C++ numeric pair tables without executing repository code."""

import ast
import math
import operator
import re


class TableError(ValueError):
    """The source cannot be interpreted reliably as a numeric lookup table."""


def _without_comments(source: str) -> str:
    # Preserve strings so comment markers inside them are not mistaken for code.
    return re.sub(
        r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|//[^\n]*|/\*[\s\S]*?\*/',
        lambda match: " " if match[0].startswith(("//", "/*")) else match[0],
        source,
    )


def _split_values(text: str) -> list[str]:
    depth = 0
    start = 0
    values = []
    for index, char in enumerate(text):
        if char in "{(":
            depth += 1
        elif char in "})":
            depth -= 1
        elif char == "," and depth == 0:
            values.append(text[start:index].strip())
            start = index + 1
    if text[start:].strip():
        values.append(text[start:].strip())
    return values


def _evaluate(expression: str, source: str, resolving=()) -> float | int:
    if len(expression) > 500 or len(resolving) > 10:
        raise TableError("The table contains an expression that is too complex.")
    # C++ floating-point and integer suffixes; do not strip unit suffixes or identifiers.
    expression = re.sub(
        r"(?<!\w)(\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?[fF]\b",
        lambda match: str(float(match[1])), expression,
    )
    expression = re.sub(
        r"(?<!\w)(\d+)[uUlL]{1,3}\b",
        r"\1", expression,
    )
    try:
        root = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as error:
        raise TableError("Unsupported C++ expression; use a plain numeric pair table.") from error
    if sum(1 for _ in ast.walk(root)) > 80:
        raise TableError("The table contains an expression that is too complex.")

    def visit(node):
        if isinstance(node, ast.Constant) and type(node.value) in (float, int):
            value = node.value
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        elif isinstance(node, ast.BinOp) and type(node.op) in (ast.Add, ast.Sub, ast.Mult, ast.Div):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Div):
                if right == 0:
                    raise TableError("The table expression divides by zero.")
                value = left / right
                if isinstance(left, int) and isinstance(right, int):
                    value = math.trunc(value)  # C++ integer division truncates toward zero.
            else:
                value = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul}[type(node.op)](left, right)
        elif isinstance(node, ast.Name):
            name = node.id
            if name in resolving:
                raise TableError(f"Circular definition of {name}.")
            definitions = list(re.finditer(
                rf"\b(?:(?:const|constexpr|static)\s+)*(double|float|int|unsigned\s+int|int32_t|int64_t|uint32_t|uint64_t|size_t|auto)\s+{re.escape(name)}\s*(?:=\s*([^;{{}}]+)|\{{\s*([^;{{}}]+)\s*\}});", source,
            ))
            writes = re.findall(rf"\b{re.escape(name)}\s*(?:[+*/-]?=(?!=)|\+\+|--|\{{)", source)
            prefix_write = re.search(rf"(?:\+\+|--)\s*\b{re.escape(name)}\b", source)
            if len(definitions) != 1 or len(writes) != 1 or prefix_write:
                raise TableError(f"Cannot resolve {name} to one unchanged scalar definition before the table.")
            definition = definitions[0]
            val_expr = definition[2] if definition[2] is not None else definition[3]
            value = _evaluate(val_expr, source[:definition.start()], (*resolving, name))
            if definition[1] in ("double", "float"):
                value = float(value)
            elif definition[1] in ("int", "unsigned int", "int32_t", "int64_t", "uint32_t", "uint64_t", "size_t"):
                value = math.trunc(value)
        else:
            raise TableError("Only numeric literals, scalar names, and +, -, *, / are supported; C++ calls are not executed.")
        if not math.isfinite(value) or abs(value) > 1e12:
            raise TableError("Table values must be finite and no larger than 1e12 in magnitude.")
        return value

    return visit(root.body)


def extract_table(source: str, table: str) -> list[tuple[float, float]]:
    """Extract every {x, y} row in a named brace initializer, including offsets."""
    if not re.fullmatch(r"[A-Za-z_]\w{0,99}", table):
        raise TableError("Use the table's exact unqualified C++ variable name.")
    source = _without_comments(source)
    starts = list(re.finditer(rf"\b{re.escape(table)}(?:\s*\[\s*\d*\s*\])?\s*(?:=\s*)?(?:\(\s*)?\{{", source))
    if len(starts) != 1:
        raise TableError(f"Expected one brace initializer for {table}; found {len(starts)}.")
    start = starts[0].end()
    depth = 1
    end = start
    while end < len(source) and depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    if depth:
        raise TableError("The table is incomplete; read the complete source before plotting.")
    rows = _split_values(source[start:end - 1])
    if not 2 <= len(rows) <= 500:
        raise TableError("Each table must contain between 2 and 500 points.")
    points = []
    for row in rows:
        if not (row.startswith("{") and row.endswith("}")):
            raise TableError("Expected a table containing {x, y} rows.")
        pair = _split_values(row[1:-1])
        if len(pair) != 2:
            raise TableError("Every row must contain exactly two values.")
        points.append(tuple(float(_evaluate(value, source[:starts[0].start()])) for value in pair))
    if len({point[0] for point in points}) != len(points):
        raise TableError("Duplicate x values make this lookup table ambiguous.")
    return sorted(points)
