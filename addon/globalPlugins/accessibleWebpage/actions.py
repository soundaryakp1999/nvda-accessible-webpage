"""Replays the user's interaction from the viewer onto the original page.

Values are entered the way a sighted user would: focus the real element,
select its contents and type, so frameworks such as React or Angular receive
genuine keyboard and input events. Clicks use the element's accessibility
action (or a real mouse click as fallback) so the page's handlers fire.

Steps run as a generator driven by core.callLater, so NVDA stays responsive
and the browser has time to react between steps.
"""

import addonHandler
import api
import brailleInput
import controlTypes
import core
import ui
import winUser
from keyboardHandler import KeyboardInputGesture
from logHandler import log

addonHandler.initTranslation()

Role = controlTypes.Role
State = controlTypes.State


def perform(snapshot, response, clickMethod):
	_run(_steps(snapshot, response, clickMethod))


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
