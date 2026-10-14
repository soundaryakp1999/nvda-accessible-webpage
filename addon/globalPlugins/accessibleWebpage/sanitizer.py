"""Allowlist HTML sanitizer for model output.

The model's HTML is untrusted: it is rendered by MSHTML inside NVDA's process,
so scripts, event handlers, external resources and navigation are stripped, and
data-ref values that do not exist in the snapshot are dropped.

It also fills in what the model is told to leave out, to save output tokens:
href and button types, aria-required, aria-expanded on live expanders, and the
original text of elements that reference a long snapshot text with data-text.
"""

import re
from html import escape
from html.parser import HTMLParser

ALLOWED_TAGS = frozenset((
	"main", "nav", "header", "footer", "section", "article", "aside", "div", "span", "p",
	"h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "dl", "dt", "dd",
	"a", "button", "form", "fieldset", "legend", "label", "input", "textarea", "select", "option", "optgroup",
	"table", "caption", "thead", "tbody", "tfoot", "tr", "th", "td",
	"strong", "em", "b", "i", "u", "small", "mark", "abbr", "code", "pre", "blockquote",
	"figure", "figcaption", "br", "hr", "time", "output", "sup", "sub",
))
# h1 is reserved for the viewer's title; demote it.
TAG_REWRITES = {"h1": "h2"}
VOID_TAGS = frozenset(("br", "hr", "input"))
# Elements dropped together with their content. Must not contain void elements.
DROP_WITH_CONTENT = frozenset(("script", "style", "iframe", "object", "noscript", "template", "svg", "math", "head", "title", "canvas", "video", "audio"))

GLOBAL_ATTRS = frozenset((
	"id", "role", "lang", "title", "hidden", "tabindex",
	"data-ref", "data-expand", "data-toggle", "data-toggle-group",
))
_NAME = re.compile(r"[A-Za-z][\w-]{0,63}$")
TAG_ATTRS = {
	"label": ("for",),
	"input": ("type", "name", "value", "checked", "placeholder", "required", "disabled", "readonly", "min", "max", "step", "maxlength", "minlength", "size", "multiple", "autocomplete"),
	"textarea": ("name", "rows", "cols", "required", "placeholder", "readonly", "disabled", "maxlength"),
	"select": ("name", "required", "multiple", "disabled", "size"),
	"option": ("value", "selected", "disabled", "label"),
	"optgroup": ("label", "disabled"),
	"a": ("href",),
	"button": ("type", "disabled", "name", "value"),
	"th": ("scope", "colspan", "rowspan", "headers"),
	"td": ("colspan", "rowspan", "headers"),
	"ol": ("start", "reversed"),
	"li": ("value",),
	"time": ("datetime",),
	"output": ("for",),
}
INPUT_TYPES = frozenset((
	"text", "email", "tel", "url", "number", "search", "password", "date", "time", "datetime-local",
	"month", "week", "checkbox", "radio", "button", "submit", "reset", "range", "color",
))


class _Sanitizer(HTMLParser):
	def __init__(self, validRefs, texts):
		super().__init__(convert_charrefs=True)
		self.validRefs = validRefs
		self.texts = texts
		self.out = []
		self.stack = []
		self.skip = 0

	def handle_starttag(self, tag, attrs):
		self._start(tag, attrs, selfClosing=False)

	def handle_startendtag(self, tag, attrs):
		self._start(tag, attrs, selfClosing=True)

	def _start(self, tag, attrs, selfClosing):
		if tag in DROP_WITH_CONTENT:
			if not selfClosing:
				self.skip += 1
			return
		if self.skip:
			return
		if tag == "img":
			alt = dict(attrs).get("alt")
			if alt:
				self.out.append(escape(alt, quote=False))
			return
		tag = TAG_REWRITES.get(tag, tag)
		if tag not in ALLOWED_TAGS:
			return
		clean = self._cleanAttrs(tag, attrs)
		if clean is None:
			return
		self.out.append("<%s%s>" % (tag, "".join(
			' %s' % k if v is None else ' %s="%s"' % (k, escape(v, quote=True)) for k, v in clean
		)))
		if tag not in VOID_TAGS:
			text = self.texts.get(dict(attrs).get("data-text"))
			if text:
				self.out.append(escape(text, quote=False))
			if selfClosing:
				self.out.append("</%s>" % tag)
			else:
				self.stack.append(tag)

	def _cleanAttrs(self, tag, attrs):
		allowed = TAG_ATTRS.get(tag, ())
		clean = []
		seen = set()
		for k, v in attrs:
			k = k.lower()
			if k in seen or not (k in GLOBAL_ATTRS or k in allowed or k.startswith("aria-")):
				continue
			seen.add(k)
			if k in ("data-ref", "data-expand"):
				if v not in self.validRefs:
					continue
			elif k in ("data-toggle", "data-toggle-group"):
				if not v or not _NAME.match(v) or v.startswith("awp-"):
					continue
			elif k == "href":
				v = "#"
			elif k == "id" and v and v.startswith("awp-"):
				continue
			elif k == "tabindex":
				if v not in ("0", "-1"):
					continue
			elif k == "type" and tag == "input":
				v = (v or "text").lower()
				if v == "hidden":
					return None
				if v in ("file", "image"):
					v = "button"
				elif v not in INPUT_TYPES:
					v = "text"
			elif k == "type" and tag == "button":
				v = "submit" if (v or "").lower() == "submit" else "button"
			clean.append((k, v))
		if tag == "a" and "href" not in seen:
			clean.append(("href", "#"))
		if tag == "button" and "type" not in seen:
			clean.append(("type", "button"))
		if "required" in seen and "aria-required" not in seen:
			clean.append(("aria-required", "true"))
		if "aria-expanded" not in seen and any(k == "data-expand" for k, _v in clean):
			clean.append(("aria-expanded", "false"))
		return clean

	def handle_endtag(self, tag):
		if tag in DROP_WITH_CONTENT:
			if self.skip:
				self.skip -= 1
			return
		if self.skip:
			return
		tag = TAG_REWRITES.get(tag, tag)
		if tag not in self.stack:
			return
		while self.stack:
			open_ = self.stack.pop()
			self.out.append("</%s>" % open_)
			if open_ == tag:
				break

	def handle_data(self, data):
		if not self.skip:
			self.out.append(escape(data, quote=False))

	def result(self):
		self.close()
		while self.stack:
			self.out.append("</%s>" % self.stack.pop())
		return "".join(self.out)


def sanitize(html, validRefs, texts=None):
	"""texts: the snapshot's long texts by id, for data-text."""
	parser = _Sanitizer(frozenset(validRefs), texts or {})
	parser.feed(html or "")
	return parser.result()
