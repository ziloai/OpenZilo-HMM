# SPDX-License-Identifier: MPL-2.0
"""Explicit installation of trusted, non-sandboxed Python plugins.

prepare_local/prepare_github only copy/download and validate; they never import
code or install dependencies. Candidates belong to the Installer instance that
prepared them. commit() replaces code with rollback on failure, preserves user
data and compatible saved settings/bindings, and disables every new/update
installation. Callers must stop a running plugin BEFORE committing/removing it.
Updates are always manual. GitHub downloads are pinned to a resolved 40-hex SHA.

install_dependencies() is a blocking, explicit worker operation (never call on
the UI thread). It creates a per-plugin venv even with no requirements. Direct
requirements must be exact pins; pip resolves wheel-only transitive dependencies
on the first explicit installation. A pip-freeze lock is persisted outside the
venv and reused for rebuilds with the same requirements/Python/platform.
python_executable() only returns a path; it neither creates a venv nor networks.
A ready marker is written only after installation succeeds. The runtime should
call dependencies_ready() and refuse startup until explicit installation.
"""
from __future__ import annotations

from concurrent.futures import CancelledError
from dataclasses import replace as dataclass_replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import queue
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

from .manifest import (contained_path, load_manifest, read_json, safe_relative,
                       validate_requirement, validate_values)
from .models import GitHubSource, Plugin
from .storage import PluginStore, atomic_json

MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
MAX_UNPACKED_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_FILES = 10000
MAX_COMPRESSION_RATIO = 200
NETWORK_TIMEOUT = 15
NETWORK_DEADLINE = 60
INSTALL_DEADLINE = 300  # one shared wall-clock budget for venv + pip + check + freeze
MAX_PROCESS_OUTPUT = 2 * 1024 * 1024
_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}\Z")


def _safe_url(url, host):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != host or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.fragment):
        raise ValueError("Refusing an unexpected download URL")
    return url


class _Redirects(HTTPRedirectHandler):
    def __init__(self, host):
        self.host = host

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _safe_url(newurl, self.host)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url, host, maximum, destination=None):
    """Bounded HTTPS fetch. Validate redirects BEFORE sending their requests."""
    _safe_url(url, host)
    request = Request(url, headers={"User-Agent": "OpenZilo-Studio-Plugins/1",
                                   "Accept": "application/vnd.github+json" if host == "api.github.com"
                                             else "application/zip"})
    started = time.monotonic()
    chunks, total = [], 0
    output = destination.open("wb") if destination is not None else None
    try:
        with build_opener(_Redirects(host)).open(request, timeout=NETWORK_TIMEOUT) as response:
            _safe_url(response.geturl(), host)
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdigit() or int(length) > maximum):
                raise ValueError("Download exceeds size limit")
            while True:
                if time.monotonic() - started > NETWORK_DEADLINE:
                    raise TimeoutError("Plugin download deadline exceeded")
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    raise ValueError("Download exceeds size limit")
                if output is None:
                    chunks.append(chunk)
                else:
                    output.write(chunk)
    finally:
        if output is not None:
            output.close()
    return b"".join(chunks)


def _api(path):
    raw = _download(f"https://api.github.com{path}", "api.github.com", 2 * 1024 * 1024)
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid GitHub API response") from exc
    if not isinstance(result, dict):
        raise ValueError("Invalid GitHub API response")
    return result


def _repo(value):
    if not isinstance(value, str):
        raise ValueError("Repository must be owner/name or a GitHub HTTPS URL")
    if value.startswith("https://"):
        parsed = urlsplit(_safe_url(value, "github.com"))
        if parsed.query:
            raise ValueError("Repository URL cannot have a query")
        value = parsed.path.strip("/")
    if value.endswith(".git"):
        value = value[:-4]
    if not _REPO.fullmatch(value) or any(part in (".", "..") for part in value.split("/")):
        raise ValueError("Repository must be owner/name")
    return value


