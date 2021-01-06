"""Datastore manages file retrieval for PUDL datasets."""

import argparse
import copy
import hashlib
import json
import logging
import re
import sys
import zipfile
import io
from pathlib import Path
from typing import Dict, Iterator, Optional, Any, List, Tuple, NamedTuple

import coloredlogs
import datapackage
import requests
import yaml
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from pudl.workspace import resource_cache
from pudl.workspace.resource_cache import PudlResourceKey

logger = logging.getLogger(__name__)

# The Zenodo tokens recorded here should have read-only access to our archives.
# Including them here is correct in order to allow public use of this tool, so
# long as we stick to read-only keys.




PUDL_YML = Path.home() / ".pudl.yml"


class DatapackageDescriptor:
    """A simple wrapper providing access to datapackage.json."""
    """An abstract representation of the datapackage resources."""
    def __init__(self, datapackage_json: dict, dataset: str, doi: str):
        self.datapackage_json = datapackage_json
        self.dataset = dataset
        self.doi = doi 
        self._validate_datapackage(datapackage_json)

    def get_resource_path(self, name: str) -> str:
        """Returns zenodo url that holds contents of given named resource."""
        for res in self.datapackage_json["resources"]:
            if res["name"] == name:
                # remote_url is sometimes set on the local cached version of datapackage.json
                # so we should be using that if it exists.
                return res.get("remote_url") or res.get("path")
        raise KeyError(f"Resource {name} not found for {self.dataset}/{self.doi}")

    def _matches(self, res: dict, **filters: Any):
        parts = res.get('parts', {})
        return all(str(parts.get(k)) == str(v) for k, v in filters.items())

    def get_resources(self, name: str = None, **filters: Any) -> Iterator[PudlResourceKey]:
        for res in self.datapackage_json["resources"]:
            if name and res["name"] != name:
                continue
            if self._matches(res, **filters):
                yield PudlResourceKey(
                    dataset=self.dataset,
                    doi=self.doi,
                    name=res["name"])

    def _validate_datapackage(self, datapackage_json: dict):
        """Checks the correctness of datapackage.json metadata. Throws ValueError if invalid."""
        dp = datapackage.Package(datapackage_json)
        if not dp.valid:
            msg = f"Found {len(dp.errors)} datapackage validation errors:\n"
            for e in dp.errors:
                msg = msg + f"  * {e}\n"
            raise ValueError(msg)

    def get_json_string(self) -> str:
        """Exports the underlying json as normalized (sorted, indented) json string."""
        return json.dumps(self.datapackage_json, sort_keys=True, indent=4)


