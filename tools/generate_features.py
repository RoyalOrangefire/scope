#!/usr/bin/env python
import scope
import argparse
import pathlib
import yaml
import os
from tools.get_quad_ids import get_ids_loop, get_field_ids
from scope.fritz import get_lightcurves_via_ids
from scope.utils import (
    TychoBVfromGaia,
    exclude_radius,
    removeHighCadence,
    write_parquet,
)
import numpy as np
from penquins import Kowalski
import pandas as pd
from astropy.coordinates import SkyCoord
import astropy.units as u
from datetime import datetime
from tools.featureGeneration import lcstats, periodsearch, alertstats, external_xmatch
import warnings
from cesium.featurize import time_series, featurize_single_ts
import json


BASE_DIR = pathlib.Path(__file__).parent.parent.absolute()

# setup connection to Kowalski instances
config_path = pathlib.Path(__file__).parent.parent.absolute() / "config.yaml"
with open(config_path) as config_yaml:
    config = yaml.load(config_yaml, Loader=yaml.FullLoader)

# use token specified as env var (if exists)
kowalski_token_env = os.environ.get("KOWALSKI_TOKEN")
kowalski_alt_token_env = os.environ.get("KOWALSKI_ALT_TOKEN")
if (kowalski_token_env is not None) & (kowalski_alt_token_env is not None):
    config["kowalski"]["token"] = kowalski_token_env
    config["kowalski"]["alt_token"] = kowalski_alt_token_env

timeout = config['kowalski']['timeout']

gloria = Kowalski(**config['kowalski'], verbose=False)
melman = Kowalski(
    token=config['kowalski']['token'],
    protocol="https",
    host="melman.caltech.edu",
    port=443,
    timeout=timeout,
)
kowalski = Kowalski(
    token=config['kowalski']['alt_token'],
    protocol="https",
    host="kowalski.caltech.edu",
    port=443,
    timeout=timeout,
)

source_catalog = config['kowalski']['collections']['sources']
alerts_catalog = config['kowalski']['collections']['alerts']
gaia_catalog = config['kowalski']['collections']['gaia']
dmdt_ints = config['feature_generation']['dmdt_ints']
ext_catalog_info = config['feature_generation']['external_catalog_features']
cesium_feature_list = config['feature_generation']['cesium_features']
period_algorithms = config['feature_generation']['period_algorithms']

kowalski_instances = {'kowalski': kowalski, 'gloria': gloria, 'melman': melman}


