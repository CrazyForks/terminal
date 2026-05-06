# Dialogs

Native alerts, confirms, prompts, and beforeunload dialogs freeze page JavaScript. Check `page_info()` or load tracing helpers for `pending_dialog()`.

```python
info = page_info()
load_skill("tracing")
dialog = pending_dialog()
cdp("Page.handleJavaScriptDialog", accept=True)
```

Handle the dialog before trying more `js(...)` calls.