def _copy_directory(source, target):
    count = total = 0
    target.mkdir(parents=True, exist_ok=True)
    for directory, dirs, files in os.walk(source, followlinks=False):
        relative = Path(directory).relative_to(source)
        for name in dirs + files:
            item = Path(directory) / name
            rel = (relative / name).as_posix()
            dest = contained_path(target, rel)
            mode = item.lstat().st_mode
            count += 1
            if count > MAX_FILES:
                raise ValueError("Too many plugin files")
            if stat.S_ISLNK(mode):
                raise ValueError("Plugin symlinks are not allowed")
            if stat.S_ISDIR(mode):
                dest.mkdir(exist_ok=True)
            elif stat.S_ISREG(mode):
                size = item.stat().st_size
                total += size
                if size > MAX_FILE_BYTES or total > MAX_UNPACKED_BYTES:
                    raise ValueError("Plugin files exceed size limit")
                # Bound the actual read too; a source file can grow during copying.
                with item.open("rb") as src, dest.open("xb") as dst:
                    copied = 0
                    while chunk := src.read(64 * 1024):
                        copied += len(chunk)
                        if copied > size:
                            raise ValueError("Source changed during copy")
                        dst.write(chunk)
            else:
                raise ValueError("Only regular files/directories are allowed")


def _extract_zip(source, target):
    if source.stat().st_size > MAX_DOWNLOAD_BYTES:
        raise ValueError("ZIP exceeds compressed size limit")
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_FILES:
            raise ValueError("Too many ZIP entries")
        total = 0
        seen = set()
        for info in entries:
            name = info.filename[:-1] if info.is_dir() else info.filename
            # ZipInfo truncates NUL names; inspect its original filename as well.
            if "\x00" in info.orig_filename:
                raise ValueError("Invalid ZIP filename")
            relative = safe_relative(name)
            folded = relative.as_posix().casefold()
            if folded in seen:
                raise ValueError("Duplicate/case-colliding ZIP path")
            seen.add(folded)
            mode = info.external_attr >> 16
            filetype = stat.S_IFMT(mode)
            if (filetype not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or info.flag_bits & 1
                    or (filetype == stat.S_IFDIR and not info.is_dir())
                    or (filetype == stat.S_IFREG and info.is_dir())):
                raise ValueError("ZIP symlinks/special files/encrypted entries are not allowed")
            total += info.file_size
            if (info.file_size > MAX_FILE_BYTES or total > MAX_UNPACKED_BYTES
                    or info.file_size > max(1, info.compress_size) * MAX_COMPRESSION_RATIO):
                raise ValueError("ZIP expansion exceeds limits")
            dest = contained_path(target, relative.as_posix())
            if info.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, dest.open("xb") as dst:
                copied = 0
                while chunk := src.read(64 * 1024):
                    copied += len(chunk)
                    if copied > info.file_size or copied > MAX_FILE_BYTES:
                        raise ValueError("ZIP entry exceeds declared size")
                    dst.write(chunk)


def _code_root(extracted):
    if (extracted / "plugin.json").is_file():
        return extracted
    children = list(extracted.iterdir())
    if len(children) == 1 and children[0].is_dir() and (children[0] / "plugin.json").is_file():
        return children[0]
    raise ValueError("ZIP must contain plugin.json at root or in one top-level directory")


