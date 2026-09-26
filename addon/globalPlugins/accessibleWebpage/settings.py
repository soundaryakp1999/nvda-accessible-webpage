"""Configuration spec, provider selection and the NVDA settings panel."""

import os
import threading

import addonHandler
import config
import ui
import wx
from gui import guiHelper, nvdaControls, settingsDialogs

from . import llm

addonHandler.initTranslation()

CONF_SECTION = "accessibleWebpage"
DEFAULT_MODEL = "gemini-3.8-flash"
# Models Google has retired; saved configs using them are moved to DEFAULT_MODEL.
RETIRED_MODELS = frozenset(("gemini-2.5-flash",))
DEFAULT_OLLAMA_URL = "http://localhost:11434"
# Rough characters per token, used to keep Ollama snapshots inside the context window.
CHARS_PER_TOKEN = 3.5
# Share of the Ollama context the snapshot may use; the rest is for the prompt, thinking and the answer.
OLLAMA_INPUT_SHARE = 0.55
OLLAMA_INPUT_SHARE_THINKING = 0.35

confspec = {
	"provider": "option('gemini', 'ollama', default='gemini')",
	"apiKey": "string(default='')",
	"model": "string(default='%s')" % DEFAULT_MODEL,
	"ollamaUrl": "string(default='%s')" % DEFAULT_OLLAMA_URL,
	"ollamaModel": "string(default='')",
	"ollamaContextSize": "integer(default=32768, min=2048, max=1048576)",
	"ollamaThinking": "option('off', 'on', 'default', default='off')",
	"clickMethod": "option('action', 'mouse', default='action')",
	"maxChars": "integer(default=150000, min=5000, max=2000000)",
	"timeout": "integer(default=300, min=10, max=1800)",
}

PROVIDERS = (
	# Translators: an LLM provider in the settings panel.
	("gemini", _("Google Gemini (cloud)")),
	# Translators: an LLM provider in the settings panel.
	("ollama", _("Ollama (local or self-hosted)")),
)

THINKING_MODES = (
	# Translators: an Ollama thinking mode in the settings panel.
	("off", _("Off (much faster)")),
	# Translators: an Ollama thinking mode in the settings panel.
	("on", _("On (much slower, may handle complex pages better; thinking models only)")),
	# Translators: an Ollama thinking mode in the settings panel.
	("default", _("Model's default")),
)
# Value of Ollama's "think" parameter for each mode; None leaves it unset.
THINK_PARAM = {"off": False, "on": True, "default": None}

CLICK_METHODS = (
	# Translators: a click method in the settings panel.
	("action", _("Accessibility action, falling back to mouse (recommended)")),
	# Translators: a click method in the settings panel.
	("mouse", _("Simulated mouse click")),
)


def conf():
	return config.conf[CONF_SECTION]


def getApiKey():
	return conf()["apiKey"].strip() or os.environ.get("GEMINI_API_KEY", "").strip()


def makeProvider():
	"""Returns (provider, None), or (None, message) when the provider is not configured."""
	c = conf()
	if c["provider"] == "ollama":
		if not c["ollamaModel"].strip():
			# Translators: reported when no Ollama model is configured.
			return None, _("No Ollama model selected. Choose one in NVDA settings, Accessible Webpage category.")
		provider = llm.OllamaProvider(
			c["ollamaUrl"].strip() or DEFAULT_OLLAMA_URL,
			c["ollamaModel"].strip(),
			c["timeout"],
			c["ollamaContextSize"],
			THINK_PARAM[c["ollamaThinking"]],
		)
		return provider, None
	apiKey = getApiKey()
	if not apiKey:
		# Translators: reported when no API key is configured.
		return None, _("No Gemini API key. Set one in NVDA settings, Accessible Webpage category.")
	return llm.GeminiProvider(apiKey, c["model"], c["timeout"]), None


