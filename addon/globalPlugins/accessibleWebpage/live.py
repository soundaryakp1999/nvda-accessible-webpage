"""Live channel between the open viewer and NVDA.

The viewer is served from a loopback HTTP server so its script can call back
into NVDA while the dialog stays open. That lets "expand" style controls whose
content is not in the snapshot be operated on the original page in the
background, with only the newly revealed content rebuilt and inserted in place.

Uses raw sockets rather than http.server, which NVDA's bundled Python may not ship.
"""

import difflib
import json
import re
import secrets
import socket
import threading
import time

import addonHandler
import queueHandler
import tones
import ui
from logHandler import log

from . import prompt, sanitizer, snapshot

addonHandler.initTranslation()

MAX_REQUEST = 1024 * 1024
SETTLE_INTERVAL = 0.4
SETTLE_TIMEOUT = 4.0
_CLOSING_TAG = re.compile(r"\s*</\w+>\s*$")


def callOnMain(func, timeout=15):
	"""Runs func on NVDA's main thread and returns its result. Call only from a background thread."""
	done = threading.Event()
	box = {}

	def run():
		try:
			box["result"] = func()
		except Exception as e:
			box["error"] = e
		finally:
			done.set()

	queueHandler.queueFunction(queueHandler.eventQueue, run)
	if not done.wait(timeout):
		raise TimeoutError("NVDA main thread did not respond")
	if "error" in box:
		raise box["error"]
	return box.get("result")


def speak(text):
	queueHandler.queueFunction(queueHandler.eventQueue, ui.message, text)


class _Beeper:
	"""Progress beeps while a background request runs."""

	def __enter__(self):
		self._stop = threading.Event()
		threading.Thread(target=self._run, daemon=True).start()
		return self

	def _run(self):
		while not self._stop.wait(1.5):
			queueHandler.queueFunction(queueHandler.eventQueue, tones.beep, 440, 30)

	def __exit__(self, *exc):
		self._stop.set()


def addedLines(before, after, ref):
	"""Snapshot lines that are new or changed in after, excluding the activated control itself."""
	a, b = before.split("\n"), after.split("\n")
	marker = 'ref="%s"' % ref
	out = []
	for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
		if tag in ("insert", "replace"):
			out.extend(line for line in b[j1:j2] if marker not in line and not _CLOSING_TAG.match(line))
	return out


class LiveSession:
	def __init__(self, snap, provider, maxChars):
		self.snapshot = snap
		self.provider = provider
		self.maxChars = maxChars
		self.closed = False
		self._lock = threading.Lock()

	def close(self):
		self.closed = True

	def expand(self, ref):
		"""Runs on a server thread. Returns {"html": ...}; the result is spoken by NVDA."""
		with self._lock:
			if self.closed or ref not in self.snapshot.refs:
				return {"html": ""}
			if self.provider is None:
				# Translators: spoken when expanding is impossible because no AI provider is configured.
				speak(_("No AI provider is configured"))
				return {"html": ""}
			# Translators: spoken while an expandable control is operated on the original page.
			speak(_("Expanding"))
			if not callOnMain(lambda: self._activate(ref)):
				# Translators: spoken when an expandable control could not be operated.
				speak(_("Could not expand this on the page. Try rebuilding the page."))
				return {"html": ""}
			newSnap, added = self._waitForChanges(ref)
			self.snapshot = newSnap
			if not added:
				# Translators: spoken when activating a control revealed nothing new.
				speak(_("Nothing new appeared on the page"))
				return {"html": ""}
			with _Beeper():
				result = self.provider.generateJson(
					prompt.FRAGMENT_SYSTEM_PROMPT,
					prompt.buildFragmentMessage(newSnap, ref, added),
					prompt.FRAGMENT_SCHEMA,
				)
			if self.closed:
				return {"html": ""}
			html = sanitizer.sanitize(result.get("html", ""), newSnap.refs.keys())
			# Translators: default announcement after content was expanded.
			speak(result.get("announcement") or _("Expanded"))
			return {"html": html}

	def _activate(self, ref):
		obj = self.snapshot.resolve(ref)
		if obj is None:
			return False
		# Only the accessibility action works here: the viewer covers the page, so a mouse click would hit it.
		try:
			obj.doAction()
			return True
		except Exception:
			log.debugWarning("doAction failed for %s" % ref, exc_info=True)
			return False

	def _waitForChanges(self, ref):
		"""Re-snapshots until the page stops changing, keeping refs stable via the shared registry."""
		before = self.snapshot.text
		ti, registry = self.snapshot.treeInterceptor, self.snapshot.registry
		latest, previous = self.snapshot, None
		deadline = time.monotonic() + SETTLE_TIMEOUT
		while time.monotonic() < deadline:
			time.sleep(SETTLE_INTERVAL)
			latest = callOnMain(lambda: snapshot.take(ti, self.maxChars, registry))
			added = addedLines(before, latest.text, ref)
			if added and added == previous:
				break
			previous = added
		return latest, previous or []


