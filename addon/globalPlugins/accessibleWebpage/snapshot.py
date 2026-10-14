"""Serializes a browse-mode virtual buffer into compact pseudo-HTML for the LLM.

We read NVDA's virtual buffer instead of the raw DOM: it is fast (in-process),
works for Chrome, Edge and Firefox, already reflects what the screen reader can
see, and every control field carries an identifier that can be turned back into
a live NVDAObject when we need to act on the original page.
"""

import re

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
	"HASPOPUP", "EDITABLE", "CURRENT",
)
TEXT_ROLES = _members(Role, "EDITABLETEXT", "PASSWORDEDIT")
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
# Tag and ARIA role hints that only restate the role, so they reveal nothing about fake controls.
NATIVE_HINTS = {
	getattr(Role, k): frozenset(v)
	for k, v in {
		"BUTTON": ("button",), "TOGGLEBUTTON": ("button",), "MENUBUTTON": ("button",), "LINK": ("a", "link"),
		"EDITABLETEXT": ("input", "textarea", "textbox"), "PASSWORDEDIT": ("input", "textbox"),
		"CHECKBOX": ("input", "checkbox"), "RADIOBUTTON": ("input", "radio"), "COMBOBOX": ("select", "combobox"),
		"SLIDER": ("input", "slider"), "SPINBUTTON": ("input", "spinbutton"), "SWITCH": ("switch",),
		"TAB": ("tab",), "MENUITEM": ("menuitem",), "CHECKMENUITEM": ("menuitemcheckbox",),
		"RADIOMENUITEM": ("menuitemradio",), "TREEVIEWITEM": ("treeitem",), "LISTITEM": ("li", "listitem"),
	}.items()
	if hasattr(Role, k)
}
MAX_ATTR_LEN = 200
# Long texts get an id, so the model can reference them instead of copying them into its output.
TEXT_ID_MIN = 60
# Class tokens that only describe looks (Tailwind, Bootstrap utilities) or are generated hashes.
_UTILITY_CLASS = re.compile(
	r"[\w-]*[:\[].*"  # Variants and arbitrary values: md:flex, hover:bg-blue-500, w-[30px]
	# Spacing, size and position only with a value, so p-button (PrimeNG) or right-arrow stay.
	r"|-?(?:[pm][xytrblse]?|w|h|min-w|min-h|max-w|max-h|size|gap(?:-[xy])?|space-[xy]|inset(?:-[xy])?|top|right"
	r"|bottom|left|z|order|col|col-span|col-start|col-end|row-span|grid-cols|grid-rows|basis|translate-[xy])"
	r"(?:-[a-z]+)?-(?:\d[\w./%]*|auto|px|full|screen|min|max|fit|none|xs|sm|md|lg|\d?xl|xxl)"
	r"|-?(?:flex|items|justify|self|place-[a-z]+|align|text|bg|font|leading|tracking|border|rounded|shadow|ring"
	r"|outline|opacity|overflow|cursor|transition|duration|ease|delay|rotate|scale|fill|stroke|object|aspect|d"
	r"|float|line-clamp|decoration)-[a-z0-9./%-]+"
	r"|flex|grid|block|inline|inline-block|inline-flex|relative|absolute|fixed|sticky|static|truncate|uppercase"
	r"|lowercase|capitalize|italic|underline|rounded|shadow|border|container|row|clearfix|visible|invisible|transition"
)
_GENERATED_CLASS = re.compile(
	r"(?:css|jsx|svelte|emotion)-[a-z0-9]+|sc-[A-Za-z]+|ember\d+|ng-(?:tns|star-inserted|untouched|pristine|touched|dirty)[\w-]*"
)
# CSS modules: Button_root__1a2B3 -> Button_root.
_CSS_MODULE = re.compile(r"([A-Za-z][\w-]*_[A-Za-z][\w-]*)__([\w-]{5})")
_GENERATED_ID = re.compile(
	r".*[:«»].*|(?=.*\d)[0-9a-f-]{16,}|(?:react|radix|headlessui|mui|downshift|rc|yui|uid|ui-id)[-_].*\d.*|ember\d+",
	re.IGNORECASE,
)


class SnapshotError(Exception):
	pass


