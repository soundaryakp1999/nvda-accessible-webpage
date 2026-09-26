"""Serializes a browse-mode virtual buffer into compact pseudo-HTML for the LLM.

We read NVDA's virtual buffer instead of the raw DOM: it is fast (in-process),
works for Chrome, Edge and Firefox, already reflects what the screen reader can
see, and every control field carries an identifier that can be turned back into
a live NVDAObject when we need to act on the original page.
"""

import addonHandler
import controlTypes
import textInfos
import winUser
from logHandler import log

addonHandler.initTranslation()

Role = controlTypes.Role
State = controlTypes.State


def _members(enum, *names):
	return frozenset(getattr(enum, n) for n in names if hasattr(enum, n))


INTERACTIVE_ROLES = _members(
	Role,
	"BUTTON", "LINK", "EDITABLETEXT", "PASSWORDEDIT", "CHECKBOX", "RADIOBUTTON",
	"COMBOBOX", "TOGGLEBUTTON", "MENUBUTTON", "SPLITBUTTON", "DROPDOWNBUTTON",
	"SPINBUTTON", "SLIDER", "TAB", "MENUITEM", "CHECKMENUITEM", "RADIOMENUITEM",
	"TREEVIEWITEM", "SWITCH",
)
INTERACTIVE_STATES = _members(State, "FOCUSABLE", "CLICKABLE", "EDITABLE", "CHECKABLE", "SELECTABLE")
# Focusable containers that are never useful targets.
NON_TARGET_ROLES = _members(Role, "DOCUMENT", "FRAME", "INTERNALFRAME", "APPLICATION", "DIALOG", "PANE")
# Structural wrappers that are dropped (children kept) when they carry no information.
COLLAPSIBLE_ROLES = _members(
	Role, "SECTION", "GROUPING", "TEXTFRAME", "UNKNOWN", "PANE", "DOCUMENT",
	"FRAME", "INTERNALFRAME", "STATICTEXT", "BLOCKQUOTE",
)
REPORTED_STATES = _members(
	State,
	"CLICKABLE", "CHECKED", "HALFCHECKED", "PRESSED", "SELECTED", "EXPANDED", "COLLAPSED",
	"REQUIRED", "INVALID_ENTRY", "READONLY", "UNAVAILABLE", "PROTECTED", "MULTILINE",
	"HASPOPUP", "VISITED", "EDITABLE", "CURRENT",
)
TAG_FOR_ROLE = {
	getattr(Role, k): v
	for k, v in {
		"LINK": "a", "BUTTON": "button", "PARAGRAPH": "p", "LIST": "list", "LISTITEM": "listitem",
		"TABLE": "table", "TABLEROW": "tr", "TABLECELL": "td", "TABLECOLUMNHEADER": "th",
		"TABLEROWHEADER": "th", "GRAPHIC": "img", "FORM": "form", "LABEL": "label",
		"EDITABLETEXT": "textbox",
	}.items()
	if hasattr(Role, k)
}
MAX_ATTR_LEN = 200


class SnapshotError(Exception):
	pass


class RefInfo:
	__slots__ = ("docHandle", "id", "role", "name")

	def __init__(self, docHandle, id, role, name):
		self.docHandle = docHandle
		self.id = id
		self.role = role
		self.name = name


class RefRegistry:
	"""Assigns refs to page elements, keeping them stable across snapshots of the same page."""

	def __init__(self):
		self.refs = {}
		self._byIdentifier = {}

	def refFor(self, docHandle, controlId, role, name):
		key = (docHandle, controlId)
		ref = self._byIdentifier.get(key)
		if ref is None:
			ref = "e%d" % (len(self._byIdentifier) + 1)
			self._byIdentifier[key] = ref
		self.refs[ref] = RefInfo(docHandle, controlId, role, name)
		return ref


class Snapshot:
	def __init__(self, treeInterceptor, text, registry, title, url, hwnd, truncated):
		self.treeInterceptor = treeInterceptor
		self.text = text
		self.registry = registry
		self.title = title
		self.url = url
		self.hwnd = hwnd
		self.truncated = truncated

	@property
	def refs(self):
		return self.registry.refs

	def resolve(self, ref):
		"""Returns the live NVDAObject for a ref, or None if it no longer exists."""
		info = self.refs.get(ref)
		if info is None:
			return None
		try:
			return self.treeInterceptor.getNVDAObjectFromIdentifier(info.docHandle, info.id)
		except Exception:
			log.debugWarning("Could not resolve %s" % ref, exc_info=True)
			return None


class _Node:
	__slots__ = ("field", "children")

	def __init__(self, field):
		self.field = field
		self.children = []


def _clean(s):
	if not s:
		return ""
	return " ".join(str(s).split())


def _esc(s):
	return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _attrEsc(s):
	s = _clean(s)
	if len(s) > MAX_ATTR_LEN:
		s = s[:MAX_ATTR_LEN] + "…"
	return _esc(s).replace('"', "&quot;")


def _buildTree(treeInterceptor):
	info = treeInterceptor.makeTextInfo(textInfos.POSITION_ALL)
	root = _Node({})
	stack = [root]
	for item in info.getTextWithFields():
		if isinstance(item, str):
			stack[-1].children.append(item)
		elif isinstance(item, textInfos.FieldCommand):
			if item.command == "controlStart":
				node = _Node(item.field)
				stack[-1].children.append(node)
				stack.append(node)
			elif item.command == "controlEnd" and len(stack) > 1:
				stack.pop()
	return root


