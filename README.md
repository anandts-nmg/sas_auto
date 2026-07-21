# SAS.Planet KMZ to Z16 download sessions

This Windows project validates `Selection_91_All_Areas.kmz`, separates its 20
tender-area polygons from the 854 point placemarks, and writes native
SAS.Planet saved download sessions (`.sls`). SAS.Planet can start those sessions
directly with:

```powershell
SASPlanet.exe --sls-autostart 'C:\path\to\session.sls'
```

The project does not drive the desktop, click controls, use screen coordinates,
take screenshots, or automate Google Earth. It relies only on SAS.Planet's saved
session format and documented `--sls-autostart` command-line option.

## Verified local environment

- Windows 11 and PowerShell 7
- Python 3.13.14 (Python 3.11 or newer is supported)
- SAS.Planet `26.4.4.10916`:
  `C:\Users\anand.ts\Downloads\SAS.Planet.Release.260404.x64\SASPlanet.exe`
- Installed map: `ESRI ArcGIS.Imagery`
- Installed ESRI GUID: `{7B743985-BC5F-4AB6-8915-AC5DBBB8F552}`
- SAS.Planet executable contains `--sls-autostart`

Re-run the non-launching environment inspection:

```powershell
Set-Location 'C:\Users\anand.ts\Downloads\sas_auto'
.\scripts\inspect_environment.ps1
```

For JSON output:

```powershell
.\scripts\inspect_environment.ps1 -AsJson
```

## Setup

```powershell
Set-Location 'C:\Users\anand.ts\Downloads\sas_auto'
.\scripts\setup.ps1
```

The runtime dependency is only PyYAML. The setup script also installs the
development lint and type-check tools.

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
```

`Esri World Imagery` resolves to the installed SAS.Planet map named
`ESRI ArcGIS.Imagery`. No provider fallback is performed. If that definition is
missing or inactive, the command stops.

`download_missing_tiles_only: true` writes `ReplaceExistTiles=0`, so existing
cache tiles are not replaced.

## Validate the KMZ

```powershell
.\scripts\run.ps1 inspect
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

After reviewing the pilot, start all 20 polygons as one SAS.Planet session:

```powershell
.\scripts\run.ps1 run-all --confirm-download
```

The combined SLS separates polygons with SAS.Planet's `NaN,NaN` delimiter, so
the download iterator uses the polygon collection rather than the large overall
bounding rectangle.

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

## State and logs

```powershell
.\scripts\run.ps1 status
```

Launch state is written atomically to `state\workflow.json`. Plans are under
`state\plans\`, and command logs are under `logs\`.

The input KMZ and SAS.Planet `Maps` definitions are always read-only. The only
SAS.Planet data changed by a real run is its normal configured imagery cache.

## SLS fields generated

The writer follows the installed SAS.Planet v26.4.4 source schema:

- `[Session]`
- installed `MapGUID`
- one-based `Zoom` and `ZoomArr`
- `ReplaceExistTiles=0` for missing-only downloads
- zeroed progress counters
- `PointLon_n` and `PointLat_n` in WGS 84
- `NaN,NaN` separators between polygons
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

### GeoTIFF/JPEG output

This direct workflow downloads imagery into the SAS.Planet cache. It does not
automate the desktop stitching/export dialog. Export is intentionally outside
this repository's current scope.
