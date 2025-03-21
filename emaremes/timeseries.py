import gzip
from typing import Mapping

from pathlib import Path
from multiprocessing import Pool
from tempfile import NamedTemporaryFile

import numpy as np
import pandas as pd
import geopandas as gpd
import xarray as xr

from shapely.geometry import Point
from shapely.affinity import translate

from .utils import Extent


def _extract_point_from_grib2_file(f: Path, lat: float, lon: float) -> tuple[pd.Timestamp, float]:
    """
    Extracts the nearest value of a grib2 file provided a latitude and longitude.
    This is a helper function for `extract_point_value`.

    Parameters
    ----------
    f : Path
        Path to the grib2 file.
    lat : float
        Latitude to extract the value from.
    lon : float
        Longitude to extract the value from.

    Returns
    -------
    tuple[pd.Timestamp, float]
        A tuple with the timestamp and value of the point.
    """
    with xr.open_dataset(f, engine="cfgrib", decode_timedelta=False) as ds:
        time = ds.time.values.copy()
        val = ds.sel(latitude=lat, longitude=lon, method="nearest")["unknown"].values.copy()

    return pd.Timestamp(time), val


def _extract_point_from_zipped_file(f: Path, lat: float, lon: float) -> tuple[pd.Timestamp, float]:
    """
    Extracts the nearest value of a gzipped grib2 file provided a latitude and longitude.
    This just deflates the file and calls `_extract_point_from_grib2_file`.

    Parameters
    ----------
    f : Path
        Path to the gzipped grib2 file.

    Returns
    -------
    tuple[pd.Timestamp, float]
        A tuple with the timestamp and value of the point.
    """
    with gzip.open(f, "rb") as gzip_file_in:
        with NamedTemporaryFile("ab+", suffix=".grib2") as tf:
            unzipped_bytes = gzip_file_in.read()
            tf.write(unzipped_bytes)
            time, val = _extract_point_from_grib2_file(tf.name, lat, lon)

    return time, val


def extract_point_value(f: Path, lat: float, lon: float) -> tuple[pd.Timestamp, float]:
    """
    Extracts the nearest value of a grib2 file provided a latitude and longitude.

    Parameters
    ----------
    f : Path
        Path to the grib2 file.
    lat : float
        Latitude to extract the value from.
    lon : float
        Longitude to extract the value from.

    Returns
    -------
    tuple[pd.Timestamp, float]
        A tuple with the timestamp and value of the point.
    """
    if f.suffix == ".grib2":
        time, val = _extract_point_from_grib2_file(f, lat, lon)

    elif f.suffix == ".gz":
        time, val = _extract_point_from_zipped_file(f, lat, lon)

    else:
        raise ValueError("File is not `.gz` nor `.grib2`")

    return time, val


def extract_point_series(files: list[Path], lat: float, lon: float) -> pd.DataFrame:
    """
    Parallelizes the extraction of point values from grib2 files. For a large number of files,
    this can be much faster than using `xr.open_mfdataset`.

    Parameters
    ----------
    files : list[Path]
        List of grib2 files to extract the point value from.
    lat : float
        Latitude of the point to extract the value from.
    lon : float
        Longitude of the point to extract the value from.

    Returns
    -------
    pd.DataFrame
        A pandas DataFrame with the timestamps and values of the point.
    """
    with Pool() as pool:
        query = pool.starmap(extract_point_value, [(f, lat, lon) for f in files])

    df = pd.DataFrame(
        {
            "timestamp": [q[0] for q in query],
            "value": [q[1] for q in query],
        },
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["value"] = df["value"].astype(float)
    df.set_index("timestamp", inplace=True)

    return df


def extract_masked_value(
    file: Path,
    mask: np.ndarray,
    extent: Extent,
    variable: str = "unknown",
    upsample_coords: Mapping[str, np.ndarray] | None = None,
):
    """
    Extracts the mean value of a grib2 file provided a mask and an extent.

    Parameters
    ----------
    file : Path
        Path to the grib2 file.
    mask : np.ndarray
        Mask to apply to the grib2 file.
    extent : Extent
        Extent to clip the grib2 file to.
    variable : str, optional
        Variable to extract from the grib2 file, by default "unknown" which
        represents precipitation intensity in mm/h.
    upsample_coords : Mapping[str, np.ndarray] | None, optional
        Coordinates to upsample the data to, by default None.

    Returns
    -------
    tuple[pd.Timestamp, float]
        A tuple with the timestamp and value of the polygon.
    """

    with xr.open_dataset(file, engine="cfgrib", decode_timedelta=True) as ds:
        # Open file and do a coarse clip
        time = ds.time.values.copy()
        xclip = ds.loc[extent.as_xr_slice()]

        # Upscaling helper
        if upsample_coords:
            upsample = xclip.interp(coords=upsample_coords, method="nearest")
        else:
            upsample = xclip

        mask_ds = upsample.where(mask)

        # Actually access the files and extract the data
        mean_precip = mask_ds[variable].mean(dim=["longitude", "latitude"])
        metric = mean_precip.values.copy()

    return time, metric


def extract_polygon_series(
    files: list[Path],
    polygon: gpd.GeoSeries,
    upsample: bool = False,
) -> pd.DataFrame:
    """
    Parallelizes the extraction of polygon values from grib2 files. For a large number of files,
    this can be much faster than using `xr.open_mfdataset`.

    Parameters
    ----------
    files : list[Path]
        List of grib2 files to extract the polygon value from.
    polygon : gpd.GeoSeries
        Polygon to extract the value from.
    upsample : bool, optional
        Whether to upsample the data to a finer grid, by default False.
    """
    if len(polygon) != 1:
        raise ValueError("Only one polygon is supported")

    # Figure out the extent of first clip
    polygon_reproj = polygon.to_crs("4326")
    bounds = polygon_reproj.bounds
    extent = Extent((bounds.miny[0], bounds.maxy[0]), (bounds.minx[0], bounds.maxx[0]))

    translated_polygon = translate(polygon_reproj.geometry[0], xoff=360)

    # Generate mask from first GRIB file
    with xr.open_dataset(files[0], engine="cfgrib", decode_timedelta=True) as ds:
        xclip = ds.loc[extent.as_xr_slice()]

        # Generate points to evaluate
        lon, lat = xclip.longitude.values, xclip.latitude.values

        if upsample:
            llon = np.linspace(min(lon), max(lon), num=4 * len(lon) - 1)
            llat = np.linspace(min(lat), max(lat), num=4 * len(lat) - 1)

        else:
            llon, llat = lon, lat

        mlon, mlat = np.meshgrid(llon, llat)
        points = np.vstack((mlon.flatten(), mlat.flatten())).T

        # Mask using the polygon.contains calculation
        mask = [translated_polygon.contains(Point(x, y)) for x, y in points]
        mask = np.array(mask).reshape(len(llat), len(llon))

    # Query all GRIB files

    with Pool() as pool:
        if upsample:
            upsample_coords = {"longitude": llon, "latitude": llat}

            query = pool.starmap(
                extract_masked_value, [(f, mask, extent, upsample_coords) for f in files]
            )

        else:
            query = pool.starmap(extract_masked_value, [(f, mask, extent) for f in files])

    df = pd.DataFrame(
        {
            "timestamp": [q[0] for q in query],
            "value": [q[1] for q in query],
        }
    )

    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["value"] = df["value"].astype(float)
    df.set_index("timestamp", inplace=True)

    return df
