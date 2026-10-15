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
DEFAULT_OPENAI_MODEL = "gpt-6-luna"
# The OpenAI key is only read from the environment, never stored in NVDA's configuration.
OPENAI_KEY_VARIABLE = "OPENAI_API_KEY"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
# llama.cpp's llama-server default address.
DEFAULT_SERVER_URL = "http://localhost:8080/v1"
# Rough characters per token, used to keep local snapshots inside the context window.
CHARS_PER_TOKEN = 3.5
# Share of a local context the snapshot may use; the rest is for the prompt, thinking and the answer.
LOCAL_INPUT_SHARE = 0.55
LOCAL_INPUT_SHARE_THINKING = 0.35
# Upper bound for snapshots of pages loaded in parts; only the parts are sent to the model.
SNAPSHOT_LIMIT = 2000000

confspec = {
	"provider": "option('gemini', 'openai', 'ollama', 'server', default='gemini')",
	"apiKey": "string(default='')",
	"model": "string(default='%s')" % DEFAULT_MODEL,
	"geminiThinking": "option('minimal', 'low', 'medium', 'high', 'default', default='low')",
	"openaiModel": "string(default='%s')" % DEFAULT_OPENAI_MODEL,
	"openaiReasoning": "option('none', 'low', 'medium', 'high', 'default', default='low')",
	"ollamaUrl": "string(default='%s')" % DEFAULT_OLLAMA_URL,
	"ollamaModel": "string(default='')",
	"ollamaContextSize": "integer(default=32768, min=2048, max=1048576)",
	"ollamaThinking": "option('off', 'on', 'default', default='off')",
	"serverUrl": "string(default='%s')" % DEFAULT_SERVER_URL,
	"serverModel": "string(default='')",
	"serverApiKey": "string(default='')",
	"serverContextSize": "integer(default=32768, min=2048, max=1048576)",
	"serverThinking": "option('off', 'on', 'default', default='off')",
	"clickMethod": "option('action', 'mouse', default='action')",
	"maxChars": "integer(default=150000, min=5000, max=2000000)",
	"partChars": "integer(default=30000, min=0, max=2000000)",
	"timeout": "integer(default=300, min=10, max=1800)",
}

PROVIDERS = (
	# Translators: an LLM provider in the settings panel.
	("gemini", _("Google Gemini (cloud)")),
	# Translators: an LLM provider in the settings panel.
	("openai", _("OpenAI (cloud)")),
	# Translators: an LLM provider in the settings panel.
	("ollama", _("Ollama (local or self-hosted)")),
	# Translators: an LLM provider in the settings panel.
	("server", _("OpenAI-compatible server (llama.cpp, LM Studio, Foundry Local)")),
)

THINKING_MODES = (
	# Translators: a thinking mode for local models in the settings panel.
	("off", _("Off (much faster)")),
	# Translators: a thinking mode for local models in the settings panel.
	("on", _("On (much slower, may handle complex pages better; thinking models only)")),
	# Translators: a thinking mode for local models in the settings panel.
	("default", _("Model's default")),
)
REASONING_EFFORTS = (
	# Translators: an OpenAI reasoning effort in the settings panel.
	("none", _("None (fastest; not supported by every model)")),
	# Translators: an OpenAI reasoning effort in the settings panel.
	("low", _("Low (recommended)")),
	# Translators: an OpenAI reasoning effort in the settings panel.
	("medium", _("Medium (slower)")),
	# Translators: an OpenAI reasoning effort in the settings panel.
	("high", _("High (much slower)")),
	# Translators: an OpenAI reasoning effort in the settings panel.
	("default", _("Model's default")),
)
GEMINI_THINKING_LEVELS = (
	# Translators: a Gemini thinking level in the settings panel.
	("minimal", _("Minimal (fastest; not supported by every model)")),
	# Translators: a Gemini thinking level in the settings panel.
	("low", _("Low (recommended)")),
	# Translators: a Gemini thinking level in the settings panel.
	("medium", _("Medium (slower)")),
	# Translators: a Gemini thinking level in the settings panel.
	("high", _("High (much slower)")),
	# Translators: a Gemini thinking level in the settings panel.
	("default", _("Model's default")),
)
# Value of the providers' think parameter for each mode; None leaves it unset.
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