def drop_close_bright_stars(
    id_dct: dict,
    kowalski_instance: Kowalski,
    catalog: str = gaia_catalog,
    query_radius_arcsec: float = 300.0,
    xmatch_radius_arcsec: float = 2.0,
    limit: int = 10000,
):
    """
    Use Gaia to identify and drop sources that are too close to bright stars

    :param id_dct: one quadrant's worth of id-coordinate pairs (dict)
    :param kowalski_instance: authenticated instance of a Kowalski database
    :param catalog: name of catalog to use [currently only supports Gaia catalogs] (str)
    :param query_radius_arcsec: size of cone search radius to search for bright stars.
        Default is 300 corresponding with approximate maximum from A. Drake's exclusion radius (float)
    :param xmatch_radius_arcsec: size of cone within which to match a queried source with an input source.
        If any sources from the query fall within this cone, the closest one will be matched to the input source and dropped from further bright-star considerations (float)
    :param limit: batch size of kowalski_instance queries (int)

    :return id_dct_keep: dictionary containing subset of input sources far enough away from bright stars
    """

    ids = [x for x in id_dct]
    id_dct_keep = id_dct.copy()

    n_sources = len(id_dct)
    if n_sources % limit != 0:
        n_iterations = n_sources // limit + 1
    else:
        n_iterations = n_sources // limit

    gaia_results_dct = {}

    print(f'Querying {catalog} catalog in batches...')
    for i in range(0, n_iterations):
        print(f"Iteration {i+1} of {n_iterations}...")
        id_slice = [x for x in id_dct.keys()][
            i * limit : min(n_sources, (i + 1) * limit)
        ]

        radec_geojson = np.array(
            [id_dct[x]['radec_geojson']['coordinates'] for x in id_slice]
        ).transpose()

        # Need to add 180 -> no negative RAs
        radec_geojson[0, :] += 180.0

        # Get Gaia EDR3 ID, G mag, BP-RP, and coordinates
        query = {
            "query_type": "cone_search",
            "query": {
                "object_coordinates": {
                    "radec": dict(zip(id_slice, radec_geojson.transpose().tolist())),
                    "cone_search_radius": query_radius_arcsec,
                    "cone_search_unit": 'arcsec',
                },
                "catalogs": {
                    catalog: {
                        # Select sources brighter than G magnitude 13:
                        # -Conversion to Tycho mags only good for G < 13
                        # -Need for exclusion radius only for stars with B <~ 13
                        # -For most stars, if G > 13, B > 13
                        "filter": {"phot_g_mean_mag": {"$lt": 13.0}},
                        "projection": {
                            "phot_g_mean_mag": 1,
                            "bp_rp": 1,
                            "coordinates.radec_geojson.coordinates": 1,
                        },
                    }
                },
                "filter": {},
            },
        }

        q = kowalski_instance.query(query)
        gaia_results = q['data'][catalog]
        gaia_results_dct.update(gaia_results)

    print('Identifying sources too close to bright stars...')
    # Loop over each id to compare with query results
    count = 0
    for id in ids:
        count += 1
        if count % limit == 0:
            print(f"{count} done")
        if count == len(ids):
            print(f"{count} done")

        val = id_dct[id]

        ra_geojson, dec_geojson = val['radec_geojson']['coordinates']

        single_result = gaia_results_dct[str(id)]
        if len(single_result) > 0:
            coords = np.array(
                [
                    x['coordinates']['radec_geojson']['coordinates']
                    for x in single_result
                ]
            )
            coords[:, 0] += 180.0

            # SkyCoord object for query results
            Coords = SkyCoord(coords, unit=['deg', 'deg'])
            # SkyCoord object for input source
            coord = SkyCoord(ra_geojson + 180.0, dec_geojson, unit=['deg', 'deg'])

            all_separations = Coords.separation(coord)
            # Identify closest source to input
            drop_source = np.argmin(all_separations)

            # If closest source is within specified radius, treat it as the input source and drop it from further consideration
            xmatch_source = {}
            if all_separations[drop_source] < xmatch_radius_arcsec * u.arcsec:
                xmatch_source = single_result.pop(drop_source)
                Coords = np.delete(Coords, drop_source)

            # If possible, use all-Gaia coordinates for next step
            if len(xmatch_source) > 0:
                xmatch_ra, xmatch_dec = xmatch_source['coordinates']['radec_geojson'][
                    'coordinates'
                ]
                xmatch_ra += 180.0
                xmatch_coord = SkyCoord(xmatch_ra, xmatch_dec, unit=['deg', 'deg'])
            else:
                xmatch_coord = SkyCoord(
                    ra_geojson + 180.0, dec_geojson, unit=['deg', 'deg']
                )

            # Use mapping from Gaia -> Tycho to set exclusion radius for each source
            for idx, source in enumerate(single_result):
                try:
                    B, V = TychoBVfromGaia(source['phot_g_mean_mag'], source['bp_rp'])
                    excl_radius = exclude_radius(B, V)
                except KeyError:
                    # Not all Gaia sources have BP-RP
                    excl_radius = 0.0
                if excl_radius > 0.0:
                    sep = xmatch_coord.separation(Coords[idx])
                    if excl_radius * u.arcsec > sep.to(u.arcsec):
                        # If there is a bright star that's too close, drop from returned dict
                        id_dct_keep.pop(id)
                        break

    print(f"Dropped {len(id_dct) - len(id_dct_keep)} sources.")
    return id_dct_keep