class Installer:
    """Two-phase local/GitHub installation; all candidates require explicit commit."""

    def __init__(self, store: PluginStore):
        self.store = store
        self._candidates: dict[int, Path] = {}
        self._prepared_envs: dict[int, Path] = {}

    def _stage(self):
        staging = contained_path(self.store.root, ".staging")
        staging.mkdir(exist_ok=True, mode=0o700)
        return Path(tempfile.mkdtemp(prefix="plugin-", dir=staging))

    def prepare_local(self, source) -> Plugin:
        """Copy a local directory or ZIP into private staging and validate it."""
        source = Path(source).expanduser()
        if source.is_symlink():
            raise ValueError("Source cannot be a symlink")
        source = source.resolve(strict=True)
        if source.is_dir() and self.store.root.is_relative_to(source):
            raise ValueError("Source cannot contain the installation store")
        stage = self._stage()
        try:
            code = stage / "code"
            if source.is_dir():
                _copy_directory(source, code)
            elif source.is_file() and zipfile.is_zipfile(source):
                extracted = stage / "extracted"
                _extract_zip(source, extracted)
                _code_root(extracted).rename(code)
                if extracted.exists():
                    shutil.rmtree(extracted)
            else:
                raise ValueError("Source must be a directory or ZIP")
            candidate = load_manifest(code)
            candidate.source = str(source)
            self._candidates[id(candidate)] = stage
            return candidate
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    def prepare_github(self, repo, ref=None) -> Plugin:
        """Latest stable release (fallback default branch), or explicit ref, pinned to SHA.

        ref may be a tag, branch or commit. No automatic refresh is scheduled.
        Tests should mock _api/_download; this method is the explicit network path.
        """
        repo = _repo(repo)
        if ref is None:
            try:
                release = _api(f"/repos/{repo}/releases/latest")
                ref = release.get("tag_name")
                if not isinstance(ref, str) or not ref:
                    raise ValueError("Release response has no tag")
            except HTTPError as exc:
                if exc.code != 404:
                    raise
                ref = _api(f"/repos/{repo}").get("default_branch")
        if not isinstance(ref, str) or not ref or len(ref) > 200 or any(c.isspace() or ord(c) < 32 for c in ref):
            raise ValueError("Invalid GitHub ref")
        commit = _api(f"/repos/{repo}/commits/{quote(ref, safe='')}").get("sha")
        if not isinstance(commit, str) or not _SHA.fullmatch(commit):
            raise ValueError("GitHub did not resolve a full commit SHA")
        commit = commit.lower()
        stage = self._stage()
        try:
            archive = stage / "source.zip"
            _download(f"https://codeload.github.com/{repo}/zip/{commit}", "codeload.github.com",
                      MAX_DOWNLOAD_BYTES, archive)
            candidate = self.prepare_local(archive)
            candidate.source = f"https://github.com/{repo}"
            candidate.github = GitHubSource(repo, commit, datetime.now(timezone.utc).isoformat())
            return candidate
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def prepare_environment(self, candidate, progress_callback=None, cancel_event=None):
        """Build candidate dependencies in private staging before committing an update.

        Blocking worker operation; does not change candidate.directory or run
        plugin code. A failure/cancellation never touches the installed version.
        Existing matching dependency locks are reused. cleanup() owns all staged
        environment files. Only interpreter/module invocation is supported after
        relocation (activation scripts and wheel console-script shebangs may
        still refer to staging); the runtime always uses venv Python directly.
        """
        stage = self._candidates.get(id(candidate))
        if stage is None or candidate.directory != stage / "code":
            raise ValueError("Unknown or already committed candidate")
        fresh = load_manifest(stage / "code")
        if fresh.id != candidate.id:
            raise ValueError("Candidate id changed")
        preparation_deadline = time.monotonic() + INSTALL_DEADLINE
        _check_install(cancel_event, preparation_deadline)
        self._prepared_envs.pop(id(candidate), None)
        workspace = stage / "environment"
        if workspace.exists():
            shutil.rmtree(workspace)
        prepared_base = workspace / fresh.id
        prepared_code = prepared_base / "code"
        prepared_code.mkdir(parents=True)
        staged = dataclass_replace(fresh, directory=prepared_code)
        try:
            old_lock = contained_path(self.store.base(fresh), "requirements.lock.json")
            if old_lock.exists():
                shutil.copyfile(old_lock, prepared_base / "requirements.lock.json")
            install_dependencies(staged, progress_callback, cancel_event=cancel_event)
            _check_install(cancel_event, preparation_deadline)
            if not dependencies_ready(staged):
                raise RuntimeError("Prepared Python environment is not ready")
            self._prepared_envs[id(candidate)] = prepared_base
        except BaseException:
            shutil.rmtree(workspace, ignore_errors=True)
            raise

    def cleanup(self, candidate):
        """Discard this instance's candidate/environment; never remove installed data."""
        self._prepared_envs.pop(id(candidate), None)
        stage = self._candidates.pop(id(candidate), None)
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)

    def commit(self, candidate: Plugin, replace=False) -> Plugin:
        """Commit staged code, preserving compatible user configuration on updates.

        Schema-incompatible updates fail before touching old code/settings.
        Removed/changed actions with existing bindings must be resolved by the
        caller first. New settings use defaults. For updates the caller should
        explicitly prepare_environment() first; its environment and lock move
        with the code and are smoke-tested after relocation before state is saved.
        Any failure restores old code/environment/lock. Without a prepared venv,
        commit intentionally leaves an unprepared install (initial-install flow).
        """
        stage = self._candidates.get(id(candidate))
        if stage is None or candidate.directory != stage / "code":
            raise ValueError("Unknown or already committed candidate")
        # Re-read rather than trusting mutable declarations on a preview object.
        installed = load_manifest(stage / "code")
        if installed.id != candidate.id:
            raise ValueError("Candidate id changed")
        installed.source, installed.github = candidate.source, candidate.github
        installed.values = dict(candidate.values)
        prepared = self._prepared_envs.get(id(candidate))
        if prepared is not None:
            staged_plugin = dataclass_replace(installed, directory=prepared / "code")
            if not dependencies_ready(staged_plugin):
                raise ValueError("Prepared environment no longer matches the candidate manifest")
            if not contained_path(prepared, "requirements.lock.json").is_file():
                raise ValueError("Prepared environment has no dependency lock")
        with self.store._lock:
            base = self.store.base(installed)
            code = contained_path(base, "code")
            environment = contained_path(base, "venv")
            exists = code.exists()
            if exists and not replace:
                raise FileExistsError(f"Plugin {installed.id} is already installed")
            if exists:
                old = next((plugin for plugin in self.store.discover() if plugin.id == installed.id), None)
                if old is None:
                    raise ValueError("Existing installation is invalid; refusing to overwrite it")
                # Never migrate a saved credential into a non-secret field.
                old_specs = {spec.key: spec for spec in old.settings}
                for spec in installed.settings:
                    if spec.key in old_specs and spec.kind != old_specs[spec.key].kind:
                        raise ValueError("Update changes the type of a saved setting")
                old_actions = {action.key: action for action in old.actions}
                for action in installed.actions:
                    old_params = {spec.key: spec for spec in old_actions[action.key].parameters} if action.key in old_actions else {}
                    for spec in action.parameters:
                        if spec.key in old_params and spec.kind != old_params[spec.key].kind:
                            raise ValueError("Update changes the type of a saved action parameter")
                # Keep existing values where the setting still exists; new fields use defaults.
                old_values = {key: value for key, value in old.values.items()
                              if key in {spec.key for spec in installed.settings}}
                installed.values = validate_values(
                    tuple(dataclass_replace(spec, required=False) for spec in installed.settings),
                    old_values)
                installed.bindings = list(old.bindings)
                installed.recording_scope = old.recording_scope
                installed.recording_events_enabled = old.recording_events_enabled
            installed.enabled = False
            if installed.github is not None:
                installed.github.auto_update = False
            # Stage state validation before the first directory rename, including bindings.
            actions = {action.key: action for action in installed.actions}
            for binding in installed.bindings:
                if binding.action not in actions:
                    raise ValueError("Update removes an action used by a saved binding")
                validate_values(tuple(dataclass_replace(spec, required=False)
                                      for spec in actions[binding.action].parameters), binding.parameters)
            base.mkdir(parents=True, exist_ok=True, mode=0o700)
            backup = stage / "previous-code"
            old_venv = stage / "previous-venv"
            lock_path = contained_path(base, "requirements.lock.json")
            old_lock = stage / "previous-lock.json"
            moved_old = moved_new = moved_venv = False
            moved_prepared = moved_old_lock = moved_new_lock = False
            try:
                if exists:
                    os.replace(code, backup)
                    moved_old = True
                os.replace(stage / "code", code)
                moved_new = True
                if environment.exists():
                    os.replace(environment, old_venv)
                    moved_venv = True
                installed.directory = code
                if prepared is not None:
                    os.replace(prepared / "venv", environment)
                    moved_prepared = True
                    if lock_path.exists():
                        os.replace(lock_path, old_lock)
                        moved_old_lock = True
                    os.replace(prepared / "requirements.lock.json", lock_path)
                    moved_new_lock = True
                    # Venv Python locates pyvenv.cfg/site-packages by its NEW
                    # location. Never use the relocated pip/console shebangs.
                    _smoke_environment(installed, deadline=time.monotonic() + 30)
                    if not dependencies_ready(installed):
                        raise RuntimeError("Relocated Python environment is not ready")
                self.store.save(installed)
            except BaseException:
                if moved_new_lock:
                    os.replace(lock_path, prepared / "requirements.lock.json")
                if moved_old_lock:
                    os.replace(old_lock, lock_path)
                if moved_prepared:
                    os.replace(environment, prepared / "venv")
                if moved_venv:
                    os.replace(old_venv, environment)
                if moved_new:
                    os.replace(code, stage / "code")
                if moved_old:
                    os.replace(backup, code)
                raise
            candidate.directory = code
            self.cleanup(candidate)
            return installed


