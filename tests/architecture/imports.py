"""Resolve absolute, relative, type-only and literal dynamic Python imports."""
import ast
from dataclasses import dataclass
from importlib.util import resolve_name


@dataclass(frozen=True)
class ImportEdge:
    source: str
    target: str
    line: int


def imports(source: str, module: str, *, package: bool = False) -> tuple[ImportEdge, ...]:
    """Inspect all branches and function bodies; never execute imported code."""
    tree = ast.parse(source)
    parent = module if package else module.rpartition('.')[0]
    bindings: dict[str, str] = {}
    edges: list[ImportEdge] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                edges.append(ImportEdge(module, alias.name, node.lineno))
                bindings[alias.asname or alias.name.split('.')[0]] = alias.name if alias.asname else alias.name.split('.')[0]
        elif isinstance(node, ast.ImportFrom):
            name = '.' * node.level + (node.module or '')
            target = resolve_name(name, parent) if node.level else name
            edges.append(ImportEdge(module, target, node.lineno))
            for alias in node.names:
                imported = target + '.' + alias.name
                bindings[alias.asname or alias.name] = imported
                # Include imported submodules as well as the parent package.
                edges.append(ImportEdge(module, imported, node.lineno))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = ast.unparse(node.func)
        first, separator, rest = function.partition('.')
        function = bindings.get(first, first) + (separator + rest if separator else '')
        if function not in ('__import__', 'importlib.import_module'):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
            raise ValueError(f'{module}:{node.lineno}: computed dynamic import requires explicit review')
        target = node.args[0].value
        if target.startswith('.'):
            supplied = node.args[1] if len(node.args) > 1 else next((keyword.value for keyword in node.keywords if keyword.arg == 'package'), None)
            if isinstance(supplied, ast.Constant) and isinstance(supplied.value, str):
                target = resolve_name(target, supplied.value)
            else:
                raise ValueError(f'{module}:{node.lineno}: dynamic relative import needs a literal package')
        edges.append(ImportEdge(module, target, node.lineno))
    return tuple(edges)
