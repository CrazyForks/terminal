# Downloads

Load the downloads skill when the task creates or inspects downloaded files.

```python
load_skill("downloads")
click_at_xy(500, 420)
item = wait_for_download("*.csv", timeout=30)
result = item["path"]
```

`download_info()` lists the current download directory, known files, and CDP download events.
