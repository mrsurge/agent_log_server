use anyhow::{Context, Result, anyhow, bail};
use std::{
    fs,
    path::{Component, Path, PathBuf},
};

#[derive(Clone, Debug)]
struct FilePatch {
    path: Option<String>,
    hunks: Vec<Hunk>,
}

#[derive(Clone, Debug)]
struct Hunk {
    modified_start: u64,
    lines: Vec<DiffLine>,
}

#[derive(Clone, Debug)]
enum DiffLine {
    Context(String),
    Removed(String),
    Added(String),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum LineEnding {
    Lf,
    Crlf,
}

pub fn apply_reverse_patch(
    repo_root: &Path,
    path_hint: Option<&str>,
    diff_text: &str,
) -> Result<()> {
    let patches = parse_unified_diff(diff_text)?;
    let target_rel = target_rel_path(repo_root, path_hint, &patches)?;
    let mut hunks = Vec::new();
    let mut saw_other_file = false;

    for patch in patches {
        match patch.path.as_deref() {
            Some(path) => {
                let patch_rel = normalize_rel_path(repo_root, path)?;
                if patch_rel == target_rel {
                    hunks.extend(patch.hunks);
                } else if !patch.hunks.is_empty() {
                    saw_other_file = true;
                }
            }
            None => hunks.extend(patch.hunks),
        }
    }

    if saw_other_file {
        bail!("tracked diff spans multiple files; refusing programmatic reverse apply");
    }
    if hunks.is_empty() {
        bail!(
            "tracked diff contains no hunks for {}",
            target_rel.display()
        );
    }

    let target_path = repo_root.join(&target_rel);
    let content = fs::read_to_string(&target_path)
        .with_context(|| format!("failed to read {}", target_path.display()))?;
    let (mut lines, trailing_newline, line_ending) = split_text_lines(&content);

    let mut indexed_hunks: Vec<(usize, Hunk)> = hunks.into_iter().enumerate().collect();
    indexed_hunks.sort_by_key(|(index, hunk)| (hunk.modified_start, *index));
    for (_index, hunk) in indexed_hunks.into_iter().rev() {
        reverse_hunk(&mut lines, &hunk)
            .with_context(|| format!("failed to reverse hunk at +{}", hunk.modified_start))?;
    }

    let next_content = join_text_lines(&lines, trailing_newline, line_ending);
    fs::write(&target_path, next_content)
        .with_context(|| format!("failed to write {}", target_path.display()))?;
    Ok(())
}

fn parse_unified_diff(diff_text: &str) -> Result<Vec<FilePatch>> {
    let mut patches = Vec::new();
    let mut current = FilePatch {
        path: None,
        hunks: Vec::new(),
    };
    let mut current_hunk: Option<Hunk> = None;

    for line in diff_text.lines() {
        if let Some(path) = diff_git_new_path(line) {
            flush_hunk(&mut current, &mut current_hunk);
            flush_patch(&mut patches, &mut current);
            current.path = Some(path);
            continue;
        }

        if let Some(path) = plus_file_path(line) {
            current.path = Some(path);
            continue;
        }

        if line.starts_with("@@") {
            flush_hunk(&mut current, &mut current_hunk);
            let (_original_start, _original_count, modified_start, _modified_count) =
                parse_hunk_header(line).ok_or_else(|| anyhow!("invalid hunk header: {line}"))?;
            current_hunk = Some(Hunk {
                modified_start,
                lines: Vec::new(),
            });
            continue;
        }

        let Some(hunk) = current_hunk.as_mut() else {
            continue;
        };
        if line == "\\ No newline at end of file" {
            continue;
        }
        if let Some(text) = line.strip_prefix(' ') {
            hunk.lines.push(DiffLine::Context(text.to_owned()));
        } else if let Some(text) = line.strip_prefix('-') {
            hunk.lines.push(DiffLine::Removed(text.to_owned()));
        } else if let Some(text) = line.strip_prefix('+') {
            hunk.lines.push(DiffLine::Added(text.to_owned()));
        } else {
            bail!("unsupported diff line inside hunk: {line}");
        }
    }

    flush_hunk(&mut current, &mut current_hunk);
    flush_patch(&mut patches, &mut current);
    if patches.iter().all(|patch| patch.hunks.is_empty()) {
        bail!("tracked diff contains no unified hunks");
    }
    Ok(patches)
}

fn flush_hunk(current: &mut FilePatch, current_hunk: &mut Option<Hunk>) {
    if let Some(hunk) = current_hunk.take() {
        current.hunks.push(hunk);
    }
}

fn flush_patch(patches: &mut Vec<FilePatch>, current: &mut FilePatch) {
    if current.path.is_some() || !current.hunks.is_empty() {
        patches.push(FilePatch {
            path: current.path.take(),
            hunks: std::mem::take(&mut current.hunks),
        });
    }
}

fn reverse_hunk(lines: &mut Vec<String>, hunk: &Hunk) -> Result<()> {
    let start = hunk_start_index(hunk.modified_start)?;
    let expected = modified_side_lines(hunk);
    if start > lines.len() {
        bail!(
            "hunk starts at line {}, but file has {} lines",
            hunk.modified_start,
            lines.len()
        );
    }
    if start + expected.len() > lines.len() {
        bail!(
            "hunk expects {} line{} at line {}, but file has {} line{}",
            expected.len(),
            plural(expected.len()),
            hunk.modified_start,
            lines.len(),
            plural(lines.len())
        );
    }

    let actual = &lines[start..start + expected.len()];
    for (offset, (actual_line, expected_line)) in actual.iter().zip(expected.iter()).enumerate() {
        if !line_matches_whitespace_tolerant(actual_line, expected_line) {
            bail!(
                "line {} mismatch: expected {:?}, found {:?}",
                start + offset + 1,
                expected_line,
                actual_line
            );
        }
    }

    let replacement = original_side_lines_preserving_context(hunk, actual);
    lines.splice(start..start + expected.len(), replacement);
    Ok(())
}

fn modified_side_lines(hunk: &Hunk) -> Vec<String> {
    hunk.lines
        .iter()
        .filter_map(|line| match line {
            DiffLine::Context(text) | DiffLine::Added(text) => Some(text.clone()),
            DiffLine::Removed(_) => None,
        })
        .collect()
}

fn original_side_lines_preserving_context(
    hunk: &Hunk,
    actual_modified_side: &[String],
) -> Vec<String> {
    let mut replacement = Vec::new();
    let mut modified_index = 0usize;
    for line in &hunk.lines {
        match line {
            DiffLine::Context(_) => {
                if let Some(actual) = actual_modified_side.get(modified_index) {
                    replacement.push(actual.clone());
                }
                modified_index += 1;
            }
            DiffLine::Added(_) => {
                modified_index += 1;
            }
            DiffLine::Removed(text) => replacement.push(text.clone()),
        }
    }
    replacement
}

fn line_matches_whitespace_tolerant(actual: &str, expected: &str) -> bool {
    actual == expected || normalize_whitespace(actual) == normalize_whitespace(expected)
}

fn normalize_whitespace(value: &str) -> String {
    value.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn hunk_start_index(start_line: u64) -> Result<usize> {
    if start_line == 0 {
        Ok(0)
    } else {
        usize::try_from(start_line - 1).map_err(|_| anyhow!("hunk line is too large"))
    }
}

fn target_rel_path(
    repo_root: &Path,
    path_hint: Option<&str>,
    patches: &[FilePatch],
) -> Result<PathBuf> {
    if let Some(path_hint) = path_hint {
        return normalize_rel_path(repo_root, path_hint);
    }
    let mut patch_paths = patches
        .iter()
        .filter_map(|patch| patch.path.as_deref())
        .map(|path| normalize_rel_path(repo_root, path))
        .collect::<Result<Vec<_>>>()?;
    patch_paths.sort();
    patch_paths.dedup();
    match patch_paths.len() {
        0 => bail!("tracked diff has no file path"),
        1 => Ok(patch_paths.remove(0)),
        _ => bail!("tracked diff spans multiple files and has no explicit target path"),
    }
}

fn normalize_rel_path(repo_root: &Path, raw_path: &str) -> Result<PathBuf> {
    let path = raw_path.trim();
    if path.is_empty() {
        bail!("empty patch path");
    }
    let path = path
        .strip_prefix("file://")
        .unwrap_or(path)
        .trim_start_matches("./");
    let path = path
        .strip_prefix("a/")
        .or_else(|| path.strip_prefix("b/"))
        .unwrap_or(path);
    let path_buf = PathBuf::from(path);
    let rel = if path_buf.is_absolute() {
        path_buf
            .strip_prefix(repo_root)
            .with_context(|| {
                format!(
                    "patch path {} is outside repo root {}",
                    path_buf.display(),
                    repo_root.display()
                )
            })?
            .to_path_buf()
    } else {
        path_buf
    };
    reject_unsafe_rel_path(&rel)?;
    Ok(rel)
}

fn reject_unsafe_rel_path(path: &Path) -> Result<()> {
    for component in path.components() {
        match component {
            Component::Normal(_) | Component::CurDir => {}
            Component::ParentDir => {
                bail!("patch path contains parent traversal: {}", path.display())
            }
            Component::RootDir | Component::Prefix(_) => {
                bail!("patch path is not relative: {}", path.display())
            }
        }
    }
    Ok(())
}

fn diff_git_new_path(line: &str) -> Option<String> {
    let rest = line.strip_prefix("diff --git ")?;
    let mut parts = rest.split_whitespace();
    let _old = parts.next();
    parts
        .next()
        .and_then(|value| value.strip_prefix("b/").or(Some(value)))
        .map(str::trim)
        .filter(|value| !value.is_empty() && *value != "/dev/null")
        .map(ToOwned::to_owned)
}

fn plus_file_path(line: &str) -> Option<String> {
    let path = line.strip_prefix("+++ ")?;
    let path = path
        .trim()
        .strip_prefix("b/")
        .unwrap_or_else(|| path.trim());
    (!path.is_empty() && path != "/dev/null").then(|| path.to_owned())
}

fn parse_hunk_header(line: &str) -> Option<(u64, u64, u64, u64)> {
    let mut original = None;
    let mut modified = None;
    for part in line.split_whitespace() {
        if let Some(rest) = part.strip_prefix('-') {
            original = parse_range_part(rest);
        } else if let Some(rest) = part.strip_prefix('+') {
            modified = parse_range_part(rest);
        }
        if original.is_some() && modified.is_some() {
            break;
        }
    }
    let (original_start, original_count) = original?;
    let (modified_start, modified_count) = modified?;
    Some((
        original_start,
        original_count,
        modified_start,
        modified_count,
    ))
}

fn parse_range_part(value: &str) -> Option<(u64, u64)> {
    let mut parts = value.split(',');
    let start = parts.next()?.parse::<u64>().ok()?;
    let count = parts
        .next()
        .and_then(|value| value.parse::<u64>().ok())
        .unwrap_or(1);
    Some((start, count))
}

fn split_text_lines(text: &str) -> (Vec<String>, bool, LineEnding) {
    let line_ending = if text.contains("\r\n") {
        LineEnding::Crlf
    } else {
        LineEnding::Lf
    };
    let trailing_newline = text.ends_with('\n');
    let mut parts: Vec<&str> = text.split('\n').collect();
    if trailing_newline {
        parts.pop();
    }
    let lines = parts
        .into_iter()
        .map(|line| line.strip_suffix('\r').unwrap_or(line).to_owned())
        .collect();
    (lines, trailing_newline, line_ending)
}

fn join_text_lines(lines: &[String], trailing_newline: bool, line_ending: LineEnding) -> String {
    let separator = match line_ending {
        LineEnding::Lf => "\n",
        LineEnding::Crlf => "\r\n",
    };
    let mut out = lines.join(separator);
    if trailing_newline {
        out.push_str(separator);
    }
    out
}

fn plural(count: usize) -> &'static str {
    if count == 1 { "" } else { "s" }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        fs,
        time::{SystemTime, UNIX_EPOCH},
    };

