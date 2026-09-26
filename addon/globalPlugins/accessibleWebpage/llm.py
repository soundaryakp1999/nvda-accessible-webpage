"""LLM providers. Each provider exposes generateJson(system, user, schema) -> dict.

Schemas are written in Gemini's dialect (uppercase types, propertyOrdering);
providers that expect standard JSON Schema convert them with toJsonSchema.
"""

import json
import ssl
import urllib.error
import urllib.request

SETTINGS_HINT = "Check the settings in NVDA settings, Accessible Webpage category."


class LLMError(Exception):
	pass


def _request(service, url, body=None, headers=None, timeout=120):
	"""Sends a JSON request (GET when body is None) and returns the decoded JSON response."""
	request = urllib.request.Request(
		url,
		data=None if body is None else json.dumps(body).encode("utf-8"),
		headers=dict(headers or {}, **{"Content-Type": "application/json"}),
		method="GET" if body is None else "POST",
	)
	context = ssl.create_default_context() if url.lower().startswith("https:") else None
	try:
		with urllib.request.urlopen(request, timeout=timeout, context=context) as resp:
			return json.loads(resp.read().decode("utf-8"))
	except urllib.error.HTTPError as e:
		message = "%s returned HTTP %d: %s" % (service, e.code, _errorMessage(e))
		if e.code == 404:
			message += " " + SETTINGS_HINT
		raise LLMError(message)
	except urllib.error.URLError as e:
		raise LLMError("Could not reach %s at %s: %s" % (service, url.split("/v1", 1)[0].split("/api/", 1)[0], e.reason))
	except TimeoutError:
		raise LLMError("%s did not answer within %d seconds" % (service, timeout))
	except ValueError:
		raise LLMError("%s returned an invalid response" % service)


def _errorMessage(httpError):
	try:
		error = json.loads(httpError.read().decode("utf-8"))["error"]
		# Gemini: {"error": {"message": ...}}; Ollama: {"error": "..."}.
		return error["message"] if isinstance(error, dict) else str(error)
	except Exception:
		return httpError.reason


def _parseJson(text):
	text = text.strip()
	if text.startswith("```"):
		text = text.split("\n", 1)[1] if "\n" in text else ""
		text = text.rsplit("```", 1)[0]
	try:
		return json.loads(text)
	except ValueError:
		raise LLMError("The model returned an invalid response")


def toJsonSchema(schema):
	"""Converts a Gemini-style schema into standard JSON Schema."""
	out = {}
	for key, value in schema.items():
		if key == "propertyOrdering":
			continue
		if key == "type":
			out[key] = value.lower()
		elif key == "properties":
			out[key] = {name: toJsonSchema(sub) for name, sub in value.items()}
		elif key == "items":
			out[key] = toJsonSchema(value)
		else:
			out[key] = value
	return out


class GeminiProvider:
	ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

	def __init__(self, apiKey, model, timeout=120):
		self.apiKey = apiKey
		self.model = model[len("models/"):] if model.startswith("models/") else model
		self.timeout = timeout

	def generateJson(self, system, user, schema):
		body = {
			"systemInstruction": {"parts": [{"text": system}]},
			"contents": [{"role": "user", "parts": [{"text": user}]}],
			"generationConfig": {
				"temperature": 0.2,
				"responseMimeType": "application/json",
				"responseSchema": schema,
			},
		}
		data = _request(
			"Gemini", self.ENDPOINT.format(model=self.model), body, {"x-goog-api-key": self.apiKey}, self.timeout
		)
		feedback = data.get("promptFeedback") or {}
		if feedback.get("blockReason"):
			raise LLMError("Gemini blocked the request: %s" % feedback["blockReason"])
		candidates = data.get("candidates") or []
		if not candidates:
			raise LLMError("Gemini returned no answer")
		candidate = candidates[0]
		text = "".join(
			p.get("text", "")
			for p in (candidate.get("content") or {}).get("parts", [])
			if not p.get("thought")
		)
		if candidate.get("finishReason") == "MAX_TOKENS":
			raise LLMError("The page is too large for the model's output limit. Try the goal-based rebuild instead.")
		return _parseJson(text)


