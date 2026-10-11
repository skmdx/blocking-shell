"""Bounded, read-only fingerprints for repeated shell procedures.

These identify refactoring candidates, never equivalent commands or safe rewrites.
Unsupported syntax is excluded rather than compared by its outer wrapper alone.
"""
import ast
from dataclasses import dataclass
from difflib import SequenceMatcher
import hashlib
import re
import shlex

from tree_sitter import Language, Parser
import tree_sitter_bash

MIN_LENGTH = 400
MAX_BYTES = 32768
MAX_TOKENS = 1024
MAX_PATTERNS = 16
HISTORY_LIMIT = 64
SUGGESTION = ('Similar long commands have repeated. Consider scripting the shared steps '
              'or integrating them into an existing tool.')
LANGUAGE = Language(tree_sitter_bash.language())
SHELLS = {'bash', 'sh', 'dash', 'zsh'}
PYTHON = re.compile(r'python(?:[23](?:\.\d+)?)?\Z')


@dataclass(frozen=True)
class Pattern:
    tokens: tuple[str, ...]
    keys: tuple[str, ...]

    def matches(self, other: 'Pattern') -> bool:
        if self.keys != other.keys:
            return False
        matcher = SequenceMatcher(None, self.tokens, other.tokens, autojunk=False)
        return matcher.quick_ratio() >= .7 and matcher.ratio() >= .7


def unquote(text: str) -> str:
    words = shlex.split(text)
    return words[0] if len(words) == 1 else text


def value(text: str) -> str:
    if any(c.isspace() for c in text):
        return text
    if re.fullmatch(r'[0-9.]+|[0-9a-f]{10,64}', text):
        return '<number>'
    if '/' in text or re.search(r'\.(?:json|xml|log|txt)$', text):
        return '<path>'
    return text