    #[test]
    fn reverses_added_line_from_hunk_only_diff() {
        let root = temp_repo("reverse_added_line_from_hunk_only_diff");
        write_file(&root, "notes.md", "alpha\n<!-- canary -->\nomega\n");
        apply_reverse_patch(
            &root,
            Some("notes.md"),
            "@@ -1,2 +1,3 @@\n alpha\n+<!-- canary -->\n omega",
        )
        .unwrap();
        assert_eq!(read_file(&root, "notes.md"), "alpha\nomega\n");
    }

    #[test]
    fn reverses_modified_line() {
        let root = temp_repo("reverse_modified_line");
        write_file(&root, "src/lib.rs", "fn main() { new_value(); }\n");
        apply_reverse_patch(
            &root,
            Some("src/lib.rs"),
            "diff --git a/src/lib.rs b/src/lib.rs\n--- a/src/lib.rs\n+++ b/src/lib.rs\n@@ -1,1 +1,1 @@\n-fn main() { old_value(); }\n+fn main() { new_value(); }",
        )
        .unwrap();
        assert_eq!(
            read_file(&root, "src/lib.rs"),
            "fn main() { old_value(); }\n"
        );
    }

    #[test]
    fn reverses_deleted_line() {
        let root = temp_repo("reverse_deleted_line");
        write_file(&root, "notes.md", "alpha\nomega\n");
        apply_reverse_patch(
            &root,
            Some("notes.md"),
            "@@ -1,3 +1,2 @@\n alpha\n-deleted\n omega",
        )
        .unwrap();
        assert_eq!(read_file(&root, "notes.md"), "alpha\ndeleted\nomega\n");
    }