class RefInfo:
	__slots__ = ("docHandle", "id", "role", "name", "key", "index")

	def __init__(self, docHandle, id, role, name, key, index):
		self.docHandle = docHandle
		self.id = id
		self.role = role
		self.name = name
		# Role, name and text, plus how many elements with the same key come before it on the page:
		# finds the element again when the browser gives it a new ID.
		self.key = key
		self.index = index


class RefRegistry:
	"""Assigns refs to page elements, keeping them stable across snapshots of the same page."""

	def __init__(self):
		self.refs = {}
		self._byIdentifier = {}
		self.texts = {}
		self._textIds = {}

	def refFor(self, docHandle, controlId, role, name, key, index):
		identifier = (docHandle, controlId)
		ref = self._byIdentifier.get(identifier)
		if ref is None:
			ref = "e%d" % (len(self._byIdentifier) + 1)
			self._byIdentifier[identifier] = ref
		self.refs[ref] = RefInfo(docHandle, controlId, role, name, key, index)
		return ref

	def textIdFor(self, text):
		"""Keyed by content, so ids stay stable when live expands re-snapshot the page."""
		tid = self._textIds.get(text)
		if tid is None:
			tid = "t%d" % (len(self._textIds) + 1)
			self._textIds[text] = tid
			self.texts[tid] = text
		return tid


