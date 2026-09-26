# Accessible Webpage (NVDA add-on)

Rebuilds an inaccessible web page as clean, accessible HTML using Google Gemini, then passes
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
   which is how the model spots a `div` that is really a button. Each element you can interact with gets a
   ref (`e12`) that maps to the NVDA identifier of the live element. Password contents are never sent.
2. **Rebuild** ([llm.py](addon/globalPlugins/accessibleWebpage/llm.py), [prompt.py](addon/globalPlugins/accessibleWebpage/prompt.py)):
   Gemini returns `{title, language, summary, html}` as structured JSON. Every control in the HTML has a
   `data-ref` pointing back to the original element.
3. **Sanitize** ([sanitizer.py](addon/globalPlugins/accessibleWebpage/sanitizer.py)): an allowlist that
   strips scripts, event handlers and external resources, and removes any `data-ref` the model made up.
4. **View** ([viewer.py](addon/globalPlugins/accessibleWebpage/viewer.py)): a modal MSHTML dialog, the same
   engine NVDA uses for browseable messages. The page's script returns your interaction as JSON.
   The page is served from a loopback server protected by a random token
   ([live.py](addon/globalPlugins/accessibleWebpage/live.py)), so it can ask NVDA to expand controls
   while the view stays open. After each expand, the add-on takes a new snapshot of the page and
   sends only the new or changed lines to Gemini. Element IDs stay the same across snapshots, so
   new fields and buttons still link to the real page.
5. **Replay** ([actions.py](addon/globalPlugins/accessibleWebpage/actions.py)): brings the browser to the
   front, focuses each changed field, selects its contents and types the new value, so React and similar
   frameworks get real input events. It toggles checkboxes and radios, picks options, and then clicks the
   target using its accessibility action, falling back to a real mouse click.

## Privacy

With Gemini, the page's text, structure and field values are sent to Google. Password values and URL query
strings are never sent. For sensitive pages, use Ollama on your own computer so nothing leaves it.

## Limitations

- Content hidden from the accessibility tree (for example `aria-hidden`) or drawn on a canvas is invisible
  to NVDA, so the model can't see it either.
- Labels the model guessed from class names end with "(guessed)".
- Very large pages are cut off at the configured character limit. Use the goal-based rebuild for those.
