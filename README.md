# UMEP Source Area input generator

This project converts directional output from UMEP's **Morphometric
Calculator (Point)** and time-series weather data into the 13-column input
format used by **Urban Morphology: Source Area (Point)**:

```text
iy id it imin z_0_input z_d_input z_m_input sigv Obukhov ustar dir h por
```

The script uses the wind direction for each weather record to circularly
interpolate `z0` and `zd` from the anisotropic morphometric file. It accepts
either comma-separated weather data (including ERA5 `valid_time,u10,v10,...`)
or UMEP-style whitespace-separated weather data.

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

Consequently, a sensor at 2 m is not valid with the included morphology:
directional displacement heights are mostly around 8–14 m. The Kljun model
also requires the sensor to be above the roughness sublayer:

```text
z_m_input > z_d_input + 12.5 * z_0_input
```

The generator checks these constraints and does not write an invalid file.
If "2 m" means 2 m above a roof or tower platform, pass the total sensor
height above local ground—not 2 m.

## Meteorological requirements

The Source Area model needs values that cannot be recovered from hourly mean
10 m wind alone:

- lateral wind standard deviation (`sigv`), ideally from the sonic anemometer;
- friction velocity (`ustar`), ideally from eddy-covariance data;
- Obukhov length, ideally from eddy-covariance data or suitable ERA5 flux and
  stress fields;
- boundary-layer height (`h`) for Kljun, available as ERA5 `blh`.

If the weather file contains columns named `sigv`, `ustar`, `Obukhov`, and
`blh`/`h`, they are used directly. Otherwise an explicit fallback must be
supplied. Fallbacks are useful for sensitivity tests, not as observational
substitutes.

## Usage

Inspect all options:

```powershell
python .\generate_umep_footprint_input.py --help
```

Example using explicit, provisional assumptions with the included raw ERA5
CSV (replace `50` with the actual sensor height above ground):

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

For the 200 m agricultural morphology and a 2 m sensor:

```powershell
python .\generate_umep_footprint_input.py `
  --morphology .\morphometric_data\200m_IMPPoint_anisotropic.txt `
  --crop-height-schedule .\crop_schedules\south_oslo_spring_cereal_example_2025.csv `
  --weather .\era_5_weatherdata\59.66024225482937N10.78266480752292E-2025-sfc.csv `
  --output .\output\umep_source_area_crop_2025.txt `
  --measurement-height 2 `
  --model kljun `
  --friction-velocity 0.30 `
  --sigma-v 0.60 `
  --obukhov 1000000 `
  --boundary-layer-height 1000 `
  --start 2025-01-01 `
  --end 2026-01-01 `
  --invalid-row-policy skip
```

The nonzero northeast sector in the included 200 m morphology has
`zd=2.27 m`, above the 2 m sensor. Weather records whose footprint uses that
obstructed sector fail physical validation. `--invalid-row-policy skip`
omits and counts these records rather than disguising the sector as crop.
Without this option, the default policy stops with an error.

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

For rapid tower-location screening, `--resolution 10` uses one quarter as many
grid cells as the 5 m final product. A practical workflow is to compare all
candidate locations at 10 m, then rerun only the shortlist at 5 m.

On Windows, QGIS locks loaded GeoTIFFs. If an existing output cannot be
replaced, the runner preserves it and automatically writes a matched new pair
such as `footprint_run2_density.tif` and `footprint_run2_percent.tif`.

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
