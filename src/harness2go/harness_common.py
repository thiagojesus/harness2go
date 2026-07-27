"""Shared helpers for opencode2claude.py and claude2opencode.py: JSONC parsing,
frontmatter parsing, secret masking, and interactive prompt primitives used by
both directions of the harness2go session/config migration tools."""

import json
import os
import re


def slugify_cwd(cwd):
    """Claude Code's own project-folder naming: every / and . becomes -."""
    return "".join("-" if c in ("/", ".") else c for c in cwd)


def mask_secret(value):
    if not isinstance(value, str) or len(value) <= 8:
        return "***"
    return value[:4] + "…" + value[-2:]


def strip_jsonc_comments(text):
    """Remove // and /* */ comments from JSONC, respecting string literals
    (so URLs like "https://..." don't get truncated by a naive // strip)."""
    out = []
    i, n = 0, len(text)
    in_string = False
    escape = False
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def load_jsonc(path):
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        raw = f.read()
    cleaned = strip_jsonc_comments(raw)
    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)  # trailing commas
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {}


def has_jsonc_comments(path):
    """True if the file contains // or /* */ comments that a plain json.load
    would choke on (used to warn before an overwrite that would drop them)."""
    if not os.path.exists(path):
        return False
    with open(path) as f:
        raw = f.read()
    return strip_jsonc_comments(raw) != raw


def parse_scalar(v):
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        return [parse_scalar(item.strip()) for item in inner.split(",") if item.strip()] if inner else []
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def parse_frontmatter(text):
    """Minimal YAML-ish frontmatter parser: flat `key: value` pairs, a
    single-line flow sequence (`key: [a, b]`), or one level of nested
    `key:\\n  sub: value` maps / `key:\\n  - item` lists — enough for
    typical agent markdown files (Claude Code's, OpenCode's, or VS Code's),
    without a YAML dependency."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm_text = text[3:end].strip("\n")
    body = text[end + 4:].lstrip("\n")

    data = {}
    current_key = None
    for line in fm_text.splitlines():
        if not line.strip():
            continue
        if line[:1] in (" ", "\t") and current_key is not None:
            stripped = line.strip()
            if stripped.startswith("- "):
                if not isinstance(data.get(current_key), list):
                    data[current_key] = []
                data[current_key].append(parse_scalar(stripped[2:]))
                continue
            k, sep, v = stripped.partition(":")
            if sep:
                if not isinstance(data.get(current_key), dict):
                    data[current_key] = {}
                data[current_key][k.strip()] = parse_scalar(v.strip())
            continue
        k, sep, v = line.partition(":")
        if not sep:
            continue
        k, v = k.strip(), v.strip()
        if v == "":
            data[k] = None  # becomes a dict or list once nested lines (if any) are seen
            current_key = k
        else:
            data[k] = parse_scalar(v)
            current_key = None
    return data, body


def slugify_name(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "agent"


def prompt_yes_no(question, default=False):
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        ans = input(question + suffix).strip().lower()
    except EOFError:
        return default
    if not ans:
        return default
    return ans in ("y", "yes")


def prompt_choice(question, choices, default=None):
    choice_str = "/".join(choices)
    default_hint = f" [{default}]" if default else ""
    while True:
        try:
            ans = input(f"{question} ({choice_str}){default_hint}: ").strip().lower()
        except EOFError:
            return default
        if not ans and default:
            return default
        if ans in choices:
            return ans
        print(f"  Please answer one of: {choice_str}")


def prompt_text(question, default=""):
    suffix = f" [{default}]" if default else ""
    try:
        ans = input(f"{question}{suffix}: ").strip()
    except EOFError:
        return default
    return ans or default
