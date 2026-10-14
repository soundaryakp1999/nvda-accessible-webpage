# Accessible Webpage (NVDA add-on)

Rebuilds an inaccessible web page as clean, accessible HTML using Google Gemini, OpenAI or a local model, then passes
whatever you do in that view back to the original page, the way the page expects.

## Usage

| Gesture | Action |
| --- | --- |
| NVDA+shift+c | Rebuild the current page |
| NVDA+shift+control+c | Ask "what do you want to do?" first, then rebuild around that goal |

Both gestures can be changed in NVDA's Input Gestures dialog, in the "Accessible Webpage" category.

In the rebuilt view:

- Read it in browse mode like any web page. It has headings, landmarks, labelled fields and grouped options.
- Fill in fields, then activate a button or link. The view closes. The add-on fills in the same fields on the
  real page and activates the real button or link.
- Buttons that only show or hide content (accordions, sidebars, tabs, "show more") work inside the view:
  - If the content was already on the page, the view shows or hides it immediately.
  - If the content doesn't exist until you click, the view stays open. The add-on activates the real
    control in the background, rebuilds only what appeared, and inserts it after the button.
    NVDA says "Expanding", then describes what appeared.
- "Fill in the original page without submitting" fills in the fields without clicking anything.
- Escape closes the view without doing anything.

If the page changes after you act (a new step, an error message), press NVDA+shift+c again.

## Setup

Open NVDA settings, go to the "Accessible Webpage" category, and choose an AI provider.

**Google Gemini (cloud)**

1. Get an API key from Google AI Studio.
2. Enter the key in the settings. You can also set the `GEMINI_API_KEY` environment variable instead.
3. You can change the model there too (default `gemini-3.8-flash`).

**OpenAI (cloud)**

1. Create an API key in the OpenAI platform dashboard.
2. Set the `OPENAI_API_KEY` environment variable to the key, for example with
   `setx OPENAI_API_KEY "sk-..."`, then restart NVDA so it sees the new variable. The key is only read
   from the environment and never stored in NVDA's configuration.
3. Choose OpenAI as the provider. The default model is `gpt-6-luna`, OpenAI's fastest and cheapest model.
   For harder pages, try `gpt-6.1-sol`.
