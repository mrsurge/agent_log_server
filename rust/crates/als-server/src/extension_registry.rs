use anyhow::{Context, Result, anyhow};
use serde::Serialize;
use serde_json::{Map, Value};
use std::{
    fs,
    path::PathBuf,
    sync::{Arc, RwLock},
};

const EXTENSIONS_STATE_FILE: &str = "extensions_state.json";

#[derive(Clone, Debug)]
pub struct ExtensionRegistry {
    inner: Arc<RwLock<ExtensionRegistryInner>>,
}

#[derive(Debug)]
struct ExtensionRegistryInner {
    extension_roots: Vec<PathBuf>,
    config_dir: Option<PathBuf>,
    entries: Vec<ExtensionRegistryEntry>,
}

impl ExtensionRegistry {
    pub fn load_with_config(
        extension_roots: Vec<PathBuf>,
        config_dir: Option<PathBuf>,
    ) -> Result<Self> {
        let entries = discover_extensions(&extension_roots)?;
        let mut entries = entries;
        apply_enabled_overrides(&mut entries, config_dir.as_ref())?;
        Ok(Self::from_parts(extension_roots, config_dir, entries))
    }

    pub fn load_empty_with_config(
        extension_roots: Vec<PathBuf>,
        config_dir: Option<PathBuf>,
    ) -> Self {
        Self::from_parts(extension_roots, config_dir, Vec::new())
    }

    pub fn reload(&self) -> Result<Vec<ExtensionRegistryEntry>> {
        let extension_roots = self.extension_roots();
        let config_dir = self.config_dir();
        let mut entries = discover_extensions(&extension_roots)?;
        apply_enabled_overrides(&mut entries, config_dir.as_ref())?;
        let mut guard = self
            .inner
            .write()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        guard.entries = entries.clone();
        Ok(entries)
    }

    pub fn apply_runtime_extensions(&self, value: &Value) -> Vec<ExtensionRegistryEntry> {
        let Some(extensions) = value
            .as_object()
            .and_then(|object| object.get("extensions"))
            .and_then(Value::as_array)
        else {
            return self.list();
        };
        let mut guard = self
            .inner
            .write()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        for runtime in extensions {
            let Some(runtime_map) = runtime.as_object() else {
                continue;
            };
            let Some(extension_id) = string_field(runtime_map, "id") else {
                continue;
            };
            let Some(entry) = guard
                .entries
                .iter_mut()
                .find(|entry| entry.id == extension_id)
            else {
                continue;
            };
            apply_runtime_fields(entry, runtime_map);
        }
        guard.entries.clone()
    }

    pub fn list(&self) -> Vec<ExtensionRegistryEntry> {
        self.inner
            .read()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .entries
            .clone()
    }

    pub fn get(&self, extension_id: &str) -> Option<ExtensionRegistryEntry> {
        self.inner
            .read()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .entries
            .iter()
            .find(|entry| entry.id == extension_id)
            .cloned()
    }

    pub fn set_enabled(
        &self,
        extension_id: &str,
        enabled: bool,
    ) -> Result<Option<ExtensionRegistryEntry>> {
        if self.get(extension_id).is_none() {
            return Ok(None);
        }
        let config_dir = self.config_dir().ok_or_else(|| {
            anyhow!("ALS-RS extension enablement requires a configured config dir")
        })?;
        write_enabled_override(&config_dir, extension_id, enabled)?;
        let mut guard = self
            .inner
            .write()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let Some(entry) = guard
            .entries
            .iter_mut()
            .find(|entry| entry.id == extension_id)
        else {
            return Ok(None);
        };
        entry.enabled = enabled;
        entry.active = enabled && entry.dependency_ok;
        Ok(Some(entry.clone()))
    }

    pub fn enabled_overrides(&self) -> Map<String, Value> {
        self.inner
            .read()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .entries
            .iter()
            .map(|entry| (entry.id.clone(), Value::Bool(entry.enabled)))
            .collect()
    }

    fn from_parts(
        extension_roots: Vec<PathBuf>,
        config_dir: Option<PathBuf>,
        entries: Vec<ExtensionRegistryEntry>,
    ) -> Self {
        Self {
            inner: Arc::new(RwLock::new(ExtensionRegistryInner {
                extension_roots,
                config_dir,
                entries,
            })),
        }
    }

    fn extension_roots(&self) -> Vec<PathBuf> {
        self.inner
            .read()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .extension_roots
            .clone()
    }

