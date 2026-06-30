# UMEP Source Area input generator

This project runs the UMEP Source Area workflow without QGIS. Given full-
resolution DOM/DTM rasters, flux-tower points, hourly weather, and a crop-
height schedule, it calculates directional Kanda morphometry, creates the
13-column UMEP input, runs the Kljun footprint model, and writes styled
GeoTIFFs and tower-placement summaries:

```text
iy id it imin z_0_input z_d_input z_m_input sigv Obukhov ustar dir h por
```

The columns are populated as follows:

| Column | Source |
|---|---|
| `iy id it imin` | Calculated from each weather timestamp |
| `z_0_input`, `z_d_input` | DOM−DTM directional morphometry and UMEP Kanda method; zero-object sectors use the dated crop schedule |
| `z_m_input` | Tower height supplied in the point CSV or with `--measurement-height` |
| `sigv` | Sonic-anemometer input when available; otherwise an explicit approximation such as `--sigma-v-ustar-ratio 2.0` |
| `Obukhov`, `ustar` | Calculated by `merge_footprint_weather.py` from ERA5 turbulent flux and stress fields |
| `dir` | Local Frost wind direction, with ERA5/local-file fallback |
| `h` | ERA5 boundary-layer height |
| `por` | User setting, default 60% |

`generate_umep_footprint_input.py` remains available as the lower-level
converter. It circularly interpolates directional `z0` and `zd` for every
weather record.

## Python environment

The project targets Python 3.11 and keeps dependencies in a repository-local
virtual environment. On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r .\requirements.txt
```

If PowerShell blocks activation, the environment can be used directly without
changing execution policy:

```powershell
.\.venv\Scripts\python.exe .\generate_umep_footprint_input.py --help
```

Run the tests inside the environment:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Agricultural fields and dated crop height

The UMEP morphometric calculator returns `z0=0` and `zd=0` where its DSM has no
resolved buildings or other 3D objects. These zeros do not represent the real
aerodynamic roughness of soil, grass, wheat, or barley.

Use `--crop-height-schedule` with a CSV containing:

```csv
date,height_m
2025-05-01,0.05
2025-06-01,0.30
2025-07-01,0.75
2025-09-01,0.05
```

For every weather timestamp, the generator linearly interpolates crop height
between dated observations. In zero-object direction sectors it uses:

```text
zd = (2/3) * crop height
z0 = max(0.01 m, 0.123 * crop height)
```

Nonzero building/tree sectors from the morphometric file are retained. The
factors and bare-surface minimum can be changed with `--crop-zd-factor`,
`--crop-z0-factor`, and `--minimum-z0`.

An illustrative spring-cereal schedule is included at
`crop_schedules/south_oslo_spring_cereal_example_2025.csv`. It is a starting
assumption, not a site observation or automatic phenology model. Replace its
dates and heights with the actual crop, sowing date, growth observations, and
harvest date. Winter wheat needs a different schedule.

## Important height constraint

`z_m_input` is the sensor height **above ground**, but UMEP internally uses:

```text
effective measurement height = z_m_input - z_d_input
```

The Kljun model also requires the sensor to be above the roughness sublayer:

```text
z_m_input > z_d_input + 12.5 * z_0_input
```

The generator checks these constraints for every hour. In the current Sørås
200 m calculation, the maximum object-sector displacement height is about
0.98 m and maximum object-sector `z0` is about 0.058 m, so a 2 m sensor passes
that sector's roughness-sublayer test. These values are recalculated for every
candidate and depend on tower position, search radius, and raster data.

## Meteorological requirements

The merged-weather workflow calculates or retrieves most required inputs:

- `ustar` from ERA5 surface stress and air density;
- Obukhov length from ERA5 turbulent heat/moisture fluxes, stress,
  temperature, humidity, and pressure;
- boundary-layer height (`h`) from ERA5 `blh`;
- wind direction preferentially from local Frost observations.

`sigv` is the important remaining quantity absent from hourly Frost/ERA5
products. It should ideally come from a sonic anemometer. Until then,
`--sigma-v-ustar-ratio 2.0` is an explicit sensitivity assumption. Direct
weather-file values take precedence when supplied.

## Low-level input-generation usage

Inspect all options:

```powershell
python .\generate_umep_footprint_input.py --help
```

This is a low-level sensitivity example using fixed provisional assumptions.
The batch workflow documented below is preferred for production runs.

```powershell
python .\generate_umep_footprint_input.py `
  --morphology .\morphometric_data\testkanda2_IMPPoint_anisotropic.txt `
  --weather .\era_5_weatherdata\59.66024225482937N10.78266480752292E-2025-sfc.csv `
  --output .\output\umep_source_area_2025.txt `
  --measurement-height 50 `
  --model kljun `
  --friction-velocity 0.30 `
  --sigma-v 0.60 `
  --obukhov 1000000 `
  --boundary-layer-height 1000 `
  --start 2025-01-01 `
  --end 2026-01-01 `
  --porosity 60