4. Reasoning effort defaults to Low. None is fastest but not every model supports it (`gpt-6.1-sol`
   doesn't); Medium and High are much slower.

**OpenAI-compatible server (local, fastest on Snapdragon X / Windows on Arm)**

This works with llama.cpp's `llama-server`, LM Studio, Foundry Local and other servers that offer an
OpenAI-style `/v1/chat/completions` endpoint.

1. Start the server. For llama.cpp, download the `win-cpu-arm64` (or your platform's) release and run
   `llama-server -m model.gguf -c 32768 --port 8080`.
2. Enter the server address (llama.cpp: `http://localhost:8080/v1`, LM Studio: `http://localhost:1234/v1`).
   The model can stay empty for llama.cpp. "Refresh server models" lists what the server offers.
3. Set the context size to the value the server was started with (`-c`). The add-on shortens the page to fit.
4. Thinking works as for Ollama. It's sent as `chat_template_kwargs.enable_thinking`, which llama.cpp
   supports.

Measured on a Snapdragon X Elite (Surface Laptop 7, 32 GB) with a 10,000-token shop page:

- Qwen3-4B on the Adreno GPU (llama.cpp OpenCL build, `-ngl 99 -fa on`): 17 minutes.
- Ollama with qwen3.5:35b on the CPU: 23 minutes.
- With a long page in context, the Adreno GPU was 2 to 4 times faster than the CPU. Models over about
  8B don't fit in the GPU's memory limit.
- llama.cpp's CPU build is much faster than Ollama for older architectures (Llama 3.2: 4 times faster
  reading) but not for Qwen 3.5.
- Most of the time goes into writing the HTML (about 8,000 tokens for this page), so page size matters
  more than hardware settings.

**Ollama (local, nothing leaves your computer)**

1. Install Ollama and download a model, for example `ollama pull qwen3.5:35b`.
2. Enter the server address (default `http://localhost:11434`), press "Refresh installed models", and
   choose a model.
3. Context size: the add-on automatically shortens the page to fit this size. Larger values keep more
   of the page but use more memory.
4. Thinking: "Off" (the default) works with every model and is much faster. "On" makes thinking models
   (such as qwen3.5 or deepseek-r1) reason before answering. It is several times slower, but in testing it
   handled expand/collapse buttons more accurately. Models that can't think report an error when it is on.
   "Model's default" leaves the choice to the model; thinking models then think.
5. Local models are much slower than Gemini, so raise the request timeout if needed. It defaults to 300
   seconds, and larger models can need several minutes per page. Small models (around 3B) often write
   poor HTML, so a model of about 30B or more works noticeably better.

## Build

```
python build.py
```

This produces `dist/accessibleWebpage-<version>.nvda-addon`. Open that file to install it.

## How it works

1. **Snapshot** ([snapshot.py](addon/globalPlugins/accessibleWebpage/snapshot.py)): walks NVDA's browse-mode
   buffer and writes compact pseudo-HTML. It includes roles, names, states and the source tag, id and class,
   which is how the model spots a `div` that is really a button. Hints that only restate the role, generated
   ids and purely visual classes (Tailwind and Bootstrap utilities, CSS-in-JS hashes) are left out. Each
   element you can interact with gets a ref (`e12`) that maps to the NVDA identifier of the live element.
   Long texts get an id (`t3`) that the model references instead of copying, so they appear word for word
   and cost no output tokens. Password contents are never sent.
2. **Rebuild** ([llm.py](addon/globalPlugins/accessibleWebpage/llm.py), [prompt.py](addon/globalPlugins/accessibleWebpage/prompt.py)):
   The model returns `{title, language, summary, html}` as structured JSON. Every control in the HTML has a
   `data-ref` pointing back to the original element.
3. **Sanitize** ([sanitizer.py](addon/globalPlugins/accessibleWebpage/sanitizer.py)): an allowlist that
   strips scripts, event handlers and external resources, and removes any `data-ref` the model made up.
   It also fills in what the model is told to leave out: referenced texts, `href`, button types,
   `aria-required` and `aria-expanded`.
4. **View** ([viewer.py](addon/globalPlugins/accessibleWebpage/viewer.py)): a modal MSHTML dialog, the same
   engine NVDA uses for browseable messages. The page's script returns your interaction as JSON.
   The page is served from a loopback server protected by a random token
   ([live.py](addon/globalPlugins/accessibleWebpage/live.py)), so it can ask NVDA to expand controls
   while the view stays open. After each expand, the add-on takes a new snapshot of the page and
   sends only the new or changed lines to the model. Element IDs stay the same across snapshots, so
   new fields and buttons still link to the real page.
   MSHTML runs these dialogs in IE8 document mode whatever the page asks for, so the viewer script
   uses only what IE8 supports, and the sanitizer turns HTML5 elements such as `nav` and `main` into
   `div`s with the matching ARIA role.
5. **Replay** ([actions.py](addon/globalPlugins/accessibleWebpage/actions.py)): brings the browser to the
   front, focuses each changed field, selects its contents and types the new value, so React and similar
   frameworks get real input events. It toggles checkboxes and radios, picks options, and then clicks the
   target using its accessibility action, falling back to a real mouse click.

## Privacy

With Gemini or OpenAI, the page's text, structure and field values are sent to Google or OpenAI. OpenAI
requests are made with `store: false`, so they are not kept as stored responses. Password values and URL query
strings are never sent. For sensitive pages, use Ollama on your own computer so nothing leaves it.

## Limitations

- Content hidden from the accessibility tree (for example `aria-hidden`) or drawn on a canvas is invisible
  to NVDA, so the model can't see it either.
- Labels the model guessed from class names end with "(guessed)".
- Very large pages are cut off at the configured character limit. Use the goal-based rebuild for those.
