import copy
import datetime as dt
import json
import os
import pathlib
from typing import Dict, Any, List, Optional, Tuple
from abc import ABC, abstractmethod
import re
import click
import git
import mlflow
import mlflow.entities
import pkg_resources
import requests
from databricks_cli.configure.config import _get_api_client  # noqa
from databricks_cli.configure.provider import (
    DEFAULT_SECTION,
    ProfileConfigProvider,
    EnvironmentVariableConfigProvider,
    DatabricksConfig,
)
from databricks_cli.dbfs.api import DbfsService
from databricks_cli.sdk import ClusterService
from databricks_cli.sdk.api_client import ApiClient
from databricks_cli.workspace.api import WorkspaceService
from path import Path
from retry import retry
import ruamel.yaml
from setuptools import sandbox
import emoji

DBX_PATH = ".dbx"
INFO_FILE_PATH = f"{DBX_PATH}/project.json"
LOCK_FILE_PATH = f"{DBX_PATH}/lock.json"
DATABRICKS_MLFLOW_URI = "databricks"
DEFAULT_DEPLOYMENT_FILE_PATH = "conf/deployment.json"

PROJECTS_RELATIVE_PATH = "templates/projects"
TEMPLATE_CHOICES = pkg_resources.resource_listdir("dbx", PROJECTS_RELATIVE_PATH)
TEMPLATE_ROOT_PATH = pathlib.Path(pkg_resources.resource_filename("dbx", PROJECTS_RELATIVE_PATH))


def dbx_echo(message: str):
    formatted_time = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    formatted_message = f"[dbx][{formatted_time}] {message}"
    try:
        click.echo(emoji.emojize(formatted_message))
    # this is a fix for unicode error on some platforms as per https://github.com/databrickslabs/dbx/issues/121
    except UnicodeEncodeError:
        click.echo(formatted_message)


def parse_multiple(multiple_argument: List[str]) -> Dict[str, str]:
    tags_splitted = [t.split("=") for t in multiple_argument]
    tags_dict = {t[0]: t[1] for t in tags_splitted}
    return tags_dict


def read_json(file_path: str) -> Dict[str, Any]:
    with open(file_path, "r") as f:
        return json.load(f)


def write_json(content: Dict[str, Any], file_path: str):
    with open(file_path, "w") as f:
        json.dump(content, f, indent=4)


def update_json(new_content: Dict[str, Any], file_path: str):
    try:
        content = read_json(file_path)
    except FileNotFoundError:
        content = {}
    content.update(new_content)
    write_json(content, file_path)


class ContextLockFile:
    @staticmethod
    def set_context(context_id: str) -> None:
        update_json({"context_id": context_id}, LOCK_FILE_PATH)

    @staticmethod
    def get_context() -> Any:
        if pathlib.Path(LOCK_FILE_PATH).exists():
            return read_json(LOCK_FILE_PATH).get("context_id")
        else:
            return None


class AbstractDeploymentConfig(ABC):
    def __init__(self, path):
        self._path = path

    @abstractmethod
    def get_environment(self, environment: str) -> Any:
        pass

    @abstractmethod
    def get_all_environment_names(self) -> Any:
        pass

    def _get_file_extension(self):
        return self._path.split(".").pop()


class YamlDeploymentConfig(AbstractDeploymentConfig):
    # only for reading purposes.
    # if you need to round-trip see this: https://yaml.readthedocs.io/en/latest/overview.html

    # ENV variable pattern and tag
    PATTERN = re.compile(r".*?\$\{([^}{:]+)(:[^}^{]+)?\}.*?")
    YAML_TAG = "!ENV"

    def resolve_env_vars(self, loader: ruamel.yaml.SafeLoader, node: ruamel.yaml.Node):
        value = loader.construct_scalar(node)
        match = self.PATTERN.findall(value)

        full_value = value
        if match:
            for var in match:
                env_var_name, default_val = var
                env_val = os.environ.get(env_var_name, "")

                if env_val == "" and default_val:
                    env_val = default_val[1:]  # Remove the leading colon

                val_to_replace = "".join(var)
                full_value = full_value.replace(f"${{{val_to_replace}}}", env_val)

        return full_value

    def _read_yaml(self, file_path: str) -> Dict[str, Any]:
        loader = ruamel.yaml.SafeLoader

        # Tags indicate where to search for the pattern
        # In this case, it is !ENV
        loader.add_implicit_resolver(self.YAML_TAG, self.PATTERN, None)

        # Env variable resolver
        loader.add_constructor(self.YAML_TAG, self.resolve_env_vars)

        with open(file_path, "r") as f:
            return ruamel.yaml.load(f, Loader=loader)

    def get_environment(self, environment: str) -> Any:
        return self._read_yaml(self._path).get("environments").get(environment)

    def get_all_environment_names(self) -> List[str]:
        return list(self._read_yaml(self._path).get("environments").keys())