class ViewerServer:
	"""Serves the viewer page and its expand endpoint on 127.0.0.1, behind a random token."""

	def __init__(self, page, onExpand):
		self._page = page.encode("utf-8")
		self._onExpand = onExpand
		self._token = secrets.token_urlsafe(16)
		self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		self._sock.bind(("127.0.0.1", 0))
		self._sock.listen(8)
		self.url = "http://127.0.0.1:%d/%s/view" % (self._sock.getsockname()[1], self._token)
		threading.Thread(target=self._serve, daemon=True, name="accessibleWebpage-server").start()

	def stop(self):
		try:
			self._sock.close()
		except OSError:
			pass

	def _serve(self):
		while True:
			try:
				conn, _addr = self._sock.accept()
			except OSError:
				return
			threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

	def _handle(self, conn):
		with conn:
			try:
				conn.settimeout(30)
				method, path, body = self._read(conn)
				status, contentType, payload = self._route(method, path, body)
			except Exception:
				log.debugWarning("Viewer server request failed", exc_info=True)
				status, contentType, payload = "500 Internal Server Error", "text/plain", b""
			head = (
				"HTTP/1.1 %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
				"Cache-Control: no-store\r\nX-UA-Compatible: IE=edge\r\nConnection: close\r\n\r\n"
				% (status, contentType, len(payload))
			)
			try:
				conn.sendall(head.encode("latin-1") + payload)
			except OSError:
				pass

	@staticmethod
	def _read(conn):
		data = b""
		while b"\r\n\r\n" not in data:
			chunk = conn.recv(65536)
			if not chunk or len(data) > MAX_REQUEST:
				raise ValueError("Incomplete request")
			data += chunk
		head, body = data.split(b"\r\n\r\n", 1)
		lines = head.decode("latin-1").split("\r\n")
		method, path, _version = lines[0].split(" ", 2)
		length = 0
		for line in lines[1:]:
			name, _sep, value = line.partition(":")
			if name.strip().lower() == "content-length":
				length = int(value.strip())
		if length > MAX_REQUEST:
			raise ValueError("Request too large")
		while len(body) < length:
			chunk = conn.recv(65536)
			if not chunk:
				break
			body += chunk
		return method, path, body[:length]

	def _route(self, method, path, body):
		if method == "GET" and path == "/%s/view" % self._token:
			return "200 OK", "text/html; charset=utf-8", self._page
		if method == "POST" and path == "/%s/expand" % self._token:
			ref = json.loads(body.decode("utf-8") or "{}").get("ref")
			try:
				result = self._onExpand(ref)
			except Exception as e:
				log.debugWarning("Expand failed", exc_info=True)
				# Translators: spoken when expanding failed; {error} is the reason.
				speak(_("Expanding failed: {error}").format(error=e))
				result = {"html": ""}
			return "200 OK", "application/json; charset=utf-8", json.dumps(result).encode("utf-8")
		return "404 Not Found", "text/plain", b""
