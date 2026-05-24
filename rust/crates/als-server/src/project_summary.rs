use anyhow::{Context, Result};
use git2::{DiffOptions, Patch, Repository, Status, StatusOptions};
use serde::Serialize;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
};

const DEFAULT_MAX_DIFF_BYTES: u64 = 15 * 1024;
const MAX_CHANGED_FILES: usize = 500;

#[derive(Debug, Serialize)]
struct ProjectSummary {
    ok: bool,
    root: String,
    branch: Option<String>,
    head: Option<String>,
    head_short: Option<String>,
    dirty: bool,
    changed_files: usize,
    additions: usize,
    deletions: usize,
    max_diff_bytes: u64,
    files: Vec<ProjectFileSummary>,
    truncated_files: bool,
}

#[derive(Debug, Serialize)]
struct ProjectFileSummary {
    path: String,
    status: String,
    additions: usize,
    deletions: usize,
    bytes: Option<u64>,
    diff_bytes: Option<u64>,
    diff_truncated: bool,
    diff_text: Option<String>,
}

#[derive(Debug)]
struct PatchSummary {
    additions: usize,
    deletions: usize,
    diff_bytes: u64,
    diff_truncated: bool,
    diff_text: Option<String>,
}

pub fn project_summary(start: &Path, max_diff_bytes: Option<u64>) -> Result<Value> {
    let repo = Repository::discover(start)
        .with_context(|| format!("No git repository found from {}", start.display()))?;
    let repo_root = repo
        .workdir()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| repo.path().to_path_buf());
    let max_diff_bytes = max_diff_bytes.unwrap_or(DEFAULT_MAX_DIFF_BYTES).max(1);

    let head = repo.head().ok();
    let branch = head
        .as_ref()
        .and_then(|reference| reference.shorthand())
        .map(ToOwned::to_owned);
    let head_oid = head.as_ref().and_then(|reference| reference.target());
    let head_hex = head_oid.map(|oid| oid.to_string());
    let head_short = head_hex
        .as_ref()
        .map(|value| value.chars().take(12).collect::<String>());
    let head_tree = head
        .as_ref()
        .and_then(|reference| reference.peel_to_tree().ok());

    let status_map = collect_statuses(&repo)?;
    let mut diff_options = DiffOptions::new();
    diff_options
        .include_untracked(true)
        .recurse_untracked_dirs(true)
        .include_typechange(true);
    let diff = repo.diff_tree_to_workdir_with_index(head_tree.as_ref(), Some(&mut diff_options))?;

    let mut files = Vec::new();
    let mut additions = 0usize;
    let mut deletions = 0usize;
    for (idx, delta) in diff.deltas().enumerate() {
        let Some(path) = delta
            .new_file()
            .path()
            .or_else(|| delta.old_file().path())
            .map(path_to_string)
        else {
            continue;
        };
        let patch_summary = Patch::from_diff(&diff, idx)?
            .map(|patch| summarize_patch(patch, max_diff_bytes))
            .transpose()?;
        let file_additions = patch_summary
            .as_ref()
            .map(|summary| summary.additions)
            .unwrap_or(0);
        let file_deletions = patch_summary
            .as_ref()
            .map(|summary| summary.deletions)
            .unwrap_or(0);
        additions += file_additions;
        deletions += file_deletions;
        let bytes = file_size(&repo_root, &path);
        let status = status_map
            .get(&path)
            .map(|status| status_summary(*status))
            .filter(|label| !label.is_empty())
            .unwrap_or_else(|| format!("{:?}", delta.status()).to_lowercase());
        let diff_bytes = patch_summary.as_ref().map(|summary| summary.diff_bytes);
        let diff_truncated = patch_summary
            .as_ref()
            .map(|summary| summary.diff_truncated)
            .unwrap_or(false);
        let diff_text = patch_summary.and_then(|summary| summary.diff_text);
        files.push(ProjectFileSummary {
            path,
            status,
            additions: file_additions,
            deletions: file_deletions,
            bytes,
            diff_bytes,
            diff_truncated,
            diff_text,
        });
        if files.len() >= MAX_CHANGED_FILES {
            break;
        }
    }

    for (path, status) in status_map {
        if files.iter().any(|file| file.path == path) {
            continue;
        }
        let bytes = file_size(&repo_root, &path);
        files.push(ProjectFileSummary {
            path,
            status: status_summary(status),
            additions: 0,
            deletions: 0,
            bytes,
            diff_bytes: None,
            diff_truncated: false,
            diff_text: None,
        });
        if files.len() >= MAX_CHANGED_FILES {
            break;
        }
    }

    sort_project_files(&mut files);
    let changed_files = files.len();
    let truncated_files = changed_files >= MAX_CHANGED_FILES;
    Ok(json!(ProjectSummary {
        ok: true,
        root: path_to_string(&repo_root),
        branch,
        head: head_hex,
        head_short,
        dirty: changed_files > 0,
        changed_files,
        additions,
        deletions,
        max_diff_bytes,
        files,
        truncated_files,
    }))
}

