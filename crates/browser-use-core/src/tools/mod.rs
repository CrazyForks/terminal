use browser_use_protocol::ToolSpec;

pub(crate) mod command;
pub(crate) mod files;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum ToolHandlerKind {
    Done,
    Python,
    ExecCommand,
    WriteStdin,
    ApplyPatch,
    ReadFile,
    SearchFiles,
    ListFiles,
    ViewImage,
    SpawnAgent,
    WaitAgent,
    SendMessage,
    FollowupTask,
    ListAgents,
    CloseAgent,
}

#[derive(Clone, Debug)]
pub(crate) struct RegisteredTool {
    spec: ToolSpec,
    handler: ToolHandlerKind,
}

#[derive(Clone, Debug, Default)]
pub(crate) struct ToolRegistry {
    tools: Vec<RegisteredTool>,
}

impl ToolRegistry {
    pub(crate) fn browser_agent() -> Self {
        let mut registry = Self::default();
        registry.register(exec_command_tool_spec(), ToolHandlerKind::ExecCommand);
        registry.register(write_stdin_tool_spec(), ToolHandlerKind::WriteStdin);
        registry.register(apply_patch_tool_spec(), ToolHandlerKind::ApplyPatch);
        registry.register(read_file_tool_spec(), ToolHandlerKind::ReadFile);
        registry.register(search_files_tool_spec(), ToolHandlerKind::SearchFiles);
        registry.register(list_files_tool_spec(), ToolHandlerKind::ListFiles);
        registry.register(view_image_tool_spec(), ToolHandlerKind::ViewImage);
        registry.register(python_tool_spec(), ToolHandlerKind::Python);
        registry.register(done_tool_spec(), ToolHandlerKind::Done);
        registry.register(spawn_agent_tool_spec(), ToolHandlerKind::SpawnAgent);
        registry.register(wait_agent_tool_spec(), ToolHandlerKind::WaitAgent);
        registry.register(send_message_tool_spec(), ToolHandlerKind::SendMessage);
        registry.register(followup_task_tool_spec(), ToolHandlerKind::FollowupTask);
        registry.register(list_agents_tool_spec(), ToolHandlerKind::ListAgents);
        registry.register(close_agent_tool_spec(), ToolHandlerKind::CloseAgent);
        registry
    }

    pub(crate) fn register(&mut self, spec: ToolSpec, handler: ToolHandlerKind) {
        self.tools.push(RegisteredTool { spec, handler });
    }

    pub(crate) fn specs(&self) -> Vec<ToolSpec> {
        self.tools.iter().map(|tool| tool.spec.clone()).collect()
    }

    pub(crate) fn handler_for(&self, name: &str) -> Option<ToolHandlerKind> {
        self.tools
            .iter()
            .find(|tool| tool.spec.name == name)
            .map(|tool| tool.handler)
    }
}

fn exec_command_tool_spec() -> ToolSpec {
    ToolSpec {
        name: "exec_command".to_string(),
        description: "Runs a command, returning output or a session ID for ongoing interaction."
            .to_string(),
        input_schema: serde_json::json!({
            "type": "object",
            "properties": {
                "cmd": {
                    "type": "string",
                    "description": "Shell command to execute."
                },
                "workdir": {
                    "type": "string",
                    "description": "Optional working directory to run the command in; defaults to the task cwd."
                },
                "shell": {
                    "type": "string",
                    "description": "Shell binary to launch. Defaults to the user's default shell."
                },
                "tty": {
                    "type": "boolean",
                    "description": "Whether to request a TTY. Currently accepted for Codex compatibility; PTY allocation is a follow-up hardening item."
                },
                "login": {
                    "type": "boolean",
                    "description": "Whether to run the shell with login semantics."
                },
                "yield_time_ms": {
                    "type": "integer",
                    "description": "How long to wait in milliseconds for output before yielding."
                },
                "max_output_tokens": {
                    "type": "integer",
                    "description": "Maximum number of tokens to return. Excess output will be truncated."
                }
            },
            "required": ["cmd"],
            "additionalProperties": false
        }),
    }
}

