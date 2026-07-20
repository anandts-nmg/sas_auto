# SAS.Planet tender-area automation toolkit

This Windows-first project reads `Selection_91_All_Areas.kmz` without modifying it, verifies the 20 tender-area polygons, generates clean code-based GIS outputs, opens generated KML files for human review in Google Earth Pro, and prepares a calibrated, resumable SAS.Planet workflow.

The default is deliberately safe:

- `dry_run: true`
- only area `9101` is selected in `config.yaml`
- export is disabled
- no command silently changes imagery providers
- no real SAS.Planet download can begin until `--confirm-download` is supplied and a reusable UI workflow has been calibrated and approved
- the original KMZ, SAS.Planet `Maps`, cache, and application files are never written by this toolkit

## Verified local environment

- Windows 11 / PowerShell 7 workflow
- Python 3.13.14
- SAS.Planet `26.4.4.10916` at `C:\Users\anand.ts\Downloads\SAS.Planet.Release.260404.x64\SASPlanet.exe`
- Google Earth Pro `7.3.7.1155` at `C:\Program Files\Google\Google Earth Pro\client\googleearth.exe`
- both public-desktop and Start-menu Google Earth shortcuts resolve to that executable
- the SAS.Planet map set contains `Google - Satellite` and `ESRI ArcGIS.Imagery`

Run the reproducible read-only environment check at any time:

```powershell
Set-Location 'C:\Users\anand.ts\Downloads\sas_auto'
.\scripts\inspect_environment.ps1
```

For machine-readable output:

```powershell
.\scripts\inspect_environment.ps1 -AsJson
```

This script resolves Google Earth `.lnk` files with `WScript.Shell`; it does not launch either application.

## Prerequisites and setup

Required:

- PowerShell 7
- Python 3.11 or newer through the `py` launcher
- the configured SAS.Planet release
- Google Earth Pro for visual verification

Create the isolated environment and install the project:

```powershell
Set-Location 'C:\Users\anand.ts\Downloads\sas_auto'
.\scripts\setup.ps1
```

Activate it if you want to use the requested `python -m` form directly:

```powershell
.\.venv\Scripts\Activate.ps1
python -m sas_auto.cli --help
```

If script execution is blocked for the current PowerShell process only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\scripts\setup.ps1
```

Do not run the shell as Administrator. The current paths do not require elevation.

## Configuration

`config.yaml` contains the detected executable paths and safe starting values. `config.example.yaml` is a reusable template with `google_earth_exe: null`.

The first entry under `imagery.preferred_sources` is the requested provider. The planner resolves name aliases such as `Google Satellite` to the installed definition `Google - Satellite`. If that requested source is missing, it stops and reports alternatives; it does not fall through silently to the next provider.

Zoom `15`, a 10% framing buffer, missing-tiles-only behavior, area `9101`, dry-run mode, and disabled export are configuration defaults—not permission to download.

## Inspect and generate

Inspect the KMZ:

```powershell
python -m sas_auto.cli inspect
```

Generate manifests and clean geometry files:

```powershell
python -m sas_auto.cli generate
```

Equivalent wrapper commands, without activating the environment:

```powershell
.\scripts\run.ps1 inspect
.\scripts\run.ps1 generate
```

The source inventory verified during implementation is:

- ZIP entry: `doc.kml`
- KML namespace: `http://www.opengis.net/kml/2.2`
- 874 placemarks total
- 20 polygon placemarks, codes exactly `9101`–`9120`
- 854 point placemarks
- 814 vertex points, matching the sum of all declared polygon vertex counts
- 40 auxiliary points: one centroid and one label point per area
- WGS 84 / EPSG:4326 for all polygons
- every source LinearRing is closed

Each polygon has one more coordinate in KML than its declared count because a `LinearRing` repeats the first vertex at the end. The manifest records the source, unique, and declared counts separately. That required closing point is not treated as a source repair.

## Google Earth Pro verification

Open only area 9101’s generated KML:

```powershell
python -m sas_auto.cli earth-open --area 9101
```

Open the clean combined KML after individual checks:

```powershell
python -m sas_auto.cli earth-open --all
```

Google Earth is used only for visual verification. The toolkit does not scrape imagery or automate licenses, sign-in, CAPTCHA, or security dialogs.

Manual checklist for area 9101:

1. Confirm the Places entry is `9101 - Уудавын булаг`.
2. Confirm exactly one polygon appears and no vertex-point placemarks are present.
3. Confirm the polygon is near 92.358–92.414 E and 46.340–46.394 N.
4. Inspect for self-crossing edges, unexpected gaps, or an implausibly broad envelope.
5. Open the placemark description and verify tender 91, code 9101, Ховд / Алтай, and 1053.51 ha.
6. Do not use Google Earth imagery as an automated data source.

## SAS.Planet calibration

The local executable contains command tokens for `--map`, `--zoom`, `--move`, `--move-xyz`, `--navigate`, placemark operations, and `--sls-autostart`. The local changelog also mentions saved-session autostart. Those findings are diagnostic only: the toolkit does not assume undocumented argument syntax or construct a download session before calibration.

Start safe UI inspection:

```powershell
python -m sas_auto.cli calibrate
```

