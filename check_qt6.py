#!/usr/bin/env python3
"""Heuristic Qt6 / QGIS 4 enum check.

PyQt6 no longer exposes enum members on the class, so ``QFrame.HLine`` must
be written ``QFrame.Shape.HLine``. The QGIS Plugin Repository rejects
plugins that still use the short form.

This flags ``<Class>.<Member>`` where <Class> is imported from qgis.PyQt,
qgis.core or qgis.gui and <Member> looks like an enum value (leading capital)
and is not itself continued by a further attribute (``QFrame.Shape.HLine``
is fine, ``QFrame.HLine`` is not).

It is a heuristic, not a replacement for the repository's own scanner:
it catches the common cases, not every one.

Usage: check_qt6.py <dir> [<dir> ...]
"""
import ast
import sys
from pathlib import Path

QT_MODULES = ("qgis.PyQt", "qgis.core", "qgis.gui", "PyQt5", "PyQt6")


def _qt_names(tree: ast.AST) -> set:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and \
                node.module.startswith(QT_MODULES):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def check_file(path: Path) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    qt = _qt_names(tree)
    if not qt:
        return []

    # Attribute nodes that are continued (X.Y in X.Y.Z) or called (X.Y()).
    continued = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute):
            continued.add(id(node.value))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            continued.add(id(node.func))

    problems = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in qt
            and node.attr[:1].isupper()
            and id(node) not in continued
        ):
            problems.append((node.lineno, node.col_offset, node.value.id, node.attr))
    return sorted(problems)


def main(argv: list) -> int:
    roots = [Path(a) for a in argv] or [Path("plugin")]
    total = 0
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            for line, col, cls, attr in check_file(path):
                print(f"{path}:{line}:{col}: {cls}.{attr} — "
                      f"enum member not fully qualified (Qt6)")
                total += 1
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