fn write_stdin_tool_spec() -> ToolSpec {
    ToolSpec {
        name: "write_stdin".to_string(),
        description: "Writes characters to an existing command session and returns recent output."
            .to_string(),
        input_schema: serde_json::json!({
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Identifier of the running command session."
                },
                "chars": {
                    "type": "string",
                    "description": "Bytes to write to stdin. May be empty to poll."
                },
                "yield_time_ms": {
                    "type": "integer",
                    "description": "How long to wait in milliseconds for output before yielding."
                },
                "max_output_tokens": {
                    "type": "integer",
                    "description": "Maximum number of tokens to return. Excess output will be truncated."
                }
            },
            "required": ["session_id"],
            "additionalProperties": false
        }),
    }
}

fn apply_patch_tool_spec() -> ToolSpec {
    ToolSpec {
        name: "apply_patch".to_string(),
        description: concat!(
            "Apply a Codex-style patch to local files. The patch string must use ",
            "*** Begin Patch / *** End Patch with add, delete, update, and move directives."
        )
        .to_string(),
        input_schema: serde_json::json!({
            "type": "object",
            "properties": {
                "patch": {
                    "type": "string",
                    "description": "Codex-style patch body."
                }
            },
            "required": ["patch"],
            "additionalProperties": false
        }),
    }
}

fn read_file_tool_spec() -> ToolSpec {
    ToolSpec {
        name: "read_file".to_string(),
        description: "Read a local text file with optional line range and truncation.".to_string(),
        input_schema: serde_json::json!({
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path to read. Relative paths resolve from the task cwd."
                },
                "start_line": {
                    "type": "integer",
                    "description": "1-based line number to start reading."
                },
                "end_line": {
                    "type": "integer",
                    "description": "1-based line number to stop reading."
                },
                "max_lines": {
                    "type": "integer",
                    "description": "Maximum lines to return."
                },
                "max_bytes": {
                    "type": "integer",
                    "description": "Maximum output characters to return."
                }
            },
            "required": ["path"],
            "additionalProperties": false
        }),
    }
}

fn search_files_tool_spec() -> ToolSpec {
    ToolSpec {
        name: "search_files".to_string(),
        description: "Search files under a path using ripgrep when available.".to_string(),
        input_schema: serde_json::json!({
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query. Treated as a ripgrep regex when ripgrep is available."
                },
                "path": {
                    "type": "string",
                    "description": "Root path to search. Defaults to the task cwd."
                },
                "glob": {
                    "description": "Optional glob or list of globs, such as *.rs.",
                    "anyOf": [
                        { "type": "string" },
                        { "type": "array", "items": { "type": "string" } }
                    ]
                },
                "context_lines": {
                    "type": "integer",
                    "description": "Number of context lines to include around matches."
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of matches to return."
                }
            },
            "required": ["query"],
            "additionalProperties": false
        }),
    }
}

fn list_files_tool_spec() -> ToolSpec {
    ToolSpec {
        name: "list_files".to_string(),
        description: "List or fuzzy-filter files under a path while respecting ignore files."
            .to_string(),
        input_schema: serde_json::json!({
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Root path to list. Defaults to the task cwd."
                },
                "query": {
                    "type": "string",
                    "description": "Optional path substring or fuzzy query."
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of paths to return."
                },
                "include_hidden": {
                    "type": "boolean",
                    "description": "Whether to include hidden files."
                },
                "include_dirs": {
                    "type": "boolean",
                    "description": "Whether to include directories as well as files."
                }
            },
            "additionalProperties": false
        }),
    }
}

fn view_image_tool_spec() -> ToolSpec {
    ToolSpec {
        name: "view_image".to_string(),
        description: "Inspect a local image file and pass it back to the model when supported."
            .to_string(),
        input_schema: serde_json::json!({
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Image path to inspect. Relative paths resolve from the task cwd."
                },
                "detail": {
                    "type": "string",
                    "description": "Image detail hint: auto, low, or high."
                }
            },
            "required": ["path"],
            "additionalProperties": false
        }),
    }
}

fn python_tool_spec() -> ToolSpec {
    ToolSpec {
        name: "python".to_string(),
        description: concat!(
            "Run Python in the persistent browser session. Browser-harness helpers are already imported when available: ",
            "goto_url, page_info, js, fill_input, click_at_xy, type_text, press_key, scroll, wait_for_load, ",
            "wait_for_element, wait_for_network_idle, capture_screenshot, current_tab, list_tabs, switch_tab, ",
            "new_tab, cdp, drain_events, and http_get. Do not import Playwright, Selenium, or Pyppeteer. ",
            "Use copy_artifact and emit_image for files and screenshots."
        )
        .to_string(),
        input_schema: serde_json::json!({
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to run in the persistent browser namespace."
                }
            },
            "required": ["code"],
            "additionalProperties": false
        }),
    }
}