Calibration may launch SAS.Planet, but it will not import a KML, click a download command, dismiss a dialog, or start a network operation. It records:

- window title and bounds
- window DPI and scale
- monitor geometry
- UI Automation control metadata and visible process windows
- a screenshot under `screenshots\`
- visible KML/import, polygon-selection, and zoom label candidates
- detected command tokens
- required manual checks in `state\calibration.json`

If an additional window or dialog appears, calibration records it and stops for human review. Move the mouse to PyAutoGUI’s top-left fail-safe corner or press `Ctrl+C` to stop any later calibrated automation.

Because SAS.Planet uses custom/Delphi controls in some builds and menu labels can be English or Russian, real playback remains blocked until the installation-specific import, polygon selection, provider, download, and export controls have been visibly verified. Absolute coordinates are not used unless a later calibration explicitly records matching DPI, window, and monitor geometry.

## Plan and dry run

Create a no-network plan for the pilot area:

```powershell
python -m sas_auto.cli plan --area 9101
```

Run the pilot dry run and atomically persist its state:

```powershell
python -m sas_auto.cli run --area 9101 --dry-run
```

The 9101 plan currently resolves `Google - Satellite`, zoom 15, and a conservative 63-tile buffered bounding rectangle. SAS.Planet polygon selection may cover fewer tiles. No network request occurs during planning.

After the 9101 dry run is reviewed, all 20 plans can be generated without downloads:

```powershell
python -m sas_auto.cli run-all --dry-run
```

`run-all` refuses to proceed until state shows a successful 9101 dry run. There is no bulk real-download command in this initial implementation.

## Approving one real test area

The explicit command boundary is:

```powershell
python -m sas_auto.cli run --area 9101 --confirm-download
```

Supplying the flag records explicit intent, but the command still stops safely unless calibration has a verified, reusable workflow for this exact SAS.Planet installation. Initial calibration intentionally sets `download_workflow_verified` to false and lists the human checks still required. Do not manually change that field merely to bypass the gate.

Before approving a real area, verify all of the following:

1. Generated `9101.kml` imports as one selectable polygon.
2. The visible provider is exactly the requested configured provider.
3. Only zoom 15 is selected.
4. “Missing tiles only” behavior is confirmed in the visible dialog.
5. The selected scope follows the polygon or the displayed rectangular scope is explicitly accepted.
6. The tile count is plausible compared with the saved plan.
7. A pre-operation screenshot exists.
8. Export remains off unless the selected georeferenced format has been verified.
9. Provider terms, authentication, and rate limits permit the operation.

Unknown dialogs are never accepted automatically.

## Status and resume

View atomic state:

```powershell
python -m sas_auto.cli status
```

Resume the next incomplete configured area in dry-run mode:

```powershell
python -m sas_auto.cli resume
```

`resume` refuses automatic operation if `dry_run` is false. Completed or dry-run-completed areas are not repeated. State uses a temporary file, flush, `fsync`, and atomic `os.replace` in the same directory.

## Output layout

```text
generated\manifest\areas.csv
generated\manifest\areas.json
generated\geojson\selection_91_areas.geojson
generated\kml\9101.kml ... 9120.kml
generated\kml\selection_91_areas.kml
state\plans\<area_code>.json
state\workflow.json
state\calibration.json            # after calibration
logs\<UTC timestamp>_<command>.log
screenshots\<UTC timestamp>_*.png
output\<area_code>\               # reserved for validated exports
```

Mongolian names remain in UTF-8 metadata. Filesystem output directories use area codes so Unicode path handling does not become part of the GUI automation contract.

## Tests

Run non-GUI tests:

```powershell
python -m pytest -m 'not gui'
```

Run the full suite (the interactive GUI test is skipped by default):

```powershell
python -m pytest
```

The implementation session result was `23 passed, 1 skipped` on Python 3.13.14.

## Troubleshooting

### Windows DPI scaling

Run calibration again after changing display scaling. Do not reuse coordinate fallbacks when saved DPI, window size, or monitor geometry differs. Prefer UIA/Win32 controls or image anchors.

### Multiple monitors

Keep SAS.Planet on the monitor used for calibration. Recalibrate after monitor order, resolution, orientation, or primary-monitor changes. Negative monitor coordinates are valid on Windows.

### Missing map provider

Run `plan`. It reports the exact requested provider and refuses substitution. Check SAS.Planet’s existing `Maps` definitions manually; this toolkit never edits them. Update `config.yaml` only after consciously choosing an available provider and reviewing its terms.

### Unexpected dialog

Do not dismiss it through automation. Capture it, read it, and decide manually. Calibration and later UI drivers are configured to stop and save diagnostics.

### Unicode output or paths

Use PowerShell 7 and the supplied scripts. The CLI explicitly writes UTF-8 console output. Keep SAS.Planet itself in its current Latin-character path; generated filesystem names use numeric codes.

### Google Earth opens the wrong data

Use `earth-open --area 9101`; it passes `generated\kml\9101.kml`, never the original KMZ. Regenerate if the file is missing.

### SAS.Planet cache or Maps concerns

Stop immediately. No project command should delete a cache, rewrite a map definition, or install a map. Compare paths in `config.yaml` and the plan before continuing.