def getOpenAIKey():
	return os.environ.get(OPENAI_KEY_VARIABLE, "").strip()


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
	if c["provider"] == "openai":
		apiKey = getOpenAIKey()
		if not apiKey:
			return None, _(
				# Translators: reported when the OpenAI key environment variable is missing; {name} is its name.
				"No OpenAI API key. Set the {name} environment variable, then restart NVDA."
			).format(name=OPENAI_KEY_VARIABLE)
		effort = c["openaiReasoning"]
		provider = llm.OpenAIProvider(
			apiKey,
			c["openaiModel"].strip() or DEFAULT_OPENAI_MODEL,
			c["timeout"],
			None if effort == "default" else effort,
		)
		return provider, None
	if c["provider"] == "server":
		provider = llm.OpenAICompatibleProvider(
			c["serverUrl"].strip() or DEFAULT_SERVER_URL,
			c["serverModel"].strip(),
			c["serverApiKey"].strip(),
			c["timeout"],
			THINK_PARAM[c["serverThinking"]],
		)
		return provider, None
	apiKey = getApiKey()
	if not apiKey:
		# Translators: reported when no API key is configured.
		return None, _("No Gemini API key. Set one in NVDA settings, Accessible Webpage category.")
	level = c["geminiThinking"]
	return llm.GeminiProvider(apiKey, c["model"], c["timeout"], None if level == "default" else level), None


def providerId():
	"""Identifies the configured provider and model, e.g. for cache keys."""
	c = conf()
	if c["provider"] == "ollama":
		return "ollama:%s:%s:%d:%s" % (c["ollamaUrl"], c["ollamaModel"], c["ollamaContextSize"], c["ollamaThinking"])
	if c["provider"] == "openai":
		return "openai:%s:%s" % (c["openaiModel"], c["openaiReasoning"])
	if c["provider"] == "server":
		return "server:%s:%s:%s" % (c["serverUrl"], c["serverModel"], c["serverThinking"])
	return "gemini:%s:%s" % (c["model"], c["geminiThinking"])


def effectiveMaxChars():
	"""The snapshot size limit, capped so local models never overflow their context."""
	c = conf()
	if c["provider"] in ("ollama", "server"):
		context, thinking = c[c["provider"] + "ContextSize"], c[c["provider"] + "Thinking"]
		share = LOCAL_INPUT_SHARE if thinking == "off" else LOCAL_INPUT_SHARE_THINKING
		return min(c["maxChars"], int(context * CHARS_PER_TOKEN * share))
	return c["maxChars"]


