"""Replays the user's interaction from the viewer onto the original page.

Values are entered the way a sighted user would: focus the real element,
select its contents and type, so frameworks such as React or Angular receive
genuine keyboard and input events. Clicks use the element's accessibility
action (or a real mouse click as fallback) so the page's handlers fire.

Steps run as a generator driven by core.callLater, so NVDA stays responsive
and the browser has time to react between steps.

moveTo instead puts NVDA's browse mode cursor on an element, using only the
browse mode buffer. It works even when the element can't be retrieved as an
object, and the user then activates it with NVDA's own Enter.
"""

import addonHandler
import api
import brailleInput
import controlTypes
import core
import speech
import textInfos
from textInfos.offsets import Offsets
import ui
import winUser
from keyboardHandler import KeyboardInputGesture
from logHandler import log
from virtualBuffers import VirtualBuffer

from . import snapshot as snapshotModule

addonHandler.initTranslation()

Role = controlTypes.Role
State = controlTypes.State


def perform(snapshot, response, clickMethod):
	_run(_steps(snapshot, response, clickMethod))


def moveTo(snapshot, ref):
	"""Brings the page to the front and puts the browse mode cursor on the element for ref."""
	_run(_moveSteps(snapshot, ref))


def _moveSteps(snap, ref):
	if snap.hwnd and winUser.getForegroundWindow() != snap.hwnd:
		winUser.setForegroundWindow(snap.hwnd)
	yield 500
	if not _moveCaret(snap, ref):
		# Translators: reported when the chosen element could not be found on the original page.
		ui.message(_("Could not find that element on the page. Rebuild the page and try again."))


def _currentTreeInterceptor(snap):
	"""The browse mode document for the page: the focused one if it shows the same address,
	since NVDA may have replaced the snapshot's, otherwise the snapshot's own."""
	ti = getattr(api.getFocusObject(), "treeInterceptor", None)
	if isinstance(ti, VirtualBuffer) and ti is not snap.treeInterceptor:
		url = (getattr(ti, "documentConstantIdentifier", None) or "").split("?", 1)[0].split("#", 1)[0]
		if url == snap.url:
			log.debug("Using the focused browse mode document instead of the snapshot's")
			return ti
	if snap.treeInterceptor.isAlive:
		return snap.treeInterceptor
	return ti if isinstance(ti, VirtualBuffer) else None


def _offsetsFor(ti, docHandle, ID):
	"""Start and end offsets of an element in the browse mode buffer; raises LookupError if absent.
	The lookup lives on the buffer's TextInfo, not on the buffer itself."""
	return ti.makeTextInfo(textInfos.POSITION_FIRST)._getOffsetsFromFieldIdentifier(docHandle, ID)


def _findOffsets(snap, ti, ref):
	info = snap.refs.get(ref)
	if info is None:
		return None
	try:
		return _offsetsFor(ti, info.docHandle, info.id)
	except LookupError:
		log.debug("%s is not at its old place in the buffer, looking it up by key" % ref)
	# Another buffer or new IDs: find the same element by its key and position among look-alikes.
	try:
		fresh = snapshotModule.take(ti, snap.maxChars)
	except Exception:
		log.debugWarning("Snapshot for finding %s failed" % ref, exc_info=True)
		return None
	for candidate in fresh.refs.values():
		if candidate.key == info.key and candidate.index == info.index:
			try:
				return _offsetsFor(ti, candidate.docHandle, candidate.id)
			except LookupError:
				log.debugWarning("Found %s by key but not in the buffer" % ref, exc_info=True)
				return None
	log.debugWarning("No element matches %s any more" % ref)
	return None


def _moveCaret(snap, ref):
	"""Puts the browse mode cursor on the element for ref and reads it, like quick navigation does."""
	ti = _currentTreeInterceptor(snap)
	if ti is None:
		return False
	offsets = _findOffsets(snap, ti, ref)
	if offsets is None:
		return False
	info = ti.makeTextInfo(Offsets(offsets[0], offsets[0]))
	reason = controlTypes.OutputReason.QUICKNAV
	try:
		ti._set_selection(info, reason=reason)
	except TypeError:
		ti.selection = info
	info.expand(textInfos.UNIT_LINE)
	speech.cancelSpeech()
	speech.speakTextInfo(info, unit=textInfos.UNIT_LINE, reason=reason)
	log.debug("Moved the browse mode cursor to %s at offset %d" % (ref, offsets[0]))
	return True