def generate_features(
    source_catalog: str = source_catalog,
    alerts_catalog: str = alerts_catalog,
    gaia_catalog: str = gaia_catalog,
    bright_star_query_radius_arcsec: float = 300.0,
    xmatch_radius_arcsec: float = 2.0,
    kowalski_instances: dict = kowalski_instances,
    limit: int = 10000,
    period_algorithms: list = ['LS'],
    period_batch_size: int = 1,
    doCPU: bool = False,
    doGPU: bool = False,
    samples_per_peak: int = 10,
    doLongPeriod: bool = False,
    doRemoveTerrestrial: bool = False,
    doParallel: bool = False,
    Ncore: int = 8,
    field: int = 296,
    ccd: int = 1,
    quad: int = 1,
    min_n_lc_points: int = 50,
    min_cadence_minutes: float = 30.0,
    dirname: str = 'generated_features',
    filename: str = 'gen_features',
    doCesium: bool = False,
    doNotSave: bool = False,
    stop_early: bool = False,
    doQuadrantFile: bool = False,
    quadrant_file: str = 'slurm.dat',
    quadrant_index: int = 0,
):

    # Get code version and current date/time for metadata
    code_version = scope.__version__
    utcnow = datetime.utcnow()
    start_dt = utcnow.strftime("%Y-%m-%d %H:%M:%S")

    # Code supporting parallelization across fields/ccds/quads
    slurmDir = os.path.join(str(BASE_DIR / dirname), 'slurm')
    if doQuadrantFile:
        names = ["job_number", "field", "ccd", "quadrant"]
        df_original = pd.read_csv(
            os.path.join(slurmDir, quadrant_file),
            header=None,
            delimiter=' ',
            names=names,
        )
        row = df_original.iloc[quadrant_index, :]
        field, ccd, quad = int(row["field"]), int(row["ccd"]), int(row["quadrant"])

    print(f'Running field {field}, CCD {ccd}, Quadrant {quad}...')

    print('Getting IDs...')
    _, lst = get_ids_loop(
        get_field_ids,
        catalog=source_catalog,
        kowalski_instance=kowalski_instances['melman'],
        limit=limit,
        field=field,
        ccd_range=ccd,
        quad_range=quad,
        minobs=0,
        save=False,
        get_coords=True,
        stop_early=stop_early,
    )

    # Each index of lst corresponds to a different ccd/quad combo
    feature_gen_source_list = drop_close_bright_stars(
        lst[0],
        kowalski_instance=kowalski_instances['gloria'],
        catalog=gaia_catalog,
        query_radius_arcsec=bright_star_query_radius_arcsec,
        xmatch_radius_arcsec=xmatch_radius_arcsec,
        limit=limit,
    )

    print('Getting lightcurves...')
    lcs = get_lightcurves_via_ids(
        kowalski_instance=kowalski_instances['melman'],
        ids=[x for x in feature_gen_source_list.keys()],
        catalog=source_catalog,
        limit_per_query=limit,
    )

    feature_dict = feature_gen_source_list.copy()
    print('Analyzing lightcuves and computing basic features...')
    # Start by dropping flagged points
    count = 0
    baseline = 0
    keep_id_list = []
    tme_collection = []
    for idx, lc in enumerate(lcs):
        count += 1
        if (idx + 1) % limit == 0:
            print(f"{count} done")
        if count == len(lcs):
            print(f"{count} done")
        _id = lc['_id']
        lc_unflagged = [x for x in lc['data'] if x['catflags'] == 0]
        flt = lc['filter']

        tme = [[x['hjd'], x['mag'], x['magerr']] for x in lc_unflagged]
        try:
            tme_arr = np.array(tme)
            t, m, e = tme_arr.transpose()

            # Remove all but the first of each group of high-cadence points
            tt, mm, ee = removeHighCadence(t, m, e, cadence_minutes=min_cadence_minutes)

            # Discard sources lacking minimum number of points
            if len(tt) < min_n_lc_points:
                feature_dict.pop(_id)
            else:
                keep_id_list += [_id]
                if doCesium:
                    cesium_TS = time_series.TimeSeries(tt, mm, ee)

                # Determine largest time baseline over loop
                new_baseline = max(tt) - min(tt)
                if new_baseline > baseline:
                    baseline = new_baseline

                new_tme_arr = np.array([tt, mm, ee])
                tme_collection += [new_tme_arr]

                # Add basic info
                feature_dict[_id]['ra'] = (
                    feature_gen_source_list[_id]['radec_geojson']['coordinates'][0]
                    + 180.0
                )
                feature_dict[_id]['dec'] = feature_gen_source_list[_id][
                    'radec_geojson'
                ]['coordinates'][1]
                feature_dict[_id]['field'] = field
                feature_dict[_id]['ccd'] = ccd
                feature_dict[_id]['quad'] = quad
                feature_dict[_id]['filter'] = flt

                # Begin generating features - start with basic stats
                (
                    N,
                    median,
                    wmean,
                    chi2red,
                    RoMS,
                    wstd,
                    NormPeaktoPeakamp,
                    NormExcessVar,
                    medianAbsDev,
                    iqr,
                    i60r,
                    i70r,
                    i80r,
                    i90r,
                    skew,
                    smallkurt,
                    invNeumann,
                    WelchI,
                    StetsonJ,
                    StetsonK,
                    AD,
                    SW,
                ) = lcstats.calc_basic_stats(tt, mm, ee)

                feature_dict[_id]['n'] = N
                feature_dict[_id]['median'] = median
                feature_dict[_id]['wmean'] = wmean
                feature_dict[_id]['chi2red'] = chi2red
                feature_dict[_id]['roms'] = RoMS
                feature_dict[_id]['wstd'] = wstd
                feature_dict[_id]['norm_peak_to_peak_amp'] = NormPeaktoPeakamp
                feature_dict[_id]['norm_excess_var'] = NormExcessVar
                feature_dict[_id]['median_abs_dev'] = medianAbsDev
                feature_dict[_id]['iqr'] = iqr
                feature_dict[_id]['i60r'] = i60r
                feature_dict[_id]['i70r'] = i70r
                feature_dict[_id]['i80r'] = i80r
                feature_dict[_id]['i90r'] = i90r
                feature_dict[_id]['skew'] = skew
                feature_dict[_id]['smallkurt'] = smallkurt
                feature_dict[_id]['inv_vonneumannratio'] = invNeumann
                feature_dict[_id]['welch_i'] = WelchI
                feature_dict[_id]['stetson_j'] = StetsonJ
                feature_dict[_id]['stetson_k'] = StetsonK
                feature_dict[_id]['ad'] = AD
                feature_dict[_id]['sw'] = SW

                if doCesium:
                    cesium_features = featurize_single_ts(
                        cesium_TS, cesium_feature_list
                    )
                    cesium_features_dict = (
                        cesium_features.reset_index()
                        .drop('channel', axis=1)
                        .set_index('feature')
                        .to_dict()[0]
                    )
                    feature_dict[_id].update(cesium_features_dict)

        except ValueError:
            feature_dict.pop(_id)

    if baseline > 0:
        # Define frequency grid using largest LC time baseline
        if doLongPeriod:
            fmin, fmax = 2 / baseline, 48
        else:
            fmin, fmax = 2 / baseline, 480

        df = 1.0 / (samples_per_peak * baseline)
        nf = int(np.ceil((fmax - fmin) / df))
        freqs = fmin + df * np.arange(nf)

        # Define terrestrial frequencies to remove
        if doRemoveTerrestrial:
            freqs_to_remove = [
                [3e-2, 4e-2],
                [3.95, 4.05],
                [2.95, 3.05],
                [1.95, 2.05],
                [0.95, 1.05],
                [0.48, 0.52],
                [0.32, 0.34],
            ]
        else:
            freqs_to_remove = None

        # Continue with periodsearch/periodfind
        period_dict = {}
        significance_dict = {}
        pdot_dict = {}
        if doCPU or doGPU:
            if doCPU and doGPU:
                raise KeyError('Please set only one of -doCPU or -doGPU.')
            for algorithm in period_algorithms:
                print(f'Running {algorithm} algorithm...')
                periods, significances, pdots = periodsearch.find_periods(
                    algorithm,
                    tme_collection,
                    freqs,
                    batch_size=period_batch_size,
                    doGPU=doGPU,
                    doCPU=doCPU,
                    doSaveMemory=False,
                    doRemoveTerrestrial=doRemoveTerrestrial,
                    doUsePDot=False,
                    doSingleTimeSegment=False,
                    freqs_to_remove=freqs_to_remove,
                    phase_bins=20,
                    mag_bins=10,
                    doParallel=doParallel,
                    Ncore=Ncore,
                )

                period_dict[algorithm] = periods
                significance_dict[algorithm] = significances
                pdot_dict[algorithm] = pdots
        else:
            warnings.warn("Skipping period finding; setting all periods to 1.0 d.")
            # Default periods 1.0 d
            period_algorithms = ['Ones']
            period_dict['Ones'] = np.ones(len(tme_collection))
            significance_dict['Ones'] = np.ones(len(tme_collection))
            pdot_dict['Ones'] = np.ones(len(tme_collection))

        print(f'Computing Fourier stats for {len(period_dict)} algorithms...')
        for algorithm in period_algorithms:
            print(f'- Algorithm: {algorithm}')
            count = 0
            for idx, _id in enumerate(keep_id_list):
                count += 1
                if (idx + 1) % limit == 0:
                    print(f"{count} done")
                if count == len(keep_id_list):
                    print(f"{count} sources meeting min_n_lc_points requirement done")

                period = period_dict[algorithm][idx]
                significance = significance_dict[algorithm][idx]
                pdot = pdot_dict[algorithm][idx]
                tt, mm, ee = tme_collection[idx]

                # Compute Fourier stats
                (
                    f1_power,
                    f1_BIC,
                    f1_a,
                    f1_b,
                    f1_amp,
                    f1_phi0,
                    f1_relamp1,
                    f1_relphi1,
                    f1_relamp2,
                    f1_relphi2,
                    f1_relamp3,
                    f1_relphi3,
                    f1_relamp4,
                    f1_relphi4,
                ) = lcstats.calc_fourier_stats(tt, mm, ee, period)

                feature_dict[_id][f'f1_power_{algorithm}'] = f1_power
                feature_dict[_id][f'f1_BIC_{algorithm}'] = f1_BIC
                feature_dict[_id][f'f1_a_{algorithm}'] = f1_a
                feature_dict[_id][f'f1_b_{algorithm}'] = f1_b
                feature_dict[_id][f'f1_amp_{algorithm}'] = f1_amp
                feature_dict[_id][f'f1_phi0_{algorithm}'] = f1_phi0
                feature_dict[_id][f'f1_relamp1_{algorithm}'] = f1_relamp1
                feature_dict[_id][f'f1_relphi1_{algorithm}'] = f1_relphi1
                feature_dict[_id][f'f1_relamp2_{algorithm}'] = f1_relamp2
                feature_dict[_id][f'f1_relphi2_{algorithm}'] = f1_relphi2
                feature_dict[_id][f'f1_relamp3_{algorithm}'] = f1_relamp3
                feature_dict[_id][f'f1_relphi3_{algorithm}'] = f1_relphi3
                feature_dict[_id][f'f1_relamp4_{algorithm}'] = f1_relamp4
                feature_dict[_id][f'f1_relphi4_{algorithm}'] = f1_relphi4

                feature_dict[_id][f'period_{algorithm}'] = period
                feature_dict[_id][f'significance_{algorithm}'] = significance
                feature_dict[_id][f'pdot_{algorithm}'] = pdot

        print('Computing dmdt histograms...')
        count = 0
        for idx, _id in enumerate(keep_id_list):
            count += 1
            if (idx + 1) % limit == 0:
                print(f"{count} done")
            if count == len(keep_id_list):
                print(f"{count} done")

            tt, mm, _ = tme_collection[idx]
            dmdt = lcstats.compute_dmdt(tt, mm, dmdt_ints)
            feature_dict[_id]['dmdt'] = dmdt.tolist()

        # Get ZTF alert stats
        alert_stats_dct = alertstats.get_ztf_alert_stats(
            feature_dict,
            kowalski_instance=kowalski_instances['kowalski'],
            radius_arcsec=xmatch_radius_arcsec,
            limit=limit,
        )
        for _id in feature_dict.keys():
            feature_dict[_id]['n_ztf_alerts'] = alert_stats_dct[_id]['n_ztf_alerts']
            feature_dict[_id]['mean_ztf_alert_braai'] = alert_stats_dct[_id][
                'mean_ztf_alert_braai'
            ]

        # Add crossmatches to Gaia, AllWISE and PS1 (by default, see config.yaml)
        feature_dict = external_xmatch.xmatch(
            feature_dict,
            kowalski_instances['gloria'],
            catalog_info=ext_catalog_info,
            radius_arcsec=xmatch_radius_arcsec,
            limit=limit,
        )
        feature_df = pd.DataFrame.from_dict(feature_dict, orient='index')

        # Convert various _id datatypes to Int64
        colnames = [x for x in feature_df.columns]
        for col in colnames:
            if '_id' in col:
                feature_df[col] = feature_df[col].astype("Int64")

    else:
        # If baseline is still zero, then no light curves met the selection criteria
        # Generate an empty DF instead so this field/ccd/quad is treated as done
        print('No light curves meet selection criteria.')
        feature_df = pd.DataFrame()

    utcnow = datetime.utcnow()
    end_dt = utcnow.strftime("%Y-%m-%d %H:%M:%S")

    # Add metadata
    feature_df.attrs['scope_code_version'] = code_version
    feature_df.attrs['feature_generation_start_dateTime_utc'] = start_dt
    feature_df.attrs['feature_generation_end_dateTime_utc'] = end_dt
    feature_df.attrs['ZTF_source_catalog'] = source_catalog
    feature_df.attrs['ZTF_alerts_catalog'] = alerts_catalog
    feature_df.attrs['Gaia_catalog'] = gaia_catalog

    # Write results
    if not doNotSave:
        filename += f"_field_{field}_ccd_{ccd}_quad_{quad}"
        filename += '.parquet'
        dirpath = BASE_DIR / dirname / f"field_{field}"
        os.makedirs(dirpath, exist_ok=True)

        source_count = len(feature_df)
        meta_dct = {}
        meta_dct["(field, ccd, quad)"] = {
            f"({field}, {ccd}, {quad})": {
                "minobs": min_n_lc_points,
                "start_time_utc": start_dt,
                "end_time_utc": end_dt,
                "ZTF_source_catalog": source_catalog,
                "ZTF_alerts_catalog": alerts_catalog,
                "Gaia_catalog": gaia_catalog,
                "total": source_count,
            }
        }

        meta_filename = BASE_DIR / dirname / "meta.json"

        if os.path.exists(meta_filename):
            with open(meta_filename, 'r') as f:
                dct = json.load(f)
                dct["(field, ccd, quad)"].update(meta_dct["(field, ccd, quad)"])
                meta_dct = dct

        with open(meta_filename, 'w') as f:
            try:
                json.dump(meta_dct, f)
            except Exception as e:
                print("error dumping to json, message: ", e)

        filepath = dirpath / filename
        write_parquet(feature_df, str(filepath))
        print(f"Wrote features for {source_count} sources.")

    else:
        print(f"Generated features for {len(feature_df)} sources.")

    return feature_df


