# SAS.Planet KMZ polygon imagery downloads and raster exports

This Windows project reads polygon features from KMZ files, separates them from
point placemarks, writes native SAS.Planet saved download sessions (`.sls`), and
exports downloaded SQLite cache tiles as polygon-masked, georeferenced rasters.
The repository defaults to SAS.Planet Z16. The included
`Selection_91_All_Areas.kmz` is the strict regression dataset: it contains 20
tender polygons and 854 point placemarks. Other polygon KMZ files and zoom
levels are configurable. SAS.Planet starts generated sessions with:

```powershell
SASPlanet.exe --sls-autostart 'C:\path\to\session.sls'
```

The project does not drive the desktop, click controls, use screen coordinates,
take screenshots, or automate Google Earth. Downloads use SAS.Planet's saved
session format and `--sls-autostart`; export reads the configured SQLite cache
without changing or deleting it.

The workflow has two distinct data-producing stages:

1. `run` or `resume` launches SAS.Planet, which downloads tiles into its normal
   configured cache.
2. `export` reads those cached tiles and creates GeoTIFF/JPEG deliverables below
   `output\`.

A successful `run --confirm-download` therefore does not itself create a TIFF.
Wait for SAS.Planet to finish, then run the matching `export` command.

## Prerequisites

- Windows 11 and PowerShell 7
- Python 3.11 or newer, including the Windows `py` launcher
- SAS.Planet with `--sls-autostart` support
- a configured SAS.Planet SQLite cache
- an enabled SAS.Planet imagery definition, such as `ESRI ArcGIS.Imagery`
- Node.js/npm when using `scripts\setup.ps1` or running the full documentation
  and type-check suite

Google Earth, desktop-control libraries, and administrator privileges are not
required. Network access is used only when a confirmed SAS.Planet download is
running and by package installation during setup.

## Reference environment

The repository regression workflow was last verified on 2026-07-21 with:

- Windows 11 and PowerShell 7
- Python 3.13.14
- SAS.Planet `26.4.4.10916`:
  `C:\Users\anand.ts\Downloads\SAS.Planet.Release.260404.x64\SASPlanet.exe`
- Installed map: `ESRI ArcGIS.Imagery`
- Installed ESRI GUID: `{7B743985-BC5F-4AB6-8915-AC5DBBB8F552}`
- SAS.Planet executable contains `--sls-autostart`

These paths identify the checked reference machine; they are not embedded in
the reusable scripts. Set your own executable path in `config.yaml`.

Re-run the non-launching environment inspection:

```powershell
Set-Location 'C:\Users\anand.ts\Downloads\sas_auto'
.\scripts\inspect_environment.ps1
```

For JSON output:

```powershell
.\scripts\inspect_environment.ps1 -AsJson
```

The inspector reads `input_kmz` and `sasplanet_exe` from the selected config;
it has no user-specific executable path embedded in the script. To inspect a
different configuration or explicit paths without launching anything:

```powershell
.\scripts\inspect_environment.ps1 -ConfigPath .\config.example.yaml -AsJson
.\scripts\inspect_environment.ps1 `
  -InputKmz .\inputs\areas.kmz `
  -SasPlanetExe 'C:\path\to\SASPlanet.exe'
```

## Clone and set up

```powershell
git clone https://github.com/anandts-nmg/sas_auto.git
Set-Location .\sas_auto
.\scripts\setup.ps1
```

Runtime dependencies are PyYAML and Pillow. The setup script also installs the
development lint and type-check tools, PSScriptAnalyzer, and the npm-based
Markdown/Pyright tools. It creates `.venv` and installs this package in editable
mode.

All CLI examples can be run through the PowerShell wrapper without activating
the virtual environment:

```powershell
.\scripts\run.ps1 --help
```

## Configuration

The operative defaults in `config.yaml` are:

```yaml
input_kmz: Selection_91_All_Areas.kmz
sasplanet_exe: 'C:\Users\anand.ts\Downloads\SAS.Planet.Release.260404.x64\SASPlanet.exe'