class Snapshot:
	def __init__(self, treeInterceptor, text, registry, title, url, hwnd, truncated, maxChars):
		self.treeInterceptor = treeInterceptor
		self.text = text
		self.registry = registry
		self.title = title
		self.url = url
		self.hwnd = hwnd
		self.truncated = truncated
		# The size limit it was taken with, reused when re-snapshotting the same page.
		self.maxChars = maxChars

	@property
	def refs(self):
		return self.registry.refs

	@property
	def texts(self):
		return self.registry.texts

	def resolve(self, ref):
		"""Returns the live NVDAObject for a ref, or None if it no longer exists. Call on the main thread."""
		info = self.refs.get(ref)
		if info is None:
			log.debugWarning("Unknown ref %s" % ref)
			return None
		oldIds = (info.docHandle, info.id)
		obj = self._lookup(ref, info)
		if obj is None and self._remap():
			if (info.docHandle, info.id) != oldIds:
				log.debug("%s moved from %s to %s" % (ref, oldIds, (info.docHandle, info.id)))
				obj = self._lookup(ref, info)
		if obj is None:
			log.debugWarning(
				"Could not resolve %s (window %s, id %s, buffer alive %s)"
				% (ref, info.docHandle, info.id, getattr(self.treeInterceptor, "isAlive", "?"))
			)
		return obj

	def _lookup(self, ref, info):
		obj = None
		try:
			obj = self.treeInterceptor.getNVDAObjectFromIdentifier(info.docHandle, info.id)
		except Exception:
			log.debugWarning("Browse mode lookup of %s failed" % ref, exc_info=True)
		if obj is None:
			# The browse mode lookup can return None without saying why; ask the window directly.
			try:
				from NVDAObjects.IAccessible import getNVDAObjectFromEvent
				obj = getNVDAObjectFromEvent(info.docHandle, winUser.OBJID_CLIENT, info.id)
			except Exception:
				log.debugWarning("Window lookup of %s failed" % ref, exc_info=True)
		return obj

	def _remap(self):
		"""Browsers can recreate a page's accessibility objects, giving the same elements new IDs.
		Finds every known element again in a fresh snapshot by its key and position among look-alikes,
		and points its ref at the new IDs. Returns how many refs changed."""
		try:
			fresh = take(self.treeInterceptor, self.maxChars)
		except Exception:
			log.debugWarning("Fresh snapshot for remapping failed", exc_info=True)
			return 0
		current = {(i.key, i.index): (i.docHandle, i.id) for i in fresh.refs.values()}
		changed = 0
		for ref, info in self.refs.items():
			ids = current.get((info.key, info.index))
			if ids and ids != (info.docHandle, info.id):
				info.docHandle, info.id = ids
				# Later snapshots of this page then keep using the same ref for the element.
				self.registry._byIdentifier[ids] = ref
				changed += 1
		log.debug("Remapped %d of %d refs from a fresh snapshot" % (changed, len(self.refs)))
		return changed


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
		self.keyCounts = {}

	def add(self, line):
		# No indentation: closing tags already carry the structure, and indents cost tokens.
		if self.truncated:
			return
		self.size += len(line) + 1
		if self.size > self.maxChars:
			self.truncated = True
			return
		self.lines.append(line)

	def children(self, node):
		buf = []
		for child in node.children:
			if isinstance(child, str):
				buf.append(child)
				continue
			self._flushText(buf)
			self.node(child)
		self._flushText(buf)

	def _flushText(self, buf):
		text = _clean(" ".join(buf))
		buf.clear()
		if len(text) >= TEXT_ID_MIN:
			self.add('<text tid="%s">%s</text>' % (self.registry.textIdFor(text), _esc(text)))
		elif text:
			self.add(_esc(text))

	def node(self, node):
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
				key = self._key(node, role, name, states)
				index = self.keyCounts.get(key, 0)
				self.keyCounts[key] = index + 1
				ref = self.registry.refFor(docHandle, controlId, role, name, key, index)
				self.hasRefs = True

		if not ref and not name and not landmark and role in COLLAPSIBLE_ROLES:
			self.children(node)
			return

		protected = State.PROTECTED in states
		kids = [] if protected else node.children  # Never send password contents.
		if role == Role.HEADING:
			tag = "h%s" % (field.get("level") or 2)
		else:
			tag = TAG_FOR_ROLE.get(role) or (role.name.lower() if role is not None else "node")

		if all(isinstance(k, str) for k in kids):
			text = _clean(" ".join(kids))
			tid = self.registry.textIdFor(text) if len(text) >= TEXT_ID_MIN else None
			attrs = self._attrs(field, role, states, ref, tid, name if name != text else "", landmark, protected, bool(text))
			self.add("<%s%s>%s</%s>" % (tag, attrs, _esc(text), tag))
		else:
			attrs = self._attrs(field, role, states, ref, None, name, landmark, protected, True)
			self.add("<%s%s>" % (tag, attrs))
			self.children(node)
			self.add("</%s>" % tag)

	@staticmethod
	def _key(node, role, name, states):
		"""What identifies an element apart from its browser ID: role, name and direct text."""
		text = "" if State.PROTECTED in states else _clean(" ".join(k for k in node.children if isinstance(k, str)))
		return (role, name, text[:100])

	@staticmethod
	def _isTarget(role, states):
		if role in NON_TARGET_ROLES:
			return False
		return role in INTERACTIVE_ROLES or bool(states & INTERACTIVE_STATES)

	@staticmethod
	def _implied(role, state):
		"""States the role already says."""
		if state == State.CLICKABLE:
			return role in INTERACTIVE_ROLES
		return state == State.EDITABLE and role in TEXT_ROLES

	@classmethod
	def _attrs(cls, field, role, states, ref, tid, name, landmark, protected, hasText):
		attrs = []
		if ref:
			attrs.append(("ref", ref))
		if tid:
			attrs.append(("tid", tid))
		if name:
			attrs.append(("name", name))
		value = field.get("value")
		if value and not protected and role not in (Role.EDITABLETEXT,):
			attrs.append(("value", value))
		if protected and hasText:
			attrs.append(("value", "[password entered, hidden]"))
		fieldName = _clean(field.get("name"))
		for key, attr in (
			("description", "description"),
			("placeholder", "placeholder"),
			("IAccessible2::attribute_placeholder", "placeholder"),
			("keyboardShortcut", "shortcut"),
		):
			v = field.get(key)
			if v and not any(a == attr for a, _v in attrs) and not (attr == "description" and _clean(v) == fieldName):
				attrs.append((attr, v))
		if landmark:
			attrs.append(("landmark", landmark))
		reported = sorted(s.name.lower() for s in states if s in REPORTED_STATES and not cls._implied(role, s))
		if reported:
			attrs.append(("states", ",".join(reported)))
		# Source hints are what reveal fake controls such as <div class="btn">.
		if ref or role in COLLAPSIBLE_ROLES:
			native = NATIVE_HINTS.get(role, frozenset())
			for key, attr in (
				("IAccessible2::attribute_tag", "tag"),
				("IAccessible2::attribute_xml-roles", "aria-role"),
			):
				v = _clean(field.get(key)).lower()
				if v and v not in native and v != landmark:
					attrs.append((attr, v))
		if ref:
			v = _clean(field.get("IAccessible2::attribute_id"))
			if v and not _GENERATED_ID.fullmatch(v):
				attrs.append(("id", v))
			v = _classHint(field.get("IAccessible2::attribute_class"))
			if v:
				attrs.append(("class", v))
		return "".join(' %s="%s"' % (a, _attrEsc(v)) for a, v in attrs)


