# SPDX-License-Identifier: MPL-2.0
"""Read untrusted ``plugin.json`` without importing any plugin code.

Manifest: id/name/version, optional author/description/entry/api_version,
platforms (darwin/win32/linux), requirements (exact wheel dependency pins),
settings, actions, permissions, events and event_timeout (seconds in (0, 3600],
default 120). Settings and action parameters are
lists of {key, title, kind, default, help?, options?, minimum?, maximum?, group?,
visible_when?, required?}. ``type`` is accepted as an alias for ``kind``.
Choice options are [value, label] pairs or {value, label} objects. Secret defaults
must be empty. Events are subscription names, not runtime history.

Invalid manifests/values raise ValueError. No code runs and no network is used.
``validate_values`` rejects unknown keys, fills defaults, and converts numeric
UI strings to finite int/float values. Hidden fields still receive type/range
validation; required applies only when all visible_when conditions match.
Manifests allow at most 64 settings/actions/parameters/options per list and 256
setting/parameter controls in total. Labels are limited to 256 characters,
help/description to 4096, and text values/defaults to 16384.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import math
from pathlib import Path, PurePosixPath
import re

from .models import Action, Permission, Plugin, SettingSpec

MAX_MANIFEST_BYTES = 1024 * 1024
MAX_SCHEMA_FIELDS = 64
MAX_TOTAL_FIELDS = 256
MAX_LABEL_LENGTH = 256
MAX_HELP_LENGTH = 4096
MAX_TEXT_LENGTH = 16384
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}\Z")
_REQUIREMENT = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*\])?"
    r"==[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?"
    r"(?:\.post[0-9]+)?(?:\.dev[0-9]+)?(?:\+[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*)?\Z"
)
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
             *(f"lpt{i}" for i in range(1, 10))}


def validate_id(value):
    """Validate portable IDs used as a single storage directory component."""
    if (not isinstance(value, str) or not _ID.fullmatch(value)
            or ".." in value or value.endswith(".")
            or value.split(".")[0].lower() in _RESERVED):
        raise ValueError("Invalid plugin id")
    return value


def safe_relative(value: str) -> Path:
    """Reject absolute, Windows, traversal and ambiguous portable paths."""
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or "\x00" in value:
        raise ValueError("Invalid relative path")
    parts = value.split("/")
    if PurePosixPath(value).is_absolute() or any(
        part in ("", ".", "..") or part.endswith((".", " "))
        or part.split(".")[0].lower() in _RESERVED for part in parts
    ):
        raise ValueError("Unsafe relative path")
    return Path(*parts)


def contained_path(root: Path, relative: str) -> Path:
    """Resolve a relative path, rejecting every symlink below root."""
    root = root.resolve()
    result = root
    for part in safe_relative(relative).parts:
        result = result / part
        if result.is_symlink():
            raise ValueError("Symlinks are not allowed")
    if not result.resolve().is_relative_to(root):
        raise ValueError("Path escapes plugin root")
    return result


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path: Path, maximum=MAX_MANIFEST_BYTES):
    """Bounded strict JSON reader, also shared by the persistence layer."""
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("JSON file is too large")
    try:
        result = json.loads(data, object_pairs_hook=_pairs,
                            parse_constant=lambda value: _invalid(f"Non-finite JSON: {value}"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Invalid JSON") from exc
    return result


def _invalid(message):
    raise ValueError(message)


def _text(value, label, *, empty=True, maximum=MAX_TEXT_LENGTH):
    if (not isinstance(value, str) or len(value) > maximum
            or (not empty and not value.strip()) or "\x00" in value):
        raise ValueError(f"{label} must be a string" + ("" if empty else " and nonempty"))
    return value


def _bool(value, label):
    if type(value) is not bool:
        raise ValueError(f"{label} must be boolean")
    return value


def _number(value, label, *, strings=False):
    if strings and isinstance(value, str):
        try:
            value = float(value)
        except ValueError as exc:
            raise ValueError(f"{label} must be numeric") from exc
    if type(value) not in (int, float):
        raise ValueError(f"{label} must be numeric")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f"{label} must be finite")
    return int(value) if float(value).is_integer() else value


def _list(value, label, *, maximum=MAX_SCHEMA_FIELDS):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{label} must be an array with at most {maximum} entries")
    return value


def _object(value, allowed, label):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(f"Invalid or unknown fields in {label}")
    return value


def _key(value):
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise ValueError("Invalid schema key")
    return value


def _scalar(value):
    if isinstance(value, str):
        return _text(value, "choice value")
    if value is None or type(value) is bool:
        return value
    return _number(value, "choice value")


def _same(a, b):
    # bool is not interchangeable with 0/1 in choice/visibility schemas.
    return a == b and (type(a) is bool) == (type(b) is bool)


def validate_values(specs, values) -> dict:
    """Normalize all fields; only visible fields enforce required (AND equality).

    All values are normalized before conditions are evaluated, so schema order
    and numeric UI strings do not affect visibility. Hidden fields cannot bypass
    type, range, choice or text-length constraints.
    """
    if not isinstance(values, dict) or set(values) - {spec.key for spec in specs}:
        raise ValueError("Unknown setting/parameter key or invalid values object")
    result = {}
    for spec in specs:
        value = deepcopy(values.get(spec.key, spec.default))
        if spec.kind == "switch":
            value = _bool(value, spec.key)
        elif spec.kind in ("text", "secret"):
            value = _text(value, spec.key)
        elif spec.kind == "number":
            value = _number(value, spec.key, strings=True)
            if spec.minimum is not None and value < spec.minimum:
                raise ValueError(f"{spec.key} is below minimum")
            if spec.maximum is not None and value > spec.maximum:
                raise ValueError(f"{spec.key} is above maximum")
        elif spec.kind == "choice":
            if not any(_same(value, option[0]) for option in spec.options):
                raise ValueError(f"Invalid choice for {spec.key}")
        else:
            raise ValueError(f"Unknown setting kind: {spec.kind}")
        result[spec.key] = value
    for spec in specs:
        visible = all(key in result and _same(result[key], expected)
                      for key, expected in spec.visible_when.items())
        value = result[spec.key]
        if spec.required and visible and (value is None or isinstance(value, str) and not value.strip()):
            raise ValueError(f"{spec.key} is required")
    return result


def _specs(raw):
    result = []
    keys = set()
    for item in _list(raw, "schema"):
        _object(item, {"key", "title", "kind", "type", "default", "help", "options",
                       "minimum", "maximum", "group", "visible_when", "required"}, "schema")
        key = _key(item.get("key"))
        if key in keys:
            raise ValueError(f"Duplicate schema key: {key}")
        keys.add(key)
        if "kind" in item and "type" in item:
            raise ValueError("Use kind or type, not both")
        kind = item.get("kind", item.get("type"))
        if kind not in ("switch", "choice", "text", "secret", "number"):
            raise ValueError("Unsupported setting kind")
        if "default" not in item:
            raise ValueError(f"Missing default for {key}")
        options = []
        for option in _list(item.get("options", []), "options"):
            if isinstance(option, dict):
                _object(option, {"value", "label"}, "option")
                if set(option) != {"value", "label"}:
                    raise ValueError("Choice option needs value and label")
                option = [option["value"], option["label"]]
            if not isinstance(option, list) or len(option) != 2:
                raise ValueError("Choice option must be [value, label]")
            val = _scalar(option[0])
            if any(_same(val, existing[0]) for existing in options):
                raise ValueError("Duplicate choice option")
            options.append((val, _text(option[1], "option label", empty=False, maximum=MAX_LABEL_LENGTH)))
        if (kind == "choice" and not options) or (kind != "choice" and options):
            raise ValueError("Only choice settings have nonempty options")
        minimum = item.get("minimum")
        maximum = item.get("maximum")
        if kind != "number" and (minimum is not None or maximum is not None):
            raise ValueError("Only numbers have ranges")
        if minimum is not None:
            minimum = _number(minimum, "minimum")
        if maximum is not None:
            maximum = _number(maximum, "maximum")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError("Reversed numeric range")
        visible = item.get("visible_when", {})
        if not isinstance(visible, dict) or len(visible) > 16:
            raise ValueError("visible_when must be an object with at most 16 conditions")
        default = item["default"]
        if kind == "secret" and default != "":
            raise ValueError("Secret defaults must be empty (never store credentials in manifests)")
        if kind == "number":
            default = _number(default, "numeric default")
        spec = SettingSpec(key, _text(item.get("title", key), "title", empty=False, maximum=MAX_LABEL_LENGTH), kind,
                           default, _text(item.get("help", ""), "help", maximum=MAX_HELP_LENGTH), tuple(options),
                           minimum, maximum, _text(item.get("group", ""), "group", maximum=MAX_LABEL_LENGTH), visible,
                           _bool(item.get("required", False), "required"))
        validate_values((replace(spec, required=False),), {key: default})
        result.append(spec)
    by_key = {spec.key: spec for spec in result}
    normalized = []
    for spec in result:
        conditions = {}
        for key, value in spec.visible_when.items():
            if key not in by_key or key == spec.key or by_key[key].kind == "secret":
                raise ValueError("Invalid visible_when reference")
            conditions[key] = validate_values((replace(by_key[key], required=False),), {key: value})[key]
        normalized.append(replace(spec, visible_when=conditions))
    return tuple(normalized)


def validate_requirement(value):
    """Only exact, URL-free direct pins; wheel transitives are locked on install."""
    if not isinstance(value, str) or len(value) > 512 or not _REQUIREMENT.fullmatch(value):
        raise ValueError("Requirements must be exact package==version pins (no URLs/options)")
    return value


def load_manifest(path: Path) -> Plugin:
    """Load a directory or its plugin.json. Entry must be an existing .py file."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Manifest/root symlinks are not allowed")
    if path.is_dir():
        path = path / "plugin.json"
    elif path.name != "plugin.json":
        raise ValueError("Manifest must be named plugin.json")
    root = path.parent.resolve()
    path = contained_path(root, "plugin.json")
    raw = read_json(path)
    _object(raw, {"id", "name", "version", "author", "description", "entry", "api_version",
                  "platforms", "requirements", "settings", "actions", "permissions", "events", "event_timeout"},
            "manifest")
    plugin_id = validate_id(raw.get("id"))
    api = raw.get("api_version", 1)
    if type(api) is not int or api != 1:
        raise ValueError("Only api_version 1 is supported")
    entry = raw.get("entry", "main.py")
    entry_path = contained_path(root, entry)
    if entry_path.suffix != ".py" or not entry_path.is_file():
        raise ValueError("Python entry file does not exist")
    platforms = _list(raw.get("platforms", []), "platforms", maximum=3)
    if any(not isinstance(p, str) or p not in ("darwin", "win32", "linux") for p in platforms):
        raise ValueError("Unsupported platform")
    if len(set(platforms)) != len(platforms):
        raise ValueError("Duplicate platform")
    requirements = tuple(validate_requirement(value) for value in
                         _list(raw.get("requirements", []), "requirements", maximum=128))
    package_names = [re.split(r"\[|==", value)[0].lower().replace("_", "-").replace(".", "-")
                     for value in requirements]
    if len(set(package_names)) != len(package_names):
        raise ValueError("Duplicate dependency")
    settings = _specs(raw.get("settings", []))
    actions = []
    total_fields = len(settings)
    for item in _list(raw.get("actions", []), "actions"):
        _object(item, {"key", "title", "parameters", "timeout"}, "action")
        key = _key(item.get("key"))
        if any(action.key == key for action in actions):
            raise ValueError("Duplicate action key")
        timeout = _number(item.get("timeout", 30), "timeout")
        if timeout <= 0 or timeout > 3600:
            raise ValueError("Action timeout must be in (0, 3600] seconds")
        parameters = _specs(item.get("parameters", []))
        total_fields += len(parameters)
        if total_fields > MAX_TOTAL_FIELDS:
            raise ValueError(f"Manifest exceeds {MAX_TOTAL_FIELDS} setting/parameter controls")
        actions.append(Action(key, _text(item.get("title", key), "action title", empty=False,
                                        maximum=MAX_LABEL_LENGTH), parameters, float(timeout)))
    permissions = []
    for item in _list(raw.get("permissions", []), "permissions"):
        _object(item, {"key", "title", "detail", "sensitive"}, "permission")
        key = _key(item.get("key"))
        if any(permission.key == key for permission in permissions):
            raise ValueError("Duplicate permission")
        permissions.append(Permission(_text(item.get("title", key), "permission title", empty=False,
                                            maximum=MAX_LABEL_LENGTH),
                                      _text(item.get("detail", ""), "permission detail", maximum=MAX_HELP_LENGTH),
                                      _bool(item.get("sensitive", False), "sensitive"), key))
    event_timeout = _number(raw.get("event_timeout", 120), "event_timeout")
    if event_timeout <= 0 or event_timeout > 3600:
        raise ValueError("Event timeout must be in (0, 3600] seconds")
    events = _list(raw.get("events", []), "events")
    if any(not isinstance(event, str) or len(event) > 128 or not re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+", event)
           for event in events) or len(set(events)) != len(events):
        raise ValueError("Events must be unique dotted subscription names")
    return Plugin(plugin_id, _text(raw.get("name"), "name", empty=False, maximum=MAX_LABEL_LENGTH),
                  _text(raw.get("version"), "version", empty=False, maximum=128),
                  _text(raw.get("author", ""), "author", maximum=MAX_LABEL_LENGTH),
                  _text(raw.get("description", ""), "description", maximum=MAX_HELP_LENGTH), settings=settings,
                  actions=tuple(actions), permissions=tuple(permissions), entry=entry,
                  api_version=api, platforms=tuple(platforms), requirements=requirements,
                  subscriptions=tuple(events), directory=root, event_timeout=float(event_timeout))