def python_executable(plugin: Plugin) -> Path:
    """Return the installed plugin's private interpreter path; never create it."""
    if plugin.directory is None:
        raise ValueError("Plugin has no installed code directory")
    code = Path(plugin.directory)
    if (code.name != "code" or code.parent.name != plugin.id
            or code.is_symlink() or code.parent.is_symlink()):
        raise ValueError("Plugin must have an installed code directory")
    environment = contained_path(code.parent, "venv")
    # POSIX venv interpreters are normally symlinks to the host Python: allow
    # that final symlink, but never a symlink for the venv directory itself.
    executable_dir = contained_path(environment, "Scripts" if os.name == "nt" else "bin")
    return executable_dir / ("python.exe" if os.name == "nt" else "python")


def _environment_signature(plugin):
    return {"api_version": plugin.api_version, "requirements": list(plugin.requirements),
            "python": list(sys.version_info[:2]), "platform": sys.platform,
            "architecture": platform.machine(), "implementation": sys.implementation.name}


def dependencies_ready(plugin: Plugin) -> bool:
    """False means the UI must offer explicit dependency/environment installation."""
    try:
        executable = python_executable(plugin)
        marker = contained_path(executable.parent.parent, ".studio-ready.json")
        return executable.is_file() and read_json(marker) == _environment_signature(plugin)
    except (OSError, ValueError):
        return False


