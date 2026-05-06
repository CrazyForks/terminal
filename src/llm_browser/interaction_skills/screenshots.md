# Screenshots

Use screenshots as the default observation loop.

```python
new_tab("https://example.com")
wait_for_load()
capture_screenshot()
click_at_xy(410, 520)
wait_for_network_idle()
capture_screenshot()
```

Prefer visible verification over assuming a DOM action worked. Use `screenshot(label, attach=True)` when you want the image returned to the model immediately, and `capture_screenshot(path)` when you also need a file.
