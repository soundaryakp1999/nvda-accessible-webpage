"""The page tree: an NVDA dialog listing a page's sections and controls as a tree view.

Pressing Enter on a control closes the dialog and puts NVDA's browse mode cursor on
that element in the original page (see actions.moveTo), where the user operates it
with NVDA's own commands. Nothing is clicked from outside the page, and the model only
writes short labels instead of a whole HTML page.
"""

import threading

import addonHandler
import gui
import ui
import wx
from gui import guiHelper
from logHandler import log

from . import live, prompt, snapshot

addonHandler.initTranslation()

MAX_LEVEL = 4
KIND_LABELS = {
	# Translators: kind of a page tree item.
	"link": _("link"),
	# Translators: kind of a page tree item.
	"button": _("button"),
	# Translators: kind of a page tree item.
	"field": _("edit field"),
	# Translators: kind of a page tree item.
	"checkbox": _("check box"),
	# Translators: kind of a page tree item.
	"radio": _("radio button"),
	# Translators: kind of a page tree item.
	"select": _("combo box"),
	# Translators: kind of a page tree item.
	"menu": _("menu button"),
	# Translators: kind of a page tree item.
	"tab": _("tab"),
}


def normalizeNodes(raw, validRefs):
	"""Turns the model's nodes into {"level", "ref", "text"} dicts that make a valid tree.

	Unknown refs are dropped, and a control that loses its ref becomes plain text, since the
	user couldn't reach it. Levels are clamped so that each node is at most one level below
	the one before.
	"""
	nodes = []
	previous = 0
	for node in raw or []:
		if not isinstance(node, dict):
			continue
		label = " ".join(str(node.get("label") or "").split())
		if not label:
			continue
		kind = node.get("kind") if node.get("kind") in prompt.TREE_KINDS else "text"
		ref = node.get("ref") if node.get("ref") in validRefs else ""
		if kind in KIND_LABELS and not ref:
			kind = "text"
		try:
			level = int(node.get("level") or 1)
		except (TypeError, ValueError):
			level = 1
		level = max(1, min(level, previous + 1, MAX_LEVEL))
		previous = level
		text = "%s, %s" % (label, KIND_LABELS[kind]) if kind in KIND_LABELS else label
		nodes.append({"level": level, "ref": ref, "text": text})
	return nodes


def leftOutNodes(snap, lines, nodes):
	"""A group of the controls in lines that the tree's nodes left out, so nothing on the page is lost."""
	items = snapshot.leftOut(snap, lines, {n["ref"] for n in nodes if n["ref"]})
	if not items:
		return []
	# Translators: page tree group of controls the model left out; {count} is how many.
	out = [{"level": 1, "ref": "", "text": _("Other controls on the page ({count})").format(count=len(items))}]
	for ref, label, isLink in items:
		out.append({"level": 2, "ref": ref, "text": "%s, %s" % (label, KIND_LABELS["link" if isLink else "button"])})
	return out


class MoreLoader:
	"""Loads the rest of a long page into the tree, one part at a time, in the background."""

	def __init__(self, snap, provider, partChars, pending):
		self.snapshot = snap
		self.provider = provider
		self.partChars = partChars
		self.pending = list(pending)
		self.busy = False

	@property
	def hasMore(self):
		return bool(self.pending)

	def load(self, onDone):
		"""Calls onDone(nodes, error) on the main thread."""
		self.busy = True
		part, rest, context = snapshot.splitPart(self.pending, self.partChars)
		threading.Thread(target=self._run, args=(part, rest, context, onDone), daemon=True).start()

	def _run(self, part, rest, context, onDone):
		nodes, error = [], None
		try:
			with live._Beeper():
				result = self.provider.generateJson(
					prompt.TREE_MORE_SYSTEM_PROMPT,
					prompt.buildMoreMessage(self.snapshot, part, context),
					prompt.TREE_MORE_SCHEMA,
				)
			nodes = normalizeNodes(result.get("nodes"), self.snapshot.refs)
			nodes += leftOutNodes(self.snapshot, part, nodes)
			self.pending = rest
		except Exception as e:
			log.debugWarning("Loading more of the tree failed", exc_info=True)
			error = e
		self.busy = False
		wx.CallAfter(onDone, nodes, error)