def callee(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return callee(node.value) + '.' + node.attr
    if isinstance(node, ast.Call):
        return callee(node.func) + '()'
    return type(node).__name__


class Fingerprinter:
    def __init__(self):
        self.parser = Parser(LANGUAGE)
        self.tokens: list[str] = []
        self.keys: set[str] = set()
        self.fragments: list[str] = []

    def python(self, source: str):
        tree = ast.parse(source)
        for node in ast.walk(tree):
            self.tokens.append(type(node).__name__)
            if isinstance(node, ast.Call):
                self.keys.add('python:' + callee(node.func))
            if isinstance(node, ast.Name):
                self.tokens.append(node.id)
            elif isinstance(node, ast.Attribute):
                self.tokens.append(node.attr)
            elif isinstance(node, ast.Constant):
                self.tokens.append(value(node.value) if isinstance(node.value, str) else '<literal>')

    def shell(self, source: str, depth: int = 0):
        if depth > 5:
            raise ValueError('Shell nesting limit')
        data = source.encode()
        tree = self.parser.parse(data)
        if tree.root_node.has_error:
            raise ValueError('Unsupported shell syntax')
        commands = []

        def text(node):
            return data[node.start_byte:node.end_byte].decode()

        def words(node):
            name = node.child_by_field_name('name')
            return unquote(text(name)), [unquote(text(c)) for i, c in enumerate(node.children)
                                         if node.field_name_for_child(i) == 'argument']

        def embedded(body, python=False):
            self.tokens.append('python-body' if python else 'shell-body')
            if python:
                self.python(body)
            else:
                self.shell(body, depth + 1)
            self.tokens.append('end-body')

        def command(node):
            name, args = words(node)
            executable = name.rsplit('/', 1)[-1]
            for child in node.named_children:
                if child.type == 'variable_assignment':
                    self.tokens.append('env:' + text(child.child_by_field_name('name')))
            if executable == 'ssh':
                if len(args) != 2 or args[0].startswith('-'):
                    raise ValueError('Unsupported ssh invocation')
                embedded(args[1])
                return
            if executable == 'env':
                i = 0
                while i < len(args):
                    if args[i] in ('-u', '--unset', '-C', '--chdir'):
                        i += 2
                    elif args[i].startswith('-') or re.match(r'^[A-Za-z_]\w*=', args[i]):
                        i += 1
                    else:
                        break
                if i < len(args):
                    embedded(shlex.join(args[i:]))
                    return
            if '-c' in args:
                i = args.index('-c')
                direct = all(a.startswith('-') for a in args[:i])
                interpreter = executable if (direct and (executable in SHELLS or PYTHON.fullmatch(executable))) else None
                if executable in ('npm', 'npx'):
                    interpreter = 'sh'
                if interpreter is None and executable not in SHELLS and not PYTHON.fullmatch(executable):
                    positions = [j for j, a in enumerate(args[:i]) if a in SHELLS or PYTHON.fullmatch(a)]
                    if positions and all(a.startswith('-') for a in args[positions[-1] + 1:i]):
                        interpreter = args[positions[-1]]
                if interpreter is not None:
                    if i + 1 == len(args):
                        raise ValueError('Missing interpreter body')
                    embedded(args[i + 1], bool(PYTHON.fullmatch(interpreter)))
                    return
            identity = executable
            if PYTHON.fullmatch(executable) and '-m' in args:
                i = args.index('-m')
                identity = args[i + 1]
                args = args[i + 2:]
            elif executable in SHELLS | {'python', 'python2', 'python3', 'node'}:
                script = next((a for a in args if re.search(r'\.(?:py|sh|mjs|cjs|js)$', a)), None)
                if script:
                    identity += ':' + script
            if executable in ('pnpm', 'npm', 'git', 'cargo', 'uv'):
                identity += ':' + ':'.join([a for a in args if not a.startswith('-')][:2])
            self.keys.add('cmd:' + identity)
            self.tokens.append('cmd:' + identity)
            for arg in args:
                if len(arg) > 120 and any(c.isspace() for c in arg):
                    self.keys.add('opaque:' + hashlib.sha256(arg.encode()).hexdigest())
                if executable in SHELLS and arg.endswith('.sh'):
                    self.keys.add('script:' + arg)
                if identity == 'pytest' and re.search(r'\.py(?:::.*)?$', arg):
                    target = arg.split('[')[0]
                    self.keys.add('test:' + target)
                    self.tokens.append(target)
                elif arg.startswith('-') and '=' in arg:
                    self.tokens.append(arg.split('=', 1)[0] + '=<value>')
                else:
                    self.tokens.append(value(arg))
            self.tokens.append('end-command')

        def last_command(node):
            if node.type == 'command':
                return node
            for child in reversed(node.named_children):
                found = last_command(child)
                if found is not None:
                    return found
            return None

        def walk(node):
            if len(self.tokens) > MAX_TOKENS:
                raise ValueError('Fingerprint limit')
            if node.type == 'command':
                commands.append(node)
                command(node)
                return
            if node.type == 'redirected_statement':
                body = node.child_by_field_name('body')
                if body is not None:
                    walk(body)
                for child in node.named_children:
                    if child.type == 'heredoc_redirect':
                        consumer = last_command(body) if body is not None else None
                        header = []
                        if consumer is not None:
                            name, args = words(consumer)
                            header = [name, *args]
                        python = any(PYTHON.fullmatch(w) for w in header)
                        if not python and not any(w in SHELLS for w in header):
                            raise ValueError('Unsupported heredoc language')
                        for part in child.named_children:
                            if part.type == 'heredoc_body':
                                embedded(text(part), python)
                    elif child.type == 'file_redirect':
                        self.tokens.append('redirect')
                return
            if node.type == 'variable_assignment':
                self.tokens.append('assign:' + text(node.child_by_field_name('name')))
                return
            if node.type in ('comment', 'heredoc_redirect'):
                return
            self.tokens.append(node.type)
            for child in node.children:
                if child.is_named:
                    walk(child)
                elif child.type in ('&&', '||', '|', ';', '&'):
                    self.tokens.append(child.type)

        walk(tree.root_node)
        for first, second in zip(commands, commands[1:]):
            gap = data[first.end_byte:second.start_byte].decode()
            if re.fullmatch(r'\s*(?:&&|;)\s*', gap):
                fragment = data[first.start_byte:second.end_byte].decode()
                if len(fragment) >= 200:
                    self.fragments.append(fragment)

    def pattern(self) -> Pattern:
        if not self.keys or len(self.tokens) > MAX_TOKENS:
            raise ValueError('Fingerprint limit or no executable')
        return Pattern(tuple(self.tokens), tuple(sorted(self.keys)))


def patterns(command: str) -> list[Pattern]:
    if len(command) < MIN_LENGTH or len(command.encode()) > MAX_BYTES:
        return []
    try:
        full = Fingerprinter()
        full.shell(command)
        result = [full.pattern()]
        for fragment in dict.fromkeys(full.fragments):
            part = Fingerprinter()
            part.shell(fragment)
            candidate = part.pattern()
            if candidate not in result:
                result.append(candidate)
            if len(result) == MAX_PATTERNS:
                break
        return result
    except (ValueError, SyntaxError, RecursionError, IndexError):
        return []