class JsonDeploymentConfig(AbstractDeploymentConfig):
    # ENV variable pattern
    PATTERN = re.compile(r"\$\{([^}{:]+)(:[^}^{]+)?\}")

    def resolve_env_vars(self, json_obj: Dict[str, Any]) -> Dict[str, Any]:
        json_str = json.dumps(json_obj)

        def _env_resolver(match):
            env_var_name, default_val = match.group(1, 2)
            env_val = os.environ.get(env_var_name, "")

            if env_val == "" and default_val:
                env_val = default_val[1:]  # Remove the leading colon

            return json.dumps(env_val)[1:-1]

        return json.loads(self.PATTERN.sub(_env_resolver, json_str))

    def get_environment(self, environment: str) -> Any:
        return self.resolve_env_vars(read_json(self._path)).get(environment)

    def get_all_environment_names(self) -> Any:
        return list(self.resolve_env_vars(read_json(self._path)).keys())


def get_deployment_config(path: str) -> AbstractDeploymentConfig:
    ext = path.split(".").pop()
    if ext == "json":
        return JsonDeploymentConfig(path)
    elif ext in ["yml", "yaml"]:
        return YamlDeploymentConfig(path)
    else:
        raise Exception(f"Undefined config file handler for extension: {ext}")


class InfoFile:
    @staticmethod
    def _create_dir() -> None:
        if not Path(DBX_PATH).exists():
            dbx_echo("dbx directory is not present, creating it")
            os.mkdir(DBX_PATH)

    @staticmethod
    def _create_lock_file() -> None:
        if not Path(LOCK_FILE_PATH).exists():
            pathlib.Path(LOCK_FILE_PATH).write_text("{}")

    @staticmethod
    def initialize():

        InfoFile._create_dir()
        InfoFile._create_lock_file()

        init_content = {"environments": {}}
        write_json(init_content, INFO_FILE_PATH)

    @staticmethod
    def update(content: Dict[str, Any]) -> None:
        update_json(content, INFO_FILE_PATH)

    @staticmethod
    def get(item: str) -> Any:
        if not pathlib.Path(INFO_FILE_PATH).exists():
            raise Exception("Your project is not yet configured, please configure it via `dbx configure`")
        return read_json(INFO_FILE_PATH).get(item)


class ApiV1Client:
    def __init__(self, api_client: ApiClient):
        self.v1_client = copy.deepcopy(api_client)
        self.v1_client.api_version = "1.2"

    def get_command_status(self, payload) -> Dict[Any, Any]:
        result = self.v1_client.perform_query(method="GET", path="/commands/status", data=payload)
        return result

    def cancel_command(self, payload) -> None:
        self.v1_client.perform_query(method="POST", path="/commands/cancel", data=payload)

    def execute_command(self, payload) -> Dict[Any, Any]:
        result = self.v1_client.perform_query(method="POST", path="/commands/execute", data=payload)
        return result

    def get_context_status(self, payload):
        try:
            result = self.v1_client.perform_query(method="GET", path="/contexts/status", data=payload)
            return result
        except requests.exceptions.HTTPError:
            return None

    # sometimes cluster is already in the status="RUNNING", however it couldn't yet provide execution context
    # to make the execute command stable is such situations, we add retry handler.
    @retry(tries=10, delay=5, backoff=5)
    def create_context(self, payload):
        result = self.v1_client.perform_query(method="POST", path="/contexts/create", data=payload)
        return result


def generate_filter_string(env: str) -> str:
    general_filters = [
        f"tags.dbx_environment = '{env}'",
        "tags.dbx_status = 'SUCCESS'",
        "tags.dbx_action_type = 'deploy'",
    ]

    branch_name = get_current_branch_name()
    if branch_name:
        general_filters.append(f"tags.dbx_branch_name = '{branch_name}'")

    all_filters = general_filters
    filter_string = " and ".join(all_filters)
    return filter_string


def environment_option(f):
    return click.option(
        "-e",
        "--environment",
        required=False,
        default="default",
        help="""Environment name. \n
            If not provided, :code:`default` will be used.""",
    )(f)


def profile_option(f):
    return click.option(
        "--profile",
        required=False,
        default=DEFAULT_SECTION,
        help="""CLI connection profile to use.\n
             The default profile is :code:`DEFAULT`.""",
    )(f)


def _prepare_workspace_dir(api_client: ApiClient, ws_dir: str):
    p = str(pathlib.PurePosixPath(ws_dir).parent)
    service = WorkspaceService(api_client)
    service.mkdirs(p)


def get_environment_data(environment: str) -> Dict[str, Any]:
    environment_data = InfoFile.get("environments").get(environment)

    if not environment_data:
        raise Exception(f"No environment {environment} provided in the project file")
    return environment_data


def pick_config(environment_data: Dict[str, Any]) -> Tuple[str, DatabricksConfig]:
    config = EnvironmentVariableConfigProvider().get_config()
    if config:
        config_type = "ENV"
        dbx_echo("Using configuration from the environment variables")
    else:
        dbx_echo("No environment variables provided, using the ~/.databrickscfg")
        config = ProfileConfigProvider(environment_data["profile"]).get_config()
        config_type = "PROFILE"
        if not config:
            raise Exception(
                f"""Couldn't get profile with name: {environment_data["profile"]}. Please check the config settings"""
            )
    return config_type, config