class PageTreeDialog(wx.Dialog):
	def __init__(self, parent, title, summary, nodes, loader=None):
		# Translators: title of the page tree dialog; {title} is the page title.
		super().__init__(parent, title=_("Page tree: {title}").format(title=title), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
		self.chosenRef = None
		self.loader = loader
		mainSizer = wx.BoxSizer(wx.VERTICAL)
		helper = guiHelper.BoxSizerHelper(self, orientation=wx.VERTICAL)
		self.summary = helper.addLabeledControl(
			# Translators: label of the page summary in the page tree dialog.
			_("&Summary:"),
			wx.TextCtrl,
			value=summary,
			style=wx.TE_MULTILINE | wx.TE_READONLY,
			size=(640, 70),
		)
		self.tree = helper.addLabeledControl(
			# Translators: label of the tree view in the page tree dialog.
			_("&Page:"),
			wx.TreeCtrl,
			style=wx.TR_HAS_BUTTONS | wx.TR_HIDE_ROOT | wx.TR_LINES_AT_ROOT | wx.TR_SINGLE,
			size=(640, 420),
		)
		self.root = self.tree.AddRoot("")
		buttons = guiHelper.ButtonHelper(wx.HORIZONTAL)
		# Translators: button in the page tree dialog that moves to the selected element on the page.
		self.goButton = buttons.addButton(self, id=wx.ID_OK, label=_("&Go to on page"))
		# Translators: button in the page tree dialog that adds the next part of a long page.
		self.moreButton = buttons.addButton(self, label=_("Load &more"))
		# Translators: button that closes the page tree dialog.
		buttons.addButton(self, id=wx.ID_CANCEL, label=_("&Close"))
		helper.addDialogDismissButtons(buttons)
		mainSizer.Add(helper.sizer, border=guiHelper.BORDER_FOR_DIALOGS, flag=wx.ALL | wx.EXPAND, proportion=1)
		self.SetSizer(mainSizer)
		mainSizer.Fit(self)
		self.CentreOnScreen()

		self.goButton.SetDefault()
		self.goButton.Bind(wx.EVT_BUTTON, lambda evt: self._go(self.tree.GetSelection()))
		self.moreButton.Bind(wx.EVT_BUTTON, lambda evt: self._loadMore())
		self.moreButton.Show(bool(loader and loader.hasMore))
		self.tree.Bind(wx.EVT_TREE_ITEM_ACTIVATED, lambda evt: self._go(evt.GetItem()))

		first = self._addNodes(nodes)
		if first:
			self.tree.SelectItem(first)
		self.tree.SetFocus()

	def _addNodes(self, nodes):
		"""Appends nodes at the end of the tree and returns the first new item."""
		stack = [(0, self.root)]
		first = None
		for node in nodes:
			while stack[-1][0] >= node["level"]:
				stack.pop()
			item = self.tree.AppendItem(stack[-1][1], node["text"])
			self.tree.SetItemData(item, node["ref"] or None)
			stack.append((node["level"], item))
			first = first or item
		return first

	def _go(self, item):
		if not item or not item.IsOk():
			return
		ref = self.tree.GetItemData(item)
		if ref:
			self.chosenRef = ref
			self.EndModal(wx.ID_OK)
		elif self.tree.ItemHasChildren(item):
			self.tree.Toggle(item)
		else:
			# Translators: reported when Enter is pressed on a page tree item that is only text.
			ui.message(_("This is text from the page, not something you can operate."))

	def _loadMore(self):
		if not self.loader or self.loader.busy or not self.loader.hasMore:
			return
		self.moreButton.Disable()
		# Translators: reported while the next part of a long page is added to the page tree.
		ui.message(_("Loading more"))
		self.loader.load(self._onMoreLoaded)

	def _onMoreLoaded(self, nodes, error):
		if not self:  # The dialog was closed meanwhile.
			return
		self.moreButton.Enable()
		self.moreButton.Show(self.loader.hasMore)
		self.Layout()
		if error:
			# Translators: reported when loading more of a page failed; {error} is the reason.
			ui.message(_("Loading more failed: {error}").format(error=error))
			return
		first = self._addNodes(nodes)
		if first:
			self.tree.SelectItem(first)
			self.tree.SetFocus()
		# Translators: reported after more of a long page was added to the page tree.
		ui.message(_("Added {count} items").format(count=len(nodes)))


def show(title, summary, nodes, loader):
	"""Shows the dialog modally and returns the ref the user chose, or None."""
	dialog = PageTreeDialog(gui.mainFrame, title, summary, nodes, loader)
	gui.mainFrame.prePopup()
	try:
		dialog.ShowModal()
		return dialog.chosenRef
	finally:
		gui.mainFrame.postPopup()
		dialog.Destroy()