dataset:
  id: selection_91
  profile: selection_91
  namespace_outputs: false

parser:
  id_fields: [area_code, code, Код, id]
  name_fields: [area_name, name, Талбай]

dry_run: true
area_codes:
  - "9101"

imagery:
  source: Esri World Imagery
  zoom_levels:
    - 16
  download_missing_tiles_only: true

sessions:
  directory: generated/sls
  auto_close_at_finish: false
  workers_count: 1

safety:
  pilot_feature_id: "9101"
  require_completed_pilot_before_multi_download: true
  max_rectangle_tiles_per_feature: 100000
  max_rectangle_tiles_total: 250000

export:
  enabled: true
  directory: output
  preferred_format: GeoTIFF
  fallback_format: JPEG
  include_georeferencing: true
  mask_to_polygon: true
  preview_max_size: 1200
  require_complete_cache: true
  max_mosaic_pixels: 75000000
  max_mosaic_dimension: 30000
```

`Esri World Imagery` resolves to the installed SAS.Planet map named
`ESRI ArcGIS.Imagery`. No provider fallback is performed. If that definition is
missing or inactive, the command stops.

`download_missing_tiles_only: true` writes `ReplaceExistTiles=0`, so existing
cache tiles are not replaced.

`dry_run: true` is a validated compatibility marker for the intended safe
posture. Command-line authorization is the controlling gate: the current CLI
does not launch SAS.Planet unless that same invocation includes
`--confirm-download`.

`safety` limits conservative rectangle estimates before a confirmed launch.
`export.max_mosaic_pixels` and `export.max_mosaic_dimension` independently
limit raster memory allocation. Raising these values is an explicit operator
decision; the toolkit never adjusts them automatically.

### Use another KMZ

Put local KMZ inputs under `inputs\`; its contents except `.gitkeep` are ignored
by Git so large or private source files are not committed. Keep the included
regression input at the repository root.

```powershell
Copy-Item 'C:\path\to\Selection_92_All_Areas.kmz' '.\inputs\areas.kmz'
Copy-Item '.\config.example.yaml' '.\config.yaml' -Force
```

At minimum, set these paths in `config.yaml`; the copied example already uses
the portable auto-detection settings shown here:

```yaml
input_kmz: inputs/areas.kmz
sasplanet_exe: 'C:\path\to\SASPlanet.exe'

dataset:
  id: auto
  profile: auto
  namespace_outputs: true
