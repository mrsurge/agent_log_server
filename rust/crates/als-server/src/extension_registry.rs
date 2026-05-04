use anyhow::{Context, Result, anyhow};
use serde::Serialize;
use serde_json::{Map, Value};
use std::{fs, path::PathBuf};

#[derive(Clone, Debug)]
pub struct ExtensionRegistry {
    entries: Vec<ExtensionRegistryEntry>,
}

impl ExtensionRegistry {
    pub fn load(extensions_dir: PathBuf) -> Result<Self> {
        let entries = discover_extensions(&extensions_dir)?;
        Ok(Self { entries })
    }

    pub fn load_empty(_extensions_dir: PathBuf) -> Self {
        Self {
            entries: Vec::new(),
        }
    }

    pub fn list(&self) -> Vec<ExtensionRegistryEntry> {
        self.entries.clone()
    }

    pub fn get(&self, extension_id: &str) -> Option<ExtensionRegistryEntry> {
        self.entries
            .iter()
            .find(|entry| entry.id == extension_id)
            .cloned()
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct ExtensionRegistryEntry {
    pub id: String,
    pub name: String,
    #[serde(rename = "type")]
    pub extension_type: String,
    pub path: String,
    pub enabled: bool,
    pub active: bool,
    pub version: String,
    pub dependency_ok: bool,
    pub dependency_status: String,
    pub dependency_message: String,
    pub manifest: Map<String, Value>,
    pub capabilities: Map<String, Value>,
    pub ui: Map<String, Value>,
}

fn discover_extensions(extensions_dir: &PathBuf) -> Result<Vec<ExtensionRegistryEntry>> {
    if !extensions_dir.is_dir() {
        return Ok(Vec::new());
    }

    let explicit = extensions_dir.join("extensions.json");
    if explicit.is_file() {
        return discover_from_registry_file(extensions_dir, explicit);
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
            &folder,
            registry_entry,
            manifest,
        )?);
    }
    Ok(entries)
}

fn entry_from_parts(
    _extensions_dir: &PathBuf,
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

    Ok(ExtensionRegistryEntry {
        id,
        name,
        extension_type,
        path: folder.to_owned(),
        enabled,
        active: enabled && dependency_ok,
        version,
        dependency_ok,
        dependency_status,
        dependency_message,
        manifest,
        capabilities,
        ui,
    })
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

        let registry = ExtensionRegistry::load(ext_dir).unwrap();
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

    fn unix_millis() -> u128 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock should be after Unix epoch")
            .as_millis()
    }
}
