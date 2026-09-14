"""Bounded JSONL client for the installed, experimental Linux app-server."""

from collections import deque
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
from typing import NewType

from .desktop import executable, linux_only, project_roots
from .workspace import HandoffError, result


ServerProjectId = NewType("ServerProjectId", str)
MAX_LINE_BYTES = 1024 * 1024


class AppServer:
    def __init__(self, binary=None, timeout=120):
        linux_only()
        if not 1 <= timeout <= 600:
            raise HandoffError("App-server timeout must be between 1 and 600 seconds.")
        self.binary = executable(binary, (
            Path(root) / "resources/codex" for root in
            ("/usr/lib/chatgpt", "/usr/lib/ChatGPT", "/opt/chatgpt", "/opt/ChatGPT")
        ))
        self.timeout = timeout
        self.process = None
        self.selector = selectors.DefaultSelector()
        self.buffer = bytearray()
        self.events = deque()
        self.next_id = 0
        self.submission_attempted = False
        self.facts = {}

    def __enter__(self):
        self.deadline = time.monotonic() + self.timeout
        try:
            self.process = subprocess.Popen(
                [str(self.binary), "app-server", "--stdio"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0, start_new_session=True,
            )
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                os.set_blocking(stream.fileno(), False)
            self.selector.register(self.process.stdout, selectors.EVENT_READ, "stdout")
            self.selector.register(self.process.stderr, selectors.EVENT_READ, "stderr")
            self.request("initialize", {
                "clientInfo": {"name": "workspaces", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            })
            self.send({"jsonrpc": "2.0", "method": "initialized"})
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.selector.close()
        if self.process is None:
            return
        # Only the process group created by this request is eligible for cleanup.
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise HandoffError("App-server deadline exceeded; no automatic retry is permitted.")
        return remaining

    def send(self, message):
        encoded = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        if len(encoded) > MAX_LINE_BYTES:
            raise HandoffError("App-server request exceeds the JSONL size limit.")
        data = memoryview(encoded)
        # Mark uncertainty before the first byte: a partial write cannot be retried safely.
        if message.get("method") == "turn/start":
            self.submission_attempted = True
            self.facts["submission_attempted"] = True
        with selectors.DefaultSelector() as writer:
            writer.register(self.process.stdin, selectors.EVENT_WRITE)
            while data:
                if not writer.select(self.remaining()):
                    raise HandoffError("Timed out writing to app-server.")
                try:
                    count = os.write(self.process.stdin.fileno(), data)
                except BlockingIOError:
                    continue
                if not count:
                    raise HandoffError("App-server input closed.")
                data = data[count:]

    def receive(self):
        while True:
            self.remaining()
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self.buffer[:newline])
                del self.buffer[:newline + 1]
                if len(line) > MAX_LINE_BYTES:
                    raise HandoffError("App-server JSONL line exceeds the size limit.")
                try:
                    message = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise HandoffError("Malformed app-server JSONL; integration stopped.") from error
                if not isinstance(message, dict):
                    raise HandoffError("App-server messages must be JSON objects.")
                return message
            if len(self.buffer) > MAX_LINE_BYTES:
                raise HandoffError("Unterminated app-server JSONL line exceeds the size limit.")
            ready = self.selector.select(self.remaining())
            if not ready:
                raise HandoffError("Timed out waiting for app-server.")
            for key, _ in ready:
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    self.selector.unregister(key.fileobj)
                    if key.data == "stdout":
                        raise HandoffError("App-server closed before the operation completed.")
                elif key.data == "stdout":
                    self.buffer.extend(chunk)
                # Drain stderr without retaining or returning possible credentials.

    def notification(self, message):
        if "method" not in message:
            raise HandoffError("Unexpected app-server response.")
        if "id" in message:
            self.send({"jsonrpc": "2.0", "id": message["id"], "error": {
                "code": -32601, "message": "Interactive client requests are unavailable.",
            }})
            raise HandoffError("App-server requested an unsupported interactive client operation.")
        if message["method"] in ("item/completed", "turn/completed"):
            if len(self.events) >= 4096:
                raise HandoffError("Too many pending app-server notifications.")
            self.events.append(message)

    def request(self, method, params):
        self.next_id += 1
        request_id = self.next_id
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            message = self.receive()
            if "method" in message:
                self.notification(message)
                continue
            if message.get("id") != request_id:
                raise HandoffError("Uncorrelated app-server response.")
            if "error" in message:
                error = message["error"]
                code = error.get("code") if isinstance(error, dict) else None
                code = code if isinstance(code, int) else "unknown"
                raise HandoffError(f"App-server {method} failed, code {code}. Check capability and authentication in the app.")
            value = message.get("result")
            if not isinstance(value, dict):
                raise HandoffError(f"Malformed {method} result.")
            return value

    def find_project(self, repository):
        cursor = None
        seen = set()
        matches = []
        for _ in range(1000):
            params = {"limit": 100}
            if cursor is not None:
                params["cursor"] = cursor
            page = self.request("project/list", params)
            records = page.get("data", page.get("projects"))
            if not isinstance(records, list):
                raise HandoffError("Unsupported project/list response shape.")
            for record in records:
                if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"]:
                    raise HandoffError("Malformed server project record.")
                if project_roots(record) == (repository,):
                    matches.append(ServerProjectId(record["id"]))
            cursor = page.get("nextCursor")
            if cursor is None:
                if len(set(matches)) > 1:
                    raise HandoffError("Multiple server projects have the same root; resolve the ambiguity in the app.")
                return matches[0] if matches else None
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise HandoffError("Malformed or repeated project/list cursor.")
            seen.add(cursor)
        raise HandoffError("project/list pagination limit exceeded.")

    def ensure_server_only(self, workspace):
        project_id = self.find_project(workspace.project_root)
        if project_id is None:
            identity = json.dumps([workspace.workspace_id, str(workspace.project_root)])
            self.request("project/create", {
                "name": workspace.workspace_id,
                "roots": [str(workspace.project_root)],
                "idempotencyKey": "workspaces-" + hashlib.sha256(identity.encode()).hexdigest(),
            })
            project_id = self.find_project(workspace.project_root)
        if project_id is None:
            raise HandoffError("Server project was not observed after creation.")
        return result("server-only", server_only=True, desktop_project_menu="absent",
                      server_project_id=project_id, server_project_verified=True,
                      verification_required=False)

    def submit(self, workspace, desktop_project):
        self.facts = desktop_project.facts(workspace)
        project_id = self.find_project(workspace.project_root)
        if project_id is None:
            raise HandoffError("No exact single-root server project exists. Bootstrap and observe the desktop project first.")
        self.facts.update(server_project_verified=True, server_project_id=project_id)
        started = self.request("thread/start", {
            "cwd": str(workspace.project_root),
            "projectId": project_id,
            "sandbox": "workspace-write" if workspace.task else "read-only",
            "approvalPolicy": "never",
        })
        thread = started.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str) or not thread["id"]:
            raise HandoffError("thread/start returned no usable thread ID.")
        thread_id = thread["id"]
        self.facts.update(thread_started=True, thread_id=thread_id)
        if thread.get("projectId") != project_id:
            raise HandoffError("thread/start did not confirm the expected server project assignment.")
        self.facts["workspace_paths_supplied"] = True
        turn = self.request("turn/start", {
            "threadId": thread_id,
            "input": [{"type": "text", "text": workspace.prompt}],
        }).get("turn")
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str) or not turn["id"]:
            raise HandoffError("turn/start returned no usable turn ID.")
        self.facts.update(turn_id=turn["id"], task_created=True)
        assistant_text = []
        while True:
            self.remaining()
            if not self.events:
                self.notification(self.receive())
                continue
            event = self.events.popleft()
            params = event.get("params")
            if not isinstance(params, dict):
                raise HandoffError("Malformed completion notification.")
            if params.get("threadId") != thread_id:
                continue
            if event["method"] == "item/completed" and params.get("turnId") == turn["id"]:
                item = params.get("item")
                if isinstance(item, dict) and item.get("type") == "agentMessage":
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        assistant_text.append(text)
            if event["method"] == "turn/completed":
                completed = params.get("turn")
                if not isinstance(completed, dict) or completed.get("id") != turn["id"]:
                    continue
                self.facts.update(turn_completed=True, turn_status=completed.get("status"),
                                  assistant_result_verified=bool(assistant_text))
                if completed.get("status") != "completed":
                    return result("unavailable", **self.facts,
                                  reason="The submitted turn ended without success. Inspect the task; do not resubmit automatically.")
                if not assistant_text:
                    return result("unavailable", **self.facts,
                                  reason="Turn completed without an observed nonempty assistant result.")
                return result(
                    "auto-submitted-desktop-association-unknown", **self.facts,
                    assistant_text="\n".join(assistant_text),
                    reason="Execution completed with the server project assignment. Desktop task association still needs a host observation.",
                )


def submit(workspace, project, *, binary=None, timeout=120):
    server = None
    try:
        server = AppServer(binary, timeout)
        server.facts = project.facts(workspace)
        with server:
            return server.submit(workspace, project)
    except (HandoffError, OSError, KeyboardInterrupt) as error:
        uncertain = server is not None and server.submission_attempted
        facts = server.facts if server is not None else project.facts(workspace)
        reason = str(error) if isinstance(error, HandoffError) else "App-server transport interrupted. Inspect the app before an explicit retry."
        return result("submission-unknown" if uncertain else "unavailable", **facts, reason=reason)
