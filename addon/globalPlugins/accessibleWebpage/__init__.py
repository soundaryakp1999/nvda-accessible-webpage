"""Accessible Webpage: rebuilds inaccessible web pages with an LLM.

Flow: snapshot the browse-mode buffer -> ask Gemini for accessible HTML ->
show it in a modal viewer -> replay the user's interaction on the real page.
"""

import hashlib
import threading
from collections import OrderedDict

import addonHandler
import api
import core
import globalPluginHandler
import gui
import tones
import ui
import wx
from logHandler import log
from scriptHandler import script
from virtualBuffers import VirtualBuffer

from . import actions, live, llm, prompt, sanitizer, settings, snapshot, viewer

addonHandler.initTranslation()

CACHE_SIZE = 8


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	# Translators: input gestures category.
	scriptCategory = _("Accessible Webpage")

	def __init__(self):
		super().__init__()
		settings.register()
		self._busy = False
		self._cache = OrderedDict()

	def terminate(self):
		settings.unregister()
		super().terminate()

	@script(
		# Translators: input gesture description.
		description=_("Rebuilds the current web page as an accessible page"),
		gesture="kb:NVDA+shift+c",
	)
	def script_rebuildPage(self, gesture):
		snap = self._takeSnapshot()
		if snap:
			self._process(snap, None)

	@script(
		# Translators: input gesture description.
		description=_("Asks what you want to do, then rebuilds the current web page around that goal"),
		gesture="kb:NVDA+shift+control+c",
	)
	def script_rebuildPageWithGoal(self, gesture):
		snap = self._takeSnapshot()
		if not snap:
			return
		dialog = wx.TextEntryDialog(
			gui.mainFrame,
			# Translators: prompt asking for the user's goal on the page.
			_("What do you want to do on this page? For example: log in, pay the bill, find the contact form."),
			# Translators: title of the goal dialog.
			_("Accessible Webpage"),
		)

		def onResult(result):
			goal = dialog.GetValue().strip()
			if result == wx.ID_OK:
				self._process(snap, goal or None)

		gui.runScriptModalDialog(dialog, onResult)

	def _takeSnapshot(self):
		if self._busy:
			# Translators: reported when a rebuild is already in progress.
			ui.message(_("Still working on the previous request"))
			return None
		ti = api.getFocusObject().treeInterceptor
		if not isinstance(ti, VirtualBuffer):
			# Translators: reported when the focus is not in a web page.
			ui.message(_("Not in a web page"))
			return None
		try:
			return snapshot.take(ti, settings.effectiveMaxChars())
		except snapshot.SnapshotError as e:
			ui.message(str(e))
		except Exception:
			log.exception("Snapshot failed")
			# Translators: reported when the page could not be read.
			ui.message(_("Could not read this page"))
		return None

	def _process(self, snap, goal):
		provider, error = settings.makeProvider()
		if not provider:
			ui.message(error)
			return
		cacheKey = hashlib.sha256(
			"\0".join((settings.providerId(), goal or "", snap.url, snap.text)).encode("utf-8")
		).hexdigest()
		cached = self._cache.get(cacheKey)
		if cached:
			wx.CallAfter(self._show, snap, cached)
			return
		self._busy = True
		# Translators: reported while the page is being rebuilt.
		ui.message(_("Rebuilding page, please wait"))
		core.callLater(1500, self._progressBeep)
		threading.Thread(
			target=self._worker, args=(provider, snap, goal, cacheKey), daemon=True, name="accessibleWebpage"
		).start()

	def _progressBeep(self):
		if self._busy:
			tones.beep(440, 30)
			core.callLater(1500, self._progressBeep)

	def _worker(self, provider, snap, goal, cacheKey):
		try:
			result = provider.generateJson(
				prompt.SYSTEM_PROMPT, prompt.buildUserMessage(snap, goal), prompt.RESPONSE_SCHEMA
			)
			if not isinstance(result, dict) or not result.get("html"):
				raise llm.LLMError("The model returned an empty page")
		except Exception as e:
			if not isinstance(e, llm.LLMError):
				log.exception("Rebuild failed")
			wx.CallAfter(self._onError, e)
			return
		wx.CallAfter(self._onResult, snap, cacheKey, result)

	def _onError(self, error):
		self._busy = False
		# Translators: reported when the rebuild failed; {error} is the reason.
		ui.message(_("Rebuild failed: {error}").format(error=error))

	def _onResult(self, snap, cacheKey, result):
		self._busy = False
		self._cache[cacheKey] = result
		while len(self._cache) > CACHE_SIZE:
			self._cache.popitem(last=False)
		self._show(snap, result)

	def _show(self, snap, result):
		content = sanitizer.sanitize(result.get("html", ""), snap.refs.keys())
		title = result.get("title") or snap.title or _("Accessible Webpage")
		page = viewer.renderPage(title, result.get("summary", ""), result.get("language"), content)
		provider, _error = settings.makeProvider()
		session = live.LiveSession(snap, provider, settings.effectiveMaxChars())
		server = live.ViewerServer(page, session.expand)
		try:
			response = viewer.show(server.url)
		finally:
			session.close()
			server.stop()
		if not response:
			return
		# Live expands may have re-snapshotted the page; its registry knows every ref shown.
		snap = session.snapshot
		refs = snap.refs
		response["fields"] = [f for f in response.get("fields") or [] if f.get("ref") in refs]
		if response.get("action") == "click" and response.get("ref") not in refs:
			response["action"] = "apply"
		if response.get("action") != "click" and not response["fields"]:
			# Translators: reported when the user asked to apply but changed nothing.
			ui.message(_("Nothing to fill in"))
			return
		actions.perform(snap, response, settings.conf()["clickMethod"])