fn done_tool_spec() -> ToolSpec {
    ToolSpec {
        name: "done".to_string(),
        description: "Finish the browser task with a final user-facing result.".to_string(),
        input_schema: serde_json::json!({
            "type": "object",
            "properties": {
                "result": {
                    "type": "string",
                    "description": "Final answer for the user."
                }
            },
            "required": ["result"],
            "additionalProperties": false
        }),
    }
}

fn spawn_agent_tool_spec() -> ToolSpec {
    ToolSpec {
        name: "spawn_agent".to_string(),
        description: "Create a separate helper session for bounded background exploration."
            .to_string(),
        input_schema: serde_json::json!({
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The bounded task for the helper session."
                },
                "path": {
                    "type": "string",
                    "description": "Optional stable task path, such as flight-search. Relative paths are stored under /root/."
                },
                "nickname": {
                    "type": "string",
                    "description": "Optional short display name."
                },
                "role": {
                    "type": "string",
                    "description": "Optional helper role label."
                },
                "fork_mode": {
                    "type": "string",
                    "enum": ["summary", "none", "all", "last_n"],
                    "description": "How much parent context to provide. summary is sanitized and compact."
                },
                "fork_turns": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Number of recent user/follow-up turns to include when fork_mode is last_n."
                }
            },
            "required": ["message"],
            "additionalProperties": false
        }),
    }
}

fn wait_agent_tool_spec() -> ToolSpec {
    ToolSpec {
        name: "wait_agent".to_string(),
        description:
            "Read, and optionally briefly wait for, the compact status and final result for a helper session."
                .to_string(),
        input_schema: serde_json::json!({
            "type": "object",
            "properties": {
                "child_session_id": {
                    "type": "string",
                    "description": "The helper session id or canonical helper path returned by spawn_agent."
                },
                "timeout_ms": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Optional maximum time to wait for an active helper to finish before returning its current status."
                }
            },
            "required": ["child_session_id"],
            "additionalProperties": false
        }),
    }
}

fn send_message_tool_spec() -> ToolSpec {
    ToolSpec {
        name: "send_message".to_string(),
        description: "Queue a message for a helper session without waking a new turn.".to_string(),
        input_schema: serde_json::json!({
            "type": "object",
            "properties": {
                "child_session_id": {
                    "type": "string",
                    "description": "The helper session id or canonical helper path returned by spawn_agent."
                },
                "message": {
                    "type": "string",
                    "description": "The message to queue for the helper."
                }
            },
            "required": ["child_session_id", "message"],
            "additionalProperties": false
        }),
    }
}

fn followup_task_tool_spec() -> ToolSpec {
    ToolSpec {
        name: "followup_task".to_string(),
        description: "Queue a follow-up message for a helper session and wake its next turn."
            .to_string(),
        input_schema: serde_json::json!({
            "type": "object",
            "properties": {
                "child_session_id": {
                    "type": "string",
                    "description": "The helper session id or canonical helper path returned by spawn_agent."
                },
                "message": {
                    "type": "string",
                    "description": "The follow-up instruction for the helper."
                }
            },
            "required": ["child_session_id", "message"],
            "additionalProperties": false
        }),
    }
}

fn list_agents_tool_spec() -> ToolSpec {
    ToolSpec {
        name: "list_agents".to_string(),
        description: "List helper sessions spawned by this task.".to_string(),
        input_schema: serde_json::json!({
            "type": "object",
            "properties": {
                "path_prefix": {
                    "type": "string",
                    "description": "Optional canonical path prefix, such as /root/research."
                }
            },
            "additionalProperties": false
        }),
    }
}

fn close_agent_tool_spec() -> ToolSpec {
    ToolSpec {
        name: "close_agent".to_string(),
        description: "Cancel and close a helper session that is no longer needed.".to_string(),
        input_schema: serde_json::json!({
            "type": "object",
            "properties": {
                "child_session_id": {
                    "type": "string",
                    "description": "The helper session id or canonical helper path returned by spawn_agent."
                },
                "reason": {
                    "type": "string",
                    "description": "Short reason for closing the helper."
                }
            },
            "required": ["child_session_id"],
            "additionalProperties": false
        }),
    }
}