fn summarize_patch(mut patch: Patch<'_>, max_diff_bytes: u64) -> Result<PatchSummary> {
    let (_context, additions, deletions) = patch.line_stats()?;
    let buf = patch.to_buf()?;
    let diff_bytes = buf.len() as u64;
    let diff_truncated = diff_bytes > max_diff_bytes;
    let diff_text = if diff_truncated {
        None
    } else {
        Some(
            buf.as_str()
                .map(ToOwned::to_owned)
                .unwrap_or_else(|| String::from_utf8_lossy(&buf).into_owned()),
        )
    };
    Ok(PatchSummary {
        additions,
        deletions,
        diff_bytes,
        diff_truncated,
        diff_text,
    })
}

fn collect_statuses(repo: &Repository) -> Result<BTreeMap<String, Status>> {
    let mut options = StatusOptions::new();
    options
        .include_untracked(true)
        .recurse_untracked_dirs(true)
        .renames_head_to_index(true)
        .renames_index_to_workdir(true);
    let statuses = repo.statuses(Some(&mut options))?;
    let mut out = BTreeMap::new();
    for entry in statuses.iter() {
        let Some(path) = entry.path() else {
            continue;
        };
        out.insert(path.to_owned(), entry.status());
    }
    Ok(out)
}

fn status_summary(status: Status) -> String {
    let mut labels = Vec::new();
    if status.contains(Status::CONFLICTED) {
        labels.push("conflicted");
    }
    if status.contains(Status::INDEX_NEW) || status.contains(Status::WT_NEW) {
        labels.push("added");
    }
    if status.contains(Status::INDEX_MODIFIED) || status.contains(Status::WT_MODIFIED) {
        labels.push("modified");
    }
    if status.contains(Status::INDEX_DELETED) || status.contains(Status::WT_DELETED) {
        labels.push("deleted");
    }
    if status.contains(Status::INDEX_RENAMED) || status.contains(Status::WT_RENAMED) {
        labels.push("renamed");
    }
    if status.contains(Status::INDEX_TYPECHANGE) || status.contains(Status::WT_TYPECHANGE) {
        labels.push("typechange");
    }
    if status.contains(Status::WT_UNREADABLE) {
        labels.push("unreadable");
    }
    if labels.is_empty() {
        "clean".to_owned()
    } else {
        labels.join(", ")
    }
}

fn sort_project_files(files: &mut [ProjectFileSummary]) {
    files.sort_by(|left, right| {
        status_sort_rank(&left.status)
            .cmp(&status_sort_rank(&right.status))
            .then_with(|| left.path.cmp(&right.path))
            .then_with(|| left.status.cmp(&right.status))
    });
}

fn status_sort_rank(status: &str) -> u8 {
    let labels = status
        .split(',')
        .map(|label| label.trim().to_ascii_lowercase())
        .collect::<Vec<_>>();
    for (needle, rank) in [
        ("modified", 0),
        ("renamed", 1),
        ("added", 2),
        ("deleted", 3),
        ("typechange", 4),
        ("conflicted", 5),
        ("unreadable", 6),
        ("clean", 7),
    ] {
        if labels.iter().any(|label| label == needle) {
            return rank;
        }
    }
    8
}

fn file_size(root: &Path, rel_path: &str) -> Option<u64> {
    let path = if Path::new(rel_path).is_absolute() {
        PathBuf::from(rel_path)
    } else {
        root.join(rel_path)
    };
    fs::metadata(path).ok().map(|metadata| metadata.len())
}

fn path_to_string(path: &Path) -> String {
    path.to_string_lossy().into_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn project_file(path: &str, status: &str) -> ProjectFileSummary {
        ProjectFileSummary {
            path: path.to_owned(),
            status: status.to_owned(),
            additions: 0,
            deletions: 0,
            bytes: None,
            diff_bytes: None,
            diff_truncated: false,
            diff_text: None,
        }
    }

    #[test]
    fn status_sort_rank_prefers_requested_category_order() {
        assert!(status_sort_rank("modified") < status_sort_rank("renamed"));
        assert!(status_sort_rank("renamed") < status_sort_rank("added"));
        assert!(status_sort_rank("added") < status_sort_rank("deleted"));
        assert_eq!(
            status_sort_rank("added, modified"),
            status_sort_rank("modified")
        );
        assert_eq!(
            status_sort_rank("renamed, typechange"),
            status_sort_rank("renamed")
        );
    }

    #[test]
    fn sort_project_files_groups_by_status_then_path() {
        let mut files = vec![
            project_file("z-added.rs", "added"),
            project_file("b-modified.rs", "modified"),
            project_file("renamed.rs", "renamed"),
            project_file("a-modified.rs", "modified"),
            project_file("deleted.rs", "deleted"),
            project_file("other.rs", "unknown"),
        ];

        sort_project_files(&mut files);

        let ordered_paths = files
            .iter()
            .map(|file| file.path.as_str())
            .collect::<Vec<_>>();
        assert_eq!(
            ordered_paths,
            vec![
                "a-modified.rs",
                "b-modified.rs",
                "renamed.rs",
                "z-added.rs",
                "deleted.rs",
                "other.rs"
            ]
        );
    }
}