def prepare_environment(environment: str) -> ApiClient:
    environment_data = get_environment_data(environment)

    config_type, config = pick_config(environment_data)

    try:
        api_client = _get_api_client(config, command_name="cicdtemplates-")
    except IndexError as e:
        dbx_echo(
            """
        Error during initializing the API client.
        Probably, env variable DATABRICKS_HOST is set, but cannot be properly parsed.
        Please verify that DATABRICKS_HOST is in format https://<your-workspace-host>
        Original exception is below:
        """
        )
        raise e

    _prepare_workspace_dir(api_client, environment_data["workspace_dir"])

    if config_type == "ENV":
        mlflow.set_tracking_uri(DATABRICKS_MLFLOW_URI)
    elif config_type == "PROFILE":
        mlflow.set_tracking_uri(f'{DATABRICKS_MLFLOW_URI}://{environment_data["profile"]}')
    else:
        raise NotImplementedError(f"Config type: {config_type} is not implemented")

    experiment: Optional[mlflow.entities.Experiment] = mlflow.get_experiment_by_name(environment_data["workspace_dir"])

    # if there is no experiment
    if not experiment:
        mlflow.create_experiment(environment_data["workspace_dir"], environment_data["artifact_location"])
    else:
        # verify experiment location
        if experiment.artifact_location != environment_data["artifact_location"]:
            raise Exception(
                f"Required location of experiment {environment_data['workspace_dir']} "
                f"doesn't match the project defined one: \n"
                f"\t experiment artifact location: {experiment.artifact_location} \n"
                f"\t project artifact location   : {environment_data['artifact_location']} \n"
                f"Change of experiment location is currently not supported in MLflow. "
                f"Please change the experiment name to create a new experiment."
            )

    mlflow.set_experiment(environment_data["workspace_dir"])

    return api_client


def get_package_file() -> Optional[pathlib.Path]:
    dbx_echo("Locating package file")
    file_locator = list(pathlib.Path("dist").glob("*.whl"))
    sorted_locator = sorted(file_locator, key=os.path.getmtime)  # get latest modified file, aka latest package version
    if sorted_locator:
        file_path = sorted_locator[-1]
        dbx_echo(f"Package file located in: {file_path}")
        return file_path
    else:
        dbx_echo("Package file was not found")
        return None


def handle_package(rebuild_arg):
    if rebuild_arg:
        dbx_echo("No rebuild will be done, please ensure that the package distribution is in dist folder")
    else:
        dbx_echo("Re-building package")
        if not pathlib.Path("setup.py").exists():
            raise Exception(
                "No setup.py provided in project directory. Please create one, or disable rebuild via --no-rebuild"
            )
        sandbox.run_setup("setup.py", ["-q", "clean", "bdist_wheel"])
        dbx_echo("Package re-build finished")


class FileUploader:
    def __init__(self, api_client: ApiClient, is_strict: Optional[bool] = False):
        """
        api_client - Databricks API Client
        is_strict - require strict path adjustment policy
        """
        self._dbfs_service = DbfsService(api_client)
        self.is_strict = is_strict

    def file_exists(self, file_path: str):
        try:
            self._dbfs_service.get_status(file_path)
            return True
        except:  # noqa
            return False

    @retry(tries=3, delay=1, backoff=0.3)
    def upload_file(self, file_path: pathlib.Path):
        posix_path_str = file_path.as_posix()
        posix_path = pathlib.PurePosixPath(posix_path_str)
        dbx_echo(f"Deploying file: {file_path}")
        mlflow.log_artifact(str(file_path), str(posix_path.parent))


def get_current_branch_name() -> Optional[str]:
    if "GITHUB_REF" in os.environ:
        ref = os.environ["GITHUB_REF"].split("/")
        return ref[-1]
    else:
        try:
            repo = git.Repo(".", search_parent_directories=True)
            if repo.head.is_detached:
                return None
            else:
                return repo.active_branch.name
        except git.InvalidGitRepositoryError:
            return None


def _preprocess_cluster_args(api_client: ApiClient, cluster_name: Optional[str], cluster_id: Optional[str]) -> str:
    cluster_service = ClusterService(api_client)

    if not cluster_name and not cluster_id:
        raise RuntimeError("Parameters --cluster-name and --cluster-id couldn't be empty at the same time.")

    if cluster_name:

        existing_clusters = [
            c for c in cluster_service.list_clusters().get("clusters") if not c.get("cluster_name").startswith("job-")
        ]
        matching_clusters = [c for c in existing_clusters if c.get("cluster_name") == cluster_name]

        if not matching_clusters:
            cluster_names = [c["cluster_name"] for c in existing_clusters]
            raise NameError(f"No clusters with name {cluster_name} found. Available clusters are: {cluster_names} ")
        if len(matching_clusters) > 1:
            raise NameError(f"Found more then one cluster with name {cluster_name}: {matching_clusters}")

        cluster_id = matching_clusters[0]["cluster_id"]
    else:
        if not cluster_service.get_cluster(cluster_id):
            raise NameError(f"Cluster with id {cluster_id} not found")

    return cluster_id