class OllamaProvider:
	"""A local or remote Ollama server, using /api/chat with structured output."""

	def __init__(self, baseUrl, model, timeout=120, contextSize=32768, think=None):
		"""think: True or False to force thinking on or off, None for the model's default."""
		self.baseUrl = baseUrl.rstrip("/")
		self.model = model
		self.timeout = timeout
		self.contextSize = contextSize
		self.think = think

	def generateJson(self, system, user, schema):
		body = {
			"model": self.model,
			"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
			"stream": False,
			"format": toJsonSchema(schema),
			# Ollama's default context is far too small for a page snapshot and it truncates silently.
			"options": {"temperature": 0.2, "num_ctx": self.contextSize},
		}
		# Thinking models think by default, which is many times slower.
		if self.think is not None:
			body["think"] = self.think
		try:
			data = _request("Ollama", self.baseUrl + "/api/chat", body, timeout=self.timeout)
		except LLMError as e:
			if "does not support thinking" in str(e):
				raise LLMError("%s. Turn thinking off in NVDA settings, Accessible Webpage category." % e)
			raise
		if data.get("error"):
			raise LLMError("Ollama: %s" % data["error"])
		if data.get("done_reason") == "length":
			raise LLMError(
				"The answer did not fit in Ollama's context. Increase the Ollama context size or try the goal-based rebuild."
			)
		return _parseJson((data.get("message") or {}).get("content", ""))


def normalizeOpenAIUrl(baseUrl):
	"""Accepts "http://host:port" or "http://host:port/v1" and returns the /v1 base."""
	baseUrl = baseUrl.strip().rstrip("/")
	return baseUrl if baseUrl.endswith("/v1") else baseUrl + "/v1"


class OpenAICompatibleProvider:
	"""Any server with an OpenAI-style /v1/chat/completions endpoint:
	llama.cpp's llama-server, LM Studio, Foundry Local, vLLM and others.
	"""

	SERVICE = "The local AI server"

	def __init__(self, baseUrl, model="", apiKey="", timeout=300, think=None):
		"""think: True or False to force thinking on or off, None for the model's default."""
		self.baseUrl = normalizeOpenAIUrl(baseUrl)
		self.model = model
		self.apiKey = apiKey
		self.timeout = timeout
		self.think = think

	def generateJson(self, system, user, schema):
		body = {
			# llama-server serves whatever model it loaded and ignores this; others require it.
			"model": self.model or "default",
			"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
			"temperature": 0.2,
			"stream": False,
			"response_format": {
				"type": "json_schema",
				"json_schema": {"name": "response", "schema": toJsonSchema(schema)},
			},
		}
		if self.think is not None:
			# Understood by llama-server and by chat templates of thinking models such as Qwen.
			body["chat_template_kwargs"] = {"enable_thinking": self.think}
		headers = {"Authorization": "Bearer %s" % self.apiKey} if self.apiKey else {}
		data = _request(self.SERVICE, self.baseUrl + "/chat/completions", body, headers, self.timeout)
		choices = data.get("choices") or []
		if not choices:
			raise LLMError("%s returned no answer" % self.SERVICE)
		if choices[0].get("finish_reason") == "length":
			raise LLMError(
				"The answer did not fit in the server's context. Start the server with a larger context "
				"or try the goal-based rebuild."
			)
		return _parseJson((choices[0].get("message") or {}).get("content") or "")


def listOpenAIModels(baseUrl, apiKey="", timeout=5):
	"""Returns the model ids an OpenAI-compatible server offers."""
	headers = {"Authorization": "Bearer %s" % apiKey} if apiKey else {}
	data = _request(OpenAICompatibleProvider.SERVICE, normalizeOpenAIUrl(baseUrl) + "/models", headers=headers, timeout=timeout)
	return sorted(m["id"] for m in data.get("data") or [] if m.get("id"))


def listOllamaModels(baseUrl, timeout=5):
	"""Returns the names of the models installed on an Ollama server."""
	data = _request("Ollama", baseUrl.rstrip("/") + "/api/tags", timeout=timeout)
	return sorted(m["name"] for m in data.get("models") or [] if m.get("name"))