def _classHint(value):
	"""Keeps the class tokens that can say what a control is, e.g. btn or icon-search."""
	tokens = []
	for token in _clean(value).split():
		module = _CSS_MODULE.fullmatch(token)
		if module and any(c.isdigit() for c in module.group(2)):
			token = module.group(1)
		elif _UTILITY_CLASS.fullmatch(token) or _GENERATED_CLASS.fullmatch(token):
			continue
		if token not in tokens:
			tokens.append(token)
	return " ".join(tokens)


_OPEN_LINE = re.compile(r"<(\w+)(?: [^<>]*)?>")
_CLOSE_LINE = re.compile(r"</(\w+)>")


def _joinWrappers(lines):
	"""Puts an element with a single child on one line: <li>, <a>x</a>, </li> -> <li><a>x</a></li>."""
	out = []
	for line in lines:
		close = _CLOSE_LINE.fullmatch(line)
		if close and len(out) >= 2:
			opened = _OPEN_LINE.fullmatch(out[-2])
			child = out[-1]
			if (
				opened and opened.group(1) == close.group(1)
				and not _OPEN_LINE.fullmatch(child) and not _CLOSE_LINE.fullmatch(child)
			):
				out[-2:] = [out[-2] + child + line]
				continue
		out.append(line)
	return out


def splitPart(lines, maxChars):
	"""Splits snapshot lines into a part of at most maxChars and the rest, for long pages that are
	loaded in parts. Prefers the shallowest boundary in the last 40% of the part, so that feed posts,
	search results and sections stay whole.

	Returns (part, rest, context): context holds the opening lines of the containers that are still
	open where the rest starts, so the model knows where the rest belongs.
	"""
	size = 0
	stack = []
	best = None
	for i, line in enumerate(lines):
		size += len(line) + 1
		if size > maxChars and i:
			break
		if _OPEN_LINE.fullmatch(line):
			stack.append(line)
		elif _CLOSE_LINE.fullmatch(line) and stack:
			stack.pop()
		if size >= maxChars * 0.6 and (best is None or len(stack) <= len(best[1])):
			best = (i + 1, list(stack))
	else:
		return lines, [], []
	cut, context = best or (i, stack)
	return lines[:cut], lines[cut:], context


_REF = re.compile(r' ref="(e\d+)"')


def lastMainRef(lines):
	"""The ref of the last operable element in the main landmark, or on the page if it has none.
	Scrolling it into view makes infinite feeds load more."""
	stack = []
	last = lastInMain = None
	for line in lines:
		refs = _REF.findall(line)
		if refs:
			last = refs[-1]
			if any(stack) or 'landmark="main"' in line:
				lastInMain = refs[-1]
		if _OPEN_LINE.fullmatch(line):
			stack.append('landmark="main"' in line)
		elif _CLOSE_LINE.fullmatch(line) and stack:
			stack.pop()
	return lastInMain or last


def take(treeInterceptor, maxChars, registry=None):
	"""Pass the registry of an earlier snapshot of the same page to keep its refs."""
	if not treeInterceptor.isReady:
		# Translators: reported when the page is not ready yet.
		raise SnapshotError(_("The page is still loading. Try again in a moment."))
	root = _buildTree(treeInterceptor)
	serializer = _Serializer(maxChars, registry or RefRegistry())
	serializer.children(root)
	if not serializer.hasRefs and not serializer.lines:
		# Translators: reported when nothing could be read from the page.
		raise SnapshotError(_("Could not read any content from this page."))
	text = "\n".join(_joinWrappers(serializer.lines))
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
	return Snapshot(treeInterceptor, text, serializer.registry, title, url, hwnd, serializer.truncated, maxChars)