```

Run `list-features`, then replace the example `area_codes` value with one or
more detected IDs if you intend to use `--configured`. With
`safety.pilot_feature_id: auto`, the pilot is the first configured ID that
exists in the input, or otherwise the first verified polygon.

To keep several input configurations without repeatedly replacing
`config.yaml`, pass a config path before the subcommand:

```powershell
.\scripts\run.ps1 --config .\configs\selection-92.yaml inspect
.\scripts\run.ps1 --config .\configs\selection-92.yaml generate
```

Relative paths inside any selected config are resolved from the repository
root, not from the config file's directory.

`profile: auto` recognizes the tender-table pattern used by selection 91 and
similar selections such as 92. Tender codes are read from metadata; they are not
restricted to `9101`-`9120`. If those fields are absent, the parser switches to
the generic polygon profile.

Generic feature IDs are selected from `parser.id_fields`, then the KML
placemark `id`, then the placemark name, and finally a deterministic
`feature_0001` fallback. IDs are converted to portable ASCII path components;
duplicate IDs receive stable `_2`, `_3`, and later suffixes. Unicode display
names and metadata are preserved in manifests and GIS outputs. Windows device
names such as `CON`, `NUL`, `COM1`, and `LPT1` are safely prefixed before they
become directory or file names.

With `dataset.id: auto`, the namespace is the sanitized filename plus the first
eight characters of the KMZ SHA-256 hash, for example
`selection_92_a1b2c3d4`. Two different KMZ files with the same filename
therefore cannot silently share generated output or workflow state.

Supported geometry/input behavior:

- KML 2.2 and namespaceless KML members inside a valid KMZ/ZIP archive
- `doc.kml` or other `.kml` member names
- `Polygon` and polygon `MultiGeometry`, including inner rings (holes)
- WGS 84 longitude/latitude coordinates
- any number of unrelated point placemarks, which are inventory-only
- multiple inputs processed one at a time with isolated generated, session,
  state, plan, and raster output directories

LineStrings, point-only KMZ files, GroundOverlays, and non-WGS-84 coordinates
are not converted into download polygons. The command stops with a clear error
instead of guessing a buffer or coordinate transformation.
Polygons beyond Web Mercator's approximately `+/-85.051129` degree latitude
limit are also rejected because SAS.Planet's imagery tile matrix cannot
represent them correctly.

## Validate the KMZ

```powershell
.\scripts\run.ps1 inspect
.\scripts\run.ps1 list-features
```

The verified input inventory is:

- ZIP member `doc.kml`
- KML namespace `http://www.opengis.net/kml/2.2`
- WGS 84 / EPSG:4326
- 874 placemarks
- 20 actual polygon placemarks, codes `9101` through `9120`
- 854 point placemarks
- 814 vertex points and 40 auxiliary centroid/label points

Only polygon geometry is written to `.sls`. Names such as `9101_V001` are never
treated as independent download areas.

For a generic input, use `list-features` before choosing a feature ID:

```powershell
.\scripts\run.ps1 list-features
.\scripts\run.ps1 plan --feature custom-7
```

`--feature` and `--area` are equivalent CLI names.

## Command behavior

| Command | Launches SAS.Planet | Starts network download | Creates rasters |
| --- | --- | --- | --- |
| `inspect`, `list-features` | No | No | No |
| `generate`, `session`, `plan` | No | No | No |
| `run` or `run-all` without confirmation | No | No | No |
| `run --confirm-download` or confirmed `run-all` | Yes | SAS.Planet may | No |
| `resume` without confirmation | No | No | No |
| `resume --confirm-download` | Yes | Per stored SLS; default missing-only | No |
| `export` | No | No | Yes, from cache |
| `status` | No | No | No |

All commands validate `config.yaml` before doing work. `plan` writes the exact
launch command and safety estimates for review without starting SAS.Planet.

## Generate all GIS and SLS files

```powershell
.\scripts\run.ps1 generate
```

This command performs no network download and does not launch SAS.Planet. It
creates:

```text
generated\manifest\areas.csv
generated\manifest\areas.json
generated\geojson\selection_91_areas.geojson
generated\kml\9101.kml ... 9120.kml
generated\kml\selection_91_areas.kml
generated\sls\9101_ESRI_Z16.sls ... 9120_ESRI_Z16.sls
generated\sls\ALL_KMZ_ESRI_Z16.sls
```

Every `.sls` is written atomically and then parsed back for validation.

With `dataset.namespace_outputs: true`, the same artifact categories are kept
under the auto-detected dataset ID, for example:

```text
generated\selection_92_a1b2c3d4\manifest\areas.json
generated\selection_92_a1b2c3d4\kml\9201.kml
generated\selection_92_a1b2c3d4\geojson\selection_92_a1b2c3d4_areas.geojson
generated\sls\selection_92_a1b2c3d4\9201_ESRI_Z16.sls
output\selection_92_a1b2c3d4\9201\9201.tif
state\datasets\selection_92_a1b2c3d4.json
state\plans\selection_92_a1b2c3d4\9201_ESRI_Z16.json
```

## Prepare sessions without launching

One area:

```powershell
.\scripts\run.ps1 session --area 9101
```

The configured `area_codes` list:

```powershell
.\scripts\run.ps1 session --configured
```

