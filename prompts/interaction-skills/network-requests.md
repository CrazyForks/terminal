# Network Requests

Use this when page state is ambiguous, a search/filter action updates results without a clear DOM route, or the task may be faster through API/download endpoints.

Default flow:

1. Navigate or perform the visible search/filter action.
2. Call `network_resources_snapshot(keywords=[...])` with task terms such as `["search", "results", "download", "pdf", "csv"]`.
3. Prefer high-scoring API, JSON, CSV, PDF, export, pagination, and form URLs over visual page walking.
4. Use `http_get_many(...)` for public static URLs, `browser_fetch_many(...)` when browser cookies/session are needed, and `read_document_text(...)` for documents.
5. Verify extracted records against visible DOM, row/card helpers, or final artifact audits before `done`.

Do not repeatedly click pagination or detail pages when the loaded page has already exposed stable API, export, or document URLs.