    fn config_dir(&self) -> Option<PathBuf> {
        self.inner
            .read()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .config_dir
            .clone()
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct ExtensionRegistryEntry {
    pub id: String,
    pub name: String,
    #[serde(rename = "type")]
    pub extension_type: String,
    pub path: String,
    pub source_root: PathBuf,
    pub source_kind: String,
    pub enabled: bool,
    pub active: bool,
    pub version: String,
    pub dependency_ok: bool,
    pub dependency_status: String,
    pub dependency_message: String,
    pub dependency_details: Map<String, Value>,
    pub has_dependency_check: bool,
    pub has_dependency_install: bool,
    pub install_source: Map<String, Value>,
    pub installer_meta: Map<String, Value>,
    pub manifest: Map<String, Value>,
    pub capabilities: Map<String, Value>,
    pub ui: Map<String, Value>,
}

fn discover_extensions(extension_roots: &[PathBuf]) -> Result<Vec<ExtensionRegistryEntry>> {
    let mut merged: Vec<ExtensionRegistryEntry> = Vec::new();
    for (index, root) in extension_roots.iter().enumerate() {
        let source_kind = if index == 0 { "builtin" } else { "user" };
        for entry in discover_extensions_in_root(root, source_kind)? {
            if let Some(position) = merged.iter().position(|existing| existing.id == entry.id) {
                merged.remove(position);
            }
            merged.push(entry);
        }
    }
    Ok(merged)
}

fn discover_extensions_in_root(
    extensions_dir: &PathBuf,
    source_kind: &str,
) -> Result<Vec<ExtensionRegistryEntry>> {
    if !extensions_dir.is_dir() {
        return Ok(Vec::new());
    }

    let explicit = extensions_dir.join("extensions.json");
    if explicit.is_file() {
        return discover_from_registry_file(extensions_dir, explicit, source_kind);
    }

    let mut entries = Vec::new();
    for entry in fs::read_dir(extensions_dir)
        .with_context(|| format!("failed to read extensions dir {}", extensions_dir.display()))?
    {
        let entry = entry?;
        if !entry.file_type()?.is_dir() {
            continue;
        }
        let folder = entry.file_name().to_string_lossy().to_string();
        if folder.starts_with('_') || folder.starts_with('.') {
            continue;
        }
        let manifest = read_manifest(extensions_dir, &folder)?;
        if manifest.is_empty() {
            continue;
        }
        entries.push(entry_from_parts(
            extensions_dir,
            source_kind,
            &folder,
            Map::new(),
            manifest,
        )?);
    }
    entries.sort_by(|left, right| left.id.cmp(&right.id));
    Ok(entries)
}

fn discover_from_registry_file(
    extensions_dir: &PathBuf,
    registry_path: PathBuf,
    source_kind: &str,
) -> Result<Vec<ExtensionRegistryEntry>> {
    let data = read_json_object(&registry_path)?;
    let mut entries = Vec::new();
    let registry_entries = data
        .get("extensions")
        .and_then(Value::as_array)
        .ok_or_else(|| anyhow!("extensions.json must contain an extensions array"))?;
    for raw_entry in registry_entries {
        let registry_entry = object_or_empty(raw_entry);
        let folder = string_field(&registry_entry, "path")
            .or_else(|| string_field(&registry_entry, "id"))
            .unwrap_or_default();
        if folder.is_empty() {
            continue;
        }
        let manifest = read_manifest(extensions_dir, &folder)?;
        entries.push(entry_from_parts(
            extensions_dir,
            source_kind,
            &folder,
            registry_entry,
            manifest,
        )?);
    }
    Ok(entries)
}

fn entry_from_parts(
    extensions_dir: &PathBuf,
    source_kind: &str,
    folder: &str,
    registry_entry: Map<String, Value>,
    manifest: Map<String, Value>,
) -> Result<ExtensionRegistryEntry> {
    let id = string_field(&registry_entry, "id")
        .or_else(|| string_field(&manifest, "id"))
        .unwrap_or_else(|| folder.to_owned());
    let name = string_field(&registry_entry, "name")
        .or_else(|| string_field(&manifest, "name"))
        .unwrap_or_else(|| id.clone());
    let extension_type = string_field(&registry_entry, "type")
        .or_else(|| string_field(&manifest, "type"))
        .unwrap_or_else(|| folder.to_owned());
    let enabled = registry_entry
        .get("enabled")
        .or_else(|| manifest.get("enabled"))
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let version = string_field(&manifest, "version").unwrap_or_default();
    let manifest_ok = !version.is_empty();
    let dependency_ok = manifest_ok;
    let dependency_status = if manifest_ok { "unchecked" } else { "error" }.to_owned();
    let dependency_message = if manifest_ok {
        String::new()
    } else {
        format!("Extension manifest.version is required for {id}")
    };
    let capabilities = object_or_empty(manifest.get("capabilities").unwrap_or(&Value::Null));
    let ui = object_or_empty(manifest.get("ui").unwrap_or(&Value::Null));
    let dependencies = object_or_empty(manifest.get("dependencies").unwrap_or(&Value::Null));
    let install_source =
        object_or_empty(registry_entry.get("install_source").unwrap_or(&Value::Null));
    let installer_meta =
        object_or_empty(registry_entry.get("installer_meta").unwrap_or(&Value::Null));
    let has_dependency_check = dependencies
        .get("has_check")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let has_dependency_install = dependencies
        .get("has_install")
        .and_then(Value::as_bool)
        .unwrap_or(false);

    Ok(ExtensionRegistryEntry {
        id,
        name,
        extension_type,
        path: folder.to_owned(),
        source_root: extensions_dir.clone(),
        source_kind: source_kind.to_owned(),
        enabled,
        active: enabled && dependency_ok,
        version,
        dependency_ok,
        dependency_status,
        dependency_message,
        dependency_details: Map::new(),
        has_dependency_check,
        has_dependency_install,
        install_source,
        installer_meta,
        manifest,
        capabilities,
        ui,
    })
}

fn apply_runtime_fields(entry: &mut ExtensionRegistryEntry, runtime: &Map<String, Value>) {
    if let Some(enabled) = runtime.get("enabled").and_then(Value::as_bool) {
        entry.enabled = enabled;
    }
    if let Some(dependency_ok) = runtime.get("dependency_ok").and_then(Value::as_bool) {
        entry.dependency_ok = dependency_ok;
    }
    if let Some(status) = string_field(runtime, "dependency_status") {
        entry.dependency_status = status;
    }
    if let Some(message) = runtime.get("dependency_message").and_then(Value::as_str) {
        entry.dependency_message = message.to_owned();
    }
    if let Some(details) = runtime.get("dependency_details").and_then(Value::as_object) {
        entry.dependency_details = details.clone();
    }
    if let Some(has_check) = runtime.get("has_dependency_check").and_then(Value::as_bool) {
        entry.has_dependency_check = has_check;
    }
    if let Some(has_install) = runtime
        .get("has_dependency_install")
        .and_then(Value::as_bool)
    {
        entry.has_dependency_install = has_install;
    }
    entry.active = entry.enabled && entry.dependency_ok;
}

fn read_manifest(extensions_dir: &PathBuf, folder: &str) -> Result<Map<String, Value>> {
    let manifest_path = extensions_dir.join(folder).join("manifest.json");
    if !manifest_path.is_file() {
        return Ok(Map::new());
    }
    read_json_object(&manifest_path)
}

fn read_json_object(path: &PathBuf) -> Result<Map<String, Value>> {
    let raw = fs::read_to_string(path)
        .with_context(|| format!("failed to read JSON file {}", path.display()))?;
    let value: Value = serde_json::from_str(&raw)
        .with_context(|| format!("failed to parse JSON file {}", path.display()))?;
    Ok(object_or_empty(&value))
}

fn apply_enabled_overrides(
    entries: &mut [ExtensionRegistryEntry],
    config_dir: Option<&PathBuf>,
) -> Result<()> {
    let Some(config_dir) = config_dir else {
        return Ok(());
    };
    let state_path = config_dir.join(EXTENSIONS_STATE_FILE);
    if !state_path.is_file() {
        return Ok(());
    }
    let state = read_json_object(&state_path)?;
    let Some(extensions) = state.get("extensions").and_then(Value::as_object) else {
        return Ok(());
    };
    for entry in entries {
        let Some(enabled) = extensions
            .get(&entry.id)
            .and_then(Value::as_object)
            .and_then(|item| item.get("enabled"))
            .and_then(Value::as_bool)
        else {
            continue;
        };
        entry.enabled = enabled;
        entry.active = enabled && entry.dependency_ok;
    }
    Ok(())
}

fn write_enabled_override(config_dir: &PathBuf, extension_id: &str, enabled: bool) -> Result<()> {
    fs::create_dir_all(config_dir).with_context(|| {
        format!(
            "failed to create ALS-RS config dir {}",
            config_dir.display()
        )
    })?;
    let state_path = config_dir.join(EXTENSIONS_STATE_FILE);
    let mut state = if state_path.is_file() {
        read_json_object(&state_path)?
    } else {
        Map::new()
    };
    let extensions_value = state
        .entry("extensions".to_owned())
        .or_insert_with(|| Value::Object(Map::new()));
    if !extensions_value.is_object() {
        *extensions_value = Value::Object(Map::new());
    }
    let extensions = extensions_value
        .as_object_mut()
        .expect("extensions state should be an object after normalization");
    let mut extension_state = object_or_empty(extensions.get(extension_id).unwrap_or(&Value::Null));
    extension_state.insert("enabled".to_owned(), Value::Bool(enabled));
    extensions.insert(extension_id.to_owned(), Value::Object(extension_state));

    let raw = serde_json::to_string_pretty(&Value::Object(state))?;
    fs::write(&state_path, format!("{raw}\n"))
        .with_context(|| format!("failed to write extension state {}", state_path.display()))?;
    Ok(())
}

fn object_or_empty(value: &Value) -> Map<String, Value> {
    value.as_object().cloned().unwrap_or_default()
}

fn string_field(map: &Map<String, Value>, key: &str) -> Option<String> {
    map.get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        fs,
        time::{SystemTime, UNIX_EPOCH},
    };

    #[test]
    fn discovers_extensions_from_registry_file() {
        let root = std::env::temp_dir().join(format!("als-rs-ext-reg-{}", unix_millis()));
        let ext_dir = root.join("extensions");
        fs::create_dir_all(ext_dir.join("copilot_sdk")).unwrap();
        fs::write(
            ext_dir.join("extensions.json"),
            r#"{"version":"1.0","extensions":[{"id":"copilot-sdk","name":"GitHub Copilot","type":"copilot_sdk","path":"copilot_sdk","enabled":true}]}"#,
        )
        .unwrap();
        fs::write(
            ext_dir.join("copilot_sdk").join("manifest.json"),
            r#"{"id":"copilot-sdk","name":"GitHub Copilot","version":"1.0.0","type":"copilot_sdk","capabilities":{"modelListing":true}}"#,
        )
        .unwrap();

        let registry = ExtensionRegistry::load_with_config(vec![ext_dir], None).unwrap();
        let entries = registry.list();
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].id, "copilot-sdk");
        assert_eq!(entries[0].extension_type, "copilot_sdk");
        assert!(entries[0].active);
        assert_eq!(
            entries[0]
                .capabilities
                .get("modelListing")
                .and_then(Value::as_bool),
            Some(true)
        );

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn reload_refreshes_registry_entries_from_disk() {
        let root = std::env::temp_dir().join(format!("als-rs-ext-reg-reload-{}", unix_millis()));
        let ext_dir = root.join("extensions");
        fs::create_dir_all(ext_dir.join("copilot_sdk")).unwrap();
        fs::create_dir_all(ext_dir.join("codex_ext")).unwrap();
        fs::write(
            ext_dir.join("extensions.json"),
            r#"{"version":"1.0","extensions":[{"id":"copilot-sdk","name":"GitHub Copilot","type":"copilot_sdk","path":"copilot_sdk","enabled":true}]}"#,
        )
        .unwrap();
        fs::write(
            ext_dir.join("copilot_sdk").join("manifest.json"),
            r#"{"id":"copilot-sdk","name":"GitHub Copilot","version":"1.0.0","type":"copilot_sdk"}"#,
        )
        .unwrap();
        fs::write(
            ext_dir.join("codex_ext").join("manifest.json"),
            r#"{"id":"codex-ext","name":"Codex","version":"1.0.0","type":"codex_ext"}"#,
        )
        .unwrap();

        let registry = ExtensionRegistry::load_with_config(vec![ext_dir.clone()], None).unwrap();
        assert_eq!(registry.list().len(), 1);
        fs::write(
            ext_dir.join("extensions.json"),
            r#"{"version":"1.0","extensions":[{"id":"copilot-sdk","name":"GitHub Copilot","type":"copilot_sdk","path":"copilot_sdk","enabled":true},{"id":"codex-ext","name":"Codex","type":"codex_ext","path":"codex_ext","enabled":true}]}"#,
        )
        .unwrap();

        let entries = registry.reload().unwrap();
        assert_eq!(entries.len(), 2);
        assert!(registry.get("codex-ext").is_some());

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn user_root_overrides_builtin_root_and_reload_sees_new_user_entries() {
        let root = std::env::temp_dir().join(format!("als-rs-ext-reg-roots-{}", unix_millis()));
        let builtin_dir = root.join("builtin");
        let user_dir = root.join("user");
        fs::create_dir_all(builtin_dir.join("shared_ext")).unwrap();
        fs::create_dir_all(user_dir.join("shared_ext")).unwrap();
        fs::write(
            builtin_dir.join("shared_ext").join("manifest.json"),
            r#"{"id":"shared","name":"Builtin Shared","version":"1.0.0","type":"builtin_type"}"#,
        )
        .unwrap();
        fs::write(
            user_dir.join("shared_ext").join("manifest.json"),
            r#"{"id":"shared","name":"User Shared","version":"2.0.0","type":"user_type"}"#,
        )
        .unwrap();

        let registry =
            ExtensionRegistry::load_with_config(vec![builtin_dir.clone(), user_dir.clone()], None)
                .unwrap();
        let shared = registry.get("shared").unwrap();
        assert_eq!(shared.name, "User Shared");
        assert_eq!(shared.extension_type, "user_type");
        assert_eq!(shared.source_kind, "user");
        assert_eq!(shared.source_root, user_dir);

        fs::create_dir_all(builtin_dir.join("builtin_only")).unwrap();
        fs::write(
            builtin_dir.join("builtin_only").join("manifest.json"),
            r#"{"id":"builtin-only","name":"Builtin Only","version":"1.0.0","type":"builtin_type"}"#,
        )
        .unwrap();
        let entries = registry.reload().unwrap();
        assert!(entries.iter().any(|entry| entry.id == "builtin-only"));

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn set_enabled_persists_overlay_and_updates_active_state() {
        let root = std::env::temp_dir().join(format!("als-rs-ext-reg-enabled-{}", unix_millis()));
        let ext_dir = root.join("extensions");
        let config_dir = root.join("config");
        fs::create_dir_all(ext_dir.join("copilot_sdk")).unwrap();
        fs::write(
            ext_dir.join("extensions.json"),
            r#"{"version":"1.0","extensions":[{"id":"copilot-sdk","name":"GitHub Copilot","type":"copilot_sdk","path":"copilot_sdk","enabled":true}]}"#,
        )
        .unwrap();
        fs::write(
            ext_dir.join("copilot_sdk").join("manifest.json"),
            r#"{"id":"copilot-sdk","name":"GitHub Copilot","version":"1.0.0","type":"copilot_sdk"}"#,
        )
        .unwrap();

        let registry =
            ExtensionRegistry::load_with_config(vec![ext_dir.clone()], Some(config_dir.clone()))
                .unwrap();
        let updated = registry.set_enabled("copilot-sdk", false).unwrap().unwrap();
        assert!(!updated.enabled);
        assert!(!updated.active);
        assert!(!registry.get("copilot-sdk").unwrap().active);

        let reloaded =
            ExtensionRegistry::load_with_config(vec![ext_dir], Some(config_dir)).unwrap();
        let entry = reloaded.get("copilot-sdk").unwrap();
        assert!(!entry.enabled);
        assert!(!entry.active);

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn runtime_dependency_fields_update_active_state() {
        let root = std::env::temp_dir().join(format!("als-rs-ext-reg-runtime-{}", unix_millis()));
        let ext_dir = root.join("extensions");
        fs::create_dir_all(ext_dir.join("copilot_sdk")).unwrap();
        fs::write(
            ext_dir.join("extensions.json"),
            r#"{"version":"1.0","extensions":[{"id":"copilot-sdk","name":"GitHub Copilot","type":"copilot_sdk","path":"copilot_sdk","enabled":true}]}"#,
        )
        .unwrap();
        fs::write(
            ext_dir.join("copilot_sdk").join("manifest.json"),
            r#"{"id":"copilot-sdk","name":"GitHub Copilot","version":"1.0.0","type":"copilot_sdk","dependencies":{"has_check":true,"has_install":true}}"#,
        )
        .unwrap();

        let registry = ExtensionRegistry::load_with_config(vec![ext_dir], None).unwrap();
        assert!(registry.get("copilot-sdk").unwrap().active);
        registry.apply_runtime_extensions(&serde_json::json!({
            "extensions": [{
                "id": "copilot-sdk",
                "dependency_ok": false,
                "dependency_status": "unmet",
                "dependency_message": "copilot missing",
                "dependency_details": {"binary": null},
                "has_dependency_check": true,
                "has_dependency_install": true
            }]
        }));
        let entry = registry.get("copilot-sdk").unwrap();
        assert!(!entry.dependency_ok);
        assert!(!entry.active);
        assert_eq!(entry.dependency_status, "unmet");
        assert_eq!(entry.dependency_message, "copilot missing");
        assert!(entry.has_dependency_check);
        assert!(entry.has_dependency_install);

        let _ = fs::remove_dir_all(root);
    }

    fn unix_millis() -> u128 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock should be after Unix epoch")
            .as_millis()
    }
}