def _check_install(cancel_event, deadline):
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError("Plugin environment installation was cancelled")
    if time.monotonic() >= deadline:
        raise TimeoutError("Plugin environment installation exceeded its deadline")


def _terminate_process_tree(process):
    """Terminate the isolated command group, including ensurepip/pip descendants."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            try:
                process.terminate()
            except OSError:
                pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        finally:
            # The parent may exit while a child still holds stdout open.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                try:
                    process.kill()
                except OSError:
                    pass
    else:
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            process.kill()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()


def _run_process(command, *, deadline, cancel_event=None, progress_callback=None,
                 env=None, capture=False):
    """Run one bounded command; a daemon reader never blocks cancellation checks.

    Log records and the queue are bounded even if a child never prints a newline.
    capture is only for small machine-readable output (pip freeze). Callbacks
    run on the caller's worker thread and must return promptly. POSIX process
    groups are terminated; Windows tree cleanup is best effort via taskkill.
    """
    _check_install(cancel_event, deadline)
    kwargs = {"start_new_session": True} if os.name == "posix" else {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, env=env, **kwargs)
    messages = queue.Queue(maxsize=128)
    stop = threading.Event()

    def send(message):
        while not stop.is_set():
            try:
                messages.put(message, timeout=0.05)
                return
            except queue.Full:
                pass

    def read_output():
        try:
            while not stop.is_set():
                data = process.stdout.readline(4096)
                if not data:
                    break
                send(data)
        except (OSError, ValueError) as exc:
            send(exc)
        finally:
            send(None)

    reader = threading.Thread(target=read_output, name="plugin-install-output", daemon=True)
    reader.start()
    output, size = [], 0
    finished = False
    try:
        while not finished or process.poll() is None:
            _check_install(cancel_event, deadline)
            try:
                message = messages.get(timeout=min(0.05, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if message is None:
                finished = True
                continue
            if isinstance(message, Exception):
                raise RuntimeError("Could not read installer output") from message
            text = message.decode("utf-8", errors="replace")
            if capture:
                size += len(message)
                if size > MAX_PROCESS_OUTPUT:
                    raise RuntimeError("Installer output exceeds its size limit")
                output.append(text)
            if progress_callback is not None:
                progress_callback(text.rstrip("\r\n"))
        _check_install(cancel_event, deadline)
        if process.returncode != 0:
            raise RuntimeError(f"Environment command failed (exit {process.returncode}); see installation log")
        return "".join(output)
    except BaseException:
        stop.set()
        _terminate_process_tree(process)
        raise
    finally:
        stop.set()
        reader.join(timeout=1)
        # Do not block closing a buffered pipe held by an intentionally detached
        # descendant. Such processes cannot be contained without an OS sandbox.
        if not reader.is_alive():
            process.stdout.close()


def _installer_environment():
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("PIP_", "PYTHON"))}
    environment["PIP_CONFIG_FILE"] = os.devnull
    return environment


def _smoke_environment(plugin, *, deadline, cancel_event=None):
    # Do not import the plugin or arbitrary dependency entry points.
    _run_process([str(python_executable(plugin)), "-I", "-c",
                  "import sys, json, pip; assert sys.prefix != sys.base_prefix"],
                 deadline=deadline, cancel_event=cancel_event, env=_installer_environment())


def install_dependencies(plugin: Plugin, progress_callback=None, *, cancel_event=None):
    """Explicit worker-only venv/wheel installation; callback receives log lines.

    No dependencies means ensurepip/venv only (no network). Direct requirements
    use fixed versions, PyPI wheels and --isolated; transitives are resolved at
    installation, not prelocked. ``requirements.lock.json`` stores pip freeze's
    exact resolution and is reused on subsequent rebuilds of the same manifest
    requirements/Python/platform. Source builds/setup.py never run. Failed
    installation leaves the environment NOT ready. This does not sandbox wheel
    contents or plugin execution; only install plugins you trust.

    cancel_event may be a threading.Event. Cancellation raises CancelledError;
    the entire venv/pip/check/freeze/smoke operation has a 300-second deadline
    (TimeoutError). All commands run in terminable process groups. Neither
    cancellation nor failure writes a ready marker. For updates, use
    Installer.prepare_environment() so the installed environment stays untouched.
    """
    deadline = time.monotonic() + INSTALL_DEADLINE
    _check_install(cancel_event, deadline)
    requirements = [validate_requirement(item) for item in plugin.requirements]
    executable = python_executable(plugin)
    environment = executable.parent.parent
    marker = contained_path(environment, ".studio-ready.json")
    lock_path = contained_path(environment.parent, "requirements.lock.json")
    signature = _environment_signature(plugin)
    requested = list(requirements)
    if requirements and lock_path.exists():
        locked = read_json(lock_path)
        if not isinstance(locked, dict):
            raise ValueError("Invalid dependency lock")
        if locked.get("signature") == signature:
            resolved = locked.get("resolved")
            if not isinstance(resolved, list) or not resolved:
                raise ValueError("Invalid dependency lock resolution")
            requested = [validate_requirement(item) for item in resolved]
            # Preserve direct pins too: a corrupted/stale lock must not relax them.
            requested = list(dict.fromkeys([*requirements, *requested]))
    marker.unlink(missing_ok=True)

    def progress(line):
        if progress_callback is not None:
            progress_callback(line)

    progress("Creating private Python environment (plugins are not sandboxed).")
    _check_install(cancel_event, deadline)
    # Rebuild instead of reusing possibly incompatible dependencies. Preserve code/data.
    if environment.exists():
        shutil.rmtree(environment)
    env = _installer_environment()
    _run_process([sys.executable, "-I", "-m", "venv", str(environment)],
                 deadline=deadline, cancel_event=cancel_event, progress_callback=progress, env=env)
    resolved = []
    if requirements:
        command = [str(executable), "-I", "-m", "pip", "--isolated", "--disable-pip-version-check",
                   "install", "--no-input", "--only-binary=:all:",
                   "--index-url", "https://pypi.org/simple", *requested]
        _run_process(command, deadline=deadline, cancel_event=cancel_event,
                     progress_callback=progress, env=env)
        prefix = [str(executable), "-I", "-m", "pip", "--isolated", "--disable-pip-version-check"]
        _run_process([*prefix, "check"], deadline=deadline, cancel_event=cancel_event,
                     progress_callback=progress, env=env)
        frozen = _run_process([*prefix, "freeze", "--all"], deadline=deadline,
                              cancel_event=cancel_event, env=env, capture=True)
        resolved = [validate_requirement(line.strip()) for line in frozen.splitlines()
                    if line.strip() and not line.startswith("#")]
        if not resolved:
            raise RuntimeError("pip freeze returned no dependency resolution")
    _smoke_environment(plugin, deadline=deadline, cancel_event=cancel_event)
    _check_install(cancel_event, deadline)
    atomic_json(lock_path, {"signature": signature, "resolved": resolved})
    _check_install(cancel_event, deadline)
    atomic_json(marker, signature)
    progress("Private Python environment is ready.")
