# Move from the nested Workbench layout on Windows

The repository now has one application at its root on `main`. A fresh clone
beside your existing folder gives you that layout without disturbing local
captures, uncommitted work, older scripts, or environments.

These are local operator steps. Publishing the repository cleanup does not
move anything on your PC.

## 1. Inspect and preserve the existing checkout

Finish any capture in progress and stop Workbench using Ctrl+C in its own
terminal. Use the actual existing repository root for `$oldRepo`:

```powershell
$oldRepo = 'C:\Users\lazer\Documents\OpenFlight\BenchTesting\openflight-benchtesting-tools'
if (-not (Test-Path -LiteralPath (Join-Path $oldRepo '.git'))) {
    throw 'Set $oldRepo to the existing repository root before continuing.'
}
git -C $oldRepo status --short --branch
git -C $oldRepo branch -vv
git -C $oldRepo ls-files --others --exclude-standard
git -C $oldRepo status --short --ignored
```

Keep that folder intact. Uncommitted changes, untracked files, and unpushed
commits are only in the old checkout until you deliberately carry them over.
The ignored-file listing helps locate captures, archives, and local settings.
Do not use `git clean`, `reset --hard`, or mass deletion to perform this migration.

## 2. Clone the organized main branch beside it

```powershell
$newRepo = Join-Path (Split-Path $oldRepo -Parent) 'OpenFlight-Radar-Workbench'
if (Test-Path -LiteralPath $newRepo) {
    throw 'Destination exists. Inspect it or choose a new empty destination.'
}
git clone --branch main https://github.com/Robinante/OpenFlight-Radar-Workbench.git $newRepo
if ($LASTEXITCODE -ne 0) { throw 'Clone failed; stop here.' }
Set-Location -LiteralPath $newRepo
py -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
.\.venv\Scripts\python.exe -m pip install -e ".[workbench,dev]"
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
```

The new clone already uses the renamed GitHub URL. Rebuild `.venv`; do not copy
or move the old virtual environment, whose launchers can contain absolute paths.

## 3. Copy complete capture directories, then verify every file

First inspect where the data actually lives. Common locations are:

- `Radar Workbench\captures` under the old repository;
- `captures` at the old repository root;
- an external capture directory outside the repository;
- old chunk folders that may still contain local-only sessions.

Run the following for one source directory at a time. The example gives the
old Workbench's captures their own destination under the new `captures` folder,
so sessions from different sources cannot overwrite one another:

```powershell
$captureSource = Join-Path $oldRepo 'Radar Workbench\captures'
$captureDestination = Join-Path $newRepo 'captures\from-workbench'
if (-not (Test-Path -LiteralPath $captureSource -PathType Container)) {
    throw 'Capture source not found. Set it to the actual data directory.'
}
if (Test-Path -LiteralPath $captureDestination) {
    throw 'Destination exists. Choose a new destination to avoid overwriting data.'
}
New-Item -ItemType Directory -Force -Path (Split-Path $captureDestination -Parent) | Out-Null
Copy-Item -LiteralPath $captureSource -Destination $captureDestination -Recurse -ErrorAction Stop
$sourceRoot = (Resolve-Path -LiteralPath $captureSource).Path
$sourceFiles = @(Get-ChildItem -LiteralPath $sourceRoot -File -Recurse -Force)
if ($sourceFiles.Count -eq 0) { throw 'Source contains no files; check the selected directory.' }
foreach ($file in $sourceFiles) {
    $relative = [System.IO.Path]::GetRelativePath($sourceRoot, $file.FullName)
    $copy = Join-Path $captureDestination $relative
    if (-not (Test-Path -LiteralPath $copy -PathType Leaf)) { throw "Missing copy: $relative" }
    $sourceHash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
    $copyHash = (Get-FileHash -LiteralPath $copy -Algorithm SHA256).Hash
    if ($sourceHash -ne $copyHash) { throw "Hash mismatch: $relative" }
}
"Verified $($sourceFiles.Count) copied files. Original data is still in place."
```

This hash-check block uses PowerShell 7. Copy all files, including JSON
sidecars, manifests, CFG snapshots, `.wire.bin` recordings, rejected dumps, and
hash lists. For another source, change both variables, for example to old root
`captures` and new `captures\from-legacy-root`. Keep each source separate until
you have checked for duplicate sessions. External data can also stay where it
is; Workbench accepts an absolute capture-folder path.

## 4. Verify the new copy

```powershell
Set-Location -LiteralPath $newRepo
.\.venv\Scripts\python.exe -m pytest -q -rs
& '.\Launch Radar Workbench.bat'
```

Set **Capture folder** to a specific copied session (or its dated subfolder),
not the parent of your entire collection. Workbench searches the chosen folder
and, if needed, one level below it. Confirm that a known capture opens and its
plots and metadata look as expected. Dataset-dependent tests skip unless you
configure the external fixtures described in [FIXTURES.md](../tests/FIXTURES.md).

## 5. Archive deliberately

Review local-only files from step 1 before retiring the old checkout. Copy any
needed calibration, notes, or configuration into an appropriate location;
review code changes against the new version before applying them. Do not copy
the whole old project over the new clone.

Once captures are hash-verified, the application opens correctly, and any
uncommitted/unpushed work is accounted for, the old folder can be renamed or
moved into an `Archive` folder outside the active repo. Preserve it as a backup.
Keep old chunk ZIPs and scratch screenshots there rather than in the active
application folder. Update shortcuts to the new root launcher.

There is no need to delete the development branch or rewrite Git history. The
published cleanup retains the prior commits.
