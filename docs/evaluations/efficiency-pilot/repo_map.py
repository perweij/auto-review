"""Disposable Python navigation map; no stored index and no project imports."""
import ast
from pathlib import Path
import re
import subprocess


def generate(repo, task, budget=6000):
    repo = Path(repo)
    paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=repo).decode().split('\0')
    changed = subprocess.run(['git', 'diff', '--name-only', 'HEAD^', 'HEAD'], cwd=repo,
                             capture_output=True, text=True).stdout.splitlines()
    words = set(re.findall(r'[a-z_]{4,}', task.lower()))
    def rank(path):
        parts = set(re.findall(r'[a-z]{4,}', path.lower()))
        return (-int(path in task), -len(words & parts), -int(path in changed), path)
    header = ('Disposable navigation map (partial; current source is authoritative).\n'
              'Signatures and locations only; inspect source before editing.\n')
    lines = [header]
    used = len(header)
    for name in sorted((p for p in paths if p.endswith('.py')), key=rank):
        path = repo / name
        if path.is_symlink():
            continue
        try:
            tree = ast.parse(path.read_text(), filename=name)
        except (OSError, UnicodeError, SyntaxError):
            continue
        symbols = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(f'  {node.lineno}: {node.name}({ast.unparse(node.args)})')
            elif isinstance(node, ast.ClassDef):
                symbols.append(f'  {node.lineno}: class {node.name}')
                for method in node.body:
                    if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        symbols.append(f'  {method.lineno}: {node.name}.{method.name}({ast.unparse(method.args)})')
        block = name + '\n'
        for symbol in symbols:
            line = symbol[:160] + (' ...' if len(symbol) > 160 else '') + '\n'
            if len(block) + len(line) > 900:
                block += '  ... additional symbols omitted\n'
                break
            block += line
        if used + len(block) > budget:
            continue
        lines.append(block)
        used += len(block)
    return ''.join(lines)[:budget]
