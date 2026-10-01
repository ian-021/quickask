"""Scopes: one markdown file per project, with YAML frontmatter.

    ---
    name: trawl
    description: one line Jev uses to decide whether a question is about trawl
    ---
    The body: context Cerebras gets as the system prompt when a question is
    routed to this scope.

`name` is the routing label and must match the file name (scopes/trawl.md ->
trawl). `description` is what Jev sees for this option, so it should say what
the project is in the words a question about it would use. The body is never
shown to Jev, only to Cerebras.
"""

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Routing labels are keys in the Jev Choice criteria, so keep them simple.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# The router adds this option itself; a scope may not claim it.
RESERVED_NAMES = {"none"}

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?(.*)\Z", re.DOTALL)


class ScopeError(Exception):
    """One scope file is unusable. The message names the file and the reason."""


@dataclass(frozen=True)
class Scope:
    name: str
    description: str
    body: str
    path: Path

    @property
    def is_template(self):
        """Still carries the TODO placeholders from the generated template."""
        return "TODO" in self.description or "TODO" in self.body


@dataclass
class LoadedScopes:
    """Everything the loader found. Invalid files are reported, not fatal, so
    one bad scope never takes the popup down with it."""

    scopes: dict = field(default_factory=dict)   # name -> Scope, in file order
    errors: list = field(default_factory=list)   # str, one per unusable file
    directory: Path = None

    def __iter__(self):
        return iter(self.scopes.values())

    def __len__(self):
        return len(self.scopes)

    def __bool__(self):
        return bool(self.scopes)

    def get(self, name):
        return self.scopes.get(name)


def default_scopes_dir():
    """The repo's scopes/ folder when running from source. The built .app
    cannot be edited in place, so it reads ~/.config/quickask/scopes instead.
    QUICKASK_SCOPES_DIR overrides both."""
    import os
    override = os.environ.get("QUICKASK_SCOPES_DIR")
    if override:
        return Path(override).expanduser()
    if getattr(sys, "frozen", False):
        return Path.home() / ".config" / "quickask" / "scopes"
    return Path(__file__).resolve().parent.parent / "scopes"


def parse_scope(text, path):
    """Parse one file's text into a Scope. Raises ScopeError."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise ScopeError(f"{path.name}: no YAML frontmatter (the file must start with a --- block)")
    raw_meta, body = m.group(1), m.group(2)
    try:
        meta = yaml.safe_load(raw_meta)
    except yaml.YAMLError as e:
        raise ScopeError(f"{path.name}: frontmatter is not valid YAML: {e}") from None
    if not isinstance(meta, dict):
        raise ScopeError(f"{path.name}: frontmatter must be a mapping with name and description")

    name = meta.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ScopeError(f"{path.name}: frontmatter needs a non-empty 'name'")
    name = name.strip()
    if not _NAME_RE.match(name):
        raise ScopeError(f"{path.name}: name {name!r} must be lowercase letters, digits and dashes")
    if name in RESERVED_NAMES:
        raise ScopeError(f"{path.name}: name {name!r} is reserved by the router")
    if name != path.stem:
        raise ScopeError(f"{path.name}: name {name!r} does not match the file name ({path.stem})")

    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ScopeError(f"{path.name}: frontmatter needs a non-empty 'description'")
    description = " ".join(description.split())  # one line, single spaces

    extra = set(meta) - {"name", "description"}
    if extra:
        raise ScopeError(f"{path.name}: unknown frontmatter keys: {', '.join(sorted(extra))}")

    body = body.strip()
    if not body:
        raise ScopeError(f"{path.name}: the body (system prompt) is empty")

    return Scope(name=name, description=description, body=body, path=path)


def load_scopes(directory=None):
    """Read every *.md in the scopes directory. Files that fail validation are
    listed in .errors; the rest are usable. A missing directory is an error
    with no scopes, not an exception."""
    directory = Path(directory) if directory else default_scopes_dir()
    loaded = LoadedScopes(directory=directory)
    if not directory.is_dir():
        loaded.errors.append(f"scopes directory not found: {directory}")
        return loaded
    for path in sorted(directory.glob("*.md")):
        try:
            scope = parse_scope(path.read_text(encoding="utf-8"), path)
        except ScopeError as e:
            loaded.errors.append(str(e))
            continue
        except OSError as e:
            loaded.errors.append(f"{path.name}: could not read: {e}")
            continue
        if scope.name in loaded.scopes:
            loaded.errors.append(f"{path.name}: duplicate scope name {scope.name!r}")
            continue
        loaded.scopes[scope.name] = scope
    return loaded


def main(argv=None):
    """python -m quickask.scopes [dir]: list what the loader sees."""
    argv = sys.argv[1:] if argv is None else argv
    loaded = load_scopes(argv[0] if argv else None)
    print(f"scopes in {loaded.directory}:")
    for s in loaded:
        flag = "  (template, still has TODOs)" if s.is_template else ""
        print(f"  {s.name:<20} {s.description[:70]}{flag}")
    for err in loaded.errors:
        print(f"  ERROR {err}")
    return 1 if loaded.errors else 0


if __name__ == "__main__":
    sys.exit(main())