All 20 polygons in one session:

```powershell
.\scripts\run.ps1 session --all
```

Create a JSON plan with bounds and conservative tile estimates:

```powershell
.\scripts\run.ps1 plan --area 9101
.\scripts\run.ps1 plan --all
```

Plans are saved under `state\plans\` and never start a download.
SAS.Planet zooms are one-based, so a plan for SAS.Planet Z16 reports Web
Mercator tile-matrix zoom 15. The plan records both values to prevent an
off-by-one scope estimate. It also records conservative per-feature and total
rectangle estimates; a confirmed launch is refused if either configured safety
limit is exceeded.

## Real Z16 download

Commands remain dry runs unless the same command includes
`--confirm-download`.

Review the pilot first:

```powershell
.\scripts\run.ps1 run --area 9101 --dry-run
```

Start the real area 9101 ESRI Z16 session:

```powershell
.\scripts\run.ps1 run --area 9101 --confirm-download
```

That executes the equivalent of:

```powershell
& 'C:\Users\anand.ts\Downloads\SAS.Planet.Release.260404.x64\SASPlanet.exe' `
  --sls-autostart `
  'C:\Users\anand.ts\Downloads\sas_auto\generated\sls\9101_ESRI_Z16.sls'
```

After the pilot download finishes, export and validate it:

```powershell
.\scripts\run.ps1 export --area 9101
```

Only after state records the pilot as `completed`, start all 20 regression
polygons as one SAS.Planet session:

```powershell
.\scripts\run.ps1 run-all --confirm-download
```

The combined SLS separates polygons with SAS.Planet's `NaN,NaN` delimiter, so
the download iterator uses the polygon collection rather than the large overall
bounding rectangle.

`run-all --confirm-download` and confirmed multi-feature `resume` commands are
blocked until `safety.pilot_feature_id` has a validated raster export. Dry-run,
session, and plan commands remain available before that gate.

Planning or relaunching a scope that includes an already completed area does
not reset that area's `completed` state. SAS.Planet still skips its existing
tiles because the session uses `ReplaceExistTiles=0`.

The CLI reports `download_launched`, not `completed`. SAS.Planet owns the actual
tile requests and displays download completion in its progress window.

## Resume an interrupted download

Relaunch the same area session:

```powershell
.\scripts\run.ps1 resume --area 9101 --confirm-download
```

Relaunch the combined session:

```powershell
.\scripts\run.ps1 resume --all --confirm-download
```

The session re-enumerates its polygon tiles, but `ReplaceExistTiles=0` makes
SAS.Planet skip tiles already present in its configured cache and request only
missing tiles. The toolkit never deletes or rewrites cache contents.

## Export and validate cached imagery

After SAS.Planet finishes the pilot download, export area `9101` without making
any network requests:

```powershell
.\scripts\run.ps1 export --area 9101
```

The exporter discovers `SQLiteCache` from `SASPlanet.ini`, resolves
`NameInCache` from the selected provider's `params.txt`, and requires every tile
intersecting the verified polygon. It then creates:

```text
output\9101\9101.tif
output\9101\9101.tfw
output\9101\9101.prj
output\9101\preview.png
output\9101\validation.json
output\9101\run-summary.txt
```

The GeoTIFF contains embedded EPSG:3857 pixel scale, tie point, and CRS keys. The
world and projection sidecars are also written for compatibility. Pixels outside
the tender polygon are transparent.

Validation checks the tile inventory, image decoding, raster dimensions and
content variation, polygon coverage, raster bounds, georeferencing, and output
hashes. Workflow state is marked `completed` only after every check passes.
Before allocating image memory, export also enforces `max_mosaic_pixels` and
`max_mosaic_dimension`. This safely rejects widely separated MultiPolygon parts
that would otherwise require a massive mostly-empty rectangular mosaic.

After all 20 cache downloads are available, export them sequentially:

```powershell
.\scripts\run.ps1 export --all
```

An all-area export continues past areas with missing cache tiles, records them as
failed, and creates these aggregate files:

```text
output\batch-summary.csv
output\batch-summary.json
output\batch-report.md
output\checksums.sha256
output\file-inventory.csv
```

Use `--configured` instead of `--all` to export only codes listed in
`config.yaml`.

## State and logs

```powershell
.\scripts\run.ps1 status
```

Launch and validated-export state is written atomically to
`state\workflow.json`. Plans are under `state\plans\`, and command logs are
under `logs\`.

For namespaced arbitrary inputs, state is written to
`state\datasets\<dataset-id>.json`, and plans are written below
`state\plans\<dataset-id>\`.

The input KMZ and SAS.Planet `Maps` definitions are always read-only. The only
SAS.Planet data changed by a real run is its normal configured imagery cache.

## SLS fields generated

The writer was verified against the installed SAS.Planet v26.4.4 saved-session
schema:

- `[Session]`
- installed `MapGUID`
- one-based `Zoom` and `ZoomArr`
- `ReplaceExistTiles=0` for missing-only downloads
- zeroed progress counters
- `PointLon_n` and `PointLat_n` in WGS 84
- `NaN,NaN` separators between polygons
- SAS.Planet inner-ring delimiters for polygon holes
- one worker by default
- session auto-save disabled, so no files are written inside the SAS.Planet
  installation

## Tests and quality checks

```powershell
.\.venv\Scripts\python.exe -m pytest
.\scripts\lint.ps1
```

The lint command runs Ruff, Ruff formatting verification, mypy,
Pyright/Pylance-compatible checks, yamllint, PSScriptAnalyzer, and
markdownlint-cli2.

`pytest` uses portable fixtures for SAS.Planet map definitions and executable
capability detection. It does not require the reference machine's external
SAS.Planet installation and never starts a download.

## Troubleshooting

### Map with GUID not found

Run `inspect_environment.ps1` and confirm the ESRI definition and GUID. The
toolkit reads the GUID from the installed `Maps` tree every time; it never edits
`params.txt`.

### Download stays at zero

Check that `ESRI ArcGIS.Imagery` is enabled and functional in SAS.Planet and
that the network/provider is available. The toolkit does not bypass provider
restrictions, authentication, or rate limits.

### Existing tiles are downloaded again

Confirm the generated SLS contains `ReplaceExistTiles=0` and that SAS.Planet is
using the expected cache location.

### Resume session is missing

Regenerate it without launching:

```powershell
.\scripts\run.ps1 session --area 9101
```

Then run the matching `resume` command with explicit confirmation.

### Multi-feature launch is blocked by the pilot gate

Download and export the configured pilot first:

```powershell
.\scripts\run.ps1 run --area 9101 --confirm-download
# Wait for SAS.Planet to finish.
.\scripts\run.ps1 export --area 9101
```

Use `status` to confirm the pilot is `completed`. For arbitrary inputs, obtain
the actual pilot ID with `list-features` and check `safety.pilot_feature_id`.

### Tile-scope or mosaic safety limit is exceeded

Review the JSON plan, geometry bounds, polygon parts, and configured zoom.
Widely separated MultiPolygon parts can imply a very large rectangular raster
even when relatively few tiles intersect the polygons. Prefer splitting such a
feature into separate output features. Increase a safety limit only after
estimating the required download size and image memory.

### GeoTIFF/JPEG output

If export reports missing tiles, first resume the matching download and wait for
SAS.Planet to finish:

```powershell
.\scripts\run.ps1 resume --area 9101 --confirm-download
.\scripts\run.ps1 export --area 9101
```

The exporter refuses incomplete or corrupt cache coverage by default. It never
silently fills missing tiles, switches providers, or marks a partial raster as
complete. If GeoTIFF encoding itself fails, the configured JPEG fallback is
written with `.jgw` and `.prj` georeferencing files and the fallback is recorded
in validation output.