    #[test]
    fn matches_current_lines_with_whitespace_tolerance() {
        let root = temp_repo("matches_current_lines_with_whitespace_tolerance");
        write_file(&root, "notes.md", "alpha\n  <!-- canary -->   \nomega\n");
        apply_reverse_patch(
            &root,
            Some("notes.md"),
            "@@ -1,2 +1,3 @@\n alpha\n+<!-- canary -->\n omega",
        )
        .unwrap();
        assert_eq!(read_file(&root, "notes.md"), "alpha\nomega\n");
    }

    #[test]
    fn refuses_when_expected_line_is_missing() {
        let root = temp_repo("refuses_when_expected_line_is_missing");
        write_file(&root, "notes.md", "alpha\nsomething else\nomega\n");
        let error = apply_reverse_patch(
            &root,
            Some("notes.md"),
            "@@ -1,2 +1,3 @@\n alpha\n+<!-- canary -->\n omega",
        )
        .unwrap_err();
        assert!(
            format!("{error:#}").contains("line 2 mismatch"),
            "{error:#}"
        );
        assert_eq!(
            read_file(&root, "notes.md"),
            "alpha\nsomething else\nomega\n"
        );
    }

    #[test]
    fn preserves_crlf_line_endings() {
        let root = temp_repo("preserves_crlf_line_endings");
        write_file(&root, "notes.md", "alpha\r\ncanary\r\nomega\r\n");
        apply_reverse_patch(
            &root,
            Some("notes.md"),
            "@@ -1,2 +1,3 @@\n alpha\n+canary\n omega",
        )
        .unwrap();
        assert_eq!(read_file(&root, "notes.md"), "alpha\r\nomega\r\n");
    }

    fn temp_repo(name: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("als-rs-{name}-{nonce}"));
        fs::create_dir_all(&root).unwrap();
        root
    }

    fn write_file(root: &Path, rel: &str, text: &str) {
        let path = root.join(rel);
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).unwrap();
        }
        fs::write(path, text).unwrap();
    }

    fn read_file(root: &Path, rel: &str) -> String {
        fs::read_to_string(root.join(rel)).unwrap()
    }
}