def providerId():
	"""Identifies the configured provider and model, e.g. for cache keys."""
	c = conf()
	if c["provider"] == "ollama":
		return "ollama:%s:%s:%d:%s" % (c["ollamaUrl"], c["ollamaModel"], c["ollamaContextSize"], c["ollamaThinking"])
	return "gemini:%s" % c["model"]


def effectiveMaxChars():
	"""The snapshot size limit, capped so Ollama never silently truncates the prompt."""
	c = conf()
	if c["provider"] == "ollama":
		share = OLLAMA_INPUT_SHARE if c["ollamaThinking"] == "off" else OLLAMA_INPUT_SHARE_THINKING
		return min(c["maxChars"], int(c["ollamaContextSize"] * CHARS_PER_TOKEN * share))
	return c["maxChars"]


def _choiceIndex(options, key):
	keys = [k for k, _label in options]
	return keys.index(key) if key in keys else 0


class AccessibleWebpagePanel(settingsDialogs.SettingsPanel):
	# Translators: title of the settings panel.
	title = _("Accessible Webpage")

	def makeSettings(self, settingsSizer):
		helper = guiHelper.BoxSizerHelper(self, sizer=settingsSizer)
		c = conf()

		self.providerChoice = helper.addLabeledControl(
			# Translators: label in the settings panel.
			_("AI &provider:"),
			wx.Choice,
			choices=[label for _key, label in PROVIDERS],
		)
		self.providerChoice.SetSelection(_choiceIndex(PROVIDERS, c["provider"]))
		self.providerChoice.Bind(wx.EVT_CHOICE, lambda evt: self._updateEnabled())

		# Translators: title of the Gemini settings group.
		geminiSizer = wx.StaticBoxSizer(wx.VERTICAL, self, label=_("Gemini"))
		self.geminiBox = geminiSizer.GetStaticBox()
		gemini = helper.addItem(guiHelper.BoxSizerHelper(self.geminiBox, sizer=geminiSizer))
		self.apiKeyEdit = gemini.addLabeledControl(
			# Translators: label in the settings panel.
			_("Gemini API &key (or set the GEMINI_API_KEY environment variable):"),
			wx.TextCtrl,
			style=wx.TE_PASSWORD,
		)
		self.apiKeyEdit.SetValue(c["apiKey"])
		# Translators: label in the settings panel.
		self.modelEdit = gemini.addLabeledControl(_("Gemini &model:"), wx.TextCtrl)
		self.modelEdit.SetValue(c["model"])

		# Translators: title of the Ollama settings group.
		ollamaSizer = wx.StaticBoxSizer(wx.VERTICAL, self, label=_("Ollama"))
		self.ollamaBox = ollamaSizer.GetStaticBox()
		ollama = helper.addItem(guiHelper.BoxSizerHelper(self.ollamaBox, sizer=ollamaSizer))
		# Translators: label in the settings panel.
		self.ollamaUrlEdit = ollama.addLabeledControl(_("Ollama server &address:"), wx.TextCtrl)
		self.ollamaUrlEdit.SetValue(c["ollamaUrl"])
		self.ollamaModelCombo = ollama.addLabeledControl(
			# Translators: label in the settings panel.
			_("Ollama m&odel:"),
			wx.ComboBox,
			choices=[c["ollamaModel"]] if c["ollamaModel"] else [],
		)
		self.ollamaModelCombo.SetValue(c["ollamaModel"])
		# Translators: button that lists the models installed on the Ollama server.
		self.refreshButton = ollama.addItem(wx.Button(self.ollamaBox, label=_("&Refresh installed models")))
		self.refreshButton.Bind(wx.EVT_BUTTON, self._onRefresh)
		self.thinkingChoice = ollama.addLabeledControl(
			# Translators: label in the settings panel.
			_("T&hinking:"),
			wx.Choice,
			choices=[label for _key, label in THINKING_MODES],
		)
		self.thinkingChoice.SetSelection(_choiceIndex(THINKING_MODES, c["ollamaThinking"]))
		self.contextSpin = ollama.addLabeledControl(
			# Translators: label in the settings panel.
			_("Ollama context si&ze (tokens; larger pages need more memory):"),
			nvdaControls.SelectOnFocusSpinCtrl,
			min=2048,
			max=1048576,
			initial=c["ollamaContextSize"],
		)

		self.clickMethodChoice = helper.addLabeledControl(
			# Translators: label in the settings panel.
			_("How to &click elements on the original page:"),
			wx.Choice,
			choices=[label for _key, label in CLICK_METHODS],
		)
		self.clickMethodChoice.SetSelection(_choiceIndex(CLICK_METHODS, c["clickMethod"]))

		self.maxCharsSpin = helper.addLabeledControl(
			# Translators: label in the settings panel.
			_("Maximum page size sent to the model (characters):"),
			nvdaControls.SelectOnFocusSpinCtrl,
			min=5000,
			max=2000000,
			initial=c["maxChars"],
		)
		self.timeoutSpin = helper.addLabeledControl(
			# Translators: label in the settings panel.
			_("Request &timeout (seconds; local models may need more):"),
			nvdaControls.SelectOnFocusSpinCtrl,
			min=10,
			max=1800,
			initial=c["timeout"],
		)
		self._updateEnabled()

	def _selectedProvider(self):
		return PROVIDERS[self.providerChoice.GetSelection()][0]

	def _updateEnabled(self):
		isOllama = self._selectedProvider() == "ollama"
		self.geminiBox.Enable(not isOllama)
		self.ollamaBox.Enable(isOllama)

	def _onRefresh(self, evt):
		url = self.ollamaUrlEdit.GetValue().strip() or DEFAULT_OLLAMA_URL
		self.refreshButton.Disable()

		def worker():
			try:
				models, error = llm.listOllamaModels(url), None
			except Exception as e:
				models, error = [], e
			wx.CallAfter(self._onModels, models, error)

		threading.Thread(target=worker, daemon=True).start()

	def _onModels(self, models, error):
		if not self:  # The settings dialog was closed meanwhile.
			return
		self.refreshButton.Enable()
		if error:
			ui.message(str(error))
			return
		if not models:
			# Translators: reported when the Ollama server has no models installed.
			ui.message(_("No models installed. Install one with: ollama pull followed by the model name"))
			return
		current = self.ollamaModelCombo.GetValue().strip()
		self.ollamaModelCombo.SetItems(models)
		self.ollamaModelCombo.SetValue(current if current in models else models[0])
		# Translators: reported after listing Ollama models; {count} is how many were found.
		ui.message(_("{count} models found").format(count=len(models)))

	def onSave(self):
		c = conf()
		c["provider"] = self._selectedProvider()
		c["apiKey"] = self.apiKeyEdit.GetValue().strip()
		c["model"] = self.modelEdit.GetValue().strip() or DEFAULT_MODEL
		c["ollamaUrl"] = self.ollamaUrlEdit.GetValue().strip() or DEFAULT_OLLAMA_URL
		c["ollamaModel"] = self.ollamaModelCombo.GetValue().strip()
		c["ollamaContextSize"] = self.contextSpin.GetValue()
		c["ollamaThinking"] = THINKING_MODES[self.thinkingChoice.GetSelection()][0]
		c["clickMethod"] = CLICK_METHODS[self.clickMethodChoice.GetSelection()][0]
		c["maxChars"] = self.maxCharsSpin.GetValue()
		c["timeout"] = self.timeoutSpin.GetValue()


def register():
	config.conf.spec[CONF_SECTION] = confspec
	if conf()["model"] in RETIRED_MODELS:
		conf()["model"] = DEFAULT_MODEL
	settingsDialogs.NVDASettingsDialog.categoryClasses.append(AccessibleWebpagePanel)


def unregister():
	try:
		settingsDialogs.NVDASettingsDialog.categoryClasses.remove(AccessibleWebpagePanel)
	except ValueError:
		pass