```

Here, `ustar = 0.30 m/s`, `sigv = 0.60 m/s`, near-neutral `Obukhov =
1,000,000 m`, and `h = 1,000 m` are deliberately visible assumptions. A
production file should replace them with measured or retrieved time-varying
values. The included raw CSV extends into June 2026 despite its filename, so
the date bounds keep this output to calendar year 2025.

## Downloading additional ERA5 footprint fields

`download_era5_footprint_data.py` downloads the hourly fields needed to derive
ERA5 boundary-layer height, friction velocity, and Obukhov length. It uses the
CDS API credentials in the standard user file:

```text
C:\Users\fnk\.cdsapirc
```

The token is neither read by this project nor included in command-line
arguments; `cdsapi` loads it itself. Install the client once:

```powershell
python -m pip install "cdsapi>=0.7.7"
```

Inspect the request without contacting CDS:

```powershell
python .\download_era5_footprint_data.py --year 2025 --dry-run
```

Download the full year:

```powershell
python .\download_era5_footprint_data.py --year 2025
```

To stay below CDS request-cost limits, a full year is submitted as 12 monthly
requests. Existing monthly files are skipped, so the same command safely
resumes an interrupted download. Use `--overwrite` to replace them, or download
one month only with `--month 1`.

The site coordinates default to `59.66024225482937, 10.78266480752292` and are
snapped to the nearest 0.25-degree ERA5 grid point (`59.75, 10.75`), matching
the existing data. Full-year outputs are:

```text
era_5_weatherdata\era5_footprint_parameters_2025_01.nc
...
era_5_weatherdata\era5_footprint_parameters_2025_12.nc
```

Before the first request, log in to the CDS website, accept the ERA5 dataset
licence, and create `.cdsapirc` from
<https://cds.climate.copernicus.eu/how-to-api>. The download does not provide
`sigv`; that still requires sonic-anemometer data or an explicit approximation.

## Merging local Frost observations with ERA5

`merge_footprint_weather.py` creates the wide hourly weather CSV used by the
UMEP generator. It:

- uses local 10 m wind speed/direction and surface pressure from `SN76914`;
- chooses the best-quality local 2 m temperature from the three nearby stations;
- uses local dew point from `SN17850`, with `SN17853` as fallback;
- uses ERA5 `u10/v10` only for missing or calm local wind;
- retains ERA5 boundary-layer height;
- derives `ustar` and Obukhov length from ERA5 stresses and turbulent fluxes
  using ECMWF's documented equations.

The command is strict by default and waits until all 12 monthly ERA5 files are
present:

```powershell
python .\merge_footprint_weather.py --year 2025
```

For inspection while downloads are still in progress:

```powershell
python .\merge_footprint_weather.py --year 2025 --allow-partial
```

The default output is:

```text
local_weatherdata\merged_footprint_weather_2025.csv
```

The output includes source/provenance columns and `sdfor`. ECMWF recommends
caution when deriving Obukhov length where the standard deviation of filtered
subgrid orography (`sdfor`) is 50 m or greater. The ERA5 grid cell used here is
approximately 53 m, so the merger labels those rows `sdfor_ge_50m` rather than
hiding that limitation.

After all months are downloaded and merged, generate the UMEP input with:

```powershell
python .\generate_umep_footprint_input.py `
  --morphology .\morphometric_data\200m_IMPPoint_anisotropic.txt `
  --crop-height-schedule .\crop_schedules\south_oslo_spring_cereal_example_2025.csv `
  --weather .\local_weatherdata\merged_footprint_weather_2025.csv `
  --output .\output\umep_source_area_crop_2025.txt `
  --measurement-height 2 `
  --model kljun `
  --sigma-v-ustar-ratio 2.0 `
  --start 2025-01-01 `
  --end 2026-01-01 `
  --invalid-row-policy skip
