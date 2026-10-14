"""Shows the rebuilt page in a modal MSHTML dialog and returns the user's interaction.

This is the same mechanism NVDA uses for browseable messages, but modal: the
page's script sets window.returnValue to a JSON description of what the user
did, which ShowHTMLDialogEx hands back to us. The page is loaded from the
live server (see live.py) so it can also talk to NVDA while open.

Must be called from the wx main loop (wx.CallAfter), never from inside a
script, or NVDA's core pump cannot run while the dialog is open.
"""

import json
import os
from ctypes import POINTER, byref, c_wchar_p, windll
from html import escape

import addonHandler
import gui
from comtypes import IUnknown
from comtypes.automation import VARIANT
from logHandler import log

addonHandler.initTranslation()

URL_MK_UNIFORM = 1
HTMLDLG_MODAL = 0x20
DIALOG_OPTIONS = "resizable:yes;help:no;status:no;scroll:yes;dialogWidth:1000px;dialogHeight:750px"
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "viewer.html")


def renderPage(title, summary, language, content, hasMore=False):
	"""hasMore: show the Load more button, for long pages rebuilt in parts."""
	with open(TEMPLATE_PATH, encoding="utf-8") as f:
		page = f.read()
	replacements = {
		"{{LANG}}": escape(language or "en"),
		"{{TITLE}}": escape(title),
		"{{SUMMARY}}": escape(summary),
		# Translators: note shown under the summary of the rebuilt page.
		"{{NOTE}}": escape(_("AI-generated accessible view. Buttons that show or hide content work right here; other controls act on the original page. Press Escape to close.")),
		# Translators: label of the region holding the viewer's own buttons.
		"{{TOOLBAR}}": escape(_("Viewer actions")),
		# Translators: button in the viewer that fills the original page without clicking anything.
		"{{APPLY}}": escape(_("Fill in the original page without submitting")),
		# Translators: button that closes the viewer.
		"{{CLOSE}}": escape(_("Close")),
		# Translators: button at the end of a long page that rebuilds the next part of it.
		"{{MORE}}": escape(_("Load more")),
		"{{MOREHIDDEN}}": "" if hasMore else " hidden",
	}
	for key, value in replacements.items():
		page = page.replace(key, value)
	# Content last, so placeholders inside model output are never expanded.
	return page.replace("{{CONTENT}}", content)


def show(url):
	"""Returns the interaction dict, or None if the user closed the viewer."""
	moniker = POINTER(IUnknown)()
	windll.urlmon.CreateURLMonikerEx(0, c_wchar_p(url), byref(moniker), URL_MK_UNIFORM)
	result = VARIANT()
	gui.mainFrame.prePopup()
	try:
		windll.mshtml.ShowHTMLDialogEx(
			gui.mainFrame.Handle, moniker, HTMLDLG_MODAL, None, c_wchar_p(DIALOG_OPTIONS), byref(result)
		)
	finally:
		gui.mainFrame.postPopup()
	value = result.value
	if not value or not isinstance(value, str):
		return None
	try:
		return json.loads(value)
	except ValueError:
		log.error("Viewer returned invalid JSON: %r" % value)
		return None
