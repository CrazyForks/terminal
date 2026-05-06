# Iframes

Try compositor-level input first. Browser-process coordinate clicks usually pass through same-origin, cross-origin, and shadow DOM boundaries.

```python
capture_screenshot()
click_at_xy(612, 448)
capture_screenshot()
```

Use `iframe_target(url_substr)` only when you need DOM inspection inside a specific frame. For raw frame work, drop to `cdp(...)`.