class _Serializer:
	def __init__(self, maxChars, registry):
		self.maxChars = maxChars
		self.registry = registry
		self.lines = []
		self.size = 0
		self.hasRefs = False
		self.truncated = False

	def add(self, depth, text):
		if self.truncated:
			return
		line = " " * min(depth, 30) + text
		self.size += len(line) + 1
		if self.size > self.maxChars:
			self.truncated = True
			return
		self.lines.append(line)

	def children(self, node, depth):
		buf = []
		for child in node.children:
			if isinstance(child, str):
				buf.append(child)
				continue
			self._flushText(buf, depth)
			self.node(child, depth)
		self._flushText(buf, depth)

	def _flushText(self, buf, depth):
		text = _clean(" ".join(buf))
		buf.clear()
		if text:
			self.add(depth, _esc(text))

	def node(self, node, depth):
		if self.truncated:
			return
		field = node.field
		role = field.get("role")
		states = field.get("states") or set()
		name = _clean(field.get("name"))
		landmark = field.get("landmark")

		ref = None
		if self._isTarget(role, states):
			docHandle = field.get("controlIdentifier_docHandle")
			controlId = field.get("controlIdentifier_ID")
			if docHandle is not None and controlId is not None:
				ref = self.registry.refFor(docHandle, controlId, role, name)
				self.hasRefs = True

		if not ref and not name and not landmark and role in COLLAPSIBLE_ROLES:
			self.children(node, depth)
			return

		protected = State.PROTECTED in states
		kids = [] if protected else node.children  # Never send password contents.
		if role == Role.HEADING:
			tag = "h%s" % (field.get("level") or 2)
		else:
			tag = TAG_FOR_ROLE.get(role) or (role.name.lower() if role is not None else "node")

		if all(isinstance(k, str) for k in kids):
			text = _clean(" ".join(kids))
			attrs = self._attrs(field, role, states, ref, name if name != text else "", landmark, protected, bool(text))
			self.add(depth, "<%s%s>%s</%s>" % (tag, attrs, _esc(text), tag))
		else:
			attrs = self._attrs(field, role, states, ref, name, landmark, protected, True)
			self.add(depth, "<%s%s>" % (tag, attrs))
			self.children(node, depth + 1)
			self.add(depth, "</%s>" % tag)

	@staticmethod
	def _isTarget(role, states):
		if role in NON_TARGET_ROLES:
			return False
		return role in INTERACTIVE_ROLES or bool(states & INTERACTIVE_STATES)

	@staticmethod
	def _attrs(field, role, states, ref, name, landmark, protected, hasText):
		attrs = []
		if ref:
			attrs.append(("ref", ref))
		if name:
			attrs.append(("name", name))
		value = field.get("value")
		if value and not protected and role not in (Role.EDITABLETEXT,):
			attrs.append(("value", value))
		if protected and hasText:
			attrs.append(("value", "[password entered, hidden]"))
		for key, attr in (
			("description", "description"),
			("placeholder", "placeholder"),
			("IAccessible2::attribute_placeholder", "placeholder"),
			("keyboardShortcut", "shortcut"),
		):
			v = field.get(key)
			if v and not any(a == attr for a, _v in attrs):
				attrs.append((attr, v))
		if landmark:
			attrs.append(("landmark", landmark))
		reported = sorted(s.name.lower() for s in states if s in REPORTED_STATES)
		if reported:
			attrs.append(("states", ",".join(reported)))
		# Source hints are what reveal fake controls such as <div class="btn">.
		if ref or role in COLLAPSIBLE_ROLES:
			for key, attr in (
				("IAccessible2::attribute_tag", "tag"),
				("IAccessible2::attribute_xml-roles", "aria-role"),
			):
				v = field.get(key)
				if v:
					attrs.append((attr, v))
		if ref:
			for key, attr in (("IAccessible2::attribute_id", "id"), ("IAccessible2::attribute_class", "class")):
				v = field.get(key)
				if v:
					attrs.append((attr, v))
		return "".join(' %s="%s"' % (a, _attrEsc(v)) for a, v in attrs)


def take(treeInterceptor, maxChars, registry=None):
	"""Pass the registry of an earlier snapshot of the same page to keep its refs."""
	if not treeInterceptor.isReady:
		# Translators: reported when the page is not ready yet.
		raise SnapshotError(_("The page is still loading. Try again in a moment."))
	root = _buildTree(treeInterceptor)
	serializer = _Serializer(maxChars, registry or RefRegistry())
	serializer.children(root, 0)
	if not serializer.hasRefs and not serializer.lines:
		# Translators: reported when nothing could be read from the page.
		raise SnapshotError(_("Could not read any content from this page."))
	text = "\n".join(serializer.lines)
	if serializer.truncated:
		text += "\n<!-- snapshot truncated: the page is longer than the configured limit -->"

	rootObj = treeInterceptor.rootNVDAObject
	title = _clean(getattr(rootObj, "name", "")) or ""
	url = getattr(treeInterceptor, "documentConstantIdentifier", None) or ""
	# Query strings frequently carry session tokens; the path is enough context.
	url = url.split("?", 1)[0].split("#", 1)[0]
	try:
		hwnd = winUser.getAncestor(rootObj.windowHandle, getattr(winUser, "GA_ROOT", 2))
	except Exception:
		hwnd = None
	return Snapshot(treeInterceptor, text, serializer.registry, title, url, hwnd, serializer.truncated)
