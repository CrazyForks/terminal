use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum PaletteAction {
    NewTask,
    OpenBrowser,
    ReconnectBrowser,
    PreviousWork,
    ChooseModel,
    SignIn,
    ConfigureLaminar,
}

#[derive(Clone, Copy, Debug)]
pub(crate) struct PaletteItem {
    pub(crate) label: &'static str,
    pub(crate) action: PaletteAction,
}

const ITEMS: [PaletteItem; 7] = [
    PaletteItem {
        label: "New task",
        action: PaletteAction::NewTask,
    },
    PaletteItem {
        label: "Open browser",
        action: PaletteAction::OpenBrowser,
    },
    PaletteItem {
        label: "Reconnect browser",
        action: PaletteAction::ReconnectBrowser,
    },
    PaletteItem {
        label: "Previous work",
        action: PaletteAction::PreviousWork,
    },
    PaletteItem {
        label: "Choose model",
        action: PaletteAction::ChooseModel,
    },
    PaletteItem {
        label: "Sign in",
        action: PaletteAction::SignIn,
    },
    PaletteItem {
        label: "Configure Laminar",
        action: PaletteAction::ConfigureLaminar,
    },
];

#[derive(Debug, Default)]
pub(crate) struct Palette {
    filter: String,
}

impl Palette {
    pub(crate) fn clear(&mut self) {
        self.filter.clear();
    }

    pub(crate) fn filter(&self) -> &str {
        &self.filter
    }

    pub(crate) fn push_filter_str(&mut self, text: &str) {
        self.filter
            .extend(text.chars().filter(|ch| !ch.is_control()));
    }

    pub(crate) fn items(&self) -> Vec<PaletteItem> {
        let filter = self.filter.trim().to_ascii_lowercase();
        if filter.is_empty() {
            return ITEMS.to_vec();
        }
        ITEMS
            .iter()
            .copied()
            .filter(|item| item.label.to_ascii_lowercase().contains(&filter))
            .collect()
    }

    pub(crate) fn selected_action(&self, selected_row: usize) -> Option<PaletteAction> {
        self.items().get(selected_row).map(|item| item.action)
    }

    pub(crate) fn handle_filter_key(&mut self, key: KeyEvent) -> bool {
        match key {
            KeyEvent {
                code: KeyCode::Backspace,
                ..
            } => {
                self.filter.pop();
                true
            }
            KeyEvent {
                code: KeyCode::Char(ch),
                modifiers,
                ..
            } if modifiers.is_empty() || modifiers == KeyModifiers::SHIFT => {
                self.filter.push(ch);
                true
            }
            _ => false,
        }
    }
}