if __name__ == "__main__":

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--source_catalog",
        default=source_catalog,
        help="name of source collection on Kowalski",
    )
    parser.add_argument(
        "--alerts_catalog",
        default=alerts_catalog,
        help="name of alerts collection on Kowalski",
    )
    parser.add_argument(
        "--gaia_catalog",
        default=gaia_catalog,
        help="name of Gaia collection on Kowalski",
    )
    parser.add_argument(
        "--bright_star_query_radius_arcsec",
        type=float,
        default=300.0,
        help="size of cone search radius to search for bright stars",
    )
    parser.add_argument(
        "--xmatch_radius_arcsec",
        type=float,
        default=2.0,
        help="cone radius for all crossmatches",
    )
    parser.add_argument(
        "--query_size_limit",
        type=int,
        default=10000,
        help="sources per query limit for large Kowalski queries",
    )
    parser.add_argument(
        "--period_algorithms",
        nargs='+',
        default=period_algorithms,
        help="algorithms in periodsearch.py to use for period-finding",
    )
    parser.add_argument(
        "--period_batch_size",
        type=int,
        default=1,
        help="batch size for GPU-accelerated period algorithms",
    )
    parser.add_argument(
        "--doCPU",
        action='store_true',
        default=False,
        help="if set, run period-finding algorithm on CPU",
    )
    parser.add_argument(
        "--doGPU",
        action='store_true',
        default=False,
        help="if set, use GPU-accelerated period algorithm",
    )
    parser.add_argument(
        "--samples_per_peak",
        default=10,
        type=int,
    )
    parser.add_argument(
        "--doLongPeriod",
        action='store_true',
        default=False,
        help="if set, optimize frequency grid for long periods",
    )
    parser.add_argument(
        "--doRemoveTerrestrial",
        action='store_true',
        default=False,
        help="if set, remove terrestrial frequencies from period analysis",
    )
    parser.add_argument(
        "--doParallel",
        action="store_true",
        default=False,
        help="If set, parallelize period finding",
    )
    parser.add_argument(
        "--Ncore",
        default=8,
        type=int,
        help="number of cores for parallel period finding",
    )
    parser.add_argument(
        "--field",
        type=int,
        default=296,
        help="if not -doAllFields, ZTF field to run on",
    )
    parser.add_argument(
        "--ccd", type=int, default=1, help="if not -doAllCCDs, ZTF ccd to run on"
    )
    parser.add_argument(
        "--quad", type=int, default=1, help="if not -doAllQuads, ZTF field to run on"
    )
    parser.add_argument(
        "--min_n_lc_points",
        type=int,
        default=50,
        help="minimum number of unflagged light curve points to run feature generation",
    )
    parser.add_argument(
        "--min_cadence_minutes",
        type=float,
        default=30.0,
        help="minimum cadence (in minutes) between light curve points. For groups of points closer together than this value, only the first will be kept.",
    )
    parser.add_argument(
        "--dirname",
        type=str,
        default='generated_features',
        help="Directory name for generated features",
    )
    parser.add_argument(
        "--filename",
        type=str,
        default='gen_features',
        help="Prefix for generated feature file",
    )
    parser.add_argument(
        "--doCesium",
        action='store_true',
        default=False,
        help="if set, use Cesium to generate additional features specified in config",
    )
    parser.add_argument(
        "--doNotSave",
        action='store_true',
        default=False,
        help="if set, do not save features",
    )
    parser.add_argument(
        "--stop_early",
        action='store_true',
        default=False,
        help="if set, stop when number of sources reaches query_size_limit. Helpful for testing on small samples.",
    )
    parser.add_argument("--doQuadrantFile", action="store_true", default=False)
    parser.add_argument("--quadrant_file", default="slurm.dat")
    parser.add_argument("--quadrant_index", default=0, type=int)

    args = parser.parse_args()

    if type(args.period_algorithms) == dict:
        if args.doCPU:
            args.period_algorithms = args.period_algorithms['CPU']
        elif args.doGPU:
            args.period_algorithms = args.period_algorithms['GPU']

    # call generate_features
    generate_features(
        source_catalog=args.source_catalog,
        alerts_catalog=args.alerts_catalog,
        gaia_catalog=args.gaia_catalog,
        bright_star_query_radius_arcsec=args.bright_star_query_radius_arcsec,
        xmatch_radius_arcsec=args.xmatch_radius_arcsec,
        limit=args.query_size_limit,
        period_algorithms=args.period_algorithms,
        period_batch_size=args.period_batch_size,
        doCPU=args.doCPU,
        doGPU=args.doGPU,
        samples_per_peak=args.samples_per_peak,
        doLongPeriod=args.doLongPeriod,
        doRemoveTerrestrial=args.doRemoveTerrestrial,
        doParallel=args.doParallel,
        Ncore=args.Ncore,
        field=args.field,
        ccd=args.ccd,
        quad=args.quad,
        min_n_lc_points=args.min_n_lc_points,
        min_cadence_minutes=args.min_cadence_minutes,
        dirname=args.dirname,
        filename=args.filename,
        doCesium=args.doCesium,
        doNotSave=args.doNotSave,
        stop_early=args.stop_early,
        doQuadrantFile=args.doQuadrantFile,
        quadrant_file=args.quadrant_file,
        quadrant_index=args.quadrant_index,
    )