```

Here `ustar`, Obukhov length, boundary-layer height, and wind direction vary
hourly. `--sigma-v-ustar-ratio 2.0` remains an explicit approximation because
hourly Frost/ERA5 data do not contain the high-frequency lateral-wind variance
measured by a sonic anemometer.

For wind-dependent sensitivity assumptions, `--ustar-fraction 0.10` can
replace `--friction-velocity`, and `--sigma-v-ustar-ratio 2.0` can replace
`--sigma-v`. Kljun rejects records where the resulting `ustar <= 0.1 m/s`.

For the Kormann–Meixner option use `--model kormann`; `h` remains a required
output column even though that model does not use it.

## Tests

```powershell
python -m unittest discover -s tests -v
```
## Run the footprint model without QGIS

`run_footprint_standalone.py` runs the Kljun et al. (2015) parameterisation
directly from a generated 13-column UMEP input file. It writes two GeoTIFFs:
footprint density and cumulative contribution percentage. Cells with values
from `1` through `80` form the integrated 80% source area.

Matching `.qml` files are written automatically. QGIS normally applies these
same-basename styles when the GeoTIFFs are added: the percentage raster uses
UMEP's red-to-blue footprint ramp and transparency, while the density raster
uses a transparent heatmap based on its calculated maximum.

Footprints are calculated in parallel using up to four workers by default.
Use `--workers 1` for deterministic sequential benchmarking, or increase the
value cautiously on machines with ample memory.
The terminal reports elapsed seconds and minutes for each footprint run,
each tower, and the complete multi-tower batch.

For rapid tower-location screening, `--resolution 10` uses one quarter as many
grid cells as the 5 m final product. A practical workflow is to compare all
candidate locations at 10 m, then rerun only the shortlist at 5 m.

On Windows, QGIS locks loaded GeoTIFFs. If an existing output cannot be
replaced, the runner preserves it and automatically writes a matched new pair
such as `footprint_run2_density.tif` and `footprint_run2_percent.tif`.

For tower siting, add `--placement-analysis`. The hourly footprints are still
calculated only once, but are accumulated into annual, growing/dormant,
stable/neutral/unstable, and eight wind-sector climatologies. A
`footprint_placement_summary.csv` compares valid hours, captured mass, and 80%
source-area size. The growing season defaults to April–October and can be
changed with `--growing-months`, for example `--growing-months 5-9`.
For multi-tower batch runs, these summaries are also collected into
`placement_comparison.csv` in the batch output directory.

The QGIS percentage style is visually clipped to the 80% cumulative source
area by default, while the GeoTIFF retains all values. Change the display
boundary with `--display-percent 90` (or another value from 1 to 100).

The tower coordinates must use the same projected CRS supplied with `--crs`.
For the current QGIS project (EPSG:25832), a test run using the coordinates
shown in the project is:

```powershell
python .\run_footprint_standalone.py `
  --input .\output\umep_source_area_crop_2025_Jan-Jun.txt `
  --tower-x 599753 `
  --tower-y 6615344 `
  --crs EPSG:25832 `
  --fetch 2000 `
  --resolution 5 `
  --output-prefix .\output\crop_2025_Jan-Jun
```

For a quick ten-hour check, append `--limit 10`. Progress is printed every 50
rows by default; use `--log-every 10` for more frequent messages and `--debug`
for verbose logging. Invalid meteorological rows are skipped and recorded in
`*_qc.csv`; use `--invalid-row-policy error` to stop on the first invalid row.

The standalone calculation uses the time-varying crop/morphometric `z0` and
`zd` already present in the input file. UMEP's footprint-weighted Kanda values
are diagnostic outputs and are not fed back into the same timestep's
footprint, so this runner likewise does not iterate each footprint.

## Batch workflow from DOM/DTM and tower points

`calculate_morphometry.py` reproduces UMEP's point morphometric calculation:
it subtracts the DTM from the DOM, removes objects below 2 m, rotates the
object-height raster for each direction, and applies UMEP's Kanda method 1.
The default search radius is 200 m.

```powershell
python .\calculate_morphometry.py `
  --dom .\maps\dom_soraas_extended.tif `
  --dtm .\maps\dtm_soraas_extended.tif `
  --towers .\maps\Flux_Tower.shp `
  --output-dir .\output\towers
```

The complete multi-tower workflow also generates the meteorological input and
runs the footprint climatology:

```powershell
python .\run_tower_batch.py `
  --dom .\maps\dom_soraas_extended.tif `
  --dtm .\maps\dtm_soraas_extended.tif `
  --towers .\maps\Flux_Tower.shp `
  --weather .\local_weatherdata\merged_footprint_weather_2025.csv `
  --measurement-height 2 `
  --sigma-v-ustar-ratio 2.0 `
  --crop-height-schedule .\crop_schedules\south_oslo_spring_cereal_example_2025.csv `
  --output-dir .\output\towers
```

Tower CSV columns are `id,x,y`, with optional `measurement_height_m`.
Point shapefiles are also accepted. A shapefile with a `.prj` is transformed
to the DOM/DTM coordinate system automatically; CSV coordinates are assumed
to already use the raster CRS. Each tower gets its own morphology, UMEP input,
footprint GeoTIFFs, and QC CSV.