class ZenodoFetcher:
    """API for fetching datapackage descriptors and resource contents from zenodo."""

    TOKEN = {
        # Read-only personal access tokens for pudl@catalyst.coop:
        "sandbox": "qyPC29wGPaflUUVAv1oGw99ytwBqwEEdwi4NuUrpwc3xUcEwbmuB4emwysco",
        "production": "KXcG5s9TqeuPh1Ukt5QYbzhCElp9LxuqAuiwdqHP0WS4qGIQiydHn6FBtdJ5"
    }

    DOI = {
        "sandbox": {
            "censusdp1tract": "10.5072/zenodo.674992",
            "eia860": "10.5072/zenodo.672210",
            "eia860m": "10.5072/zenodo.692655",
            "eia861": "10.5072/zenodo.687052",
            "eia923": "10.5072/zenodo.687071",
            "epacems": "10.5072/zenodo.672963",
            "ferc1": "10.5072/zenodo.687072",
            "ferc714": "10.5072/zenodo.672224",
        },
        "production": {
            "censusdp1tract": "10.5281/zenodo.4127049",
            "eia860": "10.5281/zenodo.4127027",
            "eia860m": "10.5281/zenodo.4281337",
            "eia861": "10.5281/zenodo.4127029",
            "eia923": "10.5281/zenodo.4127040",
            "epacems": "10.5281/zenodo.4127055",
            "ferc1": "10.5281/zenodo.4127044",
            "ferc714": "10.5281/zenodo.4127101",
        },
    }
    API_ROOT = {
        "sandbox": "https://sandbox.zenodo.org/api",
        "production": "https://zenodo.org/api",
    }

    def __init__(self, sandbox: bool = False, timeout: float = 15.0):
        backend = "sandbox" if sandbox else "production"
        self._api_root = self.API_ROOT[backend]
        self._token = self.TOKEN[backend]
        self._dataset_to_doi = self.DOI[backend]
        self._descriptor_cache = {}  # type: Dict[str, DatapackageDescriptor]

        self.timeout = timeout
        retries = Retry(backoff_factor=2, total=3,
                        status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retries)

        self.http = requests.Session()
        self.http.mount("http://", adapter)
        self.http.mount("https://", adapter)

    def _fetch_from_url(self, url: str) -> requests.Response:
        # logger.info(f"Retrieving {url} from zenodo")
        response = self.http.get(
            url,
            params={"access_token": self._token},
            timeout=self.timeout)
        if response.status_code == requests.codes.ok:
            # logger.info(f"Successfully downloaded {url}")
            return response
        else:
            raise ValueError(f"Could not download {url}: {response.text}")

    def _doi_to_url(self, doi: str) -> str:
        """Returns url that holds the datapackage for given doi."""
        match = re.search(r"zenodo.([\d]+)", doi)
        if match is None:
            raise ValueError(f"Invalid doi {doi}")

        zen_id = int(match.groups()[0])
        return f"{self._api_root}/deposit/depositions/{zen_id}"

    def get_descriptor(self, dataset: str) -> DatapackageDescriptor:
        doi = self._dataset_to_doi.get(dataset)
        if not doi:
            raise KeyError(f"No doi found for dataset {dataset}")
        if doi not in self._descriptor_cache:
            dpkg = self._fetch_from_url(self._doi_to_url(doi))
            for f in dpkg.json()["files"]:
                if f["filename"] == "datapackage.json":
                    resp = self._fetch_from_url(f["links"]["download"])
                    self._descriptor_cache[doi] = DatapackageDescriptor(resp.json(), dataset=dataset, doi=doi)
                    break
            else:
                raise RuntimeError(f"Zenodo datapackage for {dataset}/{doi} does not contain valid datapackage.json")
        return self._descriptor_cache[doi]

    def get_resource_key(self, dataset: str, name: str) -> PudlResourceKey:
        """Returns PudlResourceKey for given resource."""
        return PudlResourceKey(dataset, self._dataset_to_doi[dataset], name)

    def get_doi(self, dataset: str) -> str:
        """Returns DOI for given dataset."""
        return self._dataset_to_doi[dataset]

    def get_resource(self, res: PudlResourceKey) -> bytes:
        """Given resource key, retrieve contents of the file from zenodo."""
        url = self.get_descriptor(res.dataset).get_resource_path(res.name)
        return self._fetch_from_url(url).content

    def get_known_datasets(self) -> List[str]:
        """Returns list of supported datasets."""
        return sorted(self._dataset_to_doi)




class Datastore:
    """Handle connections and downloading of Zenodo Source archives."""

    def __init__(
        self,
        local_cache_path: Optional[Path] = None,
        gcs_cache_path: Optional[str] = None,
        sandbox: bool = False,
        timeout: float = 15
    ):
    # TODO(rousik): figure out an efficient way to configure datastore caching
        """
        Datastore manages file retrieval for PUDL datasets.

        Args:
            local_cache_path (Path): if provided, LocalFileCache pointed at the data
              subdirectory of this path will be used with this Datastore.
            gcs_cache_path (str): if provided, GoogleCloudStorageCache will be used
              to retrieve data files. The path is expected to have the following
              format: gs://bucket[/path_prefix]
            sandbox (bool): if True, use sandbox zenodo backend when retrieving files,
              otherwise use production. This affects which zenodo servers are contacted
              as well as dois used for each dataset.
            timeout (floaTR): connection timeouts (in seconds) to use when connecting
              to Zenodo servers.

        """
        self.cache = resource_cache.LayeredCache()
        self._datapackage_descriptors = {}  # type: Dict[str, DatapackageDescriptor]

        if local_cache_path:
            self.cache.add_cache_layer(
                resource_cache.LocalFileCache(local_cache_path))
        if gcs_cache_path:
            self.cache.add_cache_layer(resource_cache.GoogleCloudStorageCache(gcs_cache_path))

        self._zenodo_fetcher = ZenodoFetcher(
            sandbox=sandbox,
            timeout=timeout)

    def get_known_datasets(self) -> List[str]:
        """Returns list of supported datasets."""
        return self._zenodo_fetcher.get_known_datasets()

    def get_datapackage_descriptor(self, dataset: str) -> DatapackageDescriptor:
        """Fetch datapackage descriptor for given dataset either from cache or from zenodo."""
        doi = self._zenodo_fetcher.get_doi(dataset)
        if doi not in self._datapackage_descriptors:
            res = PudlResourceKey(dataset, doi, "datapackage.json")
            if self.cache.contains(res):
                self._datapackage_descriptors[doi] = DatapackageDescriptor(
                    json.loads(self.cache.get(res).decode('utf-8')),
                    dataset=dataset,
                    doi=doi)
            else:
                desc = self._zenodo_fetcher.get_descriptor(dataset)
                self._datapackage_descriptors[doi] = desc
                self.cache.set(res, bytes(desc.get_json_string(), "utf-8"))
        return self._datapackage_descriptors[doi]

    def get_resources(self, dataset: str, **filters: Any) -> Iterator[Tuple[PudlResourceKey, bytes]]:
        """Return content of the matching resources.

        Args:
            dataset (str): name of the dataset to query
            **filters (key=val): only return resources that match the key-value mapping in their
            metadata["parts"].

        Yields:
            (PudlResourceKey, io.BytesIO) holding content for each matching resource
        """
        desc = self.get_datapackage_descriptor(dataset)
        for res in desc.get_resources(**filters):
            if self.cache.contains(res):
                logger.debug(f"Retrieved {res} from cache.")
                yield (res, self.cache.get(res))
            else:
                logger.debug(f"Retrieved {res} from zenodo.")
                contents = self._zenodo_fetcher.get_resource(res)
                self.cache.set(res, contents)
                yield (res, contents)

    def get_unique_resource(self, dataset: str, **filters: Any) -> bytes:
        """Returns content of a resource assuming there is exactly one that matches."""
        res = self.get_resources(dataset, **filters)
        try:
            _, content = next(res)
        except StopIteration:
            raise KeyError(f"No resources found for {dataset}: {filters}")
        try:
            next(res)
        except StopIteration:
            return content
        raise KeyError(f"Multiple resources found for {dataset}: {filters}")

    def get_zipfile_resource(self, dataset: str, **filters: Any) -> zipfile.ZipFile:
        return zipfile.ZipFile(io.BytesIO(self.get_unique_resource(dataset, **filters)))