def _run(gen):
	try:
		delay = next(gen)
	except StopIteration:
		return
	except Exception:
		log.exception("Replaying interaction failed")
		# Translators: reported when replaying the user's action on the page failed.
		ui.message(_("Could not complete the action on the page."))
		return
	core.callLater(delay, _run, gen)


def _steps(snapshot, response, clickMethod):
	ti = snapshot.treeInterceptor
	if not ti.isAlive:
		# Translators: reported when the original page is gone.
		ui.message(_("The original page is no longer available."))
		return
	if snapshot.hwnd and winUser.getForegroundWindow() != snapshot.hwnd:
		winUser.setForegroundWindow(snapshot.hwnd)
	yield 400

	fields = response.get("fields") or []
	failed = 0
	for field in fields:
		obj = snapshot.resolve(field.get("ref"))
		if obj is None:
			failed += 1
			continue
		kind = field.get("kind")
		if kind == "text":
			yield from _setText(obj, field.get("value") or "", clickMethod)
		elif kind == "check":
			if _isChecked(obj) != bool(field.get("checked")):
				yield from _click(obj, clickMethod)
		elif kind == "select":
			yield from _select(snapshot, obj, field, clickMethod)

	action = response.get("action")
	if action == "click":
		target = snapshot.resolve(response.get("ref"))
		if target is None:
			# The element can't be retrieved as an object, but browse mode can still reach it.
			if _moveCaret(snapshot, response.get("ref")):
				# Translators: reported when the add-on could not click an element and moved to it instead.
				ui.message(_("Could not activate it from here. The cursor is on it: press Enter."))
			else:
				# Translators: reported when the element to click no longer exists on the page.
				ui.message(_("That element is no longer on the page. Rebuild the page and try again."))
			return
		yield from _click(target, clickMethod)
	elif fields:
		# Translators: reported after fields were filled without clicking anything.
		ui.message(_("Filled in {count} fields").format(count=len(fields) - failed))
	if failed:
		# Translators: reported when some fields could not be found on the original page.
		ui.message(_("{count} fields could not be found on the page").format(count=failed))


def _isChecked(obj):
	states = obj.states
	return State.CHECKED in states or State.PRESSED in states


def _focus(obj, clickMethod):
	obj.setFocus()
	yield 200
	if api.getFocusObject() != obj:
		# Some custom widgets only take focus from a real click.
		yield from _mouseClick(obj)


def _typeText(text):
	lines = text.replace("\r\n", "\n").split("\n")
	for i, line in enumerate(lines):
		if i:
			KeyboardInputGesture.fromName("enter").send()
		if line:
			brailleInput.handler.sendChars(line)


def _setText(obj, value, clickMethod):
	yield from _focus(obj, clickMethod)
	KeyboardInputGesture.fromName("control+a").send()
	yield 50
	if value:
		_typeText(value)
	else:
		KeyboardInputGesture.fromName("delete").send()
	yield 150


def _select(snapshot, obj, field, clickMethod):
	text = field.get("text") or ""
	option = snapshot.resolve(field.get("optionRef")) if field.get("optionRef") else None
	role, states = obj.role, obj.states
	if role == Role.COMBOBOX and State.EDITABLE not in states:
		# Native <select>: typing the option text picks it and fires a change event.
		yield from _focus(obj, clickMethod)
		_typeText(text)
		yield 200
		return
	if role == Role.COMBOBOX:
		# Autocomplete-style combobox: type to filter, then pick the option if we know it.
		yield from _setText(obj, text, clickMethod)
		yield 400
	if option is not None:
		yield from _click(option, clickMethod)
	elif role != Role.COMBOBOX:
		yield from _focus(obj, clickMethod)
		_typeText(text)
		yield 200


def _click(obj, clickMethod):
	if clickMethod == "action":
		try:
			obj.doAction()
			yield 300
			return
		except Exception:
			log.debugWarning("doAction failed, falling back to mouse click", exc_info=True)
	yield from _mouseClick(obj)
	yield 300


def _mouseClick(obj):
	try:
		obj.scrollIntoView()
		yield 150
	except Exception:
		pass
	location = obj.location
	if not location or location.width <= 0 or location.height <= 0:
		log.debugWarning("Cannot mouse-click object without a location: %r" % obj)
		return
	winUser.setCursorPos(location.left + location.width // 2, location.top + location.height // 2)
	try:
		import mouseHandler
		mouseHandler.doPrimaryClick()
	except (ImportError, AttributeError):
		winUser.mouse_event(winUser.MOUSEEVENTF_LEFTDOWN, 0, 0, None, None)
		winUser.mouse_event(winUser.MOUSEEVENTF_LEFTUP, 0, 0, None, None)