def effectivePartChars():
	"""How much of a long page is rebuilt at a time, or 0 to send the whole page at once."""
	part = conf()["partChars"]
	return min(max(part, 5000), effectiveMaxChars()) if part else 0


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
		self.geminiThinkingChoice = gemini.addLabeledControl(
			# Translators: label in the settings panel.
			_("Gemini thinking le&vel:"),
			wx.Choice,
			choices=[label for _key, label in GEMINI_THINKING_LEVELS],
		)
		self.geminiThinkingChoice.SetSelection(_choiceIndex(GEMINI_THINKING_LEVELS, c["geminiThinking"]))

		# Translators: title of the OpenAI settings group.
		openaiSizer = wx.StaticBoxSizer(wx.VERTICAL, self, label=_("OpenAI"))
		self.openaiBox = openaiSizer.GetStaticBox()
		openai = helper.addItem(guiHelper.BoxSizerHelper(self.openaiBox, sizer=openaiSizer))
		if getOpenAIKey():
			# Translators: shown in the settings panel; {name} is the environment variable.
			keyStatus = _("The API key is read from the {name} environment variable, which is set.")
		else:
			# Translators: shown in the settings panel; {name} is the environment variable.
			keyStatus = _("The API key is read from the {name} environment variable, which is not set. Set it, then restart NVDA.")
		openai.addItem(wx.StaticText(self.openaiBox, label=keyStatus.format(name=OPENAI_KEY_VARIABLE)))
		# Translators: label in the settings panel.
		self.openaiModelEdit = openai.addLabeledControl(_("OpenAI mode&l:"), wx.TextCtrl)
		self.openaiModelEdit.SetValue(c["openaiModel"])
		self.openaiReasoningChoice = openai.addLabeledControl(
			# Translators: label in the settings panel.
			_("Rea&soning effort:"),
			wx.Choice,
			choices=[label for _key, label in REASONING_EFFORTS],
		)
		self.openaiReasoningChoice.SetSelection(_choiceIndex(REASONING_EFFORTS, c["openaiReasoning"]))

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
		self.ollamaRefreshButton = ollama.addItem(wx.Button(self.ollamaBox, label=_("&Refresh installed models")))
		self.ollamaRefreshButton.Bind(
			wx.EVT_BUTTON,
			lambda evt: self._refreshModels(
				self.ollamaRefreshButton,
				self.ollamaModelCombo,
				lambda: llm.listOllamaModels(self.ollamaUrlEdit.GetValue().strip() or DEFAULT_OLLAMA_URL),
			),
		)
		self.ollamaThinkingChoice = ollama.addLabeledControl(
			# Translators: label in the settings panel.
			_("T&hinking:"),
			wx.Choice,
			choices=[label for _key, label in THINKING_MODES],
		)
		self.ollamaThinkingChoice.SetSelection(_choiceIndex(THINKING_MODES, c["ollamaThinking"]))
		self.ollamaContextSpin = ollama.addLabeledControl(
			# Translators: label in the settings panel.
			_("Ollama context si&ze (tokens; larger pages need more memory):"),
			nvdaControls.SelectOnFocusSpinCtrl,
			min=2048,
			max=1048576,
			initial=c["ollamaContextSize"],
		)

		# Translators: title of the OpenAI-compatible server settings group.
		serverSizer = wx.StaticBoxSizer(wx.VERTICAL, self, label=_("OpenAI-compatible server"))
		self.serverBox = serverSizer.GetStaticBox()
		server = helper.addItem(guiHelper.BoxSizerHelper(self.serverBox, sizer=serverSizer))
		self.serverUrlEdit = server.addLabeledControl(
			# Translators: label in the settings panel.
			_("Server a&ddress (llama.cpp: http://localhost:8080/v1, LM Studio: http://localhost:1234/v1):"),
			wx.TextCtrl,
		)
		self.serverUrlEdit.SetValue(c["serverUrl"])
		self.serverModelCombo = server.addLabeledControl(
			# Translators: label in the settings panel.
			_("Server mod&el (may stay empty for llama.cpp):"),
			wx.ComboBox,
			choices=[c["serverModel"]] if c["serverModel"] else [],
		)
		self.serverModelCombo.SetValue(c["serverModel"])
		# Translators: button that lists the models an OpenAI-compatible server offers.
		self.serverRefreshButton = server.addItem(wx.Button(self.serverBox, label=_("Re&fresh server models")))
		self.serverRefreshButton.Bind(
			wx.EVT_BUTTON,
			lambda evt: self._refreshModels(
				self.serverRefreshButton,
				self.serverModelCombo,
				lambda: llm.listOpenAIModels(
					self.serverUrlEdit.GetValue().strip() or DEFAULT_SERVER_URL,
					self.serverApiKeyEdit.GetValue().strip(),
				),
			),
		)
		self.serverApiKeyEdit = server.addLabeledControl(
			# Translators: label in the settings panel.
			_("Server API ke&y (usually not needed for local servers):"),
			wx.TextCtrl,
			style=wx.TE_PASSWORD,
		)
		self.serverApiKeyEdit.SetValue(c["serverApiKey"])
		self.serverThinkingChoice = server.addLabeledControl(
			# Translators: label in the settings panel.
			_("Server thinkin&g:"),
			wx.Choice,
			choices=[label for _key, label in THINKING_MODES],
		)
		self.serverThinkingChoice.SetSelection(_choiceIndex(THINKING_MODES, c["serverThinking"]))
		self.serverContextSpin = server.addLabeledControl(
			# Translators: label in the settings panel.
			_("Server conte&xt size (tokens; set it to the context the server was started with):"),
			nvdaControls.SelectOnFocusSpinCtrl,
			min=2048,
			max=1048576,
			initial=c["serverContextSize"],
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
		self.partCharsSpin = helper.addLabeledControl(
			# Translators: label in the settings panel.
			_("Load long pages in p&arts of (characters; 0 sends the whole page at once):"),
			nvdaControls.SelectOnFocusSpinCtrl,
			min=0,
			max=2000000,
			initial=c["partChars"],
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
		selected = self._selectedProvider()
		self.geminiBox.Enable(selected == "gemini")
		self.openaiBox.Enable(selected == "openai")
		self.ollamaBox.Enable(selected == "ollama")
		self.serverBox.Enable(selected == "server")

	def _refreshModels(self, button, combo, fetch):
		"""Fills combo with the model names returned by fetch(), which runs in the background."""
		button.Disable()

		def worker():
			try:
				models, error = fetch(), None
			except Exception as e:
				models, error = [], e
			wx.CallAfter(self._onModels, button, combo, models, error)

		threading.Thread(target=worker, daemon=True).start()

	def _onModels(self, button, combo, models, error):
		if not self:  # The settings dialog was closed meanwhile.
			return
		button.Enable()
		if error:
			ui.message(str(error))
			return
		if not models:
			# Translators: reported when a server offers no models.
			ui.message(_("The server offers no models"))
			return
		current = combo.GetValue().strip()
		combo.SetItems(models)
		combo.SetValue(current if current in models else models[0])
		# Translators: reported after listing models; {count} is how many were found.
		ui.message(_("{count} models found").format(count=len(models)))

	def onSave(self):
		c = conf()
		c["provider"] = self._selectedProvider()
		c["apiKey"] = self.apiKeyEdit.GetValue().strip()
		c["model"] = self.modelEdit.GetValue().strip() or DEFAULT_MODEL
		c["geminiThinking"] = GEMINI_THINKING_LEVELS[self.geminiThinkingChoice.GetSelection()][0]
		c["openaiModel"] = self.openaiModelEdit.GetValue().strip() or DEFAULT_OPENAI_MODEL
		c["openaiReasoning"] = REASONING_EFFORTS[self.openaiReasoningChoice.GetSelection()][0]
		c["ollamaUrl"] = self.ollamaUrlEdit.GetValue().strip() or DEFAULT_OLLAMA_URL
		c["ollamaModel"] = self.ollamaModelCombo.GetValue().strip()
		c["ollamaContextSize"] = self.ollamaContextSpin.GetValue()
		c["ollamaThinking"] = THINKING_MODES[self.ollamaThinkingChoice.GetSelection()][0]
		c["serverUrl"] = self.serverUrlEdit.GetValue().strip() or DEFAULT_SERVER_URL
		c["serverModel"] = self.serverModelCombo.GetValue().strip()
		c["serverApiKey"] = self.serverApiKeyEdit.GetValue().strip()
		c["serverContextSize"] = self.serverContextSpin.GetValue()
		c["serverThinking"] = THINKING_MODES[self.serverThinkingChoice.GetSelection()][0]
		c["clickMethod"] = CLICK_METHODS[self.clickMethodChoice.GetSelection()][0]
		c["maxChars"] = self.maxCharsSpin.GetValue()
		c["partChars"] = self.partCharsSpin.GetValue()
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