def parse_command_line():
    """Collect the command line arguments."""
    prod_dois = "\n".join([f"    - {x}" for x in DOI["production"].keys()])
    sand_dois = "\n".join([f"    - {x}" for x in DOI["sandbox"].keys()])

    dataset_msg = f"""
Available Production Datasets:
{prod_dois}

Available Sandbox Datasets:
{sand_dois}"""

    parser = argparse.ArgumentParser(
        description="Download and cache ETL source data from Zenodo.",
        epilog=dataset_msg,
        formatter_class=argparse.RawTextHelpFormatter
    )

    parser.add_argument(
        "--dataset",
        help="Download the specified dataset only. See below for available options. "
        "The default is to download all, which may take an hour or more."
        "speed."
    )
    parser.add_argument(
        "--pudl_in",
        help="Override pudl_in directory, defaults to setting in ~/.pudl.yml",
    )
    parser.add_argument(
        "--validate",
        help="Validate locally cached datapackages, but don't download anything.",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--sandbox",
        help="Download data from Zenodo sandbox server. For testing purposes only.",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--loglevel",
        help="Set logging level (DEBUG, INFO, WARNING, ERROR, or CRITICAL).",
        default="INFO",
    )
    parser.add_argument(
        "--quiet",
        help="Do not send logging messages to stdout.",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--populate-gcs-cache",
        default=None,
        help="If specified, upload data resources to this GCS bucket"
        )
    parser.add_argument(
        "--partition",
        type=str,
        help="k1=v1,k2=v2 selectors to apply when retrieving resources.")

    return parser.parse_args()


def main():
    """Cache datasets."""
    args = parse_command_line()

    # logger = logging.getLogger(pudl.__name__)
    log_format = '%(asctime)s [%(levelname)8s] %(name)s:%(lineno)s %(message)s'
    coloredlogs.install(fmt=log_format, level='INFO', logger=logger)

    logger.setLevel(args.loglevel)
    # if not args.quiet:
    #    logger.addHandler(logging.StreamHandler())

    pudl_in = args.pudl_in

    if pudl_in is None:
        with PUDL_YML.open() as f:
            cfg = yaml.safe_load(f)
            pudl_in = Path(cfg["pudl_in"])
    else:
        pudl_in = Path(pudl_in)

    dstore = Datastore(sandbox=args.sandbox)
    if args.populate_gcs_cache:
        dstore.cache.add_cache_layer(resource_cache.GoogleCloudStorageCache(args.populate_gcs_cache))
    else:
        dstore.cache.add_cache_layer(resource_cache.LocalFileCache(Path(pudl_in) / "data"))

    datasets = []
    if args.dataset:
        datasets.append(args.dataset)
    else:
        datasets = dstore.get_known_datasets()

    partition = {}
    if args.partition:
        for kv in args.partition.split(','):
            k, v = kv.split('=')
            partition[k] = v
        logger.info(f"Only retrieving resources for partition: {partition}")

    for selection in datasets:
        if args.validate:
            dstore.validate(selection)
        else:
            for res, _ in dstore.get_resources(selection, **partition):
                logger.info(f"Retrieved {res}.")

if __name__ == "__main__":
    sys.exit(main())
